"""Calling an agent as a tool: the contract an author no longer has to know.

An `agentTool` takes `request`, answers `{"response": "<text>"}`, and says nothing about
success. Every one of those is a silent failure when it is wrong — a call with any other
argument name is rejected by the platform, and a task left on the default `success_check`
reads a good answer as failed and escalates on its first fire.

So the tests come in pairs: what the declaration fills in, and what the validator says
when someone wires it by hand instead.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

import flows


def _flow(task_dict, config_id="p"):
  f = flows.Flow(config_id, root_agent="root_agent")
  f.add(flows.user_slot("q", "What would you like to know?"),
        flows.result_slot("a", "Ask"))
  f.task(task_dict)
  return f


def _spec():
  return flows.helper_agent("spec", instruction="Answer in one sentence.")


# --- the declaration -------------------------------------------------------


def test_a_helper_agent_needs_something_to_answer_with():
  with pytest.raises(ValueError, match="nothing to answer with"):
    flows.helper_agent("spec", instruction="  ")


def test_an_agent_tool_needs_a_description_because_the_model_routes_on_it():
  with pytest.raises(ValueError, match="description"):
    flows.agent_tool("ask", agent=_spec(), description="")


def test_a_name_that_cannot_be_a_resource_name_is_refused():
  with pytest.raises(ValueError, match="letters, digits and underscores"):
    flows.agent_tool("ask-specialist", agent=_spec(), description="x")


def test_the_target_may_be_a_declaration_or_a_bare_name():
  """A converted app's agents arrive by graft, after emit, so a name has to be legal."""
  assert flows.agent_tool("a", agent=_spec(), description="x").agent == "spec"
  assert flows.agent_tool("b", agent="grafted_one", description="x").agent == "grafted_one"


# --- what task() fills in --------------------------------------------------


def test_the_wire_contract_comes_from_the_declaration():
  ask = flows.agent_tool("ask", agent=_spec(), description="Answers things.")
  t = _flow(flows.task("Ask", ask, ["q"], "a")).to_config()["tasks"][0]
  assert t["inputs"] == {"q": "request"}
  assert t["outputs"] == {"response": "a"}
  assert t["success_check"] == "response"


def test_each_default_is_overridable():
  """Defaults, not a lock: one measured data point should not become a straitjacket."""
  ask = flows.agent_tool("ask", agent=_spec(), description="Answers things.")
  t = _flow(flows.task("Ask", ask, ["q"], "a",
                       out_key="other", success_check="other")).to_config()["tasks"][0]
  assert t["outputs"] == {"other": "a"}
  assert t["success_check"] == "other"


def test_two_inputs_are_refused_at_authoring_time():
  ask = flows.agent_tool("ask", agent=_spec(), description="Answers things.")
  with pytest.raises(ValueError, match="takes exactly one"):
    flows.task("Ask", ask, ["q", "a"], "a")


def test_the_declaration_does_not_leak_into_the_emitted_config():
  ask = flows.agent_tool("ask", agent=_spec(), description="Answers things.")
  t = _flow(flows.task("Ask", ask, ["q"], "a")).to_config()["tasks"][0]
  assert not [k for k in t if k.startswith("_")], t


# --- what the validator catches when it is wired by hand -------------------


def _validate(task_dict, tool, spec=None, agents=None):
  app = flows.App(root_flow=_flow(task_dict), app_display_name="t",
                  agents=list(agents if agents is not None else [spec or _spec()]),
                  agent_tools=[tool])
  return flows.validate_app(app)


def test_handing_task_the_declaration_validates_clean():
  ask = flows.agent_tool("ask", agent=_spec(), description="Answers things.")
  errors, _ = _validate(flows.task("Ask", ask, ["q"], "a"), ask)
  assert errors == []


def test_the_wrong_argument_name_is_an_error_not_a_platform_rejection():
  ask = flows.agent_tool("ask", agent=_spec(), description="Answers things.")
  errors, _ = _validate(
      flows.task("Ask", "ask", {"q": "task"}, "a", out_key="response",
                 success_check="response"), ask)
  assert any("takes exactly one argument" in e for e in errors), errors


def test_the_default_success_check_is_an_error():
  """It would read every answer as failed and escalate on the first fire."""
  ask = flows.agent_tool("ask", agent=_spec(), description="Answers things.")
  errors, _ = _validate(flows.task("Ask", "ask", {"q": "request"}, "a"), ask)
  assert any("says nothing about success" in e for e in errors), errors


def test_an_output_key_the_agent_never_returns_is_an_error():
  ask = flows.agent_tool("ask", agent=_spec(), description="Answers things.")
  errors, _ = _validate(
      flows.task("Ask", "ask", {"q": "request"}, "a", out_key="summary",
                 success_check="response"), ask)
  assert any("intake maps by" in e for e in errors), errors


def test_an_unresolvable_agent_is_a_warning_not_an_error():
  """A converted app grafts its agents in after emit, so this cannot be fatal here."""
  ask = flows.agent_tool("ask", agent="arrives_by_graft", description="Answers things.")
  errors, warnings = _validate(flows.task("Ask", ask, ["q"], "a"), ask, agents=[])
  assert errors == []
  assert any("does not declare" in w for w in warnings), warnings


# --- emit ------------------------------------------------------------------


def _emit(app):
  out = tempfile.mkdtemp()
  flows.build_app(app, os.path.join(out, "app"))
  return os.path.join(out, "app")


def _app(asynchronous=False, emit=True):
  spec = _spec()
  ask = flows.agent_tool("ask", agent=spec, description="Answers things.",
                         asynchronous=asynchronous, emit=emit)
  return flows.App(root_flow=_flow(flows.task("Ask", ask, ["q"], "a")),
                   app_display_name="t", agents=[spec], agent_tools=[ask])


def test_it_emits_a_bodyless_agent_tool_resource():
  out = _emit(_app())
  with open(os.path.join(out, "tools", "ask", "ask.json")) as fh:
    resource = json.load(fh)
  assert resource["agentTool"] == {"name": "ask", "description": "Answers things.",
                                   "agent": "spec"}
  assert resource["executionType"] == "SYNCHRONOUS"
  assert not os.path.exists(os.path.join(out, "tools", "ask", "python_function"))


def test_asynchronous_reaches_the_resource():
  """The whole reason to prefer this flavour: an async remoteAgentTool is dropped at
  deploy (ces-probes 133) and this one defers and completes (134)."""
  out = _emit(_app(asynchronous=True))
  with open(os.path.join(out, "tools", "ask", "ask.json")) as fh:
    assert json.load(fh)["executionType"] == "ASYNCHRONOUS"


def test_the_helper_agent_is_emitted_with_its_instruction():
  out = _emit(_app())
  with open(os.path.join(out, "agents", "spec", "spec.json")) as fh:
    assert json.load(fh)["instruction"] == "agents/spec/instruction.txt"
  with open(os.path.join(out, "agents", "spec", "instruction.txt")) as fh:
    assert "one sentence" in fh.read()


def test_the_tool_is_scoped_onto_the_agent_that_must_call_it():
  out = _emit(_app())
  with open(os.path.join(out, "agents", "root_agent", "root_agent.json")) as fh:
    assert "ask" in json.load(fh)["tools"]


def test_a_carried_tool_is_declared_but_never_written():
  """`emit=False` is for a resource the app already has — a converted agent's grafted
  specialist, which arrives with its own toolFakeConfig. Writing one would take the
  mocked path with it."""
  out = _emit(_app(emit=False))
  assert not os.path.exists(os.path.join(out, "tools", "ask"))


def test_a_carried_tool_still_counts_as_available():
  """It is real, so a task firing it must not read as a dangling reference."""
  errors, _ = flows.validate_app(_app(emit=False))
  assert errors == []


# --- the push gate ---------------------------------------------------------


def test_the_push_gate_counts_bodyless_tools():
  """It derived ground truth from python bodies alone, so every body-less tool looked
  absent and a config firing one failed the strict gate. That already affected A2A."""
  from flows.deploy import gates
  seen = gates._available_tools_from_files({
      "tools/lookup/python_function/python_code.py": "def lookup(): ...",
      "tools/ask/ask.json": '{"agentTool": {"agent": "spec"}}',
      "tools/a2a/a2a.json": '{"remoteAgentTool": {}}',
      "tools/searcher/searcher.json": '{"googleSearchTool": {}}',
  })
  assert {"ask", "a2a", "searcher", "lookup"} <= set(seen)
