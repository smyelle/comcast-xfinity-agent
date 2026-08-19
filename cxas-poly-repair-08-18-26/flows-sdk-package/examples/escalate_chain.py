"""Warm hand-off: run your own tasks BEFORE the transfer, so the human gets context.

`transfer_to_human` is a marker. It records that the caller asked for a person and ends
the session — that is all it does. An app that builds a hand-off summary builds it in
its own DAG, and the escalate rail short-circuits straight past that DAG, so the
associate picks up a call knowing nothing.

`escalate(tasks=[...])` names an ordered chain to run first.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "my internet is out"            asks for the account number
    "8069100230361049"              runs diagnostics on the line
    "just get me a person"          ARMS THE CHAIN: prepare_handoff builds a
                                    summary, then hand_off files it, and only
                                    THEN the ordinary escalate disposition runs

The third row is the point. Without the chain that turn is a single
`transfer_to_human()` and nothing else.

Each piece earns its place:

* `flows.escalated()` gates both members. The engine fills the synthesized `escalate`
  slot before running the chain, so a member gated on it is inert in the ordinary spine
  walk and eligible only on the hand-off path. The validator REJECTS an ungated member,
  because the ordinary walk would otherwise fire it before any escalate happens.
* `Diagnostics` is deliberately eligible the moment the account lands. It is declared
  first, so in an unrestricted walk it would win the race against anything the chain
  wants to fire — which is why the chain's walk is scoped to its own members.
* Neither member is `terminal` (it would tear the flow down before the disposition) and
  neither `awaits` (an awaited result is turns away, and escalation is the one rail a
  caller must never be held on). The validator enforces both.
* The holding line sits on the LAST member. A `then_say` on a member that another
  member follows is emitted twice — once beside the next member's tool call, once
  again ahead of `escalate.say` — so the caller hears it back to back.

Build + validate offline:
    python -m examples.escalate_chain      # emits ./escalate_chain_app
"""

from pydantic import BaseModel, Field

import flows


class DiagnosisResult(BaseModel):
  diagnosis: str = ""
  success: bool = Field(default=True)


class SummaryResult(BaseModel):
  summary: str = ""
  success: bool = Field(default=True)


class HandOffResult(BaseModel):
  handoff_ack: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="support")
def diagnose(account: str = "") -> DiagnosisResult:
  """Run line diagnostics for an account."""
  return DiagnosisResult(
      diagnosis="the gateway has been offline since 9:14 this morning")


@flows.tool(flow="support")
def prepare_handoff(account: str = "", diagnosis: str = "") -> SummaryResult:
  """Build the summary the receiving associate reads."""
  detail = f" Diagnostics found {diagnosis}." if diagnosis else ""
  return SummaryResult(
      summary=f"Account {account} asked for a person.{detail}")


@flows.tool(flow="support")
def hand_off(summary: str = "") -> HandOffResult:
  """File the summary against the case before the transfer."""
  return HandOffResult(handoff_ack=f"filed: {summary}")


support = flows.Flow("support", root_agent="support_agent")

support.add(
    flows.user_slot(
        "account",
        ask="What's your account number?",
        hint="the account number on the bill",
    ),
    flows.result_slot("diagnosis", "Diagnostics"),
    flows.result_slot("summary", "PrepareHandoff"),
    flows.result_slot("handoff_ack", "HandOff"),
)

# The ordinary spine. Eligible as soon as the account is captured, which is what makes
# the chain's scoped walk observable rather than incidental.
support.task(flows.task(
    "Diagnostics", "diagnose", ["account"], "diagnosis",
    out_key="diagnosis",
))

# Chain members. Gated on escalated(), so they are invisible until the caller asks.
support.task(flows.task(
    "PrepareHandoff", "prepare_handoff", ["account", "diagnosis"], "summary",
    out_key="summary",
    condition=flows.escalated(),
))
# `then_say` belongs on the LAST member. On a non-final member the engine both stashes
# it for the disposition and sends it alongside the next member's tool call, so the
# caller hears it twice.
support.task(flows.task(
    "HandOff", "hand_off", ["summary"], "handoff_ack",
    out_key="handoff_ack",
    condition=flows.escalated(),
    then_say="One moment while I pull your details together.",
))

# Deliberately NOT `end=True`: the flow has to stay open for the caller to be able to
# ask for a person at all, which is the whole scenario. The wrap-up slot below is what
# holds the turn open after the verdict is read out.
support.add(
    # `preempt=True` or the texts are dropped: an announce only renders its own words
    # when it preempts the model. Without it this line is never spoken.
    flows.announce("verdict", ["Here's what I found: {diagnosis}."],
                   requires=["diagnosis"], preempt=True),
    # NOT named `wrap_up`: `collect()` routes any `set_wrap_up*` setter to
    # `gen_wrap_up_setter`, which closes the session — so a slot by that name ends the
    # call before the caller can ask for anyone.
    flows.user_slot(
        "next_step",
        ask="Does that answer it, or would you like me to get someone on the line?",
        hint="whether the caller is satisfied or wants a person",
        requires=["diagnosis"],
    ),
)

support.set("escalate", flows.escalate(
    say="Let me get you to someone who can help.",
    tasks=["PrepareHandoff", "HandOff"],
))

app = flows.App(
    root_flow=support,
    app_display_name="Escalate Hand-off Chain",
)


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./escalate_chain_app")
    print("built: ./escalate_chain_app")
