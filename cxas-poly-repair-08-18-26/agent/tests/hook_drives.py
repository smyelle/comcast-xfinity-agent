"""The five live drives that see hook behaviour, recorded so steps can be diffed.

Steps that move a write out of `hooks.py` and into the declarative layer cannot be proven
offline: the byte-identical oracle does not apply, and `hook_diff --e2e` runs the engine
but not CES. So each of those steps is checked against a recorded baseline of what the
deployed agent actually said.

    APP_ID=<id> python tests/hook_drives.py --save baseline
    APP_ID=<id> python tests/hook_drives.py --against baseline

Each drive targets one thing a step puts at risk:

  dispatch    the P4 transfer interpolates activityType/activityCode/jobType. An
              unresolvable placeholder makes the engine raise mid-render and the caller
              hears the CES crash envelope. This is what `event_slot(default=)` must keep
              working.
  fee         `technician_fee` interpolated into the fee answer, asked twice with a
              completed flow in between so the re-arm path is covered.
  spoken      the account given by VOICE rather than seeded, which is the only path where
              the value exists in `filled` and nothing but the mirror puts it in session
              state -- what `publish=` has to replace.
  completed   a repair journey driven to a successful finish. It was meant to prove
              re-entry, and instead proved re-entry is NOT REACHABLE here: every path that
              completes `repair` also ends the call -- declining the walkthrough, fixing
              the fault, exhausting the tips. A pending offer also swallows a topic
              change. So `_flow_clear` on re-entry, which is what `shared=True` would
              protect against, has no driveable path in this agent.
  walkthrough the all-clear walkthrough answered tip by tip, exercising the per-turn
              mutexes and the tip cap -- both deliberately NOT being changed, so a
              difference here means a step reached further than intended.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SDK = os.path.dirname(_HERE)
sys.path.insert(0, _SDK)
sys.path.insert(0, _HERE)

import labs_paths  # noqa: E402
labs_paths.add_sdk_paths(driver=True)

from cxas_scrapi.core.sessions import Modality, Sessions  # noqa: E402
import diag_check as dc  # noqa: E402

PROJECT, LOCATION = "ces-deployment-dev", "us"

# (name, [turns]) — a turn is an utterance, or "" for a silence tick.
DRIVES = [
    ("dispatch", ["my internet is down", "8069100230359944", "", ""]),
    ("fee", ["my internet is down", "8069100230361005", "will this cost me anything",
             "", "and will the visit cost me"]),
    ("spoken", ["my internet is not working", "eight zero six nine one zero zero two "
                "three zero three five nine nine four six", ""]),
    # Named for what it actually does. Three routes to a completed repair journey were
    # tried -- decline, fix, exhaust -- and all three END the session, so the turn after
    # the completion always errors SESSION_ALREADY_ENDED. That error IS the recorded
    # baseline: if a later change makes the call survive a completion, this goes red.
    ("completed", ["my internet is down", "8069100230359946", "just one device",
                   "yes please", "that fixed it thanks",
                   "my internet is playing up again"]),
    ("walkthrough", ["my internet is down", "8069100230359946", "just one device",
                     "yes please", "that didn't work", "still nothing",
                     "no better"]),
]


# Measured, not assumed: driving the SAME build twice produces these two verdicts
# inconsistently on the opening turns. Both are timing-dependent by nature -- the early
# scope announce and the ContextGate bridge fire only if the sweep has not already
# resolved within the turn -- so on a live deployment their presence is a race, not a
# behaviour. Comparing them turn-by-turn reports a regression on every second run.
#
# They are EXCLUDED from the comparison and listed here so that exclusion is a decision
# on the record rather than a silent tolerance. Everything else is compared exactly.
_RACY = {"verdict_wifi_scope_early", "verdict_bridge_to_sweep"}


def _stable(verdicts):
  return [v for v in verdicts if v not in _RACY]


def _norm(agent):
  """A CES error carries a fresh session id; compare the failure, not the id."""
  if agent.startswith("ERR ") and "reason:" in agent:
    return agent.split("reason:")[0].strip()
  return agent


def resolve(app):
  if app.startswith("projects/"):
    return app
  return f"projects/{PROJECT}/locations/{LOCATION}/apps/{app}"


def drive(app, name, turns, tag):
  sess = Sessions(app_name=resolve(app))
  sid = f"{name}-{tag}"
  rows = []
  for utt in turns:
    try:
      resp = sess.run(sid, text=utt, modality=Modality.TEXT)
      text, tools = dc.turn_result(resp)
    except Exception as exc:                                   # noqa: BLE001
      rows.append({"said": utt, "agent": f"ERR {type(exc).__name__}: {exc}",
                   "verdicts": []})
      break
    rows.append({
        "said": utt,
        "agent": (text or "").strip(),
        "verdicts": sorted(t for t in tools if t.startswith("verdict_")),
    })
  return rows


def main():
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--app", default=os.environ.get("APP_ID", ""))
  ap.add_argument("--save", metavar="NAME", help="record these drives under a name")
  ap.add_argument("--against", metavar="NAME", help="compare against a recorded run")
  ap.add_argument("--only", action="append", help="run just this drive (repeatable)")
  ap.add_argument("--tag", default=str(os.getpid()))
  args = ap.parse_args()
  if not args.app:
    raise SystemExit("APP_ID or --app is required")

  wanted = [d for d in DRIVES if not args.only or d[0] in args.only]
  out = {name: drive(args.app, name, turns, args.tag) for name, turns in wanted}

  for name, rows in out.items():
    print(f"\n=== {name}")
    for r in rows:
      print(f"  CALLER : {r['said'] or '(silence)'}")
      print(f"  AGENT  : {r['agent'][:150] or '(silent tick)'}")
      if r["verdicts"]:
        print(f"           {','.join(r['verdicts'])}")

  path = os.path.join(_HERE, "drive_records", f"{args.save or args.against}.json")
  if args.save:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
      json.dump(out, fh, indent=2, sort_keys=True)
    print(f"\nsaved -> {path}")
    return 0

  if not args.against:
    return 0

  with open(path) as fh:
    base = json.load(fh)
  problems = []
  for name, rows in out.items():
    was = base.get(name)
    if was is None:
      problems.append(f"{name}: no baseline recorded")
      continue
    for i, (a, b) in enumerate(zip(was, rows)):
      # The agent's exact wording can vary on a model turn; the VERDICT that fired and
      # whether anything was said at all are the parts that must not move.
      if _stable(a["verdicts"]) != _stable(b["verdicts"]):
        problems.append(f"{name} turn {i}: verdicts "
                        f"{_stable(a['verdicts'])} -> {_stable(b['verdicts'])}")
      if bool(a["agent"]) != bool(b["agent"]):
        problems.append(f"{name} turn {i}: spoke={bool(a['agent'])} -> {bool(b['agent'])}")
      if _norm(a["agent"]) != _norm(b["agent"]):
        problems.append(f"{name} turn {i}: WORDING changed\n"
                        f"      was: {a['agent'][:110]}\n"
                        f"      now: {b['agent'][:110]}")
    if len(was) != len(rows):
      problems.append(f"{name}: {len(was)} turns -> {len(rows)}")

  print()
  if problems:
    print(f"{len(problems)} difference(s) from '{args.against}':")
    for p in problems:
      print(f"  {p}")
    return 1
  print(f"identical to '{args.against}' — same verdicts, same wording, every turn")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
