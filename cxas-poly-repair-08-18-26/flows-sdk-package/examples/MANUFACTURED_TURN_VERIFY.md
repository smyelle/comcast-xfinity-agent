# Manufactured turn — live verification

An outstanding question is not put again on a turn the caller did not take. Proven over
voice, because neither turn this is about can be produced any other way: CES rejects a
hand-written `<context>` marker as malicious input, and the offline harness cannot
produce an asynchronous completion at all.

App: `projects/ces-deployment-dev/locations/us/apps/c0bc4013-6975-46d9-949e-caaaefa88ceb`
("Manufactured turn (ask ladder)"). Engine `2026.08.18.1+manufactured-turn`, flows 0.18.1.

## Run it

```bash
cd packages/flows
PYTHONPATH=src python -m examples.manufactured_turn --out /tmp/mt_app
PYTHONPATH=src python -c "from flows.deploy.push import deploy; \
  deploy('/tmp/mt_app','projects/ces-deployment-dev/locations/us/apps/c0bc4013-6975-46d9-949e-caaaefa88ceb')"
PYTHONPATH=src python -m examples.manufactured_turn_drive \
  --app projects/ces-deployment-dev/locations/us/apps/c0bc4013-6975-46d9-949e-caaaefa88ceb
```

macOS only — the driver synthesizes caller audio with `say` and `afconvert`, so it needs
no TTS credentials and no ffmpeg.

## What the caller heard

Session `7fe22f7e-4838-43fb-843a-550664fdcc98`. The caller speaks twice and then goes
quiet for half a minute.

```
t=   8.2  Let me check the line while we talk.
t=  20.2  Which device is having trouble? Is it the TV box, or something else?
t=  55.8  Could you tell me which device is having the connection issue? For example,
          is it your TV box, a computer, or a phone?

poll window: t=15s to t=47s (the caller says nothing; a push and several ticks arrive)
  t=  20.2  Which device is having trouble? Is it the TV box, or something else?

verdict:
  agent turns inside the poll window : 1     (want 1 — the question, put ONCE)
  agent answered the caller after it : True  (want True — the line did not die)

PASS
```

## The turns themselves

Silence in a transcript proves nothing on its own — a poll that never arrived looks
exactly like one that was correctly ignored. From the conversation trace for the same
session, every turn on the call:

```
t0 user   My internet keeps dropping out.
t0 agent  Let me check the line while we talk.
t1 user   It has been doing it since yesterday.
t1 agent  Which device is having trouble? Is it the TV box, or something else?
t2 user   <context>function [check_line] completed with response {…}</context>
t3 user   <context>no user activity detected for 8 seconds.</context>
t4 user   <context>no user activity detected for 8 seconds.</context>
t5 user   I'm not sure what you mean.
t5 agent  Could you tell me which device is having the connection issue? …
t6 user   <context>no user activity detected for 8 seconds.</context>
```

**Four manufactured turns — one completion push (t2) and three inactivity ticks (t3, t4,
t6) — and not one of them has an agent reply.** Both caller turns do. And t5 is rung
**two**, not rung three: the polls held the ladder rather than burning it.

## The other arm: a declared `no_input` policy

Silence is the one thing an author can have a policy about, and this change moves
`is_inactivity` onto that ladder's own guard — so if anything were going to break a
declared policy, it would break here. The same flow with a flow-level `no_input` block is
the A/B; the two arms differ by that block and nothing else.

App: `projects/ces-deployment-dev/locations/us/apps/374296d4-1800-46c0-bd8d-3e1f21787056`
("Manufactured turn NO_INPUT"), session `ac19121d-b463-4548-8233-6183df50b534`.

```bash
PYTHONPATH=src python -m examples.manufactured_turn --no-input
PYTHONPATH=src python -m examples.manufactured_turn_drive --no-input --app <that app>
```

```
t0 user   My internet keeps dropping out.
t0 agent  Let me check the line while we talk.
t1 user   It has been doing it since yesterday.
t1 agent  Which device is having trouble? Is it your TV box, or something else?
t2 user   <context>function [check_line] completed with response {…}</context>
t3 user   <context>no user activity detected for 8 seconds.</context>
t3 agent  Are you still there?
t4 user   <context>no user activity detected for 8 seconds.</context>
t4 agent  I can still hear the line -- take your time.
t5 user   <context>agent speaking was interrupted. user only heard 'I can still hear the'…
t5 user   I'm not sure what you mean.
t5 agent  Could you tell me which device is having the issue? …
```

Three things, and all three are the point:

- **The ticks belong to the policy again.** t3 and t4 speak its reprompts, in order,
  out loud. Identical to the behaviour before this change.
- **The completion push is still held** (t2, no reply) — in *both* arms. A push is not
  the caller being silent, so it reaches neither ladder and must not spend the caller's
  patience budget.
- **t5 arrived with a real barge-in marker** on the same turn as the speech. That is
  exactly why the engine branch was widened rather than replaced: a barge classifies as
  the caller acting while `is_inactivity` is still set, and keying on `turn_kind` alone
  would have dropped this turn out of the ladder. It turned up on its own, unprompted.

## The two failures this replaced

Both were measured on this same app before the fix landed, and each one is a reason a
piece of the change exists.

**Drive 2** (`cc73d650`) — the ticks were already silent, and the push spoke:

```
t=  20.2  Which device is having trouble? Is it your TV box, or something else?
t=  34.5  Which device is having trouble? Is it your TV box, or something else?   <-- the push
```

A pass inside the push classified as `"caller"` and overwrote the latch mid-turn. On a
live call a re-invoke does not reliably arrive as a continuation: once CES has blanked
the envelope, the newest user content is the caller's last real utterance again. Fixed
by latching `"caller"` only when the caller-turn counter has actually moved.

**Drive 3** (`dddacb2f`) — same symptom, deeper cause. The trace showed the engine being
handed this on the push turn:

| pass | `_turn_n` | `_awaiting` | `task_results` |
|---|---|---|---|
| end of turn 1 (question asked) | 2 | `device` | `check` |
| **turn 2, the push — the decision runs here** | **1** | **`reason`** | **none** |
| turn 2, after reconcile | 2 | `device` | `check` |

CES delivers an asynchronous completion by resuming the invocation that made the call,
so the state arrives as it was at the time. `_awaiting` pointed at a question two turns
old and every sm-derived guard was blind. Fixed by detecting the snapshot with
`n_user_turns`, which is counted off the request contents and cannot roll back.

## Notes for anyone re-running this

- **The caller's second utterance is load-bearing.** The line check starts the moment the
  first one lands, and while a task is awaited the outstanding question is the *wait*, not
  the ask. Go quiet after one utterance and the engine asks about the device for the first
  time on a poll — and a first ask is supposed to speak. That is correct behaviour, and it
  looks like the bug.
- **Do not grade on wording.** Each rung is a directive and the model words it. An earlier
  verdict counted "which device" and read rung two as a second rung one. The claim is about
  *when* the agent speaks, so the driver counts agent turns inside the poll window instead.
- **The wait must give up before the check answers.** `awaits(max_turns=2)` against a 25s
  backend is what puts a completion push and an open question on the call together.
