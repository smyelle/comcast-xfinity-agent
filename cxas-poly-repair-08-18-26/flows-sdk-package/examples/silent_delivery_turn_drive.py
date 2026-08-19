"""Drive `examples/silent_delivery_turn` over REAL AUDIO, with a caller who goes quiet.

The claim this example makes is about a turn the caller did not take, and a text channel
has no such turn: every request over text carries an utterance. The turns that matter
here — an inactivity tick, and an async completion delivered onto silence — are
manufactured by the PLATFORM when nobody is speaking, so they only exist on a call.

So this is the same app, called over the voice channel by a caller who says three things
and then holds the line. The diagnostic takes thirty seconds; the hold is longer; the
completion therefore lands on a turn with no utterance on it, which is the turn under
test.

    python -m examples.silent_delivery_turn_drive --app projects/<p>/locations/us/apps/<id>
    python -m examples.silent_delivery_turn_drive --app <resource> --hold 45 --then "yes please"

`--hold` is the length of the silence after the caller describes the fault, and it is a
knob rather than a constant on purpose: one pass at one timing is not a result. The
completion lands on whichever tick happens to follow it, and a demo that only ever ran at
one hold length would not notice a guard that held for the first tick and let go on the
third.

`--then` speaks at the end of the hold instead of leaving it silent, which is the OTHER
half of the claim: a caller who does answer on the delivery turn must still be captured.
A fix that simply refused the scan on every quiet turn would pass the silent arm and fail
this one.

Caller speech comes from macOS `say` piped through `afconvert`, so no TTS credentials are
needed. Ported from the probe drivers in `ces-probes`.
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

# What the caller says, in order. The LAST line is the one this whole example turns on:
# it is a description of a broken heater, it contains "not", and "not" is inside the
# offer slot's plainly authored decline cue. When the caller then goes quiet it stays the
# newest real utterance in the history for the rest of the call.
OPENING = "hello"
SERIAL = "the serial is 4417"
FAULT = "it is not heating at all and the display is not lighting up"


def _tts(phrase: str) -> bytes:
  """Render a phrase to raw 16kHz mono LINEAR16 with macOS speech synthesis.

  macOS-only on purpose: `say` and `afconvert` ship with the OS, so producing caller
  audio needs no TTS credentials, no pip install and no ffmpeg.
  """
  if platform.system() != "Darwin":
    raise RuntimeError(
        "this driver needs macOS `say` and `afconvert` to synthesize caller audio; this"
        f" is {platform.system()}. Port _tts to espeak + sox/ffmpeg if you need Linux.")
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
  ap.add_argument("--app", default="",
                  help="the deployed CES app resource. Omitted, the driver prints the "
                       "call it WOULD place and stops — which is what keeps it "
                       "exercisable by a test on a machine with no app, no credentials "
                       "and no speech synthesizer")
  ap.add_argument("--gap", type=float, default=7.0,
                  help="silence between the caller's opening utterances, long enough "
                       "for the agent to answer and the platform to endpoint")
  ap.add_argument("--hold", type=float, default=50.0,
                  help="silence after the fault description — the window the completion "
                       "has to land in. Vary it; one timing is not a result")
  ap.add_argument("--then", dest="then", default="",
                  help="speak this at the end of the hold instead of staying silent")
  ap.add_argument("--tail", type=float, default=25.0,
                  help="trailing silence, so the agent can keep talking")
  ap.add_argument("--timeout", type=float, default=400.0,
                  help="the driver's own bidi ceiling. The vendored client defaults to "
                       "120s and hangs UP at it, which reads exactly like the platform "
                       "dropping the call")
  # `argv or []` rather than letting argparse read `sys.argv`: this is called with no
  # arguments by the driver-rot test, and there it would otherwise parse PYTEST's
  # command line and exit.
  args = ap.parse_args(argv or [])

  if not args.app:
    print("dry run — no --app, so no call is placed. The call this would make:")
    for line, after in ((OPENING, args.gap), (SERIAL, args.gap), (FAULT, args.hold)):
      print(f"  caller: {line!r}, then {after:.0f}s of silence")
    print(f"  caller: {args.then!r}" if args.then
          else "  caller: nothing more at all")
    print(f"  then {args.tail:.0f}s of silence while the agent keeps talking.")
    print("The diagnostic sleeps 30s, so the completion lands on one of the inactivity"
          " ticks inside that hold — which is the turn under test.")
    return 0

  buf = (_tts(OPENING) + _silence(args.gap)
         + _tts(SERIAL) + _silence(args.gap)
         + _tts(FAULT) + _silence(args.hold))
  if args.then:
    buf += _tts(args.then)
  buf += _silence(args.tail)
  secs = len(buf) / (SAMPLE_RATE * SAMPLE_WIDTH)
  print(f"caller audio: {secs:.0f}s  gap={args.gap}s hold={args.hold}s "
        f"then={args.then or '(silence)'}")

  import cxas_scrapi
  from cxas_scrapi.core import sessions as sess_mod

  # The vendored client force-closes a bidi run at 120 seconds and logs it as the
  # session exceeding a limit. That is the DRIVER hanging up, not CES, and a hold long
  # enough to be interesting runs straight into it.
  sess_mod._BIDI_RUN_TIMEOUT_S = args.timeout  # noqa: SLF001

  sessions = cxas_scrapi.Sessions(app_name=args.app)
  session_id = sessions.create_session_id()
  print(f"session {session_id}")

  start = time.time()
  timeline: list[tuple[float, str]] = []
  original = sess_mod.BidiSessionHandler._on_message  # noqa: SLF001

  def timed(self, ws, message):
    """Timestamp every server message as it lands.

    The returned response is a bag of outputs with no clock on it, and this demo is
    entirely about WHICH TURN a line was spoken on. Without the timestamps the silent
    turns and the delivery turn collapse into one undifferentiated reply.
    """
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

  said = " ".join(t for _, t in timeline).lower()
  # The verdict, spelled out rather than left to the reader, because the two outcomes
  # differ by one sentence and both sound like a working agent read quickly.
  hung_up = "leave the firmware as it is" in said
  offered = "firmware update" in said and not hung_up
  applied = "is updating now" in said
  print("\nverdict:")
  print(f"  refused on the caller's behalf and ended the call : {hung_up}")
  print(f"  still asking (the question survived the silence)  : {offered}")
  print(f"  answered on the delivery turn and applied the fix : {applied}")
  return 1 if hung_up else 0


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
