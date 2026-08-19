"""A2A remote agents: card construction, body-less emission, scoping, and the
guards that stop a remote agent being wired into a task the way an ordinary tool is.

The wire shapes asserted here (the tool's `task`/`contextId` parameters, and the
`message` / `task` kinds of reply) were taken from live calls against the reference
app `[REFERENCE] A2A Protocol Inbound and Outbound`, not from the proto alone — the
`{"result": "pending"}` placeholder in particular is not in the proto.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_a2a.py
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

import flows
from flows.authoring import a2a as _a2a
from flows.authoring import tools as _tools
from flows.engine import loader as _loader


@pytest.fixture(autouse=True)
def _clean_registry():
  """`delegate` registers its generated unwrap tool process-wide, like `@flows.tool`.

  Snapshot and restore rather than clear: other test modules register their tools at
  IMPORT time, and pytest imports every module before running any test, so clearing
  would delete registrations those tests still need.
  """
  saved = dict(_tools._REGISTRY)
  yield
  _tools._REGISTRY.clear()
  _tools._REGISTRY.update(saved)


def _agent(name="wx", **kw):
  kw.setdefault("description", "Weather answers.")
  kw.setdefault("url", "https://wx.example.com/a2a/v1")
  kw.setdefault("skills", [flows.agent_skill("get_weather")])
  return flows.remote_agent(name, **kw)


def _app(*, tasks=(), slots=(), remote_agents=(), **appkw):  # noqa: D401
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("q", ask="What?"), *slots)
  if tasks:
    flow.task(*tasks)
  return flows.App(
      root_flow=flow, app_display_name="T", remote_agents=list(remote_agents), **appkw)


# --- Card construction ------------------------------------------------------


def test_agent_card_shape_matches_the_api():
  card = _agent(
      "svc",
      description="Creates cases.",
      url="https://x.example.com/a2a",
      version="2.1.0",
      tenant="acme",
      skills=[flows.agent_skill(
          "create_case", name="Create case", description="Open a case.",
          tags=["support"], input_modes=["application/json"])],
  ).agent_card()
  assert card == {
      "name": "svc",
      "description": "Creates cases.",
      "supportedInterfaces": [{
          "url": "https://x.example.com/a2a",
          "protocolBinding": "HTTP+JSON",
          "protocolVersion": "1.0",
          "tenant": "acme",
      }],
      "version": "2.1.0",
      "skills": [{
          "id": "create_case",
          "name": "Create case",
          "description": "Open a case.",
          "tags": ["support"],
          "inputModes": ["application/json"],
      }],
  }


def test_optional_card_fields_are_omitted_not_emitted_empty():
  """A pulled card carries no empty repeated fields; an emitted one must not either,
  or every round-trip shows a spurious diff."""
  card = _agent().agent_card()
  assert "tenant" not in card["supportedInterfaces"][0]
  skill = card["skills"][0]
  assert set(skill) == {"id", "name", "description", "tags"}


def test_skill_name_and_description_default_to_the_id():
  skill = flows.agent_skill("get_weather")
  assert (skill.name, skill.description) == ("get_weather", "get_weather")


def test_card_name_can_differ_from_the_tool_name():
  ra = _agent("wx-tool", card_name="Weather", card_description="The weather service.")
  assert ra.agent_card()["name"] == "Weather"
  assert ra.agent_card()["description"] == "The weather service."
  assert ra.tool_payload()["name"] == "wx-tool"


def test_ces_agent_resolves_eagerly_when_nothing_is_left_to_inherit():
  ra = flows.ces_agent(
      "peer", description="A peer CXAS app.", project="google.com:proj",
      location="us", app_id="abc123", skills=[flows.agent_skill("s")])
  assert ra.is_resolved
  assert ra.url == (
      "https://ces.googleapis.com/v1/projects/google.com:proj/locations/us/apps/abc123")


def test_a_project_without_a_location_still_defers():
  """Defaulting the location to "us" here would point an app deployed in eu at the
  wrong region; the app's own location is the only safe answer."""
  ra = flows.ces_agent("peer", description="A peer.", app_id="abc123",
                       project="google.com:elsewhere")
  assert not ra.is_resolved
  out = _build(_app(remote_agents=[ra], gcp_project="google.com:mine", location="eu"))
  card = json.load(open(os.path.join(out, "tools", "peer", "peer.json")))
  assert card["remoteAgentTool"]["agentCard"]["supportedInterfaces"][0]["url"] == (
      "https://ces.googleapis.com/v1/projects/google.com:elsewhere/locations/eu/apps/abc123")


@pytest.mark.parametrize("kwargs, fragment", [
    ({"name": "has space"}, "letters, digits"),
    ({"url": "http://x.example.com/a2a"}, "https://"),
    ({"url": ""}, "url is required"),
    ({"description": ""}, "description is required"),
    ({"version": " "}, "version is required"),
])
def test_invalid_cards_are_rejected_at_construction(kwargs, fragment):
  """A card CES rejects fails a push that has already overwritten the target app, so
  the check belongs at construction."""
  name = kwargs.pop("name", "wx")
  with pytest.raises(ValueError, match=fragment):
    _agent(name, **kwargs)


def test_ces_agent_requires_an_app_id():
  with pytest.raises(ValueError, match="app_id"):
    flows.ces_agent("peer", description="A peer.", app_id="")


# --- Defaults ---------------------------------------------------------------


def test_version_and_protocol_default_without_being_passed():
  card = flows.remote_agent(
      "wx", description="Weather.", url="https://wx.example.com/a2a").agent_card()
  assert card["version"] == "1.0.0"
  assert card["supportedInterfaces"][0]["protocolBinding"] == "HTTP+JSON"
  assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"


def test_a_single_skill_is_derived_from_the_name_and_description():
  """The API requires a skill; for a single-purpose agent the name and description
  already say what it does, so it should not have to be written twice."""
  card = flows.remote_agent(
      "wx", description="Answers weather questions.",
      url="https://wx.example.com/a2a").agent_card()
  assert card["skills"] == [{
      "id": "wx", "name": "wx",
      "description": "Answers weather questions.", "tags": [],
  }]


def test_explicit_skills_are_not_replaced_by_the_derived_one():
  card = _agent(skills=[flows.agent_skill("a"), flows.agent_skill("b")]).agent_card()
  assert [s["id"] for s in card["skills"]] == ["a", "b"]


def test_ces_agent_inherits_the_apps_project_and_location():
  ra = flows.ces_agent("peer", description="A peer CXAS app.", app_id="abc123")
  assert not ra.is_resolved
  out = _build(flows.App(
      root_flow=flows.Flow("t", root_agent="Root Agent").add(
          flows.user_slot("q", ask="What?")),
      app_display_name="T", gcp_project="google.com:my-proj", location="eu",
      remote_agents=[ra]))
  card = json.load(open(os.path.join(out, "tools", "peer", "peer.json")))
  assert card["remoteAgentTool"]["agentCard"]["supportedInterfaces"][0]["url"] == (
      "https://ces.googleapis.com/v1/projects/google.com:my-proj/locations/eu/apps/abc123")


def test_an_explicit_project_beats_the_apps():
  ra = flows.ces_agent("peer", description="A peer.", app_id="abc123",
                       project="google.com:elsewhere")
  out = _build(_app(remote_agents=[ra], gcp_project="google.com:my-proj"))
  card = json.load(open(os.path.join(out, "tools", "peer", "peer.json")))
  assert "google.com:elsewhere" in (
      card["remoteAgentTool"]["agentCard"]["supportedInterfaces"][0]["url"])


def test_one_declaration_can_be_built_into_two_projects():
  """Resolution returns a new instance rather than mutating, so a shared declaration
  does not carry the first app's project into the second."""
  ra = flows.ces_agent("peer", description="A peer.", app_id="abc123")
  for project in ("google.com:one", "google.com:two"):
    out = _build(_app(remote_agents=[ra], gcp_project=project))
    card = json.load(open(os.path.join(out, "tools", "peer", "peer.json")))
    assert project in card["remoteAgentTool"]["agentCard"]["supportedInterfaces"][0]["url"]
  assert not ra.is_resolved


def test_an_unresolved_agent_asked_for_its_card_says_what_is_missing():
  ra = flows.ces_agent("peer", description="A peer.", app_id="abc123")
  with pytest.raises(ValueError, match="no endpoint yet"):
    ra.agent_card()


def test_skills_must_be_built_with_agent_skill():
  with pytest.raises(ValueError, match="flows.agent_skill"):
    _agent(skills=[{"id": "get_weather"}])


def test_remote_agents_must_be_built_with_remote_agent():
  with pytest.raises(ValueError, match="flows.remote_agent"):
    flows.validate_app(_app(remote_agents=[{"name": "wx"}]))


def test_two_different_agents_under_one_name_is_an_error():
  other = _agent("wx", url="https://other.example.com/a2a")
  with pytest.raises(ValueError, match="both named 'wx'"):
    flows.validate_app(_app(remote_agents=[_agent("wx"), other]))


def test_the_same_agent_declared_twice_is_fine():
  """The host and a sub-agent legitimately share one; only one resource is emitted."""
  ra = _agent()
  errors, _warnings = flows.validate_app(_app(remote_agents=[ra, ra]))
  assert errors == []
  out = _build(_app(remote_agents=[ra, ra]))
  assert os.path.exists(os.path.join(out, "tools", "wx", "wx.json"))


# --- Emission ---------------------------------------------------------------


def _build(app):
  tmp = tempfile.mkdtemp(prefix="flows_a2a_")
  out = os.path.join(tmp, "app")
  res = flows.build_app(app, out)
  assert res.ok, res.validation
  return out


def test_emits_a_remote_agent_tool_resource_with_no_python_body():
  """`remoteAgentTool` is a `tool_type` oneof sibling of `pythonFunction`: emitting a
  body alongside it would make the sandbox stub answer instead of the remote agent."""
  out = _build(_app(remote_agents=[_agent()]))
  doc = json.load(open(os.path.join(out, "tools", "wx", "wx.json")))
  assert doc["displayName"] == "wx"
  assert doc["executionType"] == "SYNCHRONOUS"
  assert doc["remoteAgentTool"]["agentCard"]["name"] == "wx"
  assert "pythonFunction" not in doc
  assert not os.path.exists(os.path.join(out, "tools", "wx", "python_function"))


def test_a_declared_remote_agent_is_scoped_onto_the_agent():
  """The reference pattern is a model-callable tool no task names, so flow scanning
  alone would emit the resource and leave the agent unable to call it."""
  out = _build(_app(remote_agents=[_agent()]))
  agent = json.load(open(os.path.join(out, "agents", "Root Agent", "Root Agent.json")))
  assert "wx" in agent["tools"]


def test_no_executor_stub_is_generated_for_a_remote_agent():
  ra = _agent()
  task = flows.task("call", "wx", ["q"], "out", out_key="message",
                    success_check="message")
  out = _build(_app(tasks=[task], slots=[flows.result_slot("out", "call")],
                    remote_agents=[ra]))
  assert not os.path.exists(os.path.join(out, "tools", "wx", "python_function"))


def test_an_app_without_remote_agents_emits_no_a2a_anything():
  out = _build(_app())
  for name in os.listdir(os.path.join(out, "tools")):
    doc_path = os.path.join(out, "tools", name, f"{name}.json")
    if os.path.exists(doc_path):
      assert "remoteAgentTool" not in json.load(open(doc_path))


def test_multi_agent_scopes_app_level_onto_host_and_agent_level_onto_the_sub_agent():
  host_ra, child_ra = _agent("host-agent"), _agent("child-agent")
  billing = flows.Flow("billing", root_agent="Billing")
  billing.add(flows.user_slot("acct", ask="Account?"))
  sub = flows.Agent(name="Billing", flow=billing, remote_agents=[child_ra])
  app = flows.App(
      app_display_name="M",
      host=flows.HostRouter(name="Router", routes={"billing": sub}),
      agents=[sub],
      remote_agents=[host_ra],
  )
  out = _build(app)
  host = json.load(open(os.path.join(out, "agents", "Router", "Router.json")))
  child = json.load(open(os.path.join(out, "agents", "Billing", "Billing.json")))
  assert "host-agent" in host["tools"] and "child-agent" not in host["tools"]
  assert "child-agent" in child["tools"]
  # Both resources are emitted once, regardless of who references them.
  for name in ("host-agent", "child-agent"):
    assert os.path.exists(os.path.join(out, "tools", name, f"{name}.json"))


# --- Guards on hand-rolled tasks --------------------------------------------


def _errors(**taskkw):
  taskkw.setdefault("out_key", "message")
  task = flows.task("call", "wx", ["q"], "out", **taskkw)
  return flows.validate_app(
      _app(tasks=[task], slots=[flows.result_slot("out", "call")],
           remote_agents=[_agent()]))


def test_default_success_check_is_an_error():
  """Neither kind of reply carries a `success` key, so intake reads every call as failed and — with
  max_retries defaulting to 0 — escalates the flow on the very first fire."""
  errors, _ = _errors(out_key="message", success_check="success")
  assert any("success_check='success'" in e and "escalate" in e for e in errors)


def test_an_output_key_that_is_not_an_arm_is_an_error():
  errors, _ = _errors(out_key="reply", success_check="message")
  assert any("['reply']" in e and "flat top-level key" in e for e in errors)


def test_succeeding_on_the_task_arm_warns_that_it_is_a_receipt():
  """A `task` reply is what an agent returns when it ACCEPTS work, so the task passes
  on a receipt rather than an answer."""
  _errors_, warnings = _errors(out_key="task", success_check="task")
  assert any("receipt" in w for w in warnings)


def test_the_task_arm_warning_does_not_recommend_awaits_as_the_fix():
  """`awaits` cannot bridge a `task` reply: the engine enters a wait only for CES's
  `{"result": "pending"}` placeholder (`_is_async_pending`), and a `task` reply is a real
  response it never sees as pending. Telling the author to add `awaits` would send
  them after a fix that silently does nothing."""
  _errors_, warnings = _errors(out_key="task", success_check="task")
  warning = next(w for w in warnings if "receipt" in w)
  assert "pending" in warning and "does not bridge" in warning
  assert "'message'" in warning


def test_the_task_arm_warning_still_fires_when_awaits_is_present():
  """Adding `awaits` does not make the shape correct, so it must not silence the
  warning — that combination is precisely the trap."""
  _e, warnings = _errors(
      out_key="task", success_check="task",
      awaits=flows.awaits(max_turns=3, on_timeout={"say": "No luck."}))
  assert any("receipt" in w for w in warnings)


def test_the_correct_hand_rolled_shape_is_clean():
  assert _errors(out_key="message", success_check="message") == ([], [])


def test_awaits_on_a_remote_agent_is_not_flagged_as_dead_config():
  """The async-pairing check warns when `awaits` rides a synchronous tool. A remote
  agent is neither — the platform picks per call — so a wait on one is prudent."""
  _e, warnings = _errors(
      out_key="message", success_check="message",
      awaits=flows.awaits(max_turns=3, on_timeout={"say": "No luck."}))
  assert not any("not asynchronous" in w for w in warnings)


# --- delegate() -------------------------------------------------------------


def _delegated_app():
  ra = _agent()
  d = flows.delegate("ask", ra, request_slot="q", reply_slot="answer",
                     then_say="{answer}", terminal=True)
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("q", ask="What?"), *d.slots)
  flow.task(*d.tasks)
  return d, flows.App(root_flow=flow, app_display_name="T", remote_agents=[ra])


def test_delegate_builds_a_valid_two_task_pair():
  d, app = _delegated_app()
  assert flows.validate_app(app) == ([], [])
  call, read = d.tasks
  # The remote agent's parameters are the platform's, so inputs use the dict form.
  assert call["inputs"] == {"q": _a2a.A2A_REQUEST_PARAM}
  assert call["success_check"] == _a2a.A2A_MESSAGE_REPLY
  assert call["outputs"] == {_a2a.A2A_MESSAGE_REPLY: "answer_envelope"}
  assert read["name"] == "ask_read"
  assert read["tool"] == "ask_unwrap"
  assert read["outputs"] == {"reply": "answer"}


def test_delegate_defaults_the_reply_slot_from_the_task_name():
  d = flows.delegate("ask_weather", _agent(), request_slot="q")
  assert d.reply_slot == "ask_weather_reply"
  assert d.envelope_slot == "ask_weather_reply_envelope"
  # The reply TASK must not collide with the reply SLOT.
  assert {t["name"] for t in d.tasks}.isdisjoint({s["name"] for s in d.slots})


def test_delegate_with_defaulted_slots_still_validates_and_builds():
  ra = _agent()
  d = flows.delegate("ask_weather", ra, request_slot="q", then_say="{ask_weather_reply}",
                     terminal=True)
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("q", ask="What?"), *d.slots)
  flow.task(*d.tasks)
  app = flows.App(root_flow=flow, app_display_name="T", remote_agents=[ra])
  assert flows.validate_app(app) == ([], [])
  _build(app)


def test_delegate_emits_its_unwrap_tool_and_no_body_for_the_remote_agent():
  _d, app = _delegated_app()
  out = _build(app)
  assert os.path.exists(
      os.path.join(out, "tools", "ask_unwrap", "python_function", "python_code.py"))
  assert not os.path.exists(os.path.join(out, "tools", "wx", "python_function"))


def _undeclared_delegation_app():
  """A delegation spliced into a flow whose App does NOT list the agent."""
  ra = _agent()
  d = flows.delegate("ask", ra, request_slot="q", reply_slot="answer",
                     then_say="{answer}", terminal=True)
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("q", ask="What?"), *d.slots)
  flow.task(*d.tasks)
  return flows.App(root_flow=flow, app_display_name="T")


def test_a_delegation_declares_its_agent_without_remote_agents():
  """Splicing a delegation is enough to declare the agent.

  Regression: it used to emit a python STUB under the agent's name — a generated
  "Record the value for wx." executor that answers in the remote agent's place. It
  built clean (no error, no warning) and deployed, so the substitution was invisible
  until someone talked to the app and the remote agent was never called.
  """
  out = _build(_undeclared_delegation_app())
  doc = json.load(open(os.path.join(out, "tools", "wx", "wx.json")))
  assert "remoteAgentTool" in doc, "the agent was emitted as a stub, not a card"
  assert "pythonFunction" not in doc
  assert not os.path.exists(os.path.join(out, "tools", "wx", "python_function"))


def test_an_undeclared_delegated_agent_is_still_scoped_onto_the_agent():
  out = _build(_undeclared_delegation_app())
  agent = json.load(open(os.path.join(out, "agents", "Root Agent", "Root Agent.json")))
  assert "wx" in agent["tools"]


def test_declaring_the_delegated_agent_as_well_is_still_one_resource():
  """Naming it in both places is the documented shape and must keep deduplicating."""
  ra = _agent()
  d = flows.delegate("ask", ra, request_slot="q", reply_slot="answer",
                     then_say="{answer}", terminal=True)
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("q", ask="What?"), *d.slots)
  flow.task(*d.tasks)
  out = _build(flows.App(root_flow=flow, app_display_name="T", remote_agents=[ra]))
  doc = json.load(open(os.path.join(out, "tools", "wx", "wx.json")))
  assert "remoteAgentTool" in doc


def test_a_delegated_agent_clashing_with_a_declared_one_is_an_error():
  """The harvested agent goes through the same one-name-one-resource funnel."""
  d = flows.delegate("ask", _agent(), request_slot="q", reply_slot="answer",
                     then_say="{answer}", terminal=True)
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("q", ask="What?"), *d.slots)
  flow.task(*d.tasks)
  other = _agent(url="https://other.example.com/a2a/v1")
  app = flows.App(root_flow=flow, app_display_name="T", remote_agents=[other])
  with pytest.raises(ValueError, match="two different remote agents"):
    _build(app)


def test_the_agent_reference_never_reaches_the_emitted_config():
  """It is an authoring-time object; in config it would be unserializable JSON."""
  d = flows.delegate("ask", _agent(), request_slot="q")
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("q", ask="What?"), *d.slots)
  flow.task(*d.tasks)
  cfg = flow.to_config()
  assert all("_remote_agent" not in t for t in cfg["tasks"])
  json.dumps(cfg)


def _gated_config(requires):
  """A delegation whose request slot is passive, so only `requires` holds the call."""
  ra = _agent()
  kw = {"requires": requires} if requires is not None else {}
  d = flows.delegate("ask", ra, request_slot="question", reply_slot="answer",
                     then_say="{answer}", terminal=True, **kw)
  flow = flows.Flow("j", root_agent="Root Agent")
  flow.add(flows.user_slot("identity", ask="Name on the account?"),
           flows.passive_slot("question"), *d.slots)
  flow.task(*d.tasks)
  return flow.to_config()


def _fires(config, filled):
  """The tool the engine dispatches this turn, if any."""
  engine = _loader.load_engine()
  sm = _loader.seed_sm(config)
  sm["filled"], sm["pending"] = dict(filled), {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "j"
    sm["filled"][gate] = "j"
  action = engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "ok",
      "scanned_user_text": "ok", "is_inactivity": False, "event_data": {},
      "config_id": "j", "n_user_turns": 1,
  })["action"]
  return (action.get("function_call") or {}).get("name")


def test_requires_gates_the_call_on_something_the_request_does_not_imply():
  """The reason to pass `requires`: hold the call until the caller is identified."""
  gated = _gated_config(["identity"])
  assert _fires(gated, {"question": "where is my parcel"}) is None
  assert _fires(gated, {"identity": "Rivera", "question": "where is my parcel"}) == "wx"
  # Ungated, the same request fires without the caller being identified at all.
  assert _fires(_gated_config(None), {"question": "where is my parcel"}) == "wx"


def test_an_explicit_requires_does_not_let_the_call_fire_without_its_input():
  """`requires` REPLACES the default `[request_slot]`, which reads like it drops the
  request as a prerequisite. It does not — a task never dispatches with an unfilled
  input, so the gate composes with the request instead of replacing it."""
  gated = _gated_config(["identity"])
  assert "question" not in (gated["tasks"][0].get("requires") or [])
  assert _fires(gated, {"identity": "Rivera"}) is None


def test_delegate_can_target_the_task_arm():
  """Without `expect=`, the envelope slot could only ever be filled from a `message`
  reply — which made the unwrap's task-reply parsing unreachable in practice."""
  d = flows.delegate("ask", _agent(), request_slot="q", expect=_a2a.A2A_TASK_REPLY)
  call = d.tasks[0]
  assert call["success_check"] == _a2a.A2A_TASK_REPLY
  assert call["outputs"] == {_a2a.A2A_TASK_REPLY: "ask_reply_envelope"}


def test_the_task_arm_path_reaches_the_unwraps_task_branch():
  """The end-to-end point of `expect=`: what the call task parks is what the generated
  unwrap then reads, so the task branch is live code on this path."""
  d = flows.delegate("ask", _agent(), request_slot="q", expect=_a2a.A2A_TASK_REPLY)
  parked_from = next(iter(d.tasks[0]["outputs"]))          # the reply kind intake stores
  assert parked_from == _a2a.A2A_TASK_REPLY
  completed = {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED",
                                      "message": {"content": [{"text": "Done."}]}}}
  assert _unwrap()(completed) == {"reply": "Done.", "state": "TASK_STATE_COMPLETED",
                                  "success": True}


def test_delegate_rejects_an_arm_that_is_not_one_of_the_two():
  with pytest.raises(ValueError, match="expect must be one of"):
    flows.delegate("ask", _agent(), request_slot="q", expect="result")


def test_delegate_sends_the_context_id_when_given_a_context_slot():
  ra = _agent()
  d = flows.delegate("ask", ra, request_slot="q", reply_slot="answer",
                     context_slot="ctx")
  assert d.tasks[0]["inputs"]["ctx"] == _a2a.A2A_CONTEXT_PARAM


def test_delegate_rejects_a_reply_slot_that_is_also_the_envelope_slot():
  with pytest.raises(ValueError, match="must differ"):
    flows.delegate("ask", _agent(), request_slot="q", reply_slot="a", envelope_slot="a")


def test_delegate_rejects_a_non_remote_agent():
  with pytest.raises(ValueError, match="flows.remote_agent"):
    flows.delegate("ask", "wx", request_slot="q", reply_slot="a")


def test_delegate_unpacks_as_slots_and_tasks():
  slots, tasks = flows.delegate("ask", _agent(), request_slot="q", reply_slot="a")
  assert len(slots) == 2 and len(tasks) == 2


# --- The generated unwrap tool, against the replies observed live --------------


def _unwrap():
  src = _a2a.unwrap_tool_source("read_reply", "env")
  ns: dict = {}
  exec(compile(src, "<generated>", "exec"), ns)
  return ns["read_reply"]


def test_unwrap_reads_the_message_arm():
  """Shape taken from a live call to a CXAS remote agent."""
  out = _unwrap()({"contextId": "c1",
                   "parts": [{"text": "The current weather in Boston is 72.0 F.\n"}]})
  assert out == {"reply": "The current weather in Boston is 72.0 F.",
                 "state": "", "success": True}


def test_unwrap_reads_a_completed_task_arm():
  out = _unwrap()({"id": "t1", "status": {
      "state": "TASK_STATE_COMPLETED",
      "message": {"content": [{"text": "It is 72 F."}]}}})
  assert out == {"reply": "It is 72 F.", "state": "TASK_STATE_COMPLETED",
                 "success": True}


def test_unwrap_reads_task_artifacts_when_the_status_carries_no_text():
  out = _unwrap()({"status": {"state": "TASK_STATE_COMPLETED"},
                   "artifacts": [{"parts": [{"text": "Report ready."}]}]})
  assert out["reply"] == "Report ready."


def test_unwrap_reports_an_unfinished_task_as_unsuccessful():
  """A SUBMITTED receipt has no answer in it; `success: False` routes it to the
  failure ladder rather than filling a slot with an empty string."""
  out = _unwrap()({"id": "t2", "status": {"state": "TASK_STATE_SUBMITTED"}})
  assert out == {"reply": "", "state": "TASK_STATE_SUBMITTED", "success": False}


@pytest.mark.parametrize("bad", [None, "pending", 7, [], {}])
def test_unwrap_survives_anything_that_is_not_an_envelope(bad):
  """CES substitutes `{"result": "pending"}` when it defers the call, which after_tool
  unwraps to the bare string "pending" — a crash here takes the whole intake down."""
  assert _unwrap()(bad) == {"reply": "", "state": "", "success": False}


def test_unwrap_source_is_sandbox_safe():
  """CES tools run in an isolated sandbox: stdlib/pydantic only, and no
  `from __future__ import annotations` (it breaks pydantic model creation there)."""
  src = _a2a.unwrap_tool_source("read_reply", "env")
  assert "from __future__" not in src
  assert "import requests" not in src and "import os" not in src


# --- task() dict inputs (what makes the platform's parameter names reachable) ---


def test_task_preserves_dict_inputs_and_defaults_requires_to_the_slots():
  t = flows.task("call", "wx", {"my_slot": "task"}, "out")
  assert t["inputs"] == {"my_slot": "task"}
  assert t["requires"] == ["my_slot"]


def test_task_still_accepts_a_list_of_slot_names():
  t = flows.task("call", "tool", ["a", "b"], "out")
  assert t["inputs"] == ["a", "b"]


def test_flow_task_accepts_several_prebuilt_dicts():
  flow = flows.Flow("t", root_agent="Root Agent")
  one = flows.task("a", "tool", ["x"], "o1")
  two = flows.task("b", "tool", ["y"], "o2")
  flow.task(one, two)
  assert [t["name"] for t in flow.to_config()["tasks"]] == ["a", "b"]


# --- source-registered tools ------------------------------------------------


def test_a_source_registered_tool_renders_verbatim():
  """`@flows.tool` introspects a function; a generated tool has source and no
  function, and must not get a second import header stapled on."""
  src = '"""Generated."""\nfrom typing import Any\n\n\ndef gen() -> dict:\n  return {}\n'
  spec = _tools.register_source_tool("gen", src, output_keys=["a"])
  assert _tools.render_tool(spec) == src
  assert _tools.collect_tools(["anything"])["gen"] == src
  assert _tools.registered_output_keys()["gen"] == ["a"]
