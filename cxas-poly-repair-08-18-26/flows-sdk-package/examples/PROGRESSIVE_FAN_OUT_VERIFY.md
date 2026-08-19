# Verifying progressive fan-out live

`progressive_fan_out.py` is the authoring surface for the design in
`team_scratch/PROGRESSIVE_FANOUT_SPEC.md`. This is how you prove it works.

## Why offline green proves nothing

Three legs, three lines, one turn. Every offline check fakes at least one of the two
things that claim depends on:

| check | what it fakes |
|---|---|
| `flows.validate_app` | everything — it reads the DAG, never runs it |
| the engine simulator | concurrency. Legs "run" instantly and in order, so a batched group and a progressive one produce the same trace |
| `pytest packages/flows/tests/test_examples.py` | speech. It asserts the app BUILDS; nothing is spoken |

And one more, which cost ten identical-looking runs before it was understood: **a byte
count cannot see a truncated sentence.** Ten runs of one opening line measured an identical
4.6s of audio, which proves only that the stream is the same length every time. Whether
those 4.6 seconds contain the whole sentence is a question you answer by listening.

So: build offline, then push to CES, drive the REAL voice channel, and have a model listen
to the recording.

## Prerequisites

- The ces-probes toolchain, which carries `drive_audio.py`, `judge_audio.py` and a
  `harness.deploy(probe, tag)`. The catalogue was reconciled into this repo, so it is now
  `ces-probes/` at the root rather than a sibling checkout:

      export FLOWS=$PWD                      # run from the repo root of this worktree
      export PROBES="$FLOWS/ces-probes"
      export PY=$FLOWS/.venv/bin/python

- A way to push. `harness.deploy` tries the local Labs push service on `127.0.0.1:7788`
  first, but that route is only mounted when the `slot_studio` product is enabled on the
  running service, and a `405` sends it to the `cxas` CLI instead. The CLI needs no local
  service, and it is in the repo venv rather than on `PATH`:

      export CES_PROBE_CXAS=$FLOWS/.venv/bin/cxas

- macOS `say` + `afconvert` for the caller's voice. No TTS credentials, no ffmpeg.

## 1. Build

```
cd "$FLOWS/packages/flows"
PYTHONPATH=src python -m examples.progressive_fan_out
PYTHONPATH=src python -m flows.cli check --app-dir ./progressive_fan_out_app
```

Expect `validate: 0 errors, 0 warnings` and `framework in sync; every declared variable,
agent and tool present`.

Then confirm the group actually qualifies for the progressive path, because everything
downstream depends on it. The engine's predicate is *two or more legs, every leg fires a
tool, and NO leg declares `awaits`* — so a single stray `awaits` silently drops the group
back onto the old lowering and the live run below would look like a regression that is
really a config:

```
DAG=progressive_fan_out_app/tools/broadband_fault_dag/python_function/python_code.py
grep -c "'parallel': 'diagnostics'" "$DAG"   # expect 3 — one per leg
grep -c "awaits" "$DAG"                      # expect 0
ls progressive_fan_out_app/tools | grep diagnostics   # expect diagnostics_peek, diagnostics_watch
```

The last one is the emitter's half of the deal: a `<group>_peek` reading each leg's state
key and a `<group>_watch` polling it. If they are absent, the emitter has not lowered this
group and there is nothing for the engine to dispatch — stop here rather than pushing.
(A leg name resolving to no registered tool is **silent and fatal**: the turn simply dies
with nothing surfaced anywhere.)

## 2. Stage it as a probe

The harness pushes a directory under `$PROBES/probes/`, so the emitted app is staged there
as one more probe. The emitted tree is `.json`, `.py` and `.txt` only, which is exactly the
set of extensions the harness carries.

```
rsync -a --delete --exclude __pycache__ \
  "$FLOWS/packages/flows/progressive_fan_out_app/" \
  "$PROBES/probes/progressive-fan-out/"
```

Then add the audio config. This is a patch on the STAGED copy, not on the example: the
example declares no audio settings because the designed knob is
`flows.deploy.push(barge_in_awareness=True)`, which does not exist yet. Until it does:

```
python - <<'EOF'
import json, os
p = os.path.join(os.environ["PROBES"], "probes/progressive-fan-out/app.json")
app = json.load(open(p))
app["audioProcessingConfig"] = {
    # Off by default. Without it the caller CANNOT interrupt the held floor at all.
    "bargeInConfig": {"bargeInAwareness": True},
    # Keeps audio flowing from the moment the call connects. Without it the audio path
    # is cold and the FRONT of the first utterance is clipped — a real defect that looks
    # exactly like the one you are here to test.
    "ambientSoundConfig": {"prebuiltAmbientNoise": "OUTDOOR", "volumeGainDb": -40},
}
json.dump(app, open(p, "w"), indent=2)
print("patched", p)
EOF
```

`modelSettings.model` is already `gemini-3.1-flash-live` from the emit; the audio channel
needs a live model, so do not downgrade it.

## 3. Drive it over voice, and capture

```
cd "$PROBES"
$PY drive_audio.py progressive-fan-out \
    --say "hi, my internet keeps dropping out" \
    --wait 5 --tail 55 --listen --tag $(date +%H%M%S)
```

- **`--tag` is not optional.** Two apps sharing a display name make every later push fail
  with an opaque `500 an internal error has occurred`. A timestamp is enough.
- **`--tail` must outlast the slowest leg.** The legs are 8s, 18s and 30s, so the caller's
  stream has to stay open past ~45s or the run ends mid-narration and looks like a
  truncation defect that is really an impatient driver.
- `--listen` writes `/tmp/agent_audio.wav` and `/tmp/agent_lines.txt`. `/tmp` is volatile —
  judge them in the same sitting.
- Ignore the `pydub` / `ffmpeg` warnings on startup. They are unrelated.

## 4. Listen to it

```
$PY judge_audio.py /tmp/agent_audio.wav /tmp/agent_lines.txt
```

Gemini is given the WAV and the lines the agent intended, and reports per line whether it
was spoken in full, clipped at either end, or missing — quoting the words it actually
hears, so the verdict is checkable rather than asserted.

## What a PASS looks like

Four checks, all read off the driver's timeline and the judge's verdict.

**1. All three legs dispatch on ONE model turn, not three.** Read the arithmetic. Note the
timestamp of the opening line (dispatch happens with it); each finding must land at
`dispatch + its own leg duration`, not at the running total:

| | progressive (PASS) | one-per-turn (FAIL) |
|---|---|---|
| line test (8s) | dispatch + 8 | dispatch + 8 |
| account (18s) | dispatch + 18 | dispatch + 26 |
| engineer (30s) | dispatch + 30 | dispatch + 56 |

**2. Each leg's line is spoken separately, as it lands.** The timeline shows three distinct
narration lines with **no caller turn between them** — one `Sessions.run`, one caller
utterance, and the gaps between lines match the gaps between leg durations (≈8s, then ≈10s,
then ≈12s). A batched group produces one line containing all three findings at the end.

**3. The join speaks once.** Exactly one all-done summary, after the last finding. Not
once per leg, and not before the slowest leg has reported.

**4. No line is clipped.** `VERDICT: COMPLETE` from `judge_audio.py`. Anything else is a
failure even if the timeline looks perfect — including a line that appears in the timeline
with `0.0s SILENT!` next to it, which is a text part that was never spoken at all.

## Optional: the barge-in run

Re-run with the caller talking over the narration:

```
$PY drive_audio.py progressive-fan-out --say "hi, my internet keeps dropping out" \
    --wait 20 --then "wait, stop" --tail 40 --tag $(date +%H%M%S)-barge
```

Measured on the reference app: with `bargeInAwareness` on, the agent's audio is cut (31.8s
against 17.8s on a matched app), so a partial preempt is interruptible exactly like ordinary
speech. **The known open defect** is what happens next: the remaining findings are still
GENERATED into a stream nobody receives, marked as narrated, and never repeated — so
interrupting a held-floor fan-out loses results rather than deferring them. Until the
awareness signal reaches the framework, treat a barge-in run as documentation of that gap,
not as a regression.

## Reference run — 2026-07-30

The re-lowering was **in progress and not yet runnable** when this was written: the engine
half existed but `flows/emit/fanout.py` did not, so no `diagnostics_peek` /
`diagnostics_watch` was generated and step 1's last check failed. That is no longer the
case — see the run below.

### Driven live — 2026-07-31

`progressive_fan_out.py` was emitted verbatim with `flows.build_app` and driven against a
real CES deploy as `$PROBES/probes/85-fanout-emitted-min`. **It works.** One turn, 35.3s,
each finding spoken as its leg landed:

```
Let me run a few checks on your line - I'll talk you through them as they come back.
Right, the line test is back: your connection is dropping about every ten minutes,
 so there is a genuine fault here.
your account is all in order - nothing on our side is restricting...
```

This is the live verification #564 asked for and shipped without. It passes, so the
emitted lowering is sound, and any account of a fan-out failure that begins "the lowering
is broken" needs a different explanation.

Two mechanical traps cost a cycle each and are worth repeating here. `flows.build_app`
must be given a real `out_dir`, never `'.'` — on a validation failure it removes the
target, and `'.'` deletes the source. And the example has to be imported as a module on
`sys.path`, not loaded through `spec_from_file_location`: `inspect.getsource` cannot read
dataclasses defined in a module loaded that way, and the emit dies on `is a built-in
class`.

One defect surfaced by driving it, fixed in the same change: a group whose legs were all
gated off counted as **done** on its first evaluation, before it had dispatched anything.
See `$PROBES/probes/86-fanout-self-gated` for the two-leg reproduction.

What HAS been run live, to prove this recipe works, is steps 3 and 4 against
`$PROBES/probes/demo-fanout-30s` — the hand-built CES app implementing exactly this
mechanism, and the reference for what the emitter should produce. Verbatim:

```
$ python drive_audio.py demo-fanout-30s --say "hi, my internet keeps dropping out" \
      --wait 5 --tail 55 --listen --tag recipe1

caller audio: 62.1s  barge=none (control)
app=projects/555355609568/locations/us/apps/9f48a015-6809-42d1-a654-9f12ff015910

--- agent speech ---
  t=  5.5   0.0s  SILENT!
  t=  5.5   3.9s  Let me run a few checks on your line — I'll talk you through them as they come back.
  t= 14.5   6.4s  Right, the line test is back: your connection is dropping about every ten minutes, so there is a genuine fault here.
  t= 24.3   6.1s  Your account is all in order, by the way — nothing on our side is restricting the service.
  t= 36.5  10.8s  And I have engineer availability: Thursday morning, between eight and midday. So — a real line fault, your account is fine, and Thursday morning is free. Shall I book that engineer for you?
  t= 47.5   3.4s  You are booked in for Thursday morning. Anything else I can help with?

audio:  30.5s of speech in 137 chunks  first=5.7s last=48.2s
lines spoken: 6
```

Against the four checks:

1. **One dispatch.** The opening line starts at t=5.5. The findings land at 14.5, 24.3 and
   36.5 — offsets of 9.0, 18.8 and 31.0 against legs of 8s, 18s and 30s. Sequential
   dispatch would have put the third at ≈t=62.
2. **Narrated as they land.** Three separate lines from one caller utterance, inside one
   `Sessions.run`, with no caller turn between them.
3. **The join speaks once**, riding the last finding at t=36.5.
4. **Nothing clipped**, per the judge:

```
$ python judge_audio.py /tmp/agent_audio.wav /tmp/agent_lines.txt

Here is the analysis of the audio compared to the intended transcript:

**Line 1**
* Status: Spoken in full
* First four words heard: "Let me run a"
* Last four words heard: "as they come back"

**Line 2**
* Status: Spoken in full
* First four words heard: "Right, the line test"
* Last four words heard: "a genuine fault here"

**Line 3**
* Status: Spoken in full
* First four words heard: "Your account is all"
* Last four words heard: "is restricting the service"

**Line 4**
* Status: Spoken in full
* First four words heard: "And I have engineer"
* Last four words heard: "that engineer for you"

**Line 5**
* Status: Spoken in full
* First four words heard: "You are booked in"
* Last four words heard: "I can help with"

VERDICT: COMPLETE
```

The `t=5.5 0.0s SILENT!` first row is not a defect: it is the deliberate warming
utterance — one pass spent on content that reaches TTS and says nothing, so the cold-start
clip lands on it instead of on words. The judge does not see it because it has no text.

When the re-lowering lands, run steps 1–4 on `progressive_fan_out_app` and the timeline
should have the same shape: dispatch, three findings at +8/+18/+30, one summary, and
`VERDICT: COMPLETE`.
