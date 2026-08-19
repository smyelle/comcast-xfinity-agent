"""Mid-flow topic changes: `switchable` intent slots and menu-returning cancel.

Both exist because of the same observed failure. A caller halfway through one journey
says something that plainly belongs to another — "actually, why is my bill so high" while
being asked for their phone number — and the engine has no way to act on it. The intent
slot is already filled, so the cue never matches; the only escape hatch, cancel, tears the
whole conversation down.

These tests drive the real engine through the offline loader, the same way
`test_async_tools.py` does, so they exercise the shipped code path rather than a model of
it.
"""

from __future__ import annotations

import pytest

import flows
from flows.engine import loader as fb


ORDER_CUES = {"activation": [r"\bactivate\b"], "billing": [r"\bmy bill\b"]}


def _flow(*, switchable: bool, cancel_returns: bool = False):
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot(
      "journey", ORDER_CUES, ask="What are you calling about?",
      switchable=switchable))
  # One data slot per journey, each gated on the intent AND declaring it in `requires` —
  # which is what makes it downstream, and therefore clearable.
  f.add(flows.user_slot(
      "last_four", ask="Last four digits?", requires=["journey"],
      condition={"slot": "journey", "eq": "activation"}))
  # Downstream by `requires` with NO condition naming the intent. This is the slot that
  # actually proves the abandonment: the engine independently prunes slots whose
  # CONDITION has gone false, so a condition-gated slot disappears either way and
  # asserting on it passes for the wrong reason.
  f.add(flows.user_slot("note", ask="Anything else about it?", requires=["journey"]))
  # Runs BEFORE the intent is chosen and is not downstream of it — an authentication or
  # eligibility lookup. Its result must survive an abandonment: re-running it is not
  # just slow, a task with a side effect would fire twice.
  # Downstream of the intent: consumes a slot the abandonment clears.
  f.task(flows.task("lookup", "do_lookup", ["note"], "lookup_result", out_key="ok"))
  f.add(flows.result_slot("lookup_result", "lookup"))
  f.add(flows.event_slot("caller_id"))
  f.task(flows.task("auth", "check_auth", ["caller_id"], "authed", out_key="ok"))
  f.add(flows.result_slot("authed", "auth"))
  f.add(flows.announce(
      "bill_line", ["Your bill went up."], requires=["journey"],
      condition={"slot": "journey", "eq": "billing"}))
  if cancel_returns:
    f.set("cancel", flows.cancel(
        say="No problem.", end_conversation=False, clear_slots=["journey"]))
  else:
    f.set("cancel", flows.cancel(say="No problem."))
  return f


def _config(**kw):
  app = flows.App(root_flow=_flow(**kw), app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert not errors, errors
  return app.root_flow.to_config()


def _sm(config):
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "j"
    sm["filled"][gate] = "j"
  return sm


def _turn(engine, config, sm, text, n):
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False, "event_data": {},
      "config_id": "j", "n_user_turns": n,
  })["action"]


def _spoken(action):
  parts = [action.get("message") or ""]
  for part in action.get("response") or []:
    if isinstance(part, dict) and part.get("type") == "text":
      parts.append(part.get("text") or "")
  return " ".join(p for p in parts if p).strip()


# ── switchable ───────────────────────────────────────────────────────────────

def test_switch_rewrites_the_intent_and_clears_the_abandoned_journey():
  config = _config(switchable=True)
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  assert sm["filled"]["journey"] == "activation"
  # Answer the activation questions, so there is state to lose.
  sm["filled"]["last_four"] = "9413"
  sm["filled"]["note"] = "cracked screen"
  sm.setdefault("task_results", {})["lookup"] = {"ok": True}
  sm["task_results"]["auth"] = {"ok": True}

  _turn(engine, config, sm, "actually why is my bill so high", 2)
  assert sm["filled"]["journey"] == "billing"
  # The abandoned journey's data must not survive: a DAG still holding these while the
  # caller asks about billing answers a question nobody asked any more.
  assert "note" not in sm["filled"], "downstream-by-requires must be cleared"
  assert "last_four" not in sm["filled"]
  # A task that ran for the abandoned journey has to be free to run again on return...
  assert "lookup" not in sm.get("task_results", {})
  # ...but one that ran BEFORE the intent was chosen must be left alone.
  assert sm["task_results"].get("auth") == {"ok": True}


def test_without_switchable_the_intent_is_frozen_once_filled():
  """The default, and the behaviour every existing agent relies on."""
  config = _config(switchable=False)
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  sm["filled"]["last_four"] = "9413"
  sm["filled"]["note"] = "cracked screen"

  _turn(engine, config, sm, "actually why is my bill so high", 2)
  assert sm["filled"]["journey"] == "activation"
  assert sm["filled"]["last_four"] == "9413"
  assert sm["filled"]["note"] == "cracked screen"


def test_switching_back_re_announces():
  """An announce already 'said' for a journey must fire again on return.

  Announce state lives in `filled`, so without clearing it the caller is walked back
  through the journey in silence.
  """
  config = _config(switchable=True)
  engine = fb.load_engine()
  sm = _sm(config)
  # The announce cascades onto the turn that fills the intent.
  first = _spoken(_turn(engine, config, sm, "why is my bill so high", 1))
  assert "Your bill went up." in first
  _turn(engine, config, sm, "actually I want to activate my phone", 2)
  assert sm["filled"]["journey"] == "activation"

  again = _spoken(_turn(engine, config, sm, "no wait, my bill", 3))
  assert sm["filled"]["journey"] == "billing"
  assert "Your bill went up." in again


def test_an_ambiguous_utterance_does_not_switch():
  """Two matching values leave the slot alone — the existing ambiguity guard.

  This is the property that makes `switchable` safe to turn on: the cost of a false
  positive is discarding a journey the caller was halfway through.
  """
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot(
      "journey", {"activation": [r"\bactivate\b"], "billing": [r"\bactivate\b"]},
      ask="?", switchable=True))
  config = flows.App(root_flow=f, app_display_name="t").root_flow.to_config()
  engine = fb.load_engine()
  sm = _sm(config)
  sm["filled"]["journey"] = "billing"
  _turn(engine, config, sm, "activate", 2)
  assert sm["filled"]["journey"] == "billing"


def test_restating_the_same_intent_is_not_a_switch():
  config = _config(switchable=True)
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  sm["filled"]["note"] = "cracked screen"
  _turn(engine, config, sm, "yes, activate my phone", 2)
  assert sm["filled"]["note"] == "cracked screen", "a restatement must not discard progress"


# ── menu-returning cancel ────────────────────────────────────────────────────

def test_cancel_can_return_to_the_menu_instead_of_ending_the_call():
  config = _config(switchable=False, cancel_returns=True)
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  sm["filled"]["note"] = "cracked screen"

  sm.setdefault("pending", {})["cancel"] = True
  action = _turn(engine, config, sm, "never mind", 2)

  assert not any((p or {}).get("type") == "end_session"
                 for p in (action.get("response") or [])), "the call must stay up"
  assert sm.get("status") != "zombie"
  assert "journey" not in sm["filled"], "the journey must be un-decided"
  assert "note" not in sm["filled"]
  said = _spoken(action)
  # The acknowledgement and the menu question land on ONE turn, so backing out does not
  # cost the caller an extra exchange — and IN THAT ORDER. Asserting only that both are
  # present passed while the live agent said "What are you calling about? No problem."
  assert said.startswith("No problem."), said
  assert said.index("No problem.") < said.index("What are you calling about?"), said


def test_menu_returning_cancel_does_not_latch():
  """The cancel slot must be un-filled, or every later turn re-cancels."""
  config = _config(switchable=False, cancel_returns=True)
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  sm.setdefault("pending", {})["cancel"] = True
  _turn(engine, config, sm, "never mind", 2)
  assert "cancel" not in sm["filled"] and "cancel" not in sm.get("pending", {})

  _turn(engine, config, sm, "activate my phone", 3)
  assert sm["filled"]["journey"] == "activation", "the caller must be able to proceed"


def test_default_cancel_still_ends_the_conversation():
  """Unchanged for every app that has not opted in."""
  config = _config(switchable=False, cancel_returns=False)
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  sm.setdefault("pending", {})["cancel"] = True
  action = _turn(engine, config, sm, "never mind", 2)
  assert any((p or {}).get("type") == "end_session"
             for p in (action.get("response") or []))


@pytest.mark.parametrize("switchable", [True, False])
def test_a_flow_without_the_features_is_byte_identical(switchable):
  """The emitted config gains nothing unless the author asks for it."""
  config = _config(switchable=switchable)
  slot = next(s for s in config["slots"] if s["name"] == "journey")
  assert ("switchable" in slot) is switchable
  assert "end_conversation" not in (config.get("cancel") or {})


# ── switchable="defer": step away and come back ──────────────────────────────

def _defer_flow():
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot("journey", ORDER_CUES, ask="What are you calling about?",
                          switchable="defer"))
  f.add(flows.user_slot("last_four", ask="Last four digits?", requires=["journey"],
                        condition={"slot": "journey", "eq": "activation"}))
  f.add(flows.user_slot("note", ask="Anything else about it?", requires=["journey"]))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert not errors, errors
  return app.root_flow.to_config()


def _defer_flow_with_task():
  """A defer flow whose activation half runs a task, so task state can be parked too."""
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot("journey", ORDER_CUES, ask="What are you calling about?",
                          switchable="defer"))
  f.add(flows.user_slot("last_four", ask="Last four digits?", requires=["journey"],
                        condition={"slot": "journey", "eq": "activation"}))
  f.add(flows.user_slot("note", ask="Anything else about it?", requires=["journey"]))
  f.add(flows.result_slot("activation_result", "Activate"))
  f.task(flows.task("Activate", "activate_line", ["last_four"], "activation_result"))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert not errors, errors
  return app.root_flow.to_config()


def test_stepping_away_parks_the_journey_and_coming_back_restores_it():
  """"Hold on, what's my balance" then "back to the activation" is ordinary.

  Re-asking for a number the caller already gave reads as the agent having lost the
  thread — the retention failure an explorer run is built to find.
  """
  config = _defer_flow()
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  sm["filled"]["last_four"] = "9413"
  sm["filled"]["note"] = "cracked screen"

  _turn(engine, config, sm, "actually what about my bill", 2)
  assert sm["filled"]["journey"] == "billing"
  # Out of the way, so the billing half does not run on activation's answers...
  assert "last_four" not in sm["filled"]
  # ...but not thrown away.
  assert sm["_parked"]["activation"]["last_four"] == "9413"

  _turn(engine, config, sm, "okay, back to activate my phone", 3)
  assert sm["filled"]["journey"] == "activation"
  assert sm["filled"]["last_four"] == "9413", "the caller should not be asked again"
  assert sm["filled"]["note"] == "cracked screen"


def test_default_switchable_still_abandons():
  """`True` is for a caller who changed their mind; the old journey is finished."""
  config = _config(switchable=True)
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  sm["filled"]["note"] = "cracked screen"
  _turn(engine, config, sm, "actually what about my bill", 2)
  _turn(engine, config, sm, "back to activate my phone", 3)
  assert "note" not in sm["filled"]
  assert not sm.get("_parked")


def test_switchable_rejects_an_unknown_mode():
  with pytest.raises(ValueError, match="must be True or 'defer'"):
    flows.intent_slot("j", ORDER_CUES, ask="?", switchable="maybe")


def test_parking_takes_a_slot_s_retries_with_it():
  """Validation retries belong to the journey that accrued them.

  Left in place they are global. A slot used by BOTH journeys carries the first
  journey's failures into the second, so the second exhausts early and transfers a
  caller who has not actually failed at anything — and on return the first journey has
  lost the count it was owed. `note` is deliberately shared here (no condition), which
  is the shape where the leak bites.
  """
  config = _defer_flow()
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  # Two failed attempts at the shared slot while activation is live.
  sm.setdefault("_retries", {})["slot:note"] = 2

  _turn(engine, config, sm, "actually what about my bill", 2)
  assert sm["filled"]["journey"] == "billing"
  assert "slot:note" not in sm.get("_retries", {}), (
      "billing must start this slot clean, not two failures down")
  assert sm["_parked_retries"]["activation"]["note"] == 2

  _turn(engine, config, sm, "okay, back to activate my phone", 3)
  assert sm["_retries"].get("slot:note") == 2, (
      "returning to a journey should restore the count it was left at")


def test_parking_takes_a_failed_task_s_retries_with_it():
  """A task that only ever FAILED still belongs to the journey that ran it.

  The park loop used to key on `task_results`, so a task with retries and no result was
  skipped entirely and its count stayed global — the next journey started already part
  way through the retry budget of a task it had never run, and gave up early on the
  first real failure. No result is exactly the state a task that has failed every
  attempt is in, so the case the loop skipped is the one that accrues retries.
  """
  config = _defer_flow_with_task()
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  # Two failed attempts and NOTHING in task_results — the shape the old loop skipped.
  sm.setdefault("_retries", {})["Activate"] = 2
  assert "Activate" not in sm.get("task_results", {})

  _turn(engine, config, sm, "actually what about my bill", 2)
  assert sm["filled"]["journey"] == "billing"
  assert "Activate" not in sm.get("_retries", {}), (
      "billing must not inherit a retry count from a task activation ran")
  assert sm["_parked_task_retries"]["activation"]["Activate"] == 2

  _turn(engine, config, sm, "okay, back to activate my phone", 3)
  assert sm["_retries"].get("Activate") == 2, (
      "returning to a journey should restore the count it was left at")


def test_a_parked_task_keeps_its_result_and_its_retries_together():
  """Half-failed is the ordinary case: some attempts spent, then a result.

  Popping the result while discarding the count would forgive those attempts on return.
  """
  config = _defer_flow_with_task()
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  sm.setdefault("task_results", {})["Activate"] = {"success": True}
  sm.setdefault("_retries", {})["Activate"] = 1

  _turn(engine, config, sm, "actually what about my bill", 2)
  assert "Activate" not in sm.get("task_results", {})
  assert "Activate" not in sm.get("_retries", {})

  _turn(engine, config, sm, "okay, back to activate my phone", 3)
  assert sm["task_results"]["Activate"] == {"success": True}
  assert sm["_retries"].get("Activate") == 1


def test_a_falsy_parked_value_does_not_drop_the_caller_s_answers():
  """The park pops from `filled` UNCONDITIONALLY and stashes after.

  So a guard on truthiness rather than `is not None` does not merely skip the park —
  it loses the answers, because they are already out of `filled` by then. An intent
  value is a non-empty enum key today, so this is unreachable through the DAG; it is
  asserted directly because the failure mode is silent data loss.
  """
  config = _defer_flow()
  engine = fb.load_engine()
  sm = _sm(config)
  sm["filled"]["journey"] = "activation"
  sm["filled"]["note"] = "cracked screen"

  compiled = engine._compile_config(config)              # noqa: SLF001
  for falsy in (False, 0, ""):
    scratch = dict(sm)
    scratch["filled"] = dict(sm["filled"])
    engine._park_journey(scratch, compiled, "journey", falsy)   # noqa: SLF001
    assert scratch["_parked"][str(falsy)]["note"] == "cracked screen", (
        f"answers vanished when the parked value was {falsy!r}")
