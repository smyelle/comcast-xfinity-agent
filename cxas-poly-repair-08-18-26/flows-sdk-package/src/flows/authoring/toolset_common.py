"""Shared foundation for CES toolsets — auth, secrets, and the wrapper/mock machinery.

A CES *toolset* is a resource kind of its own (`create_toolset`, a sibling of
`create_tool`), and there is more than one flavour of it: `openApiToolset` calls a REST
API, `mcpToolset` calls an MCP server. They differ in what the resource declares — a
spec and response schema for OpenAPI, a server address and dynamically-discovered tools
for MCP — but everything AROUND that is the same:

* **an agent cannot call a toolset.** Its operations exist only as sandbox symbols
  `tools.<toolset>_<name>`, so the callable thing a flow fires is a generated
  `pythonFunction` wrapper that forwards to one. That wrapper is identical in shape
  whichever toolset it fronts;
* **authentication** is the one `apiAuthentication` message either resource carries —
  CES performs the exchange and injects the credential, so none of it reaches the
  wrapper body;
* **mocking** is a runtime switch (`mock_apis`), the mock emitted alongside the live
  call as its own editable tool.

This module holds exactly that shared middle. `flows.authoring.openapi` and
`flows.authoring.mcp` each add the part that is theirs — the spec parser, the resource
body — and reuse the auth builders, `wrapper_tool_source`, and the mock machinery from
here so a single fix lands for both.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union

from . import tools as _tools

# A secret is referenced by VERSION, never inlined. CES resolves the reference with the
# app's service agent, so the emitted app dir stays safe to commit.
_SECRET_VERSION_RE = re.compile(
    r"^projects/[^/]+/secrets/[^/]+/versions/[^/]+$")

# Where an API key rides. CES's `requestLocation` enum.
KEY_LOCATIONS = ("HEADER", "QUERY", "COOKIE")

# The marker CES reads as "this value comes from environment.json". Only emitted for an
# `env_scoped=True` toolset — see each toolset's `environment_entry`.
ENV_VAR = "$env_var"

# A toolset name prefixes the generated symbol `tools.<toolset>_<name>`, so it has to be
# a python identifier — a dash here is a NameError in the sandbox, at runtime, on the
# first call.
_TOOLSET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A generated wrapper's name is emitted verbatim as its `def <name>(...)` entrypoint —
# CES calls the body function whose name matches the tool — so it has to be a python
# identifier. A dash would be a SyntaxError in the emitted body, at the first call.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Mocking is a RUNTIME switch, not a build-time one, so a single deployed app can be
# flipped between mocked and live without re-emitting. `App(mock_apis=True)` only sets
# this variable's default.
MOCK_FLAG_VAR = "mock_apis"
# `mock_<tool>` holds a payload that pins ONE call for one session — the convention the
# reference app uses by hand, kept because it is what evals want.
MOCK_VAR_PREFIX = "mock_"


def _require(value: Any, what: str) -> str:
  """A required string, rejected when blank."""
  text = "" if value is None else str(value).strip()
  if not text:
    raise ValueError(f"{what} is required and cannot be empty")
  return text


def _secret(value: Any, what: str) -> str:
  """A Secret Manager VERSION reference, rejected when it looks like a raw secret.

  Checked here because the failure it prevents is committing a live credential, which
  no later stage can undo.
  """
  text = _require(value, what)
  if not _SECRET_VERSION_RE.match(text):
    raise ValueError(
        f"{what} must be a Secret Manager version reference "
        f"('projects/<project>/secrets/<secret>/versions/<version>'), got {text!r}. "
        "CES resolves the reference itself — never put the secret value in the app"
    )
  return text


# ---------------------------------------------------------------------------
# Authentication. CES performs the exchange and injects the credential into every
# operation in the toolset, so none of this reaches the wrapper body. The
# `apiAuthentication` message is shared byte-for-byte between `openApiToolset` and
# `mcpToolset` (both reference the same CES `ApiAuthentication`), which is why this
# lives here rather than beside either one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolsetAuth:
  """How CES authenticates to the API behind a toolset.

  Built by `api_key_auth` / `oauth_auth` / `bearer_auth` / `service_agent_auth`; the
  `kind` selects which `apiAuthentication` sub-message is emitted.
  """

  kind: str
  key_name: str = ""
  key_location: str = "HEADER"
  secret_version: str = ""
  client_id: str = ""
  token_endpoint: str = ""
  scopes: tuple[str, ...] = ()

  def to_dict(self, *, env_scoped: bool = False) -> dict[str, Any]:
    """The `apiAuthentication` block for the toolset resource.

    With `env_scoped`, the environment-varying values become `$env_var` and their real
    values move to `environment.json` (see `environment_auth`). The structural fields
    — where the key rides, which grant, which scopes — stay here either way, because
    they describe the API rather than the deployment.
    """
    secret = ENV_VAR if env_scoped else self.secret_version
    if self.kind == "api_key":
      return {"apiKeyConfig": {
          "keyName": self.key_name,
          "apiKeySecretVersion": secret,
          "requestLocation": self.key_location,
      }}
    if self.kind == "oauth":
      return {"oauthConfig": {
          "oauthGrantType": "CLIENT_CREDENTIAL",
          "clientId": self.client_id,
          "clientSecretVersion": secret,
          "tokenEndpoint": ENV_VAR if env_scoped else self.token_endpoint,
          "scopes": list(self.scopes),
      }}
    if self.kind == "service_agent":
      return {"serviceAgentIdTokenAuthConfig": {}}
    return {}

  def environment_auth(self) -> dict[str, Any]:
    """The `apiAuthentication` half that belongs in `environment.json`.

    Only the values that differ per deployment — the secret versions and the OAuth
    token endpoint. Empty for auth that has none (`service_agent`, `none`).
    """
    if self.kind == "api_key":
      return {"apiKeyConfig": {"apiKeySecretVersion": self.secret_version}}
    if self.kind == "oauth":
      return {"oauthConfig": {
          "clientSecretVersion": self.secret_version,
          "tokenEndpoint": self.token_endpoint,
      }}
    return {}


def api_key_auth(
    key_name: str, *, secret: str, location: str = "HEADER") -> ToolsetAuth:
  """An API key CES injects into every call in the toolset.

  Args:
    key_name: The header/query/cookie name carrying the key (e.g. `Authorization`).
    secret: Secret Manager VERSION reference holding the key's value.
    location: Where the key rides — `HEADER` (default), `QUERY` or `COOKIE`.
  """
  loc = _require(location, "api_key_auth(): location").upper()
  if loc not in KEY_LOCATIONS:
    raise ValueError(
        f"api_key_auth(): location must be one of {KEY_LOCATIONS}, got {location!r}")
  return ToolsetAuth(
      kind="api_key",
      key_name=_require(key_name, "api_key_auth(): key_name"),
      key_location=loc,
      secret_version=_secret(secret, "api_key_auth(): secret"),
  )


def oauth_auth(
    *, client_id: str, token_endpoint: str, secret: str,
    scopes: Sequence[str] = ()) -> ToolsetAuth:
  """OAuth 2.0 client-credentials. CES does the token exchange AND the refresh.

  That is the reason to prefer this over hand-rolling a token call as a tool: the
  bearer never enters the sandbox, and nothing in the flow has to model expiry.
  """
  endpoint = _require(token_endpoint, "oauth_auth(): token_endpoint")
  if not endpoint.startswith("https://"):
    raise ValueError(
        f"oauth_auth(): token_endpoint must be an absolute https:// URL, got {endpoint!r}")
  return ToolsetAuth(
      kind="oauth",
      client_id=_require(client_id, "oauth_auth(): client_id"),
      token_endpoint=endpoint,
      secret_version=_secret(secret, "oauth_auth(): secret"),
      scopes=tuple(scopes),
  )


def bearer_auth(*, secret: str) -> ToolsetAuth:
  """A static bearer token from Secret Manager, sent as the `Authorization` header.

  Emitted as an `apiKeyConfig` (keyName `Authorization`, in the HEADER), not a
  `bearerTokenConfig`: the CES `BearerTokenConfig` message has a single `token` field
  that must be a `$context.variables.<name>` session reference — it cannot carry a Secret
  Manager version, whereas `apiKeyConfig` can. Store the exact header value the API wants
  in the secret (include a `Bearer ` prefix if it requires one — CES sends it verbatim).
  """
  return ToolsetAuth(
      kind="api_key", key_name="Authorization", key_location="HEADER",
      secret_version=_secret(secret, "bearer_auth(): secret"))


def service_agent_auth() -> ToolsetAuth:
  """A Google ID token for the app's own service agent — the zero-secret option.

  Right for an API on Cloud Run / API Gateway that accepts the service agent's
  identity, which needs no Secret Manager wiring at all.
  """
  return ToolsetAuth(kind="service_agent")


# ---------------------------------------------------------------------------
# The wrapper tool — what an agent can actually call — and the mock it can carry.
# Both are toolset-agnostic: they call one sandbox symbol `tools.<symbol>(request)`,
# whether that symbol is an OpenAPI operation or an MCP tool.
# ---------------------------------------------------------------------------


def _py_name(text: str) -> str:
  """A safe python parameter name derived from a wire name."""
  s = re.sub(r"[^A-Za-z0-9_]", "_", str(text)).strip("_")
  if not s:
    return "arg"
  return s if not s[0].isdigit() else f"p_{s}"


def _normalize_params(
    params: Union[Sequence[str], Mapping[str, str], None]
) -> dict[str, str]:
  """`{python_arg: wire_path}`, accepting the list or dict form.

  A list means "the wire names are already fine as argument names"; the dict form is
  `{arg: wire}`, the same source→target direction as `task(inputs={slot: param})`.
  """
  if not params:
    return {}
  if isinstance(params, Mapping):
    return {_py_name(k): str(v) for k, v in params.items()}
  return {_py_name(p): str(p) for p in params}


def mock_tool_name(tool_name: str) -> str:
  """The tool a mock is emitted as. Never scoped onto an agent — the wrapper calls it."""
  return f"{tool_name}_mock"


def check_mock_callable(where: str, mock: Any) -> None:
  """Refuse a callable a mock TOOL cannot be generated from.

  A callable mock is not called at build time and it is not imported at run time: its
  source is read with `inspect.getsource` and INLINED into the emitted tool, which the
  generated entrypoint then calls by NAME. Three shapes break that, all of them at a
  distance from the `mock=` that caused them:

    * a **lambda**. Its `__name__` is `<lambda>`, which is not an identifier, so the
      emitted tool carries `return _envelope(<lambda>())` and the whole tool fails to
      load with a `SyntaxError` — reported by CES as a tool that does not exist.
    * a **class**, or any non-function callable. `MyMock` renders as its class source
      and is emitted as `MyMock()`, which CONSTRUCTS it instead of calling it: the tool
      answers an instance nothing can serialize, rather than the payload `__call__`
      would have returned.
    * a **closure over a free variable**. `getsource` captures the function and nothing
      of the scope around it, so every enclosing name is unbound in the sandbox and the
      first mocked call raises `NameError` — after a deploy, in a tool body, naming a
      variable that reads as though it should obviously be there.

  Caught here so the message names the `mock=` that has to change. The alternative in
  every case is a failure that surfaces as "the mock answered nothing".
  """
  import inspect  # noqa: PLC0415

  name = getattr(mock, "__name__", "")
  if isinstance(mock, type):
    raise ValueError(
        f"{where}: the mock {name or type(mock).__name__!r} is a CLASS. A mock is "
        "emitted by inlining its source and calling it by name, so this would be "
        "constructed rather than called and the tool would answer an instance. Pass a "
        "module level `def` (or an instance's bound method), or plain data")
  if name == "<lambda>":
    raise ValueError(
        f"{where}: the mock is a lambda. A mock is emitted as a tool by inlining its "
        "source and calling it by NAME, and `<lambda>` is not one — the generated tool "
        "would not parse. Give it a `def` in a real module, or pass the answer as "
        "plain data")
  if not (inspect.isfunction(mock) or inspect.ismethod(mock)):
    raise ValueError(
        f"{where}: the mock is a {type(mock).__name__}, which has no source to inline. "
        "A mock is emitted as a tool of its own, so it must be a `def` in a real "
        "module (a builtin, a partial or a callable instance cannot be), or plain data")
  free = tuple(getattr(getattr(mock, "__code__", None), "co_freevars", ()) or ())
  if free:
    raise ValueError(
        f"{where}: the mock {name!r} closes over "
        f"{', '.join(repr(f) for f in free)} — only its own source is inlined into the "
        "emitted tool, so the enclosing scope is gone and the first mocked call would "
        f"raise NameError on {free[0]!r}. Move it to module level and inline what it "
        "reads, or read it from the session through `context`")


def _mock_convention(tool_name: str, mock: Any, params: Mapping[str, str]) -> str:
  """How a callable mock wants to be called: `"named"` or `"request"`.

  A mock written against the operation's own parameters (`def fake(zipcode)`) reads far
  better than one picking them out of a dict, so both are supported and told apart by
  the parameter NAMES. Anything else is rejected here rather than in the sandbox.
  """
  import inspect

  try:
    sig = inspect.signature(mock)
  except (TypeError, ValueError):  # a builtin or C callable — nothing to inspect
    return "request"
  taken = [p for p in sig.parameters.values()
           if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
  if not taken:
    return "named"  # a constant mock; call it with nothing
  if all(p.name in params for p in taken):
    return "named"
  unknown = [p.name for p in taken if p.name not in params]
  if len(taken) == 1:
    return "request"  # one parameter, not an operation name -> the request dict
  raise ValueError(
      f"{tool_name!r}: the mock takes {', '.join(repr(u) for u in unknown)}, "
      f"which {'is' if len(unknown) == 1 else 'are'} not "
      f"{'a parameter' if len(unknown) == 1 else 'parameters'} of the operation "
      f"({', '.join(sorted(params)) or 'none'}). A mock takes either the operation's "
      "own parameters or a single argument receiving the whole request dict"
  )


def _accepted_params(mock: Any, params: Mapping[str, str]) -> list[str]:
  """The operation parameters a `"named"` mock actually takes, in declaration order.

  A mock is free to care about one parameter, or none — `**kwargs` takes them all.
  """
  import inspect

  try:
    sig = inspect.signature(mock)
  except (TypeError, ValueError):
    return list(params)
  if any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
    return list(params)
  return [a for a in params if a in sig.parameters]


def _defaulted_params(mock: Any, wanted: Sequence[str]) -> list[str]:
  """Of `wanted`, the ones the mock declares a DEFAULT for.

  These are the only arguments it is safe to leave out of the generated call — see the
  `_args` block in `mock_tool_source`. A parameter with no default has to be passed
  whatever its value, or the mocked call is a `TypeError` in the sandbox.
  """
  import inspect  # noqa: PLC0415

  try:
    sig = inspect.signature(mock)
  except (TypeError, ValueError):
    return []
  empty = inspect.Parameter.empty
  return [a for a in wanted
          if a in sig.parameters and sig.parameters[a].default is not empty]


def mock_tool_source(
    name: str, params: Mapping[str, str], mock: Any, convention: str) -> str:
  """Source for the standalone tool a mock is emitted as.

  Emitted as a tool of its own rather than inlined into the wrapper so it can be READ
  AND EDITED in the CES console — changing what a mocked call returns without a
  rebuild, which is the whole point of a mock during a demo. It is never scoped onto
  an agent, so the model cannot call it; only the wrapper's body can, the way the
  reference app's `escalate_transfer` calls `get_department_ext`.

  Its parameters are the operation's, flat, whichever way the author wrote the mock —
  a tool-to-tool call passes one dict of the callee's named arguments, and a
  dict-typed parameter has no good schema.
  """
  args = ", ".join(f"{a}: str = ''" for a in params)
  pre: list[str] = []  # statements the entrypoint runs before its return
  lines = [f'"""Mock for the {name} operation (generated). Edit to change the answer."""',
           _tools._HEADER.rstrip(), ""]
  if callable(mock):
    check_mock_callable(repr(name), mock)
    try:
      body = _tools.render_callable(mock).rstrip()
    except (OSError, TypeError) as exc:
      # The mock is INLINED into the emitted tool, so its source has to be readable.
      # A builtin, a C callable or a lambda defined in exec'd code has none, and
      # `inspect.getsource` raises something opaque about it.
      raise ValueError(
          f"{name!r}: the mock "
          f"{getattr(mock, '__name__', type(mock).__name__)!r} has no source that can "
          f"be read ({type(exc).__name__}: {exc}), and a mock is emitted as a tool by "
          "inlining it. Use a def in a real module, or pass the answer as plain data"
      ) from exc
    lines += ["", body, ""]
    inner = getattr(mock, "__name__", "")
    if convention == "named":
      # Only what it actually accepts. A mock caring about one of three parameters —
      # or about none at all — is perfectly reasonable, and passing the rest would be
      # a TypeError in the sandbox on the first mocked call.
      wanted = _accepted_params(mock, params)
      # An argument the caller did not supply is OMITTED, so the mock's own default
      # stands. Every parameter of this generated tool defaults to `''` (it has no
      # schema to type them from), so passing all of them unconditionally handed the
      # author's `def fake(zipcode="94040")` an empty string and silently overrode the
      # default they wrote — a mock answering for the wrong input, with nothing in the
      # transcript to say so. Same rule the wrapper applies to the live request, where
      # an unset optional is left out rather than sent empty.
      #
      # Only the DEFAULTED ones: a parameter the mock requires must be passed whatever
      # its value, or the mocked call is a TypeError in the sandbox.
      optional = _defaulted_params(mock, wanted)
      if optional:
        required = [a for a in wanted if a not in optional]
        pre += ["  _args = {" + ", ".join(f"{a!r}: {a}" for a in required) + "}"]
        for arg in optional:
          pre += [f"  if {arg} not in (None, ''):",
                  f"    _args[{arg!r}] = {arg}"]
        call = f"{inner}(**_args)"
      else:
        call = f"{inner}({', '.join(f'{a}={a}' for a in wanted)})"
    else:
      built = ", ".join(f"{wire!r}: {arg}" for arg, wire in params.items())
      call = f"{inner}({{{built}}})"
  else:
    try:
      json.dumps(mock)
    except TypeError as exc:
      raise ValueError(
          f"{name!r}: mock must be JSON-serializable data or a callable, "
          f"got {type(mock).__name__} ({exc})"
      ) from exc
    call = repr(mock)
  lines += [
      "",
      f"def {name}({args}) -> Any:",
      '  """The stand-in answer, in the shape the REAL API would return.',
      "",
      "  The wrapper runs its ordinary extraction over this, so a mocked run exercises",
      "  the same response mapping the live call depends on.",
      '  """',
      *pre,
      f"  return {call}",
      "",
  ]
  return "\n".join(lines)


# Emitted into a generated wrapper that declares an `integer` parameter. Its own
# docstring ships in the emitted source, because that source is what anyone debugging a
# rejected call reads.
# Emitted into a generated wrapper that has BOTH a declared mock and a non-string
# parameter. Its docstring ships in the source for the same reason `_as_int`'s does.
_MOCK_ARG_LINES: tuple[str, ...] = (
    "",
    "",
    "def _mock_arg(value: Any) -> str:",
    '  """A coerced argument, back as the `str` the mock TOOL declares.',
    "",
    "  A mock is a tool of its own so it can be read and edited in the console, and it",
    "  has no schema to type its parameters from, so it takes them all as `str`. CES",
    "  type-checks a tool-to-tool call against that signature before the body runs, and",
    "  by here a declared integer/number/boolean has already been coerced for the",
    "  request -- so the mocked call is refused with `Expected String, received",
    "  kotlin.Double`, and the wrapper reports it as a missing output.",
    "",
    "  A dict or a list goes back as JSON, not as `str(value)`. `str` on a container is",
    "  its REPR -- single-quoted keys, `True`, `None` -- which is not JSON, so a mock",
    "  that does the obvious thing with a structured parameter and calls `json.loads`",
    "  raises inside the sandbox and the wrapper reports the whole call as a missing",
    '  output. `json.dumps` is what the live request would have carried.',
    '  """',
    "  import json as _json",
    "  if value is None:",
    "    return ''",
    "  if isinstance(value, bool):",
    "    return 'true' if value else 'false'",
    "  if isinstance(value, (dict, list, tuple)):",
    "    try:",
    "      return _json.dumps(value)",
    "    except (TypeError, ValueError):",
    "      return str(value)",
    "  if isinstance(value, float) and value.is_integer():",
    "    return str(int(value))",
    "  return str(value)",
)


_AS_INT_LINES: tuple[str, ...] = (
    "",
    "",
    "def _as_int(value: Any) -> Any:",
    '  """Coerce to int, including the integral-float STRING a slot can hold.',
    "",
    "  A numeric default crosses CES as a protobuf Struct, where every number is a",
    '  double, so an authored `240` can reach here as 240.0 or as the text "240.0".',
    '  `int("240.0")` raises, and the uncoerced string is exactly what an API declaring',
    "  the parameter an integer then rejects. A genuinely fractional value is left",
    "  alone rather than silently truncated -- that is the caller's error to see.",
    '  """',
    "  try:",
    "    return int(value)",
    "  except (TypeError, ValueError):",
    "    pass",
    "  try:",
    "    number = float(value)",
    "  except (TypeError, ValueError):",
    "    return value",
    "  return int(number) if number.is_integer() else value",
)


def wrapper_tool_source(
    tool_name: str,
    symbol: str,
    *,
    description: str,
    params: Mapping[str, str],
    outputs: Mapping[str, str],
    mock: Any = None,
    mock_default: bool = False,
    param_types: Optional[Mapping[str, str]] = None,
) -> str:
  """Source for the generated tool that calls one toolset operation.

  Runs in the CES sandbox (stdlib only, no `from __future__` — see `tools._HEADER`).
  It does three things the raw operation cannot: assembles the request (nesting
  dot-path body fields), flattens the declared `outputs` out of a nested response to
  the FLAT top-level keys intake maps by, and reports a `success` flag intake can read.

  A declared `mock` is emitted alongside the live call rather than instead of it, and
  chosen at RUNTIME. That is what lets one deployed app switch between mocked and live
  without a rebuild, and it is why the mock returns a raw API response rather than the
  tool's own return shape: the unwrap, the dot-path extraction and the `success` rule
  below are the same code either way, so a passing mocked run has actually exercised
  the mapping the live call depends on.
  """
  args = ", ".join(f"{a}: str = ''" for a in params) or ""
  # Every generated wrapper takes its arguments as STRINGS, because that is what a slot
  # holds. For an API whose spec says otherwise that is a wire-type error the caller
  # never sees coming: the platform rejects the call with "Expected Long, received
  # String" and the tool reports a failure nobody can explain from the flow. Declared
  # types are coerced here, once, before the request is assembled.
  coerce_lines: list[str] = []
  int_helper: list[str] = []
  for arg, kind in (param_types or {}).items():
    if arg not in params or kind == "string":
      continue
    if kind == "integer":
      # Any input type, not just str. A slot default round-trips through JSON, so an
      # authored `240` arrives as the FLOAT 240.0 — which a spec saying `integer`
      # rejects just as firmly as it rejects the string "240", and for a reason no
      # amount of reading the flow would reveal. Via a helper because a bare `int(...)`
      # is not enough for the STRING that same round-trip can produce.
      int_helper = list(_AS_INT_LINES)
      coerce_lines += [
          f"  if {arg} not in (None, ''):",
          f"    {arg} = _as_int({arg})",
      ]
    elif kind == "number":
      coerce_lines += [
          f"  if {arg} not in (None, ''):",
          "    try:",
          f"      {arg} = float({arg})",
          "    except (TypeError, ValueError):",
          "      pass",
      ]
    elif kind == "boolean":
      coerce_lines += [
          f"  if isinstance({arg}, str) and {arg}.strip():",
          f"    {arg} = {arg}.strip().lower() in ('true', '1', 'yes')",
      ]
  # The mock is a TOOL now, not an inlined function — see `mock_tool_source`.
  #
  # Its arguments are stringified on the way in, and that is not tidiness. A mock tool
  # declares every parameter `str` (it has no schema to type them from), CES type-checks
  # a tool-to-tool `function_call` against the callee's signature BEFORE its body runs,
  # and by this point in the wrapper a declared `integer`/`number`/`boolean` parameter
  # has already been coerced away from `str` for the request. Driven live:
  #
  #   Invalid value for parameter `duration_seconds` in the tool call in Python tool
  #   build_report_mock: Expected `String`, received `kotlin.Double` (240.0).
  #
  # which the wrapper reports as `success: false, missing from response: <job slot>` —
  # a mocked remote job that never started, wearing a missing-output's name. The same
  # rule that made the engine render a remote START call as strings applies one layer
  # in, to the wrapper's own call.
  mock_symbol = mock_tool_name(tool_name) if mock is not None else ""
  coerced = {a for a, kind in (param_types or {}).items()
             if a in params and kind != "string"}
  mock_args = ", ".join(
      f"{a!r}: _mock_arg({a})" if a in coerced else f"{a!r}: {a}" for a in params)
  mock_arg_helper = list(_MOCK_ARG_LINES) if (mock_symbol and coerced) else []
  mock_lines: list[str] = []
  lines: list[str] = [
      f'"""Call the {symbol} operation (generated)."""',
      "from typing import Any",
      "",
      "# Baked in from App(mock_apis=...). It is a CONSTANT rather than a read of the",
      "# `mock_apis` variableDeclaration's default because that default does not reach a",
      "# tool body — verified live: an app emitted with mock_apis=True still called the",
      "# real API. A session variable of the same name still overrides this.",
      f"_MOCK_DEFAULT = {bool(mock_default)!r}",
      "",
      "",
      "def _session_value(name: str) -> Any:",
      '  """Read a session variable, or None when it is not set.',
      "",
      "  CES exposes `context.variables`; the framework's own tools read `context.state`.",
      "  Which of the two carries a given value differs, so check both — a mock flag that",
      '  silently reads empty would send a "mocked" run to the real API.',
      '  """',
      "  for attr in ('variables', 'state'):",
      "    box = getattr(context, attr, None)",
      "    if box is None:",
      "      continue",
      "    try:",
      "      value = box.get(name)",
      "    except Exception:",
      "      value = None",
      "    if value is not None:",
      "      return value",
      "  return None",
      "",
      "",
      "def _unwrap(response: Any) -> dict:",
      '  """A toolset call answers a response object, a .result, or a plain dict."""',
      "  if isinstance(response, dict):",
      "    return response",
      "  for attr in ('json', 'result'):",
      "    value = getattr(response, attr, None)",
      "    if callable(value):",
      "      try:",
      "        value = value()",
      "      except Exception:",
      "        value = None",
      "    if isinstance(value, dict):",
      "      return value",
      "  as_dict = getattr(response, '__dict__', None)",
      "  return as_dict if isinstance(as_dict, dict) else {}",
      "",
      "",
      "def _tool_result(response: Any) -> Any:",
      '  """A python tool\'s return, peeled out of CES\'s {\"result\": ...} envelope.',
      "",
      "  CES wraps whatever a pythonFunction returns, and the mock is one — so its",
      "  answer arrives nested and every dot-path would dig one level too shallow.",
      '  """',
      "  data = _unwrap(response)",
      "  if isinstance(data, dict) and set(data) == {'result'}:",
      "    return data['result']",
      "  return data",
      "",
      "",
      "def _dig(node: Any, path: str) -> Any:",
      '  """Walk a dot-path; a numeric step indexes a list."""',
      "  for key in path.split('.'):",
      "    if isinstance(node, list):",
      "      if not key.isdigit() or int(key) >= len(node):",
      "        return None",
      "      node = node[int(key)]",
      "      continue",
      "    if not isinstance(node, dict):",
      "      return None",
      "    node = node.get(key)",
      "  return node",
      "",
      "",
      "def _place(target: dict, path: str, value: Any) -> None:",
      '  """Put a flat argument at its dot-path in the request body."""',
      "  keys = path.split('.')",
      "  node = target",
      "  for key in keys[:-1]:",
      "    node = node.setdefault(key, {})",
      "  node[keys[-1]] = value",
      *int_helper,
      *mock_arg_helper,
      *mock_lines,
      "",
      "",
      f"def {tool_name}({args}) -> dict[str, Any]:",
      f'  """{description}',
      "",
      "  Args:",
  ]
  for arg, wire in params.items():
    lines.append(f"    {arg}: Value for the {wire!r} parameter.")
  if not params:
    lines.append("    (none)")
  declared = [*outputs, "success", "error", "response"]
  lines += [
      "",
      "  Returns:",
      f"    Dict with {', '.join(repr(k) for k in declared)}.",
      '  """',
  ]
  lines += coerce_lines
  lines += ["  request: dict[str, Any] = {}"]
  for arg, wire in params.items():
    # An unset optional argument is omitted rather than sent empty: a blank query
    # parameter is a filter the caller never asked for, and some APIs 400 on it.
    lines += [
        f"  if {arg} not in (None, ''):",
        f"    _place(request, {wire!r}, {arg})",
    ]
  fail = "{" + ", ".join(
      [*(f"{k!r}: None" for k in outputs),
       "'success': False", "'error': str(exc)", "'response': {}"]) + "}"
  lines += [
      "  data = None",
      "  # A per-tool payload pins THIS call for one session (set it from an eval or a",
      "  # before_model callback); it works whether or not a mock was declared.",
      f"  pinned = _session_value('{MOCK_VAR_PREFIX}{tool_name}')",
      "  if pinned:",
      "    data = _unwrap(pinned)",
  ]
  if mock_symbol:
    lines += [
        f"  flag = _session_value({MOCK_FLAG_VAR!r})",
        "  # The session wins over the build-time default, either way round: an app",
        "  # emitted live can be mocked, and a mocked one can be sent to the real API.",
        "  if data is None and (_MOCK_DEFAULT if flag is None else bool(flag)):",
        # Keyed by the mock tool's OWN parameter names, not by the wire names in
        # `request` — a wire name can be a dot-path (`message.body`), which is no
        # kind of python parameter. The mock tool rebuilds the request if it wants it.
        f"    data = _tool_result(tools.{mock_symbol}({{{mock_args}}}))",
    ]
  lines += [
      "  if data is None:",
      "    try:",
      # getattr, not `tools.<symbol>`: an OpenAPI operationId (and so the symbol) may
      # contain a dash, which `tools.a_b-c(request)` would parse as subtraction.
      f"      raw = getattr(tools, {symbol!r})(request)",
      "    except Exception as exc:  # the call itself failed (transport, auth, 5xx)",
      f"      return {fail}",
      "    data = _unwrap(raw)",
      "  out: dict[str, Any] = {}",
  ]
  for key, path in outputs.items():
    lines.append(f"  out[{key!r}] = _dig(data, {path!r})")
  lines += [
      "  missing = [k for k, v in out.items() if v is None]",
      "  out['response'] = data",
      "  # The call can answer 200 and still not carry what the flow asked for, which",
      "  # is a miss rather than a fill: intake would otherwise write None into a slot.",
      "  out['success'] = not missing",
      "  out['error'] = ('missing from response: ' + ', '.join(missing)) if missing else ''",
      "  return out",
      "",
  ]
  return "\n".join(lines)


def check_no_overlapping_paths(where: str, params: Mapping[str, str]) -> None:
  """Reject request parameters whose wire paths nest inside one another.

  The wrapper assembles the request by walking each dot-path with `setdefault`, so a
  pair like `message` and `message.body` would either overwrite one value or crash the
  sandbox with a TypeError (`setdefault` on the string already placed at `message`).
  Neither is what the author meant — caught here rather than at call time.
  """
  wires = list(params.values())
  for outer in wires:
    for inner in wires:
      if inner != outer and inner.startswith(outer + "."):
        raise ValueError(
            f"{where}: request parameters {outer!r} and {inner!r} overlap — one is "
            "nested inside the other, so assembling the request would corrupt it. Send "
            "one or the other, not both"
        )


def register_wrapper(
    name: str,
    symbol: str,
    *,
    params: Mapping[str, str],
    outputs: Mapping[str, str],
    description: str,
    mock: Any,
    mock_default: bool,
    meta: Mapping[str, Any],
    param_types: Optional[Mapping[str, str]] = None,
) -> None:
  """Register a generated toolset wrapper (and its mock tool, if any).

  Shared by the OpenAPI and MCP builders — the wrapper source, the output-key contract,
  and the mock-tool emission are identical; only `meta` differs (it records the toolset
  kind, so the build and the validation guards can tell wrappers apart). `render` is
  stored on the wrapper's meta so a caller can re-render it at build time (a deferred
  `outputs`, the baked mock default).
  """
  render = {"symbol": symbol, "description": description,
            "params": dict(params), "outputs": dict(outputs), "mock": mock}
  if param_types:
    render["param_types"] = dict(param_types)
  _tools.register_source_tool(
      name,
      wrapper_tool_source(name, mock_default=mock_default, **render),
      output_keys=[*outputs, "success", "error", "response"],
      meta={**dict(meta), "has_mock": mock is not None, "render": render},
  )
  if mock is not None:
    # A tool of its own, so it can be read and edited in the CES console. Registered but
    # never referenced by a task, so `scoped_agent_tools` leaves it off every agent — the
    # model cannot call it, only the wrapper's body can.
    mocked = mock_tool_name(name)
    _tools.register_source_tool(
        mocked,
        mock_tool_source(mocked, params, mock,
                         _mock_convention(name, mock, params) if callable(mock)
                         else "named"),
        meta={"mock_for": name},
    )


def mock_flag_variable(default: bool) -> dict[str, Any]:
  """The `mock_apis` variable declaration, whose default `App(mock_apis=...)` sets.

  A BOOLEAN-typed variable rather than a build-time constant so the choice stays
  live: an eval, a callback, or a console edit can flip one session to mocked without
  touching the emitted app.
  """
  return {
      "name": MOCK_FLAG_VAR,
      "description": (
          "When true, toolset-backed tools answer from their declared mock instead of "
          f"calling the API. A per-tool '{MOCK_VAR_PREFIX}<tool>' variable holding a "
          "payload overrides this for that one call."
      ),
      "schema": {"type": "BOOLEAN", "default": bool(default)},
  }


def task_output_keys(all_configs: Mapping[str, Any], tool_name: str) -> list[str]:
  """Response keys every task firing `tool_name` asks for, in first-seen order.

  The build uses this to fill a wrapper's `outputs` from what its tasks actually want,
  so nothing has to be declared twice — for OpenAPI against the response schema, for
  MCP as literal dot-paths (there is no schema to resolve against).
  """
  keys: list[str] = []
  for cfg in all_configs.values():
    for task in cfg.get("tasks") or []:
      if task.get("tool") != tool_name:
        continue
      for key in (task.get("outputs") or {}):
        if key not in keys and key not in ("success", "error", "response"):
          keys.append(key)
  return keys


def environment_json(toolsets: Sequence[Any]) -> "str | None":
  """`environment.json` for the `env_scoped` toolsets, or None when none are.

  CES merges this over the committed resources at import, which is how one app dir
  deploys to dev and prod with different URLs and secrets. Works across toolset kinds:
  each contributes its own `environment_entry()` under its display name.
  """
  entries = {ts.name: entry for ts in toolsets
             if (entry := ts.environment_entry()) is not None}
  if not entries:
    return None
  return json.dumps({"toolsets": entries}, indent=2)
