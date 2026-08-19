"""`sets` is what turns a pile of announces into a LADDER — one verdict, not four.

An order-problems line looks the order up and finds, quite normally, that several things
are wrong at once: the card was declined, AND the address never verified, AND one item is
backordered. Only the most important of those is worth a caller's turn. The rest are
noise on top of a problem they cannot act on yet.

That is a LADDER: an ordered list of rungs, each gated, the first eligible one speaks and
closes the rest out. The natural way to write a rung is an announce — it says a line and
fills its own slot, all inside the engine, with no tool and no round trip.

Except an announce latches only its OWN name. Nothing stops the next rung, so all four
fire on the same pass, and because the whole announce cascade leaves as ONE preempt the
caller hears every one of them in a single breath.

`sets` is the fix and it is one key. Each rung writes the SHARED latch the others are
gated on:

    flows.announce("PaymentFailed", [...],
                   condition={"all": [UNSPOKEN, {"slot": "payment_failed", "eq": "yes"}]},
                   sets={"verdict_given": "true"}, preempt=True)

The cascade recomputes after every announce, so the moment the first rung writes
`verdict_given` the other three stop being eligible. Exactly one speaks.

WHY NOT TASKS. A rung like this can be written as a task with `out_key=verdict_given`, and
that is what the ladder pattern used to require. It works, and it costs a platform round
trip per rung: the engine emits a `function_call`, CES runs a tool whose body is `pass`,
`after_tool` fires, and `before_model` re-enters the engine to process the result. On a
deployed agent that re-entry measured ~230ms — to write one constant. As announces the
same ladder resolves inside a single engine invocation.

DRIVEN LIVE against the real platform, one deployed app per arm, same two utterances.
Verbatim from the drive; ANNOUNCE_LADDER_VERIFY.md has the commands and the app ids.

  TREATMENT (`sets=`) — order A9, which has all three problems at once.
     caller: "my order has a problem"
     agent:  "I can help with that. Could you please provide your order number?"
     caller: "A nine"
     agent:  "Your payment didn't go through, so the order is on hold. Once the card is
              sorted the rest will follow."
     ONE rung. The three lower ones never spoke.

  CONTROL (`--control`, identical but for the missing `sets=`) — same utterances.
     caller: "A nine"
     agent:  "Your payment didn't go through, so the order is on hold. Once the card is
              sorted the rest will follow. We couldn't verify your delivery address, so
              nothing will ship until that's confirmed. One item is on back order, so the
              order will ship in two parts. Everything checks out — your order is on track."
     FOUR verdicts in one breath, ending with "everything checks out" immediately after
     three separate problems. That last line is the tell: `AllClear` is the floor rung,
     eligible precisely because nothing had closed the ladder.

The control is worth keeping in the file rather than describing, because it is what proves
the one-line result is `sets` doing the work and not the conditions happening to be
exclusive. They are not exclusive here — all four hold at once, by construction.

TWO THINGS THAT ARE EASY TO GET WRONG

* `preempt=True` on every rung. Verbatim `texts` only reach the caller on a preempting
  turn; leave it at the DSL default and the rung says nothing at all.
* Order is the contract. Announces are scanned in declaration order, so the ladder's
  priority is the order you `add()` them in — and announces are scanned BEFORE tasks, so a
  rung converted from a task now outranks every task in the flow.

Build + validate offline:
    PYTHONPATH=src python -m examples.announce_ladder            # treatment
    PYTHONPATH=src python -m examples.announce_ladder --control  # the A/B control
"""

import sys

from pydantic import BaseModel, Field

import flows

CONTROL = "--control" in sys.argv

# The shared gate. Every rung is held behind it and every rung closes it, which is the
# whole ladder in one condition.
UNSPOKEN = {"slot": "verdict_given", "filled": False}


class OrderRecord(BaseModel):
  """What the order system knows about one order, problems and all."""

  payment_failed: str = Field(description="'yes' if the card was declined.")
  address_unverified: str = Field(description="'yes' if the address never verified.")
  backordered: str = Field(description="'yes' if any item is on back order.")
  success: bool = Field(default=True, description="Whether the order was found.")


@flows.tool(flow="order_issue")
def look_up_order(order_id: str) -> OrderRecord:
  """Look an order up and report every problem currently on it."""
  # Inline rather than a module-level constant: only the function's own source ships to
  # the platform, so a global it closes over is a NameError at the far end.
  #
  # A9 is the interesting one — everything is wrong with it at once, which is what makes
  # the ladder mean anything. A1 is the clean order that falls through to the floor rung.
  if order_id.upper().replace(" ", "") == "A9":
    return OrderRecord(payment_failed="yes", address_unverified="yes", backordered="yes")
  return OrderRecord(payment_failed="no", address_unverified="no", backordered="no")


def _rung(name: str, flag: str, line: str) -> dict:
  """One ladder rung: gated on the shared latch AND its own problem, and closes the gate.

  `sets` is the only thing separating this from a pile of announces that all fire. Under
  `--control` it is dropped and nothing else changes.
  """
  condition = {"all": [UNSPOKEN, {"slot": flag, "eq": "yes"}]} if flag else UNSPOKEN
  extra = {} if CONTROL else {"sets": {"verdict_given": "true"}}
  # `requires` names the flag this rung READS, not just any lookup result: the validator
  # warns when a condition reads a slot the rung does not wait for, and it is right to —
  # a rung that fires before its own flag exists is a rung gated on nothing.
  return flows.announce(name, [line], condition=condition,
                        requires=[flag] if flag else ["payment_failed"],
                        preempt=True, **extra)


order_issue = flows.Flow("order_issue", root_agent="Order_Agent",
                         bootstrap={"reset_on_complete": True})

order_issue.add(
    flows.user_slot("order_id", ask="What's the order number?"),
    # The shared latch, declared like any other slot. `sets` writes it and every rung's
    # condition reads it, so it has to exist: an `event_slot` is the never-asked,
    # engine-written flag this wants, and the validator rejects a condition that names
    # a slot the flow never declares.
    flows.event_slot("verdict_given"),
    flows.result_slot("payment_failed", "LookUpOrder"),
    flows.result_slot("address_unverified", "LookUpOrder"),
    flows.result_slot("backordered", "LookUpOrder"),

    # THE LADDER. Declaration order is priority order, highest first. Every rung
    # `requires` a lookup result so none of them can fire before the order is read —
    # an announce with no `requires` is eligible on the opening turn.
    _rung("PaymentFailed", "payment_failed",
          "Your payment didn't go through, so the order is on hold. Once the card is"
          " sorted the rest will follow."),
    _rung("AddressUnverified", "address_unverified",
          "We couldn't verify your delivery address, so nothing will ship until that's"
          " confirmed."),
    _rung("Backordered", "backordered",
          "One item is on back order, so the order will ship in two parts."),
    # The floor. No problem flag, so it is eligible whenever the gate is still open —
    # which is exactly what makes it the tell in the control arm, where it speaks
    # straight after three problems have been read out.
    _rung("AllClear", "", "Everything checks out — your order is on track."),
)

# One tool, three result slots: `out_slot` takes the first and `extra_outputs` maps the
# rest, keyed by the field name the tool returns.
order_issue.task(flows.task(
    "LookUpOrder", "look_up_order", ["order_id"], "payment_failed",
    extra_outputs={"address_unverified": "address_unverified",
                   "backordered": "backordered"}))

app = flows.App(root_flow=order_issue,
                app_display_name="Announce ladder" + (" CONTROL" if CONTROL else ""),
                model="gemini-composite-v1")


def _show_ladder() -> None:
  """Print the emitted rungs, so the difference between the arms is visible offline."""
  cfg = order_issue.to_config()
  print(f"\n  arm: {'CONTROL (no sets)' if CONTROL else 'TREATMENT (sets=)'}")
  for slot in cfg["slots"]:
    if "announce" not in str(slot.get("source", "")):
      continue
    closes = slot.get("sets") or {}
    print(f"  {slot['name']:20} closes the gate: {bool(closes)}"
          f"{'  ' + str(closes) if closes else ''}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    out = "./announce_ladder_control_app" if CONTROL else "./announce_ladder_app"
    flows.build_app(app, out)
    print(f"built: {out}")
    _show_ladder()
