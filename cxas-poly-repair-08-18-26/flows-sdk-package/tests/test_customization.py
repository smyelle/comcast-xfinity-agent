"""Author customization: steering hook + raw lifecycle hooks.

The hook functions are defined at MODULE scope so `inspect.getsource` can render
them (same rule as `@flows.tool`). Covers single-agent + multi-agent host emission,
callback file placement (_00 before / _02 after the framework _01), and JSON
registration order.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_customization.py
"""

from __future__ import annotations

import json
import os

import pytest

import flows


# --- module-scope author callbacks (so getsource works) ----------------------
def steer(state) -> "str | None":
  """Route enterprise shippers straight to pickup, else defer."""
  if state.get("caller_segment") == "enterprise":
    return "pickup"
  return None


def steer_bad_arity(state, extra):
  """A steering hook with the wrong number of parameters."""
  return None


def steer_with_default(state, opt="x"):
  """A valid steering hook: callable with 1 arg despite a second defaulted param."""
  return None


def my_before_model(callback_context, llm_request):
  """A raw before_model override."""
  return {"decision": "OK"}


def after_model_callback(callback_context, llm_response):
  """A raw after_model override (already named the CES entry point)."""
  return None


def _solo(name: str = "Solo_Agent") -> flows.Flow:
  f = flows.Flow("solo", root_agent=name)
  f.add(flows.user_slot("x", "x?"), flows.announce("d", ["ok"], end=True))
  return f


def _agent(name: str, cid: str) -> flows.Agent:
  f = flows.Flow(cid, root_agent=name)
  f.add(flows.user_slot(f"{cid}_v", "v?"), flows.announce("d", ["ok"], end=True))
  return flows.Agent(name, flow=f)


def _read(out: str, *parts: str) -> str:
  return open(os.path.join(out, *parts)).read()


def _json(out: str, *parts: str) -> dict:
  return json.loads(_read(out, *parts))


# --- steering (single agent) -------------------------------------------------
def test_steering_emits_before_agent_00(tmp_path):
  app = flows.App(root_flow=_solo(), app_display_name="Steered", steering=steer)
  out = str(tmp_path / "app")
  res = flows.build_app(app, out)
  assert res.ok, res.error

  path = os.path.join(out, "agents", "Solo_Agent", "before_agent_callbacks",
                      "before_agent_callbacks_00", "python_code.py")
  assert os.path.isfile(path)
  src = open(path).read()
  assert "def steer(state)" in src
  assert "def before_agent_callback(callback_context)" in src
  assert '_active_config_id' in src

  aj = _json(out, "agents", "Solo_Agent", "Solo_Agent.json")
  dirs = [c["pythonCode"].split("/")[-2] for c in aj["beforeAgentCallbacks"]]
  # author _00 runs BEFORE the framework _01.
  assert dirs == ["before_agent_callbacks_00", "before_agent_callbacks_01"]


# --- steering on a multi-agent host -----------------------------------------
def test_steering_on_multi_agent_host(tmp_path):
  a = _agent("Tracking_Agent", "tracking")
  b = _agent("Pickup_Agent", "pickup")
  host = flows.HostRouter("Host", routes={"tracking": a, "pickup": b},
                          strategy="engine", steering=steer)
  app = flows.App(host=host, agents=[a, b], app_display_name="Steered MA")
  out = str(tmp_path / "app")
  res = flows.build_app(app, out)
  assert res.ok, res.error
  assert os.path.isfile(os.path.join(
      out, "agents", "Host", "before_agent_callbacks",
      "before_agent_callbacks_00", "python_code.py"))
  aj = _json(out, "agents", "Host", "Host.json")
  assert aj["beforeAgentCallbacks"][0]["pythonCode"].endswith(
      "before_agent_callbacks_00/python_code.py")


# --- raw lifecycle hooks -----------------------------------------------------
def test_raw_hooks_bracket_the_framework(tmp_path):
  hooks = flows.AgentHooks(before_model=my_before_model,
                           after_model=after_model_callback)
  app = flows.App(root_flow=_solo(), app_display_name="Hooked", hooks=hooks)
  out = str(tmp_path / "app")
  res = flows.build_app(app, out)
  assert res.ok, res.error

  # before_model author file at _00 (before framework _01); aliased to the entry.
  bm = _read(out, "agents", "Solo_Agent", "before_model_callbacks",
             "before_model_callbacks_00", "python_code.py")
  assert "before_model_callback = my_before_model" in bm
  # after_model author file at _02 (after framework _01); already correctly named.
  am_path = os.path.join(out, "agents", "Solo_Agent", "after_model_callbacks",
                         "after_model_callbacks_02", "python_code.py")
  assert os.path.isfile(am_path)

  aj = _json(out, "agents", "Solo_Agent", "Solo_Agent.json")
  bm_dirs = [c["pythonCode"].split("/")[-2] for c in aj["beforeModelCallbacks"]]
  am_dirs = [c["pythonCode"].split("/")[-2] for c in aj["afterModelCallbacks"]]
  assert bm_dirs == ["before_model_callbacks_00", "before_model_callbacks_01"]
  assert am_dirs == ["after_model_callbacks_01", "after_model_callbacks_02"]


def test_steering_logs_exceptions_not_silent(tmp_path):
  app = flows.App(root_flow=_solo(), app_display_name="Steered", steering=steer)
  out = str(tmp_path / "app")
  flows.build_app(app, out)
  src = _read(out, "agents", "Solo_Agent", "before_agent_callbacks",
             "before_agent_callbacks_00", "python_code.py")
  assert "import logging" in src
  assert "_logger.exception(" in src


def test_wrong_arity_steering_rejected(tmp_path):
  app = flows.App(root_flow=_solo(), app_display_name="BadSteer",
                  steering=steer_bad_arity)
  with pytest.raises(ValueError, match="1 positional"):
    flows.build_app(app, str(tmp_path / "app"))


def test_steering_with_default_arg_accepted(tmp_path):
  app = flows.App(root_flow=_solo(), app_display_name="DefaultSteer",
                  steering=steer_with_default)
  res = flows.build_app(app, str(tmp_path / "app"))
  assert res.ok


def test_steering_and_hook_before_agent_conflict_raises(tmp_path):
  hooks = flows.AgentHooks(before_agent=steer)  # collides with steering _00 slot
  app = flows.App(root_flow=_solo(), app_display_name="Conflict",
                  steering=steer, hooks=hooks)
  with pytest.raises(ValueError, match="before_agent"):
    flows.build_app(app, str(tmp_path / "app"))
