"""Calling a Model Context Protocol server from a flow — an MCP toolset, with mocking.

Declare the server and fire one of its tools. CES DISCOVERS the server's tools at
runtime, so — unlike an OpenAPI spec — there is nothing to parse offline: the author
states the contract of each tool with `mcp_tool`, and the build generates the wrapper:

    account = flows.mcp_toolset("acme_account", server_url="https://.../mcp/")
    flows.mcp_tool("get_balance", account, "get_account_balance", params=["account_id"],
                   outputs={"balance": "data.currentBalance"})
    flow.task("look_up", "get_balance", {"account_id": "acct"}, "balance", out_key="balance")

CES models the server as an `mcpToolset`: its own resource kind, emitted under
`toolsets/` (a sibling of `openApiToolset`), NOT a tool under `tools/`. It is ONE file —
an MCP server has no local spec to sit beside it. And, exactly as for OpenAPI, **an agent
cannot call a toolset**: each discovered tool exists only inside the sandbox, as
`tools.<toolset>_<tool>`, so the callable thing a flow fires is a generated
`pythonFunction` wrapper that forwards to it. `mcp_tool` generates that wrapper — here it
is REQUIRED, not an escape hatch, because there is no spec to derive one from.

Because the body is generated, it fixes the same three mismatches with intake the
OpenAPI wrapper does (see `openapi_toolsets.py`): it lifts the paths a task named out of
the nested result to the FLAT top-level keys intake maps by, returns a real `success`
(false when the call failed AND when it answered without the field the flow asked for),
and turns a transport error into a result a task's `on_failure` can act on.

MOCKING is the same runtime switch. A declared mock is emitted ALONGSIDE the live call,
so one deployed app flips between them without a rebuild:

    App(mock_apis=True)               every tool with a mock answers from it
    session var `mock_apis`           overrides that default, either direction
    session var `mock_<tool>`         pins ONE call to a payload (evals)

AUTH is the shared `apiAuthentication` message CES injects — the same builders OpenAPI
uses (`flows.oauth_auth` and friends). CUSTOM HEADERS are MCP's own: each value is a
`$context.variables.<name>` reference CES resolves from the session.

Run:  python examples/mcp_toolsets.py
"""

import flows

# ---------------------------------------------------------------------------
# The toolset. `server_url` is the MCP server address; a server built with the MCP SDK
# expects the `/mcp/` suffix. Secrets are referenced by Secret Manager VERSION and never
# inlined — `flows` refuses a raw value, because committing a live credential is the one
# mistake no later stage can undo. Custom headers carry per-session values CES resolves
# from `$context.variables.*`.
#
# `mocks` are keyed by the server's tool name, for a tool that needs no own `mock=`.
# ---------------------------------------------------------------------------

account_api = flows.mcp_toolset(
    "acme_account",                                   # a python identifier: it prefixes
    server_url="https://accounts.acme.example.com/mcp/",  # tools.acme_account_<tool>
    description="Acme account lookups over MCP.",
    auth=flows.oauth_auth(
        client_id="acme-iva",
        token_endpoint="https://identity.acme.example.com/oauth2/v1/token",
        secret="projects/example/secrets/acme-mcp-client/versions/1",
        scopes=["accounts/.default"],
    ),
    headers={
        "session_id": "$context.variables.session_id",
        "x-request-id": "$context.variables.e2e_request_id",
    },
    mocks={
        "get_account_balance": {
            "data": {"currentBalance": "$42.17", "dueDate": "the 15th"},
        },
    },
)

# ---------------------------------------------------------------------------
# The tool. There is no spec, so the parameters and the result paths are declared here.
# `outputs` maps a friendly key to a dot-path into the tool's result; a numeric step
# indexes a list, exactly as in OpenAPI. Omit `outputs` and the build fills it from the
# keys the tasks ask for, each taken as a literal path.
# ---------------------------------------------------------------------------

flows.mcp_tool(
    "get_balance", account_api, "get_account_balance",
    params=["account_id"],
    outputs={"balance": "data.currentBalance", "due_date": "data.dueDate"},
    description="Look up the current balance and due date for an account.",
)

# ---------------------------------------------------------------------------
# The flow. `get_balance` is the wrapper the task fires — never the toolset, and never
# the in-sandbox symbol `acme_account_get_account_balance` (validate_app errors on both).
# ---------------------------------------------------------------------------

flow = flows.Flow(
    "account_balance", root_agent="Account_Agent",
    bootstrap={"welcome_slot": "welcome"},
)
flow.add(
    flows.announce("welcome", ["I can check your account balance."], shared=True,
                   preempt=True),
    flows.user_slot("account_id", "What's your account number?", readback=True),
    flows.result_slot("balance", "look_up"),
    flows.result_slot("due_date", "look_up"),
    flows.announce(
        "status", ["Your balance is {balance}, due {due_date}."],
        requires=["balance", "due_date"], preempt=True, end=True),
)
flow.task(
    "look_up", "get_balance", {"account_id": "account_id"}, "balance",
    out_key="balance",
    extra_outputs={"due_date": "due_date"},
    condition=flows.has("account_id"),
    # The wrapper returns rather than raising, so this ladder actually runs.
    on_failure={
        "max_retries": 1,
        "retry_say": "That didn't come back. Let me try once more.",
        "on_exhaust": {"say": "I can't reach our account system right now.",
                       "then": {"tool": "transfer_to_human"}},
    },
)

app = flows.App(
    root_flow=flow,
    app_display_name="Account Balance (MCP)",
    toolsets=[account_api],
    # Flip to True to demo offline. It is baked into the emitted body AND declared as a
    # session variable, so a deployed app can still be flipped either way.
    mock_apis=False,
)

if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  print("errors:  ", errors or "none")
  print("warnings:", warnings or "none")
  flows.build_app(app, "./account_balance_app")
  print("emitted -> ./account_balance_app")
  print("  toolsets/acme_account/acme_account.json   (the resource — one file, no spec)")
  print("  tools/get_balance/…                        (the generated wrapper)")
  print("  tools/get_balance_mock/…                   (the editable mock tool)")
