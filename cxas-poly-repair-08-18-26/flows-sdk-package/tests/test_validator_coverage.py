"""Rule-by-rule coverage for the design-time DAG config validator.

The validator is the LAST gate before a config reaches a live agent, so a rule
nobody exercises is a rule nobody knows works. This file walks the rules that
had no test at all and pins each one with a PAIR:

  * a config that VIOLATES the rule, asserting the specific diagnostic fires,
    at the severity the rule chose (error / warning / needs-review blocker);
  * a minimally-different config that SATISFIES it, asserting the same
    diagnostic does NOT fire.

The second half is the one that matters most. A missed defect costs a debugging
session; a false positive blocks a build that was correct, and the author has no
way to argue with a linter. So every violation below is paired with the nearest
legal config, and `BASE` is a config that lints with zero errors AND zero
warnings so any finding is unambiguously caused by the mutation under test.

Out of scope (covered elsewhere): the crash-on-malformed-input contract lives in
test_validator_hardening.py, and this file deliberately stays off that ground —
these are the *rule* bodies, not the guards around them.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_validator_coverage.py
"""
from __future__ import annotations

import copy

from flows.engine import loader

vdc = loader.load_validator()


# ── Fixtures / helpers ────────────────────────────────────────────────────

#: Lints completely clean — no errors AND no warnings.
BASE = {
    "slots": [
        {"name": "acct", "source": "user", "setter": "set_acct",
         "ask": "What is your account number?", "hint": "account number"},
        {"name": "res", "source": "task:Lookup"},
    ],
    "tasks": [
        {"name": "Lookup", "tool": "lookup_tool", "inputs": ["acct"],
         "outputs": {"result": "res"}, "terminal": True,
         "then_say": "All set."},
    ],
}


def base() -> dict:
  return copy.deepcopy(BASE)


def result(cfg, **kw):
  return vdc.DagConfigValidator(cfg, **kw).validate()


def errs(cfg, **kw) -> list[str]:
  return result(cfg, **kw).errors


def warns(cfg, **kw) -> list[str]:
  return result(cfg, **kw).warnings


def has(msgs, fragment: str) -> bool:
  return any(fragment in m for m in msgs)


def slot(cfg, name) -> dict:
  return next(s for s in cfg["slots"] if s.get("name") == name)


def task(cfg, name) -> dict:
  return next(t for t in cfg["tasks"] if t.get("name") == name)


def cond_errors(spec, slot_set=frozenset({"a", "b"}), context="condition"):
  return vdc._validate_condition_spec(spec, slot_set, context)


def test_the_baseline_lints_clean():
  """Every pair below is a mutation of this; if it is not clean they lie."""
  r = result(base())
  assert r.valid is True
  assert r.errors == []
  assert r.warnings == []
  assert r.blockers == []
  assert r.shippable is True


# ══════════════════════════════════════════════════════════════════════════
# Declarative condition specs — _validate_condition_recursive
#
# A condition is the only thing standing between a caller and a branch that
# should not run. Every shape error below makes the gate either crash (and
# fail OPEN, in the engine's swallow-and-continue reading) or silently never
# match, so the linter is the only place it can be caught.
# ══════════════════════════════════════════════════════════════════════════

def test_condition_must_be_a_dict():
  assert has(cond_errors("acct == 1"), "condition must be a dict, got str")


def test_condition_dict_is_not_flagged_as_non_dict():
  assert not has(cond_errors({"slot": "a", "eq": "x"}),
                 "condition must be a dict")


def test_all_combinator_rejects_sibling_keys():
  out = cond_errors({"all": [{"slot": "a", "filled": True},
                             {"slot": "b", "filled": True}],
                     "slot": "a"})
  assert has(out, "'all' combinator has extra keys: ['slot']")


def test_all_combinator_alone_is_accepted():
  assert cond_errors({"all": [{"slot": "a", "filled": True},
                              {"slot": "b", "filled": True}]}) == []


def test_all_needs_at_least_two_sub_conditions():
  out = cond_errors({"all": [{"slot": "a", "filled": True}]})
  assert has(out, "'all' must have at least 2 sub-conditions")


def test_all_with_two_sub_conditions_is_accepted():
  assert not has(cond_errors({"all": [{"slot": "a", "filled": True},
                                      {"slot": "b", "filled": True}]}),
                 "at least 2 sub-conditions")


def test_any_combinator_rejects_sibling_keys():
  out = cond_errors({"any": [{"slot": "a", "filled": True},
                             {"slot": "b", "filled": True}],
                     "eq": "x"})
  assert has(out, "'any' combinator has extra keys: ['eq']")


def test_any_needs_at_least_two_sub_conditions():
  out = cond_errors({"any": [{"slot": "a", "filled": True}]})
  assert has(out, "'any' must have at least 2 sub-conditions")


def test_any_alone_with_two_subs_is_accepted():
  assert cond_errors({"any": [{"slot": "a", "eq": "x"},
                              {"slot": "b", "eq": "y"}]}) == []


def test_not_combinator_rejects_sibling_keys():
  out = cond_errors({"not": {"slot": "a", "filled": True}, "slot": "b"})
  assert has(out, "'not' combinator has extra keys: ['slot']")


def test_not_alone_is_accepted():
  assert cond_errors({"not": {"slot": "a", "filled": True}}) == []


def test_capability_and_surface_are_mutually_exclusive():
  out = cond_errors({"capability": "payloads", "surface": "voice"})
  assert has(out, "leaf reads 'capability' or 'surface', not both")


def test_capability_and_slot_are_mutually_exclusive():
  out = cond_errors({"capability": "payloads", "slot": "a"})
  assert has(out, "leaf reads 'capability' or 'slot', not both")


def test_surface_and_slot_are_mutually_exclusive():
  out = cond_errors({"surface": "voice", "slot": "a"})
  assert has(out, "leaf reads 'surface' or 'slot', not both")


def test_capability_alone_is_accepted():
  assert cond_errors({"capability": "payloads"}) == []


def test_surface_alone_is_accepted():
  assert cond_errors({"surface": "voice"}) == []


def test_capability_must_be_a_string():
  out = cond_errors({"capability": 7})
  assert has(out, "'capability' must be a string, got int")


def test_surface_must_be_a_string():
  out = cond_errors({"surface": ["voice"]})
  assert has(out, "'surface' must be a string, got list")


def test_unknown_capability_is_reported_with_the_valid_set():
  out = cond_errors({"capability": "teleport"})
  assert has(out, "unknown capability 'teleport'")


def test_a_surface_name_is_not_checked_against_the_capability_set():
  # `surface` names a delivery surface, not a capability — the capability
  # whitelist must not be applied to it.
  assert not has(cond_errors({"surface": "teleport"}), "unknown capability")


def test_capability_leaf_rejects_unknown_sibling_keys():
  out = cond_errors({"capability": "payloads", "colour": "red"})
  assert has(out, "unknown condition keys: ['colour']")


def test_capability_leaf_rejects_two_operators():
  out = cond_errors({"capability": "payloads", "eq": "x", "neq": "y"})
  assert has(out, "leaf condition has multiple operators: ['eq', 'neq']")


def test_capability_leaf_with_one_operator_is_accepted():
  assert cond_errors({"capability": "payloads", "eq": True}) == []


def test_leaf_without_slot_capability_or_surface():
  out = cond_errors({"eq": "gold"})
  assert has(out, "leaf condition missing 'slot' key")


def test_slot_must_be_a_string():
  out = cond_errors({"slot": 3, "eq": "x"})
  assert has(out, "'slot' must be a string, got int")


def test_declined_counters_are_exempt_from_the_slot_whitelist():
  # The engine synthesizes cancel_declined/escalate_declined at run time, so a
  # "contain the request once" gate names a slot no config declares.
  assert cond_errors({"slot": "cancel_declined", "gte": 1}) == []
  assert cond_errors({"slot": "escalate_declined", "gte": 1}) == []


def test_unknown_slot_in_a_condition_is_reported():
  out = cond_errors({"slot": "nope", "eq": "x"})
  assert has(out, "condition references unknown slot 'nope'")


def test_a_known_slot_is_not_reported():
  assert not has(cond_errors({"slot": "a", "eq": "x"}), "unknown slot")


def test_an_empty_slot_set_disables_the_whitelist():
  # No slot set to check against means "caller did not supply one" — the rule
  # must stay quiet rather than reject every name.
  assert cond_errors({"slot": "anything", "eq": "x"}, slot_set=set()) == []


def test_unknown_leaf_keys_are_reported():
  out = cond_errors({"slot": "a", "eq": "x", "tolerance": 2})
  assert has(out, "unknown condition keys: ['tolerance']")


def test_leaf_with_no_operator():
  out = cond_errors({"slot": "a"})
  assert has(out, "leaf condition has no operator")


def test_leaf_with_two_operators():
  out = cond_errors({"slot": "a", "eq": "x", "filled": True})
  assert has(out, "leaf condition has multiple operators: ['eq', 'filled']")


def test_leaf_with_exactly_one_operator_is_accepted():
  assert cond_errors({"slot": "a", "filled": True}) == []


def test_upper_must_be_a_bool():
  out = cond_errors({"slot": "a", "eq": "GOLD", "upper": "yes"})
  assert has(out, "'upper' must be bool, got str")


def test_upper_is_meaningless_on_a_numeric_comparison():
  out = cond_errors({"slot": "a", "gte": 3, "upper": True})
  assert has(out, "'upper' not applicable to 'gte' operator")


def test_upper_is_meaningless_on_filled():
  out = cond_errors({"slot": "a", "filled": True, "upper": True})
  assert has(out, "'upper' not applicable to 'filled' operator")


def test_upper_on_an_equality_leaf_is_accepted():
  assert cond_errors({"slot": "a", "eq": "GOLD", "upper": True}) == []


def test_filled_must_be_a_bool():
  out = cond_errors({"slot": "a", "filled": "yes"})
  assert has(out, "'filled' must be bool, got str")


def test_in_must_be_a_list():
  out = cond_errors({"slot": "a", "in": "gold"})
  assert has(out, "'in' must be a list, got str")


def test_not_in_must_be_a_list():
  out = cond_errors({"slot": "a", "not_in": {"gold": 1}})
  assert has(out, "'not_in' must be a list, got dict")


def test_in_as_a_list_is_accepted():
  assert cond_errors({"slot": "a", "in": ["gold", "silver"]}) == []


def test_comparison_operand_must_be_numeric():
  out = cond_errors({"slot": "a", "gt": "3"})
  assert has(out, "'gt' must be int or float, got str")


def test_comparison_default_must_be_numeric():
  out = cond_errors({"slot": "a", "gte": 3, "default": "zero"})
  assert has(out, "'default' for 'gte' must be numeric, got str")


def test_comparison_with_a_numeric_default_is_accepted():
  assert cond_errors({"slot": "a", "gte": 3, "default": 0}) == []


def test_nested_sub_condition_errors_carry_their_path():
  out = cond_errors({"all": [{"slot": "a", "filled": True},
                             {"slot": "nope", "eq": "x"}]})
  assert has(out, "condition.all[1]: condition references unknown slot 'nope'")


# ══════════════════════════════════════════════════════════════════════════
# steer_back — the off-topic ladder
#
# The thresholds are compared with `<=` at run time, so a string one is a
# TypeError mid-call and an out-of-order triple silently skips a rung.
# ══════════════════════════════════════════════════════════════════════════

def test_steer_back_must_be_a_dict():
  cfg = base()
  cfg["steer_back"] = "escalate"
  assert has(errs(cfg), "'steer_back' must be a dict")


def test_steer_back_thresholds_must_be_ints():
  cfg = base()
  cfg["steer_back"] = {"soft_after": "2"}
  assert has(errs(cfg), "steer_back.soft_after must be an int")


def test_steer_back_reports_each_non_int_threshold():
  cfg = base()
  cfg["steer_back"] = {"hard_after": 1.5, "escalate_after": None}
  out = errs(cfg)
  assert has(out, "steer_back.hard_after must be an int")
  # escalate_after is None == unset, so it must NOT be reported.
  assert not has(out, "steer_back.escalate_after must be an int")


def test_steer_back_thresholds_must_be_ordered():
  cfg = base()
  cfg["steer_back"] = {"soft_after": 5, "hard_after": 3, "escalate_after": 9}
  assert has(errs(cfg), "steer_back ordering violated")


def test_steer_back_ordering_uses_the_engine_defaults_for_absent_rungs():
  # soft_after alone, pushed past the default hard_after (4): still a violation.
  cfg = base()
  cfg["steer_back"] = {"soft_after": 5}
  assert has(errs(cfg), "steer_back ordering violated")


def test_ordered_steer_back_thresholds_are_accepted():
  cfg = base()
  cfg["steer_back"] = {"soft_after": 1, "hard_after": 2, "escalate_after": 3}
  r = result(cfg)
  assert r.errors == []
  assert r.warnings == []


def test_equal_steer_back_thresholds_are_accepted():
  cfg = base()
  cfg["steer_back"] = {"soft_after": 2, "hard_after": 2, "escalate_after": 2}
  assert not has(errs(cfg), "ordering violated")


def test_steer_back_rejects_cancel_keys():
  cfg = base()
  cfg["steer_back"] = {"cancel_tool": "cancel", "cancel_say": "Okay."}
  out = errs(cfg)
  assert has(out, "steer_back.cancel_tool/cancel_say are not supported")
  # ...and exactly once: the unknown-key diff subtracts them so the author is
  # not told the same thing twice in two different vocabularies.
  assert not has(out, "steer_back has unknown keys")


def test_steer_back_flags_unknown_keys():
  cfg = base()
  cfg["steer_back"] = {"soft_afterr": 2}
  assert has(errs(cfg), "steer_back has unknown keys: ['soft_afterr']")


def test_steer_back_known_keys_are_accepted():
  cfg = base()
  cfg["steer_back"] = {"soft_after": 1, "steer_back_directive": "Get back."}
  assert not has(errs(cfg), "unknown keys")


def test_steer_back_on_exhaust_is_validated():
  cfg = base()
  cfg["steer_back"] = {"on_exhaust": {"then": 5}}
  assert has(errs(cfg), "steer_back.on_exhaust.then must be string or dict")


def test_steer_back_on_exhaust_with_a_string_then_is_accepted():
  cfg = base()
  cfg["steer_back"] = {"on_exhaust": {"then": "escalate", "say": "One moment."}}
  assert errs(cfg) == []


# ══════════════════════════════════════════════════════════════════════════
# no_input — the silence ladder
# ══════════════════════════════════════════════════════════════════════════

def test_no_input_must_be_a_dict():
  cfg = base()
  cfg["no_input"] = ["Are you there?"]
  assert has(errs(cfg), "'no_input' must be a dict")


def test_no_input_ladders_must_be_lists():
  cfg = base()
  cfg["no_input"] = {"reprompts": "Are you there?"}
  assert has(errs(cfg), "no_input.reprompts must be a list")


def test_no_input_ladder_rungs_must_be_strings():
  cfg = base()
  cfg["no_input"] = {"hold_reprompts": ["ok", 7]}
  assert has(errs(cfg), "no_input.hold_reprompts[1] must be a string")


def test_no_input_string_ladders_are_accepted():
  cfg = base()
  cfg["no_input"] = {"reprompts": ["Are you there?"],
                     "hold_phrases": ["one moment"],
                     "hold_reprompts": []}
  assert errs(cfg) == []


def test_no_input_hold_ack_must_be_a_string():
  cfg = base()
  cfg["no_input"] = {"hold_ack": ["Take your time."],
                     "hold_phrases": ["one moment"]}
  assert has(errs(cfg), "no_input.hold_ack must be a string")


def test_hold_ack_without_hold_phrases_can_never_fire():
  cfg = base()
  cfg["no_input"] = {"hold_ack": "Take your time."}
  assert has(errs(cfg), "no_input.hold_ack is set but no_input.hold_phrases is")


def test_hold_ack_with_hold_phrases_is_accepted():
  cfg = base()
  cfg["no_input"] = {"hold_ack": "Take your time.",
                     "hold_phrases": ["give me a second"]}
  assert errs(cfg) == []


def test_no_input_flags_unknown_keys():
  cfg = base()
  cfg["no_input"] = {"reprompt": ["Are you there?"]}
  assert has(errs(cfg), "no_input has unknown keys: ['reprompt']")


def test_no_input_on_exhaust_may_arm_a_declared_open_slot():
  cfg = base()
  cfg["no_input"] = {"on_exhaust": {"open_slot": "acct"}}
  assert errs(cfg) == []


def test_no_input_on_exhaust_open_slot_must_be_declared():
  cfg = base()
  cfg["no_input"] = {"on_exhaust": {"open_slot": "nope"}}
  assert has(errs(cfg),
             "no_input.on_exhaust.open_slot 'nope' is not a declared slot")


# ══════════════════════════════════════════════════════════════════════════
# on_exhaust — the shared bottom-of-the-ladder shape
#
# The same dict is read at five call sites with different capabilities, and
# the site-gating is the interesting part: a key that works on a task's
# on_failure is DEAD on a slot's validation exhaust.
# ══════════════════════════════════════════════════════════════════════════

def _slot_exhaust(cfg, exhaust):
  slot(cfg, "acct")["validation"] = {"on_exhaust": exhaust}
  return cfg


def test_on_exhaust_must_be_a_dict():
  cfg = _slot_exhaust(base(), "escalate")
  assert has(errs(cfg), "Slot 'acct' validation.on_exhaust must be a dict")


def test_fill_is_allowed_on_a_slot_validation_exhaust():
  cfg = _slot_exhaust(base(), {"fill": "unknown"})
  assert not has(errs(cfg), "is only supported on a slot's validation")


def test_fill_is_rejected_on_a_task_on_failure_exhaust():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"on_exhaust": {"fill": "x"}}
  assert has(errs(cfg),
             "on_failure.on_exhaust.fill is only supported on a slot's"
             " validation.on_exhaust")


def test_fill_must_be_a_string():
  cfg = _slot_exhaust(base(), {"fill": ["unknown"]})
  assert has(errs(cfg), "validation.on_exhaust.fill must be a string")


def test_fill_and_then_are_contradictory_dispositions():
  cfg = _slot_exhaust(base(), {"fill": "unknown", "then": "escalate"})
  assert has(errs(cfg), "sets both `fill` and `then`")


def test_open_slot_is_rejected_on_a_slot_validation_exhaust():
  cfg = _slot_exhaust(base(), {"open_slot": "acct"})
  assert has(errs(cfg), "open_slot is only supported on task on_failure and")


def test_open_slot_must_be_a_string():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"on_exhaust": {"open_slot": 3}}
  assert has(errs(cfg), "on_failure.on_exhaust.open_slot must be a string")


def test_component_is_rejected_on_a_slot_validation_exhaust():
  cfg = _slot_exhaust(base(), {"component": "help_offer"})
  assert has(errs(cfg), "component is only supported on task on_failure and")


def test_component_must_be_a_non_empty_string():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"on_exhaust": {"component": ""}}
  assert has(errs(cfg),
             "on_failure.on_exhaust.component must be a non-empty string")


def test_component_is_accepted_on_a_task_on_failure_exhaust():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"on_exhaust": {"component": "help"}}
  assert not has(errs(cfg), "is only supported on task on_failure")


def test_escalate_must_be_a_bool_where_it_is_allowed():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"on_exhaust": {"escalate": "no"}}
  assert has(errs(cfg), "on_failure.on_exhaust.escalate must be a bool")


def test_escalate_false_is_accepted_on_a_task_on_failure_exhaust():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {
      "on_exhaust": {"escalate": False, "then": "pivot"}}
  assert not has(errs(cfg), "escalate")


def test_then_dict_must_carry_a_tool():
  cfg = _slot_exhaust(base(), {"then": {"args": {"a": 1}}})
  assert has(errs(cfg), "validation.on_exhaust.then dict missing 'tool'")


def test_then_dict_with_a_tool_is_accepted():
  cfg = _slot_exhaust(base(), {"then": {"tool": "escalate_tool"}})
  assert not has(errs(cfg), "missing 'tool'")


def test_then_must_be_a_string_or_dict():
  cfg = _slot_exhaust(base(), {"then": ["escalate"]})
  assert has(errs(cfg), "validation.on_exhaust.then must be string or dict")


def test_say_must_be_a_string_where_no_reason_map_is_allowed():
  cfg = _slot_exhaust(base(), {"then": "escalate", "say": {"E1": "Sorry."}})
  assert has(errs(cfg), "validation.on_exhaust.say must be a string")
  assert not has(errs(cfg), "reason map keyed by error_code")


def test_a_reason_keyed_say_is_allowed_on_a_task_on_failure_exhaust():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {
      "on_exhaust": {"say": {"E1": "Sorry.", "_default": "Sorry."}}}
  assert not has(errs(cfg), "say must be a string")


def test_an_empty_reason_map_is_reported():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"on_exhaust": {"say": {}}}
  assert has(errs(cfg), "on_failure.on_exhaust.say is an empty reason map")


def test_a_reason_map_must_be_strings_to_strings():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"on_exhaust": {"say": {"E1": 7}}}
  assert has(errs(cfg), "say reason map must be {error_code: line} strings")


def test_a_non_string_say_names_the_reason_map_where_it_is_allowed():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"on_exhaust": {"say": 7}}
  assert has(errs(cfg), "say must be a string or a reason map keyed by error_code")


def test_exhaust_channel_responses_must_be_a_dict():
  cfg = _slot_exhaust(base(), {"then": "escalate", "channel_responses": []})
  assert has(errs(cfg),
             "validation.on_exhaust.channel_responses must be a dict of channel")


def test_exhaust_channel_responses_parts_are_validated():
  cfg = _slot_exhaust(
      base(), {"then": "escalate",
               "channel_responses": {"voice": [{"text": "Sorry."}]}})
  assert has(errs(cfg),
             "validation.on_exhaust channel_responses[voice] response[0]"
             " missing 'type'")


def test_exhaust_channel_responses_with_valid_parts_are_accepted():
  cfg = _slot_exhaust(
      base(),
      {"then": "escalate",
       "channel_responses": {"voice": [{"type": "text", "text": "Sorry."}]}})
  assert errs(cfg) == []


def test_exhaust_end_conversation_must_be_a_bool():
  cfg = _slot_exhaust(base(), {"then": "escalate", "end_conversation": "yes"})
  assert has(errs(cfg), "validation.on_exhaust.end_conversation must be a bool")


def test_exhaust_flags_unknown_keys():
  cfg = _slot_exhaust(base(), {"then": "escalate", "sey": "typo"})
  assert has(errs(cfg), "validation.on_exhaust has unknown keys: ['sey']")


def test_a_site_gated_key_is_not_also_reported_as_unknown():
  # open_slot got a specific "only supported on..." diagnostic; repeating it in
  # the unknown-key diff would send the author looking for a typo.
  cfg = _slot_exhaust(base(), {"open_slot": "acct"})
  assert not has(errs(cfg), "has unknown keys")


# ══════════════════════════════════════════════════════════════════════════
# exit_status
# ══════════════════════════════════════════════════════════════════════════

def test_exit_status_must_be_a_dict():
  cfg = base()
  cfg["exit_status"] = ["done"]
  assert has(errs(cfg), "'exit_status' must be a dict")


def test_exit_status_values_must_be_strings():
  cfg = base()
  cfg["exit_status"] = {"outcome": 7}
  assert has(errs(cfg), "exit_status['outcome'] must be a string")


def test_exit_status_string_values_are_accepted():
  cfg = base()
  cfg["exit_status"] = {"outcome": "res"}
  r = result(cfg)
  assert r.errors == []
  assert r.warnings == []


def test_exit_status_without_a_terminal_task_is_dead_config():
  cfg = base()
  cfg["exit_status"] = {"outcome": "res"}
  task(cfg, "Lookup").pop("terminal")
  assert has(warns(cfg), "exit_status is defined but no task is")


# ══════════════════════════════════════════════════════════════════════════
# event_mappings
#
# The engine writes event_data[mapping_key], and the prefill loop reads
# event_data[event_key or name]. A key that is neither never lands, silently.
# ══════════════════════════════════════════════════════════════════════════

def _event_base():
  cfg = base()
  cfg["slots"].append({"name": "ani", "source": "event", "event_key": "ani"})
  return cfg


def test_event_mappings_must_be_a_dict():
  cfg = _event_base()
  cfg["event_mappings"] = ["call_start"]
  assert has(errs(cfg), "'event_mappings' must be a dict")


def test_an_event_mapping_entry_must_be_a_dict():
  cfg = _event_base()
  cfg["event_mappings"] = {"call_start": "ani"}
  assert has(errs(cfg), "event_mappings['call_start'] must be a dict")


def test_an_event_mapping_to_a_declared_event_key_is_accepted():
  cfg = _event_base()
  cfg["event_mappings"] = {"call_start": {"ani": "5551234"}}
  assert errs(cfg) == []


def test_an_event_mapping_value_outside_the_slots_enum_never_lands():
  cfg = _event_base()
  slot(cfg, "ani")["validation_rules"] = [{"kind": "enum", "detail": "a|b"}]
  cfg["event_mappings"] = {"call_start": {"ani": "z"}}
  assert has(errs(cfg),
             "event_mappings['call_start']['ani'] value 'z' not in enum")


def test_an_event_mapping_value_inside_the_enum_is_accepted():
  cfg = _event_base()
  slot(cfg, "ani")["validation_rules"] = [{"kind": "enum", "detail": "a|b"}]
  cfg["event_mappings"] = {"call_start": {"ani": "a"}}
  assert not has(errs(cfg), "not in enum")


def test_an_event_mapping_to_an_unknown_slot_is_a_warning():
  cfg = _event_base()
  cfg["event_mappings"] = {"call_start": {"ghost": "x"}}
  r = result(cfg)
  assert has(r.warnings, "event_mappings['call_start'] targets unknown slot 'ghost'")
  assert r.valid is True  # a warning must never block the build


def test_an_event_mapping_to_a_non_event_slot_never_prefills():
  cfg = _event_base()
  cfg["event_mappings"] = {"call_start": {"acct": "5551234"}}
  assert has(errs(cfg),
             "event_mappings['call_start'] target 'acct' must be a source:event"
             " slot")


def test_an_event_mapping_keyed_by_name_when_the_slot_renamed_its_event_key():
  cfg = _event_base()
  slot(cfg, "ani")["event_key"] = "caller_number"
  cfg["event_mappings"] = {"call_start": {"ani": "5551234"}}
  assert has(errs(cfg),
             "event_mappings['call_start'] key 'ani' must equal target event_key"
             " 'caller_number'")


def test_an_event_slot_with_no_event_key_is_addressed_by_its_name():
  cfg = base()
  cfg["slots"].append({"name": "ani", "source": "event"})
  cfg["event_mappings"] = {"call_start": {"ani": "5551234"}}
  # (the slot draws its own unrelated findings for the missing event_key; the
  # mapping itself resolves, which is what this pins)
  assert not has(errs(cfg), "event_mappings")
  assert not has(warns(cfg), "event_mappings")


# ══════════════════════════════════════════════════════════════════════════
# answer — the grounded, non-advancing free-response policy
# ══════════════════════════════════════════════════════════════════════════

def _answer_entry(**over):
  entry = {"name": "acct_qa", "scope": "account questions",
           "instruction": "Answer from the balance only.",
           "max_turns": 2, "grounds": ["res"]}
  entry.update(over)
  return entry


def _with_answer(entry):
  cfg = base()
  cfg["answer"] = [entry] if isinstance(entry, dict) else entry
  return cfg


def test_a_well_formed_answer_policy_is_clean():
  r = result(_with_answer(_answer_entry()))
  assert r.errors == []
  assert r.warnings == []


def test_answer_must_be_a_list():
  cfg = base()
  cfg["answer"] = _answer_entry()
  assert has(errs(cfg), "'answer' must be a list of answer configs")


def test_an_answer_entry_must_be_a_dict():
  assert has(errs(_with_answer(["acct_qa"])), "answer[0] must be a dict")


def test_answer_prose_fields_must_be_non_empty_strings():
  out = errs(_with_answer(_answer_entry(scope="   ")))
  assert has(out, "answer 'acct_qa' scope must be a non-empty string")


def test_an_unnamed_answer_entry_is_addressed_by_index():
  out = errs(_with_answer(_answer_entry(name="")))
  assert has(out, "answer[0] name must be a non-empty string")


def test_answer_max_turns_must_be_a_positive_int():
  assert has(errs(_with_answer(_answer_entry(max_turns=0))),
             "answer 'acct_qa' max_turns must be an int > 0")
  assert has(errs(_with_answer(_answer_entry(max_turns=True))),
             "max_turns must be an int > 0")
  assert has(errs(_with_answer(_answer_entry(max_turns="2"))),
             "max_turns must be an int > 0")


def test_answer_allow_math_must_be_a_bool():
  assert has(errs(_with_answer(_answer_entry(allow_math="false"))),
             "answer 'acct_qa' allow_math must be a bool, got str")


def test_answer_allow_math_true_is_accepted():
  assert errs(_with_answer(_answer_entry(allow_math=True))) == []


def test_answer_grounds_must_be_a_list():
  assert has(errs(_with_answer(_answer_entry(grounds="res"))),
             "answer 'acct_qa' grounds must be a list of slot/var names")


def test_answer_tools_must_be_a_list():
  assert has(errs(_with_answer(_answer_entry(tools="lookup_tool"))),
             "answer 'acct_qa' tools must be a list of declared tool names")


def test_an_answer_with_neither_grounds_nor_tools_can_only_invent():
  entry = _answer_entry()
  entry.pop("grounds")
  assert has(errs(_with_answer(entry)),
             "must set at least one of 'grounds' or 'tools'")


def test_answer_grounds_entries_must_be_non_empty_strings():
  assert has(errs(_with_answer(_answer_entry(grounds=[""]))),
             "answer 'acct_qa' grounds entry must be a non-empty string")


def test_answer_grounds_must_name_a_declared_slot():
  assert has(errs(_with_answer(_answer_entry(grounds=["ghost"]))),
             "answer 'acct_qa' grounds references unknown slot/var 'ghost'")


def test_answer_tools_entries_must_be_non_empty_strings():
  assert has(errs(_with_answer(_answer_entry(tools=[7]))),
             "answer 'acct_qa' tools entry must be a non-empty string")


def test_answer_tools_must_be_declared_when_a_tool_list_is_supplied():
  cfg = _with_answer(_answer_entry(tools=["read_balance"]))
  out = errs(cfg, available_tools=["lookup_tool", "set_acct"])
  assert has(out, "answer 'acct_qa' tool 'read_balance' not in agent tool list")


def test_a_declared_answer_tool_is_accepted():
  cfg = _with_answer(_answer_entry(tools=["read_balance"]))
  out = errs(cfg, available_tools=["lookup_tool", "set_acct", "read_balance"])
  assert not has(out, "not in agent tool list")


def test_a_commit_looking_answer_tool_is_only_a_warning():
  cfg = _with_answer(_answer_entry(tools=["submit_order"]))
  r = result(cfg)
  assert has(r.warnings, "whose name looks like a COMMIT")
  assert r.valid is True


def test_a_read_looking_answer_tool_draws_no_warning():
  cfg = _with_answer(_answer_entry(tools=["read_balance"]))
  assert warns(cfg) == []


def test_answer_condition_must_be_a_dict():
  assert has(errs(_with_answer(_answer_entry(condition="lambda f: True"))),
             "answer 'acct_qa' condition must be a dict")


def test_answer_condition_is_validated_against_the_slot_set():
  out = errs(_with_answer(_answer_entry(condition={"slot": "ghost", "filled": True})))
  assert has(out, "answer 'acct_qa': condition references unknown slot 'ghost'")


def test_a_valid_answer_condition_is_accepted():
  assert errs(_with_answer(
      _answer_entry(condition={"slot": "res", "filled": True}))) == []


def test_answer_requires_must_be_a_list():
  assert has(errs(_with_answer(_answer_entry(requires="res"))),
             "answer 'acct_qa' requires must be a list of slot names")


def test_answer_requires_entries_must_be_non_empty_strings():
  assert has(errs(_with_answer(_answer_entry(requires=["  "]))),
             "answer 'acct_qa' requires entry must be a non-empty string")


def test_answer_requires_must_name_a_declared_slot():
  assert has(errs(_with_answer(_answer_entry(requires=["ghost"]))),
             "answer 'acct_qa' requires unknown slot 'ghost'")


def test_a_declared_answer_requires_is_accepted():
  assert errs(_with_answer(_answer_entry(requires=["acct"]))) == []


def test_an_answer_turn_may_not_fill_a_slot():
  out = errs(_with_answer(_answer_entry(setter="set_acct")))
  assert has(out, "sets slot-fill key(s) ['setter'] — the answer turn")
  # ...and the dedicated message replaces the unknown-key diff, not doubles it.
  assert not has(out, "has unknown keys")


def test_answer_flags_unknown_keys():
  assert has(errs(_with_answer(_answer_entry(instructions="typo"))),
             "answer 'acct_qa' has unknown keys: ['instructions']")


# ══════════════════════════════════════════════════════════════════════════
# The ValidationResult contract callers depend on
# ══════════════════════════════════════════════════════════════════════════

def test_a_warning_never_blocks_a_build():
  """The single most important property in the file.

  Warnings are heuristics. If one flipped `valid` the linter would block a
  build on a guess, so the split is asserted directly rather than inferred.
  """
  cfg = base()
  cfg["exit_status"] = {"outcome": "res"}
  task(cfg, "Lookup").pop("terminal")
  r = result(cfg)
  assert r.warnings, "expected the dead-exit_status warning"
  assert r.errors == []
  assert r.valid is True
  assert r.shippable is True


def test_an_error_does_block_a_build():
  cfg = base()
  cfg["exit_status"] = {"outcome": 7}
  r = result(cfg)
  assert r.errors
  assert r.valid is False
  assert r.shippable is False


def test_every_diagnostic_has_a_flat_twin():
  cfg = base()
  cfg["steer_back"] = {"soft_afterr": 1}
  cfg["exit_status"] = {"outcome": "res"}
  task(cfg, "Lookup").pop("terminal")
  r = result(cfg)
  by_sev = {"error": [], "warning": [], "needs_review": []}
  for d in r.diagnostics:
    assert set(d) == {"severity", "message", "code", "anchor", "fix_id"}
    by_sev[d["severity"]].append(d["message"])
  assert by_sev["error"] == r.errors
  assert by_sev["warning"] == r.warnings


def test_a_coded_diagnostic_carries_its_invariant_signature():
  # The Studio client and the fix synthesiser switch on `code`, not on prose,
  # so a message edit that drops the signature has to be caught here.
  cfg = base()
  cfg["slots"].append({"name": "acct", "source": "user", "ask": "Again?"})
  r = result(cfg)
  coded = [d for d in r.diagnostics if d["code"]]
  assert coded, "expected at least one coded diagnostic"
  for d in coded:
    assert d["code"] in vdc._CODE_MESSAGES, d
    assert vdc._CODE_MESSAGES[d["code"]] in d["message"], d


def test_the_needs_review_tier_leaves_valid_alone_but_sinks_shippable():
  """The third severity tier's contract, exercised on the helper directly.

  Nothing in the validator emits one today (see the report accompanying this
  file), so this is the only place the tier's promise — `valid` counts ERRORS,
  `shippable` counts errors AND blockers — is actually pinned.
  """
  for cls, args in ((vdc.DagConfigValidator, (base(),)),
                    (vdc.CrossConfigValidator, ({"acme": base()},))):
    v = cls(*args)
    v._blocker("looks hand-edited", code=None, anchor={"kind": "slot"})
    r = v.validate()
    assert r.blockers == ["looks hand-edited"]
    assert r.valid is True
    assert r.shippable is False
    assert any(d["severity"] == "needs_review" for d in r.diagnostics)


def test_repeated_validate_calls_are_idempotent():
  v = vdc.DagConfigValidator(base())
  first = v.validate()
  second = v.validate()
  assert first.errors == second.errors
  assert first.warnings == second.warnings


# ══════════════════════════════════════════════════════════════════════════
# CrossConfigValidator — the bundle-level rules
# ══════════════════════════════════════════════════════════════════════════

def cross(configs, routing_tables=None):
  return vdc.CrossConfigValidator(configs, routing_tables=routing_tables).validate()


def _leaf(**over):
  """A config that is clean on its OWN, so a cross-config finding is the only
  thing a bundle of them can produce."""
  cfg = {"slots": [{"name": "acct", "source": "user", "setter": "set_acct",
                    "ask": "Account?", "hint": "account number"},
                   {"name": "res", "source": "task:Lookup"}],
         "tasks": [{"name": "Lookup", "tool": "lookup_tool", "inputs": ["acct"],
                    "outputs": {"result": "res"}, "terminal": True,
                    "then_say": "All set."}],
         "bootstrap": {"tool": "start_flow", "reset_on_complete": True}}
  cfg.update(over)
  return cfg


def test_the_cross_config_leaf_fixture_is_clean_on_its_own():
  r = result(_leaf())
  assert r.errors == []
  assert r.warnings == []


def test_a_single_config_bundle_still_runs_the_component_checks():
  # The <2 early return must not skip the checks that need only one config —
  # a self-referential component is a length-1 cycle and is real.
  configs = {"acme": {"tasks": [{"name": "Sub", "component": "acme"}]}}
  out = cross(configs).errors
  assert has(out, "Component config cycle detected involving 'acme'")


def test_a_single_clean_config_bundle_is_quiet():
  r = cross({"acme": _leaf()})
  assert r.errors == []
  assert r.warnings == []
  assert r.valid is True


def test_a_single_config_bundle_skips_the_sibling_only_rules():
  # gate_slot / bootstrap-tool consistency compare SIBLINGS; with none there is
  # nothing to be inconsistent with.
  r = cross({"acme": _leaf(gate_slot="intent")})
  assert not has(r.warnings, "Different gate_slot names")
  assert not has(r.warnings, "Different bootstrap tools")


# ── routing tables ────────────────────────────────────────────────────────

def _router_bundle():
  configs = {
      "router": {"router": True, "flow_types": ["billing", "tech"],
                 "gate_slot": "intent"},
      "billing": _leaf(gate_slot="intent"),
      "tech": _leaf(gate_slot="intent"),
  }
  tables = {"default_config_id": "router",
            "flow_config_map": {"billing": "billing", "tech": "tech"}}
  return configs, tables


def test_a_consistent_router_bundle_draws_no_routing_findings():
  configs, tables = _router_bundle()
  r = cross(configs, tables)
  assert not has(r.errors, "flow_config_map")
  assert not has(r.errors, "router flow_type")
  assert not has(r.warnings, "gate_slot")


def test_routing_is_not_checked_at_all_without_routing_tables():
  configs, _ = _router_bundle()
  assert not has(cross(configs).errors, "not in bundled configs")


def test_a_default_config_id_that_is_not_bundled():
  configs, tables = _router_bundle()
  tables["default_config_id"] = "ghost"
  assert has(cross(configs, tables).errors,
             "default_config_id 'ghost' not in bundled configs")


def test_a_flow_config_map_target_that_is_not_bundled():
  configs, tables = _router_bundle()
  tables["flow_config_map"]["billing"] = "ghost"
  assert has(cross(configs, tables).errors,
             "flow_config_map target 'ghost' (for 'billing') not in bundled")


def test_an_agent_config_map_target_that_is_not_bundled():
  configs, tables = _router_bundle()
  tables["agent_config_map"] = {"BillingAgent": "ghost"}
  assert has(cross(configs, tables).errors,
             "agent_config_map target 'ghost' (for 'BillingAgent') not in bundled")


def test_an_intent_config_map_target_that_is_not_bundled():
  configs, tables = _router_bundle()
  tables["intent_config_map"] = {"pay_bill": "ghost"}
  assert has(cross(configs, tables).errors,
             "intent_config_map target 'ghost' (for 'pay_bill') not in bundled")


def test_routing_tables_are_accepted_as_json_strings():
  # before_agent stores these in SM state as JSON, so a tool caller may hand
  # either shape over; the check must read both the same way.
  import json as _json
  configs, tables = _router_bundle()
  tables["flow_config_map"] = _json.dumps({"billing": "ghost", "tech": "tech"})
  assert has(cross(configs, tables).errors, "flow_config_map target 'ghost'")


def test_a_router_flow_type_with_no_config_leaves_the_caller_on_the_router():
  configs, tables = _router_bundle()
  del tables["flow_config_map"]["tech"]
  assert has(cross(configs, tables).errors,
             "router flow_type 'tech' has no config")


def test_a_router_flow_type_routed_via_its_agent_is_accepted():
  configs, tables = _router_bundle()
  del tables["flow_config_map"]["tech"]
  tables["flow_to_agent"] = {"tech": "TechAgent"}
  tables["agent_config_map"] = {"TechAgent": "tech"}
  assert not has(cross(configs, tables).errors, "router flow_type 'tech'")


def test_a_flow_to_agent_hop_that_dead_ends_is_still_reported():
  configs, tables = _router_bundle()
  del tables["flow_config_map"]["tech"]
  tables["flow_to_agent"] = {"tech": "TechAgent"}  # no agent_config_map entry
  assert has(cross(configs, tables).errors, "router flow_type 'tech' has no config")


def test_a_dangling_default_config_id_does_not_cascade():
  # Reachability and gate alignment both read the ROUTER config; with the
  # default dangling they must stay quiet rather than blame every flow_type.
  configs, tables = _router_bundle()
  tables["default_config_id"] = "ghost"
  out = cross(configs, tables).errors
  assert not has(out, "router flow_type")


def test_a_target_gating_on_a_different_slot_is_a_warning():
  configs, tables = _router_bundle()
  configs["tech"]["gate_slot"] = "topic"
  r = cross(configs, tables)
  assert has(r.warnings,
             "flow 'tech' target 'tech' gate_slot 'topic' != router gate_slot"
             " 'intent'")
  assert not has(r.errors, "gate_slot 'topic'")


def test_a_target_with_no_gate_slot_of_its_own_is_not_flagged():
  configs, tables = _router_bundle()
  configs["tech"].pop("gate_slot")
  assert not has(cross(configs, tables).warnings, "flow 'tech' target")


def test_a_router_with_no_gate_slot_skips_the_alignment_rule():
  configs, tables = _router_bundle()
  configs["router"].pop("gate_slot")
  configs["tech"]["gate_slot"] = "topic"
  assert not has(cross(configs, tables).warnings, "!= router gate_slot")


# ── components across configs ─────────────────────────────────────────────

def _parent_with_component(child_id="child", **task_over):
  t = {"name": "Sub", "component": child_id}
  t.update(task_over)
  return {"slots": [{"name": "acct", "source": "user", "setter": "set_acct",
                     "ask": "Account?"}],
          "tasks": [t, {"name": "Done", "tool": "finish", "terminal": True}],
          "bootstrap": {"tool": "start_flow", "reset_on_complete": True}}


def _child(**over):
  cfg = {"slots": [{"name": "pin", "source": "user", "setter": "set_pin",
                    "ask": "PIN?"}],
         "tasks": [{"name": "Verify", "tool": "verify", "terminal": True,
                    "outputs": {"ok": "verified"}}]}
  cfg.update(over)
  return cfg


def test_a_component_child_that_does_not_exist():
  out = cross({"parent": _parent_with_component("ghost")}).errors
  assert has(out, "Config 'parent' component task 'Sub' references unknown"
                  " child config 'ghost'")


def test_a_component_child_that_exists_is_accepted():
  out = cross({"parent": _parent_with_component(), "child": _child()}).errors
  assert not has(out, "references unknown child config")


def test_an_empty_component_ref_is_not_reported_as_a_missing_child():
  # An empty string is a SHAPE defect, owned by the per-config pass; blaming a
  # missing child config '' would send the author looking for the wrong thing.
  out = cross({"parent": _parent_with_component("")}).errors
  assert not has(out, "references unknown child config")


def test_an_on_exhaust_component_descent_must_resolve():
  parent = _parent_with_component()
  parent["no_input"] = {"on_exhaust": {"component": "ghost"}}
  out = cross({"parent": parent, "child": _child()}).errors
  assert has(out, "no_input.on_exhaust.component references unknown child"
                  " config 'ghost'")


def test_a_task_on_failure_component_descent_must_resolve():
  parent = _parent_with_component()
  parent["tasks"][1]["on_failure"] = {"on_exhaust": {"component": "ghost"}}
  out = cross({"parent": parent, "child": _child()}).errors
  assert has(out, "task 'Done' on_failure.on_exhaust.component references"
                  " unknown child config 'ghost'")


def test_a_component_child_needs_a_way_out():
  child = _child(tasks=[{"name": "Verify", "tool": "verify"}])
  out = cross({"parent": _parent_with_component(), "child": child}).errors
  assert has(out, "child config 'child' has no terminal task and does not end"
                  " the conversation")


def test_a_component_child_that_ends_the_call_is_accepted():
  child = _child(tasks=[{"name": "Bye", "tool": "hangup",
                         "end_conversation": True, "terminal": True}])
  out = cross({"parent": _parent_with_component(), "child": child}).errors
  assert not has(out, "has no terminal task")


def test_an_exhaust_component_must_end_the_conversation_not_just_return():
  # There is no parent slot to fall back to, so a plain frame-return re-asks
  # the slot that just exhausted — forever.
  parent = _parent_with_component()
  parent["no_input"] = {"on_exhaust": {"component": "child"}}
  out = cross({"parent": parent, "child": _child()}).errors
  assert has(out, "no_input.on_exhaust.component 'child' must END the"
                  " conversation")


def test_an_exhaust_component_that_ends_the_call_is_accepted():
  parent = _parent_with_component()
  parent["no_input"] = {"on_exhaust": {"component": "child"}}
  child = _child(tasks=[{"name": "Bye", "tool": "hangup", "terminal": True,
                         "end_conversation": True}])
  out = cross({"parent": parent, "child": child}).errors
  assert not has(out, "must END the conversation")


def test_an_exhaust_component_terminating_via_an_announce_slot():
  parent = _parent_with_component()
  parent["no_input"] = {"on_exhaust": {"component": "child"}}
  child = _child(slots=[{"name": "bye", "source": "announce",
                         "message": "Goodbye.", "end_conversation": True,
                         "response": [{"type": "end_session"}]}])
  assert not has(cross({"parent": parent, "child": child}).errors,
                 "must END the conversation")


def test_a_channel_specific_end_session_still_terminates_the_child():
  # A child that only hangs up on the voice channel still terminates; reading
  # `response` alone would false-positive on it.
  parent = _parent_with_component()
  parent["no_input"] = {"on_exhaust": {"component": "child"}}
  child = _child(slots=[{"name": "bye", "source": "announce",
                         "message": "Goodbye.", "end_conversation": True,
                         "channel_responses": {
                             "voice": [{"type": "end_session"}]}}])
  assert not has(cross({"parent": parent, "child": child}).errors,
                 "must END the conversation")


def test_a_component_input_must_name_a_slot_in_the_child():
  parent = _parent_with_component(inputs={"acct": "ghost"})
  out = cross({"parent": parent, "child": _child()}).errors
  assert has(out, "input maps to child slot 'ghost' not in child config 'child'")


def test_a_component_input_that_names_a_real_child_slot_is_accepted():
  parent = _parent_with_component(inputs={"acct": "pin"})
  assert not has(cross({"parent": parent, "child": _child()}).errors,
                 "input maps to child slot")


def test_a_component_output_key_must_be_produced_by_the_child():
  parent = _parent_with_component(outputs={"ghost": "acct"})
  out = cross({"parent": parent, "child": _child()}).errors
  assert has(out, "output key 'ghost' is not produced by child config 'child'")


def test_a_component_output_key_the_child_produces_is_accepted():
  parent = _parent_with_component(outputs={"ok": "acct"})
  assert not has(cross({"parent": parent, "child": _child()}).errors,
                 "is not produced by child config")


def test_a_repeated_component_done_setter_must_be_a_child_slot():
  parent = _parent_with_component(
      repeated={"until": {"done_setter": "ghost"}})
  out = cross({"parent": parent, "child": _child()}).errors
  assert has(out, "done_setter 'ghost' is not a slot in child config 'child'")


def test_a_repeated_component_done_setter_that_is_a_child_slot_is_accepted():
  parent = _parent_with_component(repeated={"until": {"done_setter": "pin"}})
  assert not has(cross({"parent": parent, "child": _child()}).errors,
                 "is not a slot in child config")


def test_a_repeated_component_element_key_must_be_a_child_slot():
  parent = _parent_with_component(
      repeated={"until": {"max_count": 3}}, element={"ghost": "field"})
  out = cross({"parent": parent, "child": _child()}).errors
  assert has(out, "element key 'ghost' is not a slot in child config 'child'")


def test_a_repeated_component_element_key_that_resolves_is_accepted():
  parent = _parent_with_component(
      repeated={"until": {"max_count": 3}}, element={"pin": "field"})
  assert not has(cross({"parent": parent, "child": _child()}).errors,
                 "element key")


def test_a_non_repeated_component_task_skips_the_repeated_rules():
  parent = _parent_with_component(element={"ghost": "field"})
  assert not has(cross({"parent": parent, "child": _child()}).errors,
                 "element key")


def test_a_two_config_component_cycle_is_reported():
  configs = {"a": {"tasks": [{"name": "S", "component": "b"}]},
             "b": {"tasks": [{"name": "S", "component": "a"}]}}
  assert has(cross(configs).errors, "Component config cycle detected")


def test_a_self_referential_exhaust_descent_is_a_cycle():
  configs = {"a": {"no_input": {"on_exhaust": {"component": "a"}}}}
  assert has(cross(configs).errors,
             "Component config cycle detected involving 'a'")


def test_an_escalate_component_cycle_is_reported():
  configs = {"a": {"escalate": {"component": "a"}}}
  assert has(cross(configs).errors,
             "Component config cycle detected involving 'a'")


def test_component_call_depth_is_capped_at_three():
  chain = ["a", "b", "c", "d", "e"]
  configs = {cid: {"tasks": [{"name": "S", "component": nxt}]}
             for cid, nxt in zip(chain, chain[1:])}
  configs["e"] = {"tasks": [{"name": "End", "tool": "t", "terminal": True}]}
  out = cross(configs).errors
  assert has(out, "Component call depth exceeds 3 on path a -> b -> c -> d -> e")


def test_a_three_deep_component_chain_is_accepted():
  chain = ["a", "b", "c", "d"]
  configs = {cid: {"tasks": [{"name": "S", "component": nxt},
                             {"name": "End", "tool": "t", "terminal": True}]}
             for cid, nxt in zip(chain, chain[1:])}
  configs["d"] = {"tasks": [{"name": "End", "tool": "t", "terminal": True}]}
  assert not has(cross(configs).errors, "Component call depth exceeds")


# ── shared-state contamination across configs ─────────────────────────────

def test_a_terminal_config_without_reset_on_complete_zombies_its_siblings():
  a = _leaf()
  a["bootstrap"] = {"tool": "start_flow"}
  out = cross({"a": a, "b": _leaf()}).errors
  assert has(out, "Config 'a' has terminal tasks but bootstrap.reset_on_complete"
                  " is not True")


def test_reset_on_complete_clears_the_zombie_rule():
  assert not has(cross({"a": _leaf(), "b": _leaf()}).errors, "zombie")


def test_a_duplicated_announce_slot_is_only_announced_once():
  welcome = {"name": "welcome", "source": "announce", "message": "Hello."}
  a = _leaf()
  b = _leaf()
  a["slots"] = a["slots"] + [welcome]
  b["slots"] = b["slots"] + [dict(welcome)]
  r = cross({"a": a, "b": b})
  assert has(r.warnings, "Announce slot 'welcome' in configs ['a', 'b']")
  assert r.valid is True


def test_a_uniquely_named_announce_slot_is_not_flagged():
  a = _leaf()
  b = _leaf()
  a["slots"] = a["slots"] + [{"name": "welcome_a", "source": "announce",
                              "message": "Hi."}]
  b["slots"] = b["slots"] + [{"name": "welcome_b", "source": "announce",
                              "message": "Hi."}]
  assert not has(cross({"a": a, "b": b}).warnings, "Announce slot")


def test_an_unscoped_shared_user_slot_is_flagged():
  assert has(cross({"a": _leaf(), "b": _leaf()}).warnings,
             "Slot 'acct' shared by configs ['a', 'b'] without scoping conditions")


def test_asymmetric_scoping_gets_its_own_message():
  a = _leaf()
  b = _leaf()
  b["slots"] = [dict(b["slots"][0], condition={"slot": "acct", "filled": False})]
  r = cross({"a": a, "b": b})
  assert has(r.warnings, "Slot 'acct' shared: ['a'] have no condition, ['b'] do")
  assert not has(r.warnings, "without scoping conditions")


def test_both_sides_scoped_draws_no_sharing_warning():
  cond = {"slot": "acct", "filled": False}
  a = _leaf()
  b = _leaf()
  a["slots"] = [dict(a["slots"][0], condition=cond)]
  b["slots"] = [dict(b["slots"][0], condition=cond)]
  assert not has(cross({"a": a, "b": b}).warnings, "Slot 'acct' shared")


def test_a_shared_retry_budget_is_flagged():
  a = _leaf()
  b = _leaf()
  a["slots"] = [dict(a["slots"][0], validation={"max_retries": 2})]
  b["slots"] = [dict(b["slots"][0], validation={"max_retries": 3})]
  assert has(cross({"a": a, "b": b}).warnings,
             "Slot 'acct' has validation.max_retries in configs ['a', 'b']")


def test_a_retry_budget_in_only_one_config_is_not_flagged():
  a = _leaf()
  a["slots"] = [dict(a["slots"][0], validation={"max_retries": 2})]
  assert not has(cross({"a": a, "b": _leaf()}).warnings, "carries over between")


def test_divergent_steer_back_thresholds_carry_over():
  a = _leaf(steer_back={"soft_after": 1, "hard_after": 2, "escalate_after": 3})
  b = _leaf(steer_back={"soft_after": 4, "hard_after": 5, "escalate_after": 6})
  assert has(cross({"a": a, "b": b}).warnings,
             "steer_back thresholds differ across configs")


def test_identical_steer_back_thresholds_do_not_warn():
  sb = {"soft_after": 1, "hard_after": 2, "escalate_after": 3}
  assert not has(cross({"a": _leaf(steer_back=sb),
                        "b": _leaf(steer_back=dict(sb))}).warnings,
                 "steer_back thresholds differ")


def test_a_lone_steer_back_block_has_nothing_to_diverge_from():
  a = _leaf(steer_back={"soft_after": 1})
  assert not has(cross({"a": a, "b": _leaf()}).warnings,
                 "steer_back thresholds differ")


def test_divergent_gate_slots_are_flagged():
  assert has(cross({"a": _leaf(gate_slot="intent"),
                    "b": _leaf(gate_slot="topic")}).warnings,
             "Different gate_slot names across configs")


def test_a_single_shared_gate_slot_is_not_flagged():
  assert not has(cross({"a": _leaf(gate_slot="intent"),
                        "b": _leaf(gate_slot="intent")}).warnings,
                 "Different gate_slot names")


def test_divergent_bootstrap_tools_are_flagged():
  b = _leaf()
  b["bootstrap"] = {"tool": "other_start", "reset_on_complete": True}
  assert has(cross({"a": _leaf(), "b": b}).warnings,
             "Different bootstrap tools across configs")


def test_a_single_shared_bootstrap_tool_is_not_flagged():
  assert not has(cross({"a": _leaf(), "b": _leaf()}).warnings,
                 "Different bootstrap tools")


def test_a_control_block_without_transfer_to_is_a_multi_agent_warning():
  a = _leaf(cancel={"say": "Okay, cancelled."})
  r = cross({"a": a, "b": _leaf()})
  assert has(r.warnings, "[a] 'cancel' control block omits 'transfer_to'")
  assert r.valid is True


def test_a_control_block_with_transfer_to_is_accepted():
  a = _leaf(escalate={"say": "One moment.", "transfer_to": "Human"})
  assert not has(cross({"a": a, "b": _leaf()}).warnings,
                 "control block omits 'transfer_to'")


# ── the CES tool entry point over a bundle ────────────────────────────────

def test_the_entry_point_reports_per_config_and_cross_config_separately():
  a = _leaf()
  a["bootstrap"] = {"tool": "start_flow"}  # cross-config zombie error
  a["slots"] = a["slots"] + [{"name": "acct", "source": "user",
                              "ask": "Again?"}]  # per-config duplicate
  out = vdc.validate_dag_config({"all_configs": {"a": a, "b": _leaf()}})
  assert out["valid"] is False
  assert set(out) >= {"valid", "errors", "warnings", "per_config",
                      "cross_config", "blockers", "shippable"}
  assert has(out["per_config"]["a"]["errors"], "Duplicate slot names")
  assert has(out["cross_config"]["errors"], "reset_on_complete is not True")
  # Every per-config finding is namespaced by config id in the combined list.
  assert has(out["errors"], "[a] Duplicate slot names")
  assert out["per_config"]["b"]["valid"] is True


def test_the_entry_point_accepts_routing_tables():
  configs, tables = _router_bundle()
  tables["default_config_id"] = "ghost"
  out = vdc.validate_dag_config({"all_configs": configs,
                                 "routing_tables": tables})
  assert has(out["cross_config"]["errors"], "default_config_id 'ghost'")


# ══════════════════════════════════════════════════════════════════════════
# Repeated slots — Mode A (collect N scalars into a list slot)
#
# The failure this family guards is a collection loop with no bottom: the
# caller is asked the same question until the reasoning-loop cap fires.
# ══════════════════════════════════════════════════════════════════════════

def repeated_base(**over):
  cfg = base()
  s = {"name": "items", "source": "user", "setter": "set_item",
       "ask": "Which item?", "hint": "item",
       "repeated": {"until": {"max_count": 3}, "ask_more": "Anything else?"},
       "readback_fmt": {"type": "join", "each": "{item}"}}
  s.update(over)
  cfg["slots"].insert(1, s)
  return cfg


def test_the_repeated_slot_fixture_is_clean():
  r = result(repeated_base())
  assert r.errors == []
  assert r.warnings == []


def test_repeated_must_be_a_dict():
  cfg = repeated_base(repeated=["until"])
  assert has(errs(cfg), "Slot 'items' repeated must be a dict, got list")


def test_repeated_flags_a_typo_in_its_own_keys():
  cfg = repeated_base(repeated={"untill": {"max_count": 3}})
  assert has(errs(cfg), "Slot 'items' repeated has unknown keys: ['untill']")


def test_repeated_until_must_be_a_dict():
  cfg = repeated_base(repeated={"until": 3})
  assert has(errs(cfg), "Slot 'items' repeated.until must be a dict")


def test_repeated_until_flags_a_typo_in_its_own_keys():
  cfg = repeated_base(repeated={"until": {"max_cont": 3}})
  assert has(errs(cfg), "repeated.until has unknown keys: ['max_cont']")


def test_repeated_done_setter_must_be_a_string():
  cfg = repeated_base(repeated={"until": {"done_setter": 7}})
  assert has(errs(cfg), "repeated.until.done_setter must be a string, got int")


def test_a_repeated_slot_must_be_user_sourced():
  cfg = repeated_base(source="task:Lookup")
  assert has(errs(cfg), "Repeated slot 'items' must have source 'user'")


def test_a_repeated_slot_needs_a_setter_to_stage_each_element():
  cfg = repeated_base()
  slot(cfg, "items").pop("setter")
  assert has(errs(cfg), "Repeated slot 'items' must have a 'setter'")


def test_a_repeated_slot_may_not_ask_for_per_element_readback():
  cfg = repeated_base(requires_readback=True)
  assert has(errs(cfg), "Repeated slot 'items' must not set requires_readback")


def test_a_repeated_slot_needs_a_termination_affordance():
  cfg = repeated_base(repeated={"ask_more": "Anything else?"})
  assert has(errs(cfg), "Repeated slot 'items' needs a termination affordance")


def test_an_empty_done_setter_is_not_a_termination_affordance():
  # It is falsy at run time, so the loop never terminates — treating it as
  # present would let the config ship.
  cfg = repeated_base(repeated={"until": {"done_setter": "   "}})
  assert has(errs(cfg), "needs a termination affordance")


def test_a_done_setter_terminates_collection():
  cfg = repeated_base(repeated={"until": {"done_setter": "set_done"}})
  assert not has(errs(cfg), "needs a termination affordance")


def test_a_slot_condition_is_not_a_termination_affordance():
  cfg = repeated_base(repeated={"ask_more": "More?"},
                      condition={"slot": "acct", "filled": True})
  assert has(errs(cfg), "a slot 'condition' is the activation gate, not a"
                        " termination affordance")


def test_max_count_must_not_be_a_bool():
  cfg = repeated_base(repeated={"until": {"max_count": True}})
  assert has(errs(cfg),
             "Repeated slot 'items' repeated.until.max_count must be an int,"
             " got bool")


def test_an_integral_float_max_count_is_accepted():
  # CES deserializes config numbers as floats, so 3 arrives as 3.0. Rejecting
  # it was a false ship-blocker.
  cfg = repeated_base(repeated={"until": {"max_count": 3.0}})
  assert errs(cfg) == []


def test_a_fractional_max_count_is_a_typo():
  cfg = repeated_base(repeated={"until": {"max_count": 3.5}})
  assert has(errs(cfg), "max_count must be a whole number, got 3.5")


def test_an_infinite_max_count_is_rejected():
  cfg = repeated_base(repeated={"until": {"max_count": float("inf")}})
  assert has(errs(cfg), "max_count must be an int, got inf")


def test_a_nan_max_count_is_rejected():
  cfg = repeated_base(repeated={"until": {"max_count": float("nan")}})
  assert has(errs(cfg), "max_count must be an int, got nan")


def test_a_non_numeric_max_count_is_rejected():
  cfg = repeated_base(repeated={"until": {"max_count": "3"}})
  assert has(errs(cfg), "max_count must be an int, got str")


def test_max_count_must_be_positive():
  cfg = repeated_base(repeated={"until": {"max_count": 0}})
  assert has(errs(cfg), "repeated.until.max_count must be > 0, got 0")


def test_min_count_must_not_be_negative():
  cfg = repeated_base(
      repeated={"until": {"max_count": 3}, "min_count": -1})
  assert has(errs(cfg), "repeated.min_count must be >= 0, got -1")


def test_min_count_may_not_exceed_max_count():
  cfg = repeated_base(repeated={"until": {"max_count": 2}, "min_count": 5})
  assert has(errs(cfg), "repeated.min_count (5) must be <= max_count (2)")


def test_a_min_count_within_max_count_is_accepted():
  cfg = repeated_base(repeated={"until": {"max_count": 5}, "min_count": 2})
  assert errs(cfg) == []


def test_a_malformed_min_count_stops_the_range_comparison():
  # It could not be coerced, so comparing it to max_count would be nonsense.
  cfg = repeated_base(repeated={"until": {"max_count": 2}, "min_count": "two"})
  out = errs(cfg)
  assert has(out, "repeated.min_count must be an int, got str")
  assert not has(out, "must be <= max_count")


def test_a_mode_a_join_template_may_only_bind_item():
  cfg = repeated_base(readback_fmt={"type": "join", "each": "{qty} x {name}"})
  assert has(warns(cfg),
             "readback_fmt `each` references ['name', 'qty'] but Mode A"
             " elements are scalars")


def test_a_mode_a_join_template_binding_item_draws_no_warning():
  assert warns(repeated_base()) == []


def test_a_mode_a_done_setter_must_be_a_registered_tool():
  cfg = repeated_base(repeated={"until": {"done_setter": "set_done"}})
  out = errs(cfg, available_tools=["set_acct", "set_item", "lookup_tool"])
  assert has(out, "repeated.until.done_setter 'set_done' not in agent tool list")


def test_a_registered_done_setter_is_accepted():
  cfg = repeated_base(repeated={"until": {"done_setter": "set_done"}})
  out = errs(cfg, available_tools=["set_acct", "set_item", "lookup_tool",
                                   "set_done"])
  assert not has(out, "not in agent tool list")


# ══════════════════════════════════════════════════════════════════════════
# Repeated components — Mode B (re-descend a child DAG once per element)
# ══════════════════════════════════════════════════════════════════════════

def component_base(**task_over):
  cfg = base()
  cfg["slots"].append({"name": "lines", "source": "task:Sub",
                       "readback_fmt": {"type": "join", "each": "{qty}"}})
  t = {"name": "Sub", "component": "child",
       "repeated": {"until": {"max_count": 3}},
       "collect": "lines", "element": {"pin": "qty"}}
  t.update(task_over)
  cfg["tasks"].insert(0, t)
  return cfg


def test_the_repeated_component_fixture_is_clean():
  r = result(component_base())
  assert r.errors == []
  assert r.warnings == []


def test_collect_on_a_non_component_task_is_rejected():
  cfg = base()
  task(cfg, "Lookup")["collect"] = "res"
  assert has(errs(cfg),
             "Task 'Lookup' has 'collect'/'element' but is not a component task")


def test_collect_on_a_component_without_repeated_is_rejected():
  cfg = component_base()
  task(cfg, "Sub").pop("repeated")
  assert has(errs(cfg),
             "Component task 'Sub' has 'collect'/'element' but no 'repeated'")


def test_a_component_repeated_must_be_a_dict():
  cfg = component_base(repeated="3")
  assert has(errs(cfg), "Component task 'Sub' repeated must be a dict, got str")


def test_a_repeated_component_must_name_a_collect_slot():
  cfg = component_base()
  task(cfg, "Sub").pop("collect")
  assert has(errs(cfg),
             "Repeated component task 'Sub' must name a 'collect' slot")


def test_a_repeated_component_collect_slot_must_exist():
  cfg = component_base(collect="ghost")
  assert has(errs(cfg),
             "Repeated component task 'Sub' collect slot 'ghost' not in slots")


def test_a_collect_slot_needs_a_join_readback_fmt():
  cfg = component_base()
  slot(cfg, "lines").pop("readback_fmt")
  assert has(errs(cfg), "must declare a 'join' readback_fmt — elements are"
                        " dicts")


def test_a_join_each_template_may_only_name_element_fields():
  cfg = component_base()
  slot(cfg, "lines")["readback_fmt"] = {"type": "join", "each": "{qty} {sku}"}
  assert has(errs(cfg), "readback_fmt `each` references '{sku}' which is not an"
                        " element field")


def test_a_join_each_template_naming_only_element_fields_is_accepted():
  cfg = component_base(element={"pin": "qty", "code": "sku"})
  slot(cfg, "lines")["readback_fmt"] = {"type": "join", "each": "{qty} {sku}"}
  assert not has(errs(cfg), "is not an element field")


def test_a_repeated_component_needs_a_non_empty_element_map():
  cfg = component_base(element={})
  assert has(errs(cfg),
             "Repeated component task 'Sub' must have a non-empty 'element' dict")


def test_a_repeated_component_needs_a_termination_affordance():
  cfg = component_base(repeated={})
  assert has(errs(cfg),
             "Repeated component task 'Sub' needs a termination affordance")


def test_an_over_list_is_itself_a_termination_affordance():
  cfg = component_base(repeated={"over": "res", "each": {"pin": "qty"}},
                       requires=["res"])
  assert not has(errs(cfg), "needs a termination affordance")


def test_over_must_name_a_declared_slot():
  cfg = component_base(repeated={"over": "ghost", "each": {"pin": "qty"}})
  assert has(errs(cfg), "repeated.over slot 'ghost' not in slots")


def test_over_must_be_a_non_empty_string():
  cfg = component_base(repeated={"over": [], "each": {"pin": "qty"}})
  assert has(errs(cfg), "repeated.over must be a non-empty string")


def test_over_requires_an_each_mapping():
  cfg = component_base(repeated={"over": "res"}, requires=["res"])
  assert has(errs(cfg),
             "repeated.over requires a non-empty repeated.each dict")


def test_over_may_not_name_a_provably_scalar_slot():
  cfg = component_base(repeated={"over": "acct", "each": {"pin": "qty"}},
                       requires=["acct"])
  slot(cfg, "acct")["validation_rules"] = [{"kind": "length_digits",
                                            "detail": "8"}]
  assert has(errs(cfg), "repeated.over slot 'acct' is a single scalar value")


def test_over_on_a_mode_a_repeated_slot_is_a_warning():
  cfg = component_base(repeated={"over": "picks", "each": {"pin": "qty"}},
                       requires=["picks"])
  cfg["slots"].append({"name": "picks", "source": "user", "setter": "set_pick",
                       "ask": "Which?", "hint": "pick",
                       "repeated": {"until": {"max_count": 2}},
                       "readback_fmt": {"type": "join", "each": "{item}"}})
  r = result(cfg)
  assert has(r.warnings, "is a Mode-A repeated slot (a list of scalars)")
  assert not has(r.errors, "repeated.over slot 'picks'")


def test_over_on_a_plain_user_scalar_is_a_warning():
  cfg = component_base(repeated={"over": "acct", "each": {"pin": "qty"}},
                       requires=["acct"])
  assert has(warns(cfg), "repeated.over slot 'acct' is a plain user scalar")


def test_an_unproduced_over_list_is_a_warning():
  # Nothing fills `acct` before the component fires, so the loop finalizes with
  # zero elements and the collection silently never happens.
  cfg = component_base(repeated={"over": "acct", "each": {"pin": "qty"}})
  assert has(warns(cfg), "is not in requires, not gated by the task condition,"
                         " and not produced by any task output")


def test_an_over_list_a_sibling_task_produces_draws_no_production_warning():
  cfg = component_base(repeated={"over": "res", "each": {"pin": "qty"}})
  assert not has(warns(cfg), "not produced by any task output")


def test_a_required_over_list_draws_no_production_warning():
  cfg = component_base(repeated={"over": "res", "each": {"pin": "qty"}},
                       requires=["res"])
  assert not has(warns(cfg), "not produced by any task output")


def test_a_condition_gated_over_list_draws_no_production_warning():
  cfg = component_base(repeated={"over": "res", "each": {"pin": "qty"}},
                       condition={"slot": "res", "filled": True})
  assert not has(warns(cfg), "not produced by any task output")


# ══════════════════════════════════════════════════════════════════════════
# validate_against — the cross-slot comparison block
# ══════════════════════════════════════════════════════════════════════════

def test_validate_against_must_be_a_dict():
  cfg = base()
  slot(cfg, "acct")["validate_against"] = ["res"]
  assert has(errs(cfg), "Slot 'acct' validate_against must be a dict")


def test_validate_against_requires_all_three_fields():
  cfg = base()
  slot(cfg, "acct")["validate_against"] = {"response_field": "ok"}
  out = errs(cfg)
  assert has(out, "Slot 'acct' validate_against missing 'filled_slot'")
  assert has(out, "Slot 'acct' validate_against missing 'error_code'")
  assert not has(out, "missing 'response_field'")


def test_validate_against_filled_slot_must_exist():
  cfg = base()
  slot(cfg, "acct")["validate_against"] = {
      "response_field": "ok", "filled_slot": "ghost", "error_code": "E1"}
  assert has(errs(cfg),
             "Slot 'acct' validate_against.filled_slot 'ghost' not in slots")


def test_a_complete_validate_against_block_is_accepted():
  cfg = base()
  slot(cfg, "acct")["validate_against"] = {
      "response_field": "ok", "filled_slot": "res", "error_code": "E1"}
  assert errs(cfg) == []


# ══════════════════════════════════════════════════════════════════════════
# Lambda-string conditions
#
# Every defect here fails OPEN — the engine swallows the error and treats the
# gate as active — so the linter is the only thing that can catch it.
# ══════════════════════════════════════════════════════════════════════════

def _with_lambda(src):
  cfg = base()
  slot(cfg, "res")["condition"] = src
  return cfg


def test_a_lambda_condition_that_does_not_parse():
  assert has(errs(_with_lambda("lambda f: f[")), "condition syntax error")


def test_a_lambda_condition_containing_a_nul_byte():
  assert has(errs(_with_lambda("lambda f: f\x00")), "condition syntax error")


def test_a_condition_string_that_is_not_a_lambda_fails_open():
  assert has(errs(_with_lambda("f.get('acct')")),
             "condition string must be a lambda expression")


def test_a_lambda_with_the_wrong_arity():
  assert has(errs(_with_lambda("lambda a, b: True")),
             "condition lambda must take exactly one plain positional arg")


def test_a_lambda_with_a_default_arg():
  assert has(errs(_with_lambda("lambda f=None: True")),
             "must take exactly one plain positional arg")


def test_a_lambda_with_varargs():
  assert has(errs(_with_lambda("lambda *f: True")),
             "must take exactly one plain positional arg")


def test_a_lambda_referencing_an_undefined_name():
  assert has(errs(_with_lambda("lambda f: f.get('acct') == threshold")),
             "condition lambda references undefined name 'threshold'")


def test_a_lambda_reading_only_its_own_arg_is_accepted():
  assert errs(_with_lambda("lambda f: bool(f.get('acct'))")) == []


def test_a_lambda_may_bind_names_in_a_comprehension():
  assert errs(_with_lambda(
      "lambda f: len([x for x in f.get('acct') or []]) > 0")) == []


def test_a_lambda_may_only_call_the_safe_builtins():
  # `any` is not in the safe globals, so it is a NameError at gate time.
  assert has(errs(_with_lambda("lambda f: any(f.values())")),
             "references undefined name 'any'")


def test_a_slot_condition_of_an_impossible_type():
  cfg = base()
  slot(cfg, "res")["condition"] = 7
  assert has(errs(cfg),
             "Slot 'res' condition must be dict, callable or string, got int")


# ══════════════════════════════════════════════════════════════════════════
# bootstrap / gate_slot / single_flow / flow_types
# ══════════════════════════════════════════════════════════════════════════

def test_bootstrap_must_be_a_dict():
  cfg = base()
  cfg["bootstrap"] = "start_flow"
  assert has(errs(cfg), "'bootstrap' must be a dict")


def test_bootstrap_name_fields_must_be_strings():
  cfg = base()
  cfg["bootstrap"] = {"tool": ["start_flow"]}
  assert has(errs(cfg), "bootstrap.tool must be a name string, got list")


def test_a_toolless_bootstrap_slot_may_be_filled_externally():
  cfg = base()
  cfg["bootstrap"] = {"slot": "intent"}
  w = warns(cfg)
  assert has(w, "bootstrap.slot 'intent' not in slots — may be filled externally")
  assert has(w, "bootstrap has no 'tool'")


def test_a_bootstrap_with_a_tool_fills_its_own_gate_slot():
  # The standard framework pattern: the gate slot lives in the tool, not in
  # `slots`. Warning about it would fire on every routed config there is.
  cfg = base()
  cfg["bootstrap"] = {"slot": "intent", "tool": "start_flow"}
  assert not has(warns(cfg), "may be filled externally")


def test_a_welcome_slot_must_exist():
  cfg = base()
  cfg["bootstrap"] = {"tool": "start_flow", "welcome_slot": "ghost"}
  assert has(warns(cfg), "bootstrap.welcome_slot 'ghost' not in slots")


def test_a_welcome_slot_must_be_an_announce_slot():
  cfg = base()
  cfg["bootstrap"] = {"tool": "start_flow", "welcome_slot": "acct"}
  assert has(warns(cfg), "bootstrap.welcome_slot 'acct' is not an announce slot")


def test_an_announce_welcome_slot_is_accepted():
  cfg = base()
  cfg["slots"].insert(0, {"name": "welcome", "source": "announce",
                          "message": "Hello."})
  cfg["bootstrap"] = {"tool": "start_flow", "welcome_slot": "welcome"}
  assert warns(cfg) == []


def test_bootstrap_pass_through_on_transfer_must_be_a_bool():
  cfg = base()
  cfg["bootstrap"] = {"tool": "start_flow", "pass_through_on_transfer": "yes"}
  assert has(errs(cfg), "bootstrap.pass_through_on_transfer must be a bool")


def test_bootstrap_flags_a_nested_typo():
  cfg = base()
  cfg["bootstrap"] = {"tool": "start_flow", "reset_on_complte": True}
  assert has(errs(cfg), "bootstrap has unknown keys: ['reset_on_complte']")


def test_gate_slot_must_be_a_string():
  cfg = base()
  cfg["gate_slot"] = ["intent"]
  assert has(errs(cfg), "gate_slot must be a slot name string, got list")


def test_an_undeclared_gate_slot_may_be_filled_externally():
  cfg = base()
  cfg["gate_slot"] = "intent"
  assert has(warns(cfg), "gate_slot 'intent' not in slots — may be filled")


def test_a_bootstrap_filled_gate_slot_is_not_flagged():
  cfg = base()
  cfg["gate_slot"] = "intent"
  cfg["bootstrap"] = {"slot": "intent", "tool": "start_flow"}
  assert not has(warns(cfg), "gate_slot 'intent' not in slots")


def _single_flow(**boot):
  cfg = base()
  cfg["single_flow"] = True
  cfg["gate_slot"] = "scope"
  b = {"slot": "scope", "reset_on_complete": True}
  b.update(boot)
  cfg["bootstrap"] = b
  return cfg


def test_a_well_formed_single_flow_config_is_clean():
  r = result(_single_flow())
  assert r.errors == []
  assert r.warnings == []


def test_a_single_flow_agent_may_not_declare_a_bootstrap_tool():
  assert has(errs(_single_flow(tool="start_flow")),
             "single_flow agent has no router — bootstrap must NOT declare a"
             " 'tool'")


def test_single_flow_requires_a_self_seeded_gate():
  cfg = _single_flow()
  cfg["gate_slot"] = "other"
  assert has(errs(cfg), "single_flow requires a self-seeded gate")


def test_single_flow_with_a_terminal_task_needs_reset_on_complete():
  cfg = _single_flow()
  cfg["bootstrap"].pop("reset_on_complete")
  assert has(errs(cfg), "single_flow with a terminal task requires"
                        " bootstrap.reset_on_complete:true")


def test_single_flow_auto_seed_must_be_a_string():
  assert has(errs(_single_flow(auto_seed=True)),
             "bootstrap.auto_seed must be a string")


def test_single_flow_auto_seed_as_a_string_is_accepted():
  assert errs(_single_flow(auto_seed="billing")) == []


def test_flow_types_must_be_a_list():
  cfg = base()
  cfg["flow_types"] = "billing"
  assert has(errs(cfg), "'flow_types' must be a list")


def test_flow_types_entries_must_be_non_empty_strings():
  cfg = base()
  cfg["flow_types"] = ["billing", "  "]
  assert has(errs(cfg), "flow_types entries must be non-empty strings")


def test_a_list_of_flow_type_names_is_accepted():
  cfg = base()
  cfg["flow_types"] = ["billing", "tech"]
  assert errs(cfg) == []


# ══════════════════════════════════════════════════════════════════════════
# Control blocks (cancel / escalate) and their refusal lines
# ══════════════════════════════════════════════════════════════════════════

def _with_block(name="escalate", **fields):
  cfg = base()
  cfg[name] = fields
  return cfg


def test_a_control_block_must_be_a_dict():
  cfg = base()
  cfg["escalate"] = "transfer"
  assert has(errs(cfg), "'escalate' must be a dict")


def test_control_block_transfer_to_must_be_a_string():
  assert has(errs(_with_block(transfer_to=["Human"])),
             "escalate.transfer_to must be a string")


def test_control_block_prose_fields_must_be_strings():
  assert has(errs(_with_block(say=7)), "escalate.say must be a string")
  assert has(errs(_with_block(name="cancel", outcome=7)),
             "cancel.outcome must be a string")


def test_control_block_verbatim_must_be_a_bool():
  assert has(errs(_with_block(verbatim="yes")),
             "escalate.verbatim must be a bool")


def test_control_block_requires_readback_must_be_a_bool():
  assert has(errs(_with_block(requires_readback="yes")),
             "escalate.requires_readback must be a bool")


def test_control_block_exit_status_must_be_a_dict():
  assert has(errs(_with_block(exit_status=["x"])),
             "escalate.exit_status must be a dict")


def test_control_block_condition_must_be_a_dict():
  assert has(errs(_with_block(condition="lambda f: True")),
             "escalate.condition must be a dict")


def test_control_block_condition_slots_must_be_declared():
  assert has(errs(_with_block(condition={"slot": "ghost", "filled": False})),
             "escalate.condition references undeclared slot 'ghost'")


def test_a_control_block_may_gate_on_its_own_declined_counter():
  # The engine synthesizes it; no slot declares it, so this must not error.
  cfg = _with_block(condition={"slot": "escalate_declined", "lt": 1},
                    declined_say="Let me try to help first.")
  assert errs(cfg) == []


def test_a_declined_say_without_a_condition_can_never_be_spoken():
  assert has(errs(_with_block(declined_say="Not right now.")),
             "escalate.declined_say is set without escalate.condition")


def test_a_control_block_tool_is_ignored_at_runtime():
  r = result(_with_block(tool="my_escalate"))
  assert has(r.warnings, "escalate.tool ('my_escalate') is ignored at runtime")
  assert r.valid is True


def test_declined_say_must_be_a_line_a_ladder_or_reasons():
  cfg = _with_block(condition={"slot": "acct", "filled": True},
                    declined_say={"when": None})
  assert has(errs(cfg), "escalate.declined_say must be a line, a ladder of"
                        " lines, or a list")


def test_an_empty_declined_say_list_says_nothing():
  cfg = _with_block(condition={"slot": "acct", "filled": True},
                    declined_say=[])
  assert has(errs(cfg), "escalate.declined_say is an empty list")


def test_a_declined_say_ladder_must_hold_strings():
  cfg = _with_block(condition={"slot": "acct", "filled": True},
                    declined_say=["First refusal.", 7])
  assert has(errs(cfg), "escalate.declined_say list must hold strings")


def test_a_declined_say_ladder_of_strings_is_accepted():
  cfg = _with_block(condition={"slot": "acct", "filled": True},
                    declined_say=["Let me try.", "Still let me try."])
  assert errs(cfg) == []


def test_a_declined_say_reason_needs_a_say_line():
  cfg = _with_block(condition={"slot": "acct", "filled": True},
                    declined_say=[{"when": {"slot": "acct", "filled": True}}])
  assert has(errs(cfg), "escalate.declined_say[0] needs a `say` line or ladder")


def test_a_declined_say_reason_when_must_be_a_condition_dict():
  cfg = _with_block(condition={"slot": "acct", "filled": True},
                    declined_say=[{"when": "acct", "say": "No."}])
  assert has(errs(cfg), "escalate.declined_say[0].when must be a condition dict")


def test_a_declined_say_reason_entry_of_the_wrong_type():
  cfg = _with_block(condition={"slot": "acct", "filled": True},
                    declined_say=[7, {"say": "No."}])
  assert has(errs(cfg), "escalate.declined_say[0] must be a line or a")


def test_a_declined_say_reason_when_must_name_declared_slots():
  cfg = _with_block(condition={"slot": "acct", "filled": True},
                    declined_say=[{"when": {"slot": "ghost", "filled": True},
                                   "say": "No."},
                                  {"say": "Sorry."}])
  assert has(errs(cfg),
             "escalate.declined_say[0].when references undeclared slot 'ghost'")


def test_a_catch_all_reason_must_come_last():
  cfg = _with_block(
      condition={"slot": "acct", "filled": True},
      declined_say=[{"say": "Sorry."},
                    {"when": {"slot": "acct", "filled": True}, "say": "No."}])
  assert has(errs(cfg), "escalate.declined_say[1] can never be reached")


def test_reasons_with_the_catch_all_last_are_accepted():
  cfg = _with_block(
      condition={"slot": "acct", "filled": True},
      declined_say=[{"when": {"slot": "acct", "filled": True}, "say": "No."},
                    {"say": "Sorry."}])
  assert errs(cfg) == []
  assert warns(cfg) == []


def test_reasons_with_no_catch_all_can_refuse_in_silence():
  cfg = _with_block(
      condition={"slot": "acct", "filled": True},
      declined_say=[{"when": {"slot": "acct", "filled": True}, "say": "No."}])
  r = result(cfg)
  assert has(r.warnings, "escalate.declined_say has no catch-all reason")
  assert r.valid is True


# ══════════════════════════════════════════════════════════════════════════
# readback_fmt — the voice in which a value is read back
#
# The compiler reads named keys and drops the rest, so a bad param has no
# symptom until a live call reads the slot back in the wrong voice.
# ══════════════════════════════════════════════════════════════════════════

def _fmt(value):
  cfg = base()
  slot(cfg, "acct")["readback_fmt"] = value
  return cfg


def test_a_readback_fmt_string_shorthand_must_be_a_known_type():
  assert has(errs(_fmt("shouty")), "readback_fmt string 'shouty' not recognized")


def test_a_known_readback_fmt_string_shorthand_is_accepted():
  assert errs(_fmt("digits")) == []


def test_readback_fmt_type_must_be_a_string():
  assert has(errs(_fmt({"type": ["digits"]})),
             "readback_fmt 'type' must be a string, got list")


def test_a_readback_fmt_dict_needs_a_type():
  assert has(errs(_fmt({"text": "Account"})),
             "Slot 'acct' readback_fmt dict missing 'type'")


def test_a_readback_fmt_dict_type_must_be_known():
  assert has(errs(_fmt({"type": "shouty"})),
             "readback_fmt type 'shouty' not recognized")


def test_a_readback_fmt_type_must_carry_its_required_fields():
  out = errs(_fmt({"type": "plural", "one": "item"}))
  assert has(out, "readback_fmt type 'plural' missing required field 'other'")
  assert not has(out, "missing required field 'one'")


def test_a_complete_readback_fmt_dict_is_accepted():
  assert errs(_fmt({"type": "plural", "one": "item", "other": "items"})) == []


def test_a_readback_fmt_param_the_formatter_never_reads():
  assert has(errs(_fmt({"type": "digits", "values": {"a": "b"}})),
             "readback_fmt type 'digits' has unknown param(s) ['values']")


def test_an_optional_readback_fmt_param_is_accepted():
  assert errs(_fmt({"type": "digits", "text": "Your account"})) == []
  assert errs(_fmt({"type": "prefix", "text": "Account",
                    "values": {"a": "b"}})) == []


def test_readback_fmt_must_be_a_string_dict_or_callable():
  assert has(errs(_fmt(7)),
             "Slot 'acct' readback_fmt must be string, dict, or callable")


def test_a_callable_readback_fmt_is_accepted():
  assert errs(_fmt(lambda v: v)) == []


# ══════════════════════════════════════════════════════════════════════════
# validation blocks
# ══════════════════════════════════════════════════════════════════════════

def _validation(block):
  cfg = base()
  slot(cfg, "acct")["validation"] = block
  return cfg


def test_validation_errors_must_be_a_dict():
  assert has(errs(_validation({"errors": ["nope"]})),
             "Slot 'acct' validation.errors must be a dict")


def test_validation_max_retries_must_be_an_int():
  assert has(errs(_validation({"max_retries": "2"})),
             "Slot 'acct' validation.max_retries must be an int")


def test_a_max_retries_below_one_means_no_retries():
  r = result(_validation({"max_retries": 0}))
  assert has(r.warnings, "validation.max_retries is 0 — effectively no retries")
  assert r.valid is True


def test_validation_reprompts_must_be_a_list_of_strings():
  assert has(errs(_validation({"reprompts": "Try again."})),
             "Slot 'acct' validation.reprompts must be a list of strings")


def test_validation_flags_unknown_keys():
  assert has(errs(_validation({"max_retrys": 2})),
             "Slot 'acct' validation has unknown keys: ['max_retrys']")


def test_a_complete_validation_block_is_accepted():
  cfg = _validation({"max_retries": 2, "reprompts": ["Once more?"],
                     "errors": {"not_in_enum": "Try again."},
                     "on_exhaust": {"then": "escalate"}})
  assert errs(cfg) == []


def test_an_exhaust_fill_must_be_one_of_the_slots_own_values():
  cfg = base()
  s = slot(cfg, "acct")
  s["validation_rules"] = [{"kind": "enum", "detail": "gold|silver"}]
  s["validation"] = {"on_exhaust": {"fill": "bronze"}}
  assert has(errs(cfg),
             "validation.on_exhaust.fill is 'bronze', which is not one of its"
             " values: ['gold', 'silver']")


def test_an_exhaust_fill_inside_the_enum_is_accepted():
  cfg = base()
  s = slot(cfg, "acct")
  s["validation_rules"] = [{"kind": "enum", "detail": "gold|silver"}]
  s["validation"] = {"on_exhaust": {"fill": "gold"}}
  assert not has(errs(cfg), "not one of its values")


# ══════════════════════════════════════════════════════════════════════════
# Slot wiring — announce, requires, options_from
# ══════════════════════════════════════════════════════════════════════════

def test_an_announce_slot_needs_a_message_or_a_response():
  cfg = base()
  cfg["slots"].insert(0, {"name": "welcome", "source": "announce"})
  assert has(errs(cfg), "Announce slot 'welcome' requires 'message' or 'response'")


def test_an_announce_slot_with_only_a_response_is_accepted():
  cfg = base()
  cfg["slots"].insert(0, {"name": "welcome", "source": "announce",
                          "response": [{"type": "text", "text": "Hello."}]})
  assert not has(errs(cfg), "requires 'message' or 'response'")


def test_an_announce_slot_must_not_carry_a_setter():
  cfg = base()
  cfg["slots"].insert(0, {"name": "welcome", "source": "announce",
                          "message": "Hello.", "setter": "set_welcome"})
  assert has(errs(cfg), "Announce slot 'welcome' must not have 'setter'")


def test_nothing_may_require_a_control_slot():
  cfg = base()
  slot(cfg, "acct")["requires"] = ["cancel"]
  assert has(errs(cfg), "Slot 'acct' requires 'cancel' — nothing may depend on"
                        " a control slot")


def test_a_slot_may_not_require_an_undeclared_slot():
  cfg = base()
  slot(cfg, "acct")["requires"] = ["ghost"]
  assert has(errs(cfg), "Slot 'acct' requires unknown 'ghost'")


def test_options_from_must_name_a_declared_slot():
  cfg = base()
  slot(cfg, "acct")["response"] = [
      {"type": "chips", "options_from": "ghost"}]
  assert has(errs(cfg),
             "Slot 'acct' response has options_from 'ghost' not in slots")


def test_options_from_is_found_inside_a_nested_response_part():
  cfg = base()
  slot(cfg, "acct")["response"] = [
      {"type": "chips", "data": {"inner": [{"options_from": "ghost"}]}}]
  assert has(errs(cfg), "options_from 'ghost' not in slots")


def test_options_from_naming_a_real_slot_is_accepted():
  cfg = base()
  slot(cfg, "acct")["response"] = [{"type": "chips", "options_from": "res"}]
  assert not has(errs(cfg), "options_from")


def test_options_from_a_slot_ordered_after_this_one():
  cfg = base()
  slot(cfg, "acct")["response"] = [{"type": "chips", "options_from": "res"}]
  cfg["slots"].append({"name": "later", "source": "user", "setter": "set_later",
                       "ask": "Later?", "hint": "later"})
  slot(cfg, "res")["requires"] = ["acct"]
  assert has(warns(cfg), "Slot 'acct' options_from 'res' but 'res' requires"
                         " 'acct' — options won't be filled yet")


# ══════════════════════════════════════════════════════════════════════════
# Announce dead config / empty strings / ask-vs-response
# ══════════════════════════════════════════════════════════════════════════

def _announce(**over):
  cfg = base()
  s = {"name": "welcome", "source": "announce", "message": "Hello."}
  s.update(over)
  cfg["slots"].insert(0, s)
  return cfg


def test_announce_slots_flag_every_field_that_does_nothing():
  cfg = _announce(ask="Ready?", readback_fmt="digits",
                  validation={"max_retries": 2}, scan_keywords=["hi"],
                  setter="set_welcome")
  w = warns(cfg)
  for field in ("ask", "readback_fmt", "validation", "scan_keywords", "setter"):
    assert has(w, f"Announce slot 'welcome' has '{field}'"), field


def test_a_plain_announce_slot_draws_none_of_those_warnings():
  assert not has(warns(_announce()), "Announce slot 'welcome' has")


def test_an_empty_user_facing_string_is_flagged():
  cfg = base()
  slot(cfg, "acct")["ask"] = "   "
  assert has(warns(cfg), "Slot 'acct' has empty 'ask' string")


def test_an_empty_task_completion_string_is_flagged():
  cfg = base()
  task(cfg, "Lookup")["then_say"] = ""
  assert has(warns(cfg), "Task 'Lookup' has empty 'then_say' string")


def test_an_ask_and_a_text_response_part_are_redundant():
  cfg = base()
  slot(cfg, "acct")["response"] = [{"type": "text", "text": "Account?"}]
  assert has(warns(cfg), "Slot 'acct' has both 'ask' and a text-type response")


def test_a_non_text_response_part_alongside_an_ask_is_fine():
  cfg = base()
  slot(cfg, "acct")["response"] = [{"type": "chips", "options_from": "res"}]
  assert not has(warns(cfg), "both 'ask' and a text-type response")


def test_an_ask_placeholder_that_is_not_required_may_render_empty():
  cfg = base()
  slot(cfg, "acct")["ask"] = "Is {res} right?"
  assert has(warns(cfg), "Slot 'acct' ask uses '{res}' but doesn't require 'res'")


def test_an_ask_placeholder_that_is_required_is_accepted():
  cfg = base()
  slot(cfg, "acct")["ask"] = "Is {res} right?"
  slot(cfg, "acct")["requires"] = ["res"]
  assert not has(warns(cfg), "placeholder may be empty")


# ══════════════════════════════════════════════════════════════════════════
# Format-string placeholders
# ══════════════════════════════════════════════════════════════════════════

def test_an_ask_placeholder_naming_nothing_at_all():
  cfg = base()
  slot(cfg, "acct")["ask"] = "Account for {ghost}?"
  assert has(warns(cfg), "Slot 'acct' ask references unknown placeholder"
                         " '{ghost}'")


def test_a_response_text_placeholder_naming_nothing():
  cfg = base()
  slot(cfg, "res")["response"] = [{"type": "text", "text": "Got {ghost}."}]
  assert has(warns(cfg), "Slot 'res' response references unknown placeholder"
                         " '{ghost}'")


def test_a_then_say_placeholder_naming_nothing():
  cfg = base()
  task(cfg, "Lookup")["then_say"] = "Balance is {ghost}."
  assert has(warns(cfg), "Task 'Lookup' then_say references unknown placeholder"
                         " '{ghost}'")


def test_a_then_response_placeholder_naming_nothing():
  cfg = base()
  t = task(cfg, "Lookup")
  t["then_response"] = [{"type": "text", "text": "Balance is {ghost}."}]
  assert has(warns(cfg), "Task 'Lookup' then_response references unknown"
                         " placeholder '{ghost}'")


def test_a_channel_override_placeholder_naming_nothing():
  cfg = base()
  slot(cfg, "acct")["channel_responses"] = {
      "voice": [{"type": "text", "text": "Account for {ghost}?"}]}
  assert has(warns(cfg), "Slot 'acct' channel_responses[voice] references"
                         " unknown placeholder '{ghost}'")


def test_a_placeholder_naming_a_slot_or_a_tool_result_key_is_accepted():
  cfg = base()
  task(cfg, "Lookup")["then_say"] = "{acct}: {result} ({success})"
  assert not has(warns(cfg), "unknown placeholder")


# ══════════════════════════════════════════════════════════════════════════
# Response parts and channel overrides
# ══════════════════════════════════════════════════════════════════════════

def test_a_response_must_be_a_list():
  cfg = base()
  slot(cfg, "res")["response"] = {"type": "text", "text": "Hi."}
  assert has(errs(cfg), "Slot 'res' response must be a list")


def test_a_response_part_must_be_a_dict():
  cfg = base()
  slot(cfg, "res")["response"] = ["Hi."]
  assert has(errs(cfg), "Slot 'res' response[0] must be a dict")


def test_a_response_part_must_carry_a_type():
  cfg = base()
  slot(cfg, "res")["response"] = [{"text": "Hi."}]
  assert has(errs(cfg), "Slot 'res' response[0] missing 'type'")


def test_a_non_standard_response_part_type_is_only_a_warning():
  cfg = base()
  slot(cfg, "res")["response"] = [{"type": "hologram"}]
  r = result(cfg)
  assert has(r.warnings, "Slot 'res' response[0] type 'hologram' not standard")


def test_a_payload_parts_data_must_be_a_dict():
  cfg = base()
  slot(cfg, "res")["response"] = [{"type": "payload", "data": ["x"]}]
  assert has(errs(cfg), "Slot 'res' response[0] payload 'data' must be a dict")


def test_an_audio_part_needs_an_audio_uri():
  cfg = base()
  slot(cfg, "res")["response"] = [{"type": "audio"}]
  assert has(errs(cfg),
             "Slot 'res' response[0] audio part requires a non-empty 'audioUri'")


def test_a_response_part_condition_is_validated():
  cfg = base()
  slot(cfg, "res")["response"] = [
      {"type": "text", "text": "Hi.", "condition": {"slot": "ghost",
                                                    "filled": True}}]
  assert has(errs(cfg), "response[0]: condition references unknown slot 'ghost'")


def test_a_response_part_lambda_condition_is_syntax_checked():
  cfg = base()
  slot(cfg, "res")["response"] = [
      {"type": "text", "text": "Hi.", "condition": "lambda f: f["}]
  assert has(errs(cfg), "response[0] condition syntax error")


def test_a_response_part_condition_of_an_impossible_type():
  cfg = base()
  slot(cfg, "res")["response"] = [{"type": "text", "text": "Hi.",
                                   "condition": 7}]
  assert has(errs(cfg), "response[0] condition must be dict, callable or"
                        " string, got int")


def test_a_valid_response_part_condition_is_accepted():
  cfg = base()
  slot(cfg, "res")["response"] = [
      {"type": "text", "text": "Hi.", "condition": {"slot": "acct",
                                                    "filled": True}}]
  assert errs(cfg) == []


def test_end_conversation_without_a_terminating_part_is_inert():
  cfg = base()
  s = slot(cfg, "res")
  s["end_conversation"] = True
  s["response"] = [{"type": "text", "text": "Bye."}]
  assert has(warns(cfg), "Slot 'res' has end_conversation but no"
                         " end_session/transfer part")


def test_end_conversation_with_an_end_session_part_is_accepted():
  cfg = base()
  s = slot(cfg, "res")
  s["end_conversation"] = True
  s["response"] = [{"type": "text", "text": "Bye."}, {"type": "end_session"}]
  assert not has(warns(cfg), "the flag is inert here")


def test_a_channel_override_must_be_a_dict_of_channel_to_parts():
  cfg = base()
  slot(cfg, "acct")["channel_responses"] = [{"type": "text", "text": "Hi."}]
  assert has(errs(cfg), "Slot 'acct' channel_responses must be a dict of"
                        " channel -> response parts, got list")


def test_a_channel_override_on_a_task_is_validated():
  cfg = base()
  task(cfg, "Lookup")["channel_then_response"] = {"voice": [{"text": "Hi."}]}
  assert has(errs(cfg),
             "Task 'Lookup' channel_then_response[voice] response[0] missing"
             " 'type'")


def test_a_channel_override_on_a_task_on_failure_is_validated():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {
      "channel_retry_response": {"voice": [{"text": "Hi."}]}}
  assert has(errs(cfg), "Task 'Lookup' on_failure channel_retry_response[voice]")


def test_the_config_level_channel_readback_override_is_validated():
  cfg = base()
  cfg["channel_readback_response"] = {"voice": [{"text": "Hi."}]}
  assert has(errs(cfg), "Config channel_readback_response[voice] response[0]"
                        " missing 'type'")


def test_a_channel_override_falling_back_to_the_base_response_is_accepted():
  cfg = base()
  slot(cfg, "acct")["channel_responses"] = {"voice": None}
  assert errs(cfg) == []


# ══════════════════════════════════════════════════════════════════════════
# Response text coverage — a turn nobody hears
# ══════════════════════════════════════════════════════════════════════════

def test_a_response_with_neither_text_nor_payload_is_silent():
  cfg = base()
  slot(cfg, "res")["response"] = [{"type": "chips"}]
  assert has(errs(cfg),
             "Slot 'res' response has neither text nor payload — turn will be"
             " silent")


def test_a_payload_only_response_warns_about_text_only_channels():
  cfg = base()
  slot(cfg, "res")["response"] = [{"type": "payload", "data": {"a": 1}}]
  r = result(cfg)
  assert has(r.warnings, "Slot 'res' response has no text")
  assert r.valid is True


def test_a_transfer_turn_is_allowed_to_be_wordless():
  cfg = base()
  slot(cfg, "res")["response"] = [{"type": "transfer"}]
  assert not has(errs(cfg), "turn will be silent")
  assert not has(warns(cfg), "response has no text")


def test_a_task_then_response_with_neither_text_nor_payload_is_silent():
  cfg = base()
  t = task(cfg, "Lookup")
  t.pop("then_say")
  t["then_response"] = [{"type": "chips"}]
  assert has(errs(cfg), "Task 'Lookup' then_response has neither text nor"
                        " payload")


def test_a_payload_only_task_response_warns():
  cfg = base()
  t = task(cfg, "Lookup")
  t.pop("then_say")
  t["then_response"] = [{"type": "payload", "data": {"a": 1}}]
  assert has(warns(cfg), "Task 'Lookup' then_response has no text")


def test_a_task_end_session_turn_is_allowed_to_be_wordless():
  cfg = base()
  t = task(cfg, "Lookup")
  t.pop("then_say")
  t["then_response"] = [{"type": "end_session"}]
  assert not has(errs(cfg), "then_response has neither text nor payload")


# ══════════════════════════════════════════════════════════════════════════
# Reachability — the rule that catches a slot or task nobody can ever get to
# ══════════════════════════════════════════════════════════════════════════

def test_a_slot_with_no_fill_mechanism_is_unreachable():
  cfg = base()
  cfg["slots"].append({"name": "orphan", "source": "user", "ask": "Which?",
                       "hint": "which"})
  r = result(cfg)
  assert has(r.errors, "Slot 'orphan' is unreachable")
  assert any(d["code"] == vdc.Codes.SLOT_UNREACHABLE for d in r.diagnostics)


def test_an_unreachable_slot_names_the_requirement_it_cannot_get():
  cfg = base()
  cfg["slots"].append({"name": "ghost_src", "source": "user", "ask": "?",
                       "hint": "?"})
  cfg["slots"].append({"name": "downstream", "source": "user", "ask": "?",
                       "hint": "?", "requires": ["ghost_src"]})
  assert has(errs(cfg),
             "Slot 'downstream' is unreachable: requires unfillable"
             " ['ghost_src']")


def test_a_requires_chain_that_does_resolve_is_reachable():
  cfg = base()
  cfg["slots"].append({"name": "downstream", "source": "user",
                       "setter": "set_down", "ask": "?", "hint": "?",
                       "requires": ["acct"]})
  assert not has(errs(cfg), "unreachable")


def test_a_cue_only_slot_is_a_reachable_root():
  # option_cues fill a slot deterministically with no setter at all.
  cfg = base()
  cfg["slots"].append({"name": "topic", "source": "user", "ask": "Which?",
                       "hint": "topic",
                       "option_cues": {"bill": ["billing"], "tech": ["outage"]}})
  assert not has(errs(cfg), "Slot 'topic' is unreachable")


def test_a_task_whose_inputs_can_never_fill_is_unreachable():
  cfg = base()
  cfg["slots"].append({"name": "orphan", "source": "user", "ask": "?",
                       "hint": "?"})
  cfg["tasks"].append({"name": "Later", "tool": "later_tool",
                       "inputs": ["orphan"], "then_say": "Done."})
  out = errs(cfg)
  assert has(out, "Task 'Later' is unreachable: unfillable inputs ['orphan']")


def test_an_unreachable_task_reports_its_unfillable_requires_too():
  cfg = base()
  cfg["slots"].append({"name": "orphan", "source": "user", "ask": "?",
                       "hint": "?"})
  cfg["tasks"].append({"name": "Later", "tool": "later_tool",
                       "requires": ["orphan"], "then_say": "Done."})
  assert has(errs(cfg), "Task 'Later' is unreachable: unfillable requires"
                        " ['orphan']")


def test_a_component_seeds_its_output_slots_as_reachable():
  # Without this a parent slot fed only by a child DAG reads as unreachable.
  cfg = base()
  cfg["slots"].append({"name": "verified", "source": "task:Verify"})
  cfg["tasks"].insert(0, {"name": "Verify", "component": "child",
                          "inputs": {"acct": "acct"},
                          "outputs": {"ok": "verified"}})
  assert not has(errs(cfg), "Slot 'verified' is unreachable")


def test_a_condition_may_not_read_a_slot_nothing_can_fill():
  cfg = base()
  cfg["slots"].append({"name": "orphan", "source": "user", "ask": "?",
                       "hint": "?"})
  slot(cfg, "res")["condition"] = {"slot": "orphan", "filled": True}
  assert has(errs(cfg), "orphan")


# ══════════════════════════════════════════════════════════════════════════
# Component task shape (single-config)
# ══════════════════════════════════════════════════════════════════════════

def _comp_task(**over):
  cfg = base()
  t = {"name": "Sub", "component": "child"}
  t.update(over)
  cfg["tasks"].insert(0, t)
  return cfg


def test_a_component_ref_must_be_a_non_empty_string():
  assert has(errs(_comp_task(component=7)),
             "Component task 'Sub' 'component' must be a non-empty string child"
             " config_id, got int")


def test_a_task_carries_a_tool_or_a_component_never_both():
  assert has(errs(_comp_task(tool="verify")),
             "Component task 'Sub' has both 'tool' and 'component'")


def test_component_on_abort_must_be_a_known_mode():
  assert has(errs(_comp_task(on_abort="retry")),
             "Component task 'Sub' on_abort 'retry' invalid")


def test_a_known_component_on_abort_is_accepted():
  assert not has(errs(_comp_task(on_abort="skip")), "on_abort")


def test_a_component_task_may_not_carry_a_forbidden_key():
  assert has(errs(_comp_task(success_check="ok")),
             "Component task 'Sub' must not have 'success_check'")


def test_a_component_task_may_not_be_terminal():
  assert has(errs(_comp_task(terminal=True)),
             "Component task 'Sub' must not be terminal=True")


def test_a_component_input_may_not_name_a_control_slot():
  assert has(errs(_comp_task(inputs={"cancel": "pin"})),
             "Component task 'Sub' input references reserved 'cancel' slot")


def test_a_component_input_must_name_a_parent_slot():
  assert has(errs(_comp_task(inputs={"ghost": "pin"})),
             "Component task 'Sub' input 'ghost' not in slots")


def test_a_component_output_may_not_target_a_control_slot():
  assert has(errs(_comp_task(outputs={"ok": "escalate"})),
             "Component task 'Sub' output targets reserved 'escalate' slot")


def test_a_component_output_must_target_a_parent_slot():
  assert has(errs(_comp_task(outputs={"ok": "ghost"})),
             "Component task 'Sub' output 'ghost' not in slots")


def test_a_well_wired_component_task_is_accepted():
  cfg = _comp_task(inputs={"acct": "pin"}, outputs={"ok": "res"})
  assert not has(errs(cfg), "Component task 'Sub'")


# ══════════════════════════════════════════════════════════════════════════
# Task wiring
# ══════════════════════════════════════════════════════════════════════════

def test_a_task_needs_a_tool():
  cfg = base()
  task(cfg, "Lookup").pop("tool")
  r = result(cfg)
  assert has(r.errors, "Task 'Lookup' has no 'tool' key")
  assert any(d["code"] == vdc.Codes.TASK_NO_TOOL for d in r.diagnostics)


def test_a_task_input_may_not_name_a_control_slot():
  cfg = base()
  task(cfg, "Lookup")["inputs"] = ["cancel"]
  assert has(errs(cfg), "Task 'Lookup' input references reserved 'cancel' slot")


def test_a_task_output_may_not_target_a_control_slot():
  cfg = base()
  task(cfg, "Lookup")["outputs"] = {"result": "escalate"}
  assert has(errs(cfg), "Task 'Lookup' output targets reserved 'escalate' slot")


def test_a_task_may_not_require_a_control_slot():
  cfg = base()
  task(cfg, "Lookup")["requires"] = ["escalate"]
  assert has(errs(cfg), "Task 'Lookup' requires 'escalate' — nothing may depend"
                        " on a control slot")


def test_a_task_declarative_condition_is_validated():
  cfg = base()
  task(cfg, "Lookup")["condition"] = {"slot": "ghost", "filled": True}
  assert has(errs(cfg), "Task 'Lookup': condition references unknown slot"
                        " 'ghost'")


def test_a_task_condition_of_an_impossible_type():
  cfg = base()
  task(cfg, "Lookup")["condition"] = 7
  assert has(errs(cfg),
             "Task 'Lookup' condition must be dict, callable or string, got int")


def test_a_callable_task_condition_is_accepted():
  cfg = base()
  task(cfg, "Lookup")["condition"] = lambda f: True
  assert errs(cfg) == []


def test_on_complete_flags_unknown_keys():
  cfg = base()
  task(cfg, "Lookup")["on_complete"] = {"clear_slot": ["acct"]}
  assert has(warns(cfg), "Task 'Lookup' on_complete has unknown keys")


def test_on_complete_transfer_to_must_be_a_string():
  cfg = base()
  task(cfg, "Lookup")["on_complete"] = {"transfer_to": ["Human"]}
  assert has(errs(cfg), "Task 'Lookup' on_complete.transfer_to must be a string")


def test_on_complete_auto_resume_deferred_must_be_a_bool():
  cfg = base()
  task(cfg, "Lookup")["on_complete"] = {"auto_resume_deferred": "yes"}
  assert has(errs(cfg),
             "Task 'Lookup' on_complete.auto_resume_deferred must be a bool")


def test_a_well_formed_on_complete_is_accepted():
  cfg = base()
  task(cfg, "Lookup")["on_complete"] = {"clear_slots": ["acct"],
                                        "transfer_to": "Human",
                                        "auto_resume_deferred": True}
  assert errs(cfg) == []


# ══════════════════════════════════════════════════════════════════════════
# awaits — the asynchronous-tool wait policy
# ══════════════════════════════════════════════════════════════════════════

def _awaits(value, **task_over):
  cfg = base()
  t = {"name": "Poll", "tool": "poll_tool", "awaits": value,
       "inputs": ["acct"], "outputs": {"status": "polled"},
       "then_say": "Done."}
  t.update(task_over)
  cfg["tasks"].insert(0, t)
  cfg["slots"].append({"name": "polled", "source": "task:Poll"})
  return cfg


def test_awaits_must_be_a_dict():
  assert has(errs(_awaits("30s")), "Task 'Poll' awaits must be a dict")


def test_awaits_requires_max_turns():
  assert has(errs(_awaits({"say": "One moment."})),
             "Task 'Poll' awaits requires 'max_turns'")


def test_awaits_max_turns_must_be_positive():
  assert has(errs(_awaits({"max_turns": 0, "on_timeout": {"then": "escalate"}})),
             "Task 'Poll' awaits.max_turns must be > 0")


def test_awaits_max_turns_may_be_an_integral_float():
  # Every config that round-trips through JSON deserializes 3 as 3.0.
  assert not has(errs(_awaits({"max_turns": 3.0,
                               "on_timeout": {"then": "escalate"}})),
                 "max_turns")


def test_a_terminal_task_may_not_await():
  cfg = _awaits({"max_turns": 3, "on_timeout": {"then": "escalate"}},
                terminal=True)
  assert has(errs(cfg), "Task 'Poll' is terminal and awaits")


def test_awaits_without_on_timeout_gives_up_silently():
  r = result(_awaits({"max_turns": 3}))
  assert has(r.warnings, "Task 'Poll' awaits without on_timeout")


def test_two_tasks_awaiting_one_tool_are_indistinguishable():
  cfg = _awaits({"max_turns": 3, "on_timeout": {"then": "escalate"}})
  cfg["tasks"].insert(1, {"name": "Poll2", "tool": "poll_tool",
                          "awaits": {"max_turns": 3,
                                     "on_timeout": {"then": "escalate"}},
                          "then_say": "Done."})
  assert has(warns(cfg), "Tasks 'Poll' and 'Poll2' both await tool 'poll_tool'")


def test_a_well_formed_awaits_is_accepted():
  cfg = _awaits({"max_turns": 3, "on_timeout": {"then": "escalate"},
                 "while_waiting": ["Still checking."]})
  assert errs(cfg) == []


# ══════════════════════════════════════════════════════════════════════════
# Parallel fan-out groups
# ══════════════════════════════════════════════════════════════════════════

def _parallel(*legs, **leg_over):
  cfg = base()
  built = []
  for i, extra in enumerate(legs):
    leg = {"name": f"Leg{i}", "tool": f"leg_tool_{i}", "parallel": "checks",
           "inputs": ["acct"], "outputs": {f"k{i}": f"out{i}"},
           "on_failure": {"max_retries": 2,
                          "on_exhaust": {"say": "One check did not run."}}}
    leg.update(extra)
    built.append(leg)
    cfg["slots"].append({"name": f"out{i}", "source": f"task:Leg{i}"})
  cfg["tasks"] = built + cfg["tasks"]
  cfg.update(leg_over)
  return cfg


def test_a_two_leg_parallel_group_is_clean():
  r = result(_parallel({}, {}))
  assert r.errors == []
  assert r.warnings == []


def test_a_parallel_group_name_must_be_a_string():
  cfg = base()
  task(cfg, "Lookup")["parallel"] = ["checks"]
  assert has(errs(cfg), "Task 'Lookup' parallel group must be a string, got list")


def test_a_parallel_group_of_one_is_a_plain_task():
  assert has(errs(_parallel({})), "Parallel group 'checks' has 1 leg")


def test_a_component_cannot_be_a_parallel_leg():
  cfg = _parallel({}, {"component": "child", "tool": None})
  cfg["tasks"][0].pop("tool")
  assert has(errs(cfg), "Task 'Leg1' is a component and a leg of parallel group")


def test_a_terminal_task_cannot_be_a_parallel_leg():
  assert has(errs(_parallel({}, {"terminal": True})),
             "Task 'Leg1' is terminal and a leg of parallel group 'checks'")


def test_a_parallel_leg_must_name_a_tool():
  cfg = _parallel({}, {})
  next(t for t in cfg["tasks"] if t["name"] == "Leg1").pop("tool")
  assert has(errs(cfg), "Parallel group 'checks' leg 'Leg1' names no tool")


def test_a_parallel_leg_naming_an_unregistered_tool_is_silent_and_fatal():
  cfg = _parallel({}, {})
  out = errs(cfg, available_tools=["set_acct", "lookup_tool", "leg_tool_0"])
  assert has(out, "leg 'Leg1' calls tool 'leg_tool_1', which is not in the"
                  " agent's tool list")


def test_two_legs_may_not_share_one_tool():
  cfg = _parallel({}, {"tool": "leg_tool_0"})
  assert has(errs(cfg), "legs 'Leg0' and 'Leg1' both call tool 'leg_tool_0'")


def test_a_leg_may_not_consume_a_siblings_output():
  cfg = _parallel({}, {"inputs": ["out0"]})
  assert has(errs(cfg), "leg 'Leg1' needs slot 'out0', which leg 'Leg0' of the"
                        " same group produces")


def test_only_one_leg_may_speak_a_filler():
  cfg = _parallel({"filler_say": "One moment."}, {"filler_say": "Checking."})
  assert has(errs(cfg), "legs 'Leg0' and 'Leg1' both declare 'filler_say'")


def test_only_one_leg_may_speak_a_holding_line():
  aw = {"max_turns": 3, "say": "Hold on.", "on_timeout": {"then": "escalate"}}
  cfg = _parallel({"awaits": dict(aw)}, {"awaits": dict(aw)})
  assert has(errs(cfg), "both declare a holding line")


def test_a_group_with_no_failure_line_leaves_the_caller_uninformed():
  cfg = _parallel({}, {})
  for t in cfg["tasks"]:
    t.pop("on_failure", None)
  assert has(warns(cfg), "No leg of parallel group 'checks' has an on_failure"
                         " line")


# ══════════════════════════════════════════════════════════════════════════
# Tool availability (only meaningful when the agent's tool list is supplied)
# ══════════════════════════════════════════════════════════════════════════

TOOLS = ["set_acct", "lookup_tool"]


def test_tools_are_not_checked_without_an_agent_tool_list():
  cfg = base()
  cfg["correction_tool"] = "never_registered"
  assert errs(cfg) == []


def test_a_missing_slot_setter_is_reported():
  assert has(errs(base(), available_tools=["lookup_tool"]),
             "Slot 'acct' setter 'set_acct' not in agent tool list")


def test_a_missing_task_tool_is_reported():
  assert has(errs(base(), available_tools=["set_acct"]),
             "Task 'Lookup' tool 'lookup_tool' not in agent tool list")


def test_a_missing_bootstrap_tool_is_reported():
  cfg = base()
  cfg["bootstrap"] = {"tool": "start_flow", "slot": "intent"}
  assert has(errs(cfg, available_tools=TOOLS),
             "Bootstrap tool 'start_flow' not in agent tool list")


def test_a_missing_correction_tool_is_reported():
  cfg = base()
  cfg["correction_tool"] = "fix_it"
  assert has(errs(cfg, available_tools=TOOLS),
             "correction_tool 'fix_it' not in agent tool list")


def test_a_missing_intent_change_tool_is_reported():
  cfg = base()
  cfg["intent_change"] = {"tool": "switch_intent"}
  assert has(errs(cfg, available_tools=TOOLS),
             "intent_change tool 'switch_intent' not in agent tool list")


def test_every_declared_tool_present_is_accepted():
  cfg = base()
  cfg["correction_tool"] = "fix_it"
  cfg["intent_change"] = {"tool": "switch_intent"}
  assert errs(cfg, available_tools=TOOLS + ["fix_it", "switch_intent"]) == []


def test_a_missing_exhaust_then_tool_is_reported():
  cfg = base()
  cfg["steer_back"] = {"on_exhaust": {"then": "hand_off"}}
  assert has(errs(cfg, available_tools=TOOLS),
             "steer_back.on_exhaust.then tool 'hand_off' not in agent tool list")


def test_an_engine_disposition_is_never_a_missing_tool():
  cfg = base()
  cfg["steer_back"] = {"on_exhaust": {"then": "escalate"}}
  assert errs(cfg, available_tools=TOOLS) == []


# ══════════════════════════════════════════════════════════════════════════
# Setter / tool source agreement
# ══════════════════════════════════════════════════════════════════════════

MULTI_SETTER_SRC = '''
def set_profile(first, last):
    values = {}
    values["first"] = first
    return {"values": values}
'''

SIMPLE_SETTER_SRC = '''
def set_acct(value):
    return {"ok": True}
'''

TOOL_SRC = '''
def lookup_tool(acct):
    return {"success": True, "result": "ok"}
'''

DYNAMIC_TOOL_SRC = '''
def lookup_tool(acct):
    out = {"success": True}
    out.update(whatever)
    return out
'''


def test_a_setter_field_the_setter_never_writes():
  cfg = base()
  cfg["slots"] = [
      {"name": "first", "source": "user", "setter": "set_profile",
       "setter_field": "first", "ask": "First name?", "hint": "first name"},
      {"name": "last", "source": "user", "setter": "set_profile",
       "setter_field": "last", "ask": "Last name?", "hint": "last name"},
  ]
  cfg["tasks"] = [{"name": "Done", "tool": "finish", "terminal": True,
                   "then_say": "Thanks."}]
  out = errs(cfg, setter_sources={"set_profile": MULTI_SETTER_SRC})
  assert has(out, "Setter 'set_profile' config expects setter_field 'last' but"
                  " source code never writes values[\"last\"]")
  assert not has(out, "setter_field 'first'")


def test_a_simple_setter_that_may_not_return_a_value_key():
  r = result(base(), setter_sources={"set_acct": SIMPLE_SETTER_SRC})
  assert has(r.warnings, "Setter 'set_acct' may not return a 'value' key")


def test_a_simple_setter_that_does_return_value_is_accepted():
  src = "def set_acct(value):\n    return {'value': value}\n"
  assert warns(base(), setter_sources={"set_acct": src}) == []


def test_a_task_output_key_the_tool_never_returns():
  cfg = base()
  task(cfg, "Lookup")["outputs"] = {"balance": "res"}
  out = errs(cfg, task_tool_sources={"lookup_tool": TOOL_SRC})
  assert has(out, "Task 'Lookup' expects output key 'balance' but tool"
                  " 'lookup_tool' never returns it")


def test_a_task_output_key_the_tool_does_return_is_accepted():
  assert errs(base(), task_tool_sources={"lookup_tool": TOOL_SRC}) == []


def test_a_remote_tasks_outputs_may_come_from_its_status_tool():
  status_src = "def poll_job(job):\n    return {'balance': 1}\n"
  cfg = base()
  t = task(cfg, "Lookup")
  t["outputs"] = {"balance": "res"}
  cfg["remote_tools"] = {"lookup_tool": {"status_tool": "poll_job",
                                         "job_slot": "acct"}}
  out = errs(cfg, task_tool_sources={"lookup_tool": TOOL_SRC,
                                     "poll_job": status_src})
  assert not has(out, "never returns it")


def test_a_completion_placeholder_naming_nothing_the_tool_returns():
  cfg = base()
  task(cfg, "Lookup")["then_say"] = "Balance is {balance}."
  out = errs(cfg, task_tool_sources={"lookup_tool": TOOL_SRC})
  assert has(out, "Task 'Lookup' completion text references '{balance}' but"
                  " root 'balance' is not a slot, an outputs value, or a key"
                  " tool 'lookup_tool' returns")


def test_a_completion_placeholder_the_tool_does_return_is_accepted():
  cfg = base()
  task(cfg, "Lookup")["then_say"] = "Result is {result}."
  assert errs(cfg, task_tool_sources={"lookup_tool": TOOL_SRC}) == []


def test_a_dynamically_built_return_dict_is_not_second_guessed():
  # The keys cannot be known statically, so guessing would false-positive on a
  # perfectly good tool.
  cfg = base()
  task(cfg, "Lookup")["then_say"] = "Balance is {balance}."
  out = errs(cfg, task_tool_sources={"lookup_tool": DYNAMIC_TOOL_SRC})
  assert not has(out, "completion text references")


def test_a_then_response_placeholder_is_checked_against_the_tool_too():
  cfg = base()
  t = task(cfg, "Lookup")
  t.pop("then_say")
  t["then_response"] = [{"type": "text", "text": "Balance is {balance}."}]
  out = errs(cfg, task_tool_sources={"lookup_tool": TOOL_SRC})
  assert has(out, "completion text references '{balance}'")


# ══════════════════════════════════════════════════════════════════════════
# route_cues — the deterministic routing backstop
# ══════════════════════════════════════════════════════════════════════════

def test_route_cues_must_be_a_dict():
  cfg = base()
  cfg["route_cues"] = ["billing"]
  assert has(errs(cfg), "'route_cues' must be a dict {flow: [cue, ...]}")


def test_a_route_cues_flow_key_must_be_a_non_empty_string():
  cfg = base()
  cfg["route_cues"] = {"  ": ["bill"]}
  assert has(errs(cfg), "route_cues flow key must be a non-empty string")


def test_route_cues_must_be_a_list_not_a_bare_string():
  # A bare string is iterated per CHARACTER, so it mis-routes on single letters.
  cfg = base()
  cfg["route_cues"] = {"billing": "bill"}
  assert has(errs(cfg),
             "route_cues['billing'] must be a list of cue strings, got str")


def test_route_cue_entries_must_be_non_empty_strings():
  cfg = base()
  cfg["route_cues"] = {"billing": ["bill", ""]}
  assert has(errs(cfg),
             "route_cues['billing'] entries must be non-empty strings")


def test_a_route_cues_flow_outside_flow_types_loses_its_backstop():
  cfg = base()
  cfg["flow_types"] = ["billing"]
  cfg["route_cues"] = {"tech": ["outage"]}
  r = result(cfg)
  assert has(r.warnings, "route_cues flow 'tech' not in flow_types")
  assert r.valid is True


def test_a_router_with_two_flows_and_no_cues_is_model_only():
  cfg = base()
  cfg["router"] = True
  cfg["flow_types"] = ["billing", "tech"]
  assert has(warns(cfg), "router with >=2 flow_types has no 'route_cues'")


def test_a_router_with_cues_draws_no_model_only_warning():
  cfg = base()
  cfg["router"] = True
  cfg["flow_types"] = ["billing", "tech"]
  cfg["route_cues"] = {"billing": ["bill"], "tech": ["outage"]}
  assert warns(cfg) == []


# ══════════════════════════════════════════════════════════════════════════
# shared_slots, dtmf_map, option_cues and their twins
# ══════════════════════════════════════════════════════════════════════════

def test_shared_slots_must_name_declared_slots():
  cfg = base()
  cfg["shared_slots"] = ["ghost"]
  assert has(errs(cfg), "shared_slots lists 'ghost' which is not a defined slot")


def test_a_shared_slots_entry_without_per_slot_shared_true_is_inert():
  cfg = base()
  cfg["shared_slots"] = ["acct"]
  r = result(cfg)
  assert has(r.warnings, "shared_slots lists 'acct' but slot lacks shared:true")
  assert r.valid is True


def test_a_properly_marked_shared_slot_is_accepted():
  cfg = base()
  cfg["shared_slots"] = ["acct"]
  slot(cfg, "acct")["shared"] = True
  assert errs(cfg) == [] and warns(cfg) == []


def _dtmf(**over):
  cfg = base()
  s = slot(cfg, "acct")
  s.update(over)
  return cfg


def test_a_dtmf_map_must_be_a_dict():
  assert has(errs(_dtmf(dtmf_map=["1"])),
             "Slot 'acct' dtmf_map must be a dict, got list")


def test_a_dtmf_value_outside_the_enum_lands_an_invalid_value():
  cfg = _dtmf(validation_rules=[{"kind": "enum", "detail": "gold|silver"}],
              dtmf_map={"1": "bronze"})
  assert has(errs(cfg),
             "Slot 'acct' dtmf_map['1'] value 'bronze' is not an enum option")


def test_a_dtmf_map_inside_the_enum_is_accepted():
  cfg = _dtmf(validation_rules=[{"kind": "enum", "detail": "gold|silver"}],
              dtmf_map={"1": "gold", "2": "silver"},
              option_cues={"gold": ["gold"], "silver": ["silver"]})
  assert errs(cfg) == []


def test_a_dtmf_map_on_a_non_user_slot_is_dead_config():
  cfg = base()
  slot(cfg, "res")["dtmf_map"] = {"1": "yes"}
  assert has(warns(cfg), "Slot 'res' has dtmf_map but source is not 'user'")


def test_option_cues_must_be_a_dict():
  assert has(errs(_dtmf(option_cues=["gold"])),
             "Slot 'acct' option_cues must be a dict, got list")


def test_option_cue_patterns_must_be_a_list_not_a_bare_string():
  assert has(errs(_dtmf(option_cues={"gold": "gold card"})),
             "Slot 'acct' option_cues['gold'] must be a list of strings (a bare"
             " string is char-iterated as regexes)")


def test_an_off_enum_option_cue_key_is_a_warning():
  cfg = _dtmf(validation_rules=[{"kind": "enum", "detail": "gold|silver"}],
              option_cues={"bronze": ["bronze"]})
  r = result(cfg)
  assert has(r.warnings, "Slot 'acct' option_cues key 'bronze' is not an enum"
                         " option")
  assert r.valid is True


def test_option_cues_on_a_non_user_slot_only_fill_user_slots():
  cfg = base()
  slot(cfg, "res")["option_cues"] = {"yes": ["yes"]}
  assert has(warns(cfg), "Slot 'res' has option_cues but source is not 'user'")


def test_the_keypad_and_text_twins_must_select_the_same_values():
  cfg = _dtmf(dtmf_map={"1": "gold"},
              option_cues={"gold": ["gold"], "silver": ["silver"]})
  assert has(warns(cfg), "Slot 'acct' dtmf_map values and option_cues keys"
                         " diverge: ['silver']")


def test_matching_twins_draw_no_warning():
  cfg = _dtmf(dtmf_map={"1": "gold"}, option_cues={"gold": ["gold"]})
  assert warns(cfg) == []


def test_switchable_must_be_true_or_defer():
  cfg = _dtmf(switchable="always", option_cues={"gold": ["gold"]}, kind="intent",
              validation_rules=[{"kind": "enum", "detail": "gold"}])
  assert has(errs(cfg), "Slot 'acct' switchable must be true or 'defer'")


def test_switchable_without_option_cues_can_never_match():
  assert has(errs(_dtmf(switchable=True)),
             "Slot 'acct' sets 'switchable' but declares no 'option_cues'")


def test_switchable_on_a_non_intent_slot_is_a_warning():
  cfg = _dtmf(switchable=True, option_cues={"gold": ["gold"]})
  r = result(cfg)
  assert has(r.warnings, "Slot 'acct' sets 'switchable' but is not kind:'intent'")
  assert r.valid is True


def test_cue_priority_must_be_a_known_mode():
  assert has(errs(_dtmf(cue_priority="last", option_cues={"gold": ["gold"]})),
             "Slot 'acct' cue_priority must be 'unique' or 'first'")


def test_cue_priority_without_option_cues_has_no_effect():
  assert has(warns(_dtmf(cue_priority="first")),
             "Slot 'acct' sets cue_priority but has no option_cues")


def test_cue_priority_with_option_cues_is_accepted():
  assert warns(_dtmf(cue_priority="first",
                     option_cues={"gold": ["gold"]})) == []


# ══════════════════════════════════════════════════════════════════════════
# cancel menu-return, value policy, numeric / enum condition sanity
# ══════════════════════════════════════════════════════════════════════════

def test_a_menu_returning_cancel_must_clear_something():
  cfg = base()
  cfg["cancel"] = {"end_conversation": False, "say": "Okay."}
  assert has(errs(cfg), "'cancel' sets end_conversation: false but lists no"
                        " 'clear_slots'")


def test_a_menu_returning_cancel_must_clear_real_slots():
  cfg = base()
  cfg["cancel"] = {"end_conversation": False, "clear_slots": ["ghost"]}
  assert has(errs(cfg), "'cancel.clear_slots' names 'ghost', which is not a"
                        " slot in this flow")


def test_a_menu_returning_cancel_that_clears_a_real_slot_is_accepted():
  cfg = base()
  cfg["cancel"] = {"end_conversation": False, "clear_slots": ["acct"]}
  assert not has(errs(cfg), "clear_slots")


def test_a_default_must_be_a_list_of_fallbacks():
  cfg = base()
  slot(cfg, "res")["default"] = "unknown"
  assert has(errs(cfg), "slot 'res': 'default' must be a LIST of")


def test_a_user_filled_slot_may_not_have_a_default():
  cfg = base()
  slot(cfg, "acct")["default"] = [{"value": "0000"}]
  assert has(errs(cfg), "slot 'acct': a user-filled slot cannot have a 'default'")


def test_reject_and_publish_must_be_lists():
  cfg = base()
  slot(cfg, "res")["reject"] = "none"
  slot(cfg, "res")["publish"] = "none"
  out = errs(cfg)
  assert has(out, "slot 'res': 'reject' must be a list of strings")
  assert has(out, "slot 'res': 'publish' must be a list of strings")


def test_a_default_no_branch_accepts_is_a_dead_end():
  cfg = base()
  slot(cfg, "res")["default"] = [{"value": "unknown"}]
  task(cfg, "Lookup")["condition"] = {"slot": "res", "eq": "ok"}
  assert has(warns(cfg), "slot 'res': defaults to 'unknown', which none of the"
                         " branches reading it accept")


def test_a_default_a_branch_does_accept_is_quiet():
  cfg = base()
  slot(cfg, "res")["default"] = [{"value": "unknown"}]
  task(cfg, "Lookup")["condition"] = {"slot": "res", "in": ["ok", "unknown"]}
  assert not has(warns(cfg), "none of the branches reading it accept")


def test_a_numeric_comparison_on_an_intent_slot_fails_the_gate_open():
  cfg = base()
  cfg["slots"].append({"name": "intent", "source": "user", "setter": "set_int",
                       "ask": "Which?", "hint": "topic", "kind": "intent",
                       "option_cues": {"bill": ["bill"], "tech": ["tech"]},
                       "validation_rules": [{"kind": "enum",
                                             "detail": "bill|tech"}]})
  task(cfg, "Lookup")["condition"] = {"slot": "intent", "gte": 1}
  assert has(errs(cfg), "Task 'Lookup' condition uses a numeric comparison"
                        " (gt/lt/gte/lte) on non-numeric slot 'intent'")


def test_a_numeric_comparison_on_a_date_slot_is_rejected():
  cfg = base()
  slot(cfg, "acct")["validation_rules"] = [{"kind": "date_format",
                                            "detail": "%Y-%m-%d"}]
  slot(cfg, "res")["condition"] = {"slot": "acct", "lt": 5}
  assert has(errs(cfg), "numeric comparison (gt/lt/gte/lte) on non-numeric slot"
                        " 'acct'")


def test_a_numeric_comparison_on_a_digit_slot_is_accepted():
  # length_digits slots hold digit strings, which int() reads fine.
  cfg = base()
  slot(cfg, "acct")["validation_rules"] = [{"kind": "length_digits",
                                            "detail": "8"}]
  slot(cfg, "res")["condition"] = {"slot": "acct", "gte": 5}
  assert not has(errs(cfg), "numeric comparison")


def test_a_numeric_comparison_on_a_numeric_enum_is_accepted():
  cfg = base()
  slot(cfg, "acct")["validation_rules"] = [{"kind": "enum", "detail": "1|2|3"}]
  slot(cfg, "res")["condition"] = {"slot": "acct", "gte": 2}
  assert not has(errs(cfg), "numeric comparison")


def test_an_off_enum_equality_on_a_plain_slot_is_a_warning():
  cfg = base()
  slot(cfg, "acct")["validation_rules"] = [{"kind": "enum",
                                            "detail": "gold|silver"}]
  slot(cfg, "res")["condition"] = {"slot": "acct", "eq": "GOLD"}
  r = result(cfg)
  assert has(r.warnings, "Slot 'res' condition eq='GOLD' on slot 'acct' is not"
                         " one of its enum options")
  assert r.valid is True


def test_an_off_enum_equality_on_an_intent_slot_is_an_error():
  # There the enum IS the authoritative closed set, so the branch is dead.
  cfg = base()
  s = slot(cfg, "acct")
  s["kind"] = "intent"
  s["option_cues"] = {"gold": ["gold"], "silver": ["silver"]}
  s["validation_rules"] = [{"kind": "enum", "detail": "gold|silver"}]
  slot(cfg, "res")["condition"] = {"slot": "acct", "eq": "GOLD"}
  assert has(errs(cfg), "condition eq='GOLD' on slot 'acct' is not one of its"
                        " enum options")


def test_an_off_enum_in_member_is_reported():
  cfg = base()
  slot(cfg, "acct")["validation_rules"] = [{"kind": "enum",
                                            "detail": "gold|silver"}]
  task(cfg, "Lookup")["condition"] = {"slot": "acct", "in": ["gold", "bronze"]}
  assert has(warns(cfg), "Task 'Lookup' condition in='bronze'")


def test_an_on_enum_equality_is_accepted():
  cfg = base()
  slot(cfg, "acct")["validation_rules"] = [{"kind": "enum",
                                            "detail": "gold|silver"}]
  slot(cfg, "res")["condition"] = {"slot": "acct", "eq": "gold"}
  slot(cfg, "res")["requires"] = ["acct"]
  assert warns(cfg) == []


# ══════════════════════════════════════════════════════════════════════════
# Contradictions and tautologies in `all` combinators
# ══════════════════════════════════════════════════════════════════════════

def contradictions(spec):
  return vdc._find_contradictions(spec)


def test_a_slot_cannot_equal_two_values_at_once():
  out = contradictions({"all": [{"slot": "a", "eq": "x"},
                                {"slot": "a", "eq": "y"}]})
  assert has(out, "Contradictory: slot 'a' cannot equal both 'x' and 'y'")


def test_eq_and_neq_of_the_same_value_contradict():
  out = contradictions({"all": [{"slot": "a", "eq": "x"},
                                {"slot": "a", "neq": "x"}]})
  assert has(out, "Contradictory: slot 'a' eq='x' and neq='x'")


def test_filled_true_and_filled_false_contradict():
  out = contradictions({"all": [{"slot": "a", "filled": True},
                                {"slot": "a", "filled": False}]})
  assert has(out, "Contradictory: slot 'a' filled=True and filled=False")


def test_an_in_set_fully_excluded_by_not_in():
  out = contradictions({"all": [{"slot": "a", "in": ["x", "y"]},
                                {"slot": "a", "not_in": ["x", "y", "z"]}]})
  assert has(out, "Contradictory: slot 'a' in-set fully excluded by not_in")


def test_an_eq_outside_its_sibling_in_set():
  out = contradictions({"all": [{"slot": "a", "eq": "z"},
                                {"slot": "a", "in": ["x", "y"]}]})
  assert has(out, "Contradictory: slot 'a' eq='z' is not in its 'in' set")


def test_an_eq_excluded_by_a_sibling_not_in():
  out = contradictions({"all": [{"slot": "a", "eq": "x"},
                                {"slot": "a", "not_in": ["x"]}]})
  assert has(out, "Contradictory: slot 'a' eq='x' is excluded by 'not_in'")


def test_an_empty_numeric_range():
  out = contradictions({"all": [{"slot": "a", "gte": 10},
                                {"slot": "a", "lt": 5}]})
  assert has(out, "Contradictory: slot 'a' numeric range is empty (lower bound"
                  " 10 vs upper bound 5)")


def test_an_exclusive_bound_meeting_at_one_point_is_empty():
  out = contradictions({"all": [{"slot": "a", "gte": 5},
                                {"slot": "a", "lt": 5}]})
  assert has(out, "numeric range is empty")


def test_an_inclusive_bound_meeting_at_one_point_is_satisfiable():
  assert contradictions({"all": [{"slot": "a", "gte": 5},
                                 {"slot": "a", "lte": 5}]}) == []


def test_a_satisfiable_numeric_range_is_quiet():
  assert contradictions({"all": [{"slot": "a", "gte": 1},
                                 {"slot": "a", "lte": 10}]}) == []


def test_contradictions_are_found_inside_any_and_not():
  nested = {"all": [{"slot": "a", "eq": "x"}, {"slot": "a", "eq": "y"}]}
  assert has(contradictions({"any": [nested, {"slot": "b", "filled": True}]}),
             "cannot equal both")
  assert has(contradictions({"not": nested}), "cannot equal both")


def test_a_contradictory_task_condition_is_reported_by_the_validator():
  cfg = base()
  task(cfg, "Lookup")["condition"] = {
      "all": [{"slot": "acct", "eq": "x"}, {"slot": "acct", "eq": "y"}]}
  assert has(errs(cfg), "cannot equal both")


def test_a_tautological_gate_is_a_warning():
  cfg = base()
  slot(cfg, "acct")["validation_rules"] = [{"kind": "enum",
                                            "detail": "gold|silver"}]
  task(cfg, "Lookup")["condition"] = {
      "any": [{"slot": "acct", "eq": "gold"}, {"slot": "acct", "neq": "gold"}]}
  r = result(cfg)
  assert r.warnings
  assert r.valid is True


# ══════════════════════════════════════════════════════════════════════════
# speech — the improvisation policy
# ══════════════════════════════════════════════════════════════════════════

def test_speech_must_be_a_dict():
  cfg = base()
  cfg["speech"] = ["recovery"]
  assert has(errs(cfg), "'speech' must be a dict")


def test_speech_flags_unknown_keys():
  cfg = base()
  cfg["speech"] = {"improvize": ["recovery"]}
  assert has(errs(cfg), "speech has unknown keys: ['improvize']")


def test_speech_improvise_style_must_be_a_string():
  cfg = base()
  cfg["speech"] = {"improvise_style": ["warm"]}
  assert has(errs(cfg), "speech.improvise_style must be a string")


def test_a_style_with_no_classes_improvises_nothing():
  cfg = base()
  cfg["speech"] = {"improvise_style": "warm"}
  assert has(errs(cfg), "speech.improvise_style is set but speech.improvise"
                        " names no classes")


def test_speech_improvise_must_be_a_list():
  cfg = base()
  cfg["speech"] = {"improvise": "recovery"}
  assert has(errs(cfg), "speech.improvise must be a list")


def test_a_speech_class_must_be_a_string():
  cfg = base()
  cfg["speech"] = {"improvise": [7]}
  assert has(errs(cfg), "speech.improvise[0] must be a string")


def test_an_unknown_speech_class_is_rejected():
  cfg = base()
  cfg["speech"] = {"improvise": ["singing"]}
  assert has(errs(cfg), "speech.improvise[0] is not a known class")


def test_improvising_fillers_with_no_filler_to_reword():
  cfg = base()
  cfg["speech"] = {"improvise": ["filler"]}
  assert has(warns(cfg), "speech.improvise includes 'filler' but no task or slot"
                         " has a `filler_say`")


def test_improvising_fillers_with_a_filler_present_is_quiet():
  cfg = base()
  cfg["speech"] = {"improvise": ["filler"]}
  task(cfg, "Lookup")["filler_say"] = "One moment."
  assert not has(warns(cfg), "no holding line for the model to reword")


def test_improvising_control_with_no_declined_say_reaches_nothing():
  cfg = base()
  cfg["speech"] = {"improvise": ["control"]}
  cfg["escalate"] = {"say": "One moment."}
  assert has(warns(cfg), "speech.improvise includes 'control' but no control"
                         " block has a `declined_say`")


def test_improvising_control_with_a_declined_say_is_quiet():
  cfg = base()
  cfg["speech"] = {"improvise": ["control"]}
  cfg["escalate"] = {"say": "One moment.",
                     "condition": {"slot": "acct", "filled": True},
                     "declined_say": "Let me try first."}
  assert not has(warns(cfg), "is the only line the class reaches")


# ══════════════════════════════════════════════════════════════════════════
# Intent slots
# ══════════════════════════════════════════════════════════════════════════

def intent_base(**over):
  cfg = base()
  s = {"name": "intent", "source": "user", "setter": "set_intent",
       "ask": "How can I help?", "hint": "topic", "kind": "intent",
       "option_cues": {"billing": ["bill"], "tech": ["outage"]},
       "validation_rules": [{"kind": "enum", "detail": "billing|tech"}]}
  s.update(over)
  cfg["slots"].insert(0, s)
  return cfg


def test_the_intent_slot_fixture_is_clean():
  r = result(intent_base())
  assert r.errors == []
  assert r.warnings == []


def test_an_intent_slot_must_have_option_cues():
  cfg = intent_base()
  slot(cfg, "intent").pop("option_cues")
  assert has(errs(cfg), "Intent slot 'intent' must have non-empty option_cues")


def test_an_intent_slot_must_have_an_enum_rule():
  cfg = intent_base()
  slot(cfg, "intent").pop("validation_rules")
  assert has(errs(cfg), "Intent slot 'intent' must have an enum validation_rule")


def test_an_intent_cue_key_must_be_an_enum_option():
  cfg = intent_base(option_cues={"billing": ["bill"], "sales": ["buy"]})
  assert has(errs(cfg),
             "Intent slot 'intent' option_cues key 'sales' is not an enum option")


def test_an_enum_option_with_no_cue_can_only_be_picked_by_the_model():
  cfg = intent_base(option_cues={"billing": ["bill"]})
  r = result(cfg)
  assert has(r.warnings, "Intent slot 'intent' enum option 'tech' has no"
                         " option_cues entry")
  assert r.valid is True


def test_a_passive_intent_slot_is_exempt_from_the_no_cue_warning():
  cfg = intent_base(option_cues={"billing": ["bill"]}, passive=True)
  cfg["slots"][0].pop("ask")
  assert not has(warns(cfg), "has no option_cues entry")


def test_an_intent_slot_may_not_carry_a_numeric_rule():
  cfg = intent_base(validation_rules=[
      {"kind": "enum", "detail": "billing|tech"},
      {"kind": "length_digits", "detail": "4"}])
  assert has(errs(cfg), "Intent slot 'intent' must not carry a numeric/length"
                        " rule ['length_digits']")


def test_an_unasked_intent_slot_must_be_passive():
  cfg = intent_base()
  slot(cfg, "intent").pop("ask")
  assert has(errs(cfg), "Intent slot 'intent' has no `ask` but is not `passive`")


def test_a_passive_intent_slot_may_not_surface_its_enum_to_the_caller():
  cfg = intent_base(passive=True,
                    validation={"errors": {"not_in_enum": "Pick one of..."}})
  cfg["slots"][0].pop("ask")
  assert has(errs(cfg), "Passive intent slot 'intent' declares a caller-facing"
                        " 'not_in_enum' error")


# ══════════════════════════════════════════════════════════════════════════
# readback_inputs / top-level structure
# ══════════════════════════════════════════════════════════════════════════

def test_readback_inputs_with_nothing_to_confirm():
  cfg = base()
  task(cfg, "Lookup")["readback_inputs"] = True
  assert has(warns(cfg), "Task 'Lookup' has readback_inputs but none of its"
                         " input slots have requires_readback")


def test_readback_inputs_over_a_confirmed_slot_is_accepted():
  cfg = base()
  task(cfg, "Lookup")["readback_inputs"] = True
  slot(cfg, "acct")["requires_readback"] = True
  assert not has(warns(cfg), "confirmation will be skipped")


def test_slots_must_be_a_list():
  assert has(errs({"slots": {"acct": {}}, "tasks": []}),
             "Config 'slots' must be a list, got dict")


def test_a_slot_entry_must_be_a_dict():
  assert has(errs({"slots": ["acct"], "tasks": []}),
             "Slot at index 0 must be a dict, got str")


def test_a_task_entry_must_be_a_dict():
  cfg = base()
  cfg["tasks"] = ["Lookup"]
  assert has(errs(cfg), "Task at index 0 must be a dict, got str")


def test_a_config_with_neither_slots_nor_tasks():
  assert has(errs({}), "Config has no 'slots' and no 'tasks'")


def test_a_router_config_may_legitimately_have_neither():
  assert not has(errs({"router": True, "flow_types": ["billing"]}),
                 "Config has no")


# ══════════════════════════════════════════════════════════════════════════
# Tautological `any` gates — an inert gate that reads as protection
# ══════════════════════════════════════════════════════════════════════════

def tautologies(spec, slot_map=None):
  return vdc._find_tautologies(spec, slot_map or {})


def test_filled_true_or_filled_false_covers_everything():
  assert has(tautologies({"any": [{"slot": "a", "filled": True},
                                  {"slot": "a", "filled": False}]}),
             "Tautological 'any': slot 'a' filled=True and filled=False cover"
             " all cases")


def test_eq_or_neq_of_the_same_value_is_always_true():
  assert has(tautologies({"any": [{"slot": "a", "eq": "x"},
                                  {"slot": "a", "neq": "x"}]}),
             "Tautological 'any': slot 'a' eq='x' and neq='x' together are"
             " always true")


def test_a_neq_outside_the_enum_is_always_true():
  slot_map = {"a": {"validation_rules": [{"kind": "enum",
                                          "detail": "gold|silver"}]}}
  assert has(tautologies({"any": [{"slot": "a", "neq": "bronze"},
                                  {"slot": "b", "filled": True}]}, slot_map),
             "Tautological 'any': slot 'a' neq='bronze' is outside its enum")


def test_a_not_in_excluding_only_off_enum_values_is_always_true():
  slot_map = {"a": {"validation_rules": [{"kind": "enum",
                                          "detail": "gold|silver"}]}}
  assert has(tautologies({"any": [{"slot": "a", "not_in": ["bronze"]},
                                  {"slot": "b", "filled": True}]}, slot_map),
             "Tautological 'any': slot 'a' not_in excludes only values outside"
             " its enum")


def test_a_neq_inside_the_enum_can_actually_be_false():
  slot_map = {"a": {"validation_rules": [{"kind": "enum",
                                          "detail": "gold|silver"}]}}
  assert tautologies({"any": [{"slot": "a", "neq": "gold"},
                              {"slot": "b", "filled": True}]}, slot_map) == []


def test_tautologies_are_found_inside_all_and_not():
  taut = {"any": [{"slot": "a", "filled": True}, {"slot": "a", "filled": False}]}
  assert has(tautologies({"all": [taut, {"slot": "b", "filled": True}]}),
             "cover all cases")
  assert has(tautologies({"not": taut}), "cover all cases")


def test_a_tautological_slot_gate_is_reported_against_the_slot():
  cfg = base()
  slot(cfg, "res")["condition"] = {
      "any": [{"slot": "acct", "filled": True}, {"slot": "acct", "filled": False}]}
  assert has(warns(cfg), "Slot 'res': Tautological 'any'")


def test_a_tautological_task_gate_is_reported_against_the_task():
  cfg = base()
  task(cfg, "Lookup")["condition"] = {
      "any": [{"slot": "acct", "filled": True}, {"slot": "acct", "filled": False}]}
  assert has(warns(cfg), "Task 'Lookup': Tautological 'any'")


# ══════════════════════════════════════════════════════════════════════════
# Structural hygiene: names, keys, bool fields, duplicate wiring
# ══════════════════════════════════════════════════════════════════════════

def test_a_slot_with_no_name_is_reported_by_index():
  cfg = base()
  cfg["slots"].append({"source": "user", "setter": "set_x", "ask": "?",
                       "hint": "?"})
  r = result(cfg)
  assert has(r.errors, "Slot at index 2 has no 'name'")
  assert any(d["code"] == vdc.Codes.SLOT_MISSING_NAME for d in r.diagnostics)


def test_a_task_with_no_name_is_reported_by_index():
  cfg = base()
  cfg["tasks"].append({"tool": "other_tool"})
  assert has(errs(cfg), "Task at index 1 has no 'name'")


def test_a_task_with_unknown_keys_is_reported():
  cfg = base()
  task(cfg, "Lookup")["input"] = ["acct"]
  r = result(cfg)
  assert has(r.errors, "Task 'Lookup' has unknown keys: ['input']")
  assert any(d["code"] == vdc.Codes.UNKNOWN_TASK_KEY for d in r.diagnostics)


def test_a_truthy_non_bool_task_flag_is_rejected():
  cfg = base()
  task(cfg, "Lookup")["terminal"] = "false"
  assert has(errs(cfg),
             "Task 'Lookup' field 'terminal' must be bool, got str: 'false'")


def test_two_output_keys_may_not_target_one_slot():
  cfg = base()
  task(cfg, "Lookup")["outputs"] = {"result": "res", "balance": "res"}
  assert has(errs(cfg),
             "Task 'Lookup' outputs: both 'result' and 'balance' map to slot"
             " 'res'")


def test_one_output_key_may_name_several_slots():
  cfg = base()
  cfg["slots"].append({"name": "res2", "source": "task:Lookup"})
  task(cfg, "Lookup")["outputs"] = {"result": ["res", "res2"]}
  assert errs(cfg) == []


def test_two_slots_may_not_share_a_setter_without_setter_field():
  cfg = base()
  cfg["slots"].append({"name": "acct2", "source": "user", "setter": "set_acct",
                       "ask": "And the other?", "hint": "account number"})
  r = result(cfg)
  assert has(r.errors, "all map to setter 'set_acct' without setter_field")
  assert any(d["code"] == vdc.Codes.DUPLICATE_SETTER for d in r.diagnostics)


def test_setter_field_disambiguates_a_shared_setter():
  cfg = base()
  slot(cfg, "acct")["setter_field"] = "acct"
  cfg["slots"].append({"name": "acct2", "source": "user", "setter": "set_acct",
                       "setter_field": "acct2", "ask": "And the other?",
                       "hint": "account number"})
  assert not has(errs(cfg), "without setter_field")


def test_setter_field_without_a_setter_is_rejected():
  cfg = base()
  slot(cfg, "res")["setter_field"] = "value"
  assert has(errs(cfg), "Slot 'res' has 'setter_field' without 'setter'")


def test_gate_slot_and_bootstrap_slot_must_agree():
  cfg = base()
  cfg["gate_slot"] = "intent"
  cfg["bootstrap"] = {"slot": "scope", "tool": "start_flow"}
  assert has(errs(cfg),
             "gate_slot 'intent' != bootstrap.slot 'scope'")


def test_matching_gate_and_bootstrap_slots_are_accepted():
  cfg = base()
  cfg["gate_slot"] = "intent"
  cfg["bootstrap"] = {"slot": "intent", "tool": "start_flow"}
  assert not has(errs(cfg), "!= bootstrap.slot")


def test_requires_readback_on_a_slot_with_no_user_path_is_inert():
  cfg = base()
  slot(cfg, "res")["requires_readback"] = True
  assert has(warns(cfg), "Slot 'res' sets requires_readback but its source")


def test_requires_readback_on_a_user_slot_is_accepted():
  cfg = base()
  slot(cfg, "acct")["requires_readback"] = True
  assert not has(warns(cfg), "sets requires_readback but its source")


def test_a_push_back_block_must_be_a_dict():
  cfg = base()
  slot(cfg, "acct")["push_back"] = ["Are you sure?"]
  assert has(errs(cfg), "Slot 'acct' push_back must be a dict")


def test_push_back_reprompts_must_be_a_list_of_strings():
  cfg = base()
  slot(cfg, "acct")["push_back"] = {"reprompts": "Are you sure?",
                                    "end_conversation": True}
  assert has(errs(cfg), "Slot 'acct' push_back.reprompts must be a list of"
                        " strings")


def test_on_failure_clear_slots_must_name_declared_slots():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"max_retries": 2,
                                       "clear_slots": ["ghost"]}
  assert has(errs(cfg), "Task 'Lookup' on_failure.clear_slots references unknown"
                        " slot 'ghost'")


def test_on_failure_clear_slots_should_be_inputs_of_the_task():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"max_retries": 2, "clear_slots": ["res"]}
  assert has(warns(cfg), "Task 'Lookup' on_failure.clear_slots ['res'] are not"
                         " inputs of the task")


def test_clearing_an_input_is_accepted():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"max_retries": 2, "clear_slots": ["acct"]}
  assert not has(warns(cfg), "won't trigger a retry")


def test_readback_inputs_reads_a_dict_shaped_inputs_map():
  cfg = base()
  t = task(cfg, "Lookup")
  t["inputs"] = {"acct": "account_number"}
  t["readback_inputs"] = True
  assert has(warns(cfg), "Task 'Lookup' has readback_inputs but none of its"
                         " input slots have requires_readback")


# ══════════════════════════════════════════════════════════════════════════
# List-valued slots may not be read with a declarative value op
# ══════════════════════════════════════════════════════════════════════════

def test_a_value_op_against_a_repeated_slot_is_rejected():
  cfg = repeated_base()
  task(cfg, "Lookup")["condition"] = {"slot": "items", "eq": "one"}
  assert has(errs(cfg),
             "Task 'Lookup' condition uses a declarative value op on list-valued"
             " slot 'items'")


def test_a_value_op_nested_in_a_combinator_is_still_found():
  cfg = repeated_base()
  task(cfg, "Lookup")["condition"] = {
      "all": [{"slot": "acct", "filled": True},
              {"any": [{"slot": "items", "in": ["a", "b"]},
                       {"slot": "acct", "filled": False}]}]}
  assert has(errs(cfg), "declarative value op on list-valued slot 'items'")


def test_a_value_op_under_not_is_still_found():
  cfg = repeated_base()
  slot(cfg, "res")["condition"] = {"not": {"slot": "items", "neq": "x"}}
  assert has(errs(cfg), "declarative value op on list-valued slot 'items'")


def test_a_filled_truthiness_leaf_is_list_safe():
  cfg = repeated_base()
  task(cfg, "Lookup")["condition"] = {"slot": "items", "filled": True}
  assert not has(errs(cfg), "declarative value op")


# ══════════════════════════════════════════════════════════════════════════
# escalate.component — the in-DAG deflection sub-flow
# ══════════════════════════════════════════════════════════════════════════

def _deflect(**over):
  block = {"component": "deflect_child"}
  block.update(over)
  cfg = base()
  cfg["escalate"] = block
  return cfg


def test_a_deflection_component_is_only_valid_on_escalate():
  cfg = base()
  cfg["cancel"] = {"component": "deflect_child"}
  assert has(errs(cfg), "cancel.component is only valid on 'escalate', not"
                        " 'cancel'")


def test_a_deflection_component_must_be_a_non_empty_id():
  assert has(errs(_deflect(component="")),
             "escalate.component must be a non-empty child-config id string")


def test_a_deflection_component_may_not_be_combined_with_a_chain():
  assert has(errs(_deflect(tasks=[{"tool": "notify"}])),
             "escalate.component cannot be combined with escalate.tasks")


def test_a_deflection_component_may_not_be_combined_with_transfer_to():
  assert has(errs(_deflect(transfer_to="Human")),
             "escalate.component cannot be combined with escalate.transfer_to")


def test_a_deflection_component_may_not_carry_a_handoff_response():
  assert has(errs(_deflect(response=[{"type": "end_session"}])),
             "escalate.component cannot be combined with a hand-off `response`")


def test_a_deflection_component_on_abort_must_be_known():
  assert has(errs(_deflect(on_abort="retry")),
             "escalate.on_abort must be 'skip' or 'fail_flow', got 'retry'")


def test_a_deflection_component_io_must_be_dicts():
  out = errs(_deflect(inputs=["acct"], outputs=["res"]))
  assert has(out, "escalate.inputs must be a dict")
  assert has(out, "escalate.outputs must be a dict")


def test_a_plain_deflection_component_is_accepted():
  assert errs(_deflect(inputs={"acct": "acct"}, on_abort="skip")) == []


# ══════════════════════════════════════════════════════════════════════════
# Small pure helpers whose branches the config-level rules do not all reach
# ══════════════════════════════════════════════════════════════════════════

def test_clear_slot_names_reads_the_reason_keyed_dict_form():
  # `clear_slots` may be {error_code: [slots]}; the KEYS are error codes and
  # must never be validated as slot names.
  assert sorted(vdc._clear_slot_names({"E_BAD_ACCT": ["acct"],
                                       "E_STALE": ["res"]})) == ["acct", "res"]


def test_a_reason_keyed_clear_slots_is_resolved_against_the_slot_set():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"max_retries": 2,
                                       "clear_slots": {"E_BAD": ["ghost"]}}
  out = errs(cfg)
  assert has(out, "on_failure.clear_slots references unknown slot 'ghost'")
  assert not has(out, "'E_BAD'")


def test_clear_slot_names_reads_the_list_form():
  assert vdc._clear_slot_names(["acct"]) == ["acct"]


def test_accepts_value_reads_every_leaf_operator():
  assert vdc._accepts_value({"eq": "x"}, "x") is True
  assert vdc._accepts_value({"eq": "x"}, "y") is False
  assert vdc._accepts_value({"in": ["x", "y"]}, "y") is True
  assert vdc._accepts_value({"neq": "x"}, "y") is True
  assert vdc._accepts_value({"neq": "x"}, "x") is False
  assert vdc._accepts_value({"not_in": ["x"]}, "y") is True
  assert vdc._accepts_value({"not_in": ["x"]}, "x") is False
  # A leaf that tests nothing about the value accepts anything.
  assert vdc._accepts_value({"filled": True}, "anything") is True


def test_looks_numeric_covers_the_shapes_an_enum_option_can_take():
  assert vdc._looks_numeric(True) is True   # bool is an int subclass
  assert vdc._looks_numeric(3) is True
  assert vdc._looks_numeric("3.5") is True
  assert vdc._looks_numeric("gold") is False
  assert vdc._looks_numeric(None) is False


def test_lambda_bound_names_covers_every_binding_form():
  cfg = base()
  # A nested lambda's arg, a walrus target, and star-args are all in scope, so
  # none of them may be reported as an undefined name.
  slot(cfg, "res")["condition"] = (
      "lambda f: (lambda *rest, **kw: len(rest) + len(kw))(*f.values())")
  assert errs(cfg) == []
  cfg2 = base()
  slot(cfg2, "res")["condition"] = "lambda f: bool((n := f.get('acct')) and n)"
  assert errs(cfg2) == []


def test_value_op_condition_slots_walks_every_combinator():
  spec = {"all": [{"any": [{"slot": "a", "eq": "x"},
                           {"not": {"slot": "b", "in": ["y"]}}]},
                  {"slot": "c", "filled": True}]}
  # `filled` is list-safe and deliberately excluded.
  assert vdc._value_op_condition_slots(spec) == {"a", "b"}


def test_condition_leaves_walks_into_not():
  leaves = list(vdc._condition_leaves({"not": {"slot": "a", "eq": "x"}}))
  assert leaves == [{"slot": "a", "eq": "x"}]


# ══════════════════════════════════════════════════════════════════════════
# Remaining rule bodies — the long tail
# ══════════════════════════════════════════════════════════════════════════

def test_on_failure_flags_unknown_keys():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = {"max_retrys": 2}
  assert has(errs(cfg), "Task 'Lookup' on_failure has unknown keys:"
                        " ['max_retrys']")


def test_on_failure_must_be_a_dict():
  cfg = base()
  task(cfg, "Lookup")["on_failure"] = ["retry"]
  assert has(errs(cfg), "Task 'Lookup' on_failure must be a dict")


def test_a_task_condition_reading_an_unreachable_slot_can_never_be_satisfied():
  cfg = base()
  cfg["slots"].append({"name": "orphan", "source": "user", "ask": "?",
                       "hint": "?"})
  task(cfg, "Lookup")["condition"] = {"slot": "orphan", "filled": True}
  assert has(errs(cfg), "Task 'Lookup' condition references 'orphan' which is"
                        " unreachable — condition can never be satisfied")


def test_an_enum_check_skips_a_condition_slot_that_does_not_exist():
  # The dangling reference is reported by the condition-spec pass; the enum
  # rule must not also blame it (or crash looking the slot up).
  cfg = base()
  task(cfg, "Lookup")["condition"] = {"slot": "ghost", "eq": "x"}
  out = errs(cfg)
  assert has(out, "condition references unknown slot 'ghost'")
  assert not has(out, "is not one of its enum options")


def test_a_numeric_comparison_on_a_non_numeric_enum_slot_is_rejected():
  cfg = base()
  slot(cfg, "acct")["validation_rules"] = [{"kind": "enum",
                                            "detail": "gold|silver"}]
  slot(cfg, "res")["condition"] = {"slot": "acct", "gt": 1}
  assert has(errs(cfg), "numeric comparison (gt/lt/gte/lte) on non-numeric slot"
                        " 'acct'")


def test_shared_slots_ignores_a_non_string_entry():
  cfg = base()
  cfg["shared_slots"] = [7, "acct"]
  slot(cfg, "acct")["shared"] = True
  assert errs(cfg) == []


def test_a_default_carrying_no_fallback_values_is_not_second_guessed():
  cfg = base()
  slot(cfg, "res")["default"] = ["unknown"]  # not {value: ...} dicts
  assert not has(warns(cfg), "none of the branches reading it accept")


def test_gate_bootstrap_consistency_needs_a_dict_bootstrap():
  cfg = base()
  cfg["gate_slot"] = "intent"
  cfg["bootstrap"] = "start_flow"
  out = errs(cfg)
  assert has(out, "'bootstrap' must be a dict")
  assert not has(out, "!= bootstrap.slot")


def test_a_control_block_response_that_is_not_a_list():
  cfg = base()
  cfg["escalate"] = {"response": "Goodbye.", "transfer_to": "Human"}
  out = errs(cfg)
  assert has(out, "'escalate' response must be a list")
  assert not has(out, "carries both a hand-off payload")


def test_an_ask_alongside_a_malformed_response_part_does_not_misfire():
  cfg = base()
  slot(cfg, "acct")["response"] = ["Account?"]
  out = errs(cfg)
  assert has(out, "Slot 'acct' response[0] must be a dict")
  assert not has(warns(cfg), "both 'ask' and a text-type response")


def test_a_channel_override_text_part_with_a_non_string_text():
  cfg = base()
  slot(cfg, "acct")["channel_responses"] = {"voice": [{"type": "text",
                                                       "text": 7}]}
  assert not has(warns(cfg), "unknown placeholder")


def test_an_exhaust_then_tool_check_ignores_a_non_dict_exhaust():
  cfg = base()
  cfg["steer_back"] = {"on_exhaust": "escalate"}
  out = errs(cfg, available_tools=TOOLS)
  assert has(out, "steer_back.on_exhaust must be a dict")


def test_a_task_then_response_that_is_not_a_list_reads_as_silent():
  # The shape check owns the diagnosis; the coverage pass sees a value it
  # cannot read as parts and reports the consequence. Both are errors, so the
  # build blocks either way.
  cfg = base()
  t = task(cfg, "Lookup")
  t.pop("then_say")
  t["then_response"] = {"type": "text", "text": "Done."}
  out = errs(cfg)
  assert has(out, "Task 'Lookup' response must be a list")
  assert has(out, "Task 'Lookup' then_response has neither text nor payload")


def test_setter_sources_only_check_the_setters_they_carry():
  # A partial source map (only some setters available) must not be read as
  # "this setter returns nothing".
  cfg = base()
  slot(cfg, "acct")["setter_field"] = "acct"
  cfg["slots"].append({"name": "acct2", "source": "user", "setter": "set_acct",
                       "setter_field": "acct2", "ask": "Other?",
                       "hint": "account number"})
  r = result(cfg, setter_sources={"unrelated": "def f():\n    return {}\n"})
  assert not has(r.errors, "never writes values")
  assert not has(r.warnings, "may not return a 'value' key")


def test_an_unparseable_setter_source_is_not_second_guessed():
  r = result(base(), setter_sources={"set_acct": "def set_acct(:\n"})
  assert not has(r.warnings, "may not return a 'value' key")


def test_an_unparseable_tool_source_is_not_second_guessed():
  cfg = base()
  task(cfg, "Lookup")["outputs"] = {"balance": "res"}
  out = errs(cfg, task_tool_sources={"lookup_tool": "def lookup_tool(:\n"})
  assert not has(out, "never returns it")


def test_a_non_string_output_key_is_left_to_the_shape_check():
  cfg = base()
  task(cfg, "Lookup")["outputs"] = {7: "res"}
  out = errs(cfg, task_tool_sources={"lookup_tool": TOOL_SRC})
  assert not has(out, "never returns it")


def test_a_completion_placeholder_check_skips_a_non_string_tool_name():
  cfg = base()
  task(cfg, "Lookup")["tool"] = ["lookup_tool"]
  task(cfg, "Lookup")["then_say"] = "Balance is {balance}."
  out = errs(cfg, task_tool_sources={"lookup_tool": TOOL_SRC})
  assert not has(out, "completion text references")


def test_an_unparseable_tool_source_skips_the_completion_check():
  cfg = base()
  task(cfg, "Lookup")["then_say"] = "Balance is {balance}."
  out = errs(cfg, task_tool_sources={"lookup_tool": "def lookup_tool(:\n"})
  assert not has(out, "completion text references")


def test_a_positional_placeholder_has_no_root_to_resolve():
  cfg = base()
  task(cfg, "Lookup")["then_say"] = "Balance is {}."
  out = errs(cfg, task_tool_sources={"lookup_tool": TOOL_SRC})
  assert not has(out, "completion text references")


def test_a_router_flow_types_list_may_carry_a_blank_entry():
  configs, tables = _router_bundle()
  configs["router"]["flow_types"] = ["billing", "  ", "tech"]
  assert not has(cross(configs, tables).errors, "router flow_type")


def test_a_component_input_binding_that_is_not_a_string_is_left_alone():
  parent = _parent_with_component(inputs={"acct": 7})
  assert not has(cross({"parent": parent, "child": _child()}).errors,
                 "input maps to child slot")


def test_a_repeated_component_pointing_at_a_missing_child_is_not_double_reported():
  parent = _parent_with_component("ghost",
                                  repeated={"until": {"done_setter": "pin"}})
  out = cross({"parent": parent}).errors
  assert has(out, "references unknown child config 'ghost'")
  assert not has(out, "is not a slot in child config")


def test_status_contamination_reads_a_malformed_bootstrap_as_absent():
  a = _leaf()
  a["bootstrap"] = "start_flow"
  assert has(cross({"a": a, "b": _leaf()}).errors,
             "Config 'a' has terminal tasks but bootstrap.reset_on_complete is"
             " not True")


def test_a_task_response_that_is_not_a_list_still_reports_its_shape():
  cfg = base()
  t = task(cfg, "Lookup")
  t.pop("then_say")
  t["then_response"] = "Done."
  assert has(errs(cfg), "Task 'Lookup' response must be a list")


def test_an_identical_diagnostic_is_reported_once():
  # Two slots that both refuse to be reachable produce one message each; two
  # copies of the SAME message collapse, so a caller never sees a doubled line.
  dup = {"severity": "error", "message": "same", "code": "SF001",
         "anchor": None, "fix_id": None}
  deduped, errors, warnings = vdc._dedupe_sort_diagnostics(
      [dict(dup), dict(dup),
       {"severity": "warning", "message": "w", "code": None, "anchor": None,
        "fix_id": None}])
  assert len(deduped) == 2
  assert errors == ["same"]
  assert warnings == ["w"]
  # Errors sort before warnings, and re-feeding the output is a no-op.
  assert vdc._dedupe_sort_diagnostics(deduped)[0] == deduped


def test_a_malformed_json_routing_table_reads_as_empty():
  # before_agent stores these as JSON strings; an unparseable one must not
  # crash the linter and must not be read as a table full of dangling targets.
  configs, tables = _router_bundle()
  tables["flow_config_map"] = "{not json"
  out = cross(configs, tables).errors
  assert not has(out, "flow_config_map target")
  # ...and the flow types then legitimately resolve to nothing.
  assert has(out, "router flow_type 'billing' has no config")


def test_a_malformed_join_template_is_not_read_as_a_placeholder_list():
  cfg = repeated_base(readback_fmt={"type": "join", "each": "{"})
  assert not has(warns(cfg), "readback_fmt `each` references")


def test_a_malformed_component_join_template_is_not_read_as_placeholders():
  cfg = component_base()
  slot(cfg, "lines")["readback_fmt"] = {"type": "join", "each": "{"}
  assert not has(errs(cfg), "is not an element field")


def test_a_setter_with_no_source_in_the_map_is_not_second_guessed():
  r = result(base(), setter_sources={"unrelated": "def f():\n    return {}\n"})
  assert not has(r.warnings, "may not return a 'value' key")


def test_an_ask_ladder_with_no_usable_rung_reads_as_no_ask_at_all():
  cfg = intent_base(ask=["", "   "])
  assert has(errs(cfg), "Intent slot 'intent' has no `ask` but is not `passive`")
