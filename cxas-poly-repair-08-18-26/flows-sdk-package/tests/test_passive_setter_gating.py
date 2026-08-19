"""A passive slot's setter is callable only while the slot is actually reachable.

`passive` means "does not hold the turn" — the caller may volunteer it whenever it is
relevant. It does NOT mean "callable regardless of gating", but that is what the engine
did: an un-hide meant for the cancel setter ("the user can cancel on any turn") ran over
EVERY passive slot and resurrected setters the hiding loop had just hidden for having a
false condition.

The result was a tool in the function-calling schema that the prompt never mentions,
because TOOL SELECTION filters on active. Live, on an ACTIVATION call, a caller who
asked to change their plan was answered with an invented request to consent to CPNI
access — a phrase that appears nowhere in the app — and the model recorded it by calling
`set_waiver_consent`, a fee-waiver slot from the billing journey gated on a bill-explain
intent that was not in play. The engine rejected the value because the slot was
inactive, and the caller dead-ended on "Could you try that again?".

Cancel keeps working because a control slot carries no condition and no requires, so it
is always active — it passes the gate on its own terms rather than by exemption.
"""

from __future__ import annotations

import flows
from flows.engine import loader as fb


def _config():
  """An activation-ish flow with a passive slot fenced off behind another journey."""
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot(
      "journey",
      {"activation": [r"\bactivate\b"], "billing": [r"\bmy bill\b"]},
      ask="What are you calling about?"))
  f.add(flows.user_slot("last_four", ask="Last four?", requires=["journey"],
                        condition={"slot": "journey", "eq": "activation"}))
  # Reachable ONLY in the billing journey — the shape of the fee-waiver consent.
  f.add(flows.intent_slot(
      "waiver_consent", {"yes": [r"\byes\b"], "no": [r"\bno\b"]}, passive=True,
      requires=["journey"], condition={"slot": "journey", "eq": "billing"}))
  f.set("cancel", flows.cancel(say="No problem."))
  app = flows.App(root_flow=f, app_display_name="t")
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


def _setter_of(config, slot):
  return next(s.get("setter") for s in config["slots"] if s["name"] == slot)


def _menu(action):
  si = action.get("si") or ""
  return si.split("3. TOOL SELECTION:", 1)[-1].split("4. ORDERING", 1)[0]


def test_a_passive_setter_for_another_journey_is_not_callable():
  config = _config()
  engine = fb.load_engine()
  sm = _sm(config)
  action = _turn(engine, config, sm, "activate my phone", 1)
  assert sm["filled"]["journey"] == "activation"

  setter = _setter_of(config, "waiver_consent")
  hidden = action.get("hide_tools") or []
  assert setter in hidden, (
      "a passive slot gated to a journey the caller is not in must not be callable")
  # The invariant that actually matters, and the one that was violated: never leave a
  # tool callable that the prompt does not advertise. Either both or neither.
  assert setter not in _menu(action)


def test_the_passive_setter_becomes_callable_in_its_own_journey():
  """The un-hide still does its job — this is a gate, not a removal."""
  config = _config()
  engine = fb.load_engine()
  sm = _sm(config)
  action = _turn(engine, config, sm, "my bill is wrong", 1)
  assert sm["filled"]["journey"] == "billing"

  assert _setter_of(config, "waiver_consent") not in (action.get("hide_tools") or [])


def test_cancel_stays_callable_on_every_turn():
  """What the un-hide was written for. A control slot has no condition and no
  requires, so it is always active and passes the gate on its own terms."""
  config = _config()
  engine = fb.load_engine()
  sm = _sm(config)
  for n, text in enumerate(["activate my phone", "9413"], start=1):
    action = _turn(engine, config, sm, text, n)
    assert "cancel_flow" not in (action.get("hide_tools") or []), (
        f"cancel must survive turn {n} ({text!r})")


def test_the_correction_focus_pass_applies_the_same_gate():
  """`_correction_focus_directive` builds its own hide list and had its own copy of
  the blanket exemption, so fixing `_compute_hidden_tools` alone left the leak open on
  exactly the turns a correction is being collected."""
  config = _config()
  config["correction_tool"] = "set_slot_change"
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "activate my phone", 1)
  sm["filled"]["last_four"] = "9413"
  sm.update(fb.run_intake(
      "set_slot_change", {"slots": ["last_four"], "success": True}, sm)["sm"])

  action = _turn(engine, config, sm, "", 2)
  assert "<correction_focus>" in (action.get("si") or ""), "focus must be armed"
  hidden = action.get("hide_tools") or []
  assert _setter_of(config, "waiver_consent") in hidden, (
      "a passive setter from another journey must stay hidden mid-correction")
  # The focus setter is still exposed — that is what the pass is for.
  assert _setter_of(config, "last_four") not in hidden


def test_an_unresolvable_requires_hides_the_setter_rather_than_raising():
  """The gate decides tool VISIBILITY, so it fails CLOSED.

  A `requires` naming a slot that does not exist is rejected by the validator, so this
  should be unreachable — but a config can reach the engine without having been through
  it, and an exception on a visibility decision drops the call. Hiding the setter costs
  nothing by comparison.
  """
  engine = fb.load_engine()
  slots = [{"name": "p", "passive": True, "setter": "set_p",
            "requires": ["does_not_exist"]}]
  hidden = engine._compute_hidden_tools(  # noqa: SLF001
      slots, {}, {}, [], {n["name"]: n for n in slots})
  assert "set_p" in hidden
