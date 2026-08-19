# Verifying build-time automatic fillers

## What the unit tests do and do not prove

`tests/test_automatic_fillers.py` asserts that the pass moves the right sentence and
leaves the wrong ones alone. That is worth having, and it is not the claim. The claim
is that **the caller stops sitting in silence**, and none of it lives in the config —
both builds contain exactly the same words.

| what it checks | what it cannot tell you |
| --- | --- |
| the config diff | when either half is spoken |
| `validate_app` | nothing about timing at all |
| a fakes harness (`use_tool_fakes=True`) | anything — fakes make the tool instant, so there is no wait to cover |
| the offline engine | that the platform SPEAKS the fire-turn message, or when the first audio sample lands |
| all of them | that a hoisted slot opener can surface a whole turn later than it was authored |

## The offline driver

```
cd packages/flows && PYTHONPATH=src:. python -m examples.automatic_fillers_drive
```

Builds `examples/automatic_fillers` twice — pass off, pass on — and runs the blessed
engine over both. Real output:

```text
automatic_fillers OFF
  ask turn     (silence)
  fire turn    (silence)
  result turn  Thanks for holding. Your balance is 42 dollars and 10 cents.

automatic_fillers ON
  ask turn     Okay.
  fire turn    Thanks for holding.   <- rides the fetch_balance call
  result turn  Your balance is 42 dollars and 10 cents.
```

This is the structural half of the claim, and it is genuinely load-bearing: the fire
turn carries the line **and** the `function_call` in one action, which is what makes it
free. It says nothing about seconds.

## Measured over real audio

Two builds of one flow deployed side by side, called over the voice channel with
alternating arms, timing from the end of the caller's utterance to the first *audible*
agent sample on the turn the task fires. No tool fakes — the tool really sleeps three
seconds. Harness: `audio-latency-scratch/drive_audio_vars.py`.

| | median | range | runs |
| --- | --- | --- | --- |
| off | 6.30s of silence | 6.04 – 7.03 | 4 |
| on | 0.99s of silence | 0.88 – 1.22 | 6 |

**5.3s less dead air**, ranges non-overlapping. Within the on arm:

```text
utterance ends
  +0.99s   Okay.                                 <- ask hoist
  +2.72s   Thanks for holding.                   <- task hoist, riding the call
  +6.34s   Your balance is forty two dollars...
```

Two things this settles that no offline check could:

1. **The platform speaks the fire-turn message concurrently with the tool**, rather than
   serializing behind it. That was the load-bearing unknown — the engine emits the line
   and the `function_call` in one action, but only the platform decides what to do with
   that. It speaks it.
2. **The answer lands at the same moment either way** (6.34s on, 6.30s off). This buys
   no throughput at all. It converts silence into speech, nothing more, and the docs
   should never imply otherwise.

The `on` median of ~1.0s also agrees with the ~1.1s turn-latency floor measured
independently in earlier work, which is the sanity check that the clock is now right.

To reproduce, build the demo twice with `App.automatic_fillers` flipped, push each to its
own scratch app so the arms can be interleaved rather than run in blocks, and drive:

```bash
cxas create latency-measure-off --project-id ces-deployment-dev --location us
flows deploy --app-dir ./app_off --to <that app resource>      # and again for --on

python drive_audio_vars.py x --app "$ARM_APP" \
  --say hello --lead 1 --wait 10 --then "five five five one two three four" \
  --tail 13 --out /tmp/run_${ARM}_$i
```

`--tail 13` matters: a longer trailing silence draws a further no-input turn, and the
driver reports the LAST turn, so the number you read is then the wrong turn's.

## The driver's clock reads ~3.4s too high — fix it before believing anything

**The first version of these numbers was wrong** (9.65s and 4.06s) for exactly the
reason this codebase has been caught by twice already. `sessions.py` sends a 0.1s audio
chunk and then sleeps a FLAT `CHUNK_DELAY` of 0.1s, so each iteration costs the sleep
*plus* protobuf, JSON and the socket write. Over ~160 chunks the stream falls seconds
behind real time — while `utt_end` is computed as `lead + duration(speech)`, buffer
arithmetic that assumes no drift. Every latency then reads high by the accumulated
drift. It also silently ignores the 3 chunks (0.3s) of leading silence the library
prepends.

The fix is not to model the drift but to measure it: hook the paced sleep, record when
each chunk actually left, and take `utt_end` off the wire. Then both endpoints are on
one clock and the connect/setup offset cancels too. `drive_audio_paced.py` does this and
prints a residual per run.

The delta survived the correction (5.6s → 5.3s) because the bias is common to both
arms. The absolutes did not — they moved by ~3.4s. If a measurement only ever gets used
as an A/B this matters less, but never quote the absolute without checking the clock.

**What is still wrong even after the fix.** Pacing to an absolute grid only got the
residual from 3.4s down to 2.3s, because `ws_app.send` blocks on the socket — the client
genuinely cannot deliver 16s of audio in 16s from here. So the platform hears a caller
whose pauses are ~14% stretched and can endpoint early, which is why the occasional run
shows the agent answering *before* the caller finished. Discard runs whose residual
departs from the median by more than a second; they are a different call.

## Other harness rules

Time to *first audible sample*, not first chunk — roughly three quarters of the returned
stream is comfort silence, so first-chunk arrival scores every part at ~0.1s; the driver
thresholds at RMS >= 150 for this reason. Do not use character counts as a proxy: TTS is
streamed, so a shorter first part does not reach the ear sooner.

Run the arms **alternately and sequentially**. Concurrent calls contend, and a block of
one arm followed by a block of the other measures backend drift as much as the change.
Expect roughly one call in five to come back empty — a session flake, not an arm
difference; top up rather than reporting the survivors as the sample.

## Check the turn AFTER the split, not just the split

Splitting a line across the fire and result turns has previously changed what the agent
said on the *following* turn — an agent given a diagnosis in the same message as its
tool call went on to assert a cause it could not know, three runs of three, while the
split turn itself stayed byte-identical. Establish a same-app-against-itself noise
floor first; model nondeterminism alone diverges a few journeys in twenty.

## Two regressions this feature already shipped and lost

Both were invisible to the config diff and to a single-flow drive. Re-check them after
any change to the pass:

1. **The following question.** An earlier draft also set `preempt_then_say`, which
   returns before the engine folds the then_say together with the next question. Drive a
   flow shaped `slot -> task -> slot` and confirm the result turn still ends with the
   next question. `test_the_following_question_survives_the_hoist` pins it.
2. **Outcome claims.** A gate built from allowed WORDS rather than whole phrases hoists
   "All good.", "No." and "We will take it back." — an answer and two claims about what
   the backend did, all spoken *before* the call. `test_it_leaves_everything_else_alone`
   pins the rejections.

## How often it fires

Measured across the repo, not estimated: 3 hoists in 367 slots and tasks over 53 example
apps (all three in the demo itself), and 1 distinct opener across 85 unique authored
`then_say`/`ask` lines in the real customer agents — on a task whose tool is a no-op.

The gate is not the reason. Only 9 of those 85 lines are multi-sentence at all, and the
three short fixed openers it refused were `"Done — that's paid."`, `"You're verified."`
and a sign-off naming the brand — two outcome claims and a proper noun, all correctly
refused. Authors simply do not write a pleasantry in front of post-tool copy.

So treat this as latent until proven otherwise on a real agent. The dead air is real —
the larger of the two shipped agents sets `filler_say` on none of its seventeen
tool-backed tasks, and `while_running` on none either — but this pass can only
reschedule copy that already exists, and that copy is not there.
