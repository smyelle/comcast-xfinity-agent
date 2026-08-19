"""The settle guard is fired by the ENGINE, so the agent has to be allowed to call it.

`settle_guard` holds a turn open while a deferred dispatch lands (ces-probes 129/130).
The engine emits it as a `function_call` in the same preempt as the deferred tool — and
CES only lets an agent call a tool that agent lists. Unlisted, the call is dropped; the
engine still sees the task un-fired, re-enters, and the turn burns to the platform's
ten-reasoning-loop cap having said nothing.

Measured on a converted agent before the fix: `slot_filling_engine -> repair_dag` eight
times over, `settle_guard` absent from the trace entirely and the deferred tool never
fired. After it: `settle_guard` runs on every pass.
"""

from __future__ import annotations

import flows
from flows.authoring import build as _build


def _flow():
  f = flows.Flow("checks", root_agent="root_agent")
  f.add(flows.user_slot("go", "Say go."), flows.result_slot("out", "Run"))
  f.task(flows.task("Run", "slow_tool", [], "out", out_key="summary",
                    condition=flows.has("go")))
  return f


def test_the_agent_can_call_the_settle_guard():
  """Without this the guard is emitted as a resource nothing is allowed to invoke."""
  tools = _build.scoped_agent_tools("checks", [_flow().to_config()], [])
  assert "settle_guard" in tools


def test_it_is_scoped_even_when_no_task_defers():
  """Scoping is static and the decision to defer is per-turn — a task can acquire an
  `awaits` policy, or a group can be lowered progressively, long after the tool list was
  written. Cheap to carry, and its absence is a ten-loop crash rather than a warning."""
  f = flows.Flow("plain", root_agent="root_agent")
  f.add(flows.user_slot("name", "Your name?"))
  tools = _build.scoped_agent_tools("plain", [f.to_config()], [])
  assert "settle_guard" in tools


def test_it_reaches_the_emitted_agent():
  """The unit above is the contract; this is the thing that actually ships."""
  import json
  import os
  import tempfile

  app = flows.App(root_flow=_flow(), app_display_name="t",
                  tool_bodies={"slow_tool": "def slow_tool() -> dict:\n"
                                            '    """Run it."""\n'
                                            "    return {'success': True, 'summary': 'x'}\n"})
  out = os.path.join(tempfile.mkdtemp(), "app")
  flows.build_app(app, out)
  with open(os.path.join(out, "agents", "root_agent", "root_agent.json")) as fh:
    assert "settle_guard" in json.load(fh)["tools"]
