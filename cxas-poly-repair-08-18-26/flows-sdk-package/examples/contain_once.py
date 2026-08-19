"""Deflect the first request for a human, honour the second.

`escalate(condition=...)` refuses a hand-off outright, which is right when no human can
help — an area outage does not improve because someone joins the call. But the commonest
reason to decline is the opposite shape, and it is what almost every real script asks
for: contain the FIRST request, then put the caller through.

That could not be written. A declined request is DROPPED, and until now nothing recorded
that it happened — so the condition read identically on the next ask. Gate on anything
the flow does not otherwise change and the deflection fires forever: the caller can never
reach a person at all, which is worse than having no gate.

`<block>_declined` is the counter that makes it expressible.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "my bill is wrong"              asks what is wrong with it
    "just get me a person"          dropped. Rung one ASKS: what is driving this?
    "no, a person"                  dropped. Rung two OFFERS: ten dollars a month
                                    off — not the same sentence again, which is
                                    the point of the ladder
    "a person, please"              counter is 2: the disposition runs and the
                                    call transfers, with the chain attached

The second row is the point. Without the counter that row repeats forever.

Each piece earns its place:

* `condition` is checked BEFORE the `tasks` chain arms, so a declined request never runs
  the hand-off work. Building a summary for a transfer that is not going to happen would
  be wasted at best, and a chain member with a side effect would fire on a request that
  was refused.
* `declined_say` is what the caller hears instead. Omit it and the block simply never
  fires, which is the right default for a disposition that should be invisible rather
  than refused out loud — but here the caller asked a direct question and deserves an
  answer.
* The counter increments on EVERY refusal, not only the first. A condition that stays
  false still declines every time; the counter must not quietly turn every gate into a
  two-strike one.
* `declined_say` only SPEAKS. It cannot fire a tool, and no task runs on the declined
  path — the condition is checked before the `tasks` chain arms. So a rung can make an
  offer but not apply one; the caller answering "yes, do that" is an ordinary turn, and
  an ordinary slot and task in the spine pick it up.
* The ladder CLAMPS to its last line rather than draining to silence, which is where it
  differs from `awaits.while_waiting`. A hold going quiet is fine — the caller is
  waiting, not asking. A refusal answers a direct question, and hearing nothing back is
  the worst available response.

Build + validate offline:
    python -m examples.contain_once      # emits ./contain_once_app
"""

import flows


# A LADDER, one line per refusal. Two identical deflections read as the agent not
# listening, which is the specific thing that makes a caller escalate harder.
#
# The rungs ESCALATE WHAT IS ON OFFER rather than softening the apology. Rung one asks
# to understand; rung two spends something. That ordering matters: conceding on the
# first ask gives away margin to a caller who might have been satisfied by an answer.
DEFLECT = [
    "Before I put you through, let me see what's driving that — can you tell me a bit "
    "more about what looks wrong?",
    "I can see a loyalty offer on your account: ten dollars a month off for twelve "
    "months. Would you like me to add that while we talk?",
]
HAND_OFF = "Of course — let me get you to someone who can help."


def build() -> flows.App:
  """A one-question flow whose escalate rail contains the first ask."""
  flow = flows.Flow("billing_help", root_agent="billing_agent")

  flow.add(flows.user_slot(
      "problem",
      ask="What's wrong with the bill?",
      hint="what the caller thinks is wrong",
  ))
  # What the second rung spends. Seeded by whatever established eligibility earlier in
  # a real flow; here it is an event slot so the example stays one file.
  flow.add(flows.event_slot("offer_available"))

  flow.set("escalate", flows.escalate(
      say=HAND_OFF,
      declined_say=DEFLECT,
      # Deflect twice, then hand over — but ONLY while there is something to offer.
      # The `all` is the argument for a condition over a plain `contain=2` knob: a
      # caller with no offer on their account is not deflected twice for nothing, since
      # the eligibility leg is false regardless of the counter.
      condition={"all": [
          {"slot": "escalate_declined", "gte": 2},
          {"slot": "offer_available", "eq": "yes"},
      ]},
  ))

  return flows.App(
      root_flow=flow,
      app_display_name="contain-once",
      agent_instruction=(
          "You help with billing questions. Answer what you can. If the caller asks for "
          "a person, the engine decides whether to hand off — never promise a transfer "
          "yourself."
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
    flows.build_app(app, "./contain_once_app", overwrite=True)
    print("built: ./contain_once_app")
