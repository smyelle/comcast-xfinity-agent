#!/usr/bin/env python3
"""Does the caller's SPOKEN answer to the mid-sweep scoping question get captured?

`demo_voice.py` sends the walkthrough turns as TEXT on an audio session, and that is
exactly the case this question survives. The one that does not is the caller ANSWERING
OUT LOUD while the diagnostics job is still out — a live call lost the answer on that
path, and no text driver can see it, because a text turn cannot arrive over the top of
a line the agent is in the middle of speaking.

So the answer goes into the SAME continuous audio buffer as the complaint and the
account number, at a chosen offset into the wait. That is what a real caller does, and
it is the only way to reproduce the collision: the reassurance ladder speaks on the
platform's inactivity ticks, so whether the answer lands over a line is a matter of
timing rather than of intent.

Scored, not just printed, and scored on TWO channels, because either alone lies:

  * the ENGINE LOG — `wifi_scope_early` filled. The slot is cue-only, so a fill is a
    deterministic match on text that reached the engine. No fill means the engine never
    saw the answer, whatever the transcript shows.
  * the SPOKEN LINE — the acknowledgement. A filled slot the caller never hears
    acknowledged is still a broken turn, and the failure being chased is a SILENT one.

    APP_ID=<uuid> python tests/scope_voice_probe.py --runs 3
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
# HERE too, and imported as a bare module below rather than as `tests.demo_voice`: the
# SDK checkout carries a `tests` package of its own, and with `packages/flows` on the
# path it wins the name and this file's siblings become unreachable.
sys.path.insert(0, HERE)

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

from cxas_scrapi.core.sessions import Modality, Sessions  # noqa: E402
from demo_voice import (  # noqa: E402
    ACCOUNT, _digits, _engine, _print, _silence, _spoken, _tts,
)

PROJECT = "ces-deployment-dev"
LOCATION = "us"

# The answer, and the value the cues must resolve it to. "Everything seems to be having
# trouble" is the utterance the live call lost; it matches `\beverything\b(?!\s+else)`.
ANSWER = "everything seems to be having trouble"
SCOPE_SLOT = "wifi_scope_early"
# A substring of `scripts.SAY_SCOPE_NOTED`, and it has to be a long one. "that helps"
# alone is inside the QUESTION ("one thing that helps either way"), so it matched on every
# run and made four lost answers read as acknowledged ones.
ACK_CUES = ("got it, that helps",)


def _scope_value(resp) -> str:
  """What the early scope slot ended this run holding, read off the engine's own state.

  From `filled` rather than from the log, and that is deliberate: `sm["_log"]` is
  bounded, so on a long wait the fill can scroll out of it while the slot itself is
  perfectly well set. Every `updatedVariables` chunk carries the whole slot machine, so
  the last one to mention the slot is the answer.
  """
  value = ""
  for output in resp.outputs:
    di = getattr(output, "diagnostic_info", None)
    for message in (getattr(di, "messages", None) or []):
      for chunk in getattr(message, "chunks", []):
        kind = chunk._pb.WhichOneof("data") if hasattr(chunk, "_pb") else None
        if kind != "updated_variables":
          continue
        sm = (dict(getattr(chunk, kind)) or {}).get("sm") or {}
        try:
          filled = dict(dict(sm).get("filled") or {})
        except (TypeError, ValueError):
          continue
        if filled.get(SCOPE_SLOT):
          value = str(filled[SCOPE_SLOT])
  return value


def _acknowledged(resp) -> bool:
  return any(cue in text.lower()
             for _turn, text, _ts in _spoken(resp) for cue in ACK_CUES)


def _drive(app: str, a, run: int) -> tuple[bool, bool]:
  sess = Sessions(app_name=app)
  sid = f"scopevoice-{int(time.time())}-{run}"
  spoken_account = _digits(a.account)
  # ONE buffer. The silence on either side of the answer is what makes the platform cut
  # its own turns: the account number, then the ticks the sweep is polled on, then the
  # answer, then more ticks. Splitting it into two `run` calls would hand the answer a
  # clean turn boundary the caller never gets.
  # The HESITATION is not decoration, and the live failure does not reproduce without
  # it. The reassurance ladder advances one line per idle turn, so a caller who thinks
  # out loud before answering buys the agent an extra turn and pulls the next line
  # forward — onto the moment they were about to speak. On the call this probe is
  # written from, "Uh" landed at 45.4s, the line at 46.1s, and the answer at 46.3s, over
  # the top of it. Without the "Uh" the answer arrives in a gap and is captured, which
  # is why this looked fine from a text driver and from a first voice drive.
  hesitation = (_tts(a.hesitate) + _silence(a.hesitate_gap)) if a.hesitate else b""
  answer = _tts(a.answer) + _silence(a.hold)
  # `--split` is the CONTROL, and it is the difference between a defect and a driver
  # artifact. One buffer means the answer shares a `run` call with the hesitation, so a
  # platform that simply cannot start a second turn inside one stream would lose it for
  # reasons that have nothing to do with the agent. Split, the answer gets its own call
  # and its own clean turn boundary; if it is STILL lost, the sequencing is the defect.
  opening = (_tts(a.complaint) + _silence(a.gap)
             + _tts(f"my account number is {spoken_account}")
             + _silence(a.answer_at) + hesitation)
  if not a.split:
    opening += answer
  print(f"\n=== run {run}  session {sid}{'  [SPLIT]' if a.split else ''}")
  print(f"[caller] {a.complaint!r} … {a.gap:.0f}s … account … "
        f"{a.answer_at:.0f}s silence … {a.hesitate!r} … {a.answer!r} … "
        f"{a.hold:.0f}s silence")
  start = time.time()
  resp = sess.run(sid, audio=opening + (_silence(a.hold) if a.split else b""),
                  modality=Modality.AUDIO)
  _print(_spoken(resp), start)
  if a.split:
    resp2 = sess.run(sid, audio=answer, modality=Modality.AUDIO)
    _print(_spoken(resp2), start)
    resp = resp2 if not _scope_value(resp) else resp
  value, acked = _scope_value(resp), _acknowledged(resp)
  if a.engine:
    for _t, line in _engine(resp):
      print(f"        . {line[:150]}")
  print(f"  -> {SCOPE_SLOT} = {value or '(unfilled)'}   acknowledged out loud: {acked}")
  return bool(value), acked


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--app", default=os.environ.get("APP_ID", ""))
  ap.add_argument("--runs", type=int, default=3)
  ap.add_argument("--account", default=ACCOUNT)
  ap.add_argument("--complaint", default="my internet is not working")
  ap.add_argument("--answer", default=ANSWER)
  ap.add_argument("--gap", type=float, default=11.0)
  # 17s, because that is where the collision is. The reassurance line lands ~16.5s after
  # the question (two silent ticks, then the first spoken one), and the answer has to
  # arrive over the top of it for the failure to happen at all. Driven against the same
  # build: 12s and 15s captured the answer every time, 17s and 19s lost it every time.
  ap.add_argument("--answer-at", type=float, default=17.0, dest="answer_at",
                  help="silence between the account number and the answer — how far "
                       "into the wait the caller speaks")
  ap.add_argument("--hesitate", default="uh",
                  help="what the caller says before the answer; '' for none. See the "
                       "comment in _drive — the failure needs it")
  ap.add_argument("--hesitate-gap", type=float, default=0.8, dest="hesitate_gap")
  ap.add_argument("--hold", type=float, default=16.0,
                  help="silence after the answer, so the verdict has ticks to land on")
  ap.add_argument("--split", action="store_true",
                  help="send the answer as its own call — the control; see _drive")
  ap.add_argument("--engine", action="store_true")
  a = ap.parse_args(argv)
  if not a.app:
    ap.error("--app (or APP_ID) is required")
  app = a.app if a.app.startswith("projects/") else (
      f"projects/{PROJECT}/locations/{LOCATION}/apps/{a.app}")

  results = [_drive(app, a, i + 1) for i in range(a.runs)]
  captured = sum(1 for filled, _ in results if filled)
  acked = sum(1 for _, ack in results if ack)
  print(f"\n{captured}/{a.runs} runs captured the spoken answer "
        f"({SCOPE_SLOT} filled); {acked}/{a.runs} acknowledged it out loud")
  return 0 if captured == a.runs and acked == a.runs else 1


if __name__ == "__main__":
  raise SystemExit(main())
