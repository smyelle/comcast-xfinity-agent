"""MCP toolsets: resource emission, wrapper generation, mocking, and the guards that
stop a toolset being wired up as if it were a tool.

An MCP toolset is the sibling of an OpenAPI one (see `test_openapi.py`) and shares its
wrapper/mock machinery through `toolset_common`, so this module tests the parts that are
MCP's own: the `mcpToolset` resource shape (a single file — an MCP server has no local
spec), the `$context.variables.*` custom-header contract, and the fact that a tool is
reachable ONLY through a declared `mcp_tool()` because there is nothing to derive one
from.

The generated wrapper is EXECUTED here against faked `context`/`tools` globals rather
than only string-matched, because it is emitted source that nothing else compiles: a
syntax error or a bad symbol would otherwise surface on the first live call.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_mcp.py
"""

from __future__ import annotations

import json
import os
import tempfile
import types

import pytest

import flows
from flows.authoring import mcp as _mcp
from flows.authoring import tools as _tools
from toolset_testkit import registry_snapshot, run_wrapper
from toolset_testkit import source as _source

SECRET = "projects/p/secrets/mcp-key/versions/3"
URL = "https://mcp.example.com/mcp/"


@pytest.fixture(autouse=True)
def _clean_registry():
  with registry_snapshot():
    yield


def _toolset(name="mcp_api", **kw):
  kw.setdefault("server_url", URL)
  return flows.mcp_toolset(name, **kw)


def _run(name, *, variables=None, live=None, symbol="mcp_api_get_thing"):
  return run_wrapper(name, symbol=symbol, variables=variables, live=live)


def _app(*, toolsets=(), tasks=(), slots=(), **kw):
  """A one-flow app. Each entry in `tasks` is the `flow.task(...)` argument tuple,
  optionally ending in a dict of keyword arguments."""
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("mdn", ask="Mobile number?"), *slots)
  for spec in tasks:
    args = list(spec)
    kwargs = args.pop() if args and isinstance(args[-1], dict) else {}
    flow.task(*args, **kwargs)
  return flows.App(
      root_flow=flow, app_display_name="T", toolsets=list(toolsets), **kw)


# --- The toolset resource ---------------------------------------------------


def test_toolset_emits_resource_with_no_spec_file():
  """An MCP toolset is one file — the server has no local spec to sit beside it."""
  ts = _toolset(description="Billing MCP.")
  app = _app(toolsets=[ts])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    resource = os.path.join(out, "toolsets/mcp_api/mcp_api.json")
    assert os.path.isfile(resource)
    # No OpenAPI-style second file, and nothing under tools/ for the toolset itself.
    assert not os.path.exists(os.path.join(out, "toolsets/mcp_api/open_api_toolset"))
    assert not os.path.exists(os.path.join(out, "tools/mcp_api"))
    doc = json.load(open(resource))
  assert doc["displayName"] == "mcp_api"
  assert doc["description"] == "Billing MCP."
  assert doc["mcpToolset"]["serverAddress"] == URL
  assert doc["name"] and doc["name"] != "mcp_api"  # a UUID, as every resource has


def test_server_url_is_emitted_verbatim():
  ts = _toolset(server_url="https://a.example.com/mcp/")
  assert ts.resource_body()["mcpToolset"]["serverAddress"] == "https://a.example.com/mcp/"


def test_oauth_auth_block_is_wrapped_in_mcp_toolset():
  """Auth is the shared apiAuthentication message — under `mcpToolset` here."""
  ts = _toolset(auth=flows.oauth_auth(
      client_id="svc", token_endpoint="https://id.example.com/token",
      secret=SECRET, scopes=["a/.default"]))
  block = ts.resource_body()["mcpToolset"]["apiAuthentication"]["oauthConfig"]
  assert block == {
      "oauthGrantType": "CLIENT_CREDENTIAL",
      "clientId": "svc",
      "clientSecretVersion": SECRET,
      "tokenEndpoint": "https://id.example.com/token",
      "scopes": ["a/.default"],
  }


def test_api_key_and_bearer_auth_blocks():
  key = _toolset("a", auth=flows.api_key_auth("Authorization", secret=SECRET))
  bearer = _toolset("b", auth=flows.bearer_auth(secret=SECRET))
  assert key.resource_body()["mcpToolset"]["apiAuthentication"] == {
      "apiKeyConfig": {"keyName": "Authorization", "apiKeySecretVersion": SECRET,
                       "requestLocation": "HEADER"}}
  # bearer_auth maps to apiKeyConfig(Authorization) — CES BearerTokenConfig has only a
  # `token` field that must be a $context.variables.* ref, not a secret version.
  assert bearer.resource_body()["mcpToolset"]["apiAuthentication"] == {
      "apiKeyConfig": {"keyName": "Authorization", "apiKeySecretVersion": SECRET,
                       "requestLocation": "HEADER"}}


def test_no_auth_emits_no_authentication_block():
  assert "apiAuthentication" not in _toolset().resource_body()["mcpToolset"]


def test_custom_headers_are_emitted():
  ts = _toolset(headers={"session_id": "$context.variables.session_id"})
  assert ts.resource_body()["mcpToolset"]["customHeaders"] == {
      "session_id": "$context.variables.session_id"}


def test_a_literal_header_value_is_refused():
  """CES resolves a custom header from a session variable and ONLY in that shape."""
  with pytest.raises(ValueError, match=r"\$context.variables"):
    _toolset(headers={"x-region": "us-east"})


def test_no_custom_headers_key_when_none_given():
  assert "customHeaders" not in _toolset().resource_body()["mcpToolset"]


def test_toolset_name_must_be_a_python_identifier():
  """It prefixes `tools.<name>_<tool>` — a dash is a runtime NameError."""
  with pytest.raises(ValueError, match="must be a python identifier"):
    _toolset("mcp-api")


def test_server_url_must_be_absolute():
  with pytest.raises(ValueError, match="must be an absolute URL"):
    _toolset(server_url="mcp.example.com")


def test_server_url_is_required():
  with pytest.raises(ValueError, match="server_url is required"):
    flows.mcp_toolset("mcp_api", server_url="")


def test_auth_must_be_built_with_a_helper():
  with pytest.raises(ValueError, match="must be built with flows.api_key_auth"):
    _toolset(auth={"oauthConfig": {}})


def test_a_raw_secret_is_still_refused_through_the_shared_builders():
  with pytest.raises(ValueError, match="Secret Manager version reference"):
    flows.oauth_auth(client_id="c", secret="shhh",
                     token_endpoint="https://t.example.com")


# --- environment.json -------------------------------------------------------


def test_no_environment_json_by_default():
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(_app(toolsets=[_toolset()]), out)
    assert not os.path.exists(os.path.join(out, "environment.json"))


def test_env_scoped_moves_url_and_secret_out_and_leaves_markers():
  ts = _toolset(env_scoped=True, auth=flows.oauth_auth(
      client_id="c", token_endpoint="https://id.example.com/token", secret=SECRET),
      headers={"session_id": "$context.variables.session_id"})
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(_app(toolsets=[ts]), out)
    env = json.load(open(os.path.join(out, "environment.json")))
    doc = json.load(open(os.path.join(out, "toolsets/mcp_api/mcp_api.json")))
  assert env["toolsets"]["mcp_api"]["mcpToolset"] == {
      "serverAddress": URL,
      "apiAuthentication": {"oauthConfig": {
          "clientSecretVersion": SECRET,
          "tokenEndpoint": "https://id.example.com/token"}},
  }
  block = doc["mcpToolset"]
  assert block["serverAddress"] == _mcp.ENV_VAR
  # Structural fields stay on the resource — the client id and the headers.
  assert block["apiAuthentication"]["oauthConfig"]["clientId"] == "c"
  assert block["customHeaders"] == {"session_id": "$context.variables.session_id"}


# --- mcp_tool: the wrapper --------------------------------------------------


def test_mcp_tool_registers_a_callable_tool_that_forwards_to_the_symbol():
  flows.mcp_tool("get_thing", _toolset(), "get_account_thing", params=["mdn"])
  assert ("getattr(tools, 'mcp_api_get_account_thing')(request)"
          in _source("get_thing"))


def test_tool_name_is_used_verbatim():
  """CES derives the symbol from the name the server reports."""
  flows.mcp_tool("t", _toolset(), "getAccountBalance", params=["mdn"])
  assert "mcp_api_getAccountBalance" in _source("t")


def test_a_dashed_tool_name_is_refused():
  """It becomes tools.<toolset>_<tool>, and a dash there is a NameError."""
  with pytest.raises(ValueError, match="must contain only letters, digits and '_'"):
    flows.mcp_tool("t", _toolset(), "get-balance", params=["mdn"])


def test_a_dashed_wrapper_name_is_refused():
  """The wrapper name is emitted verbatim as `def <name>` — a dash is a SyntaxError."""
  with pytest.raises(ValueError, match="must be a python identifier"):
    flows.mcp_tool("get-balance", _toolset(), "get_balance", params=["mdn"])


def test_overlapping_request_parameter_paths_are_refused():
  """`message` and `message.body` would corrupt the assembled request (setdefault on a
  string). MCP has no spec to validate params against, so this guard is where it lands."""
  with pytest.raises(ValueError, match="overlap"):
    flows.mcp_tool("t", _toolset(), "send_message",
                   params={"m": "message", "b": "message.body"})


def test_reserved_output_keys_are_refused():
  for key in ("success", "error", "response"):
    with pytest.raises(ValueError, match="is reserved"):
      flows.mcp_tool(f"t_{key}", _toolset(), "get_thing", outputs={key: "a.b"})


def test_toolset_must_be_a_mcp_toolset():
  with pytest.raises(ValueError, match="must be a flows.mcp_toolset"):
    flows.mcp_tool("t", "mcp_api", "get_thing")


def test_tool_name_is_required():
  with pytest.raises(ValueError, match="tool_name is required"):
    flows.mcp_tool("t", _toolset(), "")


def test_declared_output_keys_include_the_wrapper_contract():
  flows.mcp_tool("t", _toolset(), "get_thing", outputs={"balance": "data.balance"})
  assert _tools._REGISTRY["t"].output_keys == [
      "balance", "success", "error", "response"]


def test_params_accept_a_dict_rename():
  t = flows.mcp_tool("t", _toolset(), "get_thing", params={"mdn": "phoneNumber"})
  assert dict(t.params) == {"mdn": "phoneNumber"}


def test_a_param_less_tool_is_allowed():
  """An MCP tool that takes no arguments is common, and there is no spec to require."""
  t = flows.mcp_tool("t", _toolset(), "list_all")
  assert dict(t.params) == {}
  assert "def t() ->" in _source("t")


# --- The wrapper at runtime -------------------------------------------------


def test_wrapper_maps_a_nested_response_to_flat_keys():
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  fn = _run("t", live=lambda req: {"data": {"currentBalance": "42.00"}},
            symbol="mcp_api_get_thing")
  out = fn(mdn="5551234")
  assert out["balance"] == "42.00"
  assert out["success"] is True


def test_wrapper_sends_the_wire_names_not_the_argument_names():
  seen = {}
  flows.mcp_tool("t", _toolset(), "get_thing", params={"mdn": "phoneNumber"})
  fn = _run("t", live=lambda req: (seen.update(req), {"ok": 1})[1])
  fn(mdn="5551234")
  assert seen == {"phoneNumber": "5551234"}


def test_an_unset_optional_argument_is_omitted_from_the_request():
  seen = {}
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn", "account"])
  fn = _run("t", live=lambda req: (seen.update(req), {"ok": 1})[1])
  fn(mdn="5551234")
  assert seen == {"mdn": "5551234"}


def test_a_failed_call_reports_failure_rather_than_raising():
  def boom(_req):
    raise RuntimeError("mcp unreachable")

  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  out = _run("t", live=boom)(mdn="5551234")
  assert out["success"] is False
  assert "mcp unreachable" in out["error"]
  assert out["balance"] is None


def test_a_response_missing_the_mapped_field_is_a_miss_not_a_none_fill():
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  out = _run("t", live=lambda req: {"data": {}})(mdn="5551234")
  assert out["success"] is False
  assert "balance" in out["error"]


def test_dot_path_indexes_a_list():
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"first": "items.0.sku"})
  out = _run("t", live=lambda req: {"items": [{"sku": "ABC"}]})(mdn="5551234")
  assert out["first"] == "ABC"


def test_response_object_is_unwrapped_via_result():
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  boxed = types.SimpleNamespace(result={"data": {"currentBalance": "OK"}})
  assert _run("t", live=lambda req: boxed)(mdn="5551234")["balance"] == "OK"


# --- Mocking ----------------------------------------------------------------


def test_callable_mock_answers_when_the_flag_is_on_and_varies_by_input():
  def fake(mdn):
    return {"data": {"currentBalance": "10" if mdn == "1" else "20"}}

  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"}, mock=fake)
  fn = _run("t", variables={"mock_apis": True})  # live path raises if taken
  assert fn(mdn="1")["balance"] == "10"
  assert fn(mdn="2")["balance"] == "20"


def test_dict_mock_answers_when_the_flag_is_on():
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"},
                 mock={"data": {"currentBalance": "99"}})
  out = _run("t", variables={"mock_apis": True})(mdn="1")
  assert out["balance"] == "99"


def test_the_flag_off_goes_live_even_with_a_mock_declared():
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"},
                 mock={"data": {"currentBalance": "99"}})
  out = _run("t", variables={"mock_apis": False},
             live=lambda req: {"data": {"currentBalance": "LIVE"}})(mdn="1")
  assert out["balance"] == "LIVE"


def test_a_per_tool_pinned_payload_beats_the_flag():
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"},
                 mock={"data": {"currentBalance": "99"}})
  out = _run("t", variables={"mock_apis": True,
                             "mock_t": {"data": {"currentBalance": "PIN"}}})(mdn="1")
  assert out["balance"] == "PIN"


def test_a_tool_with_no_mock_is_still_pinnable_per_session():
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  out = _run("t", variables={"mock_t": {"data": {"currentBalance": "PIN"}}})(mdn="1")
  assert out["balance"] == "PIN"


def test_the_flag_is_read_from_context_state_as_well_as_variables():
  """The framework's own tools use `context.state`; CES exposes `context.variables`."""
  flows.mcp_tool("t", _toolset(), "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"},
                 mock={"data": {"currentBalance": "MOCK"}})
  context = types.SimpleNamespace(state={"mock_apis": True})
  toolsmod = types.SimpleNamespace(**{
      "mcp_api_get_thing": lambda r: pytest.fail("went live")})
  mns: dict = {"context": context, "tools": toolsmod}
  exec(compile(_source("t_mock"), "t_mock.py", "exec"), mns)  # noqa: S102
  toolsmod.t_mock = lambda request: {"result": mns["t_mock"](**request)}
  ns: dict = {"context": context, "tools": toolsmod}
  exec(compile(_source("t"), "t.py", "exec"), ns)  # noqa: S102
  assert ns["t"](mdn="1")["balance"] == "MOCK"


def test_toolset_level_mocks_serve_a_tool_that_declared_none():
  """A convenience: every mock can live on the toolset instead of on each mcp_tool."""
  ts = _toolset(mocks={"get_thing": {"data": {"currentBalance": "FROM_TOOLSET"}}})
  flows.mcp_tool("t", ts, "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  out = _run("t", variables={"mock_apis": True})(mdn="1")
  assert out["balance"] == "FROM_TOOLSET"


def test_an_explicit_mock_beats_the_toolset_level_one():
  ts = _toolset(mocks={"get_thing": {"data": {"currentBalance": "TOOLSET"}}})
  flows.mcp_tool("t", ts, "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"},
                 mock={"data": {"currentBalance": "EXPLICIT"}})
  out = _run("t", variables={"mock_apis": True})(mdn="1")
  assert out["balance"] == "EXPLICIT"


def test_the_baked_default_mocks_with_no_session_variable_at_all():
  ts = _toolset()
  flows.mcp_tool("get_thing", ts, "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"},
                 mock={"data": {"currentBalance": "MOCKED"}})
  app = _app(toolsets=[ts], mock_apis=True,
             slots=[flows.result_slot("balance", "lookup")],
             tasks=[("lookup", "get_thing", ["mdn"], "balance", {"out_key": "balance"})])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
  assert "_MOCK_DEFAULT = True" in _source("get_thing")
  out_ = _run("get_thing")(mdn="1")  # no variables, live path raises
  assert out_["balance"] == "MOCKED"


def test_a_mock_is_emitted_as_its_own_tool_and_scoped_onto_no_agent():
  ts = _toolset()
  flows.mcp_tool("get_thing", ts, "get_thing", params=["mdn"],
                 outputs={"balance": "data.currentBalance"},
                 mock={"data": {"currentBalance": "MOCKED"}})
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("balance", "lookup")],
             tasks=[("lookup", "get_thing", ["mdn"], "balance", {"out_key": "balance"})])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    assert os.path.isfile(
        os.path.join(out, "tools/get_thing_mock/python_function/python_code.py"))
    agent = json.load(open(os.path.join(out, "agents/Root Agent/Root Agent.json")))
  assert "get_thing_mock" not in agent["tools"]
  assert "get_thing" in agent["tools"]


# --- Deriving outputs from the tasks ----------------------------------------


def test_outputs_are_derived_from_the_task_as_literal_paths():
  """No response schema, so each key a task asks for is taken as a literal dot-path."""
  ts = _toolset()
  flows.mcp_tool("get_thing", ts, "get_thing", params=["mdn"])  # no outputs declared
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("balance", "lookup")],
             tasks=[("lookup", "get_thing", ["mdn"], "balance",
                     {"out_key": "data.currentBalance"})])
  assert flows.validate_app(app)[0] == []
  assert "out['data.currentBalance'] = _dig(data, 'data.currentBalance')" in _source(
      "get_thing")


# --- Wiring into a flow -----------------------------------------------------


def _wired_app(**appkw):
  ts = _toolset()
  flows.mcp_tool("get_balance", ts, "get_account_balance", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  return _app(
      toolsets=[ts],
      slots=[flows.result_slot("balance", "lookup"),
             flows.announce("done", ["It is {balance}."],
                            requires=["balance"], end=True)],
      tasks=[("lookup", "get_balance", ["mdn"], "balance", {"out_key": "balance"})],
      **appkw)


def test_a_wired_toolset_app_validates_clean():
  errors, _warnings = flows.validate_app(_wired_app())
  assert errors == []


def test_the_wrapper_is_scoped_onto_the_agent_but_the_toolset_is_not():
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(_wired_app(), out)
    agent = json.load(open(os.path.join(out, "agents/Root Agent/Root Agent.json")))
  assert "get_balance" in agent["tools"]
  assert "mcp_api" not in agent["tools"]
  assert not [t for t in agent["tools"] if t.startswith("mcp_api_")]


def test_firing_the_toolset_itself_is_an_error_that_names_the_fix():
  ts = _toolset()
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("b", "lookup")],
             tasks=[("lookup", "mcp_api", ["mdn"], "b")])
  errors, _warnings = flows.validate_app(app)
  assert any("is an MCP TOOLSET, not a tool" in e for e in errors)
  assert any("flows.mcp_tool(" in e for e in errors)


def test_firing_the_in_sandbox_symbol_is_an_error_that_names_the_fix():
  """`<toolset>_<tool>` looks callable precisely because a body calls it."""
  ts = _toolset()
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("b", "lookup")],
             tasks=[("lookup", "mcp_api_get_account_balance", ["mdn"], "b")])
  errors, _warnings = flows.validate_app(app)
  assert any("in-sandbox symbol" in e and "MCP" in e for e in errors)
  assert any("get_account_balance" in e for e in errors)


def test_toolsets_must_be_built_with_the_builder():
  with pytest.raises(ValueError, match="must be built with flows.openapi_toolset"):
    flows.validate_app(_app(toolsets=[{"name": "mcp_api"}]))


def test_the_same_toolset_declared_twice_emits_once():
  ts = _toolset()
  flows.mcp_tool("get_balance", ts, "get_account_balance", params=["mdn"])
  app = _app(toolsets=[ts, ts],
             slots=[flows.result_slot("b", "lookup")],
             tasks=[("lookup", "get_balance", ["mdn"], "b")])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    assert len(os.listdir(os.path.join(out, "toolsets"))) == 1


def test_two_different_toolsets_under_one_name_is_an_error():
  a = _toolset()
  b = _toolset(description="different")
  with pytest.raises(ValueError, match="two different toolsets"):
    flows.validate_app(_app(toolsets=[a, b]))


def test_an_app_without_toolsets_emits_no_toolsets_dir():
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(_app(), out)
    assert not os.path.exists(os.path.join(out, "toolsets"))


def test_mock_apis_warns_about_a_call_that_still_goes_live():
  ts = _toolset()
  flows.mcp_tool("get_balance", ts, "get_account_balance", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  app = _app(toolsets=[ts], mock_apis=True,
             slots=[flows.result_slot("balance", "lookup")],
             tasks=[("lookup", "get_balance", ["mdn"], "balance")])
  errors, warnings = flows.validate_app(app)
  assert errors == []
  assert any("declared no" in w and "get_balance" in w for w in warnings)


def test_the_mock_flag_variable_is_declared_whenever_the_app_has_a_toolset():
  ts = _toolset()
  flows.mcp_tool("get_balance", ts, "get_account_balance", params=["mdn"])
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("b", "lookup")],
             tasks=[("lookup", "get_balance", ["mdn"], "b")])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    decls = json.load(open(os.path.join(out, "app.json")))["variableDeclarations"]
  assert any(d["name"] == "mock_apis" for d in decls)


# --- Multi-agent ------------------------------------------------------------


def test_a_sub_agents_toolset_is_emitted_and_its_wrapper_scoped_to_it_alone():
  ts = _toolset()
  flows.mcp_tool("get_balance", ts, "get_account_balance", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  billing = flows.Flow("billing", root_agent="Billing")
  billing.add(
      flows.user_slot("mdn", ask="Mobile number?"),
      flows.result_slot("balance", "lookup"),
      flows.announce("done", ["It is {balance}."], requires=["balance"], end=True),
  )
  billing.task("lookup", "get_balance", ["mdn"], "balance", out_key="balance")
  hours = flows.Flow("hours", root_agent="Hours")
  hours.add(flows.announce("h", ["We are open."], end=True))
  billing_agent = flows.Agent(name="Billing", flow=billing, toolsets=[ts])
  hours_agent = flows.Agent(name="Hours", flow=hours)
  app = flows.App(
      app_display_name="Multi",
      host=flows.HostRouter(name="Router",
                            routes={"billing": billing_agent, "hours": hours_agent}),
      agents=[billing_agent, hours_agent],
  )
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    assert os.path.isfile(os.path.join(out, "toolsets/mcp_api/mcp_api.json"))
    billing_json = json.load(open(os.path.join(out, "agents/Billing/Billing.json")))
    hours_json = json.load(open(os.path.join(out, "agents/Hours/Hours.json")))
  assert "get_balance" in billing_json["tools"]
  assert "get_balance" not in hours_json["tools"]
  assert "mcp_api" not in billing_json["tools"]


# --- Mixed: OpenAPI + MCP in one app ----------------------------------------


def test_openapi_and_mcp_toolsets_coexist_in_one_app():
  """Both kinds emit under toolsets/; only the OpenAPI one carries a schema file."""
  rest = flows.openapi_toolset("rest_api", spec={
      "openapi": "3.0.1", "info": {"title": "X", "version": "1"},
      "paths": {"/x/{mdn}": {"get": {
          "operationId": "getX",
          "parameters": [{"name": "mdn", "in": "path", "required": True,
                          "schema": {"type": "string"}}],
          "responses": {"200": {"description": "ok"}}}}}})
  flows.api_tool("get_x", rest, "getX", params={"mdn": "mdn"})
  mcp_ts = _toolset()
  flows.mcp_tool("get_balance", mcp_ts, "get_account_balance", params=["mdn"],
                 outputs={"balance": "data.currentBalance"})
  app = _app(
      toolsets=[rest, mcp_ts],
      slots=[flows.result_slot("x", "look_x"),
             flows.result_slot("balance", "look_bal")],
      tasks=[("look_x", "get_x", ["mdn"], "x"),
             ("look_bal", "get_balance", ["mdn"], "balance", {"out_key": "balance"})])
  assert flows.validate_app(app)[0] == []
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    assert os.path.isfile(os.path.join(
        out, "toolsets/rest_api/open_api_toolset/open_api_schema.yaml"))
    assert not os.path.exists(os.path.join(out, "toolsets/mcp_api/open_api_toolset"))
    decls = json.load(open(os.path.join(out, "app.json")))["variableDeclarations"]
  # One shared mock flag, not one per kind.
  assert len([d for d in decls if d["name"] == "mock_apis"]) == 1
