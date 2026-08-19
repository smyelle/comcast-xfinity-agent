"""Higher-level authoring: router_flow, journey, and the build-time oracles.

Covers router_flow() shape + order preservation, journey() intent/spine/terminal
gating + its raises, HostRouter.route_cues threading through validate_app, the
_check_journey_gates oracle, and build's sensitive-marker handling.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_journey_router.py
"""

from __future__ import annotations

import pytest

from flows import (
    Agent,
    App,
    Flow,
    HostRouter,
    Operation,
    announce,
    eq,
    has,
    intent_slot,
    journey,
    result_slot,
    router_flow,
    task,
    user_slot,
    validate_app,
)
from flows.authoring.build import _check_journey_gates, _router_config_for


# --- router_flow ------------------------------------------------------------
def test_router_flow_shape():
  rf = router_flow("router", ["pay", "refund"])
  cfg = rf.to_config()
  assert cfg["router"] is True
  assert cfg["gate_slot"] == "active_flow"
  # intent_first is what lets the engine's ordered route/default backstops run; without
  # it routing rests entirely on the model naming a valid flow.
  assert cfg["bootstrap"] == {"tool": "set_active_flow", "slot": "active_flow",
                              "intent_first": True}
  assert cfg["flow_types"] == ["pay", "refund"]


def test_router_flow_preserves_order_not_sorted():
  # Order is the same-offset routing tiebreak — flow_types must be verbatim, not sorted.
  rf = router_flow("router", ["zebra", "apple", "mango"])
  assert rf.to_config()["flow_types"] == ["zebra", "apple", "mango"]


def test_router_flow_route_cues_verbatim():
  cues = {"refund": ["get a refund"], "pay": ["pay my bill"]}
  rf = router_flow("router", ["pay", "refund"], route_cues=cues)
  # verbatim + order-preserving (dict order kept, not re-sorted).
  assert list(rf.to_config()["route_cues"].items()) == list(cues.items())


def test_router_flow_intent_slot_added():
  isl = intent_slot("journey_intent", {"pay": ["pay"], "refund": ["refund"]},
                    passive=True)
  rf = router_flow("router", ["pay", "refund"], intent_slot=isl)
  assert isl in rf.to_config()["slots"]


# --- journey ----------------------------------------------------------------
def _journey() -> Flow:
  ops = [
      Operation("pay", ["pay my bill", "payment"],
                slots=[user_slot("amount", "How much?")],
                tasks=[task("pay_task", "do_pay", ["amount"], "pay_res",
                            terminal=True)]),
      Operation("refund", ["refund", "money back"],
                tasks=[task("refund_task", "do_refund", ["acct"], "refund_res",
                            terminal=True)]),
  ]
  return journey("journey_demo", spine=[user_slot("acct", "Account?")],
                 operations=ops, parent="Host_Agent")


def test_journey_has_intent_slot_from_operations():
  cfg = _journey().to_config()
  intent = next(s for s in cfg["slots"] if s.get("kind") == "intent")
  assert intent["name"] == "journey_intent"
  # option_cues derive from operations (the single source of truth), order preserved.
  assert intent["option_cues"] == {
      "pay": ["pay my bill", "payment"],
      "refund": ["refund", "money back"],
  }


def test_journey_one_gated_terminal_per_op():
  cfg = _journey().to_config()
  terminals = [t for t in cfg["tasks"] if t.get("terminal")]
  assert len(terminals) == 2
  by_name = {t["name"]: t for t in terminals}
  # each terminal gate references the intent value (derived, cannot desync).
  assert by_name["pay_task"]["condition"] == eq("journey_intent", "pay")
  assert by_name["refund_task"]["condition"] == eq("journey_intent", "refund")
  # each terminal transfers back to the parent.
  assert by_name["pay_task"]["on_complete"]["transfer_to"] == "Host_Agent"
  assert by_name["refund_task"]["on_complete"]["transfer_to"] == "Host_Agent"


def test_journey_derived_gate_overwrites_authored():
  # A hand-authored gate on the terminal is overwritten by the derived intent gate.
  op = Operation("pay", ["pay"],
                 tasks=[task("t", "do_pay", ["a"], "r", terminal=True,
                             condition=has("a"))])
  cfg = journey("j", spine=[], operations=[op], parent="P").to_config()
  term = next(t for t in cfg["tasks"] if t.get("terminal"))
  assert term["condition"] == eq("journey_intent", "pay")


def test_journey_duplicate_value_raises():
  ops = [
      Operation("pay", ["a"], tasks=[task("t1", "x", [], "r1", terminal=True)]),
      Operation("pay", ["b"], tasks=[task("t2", "y", [], "r2", terminal=True)]),
  ]
  with pytest.raises(ValueError, match="duplicate operation value"):
    journey("j", spine=[], operations=ops, parent="P")


def test_journey_no_tasks_raises():
  with pytest.raises(ValueError, match="has no tasks"):
    journey("j", spine=[], operations=[Operation("pay", ["a"])], parent="P")


def test_journey_multiple_terminals_raises():
  op = Operation("pay", ["a"], tasks=[
      task("t1", "x", [], "r1", terminal=True),
      task("t2", "y", [], "r2", terminal=True),
  ])
  with pytest.raises(ValueError, match="exactly ONE terminal"):
    journey("j", spine=[], operations=[op], parent="P")


# --- _check_journey_gates oracle --------------------------------------------
def test_check_journey_gates_clean_journey():
  assert _check_journey_gates(_journey().to_config()) == []


def test_check_journey_gates_flags_tampered_gate():
  cfg = _journey().to_config()
  # Tamper a terminal's gate to a value that is NOT an intent option.
  for t in cfg["tasks"]:
    if t["name"] == "pay_task":
      t["condition"] = eq("journey_intent", "BOGUS")
  errors = _check_journey_gates(cfg)
  assert errors
  assert any("BOGUS" in e for e in errors)


# --- HostRouter.route_cues through validate_app (engine strategy) ------------
def _sub_agent(name: str, cid: str) -> Agent:
  f = Flow(cid, root_agent=name)
  f.add(
      user_slot(f"{cid}_ref", "Reference?"),
      result_slot(f"{cid}_res", f"{cid}_task"),
      announce("done", ["{%s_res}" % cid], requires=[f"{cid}_res"], end=True),
  )
  f.task(f"{cid}_task", f"do_{cid}", [f"{cid}_ref"], f"{cid}_res",
         condition=has(f"{cid}_ref"))
  return Agent(name, flow=f)


def _engine_host_app() -> App:
  a = _sub_agent("Pay_Agent", "pay")
  b = _sub_agent("Refund_Agent", "refund")
  host = HostRouter(
      "Host", routes={"pay": a, "refund": b}, strategy="engine",
      route_cues={"pay": ["pay my bill", "payment"], "refund": ["get a refund"]},
  )
  return App(host=host, agents=[a, b], app_display_name="Route Demo")


def test_host_route_cues_validate_clean():
  errors, _warnings = validate_app(_engine_host_app())
  assert errors == [], errors


def test_host_route_cues_threaded_verbatim_and_ordered():
  host = _engine_host_app().host
  _cid, cfg = _router_config_for(host)
  # Explicit route_cues override alias-derived cues verbatim; order preserved.
  assert list(cfg["route_cues"].items()) == [
      ("pay", ["pay my bill", "payment"]),
      ("refund", ["get a refund"]),
  ]


# --- sensitive handling through build ---------------------------------------
def _sensitive_app() -> App:
  f = Flow("sens", root_agent="Sens_Agent")
  f.add(
      user_slot("ssn", "SSN?", sensitive=True),
      result_slot("res", "verify"),
      announce("done", ["Verified."], requires=["res"], end=True),
  )
  f.task("verify", "do_verify", ["ssn"], "res", terminal=True, condition=has("ssn"))
  return App(root_flow=f, app_display_name="Sensitive Demo")


def test_sensitive_slot_validates_clean():
  errors, _warnings = validate_app(_sensitive_app())
  assert errors == [], errors


def test_sensitive_stripped_and_readback_gated():
  from flows.authoring.build import _apply_sensitive_readback
  cfg = _sensitive_app().root_flow.to_config()
  assert any(s.get("sensitive") for s in cfg["slots"])  # present before build
  out = _apply_sensitive_readback(cfg)
  # `sensitive` never reaches the validated/emitted config.
  assert not any("sensitive" in s for s in out["slots"])
  # terminal readback_inputs derived False when a sensitive input is present.
  terminal = next(t for t in out["tasks"] if t.get("terminal"))
  assert terminal["readback_inputs"] is False
