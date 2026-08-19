# pylint: disable=invalid-name,undefined-variable,unused-argument,broad-exception-caught,line-too-long,g-doc-args,g-doc-return-or-yield,g-docstring-missing-newline,g-no-space-after-docstring-summary,g-short-docstring-punctuation,missing-function-docstring,protected-access
"""slot_intake tool — apply a tool's result to the slot-machine state.

FRAMEWORK CODE — fully generic across all agents. The intake half of the
slot-filling system: the engine decides what to collect; this records what a
setter / task / flow tool returned into sm. Called once per tool result by the
after_tool_callback CES adapter (it owns the CES types; this stays pure).

slot_intake() is a thin DISPATCHER on tool_name; each branch is a focused
`_intake_*` handler that takes (sm, ...) plus the shared `out`
({transfer_slots, pending_transfer}) and returns {"sm": sm, **out}.
"""

import json as json_lib
import logging
import traceback
from typing import Any


_BOOTSTRAP_KEYS = frozenset({
    "stored", "value", "target_agent", "error", "error_code",
})


# ── Flow-scope lifecycle — see slot_filling_engine for the full rationale.
# Per-flow state is thrown away by clearing EVERYTHING not in the keep-set, so a
# new sm key is per-flow by default. DUPLICATED VERBATIM from slot_filling_engine
# (CES tools can't import each other); a unit test asserts the copies are identical.
_KEEP_SESSION = frozenset({
    "channel", "_flow_state", "_flow_instance_seq", "_log", "_log_level",
    "_shared_slots",
    "_capture_si", "_si_trace", "_si_turn",
})
_KEEP_CONFIG = frozenset({
    "_config_id", "_bootstrap", "_gate_slot", "_single_flow", "_flow_types",
    "_route_cues", "_cancel_tool", "_escalate_tool", "_intent_first",
    "_default_flow", "_intent_switch", "_intent_changed_tool",
    "_first_engine_run",
})
_KEEP_TRANSITION = frozenset({
    "_zombie", "_resume_offer_pending", "_resume_offer_turn", "_resume_result",
    "_resume_target", "_auto_resume_deferred", "_pending_switch",
    "_router_dispatched",
    # Component call frame (sub-DAG): call stack + one-shot rebind-guard signal +
    # deferred parent-fail flag. Member-identical to the engine copy (asserted by
    # the H-KEEP dual-copy test).
    "_call_stack", "_frame_transition", "_fail_parent_flow",
})
_FLOW_KEEP = _KEEP_SESSION | _KEEP_CONFIG | _KEEP_TRANSITION


def _wait_clock(sm):
  """Turns a WAIT has had a chance on: caller turns plus inactivity ticks.

  DUPLICATED VERBATIM from slot_filling_engine (CES tools cannot import each other); a
  unit test asserts the two agree. Both halves are written by the engine — intake only
  reads them, to stamp a wait mark the engine will later measure. See the engine's copy
  for why `_turn_n` alone is the wrong unit here.
  """
  return int(sm.get("_turn_n") or 0) + int(sm.get("_tick_n") or 0)


def _flow_clear(sm):
  """Throw away ALL per-flow state in ONE shot (see slot_filling_engine._flow_clear).
  Pops every sm key not in the keep-set, then re-inits the empty core containers.
  Callers seed the gate slot / carried shared values / status afterwards."""
  for k in [k for k in sm if k not in _FLOW_KEEP]:
    del sm[k]
  sm["filled"] = {}
  sm["pending"] = {}
  sm["deferred"] = {}
  sm["task_results"] = {}


# The core slot containers _intake_bootstrap seeds itself (gate slot + carried
# shared values + paused-instance restore); everything ELSE per-flow is internal
# state (counters, derived setter-maps, intent/correction flags) that must be wiped
# on a transition. _flow_clear_internals does exactly that, leaving the slot maps
# for the bootstrap's own (already-correct) seeding logic.
_FLOW_SLOT_KEYS = frozenset({
    "filled", "pending", "deferred", "task_results", "status",
})

# The config-derived setter/task lookups (written by the engine's
# _stash_after_tool_mappings, guarded on "_setter_slots" not in sm). They describe
# the CONFIG, not the flow, so a transition must NOT wipe them: a second tool of
# the SAME turn (the caller answering while switching — "now a widget, it's W-5")
# reaches intake with no engine pass in between, and without these maps its value
# is routed nowhere and lost silently. A genuine config change (cross-agent hop /
# component descent) pops them in the engine before re-deriving, so keeping them
# here can never leave another config's setters in place.
_DERIVED_SETTER_KEYS = frozenset({
    "_setter_slots", "_multi_setter_slots", "_slot_requires",
    "_slot_validates", "_executor_tasks",
})


def _flow_clear_internals(sm):
  """Discard per-flow INTERNAL state on a flow transition (the source of the
  cross-flow stale-state leaks), leaving the core slot maps to the caller."""
  for k in [k for k in sm
            if k not in _FLOW_KEEP and k not in _FLOW_SLOT_KEYS
            and k not in _DERIVED_SETTER_KEYS]:
    del sm[k]


_LEVEL_MAP = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
              "WARN": logging.WARNING, "ERROR": logging.ERROR}
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
_logger = logging.getLogger("slot_filling.after_tool")


# sm["_log"] is serialized into session state every turn; cap it (ring buffer).
_LOG_CAP = 200


def _log(sm, tag, level="INFO", **data):
  """Emit a structured log entry (level DEBUG/INFO/WARN/ERROR) to sm["_log"]."""
  min_level = sm.get("_log_level", "INFO")
  if _LEVEL_ORDER.get(level, 1) < _LEVEL_ORDER.get(min_level, 1):
    return
  entry = {"src": "after_tool", "tag": tag, "level": level,
           "data": {k: v for k, v in data.items() if v is not None}}
  _logger.log(_LEVEL_MAP.get(level, logging.INFO),
              json_lib.dumps(entry, default=str))
  _lst = sm.setdefault("_log", [])
  _lst.append(entry)
  if len(_lst) > _LOG_CAP:
    del _lst[:-_LOG_CAP]


def _drop_recollect(sm, slot):
  """Remove a slot from the correction re-collect set once it's resolved
  (a setter produced a value or surfaced an error for it). Clears the key when
  empty so the engine's focus pass ends."""
  recollect = sm.get("_correction_recollect")
  if not recollect or slot not in recollect:
    return
  remaining = [s for s in recollect if s != slot]
  if remaining:
    sm["_correction_recollect"] = remaining
  else:
    sm.pop("_correction_recollect", None)


def _stage_setter_value(sm, slot, value):
  """Stage a setter's value.

  Phase 2 of a two-phase correction: if this slot was named by set_slot_change
  (still in _correction_recollect) and is still confirmed (in filled), the
  setter has now produced the new value — route it as a correction so the engine
  clears downstream + reads back. Otherwise stage into pending normally. The
  old confirmed value is only popped HERE, once the new value exists.

  The popped value is ALSO mirrored into `_correction_prior` (slot -> old value),
  which outlives the engine's one-shot pop of `_correction_pending`: if the caller
  then REJECTS the correction readback, the slot would otherwise be empty and the
  agent would re-ask a value it had already confirmed. Per-flow by design (no
  keep-set entry), so a switch discards it with the rest of the scope.
  """
  recollect = sm.get("_correction_recollect") or []
  filled = sm.get("filled", {})
  if slot in recollect and slot in filled:
    old_value = filled.pop(slot, None)
    sm.setdefault("_correction_pending", []).append({
        "slot": slot, "value": value,
        "old_type": type(old_value).__name__, "old_value": old_value,
    })
    sm.setdefault("_correction_prior", {})[slot] = old_value
    _log(sm, "correction_recollect_applied", slot=slot, value=value)
  else:
    sm.setdefault("pending", {})[slot] = value
  _drop_recollect(sm, slot)


def _resolve_correction_slot(guess, sm):
  """Map the model's slot guess in set_slot_change to a real slot.

  Resolves against ALL known user slots that have a setter (filled, pending,
  deferred, OR still unfilled — set_slot_change can name an unfilled slot to ADD
  it). The model often passes the setter-name root instead of the slot name —
  e.g. "takeout_guest_name" (from set_takeout_guest_name) for "guest_name" — so
  accept the slot name itself, its setter, or the setter without the "set_"
  prefix. Returns the resolved slot name, or None.
  """
  g = str(guess).lower().strip()
  setter_of = {}
  for setter, slot in (sm.get("_setter_slots") or {}).items():
    setter_of[slot] = setter
  for setter, field_map in (sm.get("_multi_setter_slots") or {}).items():
    for _field, slot in field_map.items():
      setter_of[slot] = setter
  if g in setter_of:
    return g
  for slot, setter in setter_of.items():
    s = str(setter).lower()
    aliases = {s}
    if s.startswith("set_"):
      aliases.add(s[4:])
    if g in aliases:
      return slot
  return None


# ─────────────────────────────────────────────────────────────────────
# PER-TOOL INTAKE HANDLERS — one per slot_intake() dispatch branch.
# Each is a pure (sm, ...) reduction returning {"sm": sm, **out}.
# ─────────────────────────────────────────────────────────────────────


def _intake_intent_changed(sm, response_data, out):
  """set_intent_changed (two-phase intent recognition, phase 1).

  set_intent_changed is a SETTER for the passive intent_changed slot — filling it
  flags "the guest wants something other than the current details" and switches
  the next pass into the focused intent pass (engine shows ONLY the real
  control/transition tools). Modeled as a setter (not a flag) so the model's call
  is non-terminal and CES gives us the phase-2 pass; phase 2 (the model picking a
  real tool) clears the flag. Store to pending exactly like cancel_flow stores to
  pending["cancel"], then record the classification (seed _correction_recollect
  for a slot correction, else stash _classified for before_model's phase-2 inject).
  """
  sm.setdefault("pending", {})["intent_changed"] = True
  intent = response_data.get("intent")
  target = response_data.get("target", "")
  if intent == "correct":
    recollect = []
    for nm in [s.strip() for s in str(target).split(",") if s.strip()]:
      resolved = _resolve_correction_slot(nm, sm)
      if (resolved and resolved not in (sm.get("_gate_slot"), "cancel")
          and resolved not in recollect):
        recollect.append(resolved)
    if recollect:
      sm["_correction_recollect"] = recollect
    _log(sm, "intent_classified", intent=intent, slots=recollect)
  elif intent:
    sm["_classified"] = {"intent": intent, "target": target}
    _log(sm, "intent_classified", intent=intent, target=target)
  return {"sm": sm, **out}


def _intake_classify_turn_intent(sm, response_data, out):
  """classify_turn_intent (intent-first Pass A): the model's mandatory per-turn
  intent classification.

  Records the gate marker (_pending_intent — consumed by the engine's intent-first
  gate to enter Pass B) and, for non-`continue` intents, the SAME control state the
  opportunistic set_intent_changed setter produces (_classified for transitions,
  _correction_recollect for corrections). Pass B then routes it through the
  unchanged machinery (_intent_directive / _correction_focus_directive). The label
  is `continue`, `cancel`, `escalate`, or a `<base>:<target>` form
  (`switch:`/`new_request:`/`resume:`/`correct:`)."""
  intent = str(response_data.get("intent", "")).lower().strip()
  base, _, target = intent.partition(":")
  base = base.strip()
  target = target.strip()
  sm["_pending_intent"] = intent or "continue"
  if base == "correct":
    recollect = []
    for nm in [s.strip() for s in target.split(",") if s.strip()]:
      resolved = _resolve_correction_slot(nm, sm)
      if (resolved and resolved not in (sm.get("_gate_slot"), "cancel")
          and resolved not in recollect):
        recollect.append(resolved)
    if recollect:
      sm["_correction_recollect"] = recollect
    _log(sm, "turn_intent_classified", intent=base, slots=recollect)
  elif base in ("new_request", "switch", "resume", "cancel", "escalate"):
    sm["_classified"] = {"intent": base, "target": target}
    _log(sm, "turn_intent_classified", intent=base, target=target)
  else:
    _log(sm, "turn_intent_classified", intent="continue")
  return {"sm": sm, **out}


def _intake_resume_flow(sm, response_data, out):
  """resume_flow: resolve which paused flow the user means, deterministically.

  Sets _resume_target (single match) or _resume_result (ambiguous/error); the
  actual restore happens when the LLM then calls set_active_flow, which reads
  _resume_target in the bootstrap restore block below (reusing that machinery).
  """
  flow_stack = sm.get("_flow_state", [])
  if not flow_stack:
    sm.pop("_resume_target", None)
    sm["_resume_result"] = {
        "error": True, "message": "No paused requests to resume.",
    }
    return {"sm": sm, **out}
  want_id = response_data.get("instance_id") or 0
  want_flow = (response_data.get("flow") or "").lower().strip()
  slot_name = response_data.get("slot_name", "")
  slot_value = response_data.get("slot_value", "")
  # Narrow by flow type first when the user named a service ("my reservation"),
  # then by instance id or slot value within that flow type.
  candidates = flow_stack
  if want_flow:
    candidates = [e for e in flow_stack
                  if str(e.get("flow", "")).lower() == want_flow]
  if want_id:
    matches = [e for e in candidates if e.get("id") == want_id]
  elif slot_name:
    matches = []
    for entry in candidates:
      merged = {**entry.get("slots", {}), **entry.get("pending", {}),
                **entry.get("deferred", {}), **entry.get("repeat_acc", {})}
      saved = str(merged.get(slot_name, ""))
      if slot_value:
        if slot_value.lower() in saved.lower():
          matches.append(entry)
      elif saved:
        matches.append(entry)
  else:
    matches = list(candidates)
  if len(matches) == 1:
    sm.pop("_resume_result", None)
    sm["_resume_target"] = {
        "flow": matches[0]["flow"],
        "instance_id": matches[0].get("id"),
    }
    _log(sm, "resume_matched", flow=matches[0]["flow"],
         instance_id=matches[0].get("id"))
  elif len(matches) > 1:
    sm.pop("_resume_target", None)
    sm["_resume_result"] = {
        "ambiguous": True,
        "options": [
            {"id": m.get("id"), "flow": m["flow"],
             "slots": m.get("slots", {})}
            for m in matches
        ],
    }
    _log(sm, "resume_ambiguous", count=len(matches))
  else:
    sm.pop("_resume_target", None)
    sm["_resume_result"] = {
        "error": True,
        "message": "No paused request matches that description.",
    }
    _log(sm, "resume_no_match", slot_name=slot_name, slot_value=slot_value)
  return {"sm": sm, **out}


def _intake_bootstrap(sm, tool_name, response_data, current_agent, channel,
                      bootstrap, out):
  """Bootstrap / new_flow_instance: the gate setter (config-driven, pre-engine).

  Saves the current instance to the flow stack on a flow change / forced-new /
  resume, optionally reaps a completed flow (reset_on_complete) carrying shared
  slots forward, sets the gate slot, restores a paused instance, and decides the
  transfer to the specialist agent.
  """
  if response_data.get("stored"):
    slot_name = bootstrap["slot"]
    # ROUTE-VALIDATION (#1/#4): a router only activates flows it ADVERTISES, and the gate is
    # the one slot whose bad value cannot be corrected later. An id NOT in flow_types is
    # resolved in three steps — canonicalize a merely mis-cased id, else coerce to the
    # configured default/home-base (rather than activating an unadvertised flow, e.g. the
    # model picking a deployed-but-absorbed capability flow that shadows the orchestrator's
    # verdict — which makes a fan-out/verdict a reliable catch-all), else refuse. A no-op
    # when flow_types is empty or the value is valid ⇒ byte-identical for every existing
    # agent whose model names a real flow.
    _ft = sm.get("_flow_types") or []
    _dflt = sm.get("_default_flow") or ""
    _want = response_data.get("value")
    if _ft and _want and _want not in _ft:
      _canon = next(
          (f for f in _ft
           if str(f).lower().strip() == str(_want).lower().strip()), "")
      if _canon:
        # Advertised, only mis-cased (a hand-written or customized setter that
        # skipped the normalize the emitted enum setter does): canonicalize,
        # never reject.
        response_data = {**response_data, "value": _canon}
      elif _dflt:
        _log(sm, "route_coerced_to_default", tool=tool_name,
             requested=_want, default=_dflt)
        response_data = {**response_data, "value": _dflt}
      else:
        # No home base to fall back on — REFUSE rather than latch. Latching an id
        # no flow answers to leaves every flow's slots inactive AND (because the
        # gate now reads as filled) hides the gate setter along with every other
        # setter, so the turn renders with no callable move and repeats forever.
        # Returning before the gate is written keeps the gate/current flow intact,
        # where the gate setter is still visible and the route cues still fire, so
        # the turn is recoverable. The emitted enum setter refuses this upstream —
        # this is the write path's own copy of that invariant, for the id that
        # reaches intake some other way.
        _log(sm, "route_rejected_unknown_flow", "WARN", tool=tool_name,
             requested=_want, flows=_ft)
        return {"sm": sm, **out}
    prev_flow_value = sm.get("filled", {}).get(slot_name)
    flow_changed = (prev_flow_value
                    and prev_flow_value != response_data["value"])
    # new_flow_instance starts a FRESH instance of the same flow type: stash
    # the current one and begin clean (no restore), even if type is unchanged.
    force_new = (tool_name == "new_flow_instance"
                 or bool(response_data.get("new_instance")))
    # A resume in progress (resume_flow resolved a _resume_target) swaps the
    # active instance for a paused one — stash current + restore the target by
    # id even when the flow TYPE is unchanged (resuming another instance of the
    # same flow type, e.g. a second takeout order).
    resume_in_progress = bool(sm.get("_resume_target")) and not force_new
    did_reset = False

    # Repeated-slot accumulator (engine's `_repeat_acc`, §R2.1): capture it BEFORE
    # _flow_clear_internals below wipes it (it is NOT a KEEP-set key nor a slot
    # container), so an in-progress repeated collection can be stashed onto the flow
    # stack alongside `filled` and restored on resume.
    prev_repeat_acc = dict(sm.get("_repeat_acc", {}))

    # A flow transition is happening (switch / new instance / resume / re-entry
    # into a completed flow). Discard the prior flow's INTERNAL state up front —
    # counters, derived setter-maps, intent/correction flags — so none of it leaks
    # into the new flow (this is the recurring cross-flow stale-state bug class).
    # The slot containers are seeded explicitly by the blocks below; this only wipes
    # the easy-to-forget internals, driven by the single keep-set (a new per-flow
    # key is wiped here by default).
    if (flow_changed or force_new or resume_in_progress
        or (bootstrap.get("reset_on_complete")
            and sm.get("status") in ("complete", "zombie", "escalated"))):
      _flow_clear_internals(sm)

    if ((flow_changed or force_new or resume_in_progress)
        and sm.get("status") not in ("complete", "zombie", "escalated")):
      shared_slots = set(sm.get("_shared_slots", []))
      private_filled = {
          k: v for k, v in sm.get("filled", {}).items()
          if k != slot_name and k not in shared_slots
      }
      private_pending = {
          k: v for k, v in sm.get("pending", {}).items()
          if k not in shared_slots
      }
      private_deferred = {
          k: v for k, v in sm.get("deferred", {}).items()
          if k not in shared_slots
      }
      # Partition the repeated-slot accumulator to the private scope, exactly like
      # `filled` (§R2.1) — a shared/gate slot is never repeated, but mirror the
      # partition so the stash carries only this instance's in-progress collections.
      private_repeat_acc = {
          k: v for k, v in prev_repeat_acc.items()
          if k != slot_name and k not in shared_slots
      }
      flow_stack = sm.setdefault("_flow_state", [])
      instance_id = sm.get("_flow_instance_seq", 0) + 1
      sm["_flow_instance_seq"] = instance_id
      flow_stack.append({
          "id": instance_id,
          "flow": prev_flow_value,
          "slots": private_filled,
          "pending": private_pending,
          "deferred": private_deferred,
          "repeat_acc": private_repeat_acc,
          "task_results": dict(sm.get("task_results", {})),
      })
      _log(sm, "flow_state_saved", flow=prev_flow_value,
           slots=sorted(private_filled.keys()))

    if (bootstrap.get("reset_on_complete")
        and sm.get("status") in ("complete", "zombie", "escalated")):
      # Shared slots (e.g. the once-per-session welcome announce, guest_name)
      # were moved into the zombie by _terminate, which cleared `filled`. Carry
      # them back so they are NOT re-collected / re-announced on the next flow
      # (the OS model: environment is inherited across a new exec).
      carried_shared = dict(sm.get("_zombie", {}).get("shared_values", {}))
      sm.pop("_zombie", None)
      shared_slots = set(sm.get("_shared_slots", []))
      shared_filled = {k: v for k, v in sm.get("filled", {}).items()
                       if k in shared_slots}
      shared_deferred = {k: v for k, v in sm.get("deferred", {}).items()
                         if k in shared_slots}
      sm["status"] = "in_progress"
      sm["task_results"] = {}
      # KEEP what is already staged. The prior flow's teardown emptied `pending`,
      # so anything here was staged AFTER completion — i.e. the value the caller
      # supplied in the very turn that re-arms the gate ("thanks, now I need a
      # widget, it's W-5"). Overwriting the dict dropped it and re-asked. A staged
      # value the NEW flow has no active slot for is swept by the engine's
      # conditional-slot pass, so nothing stale survives. The re-deferred shared
      # values still win their own keys (unchanged for every existing agent).
      sm["pending"] = {**sm.get("pending", {}), **shared_deferred}
      sm["deferred"] = {}
      gate_val = sm.get("filled", {}).get(slot_name)
      sm["filled"] = {slot_name: gate_val} if gate_val else {}
      sm["filled"].update(carried_shared)
      sm["filled"].update(shared_filled)
      did_reset = True

    sm.setdefault("filled", {})[slot_name] = response_data["value"]

    # Intent-first: a flow was just entered/changed (routed-in, switched, new
    # instance, or resumed) — the destination flow agent's first turn is an
    # EXTRACTION turn whose intent is already known from the routing, so it must
    # NOT run a Pass-A classification (which makes the model treat the opening
    # message as already-handled and skip the setter). Arm a one-shot skip the
    # destination's engine gate consumes so the entry turn goes straight to Pass B.
    # Unconditional: this store happens in the SOURCE/router context (where
    # _intent_first may be False); the intent-first destination is what consumes it.
    sm["_skip_pass_a_once"] = True

    # Cross-flow new instance: new_flow_instance set the gate to a DIFFERENT
    # flow but carries no target_agent (same-agent tool). Chain a set_active_flow
    # (via _pending_switch -> before_model branch 0 gate_correction_switch) so we
    # transfer to the right specialist, where the original message's slots are
    # then captured. Same-flow new instances (flow_changed False) need no transfer.
    if force_new and flow_changed:
      sm["_pending_switch"] = response_data["value"]
      _log(sm, "new_instance_cross_flow", flow=response_data["value"])

    if (flow_changed or force_new or resume_in_progress) and not did_reset:
      shared_slots = set(sm.get("_shared_slots", []))
      for k in list(sm.get("filled", {}).keys()):
        if k != slot_name and k not in shared_slots:
          sm["filled"].pop(k)
      for k in list(sm.get("pending", {}).keys()):
        if k not in shared_slots:
          sm["pending"].pop(k)
      shared_deferred = {k: v for k, v in sm.get("deferred", {}).items()
                         if k in shared_slots}
      sm["deferred"] = {}
      sm.setdefault("pending", {}).update(shared_deferred)
      sm["task_results"] = {}

    if (flow_changed or resume_in_progress) and not force_new:
      # force_new starts a clean instance — skip restore entirely.
      flow_stack = sm.get("_flow_state", [])
      # resume_flow may have pinned a specific instance to restore; if so,
      # match by id. Otherwise fall back to the existing flow-type match
      # (byte-identical to pre-resume_flow behavior).
      resume_target = sm.pop("_resume_target", None)
      target_id = (resume_target.get("instance_id")
                   if resume_target else None)
      saved = None
      for i, entry in enumerate(flow_stack):
        if target_id is not None:
          if entry.get("id") == target_id:
            saved = flow_stack.pop(i)
            break
        elif entry.get("flow") == response_data["value"]:
          saved = flow_stack.pop(i)
          break
      if saved:
        sm.setdefault("pending", {}).update(saved.get("slots", {}))
        sm.setdefault("pending", {}).update(saved.get("pending", {}))
        sm.setdefault("deferred", {}).update(saved.get("deferred", {}))
        sm["task_results"] = saved.get("task_results", {})
        # Restore the in-progress repeated-slot accumulator (§R2.1). Unlike `slots`
        # (which resume re-validates into pending), it resumes AS the live
        # accumulator — collection picks up mid-list. _flow_clear_internals already
        # wiped the stale live copy, so set it fresh.
        saved_repeat_acc = saved.get("repeat_acc")
        if saved_repeat_acc:
          sm["_repeat_acc"] = dict(saved_repeat_acc)
        sm["_restored_flow"] = True
        _log(sm, "flow_state_restored",
             flow=response_data["value"],
             slots=sorted(saved.get("slots", {}).keys()))
      sm.pop("_auto_resume_deferred", None)
      sm.pop("_resume_offer_pending", None)

    _log(sm, "bootstrap_stored",
         tool=tool_name, slot=slot_name, value=response_data["value"],
         did_reset=did_reset)

    target = response_data.get("target_agent")
    if target:
      # A same-type resume restores in-place (target == current agent), but the
      # render pass after the stacked resume_flow + set_active_flow injections
      # makes the model emit empty content ("having trouble") — the cross-type
      # path avoids this because its transfer gives a fresh turn boundary. Force
      # that same boundary here with a self-transfer when we just restored.
      if target != current_agent or sm.get("_restored_flow"):
        if bootstrap.get("pass_through_on_transfer"):
          params = {k: v for k, v in response_data.items()
                    if k not in _BOOTSTRAP_KEYS and v}
          if channel and "channel" not in params:
            params["channel"] = channel
          if params:
            out["transfer_slots"] = params
        _log(sm, "bootstrap_transfer", tool=tool_name, target=target)
        out["pending_transfer"] = target
      else:
        if did_reset or bootstrap.get("pass_through_on_transfer"):
          params = {k: v for k, v in response_data.items()
                    if k not in _BOOTSTRAP_KEYS and v}
          if channel and "channel" not in params:
            params["channel"] = channel
          if params:
            out["transfer_slots"] = params
          _log(sm, "bootstrap_reentry", tool=tool_name,
               slots=list(params.keys()))
  return {"sm": sm, **out}


def _intake_correction(sm, tool_name, response_data, out):
  """correction tool (set_slot_change): NAME the slot(s) the guest wants to change
  or add (no values). Record them in _correction_recollect; the engine then runs a
  focused pass showing only those slots' setters, and the setter result (phase 2,
  via _stage_setter_value) is what actually mutates state. Nothing is cleared here,
  so a confirmed value survives if focus never produces a replacement."""
  if response_data.get("error"):
    return {"sm": sm, **out}
  if response_data.get("success"):
    names = response_data.get("slots", [])
    gate_slot = sm.get("_gate_slot")
    recollect = []
    for nm in names:
      resolved = _resolve_correction_slot(nm, sm)
      # Structural slots (gate/active_flow, cancel) are not user corrections —
      # a flow switch is handled by the deterministic switch backstop instead.
      if resolved in (gate_slot, "cancel"):
        _log(sm, "correction_structural_slot_skipped", tool=tool_name, slot=nm)
        continue
      if not resolved:
        _log(sm, "correction_recollect_unresolved", "WARN",
             tool=tool_name, slot=nm)
        continue
      if resolved not in recollect:
        recollect.append(resolved)
    if recollect:
      sm["_correction_recollect"] = recollect
      _log(sm, "correction_recollect_requested", tool=tool_name, slots=recollect)
    elif names:
      # The user named slot(s) to change/add but NONE resolved to a collectable
      # slot (e.g. a not-yet-reachable slot whose prerequisites are unmet). There
      # is no focus to run, so the next engine pass would render with no
      # directive -> empty turn. Flag it so the engine force-preempts the current
      # readback/question instead of freezing.
      sm["_correction_unresolved"] = True
      _log(sm, "correction_recollect_empty", tool=tool_name, named=names)
  return {"sm": sm, **out}


def _intake_multi_setter(sm, tool_name, response_data, field_map, out):
  """Multi-field setter (one tool fills several slots): record per-field validation
  errors and stage the valid field values (prereq + validation checked per slot)."""
  filled = sm.get("filled", {})
  field_errors = response_data.get("field_errors", {})
  for field_name, error_code in field_errors.items():
    mapped_slot = field_map.get(field_name)
    if mapped_slot:
      _log(sm, "multi_setter_error", "WARN",
           tool=tool_name, field=field_name,
           slot=mapped_slot, code=error_code)
      sm.setdefault("_slot_errors", []).append(
          {"slot": mapped_slot, "code": error_code},
      )
      # A validation failure on a FILLED slot can only be a focused
      # correction (the setter is hidden otherwise). Clear the slot so the
      # guest's retry flows through the now-visible setter — same as an
      # initial out-of-range — instead of a hidden-setter dead end.
      if filled.pop(mapped_slot, None) is not None:
        _log(sm, "correction_error_cleared_slot", slot=mapped_slot)
      _drop_recollect(sm, mapped_slot)
  for field_name, value in response_data.get("values", {}).items():
    mapped_slot = field_map.get(field_name)
    if not mapped_slot:
      continue
    # The same result reported an error for this field above (and cleared the
    # slot): the write path must not then honor the value it just rejected.
    if field_name in field_errors:
      _log(sm, "multi_setter_value_rejected", "WARN",
           tool=tool_name, field=field_name, slot=mapped_slot,
           code=field_errors[field_name])
      continue
    slot_requires = sm.get("_slot_requires", {})
    prereq_fail = False
    for req in slot_requires.get(mapped_slot, []):
      if req not in filled:
        _log(sm, "prereq_not_met", "WARN", slot=mapped_slot, missing=req)
        sm.setdefault("_slot_errors", []).append(
            {"slot": mapped_slot, "code": "prereq_not_met"},
        )
        prereq_fail = True
        break
    if prereq_fail:
      continue
    slot_validates = sm.get("_slot_validates", {})
    validation = slot_validates.get(mapped_slot)
    if validation:
      # A MULTI-field setter reports what it captured under `values` — that is the
      # contract the framework's own generated body emits ({"stored", "values",
      # "field_errors"}, no top-level mirror). `validate_against` names a RESPONSE
      # FIELD, and a bare top-level read finds nothing on every conforming setter, so
      # `check_value` was always "" and the check rejected the value the caller had
      # just given. Silent and total: the slot never stages, so no readback opens, a
      # confirm has nothing to commit, and every task gated on that slot is unreachable
      # for the rest of the call. Resolve in the field namespace first and keep the
      # top-level read as the fallback, so a setter that ALSO mirrors the field at the
      # top level (the hand-written kind this contract grew up alongside) is unchanged.
      response_field = validation["response_field"]
      value_fields = response_data.get("values")
      if not isinstance(value_fields, dict):
        value_fields = {}
      check_value = value_fields.get(
          response_field, response_data.get(response_field, ""))
      against_raw = filled.get(validation["filled_slot"], "")
      # Case/space-insensitive membership (see the single-setter twin): a canonicalized
      # value must still match an allowed-list token written in a different case.
      valid_options = [t.strip().casefold() for t in against_raw.split(",")]
      if str(check_value).strip().casefold() not in valid_options:
        _log(sm, "validation_failed", "WARN", slot=mapped_slot,
             code=validation.get("error_code", "invalid"))
        sm.setdefault("_slot_errors", []).append(
            {"slot": mapped_slot,
             "code": validation.get("error_code", "invalid")},
        )
        continue
    _log(sm, "multi_setter_stored",
         tool=tool_name, field=field_name, slot=mapped_slot, value=value)
    _stage_setter_value(sm, mapped_slot, value)
  return {"sm": sm, **out}


def _coerce_response(response_data, declared=()):
  """Normalize what a tool handed back into the flat dict `outputs` maps against.

  Three shapes reach here that are not that dict, and each one silently maps NOTHING
  — the task has already succeeded, so there is no failure to escalate and no slot to
  ask for, and the caller hears nothing:

  * a JSON STRING. after_tool unwraps the CES envelope but never parses a body, so a
    tool returning serialized JSON arrives as text;
  * a SECOND `result` envelope. after_tool peels exactly one, and a tool that wraps
    its own payload leaves the real keys a level below where the mapping looks;
  * a SCALAR. CES hands us exactly that for an ASYNCHRONOUS tool — after_tool unwraps
    `{"result": "pending"}` down to the bare string. Wrapping it keeps the `.get`
    below from raising into the crash envelope.

  The framework already tolerates the first two on the fan-out and async-completion
  paths (before_model `_fanout_publications` / `_parse_async_completions`); this is
  the same tolerance on the ordinary synchronous one.
  """
  if isinstance(response_data, str):
    text = response_data.strip()
    if text[:1] in ("{", "["):
      try:
        response_data = json_lib.loads(text)
      except Exception:  # pylint: disable=broad-except
        pass
  # ...unless the task MAPS `result`. A tool whose payload is legitimately a single
  # `result` field is indistinguishable from an envelope by shape alone; the config
  # is what tells them apart, and peeling one the author declared leaves its slot
  # silently empty.
  if (isinstance(response_data, dict) and set(response_data) == {"result"}
      and "result" not in declared):
    inner = response_data["result"]
    if isinstance(inner, str):
      text = inner.strip()
      if text[:1] == "{":
        try:
          inner = json_lib.loads(text)
        except Exception:  # pylint: disable=broad-except
          pass
    # Only when `result` is the SOLE key: a payload that legitimately carries a
    # `result` field alongside its other outputs must not be peeled down to it.
    if isinstance(inner, dict):
      return inner
  if not isinstance(response_data, dict):
    return {"result": response_data}
  return _derive_error_code(response_data)


# The platform's own failures, keyed by the prefix each announces itself with. Measured
# live (ces-probes 110, 113), not inferred: every entry was observed coming back on a
# tool's function_response as `{"status": "error", "error": "<prefix>…"}`.
# Order matters: the specific platform prefixes are tried first, and the bare
# `TimeoutException` last. A python body that raises its own timeout has RUN and failed —
# the platform reports it as `Python function execution failed` and it is a crash of the
# body, not the platform stopping it. `TimeoutException` on its own is a timeout reported
# by some other layer, which is how an MCP toolset call surfaces:
#   Calling MCP tool 'x' failed: java.util.concurrent.TimeoutException: Did not observe
#   any item or terminal signal within 500ms in 'source(MonoDeferContextual)'
# Seen in a live RunSession trace. Note that 500ms is the MCP client's own deadline and
# has nothing to do with the tool `timeout` field, which cannot reach a toolset at all.
_PLATFORM_ERRORS = (
    ("Code execution timed out", "timeout"),
    ("Python function execution failed", "tool_crash"),
    ("Missing parameter", "missing_parameter"),
    ("TimeoutException", "timeout"),
)


def _derive_error_code(payload):
  """Give a platform failure the `error_code` a config can branch on.

  A tool killed by its declared `timeout`, one that raised, and one fired without a
  required parameter all come back as `{"status": "error", "error": "<message>"}` carrying
  no `error_code` — so all three look identical to the only reason-aware surfaces there
  are (`on_failure`'s keyed fields, a slot's `validation.errors`). Naming them separates
  "the backend is slow" from "this call is broken", which are opposite responses.

  Matched POSITIVELY, by prefix, never by negation. Of the six failure shapes probed, two
  return no error at all and one returned a prefix that was not known the day before, so
  "not a timeout, therefore a crash" would mislabel the first unenumerated failure and an
  author would speak the wrong line for a reason nobody has observed. Anything
  unrecognized keeps no code and falls to the caller's `_default` branch.

  Args:
    payload: The coerced tool response.

  Returns:
    The payload, with `error_code` added when the message names a known failure.
  """
  if not isinstance(payload, dict) or payload.get("error_code"):
    return payload
  message = payload.get("error")
  if not isinstance(message, str):
    return payload
  for prefix, code in _PLATFORM_ERRORS:
    if prefix in message:
      out = dict(payload)
      out["error_code"] = code
      return out
  return payload


# The async completion envelope's verdict, as before_model reports it. Only ever
# consulted when a payload carries no success key of its own.
_OUTCOME_OK = "completed"
_ASYNC_PENDING = "pending"


def _is_error_payload(payload) -> bool:
  """Does this payload say, in its own words, that the call failed?

  Measured for a platform-executed agent call (ces-probes 135): a target that is gone or
  a host that will not resolve both come back as `{"status": "error", "error": "..."}` —
  the same envelope a python tool's failure uses, and NOT the `{"error": ...}` the A2A
  docs describe.

  It matters here because that payload has no `response` key, which is exactly the
  condition under which the completion envelope's verb is consulted. Whether an
  asynchronous agent failure arrives under `failed with error` or under `completed with
  response` carrying this shape has never been measured — 134 saw the success envelope
  and never made one fail. So the verb is not allowed to overrule a payload that has
  already declared itself an error.
  """
  if not isinstance(payload, dict):
    return False
  if str(payload.get("status") or "").strip().lower() == "error":
    return True
  return bool(payload.get("error"))


def _is_async_placeholder(payload) -> bool:
  """Is this CES's stand-in for a deferred call, rather than an answer?

  Intake used to need no such test: the placeholder is `{"result": "pending"}`, tasks
  check `success`, and a key that is absent is falsy — so it fell out of the arithmetic.
  That stops being true the moment `success_check` names the key the placeholder itself
  uses. An agent tool answers under `response`, so its placeholder is
  `{"response": "pending"}` and satisfies its own success test: driven live, the slot
  filled with the literal string "pending", the flow announced it to the caller and
  ended, and the real answer arrived afterwards with nowhere to go.

  The engine has this test too (`_is_async_pending`) but only reaches it AFTER intake
  has mapped outputs, so it cannot undo the fill.
  """
  if isinstance(payload, str):
    return payload.strip().lower() == _ASYNC_PENDING
  if isinstance(payload, dict):
    for key in ("result", "response"):
      inner = payload.get(key)
      if isinstance(inner, str) and inner.strip().lower() == _ASYNC_PENDING:
        return True
  return False


def _intake_remote(sm, response_data, task_info, remote, out):
  """The two answers a REMOTE task gives that are not results.

  A remote task is answered by two tools and only the second one ever carries the
  outputs. Left alone, both of the cases below would complete the task with none of its
  slots filled, because each is a perfectly successful HTTP call:

  * the START call, which returns a job handle. The handle is recorded as an ordinary
    output slot — the poll needs it — but the task is NOT finished, so the completion
    mark is withheld and the engine opens a wait instead.
  * a STATUS poll that says `running`. Same shape, same reason.

  A terminal status (`done` / `failed` / `timeout`) is NOT handled here: it falls
  through to the ordinary path so the task completes exactly as a local tool's would,
  with the same output mapping, the same `then_say` and the same `on_failure` ladder
  keyed on the service's `error_code`.

  Returns `(handled, response_data)` — `handled` True means the caller returns at once.
  """
  # A DEFERRED CALL IS NOT AN ANSWER, and the status branch below cannot tell the
  # difference on its own. When the status resource is deployed `executionType:
  # ASYNCHRONOUS` — a supported CES shape, and the one a slow poll wants — the platform
  # replies at once with `{"result": "pending"}` and delivers the real payload later, in
  # a completion envelope. That reply carries no `status` field, so the four-value check
  # below read it as a service that broke its contract: `success: False`,
  # `error_code: "remote_contract"`, `error: "unknown status: (absent)"`, and a WARN
  # `task_completed success=false` on the turn the job was merely started.
  #
  # Observed on a live voice call, where the job then ran and reported perfectly well.
  # It survived only because the task also declares `awaits`, so the engine's own
  # `_is_async_pending` opened the wait a moment later — but the payload it opened the
  # wait ON was by then stamped with a failure code, which is exactly what an
  # `on_failure` keyed by `error_code` branches on. A wait must not begin with a verdict
  # about the service already recorded against it.
  #
  # Left for the caller's own placeholder test (`_is_async_placeholder`) to handle,
  # untouched, which is what it is for.
  if _is_async_placeholder(response_data):
    return False, response_data
  task = task_info["task_name"]
  job_slot = remote.get("job_slot") or ""
  if not task_info.get("remote_status"):
    handle = str(response_data.get(job_slot) or "").strip()
    if not handle:
      # A start that produced no handle is a real failure, and a silent one otherwise:
      # nothing to poll, so the task would wait out its budget for an answer that can
      # never arrive. Named so `on_failure` can branch on it.
      response_data = dict(response_data)
      response_data["success"] = False
      response_data.setdefault("error_code", "remote_bad_handle")
      return False, response_data
    sm.setdefault("filled", {})[job_slot] = handle
    # Marked in flight HERE, not left for the engine's next pass to derive. Intake runs
    # inside `after_tool`, and the very next thing the engine does is recompute task
    # eligibility — so a task downstream of this one would find its inputs unfilled but
    # this task no longer running, fire on the start turn, and deliver an empty result.
    # Measured live: the delivery task fired immediately and spoke a report that did not
    # exist yet.
    # Stamped on the WAIT clock, not `_turn_n`: the engine measures this mark against
    # `_wait_clock`, which counts inactivity ticks as well as caller turns, and a mark
    # in the wrong units reads as a wait that started in the future.
    since = _wait_clock(sm)
    sm.setdefault("_awaiting_async", {})[task] = {
        "tool": "", "since": since,
        "remote": {"job": handle, "since": since}}
    sm.setdefault("_remote_started", {})[task] = {"job": handle, "since": since}
    return True, response_data

  status = str(response_data.get("status") or "").strip().lower()
  if status == "running":
    return True, response_data

  # A status answer's verdict is its `status` field, and nothing else. The generated
  # wrapper cannot know that: it reports success as "every declared output came back",
  # which is right for an ordinary call and wrong for this one — `error_code` is absent
  # on a healthy job, so a FINISHED job answered `success: False, error: "missing from
  # response: error_code"` and the task went down its on_failure ladder with the report
  # sitting in the payload. Measured live; the caller was told the report could not be
  # built on the turn it was built.
  response_data = dict(response_data)
  if status in ("failed", "timeout"):
    response_data["success"] = False
    response_data.setdefault(
        "error_code", "remote_timeout" if status == "timeout" else "remote_failed")
    return False, response_data
  if status == "done":
    absent = [k for k in (remote.get("outputs") or [])
              if response_data.get(k) is None]
    if absent:
      # Finished, and not what was declared. Named rather than reported as a generic
      # failure so `on_failure` can tell a service that broke its contract apart from a
      # job that ran and failed — they need different answers, and only one is a retry.
      response_data["success"] = False
      response_data.setdefault("error_code", "remote_contract")
      response_data["error"] = "missing from result: " + ", ".join(absent)
    else:
      response_data["success"] = True
      response_data.pop("error", None)
    return False, response_data
  # Neither pending nor terminal: the contract names exactly four values, so anything
  # else is a service the agent cannot follow rather than a job worth waiting on.
  response_data["success"] = False
  response_data.setdefault("error_code", "remote_contract")
  response_data.setdefault("error", f"unknown status: {status or '(absent)'}")
  return False, response_data


def _intake_executor(sm, tool_name, response_data, task_info, out, outcome=""):
  """Executor task result: record the raw output under its task name and, on
  success, map the declared outputs the tool ACTUALLY returned into filled slots;
  mark the task just-completed.

  `outcome` is the async completion envelope's own verdict ("completed"/"failed"),
  and is empty for an ordinary synchronous result. See the success test below.
  """
  response_data = _coerce_response(
      response_data, declared=set(task_info.get("outputs") or {}))

  remote = task_info.get("remote")
  if remote:
    # Unguarded. `_intake_remote` interprets a payload from a service nobody here
    # deploys, which is a real reason to be careful — but it is careful IN THE BRANCHES
    # rather than in a `try`: `_coerce_response` above guarantees a dict, every read
    # below it is a `.get` with a default, and every shape a misbehaving service can
    # produce already has a named outcome (`remote_bad_handle`, `remote_contract`).
    # A blanket except adds no case those do not cover and takes one away: it would
    # report an engine bug as the service's fault, under whichever error_code it
    # happened to default to.
    handled, response_data = _intake_remote(
        sm, response_data, task_info, remote, out)
    if handled:
      return {"sm": sm, **out}

  success_key = task_info.get("success_check", "success")
  if _is_async_placeholder(response_data):
    # Not an answer and not a failure: the call deferred. Say "not successful" so no
    # output is mapped, and leave the payload untouched so the engine's own placeholder
    # test still sees it and opens the wait.
    success = False
  elif success_key in response_data:
    # The payload says for itself, and it always wins. Every python tool is here: its
    # pydantic model carries `success`, so nothing about those turns changes.
    success = bool(response_data[success_key])
  else:
    # Nothing to read. An agent called as a tool answers `{"response": "<text>"}` and a
    # crashed one answers with an error payload — neither carries a success key, so
    # before the envelope's verb was kept these two were the same event here, and a good
    # answer went to the on_failure ladder. Absent a verb (any synchronous result) this
    # stays false, which is the behaviour every existing app already has.
    success = outcome == _OUTCOME_OK and not _is_error_payload(response_data)
    # RECORD the verdict, do not just act on it. The engine re-derives success from the
    # stored payload in half a dozen places (the post-executor handler, fire
    # eligibility, the parallel legs), none of which can see the envelope. Writing the
    # key the payload lacked is what makes every one of them agree, and it can never
    # overwrite an author's value because this branch only runs when the key is absent.
    if outcome and isinstance(response_data, dict):
      response_data = dict(response_data)
      response_data[success_key] = success

  task_results_map = sm.setdefault("task_results", {})
  task_results_map[task_info["task_name"]] = response_data
  _log(sm, "task_completed", "INFO" if success else "WARN",
       tool=tool_name, task=task_info["task_name"], success=success)
  if success:
    # Map each output the tool RETURNED; leave the rest unfilled. This was
    # all-or-nothing (`if all(k in response_data ...)`), which fails silently and
    # totally: a tool that legitimately returns a SUBSET filled nothing at all — not
    # even the keys it did return — and the flow then sat on a task that had already
    # succeeded, with no slot to ask for and no failure to escalate. The caller hears
    # nothing, forever.
    #
    # A variable-length result set is the ordinary case, not an edge: a KBA generator
    # declares question_1..4 and returns as many as the bureau produced, which is why
    # such configs gate their per-question announces on `question_count >= N`. Under
    # all-or-nothing that gate could never fire, because `question_count` was dropped
    # along with the questions.
    #
    # Per-key also makes the two merge sites AGREE: `_frame_return` in the engine, the
    # component analogue of this merge, already documents "a declared output the child
    # did not produce is skipped (the downstream slot stays unfilled and is re-asked)".
    # An absent key now behaves exactly as if it had never been declared.
    outputs = task_info.get("outputs", {})
    filled_map = sm.setdefault("filled", {})
    absent = []
    for result_key, slot_nm in outputs.items():
      if result_key in response_data:
        # A key may name SEVERAL slots. One backend field routinely lands under two
        # slot names (a modem MAC that is also the device id), and the alternative is
        # a second declared key no tool returns — which, being absent, maps nothing.
        targets = slot_nm if isinstance(slot_nm, list) else [slot_nm]
        for target in targets:
          filled_map[target] = response_data[result_key]
      else:
        absent.append(result_key)
    if absent and len(absent) < len(outputs):
      # Observability, not an error: a partial set is legitimate, but "the slot is
      # empty because the tool did not return it" is the first thing you need to know
      # when a downstream condition does not fire.
      #
      # A task that returned NONE of its outputs is NOT this: that is the ordinary
      # "nothing yet" shape (an async poll that has not resolved yet returns a bare
      # marker on purpose), so it stays quiet rather than crying wolf every poll turn.
      _log(sm, "task_outputs_partial", "INFO", tool=tool_name,
           task=task_info["task_name"], mapped=len(outputs) - len(absent),
           absent=absent)

  sm["_task_just_completed"] = task_info["task_name"]
  return {"sm": sm, **out}


def _intake_single_setter(sm, tool_name, response_data, slot_name, out):
  """Single-slot setter: record an error (clearing a filled slot for the
  focused-correction retry), or stage a stored value after prereq + validation,
  plus any inferred sibling slots."""
  filled = sm.get("filled", {})
  # A setter reaches a slow or broken backend exactly as an executor does, and the platform
  # reports it the same way — but this path never went through `_coerce_response`, so the
  # failure arrived coded `"unknown"` and no `validation.errors` key could ever match it.
  response_data = _derive_error_code(response_data)

  if response_data.get("error"):
    error_code = response_data.get("error_code", "unknown")
    _log(sm, "setter_error", "WARN", tool=tool_name, slot=slot_name, code=error_code)
    sm.setdefault("_slot_errors", []).append(
        {"slot": slot_name, "code": error_code}
    )
    # Clear a FILLED slot on validation failure (focused-correction case) so the
    # retry flows through the now-visible setter instead of a dead end.
    if filled.pop(slot_name, None) is not None:
      _log(sm, "correction_error_cleared_slot", slot=slot_name)
    _drop_recollect(sm, slot_name)
    return {"sm": sm, **out}

  if response_data.get("stored"):
    value = response_data.get("value")
    # A setter that reports success but carries no value has told us nothing —
    # staging the None would read the empty slot back to the caller as confirmed
    # and count as progress. Refuse it and leave the slot to be asked again.
    # `False`/0/"" are real answers, so the test is `is None`, not falsiness.
    if value is None:
      _log(sm, "setter_stored_without_value", "WARN",
           tool=tool_name, slot=slot_name)
      return {"sm": sm, **out}

    slot_requires = sm.get("_slot_requires", {})
    for req in slot_requires.get(slot_name, []):
      if req not in filled:
        _log(sm, "prereq_not_met", slot=slot_name, missing=req)
        sm.setdefault("_slot_errors", []).append(
            {"slot": slot_name, "code": "prereq_not_met"}
        )
        return {"sm": sm, **out}

    slot_validates = sm.get("_slot_validates", {})
    validation = slot_validates.get(slot_name)
    if validation:
      check_value = response_data.get(validation["response_field"], "")
      against_raw = filled.get(validation["filled_slot"], "")
      # Case/space-insensitive membership: a classifier setter canonicalizes the value
      # (e.g. "Outpatient" -> "outpatient"), so an allowed-list token in a different
      # case would otherwise reject the valid answer the caller just gave.
      valid_options = [t.strip().casefold() for t in against_raw.split(",")]
      if str(check_value).strip().casefold() not in valid_options:
        _log(sm, "validation_failed", "WARN", slot=slot_name,
             code=validation.get("error_code", "invalid"))
        sm.setdefault("_slot_errors", []).append(
            {"slot": slot_name, "code": validation.get("error_code", "invalid")}
        )
        return {"sm": sm, **out}

    _log(sm, "setter_stored", tool=tool_name, slot=slot_name, value=value)
    _stage_setter_value(sm, slot_name, value)

    inferred = response_data.get("inferred", {})
    if inferred:
      _log(sm, "inferred_slots", source_slot=slot_name, inferred=inferred)
    for inf_slot, inf_value in inferred.items():
      if inf_slot not in sm.get("filled", {}):
        sm["pending"][inf_slot] = inf_value

  return {"sm": sm, **out}


def slot_intake(input_data: dict[str, Any]) -> dict[str, Any]:
  """Top-level crash envelope around the intake reduction.

  An uncaught exception here propagates through after_tool_callback and surfaces
  to the user as the empty "having trouble" render. Catch it, log to sm._log, and
  return sm unchanged with the default outputs so the turn degrades safely and the
  crash is diagnosable. The real work lives in _slot_intake_impl.
  """
  sm = input_data.get("sm", {}) if isinstance(input_data, dict) else {}
  try:
    return _slot_intake_impl(input_data)
  except Exception as _e:  # pylint: disable=broad-except
    tool_name = input_data.get("tool_name", "?") if isinstance(input_data, dict) else "?"
    _log(sm, "slot_intake_CRASH", level="ERROR", tool=tool_name, err=repr(_e),
         tail=traceback.format_exc().strip().splitlines()[-1][:160])
    return {"sm": sm, "transfer_slots": None, "pending_transfer": None}


def _slot_intake_impl(input_data: dict[str, Any]) -> dict[str, Any]:
  """Apply a tool's result to the slot-machine state (the intake half).

  Pure (sm, tool_result) -> sm reduction: stages setter values into pending,
  records validation errors, captures executor task outputs, resolves resume
  targets, and moves instances on/off the flow stack. A thin dispatcher on
  tool_name — each branch is an `_intake_*` handler above. The CES-bound adapter
  (after_tool_callback) extracts the inputs and applies the outputs.

  Args:
    input_data: Dict with 'tool_name', 'response_data' (the tool result, already
      unwrapped), 'sm', 'current_agent' (display name, for the transfer
      decision), 'channel', and — on an async completion only — 'outcome'.

  Returns:
    Dict with 'sm' (updated), 'transfer_slots' (dict or None), and
    'pending_transfer' (agent name or None) for the adapter to apply.
  """
  tool_name = input_data.get("tool_name", "")
  response_data = input_data.get("response_data", {}) or {}
  sm = input_data.get("sm", {})
  current_agent = input_data.get("current_agent", "")
  channel = input_data.get("channel", "")
  # Set only by before_model when ingesting an async completion envelope; absent for
  # every synchronous result, which is what keeps this a fallback.
  outcome = input_data.get("outcome", "")
  out = {"transfer_slots": None, "pending_transfer": None}

  # Steer-back stall counter resets on ANY substantive tool call. Calling a tool is
  # the guest engaging with the flow — setting a value, confirming, rejecting,
  # correcting, switching — which is forward progress even when no slot is filled
  # THIS turn (e.g. set_slot_change names a correction in phase 1, before the value
  # lands). The framework-internal control tools are NOT engagement and must be
  # excluded: classify_turn_intent fires on EVERY turn (incl. off-topic ones, so it
  # would never let steer-back climb) and try_again is a no-op retry hop. This
  # replaces the old one-shot "grace turn" in the engine, which was spent on
  # unrelated turns (e.g. a "yes" confirm) and so mis-timed off-topic escalation.
  if tool_name not in ("classify_turn_intent", "try_again"):
    if sm.get("_steer_back_turns"):
      sm["_steer_back_turns"] = 0
    sm.pop("_steer_reask", None)

  # Cancellation is a passive slot: the cancel setter routes through the generic
  # setter path (cancel_flow -> "cancel" in pending) and the engine terminates.
  # Match the CONFIGURED setter as well as the framework default. The SI advertises
  # `intent_change.tool`, so an app that names its own would otherwise have the model
  # call a tool intake does not recognise — it would fall through to the generic setter
  # path, find no slot mapped, and silently do nothing.
  if tool_name == "set_intent_changed" or (
      sm.get("_intent_changed_tool") and tool_name == sm["_intent_changed_tool"]):
    return _intake_intent_changed(sm, response_data, out)
  if tool_name == "classify_turn_intent":
    return _intake_classify_turn_intent(sm, response_data, out)
  if tool_name == "try_again":
    return {"sm": sm, **out}
  if tool_name == "resume_flow":
    return _intake_resume_flow(sm, response_data, out)

  bootstrap = sm.get("_bootstrap")
  # A single-flow bootstrap has NO tool (the engine self-seeds the gate), so this
  # never matches and its setters route through normal dispatch below — avoiding
  # _intake_bootstrap's bootstrap["slot"]/response_data["value"] reads. Behavior-
  # preserving for gated configs, whose bootstrap.tool is always truthy.
  if bootstrap and bootstrap.get("tool") and tool_name in (
      bootstrap["tool"], "new_flow_instance"):
    return _intake_bootstrap(sm, tool_name, response_data, current_agent,
                             channel, bootstrap, out)

  correction_tool = sm.get("_correction_tool")
  if correction_tool and tool_name == correction_tool:
    return _intake_correction(sm, tool_name, response_data, out)

  # Setter dispatch: a single-slot setter (_setter_slots), a multi-field setter
  # (_multi_setter_slots), or an executor task (_executor_tasks). A tool in NONE
  # of the maps is a no-op (returns out unchanged) — it never reaches the
  # single-setter handler.
  slot_name = sm.get("_setter_slots", {}).get(tool_name)
  if slot_name is None:
    field_map = sm.get("_multi_setter_slots", {}).get(tool_name)
    if field_map:
      return _intake_multi_setter(sm, tool_name, response_data, field_map, out)
    task_info = sm.get("_executor_tasks", {}).get(tool_name)
    if task_info:
      return _intake_executor(sm, tool_name, response_data, task_info, out, outcome)
    return {"sm": sm, **out}
  return _intake_single_setter(sm, tool_name, response_data, slot_name, out)
