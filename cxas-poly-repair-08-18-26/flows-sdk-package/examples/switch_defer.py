"""`switchable="defer"` — the caller steps aside, and comes back to where they were.

An intent slot is filled once, deliberately, so a stray cue in a later sentence cannot
re-route someone who has already chosen. `switchable=True` opts one slot out of that: a
later unambiguous match re-decides it, and everything derived from the old value is
cleared so the DAG does not run on in a mixed state.

Clearing is right when the caller changed their mind. It is wrong when they only stepped
aside, which sounds like this:

    > activate my phone
    < Last four digits of that line?
    > 9413
    < Anything else about it?
    > hold on, what's my balance
    < Your balance is forty dollars, due on the fifteenth.
    > okay, back to the activation
    < Last four digits of that line?        <-- with switchable=True

Nothing errored. The caller answered, was heard, and is asked again anyway, because the
journey they left was thrown away the moment they left it.

`switchable="defer"` parks it instead, and restores it if they return, so the last line
above becomes "Anything else about it?" — the question they were actually on.

Three things travel with the journey, and the third is the one that bites:

    filled slots    so nobody is asked twice for a number they already read out
    task results    so a lookup already paid for is not paid for again
    retry counts    so a slot fumbled twice in activation does not arrive in billing
                    already two failures down -- retries live in one place, so a journey
                    that is never run can otherwise inherit failures and give up early,
                    transferring a caller who has not actually failed at anything

Parking is per value: a caller may leave and return repeatedly, and two journeys never
see each other's state. A journey never returned to stays parked and costs nothing.

Which mode to use: ask what coming back MEANS. A new request that should start clean is
`True`. A resumption, where the caller expects the agent to remember, is `"defer"`. When
unsure, `"defer"` errs the safer way -- it risks remembering something the caller has
moved on from, which the next answer overwrites, rather than re-interrogating someone who
already answered.

`note` below is deliberately shared by both journeys (no condition), because that is the
shape where parking is observable: it is the slot both branches want.

Verified live on CES, not just offline. This app was deployed and driven, alongside a
CONTROL that is byte-identical except for `switchable=True`, so the only variable is the
mode. Both were asked the same four things:

    > activate my phone
    > 9413
    > hold on, what's my balance
    > okay, back to the activation

    switchable="defer"   < Anything else about it?           <-- journey restored
    switchable=True      < Last four digits of that line?    <-- number asked for twice

The last line is the whole feature. The offline test
(`test_stepping_away_parks_the_journey_and_coming_back_restores_it`) asserts the same
thing against the engine directly; the live run is what proves the emitted config carries
it, which an offline pass cannot say.

Run: PYTHONPATH=packages/flows/src python packages/flows/examples/switch_defer.py
"""

import flows
from pydantic import BaseModel, Field

JOURNEY_CUES = {
    "activation": [r"\bactivate\b", r"\bactivation\b", r"\bnew (line|phone|sim)\b"],
    # Kept disjoint from the activation set on purpose: a switch fires only on an
    # unambiguous match, so cues that can both hit one sentence simply never switch.
    "billing": [r"\bbalance\b", r"\bbill\b", r"\bhow much do i owe\b"],
}


class Balance(BaseModel):
  """What the billing journey looks up."""

  success: bool = Field(description="Whether the lookup completed.")
  balance: str = Field(description="The amount owed, spoken.")
  due: str = Field(description="When it is due, spoken.")


@flows.tool(flow="care")
def look_up_balance() -> Balance:
  """Read the account balance. Takes NO arguments — the account is already identified."""
  return Balance(success=True, balance="forty dollars", due="the fifteenth")


def build() -> flows.App:
  """A two-journey flow the caller can leave and come back to."""
  care = flows.Flow("care", root_agent="Acme Mobile")

  care.add(flows.intent_slot(
      "journey",
      JOURNEY_CUES,
      ask="What are you calling about today?",
      switchable="defer",
  ))

  # `requires=["journey"]` is what puts this slot on the intent's blast radius, and so
  # what makes it park. A `condition` alone would NOT: conditions are not traversed,
  # because a condition can name a slot the author never meant as a dependency.
  care.add(flows.user_slot(
      "last_four",
      ask="Last four digits of that line?",
      requires=["journey"],
      condition={"slot": "journey", "eq": "activation"},
  ))
  care.add(flows.user_slot(
      "note",
      ask="Anything else about it?",
      requires=["journey"],
  ))

  care.add(flows.result_slot("balance", "LookUpBalance"))
  care.add(flows.result_slot("due_date", "LookUpBalance"))
  care.task(flows.task(
      "LookUpBalance",
      tool="look_up_balance",
      # `journey` is in requires, not only in the condition: the validator wants a task
      # to declare the slot it gates on, and it is what parks the result with the journey.
      requires=["journey"],
      inputs=[],
      out_slot="balance",
      out_key="balance",
      extra_outputs={"due": "due_date"},
      condition={"slot": "journey", "eq": "billing"},
      then_say="Your balance is {balance}, due on {due_date}.",
  ))

  return flows.App(root_flow=care, app_display_name="Acme Mobile (switch defer)")


app = build()


if __name__ == "__main__":
  import sys

  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  if errors:
    sys.exit(1)
  print("validate: ok")
