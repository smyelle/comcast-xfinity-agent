"""Two cancel phrases in one breath must still be a cancel.

`_cancel_intent` is a whole-utterance EXACT match against a phrase set, and the
whole-utterance rule is the right rule: it is what stops "I don't want to cancel"
and "stop, I can hear you fine" from tearing a flow down. But it also rejects the
clearest cancel a caller can give. Measured on a converted agent, mid-wait:

    "forget it"            -> cancel_backstop -> cancel_flow
    "I give up"            -> cancel_backstop -> cancel_flow
    "forget it, I give up" -> nothing at all

The caller was maximally clear and got the worst outcome — and on a turn where
the model was preempted, the keyword classifier was the only detector left, so
"nothing at all" meant a dead line.

The fix splits on clause boundaries and requires EVERY clause to be a cancel
phrase, which makes the predicate a strict superset of the equality test and
nothing more. "Any clause matches" was rejected: it reads "forget it, can you
transfer me" as a cancel, and the half that decides there is the second one.
"""

from __future__ import annotations

import pytest

from flows.engine import loader as fb

ENGINE = fb.load_engine()


# Every row is an utterance a caller can plausibly say in one breath. The
# multi-clause TRUE rows are the ones that do not work today.
CANCELS = [
    # Two phrases run together — the defect.
    "forget it, I give up",
    "never mind, forget it",
    "stop it. quit.",
    "stop and exit",
    "cancel that, never mind",
    "forget it; stop",
    # Single-clause rows: the equality test already covers these and must keep
    # covering them.
    "forget it",
    "I give up",
    "cancel",
    "never mind",
    "stop",
    # The object regex, untouched.
    "cancel my order",
    "please cancel the whole reservation",
]

NOT_CANCELS = [
    # A cancel-shaped preamble to a question is a question. The clause that
    # decides is the second one.
    "forget it, can you transfer me",
    "stop, I can hear you fine",
    "no, stop",
    "cancel, but first tell me the balance",
    # Asking for time is the opposite of asking to leave.
    "hold on, let me find my account number",
    # Negations and topic mentions.
    "I don't want to cancel",
    "do not cancel my order",
    "I'm calling about a cancellation",
    "what is your cancellation policy",
    # Ordinary content.
    "my internet is down",
    "",
]


@pytest.mark.parametrize("utterance", CANCELS)
def test_a_cancel_said_twice_is_still_a_cancel(utterance):
    assert ENGINE._cancel_intent(utterance) is True


@pytest.mark.parametrize("utterance", NOT_CANCELS)
def test_a_cancel_shaped_clause_alone_does_not_cancel(utterance):
    assert ENGINE._cancel_intent(utterance) is False


def _cancellable_flow() -> dict:
    """One question, with a cancel disposition the caller can reach."""
    return {
        "slots": [{"name": "account_number", "source": "user",
                   "setter": "set_account", "ask": "What is your account?"}],
        "tasks": [],
        "gate_slot": None,
        "cancel": {"say": "Okay, I'll stop there."},
    }


def _drive(cfg, sm, text, turn=1):
    return ENGINE.slot_filling_engine({
        "raw_config": cfg, "sm": sm, "last_user_text": text,
        "scanned_user_text": text, "is_inactivity": False, "event_data": {},
        "config_id": "t", "n_user_turns": turn,
    })["action"]


def test_the_doubled_cancel_reaches_the_disposition():
    """The predicate is not the point; reaching `cancel` is.

    Asserted through the engine rather than the helper so the row fails if the
    backstop stops consulting `_cancel_intent`.
    """
    cfg = _cancellable_flow()
    sm = fb.seed_sm(cfg)
    sm["filled"], sm["pending"] = {}, {}
    _drive(cfg, sm, "")  # put the question, so there is something to abandon

    action = _drive(cfg, sm, "forget it, I give up", turn=2)

    assert action.get("tag") == "cancel_backstop"
    assert (action.get("function_call") or {}).get("name") == "cancel_flow"
