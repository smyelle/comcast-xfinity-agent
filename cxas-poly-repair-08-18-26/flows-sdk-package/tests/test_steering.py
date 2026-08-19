"""Steering — the first-class Route-based `router_flow`.

Four bands, mirroring the other component tests:

* construction — `route(...)` / `router_flow([...routes])` shape + the guards;
* emit — the generated `<routing>` instruction, the NON-identity `flow_config_map`, the
  one shared deferral flow + its recorder tool, and that every route key stays a valid
  gate value;
* engine (offline sim) — a handled route reaches its own flow; a DEFERRED route reaches
  the ONE shared deferral flow through the map while keeping its own label on the gate;
* back-compat — the bare-flow-key form is untouched.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_steering.py
"""

from __future__ import annotations

import json
import os

import pytest

import flows
from flows.authoring import steering as _steering
from flows.authoring import tools as _tools
from flows.sim import engine_sim


@pytest.fixture(autouse=True)
def _clean_registry():
  _tools.clear_registry()
  yield
  _tools.clear_registry()


# --- fixtures ---------------------------------------------------------------------

def _diag() -> flows.Flow:
  f = flows.Flow("diagnostics")
  f.add(flows.announce("diag_open", ["Let's get your internet working."]))
  return f


def _reboot() -> flows.Flow:
  f = flows.Flow("reboot")
  f.add(flows.announce("reboot_open", ["I can restart your gateway now."]))
  return f


def _routes() -> list:
  return [
      flows.route("diagnostics", "the caller's internet or WiFi is not working",
                  flow=_diag()),
      flows.route("reboot", "the caller asks to restart or power-cycle the gateway",
                  flow=_reboot(), cues=["power cycle the modem", "reset the router"]),
      flows.route("billing", "the caller wants to understand or dispute a bill",
                  backstop=["my bill", "a charge", "my statement", "overcharged"]),
      flows.route("payments", "the caller wants to make a payment or set up autopay",
                  backstop=["make a payment", "pay my bill", "autopay"]),
  ]


def _router() -> flows.Flow:
  return flows.router_flow("steering", _routes(), disambiguate=True)


def _app() -> flows.App:
  return flows.App(root_flow=_router(), app_display_name="steering-test",
                   agent_instruction="You are a friendly Acme Internet agent.")


def _build(app: flows.App, tmp_path) -> str:
  out = os.path.join(str(tmp_path), "app")
  res = flows.build_app(app, out)
  assert res.ok, res
  return out


# --- construction -----------------------------------------------------------------

def test_route_carries_the_four_fields_plus_cues():
  r = flows.route("billing", "understand a bill", cues=["my bill"])
  assert (r.name, r.description) == ("billing", "understand a bill")
  assert r.flow is None and r.handled is False
  assert r.cues == ("my bill",)


def test_a_route_with_a_flow_is_handled():
  r = flows.route("diagnostics", "internet down", flow=_diag())
  assert r.handled is True


def test_route_requires_a_name_and_a_description():
  with pytest.raises(ValueError):
    flows.route("", "desc")
  with pytest.raises(ValueError):
    flows.route("billing", "")


def test_router_flow_sets_flow_types_to_every_route_key():
  cfg = _router().to_config()
  assert cfg["flow_types"] == ["diagnostics", "reboot", "billing", "payments"]
  assert cfg["router"] is True and cfg["gate_slot"] == "active_flow"


def test_router_flow_folds_per_route_cues():
  cfg = _router().to_config()
  assert cfg["route_cues"] == {"reboot": ["power cycle the modem", "reset the router"]}


def test_router_flow_stashes_a_spec_but_does_not_leak_it_into_config():
  f = _router()
  assert isinstance(f._steering, _steering.SteeringSpec)
  assert "_steering" not in f.to_config()


def test_duplicate_route_names_are_rejected():
  with pytest.raises(ValueError, match="duplicate route"):
    flows.router_flow("s", [flows.route("a", "x"), flows.route("a", "y")])


def test_default_flow_is_rejected_for_a_route_based_router():
  # default_flow is a pre-model preempt; a Route router handles low confidence via
  # disambiguate= (model-driven), never default_flow.
  with pytest.raises(ValueError, match="default_flow"):
    flows.router_flow("s", [flows.route("a", "x", flow=_diag())], default_flow="a")


def test_mixing_routes_and_bare_strings_is_an_error():
  with pytest.raises(ValueError, match="mix"):
    flows.router_flow("s", [flows.route("a", "x"), "b"])


def test_multiple_routes_can_share_one_handoff_flow():
  # The real-hand-off pattern (what replaces handoff_say): give several routes the SAME
  # flow. The non-identity flow_config_map points them all at that one flow's config; the
  # chosen route key still survives on the gate so the flow knows which intent it is.
  handoff = _reboot()  # any real flow stands in for a live-agent hand-off
  router = flows.router_flow("s", [
      flows.route("diagnostics", "internet down", flow=_diag()),
      flows.route("billing", "a bill question", flow=handoff),
      flows.route("human", "wants a person", flow=handoff),
  ])
  cmap = router._steering.config_map()
  assert cmap == {"diagnostics": "diagnostics", "billing": "reboot", "human": "reboot"}
  # only one copy of the shared flow is added as a child
  child_ids = [f.config_id for f in router._steering.child_flows()]
  assert child_ids.count("reboot") == 2  # returned per-route; build dedups by config_id


# --- back-compat: bare flow keys ---------------------------------------------------

def test_bare_key_router_flow_is_unchanged():
  cfg = flows.router_flow("s", ["billing", "sales"], default_flow="billing").to_config()
  assert cfg["flow_types"] == ["billing", "sales"]
  assert cfg["bootstrap"]["default_flow"] == "billing"
  assert not hasattr(flows.router_flow("s", ["a"]), "_steering") or \
      getattr(flows.router_flow("s", ["a"]), "_steering", None) is None


# --- emit -------------------------------------------------------------------------

def test_emits_the_generated_routing_instruction(tmp_path):
  out = _build(_app(), tmp_path)
  instr = open(os.path.join(out, "agents", "steering_agent", "instruction.txt")).read()
  assert "You are a friendly Acme Internet agent." in instr  # persona preserved
  assert "<routing>" in instr and "</routing>" in instr
  # every route's description reaches the classifier block
  for phrase in ["internet or WiFi is not working", "understand or dispute a bill",
                 "make a payment or set up autopay", "restart or power-cycle"]:
    assert phrase in instr
  # disambiguate=True folds ALL low-confidence cases into one clarify path
  assert "clarifying question" in instr and "off-topic" in instr


def test_emits_a_non_identity_flow_config_map(tmp_path):
  out = _build(_app(), tmp_path)
  app_json = json.load(open(os.path.join(out, "app.json")))
  vs = {v["name"]: v for v in app_json.get("variableDeclarations", [])}
  cmap = json.loads(vs["flow_config_map"]["schema"]["default"])
  # handled routes -> own config; BOTH deferred routes -> the ONE shared deferral flow.
  assert cmap == {"diagnostics": "diagnostics", "reboot": "reboot",
                  "billing": "steering_defer", "payments": "steering_defer"}


def test_emits_the_shared_deferral_flow_and_recorder_tool(tmp_path):
  out = _build(_app(), tmp_path)
  assert os.path.isdir(os.path.join(out, "tools", "steering_defer_dag"))
  assert os.path.isdir(os.path.join(out, "tools", "steering_record_intent"))
  # the handled routes emit their own DAGs too
  assert os.path.isdir(os.path.join(out, "tools", "diagnostics_dag"))
  assert os.path.isdir(os.path.join(out, "tools", "reboot_dag"))


def test_every_route_key_is_resolvable_by_the_map(tmp_path):
  # Every route key (handled AND deferred) must appear in flow_config_map, or the
  # before_agent resolver could not activate a config for it — the coerce guard would
  # drop the label to the fallback.
  out = _build(_app(), tmp_path)
  app_json = json.load(open(os.path.join(out, "app.json")))
  vs = {v["name"]: v for v in app_json.get("variableDeclarations", [])}
  cmap = json.loads(vs["flow_config_map"]["schema"]["default"])
  assert set(cmap) == {"diagnostics", "reboot", "billing", "payments"}


def test_a_router_with_no_deferred_routes_emits_no_deferral_flow(tmp_path):
  app = flows.App(
      root_flow=flows.router_flow(
          "s", [flows.route("diagnostics", "internet down", flow=_diag()),
                flows.route("reboot", "restart", flow=_reboot())]),
      app_display_name="no-defer")
  out = _build(app, tmp_path)
  assert not os.path.isdir(os.path.join(out, "tools", "steering_defer_dag"))
  assert not os.path.isdir(os.path.join(out, "tools", "steering_record_intent"))


# --- flat single-pass (A2) --------------------------------------------------------

def _flat_tree() -> list:
  """A 2-category tree with leaf subroutes — the shape A2 folds into a flat leaf set."""
  return [
      flows.route("billing", "questions about the bill, balance, or a charge",
                  subroutes=[flows.route("bill_balance", "how much is owed on the account"),
                             flows.route("bill_dispute", "dispute or question a charge")],
                  disambiguate=flows.disambiguation(max_turns=1), default="bill_balance"),
      flows.route("payments", "make or manage a payment",
                  subroutes=[flows.route("pay_now", "pay the bill right now"),
                             flows.route("autopay", "set up automatic payments")],
                  disambiguate=flows.disambiguation(max_turns=1), default="pay_now"),
  ]


def test_flat_mode_folds_the_tree_to_a_leaf_gate_enum():
  f = flows.router_flow("steering", _flat_tree(), route_mode="flat")
  spec = f._steering
  # The gate classifies straight to a LEAF (one inference), not the category.
  assert f.to_config()["flow_types"] == ["bill_balance", "bill_dispute", "pay_now", "autopay"]
  assert spec.route_mode == "flat"
  # No internal nodes survive: no scoped sub-classifiers, so it is a single pass.
  assert spec.has_internal is False and spec.has_deferred is True
  # Every leaf shares the ONE deferral flow (non-identity map).
  assert set(spec.config_map().values()) == {spec.defer_config_id}


def test_flat_mode_derives_the_category_path_per_leaf():
  spec = flows.router_flow("steering", _flat_tree(), route_mode="flat")._steering
  assert spec.leaf_paths == {
      "bill_balance": "billing/bill_balance", "bill_dispute": "billing/bill_dispute",
      "pay_now": "payments/pay_now", "autopay": "payments/autopay"}


def test_flat_mode_emits_a_category_grouped_routing_block():
  instr = _steering.routing_instruction(
      flows.router_flow("steering", _flat_tree(), route_mode="flat")._steering)
  assert "TOPIC AREA — billing" in instr and "TOPIC AREA — payments" in instr
  # leaves listed under their area, with a contrastive rule-out note
  assert 'flow="bill_dispute"' in instr and "Rule out:" in instr
  assert "belongs to a different topic area" in instr


def test_flat_mode_with_mixed_top_level_leaves_and_categories():
  """A top-level LEAF (e.g. a direct handoff route) mixed with category subtrees. flatten_tree
  keeps the leaf as its own single-leaf area — its path is just its own name and it gets NO
  rule-out note (nothing under it to rule out) — while each category area lists it as a sibling
  topic to rule out (single-quoted)."""
  tree = [
      flows.route("billing", "questions about the bill or a charge",
                  subroutes=[flows.route("bill_balance", "how much is owed"),
                             flows.route("bill_dispute", "dispute a charge")]),
      flows.route("payments", "make or manage a payment",
                  subroutes=[flows.route("pay_now", "pay right now"),
                             flows.route("autopay", "set up automatic payments")]),
      flows.route("human", "asks to speak with a person"),   # top-level deferred leaf
  ]
  f = flows.router_flow("steering", tree, route_mode="flat")
  spec = f._steering
  # The top-level leaf joins the flat gate enum alongside the folded category leaves.
  assert f.to_config()["flow_types"] == [
      "bill_balance", "bill_dispute", "pay_now", "autopay", "human"]
  assert set(spec.leaf_paths) == {
      "bill_balance", "bill_dispute", "pay_now", "autopay", "human"}
  # A top-level leaf's path is just its own name (no category ancestor).
  assert spec.leaf_paths["human"] == "human"
  assert spec.leaf_paths["bill_balance"] == "billing/bill_balance"
  groups = {g[0]: g for g in spec.groups}   # cat_name -> (name, desc, pairs, rule_out_note)
  # The top-level leaf is its own single-leaf area with NO rule-out note...
  assert groups["human"][2] == (("human", "asks to speak with a person"),)
  assert groups["human"][3] == ""
  # ...while each category lists the top-level leaf as a sibling area to rule out (quoted).
  assert "'human'" in groups["billing"][3] and "'payments'" in groups["billing"][3]
  assert "'human'" in groups["payments"][3]


def test_flat_mode_recorder_records_the_derived_path(tmp_path):
  app = flows.App(
      root_flow=flows.router_flow("steering", _flat_tree(), route_mode="flat"),
      app_display_name="flat-steering", agent_instruction="You are an agent.")
  out = _build(app, tmp_path)
  # ONE deferral flow + recorder for the whole flat set; no per-category classification flows.
  assert os.path.isdir(os.path.join(out, "tools", "steering_defer_dag"))
  assert not os.path.isdir(os.path.join(out, "tools", "billing_dag"))
  src = open(os.path.join(
      out, "tools", "steering_record_intent", "python_function", "python_code.py")).read()
  assert "detected_path" in src and "billing/bill_dispute" in src


def test_flat_mode_needs_the_route_object_form():
  with pytest.raises(ValueError):
    flows.router_flow("s", ["billing", "payments"], route_mode="flat")


def test_route_mode_must_be_valid():
  with pytest.raises(ValueError):
    flows.router_flow("s", _flat_tree(), route_mode="nope")


# --- engine (offline sim) ---------------------------------------------------------

def _sim_configs(spec: _steering.SteeringSpec) -> dict:
  return {
      "diagnostics": _diag().to_config(),
      "reboot": _reboot().to_config(),
      spec.defer_config_id: _steering.defer_flow(spec).to_config(),
  }


def _sim_start():
  router = _router()
  spec = router._steering
  engine_sim.reset_store()
  sid, _ = engine_sim.start(router.to_config(), "steering",
                            configs=_sim_configs(spec))
  session = engine_sim._SESSIONS[sid]
  session.flow_config_map = spec.config_map()
  return sid, session


def _route_to(session, key: str):
  """Fill the router gate with `key` (as set_active_flow would) and resolve the switch,
  exactly as live before_agent does — the pattern the router unit tests use."""
  session.sm.setdefault("filled", {})["active_flow"] = key
  return engine_sim._follow_flow_switch(session, session.sm)


def test_a_handled_route_reaches_its_own_flow():
  _sid, session = _sim_start()
  assert _route_to(session, "diagnostics") == "diagnostics"
  assert "diagnostics" in session.config_id


def test_a_deferred_route_reaches_the_shared_flow_but_keeps_its_label():
  _sid, session = _sim_start()
  # routed by LABEL "billing" ...
  assert _route_to(session, "billing") == "billing"
  # ... but resolved to the ONE shared deferral CONFIG (the non-identity map) ...
  assert "steering_defer" in session.config_id
  assert any(t["name"] == "record_detected_intent"
             for t in session.config.get("tasks", []))
  # ... and the chosen label survives on the gate for detected_intent.
  assert session.sm.get("filled", {}).get("active_flow") == "billing"


def test_two_deferred_routes_share_one_config():
  _sid, session = _sim_start()
  assert _route_to(session, "payments") == "payments"
  assert "steering_defer" in session.config_id
  assert session.sm.get("filled", {}).get("active_flow") == "payments"


# --- Phase B: post-model backstop + disambiguation budget (state-var emission) ---------
# The after_model handler that consumes these vars runs only inside CES (the offline sim
# does not load callbacks), so it is proven live (see the deploy driver); here we pin that
# the authoring + build layers EMIT the vars the handler keys on, and only when declared.

def test_disambiguation_factory():
  d = flows.disambiguation(max_turns=3, on_exhaust="human")
  assert d.max_turns == 3 and d.on_exhaust == "human"
  with pytest.raises(ValueError):
    flows.disambiguation(max_turns=-1)


def test_on_exhaust_must_be_a_real_route():
  with pytest.raises(ValueError, match="on_exhaust"):
    flows.router_flow("s", [flows.route("a", "x", flow=_diag())],
                      disambiguate=flows.disambiguation(max_turns=2, on_exhaust="nope"))


def test_emits_the_backstop_keyword_net(tmp_path):
  out = _build(_app(), tmp_path)
  app_json = json.load(open(os.path.join(out, "app.json")))
  vs = {v["name"]: v for v in app_json.get("variableDeclarations", [])}
  backstop = json.loads(vs["steering_backstop"]["schema"]["default"])
  assert backstop == {"billing": ["my bill", "a charge", "my statement", "overcharged"],
                      "payments": ["make a payment", "pay my bill", "autopay"]}


def test_no_backstop_declared_emits_no_var(tmp_path):
  app = flows.App(root_flow=flows.router_flow(
      "s", [flows.route("diagnostics", "internet down", flow=_diag()),
            flows.route("billing", "a bill question")]),
      app_display_name="no-backstop")
  out = _build(app, tmp_path)
  vs = {v["name"] for v in json.load(open(os.path.join(out, "app.json")))
        .get("variableDeclarations", [])}
  assert "steering_backstop" not in vs
  assert "steering_disambiguate" not in vs


def test_emits_the_disambiguation_budget(tmp_path):
  app = flows.App(root_flow=flows.router_flow(
      "s", [flows.route("diagnostics", "internet down", flow=_diag()),
            flows.route("billing", "a bill question"),
            flows.route("human", "wants a person / to be transferred to an agent")],
      disambiguate=flows.disambiguation(max_turns=2, on_exhaust="human")),
      app_display_name="disambig")
  out = _build(app, tmp_path)
  vs = {v["name"]: v for v in json.load(open(os.path.join(out, "app.json")))
        .get("variableDeclarations", [])}
  budget = json.loads(vs["steering_disambiguate"]["schema"]["default"])
  assert budget == {"max_turns": 2, "on_exhaust": "human"}


def test_plain_disambiguate_true_emits_no_budget_var(tmp_path):
  # disambiguate=True is instruction-only (ask, no hard budget) — no runtime state var.
  # Build a router that EXPLICITLY sets disambiguate=True (not via _app() indirection).
  app = flows.App(root_flow=flows.router_flow(
      "steering",
      [flows.route("diagnostics", "internet down", flow=_diag()),
       flows.route("billing", "a bill question")],
      disambiguate=True), app_display_name="disambig-true")
  out = _build(app, tmp_path)
  vs = {v["name"] for v in json.load(open(os.path.join(out, "app.json")))
        .get("variableDeclarations", [])}
  assert "steering_disambiguate" not in vs
  instr = open(os.path.join(out, "agents", "steering_agent", "instruction.txt")).read()
  assert "clarifying question" in instr  # the ask IS instructed


# --- Owl review fixes: validation + aliases + namespacing ---------------------------

def test_route_rejects_a_name_with_special_characters():
  for bad in ('bill"ing', "my flow", "a/b", "x.y"):
    with pytest.raises(ValueError, match="name"):
      flows.route(bad, "desc")


def test_route_rejects_non_string_cue_backstop_alias_elements():
  with pytest.raises(ValueError, match="cues"):
    flows.route("a", "desc", cues=[1, 2])
  with pytest.raises(ValueError, match="backstop"):
    flows.route("a", "desc", backstop=["ok", 3])
  with pytest.raises(ValueError, match="aliases"):
    flows.route("a", "desc", aliases=[None])


def test_disambiguation_on_exhaust_requires_a_budget():
  with pytest.raises(ValueError, match="on_exhaust needs max_turns"):
    flows.disambiguation(max_turns=0, on_exhaust="human")
  # ask-with-no-budget (no on_exhaust) is fine
  assert flows.disambiguation(max_turns=0).on_exhaust == ""


def test_aliases_fold_into_route_cues():
  r = flows.router_flow("s", [
      flows.route("reboot", "restart", flow=_reboot(),
                  cues=["power cycle"], aliases=["reset the box"])])
  assert r.to_config()["route_cues"]["reboot"] == ["power cycle", "reset the box"]


def test_generated_deferral_members_are_namespaced_by_router_config_id(tmp_path):
  # Two different routers → distinct steering_defer / record-intent members (no collision).
  app = flows.App(root_flow=flows.router_flow(
      "support", [flows.route("diagnostics", "internet down", flow=_diag()),
                  flows.route("billing", "a bill question")]),
      app_display_name="ns")
  out = _build(app, tmp_path)
  assert os.path.isdir(os.path.join(out, "tools", "support_defer_dag"))
  assert os.path.isdir(os.path.join(out, "tools", "support_record_intent"))
  app_json = json.load(open(os.path.join(out, "app.json")))
  cmap = json.loads(next(v for v in app_json["variableDeclarations"]
                         if v["name"] == "flow_config_map")["schema"]["default"])
  assert cmap["billing"] == "support_defer"


def test_sim_returns_to_router_when_a_child_flow_completes():
  # Live before_agent re-resolves a completed child's gate back to the router; the sim
  # mirrors that so a multi-turn router session can route again (Owl #5).
  router = _router()
  spec = router._steering
  engine_sim.reset_store()
  sid, _ = engine_sim.start(router.to_config(), "steering", configs=_sim_configs(spec))
  session = engine_sim._SESSIONS[sid]
  session.flow_config_map = spec.config_map()
  _route_to(session, "diagnostics")
  assert "diagnostics" in session.config_id            # on the child
  # child completes -> should hand back to the router (gate cleared, config restored)
  session.sm["status"] = "complete"
  back = engine_sim._follow_flow_switch(session, session.sm)
  assert back                                            # a switch happened
  assert session.config is session.initial_config       # back on the router
  assert session.sm.get("filled", {}).get("active_flow") in (None, "")  # gate cleared


# --- multi-level (hierarchical) steering ------------------------------------------
# A route with `subroutes` is an INTERNAL node: once routed there (level 1), a scoped
# SILENT classifier picks a child from the same utterance, and recurses, so one turn
# resolves a whole intent PATH. The deeper levels are passive intent slots + classifiers
# (no engine change), condition-gated on the level above.

def _ml_routes() -> list:
  return [
      flows.route(
          "billing", "charges, invoices, payments, and disputes",
          subroutes=[
              flows.route(
                  "billing_dispute", "believes a specific charge is wrong",
                  cues=["dispute a charge"],
                  subroutes=[
                      flows.route("dispute_latefee", "a late fee they think is unfair",
                                  cues=["late fee"]),
                      flows.route("dispute_overcharge", "charged more than the plan price",
                                  cues=["overcharged"], backstop=["higher than my plan"]),
                  ]),
              flows.route("billing_explain", "just wants their bill explained"),
          ]),
      flows.route(
          "tech", "technical or service problems",
          subroutes=[
              flows.route("tech_tv", "television problems", default="tv_nosignal",
                          disambiguate=False,
                          subroutes=[
                              flows.route("tv_nosignal", "no signal / black screen"),
                              flows.route("tv_channels", "channels are missing"),
                          ]),
              flows.route("tech_phone", "home phone problems"),
          ]),
      flows.route("diagnostics", "internet is slow or down", flow=_diag()),
      flows.route("human", "asks for a person", flow=_reboot()),
  ]


def _ml_router(**kw) -> flows.Flow:
  return flows.router_flow("steering", _ml_routes(), **kw)


def _ml_app(**kw) -> flows.App:
  return flows.App(root_flow=_ml_router(**kw), app_display_name="ml-steering",
                   agent_instruction="You are the Riverline front desk.")


# construction

def test_route_with_subroutes_is_an_internal_node():
  r = flows.route("billing", "bills", subroutes=[flows.route("a", "x"), flows.route("b", "y")])
  assert r.is_internal is True and r.handled is False
  assert [s.name for s in r.subroutes] == ["a", "b"]
  assert flows.route("leaf", "z").is_internal is False


def test_a_route_cannot_be_both_internal_and_handled():
  with pytest.raises(ValueError, match="internal node"):
    flows.route("x", "d", flow=_diag(), subroutes=[flows.route("a", "y")])


def test_default_must_be_a_subroute_and_needs_subroutes():
  with pytest.raises(ValueError, match="default"):
    flows.route("x", "d", subroutes=[flows.route("a", "y")], default="nope")
  with pytest.raises(ValueError, match="needs subroutes"):
    flows.route("x", "d", default="a")


def test_disambiguate_only_on_an_internal_node():
  with pytest.raises(ValueError, match="internal node"):
    flows.route("leaf", "d", disambiguate=True)


def test_duplicate_names_anywhere_in_the_tree_are_rejected():
  with pytest.raises(ValueError, match="duplicate route names"):
    flows.router_flow("s", [
        flows.route("billing", "b", subroutes=[flows.route("dup", "x")]),
        flows.route("dup", "d", flow=_diag())])


def test_deep_on_exhaust_is_rejected_in_v1():
  with pytest.raises(ValueError, match="on_exhaust"):
    flows.router_flow("s", [
        flows.route("billing", "b",
                    disambiguate=flows.disambiguation(max_turns=2, on_exhaust="billing_explain"),
                    subroutes=[flows.route("billing_pay", "p"),
                               flows.route("billing_explain", "e")])])


def test_a_deep_handled_leaf_is_rejected_in_v1():
  with pytest.raises(ValueError, match="top-level"):
    flows.router_flow("s", [
        flows.route("tech", "t", subroutes=[
            flows.route("tech_internet", "down", flow=_diag()),
            flows.route("tech_phone", "phone")])])


# emit / build

def test_config_map_routes_internal_nodes_to_their_own_flow():
  spec = _ml_router()._steering
  cmap = spec.config_map()
  assert cmap["billing"] == "billing"          # internal -> own classification flow
  assert cmap["tech"] == "tech"                # internal -> own classification flow
  assert cmap["diagnostics"] == "diagnostics"  # handled -> its flow
  assert cmap["human"] == "reboot"             # handled -> its flow config id


def test_internal_nodes_walk_resolves_inherited_disambiguation():
  spec = _ml_router(disambiguate=flows.disambiguation(max_turns=2, on_exhaust="human"))._steering
  by_name = {n.name: eff for n, _p, eff in spec.internals}
  # billing / tech / billing_dispute all present; tech_tv explicitly OFF
  assert isinstance(by_name["billing"], _steering.Disambiguation)      # inherited router budget
  assert isinstance(by_name["billing_dispute"], _steering.Disambiguation)
  assert by_name["tech_tv"] is False                                   # explicit override -> off


def test_enum_is_the_default_so_no_head_classifiers():
  # Default classifier_style="enum" -> sub-intent uses the enum setter (like the L1 gate),
  # so NO classifiers are registered.
  assert _ml_router()._steering.head_classifiers() == {}


def test_head_classifiers_default_silent_vs_asked():
  # The fuzzy escape hatch registers a classifier per internal node.
  # silent (no router disambiguate): default = the fallback child
  silent = _ml_router(classifier_style="fuzzy")._steering.head_classifiers()
  assert silent["set_sub_intent__tech_tv"][1] == "tv_nosignal"   # explicit default=
  # asked (inherited budget): default = None so a no-match re-asks
  asked = _ml_router(classifier_style="fuzzy",
                     disambiguate=flows.disambiguation(max_turns=2, on_exhaust="human")) \
      ._steering.head_classifiers()
  assert asked["set_sub_intent__billing"][1] is None
  # tech_tv stays silent even under a router budget (disambiguate=False) -> keeps its default
  assert asked["set_sub_intent__tech_tv"][1] == "tv_nosignal"


def test_sub_intent_slots_are_condition_gated_on_the_parent():
  spec = _ml_router()._steering
  cfg = _steering.classification_flow(spec.routes[0], spec).to_config()  # billing
  slots = {s["name"]: s for s in cfg["slots"] if s["name"].startswith("sub_intent__")}
  assert slots["sub_intent__billing"].get("condition") is None          # level-1: no gate
  assert "billing_dispute" in str(slots["sub_intent__billing_dispute"]["condition"])
  assert slots["sub_intent__billing"]["passive"] is True                # silent by default


def test_deeper_cues_and_backstop_fold_into_option_cues():
  spec = _ml_router()._steering
  cfg = _steering.classification_flow(spec.routes[0], spec).to_config()
  disp = next(s for s in cfg["slots"] if s["name"] == "sub_intent__billing_dispute")
  # dispute_overcharge carries cues + backstop; both become deterministic option cues
  assert disp["option_cues"]["dispute_overcharge"] == ["overcharged", "higher than my plan"]
  # a child with no cues falls back to its humanized name
  bill = next(s for s in cfg["slots"] if s["name"] == "sub_intent__billing")
  assert bill["option_cues"]["billing_explain"] == ["billing explain"]


def test_build_emits_classification_flows_classifiers_and_the_recorder(tmp_path):
  out = _build(_ml_app(disambiguate=flows.disambiguation(max_turns=2, on_exhaust="human")),
               tmp_path)
  tools = set(os.listdir(os.path.join(out, "tools")))
  assert {"billing_dag", "tech_dag", "diagnostics_dag", "reboot_dag"} <= tools
  assert {"set_sub_intent__billing", "set_sub_intent__billing_dispute",
          "set_sub_intent__tech", "set_sub_intent__tech_tv"} <= tools
  assert "steering_record_path" in tools
  # flow_config_map points internal nodes at their own classification flow
  cmap = json.loads(next(v for v in json.load(open(os.path.join(out, "app.json")))
                         ["variableDeclarations"] if v["name"] == "flow_config_map")
                    ["schema"]["default"])
  assert cmap["billing"] == "billing" and cmap["tech"] == "tech"


def test_recorder_members_are_namespaced_by_router(tmp_path):
  app = flows.App(root_flow=flows.router_flow(
      "support", [flows.route("billing", "b", subroutes=[
          flows.route("billing_pay", "p"), flows.route("billing_ask", "a")])]),
      app_display_name="ns-ml")
  out = _build(app, tmp_path)
  assert os.path.isdir(os.path.join(out, "tools", "support_record_path"))


# engine (offline sim): the option-cue cascade resolves a whole PATH in ONE turn

def test_option_cues_resolve_the_full_path_in_one_turn():
  router = _ml_router()  # silent (no disambiguate) so passive slots fill via option_cues
  spec = router._steering
  configs = {"billing": _steering.classification_flow(spec.routes[0], spec).to_config()}
  engine_sim.reset_store()
  sid, _ = engine_sim.start(router.to_config(), "steering", configs=configs)
  session = engine_sim._SESSIONS[sid]
  session.flow_config_map = spec.config_map()
  session.sm.setdefault("filled", {})["active_flow"] = "billing"
  engine_sim._follow_flow_switch(session, session.sm)
  engine_sim.step({"session_id": sid, "kind": "user_text",
                   "text": "I want to dispute a charge, it's a late fee"})
  filled = engine_sim.session_sm(sid).get("filled", {})
  assert filled.get("sub_intent__billing") == "billing_dispute"       # level 2 (cue)
  assert filled.get("sub_intent__billing_dispute") == "dispute_latefee"  # level 3 (cue)


# the recorder walks the sub_intent chain from the gate to the deepest leaf

def _run_recorder(spec, state) -> dict:
  name, src, _keys = _steering.record_path_tool(spec.record_path_name)

  class _Ctx:
    pass

  ctx = _Ctx()
  ctx.state = state
  g = {"context": ctx}
  exec(src, g)  # noqa: S102 — generated tool source, tested in isolation
  return g[name](), ctx


def test_record_path_walks_to_the_deepest_leaf():
  spec = _ml_router()._steering
  out, ctx = _run_recorder(spec, {
      "active_flow": "billing",
      "sub_intent__billing": "billing_dispute",
      "sub_intent__billing_dispute": "dispute_overcharge"})
  assert out["detected_intent"] == "dispute_overcharge"
  assert out["detected_path"] == "billing/billing_dispute/dispute_overcharge"
  assert ctx.state["detected_intent"] == "dispute_overcharge"        # written for downstream
  assert ctx.state["detected_path"] == "billing/billing_dispute/dispute_overcharge"


def test_record_path_reads_slots_from_the_sm_filled_map():
  spec = _ml_router()._steering
  out, _ctx = _run_recorder(spec, {
      "active_flow": "billing",
      "sm": {"filled": {"sub_intent__billing": "billing_explain"}}})
  assert out["detected_intent"] == "billing_explain"
  assert out["detected_path"] == "billing/billing_explain"


def test_record_path_reads_the_active_flow_gate_from_sm_filled():
  # The blessed engine rides the active_flow GATE on sm.filled, not at the state root, so the
  # recorder must read the gate via the same _slot fallback as every other slot. Regression:
  # it used state.get("active_flow") directly, so the walk never started and detected_intent
  # came back empty even though the sub-intent was resolved.
  spec = _ml_router()._steering
  out, _ctx = _run_recorder(spec, {
      "sm": {"filled": {"active_flow": "billing",
                        "sub_intent__billing": "billing_explain"}}})
  assert out["detected_intent"] == "billing_explain"
  assert out["detected_path"] == "billing/billing_explain"


def test_record_path_of_a_bare_level_1_route_is_the_route_itself():
  spec = _ml_router()._steering
  out, _ctx = _run_recorder(spec, {"active_flow": "billing"})
  assert out["detected_intent"] == "billing"
  assert out["detected_path"] == "billing"


def test_classifier_merging_priority_author_wins(tmp_path):
  # Author-defined classifiers should override build-generated ones if they share the same name.
  custom_mapping = {"billing_dispute": ["this is a custom billing dispute description"]}
  custom_default = "billing_explain"
  app = _ml_app()
  app.classifiers["set_sub_intent__billing"] = (custom_mapping, custom_default)

  out = _build(app, tmp_path)

  tool_path = os.path.join(out, "tools", "set_sub_intent__billing", "python_function", "python_code.py")
  tool_code = open(tool_path).read()
  # The generated classifier should match the custom mapping.
  assert "this is a custom billing dispute description" in tool_code
  # Also check that the custom default is used (instead of the generated default which was None)
  assert "billing_explain" in tool_code
