"""Calling another agent as a tool, and not waiting on the line while it thinks.

A specialist that reasons for several seconds is the case this exists for. Declared
`asynchronous=True`, the call defers: the caller hears the wait line straight away and
the answer arrives on a later turn, instead of the line going dead until the specialist
is done.

Two things the author does NOT write, because both follow from the tool being an agent:

  * the argument name (`request`), which the platform rejects any other spelling of
  * `out_key` / `success_check` (`response`), without which a perfectly good answer
    reads as a failure and the flow escalates on its first fire

`flows.task(...)` fills them in from the declaration, the same way it does for a search
tool. Both stay overridable.

Two arms, selected by env var, so the failure path can be driven as well as the happy
one — a `failed with error` envelope looks exactly like a successful agent reply, and
telling them apart is the whole reason the engine keeps the envelope's verb.

    python -m examples.agent_tool            # the specialist answers
    ARM=failing python -m examples.agent_tool  # the specialist blows up
"""

import os

from pydantic import BaseModel

import flows

ARM = os.environ.get("ARM", "answers")


class Lookup(BaseModel):
  summary: str
  success: bool = True


# The arm is chosen HERE, not inside the body: only the function itself and the models
# it names are inlined into the emitted tool, so a body that reads a module-level name
# fails on its first call with "name 'os' is not defined" (validate_app says so).
if ARM == "failing":

  @flows.tool(flow="parcel")
  def parcel_backend(tracking: str) -> Lookup:
    """Look a parcel up.

    Args:
      tracking: The tracking reference to look up.
    """
    raise RuntimeError("parcel backend is down")

else:

  @flows.tool(flow="parcel")
  def parcel_backend(tracking: str) -> Lookup:
    """Look a parcel up.

    Args:
      tracking: The tracking reference to look up.
    """
    return Lookup(summary="out for delivery in Boston")


specialist = flows.helper_agent(
    "parcel_specialist",
    instruction=(
        "You are a parcel specialist. Call parcel_backend with any reference you are "
        "given, then reply with one sentence beginning HELPER9f21 and nothing else."
        if ARM == "answers" else
        "You are a parcel specialist. ALWAYS call parcel_backend before answering."),
    tools=["parcel_backend"],
)

ask_specialist = flows.agent_tool(
    "ask_specialist",
    agent=specialist,
    description="Answers questions about where a parcel is.",
    asynchronous=True,
)

parcel = flows.Flow("parcel", root_agent="root_agent",
                    bootstrap={"welcome_slot": "welcome"})
parcel.add(
    flows.announce("welcome", ["Parcel desk. What's your question?"],
                   shared=True, preempt=True),
    flows.user_slot("question", "What would you like to know?"),
    flows.result_slot("answer", "Ask"),
    flows.announce("done", ["{answer}"], requires=["answer"], preempt=True, end=True),
)
parcel.task(flows.task(
    "Ask", ask_specialist, ["question"], "answer",
    awaits=flows.awaits(
        max_turns=6,
        say="Let me ask the specialist.",
        while_waiting=["Still waiting on them."],
        on_timeout={"say": "They aren't answering right now."}),
    on_failure={"max_retries": 0,
                "on_exhaust": {"say": "The specialist couldn't look that up."}},
))

app = flows.App(
    root_flow=parcel,
    agents=[specialist],
    app_display_name=f"Agent Tool ({ARM})",
    model="gemini-composite-v1",
    agent_instruction="You are a parcel desk. Be brief.",
)


if __name__ == "__main__":
  out = f"./agent_tool_{ARM}_app"
  flows.build_app(app, out)
  print(f"built: {out}")
