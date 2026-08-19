"""Every flow's `{config_id}_dag` loader is hidden from the model, not just the active one.

A multi-flow app (a router plus `extra_flows`) declares one agent carrying a `_dag`
config loader for EVERY flow. The engine hid only the active config's, so on any turn
after the routing turn the model could still call a SIBLING flow's loader and enter a
flow the router had not chosen.

`router_hide_tools` covers these, but only on the router turn — which is why the routing
decision itself was never the symptom. Observed on a repair agent driven over voice:
having correctly routed to the repair flow, the model called `reboot_dag` and announced
it had restarted the caller's gateway on a healthy account, and on another run called
`technical_phone_dag` and deflected an internet fault to the phone queue. 2 of 3 spoken
runs, 0 of 3 in text.

The loaders are pure config fetches the engine makes through the app registry
(`getattr(tools, f"{cid}_dag")({})`), never through the agent's tool list, so hiding them
from the model does not affect dispatch.
"""

from __future__ import annotations

import json
import os

import pytest

_CALLBACK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "flows", "engine", "framework", "callbacks", "before_model.py")


class _Config:
  """Records hide_tool() calls, which is the whole observable behaviour here."""

  def __init__(self):
    self.hidden: list[str] = []

  def hide_tool(self, name):
    self.hidden.append(name)


class _Request:
  def __init__(self):
    self.config = _Config()
    self.contents = []


class _Context:
  def __init__(self, state):
    self.state = state


def _hide_internal_tools():
  """The live callback function, exec'd from the source CES renders verbatim.

  It cannot be imported: the module resolves CES-injected globals (CallbackContext,
  LlmRequest, Part, ...) at def time, and they exist only inside the deployed sandbox.
  Exec'ing the real file — rather than restating the logic — is what makes this a test
  of the shipped callback instead of a lookalike.
  """
  with open(_CALLBACK) as fh:
    src = fh.read()
  ns: dict = {name: object for name in
              ("CallbackContext", "LlmRequest", "LlmResponse", "Part", "Content",
               "FunctionCall", "FunctionResponse", "types", "tools")}
  ns["__name__"] = "_before_model_under_test"
  exec(compile(src, _CALLBACK, "exec"), ns)  # noqa: S102 — the shipped source IS the contract
  return ns["_hide_internal_tools"]


@pytest.fixture(name="hide")
def _hide_fixture():
  return _hide_internal_tools()


def test_sibling_flow_dag_loaders_are_hidden(hide):
  """The defect: only the ACTIVE config's loader was hidden."""
  request = _Request()
  hide(_Context({
      "_active_config_id": "repair",
      "flow_config_map": json.dumps({"repair": "repair",
                                     "reboot": "reboot",
                                     "technical_phone": "technical_phone"}),
  }), request)

  assert "reboot_dag" in request.config.hidden, (
      "a sibling flow's loader stayed callable, which is how the model entered a flow "
      f"the router never chose; hidden={request.config.hidden}")
  assert "technical_phone_dag" in request.config.hidden
  assert "repair_dag" in request.config.hidden, "the active config's loader too"


def test_framework_internals_are_still_hidden(hide):
  """The pre-existing hides must survive the change."""
  request = _Request()
  hide(_Context({"_active_config_id": "repair"}), request)
  for name in ("slot_filling_engine", "slot_intake", "transfer_to_agent", "settle_guard",
               "repair_dag", "evaluate_conditions"):
    assert name in request.config.hidden, name


def test_no_flow_config_map_is_a_no_op(hide):
  """A single-flow (non-router) app must be byte-identical to before."""
  request = _Request()
  hide(_Context({"_active_config_id": "billing"}), request)
  assert sorted(request.config.hidden) == sorted(
      ["slot_filling_engine", "slot_intake", "transfer_to_agent", "settle_guard",
       "billing_dag", "evaluate_conditions"])


@pytest.mark.parametrize("fmap", ["not json at all", "[]", "null", ""])
def test_a_malformed_flow_config_map_never_raises(hide, fmap):
  """This runs on every model call, including the crash fallback, so it must degrade
  to fewer hides rather than take the turn down with it."""
  request = _Request()
  hide(_Context({"_active_config_id": "repair", "flow_config_map": fmap}), request)
  assert "repair_dag" in request.config.hidden
  assert "slot_filling_engine" in request.config.hidden


def test_loaders_are_hidden_before_the_config_resolves(hide):
  """The case that actually bites, and the reason the fix keys off flow_config_map.

  The old hide was `if _cid: hide += [f"{_cid}_dag", ...]` — so with no
  `_active_config_id` in state it hid the three statics and NOTHING else, leaving
  every loader in a multi-flow app visible at once.

  That is not hypothetical: on voice, `before_agent` runs before the transport
  attaches the caller's utterance, so the first in-flow turn can reach the model with
  the config unresolved. It is the turn where the model has no rung to run and is
  most likely to reach for a tool — with, until now, the whole flow surface in front
  of it. `flow_config_map` is app-level state that does not depend on resolution,
  which is why keying off it closes the window rather than narrowing it.
  """
  request = _Request()
  hide(_Context({"flow_config_map": json.dumps({"repair": "repair",
                                                "reboot": "reboot"})}), request)
  assert "reboot_dag" in request.config.hidden, (
      "with the config unresolved the old code hid no loaders at all; "
      f"hidden={request.config.hidden}")
  assert "repair_dag" in request.config.hidden


def test_the_routers_own_loader_is_hidden_too(hide):
  """`flow_config_map` holds the routable CHILDREN, never the router's own config, so
  hiding the map's values alone left the router's loader callable from inside a child.

  It is the one that matters most: the observed leak entered through it —
  `steering_dag` -> a sibling flow's DAG -> an unrequested gateway restart. Measured on a
  real agent after the child loaders were hidden, the router's own was still reached on
  5 of 6 runs, which is what caught this.
  """
  request = _Request()
  hide(_Context({
      "_active_config_id": "repair",
      "default_config_id": "steering",
      "flow_config_map": json.dumps({"repair": "repair", "reboot": "reboot"}),
  }), request)
  assert "steering_dag" in request.config.hidden, (
      "the router's own loader stayed callable from inside a child flow; "
      f"hidden={request.config.hidden}")
  assert "reboot_dag" in request.config.hidden


def test_intent_config_map_loaders_are_hidden(hide):
  """Set when an engine host routes on an intent; its values are sibling configs."""
  request = _Request()
  hide(_Context({"_active_config_id": "repair",
                 "intent_config_map": json.dumps({"pay": "payments",
                                                  "cancel": "cancellation"})}), request)
  assert "payments_dag" in request.config.hidden
  assert "cancellation_dag" in request.config.hidden


def test_agent_config_map_loaders_are_hidden(hide):
  """Mostly redundant — a sibling agent's loader is not declared on this agent — but
  `scoped_agent_tools(extra_config_ids=...)` can declare extras named in no other map."""
  request = _Request()
  hide(_Context({"_active_config_id": "host",
                 "agent_config_map": json.dumps({"Billing_Agent": "billing",
                                                 "Support_Agent": "support"})}), request)
  assert "billing_dag" in request.config.hidden
  assert "support_dag" in request.config.hidden


def test_the_dag_suffix_is_appended_even_to_a_dag_suffixed_config_id(hide):
  """Do NOT "normalize" the suffix. The emitted tool for a config is always
  `{config_id}_dag`, so a flow named `app_host_dag` emits `app_host_dag_dag` — verified
  by building one. Treating the suffix as already-present would hide `app_host_dag`,
  a name that does not exist, and leave the real loader visible."""
  request = _Request()
  hide(_Context({"default_config_id": "app_host_dag"}), request)
  assert "app_host_dag_dag" in request.config.hidden, request.config.hidden
  assert "app_host_dag" not in request.config.hidden


def test_a_dict_flow_config_map_is_accepted(hide):
  """State can hand back a parsed dict rather than a JSON string."""
  request = _Request()
  hide(_Context({"_active_config_id": "repair",
                 "flow_config_map": {"reboot": "reboot"}}), request)
  assert "reboot_dag" in request.config.hidden
