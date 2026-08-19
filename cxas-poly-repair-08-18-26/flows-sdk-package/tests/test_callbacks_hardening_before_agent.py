"""A brand-new session must not start out holding the LAST caller's slots.

`_SM_DEFAULTS` is a module-level dict of literal containers and
`_ensure_sm_initialized` installed them with `sm.setdefault(key, value)` — BY
REFERENCE. Every slot machine in the process therefore aliased the SAME
`filled` / `pending` / `task_results`: session A wrote a caller's account number
and session B, on its very first turn, already had it. Nothing surfaced the
leak either, because a pre-filled slot is never asked for and so never read back.

The blessed callbacks run inside CES, where `CallbackContext` / `Content` are
runtime-injected globals, so a plain import raises `NameError` on the first
annotated `def` that names one. Everything above that line is fully built by
then — exec the module, swallow the error, drive the helpers directly (the house
pattern, see `test_callbacks_before_model.py` on the sweep branch).
"""
from __future__ import annotations

import importlib.util
import os

from flows.engine import blessed_source as _bs


def _load():
  path = os.path.join(_bs._CALLBACKS_DIR, "before_agent.py")
  spec = importlib.util.spec_from_file_location("_ba_hardening", path)
  mod = importlib.util.module_from_spec(spec)
  try:
    spec.loader.exec_module(mod)
  except Exception:  # CES globals are undefined at import time; expected.
    pass
  return mod


_BA = _load()


def test_two_sessions_do_not_share_their_filled_slots():
  """The leak, at its smallest: one caller's account number in the next call."""
  caller_a, caller_b = {}, {}
  _BA._ensure_sm_initialized(caller_a)
  caller_a["filled"]["account_number"] = "794655102288"

  _BA._ensure_sm_initialized(caller_b)
  assert caller_b["filled"] == {}, "a fresh session started already filled"


def test_every_default_container_is_per_session():
  a, b = {}, {}
  _BA._ensure_sm_initialized(a)
  _BA._ensure_sm_initialized(b)
  for key in ("filled", "pending", "task_results"):
    assert a[key] is not b[key], key


def test_the_module_level_defaults_are_never_mutated():
  """The aliasing was permanent for the life of the process: the write landed on
  the module constant itself, so every session created afterwards inherited it."""
  sm = {}
  _BA._ensure_sm_initialized(sm)
  sm["filled"]["ssn"] = "1234"
  sm["pending"]["dob"] = "1990-01-01"
  sm["task_results"]["lookup"] = {"success": True}
  assert _BA._SM_DEFAULTS == {"filled": {}, "pending": {},
                              "status": "in_progress", "task_results": {}}


def test_a_scalar_default_is_still_installed():
  sm = {}
  _BA._ensure_sm_initialized(sm)
  assert sm["status"] == "in_progress"
  assert sm["_initialized"] is True


def test_it_never_wipes_an_sm_that_is_already_carrying_state():
  """Idempotence: before_agent runs once per TURN, not once per call."""
  sm = {}
  _BA._ensure_sm_initialized(sm)
  sm["filled"]["acct"] = "1"
  sm["status"] = "zombie"
  _BA._ensure_sm_initialized(sm)
  assert sm["filled"] == {"acct": "1"}
  assert sm["status"] == "zombie"


def test_the_initialized_short_circuit_still_returns_early():
  """An sm already marked initialized is left exactly as it is — the flag is what
  keeps a mid-flow turn from re-seeding containers the engine is using."""
  sm = {"_initialized": True}
  _BA._ensure_sm_initialized(sm)
  assert sm == {"_initialized": True}


def test_a_caller_supplied_container_is_kept_not_replaced():
  """setdefault semantics are preserved: a pre-seeded `filled` (a transfer-in
  carrying slots) must survive initialization."""
  seeded = {"acct": "9"}
  sm = {"filled": seeded}
  _BA._ensure_sm_initialized(sm)
  assert sm["filled"] is seeded
