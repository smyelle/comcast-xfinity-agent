"""The five task factories every verdict in the ladder is built from."""

import clarify
import flows

from journeys.common.gates import INQUIRY_SETTLED, NOT_YET_ANSWERED, SWEPT

__all__ = ['SAY_REBOOT_BLOCKED']

# Lives here rather than in `scripts.py` because every journey imports this module, and
# `scripts.py` re-exports the journeys' copy, so reaching back would make the import
# graph a cycle. Spoken when the reboot did NOT happen: `reboot` reports
# `timeline_blocked` when the gateway was restarted recently and `error` when Convoy
# could not be reached, and neither is worth retrying with identical inputs.
#
# Carries its own hand-off sentence because `on_exhaust.then` fires the transfer TOOL,
# not the declined RUNG, so that rung's line never plays.
#
# It states the restart flatly rather than hedging it, because the tool has measured it.
# "Gateway specialist" stays: it names the real person the caller is about to reach.
SAY_REBOOT_BLOCKED = (
    "Your gateway was restarted not long ago, so another reboot won't help yet. Let me "
    "get you to a gateway specialist who can take a closer look."
)



# --------------------------------------------------------------------------- #
# The priority ladder. ORDER IS THE CONTRACT — the engine fires the first task whose
# condition holds, so these must stay in source priority order.
# --------------------------------------------------------------------------- #


def advice_rung(name, tool, condition, then_say):
    """A rung that advises instead of diagnosing, for a caller whose fault is one app."""
    # Exempt from the clarification gate, because reaching ONLY_APP IS that gate
    # resolving. NOT exempt from `SWEPT`: the sweep runs unconditionally, so an area
    # outage is already measured, and an unswept advice rung outranks the outage rung and
    # stops the walk — telling a caller their app is at fault during a live outage.
    gated = {"all": [SWEPT, NOT_YET_ANSWERED, condition]}
    return flows.task(name, tool, [], "verdict_delivered",
                      out_key="verdict_delivered", condition=gated, then_say=then_say)


def rung(name, tool, condition, then_say, inputs=(), say_first=None, ends=None,
         say_first_is_copy=True):
    """One ladder rung: a condition-gated task that performs its action and speaks."""
    # NOT marked `terminal`, for two independent reasons. The engine DEFERS a terminal
    # fire on any turn carrying fresh user text, expecting a setter call and a post-setter
    # re-invoke; this flow collects nothing, so that re-invoke never arrives and the
    # verdict is never spoken. And a terminal is absorbing — it marks the flow complete
    # and every later turn re-speaks the same sentence. Latching `verdict_delivered`
    # instead makes the ladder speak exactly once and leaves later turns to the model.
    gated = {"all": [SWEPT, NOT_YET_ANSWERED, clarify.CLARIFIED, INQUIRY_SETTLED,
                     condition]}
    extra = {}
    # `say_first` speaks on the FIRING turn, `then_say` after the tool returns. For a tool
    # that DOES something slow that is the whole difference: the reboot is a ~3s round
    # trip, so a `then_say` leaves the caller in silence and then announces a signal that
    # has already gone.
    if say_first:
        extra["filler_say"] = say_first
        # ...but only on a surface that CAN speak one. The engine gates `filler_say` on
        # the surface's `filler` capability, which is False for `chat` — and `text`,
        # `web`, `webchat`, `api` and `mobile` all alias to `chat`. So on every text
        # channel the lead is discarded silently and the `then_say` renders alone,
        # stripping the diagnosis off a hand-off verdict.
        #
        # `say()` puts the WHOLE approved sentence back as the floor. `brief` — the
        # spoken form — stays the REST, because on voice the lead has already been spoken
        # as the filler, so the voice projection is unchanged.
        #
        # `say_first_is_copy=False` opts out for a lead that is a HOLDING LINE rather
        # than copy. Dropping one of those in a chat window is correct: the surface shows
        # a spinner, and re-injecting it would put an empty bubble in front of the answer.
        if say_first_is_copy:
            then_say = flows.say(f"{say_first} {then_say}", brief=then_say)
    task = flows.task(name, tool, list(inputs), "verdict_delivered",
                      out_key="verdict_delivered", condition=gated,
                      then_say=then_say, **extra)
    # `ends` closes the call after the rung has spoken — `then_response` rather than
    # `terminal=True`, for the deferral reason above. Only for the rungs that hand the
    # caller OVER: every other rung leaves the session open so a follow-up can still be
    # answered, but a rung that has just said "let me get you to someone" has nothing left
    # to answer with, and an open session lets the engine's own "All information
    # collected!" sentinel reach the caller.
    if ends is not None:
        task["then_response"] = _ends(ends)
    return task


def reboot_rung(name, tool, condition, then_say, inputs=(), say_first=None,
                latch="verdict_delivered", gated=True):
    """A rung whose tool can legitimately DECLINE to do the thing."""
    # `success_check` names a key in the executor's return: the engine treats a falsy
    # value as a failure, skips the output mapping — so the latch stays empty and the
    # ladder stays open — and runs `on_failure` instead of `then_say`.
    #
    # `gated=False` for the explicit request: it is not a verdict competing for the
    # diagnostic turn but the caller asking for an action, and it can arrive at any point
    # in the call. Its own condition names the states that keep it safe.
    cond = ({"all": [SWEPT, NOT_YET_ANSWERED, clarify.CLARIFIED, INQUIRY_SETTLED,
                     condition]} if gated
            else {"all": [SWEPT, clarify.CLARIFIED, INQUIRY_SETTLED, condition]})
    # No `say()` wrapper here, unlike `rung()`. What leads a reboot is a HOLDING LINE,
    # and the approved sentence sits behind `success_check` in `then_say` because
    # asserting the signal has gone before the call returns is a lie on two of the tool's
    # three outcomes. A surface that cannot speak a filler is right to drop this one.
    extra = {"filler_say": say_first} if say_first else {}
    return flows.task(name, tool, list(inputs), latch,
                      out_key=latch, condition=cond,
                      then_say=then_say, success_check="rebooted",
                      on_failure={"max_retries": 0,
                                  "on_exhaust": {
                                      "say": SAY_REBOOT_BLOCKED,
                                      "then": {"tool": "verdict_reboot_declined"}}},
                      **extra)


def offer_rung(name, tool, condition, then_say, latch="reboot_offered"):
    """A rung that ASKS rather than concludes."""
    # The latch is the offer's own flag, not `verdict_delivered`, so the ladder stays open
    # for the answer on the following turn, and each offer needs its own flag so two
    # cannot be confused on a call that reaches both. Speaking the question from a rung
    # rather than from the slot's own ask is what guarantees the caller hears it: the
    # model otherwise calls the setter with its own guess and pre-empts the question.
    #
    # It carries the clarification gate for the same reason every concluding rung does:
    # without it the offer outranks the gate, proposing a disruptive action before
    # anything has established the connection is at fault.
    gated = {"all": [SWEPT, NOT_YET_ANSWERED, clarify.CLARIFIED, INQUIRY_SETTLED,
                     condition]}
    return flows.task(name, tool, [], latch, out_key=latch,
                      condition=gated, then_say=then_say)


# What a closing rung emits so the CALL ACTUALLY ENDS. The transfer tool fires and `sm`
# records it, but CES ends a call on seeing an end_session part and on nothing else — so
# without this a caller who has been told they are being connected is left on the line
# with nothing pending, and the model fills that free turn with confident fiction.
def _ends(escalated):
  return [{"type": "end_session",
           "reason": "escalated" if escalated else "completed",
           "escalated": escalated}]


def say_rung(name, tool, condition, then_say, latch="verdict_delivered",
             ends=None, filler=None, requires=None):
    """A rung that only speaks, outside the diagnostic ladder's own gates."""
    # These turns come AFTER a verdict, so they cannot carry `NOT_YET_ANSWERED` and are
    # not gated on the clarification question. Each latches its own flag so it speaks
    # exactly once, which keeps one tip to one turn without a counter.
    #
    # `requires` is about WHEN, not whether. A rung with no `inputs` and no `requires` is
    # held back on any turn the caller has spoken while an askable slot is unfilled, so it
    # cannot preempt the model's setter — which costs it a tick. A rung that must speak in
    # the same breath as the turn it belongs to has to name what it depends on.
    extra = {"filler_say": filler} if filler else {}
    if requires:
        extra["requires"] = requires
    task = flows.task(name, tool, [], latch, out_key=latch,
                      condition=condition, then_say=then_say, **extra)
    if ends is not None:
        task["then_response"] = _ends(ends)
    return task
