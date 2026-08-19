#!/usr/bin/env python3
"""Talk to the deployed parallel-fan-out Comcast agent, interactively.

Prints what the agent SAYS and, dimmed beneath it, which tools fired that turn — the
fan-out is the point of this build, so seeing `resolve_account_context` gate and then
`SweepLegs_leg_*` go out together is most of what there is to look at.

    python tests/try_it.py                       # mocked "all clear" account
    python tests/try_it.py --cuj area_outage     # a mocked scenario (see cujs.yaml)
    python tests/try_it.py --real                # the real backends, no fixtures
    python tests/try_it.py --list                # the scenarios available

Type your turns; blank line or Ctrl-D to leave. The first turn seeds the scenario, so
open with an actual complaint ("my internet is not working") rather than "hello" —
nothing sweeps until the caller has said what is wrong.
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
APP = os.environ.get("APP_ID", "2e058bf5-e8ff-40c7-a45f-9676919ab68b")
DIM, OFF = "\033[2m", "\033[0m"


def _parsed(resp):
  from cxas_scrapi.core.response_parser import ParsedSessionResponse  # noqa: PLC0415
  return ParsedSessionResponse(resp)


def main() -> int:
  cujs = flows.load_cujs(start=HERE)
  ap = argparse.ArgumentParser()
  ap.add_argument("--cuj", default="all_clear")
  ap.add_argument("--app", default=APP)
  ap.add_argument("--real", action="store_true",
                  help="hit the real backends instead of the recorded fixtures")
  ap.add_argument("--list", action="store_true", help="list the mocked scenarios")
  # The scenario normally seeds accountNumber, so the agent never asks for it. Drop it and
  # the collection turn happens for real -- which is the half of the journey a seeded run
  # skips, and the one where the sweep's filler and the fan-out actually start.
  ap.add_argument("--ask-account", action="store_true",
                  help="do NOT seed the account; make the agent ask for it")
  args = ap.parse_args()

  if args.list:
    # `CUJSet` is not a mapping: it iterates VALUES and exposes `names()`/`get()`.
    for name in sorted(cujs.names()):
      print(f"  {name}")
    return 0
  if args.cuj not in set(cujs.names()):
    print(f"unknown scenario {args.cuj!r}; --list shows them")
    return 2

  seed = dict(cujs[args.cuj].variables)
  if args.ask_account:
    for k in ("accountNumber", "account_id"):
      seed.pop(k, None)
  sess = Sessions(app_name=f"projects/{PROJECT}/locations/{LOCATION}/apps/{args.app}")
  session_id = f"tryit-{os.getpid()}"
  print(f"app      {args.app}")
  print(f"scenario {args.cuj}{'  (REAL backends — fixtures ignored)' if args.real else ''}")
  print("try:     my internet is not working\n")

  turn = 0
  while True:
    try:
      text = input("you > ").strip()
    except EOFError:
      print()
      return 0
    if not text:
      return 0
    try:
      resp = sess.run(session_id, text=text, modality=Modality.TEXT,
                      variables=seed if turn == 0 else None,
                      use_tool_fakes=not args.real)
    except Exception as exc:  # noqa: BLE001 - a platform error IS the observation
      print(f"    ! {type(exc).__name__}: {str(exc)[:200]}\n")
      return 1
    turn += 1
    parsed = _parsed(resp)
    print(f"\nagent> {parsed.consolidated_agent_text or '(silent)'}")
    names = [tc.name for tc in (parsed.tool_calls or [])]
    if names:
      print(f"{DIM}       tools: {', '.join(names)}{OFF}")
    print()


if __name__ == "__main__":
  raise SystemExit(main())
