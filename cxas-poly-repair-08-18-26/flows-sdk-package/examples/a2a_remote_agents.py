"""Calling other agents over A2A — the two ways, in one live demo.

A2A (Agent2Agent) lets this agent hand work to an agent it does not own: an ADK agent
on Cloud Run, a third-party SaaS agent, another CXAS app. CES models each one as a tool
carrying an A2A **agent card**, so `flows` declares them the way it declares anything
else and the emitter writes a body-less `remoteAgentTool` resource. There is no python
to write — the platform makes the call, not the sandbox.

This file is a demo that runs end to end, so it carries BOTH sides: two small agents to
deploy as the remote side, and the concierge that calls them. The concierge uses each of
the two ways one can be used:

    the two paths                what it looks like
    ---------------------------  ---------------------------------------------
    MODEL-CALLABLE               `remote_agents=[...]` scopes the tool onto the
    (`ticket-agent`)             agent; the instruction points at it with
                                 `{@TOOL: name}` and the model calls it when the
                                 caller's problem matches the card
    SLOT-FILLING                 `delegate(...)` fires the agent from a task,
    (`parcel-agent`)             parks the raw A2A reply in a slot and reads the
                                 text out of it into another

Use the first when the remote agent IS the answer and the model should decide. Use the
second when the remote agent is one step of a transaction you are driving — the flow
collects what it needs, delegates, and keeps going with the reply in a slot.

The second path is the one with a trap in it, and the reason `delegate` exists rather
than a bare `task(tool=...)`. A remote agent answers with the A2A `SendMessageResponse`
oneof, which is one of two shapes:

    {"message": {"parts": [{"text": "It is 72 F."}], "contextId": "..."}}   # answered
    {"task":    {"id": ..., "status": {"state": "TASK_STATE_SUBMITTED"}}}   # accepted

Neither carries a `success` key, and intake decides a task worked by reading exactly
that (`success = bool(response_data.get(success_check))`). So a task left on the default
reads as failed on every fire, and with `on_failure.max_retries` defaulting to zero the
flow escalates the first time it runs, with nothing wrong. `outputs` has the matching
problem: intake maps by flat top-level key, and the reply text is nested inside the
reply where a flat map cannot reach it. Deployed past the guard, that is what the caller
hears when the remote agent has in fact answered correctly:

    > where is my parcel, tracking number 12345678?
    < An error occurred.

`delegate` handles both: it pins the call to the reply that carries a finished answer,
parks that reply whole, and generates a small sandbox-safe tool to read the text out of
it. `flows.build_app` refuses the hand-rolled shapes rather than emitting them.

Not claimed: `contextId`. A2A replies carry one, and passing it back continues that
conversation rather than starting a new one — `delegate(context_slot=...)` sends it,
but nothing here captures it from a previous reply.

Build + validate offline:
    python -m examples.a2a_remote_agents      # emits ./a2a_remote_agents_app/*

Run it live, remote side first, because the concierge addresses those two by app id:
    cxas push --app-dir ./a2a_remote_agents_app/parcel_agent --display-name parcel-agent
    cxas push --app-dir ./a2a_remote_agents_app/ticket_agent --display-name ticket-agent
    PARCEL_AGENT_APP_ID=<id> TICKET_AGENT_APP_ID=<id> python -m examples.a2a_remote_agents
    cxas push --app-dir ./a2a_remote_agents_app/concierge --display-name concierge
    cxas run-session text projects/<n>/locations/us/apps/<concierge id>
"""

import os

import flows
from pydantic import BaseModel, Field

# --- The remote side. -------------------------------------------------------
# Two ordinary CXAS agents. Nothing here opts into A2A: a deployed CES app is an A2A
# endpoint already, which is the inbound half of the protocol and why it needs no
# config. Each answers in one turn and asks nothing back, because a remote agent is
# called with a single request string and cannot run a conversation with the caller.


class ParcelStatus(BaseModel):
  status: str = Field(description="Where the parcel is now.")
  eta: str = Field(description="When it is expected.")


class Case(BaseModel):
  case_number: str = Field(description="The case reference to read back.")
  queue: str = Field(description="The queue the case landed in.")


@flows.tool()
def track_parcel(tracking_number: str) -> ParcelStatus:
  """Look up where a parcel is by its tracking number."""
  known = {
      "12345678": ParcelStatus(status="out for delivery in Boston",
                               eta="today before 6 PM"),
      "99887766": ParcelStatus(status="held at the Denver sorting center",
                               eta="Thursday"),
  }
  return known.get(str(tracking_number).strip(),
                   ParcelStatus(status="no record of that tracking number",
                                eta="unknown"))


@flows.tool()
def open_case(summary: str) -> Case:
  """Open a support case from a one-line description of the problem."""
  return Case(case_number=f"CS-{abs(hash(str(summary).lower())) % 9000 + 1000}",
              queue="deliveries")


_parcel_flow = flows.Flow("parcel_answer", root_agent="Parcel_Agent")
_parcel_flow.add(flows.passive_slot("tracking_number"))

parcel_agent_app = flows.App(
    root_flow=_parcel_flow,
    app_display_name="A2A Demo Parcel Agent",
    extra_agent_tools=["track_parcel"],
    agent_instruction=(
        "You are a parcel tracking service. Answer the question you are given in "
        "one or two sentences and then stop.\n\n"
        "Call {@TOOL: track_parcel} with the tracking number in the request and "
        "report the status and ETA it returns. If the request carries no tracking "
        "number, say that you need one. Never ask a follow-up question."
    ),
)

_ticket_flow = flows.Flow("case_answer", root_agent="Case_Agent")
_ticket_flow.add(flows.passive_slot("summary"))

ticket_agent_app = flows.App(
    root_flow=_ticket_flow,
    app_display_name="A2A Demo Ticket Agent",
    extra_agent_tools=["open_case"],
    agent_instruction=(
        "You are a support case service. Answer the request you are given in one "
        "sentence and then stop.\n\n"
        "Call {@TOOL: open_case} with a one-line summary of the problem and report "
        "the case number it returns. Never ask a follow-up question."
    ),
)

# --- The calling side. ------------------------------------------------------
# A card's `description` and its skills are the model's only signal for whether an
# agent is worth calling, so they describe the DOMAIN, not the transport. Everything
# else on the card is defaulted; spell a field out only where the default is wrong.

# Stands in so the concierge still builds before the remote side exists — CI builds this
# file with nothing set. It is not a working endpoint: pointed at one, a call comes back
# `errorCode: NOT_FOUND` and the caller hears the escalation line, so `__main__` says so
# rather than letting a deployable-looking app fail only once someone talks to it.
UNSET_APP_ID = "00000000-0000-0000-0000-000000000000"

PARCEL_AGENT_APP_ID = os.environ.get("PARCEL_AGENT_APP_ID", "") or UNSET_APP_ID
TICKET_AGENT_APP_ID = os.environ.get("TICKET_AGENT_APP_ID", "") or UNSET_APP_ID

# One job, so the single skill is derived from the name and description. `ces_agent`
# inherits the App's project and location, which is what two apps deployed side by side
# want; the URL shape it builds is the whole of the outbound configuration.
parcel = flows.ces_agent(
    "parcel-agent",
    description="Answers questions about where a parcel is and when it arrives.",
    app_id=PARCEL_AGENT_APP_ID,
)

# Named skills instead, because they are what the model routes on: two remote agents
# under one instruction are separated by their card text and nothing else.
ticket = flows.ces_agent(
    "ticket-agent",
    description="Opens support cases for delivery problems and returns a case number.",
    app_id=TICKET_AGENT_APP_ID,
    skills=[
        flows.agent_skill(
            "open_case",
            name="Open case",
            description="Open a support case from a description of the problem.",
            tags=["support", "ticketing"],
        ),
    ],
)

concierge = flows.Flow("a2a_concierge", root_agent="Concierge")

# The delegation: collect a question, hand it to the parcel agent, speak the reply.
# `question` is sent as the remote agent tool's `task` parameter (its parameters are
# the platform's, not ours — `delegate` maps them).
ask_parcel = flows.delegate(
    "ask_parcel",
    parcel,
    request_slot="question",
    # reply_slot defaults to `<name>_reply` — here, `ask_parcel_reply`.
    then_say="{ask_parcel_reply}",
    terminal=True,
    # Do not hand a caller's question to an agent we do not own until we know who is
    # asking. An explicit `requires` REPLACES its default of [request_slot] rather than
    # adding to it, which reads like it drops the question — it does not: a task never
    # dispatches with an unfilled input, so this gates on identity AND waits for the
    # question. It does NOT gate `ticket-agent`, which the model calls directly.
    requires=["account_last_name"],
    # Guards ONE thing: CES deferring the call with its `{"result": "pending"}`
    # placeholder, which is the only shape the engine treats as a wait. It does not
    # cover the `task` reply — that is a real response, judged by success_check.
    awaits=flows.awaits(
        max_turns=4,
        say="Let me check that for you.",
        while_waiting=["Still checking.", "Almost there."],
        # A wait with no disposition is dropped silently at max_turns and the reply
        # slot never fills, so anything downstream of it waits for the rest of the
        # call. Say something instead.
        on_timeout={"say": "I can't reach the parcel service right now, sorry."},
    ),
)

concierge.add(
    flows.user_slot("account_last_name",
                    ask="What's the last name on the account?"),
    flows.user_slot(
        "question",
        ask="What would you like to know about your parcel?",
        reprompts=["Which tracking number should I look up?"],
    ),
    *ask_parcel.slots,
)
concierge.task(*ask_parcel.tasks)

app = flows.App(
    root_flow=concierge,
    app_display_name="A2A Demo Concierge",
    # Only `ticket` needs declaring here: `remote_agents` is what makes an agent
    # MODEL-callable. `parcel` is declared by splicing its delegation into the flow, so
    # repeating it would be redundant (harmless, and deduplicated, but redundant).
    remote_agents=[ticket],
    agent_instruction=(
        "You are a delivery concierge.\n\n"
        "For a question about where a parcel is, collect the question and let the "
        "flow handle it.\n"
        "To report a damaged or lost delivery, use {@TOOL: ticket-agent} and tell "
        "the caller the case number it returns.\n"
        "If a remote agent fails, say so plainly rather than inventing an answer."
    ),
)


if __name__ == "__main__":
  unset = [n for n, v in (("PARCEL_AGENT_APP_ID", PARCEL_AGENT_APP_ID),
                          ("TICKET_AGENT_APP_ID", TICKET_AGENT_APP_ID))
           if v == UNSET_APP_ID]
  if unset:
    print(f"warn: {', '.join(unset)} unset — the concierge will be built against a "
          "placeholder app id. It deploys, but every call to that agent returns "
          "NOT_FOUND and the caller hears the escalation line. Deploy the remote side "
          "first and re-run with the ids it prints.")

  for name, built in (("parcel_agent", parcel_agent_app),
                      ("ticket_agent", ticket_agent_app),
                      ("concierge", app)):
    errors, warnings = flows.validate_app(built)
    for w in warnings:
      print(f"warn: [{name}]", w)
    for e in errors:
      print(f"ERROR: [{name}]", e)
    print(f"validate {name}: {len(errors)} errors, {len(warnings)} warnings")
    if not errors:
      flows.build_app(built, f"./a2a_remote_agents_app/{name}")
      print(f"built: ./a2a_remote_agents_app/{name}")
