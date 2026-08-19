# Barge-in awareness — driven live on `gemini-composite-v1`

Everything below is a real transcript from a deployed app, captured with
`ces-probes/drive_barge_audio.py`. Offline tests prove the logic; only a live drive proves
the caller hears it, because the defect is invisible to a text channel — the text part is
complete, and only the audio is cut.

## The two apps

```
cd packages/flows
PYTHONPATH=src python -m examples.barge_in_awareness            # treatment
PYTHONPATH=src python -m examples.barge_in_awareness --control  # A/B control
```

The control differs in exactly two ways: `barge_in_awareness=False`, and no `repair=` on
its announces. Both run `gemini-composite-v1`, both read the same four-part disclosure.

```
cxas push --app-dir barge_in_awareness_app --project-id ces-deployment-dev \
          --location us --display-name "Barge-in demo TREATMENT"
```

> `--overwrite` on an existing app fails validation with *"model gemini-composite-v1 is
> not available for the app"* even though the same model is accepted on CREATE and the
> deployed app demonstrably runs it. Push a new app rather than overwriting.

## 1. The A/B — same interruption, opposite outcomes

Caller says "mhmm" while the disclosure is being read. Both runs were interrupted at
effectively the same point (playback ~4.09s vs ~4.10s), so the only variable is the
feature.

```
python drive_barge_audio.py x --app <APP> --say "hi, I want to open an account" \
       --wait 5 --then "mhmm" --gap 20 --tail 10 --out /tmp/ab.wav
```

**CONTROL** — `INTERRUPTION at 9.92s, played ~4.09s`

```
agent   Thanks for calling Northwind. I can open a new account for you.
        Before I can open the account I need to read you a few terms.
        Calls are recorded for training and quality.
        Your personal data is retained for ninety days after the call.
        You can opt out of marketing at any time by calling us back.
        Would you like a checking or a savings account?
caller  mhmm                                              [cut at ~4.1s of playback]
agent   Would you like to open a checking account or a savings account?
```

The retention and opt-out terms were never heard and are never spoken again. All four
announces are latched delivered.

**TREATMENT** — `INTERRUPTION at 10.08s, played ~4.10s`

```
caller  mhmm                                              [cut at ~4.1s of playback]
agent   Sorry — as I was saying, Calls are recorded for training and quality. Your
        personal data is retained for ninety days after the call. You can opt out of
        marketing at any time by calling us back. Would you like a checking or a
        savings account?
```

Verified on the AUDIO, not just the text frames — `judge_audio.py` over each arm's
second turn:

```
CONTROL    VERDICT: RETENTION=NO  OPTOUT=NO
TREATMENT  VERDICT: RETENTION=YES OPTOUT=YES
```

## 2. The replay boundary tracks the interruption

One drive is not proof — a single pass can land right by luck. Four barge times against
the same deployment, and the resume point moves with the cut:

| caller interrupts | playback position | replay resumes at |
|---|---|---|
| 3s | ~1.49s | "Before I can open the account I need to read you a few terms." |
| 5s | ~4.10s | "Calls are recorded for training and quality." |
| 8s | ~7.03s | "Your personal data is retained for ninety days after the call." |
| 11s | ~10.14s | "You can opt out of marketing at any time by calling us back." |

Nothing already heard is repeated, and nothing missed is dropped. This is the payoff of
recording the ledger per RESPONSE PART: every line after the cut point is replayed
verbatim, with no attempt to reconstruct anything.

## 3. A backchannel with nothing cut is simply absorbed

The caller says "mhmm" twice after the agent has finished speaking:

```
   32.67s  asr    MHMM
   33.75s  text   Would you like to open a checking account or a savings account?
   41.86s  asr    MHMM
   42.91s  text   Would you prefer a checking account or a savings account?
```

No interruption signal (the agent had finished), no replay, and no escalation. Today each
of those turns fills no slot, so `_handle_state_change` reports no progress and
`_handle_steer_back` counts them — enough of them escalate the call. That the counter stays
at zero is pinned offline in
`test_barge_in.py::test_a_backchannel_does_not_count_as_a_stall`, because the counter is
internal state a live drive cannot read.

## What the live runs also confirmed about the platform

The **control still received `{"bargeIn": true}` on the wire** and its speech was still cut,
with no `audioProcessingConfig` at all. That is probe `162`'s finding reproduced
incidentally by the demo: the flag does not decide whether the caller can interrupt, only
whether the AGENT is told. Omitting it never prevents the cut — it only loses the report.

## The bug this demo was hiding

The first version of this page claimed the treatment "delivers exactly the lines the caller
did not reach". It did not. Asked whether the replay was skipping part of a sentence, the
honest answer turned out to be that it was skipping a whole one.

The `welcome` announce declares no `repair=`, so it was never written to the ledger — but
the caller *heard* it, so the platform's reported prefix begins with its 63 characters. The
boundary overshot by exactly that much and the first disclosure line was marked heard and
dropped. Live, at the same cut point, before and after:

```
before   Sorry — as I was saying, Calls are recorded for training and quality. …
after    Sorry — as I was saying, Before I can open the account I need to read you
         a few terms. Calls are recorded for training and quality. …
```

Two changes, because the data was wrong *and* the arithmetic trusted it:

* **Record every announce the cascade speaks**, not only the repairable ones — offsets are
  correct only when the recording covers all the speech. Repairability is now decided at
  replay. A flow with no `repair=` anywhere still records nothing.
* **Anchor the boundary on content, not length.** Walk the reported prefix against what was
  actually said and stop at the first disagreement. When they agree this is identical; when
  they do not, the common prefix is shorter, so the failure mode is repeating a line the
  caller already heard rather than silently dropping one they did not. A repeat is
  noticeable and harmless; a drop is neither.

Pinned by `test_a_non_repairable_announce_still_occupies_the_heard_prefix` and
`test_a_misaligned_prefix_replays_more_rather_than_dropping_a_line`.

**The general lesson:** every "line the caller missed" claim needs to be read against the
FULL text that was spoken, not against the subset the feature happens to own.

## Gotcha this demo walked into

An announce's `texts` are **dropped unless `preempt=True`**. The first build of this demo
authored the disclosure without it: every announce filled its slot, the transcript looked
right, and the caller heard nothing but the closing question. A disclosure that must be
read verbatim is exactly the case that cannot be left to the model's own wording, and a
demo whose lines never reach the caller proves nothing about recovering them.
