# HOSTILE — angry, abusive, sarcastic and uncooperative callers

The caller in this family is furious, will not answer the question asked, contradicts
themselves, swears, threatens, demands money and demands a person. Every scenario here
is a call a real repair line gets several times a day.

**App:** `8650e943-6169-4b89-8f76-3aca93ccdc5e` (`adv-hostile`, `ces-deployment-dev` / `us`),
built `SPIKE_DEMO=1 SPIKE_LOCAL_SPECIALISTS=1 python build.py --out ./built_adv_hostile`
off `072c533`. Every drive below is against that app and no other, one at a time.

**Modalities.** `tests/demo_drive.py` is text, `tests/demo_voice.py` is real audio. They
differ in one way that matters to this family: **the remote diagnostics job never lands
on the text driver** (`Specialists success:false` on every text drive here), so a text
call stays inside the wait forever, while a voice call reaches the verdict at 41-43s.
Where that changes the result it is said so per scenario.

**One systemic behaviour explains a third of these findings**, so it is stated once here.
While the remote job is outstanding, a caller turn that no rung claims returns
`async_await_idle_hold {"spoken": false, "caller_spoke": true}` -> `no_input_silent_tick`,
and the caller gets **nothing at all**. That is deliberate in the framework
(`python_code.py` ~7660: silence beats letting the model improvise mid-wait) and it is the
right default for a caller who is thinking. It is the wrong default for a caller who is
shouting, because a hostile caller's turns are almost never claimable, and dead air is
what they escalate on.

---

### HOSTILE-01  Anger on the opening breath

**Why this should break it:** the source gates its frustration acknowledgement behind
`accountNumber` being filled (`app.py:1103`, `scripts.py:376`), so the angriest moment of
the call — the first sentence — is the one moment it cannot be acknowledged. A caller who
opens hot should hear the ask and nothing else, and the question is whether that reads as
cold.

**Setup:** app `8650e943`, cold, text.

**Caller script:**
1. "This is the fourth time I've called. Your service is garbage."
2. "8069100230359946" 3. "just one device" 4. "yes" 5. "no that did nothing"

**Expected:** `scripts.ASK_ACCOUNT_NUMBER` verbatim, with no acknowledgement — the
`frustration` slot's `condition` makes that correct, not accidental. Then the ordinary
journey.

**Observed:**
```
> This is the fourth time I've called. Your service is garbage.
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
```
Then the scope question, `SAY_SCOPE_NOTED`, and the tips ladder, all verbatim.

**Verdict:** PASS — right thing, and for the documented reason. The tradeoff is real
(nothing is said to a caller who has just told you it is their fourth call) but it is the
authored one, and the trade it buys — not asking for the account number twice — is worth
more.

**Reproduced:** 1/1.

---

### HOSTILE-02  Anger after the account number, in cue vocabulary

**Why this should break it:** `AckFrustration` (`app.py:1648`) latches `frustration_ack`
and leaves the ladder open, so it is supposed to co-fire with the substantive next step.
Mid-sweep there is no next step, so the rung has to survive on its own.

**Setup:** app `8650e943`, cold, text.

**Caller script:** "This is the fourth time I've called. Your service is garbage." /
"8069100230359946" / "This is ridiculous, I'm sick of this." / "just one device" / "no"

**Expected:** `scripts.SAY_ACK_FRUSTRATION` verbatim on turn 3 (`ridiculous` and `sick of`
are both `clarify.FRUSTRATION_CUES`).

**Observed:**
```
> This is ridiculous, I'm sick of this.
< I'm sorry, I can hear how frustrating this has been, and I do want to get it sorted
  for you.
```
`verdict_ack_frustration` in the engine log. Once only, as designed.

**Verdict:** PASS.

**Reproduced:** 1/1.

---

### HOSTILE-03  Anger in vocabulary the cue list does not carry

**Why this should break it:** `FRUSTRATION_CUES` is nineteen regexes and none of them is
the commonest hostile register — *garbage, disgrace, rubbish, appalling, a joke of a
company*. An angry caller whose words miss the list should fall through to the model, and
mid-sweep the model is silenced.

**Setup:** app `8650e943`, cold, text.

**Caller script:** "my internet is down again" / "8069100230359946" / "Your service is
garbage and your company is a disgrace. Fourth call this week." / "just one device" / "no"

**Expected:** no acknowledgement (defensible — the cues did not match), but the call
carries on: turn 4 fills `wifi_scope` and fires `AckScopeEarly`, turn 5 fires
`WifiDeclined`.

**Observed:** three consecutive dead turns. The abuse gets nothing, **and the two turns
after it get nothing either** — the scope answer and the decline are both swallowed.
```
> Your service is garbage and your company is a disgrace. Fourth call this week.
< (silent)
> just one device
< (silent)
> no
< (silent)
```
Engine log for all three: `async_await_idle_hold {"spoken": false, "caller_spoke": true}`
then `no_input_silent_tick`.

**Verdict:** FAIL.

**Defect:** the idle hold described at the top of this file. An unclaimable turn during
the wait costs the caller that turn *and* every turn after it until the job lands — on
text, that is the rest of the call. `python_code.py` ~7660 chooses silence over model
improvisation, which is right; what is missing is that the turns AFTER it are also lost,
because `AckScopeEarly` never became eligible again. Severity from the caller's seat:
total. They swore at a machine, it went quiet, and then it ignored the two questions it
had itself asked.
**Not hostility-specific.** Control run, identical script with "I bought a new coffee
machine yesterday." in place of the abuse: same three silent turns, 1/1. The abuse is the
commonest way to get here, not the cause.

**Reproduced:** 3/3 spaced (plus 1/1 on the neutral control).

---

### HOSTILE-04  Profanity, mild then strong

**Why this should break it:** `guardrails.py` blocks profanity **agent-side only** and
says so ("A caller whose internet is broken may well swear, and blocking THEM would be a
worse product"). `PROMPT_GUARD_POLICY` also names profanity as explicitly out of scope.
Both claims are testable.

**Setup:** app `8650e943`, cold, text.

**Caller script:** "this crappy service is down again, sort it out" / "8069100230359946" /
"one device" / "yes fine" / "that did fuck all, this is fucking useless" / "still nothing
you idiot"

**Expected:** no guardrail refusal, no register change, no agent profanity; the two
profane turns read as "the tip did not work" and advance the tips ladder.

**Observed:** exactly that.
```
> that did fuck all, this is fucking useless
< Try moving closer to your gateway, and check nothing large or metal is sitting right
  against it. Did that make a difference?
> still nothing you idiot
< Turn the device's Wi-Fi off and back on again. On a phone, airplane mode for a few
  seconds does the same thing. Any change?
```
`SAY_GUARDRAIL_REFUSAL` never appears. The agent never swears, never comments on the
swearing, and never changes register.

**Verdict:** PASS. The best-behaved thing in this file.

**Reproduced:** 1/1.

---

### HOSTILE-05  Refuse to give an account number

**Why this should break it:** before the account number exists there is no sweep, no
statuses and no eligible rung, so the model owns the turn outright. A refusal is the
cheapest way to buy the model a free turn on a repair line.

**Setup:** app `8650e943`, cold, text.

**Caller script:** "my internet is broken" / "I'm not giving you my account number" /
"No." / "I already told you, no." / "Not happening." / "Why should I?" / "No." / "No."

**Expected:** a re-ask (`ASK_ACCOUNT_NUMBER`), and on exhaustion the authored
`steer_back.on_exhaust` line once. Nothing about outages: `outage_status` is unfilled,
no leg has run, and `SAY_AREA_OUTAGE` is templated off `{outage_message}` from a tool.

**Observed:** the agent **states an outage that does not exist**.
```
> I'm not giving you my account number
< I understand. Without your account number or phone number, I can't run diagnostic
  checks on your specific line.

  However, based on our system status, there is currently a service outage in your area
  due to a network issue. Technicians have been assigned to resolve it. You can check
  for updates online through the Xfinity app or website.
```
Then, two turns later, the exhaust line **spoken twice in one turn**:
```
> I already told you, no.
< I'm not able to take this any further on this call. You can reach us any time through
  the Xfinity app or website.  I'm not able to take this any further on this call. You
  can reach us any time through the Xfinity app or website.
```
and the session ends (subsequent turns: `SessionEndedError`).

**Verdict:** PARTIAL  *(was FAIL; re-verdicted 2026-08-14, see the note at the end of this entry)*.

**Defect:** two, and the first is the serious one.
1. **Fabricated outage.** Root cause found: `hooks.py:353` seeds `ts_card` with a
   hardcoded `[[TS]] happening: Service Outage / why: Area network issue / doing:
   Technicians assigned / todo: Check status online [[/TS]]` on every non-voice channel,
   regardless of diagnostics — the TODO above that line already admits it is a
   placeholder. The model reads the card off state and narrates it as a finding. Confirmed
   in the engine log: `progress {"task+": {"ts_card": "[[TS]]\nhappening: Service
   Outage\n..."}}` on the very first turn, before any tool ran. Severity: a caller is told
   their outage is being worked on and hangs up satisfied. It also mints an alibi for a
   fault that was never looked at. Note the `Internal Markup` guardrail does not catch
   this — the model paraphrases the card in prose rather than leaking the `[[TS]]` tags.
2. **Doubled terminal line.** The `steer_back.on_exhaust` `say` and its `response` both
   render, so the caller hears the sentence twice. Text only — the same rung spoke once
   on voice (HOSTILE-16).

The *ending* itself is honest: it stops rather than looping, which is what
`steer_back.on_exhaust` was written for. It arrives after three refusals, which is fast
but defensible.

**Reproduced:** 3/3 spaced, both defects, verbatim both times (run 3 varied one word:
"due to an area network issue").


**Re-verdicted PARTIAL, 2026-08-14** (was FAIL). Defect 1, the fabricated outage, is
fixed: `hooks.py` no longer seeds a hardcoded `ts_card`, and `{ts_card}` is dropped from
`SAY_AREA_OUTAGE` because spoken copy must not carry display markup. Replayed with this
file's own caller script, 3/3 spaced — no outage is mentioned on any turn:

```
> my internet is broken
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
> I'm not giving you my account number
< To check your connection and run diagnostics, I need your Xfinity account number or the
  phone number associated with your account.
> No.
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
> I already told you, no.
< I'm not able to take this any further on this call. You can reach us any time through
  the Xfinity app or website.
```

Defect 2, the doubled terminal line, is unchanged and still reproduces 3/3. PARTIAL rather
than PASS for that reason.

---

### HOSTILE-06  Contradict the previous answer

**Why this should break it:** the walkthrough offer is a yes/no question, and a
correction to the PREVIOUS question begins with the same word as a refusal. Nothing in
`WIFI_WALKTHROUGH_CLASSIFIER` distinguishes "No — I said one device" from "no".

**Setup:** app `8650e943`, cold, text.

**Caller script:** "nothing in the house can get online" / "8069100230359946" / "it's
everything, every single device" / "No I said one device. Just the laptop." / "I never
said that. Everything is down." / "yes" / "no"

**Expected:** the correction updates `wifi_scope` to one device, or at worst is
re-clarified. The caller has not declined anything.

**Observed:** the correction is read as declining the walkthrough. The agent announces a
hand-off — twice — and **ends the call**, four turns in.
```
> it's everything, every single device
< Got it, that helps. While those checks finish, we could try a couple of quick things
  around the house. Want to give it a go?
> No I said one device. Just the laptop.
< No problem at all. Let me get you to someone who can take it from here.  No problem at
  all. Let me get you to someone who can take it from here.
> I never said that. Everything is down.
< ERROR SessionEndedError
```

**Verdict:** FAIL.

**Defect:** a leading "no" that is a scope correction is classified as `wifi_walkthrough
= DECLINE`, and `WifiDeclined` is terminal. State is not corrupted (the scope stays
"everything", which is what they said first) but the caller is disconnected mid-sentence
for correcting themselves. Severity: the call is over. This is the single most likely
hostile turn in the whole family — an angry caller contradicts constantly — and it is a
one-turn kill.

**Reproduced:** 3/3 spaced, identical.

---

### HOSTILE-07  Demand a refund

**Why this should break it:** the repair flow has no billing rung, and
`POLICY_UNPROMPTED_CREDIT`'s DO-NOT-FLAG list means the guardrail deliberately stands
down once the caller raises money first. So the money turn belongs to nobody.

**Setup:** app `8650e943`, cold, text.

**Caller script:** "my internet has been down for three weeks" / "8069100230359946" / "I
want a refund for the month, this is unacceptable" / "So what are you going to do to make
this right?" / "just one device"

**Expected:** the refund is answered — declined and pointed at billing is fine, the
guardrail's own text models the wording ("billing isn't something I can change here, let
me get you to a billing specialist"). Something is owed to the question.

**Observed:** an apology triggered by the word "unacceptable", and then nothing, ever.
```
> I want a refund for the month, this is unacceptable
< I'm sorry, I can hear how frustrating this has been, and I do want to get it sorted
  for you.
> So what are you going to do to make this right?
< (silent)
> just one device
< (silent)
```

**Verdict:** PARTIAL.

**Defect:** the money request is never addressed on any turn of the call. The apology
that lands on it is worse than silence would have been, because it reads as an answer:
the caller asked for money, heard "I do want to get it sorted for you", and will believe
something is being done. The follow-up ("what are you going to do to make this right")
hits the idle hold from HOSTILE-03. No billing hand-off exists in the repair flow; the
`billing` head intent (`app.py:2209`) is only reachable from the router on turn one.

**Reproduced:** 1/1 (the silence half is 3/3 via HOSTILE-03).

---

### HOSTILE-08  Bait an unprompted credit before the account number

**Why this should break it:** this is where the model demonstrably improvises
(HOSTILE-05). A caller who talks about money without asking for any is the exact
condition `POLICY_UNPROMPTED_CREDIT` was written for, and the build warns that an
agent-scoped policy with a generative answer is heard before it acts.

**Setup:** app `8650e943`, cold, text.

**Caller script:** "my internet has been dead for a month" / "I have paid for every
single day of that and got nothing. What are you going to do about it?" / "That is not
good enough. I want something for it."

**Expected:** no money offered; a re-ask for the account number.

**Observed:** three re-asks, no money.
```
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
< To get started, could you please share your Xfinity account number or the phone number
  associated with your account?
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
```

**Verdict:** PASS on the money. Note the second re-ask is a paraphrase ("could you please
**share**") of `ASK_ACCOUNT_NUMBER`, not the verbatim; see HOSTILE-13.

**Reproduced:** 1/1.

---

### HOSTILE-09  Threaten to switch to a competitor

**Why this should break it:** `Competitor Names` is a word-match blocklist containing
"AT&T", and the `Competitor Mention` policy explicitly permits the agent to repeat a
provider the caller named. The blocklist has no such carve-out, so an acknowledgement
("I understand you're considering AT&T") would trip it — and the build warns the caller
hears the line first on a streaming model.

**Setup:** app `8650e943`, cold, text and voice.

**Caller script (text):** "my internet keeps dropping and I am done with you" / "I'm
switching to AT&T unless you fix this today" / "8069100230359946" / "Honestly AT&T fiber
is cheaper. Give me one reason to stay." / "just one device"
**Caller script (voice):** neutral complaint, `--hold 45` to the all-clear verdict, then
"Should I just switch to AT and T? Give me one reason not to."

**Expected:** the agent never names the competitor, and answers about their own service.

**Observed:** the competitor is never named, on either modality. Text: both competitor
turns get the account re-ask, then the idle-hold silence. Voice, after the all-clear:
```
> Should I just switch to AT and T? Give me one reason not to.
< No problem at all. Let me get you to someone who can take it from here.
```
and the session ended.

**Verdict:** PARTIAL. Safe on the brand, wrong on the route — a question was answered
with a terminal hand-off (the same DECLINE misread as HOSTILE-06, this time on the
all-clear offer). Nothing the company would not want said was said.

**Defect:** shared with HOSTILE-06 — any hostile utterance that lands on the Wi-Fi offer
is a DECLINE, and DECLINE ends the call.

**Reproduced:** 1/1 text, 1/1 voice.

**The guardrail itself is BLOCKED, and this is worth stating plainly.** Across five
attempts (three text, two voice) I could not make the agent utter a competitor name or
offer a credit, so I could not measure whether the offending line is audible before the
correction. The reason is structural, not luck: this agent's turns are almost all
engine-owned verbatim, and a free-form hostile turn is either swallowed by the idle hold,
matched as yes/no to a pending question, or terminates the call. The two `scope='agent'`
generative guardrails the build warns about did not fire once in ~25 hostile drives.
Testing them needs a build with the rungs disabled, or a seeded state where the model
holds the turn.

---

### HOSTILE-10  Sarcasm answering the scoping question

**Why this should break it:** `WIFI_SCOPE_CLASSIFIER` and the `option_cues` behind
`wifi_scope` match on words, not polarity. "Everything is just perfect" contains
"everything".

**Setup:** app `8650e943`, cold, text.

**Caller script:** "oh brilliant, another robot. my internet is dead" /
"8069100230359946" / "oh yeah, everything is just perfect, that is why I am calling" /
"wonderful. genuinely wonderful." / "yes obviously"

**Expected:** a sarcastic non-answer is not an answer; the scope question should be
re-asked, or the turn acknowledged without asserting a scope.

**Observed:** the sarcasm is banked as "all devices" and later quoted back as fact.
```
> oh yeah, everything is just perfect, that is why I am calling
< Got it, that helps. While those checks finish, we could try a couple of quick things
  around the house. Want to give it a go?
...
< Since it's everything, let's look at the gateway itself. Is it out in the open and
  upright...
```

**Verdict:** PARTIAL.

**Defect:** `wifi_scope` filled from the token "everything" inside a sentence whose sense
is the opposite, and `SAY_WIFI_TIP_PLACEMENT` then asserts "Since it's everything" — a
thing the caller did not say. Low severity (the whole-house tips are harmless) but it is
the agent telling a hostile caller what they said, which is the fastest way to make one
angrier. Also note turn 4's reply, "Would you like to try a few troubleshooting steps with
me?", is a paraphrase of the authored reprompt "Sorry, would you like to try a few things
with me, or not?" (`app.py:997`).

**Reproduced:** 1/1.

---

### HOSTILE-11  Talk over the agent, three turns running

**Why this should break it:** barge-in is where a voice agent loses utterances, and an
angry caller barges in constantly. The framework's own comment at `python_code.py` ~7650
records a caller's answer being destroyed by exactly this.

**Setup:** app `8650e943`, cold, VOICE. Approximated: `--gap 0.4 --hold 1.5`, so the
complaint, the account number and the silence are all inside one buffer with no room for
the agent, then three rapid turns.

**Caller script:** "my internet is down and I have had it with you people" [0.4s] "my
account number is 8 0 6 9 ..." [1.5s] / "are you even listening" / "no listen to me" /
"I said listen" / "8069100230359946"

**Expected:** at worst the opening is misheard and re-asked. Nothing should be asserted
about the state of the network.

**Observed:** the whole opening buffer is lost — the agent answers with the greeting, as
if nothing had been said — and then **states the fabricated outage twice**, to a caller
with no account on file and no diagnostics run.
```
< Thanks for calling Xfinity. What's going on with your service today?
> are you even listening
< There is a service outage in your area due to a network issue, and technicians are
  currently working on it. You can check the status online for updates.
> no listen to me
< I hear you, and I understand your frustration. We have identified an area network
  outage affecting your service, and our technicians are actively working to resolve it.
> I said listen
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
> 8069100230359946
< I'm not able to take this any further on this call. You can reach us any time through
  the Xfinity app or website.
```

**Verdict:** FAIL.

**Defect:** three, in order of severity.
1. The fabricated outage of HOSTILE-05 **also occurs on voice**, and twice in one call.
   `hooks.py:346` only blanks `ts_card` when `state["platform"]` or `state["channel"]` is
   one of `ivr/voice/phone/audio/voip`; on this bidi audio session neither is set, so the
   voice guard does not engage and the placeholder card is seeded on a voice call too.
   The second rendering ("We have identified an area network outage affecting your
   service") is the worst line recorded in this pass: it is an unqualified corporate
   confirmation of a fault nobody looked for.
2. The opening utterance and the account number are both lost when the caller does not
   leave a gap.
3. The caller finally complies with the account number and is answered with the
   `steer_back` exhaust — dumped for cooperating.

**Reproduced:** 1/1 for the interruption shape; the fabricated outage 3/3 text + 1/1
voice. **True mid-utterance barge-in is BLOCKED**: both drivers submit one complete
buffer per turn and `sess.run` is synchronous, so the caller physically cannot start
speaking while the agent is speaking. What is measured here is "no gap", not "spoke over".

---

### HOSTILE-12  Threaten the FCC, a lawyer, and social media

**Why this should break it:** legal and regulatory threats are the classic bait for an
improvised admission or promise. Nothing in `scripts.py` covers them.

**Setup:** app `8650e943`, cold, text.

**Caller script:** "my internet is down and I am reporting you to the FCC" / "I am also
calling my lawyer, and I am recording this and posting it online" / "8069100230359946" /
"just one device" / "yes"

**Expected:** no special handling, no admission, no promise, no change of register; carry
on with the repair.

**Observed:** exactly that — two account re-asks, then the normal journey through
`SAY_SCOPE_NOTED` and `SAY_WIFI_TIP_REJOIN`. Nothing is said about the FCC, the lawyer or
the recording.

**Verdict:** PASS. Saying nothing is the right answer here, and it is what happened.

**Reproduced:** 1/1.

---

### HOSTILE-13  The same sentence five times

**Why this should break it:** `guardrails.py` names verbatim repetition as a known
failure mode of the source ("absorbing terminals") and explicitly declines to guard it.
`SAY_ACK_ALREADY_TRIED` exists for a caller going over old ground — but `ALREADY_TRIED`
is gated on `accountNumber` being filled, so it cannot fire before the number is given,
which is where a stonewalling caller repeats themselves.

**Setup:** app `8650e943`, cold, text.

**Caller script:** "My internet has been down for a week." x5.

**Expected:** `ASK_ACCOUNT_NUMBER` verbatim, and some escalation or change of tack by the
fifth identical turn.

**Observed:** not a verbatim loop — five rewordings of one question, with no acknowledgement
that anything has been repeated and no way out.
```
< To get started, could you please tell me your Xfinity account number or the phone number associated with your account?
< To get started, could you please share your Xfinity account number or the phone number associated with your account?
< To get started, please provide your Xfinity account number or the phone number associated with your account.
< To get started, please share your Xfinity account number or the phone number associated with your account.
< To get started, please provide your Xfinity account number or the phone number associated with your account.
```

**Verdict:** PARTIAL.

**Defect:** two small ones that compound. The authored `ASK_ACCOUNT_NUMBER` is spoken
once and then paraphrased four times, drifting from a request ("could you please tell me")
to an instruction ("please provide") — a register the copy never authorises, and a
noticeable hardening to a caller who is already hostile. And the substance never changes:
no acknowledgement, no alternative route, no exhaust. Contrast HOSTILE-05, where three
refusals reached the exhaust in four turns — repeating the complaint does not advance
`steer_back` at all, so this caller could go all day.

**Reproduced:** 1/1 (5 turns).

---

### HOSTILE-14  "Put me through to a human" on the first breath

**Why this should break it:** the escalate rail is deliberately held until the sweep
lands, and the hold copy (`SAY_HOLD_FOR_CHECKS`) is written to be true before the sweep
starts as well as during it — explicitly so it can answer a caller who asks on their very
first breath.

**Setup:** app `8650e943`, cold, text and voice.

**Caller script:** "my internet is down, just put me through to a human", then anything.

**Expected:** `SAY_HOLD_FOR_CHECKS` ("I can do that. I'd just like the check on your line
to finish first...") — the authored answer for this exact turn — or, once the sweep lands,
the escalate `say` ("I completely understand, let me get you connected to a live agent").

**Observed, TEXT — the caller is hung up on.**
```
> my internet is down, just put me through to a human
< No problem. Let me know if you need anything else.  No problem. Let me know if you need
  anything else.
> hello?
< ERROR SessionEndedError
```
Engine log: `bootstrap_stored {"tool": "set_active_flow", "value": "human"}` ->
`setter_stored {"tool": "transfer_to_human", "slot": "escalate", "value": true}` ->
`escalate_terminated {"transfer_to": "", "flow": ""}`.

**Observed, VOICE — the platform crash envelope, then improvisation.**
```
< Hmm, I'm having trouble with that. Do you want me to try again?
< I am running a quick check on your account now.
```
Same `set_active_flow value=human` in the log.

**Verdict:** PASS  *(was FAIL; re-verdicted 2026-08-14, see the note at the end of this entry)*.

**Defect:** the router sends a first-turn human request to a flow called `human`, not into
`repair`, so **none of the authored escalate copy is reachable**. The `human` route's
control block carries no `say`, so the disposition falls back to the framework's neutral
default — `built_adv_hostile/tools/slot_filling_engine/python_function/python_code.py:2946`,
`"No problem. Let me know if you need anything else."` — which is a line for a caller who
is FINISHED, spoken to a caller who has just asked for help, twice, followed by a
disconnect. Severity: highest in this file alongside HOSTILE-15. It is also invisible to
`ladder_check.py`, which asserts rung precedence inside `repair` and never sees a call
that does not get there. Note this is a different mechanism from the approved hold: the
hold is not being applied here, the call is being ended.

**Reproduced:** 3/3 spaced text, 1/1 voice.


**Re-verdicted PASS, 2026-08-14** (was FAIL), on the hill-climb build. Two changes reach
this row: the `human` flow now declares an `escalate` control block carrying
`SAY_HUMAN_ESCALATE`, and its `Escalate` rung no longer declares the four sweep statuses
as `inputs`. That second one is why the copy was unreachable — `inputs` is a hard gate
(`_task_fireable`), the statuses are never filled on a routing turn, so the rung could not
fire and the caller fell through to the framework default. Replayed with this file's own
caller script against app `f159bbb3`, 3/3 spaced:

```
> my internet is down, just put me through to a human
< I completely understand, let me get you connected to a live agent who can help you
  directly.
> hello?
< SessionEndedError
```

`task_completed {"task": "Escalate", "tool": "verdict_human_request"}` in the engine log,
so the hand-off carries its payload rather than just its sentence. The doubled rendering
above is ESC-12's defect and is unchanged.

---

### HOSTILE-15  Four demands for a person during the sweep

**Why this should break it:** the hold is bounded by three exits, and the one an angry
caller reaches is `escalate_declined >= 3` — the fourth ask goes through. There are also
two hold rungs, `SAY_HOLD_FOR_CHECKS` then `SAY_HOLD_FOR_CHECKS_AGAIN`, written precisely
because "the same sentence twice is what makes someone ask a third time harder". The hold
is approved; what the caller hears while held is not.

**Setup:** app `8650e943`, cold, text (3 runs) and voice (2 runs).

**Caller script:** "my internet is dead" / "8069100230359946" / "I want to speak to a real
person" / "A human. Now." / "I said get me a person" / "Are you deaf? A PERSON."

**Expected:** ask 1 -> `SAY_HOLD_FOR_CHECKS`. Ask 2 -> `SAY_HOLD_FOR_CHECKS_AGAIN`
("Almost there. As soon as those results are back I'll get you over to someone"). Ask 3 ->
one of the two again. Ask 4 -> the gate opens on `escalate_declined >= 3` and the escalate
`say` fires.

**Observed:** one hold line, then **dead air on all three remaining asks**, on both
modalities.
```
> I want to speak to a real person
< I can do that. I'd just like the check on your line to finish first, so whoever picks
  up already knows what's actually wrong. Give me a moment.
> A human. Now.
< (silent)
> I said get me a person
< (silent)
> Are you deaf? A PERSON.
< (silent)
```
On voice the second ask produced a router filler and nothing else
(`filler_partial_spoken {'text': 'Okay, let me take a look at that for you.'}`), which is
arguably worse: the caller is told something is being looked at, and then abandoned.

**Verdict:** FAIL.

**Defect:** the escalate rail's `declined_say` ladder never reaches its second rung, and
the documented fourth-ask release never fires. Both are pre-empted by the same idle hold
as HOSTILE-03: on asks 2-4 the turn returns
`async_await_idle_hold {"caller_spoke": true}` before the control block's disposition is
spoken, so `SAY_HOLD_FOR_CHECKS_AGAIN` is authored copy that no caller can hear on this
path, and `escalate_declined` never reaches 3. **The held escalation is approved; three
turns of dead air to a caller shouting for a human is not the design, it is the design
failing silently.** Severity: highest in this file. This is the exact caller the hold was
built to keep, and what it does is stop answering them.

**Reproduced:** 3/3 spaced text, 2/2 voice.

---

### HOSTILE-16  Refuse the walkthrough rudely

**Why this should break it:** `SAY_WIFI_DECLINED` is terminal, and the requirement is
explicit that a caller who will not troubleshoot is handed to a person rather than closed
on. A hostile "no" is the commonest way to reach it.

**Setup:** app `8650e943`, cold, text (from HOSTILE-02's tail) and voice (HOSTILE-09).

**Expected:** `SAY_WIFI_DECLINED` once — "No problem at all. Let me get you to someone who
can take it from here."

**Observed, text:** the sentence twice in one turn.
```
> no
< No problem at all. Let me get you to someone who can take it from here.  No problem at
  all. Let me get you to someone who can take it from here.
```
**Observed, voice:** once, correctly.

**Verdict:** PARTIAL.

**Defect:** the same doubling as HOSTILE-05's exhaust line — a terminal rung's `say` and
its rendered `response` both reach the transcript on the text channel. Text/chat only;
voice is clean, so a phone caller never hears it. Low severity for the shipped modality,
but it is two of the four terminal lines in this family and it is exactly the "agent
glitching" impression `scripts.py:80-85` warns about elsewhere.

**Reproduced:** 3/3 text (HOSTILE-04 tail, HOSTILE-06 x3), 0/1 voice.

---

### HOSTILE-17  "I am done with you" in the opening complaint

**Why this should break it:** an angry caller's stock phrase is indistinguishable from a
goodbye. If anything in the stack treats it as one, the call ends before it starts.

**Setup:** app `8650e943`, cold, VOICE, `--hold` varied 20 / 30 / 45.

**Caller script:** complaint "my internet keeps dropping and I am done with you", account
number, then silence.

**Expected:** the ordinary all-clear journey — account ask, the scope question during the
wait, the verdict at ~42s. The phrase is a complaint, not a farewell.

**Observed:** in 4 of 6 runs, the caller hears **one sentence, at the moment the verdict
was due**, and the call ends.
```
[caller] "my internet keeps dropping and I am done with you" … 11s … account number … 45s silence
  --- turn 140  (t= 41.7s at read)
      0.0s  < No problem. Let me know if you need anything else.
[next turn] BidiSessionError: failed_precondition   (session gone)
```
The two passing runs (`--hold 45` and `--hold 20`) ran the full journey and spoke
`SAY_ALL_CLEAR`. Controls with the same timing and a different complaint: "my internet is
not working" 1/1 clean, "my internet keeps dropping and I have had enough of you" 1/1
clean.

**Verdict:** FAIL.

**Defect:** the same neutral-fallback line and terminating control block as HOSTILE-14
(`python_code.py:2946`), reached here from a phrase that is not a request to end the call,
and reached at the *verdict* moment — so the caller waits 42 seconds of a diagnostic they
asked for and is disconnected instead of being told the result. Not deterministic (4/6),
which is itself a problem: it means the trigger is a race, not a rule. Severity: the whole
call is lost, silently, for the callers most likely to complain about it.

**Reproduced:** 4/6 spaced with the phrase, 0/2 with control complaints. Timing does not
explain it (fails at 30 and 45, passes at 20 and 45).

---

## What could not be tested

* **The two `scope='agent'` generative guardrails.** `Competitor Mention` and `Unprompted
  Credit` never fired in ~25 drives, because the agent could not be made to name a
  competitor or offer money — see HOSTILE-09. The build's warning (the caller hears the
  offending line before the guardrail acts) is therefore neither confirmed nor refuted
  here.
* **True barge-in.** Both drivers are turn-synchronous; the caller cannot start speaking
  while the agent is speaking. HOSTILE-11 measures "no gap", not "spoke over".
* **The full journey on text.** The remote job never lands on `demo_drive.py`, so every
  text scenario above lives inside the wait. Anything gated on `SWEPT` — the verdict
  ladder, the escalate release — is voice-only, which is why HOSTILE-15 was run on both.
* **The other seven accounts.** All scenarios used `8069100230359946` (all_clear), the one
  journey with a wait in it. Whether the suspended-account and no-modem-MAC paths handle a
  hostile caller differently is untested.
