"""Hand the caller to a live agent — the payload the platform actually routes on.

A contact-center platform does not learn that a call is being escalated from anything
the agent says. It learns it from a structured payload on the turn. The spoken line is
for the caller; the payload is for the platform, and only one of the two puts a person
on the line.

`flows.handoff` emits the payload AND the `end_session` that has to accompany it, as
one unit, because half a hand-off is worse than none:

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "I need to check my order"      asks for the order number
    "48812"                         lookup fails three times -> ON_EXHAUST hands
                                    off, rather than apologizing and carrying on
    "just get me a person"          the ESCALATE rail hands off, with the payload
                                    that used to be missing from it

There are four places one flow gives up a call, and all four take a hand-off:

* the flow-level `escalate` block — `escalate(handoff=...)`
* a terminal announce — `announce(..., handoff=...)`
* a task's failure ladder — `on_failure={"on_exhaust": human.on_exhaust(...)}`
* a slot's No-Match ladder — `user_slot(..., on_exhaust_handoff=...)`

Each piece earns its place:

* ONE `human` object, declared once and reused at all four sites. The menu id is the
  routing decision — it picks which team answers — so having it in four hand-written
  literals is four chances for one of them to drift.
* `user_slot`'s default exhaust is `then: transfer_to_human`, which only RECORDS the
  request. On a platform that routes on a payload, the marker alone reaches nobody:
  `on_exhaust_handoff` is what replaces the marker with the real thing.
* Nothing here carries a surface condition. An unrecognized payload is inert on a chat
  client, a missing one on a phone call is a dropped caller, and an unknown channel
  resolves to voice — so unconditional is the safe direction. `flows.handoff(surface=
  "voice")` gates BOTH parts together for an app that genuinely needs it.

Build + validate offline:
    python -m examples.telephony_handoff      # emits ./telephony_handoff_app
"""

from pydantic import BaseModel, Field

import flows


class OrderStatus(BaseModel):
  status_msg: str = ""
  success: bool = Field(default=False)


@flows.tool(flow="order_status")
def lookup_order(order_number: str = "") -> OrderStatus:
  """Look up the delivery status of an order."""
  return OrderStatus(status_msg="out for delivery, arriving today", success=True)


# One hand-off, declared once. `menu_id` routes the caller to a queue, so it is the one
# argument with no default — and the one worth having in exactly one place.
HUMAN = flows.handoff(flows.ujet(menu_id="90"))


def build() -> flows.App:
  flow = flows.Flow("order_status", root_agent="order_agent")

  flow.add(
      flows.user_slot(
          "order_number",
          ask="What's your order number?",
          hint="the order number from the confirmation email",
          on_exhaust="I'm not getting that order number, so let me get you to "
                     "someone who can look it up. Please hold.",
          on_exhaust_handoff=HUMAN,
      ),
      flows.result_slot("status_msg", "Lookup"),
  )

  flow.task(flows.task(
      "Lookup", "lookup_order", ["order_number"], "status_msg",
      out_key="status_msg",
      on_failure={
          "max_retries": 2,
          "retry_say": "Let me try that again.",
          "on_exhaust": HUMAN.on_exhaust(
              "I can't reach our order system right now, so I'll put you through "
              "to an agent. Please hold."),
      },
  ))

  flow.add(
      flows.announce("status", ["Your order is {status_msg}."],
                     requires=["status_msg"]),
      # A caller who is not satisfied by the status still gets a person, and this
      # announce is the one that hands them over.
      flows.user_slot(
          "anything_else",
          ask="Does that answer it, or would you like me to get someone on the line?",
          hint="whether the caller is satisfied or wants a person",
          requires=["status_msg"],
      ),
      flows.announce(
          "to_an_agent",
          ["Of course — connecting you with an agent now. Please hold."],
          requires=["anything_else"],
          condition=flows.eq("anything_else", "agent"),
          handoff=HUMAN,
      ),
  )

  # The rail a caller reaches by simply ASKING for a person, at any point. Without
  # `handoff` this speaks its line and emits a bare end_session: the caller is told
  # someone is coming and is then disconnected, with nothing routing them anywhere.
  flow.set("escalate", flows.escalate(
      say="Of course — let me get you to someone who can help.",
      handoff=HUMAN,
  ))

  return flows.App(
      root_flow=flow,
      app_display_name="telephony-handoff",
      agent_instruction=(
          "You help callers check the status of an order. If the caller asks for a "
          "person, the engine hands them off — never promise a transfer yourself."
      ),
  )


app = build()

if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  if not errors:
    flows.build_app(app, "./telephony_handoff_app", overwrite=True)
    print("built: ./telephony_handoff_app")
