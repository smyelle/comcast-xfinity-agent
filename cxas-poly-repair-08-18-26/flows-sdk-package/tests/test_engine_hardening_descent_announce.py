"""Engine hardening — a component descent must not swallow the parent's announce.

An announce that becomes eligible on the SAME pass a component task fires is spoken by
the tool-task branch (it merges the announce text into the fire message) but was NOT by
the component branch, which returned the bare end-of-descent dict. The cascade has
already marked the announce slot filled by then, so the line is never re-offered: the
authored copy is gone for the rest of the call, silently, with the child's first
question standing in its place.

Parking it on `sm` rather than writing it onto the returned action is deliberate — an
in-pass descent re-walk discards that dict. `_finalize_directive` is the one point every
descent path passes through exactly once, so the merge happens there.

Driven end to end through the offline sim (no network, no creds, no LLM).

Run:
  cd /Users/fsamuel/Labs/cxas-labs
  PYTHONPATH=packages/flows/src .venv/bin/python -m pytest \
      packages/flows/tests/test_engine_hardening_descent_announce.py -q
"""

from __future__ import annotations

import pytest

import flows
from flows.engine import loader as fb
from flows.sim import engine_sim

VERDICT = "Here is what I found: a bad modem."
CHILD_ASK = "What is your PIN?"


@pytest.fixture(autouse=True)
def _drop_engine_caches():
  """The engine caches compiled configs process-globally, keyed by config id."""
  yield
  fb.clear_cache()


def _child() -> dict:
  c = flows.Flow("hardening_descent_child", root_agent="A")
  c.add(flows.user_slot("pin", ask=CHILD_ASK))
  return c.to_config()


def _parent(*, preempt: bool) -> dict:
  """A diagnostic task, an announce reporting its verdict, and a component — the
  announce and the component both become eligible the pass the task's result lands."""
  f = flows.Flow("hardening_descent_parent", root_agent="A")
  f.add(
      flows.event_slot("account"),
      flows.result_slot("diagnosis", "Diag"),
  )
  f.task(flows.task("Diag", "diagnose", ["account"], "diagnosis",
                    out_key="diagnosis"))
  f.add(flows.announce("verdict", ["Here is what I found: {diagnosis}."],
                       requires=["diagnosis"], preempt=preempt))
  f.task(flows.component("Helper", "hardening_descent_child",
                         requires=["diagnosis"]))
  return f.to_config()


def _descend(*, preempt: bool = False) -> dict:
  """Open the flow, answer the diagnostic, and return the descending turn."""
  engine_sim.reset_store()
  session_id, opening = engine_sim.start(
      _parent(preempt=preempt), "hardening_descent_parent",
      event_data={"account": "123"},
      configs={"hardening_descent_child": _child()})
  assert (opening.get("function_call") or {}).get("name") == "diagnose"
  return engine_sim.step({
      "session_id": session_id, "kind": "task_result", "task_name": "Diag",
      "success": True, "result": {"success": True, "diagnosis": "a bad modem"},
  })


def _spoken(result: dict) -> list[str]:
  return [p.get("text", "") for p in (result.get("response_parts") or [])]


def test_the_pass_really_does_descend_into_the_child():
  """Pins the premise: without the descent on this turn there is nothing to swallow
  and the assertions below would pass for the wrong reason."""
  out = _descend()
  assert [f.get("child_config") for f in out["sm"].get("_call_stack", [])] == [
      "hardening_descent_child"]


def test_the_parent_announce_is_spoken_on_the_descending_turn():
  out = _descend()
  assert VERDICT in _spoken(out), (
      "the authored verdict is lost for good — its slot is already marked filled")


def test_the_child_question_still_follows_the_announce():
  """Order matters: the announce reports what just happened, the child's question
  asks what happens next. Reversed, the caller answers before hearing the finding."""
  spoken = _spoken(_descend())
  assert spoken.index(VERDICT) < spoken.index(CHILD_ASK)


def test_nothing_is_left_parked_for_a_later_turn():
  """The carry is one-shot. Left on `sm` it would be re-spoken every later turn."""
  out = _descend()
  assert out["sm"].get("_carry_announce") is None


def test_a_preempting_announce_keeps_its_preempt_through_the_descent():
  """`preempt` is the author saying the line must not be re-worded by the model;
  merging the text while dropping the flag would quietly change the delivery."""
  assert VERDICT in _spoken(_descend(preempt=True))
