"""Prove `hook_diff.py` can fail.

The whole staged plan for `hooks.py` rests on this harness: steps that claim "changes
nothing" are believed because the diff is empty. An empty diff from a harness that cannot
see anything is worth less than no harness at all, so each mutation below is a specific
way a step could silently break the agent, and every one must be caught.

    python tests/hook_diff_selftest.py
"""

import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SDK = os.path.dirname(_HERE)

# (label, find, replace) — applied to hooks.py one at a time.
MUTATIONS = [
    # A dropped mirror write: the classic way a `publish=` migration loses session state.
    # Re-anchored when #37 replaced the `seed()` helper with direct writes; this is the
    # same defect at the same layer, on a write that still exists.
    ("drop a mirror write",
     '        state[allowed] = "true"', '        pass  # MUTANT'),
    # A gate latch that stops opening: the caller's answer lands on a dark ladder.
    ("stop opening the wifi answer gate",
     'wifi_answer_allowed', 'wifi_answer_allowed_MUTANT'),
    # A dispatch value that stops being promoted: renders the wrong payload rather than
    # raising. Re-anchored for #37, which moved the literal defaults out of this file and
    # left the PROMOTION of an upstream-seeded value behind -- so that is what is mutated.
    ("stop promoting a seeded dispatch value",
     'for key in ("activityType", "activityCode", "jobType"):',
     'for key in ():'),
    # A skipped early return: the sweep re-dispatches on a turn it should not.
    ("skip the already-swept early return",
     'and filled.get("diagnostics_complete")', 'and False'),
    # The account precedence order quietly reversed.
    ("break account precedence",
     'state.get("accountNumber")', 'state.get("account_id")'),
    # The per-turn clear stops clearing: the fee answer is suppressed forever.
    ("stop clearing a per-turn mutex", '"cost_answered",', '"cost_answered_MUTANT",'),
    # The tip cap moves: the walkthrough talks through the whole list and ends the call.
    ("move the tip cap", "_WIFI_TIP_LIMIT = 3", "_WIFI_TIP_LIMIT = 99"),
]


def run_diff():
  env = dict(os.environ)
  env.pop("CXAS_LABS", None)
  env["PYTHONDONTWRITEBYTECODE"] = "1"
  proc = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "hook_diff.py")],
                        cwd=_SDK, env=env, capture_output=True, text=True)
  return proc.returncode, proc.stdout + proc.stderr


def main():
  path = os.path.join(_SDK, "hooks.py")
  original = open(path).read()

  # A clean tree reports either "unchanged" (byte-equal to the ref) or "IDENTICAL"
  # (differs textually but writes the same thing). Both mean no behaviour change; which
  # one depends on whether an earlier step has already landed.
  def clean(text):
    return "unchanged" in text or "IDENTICAL" in text

  code, out = run_diff()
  if not clean(out):
    print("Expected a clean tree. Got:\n" + out[-800:])
    return 1
  print("positive control: clean tree shows no behaviour change\n")

  failures = []
  for label, find, replace in MUTATIONS:
    n = original.count(find)
    if n == 0:
      failures.append(f"{label}: anchor {find!r} not found — the selftest has rotted")
      print(f"  {label:<38} ANCHOR MISSING")
      continue
    try:
      open(path, "w").write(original.replace(find, replace))
      code, out = run_diff()
    finally:
      open(path, "w").write(original)

    caught = code != 0 and not clean(out)
    if not caught:
      failures.append(f"{label}: hook_diff did NOT catch it")
      print(f"  {label:<38} NOT CAUGHT  <-- harness is blind here")
      continue
    m = re.search(r"(\d+) cases, (\d+) differ", out)
    where = f"{m.group(2)}/{m.group(1)} cases" if m else "caught"
    print(f"  {label:<38} caught ({where})")

  code, out = run_diff()
  if not clean(out):
    failures.append("hooks.py did not restore cleanly")
    print("\nRESTORE FAILED")

  print()
  if failures:
    print(f"{len(failures)} problem(s):")
    for f in failures:
      print(f"  {f}")
    return 1
  print(f"all {len(MUTATIONS)} mutations caught; hook_diff can fail")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
