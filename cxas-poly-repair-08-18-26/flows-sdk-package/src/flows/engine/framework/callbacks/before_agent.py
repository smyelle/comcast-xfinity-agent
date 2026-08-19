# pylint: disable=invalid-name,undefined-variable,unused-argument,broad-exception-caught,line-too-long
"""Before-agent callback — SM init and config resolution.

FRAMEWORK CODE — fully generic across all agents.
Config-driven: reads config_id from agent_config_map variable.
Runs once per turn: initializes sm and resolves config_id. The per-call SI/prompt
assembly is handled by before_model_callback (via the slot_filling_engine).
"""

import copy as copy_lib
import json as json_lib
import logging
import traceback
from typing import Any, Optional


_SM_KEY = "sm"

_LEVEL_MAP = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
              "WARN": logging.WARNING, "ERROR": logging.ERROR}
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
_logger = logging.getLogger("slot_filling.before_agent")


# sm["_log"] is serialized into session state every turn; cap it (ring buffer).
_LOG_CAP = 200


def _log(sm, tag, level="INFO", **data):
  """Emit structured log entry; append to sm["_log"].

  Args:
    sm: Session state machine dict (callback_context.state).
    tag: Short label identifying the log event.
    level: Severity — DEBUG, INFO, WARN, or ERROR.
    **data: Arbitrary key-value payload for the log entry.
  """
  min_level = sm.get("_log_level", "INFO")
  if _LEVEL_ORDER.get(level, 1) < _LEVEL_ORDER.get(min_level, 1):
    return
  entry = {"src": "before_agent", "tag": tag, "level": level,
           "data": {k: v for k, v in data.items() if v is not None}}
  _logger.log(_LEVEL_MAP.get(level, logging.INFO),
              json_lib.dumps(entry, default=str))
  _lst = sm.setdefault("_log", [])
  _lst.append(entry)
  if len(_lst) > _LOG_CAP:
    del _lst[:-_LOG_CAP]


_SM_DEFAULTS = {
    "filled": {},
    "pending": {},
    "status": "in_progress",
    "task_results": {},
}


def _ensure_sm_initialized(sm: dict[str, Any]) -> None:
  if sm.get("_initialized"):
    return
  for key, value in _SM_DEFAULTS.items():
    # COPY the container. `setdefault(key, value)` installs the module-level dict
    # BY REFERENCE, so every slot machine in the process aliases ONE `filled` /
    # `pending` / `task_results`: one caller's account number is already present
    # in the next caller's brand-new session — and a pre-filled slot skips its
    # readback, so nothing surfaces the leak.
    sm.setdefault(key, copy_lib.deepcopy(value))
  sm["_initialized"] = True


def _resolve_config_id(callback_context, sm=None):
  """Derive config_id from the agent_config_map variable + transfer events.

  Args:
    callback_context: CES CallbackContext with state and events.
    sm: the slot machine (for the `active_flow` gate value); optional for callers
      that only need the agent/transfer resolution.

  Returns:
    Tuple of (config_id, source) where source describes how it was resolved.
  """
  sm = sm or {}
  # Inside a component sub-flow (the engine descended via a component / repeated over-each loop): the active
  # config is the child the engine is running (`sm._config_id`), tracked on the call frame. The child scope
  # does NOT carry the parent's `active_flow` gate, so a single-agent router's flow_config_map resolution
  # below would see "no active flow" and re-resolve to the router host — running the child's collection turn
  # against the wrong config (its slot never offered → empty "having trouble" render). Respect the live frame
  # so the child runs to completion; when it returns, the frame pops and normal resolution resumes. No call
  # stack ⇒ this is a no-op (byte-identical for every non-component flow).
  if sm.get("_call_stack") and sm.get("_config_id"):
    _cid = sm["_config_id"]
    callback_context.state["_active_config_id"] = _cid
    return _cid, "component_frame"
  agent_name = None
  for event in reversed(callback_context.events):
    for part in (event.parts() or []):
      fc = getattr(part, "function_call", None)
      if fc and fc.name == "transfer_to_agent":
        agent_name = fc.args.get("agent_name") or fc.args.get("agent")
        break
    if agent_name:
      break

  if agent_name:
    raw_map = callback_context.state.get("agent_config_map", "{}")
    try:
      config_map = (
          json_lib.loads(raw_map) if isinstance(raw_map, str) else raw_map
      )
    except Exception:
      config_map = {}
    config_id = config_map.get(agent_name)
    if config_id:
      callback_context.state["_active_config_id"] = config_id
      return config_id, "transfer_event"

  # Single-agent router->flow activation. When the app declares a `flow_config_map`
  # (a router host whose flows are IN-AGENT configs, not separate agents), the
  # `active_flow` gate is authoritative: derive config from it EACH turn so setting
  # active_flow=X runs X's DAG in-agent, and clearing it (a flow's
  # bootstrap.reset_on_complete) returns to the router. This is the flow-keyed analog
  # of the agent-keyed transfer path above; it runs BEFORE `cached` so it overrides a
  # stale router/flow config. Absent flow_config_map it is a no-op and resolution
  # falls through unchanged -> byte-identical for every multi-agent/single-flow app.
  raw_fmap = callback_context.state.get("flow_config_map")
  if raw_fmap:
    try:
      fmap = json_lib.loads(raw_fmap) if isinstance(raw_fmap, str) else raw_fmap
    except Exception:
      fmap = {}
    gate = sm.get("_gate_slot") or "active_flow"
    active_flow = (sm.get("filled") or {}).get(gate)
    status = sm.get("status", "in_progress")
    cfg = fmap.get(active_flow) if active_flow else None
    # ── Clear a leftover zombie once control has left a terminated flow. ────────
    # A flow with bootstrap.reset_on_complete TERMINATES to status="zombie": _terminate
    # wipes the gate slot and stashes a _zombie marker, handing control back to the router
    # (e.g. a defer category records its intent, speaks a hand-off, ends). The flow already
    # rendered its terminal on its OWN turn; by the time this resolver runs a fresh turn,
    # control has moved on and the zombie is stale. The router config carries no
    # reset_on_complete, so the engine's own _reap_zombie_on_reentry never runs, and a
    # lingering status=="zombie" forces the NEXT turn — whichever config it lands on — into
    # the engine's is_render_turn terminal path, so it renders "having trouble" and ignores
    # the caller. Two cases, one fix — reap the zombie IN PLACE (carry any shared_values
    # into filled, drop _zombie, back to in_progress):
    #   * gate RE-FILLED (a route_cues/backstop match or set_active_flow, SAME flow or a
    #     DIFFERENT one) -> cfg resolves -> the guard below dispatches the flow's DAG.
    #   * gate EMPTY (the caller said something the model did NOT re-route — e.g. another
    #     question of the same category the router treats as a continuation) -> cfg is None
    #     -> we fall to the host router below, which now runs in a CLEAN in_progress state
    #     and re-classifies the caller instead of looping on the terminal render.
    # It must be in place, not by resolving to the child config and leaning on the engine's
    # own reap: that fixes the turn-START path but NOT the MID-turn one — a model-driven
    # router fires set_active_flow mid-turn, so the model runs on the router config, sees
    # the zombie, and renders "having trouble" BEFORE the engine reap can flip it (verified
    # live: it regressed the switch). Keyed on status=="zombie" (the reapable state, set
    # with _zombie by _terminate), NOT on cfg or _zombie["flow"] being populated — the
    # steering record_path terminator wipes the gate AND leaves an EMPTY flow name, so a
    # cfg-gated or flow-name-gated guard silently skipped exactly the return-to-router case
    # (both verified live). A "complete"/"escalated" status (no zombie to reap) is left
    # alone. Never while PAUSED (_flow_state → the resume-offer path owns the paused flow).
    # Absent flow_config_map this branch is unreachable, so every multi-agent/single-flow
    # app is byte-identical.
    if status == "zombie" and not sm.get("_flow_state"):
      _carried = dict((sm.get("_zombie") or {}).get("shared_values", {}))
      sm.pop("_zombie", None)
      sm["status"] = "in_progress"
      status = "in_progress"
      if _carried:
        sm.setdefault("filled", {}).update(_carried)
      # ── Re-entry breadcrumb (post-deferral). We just reaped a terminated flow. When
      # the gate is EMPTY (return-TO-ROUTER: the defer wiped it and the caller said
      # nothing the router re-routed), leave a one-shot marker naming the last routed
      # intent so the engine's router short-circuit can COMPEL a fresh classification of
      # the caller's follow-up instead of dead-ending on a byte-identical "cold" router
      # turn. `detected_intent` / `detected_path` are written to session state by the
      # steering recorder and survive the terminate + reap. `_reentry_count` bounds a
      # record-and-terminate defer that keeps re-zombie-ing (reset below when a live flow
      # actually engages). Gate REFILLED (a re-route) skips this — the dispatch branch
      # below resets instead. No detected intent ⇒ nothing to re-enter ⇒ no marker.
      if not active_flow:
        _dp = str(callback_context.state.get("detected_path") or "").strip()
        _det = (_dp.split("/")[0] if _dp
                else str(callback_context.state.get("detected_intent") or "").strip())
        if _det:
          sm["_router_reentry_intent"] = _det
          sm["_reentry_count"] = int(sm.get("_reentry_count", 0) or 0) + 1
    if cfg and status not in ("complete", "zombie", "escalated"):
      # A live non-terminal flow is engaging (a REAL handler, not a record-and-terminate
      # defer that re-zombies within its own turn) — the re-entry loop is over, so drop
      # the breadcrumb + counter. A pure defer never reaches here (it activates mid-turn
      # in before_model and re-zombies), so its counter climbs to the cap and escalates.
      sm.pop("_reentry_count", None)
      sm.pop("_router_reentry_intent", None)
      callback_context.state["_active_config_id"] = cfg
      return cfg, "active_flow"                     # run the active flow's DAG in-agent
    # No active flow (or it just finished) -> the router host, authoritatively:
    # override any stale cached flow config so the next request re-routes cleanly.
    default_id = callback_context.state.get("default_config_id")
    if default_id:
      callback_context.state["_active_config_id"] = default_id
      return default_id, "active_flow_router"

  cached = callback_context.state.get("_active_config_id")
  if cached:
    return cached, "cached"

  # Variable-driven entry selection: an upstream intent tag may pick a hosted flow
  # other than the default. Generic — the app supplies the intent->config_id map
  # (intent_config_map); the tag arrives in ENTRY_INTENT. Absent either, this is a
  # no-op and resolution falls through unchanged. Turn-1 only (cached thereafter).
  raw_imap = callback_context.state.get("intent_config_map")
  if raw_imap:
    try:
      imap = json_lib.loads(raw_imap) if isinstance(raw_imap, str) else raw_imap
    except Exception:
      imap = {}
    intent = str(callback_context.state.get("ENTRY_INTENT") or "").strip().lower()
    config_id = imap.get(intent)
    if config_id:
      callback_context.state["_active_config_id"] = config_id
      return config_id, "entry_intent"

  raw_map = callback_context.state.get("agent_config_map", "{}")
  try:
    config_map = (
        json_lib.loads(raw_map) if isinstance(raw_map, str) else raw_map
    )
  except Exception:
    config_map = {}
  if len(config_map) == 1:
    config_id = next(iter(config_map.values()))
    callback_context.state["_active_config_id"] = config_id
    return config_id, "single_entry_map"

  # Fall back to the configured root/default config (e.g. the Host router) when
  # nothing else resolves — i.e. a cold turn-1 at the root agent, before any
  # transfer event. Lets the root run the framework (and its DAG) like any agent.
  default_id = callback_context.state.get("default_config_id")
  if default_id:
    callback_context.state["_active_config_id"] = default_id
    return default_id, "default"

  return None, None


def _before_agent_body(callback_context, sm):
  """Per-turn work wrapped by before_agent_callback's crash envelope: SI-capture
  reset, config resolution, deferred rejection. Caller persists sm afterwards."""
  _ensure_sm_initialized(sm)

  # ── SI inspector (debug): once-per-turn reset of the capture buffer ──
  # `capture_si` is an app variable the client (cxas chat / chat-step --with-si)
  # sets to have before_model record the exact system instruction the LLM sees
  # on each pass this turn. Off by default ⇒ no overhead. Reset here because
  # before_agent runs exactly once per user turn, so each turn's trace is fresh.
  if callback_context.state.get("capture_si"):
    sm["_capture_si"] = True
    sm["_si_trace"] = []  # fresh buffer per turn (before_agent runs once/turn)
  else:
    sm.pop("_capture_si", None)
    sm.pop("_si_trace", None)
    sm.pop("_si_turn", None)

  config_id, config_source = _resolve_config_id(callback_context, sm)

  callback_context.state["_active_sm_key"] = _SM_KEY
  if config_id:
    _log(sm, "config_resolved", config_id=config_id, source=config_source)
    callback_context.state["_active_config_id"] = config_id

  # ── Deferred rejection ───────────────────────────────────────
  if "_rejection_snapshot" in sm:
    snapshot = sm.pop("_rejection_snapshot")
    sm.pop("_rejection_requested", None)
    sm["_progress_turns"] = 0
    sm.pop("_readback_stall", None)
    sm.pop("_active_readback", None)
    pending = sm.get("pending", {})
    for k in snapshot:
      pending.pop(k, None)
    sm["pending"] = pending
    _log(sm, "rejection_applied", slots=list(snapshot.keys()))

  callback_context.state[_SM_KEY] = sm


def before_agent_callback(
    callback_context: CallbackContext,
) -> Optional[Content]:
  """Runs once per user turn before static variable substitution.

  Crash envelope: an uncaught exception in the per-turn body would surface to the
  user as the empty "having trouble" render. Log it to sm._log and proceed with
  state intact instead of propagating (mirrors the before_model / after_tool
  envelopes).
  """
  # Clear any pending transfer flag since we have successfully arrived!
  callback_context.state.pop("_pending_transfer", None)
  sm = callback_context.state.get(_SM_KEY, {})
  try:
    _before_agent_body(callback_context, sm)
  except Exception as _e:  # pylint: disable=broad-except
    _log(sm, "before_agent_CRASH", level="ERROR", err=repr(_e),
         tail=traceback.format_exc().strip().splitlines()[-1][:160])
    callback_context.state[_SM_KEY] = sm
  return None
