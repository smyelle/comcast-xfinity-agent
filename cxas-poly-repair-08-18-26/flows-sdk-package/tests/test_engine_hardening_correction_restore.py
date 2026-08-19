"""Engine hardening — rejecting a correction must not lose the value it replaced.

A two-phase correction pops the previously CONFIRMED value out of `filled` at the
moment the phase-2 setter produces the new one, and stages the new value for readback.
If the caller then REJECTS that readback, the new value is discarded and the slot is
simply EMPTY — so the agent asks again for a number the caller already gave and never
withdrew. Over voice that reads as the agent having forgotten the last two turns.

`_correction_pending` is popped in the same pass it is applied, so nothing survived to
restore from. `_apply_correction_pending` now mirrors the old value into
`_correction_prior`, which outlives that pop, and settles the mirror once the staged
value leaves flight: on a confirm the new value is in `filled` and the mirror is just
dropped; on a reject the slot is empty and the old value goes back.

Mirroring in the engine, from the `old_value` the correction entry already carries,
keeps the restore working on its own. `slot_intake` writes the same mirror at the
moment it pops the value; the two agree by construction (same slot, same value), so
either half alone is enough and both together are idempotent.

Fully offline: no network, no creds, no LLM.

Run:
  cd /Users/fsamuel/Labs/cxas-labs
  PYTHONPATH=packages/flows/src .venv/bin/python -m pytest \
      packages/flows/tests/test_engine_hardening_correction_restore.py -q
"""

from __future__ import annotations

import pytest

import flows
from flows.engine import loader as fb

OLD, NEW = "9413", "9414"


@pytest.fixture(autouse=True)
def _drop_engine_caches():
  """The engine caches compiled configs process-globally, keyed by config id."""
  yield
  fb.clear_cache()


def _config() -> dict:
  f = flows.Flow("hardening_correction", root_agent="a")
  f.add(flows.user_slot("last_four", ask="Last four digits?", readback=True))
  f.task(flows.task("lookup", "do_lookup", ["last_four"], "order_status",
                    out_key="status"))
  f.add(flows.result_slot("order_status", "lookup"))
  f.set("correction_tool", "set_slot_change")
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _ = flows.validate_app(app)
  assert not errors, errors
  return app.root_flow.to_config()


def _turn(engine, config, sm, n):
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "",
      "scanned_user_text": "", "is_inactivity": False, "event_data": {},
      "config_id": "hardening_correction", "n_user_turns": n,
  })["action"]


def _staged_correction() -> tuple:
  """State at the moment the corrected value is read back for confirmation.

  The opening turn is not decoration: the engine seeds `_correction_tool` into the
  slot machine while running, and intake dispatches `set_slot_change` on that key.
  """
  config, engine = _config(), fb.load_engine()
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "hardening_correction"
    sm["filled"][gate] = "hardening_correction"
  engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "hello",
      "scanned_user_text": "hello", "is_inactivity": False, "event_data": {},
      "config_id": "hardening_correction", "n_user_turns": 1})
  sm["filled"]["last_four"] = OLD

  # Phase 1: the model reports the caller wants this slot changed.
  sm.update(fb.run_intake(
      "set_slot_change", {"slots": ["last_four"], "success": True}, sm)["sm"])
  _turn(engine, config, sm, 2)
  # Phase 2: the focused setter answers with the new value.
  setter = next(s["setter"] for s in config["slots"] if s["name"] == "last_four")
  sm.update(fb.run_intake(setter, {"stored": True, "value": NEW}, sm)["sm"])
  action = _turn(engine, config, sm, 3)
  return config, engine, sm, action


def _reject(sm):
  """The caller says the readback is wrong. `reject_pending` only marks the turn;
  before_agent clears the rejected values at the top of the next one."""
  fb.call_readback_tool("reject_pending", sm)
  for name in sm.pop("_rejection_snapshot", {}):
    sm.get("pending", {}).pop(name, None)
  sm.pop("_rejection_requested", None)


def _confirm(sm):
  fb.call_readback_tool("confirm_pending", sm)


def test_the_correction_is_staged_for_readback_and_the_old_value_is_mirrored():
  """Pins the premise of everything below: the old value really is out of `filled`
  (so a reject would leave the slot empty) and really is remembered."""
  _config_, _engine, sm, action = _staged_correction()

  assert sm["pending"]["last_four"] == NEW
  assert "last_four" not in sm["filled"]
  assert sm["_correction_prior"]["last_four"] == OLD
  assert NEW in (action.get("message") or ""), "the new value is read back"


def test_rejecting_the_correction_puts_the_previous_value_back():
  config, engine, sm, _ = _staged_correction()

  _reject(sm)
  _turn(engine, config, sm, 4)

  assert sm["filled"]["last_four"] == OLD, (
      "the slot was left empty, so the agent re-asks for a value it already had")
  assert any(e.get("tag") == "correction_rejected_restored"
             for e in sm.get("_log", []))


def test_the_restore_is_reported_with_the_value_it_put_back():
  """A value reappearing in `filled` with nothing in the log is indistinguishable
  from a re-collection when reading a transcript afterwards."""
  config, engine, sm, _ = _staged_correction()
  _reject(sm)
  _turn(engine, config, sm, 4)

  entry = next(e for e in sm["_log"]
               if e.get("tag") == "correction_rejected_restored")
  assert entry["data"] == {"slot": "last_four", "value": OLD}


def test_confirming_the_correction_keeps_the_new_value():
  """The other settle branch: the caller meant it, so the mirror must NOT fire —
  restoring here would undo the correction the caller just made."""
  config, engine, sm, _ = _staged_correction()

  _confirm(sm)
  _turn(engine, config, sm, 4)

  assert sm["filled"]["last_four"] == NEW
  assert not any(e.get("tag") == "correction_rejected_restored"
                 for e in sm.get("_log", []))


def test_the_mirror_is_dropped_once_it_has_settled():
  """It is one-shot either way. Left behind, a LATER pass that legitimately empties
  the slot would resurrect a stale value nobody asked for."""
  config, engine, sm, _ = _staged_correction()
  _reject(sm)
  _turn(engine, config, sm, 4)

  assert sm.get("_correction_prior") is None


def test_nothing_settles_while_the_correction_is_still_in_flight():
  """The readback turn itself must leave the mirror alone — the caller has not
  answered yet, and `filled` is legitimately empty for that slot in the meantime."""
  config, engine, sm, _ = _staged_correction()

  _turn(engine, config, sm, 4)      # another pass, still awaiting the answer

  assert sm["pending"]["last_four"] == NEW
  assert sm["_correction_prior"]["last_four"] == OLD
  assert "last_four" not in sm["filled"]
