"""Regression lock for #517 — intent-first must not talk over a silent caller.

In a multi-agent, `intent_first=True` app, a caller who says "hold on" then goes
silent was talked over: every `<context>no user activity>` turn was answered aloud
instead of running the quiet HOLD-mode silent-tick ladder (`hold_reprompts`).

The break was NOT in inactivity detection (before_model correctly blanks the turn
and sets `is_inactivity=True`) but INSIDE the engine's intent-first Pass-B path:
`effective_user_text` fell back to `scanned_user_text` (the last real utterance,
"hold on") whenever `last_user_text` was empty — even on a genuine silence turn —
so `_run_slot_filling` read silence as re-engaged speech and skipped the
`elif is_inactivity` no_input silent-tick branch.

`engine_sim` could not reproduce it because the offline loader never passed
`scanned_user_text`, so the fallback was a no-op there. These tests supply
`scanned_user_text` exactly as the live before_model does — the one input that
makes the two-pass runtime path observable offline.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows.engine import loader as fb  # noqa: E402
from flows.sim import engine_sim  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)


# hold_reprompts: two silent ticks, then one gentle spoken check-in.
HOLD_REPROMPTS = ["", "", "Take your time."]
CHECK_IN = "Take your time."


def _cfg(gate=False):
  """A one-question tracking flow under intent_first with a HOLD-mode no_input."""
  cfg = {
      "slots": [{
          "name": "tracking_number", "source": "user",
          "setter": "set_tracking_number",
          "ask": "What's your tracking number?",
      }],
      "tasks": [],
      "gate_slot": "active_flow" if gate else None,
      # bootstrap.intent_first is what sets sm["_intent_first"] on the first engine
      # run — the flag whose fallback the bug rode on.
      "bootstrap": {"intent_first": True, "tool": "set_active_flow",
                    "slot": "active_flow"},
      "no_input": {
          "reprompts": ["Sorry, what's the tracking number?"],
          "hold_phrases": ["hold on", "give me a second", "one moment"],
          "hold_reprompts": list(HOLD_REPROMPTS),
          "on_exhaust": {"say": "Let me get someone to help."},
      },
  }
  return cfg


def _fresh(cfg, gate=False):
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  if gate:
    sm["filled"]["active_flow"] = "tracking"
    sm["active_flow"] = "tracking"
  return sm


def _drive(engine, cfg, sm, text, n, inactivity=False, scanned=None):
  """One engine turn with the live before_model input shape.

  `scanned` defaults to `text`; on a silence turn the caller passes the PRIOR real
  utterance (what before_model scans from history) while `text` is empty — exactly
  the shape that triggered #517.
  """
  scanned = text if scanned is None else scanned
  return engine.slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": scanned, "is_inactivity": inactivity,
      "event_data": {}, "config_id": "t", "n_user_turns": n,
  })["action"]


# --------------------------------------------------------------------------- #
# 1. The observed bug (fix #1): silence after "hold on" stays quiet.

def test_silence_after_hold_runs_the_silent_tick_not_the_model():
  """The #517 repro: hold, then go silent with the prior utterance in history."""
  engine = fb.load_engine()
  cfg = _cfg()
  sm = _fresh(cfg)
  # Entry turn — the flow asks its first question (sets _awaiting=tracking_number).
  _drive(engine, cfg, sm, "", n=0)
  # Turn 1 — the caller asks for a moment. Hold mode latches.
  _drive(engine, cfg, sm, "hold on", n=1)
  assert sm.get("_hold_on") is True

  # Turn 2 — genuine silence. last_user_text is blanked (before_model), but the
  # prior real utterance is still in history -> scanned_user_text="hold on".
  out = _drive(engine, cfg, sm, "", n=1, inactivity=True, scanned="hold on")
  # The silent-tick branch must own this turn: empty reprompt, model suppressed.
  assert sm.get("_no_input_counter") == 1
  assert (out.get("message") or "") == ""
  assert out.get("preempt") is True


def test_hold_silence_ladder_advances_then_checks_in():
  """Two quiet ticks, then the one gentle spoken check-in — the HOLD ladder."""
  engine = fb.load_engine()
  cfg = _cfg()
  sm = _fresh(cfg)
  _drive(engine, cfg, sm, "", n=0)
  _drive(engine, cfg, sm, "hold on", n=1)
  assert sm.get("_hold_on") is True

  out1 = _drive(engine, cfg, sm, "", n=1, inactivity=True, scanned="hold on")
  assert sm.get("_no_input_counter") == 1 and (out1.get("message") or "") == ""

  out2 = _drive(engine, cfg, sm, "", n=1, inactivity=True, scanned="hold on")
  assert sm.get("_no_input_counter") == 2 and (out2.get("message") or "") == ""

  out3 = _drive(engine, cfg, sm, "", n=1, inactivity=True, scanned="hold on")
  assert sm.get("_no_input_counter") == 3
  assert out3.get("message") == CHECK_IN
  assert out3.get("preempt") is True


def test_real_reengagement_still_resets_hold_window():
  """Guardrail: a real utterance after holding is NOT silence — it re-engages."""
  engine = fb.load_engine()
  cfg = _cfg()
  sm = _fresh(cfg)
  _drive(engine, cfg, sm, "", n=0)
  _drive(engine, cfg, sm, "hold on", n=1)
  _drive(engine, cfg, sm, "", n=1, inactivity=True, scanned="hold on")
  assert sm.get("_no_input_counter") == 1
  # Caller speaks again (not inactivity): the silence window resets.
  _drive(engine, cfg, sm, "still looking", n=2, scanned="still looking")
  assert not sm.get("_no_input_counter")


# --------------------------------------------------------------------------- #
# 2. The defensive guard (fix #2): an inactivity turn is never classified.

def test_inactivity_turn_is_never_routed_to_pass_a_classify():
  """Under a filled gate, silence must go straight to Pass B (no_input), even when
  pass_state/n_user_turns would otherwise select the Pass-A classifier."""
  engine = fb.load_engine()
  cfg = _cfg(gate=True)
  sm = _fresh(cfg, gate=True)
  # Pre-arm the exact state at the start of a silence turn: holding, awaiting the
  # tracking number, and a pass_state whose turn != this turn's n_user_turns (which
  # WITHOUT the guard selects Pass A -> classify -> model, not the no_input tick).
  sm["_hold_on"] = True
  sm["_awaiting"] = "tracking_number"
  sm["_intent_pass"] = {"turn": 1, "intent": "continue"}

  out = _drive(engine, cfg, sm, "", n=2, inactivity=True, scanned="hold on")

  # Guard held: no classify pass, and the silent-tick ladder actually ran.
  assert sm.get("_classify_mode") is False
  assert sm.get("_no_input_counter") == 1
  assert (out.get("message") or "") == ""


# --------------------------------------------------------------------------- #
# 3. Blind-spot closure: engine_sim now reproduces the scenario (scanned_user_text
#    threaded through the offline loader), so this class is catchable in sim.

def test_engine_sim_keeps_silence_quiet_after_hold():
  engine_sim.reset_store()
  cfg = _cfg()
  sid, _ = engine_sim.start(cfg, flow_id="t")
  engine_sim.step({"session_id": sid, "kind": "user_text", "text": "hold on"})
  res = engine_sim.step({"session_id": sid, "kind": "user_text",
                         "text": "", "is_inactivity": True})
  # The sim carried "hold on" forward as scanned_user_text; with the fix the tick
  # is silent (no agent speech) and the counter advanced.
  assert res["sm"].get("_no_input_counter") == 1
  assert (res.get("agent_text") or "") == ""
