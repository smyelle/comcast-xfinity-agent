"""Two silent-hang defects in the executor path, both found in a live migration.

Both were reproduced by EXECUTING the production fork
(`elili_equifax_slotfilling/tools/…`) and this engine side by side on the same input;
the fork has both defects too, so these are deliberate divergences from it, not ports.

  * D4 — a PARTIAL task-output set filled nothing. `_intake_executor` mapped outputs
    all-or-nothing (`if all(k in response_data ...)`), so a tool that legitimately
    returns a subset — a KBA generator declaring question_1..4 and returning as many as
    the bureau produced — filled NO slot at all, not even the keys it did return. The
    task had succeeded, so nothing re-fired and nothing escalated; the caller heard
    silence for the rest of the call.

  * D19 — a task-failure RETRY stalled. The retry branch spoke `retry_say` and ended the
    turn, and the copy that ships with it ("Let me check your file again") invites no
    answer, so the flow only resumed if the caller happened to speak anyway. The task
    stayed fire-eligible the whole time — the intent was always to run it again.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_partial_outputs_and_task_retry.py
"""

from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)


# --------------------------------------------------------------------------- #
# D4 — partial executor outputs
# --------------------------------------------------------------------------- #

KBA_CFG = {
    "config_id": "kba",
    "slots": [
        {"name": "ssn", "source": "user", "ask": "SSN?", "setter": "set_pii"},
        {"name": "q1", "source": "system"},
        {"name": "q2", "source": "system"},
        {"name": "q3", "source": "system"},
        {"name": "q4", "source": "system"},
        {"name": "question_count", "source": "system"},
        {"name": "transaction_id", "source": "system"},
    ],
    "tasks": [{
        "name": "generate_kba", "tool": "generate_kba", "inputs": ["ssn"],
        "requires": ["ssn"], "success_check": "success",
        "outputs": {"question_1": "q1", "question_2": "q2",
                    "question_3": "q3", "question_4": "q4",
                    "question_count": "question_count",
                    "transaction_id_kba": "transaction_id"},
    }],
}


def _intake_kba(result):
  sm = fb.seed_sm(copy.deepcopy(KBA_CFG))
  sm["filled"] = {"ssn": "123456789"}
  sm["pending"] = {}
  return fb.run_intake("generate_kba", result, sm)["sm"]


def test_a_partial_output_set_fills_the_keys_it_returned():
  """REGRESSION: two questions back used to fill NOTHING and hang the call."""
  sm = _intake_kba({
      "success": True,
      "question_1": "Which street?", "question_2": "Which employer?",
      "question_count": 2, "transaction_id_kba": "TX-1",
  })
  assert sm["filled"] == {
      "ssn": "123456789", "q1": "Which street?", "q2": "Which employer?",
      "question_count": 2, "transaction_id": "TX-1",
  }
  # The absent ones stay unfilled — as if never declared, so the DAG's own conditions
  # (`question_count >= 3`) decide what to ask.
  assert "q3" not in sm["filled"] and "q4" not in sm["filled"]


def test_the_partial_map_is_logged():
  """A slot empty because the tool did not return it is the first thing you need to
  know when a downstream condition does not fire."""
  sm = _intake_kba({"success": True, "question_1": "Q?", "question_count": 1})
  tags = [e.get("tag") for e in sm.get("_log", [])]
  assert "task_outputs_partial" in tags
  entry = next(e for e in sm["_log"] if e.get("tag") == "task_outputs_partial")
  assert entry["data"]["mapped"] == 2
  assert set(entry["data"]["absent"]) == {
      "question_2", "question_3", "question_4", "transaction_id_kba"}


def test_a_full_output_set_is_unchanged():
  sm = _intake_kba({
      "success": True, "question_1": "a", "question_2": "b", "question_3": "c",
      "question_4": "d", "question_count": 4, "transaction_id_kba": "TX-1",
  })
  assert sm["filled"]["q4"] == "d" and sm["filled"]["question_count"] == 4
  assert not [e for e in sm.get("_log", []) if e.get("tag") == "task_outputs_partial"]


def test_a_failed_task_still_maps_nothing():
  """Only SUCCESS maps outputs; a failure has its own ladder."""
  sm = _intake_kba({"success": False, "question_1": "Q?", "question_count": 1})
  assert sm["filled"] == {"ssn": "123456789"}


# --------------------------------------------------------------------------- #
# D19 — the task-failure retry
# --------------------------------------------------------------------------- #

def _retry_cfg(**on_failure):
  base = {"retry_say": "Let me check your file again.", "max_retries": 2,
          "on_exhaust": {"say": "I can't reach your file right now."}}
  base.update(on_failure)
  return {
      "config_id": "retry",
      "slots": [{"name": "ready", "source": "user", "ask": "Ready?",
                 "setter": "set_ready"},
                {"name": "status", "source": "system"}],
      "tasks": [{"name": "check", "tool": "check_eligibility", "inputs": ["ready"],
                 "requires": ["ready"], "success_check": "success",
                 "outputs": {"status": "status"}, "on_failure": base}],
  }


def _fail_once(cfg):
  """Fire the task, fail it, and return the engine action for that same turn."""
  sm = fb.seed_sm(copy.deepcopy(cfg))
  sm["filled"] = {"ready": "yes"}
  sm["pending"] = {}
  fired = fb.run_engine(cfg, sm, config_id="retry")
  assert fired["action"]["function_call"]["name"] == "check_eligibility"
  sm = fb.run_intake("check_eligibility", {"success": False}, fired["sm"])["sm"]
  return fb.run_engine(cfg, sm, config_id="retry")["action"]


def test_a_retry_re_fires_the_task_on_the_same_turn():
  """REGRESSION: it used to speak and WAIT for a caller turn nothing invited."""
  action = _fail_once(_retry_cfg())
  assert action["message"] == "Let me check your file again."
  assert action["function_call"] == {"name": "check_eligibility",
                                     "args": {"ready": "yes"}}
  # The tool must be callable on the turn we ask for it.
  assert "check_eligibility" not in action["hide_tools"]


def test_the_retry_is_bounded_by_max_retries():
  """`max_retries: 2` = one re-fire, then exhaust. The bound is load-bearing: an
  unbounded version re-dispatches a backend action on a loop."""
  cfg = _retry_cfg()
  sm = fb.seed_sm(copy.deepcopy(cfg))
  sm["filled"] = {"ready": "yes"}
  sm["pending"] = {}
  fires, said = 0, []
  res = fb.run_engine(cfg, sm, config_id="retry")
  for _ in range(6):  # a cap, so a runaway fails the test instead of hanging it
    action, sm = res["action"], res["sm"]
    if action.get("message"):
      said.append(action["message"])
    if not action.get("function_call"):
      break
    fires += 1
    sm = fb.run_intake("check_eligibility", {"success": False}, sm)["sm"]
    res = fb.run_engine(cfg, sm, config_id="retry")
  assert fires == 2                       # the first fire + exactly one re-fire
  assert said == ["Let me check your file again.",
                  "I can't reach your file right now."]


def test_an_explicit_on_failure_then_still_wins():
  """`on_failure.then` is the author saying what the retry should DO; it is not
  overridden by the re-fire."""
  action = _fail_once(_retry_cfg(then={"tool": "send_otp", "args": {}}))
  assert action["function_call"]["name"] == "send_otp"


def test_clear_slots_opts_out_because_the_retry_needs_the_caller():
  """A retry that drops slots is asking the CALLER again — re-running the same call
  with the same inputs would talk over the question."""
  action = _fail_once(_retry_cfg(clear_slots=["ready"]))
  assert "function_call" not in action


def test_refire_false_opts_out_explicitly():
  """Mirrors `on_exhaust.escalate: False` — one opt-out idiom, not two."""
  action = _fail_once(_retry_cfg(refire=False))
  assert "function_call" not in action
  assert action["message"] == "Let me check your file again."


def test_the_exhaust_branch_is_untouched():
  cfg = _retry_cfg(max_retries=1)
  action = _fail_once(cfg)
  assert action["message"] == "I can't reach your file right now."
  assert "function_call" not in action
