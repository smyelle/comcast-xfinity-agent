"""flows lint — renderers, the shared LintContext read-model, and rule stragglers.

Companion to `test_lint.py` (which covers rule firing / near-miss guards). This file
targets the parts a rule test never reaches:

  * `lint/render.py` — the human TTY view, the versioned JSON view, `--list-rules`
    and `--explain`. The rendered text IS the product for a CLI user, so the
    assertions here are on exact strings, not substrings, wherever the contract is
    exact (severity column width, the clean line, the summary line).
  * `lint/context.py` — the fillability fixpoint's non-`setter` roots, the spoken-text
    walker's task/cancel/no_input arms, and the shape-tolerant helpers.
  * `lint/models.py`, `lint/registry.py`, and the branches in `rules/` that only a
    differently-shaped config reaches.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_lint_coverage.py
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from flows.config.models import DiagnosticFix, NodeAnchor
from flows.lint import LintContext, run_rules
from flows.lint.context import (
    build_context,
    normalize_sources,
    relative_field,
    _task_input_slots,
)
from flows.lint.models import (
    CATEGORY_LETTER,
    Category,
    Finding,
    LintReport,
    Location,
    Summary,
    severity_rank,
)
from flows.lint.registry import Rule, RuleRegistry, load_all_rules
from flows.lint.render import (
    render_explain,
    render_human,
    render_json,
    render_list_rules,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _ctx(configs, *, bodies=None, available=None, host_cid=None, **appkw) -> LintContext:
  app = SimpleNamespace(
      lint_ignore=appkw.get("lint_ignore", []),
      extra_agent_tools=appkw.get("extra_agent_tools", []),
      host=appkw.get("host"),
      agents=appkw.get("agents", []),
      automatic_fillers=appkw.get("automatic_fillers", False),
  )
  return LintContext(app=app, configs=configs, bodies=bodies or {},
                     available=available or [], host_cid=host_cid)


def _codes(report):
  return [f.code for f in report.findings if not f.suppressed_by]


def _finding(**kw) -> Finding:
  """A Finding with every required field defaulted, so a test names only what it means."""
  base = dict(code="FLV001", category=Category.VOICE, severity="warning",
              title="dash in spoken copy", message="Rewrite the line.")
  base.update(kw)
  return Finding(**base)


def _gutters(rendered: str) -> list[str]:
  """The `  <severity> <code>  <title>` lines: 2-space indent, never the 9-space body."""
  return [ln for ln in rendered.splitlines()
          if ln.startswith("  ") and not ln.startswith("   ")]


def _report(*findings, ran=("FLV001",)) -> LintReport:
  fs = list(findings)
  return LintReport(findings=fs, summary=Summary.of(fs), ran_rules=list(ran))


class _FakeRule(Rule):
  """A stand-in rule, registered into a throwaway registry.

  Built by hand rather than with `@rule` on purpose: the decorator registers into
  the process-global `RULES`, and a test rule leaking into it would change what
  every other test in the suite sees from `load_all_rules()`.
  """


def _mk_rule(code, category, severity, title, docs=None, doc=None) -> Rule:
  cls = type(f"Rule_{code}", (_FakeRule,), {"__doc__": doc})
  cls.code = code
  cls.category = category
  cls.default_severity = severity
  cls.title = title
  cls.docs = docs
  return cls()


# ==========================================================================
# render.py — render_human
# ==========================================================================

def test_render_human_clean_report_says_clean_twice():
  """The zero-finding case: a headline a human reads and a machine-stable summary."""
  assert render_human(_report()) == "lint: clean — no findings\nSummary: clean"


def test_render_human_renders_one_finding_verbatim():
  """The exact contract: header, an 8-column severity gutter, then the wrapped
  message at a 9-space indent. Asserted whole because the columns are the format."""
  f = _finding(location=Location(config_id="acme_flow", node="member_id",
                                 json_path="slots[0].ask"),
               message="Rewrite the dash as a comma.")
  assert render_human(_report(f)) == (
      "acme_flow > member_id > slots[0].ask\n"
      "  warning FLV001  dash in spoken copy\n"
      "         Rewrite the dash as a comma.\n"
      "\n"
      "Summary: 1 warning"
  )


def test_render_human_uses_app_header_when_the_finding_has_no_location():
  out = render_human(_report(_finding()))
  assert out.splitlines()[0] == "(app)"


def test_render_human_shows_the_fix_and_docs_lines_only_when_present():
  bare = render_human(_report(_finding()))
  assert "fix:" not in bare and "docs:" not in bare

  rich = render_human(_report(_finding(
      docs_url="flows lint --explain FLV001",
      fix=DiagnosticFix(label="replace the dash", patch={"op": "set"}))))
  assert "         fix: replace the dash  (autofixable)" in rich
  assert "         docs: flows lint --explain FLV001" in rich
  assert rich.endswith("Summary: 1 warning  (1 autofixable)")


def test_render_human_labels_every_severity_and_orders_by_rank():
  """`needs_review` renders as `review`; findings sort most-severe first regardless
  of the order they were appended in."""
  report = _report(
      _finding(code="FLC101", category=Category.CONVERSATION, severity="info",
               title="info one"),
      _finding(code="FLV001", severity="needs_review", title="review one"),
      _finding(code="FLR001", category=Category.REACHABILITY, severity="error",
               title="error one"),
      _finding(code="FLM001", category=Category.MODEL_RELIANCE, severity="warning",
               title="warning one"),
  )
  assert _gutters(render_human(report)) == [
      "  error   FLR001  error one",
      "  warning FLM001  warning one",
      "  info    FLC101  info one",
      "  review  FLV001  review one",
  ]


def test_render_human_summary_counts_and_pluralizes_per_severity():
  report = _report(
      _finding(code="FLR001", category=Category.REACHABILITY, severity="error"),
      _finding(code="FLR002", category=Category.REACHABILITY, severity="error"),
      _finding(code="FLM001", category=Category.MODEL_RELIANCE, severity="warning"),
      _finding(code="FLC101", category=Category.CONVERSATION, severity="info"),
      _finding(code="FLC102", category=Category.CONVERSATION, severity="info"),
      _finding(code="FLV001", severity="needs_review"),
  )
  last = render_human(report).splitlines()[-1]
  assert last == "Summary: 2 errors, 1 warning, 2 infos, 1 review"


def test_render_human_sorts_within_a_severity_by_config_then_path_then_code():
  report = _report(
      _finding(code="FLV002", location=Location(config_id="b", json_path="slots[0]")),
      _finding(code="FLV001", location=Location(config_id="a", json_path="slots[9]")),
      _finding(code="FLV001", location=Location(config_id="a", json_path="slots[1]")),
      _finding(code="FLV000", location=Location(config_id="a", json_path="slots[1]")),
  )
  headers = [ln for ln in render_human(report).splitlines()
             if ln and not ln.startswith((" ", "Summary"))]
  assert headers == ["a > slots[1]", "a > slots[1]", "a > slots[9]", "b > slots[0]"]
  assert [ln.split()[1] for ln in _gutters(render_human(report))] == [
      "FLV000", "FLV001", "FLV001", "FLV002"]


def test_render_human_hides_suppressed_findings_but_still_counts_them():
  f = _finding(suppressed_by="app.lint_ignore")
  hidden = render_human(_report(f))
  assert hidden == "lint: clean — no findings\nSummary: clean  (1 suppressed)"

  shown = render_human(_report(f), show_suppressed=True)
  assert "lint: clean" not in shown
  assert "  warning FLV001  dash in spoken copy  suppressed" in shown
  assert shown.endswith("Summary: clean  (1 suppressed)")


def test_render_human_wraps_a_long_message_at_the_indented_width():
  """88 columns minus the 9-space indent: no rendered line may exceed 88."""
  msg = " ".join(["reword"] * 60)
  out = render_human(_report(_finding(message=msg)))
  body = [ln for ln in out.splitlines() if ln.startswith("         ")]
  assert len(body) > 1, "a 60-word message must wrap"
  assert all(len(ln) <= 88 for ln in body)
  assert " ".join(ln.strip() for ln in body) == msg


def test_render_human_emits_an_indented_blank_for_an_empty_message():
  """`textwrap.wrap("")` is `[]`; the renderer must still emit a body line rather
  than collapse the record and shift the fix/docs lines up under the gutter."""
  out = render_human(_report(_finding(message="", fix=DiagnosticFix(
      label="x", patch={})))).splitlines()
  assert out[2] == "         "
  assert out[3] == "         fix: x  (autofixable)"


def test_render_human_passes_unicode_through_unmangled():
  out = render_human(_report(_finding(
      message="Say “un momento, por favor” — 少々お待ちください.",
      location=Location(node="saludo"))))
  assert "Say “un momento, por favor” — 少々お待ちください." in out
  assert out.splitlines()[0] == "saludo"


def test_render_human_of_many_findings_emits_one_record_each():
  report = _report(*[
      _finding(code=f"FLV{i:03d}", location=Location(node=f"slot_{i}"))
      for i in range(25)])
  out = render_human(report)
  assert out.count("dash in spoken copy") == 25
  assert out.endswith("Summary: 25 warnings")


# ==========================================================================
# render.py — render_json
# ==========================================================================

def test_render_json_round_trips_to_the_versioned_single_shape():
  f = _finding(location=Location(config_id="acme_flow", node="member_id"),
               anchor=NodeAnchor(kind="slot", ref="member_id", field="ask"),
               rationale="dashes read as pauses",
               fix_id="rewrite_dash",
               docs_url="flows lint --explain FLV001",
               related=["issue #1"])
  blob = json.loads(render_json(_report(f, ran=("FLV001", "FLC101"))))
  assert set(blob) == {"schema_version", "findings", "summary", "ran_rules"}
  assert blob["schema_version"] == 1
  assert blob["ran_rules"] == ["FLV001", "FLC101"]
  assert blob["findings"][0]["category"] == "voice"       # the enum's VALUE
  assert blob["findings"][0]["location"]["config_id"] == "acme_flow"
  assert blob["findings"][0]["anchor"] == {
      "kind": "slot", "ref": "member_id", "field": "ask"}
  assert blob["summary"] == {"total": 1, "by_severity": {"warning": 1},
                             "by_category": {"voice": 1}, "fixable": 0,
                             "suppressed": 0}


def test_render_json_of_an_empty_report_is_still_the_full_shape():
  blob = json.loads(render_json(_report(ran=())))
  assert blob == {"schema_version": 1, "findings": [], "ran_rules": [],
                  "summary": {"total": 0, "by_severity": {}, "by_category": {},
                              "fixable": 0, "suppressed": 0}}


def test_render_json_keeps_suppressed_findings_in_the_record():
  """The human view hides them; the machine view must not, or a CI diff of two
  runs would silently lose the suppression."""
  blob = json.loads(render_json(_report(_finding(suppressed_by="app.lint_ignore"))))
  assert len(blob["findings"]) == 1
  assert blob["findings"][0]["suppressed_by"] == "app.lint_ignore"
  assert blob["summary"]["total"] == 0 and blob["summary"]["suppressed"] == 1


def test_render_json_is_indented_and_unicode_safe():
  out = render_json(_report(_finding(message="少々お待ちください")))
  assert "\n  " in out, "indent=2 keeps the blob diffable"
  assert json.loads(out)["findings"][0]["message"] == "少々お待ちください"


# ==========================================================================
# render.py — render_list_rules / render_explain
# ==========================================================================

def _two_rule_registry() -> RuleRegistry:
  reg = RuleRegistry()
  reg.register(_mk_rule("FLV001", Category.VOICE, "needs_review", "dash in copy"))
  reg.register(_mk_rule("FLC101", Category.CONVERSATION, "info", "no silence ladder",
                        docs="silence"))
  return reg


def test_render_list_rules_groups_by_category_in_code_order():
  out = render_list_rules(_two_rule_registry())
  assert out == (
      "2 rules:\n"
      "\n[conversation]\n"
      "  FLC101  info          no silence ladder\n"
      "\n[voice]\n"
      "  FLV001  needs_review  dash in copy"
  )


def test_render_list_rules_repeats_a_header_when_a_category_is_not_contiguous():
  """`all()` sorts by CODE, so an interleaved category legitimately re-heads. The
  renderer only compares against the previous rule, which is the intended behavior —
  the list stays in code order rather than being silently regrouped."""
  reg = RuleRegistry()
  reg.register(_mk_rule("FLC101", Category.CONVERSATION, "info", "a"))
  reg.register(_mk_rule("FLM001", Category.MODEL_RELIANCE, "warning", "b"))
  reg.register(_mk_rule("FLC102", Category.CONVERSATION, "info", "c"))
  headers = [ln for ln in render_list_rules(reg).splitlines() if ln.startswith("[")]
  assert headers == ["[conversation]", "[model_reliance]"]
  assert render_list_rules(reg).splitlines()[0] == "3 rules:"


def test_render_list_rules_of_an_empty_registry():
  assert render_list_rules(RuleRegistry()) == "0 rules:"
  assert json.loads(render_list_rules(RuleRegistry(), as_json=True)) == []


def test_render_list_rules_json_carries_a_reachable_docs_hint():
  blob = json.loads(render_list_rules(_two_rule_registry(), as_json=True))
  assert blob == [
      {"code": "FLC101", "category": "conversation", "default_severity": "info",
       "title": "no silence ladder", "docs": "flows lint --explain FLC101",
       "catalog": "docs/lint/RULES.md"},
      {"code": "FLV001", "category": "voice", "default_severity": "needs_review",
       "title": "dash in copy", "docs": "flows lint --explain FLV001",
       "catalog": "docs/lint/RULES.md"},
  ]


def test_render_explain_prints_the_header_then_the_indented_docstring():
  r = _mk_rule("FLR001", Category.REACHABILITY, "error", "dead end",
               doc="Line one.\n\nLine two.  ")
  assert render_explain(r) == (
      "FLR001  (reachability, default error)\n"
      "  dead end\n"
      "  more: flows lint --explain FLR001   (catalog: docs/lint/RULES.md)\n"
      "\n"
      "  Line one.\n"
      "\n"
      "  Line two."
  )


def test_render_explain_survives_a_rule_with_no_docstring():
  r = _mk_rule("FLR002", Category.REACHABILITY, "error", "no docs", docs="slug")
  assert render_explain(r) == (
      "FLR002  (reachability, default error)\n"
      "  no docs\n"
      "  more: flows lint --explain FLR002   (catalog: docs/lint/RULES.md)\n\n"
  )


def test_render_explain_covers_every_registered_rule():
  """Smoke: the real registry must not have a rule whose metadata breaks the view."""
  for r in load_all_rules().all():
    text = render_explain(r)
    assert text.startswith(f"{r.code}  ({r.category.value}, default ")
    assert f"flows lint --explain {r.code}" in text


# ==========================================================================
# models.py
# ==========================================================================

@pytest.mark.parametrize("kw,want", [
    ({}, ""),
    ({"config_id": "acme"}, "acme"),
    ({"node": "member_id"}, "member_id"),
    ({"json_path": "slots[0].ask"}, "slots[0].ask"),
    ({"config_id": "acme", "node": "member_id", "json_path": "slots[0].ask"},
     "acme > member_id > slots[0].ask"),
    # json_path == node adds nothing; the label must not stutter.
    ({"config_id": "acme", "node": "no_input", "json_path": "no_input"},
     "acme > no_input"),
])
def test_location_label(kw, want):
  assert Location(**kw).label() == want


def test_finding_downcasts_to_a_diagnostic_carrying_the_code():
  anchor = NodeAnchor(kind="slot", ref="member_id", field="ask")
  fix = DiagnosticFix(label="rewrite", patch={"op": "set"})
  d = _finding(severity="error", message="Rewrite it.", anchor=anchor, fix=fix
               ).to_diagnostic()
  assert (d.severity, d.message) == ("error", "Rewrite it.")
  assert d.raw == "[FLV001] Rewrite it."
  assert d.anchor is anchor and d.fix is fix


def test_severity_rank_orders_the_vocab_and_sinks_an_unknown():
  assert [severity_rank(s) for s in
          ("error", "warning", "info", "needs_review")] == [0, 1, 2, 3]
  assert severity_rank("wat") == 99


def test_every_category_has_a_letter_and_no_two_share_one():
  letters = [CATEGORY_LETTER[c] for c in Category]
  assert len(letters) == len(Category) == len(set(letters))


def test_summary_of_excludes_suppressed_from_every_count_but_its_own():
  s = Summary.of([
      _finding(code="FLR001", category=Category.REACHABILITY, severity="error",
               fix=DiagnosticFix(label="f", patch={})),
      _finding(code="FLV001", severity="warning"),
      _finding(code="FLV002", severity="warning", suppressed_by="app.lint_ignore",
               fix=DiagnosticFix(label="f", patch={})),
  ])
  assert s.total == 2
  assert s.by_severity == {"error": 1, "warning": 1}
  assert s.by_category == {"reachability": 1, "voice": 1}
  assert s.fixable == 1        # the suppressed fixable one does not count
  assert s.suppressed == 1


def test_blocking_ignores_suppressed_and_respects_strict():
  r = _report(
      _finding(code="FLR001", category=Category.REACHABILITY, severity="error",
               suppressed_by="app.lint_ignore"),
      _finding(code="FLM001", category=Category.MODEL_RELIANCE, severity="warning"),
      _finding(code="FLC101", category=Category.CONVERSATION, severity="info"),
  )
  assert r.blocking() == [] and r.ok() is True
  assert [f.code for f in r.blocking(strict=True)] == ["FLM001"]
  assert r.ok(strict=True) is False


# ==========================================================================
# registry.py
# ==========================================================================

def test_registry_rejects_a_code_that_is_not_the_FL_shape():
  reg = RuleRegistry()
  with pytest.raises(ValueError, match="is not FL<category-letter><3-digit>"):
    reg.register(_mk_rule("VOICE1", Category.VOICE, "info", "t"))


def test_registry_rejects_a_letter_that_contradicts_the_category():
  reg = RuleRegistry()
  with pytest.raises(ValueError, match="should be 'V'"):
    reg.register(_mk_rule("FLR001", Category.VOICE, "info", "t"))


def test_registry_rejects_a_duplicate_code():
  reg = RuleRegistry()
  reg.register(_mk_rule("FLV001", Category.VOICE, "info", "first"))
  with pytest.raises(ValueError, match="duplicate rule code"):
    reg.register(_mk_rule("FLV001", Category.VOICE, "info", "second"))


def test_registry_get_and_clear():
  reg = _two_rule_registry()
  assert reg.get("FLV001").title == "dash in copy"
  assert reg.get("FLZ999") is None
  reg.clear()
  assert reg.all() == [] and reg.get("FLV001") is None


def test_load_all_rules_is_idempotent_and_every_code_matches_its_category():
  reg = load_all_rules()
  again = load_all_rules()
  assert reg is again
  codes = [r.code for r in reg.all()]
  assert codes == sorted(codes) and len(codes) == len(set(codes))
  for r in reg.all():
    assert r.code[2] == CATEGORY_LETTER[r.category]


def test_rule_finding_stamps_the_rule_metadata_and_honors_a_per_case_severity():
  r = _mk_rule("FLV009", Category.VOICE, "info", "a title", docs="slug")
  f = r.finding(message="m")
  assert (f.code, f.category, f.severity, f.title) == (
      "FLV009", Category.VOICE, "info", "a title")
  # No longer a URL: the old one was not a resolvable host, so every finding
  # the linter printed carried a dead link. --explain works offline.
  assert f.docs_url == "flows lint --explain FLV009"
  assert f.location == Location() and f.related == []
  assert r.finding(message="m", severity="error").severity == "error"


def test_the_rule_base_class_check_is_abstract():
  with pytest.raises(NotImplementedError):
    Rule().check(_ctx({}))


# ==========================================================================
# context.py — shape-tolerant helpers
# ==========================================================================

@pytest.mark.parametrize("src,want", [
    (None, ["user"]),
    ("announce", ["announce"]),
    (["user", "event"], ["user", "event"]),
    ([], []),
    ([1, "user", None], ["user"]),      # non-strings dropped, not crashed on
    (17, ["user"]),
])
def test_normalize_sources(src, want):
  assert normalize_sources(src) == want


@pytest.mark.parametrize("inputs,want", [
    (None, []),
    ({"member_id": "id", "zip": "z"}, ["member_id", "zip"]),
    (["member_id"], ["member_id"]),
    ([{"nope": 1}, "member_id"], ["member_id"]),
    ("member_id", []),                  # a bare string is not a valid inputs shape
])
def test_task_input_slots(inputs, want):
  assert _task_input_slots(inputs) == want


@pytest.mark.parametrize("path,want", [
    ("slots[3].ask", "ask"),
    ("tasks[1].then_response[0]", "then_response[0]"),
    ("ask", "ask"),
    ("tasks[0].on_failure.on_exhaust.open_slot", "on_failure.on_exhaust.open_slot"),
])
def test_relative_field(path, want):
  assert relative_field(path) == want


# ==========================================================================
# context.py — accessors
# ==========================================================================

def test_accessors_skip_non_dict_entries_and_unnamed_nodes():
  cfg = {"f": {
      "slots": [{"name": "a"}, "not a slot", {"no_name": 1}],
      "tasks": [{"name": "t1"}, None, {"name": "t2", "terminal": True}],
  }}
  ctx = _ctx(cfg)
  assert ctx.config_ids() == ["f"]
  assert [s.get("name") for s in ctx.slots("f")] == ["a", None]
  assert list(ctx.slot_map("f")) == ["a"]
  assert [t["name"] for t in ctx.tasks("f")] == ["t1", "t2"]
  assert list(ctx.task_map("f")) == ["t1", "t2"]
  assert [t["name"] for t in ctx.terminals("f")] == ["t2"]


def test_accessors_tolerate_missing_and_null_sections():
  ctx = _ctx({"f": {}, "g": {"slots": None, "tasks": None}})
  assert sorted(ctx.config_ids()) == ["f", "g"]
  for cid in ("f", "g"):
    assert ctx.slots(cid) == [] and ctx.tasks(cid) == []
    assert ctx.slot_map(cid) == {} and ctx.task_map(cid) == {}
    assert ctx.terminals(cid) == []
    assert ctx.fillable_slots(cid) == set()


def test_referenced_tools_merges_across_configs_and_caches():
  cfg = {
      "a": {"slots": [{"name": "s", "setter": "set_s"}], "tasks": []},
      "b": {"slots": [], "tasks": [{"name": "t", "tool": "do_t"}]},
  }
  ctx = _ctx(cfg)
  assert {"set_s", "do_t"} <= ctx.referenced_tool_names()
  first = ctx.referenced_tools()
  assert ctx.referenced_tools() is first, "the walk is cached, not repeated per rule"
  assert "transfer_to_human" in ctx.reserved_tool_names()


# ==========================================================================
# context.py — the fillability fixpoint
# ==========================================================================

def test_fillable_seeds_from_announce_event_bootstrap_and_gate_slot():
  cfg = {"f": {
      "bootstrap": {"slot": "boot_slot", "welcome_slot": "welcome"},
      "gate_slot": "verified",
      "slots": [
          {"name": "from_announce", "source": "announce"},
          {"name": "from_event", "source": ["event"], "event_key": "cb"},
          {"name": "event_no_key", "source": ["event"]},
          {"name": "asked_only", "source": "user", "ask": "?"},
      ],
      "tasks": [],
  }}
  fillable = _ctx(cfg).fillable_slots("f")
  assert fillable == {"from_announce", "from_event", "boot_slot", "welcome",
                      "verified"}
  assert "event_no_key" not in fillable, "an event source with no event_key is inert"
  assert "asked_only" not in fillable, "a plain ask is not a fillability ROOT"


def test_fillable_is_cached_per_config():
  cfg = {"f": {"slots": [{"name": "a", "setter": "s"}]}}
  ctx = _ctx(cfg)
  first = ctx.fillable_slots("f")
  ctx.configs["f"]["slots"].append({"name": "late", "setter": "s2"})
  assert ctx.fillable_slots("f") is first, "second call must hit the cache"
  assert "late" not in ctx.fillable_slots("f")


def test_a_slot_with_its_own_fillable_source_is_a_root_regardless_of_requires():
  """The seed pass takes `setter` / `announce` / `event`+`event_key` at face value and
  does NOT consult `requires`, so such a slot is fillable even behind an unmet gate.

  That is the permissive direction, which is the right one for a linter: over-marking
  a slot fillable costs a missed finding, under-marking it costs a false "unreachable"
  on a config that runs fine. Pinned here because it is the reason the fixpoint loop's
  source re-test can never fire (see the dead-branch note in the sweep report)."""
  cfg = {"f": {"slots": [
      {"name": "root", "setter": "set_root"},
      {"name": "mid", "source": "announce", "requires": ["root"]},
      {"name": "leaf", "source": ["event"], "event_key": "k", "requires": ["mid"]},
      {"name": "gated", "source": "announce", "requires": ["never_filled"]},
      # No fillable SOURCE of its own -> never fillable, met requires or not.
      {"name": "user_only", "source": "user", "ask": "?", "requires": ["root"]},
      {"name": "inert", "requires": ["root"]},
  ]}}
  fillable = _ctx(cfg).fillable_slots("f")
  assert fillable == {"root", "mid", "leaf", "gated"}
  assert "user_only" not in fillable and "inert" not in fillable


def test_task_outputs_become_fillable_only_when_inputs_and_requires_are_met():
  cfg = {"f": {
      "slots": [{"name": "member_id", "setter": "set_member"},
                {"name": "flag", "setter": "set_flag"},
                {"name": "balance"}, {"name": "never"}],
      "tasks": [
          {"name": "lookup", "tool": "get_balance",
           "inputs": {"member_id": "id"}, "requires": ["flag"],
           "outputs": {"amount": "balance"}},
          {"name": "blocked", "tool": "other", "inputs": ["balance", "missing"],
           "outputs": {"r": "never"}},
      ]}}
  fillable = _ctx(cfg).fillable_slots("f")
  assert "balance" in fillable
  assert "never" not in fillable, "a task with an unfillable input yields nothing"


def test_a_component_tasks_collect_slot_is_seeded_alongside_its_outputs():
  """A repeated component merges a `collect` list back into the parent; a plain
  task in the same config must not be re-walked by the component pass."""
  cfg = {"f": {
      "slots": [{"name": "x", "setter": "sx"}, {"name": "y"}, {"name": "items"},
                {"name": "plain_out"}],
      "tasks": [
          {"name": "plain", "tool": "t", "inputs": ["x"],
           "outputs": {"o": "plain_out"}},
          {"name": "child", "component": "sub", "inputs": ["x"],
           "outputs": {"res": "y"}, "collect": "items"},
      ]}}
  assert _ctx(cfg).fillable_slots("f") >= {"x", "y", "items", "plain_out"}


def test_user_askable_needs_both_an_ask_and_a_user_source():
  cfg = {"f": {"slots": [
      {"name": "yes", "source": "user", "ask": "?"},
      {"name": "also_yes", "ask": "?"},                       # source defaults to user
      {"name": "no_ask", "source": "user"},
      {"name": "empty_ask", "source": "user", "ask": ""},
      {"name": "not_user", "source": "announce", "ask": "?"},
      {"name": "multi", "source": ["announce", "user"], "ask": "?"},
  ]}}
  assert [s["name"] for s in _ctx(cfg).user_askable_slots("f")] == [
      "yes", "also_yes", "multi"]


# ==========================================================================
# context.py — the spoken-text walker
# ==========================================================================

def _spoken(cfg, cid="f"):
  return {(i.json_path, i.text) for i in _ctx(cfg).iter_spoken(cid)}


def test_the_walker_reaches_every_task_side_line():
  cfg = {"f": {"tasks": [{
      "name": "lookup",
      "then_say": "Here it is.",
      "filler_say": "One moment.",
      "then_response": [{"type": "text", "text": "Your balance is {b}."}],
      "then_say_variants": [{"type": "text", "text": "Balance: {b}."}],
      "on_failure": {"retry_say": "Let me try again.",
                     "on_exhaust": {"say": "I could not reach the system."}},
      "awaits": {"say": "Checking now.", "hold_say": "Still checking.",
                 "hold_ack": "Thanks for waiting.",
                 "while_waiting": ["Almost there.", None],
                 "hold_reprompts": ["Are you still with me?"]},
  }]}}
  assert _spoken(cfg) == {
      ("tasks[0].then_say", "Here it is."),
      ("tasks[0].filler_say", "One moment."),
      ("tasks[0].then_response[0].text", "Your balance is {b}."),
      ("tasks[0].then_say_variants[0].text", "Balance: {b}."),
      ("tasks[0].on_failure.retry_say", "Let me try again."),
      ("tasks[0].on_failure.on_exhaust.say", "I could not reach the system."),
      ("tasks[0].awaits.say", "Checking now."),
      ("tasks[0].awaits.hold_say", "Still checking."),
      ("tasks[0].awaits.hold_ack", "Thanks for waiting."),
      ("tasks[0].awaits.while_waiting[0]", "Almost there."),
      ("tasks[0].awaits.hold_reprompts[0]", "Are you still with me?"),
  }


def test_the_walker_reaches_the_slot_reprompt_ladders_and_exhaust_lines():
  cfg = {"f": {"slots": [{
      "name": "member_id",
      "ask": "What is your member id?",
      "hint": "It is on your card.",
      "message": "Thanks.",
      "ask_variants": [{"type": "text", "text": "Member id, please?"}],
      "validation": {"reprompts": ["I did not catch that.", None],
                     "on_exhaust": {"say": "Let me get someone."},
                     "errors": {"too_short": "That is too short."}},
      "push_back": {"reprompts": ["I do need it."], "say": "I understand."},
  }]}}
  assert _spoken(cfg) == {
      ("slots[0].ask", "What is your member id?"),
      ("slots[0].hint", "It is on your card."),
      ("slots[0].message", "Thanks."),
      ("slots[0].ask_variants[0].text", "Member id, please?"),
      ("slots[0].validation.reprompts[0]", "I did not catch that."),
      ("slots[0].validation.on_exhaust.say", "Let me get someone."),
      ("slots[0].validation.errors.too_short", "That is too short."),
      ("slots[0].push_back.reprompts[0]", "I do need it."),
      ("slots[0].push_back.say", "I understand."),
  }


def test_the_walker_reaches_no_input_readback_and_the_flow_level_lines():
  cfg = {"f": {
      "no_input": {"reprompts": ["Are you there?"],
                   "on_exhaust": {"say": "I will let you go."}},
      "all_done_say": "All set.",
      "filler_say": "Let me check.",
      "readback_response": [{"type": "text", "text": "I heard {value}."}],
  }}
  assert _spoken(cfg) == {
      ("no_input.reprompts[0]", "Are you there?"),
      ("no_input.on_exhaust.say", "I will let you go."),
      ("all_done_say", "All set."),
      ("filler_say", "Let me check."),
      ("readback_response[0].text", "I heard {value}."),
  }


def test_the_walker_reaches_a_declined_say_in_all_three_shapes():
  """`declined_say` is a bare line, a ladder, or a list of `{when, say}` reasons —
  a reason's copy is heard exactly as a plain ladder's is, so it must be walked."""
  cfg = {"f": {
      "cancel": {"declined_say": "I will keep going."},
      "escalate": {"declined_say": [
          "Let me try once more.",
          {"when": "lambda f: True", "say": "I can still help with that."},
          {"when": "lambda f: False", "say": ["First line.", "Second line.", None]},
          {"when": "lambda f: False"},          # a reason with no copy at all
          None,                                  # a junk entry must not crash
      ]},
  }}
  assert _spoken(cfg) == {
      ("cancel.declined_say", "I will keep going."),
      ("escalate.declined_say[0]", "Let me try once more."),
      ("escalate.declined_say[1].say", "I can still help with that."),
      ("escalate.declined_say[2].say[0]", "First line."),
      ("escalate.declined_say[2].say[1]", "Second line."),
  }


def test_the_walker_skips_every_wrong_shape_instead_of_crashing():
  """Hand-authored YAML gets these wrong constantly; a linter that dies on a
  malformed field cannot report the malformed field."""
  cfg = {"f": {
      "no_input": "not a dict",
      "cancel": ["not a dict"],
      "all_done_say": 42,
      "filler_say": {"not": "a pool"},
      "readback_response": "not a list",
      "slots": [{
          "name": "s",
          "ask": 7,                              # not a string
          "response": "not a list",
          "validation": {"reprompts": "not a ladder", "errors": "not a map",
                         "on_exhaust": "not a dict"},
          "push_back": "not a dict",
          "filler_say": [None, 3, ""],
      }],
      "tasks": [{"name": "t", "then_say": "", "on_failure": "not a dict",
                 "awaits": "not a dict",
                 "then_response": [{"type": "text"}, {"text": ""}, "junk"]}],
  }}
  assert _spoken(cfg) == set()


def test_iter_spoken_parts_pairs_the_descriptor_only_with_part_shaped_text():
  cfg = {"f": {"slots": [{"name": "s", "ask": "Ready?",
                          "response": [{"type": "text", "text": "One moment.",
                                        "partial": True, "interruptable": False}]}]}}
  by_path = {i.json_path: part for i, part in _ctx(cfg).iter_spoken_parts("f")}
  assert by_path["slots[0].ask"] is None
  assert by_path["slots[0].response[0].text"]["partial"] is True


def test_the_walker_names_an_unnamed_node_positionally():
  cfg = {"f": {"slots": [{"ask": "Hello?"}], "tasks": [{"then_say": "Bye."}]}}
  assert {(i.node_kind, i.node) for i in _ctx(cfg).iter_spoken("f")} == {
      ("slot", "<slot 0>"), ("task", "<task 0>")}


# ==========================================================================
# context.py — build_context
# ==========================================================================

def test_build_context_assembles_a_real_app():
  import flows

  f = flows.Flow("acme_flow", root_agent="acme")
  f.add(flows.user_slot("member_id", ask="What is your member id?"))
  app = flows.App(root_flow=f, app_display_name="Widget_Agent")

  ctx = build_context(app)
  assert ctx.assembly_error is None
  assert ctx.app is app and ctx.host_cid is None
  assert "acme_flow" in ctx.configs
  assert "member_id" in ctx.slot_map("acme_flow")
  assert run_rules(ctx, select=["FLC101"]).findings[0].code == "FLC101"


# ==========================================================================
# runner.py
# ==========================================================================

def test_a_raising_rule_becomes_a_warning_finding_and_does_not_wedge_the_run():
  class Boom(_FakeRule):
    def check(self, ctx):
      raise RuntimeError("kaboom")

  boom = _mk_rule("FLV099", Category.VOICE, "info", "boom rule")
  boom.__class__.check = Boom.check
  reg = RuleRegistry()
  reg.register(boom)
  reg.register(_mk_rule("FLC101", Category.CONVERSATION, "info", "quiet"))
  reg.get("FLC101").__class__.check = lambda self, ctx: iter(())

  r = run_rules(_ctx({"f": {}}), registry=reg)
  assert r.ran_rules == ["FLC101", "FLV099"], "the run continued past the crash"
  assert len(r.findings) == 1
  f = r.findings[0]
  assert (f.code, f.severity) == ("FLV099", "warning")
  assert "internal: rule FLV099 raised RuntimeError: kaboom" in f.message
  assert "this is a linter bug" in f.message


def test_lint_app_is_the_one_call_that_assembles_and_runs():
  import flows

  from flows.lint import lint_app

  f = flows.Flow("acme_flow", root_agent="acme")
  f.add(flows.user_slot("member_id", ask="What is your member id?"))
  app = flows.App(root_flow=f, app_display_name="Widget_Agent")

  report = lint_app(app, select=["FLC101"])
  assert report.ran_rules == ["FLC101"]
  assert _codes(report) == ["FLC101"]
  assert lint_app(app, select=["FLC101"], ignore=["FLC101"]).ran_rules == []


def test_lint_app_on_a_broken_app_reports_rather_than_raises():
  from flows.lint import lint_app

  report = lint_app(SimpleNamespace())
  assert [f.code for f in report.findings] == ["FLX001"]
  assert report.findings[0].title == "app does not assemble"
  assert report.ran_rules == []


def test_select_and_ignore_accept_a_category_name():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "one — two"}]}}
  assert _codes(run_rules(_ctx(cfg), select=["VOICE"])) == ["FLV001"]
  assert "FLV001" not in run_rules(_ctx(cfg), ignore=["voice"]).ran_rules


# ==========================================================================
# rules/ — the branches a differently-shaped config reaches
# ==========================================================================

def test_flw003_respects_extra_tools_on_the_host_and_on_a_sub_agent():
  bodies = {"host_extra": "def host_extra(): ...",
            "agent_extra": "def agent_extra(): ...",
            "orphan": "def orphan(): ..."}
  ctx = _ctx({"f": {"slots": [], "tasks": []}}, bodies=bodies,
             available=list(bodies),
             host=SimpleNamespace(extra_tools=["host_extra"]),
             agents=[SimpleNamespace(extra_tools=["agent_extra"]),
                     SimpleNamespace(extra_tools=None)])
  r = run_rules(ctx, select=["FLW003"])
  assert [f.location.node for f in r.findings] == ["orphan"]


def test_flw003_never_flags_a_generated_dag_body():
  """`*_dag` / `dag_config` are emitted by the framework, not authored, so they are
  referenced by the emitter rather than by any task."""
  bodies = {"member_dag": "def member_dag(): ...",
            "dag_config": "def dag_config(): ..."}
  ctx = _ctx({"f": {"slots": [], "tasks": []}}, bodies=bodies, available=list(bodies))
  assert _codes(run_rules(ctx, select=["FLW003"])) == []
  # Near-miss: a body whose name merely CONTAINS dag is still an ordinary tool.
  ctx2 = _ctx({"f": {"slots": [], "tasks": []}},
              bodies={"dag_helper": "def dag_helper(): ..."},
              available=["dag_helper"])
  assert _codes(run_rules(ctx2, select=["FLW003"])) == ["FLW003"]


def _flr001_cfg(*, on_failure_extra=None, task_extra=None, open_slot="retry_flag"):
  of = {"max_retries": 2, "on_exhaust": {"open_slot": open_slot}}
  of.update(on_failure_extra or {})
  task = {"name": "verify", "tool": "verify_tool", "inputs": ["member_id"],
          "on_failure": of}
  task.update(task_extra or {})
  return {"f": {"slots": [
      {"name": "member_id", "source": "user", "ask": "id?", "setter": "set_member"},
      {"name": "retry_flag"}],
      "tasks": [task]}}


def test_flr001_treats_a_cleared_askable_slot_as_a_reachable_next_question():
  """`clear_slots` un-fills a slot the task consumed, so its question comes BACK —
  that is a live next question and the dead-end rule must stand down."""
  cfg = _flr001_cfg(on_failure_extra={"clear_slots": ["member_id"]})
  assert _codes(run_rules(_ctx(cfg), select=["FLR001"])) == []
  # The {error_code: [slots]} spelling of the same thing.
  cfg = _flr001_cfg(on_failure_extra={"clear_slots": {"not_found": ["member_id"]}})
  assert _codes(run_rules(_ctx(cfg), select=["FLR001"])) == []
  # Near-miss: clearing a slot that is not askable restores no question.
  cfg = _flr001_cfg(on_failure_extra={"clear_slots": {"not_found": ["retry_flag"],
                                                      "bad": "not a list"}})
  assert _codes(run_rules(_ctx(cfg), select=["FLR001"])) == ["FLR001"]


def test_flr001_skips_shapes_it_deliberately_leaves_to_the_validator():
  from flows.lint.rules.reachability import _task_input_names

  for cfg in (
      # on_failure / on_exhaust of the wrong shape
      {"f": {"slots": [], "tasks": [{"name": "t", "on_failure": "nope"}]}},
      {"f": {"slots": [], "tasks": [{"name": "t",
                                     "on_failure": {"on_exhaust": "nope"}}]}},
      # open_slot absent, empty, or not a string
      {"f": {"slots": [], "tasks": [{"name": "t",
                                     "on_failure": {"on_exhaust": {}}}]}},
      {"f": {"slots": [], "tasks": [{"name": "t", "on_failure": {
          "on_exhaust": {"open_slot": ""}}}]}},
      {"f": {"slots": [], "tasks": [{"name": "t", "on_failure": {
          "on_exhaust": {"open_slot": ["retry_flag"]}}}]}},
      # open_slot names a slot that does not exist -> the blessed validator's job
      _flr001_cfg(open_slot="no_such_slot"),
  ):
    assert _codes(run_rules(_ctx(cfg), select=["FLR001"])) == []

  # inputs as a {slot: param} map and a bare `requires` both count as consumed.
  assert _task_input_names({"inputs": {"a": "p"}, "requires": ["b", 3]}) == {"a", "b"}
  assert _task_input_names({"inputs": "junk"}) == set()


def test_flm001_ignores_a_falsy_condition_and_an_unnamed_slot():
  cfg = {"f": {"slots": [
      {"source": "user", "ask": "unnamed?"},                      # no name
      {"name": "intent", "source": "user", "ask": "which?"}],
      "tasks": [{"name": "a", "tool": "a", "condition": None},
                {"name": "b", "tool": "b", "condition": ""}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLM001"])) == []


def test_flm001_fires_on_a_single_gated_task_only_for_an_intent_slot():
  """One branch is normally not a fan-out — but an intent slot with >=2 option_cues
  IS multi-outcome even when only one of the outcomes has a task."""
  intent = {"name": "intent", "source": "user", "ask": "which?", "kind": "intent",
            "option_cues": {"billing": ["bill"], "tech": ["tech"]}}
  one_task = [{"name": "a", "tool": "a",
               "condition": "lambda f: f.get('intent') == 'billing'"}]
  fires = {"f": {"slots": [intent], "tasks": one_task}}
  r = run_rules(_ctx(fires), select=["FLM001"])
  assert _codes(r) == ["FLM001"]
  assert "branches into 2 outcomes" in r.findings[0].message

  plain = {"f": {"slots": [{"name": "intent", "source": "user", "ask": "which?"}],
                 "tasks": one_task}}
  assert _codes(run_rules(_ctx(plain), select=["FLM001"])) == []


def test_flc101_stands_down_on_a_router_config():
  """A synthesized host/router asks the caller nothing of its own, so a silence
  ladder there would be copy nobody hears."""
  cfg = {"host": {"router": True, "slots": [
      {"name": "intent", "source": "user", "ask": "How can I help?"}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLC101"])) == []
  assert _codes(run_rules(_ctx(cfg, host_cid="host"), select=["FLC101"])) == []
  # Near-miss: the same slots on an ordinary flow DO want a ladder.
  plain = {"member": {"slots": [
      {"name": "intent", "source": "user", "ask": "How can I help?"}]}}
  assert _codes(run_rules(_ctx(plain), select=["FLC101"])) == ["FLC101"]


def test_flc121_ignores_a_task_with_no_awaits_block():
  cfg = {"f": {"tasks": [{"name": "sync", "tool": "t"},
                         {"name": "bad_shape", "tool": "t", "awaits": "nope"}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLC121"])) == []


def test_flc130_finds_a_transfer_in_a_slot_response_and_in_a_cancel_block():
  cfg = {"f": {
      "slots": [{"name": "s", "source": "user", "ask": "?", "response": [
          {"type": "text", "text": "Okay."},           # a non-transfer part
          {"type": "transfer"}]}],
      "cancel": {"response": [{"type": "transfer", "disclaimer": "Transferring."}]},
  }}
  r = run_rules(_ctx(cfg), select=["FLC130"])
  assert {f.location.json_path for f in r.findings} == {
      "slots[0].response[1]", "cancel.response[0]"}
  by_path = {f.location.json_path: f for f in r.findings}
  assert "missing disclaimer, context" in by_path["slots[0].response[1]"].message
  assert "missing context" in by_path["cancel.response[0]"].message
  assert by_path["cancel.response[0]"].anchor.kind == "field"


def test_flv004_reads_the_first_rung_of_an_ask_ladder():
  """`ask` may be a ladder; testing the list itself would make the rule silent on
  the very case its docstring names."""
  ladder = ["One moment. What is your member id?", "Your member id, please?"]
  cfg = {"f": {"slots": [{"name": "member_id", "source": "user", "ask": ladder,
                          "verbatim": True}]}}
  r = run_rules(_ctx(cfg, automatic_fillers=True), select=["FLV004"])
  assert _codes(r) == ["FLV004"]
  assert r.findings[0].location.json_path == "slots[0].ask"
  # Near-miss: an empty ladder has no first rung to hoist.
  empty = {"f": {"slots": [{"name": "member_id", "source": "user", "ask": [],
                            "verbatim": True}]}}
  assert _codes(run_rules(_ctx(empty, automatic_fillers=True),
                          select=["FLV004"])) == []


def test_flx001_is_silent_when_there_is_nothing_assembled_to_validate():
  assert run_rules(_ctx({}), select=["FLX001"]).findings == []


def test_flx001_also_runs_the_cross_config_validator():
  """Two configs means the cross-config validator runs too; a cross diagnostic is
  attached to no single config, so its location carries a null config_id."""
  slot = {"name": "x", "source": "user", "ask": "?", "hint": "h", "setter": "set_x"}
  cfg = {"a": {"slots": [dict(slot)], "tasks": []},
         "b": {"slots": [dict(slot)], "tasks": [
             {"name": "t", "tool": "missing_tool", "inputs": ["x"]}]}}
  ctx = _ctx(cfg, bodies={"set_x": "def set_x(): ..."}, available=["set_x"])
  r = run_rules(ctx, select=["FLX001"])
  cids = {f.location.config_id for f in r.findings}
  assert None in cids, "a cross-config diagnostic belongs to no one config"
  assert "b" in cids
  assert any(f.severity == "error" and f.location.config_id == "b"
             for f in r.findings)
  # A single config never reaches the cross pass, so nothing is unattributed.
  solo = _ctx({"a": cfg["a"]}, bodies={"set_x": "def set_x(): ..."},
              available=["set_x"])
  assert all(f.location.config_id == "a"
             for f in run_rules(solo, select=["FLX001"]).findings)


def test_flx001_falls_back_when_the_framework_root_cannot_be_imported(monkeypatch):
  """The adapter must still run against the validator's own default root."""
  import flows.authoring.build as build_mod

  monkeypatch.delattr(build_mod, "FRAMEWORK_ROOT")
  cfg = {"f": {"slots": [{"name": "x", "source": "user", "ask": "?", "hint": "h",
                          "setter": "set_x"}], "tasks": []}}
  r = run_rules(_ctx(cfg, bodies={"set_x": "def set_x(): ..."}, available=["set_x"]),
                select=["FLX001"])
  assert not any(f.severity == "error" for f in r.findings)
