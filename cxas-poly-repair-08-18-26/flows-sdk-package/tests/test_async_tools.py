"""Asynchronous tools, driven against the real engine.

An ASYNCHRONOUS CES tool answers twice: the call returns a platform-substituted
`{"result": "pending"}` and the real payload arrives one or more turns later as a
synthetic user turn. All of that is measured, not assumed — see
`ces-probes/probes/24-async-execution/`.

Each behavioural test is an A/B: the same task with `awaits` off and on, so the assertion
is about the difference the primitive makes rather than the fixture's particular wording.
"""

import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)

PENDING = {"result": "pending"}


_CES_GLOBALS = ("CallbackContext", "LlmRequest", "LlmResponse", "Content", "Part",
                "Tool", "tools", "ces_internal")


def _load_before_model():
  """Import the before_model callback as a module so its pure helpers are testable.

  It is CES-hosted source: the runtime injects `CallbackContext`, `LlmResponse`, `Part`
  and friends as globals. Signature annotations reference them and are evaluated at def
  time (the file deliberately omits `from __future__ import annotations`, because
  postponed annotations break CES's schema derivation), so the names must exist before
  exec. Stubs suffice — nothing here calls into them.
  """
  path = os.path.join(
      os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
      "src/flows/engine/framework/callbacks/before_model.py")
  spec = importlib.util.spec_from_file_location("_bm_under_test", path)
  mod = importlib.util.module_from_spec(spec)
  for name in _CES_GLOBALS:
    setattr(mod, name, type(name, (), {}))
  spec.loader.exec_module(mod)
  return mod


def drive(cfg, sm, text="", turn=1):
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False,
      "event_data": {}, "config_id": "t", "n_user_turns": turn,
  })


def cfg_with(awaits=None, on_failure=None):
  """A flow whose single task fires an asynchronous lookup with nothing to collect."""
  task = {
      "name": "lookup", "tool": "slow_lookup", "inputs": [],
      "outputs": {"status_msg": "status"},
      "success_check": "success", "terminal": False,
      "requires": [], "then_say": "Done: {status_msg}",
  }
  if awaits is not None:
    task["awaits"] = awaits
  if on_failure is not None:
    task["on_failure"] = on_failure
  return {
      "slots": [{"name": "status", "source": "task:lookup"}],
      "tasks": [task],
      "gate_slot": None,
  }


def fresh(cfg):
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  return sm


def land(sm, cfg, result, turn, outcome=""):
  """Deliver a tool result the way intake does, then run one engine turn.

  `outcome` is what before_model passes when the result arrived in an async completion
  envelope; empty means an ordinary synchronous result.
  """
  sm.update(fb.run_intake("slow_lookup", result, sm, outcome=outcome)["sm"])
  return drive(cfg, sm, "", turn=turn)


# --------------------------------------------------------------------------- #
# 1. The envelope parser
# --------------------------------------------------------------------------- #

def test_parses_tool_name_and_unwraps_the_result_key():
  bm = _load_before_model()
  out = bm._parse_async_completions(
      '<context>function [poll_status] completed with response '
      '{"result": {"success": true, "status_msg": "ok"}}</context>')
  assert out == [("poll_status", {"success": True, "status_msg": "ok"}, "completed")]


def test_parses_a_multi_line_pretty_printed_payload():
  """CES pretty-prints the payload, so the pattern needs DOTALL."""
  bm = _load_before_model()
  out = bm._parse_async_completions(
      '<context>function [poll_status] completed with response {\n'
      '  "result": {\n    "status": "success"\n  }\n}</context>')
  assert out == [("poll_status", {"status": "success"}, "completed")]


def test_parses_the_failure_envelope_too():
  """A crashed async tool reports `failed with error`, not `completed with response`.

  Observed live: a NameError in the tool body produced this shape. Unrecognized, a
  crashed backend is indistinguishable from a slow one and the flow waits out its whole
  deadline instead of failing fast.
  """
  bm = _load_before_model()
  (tool, payload, _outcome), = bm._parse_async_completions(
      '<context>function [run_line_test] failed with error '
      '{"status": "error", "error": "name \'time\' is not defined"}</context>')
  assert tool == "run_line_test"
  assert payload["status"] == "error"
  # No `success` key, so it routes through the ordinary failure ladder unchanged.
  assert "success" not in payload
  # And the envelope's own verb is kept, which is the ONLY thing separating this from a
  # successful agent reply — that also carries no success key.
  assert _outcome == "failed"


def test_a_failure_envelope_clears_the_wait_and_fails_the_task():
  cfg = cfg_with(awaits={"max_turns": 9},
                 on_failure={"max_retries": 2, "retry_say": "Let me try that again."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  assert "lookup" in sm["_awaiting_async"]

  out = land(sm, cfg, {"status": "error", "error": "boom"}, turn=3)
  assert "_awaiting_async" not in sm
  assert out["action"]["message"] == "Let me try that again."


def test_a_malformed_payload_degrades_instead_of_throwing():
  """The envelope is a platform convention, not a documented contract."""
  bm = _load_before_model()
  (tool, payload, _outcome), = bm._parse_async_completions(
      "<context>function [poll_status] completed with response not-json</context>")
  assert tool == "poll_status"
  assert payload == {"raw": "not-json"}


@pytest.mark.parametrize("text", [
    "",
    "just a caller saying something",
    "<context>no user activity detected for 9 seconds.</context>",
    "<context>function [] completed with response {}</context>",
])
def test_non_completions_are_not_recognized(text):
  bm = _load_before_model()
  assert bm._parse_async_completions(text) == []


def test_the_inactivity_context_is_still_inactivity():
  """The two envelopes share the <context> wrapper; they must not collide."""
  bm = _load_before_model()
  silence = "<context>no user activity detected for 9 seconds.</context>"
  assert bm._INACTIVITY_PATTERN.search(silence)
  assert bm._parse_async_completions(silence) == []


# --------------------------------------------------------------------------- #
# 2. A pending result holds instead of failing
# --------------------------------------------------------------------------- #

def test_without_awaits_a_pending_result_is_treated_as_failure():
  """The behaviour being fixed: on_failure.max_retries defaults to 0, so the very
  first pending placeholder exhausts the ladder and escalates."""
  cfg = cfg_with()
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  out = land(sm, cfg, dict(PENDING), turn=2)
  assert "_awaiting_async" not in sm
  # The failure ladder ran: max_retries defaults to 0, so the first pending placeholder
  # exhausts it immediately and the flow speaks a give-up line rather than waiting.
  assert sm["_retries"].get("lookup") == 1
  assert out["action"].get("message") == "An error occurred."


def test_with_awaits_a_pending_result_holds_and_does_not_retry():
  cfg = cfg_with(awaits={"max_turns": 3, "say": "Still working on that."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  out = land(sm, cfg, dict(PENDING), turn=2)
  assert out["action"]["message"] == "Still working on that."
  assert sm["_awaiting_async"]["lookup"]["tool"] == "slow_lookup"
  assert not sm.get("_retries", {}).get("lookup")
  assert sm.get("status", "in_progress") == "in_progress"


def test_an_agent_tool_keyed_placeholder_also_holds():
  """An `agentTool` spells its whole wire contract `response`, placeholder included:
  `{"response": "pending"}` (ces-probes 134). Matching only `result` read that as a
  SUCCESS — the slot filled with the literal string "pending", the wait never started,
  and the real answer arrived a turn later with nowhere to go.

  It is the only agent-as-a-tool flavour that can defer at all: an async
  `remoteAgentTool` is dropped at deploy (ces-probes 133).
  """
  cfg = cfg_with(awaits={"max_turns": 3, "say": "Still working on that."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  out = land(sm, cfg, {"response": "pending"}, turn=2)
  assert out["action"]["message"] == "Still working on that."
  assert sm["_awaiting_async"]["lookup"]["tool"] == "slow_lookup"
  assert sm["filled"].get("lookup_result") != "pending", (
      "the placeholder was banked as a real answer")


def test_a_real_answer_keyed_response_is_not_the_placeholder():
  """Only the word `pending` is the placeholder. An agent that genuinely answers under
  a `response` key must still land as a result rather than starting a wait."""
  cfg = cfg_with(awaits={"max_turns": 3, "say": "Still working on that."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, {"response": "the parcel is in Boston"}, turn=2)
  assert not sm.get("_awaiting_async"), "a real answer started a wait"


def test_the_placeholder_is_not_an_answer_even_when_success_check_names_its_key():
  """The configuration `flows.agent_tool` actually produces, which the earlier tests
  missed by leaving `success_check` on its default.

  An agent answers under `response`, so a task firing one checks `response` — and the
  deferral placeholder is `{"response": "pending"}`, which satisfies that check. Driven
  live, all three arms filled the slot with the literal string "pending", announced it
  to the caller and ended the session; the real answer arrived afterwards with nowhere
  to go. Intake has to know a placeholder on sight, not infer it from a missing key.
  """
  cfg = cfg_with(awaits={"max_turns": 3, "say": "Still working on that."})
  cfg["tasks"][0]["success_check"] = "response"
  cfg["tasks"][0]["outputs"] = {"response": "status"}
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  out = land(sm, cfg, {"response": "pending"}, turn=2)
  assert sm["filled"].get("status") != "pending", "the placeholder was banked as the answer"
  assert out["action"]["message"] == "Still working on that."
  assert sm["_awaiting_async"]["lookup"]["tool"] == "slow_lookup"


def test_the_real_answer_lands_after_that_placeholder():
  """The other half: once the wait is open, the agent's actual reply must fill the slot
  even though it, too, carries no success key."""
  cfg = cfg_with(awaits={"max_turns": 3, "say": "Still working on that."})
  cfg["tasks"][0]["success_check"] = "response"
  cfg["tasks"][0]["outputs"] = {"response": "status"}
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, {"response": "pending"}, turn=2)
  land(sm, cfg, {"response": "out for delivery in Boston"}, turn=3, outcome="completed")
  assert sm["filled"].get("status") == "out for delivery in Boston"


def test_an_agent_answer_succeeds_on_the_envelope_verb_alone():
  """The point of capturing the verb. An agent's reply carries no `success` key, so
  before this it read as a failure and the caller heard the give-up line while the
  answer sat in the payload."""
  cfg = cfg_with(awaits={"max_turns": 3, "say": "Still working on that."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, {"response": "pending"}, turn=2)
  out = land(sm, cfg, {"status_msg": "the parcel is in Boston"}, turn=3,
             outcome="completed")
  assert sm["filled"].get("status") == "the parcel is in Boston"
  assert out["action"].get("message") != "An error occurred."


def test_the_failure_envelope_still_fails_though_it_looks_identical():
  """Same shape, no success key, opposite verdict — and the verb is the only thing that
  separates them. Without it this test and the one above cannot both pass."""
  cfg = cfg_with(awaits={"max_turns": 3, "say": "Still working on that."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, {"response": "pending"}, turn=2)
  out = land(sm, cfg, {"error": "backend exploded"}, turn=3, outcome="failed")
  assert not sm["filled"].get("status")
  assert out["action"].get("message") == "An error occurred."


def test_an_error_payload_is_not_rescued_by_a_completed_verb():
  """The shape a platform-executed agent call fails with (ces-probes 135):
  `{"status": "error", "error": "..."}`, with no `response` key — which is precisely
  the condition that hands the verdict to the envelope's verb.

  Whether an async agent failure arrives as `failed with error` or as `completed with
  response` carrying this payload is unmeasured, so the verb must not be able to call
  it a success.
  """
  cfg = cfg_with(awaits={"max_turns": 3, "say": "Still working on that."})
  cfg["tasks"][0]["success_check"] = "response"
  cfg["tasks"][0]["outputs"] = {"response": "status"}
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, {"response": "pending"}, turn=2)
  out = land(sm, cfg, {"status": "error", "error": "Error fetching from URL"},
             turn=3, outcome="completed")
  assert not sm["filled"].get("status"), "an error payload was banked as the answer"
  assert out["action"].get("message") == "An error occurred."


def test_an_explicit_success_key_still_wins_over_the_verb():
  """The guard on the whole change: a python tool says for itself and must keep
  deciding. A `completed` envelope carrying `success: False` is still a failure."""
  cfg = cfg_with(awaits={"max_turns": 3, "say": "Still working on that."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  out = land(sm, cfg, {"success": False, "status_msg": "ignored"}, turn=3,
             outcome="completed")
  assert not sm["filled"].get("status"), "the verb overrode an explicit success=False"
  assert out["action"].get("message") == "An error occurred."


def test_a_synchronous_result_is_unchanged_by_the_verb_fallback():
  """No envelope, no verb. Every existing app lands here and must behave as before."""
  cfg = cfg_with()
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, {"success": True, "status_msg": "ok"}, turn=2)
  assert sm["filled"].get("status") == "ok"


def test_an_empty_say_holds_silently():
  """No text and silent=True makes before_model emit an empty LlmResponse, so the
  caller hears nothing rather than the model improvising while the backend works."""
  cfg = cfg_with(awaits={"max_turns": 3})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  out = land(sm, cfg, dict(PENDING), turn=2)
  assert out["action"]["message"] == ""
  assert out["action"]["silent"] is True


# --------------------------------------------------------------------------- #
# 3. An in-flight task is not re-fired
# --------------------------------------------------------------------------- #

def test_an_in_flight_task_is_not_redispatched():
  """A pending result is falsy under success_check, so _compute_dag_state's
  already-succeeded skip does NOT catch it — without the in-flight guard the engine
  re-dispatches the tool on every turn for as long as the backend takes."""
  cfg = cfg_with(awaits={"max_turns": 5})
  sm = fresh(cfg)
  first = drive(cfg, sm, "", turn=1)
  assert (first["action"].get("function_call") or {}).get("name") == "slow_lookup"

  land(sm, cfg, dict(PENDING), turn=2)
  for turn in (3, 4):
    again = drive(cfg, sm, "", turn=turn)
    assert again["action"].get("function_call") is None, turn


def test_without_awaits_the_task_is_redispatched_every_turn():
  """The A/B half: this is exactly the runaway the guard prevents.

  Re-firing across USER TURNS is intended (retry-next-turn). What must NOT happen is
  re-firing across the reasoning PASSES of ONE turn — see the #698 tests below, which
  the turn-scoped in-flight mark keeps distinct from this per-turn retry.
  """
  cfg = cfg_with()
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  sm.update(fb.run_intake("slow_lookup", dict(PENDING), sm)["sm"])
  sm.pop("_task_just_completed", None)  # skip the failure ladder; ask the selector
  again = drive(cfg, sm, "", turn=2)
  assert (again["action"].get("function_call") or {}).get("name") == "slow_lookup"


# --------------------------------------------------------------------------- #
# 3c. #698 — a SYNCHRONOUS input-free entry executor must not re-fire within one
#     turn to the ten-reasoning-loop cap (deterministic; makes a specialist
#     unreachable on every entry path). Mirrors fedex tracking's
#     get_tracking_configuration_task: inputs=[], no requires, non-terminal,
#     condition true on entry, and a guard slot fed from a FALSY out_key so the
#     success-recorded skip can never latch.
# --------------------------------------------------------------------------- #

def cfg_entry_config_load():
  return {
      "slots": [
          {"name": "tracking_number", "source": "user", "setter": "set_tn",
           "ask": "What's your tracking number?"},
          {"name": "session_config_loaded", "source": "task:load_config"},
      ],
      "tasks": [{
          "name": "load_config", "tool": "get_config", "inputs": [],
          "outputs": {"isDisputeDeliveryFlowEnabled": "session_config_loaded"},
          "success_check": "success", "terminal": False, "requires": [],
          "condition": "lambda f: not f.get('session_config_loaded')",
      }],
      "gate_slot": None,
  }


def test_698_input_free_entry_executor_does_not_refire_within_a_turn():
  """The loop itself. On the empty-contents auto-turn (specialist entered via
  transfer_to_agent, the caller's utterance already consumed) the fire's after_tool
  never lands, so before the fix the engine re-emitted get_config on every before_model
  pass to the ten-loop cap, never yielding. It must fire at most once, then yield the
  opener; the still-eligible condition must NOT keep re-firing this turn."""
  cfg = cfg_entry_config_load()
  sm = fresh(cfg)
  first = drive(cfg, sm, "", turn=1)
  assert (first["action"].get("function_call") or {}).get("name") == "get_config"
  sm = first["sm"]
  for _ in range(3):  # re-invoke the SAME turn with NO intake (result never lands)
    again = drive(cfg, sm, "", turn=1)
    sm = again["sm"]
    assert again["action"].get("function_call") is None, (
        "the input-free executor re-fired within one turn — the #698 reasoning loop")
    assert "tracking number" in (again["action"].get("message") or ""), (
        "the turn did not yield the opener after the fire was held")


def test_698_the_input_free_load_does_not_eat_the_callers_turn():
  """The second half of #698. A fresh caller turn clears the mark, so the load is
  eligible again — but a turn carrying user text must NOT preempt-fire it ahead of the
  model, or the setter the model would call for that utterance never runs and the value
  is dropped (the entry load ate the tracking number). It defers here and fires on the
  post-setter (empty-text) re-invoke, where its after_tool lands and it completes."""
  cfg = cfg_entry_config_load()
  sm = fresh(cfg)
  first = drive(cfg, sm, "", turn=1)
  assert (first["action"].get("function_call") or {}).get("name") == "get_config"
  # New caller turn carrying the tracking number: the load must step aside for the model.
  spoke = drive(cfg, first["sm"], "it's 7 7 9 8", turn=2)
  assert spoke["action"].get("function_call") is None, (
      "the input-free load preempt-fired on a turn with fresh user text — it would drop "
      "the setter the model was about to call (#698)")
  # The model captured the value (fill it directly — the setter wiring is not what this
  # test exercises). The caller's utterance is now consumed, so on the post-setter
  # re-invoke the load fires (a content turn where its after_tool lands and completes).
  sm2 = spoke["sm"]
  sm2.setdefault("filled", {})["tracking_number"] = "7798"
  after = drive(cfg, sm2, "", turn=2)
  assert (after["action"].get("function_call") or {}).get("name") == "get_config"


def test_698_the_in_flight_mark_is_released_by_the_RESULT_not_only_the_turn():
  """The mark means "fired, not yet intaken", so the result landing is what ends it.

  The caller-turn boundary is too coarse to be the only release, in two ways that both
  reach production. `_turn_n` counts CALLER turns, so a silent caller (inactivity ticks)
  never advances it; and a repeated collection fires the SAME task once per element
  inside one turn. Either way a task whose result had already landed stayed marked
  in-flight and could never run again -- the collection simply stopped.
  """
  cfg = cfg_entry_config_load()
  sm = fresh(cfg)
  first = drive(cfg, sm, "", turn=1)
  assert (first["action"].get("function_call") or {}).get("name") == "get_config"
  sm = first["sm"]
  assert "load_config" in (sm.get("_sync_fire_pending") or {})

  # The result lands -- same turn, exactly as after_tool -> slot_intake delivers it.
  sm.setdefault("task_results", {})["load_config"] = {"success": True}
  sm = drive(cfg, sm, "", turn=1)["sm"]
  assert "load_config" not in (sm.get("_sync_fire_pending") or {}), (
      "the in-flight mark outlived the result it was waiting for, so a within-turn "
      "re-run (a repeated loop's next element) or a silent caller would strand")

  # And the release is real: with the result cleared the way a repeated loop clears it
  # to run the next element, the task is eligible again WITHOUT a new caller turn.
  sm["task_results"].pop("load_config", None)
  again = drive(cfg, sm, "", turn=1)
  assert (again["action"].get("function_call") or {}).get("name") == "get_config"


# --------------------------------------------------------------------------- #
# 3b. The flow keeps working while the backend does
# --------------------------------------------------------------------------- #

def cfg_concurrent(say=None, extra_slot=True, extra_task=False):
  """An awaited lookup plus work that does not depend on it."""
  slots = [
      {"name": "acct", "source": "user", "setter": "set_acct", "ask": "Account number?"},
      {"name": "status", "source": "task:poll"},
  ]
  tasks = [{
      "name": "poll", "tool": "poll_x", "inputs": ["acct"],
      "outputs": {"status_msg": "status"}, "success_check": "success",
      "terminal": False, "requires": ["acct"],
      "awaits": {"max_turns": 9, **({"say": say} if say else {})},
  }]
  if extra_slot:
    slots.append({"name": "email", "source": "user", "setter": "set_email",
                  "ask": "Email address?"})
  if extra_task:
    slots.append({"name": "sent", "source": "task:notify"})
    tasks.append({"name": "notify", "tool": "notify_x", "inputs": ["email"],
                  "outputs": {"ok": "sent"}, "success_check": "success",
                  "terminal": False, "requires": ["email"]})
  return {"slots": slots, "tasks": tasks, "gate_slot": None}


def start_wait(cfg, sm):
  """Drive to the point where the async lookup is outstanding."""
  drive(cfg, sm, "", turn=1)
  sm.update(fb.run_intake("set_acct", {"stored": True, "value": "1"}, sm)["sm"])
  drive(cfg, sm, "1", turn=2)
  sm.update(fb.run_intake("poll_x", dict(PENDING), sm)["sm"])


def test_collection_continues_while_the_backend_works():
  """The wait blocks its own task, not the conversation."""
  cfg = cfg_concurrent(say="Looking that up.")
  sm = fresh(cfg)
  start_wait(cfg, sm)
  assert drive(cfg, sm, "", turn=3)["action"]["message"] == "Looking that up."
  assert drive(cfg, sm, "ok", turn=4)["action"]["message"] == "Email address?"
  assert "poll" in sm["_awaiting_async"]


def test_an_unrelated_task_still_fires_while_waiting():
  cfg = cfg_concurrent(say="Looking that up.", extra_task=True)
  sm = fresh(cfg)
  start_wait(cfg, sm)
  drive(cfg, sm, "", turn=3)
  drive(cfg, sm, "ok", turn=4)
  sm.update(fb.run_intake("set_email", {"stored": True, "value": "a@b.c"}, sm)["sm"])
  fired = (drive(cfg, sm, "a@b.c", turn=5)["action"].get("function_call") or {}).get("name")
  assert fired == "notify_x"
  assert "poll" in sm["_awaiting_async"]


def test_a_silent_wait_does_not_burn_a_turn_that_had_a_question_ready():
  """With no line to speak there is nothing to end the turn FOR — asking the next
  question immediately is strictly better than a turn of silence."""
  cfg = cfg_concurrent(say=None)
  sm = fresh(cfg)
  start_wait(cfg, sm)
  assert drive(cfg, sm, "", turn=3)["action"]["message"] == "Email address?"


def test_an_idle_wait_holds_silently_rather_than_letting_the_model_improvise():
  """Nothing left to collect is not a finished flow. Without the deliberate hold the
  turn proceeds with no directive and the model fills the silence itself."""
  cfg = cfg_concurrent(say=None, extra_slot=False)
  sm = fresh(cfg)
  start_wait(cfg, sm)
  action = drive(cfg, sm, "", turn=3)["action"]
  assert action["preempt"] is True
  assert action["silent"] is True
  assert action["message"] == ""


# --------------------------------------------------------------------------- #
# 4. The real result resolves the wait
# --------------------------------------------------------------------------- #

def test_the_real_payload_clears_the_wait_and_maps_outputs():
  cfg = cfg_with(awaits={"max_turns": 5})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  assert "lookup" in sm["_awaiting_async"]

  out = land(sm, cfg, {"success": True, "status_msg": "all set"}, turn=3)
  assert "_awaiting_async" not in sm
  assert sm["filled"]["status"] == "all set"
  assert out["action"]["message"] == "Done: all set"


def test_a_real_failure_after_a_pending_still_reaches_the_failure_ladder():
  """Holding must not swallow a genuine error that arrives later."""
  cfg = cfg_with(awaits={"max_turns": 5},
                 on_failure={"max_retries": 2, "retry_say": "Let me retry."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  out = land(sm, cfg, {"success": False}, turn=3)
  assert "_awaiting_async" not in sm
  assert out["action"]["message"] == "Let me retry."


# --------------------------------------------------------------------------- #
# 5. The timeout
# --------------------------------------------------------------------------- #

def test_the_wait_gives_up_after_max_turns():
  """CES has no timeout of its own — a hung async tool never sends its completion
  turn, so max_turns is the only thing between a wedged backend and a wedged call."""
  cfg = cfg_with(awaits={
      "max_turns": 2,
      "on_timeout": {"say": "I'm having trouble with that."},
  })
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)

  assert drive(cfg, sm, "", turn=3)["action"].get("message") != \
      "I'm having trouble with that."
  timed_out = drive(cfg, sm, "", turn=4)
  assert timed_out["action"]["message"] == "I'm having trouble with that."
  assert "_awaiting_async" not in sm


def test_a_waiting_caller_is_not_counted_as_off_topic():
  """Found live: "hello? are you there" while a backend hung scored as off-topic,
  steer-back returned before the deadline was even checked, and the flow reassured the
  caller forever instead of giving up."""
  cfg = cfg_with(awaits={"max_turns": 4})
  cfg["steer_back"] = {"soft_after": 1, "hard_after": 2, "escalate_after": 3}
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  for turn in (3, 4):
    drive(cfg, sm, "hello? are you there", turn=turn)
  assert not sm.get("_steer_back_turns")


def test_the_deadline_outranks_steer_back():
  """Same fixture, past the bound: the give-up must win, not the off-topic ladder."""
  cfg = cfg_with(awaits={"max_turns": 1, "on_timeout": {"say": "gave up"}})
  cfg["steer_back"] = {"soft_after": 1, "hard_after": 2, "escalate_after": 3}
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  assert drive(cfg, sm, "are you there", turn=3)["action"]["message"] == "gave up"


def test_a_completion_on_the_deadline_turn_beats_the_timeout():
  """The sweep runs after the post-executor handler, so a result that DID land wins."""
  cfg = cfg_with(awaits={"max_turns": 1, "on_timeout": {"say": "gave up"}})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  out = land(sm, cfg, {"success": True, "status_msg": "just in time"}, turn=9)
  assert out["action"]["message"] == "Done: just in time"
  assert "_awaiting_async" not in sm


# --------------------------------------------------------------------------- #
# 6. Lifetime, tolerance, and no-op-when-unused
# --------------------------------------------------------------------------- #

def test_the_in_flight_marker_is_not_carried_across_a_flow_change():
  """It names a task in the OLD config. Surviving a flow change would leave a hold
  with no timeout owner — so it belongs in none of the keep-sets."""
  eng = fb.load_engine()
  intake = fb.load_intake()
  assert "_awaiting_async" not in eng._FLOW_KEEP
  assert "_awaiting_async" not in intake._FLOW_KEEP


def test_a_scalar_tool_result_is_recorded_instead_of_crashing_intake():
  """after_tool unwraps {"result": "pending"} to the bare string, which used to raise
  AttributeError inside intake's crash envelope and silently drop the result."""
  cfg = cfg_with(awaits={"max_turns": 3})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  sm.update(fb.run_intake("slow_lookup", "pending", sm)["sm"])
  assert sm["task_results"]["lookup"] == {"result": "pending"}
  assert sm["_task_just_completed"] == "lookup"


def test_a_task_without_awaits_behaves_exactly_as_before():
  """Byte-identical config, unchanged failure routing."""
  cfg = cfg_with(on_failure={"max_retries": 2, "retry_say": "Let me try again."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  out = land(sm, cfg, {"success": False}, turn=2)
  assert out["action"]["message"] == "Let me try again."
  assert "_awaiting_async" not in sm


# --------------------------------------------------------------------------- #
# 7. Authoring surface
# --------------------------------------------------------------------------- #

def test_awaits_requires_a_positive_bound():
  import flows
  for bad in (0, -1, None, "3", True, 2.5):
    with pytest.raises(ValueError, match="max_turns"):
      flows.awaits(max_turns=bad)


def test_an_integral_float_bound_is_accepted_and_normalized():
  """A config that has round-tripped through JSON carries 3 as 3.0. The engine already
  reads max_turns as (int, float), so the authoring and validation layers must not be
  stricter than the runtime."""
  import flows
  assert flows.awaits(max_turns=3.0) == {"max_turns": 3}


def test_the_validator_accepts_an_integral_float_bound():
  errors = _lint_awaits({"max_turns": 3.0})["errors"]
  assert [e for e in errors if "max_turns" in e] == [], errors


def test_the_validator_still_rejects_a_fractional_bound():
  """Turns are discrete; 2.5 means the author misunderstood the unit."""
  errors = _lint_awaits({"max_turns": 2.5})["errors"]
  assert any("whole number" in e for e in errors), errors


def test_awaits_emits_only_what_was_asked_for():
  import flows
  assert flows.awaits(max_turns=4) == {"max_turns": 4}
  assert flows.awaits(max_turns=4, say="hold on") == {"max_turns": 4, "say": "hold on"}


def test_task_omits_the_key_entirely_when_not_awaiting():
  import flows
  assert "awaits" not in flows.task("t", "tool", [], "out")


def test_execution_type_is_emitted_only_for_async_tools():
  from flows.emit import scaffold
  import json
  plain = json.loads(scaffold._setter_tool_json("set_x"))
  assert "executionType" not in plain
  asynchronous = json.loads(scaffold._setter_tool_json("poll_x", asynchronous=True))
  assert asynchronous["executionType"] == "ASYNCHRONOUS"


# --------------------------------------------------------------------------- #
# 8. `while_waiting` — the turns AFTER the wait starts
# --------------------------------------------------------------------------- #

def test_an_idle_wait_speaks_the_ladder_one_line_per_turn():
  """`say` covers the moment the wait starts; the turns after it were silent.

  A silent hold is right for a gap of a second or two and wrong for thirty — the
  caller hears nothing, assumes the line dropped, and hangs up.
  """
  cfg = cfg_with(awaits={
      "max_turns": 6, "say": "Looking that up.",
      "while_waiting": ["Still checking.", "Almost there."]})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  assert land(sm, cfg, dict(PENDING), turn=2)["action"]["message"] == "Looking that up."

  assert drive(cfg, sm, "", turn=3)["action"]["message"] == "Still checking."
  assert drive(cfg, sm, "", turn=4)["action"]["message"] == "Almost there."


def test_the_ladder_drains_rather_than_cycling():
  """Reassurance on a loop reads worse than silence, and `max_turns` is what
  actually ends the wait."""
  cfg = cfg_with(awaits={"max_turns": 8, "while_waiting": ["One moment."]})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  # No `say`, so the placeholder turn does not end the turn itself — it falls through
  # to the idle hold, which is where the ladder speaks.
  assert land(sm, cfg, dict(PENDING), turn=2)["action"]["message"] == "One moment."

  spent = drive(cfg, sm, "", turn=3)["action"]
  assert spent["message"] == ""
  assert spent["silent"] is True


def test_without_the_ladder_an_idle_wait_is_still_silent():
  """The A/B half: the default is unchanged, so existing agents hold as before."""
  cfg = cfg_with(awaits={"max_turns": 6, "say": "Looking that up."})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  idle = drive(cfg, sm, "", turn=3)["action"]
  assert idle["message"] == ""
  assert idle["silent"] is True


def test_the_ladder_does_not_talk_over_a_question_the_flow_can_ask():
  """`while_waiting` is for DEAD air only. A wait busy collecting an unrelated slot
  asks the question; interleaving the two would speak over it."""
  cfg = cfg_concurrent(say="Looking that up.")
  cfg["tasks"][0]["awaits"]["while_waiting"] = ["Still checking."]
  sm = fresh(cfg)
  start_wait(cfg, sm)
  drive(cfg, sm, "", turn=3)
  assert drive(cfg, sm, "ok", turn=4)["action"]["message"] == "Email address?"
  # Untouched, so the line is still available for the first genuinely idle turn.
  assert sm["_awaiting_async"]["poll"].get("held", 0) == 0


def test_the_ladder_does_not_speak_over_a_caller_who_is_answering():
  """`while_waiting` is cover for DEAD air, and a turn the caller spoke on is not that.

  From inside the engine a turn the caller is spending THINKING is indistinguishable
  from an inactivity tick, so the ladder drew a line on it. On voice that is not a
  manners problem: driven on a repair agent asking a scoping question during a
  thirty-second job, the caller said "Uh", the ladder answered over the top of them, and
  the real answer arrived as a BARGE-IN that never reached the request at all — the
  engine's view of the caller stayed frozen on "Uh" for the rest of the call and the
  capture slot never filled.
  """
  cfg = cfg_with(awaits={
      "max_turns": 9, "while_waiting": ["Still checking.", "Almost there."]})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  # The placeholder turn falls through to the hold, which is where the first line goes.
  assert land(sm, cfg, dict(PENDING), turn=2)["action"]["message"] == "Still checking."
  spoke = drive(cfg, sm, "uh", turn=3)["action"]
  assert spoke["message"] == "", spoke
  assert spoke["silent"] is True


def test_a_line_the_caller_talked_through_is_not_spent():
  """The reassurance is still OWED — the wait has not gone quiet, the caller filled it.
  Consuming the line would pay it to nobody and leave the next real silence bare."""
  cfg = cfg_with(awaits={
      "max_turns": 9, "while_waiting": ["Still checking.", "Almost there."]})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  drive(cfg, sm, "uh", turn=3)
  assert sm["_awaiting_async"]["lookup"].get("held", 0) == 1
  assert drive(cfg, sm, "", turn=4)["action"]["message"] == "Almost there."


def test_a_genuinely_idle_turn_still_gets_its_line():
  """The A/B half: nothing changes for a wait whose turns are real ticks."""
  cfg = cfg_with(awaits={
      "max_turns": 9, "while_waiting": ["Still checking.", "Almost there."]})
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  land(sm, cfg, dict(PENDING), turn=2)
  assert drive(cfg, sm, "", turn=3)["action"]["message"] == "Almost there."


def test_the_ladder_interpolates_filled_slots():
  cfg = cfg_concurrent()
  cfg["tasks"][0]["awaits"]["while_waiting"] = ["Still checking account {acct}."]
  sm = fresh(cfg)
  start_wait(cfg, sm)
  sm["filled"]["email"] = "a@b.c"  # nothing left to collect, so the hold is idle
  assert drive(cfg, sm, "", turn=3)["action"]["message"] == "Still checking account 1."


def test_a_ladder_longer_than_the_bound_is_rejected():
  """A line the wait ends before reaching is a script the caller never hears."""
  import flows
  with pytest.raises(ValueError, match="while_waiting"):
    flows.awaits(max_turns=1, while_waiting=["a", "b"])
  with pytest.raises(ValueError, match="while_waiting"):
    flows.awaits(max_turns=3, while_waiting="not a list")


# --------------------------------------------------------------------------- #
# 9. A consumer of a slot the wait will overwrite has to wait too
# --------------------------------------------------------------------------- #

def cfg_stale_consumer():
  """A status slot SEEDED before the poll resolves it, plus a task that reads it.

  The in-flight guard keeps the awaited task out of the selector, but the consumer is
  a different task and is perfectly eligible on the seeded value.
  """
  return {
      "slots": [
          {"name": "status", "source": "task:poll"},
          {"name": "advice", "source": "task:advise"},
      ],
      "tasks": [
          {"name": "poll", "tool": "poll_x", "inputs": [],
           "outputs": {"status_msg": "status"}, "success_check": "success",
           "terminal": False, "requires": [],
           "awaits": {"max_turns": 9}},
          {"name": "advise", "tool": "advise_x", "inputs": ["status"],
           "outputs": {"advice": "advice"}, "success_check": "success",
           "terminal": False, "requires": ["status"]},
      ],
      "gate_slot": None,
  }


def test_a_consumer_does_not_fire_on_a_value_the_wait_will_replace():
  cfg = cfg_stale_consumer()
  sm = fresh(cfg)
  sm["filled"]["status"] = "pending"  # seeded for the backend to resolve
  drive(cfg, sm, "", turn=1)
  sm.update(fb.run_intake("poll_x", dict(PENDING), sm)["sm"])

  held = drive(cfg, sm, "", turn=2)["action"]
  assert held.get("function_call") is None
  assert "poll" in sm["_awaiting_async"]


def test_the_consumer_fires_once_the_real_value_lands():
  cfg = cfg_stale_consumer()
  sm = fresh(cfg)
  sm["filled"]["status"] = "pending"
  drive(cfg, sm, "", turn=1)
  sm.update(fb.run_intake("poll_x", dict(PENDING), sm)["sm"])
  drive(cfg, sm, "", turn=2)

  # `land` is wired to the section-1 fixture's tool; this flow polls `poll_x`.
  sm.update(fb.run_intake(
      "poll_x", {"success": True, "status_msg": "line fault"}, sm)["sm"])
  resolved = drive(cfg, sm, "", turn=3)
  assert "_awaiting_async" not in sm
  assert (resolved["action"].get("function_call") or {})["args"] == {
      "status": "line fault"}


def test_a_consumer_of_an_UNRELATED_slot_still_fires_during_the_wait():
  """The hold is scoped to the wait's own outputs — it must not stall the flow."""
  cfg = cfg_concurrent(extra_task=True)
  sm = fresh(cfg)
  start_wait(cfg, sm)
  sm["filled"]["email"] = "a@b.c"
  fired = (drive(cfg, sm, "", turn=3)["action"].get("function_call") or {}).get("name")
  assert fired == "notify_x"


# --------------------------------------------------------------------------- #
# 10. Envelope ingestion when the turn carries more than one thing
# --------------------------------------------------------------------------- #

ENVELOPE_A = ('<context>function [poll_x] completed with response '
              '{"result": {"success": true, "status_msg": "ok"}}</context>')
ENVELOPE_B = ('<context>function [notify_x] completed with response '
              '{"result": {"success": true, "ok": "sent"}}</context>')


def test_two_completions_in_one_turn_are_both_ingested():
  """Ordinarily each completion is its own synthetic turn, but nothing makes the
  platform space two outstanding tools out. A completion that is merely NOT SEEN is
  worse than a late one: the task waits out `max_turns` and reports a timeout for a
  backend that actually answered.
  """
  bm = _load_before_model()
  assert bm._parse_async_completions(ENVELOPE_A + "\n" + ENVELOPE_B) == [
      ("poll_x", {"success": True, "status_msg": "ok"}, "completed"),
      ("notify_x", {"success": True, "ok": "sent"}, "completed"),
  ]


def test_the_callback_strips_the_envelope_and_flags_the_turn():
  """The split: the callback removes the ENVELOPE (never speech) and reports that a
  completion landed. Whether the REMAINDER counts as speech is `awaits.answer_first`,
  and that config lives in the engine — see section 13 for the decision itself."""
  bm = _load_before_model()
  both = "my email is a@b.c " + ENVELOPE_A
  assert bm._parse_async_completions(both) == [
      ("poll_x", {"success": True, "status_msg": "ok"}, "completed")]

  extracted = _extract_with_parts([both])
  assert extracted["last_user_text"] == "my email is a@b.c"
  assert extracted["async_completion_landed"] is True
  assert extracted["async_completions"] == [
      ("poll_x", {"success": True, "status_msg": "ok"}, "completed")]


def _extract_with_parts(texts):
  """Run the real `_extract` over one user turn built from `texts`."""
  bm = _load_before_model()
  part = type("P", (), {})
  parts = []
  for t in texts:
    p = part()
    p.text = t
    parts.append(p)
  content = type("C", (), {})()
  content.role, content.parts = "user", parts
  req = type("R", (), {})()
  req.contents = [content]
  ctx = type("Ctx", (), {})()
  ctx.state = {}
  return bm._extract(ctx, req, {})


def test_both_completions_in_a_split_turn_are_harvested():
  """Two parts, two waits resolving together — neither may be lost."""
  got = _extract_with_parts([ENVELOPE_A, ENVELOPE_B])
  assert got["last_user_text"] == ""
  assert [t for t, _, _ in got["async_completions"]] == ["poll_x", "notify_x"]


def test_an_ordinary_turn_is_untouched():
  """The A/B half: no envelope, nothing blanked."""
  got = _extract_with_parts(["my email is a@b.c"])
  assert got["last_user_text"] == "my email is a@b.c"
  assert got["async_completions"] == []


# A barge-in delivers the SAME "context part alongside real speech" shape as the
# co-present inactivity/async cases above. CES prepends an interruption note when the
# caller talks over the agent and answers in one breath; that note is not inactivity,
# so the old inactivity-only filter grabbed it and DROPPED the speech (issue #511).
_INTERRUPTION = ("<context>agent speaking was interrupted. user only heard 'What's "
                 "your tracking number' in the last agent response.</context>")


def test_barge_in_context_does_not_shadow_the_real_utterance():
  """The interruption note rides in part 1, the number in part 2. last_user_text must
  resolve to the SPEECH, not the note, or the slot re-asks and the flow stalls."""
  got = _extract_with_parts([_INTERRUPTION, "It's 779881234567."])
  assert got["last_user_text"] == "It's 779881234567."
  # A barge-in turn carries real speech, so it is NOT a silence tick.
  assert got["is_inactivity"] is False


def test_barge_in_note_order_independent():
  """Speech first, note second must resolve the same way (pick the real utterance)."""
  got = _extract_with_parts(["It's 779881234567.", _INTERRUPTION])
  assert got["last_user_text"] == "It's 779881234567."


# A DTMF keypress rides as its OWN <context> wrapper, so _is_real_user_text rejects it
# like the barge-in note. _extract lifts the bare token so the engine's dtmf_map
# fast-path can fire (previously only the model, reading the wrapper, handled keypads).
_KEYPAD = "<context>user pressed 1 on keypad.</context>"


def test_keypad_press_becomes_the_bare_token():
  got = _extract_with_parts([_KEYPAD])
  assert got["last_user_text"] == "1"
  assert got["is_inactivity"] is False


def test_keypad_press_survives_a_co_present_barge_in_note():
  """Barge-in DTMF: interruption note + keypress. The digit wins over the note."""
  got = _extract_with_parts([_INTERRUPTION, _KEYPAD])
  assert got["last_user_text"] == "1"


def test_multi_digit_keypad_entry_is_lifted_whole():
  got = _extract_with_parts(["<context>user pressed 7231232 on keypad.</context>"])
  assert got["last_user_text"] == "7231232"


def test_spoken_utterance_still_wins_over_a_co_present_keypress():
  got = _extract_with_parts([_KEYPAD, "actually make it two"])
  assert got["last_user_text"] == "actually make it two"


def test_stripping_leaves_ordinary_speech_untouched():
  bm = _load_before_model()
  assert bm._strip_async_envelopes("just a caller talking") == "just a caller talking"
  assert bm._strip_async_envelopes(ENVELOPE_A) == ""


# --------------------------------------------------------------------------- #
# 10b. "A completion landed" must mean one was RECORDED, not merely parsed
#
# The flag is the engine's cue to read the turn as a delivery and blank the caller's
# speech (section 13). An envelope that is SKIPPED unblocks nothing, so blanking on it
# is pure loss — the answer is discarded and the flow re-asks. A progressive fan-out leg
# republishes its envelope turns after the group ingested it from state, which is
# exactly when a caller answering a question asked DURING the wait loses their answer.
# --------------------------------------------------------------------------- #

class _Resp:
  def __init__(self, payload):
    self._payload = payload

  def json(self):
    return self._payload


def _run_ingest(texts, sm, intake=None):
  """Drive the real `before_model_callback` far enough to see what the engine is told.

  Everything past the ingest loop is stubbed: `tools` (CES-injected, and the only way
  intake can run) and `_apply` (needs a live LlmRequest). The captured `input_data` is
  the engine's whole view of the turn, which is the thing under test.
  """
  bm = _load_before_model()
  captured = {}

  def _default_intake(arg):
    _sm = dict(arg["input_data"]["sm"])
    _sm["_task_just_completed"] = arg["input_data"]["tool_name"]
    return _Resp({"result": {"sm": _sm}})

  class _Tools:
    slot_intake = staticmethod(intake or _default_intake)

    @staticmethod
    def slot_filling_engine(arg):
      captured.update(arg["input_data"])
      return _Resp({"result": {"sm": arg["input_data"]["sm"], "action": {}}})

  bm.tools = _Tools()
  bm._apply = lambda ctx, req, out_sm, action: None

  parts = []
  for t in texts:
    p = type("P", (), {})()
    p.text = t
    parts.append(p)
  content = type("C", (), {})()
  content.role, content.parts = "user", parts
  req = type("R", (), {})()
  req.contents = [content]
  req.model = "gemini-3.1-flash-live"
  ctx = type("Ctx", (), {})()
  ctx.state = {"sm": dict(sm)}
  bm.before_model_callback(ctx, req)
  assert captured, "the engine was never called — the callback crashed before it"
  return captured


def test_a_skipped_fanout_envelope_does_not_read_as_a_delivery():
  """The regression: a leg's republished envelope must not cost the caller their answer.

  Driven live on a repair agent that asks a scoping question during its sweep, the two
  fan-out legs republished their envelopes on the very turn the caller answered on, and
  the answer was blanked on 12 of 12 voice drives.
  """
  got = _run_ingest([ENVELOPE_A, "just one device"], {"_fanout_ingested": ["poll_x"]})
  assert got["async_completion_landed"] is False
  assert got["last_user_text"] == "just one device"


def test_a_real_completion_still_reads_as_a_delivery():
  """The A/B half: an envelope that IS ingested keeps the delivery reading, so
  `answer_first` remains the only thing that saves the speech."""
  got = _run_ingest([ENVELOPE_A, "just one device"], {})
  assert got["async_completion_landed"] is True
  assert got["last_user_text"] == "just one device"


def test_a_completion_that_could_not_be_ingested_does_not_blank_the_turn():
  """Intake threw, so nothing was recorded and nothing was unblocked. The task's own
  `awaits.max_turns` is the backstop; until then the caller's turn is an ordinary one."""
  def _boom(_arg):
    raise ValueError("unparseable")

  got = _run_ingest([ENVELOPE_A, "just one device"], {}, intake=_boom)
  assert got["async_completion_landed"] is False
  assert got["last_user_text"] == "just one device"


# --------------------------------------------------------------------------- #
# 11. The tool declaration and the task's wait must agree
# --------------------------------------------------------------------------- #

def _pairing(asynchronous, with_awaits):
  """An app with one task, dialled between the two halves of the declaration."""
  import flows
  from flows.authoring import tools as tool_registry
  # The registry is a module global other test modules populate at import time, so
  # snapshot it rather than clearing outright.
  saved = dict(tool_registry._REGISTRY)
  tool_registry._REGISTRY.clear()

  @flows.tool(flow="pair", asynchronous=asynchronous)
  def poll_x(acct: str = "") -> dict:
    """Poll something."""
    return {"status_msg": "ok", "success": True}

  f = flows.Flow("pair", root_agent="Pair_Agent")
  f.add(
      flows.user_slot("acct", ask="Account number?"),
      flows.result_slot("status_msg", "poll"),
  )
  f.task(flows.task(
      "poll", "poll_x", ["acct"], "status_msg", out_key="status_msg",
      awaits=flows.awaits(max_turns=5) if with_awaits else None))
  try:
    return flows.validate_app(flows.App(root_flow=f, app_display_name="Pair"))
  finally:
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(saved)


def test_an_asynchronous_tool_without_awaits_is_an_error():
  """The two halves live in different files — `executionType` on the tool resource,
  `awaits` on the task — so nothing caught a mismatch until the flow ran. Missing
  `awaits` is the exact failure the primitive exists to prevent.
  """
  errors, _warnings = _pairing(asynchronous=True, with_awaits=False)
  assert any("declared ASYNCHRONOUS" in e and "no `awaits` block" in e
             for e in errors), errors


def test_awaits_on_a_synchronous_tool_warns():
  """Dead config rather than a broken flow: the author has a timeout they think is
  protecting them."""
  errors, warnings = _pairing(asynchronous=False, with_awaits=True)
  assert errors == []
  assert any("is not asynchronous" in w for w in warnings), warnings


def test_a_matched_pair_is_clean():
  errors, warnings = _pairing(asynchronous=True, with_awaits=True)
  assert errors == []
  assert not [w for w in warnings if "asynchronous" in w], warnings


def test_a_progressive_legs_awaits_does_not_warn():
  """A lowered leg IS asynchronous, whatever its author's tool was declared as.

  Progressive lowering replaces the leg's tool with a generated wrapper emitted
  `executionType: ASYNCHRONOUS`, and `parallel()` merges `deadline`/`waiting_say`/
  `while_waiting` onto exactly those legs. Judged by the decorator registry the pair
  reads as dead config and warns on a correct group -- which is worse than silence,
  because it tells an author to delete the block that is working. Seen for real: a
  converted agent's four-leg group warned on every leg while the fan-out ran fine.
  """
  import flows
  from flows.authoring import tools as tool_registry
  saved = dict(tool_registry._REGISTRY)
  tool_registry._REGISTRY.clear()  # no leg's tool is a declared async tool
  try:
    f = flows.Flow("pair", root_agent="Pair_Agent")
    f.add(flows.user_slot("acct", ask="Account number?"),
          flows.result_slot("a_res", "leg_a"), flows.result_slot("b_res", "leg_b"))
    legs = [flows.task("leg_a", tool="check_a", inputs=["acct"], out_slot="a_res"),
            flows.task("leg_b", tool="check_b", inputs=["acct"], out_slot="b_res")]
    for leg in legs:
      leg["awaits"] = {"max_turns": 6}
    f.task(flows.parallel("diag", tasks=legs, progressive=True))
    errors, warnings = flows.validate_app(
        flows.App(root_flow=f, app_display_name="Pair"))
  finally:
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(saved)
  assert errors == []
  assert not [w for w in warnings if "is not asynchronous" in w], warnings


def _lint_awaits(awaits):
  """Lint a one-task flow whose awaits block is the thing under test.

  Section 8's behavioural tests drive the ENGINE directly, which never consults the
  validator — so a key the engine honours but the validator rejects passes every one
  of them and only fails at build. This closes that gap.
  """
  from flows.engine import blessed_source
  cfg = cfg_with(awaits=awaits)
  return blessed_source.lint_config(cfg, ["slow_lookup"])


def test_the_validator_accepts_a_while_waiting_ladder():
  errors = _lint_awaits({"max_turns": 4, "while_waiting": ["a", "b"]})["errors"]
  # The fixture task has no inputs, which draws its own unrelated complaint.
  assert [e for e in errors if "awaits" in e] == [], errors


def test_the_validator_rejects_a_ladder_longer_than_the_bound():
  errors = _lint_awaits({"max_turns": 1, "while_waiting": ["a", "b"]})["errors"]
  assert any("while_waiting has 2 lines" in e for e in errors), errors


def test_the_validator_rejects_a_non_string_ladder():
  errors = _lint_awaits({"max_turns": 4, "while_waiting": [1, 2]})["errors"]
  assert any("must be a list of strings" in e for e in errors), errors


def test_the_validator_still_rejects_an_unknown_awaits_key():
  errors = _lint_awaits({"max_turns": 4, "whlie_waiting": ["typo"]})["errors"]
  assert any("unknown keys" in e for e in errors), errors


# --------------------------------------------------------------------------- #
# 12. The simulator reports an outstanding wait
# --------------------------------------------------------------------------- #

def test_the_sim_reports_the_outstanding_wait():
  """`slot_inspection.awaiting_tasks` is how an offline harness sees a wait at all.

  It reads a private engine key, and a mismatch degrades to a field that is silently
  always empty rather than to an error — which is exactly what happened when this was
  first written against an older name for the marker.
  """
  from flows.sim import engine_sim

  cfg = cfg_with(awaits={"max_turns": 4})
  engine_sim.reset_store()
  session_id, _opening = engine_sim.start(cfg, "t")
  engine_sim.step({"session_id": session_id, "kind": "task_result",
                   "task_name": "lookup", "success": False, "result": dict(PENDING)})

  out = engine_sim.step({"session_id": session_id, "kind": "user_text", "text": ""})
  assert out["slot_inspection"]["awaiting_tasks"] == ["lookup"]


def test_the_sim_field_is_wired_to_the_engines_actual_key():
  """The pin. Both sides name the same sm key, so a rename breaks a test instead of
  quietly emptying the field."""
  import inspect
  from flows.sim import engine_sim

  src = inspect.getsource(engine_sim._slot_inspection)
  assert '"_awaiting_async"' in src or "'_awaiting_async'" in src
  assert "_awaiting_tasks" not in src


# --------------------------------------------------------------------------- #
# 13. `answer_first` — caller speech that lands ON the completion turn
# --------------------------------------------------------------------------- #

def cfg_answer_first(answer_first=None):
  """A wait whose completion unblocks a TERMINAL task — the case where the collision
  between a completion and caller speech actually bites."""
  awaits = {"max_turns": 9}
  if answer_first is not None:
    awaits["answer_first"] = answer_first
  return {
      "slots": [
          {"name": "acct", "source": "user", "setter": "set_acct", "ask": "Account?"},
          {"name": "status", "source": "task:poll"},
          {"name": "done", "source": "task:finish"},
      ],
      "tasks": [
          {"name": "poll", "tool": "poll_x", "inputs": ["acct"],
           "outputs": {"status_msg": "status"}, "success_check": "success",
           "terminal": False, "requires": ["acct"], "awaits": awaits},
          {"name": "finish", "tool": "finish_x", "inputs": ["status"],
           "outputs": {"closing": "done"}, "success_check": "success",
           "terminal": True, "requires": ["status"], "then_say": "{closing}"},
      ],
      "gate_slot": None,
  }


def _wait_started(cfg):
  sm = fresh(cfg)
  drive(cfg, sm, "", turn=1)
  sm.update(fb.run_intake("set_acct", {"stored": True, "value": "1"}, sm)["sm"])
  drive(cfg, sm, "1", turn=2)
  sm.update(fb.run_intake("poll_x", dict(PENDING), sm)["sm"])
  drive(cfg, sm, "", turn=3)
  assert "poll" in sm["_awaiting_async"]
  return sm


def _completion_lands(cfg, sm, text, turn):
  """The completion turn, with `text` as co-present caller speech."""
  sm.update(fb.run_intake(
      "poll_x", {"success": True, "status_msg": "all set"}, sm)["sm"])
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False, "event_data": {},
      "config_id": "t", "n_user_turns": turn, "async_completion_landed": True,
  })


def test_by_default_the_completion_turn_is_a_delivery_and_fires(d=None):
  """Unchanged behaviour: the speech is dropped so the terminal is not stranded."""
  cfg = cfg_answer_first()
  sm = _wait_started(cfg)
  out = _completion_lands(cfg, sm, "wait, one more thing", turn=4)

  assert (out["action"].get("function_call") or {}).get("name") == "finish_x"
  assert "_answer_first_left" not in out["sm"]


def test_answer_first_keeps_the_speech_and_holds_the_terminal():
  cfg = cfg_answer_first(answer_first=2)
  sm = _wait_started(cfg)
  out = _completion_lands(cfg, sm, "wait, one more thing", turn=4)

  assert out["action"].get("function_call") is None, "the terminal should be held"
  assert out["sm"]["_answer_first_left"] == 1
  assert any(e["tag"] == "answer_first_armed" for e in out["sm"]["_log"])


def test_the_budget_runs_down_and_the_terminal_then_fires():
  """The whole point of bounding it: the hold must END. Without the bound every later
  turn carries speech too, so the terminal is never re-fired and the flow never closes
  out — measured against a live app."""
  cfg = cfg_answer_first(answer_first=2)
  sm = _wait_started(cfg)
  out = _completion_lands(cfg, sm, "wait, one more thing", turn=4)
  sm = out["sm"]

  out = drive(cfg, sm, "and another thing", turn=5)
  assert (out["action"].get("function_call") or {}).get("name") == "finish_x"
  assert "_answer_first_left" not in out["sm"]
  assert any(e["tag"] == "answer_first_exhausted" for e in out["sm"]["_log"])


def test_a_quiet_turn_still_lets_the_terminal_fire_immediately():
  """The budget bounds the deferral; it does not force the flow to wait it out. With no
  speech there is nothing to defer for, so the terminal fires at once."""
  cfg = cfg_answer_first(answer_first=3)
  sm = _wait_started(cfg)
  out = _completion_lands(cfg, sm, "", turn=4)
  assert (out["action"].get("function_call") or {}).get("name") == "finish_x"


def test_the_budget_is_capped_by_the_engine():
  """A caller must not be able to hold a completed transaction open by talking."""
  eng = fb.load_engine()
  cfg = cfg_answer_first(answer_first=99)
  sm = _wait_started(cfg)
  out = _completion_lands(cfg, sm, "hello", turn=4)
  assert out["sm"]["_answer_first_left"] == eng._ANSWER_FIRST_CAP - 1


def test_the_budget_is_not_carried_across_a_flow_change():
  """It names one completion→terminal handover; surviving a flow change would bound a
  deferral in a config that no longer exists."""
  eng = fb.load_engine()
  assert "_answer_first_left" not in eng._FLOW_KEEP


def test_answer_first_authoring_surface():
  import flows
  assert "answer_first" not in flows.awaits(max_turns=4)
  assert flows.awaits(max_turns=4, answer_first=2)["answer_first"] == 2
  assert flows.awaits(max_turns=4, answer_first=2.0)["answer_first"] == 2
  for bad in (0, -1, 1.5, True, "2"):
    with pytest.raises(ValueError, match="answer_first"):
      flows.awaits(max_turns=4, answer_first=bad)


def test_the_validator_checks_answer_first():
  assert [e for e in _lint_awaits({"max_turns": 4, "answer_first": 2})["errors"]
          if "answer_first" in e] == []
  assert any("answer_first" in e
             for e in _lint_awaits({"max_turns": 4, "answer_first": 0})["errors"])


# --------------------------------------------------------------------------- #
# 14. Review follow-ups — the collision's sharper edges
# --------------------------------------------------------------------------- #

def test_answer_first_is_what_saves_a_control_intent_on_the_completion_turn():
  """"cancel" spoken as the backend replies.

  Cancel here is MODEL-driven — the model calls `cancel_flow`; there is no keyword path
  the engine could rescue it through. So the only question that matters is whether the
  model still has the floor on that turn. By default it does not: the engine preempts
  with the terminal fire and the caller's words go unheard. `answer_first` is the
  mitigation, and this pins the difference rather than asserting a cancel the fixture
  cannot produce.
  """
  cfg = cfg_answer_first()
  sm = _wait_started(cfg)
  preempted = _completion_lands(cfg, sm, "actually cancel that", turn=4)
  assert (preempted["action"].get("function_call") or {}).get("name") == "finish_x", (
      "default: the terminal takes the turn, so a co-present cancel cannot land")

  cfg = cfg_answer_first(answer_first=2)
  sm = _wait_started(cfg)
  kept = _completion_lands(cfg, sm, "actually cancel that", turn=4)
  assert kept["action"].get("function_call") is None, (
      "answer_first: the model must keep the floor so it can act on what was said")


def test_two_completions_in_one_turn_both_clear_their_waits():
  """The bug the batch resolver exists for. `slot_intake` writes a SCALAR
  `_task_just_completed` and the handler pops one, so the FIRST of two completions kept
  its entry in `_awaiting_async` and sat there until max_turns — a timeout for a backend
  that answered."""
  cfg = cfg_concurrent(extra_task=True)
  cfg["tasks"][1]["awaits"] = {"max_turns": 9}
  sm = fresh(cfg)
  start_wait(cfg, sm)
  sm.update(fb.run_intake("set_email", {"stored": True, "value": "a@b.c"}, sm)["sm"])
  drive(cfg, sm, "a@b.c", turn=3)
  sm.update(fb.run_intake("notify_x", dict(PENDING), sm)["sm"])
  drive(cfg, sm, "", turn=4)
  assert {"poll", "notify"} <= set(sm["_awaiting_async"]), sm.get("_awaiting_async")

  # Both land together, the way before_model ingests them.
  sm.update(fb.run_intake("poll_x", {"success": True, "status_msg": "ok"}, sm)["sm"])
  sm.update(fb.run_intake("notify_x", {"success": True, "ok": "sent"}, sm)["sm"])
  sm["_async_batch"] = ["poll", "notify"]
  out = drive(cfg, sm, "", turn=5)

  assert not out["sm"].get("_awaiting_async"), (
      "a sibling completion left its wait outstanding and will time out")


def test_the_ladder_does_not_quote_back_the_value_it_is_waiting_on():
  """`while_waiting` formatted against raw `filled` renders the placeholder the wait is
  about to replace — "Still checking on still checking"."""
  cfg = cfg_stale_consumer()
  cfg["tasks"][0]["awaits"]["while_waiting"] = ["Still checking on {status}."]
  sm = fresh(cfg)
  sm["filled"]["status"] = "pending"
  drive(cfg, sm, "", turn=1)
  sm.update(fb.run_intake("poll_x", dict(PENDING), sm)["sm"])

  spoken = drive(cfg, sm, "", turn=2)["action"]["message"]
  assert "pending" not in spoken, spoken


def test_the_validator_warns_when_a_wait_can_time_out_into_nothing():
  """No `on_timeout` and the give-up is silent: the wait is dropped, its output slots
  stay empty, and anything downstream of them never runs."""
  warnings = _lint_awaits({"max_turns": 3})["warnings"]
  assert any("on_timeout" in w for w in warnings), warnings
  quiet = _lint_awaits(
      {"max_turns": 3, "on_timeout": {"say": "Sorry."}})["warnings"]
  assert not [w for w in quiet if "on_timeout" in w], quiet


# --------------------------------------------------------------------------- #
# 15. The framework version stamp
# --------------------------------------------------------------------------- #

def test_the_bootstrap_version_and_the_manifest_agree():
  """Two places carry the framework version and only one is regenerated.

  `blessed_source._VERSION` is the bootstrap stamp; `manifest.json` is what
  `version()` actually returns once committed. `write_manifest()` with no argument
  PRESERVES the manifest's version, so bumping the constant alone is silently
  ineffective — and bumping the manifest alone leaves the constant lying.
  """
  from flows.engine import blessed_source as bs
  assert bs._VERSION == bs.manifest()["version"]
  assert bs.version() == bs._VERSION


def test_the_manifest_matches_the_bundle_on_disk():
  """The drift contract: editing a framework file without regenerating the manifest
  must fail here rather than at emit time."""
  from flows.engine import blessed_source as bs
  assert bs.manifest() == bs.compute_manifest()
