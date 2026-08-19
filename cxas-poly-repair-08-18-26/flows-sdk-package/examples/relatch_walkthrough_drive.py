"""Drive `relatch_walkthrough` over VOICE and count the steps per turn.

The claim is about PACING, so the measurement is one number: how many walkthrough steps
the caller hears on each turn they take. One is right. Two on a turn means the ladder
ran ahead of them, which live is the difference between being helped and being read a
list while you are behind the television.

    python -m examples.relatch_walkthrough_drive --app <treatment resource>
    python -m examples.relatch_walkthrough_drive --app <control resource> --control

macOS only: `say` and `afconvert` ship with the OS, so caller audio needs no TTS
credentials and no ffmpeg.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import tempfile
import time
import wave

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2

OPENING = "my internet keeps dropping out"
# What the caller says when they come back from doing a step. Deliberately plain: it
# answers the step without supplying anything the flow collects, so the only thing that
# can move the walkthrough on is the turn itself.
REPLIES = ["okay, I did that", "right, that's done", "okay, tried that one too"]

# Matched loosely: each step is a directive the model words itself, so the assertion is
# on a distinctive phrase rather than the authored sentence.
STEP_CUES = ["unplug", "away from", "socket"]
HANDOFF = "engineer"


def _tts(phrase: str) -> bytes:
  """Render a phrase to raw 16kHz mono LINEAR16 with macOS speech synthesis."""
  if platform.system() != "Darwin":
    raise RuntimeError(
        "this driver needs macOS `say` and `afconvert` to synthesize caller audio; this"
        f" is {platform.system()}.")
  with tempfile.TemporaryDirectory() as tmp:
    aiff, wav = os.path.join(tmp, "s.aiff"), os.path.join(tmp, "s.wav")
    subprocess.run(["say", "-o", aiff, phrase], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                    aiff, wav], check=True, capture_output=True)
    with wave.open(wav, "rb") as fh:
      return fh.readframes(fh.getnframes())


def _silence(seconds: float) -> bytes:
  return b"\x00" * int(seconds * SAMPLE_RATE * SAMPLE_WIDTH)


def main(argv: list[str] | None = None) -> int:
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--app", default="", help="the deployed CES app resource")
  ap.add_argument("--control", action="store_true",
                  help="grade the control arm, which is EXPECTED to run ahead")
  ap.add_argument("--gap", type=float, default=14.0,
                  help="how long the caller takes to go and do each step")
  ap.add_argument("--tail", type=float, default=15.0)
  ap.add_argument("--timeout", type=float, default=300.0)
  # `argv or []`, NOT sys.argv: the driver-rot test calls main() with no arguments.
  args = ap.parse_args(argv or [])

  if not args.app:
    print("dry run -- no --app, so no call is placed. The call this would make:")
    print(f"  caller: {OPENING!r}")
    for r in REPLIES:
      print(f"  ...{args.gap:.0f}s doing the step, then: {r!r}")
    print("Treatment: one step per turn. Control: steps two and three together.")
    return 0

  buf = _tts(OPENING)
  # When the caller STOPS talking each time. Everything the agent says between two of
  # these belongs to one turn, which is the unit the claim is about -- a step count per
  # server message is always 1, however many the caller heard in one breath.
  turn_edges = [len(buf) / (SAMPLE_RATE * SAMPLE_WIDTH)]
  for reply in REPLIES:
    buf += _silence(args.gap) + _tts(reply)
    turn_edges.append(len(buf) / (SAMPLE_RATE * SAMPLE_WIDTH))
  buf += _silence(args.tail)
  print(f"caller audio: {len(buf) / (SAMPLE_RATE * SAMPLE_WIDTH):.0f}s")

  import cxas_scrapi
  from cxas_scrapi.core import sessions as sess_mod

  sess_mod._BIDI_RUN_TIMEOUT_S = args.timeout  # noqa: SLF001
  sessions = cxas_scrapi.Sessions(app_name=args.app)
  session_id = sessions.create_session_id()
  print(f"session {session_id}")

  start = time.time()
  timeline: list[tuple[float, str]] = []
  original = sess_mod.BidiSessionHandler._on_message  # noqa: SLF001

  def timed(self, ws, message):
    before = len(self.outputs)
    original(self, ws, message)
    for out in self.outputs[before:]:
      for attr in ("text", "transcript", "output_text"):
        val = getattr(out, attr, None)
        if val:
          timeline.append((time.time() - start, str(val).strip()))
          break

  sess_mod.BidiSessionHandler._on_message = timed  # noqa: SLF001
  try:
    sessions.run(session_id=session_id, audio=buf, modality=sess_mod.Modality.AUDIO)
  finally:
    sess_mod.BidiSessionHandler._on_message = original  # noqa: SLF001

  print("\n--- what the caller heard ---")
  for at, text in timeline:
    print(f"  t={at:6.1f}  {text}")

  # Group by TURN: everything spoken after the caller stopped talking and before they
  # speak again. CES delivers each line as its own message, so a per-message count reads
  # three steps in one breath as three turns of one step -- which is the defect, scored
  # as a pass.
  # Counted on FIRST appearance only. A step is handed out once; after that the model
  # refers back to it -- "did you manage to switch the TV box over?" carries the same
  # cue as the step itself, and counting that as a second step marks a perfectly paced
  # call as a failure. This is the same trap as grading a reworded ask ladder by wording.
  bounds = turn_edges + [1e9]
  worst, per_turn, delivered = 0, [], set()
  for i in range(len(turn_edges)):
    spoken = " ".join(t.lower() for at, t in timeline
                      if bounds[i] <= at < bounds[i + 1])
    fresh = {cue for cue in STEP_CUES if cue in spoken} - delivered
    delivered |= fresh
    per_turn.append(len(fresh))
    worst = max(worst, len(fresh))

  said = " ".join(t.lower() for _, t in timeline)
  seen = [c for c in STEP_CUES if c in said]
  handed_off = HANDOFF in said

  print("\nverdict:")
  print(f"  steps per caller turn         : {per_turn}")
  print(f"  distinct steps heard          : {len(seen)}/3 {seen}")
  print(f"  most steps in a single turn   : {worst}  (want 1)")
  print(f"  reached the engineer hand-off : {handed_off}")

  if args.control:
    ok = worst > 1
    print(f"\n{'PASS' if ok else 'FAIL'} (control): the ladder is EXPECTED to run "
          "ahead of the caller here")
    return 0 if ok else 1
  ok = worst == 1 and len(seen) == 3
  print(f"\n{'PASS' if ok else 'FAIL'}: one step per turn, and the caller was asked "
        "between every one")
  return 0 if ok else 1


if __name__ == "__main__":
  raise SystemExit(main(sys.argv[1:]))
