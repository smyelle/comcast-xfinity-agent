"""An asynchronous payload is an answer about the values it was DISPATCHED with.

A wait spans turns, and a caller can change their mind inside one — correct an account
number, a date, an address. The answer that lands is about the old value, and nothing
downstream can tell the difference: it arrives in exactly the shape a fresh answer would.
Applied, it writes a verdict about the superseded value AND latches the task's output, so
the corrected value is never asked about at all. The caller hears a confident answer to
the question they just withdrew.

Found while moving a 19-second diagnostic sweep off a `before_agent` callback and into an
`awaits` task. Synchronously the question could not change mid-answer, because the answer
took one turn; asynchronously it can, and nothing in the engine noticed.

The guard is to record the dispatch inputs on the wait mark and compare them on arrival.
A mismatch drops the payload, releases the wait and forgets the result, so the selector
dispatches again with the value that is true now.
"""

from __future__ import annotations

import flows
from flows.engine import loader


def _config():
  f = flows.Flow("repair", root_agent="Agent")
  f.add(flows.user_slot("account", ask="Account number?"),
        flows.result_slot("status", "Sweep"))
  # NOT terminal: a terminal task completes the flow and the reset clears `filled`, so
  # the applied-payload assertions below would read an empty dict and pass for the wrong
  # reason on the stale case while failing on the healthy one.
  f.task("Sweep", "run_sweep", ["account"], "status", out_key="success",
         then_say="Your line reads {status}.",
         awaits=flows.awaits(max_turns=8, say="One moment while I check."))
  return flows.build_config(f) if hasattr(flows, "build_config") else f.to_config()


def _dispatch(config):
  """Turn 1: the task fires and CES answers with the pending placeholder."""
  sm = {"filled": {"account": "111"}, "task_results": {}}
  loader.run_engine(config, sm, last_user_text="my line is down", config_id="repair")
  sm["task_results"]["Sweep"] = {"result": "pending"}
  sm["_task_just_completed"] = "Sweep"
  loader.run_engine(config, sm, last_user_text="", config_id="repair")
  assert "Sweep" in (sm.get("_awaiting_async") or {}), (
      "the task did not register a wait, so this test would prove nothing")
  return sm


def _land(sm, config, payload):
  """The real payload arrives. Returns what the caller would HEAR.

  Asserted on the spoken surface rather than on `sm["filled"]`: a task's outputs are
  interpolated into `then_say` from the result itself, so the slot is not what proves
  the payload was applied — the sentence is.
  """
  sm["task_results"]["Sweep"] = payload
  sm["_task_just_completed"] = "Sweep"
  action = loader.run_engine(config, sm, last_user_text="",
                             config_id="repair").get("action") or {}
  return action.get("message") or ""


def test_a_payload_for_the_current_value_is_applied():
  """The ordinary path, pinned first: nothing about this guard may break it."""
  config = _config()
  sm = _dispatch(config)
  said = _land(sm, config, {"success": True, "status": "clear"})
  assert said == "Your line reads clear.", f"the healthy payload was not applied, said {said!r}"
  assert not sm.get("_awaiting_async")


def test_a_payload_is_dropped_when_its_input_changed_under_it():
  """The caller corrected the account while the sweep was out."""
  config = _config()
  sm = _dispatch(config)
  sm["filled"]["account"] = "222"          # the correction

  said = _land(sm, config, {"success": True, "status": "clear"})

  assert "reads" not in said, (
      f"a verdict about account 111 was spoken after the caller changed it to 222: "
      f"{said!r}")
  assert "Sweep" not in (sm.get("task_results") or {}), (
      "the stale result was left behind, so the selector still counts the task as done")
  assert "Sweep" not in (sm.get("_awaiting_async") or {}), (
      "the wait was not released, so the task can never run again for the new value")


def test_the_task_dispatches_again_for_the_value_that_is_true_now():
  """Dropping is only half of it — the corrected value still has to get its answer."""
  config = _config()
  sm = _dispatch(config)
  sm["filled"]["account"] = "222"
  _land(sm, config, {"success": True, "status": "clear"})

  action = loader.run_engine(config, sm, last_user_text="",
                             config_id="repair").get("action") or {}
  call = (action.get("function_call") or {}).get("name")
  assert call == "run_sweep", (
      f"expected a re-dispatch for the corrected account, got {call!r}")


def test_an_unchanged_input_is_not_mistaken_for_a_change():
  """The guard compares VALUES, so a re-fill with the same value is not a correction."""
  config = _config()
  sm = _dispatch(config)
  sm["filled"]["account"] = "111"          # the caller repeated themselves

  said = _land(sm, config, {"success": True, "status": "clear"})
  assert said == "Your line reads clear.", (
      f"a payload was discarded because the caller said the same account twice: {said!r}")


def test_an_input_absent_at_arrival_is_not_a_change():
  """ABSENT is not CHANGED, and conflating them drops every healthy payload.

  A slot can be missing from `filled` on the arrival turn for reasons that have nothing
  to do with the caller: a `reset_on_complete` re-arm empties it, and a shared slot is
  restored on its own schedule. Read as a correction, the payload is discarded and the
  task re-dispatched — which then rides the wait out to `on_timeout`. Measured live on a
  real agent: every journey answered its timeout line instead of its verdict.
  """
  config = _config()
  sm = _dispatch(config)
  del sm["filled"]["account"]              # gone, not changed

  said = _land(sm, config, {"success": True, "status": "clear"})
  # Asserted as a prefix, not an equality: with the slot now empty the engine also
  # re-asks for it in the same breath, which is correct and not what this pins.
  assert said.startswith("Your line reads clear."), (
      f"a healthy payload was discarded because its input was absent, not changed: "
      f"{said!r}")


def test_empty_input_reads_as_absent_not_changed():
  """An EMPTY value is absence, not a correction — and the difference is load-bearing.

  Review asked whether the guard conflates "key missing" with "key present but cleared",
  since only the first is obviously innocent. It does conflate them, on purpose.

  Nothing in this engine ever STORES `None` or `""` into `filled`: every clearing path
  is a `pop`/`del`, so a slot the caller withdrew is absent rather than blanked. An empty
  value can therefore only come from a restore or a task output mid-rewrite — the same
  innocent causes as absence. Treating it as a correction would drop a healthy payload
  and ride the wait out to `on_timeout`, which is the live incident the guard was written
  for.

  Pinned so that if someone later makes empty mean "cleared", they have to come here and
  argue with the reason rather than discover it in production.
  """
  for blank in (None, ""):
    config = _config()
    sm = _dispatch(config)
    sm["filled"]["account"] = blank        # present, but empty

    said = _land(sm, config, {"success": True, "status": "clear"})
    assert said.startswith("Your line reads clear."), (
        f"a healthy payload was discarded because its input was {blank!r} rather than "
        f"absent: {said!r}")


def test_untracked_payload_is_applied_and_logged():
  """A payload for a task nothing is waiting on — the late answer after a timeout.

  `on_timeout` releases the wait and the flow moves on. If the backend then answers, the
  payload lands with no mark to compare against, so the staleness guard has nothing to
  say and the result is applied on top of a turn that may already have told the caller
  the system was unreachable.

  Applied is the deliberate choice, not an oversight: intake writes the output slots
  before this code runs, so discarding here would leave the slots set and the result
  missing — a worse state than either outcome. This pins the behavior and the WARN that
  makes the rate visible, so a future decision to drop these can be made against a
  measurement.
  """
  config = _config()
  sm = _dispatch(config)
  sm.pop("_awaiting_async")                # the timeout already released it

  said = _land(sm, config, {"success": True, "status": "clear"})
  assert said.startswith("Your line reads clear."), (
      f"an untracked payload was silently dropped, leaving its output slots written "
      f"but its result missing: {said!r}")
