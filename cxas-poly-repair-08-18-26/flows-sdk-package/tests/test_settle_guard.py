"""A dispatch that DEFERS carries a synchronous guard to hold the turn open.

A deferred (`executionType: ASYNCHRONOUS`) call is launched by the turn that dispatches
it. When that turn ends immediately afterwards, two things go missing silently and at a
rate: the launch itself, and any `context.state` write made by a tool the deferred body
calls nested through `tools`. Measured live in ces-probes 122-129:

  no guard, one leg per pass      15/18 legs launched,  7/18 nested writes survived
  no guard, multi-call preempt    10/18 legs launched,  2/18 nested writes survived
  guard, inline in that preempt   18/18 legs launched, 18/18 nested writes survived

The multi-call shape is the one `flows.parallel` emits, and it is the WORSE of the two
unguarded — one run launched a single leg of six. So the guard rides in the same preempt
as the calls it guards, which also costs no extra model pass: a wide fan-out cannot spare
one against the ten-pass cap.

What these pin is the SHAPE — that the guard goes out with deferred dispatches and stays
away from synchronous ones. The durations and rates above are live findings and belong in
the probes; they cannot be observed offline.
"""

from __future__ import annotations

import flows
from flows.engine import loader

_GUARD = "settle_guard"


def _dispatched(action: dict) -> list[str]:
  calls = action.get("function_calls") or (
      [action["function_call"]] if action.get("function_call") else [])
  return [c["name"] for c in calls]


def _fire(flow, filled=None):
  config = flow.to_config()
  sm = {"filled": dict(filled or {}), "task_results": {}}
  out = loader.run_engine(config, sm, last_user_text="go", config_id=flow.config_id)
  return out.get("action") or {}


def _flow_with(task):
  f = flows.Flow("probe", root_agent="Agent")
  f.add(flows.user_slot("account", ask="Account?"), flows.result_slot("done", "Work"))
  f.task(task)
  return f


def test_a_synchronous_task_dispatches_no_guard():
  """The baseline, and the one that must not regress: a synchronous execution reports
  inside its own turn and was never once observed to lose a write, so guarding it would
  buy nothing and cost half a second on every ordinary dispatch."""
  action = _fire(_flow_with(
      flows.task("Work", "do_work", ["account"], "done", out_key="success")),
      {"account": "A-1"})
  assert _dispatched(action) == ["do_work"]
  assert action.get("function_calls") is None, (
      "a synchronous dispatch grew a `function_calls` list it did not have before")


def test_a_task_that_awaits_carries_the_guard():
  """`awaits` is the author saying this tool is asynchronous, so the dispatch defers."""
  action = _fire(_flow_with(
      flows.task("Work", "do_work", ["account"], "done", out_key="success",
                 awaits=flows.awaits(max_turns=8, say="One moment."))),
      {"account": "A-1"})
  assert _dispatched(action) == ["do_work", _GUARD], (
      "a deferred dispatch went out unguarded; ces-probes 129 measured that shape "
      "landing 10 of 18 legs and 2 of 18 nested writes")


def test_the_guard_is_dispatched_last():
  """Order is not cosmetic: the guard exists to outlive the calls beside it, so they
  have to be launched before it starts spending its half second."""
  action = _fire(_flow_with(
      flows.task("Work", "do_work", ["account"], "done", out_key="success",
                 awaits=flows.awaits(max_turns=8, say="One moment."))),
      {"account": "A-1"})
  assert _dispatched(action)[-1] == _GUARD


def test_a_progressive_group_carries_the_guard():
  """The case most likely to be missed: a progressive group's legs are lowered to
  asynchronous wrappers, so the author never writes `awaits` for them, and this is
  exactly the multi-call shape 129 measured at 2 of 18 nested writes surviving."""
  f = flows.Flow("probe", root_agent="Agent")
  f.add(flows.user_slot("account", ask="Account?"),
        flows.result_slot("a", "LegA"), flows.result_slot("b", "LegB"))
  f.task(flows.parallel(
      "Checks",
      tasks=[flows.task("LegA", "check_a", ["account"], "a", out_key="success"),
             flows.task("LegB", "check_b", ["account"], "b", out_key="success")]))
  fired = _dispatched(_fire(f, {"account": "A-1"}))
  assert fired[-1] == _GUARD, f"progressive group dispatched unguarded: {fired}"
  assert {"check_a", "check_b"} <= set(fired)


def test_a_batch_group_carries_no_guard():
  """`progressive=False` keeps the legs SYNCHRONOUS — CES runs them concurrently and
  hands the whole batch back on the same pass. Nothing defers, so nothing needs holding
  open, and the guard would be half a second spent for no reason."""
  f = flows.Flow("probe", root_agent="Agent")
  f.add(flows.user_slot("account", ask="Account?"),
        flows.result_slot("a", "LegA"), flows.result_slot("b", "LegB"))
  f.task(flows.parallel(
      "Checks", progressive=False,
      tasks=[flows.task("LegA", "check_a", ["account"], "a", out_key="success"),
             flows.task("LegB", "check_b", ["account"], "b", out_key="success")]))
  fired = _dispatched(_fire(f, {"account": "A-1"}))
  assert _GUARD not in fired, f"a synchronous batch was guarded: {fired}"


def test_the_guard_is_hidden_from_the_model():
  """It is the framework's to dispatch, never the model's to choose. Offered, it is a
  plausible-looking no-op that a model with nothing better to do will call — the
  unadvertised-tool lure. Pinned against the blessed consumer rather than a copy of it."""
  import os
  consumer = os.path.join(
      os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
      "src", "flows", "engine", "framework", "callbacks", "before_model.py")
  with open(consumer) as fh:
    assert '"settle_guard"' in fh.read()
