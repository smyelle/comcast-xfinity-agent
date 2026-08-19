"""OpenAPI toolsets: resource emission, wrapper generation, mocking, and the guards
that stop a toolset being wired up as if it were a tool.

The on-disk shapes asserted here were read off a real pulled app — `[ygupta] THD Prod`
in ces-deployment-dev, which runs ten `openApiToolset`s in production — not from the
proto. Two facts from that app drive most of this module: the toolset lives under
`toolsets/` with its spec in a second file, and NO agent references a toolset or a
`<toolset>_<operationId>` member in its `tools[]`. Every API call there goes through a
`pythonFunction` wrapper, which is what `api_tool` generates.

The generated wrapper is EXECUTED here against faked `context`/`tools` globals rather
than only string-matched, because it is emitted source that nothing else compiles: a
syntax error or a bad symbol would otherwise surface on the first live call.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_openapi.py
"""

from __future__ import annotations

import json
import os
import tempfile
import types

import pytest
import yaml
from pydantic import BaseModel

import flows
from flows.authoring import openapi as _openapi
from flows.authoring import tools as _tools
from toolset_testkit import registry_snapshot, run_wrapper
from toolset_testkit import source as _source

SECRET = "projects/p/secrets/api-key/versions/3"


class _MockOrder(BaseModel):
  status: str


class _MockOrderResponse(BaseModel):
  """Module level on purpose: this file uses PEP 563, so `get_type_hints` cannot
  resolve a model defined inside a test function and no model would be inlined."""

  order: _MockOrder

SPEC = """
openapi: 3.0.1
info: {title: Order API, version: 1.0.0}
servers: [{url: https://published.example.com}]
paths:
  /api/orders/{orderId}:
    get:
      summary: Search order by ID
      operationId: searchOrdersByOrderId
      parameters:
        - {name: orderId, in: path, required: true, schema: {type: string}}
        - {name: sessionId, in: header, required: false, schema: {type: string}}
      responses: {'200': {description: ok}}
  /api/sms:
    post:
      summary: Send a tracking SMS
      operationId: sendTrackingSms
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [phone]
              properties:
                phone: {type: string}
                message:
                  type: object
                  properties:
                    body: {type: string}
      responses: {'200': {description: ok}}
"""


@pytest.fixture(autouse=True)
def _clean_registry():
  with registry_snapshot():
    yield


def _toolset(name="order_api", **kw):
  kw.setdefault("spec", SPEC)
  return flows.openapi_toolset(name, **kw)


def _run(name, *, variables=None, live=None, symbol="order_api_searchOrdersByOrderId"):
  return run_wrapper(name, symbol=symbol, variables=variables, live=live)


def _app(*, toolsets=(), tasks=(), slots=(), **kw):
  """A one-flow app. Each entry in `tasks` is the `flow.task(...)` argument tuple,
  optionally ending in a dict of keyword arguments."""
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("order_id", ask="Order number?"), *slots)
  for spec in tasks:
    args = list(spec)
    kwargs = args.pop() if args and isinstance(args[-1], dict) else {}
    flow.task(*args, **kwargs)
  return flows.App(
      root_flow=flow, app_display_name="T", toolsets=list(toolsets), **kw)


# --- Spec parsing -----------------------------------------------------------


def test_parses_operations_by_operation_id():
  ts = _toolset()
  assert sorted(ts.operations) == ["searchOrdersByOrderId", "sendTrackingSms"]
  op = ts.operations["searchOrdersByOrderId"]
  assert (op.method, op.path) == ("GET", "/api/orders/{orderId}")
  assert op.summary == "Search order by ID"
  assert op.params == ("orderId", "sessionId")
  assert op.required == ("orderId",)


def test_request_body_leaves_become_dot_paths():
  """A nested body field is addressed by dot-path; the wrapper reassembles it."""
  op = _toolset().operations["sendTrackingSms"]
  assert set(op.body_params) == {"phone", "message.body"}
  assert op.required_body == ("phone",)


def test_spec_accepts_a_path_a_string_or_a_dict():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "spec.yaml")
    with open(path, "w") as fh:
      fh.write(SPEC)
    from_path = _toolset("a", spec=path)
  from_text = _toolset("b", spec=SPEC)
  from_dict = _toolset("c", spec=yaml.safe_load(SPEC))
  assert sorted(from_path.operations) == sorted(from_text.operations)
  assert sorted(from_dict.operations) == sorted(from_text.operations)


def test_a_response_without_a_description_is_rejected():
  """CES drops the WHOLE toolset for this, and the push still reports success.

  Proven live: one spec pushed twice, differing only by this line. Without it the
  toolset never appears in the deployed app (`cxas tools list` shows no Toolset, a
  pull has no `toolsets/`) and the first sign is a live call failing with
  `Tool with name zip_api_lookupZip not found` — which points at the operation
  rather than at the spec. With it, the toolset imports and the call returns.
  """
  spec = """
openapi: 3.0.1
info: {title: X, version: 1.0.0}
paths:
  /x:
    get:
      operationId: getX
      responses:
        '200':
          content:
            application/json:
              schema: {type: object, properties: {a: {type: string}}}
"""
  with pytest.raises(ValueError) as exc:
    _toolset(spec=spec)
  assert "no `description`" in str(exc.value)
  assert "GET /x -> 200" in str(exc.value)


def test_spec_with_no_operation_ids_is_rejected_and_names_the_offenders():
  """CES derives one callable per operationId, so an operation without one is dead.

  Real CES specs do ship these — one in the production reference app has a single
  operation with no id — so the message has to say which path to fix. No name is
  invented: a guess that does not match what CES derives is a NameError on the first
  live call instead of a build error here.
  """
  spec = "openapi: 3.0.1\npaths:\n  /x:\n    get:\n      responses: {'200': {description: ok}}\n"
  with pytest.raises(ValueError) as exc:
    _toolset(spec=spec)
  assert "no operations with an `operationId`" in str(exc.value)
  assert "GET /x" in str(exc.value)


COMPOSED_SPEC = """
openapi: 3.0.1
info: {title: Composed, version: 1.0.0}
paths:
  /x:
    post:
      operationId: doX
      requestBody:
        content:
          application/json:
            schema:
              allOf:
                - type: object
                  required: [id]
                  properties: {id: {type: string}}
                - type: object
                  properties: {note: {type: string}}
      responses:
        '200':
          description: The composed result.
          content:
            application/json:
              schema:
                allOf:
                  - type: object
                    properties: {status: {type: string}}
                  - type: object
                    properties:
                      detail:
                        type: object
                        properties: {code: {type: string}}
"""


def test_all_of_schemas_are_flattened():
  """A node carrying only `allOf` has no `properties` of its own.

  Spec generators express inheritance this way constantly, so without flattening a
  valid body parameter is rejected as unknown and a real response path cannot resolve.
  """
  op = _toolset(spec=COMPOSED_SPEC).operations["doX"]
  assert set(op.body_params) == {"id", "note"}
  assert op.required_body == ("id",)
  assert set(op.response_paths) == {"status", "detail.code"}


def test_an_all_of_body_parameter_is_accepted():
  flows.api_tool("t", _toolset(spec=COMPOSED_SPEC), "doX", params={"id": "id"})


@pytest.mark.parametrize("keyword", ["anyOf", "oneOf"])
def test_any_of_and_one_of_expose_every_branch_but_require_nothing(keyword):
  """The caller may be satisfying a different branch, so nothing is required."""
  spec = {
      "openapi": "3.0.1",
      "paths": {"/x": {"post": {
          "operationId": "doX",
          "requestBody": {"content": {"application/json": {"schema": {
              keyword: [
                  {"type": "object", "required": ["card"],
                   "properties": {"card": {"type": "string"}}},
                  {"type": "object", "required": ["bank"],
                   "properties": {"bank": {"type": "string"}}},
              ]}}}},
      }}},
  }
  op = _toolset(spec=spec).operations["doX"]
  assert set(op.body_params) == {"card", "bank"}
  assert op.required_body == ()


def test_a_spec_path_that_does_not_exist_says_so():
  """It would otherwise parse AS the document and fail two steps later as 'got str'."""
  with pytest.raises(ValueError) as exc:
    _toolset(spec="specs/definitely_not_here.yaml")
  assert "spec file not found" in str(exc.value)
  assert "definitely_not_here.yaml" in str(exc.value)


def test_one_line_json_is_still_read_as_a_document():
  """Its own `paths` keys are full of slashes, so a bare slash test would misfire."""
  ts = _toolset(spec=json.dumps(yaml.safe_load(SPEC)))
  assert "searchOrdersByOrderId" in ts.operations


def test_base_url_trailing_slash_is_stripped():
  """It would meet the spec's leading-slash paths as `https://host//api/...`."""
  ts = _toolset(base_url="https://orders.example.com/v1/")
  assert ts.base_url == "https://orders.example.com/v1"
  assert yaml.safe_load(ts.spec_text)["servers"] == [
      {"url": "https://orders.example.com/v1"}]


def test_local_refs_are_followed():
  spec = {
      "openapi": "3.0.1",
      "paths": {"/x": {"post": {
          "operationId": "doX",
          "parameters": [{"$ref": "#/components/parameters/Trace"}],
          "requestBody": {"$ref": "#/components/requestBodies/Body"},
      }}},
      "components": {
          "parameters": {"Trace": {"name": "traceId", "in": "header", "required": True}},
          "requestBodies": {"Body": {"content": {"application/json": {"schema": {
              "type": "object", "required": ["amount"],
              "properties": {"amount": {"type": "number"}}}}}}},
      },
  }
  op = _toolset(spec=spec).operations["doX"]
  assert op.params == ("traceId",)
  assert op.body_params == ("amount",)


# --- The toolset resource ---------------------------------------------------


def test_toolset_emits_resource_and_spec_under_toolsets():
  """A toolset is its own resource kind — `toolsets/`, never `tools/`."""
  ts = _toolset(description="Orders.", base_url="https://orders.example.com")
  app = _app(toolsets=[ts])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    resource = os.path.join(out, "toolsets/order_api/order_api.json")
    spec = os.path.join(out, "toolsets/order_api/open_api_toolset/open_api_schema.yaml")
    assert os.path.isfile(resource) and os.path.isfile(spec)
    assert not os.path.exists(os.path.join(out, "tools/order_api"))
    doc = json.load(open(resource))
  assert doc["displayName"] == "order_api"
  assert doc["description"] == "Orders."
  assert doc["openApiToolset"]["openApiSchema"] == (
      "toolsets/order_api/open_api_toolset/open_api_schema.yaml")
  assert doc["name"] and doc["name"] != "order_api"  # a UUID, as every resource has


def test_base_url_overrides_the_specs_own_servers():
  """A published spec names ITS environment; the caller's base_url is the real one."""
  ts = _toolset(base_url="https://orders.internal.example.com")
  assert yaml.safe_load(ts.spec_text)["servers"] == [
      {"url": "https://orders.internal.example.com"}]


def test_spec_servers_are_left_alone_without_base_url():
  assert yaml.safe_load(_toolset().spec_text)["servers"] == [
      {"url": "https://published.example.com"}]


def test_api_key_auth_block():
  ts = _toolset(auth=flows.api_key_auth("staticAuthGuid", secret=SECRET))
  assert ts.resource_body()["openApiToolset"]["apiAuthentication"] == {
      "apiKeyConfig": {
          "keyName": "staticAuthGuid",
          "apiKeySecretVersion": SECRET,
          "requestLocation": "HEADER",
      }
  }


def test_oauth_auth_block():
  ts = _toolset(auth=flows.oauth_auth(
      client_id="spiffe://example/svc",
      token_endpoint="https://identity.example.com/oauth2/v1/token",
      secret=SECRET, scopes=["a/.default"]))
  assert ts.resource_body()["openApiToolset"]["apiAuthentication"]["oauthConfig"] == {
      "oauthGrantType": "CLIENT_CREDENTIAL",
      "clientId": "spiffe://example/svc",
      "clientSecretVersion": SECRET,
      "tokenEndpoint": "https://identity.example.com/oauth2/v1/token",
      "scopes": ["a/.default"],
  }


def test_bearer_and_service_agent_auth_blocks():
  bearer = _toolset("a", auth=flows.bearer_auth(secret=SECRET))
  agent = _toolset("b", auth=flows.service_agent_auth())
  # bearer_auth maps to apiKeyConfig(Authorization) — CES BearerTokenConfig has only a
  # `token` field that must be a $context.variables.* ref, not a secret version.
  assert bearer.resource_body()["openApiToolset"]["apiAuthentication"] == {
      "apiKeyConfig": {"keyName": "Authorization", "apiKeySecretVersion": SECRET,
                       "requestLocation": "HEADER"}}
  assert agent.resource_body()["openApiToolset"]["apiAuthentication"] == {
      "serviceAgentIdTokenAuthConfig": {}}


def test_no_auth_emits_no_authentication_block():
  assert "apiAuthentication" not in _toolset().resource_body()["openApiToolset"]


@pytest.mark.parametrize("builder", [
    lambda: flows.api_key_auth("k", secret="hunter2"),
    lambda: flows.bearer_auth(secret="raw-token-value"),
    lambda: flows.oauth_auth(client_id="c", secret="shhh",
                             token_endpoint="https://t.example.com"),
])
def test_a_raw_secret_is_refused(builder):
  """The one failure no later stage can undo: committing a live credential."""
  with pytest.raises(ValueError, match="Secret Manager version reference"):
    builder()


def test_api_key_location_is_validated():
  with pytest.raises(ValueError, match="location must be one of"):
    flows.api_key_auth("k", secret=SECRET, location="BODY")


def test_toolset_name_must_be_a_python_identifier():
  """It prefixes `tools.<name>_<operationId>` — a dash is a runtime NameError."""
  with pytest.raises(ValueError, match="must be a python identifier"):
    _toolset("order-api")


def test_auth_must_be_built_with_a_helper():
  with pytest.raises(ValueError, match="must be built with flows.api_key_auth"):
    _toolset(auth={"apiKeyConfig": {}})


# --- environment.json -------------------------------------------------------


def test_no_environment_json_by_default():
  """Inlining keeps the emitted dir self-contained and pushable as-is."""
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(_app(toolsets=[_toolset(base_url="https://x.example.com")]), out)
    assert not os.path.exists(os.path.join(out, "environment.json"))


def test_env_scoped_moves_url_and_secret_out_and_leaves_markers():
  ts = _toolset(base_url="https://orders.example.com", env_scoped=True,
                auth=flows.api_key_auth("staticAuthGuid", secret=SECRET))
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(_app(toolsets=[ts]), out)
    env = json.load(open(os.path.join(out, "environment.json")))
    doc = json.load(open(os.path.join(out, "toolsets/order_api/order_api.json")))
    spec = yaml.safe_load(
        open(os.path.join(out, "toolsets/order_api/open_api_toolset/open_api_schema.yaml")))
  assert env["toolsets"]["order_api"]["openApiToolset"] == {
      "url": "https://orders.example.com",
      "apiAuthentication": {"apiKeyConfig": {"apiKeySecretVersion": SECRET}},
  }
  # The structural half stays on the resource; only what varies per deploy moves.
  key = doc["openApiToolset"]["apiAuthentication"]["apiKeyConfig"]
  assert key["apiKeySecretVersion"] == _openapi.ENV_VAR
  assert key["keyName"] == "staticAuthGuid"
  assert key["requestLocation"] == "HEADER"
  assert spec["servers"] == [{"url": _openapi.ENV_VAR}]


def test_env_scoped_oauth_moves_token_endpoint_too():
  """The token endpoint differs per environment (dev vs prod identity service)."""
  ts = _toolset(env_scoped=True, auth=flows.oauth_auth(
      client_id="c", token_endpoint="https://identity.example.com/token", secret=SECRET))
  entry = ts.environment_entry()["openApiToolset"]["apiAuthentication"]["oauthConfig"]
  assert entry == {"clientSecretVersion": SECRET,
                   "tokenEndpoint": "https://identity.example.com/token"}
  assert ts.resource_body()["openApiToolset"]["apiAuthentication"]["oauthConfig"][
      "clientId"] == "c"


# --- api_tool: the wrapper --------------------------------------------------


def test_api_tool_registers_a_callable_tool_that_forwards_to_the_operation():
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId", params={"order_id": "orderId"})
  assert ("getattr(tools, 'order_api_searchOrdersByOrderId')(request)"
          in _source("search_order"))


def test_operation_id_is_used_verbatim_not_snake_cased():
  """CES derives the symbol from the spec; snake-casing invents a name."""
  ts = _toolset()
  flows.api_tool("t", ts, "searchOrdersByOrderId", params={"order_id": "orderId"})
  assert "order_api_searchOrdersByOrderId" in _source("t")
  assert "search_orders_by_order_id" not in _source("t")


def test_a_dashed_operation_id_is_called_via_getattr_not_attribute_access():
  """OpenAPI allows a dash in operationId; `tools.a_b-c(request)` would be subtraction,
  so the wrapper reaches the symbol with getattr and the generated body still runs."""
  spec = {
      "openapi": "3.0.1", "info": {"title": "X", "version": "1"},
      "paths": {"/u/{id}": {"get": {
          "operationId": "get-user",
          "parameters": [{"name": "id", "in": "path", "required": True,
                          "schema": {"type": "string"}}],
          "responses": {"200": {"description": "ok"}}}}}}
  ts = _toolset("dash_api", spec=spec)
  flows.api_tool("get_user", ts, "get-user", params={"id": "id"},
                 outputs={"name": "name"})
  assert "getattr(tools, 'dash_api_get-user')(request)" in _source("get_user")
  fn = _run("get_user", live=lambda req: {"name": "Ada"}, symbol="dash_api_get-user")
  assert fn(id="1")["name"] == "Ada"


def test_a_dashed_wrapper_name_is_refused():
  """The wrapper name is emitted verbatim as `def <name>` — a dash is a SyntaxError."""
  with pytest.raises(ValueError, match="must be a python identifier"):
    flows.api_tool("get-order", _toolset(), "searchOrdersByOrderId",
                   params={"order_id": "orderId"})


def test_unknown_operation_is_a_build_error_listing_what_exists():
  with pytest.raises(ValueError) as exc:
    flows.api_tool("t", _toolset(), "searchOrders", params=[])
  assert "not in the 'order_api' spec" in str(exc.value)
  assert "searchOrdersByOrderId" in str(exc.value)


def test_unknown_parameter_is_a_build_error():
  with pytest.raises(ValueError, match="not a parameter of 'searchOrdersByOrderId'"):
    flows.api_tool("t", _toolset(), "searchOrdersByOrderId", params={"x": "orderID"})


def test_missing_required_parameter_is_a_build_error():
  with pytest.raises(ValueError, match="requires 'orderId'"):
    flows.api_tool("t", _toolset(), "searchOrdersByOrderId", params={"s": "sessionId"})


def test_params_default_to_every_parameter_the_spec_declares():
  """The spec already says what they are; restating them was pure duplication.

  An optional parameter the flow never supplies is simply omitted from the request.
  """
  t = flows.api_tool("t", _toolset(), "searchOrdersByOrderId")
  assert dict(t.params) == {"orderId": "orderId", "sessionId": "sessionId"}


def test_params_accept_a_list_of_wire_names():
  t = flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                     params=["orderId", "sessionId"])
  assert dict(t.params) == {"orderId": "orderId", "sessionId": "sessionId"}


def test_reserved_output_keys_are_refused():
  for key in ("success", "error", "response"):
    with pytest.raises(ValueError, match="is reserved"):
      flows.api_tool(f"t_{key}", _toolset(), "searchOrdersByOrderId",
                     outputs={key: "a.b"})


def test_description_defaults_to_the_specs_summary():
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId")
  assert "Search order by ID" in _source("t")


def test_declared_output_keys_include_the_wrapper_contract():
  """`_check_task_success_keys` reads these to prove the task can detect success."""
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 outputs={"order_status": "order.status"})
  assert _tools._REGISTRY["t"].output_keys == [
      "order_status", "success", "error", "response"]


def test_toolset_must_be_a_toolset():
  with pytest.raises(ValueError, match="must be a flows.openapi_toolset"):
    flows.api_tool("t", "order_api", "searchOrdersByOrderId")


# --- The wrapper at runtime -------------------------------------------------


def test_wrapper_maps_a_nested_response_to_flat_keys():
  """Intake maps `outputs` by FLAT top-level key, so the wrapper does the digging."""
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"})
  fn = _run("t", live=lambda req: {"order": {"status": "SHIPPED"}})
  out = fn(order_id="W1")
  assert out["order_status"] == "SHIPPED"
  assert out["success"] is True


def test_wrapper_sends_the_wire_names_not_the_argument_names():
  seen = {}
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId", "session": "sessionId"})
  fn = _run("t", live=lambda req: (seen.update(req), {"ok": 1})[1])
  fn(order_id="W1", session="S9")
  assert seen == {"orderId": "W1", "sessionId": "S9"}


def test_wrapper_nests_body_fields_by_dot_path():
  seen = {}
  ts = _toolset()
  flows.api_tool("sms", ts, "sendTrackingSms",
                 params={"phone": "phone", "body": "message.body"})
  fn = _run("sms", live=lambda req: (seen.update(req), {"ok": 1})[1],
            symbol="order_api_sendTrackingSms")
  fn(phone="555", body="on its way")
  assert seen == {"phone": "555", "message": {"body": "on its way"}}


def test_an_unset_optional_argument_is_omitted_from_the_request():
  """A blank query parameter is a filter the caller never asked for."""
  seen = {}
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId", "session": "sessionId"})
  fn = _run("t", live=lambda req: (seen.update(req), {"ok": 1})[1])
  fn(order_id="W1")
  assert seen == {"orderId": "W1"}


def test_a_failed_call_reports_failure_rather_than_raising():
  """The task's `on_failure` ladder can only run if the tool returns."""
  def boom(_req):
    raise RuntimeError("403 forbidden")

  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"})
  out = _run("t", live=boom)(order_id="W1")
  assert out["success"] is False
  assert "403 forbidden" in out["error"]
  assert out["order_status"] is None


def test_a_200_missing_the_mapped_field_is_a_miss_not_a_none_fill():
  """Intake would otherwise write None into the slot and carry on."""
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"})
  out = _run("t", live=lambda req: {"order": {}})(order_id="W1")
  assert out["success"] is False
  assert "order_status" in out["error"]


def test_wrapper_succeeds_with_no_declared_outputs():
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId", params={"order_id": "orderId"})
  out = _run("t", live=lambda req: {"anything": 1})(order_id="W1")
  assert out["success"] is True
  assert out["response"] == {"anything": 1}


def test_dot_path_indexes_a_list():
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"first_item": "items.0.sku"})
  out = _run("t", live=lambda req: {"items": [{"sku": "ABC"}]})(order_id="W1")
  assert out["first_item"] == "ABC"


def test_response_object_is_unwrapped_via_result_or_json():
  """A toolset call may answer an object rather than a plain dict."""
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"})
  boxed = types.SimpleNamespace(result={"order": {"status": "OK"}})
  assert _run("t", live=lambda req: boxed)(order_id="W1")["order_status"] == "OK"


# --- Mocking ----------------------------------------------------------------


def test_callable_mock_answers_when_the_flag_is_on_and_varies_by_input():
  def fake(request):
    return {"order": {"status": "SHIPPED" if request["orderId"] == "W1" else "LOST"}}

  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"}, mock=fake)
  fn = _run("t", variables={"mock_apis": True})  # live path raises if taken
  assert fn(order_id="W1")["order_status"] == "SHIPPED"
  assert fn(order_id="W2")["order_status"] == "LOST"


def test_dict_mock_answers_when_the_flag_is_on():
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"},
                 mock={"order": {"status": "DELIVERED"}})
  out = _run("t", variables={"mock_apis": True})(order_id="W1")
  assert out["order_status"] == "DELIVERED"


def test_the_flag_off_goes_live_even_with_a_mock_declared():
  """The mock is emitted ALONGSIDE the live call and chosen at runtime."""
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"},
                 mock={"order": {"status": "DELIVERED"}})
  out = _run("t", variables={"mock_apis": False},
             live=lambda req: {"order": {"status": "LIVE"}})(order_id="W1")
  assert out["order_status"] == "LIVE"


def test_a_mock_runs_through_the_same_extraction_as_a_live_call():
  """So a passing mocked run has actually proved the dot-path mapping."""
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"order_status": "order.WRONG"},
                 mock={"order": {"status": "DELIVERED"}})
  out = _run("t", variables={"mock_apis": True})(order_id="W1")
  assert out["success"] is False  # the bad path fails under the mock too


def test_a_per_tool_pinned_payload_beats_the_flag():
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"},
                 mock={"order": {"status": "DELIVERED"}})
  out = _run("t", variables={"mock_apis": True,
                             "mock_t": {"order": {"status": "PINNED"}}})(order_id="W1")
  assert out["order_status"] == "PINNED"


def test_a_tool_with_no_mock_is_still_pinnable_per_session():
  """The reference app's hand-rolled convention, kept because evals want it."""
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId", params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"})
  out = _run("t", variables={"mock_t": {"order": {"status": "PINNED"}}})(order_id="W1")
  assert out["order_status"] == "PINNED"


def test_a_tool_with_no_mock_ignores_the_flag_and_goes_live():
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId", params={"order_id": "orderId"},
                 outputs={"order_status": "order.status"})
  out = _run("t", variables={"mock_apis": True},
             live=lambda req: {"order": {"status": "LIVE"}})(order_id="W1")
  assert out["order_status"] == "LIVE"


def test_the_baked_default_mocks_with_no_session_variable_at_all():
  """The App's choice is COMPILED IN.

  A `variableDeclarations` default does not reach a tool body — verified live: an app
  emitted with `mock_apis=True` still called the real API until the flag became a
  constant in the generated source.
  """
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId",
                 params={"order_id": "orderId"}, outputs={"st": "order.status"},
                 mock={"order": {"status": "MOCKED"}})
  app = _app(toolsets=[ts], mock_apis=True,
             slots=[flows.result_slot("st", "lookup")],
             tasks=[("lookup", "search_order", ["order_id"], "st", {"out_key": "st"})])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
  assert "_MOCK_DEFAULT = True" in _source("search_order")
  out_ = _run("search_order")(order_id="W1")  # no variables, live path raises
  assert out_["st"] == "MOCKED"


def test_the_baked_default_is_false_without_app_mock_apis():
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId",
                 params={"order_id": "orderId"}, outputs={"st": "order.status"},
                 mock={"order": {"status": "MOCKED"}})
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("st", "lookup")],
             tasks=[("lookup", "search_order", ["order_id"], "st", {"out_key": "st"})])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
  assert "_MOCK_DEFAULT = False" in _source("search_order")
  live = _run("search_order", live=lambda req: {"order": {"status": "LIVE"}})
  assert live(order_id="W1")["st"] == "LIVE"


def test_a_session_variable_overrides_the_baked_default_both_ways():
  """A live-emitted app can be mocked, and a mocked one sent to the real API."""
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId",
                 params={"order_id": "orderId"}, outputs={"st": "order.status"},
                 mock={"order": {"status": "MOCKED"}})
  app = _app(toolsets=[ts], mock_apis=True,
             slots=[flows.result_slot("st", "lookup")],
             tasks=[("lookup", "search_order", ["order_id"], "st", {"out_key": "st"})])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
  # baked True, session says False -> live
  fn = _run("search_order", variables={"mock_apis": False},
            live=lambda req: {"order": {"status": "LIVE"}})
  assert fn(order_id="W1")["st"] == "LIVE"


def test_the_flag_is_read_from_context_state_as_well_as_variables():
  """The framework's own tools use `context.state`; CES exposes `context.variables`.

  A flag that silently read empty would send a "mocked" run to the real API.
  """
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"order_id": "orderId"}, outputs={"st": "order.status"},
                 mock={"order": {"status": "MOCKED"}})
  context = types.SimpleNamespace(state={"mock_apis": True})
  toolsmod = types.SimpleNamespace(**{
      "order_api_searchOrdersByOrderId": lambda r: pytest.fail("went live")})
  mns: dict = {"context": context, "tools": toolsmod}
  exec(compile(_source("t_mock"), "t_mock.py", "exec"), mns)  # noqa: S102
  toolsmod.t_mock = lambda request: {"result": mns["t_mock"](**request)}
  ns: dict = {"context": context, "tools": toolsmod}
  exec(compile(_source("t"), "t.py", "exec"), ns)  # noqa: S102
  assert ns["t"](order_id="W1")["st"] == "MOCKED"


def test_a_mock_may_take_the_operations_own_parameters():
  """The natural way to write one, and it reads far better than digging in a dict."""
  def fake(orderId: str = "") -> dict:  # noqa: N803 — the operation's own name
    return {"order": {"status": f"status of {orderId}"}}

  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"orderId": "orderId"},
                 outputs={"st": "order.status"}, mock=fake)
  out = _run("t", variables={"mock_apis": True})(orderId="W1")
  assert out["st"] == "status of W1"


def test_a_mock_may_take_only_some_of_the_operations_parameters():
  """Caring about one of several is reasonable; passing the rest would be a TypeError."""
  def fake(orderId: str = ""):  # noqa: N803 — the operation also has sessionId
    return {"order": {"status": f"only {orderId}"}}

  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 outputs={"st": "order.status"}, mock=fake)
  # Built as kwargs, not passed unconditionally: `orderId` has a DEFAULT, and an
  # argument the caller never supplied has to leave that default standing rather than
  # overwrite it with the generated tool's own `''`.
  src = _source("t_mock")
  assert "if orderId not in (None, ''):" in src
  assert "return fake(**_args)" in src
  out = _run("t", variables={"mock_apis": True})(orderId="W1", sessionId="S")
  assert out["st"] == "only W1"


def test_a_mock_may_take_no_parameters_at_all():
  """A constant mock. Passing the operation's parameters would be a TypeError."""
  def fake():
    return {"order": {"status": "CONSTANT"}}

  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 outputs={"st": "order.status"}, mock=fake)
  assert "return fake()" in _source("t_mock")
  assert _run("t", variables={"mock_apis": True})(orderId="W1")["st"] == "CONSTANT"


def test_a_mock_taking_kwargs_gets_every_parameter():
  def fake(**kwargs):
    return {"order": {"status": ",".join(sorted(kwargs))}}

  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 outputs={"st": "order.status"}, mock=fake)
  assert _run("t", variables={"mock_apis": True})(
      orderId="W1", sessionId="S")["st"] == "orderId,sessionId"


def test_a_mock_taking_parameters_the_operation_does_not_have_is_refused():
  """Nothing could supply them — and it would fail only in the sandbox."""
  def fake(order_id, session_id):
    return {}

  with pytest.raises(ValueError, match="not parameters of the operation"):
    flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                   params={"orderId": "orderId"}, mock=fake)


def test_a_mock_is_emitted_as_its_own_tool_and_scoped_onto_no_agent():
  """So it is readable and editable in the CES console, but the model cannot call it.

  Mirrors how the reference app's `escalate_transfer` calls `get_department_ext`: a
  pythonFunction reached from another tool's body, in no agent's `tools[]`.
  """
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId",
                 params={"orderId": "orderId"}, outputs={"st": "order.status"},
                 mock={"order": {"status": "MOCKED"}})
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("st", "lookup")],
             tasks=[("lookup", "search_order", ["order_id"], "st", {"out_key": "st"})])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    assert os.path.isfile(
        os.path.join(out, "tools/search_order_mock/python_function/python_code.py"))
    doc = json.load(open(
        os.path.join(out, "tools/search_order_mock/search_order_mock.json")))
    agent = json.load(open(os.path.join(out, "agents/Root Agent/Root Agent.json")))
  assert doc["pythonFunction"]["name"] == "search_order_mock"
  assert "search_order_mock" not in agent["tools"]
  assert "search_order" in agent["tools"]


def test_the_wrapper_peels_the_result_envelope_off_the_mock_tool():
  """CES wraps a pythonFunction's return, so a dot-path would dig one level shallow."""
  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"orderId": "orderId"}, outputs={"st": "order.status"},
                 mock={"order": {"status": "MOCKED"}})
  assert "_tool_result(tools.t_mock({'orderId': orderId}))" in _source("t")
  assert _run("t", variables={"mock_apis": True})(orderId="W1")["st"] == "MOCKED"


def test_a_pydantic_returning_mock_inlines_its_models():
  """`render_callable` carries the models in, so the mock tool is self-contained."""
  def fake(orderId: str = "") -> _MockOrderResponse:  # noqa: N803
    return _MockOrderResponse(order=_MockOrder(status="TYPED"))

  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"orderId": "orderId"}, mock=fake)
  src = _source("t_mock")
  assert "class _MockOrderResponse(BaseModel):" in src
  assert "class _MockOrder(BaseModel):" in src


def test_a_request_taking_mock_is_accepted():
  def fake(request):
    return {"order": {"status": request["orderId"]}}

  flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                 params={"orderId": "orderId"},
                 outputs={"st": "order.status"}, mock=fake)
  out = _run("t", variables={"mock_apis": True})(orderId="W1")
  assert out["st"] == "W1"


@pytest.mark.parametrize("responses", [["200"], "200", 200])
def test_a_malformed_responses_block_does_not_crash_the_parser(responses):
  """These guards exist to explain a bad spec, so crashing on one defeats them."""
  spec = {"openapi": "3.0.1", "info": {"title": "X", "version": "1"},
          "paths": {"/x": {"get": {"operationId": "getX", "responses": responses}}}}
  ts = _toolset(spec=spec)
  assert ts.operations["getX"].response_paths == ()


def test_a_mock_with_no_readable_source_is_refused():
  """A mock is INLINED into the emitted tool, so its source has to be readable.

  A `def` that came from `exec` has none, and `inspect.getsource` raises something
  opaque about it. (A builtin and a lambda are refused a step earlier, by shape — see
  below — because both produce an emitted tool that fails in a way nothing traces back
  to the `mock=`.)
  """
  ns = {}
  exec("def from_exec(q=None):\n  return {}", ns)  # noqa: S102
  with pytest.raises(ValueError, match="no source that can be read"):
    flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                   params={"orderId": "orderId"}, mock=ns["from_exec"])


@pytest.mark.parametrize("mock,why", [
    (str, "CLASS"),                              # a builtin type: constructed, not called
    (eval("lambda q=None: {}"), "lambda"),       # noqa: S307 — `<lambda>` is no identifier
    (len, "builtin_function_or_method"),         # a C callable has no source at all
])
def test_a_mock_whose_SHAPE_cannot_be_emitted_is_refused_by_name(mock, why):
  """Each of these was emitted happily and failed after a deploy: a lambda produced a
  tool that would not parse, a class produced one that constructed instead of calling,
  and a builtin produced one with nothing to inline. Named at the `mock=` instead."""
  with pytest.raises(ValueError, match=why):
    flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                   params={"orderId": "orderId"}, mock=mock)


def test_an_unserializable_mock_is_refused():
  with pytest.raises(ValueError, match="JSON-serializable"):
    flows.api_tool("t", _toolset(), "searchOrdersByOrderId",
                   params={"order_id": "orderId"}, mock={"when": object()})


def test_mock_flag_variable_is_declared_whenever_the_app_has_a_toolset():
  """Declared even when emitting live, so a deployed app can be flipped ON."""
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId",
                 params={"order_id": "orderId"}, outputs={"st": "order.status"})
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("st", "lookup")],
             tasks=[("lookup", "search_order", ["order_id"], "st")])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    decls = json.load(open(os.path.join(out, "app.json")))["variableDeclarations"]
  flag = next(d for d in decls if d["name"] == "mock_apis")
  assert flag["schema"] == {"type": "BOOLEAN", "default": False}


def test_app_mock_apis_sets_the_variable_default():
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId",
                 params={"order_id": "orderId"}, outputs={"st": "order.status"},
                 mock={"order": {"status": "X"}})
  app = _app(toolsets=[ts], mock_apis=True,
             slots=[flows.result_slot("st", "lookup")],
             tasks=[("lookup", "search_order", ["order_id"], "st")])
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    decls = json.load(open(os.path.join(out, "app.json")))["variableDeclarations"]
  flag = next(d for d in decls if d["name"] == "mock_apis")
  assert flag["schema"]["default"] is True


def test_no_mock_flag_variable_without_a_toolset():
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(_app(), out)
    decls = json.load(open(os.path.join(out, "app.json")))["variableDeclarations"]
  assert not [d for d in decls if d["name"] == "mock_apis"]


def test_mock_apis_warns_about_a_call_that_still_goes_live():
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId",
                 params={"order_id": "orderId"}, outputs={"st": "order.status"})
  app = _app(toolsets=[ts], mock_apis=True,
             slots=[flows.result_slot("st", "lookup")],
             tasks=[("lookup", "search_order", ["order_id"], "st")])
  errors, warnings = flows.validate_app(app)
  assert errors == []
  assert any("declared no mock" in w and "search_order" in w for w in warnings)


# --- Wiring into a flow -----------------------------------------------------


def _wired_app(**appkw):
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId",
                 params={"order_id": "orderId"}, outputs={"order_status": "order.status"})
  return _app(
      toolsets=[ts],
      slots=[flows.result_slot("order_status", "lookup"),
             flows.announce("done", ["It is {order_status}."],
                            requires=["order_status"], end=True)],
      tasks=[("lookup", "search_order", ["order_id"], "order_status",
              {"out_key": "order_status"})],
      **appkw)


def test_a_wired_toolset_app_validates_clean():
  errors, _warnings = flows.validate_app(_wired_app())
  assert errors == []


def test_the_wrapper_is_scoped_onto_the_agent_but_the_toolset_is_not():
  """An agent's `tools[]` can hold the wrapper and must never hold the toolset."""
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(_wired_app(), out)
    agent = json.load(open(os.path.join(out, "agents/Root Agent/Root Agent.json")))
  assert "search_order" in agent["tools"]
  assert "order_api" not in agent["tools"]
  assert not [t for t in agent["tools"] if t.startswith("order_api_")]


def test_firing_the_toolset_itself_is_an_error_that_names_the_fix():
  ts = _toolset()
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("st", "lookup")],
             tasks=[("lookup", "order_api", ["order_id"], "st")])
  errors, _warnings = flows.validate_app(app)
  assert any("is an OpenAPI TOOLSET, not a tool" in e for e in errors)
  assert any("flows.api_tool(" in e for e in errors)


def test_firing_the_in_sandbox_symbol_is_an_error_that_names_the_fix():
  """`<toolset>_<operationId>` looks callable precisely because a body calls it."""
  ts = _toolset()
  app = _app(toolsets=[ts],
             slots=[flows.result_slot("st", "lookup")],
             tasks=[("lookup", "order_api_searchOrdersByOrderId", ["order_id"], "st")])
  errors, _warnings = flows.validate_app(app)
  assert any("in-sandbox symbol" in e for e in errors)
  assert any("searchOrdersByOrderId" in e for e in errors)


def test_toolsets_must_be_built_with_the_builder():
  with pytest.raises(ValueError, match="must be built with flows.openapi_toolset"):
    flows.validate_app(_app(toolsets=[{"name": "order_api"}]))


def test_the_same_toolset_declared_twice_emits_once():
  ts = _toolset()
  app = _app(toolsets=[ts, ts])
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


# --- Deriving everything from the spec --------------------------------------
#
# The spec declares the parameters and the response shape, and the task already says
# which value it wants. Nothing in between should have to be restated.

SCHEMA_SPEC = """
openapi: 3.0.1
info: {title: Zip, version: 1.0.0}
paths:
  /us/{zipcode}:
    get:
      summary: Look up a US zip code.
      operationId: lookupZip
      parameters:
        - {name: zipcode, in: path, required: true, schema: {type: string}}
      responses:
        '200':
          description: The place for that zip code.
          content:
            application/json:
              schema:
                type: object
                properties:
                  post code: {type: string}
                  country: {type: string}
                  places:
                    type: array
                    items:
                      type: object
                      properties:
                        place name: {type: string}
                        state: {type: string}
                        latitude: {type: string}
"""


def _zip_toolset(**kw):
  return flows.openapi_toolset("zip_api", spec=SCHEMA_SPEC, **kw)


def _zip_app(out_key="places.0.state", extra=None, **kw):
  ts = _zip_toolset(**kw)
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("zipcode", ask="Zip?"),
           flows.result_slot("state", "lookup"))
  flow.task("lookup", "lookupZip", ["zipcode"], "state",
            out_key=out_key, extra_outputs=extra)
  return ts, flows.App(root_flow=flow, app_display_name="T", toolsets=[ts])


def test_response_paths_come_off_the_schema_with_list_steps_as_index_zero():
  op = _zip_toolset().operations["lookupZip"]
  assert set(op.response_paths) == {
      "post code", "country",
      "places.0.place name", "places.0.state", "places.0.latitude",
  }


def test_a_task_naming_an_operation_id_generates_its_wrapper():
  """No api_tool call: drop the spec in and fire the operation."""
  _ts, app = _zip_app()
  errors, _warnings = flows.validate_app(app)
  assert errors == []
  assert "getattr(tools, 'zip_api_lookupZip')(request)" in _source("lookupZip")


def test_the_generated_wrapper_lifts_exactly_the_keys_the_task_asked_for():
  """Literal assignments, and only those — `getStore` has 37 leaves."""
  _ts, app = _zip_app(out_key="places.0.state")
  flows.validate_app(app)
  src = _source("lookupZip")
  assert "out['places.0.state'] = _dig(data, 'places.0.state')" in src
  assert "latitude" not in src


def test_an_output_key_may_be_an_unambiguous_leaf_name():
  _ts, app = _zip_app(out_key="state")
  assert flows.validate_app(app)[0] == []
  assert "out['state'] = _dig(data, 'places.0.state')" in _source("lookupZip")


def test_a_later_list_index_resolves_against_the_schemas_index_zero():
  _ts, app = _zip_app(out_key="places.2.state")
  assert flows.validate_app(app)[0] == []
  assert "_dig(data, 'places.2.state')" in _source("lookupZip")


def test_an_output_key_the_response_cannot_supply_is_a_build_error():
  """Otherwise the slot silently never fills."""
  _ts, app = _zip_app(out_key="postcode")
  with pytest.raises(ValueError, match="is not in the 'lookupZip' response"):
    flows.validate_app(app)


def test_the_generated_wrapper_exposes_every_spec_parameter():
  _ts, app = _zip_app()
  flows.validate_app(app)
  assert "def lookupZip(zipcode: str = '')" in _source("lookupZip")


def test_the_generated_wrapper_runs_and_maps_the_real_shape():
  _ts, app = _zip_app(out_key="places.0.state", extra={"places.0.place name": "place"})
  flows.validate_app(app)
  fn = _run("lookupZip", live=lambda req: {
      "post code": req["zipcode"],
      "places": [{"place name": "Beverly Hills", "state": "California"}],
  }, symbol="zip_api_lookupZip")
  out = fn(zipcode="90210")
  assert out["places.0.state"] == "California"
  assert out["places.0.place name"] == "Beverly Hills"
  assert out["success"] is True


def test_a_toolset_mock_serves_a_generated_wrapper():
  """There is no api_tool call to hang a mock on, so the toolset carries it."""
  _ts, app = _zip_app(
      mocks={"lookupZip": {"places": [{"state": "Mockachusetts"}]}})
  app.mock_apis = True
  flows.validate_app(app)
  out = _run("lookupZip", symbol="zip_api_lookupZip")(zipcode="90210")
  assert out["places.0.state"] == "Mockachusetts"


def test_a_mock_for_an_operation_the_spec_lacks_is_refused():
  with pytest.raises(ValueError, match="mocks name operation"):
    _zip_toolset(mocks={"lookupZipCode": {}})


def test_only_the_operations_a_flow_fires_are_generated():
  """A large spec must not become a hundred tool resources."""
  _ts, app = _zip_app()
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    emitted = set(os.listdir(os.path.join(out, "tools")))
  assert "lookupZip" in emitted


def test_api_tool_still_overrides_when_you_want_an_alias():
  """The escape hatch: a friendlier tool name and output key than the spec's."""
  ts = _zip_toolset()
  flows.api_tool("look_up_zip", ts, "lookupZip",
                 outputs={"state": "places.0.state"})
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("zipcode", ask="Zip?"),
           flows.result_slot("state", "lookup"))
  flow.task("lookup", "look_up_zip", ["zipcode"], "state", out_key="state")
  app = flows.App(root_flow=flow, app_display_name="T", toolsets=[ts])
  assert flows.validate_app(app)[0] == []
  assert "out['state'] = _dig(data, 'places.0.state')" in _source("look_up_zip")


def test_a_spec_with_no_response_schema_accepts_any_output_key():
  """Plenty of specs declare none; there is nothing to check against."""
  ts = _toolset()  # its 200s carry only a description
  flow = flows.Flow("t", root_agent="Root Agent")
  flow.add(flows.user_slot("order_id", ask="Order?"),
           flows.result_slot("st", "lookup"))
  flow.task("lookup", "searchOrdersByOrderId", {"order_id": "orderId"}, "st",
            out_key="anything.at.all")
  app = flows.App(root_flow=flow, app_display_name="T", toolsets=[ts])
  assert flows.validate_app(app)[0] == []


# --- Multi-agent ------------------------------------------------------------


def test_a_sub_agents_toolset_is_emitted_and_its_wrapper_scoped_to_it_alone():
  ts = _toolset()
  flows.api_tool("search_order", ts, "searchOrdersByOrderId",
                 params={"order_id": "orderId"}, outputs={"order_status": "order.status"})
  orders = flows.Flow("orders", root_agent="Orders")
  orders.add(
      flows.user_slot("order_id", ask="Order number?"),
      flows.result_slot("order_status", "lookup"),
      flows.announce("done", ["It is {order_status}."],
                     requires=["order_status"], end=True),
  )
  orders.task("lookup", "search_order", ["order_id"], "order_status",
              out_key="order_status")
  hours = flows.Flow("hours", root_agent="Hours")
  hours.add(flows.announce("h", ["We are open."], end=True))
  order_agent = flows.Agent(name="Orders", flow=orders, toolsets=[ts])
  hours_agent = flows.Agent(name="Hours", flow=hours)
  app = flows.App(
      app_display_name="Multi",
      host=flows.HostRouter(name="Router",
                            routes={"orders": order_agent, "hours": hours_agent}),
      agents=[order_agent, hours_agent],
  )
  with tempfile.TemporaryDirectory() as out:
    flows.build_app(app, out)
    assert os.path.isfile(os.path.join(out, "toolsets/order_api/order_api.json"))
    orders_json = json.load(open(os.path.join(out, "agents/Orders/Orders.json")))
    hours_json = json.load(open(os.path.join(out, "agents/Hours/Hours.json")))
  assert "search_order" in orders_json["tools"]
  assert "search_order" not in hours_json["tools"]
  assert "order_api" not in orders_json["tools"]
