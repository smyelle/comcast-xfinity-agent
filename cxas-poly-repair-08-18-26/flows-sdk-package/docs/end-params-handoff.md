# Native-channel return on `end_session.params`

`flows.end_params_handoff` + `Flow.on_end` let a flow hand structured data back to the
telephony/contact-center platform it runs behind, at the moment the session ends.

## Background

A flows app often runs *behind* a contact-center platform — a "native channel." When the
call ends, the platform expects the app to return data: typically a routing code and/or
custom SIP `X-*` headers that tell the platform where to send the caller next. (FIVE9, for
one, re-emits a returned `xHeaders` map as SIP `X-*` headers.)

The platform reads that return from exactly one place — the **`params` of the `end_session`
tool call**, under a channel-specific key (the *envelope*):

```
end_session(params = { "<ENVELOPE>": { "xHeaders": { ... }, "endSession": true } })
```

Nothing else reaches the platform: not a session variable, not a separate data part.

Two things make this hard to deliver reliably:

1. **`flows.handoff(...)` has the wrong shape.** It emits vendor data as a separate
   `{type: payload}` part beside `end_session`. A native channel ignores that part — it reads
   only `end_session.params`.
2. **The model can drop the return.** On a live model, a spoken closing line lets the model
   end the call itself with a *bare* `end_session` (no `params`), silently dropping whatever
   the app prepared.

## The abstraction

Declare the return channel once, on the flow:

```python
import flows

flow.on_end(flows.end_params_handoff(
    envelope="LIVE_AGENT_HANDOFF",    # the params key the platform reads
    from_state="LIVE_AGENT_HANDOFF",  # the session variable the app stages the return into
))
```

- **`end_params_handoff(*, envelope, from_state)`** declares: "at every terminal end, take the
  value in session variable `from_state` and deliver it as `end_session.params[envelope]`."
- **`Flow.on_end(...)`** attaches it flow-wide, so it applies to *every* way the flow can end —
  no per-exit wiring.

The app still decides *what* to return: some tool computes the value and writes it into the
`from_state` variable. `end_params_handoff` owns only *how and where* it is delivered.

### Division of labor

- Each exit keeps owning its own spoken close and its `end_session` reason. Unchanged.
- `on_end` owns only the return channel — the `params` it folds onto whatever `end_session`
  the flow emits. One flow-level declaration coexists with many different exits; nothing has to
  reconcile a single reason across them.

## How the framework delivers it

Delivery is guaranteed by the framework, never left to the model:

1. The flow's `on_end` config is surfaced to the engine.
2. On a terminal turn, if the `from_state` variable holds a real return (it has `xHeaders` or
   `endSession`), the framework **takes over the turn**: it speaks the close and emits
   `end_session` with the return folded into `params`. The model never runs, so it cannot end
   the call without the return.
3. A flow without `on_end`, or with nothing staged, is unaffected — behavior is identical to
   before the feature.

## Config shape

`Flow.on_end(...)` adds one top-level key to the flow config:

```json
{ "on_end": { "delivery": "end_params",
              "envelope": "LIVE_AGENT_HANDOFF",
              "from_state": "LIVE_AGENT_HANDOFF" } }
```

`delivery` names the delivery shape (only `"end_params"` exists today, but the field leaves
room for others). The validator requires `delivery == "end_params"` with non-empty string
`envelope` and `from_state`, and rejects any unknown `delivery`. `end_params_handoff` and
`on_end` are additive — existing `handoff()` users are untouched.

## Tests

- Unit: config shape (`end_params_handoff`, `Flow.on_end`), validator accept/reject, and the
  resolve/fold helpers.
- Integration: the real terminal callback, given a staged return, takes over the turn and
  emits `[close, end_session(params)]`.

See `tests/test_end_session_params.py`.
