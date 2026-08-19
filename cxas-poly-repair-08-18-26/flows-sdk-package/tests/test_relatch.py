"""A latch that can be re-armed.

`since_turns` says "this happened on an EARLIER caller turn", which is exactly what a
step needs when it has asked the caller to go and DO something: do not move on until
they have actually replied. It works perfectly for a WRITE-ONCE latch -- a branch that
offers something once and reads its own latch afterwards.

It does not work for a latch that re-arms, and two things compound to stop it:

  * `_stamp_fills` records the FIRST fill and never restamps. For a write-once latch
    first and last fill are the same thing; for a re-arming one they are not.
  * `sets` deliberately refuses to overwrite a slot that is already filled, so the
    second time round there is no re-fill to stamp at all.

So an author who wants a re-arming latch ends up clearing the slot from a hook, on a
caller-turn boundary the hook has to work out for itself. `relatch=True` is that,
declared: a rung that `sets` this slot may re-arm it while it is filled, and doing so
refreshes the stamp `since_turns` reads.

The scenario throughout is a walkthrough handing out tips. Each tip ends in something
the caller has to go and try, which takes minutes. Give the next one too early and the
agent talks through the whole list to a caller who is behind the television.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import flows  # noqa: E402
from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)

TIPS = ["Try unplugging the router for thirty seconds.",
        "Now move the router away from the microwave.",
        "Last thing -- switch the TV box to the other socket."]


def _cfg(relatch=False):
  """THREE tips, each waiting on the caller having replied to the one before.

  Three, not two, and that is the whole reproduction. With two, the second correctly
  waits a turn: the stamp is one turn old by then whether or not it ever moves. The
  bug only shows on the third, because the stamp is STILL sitting on tip one -- so
  tips two and three come out together, on one turn, to a caller who has answered
  neither.
  """
  latch = (flows.event_slot("tip_given", relatch=True) if relatch
           else flows.event_slot("tip_given"))
  f = flows.Flow("walkthrough", root_agent="A")
  parts = [flows.user_slot("problem", ask="What's wrong?"), latch]
  for i, text in enumerate(TIPS, 1):
    gate = [{"slot": f"tip{i}_done", "filled": False}]
    if i > 1:
      # Every tip after the first waits for the caller to have replied to the last
      # one, which is exactly what `since_turns` is for.
      gate = [{"slot": f"tip{i - 1}_done", "filled": True},
              flows.since("tip_given", turns=1)] + gate
    parts.append(flows.announce(
        f"Tip{i}", [text], requires=["problem"],
        condition={"all": gate} if len(gate) > 1 else gate[0],
        sets={"tip_given": "true", f"tip{i}_done": "true"}, preempt=True))
    parts.append(flows.event_slot(f"tip{i}_done"))
  f.add(*parts)
  cfg = f.to_config()
  cfg["single_flow"] = True
  return cfg


class Caller:
  """Drives the flow, remembering sm across turns the way before_model does."""

  def __init__(self, relatch=False):
    self.cfg = _cfg(relatch)
    # Seeded, not spoken. A user slot fills through its SETTER, which only the model
    # calls; offline the engine would just keep asking, and the walkthrough behind it
    # -- the thing under test -- would never start.
    self.sm = {"filled": {"problem": "the wifi keeps dropping"}}
    self.turns = 0
    self.said = []

  def speaks(self, text="okay, I tried that"):
    self.turns += 1
    return self._run(text)

  def _run(self, text):
    out = fb.run_engine(self.cfg, self.sm, last_user_text=text, config_id="rl",
                        n_user_turns=self.turns)
    self.sm = out["sm"]
    spoken = " ".join(
        [out["action"].get("message") or ""]
        + [p.get("text") or "" for p in (out["action"].get("response") or [])
           if isinstance(p, dict)])
    self.said.append(spoken.strip())
    return spoken

  def tips_on_the_last_turn(self):
    return sum(1 for t in TIPS if t in self.said[-1])

  @property
  def stamp(self):
    return (self.sm.get("_filled_turn") or {}).get("tip_given")


# ---------------------------------------------------------------------------
# The defect, encoded so the fix has something to beat
# ---------------------------------------------------------------------------


def test_without_relatch_the_walkthrough_runs_ahead_of_the_caller():
  """Today's behaviour, and the reason a hook exists.

  Tip one latches `tip_given` on the turn it is given, so tip two is correctly held.
  The caller replies, and it is correctly released. But tip two's own `sets` cannot
  re-fill a slot that is already filled, so the stamp is STILL on tip one -- and tip
  three's gate, which asks the same question, is open on that same turn. Two tips land
  together on a caller who has answered neither."""
  c = Caller(relatch=False)
  c.speaks()
  assert c.tips_on_the_last_turn() == 1, "tip one should arrive alone"
  assert c.stamp == 1

  c.speaks()
  assert c.stamp == 1, (
      "the stamp moved without `relatch`; this test no longer describes the defect")
  assert c.tips_on_the_last_turn() == 2, (
      "expected tips two AND three together -- the defect this exists to encode")


def test_with_relatch_each_tip_waits_for_a_reply():
  """The fix, as the caller experiences it: one tip per turn they answer on."""
  c = Caller(relatch=True)
  for expected_stamp in (1, 2, 3):
    c.speaks()
    assert c.tips_on_the_last_turn() == 1, (
        f"turn {c.turns} did not hand out exactly one tip")
    assert c.stamp == expected_stamp, "the latch did not re-arm on the turn it fired"


def test_relatch_re_arms_DURING_the_cascade_not_after_it():
  """Where the stamp is written matters. Announces cascade within one pass, so if the
  re-arm were deferred to the end-of-turn sweep, tip three would read tip one's stamp
  and come out alongside tip two anyway -- the defect, with extra steps."""
  c = Caller(relatch=True)
  c.speaks()
  c.speaks()
  assert c.tips_on_the_last_turn() == 1
  assert TIPS[2] not in c.said[-1], "tip three rode out on tip two's turn"


def test_a_re_invoke_inside_the_turn_does_not_advance_the_walkthrough():
  """`since_turns` counts CALLER turns, so the extra passes a turn makes -- a tool
  result, a steer-back -- cannot move the walkthrough on."""
  c = Caller(relatch=True)
  c.speaks()
  c._run("")                              # same caller turn, another pass
  assert c.tips_on_the_last_turn() == 0, "a re-invoke handed out another tip"


# ---------------------------------------------------------------------------
# What relatch must NOT change
# ---------------------------------------------------------------------------


def test_the_slot_stays_filled_either_way():
  """`relatch` moves a STAMP. It does not clear anything, so a plain `filled` leaf
  reads exactly the same with and without it -- which is what keeps this from being a
  behaviour change for every condition that is not turn-relative."""
  for relatch in (False, True):
    c = Caller(relatch=relatch)
    c.speaks()
    assert c.sm["filled"].get("tip_given"), relatch
    c.speaks()
    assert c.sm["filled"].get("tip_given"), relatch


def test_a_slot_without_relatch_keeps_the_no_overwrite_guard():
  """The guard `sets` has today exists so a late announce cannot silently rewrite a
  verdict that has already been spoken. Opting in per slot is what preserves it
  everywhere else, so this pins the VALUE, not just the stamp."""
  c = Caller(relatch=False)
  c.speaks()
  c.sm["filled"]["tip_given"] = "FIRST"
  c.speaks()
  assert c.sm["filled"]["tip_given"] == "FIRST", "a later `sets` clobbered the value"


def test_a_relatch_slot_cleared_and_refilled_still_works():
  """The ordinary sweep path is untouched: a slot that really is cleared loses its
  stamp and takes a fresh one on the next fill, exactly as before."""
  c = Caller(relatch=True)
  c.speaks()
  assert c.stamp == 1
  # `Tip1` as well as its two keys: an announce records its own dispatch by filling
  # its NAME, so a step whose outputs are cleared still will not run again while that
  # mark stands.
  for _k in ("tip_given", "tip1_done", "Tip1"):
    c.sm["filled"].pop(_k, None)
  c.speaks()
  assert c.stamp == 2, "a genuinely cleared slot did not take a fresh stamp"


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_the_validator_accepts_relatch():
  f = flows.Flow("t", root_agent="A")
  f.add(flows.user_slot("problem", ask="What's wrong?"),
        flows.event_slot("tip_given", relatch=True))
  app = flows.App(root_flow=f, app_display_name="Relatch")
  errors, _ = flows.validate_app(app)
  assert not [e for e in errors if "relatch" in e], errors


def test_relatch_is_omitted_when_not_asked_for():
  """An unset flag emits nothing, so every existing config is byte-identical."""
  plain = flows.event_slot("tip_given")
  assert "relatch" not in plain
  assert flows.event_slot("tip_given", relatch=True)["relatch"] is True
