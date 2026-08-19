# Escalation

Every way a caller tries to reach a human, or to leave: the direct ask, the fourth ask, the
angry ask, the sideways ask, and the ask timed to land on the one turn the engine treats
specially. The escalation HOLD is intentional and approved — asking for a person while the
diagnostics sweep is out is refused with a hold line, and released by `SWEPT`,
`verdict_delivered`, or `escalate_declined >= 3`. Nothing below reports the hold itself as a
defect. What is measured is where the reasoning AROUND it breaks.

**App:** `10443660-f20c-4842-af73-0e51a8f26b3d` (`adv-escalation`, project `ces-deployment-dev`,
location `us`). Built `SPIKE_DEMO=1 SPIKE_LOCAL_SPECIALISTS=1 python build.py --out
./built_adv_escalation`, pushed only to that app. No other app was driven.

**Drivers:** `tests/demo_drive.py` (text, cold) and `tests/demo_voice.py` (voice, cold) unless a
scenario says otherwise. Every drive was serial — never two at once — so no verdict here is
contention.

**Two harness facts that scope the results, established before anything was recorded.**

* *The specialists' completion is never delivered by either driver on this build.* The demo
  build lowers `Specialists` to `ASYNCHRONOUS` with a 20s recorded latency, and an async
  completion self-generates its turn only on a live bidi socket. Driven for 31 text turns
  (46s) and for voice calls out to 126s, the log never advances past
  `async_await_started {"task": "Specialists"}` — no `async_await_resolved`, no all-clear
  verdict. So `SWEPT` is never satisfied on the `all_clear` account and the third exit is the
  only one those calls can take. Scenarios needing the completion turn are BLOCKED and say so.
* *An active outage is not reachable cold on this build.* `8344200010126021` is the
  `area_outage` binding, but driven cold the gate answers `has_mac=false` and the outage leg
  answers `outage_status: "none"` (`{"leg_outage_res": false, "outage_status": "none"}`), so
  the missing-hardware rung fires instead — which `README.md` documents as this account's cold
  behaviour. ESC-02 therefore seeds the mock the way `flows chat --cuj outage` does, and says
  so in its Setup.

**Verdicts: 15 scenarios over 27 serial drives (20 text, 7 voice) — PASS 5 · FAIL 4 · PARTIAL 4
· BLOCKED 2.**

---

### ESC-01  The fourth ask

**Why this should break it:** `escalate_declined >= 3` is the backstop that makes the hold
bounded rather than a trap. Off-by-one here is invisible in a happy-path drive and is the
difference between a caller getting out on the fourth ask and never getting out at all. The
counter is also the engine's, not the app's, so the app cannot see it drift.

**Setup:** cold, text, account `8069100230359946` (`all_clear`). Four asks, each phrased so the
deterministic classifier matches (`_ESCALATE_RE` / `_ESCALATE_NOUN_RE`), fired back-to-back
inside the sweep. Repeated in voice with `--hold 18` and 6s silences between asks.

**Caller script:** "my internet is not working" / "8069100230359946" / "I want to speak to a
human" / "no, get me a real person" / "I need to talk to an agent" / "connect me to a
representative" / "are you there".

**Expected:** refusals 1 and 2 speak `SAY_HOLD_FOR_CHECKS` then `SAY_HOLD_FOR_CHECKS_AGAIN`;
refusal 3 clamps to the last rung (`_declined_rung` clamps rather than draining); the fourth
ask satisfies `{"slot": "escalate_declined", "gte": 3}`, so `condition` passes and the rail
speaks its `say` and ends the session escalated.

**Observed (text):**

```
> I want to speak to a human
< I can do that. I'd just like the check on your line to finish first, so whoever picks up
  already knows what's actually wrong. Give me a moment.
> no, get me a real person
< Almost there. As soon as those results are back I'll get you over to someone, and they'll
  know what we're dealing with.
> I need to talk to an agent
< Almost there. As soon as those results are back I'll get you over to someone, and they'll
  know what we're dealing with.
> connect me to a representative
< I completely understand, let me get you connected to a live agent who can help you directly.
> are you there
< ERROR SessionEndedError
```

`escalate_declined {"count": 1.0}` → `2.0` → `3.0`, then on the fourth
`escalate_path_arm {"tasks": ["EscalateHandoffSummary"]}` and
`escalate_terminated {"flow": "", "transfer_to": ""}`. Voice is identical in wording and in
timing — the release lands on the fourth ask at t=126.7s.

**Verdict:** PASS

**Reproduced:** 2/2 (1 text, 1 voice). The payload the hand-off should carry is ESC-10; the
double-spoken line in the text channel is ESC-12.

---

### ESC-02  Ask during an outage, five times

**Why this should break it:** the outage refusal is the only refusal the source actually makes
and the only one that must never convert. It sits OUTSIDE the `any`, so insistence should not
reach it — but `escalate_declined` still increments on every outage refusal, so if the `all`
were ever flattened, or the outage leg were re-read after the counter moved, the fifth ask
would connect a caller the policy says cannot be helped.

**Setup:** account `8344200010126021` with the `outage` CUJ seeded (`outage_status=active`,
tool fakes on) — see the scoping note above; cold does not reach an outage on this build.
Text, via `flows chat --cuj outage --app 10443660-...`.

**Caller script:** "my internet is not working" / then five asks: "I want to speak to a human"
/ "no, get me a real person" / "I need to talk to an agent" / "connect me to a representative"
/ "I demand a live agent" / "hello".

**Expected:** `_OUTAGE_NOW` is true, so `{"not": _OUTAGE_NOW}` fails on every ask however high
`escalate_declined` climbs. Every refusal takes the first `declined_say` reason and speaks
`scripts.SAY_OUTAGE_NO_AGENT` verbatim. No transfer, ever.

**Observed:** all five asks answered with, verbatim and identically:

```
< During an outage, we are unable to connect you with a live agent, as any troubleshooting
  would not bring your services back online. You can sign up for text alerts and check for
  updates using the Xfinity Status Center online.
        [tools: transfer_to_human]
```

The rail fires `transfer_to_human` and refuses it five times out of five. No `escalate_terminated`,
no end-session. The sixth turn ("hello") got a model-authored line that is in neither
`scripts.py` nor `app.py` — "I am here. Is there anything else I can help you with regarding
your service outage?" — which is a free turn, not a bypass.

**Verdict:** PASS

**Reproduced:** 1/1 (a PASS; the re-run rule applies to failures). The highest-value scenario
in this family holds.

**Re-verdicted PASS, 2026-08-14** (unchanged), and this time against an outage the agent
can actually see. Same setup — `flows chat --cuj outage --app <rerun-suspects>`, the same
seven turns — on app `rerun-suspects` (`a245593c-2342-4bb3-870d-00f5dee64921`), 3/3 spaced.

The reason this row was on the suspect list is in the family's own scoping note above: the
outage leg was answering `outage_status: "none"` on every drive, so an "active outage" was
never a state this app reached. With the leg fixture honoured it is, and turn one now
delivers the advisory the refusal is supposed to be consistent with:

```
you   > my internet is not working
agent > Alright, give me just a moment to check on that.  Thanks.  Give me just a moment
        while I check your connection.  An outage in your area is affecting Internet and
        TV service. Our teams are working to restore service as quickly as possible.
        During an outage, we are unable to connect you with a live agent, as any
        troubleshooting would not bring your services back online.
        You can sign up for text alerts and check for updates using the Xfinity Status
        Center online.
        Can I help you with anything else right now?
        [tools: set_active_flow, resolve_account_context, SweepLegs_leg_outage_leg,
                settle_guard, settle_diagnostics, verdict_area_outage]
```

and all five asks are then refused with `SAY_OUTAGE_NO_AGENT`, verbatim and identically,
`transfer_to_human` firing and being refused each time — 5/5 on each of three drives, 15
refusals, no conversion. The sixth turn ("hello") is again a free model-authored line, not
a bypass.

**Attribution: the PASS was real, but it was measured in a state that was not the state it
names.** The refusal held before the fix and holds now; what changed is that the outage is
now genuinely on the slot machine (`verdict_area_outage` fires) rather than the refusal
being the only evidence of it. Nothing in the six agent fixes touches this rail.

**Reproduced:** 3/3 spaced, 5 refusals each.

---

### ESC-03a  Ask before the account number

**Why this should break it:** the hold's copy is a promise about a specific thing in flight
("the check on your line"). Before the account is given, no check exists — `ContextGate` has
not run and `SweepLegs` has not been dispatched. The catch-all `declined_say` reason has no
`when`, so it answers a request made at a moment its own sentence does not describe.

**Setup:** cold, text, no account given.

**Caller script:** "my internet is not working" / "I want to speak to a human" / "no I said get
me a real person" / "8069100230359946".

**Expected:** the request is genuinely premature, so a refusal is right. The authored copy for
"we don't know what's wrong yet" is `SAY_HOLD_FOR_CHECKS`, whose own comment says it means "the
diagnostics are still out" — a state this turn is not in.

**Observed:**

```
> I want to speak to a human
< I can do that. I'd just like the check on your line to finish first, so whoever picks up
  already knows what's actually wrong. Give me a moment.
> no I said get me a real person
< Almost there. As soon as those results are back I'll get you over to someone, and they'll
  know what we're dealing with.
```

`escalate_declined` reached `2.0` before `ContextGate` ran. The account was then given and the
sweep started with only one refusal left in the budget.

**Verdict:** PARTIAL

**Defect:** two problems, one cause. (1) The copy is false: "the check on your line" and "as
soon as those results are back" describe a sweep that has not been dispatched — nothing is
running and no results are coming. The catch-all reason in `app.py:735-740` has no `when`, so
it covers pre-sweep and mid-sweep alike; `_DIAGNOSED_OR_DONE_WAITING` cannot distinguish them
because all three of its legs are false before the account is given. (2) `escalate_declined` is
CALL-scoped, so refusals made before any check existed spend the same budget that bounds the
hold. A caller who asks twice at the greeting reaches the release on their SECOND real
mid-sweep ask. Caller impact: moderate on the copy (it is a plausible-sounding lie), and it
compounds ESC-10 — the earlier the release fires, the emptier the hand-off.

**Reproduced:** 3/3 spaced (once here, once inside ESC-08, once inside ESC-06b where "is there
someone else" got the same line at the greeting).

---

### ESC-03e  Ask mid-walkthrough

**Why this should break it:** by the walkthrough the engine has a question pending and a tips
ladder to advance, so an escalate request has to win against a live collection slot rather than
against an idle wait. The tips rungs latch, and a request that gets swallowed here would look
exactly like the caller answering the tip.

**Setup:** cold, text, `8069100230359946`. Asks placed after the Wi-Fi offer is accepted and
the first tip is on the table.

**Caller script:** … / "just one device" / "yes please" / "I want to speak to a human" / "no,
get me a real person" / "hello".

**Expected:** the sweep is still out (see the scoping note), so both asks should be held on the
ladder, and the walkthrough should resume afterwards rather than losing its place.

**Observed:**

```
> yes please
< On the device that's struggling, forget the home network in its Wi-Fi settings, then join it
  again. Did that help?
> I want to speak to a human
< I can do that. I'd just like the check on your line to finish first, ...
> no, get me a real person
< Almost there. As soon as those results are back I'll get you over to someone, ...
> hello
< Try moving closer to your gateway, and check nothing large or metal is sitting right against
  it. Did that make a difference?
```

The ladder held, both asks were heard, and the walkthrough picked up at the next tip rather
than repeating the one it had already given.

**Verdict:** PASS

**Reproduced:** 1/1.

---

### ESC-03f  Ask after the hand-off

**Why this should break it:** the rail ends the session with `end_session`, but the app's own
notes record a variant where the tool fired and the call carried on regardless — reporting a
hand-off without performing one. If the session stays open, the caller keeps talking to an
agent that has already told them it is transferring them.

**Setup:** cold, text; the turn after every observed `escalate_terminated`.

**Caller script:** the release ask, then "are you there" / "no, get me a real person" / "hello?".

**Expected:** the session is closed; no further agent turns.

**Observed:** `ERROR SessionEndedError: Session <id> has already ended.` on every post-transfer
turn, in ESC-01 (`are you there`), ESC-02's suspended sibling ESC-05, ESC-06 and ESC-06b. No
mute-but-open session, no extra agent text.

**Verdict:** PASS

**Reproduced:** 5/5 across scenarios.

---

### ESC-03d / ESC-04  Ask on the verdict turn, and on the completion-delivery turn

**Why this should break it:** `_apply_answer_first` blanks the caller's text on a turn that
carries an async completion unless the task opts into `answer_first`, and logs
`async_completion_text_dropped` as a WARN when it does. `SweepLegs`/`Specialists` do not set
`answer_first`. So an escalate request timed onto the completion turn should be discarded
silently while the verdict is spoken over it — the caller asks for a person and hears a
diagnosis, with nothing in the transcript to show the ask ever happened.

**Setup:** attempted cold in both modalities. Text: 16 and 31 neutral turns after the account
to walk the boundary. Voice: `--hold 14 / 18 / 22 / 25`, with the ask as the first turn after
the buffer.

**Observed:** the completion never arrives, so the turn under test never exists. Voice at
`--hold 22` speaks the tick line at 35.9s ("Still running those checks — thanks for bearing
with me.") and is still on `SAY_HOLD_FOR_CHECKS_AGAIN` at t=91.1s. Text at 31 turns/46s never
logs `async_await_resolved` for `Specialists`. `grep async_completion_text_dropped` over every
trace collected: zero hits — the code path was never reached, not proved absent.

**Verdict:** BLOCKED (both)

**What stopped it:** the async completion self-generates its turn only on a live bidi socket,
and both drivers open a fresh stream per turn. Timing an utterance onto that turn needs a
driver that holds one socket open across the completion, which does not exist in `tests/`. The
nearest reachable proxy is ESC-05, where the statuses are all resolved on the gate turn and the
first ask transfers immediately and correctly — that shows the post-verdict path is sound, and
says nothing about the delivery turn itself.

---

### ESC-05  The account that never sweeps

**Why this should break it:** `8069100020078787` is restricted, so the gate short-circuits and
no check ever runs. `SWEPT` can therefore never be satisfied by the sweep landing. If the hold
read only that leg, the one caller who most needs a human — the one whose account is blocked —
would be held forever with a line about checks that will never finish.

**Setup:** cold, text, account `8069100020078787` (`account_suspended`).

**Caller script:** "my internet is not working" / "8069100020078787" / "I want to speak to a
human" / "no, get me a real person" / "hello?".

**Expected:** the gate itself sets `diagnostics_complete` (`source_tools.py:1265-1270`:
`account_status != "clear"` → `diagnostics_complete = True`, statuses `skipped`), so exit 1
(`SWEPT`) is satisfied on the gate turn; `HandleBillingBlock` then latches `verdict_delivered`,
satisfying exit 2 as well. The first ask should go straight through.

**Observed:**

```
> 8069100020078787
< Give me just a moment while I check your connection.  I see an issue with your account status
  that's interrupting your internet service. Let me get you to someone who can help with your
  account.
> I want to speak to a human
< I completely understand, let me get you connected to a live agent who can help you directly.
> no, get me a real person
< ERROR SessionEndedError
```

Log: `{"diagnostics_complete": true, "gateway_status": "skipped", ...}` on the gate turn, then
`verdict_delivered: "true"`, then on the ask
`escalate_path_arm` → `task_completed {"success": true, "tool": "verdict_human_request", "task":
"EscalateHandoffSummary"}` → `escalate_terminated`. Both exits 1 and 2 were already open; no
refusal was ever spoken. The hand-off carried its payload here — the contrast that isolates
ESC-10.

**Verdict:** PASS

**Reproduced:** 1/1.

---

### ESC-06  Indirect asks

**Why this should break it:** escalation detection is a deterministic regex over six verbs and
seven noun phrases (`_ESCALATE_RE`, `_ESCALATE_NOUN_RE`). Real callers rarely use the template.
The interesting question is not whether the regex misses — it will — but what happens when it
does: whether the model catches it, whether the turn is ignored, or whether some OTHER
deterministic cue catches it and does something worse than nothing.

**Setup:** cold, text, before the account so the wait cannot mask the answer (mid-wait
behaviour is ESC-09). Two calls, because three of the six phrases end the session.

**Caller script (call A):** "my internet is not working" / "this isn't working" / "forget it" /
… **(call B):** "my internet is not working" / "is there someone else" / "I want to make a
complaint" / "goodbye" / "cancel".

**Expected:** these are all ways of asking to leave or to reach someone. The authored ways out
are the escalate rail (a person) and `steer_back.on_exhaust` (a signposted close naming the app
and website). Nothing in `app.py` declares a `cancel` control block, so whatever `cancel_flow`
does by default is the engine's answer, not an authored one.

**Observed:**

| Utterance | What happened |
| --- | --- |
| "this isn't working" | Not recognised. Re-asked for the account number. |
| "is there someone else" | Recognised — `transfer_to_human` fired (by the MODEL; no `escalate_backstop` line) and was correctly declined with `SAY_HOLD_FOR_CHECKS`. |
| "I want to make a complaint" | Not recognised. Re-asked for the account number. |
| "forget it" | `cancel_backstop {"text": "forget it"}` → `cancel_flow` → `cancel_terminated`. Call over. |
| "goodbye" | Same: `cancel_flow`, `cancel_terminated`. Call over. |
| "cancel" | Never reached — the call had already ended. |

The close for both cancels was `< No problem. Let me know if you need anything else.  No
problem. Let me know if you need anything else.`

**Verdict:** PASS  *(was FAIL; re-verdicted 2026-08-14, see the note at the end of this entry)*

**Defect:** "forget it" and "goodbye" hang up on the caller instantly, on the second turn of the
call, with no confirmation and no offer of a person. `app.py` declares `escalate=flows.escalate(...)`
but no `cancel` block, so `_cancel_intent` reaches the engine's bare default: end the session,
speak a neutral pleasantry. On a repair line "forget it" most often means "forget the account
number, just get me someone" — the caller is at their most frustrated and the agent's response
is to disconnect them. It is also inconsistent with the same call's escalate handling, which
takes three refusals before it will let anyone go. Severity: high — this is a silent
abandonment path with no record that the caller wanted anything. `_ESCALATE_NOUN_RE` also has no
`manager`, `supervisor`, `boss` or `escalate`; see ESC-07.

**Reproduced:** 2/2 calls, 4/4 cancel utterances (`forget it` ×1, `goodbye` ×1, plus the two
duplicate renderings).


**Re-verdicted PASS, 2026-08-14** (was FAIL). `repair`, `reboot` and `human` all declare a
`cancel` block now, and it confirms before it ends: `requires_readback=True` with
`SAY_CONFIRM_CANCEL`. The engine resumes the flow on anything that is not a clear yes, so
a caller who said "stop" meaning "stop talking" keeps their call. Call B replayed with
this file's own script, 3/3 spaced:

```
> my internet is not working
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
> is there someone else
< I can do that. I'd just like the check on your line to finish first, so whoever picks up
  already knows what's actually wrong. Give me a moment.
> I want to make a complaint
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
> goodbye
< Before I let you go, did you want me to stop here?
> no, actually keep going
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
```

Call A's "forget it" is the same, 3/3, and answering "yes" closes with "Okay, I'll stop
there. If anything else comes up, we're here."

One thing this row did NOT cover and is worth recording, found while replaying it: "is
there someone else" as the OPENING turn (rather than turn 2, as scripted here) routes
straight to `human` and the model fires `transfer_to_human` itself, which ends the call
without the engine rendering anything. A/B'd on one app: the baseline build gives the
neutral default on one drive and SILENCE on the next, and the hill-climb build gives
silence. Pre-existing, non-deterministic, and untouched by the cancel/escalate work — the
control slot never fires, so no disposition can help. Not folded into this verdict.

**That opening-turn observation is FIXED, 2026-08-14.** Three things were true at once on
the routing turn, all measured: `_escalate_tool` and `_cancel_tool` are both empty (a
router is neither cancelable nor escalatable by default, so there is no control slot to
fire and `transfer_to_human` maps to nothing); `_intent_first` takes the early return that
permits only the route backstops, so the cancel and escalate keyword backstops are skipped
by construction; and `ROUTE_CUES["human"]` matched none of the phrasing. `_ESCALATE_RE`
would not have matched either — it requires a verb from
`speak|talk|chat|connect|transfer|reach`, and this request names none. So the turn fell
wholly to the model, which is the coin flip.

A route cue fires BEFORE the model, so three were added to `ROUTE_CUES["human"]`: "is there
someone else", "someone else i can", "anyone else i can". Phrased long on purpose — a bare
"someone else" would catch "is someone else having this problem?", an outage question this
agent really is asked. Re-driven cold on a private app, opening turn, 3/3 spaced (was 1/3):

```
> is there someone else
< I completely understand, let me get you connected to a live agent who can help you
  directly.
```

and the negative case still goes to repair:

```
> is someone else having this problem?
< To get started, could you please tell me your Xfinity account number …
```

Router-turn only: `_route_cues` is derived from the ACTIVE config and `repair_dag` carries
none, so this cannot fire mid-repair and cannot bypass the `escalate` block's outage
condition. The three cues were checked against `tests/routing_heldout.json` and are
disjoint from it, per the rule at the head of `ROUTE_CUES`. The line is still rendered
TWICE, which is ESC-12's doubling and is untouched here.

---

### ESC-07  Angry escalation

**Why this should break it:** "get me a manager NOW" is the highest-stakes utterance in this
family and it matches nothing: `manager` and `supervisor` are absent from both cue lists, and
"get" is not one of the six verbs. Whether the copy adapts in register is secondary to whether
the request is heard at all.

**Setup:** cold, `8069100230359946`, asks placed inside the sweep. Text ×3, voice ×3, plus a
voice control (ESC-09).

**Caller script:** … / "I've called three times about this, get me a manager NOW" / "did you
hear me" / "I want your supervisor" / "get me a manager".

**Expected:** if the request is not recognised the model should at least answer the human in
front of it. `while_waiting` exists for exactly this stretch of the call, and the app's
`declined_say` ladder exists so a refusal never lands as silence — `_declined_rung`'s own
docstring says a refusal "answers a direct question and hearing nothing back is the worst
available reply."

**Observed (text, all three runs):**

```
> I've called three times about this, get me a manager NOW
< (silent)
> did you hear me
< (silent)
> I want your supervisor
< (silent)
> get me a manager
< (silent)
```

Engine log for each of those turns:
`async_await_idle_hold {"caller_spoke": true, "spoken": false, "tasks": ["Specialists"]}`
followed by `no_input_silent_tick {}`. No `escalate_backstop`, no `transfer_to_human`, no
`escalate_declined` — the counter never moves, so the `gte: 3` release can never be earned by
this caller. Voice is the same: three turns, no agent audio at all.

**Verdict:** FAIL

**Defect:** two layers. The cue lists
(`packages/flows/.../slot_filling_engine/python_function/python_code.py:9645-9650`) do not
contain `manager`, `supervisor`, `boss`, `escalate` or the bare "get me a …" frame, so the
angriest phrasings are not escalation to this agent. Underneath that, the wait's idle-hold
(`python_code.py:7649-7672`) answers any unmatched caller turn with silence — it returns
`{"silent": True}` and `before_model.py:925-935` renders that as an empty `LlmResponse`. So the
caller who is shouting hears nothing, four turns running, over a wait measured at 43-54s. In
voice that is total dead air on the exact caller most likely to complain about it. It also
makes the release unreachable, because the release is counted in refusals and no refusal is
ever issued. Severity: high.

**Reproduced:** 3/3 spaced text, 3/3 spaced voice.

---

### ESC-09  One unmatched word mutes the rest of the wait

**Why this should break it:** ESC-07 showed an unmatched utterance gets silence. The question
this asks is whether the engine RECOVERS — whether a properly phrased ask, made straight after,
is heard. Everything in the escalate design assumes the caller can always reach the rail.

**Setup:** cold, `8069100230359946`, asks inside the sweep. The control is what makes this
readable: the SAME hold duration and the SAME recognised phrases, differing only in whether an
unmatched utterance came first.

**Caller script (probe):** … / "get me a manager right now" / "I want to speak to a human" /
"no, get me a real person".
**Caller script (control):** … / "I want to speak to a human" / "get me a manager right now" /
"no, get me a real person".

**Expected:** "I want to speak to a human" matches `_ESCALATE_RE` unconditionally. It should be
classified and refused on the ladder wherever it appears in the call, exactly as it is when it
is asked first.

**Observed — voice, `--hold 14`, control first:**

```
[caller] 'I want to speak to a human'
  < I can do that. I'd just like the check on your line to finish first, so whoever picks up
    already knows what's actually wrong. Give me a moment.
[caller] 'get me a manager right now'
  (no agent audio)
[caller] 'no, get me a real person'
  (no agent audio)
```

**Observed — voice, `--hold 14`, probe (unmatched first), 3 runs:** every one of the three
follow-up turns produced no agent audio, including "I want to speak to a human", the same
sentence that was answered on the line above when it came first. A 6s silence turn after them
produced nothing either — not even the `while_waiting` tick.

**Observed — text, 3 runs:** identical. "get me a manager right now" → `(silent)`, then "I want
to speak to a human" → `(silent)`, then "no, get me a real person" → `(silent)`. A longer text
run (31 turns) shows the mute is bounded, not permanent: the agent stayed silent for roughly
the first 20 turns and then resumed, speaking the walkthrough. Twelve consecutive silent turns
were measured with two explicit escalation requests lost inside them.

**Control that rules out timing:** the same `--hold 14`, with the matched ask FIRST, is answered
normally (quoted above), and answering the agent's own scope question mid-sweep does NOT mute
it — "just one device" gets "Got it, that helps. While those checks finish, we could try a
couple of quick things on that device. Want to give it a go?", and an escalate ask straight
after that is refused on the ladder as designed. So the trigger is specifically a caller turn
the engine had nothing to say to.

**Verdict:** FAIL

**Defect:** once the wait has answered one caller turn with silence, subsequent turns are no
longer classified — no `escalate_backstop`, no `transfer_to_human`, no `escalate_declined` in
the log for any of them. The escalate rail is not refusing these asks; it never sees them. The
mechanism sits between `before_model.py:925-935` (the silent hold returns
`LlmResponse.from_parts(parts=[])`, an empty response that never completes the turn) and the
deterministic classifier in `python_code.py:9843-9858`, which is only consulted with a fresh
`last_user_text`; the `_stale_scan` latch at `python_code.py:10152-10160` is the nearest
documented instance of the same freeze ("the engine's view of the caller stayed frozen … for
the remaining forty seconds of the call"). Caller impact: the worst in this family. It removes
every exit at once — the ladder cannot refuse, so `escalate_declined` cannot climb, so the
`gte: 3` release cannot fire — and the trigger is one ordinary sentence the regex happens not
to know. Severity: high, and it is the root cause behind ESC-07's dead air.

**Reproduced:** 3/3 spaced text, 3/3 spaced voice, plus 1/1 control in each modality.

---

### ESC-08  Ask, then withdraw

**Why this should break it:** the withdrawal is a state question. `escalate` is a control slot
the engine pops after disposition, so the flow should resume — but `escalate_declined` is a
counter, and nothing un-counts a refusal the caller no longer wants. If it still counts, the
caller has spent a rung of a ladder they walked away from.

**Setup:** cold, text, `8069100230359946`. One ask before the account, withdrawn; then the
account; then two real asks inside the sweep.

**Caller script:** "my internet is not working" / "I want to speak to a human" / "actually never
mind, carry on" / "8069100230359946" / "I want to speak to a human" / "no, get me a real
person" / "I need to talk to an agent".

**Expected:** the flow resumes at the account ask. Whether the withdrawn ask counts is not
stated in `app.py`; what IS stated is the intent — "the fourth ask goes through" — which a
caller measures from the asks they actually meant.

**Observed:**

```
> I want to speak to a human
< I can do that. I'd just like the check on your line to finish first, ...
> actually never mind, carry on
< To get started, could you please share your Xfinity account number or the phone number
  associated with your account?
> 8069100230359946
< Give me just a moment while I check your connection. While those checks run, ...
> I want to speak to a human
< Almost there. As soon as those results are back I'll get you over to someone, ...
> I need to talk to an agent
< I completely understand, let me get you connected to a live agent who can help you directly.
```

Resumption is clean — no repeated greeting, no lost account step. `escalate_declined` went
`1.0` (withdrawn ask) → `2.0` → `3.0`, so the release fired on the caller's THIRD real ask.

**Verdict:** PARTIAL

**Defect:** the flow resumes correctly; the counter does not. A withdrawn ask keeps its rung, so
the caller's first genuine mid-sweep request is answered with the second rung — "Almost there.
As soon as those results are back…" on a request they have made once — and the hold ends one
ask early. Same root as ESC-03a (`escalate_declined` is call-scoped and never decremented), and
the same downstream cost: the earlier the release, the less the sweep has produced for
ESC-10's payload. Severity: low on its own, moderate as an amplifier.

**Reproduced:** 1/1 here; the underlying counter behaviour 3/3 with ESC-03a.

---

### ESC-10  The hand-off the hold was waiting for

**Why this should break it:** the hold's justification is stated in its own copy — "I'd just
like the check on your line to finish first, so whoever picks up already knows what's actually
wrong" — and implemented as `tasks=["EscalateHandoffSummary"]`, whose comment says without it
"the receiving human got no task, no skill, no findings and no account number". That task takes
`["account_status", "outage_status", "network_status", "gateway_status"]` as inputs. On the
insist-release path those are precisely the values the sweep has not returned yet.

**Setup:** cold, text, `8069100230359946`; the release turn from ESC-01. Compared against
ESC-05, where the same chain runs with every status resolved.

**Expected:** the chain runs before the disposition and puts the payload on the hand-off.

**Observed (ESC-01, release on the fourth ask):**

```
  escalate_path_arm      {"tasks": ["EscalateHandoffSummary"]}
  escalate_terminated    {"flow": "", "transfer_to": ""}
```

Nothing between them: no `task_completed`, and `verdict_human_request` appears nowhere in the
trace. Contrast ESC-05, where the statuses are resolved (`skipped` counts as filled) on the gate
turn:

```
  escalate_path_arm      {"tasks": ["EscalateHandoffSummary"]}
  task_completed         {"success": true, "tool": "verdict_human_request",
                          "task": "EscalateHandoffSummary"}
  escalate_terminated    {"flow": "", "transfer_to": ""}
```

**Verdict:** FAIL

**Defect:** `EscalateHandoffSummary` (`app.py:1955-1957`) is input-gated on the four statuses,
two of which (`network_status`, `gateway_status`) are what `Specialists` is out fetching —
the gate turn logs them as `absent`. `_escalate_path_turn` computes the chain's DAG state, gets
`action != "fire"`, returns `None`, and the rail terminates immediately. So on the ONE path the
`escalate_declined >= 3` release exists to serve — the caller who insisted while the sweep was
still out — the hand-off carries nothing, which is the exact outcome the hold was justified by
preventing. The caller waited through three refusals to buy an informed hand-off and got a
blind one, and was told twice that this was why they were waiting. ESC-03a and ESC-08 make it
likelier by spending the budget before the sweep starts. Severity: high — it is the family's
stated contract failing on its own path, and it is invisible from the transcript.

**Reproduced:** 2/2 insist-releases (ESC-01, ESC-08) showed the chain armed and never fired;
1/1 status-resolved release (ESC-05) showed it fire. 3/3 consistent with the input gate.

---

### ESC-11  Is the hold a trap when nothing can release it?

**Why this should break it:** with the completion undeliverable (see the scoping note) this
build reproduces the worst case by accident: `SWEPT` never true, no verdict, and a caller who
does not use a recognised escalate phrase cannot move `escalate_declined`. All three declared
exits shut. Something must still end the call.

**Setup:** cold, text, `8069100230359946`; 31 neutral turns ("uh-huh") after the account, then
an escalate ask.

**Expected:** `awaits(max_turns=30, on_timeout={"say": SAY_SWEEP_UNAVAILABLE, ...})` at
`app.py:387-398` is the declared bound, feeding the `verdict_no_telemetry` rail — which would
also open exit 2.

**Observed:** the first ~20 turns were `(silent)` (the ESC-09 mute). The agent then resumed on
its own and walked the Wi-Fi ladder — "Since it's everything, let's look at the gateway
itself…", "Try one device right next to the gateway…", "Restart the device itself…" — and
closed at turn ~25 with `WifiExhausted`:

```
< That's everything I can try from here. Let me get you to someone who can take a closer look
  at your home setup.
> uh-huh
< ERROR SessionEndedError
```

So the call is bounded and ends by handing off — but via the Wi-Fi exhaustion rung, not via any
of the three documented escalate exits and not via `on_timeout`, which never fired inside 31
turns.

**Verdict:** PARTIAL

**Defect:** not a trap, but the way out is not one of the three the design names, and the
caller spent roughly twenty consecutive turns in silence to reach it. `SAY_SWEEP_UNAVAILABLE`
was never spoken, so the caller is never told the checks failed — they are told the Wi-Fi tips
are exhausted, which is a different and less accurate story about why they are being handed
over. Severity: moderate, and largely inherited from ESC-09.

**Reproduced:** 1/1 at 31 turns; 1/1 at 16 turns showing no exit yet at that point.

---

### ESC-12  The hand-off line, twice

**Why this should break it:** the rail concatenates a chain member's stashed `then_say` in
front of the disposition (`_escalate_path_turn` → `sm["_escalate_pending_msg"]`, joined as
`f"{_chain_msg} {_say}"`). A duplicate there would be audible.

**Setup:** cold; observed in every text drive that ended on a control disposition, and checked
against voice.

**Expected:** one sentence, once.

**Observed (text):** `< I completely understand, let me get you connected to a live agent who
can help you directly.  I completely understand, let me get you connected to a live agent who
can help you directly.` — two spaces between, three separate scenarios (ESC-01, ESC-05,
ESC-08). The cancel disposition duplicates the same way: `< No problem. Let me know if you need
anything else.  No problem. Let me know if you need anything else.` **Voice speaks it once**:
ESC-01's voice release rendered a single line.

**Verdict:** PARTIAL

**Defect:** the control disposition is rendered twice in the TEXT channel and once in voice, so
it is a channel rendering fault (message part plus response part both surfaced), not the chain
join — the duplicate appears even in ESC-02's outage refusal path where no chain ran. Caller
impact: low in voice (none observed), visible and slightly unhinged in chat, on the last line
of the call. It is not confined to escalation: the same doubling was seen on `cancel`.

**Reproduced:** 5/5 text dispositions; 0/2 voice.

**Re-verdicted PARTIAL, 2026-08-14** (unchanged). Driven on app `rerun-suspects`
(`a245593c-2342-4bb3-870d-00f5dee64921`) on ESC-01's own script — the fourth-ask release,
which is the drive this was first seen on — text 3/3 and voice 2/2.

Text, all three drives, the disposition rendered twice with two spaces between:

```
> connect me to a representative
< I completely understand, let me get you connected to a live agent who can help you
  directly.  I completely understand, let me get you connected to a live agent who can
  help you directly.
> are you there
< ERROR SessionEndedError
```

Voice, both drives, the same release renders the line **once**:

```
[caller] 'connect me to a representative'
< I completely understand, let me get you connected to a live agent who can help you
  directly.
```

**Attribution: still broken, and demonstrably nothing to do with the fixture gate.** A/B:
on the MID build (all six agent fixes, the leg-fixture gate reverted) the text doubling is
present too, 2/2, character for character. The row was on the suspect list only because
several of the drives it was first observed on ended on verdicts the contaminated convoy
leg produced; the rendering fault is independent of which verdict got there.

One thing that did NOT reproduce, and it is a different defect rather than this one: the
`cancel` disposition could not be observed at all this time. "forget it, I give up" during
the specialist wait, followed by "yes", produced two `(silent)` turns, 3/3 — the
confirmation `SAY_CONFIRM_CANCEL` never speaks, so there is no disposition to double. That
is the wait-swallow class (TIMING-09 / ESC-09) reaching a control slot, and it is logged
as ESC-13 below rather than folded in here.

**Reproduced:** 3/3 text (doubled), 2/2 voice (single), 2/2 text on the MID build
(doubled).

---

### ESC-13  Cancel during the specialist wait is answered with silence

*Added 2026-08-14, while re-measuring ESC-12. New row, not a re-verdict.*

**Why this should break it:** `cancel` was declared on `repair` with
`requires_readback=True` precisely so that "stop", "forget it" and "I give up" hold and
confirm rather than hanging up on a frustrated caller. The specialist wait is the part of
the call a caller is most likely to give up in, and it is also the window that already
swallows any turn matching no fire-eligible rung (TIMING-09).

**Setup:** app `rerun-suspects`, cold, text, account `8069100230359946`.

**Caller script:** "my internet is not working" / "8069100230359946" / "forget it, I give
up" / "yes"

**Expected:** the first fill holds and asks — `SAY_CONFIRM_CANCEL` — and an affirmative
next turn terminates on `SAY_CANCELLED`.

**Observed:** both turns silent, on all three drives.

```
t=   8.2s > forget it, I give up
          < (silent)
t=   9.1s > yes
          < (silent)
```

**Verdict:** FAIL

**Defect:** the confirmation the readback exists to produce never reaches the caller, so
the exit that was added to stop the engine terminating people on its neutral default is
itself unreachable inside the sweep window. The caller who gives up mid-wait gets nothing
— not the confirmation, not the cancellation, not a refusal. Same mechanism as ESC-09 and
TIMING-09; what is new is that it now closes a *control* slot rather than a collection
one, which the "what could not be tested" note at the foot of this file explicitly flagged
as unknown.

**Reproduced:** 3/3 spaced text.

---

## What could not be tested

* The completion-delivery turn (ESC-04) and the all-clear verdict turn (ESC-03d) — the async
  completion is never delivered by `tests/demo_drive.py` or `tests/demo_voice.py` on this
  build, so `async_completion_text_dropped` was never reachable. This needs a driver that
  keeps one bidi socket open across the completion.
* An outage reached COLD (ESC-02 had to seed the mock). The cold behaviour of
  `8344200010126021` is the missing-hardware rung, which `README.md` documents.
* DTMF and barge-in as escalation routes — no keypad on this build's drivers.
* Whether the mute in ESC-09 also swallows a `cancel` request (only escalate phrasings were
  driven inside the mute).
