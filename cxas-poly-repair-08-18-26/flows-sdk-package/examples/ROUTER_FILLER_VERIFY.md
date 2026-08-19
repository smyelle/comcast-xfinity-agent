# Verifying the routing filler

## Why offline green proves nothing here

The unit tests assert that `action["filler_partial"]` is set on a routing turn. That is
worth having, and it is not the claim. The claim is about **how long a caller sits in
silence**, and every part of that number lives outside the engine:

| what fakes it | what it cannot tell you |
| --- | --- |
| `validate_app` | nothing about timing at all |
| the offline engine loader | that the platform actually SPEAKS the partial part |
| a text drive | when the first audio sample arrives |
| any of them | whether the partial preempt kept the floor, or ended the turn |

That last one is the whole feature. A text-only preempt **ends** the turn (ces-probes 26);
only a `partial` one speaks and keeps the floor (ces-probes 57). If that degrades, the
caller hears "One moment." and then has to repeat themselves — which reads as a *faster*
agent in every offline check and is much worse on a phone.

## The instrument

`ces-probes/measure_turn_autofill.py`. It wraps the websocket in both directions and
timestamps:

* the last **outgoing** caller chunk whose RMS clears silence — the instant they stop talking
* the first **incoming** agent chunk whose RMS clears silence — the instant they hear something

One clock, both ends, no nominal buffer durations and no session setup inside the window.
This matters: the previous approach measured from `Sessions.run()` and added the caller
buffer's nominal length, which inflated every reading by roughly 2.3s and produced a
"~2.9s platform floor" that does not exist. The real floor, measured on a bare app with no
engine, no DAG, no tools and no model, is **1.08s**.

## Recipe

Build two apps that differ **only** in the filler, so they can be driven in one window
rather than compared across two:

```bash
cd packages/flows
python -m examples.steering                 # the filler arm
# the control arm: the same app with filler_say omitted from router_flow(...)

cxas create rtr-filler  --project-id <project> --location us
cxas push --app-dir ./steering_app --to <resource> --overwrite
```

`cxas push --display-name` returns a 404 on this deployment; create the app first and push
with `--to <resource> --overwrite`.

Drive, alternating arms:

```bash
python ces-probes/measure_turn_autofill.py --app <resource> \
    --say "my wifi keeps dropping every few minutes" --lead 9 --tail 30
```

`--lead` is silence before the caller speaks, so the greeting finishes first — a routing
turn measured without a greeting in front of it is not the shape a phone call produces.

## What a PASS looks like

Two things, and the second one is the one that can silently regress:

1. first audio drops to within a few tenths of the 1.08s floor
2. the routed answer still arrives **on the same turn**, with no second caller utterance
   between the filler and it

## Which latency, exactly

Two numbers get called "latency" here and they do not agree, so state which one you mean.

**The `cxas` number** (`evals/guardrail_evals.py`) is wall clock around a **text**
`sessions_client.run()`: the WHOLE turn, request to response. It contains no endpointing,
no ASR and no TTS, and it has no notion of when the agent *starts* speaking.

**The caller's number** is end of their speech to the first audible sample, over voice.

A filler moves these in OPPOSITE directions, which is the whole point of it and the reason
a single figure will mislead you. It spends one of the ten reasoning passes, so the turn
takes slightly longer to finish — and the caller stops waiting in silence far sooner.

| `gemini-composite-v1` | cxas-style whole turn (text) | caller: speech → first audio (voice) |
| --- | --- | --- |
| control | 4135, 3368, 2550 — median **3.37s** | 4.21, 4.60, 4.14 — median **4.21s** |
| filler | 3724, 2893, 4560 — median **3.72s** | 1.43, 1.37, 1.38 — median **1.38s** |
| | filler ~0.35s SLOWER | filler **3× faster** |

If the budget is written as "a second from the caller finishing to the agent starting to
speak", only the right-hand column can answer it. Judge a filler by the left-hand column
and it looks like a regression.

## Re-driven on the rebase — 2026-08-09, `gemini-composite-v1`

The branch was rebased onto current main and everything below re-run, because a latency
claim measured against a different base is not a claim about this branch.

| arm | end of caller speech → agent speaks |
| --- | --- |
| control (no filler) | 3.86, 5.14, 5.62 — **median 5.14s** |
| filler | 1.27, 1.69, 1.65 — **median 1.65s** |
| bare app, no engine/model/tools | **1.08s** |

The window is slower than yesterday's on both arms (control was 4.21s), which is why the
control is re-driven every time rather than quoted from a previous session. The ratio holds:
**3× to first audio**, landing about half a second above a floor no application can beat.

Both features are in the deployed pair, so one drive shows both. The greeting is
engine-spoken and identical on both arms, and on the filler arm the holding line precedes
the routed answer within a single turn:

```
control   greet: Thanks for calling Acme. What can I help you with?
          turn : I ran a quick check and your line looks degraded — let's try a few fixes.

filler    greet: Thanks for calling Acme. What can I help you with?
          turn : Sure thing.  I ran a quick check and your line looks degraded — …
          turn : Okay.        I ran a quick check and your line looks degraded — …   (next call)
```

The greeting being byte-identical across runs is the point of it: before this it was
improvised, and twelve drives of one agent produced five different openings.

## Driven live — 2026-08-08

Against **`gemini-composite-v1`**, three runs per arm, interleaved arm-by-arm.

> An earlier revision of this file reported these numbers as composite and they were not:
> the example builds on the `flows` default, `gemini-3.1-flash-live`, and `App(model=...)`
> has to be set explicitly. Findings do not transfer between the two models, so the arms
> were rebuilt and re-driven. Flash-live, for the record, measured 4.05s → 1.54s — the same
> direction, a smaller margin.

| arm | end of caller speech → agent speaks |
| --- | --- |
| control (no filler) | 4.21, 4.60, 4.14 — **median 4.21s** |
| filler | 1.43, 1.37, 1.38 — **median 1.38s** |
| bare app, no engine/model/tools | **1.08s** |

**3× faster to first audio, landing 0.3s above a floor no application can beat.**

The turn is intact — one caller utterance, two agent parts:

```
Okay.  I ran a quick check and your line looks degraded — let's try a few fixes.
```

Check this on the model you ship. The audio driver returns when the agent stops speaking,
so on a turn whose second part arrives after a slow tool it captures only the first line —
which reads exactly like a turn that ended early. Confirm completeness with a text drive
rather than concluding it from the audio capture.

The pool rotated across runs, which is the point of authoring one: consecutive callers
heard `Okay.`, `Sure thing.` and `One moment.` rather than one line becoming the agent's
tic.

**The honest cost.** The filler spends one of the ten reasoning passes, and the routed
answer lands a little later for it: control 3.30/3.40/3.78s against 3.47/3.64/4.00/4.29s
with the filler. So the caller starts hearing something 2.5s sooner and gets the substance
about 0.3s later. On a voice call that is a good trade, and on a chat surface it is not one
worth making — which is why the surface capability drops the filler there and the client
shows a spinner instead.
