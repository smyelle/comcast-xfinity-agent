#!/usr/bin/env python3
"""Did the deferred sweep fire once, run and report — or re-fire to the loop cap?

The question is narrow and was previously read wrong. `tool_calls` records a DEFERRED
launch AT LAUNCH TIME (ces-probes 146, 147, 149), so one entry per turn is a healthy
dispatch and nine is the re-fire loop. A turn that hits the ten-pass cap returns a
platform 400 with no response object at all, so every channel reads zero for a turn that
actually dispatched nine times — which is why the count has to be read per turn alongside
the error, never on its own.

Driven the way `diag_check.py` drives: the full app resource name, tool fakes on, the CUJ
preset's variables seeded on turn 0.

    APP_ID=<uuid> python tests/sweep_probe.py [--cuj all_clear]
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

import flows  # noqa: E402
from cxas_scrapi.core.sessions import Modality, Sessions  # noqa: E402

PROJECT = "ces-deployment-dev"
LOCATION = "us"
SWEEP = "run_comcast_diagnostics_resolved"
GUARD = "settle_guard"
CUJS = flows.load_cujs(start=HERE)


def _names(resp) -> list[str]:
  from cxas_scrapi.core.response_parser import ParsedSessionResponse  # noqa: PLC0415
  return [tc.name for tc in (ParsedSessionResponse(resp).tool_calls or [])]


def _text(resp) -> str:
  from cxas_scrapi.core.response_parser import ParsedSessionResponse  # noqa: PLC0415
  return ParsedSessionResponse(resp).consolidated_agent_text or ""


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--app", default=os.environ.get("APP_ID", ""))
  ap.add_argument("--cuj", default="all_clear")
  ap.add_argument("--turns", type=int, default=5)
  # Tool fakes make the sweep's nested fan-out INSTANT, which is the one thing this
  # probe most needs to be slow: ces-probes 147 measured a deferred body's nested call
  # aborted at 1.0s against a 0.5s guard, and the real fan-out is 4.2s+. A faked run
  # therefore proves the re-fire is gone and says nothing about whether the body
  # survives, so the real backend is a separate, explicit arm.
  ap.add_argument("--real", action="store_true",
                  help="hit the real backends instead of the tool fakes")
  args = ap.parse_args()

  app = f"projects/{PROJECT}/locations/{LOCATION}/apps/{args.app}"
  seed = dict(CUJS[args.cuj].variables)
  sess = Sessions(app_name=app)
  session_id = f"sweepprobe-{os.getpid()}"

  utterances = ["my internet is not working"] + ["ok"] * (args.turns - 1)
  total_sweeps = 0
  for i, utt in enumerate(utterances):
    try:
      resp = sess.run(session_id, text=utt, modality=Modality.TEXT,
                      variables=seed if i == 0 else None,
                      use_tool_fakes=not args.real)
    except Exception as exc:  # noqa: BLE001 - a platform error IS the observation
      print(f"t{i}  ERROR {type(exc).__name__}: {str(exc)[:200]}")
      continue
    names = _names(resp)
    sweeps = names.count(SWEEP)
    total_sweeps += sweeps
    verdicts = [n for n in names if n.startswith("verdict_")]
    print(f"t{i}  sweep={sweeps}  guard={names.count(GUARD)}  "
          f"verdict={','.join(verdicts) or '-'}")
    print(f"     tools={names or '-'}")
    print(f"     say={_text(resp)[:160] or '(silent)'}")

  print(f"\nTOTAL sweep dispatches across {args.turns} turns: {total_sweeps}")
  print("  1-2 = healthy (dispatch, maybe one re-ask)."
        "  >=8 on one turn = the re-fire loop.  0 everywhere = never dispatched.")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
