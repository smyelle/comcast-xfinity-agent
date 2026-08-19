"""`announce(sets=)`: extra slots written when the announce fires.

The point of the feature is a LADDER of mutually exclusive announces. Without a shared
latch every eligible rung fires on the same pass and the caller hears all of them, because
the cascade leaves as one preempt. With one, the first rung closes the gate the rest are
conditioned on, on the recompute the cascade already does after every announce.
"""

from __future__ import annotations

import flows
from flows.engine import blessed_source, loader


def _run(slots, filled=None):
  config = {"slots": slots, "tasks": []}
  sm = {"filled": dict(filled or {}), "pending": {}, "status": "in_progress",
        "task_results": {}}
  out = loader.run_engine(config, sm, last_user_text="", config_id="t")
  return out["action"], out["sm"]


def _spoken(action):
  return [p.get("text") for p in (action.get("response") or [])
          if p.get("type") == "text"]


def test_sets_lands_in_filled_where_conditions_read_it():
  action, sm = _run([flows.announce("Hello", ["hi"], sets={"greeted": "true"}, preempt=True)])
  assert sm["filled"].get("greeted") == "true"
  assert "hi" in _spoken(action)


def test_a_ladder_of_announces_speaks_exactly_one_rung():
  """The behaviour the feature exists for.

  Three rungs, all eligible on this turn, each gated on the shared latch being unset.
  The first writes it, the cascade recomputes, and the other two are no longer active.
  """
  ladder = [
      flows.announce("P1", ["first"], condition={"slot": "verdict", "filled": False},
                     sets={"verdict": "done"}, preempt=True),
      flows.announce("P2", ["second"], condition={"slot": "verdict", "filled": False},
                     sets={"verdict": "done"}, preempt=True),
      flows.announce("P3", ["third"], condition={"slot": "verdict", "filled": False},
                     sets={"verdict": "done"}, preempt=True),
  ]
  action, sm = _run(ladder)
  assert _spoken(action) == ["first"], "only the highest rung may speak"
  assert sm["filled"]["verdict"] == "done"
  assert "P2" not in sm["filled"] and "P3" not in sm["filled"]


def test_without_sets_the_same_ladder_speaks_everything():
  """The control, so the test above is measuring the latch and not the conditions.

  Same three rungs with nothing to close the gate: the cascade walks all of them and the
  caller hears three verdicts in one breath.
  """
  ladder = [flows.announce(f"P{i}", [t], condition={"slot": "verdict", "filled": False},
                           preempt=True)
            for i, t in ((1, "first"), (2, "second"), (3, "third"))]
  action, _sm = _run(ladder)
  assert _spoken(action) == ["first", "second", "third"]


def test_sets_does_not_overwrite_a_value_that_is_already_filled():
  """A rung fires on its own condition; it must not rewrite someone else's verdict.

  Reached here by a hook having already recorded the outcome. The announce still speaks
  and still latches ITSELF, but leaves the existing value alone.
  """
  action, sm = _run(
      [flows.announce("Late", ["late"], sets={"verdict": "overwritten"},
                       preempt=True)],
      filled={"verdict": "set_earlier"})
  assert sm["filled"]["verdict"] == "set_earlier"
  assert sm["filled"]["Late"] is True
  assert "late" in _spoken(action)


def test_an_announce_without_sets_is_unchanged():
  _action, sm = _run([flows.announce("Plain", ["plain"])])
  assert sm["filled"] == {"Plain": True}


def test_the_dsl_omits_the_key_entirely_when_no_sets_is_given():
  assert "sets" not in flows.announce("A", ["x"])
  assert flows.announce("A", ["x"], sets={"k": "v"})["sets"] == {"k": "v"}


def test_sets_on_a_non_announce_slot_is_a_validation_error():
  """It would be accepted, emitted, and then never read. Loudly rejected instead."""
  slot = flows.user_slot("acct", ask="Account number?", setter="set_acct")
  slot["sets"] = {"k": "v"}
  verdict = blessed_source.lint_config({"slots": [slot], "tasks": []}, ["set_acct"])
  assert not verdict["valid"]
  assert any("sets" in str(e) for e in verdict["errors"]), verdict["errors"]


def test_a_latch_holding_a_falsy_value_is_treated_as_unset():
  """The two halves of "already set" have to agree, and presence is the wrong half.

  `_eval_condition` reads a `filled` leaf as `bool(filled.get(slot))`, so a key present
  with a falsy value is UNFILLED to every condition. A presence-based guard here would
  leave the rung eligible while refusing to write the latch, so the gate could never
  close and every lower rung would speak too — the exact failure `sets` prevents, with
  nothing in the log to explain it.
  """
  ladder = [
      flows.announce("P1", ["first"], condition={"slot": "verdict", "filled": False},
                     sets={"verdict": "done"}, preempt=True),
      flows.announce("P2", ["second"], condition={"slot": "verdict", "filled": False},
                     sets={"verdict": "done"}, preempt=True),
  ]
  # A hook seeded the latch empty. Conditions call that unfilled, so the rung fires;
  # the write must therefore go through too.
  action, sm = _run(ladder, filled={"verdict": ""})
  assert _spoken(action) == ["first"]
  assert sm["filled"]["verdict"] == "done"


def test_a_truthy_value_is_still_never_overwritten():
  """The other side of the same guard: a real value belongs to whoever wrote it."""
  _action, sm = _run(
      [flows.announce("Late", ["late"], sets={"verdict": "overwritten"}, preempt=True)],
      filled={"verdict": "set_earlier"})
  assert sm["filled"]["verdict"] == "set_earlier"


def test_setting_a_falsy_value_is_a_validation_error():
  """Writing "" would latch nothing: the gate reads open and the ladder never closes."""
  slot = flows.announce("P1", ["x"], sets={"verdict": ""}, preempt=True)
  verdict = blessed_source.lint_config({"slots": [slot], "tasks": []}, [])
  assert not verdict["valid"]
  assert any("UNFILLED" in str(e) for e in verdict["errors"]), verdict["errors"]
