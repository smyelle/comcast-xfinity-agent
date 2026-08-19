"""The config validator REPORTS malformed authoring input; it never raises on it.

The validator is the design-time linter: an author (or a machine emitter) hands it a
config that is wrong in some way — that is the whole reason to run it. When a check
crashes on the bad value instead of reporting it, the author gets a stack trace out of
the tooling, and every OTHER defect in that config goes unreported with it. So the
contract asserted here is blunt:

    validate_dag_config(<anything>) returns {valid, errors, warnings} — always.

Each test below pins one shape that used to raise out of the tool entry point, grouped
by the way it got there:

  * a value the contradiction/tautology passes HASH (an `eq: ["a"]` from YAML, where a
    list is a legal literal but is unhashable in Python);
  * a combinator the spec-validation function correctly REPORTS as malformed while the
    extraction walkers iterated it anyway (`{"all": 5}`);
  * a malformed `input_data` wrapper at the entry point itself;
  * a guard applied by the check that OWNS a field and then ignored by a later reader
    of the same field (`on_failure: 5`, `outputs` as a list, a non-string name);
  * a bare string iterated per CHARACTER (`requires: "res"` -> unknown 'r'/'e'/'s').

Two properties are asserted throughout, and they matter in this order:

  1. no crash, and
  2. no false positive — a config that validated clean before must still validate
     clean. A linter that blocks a valid build is worse than one that misses a defect,
     so the hardening only ever ADDS a report about a value that is provably not the
     shape the engine can read.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_validator_hardening.py
"""
from __future__ import annotations

import copy

from flows.engine import loader

vdc = loader.load_validator()


def _run(input_data):
    """The tool entry point — the surface whose contract is 'never raises'."""
    out = vdc.validate_dag_config(input_data)
    assert isinstance(out, dict), out
    assert {"valid", "errors", "warnings"} <= set(out), out
    assert isinstance(out["errors"], list) and isinstance(out["warnings"], list)
    return out


def _errors(cfg):
    return _run({"raw_config": cfg})["errors"]


def _valid_config():
    """A config that validates CLEAN — the false-positive baseline."""
    return {
        "slots": [
            {"name": "intent", "source": "user", "setter": "set_intent",
             "ask": "How can I help?", "kind": "intent",
             "option_cues": {"billing": ["bill"], "tech": ["outage"]},
             "validation_rules": [{"kind": "enum", "detail": "billing|tech"}]},
            {"name": "account", "source": "user", "setter": "set_account",
             "ask": "What is your account number?", "requires": ["intent"],
             "condition": {"slot": "intent", "eq": "billing"}},
            {"name": "balance", "source": "task:Lookup"},
        ],
        "tasks": [
            {"name": "Lookup", "tool": "lookup", "inputs": {"account": "acct"},
             "outputs": {"balance": "balance"}, "requires": ["account"],
             "on_failure": {"max_retries": 2, "clear_slots": ["account"]}},
            {"name": "Finish", "tool": "finish", "requires": ["balance"],
             "terminal": True, "on_complete": {"clear_slots": ["account"]}},
        ],
        "gate_slot": "intent",
        "bootstrap": {"tool": "start_flow", "slot": "intent"},
    }


def test_the_baseline_config_is_clean():
    """Every other test perturbs THIS config, so its cleanliness is the control."""
    out = _run({"raw_config": _valid_config()})
    assert out["valid"], out["errors"]
    assert out["errors"] == []


# ── unhashable condition operands ─────────────────────────────────────────

def test_an_eq_list_under_all_is_reported_not_hashed():
    """`eq: ["a"]` is spec-legal until typed: the contradiction pass then hashed it.

    An `all` grouping is the shape that reaches the hashing — a bare leaf or an `any`
    took a different path — and it hashed even with an EMPTY neq set, so ONE bad leaf
    was enough.
    """
    cfg = _valid_config()
    cfg["slots"][1]["condition"] = {
        "all": [{"slot": "intent", "eq": ["billing"]},
                {"slot": "account", "filled": True}]}
    errs = _errors(cfg)
    assert any("'eq' must be a scalar" in e and "list" in e for e in errs), errs


def test_an_unhashable_neq_under_any_is_reported():
    """The tautology pass hashes `any` operands the same way the `all` pass does."""
    cfg = _valid_config()
    cfg["slots"][1]["condition"] = {
        "any": [{"slot": "intent", "neq": {"billing": 1}},
                {"slot": "account", "filled": True}]}
    assert any("'neq' must be a scalar" in e for e in _errors(cfg))


def test_an_unhashable_in_member_is_reported():
    """in/not_in are set-differenced against each other, so their MEMBERS are hashed."""
    cfg = _valid_config()
    cfg["slots"][1]["condition"] = {
        "all": [{"slot": "intent", "in": [["billing"], "tech"]},
                {"slot": "intent", "not_in": ["tech"]}]}
    assert any("'in' member must be a scalar" in e for e in _errors(cfg))


def test_a_scalar_eq_still_contradicts_a_sibling_neq():
    """The screening must not cost the detection it was screening FOR."""
    cfg = _valid_config()
    cfg["slots"][1]["condition"] = {
        "all": [{"slot": "intent", "eq": "billing"},
                {"slot": "intent", "neq": "billing"}]}
    assert any("Contradictory" in e and "eq='billing'" in e
               for e in _errors(cfg))


# ── non-iterable combinators ──────────────────────────────────────────────

def test_a_non_iterable_all_is_reported_by_every_walker():
    """The spec check already REPORTED `{"all": 5}`; the walkers iterated it anyway."""
    cfg = _valid_config()
    cfg["slots"][1]["condition"] = {"all": 5}
    errs = _errors(cfg)
    assert any("'all' must be a list" in e for e in errs), errs


def test_a_non_iterable_any_is_reported():
    cfg = _valid_config()
    cfg["slots"][1]["condition"] = {"any": "billing"}
    assert any("'any' must be a list" in e for e in _errors(cfg))


def test_a_nested_bad_combinator_under_a_good_one_is_reported():
    cfg = _valid_config()
    cfg["slots"][1]["condition"] = {
        "all": [{"any": 7}, {"slot": "intent", "filled": True}]}
    assert any("'any' must be a list" in e for e in _errors(cfg))


# ── the entry point itself ────────────────────────────────────────────────

def test_a_non_dict_input_data_is_reported():
    for bad in (None, [], "x", 5):
        out = _run(bad)
        assert not out["valid"]
        assert any("input_data must be a dict" in e for e in out["errors"])


def test_a_non_dict_raw_config_is_reported():
    for bad in (None, [], "x"):
        out = _run({"raw_config": bad})
        assert not out["valid"]
        assert any("Config must be a dict" in e for e in out["errors"]), out


def test_a_list_where_all_configs_expects_a_map_is_reported():
    out = _run({"all_configs": ["flow_a"]})
    assert not out["valid"]
    assert any("'all_configs' must be a dict" in e for e in out["errors"])


def test_a_malformed_child_config_is_reported_under_its_own_id():
    out = _run({"all_configs": {"flow_a": None, "flow_b": _valid_config()}})
    assert not out["valid"]
    assert any("[flow_a]" in e and "must be a dict" in e for e in out["errors"])
    # The healthy sibling is still validated — one bad child does not blind the run.
    assert out["per_config"]["flow_b"]["valid"], out["per_config"]["flow_b"]


def test_a_non_list_available_tools_is_reported():
    out = _run({"raw_config": _valid_config(), "available_tools": 5})
    assert any("'available_tools' must be a list" in e for e in out["errors"])


# ── a guard one check applies and a later one ignores ─────────────────────

def test_a_terminal_task_with_a_string_on_complete_is_reported_once():
    """The on-complete check guarded this and reported it cleanly; the loop-risk
    check then read .get() on the very same value. The same value on a NON-terminal
    task reported cleanly, which is what made it look like a terminal-only bug."""
    for terminal in (True, False):
        cfg = _valid_config()
        cfg["tasks"][1]["terminal"] = terminal
        cfg["tasks"][1]["on_complete"] = "x"
        errs = _errors(cfg)
        assert any("on_complete must be a dict" in e for e in errs), errs


def test_a_non_dict_on_failure_is_reported_not_re_read():
    cfg = _valid_config()
    cfg["tasks"][0]["on_failure"] = 5
    assert any("on_failure must be a dict" in e for e in _errors(cfg))


def test_a_list_shaped_outputs_is_reported_once():
    cfg = _valid_config()
    cfg["tasks"][0]["outputs"] = ["balance"]
    errs = _errors(cfg)
    assert any("outputs must be a dict" in e for e in errs), errs


def test_a_non_string_output_target_is_reported():
    cfg = _valid_config()
    cfg["tasks"][0]["outputs"] = {"balance": ["balance", 5]}
    assert any("must name a slot" in e for e in _errors(cfg))


def test_a_non_string_source_is_reported():
    """`source: 5` crashed on .startswith(); every reader now takes only the
    names it can act on, and the check that owns the field reports the type."""
    for bad in (5, [5]):
        cfg = _valid_config()
        cfg["slots"][2]["source"] = bad
        assert any("source must be a string" in e for e in _errors(cfg))


def test_an_intent_slot_with_malformed_validation_rules_is_reported():
    for bad in (["enum"], "enum"):
        cfg = _valid_config()
        cfg["slots"][0]["validation_rules"] = bad
        assert any("validation_rules" in e and "must be a" in e
                   for e in _errors(cfg))


def test_a_string_validation_block_is_reported():
    cfg = _valid_config()
    cfg["slots"][1]["validation"] = "strict"
    assert any("validation must be a dict" in e for e in _errors(cfg))


def test_a_non_list_shared_slots_does_not_raise():
    """`shared_slots: 5` crashed the orphaned-slot check, which iterated the value
    the shared_slots check had already declined to read. It stays UNREPORTED on
    purpose: the check that owns the field has always ignored a non-list quietly
    (the engine derives sharing from per-slot `shared: true` and never reads this
    key), and newly failing a config that passed before is the worse outcome."""
    for bad in (5, "shared_id", {"shared_id": True}):
        cfg = _valid_config()
        cfg["shared_slots"] = bad
        out = _run({"raw_config": cfg})
        assert out["valid"], out["errors"]


def test_a_non_list_slots_or_tasks_is_reported():
    for field in ("slots", "tasks"):
        cfg = _valid_config()
        cfg[field] = 5
        assert any(f"Config '{field}' must be a list" in e
                   for e in _errors(cfg))


def test_a_slot_or_task_that_is_not_a_dict_is_reported():
    cfg = _valid_config()
    cfg["slots"].append("account")
    cfg["tasks"].append(None)
    errs = _errors(cfg)
    assert any("Slot at index 3 must be a dict" in e for e in errs), errs
    assert any("Task at index 2 must be a dict" in e for e in errs), errs


def test_a_non_string_name_is_reported_rather_than_keyed_on():
    cfg = _valid_config()
    cfg["slots"][2]["name"] = ["balance"]
    cfg["tasks"][0]["name"] = 5
    errs = _errors(cfg)
    assert any("Slot at index 2 has a non-string 'name'" in e for e in errs)
    assert any("Task at index 0 has a non-string 'name'" in e for e in errs)


def test_a_non_string_identifier_field_is_reported():
    cfg = _valid_config()
    cfg["slots"][0]["setter"] = ["set_intent"]
    cfg["tasks"][0]["tool"] = {"name": "lookup"}
    errs = _errors(cfg)
    assert any("field 'setter' must be a name string" in e for e in errs), errs
    assert any("field 'tool' must be a name string" in e for e in errs), errs


def test_a_non_string_ask_is_reported():
    """The ask is spoken verbatim; a non-string one crashed the ask-floor reader."""
    cfg = _valid_config()
    cfg["slots"][1]["ask"] = 5
    assert any("ask must be a string" in e for e in _errors(cfg))
    cfg["slots"][1]["ask"] = ["Account?", 5]
    assert any("ask must be a string" in e for e in _errors(cfg))


def test_an_ask_ladder_of_strings_is_still_clean():
    cfg = _valid_config()
    cfg["slots"][1]["ask"] = ["Account number?", "I still need it."]
    out = _run({"raw_config": cfg})
    assert out["valid"], out["errors"]


def test_a_nan_await_bound_is_reported_not_raised():
    cfg = _valid_config()
    cfg["tasks"][0]["awaits"] = {"max_turns": float("nan")}
    assert any("max_turns" in e for e in _errors(cfg))


# ── a bare string is a value, not a sequence of characters ────────────────

def test_a_bare_string_requires_is_one_error_naming_the_field():
    """`requires: "res"` used to be iterated per character: three "unknown slot"
    errors about 'r', 'e' and 's', and nothing at all about the real mistake."""
    cfg = _valid_config()
    cfg["tasks"][1]["requires"] = "res"
    errs = _errors(cfg)
    assert any("Task 'Finish' requires must be a list of slot names" in e
               for e in errs), errs
    assert not [e for e in errs if "'r'" in e or "'e'" in e or "'s'" in e]


def test_a_bare_string_slot_requires_is_one_error_naming_the_field():
    cfg = _valid_config()
    cfg["slots"][1]["requires"] = "res"
    errs = _errors(cfg)
    assert any("Slot 'account' requires must be a list of slot names" in e
               for e in errs), errs
    assert not [e for e in errs if "requires unknown 'r'" in e]


def test_a_non_string_requires_entry_is_reported():
    cfg = _valid_config()
    cfg["tasks"][1]["requires"] = ["balance", 5]
    assert any("requires entry must be a slot name string" in e
               for e in _errors(cfg))


# ── documented shapes and interpolation ───────────────────────────────────

def test_a_values_dict_literal_is_read_like_the_subscript_form():
    """The extractor documents `values = {"field": x}`; a setter that builds its
    payload in ONE expression must not read as writing no fields at all."""
    literal = vdc._extract_values_dict_keys(
        "def s(a, b):\n  values = {'first': a, 'second': b}\n  return values")
    subscripts = vdc._extract_values_dict_keys(
        "def s(a, b):\n  values = {}\n  values['first'] = a\n"
        "  values['second'] = b\n  return values")
    assert literal == subscripts == {"first", "second"}


def test_a_computed_key_stays_undetermined():
    """The FP guard the literal form must not undo: an unknowable key set is None,
    so the caller cannot accuse the setter of never writing a field."""
    assert vdc._extract_values_dict_keys(
        "def s(f, v):\n  values = {}\n  values[f] = v\n  return values") is None


def test_the_cross_config_warnings_name_the_slot():
    """Both warnings ended in a literal placeholder — the continuation line of the
    message was not an f-string, so the author was told about `filled['{slot_name}']`."""
    child = copy.deepcopy(_valid_config())
    for cfg in (child,):
        cfg["slots"].append({"name": "greeting", "source": "announce",
                             "message": "Hello."})
    other = copy.deepcopy(child)
    out = _run({"all_configs": {"a": child, "b": other}})
    shadow = [w for w in out["warnings"] if "Announce slot 'greeting'" in w]
    assert shadow, out["warnings"]
    assert "filled['greeting']" in shadow[0]
    assert "{slot_name}" not in shadow[0]


def test_the_retry_counter_warning_names_the_slot():
    a = copy.deepcopy(_valid_config())
    a["slots"][1]["validation"] = {"max_retries": 2}
    b = copy.deepcopy(a)
    out = _run({"all_configs": {"a": a, "b": b}})
    retry = [w for w in out["warnings"] if "validation.max_retries" in w]
    assert retry, out["warnings"]
    assert "'slot:account'" in retry[0]
    assert "{slot_name}" not in retry[0]


# ── no false positives ────────────────────────────────────────────────────

def test_hardened_fields_left_well_formed_do_not_gain_errors():
    """Every field the hardening touches, in its LEGAL shape, on one config."""
    cfg = _valid_config()
    cfg["slots"][1]["requires"] = ["intent"]
    cfg["slots"][1]["validation"] = {"max_retries": 2,
                                     "errors": {"bad": "Try again."}}
    cfg["tasks"][0]["inputs"] = ["account"]
    cfg["tasks"][0]["outputs"] = {"balance": ["balance"]}
    cfg["tasks"][1]["on_complete"] = {"clear_slots": ["account"]}
    cfg["shared_slots"] = []
    out = _run({"raw_config": cfg})
    assert out["valid"], out["errors"]


def test_an_empty_slots_or_tasks_value_reports_what_it_always_did():
    """An empty non-list is falsy — it read as "absent" before, and still does.
    Reporting a TYPE error there would newly fail a config that used to pass."""
    for empty in ({}, "", None):
        cfg = _valid_config()
        cfg["tasks"] = empty
        errs = _errors(cfg)
        assert any("Config has no 'tasks'" in e for e in errs) or not [
            e for e in errs if "must be a list" in e], errs


def test_a_clean_config_survives_the_cross_config_path_too():
    a = _valid_config()
    b = copy.deepcopy(a)
    b["slots"] = [s for s in b["slots"] if s["name"] != "account"]
    b["tasks"] = [{"name": "Solo", "tool": "solo", "requires": ["intent"],
                   "terminal": True}]
    out = _run({"all_configs": {"a": a, "b": b}})
    assert out["per_config"]["a"]["valid"], out["per_config"]["a"]["errors"]
