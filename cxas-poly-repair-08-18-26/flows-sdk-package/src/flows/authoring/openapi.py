"""OpenAPI toolsets — call a REST API from a flow.

CES models an OpenAPI dependency as a **toolset**, not a tool. It is its own API
resource (`create_toolset`, a sibling of `create_tool`) and it lands on disk as two
files rather than one:

    toolsets/<name>/<name>.json                         # the openApiToolset resource
    toolsets/<name>/open_api_toolset/open_api_schema.yaml   # the spec itself

That difference matters more than it looks, because **an agent cannot call a toolset**.
Verified against the reference app (`[ygupta] THD Prod`, ces-deployment-dev): it
declares ten `openApiToolset`s, and not one agent's `tools[]` names a toolset or a
`<toolset>_<operationId>` member. Every one of its API calls goes through an ordinary
`pythonFunction` tool whose body calls the operation:

    def search_order_by_id(order_id: str) -> dict:
      return tools.order_search_v1_searchOrdersByOrderId({"orderId": order_id})

So a toolset is a *capability the sandbox gains*, and the callable thing is a wrapper.
`api_tool()` generates that wrapper, which is the whole reason this module is more than
a file emitter — and it is the exact inverse of A2A, where the tool is body-less
because the platform makes the call (see `flows.authoring.a2a`).

    orders = flows.openapi_toolset(
        "order_search_v1",
        spec="specs/orders.yaml",
        description="Order search API.",
        base_url="https://orders.example.com",
        auth=flows.api_key_auth("staticAuthGuid", secret=SECRET_VERSION),
    )

    flows.api_tool(
        "search_order_by_id", orders, "searchOrdersByOrderId",
        params={"order_id": "orderId"},
        outputs={"order_status": "order.status"},
    )

    app = flows.App(root_flow=f, toolsets=[orders], ...)
    f.task("lookup", "search_order_by_id", ["order_id"], "status_slot",
           out_key="order_status")

Because the wrapper's body is ours, the response can be shaped for intake rather than
worked around after the fact. `slot_intake._intake_executor` reads
`success = bool(response_data.get(success_check))` and maps `outputs` by FLAT top-level
key, so the wrapper flattens the dot-paths in `outputs` to top-level keys and reports a
real `success` flag. A raw REST payload is nested and carries no `success`, so a task
pointed straight at one would look failed on every fire — the same trap A2A sets, and
the reason `outputs` here takes paths instead of names.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Union

from . import tools as _tools
from .toolset_common import (
    ENV_VAR,
    ToolsetAuth,
    _normalize_params,
    _py_name,
    _require,
    _TOOL_NAME_RE,
    _TOOLSET_NAME_RE,
    check_no_overlapping_paths,
    register_wrapper,
    task_output_keys,
    wrapper_tool_source,
)

# The OpenAPI toolset keeps only what is specific to a REST API: the spec parser and the
# `openApiToolset` resource. The auth builders, the generated wrapper, and the mock
# machinery are shared with the MCP toolset and live in `toolset_common`.
_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


# ---------------------------------------------------------------------------
# Spec parsing — enough to validate offline. Not a general OpenAPI implementation:
# it answers "does this operation exist, and are these its parameters".
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
  """One operation in a spec, reduced to what validation needs."""

  operation_id: str
  method: str
  path: str
  summary: str
  params: tuple[str, ...]           # path/query/header parameter names
  required: tuple[str, ...]         # of those, the required ones
  body_params: tuple[str, ...]      # request-body leaves as dot-paths
  required_body: tuple[str, ...]
  # Dot-paths of every leaf of the 2xx response schema, with a list step written as
  # `.0.` — the form a runtime dot-path walk actually takes. Empty when the spec
  # declares no response schema, which is allowed and means output keys go unchecked.
  response_paths: tuple[str, ...] = ()

  @property
  def all_params(self) -> tuple[str, ...]:
    return self.params + self.body_params

  @property
  def all_required(self) -> tuple[str, ...]:
    return self.required + self.required_body

  @property
  def response_aliases(self) -> dict[str, str]:
    """`{short_name: full_path}` for every response leaf whose NAME is unambiguous.

    So a flow can ask for `state` rather than `places.0.state`. A name appearing at
    two different paths is not an alias — silently picking one would fill a slot from
    whichever branch happened to sort first.
    """
    by_leaf: dict[str, list[str]] = {}
    for path in self.response_paths:
      by_leaf.setdefault(path.split(".")[-1], []).append(path)
    return {leaf: paths[0] for leaf, paths in by_leaf.items()
            if len(paths) == 1 and leaf != paths[0]}

  def resolve_output(self, key: str) -> Optional[str]:
    """The response dot-path a task's output key refers to, or None if unknown.

    Accepts the full path, an unambiguous leaf name, or — when the spec declared no
    response schema — anything, since there is nothing to check it against.
    """
    if not self.response_paths:
      return key
    if key in self.response_paths:
      return key
    alias = self.response_aliases.get(key)
    if alias:
      return alias
    # A list index the schema describes generically: `items.0.sku` is declared, and
    # `items.3.sku` is the same field of a later element.
    generic = re.sub(r"(?<=\.)\d+(?=\.)", "0", key)
    return key if generic in self.response_paths else None


def _looks_like_a_path(text: str) -> bool:
  """Whether `spec` was meant as a filename rather than as the document itself.

  A document always spans lines, or (one-line JSON) opens with a brace — and one-line
  JSON is exactly why a bare "contains a slash" test will not do, since its own
  `paths` keys are full of them.
  """
  stripped = text.strip()
  if not stripped or "\n" in stripped or stripped.startswith(("{", "[")):
    return False
  return (stripped.lower().endswith((".yaml", ".yml", ".json"))
          or "/" in stripped or os.sep in stripped)


def _load_spec(spec: Union[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
  """`(parsed, raw_text)` for a spec given as a dict, a file path, or raw text."""
  import yaml  # local: pyyaml is a core dep, but keep the import surface tight

  if isinstance(spec, Mapping):
    return dict(spec), yaml.safe_dump(dict(spec), sort_keys=False)
  text = str(spec)
  if _looks_like_a_path(text):
    # Reported separately: a path that does not resolve would otherwise be parsed AS
    # the document, succeed as a plain YAML string, and fail two steps later with
    # "spec must be an OpenAPI document (a mapping), got str" — which hides the fact
    # that the file simply is not there.
    if not os.path.isfile(text):
      raise ValueError(
          f"openapi_toolset(): spec file not found: {text!r} (resolved from "
          f"{os.path.abspath(text)!r}). Paths are relative to the working directory, "
          "not to the file calling this"
      )
    with open(text, "r", encoding="utf-8") as fh:
      text = fh.read()
  try:
    parsed = yaml.safe_load(text)
  except Exception as exc:
    raise ValueError(f"openapi_toolset(): spec is not valid YAML/JSON — {exc}") from exc
  if not isinstance(parsed, dict):
    raise ValueError(
        "openapi_toolset(): spec must be an OpenAPI document (a mapping), got "
        f"{type(parsed).__name__}. Pass a path to a .yaml/.json file, the document "
        "text, or a dict"
    )
  return parsed, text


def _resolve_ref(doc: Mapping[str, Any], node: Any, depth: int = 0) -> dict[str, Any]:
  """Follow a local `$ref` into the document (bounded, non-local refs left alone)."""
  if not isinstance(node, Mapping):
    return {}
  ref = node.get("$ref")
  if not isinstance(ref, str) or not ref.startswith("#/") or depth > 8:
    return dict(node)
  target: Any = doc
  for part in ref[2:].split("/"):
    if not isinstance(target, Mapping):
      return {}
    target = target.get(part.replace("~1", "/").replace("~0", "~"))
  return _resolve_ref(doc, target, depth + 1)


def _compose(doc: Mapping[str, Any], node: Any, depth: int = 0) -> dict[str, Any]:
  """A schema with `allOf` / `anyOf` / `oneOf` flattened into one effective shape.

  Spec generators express composition and inheritance this way constantly, and a node
  carrying only an `allOf` has no `properties` of its own — so without this, its fields
  are invisible: a valid body parameter is rejected as unknown, and a response path the
  flow asks for cannot be resolved.

  `allOf` members all apply, so their properties AND their `required` merge. `anyOf` /
  `oneOf` members are alternatives: their properties merge so every branch's fields are
  discoverable, but nothing they mark required is required of the caller, who may be
  satisfying a different branch.
  """
  node = _resolve_ref(doc, node)
  if depth > 8:
    return dict(node)
  out = {k: v for k, v in node.items() if k not in ("allOf", "anyOf", "oneOf")}
  props: dict[str, Any] = dict(out.get("properties") or {})
  required: list[str] = list(out.get("required") or [])
  for keyword in ("allOf", "anyOf", "oneOf"):
    for member in node.get(keyword) or []:
      sub = _compose(doc, member, depth + 1)
      props.update(sub.get("properties") or {})
      if keyword == "allOf":
        required.extend(sub.get("required") or [])
      for carried in ("type", "items"):
        if not out.get(carried) and sub.get(carried):
          out[carried] = sub[carried]
  if props:
    out["properties"] = props
  if required:
    out["required"] = list(dict.fromkeys(required))
  return out


def _body_leaves(
    doc: Mapping[str, Any], schema: Any, prefix: str = "", depth: int = 0,
    required: bool = True,
) -> list[tuple[str, bool]]:
  """`(dot_path, required)` for each leaf of a request-body schema.

  Objects are descended into dot-paths (the wrapper reassembles them); arrays stay
  whole, matching how the api_hub exporter shapes the same call. A nested leaf is
  required only if every object on the way down to it was too — a field marked
  required inside an OPTIONAL object is not required of the caller.
  """
  node = _compose(doc, schema)
  props = node.get("properties")
  if isinstance(props, Mapping) and depth <= 6:
    req = set(node.get("required") or [])
    out: list[tuple[str, bool]] = []
    for key, sub in props.items():
      child = f"{prefix}.{key}" if prefix else str(key)
      out.extend(_body_leaves(doc, sub, child, depth + 1, required and key in req))
    return out
  return [(prefix, required)] if prefix else []


def _response_leaves(
    doc: Mapping[str, Any], schema: Any, prefix: str = "", depth: int = 0,
) -> list[str]:
  """Dot-paths of every leaf of a response schema, a list step written as `.0.`.

  `.0.` rather than `[]` because these paths are used verbatim: as the argument to the
  runtime dot-path walk, and as the output key a task names. A caller wanting a later
  element writes `.3.`, which `Operation.resolve_output` recognizes as the same field.
  """
  node = _compose(doc, schema)
  if depth > 7:
    return []
  props = node.get("properties")
  if isinstance(props, Mapping):
    out: list[str] = []
    for key, sub in props.items():
      child = f"{prefix}.{key}" if prefix else str(key)
      out.extend(_response_leaves(doc, sub, child, depth + 1))
    return out
  if node.get("type") == "array" or "items" in node:
    child = f"{prefix}.0" if prefix else "0"
    return _response_leaves(doc, node.get("items") or {}, child, depth + 1)
  return [prefix] if prefix else []


def _success_response_schema(doc: Mapping[str, Any], op: Mapping[str, Any]) -> Any:
  """The JSON schema of the operation's first 2xx response, or None."""
  responses = op.get("responses")
  if not isinstance(responses, Mapping):
    return None
  for code, entry in responses.items():
    if not str(code).startswith("2"):
      continue
    content = _resolve_ref(doc, entry).get("content")
    if not isinstance(content, Mapping):
      continue
    for media, spec in content.items():
      if "json" in str(media).lower() and isinstance(spec, Mapping):
        return spec.get("schema")
  return None


def _responses_without_description(doc: Mapping[str, Any]) -> list[str]:
  """`METHOD /path -> <code>` for each response missing its required `description`.

  OpenAPI makes `description` REQUIRED on a Response Object, and CES enforces it by
  dropping the whole toolset at import — with no error on the push. The first sign is
  a live call failing with `Tool with name <toolset>_<operationId> not found`, which
  points at the operation rather than at the spec. Confirmed by pushing one spec twice,
  differing only by this line (see `tests/test_openapi.py`).
  """
  out: list[str] = []
  for path, item in (doc.get("paths") or {}).items():
    if not isinstance(item, Mapping):
      continue
    for method, op in item.items():
      if method.lower() not in _HTTP_METHODS or not isinstance(op, Mapping):
        continue
      responses = op.get("responses")
      if not isinstance(responses, Mapping):
        continue  # malformed; `_parse_operations` reports what it can and moves on
      for code, response in responses.items():
        resolved = _resolve_ref(doc, response) if isinstance(response, Mapping) else {}
        if not str(resolved.get("description") or "").strip():
          out.append(f"{method.upper()} {path} -> {code}")
  return out


def _operations_without_id(doc: Mapping[str, Any]) -> list[str]:
  """`METHOD /path` for each operation the spec left without an `operationId`."""
  out: list[str] = []
  for path, item in (doc.get("paths") or {}).items():
    if not isinstance(item, Mapping):
      continue
    for method, op in item.items():
      if method.lower() not in _HTTP_METHODS or not isinstance(op, Mapping):
        continue
      if not str(op.get("operationId") or "").strip():
        out.append(f"{method.upper()} {path}")
  return out


def _parse_operations(doc: Mapping[str, Any]) -> dict[str, Operation]:
  """Every operation in the document, keyed by `operationId`."""
  out: dict[str, Operation] = {}
  paths = doc.get("paths")
  if not isinstance(paths, Mapping):
    return out
  for path, item in paths.items():
    if not isinstance(item, Mapping):
      continue
    shared = item.get("parameters") or []
    for method, op in item.items():
      if method.lower() not in _HTTP_METHODS or not isinstance(op, Mapping):
        continue
      op_id = op.get("operationId")
      if not isinstance(op_id, str) or not op_id.strip():
        continue
      names: list[str] = []
      required: list[str] = []
      for raw in [*shared, *(op.get("parameters") or [])]:
        p = _resolve_ref(doc, raw)
        name = p.get("name")
        if not isinstance(name, str) or name in names:
          continue
        names.append(name)
        if p.get("required"):
          required.append(name)
      body_leaves: list[tuple[str, bool]] = []
      body = _resolve_ref(doc, op.get("requestBody"))
      content = body.get("content")
      if isinstance(content, Mapping):
        # Any JSON-ish media type; the first one wins (CES sends JSON).
        for media, entry in content.items():
          if "json" in str(media).lower() and isinstance(entry, Mapping):
            body_leaves = _body_leaves(doc, entry.get("schema"))
            break
      out[op_id.strip()] = Operation(
          operation_id=op_id.strip(),
          method=method.upper(),
          path=str(path),
          summary=str(op.get("summary") or op.get("description") or "").strip(),
          params=tuple(names),
          required=tuple(required),
          body_params=tuple(p for p, _ in body_leaves),
          required_body=tuple(p for p, r in body_leaves if r),
          response_paths=tuple(
              _response_leaves(doc, _success_response_schema(doc, op))),
      )
  return out


# ---------------------------------------------------------------------------
# The toolset
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenApiToolset:
  """A REST API declared as one CES `openApiToolset` resource.

  Emitted as `toolsets/<name>/<name>.json` plus the spec at
  `toolsets/<name>/open_api_toolset/open_api_schema.yaml`. Nothing calls it directly:
  pair it with `api_tool()` for each operation a flow needs.
  """

  name: str
  spec_text: str
  description: str = ""
  base_url: str = ""
  auth: Optional[ToolsetAuth] = None
  env_scoped: bool = False
  operations: Mapping[str, Operation] = field(default_factory=dict)
  # DECLARED rather than consumed: no `spec=` was given, so the spec is generated from
  # the `remote_tool(...)` calls that name this toolset. `operations` is then filled in
  # by those calls rather than parsed up front, and `spec_text` is empty until `payload`
  # renders it. The distinction is the whole difference between the two tool families:
  # `api_tool` maps onto someone else's spec, `remote_tool` defines one.
  declared: bool = False
  # `{operationId: mock}` for operations whose wrapper the build generates. An
  # `api_tool` declares its own mock instead; this is where one lives when there is no
  # `api_tool` call to hang it on.
  mocks: Mapping[str, Any] = field(default_factory=dict)

  @property
  def schema_path(self) -> str:
    """App-root-relative path of the emitted spec (what the resource points at)."""
    return f"toolsets/{self.name}/open_api_toolset/open_api_schema.yaml"

  def symbol(self, operation_id: str) -> str:
    """The in-sandbox callable for an operation: `<toolset>_<operationId>`.

    The operationId is used VERBATIM — CES derives the symbol from the spec, so
    snake-casing a camelCase id here produces a name that does not exist.
    """
    return f"{self.name}_{operation_id}"

  def resource_body(self) -> dict[str, Any]:
    """The `toolsets/<name>/<name>.json` resource, minus its `name` (the UUID).

    The emitter mints and prepends that, as it does for every other resource, so
    UUID uniqueness stays checkable in one place.
    """
    block: dict[str, Any] = {"openApiSchema": self.schema_path}
    if self.auth is not None:
      auth = self.auth.to_dict(env_scoped=self.env_scoped)
      if auth:
        block["apiAuthentication"] = auth
    doc: dict[str, Any] = {"displayName": self.name, "openApiToolset": block}
    if self.description:
      doc["description"] = self.description
    return doc

  def spec_document(self) -> str:
    """The spec to emit — supplied verbatim, or generated from the declared operations.

    A declared toolset has no text until now on purpose: `remote_tool(...)` calls add
    their operations after the toolset object exists, so rendering any earlier would
    freeze a spec with nothing in it.
    """
    if not self.declared:
      return self.spec_text
    if not self.operations:
      raise ValueError(
          f"openapi_toolset({self.name!r}): declared with no `spec=` and no "
          "`remote_tool(...)` names it, so the emitted spec would have no operations "
          "and CES would drop the toolset at import. Either pass `spec=` to consume an "
          "existing API, or declare at least one remote tool over this toolset")
    return _render_declared_spec(self)

  def payload(self) -> dict[str, Any]:
    """What the scaffold request carries: the resource body plus the spec text."""
    return {"name": self.name, "resource": self.resource_body(),
            "spec": self.spec_document()}

  def environment_entry(self) -> Optional[dict[str, Any]]:
    """This toolset's `environment.json` entry, or None when it needs none.

    Only an `env_scoped` toolset produces one. Everything else is written literally
    into the resource and the spec, so the emitted app dir is self-contained.
    """
    if not self.env_scoped:
      return None
    block: dict[str, Any] = {}
    if self.base_url:
      block["url"] = self.base_url
    if self.auth is not None:
      auth = self.auth.environment_auth()
      if auth:
        block["apiAuthentication"] = auth
    return {"openApiToolset": block} if block else None


def openapi_toolset(
    name: str,
    *,
    spec: Union[str, Mapping[str, Any], None] = None,
    description: str = "",
    base_url: str = "",
    auth: Optional[ToolsetAuth] = None,
    env_scoped: bool = False,
    mocks: Optional[Mapping[str, Any]] = None,
) -> OpenApiToolset:
  """Declare a REST API as a CES OpenAPI toolset.

  Args:
    name: The toolset's display name. Must be a python identifier, because it
      prefixes the generated in-sandbox symbol `tools.<name>_<operationId>` — a dash
      here is a `NameError` on the first live call, not a build error.
    spec: The OpenAPI document — a path to a `.yaml`/`.json` file, the document text,
      or an already-parsed dict. OMIT it to declare the API instead of consuming one:
      the spec is then generated from the `remote_tool(...)` calls that name this
      toolset, which is the right way round when the service is yours and implements
      what the agent specifies rather than the other way about.
    description: What the API is for. Shown on the resource; the model never sees it
      (it sees each `api_tool`'s description instead).
    base_url: The server to call. Overrides the spec's own `servers`, which is the
      usual case — a spec published by another team names ITS environment, not yours.
    auth: How CES authenticates (see `api_key_auth` and friends). None emits no
      `apiAuthentication` block, for a genuinely open API.
    env_scoped: Move the URL and the secret references into `environment.json` and
      leave `$env_var` markers behind, the layout a pulled CES app uses. Off by
      default: inlining keeps the emitted dir self-contained and pushable as-is.
    mocks: `{operationId: mock}` used when `mock_apis` is on, for operations whose
      wrapper the build generates. Each is JSON data or a `fn(request) -> dict`
      returning what the REAL API would return.

  Returns:
    An `OpenApiToolset` to pass to `App(toolsets=[...])`.
  """
  nm = _require(name, "openapi_toolset(): name")
  # A trailing slash would meet the spec's own leading-slash paths and send the call to
  # `https://host//users`, which some gateways 404.
  base_url = base_url.rstrip("/")
  if not _TOOLSET_NAME_RE.match(nm):
    raise ValueError(
        f"openapi_toolset(): name {nm!r} must be a python identifier (letters, digits "
        "and '_', not starting with a digit) — it prefixes the generated symbol "
        f"tools.{nm}_<operationId>, which the sandbox resolves at call time"
    )
  if base_url and not base_url.startswith(("https://", "http://")):
    raise ValueError(
        f"openapi_toolset({nm!r}): base_url must be an absolute URL, got {base_url!r}")
  if auth is not None and not isinstance(auth, ToolsetAuth):
    raise ValueError(
        f"openapi_toolset({nm!r}): auth must be built with flows.api_key_auth(), "
        f"flows.oauth_auth(), flows.bearer_auth() or flows.service_agent_auth(), got "
        f"{type(auth).__name__}"
    )
  if spec is None:
    # Declared, not consumed. `operations` is a real dict on purpose: the
    # `remote_tool(...)` calls that follow add to it, which is the only way an operation
    # declared after the toolset object exists can reach the emitted spec.
    if mocks:
      raise ValueError(
          f"openapi_toolset({nm!r}): `mocks` keys operations from a spec, and this "
          "toolset declares its operations instead. Put the mock on the tool: "
          "flows.remote_tool(..., mock=...)")
    return OpenApiToolset(
        name=nm, spec_text="", description=str(description or "").strip(),
        base_url=base_url, auth=auth, env_scoped=env_scoped,
        operations={}, mocks={}, declared=True)

  doc, text = _load_spec(spec)
  operations = _parse_operations(doc)
  if not operations:
    # Real CES specs do ship operations with no operationId. No name is invented for
    # them: CES derives the sandbox symbol from the spec, and a guess that does not
    # match is a NameError on the first live call rather than a build error here.
    missing = ", ".join(_operations_without_id(doc)) or "(none)"
    raise ValueError(
        f"openapi_toolset({nm!r}): the spec declares no operations with an "
        "`operationId`, and CES derives one callable per operationId — so nothing in "
        f"it is reachable. Add an operationId to: {missing}"
    )
  undescribed = _responses_without_description(doc)
  if undescribed:
    raise ValueError(
        f"openapi_toolset({nm!r}): {len(undescribed)} response(s) have no "
        "`description`, which OpenAPI requires — CES rejects the spec and drops the "
        "WHOLE toolset at import without failing the push, so the first sign is a live "
        f"call failing with 'Tool with name {nm}_<operationId> not found'. Add one to: "
        + ", ".join(undescribed[:5])
        + (f" (and {len(undescribed) - 5} more)" if len(undescribed) > 5 else "")
    )
  unknown_mocks = sorted(set(mocks or ()) - set(operations))
  if unknown_mocks:
    raise ValueError(
        f"openapi_toolset({nm!r}): mocks name operation(s) "
        f"{', '.join(repr(m) for m in unknown_mocks)} that the spec does not declare. "
        f"Available: {', '.join(sorted(operations))}"
    )
  if base_url:
    doc = dict(doc)
    doc["servers"] = [{"url": ENV_VAR if env_scoped else base_url}]
    import yaml
    text = yaml.safe_dump(doc, sort_keys=False)
  return OpenApiToolset(
      name=nm,
      spec_text=text,
      description=str(description or "").strip(),
      base_url=base_url,
      auth=auth,
      env_scoped=env_scoped,
      operations=operations,
      mocks=dict(mocks or {}),
  )


@dataclass(frozen=True)
class ApiTool:
  """A generated wrapper around one toolset operation.

  Its `name` is what a task's `tool` names; `str(...)` gives that name too, so it can
  be passed to `flow.task(...)` directly.
  """

  name: str
  toolset: OpenApiToolset
  operation_id: str
  params: Mapping[str, str]
  outputs: Mapping[str, str]
  has_mock: bool = False

  def __str__(self) -> str:
    return self.name


def api_tool(
    name: str,
    toolset: OpenApiToolset,
    operation_id: str,
    *,
    params: Union[Sequence[str], Mapping[str, str], None] = None,
    outputs: Optional[Mapping[str, str]] = None,
    description: str = "",
    mock: Any = None,
    param_types: Optional[Mapping[str, str]] = None,
) -> ApiTool:
  """Make one operation on a toolset callable from a flow.

  This is the piece that turns a toolset into something an agent can use. CES exposes
  each operation only inside the sandbox, as `tools.<toolset>_<operationId>`, so the
  callable tool has to be a `pythonFunction` that forwards to it — exactly what the
  reference app hand-writes for every one of its API calls, generated here instead.

  The generated tool registers itself, so nothing needs threading through
  `App(tool_bodies=...)`; name it from a task like any other tool.

  Args:
    name: The tool's name — what `task(tool=...)` refers to and what the model calls.
    toolset: The `openapi_toolset(...)` that owns the operation.
    operation_id: The spec's `operationId`. Checked against the parsed spec, so a typo
      is a build error rather than a `NameError` on the first live call.
    params: The operation parameters to expose. Defaults to every parameter the spec
      declares, so pass this only to expose a SUBSET. A list uses the wire names as
      argument names; a `{arg: wire}` dict renames them, and a dotted wire path nests
      the value in the request body.
    outputs: `{output_key: dot.path}` read out of the response and flattened to the
      top level, because intake maps a task's `outputs` by FLAT key. Pass this to give
      a path a friendlier name than the spec's. Omitted, the build fills it from the
      keys the tasks firing this tool ask for, resolved against the response schema.
    description: What the tool does, for the model. Defaults to the operation's
      `summary` from the spec.
    mock: A stand-in answer used when the `mock_apis` flag is on — either JSON data or
      a `fn(request) -> dict`. It returns what the REAL API would return, not what this
      tool returns, so the same extraction and `success` rule run over it and a mocked
      run proves the mapping. Set the flag with `App(mock_apis=True)` or per session;
      with no mock declared the call always goes live.

  Returns:
    An `ApiTool` whose `name` a task can fire.
  """
  if not isinstance(toolset, OpenApiToolset):
    raise ValueError(
        "api_tool(): toolset must be a flows.openapi_toolset(...), got "
        f"{type(toolset).__name__}"
    )
  nm = _require(name, "api_tool(): name")
  if not _TOOL_NAME_RE.match(nm):
    raise ValueError(
        f"api_tool(): name {nm!r} must be a python identifier — it is emitted verbatim "
        "as the generated wrapper's `def` entrypoint, which CES calls by name"
    )
  op_id = _require(operation_id, f"api_tool({nm!r}): operation_id")
  op = toolset.operations.get(op_id)
  if op is None:
    known = ", ".join(sorted(toolset.operations)) or "(none)"
    raise ValueError(
        f"api_tool({nm!r}): operation {op_id!r} is not in the {toolset.name!r} spec. "
        f"CES derives one callable per operationId, so this would be a NameError on "
        f"tools.{toolset.symbol(op_id)} at the first live call. Available: {known}"
    )
  resolved = _normalize_params(params)
  if params is None:
    # Every parameter the spec declares — the spec already says what they are, and
    # restating them here was pure duplication. An optional one the flow never supplies
    # is simply omitted from the request. Pass `params=` only to expose a SUBSET.
    resolved = {_py_name(p): p for p in op.all_params}
  unknown = sorted(set(resolved.values()) - set(op.all_params))
  if unknown:
    known = ", ".join(op.all_params) or "(none)"
    raise ValueError(
        f"api_tool({nm!r}): {', '.join(repr(u) for u in unknown)} "
        f"{'is' if len(unknown) == 1 else 'are'} not "
        f"{'a parameter' if len(unknown) == 1 else 'parameters'} of {op_id!r} "
        f"({op.method} {op.path}). Available: {known}"
    )
  missing = sorted(set(op.all_required) - set(resolved.values()))
  if missing:
    raise ValueError(
        f"api_tool({nm!r}): {op_id!r} requires {', '.join(repr(m) for m in missing)}, "
        "which no argument supplies — the call would be rejected by the API at "
        "runtime. Add them to params"
    )
  out_map = {str(k): str(v) for k, v in (outputs or {}).items()}
  for key in out_map:
    if key in ("success", "error", "response"):
      raise ValueError(
          f"api_tool({nm!r}): output key {key!r} is reserved — the wrapper sets it "
          "itself ('success' is what intake reads to decide the call worked)"
      )
  check_no_overlapping_paths(f"api_tool({nm!r})", resolved)
  desc = str(description or op.summary or f"{op.method} {op.path}").strip()
  _register_wrapper(
      nm, toolset, op, params=resolved, outputs=out_map, description=desc,
      mock=mock, mock_default=False, derive_outputs=outputs is None,
      param_types=param_types)
  return ApiTool(
      name=nm, toolset=toolset, operation_id=op_id,
      params=resolved, outputs=out_map, has_mock=mock is not None)


@dataclass(frozen=True)
class AfterTurns:
  """A mock that resolves n turns after it starts, rather than at once."""

  turns: int
  value: Any


@dataclass(frozen=True)
class RemoteError:
  """A mock that fails, so a failure branch is drivable with no service deployed."""

  error_code: str


def after_turns(turns: int, value: Any) -> AfterTurns:
  """Resolve a remote tool's mock n turns from now, instead of immediately.

  A plain `mock=` resolves inline, which keeps existing offline checks working — they
  should not have to know a tool is remote. This is the opposite need: it holds the job
  open so the WAITING is exercised, which is the only way `while_waiting`, `on_timeout`
  and a group's staggered `then_say` can be driven without deploying anything.

  Worth having because copy that is only reachable live is copy nobody checks. Lines
  authored, validated, emitted and never spoken is a real failure this prevents.

  `value` is anything a plain `mock=` accepts, a code block included — so a scenario
  chosen by a session variable can also be held open for a few turns.
  """
  if turns < 1:
    raise ValueError(f"after_turns(): turns must be at least 1, got {turns!r}")
  return AfterTurns(turns=int(turns), value=value)


def remote_error(error_code: str, *, turns: int = 1) -> AfterTurns:
  """Fail a remote tool's mock, so `on_failure` branches are drivable offline.

  `error_code` reaches the task unchanged, and `on_failure`'s `retry_say`, `clear_slots`
  and `on_exhaust.say` already accept a dict keyed by it — so a failure branch is
  authored and tested in the vocabulary every other tool's failures already use.
  """
  return AfterTurns(turns=int(turns), value=RemoteError(str(error_code)))


# The suffix the engine appends to a SYNTHETIC job handle, carrying how many turns the
# wait has had. A mock is the only thing that ever sees it: the start wrapper answers
# `mock-<name>` when a mock is declared, and the engine appends this only to a handle
# that begins that way, so a handle a real service issued is never touched.
MOCK_HANDLE_PREFIX = "mock-"
MOCK_TURN_SEPARATOR = "#"


def _remote_status_mock_source(name: str, mock: Any) -> str:
  """The status wrapper's mock: the poll's answer, in the contract's own envelope.

  A remote tool's mock is unlike every other mock in the SDK in two ways, and both fall
  out of it answering the POLL rather than the call.

  The first is the envelope. A poll answers `{"status": ..., "result": {...}}`, never a
  bare payload, so this is where a plain `mock={"rows": 12}` becomes a finished job and
  where `remote_error("remote_job_lost")` becomes a failed one. A code block may return
  either: a dict whose `status` is one of the four contract words is passed through
  unchanged, anything else is wrapped as a finished job's result. There is no ambiguity
  between the two, because `status` and `error_code` are refused as output names.

  The second is that the poll carries the HANDLE and nothing else — the arguments the
  job started with are two turns gone. So a code block here takes no parameters. What it
  reads instead is the session, through the `context` every emitted tool has, and that is
  the whole point of allowing one: a scenario an agent selects with a variable (a
  `mock_config_string`, a test account) cannot be expressed as one static payload, and
  before this it could not be expressed at all.

  `after_turns(n, ...)` is honoured here too, off the turn count the engine appends to
  the synthetic handle. It has to be a mock's own business: the alternative is the engine
  fabricating a tool response it never dispatched, and a wait that is faked from BOTH
  ends proves nothing about the path a real job takes.
  """
  # noqa: PLC0415 (circular at module level)
  from .toolset_common import check_mock_callable, mock_tool_name

  tool = mock_tool_name(f"{name}__status")
  turns, value = (mock.turns, mock.value) if isinstance(mock, AfterTurns) else (0, mock)

  body = ""
  if isinstance(value, RemoteError):
    answer = repr({"status": "failed", "error_code": value.error_code})
  elif callable(value):
    # A lambda, a class or a closure produces an emitted tool that will not load, or one
    # that answers the wrong thing — all three at a distance from the `mock=` that caused
    # it. Refused by name here; see `check_mock_callable`.
    check_mock_callable(f"remote_tool({name!r})", value)
    _check_status_mock_signature(name, value)
    try:
      body = _tools.render_callable(value).rstrip()
    except (OSError, TypeError) as exc:
      # The block is INLINED into the emitted tool, so its source has to be readable.
      raise ValueError(
          f"remote_tool({name!r}): the mock "
          f"{getattr(value, '__name__', type(value).__name__)!r} has no source that can "
          f"be read ({type(exc).__name__}: {exc}), and a mock is emitted as a tool by "
          "inlining it. Use a def in a real module, or pass the answer as plain data"
      ) from exc
    answer = f"_envelope({getattr(value, '__name__', '')}())"
  else:
    try:
      json.dumps(value)
    except TypeError as exc:
      raise ValueError(
          f"remote_tool({name!r}): mock must be JSON-serializable data, a callable, "
          f"flows.after_turns(...) or flows.remote_error(...), got "
          f"{type(value).__name__} ({exc})") from exc
    answer = repr({"status": "done", "result": value})

  lines = [f'"""Mock for the {name} poll (generated). Edit to change the answer."""',
           _tools._HEADER.rstrip(), ""]
  if body:
    lines += ["", body, ""]
  lines += [
      "",
      "def _envelope(answer: Any) -> dict:",
      '  """A result becomes a finished job; a status answer is already one."""',
      f"  if isinstance(answer, dict) and answer.get('status') in {list(REMOTE_STATUSES)}:",
      "    return answer",
      "  return {'status': 'done', 'result': answer}",
      "",
      "",
      "def _waited(job_id: str) -> int:",
      '  """Turns this wait has had, which the engine appends to a synthetic handle."""',
      f"  head, sep, tail = str(job_id or '').rpartition({MOCK_TURN_SEPARATOR!r})",
      "  if not sep:",
      "    return 0",
      "  try:",
      "    return int(tail)",
      "  except ValueError:",
      "    return 0",
      "",
      "",
      f"def {tool}(jobId: str = '') -> Any:",
      '  """The poll\'s answer, in the shape the service\'s status operation returns.',
      "",
      "  The wrapper runs its ordinary extraction over this, so a mocked run exercises",
      "  the same status handling and the same output flattening a live job depends on.",
      '  """',
  ]
  if turns:
    lines += [
        f"  if _waited(jobId) < {turns}:",
        "    return {'status': 'running'}",
    ]
  lines += [f"  return {answer}", ""]
  return "\n".join(lines)


def _check_status_mock_signature(name: str, mock: Any) -> None:
  """A poll's code block takes nothing — refuse a signature that expects otherwise.

  Silently calling it with no arguments would be worse: a mock written against the
  tool's own parameters would raise inside the sandbox on the first poll, which surfaces
  as a tool that answered nothing rather than as a mistake in this file.
  """
  import inspect  # noqa: PLC0415

  try:
    sig = inspect.signature(mock)
  except (TypeError, ValueError):  # a builtin or C callable — nothing to inspect
    return
  required = [p.name for p in sig.parameters.values()
              if p.default is p.empty
              and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
  if required:
    raise ValueError(
        f"remote_tool({name!r}): the mock takes {', '.join(repr(r) for r in required)}, "
        "and a poll cannot supply it — the status call carries the job handle and "
        "nothing else, so the arguments the job started with are gone by then. Take no "
        "parameters and read what you need off `context.variables` / `context.state`, "
        "which is what makes a code block worth writing here")


_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}

# What the status operation answers with, and the only vocabulary the watcher reads.
# `running` is the pending case; the other three are terminal and carry an `error_code`
# straight into the task's `on_failure`, which already branches by code.
REMOTE_STATUSES = ("running", "done", "failed", "timeout")


def _json_schema(fields: Mapping[str, Any], where: str) -> dict[str, Any]:
  """`{name: python type}` as an OpenAPI object schema."""
  props: dict[str, Any] = {}
  for key, typ in fields.items():
    if typ not in _JSON_TYPES:
      raise ValueError(
          f"{where}: {key!r} is declared as {getattr(typ, '__name__', typ)!r}, and a "
          "remote tool's wire types must be one of str, int, float, bool — the service "
          "implements this from the generated spec, so a type OpenAPI cannot express "
          "has nothing to generate")
    props[str(key)] = {"type": _JSON_TYPES[typ]}
  return {"type": "object", "properties": props}


def _render_declared_spec(toolset: "OpenApiToolset") -> str:
  """Generate the OpenAPI document a declared toolset's service must implement.

  Two operations per remote tool — the call and its status — because that pair is what
  lets the agent start work it will not wait for. Every response carries a
  `description`: CES drops a whole toolset whose spec omits one, without failing the
  push, so the first symptom would be a live call that cannot find its tool.
  """
  paths: dict[str, Any] = {}
  for spec in sorted(_declared_ops(toolset), key=lambda s: s["operation_id"]):
    op_id = spec["operation_id"]
    result = _json_schema(spec["outputs"], f"remote_tool({spec['name']!r}) outputs")
    paths[f"/{op_id}"] = {"post": {
        "operationId": op_id,
        "summary": spec["description"] or f"Start {spec['name']}.",
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": _json_schema(spec["params"],
                                   f"remote_tool({spec['name']!r}) params")}}},
        "responses": {"200": {
            "description": "The job handle. The work continues after this returns.",
            "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"jobId": {"type": "string"}},
                "required": ["jobId"]}}}}},
    }}
    paths[f"/{op_id}/{{jobId}}"] = {"get": {
        "operationId": f"{op_id}Status",
        "summary": f"Progress of a {spec['name']} job.",
        "parameters": [{"name": "jobId", "in": "path", "required": True,
                        "schema": {"type": "string"}}],
        "responses": {"200": {
            "description": "Whether the job is still running, and its result once done.",
            "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(REMOTE_STATUSES)},
                    "error_code": {"type": "string"},
                    "result": result},
                "required": ["status"]}}}}},
    }}
  doc = {
      "openapi": "3.0.3",
      "info": {"title": toolset.name, "version": "1.0.0"},
      "servers": [{"url": ENV_VAR if toolset.env_scoped else toolset.base_url}],
      "paths": paths,
  }
  import yaml
  return yaml.safe_dump(doc, sort_keys=False)


# `{toolset name: [declared op spec]}`. Kept beside the toolset rather than on it so the
# dataclass stays frozen and hashable; the toolset name is unique per app.
_DECLARED: dict[str, list[dict[str, Any]]] = {}
# `{tool name: RemoteTool}` — every declaration made this process. The build reads it to
# emit the engine's registry, the same way the tool registry is read for bodies.
_REMOTE_TOOLS: dict[str, "RemoteTool"] = {}


def _declared_ops(toolset: "OpenApiToolset") -> list[dict[str, Any]]:
  return list(_DECLARED.get(toolset.name, ()))


def registered_remote_tools() -> dict[str, "RemoteTool"]:
  """Every `remote_tool(...)` declared, by name. Read at build to emit the registry."""
  return dict(_REMOTE_TOOLS)


def remote_registry(tool_names) -> dict[str, dict[str, Any]]:
  """The `remote_tools` config block for the tools a config actually fires.

  What the engine needs and nothing more: which status tool partners each start tool,
  where the handle lands, which outputs to lift off a finished job, and the wall-clock
  budget. Scoped to the naming config so a flow carries no entry for a tool it never
  fires.

  A mock is deliberately NOT here. It used to be, as `{"turns": n, "result": ...}`, and
  no line of the engine ever read it — so `after_turns(...)` emitted a config field
  nobody consumed, no HTTP mock at all, and an app that quietly called the real service
  while its author believed it was mocked. The count is carried to the emitted mock on
  the poll's own handle instead (`_remote_poll_handle`), where something reads it.
  """
  out: dict[str, dict[str, Any]] = {}
  for name in tool_names:
    rt = _REMOTE_TOOLS.get(name)
    if rt is None:
      continue
    entry: dict[str, Any] = {
        "status_tool": rt.status_tool,
        "job_slot": rt.job_slot,
        "outputs": sorted(rt.outputs),
    }
    if rt.timeout:
      entry["timeout_seconds"] = int(rt.timeout)
    out[name] = entry
  return out


@dataclass(frozen=True)
class RemoteTool:
  """A tool the agent DECLARES and a separately-deployed service implements.

  `str(...)` is the name a task fires — which is the start wrapper, so the task names
  exactly what the author wrote. The status wrapper is its partner and no flow mentions
  it: the engine polls it.
  """

  name: str
  toolset: OpenApiToolset
  operation_id: str
  outputs: Mapping[str, Any]
  timeout: Optional[int] = None
  # An `after_turns(...)` mock, honoured by the engine's watcher rather than by the
  # emitted HTTP mock — see the note at the call site.
  pending_mock: Optional[AfterTurns] = None

  @property
  def status_tool(self) -> str:
    return f"{self.name}__status"

  @property
  def job_slot(self) -> str:
    """Where the handle lands. The author never names it; the watcher reads it."""
    return f"{self.name}__job"

  def __str__(self) -> str:
    return self.name


def remote_tool(
    name: str,
    toolset: OpenApiToolset,
    operation_id: str,
    *,
    params: Mapping[str, Any],
    outputs: Mapping[str, Any],
    description: str = "",
    timeout: Optional[int] = None,
    mock: Any = None,
) -> RemoteTool:
  """Declare work that runs somewhere else and outlives the turn that starts it.

  The agent owns the contract — the slots in and the slots out — and a service deployed
  on its own schedule implements it. Nothing here holds the call: starting the job and
  checking on it are both sub-second, so the work may take minutes or hours without the
  60-second tool ceiling or a held turn ever entering into it.

  Sugar over `api_tool`, not a sibling of it. One declaration builds two wrappers over
  the same toolset — `<name>` to start the job and `<name>__status` to check it — so the
  emitted app contains ordinary tools, with no new resource kind and nothing new to
  debug. The engine polls the second one; no flow ever names it.

  `params` differs from `api_tool`'s in what its values mean, and deliberately: there,
  they rename onto an existing spec's wire names, and the spec supplies the types. Here
  the declaration IS the spec, so the values are the types.

  Args:
    name: The tool's name — what `task(tool=...)` fires, and the model never calls it.
    toolset: An `openapi_toolset(...)` declared with no `spec=`. Its spec is generated
      from the tools that name it, this one included.
    operation_id: The operation to generate, and the path the service serves it on.
    params: `{name: type}` the job needs to start. Types are `str`, `int`, `float`
      or `bool` — what OpenAPI can express, since the service builds from this.
    outputs: `{name: type}` the finished job returns, flattened into the task's slots
      exactly as any other tool's outputs are.
    description: What the job does, for the model and for the generated spec.
    timeout: Seconds of WALL CLOCK the job may take, carried to the service, which is
      authoritative and stops the work. Unlike everywhere else in the engine this is
      seconds rather than turns, because a service has a real clock. Noticed only on a
      turn, so it fires at the first tick after the budget rather than exactly on it.
    mock: A stand-in, so the tool is drivable with no service deployed — engaged the
      way every other toolset mock is, by `App(mock_apis=True)` or the `mock_apis`
      session variable. Four shapes: a plain value resolves INLINE, like a synchronous
      tool, which is what keeps existing offline oracles working; a CALLABLE is a code
      block run on the poll, taking no arguments and reading the session through
      `context` — the only shape that can express a scenario the session selects;
      `flows.after_turns(n, value_or_callable)` holds the job open for n turns and
      exercises the waiting copy; `flows.remote_error(code)` fails it.

  Returns:
    A `RemoteTool` whose `name` a task can fire.
  """
  if not isinstance(toolset, OpenApiToolset):
    raise ValueError(
        "remote_tool(): toolset must be a flows.openapi_toolset(...), got "
        f"{type(toolset).__name__}")
  nm = _require(name, "remote_tool(): name")
  if not toolset.declared:
    raise ValueError(
        f"remote_tool({nm!r}): {toolset.name!r} was built with `spec=`, so it consumes "
        "an API that already exists and its operations are fixed. Use flows.api_tool() "
        "for those, or drop `spec=` to declare this service's contract here instead")
  if not _TOOL_NAME_RE.match(nm):
    raise ValueError(
        f"remote_tool(): name {nm!r} must be a python identifier — it is emitted "
        "verbatim as the generated wrapper's `def` entrypoint, which CES calls by name")
  op_id = _require(operation_id, f"remote_tool({nm!r}): operation_id")
  if not params:
    raise ValueError(
        f"remote_tool({nm!r}): declare at least one param. A remote tool shares no "
        "session state, so everything the job needs has to be passed to it")
  if not outputs:
    raise ValueError(
        f"remote_tool({nm!r}): declare at least one output, or nothing the job "
        "produces can reach a slot and the task could never complete")
  for key in outputs:
    if key in ("success", "error", "response", "status", "error_code"):
      raise ValueError(
          f"remote_tool({nm!r}): output {key!r} is reserved — the wrapper and the "
          "status contract set it themselves")
  if timeout is not None and timeout <= 0:
    raise ValueError(f"remote_tool({nm!r}): timeout must be positive, got {timeout!r}")
  clash = [s for s in _declared_ops(toolset) if s["operation_id"] == op_id]
  if clash:
    raise ValueError(
        f"remote_tool({nm!r}): operation {op_id!r} is already declared on "
        f"{toolset.name!r} by {clash[0]['name']!r}. Two operations with one id collapse "
        f"to a single callable, tools.{toolset.symbol(op_id)}")

  desc = str(description or f"Run {nm}.").strip()
  _DECLARED.setdefault(toolset.name, []).append({
      "name": nm, "operation_id": op_id, "params": dict(params),
      "outputs": dict(outputs), "description": desc})

  # The two operations, registered so `api_tool` can validate against them exactly as it
  # would against a parsed spec — the generated path and the checked path are the same.
  start_op = Operation(
      operation_id=op_id, method="POST", path=f"/{op_id}", summary=desc,
      params=(), required=(), body_params=tuple(params), required_body=tuple(params),
      response_paths=("jobId",))
  status_op = Operation(
      operation_id=f"{op_id}Status", method="GET", path=f"/{op_id}/{{jobId}}",
      summary=f"Progress of a {nm} job.", params=("jobId",), required=("jobId",),
      body_params=(), required_body=(),
      response_paths=("status", "error_code") + tuple(f"result.{k} " .strip()
                                                      for k in outputs))
  ops = toolset.operations
  ops[start_op.operation_id] = start_op          # a declared toolset's dict is mutable
  ops[status_op.operation_id] = status_op

  # Every shape of mock rides the ordinary HTTP mock machinery, and all of it lands on
  # the STATUS wrapper — which is the poll, and so the only call that ever produces a
  # result. The start wrapper's mock is the same synthetic handle either way, and that
  # handle is load-bearing: `mock-<name>` is how the engine knows a job is mocked, which
  # is what lets it hand the poll the turn count an `after_turns(...)` needs. See
  # `_remote_status_mock_source`.
  #
  # This used to be split, with the turn-based shapes "honoured by the engine's watcher".
  # They were not honoured by anything: the count was emitted into the config and no line
  # of the engine ever read it, so `after_turns(...)` and `remote_error(...)` emitted an
  # app with NO mock at all and quietly called the real service.
  if mock is None:
    start_mock = status_mock = None
  else:
    start_mock = {"jobId": f"mock-{nm}"}
    status_mock = mock
  api_tool(nm, toolset, op_id,
           param_types={k: _JSON_TYPES[t] for k, t in params.items()},
           params={k: k for k in params},
           outputs={f"{nm}__job": "jobId"},
           description=desc,
           mock=start_mock)
  api_tool(f"{nm}__status", toolset, status_op.operation_id,
           params={"jobId": "jobId"},
           outputs={"status": "status", "error_code": "error_code",
                    **{k: f"result.{k}" for k in outputs}},
           description=f"Progress of a {nm} job.",
           # Data, so `api_tool` emits a mock tool and the wrapper calls it. The source
           # is replaced below: what a poll has to answer with is a STATUS envelope,
           # which is this feature's own contract and not something `api_tool` knows.
           mock=({"status": "running"} if status_mock is not None else None))
  if status_mock is not None:
    from .toolset_common import mock_tool_name  # noqa: PLC0415
    _tools.register_source_tool(
        mock_tool_name(f"{nm}__status"),
        _remote_status_mock_source(nm, status_mock),
        meta={"mock_for": f"{nm}__status"})
  declared_tool = RemoteTool(
      name=nm, toolset=toolset, operation_id=op_id, outputs=dict(outputs),
      timeout=timeout, pending_mock=mock if isinstance(mock, AfterTurns) else None)
  _REMOTE_TOOLS[nm] = declared_tool
  return declared_tool


def _register_wrapper(
    name: str,
    toolset: OpenApiToolset,
    op: Operation,
    *,
    params: Mapping[str, str],
    outputs: Mapping[str, str],
    description: str,
    mock: Any,
    mock_default: bool,
    derive_outputs: bool,
    param_types: Optional[Mapping[str, str]] = None,
) -> None:
  """Register one generated wrapper, keeping what the build needs to re-render it.

  `derive_outputs` marks a tool whose `outputs` were not declared, so the build fills
  them in from the keys its tasks ask for. The shared `register_wrapper` emits the
  wrapper and its mock tool; the `meta` here is what is OpenAPI-specific.
  """
  register_wrapper(
      name, toolset.symbol(op.operation_id),
      params=params, outputs=outputs, description=description,
      mock=mock, mock_default=mock_default, param_types=param_types,
      meta={"toolset": toolset.name, "operation": op.operation_id,
            "derive_outputs": derive_outputs,
            "toolset_obj": toolset, "operation_obj": op},
  )


def _resolve_outputs(
    tool_name: str, toolset: OpenApiToolset, op: Operation, keys: Sequence[str],
) -> dict[str, str]:
  """`{output_key: response_path}` for the keys tasks asked for, checked against the spec.

  This is why nothing has to be declared twice: the task already says which value it
  wants, and the spec already says where that value lives, so the wrapper is generated
  to bridge exactly those two. A key the spec cannot account for is a build error
  rather than a slot that never fills.
  """
  out: dict[str, str] = {}
  for key in keys:
    path = op.resolve_output(key)
    if path is None:
      known = ", ".join(sorted(set(op.response_paths) | set(op.response_aliases)))
      raise ValueError(
          f"task output key {key!r} is not in the {op.operation_id!r} response of "
          f"toolset {toolset.name!r} — nothing would ever fill that slot. Available: "
          f"{known}"
      )
    out[key] = path
  return out


def prepare_for_build(
    toolsets: Sequence[OpenApiToolset],
    all_configs: Mapping[str, Any],
    mock_default: bool,
) -> None:
  """Generate and re-render the OpenAPI wrappers this app needs. Called by the build.

  Three things can only be settled here, once the App and its flows exist:

  * **Which operations are used.** A task naming an `operationId` gets a wrapper
    generated for it, so a spec is dropped in and referenced rather than restated.
    Only what a flow actually fires is emitted — a large spec does not become a
    hundred tool resources.
  * **Which response fields to lift.** The wrapper emits a literal assignment per key
    a task asked for. It has to be literal: the blessed validator statically parses a
    tool's emitted source for dict keys and ERRORS on a task output key it cannot find
    there, so a wrapper that flattened the response dynamically would make every
    dot-path output a build failure.
  * **The mock default**, which `App(mock_apis=...)` sets. It is compiled in because a
    `variableDeclarations` default does not reach a tool body (verified live: an app
    emitted with `mock_apis=True` still called the real API). A session variable of the
    same name still overrides it.
  """
  by_operation: dict[str, tuple[OpenApiToolset, Operation]] = {}
  for ts in toolsets:
    for op_id, op in ts.operations.items():
      by_operation.setdefault(op_id, (ts, op))

  # Wrappers the author declared with api_tool(). Re-rendered rather than left alone,
  # so a deferred `outputs` and the mock default both land. The registry also holds
  # wrappers for OTHER toolset kinds (MCP), which carry a `render` too — skip them by
  # the `operation` key only an OpenAPI wrapper has (MCP re-renders its own).
  for name, spec in list(_tools._REGISTRY.items()):
    meta = spec.meta or {}
    render = meta.get("render")
    if render is None or meta.get("operation") is None:
      continue
    if meta.get("derive_outputs"):
      ts, op = meta["toolset_obj"], meta["operation_obj"]
      render = {**render,
                "outputs": _resolve_outputs(
                    name, ts, op, task_output_keys(all_configs, name))}
      spec.meta = {**meta, "render": render}
      spec.output_keys = [*render["outputs"], "success", "error", "response"]
    spec.source = wrapper_tool_source(name, mock_default=mock_default, **render)

  # Wrappers nobody declared: a task named an operationId directly.
  declared = {n for n, s in _tools._REGISTRY.items() if (s.meta or {}).get("operation")}
  for cfg in all_configs.values():
    for task in cfg.get("tasks") or []:
      name = task.get("tool")
      if not name or name in declared or name not in by_operation:
        continue
      ts, op = by_operation[name]
      outputs = _resolve_outputs(
          name, ts, op, task_output_keys(all_configs, name))
      _register_wrapper(
          name, ts, op,
          params={_py_name(p): p for p in op.all_params},
          outputs=outputs,
          description=op.summary or f"{op.method} {op.path}",
          mock=ts.mocks.get(op.operation_id),
          mock_default=mock_default,
          derive_outputs=False,
      )
      declared.add(name)
