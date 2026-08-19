# Verifying that a silent turn does not answer a question

## What this confirms, and why it needed a phone call

`tests/test_silent_turn_does_not_fill.py` drives the blessed engine directly and pins
both halves: the completion delivery a caller put no utterance on does not fill, and the
caller who does answer on that turn still does. That is worth having, and it is not the
claim. The claim is that **a caller who says nothing is not recorded as having refused
something**, and the turn that carries the refusal does not exist on a text channel at
all:

| what the offline run checks | what it cannot tell you |
| --- | --- |
| the cue pass, given a flag | that the platform ever sets that flag |
| `is_inactivity=True` | what a real inactivity tick looks like, or how often one arrives |
| the latch across two passes | how many passes a real turn makes |
| `validate_app` | nothing about turns at all |
| a text session | anything — **every text request carries an utterance**, so the turn under test cannot occur |

That last row is the whole reason this note exists. Over text there is no such thing as a
turn the caller did not take: the driver sends a string or it sends nothing. The turns
this guard is about — an inactivity tick, and an async completion delivered onto silence
— are manufactured by the platform when nobody is speaking.

* the app — `flows-demo-silent-delivery-turn`, at
  `projects/ces-deployment-dev/locations/us/apps/4e3dbbd3-1e0d-48e9-930d-8d80132be617`
* the flow — `examples/silent_delivery_turn.py`: a thirty-second `asynchronous=True`
  diagnostic, and one offer (`push_fix`) gated on its finding
* the harm — the rung behind `DECLINE` is a terminal announce, so a wrong fill does not
  sit in a log. It ends the call.

The caller's last real utterance before the silence is

> it is not heating at all and the display is not lighting up

which contains `not`, which is inside `push_fix`'s plainly authored `DECLINE` cue list.
Nothing about that cue list is careless — it is the words any author would write — and
that is the point: a stale utterance is scored against a question it was never a reply
to, so no amount of care over the cues saves you.

## Driven over real audio, with a caller who holds the line

```
cd packages/flows && PYTHONPATH=src:. python -m examples.silent_delivery_turn_drive \
    --app projects/ces-deployment-dev/locations/us/apps/4e3dbbd3-1e0d-48e9-930d-8d80132be617 \
    --hold 35
```

Caller audio is one buffer streamed at real time: three utterances, then silence. Every
turn after the third is a platform inactivity tick, and the completion lands on whichever
one of them follows the diagnostic finishing. **Six calls at five hold lengths** — 35s,
40s, 50s and 65s of silence, plus 30s and 45s with a word at the end of them — because one
pass at one timing has produced a false pass in this project before. The completion lands
on a different tick each time, and a guard that held for the first tick and let go on the
third would pass a single run.

### Hold 35s — the cleanest of the silent arms

Session `d6d3ce90-48cc-46c6-9919-36cb81d37f46`.

```
t=   5.0 < To get started, could you please tell me the serial number on your unit?
           [the caller says "hello" at t≈0, "the serial is 4417" at t≈8]
t=  15.3 < Okay, I have the serial number. What seems to be the problem with your heater?
           [the caller says the fault line at t≈16, and then nothing at all]
t=  26.2 < Let me run a diagnostic on it — that takes about half a minute.
t=  39.0 < Still going, thanks for hanging on.
t=  50.5 < Nearly done.
t=  56.9 < Looks like your heater has old firmware. I can push an update from here —
           would you like me to do that?
t=  71.7 < Are you still there? I can push a firmware update to try and fix the issue,
           just let me know if you would like me to proceed.
t=  87.1 < Do you want me to go ahead and push that
```

`t=56.9` is the turn. The completion landed on it, the caller had said nothing since
`t=15`, and the flow **asked its question** instead of answering it. Every turn after
that is another inactivity tick with the same question still open — which is the latch
holding across turns, not merely across the passes within one.

### Hold 40s, 50s and 65s — the same verdict, a different tick

Sessions `536e1691-b8a8-48be-8d83-4c1a57fd510a` (40s, quoted below),
`1a4d991f-9f16-402c-bf60-1ce1c99a56fe` (50s) and
`a536d0c3-0190-4a17-a8c8-22368ebfde55` (65s).

```
t=  49.0 < Nearly done.
t=  55.9 < Looks like the issue is old firmware on the burner control board. I can push
           an update right now — would you like me to do that?
t=  72.6 < The burner control board is running old firmware, but I can push an update
           from here if you'd like.
t=  87.3 < Are you still there? I can push a firmware update to help fix the issue if
           you want.
```

```
t=  50.5 < Nearly done.
t=  56.5 < Hmm, I'm having trouble with that. Do you want me to try again?
t=  56.5 < I can push a firmware update from here — want me to do that?
t=  74.4 < So, would you like me to push that firmware update?
t=  86.4 < So, would you like me to push that firmware update?
t=  98.9 < Are you still there? I can push a firmware update from here — want me to do that?
```

```
t=  48.5 < Nearly done.
t=  55.8 < Hmm, I'm having trouble with that. Do you want me to try again?
t=  55.8 < I can push a firmware update from here — want me to do that?
t=  73.3 < Are you still there? I can push a firmware update from here — want me to do that?
t=  86.4 < Do you want me to go ahead and push that firmware update from here?
t=  98.5 < Should I try pushing that firmware update? It might fix the problem.
t= 111.1 < Should I push the firmware update, or would you prefer to escalate this?
```

Same verdict on both: the offer survives, the call does not close. See the caveat about
`Hmm, I'm having trouble with that` below — it is not the agent, and it did not stop the
finding landing.

### Hold 30s, then "yes please" — the half a bad fix would break

Session `b1df74a6-f49c-4cd0-b6e9-1ad26c1a3bae`. The caller's `yes please` is timed to end
right where the completion lands, so the platform packs both into one turn.

```
t=  25.7 < Let me run a diagnostic on it — that takes about half a minute.
t=  38.3 < Still going, thanks for hanging on.
t=  49.6 < Nearly done.
           [the caller says "yes please" at t≈56]
t=  56.3 < Done — 4417 is updating now, it takes about a minute.
t=  71.6 < Still here if you need anything else. Otherwise, have a great day!
```

The offer was never spoken aloud, and it was still answered: the finding filled on that
turn, `push_fix` became the awaited question, and the caller's words on the same turn
filled it `ACCEPT`. **This is the arm that makes the silent arms mean something.** A fix
that simply refused the scan on every quiet-looking turn would pass all three runs above
and fail this one, and nothing in the transcript of a silent call would show it.

### Hold 45s, then "yes please" — the ordinary path, for contrast

Session `f0fc40bd-ac36-4efc-9216-2f00ecd855ae`.

```
t=  50.7 < Nearly done.
t=  57.0 < Got it. The diagnostic shows your burner control board has old firmware.
           I can push an update from here — want me to do that?
t=  72.4 < Done — 4417 is updating now, it takes about a minute.
t=  97.0 < If there's nothing else, thanks for calling and have a good day.
```

Here the completion landed on a silent tick (the question survived it), the caller then
answered on an ordinary turn, and the fix was applied. Both mechanisms in one call.

## The text channel, and what it can and cannot reach

```
cd packages/flows && PYTHONPATH=src:. python -m examples.silent_delivery_turn \
    --cxas <path to cxas> \
    --live projects/ces-deployment-dev/locations/us/apps/4e3dbbd3-1e0d-48e9-930d-8d80132be617
```

`2/2 live checks passed`. Real seconds are load-bearing here: the diagnostic sleeps
thirty of them, so a check that fires its turns back to back never reaches a delivery
turn at all.

```
    t=  1.1 > hello
            < Welcome! I'm here to help with your heater. What's the serial number on the unit?
    t=  3.2 > the serial is 4417
            < So you said 4417, I've got that. And what's it actually doing?
    t=  5.2 > it is not heating at all and the display is not lighting up
            < Let me run a diagnostic on it — that takes about half a minute.
    t= 19.8 > any news?
            <
    t= 34.3 > how about now?
            <
    t= 49.7 > yes please, go ahead
            < Done — 4417 is updating now, it takes about a minute.
```

The silent half is **unreachable over text by construction**, so the first check asserts
an absence instead: after the wait has started and one poll turn has gone by, the call has
not closed on `No problem, I'll leave the firmware as it is` — a refusal nobody made.

## The offline run, including the counterfactual

```
cd packages/flows && PYTHONPATH=src:. python -m examples.silent_delivery_turn
```

```text
  silent delivery turn        -> push_fix=None
       stale_scan_withheld {'inactivity': False, 'chars': 59}
  inactivity tick             -> push_fix=None
  answered on the delivery    -> push_fix='ACCEPT'
  ...with the latch removed   -> push_fix='DECLINE'   <- the call would end here
```

The last line is what makes the first three a result rather than an observation. Popping
`_stale_scan` off `sm` between the two passes of one turn is **exactly** the engine
before this fix: neither flag outlives the turn's first pass (`is_inactivity` is
recomputed per invocation, and the completion flag counts what was ingested on THIS
pass), so an unlatched guard holds once and the cascade pass fills the slot anyway. It
fills it `DECLINE`, and `DECLINE` ends the call.

## Two things this drive settled that the code did not say

**1. An ungated offer fills on the caller's own turn, and that is correct.** Measured
while designing the example. With `requires=["finding"]` removed, `push_fix` becomes the
awaited question on the very turn the fault is described, and the cue pass fills it
`DECLINE` from that turn's own words — before any silence, and with the guard fully in
place. That is not this defect: the scan there really is that turn's text, and refusing
it would break the within-turn re-invoke the fallback exists for. It is a cue-quality
problem, and it is why the offer in this example is gated on the finding rather than
merely declared after it.

**2. The stale scan does not expire on its own.** In the 65-second arm the fault
description was still the newest real utterance a hundred seconds and eight ticks later,
and the offer was still being asked. The latch is released by the next real utterance and
by nothing else, which is what that run shows over a much longer window than any unit
test covers.

## What is still not covered

* **`Hmm, I'm having trouble with that. Do you want me to try again?`** appeared on the
  completion turn in two of the three silent arms (hold 50 and 65) and not in the third
  (hold 35). It is the CES crash envelope, not anything this flow says — no line in the
  app resembles it. It did **not** swallow the completion: `push_fix` became askable on
  the same turn, which requires `finding` to have landed. It is unexplained, it is
  intermittent, and it is not this feature's — but a reader re-driving this will see it
  and should not read it as the demo failing.
* **`while_waiting` is spoken over voice and comes back empty over text.** The two
  holding lines are audible at `t≈39` and `t≈50` on every voice run, and the equivalent
  text turns return an empty agent string. Not chased; it does not touch this claim, and
  it matches what the remote-tool drive saw.
* **Only one model.** Whatever the target app carries — the example pins none. CES
  findings do not transfer between models, and none of this has been run against
  `gemini-3.1-flash-live`.
* **The inactivity timeout is whatever the app carries** (the SDK's deploy default is
  `8s`, and the observed ticks are 11–15s apart, so the target's own setting is in play).
  A much longer timeout means fewer ticks between the fault description and the
  completion, and that configuration has not been driven.
* **No DTMF, and no barge-in.** A caller who presses a key rather than speaking, and a
  caller who talks over the holding line, are both turns this guard has an opinion about
  and neither has been driven.
* **One backend duration.** The diagnostic always sleeps thirty seconds. A completion
  that lands on the FIRST tick after the wait engages — before either holding line is
  spent — has not been produced, and that is the tightest version of this turn.
* **The counterfactual is offline only.** The pre-fix behavior is reproduced by removing
  the latch in the offline driver, not by deploying a pre-fix build. A live A/B of the
  two engines would be stronger and was not done.
