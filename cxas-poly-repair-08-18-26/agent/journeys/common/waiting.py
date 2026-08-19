"""Covering the silences: the no-input ladder and latency fillers."""

__all__ = ['SAY_TAKE_YOUR_TIME']

# Lives here rather than in `scripts.py` because every journey imports this module, and
# `scripts.py` re-exports the journeys' copy, so reaching back would make the import
# graph a cycle. Spoken when the caller asks for TIME rather than falling silent.
SAY_TAKE_YOUR_TIME = "No problem, take your time. I'll be here when you're ready."

import flows


def _account_no_input():
    """The silence policy for a flow whose one asked slot is the account number."""
    # `no_input` is declared PER FLOW and inherits nothing, so every flow that can ask for
    # the account number needs its own. A function rather than a shared constant because
    # these are mutable dicts the builder may adopt, and two flows must not hold one.
    return {
        # QUESTION-NEUTRAL, and that is the point. `no_input` is declared per FLOW and the
        # engine applies whichever rung is next to whatever slot is currently awaited, so
        # copy that names one slot is wrong everywhere else. This flow awaits six of them:
        # a caller who falls quiet during the walkthrough and is re-asked for the account
        # number they gave a minute earlier is what naming a slot here costs, measured 3/3.
        #
        # The price is the helpful context a re-prompt is supposed to carry, and it cannot
        # be bought back here: silence is a flow-level policy in the SDK, never a per-slot
        # one. A per-slot `Slot.no_input` in `packages/flows` is filed as a framework gap;
        # when it lands, each ask gets its own re-prompt and this stays as the flow-wide
        # floor.
        "reprompts": [
            # The FIRST tick is deliberately silent. CES manufactures a turn after the
            # inactivity timeout and that turn is indistinguishable from the caller
            # speaking, so without this a caller who has opened the line and not yet
            # spoken is talked at. An empty reprompt is the engine's own silent-wait tick
            # (`action["silent"]` -> an empty LlmResponse, no audio).
            "",
            # No apology anywhere: the agent never says "sorry". The line owns the
            # mishearing and hands the floor straight back.
            "I didn't catch that. Go ahead whenever you're ready.",
            "I still didn't get that. I'm listening. Take your time.",
        ],
        # NARROWED from DEFAULT_HOLD_PHRASES, which the engine matches as a plain
        # substring anywhere in the utterance. The bare interruption markers ("hold on",
        # "hang on", "a second") far more often PREFIX a real question than constitute
        # one, so only the family meaning the caller is going to go and LOOK for something
        # is listed here.
        "hold_phrases": [
            "give me a", "gimme a", "one moment", "just a moment", "just a sec",
            "one sec", "let me find", "let me check", "let me look", "let me grab",
            "looking for", "find my", "grab my", "get my number", "still looking",
        ],
        # The rung count is the DURATION. This ladder counts turns, and on a held line the
        # only thing that makes a turn is the inactivity tick, so the hold a caller gets
        # is (rungs + 1) x `inactivityTimeout`: 8 x 5s = 40s. The caller this exists for
        # is reading a 16-digit account number off a bill.
        #
        # The silent rungs (`""`, the engine's own quiet wait tick) buy patience without
        # putting another sentence in front of someone who already said they were looking.
        "hold_reprompts": ["", "", "Take your time. I'm still here whenever you're "
                           "ready.", "", "", "", ""],
        # The caller who asks for time in the same breath as the complaint. The silence
        # ladder above cannot reach that turn — it carries speech — so without this the
        # flow asks for the account number anyway, the one reply the request rules out.
        "hold_ack": SAY_TAKE_YOUR_TIME,
        # `then` alone does NOT end the call: `verdict_human_request` REPORTS a hand-off
        # without performing one, and the silence exhaust sets `sm["status"]` without
        # emitting an end_session — CES ends a call on seeing a `Part.from_end_session`
        # and nothing else. `end_conversation: True` fires the `then` tool AND emits the
        # end_session, so the payload reaches the receiving human and the call closes.
        "on_exhaust": {
            "say": ("I'm having trouble hearing you. Let me connect you with someone "
                    "who can help."),
            "then": {"tool": "verdict_human_request"},
            "end_conversation": True,
        },
    }


def with_filler(slot, pool):
    """Attach a latency filler to a slot builder that has no `filler_say` kwarg."""
    # `user_slot` takes one natively; `intent_slot` does not, but both return the same
    # Slot dict and `filler_say` is a Slot field, so setting it here does the same thing.
    slot["filler_say"] = pool
    return slot
