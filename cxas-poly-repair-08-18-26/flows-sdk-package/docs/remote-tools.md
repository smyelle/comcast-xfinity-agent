# Remote tools

Declare a contract; let somebody else implement it. A **remote tool** is work the agent
specifies — the values in, the values out — and a service deployed on its own schedule
performs. Every call the agent makes is sub-second (start the job, check on the job), so
the job itself may run for minutes or hours without the 60-second tool ceiling or a held
turn ever entering into it.

```python
reports = flows.openapi_toolset(
    "report_service",
    base_url="https://reports.internal.example.com",
    auth=flows.service_agent_auth(),
    env_scoped=True,
)

build_report = flows.remote_tool(
    "build_report", reports, "buildReport",
    params={"account": str, "since": str},
    outputs={"headline": str, "rows": int},
    description="Crunch the quarter's numbers.",
    timeout=600,
)

app = flows.App(root_flow=f, toolsets=[reports], ...)
f.task("build", "build_report", ["account", "since"], "headline",
       out_key="headline", extra_outputs={"rows": "row_count"})
```

Note the toolset has no `spec=`. That single omission is what makes this page different
from [OpenAPI toolsets](openapi.md), and everything below follows from it.

## Consuming a spec, declaring one

`openapi_toolset(spec=...)` means **you consume somebody's API**. The spec is the source
of truth: it supplies the types, `api_tool`'s `params` merely *rename* your argument
names onto its wire names, and a typo is checked against the parsed document.

Omitting `spec=` means **you declare one**. There is nothing to parse, so the
declaration itself is the spec: `remote_tool`'s `params` carry the **types**, and the
document CES imports is generated from every `remote_tool(...)` that names the toolset.

That inversion is the whole difference, and it is why the values look the same and mean
opposite things:

```python
flows.api_tool("look_up", orders, "getOrder",
               params={"order_id": "orderId"})     # arg -> THEIR wire name

flows.remote_tool("build_report", reports, "buildReport",
                  params={"account": str},          # arg -> ITS type
                  outputs={"headline": str})
```

Mixing the two is refused rather than silently misread:

> remote_tool('build_report'): 'report_service' was built with `spec=`, so it consumes
> an API that already exists and its operations are fixed. Use flows.api_tool() for
> those, or drop `spec=` to declare this service's contract here instead

## Sugar over `api_tool`, not a sibling of it

One `remote_tool(...)` builds **two ordinary `api_tool` wrappers** over the same
toolset:

| generated tool | what it does | who names it |
| --- | --- | --- |
| `build_report` | POSTs the params, returns the job handle | your task |
| `build_report__status` | GETs the handle, returns status and result | the engine |

So the emitted app contains the same `openApiToolset` resource and the same generated
`pythonFunction` wrappers every other API call produces. **There is no new resource
kind** — nothing new lands in the app dir, nothing new to read in the CES console, and
nothing new to debug when a call misbehaves.

The status wrapper is the engine's, not yours. No flow names it, and `RemoteTool`
exposes the two derived names (`status_tool`, `job_slot`) only so the engine can find
them: the handle lands in `<name>__job`, which the author never writes.

## The generated spec

The document the service must implement, two operations per remote tool:

```yaml
paths:
  /buildReport:
    post:
      operationId: buildReport
      requestBody:          # {"account": "...", "since": "..."}
      responses:
        '200':              # {"jobId": "..."}   REQUIRED
  /buildReport/{jobId}:
    get:
      operationId: buildReportStatus
      responses:
        '200':              # {"status": ..., "error_code": ..., "result": {...}}
```

`status` is required and is exactly one of `running`, `done`, `failed`, `timeout`.
`running` is the pending case; the other three are terminal and carry an `error_code`
straight into the task's `on_failure`, which already branches by code. `result` carries
the declared `outputs`, and the status wrapper flattens `result.<key>` to a top-level
key so intake — which maps `outputs` by FLAT key — can read it.

Every generated response carries a `description`, for the reason
[OpenAPI toolsets](openapi.md) documents at length: CES drops a whole toolset whose spec
omits one, *without failing the push*, and the first symptom is a live call that cannot
find its tool.

`servers` is the toolset's `base_url`, or `$env_var` under `env_scoped=True`. Auth is the
toolset's, so a service behind Cloud Run's own IAM wants `flows.service_agent_auth()` and
no Secret Manager wiring at all.

## Wall clock, not turns

`timeout=` is **seconds**, and it is the one place in the engine that counts them.
Everywhere else the unit is turns, because the engine has no clock; a service does, so
the budget is carried to the service on the start call and **the service is
authoritative** — it is what stops the work, and what reports `timeout`.

The consequence worth internalizing: a timeout is only ever *noticed on a turn*, so it
fires at the first tick after the budget rather than exactly on it. A 600-second job on a
20-second inactivity interval is reported somewhere in the 600-620 second window.

## Inactivity ticks are the polling clock

The app must declare one:

```python
app = flows.App(
    root_flow=f,
    app_display_name="Report Desk",
    toolsets=[reports],
    app_settings={"audioProcessingConfig": {"inactivityTimeout": "20s"}},
)
```

Without inactivity ticks there are **no idle turns**. A caller who says nothing produces
no turn, nothing polls the status wrapper, no reassurance line is ever spoken, and the
job's completion is not noticed until the caller happens to speak again. The whole
feature rests on the platform manufacturing turns during silence.

A tick is a turn for the WAIT and not for the conversation, and the engine counts the
two separately. The poll's once-per-turn guard and `awaits.max_turns` are measured on
ticks plus caller turns; a no-match ladder or a steer-back still counts only what the
caller actually said. Silence is a chance for the job, not a strike against the caller.

`flows deploy` writes `audioProcessingConfig.inactivityTimeout` itself, from
`--inactivity-timeout` (default `8s`), **after** the settings merge — so it overwrites
what the app declared. Pass the flag with the value you want, or accept 8s.

## Composing

Nothing about a remote tool is special to the task that fires it:

```python
f.task(flows.task(
    "build", "build_report", ["account", "since"], "headline",
    out_key="headline", extra_outputs={"rows": "row_count"},
    awaits=flows.awaits(
        max_turns=40,
        say="I'm putting that report together now.",
        while_waiting=["Still crunching the numbers.",
                       "Nearly there — thanks for holding on."],
    ),
    on_failure={
        "max_retries": 1,
        "retry_say": {"remote_job_lost": "I lost track of that one. Starting over.",
                      "_default": "That didn't come back. Trying once more."},
        "on_exhaust": {"say": "I can't build that report right now.",
                       "then": {"tool": "transfer_to_human"}},
    },
))
```

- **`awaits`** supplies the reassurance copy for the idle turns the poll rides on.
- **`on_failure`** branches by `error_code`; `max_retries`, `retry_say`, `clear_slots`
  and `on_exhaust.say` all accept a dict keyed by it, with `_default` for the rest. HOW
  MANY times to retry is as much a property of the failure as what to say about it: a
  lost job is worth starting over, a job that ran and failed is not.
- **`flows.parallel(...)`** starts several jobs in one action; each is a sub-second POST,
  so the group's cost is one round trip rather than one per job.
- **`terminal=True`** works, because the finishing turn is an ordinary task completion.

## Error codes

Four codes are stamped by the engine, from the answer in front of it:

| code | what happened |
| --- | --- |
| `remote_bad_handle` | the start call answered without a usable `jobId` |
| `remote_failed` | the job reported `status: "failed"` |
| `remote_timeout` | the job reported `status: "timeout"` — the service stopped the work |
| `remote_contract` | a `done` job left a declared output absent, or `status` was a word the contract does not name |

Every other code is **your service's own**, returned in the status body and passed
through untouched. `timeout` is in that column too: the wall-clock budget is enforced by
the service, because the engine is polling rather than holding a connection, and only the
side running the work can stop it.

```python
on_failure={"max_retries": {"remote_job_lost": 1, "_default": 0}}
```

Which means a code you have not agreed with the service never fires. There is no
`remote_unreachable` and no `remote_forbidden`: a transport failure is not a status
answer, so nothing is there to stamp — it surfaces as an ordinary tool failure, and the
`_default` branch is what catches it. An `on_failure` keyed on a code nobody emits is a
branch that reads as handled and never runs.

`remote_job_lost` is a first-class outcome rather than an edge case, and it is the one
worth writing a line for. The service redeploys on **its** schedule, which is the point
of the feature; a rollout that drops in-flight job state is therefore a thing the agent
must be able to tell apart from "still working". A service that keeps its jobs in a
process-local dict makes this the routine path instead of the rare one.

## Testing without a service

Four shapes, and they exist to answer four different questions.

```python
flows.remote_tool(..., mock={"headline": "Q3 revenue up 4%", "rows": 1240})
flows.remote_tool(..., mock=quarter_report)                     # a code block
flows.remote_tool(..., mock=flows.after_turns(3, quarter_report))
flows.remote_tool(..., mock=flows.remote_error("remote_job_lost"))
```

- A **plain value resolves inline**, exactly as a synchronous tool would. This is
  deliberate: an existing offline oracle should not have to know a tool became remote,
  so nothing it asserts changes.
- A **code block** is run on the poll and returns the same thing the value would. It
  takes no arguments — a poll carries the handle and nothing else, so the parameters the
  job started with are two turns gone — and reads the session instead:

  ```python
  def quarter_report():
    """The job's answer, chosen by whatever scenario this session is driving."""
    if (context.variables or {}).get("scenario") == "empty":
      return {"headline": "No activity this quarter", "rows": 0}
    return {"headline": "Revenue up 4%", "rows": 1240}
  ```

  This is the shape a real agent needs, and static data cannot express it: a scenario
  the session selects through a variable is one mock with N answers, not N mocks. It may
  also return a status answer instead of a result —
  `{"status": "failed", "error_code": "remote_job_lost"}` — with no ambiguity, because
  `status` and `error_code` are refused as output names.

  It has to be a **`def` at module level**. The block is not imported at run time: its
  source is read and INLINED into the emitted tool, which then calls it by name. A
  lambda has no name to call (`<lambda>` is not an identifier, so the tool would not
  parse at all), a class would be constructed rather than called, and a closure loses
  every enclosing name the moment it lands in the sandbox. All three are build errors
  naming the `mock=`; each used to be a deploy, and then a tool that answered nothing.
  Read what you need off `context` rather than closing over it.
- **`after_turns(n, value)`** holds the job open for n turns, which is the only way to
  drive `while_waiting`, `on_timeout` and a group's staggered lines without deploying
  anything. Copy that is only reachable live is copy nobody checks; lines authored,
  validated, emitted and never spoken is a real failure this prevents. `value` is
  anything the plain form takes, a code block included.
- **`remote_error(code)`** fails the job, so an `on_failure` branch is drivable offline
  in the same vocabulary every other tool's failures use.

All four are one emitted tool: `<name>__status_mock`, which the status wrapper calls
instead of the service. It can be read and edited in the CES console like any other, so
what a mocked poll answers is changeable without a rebuild. The start wrapper is mocked
too, with the synthetic handle `mock-<name>` — and that handle is load-bearing rather
than cosmetic. It is how the engine knows a job is mocked, which is how a mock gets the
one thing it cannot work out for itself: the poll for a mocked job carries
`mock-<name>#<turns waited>`, and that is what `after_turns` counts. A handle a real
service issued is passed through untouched.

**A mock is engaged the way every other toolset mock is** — `App(mock_apis=True)`, or
the `mock_apis` session variable, which is what lets one deployed app be driven mocked
by an eval and live by a caller. Declaring `mock=` does not on its own stop the service
being called.

And a variable a code block reads has to be **declared** (`App(variables=[...])`), or
`context.variables` will not carry it. Driven live without the declaration, the `empty`
arm above answered with the busy quarter — silently, which reads exactly like a mock
that ignores the session rather than like a missing declaration.

## Offline checks

Build errors, before anything is pushed:

- `remote_tool` on a toolset built **with** `spec=` (use `api_tool`, or drop the spec)
- a declared toolset that **no** `remote_tool` names — the emitted spec would have no
  operations and CES would drop the toolset at import
- `mocks=` on a declared toolset (put the mock on the tool: `remote_tool(..., mock=...)`)
- two remote tools claiming one `operationId` on the same toolset — they would collapse
  to a single `tools.<toolset>_<operationId>` callable
- empty `params` (a remote tool shares no session state, so everything the job needs has
  to be passed to it) or empty `outputs` (nothing could ever reach a slot)
- a param or output typed as anything but `str`, `int`, `float`, `bool` — the service
  builds from the generated spec, so a type OpenAPI cannot express has nothing to
  generate
- an output named `success`, `error`, `response`, `status` or `error_code`, which the
  wrapper and the status contract set themselves
- a non-positive `timeout`
- a tool name that is not a python identifier — it is emitted verbatim as the generated
  wrapper's `def` entrypoint

## Verified

**Driven end to end, 2026-08-10**, on text and on voice, against the sample service in
`examples/remote_service/` (start, poll, durable Firestore job state, every terminal
status reachable on demand). A four-minute job on each channel: started in under a
second, polled once per turn for a dozen turns, and read out with the service's own
headline and row count. The voice run is the one that matters — real audio, and the
caller says nothing at all after giving the account, so every poll rides a platform
inactivity tick rather than a question. `remote_job_lost` was driven for real by deleting
the job's record mid-call, and the retry it provoked is what proves `max_retries` keyed
by code. Transcripts, the engine's log for each turn, the service's request log, the
Firestore record each spoken number is checked against, and the five defects the drive
found are in [`examples/REMOTE_TOOL_VERIFY.md`](../examples/REMOTE_TOOL_VERIFY.md).

The one number worth carrying: a finished job is noticed on the first poll **after** it
finishes, so the caller hears about it up to one inactivity interval late. Measured at 16
seconds late on the text run and 6 on the voice one, with a 20-second interval — the
spread is where in the window the job happened to land, not variance in the polling.

## See also

- [OpenAPI toolsets](openapi.md) — the same toolset, the other way round: consuming a
  spec somebody else publishes.
- [MCP toolsets](mcp.md) — the third member of the family, where the contract is
  discovered at runtime instead.
