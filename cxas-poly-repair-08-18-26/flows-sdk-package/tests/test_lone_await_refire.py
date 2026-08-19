"""A lone `awaits` task must not be marked as a parallel batch.

`_parallel_firing` exists for ONE reason: the legs of a fan-out run concurrently, so
`after_tool` is invoked once per leg with all of them racing to read-modify-write a single
state key, and that keeps one leg's result and drops the rest (ces-probes 37, 38). The
marker tells `after_tool` to stand aside so `before_model` — which runs once per pass
rather than once per leg — can ingest the whole batch with a single writer.

A lone `awaits` task reaches the same dispatch branch, because it needs the settle guard
just as much. It has no siblings, so there is nothing to race and nothing to protect. But
it was marked anyway, and being marked stranded it:

  * `after_tool` stood aside, so intake never saw the pending placeholder;
  * `before_model`'s compensating ingestion routes a pending payload back through
    `sm["_fanout"]["tools"]`, which only `_fanout_start` fills, and that runs for a
    PROGRESSIVE GROUP alone — a lone task has no entry, so it was dropped there too;
  * with neither path taking it, `_awaiting_async` was never marked, the selector kept
    seeing the task un-fired, and it was dispatched again on every reasoning pass until
    the ten-pass cap (ces-probes 72) killed the turn.

Measured on a converted agent as nine dispatches, nine responses and nine settle guards
inside one turn, a 400 for that turn, and recovery two turns later. `ces-probes 148`
reproduces it against a synchronous control that completes on its fire turn, which is what
established the re-fire belongs to the deferral rather than to the config.

These tests pin the SEAM, not the symptom: offline nothing dispatches concurrently, so the
race the marker defends against cannot be reproduced here. What is checkable is which side
of the marker each dispatch shape lands on, and that the wait is recorded when the result
comes back the ordinary way.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import flows  # noqa: E402
from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)

PENDING = {"result": "pending"}
_GUARD = "settle_guard"

_LEGS = (("inventory", "check_stock", "stock"),
         ("shipping", "check_shipping", "eta"))


def _lone(awaits=True):
  """One task, one tool, `awaits` on or off. The shape the Comcast sweep has."""
  f = flows.Flow("repair", root_agent="agent")
  f.add(flows.user_slot("account", ask="Account?"), flows.result_slot("done", "Sweep"))
  task = flows.task("Sweep", "run_sweep", ["account"], "done", out_key="success")
  if awaits:
    task["awaits"] = {"max_turns": 8, "say": "One moment."}
  f.task(task)
  return flows.App(root_flow=f, app_display_name="t").root_flow.to_config()


def _group():
  """Two legs in a progressive group — the shape the marker exists for."""
  f = flows.Flow("orders", root_agent="agent")
  f.add(flows.user_slot("order_id", ask="Order?"))
  for name, tool, out in _LEGS:
    f.add(flows.result_slot(out, name))
    task = flows.task(name, tool=tool, inputs=["order_id"], out_slot=out,
                      then_say=f"{name} says {{{out}}}.")
    task["parallel"] = "diagnostics"
    f.task(task)
  return flows.App(root_flow=f, app_display_name="t").root_flow.to_config()


def _sm(config, filled):
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = dict(filled), {}
  return sm


def _drive(config, sm, turn=1):
  out = fb.load_engine().slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "",
      "scanned_user_text": "", "is_inactivity": False,
      "event_data": {}, "config_id": config.get("config_id") or "t",
      "n_user_turns": turn,
  })
  return out.get("action") or {}, out["sm"]


def _dispatched(action):
  calls = action.get("function_calls") or (
      [action["function_call"]] if action.get("function_call") else [])
  return [c["name"] for c in calls]


def _after_tool_would_stand_aside(sm, tool_name):
  """The literal gate in `after_tool`, evaluated against the sm the engine produced.

  Asserting on `_parallel_firing` alone would pin a key name. This pins the consequence
  the key has, which is the thing that actually broke.
  """
  return tool_name in (sm.get("_parallel_firing") or [])


def _after_tool(sm, tool_name, result):
  """Ingest a tool result the way `after_tool` really does — gate included.

  Calling `run_intake` directly is what a test reaches for, and it is wrong here: intake
  does not consult `_parallel_firing`, so a test that bypasses the gate ingests happily
  under the bug and proves nothing. The gate IS the defect, so it has to be in the path.
  """
  if _after_tool_would_stand_aside(sm, tool_name):
    return sm  # stood aside; before_model is expected to pick it up instead
  sm.update(fb.run_intake(tool_name, result, sm)["sm"])
  return sm


def _before_model(sm):
  """The compensating ingestion, reduced to the part that decides a lone task's fate.

  For a batch this hands every landed leg to intake. For a pending payload it maps the
  tool back to a leg name through `sm["_fanout"]["tools"]` — and that map is filled by
  `_fanout_start` alone, which runs for a progressive group. A lone task has no entry, so
  there is nothing to route and the placeholder is dropped. Modelled rather than executed
  (the real callback needs a CES `LlmRequest`), which is why the test above pins the gate
  directly as well.
  """
  by_tool = {v: k for k, v in ((sm.get("_fanout") or {}).get("tools") or {}).items()}
  return [by_tool[t] for t in (sm.get("_parallel_firing") or []) if t in by_tool]


# --------------------------------------------------------------------------- #
# The fix
# --------------------------------------------------------------------------- #

def test_a_lone_awaits_task_is_not_marked_as_a_batch():
  """One leg cannot race itself, so `after_tool` must keep ingesting it."""
  config = _lone()
  action, sm = _drive(config, _sm(config, {"account": "A-1"}))
  assert "run_sweep" in _dispatched(action), "the task never fired; fixture is wrong"
  assert not _after_tool_would_stand_aside(sm, "run_sweep"), (
      "a lone `awaits` task was marked as a parallel batch, so `after_tool` stands "
      "aside and nothing ingests its placeholder — the task then re-fires every pass "
      "to the ten-pass cap (ces-probes 148)")


def test_the_lone_task_still_carries_its_guard():
  """The guard and the batch marker went out together; only the marker was wrong.

  Dropping the guard with it would trade a re-fire for a silently dropped launch
  (ces-probes 126-130), which is harder to see and worse.
  """
  config = _lone()
  action, _ = _drive(config, _sm(config, {"account": "A-1"}))
  assert _dispatched(action) == ["run_sweep", _GUARD]


def test_the_wait_is_recorded_when_the_placeholder_comes_back():
  """The end of the seam: intake now sees the placeholder, so the wait is marked.

  This is the behaviour the marker was denying. Intake records the completion and the
  ENGINE's `awaits` branch turns a pending placeholder into a wait on the next pass, so
  the assertion has to drive that pass — checking straight after intake reads the
  placeholder mid-flight, as a completion with `success: false`.

  `_awaiting_async` is what the selector honours; without it the task reads as un-fired.
  """
  config = _lone()
  _, sm = _drive(config, _sm(config, {"account": "A-1"}))
  sm = _after_tool(sm, "run_sweep", PENDING)
  assert not _before_model(sm), (
      "before_model claims it can route this placeholder; it cannot — only a progressive "
      "group has a `_fanout` map, so nothing would ingest it if `after_tool` stood aside")
  _, sm = _drive(config, sm)
  assert "Sweep" in (sm.get("_awaiting_async") or {}), (
      "the pending placeholder did not start a wait")


def test_the_task_is_not_dispatched_again_while_it_is_awaited():
  """The symptom itself: nine dispatches in one turn, then the cap.

  Drive a second pass with the placeholder ingested and nothing else changed. The task
  must not go out again — that re-dispatch, once per reasoning pass, is what burned the
  turn on the converted agent.
  """
  config = _lone()
  _, sm = _drive(config, _sm(config, {"account": "A-1"}))
  sm = _after_tool(sm, "run_sweep", PENDING)
  action, _ = _drive(config, sm, turn=1)
  assert "run_sweep" not in _dispatched(action), (
      "the awaited task was dispatched a second time on the next pass — this is the "
      "loop that reaches the ten-pass cap")


# --------------------------------------------------------------------------- #
# What must not regress
# --------------------------------------------------------------------------- #

def test_a_real_group_is_still_marked():
  """The case the marker exists for. Two legs DO race, and dropping the marker here
  would resurrect the lost-write defect ces-probes 37 and 38 measured."""
  config = _group()
  action, sm = _drive(config, _sm(config, {"order_id": "A-1042"}))
  fired = [n for n in _dispatched(action) if n != _GUARD]
  assert len(fired) > 1, f"the group did not fan out; fixture is wrong (fired={fired})"
  for tool in fired:
    assert _after_tool_would_stand_aside(sm, tool), (
        f"leg {tool} was left to `after_tool`, which is invoked once per leg with the "
        "legs racing on one state key — that keeps one result and drops the rest")


def test_a_group_with_one_eligible_leg_is_not_marked():
  """The case this change quietly moves, and the one worth pinning.

  A group is a batching hint, not a barrier: a leg gated off by its condition simply is
  not in this pass's set. So a three-leg group can dispatch ONE leg, and that dispatch is
  now left to `after_tool` rather than marked. That is correct — a lone leg races nobody,
  and the marker's whole purpose is arbitrating concurrent writers — but it is a
  different code path than the same group takes when two legs go out together, and
  nothing else in the suite covers it.
  """
  config = _group()
  # Gate the second leg off by withholding the slot its condition reads, and give the
  # first an `awaits` so the dispatch still DEFERS. Without that the single-leg dispatch
  # is plainly synchronous, never reaches the branch under test, and the test passes
  # whatever the branch does — vacuous, and it was, until this line.
  for task in config["tasks"]:
    if task["name"] == _LEGS[1][0]:
      task["condition"] = {"slot": "never_filled", "filled": True}
    if task["name"] == _LEGS[0][0]:
      task["awaits"] = {"max_turns": 8}
  action, sm = _drive(config, _sm(config, {"order_id": "A-1042"}))
  fired = [n for n in _dispatched(action) if n != _GUARD]
  assert len(fired) == 1, f"expected a single eligible leg, got {fired}"
  assert not _after_tool_would_stand_aside(sm, fired[0]), (
      "a group that dispatched ONE leg marked it as a batch; nothing would then ingest "
      "its placeholder, which is the re-fire loop this change exists to remove")


def test_a_synchronous_lone_task_is_untouched():
  """The baseline. No `awaits`, so no deferral, no guard, and no marker either way."""
  config = _lone(awaits=False)
  action, sm = _drive(config, _sm(config, {"account": "A-1"}))
  assert _dispatched(action) == ["run_sweep"]
  assert not _after_tool_would_stand_aside(sm, "run_sweep")
