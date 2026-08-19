# A2A — calling other agents

A2A (Agent2Agent) lets an agent hand work to an agent it does not own: an ADK agent on
Cloud Run or Agent Engine, a third-party SaaS agent, another CXAS app. CES models
each one as a **tool** carrying an A2A [agent card], so `flows` declares them the way it
declares anything else.

```python
billing = flows.remote_agent(
    "billing-agent",
    description="Answers billing and invoice questions.",
    url="https://billing.example.com/a2a/v1",
)

app = flows.App(root_flow=flow, remote_agents=[billing], ...)
```

Three fields is the whole minimum. `version` (`1.0.0`), `protocolBinding`
(`HTTP+JSON`) and `protocolVersion` (`1.0`) are defaulted, and a single skill is
derived from the name + description. `description` has no default on purpose: it is the
model's primary signal for whether to call the agent, and nothing else can stand in for
it. Name skills explicitly when the agent does several distinct things — they are the
per-capability half of that signal:

```python
flows.remote_agent(
    "ticketing-agent",
    description="Creates and updates support cases.",
    url="https://tickets.example.com/a2a/v1",
    skills=[
        flows.agent_skill("create_case", name="Create case",
                          description="Open a case from a description of the problem."),
        flows.agent_skill("update_case", name="Update case",
                          description="Comment on or change the state of a case."),
    ],
)
```

That emits one **body-less** tool resource — `tools/billing-agent/billing-agent.json`
and nothing else — and scopes it onto the agent. There is no python to write: CES's
`remoteAgentTool` is a `tool_type` oneof sibling of `pythonFunction`, and the platform,
not the sandbox, makes the call.

Runnable demo: [`examples/a2a_remote_agents.py`](../examples/a2a_remote_agents.py).

### Skills are what the model routes on

Worth knowing before deciding how much care to put into them, because it is measurable
rather than a matter of taste.

Two remote agents were deployed with deliberately symmetric, uninformative names and
*identical* descriptions (`"A remote agent."`), differing only in their skill — one
`get_weather`, one `track_parcel` — with an instruction that named neither. The model
picked correctly both times:

```
"what is the weather in Boston?"                 -> remote-agent-alpha   (weather skill)
"where is my parcel, tracking number 12345678?"  -> remote-agent-beta    (parcel skill)
```

Skill text was the only thing that could have separated them, so it reaches the model
and drives selection. That is the argument for naming skills on a multi-capability
agent: one blurred skill saying "creates and updates cases" gives the model a single
fuzzy target where `create_case` and `update_case` give it two sharp ones.

A caveat that comes with it: CES stores the card **inline**, author-written, rather than
fetching it from the remote agent's well-known endpoint. Nothing reconciles your skill
list against what the agent can actually do — if it drifts, the model routes confidently
on a card that is quietly wrong. The derived skill carries that risk in its mildest
form, since it can only ever restate your own description.

## The two ways to use one

| | when | how |
|---|---|---|
| **Model-callable** | the remote agent *is* the answer, and the model should decide when to reach for it | declare it in `remote_agents` and point at it from the instruction with `{@TOOL: name}` |
| **Slot-filling** | the remote agent is one step of a transaction your flow is driving | `flows.delegate(...)` — the flow collects, delegates, and continues with the reply in a slot |

The first is the reference pattern and needs nothing beyond the declaration. The second
is `delegate`, below.

### Why the second one is possible at all

A flow does not *ask* the model to call a remote agent. The engine returns a
`function_call` part from its `before_model` callback and the platform dispatches it, with
the model bypassed entirely — which is what makes a delegation a step in a transaction
rather than a suggestion the model may decline.

Worth stating because a remote agent tool has no python body, and that dispatch path had
only ever been demonstrated on tools that do. Measured live in `ces-probes` `132`: an
engine-injected call to a remote agent dispatches on the **first** fire and the reply
arrives as an ordinary tool response the next pass reads.

It also settles which of the two agent hand-offs a flow can drive. A transfer to a
sub-agent is accepted and reads back as set, and then nothing happens — no second
invocation, no hand-off (`ces-probes` `59`, `60`). So calling an agent as a tool is the
only route the engine can take deterministically, and that is why `delegate` is built on
A2A rather than on a sub-agent.

## The wire contract

Worth knowing before wiring one into a flow, because it does not look like an ordinary
tool.

**Input.** The tool takes `task` — a natural-language request string — and an optional
`contextId`. It does *not* take the remote skill's own parameters; the remote agent
parses the request itself. So a task calling one passes its slots through the dict form
of `inputs`, mapping a slot to the platform's parameter name:

```python
flows.task("ask", "billing-agent", {"question": "task"}, "reply_envelope", ...)
```

**Output.** The reply is the A2A `SendMessageResponse` oneof, which arrives as one of
two shapes:

```jsonc
{"message": {"parts": [{"text": "Your balance is $42."}], "contextId": "..."}}   // answered
{"task":    {"id": "...", "status": {"state": "TASK_STATE_SUBMITTED"}, ...}}     // accepted
```

A call that cannot be made at all comes back as
`{"status": "error", "error": "Error fetching from URL…"}` — the same envelope a python
tool's failure uses, and measured rather than assumed (`ces-probes` `135`). It carries no
`message` key, so it routes to the call task's `on_failure`, which is what you want: it is
a failure, not an answer.

Two things about that failure are worth knowing before you write the ladder:

* **It is fast and it is not silent.** A deleted target answers in ~370 ms and an
  unresolvable host in ~135 ms, both as a real tool response. Nothing hangs.
* **You cannot branch on the reason.** Both lead with `Error fetching from URL`; only a
  trailing `- Requested entity was not found.` distinguishes a target that is gone from one
  that cannot be reached — operationally opposite problems (fix the config vs retry later).
  Write one handler and say something honest.

The placeholder `{"result": "pending"}` is listed in the platform's contract but a remote
agent cannot produce it; see the `awaits` section below for why.

## Why a bare `task()` is a trap

Intake decides a task worked by reading one flat key —
`success = bool(response_data.get(success_check))` — and maps `outputs` by flat
top-level key too (`slot_intake._intake_executor`).

An A2A reply has no `success` key in it at all. So a task left on the default reads as
**failed on every fire**, and because `on_failure.max_retries` defaults to zero, the
flow escalates the first time it runs with nothing actually wrong. It is the same silent
failure `awaits` exists to prevent for asynchronous python tools.

`outputs` has the matching problem: the reply text lives at `message.parts[].text`,
nested inside the reply, where a flat map cannot reach it.

`flows.validate_app` errors on both shapes rather than letting them deploy:

```
task 'ask' fires remote agent 'billing-agent' with success_check='success', but an A2A
reply carries no such key — it is one of 'message', 'task'. Intake would read every call
as failed and escalate on the first fire. Use flows.delegate(), or set success_check to
one of them
```

## `delegate` — the slot-filling path

```python
ask = flows.delegate(
    "ask_billing",
    billing,
    request_slot="question",      # sent as the tool's `task` parameter
    reply_slot="billing_reply",   # filled with the reply TEXT; defaults to <name>_reply
    then_say="{billing_reply}",
    terminal=True,
    awaits=flows.awaits(max_turns=4, say="Let me check that.",
                        on_timeout={"say": "I can't reach billing right now."}),
)

flow.add(flows.user_slot("question", ask="What's your billing question?"), *ask.slots)
flow.task(*ask.tasks)
```

Splicing those tasks also DECLARES the agent, so a delegation needs one declaration and
not two. Add it to `App(remote_agents=[...])` as well only to ALSO make it model-callable
from the instruction.

Two tasks, because the platform's answer is an envelope and a slot wants text:

1. `ask_billing` fires the remote agent and parks the raw reply in
   `billing_reply_envelope`.
2. `ask_billing_read` runs a generated, sandbox-safe unwrap tool over that reply and
   fills `billing_reply` with the text.

`expect=` says which kind of reply to consume, and it must be exactly one:
intake requires *every* key in `outputs` to be present, so mapping both would demand a
response carrying both at once, which a oneof never is.

The default is `message`, the reply carrying a finished answer. An agent that replies with
the `task` reply has accepted the work but not done it — under the default that is a miss,
and it routes to `on_failure` rather than filling a slot with a receipt. Pass
`expect="task"` for an agent that answers with COMPLETED `task` replies; the unwrap then reads
the status message or the artifacts, and a task still in flight yields no text so the
read fails rather than filling the slot with `""`.

### What `awaits` actually covers

Less than the name suggests, and it is worth being exact. The engine enters a wait
**only** for CES's own `{"result": "pending"}` placeholder
(`slot_filling_engine._is_async_pending`) — the deferral CES performs when it will not
answer the call this turn. It does **not** engage for the `task` reply, which is a real
response judged by `success_check` like any other.

So `awaits` means "CES deferred the call", not "the remote agent is thinking". Setting
`success_check="task"` *and* `awaits` does not buy a wait: the task succeeds
immediately on the receipt, because the `task` key is present. `validate_app` warns
about exactly that combination.

The unwrap tool reads both kinds (a `message`'s parts, a completed `task`'s status
message and artifacts) and reports `success: False` for an envelope with no text in it,
so an unfinished task routes to the failure ladder instead of filling a slot with `""`.

### `awaits` cannot currently fire for a remote agent

Stronger than a caveat, and measured: **a remote agent call is always synchronous, because
the platform will not deploy an asynchronous one.**

`{"result": "pending"}` is what a tool returns when it is declared
`executionType: ASYNCHRONOUS` (`ces-probes` `24`). Declare that on a remote agent tool and
the resource is **silently discarded at deploy** — the push reports success, the tool is
absent from the app afterwards, and the referring agent's tool list has been rewritten
without it (`ces-probes` `133`, where two synchronous cards on the same target both
survive, so the flag is the cause).

So today every delegation blocks its turn for the round trip, there is no concurrent
fan-out across remote agents, and `awaits` is unreachable by this route. It is written
against an envelope A2A cannot produce.

**And the obvious fallback does not work either.** A tool's `timeout` is accepted on a
remote agent tool, survives the push, and reads back off the deployed resource — and is
then ignored. Measured in `ces-probes` `135`: a one-second budget let the call answer
normally at 3.7 s, while a python tool carrying the *same* budget in the same app and run
was killed at 1.2 s and said so. Accepted-and-ignored is worse than rejected, because the
config reads back as though the call were bounded.

Between the two, **a delegation cannot be bounded at all** — not asynchronous, not timed
out. The only real control is choosing a remote agent that answers promptly, which is worth
deciding before one goes in front of a caller on a phone line rather than after.

A slow agent is not unbounded — the platform cuts an agent call off at around 30 seconds —
but do not plan around that as a safety net. Measured on the in-app flavour
(`ces-probes` `138`), the cut is returned as an ordinary `{"response": "<text>"}` carrying
the platform's crash line rather than an error, so a task reading it sees a success and fills
its slot with an apology. Whether A2A behaves identically at that duration is untested.

Raising the budget does not move the wall — a `120s` timeout cuts at the same ~30 s as no
timeout at all (`140`) — and deferring does not either: an asynchronous agent call is cut
identically and its completion is reported as `completed with response` while carrying the
apology (`139`).

### Making a delegation detectable

Since the envelope cannot be trusted, make the **content** carry the proof. Mint a value per
call, send it in the request, and have the agent you own echo it back:

```python
nonce = f"REQ-{uuid4().hex[:8]}"      # per call, not per app
ask = flows.delegate("ask_billing", billing,
                     request_slot="question", reply_slot="billing_reply", ...)
```

then reject any reply that does not contain it. The platform's apology cannot contain a value
you invented a moment earlier, which is why this beats matching the crash line — that string
is in no contract and can be reworded or localized. Measured working in `ces-probes` `139`:
present on a healthy call, absent on a cut one.

The natural home for the check is the unwrap tool `delegate()` already emits, which is
ordinary sandbox code: fail it there and the miss flows into `on_failure` like any other.
And whatever you do, do not let an unvalidated delegated reply reach `then_say` — that is
how a caller ends up hearing the platform apologize in the middle of your agent's sentence.

For work that genuinely needs more than ~30 seconds, no configuration helps: shard it across
several shorter calls (`contextId` continues the same A2A conversation), or move the long
part into a python tool, where `timeout` is real.

The sibling tool type does not share the limitation: an `agentTool` — which calls an agent
in the **same app** by name, takes `request` rather than `task`, and replies
`{"response": "<text>"}` — deploys asynchronously, defers, and delivers its answer in a
`<context>` completion. `flows` does not model it, and its placeholder is
`{"response": "pending"}`, which is *not* the shape `_is_async_pending` recognises, so
wiring one into a task today would read the deferral as a finished answer (`ces-probes`
`134`).

## Calling another CXAS app

A deployed CES app is an A2A endpoint with no extra configuration — that is the
**inbound** half of the protocol, and it is why this is sugar rather than a second
mechanism:

```python
peer = flows.ces_agent(
    "weather-agent-cxas",
    description="Answers weather questions for a city.",
    app_id="1c972214-60f0-484c-a3a8-a7c342abe9c9",
)
```

`project` and `location` default to the **App's**, since two apps deployed side by side
is the common case. Whichever you omit is filled in at build — not at declaration, both
because the App does not exist yet and because deferring lets one declaration be built
into two projects. Pass them to reach an app elsewhere, which also needs
`ces.sessions.runSession` on that app:

```python
flows.ces_agent("peer", description="...", app_id="...",
                project="google.com:other", location="eu")
```

A project *without* a location still defers: silently defaulting it to `us` would point
an app deployed in `eu` at the wrong region.

To let *others* call your app, deploy it and hand them
`https://ces.googleapis.com/v1/projects/<project>/locations/<location>/apps/<app_id>`.
Nothing in the emitted app changes.

## Scoping

Where a remote agent is callable from follows the same per-agent scoping as every other
tool in this framework — "tools everywhere" is what makes a model call things
unprompted.

* **Single-agent** — `App(remote_agents=[...])` scopes onto the root agent.
* **Multi-agent** — `App(remote_agents=[...])` scopes onto the **host router** (the
  agent talking to the caller between transfers). Put one on a specialist instead with
  `Agent(remote_agents=[...])`, which keeps it off the router and its siblings.

Either way each resource is emitted once, however many agents reference it. Declaring
the same `RemoteAgent` twice is fine; declaring two *different* agents under one name is
a build error, since only one resource can win.

## Reference

| | |
|---|---|
| `flows.remote_agent(name, *, description, url, ...)` | declare a remote A2A agent |
| `flows.ces_agent(name, *, description, app_id, project=None, location=None, ...)` | the same, for a CES app |
| `flows.agent_skill(id, *, name=None, description=None, tags=(), ...)` | one skill on the card (`name`/`description` default to the id) |
| `flows.delegate(name, agent, *, request_slot, reply_slot=None, ...)` | the slot-filling pair |

What is left required is checked at construction — a non-empty `description`, and a
`url` that is absolute HTTPS. A card CES rejects fails a deploy that has *already*
overwritten the target app, which is a much worse place to find out.

[agent card]: https://a2a-protocol.org/dev/specification/#441-agentcard
