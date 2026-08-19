"""An `ask` may be a LIST: one wording per re-ask.

A question the caller does not answer is asked again. As a fixed string it is asked
again word for word, and the closing "anything else?" question — usually the last open
slot, and therefore the flow's idle prompt — repeats for the rest of the call. That
reads as the agent not listening, and it is the single most common complaint about a
slot-filling agent's tone.

`reprompts` does not cover it: those are indexed by the validation-retry count and fire
only when a value was offered and rejected. An utterance the flow cannot use at all
produces no value, no error, and no retry.

Drives the real engine through the offline loader, like `test_intent_switch.py`.
"""

from __future__ import annotations

import flows
from flows.engine import loader as fb


RUNGS = [
    "Anything else I can help you with?",
    "Was there anything else, or shall I let you go?",
    "I can look at your billing, your plan, or your equipment — which would help most?",
]


def _config(ask=None):
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("anything_else", ask=ask if ask is not None else RUNGS))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert errors == [], errors
  return app.root_flow.to_config()


def _sm(config):
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "j"
    sm["filled"][gate] = "j"
  return sm


def _turn(engine, config, sm, text, n):
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False, "event_data": {},
      "config_id": "j", "n_user_turns": n,
  })["action"]


def _spoken(action):
  parts = [action.get("message") or ""]
  for part in action.get("response") or []:
    if isinstance(part, dict) and part.get("type") == "text":
      parts.append(part.get("text") or "")
  return " ".join(p for p in parts if p).strip()


def _ask_over(turns, ask=None):
  """The question spoken on each of `turns` consecutive unanswered caller turns."""
  config = _config(ask)
  engine = fb.load_engine()
  sm = _sm(config)
  return [_spoken(_turn(engine, config, sm, "hmm", n))
          for n in range(1, turns + 1)]


def test_each_re_ask_uses_the_next_rung():
  assert _ask_over(3) == RUNGS


def test_the_ladder_clamps_to_its_last_rung():
  """It must not drain to silence. This slot is the flow's idle prompt, so running out
  would leave the turn empty and the model would invent something to fill it."""
  spoken = _ask_over(6)
  assert spoken[2:] == [RUNGS[-1]] * 4
  assert all(spoken), "a rung must never be empty"


def test_a_plain_string_ask_is_unchanged():
  """The default, and what every existing agent relies on."""
  assert _ask_over(3, ask="Anything else?") == ["Anything else?"] * 3


def test_every_pass_of_one_turn_resolves_to_the_same_rung():
  """One caller turn drives several engine passes when a tool fires, and each
  re-derives the question. Counting passes instead of turns would skip rungs — and
  would make two passes of the SAME turn say different things, which is how a caller
  hears one question and the transcript records another."""
  config = _config()
  engine = fb.load_engine()
  sm = _sm(config)
  said = {_spoken(_turn(engine, config, sm, "hmm", 1)) for _ in range(4)}
  assert said == {RUNGS[0]}, f"passes disagreed within one turn: {said}"
  # ...and the next turn still advances exactly one rung.
  assert _spoken(_turn(engine, config, sm, "hmm", 2)) == RUNGS[1]


def test_answering_the_question_ends_the_ladder():
  """The ladder exists for UNANSWERED turns; a captured value retires the slot."""
  config = _config()
  engine = fb.load_engine()
  sm = _sm(config)
  _turn(engine, config, sm, "hmm", 1)
  setter = next(s["setter"] for s in config["slots"] if s["name"] == "anything_else")
  sm.update(fb.run_intake(setter, {"stored": True, "value": "no"}, sm)["sm"])
  action = _turn(engine, config, sm, "", 2)
  assert sm["filled"]["anything_else"] == "no"
  assert _spoken(action) not in RUNGS, "a filled slot must not be re-asked"


def test_an_empty_ladder_is_an_authoring_error():
  """Not a silent no-question: the slot would be asked with an empty string and the
  model would improvise a question nobody wrote. Omit `ask` to leave a slot unasked."""
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("anything_else", ask=[]))
  errors, _ = flows.validate_app(flows.App(root_flow=f, app_display_name="t"))
  assert any("empty `ask` ladder" in e for e in errors), errors


def test_a_blank_rung_is_an_authoring_error():
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("anything_else", ask=["Anything else?", "   "]))
  errors, _ = flows.validate_app(flows.App(root_flow=f, app_display_name="t"))
  assert any("rung 1" in e for e in errors), errors


def test_derived_reprompts_quote_the_first_rung():
  """`user_slot` builds its no-match reprompts from the question. With a ladder it has
  several, and the one the caller heard FIRST is the one to quote back."""
  config = _config()
  slot = config["slots"][0]
  assert RUNGS[0] in slot["validation"]["reprompts"][0]


def test_an_intent_slot_takes_a_ladder_too():
  """The closing question is usually an intent slot (yes / no / another request), so
  the ladder has to work there — and the validator must not choke floor-ing a list."""
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot(
      "anything_else", {"done": [r"\bno\b"], "more": [r"\byes\b"]}, ask=RUNGS))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert errors == [], errors
  assert app.root_flow.to_config()["slots"][0]["ask"] == RUNGS


# --- a rung is spent only when the caller HEARS it ---------------------------

SILENCED = "close"


def _suppressible_config():
  """A ladder that a later-firing announce can silence mid-turn.

  This is the shape that exposed the bug, reduced: the closing question is gated to
  stay quiet while an offer is outstanding, and the offer announce fires on the same
  pass that derives the question. So the turn works out that the close is next, then
  says something else instead.
  """
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("topic", ask="Topic?"))
  f.add(flows.intent_slot("gripe", {"yes": [r"\bexpensive\b"]}, passive=True,
                          requires=["topic"]))
  f.add(flows.intent_slot("accepts", {"yes": [r"\byes\b"]}, passive=True,
                          requires=["topic"]))
  f.add(flows.announce("offer", ["Here is an offer. Would you like it?"],
                       requires=["topic", "gripe"],
                       condition={"slot": "gripe", "eq": "yes"}))
  f.add(flows.user_slot(
      SILENCED, ask=RUNGS, requires=["topic"],
      # Stay quiet while the offer is outstanding — the caller has a question in front
      # of them already.
      condition={"not": {"all": [{"slot": "offer", "filled": True},
                                 {"slot": "accepts", "filled": False}]}}))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert errors == [], errors
  return app.root_flow.to_config()


def test_a_rung_silenced_mid_turn_is_not_spent():
  """The regression. Deriving the question is not asking it.

  Turn 2 works out that the closing question is next — which is what used to spend a
  rung — and then the offer announce takes the turn, so the caller never hears it. If
  that rung is spent, the caller who heard rung one next hears rung THREE: the
  anti-loop menu, arriving as if from nowhere, at someone who answered everything.
  """
  config = _suppressible_config()
  engine = fb.load_engine()
  sm = _sm(config)
  sm["filled"]["topic"] = "x"

  first = _spoken(_turn(engine, config, sm, "hm", 1))
  assert first == RUNGS[0]

  offered = _spoken(_turn(engine, config, sm, "that's expensive", 2))
  assert "Here is an offer" in offered
  assert RUNGS[1] not in offered, "the close was silenced this turn — nothing to spend"

  # Accept the offer so the close is reachable again.
  setter = next(s["setter"] for s in config["slots"] if s["name"] == "accepts")
  sm.update(fb.run_intake(setter, {"stored": True, "value": "yes"}, sm)["sm"])
  assert _spoken(_turn(engine, config, sm, "yes", 3)) == RUNGS[1], (
      "the caller heard rung one, so rung TWO is next — not the menu")


def test_a_rung_is_spent_once_however_many_passes_the_turn_takes():
  """The other half of the same invariant, kept honest: emission must not double-count
  when one caller turn drives several engine passes."""
  config = _config()
  engine = fb.load_engine()
  sm = _sm(config)
  for _ in range(3):
    assert _spoken(_turn(engine, config, sm, "hm", 1)) == RUNGS[0]
  assert _spoken(_turn(engine, config, sm, "hm", 2)) == RUNGS[1]


def test_a_rung_carrying_a_placeholder_is_spent():
  """The staged rung must be matched against what was SAID, not the template.

  The commit test is a substring of the spoken turn, so a rung written as
  "Ask about {topic}?" never matched the resolved "Ask about billing?" and was never
  spent — the ladder froze on rung one for the life of the call. That is worse than
  the skip the staging exists to prevent: a ladder that cannot advance is just a fixed
  string with extra machinery.
  """
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("topic", ask="Topic?"))
  f.add(flows.user_slot("subtopic", requires=["topic"],
                        ask=["Ask about {topic}?", "Really ask {topic}?"]))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert errors == [], errors
  config = app.root_flow.to_config()

  engine = fb.load_engine()
  sm = _sm(config)
  sm["filled"]["topic"] = "billing"

  assert _spoken(_turn(engine, config, sm, "hm", 1)) == "Ask about billing?"
  assert _spoken(_turn(engine, config, sm, "hm", 2)) == "Really ask billing?"
  # ...and it still clamps rather than running out.
  assert _spoken(_turn(engine, config, sm, "hm", 3)) == "Really ask billing?"


def test_a_placeholder_rung_still_advances_only_once_per_turn():
  """The resolution fix must not reintroduce double-counting across passes."""
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("topic", ask="Topic?"))
  f.add(flows.user_slot("subtopic", requires=["topic"],
                        ask=["First {topic}?", "Second {topic}?", "Third {topic}?"]))
  app = flows.App(root_flow=f, app_display_name="t")
  assert flows.validate_app(app)[0] == []
  config = app.root_flow.to_config()

  engine = fb.load_engine()
  sm = _sm(config)
  sm["filled"]["topic"] = "plans"
  for _ in range(3):
    assert _spoken(_turn(engine, config, sm, "hm", 1)) == "First plans?"
  assert _spoken(_turn(engine, config, sm, "hm", 2)) == "Second plans?"
