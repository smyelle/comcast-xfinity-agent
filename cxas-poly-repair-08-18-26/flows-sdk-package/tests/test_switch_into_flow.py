"""Re-entering / switching into a mapped flow after a prior flow terminated must SERVE.

A router-over-flows app (a `flow_config_map` host) resolves the active flow to its
config EACH turn. A flow with `bootstrap.reset_on_complete` TERMINATES to
`status="zombie"`: `_terminate` wipes the gate slot and stashes the flow name in
`_zombie["flow"]`, handing control back to the router (a defer category records its
intent, speaks a hand-off, and ends). When the caller then re-fills the gate — a
`route_cues`/backstop match or a `set_active_flow`, for the SAME flow (another billing
question) or a DIFFERENT one (now the internet is down) — status is still "zombie", so
the terminal-status guard used to fall through to the router. The router config carries
no `reset_on_complete`, so the engine never reaps; status stays "zombie" with the gate
filled and the engine loops on `is_render_turn`, rendering the terminal prompt and
ignoring the caller — turn after turn. A FRESH open worked only because there was no
prior zombie to block it.

The fix reaps IN PLACE: the two config resolvers (`before_agent._resolve_config_id` at
turn start and `before_model._extract`'s mid-turn re-resolve) reap the zombie (carry
shared_values forward, drop `_zombie`) and re-arm status to `in_progress` BEFORE the
model/engine runs, then dispatch the flow's config. It must be in place, not by
resolving to the child config and leaning on the engine's own
`_reap_zombie_on_reentry`: that fixes the turn-start path but NOT the mid-turn one — a
model-driven router (comcast) fires the switch via `set_active_flow` mid-turn, so the
model runs on the router config, sees the zombie, and renders "having trouble" before
the engine reap can flip it (verified live). It fires for SAME-flow and cross-flow
re-entry alike. A plain terminal status with NO zombie has no scope to reap and is left
to the router; a PAUSED flow (`_flow_state`) is left to the resume path.

The REAL emitted callback source is compiled and driven against a fake CES context —
not a reimplementation of it.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_switch_into_flow.py
"""

from __future__ import annotations

import json
import pathlib

import flows


# ---------------------------------------------------------------------------
# Compile the two REAL callback modules and pull out the functions under test.
# The callbacks reference CES-injected type names only in annotations, so a bare
# object placeholder is enough to import them.
# ---------------------------------------------------------------------------


def _load(rel: str, **inject) -> dict:
  src = pathlib.Path(flows.__file__).parent / rel
  ns: dict = dict(inject)
  exec(compile(src.read_text(), str(src), "exec"), ns)  # noqa: S102
  return ns


_BA = _load("engine/framework/callbacks/before_agent.py",
            CallbackContext=object, Content=object)
_resolve_config_id = _BA["_resolve_config_id"]

_BM = _load("engine/framework/callbacks/before_model.py",
            CallbackContext=object, LlmRequest=object, LlmResponse=object)
_extract = _BM["_extract"]


_FMAP = {"billing": "billing_cfg", "repair": "repair_cfg"}


class _Ctx:
  """The two attributes the callbacks touch."""

  def __init__(self, state, events=None):
    self.state = state
    self.events = events or []


class _Req:
  """A minimal llm_request: only `.contents` is read (None-safe)."""

  def __init__(self, contents=None):
    self.contents = contents or []


def _router_state(active_config_id=None):
  state = {"flow_config_map": json.dumps(_FMAP), "default_config_id": "router_cfg"}
  if active_config_id is not None:
    state["_active_config_id"] = active_config_id
  return state


def _zombie_sm(gate_value, zombie_flow, *, status="zombie", shared=None,
               flow_state=None):
  sm = {
      "_gate_slot": "active_flow",
      "filled": {"active_flow": gate_value} if gate_value else {},
      "status": status,
      "_zombie": {"flow": zombie_flow},
  }
  if shared is not None:
    sm["_zombie"]["shared_values"] = shared
  if flow_state is not None:
    sm["_flow_state"] = flow_state
  return sm


# ═══════════════════════════════════════════════════════════════════════════
# before_agent._resolve_config_id — turn-START resolution
# ═══════════════════════════════════════════════════════════════════════════


def test_switch_into_a_different_flow_dispatches_and_reaps_in_place():
  """The bug repro: gate now names repair, prior billing flow zombie'd → reap in place
  (in_progress, drop _zombie, carry shared into filled) and dispatch repair_cfg."""
  sm = _zombie_sm("repair", "billing", shared={"guest_name": "Ada"})
  cfg, source = _resolve_config_id(_Ctx(_router_state()), sm)

  assert (cfg, source) == ("repair_cfg", "active_flow")   # the DAG drives, not the router
  assert sm["status"] == "in_progress"                    # re-armed
  assert "_zombie" not in sm                              # reaped
  assert sm["filled"]["guest_name"] == "Ada"             # shared slots survive the switch
  assert sm["filled"]["active_flow"] == "repair"         # gate preserved


def test_reentering_the_SAME_zombied_flow_also_dispatches_and_reaps():
  """Same-flow re-entry must serve too (Owl review). The gate is re-filled to the flow
  that just zombie'd (e.g. another billing question); leaving it on the router — which has
  no reset_on_complete — loops on is_render_turn. Reap in place and dispatch billing_cfg."""
  sm = _zombie_sm("billing", "billing", shared={"guest_name": "Ada"})
  cfg, source = _resolve_config_id(_Ctx(_router_state()), sm)

  assert (cfg, source) == ("billing_cfg", "active_flow")
  assert sm["status"] == "in_progress"                    # re-armed, not looping
  assert "_zombie" not in sm
  assert sm["filled"]["guest_name"] == "Ada"


def test_reentry_with_an_empty_zombie_flow_name_still_rearms():
  """The comcast steering deferral (a router record_path terminator) leaves an EMPTY
  _zombie["flow"], so a guard keyed on the flow NAME silently skips it and the turn loops
  on 'having trouble' (a live-found regression). Key on status=="zombie" instead: reap +
  re-arm + dispatch regardless of whether the zombie names a flow."""
  sm = _zombie_sm("repair", "")            # _zombie == {"flow": ""}, status == "zombie"
  cfg, source = _resolve_config_id(_Ctx(_router_state()), sm)

  assert (cfg, source) == ("repair_cfg", "active_flow")
  assert sm["status"] == "in_progress"
  assert "_zombie" not in sm


def test_a_paused_flow_is_left_to_the_resume_path():
  """A paused flow (`_flow_state`) is never disturbed by the re-arm — the resume path
  owns it."""
  sm = _zombie_sm("repair", "billing",
                  flow_state=[{"flow": "billing", "slots": {}}])
  cfg, source = _resolve_config_id(_Ctx(_router_state()), sm)

  assert (cfg, source) == ("router_cfg", "active_flow_router")
  assert sm["status"] == "zombie"
  assert sm["_zombie"]["flow"] == "billing"


def test_a_live_in_progress_flow_resolves_unchanged():
  """An in-progress flow already dispatched before the fix and still does — the re-arm
  block only ever fires on status=="zombie"."""
  sm = {"_gate_slot": "active_flow",
        "filled": {"active_flow": "repair"}, "status": "in_progress"}
  cfg, source = _resolve_config_id(_Ctx(_router_state()), sm)
  assert (cfg, source) == ("repair_cfg", "active_flow")
  assert sm["status"] == "in_progress"


def test_gate_empty_falls_to_router_and_clears_the_leftover_zombie():
  """After a terminate the gate is EMPTY (the caller said something the model did not
  re-route — e.g. another question of the SAME category the router treats as a
  continuation). Falling to the host router must CLEAR the leftover zombie so the router
  runs in a clean in_progress state and re-classifies. Otherwise status=='zombie' forces
  the router turn into the engine's terminal render ('having trouble') and the caller is
  ignored (a live-found loop). The steering record_path terminator leaves an EMPTY flow
  name, which is why the reap is keyed on status, not on _zombie['flow'] or cfg."""
  sm = _zombie_sm("", "")            # gate empty; _zombie == {"flow": ""}
  cfg, source = _resolve_config_id(_Ctx(_router_state()), sm)
  assert (cfg, source) == ("router_cfg", "active_flow_router")
  assert sm["status"] == "in_progress"                   # reaped so the router runs clean
  assert "_zombie" not in sm


def test_no_flow_config_map_is_byte_identical():
  """A non-router (multi-agent / single-flow) app has no flow_config_map, so the whole
  branch — and the re-arm inside it — is a no-op."""
  state = {"default_config_id": "solo_cfg"}
  sm = {"status": "zombie", "filled": {}, "_zombie": {"flow": "x"}}
  cfg, source = _resolve_config_id(_Ctx(state), sm)
  assert (cfg, source) == ("solo_cfg", "default")
  assert sm["status"] == "zombie"                         # untouched
  assert sm["_zombie"] == {"flow": "x"}


def test_completed_without_a_zombie_falls_to_the_router():
  """A terminal status with NO zombie is a flow that finished with its gate still set —
  there is no reset_on_complete scope to reap, so it is left to the router to re-route
  rather than re-armed. Mapped flows always reset_on_complete, so a completed-without-
  zombie mapped flow is not a shape the SDK emits; the guard errs to the router either
  way. Pins the slotfill emit test's 'flow finished -> router (even before the gate
  clears)' contract."""
  sm = {"_gate_slot": "active_flow",
        "filled": {"active_flow": "repair"}, "status": "complete"}
  cfg, source = _resolve_config_id(_Ctx(_router_state()), sm)
  assert (cfg, source) == ("router_cfg", "active_flow_router")
  assert sm["status"] == "complete"                      # untouched — not re-armed


# ═══════════════════════════════════════════════════════════════════════════
# before_model._extract — MID-turn re-resolve (serve on the switch turn)
#
# This is the path that matters for a model-driven router: the model calls
# set_active_flow mid-turn, so the re-arm must flip status BEFORE the model/engine runs.
# ═══════════════════════════════════════════════════════════════════════════


def test_before_model_midturn_switch_serves_on_the_same_turn():
  """On the switch turn the gate is set MID-turn (route_backstop / set_active_flow) while
  status is still zombie. The mid-turn re-resolve must reap + re-arm in place so the
  engine runs the destination flow on THIS turn, not the next one."""
  state = _router_state(active_config_id="router_cfg")
  sm = _zombie_sm("repair", "billing", shared={"guest_name": "Ada"})
  turn_input = _extract(_Ctx(state), _Req([]), sm)

  assert turn_input["config_id"] == "repair_cfg"          # engine runs repair this turn
  assert state["_active_config_id"] == "repair_cfg"
  assert turn_input["sm"]["status"] == "in_progress"      # re-armed before the model runs
  assert "_zombie" not in turn_input["sm"]
  assert turn_input["sm"]["filled"]["guest_name"] == "Ada"


def test_before_model_same_flow_reentry_also_serves():
  """Same-flow re-entry mid-turn reaps + re-arms + re-resolves to its config too (Owl
  review) — else the router keeps the config, the zombie is never reaped, and the turn
  loops on 'having trouble'."""
  state = _router_state(active_config_id="router_cfg")
  sm = _zombie_sm("billing", "billing")
  turn_input = _extract(_Ctx(state), _Req([]), sm)

  assert turn_input["config_id"] == "billing_cfg"
  assert turn_input["sm"]["status"] == "in_progress"
  assert "_zombie" not in turn_input["sm"]


def test_before_model_paused_flow_not_disturbed():
  """A paused flow is left to the resume path mid-turn too."""
  state = _router_state(active_config_id="router_cfg")
  sm = _zombie_sm("repair", "billing",
                  flow_state=[{"flow": "billing", "slots": {}}])
  turn_input = _extract(_Ctx(state), _Req([]), sm)

  assert turn_input["config_id"] == "router_cfg"
  assert turn_input["sm"]["status"] == "zombie"
