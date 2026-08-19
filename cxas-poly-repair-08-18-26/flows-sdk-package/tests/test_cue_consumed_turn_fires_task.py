"""An utterance the engine already consumed must not also block a task.

`_task_fireable` holds back an input-free, `requires`-free, non-terminal executor
on any turn the caller spoke while an askable slot is unfilled. That guard is
right for its own reason (#698): firing would preempt the model's setter and drop
the caller's answer. It fires on the quiet post-setter pass instead.

But a turn whose utterance was consumed DETERMINISTICALLY has no post-setter
pass. An `option_cues` fill is not a model setter call, so nothing re-invokes the
engine, and there is no unread user intent left for a setter to preserve either.
The task is therefore unreachable for as long as the pending question stands —
and the pending question is simply re-asked, verbatim, with no retry counter
advancing (a cue turn reports no setter error). The caller asks a question, is
asked the pending one back, asks again, and the loop does not terminate.

The engine already models exactly this condition. `_apply_option_cues` records
`_event_prefilled_this_turn`, and the terminal-deferral at the fire site reads it
with this reasoning in the framework's own words: "there is then no unread user
intent to preserve". This gate did not consult it.
"""

from __future__ import annotations

from flows.engine import loader as fb

ENGINE = fb.load_engine()

ASK = "Shall we run the checks?"
ANSWER = "There is no charge for that."
CUE_UTTERANCE = "will I be charged for that?"


def _cfg() -> dict:
    """One pending question, one cue-filled passive slot, one say-only task."""
    return {
        "slots": [
            {"name": "side_question", "source": "user", "passive": True,
             "setter": "set_side_question", "kind": "intent",
             "option_cues": {"asked": [r"\bwill i be charged\b"]}},
            {"name": "consent", "source": "user", "setter": "set_consent",
             "ask": ASK},
        ],
        "tasks": [
            # Input-free, requires-free, non-terminal: precisely the shape the
            # gate holds back.
            {"name": "AnswerCost", "tool": "answer_cost", "inputs": [],
             "outputs": {"cost_answered": "cost_answered"},
             "success_check": "success", "terminal": False, "requires": [],
             "condition": {"slot": "side_question", "filled": True},
             "then_say": ANSWER},
        ],
        "gate_slot": None,
    }


def _drive(cfg, sm, text, turn=1):
    return ENGINE.slot_filling_engine({
        "raw_config": cfg, "sm": sm, "last_user_text": text,
        "scanned_user_text": text, "is_inactivity": False, "event_data": {},
        "config_id": "t", "n_user_turns": turn,
    })["action"]


def _asked(cfg):
    """A session with the question on the table, which is the whole point."""
    sm = fb.seed_sm(cfg)
    sm["filled"], sm["pending"] = {}, {}
    assert _drive(cfg, sm, "").get("message") == ASK
    return sm


def test_a_cue_consumed_question_is_answered_not_re_asked():
    cfg = _cfg()
    sm = _asked(cfg)

    action = _drive(cfg, sm, CUE_UTTERANCE, turn=2)

    assert sm["filled"].get("side_question") == "asked", "the cue must have filled"
    assert (action.get("function_call") or {}).get("name") == "answer_cost"
    assert action.get("message") != ASK, "the pending question was re-asked instead"


def test_the_pending_question_is_still_waiting_afterwards():
    """Answering the aside must not consume the question it interrupted."""
    cfg = _cfg()
    sm = _asked(cfg)
    _drive(cfg, sm, CUE_UTTERANCE, turn=2)

    assert "consent" not in sm["filled"]
    assert sm.get("_awaiting") == "consent"


def test_a_mixed_utterance_answers_without_closing_the_setter_out():
    """The residual risk of relaxing the gate, pinned.

    An utterance can carry BOTH the pending slot's answer and a cue ("12345, and
    will I be charged?"). The cue consumes the turn, so the task now fires — the
    thing the gate exists to prevent. What makes it survivable is that firing
    does not end the exchange: the executor goes out as a function call whose
    result round-trips and re-invokes, the question is not consumed, and the
    setter stays callable with the caller's words still in the model's contents.
    If any of those three stop holding, this fix is not safe and should go.
    """
    cfg = _cfg()
    cfg["slots"][1]["ask"] = "What is your account number?"
    sm = fb.seed_sm(cfg)
    sm["filled"], sm["pending"] = {}, {}
    ENGINE.slot_filling_engine({
        "raw_config": cfg, "sm": sm, "last_user_text": "", "scanned_user_text": "",
        "is_inactivity": False, "event_data": {}, "config_id": "t",
        "n_user_turns": 1,
    })

    action = _drive(cfg, sm, "12345, and will I be charged for that?", turn=2)

    assert (action.get("function_call") or {}).get("name") == "answer_cost"
    assert sm.get("_awaiting") == "consent", "the question must survive the fire"
    assert "consent" not in sm["filled"]
    assert "set_consent" not in (action.get("hide_tools") or []), (
        "the setter must stay callable, or the caller's value is lost outright")


def test_an_utterance_no_cue_consumed_still_holds_the_task_back():
    """The guard the gate exists for is untouched.

    Nothing consumed this turn deterministically, so the caller's words are still
    the model's to read and an input-free task must not preempt the setter.
    """
    cfg = _cfg()
    sm = _asked(cfg)
    # Pre-fill the cue slot on an EARLIER turn, so the task's condition holds but
    # THIS turn's utterance was not consumed by anything.
    sm["filled"]["side_question"] = "asked"

    action = _drive(cfg, sm, "yes go ahead and do it", turn=2)

    assert (action.get("function_call") or {}).get("name") != "answer_cost"


def test_the_post_setter_pass_still_fires_the_held_task():
    """The gate only ever delays. The pass the setter re-invokes must reach it."""
    cfg = _cfg()
    sm = _asked(cfg)
    sm["filled"]["side_question"] = "asked"
    _drive(cfg, sm, "yes go ahead and do it", turn=2)
    sm["filled"]["consent"] = "yes"  # what the model's setter records

    action = _drive(cfg, sm, "", turn=2)

    assert (action.get("function_call") or {}).get("name") == "answer_cost"
