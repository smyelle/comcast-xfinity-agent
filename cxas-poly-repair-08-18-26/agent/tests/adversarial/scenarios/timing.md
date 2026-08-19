# TIMING

Silence, hold durations, barge-in, and utterances that land in awkward places.

**App:** `projects/ces-deployment-dev/locations/us/apps/02092817-7278-4097-8f45-b144b4e04157`
(`adv-timing`), built `SPIKE_DEMO=1 SPIKE_LOCAL_SPECIALISTS=1 python build.py --out
./built_advtiming` off `072c533`, pushed once and not touched again. Nothing shared with
another family's app; every drive below ran serially against this one instance.

**Driver:** `tests/demo_voice.py`. 49 drives.

**Account:** `8069100230359946` (`all_clear` — the journey with the wait), cold, every run.
No `--var`, no `--fakes`, no `--fake-context`.

## The clock this family is measured against

Established over the 18 drives that reached a verdict, so the scenarios below can quote a
margin rather than an outcome. All times are relative to the agent's first spoken line, as
`_print` reports them from the platform's own `event_time`.

| Event | Line | Time |
| --- | --- | --- |
| greeting | "To get started, could you please tell me your Xfinity account number…" | 0.0s |
| account acknowledged | `SAY_BRIDGE_ACK` + `SAY_BRIDGE_TO_SWEEP_REST` + `ASK_WIFI_SCOPE_EARLY` | 16.6 – 17.9s |
| first reassurance | `SAY_SWEEP_WAITING[0]` | 33.5 – 36.1s |
| verdict | `SAY_ALL_CLEAR` | **42.5 – 47.3s** |
| walkthrough re-ask | `wifi_walkthrough` slot ask | 65.6 – 70.5s |

Two numbers matter for everything below.

**The verdict lands 25.2 – 30.4s after the account number is acknowledged.** Not the 8.3s
the `_specialists_task` docstring measures for the job itself — the job's answer is
quantised onto the tick grid, and the grid is the cost.

**The observed tick spacing is ~5.8s, not 5s, and it is not uniform.** The gap from the
scope question to the first reassurance was 16.5 – 18.6s across 18 runs — three ticks,
because `_QUIET_TICKS = 2` (app.py:210) puts two silent rungs in front of the line. The
comment there sizes that as "about six seconds at a 3s timeout". At the timeout this app
actually runs, those two silent rungs cost the caller **seventeen seconds of dead air
immediately after being asked a question**, which is the opposite of what they were added
for.

---

### TIMING-01  The 30s coin flip

**Why this should break it:** the verdict is delivered on an inactivity tick, and the sweep
finishes between ticks. If the caller's silence runs out in the window between the job
completing and the next tick that could speak about it, there is no turn left for the
verdict to ride. 30s of dead air is close enough to the 25 – 30s the verdict needs that the
outcome should be a coin flip rather than a behaviour.

**Setup:** app `02092817`, account `8069100230359946`, voice, cold. Six drives, spaced,
serial.

**Caller script:** "my internet is not working" → 11s → "my account number is 8 0 6 9 1 0 0
2 3 0 3 5 9 9 4 6" → **30s of silence** → 10s of silence → 10s of silence.

**Expected:** `HandleAllClear` fires and the caller hears `SAY_ALL_CLEAR` (scripts.py:494) —
"Everything on our side looks healthy… Would you like me to walk you through a few things to
try?" — before anything else is said about the Wi-Fi.

**Observed:** 1 run in 6 delivered it. On the other five the verdict was **never spoken at
all**, on any later turn. What the caller got instead, in place of a diagnosis:

```
  --- turn 634
     17.6s  < Thanks.
     17.6s  < Give me just a moment while I check your connection.
     17.6s  < While those checks run, one thing that helps either way. Is everything having
              trouble connecting, or just one device?
     36.5s  < Still running those checks — thanks for bearing with me.
[caller] ... 10s of silence ...
  --- turn 81
      0.0s  < Would you like to try a few troubleshooting steps together?
[caller] ... 10s of silence ...
              (nothing)
```

The `--engine` drive of the same shape (`flip30_4e`) shows why this is worse than a late
verdict. The cumulative `sm["_log"]` for the whole call contains **no `Settle` task, no
verdict rung and no `wifi_offered` flag** — the tag set for the entire session is:

```
announce  answer_first_armed  async_await_idle_hold  async_await_line  async_await_resolved
async_await_started  async_completion_ingested  bootstrap_stored  config_resolved
fanout_*  fill_slots  filler_partial_*  no_input_reprompt  no_input_silent_tick
option_cue_fill  parallel_fire  preemption  progress  remote_poll  setter_stored
settle_guard_dispatched  stale_scan_withheld  task  task_completed  task_outputs_partial
```

`async_completion_ingested` is there; `Settle` never runs after it. So the sweep result
arrives and is then dropped on the floor, and the agent goes on to offer in-home
troubleshooting anyway — the exact thing `WALKTHROUGH_SAFE` and the `_ALL_CLEAR_STATUSES`
gate exist to make impossible. The caller is offered Wi-Fi steps by an agent that has told
them nothing about their line.

**Verdict:** FAIL

**Defect:** two, stacked.

1. **The verdict is lost, not delayed.** When the caller's silence ends inside the window
   between `async_completion_ingested` and the tick that would run `Settle`, the ingested
   result is never settled and `HandleAllClear` never fires. There is no later recovery: the
   remaining turns go to the `no_input` ladder. The caller sits through a 43-second
   diagnostic and is never told the outcome. app.py:420 (`_settle_task`) is where the
   condition stops being satisfiable; app.py:396 (`answer_first=1`) is the most likely
   accomplice, since it arms with `spoke: False` on a call where nobody spoke.
2. **The troubleshooting offer escapes its safety gate.** With no verdict and no
   `wifi_offered`, the agent still asks a walkthrough question. The wording is improvised —
   "Would you like to try a few troubleshooting steps together?" appears nowhere in
   `scripts.py` or `app.py`; the authored line is app.py:993, "Would you like to try a few
   things with me?", which one run in six produced verbatim. Severity is high: an agent that
   has silently dropped its telemetry is recommending in-home fixes.

**Reproduced:** 5/6 spaced (`baseline30_a`, `flip30_1`, `flip30_2`, `flip30_3`,
`flip30_4e` lost the verdict; `flip30_5` delivered it).

---

### TIMING-02  Hold right at the boundary

**Why this should break it:** if TIMING-01 is a race rather than a bug, there is a hold
duration above which it stops racing. Finding that number turns a coin flip into a
specification.

**Setup:** as above. One drive per duration, plus repeats at the flip point.

**Caller script:** identical to TIMING-01 with `--hold` set to 25, 30, 31, 32, 33, 36, 40,
43, 45, 48, 54, 125, 150.

**Expected:** the verdict at every duration ≥ the 25 – 30s the sweep needs.

**Observed:**

| hold after the account number | drives | verdict delivered | verdict landed at |
| --- | --- | --- | --- |
| 25s | 1 | 0/1 | — |
| **30s** | **6** | **1/6** | 43.3s (the one) |
| **31s** | **3** | **3/3** | 43.1 / 43.3 / 43.4s |
| 32s | 1 | 1/1 | 42.5s |
| 33s | 2 | 2/2 | 43.1 / 45.8s |
| 36 / 40 / 43 / 45 / 48 / 54 / 125 / 150s | 12 | 12/12 | 42.8 – 47.3s |

**The flip is between 30 and 31 seconds.** Below it the verdict is lost outright; at and
above it, 21 for 21.

That boundary is not where the arithmetic puts it, and the gap is the finding. The median
verdict lands 26.0s after the account acknowledgement, which fits inside 30s with four
seconds to spare — yet 30s fails five times in six. The stream needs roughly four seconds
of dead air *after* the verdict would be spoken before it will speak it. The margin a
caller actually has at 30s is therefore about half a second, and on the slow tail (47.3s
observed twice) it is negative.

**Verdict:** FAIL

**Defect:** the same lost-verdict defect as TIMING-01, now bounded. The agent requires
**31 seconds of continuous caller silence** to deliver a diagnostic it advertises as taking
"just a moment". Any caller who speaks, coughs, or is cut off before then loses the result
of the sweep permanently. Severity: this is the ordinary case, not an edge — 30s is a very
normal amount of patience.

**Reproduced:** 30s → 5/6 spaced. 31s → 3/3 spaced.

---

### TIMING-03  Barge-in on the reassurance

**Why this should break it:** `SAY_SWEEP_WAITING[0]` is spoken on an idle tick with no slot
pending, and the caller's answer to `ASK_WIFI_SCOPE_EARLY` is due at exactly the same
moment. A caller's answer has previously been discarded when it collided with a ladder line,
so the answer landing on the reassurance turn should be at risk.

**Setup:** as above. Four drives at holds bracketing the reassurance tick (34.5s absolute):
16s, 17s, 18s, 19s, 20s.

**Caller script:** complaint → 11s → account → *hold* → "just one device" → 10s of silence.

**Expected:** the scope answer fills `wifi_scope`, and the caller hears the acknowledgement
and early offer (`SAY_SCOPE_NOTED` + the folded offer) rather than the reassurance.

**Observed:** the answer survives, every time, at every offset. Two shapes, both correct.

Hold 16 / 17 / 18s — the buffer ends before the reassurance tick, and the reassurance is
correctly **suppressed** rather than queued:

```
     17.6s  < While those checks run, one thing that helps either way. Is everything having
              trouble connecting, or just one device?
[caller] 'just one device'
      0.0s  < Got it, that helps. While those checks finish, we could try a couple of quick
              things on that device. Want to give it a go?
```

Hold 19 / 20s — the reassurance fires first and the answer still lands intact:

```
     34.5s  < Still running those checks — thanks for bearing with me.
[caller] 'just one device'
      0.0s  < Got it, that helps. While those checks finish, we could try a couple of quick
              things on that device. Want to give it a go?
```

The device-specific wording ("on that device", and later "On the device that's struggling")
confirms `wifi_scope` was actually consumed, not merely acknowledged.

**Verdict:** PASS

**Reproduced:** 5/5 spaced (`barge_a` h16, `barge_b` h17, `barge_c` h18, `barge_d` h19,
`control_answer` h20).

---

### TIMING-03b  True acoustic barge-in

**Why this should break it:** the scenario above tests an answer on the turn *after* a
ladder line. It does not test the caller's voice overlapping the agent's audio, which is the
case that actually loses words on a phone.

**Setup / Observed:** not reachable with this harness. `demo_voice.py:298` sends every
caller turn as `text=utterance` on a fresh `sess.run`, so a caller turn can never overlap the
agent's output; and the only continuous audio buffer is the opening one, whose contents are
fixed (complaint, gap, account, silence). There is no way to place caller audio at 34.5s
inside that buffer without editing the driver, which this pass is not allowed to do.

**Verdict:** BLOCKED — the driver cannot produce overlapping audio. TIMING-03's PASS should
be read as "an answer on the turn after a ladder line survives", not "barge-in works".

---

### TIMING-04  The hesitation

**Why this should break it:** a known platform defect drops a caller's SECOND utterance
during an async wait — reproduced 5/5 elsewhere without a hesitation and 0/8 with one. This
agent has a question outstanding during the wait, so it is squarely exposed.

**Setup:** as above, hold 16s (mid-wait, before the verdict). Two shapes, plus two
discriminators.

**Caller script A (no hesitation):** … account → 16s → "just one device" → "yes please" →
10s of silence.
**Caller script B (hesitation):** … account → 16s → "uh" → 3s of silence → "just one device"
→ 10s of silence.

**Expected:** both shapes reach the same place — scope noted, offer made, offer accepted,
tip 1 spoken.

**Observed.** Shape A works, in full:

```
[caller] 'just one device'
      0.0s  < Got it, that helps. While those checks finish, we could try a couple of quick
              things on that device. Want to give it a go?
[caller] 'yes please'
      0.0s  < Let's start simple.
      0.4s  < On the device that's struggling, forget the home network in its Wi-Fi
              settings, then join it again. Did that help?
```

Shape B does not work. It does not drop the second utterance — **it kills the call**:

```
[caller] 'uh'
              (nothing)
[caller] ... 3s of silence ...
              (nothing)
[caller] 'just one device'
              (nothing)
[caller] ... 10s of silence ...
              (nothing)
```

The `--engine` drive shows the session is frozen, not merely quiet. `sm["_log"]` is
byte-identical on all four turns after "uh" — no `remote_poll`, no `fill_slots`, no
`no_input_reprompt`, nothing new at all. The log's last entry stays `stale_scan_withheld` on
every subsequent turn. No exception, no error frame, no session close: the agent simply
returns an empty response forever.

Two discriminators, to separate the hesitation from the pause:

* `hesit_um_nopause` — "um" with **no** pause, then "just one device". Same total wedge.
* `hesit_pauseonly` — 3s of silence with **no** filler, then "just one device". Works
  perfectly: reassurance at 55.1s, then "Got it, that helps. While those checks finish…".

So the pause is innocent. The filler word is the trigger. See TIMING-09 for the general form.

**Verdict:** FAIL

**Defect:** an utterance during the `Specialists` async wait that matches no cue for an open
slot causes the engine to return an empty response and **permanently wedge the session** —
every subsequent caller turn, speech or silence, is answered with nothing. The no-input
ladder does not fire, so the call never even ends honestly; the caller is left on a dead
line. This agent lands on the *worse* side of the known platform defect: elsewhere the
second utterance is merely lost, here the whole conversation is. Severity: highest in this
family. The trigger — a filler word during a 25-second wait — is what most callers do.

**Reproduced:** 3/3 spaced for "uh" (`hesit_uh`, `hesit_uh_b`, `hesit_uh_c`), plus 1/1 for
"um" without the pause. Shape A passed 2/2 (`hesit_none`, `hesit_none_b`).

---

### TIMING-05  Answer the scope question very late

**Why this should break it:** the scope question is asked at 17.6s and the verdict lands at
~45s. A caller who answers after the verdict is answering a question that has already been
superseded by a different one, and `EARLY_SCOPE_ASKABLE` (scripts.py:570) is no longer
satisfiable. Where does the answer go?

**Setup:** as above, hold 48s so the verdict lands first. Three drives.

**Caller script:** … account → 48s of silence → "just one device" → 10s of silence.

**Expected:** either the answer fills `wifi_scope` and is acknowledged, or the agent asks
again. Silently discarding it is the failure.

**Observed:** identical all three times. The verdict lands, and the late answer is met with
the walkthrough re-ask and nothing else:

```
     45.2s  < Everything on our side looks healthy. Your account, your area, the line into
              your home and your gateway all check out. That usually leaves the Wi-Fi inside
              your home as the most likely spot. Would you like me to walk you through a few
              things to try?
[caller] 'just one device'
      0.0s  < Would you like to try a few things with me?
```

The answer is not acknowledged, not repeated back, and produces no device-specific wording
anywhere afterwards — contrast TIMING-03, where the same three words produced "on that
device" and then "On the device that's struggling". The caller has answered a question the
agent asked forty seconds earlier and gets no sign that it was heard.

**Verdict:** PARTIAL

**Defect:** right outcome (the agent does not derail, and it correctly re-asks the question
that is actually outstanding), wrong route: the scope answer is dropped rather than
promoted. Because `wifi_scope` never fills, every downstream tip loses its device framing.
The caller's experience is that they were ignored. Low-to-moderate severity — nothing is
said that is untrue, but a turn of the caller's is discarded in silence.

**Reproduced:** 3/3 spaced (`late_answer`, `late_b`, `late_c`).

---

### TIMING-06  Total silence to exhaustion

**Why this should break it:** the caller never speaks after the account number. Every turn
for the rest of the call is a platform tick. Something has to end the call, and the question
is whether it ends it honestly or loops.

**Setup:** as above, one continuous silence of 125 – 150s after the account number — a
single audio buffer, so the platform's own timeout drives every turn. Three drives.

**Caller script:** complaint → 11s → account → **125s (or 150s) of unbroken silence**.

**Expected:** verdict, walkthrough offer, then the `wifi_walkthrough` slot's own ladder —
one reprompt, "Sorry, would you like to try a few things with me, or not?" (app.py:997),
`max_retries=2`, then `on_exhaust_fill="DECLINE"` (app.py:998) and the decline path.

**Observed:** it does end honestly, but by the wrong ladder, and the copy in the middle is
wrong in a way the caller will notice.

```
     44.6s  < Everything on our side looks healthy. … Would you like me to walk you through
              a few things to try?
     66.3s  < Would you like to try a few things with me?
     79.8s  < Sorry, I didn't catch that. Could you tell me your Xfinity account number or
              the phone number on the account?
     92.1s  < I still didn't get that. What's the account number or phone number?
    102.2s  < I'm having trouble hearing you. Let me connect you with someone who can help.
```

Four rungs heard, then the honest hand-off at 102 – 110s. But rungs two and three are
`_account_no_input`'s `reprompts` (app.py:599) — they ask for **the account number the
caller supplied 63 seconds earlier**, in the middle of a Wi-Fi walkthrough offer. The
`wifi_walkthrough` slot's own reprompt is never spoken and `on_exhaust_fill="DECLINE"` never
fires; the flow-level ladder pre-empts the slot-level one and takes the call to a transfer
instead of to the decline path.

The `no_input` tick spacing is also not the 5s the source assumes: 21.7, 13.5, 12.3, 10.1
seconds between rungs. The comment at app.py:630 computes the hold as `(rungs + 1) × 5s`;
observed, three rungs took 36 seconds.

**Verdict:** PARTIAL

**Defect:** right outcome (the call ends, with a truthful line and a hand-off), wrong route
and wrong copy. `no_input` is declared per flow and the `repair` flow's copy names the
account number, so silence anywhere in the flow — including 60 seconds after the account is
known and stored — re-asks for it. `app.py:599`. From the caller's seat the agent has
forgotten the entire conversation. Moderate severity: it is the second-worst thing this
family produces, and it is the *good* path.

**Reproduced:** 3/3 spaced (`exhaust_silence` h150, `exhaust_b` h125, `exhaust_c` h125).

---

### TIMING-07  Silence before the account number

**Why this should break it:** the reprompt ladder is flow-level. Before the account number
is known it is at least asking for the right thing, so this is the case it was written for —
but the count and the exit need verifying against `_account_no_input`.

**Setup:** as above, 75s and 95s of silence *between* the complaint and the account number.
Two drives.

**Caller script:** "my internet is not working" → **75s / 95s of silence** → account.

**Expected:** `_account_no_input["reprompts"]` (app.py:599) — one silent rung, then "Sorry, I
didn't catch that. Could you tell me your Xfinity account number or the phone number on the
account?", then "I still didn't get that. What's the account number or phone number?", then
`on_exhaust` "I'm having trouble hearing you. Let me connect you with someone who can help."

**Observed:** exactly that, verbatim, in order.

```
      0.0s  < To get started, could you please tell me your Xfinity account number or the
              phone number associated with your account?
     19.2s  < Sorry, I didn't catch that. Could you tell me your Xfinity account number or
              the phone number on the account?
     31.4s  < I still didn't get that. What's the account number or phone number?
     41.6s  < I'm having trouble hearing you. Let me connect you with someone who can help.
```

The first rung is silent as authored — nothing is spoken between 0.0s and 19.2s. The session
ends after the hand-off (the next `run` returns `failed_precondition`), so the account
number the caller finally read out at 76s / 96s was never heard. That is correct: they had
been transferred.

**Verdict:** PASS

**Reproduced:** 2/2 spaced (`silence_before` gap 75, `silence_before_b` gap 95).

---

### TIMING-08  Micro-answers at three depths

**Why this should break it:** a single word is the least information a caller can give, and
"yeah" is a listed `ACCEPT` cue (app.py:978) for a slot that is *gated shut* during the
wait. Whether it is heard should depend on where in the wait it lands.

**Setup:** as above. "yeah" at 5s, 20s and 45s of hold — before the bridge, mid-wait, and
after the verdict.

**Caller script:** … account → *hold* → "yeah" → 10s → 10s.

**Expected:** during the wait, `wifi_walkthrough` is shut by `WIFI_ANSWERABLE`
(scripts.py:838), so "yeah" answers nothing and the agent should carry on with the wait —
reassurance, then verdict. After the verdict the offer is open and "yeah" should accept it.

**Observed:** timing changes the interpretation completely, and two of the three depths are
fatal.

*5s* — bridge and scope question land, then "yeah", then nothing ever again:
```
     17.3s  < While those checks run, one thing that helps either way. Is everything having
              trouble connecting, or just one device?
[caller] 'yeah'   (nothing)
[caller] ... 10s of silence ...   (nothing)
[caller] ... 10s of silence ...   (nothing)
```

*20s* — reassurance lands, then "yeah", then nothing ever again. Same wedge.

*45s* — verdict lands first, and "yeah" is handled correctly:
```
     43.4s  < Everything on our side looks healthy. … Would you like me to walk you through
              a few things to try?
[caller] 'yeah'
      0.0s  < Got it.
      0.4s  < Is everything having trouble connecting, or just one device?
[caller] ... 10s of silence ...
      0.0s  < Is it all of your devices having trouble connecting, or just one?
```

**Verdict:** FAIL

**Defect:** the TIMING-04 wedge, reached by an entirely ordinary caller turn. A one-word
acceptance spoken any time before the verdict lands ends the conversation with no sound and
no hand-off. The 45s case also shows a smaller copy defect: the re-ask "Is it all of your
devices having trouble connecting, or just one?" is not `ASK_WIFI_SCOPE` (scripts.py:535),
which reads "Is everything having trouble connecting, or just one device?" — the model is
rewording approved copy on the re-ask.

**Reproduced:** 5s → 2/2 spaced (`micro5`, `micro5_b`). 20s → 2/2 spaced (`micro20`,
`micro20_b`). 45s → 1/1, PASS. Combined with TIMING-04 and TIMING-09 the wedge is 9/9.

---

### TIMING-09  Any off-cue utterance during the wait

**Why this should break it:** TIMING-04 and TIMING-08 both wedge on short utterances. If the
trigger were length or disfluency, a normal-length sentence should survive. If the trigger is
"matches no open slot", it will not. This separates them, and it is the difference between a
curiosity and a call-killer.

**Setup:** as above, hold 20s (mid-wait, after the reassurance, before the verdict).

**Caller script:** … account → 20s → *one ordinary sentence* → 10s of silence.

**Expected:** the agent has an outstanding question and a running job. Anything from a
re-ask of the scope question to the reassurance line is defensible. Silence is not.

**Observed:** three different sentences, three identical wedges.

```
     34.8s  < Still running those checks — thanks for bearing with me.
[caller] 'what is taking so long'
              (nothing)
[caller] ... 10s of silence ...
              (nothing)
```

```
     35.5s  < Still running those checks — thanks for bearing with me.
[caller] 'are you still there'
              (nothing)
[caller] ... 10s of silence ...
              (nothing)
```

"are you still there" is the cruellest confirmation available: the caller asks whether the
agent is still on the line, and the answer is that it is not, and never will be again.

Length and disfluency are both ruled out — five words, no filler, still dead. The surviving
utterances across this whole pass are "just one device" (a `wifi_scope` cue) and "yes
please" / "yeah" *after* the offer is open. Everything else spoken during the wait wedges.

**Verdict:** FAIL

**Defect:** the general form of the TIMING-04 defect, and the correct statement of it. During
the `Specialists` async wait, any caller utterance that does not match a cue for a currently
open slot produces an empty response and permanently wedges the session. The `awaits`
block's `answer_first=1` (app.py:396) reserves the turn for an answer, and when the turn does
not contain one there is nothing to fall back to — no re-ask, no reassurance, no no-input
ladder. `on_timeout` (app.py:397) never fires either, because the wedge stops the turns that
would advance `max_turns`. Severity: this is the single most damaging behaviour in the
family. It has no floor and no recovery.

**Reproduced:** 3/3 spaced (`offcue_long`, `offcue_b` — "what is taking so long";
`offcue_c` — "are you still there").

---

### TIMING-10  Silence after a hold request

**Why this should break it:** `_account_no_input` has a second, longer ladder for the caller
who says they are going to go and look for their number — `hold_ack` (app.py:649) and seven
`hold_reprompts` (app.py:642), sized to give them ~40s. The canonical trigger is documented
at app.py:624: "hold on, let me find my account number" contains "let me find". A caller who
says it in the same breath as the complaint is the case `hold_ack` was added for.

**Setup:** as above. Complaint carries the hold phrase; 95s of silence follows. Three drives,
plus a control with the plain complaint at the same 95s gap.

**Caller script:** "my internet is down. hold on, let me find my account number" → **95s of
silence** → account.

**Expected:** `hold_ack` speaks `SAY_TAKE_YOUR_TIME` (scripts.py:44) — "No problem, take your
time. I'll be here when you're ready." — instead of asking for the number. Then the seven
`hold_reprompts`, of which only the third is spoken ("Take your time. I'm still here whenever
you're ready."), then `on_exhaust`.

**Observed:** none of that. Four separate departures, identical across all three drives.

```
      0.0s  < To get started, could you please tell me your Xfinity account number or the
              phone number associated with your account?
     17.6s  < Sorry, I didn't catch that. Could you tell me your Xfinity account number or
              the phone number on the account?
     29.8s  < I still didn't get that. What's the account number or phone number?
     40.0s  < I'm having trouble hearing you. Let me connect you with someone who can help.
     42.0s  < 2026-03-30T18:28:00.000Z
     54.0s  < Thanks for calling Xfinity. What's going on with your service today?
```

1. `hold_ack` is never spoken. The agent asks for the account number anyway — "the one reply
   the request rules out", in the words of the comment at app.py:644.
2. The **seven-rung hold ladder never engages**. The caller gets the plain three-rung
   `reprompts` ladder, so the hold they actually receive is 40.0s of which 17.6s is silent —
   the same as a caller who said nothing at all. "Take your time. I'm still here whenever
   you're ready." is never spoken.
3. Two seconds after the hand-off line the agent **speaks a raw ISO timestamp out loud**:
   "2026-03-30T18:28:00.000Z".
4. Twelve seconds after that, the session **re-greets itself** — "Thanks for calling Xfinity.
   What's going on with your service today?" — having just told the caller it was connecting
   them to a human. `bootstrap={"reset_on_complete": True}` (app.py:661) is the likely
   mechanism.

The control isolates 3 and 4 to this path. With the plain complaint at the same 95s gap
(`silence_before_b`), the hand-off at 41.6s is the last thing said and the session ends
cleanly — no timestamp, no re-greeting.

**Verdict:** PARTIAL  *(was FAIL; re-verdicted 2026-08-14, see the note at the end of this entry)*

**Defect:** three, on one path.
* `hold_phrases` matching does not fire on an utterance that carries the complaint and the
  hold request together, so neither `hold_ack` nor `hold_reprompts` is reachable for the one
  caller shape they were written for (app.py:625, app.py:642, app.py:649). The 40s hold the
  comment at app.py:630 sizes so carefully is not delivered.
* A raw `2026-03-30T18:28:00.000Z` reaching TTS is an unrendered variable spoken to a
  customer. High severity — it is the kind of line that gets a demo stopped.
* Re-greeting after a hand-off tells the caller they were not transferred after all, and
  restarts a call they have already spent 54 seconds on. High severity.

**Reproduced:** 3/3 spaced (`holdphrase`, `holdphrase_b`, `holdphrase_c`), with the plain
complaint control clean 2/2.


**Re-verdicted PARTIAL, 2026-08-14** (was FAIL). Two of the three defects are gone; the
first is not.

Defects 2 and 3 — the raw `2026-03-30T18:28:00.000Z` and the re-greeting after the
hand-off — did not recur on any drive. `_account_no_input`'s `on_exhaust` now carries
`end_conversation: True`, which fires the `then` tool AND emits the `end_session` part;
without it the engine set `sm["status"]` and CES, which ends a call on the part rather
than on `sm`, kept the session open with a model and a tool result and nothing to do. The
silence-exhaust path replayed 3/3 spaced (plain complaint, 95s):

```
  0.0s  < To get started, could you please tell me your Xfinity account number or the
          phone number associated with your account?
 18.4s  < Sorry, I didn't catch that. Could you tell me your Xfinity account number or
          the phone number on the account?
 30.6s  < I still didn't get that. What's the account number or phone number?
 40.2s  < I'm having trouble hearing you. Let me connect you with someone who can help.
```

That line is the last thing said, on all three.

Defect 1 — `hold_ack` and the seven-rung `hold_reprompts` ladder never engaging — is
UNCHANGED and was not attempted. Replaying this row's own script 3/3 spaced now takes a
different route again: the agent answers the complaint without asking for the account
number at all, so `_awaiting` is never the account slot and no silence ladder of either
kind engages. What follows is three model-owned turns ("Are you still there? …", "It looks
like we got disconnected. Please give us a call back if you still need help with your
internet. Goodbye."), none of which appears in any source file — the unowned-turn class
this pass logged as WT-11. No timestamp and no re-greeting on any of the three, but the
route is not the recorded one, so these drives are not evidence about `end_conversation`
either way. The plain-complaint drives above are.

---

### TIMING-11  Dead air after the scoping question

**Why this should break it:** `_QUIET_TICKS = 2` (app.py:210) inserts two silent rungs in
front of the reassurance ladder, so the caller is not talked over while they think. The
comment sizes that as "about six seconds at a 3s timeout". If the deployed timeout is not 3s,
the two rungs buy silence the design did not intend to sell.

**Setup:** measured across every drive in this file that reached the reassurance line — 18
of them.

**Caller script:** any; the measurement is the gap between `ASK_WIFI_SCOPE_EARLY` and
`SAY_SWEEP_WAITING[0]` on the same continuous buffer.

**Expected:** ~6s of quiet, per app.py:209.

**Observed:** **16.5 – 18.6s**, mean 17.4s, across 18 runs. Every single one over 16
seconds; none anywhere near 6.

```
     17.6s  < While those checks run, one thing that helps either way. Is everything having
              trouble connecting, or just one device?
              ( 17.4 seconds of nothing )
     35.0s  < Still running those checks — thanks for bearing with me.
```

**Verdict:** PARTIAL

**Defect:** right intent, wrong magnitude. The two silent rungs are working exactly as
written; the arithmetic in the comment is stale, because the tick this app actually runs on
is ~5.8s rather than 3s. The result is that a caller who has just been asked a direct
question hears seventeen seconds of silence before anything else happens — on a call whose
entire design premise is that the wait should not be dead air. This is also the mechanism
behind TIMING-01: three ticks of quiet is most of the budget a 30s hold has.

**Reproduced:** 18/18.

---

## Tally

| Scenario | Verdict | Reproduced |
| --- | --- | --- |
| TIMING-01 The 30s coin flip | FAIL | 5/6 |
| TIMING-02 Hold right at the boundary | FAIL | 5/6 at 30s, 3/3 at 31s |
| TIMING-03 Barge-in on the reassurance | PASS | 5/5 |
| TIMING-03b True acoustic barge-in | BLOCKED | — |
| TIMING-04 The hesitation | FAIL | 3/3 |
| TIMING-05 Answer the scope question very late | PARTIAL | 3/3 |
| TIMING-06 Total silence to exhaustion | PARTIAL | 3/3 |
| TIMING-07 Silence before the account number | PASS | 2/2 |
| TIMING-08 Micro-answers at three depths | FAIL | 4/4 wedge, 1/1 pass at 45s |
| TIMING-09 Any off-cue utterance during the wait | FAIL | 3/3 |
| TIMING-10 Silence after a hold request | FAIL | 3/3 |
| TIMING-11 Dead air after the scoping question | PARTIAL | 18/18 |

**PASS 2 · FAIL 6 · PARTIAL 3 · BLOCKED 1**

## What this pass could not test

* **True acoustic barge-in** (TIMING-03b). The driver cannot overlap caller audio with agent
  audio.
* **The real sweep duration.** This is a `SPIKE_DEMO` build, where the specialists answer from
  a fixture behind a recorded `DEMO_SLEEP_SECONDS = 20.0` sleep (build.py:949). The verdict
  landed at 42.5 – 47.3s of call time, so the tick-grid arithmetic in TIMING-01/02 should
  hold on the real backends — but the 31-second boundary is measured against a 20s job, not
  a 43 – 54s one, and would have to be re-measured against the shipped build.
* **DTMF.** Out of family, and not exercised.
* **Whether the wedge survives a real telephony leg.** Every wedge above was observed on the
  bidi API with `text=` caller turns. It is not proven that a caller speaking into a phone
  triggers the same path, though nothing in the engine log suggests the input surface matters.
