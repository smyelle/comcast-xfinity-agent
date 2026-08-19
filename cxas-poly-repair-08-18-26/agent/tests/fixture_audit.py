#!/usr/bin/env python3
"""MEASUREMENT-VALIDITY probe: which diagnostic legs honour `mock_config_string`?

Drives a `--demo` build and prints, per turn, the status slots each leg writes.
Two arms:

  cold   -- no seeded variables (the account number picks the scenario)
  seeded -- `mock_config_string` on turn 0, with values a live backend cannot
            plausibly return (outage_status=active, convoy_status=predictive_swap)

Not committed; scratch instrument for the fixture audit.

    APP_ID=<uuid> python tests/fixture_audit.py --account 8069100230359946 \
        --vars 'outage_status=active&convoy_status=predictive_swap&...'
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

WATCH = [
    "mock_config_string", "account_status", "has_mac", "cable_modem_mac",
    "leg_outage_res", "outage_detected", "outage_status",
    "leg_convoy_res", "convoy_status", "convoy_customer_message",
    "network_status", "gateway_status", "technician_type", "wifi_status",
    "SweepLegs_done", "diagnostics_complete",
]


def _safe(text: str) -> str:
  return "<redacted>" if any(n in str(text) for n in NOISY) else str(text)


def _flat(state: dict) -> dict:
  out = {}
  for k in WATCH:
    if k in state:
      out[k] = state[k]
  sm = state.get("sm")
  if isinstance(sm, dict):
    filled = sm.get("filled")
    if isinstance(filled, dict):
      for k in WATCH:
        if k in filled and k not in out:
          out[k] = filled[k]
  return out


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--app", default=os.environ.get("APP_ID", ""))
  ap.add_argument("--account", default="8069100230359946")
  ap.add_argument("--vars", default="", help="mock_config_string to seed (blank = cold)")
  ap.add_argument("--fillers", type=int, default=6)
  ap.add_argument("--filler", default="are you still there")
  ap.add_argument("--label", default="run")
  a = ap.parse_args()
  if not a.app:
    ap.error("--app (or APP_ID) is required")
  app = a.app if a.app.startswith("projects/") else (
      f"projects/{PROJECT}/locations/{LOCATION}/apps/{a.app}")

  sys.path.insert(0, os.path.join(labs_paths.labs_root(), "service"))
  from app.products.slot_studio.studio.chat_session import ChatSession  # noqa: PLC0415

  seed = {"mock_config_string": a.vars} if a.vars else None
  session = ChatSession(app_name=app, channel="text", initial_variable_state=seed)
  print(f"### {a.label}  app {app.split('/')[-1]}  account {a.account}")
  print(f"    session {session.session_id}")
  print(f"    seeded: {a.vars or '(cold)'}\n")

  script = ["my internet is not working", a.account] + [a.filler] * a.fillers
  t0 = time.time()
  for i, utterance in enumerate(script):
    try:
      rec = session.send(utterance)
      said = (rec.agent_text or "(silent)").strip()
    except Exception as exc:  # noqa: BLE001
      said = f"ERROR {type(exc).__name__}: {str(exc)[:160]}"
    print(f"[{i}] t={time.time() - t0:5.1f}s > {utterance}")
    print(f"          < {_safe(said)[:400]}")
    for call in (getattr(rec, "tool_calls", None) or []):
      print(f"          call  {_safe(json.dumps(call, default=str))[:300]}")
    for resp in (getattr(rec, "tool_responses", None) or []):
      print(f"          resp  {_safe(json.dumps(resp, default=str))[:700]}")
    snap = _flat(session._variable_state)  # noqa: SLF001
    if snap:
      print(f"          state {json.dumps(snap, default=str)[:800]}")
    print()
  print("--- all session variables ---")
  st = session._variable_state  # noqa: SLF001
  for k in sorted(st):
    print(f"  {k} = {_safe(json.dumps(st[k], default=str))[:400]}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
