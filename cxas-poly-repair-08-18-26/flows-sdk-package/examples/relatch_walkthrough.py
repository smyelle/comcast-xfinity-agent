"""One tip at a time, however long the caller takes.

A walkthrough hands out steps the caller has to go and DO -- unplug the router, move it,
change a socket. Each one takes minutes, and the next must wait until they come back and
say something. `since_turns` is the gate for that: it counts real caller utterances, so a
platform silence tick cannot open it while they are behind the television.

The step latches a slot as it speaks and the next step waits a turn on that latch. It
works exactly once. A rung's `sets` will not overwrite a slot that is already filled --
deliberately, so a late announce cannot rewrite a verdict already spoken -- so the second
step's write is skipped, the clock stays on the FIRST step, and from there every
remaining gate reads as open:

    control     tip 1 ..... caller replies ..... tips 2 AND 3, together
    treatment   tip 1 ..... caller replies ..... tip 2 ..... replies ..... tip 3

`relatch=True` is the slot saying it is a latch that re-arms. Nothing else changes: the
slot stays filled, so a plain `filled` test reads the same, and every slot that does not
ask for it keeps the guard.

Build both arms:
    python -m examples.relatch_walkthrough              # treatment
    python -m examples.relatch_walkthrough --control    # no relatch

Drive it (macOS, for `say`):
    python -m examples.relatch_walkthrough_drive --app <resource>

RELATCH_WALKTHROUGH_VERIFY.md has the commands, the apps and the transcripts.
"""

import argparse
import sys

import flows

CONTROL = "--control" in sys.argv

TIPS = [
    "Start by unplugging the router, waiting thirty seconds, then plugging it back in.",
    "Next, move the router away from anything metal or electrical.",
    "Last thing: switch the TV box over to the other wall socket.",
]

# The latch, and the ONLY difference between the arms. Every step fills it as it speaks;
# every step after the first waits a caller turn on it.
tip_given = (flows.event_slot("tip_given") if CONTROL
             else flows.event_slot("tip_given", relatch=True))

walkthrough = flows.Flow("walkthrough", root_agent="Support_Agent")

parts = [
    flows.user_slot("problem", ask="What's going wrong with your connection?",
                    hint="what the caller says is wrong"),
    tip_given,
]
for i, text in enumerate(TIPS, 1):
  gate = [{"slot": f"tip{i}_done", "filled": False}]
  if i > 1:
    # "the caller has replied to the previous step" -- the whole ladder, declared.
    gate = [{"slot": f"tip{i - 1}_done", "filled": True},
            flows.since("tip_given", turns=1)] + gate
  parts.append(flows.announce(
      f"Tip{i}", [text], requires=["problem"],
      condition={"all": gate} if len(gate) > 1 else gate[0],
      sets={"tip_given": "true", f"tip{i}_done": "true"},
      preempt=True))
  parts.append(flows.event_slot(f"tip{i}_done"))

# Only reachable once the third tip has been given AND answered, which is the same gate
# again. In the control it is reached a turn early, on a caller who never answered.
parts.append(flows.announce(
    "Exhausted",
    ["That's everything I can suggest from here -- let me get an engineer to call you."],
    requires=["problem"],
    condition={"all": [{"slot": "tip3_done", "filled": True},
                       flows.since("tip_given", turns=1)]},
    preempt=True, end=True))

walkthrough.add(*parts)

app = flows.App(
    root_flow=walkthrough,
    app_display_name="Relatch walkthrough" + (" CONTROL" if CONTROL else ""),
    model="gemini-composite-v1",
    agent_instruction=(
        "You are walking a caller through fixing their internet. Say exactly what the "
        "engine gives you and nothing more; never invent a step of your own, and never "
        "give two steps in one turn."
    ),
)


def main(argv):
  ap = argparse.ArgumentParser(description="relatch walkthrough demo")
  ap.add_argument("--out", default="")
  ap.add_argument("--control", action="store_true",
                  help="drop `relatch` -- the arm that runs ahead of the caller")
  args = ap.parse_args(argv)
  out = args.out or ("./relatch_walkthrough_control_app" if CONTROL
                     else "./relatch_walkthrough_app")

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
  print(f"  arm: {'CONTROL (no relatch)' if CONTROL else 'TREATMENT (relatch)'}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main(sys.argv[1:]))
