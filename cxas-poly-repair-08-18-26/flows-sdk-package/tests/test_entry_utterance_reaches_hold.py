"""A flow entered through a gate must see the utterance that entered it.

The caller who asks for a moment in the same breath as their reason for calling
lands exactly on the entry turn: "my internet is down. hold on, let me find my
account number". The routing utterance IS carried across the gate — as the entry
text — but only the option cues, the correction focus and the system instruction
could ever read it. `_run_slot_filling` was handed `last_user_text`, which on the
first in-flow pass is empty (the model's last message is the bootstrap tool's
function response), so the hold matcher never ran and `hold_ack` and the whole
`hold_reprompts` ladder were unreachable for the one caller shape they exist for.
The caller got the question anyway — the one reply that request rules out.

The A/B is the point: the same sentence in `last_user_text` reaches `hold_ack`;
carried as entry text it did not.

Entry text is used ONLY to resolve hold state. It is deliberately NOT fed to
collection, steer-back or affirmation, which have their own reasons to read a
genuinely empty turn as empty — and NEVER on an inactivity tick, where feeding
the last real utterance back in would read a silence as re-engagement and talk
over a caller who just asked for a moment.
"""

from __future__ import annotations

from flows.engine import loader as fb

ENGINE = fb.load_engine()

ACK = "No problem, take your time. I'll be here when you're ready."
HOLD_UTTERANCE = "my internet is down. hold on, let me find my account number"
PLAIN_UTTERANCE = "my internet is down"
QUESTION = "What is your account number?"


def _gated_cfg() -> dict:
    """A gated flow that asks for a value and has a hold policy."""
    return {
        "gate_slot": "journey",
        "flow_types": ["repair"],
        "bootstrap": {"tool": "set_active_flow", "slot": "journey"},
        "slots": [{"name": "account_number", "source": "user",
                   "setter": "set_account", "ask": QUESTION}],
        "tasks": [],
        "no_input": {
            "reprompts": ["Sorry, I didn't catch that."],
            "hold_phrases": ["hold on", "give me a second", "let me find"],
            "hold_reprompts": ["", "", "Take your time."],
            "hold_ack": ACK,
            "on_exhaust": {"say": "Let me get someone to help."},
        },
    }


def _turn(cfg, sm, *, text="", scanned="", gate_text="", turn=1,
          inactivity=False):
    return ENGINE.slot_filling_engine({
        "raw_config": cfg, "sm": sm, "last_user_text": text,
        "scanned_user_text": scanned, "is_inactivity": inactivity,
        "event_data": {}, "config_id": "t", "n_user_turns": turn,
        "gate_user_text": gate_text,
    })


def _enter(cfg, utterance):
    """Drive the gate turn, then the first in-flow turn it hands over to.

    Returns `(sm, action)` for the in-flow turn — the one the caller experiences
    as the flow's first reply, and the one that carries no `last_user_text` of
    its own.
    """
    sm = fb.seed_sm(cfg)
    sm["filled"], sm["pending"] = {}, {}
    gate = _turn(cfg, sm, text=utterance, scanned=utterance, turn=1)
    carried = ((gate["action"].get("state_writes") or {})
               .get("set", {}).get("_gate_user_text", ""))
    assert carried == utterance, "the gate turn must stash the entry utterance"
    sm = gate["sm"]
    sm.setdefault("filled", {})["journey"] = "repair"
    result = _turn(cfg, sm, text="", scanned=utterance, turn=2,
                   gate_text=carried)
    return result["sm"], result["action"]


# ── the A/B ─────────────────────────────────────────────────────────────────

def test_a_hold_request_in_last_user_text_is_acknowledged():
    """The control half: the same sentence, mid-flow, already works."""
    cfg = _gated_cfg()
    sm = fb.seed_sm(cfg)
    sm["filled"], sm["pending"] = {"journey": "repair"}, {}
    action = _turn(cfg, sm, text=HOLD_UTTERANCE, scanned=HOLD_UTTERANCE)["action"]

    assert action.get("message") == ACK
    assert sm.get("_hold_on") is True


def test_a_hold_request_on_the_entry_turn_is_acknowledged():
    """The defect: identical sentence, carried across the gate instead."""
    sm, action = _enter(_gated_cfg(), HOLD_UTTERANCE)

    assert action.get("message") == ACK
    assert sm.get("_hold_on") is True
    assert sm.get("_awaiting") == "account_number", "the question is not consumed"


# ── everything the entry text must NOT change ───────────────────────────────

def test_an_ordinary_entry_utterance_still_gets_the_question():
    sm, action = _enter(_gated_cfg(), PLAIN_UTTERANCE)

    assert action.get("message") == QUESTION
    assert sm.get("_hold_on") is None


def test_an_inactivity_tick_never_reads_the_entry_utterance():
    """A silence tick must stay a silence tick.

    `entry_user_text` is the last REAL utterance. Feeding it to a tick would
    re-enter the speech branch, read the silence as re-engagement and talk over a
    caller who has just asked for a moment.
    """
    cfg = _gated_cfg()
    sm, _ = _enter(cfg, PLAIN_UTTERANCE)  # the question is now on the table
    # Carried deliberately, though the entry pass has already spent it: the guard
    # has to hold even if a caller re-stashes one.
    action = _turn(cfg, sm, text="", scanned=PLAIN_UTTERANCE, turn=1,
                   gate_text=HOLD_UTTERANCE, inactivity=True)["action"]

    assert action.get("message") != ACK
    assert sm.get("_hold_on") is None
    assert sm.get("_no_input_counter"), "the silence ladder must have advanced"


def test_a_flow_with_no_hold_policy_is_untouched_by_entry_text():
    cfg = _gated_cfg()
    cfg.pop("no_input")
    _sm, action = _enter(cfg, HOLD_UTTERANCE)

    assert action.get("message") == QUESTION
