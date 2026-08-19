"""Multi-agent authoring: host/steering router + N slot-filling sub-agents.

Covers the two host strategies (transfer/receptionist + engine/config-swap), the
emitted app shape (childAgents, agent_config_map, set_active_flow routing, per-agent
callbacks + tool scoping), framework drift, and single-agent back-compat.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_multi_agent.py
"""

from __future__ import annotations

import json
import os

import pytest

import flows
from flows.authoring import tools as _tools
from flows.engine import blessed_source as _bs


def _agent(name: str, cid: str, description: str | None = None) -> flows.Agent:
  f = flows.Flow(cid, root_agent=name)
  f.add(
      flows.user_slot(f"{cid}_ref", "What's your reference?"),
      flows.result_slot(f"{cid}_result", f"{cid}_task"),
      flows.announce("done", ["{%s_result}" % cid], requires=[f"{cid}_result"], end=True),
  )
  f.task(f"{cid}_task", f"do_{cid}", [f"{cid}_ref"], f"{cid}_result",
         condition=flows.has(f"{cid}_ref"))
  return flows.Agent(name, flow=f, description=description)


def _bella_like() -> flows.App:
  tracking = _agent("Tracking_Agent", "tracking")
  pickup = _agent("Pickup_Agent", "pickup")
  host = flows.HostRouter(
      "Steering_Host",
      routes={"tracking": tracking, "pickup": pickup},
      entry_var="ENTRY_INTENT",
  )
  return flows.App(host=host, agents=[tracking, pickup], app_display_name="Multi Demo")


def _emit(app: flows.App, tmp_path) -> str:
  out = str(tmp_path / "app")
  res = flows.build_app(app, out)
  assert res.ok, res.validation.errors if res.validation else res.error
  return out


def _load(out: str, *parts: str) -> dict:
  return json.loads(open(os.path.join(out, *parts)).read())


# --- validation --------------------------------------------------------------
def test_validate_multi_agent_clean():
  errors, _warnings = flows.validate_app(_bella_like())
  assert errors == [], errors


# --- description-driven host instruction (shared <routing> generator) --------
def test_host_instruction_is_description_driven_when_routes_have_descriptions(tmp_path):
  tracking = _agent("Tracking_Agent", "tracking",
                    description="the caller wants to track a shipment")
  pickup = _agent("Pickup_Agent", "pickup",
                  description="the caller wants to schedule a pickup")
  host = flows.HostRouter("Steering_Host",
                          routes={"tracking": tracking, "pickup": pickup})
  app = flows.App(host=host, agents=[tracking, pickup], app_display_name="Desc Demo")
  out = _emit(app, tmp_path)
  instr = open(os.path.join(out, "agents", "Steering_Host", "instruction.txt")).read()
  # SAME <routing> block the single-agent router uses — one routing-SI style.
  assert "<routing>" in instr and "</routing>" in instr
  assert 'flow="tracking" — the caller wants to track a shipment' in instr
  assert 'flow="pickup" — the caller wants to schedule a pickup' in instr
  assert "Route SILENTLY" in instr


def test_host_instruction_falls_back_to_taskflow_without_descriptions(tmp_path):
  out = _emit(_bella_like(), tmp_path)  # agents carry no description
  instr = open(os.path.join(out, "agents", "Steering_Host", "instruction.txt")).read()
  assert "<routing>" not in instr  # unchanged config_id taskflow
  assert "SILENT ROUTING" in instr


def test_agent_descriptions_thread_into_specialist_classifier_configs():
  # The mid-flow intent-first classifier judges a sibling switch by meaning: each
  # specialist config carries flow_descriptions so the SAME descriptions drive both the
  # host routing SI and the in-flow switch classifier.
  from flows.authoring import build as _build
  tracking = _agent("Tracking_Agent", "tracking", description="track a shipment")
  pickup = _agent("Pickup_Agent", "pickup", description="schedule a pickup")
  host = flows.HostRouter("Steering_Host",
                          routes={"tracking": tracking, "pickup": pickup})
  app = flows.App(host=host, agents=[tracking, pickup], app_display_name="D")
  all_map, *_ = _build.assemble_for_lint(app)
  assert all_map["tracking"]["flow_descriptions"] == {
      "tracking": "track a shipment", "pickup": "schedule a pickup"}


def test_no_agent_descriptions_means_no_flow_descriptions_key():
  from flows.authoring import build as _build
  all_map, *_ = _build.assemble_for_lint(_bella_like())
  assert "flow_descriptions" not in all_map["tracking"]  # byte-identical no-op


# --- transfer (receptionist) shape ------------------------------------------
def test_transfer_host_is_a_non_slot_filling_router(tmp_path):
  out = _emit(_bella_like(), tmp_path)
  host = _load(out, "agents", "Steering_Host", "Steering_Host.json")
  assert host["tools"] == ["end_session", "set_active_flow"]
  assert host["childAgents"] == ["Tracking_Agent", "Pickup_Agent"]
  # A router runs NO engine: only custom before_model + after_tool, no before_agent.
  assert "beforeModelCallbacks" in host and "afterToolCallbacks" in host
  assert "beforeAgentCallbacks" not in host
  assert "afterModelCallbacks" not in host
  assert "slot_filling_engine" not in host["tools"]


def test_app_json_maps_sub_agents_only(tmp_path):
  out = _emit(_bella_like(), tmp_path)
  appj = _load(out, "app.json")
  assert appj["rootAgent"] == "Steering_Host"
  acm = json.loads(
      next(v for v in appj["variableDeclarations"]
           if v["name"] == "agent_config_map")["schema"]["default"])
  assert acm == {"Tracking_Agent": "tracking", "Pickup_Agent": "pickup"}
  # transfer host runs no engine → no default_config_id (matches bella_notte).
  assert not any(v["name"] == "default_config_id" for v in appj["variableDeclarations"])


def test_sub_agents_are_full_slot_filling_agents(tmp_path):
  out = _emit(_bella_like(), tmp_path)
  sub = _load(out, "agents", "Tracking_Agent", "Tracking_Agent.json")
  tools = set(sub["tools"])
  assert {"slot_filling_engine", "slot_intake", "tracking_dag", "do_tracking",
          "set_tracking_ref", "set_active_flow", "end_session"} <= tools
  # all 4 framework callbacks
  for key in ("beforeAgentCallbacks", "beforeModelCallbacks",
              "afterToolCallbacks", "afterModelCallbacks"):
    assert key in sub


def test_set_active_flow_routes_to_target_agent(tmp_path):
  out = _emit(_bella_like(), tmp_path)
  src = open(os.path.join(out, "tools", "set_active_flow",
                          "python_function", "python_code.py")).read()
  assert "_FLOW_TO_AGENT" in src
  # repr renders single quotes; assert the route pairs are present (quote-agnostic).
  assert "'tracking': 'Tracking_Agent'" in src
  assert "'pickup': 'Pickup_Agent'" in src
  assert "target_agent" in src


def test_every_sub_agent_can_transfer_to_a_sibling(tmp_path):
  out = _emit(_bella_like(), tmp_path)
  for name in ("Tracking_Agent", "Pickup_Agent"):
    sub = _load(out, "agents", name, f"{name}.json")
    assert "set_active_flow" in sub["tools"], name


def test_sub_agent_dag_carries_flow_types_for_switch_backstop(tmp_path, dag_config):
  # flow_types = all route keys drives the engine's deterministic switch backstop,
  # so "I want <sibling> instead" mid-flow injects set_active_flow BEFORE the model
  # (else the model cancels). Verified e2e; this locks the config in.
  out = _emit(_bella_like(), tmp_path)
  src = open(os.path.join(out, "tools", "tracking_dag",
                          "python_function", "python_code.py")).read()
  cfg = dag_config(src, "tracking")
  assert "tracking" in cfg["flow_types"] and "pickup" in cfg["flow_types"]


def test_framework_in_sync(tmp_path):
  out = _emit(_bella_like(), tmp_path)
  report = _bs.verify_app_dir(out)
  assert report.ok, report.summary()


def test_per_agent_tool_scoping_isolates_flows(tmp_path):
  # A @flows.tool scoped to one flow must NOT leak onto the sibling agent.
  _tools.clear_registry()
  try:
    @flows.tool(flow="tracking")
    def track_lookup(ref: str = "") -> dict:
      """Look up tracking."""
      return {"result": "ok", "success": True}

    trk = flows.Flow("tracking", root_agent="Tracking_Agent")
    trk.add(flows.user_slot("tn", "tn?"),
            flows.result_slot("r", "t"),
            flows.announce("d", ["{r}"], requires=["r"], end=True))
    trk.task("t", "track_lookup", ["tn"], "r", out_key="result",
             condition=flows.has("tn"))
    pkp = flows.Flow("pickup", root_agent="Pickup_Agent")
    pkp.add(flows.user_slot("addr", "addr?"), flows.announce("d", ["ok"], end=True))

    tracking = flows.Agent("Tracking_Agent", flow=trk)
    pickup = flows.Agent("Pickup_Agent", flow=pkp)
    host = flows.HostRouter("Host", routes={"track": tracking, "pick": pickup})
    app = flows.App(host=host, agents=[tracking, pickup], app_display_name="Scoping")

    out = str(tmp_path / "app")
    res = flows.build_app(app, out)
    assert res.ok, res.validation.errors if res.validation else res.error
    tj = set(_load(out, "agents", "Tracking_Agent", "Tracking_Agent.json")["tools"])
    pj = set(_load(out, "agents", "Pickup_Agent", "Pickup_Agent.json")["tools"])
    assert "track_lookup" in tj
    assert "track_lookup" not in pj
  finally:
    _tools.clear_registry()


# --- host tool scoping (HostRouter.extra_tools / App.extra_agent_tools) ------
def _faq_app(tmp_path, *, on_host=None, on_app=None, flow=None, strategy="transfer"):
  """A two-specialist app whose HOST also answers an FAQ with `faq_lookup`."""

  @flows.tool(flow=flow)
  def faq_lookup(question: str = "") -> dict:
    """Answer a question from the FAQ corpus."""
    _CORPUS = {"cost": "A freeze is free."}
    return {"success": True, "answer": _CORPUS.get(question, "")}

  tracking = _agent("Tracking_Agent", "tracking")
  pickup = _agent("Pickup_Agent", "pickup")
  host = flows.HostRouter("Steering_Host",
                          routes={"tracking": tracking, "pickup": pickup},
                          strategy=strategy,
                          extra_tools=list(on_host or []))
  return flows.App(host=host, agents=[tracking, pickup],
                   app_display_name="FAQ Host",
                   extra_agent_tools=list(on_app or []))


def test_host_extra_tools_are_listed_and_emitted(tmp_path):
  """REGRESSION: a host router could carry NO tool beyond the routing pair.

  The host is the agent that talks to the caller before any transfer, so the FAQ
  answer and the front-door classification are its job — and neither is a flow's
  tool, so scoping them was impossible (`_host_tools` returned a hard-coded pair and
  `App.extra_agent_tools` was single-agent-only). Listing alone is not enough either:
  the BODY has to be emitted, or the host calls a tool the app does not contain.
  """
  _tools.clear_registry()
  try:
    app = _faq_app(tmp_path, on_host=["faq_lookup"])
    out = _emit(app, tmp_path)

    hostj = _load(out, "agents", "Steering_Host", "Steering_Host.json")
    assert hostj["tools"] == ["end_session", "faq_lookup", "set_active_flow"]
    # ... and the body is on disk, not just the name in tools[].
    body = open(os.path.join(
        out, "tools", "faq_lookup", "python_function", "python_code.py")).read()
    assert "def faq_lookup(" in body and "A freeze is free." in body
    assert os.path.isfile(os.path.join(out, "tools", "faq_lookup", "faq_lookup.json"))
    # Scoping stays per-agent: a host tool does not leak onto the specialists.
    for name in ("Tracking_Agent", "Pickup_Agent"):
      assert "faq_lookup" not in _load(out, "agents", name, f"{name}.json")["tools"]
  finally:
    _tools.clear_registry()


def test_host_extra_tool_body_is_pulled_in_by_name(tmp_path):
  """A host tool belongs to no flow of THIS app — flow attachment can't emit it.

  `collect_tools` gathers bodies by flow, so a shared tool registered against another
  app's flow was silently skipped: tools[] named it, `tools/<name>/` was absent. It is
  now pulled in by name (and the missing-body case is a build error, below).
  """
  _tools.clear_registry()
  try:
    app = _faq_app(tmp_path, on_host=["faq_lookup"], flow="some_other_app_flow")
    out = _emit(app, tmp_path)
    assert "faq_lookup" in _load(
        out, "agents", "Steering_Host", "Steering_Host.json")["tools"]
    assert os.path.isfile(os.path.join(
        out, "tools", "faq_lookup", "python_function", "python_code.py"))
  finally:
    _tools.clear_registry()


def test_app_extra_agent_tools_reach_the_multi_agent_host(tmp_path):
  """App-level extras are the ROUTER's, the same rule `App.remote_agents` follows."""
  _tools.clear_registry()
  try:
    out = _emit(_faq_app(tmp_path, on_app=["faq_lookup"]), tmp_path)
    assert "faq_lookup" in _load(
        out, "agents", "Steering_Host", "Steering_Host.json")["tools"]
  finally:
    _tools.clear_registry()


def test_engine_host_takes_extra_tools_too(tmp_path):
  _tools.clear_registry()
  try:
    out = _emit(_faq_app(tmp_path, on_host=["faq_lookup"], strategy="engine"), tmp_path)
    hostj = _load(out, "agents", "Steering_Host", "Steering_Host.json")
    assert "faq_lookup" in hostj["tools"]
    assert "slot_filling_engine" in hostj["tools"]  # the engine host keeps its own set
  finally:
    _tools.clear_registry()


def test_host_extra_tools_accept_a_framework_tool(tmp_path):
  """A blessed framework tool has no body of ours but IS available to call."""
  app = _bella_like()
  app.host.extra_tools = ["transfer_to_human"]
  out = _emit(app, tmp_path)
  assert _load(out, "agents", "Steering_Host", "Steering_Host.json")["tools"] == [
      "end_session", "set_active_flow", "transfer_to_human"]


def test_host_extra_tool_with_no_body_is_a_build_error(tmp_path):
  """Fail the build, not the call: a name with nothing behind it is invisible until
  the model tries to use it mid-call."""
  app = _bella_like()
  app.host.extra_tools = ["credit_freeze_KB"]
  with pytest.raises(ValueError, match="no tool to call"):
    flows.validate_app(app)
  with pytest.raises(ValueError, match="credit_freeze_KB"):
    flows.build_app(app, str(tmp_path / "app"))


def test_host_tools_unchanged_when_no_extras(tmp_path):
  """Default behavior is untouched by the new field."""
  out = _emit(_bella_like(), tmp_path)
  assert _load(out, "agents", "Steering_Host", "Steering_Host.json")["tools"] == [
      "end_session", "set_active_flow"]
  assert flows.HostRouter("H", routes={"a": _agent("A", "aa")}).extra_tools == []


# --- per-specialist tool scoping (Agent.extra_tools) -------------------------
def _faq_specialists(tmp_path, *, on_agents=(), flow=None):
  """Two specialists; `on_agents` names the ones that also carry `faq_lookup`."""

  @flows.tool(flow=flow)
  def faq_lookup(question: str = "") -> dict:
    """Answer a question from the FAQ corpus."""
    _CORPUS = {"cost": "A freeze is free."}
    return {"success": True, "answer": _CORPUS.get(question, "")}

  tracking = _agent("Tracking_Agent", "tracking")
  pickup = _agent("Pickup_Agent", "pickup")
  for ag in (tracking, pickup):
    if ag.name in on_agents:
      ag.extra_tools = ["faq_lookup"]
  host = flows.HostRouter("Steering_Host",
                          routes={"tracking": tracking, "pickup": pickup})
  return flows.App(host=host, agents=[tracking, pickup], app_display_name="FAQ Agents")


def test_agent_extra_tools_are_listed_and_emitted(tmp_path):
  """REGRESSION: a specialist could carry NO tool beyond the ones its flow references.

  The symmetric half of `HostRouter.extra_tools`. A caller mid-journey can ask the
  question the host would have answered — but the knowledge tool behind it is fired by
  no task and set by no slot, so before this there was no way to scope it onto the
  journeys that offer it, and the behavior was simply lost.
  """
  _tools.clear_registry()
  try:
    app = _faq_specialists(tmp_path, on_agents=("Tracking_Agent",))
    out = _emit(app, tmp_path)

    assert "faq_lookup" in _load(
        out, "agents", "Tracking_Agent", "Tracking_Agent.json")["tools"]
    # Per-agent means per-agent: the sibling and the router do NOT get it.
    assert "faq_lookup" not in _load(
        out, "agents", "Pickup_Agent", "Pickup_Agent.json")["tools"]
    assert "faq_lookup" not in _load(
        out, "agents", "Steering_Host", "Steering_Host.json")["tools"]
    # ... and the BODY is on disk, not just the name in tools[].
    body = open(os.path.join(
        out, "tools", "faq_lookup", "python_function", "python_code.py")).read()
    assert "def faq_lookup(" in body and "A freeze is free." in body
    assert os.path.isfile(os.path.join(out, "tools", "faq_lookup", "faq_lookup.json"))
  finally:
    _tools.clear_registry()


def test_agent_extra_tool_body_is_pulled_in_by_name(tmp_path):
  """A specialist's extra tool belongs to no flow of THIS app — flow attachment cannot
  emit it, so it is pulled in by name exactly as the host's is."""
  _tools.clear_registry()
  try:
    app = _faq_specialists(tmp_path, on_agents=("Tracking_Agent",),
                           flow="some_other_app_flow")
    out = _emit(app, tmp_path)
    assert "faq_lookup" in _load(
        out, "agents", "Tracking_Agent", "Tracking_Agent.json")["tools"]
    assert os.path.isfile(os.path.join(
        out, "tools", "faq_lookup", "python_function", "python_code.py"))
  finally:
    _tools.clear_registry()


def test_two_specialists_can_share_one_extra_tool(tmp_path):
  """The Equifax shape: `credit_freeze_KB` on Auth + Freeze_Action, nothing else."""
  _tools.clear_registry()
  try:
    app = _faq_specialists(tmp_path, on_agents=("Tracking_Agent", "Pickup_Agent"))
    out = _emit(app, tmp_path)
    for name in ("Tracking_Agent", "Pickup_Agent"):
      assert "faq_lookup" in _load(out, "agents", name, f"{name}.json")["tools"]
    assert "faq_lookup" not in _load(
        out, "agents", "Steering_Host", "Steering_Host.json")["tools"]
  finally:
    _tools.clear_registry()


def test_agent_extra_tools_accept_a_framework_tool(tmp_path):
  """A blessed framework tool has no body of ours but IS available to call."""
  app = _bella_like()
  app.agents[0].extra_tools = ["try_again"]
  out = _emit(app, tmp_path)
  assert "try_again" in _load(
      out, "agents", "Tracking_Agent", "Tracking_Agent.json")["tools"]
  assert "try_again" not in _load(
      out, "agents", "Pickup_Agent", "Pickup_Agent.json")["tools"]


def test_agent_extra_tool_with_no_body_is_a_build_error(tmp_path):
  """Fail the build, not the call — the same rule the host's extras follow, and the
  error says WHICH agent so a five-specialist app is diagnosable."""
  app = _bella_like()
  app.agents[1].extra_tools = ["credit_freeze_KB"]
  with pytest.raises(ValueError, match="no tool to call"):
    flows.validate_app(app)
  with pytest.raises(ValueError, match="Pickup_Agent extra tools"):
    flows.build_app(app, str(tmp_path / "app"))


def test_agent_tools_unchanged_when_no_extras(tmp_path):
  """Default behavior is untouched by the new field."""
  out = _emit(_bella_like(), tmp_path)
  assert "faq_lookup" not in _load(
      out, "agents", "Tracking_Agent", "Tracking_Agent.json")["tools"]
  assert flows.Agent("A", flow=flows.Flow("aa", root_agent="A")).extra_tools == []


# --- engine (config-swap) shape ---------------------------------------------
def test_engine_strategy_host_runs_the_engine(tmp_path):
  tracking = _agent("Tracking_Agent", "tracking")
  pickup = _agent("Pickup_Agent", "pickup")
  host = flows.HostRouter("Steering_Host",
                          routes={"tracking": tracking, "pickup": pickup},
                          strategy="engine")
  app = flows.App(host=host, agents=[tracking, pickup], app_display_name="Engine Demo")
  out = _emit(app, tmp_path)

  hostj = _load(out, "agents", "Steering_Host", "Steering_Host.json")
  assert "slot_filling_engine" in hostj["tools"]
  assert "steering_host_router_dag" in hostj["tools"]
  for key in ("beforeAgentCallbacks", "beforeModelCallbacks",
              "afterToolCallbacks", "afterModelCallbacks"):
    assert key in hostj

  appj = _load(out, "app.json")
  acm = json.loads(next(v for v in appj["variableDeclarations"]
                        if v["name"] == "agent_config_map")["schema"]["default"])
  assert acm["Steering_Host"] == "steering_host_router"  # host IS in the map
  dci = next(v for v in appj["variableDeclarations"]
             if v["name"] == "default_config_id")["schema"]["default"]
  assert dci == "steering_host_router"
  # engine host uses canonical callbacks -> strict drift check still passes.
  assert _bs.verify_app_dir(out).ok


# --- guardrails --------------------------------------------------------------
def test_route_to_undeclared_agent_raises(tmp_path):
  tracking = _agent("Tracking_Agent", "tracking")
  ghost = _agent("Ghost_Agent", "ghost")
  host = flows.HostRouter("Host", routes={"t": tracking, "g": ghost})
  app = flows.App(host=host, agents=[tracking], app_display_name="Bad")  # ghost missing
  with pytest.raises(ValueError, match="not in agents"):
    flows.build_app(app, str(tmp_path / "app"))


def test_duplicate_aliases_across_agents_rejected():
  a = flows.Agent("A_Agent", flow=_agent("A_Agent", "aa").flow, aliases=["gift card"])
  b = flows.Agent("B_Agent", flow=_agent("B_Agent", "bb").flow, aliases=["gift card"])
  host = flows.HostRouter("H", routes={"aa": a, "bb": b})
  app = flows.App(host=host, agents=[a, b], app_display_name="Dup")
  with pytest.raises(ValueError, match="overlapping route phrasing"):
    flows.validate_app(app)


@pytest.mark.parametrize("strategy", ["engine", "transfer"])
def test_host_uses_custom_welcome_message(tmp_path, strategy):
  # welcome_message must reach the greeting the host actually speaks — its
  # instruction (system instruction), since the engine bypasses a router's welcome
  # slot and the transfer host has no engine at all.
  a = _agent("Tracking_Agent", "tracking")
  b = _agent("Pickup_Agent", "pickup")
  host = flows.HostRouter("Steering_Host", routes={"tracking": a, "pickup": b},
                          strategy=strategy, welcome_message="Welcome to Acme Shipping!")
  app = flows.App(host=host, agents=[a, b], app_display_name="Welcome " + strategy)
  out = _emit(app, tmp_path)
  instr = open(os.path.join(out, "agents", "Steering_Host", "instruction.txt")).read()
  assert "Welcome to Acme Shipping!" in instr


def test_app_rejects_mixing_single_and_multi():
  f = flows.Flow("x", root_agent="X")
  a = flows.Agent("A", flow=flows.Flow("a", root_agent="A"))
  with pytest.raises(ValueError, match="not both"):
    flows.App(root_flow=f, host=flows.HostRouter("H", routes={"a": a}),
              agents=[a], app_display_name="X")


# --- single-agent back-compat ------------------------------------------------
def test_single_agent_unchanged(tmp_path):
  f = flows.Flow("solo", root_agent="Solo_Agent")
  f.add(flows.user_slot("x", "x?"), flows.announce("d", ["ok"], end=True))
  app = flows.App(root_flow=f, app_display_name="Solo")
  assert app.is_multi_agent is False
  out = _emit(app, tmp_path)
  # no host, no author callbacks, single agent dir.
  assert os.path.isdir(os.path.join(out, "agents", "Solo_Agent"))
  assert not os.path.exists(os.path.join(
      out, "agents", "Solo_Agent", "before_agent_callbacks",
      "before_agent_callbacks_00"))
  assert _bs.verify_app_dir(out).ok


# --- single-agent parity for App.extra_agent_tools ---------------------------
#
# The multi-agent path pulls agent-scoped extras in BY NAME and then checks they
# resolve (`_assemble_multi`); the single-agent path did neither. It only appeared to
# work because a `@flows.tool` with no `flows=` attaches to EVERY flow and so came
# along regardless — the two shapes that do not, below, both fell off the end.


def _solo_faq_app(*, flow=None, register=True) -> flows.App:
  """A one-agent app whose agent also answers an FAQ with `faq_lookup`."""
  if register:

    @flows.tool(flow=flow)
    def faq_lookup(question: str = "") -> dict:
      """Answer a question from the FAQ corpus."""
      _CORPUS = {"cost": "A freeze is free."}
      return {"success": True, "answer": _CORPUS.get(question, "")}

  f = flows.Flow("solo", root_agent="Solo_Agent")
  f.add(flows.user_slot("x", "x?"), flows.announce("d", ["ok"], end=True))
  return flows.App(root_flow=f, app_display_name="Solo FAQ",
                   extra_agent_tools=["faq_lookup"])


def test_single_agent_extra_tool_body_is_pulled_in_by_name(tmp_path):
  """REGRESSION: a shared tool registered against ANOTHER app's flow was dropped.

  `collect_tools` gathers bodies by flow, and the single-agent assembly passed no
  `names=`, so `tools[]` listed `faq_lookup` while `tools/faq_lookup/` was absent —
  surfacing only as the post-emit `agent lists a tool the app does not contain`,
  after the whole tree had been built and then deleted. The multi-agent path
  (`test_host_extra_tool_body_is_pulled_in_by_name`) already handled this.
  """
  _tools.clear_registry()
  try:
    out = _emit(_solo_faq_app(flow="some_other_app_flow"), tmp_path)
    assert "faq_lookup" in _load(
        out, "agents", "Solo_Agent", "Solo_Agent.json")["tools"]
    body = open(os.path.join(
        out, "tools", "faq_lookup", "python_function", "python_code.py")).read()
    assert "def faq_lookup(" in body and "A freeze is free." in body
  finally:
    _tools.clear_registry()


def test_single_agent_extra_tool_with_no_body_is_a_build_error(tmp_path):
  """Same failure, same message, as the multi-agent host: fail the build, not the call."""
  _tools.clear_registry()
  try:
    app = _solo_faq_app(register=False)
    with pytest.raises(ValueError, match="no tool to call"):
      flows.validate_app(app)
    with pytest.raises(ValueError, match="faq_lookup"):
      flows.build_app(app, str(tmp_path / "app"))
  finally:
    _tools.clear_registry()


def test_single_agent_extra_tools_accept_a_framework_tool(tmp_path):
  """A blessed framework tool has no body of ours but IS available to call."""
  _tools.clear_registry()
  try:
    f = flows.Flow("solo", root_agent="Solo_Agent")
    f.add(flows.user_slot("x", "x?"), flows.announce("d", ["ok"], end=True))
    app = flows.App(root_flow=f, app_display_name="Solo",
                    extra_agent_tools=["transfer_to_human"])
    out = _emit(app, tmp_path)
    assert "transfer_to_human" in _load(
        out, "agents", "Solo_Agent", "Solo_Agent.json")["tools"]
  finally:
    _tools.clear_registry()
