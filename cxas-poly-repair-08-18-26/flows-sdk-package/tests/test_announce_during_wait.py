"""An announce that becomes eligible while an asynchronous task is outstanding.

Two defects, both found by driving a live voice agent that asks the caller something while
a thirty-second diagnostics job runs, and both of which destroy authored copy rather than
merely misplacing it.

  * The idle hold tested `announce_msgs` and never `announce_responses`. An
    `announce(name, texts)` populates only the second, so a `texts` announce that won an
    idle turn was latched by the cascade, dropped by the hold, and skipped for the rest of
    the call. Spoken nowhere, once, silently.
  * A conditional announce's latch was deactivated. `filled[name] = True` records that the
    announce SPOKE; it is history, not state its condition describes. The condition that
    let it fire is usually the thing its firing changes, so it flipped False and the latch
    was erased -- taking with it every gate that read "that announce fired".
"""

from __future__ import annotations

from test_async_tools import PENDING, drive, fb, fresh


ANNOUNCE_TEXT = "While those checks run, is it one device or all of them?"


def cfg_announce_in_wait(*, texts=True, condition=None, preempt=True):
  """An awaited lookup with an announce that becomes eligible DURING the wait.

  `ready` is the whole trick. An unconditional announce fires on turn 1 and latches
  before the wait even starts, which tests nothing -- the defect only appears when an
  announce first becomes eligible on an idle turn with a task already outstanding. The
  tests set `ready` by hand once the wait is under way.
  """
  announce = {"name": "note", "source": "announce", "preempt": preempt,
              "condition": condition if condition is not None
                           else {"slot": "ready", "filled": True}}
  if texts:
    announce["response"] = [{"type": "text", "text": ANNOUNCE_TEXT}]
  else:
    announce["message"] = ANNOUNCE_TEXT
  return {
      "slots": [
          {"name": "acct", "source": "user", "setter": "set_acct",
           "ask": "Account number?"},
          {"name": "status", "source": "task:poll"},
          {"name": "ready", "source": "task:poll"},
          announce,
      ],
      "tasks": [{
          "name": "poll", "tool": "poll_x", "inputs": ["acct"],
          "outputs": {"status_msg": "status"}, "success_check": "success",
          "terminal": False, "requires": ["acct"],
          "awaits": {"max_turns": 9, "while_waiting": ["Still checking."]},
      }],
      "gate_slot": None,
  }


def start_wait(cfg, sm):
  """Drive to the point where the lookup is outstanding."""
  drive(cfg, sm, "", turn=1)
  sm.update(fb.run_intake("set_acct", {"stored": True, "value": "1"}, sm)["sm"])
  drive(cfg, sm, "1", turn=2)
  sm.update(fb.run_intake("poll_x", dict(PENDING), sm)["sm"])


def open_the_window(sm):
  """Make the announce eligible, as the agent's own promotion does mid-wait."""
  sm["filled"]["ready"] = True


def spoken(action):
  """Everything the caller hears on a turn, whichever channel carried it."""
  out = [action.get("message") or ""]
  for part in action.get("response") or []:
    if isinstance(part, dict) and part.get("type") == "text":
      out.append(part.get("text") or "")
  return " ".join(t for t in out if t)


def test_a_texts_announce_is_spoken_on_an_idle_turn_during_a_wait():
  cfg = cfg_announce_in_wait()
  sm = fresh(cfg)
  start_wait(cfg, sm)
  open_the_window(sm)
  action = drive(cfg, sm, "", turn=3)["action"]
  assert ANNOUNCE_TEXT in spoken(action)


def test_the_reassurance_rung_is_not_spent_on_an_announce_turn():
  """`while_waiting` covers dead air, and an announce turn is not dead air."""
  cfg = cfg_announce_in_wait()
  sm = fresh(cfg)
  start_wait(cfg, sm)
  open_the_window(sm)
  drive(cfg, sm, "", turn=3)
  assert (sm["_awaiting_async"]["poll"] or {}).get("held", 0) == 0
  # ...and the ladder is still there for the turn that IS dead air.
  assert "Still checking." in spoken(drive(cfg, sm, "", turn=4)["action"])


def test_a_message_announce_during_a_wait_is_unchanged():
  """Regression pin: the `message` form already suppressed the hold."""
  cfg = cfg_announce_in_wait(texts=False)
  sm = fresh(cfg)
  start_wait(cfg, sm)
  open_the_window(sm)
  assert ANNOUNCE_TEXT in spoken(drive(cfg, sm, "", turn=3)["action"])


def test_no_eligible_announce_still_plays_the_reassurance():
  """Regression pin: the hold is untouched when there is nothing else to say."""
  cfg = cfg_announce_in_wait(condition={"slot": "acct", "eq": "never"})
  sm = fresh(cfg)
  start_wait(cfg, sm)
  assert "Still checking." in spoken(drive(cfg, sm, "", turn=3)["action"])


def test_a_non_preempting_announce_is_still_spoken_inline_during_a_wait():
  """A wait turn has no safe model-rendered fallback -- deferring it hands the model a
  free turn mid-wait, which is what the hold exists to prevent."""
  cfg = cfg_announce_in_wait(preempt=False)
  sm = fresh(cfg)
  start_wait(cfg, sm)
  open_the_window(sm)
  assert ANNOUNCE_TEXT in spoken(drive(cfg, sm, "", turn=3)["action"])


def test_a_conditional_announce_keeps_its_latch_when_the_condition_goes_false():
  """The latch is a record that it SPOKE. Losing it silently reopens every gate that
  read 'that announce fired' -- and in the agent this was found in, that was the gate
  opening a capture window, so the caller's answer was never collected."""
  cfg = cfg_announce_in_wait(condition={"all": [{"slot": "ready", "filled": True},
                                               {"slot": "status", "filled": False}]})
  sm = fresh(cfg)
  start_wait(cfg, sm)
  open_the_window(sm)
  assert ANNOUNCE_TEXT in spoken(drive(cfg, sm, "", turn=3)["action"])
  assert sm["filled"].get("note") is True

  # The result lands, so the announce's own condition is now False.
  sm.update(fb.run_intake(
      "poll_x", {"success": True, "status_msg": "done"}, sm)["sm"])
  drive(cfg, sm, "", turn=4)
  assert sm["filled"].get("note") is True, (
      "the announce's latch was deactivated -- it has spoken, and forgetting that "
      "reopens every gate that depends on it")


def test_an_ordinary_conditional_slot_still_deactivates():
  """The exemption is for announces only; cross-slot gating is unchanged."""
  cfg = cfg_announce_in_wait()
  cfg["slots"].append({"name": "extra", "source": "user", "setter": "set_extra",
                       "ask": "Extra?", "condition": {"slot": "status",
                                                      "filled": False}})
  sm = fresh(cfg)
  start_wait(cfg, sm)
  sm["filled"]["extra"] = "kept-for-now"
  sm.update(fb.run_intake(
      "poll_x", {"success": True, "status_msg": "done"}, sm)["sm"])
  drive(cfg, sm, "", turn=4)
  assert "extra" not in sm["filled"]
