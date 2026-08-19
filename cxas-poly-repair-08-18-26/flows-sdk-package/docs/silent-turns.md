# Turns the caller did not take

Two turns on a voice call carry no utterance: an **inactivity tick**, and an **async
completion delivered onto silence**. On both, a deterministic `option_cues` match is
refused the fallback it normally uses, so a slot cannot be filled from words the caller
spoke on an earlier turn.

This page exists because the behavior is an *absence*. A slot that fails to fill is
surprising, it is invisible in a transcript, and the first instinct on meeting it is that
something is broken.

Runnable, deployed and driven: `examples/silent_delivery_turn.py` and its audio driver,
with the live transcripts in `examples/SILENT_DELIVERY_TURN_VERIFY.md`.

## The fallback, and why it exists

`option_cues` needs the caller's words. The engine does not get one clean shot at a turn:
it is re-invoked several times within one turn — after a setter has run, after a terminal
has fired — and on every pass but the first `last_user_text` is empty. Without a fallback,
a cue match would only ever work on a turn's first pass, and any slot that becomes the
awaited question *mid-turn* would never be filled from the sentence meant to fill it.

So the cue pass falls back to `scanned_user_text`: the newest real utterance in the
conversation. On an ordinary turn that is this turn's own words, and everything is fine.

## The two turns where it is not

| turn | why the newest utterance is not this turn's |
| --- | --- |
| an inactivity tick | silence, by definition |
| a completion delivery the caller put no utterance on | the result is the whole content of the turn |

Neither is distinguishable from a within-turn re-invoke by the *text* alone — in all
three cases `last_user_text` is empty and the scan holds a real sentence. They are told
apart by the turn's own signals, and on those two the scan is withheld.

## What it looks like when it goes wrong

An offer gated on an async result becomes the awaited question at the instant the result
lands:

```python
flows.intent_slot(
    "push_fix",
    {"ACCEPT": ["yes", "sure", "go ahead"],
     "DECLINE": ["no", "not now", "leave it"]},
    ask="I can push a firmware update from here — want me to do that?",
    requires=["finding"],
)
```

If the caller has gone quiet by then, the newest utterance in the history is whatever
they last said — in the example, their description of the fault:

> it is not heating at all and the display is not lighting up

which contains `not`, which is inside the `DECLINE` cue list. Nothing about that cue list
is careless; it is the words any author would write. The stale utterance is scored against
a question it was never a reply to, so no amount of care over the cue list saves you.

Without the guard, `push_fix` fills `DECLINE`, and if the rung behind `DECLINE` is
terminal the call ends. **The caller is hung up on for turning down something they were
never asked.** That is the shape this was measured on.

`stale_scan_withheld` is logged whenever the fallback is refused, with whether the turn
was an inactivity tick and how many characters were withheld, so the absence is visible
in `sm["_log"]`.

## What it deliberately does not cost

**A caller who does answer on the delivery turn is still captured.** Their words are this
turn's, so the scan is current and is used. This is not a detail — asking a question
during a slow call is the entire point of the wait primitive, and a fix that dropped the
answer would be a worse defect than the one it replaced. It is pinned by a unit test, by
the offline driver, and by two live calls in the VERIFY note.

**A spoken turn's own re-invoke still fills.** The case the fallback exists for. A
blanket "never use the scan" would take it with it.

## Why it is latched on `sm`

Neither signal outlives the turn's **first pass**. `is_inactivity` is recomputed per
invocation and is true only on the tick's first pass; the completion flag counts what was
ingested on *this* pass, which is nothing the second time.

So an unlatched guard holds once and the cascade pass fills the slot anyway — which is
exactly where the measured hang-up came from: the first pass spoke the completion's own
line, and the pass behind it read the stale words. The withheld text is latched on `sm`
and released by the next real utterance and by nothing else. Keyed on the text as well, so
a scan that has moved on is trusted even if the release was missed.

Measured over a two-minute hold: the fault description was still the newest real utterance
eight ticks later, and the offer was still being asked.

## What it is not

An offer declared with **no `requires`** becomes the awaited question on the very turn the
caller describes the problem, and the cue pass fills it from *that turn's own words* —
before any silence, and with this guard fully in place. Measured while building the
example, both ways.

That is correct behavior. The scan there is genuinely current. It is a cue-quality problem
— a `DECLINE` cue broad enough to match a sentence about a broken heater — and the fix is
either narrower cues or, better, gating the offer on the thing that makes it worth
offering.

## Relationship to `answer_first`

They solve adjacent halves of the same turn and are independent:

| | decides | when it matters |
| --- | --- | --- |
| `awaits(answer_first=N)` | whether a turn carrying BOTH a completion and speech is read as a delivery or as both | the caller **spoke** |
| the stale-scan guard | whether a cue match may use a previous turn's words | the caller **did not** |

`answer_first` keeps the words; the guard keeps silence from being read as words. A flow
that offers something during a wait wants both, and neither substitutes for the other.

## Testing it

Offline is enough for the decision itself — it is entirely the engine's — and
`tests/test_silent_turn_does_not_fill.py` pins both halves plus the latch. What offline
cannot produce is a genuine inactivity tick, so the claim is only closed by a call:
`examples/silent_delivery_turn_drive.py` places one over real audio with a caller who
holds the line, and `--hold` varies how long. Vary it. The completion lands on a different
tick each time, and a guard that held for the first tick and let go on the third would
pass a single run at a single timing.
