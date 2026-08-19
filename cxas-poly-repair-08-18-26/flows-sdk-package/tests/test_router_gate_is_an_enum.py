"""A router's gate accepts only its own flow ids.

`flow_types` is a closed set, so the bootstrap tool that fills the gate is an enum
setter. It used to be the plain value-recording setter, which stores any non-empty
string — and for a GATE that is not cosmetic. The gate reads as filled, so the router
considers itself done; the value maps to no config, so `flow_config_map` misses and no
child DAG ever drives. The call runs to its end with the model improvising and nothing
logged.

Measured live on a two-flow router with no `route_cues`: the model answered
`set_active_flow(flow="Troubleshoot Internet")` — a label it invented rather than the
`triage` key — and every later turn fired no tool at all, ending in a transfer to a
human. The identical flow as a standalone app was correct throughout.

Rejecting produces the `not_in_enum` error the No-Match ladder already handles, so the
gate stays unfilled and the route / default backstops get their turn.
"""

from __future__ import annotations

import os
import tempfile

import pytest

import flows


def _router_app(**router_kwargs):
  triage = flows.Flow("triage", bootstrap={"reset_on_complete": True})
  triage.add(flows.user_slot("scope", ask="Everything, or one app?"),
             flows.result_slot("advice", "Advise"))
  triage.task("Advise", "give_advice", ["scope"], "advice", terminal=True,
              then_say="Understood.")
  billing = flows.Flow("billing", bootstrap={"reset_on_complete": True})
  billing.add(flows.user_slot("account_number", ask="Account number?"),
              flows.result_slot("amount", "LookupBill"))
  billing.task("LookupBill", "lookup_bill", ["account_number"], "amount",
               terminal=True, then_say="Found it.")
  router = flows.router_flow("host", ["triage", "billing"], root_agent="Host_Agent",
                             **router_kwargs)
  return flows.App(root_flow=router, extra_flows=[triage, billing],
                   app_display_name="gate")


def _setter_source(app, tool="set_active_flow") -> str:
  with tempfile.TemporaryDirectory() as d:
    flows.build_app(app, d)
    path = os.path.join(d, "tools", tool, "python_function", "python_code.py")
    with open(path) as fh:
      return fh.read()


def _call(src, tool="set_active_flow"):
  ns: dict = {}
  exec(compile(src, "<generated>", "exec"), ns)  # noqa: S102 — the emitted body IS the contract
  return ns[tool]


def test_the_gate_setter_rejects_a_flow_id_it_does_not_have():
  """The defect: any non-empty string was stored, and the call was lost silently."""
  set_active_flow = _call(_setter_source(_router_app()))

  bad = set_active_flow(flow="Troubleshoot Internet")
  assert bad.get("error") is True, (
      "an invented label filled the gate, so the router looked satisfied while "
      f"flow_config_map missed and no child DAG ever drove; got {bad}")
  assert bad.get("error_code") == "not_in_enum"


@pytest.mark.parametrize("value", ["triage", "billing"])
def test_a_real_flow_id_still_stores(value):
  set_active_flow = _call(_setter_source(_router_app()))
  assert set_active_flow(flow=value) == {"stored": True, "value": value}


def test_the_id_is_matched_case_insensitively():
  """The engine's own backstops pass the id verbatim, but the MODEL types it."""
  set_active_flow = _call(_setter_source(_router_app()))
  assert set_active_flow(flow="Triage") == {"stored": True, "value": "triage"}


def test_an_empty_value_is_still_missing_not_not_in_enum():
  """`missing` and `not_in_enum` drive different ladder rungs; keep them distinct."""
  set_active_flow = _call(_setter_source(_router_app()))
  assert set_active_flow(flow="")["error_code"] == "missing"


def test_the_valid_ids_are_in_the_model_facing_docstring():
  """The docstring is the tool's schema. Naming the ids is what stops the model
  inventing one in the first place, rather than only catching it afterwards."""
  src = _setter_source(_router_app())
  assert "One of triage, billing" in src


def test_a_non_router_bootstrap_is_unchanged():
  """Only a router has a closed set of ids. An ordinary gate takes free text, and
  narrowing it would reject perfectly good values."""
  flow = flows.Flow("intake", bootstrap={"tool": "set_topic", "slot": "topic"},
                    gate_slot="topic")
  flow.add(flows.user_slot("detail", ask="Tell me more."))
  app = flows.App(root_flow=flow, app_display_name="intake")
  set_topic = _call(_setter_source(app, tool="set_topic"), tool="set_topic")
  assert set_topic(topic="anything at all") == {"stored": True,
                                                "value": "anything at all"}
