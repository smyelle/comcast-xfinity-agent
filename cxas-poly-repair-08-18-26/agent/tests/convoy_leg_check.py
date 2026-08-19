"""Offline proof that the convoy leg's own status agrees with its own routing action.

Why this file exists rather than a `ladder_check` row or a live drive.

`ladder_check` seeds `convoy_status` directly, so it can never exercise the code that
PRODUCES the value. A `--demo` build answers the leg from an inlined fixture, so no
drive of one reaches the mapping table either. And the live path needs an account whose
Convoy record carries the recommendation in question -- no account available here does.
So the table has to be tested as code: exec the EMITTED leg (not `substrate/`; the two
have differed before, and the emitted copy is what runs) with a stubbed Convoy response,
and read what it publishes.

What it caught. The priority table is the only producer of `routing_action` and it writes
`"predictive_swap"`; the status derivation twelve lines later tested `== "swap"`, a
spelling written nowhere. So a real `PREDICTIVE_GATEWAYSWAP` recommendation made the leg
publish `convoy_status = "clear"` -- a gateway that needs replacing, reported healthy.
It was masked rather than harmless: `settle_diagnostics` re-derives from `routing_action`
downstream and handles both spellings, so the wrong value was overwritten before a rung
could read it. Masked by ordering is not fixed.

The assertion that would have caught it in the first place is the last one here, and it
is a PRODUCERS-VS-CONSUMERS check rather than a per-id one: every routing action the
table can emit must be a value the status derivation recognises.

    python tests/convoy_leg_check.py [--app-dir ./built]
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

LEG = os.path.join("tools", "SweepLegs_leg_convoy_leg", "python_function",
                   "python_code.py")

# Recommendation id -> (routing_action, convoy_status) the leg must publish.
#
# Every id in the leg's own priority walk, so a new one cannot be added without a row --
# `check_every_id_is_covered` below is what enforces that. The statuses are what the
# ladder reads: `predictive_impairment` arms `HandleConvoyImpairment`, `predictive_swap`
# arms `HandleConvoySwap`, and `clear` arms nothing.
EXPECTED = {
    "PREDICTIVE_GATEWAYSWAP": ("predictive_swap", "predictive_swap"),
    "OutsideHomeSRO": ("technician", "predictive_impairment"),
    "XITNetworkImpairment": ("technician", "predictive_impairment"),
    "RFCxel": ("technician", "predictive_impairment"),
    # Routed to `technician` deliberately or not -- that is a live product question, and
    # this row pins TODAY'S behaviour so whichever way it is answered is a one-line diff
    # with a failing test in front of it.
    "XIModemOfflineDigital": ("technician", "predictive_impairment"),
    "DigitalFailingWAN": ("technician", "predictive_impairment"),
    "XIT_AIQ_PREDICTIVE_WAN_SCORE": ("technician", "predictive_impairment"),
    "PHTAllOut": ("technician", "predictive_impairment"),
    "PHTPartialChannelBonding": ("technician", "predictive_impairment"),
    "PHTLite": ("technician", "predictive_impairment"),
    # Nothing Convoy recognises as repairable.
    "SOMETHING_CONVOY_ADDED_LATER": ("none", "clear"),
}


def _load(app_dir: str):
  path = os.path.join(app_dir, LEG)
  with open(path) as fh:
    return fh.read(), path


def _publish(src: str, path: str, recommendation: str) -> dict:
  """Run the leg once against a stubbed Convoy payload and return what it publishes."""

  class _Context:

    def __init__(self):
      # The leg refuses to call out at all without this, which is itself correct.
      self.state = {"convoy_api_server": "https://stub.invalid"}
      self.session_id = "convoy-leg-check"

  class _Tools:

    def convoy_recs_account_getRecommendationsByAccount(self, _args):
      return {"recommendations": [{
          "id": recommendation,
          # `recommendedAction` is read to choose between the truck-roll and
          # no-recommendation branches. A value is supplied so that path is exercised
          # rather than skipped.
          "additionalInformation": [
              {"aspect": [{"name": "recommendedAction", "value": "createAppointment"}]}],
      }]}

  namespace = {"context": _Context(), "tools": _Tools()}
  exec(compile(src, path, "exec"), namespace)  # noqa: S102 - our own emitted file
  return namespace["check_convoy_recommendations"]("8069100230359928")


def check_every_id_is_covered(src: str) -> int:
  """A recommendation the leg knows about but this file does not is a gap, not a pass."""
  known = set()
  for line in src.splitlines():
    if "REPAIR_RECOMMENDATION_IDS" in line:
      continue
    stripped = line.strip()
    if stripped.startswith('"') and stripped.endswith('",') and stripped.count('"') == 2:
      known.add(stripped.strip('",'))
  missing = {i for i in known if i not in EXPECTED} & _priority_ids(src)
  for name in sorted(missing):
    print(f"FAIL {name:30} is in the leg's priority walk with no row here")
  return len(missing)


def _priority_ids(src: str) -> set:
  """The ids the priority walk actually branches on."""
  found = set()
  for line in src.splitlines():
    stripped = line.strip()
    if not (stripped.startswith("if name ") or stripped.startswith("elif name ")):
      continue
    for chunk in stripped.split('"')[1::2]:
      found.add(chunk)
  return found


def check_no_unreachable_status(src: str) -> int:
  """Every routing action the table can emit must be one the status derivation knows.

  This is the check that would have caught the defect. A per-id table only says what
  today does; this says the two halves of one function speak the same vocabulary.
  """
  produced, recognised = set(), set()
  in_walk = False
  for line in src.splitlines():
    stripped = line.strip()
    if stripped.startswith("routing_action = "):
      value = stripped.split("=", 1)[1].strip().strip('"')
      if value and not value.startswith(("rec", "_")):
        produced.add(value)
    if stripped.startswith('convoy_status = "clear"'):
      in_walk = True
      recognised.add("none")
    if in_walk and ("routing_action ==" in stripped or "routing_action in" in stripped):
      for chunk in stripped.split('"')[1::2]:
        recognised.add(chunk)
  orphans = {p for p in produced if p not in recognised and p != "none"}
  for name in sorted(orphans):
    print(f"FAIL routing_action {name!r} is produced by the table and recognised by "
          f"nothing in the status derivation — the leg reports 'clear' for it")
  return len(orphans)


def run(app_dir: str) -> int:
  src, path = _load(app_dir)
  failures = check_every_id_is_covered(src) + check_no_unreachable_status(src)
  for recommendation, (want_action, want_status) in sorted(EXPECTED.items()):
    published = _publish(src, path, recommendation)
    action = published.get("routing_action")
    status = published.get("convoy_status")
    ok = (action == want_action and status == want_status)
    failures += (not ok)
    print(f"{'ok  ' if ok else 'FAIL'} {recommendation:30} "
          f"routing_action={action!s:18} convoy_status={status!s}")
    if not ok:
      print(f"       wanted routing_action={want_action!r} convoy_status={want_status!r}")
  print(f"\n{len(EXPECTED) - failures}/{len(EXPECTED)} convoy recommendations map to a "
        f"status their own routing action agrees with")
  return 1 if failures else 0


if __name__ == "__main__":
  ap = argparse.ArgumentParser()
  ap.add_argument("--app-dir", default="./built")
  raise SystemExit(run(os.path.abspath(ap.parse_args().app_dir)))
