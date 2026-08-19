"""Telling a caller who needs a moment from one who needs an answer.

`hold_phrases` used to be matched as plain substrings, and that forced a choice nobody
should have to make. Carry the bare interruption markers and "hold" matches *household*
while "sec" matches *second opinion*; leave them out and the commonest thing a caller
says when they cannot answer yet -- "hold on", "hang on", "wait" -- is not recognized at
all. Real agents chose to leave them out, so the feature missed the majority of the
callers it exists for.

Detection is now two-sided: markers matched on word boundaries, and a VETO for the
utterances that carry a marker without being a request for time. That second half is what
makes the first half safe, so most of what follows is about the veto.

Two consequences of recognizing a hold are tested here as well, because both were places
a request for time was actively counted against the caller:

* the barren-turn ladders charged it as an answer that resolved to nothing, and two of
  those force-fill the slot and carry on without them;
* the keyword route backstop read the cues INSIDE the stall, so "one sec, let me grab my
  bill" left the flow for the billing route before the model ever ran.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows.authoring import dsl  # noqa: E402
from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)

ACK = "Take your time, I'm here."


def drive(cfg, sm, text, turn=1, inactivity=False):
  """One engine turn, returning the ACTION (`sm` is mutated in place)."""
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": inactivity,
      "event_data": {}, "config_id": "t", "n_user_turns": turn,
  })["action"]


def fresh(cfg):
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": {}, "last_user_text": "", "scanned_user_text": "",
      "is_inactivity": False, "event_data": {}, "config_id": "t", "n_user_turns": 0,
  })["sm"]


def policy(**over):
  p = {
      "reprompts": ["What's the account number?"],
      "hold_phrases": list(dsl.DEFAULT_HOLD_PHRASES),
      "hold_reprompts": ["", "", "Still here."],
      "hold_ack": ACK,
      "on_exhaust": {"say": "Let me get someone to help."},
  }
  p.update(over)
  return p


def acct_cfg(no_input=None, validation=None):
  slot = {"name": "account_number", "source": "user", "setter": "set_account",
          "ask": "What's your account number?"}
  if validation:
    slot["validation"] = validation
  cfg = {"slots": [slot], "tasks": [], "gate_slot": None}
  if no_input is not None:
    cfg["no_input"] = no_input
  return cfg


def held(text, no_input=None):
  """Did one utterance put the flow into HOLD mode?"""
  cfg = acct_cfg(no_input if no_input is not None else policy())
  sm = fresh(cfg)
  drive(cfg, sm, text)
  return bool(sm.get("_hold_on"))


# --------------------------------------------------------------------------- #
# 1. Word boundaries. The whole reason the bare markers can be carried.

def test_a_marker_only_matches_whole_words():
  # "hold" is in the default list. As a substring it was inside all of these.
  for text in ("the household internet is down", "i want a second opinion",
               "my holdings are fine", "i waited all day"):
    assert not held(text), text


def test_the_bare_interruption_markers_are_recognized():
  for text in ("hold on", "hang on", "wait", "hold up", "one sec", "sec",
               "just a moment", "give me a minute", "hold"):
    assert held(text), text


def test_a_marker_survives_punctuation_and_case():
  assert held("Hold on!")
  assert held("...hold on, ok?")


# --------------------------------------------------------------------------- #
# 2. The veto. A marker is necessary and not sufficient.

def test_a_question_wearing_a_marker_is_not_a_hold():
  for text in ("hold on, why do you need my account number",
               "wait, what did you say",
               "hang on, can you repeat that",
               "hold on, what do you mean"):
    assert not held(text), text


def test_asking_for_a_person_is_not_a_hold():
  # Waiting patiently for someone who asked to be transferred strands them.
  for text in ("hold on, just get me a person",
               "wait, i want to talk to a human",
               "hang on, transfer me to a representative"):
    assert not held(text), text


def test_saying_you_cannot_answer_is_not_a_hold():
  # The opposite of asking for time to find something.
  for text in ("i can't find my account number anywhere",
               "hang on, i already gave you that",
               "wait, i don't have an account with you"):
    assert not held(text), text


def test_calling_the_whole_thing_off_is_not_a_hold():
  """A marker must not swallow a cancellation.

  This is the sharpest case for the veto, because the two mechanisms compound: a hold
  turn suppresses the keyword backstops AND preempts the model, so a request to cancel
  wearing a marker reached neither. The caller asked to stop and got "take your time".
  """
  for text in ("hold on, cancel that", "wait, stop", "hang on, never mind",
               "hold on, forget it", "one sec, cancel my order"):
    assert not held(text), text


def test_a_cancellation_wearing_a_marker_still_reaches_the_turn():
  """The A/B on the compounding half: the turn must not be preempted away.

  `_cancel_intent` is a whole-utterance matcher and does not itself fire on the prefixed
  form (untouched by this change). What matters is that the turn is no longer consumed by
  the ack, so the model still sees it and can call the cancel setter.
  """
  cfg = acct_cfg(policy())
  cfg["cancelable"] = True
  plain = drive(cfg, fresh(cfg), "cancel that")
  assert (plain.get("function_call") or {}).get("name") == "cancel_flow"
  worn = drive(cfg, fresh(cfg), "hold on, cancel that")
  assert worn.get("message") != ACK
  assert not worn.get("preempt")


def test_correcting_what_the_call_is_about_is_not_a_hold():
  assert not held("hold on, this is about my bill not my internet")
  assert not held("wait, i thought this was about my appointment")


def test_reading_the_value_out_is_not_a_hold():
  # The ack preempts the model, so treating this as a stall DROPS the number.
  assert not held("hold on, it's 8069100230359946")
  assert held("hold on, it starts with 8069")     # a fragment is not the value
  assert held("wait 30 seconds")                  # small numbers are not values


def test_explaining_why_you_need_a_moment_is_still_a_hold():
  # These carry words the veto families use, and are requests for time.
  assert held("i don't have it in front of me, let me find it")
  assert held("let me see if i can find it")
  assert held("hold on, someone's at the door")


# --------------------------------------------------------------------------- #
# 3. The veto is configurable.

def test_hold_vetoes_can_be_extended():
  # An author's own disqualifier, on top of nothing else changing.
  custom = policy(hold_vetoes=["cancel my service"])
  assert not held("hold on, i want to cancel my service", custom)
  assert held("hold on, let me find it", custom)


def test_an_empty_veto_list_restores_marker_only_matching():
  bare = policy(hold_vetoes=[])
  assert held("hold on, why do you need that", bare)


def test_the_dsl_defaults_match_the_engine_defaults():
  """One list, two homes. The engine cannot import the DSL, so a test pins them equal."""
  engine = fb.load_engine()
  assert list(engine._DEFAULT_HOLD_VETOES) == list(dsl.DEFAULT_HOLD_VETOES)


# --------------------------------------------------------------------------- #
# 4. What recognizing a hold now prevents.

_FILL_ON_EXHAUST = {"max_retries": 2, "on_exhaust": {"fill": "UNKNOWN"}}


def _walk(texts):
  """Drive one caller turn per text; report what was said and what the slot ended up as.

  The counter itself is not worth asserting on: it is cleared the moment it fires, so at
  the end of a walk that exhausted it reads the same as one that never charged. The fill
  is the observable the caller actually experiences.
  """
  cfg = acct_cfg(policy(), validation=_FILL_ON_EXHAUST)
  sm = fresh(cfg)
  said = []
  for i, text in enumerate(texts, 1):
    said.append((drive(cfg, sm, text, turn=i) or {}).get("message", ""))
  return said, sm.get("filled", {}).get("account_number")


def test_a_hold_does_not_spend_the_barren_turn_retries():
  """Three requests for time must not force-fill the slot and move on without them."""
  said, filled = _walk(["hold on, let me find it", "still looking",
                        "sorry, one more second"])
  assert filled is None
  assert said[0] == ACK


def test_an_unresolvable_answer_still_spends_them():
  """The A/B: the counter is not disabled, it just does not charge for a hold.

  The same number of turns, none of them a request for time, and the slot force-fills.
  """
  _said, filled = _walk(["the weather is nice", "it really is", "lovely out there"])
  assert filled == "UNKNOWN"


def test_a_hold_in_the_middle_neither_charges_nor_forgives():
  """A request for time is neutral. The two barren turns around it still exhaust."""
  _said, filled = _walk(["the weather is nice", "hold on", "it really is"])
  assert filled == "UNKNOWN"


def _router_cfg(no_input=None):
  cfg = {
      "slots": [{"name": "active_flow", "source": "user", "setter": "set_active_flow"}],
      "tasks": [], "router": True, "gate_slot": "active_flow",
      "flow_types": ["repair", "billing"],
      "route_cues": {"billing": ["my bill"]},
      "bootstrap": {"tool": "set_active_flow"},
  }
  if no_input is not None:
    cfg["no_input"] = no_input
  return cfg


def _routed_to(text, no_input=None):
  cfg = _router_cfg(no_input)
  sm = fresh(cfg)
  action = drive(cfg, sm, text)
  fc = (action or {}).get("function_call") or {}
  return (fc.get("args") or {}).get("flow") if fc.get("name") == "set_active_flow" else None


def test_a_cue_inside_a_request_for_time_does_not_route():
  """The opening turn is a routing turn, and a route cannot be undone downstream."""
  assert _routed_to("one sec, let me grab my bill", policy()) is None


def test_the_same_cue_still_routes_when_it_is_the_request():
  assert _routed_to("i have a question about my bill", policy()) == "billing"


def test_a_router_with_no_policy_cannot_tell_and_still_routes():
  """Why every flow needs a policy: detection rides on it, so a router without one is
  exactly as exposed as before."""
  assert _routed_to("one sec, let me grab my bill", None) == "billing"
