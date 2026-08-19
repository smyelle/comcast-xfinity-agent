#!/usr/bin/env python3
"""Why did the caller hear "I'm having trouble with that"?

That sentence is the CES CRASH RENDER, not anything the agent chose to say: a callback
raised, the turn produced no content, and the platform substituted its own apology. The
framework catches its own crashes rather than propagating them, so the cause is not on
the wire — it is in `sm._log`, which only the conversation trace carries.

Prints every WARN/ERROR the framework logged, per turn, alongside what was spoken.

    APP_ID=<uuid> python tests/crash_dump.py [scenario]

Drives with TOOL FAKES unless --real. `ChatSession.send` does not expose `use_tool_fakes`,
which rides on `Sessions.run`, so nothing here is mocked no matter which scenario you
name -- the CUJ's variables are seeded but its FIXTURES are not in play. That cost me
several hours: a "decisive" test of whether a tool fake was honoured was run through this
script, so the fake was never asked for, and the conclusion drawn from it was wrong.
Use `tests/try_it.py` (or `diag_check.py`) when the fixtures matter; use this one when you
want the framework's own log off a live call.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

PROJECT = "ces-deployment-dev"
LOCATION = "us"
NOISY = ("RDK", "TOKEN", "SECRET", "CLIENTID", "X_AUTH", "Bearer", "eyJ")


def _safe(text: str) -> str:
  """Never print a credential. `cxas` traces carry live ones."""
  return "<redacted>" if any(n in text for n in NOISY) else text


def main() -> int:
  app = f"projects/{PROJECT}/locations/{LOCATION}/apps/{os.environ['APP_ID']}"
  sys.path.insert(0, os.path.join(labs_paths.labs_root(), "service"))
  from app.products.slot_studio.studio.chat_session import ChatSession  # noqa: PLC0415

  import flows  # noqa: PLC0415
  # Without the CUJ's variables the flow never gets past "what is your account number?"
  # and the group under test is never dispatched — a clean run that proves nothing.
  import sys as _s
  _cuj = _s.argv[1] if len(_s.argv) > 1 else "all_clear"
  seed = dict(flows.load_cujs(start=HERE)[_cuj].variables)
  print(f"scenario {_cuj}")
  session = ChatSession(app_name=app, channel="text", initial_variable_state=seed)
  # ChatSession has no `use_tool_fakes` parameter -- it rides on `Sessions.run` -- so wrap
  # the private call the way diag_check does. Without this the script drives the LIVE
  # backends while naming a scenario, which is exactly how a "decisive" test of tool fakes
  # ended up measuring nothing this week.
  if "--real" not in _s.argv:
    _run = session._sessions.run
    session._sessions.run = lambda **kw: _run(**dict(kw, use_tool_fakes=True))
  for i, utt in enumerate(["my internet is not working", "ok", "ok", "ok"]):
    try:
      rec = session.send(utt)
      print(f"t{i}  say={(rec.agent_text or '(silent)')[:120]}")
    except Exception as exc:  # noqa: BLE001
      print(f"t{i}  ERROR {type(exc).__name__}: {str(exc)[:160]}")

  print("\n--- framework log ---")
  norm = session.get_normalized_trace()
  sm = {}
  for entry in (norm or {}).get("entries") or []:
    variables = entry.get("variables") or {}
    if isinstance(variables.get("sm"), dict):
      sm = variables["sm"]
  if not sm:
    print("no slot machine in the trace")
    return 1
  for line in sm.get("_log") or []:
    rec = line if isinstance(line, dict) else json.loads(line)
    if True:
      print(f"  [{rec.get('level')}] {rec.get('src')}.{rec.get('tag')}: "
            f"{_safe(json.dumps(rec.get('data') or {}))[:400]}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
