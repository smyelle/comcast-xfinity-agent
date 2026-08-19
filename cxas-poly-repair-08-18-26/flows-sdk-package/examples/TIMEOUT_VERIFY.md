# Verifying the timeout work against live CES

Offline proves nothing here, and unusually little. `validate_app` and the engine simulator
run every tool body instantly, so a body that sleeps for an hour validates clean, builds
clean and deploys clean — and no timeout is ever reached, which means **no reason branch is
ever chosen offline**. Every line in `timeout_failure_ladder` that this change exists for is
unreachable until the app is on the platform.

| check | what it fakes |
| --- | --- |
| `validate_app` | the clock: no body is ever killed |
| the engine simulator | the same, plus the platform's error payloads |
| `test_examples.py` | that the app builds, not that any branch is taken |

## Prerequisites

```
export W=/path/to/worktree
export PY=$W/.venv/bin/python          # the repo venv; a worktree venv lacks fastapi
export PATH=$W/.venv/bin:$PATH         # the probe harness shells out to `cxas`
export GOOGLE_CLOUD_PROJECT=ces-deployment-dev
```

## 1. Build

```
cd $W/packages/flows
PYTHONPATH=src python -m examples.timeout_failure_ladder
```

Expect `validate: 0 errors, 0 warnings` and a `./timeout_failure_ladder_app` directory.
Preflight the emitted app before deploying — the two traps below are both invisible in the
source:

```
grep -n "^def set_policy_number" timeout_failure_ladder_app/tools/set_policy_number/python_function/python_code.py
grep -o '"timeout": "[0-9]*s"' timeout_failure_ladder_app/tools/*/*.json
```

The first must match the directory name. The second must show `20s` on every declared tool.

## 2. Stage and drive

```
rsync -a --delete --exclude __pycache__ \
  $W/packages/flows/timeout_failure_ladder_app/ \
  $W/ces-probes/probes/demo-timeout-failure-ladder/
cd $W/ces-probes && $PY harness.py demo-timeout-failure-ladder
```

The staged dir is **not committed**. An emitted app vendors the whole blessed framework —
about 13,000 lines of machine-generated copy — so staging it is a step you run, not an
artifact anyone reviews. Write the `RUNS.json` below into the staged dir after the rsync.

`RUNS.json` beside the staged app drives seven independent sessions, one per branch. The
caller's digits choose the failure: the FIRST digit picks how the setter fails, the SECOND
how the task's backend fails. A tool cannot remember which attempt it is on — a body that
does not return successfully leaves nothing behind — so the input is the only way to steer
it, and that is what makes each branch reachable on demand.

## What a PASS looks like

Nine distinct lines, one per (surface, reason). Two lines that happen to differ are not
enough: the `_default` arm and the two coded arms must be shown taking *different* paths for
*different* reasons, or the keyed dispatch is unproven.

| surface | reason | must speak |
| --- | --- | --- |
| slot `validation.errors` | `timeout` | the directory-is-slow line |
| slot `validation.errors` | `tool_crash` | the directory-refused line |
| slot `validation.errors` | `_default` | the generic re-ask |
| task `retry_say` | `timeout` | the one-more-go line |
| task `retry_say` | `tool_crash` | the not-a-waiting-problem line |
| task `retry_say` | `_default` | the didn't-come-back line |
| task `on_exhaust.say` | `timeout` | the call-you-back line |
| task `on_exhaust.say` | `tool_crash` | the broken-rather-than-busy line |
| task `on_exhaust.say` | `_default` | the can't-reach-it line |

If the three slot arms all speak the same line, the normalization is not reaching the setter
path — a code defect, not a documentation gap.

## Driven live — 2026-08-07

`python harness.py demo-timeout-failure-ladder`, app
`projects/555355609568/locations/us/apps/3aaa8e9d-b3a1-4aa7-966b-e1285105a5c3`:

```
-- run slot-timeout
  Our policy directory is slow right now. Read me the number again and I'll retry it.
-- run slot-crash
  The directory refused that lookup. Let's try the number once more.
-- run slot-default
  I didn't get that one. What's the policy number?
-- run task-timeout
  That check is taking longer than it should. Let me give it one more go.
  Our assessment system is running slowly today, so I'll have someone call you back
  with the result.
-- run task-crash
  The assessment engine refused that outright, which is not a waiting problem. Trying
  once more anyway.
  The assessment engine is broken rather than busy, so waiting will not help. Let me
  get you to someone.
-- run task-default
  That didn't come back. Let me try once more.
  I can't reach our assessment system right now.
-- run happy-path
  That's everything on the policy: no open issues on the policy.
```

**PASS on all nine.** Every reason reached its own branch on both halves, and the retry and
exhaust rungs resolved independently of one another.

## The deferred half — driven 2026-08-07

`111` said a deferred kill reported nothing, and `116` showed it reports the same payload a
synchronous one does, just a turn later. The example now carries a deferred arm so that is
demonstrated rather than asserted.

Crash and `_default` land on the very next turn, so the ordinary harness sees them:

```
-- run async-crash
   I'm running the deferred check now — give me a moment.
   DEFERRED: refused outright.                       [on_exhaust.say.tool_crash]
-- run async-default
   I'm running the deferred check now — give me a moment.
   DEFERRED: unnamed reason.                         [on_exhaust.say._default]
```

The timeout arm needs REAL TIME — the kill is delivered about thirty seconds after
dispatch, and the harness sends its turns back to back:

```
$ python drive_wait.py demo-timeout-failure-ladder --turns 7 --gap 15 \
    --first "4412345678" --say "any news?"
t=  4.1s  I'm running the deferred check now — give me a moment.   (awaits.say)
t= 19.9s  Still waiting on that one.                               (while_waiting)
t= 36.5s  DEFERRED: over its own budget.                 [on_exhaust.say.timeout]
```

**Twelve lines across the four surfaces**, three reasons each: a slot's
`validation.errors`, a synchronous task's `retry_say` and `on_exhaust.say`, and a deferred
task's `on_exhaust.say`. Detection is not a property of the execution mode.

Two things to know when reading that trail. The first `harness.py` run showed the deferred
arms never firing at all: the terminal `wrap_up` announce required only the synchronous
slot, so it ended the call before the deferred task got a turn to come back on. It now
requires both halves. And after speaking its exhaust line the deferred task re-fires —
`max_retries: 0` with no `fill` leaves it eligible — which is why the trail repeats. That
is ordinary ladder behavior, not part of what is being shown.

## Two traps this drive found

**A setter renamed with `name=` is dropped silently.** The first attempt declared
`@flows.tool(name="set_policy_number")` on a function called `check_policy_number`. The
emitted resource took the tool name and the entry function kept the Python name, and a
resource whose name does not match its function is never dispatched (ces-probes `51`). The
symptom is not an error: the slot simply never fills while the model politely re-asks
forever, which reads as a prompt problem. Name the function for the slot instead.

**`on_failure.fill` does not advance the DAG past an exhausted task.** An earlier shape of
this example chained three tasks, each filling its own slot on exhaustion so the next became
eligible. Live, the first task exhausted and then repeated its exhaust line on every
subsequent turn with `calls: none` — the flow never moved on. Recorded as an open finding;
the example was restructured to need one task rather than three, and nothing here depends on
`fill`.

## Neighbours re-verified — 2026-08-07

**`tool_timeout` still reproduces.** Its docstring quotes a live drive; re-driven against a
fresh deploy, the same three beats land with the same lines:

```
t=  2.8s  I'm re-reading the documents on that claim now — it takes a minute or
          two, so stay with me.                                    (awaits.say)
t= 20.6s  Still going through them, thanks for waiting.            (while_waiting)
t=109.3s  Good news — everything on file checks out, so the claim is cleared
          to pay.                                       (completion, then_say)
```

Timings differ from the recorded run only by drive cadence; the 90s body completes under
`timeout=180` exactly as documented. No correction needed.

One instrument note, because it cost a run: `drive_wait.py` repeats a single utterance
every turn, so an example whose wait cannot start until a slot is filled never gets past
its first question — and the trail then measures the collection loop while looking exactly
like the feature being broken. `--first` now seeds a different opening turn.

`async_timeout` and `long_leg_fan_out` are NOT re-driven. Their claims are unchanged by this
work, but "unchanged" is an argument, not a drive.

## Withdrawn: naming a written-off fan-out leg

A leg the group gives up on gets a fabricated result, and the obvious improvement was to
give it an `error_code` — `timeout` when its declared budget had provably elapsed,
`no_result` when the group stopped waiting first. The arithmetic is sound and the unit
tests pass. **Driven live it does not hold, so it is reverted.**

Built as a contrasting pair, both legs stranded at the same instant and told apart only by
their declared budgets (20s against 900s), with a fast leg landing first to start the
clock:

```
t= 28.7s  Running the checks now.  the fast check is clear.
          SHORT LEG: over its own budget.
t= 46.1s  SHORT LEG: over its own budget.
t=104.1s  That's all the checks I can get.  SHORT LEG: unnamed reason.
t=121.1s  SHORT LEG: unnamed reason.
t=137.9s  SHORT LEG: over its own budget.
t=154.6s  SHORT LEG: over its own budget.
t=171.5s  SHORT LEG: unnamed reason.
t=188.4s  SHORT LEG: unnamed reason.
```

Two things are wrong and neither is the arithmetic. The same leg alternates between its
`timeout` branch and `_default` in a steady two-on, two-off rhythm, so the disposition is
not seeing a stable result. And the long leg never reports at all, on any turn.

An earlier run with a 180s long budget had both legs report `timeout`, which was *correct*
— the group held the turn for ~220s before writing off, so both budgets really had
elapsed. That is the reading that made the mis-sizing visible and is worth keeping: **the
write-off does not happen at the ~60s the three-window ladder implies.**

Replacing the `setdefault` (so a `pending` placeholder could not shadow the written-off
result) changed nothing, byte for byte, which rules out the first hypothesis.

What survives from that work and stays on the branch: a leg's `on_exhaust.say` is now
resolved through the shared reason-map resolver instead of being appended raw. That was a
real defect — a keyed dict would have been spoken to the caller as a Python dict — and the
live run confirms the resolution path works, because the branches it chose were spoken as
sentences.

Reinstating the reason needs the leg-disposition selection understood first: why it sees a
result on some turns and not others, and why a second leg is skipped entirely. That is the
same family as the `fill` finding in `114`.

## Withdrawn: `timeout_modes` — and the premise it rested on

A second example was written to show an inversion between the execution modes: the same
overrunning body reporting a handleable failure when synchronous and nothing at all when
deferred. It could not be driven — the deferred arm never dispatched, because it depended
on the `on_exhaust.open_slot` hand-off that probe `115` still cannot verify.

**There is also no inversion to show.** Probe `116` established that a deferred kill
reports the same payload a synchronous one does, a turn later, so the asymmetry the example
existed to teach was an artifact of `111` watching the wrong surface. The deferred half is
now demonstrated inside `timeout_failure_ladder` instead, where it belongs — the same
reason map on both execution modes, which is the actual lesson.

Nothing is waiting on `115` for this any more. If `open_slot` is settled it unblocks a
different example, not this one.
