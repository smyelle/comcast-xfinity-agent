"""flows lint — engine + rule firing/near-miss guards.

Each rule test asserts both that the rule FIRES on the defect and that it does NOT
fire on a near-miss (the false-positive guard; see DESIGN.md principle 8). Rules
are exercised over a hand-built `LintContext` so a test needs no full App.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_lint.py
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from flows.lint import LintContext, run_rules
from flows.lint.models import SCHEMA_VERSION


def _ctx(configs, *, bodies=None, available=None, **appkw) -> LintContext:
  app = SimpleNamespace(
      lint_ignore=appkw.get("lint_ignore", []),
      extra_agent_tools=appkw.get("extra_agent_tools", []),
      host=appkw.get("host"),
      agents=appkw.get("agents", []),
      automatic_fillers=appkw.get("automatic_fillers", False),
  )
  return LintContext(app=app, configs=configs, bodies=bodies or {},
                     available=available or [])


def _codes(report):
  return [f.code for f in report.findings if not f.suppressed_by]


def _live(report):
  return [f for f in report.findings if not f.suppressed_by]


# --- FLR001: on_exhaust open_slot dead-end (#596 case 2) --------------------

def _flr001_config(*, say=False, open_askable=False, extra_askable=False):
  slots = [
      {"name": "member_id", "source": "user", "ask": "id?", "setter": "set_member"},
      {"name": "retry_flag"},
  ]
  if open_askable:
    slots[1] = {"name": "retry_flag", "source": "user", "ask": "retry?"}
  if extra_askable:
    slots.append({"name": "other", "source": "user", "ask": "other?", "setter": "s2"})
  exhaust = {"open_slot": "retry_flag"}
  if say:
    exhaust["say"] = "Sorry, we could not verify you."
  return {"f": {"slots": slots, "tasks": [{
      "name": "verify", "tool": "verify_tool", "inputs": ["member_id"],
      "on_failure": {"max_retries": 2, "on_exhaust": exhaust}}]}}


def test_flr001_fires_on_terminal_dead_end():
  r = run_rules(_ctx(_flr001_config()), select=["FLR001"])
  assert _codes(r) == ["FLR001"]
  assert r.findings[0].severity == "error"


def test_flr001_silent_when_say_present():
  assert _codes(run_rules(_ctx(_flr001_config(say=True)), select=["FLR001"])) == []


def test_flr001_silent_when_open_slot_is_askable():
  assert _codes(run_rules(_ctx(_flr001_config(open_askable=True)), select=["FLR001"])) == []


def test_flr001_silent_when_another_question_remains():
  assert _codes(run_rules(_ctx(_flr001_config(extra_askable=True)), select=["FLR001"])) == []


# --- FLW003: dead / unwired tool (#596 case 1) -----------------------------

def _flw003_ctx(**appkw):
  cfg = {"f": {"slots": [
      {"name": "member_id", "source": "user", "ask": "id?", "setter": "set_member"}],
      "tasks": []}}
  bodies = {"set_member": "def set_member(): ...", "orphan_tool": "def orphan_tool(): ..."}
  return _ctx(cfg, bodies=bodies, available=list(bodies), **appkw)


def test_flw003_fires_on_unreferenced_body():
  r = run_rules(_flw003_ctx(), select=["FLW003"])
  assert _codes(r) == ["FLW003"]
  assert "orphan_tool" in r.findings[0].message
  assert r.findings[0].severity == "needs_review"


def test_flw003_silent_for_declared_extra_tool():
  r = run_rules(_flw003_ctx(extra_agent_tools=["orphan_tool"]), select=["FLW003"])
  assert _codes(r) == []


def test_flw003_silent_for_referenced_setter():
  # set_member is referenced (a slot setter) so it must never be flagged.
  r = run_rules(_flw003_ctx(), select=["FLW003"])
  assert all("set_member" not in f.message for f in r.findings)


# --- FLM001: multi-outcome branch, no proceed directive (#596 case 4) -------

def _flm001_config(*, steer=False):
  # Real flows conditions are lambdas that read a slot via f.get('name').
  t_a = {"name": "do_a", "tool": "a_tool",
         "condition": "lambda f: f.get('intent') == 'a'"}
  t_b = {"name": "do_b", "tool": "b_tool",
         "condition": "lambda f: f.get('intent') == 'b'"}
  if steer:
    t_a["then_directive"] = "Ask the caller for their reference number."
  return {"f": {"slots": [
      {"name": "intent", "source": "user", "ask": "what do you need?",
       "kind": "intent", "option_cues": {"a": ["a"], "b": ["b"]}}],
      "tasks": [t_a, t_b]}}


def test_flm001_fires_on_unsteered_branch():
  r = run_rules(_ctx(_flm001_config()), select=["FLM001"])
  assert _codes(r) == ["FLM001"]
  assert r.findings[0].severity == "warning"


def test_flm001_silent_when_a_branch_steers():
  assert _codes(run_rules(_ctx(_flm001_config(steer=True)), select=["FLM001"])) == []


# --- FLV001: dash in spoken copy -------------------------------------------

def test_flv001_fires_on_em_dash():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "one moment — okay?"}]}}
  r = run_rules(_ctx(cfg), select=["FLV001"])
  assert _codes(r) == ["FLV001"]
  assert r.findings[0].severity == "needs_review"


def test_flv001_silent_on_clean_copy():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "one moment, okay?"}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLV001"])) == []


# --- FLV002 / FLV003: audio tags in spoken copy ----------------------------

def test_flv002_fires_on_an_audio_tag_in_an_ask():
  cfg = {"f": {"slots": [{"name": "s", "source": "user",
                          "ask": "[whispers] what is your order number?"}]}}
  r = run_rules(_ctx(cfg), select=["FLV002"])
  assert _codes(r) == ["FLV002"]
  assert r.findings[0].severity == "warning"


def test_flv002_silent_without_a_tag():
  cfg = {"f": {"slots": [{"name": "s", "source": "user",
                          "ask": "what is your order number?"}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLV002"])) == []


def test_flv002_does_not_fire_on_a_template_or_a_url():
  """`{order_id}` is interpolated before TTS and a URL is not read as written, so
  neither is an audio tag -- this is the false-positive guard the bracket regex needs."""
  cfg = {"f": {"slots": [{"name": "s", "source": "user",
                          "ask": "order {order_id} at https://x.test/a-b is ready"}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLV002"])) == []


def test_flv002_leaves_the_partial_case_to_flv003():
  """One finding per defect: a tagged PARTIAL part is an error, not a warning, and
  reporting both would train authors to ignore the pair."""
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "hi",
                          "response": [{"type": "text", "text": "[calm] one moment",
                                        "partial": True}]}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLV002"])) == []


def test_flv003_fires_on_a_tag_in_a_partial_part():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "hi",
                          "response": [{"type": "text", "text": "[calm] one moment",
                                        "partial": True}]}]}}
  r = run_rules(_ctx(cfg), select=["FLV003"])
  assert _codes(r) == ["FLV003"]
  assert r.findings[0].severity == "error"


def test_flv003_silent_on_an_untagged_partial_part():
  """Untagged partial parts are fine on both models -- that is the whole point of the
  latency filler, so flagging them would make the rule useless."""
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "hi",
                          "response": [{"type": "text", "text": "one moment",
                                        "partial": True}]}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLV003"])) == []


def test_flv003_silent_on_a_tag_in_a_non_partial_part():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "hi",
                          "response": [{"type": "text", "text": "[calm] one moment"}]}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLV003"])) == []


# --- the spoken-text walker -------------------------------------------------

def test_iter_spoken_keeps_its_four_field_shape():
  """`LintContext` is exported, so out-of-tree rules unpack this tuple. Widening it
  for partial-awareness would break them; `iter_spoken_parts` carries the descriptor."""
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "hello"}]}}
  ctx = _ctx(cfg)
  kind, node, path, text = next(iter(ctx.iter_spoken("f")))
  assert (kind, node, path, text) == ("slot", "s", "slots[0].ask", "hello")


def test_the_walker_reaches_a_filler_pool_and_the_flow_default():
  cfg = {"f": {"filler_say": ["flow line", None],
               "slots": [{"name": "s", "source": "user", "ask": "hi",
                          "filler_say": ["slot line", None]}]}}
  spoken = [i.text for i in _ctx(cfg).iter_spoken("f")]
  assert "slot line" in spoken and "flow line" in spoken
  assert None not in spoken, "a None pool entry is silence, not text to lint"


def test_the_walker_reaches_fields_it_used_to_miss():
  cfg = {"f": {"all_done_say": "all done",
               "slots": [{"name": "s", "source": "user", "ask": "hi",
                          "push_back": {"say": "pushed back"},
                          "validation": {"errors": {"bad_len": "that is too short"}}}],
               "cancel": {"say": "cancelled", "confirm_say": "sure?"}}}
  spoken = [i.text for i in _ctx(cfg).iter_spoken("f")]
  for text in ("all done", "pushed back", "that is too short", "sure?"):
    assert text in spoken, f"{text!r} is spoken to a caller but not walked"


# --- FLC101: no silence ladder ---------------------------------------------

def test_flc101_fires_without_no_input():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "id?"}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLC101"])) == ["FLC101"]


def test_flc101_silent_with_ladder():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "id?"}],
               "no_input": {"reprompts": ["Are you there?"]}}}
  assert _codes(run_rules(_ctx(cfg), select=["FLC101"])) == []


# --- runner: suppression, selection, determinism, schema -------------------

def test_app_level_suppression_keeps_finding_but_marks_it():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "one moment — okay?"}]}}
  r = run_rules(_ctx(cfg, lint_ignore=["FLV001: intentional"]), select=["FLV001"])
  assert _codes(r) == []                       # not live
  assert len(r.findings) == 1                  # but kept
  assert r.findings[0].suppressed_by == "app.lint_ignore"


def test_ignore_skips_rule():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "one moment — okay?"}]}}
  r = run_rules(_ctx(cfg), ignore=["FLV001"])
  assert "FLV001" not in r.ran_rules


def test_determinism():
  cfg = {"f": {"slots": [
      {"name": "s", "source": "user", "ask": "one moment — okay?"}]}}
  a = run_rules(_ctx(cfg), select=["FLV001", "FLC101"])
  b = run_rules(_ctx(cfg), select=["FLV001", "FLC101"])
  assert [f.code for f in a.findings] == [f.code for f in b.findings]


def test_report_is_versioned_and_json_serializable():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "id?"}]}}
  r = run_rules(_ctx(cfg), select=["FLC101"])
  assert r.schema_version == SCHEMA_VERSION
  blob = json.dumps(r.model_dump(mode="json"))
  assert "FLC101" in blob


def test_assembly_error_is_one_blocking_finding():
  ctx = LintContext(app=SimpleNamespace(), configs={}, bodies={}, available=[],
                    assembly_error="bad wiring")
  r = run_rules(ctx)
  assert len(r.findings) == 1
  assert r.findings[0].severity == "error"
  assert not r.ok()


# --- FLX001: blessed DAG validator adapter ---------------------------------

def test_flx001_fires_on_validator_error():
  cfg = {"f": {"slots": [{"name": "x", "source": "user", "ask": "?", "hint": "h",
                          "setter": "set_x"}],
               "tasks": [{"name": "t", "tool": "missing_tool", "inputs": ["x"]}]}}
  r = run_rules(_ctx(cfg, bodies={"set_x": "def set_x(): ..."}, available=["set_x"]),
                select=["FLX001"])
  assert "FLX001" in _codes(r)
  assert any(f.severity == "error" for f in r.findings)


def test_flx001_quiet_on_clean_config():
  cfg = {"f": {"slots": [{"name": "x", "source": "user", "ask": "?", "hint": "h",
                          "setter": "set_x"}]}}
  r = run_rules(_ctx(cfg, bodies={"set_x": "def set_x(): ..."}, available=["set_x"]),
                select=["FLX001"])
  assert not any(f.severity == "error" for f in _live(r))


# --- FLC121 / FLC130 -------------------------------------------------------

def test_flc121_fires_on_silent_async_wait():
  cfg = {"f": {"slots": [], "tasks": [{
      "name": "chk", "tool": "poll", "awaits": {"max_turns": 5}}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLC121"])) == ["FLC121"]


def test_flc121_silent_when_wait_speaks():
  cfg = {"f": {"slots": [], "tasks": [{
      "name": "chk", "tool": "poll", "awaits": {"max_turns": 5, "say": "One moment."}}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLC121"])) == []


def test_flc130_fires_on_cold_transfer():
  cfg = {"f": {"slots": [], "tasks": [{
      "name": "t", "tool": "x", "terminal": True,
      "then_response": [{"type": "transfer"}]}]}}
  r = run_rules(_ctx(cfg), select=["FLC130"])
  assert _codes(r) == ["FLC130"]


def test_flc130_silent_with_disclaimer_and_context():
  cfg = {"f": {"slots": [], "tasks": [{
      "name": "t", "tool": "x", "terminal": True,
      "then_response": [{"type": "transfer", "disclaimer": "Transferring you now.",
                         "context": "member verified"}]}]}}
  assert _codes(run_rules(_ctx(cfg), select=["FLC130"])) == []


# --- owl review regressions ------------------------------------------------

def test_flm001_ignores_value_literal_equal_to_a_slot_name():
  # A slot named like a VALUE ('active') must not be seen as a branch just because
  # conditions compare to 'active'. The branch matcher keys on the reference form
  # f.get('coverage'), so only 'coverage' is flagged, never 'active'.
  cfg = {"f": {"slots": [
      {"name": "active", "source": "user", "ask": "still active?"},
      {"name": "coverage", "source": "user", "ask": "coverage?"}],
      "tasks": [
          {"name": "do_a", "tool": "a",
           "condition": "lambda f: f.get('coverage') == 'active'"},
          {"name": "do_i", "tool": "i",
           "condition": "lambda f: f.get('coverage') == 'inactive'"}]}}
  r = run_rules(_ctx(cfg), select=["FLM001"])
  nodes = {f.location.node for f in r.findings}
  assert "active" not in nodes       # a value literal, not a branch slot
  assert "coverage" in nodes         # the real branch (2 tasks read it, no directive)


def test_flv001_ignores_hyphen_inside_template_but_flags_real_one():
  templ = {"f": {"slots": [{"name": "s", "source": "user", "ask": "code {order-id} ok"}]}}
  assert _codes(run_rules(_ctx(templ), select=["FLV001"])) == []
  real = {"f": {"slots": [{"name": "s", "source": "user", "ask": "read the door-tag"}]}}
  assert _codes(run_rules(_ctx(real), select=["FLV001"])) == ["FLV001"]


def test_component_output_is_fillable():
  cfg = {"f": {"slots": [
      {"name": "x", "source": "user", "ask": "?", "setter": "sx"}, {"name": "y"}],
      "tasks": [{"name": "c", "component": "child", "inputs": ["x"],
                 "outputs": {"res": "y"}}]}}
  assert "y" in _ctx(cfg).fillable_slots("f")


def test_anchor_fields_are_relative_to_the_node():
  fv = run_rules(_ctx({"f": {"slots": [
      {"name": "s", "source": "user", "ask": "a — b"}]}}), select=["FLV001"])
  assert fv.findings[0].anchor.field == "ask"
  fc = run_rules(_ctx({"f": {"slots": [], "tasks": [{
      "name": "t", "tool": "x", "terminal": True,
      "then_response": [{"type": "transfer"}]}]}}), select=["FLC130"])
  assert fc.findings[0].anchor.field == "then_response[0]"


def test_node_level_lint_ignore_suppresses():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "a — b",
                          "_lint_ignore": ["FLV001"]}]}}
  r = run_rules(_ctx(cfg), select=["FLV001"])
  assert _codes(r) == []
  assert r.findings[0].suppressed_by == "s._lint_ignore"


def test_lint_ignore_accepts_bare_string_and_is_case_insensitive():
  cfg = {"f": {"slots": [{"name": "s", "source": "user", "ask": "a — b"}]}}
  assert _codes(run_rules(_ctx(cfg, lint_ignore="flv001"), select=["FLV001"])) == []


def test_strict_promotes_warnings_to_blocking():
  r = run_rules(_ctx(_flm001_config()), select=["FLM001"])   # FLM001 is a warning
  assert r.ok(strict=False) is True
  assert r.ok(strict=True) is False
  assert [f.code for f in r.blocking(strict=True)] == ["FLM001"]


def _flv004_config(**task_extra):
  return {"d": {"tasks": [{"name": "lookup", "tool": "check",
                           "then_say": "Thanks for holding. Balance is {b}.",
                           **task_extra}]}}


def test_flv004_flags_an_opener_a_structural_rule_blocked():
  r = run_rules(_ctx(_flv004_config(verbatim=True), automatic_fillers=True),
                select=["FLV004"])
  assert _codes(r) == ["FLV004"]
  assert "verbatim=True" in r.findings[0].message


def test_flv004_anchors_with_an_index_like_every_other_rule():
  """`tasks[0].then_say`, not `tasks.lookup.then_say` — Slot Studio resolves the
  index form, and model_reliance / conversation / reachability all emit it."""
  r = run_rules(_ctx(_flv004_config(verbatim=True), automatic_fillers=True),
                select=["FLV004"])
  assert r.findings[0].location.json_path == "tasks[0].then_say"


def test_flv004_is_silent_when_the_app_never_opted_in():
  """Near-miss: the same blocked node, on an app that does not use the feature."""
  assert _codes(run_rules(_ctx(_flv004_config(verbatim=True)),
                          select=["FLV004"])) == []


def test_flv004_is_silent_when_nothing_blocked_the_hoist():
  """Near-miss: no structural blocker means the pass already moved it, or the
  author opted out deliberately — neither deserves a finding."""
  assert _codes(run_rules(_ctx(_flv004_config(), automatic_fillers=True),
                          select=["FLV004"])) == []


def test_flv004_is_silent_when_the_node_already_covers_its_wait():
  """Near-miss: `filler_say` present is the CORRECT state, not a defect."""
  cfg = _flv004_config(filler_say="One moment.")
  assert _codes(run_rules(_ctx(cfg, automatic_fillers=True), select=["FLV004"])) == []


def test_flv004_is_silent_when_the_opener_carries_substance():
  """Near-miss: nothing hoistable, so nothing was blocked."""
  cfg = {"d": {"tasks": [{"name": "lookup", "tool": "check", "verbatim": True,
                          "then_say": "Your card was declined. Try another."}]}}
  assert _codes(run_rules(_ctx(cfg, automatic_fillers=True), select=["FLV004"])) == []


def test_build_context_turns_a_broken_app_into_a_finding():
  from flows.lint.context import build_context
  ctx = build_context(SimpleNamespace())   # missing .is_multi_agent etc.
  assert ctx.assembly_error is not None
  assert not run_rules(ctx).ok()
