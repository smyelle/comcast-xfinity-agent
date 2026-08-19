# OpenAPI toolsets

Call a REST API from a flow. Drop the spec in and fire its operations:

```python
orders = flows.openapi_toolset(
    "order_service",
    spec="specs/orders.yaml",
    base_url="https://orders.internal.example.com",
    auth=flows.api_key_auth("Authorization", secret=SECRET_VERSION),
)

app = flows.App(root_flow=flow, toolsets=[orders], ...)
flow.task("look_up", "getOrder", {"order_id": "orderId"}, "order_status",
          out_key="status")
```

Nothing is restated. The spec already declares the parameters and the response shape,
and the task already says which value it wants, so the build generates a wrapper that
bridges exactly those two — one per operation a flow actually fires.

`out_key` is a path into the response: the full dot-path (`delivery.estimatedDate`,
`lineItems.0.name`), or an unambiguous leaf name (`status`). A key the response schema
cannot account for is a build error, because nothing would ever fill that slot.

`spec=` is what makes this page about **consuming** an API: the document is the source of
truth, and `api_tool`'s `params` rename your argument names onto its wire names. Omit
`spec=` and the toolset is DECLARED instead — the build generates the document from the
`remote_tool(...)` calls that name the toolset, and a service you deploy separately
implements it. See [remote tools](remote-tools.md); everything below assumes a spec.

## A toolset is not a tool

CES models a REST dependency as an **`openApiToolset`** — its own API resource
(`create_toolset`, a sibling of `create_tool`), emitted as two files:

```
toolsets/<name>/<name>.json                            # the resource
toolsets/<name>/open_api_toolset/open_api_schema.yaml  # the spec
```

And **an agent cannot call one.** A production reference app in ces-deployment-dev
runs ten `openApiToolset`s; no agent in it names a toolset — or a
`<toolset>_<operationId>` member — in its `tools[]`. Every API call goes through an
ordinary `pythonFunction`:

```python
def search_order_by_id(order_id):
  return tools.order_search_v1_searchOrdersByOrderId({"orderId": order_id})
```

So a toolset is a capability the *sandbox* gains, and the callable thing is a wrapper.
The build generates it. This is the exact inverse of [A2A](a2a.md), where the tool is
body-less because the platform makes the call.

## Why the build generates it, and not you

Three things are only knowable once the App and its flows exist, which is why this
happens at build rather than at declaration:

- **Which operations are used.** Only what a flow fires is emitted, so a hundred-
  operation spec does not become a hundred tool resources.
- **Which response fields to lift.** One operation in that app has 37 response leaves;
  returning all of them on every call would bloat the model's context for nothing.
- **The mock default**, which `App(mock_apis=...)` sets.

The lifts are emitted as *literal* assignments, and they have to be: the blessed
validator statically parses each tool's emitted source for dict keys and **errors** on
a task output key it cannot find there. A wrapper that flattened the response
dynamically would turn every dot-path output into a build failure.

### `api_tool` — the escape hatch

Reach for it to rename the tool, expose a subset of the parameters, alias an awkward
response path, give the model a better description than the spec's summary, or hang a
mock on one operation:

```python
flows.api_tool(
    "text_tracking_link", orders, "sendTrackingSms",
    params={"phone": "phone", "body": "message.body"},
    outputs={"queued": "accepted"},
)
```

Both `params` and `outputs` are optional there too. Omit `params` and every parameter
the spec declares is exposed; omit `outputs` and the build fills them from what the
tasks ask for.

`validate_app` errors on both ways of getting this wrong:

> task 'look_up' fires 'order_service', which is an OpenAPI TOOLSET, not a tool — CES
> exposes its operations only inside the sandbox, and no agent can call a toolset.
> Declare the operation you want with flows.api_tool(<name>, order_service,
> \<operationId\>) and fire that instead

The second — firing `order_service_getOrder`, the in-sandbox symbol — is the one worth
guarding, because it looks like it ought to work: it is exactly the name the wrapper
body calls. Neither is caught by the generic "unknown tool" check, because `collect`
invents an executor stub for a task tool it does not recognise. The stub would be
emitted as a `pythonFunction` under the toolset's own name and answer in its place.

## Why the wrapper is more than a forward

Because the body is ours, it fixes three mismatches between a REST payload and how
`slot_intake._intake_executor` reads a task result:

| the problem | what the wrapper does |
| --- | --- |
| intake maps `outputs` by FLAT top-level key; a REST payload is nested | digs the declared dot-paths out to top-level keys |
| intake reads `success = bool(response_data.get(success_check))`; a REST payload has no such key | returns a real `success` |
| a 4xx/5xx raises, and a task's `on_failure` never runs if the tool never returns | catches it and returns, so the ladder runs |

`success` is false in two distinct cases, and the second is the one that matters: the
call failed, **or** it answered 200 without the field the flow asked for. Filling a
slot with `None` because a schema changed is worse than routing to `on_failure`.

```python
outputs={
    "order_status": "status",
    "delivery_date": "delivery.estimatedDate",
    "first_item": "lineItems.0.name",   # a numeric step indexes a list
}
```

A dotted path in `params` goes the other way — it nests the value in the request body,
so `{"phone": "phone", "body": "message.body"}` sends
`{"phone": ..., "message": {"body": ...}}`.

## Mocking

Mocking is a **runtime** switch. A declared `mock` is emitted alongside the live call,
so one deployed app flips between them without a rebuild.

```python
orders = flows.openapi_toolset(
    "order_service", spec="specs/orders.yaml",
    mocks={"getOrder": {"status": "out for delivery"}},   # or a fn(request) -> dict
)

app = flows.App(..., toolsets=[orders], mock_apis=True)
```

`mocks` is keyed by `operationId`. An `api_tool` takes its own `mock=` instead.

A callable mock takes the operation's **own parameters**, which is how it reads best:

```python
def fake_order(orderId: str = "") -> dict:
    return {"status": "delivered" if orderId.startswith("W") else "in transit"}
```

A single argument that is *not* one of them receives the whole assembled request
instead, for a mock that wants to see everything at once. Taking parameters the
operation does not have is a build error — nothing could supply them.

### A mock is emitted as its own tool

Not inlined into the wrapper: `tools/<tool>_mock/`, a `pythonFunction` of its own. So
it can be **read and edited in the CES console** — changing what a mocked call returns
without a rebuild, which is most of the point during a demo — and invoked on its own to
see what it produces.

It is never scoped onto an agent, so the model cannot call it. Only the wrapper's body
can, the way the reference app's `escalate_transfer` calls `get_department_ext`: a
`pythonFunction` reached through `tools.<name>(...)` while sitting in no agent's
`tools[]`.

One consequence worth knowing: CES wraps whatever a `pythonFunction` returns in
`{"result": ...}`, so the wrapper peels that envelope before its dot-paths run.

| what | effect |
| --- | --- |
| `App(mock_apis=True)` | every operation with a mock answers from it |
| session variable `mock_apis` | overrides that default, in either direction |
| session variable `mock_<tool>` | pins ONE call to a payload — what evals want |

A mock returns **what the real API would return**, not what the tool returns. The same
extraction and the same `success` rule run over it, so a passing mocked run has
actually exercised the mapping the live call depends on — point a dot-path at a field
the mock does not have and the mocked run fails too.

The `App(mock_apis=...)` choice is compiled into the emitted body as `_MOCK_DEFAULT`,
not read from the `mock_apis` variable's declared default. That default does not reach
a tool body: an app emitted with `mock_apis=True` still called the real API until the
flag became a constant. The variable is still declared — a callback, an eval or a
console edit can set it, and the body reads both `context.variables` and
`context.state`, since the framework's own tools use the latter.

An operation with no declared mock ignores the flag and goes live. `validate_app` warns when
`mock_apis=True` leaves one of those in the flow, because a half-mocked app fails in a
way that looks like the flow's fault.

CES has its own `toolFakeConfig.enableFakeMode`, which the api_hub exporter uses.
It is not used here: it is all-or-nothing per tool and cannot be flipped per session.

## Authentication

CES performs the exchange and injects the credential into every operation in the
toolset, so none of it reaches the wrapper body — for OAuth that includes the token
refresh, which is the main reason not to hand-roll a token call as a tool.

```python
flows.api_key_auth("Authorization", secret=SECRET_VERSION, location="HEADER")
flows.oauth_auth(client_id=..., token_endpoint=..., secret=..., scopes=[...])
flows.bearer_auth(secret=SECRET_VERSION)
flows.service_agent_auth()   # a Google ID token; no Secret Manager wiring at all
```

A secret is always a Secret Manager **version reference**
(`projects/<p>/secrets/<s>/versions/<v>`). `flows` refuses anything else, because
committing a live credential is the one mistake no later stage can undo.

## environment.json

By default everything is inlined and the emitted dir is self-contained. Pass
`env_scoped=True` to split the deployment-varying values into `environment.json` and
leave `$env_var` markers behind — the layout a pulled CES app uses, and what you want
to build one app dir for dev and prod:

```jsonc
// toolsets/order_service/order_service.json — structural, committed
{"apiKeyConfig": {"keyName": "Authorization", "apiKeySecretVersion": "$env_var",
                  "requestLocation": "HEADER"}}
// environment.json — what differs per deploy
{"toolsets": {"order_service": {"openApiToolset": {
    "url": "https://orders.internal.example.com",
    "apiAuthentication": {"apiKeyConfig": {"apiKeySecretVersion": "projects/…"}}}}}}
```

Where a value lives follows what it describes: how the key rides (`requestLocation`),
which grant, which scopes and the client id stay on the resource; the secret versions,
the OAuth token endpoint and the URL move.

## Offline checks

The spec is parsed at declaration — following `$ref`, and flattening `allOf` /
`anyOf` / `oneOf`, since a node composed that way carries no `properties` of its own
and its fields would otherwise be invisible. So these are build errors rather than
runtime ones:

- an `operationId` that is not in the spec (listing the ones that are)
- an output key the response schema cannot supply (listing the paths and leaf names it
  can), so a slot that would never fill is caught before the push
- a parameter that is not on that operation
- a required parameter no argument supplies
- a `mocks` entry naming an operation the spec does not declare
- a toolset name that is not a python identifier — it prefixes
  `tools.<name>_<operationId>`, so a dash is a `NameError` on the first live call
- an `outputs` key of `success`, `error` or `response` (the wrapper sets those)
- a `spec=` path that does not resolve, said plainly — it would otherwise be parsed AS
  the document and fail two steps later as "got str"
- **a response with no `description`**, which OpenAPI requires. CES rejects the spec and
  drops the WHOLE toolset at import *without failing the push*, so the only symptom is a
  live call failing with `Tool with name <toolset>_<operationId> not found` — pointing at
  the operation rather than at the spec. Proven by pushing one spec twice, differing only
  by that line
- a spec with no `operationId`s at all, naming the `METHOD /path` of each one that
  needs one. No name is invented for them: CES derives the sandbox symbol from the
  spec, and a guess that does not match is a `NameError` on the first live call rather
  than a build error. Real CES specs do ship operations without an id

## Verified

Against `https://api.zippopotam.us` (public, auth-free) from a flows-emitted app in
ces-deployment-dev. The same app answered **"Beverly Hills, California"** with the flag
off and **"Mockville, Mockachusetts"** with it on, filling two slots from
`places.0.place name` and `places.0.state` — a dot-path through a list, into a key with
a space in it.

## See also

- [Remote tools](remote-tools.md) — the same toolset with no `spec=`: the agent declares
  the contract and a separately-deployed service implements it.
- [A2A remote agents](a2a.md) — the inverse shape: a body-less tool the platform calls.
- `examples/openapi_toolsets.py` — a two-operation app with mocks, in the CI harness.
