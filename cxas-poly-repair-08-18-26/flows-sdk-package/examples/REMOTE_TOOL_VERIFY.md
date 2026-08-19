# Verifying a remote tool

## What this confirms, and why it needed a service

Everything about remote tools is offline-green with `mock=flows.after_turns(...)`: the
wait engages, the ladder drains, the outputs land. None of that touches HTTP, an agent's
tool list, the platform's type check on a dispatch, or the turn a SILENT caller produces
— which is where all five of the defects below were, and every one of them was silent.
The app built clean, pushed clean, answered every turn, and delivered a report that did
not exist.

So this is the same example driven against a service that really does take four minutes:

* the app — `examples/remote_tool.py`, pushed to
  `projects/ces-deployment-dev/locations/us/apps/e858211f-e466-4161-bc62-8de33d0f3c99`
* the service — `examples/remote_service/`, on Cloud Run at
  `remote-report-demo-555355609568.us-central1.run.app`, OIDC-only, job state in
  Firestore
* the job — `duration_seconds=240`, so the wait is real rather than a race the backend
  happens to win

Three witnesses per run, because the transcript alone cannot tell a delivered report
from an invented one: the caller's side, the engine's own `sm["_log"]`, and the
service's record of the job in Firestore. A run counts as passed only when
`remote_landed` is in the log, the task's outputs carry the service's values, and the
spoken line quotes them.

## Driven live — 2026-08-10, `gemini-composite-v1`, text

Session `698f95ca-0f2f-4091-9933-07134f33a587`, job
`558514282aa14c42889537dd7880bb76`, one caller turn every 21 seconds.

```
t=  2.0 > I need the Q1 numbers
        < Which account would you like me to run the report for?
t=  6.5 > A-1042
        < Starting that now. It usually takes a few minutes.
t= 27.9 > any luck?
        < Still crunching the numbers.
t= 49.5 > how about now?
        < Not long now, thanks for holding.
t= 70.7 > still going?
        <
   …  (six more turns, each answered with silence, each carrying a poll) …
t=261.4 > how's it looking?
        < Here it is: Report for account A-1042 since 2026-01-01, across 2123 rows.
t=283.3 > done yet?
        < The report for account A-1042 has already been generated and delivered
          (2,123 rows). Is there anything else you need?
```

The blank turns are the wait after its two `while_waiting` lines are spent: the ladder
drains rather than cycles, so the hold goes silent and the poll keeps going out
underneath it.

The engine's account of the same call:

```
progress         {"task+": {"build_report__job": "558514282aa14c42889537dd7880bb76"}}
remote_poll      {"tasks": ["BuildReport"], "turn": 2}
remote_await_say {"task": "BuildReport"}
remote_poll      {"tasks": ["BuildReport"], "turn": 3}
async_await_line {"task": "BuildReport", "n": 1, "of": 2}
remote_poll      {"tasks": ["BuildReport"], "turn": 4}
async_await_line {"task": "BuildReport", "n": 2, "of": 2}
remote_poll      {"tasks": ["BuildReport"], "turn": 5}
async_await_idle_hold {"tasks": ["BuildReport"], "spoken": false}
…
remote_poll      {"tasks": ["BuildReport"], "turn": 14}
task_completed   {"task": "BuildReport", "success": true, "tool": "build_report__status"}
progress         {"task+": {"rows": 2123, "headline": "Report for account A-1042 since 2026-01-01"}}
async_await_resolved {"task": "BuildReport"}
remote_landed    {"task": "BuildReport", "job": "558514282a…", "turns": 12}
task_completed   {"task": "Deliver", "success": true, "tool": "deliver_report"}
```

The service's, one request per turn:

```
21:14:20 job=558514282aa14c42889537dd7880bb76 started account=A-1042 duration=240.0s deadline=780.0s
21:14:20 "POST /buildReport HTTP/1.1" 200 OK
21:14:21 "GET  /buildReport/558514282aa14c42889537dd7880bb76" 200
   …  (21:14:43, 21:15:04, 21:15:25, 21:15:47, 21:16:08, 21:16:29, 21:16:50, 21:17:11,
       21:17:32, 21:17:54, 21:18:15) …
21:18:20 job=558514282aa14c42889537dd7880bb76 finished as done (landed=True)
21:18:36 "GET  /buildReport/558514282aa14c42889537dd7880bb76" 200
```

And its record of the job, read straight out of Firestore afterwards:

```
558514282aa14c42889537dd7880bb76 done
    {'headline': 'Report for account A-1042 since 2026-01-01', 'rows': '2123'}
```

Which is the point of quoting all three. `2123` is a number only the service computed;
it reached `rows` through `build_report__status` and was read out by the `Deliver`
task's `then_say`. The job landed at `21:18:20` and was spoken at `21:18:36`: a finished
job is found on the first poll **after** it finishes, never at the instant it does.

## The same call over voice, with a caller who says nothing

The channel the feature exists for, and the one that was broken until the run below.
Driven over real audio: the caller says two things and then holds the line in silence
for five minutes, so every turn after the account is an inactivity tick the platform
manufactures.

Session `339e4ca7-d6ea-4f18-bed2-9b87d904042b`, job
`98f0f6f1b1da46bf93b1504d990e07ca`, `inactivityTimeout: 20s`.

```
t=  5.8 < Which account would you like me to run the report for?
          [the caller says "account A 1 0 4 2", and then nothing at all]
t= 18.1 < Starting that now. It usually takes a few minutes.
t= 43.2 < Still crunching the numbers.
t= 67.4 < Not long now, thanks for holding.
          [three and a half minutes of silence, twelve polls]
t=264.8 < Here it is: Report for account A1042 since 2026-01-01, across 7572 rows.
t=297.7 < Is there anything else I can help you with today?
t=321.8 < Are you still there? Let me know if you need help with anything else.
t=346.7 < Since I haven't heard from you, I'll go ahead and end the call. Have a
          great day!
```

The engine's log for it, with the platform's own silence marker left in to show that
nobody spoke on any of those turns:

```
progress              {"task+": {"build_report__job": "98f0f6f1b1da46bf93b1504d990e07ca"}}
remote_poll           {"tasks": ["BuildReport"], "turn": 2}
remote_await_say      {"task": "BuildReport"}
remote_poll           {"tasks": ["BuildReport"], "turn": 3}
async_await_line      {"task": "BuildReport", "n": 1, "of": 2}
remote_poll           {"tasks": ["BuildReport"], "turn": 4}
async_await_line      {"task": "BuildReport", "n": 2, "of": 2}
remote_poll           {"tasks": ["BuildReport"], "turn": 5}
no_input_silent_tick  {}
remote_poll           {"tasks": ["BuildReport"], "turn": 6}
no_input_silent_tick  {}
…                     (turns 7 through 12, one poll and one tick each)
remote_poll           {"tasks": ["BuildReport"], "turn": 13}
task_completed        {"task": "BuildReport", "success": true, "tool": "build_report__status"}
progress              {"task+": {"rows": 7572, "headline": "Report for account A1042 since 2026-01-01"}}
remote_landed         {"task": "BuildReport", "job": "98f0f6f1…", "turns": 11}
task_completed        {"task": "Deliver", "success": true, "tool": "deliver_report"}
```

The service saw twelve requests about 22 seconds apart, and Firestore agrees with the
line the caller heard:

```
21:19:55 job=98f0f6f1b1da46bf93b1504d990e07ca started account=A1042 duration=240.0s
21:19:55 "POST /buildReport" 200
21:19:55 "GET  /buildReport/98f0f6f1b1da46bf93b1504d990e07ca" 200
   …  (21:20:21, 21:20:45, 21:21:09, 21:21:30, 21:21:52, 21:22:13, 21:22:35,
       21:22:56, 21:23:18, 21:23:40) …
21:23:55 job=98f0f6f1b1da46bf93b1504d990e07ca finished as done (landed=True)
21:24:01 "GET  /buildReport/98f0f6f1b1da46bf93b1504d990e07ca" 200

98f0f6f1b1da46bf93b1504d990e07ca done
    {'headline': 'Report for account A1042 since 2026-01-01', 'rows': '7572'}
```

`A1042` rather than `A-1042` is the caller's synthesized speech being transcribed
without the hyphen, and it is worth leaving in: the account the agent collected is the
account the service reported on, which is one more thing a made-up report would not get
right.

The last three lines are the ordinary silence ladder taking over once the flow is
finished — the remote wait no longer owns the turn, so a caller who stays silent is
reprompted and then released. That is the behaviour every other completed flow has.

## `remote_job_lost`, driven rather than described

The failure the feature's premise implies: the service redeploys on its own schedule and
an in-flight job goes with it. Provoked by starting a real job and deleting the service's
record of it mid-call.

```
> I need the Q1 numbers
< Which account would you like me to run the report for?
> A-1042
< Starting that now. It usually takes a few minutes.
   [the service's record of job 26e20c5cecde4848ac0871a5f5415abb is deleted]
> any luck?
< Let me start that over.  Starting that now. It usually takes a few minutes.
> how about now?
< Still crunching the numbers.
> still going?
< Not long now, thanks for holding.
```

```
task_completed    {"task": "BuildReport", "success": false, "tool": "build_report__status"}
remote_landed     {"task": "BuildReport", "job": "26e20c5c…", "turns": 1}
task              {"name": "BuildReport", "ok": false}
task_retry_refire {"name": "BuildReport", "tool": "build_report", "attempt": 1, "max_retries": 2}
```

with

```json
{"success": false, "status": "failed", "error_code": "remote_job_lost",
 "error": "missing from response: headline, rows"}
```

`max_retries` was authored as `{"remote_job_lost": 2, "_default": 0}`, and the `2` in
that log line is the code being resolved: a `remote_failed` on the same task would have
got `0` and no retry. The retry started a genuinely new job
(`6d74461b32004eee9c858ac3ed0481a1`, which Firestore later shows as `done`) and opened a
fresh wait, which is why the caller hears the retry line and the opening line together.

## The five defects the drive found

Every one of them offline-invisible, and the first two fatal.

**1. A dict `max_retries` crashed the engine.** `_handle_post_executor` read it with a
plain `on_failure.get("max_retries", 0)` and then compared `retries >= max_retries`,
which against a dict raises `TypeError: '>=' not supported between instances of 'int' and
'dict'`. The raise is inside the engine TOOL, so CES answered the call with no `result`,
`before_model` raised `KeyError('result')` on the unwrap, and the turn died — twice per
turn, with the caller hearing "I wasn't able to complete that request" and the model
inventing a delivered report a turn later. Resolved by code now, like every other rung of
the ladder.

Worth recording for the next one of these: the `KeyError('result')` in `before_model` is
never the bug. It is the shape *any* raise inside a framework tool takes, and it hides
the real traceback completely. The fastest way to it was a temporary `except` at the
engine's entry point that appended `traceback.format_exc()` to `sm["_log"]`, which comes
back in the trace; four minutes of that beat an hour of reading the remote path.

**2. The status wrapper was never on the agent.** No flow names it — the engine owns the
poll — so `scoped_agent_tools` never listed it, and **CES drops a dispatch to a tool the
agent does not list without an error of any kind**. `remote_poll` logged a poll on every
turn for six minutes; not one became an HTTP request. The job finished and the agent
never found out. This is the same failure `69` recorded for a fan-out's peek/watch tools,
and it now has the same one-line answer.

**3. The platform refused the start call on a typed param.** A generated wrapper takes
every argument as `str` and casts inside its body — but CES type-checks a `function_call`
against the signature *before* the body runs:

```
Invalid value for parameter `duration_seconds` in the tool call in Python tool
build_report: Expected `String`, received `kotlin.Double` (240.0).
```

`240.0`, not `240`, because a config default crosses CES as a protobuf Struct where every
number is a double. So the engine now renders a remote start call's arguments the way its
wrapper declares them, integral floats included. Scoped to the remote registry: an
ordinary python tool declares `rows: int` and stringifying it would break the call.

**4. A finished job reported itself as a failure.** The generated wrapper computes
success as "every declared output came back", which is right for an ordinary call and
wrong for a status poll: `error_code` is absent on a healthy job, so a job that had just
succeeded answered `success: false, error: "missing from response: error_code"` and went
down its `on_failure` ladder with the report sitting in the payload. A status answer's
verdict is its `status` field and nothing else, and intake now says so — including
`remote_contract` for a `done` that arrives without the outputs it declared, and for a
status word the contract does not name.

**5. On voice, the job was polled once for the whole call.** The one only the
silent-caller run above could find, and the most instructive of the five, because the
transcript said the opposite. The poll is guarded to once per turn — an unguarded one
re-dispatches on every reasoning pass and burns the turn to the ten-loop cap — and the
guard counted `_turn_n`, which is the number of times the CALLER HAS SPOKEN. A caller
who says nothing produces inactivity ticks and no utterances, so `_turn_n` froze on the
turn the job started and the guard suppressed every poll after it.

What made it hide: `_async_idle_line` is not turn-guarded, so the reassurance ladder went
on draining a line per tick. The call sounded exactly like a call that was polling —
"Starting that now", "Still crunching the numbers", "Not long now" — and then went quiet
for the remaining four minutes and delivered nothing. The service's request log is what
settled it: **one** `GET` for the entire call, against thirteen for the same job driven
over text.

The fix is a second counter. `_wait_clock` is caller turns PLUS inactivity ticks, and it
is what every wait now stamps its mark with and measures against: the poll guard, the
`_awaiting_async` marks in both the engine and intake, and `_sweep_async_timeouts`. It
equals `_turn_n` wherever there are no ticks, which is every text call and every offline
oracle, so nothing else moves. A second thing fell out of it: `awaits.max_turns` was
measured on the same frozen counter, so the one backstop between a wedged backend and a
wedged call could never fire on a silent voice call either.

Two smaller ones, from the same drive: `awaits.say` was never spoken at all (a remote
wait never takes the pending turn `_async_hold` speaks on, so the first thing said about
a brand-new job was "Still crunching the numbers"), and `remote_landed` was only logged
for a remote task with no wait policy — the one shape nobody writes.

## One instrument correction

"A voice session drops at about 120 seconds", believed for most of a day, is not true of
the platform. `_BIDI_RUN_TIMEOUT_S = 120` is a constant in the vendored
`cxas_scrapi.core.sessions` client, and what it produces is
`WARNING Bidi session exceeded 120s without completing; forcing close` — the DRIVER
hanging up, not CES. The voice run above raised it and ran a 360-second call with a
240-second job inside it, with no platform-side interruption of any kind. Anything
measured against that 120s wall needs re-measuring.

## What is still not covered

* `remote_timeout` and `remote_failed` were not driven. The service can produce both on
  demand (`fail` and `deadline_seconds` on the POST body) but the declared contract has
  no parameter for either, so provoking them means a variant app rather than a turn.
* Polling out — `awaits.max_turns` running out with the job still `running` — needs a job
  longer than 40 polls. It carries no `error_code` at all: that is an await timeout, so
  `on_timeout` handles it and `on_failure` never sees it. `max_turns` itself is now
  reachable on a silent call (defect 5), pinned by a unit test rather than a live drive.
* Only one job at a time. `_remote_turn` polls every outstanding handle in ONE dispatch
  and that path is offline-covered, but no live call has ever had two jobs in flight.
* Only `gemini-composite-v1`. Both channels are now driven the way a caller uses them —
  text by typing, voice over real audio with genuine platform inactivity ticks — but
  nothing here has been run against `gemini-3.1-flash-live`, and CES findings do not
  transfer between the two.
* No call has been driven past about six minutes, so nothing is known about a job that
  outlives whatever the platform's own session ceiling turns out to be.
