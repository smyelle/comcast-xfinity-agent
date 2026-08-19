"""`push_back` — a slot re-offer ladder for a caller who keeps DECLINING an offer.

The counterpart, for an offer the caller keeps pushing back on, of the other bounded
ladders: `no_input` (silence), a slot's `validation` (a bad value), and `steer_back`
(a stalled turn). It re-offers `reprompts[k]` for the first `max` pushes (as a PREEMPT,
so the model cannot improvise the turn), then disposes via on_exhaust (`fill` and/or a
`then` control tool, optionally ending the leg).

It is in particular what a CUE-ONLY intent slot needs: one with `option_cues` and no
model setter, so an off-cue answer fills nothing and would otherwise let the model answer
the turn itself. This is the general form of the fix for the Verizon deflection evals,
where the model improvised a premature "I'm connecting you to a live agent now" on the
insist turn.
"""

from __future__ import annotations

import flows
from flows.engine import loader as fb
from flows.engine.framework.tools.slot_filling_engine.python_function import (
    python_code as engine,
)


# ── DSL ────────────────────────────────────────────────────────────────────────
def test_push_back_helper_builds_the_block():
  pb = flows.push_back(
      reprompts=["Try the assistant?"], max=1, say="Connecting you.",
      then={"tool": "updated_billing_transfer_call", "args": {"action": "transfer"}},
      end_conversation=True, verbatim=True)
  assert pb["reprompts"] == ["Try the assistant?"]
  assert pb["max"] == 1
  assert pb["then"] == {"tool": "updated_billing_transfer_call",
                        "args": {"action": "transfer"}}
  assert pb["say"] == "Connecting you." and pb["end_conversation"] is True
  assert pb["verbatim"] is True


def test_push_back_rejects_an_empty_ladder():
  # No reprompts and no disposition does nothing — an authoring error, not silence.
  try:
    flows.push_back(reprompts=[])
  except ValueError:
    return
  raise AssertionError("push_back() with no reprompts/fill/then should raise")


def test_cue_only_intent_slot_has_no_setter():
  slot = flows.intent_slot(
      "va_choice", {"hangup": [r"\byes\b"]}, ask="Assistant?", cue_only=True)
  assert slot["setter"] == ""  # no model setter — only option_cues fill it
  assert slot["option_cues"] == {"hangup": [r"\byes\b"]}


def test_cue_only_and_setter_are_mutually_exclusive():
  try:
    flows.intent_slot("x", {"a": [r"\ba\b"]}, cue_only=True, setter="set_x")
  except ValueError:
    return
  raise AssertionError("cue_only=True with an explicit setter should raise")


# ── validator: a cue-only slot with push_back is reachable and valid ────────────
def test_cue_only_push_back_slot_validates():
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot(
      "va_choice", {"hangup": [r"\byes\b", r"\bsure\b"]},
      ask="Would you like the Verizon Assistant?", cue_only=True, verbatim=True,
      push_back=flows.push_back(
          reprompts=["The assistant can help — shall I connect you instead?"],
          max=1, say="Connecting you now.",
          then={"tool": "transfer_to_human", "args": {}},
          end_conversation=True)))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  # A setter-less slot with option_cues is capturable (cue-fill); push_back is a valid
  # block. Neither should be flagged as unreachable / unknown.
  assert errors == [], errors


def test_validator_rejects_unknown_push_back_key():
  f = flows.Flow("j", root_agent="a")
  slot = flows.intent_slot(
      "va_choice", {"hangup": [r"\byes\b"]}, ask="?", cue_only=True,
      push_back=flows.push_back(reprompts=["again?"], fill="hangup"))
  slot["push_back"]["reprmopts"] = ["typo"]  # deliberate typo
  f.add(slot)
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert any("push_back has unknown keys" in e for e in errors), errors


def test_validator_rejects_mistyped_push_back_scalars():
  # The DSL type-hints these keys but does not enforce them, and a raw / studio
  # config can carry any type. A wrong type must be caught here, not left to crash
  # the engine mid-call (`k <= max`, `say.format(...)`).
  f = flows.Flow("j", root_agent="a")
  slot = flows.intent_slot(
      "va_choice", {"hangup": [r"\byes\b"]}, ask="?", cue_only=True,
      push_back=flows.push_back(reprompts=["again?"], fill="hangup"))
  slot["push_back"]["max"] = "two"           # not an int
  slot["push_back"]["say"] = 5               # not a str
  slot["push_back"]["end_conversation"] = 1  # not a bool
  slot["push_back"]["verbatim"] = "yes"      # not a bool
  f.add(slot)
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert any("push_back.max must be an integer" in e for e in errors), errors
  assert any("push_back.say must be a string" in e for e in errors), errors
  assert any(
      "push_back.end_conversation must be a boolean" in e for e in errors), errors
  assert any("push_back.verbatim must be a boolean" in e for e in errors), errors


def test_validator_rejects_empty_reprompts_with_no_disposition():
  # reprompts=[] is present-but-not-a-re-offer: the ladder exhausts at once, and
  # with no fill/then/end_conversation the slot re-asks itself forever. The old
  # `is not None` check let it through because the empty list is not None.
  f = flows.Flow("j", root_agent="a")
  slot = flows.intent_slot(
      "va_choice", {"hangup": [r"\byes\b"]}, ask="?", cue_only=True,
      push_back=flows.push_back(reprompts=["again?"], fill="hangup"))
  slot["push_back"]["reprompts"] = []   # no re-offer
  del slot["push_back"]["fill"]         # ...and no disposition
  f.add(slot)
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert any("push_back disposes of nothing" in e for e in errors), errors


def test_end_conversation_alone_is_a_valid_disposition():
  # A one-strike hang-up: no re-offer, end the leg on the first push. `end_conversation`
  # is a real disposition, so neither the DSL guard nor the validator should reject it.
  pb = flows.push_back(reprompts=[], end_conversation=True)
  assert pb["reprompts"] == [] and pb["end_conversation"] is True
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot(
      "va_choice", {"hangup": [r"\byes\b"]}, ask="?", cue_only=True, push_back=pb))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert not any("disposes of nothing" in e for e in errors), errors


# ── engine: the ladder re-offers, then disposes ────────────────────────────────
def _pb_slot():
  return {
      "name": "va_choice",
      "source": "user",
      "kind": "intent",
      "option_cues": {"hangup": [r"\byes\b"]},
      "setter": "",
      "verbatim": True,
      "push_back": {
          "reprompts": ["The assistant can help — shall I connect you instead?"],
          "max": 1,
          "say": "Alright, connecting you to an agent.",
          "then": {"tool": "transfer_to_human", "args": {"action": "transfer_to_agent"}},
          "end_conversation": True,
      },
  }


def _sm_awaiting(slot_name):
  sm = {"_awaiting": slot_name, "_turn_n": 1,
        "filled": {}, "pending": {slot_name: True}}
  # Mark the slot as asked on a PRIOR turn with the current progress signature, so the
  # next off-cue turn counts as a push-back rather than resetting.
  sm["_await_mark"] = {"slot": slot_name, "turn": 0,
                       "sig": engine._progress_sig(sm)}
  return sm


def test_push_back_reoffers_on_the_first_push():
  slots = [_pb_slot()]
  sm = _sm_awaiting("va_choice")
  sm["_turn_n"] = 1
  res = engine._push_back_tick(sm, slots, "no, I want a human", {})
  assert res is not None and res[0] == "reprompt"
  assert res[1] == "The assistant can help — shall I connect you instead?"
  assert res[2] is True  # verbatim honored


def test_push_back_disposes_after_max_pushes():
  slots = [_pb_slot()]
  sm = _sm_awaiting("va_choice")
  # First push re-offers (counter -> 1).
  sm["_turn_n"] = 1
  assert engine._push_back_tick(sm, slots, "no", {})[0] == "reprompt"
  # Second push exhausts (max=1) -> dispose: fire the tool + end the leg.
  sm["_turn_n"] = 2
  sm["_await_mark"] = {"slot": "va_choice", "turn": 1,
                       "sig": engine._progress_sig(sm)}
  res = engine._push_back_tick(sm, slots, "still no", {})
  assert res is not None and res[0] == "dispose"
  _, say, fc, end, _vb = res
  assert say == "Alright, connecting you to an agent."
  assert fc == {"name": "transfer_to_human",
                "args": {"action": "transfer_to_agent"}}
  assert end is True


def test_push_back_ignores_silence():
  # Silence is no_input's job, not push_back's.
  slots = [_pb_slot()]
  sm = _sm_awaiting("va_choice")
  assert engine._push_back_tick(sm, slots, "", {}) is None


def test_cue_only_slot_accept_still_fills_end_to_end():
  """An accept cue fills the cue-only slot through the real engine — no setter needed."""
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot(
      "va_choice", {"hangup": [r"\byes\b", r"\bsure\b"]},
      ask="Would you like the Verizon Assistant?", cue_only=True,
      push_back=flows.push_back(
          reprompts=["shall I connect you?"], max=1,
          then={"tool": "transfer_to_human", "args": {}}, end_conversation=True)))
  app = flows.App(root_flow=f, app_display_name="t")
  assert flows.validate_app(app)[0] == []
  config = app.root_flow.to_config()
  eng = fb.load_engine()
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "j"
    sm["filled"][gate] = "j"
  eng.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "yes please",
      "scanned_user_text": "yes please", "is_inactivity": False, "event_data": {},
      "config_id": "j", "n_user_turns": 1,
  })
  assert sm["filled"].get("va_choice") == "hangup"
