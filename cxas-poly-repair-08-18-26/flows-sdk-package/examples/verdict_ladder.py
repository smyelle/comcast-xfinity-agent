"""A card-decline triage line: a SILENT diagnostic spine, then a PRIORITY ladder.

"My card just got declined" is not a question with one answer — it is a question with
several simultaneously true answers, only one of which is worth saying. A card can be on
a fraud hold AND short of funds AND reported lost, all at once. Telling that caller "you
were $12.40 over your balance" is not merely incomplete, it is wrong advice: they will
top up and try again, and the card will decline again, because the fraud hold is what
actually stopped it.

So this agent has two halves, and `verdict()` builds both:

  * a SILENT spine — three checks that take NO arguments, so they fire on the turn that
    carries the caller's opening sentence. Nothing is asked first. (Silent in the
    `silent_home_base` sense too: the flow has no setter, so nothing can route INTO it,
    which is why it is the router's `default_flow`.)
  * an ORDERED ladder — the branches below are a priority order, not a set of
    independent rules. The first one whose gate holds is the one the caller hears, and
    the ONLY one.

That second property is the whole point, and it is not free. The engine does not stop at
the first eligible announce: it walks the cascade and speaks EVERY announce whose
condition is true, so two matching rules arrive glued together and contradicting each
other ("Your card is on a security hold. You were $12.40 short."). `verdict()` derives
the gating that prevents this — each rung additionally requires every higher rung to be
unfired, and every rung requires the WHOLE spine to have reported, so no verdict is
formed from half the evidence. Both are things that fail silently when hand-wired, which
is why they are derived rather than left to the author.

The canned backend below reports a live card, a fraud hold AND a $12.40 shortfall, so
THREE rungs match at once — 1, 4 and the 5 catch-all — and rung 1 must win alone.
`lost_report` comes from the fraud system on the session event rather than from any check
here, so seeding it makes a FOURTH rung match and flips the same ladder to rung 0 with no
change to the app: that is the `reads` escape hatch, declared as an event slot and
deliberately NOT required (a gate that can never fill would make its rung permanently
unreachable).

Verbatim rungs also PREEMPT, which `verdict()` derives too. Without it the arbitrated
verdict is only QUEUED for the next turn, and the model — which by then has every check's
raw output in context — fills the silence with a summary of ALL of them. Measured, on
this exact app: "First, there's a fraud hold on your card ... Second, your account is
short by $12.40."

Every gate is a DECLARATIVE dict — the form the engine evaluates natively, and the only
form that expresses rung 4's numeric threshold. `gate()` wraps the one compound gate so a
typo in it is an authoring-time error rather than a rung that silently never opens.

`VerdictBranch(transfer_to=...)` is implemented and unit-tested
(tests/test_verdict_ladder.py) but is NOT used here: this is a single-agent app, so there
is no second agent to hand to, and a transfer part naming an agent that does not exist is
not something this example should claim works.

Build + validate offline:
    python -m examples.verdict_ladder          # emits ./verdict_ladder_app
"""

from pydantic import BaseModel, Field

import flows
from flows import VerdictBranch


class CardStatus(BaseModel):
  """The card's own state, from the card system."""

  card_state: str = Field(description="active | frozen | expired")
  card_last4: str = Field(description="Last four digits of the card.")
  success: bool = Field(default=True, description="Whether the check completed.")


class DeclineCheck(BaseModel):
  """Why the authorisation switch refused the most recent attempt."""

  decline_reason: str = Field(
      description="fraud_hold | insufficient_funds | none")
  merchant: str = Field(description="Where the declined attempt was made.")
  success: bool = Field(default=True, description="Whether the check completed.")


class FundsCheck(BaseModel):
  """The declined amount measured against the available balance."""

  shortfall_cents: int = Field(
      description="How far over the available balance the attempt was; 0 if covered.")
  shortfall: str = Field(description="The same figure formatted, e.g. '$12.40'.")
  success: bool = Field(default=True, description="Whether the check completed.")


# ── The canned backend. Each tool is rendered into the CES sandbox as a SELF-CONTAINED
# file — its referenced pydantic models come with it, but nothing else from this module
# does. So the fixture values are literals inside the bodies: a module-level constant
# here is a NameError at execution time, which surfaces as "An error occurred" on the
# call rather than as anything at build time.


@flows.tool(flow="triage")
def check_card_status() -> CardStatus:
  """Read the card's own state. Takes NO arguments — nothing is collected first."""
  return CardStatus(card_state="active", card_last4="4417")  # not frozen, not expired


@flows.tool(flow="triage")
def check_decline_reason() -> DeclineCheck:
  """Ask the authorisation switch why the last attempt was refused. No arguments."""
  # ... but the switch refused it on a security hold ...
  return DeclineCheck(decline_reason="fraud_hold",
                      merchant="an online electronics store")


@flows.tool(flow="triage")
def check_available_funds() -> FundsCheck:
  """Measure the declined amount against the available balance. No arguments."""
  return FundsCheck(shortfall_cents=1240, shortfall="$12.40")  # ... AND short. Two rungs.


# ── The spine. Three checks, no inputs, no ordering between them: they are a
# fan-out, and the ladder below is the join.
SPINE = [
    flows.task("CardCheck", "check_card_status", [], "card_state",
               out_key="card_state", extra_outputs={"card_last4": "card_last4"}),
    flows.task("SwitchCheck", "check_decline_reason", [], "decline_reason",
               out_key="decline_reason", extra_outputs={"merchant": "merchant"}),
    flows.task("FundsCheck", "check_available_funds", [], "shortfall_cents",
               out_key="shortfall_cents", extra_outputs={"shortfall": "shortfall"}),
]

# ── The ladder. ORDER IS THE POLICY. Read top to bottom as "the most important
# true thing to say about this decline".
LADDER = [
    # 0 — Never tell someone to retry a card that has been reported stolen, and never
    # let a lower rung's "just top up and try again" reach them. Sourced from the fraud
    # system on the session event, not from any check above.
    VerdictBranch(
        condition={"slot": "lost_report", "eq": "confirmed"},
        say=("This card is already reported lost or stolen, so it won't authorise "
             "anything — a replacement is the only way forward. Let me get you to our "
             "fraud team."),
        reads=["lost_report"],
    ),
    # 1 — A hold is the bank's own block. It outranks the balance because lifting it is
    # what actually fixes the decline.
    VerdictBranch(
        condition={"slot": "decline_reason", "eq": "fraud_hold"},
        say=("Good news — your card is fine. We put a temporary security hold on it "
             "after an attempt at {merchant} that didn't look like your usual spending. "
             "I can lift that once I've checked a couple of details with you."),
        reads=["decline_reason", "merchant"],
    ),
    # 2 — The caller froze it themselves in the app; they can unfreeze it themselves.
    VerdictBranch(
        condition={"slot": "card_state", "eq": "frozen"},
        say=("Your card ending {card_last4} is frozen — it looks like it was locked "
             "from the app. Unlocking it there will let the payment through."),
        reads=["card_state", "card_last4"],
    ),
    # 3 — Expiry is a fact about the card, so it outranks a balance shortfall: money in
    # the account will not make an expired card work.
    VerdictBranch(
        condition={"slot": "card_state", "eq": "expired"},
        say=("Your card ending {card_last4} has expired. Your replacement should "
             "already be with you — activating that one will sort this out."),
        reads=["card_state", "card_last4"],
    ),
    # 4 — "The card is fine, the money wasn't there." Both halves are TRUE in the
    # fixture, so this rung matches at the same time as rung 1 — it is just a less
    # important truth, which is the only thing keeping the caller from hearing it.
    # A numeric threshold, which only the declarative form can express.
    VerdictBranch(
        condition=flows.gate({"all": [
            {"slot": "card_state", "eq": "active"},
            {"slot": "shortfall_cents", "gt": 0, "default": 0},
        ]}),
        say=("That payment was {shortfall} more than the available balance on the "
             "account. Once that's topped up the card will go through."),
        reads=["card_state", "shortfall_cents", "shortfall"],
    ),
    # 5 — The catch-all. `filled` (not a value test) is what makes it a floor: it is
    # true whenever the spine has reported at all, so the ladder always says something.
    # Generative, because "nothing is wrong but it still declined" has no fixed script.
    VerdictBranch(
        condition={"slot": "card_state", "filled": True},
        say=("Tell the caller every check came back clean — the card is active, there "
             "is no hold, and the balance covered it — so the decline most likely came "
             "from the merchant's side, and suggest trying the payment once more."),
        generative=True,
        reads=["card_state"],
    ),
]

triage = flows.verdict("triage", spine=SPINE, branches=LADDER,
                       root_agent="Card_Support_Agent")
# The verdict is the home base, and a home base has to be re-enterable: without this the
# flow completes once and the next caller turn has nowhere to land.
triage.set("bootstrap", {"reset_on_complete": True})

# ── An ordinary flow beside it, so the router is a real router: `dispute` HAS a setter,
# so it is not silent and is reached the normal way, by cue.
dispute = flows.Flow("dispute", bootstrap={"reset_on_complete": True})
dispute.add(
    flows.user_slot("disputed_amount",
                    ask="How much was the charge you want to dispute?"),
    flows.result_slot("case_number", "OpenDispute"),
)
dispute.task("OpenDispute", "open_dispute", ["disputed_amount"], "case_number",
             out_key="case_number", terminal=True,
             then_say="I've opened dispute {case_number} for that charge.")


class DisputeCase(BaseModel):
  case_number: str = Field(description="The reference for the opened dispute.")
  success: bool = Field(default=True, description="Whether the case opened.")


@flows.tool(flow="dispute")
def open_dispute(disputed_amount: str) -> DisputeCase:
  """Open a dispute case for a charge the caller does not recognise."""
  return DisputeCase(case_number="DP-88214")


router = flows.router_flow(
    "card_host",
    ["triage", "dispute"],
    default_flow="triage",        # <- home base: the silent verdict flow
    route_cues={"dispute": ["dispute", "didn't make", "did not make",
                            "don't recognise", "don't recognize", "fraudulent charge"]},
    root_agent="Card_Support_Agent",
)

app = flows.App(
    root_flow=router,
    extra_flows=[triage, dispute],
    app_display_name="flows-sdk-demo Verdict Ladder",
    agent_instruction=(
        "You are a card support agent. Follow the slot-filling framework directives "
        "exactly. Speak only what the framework gives you."
    ),
)


def _show_ladder() -> None:
  """Print the derived gating, which is the part an author would get wrong by hand."""
  cfg = triage.to_config()
  for task in cfg["tasks"]:
    print(f"  spine {task['name']:12} outputs {task['outputs']}")
  for slot in cfg["slots"]:
    if not slot["name"].startswith("triage_branch_"):
      continue
    cond = slot["condition"]
    gates = cond["all"] if "all" in cond else [cond]
    halt = [g for g in gates if str(g.get("slot", "")).startswith("triage_branch_")]
    spine_flags = [r for r in slot["requires"] if r.endswith("_ran")]
    print(f"  {slot['name']}: preempt={slot['preempt']}, waits for all "
          f"{len(spine_flags)} spine flags, loses to {len(halt)} higher rung(s)")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./verdict_ladder_app")
    print("built: ./verdict_ladder_app")
    _show_ladder()
