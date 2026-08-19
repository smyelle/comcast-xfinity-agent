"""A turn that collects nothing must not leave the setter surface callable.

The gate turn (no flow chosen yet) and the terminal turn (complete / zombie /
escalated) both render a directive without running slot-filling. They applied
`_hiding_policy` alone, which decides FLOW-CONTROL visibility and never touches
setters — so on a turn before any flow started, or after one had already finished,
every setter in the app sat callable in the function-calling schema while the prompt
advertised none of them.

That combination is a lure. A tool the model can see but the prompt never mentions
invites it to find a reason to call one, and the engine then rejects the value because
the slot is inactive — leaving the caller at a dead end after a confident-sounding
sentence.
"""

from __future__ import annotations

import flows
from flows.authoring import build
from flows.engine import loader as fb


BILLING_ONLY = ("card_number", "pin")
TECH_ONLY = ("router_serial", "street", "zip")


def _config():
  """Two journeys, so most setters are inactive whichever one the caller picks."""
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot(
      "journey", {"billing": [r"\bbill\b"], "tech": [r"\binternet\b"]},
      ask="What can I help with?"))
  for n in BILLING_ONLY + TECH_ONLY:
    f.add(flows.user_slot(
        n, ask=f"What is your {n}?", requires=["journey"],
        condition={"slot": "journey",
                   "eq": "billing" if n in BILLING_ONLY else "tech"}))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert errors == [], errors
  return app.root_flow.to_config()


def _setters(config):
  return {s["setter"] for s in config["slots"] if s.get("setter")}


def _sm(config):
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  return sm


def _turn(engine, config, sm, text, n):
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False, "event_data": {},
      "config_id": "j", "n_user_turns": n,
  })["action"]


def test_a_terminal_turn_hides_every_setter():
  config = _config()
  engine = fb.load_engine()
  sm = _sm(config)
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "j"
    sm["filled"][gate] = "j"
  sm["status"] = "complete"

  hidden = set(_turn(engine, config, sm, "anything", 2).get("hide_tools") or [])
  left = _setters(config) - hidden
  assert not left, f"callable on a finished flow: {sorted(left)}"


def test_a_terminal_turn_still_lets_the_session_end():
  """The fix must not hide the tools the terminal phase exists to offer."""
  config = _config()
  engine = fb.load_engine()
  sm = _sm(config)
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "j"
    sm["filled"][gate] = "j"
  sm["status"] = "complete"

  hidden = set(_turn(engine, config, sm, "bye", 2).get("hide_tools") or [])
  assert "end_session" not in hidden


def test_the_gate_phase_hides_every_setter_but_keeps_the_bootstrap_tool():
  """Nothing has been chosen yet, so no slot is collectable — but the tool that
  chooses the flow is the whole point of the turn.

  Tested against the phase helper directly. A single-flow app SELF-SEEDS its auto-gate,
  so the gate branch is only reached by a router/host config; asserting through a
  hand-built single-flow session would pass for the wrong reason (the branch is never
  taken) and could not fail if the helper regressed.
  """
  f = flows.Flow("collect", root_agent="Agent")
  f.add(flows.user_slot("acct", ask="Account number?"))
  f.add(flows.user_slot("pin", ask="PIN?"))
  app = flows.App(root_flow=f, app_display_name="X")
  config = build._assemble(app)[0][app.config_id]  # noqa: SLF001
  bootstrap = (config.get("bootstrap") or {}).get("tool")

  engine = fb.load_engine()
  hidden = set(engine._phase_hidden_tools({}, config, "gate"))  # noqa: SLF001
  assert _setters(config) - {bootstrap} <= hidden, "a gate turn collects nothing"
  if bootstrap:
    assert bootstrap not in hidden, "the gate keeps its bootstrap tool"


def test_the_phase_helper_hides_task_tools_too():
  """Executor tools are the engine's to fire, never the model's."""
  f = flows.Flow("collect", root_agent="Agent")
  f.add(flows.user_slot("acct", ask="Account number?"))
  f.task(flows.task("look_up", "do_lookup", ["acct"], "res", out_key="ok"))
  f.add(flows.result_slot("res", "look_up"))
  app = flows.App(root_flow=f, app_display_name="X")
  config = build._assemble(app)[0][app.config_id]  # noqa: SLF001

  engine = fb.load_engine()
  hidden = set(engine._phase_hidden_tools({}, config, "terminal"))  # noqa: SLF001
  assert "do_lookup" in hidden


def test_an_in_flow_turn_still_exposes_the_open_slot_setter():
  """The guard is scoped to phases that collect nothing. A normal collection turn must
  keep offering the setter for the question it is asking, or nothing can be answered."""
  config = _config()
  engine = fb.load_engine()
  sm = _sm(config)
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "j"
    sm["filled"][gate] = "j"

  _turn(engine, config, sm, "my bill is wrong", 1)
  assert sm["filled"]["journey"] == "billing"
  hidden = set(_turn(engine, config, sm, "", 2).get("hide_tools") or [])
  card = next(s["setter"] for s in config["slots"] if s["name"] == "card_number")
  assert card not in hidden, "the live journey's setter must stay callable"
  # ...and the other journey's setters must not be.
  serial = next(s["setter"] for s in config["slots"] if s["name"] == "router_serial")
  assert serial in hidden
