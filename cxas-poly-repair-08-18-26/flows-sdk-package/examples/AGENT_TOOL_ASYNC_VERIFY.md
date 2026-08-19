# Verifying the async agent-as-a-tool wait

## What this confirms, and why it needed an app

ces-probes `134` measured that an `agentTool` defers, and that its placeholder is
`{"response": "pending"}` where a python tool's is `{"result": "pending"}`. It then stopped
and wrote the consequence down as a **prediction**, because it had no engine in the loop:

> `_is_async_pending` recognises a bare `"pending"` string or a dict whose `result` key is
> `"pending"`; a dict keyed `response` matches neither… That last step is read off the
> engine source, not measured here, so it is a prediction: it needs a slot-filling app
> driven against an async `agentTool` to confirm.

This is that app. Two builds of it, identical but for the engine.

## The app

The SDK cannot author an `agentTool` — it emits `remoteAgentTool`, whose async form is
dropped at deploy (`133`). So the flow is authored normally against a python tool and the
emitted tool resource is rewritten into an `agentTool` afterwards, the same
build-then-rewrite a grafted conversion uses. Source: `examples/../.scratch/agent_async_demo.py`
in the branch; the rewrite is fifteen lines at the bottom of it.

The target is a second agent in the same app, instructed to answer with a token
(`HELPER9f21 …`) that can only come from it.

## Driven live — 2026-08-08, `gemini-composite-v1`

**Control — the engine as it is on main:**

```
greet  Parcel desk. What's your question?
> where is my parcel
FAIL: Agent has reached the limit of 10 reasoning loops for the input.
```

Not a soft failure. The placeholder is unrecognised, so the task is never marked in-flight
and the engine re-dispatches it on every pass until the platform kills the turn.

**Fixed — `_is_async_pending` accepting either spelling:**

```
greet  Parcel desk. What's your question?
> where is my parcel
< Let me ask the specialist.              [awaits.say — the wait engaged]
> any news?
< HELPER9f21 the parcel is in Boston.     [the deferred answer, a turn later]
```

`HELPER9f21` exists only inside the other agent, so that is genuinely the sub-agent
answering. The prediction is confirmed, and the observed symptom is worse than predicted:
`134` expected the slot to fill with the literal string `pending`, but with a wait policy
configured the placeholder is falsy under `success_check`, `max_retries` defaults to `0`,
and the task escalates on its **first** fire — offline that shows as `task_exhaust`, live
as the loop cap above.

## The second finding, which the fix does not cover

The first fixed run got as far as the wait and then said **"An error occurred."** on the
completion turn. An agent's reply is `{"response": "<text>"}` with **no `success` key**, so
the default success test reads a good answer as a failure and routes it to `on_failure`.

That one is authoring, not engine — `success_check="response"` on the task, and the
transcript above is the result. It is now in the async tools page, because nothing else
would tell an author before they hit it.

## What is still not covered

* Only a same-app agent was targeted. Cross-app is refused at push (`134`), and no other
  spelling was tried.
* The `while_waiting` ladder and `on_timeout` were declared but never reached — the helper
  answers in one turn. A slow target would exercise them.
* Only the composite model.
