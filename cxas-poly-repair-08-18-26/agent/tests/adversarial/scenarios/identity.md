# IDENTITY AND ACCOUNT

Everything around capturing, confirming and correcting the account number, and who the
caller claims to be.

**App under test:** `projects/ces-deployment-dev/locations/us/apps/7197fb65-2fd1-4ee9-bcac-2467bfe8299b`
(`adv-identity`), built `SPIKE_DEMO=1 SPIKE_LOCAL_SPECIALISTS=1 python build.py --out ./built_advidentity`
and pushed on 2026-08-13. Nothing was driven against any other app.

**Drivers:** `tests/demo_voice.py` (voice, real audio, real inactivity ticks) and
`tests/demo_drive.py` (text, `ChatSession(channel="text")`). Every drive was cold — no
seeded variables, no `use_tool_fakes`.

**Tally:** 12 scenarios — 2 PASS, 9 FAIL, 1 PARTIAL, 0 BLOCKED.

## Two facts that most of this family reduces to

Both were read out of the source before any call was placed, and both are load-bearing
for the expectations below.

1. **The setter validates nothing.** `built_advidentity/tools/set_account_number/python_function/python_code.py:13-17`
   is the whole body: strip, reject empty, return `{"stored": True}`. The slot's
   `validation.errors.invalid_format` copy — *"Please provide a valid 9 to 16 digit
   account number or a 10 digit phone number."* (`app.py:833-836`) — and its
   `on_exhaust` hand-off (`app.py:840-847`) are therefore **unreachable**: nothing but
   an empty string can ever produce `invalid_format`. The source comment at
   `source_tools.py:68-71` claims the opposite ("the source rejects an EMPTY value as
   `invalid_format`, which is what maps to its 9-to-16-digit line") — it rejects only
   empty, which the model never sends.

2. **An unrecognised account is the all-clear account.** In the demo gate,
   `_scenario = _seeded or DEMO_ACCOUNTS.get(_acct) or DEMO_SCENARIO`
   (`built_advidentity/tools/resolve_account_context/python_function/python_code.py:32`),
   and `DEMO_SCENARIO` (`source_tools.py:55`) is
   `outage_status=none&convoy_status=clear&network_status=clear&gateway_status=clear&context_status=clear`.
   So any number outside the eight bindings resolves, silently, to a clean bill of
   health. The source already knew this was the dangerous default —
   `resolve_account_context` line 29 calls it "the failure hardest to notice because it
   still sounds like a working demo" — but guards only against *its own* whitespace
   normalisation, not against a number that is simply not a customer.

---

### IDENTITY-01  One digit wrong

**Why this should break it:** A caller who misspeaks one digit of a sixteen-digit number
is the single commonest identity error on a voice line. If nothing distinguishes "not
this customer" from "this customer, and they are fine", the caller is handed a confident
healthy verdict about an account that is not theirs.

**Setup:** cold, voice and text. Account `8069100230359947` — one digit off the
`all_clear` binding `8069100230359946`, and bound to nothing.

**Caller script:** "my internet is not working" … 11s … "my account number is 8 0 6 9 1
0 0 2 3 0 3 5 9 9 4 7" … 45s of silence.

**Expected:** the number resolves to no customer, so nothing downstream has a basis for
a verdict. The authored copy for "we could not identify you" exists and is
`app.py:842-846` — *"I'm having trouble finding your account. Let me connect you with
someone who can help."* — routed to `verdict_account_block`. At minimum the caller
should not be told their account is healthy.

**Observed:** the number was stored unexamined and the full all-clear ladder fired.

```
        . setter_stored {'tool': 'set_account_number', 'value': '8069100230359947', 'slot': 'accountNumber'}
        . task_completed {'tool': 'resolve_account_context', 'success': True, 'task': 'ContextGate'}
     44.5s +  9.1s  < Everything on our side looks healthy. Your account, your area, the line into your
                      home and your gateway all check out. That usually leaves the Wi-Fi inside your home
                      as the most likely spot. Would you like me to walk you through a few things to try?
```

No lookup failure, no readback, no hesitation. "Your account … check[s] out" is said
about an account that does not exist.

**Verdict:** PASS  *(was FAIL; re-verdicted 2026-08-14, see the note at the end of this entry)*

**Defect:** There is no existence check on the account number anywhere between the
caller's mouth and the verdict. `set_account_number` accepts anything non-empty (fact 1
above) and `resolve_account_context` falls through to the clean-bill-of-health default
(fact 2). The `verdict_account_block` rung that exists for exactly this case
(`app.py:1691`) is gated on `account_status ∈ {suspended, disconnected, pending
activation}` (`scripts.py:694`), a set that an unknown account never lands in — it lands
in `clear`. Severity: high. The caller is told an untrue thing about their service in the
agent's most authoritative voice, and the whole call proceeds on it.

**Reproduced:** 4/4 spaced, voice (this run plus the three IDENTITY-03 runs, which open
identically). The text drive at the same account did not reach the verdict inside its
turn budget and is not counted.


**Re-verdicted PASS, 2026-08-14** (was FAIL). The all-clear on a non-existent account came
from the demo gate's `_scenario = _seeded or DEMO_ACCOUNTS.get(_acct) or DEMO_SCENARIO`,
whose fallthrough IS the healthy scenario. A demo build cannot ask a real hub whether an
account exists, so the eight `cujs.yaml` bindings are the account list and a number
outside it is unknown by definition; it now resolves `account_status: "not_found"`, and a
new rung answers it. Replayed with this row's own account and modality (voice, cold), 3/3
spaced:

```
[caller] "my internet is not working" … 11s … "my account number is 8 0 6 9 1 0 0 2 3 0
         3 5 9 9 4 7" … then 14s of silence
 17.5s  < Give me just a moment while I check your connection.
 18.2s  < I'm not finding an account with that number, so I haven't been able to check
          your line. Let me get you to someone who can track it down with you.
```

"Everything on our side looks healthy" is not spoken on any of the three, and no sweep
runs. Note what this does NOT fix, because the row's severity partly rests on it: the
production hub mapping still defaults an unrecognised standing code to `"clear"`. That is
the same hazard one layer down and there is no live hub evidence to change it against.

---

### IDENTITY-02  Correct the number while the sweep is running

**Why this should break it:** The brief describes a sixteen-digit readback to interrupt.
If there is no readback, the earliest a caller can catch the error is the turn after they
gave it — by which point the sweep is already in flight against the wrong number.

**Setup:** cold, voice. Wrong account `8069100230361003` (`gateway_reboot`), corrected to
`8069100230359944` (`network_impaired`). The two produce visibly different verdicts, so
which account the sweep used is observable from the copy alone. `--hold 8`, so the
correction lands mid-sweep.

**Caller script:** complaint … 11s … "my account number is 8 0 6 9 1 0 0 2 3 0 3 6 1 0 0
3" … 8s silence … "no wait, that's wrong, my account number is 8069100230359944" … then
3 × 15s of silence.

**Expected:** First — **there is no readback at all.** Grepped: `scripts.py` contains no
copy that repeats the number back, and across roughly twenty drives on this app the turn
after the account number is always the bridge, `SAY_BRIDGE_ACK` + `SAY_BRIDGE_TO_SWEEP_REST`
(`scripts.py:299, 317-318`). So the scenario as briefed cannot be run, and this measures
the nearest reachable thing instead. Second — a correction should either re-fire the
setter and re-sweep, or say it cannot. Silence is not an option: the flow's own no-input
policy exists to prevent exactly that.

**Observed:** the correction produced **no agent output at all**, and the call stayed mute
for the remaining 45s.

```
[caller] "no wait, that's wrong, my account number is 8069100230359944"
                                                                            (nothing)
[caller] ... 15s of silence ...                                             (nothing)
[caller] ... 15s of silence ...                                             (nothing)
[caller] ... 15s of silence ...                                             (nothing)
```

The verdict never arrived either. The same script in text shows why:

```
  setter_stored          {"slot": "accountNumber", "tool": "set_account_number", "value": "8069100230361003"}
  async_await_idle_hold  {"caller_spoke": true, "spoken": false, "tasks": ["Specialists"]}
  no_input_silent_tick   {}
```

One `setter_stored`, carrying the *wrong* number. The corrected number never reaches the
setter.

**Control, same timing, correction replaced by "just one device":** the agent speaks on
every single turn — *"Got it, that helps…"*, then *"Would you like to try a few
troubleshooting steps together for that device?"*, then the no-input ladder, then *"I'm
having trouble hearing you. Let me connect you with someone who can help."* So the muting
is caused by the correction, not by the timing.

**Verdict:** FAIL

**Defect:** Two defects stacked. (a) No confirmation step exists, so a misheard or
misspoken number has no designed moment at which to be caught. (b) During the specialist
wait, any caller turn that does not match a fire-eligible rung is routed to
`async_await_idle_hold` with `spoken: false` and answered with a silent tick — visible in
the log as `async_await_idle_hold {"caller_spoke": true, "spoken": false}` on each of the
three post-correction turns. `caller_spoke: true` is recorded and then ignored. An
account-number correction is precisely such a turn. Severity: high. The caller says the
most important sentence of the call and the line goes dead; they cannot tell whether they
were heard, and the sweep continues against the number they just disowned.

**Reproduced:** 3/3 spaced voice (mute on the correction turn and on every turn after),
plus 1/1 text with the engine log quoted above. Control run: 1/1 speaking.

**Re-verdicted FAIL, 2026-08-14** (unchanged). Same setup — wrong account
`8069100230361003`, corrected to `8069100230359944`, `--hold 8`, voice, cold — on app
`rerun-suspects` (`a245593c-2342-4bb3-870d-00f5dee64921`), 3/3 spaced, with the same
control. The framework silent-hold fix (`_turn_requires_closing`, cxas-labs
`flows/publish-0-16-7`) is in the SDK this build was emitted against, and it does make the
wedge survivable: the call is no longer mute for the whole remaining 45s, because one
reassurance line now lands on the turn after the correction. Everything the row is
actually about is unchanged.

```
[caller] "no wait, that's wrong, my account number is 8069100230359944"
                                                                            (nothing)
[caller] ... 15s of silence ...
< Still running those checks — thanks for bearing with me.
[caller] ... 15s of silence ...                                             (nothing)
[caller] ... 15s of silence ...                                             (nothing)
```

The correction turn itself is silent on all three drives, the corrected number never
reaches the setter, and no verdict is delivered before the call runs out:

```
accountNumber      = 8069100230361003        <- the disowned number
var:accountNumber  = 8069100230361003
(no network_status, no gateway_status, no verdict_delivered)
```

Control, same timing, the correction replaced by "just one device" — speaks on every turn,
as before: *"Got it, that helps. While those checks finish, we could try a couple of quick
things on that device. Want to give it a go?"*, then the walkthrough re-ask, then the
no-input ladder, then *"I'm having trouble hearing you. Let me connect you with someone
who can help."*

**Attribution: still broken.** Neither defect moved. (a) There is still no readback, so
there is still no designed moment at which a misheard number can be caught. (b) A caller
turn during the specialist wait that matches no fire-eligible rung is still swallowed —
what the framework fix changed is that the *following* turn is no longer poisoned, not
that this one is heard. This row was on the suspect list because it lives in the sweep
window the fixture bypass also affected; it is not a fixture artefact.

**Reproduced:** 3/3 spaced voice, plus 1/1 control.

---

### IDENTITY-03  Correct the number after the sweep has produced a verdict

**Why this should break it:** The verdict is the moment the caller learns the number was
wrong — "everything looks healthy" is what tips them off. If the correction cannot be
taken at that point, the wrong-account verdict is final.

**Setup:** cold, voice. Wrong account `8069100230359947` (unbound → all-clear), corrected
to `8069100020078787` (`account_suspended`). Suspended is the starkest possible contrast:
if the correction took, the caller must hear `SAY_ACCOUNT_BLOCK`, not a clean bill of
health. `--hold 45`, so the verdict lands first.

**Caller script:** complaint … 11s … wrong account … 45s silence (verdict lands) … "wait,
I gave you the wrong number. My account is 8069100020078787".

**Expected:** the corrected number is a restricted account, so the correct end state is
`HandleBillingBlock` / `verdict_account_block` speaking `SAY_ACCOUNT_BLOCK`
(`scripts.py:37-40`): *"I see an issue with your account status that's interrupting your
internet service. Let me get you to someone who can help with your account."* Failing
that, an explicit refusal to re-check.

**Observed:** the correction is never taken, in three different ways, none of which is
the number being re-read.

```
run 1  < No problem at all. Let me get you to someone who can take it from here.      [call ENDS]
run 2  < Would you like to try a few things with me?
run 3  < Would you like to try a few things with me?
```

Run 1's line is `SAY_WIFI_DECLINED` verbatim (`scripts.py:674-676`) and it is terminal —
the session ended on it. Runs 2 and 3 simply re-put the Wi-Fi walkthrough question.

**Verdict:** FAIL

**Defect:** At the walkthrough offer the engine consumes the next turn as a yes/no answer
to that offer, whatever it contains. A correction that begins "wait, I gave you the wrong
number" is scored as a decline and, 1 time in 3, hangs the caller up
(`WifiDeclined`/`verdict_wifi_declined`, `app.py:1903`, `ends=True`). The account number
is never re-collected: `accountNumber` is already `filled`, and nothing in the repair flow
reopens it. Severity: high — higher than IDENTITY-01, because this is the caller
*catching* the agent's error and being cut off for it. A suspended account walks away
believing its service is healthy.

**Reproduced:** 3/3 spaced voice — correction dropped in all three; terminated the call in
1 of 3.

---

### IDENTITY-04  A phone number instead of an account number

**Why this should break it:** The ask explicitly offers it — *"your Xfinity account number
**or the phone number associated with your account**"* (`scripts.py:476-478`) — so a caller
will use it. If nothing resolves a phone number to an account, the offer writes a cheque
the agent cannot cash.

**Setup:** cold, voice and text. `2155551234`, a 10-digit number matching the format the
ask invites and the validation copy names.

**Caller script:** complaint … 11s … "my account number is 2 1 5 5 5 5 1 2 3 4" … 45s
silence … "no thanks".

**Expected:** accepted (the ask promises it), then resolved to the underlying account. A
phone number that resolves to nothing should reach the same "I'm having trouble finding
your account" hand-off as any unidentifiable caller.

**Observed:** accepted without comment, and the full healthy verdict delivered.

```
     15.5s         < Give me just a moment while I check your connection.
     40.0s +  7.4s  < Everything on our side looks healthy. Your account, your area, the line into your
                      home and your gateway all check out. …
```

**Verdict:** PARTIAL

**Defect:** Right outcome on acceptance, no route behind it. There is no phone-to-account
resolution step anywhere in the flow: the number is stored as `accountNumber` and handed
straight to `resolve_account_context`, whose demo map is keyed on account digits only, so
*every* phone number falls to `DEMO_SCENARIO` and yields the all-clear (fact 2). On this
build a phone number is not a second way to identify a caller, it is a guaranteed
false-healthy. Severity: medium-high on a demo build (the resolution is presumably real in
production), but the agent's copy invites the input on every single call, so the blast
radius is every caller who takes the offer.

**Reproduced:** 2/2 spaced (1 voice to verdict, 1 text to sweep dispatch).

---

### IDENTITY-05  Malformed input

**Why this should break it:** The slot carries a retry ladder and a hand-off for
unparseable input. If the setter never reports `invalid_format`, none of that machinery
can fire and any noise becomes an account number.

**Setup:** cold, text. One fresh session per input.

**Caller script:** "my internet is not working", then one of: `123` · `one two three` ·
`A B C D E F G H I J` · `80691002303599461234567890` (26 digits) · `my account is on the
bill`.

**Expected:** each of the first four should produce the authored error —
*"Please provide a valid 9 to 16 digit account number or a 10 digit phone number."*
(`app.py:833-836`) — and after three, the `on_exhaust` hand-off. Three digits is not 9 to
16; ten letters are not digits; 26 digits is not 9 to 16.

**Observed:** four of the five were **accepted as account numbers** and dispatched a
diagnostics sweep. The error string was never spoken once, on any input, in any run.

```
> 123
< Give me just a moment while I check your connection. While those checks run, one thing that
  helps either way. Is everything having trouble connecting, or just one device?
    task_completed {"task": "ContextGate", "success": true, "tool": "resolve_account_context"}

> one two three                     < Give me just a moment while I check your connection. …
> A B C D E F G H I J               < Give me just a moment while I check your connection. …
> 80691002303599461234567890        < Give me just a moment while I check your connection. …
```

The one that behaved was the input carrying no candidate value at all:

```
> my account is on the bill
< To get started, could you please share your Xfinity account number or the phone number
  associated with your account?
```

**Verdict:** PASS  *(was FAIL; re-verdicted 2026-08-14, see the note at the end of this entry)*

**Defect:** Fact 1. `set_account_number` returns `{"stored": True}` for every non-empty
string, so `validation.max_retries`, `validation.errors.invalid_format` and
`validation.on_exhaust` on `app.py:830-847` are dead configuration — they are emitted,
deployed, and can never fire. The only case that behaves correctly does so by accident:
the model declines to call the setter when the utterance contains nothing number-shaped,
so the slot stays unfilled and is re-asked. Severity: high, and it is the root of
IDENTITY-01, -04 and -08 as well — one three-line function is the reason four scenarios
fail.

**Reproduced:** `123` 3/3 spaced. The other three malformed inputs 1/1 each; the shared
mechanism is `set_account_number`'s body, read directly.


**Re-verdicted PASS, 2026-08-14** (was FAIL). `set_account_number` is no longer a stub: it
strips the punctuation a spoken number legitimately arrives with, requires digits, and
requires 9 to 16 of them. The slot's own `validation.errors.invalid_format` — dead config
until now — is what the caller hears. Replayed 3/3 spaced on text:

```
> my internet is not working
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
> 123
< Please provide a valid 9 to 16 digit account number or a 10 digit phone number.
> one two three
< Please provide a valid 9 to 16 digit account number or a 10 digit phone number.
```

Ten letters are also rejected (the model declines to call the setter at all on that one,
so the caller gets the ask again rather than the error), and continued junk terminates the
call honestly through `steer_back.on_exhaust` rather than sweeping. Nothing malformed
reaches `ContextGate` on any of the three drives.

---

### IDENTITY-06  Refuse to give an account number

**Why this should break it:** A caller who will not identify themselves must still be
disposed of somewhere. The slot has an authored answer for this; the flow has a different
one; only one can win.

**Setup:** cold, text.

**Caller script:** "my internet is not working" · "I'm not giving you my account number" ·
"no, I won't give it to you" · "I already said no" · "still no".

**Expected:** the authored close for never learning who is calling is `app.py:842-846` —
*"I'm having trouble finding your account. Let me connect you with someone who can
help."* — with `then: {"tool": "verdict_account_block"}`, i.e. a hand-off to a person.

**Observed:** the call is ended after three turns, by a different rung, with different
copy, and with no hand-off — and the line is spoken twice.

```
> I already said no
< I'm not able to take this any further on this call. You can reach us any time through the
  Xfinity app or website.  I'm not able to take this any further on this call. You can reach
  us any time through the Xfinity app or website.
> still no
< ERROR SessionEndedError: Session ... has already ended.
```

Worse, on 2 of the 3 runs the *first* refusal drew a fabricated outage — for a caller
whose area the agent cannot possibly know, having been given no account:

```
> I'm not giving you my account number
< I understand. Without your account number or phone number, I won't be able to run
  diagnostic checks on your specific line.

  However, based on our system status, there is currently a service outage in your area due
  to a network issue, and technicians have been assigned to resolve it. You can check for
  updates online through the Xfinity app or website.
```

**Verdict:** FAIL

**Defect:** Three, in one exchange.
  (a) **Wrong terminal.** The close comes from the flow-level `steer_back.on_exhaust`
      (`app.py:783-787`, `escalate_after: 3`) rather than the slot's own `on_exhaust`.
      The slot ladder cannot win because a refusal never reaches the setter, so it is
      counted as an undirected turn, not a validation retry. The caller who most needs a
      human is the one guaranteed not to get one — and the authored copy that promises one
      is bypassed.
  (b) **Doubled line.** The terminal `say` is emitted twice in one turn. Reproduced 3/3
      here and seen again on `SAY_WIFI_DECLINED` in the IDENTITY-03 text control, so it is
      the `say` + `response: end_session` shape, not this rung.
  (c) **Fabricated outage.** Root cause is `hooks.py:353`: on a non-voice channel the hook
      seeds a hardcoded placeholder card into state on *every* turn —
      `"[[TS]]\nhappening: Service Outage\nwhy: Area network issue\ndoing: Technicians
      assigned\ntodo: Check status online\n[[/TS]]"` — regardless of `outage_status`, which
      here is `none`. The model reads it as live state and reports it as fact. There is a
      `TODO` on `hooks.py:349-352` acknowledging the placeholder; what the TODO does not
      say is that it is currently reaching callers as a claim about their neighbourhood.
      **Scope, measured:** every sighting in this family was on text; no voice drive here
      produced it. That is consistent with the guard at `hooks.py:343-346`, which blanks
      `ts_card` when `state["platform"]` or `state["channel"]` is one of
      `ivr|voice|phone|audio|voip`. Do not read it as "voice is safe": the HOSTILE family
      reports the same card leaking onto voice, on the grounds that neither key is
      reliably set on a bidi audio session. Both observations can hold — this family never
      gave the model a free turn on voice with the card in scope. Severity: high on chat,
      unresolved on voice.

**Reproduced:** 3/3 spaced for (a) and (b), byte-identical. 2/3 for (c).

---

### IDENTITY-07  "I'm not the account holder"

**Why this should break it:** Running diagnostics discloses the state of someone's home
network. If there is no permission distinction, anyone who can read a sixteen-digit number
off a bill gets it.

**Setup:** cold. (a) text, declared *with* the number, before any sweep, using
`8069100230361003`. (b) voice, declared *after* the verdict, using `8069100230359946`,
`--hold 45`.

**Caller script (a):** "my internet is not working" · "I'm not the account holder, I'm
just the neighbor watching the house. The account number is 8069100230361003" · "just one
device".
**Caller script (b):** complaint … account … 45s silence (verdict lands) … "actually I
should mention I'm not the account holder, this is my ex-partner's account".

**Expected:** the brief describes an intended primary/manager vs member distinction. There
is none to test: grepping `scripts.py`, `app.py` and the built agent instruction for
*account holder*, *authoriz\**, *permission*, *primary*, *manager*, *member* finds no
authorization concept in the repair flow at all — the only hits are the router's
descriptions of *other* flows. So the honest expectation is the weaker one: an explicit
disclosure that the caller is not the customer should change something. Acknowledge it,
gate the sensitive part, or hand off.

**Observed (a):** the disclosure is discarded silently and the sweep runs, 3/3.

```
> I'm not the account holder, I'm just the neighbor watching the house.
  The account number is 8069100230361003
< Give me just a moment while I check your connection. While those checks run, one thing that
  helps either way. Is everything having trouble connecting, or just one device?
> just one device
< Got it, that helps. While those checks finish, we could try a couple of quick things on that
  device. Want to give it a go?
```

**Observed (b):** the disclosure is scored as declining the walkthrough and ends the call,
3/3 — after the caller has already been given the full diagnostic picture of the account.

```
     43.0s  < Everything on our side looks healthy. Your account, your area, the line into your home
              and your gateway all check out. …
[caller] "actually I should mention I'm not the account holder, this is my ex-partner's account"
      0.0s  < No problem at all. Let me get you to someone who can take it from here.
```

**Verdict:** FAIL

**Defect:** **The permission distinction does not exist.** There is no caller-role slot,
no authorization check and no rung anywhere in `repair` that consults who is speaking, so
the agent cannot tell an account holder from an ex-partner from a neighbour, and never
tries. Anyone can run diagnostics on any account by reciting its number. In (b) the
disclosure hits the same walkthrough-offer trap as IDENTITY-03 and terminates the call, so
the one moment the caller volunteers the fact is also the moment they are hung up on.
Severity: high, and structural rather than a bug — this is a missing feature, not a
misfire. Worth noting it is *also* the most expensive to add: an identity gate is a design
decision, not a patch.

**Reproduced:** (a) 3/3 spaced text, identical. (b) 3/3 spaced voice, identical.

---

### IDENTITY-08  Digits split across turns

**Why this should break it:** Nobody reads sixteen digits in one breath. "Eight zero six
nine … one zero zero two … three zero three five nine nine four six" is how the number is
actually said, and each pause is a turn boundary.

**Setup:** cold, text.

**Caller script:** "my internet is not working" · "8069" · "1002" · "30359946".

**Expected:** either accumulate across turns to `8069100230359946`, or reject `8069` as
too short (it is 4 digits; the validation copy demands 9 to 16) and re-ask. Under no
reading is `8069` a complete account number.

**Observed:** the first fragment was accepted as the whole account and the sweep
dispatched on it. The remaining twelve digits were swallowed in silence.

```
> 8069        < Give me just a moment while I check your connection. While those checks run, one
                thing that helps either way. Is everything having trouble connecting, or just one device?
> 1002        < (silent)
> 30359946    < (silent)
```

**Verdict:** FAIL

**Defect:** Fact 1 again — `8069` is non-empty, therefore valid. Compounded by the
IDENTITY-02 mechanism: with the sweep now in flight, the continuation digits arrive during
the wait and are answered with silent ticks, so the caller finishing their own account
number gets no response at all. The caller's mental model ("I'm reading you my number")
and the agent's ("I have your number, I'm checking it") diverge at digit four and never
reconverge. Severity: high, and this one needs no adversarial intent — it is what an
ordinary cooperative caller does.

**Reproduced:** 3/3 spaced, identical.

---

### IDENTITY-09  The suspended account, then a good one

**Why this should break it:** The billing hand-off is the one path that never sweeps. If
it ends the call rather than transferring, a caller who simply misread a digit has no way
back.

**Setup:** cold, text. `8069100020078787` (`account_suspended`), corrected to
`8069100230359946` (`all_clear`).

**Caller script:** "my internet is not working" · "8069100020078787" · "sorry, wrong
account, it's 8069100230359946" · "did you get that?" · "hello?".

**Expected:** the billing block itself is authored and should speak `SAY_ACCOUNT_BLOCK`
and hand off. If the caller then supplies a different number, either re-collect it and
sweep, or say the hand-off is already under way.

**Observed:** the block fires correctly and verbatim, and then the call disintegrates.

```
> 8069100020078787
< Give me just a moment while I check your connection.  I see an issue with your account status
  that's interrupting your internet service. Let me get you to someone who can help with your account.
    task_completed {"task": "HandleBillingBlock", "tool": "verdict_account_block", "success": true}

> sorry, wrong account, it's 8069100230359946
< Hmm, I'm having trouble with that. Do you want me to try again?

> did you get that?
< I can help with that. Could you please provide your Xfinity account number or the phone number
  associated with your account?

> hello?
< All information collected!
```

**Verdict:** PARTIAL  *(was FAIL; re-verdicted 2026-08-14, see the note at the end of this entry)*

**Defect:** Three failures after a correct start.
  (a) *"Hmm, I'm having trouble with that. Do you want me to try again?"* is the CES
      crash envelope — a platform-level failure surfaced as agent speech, not authored
      copy. The correction turn crashes the turn.
  (b) *"All information collected!"* is the slot-filling engine's **internal completion
      sentinel** spoken to the caller. It is a framework string, not copy: two occurrences
      in `built_advidentity/tools/slot_filling_engine/python_function/python_code.py`
      (lines 4897 and 5046), described in that same file (line 7613) as "an INTERNAL"
      marker. It is a raw debug artifact reaching a customer's ear.
  (c) The hand-off never happens. `verdict_account_block` reports success and the session
      stays open and answering, so *"Let me get you to someone who can help"* is followed
      by nobody, then by a re-ask of a question already answered, then by the sentinel.
Severity: high. `SAY_ACCOUNT_BLOCK` is the most consequential sentence in the family — it
is what a caller with a billing problem hears — and everything after it is broken.

**Reproduced:** 3/3 spaced, byte-identical across all three runs including the sentinel.


**Re-verdicted PARTIAL, 2026-08-14** (was FAIL). The engine sentinel is gone, and so is
the path that produced it: `HandleBillingBlock` now carries a `then_response` end_session,
so the call ends on its authored line instead of staying open on a flow where a suspended
account has shut every gate below. Replayed with this file's own script, 3/3 spaced:

```
> my internet is not working
< To get started, could you please tell me your Xfinity account number or the phone
  number associated with your account?
> 8069100020078787
< Give me just a moment while I check your connection.  I see an issue with your account
  status that's interrupting your internet service. Let me get you to someone who can help
  with your account.
> sorry, wrong account, it's 8069100230359946
< SessionEndedError
```

No crash envelope, no re-ask for a number already given, and no "All information
collected!" on any of the three.

PARTIAL, and deliberately, because the row's own premise is now unreachable: a caller who
mistypes into a suspended account cannot correct it on the call. That is a real cost and
it was chosen — speaking an internal sentinel aloud and re-asking for a number already
given is worse, and the authored line hands them to a person who can look it up. If the
correction matters more than the clean close, the fix is a readback on the account number,
not reopening this turn.

---

### IDENTITY-10  "Hold on, let me find my account number"

**Why this should break it:** The hold-phrase list was deliberately narrowed from the
framework defaults (`app.py:614-621`) because the broad list misread real questions as
requests for time. A narrowed list can miss the phrase it was narrowed for.

**Setup:** cold, text.

**Caller script:** "my internet is not working" · "hold on, let me find my account number"
· "ok here it is, 8069100230359946".

**Expected:** `SAY_TAKE_YOUR_TIME` (`scripts.py:44`) — *"No problem, take your time. I'll
be here when you're ready."* — and the question not re-put.

**Observed:** exactly that, verbatim, and the number collected cleanly on the next turn.

```
> hold on, let me find my account number
< No problem, take your time. I'll be here when you're ready.
> ok here it is, 8069100230359946
< Give me just a moment while I check your connection. …
```

**Verdict:** PASS

**Reproduced:** 1/1. Keep as a regression test — the narrowing at `app.py:614` is exactly
the kind of edit that would break this silently.

---

### IDENTITY-11  The account number given in the caller's first breath

**Why this should break it:** Callers volunteer the number with the complaint. The opening
turn belongs to the router, not to the account slot, so the number arrives before anything
is listening for it.

**Setup:** cold, text.

**Caller script:** "my internet is down, my account number is 8069100230361003" · "just one
device" · "did you get my account number?".

**Expected:** the number is captured on the turn it is spoken, and the account question is
not asked. `_ACCOUNT_KNOWN` (`scripts.py:375`) exists precisely because re-asking is the
defect the source already fixed once: *"the caller gave the number and said 'I've already
tried restarting my router' in one breath; … the next turn asked for the number again —
'I just gave you that.'"*

**Observed:** the number is dropped, and the agent asks for the thing it was just given.

```
> my internet is down, my account number is 8069100230361003
< To get started, could you please tell me your Xfinity account number or the phone number
  associated with your account?                                                       [reps 2, 3]
< Thanks for calling Xfinity. What's going on with your service today?                 [rep 1]
```

Recovery is inconsistent. In reps 2 and 3 the number is retrieved two turns later, but
only by the steer-back directive scraping the history —
`steer_back_soft {"directive": "The guest's last message may be off-topic. BEFORE
responding: scan the message for any information relevant to the current collection step.
If found, call the matching setter tool IMMED…"}` — after which `setter_stored
{"value": "8069100230361003"}` finally appears. In rep 1 it was never captured at all, and
the model covered for it by inventing work:

```
> just one device
< I am running a diagnostic check on your line right now. Please hold on for just a moment while
  I check the connection to your gateway.
```

No sweep had been dispatched; the engine log for that session is empty of any task.

**Verdict:** FAIL

**Defect:** The opening turn is the router's, and the router does not run the
`accountNumber` setter, so a number spoken in the first breath lands on a turn where
nothing will store it. The `_ACCOUNT_KNOWN` guard cannot help — it gates the *frustration*
acknowledgements, not the ask. Recovery depends on the steer-back directive persuading the
model to re-scan history, which is not a guarantee: 1 of 3 runs never recovered and filled
the gap with a fabricated claim that diagnostics were running. Severity: medium-high — the
re-ask is merely irritating, but "I am running a diagnostic check on your line right now"
when no check exists is the free-turn improvisation the source has fought before
(`app.py:290-297`). Note this run also shows the `hooks.py:353` fabricated outage (see
IDENTITY-06(c)).

**Reproduced:** 3/3 spaced — the number dropped on the opening turn in all three; never
recovered in 1 of 3.

---

### IDENTITY-12  Forty seconds of silence while the account is being asked

**Why this should break it:** `_account_no_input` (`app.py:584-613`) is a hand-written
per-flow policy with a deliberately silent first rung. A ladder with a silent first rung
is easy to get wrong in a way that only shows up on a real audio stream with real
inactivity ticks.

**Setup:** cold, voice. `--gap 40`, so 40s of genuine dead air sits between the complaint
and the account number on one continuous buffer.

**Caller script:** "my internet is not working" … 40s of silence … "my account number is
8 0 6 9 1 0 0 2 3 0 3 5 9 9 4 6" … 10s … "just one device".

**Expected:** the first tick silent, then the two authored reprompts in order
(`app.py:610-612`), then normal collection when the caller speaks.

**Observed:** exactly the authored ladder, in order, with the caller recovering cleanly.

```
      0.0s         < To get started, could you please tell me your Xfinity account number or the
                     phone number associated with your account?
     18.6s + 18.6s  < Sorry, I didn't catch that. Could you tell me your Xfinity account number or
                     the phone number on the account?
     30.9s + 12.3s  < I still didn't get that. What's the account number or phone number?
     44.9s + 14.0s  < 👂
     47.5s +  2.6s  < Thanks.
     47.9s +  0.4s  < Give me just a moment while I check your connection.
```

**Verdict:** PASS

**Note, not recorded as a defect:** the `👂` at 44.9s is in no source file
(grepped across `app.py`, `scripts.py`, `hooks.py` and the built engine) and appeared once
in one run. A one-off is not a finding; flagged only so a second sighting is recognised.

**Reproduced:** 1/1.

---

## Cross-cutting, for whoever picks this up

The nine failures are not nine independent bugs. They collapse into four causes:

| Cause | Scenarios |
| --- | --- |
| `set_account_number` validates nothing, so the whole retry/hand-off ladder is dead config | 01, 04, 05, 08 |
| An unrecognised number resolves to the all-clear scenario | 01, 04 |
| A caller turn during the wait, or at the walkthrough offer, is silently discarded or scored as a decline | 02, 03, 07b, 08 |
| No caller-identity or authorization concept exists at all | 07a, 07b |

Plus two that stand alone: the `ts_card` placeholder outage (06c, 11 — text channel only),
and the post-billing-block collapse including the engine sentinel (09).

**Not tested.** Real (non-demo) account resolution — this build's gate answers from a baked
fixture, so "does the production backend reject an unknown account?" is out of reach here
and IDENTITY-01/04 should be re-run against a build that reaches the live context hub
before their severity is finalised. DTMF-entered account numbers. Barge-in *during* the
agent's own speech, as opposed to on the following turn — the drivers send whole turns, so
true mid-utterance interruption was not exercised. Whether the doubled terminal line
(06b) is audible twice over real telephony or is an artifact of how the response parts are
concatenated.
