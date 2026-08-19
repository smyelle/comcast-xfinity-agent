"""Two signals in one breath: `multi_fill`.

One utterance fills one intent slot. That default is right — an utterance usually
expresses one intent, and letting overlapping vocabulary set several at once makes
silent decisions the author never asked for.

But some signals genuinely travel together. "That's a lot, can you waive the fee"
carries WHAT the caller wants and HOW they feel about it, and those belong in different
slots: one drives the flow, the other picks the wording that answers them. With a single
winner the second never fills and the agent replies to a concern the caller did not
raise — and merging them into one enum only works while the signals are mutually
exclusive, which they are not.

Drives the real engine through the offline loader, like `test_intent_switch.py`.
"""

from __future__ import annotations

import flows
from flows.engine import loader as fb


REQUEST = {"waive": [r"\bwaive\b", r"\bremove\b"]}
# Disjoint from REQUEST on purpose: one phrase must not mean two things.
TONE = {"cost": [r"\bthat's a lot\b", r"\bexpensive\b"],
        "surprise": [r"\bdidn't expect\b", r"\bunexpected\b"]}


def _config(multi_fill: bool):
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("account", ask="Account number?"))
  f.add(flows.intent_slot("request", REQUEST, passive=True))
  f.add(flows.intent_slot("tone", TONE, passive=True, multi_fill=multi_fill))
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


def _say(config, sm, text, n=1):
  engine = fb.load_engine()
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False, "event_data": {},
      "config_id": "j", "n_user_turns": n,
  })["action"]


def _mid_flow(config, text):
  """Drive an opening turn, THEN the utterance under test.

  The ROUTING turn fills every `option_cues` slot the caller names, by design — an enum
  router has to be settable up front. So a one-turn test proves nothing about the
  single-winner rule: both slots fill on turn 1 whatever the flag says. The limit this
  feature lifts only exists mid-flow.
  """
  sm = _sm(config)
  _say(config, sm, "hello", 1)
  _say(config, sm, text, 2)
  return sm


def test_one_utterance_fills_both_slots_when_opted_in():
  config = _config(multi_fill=True)
  sm = _mid_flow(config, "that's a lot, can you waive the fee")
  assert sm["filled"].get("request") == "waive"
  assert sm["filled"].get("tone") == "cost", "the opted-in slot must fill too"


def test_the_default_is_still_one_slot_per_utterance():
  """Unchanged for every existing agent: without the opt-in the second slot is left to
  the ask or the model, exactly as before."""
  config = _config(multi_fill=False)
  sm = _mid_flow(config, "that's a lot, can you waive the fee")
  assert sm["filled"].get("request") == "waive"
  assert "tone" not in sm["filled"]


def test_an_opted_in_slot_still_fills_alone():
  """The flag widens when a slot MAY fill; it does not make it depend on a companion."""
  config = _config(multi_fill=True)
  sm = _mid_flow(config, "that's expensive")
  assert sm["filled"].get("tone") == "cost"
  assert "request" not in sm["filled"]


def test_an_ambiguous_match_still_fills_nothing():
  """The >=2-value guard is what makes filling several slots safe at all. An opted-in
  slot whose own cues match two values is still discarded, not guessed."""
  config = _config(multi_fill=True)
  sm = _mid_flow(config, "I didn't expect that, and that's a lot — please waive it")
  assert sm["filled"].get("request") == "waive"
  assert "tone" not in sm["filled"], "two tone values matched; neither may be chosen"


def test_cue_priority_resolves_an_opted_in_slot_instead_of_dropping_it():
  """`cue_priority` is the authored tiebreak for overlapping vocabulary. It has to apply
  when deciding WHICH slots may fill, or a slot that declares it is skipped as ambiguous
  and then never reached."""
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("account", ask="Account number?"))
  f.add(flows.intent_slot("request", REQUEST, passive=True))
  f.add(flows.intent_slot("tone", TONE, passive=True, multi_fill=True,
                          cue_priority="first"))
  app = flows.App(root_flow=f, app_display_name="t")
  assert flows.validate_app(app)[0] == []
  config = app.root_flow.to_config()

  sm = _mid_flow(config, "I didn't expect that, and that's a lot — please waive it")
  assert sm["filled"].get("request") == "waive"
  # "cost" is declared first in TONE, so it wins the tiebreak.
  assert sm["filled"].get("tone") == "cost"


def test_the_flag_reaches_the_config_and_validates():
  config = _config(multi_fill=True)
  tone = next(s for s in config["slots"] if s["name"] == "tone")
  assert tone["multi_fill"] is True
  request = next(s for s in config["slots"] if s["name"] == "request")
  assert "multi_fill" not in request, "the default must not be emitted"


def _config_ordered(tone_first: bool):
  """The same two slots, declared in either order."""
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("account", ask="Account number?"))
  pair = [("tone", TONE, True), ("request", REQUEST, False)]
  if not tone_first:
    pair.reverse()
  for name, cues, mf in pair:
    f.add(flows.intent_slot(name, cues, passive=True, multi_fill=mf))
  app = flows.App(root_flow=f, app_display_name="t")
  assert flows.validate_app(app)[0] == []
  return app.root_flow.to_config()


def test_declaration_order_does_not_change_what_fills():
  """An opted-in slot must never take the single-winner place.

  Claiming it first was order-dependent in a way nothing declared: with the
  `multi_fill` slot written ABOVE the one that drives the flow, it became the winner
  and the primary intent was dropped — the tone was recorded and the request was not,
  which is the exact failure `multi_fill` exists to prevent, reintroduced by writing
  the two slots the other way round.
  """
  said = "that's a lot, can you waive the fee"
  after_request_first = _mid_flow(_config_ordered(tone_first=False), said)["filled"]
  after_tone_first = _mid_flow(_config_ordered(tone_first=True), said)["filled"]

  for label, filled in (("request first", after_request_first),
                        ("multi_fill first", after_tone_first)):
    assert filled.get("request") == "waive", f"{label}: primary intent lost"
    assert filled.get("tone") == "cost", f"{label}: opted-in slot did not fill"
