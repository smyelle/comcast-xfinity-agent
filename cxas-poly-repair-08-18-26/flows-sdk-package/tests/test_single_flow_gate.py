"""Auto-gate: build well-forms a single-flow app as a self-seeding, re-enterable gate.

A standalone (host-less) flow needs `single_flow` + a `gate_slot` (so the engine
self-seeds it on turn 1) and `bootstrap.reset_on_complete` (so it re-arms after a
terminal task). `build_app` injects these when the author hasn't, while preserving
author bootstrap keys and no-op'ing when the author opted in or wrote a router.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_single_flow_gate.py
"""

from __future__ import annotations

import flows
from flows.authoring import build


def _cfg(app: flows.App) -> dict:
  """The root config as `build` assembles + emits it (auto-gate applied)."""
  all_map, _bodies, _avail = build._assemble(app)  # noqa: SLF001
  return all_map[app.config_id]


def _single_flow_app(**flow_kwargs) -> flows.App:
  f = flows.Flow("collect", root_agent="Agent", **flow_kwargs)
  f.add(flows.user_slot("item", "What item?"))
  return flows.App(root_flow=f, app_display_name="X")


def test_single_flow_app_is_auto_gated():
  cfg = _cfg(_single_flow_app())
  assert cfg["single_flow"] is True
  assert cfg["gate_slot"] == "active_flow"
  assert cfg["bootstrap"]["slot"] == "active_flow"
  assert cfg["bootstrap"]["reset_on_complete"] is True


def test_author_bootstrap_keys_are_preserved():
  cfg = _cfg(_single_flow_app(bootstrap={"welcome_slot": "welcome"}))
  assert cfg["bootstrap"]["welcome_slot"] == "welcome"       # preserved
  assert cfg["bootstrap"]["slot"] == "active_flow"           # gate merged in
  assert cfg["bootstrap"]["reset_on_complete"] is True


def test_author_opt_in_gate_is_not_overridden():
  # An author-set gate_slot means "I manage the gate" — leave it alone.
  cfg = _cfg(_single_flow_app(gate_slot="mine",
                              bootstrap={"slot": "mine", "reset_on_complete": False}))
  assert cfg["gate_slot"] == "mine"
  assert cfg.get("single_flow") is not True                  # untouched
  assert cfg["bootstrap"]["reset_on_complete"] is False      # author's value kept


def test_router_root_is_not_auto_gated():
  cfg = _cfg(_single_flow_app(router=True))
  assert "single_flow" not in cfg or cfg["single_flow"] is not True
  assert cfg.get("gate_slot") is None


def test_multi_flow_root_is_not_auto_gated():
  root = flows.Flow("host", root_agent="Host", router=True)
  root.add(flows.announce("hi", ["Hi"], shared=True))
  child = flows.Flow("child", root_agent="Child")
  child.add(flows.user_slot("item", "What item?"))
  app = flows.App(root_flow=root, extra_flows=[child], app_display_name="X")
  all_map, _b, _a = build._assemble(app)  # noqa: SLF001
  assert all_map["host"].get("gate_slot") is None            # host manages its own gate
