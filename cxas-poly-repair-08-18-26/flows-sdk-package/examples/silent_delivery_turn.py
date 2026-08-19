"""A turn the caller did not take must not answer a question for them.

`examples/async_tool.py` is the shape this starts from: a task declared
`asynchronous=True`, whose result arrives one or more turns after the call that started
it. This example is about the TURN THAT DELIVERY LANDS ON, and specifically about the one
where the caller said nothing at all.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "my heater won't come on"       asks for the serial number
    "4417"                          asks what the unit is doing
    "it's not heating at all, and   starts the diagnostic. The wait engages and
     the display is not lighting     speaks its holding line.
     up"
    (says nothing)                  inactivity ticks. The holding ladder drains.
    (still says nothing, and the    THE TURN THIS EXAMPLE EXISTS FOR. The finding
     completion lands here)          lands, and with it the flow's one remaining
                                    question: shall I push the fix? Nobody has
                                    answered it, and nobody must be recorded as
                                    having answered it.
    "yes please"                    the fix is applied

Why that turn is dangerous. A deterministic `option_cues` match needs the caller's words,
and on a within-turn re-invoke the engine no longer has them — `last_user_text` is empty
after a setter has run — so it falls back to `scanned_user_text`, the newest real
utterance in the history. On an ordinary turn that IS this turn's own words. On an
inactivity tick, and on a completion delivery the caller put no utterance on, it is a
PREVIOUS turn's.

Here the previous turn is the fault description, and "it's **not** heating at all" carries
the offer slot's own decline cue. So the cue pass scored a sentence about a broken heater
as a refusal of an offer that had not been made when it was spoken, `push_fix` took
`DECLINE`, and the rung behind `DECLINE` ends the call. The caller was hung up on for
turning down something they were never asked.

Each piece earns its place here:

* `requires=["finding"]` ON THE OFFER is what makes this the delivery turn's problem and
  not the previous turn's. An UNGATED offer becomes the awaited question on the very turn
  the fault is described, and the cue pass fills it from that turn's own words — which is
  correct, because those words really are this turn's, and it is the behaviour the
  fallback exists for. Measured both ways in `_demo_run`. Gating the offer on the finding
  is also the honest modeling: there is nothing to offer until the diagnostic says what is
  wrong.
* `answer_first=2` COVERS A DIFFERENT HALF of the same turn. It decides what a turn
  carrying BOTH a completion and caller speech IS; without it such a turn counts as a pure
  delivery and the utterance is discarded before any of this. The two guards are
  independent, and a demo needs both: `answer_first` keeps the words, the stale-scan guard
  keeps the SILENCE from being read as words.
* THE DECLINE RUNG IS TERMINAL, on purpose. A demo where the wrong fill is merely recorded
  proves nothing a reader will act on. Here the wrong fill ends the call, which is what
  made the defect worth a fix and what makes the transcript legible: with the guard the
  call continues and the caller is asked; without it the call is over.
* `while_waiting` DRAINS RATHER THAN CYCLES, so the hold goes quiet after two lines. That
  matters to this demo rather than being decoration — the quiet turns are the inactivity
  ticks the guard has to survive, and a ladder that cycled would hide how many there were.

Build + validate + drive the offline engine:
    python -m examples.silent_delivery_turn      # emits ./silent_delivery_turn_app

...then deploy and ASSERT the delivery turn against the real CES runtime:
    python -m examples.silent_delivery_turn --live projects/<p>/locations/us/apps/<id>

The `--live` arm covers the half a TEXT channel can reach: a caller who DOES answer on the
delivery turn is still captured. The silent half needs real inactivity ticks, so it is
driven over audio by `examples/silent_delivery_turn_drive.py`.
"""

from pydantic import BaseModel, Field

import flows

# The caller's description of the fault, and the reason this example is not hypothetical.
# It is about a heater. It contains "not". The offer slot's decline cues are the plainly
# authored words any author would write, and one of them is inside it — which is the
# point: a stale utterance is scored against a question it was never a reply to, so no
# amount of care over the cue list saves you.
OFFER_CUES = {
    "ACCEPT": ["yes", "sure", "go ahead", "please do", "do it"],
    "DECLINE": ["no", "not now", "rather not", "leave it"],
}


class Finding(BaseModel):
  finding: str = ""
  success: bool = Field(default=True)


class FixResult(BaseModel):
  closing: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="diagnostics", asynchronous=True)
def run_diagnostic(serial: str = "", fault: str = "") -> Finding:
  """Run the remote diagnostic on a unit. Declared ASYNCHRONOUS."""
  # Imports belong INSIDE a tool body: only the function is rendered into the CES tool
  # file, so a module-level import is not carried and the body dies with a NameError.
  import time
  # Slow on purpose. The whole example is about which turn the answer lands on, and a
  # backend that wins the race lands on the same turn that started it.
  time.sleep(30)
  return Finding(finding="the burner control board is running old firmware")


@flows.tool(flow="diagnostics")
def push_firmware(serial: str = "") -> FixResult:
  """Push the firmware update to the unit."""
  return FixResult(closing=f"Done — {serial} is updating now, it takes about a minute.")


diagnostics = flows.Flow("diagnostics", root_agent="diagnostics_agent")

diagnostics.add(
    flows.user_slot("serial", ask="What's the serial number on the unit?",
                    hint="the serial number from the sticker"),
    flows.user_slot("fault", ask="And what's it actually doing?",
                    hint="the caller's description of the fault"),
    flows.result_slot("finding", "diagnose"),
    # THE SLOT THIS EXAMPLE IS ABOUT. Gated on the finding, so it becomes the awaited
    # question exactly when the completion lands — which, if the caller has gone quiet,
    # is a turn nobody spoke on.
    flows.intent_slot(
        "push_fix", OFFER_CUES,
        ask="I can push a firmware update from here — want me to do that?",
        requires=["finding"],
    ),
    flows.result_slot("closing", "apply_fix"),
)

diagnostics.task(flows.task(
    "diagnose", "run_diagnostic", ["serial", "fault"], "finding",
    out_key="finding",
    awaits=flows.awaits(
        say="Let me run a diagnostic on it — that takes about half a minute.",
        while_waiting=[
            "Still going, thanks for hanging on.",
            "Nearly done.",
        ],
        # Without this, a turn carrying BOTH the completion and the caller's answer is a
        # pure delivery and the answer is thrown away. That is the other half of "a
        # caller who answers on the delivery turn is still captured", and it is a
        # different mechanism from the stale-scan guard — see the module docstring.
        answer_first=2,
        max_turns=12,
        on_timeout={
            "say": "I can't get a reading on it right now.",
            "then": {"tool": "transfer_to_human"},
        },
    ),
))

diagnostics.task(flows.task(
    "apply_fix", "push_firmware", ["serial"], "closing",
    out_key="closing", terminal=True, then_say="{closing}",
    requires=["push_fix"],
    condition={"slot": "push_fix", "eq": "ACCEPT"},
))

# THE HARM, made audible. A wrong `DECLINE` does not sit in a log — it ends the call.
diagnostics.add(flows.announce(
    "left_as_is",
    ["No problem, I'll leave the firmware as it is. Give us a call back if it keeps "
     "playing up. Bye for now."],
    requires=["push_fix"],
    condition={"slot": "push_fix", "eq": "DECLINE"},
    preempt=True, end=True,
))

app = flows.App(
    root_flow=diagnostics,
    app_display_name="Heater Diagnostics (silent delivery turn)",
    agent_instruction=(
        "You help with a heater that is not working. Collect the serial number and what "
        "the unit is doing, then run the diagnostic. Do not decide anything about the "
        "firmware update yourself — the caller answers that question."
    ),
)


def _demo_run() -> None:
  """Drive the delivery turn four ways against the blessed engine.

  Offline is enough for this one claim, because the whole decision is the engine's: the
  cue pass either reads the stale scan or refuses to. What offline CANNOT show is a
  genuine inactivity tick, which is why there is an audio driver beside this file.

  The last arm is the counterfactual. Popping `_stale_scan` off `sm` is exactly what the
  engine did before the guard existed — the flags do not outlive a turn's first pass, so
  an unlatched version holds once and the cascade pass fills the slot anyway.
  """
  import logging

  from flows.engine import loader as fb

  # The engine mirrors `sm["_log"]` to the python logger, and arming the wait offline
  # goes through the placeholder path, which logs the deferred call as an unsuccessful
  # one. It is not a finding — it is what CES answers an ASYNCHRONOUS call with — so it
  # is kept out of the demo's own output and read back off `sm` below instead.
  logging.getLogger().setLevel(logging.ERROR)

  config = app.root_flow.to_config()
  fault = "it is not heating at all and the display is not lighting up"

  def fresh_session():
    sm = fb.seed_sm(config)
    sm["filled"] = {"serial": "4417", "fault": fault}
    sm["pending"] = {}
    # A session already under way. Without this the engine reads the scan as the
    # OPENING routing utterance, which is a different (and legitimate) cue path.
    sm["_config_id"] = "diagnostics"
    gate = sm.get("_gate_slot") or config.get("gate_slot")
    if gate:
      sm[gate] = sm["filled"][gate] = "diagnostics"
    return sm

  def turn(sm, *, spoke="", tick=False, completion=False):
    return fb.load_engine().slot_filling_engine({
        "raw_config": config, "sm": sm, "last_user_text": spoke,
        "scanned_user_text": spoke or fault, "is_inactivity": tick,
        "event_data": {}, "config_id": "diagnostics", "n_user_turns": 3,
        "async_completion_landed": completion,
    })

  def waiting_session():
    """A session on the turn the caller described the fault: the task has fired and the
    wait is engaged, so the delivery turns below are the real thing rather than a
    result dropped into a flow that never asked for one."""
    sm = fresh_session()
    turn(sm, spoke=fault)
    # CES answers an ASYNCHRONOUS call with a placeholder rather than the payload, and
    # it is that placeholder — not the dispatch — that arms the wait. Skipping it leaves
    # the completion arriving at a flow that never recorded it was waiting.
    sm.update(fb.run_intake("run_diagnostic", {"result": "pending"}, sm)["sm"])
    turn(sm)
    return sm

  def land_the_finding(sm):
    sm.update(fb.run_intake("run_diagnostic",
                            {"success": True, "finding": "old firmware"}, sm)["sm"])

  # 1. THE REGRESSION. Completion on a turn the caller said nothing on.
  sm = waiting_session()
  land_the_finding(sm)
  turn(sm, completion=True)
  turn(sm)                                   # the cascade pass, same turn
  print(f"  silent delivery turn        -> push_fix={sm['filled'].get('push_fix')!r}")
  # The engine's own account of why, so the pass is not just an absence.
  for entry in sm.get("_log") or []:
    if entry.get("tag") == "stale_scan_withheld":
      print(f"       {entry['tag']} {entry['data']}")
      break

  # 2. An inactivity tick, the other turn nobody took.
  sm = waiting_session()
  land_the_finding(sm)
  turn(sm, tick=True)
  turn(sm)
  print(f"  inactivity tick             -> push_fix={sm['filled'].get('push_fix')!r}")

  # 3. THE COST SIDE. A caller who answers on the delivery turn is still captured.
  sm = waiting_session()
  land_the_finding(sm)
  turn(sm, spoke="yes please", completion=True)
  print(f"  answered on the delivery    -> push_fix={sm['filled'].get('push_fix')!r}")

  # 4. The counterfactual: the same silent turn with the latch taken off.
  sm = waiting_session()
  land_the_finding(sm)
  turn(sm, completion=True)
  sm.pop("_stale_scan", None)
  turn(sm)
  print(f"  ...with the latch removed   -> push_fix={sm['filled'].get('push_fix')!r}"
        "   <- the call would end here")


# (label, [(caller turn, seconds to wait BEFORE sending it)], substring the last reply
# must contain).
#
# Over text every turn carries an utterance, so the SILENT half is out of reach here by
# construction — that is the audio driver's job. What text proves is the half a fix could
# quietly break: the caller who does speak on the delivery turn is still captured.
#
# The waits are real seconds and they are load-bearing. The diagnostic sleeps thirty of
# them, and a text turn sent immediately arrives long before the completion — so without
# the pauses the last turn is an ordinary one and the check proves nothing at all.
#
# The opening line is "hello" rather than a description of the fault, deliberately: told
# what is wrong up front, the model fills `fault` from that sentence and the diagnostic
# starts a turn early, which leaves the SERIAL as the newest utterance in the history.
# The stale text has to be the fault description for the collision to exist.
LIVE_CHECKS = [
    ("the offer waits for the finding",
     [("hello", 0), ("the serial is 4417", 0),
      ("it is not heating at all and the display is not lighting up", 0),
      ("any news?", 12)],
     None),
    ("answered on the delivery turn -> captured",
     [("hello", 0), ("the serial is 4417", 0),
      ("it is not heating at all and the display is not lighting up", 0),
      ("any news?", 14), ("how about now?", 14),
      ("yes please, go ahead", 12)],
     "updating now"),
]


def _live_run(app_resource: str, app_dir: str, cxas_bin: str = "cxas") -> int:
  """Deploy and drive the text-reachable half against the real CES runtime."""
  import time

  import cxas_scrapi
  from flows.deploy.push import deploy

  deploy(app_dir, app_resource, cxas=cxas_bin)
  sessions = cxas_scrapi.Sessions(app_name=app_resource)
  failures = []
  for label, turns, want in LIVE_CHECKS:
    session_id = sessions.create_session_id()
    started = time.time()
    said = ""
    print(f"\n  {label}")
    for text, pause in turns:
      if pause:
        time.sleep(pause)
      res = sessions.run(session_id=session_id, text=text)
      said = (sessions.get_agent_text_from_outputs(res.outputs) or "").strip()
      print(f"    t={time.time() - started:5.1f} > {text}\n            < {said[:130]}")
    # `want=None` asserts the ABSENCE of a decision: the offer has not been answered,
    # and in particular the flow has not closed on a refusal nobody made.
    ok = (want.lower() in said.lower() if want
          else "leave the firmware as it is" not in said.lower())
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
      failures.append(f"{label}: wanted {want!r}" if want
                      else f"{label}: the call closed on a refusal nobody made")
  print(f"\n{len(LIVE_CHECKS) - len(failures)}/{len(LIVE_CHECKS)} live checks passed")
  for f in failures:
    print(f"  FAILED {f}")
  return 1 if failures else 0


if __name__ == "__main__":
  import argparse
  import sys

  ap = argparse.ArgumentParser(description="silent delivery-turn demo")
  ap.add_argument("--out", default="./silent_delivery_turn_app")
  ap.add_argument("--live", metavar="APP_RESOURCE",
                  help="deploy to this CES app and drive the text-reachable half")
  ap.add_argument("--cxas", default="cxas",
                  help="path to the cxas CLI when it is not on PATH")
  args = ap.parse_args()

  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  assert errors == [], errors
  _demo_run()
  flows.build_app(app, args.out, overwrite=True)
  print(f"built -> {args.out} (proves: a completion delivery the caller said nothing "
        "on does not answer the flow's open question)")
  if args.live:
    print(f"\nlive: {args.live}")
    sys.exit(_live_run(args.live, args.out, args.cxas))
