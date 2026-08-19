#!/usr/bin/env python3
"""Every journey this agent has, as a walkable call.

Data only -- `journey_check.py` runs these and `harness.py` supplies the step language.
Each scenario opens at the greeting and walks to a terminal outcome, and every assertion
is against the approved copy in `scripts.py` rather than a substring of it.

A note on which steps are `say()` and which are `fill()`. Cue-bearing slots MUST be
reached with `say()`, because then the cue map is graded too. Only the slots a cue map
cannot reach offline get a `fill()`: the classifier-backed intent slots (the model is the
backstop and there is no model here) and the plain `user_slot`s. Reaching for `fill()`
where a cue would do quietly stops testing the thing most likely to break.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths()

import clarify  # noqa: E402
import scripts  # noqa: E402
from harness import (  # noqa: E402
    Scenario, delivered, fill, gate, legs, quiet, remote, say, say_settling,
    specialists, task_fails, task_returns, walk,
)
from journeys import device_help  # noqa: E402

#: The caller's opening line on an ordinary repair call. Broad by cue, so the
#: clarification gate stays shut and the ladder is what is being graded.
DOWN = "hi my internet is down"


def render(template: str, **values) -> str:
  """Fill an approved line the way the engine does.

  The copy uses `{slot|fallback}` as well as plain `{slot}`, so `str.format` cannot read
  it. Asserting on the rendered line rather than on a fragment of it is what keeps a
  placeholder that silently stops resolving -- which reaches the caller as raw braces --
  a test failure.
  """
  out = []
  rest = template
  while "{" in rest:
    before, _, tail = rest.partition("{")
    field, _, rest = tail.partition("}")
    name, _, fallback = field.partition("|")
    out.append(before)
    out.append(str(values.get(name, fallback or f"{{{field}}}")))
  return "".join(out) + rest


# ==============================================================================
# A. The diagnostic ladder: one caller turn, one verdict
# ==============================================================================

SECTION_A = [
    Scenario(
        "A1", "all clear, and the walkthrough is offered", [
            say(DOWN),
            # The gate leads with an acknowledgement as the sweep is dispatched, and the
            # rest of the sentence lands when it returns. Both halves are pinned, in
            # order, because a split that reorders or mutes a half still fires the rung.
            walk(says=[scripts.SAY_BRIDGE_ACK], fc="resolve_account_context"),
            gate(says=[scripts.SAY_BRIDGE_TO_SWEEP_REST, scripts.ASK_WIFI_SCOPE_EARLY]),
            legs(),
            specialists(),
            walk(rungs=["Settle", "HandleAllClear"], says=[scripts.SAY_ALL_CLEAR],
                 filled={"wifi_offered": "true", "verdict_delivered": None}),
        ]),

    Scenario(
        "A2", "an outage in the area is reported, and nobody is transferred", [
            say(DOWN),
            walk(), gate(), legs(outage="active"), specialists(),
            walk(rungs=["Settle", "HandleAreaOutage"],
                 says=[render(scripts.SAY_AREA_OUTAGE, outage_message="OUTAGE_MSG",
                             customer_message="CUSTOMER_MSG")]),
        ]),

    Scenario(
        "A3", "a suspended account goes to billing, and no checks are run", [
            say(DOWN),
            walk(),
            gate(account="suspended"),
            # No Settle: the gate itself returned `diagnostics_complete`, which is what
            # stops a restricted account being diagnosed at all.
            walk(rungs=["HandleBillingBlock"], says=[scripts.SAY_ACCOUNT_BLOCK],
                 escalated=True),
        ]),

    Scenario(
        "A3b", "a disconnected account gets the same answer", [
            say(DOWN), walk(), gate(account="disconnected"),
            walk(rungs=["HandleBillingBlock"], says=[scripts.SAY_ACCOUNT_BLOCK]),
        ]),

    Scenario(
        "A3c", "an account pending activation gets the same answer", [
            say(DOWN), walk(), gate(account="pending activation"),
            walk(rungs=["HandleBillingBlock"], says=[scripts.SAY_ACCOUNT_BLOCK]),
        ]),

    Scenario(
        "A4", "an account we cannot see is a different desk from one we will not check", [
            say(DOWN), walk(),
            gate(account="not_found", mac="NOT_FOUND"),
            walk(rungs=["HandleAccountNotFound"], says=[scripts.SAY_ACCOUNT_NOT_FOUND],
                 escalated=True),
        ]),

    Scenario(
        "A5", "no gateway on the account, and the split rejoins", [
            say(DOWN), walk(),
            gate(mac="NOT_FOUND"),
            legs(),
            walk(rungs=["Settle", "HandleMissingHardware"],
                 joined=scripts.SAY_MISSING_HARDWARE),
        ]),

    Scenario(
        "A6", "convoy predicts an impairment, so an appointment is arranged", [
            say(DOWN), walk(), gate(), legs(action="technician"), specialists(),
            walk(rungs=["Settle", "HandleConvoyImpairment"]),
        ]),

    Scenario(
        "A7", "convoy predicts the gateway will fail, so it is replaced", [
            say(DOWN), walk(), gate(), legs(action="predictive_swap"), specialists(),
            walk(rungs=["Settle", "HandleConvoySwap"],
                 says=[scripts.SAY_HARDWARE_SWAP_CONVOY]),
        ]),

    Scenario(
        "A8", "the gateway is measured faulty, so it is replaced", [
            say(DOWN), walk(), gate(), legs(), specialists(gw="swap"),
            walk(rungs=["Settle", "HandleHardwareSwap"],
                 says=[scripts.SAY_HARDWARE_SWAP_GATEWAY]),
        ]),

    Scenario(
        "A9", "a network technician is sent, and the caller need not be home", [
            say(DOWN), walk(), gate(), legs(),
            specialists(net="impaired", tech="network tech"),
            walk(rungs=["Settle", "HandleNetworkTech"], joined=scripts.SAY_NETWORK_TECH),
        ]),

    Scenario(
        "A10", "any other technician type carries the charge warning instead", [
            say(DOWN), walk(), gate(), legs(),
            specialists(net="impaired", tech="install and repair tech"),
            walk(rungs=["Settle", "HandleNetworkImpairment"],
                 joined=scripts.SAY_NETWORK_GENERIC),
        ]),

    Scenario(
        "A11", "a gateway we cannot triage goes to a person", [
            say(DOWN), walk(), gate(), legs(), specialists(gw="unsupported_device"),
            walk(rungs=["Settle", "HandleUnsupportedDevice"],
                 says=[scripts.SAY_UNSUPPORTED_DEVICE]),
        ]),

    Scenario(
        "A12", "a gateway that will not answer goes to a person", [
            say(DOWN), walk(), gate(), legs(), specialists(gw="no_telemetry"),
            walk(rungs=["Settle", "HandleNoTelemetry"], says=[scripts.SAY_NO_TELEMETRY]),
        ]),

    Scenario(
        "A12b", "a gateway that was never checked is not reported as healthy", [
            # The contested state: `ALL_CLEAR` accepts a skipped gateway and so does
            # `NO_TELEMETRY`, so only declaration order keeps this caller from being told
            # everything checks out when the gateway was never reached. `order_check`
            # pins that order; this walks the caller who would hear the difference.
            say(DOWN), walk(), gate(), legs(), specialists(gw="skipped"),
            walk(rungs=["Settle", "HandleNoTelemetry"],
                 says=[scripts.SAY_NO_TELEMETRY], never=[scripts.SAY_ALL_CLEAR]),
        ]),

    Scenario(
        "A13", "a check that errored is reported as a failed check, not as health", [
            say(DOWN), walk(), gate(), legs(), specialists(net="error"),
            walk(rungs=["Settle", "HandleDiagnosticError"],
                 says=[scripts.SAY_NO_TELEMETRY]),
        ]),

    Scenario(
        "A13b", "an errored outage check is a failed check, not a quiet all-clear", [
            say(DOWN), walk(), gate(), legs(outage="error"), specialists(),
            walk(rungs=["Settle", "HandleDiagnosticError"],
                 says=[scripts.SAY_NO_TELEMETRY], never=[scripts.SAY_ALL_CLEAR]),
        ]),

    Scenario(
        "A13c", "an errored Wi-Fi check is too", [
            say(DOWN), walk(), gate(), legs(), specialists(wifi="error"),
            walk(rungs=["Settle", "HandleDiagnosticError"],
                 says=[scripts.SAY_NO_TELEMETRY], never=[scripts.SAY_ALL_CLEAR]),
        ]),

    Scenario(
        "A13d", "an errored gateway reads as no telemetry, not as a failed check", [
            # `gateway_status == error` satisfies BOTH rungs; declaration order is all
            # that picks the telemetry wording, which is what `order_check` O-12 pins.
            say(DOWN), walk(), gate(), legs(), specialists(gw="error"),
            walk(rungs=["Settle", "HandleNoTelemetry"], says=[scripts.SAY_NO_TELEMETRY]),
        ]),

    Scenario(
        "A6b", "convoy reporting the gateway offline offers a restart, not a visit", [
            # `device_offline` is the one routing action Settle maps to
            # `predictive_offline`, and an offline gateway is what a restart is FOR --
            # so this lands on the offer rather than on an impairment verdict.
            say(DOWN), walk(), gate(), legs(action="device_offline"), specialists(),
            walk(rungs=["Settle", "OfferReboot"], says=[scripts.SAY_REBOOT_ASK]),
        ]),

    Scenario(
        "A14", "the sweep itself fails, and the caller is not left waiting", [
            say(DOWN), walk(),
            task_fails("ContextGate", text=scripts.SAY_SWEEP_UNAVAILABLE,
                       fc="verdict_no_telemetry"),
        ],
        endings=("ContextGate.exhaust",)),

    Scenario(
        "A14b", "the remote specialist job fails, and the caller is not left waiting", [
            say(DOWN), walk(), gate(), legs(),
            task_returns("Specialists", resolve_specialists_remote__job="JOB1"),
            remote(status="failed", resolve_specialists_remote__job="JOB1",
                   text=scripts.SAY_SWEEP_UNAVAILABLE, fc="verdict_no_telemetry"),
        ],
        endings=("Specialists.exhaust",)),

    Scenario(
        "A14c", "a remote job with no handle is a failure, not a clean sweep", [
            say(DOWN), walk(), gate(), legs(),
            # The trap this whole harness had to learn: feeding the specialists' OUTPUTS
            # to the start tool looks like a completed sweep and is stamped
            # `remote_bad_handle`, so a healthy line reports a failed check.
            task_returns("Specialists", network_status="healthy",
                         gateway_status="healthy", wifi_status="healthy",
                         text=scripts.SAY_SWEEP_UNAVAILABLE, fc="verdict_no_telemetry"),
        ],
        endings=("Specialists.bad_handle",)),
]


#: The steps that get an ordinary call to a clean verdict, reused by every section that
#: needs the ladder already settled before it starts.
def _all_clear() -> list:
  return [say(DOWN), walk(), gate(), legs(), specialists(),
          walk(rungs=["Settle", "HandleAllClear"], says=[scripts.SAY_ALL_CLEAR])]


def _tech_visit() -> list:
  """A verdict with a technician visit on the table, which is what puts a fee in play."""
  return [say(DOWN), walk(), gate(), legs(),
          specialists(net="impaired", tech="network tech"),
          walk(rungs=["Settle", "HandleNetworkTech"])]


# ==============================================================================
# B. The Wi-Fi walkthrough
# ==============================================================================

SECTION_B = [
    # B1 and B1b are the two wordings of one question, and B1's `never=` is the whole
    # point of the pair. The scope question is hoisted into the sweep on nearly every
    # call, so `_all_clear()` has already spoken it once, inside `ASK_WIFI_SCOPE_EARLY`,
    # on the gate turn. Measured live and cold, the caller then heard `ASK_WIFI_SCOPE`
    # word for word two turns later.
    Scenario(
        "B1", "already asked during the checks, so it is not asked again in the same "
              "words", [
            *_all_clear(),
            say("yes please"),
            walk(rungs=["AskWifiScopeAgain"],
                 joined=" ".join((scripts.FILLER_ASK_SCOPE,
                                  scripts.ASK_WIFI_SCOPE_AGAIN)),
                 never=[scripts.ASK_WIFI_SCOPE]),
        ]),

    Scenario(
        "B1b", "and a caller who was never asked hears it for the first time", [
            # The one route to the walkthrough with the question still unasked. The
            # clarification gate held the early ask shut for the whole wait, and by the
            # time the reply landed the specialists had reported, so the announce's own
            # window ("still running") had closed. Nobody may be told "back to the one
            # thing I asked earlier" about a thing that was never asked.
            say("netflix is not working"),
            walk(), gate(), legs(), specialists(), walk(),
            fill("set_clarify_reply", clarify_reply="EVERYTHING_DOWN"),
            walk(rungs=["Settle", "HandleAllClear"], says=[scripts.SAY_ALL_CLEAR]),
            say("yes please"),
            walk(rungs=["AskWifiScope"],
                 joined=" ".join((scripts.FILLER_ASK_SCOPE, scripts.ASK_WIFI_SCOPE)),
                 never=[scripts.ASK_WIFI_SCOPE_AGAIN]),
        ]),

    Scenario(
        "B2", "one device: the three tips are given in order, one per turn", [
            *_all_clear(),
            say("yes please"), walk(),
            say("just my laptop"),
            walk(rungs=["WifiTipRejoin"],
                 says=[scripts.FILLER_TIP_REJOIN, scripts.SAY_WIFI_TIP_REJOIN]),
            say("no that didn't help"),
            walk(rungs=["WifiTipCloser"],
                 says=[scripts.FILLER_TIP_CLOSER, scripts.SAY_WIFI_TIP_CLOSER]),
            say("still nothing"),
            walk(rungs=["WifiTipToggle"],
                 says=[scripts.FILLER_TIP_TOGGLE, scripts.SAY_WIFI_TIP_TOGGLE]),
        ]),

    Scenario(
        "B3", "the whole house gets the gateway tips instead", [
            *_all_clear(),
            say("yes please"), walk(),
            say("the whole house"),
            walk(rungs=["WifiTipPlacement"],
                 says=[scripts.FILLER_TIP_PLACEMENT, scripts.SAY_WIFI_TIP_PLACEMENT]),
            say("no change"),
            walk(rungs=["WifiTipNearby"],
                 says=[scripts.FILLER_TIP_NEARBY, scripts.SAY_WIFI_TIP_NEARBY]),
        ]),

    Scenario(
        "B4", "the last tip is the same whichever scope the caller gave", [
            *_all_clear(),
            say("yes please"), walk(),
            say("the whole house"), walk(),
            say("no change"), walk(),
            say("no change"),
            walk(rungs=["WifiTipRestart"],
                 says=[scripts.FILLER_TIP_RESTART, scripts.SAY_WIFI_TIP_RESTART]),
        ]),

    Scenario(
        "B5", "when the tips run out the caller is handed over, not looped", [
            *_all_clear(),
            say("yes please"), walk(),
            say("the whole house"), walk(),
            say("no change"), walk(),
            say("no change"), walk(),
            say("no change"),
            # Only reachable because `drive()` runs the real tip bodies: the cap counts
            # the latches they write into `context.state`, so a hand-fed result would
            # walk this caller round the same three tips for ever.
            walk(rungs=["WifiExhausted"], says=[scripts.SAY_WIFI_EXHAUSTED],
                 escalated=True),
        ]),

    Scenario(
        "B5b", "a tip the caller has already tried is not offered to them again", [
            *_all_clear(),
            say("yes please"), walk(),
            # `wifi_tried` is the "already did that" cue behind every tip. Without it
            # the walkthrough hands back a step the caller has just described doing.
            say("just my laptop, and i already moved closer to the router"),
            walk(never=[scripts.SAY_WIFI_TIP_CLOSER]),
        ]),

    Scenario(
        "B6", "declining the walkthrough reaches a person", [
            *_all_clear(),
            say("no thanks"),
            walk(rungs=["WifiDeclined"], says=[scripts.SAY_WIFI_DECLINED],
                 escalated=True),
        ]),

    Scenario(
        "B7", "a caller whose Wi-Fi came back is closed out warmly, not escalated", [
            *_all_clear(),
            # NOT "it's working now": `DECLINE` carries a bare "no", which is an
            # unanchored substring and matches inside "now". See the note on B7b.
            say("it's working"),
            walk(rungs=["WifiFixed"], says=[scripts.SAY_WIFI_FIXED], escalated=False),
        ]),

    Scenario(
        "B7b", "a caller who says it is working NOW is not heard as refusing", [
            *_all_clear(),
            # The cues are unanchored regexes, so "no" used to fire inside "now" and
            # `cue_priority="first"` handed the turn to DECLINE over the RESOLVED cue the
            # caller actually said. Word-anchored, and RESOLVED declared first.
            say("actually its working now"),
            walk(rungs=["WifiFixed"], says=[scripts.SAY_WIFI_FIXED], escalated=False),
        ]),

    Scenario(
        "B7c", "describing the fault is neither accepting nor refusing", [
            *_all_clear(),
            # "ok" is inside "broken" and "no" is inside "know", so an unanchored cue set
            # read a caller describing their problem as answering the offer.
            say("my router is broken and i know it has been for days"),
            walk(rungs=[], filled={"wifi_walkthrough": None}),
        ]),

    Scenario(
        "B8", "a caller who says a tip worked is closed out mid-walkthrough", [
            *_all_clear(),
            say("yes please"), walk(),
            say("just my laptop"), walk(),
            say("that worked"),
            walk(rungs=["WifiFixed"], says=[scripts.SAY_WIFI_FIXED], escalated=False),
        ]),
]


# ==============================================================================
# C. The scope question asked DURING the sweep
# ==============================================================================
# The whole point of this section is the window in which a remote job is still
# outstanding: the question may only be put while the specialists have not reported
# (`EARLY_SCOPE_ASKABLE` requires `network_status` unfilled) and the acknowledgement only
# while the diagnosis has not landed. So the sweep is deliberately left half-finished --
# `Specialists` is started but not answered -- and the caller speaks into the gap.
#
# That window is reachable only because the harness answers the platform's in-flight-job
# poll on the caller's behalf; see `harness.Call._drain_poll`. Without it the poll takes
# the turn, the caller's words appear to vanish, and all four rungs below look untestable.

def _mid_sweep() -> list:
  """Up to the point where the checks are running and the caller can still speak."""
  return [say(DOWN), walk(), gate(), legs(),
          task_returns("Specialists", resolve_specialists_remote__job="JOB1")]


SECTION_C = [
    Scenario(
        "C1", "the scope question is asked while the checks are still running", [
            say(DOWN), walk(),
            # On the gate's OWN turn, not the one after: an announce rather than a rung,
            # so the caller hears it in the same breath as the holding line.
            gate(says=[scripts.SAY_BRIDGE_TO_SWEEP_REST, scripts.ASK_WIFI_SCOPE_EARLY]),
        ]),

    Scenario(
        "C2", "a caller who named one app is not asked a whole-house question", [
            say("netflix is not working"),
            walk(),
            # A negative, and worth one: the `any` leg of EARLY_SCOPE_ASKABLE fails while
            # the clarification gate is open, and an ask that fires when it should not is
            # invisible to every positive assertion.
            gate(never=[scripts.ASK_WIFI_SCOPE_EARLY]),
        ]),

    Scenario(
        "C3", "answering mid-sweep is acknowledged, and the offer is made there", [
            *_mid_sweep(),
            say("just one device"),
            # `never` because the three answer rungs are NOT terminal: an answer the cues
            # resolve must not also collect the "that's fine, we don't know" line, or the
            # caller is acknowledged twice in one breath for one answer.
            walk(rungs=["AckScopeEarly"], says=[scripts.SAY_SCOPE_NOTED],
                 never=[scripts.SAY_SCOPE_UNSURE],
                 filled={"wifi_offered_early": "true"}),
        ]),

    Scenario(
        "C4", "the whole-house answer gets the whole-house wording", [
            *_mid_sweep(),
            say("the whole house"),
            # "on that device" is a promise about which device, made to someone who has
            # just said there is not one -- so this is a different line, not the same one.
            walk(rungs=["AckScopeEarlyAll"],
                 says=[scripts.SAY_SCOPE_NOTED_ALL_DEVICES],
                 filled={"wifi_offered_early": "true"}),
        ]),

    Scenario(
        "C5", "a caller already trying things is not offered the walkthrough twice", [
            *_mid_sweep(),
            say("just one device"), walk(),
            remote(network_status="healthy", gateway_status="healthy",
                   wifi_status="healthy", technician_type="",
                   activityType="TROUBLE_CALL", activityCode="", jobType="",
                   resolve_specialists_remote__job="JOB1"),
            walk(rungs=["Settle", "HandleAllClearAlreadyTrying"],
                 says=[scripts.SAY_ALL_CLEAR_ALREADY_TRYING],
                 never=[scripts.SAY_ALL_CLEAR]),
        ]),

    Scenario(
        "C6", "an answer that arrives after the verdict is noted, not acted on", [
            say(DOWN), walk(), gate(), legs(),
            specialists(net="impaired", tech="network tech"),
            walk(rungs=["Settle", "HandleNetworkTech"]),
            say("just one device"),
            # The checks already found a fault, so the scope cannot change the answer --
            # and saying so is better than silently ignoring the caller.
            walk(rungs=["AckScopeAfterVerdict"],
                 says=[scripts.SAY_SCOPE_NOTED_AFTER_VERDICT]),
        ]),

    # C6 above is the turn AFTER; this is the turn the answer and the fault land on
    # TOGETHER, which is the commoner of the two -- the question is hoisted into the wait,
    # so the answer turn and the turn the specialists report on are frequently one turn.
    #
    # `joined=`, for C7's reason: the words were all there before this rung existed and
    # only the sequence was wrong, so every other assertion in this file would have passed
    # on the recorded defect. The caller's utterance and the swap verdict are the live
    # ones, transcribed off the call this was reported from.
    Scenario(
        "C6b", "an answer that lands on the verdict's own turn is acknowledged first", [
            *_mid_sweep(),
            say_settling("honestly i think it's everything", gw="swap"),
            walk(rungs=["Settle", "AckScopeBeforeVerdict", "HandleHardwareSwap"],
                 joined=" ".join((scripts.SAY_SCOPE_NOTED_WITH_VERDICT,
                                  scripts.SAY_HARDWARE_SWAP_GATEWAY)),
                 # The after-verdict wording points back at a step already described, so
                 # on this turn it would describe itself. One answer, one acknowledgement.
                 never=[scripts.SAY_SCOPE_NOTED_AFTER_VERDICT]),
        ]),

    # The negative, and it is what keeps C6b honest: the acknowledgement is for the turn
    # the two collide on and no other. An unconfined one thanks the caller in front of
    # every later turn of the call.
    Scenario(
        "C6c", "and the caller is not thanked all over again on the turns after it", [
            *_mid_sweep(),
            say_settling("honestly i think it's everything", gw="swap"), walk(),
            say("so what happens now"),
            walk(never=[scripts.SAY_SCOPE_NOTED_WITH_VERDICT,
                        scripts.SAY_SCOPE_NOTED_AFTER_VERDICT]),
        ]),

    # C7 and C8 pin an ORDER, which is why both use `joined=`: every other assertion here
    # would pass on a turn that says the same things in the wrong sequence, and the wrong
    # sequence is the whole defect. The caller has gone and done something and come back
    # to report how it went, and hearing the line checks first reads as not having
    # listened.
    #
    # They also need `say_settling` rather than `say` + `remote`: the result has to be in
    # hand before the DAG is walked, or a tip has already been dispatched and spoken its
    # filler, and the turn under test is a different one.
    Scenario(
        "C7", "a tip answer is acknowledged before the result that lands on the "
              "same turn", [
            *_mid_sweep(),
            say("just one device"), walk(),
            say("yes please"),
            walk(rungs=["WifiTipRejoin"]),
            say_settling("no that didn't help"),
            walk(rungs=["Settle", "AckWifiTipAnswer", "HandleAllClearAlreadyTrying",
                        "WifiTipCloser"],
                 joined=" ".join((scripts.SAY_WIFI_TIP_ACKNOWLEDGED,
                                  scripts.SAY_ALL_CLEAR_ALREADY_TRYING,
                                  scripts.FILLER_TIP_CLOSER,
                                  scripts.SAY_WIFI_TIP_CLOSER))),
        ]),

    Scenario(
        "C8", "and ahead of a fault verdict too, which ends the walkthrough", [
            *_mid_sweep(),
            say("just one device"), walk(),
            say("yes please"),
            walk(rungs=["WifiTipRejoin"]),
            # A measured plant fault disarms every tip, correctly, so this is the only
            # thing standing between the caller's answer and being ignored outright.
            say_settling("no that didn't help", net="impaired", tech="network tech"),
            walk(rungs=["Settle", "AckWifiTipAnswer", "HandleNetworkTech"],
                 joined=" ".join((scripts.SAY_WIFI_TIP_ACKNOWLEDGED,
                                  scripts.SAY_NETWORK_TECH))),
        ]),

    Scenario(
        "C9", "an ordinary tip answer is NOT acknowledged, because nothing collides", [
            # The negative, and it is what keeps C7 honest: on the ordinary path the sweep
            # settled before the offer, so `diagnostics_complete` holds on every tip turn.
            # A rung that is not confined to the mid-sweep path would thank this caller in
            # front of every tip for the rest of the call.
            *_all_clear(),
            say("yes please"), walk(),
            say("just my laptop"), walk(),
            say("no that didn't help"),
            walk(rungs=["WifiTipCloser"],
                 never=[scripts.SAY_WIFI_TIP_ACKNOWLEDGED]),
        ]),

    Scenario(
        "C10", "a caller who does not know is answered, rather than met with silence", [
            # Measured live and cold: asked whether it was everything or one device, the
            # caller said "I'm not sure to be honest" and the agent returned NOTHING. The
            # two rungs above own this turn for an answer the cues resolve, and "I don't
            # know" resolves to no scope, so nothing was eligible at all.
            *_mid_sweep(),
            say("i'm not sure to be honest"),
            walk(rungs=["AckScopeUnsure"], says=[scripts.SAY_SCOPE_UNSURE],
                 # It acknowledges and no more. The offer's two wordings are picked BY the
                 # scope answer, so neither can honestly be made here.
                 never=[scripts.SAY_SCOPE_NOTED, scripts.SAY_SCOPE_NOTED_ALL_DEVICES],
                 filled={"wifi_offered_early": None}),
        ]),

    Scenario(
        "C11", "and it is said once, not in front of every later turn", [
            *_mid_sweep(),
            say("i'm not sure to be honest"), walk(),
            # The cue slot stays filled for the rest of the call, so "have I already said
            # this?" is the rung's own business. What this observes is the outcome: the
            # rest of the wait is the reassurance ladder's, not a thank-you on repeat.
            say("how long is this going to take"),
            # The SECOND rung of the reassurance ladder: it is drained one line per
            # waiting turn, and the first went on the turn after the acknowledgement.
            walk(never=[scripts.SAY_SCOPE_UNSURE],
                 says=[scripts.SAY_SWEEP_WAITING[1]]),
        ]),

    Scenario(
        "C12", "a caller who just said it is only their box is not asked whether it is "
               "the whole house", [
            # Measured live and cold: "everything else works fine" about a named TV box,
            # answered one breath later with "is it everything in the house, or just the
            # one device?". `device_searched` was supposed to prevent it and cannot -- the
            # search fires on this very turn, so the announce is evaluated while that
            # latch is still empty.
            say("my cable box keeps freezing"),
            walk(), gate(never=[scripts.ASK_WIFI_SCOPE_EARLY]), legs(),
            task_returns("Specialists", resolve_specialists_remote__job="JOB1"),
            # `say`, not `fill`: the cue map is what resolves this live, and the announce
            # is only evaluated against a turn the caller SPOKE on -- so a seeded reply
            # cannot reproduce the defect and pins nothing. Dropping the guard leg makes
            # this step fail with the recorded line, word for word.
            say("everything else works fine",
                never=[scripts.ASK_WIFI_SCOPE_EARLY],
                filled={"clarify_reply_device": "ONLY_APP"}),
            walk(never=[scripts.ASK_WIFI_SCOPE_EARLY]),
        ]),
]


# ==============================================================================
# D. The gateway restart
# ==============================================================================

SECTION_D = [
    Scenario(
        "D1", "a gateway a restart would fix is offered one", [
            say(DOWN), walk(), gate(), legs(), specialists(gw="reboot"),
            walk(rungs=["Settle", "OfferReboot"], says=[scripts.SAY_REBOOT_ASK],
                 filled={"reboot_offered": "true"}),
        ]),

    Scenario(
        "D2", "the caller accepts and the restart is sent", [
            say(DOWN), walk(), gate(), legs(), specialists(gw="reboot"), walk(),
            # The answer gate opens only on the turn AFTER the question was spoken, and
            # it is the hook that opens it -- so this step is also the proof that the
            # two-turn gate works, which no other offline check here can see.
            fill("set_confirm_reboot", confirm_reboot="yes",
                 filled={"reboot_answer_allowed": "true"}),
            walk(rungs=["ExecuteReboot"], joined=scripts.SAY_REBOOT_HOLD_WHOLE),
        ]),

    Scenario(
        "D3", "the caller declines and reaches a gateway specialist", [
            say(DOWN), walk(), gate(), legs(), specialists(gw="reboot"), walk(),
            fill("set_confirm_reboot", confirm_reboot="no"),
            walk(rungs=["DeclineRebootTransfer"], says=[scripts.SAY_REBOOT_DECLINED]),
        ]),

    Scenario(
        "D4", "a gateway restarted too recently says so instead of pretending", [
            say(DOWN), walk(), gate(), legs(), specialists(gw="reboot"), walk(),
            fill("set_confirm_reboot", confirm_reboot="yes"),
            task_fails("ExecuteReboot", rebooted=False, error_code="timeline_blocked",
                       text=scripts.SAY_REBOOT_BLOCKED, fc="verdict_reboot_declined"),
        ],
        endings=("ExecuteReboot.blocked",)),

    Scenario(
        "D5", "a caller who asks outright is not also offered, and the ladder goes on", [
            say("can you reboot my router"), walk(), gate(), legs(), specialists(),
            # Three rungs in one turn: the restart latches `reboot_done` rather than
            # `verdict_delivered`, so the diagnosis still gets spoken.
            walk(rungs=["Settle", "RebootOnRequest", "HandleAllClear"],
                 says=[scripts.SAY_REBOOT_HOLD, scripts.SAY_REBOOT_WHOLE,
                       scripts.SAY_ALL_CLEAR]),
        ]),

    Scenario(
        "D6", "a restart is refused when it cannot help, whoever asked for it", [
            say("just reboot my modem"), walk(), gate(), legs(),
            specialists(gw="swap"),
            walk(rungs=["Settle", "HandleHardwareSwap"],
                 says=[scripts.SAY_HARDWARE_SWAP_GATEWAY]),
        ]),

    Scenario(
        "D7", "an unanswered offer is re-asked in the shorter words", [
            say(DOWN), walk(), gate(), legs(), specialists(gw="reboot"), walk(),
            say("sorry what was that"),
            walk(says=[scripts.SAY_REBOOT_REASK]),
        ]),
]


# ==============================================================================
# E. Collecting the account number, and silence
# ==============================================================================

SECTION_E = [
    Scenario(
        "E1", "the call opens by asking for the account", [
            walk(says=[scripts.ASK_ACCOUNT_NUMBER]),
        ],
        account=None),

    Scenario(
        "E2", "three unusable numbers reach a person rather than looping", [
            say(DOWN),
            fill("set_account_number", account_number="123"),
            fill("set_account_number", account_number="456"),
            fill("set_account_number", account_number="789",
                 status="escalated", fc="verdict_account_block"),
        ],
        account=None,
        endings=("accountNumber.exhaust",)),

    Scenario(
        "E3", "a caller who says nothing is reprompted twice, then handed over", [
            # The first tick is deliberately silent: a caller who has not spoken yet is
            # not yet a caller who is stuck.
            quiet(silent=True),
            # Question-neutral, and no apology. The ladder is declared once per flow but
            # applied to whichever slot is awaited, so naming the account number here sent
            # a caller who went quiet mid-walkthrough back to a question they had already
            # answered.
            quiet(says=["I didn't catch that. Go ahead whenever you're ready."]),
            quiet(says=["I still didn't get that. I'm listening. Take your time."]),
            quiet(status="complete", fc="verdict_human_request"),
        ],
        account=None,
        endings=("no_input.exhaust",)),

    Scenario(
        "E4", "a caller looking for their number is told to take their time", [
            say("hold on let me find my account number",
                text=scripts.SAY_TAKE_YOUR_TIME),
        ],
        account=None,
        endings=("no_input.hold_ack",)),
]


# ==============================================================================
# F. Asking for a person, and cancelling
# ==============================================================================

SECTION_F = [
    Scenario(
        "F1", "a person is held for until the checks land, not refused", [
            say("can I speak to a person"),
            fill("transfer_to_human", text=scripts.SAY_HOLD_FOR_CHECKS,
                 status="in_progress"),
        ],
        endings=("escalate.hold",)),

    Scenario(
        "F2", "asking again gets the shorter holding line, not the same one", [
            say("can I speak to a person"),
            fill("transfer_to_human"),
            say("no really, a person"),
            fill("transfer_to_human", text=scripts.SAY_HOLD_FOR_CHECKS_AGAIN),
        ],
        endings=("escalate.hold_again",)),

    Scenario(
        "F3", "once a verdict has been given, the hand-off is honoured", [
            *_all_clear(),
            say("connect me to a representative"),
            fill("transfer_to_human"),
            walk(rungs=["EscalateHandoffSummary"], says=[scripts.SAY_HUMAN_ESCALATE],
                 escalated=True),
        ],
        endings=("escalate.honoured",)),

    Scenario(
        "F4", "during an outage a live agent is refused, however often it is asked", [
            say(DOWN), walk(), gate(), legs(outage="active"), specialists(), walk(),
            say("let me talk to an agent"),
            fill("transfer_to_human", text=scripts.SAY_OUTAGE_NO_AGENT,
                 status="in_progress"),
            say("no, I really want an agent"),
            fill("transfer_to_human", text=scripts.SAY_OUTAGE_NO_AGENT,
                 status="in_progress"),
        ],
        endings=("escalate.outage_refusal",)),

    Scenario(
        "F5", "cancelling is confirmed before it is acted on", [
            *_all_clear(),
            say("forget it"),
            fill("cancel_flow", next="readback", says=[scripts.SAY_CONFIRM_CANCEL]),
        ],
        endings=("cancel.confirm",)),

    Scenario(
        "F6", "a confirmed cancel closes the call warmly and does not escalate", [
            *_all_clear(),
            say("forget it"),
            fill("cancel_flow"),
            say("yes", says=[scripts.SAY_CANCELLED], escalated=False),
        ],
        endings=("cancel.commit",)),
]


# ==============================================================================
# G. The clarification gate, app advice and device help
# ==============================================================================

SECTION_G = [
    Scenario(
        "G1", "a caller naming one app is asked whether it is only that app", [
            # The question waits for the sweep. It has to: asking it earlier would put a
            # second question in front of a caller who is already waiting on the checks.
            say("netflix is not working"),
            walk(), gate(), legs(), specialists(),
            # `app_name` is a model-set slot with no cue, so offline it renders its
            # fallback. Pinning the FALLBACK is the point: it is what every caller whose
            # app the model failed to name actually hears.
            walk(says=[render(clarify.ASK_CLARIFY)]),
        ]),

    Scenario(
        "G2", "only that app: the advice is about the app, not the line", [
            say("netflix is not working"),
            walk(), gate(), legs(), specialists(), walk(),
            fill("set_clarify_reply", clarify_reply="ONLY_APP"),
            walk(rungs=["Settle", "AdviseAppSpecific"],
                 says=[render(clarify.SAY_ONLY_APP)]),
        ]),

    Scenario(
        "G3", "everything is down: the gate bridges straight into the checks", [
            say("netflix is not working"), walk(),
            fill("set_clarify_reply", clarify_reply="EVERYTHING_DOWN",
                 says=[clarify.SAY_EVERYTHING_DOWN]),
        ]),

    Scenario(
        "G4", "a caller who is not sure gets the same bridge, not another question", [
            say("netflix is not working"), walk(),
            fill("set_clarify_reply", clarify_reply="UNSURE",
                 says=[clarify.SAY_UNSURE]),
        ]),

    Scenario(
        "G5", "a named piece of equipment gets its own question, not the app one", [
            say("my xfi pod keeps dropping off"),
            walk(), gate(), legs(), specialists(),
            # A different question, because "is it only that app" is the wrong thing to
            # ask someone who named a piece of hardware.
            walk(says=[render(clarify.ASK_CLARIFY_DEVICE)]),
        ]),

    Scenario(
        "G6", "equipment steps are looked up rather than diagnosed", [
            say("my xfi pod keeps dropping off"),
            walk(), gate(), legs(), specialists(), walk(),
            fill("set_clarify_reply_device", clarify_reply_device="ONLY_APP"),
            walk(rungs=["BuildDeviceQuery"]),
            task_returns("AnswerDeviceQuestion", snippets=["step one", "step two"],
                         search_query="xfi pod dropping off"),
        ]),

    Scenario(
        "G7", "when the lookup finds nothing, the caller is offered a person", [
            say("my xfi pod keeps dropping off"),
            walk(), gate(), legs(), specialists(), walk(),
            fill("set_clarify_reply_device", clarify_reply_device="ONLY_APP"),
            walk(rungs=["BuildDeviceQuery"]),
            task_fails("AnswerDeviceQuestion", snippets=[],
                       says=[device_help._NO_STEPS_SAY]),
        ],
        endings=("AnswerDeviceQuestion.no_results",)),

    Scenario(
        "G5b", "a TV box is equipment too, and gets the device question", [
            say("my cable box keeps freezing"),
            walk(), gate(), legs(), specialists(),
            walk(says=[render(clarify.ASK_CLARIFY_DEVICE)]),
        ]),

    # The repeat this ladder exists for, reported off the deployed demo over voice and
    # reproduced there: the question was spoken at 20.3s, again at 30.3s on the sweep's
    # own completion push, and again at 45.6s on a tick, all three in the same words.
    #
    # `joined=` on every step, because the defect is ORDER and IDENTITY rather than
    # presence -- `says=` would pass on a turn that spoke rung one twice.
    Scenario(
        "G5f", "asked once: the checks reporting in does not ask it again", [
            say("my cable box keeps freezing"),
            walk(), gate(), legs(), specialists(),
            walk(joined=render(clarify.ASK_CLARIFY_DEVICE)),
            # The turn CES makes to deliver a leg's result. It carries nothing the engine
            # has not already ingested, and before the ask ladder it re-asked verbatim.
            delivered("SweepLegs_leg_outage_leg",
                      joined=render(clarify.ASK_CLARIFY_DEVICE_AGAIN)),
            # BOTH legs are asynchronous, so there are two completions to deliver. They
            # rode one turn on the session this was reproduced from and they need not:
            # whether the platform batches them is its choice, and a question asked a
            # third time is the same defect either way. Last rung, and it asks for
            # nothing.
            delivered("SweepLegs_leg_convoy_leg",
                      joined=clarify.SAY_CLARIFY_STILL_HERE),
            # From here the silence is the `no_input` ladder's, whose first rung is a
            # deliberate silent tick. The ask ladder must not talk over it.
            quiet(silent=True),
            # Clamped, and the answer still lands: the ladder changes the wording, never
            # what the slot will accept.
            fill("set_clarify_reply_device", clarify_reply_device="ONLY_APP"),
            walk(rungs=["BuildDeviceQuery"]),
            task_returns("AnswerDeviceQuestion", snippets=["step one"],
                         search_query="cable box freezing"),
        ]),

    Scenario(
        "G1b", "the app wording of the question re-asks in its own words too", [
            say("netflix is not working"),
            walk(), gate(), legs(), specialists(),
            walk(joined=render(clarify.ASK_CLARIFY)),
            delivered(joined=render(clarify.ASK_CLARIFY_AGAIN)),
            fill("set_clarify_reply", clarify_reply="UNSURE",
                 says=[clarify.SAY_UNSURE]),
        ]),

    Scenario(
        "G5c", "so is a camera", [
            say("my doorbell camera keeps dropping off"),
            walk(), gate(), legs(), specialists(),
            walk(says=[render(clarify.ASK_CLARIFY_DEVICE)]),
        ]),

    Scenario(
        "G5d", "and the Xfinity app", [
            say("the xfinity app will not load for me"),
            walk(), gate(), legs(), specialists(),
            walk(says=[render(clarify.ASK_CLARIFY_DEVICE)]),
        ]),

    Scenario(
        "G5e", "a caller naming only the remote gets it as well", [
            say("my remote has stopped responding"),
            walk(), gate(), legs(), specialists(),
            walk(says=[render(clarify.ASK_CLARIFY_DEVICE)]),
        ]),

    Scenario(
        "G9", "everything is down, answered on the DEVICE question", [
            say("my cable box keeps freezing"),
            walk(), gate(), legs(), specialists(), walk(),
            fill("set_clarify_reply_device", clarify_reply_device="EVERYTHING_DOWN",
                 says=[clarify.SAY_EVERYTHING_DOWN]),
        ]),

    Scenario(
        "G10", "and the unsure branch of the same question", [
            say("my cable box keeps freezing"),
            walk(), gate(), legs(), specialists(), walk(),
            fill("set_clarify_reply_device", clarify_reply_device="UNSURE",
                 says=[clarify.SAY_UNSURE]),
        ]),

    Scenario(
        "G8", "two pieces of equipment are searched once, not twice", [
            say("my pod and my remote are both playing up"),
            walk(), gate(), legs(), specialists(), walk(),
            fill("set_clarify_reply_device", clarify_reply_device="ONLY_APP"),
            # A separate rung, mutually exclusive with the single-device one: two
            # searches in one turn kills the turn, so only one may ever fire.
            walk(rungs=["BuildDeviceQuery"]),
            task_returns("AnswerDeviceQuestionMulti", snippets=["a", "b"],
                         search_query="pod remote"),
        ]),
]


# ==============================================================================
# H. The outage inquiry, which runs before the ladder can answer
# ==============================================================================

SECTION_H = [
    Scenario(
        "H1", "no outage: the caller is told once the check says so, and then offered "
              "a full one", [
            say("is there an outage in my area"),
            walk(), gate(),
            # Not before this. The answer waits for the outage leg to report -- `neq`
            # holds on an unfilled slot, so without the `filled` guard the reassurance is
            # given on the asking turn with nothing checked.
            legs(),
            walk(rungs=["InquiryNoOutage"], says=[scripts.SAY_INQUIRY_NO_OUTAGE],
                 filled={"inquiry_answered": "true"}),
        ]),

    Scenario(
        "H1b", "and the asking turn itself promises nothing", [
            say("is there an outage in my area"),
            walk(never=[scripts.SAY_INQUIRY_NO_OUTAGE]),
            gate(never=[scripts.SAY_INQUIRY_NO_OUTAGE]),
        ]),

    Scenario(
        "H2", "an outage the checks found is reported to an asker", [
            say(DOWN), walk(), gate(), legs(outage="active"),
            say("is there an outage in my area"),
            walk(rungs=["InquiryOutageFound"]),
        ]),

    Scenario(
        "H3", "accepting the full check re-opens the ordinary ladder", [
            say("is there an outage in my area"),
            walk(), gate(), legs(), walk(),
            # The convoy leg is still outstanding: the inquiry answers the moment the
            # OUTAGE leg reports, without waiting for the rest of the sweep.
            legs(),
            fill("set_full_check", full_check="ACCEPT"),
            specialists(),
            walk(rungs=["Settle", "HandleAllClear"], says=[scripts.SAY_ALL_CLEAR]),
        ]),

    Scenario(
        "H4", "declining it closes the call warmly, with nothing escalated", [
            say("is there an outage in my area"),
            walk(), gate(), legs(), walk(), legs(),
            fill("set_full_check", full_check="DECLINE"),
            # The warm close is spoken, but the session does NOT end here and the sweep
            # runs on: a caller declines while the checks are still going, and a terminal
            # fire is deferred while a task is outstanding. Pre-existing -- the same is
            # true before the `filled` guard above -- and pinned as-is rather than fixed,
            # because ending the session early is an engine-level change of its own.
            walk(rungs=["InquiryDeclined"], says=[scripts.SAY_INQUIRY_DECLINED],
                 status="in_progress"),
        ]),
]


# ==============================================================================
# I. What a call costs, and hearing the caller
# ==============================================================================

SECTION_I = [
    Scenario(
        "I1", "with no visit on the table, nothing costs anything", [
            *_all_clear(),
            say("is this going to cost me anything"),
            walk(rungs=["AnswerNoCharge"], says=[scripts.SAY_NO_CHARGE]),
        ]),

    Scenario(
        "I2", "with a visit on the table, the fee schedule is given in full", [
            *_tech_visit(),
            say("is this going to cost me anything"),
            walk(rungs=["AnswerServiceFee"],
                 says=[render(scripts.SAY_SERVICE_FEE, technician_fee="$100")]),
        ]),

    Scenario(
        "I3", "asked a second time, the answer is the short one", [
            *_tech_visit(),
            say("is this going to cost me anything"), walk(),
            say("so will I be charged"),
            walk(rungs=["AnswerFeeAgain"], says=[scripts.SAY_FEE_AGAIN]),
        ]),

    Scenario(
        "I4", "a caller who sounds fed up is acknowledged once", [
            *_tech_visit(),
            say("this is so frustrating"),
            walk(rungs=["AckFrustration"], says=[scripts.SAY_ACK_FRUSTRATION]),
        ]),

    Scenario(
        "I5", "a caller who has already tried things is acknowledged differently", [
            *_tech_visit(),
            say("i already tried that"),
            walk(rungs=["AckAlreadyTried"], says=[scripts.SAY_ACK_ALREADY_TRIED]),
        ]),

    Scenario(
        "I6", "the two acknowledgements are mutually exclusive, so neither doubles up", [
            *_tech_visit(),
            say("this is so frustrating"), walk(),
            say("i already tried that"),
            walk(rungs=[]),
        ]),
]


# ==============================================================================
# J. The reboot flow, entered directly, with its own refusals
# ==============================================================================
# Every refusal here reuses the ladder's approved words for that state, so the caller
# gets one answer for their situation whichever door they came in by.

def _reboot_open() -> list:
  return [say("reboot my modem"), walk()]


SECTION_J = [
    Scenario("J1", "a clean line gets the restart it came for",
             [*_reboot_open(), gate(), specialists(), legs(),
              walk(rungs=["Settle", "DoReboot"], says=[scripts.SAY_REBOOT_STARTED])],
             flow="reboot"),

    Scenario("J2", "not during an outage",
             [*_reboot_open(), gate(), specialists(), legs(outage="active"),
              walk(rungs=["Settle", "RebootBlockedOutage"])],
             flow="reboot"),

    Scenario("J3", "not on a restricted account",
             [*_reboot_open(), gate(account="suspended"),
              walk(rungs=["RebootBlockedAccount"], says=[scripts.SAY_ACCOUNT_BLOCK])],
             flow="reboot"),

    Scenario("J4", "not when the gateway needs replacing",
             [*_reboot_open(), gate(), specialists(gw="swap"), legs(),
              walk(rungs=["Settle", "RebootBlockedSwapGateway"],
                   says=[scripts.SAY_HARDWARE_SWAP_GATEWAY])],
             flow="reboot"),

    Scenario("J5", "not when convoy predicts it is failing",
             [*_reboot_open(), gate(), specialists(), legs(action="predictive_swap"),
              walk(rungs=["Settle", "RebootBlockedSwapConvoy"],
                   says=[scripts.SAY_HARDWARE_SWAP_CONVOY])],
             flow="reboot"),

    Scenario("J6", "and not when there is no gateway to restart",
             [*_reboot_open(), gate(mac="NOT_FOUND"), legs(),
              walk(rungs=["Settle", "RebootNoGateway"],
                   says=[scripts.SAY_MISSING_HARDWARE_LEAD])],
             flow="reboot"),
]


# ==============================================================================
# K. The human flow: the caller who asked for a person up front
# ==============================================================================

SECTION_K = [
    Scenario("K1", "the caller who asked for a person gets one, and the call ends",
             [walk(rungs=["Escalate"], says=[scripts.SAY_HUMAN_ESCALATE],
                   escalated=True)],
             flow="human"),
]


# ==============================================================================
# Coverage gate inputs
# ==============================================================================

# Rungs no walk reaches. Empty, and worth keeping empty: a rung that can only fire while
# the checks are running is still walkable, because `harness.Call._drain_poll` answers the
# in-flight-job poll the way the platform does.
INERT_RUNGS: set[str] = set()

# Approved copy no walk can reach, each with the reason. An entry here is a hole in the
# coverage, so the list is asserted EXACTLY: a constant that starts being spoken must lose
# its entry, and one that stops existing must lose it too.
UNREACHABLE_COPY: dict[str, str] = {
    # Router copy. These live on the steering router, and every scenario enters a child
    # flow directly, because routing itself is a model decision with no offline path.
    "SAY_WELCOME": "spoken by the router, which routes with a model",
    "FILLER_ROUTING": "spoken by the router, which routes with a model",

    # Slot fillers, as opposed to task fillers. The engine gates `filler_say` on the
    # surface, and the one a sim presents drops it; the task fillers (FILLER_ASK_SCOPE,
    # every FILLER_TIP_*) do render and are asserted. Passing channel="voice" was tried
    # and does not change it.
    "FILLER_CLARIFY": "a slot filler, dropped on the surface a sim presents",
    "FILLER_FULLCHECK": "a slot filler, dropped on the surface a sim presents",
    "FILLER_WALKTHROUGH": "a slot filler, dropped on the surface a sim presents",

}

# Copy that is not unreachable but DEAD: named by nothing except its own definition.
#
# Empty, and the gate keeps it that way. The claim needs the agent SOURCE, not the
# transcript: a dead constant may be a SUBSTRING of a line that is spoken, which is how
# `SAY_OFFER_WHILE_CHECKING` read as covered while nothing referenced it. Both entries
# that were here have been deleted from `scripts.py`.
DEAD_COPY: dict[str, str] = {}


SCENARIOS = [*SECTION_A, *SECTION_B, *SECTION_C, *SECTION_D, *SECTION_E, *SECTION_F,
             *SECTION_G, *SECTION_H, *SECTION_I, *SECTION_J, *SECTION_K]
