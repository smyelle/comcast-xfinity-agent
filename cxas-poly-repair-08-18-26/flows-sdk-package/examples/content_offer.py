"""What an agent says AFTER the work is done — the two announce channels, side by side.

A payment agent has three things to say once the money moves, and they are not the same
kind of thing:

  1. the confirmation                — the operation's own result (`then_say`)
  2. a posting-cutoff notice         — compliance copy, VERBATIM, not one word different
  3. an autopay offer                — marketing content, which should sound like the
                                       agent talking, not like an ad break

`announce()` carries 2 and 3 down different channels, and picking the wrong one is
audible either way round. `texts` becomes `response` parts the caller hears EXACTLY as
written — right for the notice, wrong for the offer, which read out word for word sounds
spliced in. `message` becomes the engine's turn message that the MODEL renders in its own
words — right for the offer, and unsafe for the notice, which must not be paraphrased.

Neither may cut ACROSS the payment, which is what `preempt=False` buys, and it is spelled
out on the notice deliberately: the engine reads the key as `slot_def.get("preempt",
True)`, so an announce that merely OMITS it becomes a preempting one that fires the
instant its `requires` are met. `announce()` therefore always emits the key rather than
leaving it to that default.

What `preempt=False` does NOT buy is a position in the sentence. On the turn the payment
completes, the engine emits the announce's response parts and THEN the task's `then_say`,
so what the caller actually hears is "Payments made after 8 PM Eastern post to your
account the next business day. Done — that's paid. Your confirmation number is PM-40318."
Order WITHIN a turn belongs to that fold; order ACROSS turns is what `requires` controls,
and that is the lever the offer below uses.

`content_announce()` is the whole of case 3 in one call: model-rendered `message`,
`preempt=False`, and gated via `after=` so the pitch cannot precede what it is pitched
after.

What `after=` points at is load-bearing, and the obvious choice is the wrong one. Point it
at the payment's own result slot and the offer lands on the SAME turn as the confirmation
— a turn that preempts, because the task has a `then_say`. A message is only handed to
the model on a NON-preempting turn; on a preempting one the engine folds it into the
canned directive, and the caller hears the pitch's instructions read out at them. Driven
live, verbatim: "Payments made after 8 PM Eastern post to your account the next business
day. Offer to set up autopay so the caller never has to think about this again. One
friendly sentence, in your own words, and ask if they'd like it. Done — that's paid."
So the offer is pointed at `email_receipt` instead, which the caller's NEXT answer fills.
The turn boundary is what makes the content model-rendered — and pitching autopay as the
call winds down is where it belongs anyway.

Build + validate offline:
    python -m examples.content_offer           # emits ./content_offer_app
"""

from pydantic import BaseModel, Field

import flows


class PaymentReceipt(BaseModel):
  """The result of taking a payment."""

  confirmation_number: str = Field(description="Reference for the posted payment.")
  success: bool = Field(default=True, description="Whether the payment went through.")


@flows.tool(flow="payment")
def take_payment(payment_amount: str) -> PaymentReceipt:
  """Charge the amount the caller asked to pay against the account on file."""
  return PaymentReceipt(confirmation_number="PM-40318")


payment = flows.Flow("payment", root_agent="Billing_Agent",
                     bootstrap={"reset_on_complete": True})

payment.add(
    flows.user_slot("payment_amount",
                    ask="How much would you like to pay today?"),
    flows.result_slot("confirmation_number", "TakePayment"),
    # VERBATIM. `texts` (not `message`) because compliance copy the model rewrites is
    # no longer compliance copy. Non-preempting so it follows the confirmation.
    flows.announce(
        "posting_notice",
        ["Payments made after 8 PM Eastern post to your account the next business day."],
        requires=["confirmation_number"],
        preempt=False,
    ),
    # The turn boundary. A real question the caller answers, and its answer is what
    # carries the offer onto a turn of its own. Deliberately NOT "anything else I can
    # help with?": that reads as a goodbye cue, and a small model handed a pitch on
    # the same turn will close the call politely instead of delivering it.
    flows.user_slot("email_receipt",
                    ask="Would you like me to email you a receipt?",
                    requires=["posting_notice"]),
    # MODEL-RENDERED — and `after` the caller's answer, not after the payment, so it
    # lands on a turn where the model actually runs. See the module docstring.
    flows.content_announce(
        "autopay_offer",
        ("Confirm the receipt choice in a few words, then offer to set up autopay so"
         " the caller never has to think about this bill again. One friendly sentence"
         " in your own words, ending by asking whether they'd like it."),
        after="email_receipt",
    ),
)

payment.task("TakePayment", "take_payment", ["payment_amount"], "confirmation_number",
             out_key="confirmation_number",
             # Not terminal: a terminal task ENDS the flow, and everything this example
             # is about happens after the payment lands.
             then_say="Done — that's paid. Your confirmation number is "
                      "{confirmation_number}.")

app = flows.App(
    root_flow=payment,
    app_display_name="flows-sdk-demo Content Offer",
    agent_instruction=(
        "You are a billing agent. Follow the slot-filling framework directives exactly. "
        "Speak only what the framework gives you."
    ),
)


def _show_channels() -> None:
  """Print how each announce ended up on the wire — the point of the example."""
  by_name = {s["name"]: s for s in payment.to_config()["slots"]}
  for name in ("posting_notice", "autopay_offer"):
    s = by_name[name]
    channel = "verbatim response" if "response" in s else "model-rendered message"
    print(f"  {name:16} {channel:24} preempt={s['preempt']} "
          f"requires={s.get('requires')}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./content_offer_app")
    print("built: ./content_offer_app")
    _show_channels()
