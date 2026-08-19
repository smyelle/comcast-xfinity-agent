"""A question is not put again on a turn the caller did not take.

On voice the platform sends turns nobody took: an inactivity tick when the caller goes
quiet, and a push when an asynchronous check finishes. Both used to be answered like
ordinary turns, so an outstanding question was re-asked on each. Measured on a deployed
agent, one wording landed at 20.3s, 30.3s and 45.6s -- the caller's own turn, then a
completion push, then a tick -- while they were still thinking about the first.

`turn_kind` is the signal that stops it, and the interesting value is the third one:

  caller        the caller spoke, pressed a key, or talked over us
  manufactured  the platform authored the turn: a tick or a completion push
  continuation  another PASS inside a turn that already happened

Only before_model can tell these apart -- a completion push and a post-setter re-invoke
reach the engine identical on every input it has -- so the engine reads what it is told
and falls back to the pre-existing reading when told nothing.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402

from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)

RUNGS = ["Which device is affected?",
         "Sorry -- is it the TV box, or something else?",
         "Let me get someone who can help."]


def _cfg(no_input=None):
  """One question, asked up a three-rung ladder."""
  cfg = {
      "slots": [{
          "name": "device", "source": "user", "setter": "set_device",
          "ask": list(RUNGS),
      }],
      "tasks": [],
  }
  if no_input is not None:
    cfg["no_input"] = no_input
  return cfg


class Caller:
  """Drives one flow, remembering sm across turns the way before_model does."""

  def __init__(self, no_input=None):
    self.cfg = _cfg(no_input)
    self.sm = {}
    self.turns = 0
    self.said = []

  def speaks(self, text="the tv box is out"):
    self.turns += 1
    return self._run(text, turn_kind="caller")

  def tick(self):
    """A genuine CES inactivity turn -- the caller has gone quiet."""
    return self._run("", turn_kind="manufactured", is_inactivity=True)

  def push(self):
    """An asynchronous completion delivery. NOT silence: `is_inactivity` is false,
    which is why this turn never reached the no_input ladder and fell through to
    re-asking."""
    return self._run("", turn_kind="manufactured")

  def reinvoke(self):
    """The engine running again inside a turn that already happened."""
    return self._run("", turn_kind="continuation")

  def reinvoke_as_caller(self):
    """The re-invoke as it ACTUALLY arrives on a live call, which is not always as a
    continuation. Once CES has blanked the envelope, the newest user content in the
    request is the caller's last real utterance again, so the pass classifies as
    "caller" -- with the turn counter still frozen, because they have not spoken."""
    return self._run("", turn_kind="caller")

  def legacy_tick(self):
    """A caller that does not supply `turn_kind` at all -- an older before_model, or
    an offline driver. Must land on the same behaviour as `tick`."""
    return self._run("", is_inactivity=True)

  def _run(self, text, turn_kind="", is_inactivity=False):
    out = fb.run_engine(self.cfg, self.sm, last_user_text=text, config_id="mt",
                        n_user_turns=self.turns, is_inactivity=is_inactivity,
                        turn_kind=turn_kind)
    self.sm = out["sm"]
    action = out["action"]
    if (action.get("message") or "").strip():
      self.said.append(action["message"].strip())
    return action

  @property
  def rung(self):
    return self.sm.get("_ask_rung", {}).get("device")

  @property
  def counter(self):
    return self.sm.get("_no_input_counter", 0)


# ---------------------------------------------------------------------------
# The measured defect
# ---------------------------------------------------------------------------


def test_the_question_is_put_once_and_the_polls_are_silent():
  """The incident, in order: the caller speaks, a check finishes, the caller goes
  quiet. One question, not three."""
  c = Caller()
  assert c.speaks()["message"] == RUNGS[0]
  for poll in (c.push(), c.tick()):
    assert poll.get("silent") is True
    assert not (poll.get("message") or "")
  assert c.said == [RUNGS[0]], "the question was put again on a turn nobody took"


def test_polls_do_not_burn_the_ladder():
  """The other wrong answer -- advancing on each poll -- reaches the last-resort
  wording before the caller has said anything. The rung is held, so the SECOND thing
  they hear is the second rung."""
  c = Caller()
  c.speaks()
  assert c.rung == 0
  c.push(), c.push(), c.tick()
  assert c.rung == 0, "a poll spent a rung the caller never heard"
  assert c.speaks("no idea")["message"] == RUNGS[1]
  assert RUNGS[2] not in c.said


# ---------------------------------------------------------------------------
# The three values
# ---------------------------------------------------------------------------


def test_a_continuation_is_not_a_manufactured_turn():
  """THE distinction the engine cannot draw for itself. A post-setter re-invoke has
  the same empty text and the same turn count as a completion push; going silent on
  it would mute the engine inside a turn the caller is waiting on."""
  c = Caller()
  c.speaks()
  again = c.reinvoke()
  assert not again.get("silent")
  assert again["message"] == RUNGS[0], "a re-invoke must still render the question"


def test_a_continuation_inherits_the_turn_it_is_inside():
  """A push re-invokes the engine too. Those passes are still the platform's turn,
  so they stay silent rather than reverting to speech."""
  c = Caller()
  c.speaks()
  assert c.push().get("silent") is True
  assert c.reinvoke().get("silent") is True, "the pass reverted to a caller turn"
  assert c.sm["_turn_kind"] == "manufactured"


def test_a_re_invoke_misread_as_the_caller_cannot_unlatch_a_poll():
  """The regression the FIRST live drive found, and nothing offline could. The ticks
  came back silent and the completion push spoke, because a pass inside the push
  classified as "caller" -- the envelope had been blanked, so the newest user content
  was the caller's opening line again -- and overwrote the latch mid-turn.

  `n_user_turns` is the edge that holds: it counts real utterances only, so it is
  frozen for the whole of a manufactured turn and every pass of it. A "caller" reading
  on a turn number that has not moved is not a new turn, whatever it looks like."""
  c = Caller()
  c.speaks()
  assert c.push().get("silent") is True
  assert c.reinvoke_as_caller().get("silent") is True, "the poll lost its latch"
  assert c.sm["_turn_kind"] == "manufactured"
  # ...and the caller genuinely speaking still gets through, which is the other half.
  c.turns += 1
  assert not c.speaks("no idea").get("silent")


def test_the_kind_is_latched_on_sm_for_the_whole_turn():
  c = Caller()
  c.speaks()
  assert c.sm["_turn_kind"] == "caller"
  c.tick()
  assert c.sm["_turn_kind"] == "manufactured"
  c.turns += 1
  c.speaks("still broken")
  assert c.sm["_turn_kind"] == "caller"


# ---------------------------------------------------------------------------
# What must still speak
# ---------------------------------------------------------------------------


def test_the_first_question_is_never_withheld():
  """Only a question already PUT to this caller may be held back. Nothing is
  outstanding on the opening turn, so a poll that arrives there speaks."""
  c = Caller()
  first = c.push()
  assert not first.get("silent")
  assert first["message"] == RUNGS[0]


def test_a_poll_that_unblocks_a_NEW_question_speaks_it():
  """The free-polling design depends on this: a completion that moves the flow on is
  allowed to ask what comes next. Only the outstanding one is withheld."""
  c = Caller()
  c.speaks()
  assert c.push().get("silent") is True
  c.sm.setdefault("filled", {})["device"] = "tv box"   # the check answered it
  moved_on = c.push()
  assert not moved_on.get("silent"), "a poll that unblocked the flow was muted"


# ---------------------------------------------------------------------------
# The snapshot turn
# ---------------------------------------------------------------------------


def _stale_cfg():
  """A flow whose one question is NOT downstream of the task that lands."""
  return {"slots": [{"name": "reason", "source": "user", "setter": "set_reason",
                     "ask": "What's going on with your connection?"},
                    {"name": "device", "source": "user", "setter": "set_device",
                     "ask": list(RUNGS), "requires": ["reason"]},
                    {"name": "line_state", "source": "task"}],
          "tasks": [{"name": "check", "tool": "check_line", "inputs": ["reason"],
                     "outputs": {"line_state": "line_state"},
                     "requires": ["reason"]}],
          "single_flow": True}


def _stale_sm():
  """sm as the engine actually receives it on a completion turn.

  CES hands back the state from when the CALL was made -- `_turn_n` and `_awaiting` are
  two turns old -- but before_model has already routed the landing through intake, so
  the RESULT is present on top of that old state. Both halves matter: without the
  result the task simply re-fires, and without the old `_awaiting` there is nothing to
  reproduce. Taken from a live trace of this exact turn.
  """
  return {"_turn_n": 1, "_turn_kind": "caller", "_turn_kind_at": 1,
          "_awaiting": "reason", "pending": {},
          "filled": {"reason": "keeps dropping", "line_state": "upstream signal is low"},
          "task_results": {"check": {"success": True,
                                     "line_state": "upstream signal is low"}},
          # What the engine captured when it recognised the snapshot. Seeded rather
          # than driven from `_task_just_completed`, because leaving that key set sends
          # this turn through the whole just-landed handler, which wants a good deal
          # more faked state than the two lines under test here are worth.
          "_stale_landed_task": "check"}


def test_a_completion_landing_on_a_snapshot_is_recognised():
  """CES delivers an asynchronous completion by resuming the invocation that made the
  call, so the state arrives as it was AT THE CALL: `_awaiting` points at a question
  two turns old and every sm-derived guard is blind. Two live drives showed it -- the
  ticks silent, the poll speaking.

  `n_user_turns` is the way out because it is not state at all: before_model counts it
  off the request contents. On a turn the caller did not take it cannot legitimately
  run ahead of `_turn_n`, so a disagreement is a snapshot and nothing else.

  Only the DETECTION is asserted here. What the engine then does with the turn is left
  to the live drive on purpose: a completion push cannot be reproduced offline (the
  harness preempts this turn empty with or without this change, measured against the
  unmodified engine), which is also why the example is registered live-only.
  """
  out = fb.run_engine(_stale_cfg(), _stale_sm(), last_user_text="", config_id="mt",
                      n_user_turns=2, turn_kind="manufactured")
  assert out["sm"]["_sm_stale"] is True, "the snapshot was not recognised"
  # A completion turn runs several passes and only the first carries the landed task,
  # so the capture has to survive the ones that do not.
  assert out["sm"]["_stale_landed_task"] == "check"


def test_the_snapshot_branch_flattens_multi_slot_outputs():
  """An `outputs` value may name ONE slot or a list of them -- `{"result": ["a","b"]}`
  is the documented multi-slot form. Handing that straight to `set()` raises
  `TypeError: unhashable type: 'list'`, and it would raise on a completion push: the
  turn a caller is already waiting through, where a crash reads as the platform's
  "I'm having trouble" envelope. Reported by review on #765.

  Pinned at the helper and the wiring rather than by driving the crash, because that
  branch is not reachable from the offline harness -- something preempts the turn
  before the question is chosen, which is the same limitation that makes this example
  live-only. Measured, not assumed: driving `run_engine` with a list-valued `outputs`
  on a stale turn does not enter the branch at all.
  """
  import inspect

  engine = fb.load_engine(FRAMEWORK_ROOT)
  # The helper's contract: both shapes flatten, neither explodes.
  assert engine._output_targets({"a": "one"}) == ["one"]
  assert engine._output_targets({"a": ["one", "two"]}) == ["one", "two"]
  with pytest.raises(TypeError):
    set({"a": ["one", "two"]}.values())      # what the branch used to do

  src = inspect.getsource(engine._run_slot_filling)
  stale_branch = src[src.index("_stale_landed_task"):]
  assert "_output_targets(" in stale_branch[:600], (
      "the snapshot branch stopped going through the flattening helper")


def test_a_tick_on_CURRENT_state_is_not_treated_as_a_snapshot():
  """The detector must not fire on an ordinary tick, or it would take over a job
  `_awaiting` already does perfectly well on state that is not lying."""
  c = Caller()
  c.speaks()
  c.tick()
  assert c.sm["_sm_stale"] is False


def test_a_caller_turn_is_never_a_snapshot_however_far_the_count_has_moved():
  """A real utterance ALWAYS moves the counter past `_turn_n`, so the comparison alone
  would call every caller turn stale. Requiring a manufactured turn is what makes it
  mean anything."""
  c = Caller()
  c.speaks()
  c.turns += 5
  c.speaks("still broken")
  assert c.sm["_sm_stale"] is False


# ---------------------------------------------------------------------------
# no_input keeps first claim
# ---------------------------------------------------------------------------

LADDER = {"reprompts": ["Still there?", "Anyone there?"],
          "on_exhaust": {"say": "I'll let you go."}}


def test_a_declared_silence_ladder_still_owns_the_tick():
  """`no_input` is the author's policy for silence and it is untouched: the tick
  speaks its rung out loud and the counter advances."""
  c = Caller(LADDER)
  c.speaks()
  first = c.tick()
  assert first["message"] == "Still there?"
  assert first.get("speech_class") == "no_input"
  assert not first.get("silent")
  assert c.counter == 1
  assert c.tick()["message"] == "Anyone there?"
  assert c.tick()["message"] == "I'll let you go."


def test_a_completion_push_is_not_silence_and_never_spends_a_rung():
  """The half no existing test could express, because there was no input for it. A
  push is not the caller being quiet, so it must not walk the patience ladder -- but
  it must not re-ask either."""
  c = Caller(LADDER)
  c.speaks()
  pushed = c.push()
  assert pushed.get("silent") is True
  assert c.counter == 0, "a backend answering spent the caller's silence budget"
  assert c.tick()["message"] == "Still there?", "the ladder lost its place"


def test_a_barge_in_during_a_hold_still_walks_the_silence_ladder():
  """Why the branch was WIDENED rather than replaced. A barge-in marker can arrive
  alongside an inactivity envelope: before_model calls that the caller acting
  (ces-probes 161) and still sets `is_inactivity`, because it reads the flag off
  `last_user_text` after the fallback picks the envelope up. Keying the branch on
  `turn_kind` alone would have quietly dropped this turn out of the ladder."""
  c = Caller(LADDER)
  c.speaks()
  barged = c._run("", turn_kind="caller", is_inactivity=True)
  assert barged["message"] == "Still there?"
  assert barged.get("speech_class") == "no_input"
  assert c.counter == 1


def test_a_flow_with_no_silence_policy_waits_quietly():
  """The behaviour change. With no `no_input` the engine used to re-ask on every
  tick forever; now it puts the question once and waits. No ladder state, no
  termination, and the question stays outstanding -- only the nagging is gone."""
  c = Caller()
  c.speaks()
  for _ in range(8):
    assert c.tick().get("silent") is True
  assert c.said == [RUNGS[0]]
  assert c.rung == 0
  assert c.sm.get("status") is None
  assert c.sm.get("_awaiting") == "device"


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


def test_a_caller_that_supplies_no_turn_kind_reads_a_tick_as_the_platform():
  """The fallback, which is what every existing offline driver hits: a tick is the
  platform, anything else is the caller."""
  c = Caller()
  c.speaks()
  assert c.legacy_tick().get("silent") is True
  assert c.sm["_turn_kind"] == "manufactured"


def test_a_session_that_predates_the_signal_speaks_rather_than_going_quiet():
  """A call already in flight when the framework is upgraded carries an sm with no
  latched kind, so a continuation arriving there has nothing to inherit. It speaks:
  between one extra question and dead air on a live call, dead air is the harder
  failure to notice and the harder one to recover from."""
  legacy = {"_awaiting": "device", "_turn_n": 1, "filled": {}, "pending": {},
            # Mid-question: rung one was put on this turn and is therefore held.
            "_ask_rung": {"device": 0}, "_ask_rung_turn": {"device": 1}}
  out = fb.run_engine(_cfg(), legacy, last_user_text="", config_id="mt",
                      n_user_turns=1, turn_kind="continuation")
  assert not out["action"].get("silent")
  assert out["action"]["message"] == RUNGS[0]


def test_an_unknown_turn_kind_is_treated_as_the_caller():
  """Forward compatibility: a value this engine does not know must speak rather than
  go quiet, because silence is the harder failure to notice."""
  c = Caller()
  c.speaks()
  out = fb.run_engine(c.cfg, c.sm, last_user_text="", config_id="mt",
                      n_user_turns=1, turn_kind="teleport")
  assert not out["action"].get("silent")
