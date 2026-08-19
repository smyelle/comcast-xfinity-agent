#!/usr/bin/env python3
"""Offline proof that the opening greeting is spoken on a direct call and DROPPED on a
hand-off.

The greeting is the "Welcome to Xfinity" the flat repair agent opens on. It used to be the
model improvising ahead of the account ask, which is redundant when a steering agent has
already welcomed the caller and is handing the call over (transferToNga / A2A). So the ask
is `verbatim` -- the model cannot add a greeting of its own -- and the greeting rides a
`{welcome_lead}` the `before_agent` hook fills: with the greeting on a direct call's opening
turn, and with the bare "To get started," once `skip_greeting` is seeded.

This drives the REAL hook and the REAL engine against the EMITTED config, no model and no
network, so it grades the thing a deploy ships. Two calls, one per flag state, plus a drift
guard that the hook's inline lead-ins still match `scripts.WELCOME_LEAD` /
`WELCOME_LEAD_HANDOFF` (the hook carries its own copy, because a module-level reference does
not survive the emission into the deployed callback).

    python tests/greeting_check.py [--app-dir ./built]
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import harness  # noqa: E402
import scripts  # noqa: E402


class Failure(Exception):
  pass


def _opening(config: dict, app_dir: str, skip_greeting: bool) -> str:
  """The agent-first opening turn of a fresh `repair` call, as the caller hears it."""
  call = harness.new_call(config, app_dir=app_dir, flow="repair", account=None)
  if skip_greeting:
    # What an upstream hand-off seeds via the transfer variables. Set BEFORE the opening
    # turn, exactly as it arrives on a real transferToNga.
    call.state["skip_greeting"] = "true"
  result = call.turn("")  # no caller utterance: the agent speaks first
  return " ".join(call._lines(result)).strip()


def run(app_dir: str) -> int:
  config = harness.load_config(app_dir, "repair")
  core = "what's your account number? The phone number on the account works too."
  want_direct = scripts.WELCOME_LEAD + core
  want_handoff = scripts.WELCOME_LEAD_HANDOFF + core

  failures: list[str] = []

  # Drift guard: the hook renders its OWN inline copy of the lead-ins (module refs do not
  # survive emission), so a scripts edit that leaves the hook behind would ship a greeting
  # that no longer matches the approved copy. Both openers below assert the FULL rendered
  # line equals scripts + the core ask, which is what pins the hook to scripts.
  checks = [
      ("direct call keeps the greeting", False, want_direct),
      ("a hand-off drops the greeting", True, want_handoff),
  ]
  for title, skip, want in checks:
    got = _opening(config, app_dir, skip)
    status = "ok  " if got == want else "FAIL"
    print(f"{status} skip_greeting={str(skip):5} {title}")
    print(f"       heard: {got!r}")
    if got != want:
      failures.append(f"{title}: said {got!r}\n       wanted {want!r}")

  # The greeting really is gone, not merely reworded: the brand hello must not survive a
  # hand-off in any form.
  handoff_line = _opening(config, app_dir, skip_greeting=True)
  if "Welcome to Xfinity" in handoff_line:
    failures.append(f"a hand-off still greeted: {handoff_line!r}")

  # The BUILD flag: a `--skip-greeting` build bakes the greeting off, so even a DIRECT call
  # (no runtime seed) never greets. Built for real, so the flag is graded against the
  # artifact it emits rather than the source that emits it -- the same standard config_check
  # holds every other switch to.
  failures += _check_build_flag()

  print()
  for failure in failures:
    print(f"FAIL {failure}")
  print(f"{2 - len([f for f in failures if 'call' in f])}/2 greeting states correct"
        if failures else "2/2 greeting states correct")
  return 1 if failures else 0


def _check_build_flag() -> list:
  """Build with `--skip-greeting` and require the account ask to be baked greeting-free."""
  import subprocess
  import tempfile

  root = os.path.dirname(HERE)
  work = tempfile.mkdtemp(prefix="greeting_check_")
  try:
    out = os.path.join(work, "skip")
    result = subprocess.run(
        [sys.executable, os.path.join(root, "build.py"), "--out", out, "--skip-greeting"],
        cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
      return [f"--skip-greeting: build failed\n{result.stdout[-800:]}{result.stderr[-800:]}"]
    config = harness.load_config(out, "repair")
    ask = next((s.get("ask") for s in config["slots"]
                if s.get("name") == "accountNumber"), "")
    got = _opening(config, out, skip_greeting=False)  # a DIRECT call, no runtime seed
    fails = []
    if "{welcome_lead}" in (ask or ""):
      fails.append(f"--skip-greeting: the ask still carries the runtime placeholder: {ask!r}")
    if "Welcome to Xfinity" in got:
      fails.append(f"--skip-greeting: a direct call still greeted: {got!r}")
    print(f"{'FAIL' if fails else 'ok  '} --skip-greeting build: direct call does not greet")
    print(f"       heard: {got!r}")
    return fails
  finally:
    import shutil
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("--app-dir", default="built")
  args = parser.parse_args()
  raise SystemExit(run(args.app_dir))
