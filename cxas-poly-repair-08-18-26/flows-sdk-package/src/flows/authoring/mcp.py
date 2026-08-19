"""MCP toolsets — call a Model Context Protocol server from a flow.

CES models an MCP dependency as an `mcpToolset`: its own API resource (`create_toolset`,
a sibling of `create_tool`, in the same `Toolset.toolset_type` oneof as `openApiToolset`).
It lands on disk as a single file:

    toolsets/<name>/<name>.json          # the mcpToolset resource

One file, not two, because an MCP server has no local spec. CES DISCOVERS the tools it
exposes at runtime, by calling the server's list-tools endpoint — so there is nothing to
parse offline, and that is the one way this module differs from its OpenAPI sibling.
`openapi` knows every operation, its parameters, and its response schema, and validates
a task against them; here there is no such document, so the author states the contract
of each tool they fire and `flows` takes them at their word (the CES linter does the same
— it reserves the `<toolset>_` prefix and skips operation-level checks for MCP because
they cannot be resolved offline).

Everything else is the same as `openapi`, on purpose — and both reuse `toolset_common`:

* **an agent cannot call a toolset.** CES exposes each discovered tool only inside the
  sandbox, as `tools.<toolset>_<tool>`, so the callable thing a flow fires is a generated
  `pythonFunction` wrapper that forwards to it. `mcp_tool()` generates that wrapper;
* **authentication** is the shared `apiAuthentication` message CES injects (byte-for-byte
  the same one `openApiToolset` carries) — none of it reaches the wrapper body;
* **mocking** is the same runtime switch, the mock emitted as its own editable tool.

    billing = flows.mcp_toolset(
        "cxp_billing_mcp",
        server_url="https://billing.example.com/mcp/",
        auth=flows.oauth_auth(
            client_id="svc", token_endpoint="https://id.example.com/token",
            secret=SECRET_VERSION),
        headers={"session_id": "$context.variables.session_id"},
    )

    flows.mcp_tool(
        "get_balance", billing, "get_account_balance",
        params=["mdn"],
        outputs={"balance": "data.currentBalance"},
    )

    app = flows.App(root_flow=f, toolsets=[billing], ...)
    f.task("lookup", "get_balance", {"mdn_slot": "mdn"}, "balance_slot",
           out_key="balance")

The wrapper body is ours, so — exactly as in `openapi` — it flattens the declared
`outputs` out of a nested response to the FLAT top-level keys intake maps by, reports a
real `success` flag, and turns a transport error into a result a task's `on_failure` can
act on. See `toolset_common.wrapper_tool_source`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Union

from . import tools as _tools
from .toolset_common import (
    ENV_VAR,
    ToolsetAuth,
    _normalize_params,
    _require,
    _TOOL_NAME_RE,
    _TOOLSET_NAME_RE,
    check_no_overlapping_paths,
    register_wrapper,
    task_output_keys,
    wrapper_tool_source,
)

# An MCP tool name suffixes the generated symbol `tools.<toolset>_<tool>`, so it has to
# be a python identifier tail — a dash or a dot is a NameError in the sandbox on the
# first call, and there is no spec to catch it earlier. A server tool named with a dash
# would need a CES `toolOverride.nameOverride` to alias it, which this module does not
# emit yet.
_MCP_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

# CES resolves a custom header's value as a session variable, and ONLY in this shape.
# The proto is explicit: "The values must be in the format `$context.variables.<name>`".
# A literal value here would be sent as the literal string, not the caller's session
# value — the same silent-wrong-answer failure `_secret` guards against, so it is caught
# at authoring time rather than in a live call.
_CONTEXT_VAR_RE = re.compile(r"^\$context\.variables\.[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class McpToolset:
  """An MCP server declared as one CES `mcpToolset` resource.

  Emitted as `toolsets/<name>/<name>.json` — one file, since an MCP server has no local
  spec. Nothing calls it directly: pair it with `mcp_tool()` for each tool a flow fires.
  """

  name: str
  server_url: str
  description: str = ""
  auth: Optional[ToolsetAuth] = None
  headers: Mapping[str, str] = field(default_factory=dict)
  env_scoped: bool = False
  # `{tool_name: mock}` for tools whose wrapper wants a mock but whose `mcp_tool()` call
  # did not pass one — a convenience so every mock can live on the toolset. Unchecked:
  # there is no spec to verify a tool name against.
  mocks: Mapping[str, Any] = field(default_factory=dict)

  def symbol(self, tool_name: str) -> str:
    """The in-sandbox callable for a discovered tool: `<toolset>_<tool>`.

    The tool name is used VERBATIM — CES derives the symbol from the name the server
    reports, so rewriting it here produces a name that does not exist.
    """
    return f"{self.name}_{tool_name}"

  def resource_body(self) -> dict[str, Any]:
    """The `toolsets/<name>/<name>.json` resource, minus its `name` (the UUID).

    The emitter mints and prepends that, as it does for every other resource, so UUID
    uniqueness stays checkable in one place. `toolOverrides` is left absent, which tells
    CES to discover every tool the server exposes.
    """
    block: dict[str, Any] = {
        "serverAddress": ENV_VAR if self.env_scoped else self.server_url}
    if self.auth is not None:
      auth = self.auth.to_dict(env_scoped=self.env_scoped)
      if auth:
        block["apiAuthentication"] = auth
    if self.headers:
      # Structural, not deployment-varying: a header value is a `$context.variables.*`
      # reference resolved per session, so it stays on the resource even when
      # env_scoped moves the URL and the secrets out.
      block["customHeaders"] = dict(self.headers)
    doc: dict[str, Any] = {"displayName": self.name, "mcpToolset": block}
    if self.description:
      doc["description"] = self.description
    return doc

  def payload(self) -> dict[str, Any]:
    """What the scaffold request carries. No `spec`: an MCP toolset has no schema file,
    and the emitter writes only the resource when `spec` is absent."""
    return {"name": self.name, "resource": self.resource_body()}

  def environment_entry(self) -> Optional[dict[str, Any]]:
    """This toolset's `environment.json` entry, or None when it needs none.

    Only an `env_scoped` toolset produces one — the server address and the secret
    versions, the values that differ per deployment. Everything else stays inlined so
    the emitted app dir is self-contained.
    """
    if not self.env_scoped:
      return None
    block: dict[str, Any] = {}
    if self.server_url:
      block["serverAddress"] = self.server_url
    if self.auth is not None:
      auth = self.auth.environment_auth()
      if auth:
        block["apiAuthentication"] = auth
    return {"mcpToolset": block} if block else None


def mcp_toolset(
    name: str,
    *,
    server_url: str,
    description: str = "",
    auth: Optional[ToolsetAuth] = None,
    headers: Optional[Mapping[str, str]] = None,
    env_scoped: bool = False,
    mocks: Optional[Mapping[str, Any]] = None,
) -> McpToolset:
  """Declare an MCP server as a CES MCP toolset.

  Args:
    name: The toolset's display name. Must be a python identifier, because it prefixes
      the generated in-sandbox symbol `tools.<name>_<tool>` — a dash here is a
      `NameError` on the first live call, not a build error.
    server_url: The MCP server address, e.g. `https://example.com/mcp/`. CES speaks
      Streamable HTTP only; a server built with the MCP SDK expects the `/mcp/` suffix.
    description: What the server is for. Shown on the resource; the model never sees it
      (it sees each `mcp_tool`'s description instead).
    auth: How CES authenticates (see `flows.oauth_auth` and friends). None emits no
      `apiAuthentication` block. The same builders OpenAPI uses — the wire message is
      shared.
    headers: `{header_name: value}` sent on every request to the server. Each value
      must be a `$context.variables.<name>` reference, which CES resolves from the
      session — the format the platform requires, so a literal is refused here.
    env_scoped: Move the server URL and the secret references into `environment.json`
      and leave `$env_var` markers behind, the layout a pulled CES app uses. Off by
      default: inlining keeps the emitted dir self-contained and pushable as-is.
    mocks: `{tool_name: mock}` used when `mock_apis` is on, for a tool whose
      `mcp_tool()` did not carry its own `mock=`. Each is JSON data or a
      `fn(**params) -> dict` returning what the REAL tool would return.

  Returns:
    An `McpToolset` to pass to `App(toolsets=[...])`.
  """
  nm = _require(name, "mcp_toolset(): name")
  url = _require(server_url, f"mcp_toolset({nm!r}): server_url")
  if not _TOOLSET_NAME_RE.match(nm):
    raise ValueError(
        f"mcp_toolset(): name {nm!r} must be a python identifier (letters, digits and "
        "'_', not starting with a digit) — it prefixes the generated symbol "
        f"tools.{nm}_<tool>, which the sandbox resolves at call time"
    )
  if not url.startswith(("https://", "http://")):
    raise ValueError(
        f"mcp_toolset({nm!r}): server_url must be an absolute URL, got {url!r}")
  if auth is not None and not isinstance(auth, ToolsetAuth):
    raise ValueError(
        f"mcp_toolset({nm!r}): auth must be built with flows.api_key_auth(), "
        f"flows.oauth_auth(), flows.bearer_auth() or flows.service_agent_auth(), got "
        f"{type(auth).__name__}"
    )
  clean_headers: dict[str, str] = {}
  for key, value in (headers or {}).items():
    text = "" if value is None else str(value)
    if not _CONTEXT_VAR_RE.match(text):
      raise ValueError(
          f"mcp_toolset({nm!r}): header {key!r} value {text!r} must be a "
          "'$context.variables.<name>' reference — CES resolves a custom header from a "
          "session variable and only in that shape, so a literal would be sent verbatim"
      )
    clean_headers[str(key)] = text
  return McpToolset(
      name=nm,
      server_url=url,
      description=str(description or "").strip(),
      auth=auth,
      headers=clean_headers,
      env_scoped=env_scoped,
      mocks=dict(mocks or {}),
  )


@dataclass(frozen=True)
class McpTool:
  """A generated wrapper around one tool on an MCP toolset.

  Its `name` is what a task's `tool` names; `str(...)` gives that name too, so it can be
  passed to `flow.task(...)` directly.
  """

  name: str
  toolset: McpToolset
  tool_name: str
  params: Mapping[str, str]
  outputs: Mapping[str, str]
  has_mock: bool = False

  def __str__(self) -> str:
    return self.name


def mcp_tool(
    name: str,
    toolset: McpToolset,
    tool_name: str,
    *,
    params: Union[Sequence[str], Mapping[str, str], None] = None,
    outputs: Optional[Mapping[str, str]] = None,
    description: str = "",
    mock: Any = None,
) -> McpTool:
  """Make one tool on an MCP toolset callable from a flow.

  This is the piece that turns a toolset into something an agent can use, and — unlike
  its OpenAPI cousin `api_tool` — it is REQUIRED, not an escape hatch. There is no spec
  to generate a wrapper from, so a flow reaches an MCP tool only through one of these.

  Args:
    name: The tool's name — what `task(tool=...)` refers to and what the model calls.
    toolset: The `mcp_toolset(...)` that owns the tool.
    tool_name: The tool's name as the MCP server reports it. Used verbatim to build the
      sandbox symbol `tools.<toolset>_<tool_name>`; it cannot be checked offline (the
      server is not consulted at build), so a typo is a `NameError` on the first live
      call rather than a build error.
    params: The tool's arguments to expose. A list uses the argument names as-is; a
      `{arg: wire}` dict renames them, and a dotted wire path nests the value (MCP
      arguments are a JSON object). Defaults to none — an MCP tool taking no arguments
      is common, and there is no spec to enumerate them from, so declare what the tool
      accepts.
    outputs: `{output_key: dot.path}` read out of the tool's result and flattened to the
      top level, because intake maps a task's `outputs` by FLAT key. No response schema
      exists to check a path against, so any path is accepted. Omitted, the build fills
      it from the keys the tasks firing this tool ask for, each taken as a literal path.
    description: What the tool does, for the model. Defaults to a generic line naming
      the tool.
    mock: A stand-in answer used when the `mock_apis` flag is on — either JSON data or a
      `fn(**params) -> dict`. It returns what the REAL tool would return, so the same
      extraction and `success` rule run over it and a mocked run proves the mapping. Set
      the flag with `App(mock_apis=True)` or per session. Falls back to the toolset's
      `mocks[tool_name]` when not given here.

  Returns:
    An `McpTool` whose `name` a task can fire.
  """
  if not isinstance(toolset, McpToolset):
    raise ValueError(
        "mcp_tool(): toolset must be a flows.mcp_toolset(...), got "
        f"{type(toolset).__name__}"
    )
  nm = _require(name, "mcp_tool(): name")
  if not _TOOL_NAME_RE.match(nm):
    raise ValueError(
        f"mcp_tool(): name {nm!r} must be a python identifier — it is emitted verbatim "
        "as the generated wrapper's `def` entrypoint, which CES calls by name"
    )
  tl = _require(tool_name, f"mcp_tool({nm!r}): tool_name")
  if not _MCP_TOOL_NAME_RE.match(tl):
    raise ValueError(
        f"mcp_tool({nm!r}): tool_name {tl!r} must contain only letters, digits and "
        f"'_' — it becomes the sandbox symbol tools.{toolset.symbol(tl)}, and a dash "
        "or a dot there is a NameError at call time (aliasing a dashed server tool "
        "needs a CES toolOverride, which flows does not emit yet)"
    )
  resolved = _normalize_params(params)
  out_map = {str(k): str(v) for k, v in (outputs or {}).items()}
  for key in out_map:
    if key in ("success", "error", "response"):
      raise ValueError(
          f"mcp_tool({nm!r}): output key {key!r} is reserved — the wrapper sets it "
          "itself ('success' is what intake reads to decide the call worked)"
      )
  check_no_overlapping_paths(f"mcp_tool({nm!r})", resolved)
  desc = str(description or f"Call the {tl} tool on the {toolset.name} MCP server.").strip()
  effective_mock = mock if mock is not None else toolset.mocks.get(tl)
  _register_wrapper(
      nm, toolset, tl, params=resolved, outputs=out_map, description=desc,
      mock=effective_mock, mock_default=False, derive_outputs=outputs is None)
  return McpTool(
      name=nm, toolset=toolset, tool_name=tl,
      params=resolved, outputs=out_map, has_mock=effective_mock is not None)


def _register_wrapper(
    name: str,
    toolset: McpToolset,
    tool_name: str,
    *,
    params: Mapping[str, str],
    outputs: Mapping[str, str],
    description: str,
    mock: Any,
    mock_default: bool,
    derive_outputs: bool,
) -> None:
  """Register one generated wrapper, keeping what the build needs to re-render it.

  `derive_outputs` marks a tool whose `outputs` were not declared, so the build fills
  them in from the keys its tasks ask for. The shared `register_wrapper` emits the
  wrapper and its mock tool; the `meta` here (the `mcp_tool`/`mcp_toolset` keys) is what
  lets the build and the validation guards tell the two toolset kinds apart.
  """
  register_wrapper(
      name, toolset.symbol(tool_name),
      params=params, outputs=outputs, description=description,
      mock=mock, mock_default=mock_default,
      meta={"mcp_toolset": toolset.name, "mcp_tool": tool_name,
            "derive_outputs": derive_outputs, "toolset_obj": toolset},
  )


def prepare_for_build(
    toolsets: Sequence[McpToolset],
    all_configs: Mapping[str, Any],
    mock_default: bool,
) -> None:
  """Re-render the MCP wrappers this app needs. Called by the build.

  Two things can only be settled here, once the App and its flows exist:

  * **Which response fields to lift.** The wrapper emits a literal assignment per key a
    task asked for, and it has to be literal: the blessed validator statically parses a
    tool's emitted source for dict keys and ERRORS on a task output key it cannot find
    there. Unlike OpenAPI there is no schema to resolve a key against, so each key a
    task names is taken as a literal dot-path into the tool's result.
  * **The mock default**, which `App(mock_apis=...)` sets. It is compiled into the body
    because a `variableDeclarations` default does not reach a tool body (verified live
    for OpenAPI). A session variable of the same name still overrides it.

  Unlike OpenAPI there is no "a task named the tool directly" path: with no spec, an MCP
  tool is reachable only through a declared `mcp_tool()`, so this re-renders exactly the
  wrappers already registered.
  """
  known = {ts.name for ts in toolsets}
  for name, spec in list(_tools._REGISTRY.items()):
    meta = spec.meta or {}
    render = meta.get("render")
    if render is None or meta.get("mcp_tool") is None:
      continue
    if meta.get("mcp_toolset") not in known:
      continue  # a wrapper for a toolset this app does not declare — leave it be
    if meta.get("derive_outputs"):
      outputs = {k: k for k in task_output_keys(all_configs, name)}
      render = {**render, "outputs": outputs}
      spec.meta = {**meta, "render": render}
      spec.output_keys = [*outputs, "success", "error", "response"]
    spec.source = wrapper_tool_source(name, mock_default=mock_default, **render)
