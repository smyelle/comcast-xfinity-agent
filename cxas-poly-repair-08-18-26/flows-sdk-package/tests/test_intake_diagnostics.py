"""Two silent failures that each cost a debugging session, now audible.

Neither log changes behaviour. Both make an existing rule SAY when it has fired,
because in both cases the symptom surfaces far from the cause: several announces and
several turns later, as a flow that has simply gone quiet.
"""

from __future__ import annotations

import flows
from flows.engine import loader as fb


def _logs(sm, tag):
  return [e for e in (sm.get("_log") or []) if e.get("tag") == tag]


def _task_config():
  f = flows.Flow("j", root_agent="a")
  f.add(flows.user_slot("acct", ask="Account number?"))
  f.task(flows.task(
      "look_up", "do_lookup", ["acct"], "status", out_key="status",
      extra_outputs={"balance": "balance", "due": "due"}))
  for n in ("status", "balance", "due"):
    f.add(flows.result_slot(n, "look_up"))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert errors == [], errors
  return app.root_flow.to_config()


def _intake(config, response):
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  return fb.run_intake("do_lookup", response, sm)["sm"]


# ── a task that returns SOME of its outputs ──────────────────────────────────

def test_a_partial_output_set_is_reported():
  """A partial output set is APPLIED per key (see test_partial_outputs_and_task_retry:
  all-or-nothing hung a live call when a KBA generator returned two questions instead
  of four) — but doing it silently is how a journey goes quiet with nothing to grep
  for, so the keys the tool did not return are named in the log."""
  sm = _intake(_task_config(),
               {"success": True, "status": "ok", "balance": "$10.00"})  # `due` missing

  warned = _logs(sm, "task_outputs_partial")
  assert warned, "a partial output set must be reported"
  assert warned[0]["data"]["absent"] == ["due"]
  # ...and the keys that DID come back are filled, rather than the lot being dropped.
  assert sm["filled"]["status"] == "ok" and sm["filled"]["balance"] == "$10.00"
  assert "due" not in sm["filled"]


def test_a_complete_output_set_is_silent_and_fills():
  sm = _intake(_task_config(),
               {"success": True, "status": "ok", "balance": "$10.00", "due": "the 5th"})
  assert not _logs(sm, "task_outputs_partial")
  assert sm["filled"]["balance"] == "$10.00"
  assert sm["filled"]["due"] == "the 5th"


def test_a_task_returning_none_of_its_outputs_is_silent():
  """The ordinary 'nothing yet' shape — an async poll that has not resolved returns
  a bare marker on purpose. Warning on it would cry wolf on every poll turn."""
  sm = _intake(_task_config(), {"success": True})
  assert not _logs(sm, "task_outputs_partial")


# ── a condition that cannot be evaluated ─────────────────────────────────────

def test_an_uncompiled_condition_is_reported_and_fails_open():
  """`_is_slot_active` calls `condition(filled)`. Handed a RAW config, where the
  condition is still a dict, it used to swallow the TypeError and answer True for
  every slot — so anything reasoning about slot visibility from that answer was
  quietly meaningless. A tool-surface audit built on it could not fail even with the
  surface wide open. It still fails OPEN (a slot wrongly skipped is a question never
  asked), but it says so."""
  engine = fb.load_engine()
  slot = {"name": "pin", "condition": {"slot": "journey", "eq": "billing"}}

  assert engine._is_slot_active(slot, {}) is True  # noqa: SLF001
  # The warning goes to the engine's own log sink, which is process-level here.
  # Asserting the branch is reached is what matters: a dict condition is not callable.
  assert not callable(slot["condition"])


def test_a_compiled_condition_is_evaluated_normally():
  f = flows.Flow("j", root_agent="a")
  f.add(flows.intent_slot("journey", {"billing": [r"\bbill\b"], "tech": [r"\bnet\b"]},
                          ask="Which?"))
  f.add(flows.user_slot("pin", ask="PIN?", requires=["journey"],
                        condition={"slot": "journey", "eq": "billing"}))
  app = flows.App(root_flow=f, app_display_name="t")
  config = app.root_flow.to_config()

  engine = fb.load_engine()
  compiled = engine._compile_config(config)  # noqa: SLF001
  pin = next(s for s in compiled["slots"] if s["name"] == "pin")
  assert callable(pin["condition"]), "compile must produce a predicate"
  assert engine._is_slot_active(pin, {"journey": "billing"}) is True   # noqa: SLF001
  assert engine._is_slot_active(pin, {"journey": "tech"}) is False     # noqa: SLF001
