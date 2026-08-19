"""Multi-turn CUJs against a deployed app — the paths first-turn diffing cannot see.

`drive_app.py` compares one turn per scenario, which is where the eval corpus lives. But
the things most likely to be wrong are the ones that need a second turn: the reboot
offer/answer handshake, and the latch that is supposed to stop the ladder re-speaking its
verdict on every follow-up. Both were proven offline; this proves them live.

    python cuj_drive.py [app_id]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

from app.products.slot_studio.studio import state  # noqa: E402
state.apply_settings(mode="hosted", project="ces-deployment-dev", location="us")
from app.products.slot_studio.studio.chat_session import ChatSession  # noqa: E402

# argv first, then $APP_ID, then the shared dev app. The env var is here because every
# OTHER driver in this directory reads APP_ID and this one did not: `APP_ID=<mine>
# cuj_drive.py` silently drove the default app instead, which reports a perfectly
# plausible score against something you did not build. That cost an hour of chasing four
# "failures" that belonged to a different deployment.
APP_ID = (sys.argv[1] if len(sys.argv) > 1
          else os.environ.get("APP_ID") or "5fc33f37-19c2-4dee-a0c0-7e88c911f627")
APP = f"projects/ces-deployment-dev/locations/us/apps/{APP_ID}"
print(f"driving {APP_ID}")

REBOOT = ("8069100230359928",
          "convoy_status=predictive_offline&outage_status=none"
          "&network_status=clear&gateway_status=clear")

# The two ways a reboot does NOT happen. `reboot_status=` is honoured by a fake the
# build installs, because the source ships this tool with none at all — so before D4
# every drive both hit the real Convoy API and could only ever see one outcome.
# No account number seeded — the caller has to say it. Every other seed here supplies
# one, so the whole account-collection turn was untested until a recorded call walked
# into it and the agent asked three times.
NO_ACCOUNT = (None, "outage_status=none&convoy_status=clear&network_status=clear"
                    "&gateway_status=clear&context_status=clear")
SUSPENDED = ("8069100020078787", "context_status=suspended&outage_status=none")
REBOOT_BLOCKED = (REBOOT[0], REBOOT[1] + "&reboot_status=timeline_blocked")
REBOOT_ERROR = (REBOOT[0], REBOOT[1] + "&reboot_status=error")
# The all-clear account from MVP_TC_16 — 8344200010126021 resolves with NO modem MAC,
# which correctly takes the missing-hardware rung instead.
CLEAR = ("8069100230359946",
         "outage_status=none&convoy_status=clear&network_status=clear"
         "&gateway_status=clear&context_status=clear")

NETWORK = ("8069100230359944",
           "outage_status=none&convoy_status=clear&network_status=impaired"
           "&gateway_status=clear&context_status=clear")

# Same account as CLEAR, with a live outage on it. Deliberately not the MVP_TC_16
# account above: that one resolves with no modem MAC and takes missing-hardware, which
# would pass this check for the wrong reason.
OUTAGE = ("8069100230359946", "outage_status=active&convoy_status=clear"
                              "&network_status=clear&gateway_status=clear"
                              "&context_status=clear")

# A pending hardware swap, reported two ways: by the gateway check and by convoy. A
# restart cannot fix a box that is being replaced, so both must refuse one — including
# on the COLD `reboot` route, which the router owns.
GATEWAY_SWAP = ("8069100230359946",
                "outage_status=none&convoy_status=clear&network_status=clear"
                "&gateway_status=swap&context_status=clear")
CONVOY_SWAP = ("8069100230359946",
               "outage_status=none&convoy_status=predictive_swap"
               "&network_status=clear&gateway_status=clear&context_status=clear")

# The two sides of the technician split. The type is spaced and lower case, the way the
# specialist reports it — the underscored form the condition used to test never appears
# on a real value.
NETWORK_INSTALL = ("8069100230359944",
                   "outage_status=none&convoy_status=clear&network_status=impaired"
                   "&gateway_status=clear&context_status=clear"
                   "&technician_type=install and repair tech")

# The other half of the same split. Pinned because the `technician_type=` mock key is a
# build-time patch to the specialist's fake: without this, a patch that quietly stopped
# applying would leave the install case failing and the network case passing, which
# reads as "one branch is broken" rather than "the fixture died".
NETWORK_TECH_ONLY = ("8069100230359944",
                     "outage_status=none&convoy_status=clear&network_status=impaired"
                     "&gateway_status=clear&context_status=clear"
                     "&technician_type=network tech")


class _Answers:
  """Sentinel: this turn must produce a real answer, whatever the answer is.

  The only guard against the recorded MUTE defect, where a rung that speaks as its tool
  fires left the caller in silence on every turn AFTER it: "How long until it's back?"
  asked straight after accepting the reboot returned empty agent text 4 times in 5, and
  the fifth returned the fragment "I need." Every rung that also spoke after its tool
  returned answered the same question fine.

  The defect is invisible to `ladder_check`, which drives a single turn, and to the
  substring expectations below, which only look at the turn the verdict lands on. It was
  originally isolated by hand, by asking the same follow-up after each rung in turn;
  this makes that a check rather than an afternoon.
  """


ANSWERS = _Answers()

# The engine's declared waiting surface for the asynchronous sweep: `awaits.say` and the
# `while_waiting` ladder, verbatim. Spelled out rather than imported so that changing the
# copy without changing this fails the async pack loudly, instead of silently absorbing
# some other line -- a verdict, or an error -- as though it meant "still working".
# Seconds between no-input ticks while the sweep is out. Roughly what a silent voice
# caller produces, and enough that two ticks clear a ~19s backend sweep.
_WAIT_TICK_S = float(os.environ.get("WAIT_TICK_S") or 9)

# The sweep's own failure/timeout line. A turn that opens with it is the BACKEND saying
# no, and the agent relaying that correctly -- not a journey failing.
_SWEEP_FAILED = "I just ran a few checks but wasn't able to get all the info I need."

_WAITING = frozenset({
    "Still running those checks. I'll have the results in just a moment.",
    "Almost there, just waiting on the last result.",
})

# The bridge line cannot be pinned whole any more: it now reads the last four digits of
# the caller's account back to them, so the sentence differs per seed account. Its tail
# is the part that says "wait", and it is what the exact-match set above cannot carry.
_WAITING_TAIL = "Give me just a moment while I check your connection."

# (name, seed vars, [(utterance, substring(s) the reply MUST contain)])
# `expect` may be a string, a list of strings (all must appear), None meaning "must NOT
# re-speak the previous line", or ANSWERS meaning "must say something of its own".
CUJS = [
    ("reboot offered then ACCEPTED", REBOOT, [
        ("my internet is not working", "would you like me to reboot"),
        ("yes please", "sending a signal to reboot"),
    ]),
    ("reboot offered then DECLINED", REBOOT, [
        ("my internet is not working", "would you like me to reboot"),
        ("no thanks", "connect you to a gateway specialist"),
    ]),
    # D4. The gateway refuses the reboot because it was restarted minutes ago. The
    # caller must NOT be told a signal was sent — that was the defect, and it is the
    # negative half that carries the meaning here: before D4 this said "I'm sending a
    # signal to reboot your gateway now" on exactly this path.
    ("reboot BLOCKED: say so, do not claim it was sent", REBOOT_BLOCKED, [
        ("my internet is not working", "would you like me to reboot"),
        ("yes please", ["restarted not long ago", "!sending a signal"]),
    ]),
    # The same requirement on the other failing outcome. Two rungs could satisfy the
    # blocked case by accident; an errored backend is the one where a lie is likeliest,
    # because nothing about the response looks like a refusal.
    ("reboot ERRORED: still no claim that it was sent", REBOOT_ERROR, [
        ("my internet is not working", "would you like me to reboot"),
        ("yes please", "!sending a signal"),
    ]),
    # The all-clear is an OFFER now, so this checks the same property against the new
    # wording: the verdict lands once and the walkthrough offer is not re-spoken.
    ("verdict is delivered ONCE, not re-spoken on follow-up", CLEAR, [
        ("my internet is not working", "most likely spot"),
        ("ok thanks", None),          # must NOT repeat the offer
    ]),
    # The turn AFTER a two-part rung, which is where the mute defect lives. Both of
    # these rungs speak once as their tool is dispatched and once when it returns; the
    # failure being guarded is the whole call going silent from here on, not anything
    # about the verdict itself, so any real answer passes.
    ("reboot ACCEPTED, then the follow-up must be answered", REBOOT, [
        ("my internet is not working", "would you like me to reboot"),
        ("yes please", "sending a signal to reboot"),
        ("how long until it's back?", ANSWERS),
    ]),
    ("network verdict, then the follow-up must be answered", NETWORK, [
        ("my internet is not working", "technician"),
        ("when will they come out?", ANSWERS),
    ]),

    # A caller who names one app AND has a live outage. The sweep has already measured
    # the outage by the time the gate is answered, so advising about the app means
    # withholding a fault Comcast knows about. This is what the app-advice rung did
    # until it was gated and moved below the plant faults.
    ("only that app, but the area is out -> the outage wins", OUTAGE, [
        ("my Netflix won't load", "is it only netflix"),
        ("just Netflix, everything else is fine", "outage"),
    ]),

    ("clarification: only that app -> advise, no diagnostics", CLEAR, [
        ("my Netflix won't load", "is it only netflix"),
        ("just Netflix, everything else is fine", "netflix"),
    ]),
    # The bridge line and the verdict must arrive in the SAME turn — the caller should
    # not be told "let me check your service now" and then left waiting a turn for it.
    ("clarification: everything down -> bridge AND verdict, one turn", CLEAR, [
        ("my Netflix won't load", "is it only netflix"),
        ("nothing works, other sites are down too",
         ["not just the app", "most likely spot"]),
    ]),
    # Asking for a person is the commonest hand-off and was the only cold one: the
    # escalate rail spoke a line, ended the session, and passed nothing. The payload now
    # rides the rail's pre-terminal chain, so `verdict_human_request` must be called on
    # the SAME turn as the spoken line — that co-firing is the whole fix, and a text
    # assertion on the line alone would pass without it.
    # The service-charge branch. It could not fire before: the sweep dropped the
    # specialist's type, the hook substituted a constant, and the condition tested a
    # spelling the specialist never produces. All three had to change together.
    ("install and repair tech gets the service-charge wording", NETWORK_INSTALL, [
        ("my internet has been unreliable for a week", "there may be a service charge"),
    ]),
    # A network tech is Comcast's own plant, so there is no charge to warn about. The
    # assertion is on the LEAD line, which is what distinguishes the two verdicts; the
    # negative half — that no charge is mentioned — is what the split is for.
    ("network tech is NOT warned about a service charge", NETWORK_TECH_ONLY, [
        ("my internet has been unreliable for a week",
         ["problem with the network signal going to your home",
          "!service charge"],
         ["verdict_network_tech"]),
    ]),

    # The in-home Wi-Fi walkthrough, end to end. All three exits, because the interesting
    # failures are at the edges: a tip loop that never stops, a "fixed" that transfers
    # anyway, and a decline that closes on the caller.
    ("wifi walkthrough: accept, three tips, then hand off", CLEAR, [
        ("my internet is really slow", "most likely spot"),
        ("yes please", "just the one device"),
        ("just my laptop", "forget the home network"),
        ("no that didn't help", "moving closer"),
        ("still no good", "off and back on"),
        ("nope, same", "someone who can take a closer look",
         ["verdict_wifi_exhausted"]),
    ]),
    ("wifi walkthrough: fixed closes warmly, no transfer", CLEAR, [
        ("my internet is really slow", "most likely spot"),
        ("yes please", "just the one device"),
        ("just my laptop", "forget the home network"),
        ("oh that worked, thanks", "good to hear", ["verdict_wifi_fixed"]),
    ]),
    ("wifi walkthrough: declining is not a dead end", CLEAR, [
        ("my internet is really slow", "most likely spot"),
        ("no thanks", "someone who can help you with this",
         ["verdict_wifi_declined"]),
    ]),

    # R5 — the service-charge question, the source's Priority 13, which this conversion
    # had lost entirely. Asserts the interpolation resolved too: `{technician_fee}` must
    # come through as the app variable's value, not as a literal placeholder.
    ("fee asked AFTER the verdict is still answered", NETWORK, [
        ("my internet has been dropping all week", "technician"),
        ("how much is that going to cost me?",
         ["that's a $100 charge", "there's no charge", "!{technician_fee}"],
         ["verdict_service_fee"]),
    ]),
    # Mid-diagnosis. The fee answer latches its own flag, so it must NOT eat the turn —
    # the caller hears the price AND the diagnosis in one breath.
    # A reboot is not a technician visit, so the honest answer is "no charge" — and the
    # diagnosis still lands in the same breath. Before this, every cost question got the
    # visit fee schedule whether or not a visit was on the table.
    ("fee asked mid-call: no charge, AND still diagnose", REBOOT, [
        ("my internet is down, will I be charged for this?",
         ["nothing we're doing here costs anything", "reboot",
          "!$100"]),
    ]),
    # Asked AGAIN. Recorded on a real call: the caller asked four times and got the
    # schedule once and silence three times, because the latch had closed the question
    # for the rest of the call. It is cleared per turn now, so a fresh ask is answered.
    ("the cost question can be asked more than once", NETWORK, [
        ("my internet keeps dropping all week", "technician"),
        ("hang on, how much is this going to cost me?", "$100"),
        ("sorry, so will I actually be charged for this or not?",
         ["no charge for this call", "!A visit can carry a fee"]),
    ]),
    # Frustration is acknowledged, once, and the substantive answer still lands.
    ("frustration is acknowledged, not ignored", NETWORK, [
        ("this is honestly ridiculous, my internet has been useless all week and it's "
         "driving me crazy",
         ["how frustrating", "technician"]),
    ]),
    # Two apps named is not one app. The caller has already answered the clarifying
    # question in the asking, so it must not be put to them.
    ("two apps named: do not ask if it is only one of them", CLEAR, [
        ("my Netflix keeps buffering and my son's Xbox keeps dropping out",
         ["!is it only"]),
    ]),
    # The cue set has to stay narrow. A bare `\bcharge\b` fires on this, and the caller
    # would get a fee schedule read at them for mentioning a flat battery.
    ("a phone that won't charge is not a fee question", CLEAR, [
        ("my internet is slow and my phone won't charge", "!$100 charge"),
    ]),

    # R6 — the caller asks for a reboot outright, rather than being offered one. The
    # conversion only ever reached a reboot through the offer handshake, so this caller
    # was diagnosed at instead of answered.
    # The four refusals on the COLD path. These reach the `reboot` child flow, which the
    # router owns — a different code path from the ladder's `RebootOnRequest`, and one
    # that had NO blockers at all before this branch: driven, it answered "Alright, I'm
    # sending a signal to reboot your gateway now" during an active outage and on a
    # suspended account. Every other reboot CUJ opens with "my internet is down...",
    # which routes to `repair` and therefore cannot see this path.
    ("cold reboot during an outage is refused", OUTAGE, [
        ("reboot my modem",
         ["outage in your area", "!sending a signal to reboot"]),
    ]),
    ("cold reboot on a suspended account is refused", SUSPENDED, [
        ("reboot my modem",
         ["account status", "!sending a signal to reboot"]),
    ]),
    ("cold reboot with a gateway swap pending is refused", GATEWAY_SWAP, [
        ("reboot my modem",
         ["failing on and off", "!sending a signal to reboot"]),
    ]),
    ("cold reboot with a convoy swap pending is refused", CONVOY_SWAP, [
        ("reboot my modem",
         ["failing on and off", "!sending a signal to reboot"]),
    ]),

    # COLD: the router hears a restart request and sends it to the `reboot` flow, so the
    # executor is that flow's. Mid-journey the same words stay in `repair` — pinned
    # separately below, so widening this does not lose the rung's coverage.
    ("explicit reboot request is honoured", CLEAR, [
        ("can you reboot my modem please", "sending a signal to reboot",
         [("verdict_steering_reboot", "verdict_reboot_on_request")]),
    ]),
    # MID-JOURNEY: already in `repair` and the gate is filled, so the engine hides
    # `set_active_flow` and the caller cannot be re-routed. This is the path that must
    # reach OUR rung, with its blockers and its success_check.
    ("a reboot asked for mid-journey stays in repair", CLEAR, [
        ("my internet is really slow", ANSWERS),
        ("just reboot it then", "sending a signal to reboot",
         ["verdict_reboot_on_request"]),
    ]),
    # The guard that matters. A restart cannot clear an area outage, so honouring the
    # request would spend the caller's time on something Comcast already knows will not
    # work — and talk over the verdict explaining the real fault.
    ("reboot request during an outage is refused", OUTAGE, [
        ("my internet is down, just reboot my modem",
         ["outage", "!sending a signal"]),
    ]),
    # The source's own blocker, which this rung was missing until the prose was read
    # back: its Priority-0 reboot bypass does not apply on a suspended account. Ladder
    # position could not cover it, because this rung is not gated on the ladder being
    # open — a suspended caller who asked on any later turn would have got a reboot.
    ("reboot request on a suspended account is refused", SUSPENDED, [
        ("my internet is down, can you reboot my modem",
         "!sending a signal"),
    ]),
    # Past tense is a REPORT, not a request. Rebooting the gateway because the caller
    # said they had already restarted their own router would be acting on the opposite
    # of what they said.
    ("\"I restarted my router\" is not a request to reboot", CLEAR, [
        ("my internet is slow, I already restarted my router",
         "!sending a signal"),
    ]),

    # Phase 4 — the outage inquiry. A caller who rang to ask one question, not to be
    # diagnosed. The negative assertions carry the journey: the whole point is that the
    # full sweep does NOT run until they say yes.
    ("inquiry with an outage: answer it, do not diagnose", OUTAGE, [
        ("hi, is there an outage in my area?", ["outage", "!most likely spot"]),
    ]),
    ("inquiry with no outage: good news, then an offer", CLEAR, [
        ("is there an outage in my area?",
         ["no outage reported", "would you like me to run a full check"]),
    ]),
    ("inquiry declined: close warmly, no transfer", CLEAR, [
        ("any known outages in my area?", "no outage reported"),
        ("no thanks, that's all I needed",
         ["we're here any time", "!connect you"], ["verdict_inquiry_declined"]),
    ]),
    # Consent taken. The sweep runs on the NEXT turn and the caller lands in the
    # ordinary ladder — the all-clear here, since this account is healthy.
    ("inquiry accepted: the full check then runs", CLEAR, [
        ("is there an outage in my area?", "no outage reported"),
        ("yes please, go ahead", "most likely spot"),
    ]),

    # The account-collection turn. `before_agent` has already run by the time the setter
    # fires, so the sweep cannot happen until the next turn — which used to leave the
    # model a free turn. Driven, it announced "I am running diagnostics on your account
    # now" and called two raw OpenAPI operations to make that true.
    # The verdict now lands on the SAME turn as the holding line: the sweep is a task the
    # engine dispatches mid-cascade, so it speaks its filler, runs, hands back and the
    # ladder reaches the verdict in one breath. It used to take a turn longer, because the
    # sweep happened in `before_agent` and the bridge rung owned that turn alone.
    ("account given aloud: bridge, verdict, and never a raw backend call", NO_ACCOUNT, [
        ("my internet is really flaky", "account number"),
        ("my account number is 8069100230359946",
         ["give me just a moment", "most likely spot", "!running diagnostics"],
         ["run_comcast_diagnostics_resolved"]),
        ("ok", ANSWERS),
    ]),

    # An acknowledgement must not eat the turn carrying the account number. Driven
    # twice: the caller gave the number and said "I've already tried restarting my
    # router" in one breath, the ack fired, the setter did not, and the next turn asked
    # for the number again. The caller's reply was "I just gave you that."
    ("an acknowledgement does not cost the account number", NO_ACCOUNT, [
        ("my internet is really flaky", "account number"),
        ("my account number is 8069100230359946, and I've already tried restarting "
         "my router",
         ["give me just a moment", "!account number"],
         # The sweep TOOL, not the retired bridge rung: the holding line is now the
         # task's own `filler_say`, so the tool firing is what proves the account
         # survived the acknowledgement and the sweep actually ran on it.
         ["run_comcast_diagnostics_resolved"]),
    ]),

    # The sweep is unconditional and cannot know the route: the hook runs at slot `_00`,
    # BEFORE the router resolves, so on turn 0 — the only turn a defer call gets —
    # `active_flow` is still empty. A caller headed for billing is therefore swept, and
    # ~30 diagnostics slots are seeded into one global slot machine with no flow
    # qualification. These two pin the thing that would actually hurt: that none of it
    # reaches the caller. Seeded with an ALARMING substrate on purpose — an active
    # outage — so a leak would be loud rather than subtle.
    # The spoken half is asserted STRUCTURALLY, not by matching copy. A defer used to end
    # with "let me get you to the right place for that"; it now reaches a placeholder A2A
    # desk that names the detected intent back. Pinning either sentence pins whatever
    # stands in for a real desk this week, and the sentence is not what these CUJs are
    # for — the negatives are. So: assert the hand-off ACTUALLY happened (the A2A call
    # fired), and keep the negatives, which are the thing that would hurt.
    ("a deferred caller hears the handoff and no diagnostics", OUTAGE, [
        ("I want to dispute a charge on my bill",
         ["!outage", "!gateway", "!technician", "!reboot"],
         ["intent_placeholder"]),
    ]),
    ("a deferred appointment caller is not diagnosed at either", OUTAGE, [
        ("can I reschedule my technician visit",
         ["!outage", "!healthy", "!connection"],
         ["intent_placeholder"]),
    ]),

    ("asking for a person hands off WITH context", NETWORK, [
        ("my internet has been dropping all week", "technician"),
        ("can you put me through to a person", "someone who can help",
         ["verdict_human_request"]),
    ]),
]


def run(name, seed, steps):
    account, mock = seed
    seed_vars = {"mock_config_string": mock}
    if account:
        seed_vars.update({"accountNumber": account, "account_id": account})
    # ASYNC_SWEEP=1 runs the whole pack down the SweepAsync path instead of the
    # before_agent one. The two must agree turn for turn: same verdicts, same tools. That
    # equivalence is the only thing that licenses making async the default.
    if os.environ.get("ASYNC_SWEEP"):
        seed_vars["async_sweep_armed"] = "1"
    s = ChatSession(app_name=APP, initial_variable_state=seed_vars)
    orig = s._sessions.run
    s._sessions.run = lambda **kw: orig(use_tool_fakes=True, **kw)
    print(f"\n=== {name}")
    ok = True
    prior = ""
    infra = False
    for step in steps:
        # A step is (utterance, expect) or (utterance, expect, tools_that_must_fire).
        # The third element exists because some fixes are invisible in the transcript:
        # a hand-off that carries no payload speaks exactly the same line as one that
        # does, so asserting the words alone would pass on the broken version.
        utt, expect = step[0], step[1]
        must_call = step[2] if len(step) > 2 else ()
        if s.is_ended:
            print(f"  > {utt}\n  < (session already ended)")
            ok = ok and expect is None
            break
        turn = s.send(utt)
        text = (turn.agent_text or "").strip()
        called = {c.get("action") for c in (turn.tool_calls or [])}
        # The BACKEND failed, not the agent. Every expectation in this pack assumes the
        # sweep answered; when it does not, the agent's correct behaviour is exactly this
        # line and a hand-off, and scoring that as a failed journey measures RDK's health
        # rather than the app's. Proven environmental rather than ours: with the engine's
        # staleness guard compiled OUT, an A/B on the same account failed identically.
        #
        # Reported separately rather than swallowed -- a run with samples missing is not
        # a green run, and the count is printed so a degraded window is visible instead
        # of quietly shrinking the denominator.
        # On the ASYNC path the sweep's outcome is not delivered on the turn that
        # dispatches it. CES answers the call with a `{"result": "pending"}` placeholder
        # and hands the real payload back one turn boundary later, as a synthetic user
        # turn (`<context>function [...] completed with response ...</context>`); the
        # engine speaks `awaits.say` meanwhile. So the verdict this step is asking for
        # legitimately lands on the NEXT turn, and asserting it on this one measures the
        # synchronous turn shape rather than the caller's experience.
        #
        # This is NOT the assertion being relaxed. The holding line still has to be a
        # real holding line: absorbing it here means the step's own expectation must
        # still be met on the following turn, and a wait that answered with silence, an
        # error envelope or improvisation fails exactly as before -- it simply fails on
        # the turn it actually happened.
        # The sweep is ASYNCHRONOUS, so its verdict does not land on the turn that
        # dispatches it: CES answers the call with a `{"result": "pending"}` placeholder
        # and hands the real payload back a turn boundary later. While it is out, the
        # engine speaks its declared waiting surface -- `awaits.say` first, then the
        # `while_waiting` ladder. Those turns are the agent working, not the agent
        # answering, so the step's expectation is checked against the turn that actually
        # answers.
        #
        # This is the ONLY thing relaxed. A waiting turn still has to BE a waiting turn:
        # anything else -- silence, improvisation, the engine's "All information
        # collected!" sentinel, a crash envelope -- is not in _WAITING and fails the step
        # exactly as before, on the turn it happened. And the wait is bounded here at two
        # advances, well inside `awaits.max_turns`, so a sweep that never answers fails
        # rather than being waited out.
        #
        # Advanced with an EVENT, not by re-sending the caller's words: an event carries
        # no user text, so it cannot re-fill a slot the utterance already filled --
        # re-sending "yes please" would confirm twice -- and a no-input tick is what a
        # voice call actually produces while the caller waits.
        for _ in range(2):
            if text not in _WAITING and not text.endswith(_WAITING_TAIL):
                break
            # Paced, and that pacing is load-bearing. `awaits.max_turns` is a budget in
            # TURNS, but the sweep costs TIME -- ~19s against real backends. Fired
            # back-to-back, four turns elapse in three seconds, the payload has not
            # landed, and the wait times out into SAY_NO_TELEMETRY: 13 of the 39 CUJs
            # failed that way and every one of them read as a broken agent rather than
            # as a harness sprinting through a wait no caller could. A voice call paces
            # itself -- a no-input tick is seconds, not milliseconds -- so this does too.
            time.sleep(_WAIT_TICK_S)
            turn = s.send_event("no input")
            text = (turn.agent_text or "").strip()
            called |= {c.get("action") for c in (turn.tool_calls or [])}
        # Checked AFTER the wait is absorbed, not before it: on the dispatch turn the
        # agent is still saying the holding line, so a check ahead of the loop never sees
        # the failure that arrives on the turn the payload lands.
        if text.startswith(_SWEEP_FAILED):
            infra = True
            break
        # Collapse the diagnostic-mirror duplicate (see README: parser artifact).
        half = len(text) // 2
        if half and text[:half].strip() == text[half:].strip():
            text = text[:half].strip()
        verdict = "ok  "
        # A tuple entry means "any ONE of these". Under the steering router the same
        # caller intent can legitimately land on different executors depending on where
        # it arrived: a COLD "reboot my modem" is routed to the `reboot` flow, while the
        # same words MID-JOURNEY stay in `repair` and hit its own rung. Both are correct,
        # and pinning one would assert the routing rather than the behaviour.
        missing = [t for t in must_call
                   if (not any(x in called for x in t) if isinstance(t, tuple)
                       else t not in called)]
        if missing:
            verdict, ok = "FAIL", False
            print(f"  FAIL > {utt[:46]:48} < did not call {missing} (called {sorted(called)})")
            prior = text
            continue
        if expect is None:
            if prior and prior[:60].lower() in text.lower():
                verdict, ok = "FAIL", False       # re-spoke the verdict
        elif expect is ANSWERS:
            # Empty is the mute defect. A bare re-speak is the latch failing. The
            # fragment "I need." is what the fifth run returned, so length matters too:
            # anything this short is a truncated non-answer rather than a reply.
            if len(text) < 12 or (prior and prior[:60].lower() in text.lower()):
                verdict, ok = "FAIL", False
                print(f"  FAIL > {utt[:46]:48} < MUTE/NON-ANSWER: {text!r}")
                prior = text
                continue
        else:
            # A leading "!" means the phrase must be ABSENT. Some splits are only
            # meaningful as a negative: the network-tech verdict is distinguished from
            # the install-and-repair one by NOT warning about a service charge, and a
            # positive-only assertion would pass on a reply that warned about both.
            wanted = [expect] if isinstance(expect, str) else expect
            for w in wanted:
                present = w.lstrip("!").lower() in text.lower()
                if present == w.startswith("!"):
                    verdict, ok = "FAIL", False
        print(f"  {verdict} > {utt[:46]:48} < {text[:96]}")
        prior = text
    return (ok, infra)


if __name__ == "__main__":
    results = [(n,) + run(n, seed, steps) for n, seed, steps in CUJS]
    print("\n" + "=" * 60)
    for n, good, infra in results:
        print(f"  {'SWEEP' if infra else 'PASS' if good else 'FAIL'}  {n}")
    sampled = [(n, g) for n, g, i in results if not i]
    skipped = len(results) - len(sampled)
    print(f"\n{sum(g for _, g in sampled)}/{len(sampled)} CUJs correct"
          + (f"  ({skipped} not sampled: the sweep failed — backend, not the agent)"
             if skipped else ""))
