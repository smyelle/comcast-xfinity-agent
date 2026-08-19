"""An `on_exhaust` that ends the session must END the session — offline too.

A hand-off attaches at four places a flow gives up a call, and two of them mark
the leg terminal in `sm["status"]` while two did not:

    escalate rail            `_terminate_control`   -> status="zombie"    ok
    terminal announce        `_cascade_announce`    -> status="complete"  ok
    task on_failure.on_exhaust                      -> nothing            BUG
    slot validation.on_exhaust                      -> nothing            BUG

In PRODUCTION the omission is invisible. CES reads the response parts and tears
the session down the moment it sees a `Part.from_end_session`, whatever the
engine's state machine says, so a live call really does end. OFFLINE it is not
invisible: `flows.sim` derives the call's disposition from `sm["status"]`, so a
walk kept being served turns after the caller had been handed to a human — and a
suite could assert behaviour that cannot happen on a real call. The simulator is
a primary verification tool here, so that quietly weakened everything built on
it.

These tests EXECUTE the paths rather than compare their source: each walk is
driven through `flows.sim.engine_sim`, and the last test runs the same walk
against both on-disk copies of the engine (the `flows` bundle and the studio's
`studio_framework` runtime root) so the two are shown to agree by behaviour.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_exhaust_end_session.py
"""

from __future__ import annotations

import os

import pytest

import flows
from flows.engine import loader as fb
from flows.sim import engine_sim


HUMAN = flows.handoff(flows.ujet(menu_id="90"))

SAY = "I'm not able to do that here — let me get you to someone who can."

# The hand-off pair as it rides the disposition turn: the vendor payload the
# platform routes on, then the end that gives the leg up.
PAIR = [
    {"type": "payload",
     "data": {"ujet": {"menu_id": "90", "escalation_reason": "by_virtual_agent",
                       "type": "action", "action": "escalation",
                       "language": "en"}}},
    {"type": "end_session", "reason": "transfer", "escalated": True},
]


def _task_exhaust_config() -> dict:
  """A lookup that fails, exhausts on its first failure, and hands off.

  `followup` exists so the flow has somewhere to GO after the hand-off: without
  it the DAG runs out of slots and reports complete on its own, which would hide
  the defect instead of exposing it.
  """
  f = flows.Flow("exhaust_test", root_agent="Exhaust_Agent")
  f.add(
      flows.event_slot("account"),
      flows.result_slot("verdict", "Lookup"),
      flows.user_slot("followup", "Anything else I can help with?"),
  )
  f.task("Lookup", "lookup", ["account"], "verdict",
         on_failure={"max_retries": 1, "on_exhaust": HUMAN.on_exhaust(SAY)})
  return f.to_config()


def _slot_exhaust_config() -> dict:
  """The twin rung: a slot whose validation ladder gives up and hands off."""
  f = flows.Flow("exhaust_test", root_agent="Exhaust_Agent")
  f.add(
      flows.user_slot("pin", "What's your PIN?",
                      validation={"max_retries": 1,
                                  "on_exhaust": HUMAN.on_exhaust(SAY)}),
      flows.user_slot("followup", "Anything else I can help with?"),
  )
  return f.to_config()


def _announce_config() -> dict:
  """The path that always got this right — the reference the others must match."""
  f = flows.Flow("exhaust_test", root_agent="Exhaust_Agent")
  f.add(
      flows.event_slot("account"),
      flows.announce("human_transfer", [SAY], requires=["account"],
                     handoff=HUMAN),
      flows.user_slot("followup", "Anything else I can help with?"),
  )
  return f.to_config()


def _start(config: dict, **kw) -> str:
  engine_sim.reset_store()
  session_id, _opening = engine_sim.start(config, "exhaust_test", **kw)
  return session_id


def _open(config: dict, **kw) -> dict:
  """The OPENING turn — where an announce whose `requires` are already prefilled
  fires, with no user turn in between."""
  engine_sim.reset_store()
  _session_id, opening = engine_sim.start(config, "exhaust_test", **kw)
  return opening


def _step(session_id: str, **kw) -> dict:
  return engine_sim.step({"session_id": session_id, **kw})


def _fail_the_task(session_id: str) -> dict:
  return _step(session_id, kind="task_result", task_name="Lookup",
               success=False, result={})


def _reject_the_pin(config: dict, framework_root: str | None = None) -> dict:
  """Walk the slot rung to its exhaust and return the engine's `{action, sm}`.

  Driven through `run_engine` rather than the simulator's `setter_call`, which
  loads a real setter TOOL off disk — `set_pin` has no resource here. Seeding
  `_slot_errors` is exactly the input `_handle_slot_errors` pops, so this is the
  same code path a rejected value takes on a live turn.
  """
  out = fb.run_engine(config, {}, config_id="exhaust_test",
                      framework_root=framework_root)
  sm = out["sm"]
  sm["_slot_errors"] = [{"slot": "pin", "code": "invalid"}]
  return fb.run_engine(config, sm, last_user_text="0000",
                       config_id="exhaust_test", framework_root=framework_root)


def _assert_handed_off(turn: dict) -> None:
  """The disposition turn: the pair is emitted AND the leg is marked over."""
  assert turn["response_parts"] == PAIR
  assert turn["agent_text"] == SAY
  assert turn["status"] == "complete"
  assert turn["next_action"] == "terminal"


# ── The two rungs that did not mark the leg ──────────────────────────────────


def test_a_task_exhaust_handoff_ends_the_session():
  session_id = _start(_task_exhaust_config(), event_data={"account": "8069"})
  _assert_handed_off(_fail_the_task(session_id))


def test_a_slot_exhaust_handoff_ends_the_session():
  out = _reject_the_pin(_slot_exhaust_config())
  assert out["action"]["response"] == PAIR
  assert out["action"]["message"] == SAY
  assert out["sm"]["status"] == "complete"


def test_the_sim_serves_no_flow_past_a_task_exhaust_hangup():
  """The defect as the simulator saw it: the caller is gone, keep asking anyway.

  With the status unset the walk read `in_progress` / `next_question` and went
  on collecting `followup` — a turn that cannot exist, because CES ended the call
  on the `end_session` part of the turn before.
  """
  session_id = _start(_task_exhaust_config(), event_data={"account": "8069"})
  _fail_the_task(session_id)

  after = _step(session_id, kind="user_text", text="hello? are you there?")
  assert after["status"] == "complete"
  assert after["next_action"] == "terminal"


# ── Agreement with the path that was always right ────────────────────────────


def test_every_handoff_rung_reports_the_same_disposition():
  """Announce, task-exhaust and slot-exhaust: one hand-off, one outcome.

  Executed, not read: the same pair emitted through three different rungs must
  leave the session in the same state, or a suite's conclusions depend on which
  rung an author happened to attach the hand-off to.
  """
  # The announce speaks its `texts` first, then the pair; the two exhaust rungs
  # carry their line in `message` instead. Same pair, same disposition.
  from_announce = _open(_announce_config(), event_data={"account": "8069"})
  assert from_announce["response_parts"] == [{"type": "text", "text": SAY}, *PAIR]

  task = _start(_task_exhaust_config(), event_data={"account": "8069"})
  from_task = _fail_the_task(task)
  assert from_task["response_parts"] == PAIR

  from_slot = _reject_the_pin(_slot_exhaust_config())
  assert from_slot["action"]["response"] == PAIR

  for turn in (from_announce, from_task):
    assert (turn["status"], turn["next_action"]) == ("complete", "terminal")
  assert from_slot["sm"]["status"] == "complete"


# ── The blessed copy and the studio's runtime copy ───────────────────────────

# `studio_framework` is the root the Labs studio server importlib-loads at
# runtime; the flows bundle is what scaffolds new apps. A byte gate already pins
# them (slot_studio/tests/test_framework_runtime_sync.py) — this runs the walk
# through BOTH and compares the outcome, so the two are shown to agree by
# behaviour and not only by hash.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_STUDIO_FRAMEWORK = os.path.join(
    _REPO_ROOT, "service", "app", "products", "slot_studio", "studio_framework")


@pytest.mark.skipif(not os.path.isdir(_STUDIO_FRAMEWORK),
                    reason="studio_framework mirror not present in this tree")
def test_the_studio_runtime_copy_ends_the_session_too():
  blessed = _reject_the_pin(_slot_exhaust_config())
  studio = _reject_the_pin(_slot_exhaust_config(),
                           framework_root=_STUDIO_FRAMEWORK)

  assert blessed["sm"]["status"] == "complete"
  assert studio["sm"]["status"] == blessed["sm"]["status"]
  assert studio["action"]["response"] == blessed["action"]["response"] == PAIR
