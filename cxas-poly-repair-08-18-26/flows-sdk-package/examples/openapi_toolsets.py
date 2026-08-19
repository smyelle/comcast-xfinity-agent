"""Calling a REST API from a flow — an OpenAPI toolset, with mocking.

Drop the spec in and fire its operations. The spec already declares the parameters and
the response shape, and a task already says which value it wants, so nothing in between
is restated:

    orders = flows.openapi_toolset("order_service", spec="specs/orders.yaml")
    flow.task("look_up", "getOrder", ["order_id"], "status", out_key="status")

CES models the API as an `openApiToolset`: its own resource kind, emitted under
`toolsets/` with the spec beside it, NOT a tool under `tools/`. That distinction is the
whole shape of this component, because **an agent cannot call a toolset**.

That is not a guess. A production reference app in ces-deployment-dev runs ten
`openApiToolset`s, and no agent in it names a toolset — or a `<toolset>_<operationId>`
member — in its `tools[]`. Every API call goes through an ordinary `pythonFunction`
whose body calls the operation:

    def search_order_by_id(order_id):
      return tools.order_search_v1_searchOrdersByOrderId({"orderId": order_id})

So a toolset is a capability the SANDBOX gains, and the callable thing is a wrapper.
The build generates it — one per operation a flow actually fires, so a large spec does
not become a hundred tool resources. This is the exact inverse of A2A, where the tool is
body-less because the platform makes the call (see `a2a_remote_agents.py`).

Because the body is generated, it fixes three mismatches with intake:

    the problem                        what the wrapper does
    ---------------------------------  ----------------------------------------
    intake maps `outputs` by FLAT      lifts the paths the task named out of the
    top-level key, a REST payload      nested payload to top-level keys
    is nested
    intake reads `success =            returns a real `success` — false when the
    bool(response.get(...))`, a REST   call failed AND when it answered 200
    payload has no such key            without the field the flow asked for
    a 4xx/5xx raises, and a task's     catches it and returns, so `on_failure`
    on_failure never runs if the       actually runs
    tool never returns

Those lifts are emitted as LITERAL assignments, which is why the build has to know
which ones you want: the blessed validator statically parses each tool's emitted source
for dict keys and errors on a task output key it cannot find there. A wrapper that
flattened the response dynamically would turn every dot-path output into a build error.

MOCKING is a runtime switch. A declared mock is emitted ALONGSIDE the live call, so one
deployed app flips between them without a rebuild:

    App(mock_apis=True)               every operation with a mock answers from it
    session var `mock_apis`           overrides that default, either direction
    session var `mock_<tool>`         pins ONE call to a payload (evals)

A mock returns what the REAL API would return, so the same extraction and the same
`success` rule run over it — a passing mocked run has exercised the live mapping.

Verified live against `https://api.zippopotam.us` (a public, auth-free API): the same
app answered "Beverly Hills, California" with the flag off and "Mockville,
Mockachusetts" with it on, from a spec dropped in and referenced by `operationId`.

Run:  python examples/openapi_toolsets.py
"""

import flows

# ---------------------------------------------------------------------------
# The spec. Normally `spec="specs/orders.yaml"` — a path, the document text, or a
# dict all work. Inline here so the example is self-contained.
# ---------------------------------------------------------------------------

ORDERS_SPEC = """
openapi: 3.0.1
info:
  title: Order Service
  description: Order lookup and tracking notifications.
  version: 1.0.0
paths:
  /api/orders/{orderId}:
    get:
      summary: Look up an order by its ID.
      operationId: getOrder
      parameters:
        - name: orderId
          in: path
          required: true
          schema: {type: string}
          description: The order ID, e.g. WN45992058.
      responses:
        '200':
          description: The order.
          content:
            application/json:
              schema:
                type: object
                properties:
                  status: {type: string}
                  delivery:
                    type: object
                    properties:
                      estimatedDate: {type: string}
                  lineItems:
                    type: array
                    items:
                      type: object
                      properties:
                        name: {type: string}
  /api/notifications/sms:
    post:
      summary: Text the caller a tracking link.
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
      responses:
        '200':
          description: Accepted.
          content:
            application/json:
              schema:
                type: object
                properties:
                  accepted: {type: boolean}
"""

# ---------------------------------------------------------------------------
# The toolset. `base_url` overrides whatever environment the published spec names —
# usually the point, since a spec from another team names THEIR host. Secrets are
# referenced by Secret Manager VERSION and never inlined; `flows` refuses a value that
# does not look like a reference, because committing a live credential is the one
# mistake no later stage can undo.
#
# `mocks` are keyed by operationId, for the operations that need no `api_tool` call to
# hang a mock on. Each returns what the REAL API would return.
# ---------------------------------------------------------------------------

orders_api = flows.openapi_toolset(
    "order_service",                       # a python identifier: it prefixes
    spec=ORDERS_SPEC,                      # tools.order_service_<operationId>
    description="Order lookup and tracking notifications.",
    base_url="https://orders.internal.example.com",
    auth=flows.api_key_auth(
        "Authorization",
        secret="projects/example/secrets/orders-api-token/versions/1",
    ),
    mocks={
        "getOrder": {
            "status": "out for delivery",
            "delivery": {"estimatedDate": "Thursday"},
            "lineItems": [{"name": "cordless drill"}],
        },
    },
)

# ---------------------------------------------------------------------------
# `api_tool` is the ESCAPE HATCH, not the normal path. Reach for it to rename the
# tool, expose a subset of the parameters, alias an awkward response path, or give
# the model a better description than the spec's summary.
#
# Here it earns its keep: a dotted wire path nests the value in the request body, so
# the wrapper sends {"phone": ..., "message": {"body": ...}}.
# ---------------------------------------------------------------------------

flows.api_tool(
    "text_tracking_link", orders_api, "sendTrackingSms",
    params={"phone": "phone", "body": "message.body"},
    outputs={"queued": "accepted"},
    description="Text the caller a link to track their order.",
    mock={"accepted": True},
)

# ---------------------------------------------------------------------------
# The flow. `getOrder` is never declared anywhere: the task names the operationId and
# the build generates its wrapper, lifting exactly the response paths named below.
# `status` and `estimatedDate` are unambiguous leaf names from the response schema;
# `lineItems.0.name` is the full path.
# ---------------------------------------------------------------------------

flow = flows.Flow(
    "order_status", root_agent="Order_Agent",
    bootstrap={"welcome_slot": "welcome"},
)
flow.add(
    # Both announces preempt. An announce only renders its own `texts` on a turn that
    # preempts the model; authored with the default the words are dropped and the
    # caller hears the queued ask alone.
    flows.announce("welcome", ["I can check on an order for you."], shared=True,
                   preempt=True),
    flows.user_slot("order_id", "What's your order number?", readback=True),
    flows.result_slot("order_status", "look_up"),
    flows.result_slot("delivery_date", "look_up"),
    flows.announce(
        "status", ["Your order is {order_status}, arriving {delivery_date}."],
        requires=["order_status", "delivery_date"], preempt=True,
    ),
    flows.user_slot(
        "phone", "What number should I text the tracking link to?",
        condition=flows.has("order_status"),
    ),
    flows.result_slot("queued", "text_link"),
    flows.announce("done", ["Sent. Anything else?"], requires=["queued"], end=True),
)
flow.task(
    "look_up", "getOrder", {"order_id": "orderId"}, "order_status",
    out_key="status",
    extra_outputs={"estimatedDate": "delivery_date"},
    condition=flows.has("order_id"),
    # The wrapper returns rather than raising, so this ladder actually runs.
    on_failure={
        "max_retries": 1,
        "retry_say": "That didn't come back. Let me try once more.",
        "on_exhaust": {"say": "I can't reach our order system right now.",
                       "then": {"tool": "transfer_to_human"}},
    },
)
flow.task(
    "text_link", "text_tracking_link",
    {"phone": "phone", "order_status": "body"},   # {slot: python arg}
    "queued", out_key="queued", condition=flows.has("phone"),
)

app = flows.App(
    root_flow=flow,
    app_display_name="Order Status (OpenAPI)",
    toolsets=[orders_api],
    # Flip to True to demo offline. It is baked into the emitted bodies AND declared
    # as a session variable, so a deployed app can still be flipped either way.
    mock_apis=False,
)

if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  print("errors:  ", errors or "none")
  print("warnings:", warnings or "none")
  flows.build_app(app, "./order_status_app")
  print("emitted -> ./order_status_app")
  print("  toolsets/order_service/order_service.json     (the resource)")
  print("  toolsets/order_service/open_api_toolset/…yaml (the spec)")
  print("  tools/getOrder/…                              (generated from the task)")
  print("  tools/text_tracking_link/…                    (declared with api_tool)")
