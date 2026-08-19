"""Single-agent router-over-flows runtime wiring.

A router root emits every flow's DAG onto ONE agent, so config switching and tool scoping
have to happen per-turn via state vars rather than statically. These pin that wiring —
especially `flow_config_map`, without which the blessed before_agent resolver never
switches off the router config and no child DAG ever drives.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

import flows


def _router_app():
  """A router over one SILENT flow (no setter) and one ordinary collection flow."""
  silent = flows.Flow("diagnose", bootstrap={"reset_on_complete": True})
  silent.add(flows.result_slot("line_status", "RunDiag"))
  silent.task("RunDiag", "run_diagnostics", [], "line_status", terminal=True,
              then_say="Checks are done.")
  collect = flows.Flow("billing", bootstrap={"reset_on_complete": True})
  collect.add(flows.user_slot("account_number", ask="Account number?"),
              flows.result_slot("amount_due", "LookupBill"))
  collect.task("LookupBill", "lookup_bill", ["account_number"], "amount_due",
               terminal=True, then_say="Found it.")
  router = flows.router_flow("host", ["diagnose", "billing"], default_flow="diagnose",
                             root_agent="Host_Agent")
  return flows.App(root_flow=router, extra_flows=[silent, collect],
                   app_display_name="rt")


def _emitted_vars(app) -> dict[str, str]:
  with tempfile.TemporaryDirectory() as d:
    flows.build_app(app, d)
    with open(os.path.join(d, "app.json")) as f:
      decls = json.load(f)["variableDeclarations"]
  return {v["name"]: v["schema"]["default"] for v in decls}


def test_router_emits_flow_config_map():
  """Without this the resolver's active-flow branch never fires and the router config
  stays pinned for the whole call."""
  v = _emitted_vars(_router_app())
  assert json.loads(v["flow_config_map"]) == {"diagnose": "diagnose",
                                              "billing": "billing"}


def test_default_flow_is_emitted_as_state_var_not_only_config_key():
  """before_agent seeds the gate from the STATE VAR; the engine reads the bootstrap key.
  Both must be present or home-base activation silently does nothing."""
  app = _router_app()
  v = _emitted_vars(app)
  assert v["default_flow"] == "diagnose"
  assert app.root_flow.to_config()["bootstrap"]["default_flow"] == "diagnose"


def test_silent_flow_is_inferred_from_absence_of_setters():
  """`diagnose` has only task-written slots, `billing` has a user slot with a setter."""
  v = _emitted_vars(_router_app())
  assert json.loads(v["silent_flow_configs"]) == ["diagnose"]


def test_router_hide_tools_excludes_framework_and_the_routing_tool():
  """The model's only move on a router turn must be to route."""
  v = _emitted_vars(_router_app())
  hide = json.loads(v["router_hide_tools"])
  assert "set_active_flow" not in hide
  assert "slot_filling_engine" not in hide
  assert "end_session" not in hide
  assert {"run_diagnostics", "lookup_bill", "set_account_number"} <= set(hide)


def test_bootstrap_tool_is_generated_and_on_the_agent():
  """`set_active_flow` is named only in `bootstrap` and is not a blessed framework tool,
  so it has to be generated from the gate slot it fills — otherwise the agent ships
  unable to route at all."""
  with tempfile.TemporaryDirectory() as d:
    flows.build_app(_router_app(), d)
    assert os.path.isdir(os.path.join(d, "tools", "set_active_flow"))
    with open(os.path.join(d, "agents", "Host_Agent", "Host_Agent.json")) as f:
      assert "set_active_flow" in json.load(f)["tools"]


def test_gate_slot_stays_undeclared():
  """A bootstrap tool fills its own gate; declaring the gate in `slots` would make the
  engine try to COLLECT it (validator `_check_bootstrap` calls the undeclared gate the
  expected pattern)."""
  cfg = _router_app().root_flow.to_config()
  assert cfg["gate_slot"] == "active_flow"
  assert not [s for s in cfg.get("slots", []) if s.get("name") == "active_flow"]


def test_default_flow_must_be_routable():
  with pytest.raises(ValueError, match="not one of flows"):
    flows.router_flow("host", ["a", "b"], default_flow="nope")


def test_non_router_app_emits_no_routing_vars():
  """Byte-identical for every existing SDK app."""
  f = flows.Flow("solo", root_agent="Solo")
  f.add(flows.user_slot("name", ask="Your name?"),
        flows.result_slot("ok", "Save"))
  f.task("Save", "save_it", ["name"], "ok", terminal=True, then_say="Saved.")
  v = _emitted_vars(flows.App(root_flow=f, app_display_name="solo"))
  for name in ("flow_config_map", "default_flow", "silent_flow_configs",
               "router_hide_tools"):
    assert name not in v
