"""A `filler_say` a surface drops must leave a trace.

The gate is right: a latency mask ("one moment") is an empty extra bubble on a
surface that shows a spinner instead, so it is not emitted there. What was wrong
is that the drop was invisible — no log, no lint, no oracle row.

That matters because `filler_say` is also where an author puts the FIRST HALF of
a sentence they want spoken before the tool runs. On a surface with no `filler`
capability the first half vanishes and the second half renders on its own, so the
caller reads the consequence of a finding that was never stated. Four rungs of one
agent shipped that way, deterministically, on every text channel.

This does not change the gate. It makes the drop audible.
"""

from __future__ import annotations

from flows.engine import loader as fb

ENGINE = fb.load_engine()

LEAD = "Here is what the check found."
REST = "Here is what happens next."


def _cfg() -> dict:
    return {
        "slots": [{"name": "go", "source": "user", "setter": "set_go",
                   "ask": "Ready?"}],
        "tasks": [{"name": "RunCheck", "tool": "run_check", "inputs": [],
                   "outputs": {"checked": "checked"}, "success_check": "success",
                   "terminal": False, "requires": ["go"],
                   "filler_say": LEAD, "then_say": REST}],
        "gate_slot": None,
    }


def _fire(channel):
    """Drive to the turn the task fires on, returning `(action, log_tags)`."""
    cfg = _cfg()
    sm = fb.seed_sm(cfg)
    sm["filled"], sm["pending"] = {"go": "yes"}, {}
    sm["_log_level"] = "DEBUG"
    out = ENGINE.slot_filling_engine({
        "raw_config": cfg, "sm": sm, "last_user_text": "", "scanned_user_text": "",
        "is_inactivity": False, "event_data": {"channel": channel},
        "config_id": "t", "n_user_turns": 1,
    })
    entries = [e for e in (out["sm"].get("_log") or [])
               if e.get("tag") == "filler_suppressed_by_surface"]
    return out["action"], entries


def test_a_surface_that_can_speak_a_filler_speaks_it_and_logs_nothing():
    action, suppressed = _fire("voice")

    assert LEAD in (action.get("message") or "")
    assert suppressed == []


def test_a_surface_that_drops_the_filler_says_so():
    action, suppressed = _fire("text")

    assert LEAD not in (action.get("message") or "")
    assert len(suppressed) == 1
    assert suppressed[0]["data"]["task"] == "RunCheck"
    assert suppressed[0]["data"]["surface"] == "chat"
