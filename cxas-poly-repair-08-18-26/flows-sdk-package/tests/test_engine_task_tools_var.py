"""Every task executor in the app is emitted as `engine_task_tools`.

A task's `tool` is dispatched BY THE ENGINE with the task's inputs. The model must never
call it: a stray empty model call satisfies the task with no outputs and strands the flow.
CES registers every tool on the agent as model-callable, and the engine's per-turn hide
covers only the ACTIVE config's tasks — so in a single-agent multi-flow app every OTHER
flow's tasks leak, and in any app every COMPONENT's tasks do.

The blessed `before_model` has hidden this set on every turn for some time. It was reading
a variable that only `slotfill_migration` emitted, so a MIGRATED app had its executors
hidden and a hand-authored SDK app silently did not — the consumer sitting in the engine
that every app runs, the producer in one product.

Found while adding an asynchronous task to a hand-authored agent: the new executor showed
up on the agent's tool list, and `engine_task_tools` came back empty.
"""

from __future__ import annotations

import json
import os
import tempfile

import flows


def _vars_of(app) -> dict[str, str]:
  with tempfile.TemporaryDirectory() as d:
    flows.build_app(app, d)
    with open(os.path.join(d, "app.json")) as fh:
      decls = json.load(fh).get("variableDeclarations") or []
  return {v["name"]: (v.get("schema") or {}).get("default") for v in decls}


def _task_tools(app) -> list[str]:
  raw = _vars_of(app).get("engine_task_tools")
  return json.loads(raw) if raw else []


def _billing_flow(config_id="billing"):
  f = flows.Flow(config_id, root_agent="Agent",
                 bootstrap={"reset_on_complete": True})
  f.add(flows.user_slot("account", ask="Account?"),
        flows.result_slot("amount", "LookupBill"))
  f.task("LookupBill", "lookup_bill", ["account"], "amount", terminal=True,
         then_say="Found it.")
  return f


def test_a_single_flow_emits_its_executor():
  app = flows.App(root_flow=_billing_flow(), app_display_name="one")
  assert _task_tools(app) == ["lookup_bill"]


def test_every_extra_flow_contributes_too():
  """The leak this closes: only the ACTIVE config's tasks are hidden per-turn, so a
  sibling flow's executor was callable from inside another flow."""
  support = flows.Flow("support", root_agent="Agent",
                       bootstrap={"reset_on_complete": True})
  support.add(flows.result_slot("diag", "RunDiag"))
  support.task("RunDiag", "run_diagnostics", [], "diag", terminal=True, then_say="Done.")
  router = flows.router_flow("host", ["billing", "support"], root_agent="Agent")
  app = flows.App(root_flow=router, extra_flows=[_billing_flow(), support],
                  app_display_name="two")

  tools = _task_tools(app)
  assert "lookup_bill" in tools
  assert "run_diagnostics" in tools, (
      "a sibling flow's executor was left model-callable; that is the leak "
      f"engine_task_tools exists to close. got={tools}")


def test_an_app_with_no_tasks_emits_no_variable():
  """No variable rather than an empty one, so an app that never had it is byte-identical."""
  f = flows.Flow("chat", root_agent="Agent")
  f.add(flows.user_slot("topic", ask="What's up?"))
  app = flows.App(root_flow=f, app_display_name="none")
  assert "engine_task_tools" not in _vars_of(app)


def test_the_list_is_sorted_and_deduplicated():
  """Two tasks may share an executor. A stable, deduplicated list keeps the emitted app
  byte-identical across rebuilds, which is what makes a diff meaningful."""
  f = flows.Flow("dual", root_agent="Agent")
  f.add(flows.user_slot("account", ask="Account?"),
        flows.result_slot("a", "First"), flows.result_slot("b", "Second"))
  f.task("First", "shared_tool", ["account"], "a")
  f.task("Second", "shared_tool", ["account"], "b", terminal=True, then_say="Done.")
  assert _task_tools(flows.App(root_flow=f, app_display_name="dual")) == ["shared_tool"]


def test_the_variable_is_what_before_model_reads():
  """Pin the NAME against the blessed consumer, not against a copy of it. The two halves
  living in different layers is exactly how they drifted apart in the first place."""
  consumer = os.path.join(
      os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
      "src", "flows", "engine", "framework", "callbacks", "before_model.py")
  with open(consumer) as fh:
    assert 'state.get("engine_task_tools")' in fh.read()
