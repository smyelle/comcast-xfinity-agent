#!/usr/bin/env python3
"""The scope-during-the-sweep sequence, driven turn by turn against the real engine.

`ladder_check` cannot score this. It seeds one state and asks which rung fires, and the
thing under test is a SEQUENCE across turns whose defining feature -- a task parked in
`_awaiting_async` -- it has no way to express. So every mid-sweep state it seeds leaves
`ContextGate` eligible, which wins on declaration order, and the early ask is unreachable.

This drives the sequence instead: seed the wait by hand into `sm["_awaiting_async"]`, the
way the engine marks a dispatched asynchronous task, and walk the turns the caller would
actually take. It is the same engine `ladder_check` loads, so it is not a mock of the
DAG -- only of the platform's part in it.

What it CANNOT see, and what therefore still needs a live drive: the platform's actual
turn cadence, whether the remote poll lands its statuses on the turn the caller speaks or
the one after, and anything the model does with a turn the engine leaves empty. Two of
the three defects this file was written to catch were of exactly that kind.

    python tests/overlap_check.py [--app-dir ./built]
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths()

from flows.engine import loader  # noqa: E402

from tests.ladder_check import FRAMEWORK_ROOT, load_config  # noqa: E402

ACCOUNT = "8069100230359946"

# The state the engine is in once `ContextGate` has answered and `Specialists` is out:
# the account resolved, the fast legs reported, and the two statuses the remote job owns
# still unfilled. This is the shape `ladder_check` cannot seed.
MID_SWEEP = {
    "accountNumber": ACCOUNT,
    "caller_spoke": "true",
    "reason_for_call": "internet is not working",
    "account_status": "clear",
    "has_mac": "true",
    "cable_modem_mac": "AA:BB:CC:DD:EE:FF",
    "outage_status": "none",
    "convoy_status": "clear",
    "SweepLegs_done": "true",
}
# ...and what `Settle` fills in once the job reports.
SWEPT = {"network_status": "healthy", "gateway_status": "healthy",
         "wifi_status": "healthy", "diagnostics_complete": "true"}

# Which sweep tasks have already run at each stage. Mid-sweep the gate and the fast legs
# are done and `Specialists` is out; once it reports, `Settle` has run too.
_RAN = ("ContextGate", "leg_outage", "leg_convoy")
_SWEPT_RAN = _RAN + ("Specialists", "Settle")


def fresh(config: dict, filled: dict, awaiting: bool, ran: tuple = ()) -> dict:
  sm = loader.seed_sm(config)
  sm["filled"] = dict(filled)
  sm.setdefault("pending", {})
  sm[sm.get("_gate_slot") or "active_flow"] = "repair"
  if sm.get("_gate_slot"):
    sm["filled"][sm["_gate_slot"]] = "repair"
  # A task the engine has already run is remembered in `task_results`, and that -- not
  # its condition -- is what stops it firing twice. `ContextGate` matters here: it lists
  # `network_status` and `diagnostics_complete` among its outputs, so mid-sweep its
  # condition is still perfectly satisfiable and it wins on declaration order. Without
  # this the probe replays the gate forever and nothing downstream is ever reachable.
  for task_name in ran:
    sm.setdefault("task_results", {})[task_name] = {"success": True}
  if awaiting:
    # How the engine marks a dispatched ASYNCHRONOUS task. `inputs` is what the staleness
    # guard compares against on arrival, so it has to carry the real dispatch values or
    # the guard reads the wait as answering a question that has since changed.
    sm["_awaiting_async"] = {"Specialists": {
        "tool": "resolve_specialists_remote", "since": 0,
        "inputs": {"accountNumber": filled.get("accountNumber"),
                   "cable_modem_mac": filled.get("cable_modem_mac")}}}
  return sm


def _one(config: dict, sm: dict, text: str) -> tuple[str, str]:
  out = loader.run_engine(config, sm, last_user_text=text, config_id="repair")
  action = out.get("action", {})
  name = (action.get("task") or {}).get("name") or action.get("task_name")
  if not name and action.get("function_call"):
    tool = action["function_call"].get("name")
    name = next((t["name"] for t in config["tasks"] if t.get("tool") == tool), None)
  return name, (action.get("message") or "")


def turn(config: dict, sm: dict, text: str, limit: int = 6) -> list[str]:
  """Every task that fires on ONE turn, in order.

  The engine does not stop at the first one, and reading only the first is what made an
  earlier version of this file report `ContextGate` where the live agent plainly asked the
  scope question: on the real turn 522 the gate fired, its result landed, and the cascade
  carried on to the early ask in the same breath. So a turn is a LIST, and an assertion
  about a turn is an assertion about that list.

  A task parked in `_awaiting_async` is left alone rather than replayed -- that is the
  whole point of the wait, and replaying it here would model the job answering instantly.
  """
  fired: list[str] = []
  for _ in range(limit):
    name, _said = _one(config, sm, text if not fired else "")
    if not name or name in fired:
      break
    fired.append(name)
    if name in (sm.get("_awaiting_async") or {}):
      break
    task = next(t for t in config["tasks"] if t["name"] == name)
    sm.setdefault("task_results", {})[name] = {"success": True}
    for slot in (task.get("outputs") or {}).values():
      sm["filled"].setdefault(slot, "true")
    promote(sm)
  return fired


def latch(config: dict, sm: dict, task_name: str) -> None:
  """Apply a fired rung's outputs, the way the engine does when its tool returns."""
  task = next(t for t in config["tasks"] if t["name"] == task_name)
  for slot in (task.get("outputs") or {}).values():
    sm["filled"][slot] = "true"


def promote(sm: dict) -> None:
  """The before_agent hook's promotion, replayed. Kept in step with hooks.py by
  `check_early_scope_ask`, which pins the hook source that this mirrors."""
  early = sm["filled"].get("wifi_scope_early")
  if early and not sm["filled"].get("wifi_scope"):
    sm["filled"]["wifi_scope"] = early
    sm["filled"]["wifi_scope_asked"] = "true"
    sm["filled"]["wifi_scope_allowed"] = "true"


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--app-dir", default=os.path.join(os.path.dirname(HERE), "built"))
  a = ap.parse_args()
  loader.set_framework_root(FRAMEWORK_ROOT)
  config = load_config(a.app_dir)
  failures = 0

  def check(label: str, got, want: str) -> None:
    """`got` may be one task or a whole turn; a turn passes if `want` is anywhere in it."""
    nonlocal failures
    ok = (want in got) if isinstance(got, list) else got == want
    failures += 0 if ok else 1
    shown = "+".join(got) if isinstance(got, list) else str(got)
    print(f"  {'ok  ' if ok else 'FAIL'} {label:52} fired={shown or '-'}  want={want}")

  # 1. THE ASK and 3. THE ANSWER TURN are BOTH covered offline now, in
  #    `tests/journey_check.py` (C1 for the ask, C3/C4 for the answer, C5 and C6 for what
  #    the answer changes later). Neither obstacle this note recorded turned out to be
  #    what it looked like:
  #
  #    `run_engine` does NOT clear `task_results` per turn -- it clears on a config-id
  #    change. What wipes it here is building an `sm` with `seed_sm` and no `_config_id`,
  #    so the first call looks like a config switch. In a real session it persists, and
  #    "ContextGate has already run" holds across turns.
  #
  #    The remote-tool poll is real, but it is not unrepresentable. While a job is in
  #    flight the engine returns early with a PARTIAL action carrying `function_calls`
  #    for the job's status tool, and it is guarded to fire once per turn -- so live, CES
  #    answers the status tool and the engine's second pass walks the DAG on the caller's
  #    own turn. A driver only loses that turn if it never answers the poll.
  #    `harness.Call._drain_poll` answers it with `status: "running"`, which leaves the
  #    job in flight, and the two turns then behave exactly as the live transcripts show.
  #
  # So this file is no longer the only cover for those turns. It is still worth keeping:
  # everything below is the part that outlives the wait, checked from a seeded state
  # rather than by walking to it, which is the cheaper and more direct assertion.
  print("\nthe caller answers during the wait")
  sm = fresh(config, MID_SWEEP, awaiting=True, ran=_RAN)

  # 2. THE CAPTURE. The answer arrives LATE -- after the job has finished but before
  #    anything has spoken a verdict. This is the case the first build got wrong: the
  #    capture window was gated on the sweep still running and had already shut.
  sm["filled"]["wifi_scope_early"] = "ONE_DEVICE"
  promote(sm)
  check("a late answer still reaches wifi_scope", sm["filled"].get("wifi_scope"),
        "ONE_DEVICE")

  # 3. AckScopeEarly must EXIST and be shaped to own that turn, even though whether it
  #    wins the turn is only observable live.
  ack = next((t for t in config["tasks"] if t["name"] == "AckScopeEarly"), None)
  check("an acknowledgement rung owns the answer turn",
        "present" if ack else "MISSING", "present")
  if ack:
    gates = {c.get("slot") for c in (ack["condition"].get("all") or []) if "slot" in c}
    check("...and it disappears once there is a verdict to speak",
          "gated" if "diagnostics_complete" in gates else "UNGATED", "gated")

  # 4. THE PAYOFF. Job reports, verdict lands, walkthrough accepted -- and the scoping
  #    question is NOT asked a second time, because the answer is already in hand.
  print("\nand the walkthrough that follows")
  seeded = dict(MID_SWEEP, **SWEPT)
  seeded.update(wifi_scope_asked_early="true", wifi_scope_early="ONE_DEVICE")
  sm3 = fresh(config, seeded, awaiting=False, ran=_SWEPT_RAN)
  promote(sm3)
  check("swept and all clear -> the offer", turn(config, sm3, ""), "HandleAllClear")
  sm3["filled"].update(wifi_offered="true", wifi_answer_allowed="true",
                       wifi_walkthrough="ACCEPT")
  check("accepted, scope already known -> straight to a tip",
        turn(config, sm3, "yes please"), "WifiTipRejoin")

  # 5. THE OTHER HALF. Asked early, never answered: the post-verdict question must still
  #    be put, or the walkthrough reaches the tips with no scope and stalls -- but in the
  #    wording that says it is the second time of asking. `AskScopeEarly` is the announce's
  #    own latch and is what tells the two wordings apart; `wifi_scope_asked_early` is a
  #    leftover of the rung the announce replaced and nothing reads it, so seeding that
  #    alone used to score the never-asked path under the asked-early name.
  unanswered = dict(MID_SWEEP, **SWEPT)
  unanswered.update(AskScopeEarly="true", wifi_scope_asked_early="true",
                    wifi_offered="true",
                    wifi_answer_allowed="true", wifi_walkthrough="ACCEPT")
  sm4 = fresh(config, unanswered, awaiting=False, ran=_SWEPT_RAN)
  promote(sm4)
  check("asked early, never answered -> ask it again, in different words",
        turn(config, sm4, "yes please"), "AskWifiScopeAgain")

  #    ...and the caller nobody asked during the sweep still gets the plain first ask.
  never_asked = dict(unanswered)
  never_asked.pop("AskScopeEarly")
  sm4b = fresh(config, never_asked, awaiting=False, ran=_SWEPT_RAN)
  promote(sm4b)
  check("never asked at all -> the question, put plainly",
        turn(config, sm4b, "yes please"), "AskWifiScope")

  # 6. THE CORRECTNESS CASE. An outage while the early answer is in hand: the caller
  #    hears the outage and is never offered in-home troubleshooting.
  print("\nand the sweep that does not come back clear")
  outage = dict(MID_SWEEP, network_status="skipped", gateway_status="skipped",
                wifi_status="skipped", diagnostics_complete="true",
                outage_status="active", outage_message="OUTAGE_MSG",
                customer_message="CUST_MSG",
                wifi_scope_asked_early="true", wifi_scope_early="ALL_DEVICES")
  sm5 = fresh(config, outage, awaiting=False, ran=_SWEPT_RAN)
  promote(sm5)
  check("outage outranks the scope answer", turn(config, sm5, ""), "HandleAreaOutage")

  print(f"\n{'PASS' if not failures else str(failures) + ' FAILURE(S)'}")
  return 1 if failures else 0


if __name__ == "__main__":
  raise SystemExit(main())
