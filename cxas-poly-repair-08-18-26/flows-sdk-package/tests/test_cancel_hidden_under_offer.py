"""Silence-armed escalation OFFER must hide cancel_flow (abandon footgun).

When sustained silence exhausts the `no_input` ladder and `on_exhaust.open_slot`
arms an in-flow escalation OFFER, the engine must HIDE `cancel_flow` from the
model's visible tools while that offer is pending — so the model can't abandon the
caller instead of accepting the offer or transferring. `transfer_to_human` and the
offer's own setter stay visible. This mirrors the Acme shape (acme_agent.py).

Runs fully offline via `flows.sim.engine_sim` (no LLM / no creds).

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_cancel_hidden_under_offer.py
"""

from __future__ import annotations

import flows
from flows.sim import engine_sim


def _build_config() -> dict:
  """A minimal flow that arms an escalation OFFER on silence (Acme shape)."""
  f = flows.Flow(
      "offer_test",
      root_agent="Offer_Agent",
      bootstrap={"welcome_slot": "welcome"},
  )
  f.set("no_input", flows.hold_and_wait(
      reprompts=[
          "Sorry, I didn't catch that. Go ahead whenever you're ready.",
          "I still didn't hear anything. Take your time, then go ahead.",
      ],
      offer_slot="escalation_offered",
      say="Okay, I'll let you go for now. Thanks for calling.",
  ))
  f.set("escalate", {"say": "Okay, connecting you with a representative now."})
  f.set("cancel", {"say": "No problem. Let me know if there's anything else."})
  f.add(
      flows.announce("welcome", ["Sure, I can help you track that."], shared=True),
      # In-flow escalation offer flag (armed by silence via open_slot).
      {"name": "escalation_offered", "source": "user",
       "hint": "internal flag: escalation offer armed",
       "setter": "set_escalation_offered", "condition": "lambda f: False"},
      flows.user_slot(
          "escalation_choice",
          "I can connect you with a representative now, or you can call back "
          "once you have your number. Which would you prefer?",
          condition="lambda f: f.get('escalation_offered')",
          reprompts=[
              "Would you like an agent now, or to call back later?",
              "Which works better for you?",
          ]),
      flows.announce(
          "esc_agent", ["Connecting you with a representative now."],
          condition=flows.eq("escalation_choice", "agent"),
          requires=["escalation_choice"], end=True, escalated=True,
          reason="transfer"),
      flows.announce(
          "esc_callback", ["No problem, please call us back later."],
          condition=flows.eq("escalation_choice", "callback"),
          requires=["escalation_choice"], end=True),
      flows.user_slot(
          "tracking_number", "What's your tracking number?",
          reprompts=[
              "Sorry, I didn't catch that. What's your tracking number?",
              "One more time. Please read me your tracking number.",
          ]),
      flows.announce(
          "goodbye", ["Thanks for choosing Acme. Have a great day."],
          requires=["tracking_number"], end=True),
  )
  return f.to_config()


def _drive_until_offer(config: dict) -> dict:
  """Open a session, then drive silence steps until the OFFER slot is pending.

  Returns the step result on the turn the offer is presented.
  """
  engine_sim.reset_store()
  session_id, result = engine_sim.start(config, "offer_test", channel="audio")
  # The opening turn asks tracking_number; sustained silence walks the reprompt
  # ladder (2 rungs) then exhausts on the 3rd tick, arming the escalation offer.
  # Detect the offer turn by the AWAITED slot (fix-independent), so this drives
  # to the same point against both pre-fix and post-fix engines — the assertion
  # in the test, not this loop, is what distinguishes them.
  for _ in range(6):
    result = engine_sim.step({
        "session_id": session_id,
        "kind": "user_text",
        "text": "",
        "is_inactivity": True,
    })
    if (result["sm"].get("_awaiting") == "escalation_choice"
        and result["sm"].get("filled", {}).get("escalation_offered")):
      return result
  raise AssertionError(
      "offer slot was never armed by silence; "
      f"last sm markers: _awaiting={result['sm'].get('_awaiting')!r}, "
      f"_no_input_counter={result['sm'].get('_no_input_counter')!r}")


def test_cancel_flow_hidden_while_offer_pending():
  """On the silence-armed offer turn, cancel_flow is hidden; transfer stays."""
  config = _build_config()
  result = _drive_until_offer(config)

  hide_tools = result["hide_tools"]
  # The abandon footgun is removed for this turn ...
  assert "cancel_flow" in hide_tools, (
      f"cancel_flow should be hidden on the offer turn; hide_tools={hide_tools}")
  # ... but the caller can still be transferred to a human.
  assert "transfer_to_human" not in hide_tools, (
      f"transfer_to_human must stay visible; hide_tools={hide_tools}")
  # The offer slot itself is the one being awaited (armed by silence).
  assert result["sm"].get("_awaiting") == "escalation_choice"
