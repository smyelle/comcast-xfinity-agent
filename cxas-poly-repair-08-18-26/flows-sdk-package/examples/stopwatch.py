"""A timer the caller sets, that speaks by itself when it is up.

The agent has no clock and cannot wake itself. Nothing inside a tool body can create a
turn — the sandbox has no sockets, cannot authenticate back to CES, and every in-process
trigger measured so far is inert. So "say something in thirty seconds" looks impossible.

It is not, because the platform is already producing turns. On a voice call the
`inactivityTimeout` fires during silence and re-arms indefinitely, and a finished
asynchronous body is delivered on the next turn that exists — whoever made it. Put those
together and a timer needs no new mechanism at all: sleep in a deferred tool, and the
completion rides the next tick out to the caller.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "give me twenty seconds"        wait_for is dispatched, deferred
                                    -> "Twenty seconds, starting now."   (awaits.say)
    (says nothing at all)           the tick keeps making turns; the wait holds
    (still says nothing)            the body finishes at 20s and its completion
                                    lands on the next tick
                                    -> "Time's up - that was 20 seconds."

Each piece earns its place here:

* **`asynchronous=True`.** A synchronous body would hold the turn for the whole duration,
  so the caller could not speak and the agent could not narrate. Deferred, the floor is
  released the moment the timer starts.
* **`timeout` above the longest timer.** The platform kills a body at 60 seconds unless
  told otherwise, so a three-minute timer under the default is killed at one and reports a
  failure instead of a result.
* **`awaits.max_turns` sized in TICKS, not caller turns.** With an eight-second timer the
  ticks are what count against it, so a wait meant to last two minutes needs roughly
  fifteen — not the two or three a conversation would suggest.

Driven live over audio, one utterance and then silence:

    t=  7.1  Right, starting now.                      (awaits.say)
    t= 13.5  Still going.                              (while_waiting, on a tick)
    t= 19.7  Not long now.                             (while_waiting, on a tick)
    t= 27.3  Time's up - that was 20 seconds.          (the completion)

The caller spoke once, at the start. Everything after that is the platform's own turns
carrying the agent's voice, with `inactivityTimeout` at 5s.

Not claimed: precision. The completion waits for a turn, so the caller hears it up to one
inactivity interval after the timer really finished — about eight seconds on a `flows
deploy` default, less if you shorten it. And none of this works on a text channel, where no
timer runs and nothing manufactures a turn: there the completion waits for the caller to
speak. Measured in `ces-probes` 117.

Build + validate offline:

    python -m examples.stopwatch          # emits ./stopwatch_app
"""

from pydantic import BaseModel, Field

import flows


class Timer(BaseModel):
  elapsed: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="timer", asynchronous=True, timeout=300)
def wait_for(seconds: str = "") -> Timer:
  """Sleep for the requested number of seconds, then report.

  Deferred, so the caller keeps the floor while it runs. `timeout=300` because the
  platform would otherwise stop the body at sixty seconds and the timer would report a
  failure rather than the time.

  The duration is read with a regex rather than by keeping the digits: dropping
  non-digits turns "2.5" into 25 and "-5" into 5, and a demo people copy should not carry
  a silent tenfold error. Clamped to 1-240s, defaulting to 20 when the caller says
  something like "half a minute" that has no numeral in it at all.

  `elapsed` is a STRING on purpose. A number returned from a tool comes back through JSON
  as a float, so an `int` field would reach the caller as "that was 20.0 seconds"
  (ces-probes 113 measured `42` arriving as `42.0`).
  """
  import re
  import time
  found = re.search(r"[-+]?\d*\.?\d+", str(seconds))
  wanted = float(found.group(0)) if found else 20.0
  n = min(max(wanted, 1.0), 240.0)
  time.sleep(n)
  return Timer(elapsed=f"{n:g}")


def build() -> flows.App:
  """A flow whose only job is to wait, and then say so.

  Returns:
    The assembled app.
  """
  timer = flows.Flow("timer", root_agent="Acme_Timer")
  timer.add(
      flows.user_slot("seconds", ask="How many seconds shall I give you?",
                      hint="a number of seconds"),
      flows.result_slot("elapsed", "run_timer"),
      flows.announce("ring", ["Time's up - that was {elapsed} seconds."],
                     requires=["elapsed"], preempt=True, end=True),
  )
  timer.task(flows.task(
      "run_timer", "wait_for", ["seconds"], "elapsed", out_key="elapsed",
      awaits=flows.awaits(
          say="Right, starting now.",
          # Ticks, not caller turns: on a silent call these are all the turns there are.
          max_turns=40,
          while_waiting=["Still going.", "Not long now."],
          on_timeout={"say": "I lost track of that timer, sorry."})))
  return flows.App(root_flow=timer, app_display_name="Acme Timer")


app = build()


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  for line in errors + warnings:
    print(" ", line)
  if not errors:
    flows.build_app(app, "./stopwatch_app", overwrite=True)
