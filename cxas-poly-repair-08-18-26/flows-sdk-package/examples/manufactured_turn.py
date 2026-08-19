"""An outstanding question is not put again on a turn the caller did not take.

On voice the platform sends turns nobody took. Two kinds, and this example needs both on
one call:

* an **inactivity tick**, when the caller stops talking
* a **completion push**, when an asynchronous check finishes

Both used to be answered like ordinary turns, so a question already put to the caller was
put again on each. Measured on a deployed agent, one wording landed at 20.3s, 30.3s and
45.6s -- their own turn, then a push, then a tick -- word for word, while they were still
thinking about the first.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "my internet keeps dropping"    starts the line check in the BACKGROUND and
                                    asks rung one: which device?
    (thinking)                      the check finishes. A completion push arrives
                                    on a turn nobody took.  <-- silent
    (still thinking)                inactivity ticks.                  <-- silent
    "I'm not sure what you mean"    unusable, so the question is put again -- and
                                    because the polls spent nothing, what they
                                    hear is rung TWO, not the last-resort rung.

The check is a slow backend behind a SHORT wait, which is what puts a completion push and
an open question on the call at once. While a task is awaited the outstanding question is
the wait, not the ask, and that has its own ladder. Here the wait gives up after a couple
of ticks, the flow moves on to ask about the device, and the check answers anyway half a
minute in -- so the push lands with the ask outstanding, which is the incident.

The three-rung ladder is what makes the failure legible rather than merely annoying. The
last rung hands the caller off, so the wrong behaviour is not "the agent repeated itself"
but "the agent gave up on someone who had not spoken yet".

Build + validate:
    python -m examples.manufactured_turn          # emits ./manufactured_turn_app

The evidence needs real ticks and a real push, and neither can be faked from the text
channel -- CES rejects a hand-written `<context>` marker as malicious input -- so the
demo is driven over audio:
    python -m examples.manufactured_turn_drive --app <resource>

`MANUFACTURED_TURN_VERIFY.md` has the commands, the app ids and the transcripts.
"""

import argparse
import sys

from pydantic import BaseModel, Field

import flows

# The A/B. Silence is the one thing an author can have a POLICY about, and the fix must
# not touch it: `no_input` still owns every inactivity tick it owned before. The two arms
# differ by this block and nothing else.
#
#   default    no flow-level `no_input`. Ticks and pushes are held silently -- the new
#              behaviour, and what the ladder used to nag through.
#   --no-input the author's silence policy is declared. The ticks belong to IT again:
#              the reprompts are spoken out loud and the exhaust still fires.
#
# A completion push is NOT silence and reaches neither ladder, so it stays held in both.
WITH_NO_INPUT = "--no-input" in sys.argv

NO_INPUT = {
    "reprompts": ["Are you still there?",
                  "I can still hear the line -- take your time."],
    "on_exhaust": {"say": "I'll let you go for now. Call us back any time."},
}

# Rung three hands the caller off. A ladder whose rungs are three paraphrases would make
# this demo look like a cosmetic complaint; the cost of burning it is that someone who
# never spoke is escalated.
DEVICE_LADDER = [
    "Which device is having trouble -- the TV box, or something else?",
    "Sorry, I need to know which one. Is it the TV box, the internet box, or a phone?",
    "Let me get someone who can take a proper look at this with you.",
]


class LineCheck(BaseModel):
  line_state: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="connection", asynchronous=True)
def check_line(reason: str = "") -> LineCheck:
  """Check the line from the head end. Declared ASYNCHRONOUS, and NOT awaited."""
  # Imports belong INSIDE a tool body: only the function is rendered into the CES tool
  # file, so a module-level import is not carried and the body dies with a NameError.
  import time
  # Long enough that the completion lands while the caller is still thinking about the
  # question, which is the whole point. Short enough that a tick follows it on one call.
  time.sleep(25)
  return LineCheck(line_state="upstream signal is low on the segment")


connection = flows.Flow("connection", root_agent="connection_agent")

if WITH_NO_INPUT:
  connection.set("no_input", flows.no_input(
      reprompts=list(NO_INPUT["reprompts"]),
      on_exhaust=dict(NO_INPUT["on_exhaust"])))

connection.add(
    flows.user_slot("reason", ask="What's going on with your connection?",
                    hint="what the caller says is wrong"),
    # THE OUTSTANDING QUESTION. Put once, and then the polls arrive.
    flows.user_slot("device", ask=DEVICE_LADDER, requires=["reason"],
                    hint="which piece of equipment is affected"),
    flows.result_slot("line_state", "check"),
)

# A SHORT wait on a slow backend, which is how a completion push and an open question end
# up on the call together. The wait gives up after a couple of ticks and the flow moves on
# to ask about the device; the check answers anyway, half a minute in, and CES delivers it
# as a turn of its own with that question still outstanding.
#
# The validator will not accept an asynchronous task with no `awaits` at all -- CES answers
# the call itself with a `pending` placeholder, which reads as a failure and escalates the
# flow on the first fire -- so "fire and forget" is spelled as a wait that expects to lose.
connection.task(flows.task(
    "check", "check_line", ["reason"], "line_state", out_key="line_state",
    requires=["reason"],
    awaits=flows.awaits(
        say="Let me check the line while we talk.",
        max_turns=2,
        on_timeout={"say": "I'll keep looking at that in the background."},
    ),
))

app = flows.App(
    root_flow=connection,
    app_display_name=("Manufactured turn (ask ladder)"
                      + (" NO_INPUT" if WITH_NO_INPUT else "")),
    model="gemini-composite-v1",
    agent_instruction=(
        "You help with a home internet connection. Ask what the engine tells you to "
        "ask and nothing else, and never invent a question of your own."
    ),
)


def main(argv):
  ap = argparse.ArgumentParser(description="manufactured-turn ask-ladder demo")
  ap.add_argument("--out", default="")
  ap.add_argument("--no-input", action="store_true",
                  help="declare a flow-level silence policy (the A/B arm)")
  args = ap.parse_args(argv)
  out = args.out or ("./manufactured_turn_no_input_app" if WITH_NO_INPUT
                     else "./manufactured_turn_app")

  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if errors:
    return 1
  flows.build_app(app, out, overwrite=True)
  print(f"built -> {out}")
  print(f"  arm: {'no_input DECLARED' if WITH_NO_INPUT else 'no silence policy'}")
  print("  the wait gives up before the check answers, so the completion lands on a "
        "turn of its own with the device question still open")
  return 0


if __name__ == "__main__":
  # `sys.argv[1:]` explicitly, so the driver-rot test can call main([]) without argparse
  # reading pytest's command line.
  raise SystemExit(main(sys.argv[1:]))
