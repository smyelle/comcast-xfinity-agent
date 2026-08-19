#!/usr/bin/env python3
"""Drive the DEMO build end to end over real audio, with the caller silent for the sweep.

Why this exists rather than another text driver. The one thing a text drive cannot show
is the part of this agent that only happens when nobody is speaking: the platform emits
an inactivity tick, the engine polls the remote specialists on it, and the caller hears a
reassurance line instead of dead air. In text there are no ticks, so the whole wait is
invisible and the verdict simply arrives on the next thing the caller types.

Two mechanisms, because one buffer cannot express both halves of the journey:

  * the OPENING is one continuous audio buffer — the complaint, a pause, the account
    number, then a long silence. The silence is the point: it is real dead air on an open
    stream, so the platform's own inactivity timeout produces the ticks.
  * the WALKTHROUGH turns after it are ordinary audio turns on the SAME session, each
    synthesized by the platform's own TTS. By then the caller is answering questions, so
    there is nothing to hold still for.

    APP_ID=<uuid> python tests/demo_voice.py
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

from cxas_scrapi.core.sessions import Modality, Sessions  # noqa: E402

PROJECT = "ces-deployment-dev"
LOCATION = "us"
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2

# Said as digits, because a sixteen-digit number read as one word transcribes badly.
#
# Which account matters more than it looks, and on a DEMO build it is the only thing a
# cold caller can vary: the number picks the journey, because the demo gate resolves it
# against `cujs.yaml`'s bindings (`source_tools._demo_account_scenarios`). This one is
# `all_clear` -- the journey with a wait in it, and therefore the one this script exists
# to show. `flows cujs` lists the rest; give any of their accounts to `--account`.
#
# CORRECTION, and it is why the help below used to be wrong. This file claimed the
# default "is NOT clear on the real backends" and pointed at 8069100020078787 as the one
# that "clears the gate and sweeps". Backwards on both counts: on a DEMO build the gate
# reaches no real backend at all, and 8069100020078787 is the `account_suspended`
# binding -- the one account that by design can NEVER sweep, because a restricted account
# is handed to billing before a single check runs. Recommending it was recommending the
# one number that cannot show what this script measures.
ACCOUNT = "8 0 6 9 1 0 0 2 3 0 3 5 9 9 4 6"
WALKTHROUGH = ["yes please", "just one device", "that didn't work", "still nothing",
               "no change"]


def _digits(account: str) -> str:
  """Spaced digits, so the platform's ASR does not hear one very large number."""
  return " ".join(account.replace(" ", ""))


def _tts(phrase: str) -> bytes:
  """Caller audio from macOS `say`, so no TTS credentials are needed to be a caller."""
  if platform.system() != "Darwin":
    raise RuntimeError("needs macOS `say` + `afconvert` to synthesize caller audio")
  with tempfile.TemporaryDirectory() as tmp:
    aiff, wav = os.path.join(tmp, "s.aiff"), os.path.join(tmp, "s.wav")
    subprocess.run(["say", "-o", aiff, phrase], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav],
                   check=True, capture_output=True)
    with wave.open(wav, "rb") as fh:
      return fh.readframes(fh.getnframes())


def _silence(seconds: float) -> bytes:
  return b"\x00" * int(seconds * SAMPLE_RATE * SAMPLE_WIDTH)


def _spoken(resp) -> list[tuple[int, str]]:
  """(turn index, line) for everything the agent said, GROUPED BY TURN.

  The turn index is what makes this worth having. One audio buffer with thirty seconds of
  silence in it is many turns -- the complaint, the account number, then an inactivity
  tick for each stretch of quiet -- and they all come back on one response. Flattened,
  every line prints against the moment the call RETURNED, which reads as the agent saying
  five things at once and hides the only thing worth knowing: which turn each line landed
  on. That is exactly what a question asked during the wait has to be judged by.
  """
  out = []
  for i, output in enumerate(resp.outputs):
    di = getattr(output, "diagnostic_info", None)
    if not (di and getattr(di, "messages", None)):
      continue
    for message in di.messages:
      if getattr(message, "role", "") == "user":
        continue
      for chunk in getattr(message, "chunks", []):
        kind = chunk._pb.WhichOneof("data") if hasattr(chunk, "_pb") else None
        if kind in ("text", "transcript"):
          text = getattr(chunk, kind)
          if text and text.strip():
            # The message's own eventTime, which is the only honest way to see the GAPS
            # between lines that share a turn. Read times are all identical -- the whole
            # response arrives at once -- so a printed transcript makes three lines
            # spoken seconds apart look simultaneous, and hides exactly the dead air a
            # caller complains about.
            ts = getattr(message, "event_time", None)
            out.append((i, text.strip(), ts.timestamp() if ts else None))
  return out


def _engine(resp) -> list[tuple[int, str]]:
  """(turn index, engine log line) out of the session state the response carries.

  A transcript says what the caller heard; it does not say why. When a turn is filled by
  the MODEL rather than by a rung the two are indistinguishable from outside -- both are
  just the agent talking -- and that is the failure this agent is most prone to. The
  engine writes its decisions into `sm["_log"]`, and `sm` rides back on the response as an
  `updatedVariables` chunk, so the reasoning is already in hand and only needs unpacking.
  Beats Cloud Logging for this: same data, no query, correctly attributed to its turn.
  """
  out, seen = [], set()
  for i, output in enumerate(resp.outputs):
    di = getattr(output, "diagnostic_info", None)
    for message in (getattr(di, "messages", None) or []):
      for chunk in getattr(message, "chunks", []):
        kind = chunk._pb.WhichOneof("data") if hasattr(chunk, "_pb") else None
        if kind != "updated_variables":
          continue
        sm = (dict(getattr(chunk, kind)) or {}).get("sm") or {}
        for entry in (sm.get("_log") or []):
          # proto MapComposite, not a dict -- `isinstance(entry, dict)` is False and the
          # entry renders as its repr, which is how this printed twenty lines of
          # `<MapComposite object at 0x...>` the first time.
          try:
            entry = dict(entry)
          except (TypeError, ValueError):
            continue
          key = str(entry)
          if key in seen:
            continue
          seen.add(key)
          data = entry.get("data")
          try:
            data = dict(data)
          except (TypeError, ValueError):
            pass
          out.append((i, f"{entry.get('tag', '?')} {data if data else ''}"))
  return out


def _print(lines, start: float) -> None:
  """One block per turn, so a line spoken on an inactivity tick is visibly its own turn.

  Each line carries the gap since the previous one, from the platform's own event times.
  A line's position in a transcript says nothing about when the caller heard it.
  """
  last, prev_ts = None, None
  base = next((t for _, _, t in lines if t), None)
  for turn, line, ts in lines:
    if turn != last:
      print(f"  --- turn {turn}  (t={time.time() - start:5.1f}s at read)")
      last = turn
    gap = f"+{ts - prev_ts:5.1f}s" if (ts and prev_ts) else "      "
    at = f"{ts - base:6.1f}s" if (ts and base) else "       "
    print(f"   {at} {gap}  < {line}")
    if ts:
      prev_ts = ts


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--app", default=os.environ.get("APP_ID", ""))
  ap.add_argument("--hold", type=float, default=30.0,
                  help="seconds of silence after the account number — the ticks the "
                       "sweep is polled on, and the reassurance the caller hears")
  ap.add_argument("--gap", type=float, default=11.0,
                  help="silence between the complaint and the account number")
  ap.add_argument("--account", default=ACCOUNT,
                  help="spoken account number. On a DEMO build it picks the journey, via "
                       "cujs.yaml's bindings: the default (8069100230359946) is all_clear "
                       "and is the one that sweeps; 8069100020078787 is account_suspended "
                       "and hands off to billing before any check runs, so it never does")
  ap.add_argument("--complaint", default="my internet is not working")
  ap.add_argument("--say", action="append", default=[],
                  help="replaces the walkthrough turns, in order. Repeatable. Needed to "
                       "drive a journey whose first reply is not 'yes please' -- with the "
                       "scope question asked during the sweep, it is the scope answer")
  # Cold is this script's whole point, so seeding is opt-in and says so loudly when used.
  #
  # SCOPED, 2026-08-13. This used to read "cold is currently unreachable for the journey
  # worth watching -- EVERY test account answers 'I see an issue with your account status'
  # and hands off". True, and true only of the DEFAULT build, whose gate calls the real
  # context hub: driven cold on 2026-08-13 the shipped app still answers exactly that, on
  # every account, because the hub errors in dev. It was never true of a `--demo` build,
  # where the recorded fixture answers instead and a cold caller reaches the whole journey.
  # Do not reach for a seed on a demo build to work around a default build's outage.
  #
  # ⚠️ IT DOES NOT GIVE YOU A WAIT TO WATCH, and I wasted a drive believing it would. The
  # `specialist_proxy` toolset carries no fake, so the remote job is genuinely not faked --
  # but with `context_status=clear` the GATE resolves `network_status` itself, and
  # `Specialists` is gated on that being unfilled. So the sweep short circuits, the verdict
  # lands on the opening turn, and there is no wait for anything to be asked during. Use
  # this to exercise the ladder, not to measure or watch the wait.
  ap.add_argument("--fake-context", action="store_true",
                  help="seed mock_config_string and use_tool_fakes so the account clears "
                       "the gate. NOTE: this also short circuits the sweep, so there is "
                       "no wait -- see the comment above")
  # The narrower seed, for a journey `--fake-context` cannot reach.
  #
  # `--fake-context` clears the account gate by faking the context tool, and takes the
  # WAIT with it: `context_status=clear` resolves `network_status` in the gate, and
  # `Specialists` is gated on that being unfilled, so the remote job never runs. To watch
  # anything that happens DURING the sweep the gate has to clear WITHOUT the sweep being
  # short circuited, which means seeding the gate's own prepopulation keys directly
  # (`_prepopulated` in source_tools reads them off the session) and leaving
  # `network_status` alone. `--var gateway_status=healthy --var cable_modem_mac=...` is
  # the smallest seed that does it: the account reads clear, the MAC is present, and both
  # fan-out legs and the remote job still run for real.
  # Fakes WITHOUT the context seed. `--fake-context` couples the two, and the coupling
  # is what makes the mid-sweep journey unreachable: the fan-out legs reach Comcast
  # backends that dev cannot route to (driven, both legs time out and the group gives
  # up), so they need their fakes -- while the remote job, which is the wait worth
  # watching, carries no fake and stays real either way.
  ap.add_argument("--fakes", action="store_true",
                  help="use_tool_fakes without seeding mock_config_string, so the "
                       "fan-out legs answer and the remote job still runs for real")
  ap.add_argument("--var", action="append", default=[], metavar="K=V",
                  help="seed a session variable (repeatable). Unlike --fake-context this "
                       "fakes nothing, so the sweep still runs")
  ap.add_argument("--engine", action="store_true",
                  help="also print the engine's own decisions, so a turn the MODEL filled "
                       "is distinguishable from one a rung fired")
  ap.add_argument("--mock", default="outage_status=none&convoy_status=clear"
                                    "&network_status=clear&gateway_status=clear"
                                    "&context_status=clear",
                  help="mock_config_string, as k=v&k=v (see cujs.yaml defaults)")
  a = ap.parse_args()
  if not a.app:
    ap.error("--app (or APP_ID) is required")
  app = a.app if a.app.startswith("projects/") else (
      f"projects/{PROJECT}/locations/{LOCATION}/apps/{a.app}")

  sess = Sessions(app_name=app)
  sid = f"demovoice-{int(time.time())}"
  seed = {"accountNumber": a.account.replace(" ", ""), "mock_config_string": a.mock} \
      if a.fake_context else None
  if a.var:
    seed = dict(seed or {})
    seed.update(dict(kv.split("=", 1) for kv in a.var))
  print(f"app {app.split('/')[-1]}  session {sid}  VOICE, "
        + ("SEEDED context fakes (specialists still real)" if a.fake_context
           else f"seeded variables {sorted(seed)}, no tool fakes" if seed
           else "cold (no seeded variables, no tool fakes)") + "\n")
  start = time.time()

  spoken_account = _digits(a.account)
  opening = (_tts(a.complaint) + _silence(a.gap)
             + _tts(f"my account number is {spoken_account}") + _silence(a.hold))
  print(f"[caller] \"{a.complaint}\" … {a.gap:.0f}s … "
        f"\"my account number is {spoken_account}\" … then {a.hold:.0f}s of SILENCE")
  resp = sess.run(sid, audio=opening, modality=Modality.AUDIO,
                  variables=seed, use_tool_fakes=(a.fake_context or a.fakes))
  _print(_spoken(resp), start)
  if a.engine:
    for _t, _line in _engine(resp):
      print(f"        . {_line[:150]}")

  for utterance in (a.say or WALKTHROUGH):
    # `silence:N` is a turn the caller spends saying NOTHING, and the journey worth
    # showing cannot be driven without one. A remote job finishes on its own schedule,
    # and if the only turns available are caller utterances then the verdict lands on
    # one of them and EATS it -- driven, the caller's "yes please" was consumed by the
    # turn that delivered the all-clear, so the offer it was meant to answer had not been
    # made yet and the next utterance answered the wrong question. Real callers go quiet
    # while they wait; the platform's inactivity ticks poll on that silence and the
    # verdict arrives on a tick instead of on a word.
    if utterance.startswith("silence:"):
      seconds = float(utterance.split(":", 1)[1])
      print(f"\n[caller] ... {seconds:g}s of silence ...")
      resp = sess.run(sid, audio=_silence(seconds), modality=Modality.AUDIO,
                      use_tool_fakes=(a.fake_context or a.fakes))
      _print(_spoken(resp), start)
      if a.engine:
        for _t, _line in _engine(resp):
          print(f"        . {_line[:150]}")
      continue
    print(f"\n[caller] {utterance!r}")
    resp = sess.run(sid, text=utterance, modality=Modality.AUDIO,
                    use_tool_fakes=(a.fake_context or a.fakes))
    _print(_spoken(resp), start)
    if a.engine:
      for _t, _line in _engine(resp):
        print(f"        . {_line[:150]}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
