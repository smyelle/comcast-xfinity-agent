"""Deterministic per-slot cue→value fill (`option_cues`) — the text twin of dtmf_map that routes an
enum-ish user slot (e.g. journey_intent) by REGEX so op selection doesn't depend on the LLM. Verifies
empty-fill (awaited-slot scoped), routing-turn override, ambiguity skip, no-clobber on later turns, and
byte-identical no-op when a slot has no option_cues. Run: PYTHONPATH=packages/flows/src pytest.
"""
from __future__ import annotations

from flows.engine import loader

eng = loader.load_engine()

CFG = {"slots": [
    {"name": "welcome", "source": "announce", "message": "hi"},
    {"name": "journey_intent", "source": "user", "setter": "set_journey_intent",
     "option_cues": {"place": [r"\bplace\b", r"\bfreeze my credit\b"],
                     "lift": [r"\blift\b", r"\btemporar\w+ unfreeze\b"],
                     "remove": [r"\bremove\b"]}},
]}


def test_empty_fill_from_routing_utterance():
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, CFG, "i'd like to place a security freeze", is_routing=True)
    assert sm["filled"].get("journey_intent") == "place"
    assert sm.get("_event_prefilled_this_turn") is True


def test_multiword_regex_cue():
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, CFG, "can you temporarily unfreeze it", is_routing=True)
    assert sm["filled"].get("journey_intent") == "lift"


def test_routing_turn_overrides_llm_misset():
    sm = {"filled": {"journey_intent": "lift"}, "pending": {}}
    eng._apply_option_cues(sm, CFG, "i want to place a freeze", is_routing=True)
    assert sm["filled"]["journey_intent"] == "place"


def test_ambiguous_utterance_does_not_fill():
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, CFG, "should i place or remove the freeze", is_routing=True)
    assert "journey_intent" not in sm["filled"]


def test_routing_turn_fills_nonawaited_enum_slot():
    # On the routing turn, an option_cues enum slot (journey_intent) is set UP FRONT from the gate
    # utterance even when a DIFFERENT slot is awaited (e.g. the flow collects PII first). This avoids an
    # intercepted mid-flow choice turn. Awaited slot here is 'phone' (no option_cues), journey_intent is not.
    cfg = {"slots": [
        {"name": "phone", "source": "user", "setter": "set_phone"},
        {"name": "journey_intent", "source": "user", "setter": "set_journey_intent",
         "option_cues": {"place": [r"\bplace\b"], "lift": [r"\blift\b"]}},
    ]}
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, cfg, "i'd like to place a security freeze", is_routing=True)
    assert sm["filled"].get("journey_intent") == "place"


def test_nonrouting_turn_does_not_fill_nonawaited_slot():
    # Off the routing turn, a non-awaited option_cues slot is NOT filled (only the awaited slot is) — so a
    # stray cue while answering another question can't pre-set the enum.
    cfg = {"slots": [
        {"name": "phone", "source": "user", "setter": "set_phone"},
        {"name": "journey_intent", "source": "user", "setter": "set_journey_intent",
         "option_cues": {"place": [r"\bplace\b"]}},
    ]}
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, cfg, "i live at 123 place street", is_routing=False)
    assert "journey_intent" not in sm["filled"]


def test_later_turn_does_not_clobber():
    # is_routing False (later turn); an incidental cue word must NOT override an existing value.
    sm = {"filled": {"journey_intent": "place"}, "pending": {}}
    eng._apply_option_cues(sm, CFG, "the address number is 123 place street", is_routing=False)
    assert sm["filled"]["journey_intent"] == "place"


def test_word_boundary_no_false_match():
    # "\bplace\b" must not match inside "replace"; no other value matches → no fill.
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, CFG, "please replace my card", is_routing=True)
    assert "journey_intent" not in sm["filled"]


def test_no_option_cues_is_noop():
    cfg = {"slots": [{"name": "reason", "source": "user", "setter": "set_reason"}]}
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, cfg, "place a freeze", is_routing=True)
    assert sm["filled"] == {}


def test_empty_text_is_noop():
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, CFG, "", is_routing=True)
    assert sm["filled"] == {}


def test_bad_regex_falls_back_to_literal():
    cfg = {"slots": [{"name": "journey_intent", "source": "user", "setter": "set_journey_intent",
                      "option_cues": {"place": ["("]}}]}   # "(" is an invalid regex
    sm = {"filled": {}, "pending": {}}
    eng._apply_option_cues(sm, cfg, "just add a ( here", is_routing=True)
    assert sm["filled"].get("journey_intent") == "place"   # literal-substring fallback matched


# --- dtmf_map fast-path (the keypad twin of option_cues) ------------------------
# CES delivers a keypress as "<context>user pressed 1 on keypad.</context>"; before_model
# lifts the bare token so _apply_dtmf_input can match it. These assert the engine half:
# given the lifted token, the awaited dtmf_map slot fills deterministically (no LLM).
DTMF_CFG = {"slots": [
    {"name": "menu_choice", "source": "user", "setter": "set_menu_choice",
     "dtmf_map": {"1": "tracking", "2": "agent"},
     "validation_rules": [{"kind": "enum", "detail": "tracking|agent"}]},
]}


def test_dtmf_map_fills_from_a_lifted_keypad_token():
    sm = {"filled": {}, "pending": {}}
    eng._apply_dtmf_input(sm, DTMF_CFG, "1")
    assert sm["filled"].get("menu_choice") == "tracking"
    assert sm.get("_event_prefilled_this_turn") is True


def test_dtmf_map_ignores_an_unmapped_token():
    sm = {"filled": {}, "pending": {}}
    eng._apply_dtmf_input(sm, DTMF_CFG, "9")
    assert "menu_choice" not in sm["filled"]


def test_dtmf_map_no_op_when_last_user_text_is_the_barge_in_note():
    # The pre-fix failure: the note reached the engine instead of the digit, so the
    # keypad fast-path never fired. (With the fix, before_model passes "1" here.)
    sm = {"filled": {}, "pending": {}}
    note = "<context>agent speaking was interrupted. user only heard 'Main' ...</context>"
    eng._apply_dtmf_input(sm, DTMF_CFG, note)
    assert "menu_choice" not in sm["filled"]
