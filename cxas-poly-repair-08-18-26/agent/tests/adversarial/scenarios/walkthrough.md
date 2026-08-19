# THE WI-FI WALKTHROUGH

The scope question asked during the sweep, the offer that rides its acknowledgement, and
the three tips that follow. Everything from `ASK_WIFI_SCOPE_EARLY` to `SAY_WIFI_EXHAUSTED`.

**App:** `fc60c7ed-436a-46fd-851b-619fba87f8c0` (`adv-walkthrough`, project
`ces-deployment-dev`, location `us`). Built `SPIKE_DEMO=1 SPIKE_LOCAL_SPECIALISTS=1`,
pushed once, driven serially — never two calls in flight.

**Instrument:** `tests/demo_voice.py`, voice, cold, account `8069100230359946`
(`all_clear` — the journey with the wait in it).

**The timing knob, and it matters.** The sweep reports ~43s after first audio. The
opening buffer is complaint + 11s + account + `--hold`, so `--hold` decides which side of
the verdict the caller's first word lands on:

* `--hold 12` — the first utterance lands ~30s in, **during** the sweep. The early
  acknowledgement-and-offer (`AckScopeEarly` / `AckScopeEarlyAll`) owns the turn, and
  `wifi_offered_early` is the latch that gets set.
* `--hold 30` — the first utterance lands ~47s in, **after** the verdict. `HandleAllClear`
  has already spoken the offer, and `wifi_offered` is the latch that gets set.

Those two paths are not equivalent, and most of what is broken below is broken on the
early one only. Every scenario says which hold it used.

**Provenance.** Every spoken line in every transcript was checked mechanically against the
string literals in `scripts.py`, `app.py`, `clarify.py`, `guardrails.py` and
`source_tools.py` rather than by eye. Three lines failed that check, all in WT-11.

**Totals:** 14 scenarios — **5 PASS, 7 FAIL, 2 PARTIAL, 0 BLOCKED.**

---

### WT-01  Contradict the scope mid-walkthrough

**Why this should break it:** `wifi_scope` is filled once and the tips are gated on its
value (`_wifi_tip(..., scope=["ONE_DEVICE"])`, scripts.py:988-996). Nothing in the flow
re-opens it, so a caller who corrects themselves should be stuck on the branch they first
named — and the branch they are stuck on contains the one tip the source says is wrong for
them.

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "just my laptop" / "yes please" / "actually everything's down, nothing
works" / "no change"

**Expected:** the correction re-scopes the walkthrough, or at minimum the whole-house tips
(`SAY_WIFI_TIP_PLACEMENT`, `SAY_WIFI_TIP_NEARBY`) take over. `SAY_WIFI_TIP_CLOSER` must
NOT be spoken — scripts.py:987 says in as many words that "moving closer is meaningless
for a caller whose whole house is offline, so it is scoped to the one-device answer".

**Observed:**

```
[caller] "actually everything's down, nothing works"
      < Next one.
      < Try moving closer to your gateway, and check nothing large or metal is sitting
        right against it. Did that make a difference?
```

No acknowledgement of the correction. The one-device ladder ran to completion
(`SAY_WIFI_TIP_CLOSER`, then `SAY_WIFI_TIP_TOGGLE`), and the whole-house tips never became
eligible.

**Verdict:** PASS  *(was FAIL; re-verdicted 2026-08-14, see the note at the end of this entry)*

**Defect:** `wifi_scope` is write-once. The hook's promotion refuses to overwrite
(hooks.py:235, `if _early and not filled.get("wifi_scope")`), and the `wifi_scope` slot is
already filled so it is not collected again — so a later, more accurate statement of scope
cannot reach the tips. The caller hears the exact advice the design set out to withhold
from them, one turn after telling the agent their whole house is down. Severity: high —
this is the failure mode the two-branch tip split exists to prevent, reached by the
commonest correction a caller makes.

**Reproduced:** 3/3 spaced, two phrasings ("actually everything's down, nothing works"
×2, "wait, the whole house is down actually" ×1).


**Re-verdicted PASS, 2026-08-14** (was FAIL). A scope correction now has an owner. The
promotion in `before_agent` runs before the engine reads the turn, so it can only ever
promote the answer the caller is correcting — and once it has, `wifi_scope` is filled and
`wifi_scope_early` is latched, so no slot is left that can take the new one. The
correction is therefore made in `before_model`, the only hook holding this turn's
utterance, deterministically and behind three guards (overwrite only, value must differ,
exactly one cue value must match). Replayed with this row's own script and setup, 3/3
spaced:

```
[caller] 'just my laptop'
< Got it, that helps. While those checks finish, we could try a couple of quick things on
  that device. Want to give it a go?
[caller] 'yes please'
< On the device that's struggling, forget the home network in its Wi-Fi settings, then
  join it again. Did that help?
[caller] "actually everything's down, nothing works"
< Since it's everything, let's look at the gateway itself. Is it out in the open and
  upright, rather than in a cabinet, behind the TV or down on the floor? Moving it
  somewhere clear can make a real difference. Did that change anything?
[caller] 'no change'
< Try one device right next to the gateway and see if it behaves any differently there.
```

Placement then nearby: the whole-house ladder, and "moving closer" — the one tip the
source scopes away from whole-house callers — is never offered. `tests/scope_check.py`
holds the rule offline, including that the cue map the hook has to carry its own copy of
cannot drift from `app.WIFI_SCOPE_CUES`.

---

### WT-02  A scope answer the cues do not match

**Why this should break it:** `wifi_scope_early` is `setter=""`, cue-only (app.py:1031).
If the cues miss, nothing is filled, so `SCOPE_NOTED_*` cannot match and the answer turn
has no owner. scripts.py:584-596 records that the model then filled the gap with an
invented in-home diagnosis. The two rungs were added to close that hole; what happens now
when the cues still miss?

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "a couple of things" / "yes please" / 20s silence / "hello, are you
there" / 20s silence  (also run with "most of them")

**Expected:** the answer turn is owned by something. Either a cue or the classifier lands a
value and `AckScopeEarly` speaks, or the question is re-put after the verdict by
`AskWifiScope`, or — worst acceptable case — the caller hears the reassurance line.

**Observed:** nothing at all. Not one word from the scope answer to the end of the call.

```
      < While those checks run, one thing that helps either way. Is everything having
        trouble connecting, or just one device?
[caller] 'a couple of things'
[caller] 'yes please'
[caller] ... 20s of silence ...
[caller] 'hello, are you there'
[caller] ... 20s of silence ...
```

110 seconds of dead air on an open line, through two inactivity windows that should have
produced ticks and through the point the sweep should have reported.

**Control, same timing, cue-matching answer** (WT-03 below): "the TV and my phone but the
laptop's fine" at the identical hold produced the acknowledgement, the offer and the first
tip. So this is the cue miss, not contention and not the app being unwell.

**Verdict:** FAIL

**Defect:** an unmatched answer to `ASK_WIFI_SCOPE_EARLY` wedges the call silently. The
turn has no owner (no rung eligible, `wifi_scope_early` cue-only so the model cannot fill
it, `AskScopeEarly` already latched so the question is not re-put), and the wedge is not
transient — the walkthrough never opens, and the verdict never arrives to break it either
(see WT-13). Severity: highest of anything here. The caller is not misadvised, they are
abandoned mid-call, and the phrases that trigger it ("a couple of things", "most of them")
are ordinary English answers to the question just asked.

**Reproduced:** 3/3 spaced across two phrasings.

---

### WT-03  Ambiguous scope: two devices named, one working

**Why this should break it:** `WIFI_SCOPE_CUES` (app.py:949) has a lookahead carve-out for
"everything else", but nothing for a caller who enumerates. "my phone" is a `ONE_DEVICE`
cue and `cue_priority="first"` takes the earliest declared value, so a two-device answer
should quietly become one device.

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "the TV and my phone but the laptop's fine" / "yes please" / "no change"

**Expected:** defensible either way — one device is arguably right (the laptop works, so it
is not the whole house) — but the copy should not name a single device the caller does not
have.

**Observed:**

```
[caller] "the TV and my phone but the laptop's fine"
      < Got it, that helps. While those checks finish, we could try a couple of quick
        things on that device. Want to give it a go?
[caller] 'yes please'
      < On the device that's struggling, forget the home network in its Wi-Fi settings,
        then join it again. Did that help?
```

**Verdict:** PARTIAL

**Defect:** right branch, wrong words. "on that device" and "the device that's struggling"
are singular, spoken to a caller who has just named two. This is exactly the complaint
`SAY_SCOPE_NOTED_ALL_DEVICES` was written for (scripts.py:620-628 — "'on that device' is a
promise about which device, made to someone who has just said there is not one"), and the
two-value enum has no third case for it. Severity: low — cosmetic, and the advice is
actionable on either device.

**Reproduced:** 1/1.

---

### WT-04  Accept the EARLY offer and take all three tips

**Why this should break it:** this path was broken recently — `verdict_scope_noted_all` was
unregistered, the emitter substituted a stub, the latch came back `'15400'` and the
walkthrough fell through to the model. Confirm every tip is authored and quoted.

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "just one device" / "yes please" / "that didn't work" / "still nothing"
/ "no change"

**Expected:** `SAY_SCOPE_NOTED` → `SAY_WIFI_TIP_REJOIN` → `SAY_WIFI_TIP_CLOSER` →
`SAY_WIFI_TIP_TOGGLE` → `SAY_WIFI_EXHAUSTED`, each verbatim, one per turn, each preceded by
its own fixed filler.

**Observed:** exactly that.

```
      < Got it, that helps. While those checks finish, we could try a couple of quick
        things on that device. Want to give it a go?
      < Let's start simple.  On the device that's struggling, forget the home network in
        its Wi-Fi settings, then join it again. Did that help?
      < Next one.  Try moving closer to your gateway, and check nothing large or metal is
        sitting right against it. Did that make a difference?
      < Try this one.  Turn the device's Wi-Fi off and back on again. On a phone, airplane
        mode for a few seconds does the same thing. Any change?
      < That's everything I can try from here. Let me get you to someone who can take a
        closer look at your home setup.
```

Every line traces: `SAY_SCOPE_NOTED`, `FILLER_TIP_REJOIN`+`SAY_WIFI_TIP_REJOIN`,
`FILLER_TIP_CLOSER`+`SAY_WIFI_TIP_CLOSER`, `FILLER_TIP_TOGGLE`+`SAY_WIFI_TIP_TOGGLE`,
`SAY_WIFI_EXHAUSTED`. Nothing from the model. `verdict_scope_noted_all` is present in the
deployed tool list, so the stub regression is genuinely closed.

**Verdict:** PASS  (but see WT-13 — the caller who walks this path is never told what the
diagnostics found)

**Reproduced:** 5/5 — every hold-12 run that reached the tips.

---

### WT-05  Decline the early offer, then change your mind

**Why this should break it:** `WifiDeclined` is `ends=True` (app.py:1900-1903), and it is
gated on `_WIFI_ANSWERED`, which either offer satisfies. So declining DURING the wait
should end the call — before the sweep has reported, and therefore before there is
anything to hand a person.

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "just one device" / "no thanks" / "actually yes, let's try something"

**Expected:** per scripts.py:672 declining is deliberately not a dead end, and the caller is
handed to a person. But app.py:688-693 refuses an escalation while the sweep is out for a
stated reason — "a hand-off made now arrives empty and the receiving human restarts the
conversation from the top on someone who has already sat through it".

**Observed:**

```
[caller] 'no thanks'
      < No problem at all. Let me get you to someone who can take it from here.
[caller] "actually yes, let's try something"
  BidiSessionError: ... generic::failed_precondition
```

Authored copy, then the session is closed. The third turn cannot be delivered. No verdict
was ever spoken; the checks were still running when the call was handed over.

**Verdict:** FAIL

**Defect:** `WifiDeclined` bypasses the hold the escalate rail applies to exactly this
situation. `flows.escalate(condition=...)` will not hand off before
`_DIAGNOSED_OR_DONE_WAITING`; this rung has no such leg, so a "not now" spoken 30s into a
43s sweep ends the call and sends a human a caller whose diagnostics nobody waited eight
more seconds for. Second, there is no way back: the answer is terminal on the turn it is
given, so the very common "no thanks — oh, actually, go on then" is unreachable.
Severity: high — it discards a completed diagnostic sweep and the caller's time with it.

**Reproduced:** 3/3 spaced ("no thanks" ×2, "not now" ×1); session closed in all three.

---

### WT-06  "Already tried that" to each tip in turn

**Why this should break it:** `wifi_tried` is a cue slot keyed by tip
(app.py:1071-1084) and "already tried that" names no tip, so it cannot skip one. Meanwhile
`already_tried` (clarify.py:84) does match, and `AckAlreadyTried` is declared far above the
tips — two rungs wanting the same turn.

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "just one device" / "yes please" / "already tried that" ×3

**Expected:** the acknowledgement leads and the next tip follows in the same breath
(app.py:1619-1623 says the acknowledgement is meant to lead a turn, not consume it), then
the ladder advances one tip per turn to `SAY_WIFI_EXHAUSTED`.

**Observed:**

```
[caller] 'already tried that'
      < Thanks for bearing with me, and sorry for going over old ground.  Next one.
      < Try moving closer to your gateway, and check nothing large or metal is sitting
        right against it. Did that make a difference?
[caller] 'already tried that'
      < Try this one.  Turn the device's Wi-Fi off and back on again...
[caller] 'already tried that'
      < That's everything I can try from here. Let me get you to someone who can take a
        closer look at your home setup.
```

`SAY_ACK_ALREADY_TRIED` fired once, on the first, and the ladder advanced sensibly. It did
not repeat the apology on the second and third, which is the better behaviour.

**Verdict:** PASS

**Reproduced:** 1/1.

---

### WT-07  Fixed mid-way, on the early-offer path

**Why this should break it:** `WifiFixed` (app.py:1892-1896) requires
`{"slot": "wifi_offered", "filled": True}` — a bare latch, not the
`{"any": [wifi_offered, wifi_offered_early]}` pair that `_WIFI_ANSWERED` uses six lines
away in scripts.py:889. The early offer sets only `wifi_offered_early`. If that is a real
divergence, a caller who accepts during the wait can never be heard saying it is fixed.

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "just one device" / "yes please" / "oh, it's working now" / 20s silence

**Expected:** `wifi_fixed` matches the cue `\bit'?s working\b` (app.py:1069), `WIFI_RESOLVED`
goes true, `WifiFixed` speaks `SAY_WIFI_FIXED` — "That's good to hear. If anything else
comes up, we're here."

**Observed:** silence. The tip question was the last thing the caller ever heard.

```
      < On the device that's struggling, forget the home network in its Wi-Fi settings,
        then join it again. Did that help?
[caller] "oh, it's working now"
[caller] ... 20s of silence ...
```

**Control, `--hold 30`, identical words:** on the post-verdict path the same utterance is
answered correctly.

```
[caller] "oh, it's working now"
      < That's good to hear. If anything else comes up, we're here.
```

**Verdict:** PASS  *(was FAIL; re-verdicted 2026-08-14, see the note at the end of this entry)*

**Defect:** app.py:1893. `WifiFixed` reads `wifi_offered`, which only `HandleAllClear`
sets; on the early path the caller reporting success satisfies `WIFI_RESOLVED`, which then
switches off every tip rung (`{"not": WIFI_RESOLVED}`), `WIFI_SCOPE_ASKABLE` and
`WifiExhausted`'s route to it — so the fix silently disarms the whole walkthrough and puts
nothing in its place. The call wedges exactly as in WT-02. The control proves the latch is
the cause: same words, same tip, only the offer route differs. Severity: high, and it is
the single-word variety of the class this pass was hunting — a gate reading one latch where
the module next door reads both.

**Reproduced:** 3/3 spaced, two phrasings ("oh, it's working now" ×2, "that worked, it's
back" ×1); control PASS 1/1.


**Re-verdicted PASS, 2026-08-14** (was FAIL). `WifiFixed` reads
`scripts.WALKTHROUGH_OFFERED` — the `{"any": [wifi_offered, wifi_offered_early]}` pair
this row diagnosed — instead of the bare latch. Replayed with this row's own script and
setup (`--hold 12`, voice, cold), 3/3 spaced:

```
[caller] 'just one device'
< Got it, that helps. While those checks finish, we could try a couple of quick things on
  that device. Want to give it a go?
[caller] 'yes please'
< On the device that's struggling, forget the home network in its Wi-Fi settings, then
  join it again. Did that help?
[caller] "oh, it's working now"
< That's good to hear. If anything else comes up, we're here.
```

The two routes now answer the same words the same way. Three other conditions read a
single latch and were carried through the same audit; `ALL_CLEAR` /
`ALL_CLEAR_ALREADY_TRYING` deliberately still tells them apart, and now says so. A new
`ladder_check` row drives the early path so the pair cannot come apart again.

---

### WT-08  Exhaust all three tips

**Why this should break it:** `wifi_tips_exhausted` is counted in the hook
(hooks.py:278-285) rather than by the engine, and `WIFI_EXHAUSTED` additionally requires
`wifi_tip_given` unfilled, which the hook clears in `before_model` on a caller turn only.
Two clocks that have to agree.

**Setup:** cold, `--hold 12` and `--hold 30`, voice.

**Caller script:** three tips, "no change" / "still nothing" / "no change".

**Expected:** `SAY_WIFI_EXHAUSTED` verbatim after the third, and the session ends.

**Observed:** verbatim on every run that got there, on both the one-device and the
whole-house ladders.

```
      < That's everything I can try from here. Let me get you to someone who can take a
        closer look at your home setup.
```

**Verdict:** PASS

**Reproduced:** 6/6.

---

### WT-09  Answer a tip with a question

**Why this should break it:** the tip rungs are `say_rung`s with `requires=["wifi_scope"]`
and a per-turn latch. Nothing in `_wifi_tip` asks whether the caller actually ANSWERED the
previous tip, so any utterance on a tip turn should advance the ladder — including one that
is a question.

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "just one device" / "yes please" / "why would that help?" / "no change"

**Expected:** the question is answered — the model owns turns the ladder has nothing for,
and `xfinity_faq` is on the agent — and the outstanding tip is re-put or held open. Being
asked "why?" is not an answer to "did that help?".

**Observed:**

```
[caller] 'why would that help?'
      < Next one.
      < Try moving closer to your gateway, and check nothing large or metal is sitting
        right against it. Did that make a difference?
```

The question is not answered, not acknowledged, and the untried tip is spent. Three turns
later the caller is handed off having actually attempted two of the three things they were
told they had been given.

**Verdict:** FAIL

**Defect:** every tip is preemptive on any caller turn once `wifi_tip_given` clears, so the
rung beats the model to a turn the model should have had. `_wifi_tip` (scripts.py:955) has
legs for "already tried this one" and "asked about money" but none for "did not answer",
and there is no leg making a tip wait for a reply to the previous one. Severity: medium —
no wrong advice, but the caller is talked over, and the three-tip budget is spent on tips
they were never given a chance to try.

**Reproduced:** 3/3 spaced, two phrasings ("why would that help?" ×2, "hang on, what does
that actually do?" ×1).

---

### WT-10  Silence mid-tip, early-offer path

**Why this should break it:** the KNOWN issue — `no_input` is one flow-level policy applied
to whichever slot was asked last, and `repair`'s policy is `_account_no_input()`, whose
reprompts name the account number. A silent walkthrough turn should therefore be able to
ask a caller who is off restarting a laptop for their sixteen-digit account number.

**Setup:** cold, `--hold 12`, voice. Silence varied: 30s, and 30s ×4 (two minutes).

**Caller script:** "just one device" / "yes please" / 30s silence ×2–4 / "ok, no change"

**Expected:** the tip is re-put, or the caller is left in silence. The account-number
reprompts must not appear — the caller gave that number ninety seconds ago.

**Observed:** the account number was NOT re-asked. The `no_input` ladder never got the
turn; the async-await ladder owned it and spoke its own authored line, once:

```
      < On the device that's struggling, forget the home network in its Wi-Fi settings,
        then join it again. Did that help?
[caller] ... 30s of silence ...
      < Still running those checks — thanks for bearing with me.
[caller] ... 30s of silence ...   (nothing)
[caller] ... 30s of silence ...   (nothing)
[caller] ... 30s of silence ...   (nothing)
```

The caller who then speaks is picked up correctly and gets tip 2.

**Verdict:** PARTIAL

**Defect:** two smaller problems in place of the expected one. First, `SAY_SWEEP_WAITING[0]`
is authored copy but incoherent here: it answers "did that help?" with a progress report
about checks, 35s after the question, and the caller has no idea the two are unrelated.
Second, after that one line the call goes quiet indefinitely — two minutes of silence
produced no re-put tip, no hand-off and no close, so a caller who wanders off holds an open
line forever. Severity: medium. **The documented account-number hazard did not reproduce
on this path** — reported as measured, not as expected.

**Reproduced:** 2/2 spaced.

---

### WT-11  Silence mid-tip, post-verdict path

**Why this should break it:** same probe as WT-10 on the other route. Here
`diagnostics_complete` is filled, so the async-await ladder has nothing pending and cannot
own the silence. Something else must.

**Setup:** cold, `--hold 30`, voice.

**Caller script:** "just one device" / "yes please" / 30s silence ×2–3

**Expected:** authored copy or nothing. Every line this agent speaks is supposed to trace to
`scripts.py`.

**Observed:** three distinct lines that are in NO source file in this repo, and then a
loop.

```
      < On the device that's struggling, forget the home network in its Wi-Fi settings,
        then join it again. Did that help?
[caller] ... 30s of silence ...
      < Did forgetting and rejoining the network help get that device back online?
      < Are you still there? Let me know if forgetting and rejoining the network worked
        for your device.
[caller] ... 30s of silence ...
      < Goodbye!
      < Goodbye!
      < Goodbye!
      < Goodbye!
```

The wording of the first two drifts run to run — "…get that device back online" /
"…get your device back online" / "…worked for your device" — which is the signature of a
model rather than a script. `grep` for "still there", "back online" and "Goodbye" across
`scripts.py`, `app.py`, `clarify.py`, `hooks.py`, `guardrails.py`, `source_tools.py`, the
emitted `built_adv_walkthrough/`, and the grafted source app (`../agents`, `../tools`,
`../toolsets`, `../app.json`) returns nothing. "Goodbye!" is spoken four times per 30s
window and the session does not end; a third silent window produced four more.

**Verdict:** FAIL

**Defect:** on a walkthrough turn with the sweep settled, nothing authored owns a silence,
so the model does — and the copy the caller hears is improvised, twice, before the platform
falls into an unbounded "Goodbye!". This is the same class as the `verdict_scope_noted_all`
stub: an unowned turn is a turn the model takes. The flow's `no_input` policy did not
protect it (it never spoke), which is the real shape of the documented hazard — not a
wrong reprompt, but no reprompt and no owner. Severity: high — improvised customer-facing
copy plus a call that says goodbye eight times without hanging up.

**Reproduced:** 3/3 spaced. All three produced both improvised lines and the "Goodbye!"
loop.

---

### WT-12  "Will I be charged for this?" mid-walkthrough

**Why this should break it:** `_wifi_tip` carries `{"slot": "cost_answered", "filled":
False}` (scripts.py:976) because the scope question and the fee schedule were once
delivered in one breath. And there are three fee rungs; the wrong one quotes a
`{technician_fee}` schedule at a caller on an all-clear call where nothing is chargeable.

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "just one device" / "yes please" / "will I be charged for this?" /
"ok, no change" / "no change"

**Expected:** `AnswerNoCharge` → `SAY_NO_CHARGE`, because no technician is on the table; no
tip in the same breath; the walkthrough resumes on the next turn.

**Observed:**

```
[caller] 'will I be charged for this?'
      < No, nothing we're doing here costs anything. This call and any troubleshooting we
        try together are free. The only thing that can carry a charge is a technician
        visit, and I'd tell you before we booked one.
[caller] 'ok, no change'
      < Next one.  Try moving closer to your gateway, and check nothing large or metal is
        sitting right against it. Did that make a difference?
```

Right branch, verbatim, alone on its turn, and the ladder picked up where it left off.

**Verdict:** PASS

**Reproduced:** 1/1.

---

### WT-13  The verdict never arrives on the early-accept path

**Why this should break it:** noticed while running the others rather than hypothesised.
`ALL_CLEAR_ALREADY_TRYING` (scripts.py:772) exists precisely so a caller who accepted
during the wait still hears the finding, in the wording that does not re-offer. It is
declared above the walkthrough, so it should outrank every tip.

**Setup:** cold, `--hold 12`, voice. Five separate calls, one of them held silent for two
minutes mid-walkthrough to give the job every chance to land.

**Expected:** once the specialists report, `HandleAllClearAlreadyTrying` speaks
`SAY_ALL_CLEAR_ALREADY_TRYING` — "Everything on our side looks healthy… which is what we
are already looking at." — once, on the next available turn.

**Observed:** it never speaks. On every hold-12 call the caller goes from the scope
question straight through three tips to the hand-off without ever being told what the
checks found:

```
      < Got it, that helps. While those checks finish, we could try a couple of quick
        things on that device. Want to give it a go?
      ... three tips ...
      < That's everything I can try from here. Let me get you to someone who can take a
        closer look at your home setup.
```

At t=105s in the two-minute-silence run the agent was still saying "Still running those
checks — thanks for bearing with me", 87s after dispatch, against ~43s on an
otherwise-identical `--hold 30` call. The identical call at `--hold 30` speaks
`SAY_ALL_CLEAR` at 43.1s every time.

**Verdict:** FAIL

**Defect:** the walkthrough starves the sweep. Once the caller is answering a tip every
turn, `Specialists` is never settled, `diagnostics_complete` stays unfilled — which is also
what keeps `WALKTHROUGH_SAFE` true and the tips running — and no verdict rung is ever
eligible. The caller is handed to a person having been told nothing about their account,
their area, their line or their gateway, and the receiving human gets a hand-off with no
findings on it, which is the outcome app.py:688-693 refuses to allow the escalate rail to
produce. Severity: high, and it undercuts the case for hoisting the offer into the wait:
the wait exists to be filled, but filling it appears to prevent it from ever ending.
Corollary: on this path the "verdict lands in the middle of the walkthrough" awkwardness
never occurs, because the verdict never lands at all.

**Reproduced:** 5/5 hold-12 calls that reached the tips. Contrast 3/3 hold-30 calls, all of
which delivered the verdict at ~43s.

---

### WT-14  Whole-house branch, end to end

**Why this should break it:** `verdict_scope_noted_all` is the rung that was once
unregistered and silently replaced by a hash-returning stub, taking the answer gate and the
whole walkthrough down with it. Drive the branch it serves from the top.

**Setup:** cold, `--hold 12`, voice.

**Caller script:** "everything, nothing works" / "yes please" / "no change" ×3

**Expected:** `SAY_SCOPE_NOTED_ALL_DEVICES` ("around the house", not "on that device"), then
`SAY_WIFI_TIP_PLACEMENT`, `SAY_WIFI_TIP_NEARBY`, `SAY_WIFI_TIP_RESTART`,
`SAY_WIFI_EXHAUSTED`. `SAY_WIFI_TIP_CLOSER` must never appear.

**Observed:** all five lines verbatim, in order, one per turn, each behind its own filler.

```
      < Got it, that helps. While those checks finish, we could try a couple of quick
        things around the house. Want to give it a go?
      < Right then.  Since it's everything, let's look at the gateway itself. Is it out in
        the open and upright, rather than in a cabinet, behind the TV or down on the
        floor? Moving it somewhere clear can make a real difference. Did that change
        anything?
      < Here's another.  Try one device right next to the gateway and see if it behaves
        any differently there. That tells us whether it's the coverage around the house or
        the connection itself. How does it look up close?
      < One more thing.  Restart the device itself, then let it reconnect. How does it
        look after that?
      < That's everything I can try from here. Let me get you to someone who can take a
        closer look at your home setup.
```

"Moving closer" was never offered. `verdict_scope_noted_all` is present in the deployed
app's tool list, so the stub regression is closed.

**Verdict:** PASS

**Reproduced:** 1/1.

**Re-verdicted PASS, 2026-08-14** (unchanged, and now properly reproduced). Same setup —
cold, `--hold 12`, voice, account `8069100230359946` — on app `rerun-suspects`
(`a245593c-2342-4bb3-870d-00f5dee64921`), 3/3 spaced. All five authored lines, verbatim,
in order, one per turn, on every drive:

```
< Got it, that helps. While those checks finish, we could try a couple of quick things
  around the house. Want to give it a go?
< Right then.  Since it's everything, let's look at the gateway itself. Is it out in the
  open and upright, rather than in a cabinet, behind the TV or down on the floor? …
< Here's another.  Try one device right next to the gateway and see if it behaves any
  differently there. …
< One more thing.  Restart the device itself, then let it reconnect. How does it look
  after that?
< That's everything I can try from here. Let me get you to someone who can take a closer
  look at your home setup.
```

`SAY_WIFI_TIP_CLOSER` never appeared on any drive.

**Attribution: the PASS was real; only its reproduction count was weak.** This row was on
the re-measurement list because a 1/1 PASS is not a finding under this corpus's own rules,
and because the all-clear journey it runs on depends on the two legs answering "clear" —
which, before the gate fix, they were doing by accident (live backends happened to agree)
rather than from the fixture. Same words, better reason. 3/3 now, so it can carry weight
as a regression test.

**Reproduced:** 3/3 spaced.

---

## What this family could not test

* **Rung-level attribution after turn 1.** `demo_voice.py --engine` reads `sm["_log"]` off
  the response, and on this app that payload stops updating after the opening buffer —
  every later turn prints the same block. So "which rung fired" is inferred from the words
  and the source, not read from the engine, on turns 2 onward. Everything above is
  attributed from verbatim copy matches, which is unambiguous for authored lines and is
  what makes WT-11's three unattributable lines findings rather than guesses.
* **The `wifi_tried` skip cues.** WT-06 used the caller's natural phrasing, which names no
  tip. Whether "I already forgot the network and rejoined" actually skips
  `SAY_WIFI_TIP_REJOIN` is untested.
* **The other three accounts.** `8069100230361003`, `8069100020078787` and
  `8344200010126021` never reach the walkthrough by design (reboot, billing hand-off before
  any check, missing hardware), so the whole family runs on `8069100230359946`.
* **Text modality.** `tests/demo_drive.py` returns its turns in 1-2s, so the sweep is still
  out for the whole script and every walkthrough turn came back `(silent)`. The family is
  voice-only here; whether these defects reproduce in text is unknown.
* **A real 43-54s sweep with real specialists.** This is a `SPIKE_DEMO` build with a 20s
  recorded latency on the fixture path. The relative timing of verdict against walkthrough
  is therefore representative, not identical.
