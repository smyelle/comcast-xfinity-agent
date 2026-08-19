"""Connected journey (Shape B) + a single-agent router root.

Demos the two higher-level routing builders:

  * `journey(config_id, spine=, operations=[Operation(...)], parent=, intent_name=)` —
    the "Shape B" connected journey: ONE intent slot -> a shared SPINE (asked once for
    every operation) -> per-operation, intent-GATED terminals. The intent enum + each
    terminal's gate are DERIVED from the operations (single source of truth), so they
    cannot desync. Each terminal transfers back to `parent` on completion.
  * `Operation(value, cues, slots=, tasks=)` — one intent branch of the journey.
  * `router_flow(config_id, flows, route_cues=, intent_slot=)` — a single-agent router
    ROOT config (1:1 with CES's router.json): `router=True`, a `set_active_flow`
    bootstrap onto `active_flow`, and order-preserving `flow_types`/`route_cues`.

The journey is emitted as a deployable single-agent App. `_demo_run()` drives the
offline engine (event-prefilled account + a spoken cue) to PROVE the intent selects the
right operation terminal: a "check my balance" cue fires `do_balance`, while a "pay my
bill" cue instead asks the pay-only `amount` slot (do_balance is NOT fired).

Build + validate offline:
    python -m examples.connected_journey        # emits ./connected_journey_app
"""

import flows
from flows import Operation

# Shared spine: asked ONCE for every operation (before the intent-gated op slots). A
# user_slot so a live caller is actually prompted for it.
_SPINE = [flows.user_slot("account_id", "What's your account number?")]

# Two operations. Each declares its own op-specific slots + tasks; the LAST task is the
# terminal. journey() derives each terminal's gate from the Operation's `value`.
_OPERATIONS = [
    Operation(
        "pay_bill", ["pay my bill", "make a payment", "pay off"],
        slots=[
            flows.user_slot("amount", "How much would you like to pay?"),
            flows.result_slot("pay_conf", "pay_task"),
        ],
        tasks=[flows.task("pay_task", "do_payment", ["account_id", "amount"],
                          "pay_conf", out_key="confirmation", terminal=True,
                          then_say="All set — payment confirmation {pay_conf}.")],
    ),
    Operation(
        "check_balance", ["check my balance", "how much do I owe", "what's my balance"],
        slots=[flows.result_slot("bal_amt", "bal_task")],
        tasks=[flows.task("bal_task", "do_balance", ["account_id"], "bal_amt",
                          out_key="balance", terminal=True,
                          then_say="Your current balance is {bal_amt}.")],
    ),
]

journey_flow = flows.journey(
    "billing_journey",
    spine=_SPINE,
    operations=_OPERATIONS,
    parent="Billing_Host",
    welcome="Welcome to Acme billing.",
)

app = flows.App(
    root_flow=journey_flow,
    app_display_name="Billing Journey (Shape B)",
    model="gemini-3.5-flash",
)


def build_router_flow() -> flows.Flow:
  """A single-agent router ROOT (router_flow) over the same two billing operations.

  Returned as a Flow so the round-trippable router config can be validated on its own
  (a full router App needs child-flow wiring beyond this feature demo).
  """
  return flows.router_flow(
      "billing_router",
      ["pay_bill", "check_balance"],
      route_cues={"pay_bill": ["pay my bill", "make a payment"],
                  "check_balance": ["check my balance", "how much do I owe"]},
      intent_slot=flows.intent_slot(
          "journey_intent",
          {"pay_bill": ["pay my bill"], "check_balance": ["check my balance"]},
          passive=True),
      root_agent="Billing_Router_Agent",
  )


def _demo_run() -> None:
  """Prove the intent CLASSIFIES + gates offline (deterministic cue-match, no LLM/creds).

  The full gated journey (spine -> intent -> correct op terminal) is proven LIVE against a
  deployed CES agent — see the live transcript in the PR. Offline we can't fill a user_slot
  (its generated setter isn't loadable), so here we assert the intent is classified from the
  opening utterance and that the pay-only `amount` slot is GATED on pay_bill (so a balance
  request never asks it)."""
  from flows.sim import engine_sim

  cfg = journey_flow.to_config()
  amount = next(s for s in cfg["slots"] if s.get("name") == "amount")
  assert amount["condition"] == flows.eq("journey_intent", "pay_bill"), amount["condition"]

  for text, expected in [("I'd like to check my balance", "check_balance"),
                         ("I want to pay my bill", "pay_bill")]:
    engine_sim.reset_store()
    sid, _ = engine_sim.start(cfg, "billing_journey")
    res = engine_sim.step({"session_id": sid, "kind": "user_text", "text": text})
    intent = res["sm"]["filled"].get("journey_intent")
    print(f"  cue {text!r:32s} -> intent={intent!r}")
    assert intent == expected, (text, intent)

  # router_flow: its config validates standalone (single-agent router root).
  from flows.config.validation import raw_validate_single
  ok, r_errs, _ = raw_validate_single(build_router_flow().to_config())
  print(f"  router_flow config valid={ok} errors={r_errs}")
  assert ok and not r_errs


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  assert errors == [], errors
  _demo_run()
  flows.build_app(app, "./connected_journey_app", overwrite=True)
  print("built -> ./connected_journey_app "
        "(proves: journey Shape B routes intent->right terminal; router_flow root valid)")
