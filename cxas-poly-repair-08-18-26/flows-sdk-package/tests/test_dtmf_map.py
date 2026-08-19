"""Deterministic keypad fill (`dtmf_map`) — the twin of `option_cues`, matched on the TOKEN.

The engine fills the currently-AWAITED user slot when the caller's input is a mapped keypad token, with
no LLM in the loop. Two input shapes have to work, and only one of them used to:

  * a BARE token ("3"), which is what a text channel (and the sim) delivers, and
  * the CONTEXT ENVELOPE a real CES keypad press arrives in —
    ``<context>user pressed 3 on keypad.</context>``.

The envelope was the gap. `dtmf_map` exists for exactly one channel and could not fire on it: the
bare-token match saw the whole envelope string, found no key, and handed the press to the model.
Live-verified against ces-deployment-dev before the fix (`Sessions.run(dtmf="3")` filled nothing while
`Sessions.run(text="3")` filled deterministically) and after.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_dtmf_map.py
"""
from __future__ import annotations

from flows.engine import loader

eng = loader.load_engine()

CFG = {"slots": [
    {"name": "menu_choice", "source": "user", "setter": "set_menu_choice",
     "dtmf_map": {"1": "billing", "2": "support", "0": "operator", "#": "repeat"}},
    {"name": "account_number", "source": "user", "setter": "set_account_number",
     "requires": ["menu_choice"]},
]}


def _fresh():
  return {"filled": {}, "pending": {}}


# --- the bare token (text channel) -------------------------------------------------


def test_a_bare_token_fills_the_awaited_slot():
  sm = _fresh()
  eng._apply_dtmf_input(sm, CFG, "2")
  assert sm["filled"] == {"menu_choice": "support"}
  assert sm.get("_event_prefilled_this_turn") is True


def test_a_politeness_prefix_is_tolerated():
  sm = _fresh()
  eng._apply_dtmf_input(sm, CFG, "press 1")
  assert sm["filled"] == {"menu_choice": "billing"}


def test_an_unmapped_token_falls_through_to_the_model():
  sm = _fresh()
  eng._apply_dtmf_input(sm, CFG, "7")
  assert sm["filled"] == {}
  assert "_event_prefilled_this_turn" not in sm


def test_ordinary_speech_is_never_read_as_a_press():
  sm = _fresh()
  eng._apply_dtmf_input(sm, CFG, "I'd like billing please")
  assert sm["filled"] == {}


# --- the CES context envelope (the real keypad channel) ----------------------------


def test_a_ces_keypad_envelope_fills_the_awaited_slot():
  """The shape CES actually delivers. This is the whole point of the feature."""
  sm = _fresh()
  eng._apply_dtmf_input(sm, CFG, "<context>user pressed 2 on keypad.</context>")
  assert sm["filled"] == {"menu_choice": "support"}
  assert sm.get("_event_prefilled_this_turn") is True


def test_the_envelope_is_matched_case_insensitively_and_without_the_period():
  sm = _fresh()
  eng._apply_dtmf_input(sm, CFG, "<CONTEXT>User Pressed 0 On Keypad</CONTEXT>")
  assert sm["filled"] == {"menu_choice": "operator"}


def test_a_non_digit_key_in_the_envelope_still_maps():
  sm = _fresh()
  eng._apply_dtmf_input(sm, CFG, "<context>user pressed # on keypad.</context>")
  assert sm["filled"] == {"menu_choice": "repeat"}


def test_an_envelope_whose_digits_are_unmapped_falls_through():
  """A free-form entry (a typed 6-digit code) must still reach the model untouched."""
  sm = _fresh()
  eng._apply_dtmf_input(sm, CFG, "<context>user pressed 123456 on keypad.</context>")
  assert sm["filled"] == {}
  assert "_event_prefilled_this_turn" not in sm


def test_talking_about_pressing_a_key_is_not_a_press():
  """The match is anchored on the whole envelope, not on the words."""
  sm = _fresh()
  eng._apply_dtmf_input(sm, CFG, "I pressed 1 on keypad and nothing happened")
  assert sm["filled"] == {}


# --- scoping: only the awaited slot, only when it has a map ------------------------


def test_a_press_at_a_slot_with_no_map_is_inert():
  sm = {"filled": {"menu_choice": "billing"}, "pending": {}}
  eng._apply_dtmf_input(sm, CFG, "<context>user pressed 2 on keypad.</context>")
  assert sm["filled"] == {"menu_choice": "billing"}  # account_number is awaited; no map
  assert "_event_prefilled_this_turn" not in sm


def test_a_press_cannot_reach_a_non_awaited_mapped_slot():
  """Two mapped slots, the second awaited: the press fills the awaited one only."""
  cfg = {"slots": [
      {"name": "first", "source": "user", "setter": "set_first",
       "dtmf_map": {"1": "one"}},
      {"name": "second", "source": "user", "setter": "set_second",
       "dtmf_map": {"1": "uno"}},
  ]}
  sm = {"filled": {"first": "one"}, "pending": {}}
  eng._apply_dtmf_input(sm, cfg, "<context>user pressed 1 on keypad.</context>")
  assert sm["filled"] == {"first": "one", "second": "uno"}


def test_no_dtmf_map_anywhere_is_a_byte_identical_no_op():
  cfg = {"slots": [{"name": "phone", "source": "user", "setter": "set_phone"}]}
  sm = _fresh()
  eng._apply_dtmf_input(sm, cfg, "<context>user pressed 1 on keypad.</context>")
  assert sm == {"filled": {}, "pending": {}}
