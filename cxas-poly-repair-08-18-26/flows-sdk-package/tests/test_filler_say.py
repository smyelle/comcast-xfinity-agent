"""`filler_say` covers the wait, whether the wait is a tool or the model.

A task's filler rides the turn that calls the tool. But a turn handed to the MODEL had
no filler at all, because the only way to speak ahead of the model is a preempt and a
text-only preempt ends the turn (ces-probes 26) — the caller would have heard "one
moment" and then had to ask again. Marking that preempt `partial` speaks the line and
keeps the floor (ces-probes 57), so the same authored field now covers both waits and
the author never has to know which one they are facing.

Two things follow, and both are tested here: the line comes from ONE resolver so a pool
rotates on either delivery, and silence is an ordinary member of that pool rather than a
separate knob.

Drives the real engine through the offline loader, like `test_ask_ladder.py`.
"""

from __future__ import annotations

import flows
from flows.engine import loader as fb


POOL = ["Let me take a look.", "One sec.", "Just a moment."]


def _config(slot_filler=None, flow_filler=None):
  policy = {"filler_say": flow_filler} if flow_filler is not None else {}
  f = flows.Flow("j", root_agent="a", **policy)
  f.add(flows.user_slot("problem", "What's going on?", filler_say=slot_filler))
  f.add(flows.user_slot("detail", "Anything else?"))
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


def _armed_over(turns, slot_filler=None, flow_filler=None):
  """What the filler is on each of `turns` consecutive caller turns."""
  config = _config(slot_filler, flow_filler)
  engine = fb.load_engine()
  sm = _sm(config)
  return [_turn(engine, config, sm, "hmm", n).get("filler_partial")
          for n in range(1, turns + 1)]


# --- the resolver -----------------------------------------------------------


def _pick(spec, filled=None, sm=None, turn=1):
  engine = fb.load_engine()
  state = sm if sm is not None else {"_flow_id": "j", "_turn_n": turn}
  return engine._pick_filler(state, spec, filled or {})


def test_a_bare_string_is_always_spoken():
  """The shape every existing agent authored. It must not start skipping turns."""
  assert [_pick("One moment.", turn=t) for t in range(1, 6)] == ["One moment."] * 5


def test_no_filler_authored_stays_silent():
  assert _pick(None) is None
  assert _pick([]) is None


def test_a_pool_of_only_none_never_speaks():
  """The explicit way to author "never fill this wait" without deleting the field."""
  assert [_pick([None, None], turn=t) for t in range(1, 5)] == [None] * 4


def test_a_pool_rotates_across_turns():
  spoken = [_pick(POOL, turn=t) for t in range(1, 9)]
  assert set(spoken) <= set(POOL)
  assert len(set(spoken)) > 1, f"a pool that never varies is the bug: {spoken}"


def test_the_same_salt_and_turn_pick_the_same_line():
  """Deterministic given the session salt, so a pick is reproducible when you hold the
  session fixed — what varies is the salt, which is minted per caller."""
  sm = {"_filler_salt": "abc123", "_turn_n": 4}
  assert _pick(POOL, sm=dict(sm)) == _pick(POOL, sm=dict(sm))


def test_different_callers_hear_different_lines():
  """The whole point of a pool. Seeded from the config alone every caller heard the
  SAME line, and a flow with one filler turn never rotated at all -- the pool looked
  like it worked because rotation was only ever checked across turns, not callers."""
  picks = {str(_pick(POOL, sm={"_turn_n": 1})) for _ in range(40)}
  assert len(picks) > 1, f"every caller heard the same line: {picks}"


def test_the_salt_is_minted_once_and_kept():
  sm = {"_turn_n": 1}
  _pick(POOL, sm=sm)
  first = sm["_filler_salt"]
  _pick(POOL, sm=sm)
  assert sm["_filler_salt"] == first


def test_a_pool_does_not_repeat_the_previous_line():
  sm = {"_flow_id": "j", "_turn_n": 1}
  first = _pick(POOL, sm=sm)
  sm["_turn_n"] = 2
  assert _pick(POOL, sm=sm) != first


def test_a_two_entry_pool_may_repeat():
  """Excluding the previous pick from ["One sec.", None] would strictly ALTERNATE,
  which is a pattern rather than variety — so the guard stands down when dropping the
  last line would leave no real choice."""
  sm = {"_flow_id": "j", "_turn_n": 0}
  seen = []
  for t in range(1, 13):
    sm["_turn_n"] = t
    seen.append(_pick(["One sec.", None], sm=sm))
  assert any(a == b for a, b in zip(seen, seen[1:])), (
      f"a two-entry pool alternated perfectly, which sounds mechanical: {seen}")


def test_a_line_may_reference_a_filled_slot():
  assert _pick("Let me pull up order {order_id}.",
               filled={"order_id": "A12"}) == "Let me pull up order A12."


def test_an_unfilled_placeholder_is_skipped_not_spoken_raw():
  """The old inline `except KeyError: pass` left the braces in, so the caller heard
  "order open-brace order id close-brace". A pool degrades to a line that resolves."""
  assert _pick(["Let me pull up order {order_id}.", "One moment."],
               filled={}) == "One moment."


def test_a_lone_unresolvable_line_degrades_to_silence():
  """Silence beats speaking markup at the caller."""
  assert _pick("Let me pull up order {order_id}.", filled={}) is None


# --- the model-turn delivery ------------------------------------------------


def test_a_slot_filler_is_armed_on_a_model_turn():
  assert _armed_over(1, slot_filler="Let me take a look.") == ["Let me take a look."]


def test_the_flow_default_covers_a_slot_that_has_none():
  assert _armed_over(1, flow_filler="One sec.") == ["One sec."]


def test_a_slot_filler_beats_the_flow_default():
  armed = _armed_over(1, slot_filler="Slot line.", flow_filler="Flow line.")
  assert armed == ["Slot line."]


def test_no_filler_authored_arms_nothing():
  """The default for every agent that exists today: the field is absent, so the turn
  is exactly the turn it always was."""
  assert _armed_over(3) == [None, None, None]


def test_it_is_armed_at_most_once_per_caller_turn():
  """It costs one of the ten reasoning passes, and re-arming on the pass that follows
  would speak the line again and loop."""
  config = _config(slot_filler="Let me take a look.")
  engine = fb.load_engine()
  sm = _sm(config)
  first = _turn(engine, config, sm, "hmm", 1).get("filler_partial")
  second = _turn(engine, config, sm, "hmm", 1).get("filler_partial")
  assert first == "Let me take a look."
  assert second is None, "the same caller turn armed a second filler"


def test_a_pool_rotates_on_the_model_delivery_too():
  spoken = [s for s in _armed_over(6, slot_filler=POOL) if s]
  assert len(set(spoken)) > 1, f"the model delivery did not rotate: {spoken}"


# --- the classify turns -----------------------------------------------------
#
# Routing and Pass-A leave the engine long before `_finalize_directive`, which used to be
# the only place a filler was armed. So the turn the caller waits LONGEST on was the one
# turn that could not cover itself: measured end of caller speech to first agent audio,
# 2.55s for an ordinary in-flow turn against 4.3-6.5s for a router turn.


def _router_config(filler_say=None):
  child = flows.Flow("billing", bootstrap={"reset_on_complete": True})
  child.add(flows.user_slot("account_number", ask="Account number?"))
  router = flows.router_flow("host", ["billing"], root_agent="Host_Agent",
                             filler_say=filler_say)
  app = flows.App(root_flow=router, extra_flows=[child], app_display_name="rt")
  errors, _ = flows.validate_app(app)
  assert errors == [], errors
  return app.root_flow.to_config()


def _router_sm(config, active=None):
  """A router turn. The gate is EMPTY unless `active` — a filled gate is the
  auto-dispatch turn, which is a different shape entirely."""
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  if active:
    sm["filled"]["active_flow"] = active
  return sm


def _router_turn(config, sm, text="I want to pay my bill", n=1):
  engine = fb.load_engine()
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False, "event_data": {},
      "config_id": "host", "n_user_turns": n,
  })["action"]


def test_the_router_turn_speaks_while_it_classifies():
  """The turn this whole thing exists for."""
  config = _router_config(filler_say="One moment.")
  action = _router_turn(config, _router_sm(config))
  assert action["tag"] == "router"
  assert action.get("filler_partial") == "One moment."


def test_a_router_with_no_filler_authored_arms_no_filler():
  # No filler_say -> no filler_partial. The turn DOES hide classify_turn_intent: this is a
  # single-agent router that DECLARES classify (so the re-entry classifier has a tool to
  # compel) but is NOT itself intent-first, and a cold router turn hides the Pass-A-only tool
  # unconditionally so the model can't volunteer it — the exact non-intent-first-router leak
  # the cold-router hide closes (hiding an undeclared tool would be a no-op).
  config = _router_config()
  action = _router_turn(config, _router_sm(config))
  assert action == {"tag": "router", "hide_tools": ["classify_turn_intent"]}


def test_the_router_filler_rotates_across_callers():
  """Every caller hearing the same opening line is what a pool is for, and the router
  line is the FIRST thing anyone hears, so a fixed one is the most audible of all."""
  config = _router_config(filler_say=POOL)
  spoken = {_router_turn(config, _router_sm(config)).get("filler_partial")
            for _ in range(40)}
  assert len(spoken) > 1, f"every caller heard the same routing line: {spoken}"


def test_the_session_start_turn_arms_nothing():
  """A filler covers a CALLER's wait. Session start is a router turn like any other but
  nobody has spoken yet, and the turn already has the welcome to deliver.

  Found live: the opening turn armed "Alright." in front of the greeting, and the call
  went on to render the platform's own error envelope twice. A holding line belongs to a
  turn somebody is waiting on.
  """
  config = _router_config(filler_say="One moment.")
  action = _router_turn(config, _router_sm(config), text="<event>session start</event>")
  assert action.get("filler_partial") is None


def test_a_turn_with_no_user_text_arms_nothing():
  config = _router_config(filler_say="One moment.")
  assert _router_turn(config, _router_sm(config), text="").get("filler_partial") is None


def test_the_auto_dispatch_turn_arms_nothing():
  """It already speaks and already carries a `function_call`, which is the shape a
  partial filler is deliberately excluded from — arming here would talk over it."""
  config = _router_config(filler_say="One moment.")
  action = _router_turn(config, _router_sm(config, active="billing"))
  assert action["tag"] == "router_auto_dispatch"
  assert action.get("filler_partial") is None


def test_the_router_speaks_its_welcome_on_the_opening_turn():
  """A router returns before the announce cascade, so `welcome_slot` used to be declared
  and then never read — and the first thing a caller heard was the model improvising."""
  child = flows.Flow("billing", bootstrap={"reset_on_complete": True})
  child.add(flows.user_slot("account_number", ask="Account number?"))
  router = flows.router_flow("host", ["billing"], root_agent="Host_Agent",
                             filler_say="One moment.")
  router.add(flows.announce("welcome", ["Thanks for calling. How can I help?"],
                            shared=True, preempt=True))
  router.set("bootstrap", dict(router.to_config()["bootstrap"], welcome_slot="welcome"))
  app = flows.App(root_flow=router, extra_flows=[child], app_display_name="rt")
  config = app.root_flow.to_config()

  sm = _router_sm(config)
  opening = _router_turn(config, sm, text="<event>session start</event>")
  assert opening["preempt"] is True
  assert opening["response"] == [{"type": "text",
                                  "text": "Thanks for calling. How can I help?"}]
  assert opening.get("filler_partial") is None, "the greeting IS the opening line"

  # And only once: the next turn routes, it does not greet again.
  nxt = _router_turn(config, sm, text="I want to pay my bill")
  assert nxt.get("response") is None
  assert nxt.get("filler_partial") == "One moment."


def _intent_first_config(slot_filler=None, flow_filler=None):
  """A child flow under an intent-first router: gated, and classified before it asks.

  `bootstrap.intent_first` is what makes the engine set `_intent_first` and take the
  Pass-A branch, and the branch also requires the gate to be FILLED — this flow is
  live, and the question is what the caller's utterance means for it.
  """
  config = _config(slot_filler, flow_filler)
  config["gate_slot"] = "active_flow"
  config["bootstrap"] = {"intent_first": True, "tool": "set_active_flow",
                         "slot": "active_flow"}
  return config


def _intent_first_sm(config):
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {"active_flow": "j"}, {}
  sm["active_flow"] = "j"
  return sm


def test_the_intent_first_classify_turn_speaks_too():
  """Pass A is the same silence with a different name: the model is handed a classifier
  and the caller hears nothing until it comes back."""
  config = _intent_first_config(flow_filler="One sec.")
  engine = fb.load_engine()
  action = _turn(engine, config, _intent_first_sm(config),
                 "actually, something else", 1)
  assert action["tag"] == "pass_a_classify"
  assert action.get("filler_partial") == "One sec."


def test_classifying_and_asking_in_one_caller_turn_speak_once():
  """Pass A then Pass B are two engine passes inside ONE caller turn. Arming on both
  would say "one sec" twice to somebody who only spoke once."""
  config = _intent_first_config(slot_filler="Let me take a look.",
                                flow_filler="One sec.")
  engine = fb.load_engine()
  sm = _intent_first_sm(config)
  first = _turn(engine, config, sm, "hmm", 1).get("filler_partial")
  sm["_pending_intent"] = "continue"
  second = _turn(engine, config, sm, "hmm", 1).get("filler_partial")
  assert first == "One sec."
  assert second is None, "the caller heard a second filler for one utterance"
