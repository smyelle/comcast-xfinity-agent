"""A flow that refuses for more than one reason must say the right one.

`escalate(condition=...)` refuses a hand-off, and `declined_say` explains why. Both
shapes it had were single-voiced: one line, or a LADDER indexed by how many times the
request had been refused. Neither is indexed by WHAT refused it.

Measured on an agent that refuses a live agent in two states — one where a hand-off
cannot help at all, and one where it is merely too early because a diagnostic job is
still running. The two refusals need opposite words: the first is final and points the
caller elsewhere, the second is "hold on, we're nearly there". With one field the author
must either pick one sentence vague enough to cover both, or reach outside the DAG and
derive the wording in a callback — which puts spoken copy somewhere the validator, the
linter and the offline oracles cannot see it.

So `declined_say` also takes a list of REASONS: `{"when": <condition>, "say": ...}`
entries evaluated in order, first match wins, `say` may itself be a ladder, and an entry
with no `when` is the catch-all. A list is read as reasons only when it actually carries
one, so every existing ladder is byte-identical.
"""

from __future__ import annotations

import pytest

import flows
from flows.engine import loader as fb

# Two reasons to refuse, in the shape the primitive exists for: one state where the
# hand-off is refused for good, and one where it is only refused for now.
BLOCKED = "No one on this line can help while that is the case."
HOLD = ["Let's wait for the checks to finish so we know what to tell them."]
SAY = "Putting you through now."


def _two_reason_flow(declined, condition=None):
  f = flows.Flow("c", root_agent="a")
  f.add(flows.user_slot("topic", ask="What can I help with?"))
  f.add(flows.event_slot("blocker"))
  f.add(flows.event_slot("checks_done"))
  f.set("escalate", flows.escalate(
      say=SAY, declined_say=declined,
      condition=condition or {"all": [{"slot": "blocker", "filled": False},
                                      {"slot": "checks_done", "filled": True}]}))
  app = flows.App(root_flow=f, app_display_name="t")
  return app.root_flow.to_config(), app


def _ask(engine, config, sm, n):
  sm.setdefault("pending", {})["escalate"] = True
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "", "scanned_user_text": "",
      "is_inactivity": False, "event_data": {}, "config_id": "c", "n_user_turns": n,
  })["action"]


def _drive(config, state, asks=1):
  """Ask for a human `asks` times against one seeded state; return what was said."""
  engine, sm = fb.load_engine(), fb.seed_sm(config)
  sm["filled"], sm["pending"] = dict(state), {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "c"
    sm["filled"][gate] = "c"
  return [(_ask(engine, config, sm, i).get("message") or "")
          for i in range(1, asks + 1)], sm


REASONS = [{"when": {"slot": "blocker", "filled": True}, "say": BLOCKED},
           {"say": HOLD}]


# ── the reason decides the wording ───────────────────────────────────────────

def test_each_reason_speaks_its_own_line():
  config, _ = _two_reason_flow(REASONS)
  blocked, _ = _drive(config, {"blocker": "yes", "checks_done": "true"})
  waiting, _ = _drive(config, {})
  assert blocked == [BLOCKED]
  assert waiting == [HOLD[0]]


def test_the_first_matching_reason_wins():
  """Both hold at once: order in the list is the precedence, as it is in a condition."""
  config, _ = _two_reason_flow(REASONS)
  said, _ = _drive(config, {"blocker": "yes"})
  assert said == [BLOCKED]


def test_the_catch_all_answers_a_refusal_no_reason_names():
  """The entry with no `when` is what stops a refusal going silent.

  Here every named reason is absent — the block refuses for a reason the author did
  not enumerate — and the caller still gets an answer.
  """
  config, _ = _two_reason_flow(
      REASONS, condition={"slot": "topic", "eq": "__never__"})
  said, _ = _drive(config, {"checks_done": "true"})
  assert said == [HOLD[0]]


def test_a_reason_may_carry_a_ladder_of_its_own():
  """Repeated refusals for the SAME reason must not repeat one sentence."""
  config, _ = _two_reason_flow(
      [{"when": {"slot": "checks_done", "filled": False},
        "say": ["Hold on, nearly there.", "Still waiting on those checks."]},
       {"say": BLOCKED}])
  said, _ = _drive(config, {}, asks=4)
  # Clamped to the last line, exactly as a plain ladder is: a refusal is an answer to a
  # direct question, so it may not drain to silence.
  assert said == ["Hold on, nearly there.", "Still waiting on those checks.",
                  "Still waiting on those checks.", "Still waiting on those checks."]


def test_the_gate_still_opens_when_the_state_moves():
  """The whole point of holding rather than refusing: it ends."""
  config, _ = _two_reason_flow(REASONS)
  said, _ = _drive(config, {"checks_done": "true"})
  assert said == [SAY]


def test_a_bare_string_is_a_catch_all_reason():
  config, _ = _two_reason_flow(
      [{"when": {"slot": "blocker", "filled": True}, "say": BLOCKED}, "Not just yet."])
  said, _ = _drive(config, {})
  assert said == ["Not just yet."]


def test_no_reason_matching_is_silent_rather_than_wrong():
  """Better to say nothing than to explain a refusal with the wrong reason.

  Same outcome as a block with no `declined_say` at all: the request is dropped and
  the flow carries on with its own open question, rather than the caller being given
  an explanation that does not apply to their state. The validator warns about this
  shape; the engine still has to behave when it reaches production anyway.
  """
  config, _ = _two_reason_flow(
      [{"when": {"slot": "blocker", "filled": True}, "say": BLOCKED}],
      condition={"slot": "topic", "eq": "__never__"})
  said, sm = _drive(config, {})
  assert BLOCKED not in said[0] and SAY not in said[0]
  # Refused all the same — the request is dropped and counted, only unexplained.
  assert sm["filled"]["escalate_declined"] == 1


def test_an_unevaluable_when_falls_through_to_the_next_reason():
  """Unlike the block's own condition, a broken `when` only chooses wording.

  `condition` fails OPEN, because a broken gate must not swallow a request for a human.
  A broken `when` cannot swallow anything — the refusal has already happened — so the
  honest fallback is the next reason down rather than an explanation that may not hold.
  """
  config, _ = _two_reason_flow(
      [{"when": {"slot": "blocker", "bogus_op": 1}, "say": BLOCKED}, "Not just yet."])
  said, _ = _drive(config, {"blocker": "yes"})
  assert said == ["Not just yet."]


# ── the old shapes are untouched ─────────────────────────────────────────────

def test_a_plain_ladder_is_unchanged():
  config, _ = _two_reason_flow(["First.", "Second."])
  said, _ = _drive(config, {}, asks=3)
  assert said == ["First.", "Second.", "Second."]


def test_a_plain_string_is_unchanged():
  config, _ = _two_reason_flow("Not right now.")
  said, _ = _drive(config, {}, asks=2)
  assert said == ["Not right now.", "Not right now."]


# ── what the author is stopped from writing ──────────────────────────────────

def _validate(declined):
  config, app = _two_reason_flow(declined)
  return flows.validate_app(app)


def test_an_entry_after_a_catch_all_is_rejected():
  """It can never be reached, and it is the most specific wording the author has."""
  with pytest.raises(ValueError, match="never be reached"):
    flows.escalate(say=SAY, condition={"slot": "topic", "eq": "x"},
                   declined_say=["Anything.",
                                 {"when": {"slot": "blocker", "filled": True},
                                  "say": BLOCKED}])


def test_a_reason_with_no_words_is_rejected():
  with pytest.raises(ValueError, match="no `say`"):
    flows.escalate(say=SAY, condition={"slot": "topic", "eq": "x"},
                   declined_say=[{"when": {"slot": "blocker", "filled": True}}])


def test_an_empty_ladder_inside_a_reason_is_rejected():
  with pytest.raises(ValueError, match="no `say`"):
    flows.escalate(say=SAY, condition={"slot": "topic", "eq": "x"},
                   declined_say=[{"when": {"slot": "blocker", "filled": True},
                                  "say": []}])


def test_the_validator_catches_a_typo_in_a_reason_condition():
  """A `when` on a slot nobody declared never matches, and the caller hears the
  NEXT reason down — the wrong explanation, delivered with confidence."""
  errors, _ = _validate([{"when": {"slot": "no_such_slot", "eq": "x"}, "say": BLOCKED},
                         "Not just yet."])
  assert any("no_such_slot" in e for e in errors), errors


def test_the_validator_warns_when_nothing_can_catch_a_refusal():
  errors, warnings = _validate(
      [{"when": {"slot": "blocker", "filled": True}, "say": BLOCKED}])
  assert not errors, errors
  assert any("catch-all" in w for w in warnings), warnings


def test_the_supported_shape_is_clean():
  errors, warnings = _validate(REASONS)
  assert errors == [], errors
  assert not any("declined_say" in w for w in warnings), warnings
