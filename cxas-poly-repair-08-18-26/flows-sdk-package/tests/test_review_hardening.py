"""Regression tests for the intent-slot / repeated-task / cue-matching hardening added in review of the
single-agent-router framework changes. Each test pins a specific crash-or-misroute the reviewer flagged:

  - option_cues type-checking (validator + engine) so a mis-authored non-dict / non-string cue is REJECTED
    at validation time and NEVER reaches the engine as an AttributeError/TypeError.
  - repeated over/each co-presence + type checks so a bare/ill-typed `over` can't skip list-exhaustion.
  - single-word route cues match at a WORD BOUNDARY so a standalone word's real position wins (no substring
    hijack inside a longer earlier word).

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_review_hardening.py
"""
from __future__ import annotations

from flows.engine import loader

eng = loader.load_engine()
vdc = loader.load_validator()


def _errors(cfg):
    return vdc.validate_dag_config({"raw_config": cfg})["errors"]


# --------------------------------------------------------------------- validator: intent-slot option_cues
def test_validator_rejects_non_dict_option_cues():
    cfg = {"config_id": "x", "slots": [
        {"name": "method", "source": "user", "kind": "intent", "passive": True,
         "option_cues": "otp_or_kba",  # BUG shape: a string, not a dict
         "validation_rules": [{"kind": "enum", "detail": "otp|kba"}]},
    ]}
    assert any("option_cues must be a dict" in e for e in _errors(cfg))


def test_validator_rejects_non_string_cue_patterns():
    cfg = {"config_id": "x", "slots": [
        {"name": "method", "source": "user", "kind": "intent", "passive": True,
         "option_cues": {"otp": ["\\botp\\b", 123]},  # a non-string pattern in the list
         "validation_rules": [{"kind": "enum", "detail": "otp|kba"}]},
    ]}
    assert any("must be a list of strings" in e for e in _errors(cfg))


# --------------------------------------------------------------------- validator: repeated over/each
def _component_cfg(repeated):
    return {"config_id": "x", "slots": [
        {"name": "items", "source": "user"},
        {"name": "collected", "source": "user",
         "readback_fmt": {"type": "join", "each": "{a}"}},
    ], "tasks": [
        {"name": "loop", "component": "child", "repeated": repeated,
         "collect": "collected", "element": {"a": "a"}},
    ]}


def test_validator_rejects_over_without_each():
    errs = _errors(_component_cfg({"over": "items"}))
    assert any("repeated.over requires a non-empty repeated.each" in e for e in errs)


def test_validator_rejects_non_string_over():
    errs = _errors(_component_cfg({"over": 123, "each": {"a": "a"}}))
    assert any("repeated.over must be a non-empty string" in e for e in errs)


def test_validator_rejects_over_slot_not_in_slots():
    errs = _errors(_component_cfg({"over": "nonexistent", "each": {"a": "a"}}))
    assert any("repeated.over slot 'nonexistent' not in slots" in e for e in errs)


# --------------------------------------------------------------------- engine: defensive no-crash
def test_engine_cue_match_tolerates_non_string_pattern():
    # A JSON int/bool that slipped past validation must NOT crash the engine at match time.
    assert eng._cue_match("otp", "please use otp") is True
    assert eng._cue_match(123, "code 123 please") is True     # coerced to str, no TypeError
    assert eng._cue_match(123, "nothing here") is False


def test_engine_apply_option_cues_skips_non_dict():
    cfg = {"slots": [
        {"name": "method", "source": "user", "kind": "intent", "passive": True,
         "option_cues": "not_a_dict"},   # would AttributeError if not guarded
    ]}
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, cfg, "otp please", is_routing=True)  # must not raise
    assert sm["filled"].get("method") is None


# --------------------------------------------------------------------- engine: word-boundary cue position
def test_route_intent_single_word_cue_uses_word_boundary_position():
    # "age" appears as a SUBSTRING inside "manage" (pos 3) but is a standalone word only at pos 11.
    # "the" is standalone at pos 7. The earliest STANDALONE cue must win → flow B ("the"), not A ("age").
    route_cues = {"A": ["age"], "B": ["the"]}
    assert eng._route_intent("manage the age", route_cues, "") == "B"


def test_route_intent_no_substring_false_match():
    # "class" only occurs inside "classification" — a single-word cue must NOT match a substring.
    assert eng._route_intent("classification review", {"A": ["class"]}, "") == ""
