# MCP toolsets

Call a Model Context Protocol server from a flow. Declare the server and fire its tools:

```python
account = flows.mcp_toolset(
    "acme_account",
    server_url="https://accounts.internal.example.com/mcp/",
    auth=flows.oauth_auth(client_id=..., token_endpoint=..., secret=SECRET_VERSION),
    headers={"session_id": "$context.variables.session_id"},
)

flows.mcp_tool("get_balance", account, "get_account_balance",
               params=["account_id"], outputs={"balance": "data.currentBalance"})

app = flows.App(root_flow=flow, toolsets=[account], ...)
flow.task("look_up", "get_balance", {"acct": "account_id"}, "balance", out_key="balance")
```

This is the sibling of the [OpenAPI toolset](openapi.md) — same resource kind, same
wrapper, same mocking — with one difference that shapes everything: **an MCP server has
no local spec.** CES discovers the tools it exposes at runtime by calling the server, so
there is nothing to parse offline. `openapi` derives a wrapper from the spec; here you
state each tool's contract, and `flows` takes you at your word.

## A toolset is not a tool

CES models an MCP dependency as an **`mcpToolset`** — its own API resource
(`create_toolset`, in the same `Toolset` oneof as `openApiToolset`), emitted as a single
file:

```
toolsets/<name>/<name>.json          # the resource — one file, no spec beside it
```

And **an agent cannot call one.** Each discovered tool exists only inside the sandbox, as
`tools.<toolset>_<tool>`, so the callable thing a flow fires is an ordinary
`pythonFunction` that forwards to it:

```python
def get_balance(account_id):
  return tools.acme_account_get_account_balance({"account_id": account_id})
```

`mcp_tool` generates that wrapper. Unlike OpenAPI's `api_tool`, it is **required, not an
escape hatch** — with no spec, a flow reaches an MCP tool only through one of these. The
CES linter works the same way: it reserves the `<toolset>_` prefix and skips
operation-level checks for MCP, because they cannot be resolved offline.

## `mcp_tool` — the declared contract

```python
flows.mcp_tool(
    "submit_payment", account, "submit_one_time_payment",
    params={"account_id": "accountId", "amount": "amountCents"},
    outputs={"confirmation": "data.confirmationNumber"},
    description="Take a one-time payment on the account.",
)
```

- `tool_name` is the name the server reports, used verbatim to build the symbol
  `tools.<toolset>_<tool_name>`. It cannot be checked offline, so a typo is a `NameError`
  on the first live call — and it must be a python identifier tail (a dashed server tool
  needs a CES `toolOverride` to alias it, which `flows` does not emit yet).
- `params` is a list (names as-is) or a `{arg: wire}` dict (rename; a dotted wire path
  nests the value, since MCP arguments are a JSON object). An MCP tool that takes no
  arguments is fine — omit `params`.
- `outputs` maps a friendly key to a dot-path into the tool's result, flattened to the
  top level because intake maps a task's `outputs` by FLAT key. No response schema exists
  to check a path against, so any path is accepted; omit it and the build fills it from
  the keys the tasks ask for, each taken as a literal path.

Two `params` limits worth knowing: two wire paths that nest inside one another
(`message` and `message.body`) are rejected at build time — they would corrupt the
assembled request; and a numeric step in a request path (`items.0.sku`) assembles a
dict (`{"items": {"0": ...}}`), not a JSON list, so pass a whole array as one argument
rather than by index.

`validate_app` errors on firing the toolset itself, or on firing the in-sandbox symbol
`<toolset>_<tool>` directly — both name the fix (`flows.mcp_tool(...)`), because neither
is caught by the generic "unknown tool" check.

## Why the wrapper is more than a forward

Because the body is generated, it fixes the same three mismatches with intake the OpenAPI
wrapper does (see [OpenAPI toolsets](openapi.md#why-the-wrapper-is-more-than-a-forward)):

| the problem | what the wrapper does |
| --- | --- |
| intake maps `outputs` by FLAT top-level key; an MCP result is nested | digs the declared dot-paths out to top-level keys |
| intake reads `success = bool(response_data.get(success_check))` | returns a real `success` |
| a transport error raises, and a task's `on_failure` never runs if the tool never returns | catches it and returns, so the ladder runs |

`success` is false in two distinct cases: the call failed, **or** it answered without the
field the flow asked for. Filling a slot with `None` because the server changed is worse
than routing to `on_failure`.

## Mocking

The same runtime switch as OpenAPI. A declared `mock` is emitted alongside the live call,
so one deployed app flips between them without a rebuild:

```python
flows.mcp_tool("get_balance", account, "get_account_balance", params=["account_id"],
               outputs={"balance": "data.currentBalance"},
               mock={"data": {"currentBalance": "$42.17"}})   # or a fn(**params) -> dict

app = flows.App(..., toolsets=[account], mock_apis=True)
```

| what | effect |
| --- | --- |
| `App(mock_apis=True)` | every tool with a mock answers from it |
| session variable `mock_apis` | overrides that default, in either direction |
| session variable `mock_<tool>` | pins ONE call to a payload — what evals want |

A `mocks={tool_name: ...}` on the toolset serves a tool whose `mcp_tool` did not carry
its own `mock=`. A mock returns **what the real tool would return**, so the same
extraction and `success` rule run over it and a passing mocked run has exercised the live
mapping. It is emitted as its own editable `pythonFunction`, scoped onto no agent — the
model cannot call it, only the wrapper's body can. See
[OpenAPI mocking](openapi.md#mocking) for the full mechanics; they are shared.

## Authentication

CES performs the exchange and injects the credential — the same `apiAuthentication`
message `openApiToolset` carries, so the same builders apply:

```python
flows.api_key_auth("Authorization", secret=SECRET_VERSION, location="HEADER")
flows.oauth_auth(client_id=..., token_endpoint=..., secret=..., scopes=[...])
flows.bearer_auth(secret=SECRET_VERSION)
flows.service_agent_auth()   # a Google ID token; no Secret Manager wiring at all
```

A secret is always a Secret Manager **version reference**
(`projects/<p>/secrets/<s>/versions/<v>`); `flows` refuses anything else.

## Custom headers

MCP's own field. Every request to the server carries these, and each value must be a
`$context.variables.<name>` reference CES resolves from the session — the format the
platform requires, so a literal is refused at authoring time:

```python
headers={
    "session_id": "$context.variables.session_id",
    "x-request-id": "$context.variables.e2e_request_id",
}
```

## environment.json

By default everything is inlined and the emitted dir is self-contained. Pass
`env_scoped=True` to split the deployment-varying values — the server address and the
secret references — into `environment.json` and leave `$env_var` markers behind:

```jsonc
// toolsets/acme_account/acme_account.json — structural, committed
{"mcpToolset": {"serverAddress": "$env_var",
                "customHeaders": {"session_id": "$context.variables.session_id"}}}
// environment.json — what differs per deploy
{"toolsets": {"acme_account": {"mcpToolset": {
    "serverAddress": "https://accounts.internal.example.com/mcp/",
    "apiAuthentication": {"oauthConfig": {"clientSecretVersion": "projects/…"}}}}}}
```

The headers stay on the resource — a `$context.variables.*` reference is per session, not
per deployment.

## Transport

CES speaks **Streamable HTTP only**. A server built with the MCP SDK expects the `/mcp/`
suffix on its address (`https://host/mcp/`). There is no transport field to set.

## Verified

The `mcpToolset` resource shape is derived from the CES `McpToolset` proto
(`google.cloud.ces_v1beta`) — there is no public MCP fixture to read it off, the way the
OpenAPI toolset was read off a pulled production app. The wrapper, the mock machinery, and
the auth are the same code paths OpenAPI verifies live.

## See also

- [OpenAPI toolsets](openapi.md) — the sibling, for a REST API with a spec.
- [A2A remote agents](a2a.md) — a body-less tool the platform calls.
- `examples/mcp_toolsets.py` — a one-tool app with a mock, in the CI harness.
