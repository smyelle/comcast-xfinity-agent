"""A declined control-block request has to be remembered.

`escalate(condition=...)` can refuse a hand-off, which is right when no human can help —
an area outage does not get better because someone joins the call. But the commonest
reason to decline is the opposite: contain the FIRST request and honour the second, which
is what a value-first deflection is.

That could not be written. A declined request was dropped with nothing recorded, so the
condition read identically on the next ask; a gate on anything the flow does not otherwise
change deflected forever and the caller never reached a person. Counting declines gives
the condition something that moves.
"""

from __future__ import annotations

import pytest

import flows
from flows.engine import loader as fb

# ── declined requests are remembered ─────────────────────────────────────────

def _contain_once_flow():
  """The commonest reason to decline: contain the first ask, honour the second."""
  f = flows.Flow("c", root_agent="a")
  f.add(flows.user_slot("topic", ask="What can I help with?"))
  f.set("escalate", flows.escalate(
      say="Putting you through now.",
      declined_say="Let me try to help first.",
      # Nothing else in the flow changes this; the counter is what makes it openable.
      condition={"slot": "escalate_declined", "gte": 1},
  ))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert not errors, errors
  return app.root_flow.to_config()


def _ask_for_a_human(engine, config, sm, n):
  sm.setdefault("pending", {})["escalate"] = True
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "", "scanned_user_text": "",
      "is_inactivity": False, "event_data": {}, "config_id": "c", "n_user_turns": n,
  })["action"]


def test_contain_the_first_request_and_honour_the_second():
  config = _contain_once_flow()
  engine, sm = fb.load_engine(), fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "c"
    sm["filled"][gate] = "c"

  first = _ask_for_a_human(engine, config, sm, 1)
  assert "Let me try to help first." in (first.get("message") or "")
  assert sm["filled"]["escalate_declined"] == 1

  # Without the counter the condition reads the same here and the caller is deflected
  # forever — they can never reach a person at all.
  second = _ask_for_a_human(engine, config, sm, 2)
  assert "Putting you through now." in (second.get("message") or "")


def test_a_condition_that_stays_false_still_declines_every_time():
  """Recording the decline must not turn every gate into a two-strike one."""
  f = flows.Flow("c", root_agent="a")
  f.add(flows.user_slot("topic", ask="What can I help with?"))
  f.set("escalate", flows.escalate(
      say="Putting you through now.", declined_say="No agents can help with that.",
      condition={"slot": "topic", "eq": "__never__"}))
  config = flows.App(root_flow=f, app_display_name="t").root_flow.to_config()
  engine, sm = fb.load_engine(), fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "c"
    sm["filled"][gate] = "c"
  for n in (1, 2, 3):
    action = _ask_for_a_human(engine, config, sm, n)
    assert "No agents can help with that." in (action.get("message") or "")
  assert sm["filled"]["escalate_declined"] == 3


def test_the_counter_names_are_reserved():
  """An author cannot declare a slot the engine is going to overwrite.

  `<block>_declined` holds an int the engine writes on every refusal. A slot declared
  under the same name would be silently clobbered with it — worse than a shadowed value,
  because the type changes underneath whatever the author expected.
  """
  for name in ("cancel_declined", "escalate_declined"):
    f = flows.Flow("t", root_agent="a")
    f.add(flows.user_slot(name, ask="?"))
    errors, _ = flows.validate_app(flows.App(root_flow=f, app_display_name="x"))
    assert any("reserved" in e for e in errors), (name, errors)


def _ladder_flow(declined, gte):
  f = flows.Flow("c", root_agent="a")
  f.add(flows.user_slot("problem", ask="What's wrong?"))
  f.set("escalate", flows.escalate(
      say="Putting you through now.", declined_say=declined,
      condition={"slot": "escalate_declined", "gte": gte}))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert not errors, errors
  return app.root_flow.to_config()


def _drive_asks(config, n):
  engine, sm = fb.load_engine(), fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "c"
    sm["filled"][gate] = "c"
  out = []
  for i in range(1, n + 1):
    out.append(_ask_for_a_human(engine, config, sm, i).get("message") or "")
  return out


def test_declined_say_may_be_a_ladder():
  """The same sentence twice reads as the agent not listening."""
  said = _drive_asks(_ladder_flow(["First try.", "One more thing."], gte=2), 3)
  assert said == ["First try.", "One more thing.", "Putting you through now."]


def test_the_ladder_clamps_rather_than_falling_silent():
  """Where `while_waiting` drains to silence, a refusal must always answer.

  A hold going quiet is fine — the caller is waiting, not asking. A declined request is
  a direct question, and hearing nothing back is the worst available response.
  """
  said = _drive_asks(_ladder_flow(["First.", "Second."], gte=99), 4)
  assert said == ["First.", "Second.", "Second.", "Second."]


def test_an_empty_ladder_is_an_authoring_error():
  """`declined_say=[]` says nothing; omitting it entirely is how you mean that."""
  with pytest.raises(ValueError, match="says nothing"):
    flows.escalate(say="x", declined_say=[], condition={"slot": "escalate_declined",
                                                        "gte": 1})
