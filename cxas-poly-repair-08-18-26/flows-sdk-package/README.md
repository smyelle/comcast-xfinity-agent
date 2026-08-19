# flows

Author, validate, simulate, and emit CXAS/CES slot-filling agents.

`flows` is the standalone slot-filling **framework + authoring toolkit** — the single
source of truth used by both external agent builders and Labs (Slot Studio). It is the
build-time companion to `cxas-scrapi` (the runtime/interaction library): `flows` *authors*
an agent; `cxas-scrapi` *drives and deploys* it.

- Offline core (`pip install flows`): config models, validator, offline simulator, and the
  CXAS app emitter — stdlib + pydantic + pyyaml only.
- Deploy extra (`pip install "flows[deploy]"`): push + live/audio evals via `cxas-scrapi`.

## Authoring surface

Everything you author with is re-exported on the top-level `flows` module (see the docs
for the full reference):

- Slots + tasks: `user_slot`, `intent_slot`, `passive_slot`, `announce`, `event_slot`,
  `result_slot`, `task`, `component`, plus `setter_group` (one shared multi-field setter).
- Components: `repeated` (collection loops), `readback` (readback formats), `cancel` /
  `escalate` / `no_input` / `hold_and_wait` (control blocks), conditions `has`/`unset`/`eq`/`ne`.
- Refusing a hand-off with the right words: `escalate(condition=...)` gates the block, and
  `declined_say` takes a line, a ladder indexed by the refusal count, or a list of
  `{"when": ..., "say": ...}` REASONS for a gate that can say no for more than one.
  See [`docs/refusal-reasons.md`](docs/refusal-reasons.md).
- Turns the caller did not take: on an inactivity tick, and on an async completion
  delivered onto silence, a deterministic cue match may not fill a slot from the previous
  turn's words — so a caller who says nothing is not recorded as having answered. A caller
  who DOES answer on that turn still is. See [`docs/silent-turns.md`](docs/silent-turns.md).
- Routing: `HostRouter` (+ `route_cues`) / `Agent` for multi-agent apps; `router_flow` for a
  single-agent router root; `journey` + `Operation` for connected journeys (intent → shared
  spine → intent-gated terminals).
- A2A (call another agent): `remote_agent` / `ces_agent` + `agent_skill` declare a remote
  Agent2Agent agent as a tool; `delegate` fires one from a slot-filling task and lands its
  reply in a slot. See [`docs/a2a.md`](docs/a2a.md).
- Telephony hand-off (reach a live agent): `handoff` + a vendor payload (`ujet`,
  `dialogflow_cx`, or a raw dict) emit the payload AND the `end_session` that has to
  accompany it. See [`docs/handoff.md`](docs/handoff.md).
- OpenAPI (call a REST API): `openapi_toolset` declares one as a CES `openApiToolset`;
  a task naming an `operationId` gets its wrapper generated, with parameters and
  response paths taken from the spec and built-in mocking. `api_tool` is the escape
  hatch. See [`docs/openapi.md`](docs/openapi.md).
- MCP (call a Model Context Protocol server): `mcp_toolset` declares one as a CES
  `mcpToolset`; `mcp_tool` declares each tool's contract (there is no spec — CES
  discovers the server's tools at runtime) and its wrapper is generated, with the same
  built-in mocking. See [`docs/mcp.md`](docs/mcp.md).
- Guardrails (the platform's own checks): `safety`, `blocklist`, `policy` and
  `prompt_guard` emit real `guardrails/<Name>/<Name>.json` resources, attachable on the
  `App`, an `Agent` or the `HostRouter`. **Scope is the decision that matters** —
  `scope="user"` prevents on both models, `scope="agent"` only detects on
  `gemini-3.1-flash-live` (the caller hears the line, then the action). See
  [`docs/guardrails.md`](docs/guardrails.md).
- Slot value policy: `default` (with `flows.fallback(..., when=)`), `reject` for an
  upstream's not-answered-yet placeholder, `publish` to mirror a value back out to
  session variables, and `shared` to keep it across a re-arm. `validate_app` warns
  when a default is a value no branch accepts.
- Turn-relative gating: `since(slot, turns=1)` — filled, AND filled on an earlier turn,
  so a branch cannot act on a latch the same turn the branch above it set.
- Session-variable ingress: `variable_map` (+ `bind`) declares one way a conversation can
  start — the variables it arrives with, and the slots they fill — so a call handed over
  with an account number is not asked for one. Declare one per entry path; the first whose
  bindings all resolve is used.
- Config → source: `render_config_source` / `render_app_source` / `raw` — deterministically
  render a `Config` back into idiomatic, round-tripping `flows` Python (the migration / "export
  as Python" path). Builder-match-or-raw: a builder is only used when it reproduces the dict
  byte-for-byte (key order included), so nothing is silently downgraded — including nested
  calls like `announce(..., handoff=handoff(ujet(menu_id="90")))`.
- Build: `validate_app`, `build_app`; YAML interop: `load_flow`, `load_app`.

## Emitting is fail-closed

`flows emit` either writes a COMPLETE app dir or leaves nothing to deploy:

- A failed scaffold (framework drift vs the blessed manifest, duplicate resource UUIDs,
  a rejected dag) prints the real cause on stderr, exits non-zero, and removes the
  half-built tree. `--keep-failed` keeps it for debugging, stamped `EMIT_FAILED.txt`.
- Every emit ends with an asked-vs-landed self-check: each declared
  `variableDeclarations` entry, each agent, each tool a task or slot names, and the
  framework files. A disagreement fails the emit (`EmitIntegrityError`).
- `flows check --app-dir <dir>` runs the dir-only half of that check, and `flows deploy`
  runs it before pushing so a tree emitted by something else is still gated
  (`--no-verify` opts out).

## Install

`flows` is published to a GCP Artifact Registry **Python** repo (region + project are
this deployment's `GCP_REGION`/`GCP_PROJECT`; the exact index URL is the terraform
`python_index_url` output). Authenticate with your Google credentials once, then pip
installs resolve against the repo:

```bash
# one-time: keyring backend that hands pip a gcloud access token
pip install keyring keyrings.google-artifactregistry-auth
gcloud auth application-default login        # or: gcloud auth login

pip install flows \
  --index-url https://<region>-python.pkg.dev/<project>/python/simple/
```

Prefer to pin the index globally? Put it in `pip.conf` / `uv.toml` (`index-url`), or for
`uv`: `uv pip install flows --index-url <same URL>`. No registry access? Build locally
instead: `uv build packages/flows` → `pip install dist/flows-*.whl`.

## Calling other agents (A2A)

Declare a remote [Agent2Agent](https://a2a-protocol.org/) agent — an ADK agent, a SaaS
agent, another CXAS app — and `flows` emits it as a body-less `remoteAgentTool` resource
and scopes it onto the agent. Use it model-callable (`{@TOOL: name}` in the instruction)
or from a slot-filling task via `delegate`, which handles the A2A reply envelope that a
bare `task()` cannot. See [`docs/a2a.md`](docs/a2a.md) and
[`examples/a2a_remote_agents.py`](examples/a2a_remote_agents.py).

## Handing off to a live agent

A contact-center platform routes a caller to a human on a structured payload, not on
anything the agent says — and that payload is only half of the hand-off. The other half
is the `end_session` that gives up the leg; a payload without it leaves the caller on a
call nobody is coming to, and an end without it drops them. `flows.handoff` emits the
pair as a unit at all four places a flow gives up a call (the `escalate` rail, a
terminal announce, a task's `on_failure.on_exhaust`, a slot's `validation.on_exhaust`),
and the framework validator rejects a hand-written config that split them.

```python
human = flows.handoff(flows.ujet(menu_id="90"))
flow.set("escalate", flows.escalate(say="Let me get you to someone.", handoff=human))
```

Vendor payloads are swappable (`ujet`, `dialogflow_cx`, or a raw dict for a platform
with no builder yet). See [`docs/handoff.md`](docs/handoff.md) and
[`examples/telephony_handoff.py`](examples/telephony_handoff.py).

## Calling a REST API (OpenAPI)

Declare an API with `openapi_toolset` and `flows` emits a CES `openApiToolset` — its own
resource kind, under `toolsets/`, with the spec beside it. An agent cannot call a
toolset, so the build generates the `pythonFunction` wrapper that can — one per
operation a flow actually fires, taking its parameters and response paths from the spec
so nothing is restated. It lifts the paths a task asked for to the flat keys intake
maps by, reports a real `success`, and turns a 4xx/5xx into a result the task's
`on_failure` can act on. Mocking is a runtime switch, so one deployed
app flips between the mock and the live API without a rebuild. See
[`docs/openapi.md`](docs/openapi.md) and
[`examples/openapi_toolsets.py`](examples/openapi_toolsets.py).

## Calling an MCP server (MCP)

Declare a [Model Context Protocol](https://modelcontextprotocol.io/) server with
`mcp_toolset` and `flows` emits a CES `mcpToolset` — its own resource kind, under
`toolsets/`, a sibling of `openApiToolset`. It is one file, not two: an MCP server has no
local spec, because CES discovers its tools at runtime. So `mcp_tool` — required here, not
an escape hatch — states each tool's parameters and result paths, and its
`pythonFunction` wrapper is generated: the same intake shaping, the same runtime mocking,
the same shared authentication as OpenAPI, plus MCP's own `$context.variables.*` custom
headers. See [`docs/mcp.md`](docs/mcp.md) and
[`examples/mcp_toolsets.py`](examples/mcp_toolsets.py).

## App-level CES settings (and who wins at deploy)

`timeZoneSettings`, `guardrails`, `loggingSettings` and the rest of app.json's top
level are declarable on `flows.App`, so they live in your source instead of only in
the deployed app:

```python
app = flows.App(
    host=host, agents=AGENTS, app_display_name="Security Freeze IVA",
    time_zone="America/New_York",                     # -> timeZoneSettings.timeZone
    guardrails=["Default Safety Guardrail", "Default Prompt Guardrail"],
    app_settings={                                    # the long tail, verbatim
        "loggingSettings": {
            "conversationLoggingSettings": {"retentionWindow": "31536000s"}},
        "toolExecutionMode": "PARALLEL",
    },
)
```

`time_zone` is validated against the IANA database at authoring time (a wrong zone is
as silent as no zone, and shifts every date the agent derives from `current_date`).
`guardrails=[]` DECLARES that the app runs with none; leaving it `None` says "not
mine". `app_settings` rejects any key an `App` field already owns (`modelSettings` →
`model=`, `languageSettings` → `languages=`, ...) so only one thing can write each.

**Declared beats preserved; undeclared falls back to preserved.** `cxas push
--overwrite` replaces the whole app, so `flows deploy` merges app-level settings back
from the live target (`deploy/prep.PRESERVE`). That merge used to always win, which
meant a freshly created app — nothing to preserve — came up on the platform's
timezone rather than the one the source asked for, and an author who wrote
`time_zone=` got the target's value anyway. Now: what you declared stands, everything
else is still preserved, and the deploy prints a `WARN` for every behavioural setting
it took from the target instead of from your source. `flows emit` records which keys
are yours in `declared-settings.json`, because `flows deploy` is handed a path, not an
`App`; an app dir without that file declares nothing and deploys exactly as before.

The post-emit integrity check covers them too: declare a timezone and an emit that
fails to write it is a failed emit, and `flows check` catches a declared setting
edited back out of app.json.

## Multi-language support

Author multilingual agents from a few `flows.App` fields (`languages`,
`default_language`, `language_switching`): a single-language app, caller-initiated
switching (`explicit`/`auto`), or a turn-1 language menu with a hard lock (`select`).
It is opt-in and does not modify the slot-filling engine. See
[`docs/language-support.md`](docs/language-support.md) for the full walkthrough and
[`examples/language_switching.py`](examples/language_switching.py) for a runnable demo.

## Releasing

Publishing is automated by [`.github/workflows/publish-flows.yml`](../../.github/workflows/publish-flows.yml):
bump `version` in [`pyproject.toml`](pyproject.toml) and merge to `main` — the workflow
builds the wheel + sdist and uploads them to the Artifact Registry repo. Re-runs without a
version bump are a no-op (the workflow checks whether the version already exists first, since
Artifact Registry rejects duplicate uploads), so only a new version publishes.
