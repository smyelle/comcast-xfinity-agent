#!/usr/bin/env python3
"""Drive a DEMO build the way a stranger opening the CES console does: cold.

Cold means what it says, and it is the whole point of this script. No seeded variables,
no `use_tool_fakes`, nothing on the session that a person clicking "start a chat" would
not have. Every other driver here seeds a CUJ, which is exactly why none of them could
ever have caught the thing a demo build exists to fix: a tool fake is a SESSION setting,
so a console caller never fires one and reaches the live Comcast backends instead.

It also prints the engine's own log for the call, because a transcript cannot tell a
verdict the remote specialists produced from one a short circuit produced. `remote_poll`
and `remote_landed` are what say the job really went out to the service and came back.

    APP_ID=<uuid> python tests/demo_drive.py
    APP_ID=<uuid> python tests/demo_drive.py --say "my internet is not working" --say ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

PROJECT = "ces-deployment-dev"
LOCATION = "us"
NOISY = ("RDK", "TOKEN", "SECRET", "CLIENTID", "X_AUTH", "Bearer", "eyJ")

# The all-clear journey into the Wi-Fi walkthrough: one call that exercises the gate, the
# remote specialists, the verdict ladder and the tips ladder.
SCRIPT = [
    "my internet is not working",
    "8069100230359946",
    "yes please",
    "just one device",
    "that didn't work",
    "still nothing",
    "no change",
]

REMOTE_TAGS = ("remote_started", "remote_poll", "remote_landed", "remote_await_say",
               "async_await_line", "async_await_resolved", "task_completed",
               "task_retry_refire", "no_input_silent_tick")


def _safe(text: str) -> str:
  """Never print a credential; `cxas` traces carry live ones."""
  return "<redacted>" if any(n in text for n in NOISY) else text


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--app", default=os.environ.get("APP_ID", ""))
  ap.add_argument("--say", action="append", default=None,
                  help="Override the scripted utterances (repeatable, in order).")
  ap.add_argument("--session", default=f"demo-{int(time.time())}")
  ap.add_argument("--pause", type=float, default=0.0, metavar="SECONDS",
                  help="Think time before each utterance after the first.")
  ap.add_argument("--all-log", action="store_true",
                  help="Every framework log line, not just the wait/remote ones.")
  a = ap.parse_args()
  if not a.app:
    ap.error("--app (or APP_ID) is required")
  app = a.app if a.app.startswith("projects/") else (
      f"projects/{PROJECT}/locations/{LOCATION}/apps/{a.app}")

  sys.path.insert(0, os.path.join(labs_paths.labs_root(), "service"))
  from app.products.slot_studio.studio.chat_session import ChatSession  # noqa: PLC0415

  # No `initial_variable_state`, and no fakes wrapper: this is the cold path.
  session = ChatSession(app_name=app, channel="text")
  print(f"app {app.split('/')[-1]}  session {a.session}  COLD "
        f"(no seeded variables, no tool fakes)\n")

  # A typed script answers a Wi-Fi tip in two seconds; the caller it stands in for has to
  # go and move a gateway first, which takes minutes. That gap is not cosmetic here: with
  # `--sweep-delay` set, whether the sweep reports DURING the walkthrough or after it has
  # run out of tips is decided by how long the caller takes, so a driver that cannot wait
  # can only ever reproduce one of the two.
  t0 = time.time()
  for index, utterance in enumerate(a.say or SCRIPT):
    if index and a.pause:
      time.sleep(a.pause)
    turn = time.time()
    try:
      rec = session.send(utterance)
      said = (rec.agent_text or "(silent)").strip()
    except Exception as exc:  # noqa: BLE001
      said = f"ERROR {type(exc).__name__}: {str(exc)[:200]}"
    print(f"t={time.time() - t0:6.1f}s (+{time.time() - turn:4.1f}s) > {utterance}")
    print(f"                    < {said}\n")

  print("--- engine log ---")
  try:
    norm = session.get_normalized_trace()
  except Exception as exc:  # noqa: BLE001
    # A long call's normalized trace can exceed the 4 MB gRPC ceiling. Not fatal: the
    # transcript above is the demo, and the proxy's own request log is the other
    # witness that the job really went out.
    print(f"  trace unavailable ({type(exc).__name__}: {str(exc)[:120]})")
    return 0
  seen = {}
  for entry in (norm or {}).get("entries") or []:
    variables = entry.get("variables") or {}
    if isinstance(variables.get("sm"), dict):
      seen = variables["sm"]
  lines = seen.get("_log") or []
  if not lines:
    print("  no slot machine in the trace")
    return 1
  for line in lines:
    rec = line if isinstance(line, dict) else json.loads(line)
    tag = rec.get("tag")
    if not a.all_log and tag not in REMOTE_TAGS:
      continue
    print(f"  {tag:22s} {_safe(json.dumps(rec.get('data') or {}))[:200]}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
