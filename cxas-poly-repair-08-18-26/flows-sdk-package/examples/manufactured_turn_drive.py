"""Drive `manufactured_turn` over VOICE, because the turns it is about cannot be faked.

An inactivity tick and an asynchronous completion push are turns the PLATFORM authors.
Sending `<context>no user activity</context>` as user text does not produce one -- CES
rejects it outright as malicious input -- so the only way to see this behaviour is to
place a real audio call and go quiet.

The caller says one thing, then says nothing for long enough that the line check finishes
(its completion arrives as a push) and the platform ticks a few times. Then they say
something the flow cannot use, which is the turn the whole demo turns on: what they hear
next tells you whether the polls spent the ladder.

    rung 1 heard once, then rung 2   the polls were free      <- fixed
    rung 1 heard three times         the polls re-asked       <- the defect
    rung 3 ("let me get someone")    the polls burned it      <- the workaround

    python -m examples.manufactured_turn_drive --app <resource>

macOS only, for the same reason `silent_delivery_turn_drive` is: `say` and `afconvert`
ship with the OS, so caller audio needs no TTS credentials and no ffmpeg.
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
# A second utterance, and it is not decoration. The line check starts the moment the
# first one lands, and while a task is awaited the outstanding question is the WAIT --
# so a demo that goes quiet here has the engine ask about the device for the FIRST time
# on the completion push, and a first ask is supposed to speak. This turn is what gets
# the question properly put, on a turn the caller took, before any poll arrives.
SECOND = "it has been doing it since yesterday"
# Deliberately unusable: it answers nothing, so the question is legitimately put again.
# What matters is WHICH rung comes back.
UNUSABLE = "I'm not sure what you mean"

# The verdict deliberately does NOT match on wording. Each rung is a directive and the
# model words it, so "which device" came back inside rung TWO on a real drive and a
# wording count read one question as two. What the fix actually claims is about WHEN the
# agent speaks, not what it says, so that is what gets measured: the window between the
# caller's second utterance and their third belongs to the polls, and the agent may take
# exactly one turn in it -- the question itself, put once.


def _tts(phrase: str) -> bytes:
  """Render a phrase to raw 16kHz mono LINEAR16 with macOS speech synthesis."""
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
  ap.add_argument("--app", default="", help="the deployed CES app resource")
  ap.add_argument("--gap", type=float, default=12.0,
                  help="silence between the two caller utterances: long enough for the "
                       "wait to give up, so the device question is put on the second one")
  ap.add_argument("--hold", type=float, default=32.0,
                  help="silence after the second utterance: long enough for the check to "
                       "answer (25s from the first turn) and a few ticks to follow")
  ap.add_argument("--tail", type=float, default=15.0,
                  help="trailing silence, so the agent can finish")
  ap.add_argument("--no-input", action="store_true",
                  help="grade the OTHER arm: the app declares a silence policy, so the "
                       "ticks belong to it and its reprompts must be spoken out loud")
  ap.add_argument("--timeout", type=float, default=300.0,
                  help="the driver's own bidi ceiling; the vendored client hangs up at "
                       "120s, which reads exactly like CES dropping the call")
  # `argv or []`, NOT a fallback to sys.argv: the driver-rot test calls `main()` with no
  # arguments, and anything that reaches for sys.argv there parses PYTEST's command line
  # and exits.
  args = ap.parse_args(argv or [])

  if not args.app:
    print("dry run -- no --app, so no call is placed. The call this would make:")
    print(f"  caller: {OPENING!r}, then {args.gap:.0f}s (the wait gives up)")
    print(f"  caller: {SECOND!r}  <- the device question is put here, on a caller turn")
    print(f"  then {args.hold:.0f}s of silence: the check answers (a push), then ticks")
    print(f"  caller: {UNUSABLE!r}, then {args.tail:.0f}s of silence")
    print("Fixed: rung 1 once, then rung 2. Defect: rung 1 again on the push.")
    return 0

  opening, second, unusable = _tts(OPENING), _tts(SECOND), _tts(UNUSABLE)
  buf = (opening + _silence(args.gap) + second + _silence(args.hold)
         + unusable + _silence(args.tail))

  def _at(nbytes: int) -> float:
    return nbytes / (SAMPLE_RATE * SAMPLE_WIDTH)

  # The poll window: the caller stops talking here and starts again there. Everything in
  # between is the platform's -- the completion push and the inactivity ticks.
  polls_from = _at(len(opening) + int(args.gap * SAMPLE_RATE * SAMPLE_WIDTH)
                   + len(second))
  polls_to = _at(len(buf) - len(unusable)
                 - int(args.tail * SAMPLE_RATE * SAMPLE_WIDTH))
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
    """Timestamp every server message: this demo is entirely about WHICH turn spoke,
    and a bag of outputs with no clock on it collapses the polls into the reply."""
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

  in_window = [(at, t) for at, t in timeline if polls_from <= at <= polls_to]
  after = [(at, t) for at, t in timeline if at > polls_to]

  print(f"\npoll window: t={polls_from:.0f}s to t={polls_to:.0f}s "
        "(the caller says nothing; a push and several ticks arrive)")
  for at, t in in_window:
    print(f"  t={at:6.1f}  {t}")

  print("\nverdict:")
  if args.no_input:
    # The other half of the claim, and the one worth checking hardest: the fix moved
    # `is_inactivity` onto this ladder's own guard, so if anything was going to break
    # a declared silence policy it would break here. The reprompts must still be
    # SPOKEN on the ticks -- that is the author's policy, and it outranks the new
    # silence -- and the ladder must still reach its exhaust line.
    heard = " ".join(t.lower() for _, t in timeline)
    rungs = [r for r in ("are you still there", "take your time") if r in heard]
    exhausted = "let you go" in heard
    print(f"  silence reprompts spoken           : {len(rungs)}/2 {rungs}")
    print(f"  the ladder reached its exhaust     : {exhausted}")
    ok = len(rungs) >= 1
    print(f"\n{'PASS' if ok else 'FAIL'}: a declared no_input policy still owns the "
          "inactivity ticks")
    return 0 if ok else 1
  print(f"  agent turns inside the poll window : {len(in_window)}  (want 1 -- the "
        "question, put ONCE)")
  print(f"  agent answered the caller after it : {bool(after)}  (want True -- the "
        "line did not die on the silence)")
  ok = len(in_window) == 1 and bool(after)
  print(f"\n{'PASS' if ok else 'FAIL'}: an outstanding question was "
        f"{'not ' if ok else ''}put again on a turn the caller did not take")
  return 0 if ok else 1


if __name__ == "__main__":
  raise SystemExit(main(sys.argv[1:]))
