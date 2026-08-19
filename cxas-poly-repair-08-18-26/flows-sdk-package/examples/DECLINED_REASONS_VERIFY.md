# Verifying a refusal that has more than one reason

## What this confirms, and why the offline run is not it

`tests/test_declined_say_reasons.py` asserts that the right entry is selected for a given
`filled` state, and the validator tests assert that the wrong shapes are refused. Both
are worth having, and neither is the claim. The claim is that **a caller who asks for a
person hears the explanation that fits the state they are actually in**, and three things
between the selection and the caller's ear are outside any offline oracle:

| what the offline run checks | what it cannot tell you |
| --- | --- |
| which reason matches a state | whether the model ever calls the control tool |
| the selected string | whether the platform speaks it, or softens it |
| `validate_app` | nothing about wording at all |
| the refusal COUNTER | that the counter survives a real multi-turn session |

So this is `examples/declined_reasons.py` deployed and driven as a caller:

* the app — `flows-demo-declined-reasons`, at
  `projects/ces-deployment-dev/locations/us/apps/d857e0ac-6b52-4321-8235-cec9e28b59cd`
* the gate — `escalate(condition={"all": [{"not": OUTAGE}, {"slot": "check_result",
  "filled": True}]})`, so there are two ways to be refused and a third state in which the
  flow does not yet know which
* the seed — `outage_status` is a session variable a `before_agent` hook copies into
  `filled`, which is how the outage leg is driven without waiting for a real one

Re-drive it with:

```
cd packages/flows && PYTHONPATH=src:. python -m examples.declined_reasons \
    --cxas <path to cxas> \
    --live projects/ces-deployment-dev/locations/us/apps/d857e0ac-6b52-4321-8235-cec9e28b59cd
```

which redeploys and asserts all five paths, exiting non-zero if any of them drifts.

## Driven live — 2026-08-13, text

`5/5 live checks passed`. Every one of these is the **same request** — the caller asking
for a person — answered with different words because the state it arrived in was
different. The substrings each assertion matches on are taken from different sentences on
purpose: a set of checks that all passed on one shared phrase would pass just as well
against a `declined_say` that had never learned to branch.

### The catch-all: the flow does not know yet

Session `86159f4e-4612-4be4-b1e3-44f656afadf8`.

```
> I want to speak to a person
< Let me take a couple of details first — I don't want to pass you to someone who then
  has to ask you the same questions.
```

Nothing is filled, so neither the outage reason nor the "check is running" reason
matches, and the entry with no `when` speaks. This is the arm worth having: it is easy to
write a branching `declined_say` whose catch-all can never be reached, and a catch-all
nobody has heard is a catch-all nobody has read.

### Reason 1: an outage, and it is final

Session `48915cc8-7469-4865-96dc-8b2ffa1876a1`, `outage_status=active`.

```
> my internet is down
< I am sorry to hear that. I can help. To start, what's the account number on your bill?
> just put me through to someone
< There's an outage on your street right now — I can see the crew is already on it.
  Putting you through wouldn't get your service back any sooner, so let me text you the
  moment it clears instead.
```

**Both** of the first two reasons hold in this state — there is an outage AND the line
check has not run — and the outage one speaks because it is listed first. That is
first-match-wins doing the only job that matters: the two answers point in opposite
directions, and the one the caller needs is the one about the next hour of their day, not
the one about the next ten seconds.

### Reason 2: the check is still out, and its ladder

Session `3ae1cb07-9f48-4055-acc5-624be8eb4dcc`, `outage_status=clear`.

```
> my internet is down
< I am sorry to hear that. I can help. To start, what is the account number from your bill?
> the account is A 1 0 4 2
< Perfect, thanks. Now, what is the zip code for the address where the line is broken?
> can I talk to an engineer
< The line check is still running. Let me get that back first, so whoever picks up already
  knows what's wrong instead of starting you over.
> no, I'd really rather speak to someone
< Almost there — it's the last few seconds. I'd rather not hand you across without the
  result, because then you'd just be asked all of this again.
```

The last two turns are the composition this feature had to preserve: the matched reason's
`say` is itself a **ladder**, indexed by `escalate_declined` — the number of refusals on
the CALL — so a second ask inside one reason gets a second sentence rather than the first
one again. Clamped, not drained: a third ask would hear the second rung again, because a
refusal answers a direct question and silence is the worst available reply.

### The gate opens, and nothing is refused

Session `86520ec1-9590-4599-8012-56214ae23938`, `outage_status=clear`.

```
> my internet is down
< I am sorry to hear that. I can help. To start, what is the account number from your bill?
> the account is A 1 0 4 2
< Perfect, thanks. What's the zip code where you need the service?
> the zip is 94043
< I can check for known issues. Thanks, I'm not seeing any reports. If your service is
  still down, I can open a ticket for you. Would you like me to do that?
> can I talk to an engineer
< Of course — let me get you through to an engineer now.
```

The ordinary behavior once the blocking condition clears, and the arm that stops the demo
proving only that the gate is shut. `escalate_declined` is 0 in this session; in the
session above it reached 2 and the hand-off would still have happened, because the gate
does not read the counter — an important difference from the contain-once pattern, where
it does.

## The offline run, for comparison

```
cd packages/flows && PYTHONPATH=src:. python -m examples.declined_reasons
```

```text
  nothing known yet (catch-all)        -> "Let me take a couple of details first — I don't want to pass you to someone who then has"
  an outage (reason 1)                 -> "There's an outage on your street right now — I can see the crew is already on it. Puttin"
  check still out (reason 2, rung 0)   -> 'The line check is still running. Let me get that back first, so whoever picks up already'
  check still out (reason 2, rung 1)   -> "Almost there — it's the last few seconds. I'd rather not hand you across without the res"
  the check landed (no refusal)        -> 'Of course — let me get you through to an engineer now.'
```

Identical selections, which is the point of running both: the reason chosen is the
engine's decision and it is fully determined offline. What the live run adds is that the
model actually raises the request, the engine actually gets asked, and the preempted line
actually reaches the caller unaltered — the last of which is visible above, since every
refusal is quoted word for word from the source.

## What the author is stopped from writing, quoted rather than described

Two gates, and they catch different things. `flows.escalate(...)` raises on the shapes it
can see from the call alone; the validator sees the whole flow, so it is the one that can
resolve a `when` against the declared slots. Both were provoked against this example and
the messages copied back verbatim.

Authoring time, from `flows.escalate(declined_say=...)`:

```text
escalate(): declined_say[2] can never be reached — declined_say[1] has no `when`, so it
matches every refusal. Put the catch-all last.

escalate(): declined_say[1] is a reason with no `say`, so matching it would refuse the
caller in silence

escalate(): declined_say[1] must be a line or a {'when': ..., 'say': ...} reason

escalate(): declined_say=[] says nothing on a refusal — omit it entirely to make the
block silent, or give at least one line
```

`validate_app`, with this example's own `escalate` block edited in place:

```text
[line_repair] escalate.declined_say[0].when references undeclared slot 'outage_state'
```

That last one is the only one that is not obviously wrong on the page — one letter off
the slot the flow declares. A `when` on a slot the flow does not declare never matches,
so the caller hears the **next reason down**: a confident, fluent, wrong explanation, on
a call that looks completely healthy.

A branching `declined_say` with no catch-all is a **warning**, not an error:

```text
[line_repair] escalate.declined_say has no catch-all reason (an entry without `when`), so
a refusal none of its conditions match is silent — and a refusal is an answer to a direct
question.
```

Reachable config, only silent in a state the author did not describe, so it is not the
validator's business to refuse it.

## What is still not covered

* **Only text.** Every refusal here is a preempt, so the wording is the engine's and does
  not depend on the channel — but nothing in this demo has been driven over audio, and
  `preempt` and prosody have surprised this project before. A refusal is the one turn on
  a call where tone carries as much as words.
* **Only `gemini-composite-v1`** (whatever the target app carries; not pinned by the
  example). CES findings do not transfer between models, and which turn the model
  chooses to raise `transfer_to_human` on is a model decision.
* **The unevaluable `when` is not driven.** It is unit-tested — a `when` that raises is
  skipped, and `declined_say_when_error` is logged — but no live call has been made with
  a deliberately broken condition. The validator rejects the reachable causes at build
  time, which is why this was left; the runtime path exists for a condition that becomes
  unevaluable against a state at runtime, and that state has not been constructed.
* **`declined_say_no_reason_matched` is not driven either.** The validator warns at build
  time, and this example carries a catch-all, so nothing here can reach the silent
  outcome the warning is about.
* **No `verbatim` / `speech` interaction.** `escalate(verbatim=True)` pins a refusal
  against an improvisation policy, and a reason's lines go through the same lint pass as
  any other spoken line — but this app sets no `speech` policy, so the two features have
  not been exercised together on a live call.
* **The counter is per-CALL, not per-reason,** and only one session here refused twice.
  A call that is refused once for an outage and then once for a running check would ask
  the second reason for rung *two*, skipping its first line. That is arguably right and
  arguably surprising; it has not been driven, and it is the first thing to measure if
  a flow with more than two reasons is written.
