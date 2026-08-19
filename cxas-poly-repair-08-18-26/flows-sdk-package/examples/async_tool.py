"""Device activation: a flow that waits on an ASYNCHRONOUS backend.

The shape this exists for. Some CES tools take longer than a turn. Declared
`executionType: ASYNCHRONOUS`, such a tool does not run during the call at all — CES
answers the caller with a `{"result": "pending"}` placeholder and delivers the real
payload one or more turns later as a synthetic user turn. Without a primitive for it the
placeholder looks like a failed tool call, and because `on_failure.max_retries` defaults
to zero, the flow escalates on the very first fire.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "activate my phone"             asks for the last four digits
    "9413"                          fires start_activation, then poll_activation
                                    -> "pending"; speaks "activation in progress"
    "ok"                            ASKS FOR THE CALLBACK NUMBER — the wait blocks
                                    the poll task, not the conversation
    "555 0101"                      nothing left to ask, so the wait speaks its
                                    first while_waiting line instead of dead air
    (completion lands)              both inputs are in, so the terminal task fires
                                    and closes out
    (backend never finishes)        after 6 turns, hands off to a human

The headline is the third row. A slow backend used to mean dead air; here the flow
carries on collecting everything that does not depend on the pending result, and the
two converge when the terminal task's inputs are all present.

Each piece earns its place here:

* `@flows.tool(asynchronous=True)` puts `executionType: ASYNCHRONOUS` on the tool
  resource. Nothing else in the SDK emits that key.
* `awaits(max_turns=…)` is what stops the placeholder being read as a failure. It is
  required to be positive: CES enforces no timeout of its own, so a backend that never
  answers would otherwise hold the call forever.
* `say=` is spoken once, when the wait starts. Omitting it gives a silent wait instead —
  the caller hears nothing while the backend works, which is right for a sub-second gap
  and wrong for a thirty-second one.
* `while_waiting=` covers the turns AFTER that, one line per turn, and only the turns
  with nothing else to do. Once the callback number is in there is no question left to
  ask, and a 25-second poll would otherwise be several turns of dead air.
* `answer_first=` decides what a turn carrying BOTH the completion and caller speech
  is. Without it, such a turn is a pure delivery and the utterance is discarded; with
  it, the caller is answered and the terminal is held for at most that many turns.
* `on_timeout` reuses the ordinary `on_exhaust` vocabulary, so giving up routes through
  the same disposition machinery as every other ladder in the framework.
* `poll_activation` is deliberately NOT terminal. The completion lands on a later turn,
  and a terminal fire is already deferred on any turn carrying user text — so the
  closing line belongs to a separate downstream task. The validator enforces this.
* `callback_number` is collected DURING the wait and is what makes the point: it is an
  ordinary slot with no dependency on the poll, so the engine asks it on the next turn.
  `finish` requires both it and `status_msg`, so the flow converges naturally.

Not claimed: the timeout is measured in TURNS, not seconds. The engine has no clock, and
turns are the only tick it has — the same reason the no_input ladder counts reprompts.

Build + validate offline:
    python -m examples.async_tool         # emits ./async_tool_app
"""

from pydantic import BaseModel, Field

import flows


class StartResult(BaseModel):
  order_ref: str = ""
  success: bool = Field(default=True)


class PollResult(BaseModel):
  status_msg: str = ""
  success: bool = Field(default=True)


class DoneResult(BaseModel):
  closing: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="activation")
def start_activation(last_four: str = "") -> StartResult:
  """Kick off activation for the line ending in the given four digits."""
  return StartResult(order_ref=f"ORD-{last_four}")


# The whole point of the example: CES defers this body and answers the call with a
# placeholder, so its return value reaches the flow a turn or more later.
@flows.tool(flow="activation", asynchronous=True)
def poll_activation(order_ref: str = "") -> PollResult:
  """Poll the activation backend. Declared ASYNCHRONOUS."""
  # Imports belong INSIDE a tool body: only the function is rendered into the CES tool
  # file, so a module-level import is not carried and the body dies with a NameError.
  import time
  # Slow on purpose, so the demo actually shows what it claims: the callback number is
  # collected while this is still outstanding, rather than the backend quietly winning
  # the race and the concurrency never being visible in the transcript.
  time.sleep(25)
  return PollResult(
      status_msg="Good news, your device is activated. Restart it to finish.")


@flows.tool(flow="activation")
def finish_activation(status_msg: str = "", callback_number: str = "") -> DoneResult:
  """Close the call out once activation has resolved."""
  tail = f" We'll text the confirmation to {callback_number}." if callback_number else ""
  return DoneResult(closing=f"{status_msg}{tail}")


activation = flows.Flow("activation", root_agent="activation_agent")

activation.add(
    flows.user_slot(
        "last_four",
        ask="What are the last four digits of the line you're activating?",
        hint="last four digits of the mobile number",
    ),
    flows.result_slot("order_ref", "start"),
    flows.result_slot("status_msg", "poll"),
    # Collected WHILE the activation is running. Nothing about it depends on the poll,
    # so the engine asks it on the very next turn instead of leaving the caller in
    # silence — the wait blocks its own task, not the conversation.
    flows.user_slot(
        "callback_number",
        ask="While that's running — what's the best number to text the confirmation to?",
        hint="a number to text the confirmation to",
    ),
    flows.result_slot("closing", "finish"),
)

activation.task(flows.task(
    "start", "start_activation", ["last_four"], "order_ref",
    out_key="order_ref",
))

activation.task(flows.task(
    "poll", "poll_activation", ["order_ref"], "status_msg",
    out_key="status_msg",
    awaits=flows.awaits(
        say="Your activation is now in progress. Please stay on the line.",
        # For the turns AFTER the wait starts, and only the ones with nothing else to
        # do. Once the callback number is collected there is no question left to ask,
        # so without these the caller sits in silence for the rest of the poll and
        # concludes the line dropped. The ladder drains — after the last line the hold
        # goes quiet again, because reassurance on a loop is worse than silence.
        while_waiting=[
            "Still working on it, thanks for waiting.",
            "Almost there.",
        ],
        # The caller often answers the callback question at the very moment the poll
        # replies, and CES packs both into one turn. Without this the turn counts purely
        # as a delivery: the terminal fires at once and the caller's words are dropped.
        # With it they are answered first, and the terminal follows within two turns.
        answer_first=2,
        max_turns=6,
        on_timeout={
            "say": "I'm having trouble activating right now.",
            "then": {"tool": "transfer_to_human"},
        },
    ),
))

# Separate terminal task: the completion arrives on a later turn, so the closing line
# cannot ride on the awaiting task's own fire. It requires BOTH the poll result and the
# number collected during the wait, so it only fires once the two have converged.
activation.task(flows.task(
    "finish", "finish_activation", ["status_msg", "callback_number"], "closing",
    out_key="closing", terminal=True, then_say="{closing}",
))

app = flows.App(
    root_flow=activation,
    app_display_name="Async Activation",
)


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./async_tool_app")
    print("built: ./async_tool_app")
