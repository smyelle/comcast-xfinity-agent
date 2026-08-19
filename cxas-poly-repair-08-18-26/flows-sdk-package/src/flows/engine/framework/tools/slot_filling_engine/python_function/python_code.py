# pylint: disable=undefined-variable,g-doc-args,g-doc-return-or-yield,g-docstring-missing-newline,g-no-space-after-docstring-summary,g-short-docstring-punctuation,line-too-long,missing-function-docstring,protected-access
"""Slot-filling DAG engine — reusable across projects.

FRAMEWORK CODE — shared across all agents using the slot-filling engine.
Do not add agent-specific logic here; customize behavior
via the per-agent {config_id}_dag tool.

Takes config + state dict, runs one turn of the DAG engine,
returns an action dict. All state flows through the sm dict
passed in and returned. CES-agnostic: no CES types, no
LlmRequest/LlmResponse, no tool visibility mutations.

Called from the before_model_callback via:
  tools.slot_filling_engine({"input_data": {...}}).json()["result"]

═══════════════════════════════════════════════════════════════════════════
MODULE MAP  (one file is forced — CES has no imports. Navigate by Ctrl-F'ing
the `# ═══ <NAME> ═══` banner for each section below.)
═══════════════════════════════════════════════════════════════════════════
  HELPERS .............. formatters, condition/response resolution, config
                         compilation (raw DAG config -> compiled config).
  SLOT STATE HELPERS ... slot/task activeness predicates, formatter lookup.
  AFFIRMATIVE DETECTION  yes/no parsing; auto-confirm + inline-confirm.
  & CONFIRMATION
  LOGGING .............. structured log sink (uses the one allowed global,
                         `_sm_ref` — see CONVENTIONS).
  DAG ENGINE COMPONENTS  fill_slots, steer-back, terminate (cancel/escalate),
                         terminal & post-executor handling, readback build.
  DAG EVALUATION ....... compute_dag_state (the decision), slot-error
                         handling, deferred groups, hidden-tools policy.
  SYSTEM INSTRUCTION ... collection/readback/phase/resume/full-SI builders;
  SUFFIX                 render_si for the gate/terminal (no-engine) turns.
  MAIN ORCHESTRATOR .... the per-turn slot-filling pipeline (_run_slot_filling).
  TURN PIPELINE STAGES   pure steps the entry calls in order: event pre-fill,
                         after-tool mappings, collection hints, correction
                         focus/apply, finalize-directive.
  TOOL ENTRY POINT ..... slot_filling_engine(input_data) — the SPINE: unpack,
                         run the pipeline stages, return {"action", "sm"}. Read
                         this first; each stage name points to its section.

═══════════════════════════════════════════════════════════════════════════
CONVENTIONS  (keep this file legible as it grows — enforced per change)
═══════════════════════════════════════════════════════════════════════════
  * Stages are PURE: inputs/outputs explicit in the signature; no global reads
    except the documented `_log`/`_sm_ref` sink. A function you can grasp
    without scrolling is one whose inputs are all in its signature.
  * Comments explain WHY (rationale, invariants, CES quirks, the bug that
    forced a guard). Code + tests explain WHAT — do not narrate mechanics, and
    delete a comment the moment it only restates the code. Co-locate a comment
    with the exact lines it explains; when code moves, its WHY moves with it.
  * Behavioral contracts live in evals/test_turn_engine.py as executable specs
    (a named test can't go stale — it fails when behavior drifts). Update the
    test red->green WITH the behavior; a behavior change with no test change is
    not done.
  * Docstrings state the contract (what in, what out, when); keep Args in sync
    with the signature (pylint g-doc-args is the gate).
  * New turn-decision logic = a named, pure, unit-tested stage placed under its
    section banner, PLUS a line added to the MODULE MAP above. Naming carries
    intent: predicates is_/has_/should_, computations as nouns, actions as
    verbs. If a name needs a comment to explain it, rename instead.
"""

import copy
import datetime
import hashlib
import json as json_lib
import logging
import random
import re
import string
from typing import Any, Optional, TypedDict


# Names of the synthesized passive terminal control slots. Each is derived from
# a top-level control block of the same name (not user-declared); the validator
# reserves these names and forbids anything from depending on them. Both tear
# the flow down via the generic `_terminate`, differing only in disposition:
#   cancel   -> abandon the request (outcome "cancelled")
#   escalate -> hand off to a human  (outcome "escalated")
_CANCEL_SLOT = "cancel"
_ESCALATE_SLOT = "escalate"
_CONTROL_BLOCKS = (_CANCEL_SLOT, _ESCALATE_SLOT)

# The framework setters that fire each control intent. The tool is uniform across
# flows (not per-DAG); a config's optional control block customizes only the
# disposition (say/outcome/transfer_to/exit_status/requires_readback, plus the
# `response` parts a telephony hand-off rides on — see _terminate_control).
_CONTROL_TOOLS = {_CANCEL_SLOT: "cancel_flow",
                  _ESCALATE_SLOT: "transfer_to_human"}

# Bound on the turns an `escalate.tasks` chain may occupy before the disposition
# runs anyway. A chain that neither fires nor exhausts (a member wedged on a
# retry ladder) would otherwise hold the caller on a rail whose whole point is to
# reach a human. Counts every armed turn, so a two-member chain uses about four.
_ESCALATE_PATH_MAX_TICKS = 8


# ═════════════════════════════════════════════════════════════════════
# FLOW-SCOPE LIFECYCLE — the single definition of "per-flow state"
# ═════════════════════════════════════════════════════════════════════
# Per-flow state must be thrown away atomically on every flow lifecycle event
# (start, switch, new instance, resume, complete, cancel, reap). To make that
# impossible to get wrong, we enumerate what to KEEP and discard EVERYTHING else —
# so any NEW sm key is per-flow (thrown away) BY DEFAULT, and only deliberately
# registered state survives. This inverts the old, bug-prone model where each call
# site hand-listed the few fields to reset and inevitably missed some (stale
# _setter_slots / _steer_back_turns / _retries / _intent_pass leaking across flows).
#
# DUPLICATED VERBATIM in slot_intake (CES tools can't import each other); a unit
# test asserts the two copies are identical.
_KEEP_SESSION = frozenset({
    # Whole-session state, independent of any one flow.
    "channel", "_flow_state", "_flow_instance_seq", "_log", "_log_level",
    "_shared_slots",
    # Debug capture (stripped before the engine call, but keep-safe if present).
    "_capture_si", "_si_trace", "_si_turn",
})
_KEEP_CONFIG = frozenset({
    # Re-derived from raw_config by the engine's config-change block on every
    # config switch — preserved across the clear (the new config overwrites them).
    "_config_id", "_bootstrap", "_gate_slot", "_single_flow", "_flow_types",
    "_route_cues", "_cancel_tool", "_escalate_tool", "_intent_first",
    "_default_flow", "_intent_switch", "_intent_changed_tool",
    "_first_engine_run",
})
_KEEP_TRANSITION = frozenset({
    # State that intentionally SPANS the flow boundary (it describes the handoff
    # itself, not the flow being torn down): the zombie carrier, the resume offer,
    # and the cross-flow new-instance chain signal.
    "_zombie", "_resume_offer_pending", "_resume_offer_turn", "_resume_result",
    "_resume_target", "_auto_resume_deferred", "_pending_switch",
    "_router_dispatched",
    # Component call frame (synchronous sub-DAG): the call stack, the one-shot
    # rebind-guard signal, and the deferred parent-fail flag. All describe a
    # handoff in progress (the CALL stack is the sibling of the PAUSE stack in
    # _flow_state), so they span the flow boundary like _zombie/_pending_switch.
    "_call_stack", "_frame_transition", "_fail_parent_flow",
})
_FLOW_KEEP = _KEEP_SESSION | _KEEP_CONFIG | _KEEP_TRANSITION


def _flow_clear(sm):
  """Throw away ALL per-flow state in ONE shot — the single place this happens.

  Pops every sm key not in the keep-set (so any unregistered key is discarded by
  default), then re-initialises the empty core containers. Callers seed the gate
  slot / carried shared values / status afterwards.
  """
  for k in [k for k in sm if k not in _FLOW_KEEP]:
    del sm[k]
  sm["filled"] = {}
  sm["pending"] = {}
  sm["deferred"] = {}
  sm["task_results"] = {}


# ═════════════════════════════════════════════════════════════════════
# COMPONENT CALL FRAME — data model (S0c frozen contract)
# ═════════════════════════════════════════════════════════════════════
# A Component is a tool-less Task whose `component` key names a child DAG. When the
# walk fires it, the engine PUSHES a call frame onto sm["_call_stack"] and runs the
# child DAG in the parent's stashed scope; on the child's terminal success it POPS,
# restores the parent scope, merges the child's outputs into parent `filled`, and
# re-walks (resume = re-walk, never a saved cursor). These TypedDicts are pure data
# (no methods) and live ONLY in the engine — intake defines no frame code, so it is
# not given the `TypedDict` import (see the framework design doc, §2.1).


class Scope(TypedDict):
  """The stashed PARENT scope while a CHILD DAG runs in this frame.

  Mirrors a `_flow_state` PAUSE-stack stash entry but renames its `slots` key to
  `filled` and drops the pause-only `id`/`flow` fields. Captured by COPY on push
  and restored IN PLACE on pop (see `_frame_return`/`_frame_abandon`).
  """
  filled: dict[str, object]
  pending: dict[str, object]
  deferred: dict[str, object]
  task_results: dict[str, object]


class Frame(TypedDict):
  """A single synchronous call frame: the PARENT scope stashed while a CHILD DAG
  runs. LIFO entries of sm["_call_stack"]; `_call_stack[-1]` is the active frame.
  Pure data — restored verbatim on frame return/abandon.
  """
  task: str                    # parent Component task name (the done-marker key)
  parent_config: str           # parent config_id (re-selected on pop; design §6.4)
  child_config: str            # child config_id this frame runs (read by _active_config)
  outputs: dict[str, str]      # child result key -> PARENT slot name (merge map)
  on_abort: str                # "skip" (default) | "fail_flow"
  scope: Scope                 # the stashed PARENT scope (captured by copy)
  # Repeated-component data (§R2.4); falsy on an ordinary (non-repeated) component.
  repeated: bool               # True => collect one element per child completion
  collect: str                 # the list-valued PARENT slot the elements land in
  element: dict[str, str]      # child slot key -> element field (per-element merge map)
  until: dict[str, Any]        # termination affordances ({max_count}/{done_setter})
  min_count: int               # minimum elements before "done" is honored


# ─────────────────────────────────────────────────────────────────────
# COMPONENT CALL FRAME — operations (push / return / abandon + selector)
# ─────────────────────────────────────────────────────────────────────
# The ONLY code that touches sm["_call_stack"]. Pure transformations over sm
# (mutating in place, like _flow_clear/_terminate). Specializations of existing
# blocks; the deltas are stated in each docstring. Wired into the hot paths by S3
# (fire-path descent, terminal-branch return, _terminate_control abandon); defined
# here as standalone, side-effect-contained helpers.


def _active_config(
    sm: dict[str, Any],
    config_id: str,
    raw_config: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
  """Resolve the config the engine runs THIS pass: the active call frame's CHILD
  config when a frame is on the stack, else the caller's config unchanged.

  Pure. The single seam (design §1): inserted before the config FETCH so the fetch,
  the rebind block, the intent-first gate, the Pass-A taxonomy, _run_slot_filling
  and _compute_dag_state all read the resolved config — frame-awareness lands at one
  point. When it overrides the id it also DROPS any caller-passed raw_config so the
  fetch re-resolves the child (offline via _CONFIGS_THIS_TURN). Returns the id
  unchanged when no frame is active or the caller already passed the child id.
  """
  stack = sm.get("_call_stack")
  if not stack:
    return config_id, raw_config
  child_id = stack[-1]["child_config"]
  if child_id == config_id:
    return config_id, raw_config
  return child_id, {}


def _frame_push(
    sm: dict[str, Any],
    *,
    task: str,
    parent_config: str,
    child_config: str,
    outputs: dict[str, str],
    on_abort: str,
    repeated: bool = False,
    collect: str = "",
    element: Optional[dict[str, str]] = None,
    until: Optional[dict[str, Any]] = None,
    min_count: int = 0,
    fail_block: str = "",
) -> None:
  """Enter a Component sub-flow: stash the PARENT scope (by COPY) and push a frame.

  Mutates sm in place; returns None (mirrors _flow_clear). The STASH half of
  _intake_bootstrap (captured by copy), minus the private/shared partitioning (a
  call frame stashes the WHOLE parent scope) and minus the _flow_state append.

  INVARIANT (copy on capture; in-place on restore): filled/pending/deferred/
  task_results are captured as SHALLOW COPIES (dict(...)), so a later in-place
  mutation of live sm cannot corrupt the stash; the mirror on the way out
  (_frame_return/_frame_abandon) restores via d.clear()+d.update(), never rebinding
  sm[k]. The `outputs` map is stored read-only. Capture happens HERE, before any
  rebind clear (hazard H-RESULTS).

  INVARIANT (caller arms the swap): the caller (S3 fire path) resets the live scope
  to the fresh child scope + seeds it, then sets sm['_config_id']=child_config and
  sm['_frame_transition']=True, so the rebind block re-derives the child's maps but
  does NOT wipe the seeded child scope. Depth/cycle are validated BEFORE this call.
  """
  frame: Frame = {
      "task": task,
      "parent_config": parent_config,
      "child_config": child_config,
      "outputs": outputs,
      "on_abort": on_abort,
      "scope": {
          "filled": dict(sm.get("filled", {})),
          "pending": dict(sm.get("pending", {})),
          "deferred": dict(sm.get("deferred", {})),
          "task_results": dict(sm.get("task_results", {})),
      },
      "repeated": bool(repeated),
      "collect": collect or "",
      "element": dict(element or {}),
      "until": dict(until or {}),
      "min_count": min_count or 0,
      # The control block a fail_flow abort should terminate the PARENT through
      # ("" -> the default "cancel"; "escalate" for an escalate.component deflection so
      # the outcome logs as escalated, not cancelled).
      "fail_block": fail_block or "",
  }
  sm.setdefault("_call_stack", []).append(frame)


def _restore_parent_scope(sm: dict[str, Any], frame: Frame) -> None:
  """Restore the stashed PARENT scope IN PLACE and arm the rebind for next pass.

  Shared by _frame_return and _frame_abandon. For each of filled/pending/deferred/
  task_results, restore via `d = sm[k]; d.clear(); d.update(saved)` — NEVER
  `sm[k] = saved` — so the engine's LOCAL aliases (_run_slot_filling 2932-2935;
  _handle_post_executor's `filled` param) stay valid. Restores to `filled` (NOT
  `pending` — DIFFERS from intake's resume restore, which re-validates): a
  synchronous call's parent slots were already validated before descent.

  Does NOT write `_config_id`: the selector (`_active_config`) owns the id, derived
  from `_call_stack` (now one shorter / empty after the caller popped the frame), so
  the NEXT pass resolves the right config — the outer frame's child for nested
  returns, or the agent's parent id at depth 0 — and the rebind re-derives its maps.
  Sets only `_frame_transition` so that next-pass rebind suppresses the task_results
  wipe and keeps the just-restored scope (incl. the component's done-marker).
  """
  scope = frame["scope"]
  for key in ("filled", "pending", "deferred", "task_results"):
    d = sm.setdefault(key, {})
    d.clear()
    d.update(scope[key])
  sm["_frame_transition"] = True


def _frame_return(
    sm: dict[str, Any],
    frame: Frame,
    child_filled: dict[str, object],
) -> None:
  """Normal child completion: pop, restore the PARENT scope, merge the child's
  outputs into parent `filled`, and mark the Component task done.

  Mutates sm in place; returns None.

  INVARIANT (merge source = child filled; SHAPE = a tool task's outputs): writes
  parent filled[parent_slot] = child_filled[child_key] per frame['outputs']
  ({child_key: parent_slot}). Same shape as _intake_executor's output merge, but the
  SOURCE is the child's final `filled`, not a tool return dict. A declared output the
  child did not produce is skipped (the downstream slot stays unfilled and is re-asked).

  INVARIANT (capture outputs BEFORE restoring — child_filled may ALIAS sm['filled']):
  the real caller (the terminal-branch fork) passes the child's live `filled`, which
  IS sm['filled']; `_restore_parent_scope` clears that dict in place, so the output
  values MUST be snapshotted first or the merge would read an emptied dict.

  INVARIANT (resume = re-walk, idempotent): writes the LITERAL
  task_results[frame['task']] = {'success': True} — the truthiness
  _compute_dag_state's already-succeeded skip reads. The validator forbids a custom
  success_check on a component (design §5.4), so the key is ALWAYS 'success'. The next
  walk advances past the Component with no cursor. Does NOT re-walk in-pass.
  """
  stack = sm.get("_call_stack") or []
  if stack and stack[-1] is frame:
    stack.pop()

  # Repeated component (§R2.4): one child completion == ONE element. Snapshot the
  # element fields + the child's done signal BEFORE _restore_parent_scope (which
  # clears child_filled, its alias). After restore, append into the top-level
  # sm['_repeat_acc'] staging list (outside the frame scope, so untouched by the
  # restore). Write filled[collect] + success ONLY when done; else withhold success
  # and arm the in-pass re-descend (_DESCENT_CONTINUE) so the next element's first
  # child question renders THIS turn.
  if frame.get("repeated"):
    element_map = frame.get("element") or {}
    elem = {field: child_filled[k]
            for k, field in element_map.items() if k in child_filled}
    until = frame.get("until") or {}
    done_key = until.get("done_setter")
    done_signal = child_filled.get(done_key) if done_key else None
    collect = frame["collect"]
    _restore_parent_scope(sm, frame)
    acc = sm.setdefault("_repeat_acc", {}).setdefault(collect, [])
    acc.append(elem)
    runaway = len(acc) >= _REPEAT_RUNAWAY_CAP
    if runaway:
      _log("component_repeat_runaway", "WARN", child=frame["child_config"],
           task=frame["task"], collect=collect, count=len(acc))
    spec = {"until": until, "min_count": frame.get("min_count", 0)}
    if runaway or _repeat_done(spec, acc, done_signal):
      sm["filled"][collect] = acc
      del sm["_repeat_acc"][collect]
      sm["task_results"][frame["task"]] = {"success": True}
      _log("component_return", child=frame["child_config"], task=frame["task"],
           depth=len(stack), collect=collect, count=len(acc))
    else:
      sm[_DESCENT_CONTINUE] = True     # re-offer the component in-pass (next element)
      # Steer the in-pass re-walk to the PARENT config, which owns the component-fire task, so it re-fires
      # the component and pushes the NEXT element's frame THIS pass. Without this the re-walk resets to the
      # entry config — which LIVE is the CHILD (before_agent resolves the child while the frame is active to
      # run the terminal turn). The child config has no component task, so it cannot descend the next element:
      # the pass ends frame-LESS, and the following turn's before_agent (empty _call_stack) mis-resolves to the
      # router → the model re-routes and the loop re-runs (chaining). The deterministic path masked this by
      # re-entering with the parent id every turn. parent_config is empty only for a root-level component
      # (no parent to re-fire) → falls back to the entry config, unchanged. Consumed once by the re-walk loop.
      if frame.get("parent_config"):
        sm["_rewalk_config"] = frame["parent_config"]
      _log("component_repeat_element", child=frame["child_config"],
           task=frame["task"], depth=len(stack), collect=collect, count=len(acc))
    return

  # A child key may name SEVERAL parent slots, same as an ordinary task's outputs.
  merged = {}
  for child_key, parent_slot in frame["outputs"].items():
    if child_key not in child_filled:
      continue
    for target in (parent_slot if isinstance(parent_slot, list) else [parent_slot]):
      merged[target] = child_filled[child_key]
  _restore_parent_scope(sm, frame)
  sm["filled"].update(merged)
  sm["task_results"][frame["task"]] = {"success": True}
  # Observability: mirror the component_descend log so the timeline shows the
  # return boundary (depth is the parent depth we returned TO; outputs are the
  # parent slots the child produced).
  _log("component_return", child=frame["child_config"], task=frame["task"],
       depth=len(stack), outputs=sorted(merged) or None)


def _frame_abandon(sm: dict[str, Any], frame: Frame) -> str:
  """The child did NOT succeed: pop and return to the PARENT. The single collapse
  point for BOTH user-cancel inside the child AND the child's own failure-driven
  termination (retries-exhaust / end_session); the outcome is governed by
  frame['on_abort'] ('skip' | 'fail_flow').

  TOTAL over both dispositions; mutates sm in place and RETURNS the disposition
  string the one frame-aware branch in _terminate_control dispatches on (no
  call-order contract; hazard H-ABANDON). Restores the parent scope IN PLACE (same
  invariant as _frame_return) but does NOT merge outputs and does NOT mark the task
  done — on 'skip' the walk re-offers the Component next pass.

  'fail_flow' additionally sets the one-shot sm['_fail_parent_flow']=True. This
  function NEVER calls _terminate and NEVER zombifies: its caller's `config`/scope
  locals are the CHILD pass's, so terminating here would build a zombie from CHILD
  data. The parent-flow termination is DEFERRED to the next pass (top of
  _run_slot_filling consumes the one-shot under the re-resolved PARENT config; §6.8).
  Escalate does NOT come here (conversation-wide; hazard H-ESC).
  """
  stack = sm.get("_call_stack") or []
  if stack and stack[-1] is frame:
    stack.pop()
  _restore_parent_scope(sm, frame)
  disposition = frame.get("on_abort") or "skip"
  # Repeated component abort (§R2.5): a real mid-collection abort ('skip') finalizes
  # what we already have rather than re-offering the component — write filled[collect]
  # from the accumulator and mark the task done IF the minimum is met; otherwise the
  # collection under-ran, so honor fail_flow semantics. (fail_flow disposition falls
  # through to the shared handling below.) Non-repeated path is byte-identical.
  if frame.get("repeated") and disposition == "skip":
    collect = frame["collect"]
    acc = sm.get("_repeat_acc", {}).get(collect, [])
    if len(acc) >= frame.get("min_count", 0):
      sm.setdefault("filled", {})[collect] = sm.setdefault(
          "_repeat_acc", {}).pop(collect, [])
      sm.setdefault("task_results", {})[frame["task"]] = {"success": True}
      _log("component_repeat_abort_finalize", "WARN", child=frame["child_config"],
           task=frame["task"], collect=collect, count=len(acc))
    else:
      sm["_fail_parent_flow"] = frame.get("fail_block") or True
      _log("component_repeat_abort_underflow", "WARN", child=frame["child_config"],
           task=frame["task"], collect=collect, count=len(acc),
           min_count=frame.get("min_count", 0))
    return disposition
  if disposition == "fail_flow":
    # Truthy value = "fail the parent"; a STRING additionally names the control block
    # to terminate through (escalate.component -> "escalate"), else the default "cancel".
    sm["_fail_parent_flow"] = frame.get("fail_block") or True
  # Observability: the child did NOT succeed — log the return boundary with how
  # the parent handles it (skip = re-offer the component; fail_flow = fail parent).
  _log("component_abandon", "WARN", child=frame["child_config"],
       task=frame["task"], depth=len(stack), disposition=disposition)
  return disposition


_HOLD_EXTENSION_CAP = 4           # times real speech may reset the silence window before it exhausts

# ── Recognizing a request for time ────────────────────────────────────
# `hold_phrases` names the MARKERS ("hold on", "let me grab"); `hold_vetoes` names the
# things a caller says that carry a marker and are not a request for time at all. Both
# are matched on WORD BOUNDARIES, which is what lets the marker list carry the bare
# interruption words: as plain substrings "hold" matched "household" and "sec" matched
# "second opinion", so the bare markers had to be left out and the commonest way a caller
# asks for a moment went unrecognized.
#
# The veto is what buys them back. "Hold on" prefixes a request for time about as often
# as it prefixes a question, and answering "why do you need that?" with "take your time"
# is a worse failure than re-asking, because the caller is not going to say it again --
# they said it once and got patience.
_DEFAULT_HOLD_VETOES = [
    # Asking about the request rather than answering it.
    "why do you", "why are you", "why would you", "why do i", "why should i",
    "what do you need", "what for", "what is that for", "what s that for",
    "what do you mean",
    # Asking for the question again. Going quiet is the one wrong answer.
    "what did you say", "what was that", "say that again", "repeat that",
    "can you repeat", "come again", "didn t catch", "didn t hear", "pardon",
    # Asking for a person. Waiting patiently strands them.
    "a person", "real person", "a human", "an agent", "representative",
    "supervisor", "operator", "transfer me", "talk to someone", "talk to a",
    "speak to someone", "speak to a", "get me someone",
    # Saying they cannot answer, which is the opposite of asking for time to.
    "already gave", "already told", "already said", "told you",
    "don t have an", "don t have a", "do not have an", "do not have a",
    "can t find", "cannot find", "couldn t find", "can t remember",
    "don t remember", "don t know it", "no idea",
    # Correcting what the call is about.
    "this is about", "calling about", "not about", "i thought this was",
    "wrong department",
    # Calling the whole thing off. Without these a marker swallows the request: the
    # keyword backstops are suppressed on a hold turn, so "hold on, cancel that" enters
    # hold mode and the cancellation never reaches `cancel_flow`.
    "cancel", "stop", "never mind", "nevermind", "forget it", "forget about it",
]
#: Digits in the utterance at or above which the caller is reading a VALUE, not stalling.
#: Five clears the small numbers a request for time carries ("give me 2 minutes", "wait
#: 30 seconds") and sits below any identifier worth collecting. Counted across the whole
#: utterance, because a caller reads a long number in groups ("806 910 023 035 9946").
_HOLD_VALUE_DIGITS = 5


def _hold_normalize(text):
  """Lowercase, and every run of non-alphanumerics down to one space.

  Both sides of every comparison go through this, so an apostrophe in the utterance and
  one in the phrase agree: "don't have" and "don t have" both normalize to the latter.
  The leading and trailing spaces are what make a plain `in` a word-boundary match.
  """
  out = []
  for ch in text.lower():
    out.append(ch if ch.isalnum() else " ")
  return " %s " % " ".join("".join(out).split())


def _hold_phrase_in(normalized, phrase):
  """Is `phrase` present in an already-normalized utterance, on word boundaries?"""
  return _hold_normalize(phrase).strip() != "" and _hold_normalize(phrase) in normalized


def _is_hold_request(text, no_input_cfg):
  """Is this utterance the caller asking for time, rather than answering?

  A marker is necessary and not sufficient. The utterance must carry one, must not read
  a value, and must not be one of the things that wear a marker without being a request
  for time (see `_DEFAULT_HOLD_VETOES`). Authors override the veto list with
  `no_input.hold_vetoes`; an explicit empty list restores the marker-only behavior.
  """
  markers = no_input_cfg.get("hold_phrases") or []
  if not markers:
    return False
  normalized = _hold_normalize(text)
  if not any(_hold_phrase_in(normalized, m) for m in markers):
    return False
  # The caller read the answer out. Whatever else the turn carries, it is not a stall,
  # and acknowledging it as one drops the value: the ack preempts the model, so the
  # setter that would have captured it is never called.
  if sum(ch.isdigit() for ch in text) >= _HOLD_VALUE_DIGITS:
    return False
  vetoes = no_input_cfg.get("hold_vetoes")
  if vetoes is None:
    vetoes = _DEFAULT_HOLD_VETOES
  return not any(_hold_phrase_in(normalized, v) for v in vetoes)
_FRAME_DEPTH_CAP = 3              # max nested Component call frames (design §6.3 / Q1)
_DESCENT_END_PASS = {"hide_tools": [], "preempt": False}   # ordinary end-of-turn no-op
_DESCENT_CONTINUE = "_descent_continue"  # one-shot sm flag: a frame was pushed THIS pass; re-walk the child in-pass (§6.1)

# ── Repeated slots (§R2) ──────────────────────────────────────────────
# Reserved pending key a Mode-A `done_setter` stages ({@TOOL} -> f"{prefix}{slot}"):
# consumed FIRST at promote (never lingers in pending; never reaches the general
# promote loop or trips _compute_hidden_tools' hide-all-setters branch).
_REPEAT_DONE_PREFIX = "__repeat_done__:"
# Hard backstop: force-finalize a repeated collection past this many elements so a
# mis-wired done_setter cannot loop forever (§R2.4).
_REPEAT_RUNAWAY_CAP = 50

# ── skip_readback_if_matches ──────────────────────────────────────────
# Shortest staged value whose digits may stand in for "the caller already confirmed
# these digits out loud". Below this, equality is coincidence — "yes" == "yes" and a
# menu "1" == a menu "1" both compare equal after digit-stripping — and skipping a
# readback on a coincidence silently accepts a value nobody confirmed.
_READBACK_SKIP_MIN_DIGITS = 5


def _repeat_done(spec, acc_list, done_signal=None) -> bool:
  """The single termination predicate for a repeated slot (Mode A + Mode B, §R2.0).

  `spec` is the `repeated` dict (Mode A) or a `{"until":..., "min_count":...}` view
  built from the call frame (Mode B). `acc_list` is the elements collected so far;
  `done_signal` is the truthy done marker (Mode A companion flag popped from
  pending, or Mode B child `done_setter` value). v1 affordances are EXACTLY
  `until.max_count` (int > 0) and `until.done_setter`, with `min_count` AND-ed in so
  a "done" before the minimum re-asks. No `condition`-based termination (§R2.0).
  """
  n = len(acc_list)
  if n < spec.get("min_count", 0):
    return False
  until = spec.get("until") or {}
  max_count = until.get("max_count")
  # CES deserializes config numbers as floats, so `max_count: 3` arrives as 3.0;
  # accept int or float (never bool) and compare numerically.
  if isinstance(max_count, (int, float)) and not isinstance(max_count, bool) and n >= max_count:
    return True
  if until.get("done_setter") and done_signal:
    return True
  return False


def _component_fire_action(
    sm: dict[str, Any],
    config: dict[str, Any],
    task_def: dict[str, Any],
    filled: dict[str, Any],
) -> dict[str, Any]:
  """Descend into a Component sub-flow (the fire-path diversion, §4.2 / §6.1).

  Seeds the child's fresh scope (its gate_slot + the parent-supplied inputs),
  pushes the call frame, swaps _config_id to the child + arms _frame_transition,
  and arms _skip_pass_a_once so the child's entry turn goes straight to Pass B.
  Returns an ordinary end-of-turn action dict: the pass ENDS and the selector walks
  the child next pass (§6.1 END-THE-PASS). NEVER raises — the runtime depth/cycle
  guard (§6.3) returns a graceful action instead, because an uncaught engine
  exception is the documented "having trouble" empty render.

  Gate auto-seed: a child DAG with a gate_slot would otherwise short-circuit to its
  gate/entry turn (no router fills it in a sub-flow), so we seed the child gate_slot
  with the child's flow identity (its config_id) and the child collects immediately.

  Parent precedence (Q4) is structural: an input whose parent slot is filled is
  seeded into the child; any child input not listed is collected by the child.
  """
  child_id = task_def["component"]
  task_name = task_def["name"]
  on_abort = task_def.get("on_abort") or "skip"
  # Repeated component (§R2.4): the frame carries the collect/element/until config so
  # _frame_return appends one element per child completion. Falsy for a plain component.
  repeated_spec = task_def.get("repeated")
  repeated = bool(repeated_spec)
  collect = task_def.get("collect") or ""
  element = task_def.get("element") or {}
  until = (repeated_spec or {}).get("until") or {}
  min_count = (repeated_spec or {}).get("min_count", 0)
  stack = sm.setdefault("_call_stack", [])

  # Runtime depth/cycle guard (§6.3) — NON-RAISING backstop for a dynamically
  # resolved ref the validator could not catch (latest-wins). No frame is pushed on
  # refusal, so no _frame_abandon restore is needed (the live scope is the parent's).
  if len(stack) >= _FRAME_DEPTH_CAP or any(f["child_config"] == child_id for f in stack):
    _log("component_descent_refused", "ERROR", child=child_id, depth=len(stack))
    if on_abort == "fail_flow":
      # Carry the source control block ("escalate" for a deflection) so the parent
      # tears down through the matching block; "" -> the default "cancel".
      sm["_fail_parent_flow"] = task_def.get("fail_block") or True  # consumed next pass (§6.8)
    else:
      # skip: mark done. For a repeated component, finalize whatever was collected
      # into filled[collect] so the marked-done task doesn't leave the list unfilled.
      if repeated:
        filled[collect] = sm.setdefault("_repeat_acc", {}).pop(collect, [])
      sm.setdefault("task_results", {})[task_name] = {"success": True}
    return dict(_DESCENT_END_PASS)

  parent_config = sm.get("_config_id", "")
  # Snapshot parent-supplied seeds BEFORE _frame_push stashes / we clear the scope.
  seeds = {child_input: filled[parent_slot]
           for parent_slot, child_input in _task_input_pairs(task_def.get("inputs"))
           if parent_slot in filled}
  # Repeated component with per-element INPUT binding (`repeated.over` + `repeated.each`): seed the child
  # with the CURRENT iteration's element from the parent LIST slot — so each invocation gets e.g. the i-th
  # retrieved question. Iteration index = #elements already collected; when the list is exhausted, finalize
  # the collection instead of descending. Absent `over`/`each` ⇒ unchanged (byte-identical for plain Mode B).
  _rep_over = (repeated_spec or {}).get("over")
  _rep_each = (repeated_spec or {}).get("each") or {}
  if _rep_over and _rep_each:
    _lst = filled.get(_rep_over) or []
    _i = len(sm.get("_repeat_acc", {}).get(collect, []))
    if _i >= len(_lst):                             # list exhausted (or empty) → finalize, don't descend
      filled[collect] = sm.setdefault("_repeat_acc", {}).pop(collect, [])
      sm.setdefault("task_results", {})[task_name] = {"success": True}
      _log("component_repeat_over_done", child=child_id, task=task_name,
           collect=collect, count=len(filled[collect]))
      return dict(_DESCENT_END_PASS)
    if isinstance(_lst[_i], dict):
      seeds.update({_cs: _lst[_i].get(_fld) for _cs, _fld in _rep_each.items()})
    # The per-element seed (e.g. the KBA question `script`) is delivered to the child BEFORE its first
    # question is asked — exactly the event-prefill shape. Arm the event-prefill flag so the child's opening
    # question (ask interpolated from the just-seeded value) is DELIVERED AS A PREEMPT (verbatim) rather than
    # left to free model generation. Without this the model, holding the parent flow's context, can drift
    # off-script between elements (e.g. re-asking the already-chosen verification method). One question per
    # element, spoken exactly as authored.
    sm["_event_prefilled_this_turn"] = True
  child_cfg = _engine_load_config(child_id)
  child_gate = child_cfg.get("gate_slot")

  _frame_push(sm, task=task_name, parent_config=parent_config,
              child_config=child_id, outputs=task_def.get("outputs") or {},
              on_abort=on_abort, repeated=repeated, collect=collect,
              element=element, until=until, min_count=min_count,
              fail_block=task_def.get("fail_block") or "")

  # Reset the live scope to a FRESH child scope (the rebind's wipe is suppressed by
  # _frame_transition, so it is reset here), in place to keep the engine's aliases.
  for key in ("filled", "pending", "deferred", "task_results"):
    sm.setdefault(key, {}).clear()
  if child_gate:
    sm["filled"][child_gate] = child_id        # gate auto-seed -> child collects now
  sm["filled"].update(seeds)

  # Do NOT write _config_id: the selector derives it from _call_stack (now holding
  # this frame), so the NEXT pass resolves the child and the rebind derives its maps.
  sm["_frame_transition"] = True               # suppress the next-pass rebind wipe
  sm["_skip_pass_a_once"] = True               # child entry turn -> Pass B (H-ENTRY)
  _log("component_descend", child=child_id, task=task_name, depth=len(stack))
  sm[_DESCENT_CONTINUE] = True
  return dict(_DESCENT_END_PASS)


def _task_input_pairs(inputs) -> list[tuple[str, str]]:
  """Normalize a task's `inputs` to (my_slot, callee_input) pairs.

  Mirrors _task_input_slots/_task_input_args: a dict {my_slot: callee_input} yields
  its items; a bare list [name, ...] means {name: name}. Used by the descent seed.
  """
  if isinstance(inputs, dict):
    return list(inputs.items())
  return [(name, name) for name in (inputs or [])]


# ═════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════


def _pick_filler(sm, spec, filled):
  """Choose a latency filler line, substituted against filled slots, or None.

  `spec` is one string (always spoken — the historical shape) or a pool to pick from.
  A ``None`` entry in the pool is SILENCE: an ordinary member rather than a separate
  probability knob, so "sometimes say nothing" is authored the same way as "sometimes
  say this", and either is weighted by repeating the entry. An agent that says the
  same five words on every wait sounds scripted, which is the failure a filler exists
  to avoid in the first place.

  A line whose placeholder is not filled yet is DROPPED rather than spoken raw. The
  older inline `except KeyError: pass` left the braces in the text, so an unfilled
  slot was read out as "let me look up order open-brace order id close-brace"; with a
  pool that now degrades to another line, and with a lone string to silence.

  Seeded from a per-session salt plus the turn number, so the line varies across
  CALLERS. The salt is the load-bearing half: seeded from the config alone, every caller
  hears the same line, and a flow with only one filler turn never rotates at all — which
  is exactly the scripted-sounding failure a pool exists to prevent.

  Calling this twice in one turn can return different lines, because a pick updates
  `_filler_last` and the next call avoids it. That is the caller's problem to prevent,
  not this function's: `_arm_model_filler` holds a once-per-turn guard, and the task
  path resolves once per tool fire, where two fires wanting two different lines is
  right anyway.

  Shared by both deliveries — the task path (spoken with the tool call) and the
  model-turn path (spoken as a partial preempt) — because a repetitive tool filler is
  exactly as grating as a repetitive model one.
  """
  pool = [spec] if isinstance(spec, str) else list(spec or [])
  if not pool:
    return None
  salt = sm.get("_filler_salt")
  if not salt:
    salt = "%08x" % random.getrandbits(32)
    sm["_filler_salt"] = salt
  rng = random.Random("%s:%s" % (salt, sm.get("_turn_n", 0)))
  last = sm.get("_filler_last")
  # Avoid an immediate repeat, but only when dropping it leaves a real choice: a
  # two-entry pool like ["One sec.", None] would otherwise strictly alternate, which
  # is a pattern rather than variety.
  choices = [p for p in pool if p != last] if len({str(p) for p in pool}) > 2 else pool
  for cand in rng.sample(choices, len(choices)):
    if cand is None:
      sm["_filler_last"] = None
      return None
    try:
      out = cand.format(**filled)
    except (KeyError, IndexError):
      continue  # placeholder not filled on this turn — try the next candidate
    sm["_filler_last"] = cand  # remember the TEMPLATE, so rotation survives rendering
    return out
  return None


def _log_suppressed_filler(task_def, task_name):
  """Record a `filler_say` the surface dropped, so the loss is not invisible.

  The gate itself is right: a latency mask is an empty extra bubble on a surface
  that shows a spinner. But an author who splits ONE approved sentence across
  `filler_say` and `then_say` loses its first half here, silently, on every
  surface with no `filler` capability — and the second half renders on its own, so
  the caller reads the consequence of a finding that was never stated. That
  shipped once. A DEBUG line is the cheapest thing that would have made it
  visible immediately.
  """
  if (task_def or {}).get("filler_say") and not _cap("filler", True):
    _log("filler_suppressed_by_surface", "DEBUG", task=task_name,
         surface=(_surface_ref or {}).get("name"))


def _arm_model_filler(sm, config, slot_map, asked, filled, action):
  """Arm a latency filler for a turn the MODEL will author.

  A task's filler rides the same preempt as its `function_call`, so it is free. This
  one has no call to ride: `before_model` speaks it as a `partial` preempt, which
  holds the floor so the model's own reply still lands in the same turn (probe 57) —
  but it costs one of the ten reasoning passes to do it. Hence two guards: once per
  caller turn, and only while the turn has passes to spare. Deep into a ladder the
  caller needs the answer more than they need "one sec".
  """
  if not _cap("filler", True):
    return  # chat surface: a spoken filler is an empty bubble; the UI shows a spinner
  turn = sm.get("_turn_n", 0)
  if sm.get("_filler_turn") == turn or action.get("silent"):
    return
  if sm.get("_invoke_n", 0) - sm.get("_invoke_at_turn", 0) > 2:
    return
  # The slot being ASKED, which is not the same as `pending` — pending holds values
  # awaiting readback and is empty on an ordinary question turn.
  slot_def = slot_map.get(asked) or {}
  line = _pick_filler(sm, slot_def.get("filler_say") or config.get("filler_say"),
                      filled)
  if not line:
    return  # no filler authored, or the pool chose silence this turn
  sm["_filler_turn"] = turn
  action["filler_partial"] = line
  _log("filler_partial_armed", slot=asked, text=line)


def _arm_classify_filler(sm, raw_config, action, user_text=""):
  """Arm the flow's filler on a turn the engine spends CLASSIFYING.

  The routing and Pass-A returns leave the engine long before `_finalize_directive`,
  which is the only other place a filler is armed — so the turn the caller waits
  longest on was the one turn that could not cover itself. Measured end of caller
  speech to first agent audio: 2.55s for an ordinary in-flow turn against 4.3-6.5s
  for a router turn, the difference being serialized round trips (`set_active_flow`
  and the path recorder are real calls), not prompt size or route depth.

  There is no compiled config on these paths and no slot is being asked, so this
  reaches only the FLOW-level `filler_say` — which is the right scope anyway: a
  router is its own config, and its line is spoken before the intent is known.

  A filler covers a CALLER's wait, so a turn no caller spoke on does not get one.
  Session start arrives as `<event>session start</event>` in the user text and is a
  router turn like any other, so without this the opening turn was armed too — which
  put a holding line in front of a greeting nobody had waited for, and stacked a
  partial preempt onto the one turn that also carries the welcome.
  """
  if not _caller_spoke(user_text):
    return action
  _arm_model_filler(sm, raw_config, {}, "", sm.get("filled") or {}, action)
  return action


def _caller_spoke(user_text):
  """True when the turn carries a real caller utterance rather than a platform event."""
  spoken = (user_text or "").strip()
  return bool(spoken) and not (
      spoken.startswith("<event>") and spoken.endswith("</event>"))


def _router_welcome(sm, raw_config, action, user_text=""):
  """Speak the router's `welcome_slot` announce on the opening turn.

  A router returns before `_run_slot_filling`, the only caller of the announce cascade —
  so `welcome_slot` could be declared and validated and then never read, on the one flow
  whose whole job is to own the opening turn. Authors worked around it by instructing the
  model to say the greeting, which makes the first thing a caller hears improvised: on
  one agent twelve drives produced five different openings, one of them announcing a
  diagnostic before the caller had said a word.

  Only on a turn the caller has not spoken on, and only once — after that the router is
  routing, not greeting. Text alone ends the turn (ces-probes 26), which is exactly what
  a greeting wants: say it, then listen.
  """
  name = (raw_config.get("bootstrap") or {}).get("welcome_slot")
  if not name or _caller_spoke(user_text) or sm.get("_router_welcome_said"):
    return None
  for slot in raw_config.get("slots") or []:
    if slot.get("name") == name and slot.get("response"):
      sm["_router_welcome_said"] = True
      action["preempt"] = True
      action["response"] = slot["response"]
      _log("router_welcome", slot=name)
      return action
  return None


def _tool_ref(name: str) -> str:
  return "{@TOOL: " + name + "}"


def _output_targets(outputs) -> list:
  """Every slot an `outputs` map writes to (a key may name one slot or several)."""
  out = []
  for value in (outputs or {}).values():
    if isinstance(value, list):
      out.extend(value)
    else:
      out.append(value)
  return out


def _normalize_sources(source) -> list[str]:
  """Normalize slot source to a list."""
  if isinstance(source, list):
    return source
  return [source] if source else ["user"]


def _task_input_slots(inputs) -> list[str]:
  """Return slot names from task inputs (list or dict)."""
  if isinstance(inputs, dict):
    return list(inputs.keys())
  return inputs


def _task_input_args(inputs, filled: dict[str, object]) -> dict[str, object]:
  """Build tool args from task inputs, mapping slot names to param names."""
  if isinstance(inputs, dict):
    return {param: filled[slot] for slot, param in inputs.items()
            if slot in filled}
  return {k: filled[k] for k in inputs if k in filled}


def _remote_wire_args(config, tool, args):
  """Render a REMOTE start call's arguments the way its wrapper declares them.

  Every generated toolset wrapper takes `str` parameters — that is what a slot holds —
  and casts the ones whose spec says otherwise inside its own body. CES type-checks a
  `function_call` against that signature BEFORE the body runs, so a non-string value is
  refused outright:

      Invalid value for parameter `duration_seconds` in the tool call in Python tool
      build_report: Expected `String`, received `kotlin.Double` (240.0).

  Which is not a hypothetical for a remote tool, because it is the only kind whose
  params are TYPED: an author writing `params={"duration_seconds": int}` has said the
  value is a number, and a numeric slot reaches here as one. Worse, it reaches here as a
  FLOAT even when the author wrote an integer — a config default crosses CES as a
  protobuf Struct, where every number is a double — so `str(240.0)` would hand the
  wrapper `"240.0"`, which its `int(...)` cannot parse and which the service would then
  receive as a string. An integral float is rendered as the integer it is.

  Scoped to the remote registry rather than applied to every dispatch, because the
  opposite is true of an ordinary python tool: `deliver_report(rows: int)` is declared
  `int` and stringifying its argument would break the call this repairs.
  """
  if not (config or {}).get("remote_tools"):
    return args
  if not ((config["remote_tools"] or {}).get(tool)):
    return args
  out = {}
  for key, value in (args or {}).items():
    if isinstance(value, bool):
      out[key] = "true" if value else "false"
    elif isinstance(value, float) and value.is_integer():
      out[key] = str(int(value))
    elif isinstance(value, (int, float)):
      out[key] = str(value)
    elif value is None:
      out[key] = ""
    else:
      out[key] = value
  return out


# ── Built-in readback formatters ──────────────────────────────────


def _format_date(v: str, text: str = "") -> str:
  """Format a date for speech: "on Month Nth" (ISO) or "<text> Month Nth, YYYY".

  Accepts BOTH `YYYY-MM-DD` and the `MMDDYYYY` a date setter may store. MMDDYYYY
  carries the year in the spoken form because a slot that books a date range up to a
  year out is ambiguous as "December 1st" alone.

  `text` replaces the default "on" lead-in, which is what lets a preempted readback
  say "the temporary lift will start on December 1st, 2026" instead of the bare "on
  December 1st". Without a formatter a `readback_verbatim` slot sends the raw
  MMDDYYYY straight to TTS, where "12012026" is read as one twelve-million-something
  number.
  """
  raw = str(v).strip()
  dt = None
  for pattern in ("%Y-%m-%d", "%m%d%Y"):
    try:
      dt = datetime.datetime.strptime(raw, pattern)
      break
    except (ValueError, TypeError):
      continue
  if dt is None:
    return f"{text} {v}".strip() if text else f"on {v}"
  day = dt.day
  suffix = (
      "th"
      if 11 <= day <= 13
      else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
  )
  spoken = f"{dt.strftime('%B')} {day}{suffix}"
  if len(raw) == 8 and raw.isdigit():
    spoken = f"{spoken}, {dt.year}"
  return f"{text} {spoken}".strip() if text else f"on {spoken}"


def _format_time(v: str) -> str:
  """Format time as 'at H:MM AM/PM'."""
  try:
    parts = str(v).split(":")
    h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    if not 0 <= h <= 23 or not 0 <= m <= 59:
      # Out of range: `h % 12` would speak "25:00" as "at 1:00 PM", stating a time
      # the producer never supplied. Degrade to the raw value like an unparseable one.
      return f"at {v}"
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"at {h12}:{m:02d} {period}"
  except (ValueError, TypeError, IndexError):
    return f"at {v}"


def _format_prefix(v, text: str = "", values: Optional[dict] = None) -> str:
  """Format value with a text prefix, optionally speaking it via a lookup.

  `values` maps a stored slot value to its spoken form, so an enum slot can be read
  back as a sentence rather than as its key: a `freeze_action` slot holding
  "remove_lift" would otherwise reach TTS as "Just to confirm — remove_lift." An
  unmapped value falls through to itself, so adding a new enum key degrades to the
  old behaviour rather than going silent.
  """
  spoken = (values or {}).get(v, v)
  return f"{text} {spoken}".strip() if text else f"{spoken}"


def _norm_for_dup(s) -> str:
  """Normalize a spoken line for duplicate detection: lowercase, punctuation to
  spaces, whitespace collapsed. Punctuation must go — an announce written with an
  em-dash and an ask written without it are the same sentence to a listener."""
  return " ".join(
      "".join(c if c.isalnum() else " " for c in str(s or "").lower()).split()
  )


def _format_digits(v, text: str = "") -> str:
  """Speak a digit string one digit at a time ("2 1 2 …"), with an optional label.

  Needed by `readback_verbatim`: a preempted readback goes straight to TTS, where an
  unspaced "2124561234" is read as one enormous number rather than a phone number.
  Non-digits are dropped so a punctuated value ("212-456-1234") still reads cleanly.

  A value with NO digits falls back to the value itself. Dropping non-digits turns a
  sentinel like a confirmation-SMS slot's "declined" into an empty string, and with
  `readback_verbatim` that empties a line the model cannot rescue — suppression must
  never create silence. The label is dropped too on that path: "the mobile number
  declined" is worse than "declined".
  """
  raw = str(v if v is not None else "")
  spaced = " ".join(ch for ch in raw if ch.isdigit())
  if not spaced:
    return raw
  return f"{text} {spaced}".strip() if text else spaced


def _digits_only(v) -> str:
  """Bare digit string, for value identity ("212-555-0199" -> "2125550199")."""
  return "".join(ch for ch in str(v if v is not None else "") if ch.isdigit())


def _format_plural(v, one: str = "", other: str = "") -> str:
  """Format value with singular/plural unit.

  Tolerates a non-numeric value the way `_format_count` does (None/"" -> 0, any
  other non-numeric scalar -> 1): a readback formatter must never raise, since
  `_build_readback` calls it unguarded and the exception kills the whole
  confirmation turn.
  """
  try:
    n = int(v)
  except (ValueError, TypeError):
    n = 0 if v in (None, "") else 1
  return f"{n} {one if n == 1 else other}"


def _format_none_sub(v, default: str = "") -> str:
  """Substitute a default for none-like values."""
  if str(v).lower() in ("none", "no", "nothing"):
    return default
  return str(v)


def _flatten_scalar(x) -> str:
  """Render a value for the spoken surface with NO Python `repr` syntax,
  recursing into nested dicts/lists (join with ', ') so brackets, braces and
  quotes never leak — even for a dict element whose values are themselves lists
  or dicts (§R2.6)."""
  if isinstance(x, dict):
    return ", ".join(_flatten_scalar(v) for v in x.values())
  if isinstance(x, (list, tuple)):
    return ", ".join(_flatten_scalar(v) for v in x)
  return str(x)


def _format_join(v, sep: str = ", ", each: str = "{item}") -> str:
  """List-aware readback formatter (§R2.6): render each element via `each` and join.

  A dict element fills `each` by keyword (`each.format(**el)`, so an element field
  is `{field}`); a scalar fills the reserved `{item}` name. Empty list -> "".
  Tolerates a NON-list value (wrapped as a single element) so a scalar slot that
  later gains a `join` fmt never raises.
  """
  items = v if isinstance(v, list) else ([] if v in (None, "") else [v])
  parts = []
  for el in items:
    try:
      if isinstance(el, dict):
        # Flatten any nested list/dict FIELD values first, so a `{field}` whose
        # value is itself a list/dict renders as scalars (e.g. "vip, vegan")
        # rather than leaking `['vip', 'vegan']` repr on the happy path (§R2.6).
        safe = {k: (_flatten_scalar(val) if isinstance(val, (list, dict, tuple))
                    else val)
                for k, val in el.items()}
        parts.append(each.format(**safe))
      else:
        parts.append(each.format(
            item=_flatten_scalar(el) if isinstance(el, (list, dict, tuple))
            else el))
    except (KeyError, IndexError):
      # A dict element whose `each` template references a field an optional child
      # slot never filled would str(el) into Python dict repr on the spoken
      # surface (§R2.6 forbids it) — recursively flatten to its values instead.
      parts.append(_flatten_scalar(el))
  return sep.join(parts)


def _format_count(v, one: str = "", other: str = "") -> str:
  """List-aware count formatter (§R2.6): "<n> <one|other>", n = len(v).

  Tolerates a NON-list value without raising: an int-like scalar uses its numeric
  value, anything else counts as 0 (empty/none) or 1 (a lone truthy scalar).
  """
  if isinstance(v, list):
    n = len(v)
  else:
    try:
      n = int(v)
    except (ValueError, TypeError):
      n = 0 if v in (None, "") else 1
  return f"{n} {one if n == 1 else other}"


def _fallback_list_render(v) -> str:
  """Spoken-surface fallback for a list value with NO formatter (§R2.6): join
  without leaking Python `repr`. Elements (incl. nested dicts/lists) are
  recursively flattened to their scalar values — never `str(dict)`/`str(list)`
  so no braces/brackets/quotes reach the surface."""
  if not isinstance(v, list):
    return _flatten_scalar(v)
  return ", ".join(_flatten_scalar(el) for el in v)


_BUILTIN_FORMATTERS = {
    "date": _format_date,
    "time": _format_time,
    "digits": _format_digits,
}


def _resolve_exhaust_action(
    exhaust: dict[str, Any],
    filled: dict[str, Any],
) -> Optional[dict[str, Any]]:
  """Resolve on_exhaust 'then' to a function_call dict.

  Supports:
    "then": "escalate"  -> {"name": "escalate", "args": {}}
    "then": {"tool": "transfer", "args": {"name": "{guest_name}"}}
      -> args values with {slot} placeholders are filled from filled.

  Args:
    exhaust: The on_exhaust config dict with optional 'then' key.
    filled: Currently filled slot values for placeholder resolution.

  Returns:
    A function_call dict {"name": ..., "args": {...}} or None.
  """
  then = exhaust.get("then")
  if not then:
    return None
  if isinstance(then, str):
    return {"name": then, "args": {}}
  if isinstance(then, dict):
    tool = then.get("tool", "")
    raw_args = dict(then.get("args", {}))
    for k, v in raw_args.items():
      if isinstance(v, str):
        try:
          raw_args[k] = v.format(**filled)
        except KeyError:
          pass
    return {"name": tool, "args": raw_args}
  return None


def _substitute_response(
    response: list[dict[str, Any]],
    filled: dict[str, Any],
) -> list[dict[str, Any]]:
  """Recursively substitute {slot_name} in all string values.

  Supports ``options_from`` on chip objects: splits a filled slot
  value by ", " to generate individual chip options.  Optional
  ``event_name`` on the same object wraps each option with an event.

  Conditional parts: a response part may carry an optional ``condition``
  (declarative dict, lambda-source string, or compiled callable, same DSL as
  slot/task conditions). Parts whose condition is False are dropped and the
  ``condition`` key is stripped from the parts that survive, so one response can
  compose branched (if/else) output deterministically. See
  ``_filter_response_parts``.

  Args:
    response: List of response part dicts to substitute into.
    filled: Mapping of slot names to their filled values.

  Returns:
    A deep copy of response (condition-filtered) with all substitutions applied.
  """
  def _sub(obj):
    if isinstance(obj, str):
      # _safe_format, not str.format: same "unknown placeholder stays literal" behaviour
      # (the KeyError was swallowed to exactly that effect), but it also resolves the
      # inline fallback form `{slot|some words}`. Without it an announce leaks the raw
      # "{slot|some words}" at the caller — the very thing the fallback exists to stop.
      return _safe_format(obj, filled)
    elif isinstance(obj, dict):
      result = {k: _sub(v) for k, v in obj.items()}
      if "options_from" in result:
        source_slot = result.pop("options_from")
        event_name = result.pop("event_name", None)
        source_val = filled.get(source_slot, "")
        if isinstance(source_val, list):
          items = source_val
        else:
          items = str(source_val).split(",")
        options = []
        for v in items:
          v = str(v).strip()
          if not v:
            continue
          opt = {"text": v}
          if event_name:
            opt["event"] = {
                "name": event_name,
                "parameters": {event_name: v},
            }
          options.append(opt)
        # Cardinality is a surface capability, not a wording choice: a backend that
        # returns eight appointment slots is fine to show as eight chips and
        # impossible to read aloud. Truncating here (rather than trusting the
        # instruction) means a surface with a small `max_options` cannot be handed a
        # list it can't present, however long the producer's result happens to be.
        limit = _cap("max_options")
        if isinstance(limit, int) and limit > 0 and len(options) > limit:
          _log("options_truncated", "DEBUG", slot=source_slot,
               had=len(options), kept=limit)
          options = options[:limit]
        result["options"] = options
      return result
    elif isinstance(obj, list):
      return [_sub(v) for v in obj]
    return obj
  return _sub(copy.deepcopy(_filter_response_parts(response, filled)))


def _resolve_response(
    definition: dict[str, Any], field: str, filled: dict[str, Any],
    channel: str = "",
) -> Optional[list[dict[str, Any]]]:
  """Get response parts with channel override and variable substitution.

  Args:
    definition: Slot or task definition dict.
    field: Response field name (e.g. 'response', 'then_response').
    filled: Filled slot values for placeholder substitution.
    channel: Optional channel for channel-specific overrides.

  Returns:
    List of response part dicts, or None if no response defined.
  """
  # The base question field "response" pairs with the plural override key
  # "channel_responses"; every other field F pairs with "channel_F"
  # (channel_then_response, channel_retry_response, ...).
  channel_field = "channel_responses" if field == "response" else f"channel_{field}"
  channel_overrides = definition.get(channel_field, {})
  response = (
      channel_overrides.get(channel) if channel
      else None
  ) or definition.get(field)
  if response:
    return _substitute_response(response, filled)
  return None


def _resolve_variant_text(
    definition: dict[str, Any], field: str, filled: dict[str, Any],
    channel: str = "",
) -> Optional[str]:
  """The surface-specific WORDING of a text field, if the author gave one.

  A `<field>_variants` list holds alternative phrasings of the same message, each
  gated by a capability condition (see `flows.authoring.say`). Exactly one
  normally survives `_filter_response_parts`, and it REPLACES the plain-string
  field — unlike the framework's `*_response` keys, which append to what the model
  says. Appending a second phrasing of a question would make the caller hear it
  twice, which is the bug this distinction exists to prevent.

  Returns None when the author wrote a plain string (the overwhelmingly common
  case), leaving every existing config rendering exactly as it did before.
  """
  parts = _resolve_response(definition, f"{field}_variants", filled, channel)
  if not parts:
    return None
  spoken = [p.get("text", "") for p in parts
            if isinstance(p, dict) and p.get("type", "text") == "text"]
  spoken = [t for t in spoken if t]
  return " ".join(spoken) if spoken else None


# ── Surfaces ──────────────────────────────────────────────────────
#
# One agent definition, many delivery surfaces. A surface declares what it can DO
# — render structure, carry a link, offer eight choices — and authored content is
# projected onto it. Agents never branch on a channel name; they branch on
# capabilities, so a surface nobody has invented yet still behaves sanely.
#
# This table is the RUNTIME half and it is the one that runs. `flows/surfaces.py`
# carries the same defaults for the build-time authoring API; keep them in step.

_BREVITY_TIGHT = "tight"

_BUILTIN_SURFACES = {
    "voice": {"payloads": False, "brevity": "tight", "links": False,
              "filler": True, "keypad": True, "max_options": 3},
    "chat": {"payloads": True, "brevity": "normal", "links": True,
             "filler": False, "keypad": False, "max_options": 8},
}

# CES speaks its own channel vocabulary (ChannelProfile.channel_type), and a
# telephony integration that seeds one of these should land on voice without the
# agent ever learning the word.
_SURFACE_ALIASES = {
    "telephony": "voice", "phone": "voice", "audio": "voice",
    "google_telephony_platform": "voice", "twilio": "voice", "five9": "voice",
    "contact_center_as_a_service": "voice", "contact_center_integration": "voice",
    "text": "chat", "web": "chat", "webchat": "chat", "messenger": "chat",
    "web_ui": "chat", "api": "chat", "mobile": "chat",
}

# Degrade toward VOICE, deliberately. Voice failures are unrecoverable and chat
# failures are merely plain: guessing chat while actually on a phone call means
# spoken URLs and payloads nobody can hear, whereas guessing voice while actually
# in a chat window means short text and no cards. One is broken, the other is dull.
_DEFAULT_SURFACE = "voice"

# Per-turn handle to the resolved surface, {"name": str, "caps": dict}. Set at
# engine entry and cleared in the entry wrapper's finally — same lifecycle as
# _sm_ref, for the same reason (a leaked handle would let a later turn evaluate
# conditions against a finished turn's surface). Read by _eval_condition and the
# capability gates. Module-level init so a read before the first run can't NameError.
_surface_ref = None


def _surface_table(config):
  """Built-in surfaces overlaid with any the app declared."""
  table = {k: dict(v) for k, v in _BUILTIN_SURFACES.items()}
  aliases = dict(_SURFACE_ALIASES)
  declared = (config or {}).get("surfaces") or {}
  if isinstance(declared, dict):
    for name, caps in declared.items():
      if not isinstance(caps, dict):
        continue
      merged = dict(table.get(name) or _BUILTIN_SURFACES["chat"])
      merged.update({k: v for k, v in caps.items() if k != "aliases"})
      table[name] = merged
      for a in caps.get("aliases") or ():
        aliases[str(a).strip().lower()] = name
  return table, aliases


def _resolve_surface(sm, config, is_inactivity=False):
  """Decide which surface this turn is being delivered on.

  Nothing in CES hands the agent its modality today, so it is resolved rather than
  asserted, in descending order of how much the signal can be trusted:

    1. An explicit channel, seeded by the integration into `event_data["channel"]`
       and stickied into sm on the first turn.
    2. Observed evidence. A CES "no user activity" context only ever appears on an
       audio session, so a silence is proof of voice even when nobody configured
       anything. Latched once seen, since the evidence does not recur.
    3. The app's declared `default_surface`.
    4. Voice (see _DEFAULT_SURFACE for why that direction).

  Evidence outranks the app default on purpose: the default is somebody's guess at
  authoring time, and an observed silence is the live session disagreeing with it.
  """
  table, aliases = _surface_table(config)

  if is_inactivity and not sm.get("_surface_observed"):
    sm["_surface_observed"] = "voice"

  candidates = [
      (sm.get("channel") or "", "channel"),
      (sm.get("_surface_observed") or "", "observed"),
      ((config or {}).get("default_surface") or "", "app_default"),
  ]
  for raw, origin in candidates:
    key = str(raw).strip().lower()
    if not key:
      continue
    name = None
    for candidate in table:
      if candidate.lower() == key:
        name = candidate
        break
    if name is None and key in aliases and aliases[key] in table:
      name = aliases[key]
    if name is not None:
      return {"name": name, "caps": dict(table[name]), "origin": origin}
    # An unrecognized channel must not silently match nothing. Slot Studio sends
    # the literal channel "base", which under the old behavior matched no override
    # key and quietly rendered the wrong branch with no error anywhere.
    _log("surface_unmatched", "DEBUG", value=str(raw), origin=origin)

  name = _DEFAULT_SURFACE if _DEFAULT_SURFACE in table else next(iter(table))
  return {"name": name, "caps": dict(table[name]), "origin": "fallback"}


def _cap(key, default=None):
  """A capability of the surface this turn is being delivered on.

  Fails open to `default` when no surface has been resolved — an engine used
  offline (unit tests, the directive oracle) has no session, and a capability
  lookup must not be the thing that breaks it.
  """
  if not _surface_ref:
    return default
  caps = _surface_ref.get("caps") or {}
  return caps.get(key, default)


def _is_tight():
  """True when this surface wants the short form (i.e. it is spoken)."""
  return _cap("brevity", "normal") == _BREVITY_TIGHT


# ── Config compilation ────────────────────────────────────────────

_SAFE_EVAL_GLOBALS = {
    "__builtins__": {
        "int": int, "str": str, "len": len,
        "float": float, "bool": bool,
    },
}


_COMPARISON_OPS = {
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
}


_VALUE_OPS = ("eq", "neq", "in", "not_in")
_ALL_LEAF_OPS = frozenset(_VALUE_OPS) | frozenset(_COMPARISON_OPS)


def _eval_surface_leaf(spec, value, bare):
  """Apply the standard operator vocabulary to a surface-derived leaf value.

  Shared by the `capability` and `surface` leaves so they behave exactly like a
  `slot` leaf — {"capability": "max_options", "gte": 4} needs no new operators,
  it reuses the ones conditions already have.

  `bare` is the answer when no operator is given at all, which is the common case:
  {"capability": "payloads"} means "can this surface render structure", and
  {"surface": "voice"} means "is this voice".
  """
  if not (_ALL_LEAF_OPS & set(spec)):
    return bare
  for op_name, op_fn in _COMPARISON_OPS.items():
    if op_name in spec:
      try:
        return op_fn(int(value), spec[op_name])
      except (TypeError, ValueError):
        return False
  if spec.get("upper", False) and isinstance(value, str):
    value = value.upper()
  if "eq" in spec:
    return value == spec["eq"]
  if "neq" in spec:
    return value != spec["neq"]
  if "in" in spec:
    return value in spec["in"]
  if "not_in" in spec:
    return value not in spec["not_in"]
  raise ValueError(f"Unknown declarative condition: {spec!r}")


# When each slot was filled, and which turn it is now. Conditions are COMPILED ONCE
# and cached (`_COMPILED_CONFIGS`), so a turn-relative predicate cannot close over the
# answer — it has to read it at evaluation time. Set once per invocation, before
# anything evaluates a condition.
_TURN_CTX = {"stamps": {}, "now": 0}


def _publish_turn_ctx(sm, now):
  """Point the turn-relative predicates at this turn's stamps.

  Split out of `_stamp_fills` because the two halves are needed at opposite ends of a
  turn. WRITING a stamp has to happen after every fill stage, for the reason below.
  READING one has to be possible before the FIRST condition is evaluated — and the
  first is `_apply_option_cues`, which asks whether a slot is active before letting a
  cue fill it.

  Published only at the end, every turn-relative gate is measured against the previous
  turn's counter and comes out one turn late. The shape that reaches a caller: they are
  asked a question, they answer it, `since_turns` is still false so the slot is shut,
  the cue is dropped, and the agent asks the same question again.

  The dict is the one `_stamp_fills` mutates in place, so a stamp written later in the
  turn is still seen by anything that reads after it.
  """
  _TURN_CTX["stamps"] = sm.setdefault("_filled_turn", {})
  _TURN_CTX["now"] = now


def _stamp_fills(sm, now):
  """Record the turn each slot was first filled on, and forget the ones cleared.

  Done as one sweep after every fill stage rather than at each write site: there are
  ten ways a slot gets filled, and a stamp that some of them forget is worse than no
  stamp at all — a predicate reading it would be right for values that arrived one way
  and silently wrong for the same value arriving another.
  """
  filled = sm.get("filled") or {}
  stamps = sm.setdefault("_filled_turn", {})
  for name in filled:
    if name not in stamps:
      stamps[name] = now
  for name in [n for n in stamps if n not in filled]:
    del stamps[name]
  _publish_turn_ctx(sm, now)


def _eval_condition(spec, filled):
  """Evaluate a declarative condition dict against filled slots."""
  if "all" in spec:
    return all(_eval_condition(s, filled) for s in spec["all"])
  if "any" in spec:
    return any(_eval_condition(s, filled) for s in spec["any"])
  if "not" in spec:
    return not _eval_condition(spec["not"], filled)
  # Surface-aware leaves. These read the surface this turn is being delivered on
  # rather than a slot value, which is what lets one authored message render as a
  # card in chat and a short sentence on a phone call without the agent ever
  # naming a channel.
  # `since_turns`: filled, AND filled on an EARLIER turn than this one. The gap it
  # closes is an agent answering its own question — a branch that offers something
  # latches a slot as it speaks, so a branch reading that latch is satisfiable on the
  # very same turn, and the model can supply the answer it was meant to wait for.
  if "since_turns" in spec:
    name = spec.get("slot")
    if name not in filled:
      return False
    stamp = _TURN_CTX["stamps"].get(name)
    if stamp is None:
      return False
    return (_TURN_CTX["now"] - stamp) >= int(spec["since_turns"])
  if "capability" in spec:
    value = _cap(spec["capability"])
    return _eval_surface_leaf(spec, value, bool(value))
  if "surface" in spec:
    value = (_surface_ref or {}).get("name", "")
    return _eval_surface_leaf(spec, value, value == spec["surface"])
  slot = spec["slot"]
  upper = spec.get("upper", False)
  if "filled" in spec:
    v = bool(filled.get(slot))
    return v if spec["filled"] else not v
  for op_name, op_fn in _COMPARISON_OPS.items():
    if op_name in spec:
      v = int(filled.get(slot, spec.get("default", 0)))
      return op_fn(v, spec[op_name])
  v = filled.get(slot, "")
  if upper and isinstance(v, str):
    v = v.upper()
  if "eq" in spec:
    return v == spec["eq"]
  if "neq" in spec:
    return v != spec["neq"]
  if "in" in spec:
    return v in spec["in"]
  if "not_in" in spec:
    return v not in spec["not_in"]
  raise ValueError(f"Unknown declarative condition: {spec!r}")


def _eval_part_condition(cond, filled):
  """Evaluate an optional response-part ``condition`` against filled slots.

  Accepts the same forms as a slot/task condition: a declarative dict (via
  ``_eval_condition``), a lambda-source string, or an already-compiled callable.
  Fail-open: any error (bad lambda, unknown operator, missing slot) returns True
  so a malformed condition never silently drops the whole turn's text — a broken
  condition surfaces as an over-inclusive response, not a silent one, and the
  validator flags it at author time.
  """
  if cond is None:
    return True
  try:
    if callable(cond):
      return bool(cond(filled))
    if isinstance(cond, str):
      fn = eval(cond, _SAFE_EVAL_GLOBALS)  # pylint: disable=eval-used
      return bool(fn(filled))
    if isinstance(cond, dict):
      return bool(_eval_condition(cond, filled))
  except Exception:  # pylint: disable=broad-except
    return True
  return True


def _filter_response_parts(response, filled):
  """Drop response parts whose optional ``condition`` is False.

  Returns a NEW list: parts with a False condition are omitted, and the
  ``condition`` key is stripped from the parts that survive so it never reaches
  the client. Parts that are not dicts, or carry no ``condition``, pass through
  unchanged (by reference). Non-list input is returned as-is. This is the single
  place per-part conditional rendering happens, called from
  ``_substitute_response`` so every response field (question ``response``,
  ``then_response``, ``readback_response``, ``channel_*``, retry/error/announce
  responses) supports it uniformly.
  """
  if not isinstance(response, list):
    return response
  kept = []
  for part in response:
    if isinstance(part, dict) and "condition" in part:
      if not _eval_part_condition(part.get("condition"), filled):
        continue
      part = {k: v for k, v in part.items() if k != "condition"}
    kept.append(part)
  return kept


_COMPILED_CONFIGS = {}
_RAW_CONFIGS = {}
# Per-turn handle to the live sm, set at engine entry and cleared in the entry
# wrapper's finally. Read only by _log. Module-level init so a read before the
# first run can't NameError.
_sm_ref = None
# Per-turn map of {config_id: raw DAG config} injected via input_data["configs"],
# consulted by _engine_load_config so a Component's CHILD config resolves OFFLINE
# (no `tools` global) in tests/sim. Set at engine entry, cleared in the wrapper's
# finally (same lifecycle as _sm_ref). Module-level init so a read before the first
# run can't NameError.
_CONFIGS_THIS_TURN = {}


def _config_fingerprint(raw_config):
  """Content hash of a raw DAG config. The compiled-config cache is keyed on
  (config_id, fingerprint) so a CHANGED config can never serve a stale
  compilation from a warm worker — and so offline callers that pass different
  raw_configs under the same (or empty) config_id don't collide. Subsumes an
  explicit schema_version: any content change, including a version bump,
  invalidates the entry."""
  try:
    blob = json_lib.dumps(raw_config, sort_keys=True, default=str)
  except Exception:  # pylint: disable=broad-except
    blob = repr(raw_config)
  return hashlib.md5(blob.encode("utf-8")).hexdigest()


def _compile_formatter(fmt):
  """Resolve a format spec to a callable."""
  if fmt is None:
    return None
  if callable(fmt):
    return fmt
  if isinstance(fmt, str):
    if fmt in _BUILTIN_FORMATTERS:
      return _BUILTIN_FORMATTERS[fmt]
    return eval(fmt, _SAFE_EVAL_GLOBALS)  # pylint: disable=eval-used
  if isinstance(fmt, dict):
    fmt_type = fmt.get("type", "")
    if fmt_type == "prefix":
      text = fmt["text"]
      vals = fmt.get("values")
      return lambda v, _t=text, _v=vals: _format_prefix(v, text=_t, values=_v)
    if fmt_type == "date":
      text = fmt.get("text", "")
      return lambda v, _t=text: _format_date(v, text=_t)
    if fmt_type == "digits":
      text = fmt.get("text", "")
      return lambda v, _t=text: _format_digits(v, text=_t)
    if fmt_type == "plural":
      one, other = fmt["one"], fmt["other"]
      return lambda v, _o=one, _p=other: _format_plural(v, one=_o, other=_p)
    if fmt_type == "none_sub":
      default = fmt["default"]
      return lambda v, _d=default: _format_none_sub(v, default=_d)
    if fmt_type == "join":
      sep = fmt.get("sep", ", ")
      each = fmt.get("each", "{item}")
      return lambda v, _s=sep, _e=each: _format_join(v, sep=_s, each=_e)
    if fmt_type == "count":
      one, other = fmt["one"], fmt["other"]
      return lambda v, _o=one, _p=other: _format_count(v, one=_o, other=_p)
    if fmt_type in _BUILTIN_FORMATTERS:
      return _BUILTIN_FORMATTERS[fmt_type]
    raise ValueError(f"Unknown readback_fmt type: {fmt_type!r}")
  return None


def _compile_default(entry):
  """Compile one {value, when?} fallback's condition to a predicate."""
  if not isinstance(entry, dict):
    return {"value": entry}
  out = dict(entry)
  cond = out.get("when")
  if isinstance(cond, str):
    out["when"] = eval(cond, _SAFE_EVAL_GLOBALS)  # pylint: disable=eval-used
  elif isinstance(cond, dict):
    out["when"] = lambda filled, c=cond: _eval_condition(c, filled)
  return out


def _compile_config(config: dict[str, Any]) -> dict[str, Any]:
  """Compile string/dict conditions and formatters to callables."""
  compiled_slots = []
  for slot_def in config["slots"]:
    slot = dict(slot_def)
    cond = slot.get("condition")
    if isinstance(cond, str):
      slot["condition"] = eval(  # pylint: disable=eval-used
          cond, _SAFE_EVAL_GLOBALS,
      )
    elif isinstance(cond, dict):
      slot["condition"] = lambda filled, s=cond: _eval_condition(s, filled)
    slot["readback_fmt"] = _compile_formatter(slot.get("readback_fmt"))
    # A default's  is an ordinary condition and is compiled the same way, so a
    # fallback can read the rest of the flow (`when=eq("outage_status", "active")`).
    # Uncompiled it would reach _apply_slot_defaults as a raw dict and fail closed —
    # the default would silently never apply, which reads exactly like not authoring
    # one, so it must be compiled HERE rather than guessed at later.
    defaults = slot.get("default")
    if isinstance(defaults, list):
      slot["default"] = [_compile_default(d) for d in defaults]
    compiled_slots.append(slot)
  compiled_tasks = []
  for task_def in config["tasks"]:
    task = dict(task_def)
    cond = task.get("condition")
    if isinstance(cond, str):
      task["condition"] = eval(  # pylint: disable=eval-used
          cond, _SAFE_EVAL_GLOBALS,
      )
    elif isinstance(cond, dict):
      task["condition"] = lambda filled, s=cond: _eval_condition(s, filled)
    compiled_tasks.append(task)
  # Cancellation is modeled as a passive slot: the user fills it (via the cancel
  # setter, or the intent inject backstop), and filling it terminates the flow.
  # It is never asked, its setter is always available, and the setter is the
  # framework cancel tool. Every flow is cancelable; a router has nothing to
  # abandon, so cancelability defaults to `not router` (override per config with
  # an explicit "cancelable"). The optional `cancel` block customizes only the
  # disposition (transfer_to/outcome/say/exit_status); requires_readback confirms
  # before terminating.
  is_router = bool(config.get("router", False))
  cancel = config.get("cancel") or {}
  if config.get("cancelable", not is_router):
    compiled_slots.append({
        "name": _CANCEL_SLOT,
        "source": "user",
        "setter": _CONTROL_TOOLS[_CANCEL_SLOT],
        "passive": True,
        "requires_readback": bool(cancel.get("requires_readback", False)),
        "hint": cancel.get("hint", "cancel / abandon the current request"),
    })
  # Escalation: a second passive terminal control slot. Same model — escalatable
  # by default for flows, the framework escalate tool, disposition-only block.
  escalate = config.get(_ESCALATE_SLOT) or {}
  if config.get("escalatable", not is_router):
    compiled_slots.append({
        "name": _ESCALATE_SLOT,
        "source": "user",
        "setter": _CONTROL_TOOLS[_ESCALATE_SLOT],
        "passive": True,
        "requires_readback": bool(escalate.get("requires_readback", False)),
        "hint": escalate.get("hint", "reach a human / live agent"),
    })
  # Intent-changed: a passive slot the model FILLS (via the set_intent_changed
  # setter) when the guest wants something other than the current details. Unlike
  # cancel/escalate it does not terminate — filling it triggers the focused
  # phase-2 intent pass. Modeled as a setter (not a flag) so the model treats the
  # call as non-terminal (provide data → continue), giving us the phase-2 pass.
  if config.get("intent_change", {}).get("tool"):
    compiled_slots.append({
        "name": "intent_changed",
        "source": "user",
        "setter": config["intent_change"]["tool"],
        "passive": True,
        "requires_readback": False,
        "hint": config["intent_change"].get(
            "hint", "wants something other than the current details"),
    })
  compiled = dict(config)
  compiled["slots"] = compiled_slots
  compiled["tasks"] = compiled_tasks
  return compiled


# ═════════════════════════════════════════════════════════════════════
# SLOT STATE HELPERS
# ═════════════════════════════════════════════════════════════════════


def _is_slot_active(slot_def, filled):
  """Check if a conditional slot is active.

  Fails OPEN — an unevaluable condition leaves the slot active, because a slot wrongly
  skipped is a question never asked, which is worse than one asked needlessly. But it
  now SAYS SO. Handed a RAW config, where `condition` is still a dict rather than the
  compiled predicate, the bare `except` reported every slot active and anything
  reasoning about slot visibility from that answer was quietly meaningless — a
  tool-surface audit built on it could not fail even when the surface was wide open.
  """
  condition = slot_def.get("condition")
  if condition is None:
    return True
  if not callable(condition):
    _log("slot_condition_uncompiled", "WARN",
         slot=slot_def.get("name"), got=type(condition).__name__,
         note="config is not compiled — treating the slot as active")
    return True
  try:
    return bool(condition(filled))
  except Exception as exc:  # pylint: disable=broad-except
    _log("slot_condition_error", "WARN",
         slot=slot_def.get("name"), error=str(exc))
    return True


def _is_slot_active_ignoring_self(slot_def, name, state):
  """`_is_slot_active`, but the slot's OWN value is removed from `state` first.

  #585: a slot can gate its own asking on being empty (`condition: not f.get(<self>)`,
  the unset(self) idiom). Judged against state that already holds its value that reads as
  inactive — which is correct for ASKING (don't re-ask a filled slot) but WRONG anywhere a
  FILLED self-gated slot must still count: `_deactivate_conditional_slots` must not delete
  its value, and `_task_fireable` must still see it as an available input/requirement.
  Evaluating minus its own key neutralizes the self-gate while leaving genuine cross-slot
  conditions intact.
  """
  if name in state:
    state = {k: v for k, v in state.items() if k != name}
  return _is_slot_active(slot_def, state)


def _is_task_active(task_def, filled):
  """Check if a conditional task is active."""
  condition = task_def.get("condition")
  if condition is None:
    return True
  try:
    return bool(condition(filled))
  except Exception:  # pylint: disable=broad-except
    return True


def _resolve_formatter(fmt):
  """Resolve a compiled formatter to a callable."""
  if fmt is None:
    return None
  if callable(fmt):
    return fmt
  if isinstance(fmt, str):
    return _BUILTIN_FORMATTERS.get(fmt)
  return None


# ═════════════════════════════════════════════════════════════════════
# AFFIRMATIVE DETECTION & CONFIRMATION
# ═════════════════════════════════════════════════════════════════════
#
# During readback, the engine asks the user to confirm pending slots.
# Two confirmation paths handle user replies:
#
# AUTO-CONFIRM (_is_affirmative → _try_auto_confirm):
#   The entire message is a pure affirmative ("yes", "correct").
#   The engine preempts with a confirm_pending tool call — the LLM
#   never runs. Deterministic, fast, no risk of the LLM ignoring
#   the confirmation.
#
# INLINE-CONFIRM (_starts_affirmative → _apply_inline_confirm):
#   The message starts with an affirmative but has additional
#   content ("Yea, also my wife has a shellfish allergy"). The
#   engine silently confirms pending slots (moving them to filled),
#   defers the task fire, and lets the LLM run with collection
#   instructions so it can call setters for the new content. When
#   inline_confirmed is set the engine renders collection (not readback)
#   instructions in the SI it returns, so the LLM collects the new content.
# ═════════════════════════════════════════════════════════════════════


_AFFIRMATIVES = frozenset({
    "yes", "yeah", "yea", "yep", "yup", "yah", "ya",
    "correct", "right",
    "sure", "sounds good", "looks good",
    "ok", "okay", "perfect", "great", "exactly",
    "confirmed", "confirm",
    "absolutely", "definitely", "certainly",
    "that's right", "that is right",
    "that's correct", "that is correct",
    "looks right", "that looks right", "that sounds right",
})

_CORRECTION_SIGNALS = frozenset({
    "but", "actually", "wait", "change", "different",
    "instead", "not", "no", "wrong", "except", "however",
    "although", "though",
})

# Tokens that make a LEADING affirmative something other than consent: a stall
# ("ok hold on", "sure let me look"), a cancellation ("yes cancel that") or a
# sign-off ("ok bye"). Over voice these are short and start with a filler yes, so
# without this both confirmation paths read one as a full confirmation and commit
# every pending slot the caller never agreed to. Scanned over the WHOLE reply (a
# stall word can land anywhere in it).
_NON_CONSENT_SIGNALS = frozenset({
    "hold", "hang", "sec", "second", "seconds", "moment", "minute", "minutes",
    "let", "give", "pause", "now",
    "cancel", "stop", "nevermind", "forget", "abort", "quit",
    "bye", "goodbye",
})

_STRIP_PUNCT = str.maketrans("", "", ".,;:!?\"'")

# Unambiguous date/time words that signal the user supplied a VALUE alongside
# an affirmative ("ok Friday", "yes next Tuesday", "sure, noon"). Kept tight on
# purpose — only tokens that are almost never plain confirmation chatter. (No
# "may"/"am"/"morning" etc.: those are ambiguous and would mis-route harmless
# confirmations like "yes you may" into inline-confirm.) Times/numbers are
# caught by the digit check instead, so most values need no word list at all.
_VALUE_WORDS = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "today", "tomorrow", "tonight", "noon", "midnight", "pm",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
})

# Spelled-out cardinals: ASR renders a small spoken number as a WORD ("yes its
# four people"), which carries no digit, so without these the reply reads as a
# pure affirmative and auto-confirm preempts the LLM — silently dropping the
# value the caller just gave. "yes its 4 people" already defers correctly.
_NUMBER_WORDS = frozenset({
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty",
    "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
})


def _has_value_token(words):
  """True when any word looks like a supplied value (a digit, e.g. '7pm' /
  '20' / '7:30', a spelled-out cardinal, e.g. 'four', or an unambiguous date
  word, e.g. 'friday'). Used to keep an affirmative-that-also-supplies-a-value
  ("ok Friday") OUT of auto-confirm so the LLM runs and captures the value
  instead of it being preempted away.
  """
  return any(any(c.isdigit() for c in w)
             or w in _VALUE_WORDS or w in _NUMBER_WORDS
             for w in words)


def _is_affirmative(text: str) -> bool:
  """True when the ENTIRE message is a pure affirmative.

  Used for auto-confirm: the user said only "yes" / "correct" /
  "sure, sounds good" with no additional content. The engine can
  preempt with a confirm_pending tool call without running the LLM.

  Allows up to 5 words so "yes that looks right" and "yes, book my table"
  match, but rejects anything containing a correction signal ("but", "wait",
  "actually"), a non-consent signal (a stall/cancel/sign-off — "ok hold on",
  "yes cancel that") OR a supplied value token (a digit, a cardinal or a date
  word — "ok Friday", "yes 7pm", "yes its four people"). A value means the user
  added new info, so the message falls through to ``_starts_affirmative`` /
  inline-confirm where the LLM captures it; auto-confirm would preempt the LLM
  and silently drop the value.

  Args:
    text: The user's message text.

  Returns:
    True if the message is a pure affirmative.
  """
  if not text:
    return False
  normalized = text.lower().strip().rstrip(".,!? ")
  if normalized in _AFFIRMATIVES:
    return True
  words = [w.translate(_STRIP_PUNCT) for w in normalized.split()]
  if len(words) <= 5 and words and words[0] in _AFFIRMATIVES:
    if any(w in _CORRECTION_SIGNALS or w in _NON_CONSENT_SIGNALS
           for w in words[1:]):
      return False
    return not _has_value_token(words)
  return False


def _starts_affirmative(text: str) -> bool:
  """True when the message STARTS with an affirmative but has more content.

  Used for inline-confirm: the user confirmed AND added new info in
  the same message ("Yea, also my wife has a shellfish allergy").
  The engine silently confirms pending slots and lets the LLM run
  to process the additional content (e.g. calling a setter).

  A SHORT reply is scanned end to end for correction signals: "yes that is all
  wrong" is a retraction, not a yes with new content, and inline-confirming its
  pending values commits exactly what the caller just rejected — the first-4-word
  window missed it. In a LONGER reply the words past the opening clause really
  are new content ("ok so I need to change my flight"), so only the first 4 are
  scanned there. A non-consent signal (a stall/cancel/sign-off) disqualifies the
  reply at any length, which also keeps `_is_affirmative ⟹ _starts_affirmative`.

  Args:
    text: The user's message text.

  Returns:
    True if the message starts with an affirmative and has more content.
  """
  if not text:
    return False
  normalized = text.lower().strip().rstrip(".,!? ")
  if normalized in _AFFIRMATIVES:
    return True
  words = [w.translate(_STRIP_PUNCT) for w in normalized.split()]
  if not words or words[0] not in _AFFIRMATIVES:
    return False
  if any(w in _NON_CONSENT_SIGNALS for w in words[1:]):
    return False
  scan = words[1:] if len(words) <= 6 else words[1:4]
  return not any(w in _CORRECTION_SIGNALS for w in scan)


# ═════════════════════════════════════════════════════════════════════
# LOGGING
# ═════════════════════════════════════════════════════════════════════


_LEVEL_MAP = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
              "WARN": logging.WARNING, "ERROR": logging.ERROR}
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
_logger = logging.getLogger("slot_filling.engine")


# sm["_log"] is serialized into session state every turn, so it must not grow
# without bound. Keep only the most recent _LOG_CAP entries (ring buffer).
_LOG_CAP = 200


def _log(tag, level="INFO", **data):
  """Emit structured log entry; append to sm["_log"] (capped at _LOG_CAP)."""
  min_level = _sm_ref.get("_log_level", "INFO") if _sm_ref else "INFO"
  if _LEVEL_ORDER.get(level, 1) < _LEVEL_ORDER.get(min_level, 1):
    return
  entry = {"src": "engine", "tag": tag, "level": level,
           "data": {k: v for k, v in data.items() if v is not None}}
  _logger.log(_LEVEL_MAP.get(level, logging.INFO),
              json_lib.dumps(entry, default=str))
  if _sm_ref is not None:
    _lst = _sm_ref.setdefault("_log", [])
    _lst.append(entry)
    if len(_lst) > _LOG_CAP:
      del _lst[:-_LOG_CAP]


def _log_progress(filled, pending, last_state):
  """Log state deltas — skip if nothing changed."""
  last_filled = last_state.get("filled", {})
  last_pending = last_state.get("pending", {})
  confirmed = sorted(
      k for k in set(last_pending) if k in filled and k not in last_filled)
  task_out = {
      k: filled[k]
      for k in set(filled) - set(last_filled) - set(confirmed)}
  new_pending = {k: pending[k] for k in set(pending) - set(last_pending)}
  rejected = sorted(
      k for k in set(last_pending) if k not in pending and k not in filled)
  if not (confirmed or task_out or new_pending or rejected):
    return
  _log("progress",
       **({} if not new_pending else {"pending+": new_pending}),
       **({} if not confirmed else {"confirmed": confirmed}),
       **({} if not task_out else {"task+": task_out}),
       **({} if not rejected else {"rejected": rejected}))


def _log_invoke(n, phase, filled, pending, fresh_pending, hidden, *,
                asking=None, reading_back=None, fired=None, done=False,
                preempted=None, deferred=None):
  """Log invocation snapshot (DEBUG level)."""
  _log("invoke", level="DEBUG",
       n=n, phase=phase,
       **({} if not filled else {"filled": sorted(filled)}),
       **({} if not pending else {"pending": sorted(pending)}),
       **({} if not deferred else {"deferred": sorted(deferred)}),
       **({} if not fresh_pending else {"fresh": True}),
       **({} if not hidden else {"hidden": sorted(hidden)}),
       **({} if asking is None else {"asking": asking[:80]}),
       **({} if reading_back is None else {"rb": reading_back[:80]}),
       **({} if fired is None else {"fired": fired}),
       **({} if not done else {"done": True}),
       **({} if preempted is None else {"preempted": preempted[:80]}))


# ═════════════════════════════════════════════════════════════════════
# DAG ENGINE COMPONENTS
# ═════════════════════════════════════════════════════════════════════


def _handle_state_change(
    sm: dict[str, Any],
    filled: dict[str, Any], pending: dict[str, Any],
    last_state: dict[str, Any],
) -> bool:
  """Reset stall counters on state changes. Returns True when REAL forward progress
  happened this turn (a slot moved, not just an auto-confirm) — the caller uses this
  to skip steer-back's increment so a productive turn never counts as a stall."""
  current_state = {
      "filled": filled, "pending": pending,
      "deferred": sm.get("deferred", {}),
  }
  if current_state == last_state:
    return False

  progressed = False
  if not sm.pop("_auto_confirm_pending", False):
    sm["_steer_back_turns"] = 0
    # Progress was made (a slot moved) — disarm any pending hard-steer-back
    # re-ask so a captured value doesn't get overwritten by a stale redirect.
    sm.pop("_steer_reask", None)
    progressed = True

  retries = sm.get("_retries", {})
  last_filled = last_state.get("filled", {})
  last_pending = last_state.get("pending", {})
  for name in set(filled) - set(last_filled):
    retries.pop(f"slot:{name}", None)
  for name in set(pending) - set(last_pending):
    retries.pop(f"slot:{name}", None)
  if last_pending and not pending:
    retries.pop("readback", None)

  _log_progress(filled, pending, last_state)
  return progressed


def _auto_promote_and_route(
    slots, tasks, task_results, slot_map,
    filled: dict[str, Any], pending: dict[str, Any],
    deferred: dict[str, Any], sm: dict[str, Any],
) -> list[str]:
  """Promote non-readback pending slots and route deferred.

  Args:
    slots: List of slot definition dicts.
    tasks: List of task definition dicts.
    task_results: Dict of task name to result.
    slot_map: Dict mapping slot name to slot definition.
    filled: Currently filled slot values (mutated in place).
    pending: Currently pending slot values (mutated in place).
    deferred: Currently deferred slot values (mutated in place).
    sm: Session state machine (holds the repeated-slot accumulator `_repeat_acc`).

  Returns:
    The names of slots promoted from deferred to pending.
  """
  readback_set = {
      s["name"] for s in slots if s.get("requires_readback")
  }

  # `skip_readback_if_matches: [slot, ...]` — never read the same number back twice.
  # A slot can legitimately require a readback for the value the caller DICTATES while
  # that same readback is redundant for the value the caller merely re-affirms. The
  # canonical case is a confirmation-SMS number offered against the mobile already
  # verified during auth: the offer leads with those digits, the caller says "yes, that
  # one", and the readback then asks them to confirm — for the third time — digits they
  # have already confirmed out loud. A redirect to a NEW number still gets its readback,
  # which is the case the readback exists for.
  #
  # Keyed on the VALUE, not on how the setter obtained it. A provenance flag would have
  # to be threaded through every setter that can echo an earlier answer; value identity
  # covers all of them at once and states the actual rule — these exact digits have
  # already been confirmed in this session.
  #
  # The source slots are DECLARED, never inferred. Scanning for any other
  # `requires_readback` slot holding the same value does not work across a flow
  # boundary: the confirming slot lives in one config and the earlier-confirmed value
  # re-enters the next config as a task output, carrying no `requires_readback` of its
  # own. An implicit scan cannot see that without trusting arbitrary task outputs, which
  # is exactly what it must not do. Naming the source is what makes the skip safe.
  #
  # Comparison is digits-only (`_digits_only`), so "212-555-0199" matches "2125550199" —
  # the same value spoken, typed and returned by a backend rarely shares a format. Any
  # ONE listed slot matching is enough; a listed slot that is not FILLED cannot match, so
  # an unconfirmed source never suppresses a readback.
  for name in list(readback_set):
    if name not in pending:
      continue
    staged = _digits_only(pending[name])
    if len(staged) < _READBACK_SKIP_MIN_DIGITS:
      continue
    for src in (slot_map.get(name, {}).get("skip_readback_if_matches") or []):
      if src in filled and _digits_only(filled[src]) == staged:
        readback_set.discard(name)
        _log("readback_skipped_already_confirmed", slot=name, matched=src)
        break

  # Mode A repeated slots (§R2.2/R2.3). Consume the reserved `done_setter` keys
  # FIRST so they never reach the general promote loop and never linger in pending
  # (which would trip _compute_hidden_tools' hide-all-setters `if pending:` branch).
  done_signals = {}
  for key in [k for k in pending
              if isinstance(k, str) and k.startswith(_REPEAT_DONE_PREFIX)]:
    # Honor the staged value's truthiness: a parameterized done setter can return
    # value=False to mean "not done yet" — consume the key but do NOT signal done
    # (collection continues). Only a truthy value ends collection.
    if pending.pop(key):
      done_signals[key[len(_REPEAT_DONE_PREFIX):]] = True

  for name in [k for k in pending if k not in readback_set]:
    spec = slot_map.get(name, {}).get("repeated")
    if spec:
      # A repeated slot NEVER goes through the unconditional promote: stage the
      # staged scalar into the accumulator and write filled[name] only once the
      # collection is done (so filled stays absent-until-complete — no premature
      # dependency-fire / premature-skip). The done marker (a companion flag OR
      # max_count) is evaluated centrally by _repeat_done.
      value = pending.pop(name)
      acc_map = sm.setdefault("_repeat_acc", {})
      # Resume-restore guard: a COMPLETED repeated slot's value is a plain list in
      # `filled`; on flow resume slot_intake re-validates saved `slots` into
      # `pending` (scalar slots re-confirm), so the finalized list reappears here.
      # With no live accumulator for this slot, that list IS the finished
      # collection — restore it straight to filled instead of re-collecting it as a
      # single nested element. (A mid-collection resume restores via `_repeat_acc`,
      # so `name in acc_map` and this branch is skipped.)
      if isinstance(value, list) and name not in acc_map:
        filled[name] = value
        _log("repeat_restore_complete", slot=name, count=len(value))
        continue
      acc = acc_map.setdefault(name, [])
      acc.append(value)
      if len(acc) >= _REPEAT_RUNAWAY_CAP or _repeat_done(
          spec, acc, done_signals.get(name)):
        filled[name] = acc
        del sm["_repeat_acc"][name]
        _log("repeat_complete", slot=name, count=len(acc))
      else:
        _log("repeat_append", slot=name, count=len(acc))
      continue
    filled[name] = pending.pop(name)

  # A `done_setter` pressed WITHOUT a new value this turn (e.g. "that's all"):
  # finalize the existing accumulator if the minimum is met, else leave it
  # collecting (below min_count re-asks — §R2.3).
  for name, _sig in done_signals.items():
    if name in filled:
      continue                                 # already finalized in the loop above
    spec = slot_map.get(name, {}).get("repeated")
    if not spec:
      continue
    acc = sm.get("_repeat_acc", {}).get(name, [])
    if _repeat_done(spec, acc, True):
      filled[name] = list(acc)
      sm.get("_repeat_acc", {}).pop(name, None)
      _log("repeat_complete", slot=name, count=len(acc), via="done_setter")

  deferred_eligible = _compute_deferred_eligible(
      slots, tasks, task_results, slot_map,
  )
  for name in [k for k in pending if k in deferred_eligible]:
    deferred[name] = pending.pop(name)
  return _check_deferred_groups(
      tasks, filled, pending, deferred, task_results, slot_map,
  )


def _deactivate_conditional_slots(
    slots, filled: dict[str, Any], pending: dict[str, Any],
    deferred: dict[str, Any], retries: dict[str, Any],
    armed: Optional[set] = None,
) -> None:
  """Remove slots whose conditions are no longer met."""
  armed = armed or set()
  merged_state = {**filled, **pending, **deferred}
  for slot_def in slots:
    if "condition" not in slot_def:
      continue
    name = slot_def["name"]
    # A no_input-armed escalation OFFER flag (open_slot) latches: its condition is
    # deliberately False (never asked directly), so without this exemption it would
    # be cleared on the very next pass — collapsing the offer before the model's
    # proceed turn. Keep it until the offer resolves / the flow resets.
    if name in armed:
      continue
    # An ANNOUNCE's latch is a record that it SPOKE, not state its condition describes.
    # `_cascade_announce` writes `filled[name] = True` for one reason -- so the scan skips
    # it next time -- and clearing that is not "this slot stopped being relevant", it is
    # forgetting something the caller already heard.
    #
    # A CONDITIONAL announce reliably ends up here, because the condition that let it fire
    # is usually the thing its firing changes. Driven: an announce asking a question during
    # a diagnostics wait was gated on the results being absent; the results landed, the
    # condition went False, and the latch was erased. It did not re-speak -- the condition
    # was False now -- but every slot gated on "that announce fired" silently lost its gate,
    # and the capture it existed to open never happened.
    #
    # Re-firing is not the risk this creates, either: reaching this branch means the
    # condition is False, and that is the same test the announce scan applies. The
    # exemption preserves only the history, which is all an announce's entry ever meant.
    if "announce" in _normalize_sources(slot_def.get("source", "user")):
      continue
    # A slot may gate its own asking on being empty (the `unset(self)` idiom,
    # `condition: not f.get(<self>)`). Judged against state that already holds the
    # just-filled value, that condition flips False and the slot deactivates — and
    # deletes — its own answer, so it is re-asked every turn forever. Evaluate a slot's
    # continued activity against everything EXCEPT its own value; genuine cross-slot
    # conditions (on OTHER slots) still deactivate it.
    if not _is_slot_active_ignoring_self(slot_def, name, merged_state):
      if name in filled:
        filled.pop(name)
        retries.pop(f"slot:{name}", None)
        _log("slot_deactivated", slot=name, source="filled")
      if name in pending:
        pending.pop(name)
        _log("slot_deactivated", slot=name, source="pending")
      if name in deferred:
        deferred.pop(name)
        _log("slot_deactivated", slot=name, source="deferred")


def fill_slots(
    sm: dict[str, Any],
    config: dict[str, Any],
    values: dict[str, Any],
    skip_readback: bool = True,
) -> dict[str, list[str]]:
  """Fill slots programmatically.

  Args:
    sm: The state machine dict.
    config: The compiled DAG config from _compile_config().
    values: Dict of {slot_name: value} to fill.
    skip_readback: If True (default), write directly to
      filled (no user confirmation). If False, write to
      pending (triggers readback/confirm flow).

  Returns:
    {"filled": [names written], "skipped": [names skipped]}.
  """
  slot_map = {s["name"]: s for s in config["slots"]}
  filled = sm.setdefault("filled", {})
  result = {"filled": [], "skipped": []}
  for name, value in values.items():
    slot_def = slot_map.get(name)
    if not slot_def:
      result["skipped"].append(name)
      continue
    if name in filled:
      result["skipped"].append(name)
      continue
    if not _is_slot_active(slot_def, filled):
      result["skipped"].append(name)
      continue
    if skip_readback:
      filled[name] = value
    else:
      sm.setdefault("pending", {})[name] = value
    _log("fill_slots", slot=name, value=value)
    result["filled"].append(name)
  return result


def _try_auto_confirm(
    phase: str, last_user_text: str, sm: dict[str, Any],
) -> Optional[dict[str, Any]]:
  """Handle affirmative replies during readback confirmation.

  Two paths depending on message length:

  AUTO-CONFIRM -- pure affirmative ("yes", "correct"):
    Returns a preemptive confirm_pending tool call. The LLM
    never runs; pending slots move to filled deterministically.

  INLINE-CONFIRM -- affirmative + new content ("Yea, also my
    wife has a shellfish allergy"):
    Sets _inline_confirm flag on sm and returns None. The
    engine processes this flag later in _apply_inline_confirm:
    pending slots are silently confirmed, the task fire is
    deferred, and the LLM runs with collection instructions
    so it can call setters for the new content.

  Args:
    phase: Current engine phase.
    last_user_text: The user's last message text.
    sm: State machine dict (mutated in place).

  Returns:
    A preemptive action dict for auto-confirm, or None.
  """
  if phase != "awaiting_confirmation" or not last_user_text:
    return None
  if _is_affirmative(last_user_text):
    _log("auto_confirm", user_msg=last_user_text)
    sm["_auto_confirm_pending"] = True
    return {
        "hide_tools": [],
        "preempt": True,
        "force_preempt": True,
        "function_call": {"name": "confirm_pending", "args": {}},
        "message": "",
    }
  if _starts_affirmative(last_user_text):
    sm["_inline_confirm"] = True
    return None
  return None


def _apply_inline_confirm(
    sm: dict[str, Any],
    filled: dict[str, Any],
    pending: dict[str, Any],
    phase: str,
    fresh_pending: bool,
) -> tuple[bool, str, bool]:
  """Apply the inline-confirm flag set by _try_auto_confirm.

  When the user's message started with an affirmative but
  contained additional content (e.g. "Yea, also note the
  shellfish allergy"), _try_auto_confirm set sm["_inline_confirm"]
  = True and returned None so the engine continues normally.

  This function consumes that flag and:
  1. Moves all pending slots to filled (the confirmation).
  2. Resets phase to "collection" so the LLM gets collection
     instructions (not readback) and can call setters for the
     new content in the user's message.
  3. Returns inline_confirmed=True, which defers the task fire and
     makes the engine render collection (not readback) instructions
     in the SI for this turn.

  Args:
    sm: State machine dict (mutated in place).
    filled: Currently filled slot values (mutated in place).
    pending: Currently pending slot values (mutated in place).
    phase: Current engine phase.
    fresh_pending: Whether pending was just populated this turn.

  Returns:
    Tuple of (inline_confirmed, phase, fresh_pending).
  """
  if not sm.pop("_inline_confirm", False) or not pending:
    return False, phase, fresh_pending
  committed = list(pending.keys())
  filled.update(pending)
  pending.clear()
  sm["_readback_transition"] = True
  _log("auto_confirm_inline", committed=committed)
  return True, "collection", False


def _handle_steer_back(
    sm: dict[str, Any], last_user_text: str,
    steer_back_cfg: dict[str, Any],
    slots, filled: dict[str, Any], pending: dict[str, Any],
    deferred: dict[str, Any], slot_map: dict[str, Any],
    fresh_pending: bool, hide_tools: list[str],
    inv_n: int,
    channel: str = "", progressed: bool = False,
) -> Optional[dict[str, Any]]:
  """3-tier steer-back: soft SI → hard preempt → escalate."""
  # Clear last turn's speculative-increment marker on every entry, so it only ever
  # reflects an increment made on THIS turn (see the increment below and the #513
  # rollback in after_model's render_empty_backstop).
  sm.pop("_steer_back_speculative", None)
  if not last_user_text:
    return None

  # A caller who is holding ("hold on", "still looking") is not steering off-topic —
  # a hold is a first-class, expected pause. Don't accrue steer-back turns or emit an
  # "off-topic" directive during hold; the no_input silence ladder + its extension cap
  # and exhaust already bound it. Without this, spoken hold replies push steer-back to
  # its soft threshold, so the number the caller finally reads gets framed as "may be
  # off-topic" and the model reassures instead of capturing it.
  if sm.get("_hold_on"):
    return None

  # Waiting on an ASYNCHRONOUS backend is the same situation: "are you still there?"
  # while a lookup is outstanding is the caller doing exactly the right thing, not
  # drifting off topic. Without this the counter climbs on every such turn, the
  # off-topic directive pre-empts the wait's own deadline, and the model reassures the
  # caller instead of the flow either resolving or giving up.
  if sm.get("_awaiting_async"):
    return None

  # Forward progress this turn (a slot moved) is the opposite of a stall — never
  # count it. _handle_state_change already reset the counter to 0; returning here
  # also skips the increment below, so a productive turn leaves the counter at 0.
  # (Without this, the scanned-text fallback that lets steer-back SEE the user
  # message in Pass B would also make it increment on progress turns — a floor of
  # 1 that escalates one turn early.)
  if progressed:
    return None

  last_steer_text = sm.get("_steer_last_text")
  if last_steer_text == last_user_text:
    return None
  sm["_steer_last_text"] = last_user_text

  # Cancel intent is handled by the cancel_flow tool (LLM-driven),
  # not by keyword matching here.

  # Yield when user appears to be correcting a filled value.
  correction_tool = sm.get("_correction_tool")
  if correction_tool:
    words = set(last_user_text.lower().split())
    # Only a real correction if there is a confirmed USER value to change. A
    # bare correction word with nothing filled yet (e.g. "no a reservation" at
    # entry) must NOT emit a correction directive — pointing the model at
    # set_slot_change for a value that doesn't exist makes it emit an empty
    # turn. When nothing is correctable, fall through to normal steer-back.
    correctable = any(
        "user" in _normalize_sources(s.get("source", "user"))
        and s.get("setter") and s["name"] in filled
        for s in slots
    )
    if correctable and (words & _CORRECTION_SIGNALS):
      _log("steer_back_correction_yield", "DEBUG")
      # During readback phase (pending slots), the readback_protocol
      # already handles corrections naturally ("User corrects or adds
      # info → call the appropriate setter first"). Emitting a
      # steer_back_directive there conflicts with readback instructions
      # and confuses the LLM. Only emit the directive during collection
      # phase (no pending) where the LLM needs the extra nudge.
      if not pending:
        return {
            "steer_back_directive": (
                f"The user appears to want to correct a previously"
                f" confirmed value. Call {_tool_ref(correction_tool)}"
                f" with the appropriate slot_name and new_value BEFORE"
                f" generating any response. Do NOT ask for or try to"
                f" fill the current slot — handle the correction first."
            ),
        }
      return None
    # (No grace turn: any substantive tool call resets _steer_back_turns in
    # slot_intake — a real correction never reaches the escalate ladder, so the
    # blanket first-turn grace is unnecessary and was mis-timing escalation.)

  turns = sm.get("_steer_back_turns", 0) + 1
  sm["_steer_back_turns"] = turns
  # #513: this increment is SPECULATIVE. In intent-first, steer-back runs in before_model
  # (before the model can fire a setter) and sees the caller's value via the scanned-text
  # fallback. If the model then returns an EMPTY completion that render_empty_backstop
  # covers with a re-ask, the turn made no real progress by the model's doing — but it did
  # carry a value, so counting it as an "off-topic" strike marches the ladder on
  # data-answer turns. after_model rolls this one increment back when it fires the backstop.
  sm["_steer_back_speculative"] = True

  soft_after = steer_back_cfg.get("soft_after", 2)
  hard_after = steer_back_cfg.get("hard_after", 4)
  escalate_after = steer_back_cfg.get("escalate_after", 6)

  if turns < soft_after:
    return None

  if pending:
    target = (
        "read back the pending values and ask"
        " the guest to confirm"
    )
  else:
    next_q = _find_next_question(
        slots, filled, pending, slot_map,
        deferred=deferred, channel=channel, sm=sm,
    )
    target = next_q.get(
        "system_message",
        "ask for the next piece of information",
    )

  # During readback (pending slots awaiting confirmation), cap steer-back
  # at the soft level. The user is engaged (they reached readback) and
  # off-topic questions shouldn't escalate to hard preemption or cancel.
  # Soft steer-back is sufficient — it reminds the LLM to redirect to
  # the readback confirmation. The counter still increments so it
  # resumes at the right level if readback completes without confirming.
  in_readback = pending and not fresh_pending

  if turns >= escalate_after and not in_readback:
    _log("steer_back_escalate", "WARN", turns=turns)
    exhaust = steer_back_cfg.get("on_exhaust", {})
    fc = _resolve_exhaust_action(exhaust, filled)
    if fc:
      sm["status"] = "escalated"
    msg = exhaust.get("say", "Please call us for help.")
    _log_invoke(inv_n, "steer_back_escalate", filled, pending,
                fresh_pending, hide_tools, preempted=msg,
                deferred=deferred)
    result = {
        "hide_tools": hide_tools, "preempt": True,
        "force_preempt": True, "message": msg,
    }
    if fc:
      result["function_call"] = fc
    resp = _resolve_response(exhaust, "response", filled, channel)
    if resp:
      result["response"] = resp
    return result

  if turns >= hard_after and not in_readback:
    if pending:
      hint = _build_readback_hint(
          slots, pending, filled, True,
      )
      msg = f"Just to confirm — {hint}. Is that correct?"
    else:
      next_q = _find_next_question(
          slots, filled, {}, slot_map,
          deferred=deferred, channel=channel, sm=sm,
      )
      msg = next_q.get("system_message", "")
    # Hard tier RUNS the model (so a value in the user's message can still be
    # captured) but GUARANTEES the firm re-ask: `_steer_reask` arms after_model to
    # deterministically emit `msg` if the model fires no setter this turn. This
    # keeps the point of hard steer-back (a guaranteed firm redirect, not whatever
    # the model decides to say) while keeping the window recoverable — a captured
    # value clears the arm (via _handle_state_change) and resets the counter, so
    # only genuine non-response (the model repeatedly finding nothing to set)
    # marches to the escalate tier. A model-bypassing force_preempt here would make
    # the window unrecoverable: the model would never see the user's message, so a
    # value finally provided could never be captured.
    _log("steer_back_hard", "WARN", turns=turns, msg=msg)
    sm["_steer_reask"] = msg
    # The deterministic re-ask is emitted text-only by after_model; re-surface the
    # re-asked slot's chips so a hard steer-back does not strip them.
    if not pending:
      reask_parts = _reask_question_payload(
          slots, filled, pending, slot_map, channel, sm=sm,
      )
      if reask_parts:
        sm["_reask_question_payloads"] = reask_parts
    return {
        "steer_back_directive": (
            f"The guest has been off-track for several turns. FIRST, scan their"
            f" latest message for the information being collected and, if any is"
            f" present, call the matching setter tool IMMEDIATELY to record it"
            f" (do not steer back). ONLY if the message genuinely contains"
            f" nothing usable, steer back firmly but politely: {msg}"
        ),
    }

  directive = (
      f"The guest's last message may be off-topic. "
      f"BEFORE responding: scan the message for any information relevant "
      f"to the current collection step. If found, call the matching setter "
      f"tool IMMEDIATELY — do not steer back. "
      f"Only if the message is truly off-topic, acknowledge briefly and "
      f"return to: {target}"
  )
  _log("steer_back_soft", turns=turns, directive=directive)
  return {"steer_back_directive": directive}


def _format_ground_value(val, indent="  "):
  """Render a grounding value into readable, compact labeled lines (not a raw repr).

  Dicts render as `key: value` lines and lists as `- item` lines, recursing one
  level deeper for nested structure, so a ~2 KB MCP payload reads as an indented
  outline the model can scan rather than a Python dict dump.
  """
  if isinstance(val, dict):
    out = []
    for k, v in val.items():
      if isinstance(v, (dict, list)) and v:
        out.append(f"{indent}{k}:")
        out.append(_format_ground_value(v, indent + "  "))
      else:
        out.append(f"{indent}{k}: {v}")
    return "\n".join(out)
  if isinstance(val, list):
    out = []
    for item in val:
      if isinstance(item, (dict, list)) and item:
        out.append(f"{indent}-")
        out.append(_format_ground_value(item, indent + "  "))
      else:
        out.append(f"{indent}- {item}")
    return "\n".join(out)
  return f"{indent}{val}"


def _shape_grounds(names, filled, sm):
  """Compact, labeled block of the answer node's grounding vars.

  Each name is read from `filled` first, then the sm top-level (a whole-dict
  grounding capture is stashed there, not in a DAG slot). A name with no value
  anywhere is skipped rather than rendered empty.
  """
  lines = []
  for name in names or []:
    if name in filled:
      val = filled[name]
    elif name in sm:
      val = sm[name]
    else:
      continue
    lines.append(f"{name}:")
    lines.append(_format_ground_value(val, "  "))
  return "\n".join(lines)


def _build_answer_directive(entry, last_user_text, filled, sm):
  """Compose the grounded free-response SI for one answer node.

  The author's `instruction` is the spine; the intent `scope`, a labeled block of
  the shaped `grounds` data, the caller's verbatim question, and a single line
  naming the whitelisted `tools` (+ the use-the-compute-tool-for-math rule) are
  appended around it. The model composes the reply from this — it is never read
  aloud (guarded by _NO_RECITE in the SI builder).
  """
  parts = []
  instruction = (entry.get("instruction") or "").strip()
  if instruction:
    parts.append(instruction)
  scope = (entry.get("scope") or "").strip()
  if scope:
    parts.append(f"Scope: {scope}.")
  ground_block = _shape_grounds(entry.get("grounds") or [], filled, sm)
  if ground_block:
    parts.append(
        "Account data you may use to answer (do NOT recite it — draw on only"
        " what the caller asked about):\n" + ground_block)
  parts.append(f'The caller asked: "{(last_user_text or "").strip()}"')
  whitelist = entry.get("tools") or []
  math_note = (
      "" if entry.get("allow_math")
      else " Use the compute tool for any arithmetic rather than doing the math"
           " yourself.")
  if whitelist:
    tool_refs = ", ".join(_tool_ref(t) for t in whitelist)
    parts.append(
        f"You may call ONLY these tools to look up or compute what you need:"
        f" {tool_refs}.{math_note} Never state a number you cannot read from the"
        f" data above or get from one of these tools.")
  else:
    parts.append(
        "Never state a number you cannot read directly from the data above."
        + math_note)
  parts.append(
      "Answer in one to three short sentences, then stop. If the caller asks for"
      " an account change, acknowledge it and stop — the system makes changes,"
      " not you.")
  return "\n\n".join(parts)


def _nested_disposition_tools(obj) -> set:
  """Every tool referenced in a NESTED disposition anywhere in a config subtree.

  `_config_tool_names` collects only the top-level tools (setters, `tasks[].tool`,
  bootstrap, correction). DAG-fired dispositions reference their tool one level down,
  as `{"then": {"tool": ...}}` — a slot's `validation.on_exhaust.then`, a `push_back`
  disposition, the flow `no_input`/`steer_back` `on_exhaust.then`, a task's
  `on_failure[.on_exhaust].then`, and an `awaits.on_timeout.then`. Those are often
  commit tools (a transfer, a waiver submit), and on an answer turn — where the model
  composes freely — they must NOT be callable. Walk the subtree and collect every
  `then.tool` so the answer-turn hide list covers them too.
  """
  found: set = set()
  if isinstance(obj, dict):
    then = obj.get("then")
    if isinstance(then, dict) and isinstance(then.get("tool"), str):
      found.add(then["tool"])
    for v in obj.values():
      found |= _nested_disposition_tools(v)
  elif isinstance(obj, list):
    for v in obj:
      found |= _nested_disposition_tools(v)
  return found


# The cancel / escalate rails stay callable on an answer turn (a caller can always
# reach a human) — a human/cancel request is detected upstream and preempts before the
# answer node anyway, so exposing them here changes nothing the model can misuse.
_ANSWER_RAIL_TOOLS = frozenset({"cancel_flow", "transfer_to_human"})


def _answer_hide_tools(config, slots, whitelist):
  """Tools to hide on an answer turn — every DAG-advancing / mutating tool the
  model could otherwise call, MINUS the author's read/compute whitelist.

  Hidden: every slot setter, every task/commit executor tool, the bootstrap and
  correction tools (all of `_config_tool_names`), every NESTED disposition tool
  (`then.tool` in a validation/push_back/no_input/steer_back on_exhaust, a task
  on_failure, or an awaits on_timeout — `_nested_disposition_tools`), and the
  flow-switch / classify / readback control setters. Only the cancel/escalate rails
  stay exposed. This is the safety invariant: on the answer turn the model can call
  ONLY the whitelisted read/compute tools (plus the rails), so a commit via the
  answer node is structurally impossible.
  """
  hide = {s["setter"] for s in slots if s.get("setter")}
  hide |= _config_tool_names(config)
  hide |= _nested_disposition_tools(config)
  hide.update({
      "set_intent_changed", "classify_turn_intent", "try_again",
      "set_slot_change", "set_active_flow", "new_flow_instance", "resume_flow",
      "confirm_pending", "reject_pending",
  })
  hide -= _ANSWER_RAIL_TOOLS       # rails stay callable
  hide -= set(whitelist or ())     # the author's read/compute tools stay callable
  return sorted(hide)


def _handle_answer(
    sm: dict[str, Any], config: dict[str, Any], last_user_text: str,
    slots, filled: dict[str, Any], pending: dict[str, Any],
    deferred: dict[str, Any], *, progressed: bool = False,
    channel: str = "", inv_n: int = 0,
) -> Optional[dict[str, Any]]:
  """Grounded, non-advancing free-response ("answer") turn.

  When the caller — inside an intent — asks something no cue matched (the turn
  that would otherwise go to steer-back), hand the model a grounded directive on
  which it may call ONLY a whitelisted set of read/compute tools, and leave the
  DAG state unchanged so the very next cue-match still routes to the rails. This
  mirrors the `_hold_on` / `_awaiting_async` steer-back yields: an engaged, on-
  intent question is engagement, not drift. Returns a `final`-shaped result dict
  (preempt False, empty message, an `answer_directive` the SI builder renders)
  when eligible, else None (fall through to steer-back).

  Eligibility — ALL must hold:
    * non-empty user text;
    * NO forward progress this turn (`progressed`) — a cue-match/fill IS
      progress, so those route to the DAG, preserving cue precedence;
    * not mid-hold, not awaiting an async backend, and no readback pending;
    * a non-empty `answer` policy list with a FIRST entry whose `condition` holds
      (a missing/empty condition defaults to true, as announces do) and all of
      whose `requires` are filled;
    * that entry's per-node budget (`_answer_turns_<name>`) below its `max_turns`.

  Args:
    sm: Slot machine state (mutated: per-node turn counter, _steer_back_turns).
    config: Compiled DAG config; read for the `answer` policy + tool universe.
    last_user_text: The caller's message this turn.
    slots: The flow's slot defs (for the setter hide list).
    filled: Filled slot values (condition/requires/grounds source).
    pending: Pending (readback) slot values.
    deferred: Deferred slot values (logging only).
    progressed: Whether a slot moved this turn.
    channel: Delivery channel (unused today; kept parallel to _handle_steer_back).
    inv_n: Invocation counter for structured logging.

  Returns:
    A result dict, or None when no answer node is eligible.
  """
  policy = config.get("answer")
  if not policy or not isinstance(policy, list):
    return None
  if not last_user_text:
    return None
  # Forward progress (a slot moved / a cue matched) is the rails' turn, not a
  # free question — mirror steer-back's `progressed` early-return so cue
  # precedence is preserved.
  if progressed:
    return None
  # A hold, an outstanding async wait, or a pending readback each own the turn;
  # an off-menu question during one is that machinery's job, not this node's.
  if sm.get("_hold_on") or sm.get("_awaiting_async") or pending:
    return None

  entry = None
  for cand in policy:
    cond = cand.get("condition") or {}
    # A missing/empty condition defaults to true (as an announce's does).
    if cond and not _eval_condition(cond, filled):
      continue
    if not all(r in filled for r in (cand.get("requires") or [])):
      continue
    entry = cand
    break
  if entry is None:
    return None

  name = entry["name"]
  turn_key = f"_answer_turns_{name}"
  if sm.get(turn_key, 0) >= entry.get("max_turns", 0):
    return None

  sm[turn_key] = sm.get(turn_key, 0) + 1
  # An engaged, on-intent question is engagement, not drift: keep the steer-back
  # ladder from marching while the caller is being answered — bounding the Q&A is
  # `max_turns`'s job, not the steer ladder's (mirrors the _hold_on yield, which
  # also leaves _steer_back_turns untouched by returning before the increment).
  sm["_steer_back_turns"] = 0

  whitelist = list(entry.get("tools") or [])
  directive = _build_answer_directive(entry, last_user_text, filled, sm)
  hide_tools = _answer_hide_tools(config, slots, whitelist)
  _log("answer_turn", node=name, turn=sm[turn_key],
       max=entry.get("max_turns"), tools=whitelist or None)
  _log_invoke(inv_n, "answer", filled, pending, False, hide_tools,
              deferred=deferred)
  return {
      "hide_tools": hide_tools,
      "preempt": False,
      "message": "",
      "answer_directive": directive,
  }


def _next_collectible_hard_value(slots, filled, pending) -> bool:
  """True if the slot the DAG would collect NEXT is a free-VALUE user slot.

  A free-value slot (a date, a number, a name) has a setter, no `option_cues`, is not an
  intent classification, and is sourced from the user. A caller turn that does not fill such
  a slot is a validation MISS, not an off-menu question — so the grounded answer node YIELDS
  to that slot's own validation ladder (the answer check runs before the no-match/error
  handlers, so without this guard it would hijack date/number collection). Offers, yes/no
  confirmations, and the closing question (intent / cue slots) are talk-past-able, so the
  answer node intercepts those. Passive slots never hold a turn and are skipped.
  """
  for sd in slots:
    name = sd.get("name")
    if not name or name in filled or name in pending or sd.get("passive"):
      continue
    if "user" not in _normalize_sources(sd.get("source", "user")):
      continue
    if not sd.get("setter"):
      continue
    cond = sd.get("condition")
    try:
      if callable(cond):
        if not cond(filled):
          continue
      elif cond and not _eval_condition(cond, filled):
        continue
    except Exception:  # a malformed/uncompiled condition never blocks the guard
      pass
    if not all(r in filled for r in (sd.get("requires") or [])):
      continue
    # The first slot the DAG would collect: a hard value only if it carries no cue
    # classification (an intent/offer slot has option_cues or kind == "intent").
    return not sd.get("option_cues") and sd.get("kind") != "intent"
  return False


def _terminate(
    sm, config, filled, pending, deferred, task_results, *,
    transfer_to="", outcome=None, exit_config=None,
):
  """Tear the active flow down to a zombie — the single termination primitive.

  Shared by normal completion (terminal task) and cancellation. Frees every
  collected slot and leaves `sm["_zombie"]` holding the exit disposition +
  shared values, to be reaped by the parent on re-entry (the OS process model:
  exit and kill both zombify, differing only in the exit disposition).

  Args:
    sm: Session state machine dict; mutated in place (status -> "zombie", all
      collected slots cleared).
    config: The (compiled) DAG config; supplies gate_slot and default
      exit_status mapping.
    filled: The flow's filled slots (read for flow/exit/shared, then cleared).
    pending: Pending slots (cleared).
    deferred: Deferred slots (cleared).
    task_results: Task results (cleared).
    transfer_to: Agent to return control to (or a slot name holding it).
    outcome: Exit disposition; when set, written to the zombie and to
      exit_status["flow_outcome"] (None for a normal completion).
    exit_config: {state_key: slot_name} mapping for exit vars; defaults to the
      config's top-level exit_status.

  Returns:
    The constructed zombie dict.
  """
  if exit_config is None:
    exit_config = config.get("exit_status", {})
  exit_status = {k: filled.get(v, v) for k, v in exit_config.items()}
  if outcome is not None:
    exit_status["flow_outcome"] = outcome
  zombie = {"flow": filled.get(config.get("gate_slot", ""), "")}
  if outcome is not None:
    zombie["outcome"] = outcome
  if exit_status:
    zombie["exit_status"] = exit_status
  if transfer_to:
    zombie["transfer_to"] = filled.get(transfer_to, transfer_to)
  shared_slot_names = set(sm.get("_shared_slots", []))
  shared_values = {
      k: v for k, v in filled.items() if k in shared_slot_names
  }
  if shared_values:
    zombie["shared_values"] = shared_values
  # Throw away the ENTIRE flow scope (not just the 4 slot maps as before — that
  # left _retries / _steer_back_turns / _intent_pass / the setter-map lookups /
  # etc. stale into the next flow). Shared values are preserved in the zombie above.
  _flow_clear(sm)
  # A genuine flow termination (cancel/escalate/completion) is conversation-wide:
  # tear down any Component call stack too (_flow_clear keeps it via the keep-set).
  # Component RETURN/ABANDON never reach here (they use _frame_return/_frame_abandon),
  # so this only fires for real terminations — e.g. a user escalate inside a child
  # (F3), which must end the whole conversation, not just the sub-flow. pop (not
  # assign) so a non-component session never gains the key.
  sm.pop("_call_stack", None)
  sm["_zombie"] = zombie
  sm["status"] = "zombie"
  # If another flow is still paused, flag it so the parent (Host) proactively
  # offers to resume it on re-entry. The resume itself is deterministic (the
  # before_model resume backstop + completion injector, which also run at the
  # Host — itself a framework/router agent).
  if sm.get("_flow_state"):
    sm["_auto_resume_deferred"] = True
  return zombie


def _terminate_control(sm, config, block, filled, pending, deferred,
                       task_results, allow_menu_return=True):
  """Tear the flow down via a terminal control block (cancel/escalate) and
  return the preempt action carrying that block's disposition message.

  Args:
    sm: Session state machine dict (mutated to a zombie).
    config: The compiled DAG config (supplies the control block).
    block: The control block / slot name ("cancel" or "escalate").
    filled, pending, deferred, task_results: flow state (cleared by _terminate).
    allow_menu_return: Whether a `cancel` block configured with
      `end_conversation: False` may return the caller to the menu instead of tearing
      the flow down. False on the deferred fail_flow path, where the abort has already
      happened and the only correct outcome is a teardown.

  Returns:
    A preemptive action dict carrying the disposition message, or None when a
    menu-returning cancel has handled it and the caller should fall through to the
    flow's next open question.
  """
  # Cancel inside a Component is FRAME-SCOPED (§6.8): abandon the frame and return
  # to the parent, governed by on_abort — NOT a conversation cancel. We must not
  # _terminate here: `config` and the filled/pending/... locals are the CHILD pass's,
  # so a zombie built from them would carry the child's flow id / exit_status. End
  # the pass; on 'fail_flow' _frame_abandon arms _fail_parent_flow, consumed next
  # pass under the PARENT config (the deferred consumer in _run_slot_filling).
  # Escalate is NOT frame-scoped (block != "cancel"): it falls through to _terminate
  # and stays conversation-wide (F3, hazard H-ESC).
  if block == "cancel" and sm.get("_call_stack"):
    _frame_abandon(sm, sm["_call_stack"][-1])
    return dict(_DESCENT_END_PASS)
  ctrl = config.get(block) or {}
  # ── Menu-returning cancel (`end_conversation: False`) ────────────────────────
  # Backing out of one journey is not the same as ending the call. Without this the only
  # frame-less cancel is a teardown: a caller who says "never mind" to a sub-question
  # gets `end_session` and the line goes dead, when what they meant was "not this, the
  # other thing". Cancel un-decides the named slots (`clear_slots`, normally the intent
  # slot that chose the journey) and their dependents, then falls through so the flow
  # asks its next open question — which, with the intent slot cleared, is the menu.
  # Only honoured for `cancel`: escalate is conversation-wide by design.
  if block == "cancel" and allow_menu_return and ctrl.get("end_conversation") is False:
    # The cancel slot itself must be un-filled, or the next pass sees it still set and
    # cancels again — the caller would be returned to the menu forever.
    filled.pop(block, None)
    pending.pop(block, None)
    sm.pop(f"_{block}_confirm_pending", None)
    seeds = [n for n in (ctrl.get("clear_slots") or []) if n]
    cleared = _abandon_journey(sm, config, set(seeds)) if seeds else []
    _log("cancel_returned_to_menu", cleared=cleared)
    say = ctrl.get("say")
    if say:
      # Spoken ahead of the question the fall-through is about to ask, so the whole turn
      # is "No problem. What are you calling about today?" rather than a bare
      # acknowledgement that costs the caller another turn to get anywhere.
      sm["_lead_in"] = say
    return None
  # Authored disposition parts, resolved BEFORE _terminate throws the flow scope away
  # (their `{slot}` placeholders read `filled`, which the teardown clears).
  #
  # This is how a TELEPHONY HAND-OFF reaches the contact-center platform from the
  # generic escalate rail. The rail used to emit `say` plus a bare end_session, which
  # on a platform that routes on a vendor payload means the caller is told a person is
  # coming and is then disconnected with nothing routing them anywhere — the disposition
  # looked correct in every log and dropped every caller. `flows.handoff` authors the
  # payload + end pair onto `escalate.response`; absent the key, nothing changes.
  authored = _resolve_response(ctrl, "response", filled)
  zombie = _terminate(
      sm, config, filled, pending, deferred, task_results,
      transfer_to=ctrl.get("transfer_to", ""),
      outcome=ctrl.get("outcome", block),
      exit_config=ctrl.get("exit_status", {}),
  )
  _log(f"{block}_terminated", flow=zombie["flow"],
       transfer_to=zombie.get("transfer_to", ""))
  # Neutral fallback when the config supplies no `say` (no dash — dashes chop TTS).
  # Apps set config[block]["say"] to customize the cancel/escalate disposition line.
  result = {
      "hide_tools": [],
      "preempt": True,
      "force_preempt": True,
      "message": (
          ctrl.get("say")
          or "No problem. Let me know if you need anything else."
      ),
  }
  # A single-agent app has no downstream agent to hand to, so the disposition must
  # itself END the session (escalate -> escalated end routes to a human; cancel ->
  # normal end). Multi-agent (transfer_to set) delivers Part.from_agent_transfer via
  # the zombie exit instead, so no end_session here.
  if not ctrl.get("transfer_to"):
    # App-set `reason` wins (e.g. escalate(reason="escalate") for a downstream contract);
    # absent, keep the historical default (transfer for escalate, cancelled for cancel).
    end = [{
        "type": "end_session",
        "reason": ctrl.get("reason")
        or ("transfer" if block == _ESCALATE_SLOT else "cancelled"),
        "escalated": block == _ESCALATE_SLOT,
    }]
    # Authored parts win, but the end is APPENDED when they do not carry one. A
    # hand-off payload with nothing giving up the leg is the worst of both outcomes:
    # the platform escalates and the agent keeps the call. The validator rejects that
    # shape, and this makes it unreachable even from a config that never saw it.
    if authored:
      has_end = any(
          isinstance(p, dict) and p.get("type") in ("end_session", "transfer")
          for p in authored)
      result["response"] = authored if has_end else authored + end
    else:
      result["response"] = end
  elif authored:
    result["response"] = authored
  return result


def _escalate_disposition(sm, config, block, filled, pending, deferred,
                          task_results):
  """Terminate via a control block, optionally AFTER an escalate task chain.

  `transfer_to_human` is a marker: it records the request and nothing else. An app
  that builds a hand-off summary does it in its own DAG, which this rail used to
  short-circuit — so the receiving human got a cold transfer. `escalate.tasks` names
  the chain to run first; this arms it and ends the pass, leaving the disposition to
  the turn handler once the chain is spent.

  Returns:
    The terminate action, or None when a chain is armed / already in flight.
  """
  ctrl = config.get(block) or {}
  # Route the disposition into an INTERACTIVE, in-DAG, returnable deflection sub-flow
  # (escalate.component) instead of terminating: descend into the child DAG the same
  # way an on_exhaust.component or a component-task fire does. This is the one place
  # every human-request path funnels through (injected/keyword/direct/readback), so
  # the deflection fires no matter HOW the request was detected — and the child owns
  # the disposition (deflect-and-return, hand off, or hang up).
  #
  # CONSUME the escalate detection first: pop it out of the LIVE scope so the copy
  # _frame_push stashes carries no escalate=True — otherwise the restored parent scope
  # on the child's return would re-enter _handle_terminal_slots and re-fire forever.
  # Popping also makes the deflection RE-ENTRANT: a later, separate ask re-detects and
  # re-descends. The child (deflection flow) should set escalatable=False so a repeat
  # ask WITHIN it is not itself re-preempted.
  if block == _ESCALATE_SLOT and ctrl.get("component"):
    filled.pop(_ESCALATE_SLOT, None)
    pending.pop(_ESCALATE_SLOT, None)
    sm.pop(f"_{_ESCALATE_SLOT}_confirm_pending", None)
    synth = {
        "name": "_escalate_component",
        "component": ctrl["component"],
        "inputs": ctrl.get("inputs") or {},
        "outputs": ctrl.get("outputs") or {},
        "on_abort": ctrl.get("on_abort") or "skip",
        # On on_abort="fail_flow" the parent must tear down as ESCALATED, not the
        # default "cancelled" — the caller did ask for a human. Carried on the frame
        # and read by the deferred fail-flow consumer.
        "fail_block": _ESCALATE_SLOT,
    }
    _log("escalate_component", child=ctrl["component"])
    return _component_fire_action(sm, config, synth, filled)
  # Delegation is the path `cancel` and every chain-less agent takes; keep it exact.
  if block != _ESCALATE_SLOT or not ctrl.get("tasks"):
    return _terminate_control(sm, config, block, filled, pending, deferred,
                              task_results)
  if "_escalate_path" not in sm:
    sm["_escalate_path"] = list(ctrl["tasks"])
    sm["_escalate_ticks"] = 0
    _log("escalate_path_arm", tasks=list(ctrl["tasks"]))
  return None


def _escalate_path_turn(sm, config, filled, pending, deferred, task_results,
                        slot_map, channel, inv_n):
  """Run one turn of an armed escalate chain. None means the chain is spent.

  The walk is SCOPED to the chain's own members. Not terminating is not enough on
  its own: with the account prefilled an app's spine is typically eligible the
  moment escalate lands, and declaration order hands it the fire. Passing no slots
  and no pending puts announce, readback and the spine question out of reach too,
  so the only thing this walk can produce is a chain member.
  """
  tasks = config["tasks"]
  retries = sm.setdefault("_retries", {})
  hide_tools = _compute_hidden_tools(
      config["slots"], filled, pending, ["confirm_pending", "reject_pending"],
      slot_map, executor_tools=[t["tool"] for t in tasks if t.get("tool")],
      deferred=deferred, correction_tool=config.get("correction_tool"),
  )
  # The chain's members are ordinary tasks, so their results, then_say and
  # on_failure ladders run through the ordinary handler.
  result, task_msg, _task_resp = _handle_post_executor(
      sm, tasks, task_results, filled, pending, deferred,
      retries, "", inv_n, "collection", False, hide_tools,
      channel=channel, config=config,
  )
  if result:
    return result

  # The last member's `then_say` is produced HERE, but is only spoken by the fire branch
  # below. On the turn the chain is spent there is no fire, so without stashing it the
  # closing line of the final hand-off task is silently dropped and the caller hears only
  # the escalate disposition. Carried to the disposition by the turn handler.
  if task_msg:
    sm["_escalate_pending_msg"] = task_msg

  sm["_escalate_ticks"] = sm.get("_escalate_ticks", 0) + 1
  if sm["_escalate_ticks"] > _ESCALATE_PATH_MAX_TICKS:
    _log("escalate_path_capped", "WARN", ticks=sm["_escalate_ticks"])
    return None

  by_name = {t["name"]: t for t in tasks}
  subset = [by_name[n] for n in sm["_escalate_path"] if n in by_name]
  dag_result = _compute_dag_state(
      subset, [], filled, {}, task_results, slot_map, deferred={},
      channel=channel, config=config, sm=sm)
  if dag_result["action"] != "fire":
    return None

  task_def = dag_result["task_def"]
  task_name = task_def["name"]
  if "component" in task_def:
    # A descent would swap the config out from under an armed chain, and the
    # validator already rejects this — belt and braces for a hand-written config.
    _log("escalate_path_component", "ERROR", task=task_name)
    return None

  tool_name = task_def["tool"]
  args = _remote_wire_args(
      config, tool_name, _task_input_args(task_def["inputs"], filled))
  message = task_msg
  # Surface-gated, same reasoning as the ordinary task-fire path: a spoken filler
  # masks dead air on a call and is an empty extra bubble in a chat window.
  filler_say = (_pick_filler(sm, task_def.get("filler_say"), filled)
                if _cap("filler", True) else None)
  _log_suppressed_filler(task_def, task_name)
  if filler_say:
    message = f"{message} {filler_say}".strip() if message else filler_say
  _log_invoke(inv_n, "escalate_path", filled, pending, False, hide_tools,
              fired=task_name, deferred=deferred)
  return {
      "hide_tools": [t for t in hide_tools if t != tool_name],
      "preempt": True,
      "force_preempt": False,
      "function_call": {"name": tool_name, "args": args},
      "message": message,
  }


def _declined_rung(lines, count):
  """One line out of a refusal LADDER, indexed by how many refusals there have been.

  A ladder because the same sentence twice reads as the agent not listening — which is
  the specific thing that makes a caller push harder. It CLAMPS to the last line rather
  than draining to silence, which is where this differs from `while_waiting`: a hold
  going quiet is fine, because the caller is waiting rather than asking, but a refusal
  answers a direct question and hearing nothing back is the worst available reply.
  """
  if isinstance(lines, list):
    return lines[min(count, len(lines)) - 1] if lines else ""
  return lines or ""


def _declined_line(declined, filled, count):
  """The line a caller hears when a control request is refused.

  A flow may refuse for MORE THAN ONE REASON, and the caller has to hear the right
  one. The ladder above cannot express that: it is indexed by how many times the
  request has been refused, not by what refused it, so an author with two reasons
  had to pick one sentence that covered both — vague by construction, on the one
  turn where the caller has asked a direct question and deserves a direct answer.

  So `declined_say` takes three shapes, and the third is why this is a function:

    "a line"                      one reason, one answer.
    ["first", "second"]           one reason, a line per refusal (the ladder).
    [{"when": <cond>, "say": …},  REASONS. Evaluated in order against the filled
     …, {"say": …}]               state; the FIRST match supplies the line, and its
                                  `say` may itself be a ladder. An entry with no
                                  `when` (or a bare string) always matches, so it is
                                  the catch-all and anything after it is unreachable
                                  — which the validator rejects.

  A list is read as reasons only when it actually carries one, so every existing
  ladder is untouched.

  A `when` that cannot be evaluated is SKIPPED rather than matched: unlike the
  block's own `condition` — which fails open, because a broken gate must not swallow
  a request for a human — this one only chooses wording, and the honest fallback is
  the next reason down rather than an explanation that may not apply.
  """
  if not (isinstance(declined, list)
          and any(isinstance(x, dict) for x in declined)):
    return _declined_rung(declined, count)
  for entry in declined:
    if not isinstance(entry, dict):
      return _declined_rung(entry, count)
    when = entry.get("when")
    if when is not None:
      try:
        if not _eval_condition(when, filled):
          continue
      except (ValueError, KeyError, TypeError) as e:
        _log("declined_say_when_error", "WARN", error=str(e)[:120])
        continue
    return _declined_rung(entry.get("say"), count)
  # Every reason was conditional and none of them held. Saying nothing is the only
  # honest option left — an unrelated explanation would be worse — but it is a hole in
  # the authoring, so it is logged rather than passed over.
  _log("declined_say_no_reason_matched", "WARN")
  return ""


def _handle_terminal_slots(
    sm, config, filled, pending, deferred, task_results, last_user_text,
):
  """Drive the passive terminal control slots (cancel, escalate) — termination,
  optionally confirmed. Runs at the very top of the turn so it takes priority
  over collection, readback, and auto-confirm. Each control block has its own
  disposition and an independent confirm flag (`_{block}_confirm_pending`).
  Per block, by mode:

    requires_readback = False -> filling the slot terminates immediately.
    requires_readback = True  -> the first fill asks "shall I go ahead?" and
      holds; an affirmative next turn terminates, anything else aborts and
      resumes the flow (destructive actions keep the flow on an unclear reply).

  Returns:
    A preempt action dict (terminate or confirm), or None to fall through to
    normal processing (no control slot requested, or the action was aborted).
  """
  # A control block is ACTIVE when the compiler synthesized its passive slot
  # (cancelable / escalatable). The disposition block (config[block]) is
  # optional and never carries a "tool" key — the setter lives on the synthesized
  # slot and in _CONTROL_TOOLS — so gating on config[block]["tool"] would skip
  # every control block (escalate never terminating, cancel never confirming) and
  # let the slot auto-promote into the flow as ordinary data.
  control_slots = {
      s["name"] for s in config["slots"]
      if s["name"] in _CONTROL_BLOCKS and s.get("setter")
  }
  for block in _CONTROL_BLOCKS:
    if block not in control_slots:
      continue
    ctrl = config.get(block) or {}
    flag = f"_{block}_confirm_pending"
    requires_readback = bool(ctrl.get("requires_readback", False))

    # A control block can be conditionally UNAVAILABLE. Some dispositions are only
    # valid in some states: an internet-repair flow that has just found an area
    # outage must not offer a live agent, because no amount of troubleshooting
    # brings the service back and the queue would fill with callers nobody can
    # help. Without this the request is always honoured, and the only lever an
    # author had was wording — which cannot decline anything.
    #
    # Declining DROPS the request rather than deferring it: the slot is cleared so
    # the caller can ask again later (when the condition may well have changed),
    # the flow carries on, and `declined_say` explains why. With no `declined_say`
    # the block simply never fires, which is the right default for a disposition
    # that should be invisible rather than refused out loud.
    if (block in pending or block in filled) and "condition" in ctrl:
      try:
        _allowed = _eval_condition(ctrl["condition"], filled)
      except (ValueError, KeyError, TypeError) as e:
        # A malformed condition must not silently swallow a request for a human.
        _log(f"{block}_condition_error", "WARN", error=str(e)[:120])
        _allowed = True
      if not _allowed:
        pending.pop(block, None)
        filled.pop(block, None)
        sm.pop(flag, None)
        # Remember that it happened. Dropping the request without recording it means the
        # condition reads IDENTICALLY on the next ask, so the commonest reason to decline
        # — "contain the first request, honour the second" — cannot be written at all: a
        # gate on anything the flow does not otherwise change deflects forever, and the
        # caller never reaches a human. Counting the declines gives the condition
        # something that moves: `{"slot": "escalate_declined", "gte": 1}`.
        _dkey = f"{block}_declined"
        filled[_dkey] = int(filled.get(_dkey) or 0) + 1
        _log(f"{block}_declined", reason="condition", count=filled[_dkey])
        # A line, a ladder indexed by the refusal count, or a list of REASONS keyed on
        # conditions — see `_declined_line`. The third shape exists because a flow can
        # refuse for more than one reason and the caller must hear the right one.
        _declined = _declined_line(ctrl.get("declined_say"), filled, filled[_dkey])
        if _declined:
          return {"hide_tools": [], "preempt": True, "force_preempt": True,
                  "message": _safe_format(_declined, filled),
                  "speech_class": "control",
                  "verbatim": bool(ctrl.get("verbatim"))}
        continue

    # Already confirmed (moved to filled) -> terminate.
    if block in filled:
      return _escalate_disposition(sm, config, block, filled, pending, deferred,
                                   task_results)

    if block not in pending:
      sm.pop(flag, None)
      continue

    # Requested. Immediate mode: promote and terminate now.
    if not requires_readback:
      filled[block] = pending.pop(block)
      return _escalate_disposition(sm, config, block, filled, pending, deferred,
                                   task_results)

    # Confirm mode.
    if sm.get(flag):
      sm.pop(flag, None)
      if _is_affirmative(last_user_text):
        filled[block] = pending.pop(block)
        return _escalate_disposition(sm, config, block, filled, pending, deferred,
                                     task_results)
      # Not a clear yes -> abort, resume the flow.
      pending.pop(block, None)
      _log(f"{block}_aborted", reply=last_user_text)
      continue

    # First time we see the request -> ask for confirmation.
    sm[flag] = True
    _log(f"{block}_confirm")
    return {
        "hide_tools": [],
        "preempt": True,
        "force_preempt": True,
        "message": (
            ctrl.get("confirm_say")
            or "Just to confirm — shall I go ahead?"
        ),
        "speech_class": "control",
        "verbatim": bool(ctrl.get("verbatim")),
    }
  return None


_ASYNC_PENDING = "pending"


_SETTLE_GUARD = "settle_guard"


def _dispatch_defers(task_def, legs) -> bool:
  """Does this dispatch launch anything DEFERRED, and so need the turn held open?

  Two ways a call is deferred. A task declares `awaits`, which is the author saying its
  tool is `executionType: ASYNCHRONOUS`. Or a parallel group was lowered progressively,
  which makes every leg asynchronous without the author ever saying so — the case most
  likely to be missed, and the one 129 measured at 2 of 18 nested writes surviving.

  A purely synchronous dispatch needs no guard: a synchronous execution reports inside
  the turn and was never once observed to lose a write (126).
  """
  if (task_def or {}).get("awaits"):
    return True
  if any(leg.get("awaits") for leg in (legs or [])):
    return True
  # A PROGRESSIVE group lowers its legs to asynchronous wrappers, and the author never
  # writes `awaits` for them. `parallel_batch` is the reliable inverse: `parallel()` sets
  # it only on the legs of a group that opted OUT of progressive narration and so stays
  # synchronous. Legs present and unmarked therefore means progressive, means deferred.
  # `len(legs) > 1` is what the dispatcher itself uses to mean "a real group": a lone
  # task arrives here as a one-element list, so `bool(legs)` would call every ordinary
  # synchronous dispatch deferred and guard the whole world.
  return len(legs or []) > 1 and not any(leg.get("parallel_batch") for leg in legs)


def _is_async_pending(result: Any) -> bool:
  """Is this the placeholder CES substitutes for an ASYNCHRONOUS tool's return?

  Measured (ces-probes/probes/24-async-execution): a python tool's call returns literally
  `{"result": "pending"}` and the tool body has not run yet. `after_tool` unwraps a
  top-level `result` key before intake sees it, so by here it may be either shape.

  An `agentTool` — an agent called as a tool, by resource name — keys its whole wire
  contract on `response` instead, placeholder included: `{"response": "pending"}`
  (ces-probes/probes/134-agent-tool). Reading only `result` made that a SUCCESS: the slot
  filled with the literal string "pending", `awaits` never engaged, and the real answer
  arrived a turn later with nowhere to go. Which is precisely the silent success `awaits`
  exists to prevent, on the only agent-as-a-tool flavour that can defer at all — an async
  `remoteAgentTool` is dropped at deploy (`133`).
  """
  if isinstance(result, str):
    return result.strip().lower() == _ASYNC_PENDING
  if isinstance(result, dict):
    # `response` is the agentTool spelling of `result`; a tool answers in one or the
    # other, never both, so accepting either cannot mask a real value.
    for key in ("result", "response"):
      inner = result.get(key)
      if isinstance(inner, str) and inner.strip().lower() == _ASYNC_PENDING:
        return True
  return False


def _async_hold(task_def: dict[str, Any]) -> dict[str, Any]:
  """The turn that starts (or continues) waiting on an ASYNCHRONOUS tool.

  An empty `say` yields a SILENT tick — the same shape an empty `no_input` reprompt
  produces: `before_model` returns an empty LlmResponse, so the caller hears nothing and
  the model is suppressed rather than improvising while the backend works.
  """
  say = (task_def.get("awaits") or {}).get("say") or ""
  awaits_cfg = task_def.get("awaits") or {}
  hold: dict[str, Any] = {"hide_tools": [], "preempt": True, "message": say,
                          "speech_class": "await",
                          "verbatim": bool(awaits_cfg.get("verbatim"))}
  if not say:
    hold["silent"] = True
  return hold


def _async_idle_line(
    sm: dict[str, Any], tasks: list[dict[str, Any]], filled: dict[str, Any],
) -> str:
  """The next `awaits.while_waiting` line, on a turn the wait has nothing else to fill.

  `awaits.say` covers the moment the wait STARTS; this covers the turns after it. A
  silent hold is right for a gap of a second or two and wrong for thirty — the caller
  hears nothing, assumes the line dropped, and hangs up. The ladder is drained rather
  than cycled: reassurance that repeats forever reads as a loop, so once the lines are
  spent the hold goes back to silence and `max_turns` remains the thing that ends it.

  Only consulted when the turn would otherwise be dead air (the idle hold). A wait that
  is busy asking an unrelated question does not need reassuring, and interleaving the
  two would talk over the question.
  """
  waiting = sm.get("_awaiting_async") or {}
  if not waiting:
    return ""
  by_name = {t["name"]: t for t in tasks}
  for task_name, mark in waiting.items():
    awaits_cfg = ((by_name.get(task_name) or {}).get("awaits") or {})
    # A REMOTE wait opens HERE, because it never takes the turn `_async_hold` speaks on:
    # its start call answers synchronously with a job handle, so intake completes the
    # call and no `pending` placeholder ever reaches the post-executor. Left to the
    # ladder below, `awaits.say` — the one line that explains why the caller is being
    # asked to wait at all — would be silently dropped and the FIRST thing said about a
    # brand-new job would be "Still crunching the numbers." Measured live before this:
    # the opening line was never spoken, on any turn.
    if (mark or {}).get("remote") and not mark.get("said"):
      mark["said"] = True
      opening = awaits_cfg.get("say")
      if opening:
        _log("remote_await_say", task=task_name)
        return _safe_format(opening, filled)
    lines = awaits_cfg.get("while_waiting")
    if not lines:
      continue
    idx = mark.get("held", 0)
    if idx >= len(lines):
      continue
    mark["held"] = idx + 1
    _log("async_await_line", task=task_name, n=idx + 1, of=len(lines))
    # Render against filled MINUS this wait's own outputs. Those are stale by
    # construction — the whole reason the task is outstanding is that they are about to
    # be replaced — and a reassurance line that quotes the placeholder back
    # ("Still checking on still checking") is worse than the silence it replaced.
    stale = set(_output_targets((by_name.get(task_name) or {}).get("outputs")))
    return _safe_format(
        lines[idx], {k: v for k, v in filled.items() if k not in stale} if stale
        else filled)
  return ""


def _wait_clock(sm: dict[str, Any]) -> int:
  """Turns a WAIT has had a chance on: caller turns PLUS inactivity ticks.

  `_turn_n` counts real user utterances, which is the right unit for the conversational
  ladders — a no-match ladder, a steer-back, a resume offer all measure how many times
  the CALLER has spoken. It is the wrong unit for a wait, and on voice it is not merely
  imprecise but frozen: a caller who says nothing produces inactivity ticks and no
  utterances at all, so `_turn_n` never advances and anything guarded on it fires
  exactly once for the whole wait.

  Measured live before this existed, on a four-minute remote job with a silent caller:
  the engine polled the job on the turn it started it and NEVER AGAIN (one GET in the
  service's request log, against nine on the same call driven over text). The failure
  was invisible from the transcript because `_async_idle_line` is not turn-guarded — it
  drained its reassurance ladder on every tick, so the call sounded exactly like a call
  that was polling, and the job's result was simply never collected.

  Identical to `_turn_n` wherever there are no ticks, which is every text call, every
  offline oracle and every unit test. It is only ever read against a `since` recorded in
  the same units, so the two must move together — every `_awaiting_async` mark below
  stamps this, and `_sweep_async_timeouts` measures `awaits.max_turns` against it. That
  makes `max_turns` mean something on voice too: it was previously unreachable on a
  silent call, so a wedged backend held the line forever.
  """
  return int(sm.get("_turn_n") or 0) + int(sm.get("_tick_n") or 0)


def _parallel_groups(tasks):
  """{group name: [legs]} for every fan-out group in this config, or {} for none."""
  groups = {}
  for task in tasks:
    group = task.get("parallel")
    if group:
      groups.setdefault(group, []).append(task)
  return groups


# ═════════════════════════════════════════════════════════════════════
# PROGRESSIVE FAN-OUT — a line per leg, the moment that leg lands.
# ═════════════════════════════════════════════════════════════════════
# A synchronous group hands back the whole batch after its SLOWEST leg, so three
# checks of 8s/18s/30s buy the caller half a minute of silence and then a wall of
# results. Progressive lowering keeps the authoring surface exactly as it was and
# changes only what the group compiles to:
#
#   * each leg is emitted as an ASYNCHRONOUS tool that publishes its result to its
#     OWN state key (`<group>_<leg>`) — never a shared object, because concurrent
#     writes to one structure lose N-1 of them (ces-probes 37/38);
#   * the emitter also generates a `<group>_peek` (one FRESH snapshot of those keys
#     per invocation — a running tool body's own view is frozen at the moment it
#     started, ces-probes 61/71) and a `<group>_watch` that polls peek THROUGH the
#     injected `tools` global until a leg outside `seen` lands;
#   * this engine dispatches exactly one watcher per pass and, when it returns,
#     speaks the landed leg's `then_say` as a PARTIAL preempt — which says the line
#     without ending the turn — carrying another watch call alongside it.
#
# Polling has to go through `tools` because sub-calls made that way never enter the
# transcript and cost no reasoning pass (ces-probes 70). There are exactly ten passes
# per input and nothing resets them (72/73), so a watcher polling as ordinary tool
# calls would spend the entire budget before speaking a word. The ten passes are for
# NARRATION POINTS, not for looking.
#
# The watch window is chunked rather than open-ended: a single tool call is safe to
# ~29s and fails somewhere at or below 60s, while CUMULATIVE time in a turn is fine to
# at least 82s (ces-probes 80/82). An empty watch simply costs one pass and is
# re-dispatched.

# The watch window, in seconds. MIRRORS `flows.emit.fanout.WATCH_WINDOW_SECONDS`;
# change one, change both. The engine cannot read the emitter, and it has to convert a
# leg's timeout into a number of windows.
_FANOUT_WATCH_WINDOW_SECONDS = 20

# Seconds CES allows a tool body to run when its resource does not say otherwise.
# Overrunning it is SILENT: the body never reports, so there is no error and no failed
# result, and the group would wait out its ladder for a completion that is not coming.
_FANOUT_DEFAULT_TIMEOUT_SECONDS = 60

# Empty watch windows tolerated before the group is written off — the default tool
# timeout expressed in windows (60 / 20), so a group waits about as long as CES will run
# an undeclared leg.
#
# DELIBERATELY NOT DERIVED from the legs' declared timeouts, though it should be: a leg
# with `timeout=180` is written off while CES is still happily running it. Deriving it
# was tried and REVERTED — widening this number kills the live turn at ~29s with a
# platform `failed_precondition`, reproduced across six runs, and the mechanism is not
# understood (the value's only consumer is the comparison below, which cannot fire until
# ~93s). See ces-probes `106` before trying again.
_FANOUT_MAX_EMPTY_WAITS = _FANOUT_DEFAULT_TIMEOUT_SECONDS // _FANOUT_WATCH_WINDOW_SECONDS


def _progressive_groups(tasks):
  """Group names lowered onto the progressive (narrate-as-they-land) path.

  A group qualifies when it has two or more legs and every leg fires a tool. Both are
  shapes `parallel()` and the validator already refuse, so every well-formed group is
  on this path — deliberately. An eligibility rule that quietly held some groups back
  would be the same class of defect as the ghost leg name: a program that looks right,
  builds clean, and behaves like the shape it was written to replace.

  `awaits` in particular does NOT disqualify a leg. `parallel(deadline=…)`,
  `waiting_say=` and `on_timeout=` all merge into `awaits`, so excluding it would make
  the most natural way to write a slow group the one way to opt out of narrating it.
  An `awaits` leg behaves identically here: its `pending` placeholder never reaches
  intake either way, `awaits.say` is spoken on the first watch dispatch below, and
  `max_turns`/`on_timeout` stay live in `_sweep_async_timeouts` as the cross-TURN
  backstop for a group that outlives the held floor.

  THE EMITTER COMPUTES THE SAME PREDICATE (`flows/emit/fanout.py::progressive_groups`)
  to decide which groups get leg/peek/watch codegen. They must agree: a group the
  engine treats as progressive but the emitter did not lower has no watcher to
  dispatch, and a leg name that resolves to no registered tool is SILENT AND FATAL —
  the turn simply dies with nothing surfaced anywhere (ces-probes 69).
  """
  out = set()
  for group, legs in _parallel_groups(tasks).items():
    if len(legs) < 2:
      continue
    if any(not leg.get("tool") for leg in legs):
      continue
    # `parallel(progressive=False)`. The legs keep the batch shape: synchronous, one
    # action, collected on the same pass. Treating such a group as progressive here
    # would dispatch a watcher the emitter never wrote, which is fatal and silent.
    if any(leg.get("parallel_batch") for leg in legs):
      continue
    out.add(group)
  return out


def _fanout_tool_names(tasks):
  """The synthetic peek/watch tools the emitter generates for this config.

  Hidden from the model on every turn for the same reason a task executor is: they are
  engine-owned dispatch, and a turn the model can see one is a turn it can call it.
  """
  names = set()
  for group in _progressive_groups(tasks):
    names.add(f"{group}_peek")
    names.add(f"{group}_watch")
  return names


def _remote_status_tools(config):
  """Every status wrapper this config's remote tools are polled through."""
  return {entry["status_tool"]
          for entry in ((config or {}).get("remote_tools") or {}).values()
          if entry.get("status_tool")}


def _remote_mark_pending(sm, config, tasks, filled):
  """Turn a job handle into an in-flight mark.

  Derived from STATE — the handle sitting in its slot — rather than from a marker set
  when the job started. Intake runs in `after_tool`, which is after the engine pass that
  dispatched the tool, so anything it leaves behind is invisible until the next pass and
  a marker that has to survive that boundary is a race. The handle is already durable:
  it is an ordinary filled slot.

  `_awaiting_async` is reused rather than a second pending set invented: it is already
  what keeps the selector off a task, what marks its outputs stale for consumers, and
  what `_async_idle_line` drains reassurance against. A remote job is a third REASON to
  be awaiting, not a different kind of waiting.
  """
  sm.pop("_remote_started", None)
  registry = config.get("remote_tools") or {}
  if not registry:
    return
  waiting = sm.setdefault("_awaiting_async", {})
  marked = []
  for task in tasks:
    entry = registry.get(task.get("tool"))
    if not entry:
      continue
    name = task.get("name")
    if not name or name in waiting:
      continue
    job_slot = entry.get("job_slot") or ""
    handle = str((filled or {}).get(job_slot) or "").strip()
    if not handle:
      continue
    # A job whose outputs already landed is finished, not in flight. Without this the
    # task is re-marked the pass after it completes and waits forever on a job that
    # has already answered.
    outs = {k: v for k, v in (task.get("outputs") or {}).items() if k != job_slot}
    if any(slot in (filled or {}) for slot in outs.values()):
      continue
    waiting[name] = {"tool": "", "since": _wait_clock(sm),
                     "remote": {"job": handle, "since": _wait_clock(sm)}}
    marked.append(name)
  if marked:
    _log("remote_started", tasks=sorted(marked))


def _remote_poll_handle(sm, mark):
  """The handle to poll with — and, for a MOCKED job, how long it has been waiting.

  A mock has no clock of its own. The emitted status mock is an ordinary tool: it is
  called once per poll, it cannot keep a counter between calls, and nothing else it can
  read says how far into the wait it is. So `after_turns(n, ...)` needs the count handed
  to it, and the poll's one argument is the handle.

  Scoped by the handle itself. A declared mock makes the START wrapper answer
  `mock-<tool>`, so a synthetic handle announces what it is; a handle a real service
  issued is passed through untouched. That matters because a mock is only CONSULTED when
  `mock_apis` is on — the same app, same config, mocked or not — so the suffix has to be
  decided by what the start call actually returned rather than by what was declared.
  """
  job = str(mark.get("job") or "")
  if not job.startswith("mock-"):
    return job
  return f"{job}#{_wait_clock(sm) - int(mark.get('since') or 0)}"


def _remote_turn(sm, config, tasks, hide_tools, task_msg, task_resp):
  """Poll every job still in flight — all of them, once per turn.

  Once per TURN rather than once per pass is the whole safety property: a task whose
  out_slot is unfilled stays fire-eligible, so an unguarded poll re-dispatches on every
  reasoning pass and burns the turn to the ten-loop cap.

  The guard counts `_wait_clock`, NOT `_turn_n`. An inactivity tick is a turn for every
  purpose this path has — it is the only turn a silent caller produces, and the entire
  feature rests on polling during silence — but it is not a caller utterance, so
  `_turn_n` does not move on one. Guarded on `_turn_n` this polled once per CALL on
  voice and then went quiet for the rest of the job.

  All of them together, not one per turn in rotation, because that is what makes remote
  jobs genuinely parallel: N handles are N `function_calls` on one dispatch, so three
  jobs cost the caller one wait rather than three.

  Returns None when nothing is in flight, which is every config that has no remote tool
  — so an app without one is untouched by this path.
  """
  if not (config.get("remote_tools") or {}):
    return None
  waiting = sm.get("_awaiting_async") or {}
  pending = sorted(n for n, mark in waiting.items() if (mark or {}).get("remote"))
  if not pending:
    return None
  if sm.get("_remote_polled_turn") == _wait_clock(sm):
    return None
  sm["_remote_polled_turn"] = _wait_clock(sm)

  by_tool = {t["name"]: t.get("tool") for t in tasks}
  calls, polled = [], []
  for task in pending:
    remote = (config["remote_tools"] or {}).get(by_tool.get(task) or "") or {}
    status_tool = remote.get("status_tool")
    mark = (waiting.get(task) or {}).get("remote") or {}
    job = mark.get("job")
    if not status_tool or not job:
      continue
    calls.append({"name": status_tool, "args": {"jobId": _remote_poll_handle(sm, mark)}})
    polled.append(task)
  if not calls:
    return None

  action = {
      # Declared for exactly the turn it fires, as a firing executor is — a dispatch to
      # a tool the agent does not list is dropped by CES without an error.
      "hide_tools": [t for t in (hide_tools or [])
                     if t not in {c["name"] for c in calls}],
      "preempt": True,
      "function_calls": calls,
      "message": task_msg or "",
      "speech_class": "await",
  }
  if task_resp:
    action["response"] = task_resp
  if task_msg or task_resp:
    # Spoken WITHOUT ending the turn, the same reason the fan-out watcher is partial:
    # a normal response hands the floor back and abandons the jobs still running.
    action["partial"] = True
  _log("remote_poll", tasks=polled, turn=_wait_clock(sm))
  return action


def _fanout_start(sm, group, legs):
  """Record that a progressive group has just been dispatched.

  Bookkeeping only — the fire action itself is unchanged, so the legs still go out in
  ONE preempt and the watcher is dispatched on the next pass (once they are genuinely
  running) rather than racing them out of the same one.
  """
  sm["_fanout"] = {
      "group": group,
      "legs": [leg["name"] for leg in legs],
      "tools": {leg["name"]: leg["tool"] for leg in legs},
      # Legs whose published result `before_model` has already read out of state.
      "done_legs": [],
      "waits": 0,
  }
  # The skip-list that stops a leg's LATER completion envelope re-speaking a finding
  # already narrated. Reset with the group: the envelopes of a previous run of the same
  # group are stale by then, and keeping the list forever would silence a re-run.
  sm.pop("_fanout_ingested", None)
  _log("fanout_started", group=group, legs=[leg["name"] for leg in legs])


def _fanout_mark_pending(sm):
  """Turn the `pending` placeholders `before_model` saw into in-flight marks.

  A lowered leg is an ASYNCHRONOUS tool, so its fire is answered with CES's
  `{"result": "pending"}` and the real payload is published to state instead. The
  placeholder is not a result and never reaches intake, so without a mark the selector
  would find the leg un-fired and dispatch the whole group again on the very next pass.

  `_awaiting_async` is the mark the selector already honours, and it also makes the
  leg's output slots `stale` — which is what stops a consumer, or the group's own
  all-done announce, acting on a value the leg is about to write.

  Deliberately driven by what the callback OBSERVED rather than set at fire time: a
  group whose tools answer synchronously (every offline run, and any app whose emitter
  did not lower it) never reports a placeholder, so it never gets marked and behaves
  exactly as it did before this path existed.
  """
  legs = sm.pop("_fanout_pending", None) or []
  fan = sm.get("_fanout") or {}
  if not legs or not fan:
    return
  waiting = sm.setdefault("_awaiting_async", {})
  for name in legs:
    if name in (fan.get("legs") or []):
      waiting.setdefault(name, {
          "tool": (fan.get("tools") or {}).get(name, ""),
          "since": _wait_clock(sm),
          "fanout": fan.get("group", ""),
      })
  _log("fanout_pending", group=fan.get("group"), legs=sorted(legs))


def _fanout_give_up(sm, fan, task_results):
  """Write off a group whose remaining legs never published.

  The legs are recorded as failed rather than left outstanding: outstanding means the
  selector keeps them out AND `<group>_done` never fills, so the flow would sit on a
  wedged backend forever. A failure is reported, which is all `<group>_done` means, so
  the group closes and the all-done line speaks over whatever did arrive.
  """
  waiting = sm.get("_awaiting_async") or {}
  stalled = [n for n in (fan.get("legs") or []) if n in waiting]
  # Written off for the life of the flow, not merely failed. A failed task is still
  # fire-eligible — that is what a retry IS — so recording the failure alone would have
  # the selector dispatch the whole group again on the very next pass, and the group
  # that just spent a minute producing nothing would spend another one.
  sm["_fanout_written_off"] = sorted(
      set(sm.get("_fanout_written_off") or []) | set(stalled))
  for name in stalled:
    waiting.pop(name, None)
    # NOT given an `error_code`. Naming this was tried and REVERTED: driven live, the
    # same written-off leg reported a reason on two turns and none on the next two, in a
    # steady alternation, and a second leg never reported at all. Whatever selects the
    # leg disposition after a write-off does not see a stable result, so a code here is
    # not something an author could rely on. See TIMEOUT_VERIFY.md; the reason belongs
    # here once that is understood, not before.
    task_results.setdefault(name, {"success": False, "error": "fanout_no_result"})
  # `task_results` may be the local default `{}` the caller built when sm carried none
  # (every leg answered `pending`, so intake never recorded one). Bind it back or the
  # write-off is invisible to `<group>_done` and the group re-fires.
  sm["task_results"] = task_results
  if not waiting:
    sm.pop("_awaiting_async", None)
  sm.pop("_fanout", None)
  _log("fanout_gave_up", "WARN", group=fan.get("group"), legs=stalled,
       waits=fan.get("waits"))


def _fanout_hold_line(sm, tasks, filled, fan):
  """The group's holding line, spoken ONCE on the first watch dispatch.

  `parallel(waiting_say=…)` lands on the first asynchronous leg as `awaits.say`, and
  the ordinary async path speaks it from `_async_hold` on the pending turn. This path
  never takes that turn — the placeholder never reaches the post-executor — so without
  this the author's holding line would be silently dropped by the lowering.

  Rides the first watch dispatch rather than a pass of its own: on its own pass the
  next pass follows instantly and clips its tail, whereas here the watcher blocks
  behind it and the audio has room to finish.
  """
  if fan.get("said"):
    return ""
  fan["said"] = True
  by_name = {t["name"]: t for t in tasks}
  for name in (fan.get("legs") or []):
    say = ((by_name.get(name) or {}).get("awaits") or {}).get("say")
    if say:
      return _safe_format(say, filled)
  return ""


def _fanout_turn(sm, tasks, filled, task_results, hide_tools, task_msg, task_resp):
  """The turn a progressive group owns, or None once it no longer owns one.

  Returns None — letting the ordinary path run — in exactly two cases: no leg is
  outstanding any more (the group is complete, so `<group>_done` fills below and the
  authored all-done announce closes it, with the last leg's line already ahead of it
  via `_parallel_batch_spoke`), or the group has been written off.

  Otherwise the turn is a watch dispatch. `partial` is what lets the leg's line be
  SPOKEN without ending the turn (ces-probes 57); a normal response would hand the
  floor back and abandon the legs still in flight.
  """
  fan = sm.get("_fanout")
  if not fan:
    return None
  waiting = sm.get("_awaiting_async") or {}
  outstanding = [n for n in (fan.get("legs") or []) if n in waiting]
  if not outstanding:
    sm.pop("_fanout", None)
    _log("fanout_complete", group=fan.get("group"))
    return None

  landed = [n for n in (fan.get("legs") or []) if n not in waiting]
  hold_line = _fanout_hold_line(sm, tasks, filled, fan)
  if hold_line:
    task_msg = f"{hold_line} {task_msg}".strip() if task_msg else hold_line
  spoke = bool(task_msg or task_resp)
  if spoke:
    # Progress resets the ladder: the budget is for gaps between landings, not for the
    # group's total duration. A gap longer than one window costs a pass, not the group.
    fan["waits"] = 0
  else:
    fan["waits"] = int(fan.get("waits") or 0) + 1
    if fan["waits"] > _FANOUT_MAX_EMPTY_WAITS:
      _fanout_give_up(sm, fan, task_results)
      return None
  sm["_fanout"] = fan

  watch_tool = f"{fan['group']}_watch"
  action = {
      # The watcher is hidden from the model on every other turn (see _hiding_policy);
      # the turn it is DISPATCHED it must stay declared, exactly as a firing task
      # executor does, or the dispatch renders empty.
      "hide_tools": [t for t in (hide_tools or []) if t != watch_tool],
      "preempt": True,
      "function_call": {"name": watch_tool, "args": {"seen": ",".join(landed)}},
      "message": task_msg or "",
      "speech_class": "await",
  }
  if task_resp:
    action["response"] = task_resp
  if spoke:
    # Only a response that actually SAYS something is partial. A bare re-dispatch has
    # no text to speak, and marking it partial would be a partial response with
    # nothing in it.
    action["partial"] = True
  _log("fanout_watch", group=fan.get("group"), outstanding=outstanding,
       landed=landed, spoke=spoke, waits=fan.get("waits"))
  return action


def _resolve_async_batch(sm, tasks):
  """Clear the waits for completions that landed on this turn but are NOT the one the
  post-executor will handle.

  `slot_intake` writes a SCALAR `_task_just_completed` and the handler pops exactly one,
  so when two envelopes arrive together only the last one's wait is cleared. The other
  would stay in `_awaiting_async` until `max_turns` and report a timeout for a backend
  that answered — the exact failure that harvesting every envelope exists to prevent.

  Their results and output slots are already recorded (intake did that). What is skipped
  for the extras is the spoken half — `then_say` / the `on_failure` ladder — because only
  one message can be spoken per turn anyway. Logged so it is visible rather than silent.
  """
  batch = [n for n in (sm.pop("_async_batch", None) or [])
           if n != sm.get("_task_just_completed")]
  if not batch:
    return
  waiting = sm.get("_awaiting_async") or {}
  by_name = {t["name"]: t for t in tasks}
  for name in batch:
    if waiting.pop(name, None) is not None:
      success_key = (by_name.get(name) or {}).get("success_check", "success")
      ok = bool((sm.get("task_results") or {}).get(name, {}).get(success_key))
      _log("async_await_resolved", task=name, batched=True, ok=ok)
  if not waiting:
    sm.pop("_awaiting_async", None)


_ANSWER_FIRST_CAP = 4


def _apply_answer_first(sm, config, last_user_text, completion_landed):
  """Decide what a turn carrying BOTH a completion and caller speech actually IS.

  CES packs them together when the caller answers at the same moment the backend
  replies — which is not exotic, because asking questions during a slow call is the
  whole point of the primitive. Only one reading of the turn can win, and each has a
  cost:

    it is a DELIVERY (default)   the text is blanked, so the terminal the completion
                                 unblocked can fire. The caller's utterance never
                                 reaches a setter: the slot stays open and the flow
                                 re-asks. Recoverable, and what every agent does today.

    it is BOTH (`answer_first`)  the text is kept, so the model answers the caller
                                 normally. `_defer_terminal` then holds the terminal
                                 fire — correctly, on the first turn — and the budget
                                 armed here is what stops that hold becoming permanent.
                                 Without a bound it IS permanent: every later turn
                                 carries speech too, so nothing re-fires the terminal
                                 and the flow never closes out (measured on a live app).

  Opt-in, so an agent that does not set it is byte-identical. `_ANSWER_FIRST_CAP` bounds
  whatever the author asks for, on the same reasoning as every other ladder here: the
  caller must not be able to hold a completed transaction open indefinitely by talking.
  """
  if not completion_landed:
    return last_user_text
  task_def = next(
      (t for t in config.get("tasks", [])
       if t.get("name") == sm.get("_task_just_completed")), None)
  budget = ((task_def or {}).get("awaits") or {}).get("answer_first")
  if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
    if last_user_text:
      _log("async_completion_text_dropped", "WARN",
           task=(task_def or {}).get("name"), chars=len(last_user_text))
    return ""
  sm["_answer_first_left"] = min(int(budget), _ANSWER_FIRST_CAP)
  _log("answer_first_armed", task=(task_def or {}).get("name"),
       turns=sm["_answer_first_left"], spoke=bool(last_user_text))
  return last_user_text


def _sweep_async_timeouts(
    sm: dict[str, Any], tasks: list[dict[str, Any]], filled: dict[str, Any],
) -> Optional[dict[str, Any]]:
  """Give up on an ASYNCHRONOUS tool that never delivered.

  CES has no platform-side timeout — a hung async tool simply never sends its completion
  turn — so `awaits.max_turns` is the only thing between a wedged backend and a wedged
  call. Turns are the unit because the engine has no clock, the same reason the no_input
  ladder counts reprompts rather than seconds.
  """
  waiting = sm.get("_awaiting_async") or {}
  if not waiting:
    return None
  # A result that landed THIS turn has not been handled yet: `_handle_post_executor` is
  # what clears the wait, and it runs after this sweep. Timing the task out here would
  # throw away an answer already in hand.
  just_completed = sm.get("_task_just_completed")
  # Ticks count. `max_turns` is the only thing between a wedged backend and a wedged
  # call, and on a silent voice call `_turn_n` never advances — so measured against it
  # the give-up was unreachable exactly when it was needed.
  now = _wait_clock(sm)
  by_name = {t["name"]: t for t in tasks}
  for task_name, mark in list(waiting.items()):
    if task_name == just_completed:
      continue
    awaits = (by_name.get(task_name) or {}).get("awaits") or {}
    max_turns = awaits.get("max_turns")
    # CES round-trips session numbers as floats, so `since` comes back as e.g. 2.0.
    if not isinstance(max_turns, (int, float)) or isinstance(max_turns, bool):
      continue
    if now - mark.get("since", now) < max_turns:
      continue
    waiting.pop(task_name, None)
    if not waiting:
      sm.pop("_awaiting_async", None)
    on_timeout = awaits.get("on_timeout") or {}
    _log("async_await_timeout", "WARN", task=task_name, turns=max_turns)
    fc = _resolve_exhaust_action(on_timeout, filled)
    action: dict[str, Any] = {
        "hide_tools": [],
        "preempt": True,
        "message": on_timeout.get("say", ""),
        "speech_class": "await",
        "verbatim": bool(awaits.get("verbatim")),
    }
    if fc:
      action["function_call"] = fc
      sm["status"] = "escalated"
    return action
  return None


def _resolve_clear_slots(on_failure: dict, result: dict) -> list[str]:
  """The slots an `on_failure` clears for THIS failure.

  A plain list clears the same slots every time. A dict keyed by the failing tool's
  `error_code` clears a different set per reason (falling back to `_default`), so a
  verify-and-correct task can drop exactly the invalid field — e.g. a bad DOB clears
  `date_of_birth` while a wrong member clears `member_id`. Callers still get a flat list.
  """
  return list(_resolve_by_code(on_failure.get("clear_slots"), result, []) or [])


def _resolve_by_code(spec, result, fallback):
  """One keyed disposition branch, or the plain value when it is not keyed.

  The reason-aware shape shared by everything that reacts to a failure: `on_failure`'s
  `clear_slots`, `retry_say` and `on_exhaust.say`, and a slot's `validation.errors` and
  its response siblings. A plain value applies to every failure; a dict picks the branch
  named by the failing tool's `error_code`, falling back to `_default`.

  Lookup is by MEMBERSHIP, not truthiness. The original `clear_slots` resolver used
  `spec.get(code) or spec.get("_default")`, so a deliberately empty branch — clear nothing,
  say nothing — was falsy and fell through to `_default` instead. A named code now always
  wins, which makes `{"timeout": ""}` a silent retry rather than an accident.

  Args:
    spec: The authored value: a plain value, or a dict keyed by `error_code`.
    result: The failing tool's payload, which may carry an `error_code`.
    fallback: Used when the spec names neither this code nor `_default`.

  Returns:
    The branch that applies to this failure.
  """
  if not isinstance(spec, dict):
    return fallback if spec is None else spec
  code = (result or {}).get("error_code")
  if code in spec:
    return spec[code]
  return spec.get("_default", fallback)


def _handle_post_executor(
    sm: dict[str, Any], tasks: list[dict[str, Any]],
    task_results: dict[str, Any],
    filled: dict[str, Any], pending: dict[str, Any],
    deferred: dict[str, Any],
    retries: dict[str, Any], confirm_transition_prefix: str,
    inv_n: int, phase: str, fresh_pending: bool,
    hide_tools: list[str],
    channel: str = "",
    config: Optional[dict[str, Any]] = None,
) -> tuple[Optional[dict[str, Any]], str, Optional[list[dict[str, Any]]]]:
  """Handle task executor results and retries."""
  # A parallel group lands N results on ONE turn, and every one of them owns a
  # disposition — its then_say, its on_failure ladder. The scalar below holds a single
  # name, so without this the legs after the first are recorded but never speak. Run the
  # handler once per leg in DECLARATION order (arrival order is neither stable nor
  # observable) and concatenate what they say.
  batch = sm.pop("_completed_batch", None) or []
  if batch:
    actions, msgs, resps = [], [], []
    by_name = {t["name"]: t for t in tasks}
    for name in [t["name"] for t in tasks if t["name"] in batch]:
      leg = by_name[name]
      leg_result = task_results.get(name, {})
      leg_ok = bool(leg_result.get(leg.get("success_check", "success")))
      # A leg that failed does NOT take the group down. The legs were grouped because
      # they are independent, so one flaky backend silencing the other two is the
      # opposite of what was asked for -- and the default ladder would do exactly that,
      # since max_retries is 0 and the first failure escalates. The author opts back in
      # by giving the leg an explicit on_exhaust disposition; a bare say is spoken so the
      # gap is audible, and anything else is a logged non-event.
      # An asynchronous placeholder is not a failure and belongs to the handler.
      if not leg_ok and not _is_async_pending(leg_result):
        exhaust = (leg.get("on_failure") or {}).get("on_exhaust") or {}
        if not exhaust.get("then"):
          # Resolved, not read: a leg's exhaust line takes the same reason-keyed form as
          # every other disposition, and appending the raw value would speak the dict.
          leg_say = _resolve_by_code(exhaust.get("say"), leg_result, "")
          if leg_say:
            msgs.append(leg_say)
          _log("parallel_leg_failed", "WARN", task=name,
               group=leg.get("parallel"), spoken=bool(leg_say))
          # This leg REPORTED — it just reported a failure. On the progressive path
          # that has to release its in-flight mark here, because the recursion below
          # (which normally does it) is the branch we are skipping. Left marked, a
          # flaky backend would hold `<group>_done` open for the rest of the call and
          # the group would never speak its closing line.
          _waiting = sm.get("_awaiting_async") or {}
          if _waiting.pop(name, None) is not None and not _waiting:
            sm.pop("_awaiting_async", None)
          continue
      sm["_task_just_completed"] = name
      one_action, one_msg, one_resp = _handle_post_executor(
          sm, tasks, task_results, filled, pending, deferred, retries,
          confirm_transition_prefix, inv_n, phase, fresh_pending, hide_tools,
          channel=channel, config=config)
      if one_action is not None:
        actions.append((name, one_action))
      if one_msg:
        msgs.append(one_msg)
      if one_resp:
        resps.extend(one_resp)
    sm.pop("_task_just_completed", None)
    # A turn can only take ONE disposition — a hold, a failure ladder, a hand-off. Every
    # leg's spoken half is kept; the extra actions are logged rather than silently lost.
    if len(actions) > 1:
      _log("parallel_actions_dropped", "WARN", kept=actions[0][0],
           dropped=[n for n, _ in actions[1:]])
    # Tells the response assembly to put these lines AHEAD of any announce settling on
    # the same pass — the group's all-done line closes them and must not precede them.
    if msgs or resps:
      sm["_parallel_batch_spoke"] = True
    if actions:
      # The caller returns an action AS IS and drops the message beside it, so a leg
      # that produces a disposition — an asynchronous hold, a failure ladder — would
      # take the whole turn and silence every sibling that already answered. Found live:
      # a group with one deferred leg spoke only its holding line, and the three
      # completed lookups said nothing at all. Fold them in ahead of it.
      action = dict(actions[0][1])
      # Messages are merged just below; directives were not, so a leg's composed answer
      # vanished with only the WARN above to show for it. Same treatment for both.
      _directives = [a.get("task_directive") for _n, a in actions
                     if a.get("task_directive")]
      if _directives:
        _kept = action.get("task_directive")
        # dict.fromkeys: order-preserving dedup. Two legs can carry the SAME directive
        # (a shared then_directive on a fan-out), and repeating it reads as a stutter.
        action["task_directive"] = " ".join(
            dict.fromkeys(([_kept] if _kept else []) + _directives))
      if msgs:
        action["message"] = " ".join(msgs + ([action["message"]]
                                             if action.get("message") else []))
      return action, "", resps or None
    return None, " ".join(msgs), resps or None

  task_just = sm.pop("_task_just_completed", None)
  if not task_just:
    return None, "", None

  task_def = next(t for t in tasks if t["name"] == task_just)
  success_key = task_def.get("success_check", "success")
  result = task_results.get(task_just, {})

  # An ASYNCHRONOUS tool answers twice: a platform-substituted `{"result": "pending"}`
  # now, and the real payload a turn or more later as a synthetic user turn. The
  # placeholder is falsy under success_check, so without this branch it falls into the
  # on_failure ladder — where max_retries defaults to 0, making the FIRST fire escalate
  # the flow. Nothing failed; record the wait and hold.
  if task_def.get("awaits"):
    if _is_async_pending(result):
      sm.setdefault("_awaiting_async", {})[task_just] = {
          "tool": task_def.get("tool", ""),
          "since": _wait_clock(sm),
          # The values this dispatch was made WITH. A wait spans turns, and the caller
          # can change their mind inside it -- correct an account number, a date, an
          # address. The payload that lands is an answer about the OLD value, and
          # nothing downstream can tell: it arrives in the same shape a fresh one would.
          # Recorded here so arrival can compare (see `async_await_stale`).
          "inputs": {k: filled.get(k) for k in (task_def.get("inputs") or [])},
      }
      _log("async_await_started", task=task_just, tool=task_def.get("tool", ""))
      say = (task_def.get("awaits") or {}).get("say") or ""
      if say:
        return _async_hold(task_def), "", None
      # No line to speak, so do NOT end the turn: the wait blocks only this task, and
      # anything else the DAG can do — the next question, an unrelated task — should
      # still happen rather than the caller sitting through a wasted silent turn.
      # `_awaiting_async` keeps this task out of the selector, and if the DAG turns out
      # to have nothing to do the turn becomes a silent hold further down.
      return None, "", None
    # The real payload landed — but it may be an answer to a question that has since
    # changed. Compare the inputs it was dispatched with against what they are now.
    waiting = sm.get("_awaiting_async") or {}
    _mark = waiting.get(task_just) or {}
    # ABSENT is not CHANGED, and the distinction is the whole guard. A slot can be
    # missing from `filled` on the arrival turn for reasons that have nothing to do with
    # the caller -- a `reset_on_complete` re-arm empties it, and a shared slot is
    # restored on its own schedule. Treating that as a correction dropped every healthy
    # payload, re-dispatched, and rode the wait to `on_timeout`: measured live, every
    # journey answered "I just ran a few checks but wasn't able to get all the info I
    # need". Only a value that is PRESENT and DIFFERENT is the caller changing their
    # mind.
    #
    # EMPTY counts as absent, deliberately. Nothing here ever STORES `None` or `""` into
    # `filled` — every clearing path is a `pop`/`del`, so a slot the caller withdrew is
    # gone rather than blanked. An empty value can therefore only arrive from a restore
    # or a task output mid-rewrite, which are the innocent cases above; reading it as a
    # correction would resurrect the incident this guard exists to fix. Pinned by
    # `test_empty_input_reads_as_absent_not_changed`.
    _stale = [k for k, v in (_mark.get("inputs") or {}).items()
              if filled.get(k) not in (None, "", v)]
    if _stale:
      # Applying it would write a verdict about the superseded value and latch the
      # task's output, so the corrected one is never asked about at all — the caller
      # hears a confident answer to the question they just withdrew. Drop the payload,
      # release the wait, and forget the result so the selector can dispatch again with
      # the value that is true now.
      waiting.pop(task_just, None)
      if not waiting:
        sm.pop("_awaiting_async", None)
      task_results.pop(task_just, None)
      _log("async_await_stale", task=task_just, changed=sorted(_stale))
      return None, "", None
    _released = waiting.pop(task_just, None)
    if _released is not None:
      _log("async_await_resolved", task=task_just)
      # A remote job that ALSO declares `awaits` resolves here rather than in the remote
      # branch below, which only sees a task with no wait policy. Reported either way:
      # "the job the service was running came back, after N turns, under this handle" is
      # the line you look for when a wait ends, and having it depend on whether the
      # author happened to write reassurance copy makes the log a worse witness than it
      # needs to be.
      _remote_mark = (_released or {}).get("remote") or {}
      if _remote_mark:
        _log("remote_landed", task=task_just, job=_remote_mark.get("job", ""),
             turns=_wait_clock(sm) - int(_remote_mark.get("since") or 0))
    else:
      # Nothing was waiting for this. The backend answered after `on_timeout` already
      # released the wait and the flow moved on — so the payload is applied on top of a
      # turn that may have said "I couldn't reach that system", which is the same shape
      # of defect the staleness guard above exists to stop.
      #
      # Applied anyway, on purpose: intake has already written the output slots by the
      # time we get here, so dropping it now would leave the slots set and the result
      # missing, which is worse than either outcome. Dropping it properly means
      # unwinding intake too, and the last time this engine got more eager about
      # discarding payloads it took out every healthy journey — so that is a change to
      # make against a live measurement, not from first principles.
      # Logged so the rate is visible rather than assumed. Pinned by
      # `test_untracked_payload_is_applied_and_logged`.
      #
      # A fan-out LEG is the expected exception, not an anomaly. The group has its own
      # tracking: `_fanout_mark_pending` books a leg into `_awaiting_async` only when the
      # dispatch actually answered `pending`, so a leg quick enough to report inside its
      # firing turn is never booked and correctly arrives with nothing waiting for it.
      # Warning on that reads as a defect rate for the healthy fast path — which it did,
      # twice per call, on the first converted agent to use a group with a `deadline`.
      if task_just in ((sm.get("_fanout") or {}).get("legs") or []):
        _log("async_leg_completed_in_turn", task=task_just)
      else:
        _log("async_await_untracked", "WARN", task=task_just)
    if not waiting:
      sm.pop("_awaiting_async", None)
  elif ((sm.get("_awaiting_async") or {}).get(task_just) or {}).get("remote"):
    # A remote job landed. It is asynchronous by DEPLOYMENT rather than by declaration,
    # so like a fan-out leg it carries no `awaits` block for the branch above to key on.
    # Only a terminal status reaches here — intake returns early while the job is still
    # running — so release the mark and fall through to ordinary handling, which gives
    # the task the same then_say and the same on_failure ladder a local tool would get,
    # keyed on the service's own `error_code`.
    waiting = sm.get("_awaiting_async") or {}
    mark = waiting.pop(task_just, None)
    if mark is not None:
      _log("remote_landed", task=task_just,
           job=((mark or {}).get("remote") or {}).get("job", ""),
           turns=_wait_clock(sm) - int((mark or {}).get("since") or 0))
    if not waiting:
      sm.pop("_awaiting_async", None)

  elif task_just in ((sm.get("_fanout") or {}).get("legs") or []):
    # A progressive fan-out leg is asynchronous by LOWERING, not by declaration, so it
    # carries no `awaits` block for the branch above to key on. Its `pending`
    # placeholder never reaches intake (before_model drops it), so anything arriving
    # here is the real payload read out of the leg's state key: release the in-flight
    # mark and fall through to ordinary handling, which is what produces its then_say.
    waiting = sm.get("_awaiting_async") or {}
    if waiting.pop(task_just, None) is not None:
      _log("fanout_leg_landed", task=task_just,
           group=(sm.get("_fanout") or {}).get("group"))
    if not waiting:
      sm.pop("_awaiting_async", None)

  if result.get(success_key):
    _log("task", name=task_just, ok=True)
    retries.pop(task_just, None)
    sub_context = {**filled, **result}
    task_msg = ""
    task_directive = ""
    msg_template = task_def.get("then_say", "")
    # A surface-specific wording replaces the floor. Applied BEFORE the value-gate
    # below so the gate inspects the text that will actually be spoken — a brief
    # form that drops an interpolated field must not slip past a check performed
    # on the long one.
    _then_variant = _resolve_variant_text(task_def, "then_say", sub_context, channel)
    if _then_variant:
      msg_template = _then_variant
    directive_template = task_def.get("then_directive", "")
    # Value-gate a TERMINAL's value-announcing completion `then_say`. A terminal whose then_say interpolates
    # a field the executor did NOT produce (absent or None/"" in sub_context — e.g. `{confirmation_number}` on
    # a guardrail-refusal path) would, if spoken, FALSELY claim a credit-file action completed AND leak a raw
    # "{placeholder}" (a compliance hazard). Suppress it and relay a neutral non-completion line (or a
    # config-provided then_directive) instead. Scoped to TERMINAL tasks ONLY: a spine task's then_say is
    # intermediate progress, and many migrated spine tasks carry a generic "Done…" then_say whose optional
    # fields are legitimately absent on success — gating those would inject a false "couldn't complete"
    # mid-flow (regression). No-op when every referenced value is present ⇒ byte-identical for a normal
    # completion, and for every non-terminal task.
    if (task_def.get("terminal") and msg_template
        and _template_missing_field(msg_template, sub_context)):
      _log("then_say_value_gated", "WARN", task=task_just)
      task_msg = ""
      if directive_template:
        task_directive = _safe_format(directive_template, sub_context)
      else:
        task_directive = ("I wasn't able to complete that request. "
                          "Let me know if there's anything else I can help with.")
    else:
      # _safe_format, not str.format: it resolves the inline fallback form
      # `{slot|some words}` and leaves an unknown bare placeholder literal. Raw .format
      # RAISES on a fallback placeholder (the whole `slot|some words` is the field name),
      # and the exception is swallowed upstream — so the line silently vanishes and the
      # model improvises its own wording in place of the authored script.
      if msg_template:
        task_msg = _safe_format(msg_template, sub_context)
      if directive_template:
        task_directive = _safe_format(directive_template, sub_context)
    deferred_transition = sm.pop("_deferred_transition", False)
    if deferred_transition and task_msg and confirm_transition_prefix:
      task_msg = f"{confirm_transition_prefix} {task_msg}"
    if task_def.get("terminal"):
      # Component return (§6.5): if a call frame is active, the child's terminal
      # task completing means the SUB-FLOW is returning — NOT a conversation
      # termination. Pop the frame, restore the parent scope, merge outputs, mark
      # the Component done (all in _frame_return), and end the pass; the selector
      # walks the parent next pass. NON-None action so the caller's
      # `if result: return result` fires before _cascade_announce could walk the
      # child config against the just-restored parent scope. No zombie, no transfer.
      stack = sm.get("_call_stack")
      # end_conversation makes a child terminal task tear down the whole
      # conversation (via _terminate below) instead of returning to the parent.
      if stack and not task_def.get("end_conversation"):
        _frame_return(sm, stack[-1], child_filled=filled)
        return dict(_DESCENT_END_PASS), "", None
      cfg = config or {}
      # Completion is a termination: tear the flow down to a zombie via the
      # shared _terminate primitive (same path cancellation uses). A flow with
      # a paused sibling auto-resumes it instead of transferring to the parent.
      on_complete = task_def.get("on_complete") or {}
      if (on_complete.get("auto_resume_deferred")
          and sm.get("_flow_state")):
        sm["_auto_resume_deferred"] = True
        _log("on_complete_auto_resume", task=task_just,
             flow_state=[e.get("flow") for e in sm["_flow_state"]])
        zombie = _terminate(
            sm, cfg, filled, pending, deferred, task_results)
      else:
        zombie = _terminate(
            sm, cfg, filled, pending, deferred, task_results,
            transfer_to=on_complete.get("transfer_to", ""))
      _log("zombie_created", task=task_just, zombie=zombie)
      if task_directive and not task_msg:
        _log_invoke(inv_n, phase, filled, pending, fresh_pending,
                    hide_tools, fired=task_just, deferred=deferred)
        sm["_directive_open"] = True
        return {
            "hide_tools": [],
            "preempt": False,
            "task_directive": task_directive,
        }, "", None
      _log_invoke(inv_n, phase, filled, pending, fresh_pending,
                  hide_tools, fired=task_just, preempted=task_msg,
                  deferred=deferred)
      preempt_result = {
          "hide_tools": [], "preempt": True, "message": task_msg,
      }
      resp = _resolve_response(
          task_def, "then_response", sub_context, channel,
      )
      if resp:
        preempt_result["response"] = resp
      return preempt_result, task_msg, None
    task_resp = _resolve_response(
        task_def, "then_response", sub_context, channel,
    )
    if task_directive and not task_msg:
      # `task_resp` used to ride out as the third element and be dropped on the floor by
      # the caller, which returns `result` alone — so a directive task's `then_response`
      # never reached the caller.
      #
      # It cannot ride INLINE the way the preempt paths do: an inline `response` is only
      # read on a preempt, and this return deliberately is not one (the model has to run
      # to honour the directive). It also returns early, before `_route_payloads` would
      # normally place it. So stash it where a non-preempting turn's payloads belong and
      # after_model appends it to the model's reply.
      if task_resp:
        sm["_pending_payloads"] = list(task_resp)
      # Purely observational: marks the turn as owing a model-composed answer so
      # `before_model` can say so if a preempt then cancels it. Nothing reads it to
      # change behaviour.
      sm["_directive_open"] = True
      return {"task_directive": task_directive}, "", task_resp
    # `preempt_then_say` (opt-in, per task): speak this task's then_say VERBATIM even
    # though the task is non-terminal. Without it a non-terminal then_say is folded in
    # with the next question and RELAYED, so the model re-renders it — and right after a
    # backend action that is exactly where it invents outcomes it never performed
    # ("I've sent that to you in a text message" with send_confirmation never called).
    # Mirrors the terminal branch above; `then_response` rides inline rather than going
    # to the append-only payload channel (whose text parts are dropped downstream).
    if task_msg and task_def.get("preempt_then_say"):
      preempt_result = {
          "hide_tools": [], "preempt": True, "message": task_msg,
      }
      if task_resp:
        preempt_result["response"] = task_resp
      _log_invoke(inv_n, phase, filled, pending, fresh_pending,
                  hide_tools, fired=task_just, preempted=task_msg,
                  deferred=deferred)
      return preempt_result, task_msg, None
    return None, task_msg, task_resp

  _log("task", "WARN", name=task_just, ok=False)
  on_failure = task_def.get("on_failure", {})
  # Reason-aware, like every other rung of this ladder. HOW MANY TIMES to retry is as
  # much a property of the failure as what to say about it: a lost remote job is worth
  # starting over, a job the service ran and failed is not, and both arrive at the same
  # task. Read as a plain number when that is what was authored, so nothing changes for
  # a config that does not key it.
  #
  # This was a crash, not a missing feature: a dict reached the `retries >= max_retries`
  # comparisons below and raised `TypeError: '>=' not supported between instances of
  # 'int' and 'dict'` — inside the engine tool, so CES answered the call with no
  # `result`, before_model KeyError'd on it, and the whole turn died with the caller
  # hearing "I wasn't able to complete that request".
  max_retries = _resolve_by_code(on_failure.get("max_retries"), result, 0)
  if not isinstance(max_retries, int) or isinstance(max_retries, bool):
    try:
      max_retries = int(max_retries)
    except (TypeError, ValueError):
      max_retries = 0
  retries[task_just] = retries.get(task_just, 0) + 1
  exhaust = on_failure.get("on_exhaust", {})
  # Reason-aware: clear the slots this SPECIFIC failure names (keyed on the tool's
  # error_code), or the plain list if that is what was authored.
  clear_slots = _resolve_clear_slots(on_failure, result)
  for sn in clear_slots:
    filled.pop(sn, None)
  # `fill`: resolve SEVERAL slots to authored values, so the flow still arbitrates
  # over a complete picture after a failure. `open_slot` is the one-slot, always-True
  # form of this and stays as it is; a diagnostic sweep that errors needs every status
  # it feeds to say "error" rather than to go absent, because an absent value is
  # indistinguishable from a benign one and a lower-priority branch then wins a
  # comparison it should have lost. Applied AFTER clear_slots, so a failure can clear
  # what it collected and still state a disposition.
  fill = on_failure.get("fill") or {}
  if fill and retries[task_just] >= max_retries:
    for sn, sv in fill.items():
      filled[sn] = sv
    # Written off, not merely failed — the same distinction `_fanout_give_up` draws for a
    # leg. A failed result is falsy under `success_check`, which is what keeps a task
    # fire-eligible so a retry can happen at all, so recording the failure alone leaves
    # the selector dispatching this task again on the very next turn. It fails again,
    # exhausts again, and the caller hears the same disposition line every turn with no
    # ladder left to climb — a wedged call, measured in ces-probes 114.
    #
    # Scoped to `fill` on purpose. `fill` says "this task is finished, here is the value
    # to carry on with", so refiring contradicts it. A ladder using `clear_slots` means
    # the opposite — drop the bad input and let the caller supply another — and that one
    # MUST stay eligible, which is why this is not a blanket rule about exhausted tasks.
    sm["_task_written_off"] = sorted(
        set(sm.get("_task_written_off") or []) | {task_just})
    _log("task_exhaust_fill", "WARN", name=task_just, slots=sorted(fill))
  # Exhaust with open_slot: arm an IN-FLOW offer flag and fall through so the DAG
  # advances to the offer slot (same flow scope, so a fresh value spoken at the offer
  # re-fires this task instead of being lost to an isolated child).
  if retries[task_just] >= max_retries and exhaust.get("open_slot"):
    _open = exhaust["open_slot"]
    filled[_open] = True
    retries.pop(task_just, None)
    _log("task_exhaust_open_slot", "WARN", name=task_just, slot=_open)
    cfg = config or {}
    _slots = cfg.get("slots", [])
    _slot_map = {s["name"]: s for s in _slots if "name" in s}
    _reselect = _find_next_question(
        _slots, filled, pending, _slot_map, deferred=deferred,
        channel=channel, sm=sm)
    _cleared = set(clear_slots)
    if (_reselect.get("action") == "next_question"
        and _reselect.get("slot_name") not in _cleared):
      return None, "", None
    _close = _resolve_by_code(exhaust.get("say"), result, "An error occurred.")
    try:
      _close = _close.format(**filled)
    except (KeyError, IndexError):
      pass
    sm["status"] = "complete"
    _log("task_exhaust_open_slot_unreachable", "WARN", name=task_just, slot=_open)
    return {"hide_tools": hide_tools, "preempt": True, "message": _close}, "", None
  # Exhaust with a component: descend into the offer/help child DAG instead.
  if retries[task_just] >= max_retries and exhaust.get("component"):
    retries.pop(task_just, None)
    _synth = {
        "name": f"{task_just}_offer",
        "component": exhaust["component"],
        "inputs": exhaust.get("inputs") or {},
        "outputs": exhaust.get("outputs") or {},
        "on_abort": "skip",
    }
    _log("task_exhaust_component", name=task_just, child=exhaust["component"])
    return _component_fire_action(sm, config or {}, _synth, filled), "", None
  if retries[task_just] >= max_retries:
    fc = _resolve_exhaust_action(exhaust, filled)
    # `escalate: False` opts a task-exhaust `then` out of terminal escalation — used
    # for in-flow PIVOTS (e.g. OTP->KBA via a set_auth_method-style tool) that fire a
    # tool but must let the flow CONTINUE. Default True preserves every real
    # escalation (set_human_agent_transfer, etc.).
    if fc and exhaust.get("escalate", True):
      sm["status"] = "escalated"
    _log("task_exhaust", "ERROR", name=task_just)
    exhaust_msg = _resolve_by_code(exhaust.get("say"), result, "An error occurred.")
    _log_invoke(inv_n, phase, filled, pending, fresh_pending,
                hide_tools, preempted=exhaust_msg, deferred=deferred)
    result = {
        "hide_tools": hide_tools, "preempt": True, "message": exhaust_msg,
        "speech_class": "exhaust",
        "verbatim": bool(on_failure.get("verbatim")),
    }
    # Terminal exhaust: the ladder is spent, so write the task off exactly as the `fill`
    # branch does (see the note at the fill write-off). A failed result is falsy under
    # `success_check`, so without this the selector re-dispatches this same task on the
    # next turn; on an empty-contents auto-turn where the disposition line carries no
    # function_call to advance the flow, that re-fire is what wedges the call into the
    # reasoning-loop cap. The write-off self-heals at the two sites that re-open a task
    # for another attempt: `_abandon_journey` (intent switch / cancel — per-task) and
    # `_apply_correction_pending` (a corrected input — wholesale, with the result clear).
    #
    # Scoped exactly as the `fill` write-off is: NOT a blanket rule about exhausted tasks.
    # Two carve-outs, both "this task must stay eligible":
    #   * NON-TERMINAL tasks. A non-terminal task that keeps failing is a legitimate ladder
    #     — the engine re-fires it and SPEAKS its retry line every caller turn (it yields,
    #     so it does not wedge the auto-turn loop the way a silent terminal give-up does).
    #     Writing it off silences it, which reads as a stalled agent (test_uj_live_sim:
    #     `test_an_agent_that_keeps_retrying_out_loud_is_not_a_stall`). The wedge this
    #     write-off exists for is a TERMINAL exhaust whose disposition carries no
    #     function_call — hence the gate matches the "Terminal exhaust" comment above.
    #   * `clear_slots` ladders — the opposite of `fill` ("drop the bad input and let the
    #     caller supply another"). Such a task already cleared its input, so its `requires`
    #     is unmet and it CANNOT wedge; writing it off would instead block the re-fire a
    #     fresh value is supposed to trigger (a normal setter fill, which is not a
    #     correction, so `_apply_correction_pending` never heals it).
    if task_def.get("terminal") and not clear_slots:
      sm["_task_written_off"] = sorted(
          set(sm.get("_task_written_off") or []) | {task_just})
    if fc:
      result["function_call"] = fc
    resp = _resolve_response(exhaust, "response", filled, channel)
    if resp:
      result["response"] = resp
      _mark_end_session(sm, resp, "task_exhaust", name=task_just)
    return result, "", None
  retry_msg = _resolve_by_code(
      on_failure.get("retry_say"), result, "Let me try again.")
  _log_invoke(inv_n, phase, filled, pending, fresh_pending,
              hide_tools, preempted=retry_msg, deferred=deferred)
  retry_result = {
      "hide_tools": hide_tools, "preempt": True, "message": retry_msg,
      "speech_class": "retry",
      "verbatim": bool(on_failure.get("verbatim")),
  }
  resp = _resolve_response(on_failure, "retry_response", filled, channel)
  if resp:
    retry_result["response"] = resp
  # Let a RETRY fire a tool, exactly as on_exhaust already can. Without this a task can
  # only re-ask on failure, never act on it — which is why a rejected OTP was re-offered
  # instead of invalidated and replaced. Same helper, same `then` shape, read off
  # `on_failure` instead of `on_exhaust`, so there is one contract to learn rather than
  # two. Bounded by construction: this branch is only reachable while
  # retries < max_retries, so `max_retries: 3` gives at most 2 re-fires. That bound is
  # load-bearing — an unbounded version is an SMS-send primitive driven by an
  # unverified caller.
  fc = _resolve_exhaust_action(on_failure, filled)
  if fc:
    retry_result["function_call"] = fc
    return retry_result, "", None
  # No `then`, so the retry has nothing to DO — and until now it also did not re-fire the
  # task, it just spoke `retry_say` and ended the turn. That is a stall, not a retry: the
  # copy is "Let me check your file again", which invites no answer, so the caller has no
  # reason to speak and the flow only resumes if they happen to say something anyway. The
  # task stays fire-eligible (a failed result is falsy under success_check, so the
  # selector still picks it), which is the proof the intent was always to run it again —
  # the only question was WHEN, and one wasted caller turn per attempt is the wrong answer
  # on a phone call.
  #
  # Fire it on THIS turn, the way the ordinary fire branch does: same message, same tool,
  # same args, with the tool un-hidden so the model can actually call it.
  #
  # Bounded by construction: this branch is only reachable while retries < max_retries and
  # every re-fire increments the counter, so `max_retries: 2` re-fires at most once and
  # then exhausts. That bound is load-bearing — an unbounded version is an SMS-send
  # primitive driven by an unverified caller.
  #
  # Two opt-outs, both meaning "the retry needs the CALLER, not the backend":
  #   * `clear_slots` — the retry just dropped slots so they can be re-collected, so the
  #     next thing that must happen is a question, not another identical call.
  #   * `refire: False` — explicit, mirroring `on_exhaust.escalate: False`.
  retry_tool = task_def.get("tool")
  if (retry_tool and on_failure.get("refire", True)
      and not clear_slots):
    retry_result["function_call"] = {
        "name": retry_tool,
        "args": _remote_wire_args(
            config, retry_tool,
            _task_input_args(task_def.get("inputs") or [], filled)),
    }
    retry_result["hide_tools"] = [t for t in hide_tools if t != retry_tool]
    _log("task_retry_refire", name=task_just, tool=retry_tool,
         attempt=retries[task_just], max_retries=max_retries)
  return retry_result, "", None


def _build_readback_hint(
    slots, pending: dict[str, Any], filled: dict[str, Any],
    fresh_pending: bool, promoted_from_deferred: bool = False,
) -> str:
  """Build readback hint for system instruction."""
  if not fresh_pending or not pending:
    return ""
  merged_state = {**filled, **pending}
  hint_parts = []
  for slot_def in slots:
    name = slot_def["name"]
    if name not in pending:
      continue
    if not _is_slot_active_ignoring_self(slot_def, name, merged_state):
      continue
    formatter = _resolve_formatter(slot_def.get("readback_fmt"))
    val = formatter(pending[name]) if formatter else str(pending[name])
    if promoted_from_deferred:
      hint_parts.append(f"{name}: {val}")
    else:
      hint_parts.append(val)
  if not hint_parts:
    return ""
  if promoted_from_deferred:
    return "\n".join(f"  - {p}" for p in hint_parts)
  return ", ".join(hint_parts)


# ═════════════════════════════════════════════════════════════════════
# DAG EVALUATION
# ═════════════════════════════════════════════════════════════════════


def _build_readback(slots, pending, filled, config=None, channel=""):
  """Build readback confirmation prompt."""
  merged_state = {**filled, **pending}
  fragments = []
  for slot_def in slots:
    name = slot_def["name"]
    if name not in pending:
      continue
    if not _is_slot_active_ignoring_self(slot_def, name, merged_state):
      continue
    formatter = _resolve_formatter(slot_def.get("readback_fmt"))
    if formatter:
      fragments.append(formatter(pending[name]))
    else:
      fragments.append(f"{name}: {pending[name]}")
  if not fragments:
    return None
  summary = ", ".join(fragments)
  result = {
      "action": "awaiting_readback",
      "system_message": f"Just to confirm — {summary}. Is that correct?",
  }
  if config:
    resp = _resolve_response(
        config, "readback_response", {**filled, **pending}, channel,
    )
    if resp:
      result["response"] = resp
  return result


def _split_fallback(key):
  """``"app_name|that app"`` -> ``("app_name", "that app")``; no pipe -> ``(key, None)``.

  The inline-fallback form for a template placeholder. ``str.format_map`` hands the whole
  field name to the mapping and ``|`` is not special to the format mini-language, so the
  fallback rides through untouched and is resolved here.
  """
  if not isinstance(key, str) or "|" not in key:
    return key, None
  name, _, fallback = key.partition("|")
  return name.strip(), fallback


class _SafeFmt(dict):
  """A format mapping that leaves unknown {placeholders} literal (never KeyErrors).

  Also resolves the inline fallback form ``{slot|some words}``: the slot's value when it
  has one, else the literal text after the pipe. A merely cosmetic reference ("is it only
  {app_name|that app} that's not working?") should not leak braces at the caller nor cost
  the whole line — while a load-bearing value authored WITHOUT a fallback still renders
  literal, so the terminal value-gate keeps catching it.
  """

  def __missing__(self, key):
    name, fallback = _split_fallback(key)
    if fallback is not None:
      value = dict.get(self, name)
      return value if value not in (None, "") else fallback
    return "{" + key + "}"


def _safe_format(template, values):
  """Render {slot} placeholders from `values`, PARTIALLY and crash-safe: known slots
  substitute, an unfilled/unknown placeholder stays the literal "{name}", and malformed
  braces are left untouched. Used for ask/hint so an unresolved reference degrades to
  text instead of raising or dropping the prompt."""
  if not isinstance(template, str) or "{" not in template:
    return template
  try:
    return template.format_map(_SafeFmt(values or {}))
  except (ValueError, IndexError, TypeError, KeyError, AttributeError):
    # AttributeError too: `__missing__` only covers an UNKNOWN key, so a dotted
    # reference to a known slot ("{order.id}" over a dict) resolves the root and
    # then raises out of the engine. Every caller here — ask/hint, then_say,
    # announce, _substitute_response — degrades to text instead.
    return template


def _template_missing_field(template, values):
  """True if `template` interpolates a {field} that is absent or None/"" in `values` — i.e. the template
  announces a value the producer did not supply. Used to value-gate a completion `then_say` so a terminal
  never speaks a false "done, confirmation {X}" when X wasn't produced. Root field only (dotted/indexed
  references degrade to their root). Malformed templates ⇒ False (treated as safe, formatted as-is)."""
  if not isinstance(template, str) or "{" not in template:
    return False
  values = values or {}
  try:
    for _lit, field, _spec, _conv in string.Formatter().parse(template):
      if not field:
        continue
      root, fallback = _split_fallback(field.split(".")[0].split("[")[0].strip())
      if fallback is not None:    # {slot|words} always renders — never a missing field
        continue
      if root == "" or root.isdigit():          # positional {}/{0} — not a named value we can check
        continue
      if root not in values or values.get(root) in (None, ""):
        return True
  except Exception:
    return False
  return False


def _restage_ask(sm, name, ask):
  """Point the staged rung at the wording that will ACTUALLY be said.

  `_slot_ask_template` can only know the template. The commit test is a substring of
  the spoken turn, so a rung carrying a placeholder ("Ask about {topic}?") never
  matched the resolved line ("Ask about billing?") and was never spent — the ladder
  froze on rung one forever, which is worse than the skip the staging exists to stop.
  A surface `ask_variant` replaces the wording outright and would miss the same way.

  Every site that resolves a question calls this, so adding one is a one-line change
  rather than a silent regression. `_commit_ask_rung` also re-resolves as a backstop.
  """
  staged = (sm or {}).get("_ask_rung_staged")
  if staged and staged.get("slot") == name:
    staged["text"] = ask


def _slot_ask_template(slot_def, sm=None):
  """The un-formatted question template for a slot (§R2.2).

  For a repeated slot mid-collection (its accumulator already holds >=1 element),
  return `repeated.ask_more` (elements 2..N) instead of `ask` (element 1); falls
  back to `ask` when `ask_more` is absent or `sm` is unavailable. Non-repeated slots
  return `ask` unchanged, so the existing behavior is preserved verbatim.
  """
  name = slot_def["name"]
  ask = slot_def.get("ask", f"Please provide {name}.")
  repeated = slot_def.get("repeated")
  if repeated and sm is not None:
    acc = (sm.get("_repeat_acc") or {}).get(name, [])
    if acc:
      ask = repeated.get("ask_more", ask)
  return _ask_rung(name, ask, sm)


def _ask_rung(name, ask, sm):
  """Pick this turn's rung when `ask` is a LADDER (a list), else return it unchanged.

  A question the caller does not answer is re-asked. Asked as one fixed string it is
  re-asked WORD FOR WORD, which reads as the agent not listening — and for a terminal
  "anything else?" question, which is the last open slot and therefore the flow's idle
  prompt, it repeats for the rest of the call.

  `reprompts` does not cover this: it is indexed by the validation-retry count and only
  fires when a value was offered and rejected. A caller who says something the flow
  cannot use at all produces no error, no retry, and no new wording.

  The rung is indexed by how many times this slot has already been asked and CLAMPS to
  the last entry, so the ladder degrades to a fixed question rather than running out.
  Same shape as a control block's `declined_say`.

  Args:
    name: Slot name (the ladder's counter key).
    ask: The question — a string (returned as-is) or a list of rungs.
    sm: Slot machine state; None disables laddering (returns the first rung).

  Returns:
    The question template for this turn.
  """
  if not isinstance(ask, list):
    return ask
  if not ask:                      # authoring error; the validator rejects it
    return ""
  if sm is None:
    return ask[0]
  # STAGED, not spent. Deriving the next question is not the same as asking it: a turn
  # can work out that this slot is next and then never say so, because an announce takes
  # the turn or a condition that silences the slot flips later in the same pass. Spending
  # the rung here burned one on every such turn, so a caller who had heard rung one got
  # rung THREE next — the anti-loop menu, arriving as if from nowhere, at someone who had
  # answered everything asked of them.
  #
  # `_commit_ask_rung` spends it on the way out, and only if the wording actually reached
  # the caller. Within a turn this stays pure, so every pass resolves to the same rung.
  rungs = sm.setdefault("_ask_rung", {})
  spent_on = sm.setdefault("_ask_rung_turn", {})
  turn = sm.get("_turn_n")
  if spent_on.get(name) == turn:
    idx = rungs.get(name, 0)          # already spent this turn — hold the same rung
  else:
    idx = rungs.get(name, -1) + 1
  idx = min(idx, len(ask) - 1)
  sm["_ask_rung_staged"] = {"slot": name, "index": idx, "text": ask[idx], "turn": turn}
  return ask[idx]


def _commit_ask_rung(result):
  """Spend a staged ask rung, but only if its wording actually reached the caller.

  Called from the engine's single exit wrapper, so no return path can skip it. The test
  is deliberately the SPOKEN OUTPUT rather than the engine's intent: whether the caller
  heard this rung is the only thing that should decide if the next one is due.
  """
  sm = result.get("sm") if isinstance(result, dict) else None
  if not isinstance(sm, dict):
    return result
  staged = sm.pop("_ask_rung_staged", None)
  if not staged:
    return result
  spent_on = sm.setdefault("_ask_rung_turn", {})
  if spent_on.get(staged["slot"]) == staged["turn"]:
    return result                     # one rung per turn, however many passes it took
  action = result.get("action") or {}
  spoken = [action.get("message") or ""]
  for part in action.get("response") or []:
    if isinstance(part, dict) and part.get("type") == "text":
      spoken.append(part.get("text") or "")
  # Resolved again here as a backstop: if some path stages a raw template and never
  # re-stages, this still matches the spoken line rather than freezing the ladder.
  wanted = _safe_format(staged["text"], sm.get("filled", {}))
  if wanted and wanted in " ".join(spoken):
    sm.setdefault("_ask_rung", {})[staged["slot"]] = staged["index"]
    spent_on[staged["slot"]] = staged["turn"]
  return result


def _find_next_question(
    slots, filled, pending, slot_map, deferred=None,
    channel="", sm=None,
):
  """Find the next unfilled user slot to ask about."""
  deferred = deferred or {}
  merged_state = {**filled, **pending, **deferred}
  for slot_def in slots:
    name = slot_def["name"]
    if slot_def.get("passive"):
      continue
    if not _is_slot_active(slot_def, merged_state):
      continue
    if name in filled or name in pending or name in deferred:
      continue
    if "user" not in _normalize_sources(slot_def.get("source", "user")):
      continue
    requires = slot_def.get("requires", [])
    if not all(
        req in filled
        or not _is_slot_active(slot_map[req], merged_state)
        for req in requires
    ):
      continue
    ask_template = _slot_ask_template(slot_def, sm)
    ask = _safe_format(ask_template, filled)
    # A surface-specific wording of the question wins over the floor. Already
    # placeholder-substituted by _resolve_response, so it is not re-formatted.
    ask_variant = _resolve_variant_text(slot_def, "ask", filled, channel)
    if ask_variant:
      ask = ask_variant
    _restage_ask(sm, name, ask)
    result = {
        "action": "next_question",
        "system_message": ask,
        "slot_name": name,
    }
    response = _resolve_response(slot_def, "response", filled, channel)
    if response:
      result["response"] = response
    return result
  return {
      "action": "all_done",
      "system_message": "All information collected!",
  }


def _reask_question_payload(slots, filled, pending, slot_map, channel,
                            exclude=None, sm=None):
  """Question `response` parts for the slot that a deterministic / model-driven
  re-ask is about to re-ask, or None.

  Several re-ask paths emit the question text WITHOUT going through
  `_route_payloads` (the normal place a next-question's chips are stashed): the
  reject re-ask (the model re-asks in the same turn the rejected slot is still
  pending), the hard steer-back deterministic re-ask, and the empty-render
  backstop. Each of those must still re-surface the slot's chips. This computes
  them by finding the next open user question as if `exclude` (e.g. the rejected
  pending slots) were not pending, and returning that slot's response parts.

  Args:
    slots: Ordered slot definitions.
    filled: Filled slot values.
    pending: Pending slot values.
    slot_map: Slot-name -> definition map.
    channel: Channel for channel overrides.
    exclude: Optional iterable of slot names to treat as not-pending (the slots
      being rejected/re-collected this turn).

  Returns:
    The question response parts (list), or None if the next question has none.
  """
  effective_pending = {k: v for k, v in pending.items()
                       if k not in set(exclude or ())}
  next_q = _find_next_question(
      slots, filled, effective_pending, slot_map, channel=channel, sm=sm,
  )
  return next_q.get("response")


def _partial_group_collect_slot(
    slots, filled, pending, deferred, retries, slot_map,
):
  """Name of a rejected-and-unfilled slot to collect before readback, or None.

  When some slot is `pending` (which would trigger readback) but a required slot
  was REJECTED (failed validation, has a recorded retry) and is still unfilled,
  reading the pending value back strands the guest's retry: readback hides every
  value setter (corrections route through the correction tool) and the rejected
  slot is not surfaced in <provided_details> (it is in neither filled nor
  pending), so when the guest re-provides it the model has no usable tool and
  returns an empty response (the platform then emits its "having trouble"
  fallback). Stay in collection for the rejected slot (setter visible) until it
  is collected, then read everything back together.

  The retry gate is the precision: ordinary incremental collection (one field
  given, another simply not provided yet — no retry) reads back normally; only a
  validation REJECTION of a still-collectable slot defers readback. This is
  independent of how slots map to setters (one or many slots per tool) — the
  trigger is "a rejected, collectable slot coexists with a pending slot".
  """
  deferred = deferred or {}
  retries = retries or {}
  if not pending:
    return None
  merged = {**filled, **pending, **deferred}
  for slot_def in slots:
    name = slot_def["name"]
    if slot_def.get("passive"):
      continue
    if name in filled or name in pending or name in deferred:
      continue
    if "user" not in _normalize_sources(slot_def.get("source", "user")):
      continue
    if retries.get(f"slot:{name}", 0) <= 0:
      continue
    if not _is_slot_active(slot_def, merged):
      continue
    if not all(
        r in filled or not _is_slot_active(slot_map[r], merged)
        for r in slot_def.get("requires", [])
    ):
      continue
    return name
  return None


def _find_next_slot_action(
    slots, filled, pending, slot_map, deferred=None,
    channel="", sm=None,
):
  """Find the next slot action (announce or user question).

  Walks slots in declaration order. Returns the first
  eligible announce or user slot. Announce slots are filled
  by the framework; user slots produce a question prompt.

  Args:
    slots: Ordered list of slot definitions.
    filled: Dict of filled slot values.
    pending: Dict of pending slot values.
    slot_map: Dict mapping slot name to slot definition.
    deferred: Optional dict of deferred slot values.
    channel: Optional channel for channel-specific responses.

  Returns:
    Action dict with 'action' key ('announce',
    'next_question', or 'all_done').
  """
  deferred = deferred or {}
  merged_state = {**filled, **pending, **deferred}
  for slot_def in slots:
    name = slot_def["name"]
    if slot_def.get("passive"):
      continue
    if not _is_slot_active(slot_def, merged_state):
      continue
    if name in filled or name in pending or name in deferred:
      continue
    sources = _normalize_sources(
        slot_def.get("source", "user"),
    )
    requires = slot_def.get("requires", [])
    if not all(
        req in filled
        or not _is_slot_active(slot_map[req], merged_state)
        for req in requires
    ):
      continue
    if "announce" in sources:
      return {
          "action": "announce",
          "slot_def": slot_def,
      }
    if "user" in sources:
      ask = _safe_format(_slot_ask_template(slot_def, sm), filled)
      # Surface-specific wording wins over the floor (see _resolve_variant_text).
      ask_variant = _resolve_variant_text(slot_def, "ask", filled, channel)
      if ask_variant:
        ask = ask_variant
      _restage_ask(sm, slot_def["name"], ask)
      result = {
          "action": "next_question",
          "system_message": ask,
          "slot_name": name,
      }
      response = _resolve_response(slot_def, "response", filled, channel)
      if response:
        result["response"] = response
      return result
  return {
      "action": "all_done",
      "system_message": "All information collected!",
  }


def _compute_dag_state(
    tasks, slots, filled, pending, task_results, slot_map,
    deferred=None, channel="", config=None,
    skip_partial_readback=False, sm=None,
):
  """Evaluate the DAG to determine the next action.

  Announce slots (exit branches) are checked before tasks so that
  exit conditions short-circuit before any tasks fire.  Task firing
  is checked before readback so that unrelated pending items
  (e.g. promoted from a deferred group) don't block a task whose
  inputs are all in filled.

  Args:
    tasks: List of task definition dicts.
    slots: List of slot definition dicts.
    filled: Currently filled slot values.
    pending: Currently pending slot values.
    task_results: Dict of task name to result.
    slot_map: Dict mapping slot name to slot definition.
    deferred: Currently deferred slot values.
    channel: Channel identifier for channel-aware responses.
    config: Full compiled config (for readback_response).
    skip_partial_readback: When True, suppress the partial-group readback so a
      rejected sibling keeps collecting (the engine sets this during a focused
      re-collection pass).

  Returns:
    Action dict describing the next step (fire, next_question, etc.).
  """
  deferred = deferred or {}
  merged_state = {**filled, **pending, **deferred}

  # `<group>_done` is what an all-done line waits on, and it means every leg REPORTED --
  # not that every leg succeeded. A failed leg still reported, so a group whose billing
  # lookup fell over is still done; that is what lets the group speak at all instead of
  # hanging on one flaky backend. Recomputed every pass rather than written once at the
  # batch, so a straggler leg that lands on its own -- an asynchronous completion a turn
  # later -- closes the group too. Idempotent: it only ever fills.
  for _group, _legs in _parallel_groups(tasks).items():
    _done_key = f"{_group}_done"
    if _done_key in filled:
      continue
    _awaiting = (sm or {}).get("_awaiting_async") or {}
    # A group that has never dispatched is not done, it is NOT STARTED. The `all()`
    # below is vacuously true when every leg is gated off, and every leg being gated off
    # is the ordinary state before whatever seeds their gate has run — so without this
    # the group closes, and `_done_key` latches, before it ever existed.
    #
    # Found live (ces-probes 86): two legs each gated on the slot they themselves fill,
    # seeded by a preceding task. On the opening turn — before that task had run, before
    # the caller had even given an account number — the flow announced "both checks are
    # done" having run neither, and never recovered. It is also what made a real agent's
    # fan-out look like a publication failure: the group was already closed by the time
    # its legs became eligible, so they dispatched into bookkeeping that no longer
    # existed, re-fired, and each re-fire's `state_writes` pop deleted what the last one
    # published.
    if not any(leg["name"] in task_results or leg["name"] in _awaiting
               for leg in _legs):
      continue
    # A deferred leg HAS a task_results entry the moment it is dispatched — the
    # platform's "pending" placeholder — so presence alone is not reporting. Found live:
    # the all-done line closed the group while the line test was still running.
    if all((leg["name"] in task_results
            and not _is_async_pending(task_results.get(leg["name"]) or {})
            and leg["name"] not in _awaiting)
           or not _is_task_active(leg, merged_state)
           for leg in _legs):
      filled[_done_key] = True
      merged_state[_done_key] = True

  # Slots an OUTSTANDING async task is going to overwrite. Their current value is stale
  # by construction — a status slot seeded "pending" for the backend to resolve is the
  # ordinary case — so anything that consumes one must wait for the real answer. The
  # in-flight guard below only stops the awaiting task itself from re-firing; a consumer
  # is a different task, is perfectly eligible on the old value, and would act on a
  # verdict that is about to change. Kept as a SET rather than a rebind of `filled` so
  # slot collection is untouched: an unrelated question still gets asked during the wait,
  # which is the whole point of the primitive.
  stale = set()
  for task in tasks:
    if task["name"] in ((sm or {}).get("_awaiting_async") or {}):
      stale.update(_output_targets(task.get("outputs")))

  for slot_def in slots:
    name = slot_def["name"]
    if not _is_slot_active(slot_def, merged_state):
      continue
    if name in filled or name in pending or name in deferred:
      continue
    sources = _normalize_sources(slot_def.get("source", "user"))
    if "announce" not in sources:
      continue
    requires = slot_def.get("requires", [])
    if stale.intersection(requires):
      continue  # would announce a value the pending result is about to replace
    if not all(
        req in filled
        or not _is_slot_active(slot_map[req], merged_state)
        for req in requires
    ):
      continue
    return {
        "action": "announce",
        "slot_def": slot_def,
    }

  def _task_fireable(task):
    """Whether this task could be dispatched on this pass.

    Extracted so the parallel-group sweep below admits a sibling on exactly the terms
    the primary scan would have admitted it on. Two copies of this predicate would
    drift, and a leg admitted on looser terms fires with an unfilled input.
    """
    task_name = task["name"]
    success_key = task.get("success_check", "success")

    if (task_name in task_results
        and task_results[task_name].get(success_key)):
      return False

    # An ASYNCHRONOUS tool is still working. Its recorded result is the platform's
    # "pending" placeholder, which is falsy under success_check, so the skip above does
    # NOT catch it — without this the engine re-dispatches the tool on every turn for as
    # long as the backend takes.
    if task_name in ((sm or {}).get("_awaiting_async") or {}):
      return False

    # Synchronous analogue of _awaiting_async: a sync fire already dispatched this turn
    # whose result has not landed yet. Without it an input-free task re-fires every pass
    # to the 10-loop cap on the empty-contents entry turn (#698). Released when the
    # result lands, and at the turn boundary for a fire whose result never did.
    if task_name in ((sm or {}).get("_sync_fire_pending") or {}):
      return False

    # Don't let an input-free/no-requires executor jump ahead of the caller: while an
    # askable user slot is still unfilled and the caller has spoken, it would preempt the
    # model's setter and drop the answer (#698). It fires on the quiet post-setter pass.
    #
    # UNLESS the turn's utterance was already consumed deterministically. An option_cue
    # fill records `_event_prefilled_this_turn` (see `_apply_option_cues`), and the
    # terminal deferral at the fire site already reads it with exactly this reasoning:
    # there is then no unread user intent left for a setter to preserve, so holding the
    # task buys nothing and costs the caller an answer to the thing they just said. It
    # costs more than one answer, in fact — a deterministic fill produces no post-setter
    # re-invoke to release the hold on, so the task is unreachable for as long as the
    # pending question stands, and the pending question is re-asked verbatim with no
    # retry counter advancing (a cue turn reports no setter error).
    if (not task.get("inputs") and not task.get("requires")
        and not task.get("terminal")
        and not (sm or {}).get("_event_prefilled_this_turn")):
      _utterance = str((sm or {}).get("_turn_user_text") or "").strip()
      if _utterance and any(
          (sd or {}).get("source") == "user" and sd.get("ask") and s not in filled
          and _is_slot_active(sd, merged_state)
          for s, sd in slot_map.items()):
        return False

    # A fan-out leg whose group was written off after too many empty watch windows. Its
    # recorded failure is also falsy, so without this it is fire-eligible again and the
    # group that just spent a minute producing nothing starts over.
    if task_name in ((sm or {}).get("_fanout_written_off") or []):
      return False

    # A task that exhausted its ladder and resolved its output with `fill`. Same reason
    # as the two above: the recorded failure is falsy, so without this it is eligible
    # again and the flow loops on it forever (ces-probes 114).
    if task_name in ((sm or {}).get("_task_written_off") or []):
      return False

    if not _is_task_active(task, merged_state):
      return False

    all_inputs = _task_input_slots(task["inputs"])
    # A consumer of a slot the outstanding task will overwrite has to wait for the real
    # value; firing on the stale one produces a confident answer to the wrong question.
    if stale.intersection(all_inputs) or stale.intersection(task.get("requires", [])):
      _log("async_await_consumer_held", task=task_name,
           stale=sorted(stale.intersection(set(all_inputs)
                                           | set(task.get("requires", [])))))
      return False
    active_inputs = [
        s for s in all_inputs
        if _is_slot_active_ignoring_self(slot_map[s], s, merged_state)
    ]
    if all_inputs and not active_inputs:
      return False
    if not all(s in filled for s in active_inputs):
      return False
    task_reqs = [
        r for r in task.get("requires", [])
        if _is_slot_active_ignoring_self(slot_map[r], r, merged_state)
    ]
    if not all(r in filled for r in task_reqs):
      return False
    return True

  for task in tasks:
    if not _task_fireable(task):
      continue

    group = task.get("parallel")
    if not group:
      return {
          "action": "fire",
          "task_name": task["name"],
          "task_def": task,
      }

    # A fan-out group: sweep up every SIBLING that is fireable right now and hand the
    # whole set to the fire branch, which dispatches them in a single action. The
    # runtime then runs them concurrently, so the group costs the caller its slowest
    # leg rather than the sum (ces-probes 33: three four-second legs cost four seconds).
    #
    # This is a batching hint, not a barrier. A sibling gated off by its condition,
    # already complete, or awaiting an async result is simply not in this pass's set,
    # and fires on a later pass — possibly alone. Holding the group until every leg is
    # ready would reintroduce the wedge `verdict()` refuses spine `requires` to avoid:
    # one leg that never becomes ready would stall all of them forever.
    legs = [task] + [
        other for other in tasks
        if other is not task
        and other.get("parallel") == group
        and _task_fireable(other)
    ]
    return {
        "action": "fire",
        "task_name": task["name"],
        "task_def": task,
        "parallel": group,
        "group_tasks": legs,
    }

  if pending and not skip_partial_readback:
    rb = _build_readback(slots, pending, filled, config=config, channel=channel)
    if rb is not None:
      return rb

  return _find_next_slot_action(
      slots, filled, pending, slot_map, deferred=deferred,
      channel=channel, sm=sm,
  )


def _progress_sig(sm):
  """A cheap signature of "how far the conversation has got".

  Compared across turns to decide whether a turn accomplished ANYTHING. Counts, not
  contents, because we only need change detection and this runs every turn.
  """
  return (
      len(sm.get("filled") or {}),
      len(sm.get("pending") or {}),
      len(sm.get("deferred") or {}),
      len(sm.get("task_results") or {}),
      sm.get("status", "in_progress"),
      bool(sm.get("_call_stack")),
  )


def _no_match_tick(sm, slots, last_user_text):
  """The "answered, but nothing resolved" ladder — `validation.on_exhaust.fill`.

  A slot the caller ANSWERS but whose answer resolves to nothing has no ladder today:
  `_handle_slot_errors` only advances when a setter REPORTS an error, so a turn where the
  model calls nothing never counts as a retry, and an intent slot re-asks forever. This
  is that missing counter, and on exhaust it resolves the slot with the authored value so
  the flow can carry on rather than escalating out.

  A turn only counts against the awaited slot when it was BARREN: fresh user text arrived
  and nothing progressed ANYWHERE. That distinction is what makes this safe now that many
  tools are live at once — the caller answering a different question, triggering an FAQ
  announce, or correcting an earlier value are all productive turns, and charging them
  against the pending question would silently default someone who is engaging perfectly
  well. Each turn therefore lands in exactly one ladder (no_input / validation /
  steer_back / here), so no two counters can move at once.

  Returns `(slot_name, value, say)` when it fired this turn, else None.
  """
  awaited = sm.get("_awaiting")
  mark = sm.get("_await_mark") or {}
  if not awaited or mark.get("slot") != awaited:
    return None
  # Silence is no_input's; a reported bad value is validation's; and a caller who asked
  # for time is no_input's too. The hold is resolved just before these ladders run
  # precisely so they can see it: "hold on" is fresh text that progresses nothing, which
  # is indistinguishable here from an answer that resolved to nothing, and charging it
  # spends the caller's retries while they are still looking for the value.
  if (not (last_user_text or "").strip() or sm.get("_slot_errors")
      or sm.get("_hold_on")):
    return None
  turn_n = sm.get("_turn_n", 0)
  if turn_n <= mark.get("turn", -1):
    return None                      # same user turn — the engine re-invoking itself

  slot_def = next((s for s in slots if s.get("name") == awaited), None)
  if (slot_def or {}).get("push_back"):
    return None  # a `push_back` slot is owned by _push_back_tick, not this ladder
  validation = (slot_def or {}).get("validation") or {}
  exhaust = validation.get("on_exhaust") or {}
  fill_value = exhaust.get("fill")

  if _progress_sig(sm) != tuple(mark.get("sig") or ()):
    # The turn moved the conversation on. Not a failure to answer — reset.
    sm.get("_no_match", {}).pop(awaited, None)
    sm["_await_mark"] = {**mark, "turn": turn_n, "sig": _progress_sig(sm)}
    return None
  if fill_value is None:
    return None                      # nothing authored to fall back to

  counter = sm.setdefault("_no_match", {})
  counter[awaited] = counter.get(awaited, 0) + 1
  sm["_await_mark"] = {**mark, "turn": turn_n}
  attempts = validation.get("max_retries", 2)
  _log("slot_no_match", slot=awaited, n=counter[awaited], of=attempts)
  if counter[awaited] < attempts:
    return None

  # Exhausted. Resolve the slot DIRECTLY — the setter is deliberately not re-entered:
  # the value is authored and enum-checked at build time, so re-validating it can only
  # fail spuriously, and there is no model tool call to route it through.
  sm.setdefault("filled", {})[awaited] = fill_value
  sm.get("pending", {}).pop(awaited, None)
  sm.get("_retries", {}).pop(f"slot:{awaited}", None)
  counter.pop(awaited, None)
  sm.pop("_awaiting", None)
  sm.pop("_await_mark", None)
  _log("slot_no_match_filled", slot=awaited, value=fill_value)
  return awaited, fill_value, exhaust.get("say") or ""


def _push_back_tick(sm, slots, last_user_text, filled):
  """The `push_back` re-offer ladder.

  A slot may declare ``push_back = {reprompts, max, say, then, fill, end_conversation,
  verbatim}`` — the counter-driven answer to "the caller keeps answering the awaited slot
  with something that does not fill it" (declines an offer, insists, off-cue). For pushes
  1..``max`` it RE-OFFERS ``reprompts[k]`` as a PREEMPT (so the model cannot improvise this
  turn); on push ``max``+1 it DISPOSES via on_exhaust — a ``fill`` (resolve the slot so the
  cascade continues) and/or a ``then`` control tool, optionally ending the leg.

  This is the general form of the off-cue ladder a CUE-ONLY slot otherwise lacks (no setter
  → no `_handle_slot_errors` reprompt path, so the model would answer the turn itself and
  drift off-script). Distinct from `no_input` (silence), `validation` (a rejected value),
  and `steer_back` (a stalled turn) — those own their own turns; this owns "the caller
  keeps pushing back on the offer".

  Returns None (no push this turn); ("reprompt", msg, verbatim); ("dispose", say, fc, end,
  verbatim); or ("fill", value). The caller renders reprompt/dispose as preempts and lets a
  ``fill`` fall through so the dispose task/announce cascade fires this turn.
  """
  awaited = sm.get("_awaiting")
  mark = sm.get("_await_mark") or {}
  if not awaited or mark.get("slot") != awaited:
    return None
  # Silence is no_input's; a reported bad value is validation's; and a caller who asked
  # for time is no_input's too. The hold is resolved just before these ladders run
  # precisely so they can see it: "hold on" is fresh text that progresses nothing, which
  # is indistinguishable here from an answer that resolved to nothing, and charging it
  # spends the caller's retries while they are still looking for the value.
  if (not (last_user_text or "").strip() or sm.get("_slot_errors")
      or sm.get("_hold_on")):
    return None
  turn_n = sm.get("_turn_n", 0)
  if turn_n <= mark.get("turn", -1):
    return None                      # same user turn — the engine re-invoking itself
  slot_def = next((s for s in slots if s.get("name") == awaited), None)
  pb = (slot_def or {}).get("push_back")
  if not pb:
    return None
  if _progress_sig(sm) != tuple(mark.get("sig") or ()):
    # The turn moved the conversation on — not a push-back. Reset the counter.
    sm.get("_push_back", {}).pop(awaited, None)
    sm["_await_mark"] = {**mark, "turn": turn_n, "sig": _progress_sig(sm)}
    return None
  counter = sm.setdefault("_push_back", {})
  counter[awaited] = counter.get(awaited, 0) + 1
  sm["_await_mark"] = {**mark, "turn": turn_n}
  k = counter[awaited]
  reprompts = pb.get("reprompts") or []
  max_pushes = pb.get("max", len(reprompts))
  verbatim = bool(pb.get("verbatim") or (slot_def or {}).get("verbatim"))
  _log("push_back", slot=awaited, n=k, of=max_pushes)
  if k <= max_pushes and reprompts:
    idx = min(k - 1, len(reprompts) - 1)
    return ("reprompt", reprompts[idx], verbatim)
  # Exhausted -> dispose.
  counter.pop(awaited, None)
  sm.pop("_awaiting", None)
  sm.pop("_await_mark", None)
  sm.get("pending", {}).pop(awaited, None)
  sm.get("_retries", {}).pop(f"slot:{awaited}", None)
  fill_value = pb.get("fill")
  if fill_value is not None:
    # Resolve the slot with the authored value and let the cascade continue (the dispose
    # task/announce keyed on this value fires this turn). The setter is deliberately not
    # re-entered — the value is authored, so there is no model tool call to route it
    # through — the same contract `_no_match_tick`'s exhaust-fill uses.
    sm.setdefault("filled", {})[awaited] = fill_value
    _log("push_back_fill", slot=awaited, value=fill_value)
    return ("fill", fill_value)
  say = pb.get("say") or ""
  try:
    say = say.format(**filled)
  except (KeyError, IndexError):
    pass
  fc = _resolve_exhaust_action(pb, filled)
  _log("push_back_dispose", slot=awaited, fired=bool(fc),
       end=bool(pb.get("end_conversation")))
  return ("dispose", say, fc, bool(pb.get("end_conversation")), verbatim)


def _handle_slot_errors(sm, slots, channel=""):
  """Process validation errors and manage retries.

  Args:
    sm: State machine dict (mutated in place).
    slots: List of slot definition dicts.
    channel: Optional channel for channel-specific responses.

  Returns:
    Tuple of (message, exhausted, function_call, response, verbatim), where
    `verbatim` reports that an erroring slot pins its recovery lines literal.
  """
  errors = sm.pop("_slot_errors", [])
  if not errors:
    return None, False, None, None, False

  retries = sm.setdefault("_retries", {})
  filled = sm.get("filled", {})
  messages = []
  error_response = None
  verbatim = False

  for err in errors:
    slot_name = err["slot"]
    error_code = err["code"]
    retry_key = f"slot:{slot_name}"

    slot_def = next(
        (s for s in slots if s["name"] == slot_name), None,
    )
    if not slot_def:
      continue

    validation = slot_def.get("validation", {})
    max_retries = validation.get("max_retries", 3)

    retries[retry_key] = retries.get(retry_key, 0) + 1
    _log("slot_error", "WARN", slot=slot_name,
         code=error_code, retries=retries[retry_key])

    if retries[retry_key] >= max_retries:
      exhaust = validation.get("on_exhaust", {})
      # `fill` disposition: resolve the slot with the authored value and let the flow
      # CARRY ON, instead of ending the attempt the way `then` does. Handled here as well
      # as in `_no_match_tick` so the ladder behaves the same whichever way the caller
      # failed to answer — a rejected value (this path) or an answer that resolved to
      # nothing (that one). The setter is deliberately NOT re-entered: the value is
      # authored and enum-checked at build time.
      fill_value = exhaust.get("fill")
      if fill_value is not None:
        sm.setdefault("filled", {})[slot_name] = fill_value
        sm.get("pending", {}).pop(slot_name, None)
        retries.pop(retry_key, None)
        sm.get("_no_match", {}).pop(slot_name, None)
        sm.pop("_awaiting", None)
        sm.pop("_await_mark", None)
        _log("slot_error_exhaust_filled", "WARN", slot=slot_name, value=fill_value)
        # Stash the line rather than returning it: a non-None message makes the caller
        # preempt and END the turn, and the whole point of `fill` is that the flow
        # CONTINUES. The pipeline folds this into the announce stream instead.
        sm["_exhaust_fill_say"] = _safe_format(exhaust.get("say", ""),
                                               sm.get("filled", {}))
        continue
      fc = _resolve_exhaust_action(exhaust, filled)
      if fc:
        sm["status"] = "escalated"
      msg = exhaust.get(
          "say", "An error occurred. Please call us for help.",
      )
      try:
        msg = msg.format(**filled)
      except KeyError:
        pass
      resp = _resolve_response(exhaust, "response", filled, channel)
      _log("slot_error_exhaust", "ERROR", slot=slot_name)
      return msg, True, fc, resp, bool(slot_def.get("verbatim"))

    # B4: an escalating no-match ladder — `validation.reprompts` gives a distinct
    # message per attempt (Invalid1 → Invalid2 …), indexed by the retry count and
    # clamped to the last entry, so a NLU no-match walks the CFD's MAXERROR ladder
    # before `on_exhaust`. Falls back to the per-code `errors` map when absent.
    reprompts = validation.get("reprompts")
    if isinstance(reprompts, list) and reprompts:
      msg = reprompts[min(retries[retry_key] - 1, len(reprompts) - 1)]
    else:
      # `_default` here means the same as it does on a task: the branch for a
      # reason this slot does not name. Before it existed, an unnamed code fell
      # to the built-in line below and an author could not replace it.
      msg = _resolve_by_code(validation.get("errors"),
                             {"error_code": error_code},
                             "Could you try that again?")
      if not isinstance(msg, (str, list)):
        msg = "Could you try that again?"
    # A per-CODE ladder. `reprompts` above escalates by attempt but is indexed by
    # attempt ALONE, so declaring it discards the error code: "I only caught four
    # digits" and "I didn't hear a number at all" collapse into one sentence. Those
    # are the two things a caller most needs told apart, and that specificity is the
    # whole point of the `errors` map.
    #
    # So an `errors` value may be a LIST as well as a string: same code, one rung per
    # attempt, clamped to the last entry exactly as `reprompts` is. A caller who
    # mis-reads their phone number twice hears two different, escalating sentences
    # ABOUT THE PHONE NUMBER, rather than the same one twice or two sentences that
    # have stopped mentioning it.
    #
    # Backward compatible by construction: an existing string value never enters the
    # branch. Note the failure mode this replaces is not a wrong line but a CRASH —
    # `list.format` is an AttributeError inside `before_model`, which bypasses the
    # whole slot-filling turn.
    if isinstance(msg, list):
      msg = (msg[min(retries[retry_key] - 1, len(msg) - 1)] if msg
             else "Could you try that again?")
    try:
      msg = msg.format(**filled)
    except KeyError:
      pass
    messages.append(msg)
    if slot_def.get("verbatim"):
      verbatim = True

    if not error_response:
      error_responses = validation.get("error_responses", {})
      channel_error_responses = validation.get(
          "channel_error_responses", {},
      )
      resp = (
          _resolve_by_code(channel_error_responses.get(channel),
                           {"error_code": error_code}, None)
          if channel else None
      ) or _resolve_by_code(error_responses, {"error_code": error_code}, None)
      if resp:
        error_response = _substitute_response(resp, filled)

  if not messages:
    return None, False, None, None, False

  combined = " ".join(messages)
  return combined, False, None, error_response, verbatim


def _compute_deferred_eligible(slots, tasks, task_results, slot_map):
  """Identify slots eligible for deferred confirmation."""
  eligible = set()
  for slot_def in slots:
    name = slot_def["name"]
    if not slot_def.get("requires_readback"):
      continue
    has_task_requires = False
    for req in slot_def.get("requires", []):
      req_def = slot_map.get(req)
      if req_def and any(
          s.startswith("task:")
          for s in _normalize_sources(req_def.get("source", "user"))
      ):
        has_task_requires = True
        break
    if has_task_requires:
      continue
    is_deferred_input = False
    blocked = False
    for task in tasks:
      if name not in _task_input_slots(task["inputs"]):
        continue
      if task.get("readback_inputs"):
        is_deferred_input = True
      else:
        sk = task.get("success_check", "success")
        if not (task["name"] in task_results
                and task_results[task["name"]].get(sk)):
          blocked = True
          break
    if is_deferred_input and not blocked:
      eligible.add(name)
  return eligible


def _check_deferred_groups(
    tasks, filled, pending, deferred, task_results, slot_map,
) -> list[str]:
  """Promote deferred slots when all group inputs are ready.

  Args:
    tasks: List of task definition dicts.
    filled: Currently filled slot values.
    pending: Currently pending slot values.
    deferred: Currently deferred slot values (mutated in place).
    task_results: Dict of task name to result.
    slot_map: Dict mapping slot name to slot definition.

  Returns:
    The names of slots promoted from deferred to pending.
  """
  promoted = []
  merged_state = {**filled, **pending, **deferred}
  for task_def in tasks:
    if not task_def.get("readback_inputs"):
      continue
    sk = task_def.get("success_check", "success")
    if (task_def["name"] in task_results
        and task_results[task_def["name"]].get(sk)):
      continue
    deferred_inputs = []
    all_ready = True
    for inp in _task_input_slots(task_def["inputs"]):
      sd = slot_map.get(inp)
      if not sd:
        continue
      if "user" not in _normalize_sources(sd.get("source", "user")):
        continue
      if not _is_slot_active(sd, merged_state):
        continue
      if inp in filled or inp in pending:
        continue
      if inp in deferred:
        deferred_inputs.append(inp)
      else:
        all_ready = False
        break
    if all_ready and deferred_inputs:
      for inp in deferred_inputs:
        pending[inp] = deferred.pop(inp)
        promoted.append(inp)
  return promoted


def _compute_hidden_tools(
    slots, filled, pending, readback_tools, slot_map,
    *, fresh_pending=False, executor_tools=None, deferred=None,
    correction_tool=None, expose_setters=None, answer_tools=None,
):
  """Determine which tools to hide from the LLM.

  expose_setters: setters to keep visible even during a pending/readback state
    (used to collect a rejected sibling of a partially-filled multi-setter group
    — see _partial_group_collect_slot). Without this, the shared setter would be
    hidden and the guest's retry would strand the model into an empty response.
  answer_tools: read/compute tools whitelisted by an `answer` policy. They are
    exposed ONLY on the actual answer turn (see _handle_answer, which returns its
    own hide list); on every NORMAL turn they must stay hidden so those tools are
    never a lure. Empty/None for an app with no `answer` policy, keeping its hide
    list byte-identical.
  """
  deferred = deferred or {}
  expose_setters = set(expose_setters or ())
  merged_state = {**filled, **pending, **deferred}
  hidden = []
  if pending:
    if fresh_pending:
      hidden.extend(readback_tools)
    else:
      pass
  else:
    hidden.extend(readback_tools)
  if pending:
    # Readback active: corrections AND adds route through set_slot_change, so
    # hide every value setter. The focus pass re-exposes the one named setter;
    # set_active_flow (bootstrap un-hide) and cancel (passive un-hide below) stay
    # available so the guest can still switch flows or cancel during a readback.
    # expose_setters stay visible so a rejected multi-setter sibling can still be
    # collected (the group is not fully collected, so this is not yet a readback).
    for slot_def in slots:
      if slot_def.get("setter") and slot_def["setter"] not in expose_setters:
        hidden.append(slot_def["setter"])
    # A multi-slot setter writes more than one field, so one field can be in
    # pending (under readback) while a sibling field of the same setter is still
    # unfilled. Hiding the whole setter to protect the pending field would also
    # block the user from volunteering the sibling during readback, leaving no
    # usable tool -> empty render. Keep the setter visible if it has an unfilled,
    # active, dependency-met sibling field that is NOT itself pending; the
    # volunteered value joins the readback.
    multi_setters = {}
    for slot_def in slots:
      setter = slot_def.get("setter")
      field = slot_def.get("setter_field")
      if setter and field:
        multi_setters.setdefault(setter, []).append(slot_def)
    for tool_name, slot_defs in multi_setters.items():
      if tool_name in expose_setters:
        continue
      for sd in slot_defs:
        name = sd["name"]
        if (name not in filled
            and name not in pending
            and _is_slot_active(sd, merged_state)
            and all(
                r in filled
                or not _is_slot_active(slot_map.get(r, {}), merged_state)
                for r in sd.get("requires", [])
            )):
          while tool_name in hidden:
            hidden.remove(tool_name)
          break
  else:
    for slot_def in slots:
      setter = slot_def.get("setter")
      if not setter:
        continue
      name = slot_def["name"]
      if not _is_slot_active(slot_def, merged_state):
        hidden.append(setter)
      elif name in filled:
        hidden.append(setter)
      elif name in pending and fresh_pending:
        hidden.append(setter)
      elif not all(
          r in filled
          or not _is_slot_active(slot_map.get(r, {}), merged_state)
          for r in slot_def.get("requires", [])
      ):
        hidden.append(setter)
    # Un-hide multi-setter tools that still have unfilled active slots
    multi_setters = {}
    for slot_def in slots:
      setter = slot_def.get("setter")
      field = slot_def.get("setter_field")
      if setter and field:
        multi_setters.setdefault(setter, []).append(slot_def)
    for tool_name, slot_defs in multi_setters.items():
      for sd in slot_defs:
        name = sd["name"]
        if (name not in filled
            and _is_slot_active(sd, merged_state)
            and not (name in pending and fresh_pending)
            and all(
                r in filled
                or not _is_slot_active(slot_map.get(r, {}), merged_state)
                for r in sd.get("requires", [])
            )):
          while tool_name in hidden:
            hidden.remove(tool_name)
          break

  # The passive cancel setter must always be available — the user can cancel on any
  # turn, including mid-readback. That is what this un-hide is for, and control
  # slots qualify on their own terms: no condition and no requires, so they are
  # always active and the gate below never touches them.
  #
  # It must NOT resurrect a passive slot the loop above hid because its CONDITION IS
  # FALSE. Doing so left every passive setter in the app permanently callable —
  # callable but absent from TOOL SELECTION, since that list filters on active. A
  # tool in the schema that the prompt never mentions is a lure: live, an activation
  # caller who asked to change their plan was answered with an INVENTED request to
  # consent to CPNI access (a phrase that appears nowhere in the app) and the model
  # then recorded it with `set_waiver_consent` — a fee-waiver slot from a different
  # journey, gated on a bill-explain intent that was not in play. The engine rejected
  # the value because the slot was inactive, and the caller dead-ended on "Could you
  # try that again?".
  for slot_def in slots:
    setter = slot_def.get("setter")
    if not slot_def.get("passive") or not setter:
      continue
    if not _is_slot_active(slot_def, merged_state):
      continue
    # `.get` rather than `[]`: an unresolvable `requires` leaves the setter HIDDEN
    # instead of raising. The validator rejects an unknown name, so this should be
    # unreachable — but this gate decides tool VISIBILITY, where failing closed costs
    # nothing and an exception drops the call.
    if not all(
        r in filled or not _is_slot_active(slot_map.get(r, {}), merged_state)
        for r in slot_def.get("requires", [])
    ):
      continue
    while setter in hidden:
      hidden.remove(setter)

  if executor_tools:
    hidden.extend(executor_tools)

  if correction_tool:
    # Available during any readback (pending present — the sole change/add verb
    # there) and whenever the guest has provided a value that could be changed.
    has_provided_user = any(
        s["name"] in merged_state
        for s in slots
        if "user" in _normalize_sources(s.get("source", "user"))
        and s.get("setter")
    )
    if not (pending or has_provided_user):
      hidden.append(correction_tool)

  # A Mode A repeated slot's `done_setter` (nested in repeated.until, so not a
  # slot `setter` the loops above touch) must be visible ONLY while that slot is
  # actively collecting — active, requires met, not yet complete, and not during
  # a readback. Otherwise it sits permanently exposed and the model could end
  # collection out of context (min_count 0 -> finalize an empty list early).
  for slot_def in slots:
    done_setter = (slot_def.get("repeated") or {}).get("until", {}).get(
        "done_setter")
    if not done_setter:
      continue
    name = slot_def["name"]
    collecting = (
        name not in filled
        and not pending
        and _is_slot_active(slot_def, merged_state)
        and all(
            r in filled or not _is_slot_active(slot_map.get(r, {}), merged_state)
            for r in slot_def.get("requires", [])
        )
    )
    if collecting:
      while done_setter in hidden:
        hidden.remove(done_setter)
    elif done_setter not in hidden:
      hidden.append(done_setter)

  # An `answer` policy's read/compute whitelist is exposed ONLY on the answer turn
  # (_handle_answer builds its own hide list and returns before this runs). On a
  # normal turn those tools must stay hidden — otherwise a read/compute tool the
  # answer node owns would sit callable on every DAG turn.
  for t in (answer_tools or ()):
    if t not in hidden:
      hidden.append(t)

  return hidden


# ═════════════════════════════════════════════════════════════════════
# SYSTEM INSTRUCTION SUFFIX
# ═════════════════════════════════════════════════════════════════════


def _flow_in_progress(sm):
  """True if the current flow has captured user progress worth duplicating.

  Used to reveal new_flow_instance (and its SI) only mid-flow — never on the
  fragile entry turn. Excludes the gate, the welcome announce, and shared slots
  (announce slots auto-fill and would otherwise look like progress). Pending /
  deferred / a stashed flow_state also count.
  """
  bootstrap = sm.get("_bootstrap") or {}
  exclude = {bootstrap.get("slot"), bootstrap.get("welcome_slot")}
  exclude |= set(sm.get("_shared_slots", []))
  # An active Component call frame is mid-flow by definition (design §6.9), so
  # frame-only SI reveals mid-flow and the component surface never pollutes the
  # fragile entry turn.
  if (sm.get("pending") or sm.get("deferred") or sm.get("_flow_state")
      or sm.get("_call_stack")):
    return True
  return any(k for k in sm.get("filled", {}) if k not in exclude)


_READBACK_BLOCK = """\
<readback_protocol>
After calling setter tools, the values in <readback_scope> below need
your confirmation with the user before continuing.

Read back the pending values naturally in one sentence and ask
"Is that correct?" Use digits for numbers. Then STOP — do not ask
any new questions or move to the next topic.

- "yes" → call {@TOOL: confirm_pending}
- "no" without correction → call {@TOOL: reject_pending}
- The user changes a pending value OR provides any other detail (including one
  not asked for yet) → record it: call {@TOOL: set_slot_change} with the slot
  name(s) and the system will collect the value. Always capture a volunteered
  detail — never drop it.
- Only when the input is genuinely off-topic (nothing to record) → do NOT call a
  tool; repeat the summary in words and ask "Is that correct?" again. NEVER
  return an empty reply.

Always capture new information, even alongside a yes/no.
NEVER tell the user the request is done, placed, booked, or confirmed, or give a
confirmation number, unless the system provided it THIS turn — the system
finalizes and confirms; you only relay what it gives you.
</readback_protocol>"""


def _make_collection_block(
    tool_selection: str, slot_ordering: str, prereq_note: str = "",
    bootstrap_tool: str = "", cancel_tool: str = "",
    escalate_tool: str = "", intent_changed_tool: str = "",
) -> str:
  """Build the slot collection prompt block for DAG orchestration."""
  ts = tool_selection or (
      "   (Determine the correct setter from tool names and descriptions.)"
  )
  ordering = slot_ordering or "natural order"
  prereq = prereq_note
  flow_switch = ""
  if intent_changed_tool:
    # Mid-flow, every flow-transition intent is AMBIGUOUS (switch vs separate/
    # additional 'new' instance vs going back to a paused one) and the model
    # mis-routes it inline. So the ONLY door is the intent-changed SETTER: the
    # model SETS it with its classification (like any setter — non-terminal), and
    # the framework deterministically executes the action.
    flow_switch = (
        f"\n\n5. CHANGE OF INTENT: If the user wants anything OTHER than"
        f" providing or confirming the CURRENT request's details, call"
        f" {_tool_ref(intent_changed_tool)} (a setter) with intent set to the"
        f" single best match (do NOT call flow/transfer tools directly):\n"
        f"   - intent='new_request', target=<service> — the user wants ANOTHER /"
        f" SEPARATE request. ANY 'a new X', 'another X', 'also X', 'one more X' is"
        f" new_request — it ADDS a request and keeps the current one. This is the"
        f" default whenever they say 'new'/'another'/'a' + a service.\n"
        f"   - intent='switch', target=<service> — ONLY when REPLACING/abandoning"
        f" the current request for a different service INSTEAD ('instead',"
        f" 'change this to'). If they said 'new' or 'another', it is NOT switch.\n"
        f"   - intent='resume', target=<service or empty> — go BACK to an earlier"
        f" request they set aside\n"
        f"   - intent='cancel' — stop / abandon the current request\n"
        f"   - intent='escalate' — reach a person / human / live agent\n"
        f"   - intent='correct', target=<slot name(s)> — change an already-given"
        f" detail of THIS request\n"
        f"Any 'new'/'another' is new_request, NOT switch."
    )
  elif bootstrap_tool:
    flow_switch = (
        f"\n\n5. FLOW SWITCH: If the user clearly requests a DIFFERENT"
        f" service or changes their mind, call {_tool_ref(bootstrap_tool)}"
        f" immediately with the appropriate parameters."
        f" This takes PRIORITY over filling the current slot."
    )
  cancel_rule = ""
  if cancel_tool:
    # Cancel and escalate are distinct terminal intents with distinct tools.
    # Backstops catch obvious phrasings deterministically; this states the
    # semantic intent + the edit-vs-cancel guard + the anti-false-claim rail.
    escalate_line = ""
    if escalate_tool:
      escalate_line = (
          f" To reach a human / live agent / real person, call"
          f" {_tool_ref(escalate_tool)} instead."
      )
    cancel_rule = (
        f"\n\n6. CANCEL / ESCALATE: To abandon this request, call"
        f" {_tool_ref(cancel_tool)}.{escalate_line} A single edit (changing or"
        f" removing one detail) is NOT a cancel — use the correction/setter"
        f" tools for that. NEVER tell the user the request was cancelled, or"
        f" that you are connecting them to a person, unless you actually called"
        f" the matching tool this turn — just call the tool."
    )
  return f"""\
<slot_filling_protocol>
1. CALL TOOLS FIRST: After each user message, call ALL matching setter tools
   immediately — even for invalid input (the system validates). When you call
   setters, output only the tool calls, no text.

2. NEXT STEP: The directive below names the next needed value. If the
   conversation already provides it (now or earlier), call the setter directly
   — do not re-ask. Otherwise, rephrase the directive in your own words to ask.
   Relay ONLY what the system gives you — never claim the request is done,
   placed, booked, or confirmed, and never invent a confirmation number; the
   system finalizes and confirms.

3. TOOL SELECTION:
{ts}

4. ORDERING: {ordering}. Accept info out of order.{f' {prereq}' if prereq else ''}{flow_switch}{cancel_rule}
</slot_filling_protocol>"""


# Recite-guard for model-visible directive blocks. <system_directive> and
# <steer_back> carry INSTRUCTIONS the model must ACT on (ask the next question,
# steer back), never read aloud — but unlike <task_directive> they had no guard,
# so the model parroted them verbatim (e.g. speaking the steer_back directive
# "Acknowledge briefly, then guide the caller back..."). "in your own words"
# keeps the next-question deliverable while forbidding verbatim recital.
_NO_RECITE = (
    "Respond to the caller in your own words based on the above. "
    "Do NOT recite this directive."
)


def _build_phase_suffix(sm, result):
  """Build minimal phase-specific SI suffix from engine result."""
  status = sm.get("status", "in_progress")
  if status in ("complete", "zombie", "escalated"):
    bootstrap = sm.get("_bootstrap") or {}
    if bootstrap.get("reset_on_complete"):
      # Single-flow (gate-less): no bootstrap tool exists, so the suffix must NOT
      # name one. The engine auto-seeds the gate to start the next request; the
      # model just acknowledges completion. (Gated branches below are untouched.)
      if sm.get("_single_flow"):
        return (
            "\n<post_completion>\n"
            "The previous request is complete and the guest is returning"
            " mid-session — NOT a new conversation. Do NOT repeat your"
            " welcome. Briefly acknowledge it is done and ask if there is"
            " anything else. The system starts a fresh request automatically"
            " when the guest states a new one; do NOT name or call any"
            " tool.\n"
            "</post_completion>")
      tool = bootstrap.get("tool", "")
      flow_state = sm.get("_flow_state", [])
      if flow_state and sm.get("_auto_resume_deferred"):
        target_flow = flow_state[-1]["flow"]
        saved_slots = flow_state[-1].get("slots", {})
        slot_summary = ", ".join(saved_slots.keys())
        return (
            f"\n<post_completion>\n"
            f"The current request is done. The guest also has a paused"
            f" {target_flow} request (saved: {slot_summary}).\n"
            f"Briefly acknowledge the finished request, then simply ASK whether"
            f" they'd like to continue the paused {target_flow}. Ask the"
            f" question and stop — do NOT call any tool, and do NOT restate the"
            f" saved details. The system resumes it automatically when they"
            f" confirm. Do NOT invent steps (like payment) that are not part of"
            f" the system.\n"
            f"</post_completion>"
        )
      ended = ("was cancelled"
               if sm.get("_zombie", {}).get("outcome") == "cancelled"
               else "is complete")
      return (
          f"\n<post_completion>\n"
          f"The previous request {ended} and the guest is"
          f" returning to you mid-session — this is NOT a new"
          f" conversation. Do NOT repeat your full welcome or"
          f" greeting. Briefly acknowledge that the request is"
          f" done and ask if there is anything else you can help"
          f" with.\n"
          f"If the guest states a new request, call {_tool_ref(tool)}"
          f" with the appropriate flow type. Do NOT invent steps"
          f" (like payment) that are not part of the system.\n"
          f"</post_completion>"
      )
    return ""

  si_suffix = result.get("si_suffix", "")
  msg = result.get("message", "")
  inline_confirmed = result.get("inline_confirmed", False)
  event_prefilled = result.get("event_prefilled", False)
  has_readback = "<readback_scope>" in si_suffix
  has_pending = bool(sm.get("pending"))

  # An answer turn is self-contained: hand the model ONLY the grounded free-
  # response directive — no readback/collection/steer block and no next question.
  # Consumed exactly like a soft steer-back directive (preempt: False, the model
  # composes the reply), so nothing below this point applies.
  answer_directive = result.get("answer_directive", "")
  if answer_directive:
    # Self-contained answer turn: no readback/collection/steer blocks, no next-question.
    # `_build_full_si` (the render wrapper) appends `_surface_si_block()` at its tail, so
    # return the answer block ALONE — the surface guidance is added once, by the caller
    # (matching the normal path, where `_build_phase_suffix` returns parts sans surface).
    return f"\n<answer>\n{answer_directive}\n{_NO_RECITE}\n</answer>"
  # collect_partial: a rejected sibling of a partial multi-setter group is still
  # being collected. Pending is non-empty (the validated sibling awaits readback)
  # but we must render COLLECTION — the engine suppressed <readback_scope> and
  # kept the shared setter visible, so the guest's retry has a tool to land on.
  collect_partial = result.get("collect_partial", False)

  parts = []

  if (has_readback or has_pending) and not inline_confirmed and not collect_partial:
    parts.append(_READBACK_BLOCK)
    cancel_tool = sm.get("_cancel_tool", "")
    escalate_tool = sm.get("_escalate_tool", "")
    if cancel_tool:
      esc = (f" To reach a human/live agent call {_tool_ref(escalate_tool)}."
             if escalate_tool else "")
      parts.append(
          f"\n<cancel_rule>\nCANCEL TAKES PRIORITY: if the user wants to abandon"
          f" the current task, call {_tool_ref(cancel_tool)} immediately instead"
          f" of reading back — do NOT confirm or collect more.{esc} NEVER claim"
          f" it was cancelled, or that you are connecting them, unless you"
          f" actually called the matching tool.\n</cancel_rule>"
      )
    if si_suffix:
      parts.append(si_suffix)
  else:
    steer_directive = result.get("steer_back_directive", "")

    bootstrap = sm.get("_bootstrap") or {}
    # Intent-first mode handles every flow transition through the Pass-A classifier,
    # so the in-SI flow-switch block (set_intent_changed / FLOW SWITCH) is dead
    # weight here — suppress it. The cancel/escalate rail stays (a safety rail).
    _intent_first = sm.get("_intent_first")
    # Inside a component sub-flow (a call frame is active — e.g. a repeated over/each element collecting one
    # KBA answer) flow-switching does NOT apply: the user can't start/switch/resume a top-level service
    # mid-sub-task, and the child config has no flows to switch between. The `new_request/switch/resume/
    # correct` CHANGE-OF-INTENT block is pure noise here that invites the model off-script (e.g. re-asking an
    # already-answered parent question), so suppress it — the cancel/escalate rail below still applies.
    _in_component = bool(sm.get("_call_stack"))
    _suppress_switch = _intent_first or _in_component
    parts.append(_make_collection_block(
        sm.get("_tool_selection", ""),
        sm.get("_slot_ordering", ""),
        sm.get("_prereq_note", ""),
        bootstrap_tool=("" if _suppress_switch else bootstrap.get("tool", "")),
        cancel_tool=sm.get("_cancel_tool", ""),
        escalate_tool=sm.get("_escalate_tool", ""),
        # From config, NOT hardcoded: `_compile_config` only creates the
        # `intent_changed` slot when `intent_change.tool` is set, so naming a tool here
        # unconditionally told the model to call something the app had never declared —
        # CES then warns "References to undeclared tools" on every single turn, and a
        # model that obeys calls a tool that does not exist (and which intake, having no
        # slot for it, could not record anyway).
        intent_changed_tool=(
            "" if _suppress_switch
            else (sm.get("_intent_changed_tool", "")
                  if _flow_in_progress(sm) else "")),
    ))
    if si_suffix:
      parts.append(si_suffix)

    if event_prefilled and si_suffix:
      if "<system_directive>" not in "\n".join(parts):
        parts.append(si_suffix)

    if msg and "<system_directive>" not in "\n".join(parts):
      correction_hint = sm.get("_correction_hint", "")
      corrected_slot = sm.pop("_correction_applied", None)
      correction_preamble = ""
      if corrected_slot:
        correction_preamble = (
            f"A value was just changed ({corrected_slot})."
            " Some previous answers were cleared."
            " IGNORE earlier conversation about those slots"
            " and follow the directive below exactly.\n\n"
        )
      steer_preamble = ""
      if steer_directive:
        steer_preamble = f"{steer_directive}\n\n"
        steer_directive = ""
      # Only _maybe_improvise sets this, so the style line structurally cannot
      # reach an ordinary turn. It goes BEFORE _NO_RECITE so the recite-guard
      # covers it too — an agent reading its own tone guidance aloud is the
      # exact failure _NO_RECITE was added for.
      style = result.get("improvise_style", "")
      style_line = f"\n{style}" if style else ""
      if correction_hint:
        parts.append(
            f"\n<system_directive>\n{steer_preamble}IMPORTANT: {correction_hint}"
            f"\n\n{correction_preamble}Otherwise: {msg}{style_line}"
            f"\n{_NO_RECITE}\n</system_directive>"
        )
      else:
        parts.append(
            f"\n<system_directive>\n{steer_preamble}{correction_preamble}{msg}"
            f"{style_line}\n{_NO_RECITE}\n</system_directive>"
        )

    if steer_directive:
      parts.append(f"\n<steer_back>\n{steer_directive}\n{_NO_RECITE}\n</steer_back>")

  return "\n".join(parts)


def _render_si(sm, raw_config):
  """SI for the turns before_model handles WITHOUT slot-filling.

  before_model short-circuits two turn kinds — the entry/gate turn (gate_slot
  not yet filled) and terminal turns (complete/zombie/escalated) — without
  invoking the engine's slot-filling. They still need their SI rendered from
  the shared builders, so before_model calls the engine in render-only mode and
  this returns the right block from sm state alone.
  """
  # Gate takes precedence over terminal, mirroring before_model's check order
  # (the gate short-circuit runs before the terminal one).
  gate_slot = raw_config.get("gate_slot")
  if gate_slot and not sm.get("filled", {}).get(gate_slot):
    ts_lines = []
    for slot_def in raw_config.get("slots", []):
      setter = slot_def.get("setter")
      hint = _safe_format(slot_def.get("hint", slot_def["name"]), sm.get("filled", {}))
      if setter:
        ts_lines.append(f"   - {hint} → {_tool_ref(setter)}")
    return _make_collection_block("\n".join(ts_lines), "", "") + _surface_si_block()
  return _build_phase_suffix(sm, {}) + _surface_si_block()


def _build_full_si(sm, action, init_user_text):
  """The complete phase SI before_model injects on a slot-filling turn.

  The phase suffix followed by three turn-context blocks: <task_directive> (from
  the action), <user_context> (when the init/first-turn message may carry values
  — init_user_text is the callback-only signal, passed in), and the paused-flow
  <resume_*> suffix. Each block is appended only when applicable; the order is
  fixed (phase, task, user, resume).
  """
  si = _build_phase_suffix(sm, action)
  task_directive = action.get("task_directive", "")
  if task_directive:
    si += (
        f"\n<task_directive>\n{task_directive}\n"
        "Use the tool result above to compose your response. "
        "Do NOT recite this directive.\n</task_directive>"
    )
  if init_user_text and si:
    si += (
        "\n<user_context>\n"
        "Capture EVERY value the user just provided by calling the matching "
        "setter tools now. If the message ALSO asks for something unrelated, "
        "ignore that part — the system handles it separately; do not let it "
        "crowd out the extraction. When you call setters, output only the tool "
        "calls, no text."
        "\n</user_context>"
    )
  resume_si = action.get("resume_si", "")
  if resume_si:
    si = (si or "") + resume_si
  # Appended LAST, deliberately. The `init_user_text and si` test above keys off
  # whether anything else wanted to steer this turn, and prepending would silently
  # flip that test true on every turn.
  return (si or "") + _surface_si_block()


def _surface_si_block():
  """Tell the model what surface it is writing for.

  This is the ONLY lever that reaches a model-generated turn. On a proceed action
  the engine's deterministic message is discarded and the LLM composes the reply
  itself, so no amount of authored per-surface text changes what the caller hears
  there. Without this block a voice caller gets markdown bullet lists and spoken
  URLs from an agent with no idea it is on a phone.

  Derived from capabilities rather than hardcoded per surface, so a surface nobody
  has invented yet still gets correct guidance.
  """
  if not _surface_ref:
    return ""
  caps = _surface_ref.get("caps") or {}
  tight = caps.get("brevity") == _BREVITY_TIGHT
  lines = []
  if tight:
    lines.append(
        "You are speaking out loud on a live call. Reply in one or two short"
        " sentences — the caller cannot re-read you, and cannot skim.")
  else:
    lines.append(
        "You are writing in a text conversation. Keep replies short and"
        " scannable.")
  if not caps.get("payloads", True):
    lines.append(
        "Plain speech only: no lists, no bullet points, no numbered steps, no"
        " markdown, no tables, no emoji.")
  if not caps.get("links", True):
    lines.append(
        "Never speak a URL, an email address, or anything that has to be spelled"
        " out character by character. Offer to send it instead.")
  if tight:
    lines.append(
        'Never say "click", "tap", "select below", "see the list", or otherwise'
        " refer to something the caller would have to look at.")
  else:
    lines.append(
        "Never refer to hearing, speaking, or listening — the user is reading.")
  max_options = caps.get("max_options")
  if isinstance(max_options, int) and max_options > 0:
    lines.append(
        f"Offer at most {max_options} choices at a time; if there are more, say"
        " so rather than listing them all.")
  return "\n<surface>\n" + "\n".join(lines) + "\n</surface>"


# ── Intent-first (two-pass) Pass A: classifier SI + tool hiding ──────────────


def _reachable_user_slots(slots, filled, pending, slot_map, deferred=None, sm=None,
                          include_passive_intent=False):
  """Ordered currently-reachable user slots (the 'frontier'): active condition, unfilled, user source,
  `requires` satisfied. Same predicate as _find_next_question, but returns ALL such slots (not just the
  first) so callers can reason about the whole frontier. Pure/read-only.

  `include_passive_intent=True` also includes a `passive` slot when it is `kind:"intent"` — the
  intent-CAPTURE frontier (deterministic option_cues fill) so a model-classified router intent stays
  capturable even though it is never ASKED. The default (False) is the askable frontier (skips all passive)
  and is byte-identical to before."""
  deferred = deferred or {}
  merged = {**filled, **pending, **deferred}
  out = []
  for s in slots:
    name = s["name"]
    if s.get("passive") and not (include_passive_intent and s.get("kind") == "intent"):
      continue
    if not _is_slot_active(s, merged):
      continue
    if name in filled or name in pending or name in deferred:
      continue
    if "user" not in _normalize_sources(s.get("source", "user")):
      continue
    if not all(req in filled or not _is_slot_active(slot_map[req], merged)
               for req in s.get("requires", [])):
      continue
    out.append(name)
  return out


def _intent_taxonomy(raw_config, sm=None):
  """The intent labels the model may classify a turn into, derived from config + state.

  Only labels that are MEANINGFUL right now are offered (noise drives misclassification):
  `continue` (the default); `switch:<flow>` for every flow EXCEPT the active one
  (switching to the service you're already in is nonsense); `new_request:<flow>` for
  every flow (a second instance of the same service is valid); `resume:<flow>` ONLY for
  flows that are actually paused; plus `correct:<slot>`, `cancel`, `escalate`.
  """
  sm = sm or {}
  flows = [str(f).lower().strip()
           for f in (raw_config.get("flow_types") or []) if str(f).strip()]
  gate_slot = sm.get("_gate_slot")
  active = sm.get("filled", {}).get(gate_slot) if gate_slot else None
  active = str(active).lower() if active else None
  paused = sorted({str(e.get("flow")).lower()
                   for e in sm.get("_flow_state", []) if e.get("flow")})
  labels = ["continue"]
  for f in flows:
    if f != active:
      labels.append(f"switch:{f}")
    labels.append(f"new_request:{f}")
  for f in paused:
    labels.append(f"resume:{f}")
  labels += ["correct:<slot>", "cancel", "escalate"]
  return labels


def _build_classifier_suffix(sm, raw_config):
  """The Pass-A framework SI: a minimal intent classifier. This REPLACES the whole
  framework suffix (no collection/readback blocks) — the model's only move is to
  call classify_turn_intent with one label and emit no text. Biased HARD toward
  `continue`: a non-continue label requires an explicit, unambiguous request."""
  gate_slot = sm.get("_gate_slot")
  active_flow = sm.get("filled", {}).get(gate_slot) if gate_slot else None
  flow_line = (f"a turn already inside a '{active_flow}' request"
               if active_flow else "a turn before any request is active")
  # Annotate each flow-bearing label with its route_cues as caller-phrasing hints so the
  # classifier disambiguates by what the caller SAYS, not just the bare flow name. The LABEL
  # stays exactly `switch:<flow>`/`new_request:<flow>` (the value the model must emit); the cues
  # are a trailing parenthetical hint the model weighs contextually. No cues for a flow ->
  # empty hint -> byte-identical output (a no-op for any agent without route_cues).
  _cues = raw_config.get("route_cues") or {}
  # Per-flow ROUTING DESCRIPTIONS (the same by-meaning descriptions the router turn uses)
  # rendered before the cue hints, so a mid-flow switch is judged by what each flow is FOR,
  # not just its bare key. Absent flow_descriptions -> no annotation -> byte-identical.
  _descs = raw_config.get("flow_descriptions") or {}
  def _label_line(l):
    _flow = l.split(":", 1)[1] if ":" in l else ""
    _desc = _descs.get(_flow) or ""
    _kw = _cues.get(_flow) or []
    _desc_txt = f" — {_desc}" if _desc else ""
    _hint = f'  (caller might say: {", ".join(str(k) for k in _kw[:8])})' if _kw else ""
    return f"   - {l}{_desc_txt}{_hint}"
  label_list = "\n".join(_label_line(l) for l in _intent_taxonomy(raw_config, sm))
  return f"""\
<intent_classifier>
You are an intent classifier for {flow_line}. Your ONLY job this turn is to call
{_tool_ref("classify_turn_intent")} with exactly ONE intent label for the caller's
latest message. Do NOT answer, collect, confirm, read back, switch flows, or call
any other tool. IGNORE any earlier instruction that tells you to call
set_active_flow, transfer, or switch services — for THIS turn you only classify.

Decide by asking: "Relative to the CURRENT request, what is the caller doing?"

`continue` is the answer for the VAST majority of turns. It covers ALL of:
  • Answering the current question — a value, time, date, name, quantity, "none".
  • Confirming or rejecting a read-back — "yes", "yep", "no", "that's wrong".
  • ADDING or volunteering ANY extra detail, note, preference, or related item to the
    CURRENT request — a clarification, a preference, an extra item, a comment. This is
    STILL `continue` EVEN when phrased with "also", "too", "as well", "and", "one more",
    "make a note", "by the way", "oh and". Adding to the request you are already building
    is `continue` — it is NOT a new request.
  • Off-topic questions, small talk, a vague reply, or not really answering.
When in any doubt, choose `continue`.

Choose a label OTHER than `continue` ONLY when the message clearly, explicitly
matches one of these — each is about the REQUEST AS A WHOLE, not a detail within it:
{label_list}

- new_request:<flow> — the caller wants a WHOLE SEPARATE, SECOND {active_flow or "request"}
  that stands on its own (a distinct case of its own, not more detail on this one) WHILE
  keeping the current one. Decisive test: would it need its OWN separate confirmation or
  reference? If they are just adding details to the request in progress → `continue`,
  never new_request.
- switch:<flow> — STOP the current request and do a DIFFERENT service INSTEAD
  ("switch to …", "actually do … instead", "forget this, I want …"). A passing
  mention of the other service is NOT a switch → `continue`.
- resume:<flow> — go BACK to a DIFFERENT request the caller set aside earlier
  ("go back to my …", "return to the … I started").
- correct:<slot> — CHANGE a value the caller ALREADY gave for THIS request
  ("actually make it 3 instead", "change that to Friday"). Giving a NEW, not-yet-asked
  detail is `continue`, not correct.
- cancel — STOP and abandon THIS request entirely ("cancel", "never mind, forget
  the whole thing"). A plain "no", a rejected read-back, or not answering is
  `continue`, NOT cancel.
- escalate — explicitly ask for a human / live agent / real person.

Output ONLY the tool call, no text.
</intent_classifier>"""


# Framework control tools — always visible to the model regardless of flow/config
# (flow switching, cancel/escalate, confirm/reject, corrections, session end). Never
# hidden by config-tool policies (Pass A hides all EXCEPT classify_turn_intent).
_CONTROL_TOOL_ALLOWLIST = frozenset({
    "cancel_flow", "transfer_to_human", "set_active_flow",
    "new_flow_instance", "resume_flow", "set_slot_change",
    "confirm_pending", "reject_pending", "end_session",
    "set_intent_changed", "try_again",
})


def _config_tool_names(raw_config):
  """All config-OWNED tool names in `raw_config`: setters, task executor tools, the
  bootstrap tool, and the correction tool. Control tools are NOT included."""
  names = set()
  for s in raw_config.get("slots", []):
    if s.get("setter"):
      names.add(s["setter"])
  for t in raw_config.get("tasks", []):
    if t.get("tool"):
      names.add(t["tool"])
  bootstrap = raw_config.get("bootstrap") or {}
  if bootstrap.get("tool"):
    names.add(bootstrap["tool"])
  if raw_config.get("correction_tool"):
    names.add(raw_config["correction_tool"])
  return names


def _pass_a_hide_tools(raw_config):
  """Every config- and control-tool name to hide in Pass A — i.e. all of them
  except classify_turn_intent — so the model can only classify."""
  hide = _config_tool_names(raw_config) | set(_CONTROL_TOOL_ALLOWLIST)
  hide.discard("classify_turn_intent")
  return sorted(hide)


# Memoized per-root reachable-config tool universe — static per process (mirrors
# _RAW_CONFIGS): {root_config_id: frozenset(every setter/task/bootstrap/correction
# tool name reachable from that root by walking component tasks)}.
_REACHABLE_TOOLS: dict[str, frozenset] = {}


def _reachable_config_tool_universe(root_id):
  """Every config-OWNED tool name reachable from `root_id` by walking component
  tasks (depth-bounded by _FRAME_DEPTH_CAP, cycle-safe via a visited set). Memoized
  per root. Best-effort: a child config that fails to load is skipped, so the
  universe degrades to fewer names rather than raising."""
  cached = _REACHABLE_TOOLS.get(root_id)
  if cached is not None:
    return cached
  names: set[str] = set()
  seen: set[str] = set()

  def _walk(cid, depth):
    if cid in seen or depth > _FRAME_DEPTH_CAP:
      return
    seen.add(cid)
    try:
      cfg = _engine_load_config(cid)
    except Exception:
      return
    names.update(_config_tool_names(cfg))
    for t in cfg.get("tasks", []):
      child = t.get("component")
      if child:
        _walk(child, depth + 1)

  _walk(root_id, 0)
  frozen = frozenset(names)
  _REACHABLE_TOOLS[root_id] = frozen
  return frozen


def _component_isolation_hides(sm):
  """Tools to hide so a Component descent exposes ONLY the active child's setters.

  No-op (empty set) at the parent level (no call frame) — today's behavior. While a
  frame is active, hide every reachable config-owned tool EXCEPT the active child's
  own tools and the framework control allowlist, so the model cannot reach the
  parent's or a sibling component's setters and "jump between components". Shared
  tool NAMES (used by both the active child and another config) stay visible because
  the subtraction is by name and the active set wins.

  NEVER raises: any failure degrades to hiding LESS (a partial/empty set), preserving
  today's behavior rather than breaking the turn with an empty render."""
  try:
    stack = sm.get("_call_stack")
    if not stack:
      return set()
    root_id = stack[0].get("parent_config")
    active_id = stack[-1].get("child_config")
    if not root_id or not active_id:
      return set()
    universe = _reachable_config_tool_universe(root_id)
    active_tools = _config_tool_names(_engine_load_config(active_id))
    hides = set(universe) - active_tools - set(_CONTROL_TOOL_ALLOWLIST)
    # The bootstrap ROUTING tool (e.g. set_active_flow) is in the control allowlist, so the subtraction above
    # keeps it visible at the flow/router level — but INSIDE a component sub-flow the model must NOT re-route
    # or re-activate a flow: calling it re-resolves config to the parent, which re-runs the parent DAG
    # (re-firing already-satisfied tasks) and derails/chains the sub-collection. Hide it for the duration of
    # the frame (the cancel/escalate control rail stays). This is the tool-level counterpart of the SI-level
    # flow-switch suppression already applied in a component (_build_phase_suffix). Derived from the root
    # flow's bootstrap and the live _bootstrap; no-op when neither is set.
    for _boot in {(_engine_load_config(root_id).get("bootstrap") or {}).get("tool"),
                  (sm.get("_bootstrap") or {}).get("tool")}:
      if _boot and _boot not in active_tools:
        hides.add(_boot)
    return hides
  except Exception:
    return set()


def _pass_a_directive(sm, raw_config):
  """The Pass-A action: classifier SI + hide everything but classify_turn_intent.
  No preempt — the model runs and must call the tool (the after_model try_again
  loop forces a retry if it doesn't)."""
  return {
      "si": _build_classifier_suffix(sm, raw_config),
      "hide_tools": _pass_a_hide_tools(raw_config),
      "tag": "pass_a_classify",
  }


# ── Router re-entry forced-classify (post-deferral) ──────────────────────────
# A steering router DEFERS a category by recording the intent, speaking a hand-off,
# and terminating (bootstrap.reset_on_complete -> zombie); the shipped reap returns
# control to the router in a CLEAN state (before_agent). On that ONE clean router turn
# the caller's follow-up is byte-identical to a cold routing turn, so the model reads
# it as "already handled" and declines to volunteer set_active_flow -> the router
# never leaves -> dead-end. This is the router-scoped twin of the in-flow Pass A: we
# COMPEL a single intent label for the new utterance, then the ENGINE routes on the
# verdict. Bounded by a dedicated _reentry_count so a record-and-terminate defer that
# keeps re-zombie-ing eventually falls to the shipped disambiguate/on_exhaust net.
_REENTRY_CAP = 2


def _reentry_taxonomy(raw_config):
  """The intent labels the re-entry classifier may emit. The gate is EMPTY (no active
  request to add to / correct), so the in-flow labels (new_request / resume / correct /
  cancel) are meaningless here — offer only: `continue` the last intent, `switch:<flow>`
  to any advertised flow, `escalate` to a human, or `end` the call."""
  flows = [str(f).lower().strip()
           for f in (raw_config.get("flow_types") or []) if str(f).strip()]
  return ["continue"] + [f"switch:{f}" for f in flows] + ["escalate", "end"]


def _build_reentry_classifier_suffix(sm, raw_config, reentry_intent):
  """The re-entry Pass-A classifier SI: after a defer handed off and control returned
  to the router, COMPEL exactly one intent label for the caller's NEW message. Framed by
  the last routed intent so `continue` re-enters it with no re-ask. Reuses the
  route_cues + flow_descriptions annotation the in-flow classifier suffix uses so the
  model disambiguates a `switch` by what each flow is FOR, not just its bare key."""
  _cues = raw_config.get("route_cues") or {}
  _descs = raw_config.get("flow_descriptions") or {}
  def _label_line(l):
    _flow = l.split(":", 1)[1] if ":" in l else ""
    _desc = _descs.get(_flow) or ""
    _kw = _cues.get(_flow) or []
    _desc_txt = f" — {_desc}" if _desc else ""
    _hint = f'  (caller might say: {", ".join(str(k) for k in _kw[:8])})' if _kw else ""
    return f"   - {l}{_desc_txt}{_hint}"
  label_list = "\n".join(_label_line(l) for l in _reentry_taxonomy(raw_config))
  return f"""\
<intent_classifier>
The caller was just routed to '{reentry_intent}', which handed off; control has now
returned to you. Your ONLY job this turn is to call {_tool_ref("classify_turn_intent")}
with exactly ONE intent label for the caller's NEW message. Do NOT answer, greet, route,
transfer, or call any other tool. IGNORE any earlier instruction telling you to call
set_active_flow or switch services — for THIS turn you only classify.

Choose exactly one label:
{label_list}

Guidance:
  - `continue` is the answer for the VAST majority of turns — the caller's message
    plausibly continues or follows up on '{reentry_intent}' (another question of the same
    kind, more detail, "and also…", a vague or unclear reply, small talk). When in ANY
    doubt, choose `continue`.
  - `switch:<flow>` ONLY when the caller clearly wants a DIFFERENT in-scope service now
    ("actually, my box is acting up instead").
  - `escalate` ONLY when they explicitly ask for a human / live agent / real person.
  - `end` ONLY when they are done or saying goodbye ("that's all, thanks", "no, bye").

Output ONLY the tool call, no text.
</intent_classifier>"""


def _reentry_classify_directive(sm, raw_config, reentry_intent):
  """The re-entry Pass-A action: the re-entry classifier SI + hide everything but
  classify_turn_intent (so classifying is the model's only move). No preempt — the model
  runs and must call the tool; after_model's shared _classify_mode handler defaults a
  text turn to `continue` + try_again, exactly as for the in-flow Pass A."""
  return {
      "si": _build_reentry_classifier_suffix(sm, raw_config, reentry_intent),
      "hide_tools": _pass_a_hide_tools(raw_config),
      "tag": "reentry_classify",
  }


def _build_resume_suffix(sm):
  """SI for paused-flow resume results + passive awareness.

  Reads resume state set by the resume_flow after_tool handler and the
  ``_flow_state`` stash. ``_resume_result`` is one-shot (popped here);
  ``_resume_target`` persists until set_active_flow's restore consumes it.
  Generic across agents -- no flow-specific wording.

  Args:
    sm: The session state-machine dict.

  Returns:
    Resume-related system-instruction text ("" if none applies).
  """
  def _describe(entry):
    merged = {**entry.get("slots", {}), **entry.get("pending", {}),
              **entry.get("deferred", {})}
    desc = ", ".join(f"{k}={v}" for k, v in merged.items())
    return f"  - id {entry.get('id')}: {entry.get('flow')}" + (
        f" ({desc})" if desc else "")

  resume_target = sm.get("_resume_target")
  resume_result = sm.pop("_resume_result", None)
  flow_stack = sm.get("_flow_state", [])
  if resume_target:
    flow = resume_target.get("flow", "")
    return (
        f"\n<resume_flow>\n"
        f"The user wants to resume a paused {flow} request. IMMEDIATELY"
        f' call {_tool_ref("set_active_flow")} with flow="{flow}" to resume'
        f" it. Do NOT re-ask for information that was already collected.\n"
        f"</resume_flow>"
    )
  if resume_result and resume_result.get("ambiguous"):
    opts = "\n".join(_describe(o) for o in resume_result.get("options", []))
    return (
        f"\n<resume_flow_ambiguous>\n"
        f"Multiple paused requests match. Ask the user which one they mean,"
        f" then call {_tool_ref('resume_flow')} again with a more specific"
        f" slot_name and slot_value (or the instance id):\n{opts}\n"
        f"</resume_flow_ambiguous>"
    )
  if resume_result and resume_result.get("error"):
    msg = resume_result.get("message", "No paused request matches.")
    return (
        f"\n<resume_flow_error>\n"
        f"{msg} Tell the user you don't have a paused request matching"
        f" that, then continue with the current task.\n"
        f"</resume_flow_error>"
    )
  if flow_stack:
    block = "\n".join(_describe(e) for e in flow_stack)
    return (
        f"\n<paused_flows>\n"
        f"These earlier requests are paused:\n{block}\n"
        f"If the user refers to one of them, call {_tool_ref('resume_flow')}"
        f" with the identifying slot_name and slot_value (or instance id).\n"
        f"</paused_flows>"
    )
  return ""


def _provided_details_block(config, slots, filled, pending, deferred,
                            repeat_acc=None):
  """The <provided_details> SI block: every ACTIVE user-provided value with its
  status — confirmed (filled) / awaiting confirmation (pending) / noted
  (deferred) / in progress (a repeated slot mid-collection, from _repeat_acc) —
  plus the set_slot_change change/add note. "" when none.

  Surfacing all three states keeps the LLM from re-asking for values it can't
  otherwise see, including shared values carried into a new flow instance that
  live in pending/deferred.
  """
  provided_parts = []
  gate_slot = config.get("gate_slot")
  repeat_acc = repeat_acc or {}
  merged = {**filled, **pending, **deferred}
  # A repeated component's `collect` slot is `source:"task:…"`, so it lands in
  # `filled` (never `pending`) and would be skipped as task-sourced below — yet it
  # IS a user-provided value the model must see (§R2.6). Surface it via its `join`
  # formatter once complete.
  repeated_collect_slots = {
      t["collect"] for t in config.get("tasks", [])
      if t.get("repeated") and t.get("collect")
  }
  for slot_def in slots:
    name = slot_def["name"]
    # Skip the gate slot, passive control slots (e.g. cancel), announce slots,
    # and task-sourced slots (except a completed repeated `collect` slot).
    if name == gate_slot or slot_def.get("passive"):
      continue
    sources = _normalize_sources(slot_def.get("source", "user"))
    is_repeated_collect = name in repeated_collect_slots
    if "announce" in sources or (
        any(s.startswith("task:") for s in sources) and not is_repeated_collect):
      continue
    if not _is_slot_active_ignoring_self(slot_def, name, merged):
      continue
    if name in filled:
      store, status = filled, "confirmed"
    elif name in pending:
      store, status = pending, "awaiting confirmation"
    elif name in deferred:
      store, status = deferred, "noted, will be confirmed shortly"
    elif name in repeat_acc:
      # A repeated slot mid-collection: its staged list lives in _repeat_acc
      # (kept out of filled/pending/deferred so dependents don't fire early), so
      # surface it here or the model has no grounding of what's collected so far.
      store = repeat_acc
      status = f"in progress, {len(repeat_acc[name])} so far — still collecting"
    else:
      continue
    formatter = _resolve_formatter(slot_def.get("readback_fmt"))
    # Dict-aware list fallback (§R2.6): never leak `['a','b']` / `{'k': 'v'}` repr
    # to the spoken surface when a list value carries no formatter.
    val = (formatter(store[name]) if formatter
           else _fallback_list_render(store[name]))
    provided_parts.append(f"- {slot_def.get('hint', name)}: {val} ({status})")

  if not provided_parts:
    return ""
  provided_summary = "\n".join(provided_parts)
  correction_tool = config.get("correction_tool")
  correction_note = ""
  # Advertise set_slot_change whenever the guest has provided a value (filled OR
  # pending OR deferred) — it's callable in those states (see
  # _compute_hidden_tools). It is the single change/add verb during a readback.
  has_provided_user = any(
      s["name"] in merged
      and "user" in _normalize_sources(s.get("source", "user"))
      and s.get("setter")
      for s in slots
  )
  if correction_tool and has_provided_user:
    correction_note = (
        "\nTo change OR add any of these, call " + _tool_ref(correction_tool) +
        " with the slot name(s) — the system will then collect the value."
    )
  return (
      f"\n\n<provided_details>"
      f"\nThe user has already provided the following (status in"
      f" parentheses). These apply to the whole session, including any new"
      f" or separate request — do NOT ask for them again and do NOT call"
      f" setter tools for them:"
      f"\n{provided_summary}"
      f"{correction_note}"
      f"\n</provided_details>"
  )


def _readback_scope_block(readback_hint, promoted_from_deferred):
  """The <readback_scope> SI block from a non-empty readback_hint ("" when empty).
  The promoted/restored variant adds the carried-over "confirm warmly" note."""
  if not readback_hint:
    return ""
  if promoted_from_deferred:
    return (
        f"\n\n<readback_scope>"
        f"\nYou MUST confirm ALL of the following values"
        f" together in a single readback — do NOT omit"
        f" any:\n{readback_hint}"
        f"\n\nNote: Some of these values (like name or"
        f" phone number) may have been carried over or"
        f" pre-filled from previous topics or orders."
        f" Do NOT ask for them again from scratch."
        f" Instead, confirm/validate them warmly"
        f" (e.g., 'I see we have your name as John"
        f" Smith, shall I put this under the same"
        f" name?')."
        f"\n</readback_scope>"
    )
  return (
      f"\n\n<readback_scope>"
      f"\nPending confirmation: {readback_hint}."
      f"\n</readback_scope>"
  )


def _build_si_suffix(
    config, slots, pending, filled, fresh_pending,
    promoted_from_deferred, deferred_hint, deferred,
    suppress_readback_hint=False, repeat_acc=None,
):
  """Build the system instruction suffix from engine state — the
  <provided_details>, <readback_scope> and <deferred_collection> blocks
  concatenated.

  suppress_readback_hint: when collecting a rejected sibling of a partial
    multi-setter group, omit the <readback_scope> block — the group is not yet
    complete, so the pending field is reported in <provided_details> (awaiting
    confirmation) but must not be read back for confirmation yet.
  """
  si_suffix = _provided_details_block(
      config, slots, filled, pending, deferred, repeat_acc=repeat_acc)

  readback_hint = _build_readback_hint(
      slots, pending, filled, fresh_pending,
      promoted_from_deferred=promoted_from_deferred,
  )
  if suppress_readback_hint:
    readback_hint = ""
  si_suffix += _readback_scope_block(readback_hint, promoted_from_deferred)

  if deferred_hint:
    si_suffix += (
        f"\n\n<deferred_collection>"
        f"\n{deferred_hint}"
        f"\n</deferred_collection>"
    )
  return si_suffix


# ═════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════


def _route_payloads(sm, announce_responses, task_resp, dag_response, dag_result,
                    fresh_pending, preempt, combined_response,
                    slots=None, filled=None, pending=None, slot_map=None,
                    channel=""):
  """Route this turn's UI payloads (cards / chips) to one of three destinations,
  after clearing last turn's stash. Returns the inline response list when the turn
  preempts (the caller sets it as the directive's `response`), else None.

  - preempt → ride INLINE on the directive (returned here).
  - unconditional announce/task payloads (and a fresh readback's chips) →
    sm._pending_payloads, appended by after_model to the model's reply.
  - a next-question's chips → sm._pending_question_payloads, appended by
    after_model only if that slot is still open.
  See slot_filling_dag_framework.md "after_model_callback Payload Injection".
  """
  sm.pop("_pending_payloads", None)
  sm.pop("_pending_question_payloads", None)
  # Reject re-ask: reject_pending fired this turn, so the rejected slot is STILL
  # pending now (the snapshot is cleared by before_agent next turn) and the model
  # re-asks the question in this same turn. The engine is therefore in readback
  # phase and stashes no question payload below — re-surface the re-asked slot's
  # chips so the model's re-ask is not chip-less. Computed as the next open
  # question ignoring the rejected (snapshot) slots; injected unconditionally by
  # after_model (the re-asked slot is still pending, so the open-slot guard there
  # would otherwise drop it).
  if sm.get("_rejection_requested") and slots is not None:
    reask_parts = _reask_question_payload(
        slots, filled or {}, pending or {}, slot_map or {}, channel,
        exclude=list((sm.get("_rejection_snapshot") or {}).keys()), sm=sm,
    )
    if reask_parts:
      sm["_reask_question_payloads"] = reask_parts
      _log("payload_route", "DEBUG", path="reask_reject",
           n_parts=len(reask_parts))
  awaiting_rb = dag_result.get("action") == "awaiting_readback"
  # Announce payloads (e.g. the welcome card) are stashed ONCE on the turn the
  # announce fires. If that turn ALSO fires a setter (function_call) — an entry
  # message carrying slot values, or a flow switch — after_model returns on the
  # function_call without injecting, and a plain _pending_payloads stash would be
  # cleared (top of this function) next turn before any text render delivers it,
  # dropping the card. Route announce payloads to a SURVIVING key that this
  # function does NOT clear; after_model delivers it on the next text render and
  # clears it. On a preempt the announce rides inline via combined_response, so
  # only stash when not preempting.
  if announce_responses and not preempt:
    sm["_pending_announce_payloads"] = list(announce_responses)
  unconditional = []
  if task_resp:
    unconditional += task_resp
  # The readback confirm chips (readback_response) must accompany EVERY readback,
  # fresh OR not. A non-fresh readback (pending persists; the model re-renders the
  # confirm prompt naturally after the user went off-topic / asked a question)
  # used to gate the chips on fresh_pending and so dropped them. They are
  # idempotent Yes/No chips, so re-stashing them on a non-fresh readback is
  # correct, not a duplicate. On a PREEMPTED fresh readback the chips ride inline
  # via combined_response and after_model never consumes this stash, so there is
  # no double delivery.
  if dag_response and awaiting_rb:
    unconditional += dag_response
  if unconditional:
    sm["_pending_payloads"] = unconditional
    _log("payload_route", "DEBUG", path="stash_unconditional",
         n_parts=len(unconditional))
  if dag_response and not awaiting_rb:
    sm["_pending_question_payloads"] = {
        "slot": dag_result.get("slot_name"),
        "parts": dag_response,
    }
    _log("payload_route", "DEBUG", path="stash_question",
         slot=dag_result.get("slot_name"), n_parts=len(dag_response))
  if not unconditional and not dag_response:
    _log("payload_route", "DEBUG", path="none")
  if preempt and combined_response:
    _log("payload_route", "DEBUG", path="preempt_dispatch",
         n_parts=len(combined_response))
    return combined_response
  return None


def _mark_end_session(sm, resp, tag, **log_data):
  """Mark the session terminal when a disposition's `response` ENDS the leg.

  The announce cascade has always done this (see `_cascade_announce` below); the
  two `on_exhaust` rungs a hand-off also attaches to — a task's
  `on_failure.on_exhaust` and a slot's `validation.on_exhaust` — never did. In
  PRODUCTION the omission is invisible: CES tears the session down the moment it
  sees a `Part.from_end_session`, whatever `sm` says. Offline it is not. The
  engine simulator reads `sm["status"]` to decide the call is over
  (`flows.sim.engine_sim._next_action` -> "terminal"), so a `flows.sim` walk went
  on being served turns after the caller had been handed off to a human, and a
  suite built on it could assert behaviour that cannot happen on a real call.
  The simulator is a primary verification tool, so that is not a cosmetic gap.

  `escalated` is left alone — it is the more specific outcome and every
  containment report reads it — so this only ever promotes a live status to
  `complete`. Returns whether the response ended the session.
  """
  if not any(isinstance(r, dict) and r.get("type") == "end_session"
             for r in (resp or [])):
    return False
  if sm.get("status") != "escalated":
    sm["status"] = "complete"
  _log(f"{tag}_end_session", **log_data)
  return True


# Following-along cues. DUPLICATED VERBATIM from flows/authoring/continuers.py, which is
# the source of truth and carries the reasoning; a CES tool cannot import the authoring
# package, the same constraint that duplicates the _KEEP_* registries into slot_intake. A
# unit test asserts the two copies are identical.
#
# The rule that matters: match the WHOLE normalized utterance, never individual words.
# "mhm but what about the fee" is a question, and a bag-of-words gate would eat it.
_DEFAULT_CONTINUERS = frozenset({
    "mhm", "mhmm", "mm", "mmm", "mm hmm", "mmhmm", "uh huh", "uhhuh", "ah ha",
    "aha", "hmm", "hm", "yup", "yep", "yeah", "yea", "ya", "right", "ok",
    "okay", "k", "sure", "alright", "all right",
    "got it", "gotcha", "understood", "i see", "i understand", "makes sense",
    "that makes sense", "fair enough", "sounds good", "that works",
    "no problem", "of course", "cool", "great", "perfect", "excellent",
    "good", "thats good", "very good", "nice",
    "go on", "carry on", "keep going", "continue", "im listening",
    "im with you", "with you", "im here", "still here", "go ahead",
})
_MAX_CONTINUER_WORDS = 6
_CONT_KEEP = re.compile(r"[^a-z0-9 ]+")
_CONT_SPLIT = re.compile(r"[,.;!?]+| and ")


def _norm_continuer(text):
  """Lowercase, drop apostrophes and punctuation, collapse whitespace."""
  low = (text or "").lower().replace("’", "").replace("'", "")
  return re.sub(r"\s+", " ", _CONT_KEEP.sub(" ", low)).strip()


def _is_continuer(text, phrases=None, extra=None):
  """Is this WHOLE utterance nothing but following-along noise?"""
  norm = _norm_continuer(text)
  if not norm or len(norm.split()) > _MAX_CONTINUER_WORDS:
    return False
  vocab = (frozenset(_norm_continuer(p) for p in phrases)
           if phrases is not None else _DEFAULT_CONTINUERS)
  if extra:
    vocab = vocab | frozenset(_norm_continuer(p) for p in extra)
  vocab = frozenset(p for p in vocab if p)
  if norm in vocab:
    return True
  clauses = [c for c in (_norm_continuer(c) for c in _CONT_SPLIT.split(text or "")) if c]
  return len(clauses) > 1 and all(c in vocab for c in clauses)


_LEDGER_KEY = "_said_parts"
_LEDGER_CAP = 40

# Everything the comparison must ignore. The "heard" prefix is prose the PLATFORM
# composed from what it played, not a copy of the string we sent, so punctuation,
# capitalisation and spacing all drift. Compare on letters, digits and single spaces.
_SAY_NORM = re.compile(r"[^a-z0-9 ]+")
_SAY_WS = re.compile(r"\s+")


def _norm_say(text):
  """Normalize spoken text so an intended line and a heard prefix are comparable."""
  low = (text or "").lower().replace("’", "'").replace("—", " ")
  return _SAY_WS.sub(" ", _SAY_NORM.sub(" ", low)).strip()


def _ledger_add(sm, slot, msg, resp, spec=None):
  """Append one entry per TEXT part this announce contributed to the preempt.

  Only text parts: a payload carries no speech, and a transfer / end_session part ends
  the cascade anyway, so neither can be the thing a caller talked over. The announce's own
  `repair` spec rides on each row, because by the time repair runs the DAG has moved on
  and re-deriving which announce owned which part would mean recomputing the cascade --
  exactly what replay must not do.
  """
  ledger = sm.setdefault(_LEDGER_KEY, [])
  texts = [msg] if msg else []
  for part in (resp or []):
    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
      texts.append(part["text"])
  for text in texts:
    if len(ledger) >= _LEDGER_CAP:
      break
    row = {"slot": slot, "i": len(ledger), "text": text}
    if spec:
      row["repair"] = spec
    ledger.append(row)


def _split_heard(intended, heard):
  """Cut one intended line at the point the caller stopped hearing it.

  Returns `{"heard", "unheard", "exact"}`. `exact` is False whenever the boundary was
  guessed rather than matched, and callers MUST degrade on it rather than speak a
  remainder that may be wrong -- treat False as the common case, because the prefix
  arrives through TTS and the platform's own transcription, not from our string.
  """
  n_int, n_heard = _norm_say(intended), _norm_say(heard)
  if not n_int or not n_heard:
    return {"heard": "", "unheard": "", "exact": False}
  if n_int.startswith(n_heard):
    cut = _raw_offset(intended, len(n_heard))
    return {"heard": intended[:cut].strip(),
            "unheard": intended[cut:].strip(), "exact": True}
  # No clean prefix: take the longest common run and accept it only if it is long
  # enough to be a real match rather than two lines that happen to share "the".
  common = 0
  for a, b in zip(n_int, n_heard):
    if a != b:
      break
    common += 1
  if common >= 12 and common >= len(n_heard) * 0.25:
    cut = _raw_offset(intended, common)
    return {"heard": intended[:cut].strip(),
            "unheard": intended[cut:].strip(), "exact": False}
  return {"heard": "", "unheard": "", "exact": False}


def _raw_offset(raw, n_norm):
  """Index into `raw` just past its first `n_norm` NORMALIZED characters.

  One incremental pass mirroring `_norm_say` exactly (lower, fold the two smart
  characters, everything outside [a-z0-9] becomes a space, runs of space collapse to one,
  leading space dropped). Re-normalizing every prefix instead would be quadratic, and
  these lines are whole disclosure paragraphs.
  """
  if n_norm <= 0:
    return 0
  count, pending_space, started = 0, False, False
  for idx, ch in enumerate(raw):
    low = ch.lower()
    if low == "’":
      low = "'"
    if not ("a" <= low <= "z" or "0" <= low <= "9"):
      if started:
        pending_space = True
      continue
    if pending_space:
      count += 1
      pending_space = False
    count += 1
    started = True
    if count >= n_norm:
      return idx + 1
  return len(raw)


def _classify_ledger(ledger, heard):
  """Label every ledger entry `heard` / `cut` / `unspoken` against the heard prefix.

  This is why the ledger is per PART. The boundary lands inside at most one part; every
  part after it was never started, so it replays verbatim with no text analysis and no
  chance of a wrong remainder. That is the cascade case -- announces #2..#N of a batched
  preempt -- and it is the one that matters most.
  """
  # The boundary is where the caller stopped hearing, in normalized characters. Trusting
  # the reported prefix's LENGTH alone is what made a missing ledger row drop a whole
  # sentence, so anchor it on CONTENT: walk the reported prefix against what we actually
  # said and stop at the first disagreement. When they agree the two are identical; when
  # they do not, the common prefix is the honest boundary and it is always the SHORTER
  # one, so the failure mode is replaying a line the caller already heard rather than
  # silently skipping one they did not. That asymmetry is deliberate -- a repeat is
  # noticeable and harmless, a drop is neither.
  spoken = " ".join(_norm_say(e.get("text", "")) for e in ledger).strip()
  spoken = _SAY_WS.sub(" ", spoken)
  n_heard = _norm_say(heard)
  boundary = len(n_heard)
  if not spoken.startswith(n_heard):
    common = 0
    for a, b in zip(spoken, n_heard):
      if a != b:
        break
      common += 1
    boundary = common
  out, cursor = [], 0
  for entry in ledger:
    length = len(_norm_say(entry.get("text", "")))
    start, end = cursor, cursor + length
    # +1 per part for the space that joins them when spoken back to back.
    cursor = end + 1
    if not length:
      state = "heard" if end <= boundary else "unspoken"
    elif end <= boundary:
      state = "heard"
    elif start >= boundary:
      state = "unspoken"
    else:
      state = "cut"
    out.append(dict(entry, state=state))
  return out


def _plan_repair(sm, oi_cfg, heard):
  """The text to replay for announces cut short last turn, or "" if there is none.

  Reads `_said_prev` -- the ledger the cascade wrote on the turn it spoke -- and never the
  DAG. That is the whole safety property: replay cannot re-evaluate a condition or re-fire
  a downstream gate, because it is reading a recording rather than recomputing.
  """
  ledger = sm.get("_said_prev") or []
  if not ledger:
    return "", False
  default_repair = oi_cfg.get("repair_announces") or {}
  rows = _classify_ledger(ledger, heard)
  counts = sm.setdefault("_repair_counts", {})
  out, exact_all, lead_in = [], True, ""
  for row in rows:
    spec = row.get("repair") or default_repair
    if not spec or row["state"] == "heard":
      continue
    slot = row.get("slot") or ""
    if counts.get(slot, 0) >= int(spec.get("max_repairs", 2)):
      # A caller interrupting every attempt must not be told the same thing forever.
      _log("barge_repair_capped", slot=slot)
      continue
    mode = spec.get("mode", "parts")
    text = row.get("text") or ""
    if row["state"] == "unspoken" or mode == "full":
      piece = text
    else:  # the one part that was cut mid-way
      split = _split_heard(text, heard)
      if mode == "remainder" and split["exact"] and split["unheard"]:
        piece = split["unheard"]
      else:
        # `parts` restarts the cut sentence, and `remainder` degrades to that whenever the
        # boundary was guessed -- half a sentence spoken from the wrong offset is worse
        # than one repeated sentence.
        piece = text
        exact_all = exact_all and split["exact"]
    if len(_norm_say(piece)) < int(spec.get("min_unheard_chars", 15)):
      continue
    if not out:
      lead_in = spec.get("lead_in", "")
    counts[slot] = counts.get(slot, 0) + 1
    out.append(piece)
  if not out:
    return "", exact_all
  return (lead_in + " " + " ".join(out)).strip() if lead_in else " ".join(out), exact_all


def _interrupted_line(oi_cfg, sm, filled, heard):
  """The `on_interrupted` line to speak, with `{heard}` / `{unheard}` resolved.

  `say_unknown` is the honest path, not the fallback: the heard prefix is the platform's
  transcription of what it played, so it frequently will not line up with the string we
  sent. When `{unheard}` cannot be trusted it is withheld rather than guessed, because a
  confidently wrong "you missed X" is worse than a plain "let me say that again".
  """
  say, say_unknown = oi_cfg.get("say", ""), oi_cfg.get("say_unknown", "")
  intended = " ".join((row.get("text") or "")
                      for row in (sm.get("_said_prev") or []))
  split = _split_heard(intended, heard) if intended else {
      "heard": "", "unheard": "", "exact": False}
  usable = bool(split["exact"] and split["unheard"]
                and len(_norm_say(split["unheard"]))
                >= int(oi_cfg.get("min_unheard_chars", 15)))
  if say and ("{unheard}" not in say or usable):
    ctx = dict(filled)
    ctx["heard"], ctx["unheard"] = split["heard"], split["unheard"]
    return _safe_format(say, ctx)
  return _safe_format(say_unknown, filled) if say_unknown else ""


def _cascade_announce(sm, config, slots, tasks, filled, pending, deferred,
                      task_results, slot_map, channel, collect_slot):
  """Run the announce cascade: compute the DAG state and, while it wants to
  ANNOUNCE a slot, mark it filled, collect its message + response, and recompute —
  until a non-announce action (or a repeat-cycle / end_session). Mutates `filled`
  (each announced slot -> True) and sm (status on end_session). Returns
  (dag_result, announce_msgs, announce_responses, any_announce_preempt)."""
  announce_msgs = []
  announce_responses = []
  any_announce_preempt = False
  # Does anything in this flow ask for repair? If not, the ledger is never written and an
  # agent that does not use the feature is untouched. If so, EVERY announce is recorded,
  # because the offsets are only correct when the recording covers all the speech.
  _repairable_flow = bool(
      (config.get("on_interrupted") or {}).get("repair_announces")
      or any(s.get("repair") for s in (config.get("slots") or [])))

  def _dag_state():
    return _compute_dag_state(
        tasks, slots, filled, pending, task_results, slot_map,
        deferred=deferred, channel=channel, config=config,
        skip_partial_readback=bool(collect_slot), sm=sm)

  dag_result = _dag_state()
  announced = set()
  while dag_result["action"] == "announce":
    slot_def_a = dag_result["slot_def"]
    name_a = slot_def_a["name"]
    if name_a in announced:
      _log("announce_cycle_break", "WARN", slot=name_a)
      break
    announced.add(name_a)
    # `message` is optional when the announce delivers its content via `response`
    # parts (a fully-composed, conditionally-rendered welcome). Default to "" and
    # only emit a non-empty message so a response-only announce adds no blank line.
    msg_a = slot_def_a.get("message", "")
    try:
      msg_a = msg_a.format(**filled)
    except KeyError:
      pass
    filled[name_a] = True
    # `sets` is what makes a LADDER of announces possible. An announce latches only its
    # own name, so mutually exclusive rungs would all fire on the same pass and the
    # caller would hear every one of them — the whole cascade leaves as one preempt.
    # Writing a SHARED key here closes, on the recompute at the bottom of this loop, the
    # gate the remaining rungs are conditioned on, so exactly one speaks.
    #
    # Into `filled` rather than onto the action's `state_writes`: the recompute reads
    # `filled`, and a write that only reached the action would not be visible until the
    # next engine invocation — which is the round trip this exists to avoid.
    #
    # Not overwriting an existing value is deliberate. A rung's own condition is what
    # decides whether it fires; if a key it sets is already filled, some earlier rung or
    # a hook owns that value, and clobbering it here would let a late announce silently
    # rewrite a verdict that has already been spoken.
    #
    # TRUTHINESS, not `in filled`, and the two are not interchangeable. `_eval_condition`
    # reads a `filled` leaf as `bool(filled.get(slot))`, so a key present with a falsy
    # value — `""`, `False`, `0` — is UNFILLED as far as every condition is concerned.
    # Guarding on presence there would leave the rung eligible (the gate reads open) while
    # refusing to write the latch (the key exists), so the gate could never close and
    # every lower rung would speak too: exactly the failure `sets` exists to prevent, with
    # nothing in the log to say why.
    # `relatch` is the declared exception to the paragraph above, and it exists because
    # a latch that RE-ARMS cannot be expressed without one. A walkthrough step latches
    # "a tip is outstanding" as it speaks and the next step waits a caller turn on it
    # (`since_turns`); but the second tip's write lands on a slot that is still filled,
    # so without this it is skipped, the stamp stays on the FIRST tip, and every
    # remaining step reads a gate that opened turns ago -- the whole list is read out to
    # a caller who has answered none of it. Authors were clearing the slot from a hook
    # to force this write, on a caller-turn boundary the hook had to derive itself.
    #
    # The stamp is refreshed HERE rather than left to the end-of-turn `_stamp_fills`
    # sweep, because announces cascade within one pass: a later step in the same cascade
    # must already see the new stamp, or it rides out on this step's turn and nothing has
    # been fixed. The sweep only ever adds a MISSING stamp, so this write survives it.
    for _sk, _sv in (slot_def_a.get("sets") or {}).items():
      _relatch = bool((slot_map.get(_sk) or {}).get("relatch"))
      if not filled.get(_sk):
        filled[_sk] = _sv
        _log("announce_sets", slot=name_a, key=_sk, value=_sv)
      elif _relatch:
        filled[_sk] = _sv
        sm.setdefault("_filled_turn", {})[_sk] = sm.get("_turn_n", 0)
        _log("announce_relatch", slot=name_a, key=_sk, value=_sv,
             turn=sm.get("_turn_n", 0))
    if msg_a:
      announce_msgs.append(msg_a)
    resp_a = _resolve_response(slot_def_a, "response", filled, channel)
    if resp_a:
      announce_responses.extend(resp_a)
    # THE DELIVERY LEDGER. `filled[name_a] = True` above records that this announce was
    # DISPATCHED, which is not the same as heard: the whole cascade leaves as ONE preempt,
    # so a caller who talks over announce #1 never hears #2..#N and all of them are latched
    # anyway (ces-probes 161). Record what actually went out, per PART, so the next turn
    # can say which parts the caller reached and replay only the rest.
    #
    # Per part rather than per announce because the part boundary is exact: everything
    # after the cut point needs no text analysis at all, and only the one part that was cut
    # mid-way needs the fuzzy split. Repair reads this ledger and never re-enters the
    # cascade, which is what keeps it from re-firing downstream gates.
    # Ledger EVERY announce this cascade speaks, repairable or not.
    #
    # Ledgering only the repairable ones is wrong, and silently: the heard prefix the
    # platform reports covers everything the caller ACTUALLY heard, so a non-repairable
    # announce spoken first still consumes characters of it. Leaving that announce out
    # shifts the boundary by its whole length and marks the next announce heard when the
    # caller never reached it -- a sentence dropped, invisibly. Measured on the demo: a
    # 63-character welcome with no `repair=` made the first disclosure line disappear.
    #
    # Repairability is a REPLAY-time decision (`_plan_repair` skips rows with no spec),
    # not a recording-time one. The whole-flow gate below keeps an agent that uses none of
    # this writing nothing at all.
    if _repairable_flow:
      _ledger_add(sm, name_a, msg_a, resp_a, slot_def_a.get("repair"))
    if slot_def_a.get("preempt", True):
      any_announce_preempt = True
    if any(isinstance(r, dict) and r.get("type") == "end_session"
           for r in (resp_a or [])):
      sm["status"] = "complete"
      # An offer/leaf component ends the WHOLE conversation, not just its frame:
      # popping the call stack lets the end_session part survive the spine's
      # child-terminal frame-abandon guard (mirrors _terminate's teardown).
      if slot_def_a.get("end_conversation"):
        sm.pop("_call_stack", None)
        _log("announce_end_conversation", slot=name_a)
      _log("announce", slot=name_a)
      break
    # A transfer part hands control to another agent — the flow terminates here,
    # exactly like end_session. Without this the cascade recomputes past the (now
    # filled) route slot into all_done, whose "All information collected!" sentinel
    # then leaks onto the transfer turn's spoken surface. Mark terminal and stop.
    if any(isinstance(r, dict) and r.get("type") == "transfer"
           for r in (resp_a or [])):
      sm["status"] = "complete"
      if slot_def_a.get("end_conversation"):
        sm.pop("_call_stack", None)
        _log("announce_end_conversation", slot=name_a)
      _log("announce_transfer_terminal", slot=name_a)
      break
    _log("announce", slot=name_a)
    dag_result = _dag_state()
  return dag_result, announce_msgs, announce_responses, any_announce_preempt


def _readback_disposition(action, fresh_pending, pending, filled, *,
                          post_correction_flag, restored_flow,
                          promoted_from_deferred, correction_unresolved_flag):
  """Pure readback-preemption decision (no side effects).

  The orchestrator owns the sm pops (because they must happen exactly once per
  turn regardless of outcome) and passes the already-read flag values in; this
  function only computes the four signals that force the engine's readback
  message to reach the user instead of the LLM's free text. Keeping the gating
  here makes it unit-testable as a truth table — historically every readback bug
  landed in this decision. Each signal:

    post_correction_readback — a fresh readback immediately after a correction;
      the LLM's free text might ask for the wrong next slot.
    override_readback        — the user overrode an already-filled (confirmed)
      slot; the new value must be read back before proceeding.
    promoted_readback        — pending was promoted from deferred, or a paused
      flow was restored; the regrouped values need a fresh readback.
    unresolved_correction    — a set_slot_change that resolved to nothing
      collectable; the post-setter re-invocation would freeze on an LLM render,
      so re-prompt deterministically. NOT gated on fresh_pending (it happens in
      the awaiting_confirmation phase, where pending is not fresh this turn).
  """
  is_rb = action == "awaiting_readback"
  return {
      "post_correction_readback": bool(post_correction_flag and fresh_pending and is_rb),
      "override_readback": bool(
          fresh_pending and is_rb and any(k in filled for k in pending)),
      "promoted_readback": bool(
          (promoted_from_deferred or restored_flow) and fresh_pending and is_rb),
      "unresolved_correction": bool(
          correction_unresolved_flag and pending and is_rb),
  }


def _resolve_hold_state(sm, no_input, text):
  """Record whether the caller has asked for time, from this utterance.

  Idempotent, and called from two places on purpose. The engine resolves this as early as
  it can -- before the option cues, the keyword backstops and the retry ladders, each of
  which can RETURN, and each of which used to act on a request for time as though the
  caller had simply answered badly. `_run_slot_filling` then calls it again, because a
  flow entered through a gate carries its entry utterance separately, and that is the one
  turn the caller actually spoke.
  """
  if not isinstance(no_input, dict) or not text:
    return
  if _is_hold_request(text, no_input):
    sm["_hold_on"] = True
    # Distinct from `_hold_on`, which PERSISTS across the silent turns that follow. This
    # marks the single turn on which the caller actually asked, so `hold_ack`
    # acknowledges the request once rather than on every later pass.
    sm["_hold_requested"] = True
  elif sm.get("_hold_on"):
    # Any other real content exits hold: the caller is answering, and steer-back and
    # normal collection should treat it as a value to capture, not a wait to sit out.
    sm.pop("_hold_on", None)
    sm.pop("_hold_requested", None)


def _run_slot_filling(
    config: dict[str, Any],
    sm: dict[str, Any],
    last_user_text: str = "",
    is_inactivity: bool = False,
    entry_user_text: str = "",
    is_barge_in: bool = False,
    barge_heard: str = "",
) -> dict[str, Any]:
  """Run one turn of the slot-filling DAG engine.

  `entry_user_text` is the utterance a GATED flow was entered on, which never
  appears in `last_user_text` (the model's last message on that pass is the
  bootstrap tool's function response). It is read for one thing only — resolving
  hold state — and is empty on every other turn.
  """
  # Empty-render backstop arm starts cleared every engine turn; the proceed path
  # below re-arms it. Cleared here (not only on the proceed path) so a stale
  # fallback from a prior turn can never fire on a return path that doesn't arm.
  sm.pop("_render_fallback", None)
  # Roll the delivery ledger over exactly one turn. The cascade WRITES `_said_parts` on
  # the turn it speaks; repair READS it on the next one, which is the turn the barge-in
  # envelope arrives. Rotating here (rather than clearing) keeps both without letting a
  # long call accumulate every line ever spoken -- each turn sees only the turn before it.
  _prev = sm.pop(_LEDGER_KEY, None)
  if _prev:
    sm["_said_prev"] = _prev
  else:
    sm.pop("_said_prev", None)
  # Resolve hold state FIRST, before any path can return. A caller who asks for time is
  # answered by the silence policy, and every other ladder has to be able to see that
  # they did -- so this cannot sit behind a decision.
  #
  # It used to. Three separate paths returned before it: the barren-turn ladders charged
  # the request as an answer that resolved to nothing (two of those force-fill the slot
  # and carry on without the caller), the keyword route backstop read the cues INSIDE the
  # stall and left the flow, and an option cue that matched the same utterance filled the
  # awaited slot and returned. That last one is why "okay, hold on" at a yes/no question
  # was recorded as consent: `ok` matched, the turn progressed, and the engine never
  # learned the caller had asked to wait. The answer still stands -- what changes is that
  # the flow now knows to wait for them.
  #
  # A flow entered through a gate never sees the utterance that entered it: the model's
  # last message on that pass is the bootstrap tool's response, so `last_user_text` is
  # empty. The entry utterance IS carried across the gate, and resolving hold state is
  # the ONE thing it is read for -- deliberately not collection, steer-back or
  # affirmation, which have their own reasons to read a genuinely empty turn as empty.
  # `_hold_text` is read again further down, by the ack branch: the entry turn carries
  # speech even though the model's last message does not.
  _hold_text = (last_user_text or "").strip() or (entry_user_text or "").strip()
  _resolve_hold_state(sm, config.get("no_input"), _hold_text)

  slots = config["slots"]
  tasks = config["tasks"]
  # Tolerance (§4.1): a Component task has no `tool`, so exclude it here — else the
  # unconditional t["tool"] KeyErrors before any decision runs. executor_tool_names
  # (below) then excludes components for free.
  executors = {t["name"]: t["tool"] for t in tasks if t.get("tool")}
  readback_tools = ["confirm_pending", "reject_pending"]
  steer_back_cfg = config.get("steer_back", {})
  _prefix_cfg = config.get("confirm_transition_prefix", "")
  if isinstance(_prefix_cfg, list):
    confirm_transition_prefix = (
        random.choice(_prefix_cfg) if _prefix_cfg else ""
    )
  else:
    confirm_transition_prefix = _prefix_cfg

  slot_map = {s["name"]: s for s in slots}
  dag_shared = {s["name"] for s in slots if s.get("shared")}
  existing_shared = set(sm.get("_shared_slots", []))
  sm["_shared_slots"] = sorted(existing_shared | dag_shared)
  executor_tool_names = list(executors.values())
  channel = sm.get("channel", "")

  sm["_invoke_n"] = sm.get("_invoke_n", 0) + 1
  inv_n = sm["_invoke_n"]

  # setdefault, not get: a silence-first turn (no setter has run yet) arms an
  # in-flow offer via filled[open_slot]=True below — that must persist on sm.
  filled = sm.setdefault("filled", {})
  pending = sm.get("pending", {})
  deferred = sm.setdefault("deferred", {})
  task_results = sm.get("task_results", {})
  last_state = sm.get("_last_state", {})

  # Deferred fail_flow consumer (§6.8): on the pass AFTER an on_abort='fail_flow'
  # abort, the frame was popped by _frame_abandon and `config`/scope are now the
  # re-resolved PARENT's, so terminating here builds the zombie from PARENT data
  # (correct flow id + exit_status). Re-enter the existing primitive under the parent
  # control block; _call_stack is empty so it takes the non-frame path. The flag is a
  # STRING naming that block for a deflection (escalate.component -> "escalate", so the
  # outcome logs as escalated); a bare truthy value keeps the historical "cancel".
  _fail_block = sm.pop("_fail_parent_flow", False)
  if _fail_block:
    return _terminate_control(
        sm, config, _fail_block if isinstance(_fail_block, str) else "cancel",
        filled, pending, deferred, task_results, allow_menu_return=False)

  # Terminal control slots (cancel, escalate) take priority over everything else
  # (collection, readback, auto-confirm). The user filled one — via its setter or
  # the before_model backstop — so tear the flow down to a zombie (optionally
  # after a confirmation), via the same primitive completion uses. Control returns
  # to the parent (block.transfer_to) to be reaped on re-entry.
  control_result = _handle_terminal_slots(
      sm, config, filled, pending, deferred, task_results, last_user_text,
  )
  if control_result:
    return control_result

  # An armed escalate chain (phase 1) owns the whole turn, and its exhaustion runs the
  # disposition the rail would have run at once (phase 2, unchanged). Nothing below this
  # point is reachable while the chain is in flight — and nothing below it was touched,
  # so an agent without `escalate.tasks` is byte-identical.
  if sm.get("_escalate_path"):
    escalate_result = _escalate_path_turn(
        sm, config, filled, pending, deferred, task_results,
        slot_map, channel, inv_n,
    )
    if escalate_result is not None:
      return escalate_result
    sm.pop("_escalate_path", None)
    sm.pop("_escalate_ticks", None)
    _chain_msg = sm.pop("_escalate_pending_msg", "")
    _disposition = _terminate_control(sm, config, _ESCALATE_SLOT, filled, pending,
                                      deferred, task_results)
    # Speak the final member's then_say ahead of the disposition rather than losing it.
    if _chain_msg and isinstance(_disposition, dict):
      _say = _disposition.get("message") or ""
      _disposition["message"] = f"{_chain_msg} {_say}".strip() if _say else _chain_msg
    return _disposition

  progressed_this_turn = _handle_state_change(sm, filled, pending, last_state)

  # ── Following-along cues ────────────────────────────────────────────────
  # "mhmm", "got it", "go on". The caller is agreeing, not answering and not taking the
  # floor. Evaluated AFTER `_handle_state_change` so a turn that actually filled something
  # is already `progressed` and never reaches this -- which is the pending-slot-wins rule
  # in its cheapest form: if the utterance answered the question, a slot moved, and the
  # question of whether it was also a noise never arises.
  #
  # Treating it as progress is the point. It suppresses the steer-back increment that
  # otherwise counts agreement as a stall and escalates the call after enough of them.
  # Absent means ON. A caller saying "mhm" is not answering in ANY flow, and the failure
  # it causes (agreement counted as a stall, escalating the call) is a bug everywhere, not
  # a feature an author should have to opt into. `continue_cues(enabled=False)` opts out.
  sm.pop("_continuer", None)
  _cc_cfg = config.get("continue_cues") or {}
  if (_cc_cfg.get("enabled", True)
      and last_user_text and not progressed_this_turn
      and _is_continuer(last_user_text, _cc_cfg.get("phrases"),
                        _cc_cfg.get("extra"))):
    sm["_continuer"] = True
    progressed_this_turn = True
    sm["_steer_back_turns"] = 0
    _log("continuer", text=last_user_text[:40])

  deferred_promoted = _auto_promote_and_route(
      slots, tasks, task_results, slot_map,
      filled, pending, deferred, sm,
  )

  retries = sm.setdefault("_retries", {})
  _deactivate_conditional_slots(
      slots, filled, pending, deferred, retries,
      armed=set(sm.get("_armed_offer_flags", [])),
  )

  sm["_last_state"] = {
      "filled": dict(filled),
      "pending": dict(pending),
      "deferred": dict(deferred),
  }
  last_pending = last_state.get("pending", {})
  fresh_pending = pending != last_pending
  last_deferred = last_state.get("deferred", {})
  fresh_deferred = bool(set(deferred) - set(last_deferred))
  promoted_from_deferred = bool(
      set(pending) & (set(last_deferred) - set(last_pending))
  )
  # A partially-collected multi-field setter group (one field pending, a rejected
  # sibling still unfilled) must finish collecting before any readback — else the
  # lone pending field reads back with the shared setter hidden and the model
  # freezes on the guest's retry. Collect the rejected sibling first (setter
  # visible), then read the whole group back together.
  collect_slot = _partial_group_collect_slot(
      slots, filled, pending, deferred, retries, slot_map,
  )
  collect_setter = slot_map[collect_slot].get("setter") if collect_slot else None
  if pending and not collect_slot:
    phase = "fresh_readback" if fresh_pending else "awaiting_confirmation"
  else:
    phase = "collection"

  result = _try_auto_confirm(phase, last_user_text, sm)
  if result:
    return result

  inline_confirmed, phase, fresh_pending = _apply_inline_confirm(
      sm, filled, pending, phase, fresh_pending,
  )

  # An `answer` policy's read/compute tools are exposed only on the answer turn
  # (_handle_answer, below); everywhere else they stay hidden. Empty set for an
  # app with no `answer` policy — the hide list is then byte-identical.
  answer_tools = set()
  for _ans in (config.get("answer") or []):
    answer_tools.update(_ans.get("tools") or [])

  hide_tools = _compute_hidden_tools(
      slots, filled, pending, readback_tools, slot_map,
      fresh_pending=fresh_pending, executor_tools=executor_tool_names,
      deferred=deferred,
      correction_tool=config.get("correction_tool"),
      expose_setters=({collect_setter} if collect_setter else None),
      answer_tools=answer_tools,
  )
  bootstrap_tool = config.get("bootstrap", {}).get("tool")
  if bootstrap_tool and bootstrap_tool in hide_tools:
    while bootstrap_tool in hide_tools:
      hide_tools.remove(bootstrap_tool)

  # The `push_back` re-offer ladder: a slot whose caller keeps answering off-cue
  # (declining an offer / insisting) re-offers as a PREEMPT for pushes 1..max, then
  # disposes. Runs first so it owns the turn — the model cannot improvise this turn. See
  # _push_back_tick; distinct from no_input (silence), validation (bad value), steer_back.
  _pb = _push_back_tick(sm, slots, last_user_text, filled)
  if _pb is not None and _pb[0] in ("reprompt", "dispose"):
    sm["_steer_back_turns"] = 0
    if _pb[0] == "reprompt":
      _, _pb_msg, _pb_vb = _pb
      _pb_res = {"hide_tools": hide_tools, "preempt": True, "message": _pb_msg,
                 "speech_class": "reprompt"}
    else:  # dispose: fire the on_exhaust tool, optionally ending the leg
      _, _pb_msg, _pb_fc, _pb_end, _pb_vb = _pb
      _pb_res = {"hide_tools": hide_tools, "preempt": True, "message": _pb_msg,
                 "speech_class": "exhaust"}
      if _pb_fc:
        _pb_res["function_call"] = _pb_fc
      if _pb_end:
        # End the whole call after the disposition tool — pop the call stack so the
        # end_session survives a component frame-abandon guard (as no_input does).
        _pb_resp = []
        _mark_end_session(sm, _pb_resp, "push_back")
        _pb_res["response"] = _pb_resp
        sm["status"] = "complete"
        sm.pop("_call_stack", None)
      elif _pb_fc:
        sm["status"] = "escalated"
    if _pb_vb:
      _pb_res["verbatim"] = True
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                preempted=_pb_msg or "(push_back)", deferred=deferred)
    return _pb_res
  # (A push_back "fill" falls through so the dispose task/announce cascade fires below.)

  # Grounded free-response ("answer") interception. Runs HERE — before the no-match /
  # slot-error / collection paths below — so an off-menu, on-intent QUESTION ("add these
  # two", "what's my next bill") is answered from grounding rather than treated as a bad
  # answer to the open offer / closing slot (which otherwise renders an empty "having
  # trouble" turn, or hands the model a free turn with the whole tool surface). A
  # human-request/cancel already preempted upstream (_handle_terminal_slots), and a
  # progressing turn (cue match / fill) still routes to the rails — the node early-returns
  # on `progressed`. It YIELDS to active hard-VALUE collection (a date/number user slot):
  # a miss there is a validation error, handled by the ladder below. Non-advancing.
  if not _next_collectible_hard_value(slots, filled, pending):
    answer_result = _handle_answer(
        sm, config, last_user_text, slots, filled, pending, deferred,
        progressed=progressed_this_turn, channel=channel, inv_n=inv_n,
    )
    if answer_result is not None:
      return answer_result

  # "Answered, but nothing resolved" ladder (non-push_back slots). Runs BEFORE the error
  # handler so a fill unlocks its branch on THIS turn rather than costing another round
  # trip; the two are mutually exclusive by construction (_no_match_tick defers whenever
  # _slot_errors is set).
  _no_match_hit = _no_match_tick(sm, slots, last_user_text)
  no_match_msg = _no_match_hit[2] if _no_match_hit else ""

  error_msg, error_exhausted, error_fc, error_resp, error_verbatim = (
      _handle_slot_errors(sm, slots, channel=channel)
  )
  # The same `fill` disposition reached via the validation-error ladder (a rejected value
  # rather than an unresolvable one). It resolves the slot without ending the turn.
  no_match_msg = no_match_msg or sm.pop("_exhaust_fill_say", "")
  if error_msg:
    sm["_steer_back_turns"] = 0
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                preempted=error_msg, deferred=deferred)
    result = {"hide_tools": hide_tools, "preempt": True, "message": error_msg,
              "speech_class": "exhaust" if error_exhausted else "reprompt"}
    if error_verbatim:
      result["verbatim"] = True
    if error_fc:
      result["function_call"] = error_fc
    if error_resp:
      result["response"] = error_resp
      # The task-exhaust rung's twin: a slot's `validation.on_exhaust` can carry a
      # hand-off pair too, and its `end_session` ends the call the same way.
      _mark_end_session(sm, error_resp, "slot_exhaust")
    return result

  readback_transition = sm.pop("_readback_transition", False)

  # The wait's own deadline outranks the conversational ladders. Checked BEFORE
  # steer-back because that returns early, and a caller saying "are you still there?"
  # while a backend hangs would otherwise pre-empt the give-up and leave the flow
  # reassuring forever. A completion that landed on this turn still wins — the sweep
  # skips whatever `_task_just_completed` names.
  timeout_result = _sweep_async_timeouts(sm, tasks, filled)
  if timeout_result:
    return timeout_result

  steer_result = _handle_steer_back(
      sm, last_user_text, steer_back_cfg,
      slots, filled, pending, deferred, slot_map,
      fresh_pending, hide_tools, inv_n,
      channel=channel, progressed=progressed_this_turn,
  )
  if steer_result and steer_result.get("preempt"):
    return steer_result

  result, task_msg, task_resp = _handle_post_executor(
      sm, tasks, task_results, filled, pending, deferred,
      retries, confirm_transition_prefix,
      inv_n, phase, fresh_pending, hide_tools,
      channel=channel, config=config,
  )
  if result:
    return result

  # ── Progressive fan-out ─────────────────────────────────────────────────
  # A group whose legs were lowered onto the asynchronous/state path owns the turn
  # while any leg is still out: whatever just landed is spoken as a PARTIAL preempt
  # and another watcher goes out beside it, so the floor is never handed back
  # mid-group. Both calls are no-ops for every config with no group in flight, which
  # is what keeps an ungrouped (and an un-lowered) app byte-identical.
  _fanout_mark_pending(sm)
  fanout_result = _fanout_turn(
      sm, tasks, filled, task_results, hide_tools, task_msg, task_resp)
  if fanout_result is not None:
    return fanout_result

  # ── Remote jobs ─────────────────────────────────────────────────────────
  # Work running on a service that outlives this turn. Handles are marked in flight
  # here and every one still outstanding is polled together, so a caller waiting on
  # three jobs waits once. Both calls no-op for a config with no remote tool.
  #
  # UNGUARDED, like every sibling call on this path. Both read the config and `sm` and
  # nothing else — there is no payload from the service here, only a handle already in
  # a slot — so the failures a `try` could catch are engine bugs, which the suite sees
  # and a swallowed one would not. A blanket except was carried here for a while on the
  # theory that a raise inside a framework tool dies silently (it does: the tool returns
  # no `result` and `before_model` KeyErrors on the unwrap). It never caught anything:
  # the crash it was written for was a dict `max_retries` in `_handle_post_executor`,
  # thirty lines up and outside it.
  _remote_mark_pending(sm, config, tasks, filled)
  remote_result = _remote_turn(sm, config, tasks, hide_tools, task_msg, task_resp)
  if remote_result is not None:
    return remote_result

  # ── Announce slots (cascade through consecutive) ────────
  dag_result, announce_msgs, announce_responses, any_announce_preempt = (
      _cascade_announce(sm, config, slots, tasks, filled, pending, deferred,
                        task_results, slot_map, channel, collect_slot))
  if no_match_msg:
    # Speak the give-up line ahead of whatever the newly-resolved slot unlocked, so the
    # caller hears "no problem, I'll just check" and the result in one turn.
    announce_msgs.insert(0, no_match_msg)

  # Skip task fire on inline confirm — the LLM needs to run first
  # to process the additional content in the user's message. The
  # task will fire on the next before_model_callback invocation
  # after the LLM calls setters for the new content.
  _fired_this_call = set()
  # Defer a TERMINAL fire in two cases, both rendering this turn WITHOUT stacking
  # the executor onto it (the terminal stays eligible — its condition is over
  # `filled` — and fires cleanly on the next re-invoke, with then_say computed
  # from the executor result):
  #   1) announce_msgs — the terminal became eligible the SAME pass a fresh
  #      announce answered the user. Stacking the executor onto that answer
  #      dropped then_say and left the transfer in zombie limbo -> empty render.
  #   2) last_user_text — a NEW user turn brought unprocessed text while a prior
  #      announce (now persisted in `filled`, so announce_msgs is empty) still
  #      makes the terminal eligible. Preempt-firing the terminal here would
  #      hand back BEFORE the model reads the user's new message (a topic change,
  #      correction, or "no, that's all") — dropping it. A deferred terminal has
  #      no urgency to fire ahead of new input; it fires on the post-setter
  #      re-invoke, which carries no fresh user text (contents[-1] is the
  #      function_response). Zero new state: the within-turn re-invoke vs fresh
  #      user turn is already distinguished by last_user_text being empty (mirrors
  #      how _resume_offer / _intent_pass key off the turn).
  # Mirrors the inline_confirmed skip-fire. Immediate concludes (no announce, no
  # fresh user text) and non-terminal fires are unaffected.
  _fire_task = dag_result.get("task_def") or {}
  # A terminal task is normally deferred off a fresh-user-text pass so it fires on
  # the following (empty-text) re-invoke. Under intent_first the turn is already
  # classified (Pass A + any switch/cancel/correct resolve earlier), so deferring on
  # user text alone would strand the fire on a no-setter turn (e.g. a bare "yes"
  # confirmation) — fire it now instead. A pending announce still defers (speak first).
  # A fresh-user-text deferral is SKIPPED when that user text was deterministically consumed by an
  # option_cue prefill this turn (`_event_prefilled_this_turn`): there is then no unread user intent to
  # preserve, so firing the terminal now is correct — and it is the only thing that fires it, since a
  # deterministic fill (unlike a model setter call) produces no post-setter re-invoke to carry a deferred
  # terminal. Firing emits the executor as a function_call, which round-trips through CES and re-invokes
  # with empty text (yielding the next element via _frame_return). A pending announce still defers (speak
  # first). Paired with the in-frame freshness gate in _apply_option_cues (one utterance → one element),
  # this closes the repeated-collection element boundary without chaining.
  _defer_terminal = bool(
      dag_result["action"] == "fire" and _fire_task.get("terminal")
      and (announce_msgs
           or (last_user_text and not sm.get("_intent_first")
               and not sm.get("_event_prefilled_this_turn"))))
  # An `answer_first` budget makes the deferral BOUNDED for the duration of that budget.
  # The deferral itself is right — the caller said something and deserves an answer
  # before the flow closes out — but on a completion turn it is self-perpetuating, since
  # every later turn carries speech too. Spending a turn of the budget per deferral, and
  # firing once it is gone, is what turns "never" into "shortly". Untouched when no
  # budget is armed, which is every agent that has not opted in.
  if _defer_terminal and sm.get("_answer_first_left"):
    _left = int(sm["_answer_first_left"]) - 1
    if _left <= 0:
      sm.pop("_answer_first_left", None)
      _defer_terminal = False
      _log("answer_first_exhausted", task=_fire_task.get("name"))
    else:
      sm["_answer_first_left"] = _left
      _log("answer_first_turn", task=_fire_task.get("name"), left=_left)
  if _defer_terminal:
    _log("terminal_fire_deferred", task=_fire_task.get("name"))
  if dag_result["action"] == "fire" and not inline_confirmed and not _defer_terminal:
    task_def_f = dag_result["task_def"]
    task_name_f = task_def_f["name"]
    if task_name_f in _fired_this_call:
      _log("task_refire_blocked", "WARN", task=task_name_f)
    else:
      _fired_this_call.add(task_name_f)
      # The budget belongs to one completion→terminal handover; a later wait re-arms it.
      sm.pop("_answer_first_left", None)
      if "component" in task_def_f:
        # Descent seam (§4.2): a Component references a child DAG, not a tool, so
        # it is NOT a function call. Divert to the call-frame descent, which seeds
        # the child scope, pushes the frame, swaps the config, and ends the pass;
        # the selector walks the child next pass. Returns the same action-dict shape
        # this fire branch returns.
        # The announce cascade already marked its slot filled, so an authored line
        # that became eligible on THIS pass is never re-offered — park it (the tool
        # branch below merges the same text into combined_msg, so descending was the
        # one way to lose it permanently). Parking rather than writing it onto the
        # action is what carries it across the in-pass descent re-walk, which
        # discards this dict; _finalize_directive merges it onto whatever the pass
        # finally returns.
        if announce_msgs or announce_responses:
          sm["_carry_announce"] = {
              "msgs": list(announce_msgs),
              "response": list(announce_responses or []),
              "preempt": any_announce_preempt,
          }
        return _component_fire_action(sm, config, task_def_f, filled)
      tool_name = task_def_f["tool"]
      # Every leg of this pass's fan-out, or just this task when it is not in a group.
      # A component leg cannot ride a shared dispatch (a descent ends the pass, and it
      # is not a function call at all), so it is dropped here as well as rejected by
      # the validator — the primary task above already took the descent branch.
      legs = [leg for leg in (dag_result.get("group_tasks") or [task_def_f])
              if "component" not in leg]
      _fired_this_call.update(leg["name"] for leg in legs)
      merged_state = {**filled, **pending, **deferred}
      # The union of every leg's input slots, in declaration order. Taking only the
      # primary leg's here would re-defer a slot a SIBLING is about to be called with.
      leg_input_slots = []
      for leg in legs:
        for slot_name in _task_input_slots(leg["inputs"]):
          if slot_name not in leg_input_slots:
            leg_input_slots.append(slot_name)
      active_inputs = [
          s for s in leg_input_slots
          if _is_slot_active(slot_map[s], merged_state)
      ]
      args = _remote_wire_args(
          config, task_def_f.get("tool"),
          _task_input_args(task_def_f["inputs"], filled))
      for name in deferred_promoted:
        if name in pending and name not in active_inputs:
          deferred[name] = pending.pop(name)
          _log("re_deferred", slot=name, task=task_name_f)
      if readback_transition:
        sm["_deferred_transition"] = True
      sm["_last_state"] = {
          "filled": dict(filled),
          "pending": dict(pending),
          "deferred": dict(deferred),
      }
      combined_msg = task_msg
      if announce_msgs:
        announce_text = " ".join(announce_msgs)
        combined_msg = (
            f"{announce_text} {task_msg}"
            if task_msg else announce_text
        )
      # ── C2: spoken filler ("one moment…") appended to the fire message. ──
      # Gated on the surface's `filler` capability. A spoken filler masks dead air
      # on a call, but in a chat window it is a second bubble saying nothing — the
      # surface there shows a spinner instead, so the line is simply not emitted.
      filler_say = (_pick_filler(sm, task_def_f.get("filler_say"), filled)
                    if _cap("filler", True) else None)
      _log_suppressed_filler(task_def_f, task_name_f)
      if filler_say:
        combined_msg = f"{combined_msg} {filler_say}".strip() if combined_msg else filler_say
      _log_invoke(inv_n, phase, filled, pending, fresh_pending,
                  hide_tools, fired=task_name_f, deferred=deferred)
      # Every leg's tool must stay callable. A firing tool left in hide_tools renders
      # empty ("having trouble"), so a group that exempted only its first leg would
      # take the other N-1 down with it.
      fired_tools = {leg["tool"] for leg in legs}
      fire_hide = [t for t in hide_tools if t not in fired_tools]
      # ── C1: hold music while the (slow/blocking) tool runs. Emitted as an
      # audio part alongside the tool function_call; `cancellable` (default True)
      # stops it when the next response is generated. ──
      # `task_resp` FIRST: a task that completed earlier in this same turn may carry a
      # `then_response` — a closing disposition, a card. Every other preempting path
      # carries it inline (the fan-out and remote watchers, the terminal branch, the
      # `preempt_then_say` branch); this one returns long before `_route_payloads`, which
      # is where a non-preempting turn would have routed it. Left out, the payload is
      # SILENTLY DROPPED on any turn that goes on to dispatch something — so an authored
      # `end_session` reaches the caller when the turn happens to end there and vanishes
      # when it does not, which is a disposition that depends on scheduling. Ahead of the
      # fired task's own payloads, because it belongs to what just happened.
      fire_response = list(task_resp or []) + list(announce_responses or [])
      while_running = task_def_f.get("while_running")
      if isinstance(while_running, dict) and while_running.get("audioUri"):
        music = {"type": "audio", "audioUri": while_running["audioUri"],
                 "cancellable": while_running.get("cancellable", True)}
        for _k in ("interruptable", "transcript"):
          if _k in while_running:
            music[_k] = while_running[_k]
        fire_response.append(music)
      if len(legs) > 1:
        # Improvised filler works by handing the CALL to the model along with the line
        # (probes 27-32), a shape validated for exactly one call. A group emits several,
        # so it keeps engine dispatch and speaks its filler verbatim.
        handoff = None
        if filler_say:
          _log("filler_handoff_skipped_parallel", "WARN",
               group=dag_result.get("parallel"), legs=len(legs))
      else:
        handoff = _filler_handoff(
            sm, config, task_def_f, tool_name, args, filler_say, combined_msg,
            fire_response, fire_hide)
      if handoff is not None:
        return handoff
      # `count_into`: an integer slot the engine bumps each time this task fires, so a
      # cap can be a condition. The grammar compares numbers (`gte`) but nothing produced
      # one — every latch holds "true" — so counting N things meant a hook. Incremented
      # here, at dispatch, because that is the one place a fire is decided exactly once;
      # counting completions instead would miss a task that fires and fails.
      _count_slot = task_def_f.get("count_into")
      if _count_slot:
        filled[_count_slot] = int(filled.get(_count_slot) or 0) + 1
        _log("task_counted", task=task_name_f, slot=_count_slot,
             count=filled[_count_slot])
      fire_result = {
          "hide_tools": fire_hide,
          "preempt": True,
          "force_preempt": any_announce_preempt,
          "function_call": {"name": tool_name, "args": args},
          "message": combined_msg,
      }
      if len(legs) > 1:
        # The singular `function_call` above stays populated with the first leg, so
        # every existing consumer of this action — the improvisation livelock guard,
        # the preempt gate, the offline simulator, the docs transcript driver — keeps
        # working untouched. `function_calls` is emitted ONLY for a real group, which
        # is what makes a config with no group produce a byte-identical action.
        fire_result["function_calls"] = [
            {"name": leg["tool"],
             "args": _remote_wire_args(
                 config, leg["tool"],
                 _task_input_args(leg["inputs"], filled)) or {}}
            for leg in legs
        ]
      # Hold the turn open for whatever this dispatch DEFERS. A deferred call is launched
      # by the turn that dispatches it, and a turn that ends immediately afterwards loses
      # both the launch and any state written by a tool the deferred body calls nested —
      # silently, and at a rate (ces-probes 122-129). Unguarded, a multi-call preempt
      # landed 4 of 18 legs and 0 of 18 nested writes; with a 0.25s guard inline in the
      # SAME preempt, 18 of 18 and 18 of 18 (ces-probes 130 found that floor; 0.10s still
      # loses).
      #
      # Inline rather than on the following pass, which measures identically (129) but
      # costs a model pass — and a wide fan-out cannot spare one against the ten-pass cap.
      # Appended last so the calls it guards are dispatched first.
      if _dispatch_defers(task_def_f, legs):
        fire_result.setdefault("function_calls", [
            {"name": tool_name, "args": args}])
        fire_result["function_calls"].append({"name": _SETTLE_GUARD, "args": {}})
        _log("settle_guard_dispatched", tasks=[leg["name"] for leg in legs] or [task_name])
        # These legs run CONCURRENTLY, so `after_tool` is invoked once per leg with all
        # of them racing on the slot machine — a read-modify-write against one state key
        # loses N-1 of them outright, values included (ces-probes 37 and 38). The marker
        # tells `after_tool` to stand aside for exactly these tools; `before_model`, which
        # runs once per pass rather than once per leg, ingests the whole batch instead.
        # Declaration order, because arrival order is neither stable nor observable.
        #
        # ONLY for a real batch. A lone `awaits` task reaches this same branch — it needs
        # the guard just as much — but it has no siblings to race with, and standing
        # `after_tool` aside for it STRANDS its result. `before_model`'s compensating
        # ingestion routes a pending payload back through `sm["_fanout"]["tools"]`, which
        # only `_fanout_start` fills, and that runs for a progressive GROUP alone. With
        # neither path taking it the placeholder never reaches intake, `_awaiting_async`
        # is never marked, and the selector — still seeing the task un-fired — dispatches
        # it again on every pass until the ten-pass cap (72) kills the turn. Measured on a
        # converted agent as nine dispatches, nine responses and nine guards inside one
        # turn; ces-probes 148 reproduces it against a synchronous control that completes
        # on the fire turn. One leg cannot race itself, so `after_tool` ingests it as it
        # always did and the `awaits` branch records the wait.
        if len(legs) > 1:
          sm["_parallel_firing"] = [leg["tool"] for leg in legs]
        _log("parallel_fire", group=dag_result.get("parallel"),
             tasks=[leg["name"] for leg in legs])
        # A progressive group's legs are ASYNCHRONOUS by lowering, so this same
        # dispatch answers `pending` and the results arrive through state instead.
        # The action is unchanged — the watcher goes out on the NEXT pass, once the
        # legs are genuinely running — so a group that was never lowered dispatches
        # byte-identically and this is bookkeeping the rest of the turn ignores.
        if dag_result.get("parallel") in _progressive_groups(tasks):
          _fanout_start(sm, dag_result["parallel"], legs)
          # Clear last run's publications. The legs write to state, and state outlives
          # the group — a re-fire would otherwise read the PREVIOUS run's result as
          # this one's, instantly, and narrate a stale finding before the backend had
          # even been asked.
          fire_result["state_writes"] = {
              "pop": [f"{dag_result['parallel']}_{leg['name']}" for leg in legs]}
      # Mark a plain sync fire in-flight so it isn't re-emitted later this turn before its
      # result lands (#698 loop). Awaits/groups already have _awaiting_async/_parallel_firing.
      if len(legs) == 1 and not _dispatch_defers(task_def_f, legs):
        sm.setdefault("_sync_fire_pending", {})[task_name_f] = sm.get("_turn_n", 0)
      if fire_response:
        fire_result["response"] = fire_response
      return fire_result

  dag_msg = dag_result.get("system_message", "")
  # The all_done completion sentinel ("All information collected!") is an INTERNAL
  # signal, never a user-facing line. It must not reach the spoken/model surface
  # (it renders in the canned TTS voice via the preempt fold / empty-render
  # backstop). Real closings come from a terminal task's then_response or a closing
  # announce; a task_msg on the same turn is preserved below.
  if dag_result.get("action") == "all_done":
    dag_msg = ""
    # Nothing left to collect, but an ASYNCHRONOUS tool is still out. This is a WAIT,
    # not a finished flow: without the deliberate silent hold the turn proceeds with no
    # directive and the model, seeing an idle conversation, invents something to say
    # while the backend works. `awaits.say` (when set) was already spoken on the turn
    # the wait began; a `while_waiting` ladder covers the turns after it, and silence
    # is the default once (or unless) it is spent.
    # BOTH delivery channels, and the second one is why this reads oddly. An announce
    # speaks through `message` (-> announce_msgs) or through verbatim `response` parts
    # (-> announce_responses); `announce(name, texts)` populates only the second, and
    # `announce(message=...)` only the first. Testing announce_msgs alone meant a `texts`
    # announce that won an idle turn was DESTROYED here: the cascade had already latched
    # it (`filled[name] = True`), the hold returned without its parts, and the scan skipped
    # it forever after. Authored copy, spoken nowhere, once, silently.
    if (sm.get("_awaiting_async") and not task_msg
        and not announce_msgs and not announce_responses):
      # A turn the caller SPOKE on is not dead air, and a reassurance line is only ever
      # cover for dead air. `_async_idle_line`'s own docstring says so ("only consulted
      # when the turn would otherwise be dead air"); this is the test that was missing to
      # make it true. From inside the engine a turn the caller is spending THINKING looks
      # exactly like an inactivity tick, so the ladder drew a line on it and spoke over
      # them.
      #
      # It costs the answer, not just the manners, and on voice that loss is total.
      # Driven on a repair agent that asks a scoping question during a thirty-second
      # diagnostics job: the caller said "Uh" at 45.4s, the ladder answered "Still
      # running those checks" at 46.1s, and the caller's real answer arrived at 46.3s ON
      # TOP OF IT. The platform recorded a barge-in, cancelled the line — and the
      # utterance never reached `llm_request.contents` at all. The engine's view of the
      # caller stayed frozen on "Uh" for the remaining forty seconds of the call
      # (`scanned_user_text` unchanged across every later invocation), the cue-only
      # capture slot never filled, and the caller was asked the same question again after
      # the verdict. Reproduced 2 of 2 with the answer timed onto the line and 0 of 4
      # with it timed into a gap, which is what made this look like flake.
      #
      # Hold SILENTLY instead, and do not consume a ladder line: the wait has not gone
      # quiet, so the reassurance is still owed and the next genuine tick will pay it.
      # Silence is also the only safe thing to do here — this branch exists because
      # letting the model take a wait turn is how it improvises — and the caller's words
      # are not lost by it: every deterministic capture (event prefill, DTMF, option
      # cues, slot defaults) has already run against them, above the DAG.
      #
      # Byte-identical for a wait with no `while_waiting` ladder, and for one whose
      # turns are all genuine ticks.
      _spoke_this_turn = bool((last_user_text or "").strip())
      _hold_line = "" if _spoke_this_turn else _async_idle_line(sm, tasks, filled)
      _log("async_await_idle_hold", tasks=sorted(sm["_awaiting_async"]),
           spoken=bool(_hold_line), caller_spoke=_spoke_this_turn)
      hold = {"hide_tools": hide_tools, "preempt": True, "message": _hold_line,
              "speech_class": "await"}
      if not _hold_line:
        hold["silent"] = True
      return hold
    # An announce that wins a WAIT turn is spoken inline whatever its `preempt`. The
    # non-preempting path stashes parts for the model to render on a later turn
    # (`_route_payloads`), and a wait turn is the one place that cannot be allowed: the
    # hold above exists precisely because handing the model a free turn mid-wait is how
    # it invents. Deferring the announce would recreate that, one turn later.
    if sm.get("_awaiting_async") and announce_responses:
      any_announce_preempt = True
  # An announce-preempt pair puts the question in the announce; the capture slot's
  # `ask` exists for the STANDALONE re-ask (a validation retry, where no announce
  # fires). Both are emitted on the announce turn and concatenated below, so when the
  # ask adds no words the announce did not already say, the caller hears the question
  # twice — "What's your 5-digit ZIP code? What's your 5-digit ZIP code?".
  #
  # Containment, not equality: a prefixed announce ("Last one — what's your 9-digit
  # Social Security Number?") still swallows its ask. And containment, not a blanket
  # drop: a COMPLEMENTARY ask contributes new words and must survive — the KBA pairs
  # read "<question> … 5 None of the above. Please say the number of your answer.",
  # where the trailing instruction is the only thing telling the caller how to reply.
  # Suppression is scoped to this turn only; the re-ask path never has announce_msgs.
  # Scoped to THIS invocation on purpose. An earlier attempt accumulated the announce
  # text across a turn's invocations, to also catch the flow-entry question (whose
  # announce leaves on the task-fire path while the `ask` is emitted by a later
  # invocation). That REGRESSED both auth evals: on the later invocation the ask is the
  # only content, so suppressing it produced an empty agent turn, the caller's answer
  # landed while the agent was still asking, and the whole conversation slipped one turn
  # — set_pii never fired on 03-phone. Suppress a duplicate only when something else is
  # being spoken in the same breath; never let this rule create silence.
  _announce_text = " ".join(announce_msgs) if announce_msgs else ""
  if (dag_msg and _announce_text
      and dag_result.get("action") == "next_question"
      and _norm_for_dup(dag_msg) in _norm_for_dup(_announce_text)):
    _log("ask_dedup", slot=dag_result.get("slot_name"))
    dag_msg = ""
  if task_msg and dag_msg:
    msg = f"{task_msg} {dag_msg}"
  else:
    msg = task_msg or dag_msg
  if announce_msgs:
    announce_text = _announce_text
    msg = (
        f"{announce_text} {msg}" if msg
        else announce_text
    )
  sm["_last_state"] = {
      "filled": dict(filled), "pending": dict(pending),
      "deferred": dict(deferred),
  }
  # `fresh_pending` asks "did pending change since the last ENGINE invocation",
  # which is not the same question as "has this readback been spoken yet". One
  # user turn can invoke the engine twice — a task fires, returns early, and on
  # the way out records the pending it just staged into _last_state — so the
  # second invocation sees fresh_pending False for a readback nobody has heard.
  # The verbatim preempt below was gated on freshness alone and so could never
  # fire on those turns, silently handing a determinism-critical readback to the
  # LLM to paraphrase. Track what was actually SPOKEN instead of inferring it.
  # Scoped to readback_verbatim slots so unflagged slots keep the old behaviour.
  _rb_slots = sorted(pending)
  _rb_unspoken = (
      bool(_rb_slots)
      and any((slot_map.get(_p) or {}).get("readback_verbatim") for _p in pending)
      and sm.get("_readback_spoken") != _rb_slots
  )
  if not pending:
    # A cleared readback re-arms: rejecting a value and re-collecting the SAME
    # slot must be spoken verbatim again, not suppressed as already-spoken.
    sm.pop("_readback_spoken", None)

  readback_fallback = ""
  if (dag_result["action"] == "awaiting_readback" and not fresh_pending
      and not _rb_unspoken):
    # Non-fresh readback is rendered by the LLM so it can rephrase naturally and
    # act on the user's input — confirm/reject, a correction, or a value the
    # user volunteers out of order (captured via the sibling setter kept visible
    # during readback, or routed through set_slot_change). Remember the readback
    # prompt as this turn's render fallback (armed below) so the after_model
    # backstop can re-ask deterministically if the model returns no content.
    readback_fallback = dag_msg or "Just to confirm — is that correct?"
    msg = ""

  event_prefilled = sm.get("_event_prefilled_this_turn", False)
  # Readback preemption decision. The sm pops happen HERE (they must fire exactly
  # once per turn regardless of outcome); the pure gating lives in
  # _readback_disposition so it stays unit-testable as a truth table. See that
  # function's docstring for what each of the four signals means.
  restored_flow = sm.get("_restored_flow", False)
  _rb = _readback_disposition(
      dag_result["action"], fresh_pending, pending, filled,
      post_correction_flag=sm.pop("_post_correction_readback", False),
      restored_flow=restored_flow,
      promoted_from_deferred=promoted_from_deferred,
      correction_unresolved_flag=sm.pop("_correction_unresolved", False),
  )
  post_correction_readback = _rb["post_correction_readback"]
  override_readback = _rb["override_readback"]
  promoted_readback = _rb["promoted_readback"]
  unresolved_correction = _rb["unresolved_correction"]
  if promoted_readback or not restored_flow:
    sm.pop("_restored_flow", None)
  if unresolved_correction and not msg:
    rb = _build_readback(slots, pending, filled, config=config, channel=channel)
    msg = (dag_result.get("system_message")
           or (rb.get("system_message") if rb else "")
           or "Just to confirm — is that correct?")
  preempt = (
      bool(task_msg) or any_announce_preempt
      or event_prefilled or post_correction_readback
      or override_readback or promoted_readback
      or unresolved_correction
  )
  # DETERMINISM (readback): `readback_verbatim: True` speaks the ENGINE's readback text
  # exactly, model bypassed, instead of relaying it for the LLM to rephrase. Only the
  # FIRST presentation is preempted — that turn is the engine stating the value back, so
  # there is nothing of the caller's left to extract. Their confirm / reject / correction
  # arrives on the NEXT turn, which stays model-rendered, so inline confirmation ("yes,
  # and also…") and out-of-order values keep working.
  # Why this exists: a relayed readback can be re-rendered as a DIFFERENT question — a
  # street-address confirmation came back as "what's the last four of your SSN?", which
  # appears in no DAG, and cost three turns. Pair with `readback_fmt`: preempting speaks
  # the raw slot value, so an unformatted digit string would reach TTS as one number.
  if (dag_result.get("action") == "awaiting_readback"
      and (fresh_pending or _rb_unspoken) and msg
      and any((slot_map.get(_p) or {}).get("readback_verbatim") for _p in pending)):
    preempt = True
    sm["_readback_spoken"] = _rb_slots
  if readback_transition and msg and confirm_transition_prefix:
    if not msg.lower().startswith(confirm_transition_prefix.lower()):
      msg = f"{confirm_transition_prefix} {msg}"
    # Only force-preempt if there's no fresh readback to rephrase, AND not after
    # an inline-confirm. When the user confirmed AND added new info in one
    # message ("yes that's right, and let's do Friday"), the next action is a
    # next_question — preempting it skips the LLM and DROPS the new info. Let the
    # LLM run so it extracts the added value first.
    if (not (fresh_pending and dag_result["action"] == "awaiting_readback")
        and not inline_confirmed):
      preempt = True

  # ── B1c: barge-in — replay what the caller did not hear ─────────────────
  # Fires on a turn the platform reported as an interruption. Two things can be true and
  # they are handled in this order:
  #
  #   1. an announce that declared `repair=` was cut off. Replay the parts the caller
  #      never reached, read out of the ledger the cascade wrote LAST turn. This never
  #      re-enters `_cascade_announce` and never touches `filled`, so no condition is
  #      re-evaluated and no downstream gate re-fires -- the regression documented at the
  #      announce-latch exemption above.
  #   2. otherwise the flow's `on_interrupted` policy speaks, if it has one.
  #
  # Nothing here fires for a flow that declares neither, which is what keeps
  # `barge_in_awareness` defaulting on from changing any existing agent.
  _oi_cfg = config.get("on_interrupted") or {}
  # A backchannel over a line is the case repair exists for, so it resumes by default;
  # `resume_on_continuer=False` makes a following-along cue merely absorb the turn.
  _barge_resume = (_oi_cfg.get("resume_on_continuer", True)
                   or not sm.get("_continuer"))
  if is_barge_in and _barge_resume:
    _replay, _exact = _plan_repair(sm, _oi_cfg, barge_heard)
    _lead = _replay or (_interrupted_line(_oi_cfg, sm, filled, barge_heard)
                        if _oi_cfg else "")
    if _lead:
      # PREPENDED, not substituted. The caller missed something AND is still owed whatever
      # the flow was about to say; dropping the question to deliver the replay strands the
      # turn, and dropping the replay to ask the question is the bug this exists to fix.
      # "As I was saying — <what was missed>. <the pending question>".
      msg = f"{_lead} {msg}".strip() if msg else _lead
      preempt = True
      speech_class = "interrupted"
      _log("barge_repair" if _replay else "barge_say",
           chars=len(_lead), exact=_exact)
    if _oi_cfg:
      _open = _oi_cfg.get("open_slot")
      if _open and not filled.get(_open):
        # Same shape as the silence exhaust's offer: arm a REAL slot so the author can
        # gate on it with an ordinary condition, and latch it so the next pass's
        # conditional sweep does not clear a flag whose condition is False by design.
        filled[_open] = True
        _armed_oi = sm.setdefault("_armed_offer_flags", [])
        if _open not in _armed_oi:
          _armed_oi.append(_open)
        _log("barge_open_slot", slot=_open)

  _no_input_fc = None  # a silence exhaust may resolve on_exhaust.then -> function_call (B2, below)
  _no_input_silent = False  # an EMPTY no_input reprompt = a silent wait tick (suppress the model)
  _no_input_end_session = False  # a silence exhaust with end_conversation ends the whole call
  # ── B2: no-input (silence) reprompt ladder ──────────────────────────────
  # When the caller is silent (empty input) on a question we already asked, walk
  # the FLOW-LEVEL `no_input.reprompts` ladder, then run `on_exhaust`. Silence is a
  # single flow-level policy (config["no_input"]) applied to whichever user slot was
  # asked last turn (sm["_awaiting"]); a config without a flow-level `no_input` is
  # completely unaffected. on_exhaust may `say` a line AND/OR fire a tool via `then`
  # (transfer/end_session/custom) — see below.
  # Advance ONLY on a genuine CES inactivity turn (is_inactivity). A post-setter /
  # steer-back re-invoke also has empty last_user_text but must NOT count as silence.
  _ni_cfg = config.get("no_input")
  _ni_cfg = _ni_cfg if isinstance(_ni_cfg, dict) else None
  # Who took the turn, as resolved by the entry point above and parked on sm -- read
  # rather than threaded, because it is a fact about the turn the whole pass shares, not
  # an argument this one branch needs. The fallback is the reading everything used before
  # `turn_kind` existed, so a caller reaching here without one behaves exactly as it did.
  _turn_kind = sm.get("_turn_kind") or ("manufactured" if is_inactivity else "caller")
  # Which authored utterance the aggregate `final` message came from, for the
  # improvise policy. Only the silence lines set it: every other branch that reaches
  # `final` is an ask, which the model already rewords by default.
  speech_class = ""
  # Within-turn only: set by the hold_ack branch below and read by the
  # slot-transition reset further down, both on THIS pass. Deliberately a local
  # rather than an sm key — as sm state it survived any turn where the awaited slot
  # did not change (i.e. every hold that arrives AFTER the question was put), and
  # then suppressed the hold-mode reset on a later, unrelated transition.
  _hold_ack_spoken = False
  # `_hold_text`, not `last_user_text`: the entry turn carries speech even though
  # the model's last message does not. Everything inside this branch is
  # idempotent on an entry pass (the no-input counter is zero, so the
  # re-engagement reset is a no-op), and the ack itself is gated on
  # `_hold_requested`, which only the block above sets.
  if _hold_text:
    # An explicit hold request ("hold on", "give me a second") enters HOLD mode:
    # subsequent silence then waits SILENTLY (hold_reprompts). Plain silence with no
    # hold request reprompts out loud (reprompts) instead. (_hold_on is already
    # set/cleared for this utterance before steer-back, above.)
    # Real speech = re-engagement. Reset the silence window, but only for a capped
    # number of extensions (Conv 05b); past the cap, keep the counter so the next
    # silence exhausts to the escalation offer.
    if sm.get("_no_input_counter"):
      if sm.get("_hold_extensions", 0) < _HOLD_EXTENSION_CAP:
        sm["_hold_extensions"] = sm.get("_hold_extensions", 0) + 1
        sm.pop("_no_input_counter", None)
    else:
      sm.pop("_hold_extensions", None)
    # `hold_ack`: the caller SPOKE a hold request, so answer it instead of putting
    # the question again. Asking anyway is the one reply "give me a second" rules
    # out, and until now it was the only thing that could happen — the hold was
    # detected but only ever fed the silence ladder and the steer-back exemption,
    # neither of which changes what is said on a turn that carries text.
    #
    # Gated on nothing having progressed, so "hold on — actually it's 555 0123"
    # still fills and moves on. The question is NOT consumed: `_awaiting` is
    # unchanged below, so the following silence picks up the hold_reprompts ladder
    # exactly as before.
    # Popped unconditionally — the mark is spent on the turn it was set whether or
    # not an ack is configured, otherwise it lingers in sm for the whole call.
    _hold_requested = sm.pop("_hold_requested", False)
    _ack = (_ni_cfg or {}).get("hold_ack")
    # Deliberately NOT `progressed_this_turn`, which is true on the very first
    # pass simply because the state went from nothing to a pending question — it
    # would suppress the ack exactly on the turn it is most wanted (the caller who
    # asks for time in the same breath as the complaint). What matters is whether
    # the CALLER put a value in this turn.
    #
    # Restricted to the slots the caller is actually ASKED for — user-source and not
    # passive. Two things otherwise masquerade as an answer on the opening turn: an
    # author's before_agent hook seeding a dozen event slots, and an option_cue
    # prefilling a passive intent slot from the very utterance that asked to hold.
    # Both suppressed the ack on the first real agent this was wired to.
    _prev = last_state or {}
    _user_slots = {s["name"] for s in slots
                   if "user" in _normalize_sources(s.get("source", "user"))
                   and not s.get("passive")}
    _supplied = _user_slots & (
        (set(filled) | set(pending))
        - (set(_prev.get("filled") or {}) | set(_prev.get("pending") or {})))
    if (_ack and _hold_requested
        and dag_result["action"] == "next_question"
        and not _supplied):
      msg = _safe_format(_ack, filled)
      preempt = True
      speech_class = "no_input"
      _hold_ack_spoken = True
      _log("no_input_hold_ack", slot=dag_result.get("slot_name"))
  elif ((is_inactivity or _turn_kind == "manufactured")
        and dag_result["action"] == "next_question"):
    _ni_slot = dag_result.get("slot_name")
    # WIDENED, not replaced, and `is_inactivity` moves to the inner guard rather than
    # being dropped: "manufactured" is not a superset of it. A barge-in marker arriving
    # alongside an inactivity envelope classifies as the CALLER acting (ces-probes 161)
    # while this flag is still set, because before_model derives it from last_user_text
    # after the fallback that picks up the envelope. Replacing the head would have
    # quietly stopped that turn entering the reprompt ladder. As written the inner test
    # is the head that was here before, so no_input takes exactly the turns it took.
    if is_inactivity and _ni_cfg and sm.get("_awaiting") == _ni_slot:
      # Hold mode waits silently (hold_reprompts, empty = silent tick); plain
      # no-input reprompts out loud (reprompts). Falls back to reprompts.
      _reprompts = ((_ni_cfg.get("hold_reprompts") if sm.get("_hold_on")
                     else None) or _ni_cfg.get("reprompts") or [])
      _cnt = sm.get("_no_input_counter", 0)
      if _cnt < len(_reprompts):
        try:
          msg = _reprompts[_cnt].format(**filled)
        except (KeyError, IndexError):
          msg = _reprompts[_cnt]
        sm["_no_input_counter"] = _cnt + 1
        preempt = True
        speech_class = "no_input"
        # An empty reprompt is a silent wait tick (suppress the model); a non-empty
        # one speaks normally via the preempt message.
        _no_input_silent = not (msg or "").strip()
        _log("no_input_reprompt", slot=_ni_slot, attempt=_cnt + 1,
             silent=_no_input_silent)
      else:
        _exhaust = _ni_cfg.get("on_exhaust") or {}
        _open = _exhaust.get("open_slot")
        _comp = _exhaust.get("component")
        if _open and not filled.get(_open):
          # Arm a flag and re-select so an IN-FLOW offer slot (its condition keys off
          # the flag) is asked this turn. The offer stays in the SAME flow scope, so a
          # value the caller supplies AT the offer (e.g. finally reading the number) is
          # captured by the flow's setters — not lost to an isolated child component.
          filled[_open] = True
          # Latch the armed flag so _deactivate_conditional_slots doesn't clear it
          # next pass (its condition is False by design); the offer must persist
          # across the model's proceed turn instead of collapsing after this one.
          _armed = sm.setdefault("_armed_offer_flags", [])
          if _open not in _armed:
            _armed.append(_open)
          sm.pop("_hold_on", None)
          _reselect = _find_next_question(
              slots, filled, pending, slot_map, deferred=deferred,
              channel=channel, sm=sm)
          if (_reselect.get("action") == "next_question"
              and _reselect.get("slot_name") != _ni_slot):
            dag_result = _reselect
            msg = _reselect.get("system_message", "")
            preempt = True
            _log("no_input_open_slot", slot=_open,
                 offer=_reselect.get("slot_name"))
            # Mark this a silence-armed escalation OFFER so its pending turns hide
            # cancel_flow (abandon footgun); see the hide block after _awaiting.
            sm["_no_input_offer_slot"] = _reselect.get("slot_name")
          else:  # offer slot unreachable (misconfig) -> graceful close
            _ex_say = _exhaust.get("say")
            if _ex_say:
              try:
                msg = _ex_say.format(**filled)
              except KeyError:
                msg = _ex_say
            preempt = True
            sm["status"] = "complete"
            _log("no_input_open_slot_unreachable", "WARN", slot=_open)
        elif _comp:
          # Silence exhausted -> descend into the offer/help Component (a real child
          # DAG). The descent ends the pass; the spine re-walks the child in-pass so
          # the offer renders now, and the child's own terminals close the call.
          sm.pop("_no_input_counter", None)
          sm.pop("_hold_extensions", None)
          sm.pop("_hold_on", None)
          _synth = {
              "name": f"_no_input_offer:{_ni_slot}",
              "component": _comp,
              "inputs": _exhaust.get("inputs") or {},
              "outputs": _exhaust.get("outputs") or {},
              "on_abort": "skip",
          }
          _log("no_input_exhaust_component", slot=_ni_slot, child=_comp)
          return _component_fire_action(sm, config, _synth, filled)
        else:
          _ex_say = _exhaust.get("say")
          if _ex_say:
            try:
              msg = _ex_say.format(**filled)
            except KeyError:
              msg = _ex_say
          preempt = True
          # Resolve `then` (a disposition tool) REGARDLESS of end_conversation. A silence
          # exhaust that both FIRES a tool AND ends the call must do both — e.g.
          # updated_billing_transfer_call(action="hangup") then end_session — and the
          # final-action assembly already carries the function_call alongside the
          # end_session part (the same shape the task-exhaust rung uses). The earlier
          # if/else dropped the tool whenever end_conversation was set, so a "say + then +
          # end_conversation" close silently skipped its tool call.
          _no_input_fc = _resolve_exhaust_action(_exhaust, filled)
          if _exhaust.get("end_conversation"):
            # end_conversation -> end the WHOLE call. Inside a component frame (caller
            # silent through an offer child), pop the call stack so the end_session
            # survives the spine's child-terminal frame-abandon guard; otherwise the
            # exhaust would frame-return to the exhausted parent slot and loop.
            sm.pop("_call_stack", None)
            _no_input_end_session = True
            sm["status"] = "complete"
            _log("no_input_exhaust_end_conversation", slot=_ni_slot,
                 fired=bool(_no_input_fc))
          else:
            sm["status"] = "escalated" if _no_input_fc else "complete"
            _log("no_input_exhaust", slot=_ni_slot)
        sm.pop("_no_input_counter", None)
    # A SNAPSHOT turn cannot answer "was this question already put" from sm, because
    # `_awaiting` is one of the fields that rolled back (see `_sm_stale` above). What it
    # can still answer is the one case where speaking is right: a question the landing
    # completion has just UNBLOCKED. Anything the completion did not unblock was already
    # reachable before the call was made, so it was already asked -- and on a live drive
    # it was, on the caller's own turn, which is the whole complaint.
    _stale = bool(sm.get("_sm_stale"))
    _unblocked = set()
    if _stale and _ni_slot:
      _landed = next((t for t in (config.get("tasks") or [])
                      if t.get("name") == sm.get("_stale_landed_task")), None)
      # `_output_targets`, not `.values()`: an `outputs` value may name ONE slot or a
      # list of them, and handing a list straight to `set()` raises `unhashable type`.
      # It would raise here, on a completion push -- the turn a caller is already
      # waiting through, where a crash reads as the platform's "having trouble"
      # envelope. The helper flattens both shapes and every other reader uses it.
      _unblocked = (set((slot_map.get(_ni_slot) or {}).get("requires") or [])
                    & set(_output_targets((_landed or {}).get("outputs"))))
    if _stale and _ni_slot and not _unblocked and not preempt:
      msg = ""
      preempt = True
      _no_input_silent = True
      _log("manufactured_turn_silent", slot=_ni_slot, kind=_turn_kind, stale=True)
    elif sm.get("_awaiting") == _ni_slot and not preempt:
      # The turns no_input above did not claim: an asynchronous completion push (never
      # a silence, so it does not reach that ladder at all), and a tick at a flow that
      # declares no silence policy. Both used to fall through here and put the question
      # AGAIN. Measured on a deployed agent over voice, the same wording landed at
      # 20.3s, 30.3s and 45.6s -- the caller's own turn, then a completion push, then a
      # tick -- while they were still thinking about the first one.
      #
      # So say nothing. The rung is untouched, which is already how `_ask_rung` behaves
      # within a turn, and `_no_input_counter` is untouched too: a completion push is
      # not silence and must not spend the caller's patience budget. `_awaiting` is left
      # standing by the bookkeeping below, so the question simply stays outstanding.
      #
      # Guarded on `_awaiting` because only a question already PUT to this caller may be
      # withheld. A first ask, and any new question a completion unblocks, still speaks
      # -- which is the whole point of being able to poll for free. `not preempt` yields
      # to anything that has already claimed the turn, an announce most of all: an
      # author who wants a line on the poll writes one.
      msg = ""
      preempt = True
      _no_input_silent = True
      _log("manufactured_turn_silent", slot=_ni_slot, kind=_turn_kind)
  # Track the slot we're asking so a following silent turn is recognized as
  # no-input for THAT slot (not the entry/welcome turn). Moving to a different slot
  # is progress, so the silence window + extension budget reset.
  _next_await = dag_result.get("slot_name") if dag_result["action"] == "next_question" else None
  if _next_await != sm.get("_awaiting"):
    sm.pop("_no_input_counter", None)
    sm.pop("_hold_extensions", None)
    # A new slot = progress; leave hold mode. But the FIRST ask is not a
    # transition — there was no previous slot — and a caller who asked to hold in
    # the same breath as their opening complaint would otherwise be dropped
    # straight back out of hold mode, so the silence after the ack would nag with
    # the loud reprompt ladder instead of waiting quietly. Keep the hold they just
    # asked for.
    if not _hold_ack_spoken:
      sm.pop("_hold_on", None)
  if _next_await:
    sm["_awaiting"] = _next_await
    # Snapshot for the no-match ladder: the question we just put, the turn we put it on,
    # and how far the conversation had got. Next turn compares against this to decide
    # whether anything at all happened.
    if (sm.get("_await_mark") or {}).get("slot") != _next_await:
      sm["_await_mark"] = {"slot": _next_await, "turn": sm.get("_turn_n", 0),
                           "sig": _progress_sig(sm)}
  else:
    sm.pop("_awaiting", None)
    sm.pop("_await_mark", None)

  # While a silence-armed escalation OFFER slot is still pending, hide cancel_flow
  # so the model can't ABANDON the caller instead of accepting the offer or
  # transferring. transfer_to_human and the offer's own setter stay visible.
  _offer_slot = sm.get("_no_input_offer_slot")
  if _offer_slot and not filled.get(_offer_slot) and sm.get("_awaiting") == _offer_slot:
    if _CONTROL_TOOLS[_CANCEL_SLOT] not in hide_tools:
      hide_tools.append(_CONTROL_TOOLS[_CANCEL_SLOT])
  elif _offer_slot:
    sm.pop("_no_input_offer_slot", None)  # offer resolved or superseded

  # Empty-render backstop arming (single site; msg + preempt are final here). On
  # a proceed (model-renders) turn the engine hands a deterministic message to the
  # LLM and discards it — before_model only delivers `message` on the PREEMPT
  # path. If the LLM then returns nothing, after_model re-emits this fallback
  # instead of letting CES surface "Hmm, I'm having trouble". A preempt turn
  # delivers its message directly, so it needs no fallback (already cleared at the
  # top of this function). The readback proceed falls back to its readback prompt;
  # every other proceed (next_question / all_done) falls back to `msg`.
  fallback_text = "" if preempt else (readback_fallback or msg)
  if fallback_text:
    sm["_render_fallback"] = fallback_text

  action = dag_result["action"]
  if preempt:
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                preempted=msg, deferred=deferred)
  elif action == "next_question":
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                asking=msg or dag_result.get("system_message", ""),
                deferred=deferred)
  elif action == "awaiting_readback" and fresh_pending:
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                reading_back=dag_result.get("system_message", ""),
                deferred=deferred)
  else:
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                done=(action == "all_done"), deferred=deferred)

  deferred_hint = ""
  if fresh_deferred and not pending:
    next_q = _find_next_question(
        slots, filled, pending, slot_map, deferred=deferred,
        channel=channel, sm=sm,
    )
    next_msg = next_q.get("system_message", "")
    if next_msg:
      deferred_names = sorted(set(deferred) - set(last_deferred))
      deferred_hint = (
          f"The value(s) just collected ({', '.join(deferred_names)})"
          f" are noted and will be confirmed later together with"
          f" related information. Do NOT read them back or ask for"
          f" confirmation now. Instead, proceed to ask: {next_msg}"
      )

  si_suffix = _build_si_suffix(
      config, slots, pending, filled, fresh_pending,
      promoted_from_deferred, deferred_hint, deferred,
      suppress_readback_hint=bool(collect_slot),
      repeat_acc=sm.get("_repeat_acc"),
  )

  # A group's all-done line is an announce, and announces are evaluated before tasks —
  # so on the pass a fan-out settles, the closing line would be assembled AHEAD of the
  # per-leg lines it is closing ("That's everything. You have two items."). On that pass
  # only, the legs speak first. Popped unconditionally so the flag cannot leak forward.
  _legs_spoke_this_pass = sm.pop("_parallel_batch_spoke", False)
  combined_response = announce_responses or []
  if task_resp:
    combined_response = (task_resp + combined_response if _legs_spoke_this_pass
                         else combined_response + task_resp)
  dag_response = dag_result.get("response")
  if dag_response:
    combined_response = combined_response + dag_response
  # A silence exhaust with end_conversation ends the whole call: speak the graceful
  # close (msg) and carry an end_session part so CES tears the session down.
  if _no_input_end_session:
    combined_response = combined_response + [
        {"type": "end_session", "reason": "completed"}]

  # First-turn ordering fix: on a PREEMPT that emits announce/welcome parts AND a
  # question message, the greeting parts must be spoken BEFORE the question.
  # `before_model._preempt_parts` emits `message` before `response`, so fold the
  # question into the END of the response as a text part and clear the message —
  # the whole turn becomes one correctly-ordered list (chime → brand → language →
  # disclaimer → greeting → question) instead of the question being stitched first.
  if preempt and announce_responses and msg:
    # ...EXCEPT on the pass a fan-out settles. There the message holds the legs' own
    # lines and the announce is the group's all-done line, which closes them — appending
    # would say "That's everything. You have two items."
    _part = {"type": "text", "text": msg}
    combined_response = ([_part] + combined_response if _legs_spoke_this_pass
                         else combined_response + [_part])
    msg = ""

  # A one-shot line to speak BEFORE everything else this turn (menu-returning cancel).
  # `before_model._preempt_parts` emits `message` first and `response` parts after, so
  # prepending a part is not enough on its own — live that produced "What are you calling
  # about today? No problem." The question is folded to the END of the parts and the
  # message cleared, exactly as the first-turn announce ordering fix above does, so the
  # whole turn is one correctly ordered list.
  lead_in = sm.pop("_lead_in", "")
  if lead_in:
    combined_response = [{"type": "text", "text": lead_in}] + combined_response
    if msg:
      combined_response = combined_response + [{"type": "text", "text": msg}]
      msg = ""
    preempt = True

  final = {
      "hide_tools": hide_tools,
      "preempt": preempt,
      "force_preempt": (
          any_announce_preempt or inline_confirmed
          or post_correction_readback or override_readback
          or promoted_readback or unresolved_correction
      ),
      "message": msg,
      "si_suffix": si_suffix,
      "inline_confirmed": inline_confirmed,
      "collect_partial": bool(collect_slot),
  }
  if speech_class:
    final["speech_class"] = speech_class
    if (_ni_cfg or {}).get("verbatim"):
      final["verbatim"] = True
  if steer_result and steer_result.get("steer_back_directive"):
    final["steer_back_directive"] = steer_result["steer_back_directive"]
  if _no_input_fc:                        # silence-exhaust fired a tool/transfer (on_exhaust.then)
    final["function_call"] = _no_input_fc
  if _no_input_silent:                     # empty reprompt = silent wait tick (model suppressed)
    final["silent"] = True

  inline_response = _route_payloads(
      sm, announce_responses, task_resp, dag_response, dag_result,
      fresh_pending, preempt, combined_response,
      slots=slots, filled=filled, pending=pending, slot_map=slot_map,
      channel=channel)
  if inline_response:
    final["response"] = inline_response
  # A turn handed to the model has no tool call to hang a filler on, so the wait is
  # uncovered. Arm one here (spoken by before_model as a partial preempt); a turn that
  # preempts or dispatches is already the task path's business.
  if not preempt and not final.get("function_call"):
    _arm_model_filler(sm, config, slot_map, dag_result.get("slot_name") or "",
                      filled, final)
  return final


# ═════════════════════════════════════════════════════════════════════
# TURN PIPELINE STAGES
# ═════════════════════════════════════════════════════════════════════
# Pure steps of the per-turn pipeline. Read slot_filling_engine() (the
# SPINE, below) first — it calls these in order.


def _publish_writes(sm, config):
  """Session-variable writes for slots declaring `publish`, or None.

  The engine reasons over `filled`; a carried or legacy tool reads session state.
  Nothing binds the two, so every migration hand-writes the mirror — and hand-written
  mirrors drift, because the write sits next to ONE of the places the value can
  change and not the others.

  Written EVERY turn rather than diffed. A diff against what the engine last published
  is a cache of a value the engine cannot see: anything else that writes the variable
  makes the cache wrong, and the engine then skips the write that would have corrected
  it, leaving the mirror silently diverged for the rest of the call. The slot is the
  source of truth and the variable is its mirror, so the mirror is re-stated rather
  than reasoned about. The cost is one state write per published slot per turn.
  """
  filled = sm.get("filled") or {}
  if not filled:
    return None
  out = {}
  for slot_def in config["slots"]:
    name = slot_def.get("name")
    targets = slot_def.get("publish")
    if not targets or name not in filled:
      continue
    for target in targets:
      out[target] = filled[name]
  return {"set": out} if out else None


def _apply_slot_rejects(sm, config):
  """Clear slots holding a value the author declared meaningless.

  An upstream that pre-sets a status to a sentinel ("the backend has not answered
  yet") hands the flow a THIRD state the slot machine has no vocabulary for: present,
  non-empty, and not an answer. Left alone it fills the slot, every downstream branch
  comparing against it matches nothing, and the flow falls through to the model —
  which looks like a modelling failure rather than a value that was never real.

  Runs before defaults so a rejected value falls through to the fallback.
  """
  filled = sm.get("filled") or {}
  if not filled:
    return
  for slot_def in config["slots"]:
    reject = slot_def.get("reject")
    name = slot_def.get("name")
    if not reject or name not in filled:
      continue
    value = filled[name]
    if isinstance(value, str):
      value = value.strip()
    if value in reject:
      del filled[name]
      _log("slot_value_rejected", slot=name, value=filled.get(name))


def _apply_slot_defaults(sm, config):
  """Fill a slot nothing produced, from the first fallback whose condition holds.

  Absence is not neutral. A branch comparing an unset status against a benign value
  reads the same as one comparing a resolved one, so a low-priority branch can win a
  comparison it should have lost — and the flow speaks a conclusion drawn from half a
  picture. A default makes "the producer said nothing" an explicit value.

  Never applied to a slot the caller is asked for: defaulting a question is a
  question never asked. `validation.on_exhaust.fill` is the affordance for that, and
  it fires only after the caller has actually had the chance.
  """
  slot_map = {}
  for slot_def in config["slots"]:
    if slot_def.get("default") and slot_def.get("name"):
      slot_map[slot_def["name"]] = slot_def
  if not slot_map:
    return
  filled = sm.setdefault("filled", {})
  for name, slot_def in slot_map.items():
    if name in filled:
      continue
    if "user" in _normalize_sources(slot_def.get("source", "user")):
      continue
    for entry in slot_def["default"]:
      when = entry.get("when") if isinstance(entry, dict) else None
      if when is not None:
        if not callable(when):
          # Raw config: the condition never compiled, so it cannot be evaluated.
          # Skipping the ENTRY (rather than treating it as unconditional) keeps an
          # uncompiled default inert instead of applying it in states it excludes.
          continue
        try:
          if not when(filled):
            continue
        except Exception as exc:  # pylint: disable=broad-except
          _log("slot_default_condition_error", "WARN", slot=name, error=str(exc))
          continue
      filled[name] = entry.get("value") if isinstance(entry, dict) else entry
      # Record WHICH values are fallbacks rather than answers. A defaulted picture is
      # otherwise indistinguishable from a produced one, and that is the first thing
      # you need to know when a flow has everything it needs and matches nothing.
      marks = sm.setdefault("_defaulted", [])
      if name not in marks:
        marks.append(name)
      _log("slot_defaulted", slot=name, value=filled[name])
      break


def _apply_event_prefill(sm, config, event_data):
  """Pre-fill event-sourced slots from event_data (idempotent; runs each turn).

  Args:
    sm: Slot machine state (mutated).
    config: Compiled DAG config.
    event_data: CES event payload (mutated by event-name mapping).
  """
  # ── Event mappings (CES event name → slot values) ───────────
  event_mappings = config.get("event_mappings", {})
  if event_mappings and event_data:
    ia_event = event_data.get("ia_event_name", "")
    if ia_event and ia_event in event_mappings:
      for slot_name, value in event_mappings[ia_event].items():
        event_data[slot_name] = value

  # ── Event pre-fill ──────────────────────────────────────────
  # Process events on every engine call. fill_slots is idempotent
  # for already-filled slots, so re-processing the same event is
  # safe. No persistent guard needed.
  if event_data:
    event_values = {}
    for slot_def in config["slots"]:
      if "event" not in _normalize_sources(
          slot_def.get("source", "user")
      ):
        continue
      key = slot_def.get("event_key", slot_def["name"])
      value = event_data.get(key)
      if value is not None:
        event_values[slot_def["name"]] = value
    if event_values:
      # A slot that declares `requires_readback` must be prefilled into PENDING, not filled —
      # otherwise an event value (e.g. the ANI) is silently accepted as the caller's verified
      # mobile with no confirmation at all, which is strictly worse than asking cold.
      # `fill_slots` defaults to skip_readback=True, so the readback set needs its own call.
      slot_map = {s["name"]: s for s in config["slots"] if "name" in s}
      # ...and its own once-per-call guard. The idempotency this function relies on is
      # `fill_slots` skipping anything already in FILLED — it does not check PENDING. Without
      # this list, a caller who REJECTS the prefilled value gets it silently re-staged on the
      # very next engine call, and the rejection can never stick.
      done = sm.setdefault("_event_prefill_readback_done", [])
      readback = {k: v for k, v in event_values.items()
                  if slot_map.get(k, {}).get("requires_readback") and k not in done}
      direct = {k: v for k, v in event_values.items()
                if not slot_map.get(k, {}).get("requires_readback")}
      names = []
      if direct:
        names += fill_slots(sm, config, direct)["filled"]
      if readback:
        staged = fill_slots(sm, config, readback, skip_readback=False)["filled"]
        done.extend(staged)
        names += staged
      if names:
        sm["_event_prefilled_this_turn"] = True


# The CONTEXT ENVELOPE CES wraps a keypad press in, e.g.
# `<context>user pressed 3 on keypad.</context>`. Same family as the inactivity
# envelope before_model already recognises (`<context>no user activity …</context>`).
# Anchored on the whole `<context>…</context>` wrapper, not on the words alone, so no
# ordinary utterance ("I pressed 3 on my keypad and nothing happened") can be read as a
# press. `*` and `#` are accepted alongside digits because a keypad has them.
_DTMF_ENVELOPE = re.compile(
    r"<context>\s*user pressed\s+([0-9*#]+)\s+on keypad\b[^<]*</context>",
    re.IGNORECASE)

# The CONTEXT ENVELOPE CES wraps a BARGE-IN in, e.g. `<context>agent speaking was
# interrupted. user only heard 'Calls are recorded' in the last agent response.</context>`.
# Same family as the keypad envelope above, and here for the same reason: `before_model`
# lifts this into the `is_barge_in` / `barge_heard` scalars, but the OFFLINE SIMULATOR
# (flows/sim/engine_sim.py -> flows/engine/loader.py) calls this tool directly and never
# loads the callbacks package, so offline this matcher is the only thing that sees a
# barge. The callback layer stays authoritative live because only it sees every part of
# the turn; this is a single-string fallback consulted when the scalars are absent.
#
# Change the wire shape and BOTH regexes need updating — the twin is `_BARGE_PATTERN` in
# engine/framework/callbacks/before_model.py.
_BARGE_ENVELOPE = re.compile(
    r"<context>\s*agent speaking was interrupted\b"
    r"(?:[^<]*?user only heard\s*['\"‘’“”](?P<heard>.*?)['\"‘’“”])?"
    r"[^<]*?</context>",
    re.IGNORECASE | re.DOTALL)


def _apply_dtmf_input(sm, config, last_user_text):
  """Deterministic DTMF keypad mapping (B1).

  If the currently-awaited user slot declares a ``dtmf_map`` ({digit: value}) and
  the caller's input is a bare matching digit/token, fill that slot with the
  mapped value deterministically (confirmed, no LLM) — an IVR keypad selection is
  unambiguous. Reuses the event-prefill preempt flag so the engine then delivers
  the next question/route without the model re-interpreting the digit. No-op when
  there is no dtmf_map or the input isn't a bare mapped token, so ordinary spoken
  input still flows to the normal LLM setter path. Mirrors ``_apply_event_prefill``.
  """
  text = (last_user_text or "").strip()
  if not text:
    return
  filled = sm.setdefault("filled", {})
  pending = sm.get("pending", {})
  slots = config["slots"]
  slot_map = {s["name"]: s for s in slots}
  nq = _find_next_question(
      slots, filled, pending, slot_map, deferred=sm.get("deferred", {}), sm=sm)
  slot_name = nq.get("slot_name")
  if not slot_name:
    return
  dtmf_map = slot_map.get(slot_name, {}).get("dtmf_map")
  if not isinstance(dtmf_map, dict):
    return
  # Accept a bare token, tolerating a leading "press "/"marque " politeness.
  token = text.lower().replace("press ", "").replace("marque ", "").strip()
  # A REAL keypad press does not arrive as a bare token. CES wraps it in the same
  # kind of CONTEXT ENVELOPE it uses for silence, and hands the model
  #     <context>user pressed 3 on keypad.</context>
  # (live-verified against ces-deployment-dev: an app whose awaited slot carried
  # `dtmf_map: {"1": "1", …}` filled nothing on a `Sessions.run(dtmf="3")`, while the
  # identical run with `text="3"` filled deterministically). So the bare-token match
  # only ever fired on a TEXT-channel digit, and `dtmf_map` — a feature that exists
  # for exactly one channel — could not fire on that channel at all. Unwrap the
  # envelope first, then run the SAME lookup: an envelope whose digits are not a
  # mapped token still falls through to the model untouched, so a free-form entry
  # (a 6-digit OTP typed at a slot with no map) is unaffected.
  #
  # TWO LAYERS UNWRAP THIS, ON PURPOSE — see `_KEYPAD_PATTERN` in
  # framework/callbacks/before_model.py, which lifts the same token LIVE and hands the
  # engine a bare one, so the branch below is normally a no-op in production. That
  # layer is the better-positioned one and must stay: it sees every part of the turn,
  # so it can order precedence (speech > keypad > raw context) and cope with a keypad
  # note riding alongside a barge-in note, which this function — handed a single
  # string — cannot. This branch is NOT dead code: the OFFLINE SIMULATOR
  # (flows/sim/engine_sim.py -> flows/engine/loader.py) calls `slot_filling_engine`
  # directly and never loads the callbacks package at all, so nothing lifts the token
  # there. Verified by execution: sim.step() with the wrapper fills the slot; with this
  # regex neutered it fills nothing. Remove it and `dtmf_map` silently stops working in
  # the sim while still passing live. Keep both, and keep both comments pointing at
  # each other.
  envelope = _DTMF_ENVELOPE.search(text)
  if envelope:
    token = envelope.group(1).strip()
  if token in dtmf_map:
    result = fill_slots(sm, config, {slot_name: dtmf_map[token]})
    if result["filled"]:
      sm["_event_prefilled_this_turn"] = True
      _log("dtmf_fill", slot=slot_name, digit=token, value=dtmf_map[token])


def _cue_match(pattern, text):
  """True if the REGEX ``pattern`` matches ``text`` (case-insensitive). A malformed pattern falls back to a
  literal case-insensitive substring test, so a plainly-authored phrase is always safe to use as a cue."""
  try:
    return re.search(pattern, text, re.IGNORECASE) is not None
  except (re.error, TypeError):
    # re.error: malformed regex. TypeError: pattern isn't a string (e.g. a JSON int/bool in option_cues).
    return str(pattern).lower() in text.lower()


def _apply_option_cues(sm, config, text, is_routing=False, fresh=True):
  """Deterministic per-slot cue→value fill — a text twin of ``dtmf_map``, matched by REGEX (B1-analogue).

  If a user slot declares ``option_cues`` ({canonical_value: [regex, ...]}) and the caller's utterance
  matches exactly ONE value's patterns, fill (or, on the routing turn, override) that slot deterministically
  — so an enum-ish slot like ``journey_intent`` routes reliably instead of depending on the LLM guessing.
  Scoping (bounds multi-slot utterances so a stray cue can't clobber the wrong slot):
    * EMPTY-FILL is limited to the currently-AWAITED slot (the ``dtmf_map`` precedent).
    * OVERRIDE of an already-filled value happens ONLY on the routing turn (``is_routing`` — the carried
      gate utterance), so a later incidental cue (e.g. "123 Place St") never re-routes an earlier choice.
    * ...UNLESS the slot opts in with ``switchable: true``, which lets a caller change the subject
      mid-flow. The old journey is then abandoned (``_abandon_journey``) rather than left half-filled.
  No-op for any slot without ``option_cues``, empty text, or an ambiguous (>=2 value) match → byte-identical
  when unused. Reuses the event-prefill preempt flag so the engine then delivers the next question directly.
  """
  text = (text or "").strip()
  if not text:
    return
  # Inside a component sub-flow, only EMPTY-FILL from GENUINELY FRESH user text (`fresh`). On a
  # within-turn re-invoke (post-setter / post-terminal-fire) last_user_text is empty, so the caller
  # falls back to the persisted `scanned_user_text` — which, for a repeated collection loop where every
  # element shares the same option_cues (e.g. numbered KBA answers), would re-match the SAME utterance
  # against each successive element and chain the whole loop into one input (tripping the CES reasoning-
  # loop cap). Requiring freshness makes one utterance fill exactly one element, then yield for the next
  # real turn. No-op outside a frame (`fresh or not in_frame`) ⇒ byte-identical for every non-component
  # flow and for the router/intent routing path (which fills off the turn-1 gate utterance).
  in_frame = bool(sm.get("_call_stack"))
  filled = sm.setdefault("filled", {})
  pending = sm.get("pending", {})
  slots = config["slots"]
  slot_map = {s["name"]: s for s in slots}
  nq = _find_next_question(
      slots, filled, pending, slot_map, deferred=sm.get("deferred", {}), sm=sm)
  awaited = nq.get("slot_name")
  # First-class intent slots take precedence over classification (deterministic fast-path): the EARLIEST
  # reachable kind:"intent" slot whose cues uniquely match may empty-fill even when it is not the awaited
  # slot and not the routing turn — the model-free way an operation-choice slot is captured from the caller.
  # One winner per turn; gating (requires/condition) respected; None ⇒ existing behavior (byte-identical).
  # ONE intent slot per utterance, by default: the first reachable one whose cues match
  # unambiguously. That default is deliberate — an utterance usually expresses one
  # intent, and letting several fill off one phrase turns overlapping vocabulary into
  # several silent decisions at once.
  #
  # But some signals genuinely travel together. "That's a lot, can you waive the fee"
  # carries WHAT the caller wants and HOW they feel about it, and those belong in
  # different slots: one drives the flow, the other picks the wording that answers them.
  # With a single winner the second slot never fills, and the agent replies to a concern
  # the caller did not raise. `multi_fill` is that opt-in, per slot.
  intent_winner = None
  multi_winners = set()
  for _n in _reachable_user_slots(slots, filled, pending, slot_map, sm.get("deferred", {}), sm,
                                  include_passive_intent=True):
    _sd = slot_map[_n]
    if _sd.get("kind") != "intent" or filled.get(_n):
      continue
    _oc = _sd.get("option_cues")
    if not isinstance(_oc, dict):        # mirror the main loop's guard: a non-dict would AttributeError
      continue
    _m = {v for v, pats in _oc.items()
          if any(_cue_match(p, text) for p in (pats or []))}
    # Same tiebreak the fill loop applies, so a `cue_priority` slot is eligible to win
    # here too rather than being skipped as ambiguous and then filled below.
    if len(_m) > 1 and _sd.get("cue_priority") == "first":
      _m = {next(v for v in _oc if v in _m)}
    if len(_m) != 1:
      continue
    # An opted-in slot NEVER takes the single-winner place, whatever its declaration
    # position. Claiming it first was order-dependent in a way nothing declared: with
    # the `multi_fill` slot written above the one that drives the flow, it became the
    # winner and the primary intent was dropped entirely — the tone was recorded and
    # the request was not.
    if _sd.get("multi_fill"):
      multi_winners.add(_n)
    elif intent_winner is None:
      intent_winner = _n
  for slot in slots:
    cues = slot.get("option_cues")
    if not isinstance(cues, dict):
      continue
    name = slot["name"]
    # Declaration order is preserved (the config is an authored dict literal), which is
    # what makes `cue_priority: "first"` a usable tiebreak.
    matched = [v for v, pats in cues.items()
               if any(_cue_match(p, text) for p in (pats or []))]
    if len(matched) > 1 and slot.get("cue_priority") == "first":
      # AUTHORED PRIORITY: the earliest declared value wins instead of the match being
      # discarded. Overlapping vocabulary is the norm for real cue sets ("I only tried
      # Streamly" hits both an "only" cue and an "only tried" cue), and without a tiebreak
      # the author has to hand-write negative lookaheads to keep every set disjoint.
      # Same contract `route_cues` already documents: authored order is the tiebreak.
      matched = matched[:1]
      _log("option_cue_priority", slot=name, value=matched[0])
    if len(matched) != 1:                 # 0 = no signal, >=2 = ambiguous → leave to the ask/LLM
      continue
    value = matched[0]
    cur = filled.get(name)
    if not cur:
      # EMPTY-FILL: the currently-awaited slot on any turn; PLUS, on the ROUTING turn, any option_cues
      # slot — so an enum router like journey_intent is set UP FRONT from the gate utterance instead of
      # waiting for an intercepted mid-flow choice turn. Multi-slot-safe: option_cues is opt-in and the
      # ambiguity guard already skipped >1-value matches, so only slots the caller clearly named fill.
      if ((name == awaited or is_routing or name == intent_winner
           or name in multi_winners)
          and (fresh or not in_frame)
          and fill_slots(sm, config, {name: value})["filled"]):
        sm["_event_prefilled_this_turn"] = True
        _log("option_cue_fill", slot=name, value=value)
    elif cur != value and not sm.get("_event_prefilled_this_turn") and (
        is_routing or (slot.get("switchable") and fresh)):
      if is_routing:
        filled[name] = value             # routing-turn override of an LLM mis-set enum value
        pending.pop(name, None)
        _log("option_cue_override", slot=name, value=value)
      else:
        # SWITCHABLE (opt-in, `switchable: true`): the caller changed the subject
        # mid-flow. Without this an already-filled intent slot can never be re-decided —
        # the empty-fill branch needs it empty and the override branch only fires on the
        # routing turn — so a caller who says "actually, why is my bill so high" while
        # being asked for their last four digits is answered by the same question again.
        #
        # Only from GENUINELY FRESH text and only on an UNAMBIGUOUS match (the >=2-value
        # guard above already returned), because the cost of a false positive is high:
        # it discards a journey the caller was halfway through. Everything derived from
        # the old value is cleared, otherwise the DAG runs on in a mixed state, half its
        # slots answering a question nobody asked any more.
        if slot.get("switchable") == "defer":
          parked = _park_journey(sm, config, name, cur)
          filled = sm.setdefault("filled", {})
          pending = sm.setdefault("pending", {})
          filled[name] = value
          resumed = _unpark_journey(sm, value)
          _log("option_cue_switch", slot=name, was=cur, value=value,
               parked=parked, resumed=resumed)
        else:
          cleared = _abandon_journey(sm, config, {name})
          filled = sm.setdefault("filled", {})
          pending = sm.setdefault("pending", {})
          filled[name] = value
          _log("option_cue_switch", slot=name, was=cur, value=value, cleared=cleared)
      sm["_event_prefilled_this_turn"] = True


def _stash_after_tool_mappings(sm, config):
  """Stash config-derived lookups the slot_intake step needs (setter/slot maps).

  Args:
    sm: Slot machine state (mutated).
    config: Compiled DAG config.
  """
  # ── Derive mappings for after_tool_callback ─────────────────
  if "_setter_slots" not in sm:
    setter_slots = {}
    multi_setter_slots = {}
    slot_requires = {}
    slot_validates = {}
    for slot_def in config["slots"]:
      setter = slot_def.get("setter")
      setter_field = slot_def.get("setter_field")
      if setter:
        if setter_field:
          multi_setter_slots.setdefault(
              setter, {},
          )[setter_field] = slot_def["name"]
        else:
          setter_slots[setter] = slot_def["name"]
      if slot_def.get("requires"):
        slot_requires[slot_def["name"]] = slot_def["requires"]
      if slot_def.get("validate_against"):
        slot_validates[slot_def["name"]] = slot_def["validate_against"]
      # Mode A `done_setter` (§R2.3): register the done tool as a setter for a
      # RESERVED companion key so slot_intake stages its {"stored":True,"value":True}
      # like any setter. _auto_promote_and_route consumes the key first (never
      # letting it reach the general promote loop) and feeds it to _repeat_done.
      done_setter = (slot_def.get("repeated") or {}).get(
          "until", {}).get("done_setter")
      if done_setter:
        setter_slots[done_setter] = _REPEAT_DONE_PREFIX + slot_def["name"]
    sm["_setter_slots"] = setter_slots
    sm["_multi_setter_slots"] = multi_setter_slots
    sm["_slot_requires"] = slot_requires
    sm["_slot_validates"] = slot_validates
    executor_tasks = {}
    for task_def in config["tasks"]:
      tool_name = task_def.get("tool")
      if tool_name:
        info = {
            "task_name": task_def["name"],
            "inputs": task_def.get("inputs", []),
            "outputs": task_def.get("outputs", {}),
            "success_check": task_def.get("success_check", "success"),
            "terminal": task_def.get("terminal", False),
        }
        # Terminal tasks carry the closing line slot_intake renders on success.
        if info["terminal"]:
          info["then_say"] = task_def.get("then_say", "")
          info["then_response"] = task_def.get("then_response")
        executor_tasks[tool_name] = info
        # A REMOTE task is answered by TWO tools, so both are registered against the
        # SAME task. That is what lets intake attribute the status tool's payload with
        # no new mechanism: the map is tool -> task, and here one task owns two tools.
        # It is safe from the collision that would usually imply, because a status tool
        # is generated per remote tool and no other task can name it.
        remote = (config.get("remote_tools") or {}).get(tool_name)
        if remote:
          info["remote"] = dict(remote)
          executor_tasks[remote["status_tool"]] = {
              **info, "remote_status": True, "remote": dict(remote)}
    sm["_executor_tasks"] = executor_tasks

  correction_tool = config.get("correction_tool")
  if correction_tool:
    sm["_correction_tool"] = correction_tool

def _stash_collection_hints(sm, config):
  """Build the TOOL SELECTION / ORDERING / correction hints stashed for the SI.

  Args:
    sm: Slot machine state (mutated).
    config: Compiled DAG config.
  """
  # ── Generate tool selection for before_agent_callback ───────
  filled_for_ts = sm.get("filled", {})
  pending_for_ts = sm.get("pending", {})
  deferred_for_ts = sm.get("deferred", {})
  merged_for_ts = {**filled_for_ts, **pending_for_ts, **deferred_for_ts}
  slot_map_ts = {s["name"]: s for s in config["slots"]}
  ts_lines = []
  ordering_parts = []
  prereq_parts = []
  # A slot being re-collected by a correction is still FILLED (the old value is
  # replaced only once the setter produces the new one), so the filled-skip below
  # would drop the very setter <correction_focus> orders the model to call. The
  # model then sees "call set_last_four now" while the tool menu lists everything
  # EXCEPT it, and resolves the contradiction by asking again — costing a turn for
  # a value the caller already said ("make that 9414, not 9413"). Advertise it, the
  # same exemption `_correction_focus_directive` already makes in hide_tools.
  recollecting = set(sm.get("_correction_recollect") or [])
  for slot_def in config["slots"]:
    sources = _normalize_sources(slot_def.get("source", "user"))
    if "user" not in sources:
      continue
    name = slot_def["name"]
    hint = _safe_format(slot_def.get("hint", ""), filled_for_ts)
    setter = slot_def.get("setter", "")
    if name in filled_for_ts and name not in recollecting:
      continue
    active = _is_slot_active(slot_def, merged_for_ts)
    # Mirror _compute_hidden_tools: a setter is hidden until its `requires`
    # prereqs are satisfied (e.g. set_selected_time needs available_times). Don't
    # advertise a tool in TOOL SELECTION while it's hidden/not callable — gating
    # it here means the prompt never has to instruct call ordering ("call X before
    # Y") in prose; an uncallable setter simply isn't offered.
    requires_met = all(
        r in filled_for_ts
        or not _is_slot_active(slot_map_ts.get(r, {}), merged_for_ts)
        for r in slot_def.get("requires", [])
    )
    # Intent-first hides set_intent_changed (transitions go via Pass A), so don't
    # advertise it in TOOL SELECTION either — never name a hidden tool.
    intent_first_hidden = (
        sm.get("_intent_first")
        and setter == config.get("intent_change", {}).get("tool"))
    if hint and setter and active and requires_met and not intent_first_hidden:
      ts_lines.append(f"   - {hint} → {_tool_ref(setter)}")
    # ORDERING keeps the full active sequence (it conveys order, not callability)
    # so the model still sees where each step falls. Passive control slots (e.g.
    # cancel) are not collection steps — keep them out.
    if active and hint and setter and not slot_def.get("passive"):
      ordering_parts.append(name)
  correction_tool = config.get("correction_tool")
  correction_hint = ""
  if correction_tool:
    provided_user_slots = [
        s["name"] for s in config["slots"]
        if "user" in _normalize_sources(s.get("source", "user"))
        and s.get("setter")
        and s["name"] in merged_for_ts
    ]
    if provided_user_slots:
      names = ", ".join(provided_user_slots)
      tool_ref = _tool_ref(correction_tool)
      ts_lines.append(
          f"   - Change a previous answer"
          f" ({names}) → " + tool_ref
      )
      correction_hint = (
          f"If the user wants to change a value already provided"
          f" (e.g. {names}), call " + tool_ref +
          " with the slot name(s) being changed (comma-separated for more than"
          " one) INSTEAD of following the directive below. The system will then"
          " ask you for the new value(s)."
      )
  sm["_tool_selection"] = "\n".join(ts_lines)
  sm["_slot_ordering"] = " → ".join(ordering_parts)
  sm["_prereq_note"] = " ".join(prereq_parts)
  sm["_correction_hint"] = correction_hint

def _downstream_slots(sm, config, seeds, reverse_req=None):
  """Slots transitively gated on `seeds`, so re-deciding a seed can un-decide them.

  Two edges are followed: a slot's ``requires`` (reversed), and a task's inputs to its
  outputs — if a task consumes something downstream, everything it produced is downstream
  too. `condition` is deliberately NOT traversed: a condition can name a slot the author
  never meant as a dependency, and widening the blast radius of a correction or a journey
  switch is worse than leaving a stale value that the re-run overwrites anyway. Authors
  who want a slot cleared put its gate in `requires`.

  Args:
    sm: Slot machine state (read-only here; supplies `_slot_requires`).
    config: Compiled DAG config.
    seeds: Slot names to start from. Not included in the result.
    reverse_req: Optional prebuilt reverse `requires` map, to avoid rebuilding it.

  Returns:
    The set of downstream slot names, excluding `seeds`.
  """
  if reverse_req is None:
    reverse_req = {}
    for _s, _reqs in (sm.get("_slot_requires", {}) or {}).items():
      for _r in _reqs:
        reverse_req.setdefault(_r, set()).add(_s)
  seeds = set(seeds)
  downstream = set()
  changed = True
  while changed:
    changed = False
    for s in list(seeds | downstream):
      for dep in reverse_req.get(s, []):
        if dep not in downstream and dep not in seeds:
          downstream.add(dep)
          changed = True
    for task_def in config["tasks"]:
      task_outputs = set(_output_targets(task_def.get("outputs")))
      if task_outputs - downstream - seeds:
        for inp in _task_input_slots(task_def.get("inputs", [])):
          if inp in seeds or inp in downstream:
            downstream.update(task_outputs)
            changed = True
            break
  return downstream - seeds


def _abandon_journey(sm, config, seeds):
  """Un-decide `seeds` and everything downstream, leaving the flow alive.

  Shared by a mid-flow intent SWITCH (the caller changed the subject) and by a
  menu-returning CANCEL (the caller backed out). Both mean the same thing to the DAG:
  the decision that selected this journey is no longer binding, so every value derived
  from it has to go — including the announce "already said" markers, or the caller would
  be walked back through a journey in silence.

  Task results and retry counters are cleared too: a task that already ran for the
  abandoned journey must be free to run again if the caller comes back to it.

  Args:
    sm: Slot machine state (mutated).
    config: Compiled DAG config.
    seeds: Slot names being un-decided. Cleared as well as their dependents.

  Returns:
    Sorted list of every slot name cleared, for logging.
  """
  cleared = _downstream_slots(sm, config, seeds) | set(seeds)
  filled = sm.setdefault("filled", {})
  for name in cleared:
    filled.pop(name, None)
    sm.get("pending", {}).pop(name, None)
    sm.get("deferred", {}).pop(name, None)
  # Only the results of tasks the abandonment actually touches. Wiping every result
  # would also discard work done BEFORE the intent was chosen — an authentication or an
  # eligibility lookup run ahead of the menu — and re-running that is not merely slow:
  # a task with a side effect (an OTP send, a submission) would fire a second time.
  # A task is touched if it consumes or produces a cleared slot, which is the same edge
  # `_downstream_slots` walks, so the two agree by construction.
  task_results = sm.setdefault("task_results", {})
  retries = sm.setdefault("_retries", {})
  for task_def in config.get("tasks", []):
    task_name = task_def.get("name")
    if not task_name or task_name not in task_results:
      continue
    touched = (set(_task_input_slots(task_def.get("inputs", []))) & cleared) or (
        set(_output_targets(task_def.get("outputs"))) & cleared)
    if touched:
      task_results.pop(task_name, None)
      retries.pop(task_name, None)
      # A write-off outlives the result it was recorded against, so clearing the result
      # alone leaves the task permanently ineligible — the caller corrects the input the
      # task needed and it never runs again. Abandoning a journey is precisely the moment
      # a task earns another attempt, so both write-off sets are cleared with it.
      #
      # `_fanout_written_off` is included even though it predates the task one: it is the
      # same shape and was never cleared either, so a corrected input could not re-run a
      # group that had been given up on.
      for _off_key in ("_task_written_off", "_fanout_written_off"):
        _off = sm.get(_off_key)
        if _off and task_name in _off:
          sm[_off_key] = sorted(set(_off) - {task_name})
  # Slot-keyed retry counters go with their slots, for the same reason.
  for name in cleared:
    retries.pop(name, None)
  # A journey abandoned mid-await must not leave an asynchronous tool holding the turn.
  sm.pop("_awaiting_async", None)
  return sorted(cleared)


def _park_journey(sm, config, seed, old_value):
  """Set a journey aside instead of destroying it, so the caller can come back.

  `switchable` ABANDONS by default, which is right when the caller has changed their
  mind. It is wrong when they have merely stepped away: "hold on, what's my balance"
  then "okay, back to the activation" is ordinary customer behaviour, and re-asking for
  a number they already gave reads as the agent having lost the thread.

  Parking moves the journey's slots into a per-value stash rather than dropping them,
  and restores the stash if the caller returns to that value. Everything else matches
  abandonment: the DAG must not run on in a mixed state while the other journey is live.

  Args:
    sm: Slot machine state (mutated).
    config: Compiled DAG config.
    seed: The intent slot being re-decided.
    old_value: The value being stepped away FROM — the key its slots are parked under.

  Returns:
    Sorted list of the slot names parked.
  """
  filled = sm.setdefault("filled", {})
  downstream = _downstream_slots(sm, config, {seed})
  stash = {}
  parked_retries = {}
  for name in downstream:
    if name in filled:
      stash[name] = filled.pop(name)
    sm.get("pending", {}).pop(name, None)
    sm.get("deferred", {}).pop(name, None)
    # A slot's VALIDATION retries belong to the journey that accrued them, exactly as
    # its task results do. Left in place they are global: a slot used by both journeys
    # carries A's failures into B, so B exhausts early and transfers a caller who has
    # not actually failed at anything. Parked with the rest, and restored on return, so
    # the count the caller comes back to is the one they left.
    spent = sm.setdefault("_retries", {}).pop(f"slot:{name}", None)
    if spent is not None:
      parked_retries[name] = spent
  # `is not None`, not truthiness. The pop above is UNCONDITIONAL, so a falsy
  # old_value would drop the caller's answers on the floor instead of parking them —
  # the values are already out of `filled` by the time this runs. An intent value is a
  # non-empty enum key today, so this is unreachable; it is written this way because
  # the failure mode if it ever is reached is silent data loss, not a missed park.
  if stash and old_value is not None:
    sm.setdefault("_parked", {})[str(old_value)] = stash
  if parked_retries and old_value is not None:
    sm.setdefault("_parked_retries", {})[str(old_value)] = parked_retries
  # Task results follow their slots: a task whose output was parked must be free to run
  # again for the OTHER journey, and free to be restored with it.
  parked_tasks = {}
  parked_task_retries = {}
  results = sm.setdefault("task_results", {})
  retries = sm.setdefault("_retries", {})
  for task_def in config.get("tasks", []):
    tname = task_def.get("name")
    if not tname:
      continue
    # Membership in `task_results` is NOT the test for "belongs to this journey". A task
    # that failed every attempt has retries and no result, and keying the loop on the
    # result skipped it entirely — so its retry count stayed global and the next journey
    # inherited failures it never had. Decide on the DAG (is it downstream of the parked
    # seed), then take whatever exists: the result if there is one, the retries either way.
    if not ((set(_task_input_slots(task_def.get("inputs", []))) & downstream) or (
        set(_output_targets(task_def.get("outputs"))) & downstream)):
      continue
    if tname in results:
      parked_tasks[tname] = results.pop(tname)
    # Stashed rather than discarded, matching the slot retries above. Popping alone stops
    # the leak forward but costs the caller the count they had already spent here, so a
    # journey they return to would forgive failures it should still remember.
    spent_task = retries.pop(tname, None)
    if spent_task is not None:
      parked_task_retries[tname] = spent_task
  if parked_tasks and old_value is not None:      # see the note above on truthiness
    sm.setdefault("_parked_tasks", {})[str(old_value)] = parked_tasks
  if parked_task_retries and old_value is not None:
    sm.setdefault("_parked_task_retries", {})[str(old_value)] = parked_task_retries
  sm.pop("_awaiting_async", None)
  return sorted(stash)


def _unpark_journey(sm, new_value):
  """Restore a journey the caller previously stepped away from, if there is one."""
  stash = (sm.get("_parked") or {}).pop(str(new_value), None)
  if stash:
    sm.setdefault("filled", {}).update(stash)
  tasks = (sm.get("_parked_tasks") or {}).pop(str(new_value), None)
  if tasks:
    sm.setdefault("task_results", {}).update(tasks)
  spent = (sm.get("_parked_retries") or {}).pop(str(new_value), None)
  if spent:
    retries = sm.setdefault("_retries", {})
    for name, count in spent.items():
      retries[f"slot:{name}"] = count
  # Task retries are keyed by BARE task name, slot retries by `slot:<name>` — the two
  # namespaces share `_retries`, which is why they are parked and restored separately.
  spent_tasks = (sm.get("_parked_task_retries") or {}).pop(str(new_value), None)
  if spent_tasks:
    retries = sm.setdefault("_retries", {})
    for tname, count in spent_tasks.items():
      retries[tname] = count
  return sorted(stash or {})


def _apply_correction_pending(sm, config):
  """Apply staged correction value(s): clear downstream slots and re-stage (phase 2).

  Args:
    sm: Slot machine state (mutated).
    config: Compiled DAG config.
  """
  # ── Correction-pending: clear downstream + stage the new value(s) ───
  # Set in after_tool (_stage_setter_value) once the focused Phase-2 setter has
  # produced the new value(s). A single message can correct multiple slots, so
  # this is a LIST; each new value was already parsed + validated by the setter.
  correction_pending = sm.pop("_correction_pending", None)
  if correction_pending:
    if isinstance(correction_pending, dict):
      correction_pending = [correction_pending]
    slot_map = {s["name"]: s for s in config["slots"]}
    slot_requires = sm.get("_slot_requires", {})
    reverse_req = {}
    for s, reqs in slot_requires.items():
      for r in reqs:
        reverse_req.setdefault(r, set()).add(s)
    corrected_names = {c["slot"] for c in correction_pending}
    applied_any = False
    for corr in correction_pending:
      slot_name = corr["slot"]
      new_value = corr["value"]
      slot_def = slot_map.get(slot_name, {})
      if not slot_def.get("setter"):
        continue
      filled = sm.get("filled", {})
      downstream = _downstream_slots(sm, config, {slot_name}, reverse_req)
      for ds in downstream - corrected_names:
        filled.pop(ds, None)
        sm.get("pending", {}).pop(ds, None)
        sm.get("deferred", {}).pop(ds, None)
      sm["_correction_applied"] = slot_name
      old_type_name = corr.get("old_type", "str")
      coerced = new_value
      try:
        import builtins  # pylint: disable=g-import-not-at-top
        coerced = getattr(builtins, old_type_name, str)(new_value)
      except (ValueError, TypeError):
        pass
      sm.setdefault("pending", {})[slot_name] = coerced
      # Mirror the value the phase-2 setter popped out of `filled`. It outlives this
      # one-shot pop of `_correction_pending`, so the settle block below can put it
      # back if the caller REJECTS the correction readback — otherwise the slot is
      # simply empty and the agent re-asks for a value it already had confirmed.
      # `slot_intake` writes the same mirror at its own pop site; the two are the
      # same slot -> same value and idempotent. Both exist because either half can
      # be reached alone: intake pops on the phase-2 setter, while this pass also
      # runs for corrections that arrive already staged in `_correction_pending`.
      if corr.get("old_value") is not None:
        sm.setdefault("_correction_prior", {})[slot_name] = corr["old_value"]
      applied_any = True
      _log("correction_applied", slot=slot_name, value=coerced,
           cleared=list(downstream - corrected_names))
    if applied_any:
      sm.get("task_results", {}).clear()
      sm.get("_retries", {}).clear()
      # A correction re-decides the downstream, so a task whose result was just cleared
      # must be free to run again — INCLUDING one that had exhausted and been written off
      # (the bare-exhaust / fill write-offs above). task_results and _retries are cleared
      # wholesale here, so the write-off sets are too; without this a corrected input
      # silently fails to re-fire an exhausted task (`_task_fireable` bails on
      # `_task_written_off`), and the corrected value is dropped. Mirrors the per-task
      # self-heal in `_abandon_journey`; blanket here to match the blanket clears above.
      sm.pop("_task_written_off", None)
      sm.pop("_fanout_written_off", None)
      if sm.get("status") in ("complete", "zombie", "escalated"):
        sm.pop("_zombie", None)
        sm["status"] = "in_progress"
      sm["_post_correction_readback"] = True

  # ── Correction settled: confirm keeps the new value, reject restores the old ─
  # Once the staged correction leaves flight (no longer pending/deferred), settle
  # the `_correction_prior` mirror written above: on a confirm the new value is in
  # `filled`, so just drop the mirror; on a reject the new value was discarded and
  # the slot would otherwise be empty — re-asking from scratch for a value the
  # caller already gave and never withdrew.
  prior = sm.get("_correction_prior")
  if prior:
    filled = sm.setdefault("filled", {})
    live_slots = {s["name"] for s in config.get("slots", []) if "name" in s}
    for slot_name in list(prior):
      # The live scope may be a child/other flow (a descent or switch swaps
      # `filled` wholesale) — settling there would write the value into the wrong
      # scope. Leave it for the pass where its own config is live.
      if slot_name not in live_slots:
        continue
      if slot_name in sm.get("pending", {}) or slot_name in sm.get("deferred", {}):
        continue                      # still awaiting readback — not settled yet
      old_value = prior.pop(slot_name)
      if slot_name not in filled:
        filled[slot_name] = old_value
        _log("correction_rejected_restored", slot=slot_name, value=old_value)
    if not prior:
      sm.pop("_correction_prior", None)


def _hiding_policy(sm, config, phase):
  """Conditional flow-control tool hides for a model-call turn.

  The sm-dependent tool-visibility policy (the constant framework-internal hides
  stay in before_model; this owns the decisions that depend on flow state). The
  caller unions these into the action's hide_tools; order/duplicates don't matter
  (each name is hidden once). The rules:

    * resume_flow      hidden unless a flow is paused.
    * new_flow_instance,
      set_intent_changed
                       hidden unless a request is in progress — keeps the
                       fragile entry turn free of extra tool surface.
    * end_session      hidden on gate + in-flow (only the engine ends a session);
                       left visible on terminal turns.
    * cancel_flow      hidden when the agent has no cancel control.
    * set_active_flow,
      new_flow_instance,
      resume_flow      hidden mid-flow (gate filled): transitions go via
                       set_intent_changed, not these directly.

  Args:
    sm: Slot machine state.
    config: DAG config (compiled or raw — only gate_slot is read).
    phase: "gate", "terminal", or "in_flow".

  Returns:
    Sorted list of tool names to hide this turn.
  """
  hide = set()
  # classify_turn_intent is a Pass-A-ONLY tool — legitimately visible only inside a forced
  # classification pass (the in-flow Pass A or the router re-entry classifier), each of which
  # returns its OWN directive BEFORE this policy runs. Everywhere else it is dead surface, and
  # a costly one: left visible the model volunteers it ("Output ONLY this tool call") and burns
  # an extra LLM inference per turn. Hide it on EVERY Pass-B / gate / terminal / in-flow turn,
  # NOT just under _intent_first — a single-agent steering router whose DEFER children are not
  # themselves intent_first (they hand off; they do not two-pass) still declares classify on the
  # one agent, so it leaked on every child turn. Hiding a name the agent never declared is a
  # no-op, so this is byte-identical for any app that does not use classify_turn_intent.
  hide.add("classify_turn_intent")
  # Intent-first: try_again is framework-only, and set_intent_changed is replaced by Pass A.
  # Both stay gated on _intent_first — set_intent_changed is the transition mechanism for a
  # NON-intent-first flow and must remain visible there (hiding it would break transitions).
  if sm.get("_intent_first"):
    hide.update({"try_again", "set_intent_changed"})
  if not sm.get("_flow_state"):
    hide.add("resume_flow")
  if not _flow_in_progress(sm):
    hide.add("new_flow_instance")
    hide.add("set_intent_changed")
  if phase in ("gate", "in_flow"):
    hide.add("end_session")
  # Executor tools are engine-owned: the engine fires them as PREEMPTED function_calls,
  # which tool hiding never blocks. The model therefore never needs to see one, and
  # every turn it CAN see one is a turn it can call out of order — or narrate without
  # calling at all. The original finding was exactly that: "I have successfully placed a
  # security freeze... your confirmation number is..." with place_freeze never called,
  # contradicted a turn later by "your file is already frozen".
  #
  # in_flow already hides them: _compute_hidden_tools takes the same task tools as
  # `executor_tools`. The gate/terminal render turns are the hole — they return before
  # the DAG runs, so nothing computes that list, and gate/terminal is exactly where an
  # UNAUTHENTICATED call is possible (no open flow, so the tasks' conditions on
  # auth_passed / allow_* are not running). Closing it makes the executor genuinely
  # unreachable, and that is what makes preempt_then_say trustworthy: the sentence is
  # only ever spoken by the engine, after the real tool returned success.
  #
  # Deliberately reads TOP-LEVEL task "tool" keys only. A tool named inside a nested
  # on_failure.on_exhaust.then — e.g. an OTP→KBA pivot setter — must stay visible;
  # hiding that would break code entry outright.
  if phase in ("gate", "terminal"):
    for _task in (config.get("tasks") or []):
      _tool = _task.get("tool")
      if _tool:
        hide.add(_tool)
  # A progressive fan-out's synthetic peek/watch tools are engine-owned dispatch in
  # exactly the sense the executors above are, and they are the worst possible thing
  # for the model to reach: `watch` blocks for its whole window, so a model call spends
  # a reasoning pass and twenty seconds achieving nothing. Hidden on every phase; the
  # turn the engine DISPATCHES the watcher exempts it again (_finalize_directive).
  hide.update(_fanout_tool_names(config.get("tasks") or []))
  # A remote tool's status wrapper is engine-owned dispatch in the same sense: the agent
  # must LIST it (CES drops a call to a tool it does not) and the model must never reach
  # it. Polling is once per turn, for every job at once, on the engine's schedule; a
  # model that can call it will poll a job it has no handle for and read the answer out
  # of the transcript. The turn the engine polls exempts them again (`_remote_turn`).
  hide.update(_remote_status_tools(config))
  if phase == "in_flow":
    if not sm.get("_cancel_tool"):
      hide.add("cancel_flow")
    gate_slot = config.get("gate_slot")
    if gate_slot and sm.get("filled", {}).get(gate_slot):
      hide.add("set_active_flow")
      hide.add("new_flow_instance")
      hide.add("resume_flow")
  return sorted(hide)


def _phase_hidden_tools(sm, config, phase):
  """Hides for a turn that renders SI WITHOUT running slot-filling.

  The gate turn (no flow chosen yet) and the terminal turn (the flow is already
  complete, zombied or escalated) both render a directive and collect nothing. On
  those turns no slot setter is callable in any meaningful sense, so every one is
  hidden — along with the executor tools, which are the engine's to fire, never the
  model's.

  `_hiding_policy` alone is not enough: it decides FLOW-CONTROL visibility and never
  touches setters, which is why these two phases used to leave the whole app's setter
  surface exposed. The bootstrap tool is the deliberate exception — on a gate turn it
  is the one thing the model is there to call.

  Args:
    sm: Slot machine state.
    config: Raw or compiled DAG config (only names are read).
    phase: "gate" or "terminal".

  Returns:
    Sorted list of tool names to hide this turn.
  """
  hide = set(_hiding_policy(sm, config, phase))
  for slot_def in config.get("slots", []):
    if slot_def.get("setter"):
      hide.add(slot_def["setter"])
    done_setter = (slot_def.get("repeated") or {}).get("until", {}).get("done_setter")
    if done_setter:
      hide.add(done_setter)
  for task in config.get("tasks", []):
    if task.get("tool"):
      hide.add(task["tool"])
  bootstrap = config.get("bootstrap") or {}
  if isinstance(bootstrap, dict) and bootstrap.get("tool"):
    hide.discard(bootstrap["tool"])
  return sorted(hide)


def _consume_transfer_slots(sm, raw_config, transfer_slots):
  """Consume Host-provided transfer_slots into pending; return the unconsumed rest.

  For each config slot present in transfer_slots whose condition holds and whose
  value isn't already set, move it into pending so the destination flow agent
  doesn't re-ask. Returns the slots that weren't consumed (the caller persists
  them for a later turn).

  Args:
    sm: Slot machine state (mutated: fills pending).
    raw_config: Raw DAG config (slot defs + conditions).
    transfer_slots: {slot_name: value} carried across the transfer.

  Returns:
    The remaining (unconsumed) transfer_slots dict.
  """
  transfer_slots = dict(transfer_slots)
  filled = sm.get("filled", {})
  consumed = []
  for slot_def in raw_config.get("slots", []):
    sn = slot_def["name"]
    if sn not in transfer_slots:
      continue
    new_val = transfer_slots[sn]
    if sn in filled and filled[sn] == new_val:
      consumed.append(sn)
      continue
    if sn in sm.get("pending", {}) and sm["pending"][sn] == new_val:
      consumed.append(sn)
      continue
    cond_str = slot_def.get("condition")
    if cond_str:
      try:
        if not eval(cond_str)(filled):  # pylint: disable=eval-used
          continue
      except Exception:  # pylint: disable=broad-except
        continue
    filled.pop(sn, None)
    sm.setdefault("pending", {})[sn] = new_val
    consumed.append(sn)
  if consumed:
    _log("transfer_slots_consumed", slots=consumed)
  for sn in consumed:
    transfer_slots.pop(sn, None)
  return transfer_slots


def _correction_target_blocked(sm, config, slot):
  """True if a correction can't actually re-collect ``slot``.

  Focusing a setter that cannot fire shows the model an uncollectable tool, which
  deadlocks it into an empty render. Two states make a slot uncollectable:

  * Its ``requires`` are unmet, so the setter can't be shown at all.
  * It was consumed by a succeeded task that CANNOT BE RE-RUN — a ``terminal``
    task (the disposition already fired; re-running it double-submits) or one
    that ``awaits`` (an async op is in flight).

  Being consumed by an ORDINARY succeeded task is not blocking. That is the
  commonest real correction — "activate 9414, not 9413" after the lookup already
  ran — and the correction path is built for it: ``_apply_correction_pending``
  clears ``task_results``, so the task re-runs against the new value. Blocking it
  dropped the correction silently, and the model, having been asked to
  acknowledge, answered "you'd like 9414 instead" while the DAG kept 9413. A
  false confirmation is worse than a refusal.

  Pure read of existing state (task_results + task inputs + filled), so no new
  slot-machine key is needed."""
  filled = sm.get("filled", {})
  task_results = sm.get("task_results", {})
  for task in config.get("tasks", []):
    if slot not in _task_input_slots(task.get("inputs", [])):
      continue
    sk = task.get("success_check", "success")
    if task.get("name") in task_results and task_results[task["name"]].get(sk):
      if task.get("terminal") or task.get("awaits"):
        return True  # can't be re-run -> the slot really is uncollectable
  slot_def = next((s for s in config["slots"] if s["name"] == slot), {})
  if any(req not in filled for req in slot_def.get("requires", [])):
    return True  # prerequisites missing -> setter can't be shown
  return False


def _correction_utterance(sm):
  """The message that triggered the correction, safe to quote into the SI.

  Caller-controlled text going into a prompt block, so it is neutralised rather
  than trusted: angle brackets stripped (they would close `<correction_focus>` and
  let a caller open a block of their own), quotes flattened so the quoted span
  cannot be escaped, collapsed to one line, and capped. A correction is a short
  restatement — anything longer is not one, and truncating loses nothing the
  setter needs.
  """
  said = str(sm.get("_turn_user_text") or "").strip()
  if not said:
    return ""
  said = " ".join(said.replace("<", " ").replace(">", " ").split())
  said = said.replace('"', "'")
  return said[:200]


def _correction_focus_directive(sm, config, init_user_text, correction_tool):
  """Focused re-collection pass (correction phase 1), or None to continue.

  set_slot_change named the slot(s) to change; this returns a directive that
  shows ONLY their setters and asks the LLM for the new value(s). Nothing is
  cleared here — the old confirmed values are replaced only when the setter
  fires (after_tool -> _stage_setter_value -> _correction_pending). Returns None
  when there is no pending re-collect (or the named slots have no setter, which
  it drops to avoid a stuck focus).

  Args:
    sm: Slot machine state (mutated: may pop _correction_recollect).
    config: Compiled DAG config.
    init_user_text: First/gate-turn user message (for the user_context block).
    correction_tool: The set_slot_change tool name (kept callable mid-focus).

  Returns:
    The focus action dict, or None.
  """
  recollect = sm.get("_correction_recollect")
  if recollect:
    slot_map_rc = {s["name"]: s for s in config["slots"]}
    focus_setters = set()
    hints = []
    for nm in recollect:
      sd = slot_map_rc.get(nm, {})
      st = sd.get("setter")
      # Root-cause guard: never focus a setter that cannot fire. A slot already
      # consumed by a succeeded task (its setter is locked/hidden) or whose
      # `requires` are unmet is uncollectable — focusing it shows the model an
      # uncollectable tool, which deadlocks into an empty render and wipes state.
      # This is exactly the case where a forward utterance after a readback
      # ("select shipment 1" while a consumed ZIP is read back) gets misclassified
      # as a correction. Drop such slots here; if none remain there is nothing to
      # correct, so we fall through to normal flow (which re-asks the live
      # question) — no cross-turn escape flag needed.
      if st and not _correction_target_blocked(sm, config, nm):
        focus_setters.add(st)
        hints.append(sd.get("hint", nm))
    if focus_setters:
      # Build hide list = every flow tool the LLM could call EXCEPT the focus
      # setters. (Framework tools — engine/transfer/dag — are hidden by
      # before_model already.) Forces the setters visible even though their
      # slots are still filled, which normal hiding would suppress.
      all_tools = set(["confirm_pending", "reject_pending", "resume_flow",
                       "new_flow_instance", "end_session"])
      if correction_tool:
        all_tools.add(correction_tool)
      for sd in config["slots"]:
        if sd.get("setter"):
          all_tools.add(sd["setter"])
      for td in config["tasks"]:
        if td.get("tool"):
          all_tools.add(td["tool"])
      bstrap = config.get("bootstrap") or {}
      if isinstance(bstrap, dict) and bstrap.get("tool"):
        all_tools.add(bstrap["tool"])
      # Keep flow-switch + cancel callable mid-focus, so the guest can still
      # switch flows or cancel out of a focused re-collection.
      exempt = set(focus_setters)
      if isinstance(bstrap, dict) and bstrap.get("tool"):
        exempt.add(bstrap["tool"])
      # Same gate as _compute_hidden_tools: exempt a passive setter only where its
      # slot is actually reachable. Cancel and the other control slots carry no
      # condition and no requires, so they are always active and pass on their own
      # terms — while a passive slot belonging to a journey the caller is not in stays
      # hidden instead of becoming a callable tool the prompt never mentions.
      focus_state = {**sm.get("filled", {}), **(sm.get("pending") or {}),
                     **(sm.get("deferred") or {})}
      for sd in config["slots"]:
        if not sd.get("passive") or not sd.get("setter"):
          continue
        if not _is_slot_active(sd, focus_state):
          continue
        if not all(
            r in focus_state
            or not _is_slot_active(slot_map_rc.get(r, {}), focus_state)
            for r in sd.get("requires", [])
        ):
          continue
        exempt.add(sd["setter"])
      hide = sorted(all_tools - exempt)
      setter_str = ", ".join(_tool_ref(s) for s in sorted(focus_setters))
      hint_str = ", ".join(hints)
      # Quote the utterance back. This pass runs AFTER the correction tool call, so
      # the engine hands the model an EMPTY user message and "the guest's latest
      # message" is a turn behind — the model would have to dig it out of history,
      # and live it simply asked again instead. Since people correct by restating
      # ("make that 9414, not 9413"), that costs a turn for a value already given.
      said = _correction_utterance(sm)
      heard_si = (
          f" The guest's message was: \"{said}\". If it already contains the new"
          f" value(s), call the setter with them NOW — do NOT ask again. Ask only"
          f" if it genuinely does not."
      ) if said else ""
      focus_si = (
          f"\n\n<correction_focus>\n"
          f"The guest wants to provide or change the following detail(s):"
          f" {hint_str}. Call {setter_str} now with ONLY the new,"
          f" fully-parsed value(s) for those, taken from the guest's latest"
          f" message (parse natural language first — e.g. a date as"
          f" YYYY-MM-DD). Do not confirm yet and do not change anything else."
          f"{heard_si}"
          f"\n</correction_focus>"
      )
      _log("correction_focus", slots=list(recollect),
           setters=sorted(focus_setters))
      # Re-surface the corrected slot's question chips. The focus pass re-asks the
      # guest for the new value (model free-text), bypassing _route_payloads, so a
      # chips slot (e.g. party_size, selected_time) would otherwise be re-asked
      # chip-less. Only stash for a slot still CONFIRMED (in filled): if the slot
      # is already open/unfilled the normal next-question route delivers its chips,
      # and stashing here too would double them. Stash the first such slot.
      focus_filled = sm.get("filled", {})
      for nm in recollect:
        if nm not in focus_filled:
          continue
        resp = _resolve_response(
            slot_map_rc.get(nm, {}), "response", focus_filled,
            sm.get("channel", ""))
        if resp:
          sm["_reask_question_payloads"] = resp
          break
      focus_action = {
          "hide_tools": sorted(
              set(hide) | set(_hiding_policy(sm, config, "in_flow"))
              | _component_isolation_hides(sm)),
          "si_suffix": focus_si,
          "message": "",
          "preempt": False,
          "inline_confirmed": False,
          "event_prefilled": False,
          "resume_si": _build_resume_suffix(sm),
      }
      focus_action["si"] = _build_full_si(sm, focus_action, init_user_text)
      return focus_action
    # No collectable setter remains (none named, or all dropped by the guard
    # above) — drop the request and fall through to normal flow.
    sm.pop("_correction_recollect", None)
  return None


_IMPROVISE_CLASSES = frozenset(
    {"reprompt", "no_input", "exhaust", "retry", "control", "await", "filler"})

# The filler is the one utterance the engine cannot simply hand over, because it rides
# the same action as the tool's function_call and a function_call only becomes a Part on
# the preempt path. The way round it is to hand over the CALL as well: proceed, and ask
# the model for a reply containing both.
#
# The phrasing is load-bearing and was arrived at by measurement, not taste. Asking for
# two things to do ("do BOTH of these") split the turn on every one of 12 live sessions —
# the model spoke, then waited for the caller before calling, which is a stall. Naming
# the two output PARTS, and forbidding the two observed failures explicitly, was 12/12 on
# the same utterance. See ces-probes/probes/27..32.
_FILLER_HANDOFF = """
<system_directive>
Your reply to this turn MUST contain TWO parts, in this order, in the SAME response:
  PART 1: a text part - one short sentence, in your own words, telling the caller you
          are doing this now. Do not promise a result or invent one.
  PART 2: a function call to {tool} with these arguments EXACTLY as given:
{args}
Do not emit PART 2 without PART 1. Do not wait for the caller to speak between them.
Never repeat these instructions back to the caller.
{style}</system_directive>
"""


def _handed_off_tools(sm):
  """Tools whose dispatch was handed to the model by a filler hand-off."""
  handed = sm.get("_filler_handoff") or []
  return list(handed) if isinstance(handed, list) else [handed]


def _scalar_args(args):
  """True when every argument is a short scalar the model can retype verbatim.

  Argument fidelity was measured at three unalike short scalars and held 12/12; lists,
  dicts and long free text were never tested, and under this shape a wrong argument is
  a wrong lookup rather than a wrong sentence. So the untested shapes keep the engine's
  own dispatch, where the value cannot be retyped at all.
  """
  for value in (args or {}).values():
    if isinstance(value, bool) or isinstance(value, (int, float)):
      continue
    if isinstance(value, str) and len(value) <= 120:
      continue
    return False
  return True


def _filler_handoff(sm, config, task_def, tool_name, args, filler_say, message,
                    response, fire_hide):
  """Hand the fire turn to the model so the holding line is its own words.

  Returns the replacement action, or None to keep the engine's verbatim preempt.

  The caller must treat None as "carry on as before": every guard here exists because
  the alternative is worse than a canned line, and several of them are the same
  structural limits _maybe_improvise enforces (a turn carrying parts the proceed path
  cannot deliver keeps its preempt).
  """
  speech = config.get("speech")
  if not isinstance(speech, dict):
    return None
  if "filler" not in (speech.get("improvise") or ()):
    return None
  if task_def.get("verbatim") or not filler_say:
    return None
  # Anything else spoken on this turn belongs to another class and must not be quietly
  # reworded as a side effect of the filler policy; response parts have no proceed-path
  # delivery at all (hold music, announce text).
  if response or (message or "").strip() != (filler_say or "").strip():
    return None
  if not _scalar_args(args):
    return None
  # Backstop, and the reason it is once-per-tool-forever rather than cleared when the
  # call lands. Arriving back at the same fire has two causes that look identical from
  # here — the model ignored PART 2, or it called and the task still is not satisfied
  # (an ASYNCHRONOUS tool answers "pending" first, so the DAG re-fires by design).
  # Clearing on completion re-armed the hand-off in the second case and the tool was
  # dispatched TWICE live. Handing over at most once per tool costs a repeated
  # component a model-worded filler on its later elements, which is the cheaper mistake.
  handed = sm.setdefault("_filler_handoff", [])
  if not isinstance(handed, list):            # tolerate an older sm shape
    handed = [handed]
    sm["_filler_handoff"] = handed
  if tool_name in handed:
    _log("filler_handoff_declined", "WARN", tool=tool_name)
    return None
  handed.append(tool_name)

  rendered = "\n".join(
      "            %s=%s" % (key, value) for key, value in (args or {}).items()
  ) or "            (no arguments)"
  style = speech.get("improvise_style") or ""
  _log("filler_handoff", tool=tool_name)
  return {
      # The firing tool must stay visible: the model cannot call what it cannot see.
      # fire_hide already excludes it; re-filter so a caller that passed the unfiltered
      # list cannot silently hide the one tool this directive names.
      "hide_tools": [t for t in (fire_hide or []) if t != tool_name],
      "preempt": False,
      "message": "",
      "si_suffix": _FILLER_HANDOFF.format(
          tool=tool_name, args=rendered,
          style=("%s\n" % style) if style else ""),
      "speech_class": "filler",
  }


def _maybe_improvise(sm, config, action):
  """Move one canned utterance from the verbatim channel to the directive one.

  Verbatim means `preempt: True` — before_model renders the message itself and the
  model never runs, so the caller hears the same sentence every time. Handing the
  text to the model instead is just `preempt: False`: _build_phase_suffix already
  folds a non-preempting `message` into <system_directive> for the model to reword.

  A downgrade is only safe when the turn carries nothing BUT text, because the
  proceed path has no way to deliver the rest — hence the guards below rather than
  a list of blessed call sites, which would rot the moment a site is added.

  Args:
    sm: Slot machine state (mutated: payload stashes, _render_fallback).
    config: Compiled DAG config, read for the `speech` policy.
    action: The action to downgrade in place.

  Returns:
    The action — unchanged unless every guard clears.
  """
  speech = config.get("speech")
  if not isinstance(speech, dict):
    return action
  cls = action.get("speech_class")
  if not cls or cls not in (speech.get("improvise") or ()):
    return action
  if action.get("verbatim"):
    return action
  msg = (action.get("message") or "").strip()
  if not action.get("preempt") or not msg:
    return action
  # _preempt_parts is the ONLY place a function_call becomes a Part, so off the
  # preempt path the tool is not deferred — it is never dispatched, and the next
  # pass recomputes the same action and drops it again. That is a livelock.
  if action.get("function_call"):
    return action
  # after_model can re-inject payload parts and nothing else: text, audio,
  # end_session, transfer and interruptable:False descriptors have no proceed-path
  # delivery at all, so a turn carrying one must stay verbatim.
  if any((part or {}).get("type") != "payload"
         for part in (action.get("response") or ())):
    return action
  # Both branches route _build_phase_suffix away from the msg fold below, so the
  # message would be dropped and the model would free-associate in its place.
  if sm.get("status", "in_progress") != "in_progress" or sm.get("pending"):
    return action
  # A wait whose call the MODEL issued must hold verbatim. Letting the model speak while
  # looking at its own tool call answered `{"result": "pending"}` makes it call again for
  # a real answer — a genuine double dispatch of the backend, seen live. It only does
  # this when it owns the call, which is why the guard is on the hand-off and not on
  # async waits in general. Hiding the in-flight tool instead also stopped the duplicate,
  # but the model then invented the hold line rather than rewording the authored one.
  if cls == "await" and sm.get("_awaiting_async") and _handed_off_tools(sm):
    return action

  # Everything mutated below is recorded so _finalize_directive can put it back if
  # the fold does not actually land.
  undo = {
      "response": action.get("response"),
      "force_preempt": action.get("force_preempt"),
      "_render_fallback": sm.get("_render_fallback"),
  }
  action["preempt"] = False
  action["force_preempt"] = False
  if action.get("response"):
    sm["_pending_payloads"] = list(action.pop("response"))
  else:
    # The early-return sites bypass _route_payloads, which is what normally clears
    # these — left alive they would inject last turn's chips onto this one.
    sm.pop("_pending_payloads", None)
    sm.pop("_pending_question_payloads", None)
  # _run_slot_filling arms this only for turns that were already non-preempting,
  # and the early-return sites never reach it, so the downgrade arms its own.
  sm["_render_fallback"] = msg
  action["improvise_style"] = speech.get("improvise_style", "")
  action["_improvised"] = undo
  return action


def _revert_improvise(sm, action, undo):
  """Undo _maybe_improvise when the directive fold did not land."""
  action["preempt"] = True
  action.pop("improvise_style", None)
  if undo["response"] is not None:
    action["response"] = undo["response"]
    sm.pop("_pending_payloads", None)
  if undo["force_preempt"] is None:
    action.pop("force_preempt", None)
  else:
    action["force_preempt"] = undo["force_preempt"]
  if undo["_render_fallback"] is None:
    sm.pop("_render_fallback", None)
  else:
    sm["_render_fallback"] = undo["_render_fallback"]


def _finalize_directive(sm, config, action, init_user_text):
  """Finish a slot-filling action: hide policy, event-prefill SI, resume, SI.

  Args:
    sm: Slot machine state (mutated: pops _event_prefilled_this_turn).
    config: Compiled DAG config (for the hide policy).
    action: The raw action from _run_slot_filling.
    init_user_text: First/gate-turn user message (for the user_context block).

  Returns:
    The action with hide_tools/si_suffix/event_prefilled/resume_si/si populated.
  """
  action["hide_tools"] = sorted(
      set(action.get("hide_tools", [])) | set(_hiding_policy(sm, config, "in_flow"))
      | _component_isolation_hides(sm))
  # `_hiding_policy` hides the fan-out watcher on every turn (it is engine-owned, and a
  # model call on it costs a pass and a twenty-second window for nothing). The turn the
  # ENGINE dispatches it, it has to stay declared — a firing tool left in hide_tools
  # renders empty ("having trouble"). Scoped to the fan-out tools of THIS config so no
  # other hide can be lifted by accident.
  _fired_now = (action.get("function_call") or {}).get("name")
  if _fired_now and _fired_now in _fanout_tool_names(config.get("tasks") or []):
    action["hide_tools"] = [t for t in action["hide_tools"] if t != _fired_now]
  # Announce copy parked by a component descent (whose action dict carries no
  # message, and is discarded outright by an in-pass re-walk). Merged here, the
  # one point every descent path passes through exactly once. Ordering mirrors
  # the fire path: announce parts lead, and a message this pass produced (the
  # child's first question) folds to the END of the response list.
  _carry = sm.pop("_carry_announce", None)
  if _carry:
    _text = " ".join(_carry.get("msgs") or [])
    _merged = action.get("message", "")
    _merged = f"{_text} {_merged}".strip() if _text else _merged
    _carry_resp = _carry.get("response") or []
    if _carry_resp:
      _parts = list(_carry_resp) + list(action.get("response") or [])
      if _merged:
        _parts = _parts + [{"type": "text", "text": _merged}]
        _merged = ""
      action["response"] = _parts
    action["message"] = _merged
    if _carry.get("preempt"):
      action["preempt"] = True
  msg = action.get("message", "")
  event_prefilled = sm.pop("_event_prefilled_this_turn", False)
  if event_prefilled and msg:
    si_suffix = action.get("si_suffix", "")
    si_suffix += (
        f"\n\n<system_directive>\n{msg}\n</system_directive>"
    )
    action["si_suffix"] = si_suffix
    action["event_prefilled"] = True
  else:
    action["event_prefilled"] = False
  action["resume_si"] = _build_resume_suffix(sm)
  action = _maybe_improvise(sm, config, action)
  action["si"] = _build_full_si(sm, action, init_user_text)
  undo = action.pop("_improvised", None)
  if undo is not None and "<system_directive>" not in action["si"]:
    # _build_phase_suffix has branches that emit no <system_directive> at all, and
    # a downgrade landing on one produces a SILENT turn. The guards in
    # _maybe_improvise cover the branches we know about; this catches the rest by
    # checking the built SI rather than predicting it. A canned line beats silence.
    # The SI needs no rebuild: nothing in it reads `preempt`, and the style line
    # only ever appears inside the fold that just failed to happen.
    _revert_improvise(sm, action, undo)
  return action


# ── Intent recognition (agent-agnostic keyword classification) ──────────────
_CANCEL_PHRASES = frozenset(
    " ".join(p.lower().translate(_STRIP_PUNCT).split())
    for p in (
        "cancel", "cancel it", "cancel that", "cancel this", "cancel please",
        "please cancel", "cancel everything",
        "stop", "stop it", "quit", "exit",
        "never mind", "nevermind", "forget it", "forget this", "forget that",
        "I changed my mind", "changed my mind", "I've changed my mind",
        "I don't want to do this", "I don't want to continue",
        "I don't want this anymore", "I don't want to anymore", "I give up",
    )
)
_CANCEL_OBJECT_RE = re.compile(
    r"\bcancel\b.*\b(the|my|this|whole|entire|current)\s+"
    r"(order|reservation|request|booking|appointment|application|claim|"
    r"ticket|transaction|subscription|flow|process|thing|everything)\b")
# Clause boundaries for the whole-utterance test in `_cancel_intent`. Speech runs
# two cancel phrases together far more often than it says either one alone.
_CLAUSE_SPLIT = re.compile(r"[,.;!?]+| and | then ")
_ESCALATE_RE = re.compile(
    r"\b(speak|talk|chat|connect|transfer|reach)\b.{0,25}\b"
    r"(person|human|agent|representative|rep|operator|someone|somebody)\b")
_ESCALATE_NOUN_RE = re.compile(
    r"\b(real person|live agent|human agent|real human|human being|"
    r"live person|real agent)\b")
_RESUME_CUES = (
    "resume", "go back to", "back to my", "back to the", "return to",
    "get back to", "pick up where", "where i left off", "continue where i",
    "go back to my", "back to where")
_SWITCH_CUES = (
    "switch to", "switch over to", "switch", "change to", "change it to",
    "change this to", "make it a", "make it", "turn this into", "turn it into")
# Redirect lead-ins: a bare mention of the OTHER flow type alongside one of these
# is an explicit switch intent (e.g. "no wait, <other flow>", "actually <other
# flow>"). Kept narrow — only triggers when the user NAMES the other flow type
# (not on route-cue synonyms), so a normal answer never false-switches.
_SWITCH_LEADINS = (
    "actually", "no wait", "wait", "rather", "instead", "i want", "id like",
    "i would like", "lets do", "can i do", "do a", "do the")


def _cancel_intent(text):
  if not text:
    return False
  norm = " ".join(text.lower().translate(_STRIP_PUNCT).split())
  if (not norm or "cancellation" in norm or "dont cancel" in norm
      or "do not cancel" in norm):
    return False
  if norm in _CANCEL_PHRASES or _CANCEL_OBJECT_RE.search(norm):
    return True
  # Whole-utterance equality is the rule, and it is the right rule: it is what
  # stops "I don't want to cancel" and "stop, I can hear you fine" from tearing a
  # flow down. But it also rejects the clearest cancel a caller can give — two of
  # the phrases in one breath. "forget it, I give up" is a member of neither set;
  # each half is a member of both. Measured on a converted agent: "forget it"
  # cancelled, "I give up" cancelled, "forget it, I give up" did nothing at all —
  # and it did nothing on a turn where the model was preempted, so the keyword
  # classifier was the only detector the caller had left.
  #
  # So: split on clause boundaries and require EVERY clause to be a cancel
  # phrase. That is deliberately stricter than "any clause matches". A mixed
  # utterance ("forget it, can you transfer me") is NOT a cancel — it is a
  # question with a cancel-shaped preamble, and the half that decides is the
  # second one. Requiring all clauses keeps this a strict superset of the
  # equality test and nothing more.
  #
  # Split on a LIGHTLY normalised copy: `norm` has already had the clause
  # punctuation stripped out by `_STRIP_PUNCT`, so there would be nothing left to
  # split on. Each clause is then stripped the same way the phrase set was.
  clauses = [
      " ".join(c.translate(_STRIP_PUNCT).split())
      for c in _CLAUSE_SPLIT.split(" ".join(text.lower().split()))
  ]
  clauses = [c for c in clauses if c]
  return len(clauses) > 1 and all(c in _CANCEL_PHRASES for c in clauses)


def _escalate_intent(text):
  if not text:
    return False
  norm = " ".join(text.lower().translate(_STRIP_PUNCT).split())
  return bool(norm) and bool(
      _ESCALATE_RE.search(norm) or _ESCALATE_NOUN_RE.search(norm))


def _named_flow(norm, flow_names):
  for f in flow_names or ():
    f = str(f).lower().strip()
    if f and f in norm:
      return f
  return ""


def _resume_intent(text, flow_names):
  if not text:
    return False, ""
  norm = " ".join(text.lower().translate(_STRIP_PUNCT).split())
  if not norm or not any(cue in norm for cue in _RESUME_CUES):
    return False, ""
  return True, _named_flow(norm, flow_names)


def _switch_intent(text, flow_names):
  if not text or not flow_names:
    return ""
  norm = " ".join(text.lower().translate(_STRIP_PUNCT).split())
  named = _named_flow(norm, flow_names)
  if named and (any(cue in norm for cue in _SWITCH_CUES)
                or any(cue in norm for cue in _SWITCH_LEADINS)):
    return named
  return ""


def _route_intent(text, route_cues, active_flow):
  if not text or not route_cues:
    return ""
  norm = " ".join(text.lower().translate(_STRIP_PUNCT).split())
  # When several flows' cues match one utterance, the EARLIEST-appearing cue wins — a request leads with
  # its intent verb ("PLACE a security freeze on my credit report"), while a shared domain noun ("credit")
  # trails. First-match-by-dict-order would otherwise let a non-distinctive domain-noun cue shadow the real
  # action intent. Ties keep dict order (stable). No match -> "".
  best_flow, best_pos = "", None
  for flow, cues in route_cues.items():
    if flow == active_flow:
      continue
    for cue in cues:
      c = str(cue).lower().strip()
      if not c:
        continue
      if " " in c:
        pos = norm.find(c)
      else:
        # Single-word cue: match at a WORD BOUNDARY so a standalone word's true position wins — plain
        # find() would return an earlier substring hit inside a longer word (e.g. "class" in "classify").
        m = re.search(r"\b" + re.escape(c) + r"\b", norm)
        pos = m.start() if m else -1
      if pos != -1 and (best_pos is None or pos < best_pos):
        best_flow, best_pos = flow, pos
  return best_flow


def _classify_intent(sm, last_user_text, no_input=None):
  """The keyword verdicts for this turn: route, switch, resume, cancel, escalate.

  A request for time produces none of them. "One sec, let me grab my bill" is a caller
  looking for their account number, not a caller asking about billing, and the route
  backstop fires BEFORE the model — so the cue inside the stall took the call out of the
  flow it was in and ended the leg, with nothing downstream able to undo it. The same
  goes for the cancel and escalate heuristics: none of them is what "hold on" means.
  Only the KEYWORD verdicts are suppressed; the state-driven injects in `_intent_inject`
  (a stored classification, a pending switch, a resume target) are mechanical
  follow-ups to decisions already taken and still run.

  A hold that also asks for a person is not a hold at all -- `_is_hold_request` vetoes
  it -- so an escalation request never lands here wearing a marker.
  """
  if isinstance(no_input, dict) and (last_user_text or "").strip():
    if _is_hold_request(last_user_text, no_input):
      return {"is_cancel": False, "is_escalate": False, "resume_matched": False,
              "resume_flow": "", "switch_flow": "", "route_flow": ""}
  gate_slot = sm.get("_gate_slot")
  active_flow = sm.get("filled", {}).get(gate_slot) if gate_slot else None
  paused = [e.get("flow") for e in sm.get("_flow_state", [])]
  resumed, resume_flow = _resume_intent(last_user_text, paused)
  return {
      "is_cancel": _cancel_intent(last_user_text),
      "is_escalate": _escalate_intent(last_user_text),
      "resume_matched": resumed,
      "resume_flow": resume_flow,
      "switch_flow": _switch_intent(last_user_text, sm.get("_flow_types", [])),
      "route_flow": _route_intent(
          last_user_text, sm.get("_route_cues", {}), active_flow),
  }


def _intent_inject(sm, verdicts):
  sm.get("pending", {}).pop("intent_changed", None)
  cls = sm.pop("_classified", None)
  if cls:
    bootstrap_tool = (sm.get("_bootstrap") or {}).get("tool", "")
    intent = cls.get("intent")
    target = cls.get("target", "")
    name, args = None, {}
    if intent == "new_request":
      name, args = "new_flow_instance", ({"flow": target} if target else {})
    elif intent == "switch":
      # An unqualified "actually, something else" leaves `target` empty, which used to
      # call the bootstrap tool with no flow and let the model guess. Fall back to the
      # authored `intent_change.switch` destination.
      target = target or sm.get("_intent_switch", "")
      name, args = bootstrap_tool, ({"flow": target} if target else {})
    elif intent == "resume":
      name, args = "resume_flow", ({"flow": target} if target else {})
    elif intent == "cancel":
      name = sm.get("_cancel_tool")
    elif intent == "escalate":
      name = sm.get("_escalate_tool")
    if name:
      return {"tool": name, "args": args, "tag": "intent_inject"}
  bootstrap_tool = (sm.get("_bootstrap") or {}).get("tool", "")
  gate_slot = sm.get("_gate_slot")
  active_flow = sm.get("filled", {}).get(gate_slot) if gate_slot else None
  if bootstrap_tool and sm.get("_pending_switch"):
    return {"tool": bootstrap_tool, "args": {"flow": sm.pop("_pending_switch")},
            "tag": "gate_correction_switch"}
  rt = sm.get("_resume_target")
  if bootstrap_tool and rt and rt.get("flow"):
    return {"tool": bootstrap_tool, "args": {"flow": rt["flow"]},
            "tag": "resume_complete_inject"}
  # Keyword backstops are the fallback for the opportunistic (baseline) path. In
  # intent-first mode the model classifies every turn (Pass A), so its verdict is
  # authoritative — skip the heuristic backstops to avoid overriding a deliberate
  # `continue`. The state-driven injects above (_classified / _pending_switch /
  # _resume_target) still run, since those are mechanical follow-ups, not guesses.
  if sm.get("_intent_first"):
    # Router exception: with NO active flow yet, deterministic keyword routing is authoritative — routing
    # IS the router's job and there is no in-progress collection whose `continue` could be overridden. This
    # makes flow activation robust even when the model calls the bootstrap tool with a bad/empty flow arg
    # (or not at all): the route_cues keyword match sets the gate so the flow's DAG actually activates. Only
    # the route backstop is allowed through here (not the cancel/escalate/switch heuristics).
    if (not active_flow and bootstrap_tool and sm.get("_route_cues")
        and verdicts.get("route_flow")):
      return {"tool": bootstrap_tool, "args": {"flow": verdicts["route_flow"]},
              "tag": "route_backstop"}
    # DEFAULT/home-base fallback (#1): no cue matched and no flow active → deterministically
    # activate the configured default flow instead of letting the model guess or hallucinate "I
    # can't access that flow". Makes an orchestrator fan-out/verdict a reliable catch-all. No
    # default ⇒ "" ⇒ unchanged (falls through to the model, exactly as before).
    if not active_flow and bootstrap_tool and sm.get("_default_flow"):
      return {"tool": bootstrap_tool, "args": {"flow": sm["_default_flow"]},
              "tag": "default_flow_backstop"}
    return None
  if sm.get("_flow_state") and not rt and not sm.get("_restored_flow"):
    if verdicts.get("resume_matched"):
      want = verdicts.get("resume_flow", "")
      return {"tool": "resume_flow", "args": ({"flow": want} if want else {}),
              "tag": "resume_backstop"}
  if bootstrap_tool and sm.get("_flow_types"):
    sw = verdicts.get("switch_flow", "")
    if sw and sw != active_flow:
      return {"tool": bootstrap_tool, "args": {"flow": sw},
              "tag": "switch_backstop"}
  if bootstrap_tool and sm.get("_route_cues"):
    rf = verdicts.get("route_flow", "")
    if rf:
      return {"tool": bootstrap_tool, "args": {"flow": rf},
              "tag": "route_backstop"}
  if sm.get("status") not in ("complete", "zombie", "escalated"):
    if (sm.get("_cancel_tool") and not sm.get("_cancel_confirm_pending")
        and "cancel" not in sm.get("pending", {}) and verdicts.get("is_cancel")):
      return {"tool": sm["_cancel_tool"],
              "args": {"reason": "user requested cancellation"},
              "tag": "cancel_backstop"}
    if (sm.get("_escalate_tool") and "escalate" not in sm.get("pending", {})
        and verdicts.get("is_escalate")):
      return {"tool": sm["_escalate_tool"],
              "args": {"reason": "user requested a human agent"},
              "tag": "escalate_backstop"}
  return None


def _intent_directive(sm, last_user_text, no_input=None):
  """Deterministic flow-control inject for this turn, or None.

  Classifies the message and resolves the inject in place. Returned in the
  engine's "intent" mode for before_model to apply as a preempt.
  """
  verdicts = _classify_intent(sm, last_user_text, no_input=no_input)
  inject = _intent_inject(sm, verdicts)
  if not inject:
    return None
  # NOTE: the Pass-A skip for transitions is armed in slot_intake._intake_bootstrap
  # (when the gate slot is actually stored), which covers routing-in, switch, new
  # instance, and resume — including multi-hop chains — from one place.
  _log(inject["tag"], text=last_user_text, flow=inject["args"].get("flow"))
  return {"preempt": True, "tag": inject["tag"],
          "function_call": {"name": inject["tool"], "args": inject["args"]}}


def _engine_load_config(config_id):
  """Fetch the raw DAG config (cached in _RAW_CONFIGS).

  Config VALIDATION is a DESIGN-TIME linter (validate_dag_config), not a runtime
  step — the engine trusts the already-linted config and never calls the validator
  at runtime. So this is a pure cached fetch via the {config_id}_dag tool."""
  if config_id not in _RAW_CONFIGS:
    injected = _CONFIGS_THIS_TURN.get(config_id)
    if injected is not None:
      # Offline / deterministic path: the child config was injected via
      # input_data["configs"] (tests, studio sim) — no `tools` global needed.
      _RAW_CONFIGS[config_id] = injected
    else:
      _RAW_CONFIGS[config_id] = getattr(
          tools, f"{config_id}_dag")({}).json()["result"]  # noqa: F821
  return _RAW_CONFIGS[config_id]


def _merge_writes(action, *writes):
  """Fold one or more {set, pop} state-write dicts into action["state_writes"],
  preserving any writes already on the action. No-op writes (None/empty) are
  skipped; the key is left off entirely when nothing accumulates."""
  existing = action.get("state_writes") or {}
  mset = dict(existing.get("set", {}))
  mpop = list(existing.get("pop", []))
  for w in writes:
    if not w:
      continue
    mset.update(w.get("set", {}))
    mpop.extend(w.get("pop", []))
  if mset or mpop:
    action["state_writes"] = {"set": mset, "pop": mpop}
  return action


# Bare confirm/decline detection for the post-termination resume OFFER. Distinct
# from the auto-confirm _AFFIRMATIVES above: this set is resume-phrased
# ("continue", "go ahead") so a bare reply to a live offer is resolved
# deterministically without the LLM.
_OFFER_AFFIRMATIVES = frozenset({
    "yes", "yeah", "yea", "yep", "yup", "yah", "ya", "sure", "ok", "okay",
    "please", "yes please", "ok sure", "sounds good", "go ahead", "do it",
    "absolutely", "definitely", "certainly", "of course", "lets do it",
    "let's do it", "continue", "resume", "go for it",
})
_OFFER_NEGATIVES = frozenset({
    "no", "nope", "nah", "no thanks", "no thank you", "not now", "im good",
    "i'm good", "all good", "thats all", "that's all", "nothing else", "done",
})


def _offer_affirmative(text):
  """True when text is a short, pure affirmative reply to a resume offer."""
  if not text:
    return False
  norm = " ".join(text.lower().translate(_STRIP_PUNCT).split())
  if norm in _OFFER_AFFIRMATIVES:
    return True
  words = norm.split()
  if len(words) <= 5 and words and words[0] in _OFFER_AFFIRMATIVES:
    return not any(w in ("no", "not", "dont", "stop", "cancel") for w in words)
  return False


def _offer_negative(text):
  """True when text is a short, pure decline of a resume offer."""
  if not text:
    return False
  return " ".join(text.lower().translate(_STRIP_PUNCT).split()) in _OFFER_NEGATIVES


def _resume_offer_directive(sm, gate_slot, n_user_turns, last_user_text):
  """Post-termination resume offer.

  When a flow has terminated but a paused flow remains and auto-resume was
  deferred, offer to continue it and resolve the user's reply deterministically.
  Returns a phase directive (offer made / confirmed / declined / hold) or None to
  let the turn proceed. Mutates sm (offer bookkeeping); affirmative/negative are
  derived from last_user_text."""
  is_affirmative = _offer_affirmative(last_user_text)
  is_negative = _offer_negative(last_user_text)
  if not (sm.get("status") in ("complete", "zombie", "escalated")
          and sm.get("_flow_state") and sm.get("_auto_resume_deferred")
          and gate_slot and not sm.get("filled", {}).get(gate_slot)):
    return None
  offer_flow = (sm.get("_resume_offer_pending")
                or str(sm["_flow_state"][-1].get("flow")))
  if sm.get("_resume_offer_pending"):
    new_turn = n_user_turns > sm.get("_resume_offer_turn", -1)
    if new_turn and is_affirmative:
      sm.pop("_resume_offer_pending", None)
      _log("resume_offer_confirmed", flow=offer_flow)
      return {"preempt": True, "tag": "offer_confirmed",
              "function_call": {"name": "resume_flow",
                                "args": {"flow": offer_flow}}}
    if new_turn and is_negative:
      sm.pop("_resume_offer_pending", None)
      sm.pop("_auto_resume_deferred", None)
      _log("resume_offer_declined", flow=offer_flow)
      return {"tag": "offer_declined"}
    if new_turn:
      sm.pop("_resume_offer_pending", None)
      sm.pop("_auto_resume_deferred", None)
      _log("resume_offer_dropped", flow=offer_flow)
      return None
    return {"tag": "offer_hold"}
  sm["_resume_offer_pending"] = offer_flow
  sm["_resume_offer_turn"] = n_user_turns
  _log("resume_offer", flow=offer_flow)
  return {"preempt": True, "tag": "offer_made",
          "message": (f"You still have a paused {offer_flow} in progress."
                      " Would you like to continue it?")}


def _reap_zombie_on_reentry(sm, raw_config, gate_slot):
  """Tear a re-entered zombie back to in_progress.

  On re-entry with the gate slot refilled and reset_on_complete set, clears the
  prior flow's results while carrying shared/zombie values forward. Mutates sm in
  place; no return."""
  if not (sm.get("status") == "zombie"
          and gate_slot and sm.get("filled", {}).get(gate_slot)):
    return
  raw_bootstrap = raw_config.get("bootstrap", {})
  if not raw_bootstrap.get("reset_on_complete"):
    return
  # Capture what carries forward BEFORE discarding the flow scope.
  carried_shared = dict(sm.get("_zombie", {}).get("shared_values", {}))
  shared_slots = set(sm.get("_shared_slots", []))
  shared_filled = {k: v for k, v in sm.get("filled", {}).items()
                   if k in shared_slots}
  gate_val = sm["filled"].get(gate_slot)
  _flow_clear(sm)  # discard the ENTIRE prior-flow scope, not just the 4 slot maps
  sm.pop("_zombie", None)
  sm["status"] = "in_progress"
  sm["filled"] = {gate_slot: gate_val} if gate_val else {}
  sm["filled"].update(carried_shared)
  sm["filled"].update(shared_filled)
  sm.pop("_auto_resume_deferred", None)
  sm.pop("_resume_offer_pending", None)
  _log("zombie_reaped_on_reentry", gate_slot=gate_slot, gate_val=gate_val)


# ═════════════════════════════════════════════════════════════════════
# TOOL ENTRY POINT
# ═════════════════════════════════════════════════════════════════════


def slot_filling_engine(input_data: dict[str, Any]) -> dict[str, Any]:
  """Run one turn of the slot-filling DAG engine (crash-safe entry).

  Thin wrapper over _slot_filling_engine_impl that GUARANTEES the module-global
  _sm_ref (the _log sink) is cleared on every exit path — normal return OR
  exception. Previously each return site reset it by hand; an exception or a
  missed return path leaked a reference to a finished turn's sm, so a later _log
  could write into stale state. The single finally makes the invariant total.

  `_TURN_CTX` belongs to the same set and is cleared with them. It holds a reference to
  the finished turn's stamps dict, and it is reset rather than merely dropped: turn 0
  with no stamps makes every turn-relative predicate read FALSE, so a stale read outside
  a turn refuses a gate instead of opening one.
  """
  global _sm_ref, _CONFIGS_THIS_TURN, _surface_ref
  try:
    return _commit_ask_rung(_slot_filling_engine_impl(input_data))
  finally:
    _sm_ref = None
    _CONFIGS_THIS_TURN = {}
    _surface_ref = None
    _TURN_CTX["stamps"] = {}
    _TURN_CTX["now"] = 0


def _slot_filling_engine_impl(input_data: dict[str, Any]) -> dict[str, Any]:
  """Run one turn of the slot-filling DAG engine.

  Called from before_model_callback. All state flows through
  the sm dict: passed in via input_data, modified in place,
  and returned in the result.

  Args:
    input_data: Dict with keys 'raw_config' (DAG config),
      'sm' (state machine dict), 'last_user_text' (user message),
      and 'event_data' (event data for pre-fill).

  Returns:
    Dict with 'action' (the engine result) and 'sm'
    (the updated state machine).
  """
  # pylint: disable=global-variable-not-assigned
  global _COMPILED_CONFIGS, _RAW_CONFIGS, _sm_ref, _CONFIGS_THIS_TURN, _surface_ref

  # raw_config is resolved AFTER the guards: before_model passes config_id and the
  # engine FETCHES + validates it (tool→tool calls work). Offline callers (the
  # directive oracle / unit tests, which have no `tools` global) instead pass
  # raw_config directly — when present it is used as-is and the fetch is skipped.
  raw_config = input_data.get("raw_config") or {}
  sm = input_data.get("sm", {})
  _sm_ref = sm
  # Per-turn injected child-config map (offline child resolution; see
  # _engine_load_config). Optional: absent for every existing single-config caller.
  _CONFIGS_THIS_TURN = input_data.get("configs") or {}
  last_user_text = input_data.get("last_user_text", "")
  # Keep the turn's message for the passes that follow a tool call, which the
  # engine re-invokes with EMPTY text. `<correction_focus>` quotes it back so the
  # model can take the new value from the correction itself instead of re-asking.
  if last_user_text:
    sm["_turn_user_text"] = last_user_text
  is_inactivity = input_data.get("is_inactivity", False)
  # Barge-in: prefer the callback's scalars (it sees every part of the turn); fall back to
  # matching the raw envelope so the offline simulator, which never loads the callbacks
  # package, still exercises every path below. Blank the text when the envelope was ALL
  # the turn carried — live, before_model has already done this; offline, nobody has.
  is_barge_in = bool(input_data.get("is_barge_in", False))
  barge_heard = input_data.get("barge_heard", "") or ""
  if not is_barge_in and last_user_text:
    _bm = _BARGE_ENVELOPE.search(last_user_text)
    if _bm:
      is_barge_in = True
      barge_heard = (_bm.group("heard") or "").strip()
      if not _BARGE_ENVELOPE.sub(" ", last_user_text).strip():
        last_user_text = ""
  init_user_text = input_data.get("init_user_text", "")
  event_data = input_data.get("event_data") or {}
  # Channel for channel-aware response overrides. The inbound channel arrives in
  # event_data["channel"] (populated only on the session's first turn); persist it
  # stickily into sm so channel-aware responses resolve on every later turn too,
  # where event_data no longer carries it. Downstream resolution reads
  # sm["channel"] (see _run_slot_filling / _resolve_response).
  _event_channel = event_data.get("channel") if isinstance(event_data, dict) else None
  if _event_channel and not sm.get("channel"):
    sm["channel"] = _event_channel
  config_id = input_data.get("config_id", "default")
  transfer_slots = input_data.get("transfer_slots") or {}
  # Contents-derived scalars (the scan stays in before_model — the engine cannot
  # read llm_request.contents — but every DECISION that consumes them lives here).
  n_user_turns = input_data.get("n_user_turns", 0)
  _turn_n_before = sm.get("_turn_n")
  # Published on sm so the no-match ladder can tell a NEW caller turn from the engine
  # re-invoking itself within one turn. Per-flow (deliberately not in the keep-set), so
  # it resets with everything else on a flow change.
  if sm.get("_turn_n") != n_user_turns:
    # Mark where this caller turn started in the invocation count, so a model-turn
    # filler can tell "we have passes to spare" from "we are deep into the ten-pass
    # budget" (_arm_model_filler). `_invoke_n` itself is cumulative for the session.
    sm["_invoke_at_turn"] = sm.get("_invoke_n", 0)
    # Turn-scoped: a new caller turn may legitimately re-fire (retry-next-turn), #698.
    sm.pop("_sync_fire_pending", None)
  sm["_turn_n"] = n_user_turns
  # WHO took this turn. before_model classifies it off the request contents because the
  # engine's own inputs cannot: an asynchronous completion push and a post-setter
  # re-invoke arrive here identical on every one of them. Absent (an older caller, or an
  # offline one that does not supply it) it degrades to the reading everything used
  # before -- a tick is the platform, anything else is the caller.
  _turn_kind = input_data.get("turn_kind") or (
      "manufactured" if is_inactivity else "caller")
  # "caller" only latches when the caller-turn counter has actually MOVED, and that
  # guard is the whole difficulty. A re-invoke is supposed to arrive as "continuation",
  # but measured on a live call it very often does not: once CES has blanked an
  # inactivity or completion envelope, the newest user content in the request is the
  # caller's last real utterance again, so the pass classifies as "caller" and used to
  # overwrite the latch mid-turn. The first live drive lost a completion push exactly
  # that way -- the ticks stayed silent, the push spoke -- while every offline test
  # passed, because offline nothing re-invokes.
  #
  # `n_user_turns` counts only real caller utterances, so it is frozen for the whole of
  # a manufactured turn AND all its passes, which makes it the reliable edge. A
  # manufactured reading always latches: there is no turn it could be lying about.
  _latched_turn = sm.get("_turn_kind_at")
  if _turn_kind == "manufactured" or (
      _turn_kind == "caller" and _latched_turn != n_user_turns):
    sm["_turn_kind"] = _turn_kind
    sm["_turn_kind_at"] = n_user_turns
  # Not storing IS the inheritance: everything downstream reads this off sm. Defaulted
  # for a first invocation with no history, where speaking is the safe direction.
  _turn_kind = sm.setdefault("_turn_kind", "caller")
  # Is the state we were handed OLDER than the conversation?
  #
  # CES delivers an asynchronous completion by resuming the invocation that made the
  # call, and that invocation carries the session state it had AT THE TIME -- so on a
  # completion turn sm can predate caller turns that have since happened. Measured on a
  # live call: the engine was handed `_turn_n=1, _awaiting="reason", task_results={}` on
  # a turn where the caller had spoken twice and the question had already been put.
  # Every sm-derived guard is blind on that turn, which is why the first two drives
  # still heard the question repeated on the poll while the ticks were correctly silent.
  #
  # `n_user_turns` is the escape, because it is not sm at all: before_model counts real
  # utterances off the request contents, which are always current. On a manufactured
  # turn the caller has said nothing, so a live sm MUST already agree with that count.
  # If it is behind, it is a snapshot -- there is no other way for the two to disagree.
  _sm_stale = _turn_kind == "manufactured" and (_turn_n_before or 0) < n_user_turns
  sm["_sm_stale"] = _sm_stale
  if _sm_stale and sm.get("_task_just_completed"):
    # Which task's landing this is, captured HERE because the task handling below pops
    # `_task_just_completed` long before the question is chosen. It is the only thing
    # that can tell a question the completion unblocked from one it did not.
    #
    # Guarded on the key being SET, not written unconditionally: a completion turn runs
    # several passes and only the first carries it, so an unconditional write blanked
    # the capture on pass two and every question looked like one the landing had not
    # unblocked.
    sm["_stale_landed_task"] = sm["_task_just_completed"]
  _log("turn_kind", kind=_turn_kind, turn=n_user_turns, inactivity=is_inactivity,
       stale=_sm_stale)
  # Before the first condition of the turn is evaluated, and every fill stage below
  # evaluates some. `_stamp_fills` republishes at the end once this turn's own fills
  # have been stamped; this is the same dict, so that stays true.
  _publish_turn_ctx(sm, n_user_turns)
  # The mark means "fired, not yet intaken", so the landing of the result is what ends
  # it. The turn boundary above cannot be the only release: `_turn_n` counts CALLER
  # turns, so a silent caller never reaches it, and a repeated loop fires the same task
  # once per ELEMENT inside a single turn. Dropped here rather than by conditioning the
  # eligibility check on task_results, because the loop CLEARS that result to let the
  # next element run -- a mark still standing at that moment would strand the loop.
  _fired_pending = sm.get("_sync_fire_pending") or {}
  for _fired_task in [t for t in _fired_pending if t in (sm.get("task_results") or {})]:
    _fired_pending.pop(_fired_task, None)
  # The other half of the wait clock (`_wait_clock`). `is_inactivity` is a GENUINE
  # silence turn and nothing else: `before_model` computes it from this turn's own
  # parts, so it is true on the tick's first pass and false on every re-invoke the tick
  # goes on to make — which is exactly the once-per-turn increment a poll guard needs.
  # Counted separately from `_turn_n` rather than folded into it because the two answer
  # different questions, and every conversational ladder wants the caller's count.
  if is_inactivity:
    sm["_tick_n"] = int(sm.get("_tick_n") or 0) + 1
  scanned_user_text = input_data.get("scanned_user_text", "")
  # A TURN THE CALLER DID NOT TAKE MUST NOT FILL A SLOT.
  #
  # `scanned_user_text` is the last REAL utterance in the history, and on most turns that
  # is this turn's own. The engine is re-invoked several times WITHIN one turn (after a
  # setter, after a terminal fires) with `last_user_text` empty, and falling back to the
  # scan is what keeps a deterministic cue match seeing the words the caller just said.
  #
  # On a turn the caller did not take, that same fallback reaches back into a PREVIOUS
  # turn, and a cue match on those words is the engine answering — from a sentence that
  # was never a reply to the question it is being read as. Two such turns arrive, and
  # neither can be told from a within-turn re-invoke by the text alone:
  #
  #   an INACTIVITY tick        silence, by definition. `effective_user_text` already
  #                             refuses this same fallback (#517), one stage later and
  #                             for the same reason; the cue pass runs before it and was
  #                             never given the guard.
  #
  #   a COMPLETION DELIVERY     the async result is the whole content of the turn. If the
  #     carrying no utterance   caller DID speak, the scan is this turn's and stays
  #                             usable — that is how an answer given during a wait is
  #                             still captured. If they did not, it is a turn old.
  #
  # Measured on an agent that offers something during an async wait. The caller was asked
  # whether they wanted to try it, said nothing at all on the turn the completion landed
  # on, and their PREVIOUS turn — a description of the fault, which is what the offer was
  # about — matched the offer slot's own decline cue. The slot filled with the declining
  # value, the rung behind that value is terminal, and the call closed on a refusal
  # nobody made.
  #
  # Narrowed to those two turns on purpose. A blanket "never use the scan" takes the
  # within-turn re-invoke with it, which is the case the fallback exists for.
  #
  # LATCHED ON `sm`, because the flags do not outlive the turn's FIRST PASS. before_model
  # recomputes both per invocation: `is_inactivity` is documented above as true on the
  # tick's first pass and false on every re-invoke it goes on to make, and
  # `async_completion_landed` counts what was ingested THIS pass, which is nothing the
  # second time. The engine re-invokes itself several times within the turn and runs the
  # cue match on every one of them — so an unlatched guard holds for one pass and the
  # cascade pass fills the slot anyway. That cascade pass is where the measured hang-up
  # came from: the first pass spoke the completion's own line and the one behind it read
  # the stale words.
  #
  # Released by the next real utterance, and by nothing else. Keyed on the text as well,
  # so a scan that has moved on is trusted even if the release was missed.
  if last_user_text:
    sm.pop("_stale_scan", None)
  elif is_inactivity or input_data.get("async_completion_landed"):
    sm["_stale_scan"] = scanned_user_text
  _scan_is_stale = bool(scanned_user_text
                        and scanned_user_text == sm.get("_stale_scan"))
  fresh_scan = "" if _scan_is_stale else scanned_user_text
  if _scan_is_stale:
    _log("stale_scan_withheld", inactivity=bool(is_inactivity),
         chars=len(scanned_user_text))
  # `_directive_open` is turn-scoped. It is cleared when a preempt consumes it, but a
  # turn that ends NORMALLY never reaches that site — so without this it survives into
  # later turns and an unrelated preempt reports a cancellation that never happened.
  # Keyed on the utterance CHANGING: the engine is re-invoked several times within one
  # turn carrying the same text, and a presence test would clear it on every pass.
  _dir_text = str(input_data.get("last_user_text") or "")
  if _dir_text and _dir_text != sm.get("_directive_turn_text"):
    sm["_directive_turn_text"] = _dir_text
    sm.pop("_directive_open", None)
  carried_gate_text = input_data.get("gate_user_text", "")
  pending_transfer = input_data.get("pending_transfer", "")

  # ── Transfer dispatch + no-config guards. A pending transfer preempts with the
  # transfer response; no active config means there is nothing to drive
  # (before_agent has not resolved one yet) — emit a no-op so the agent's own SI
  # handles the turn.
  if pending_transfer:
    _log("transfer_dispatched", agent=pending_transfer)
    return {"action": {"preempt": True, "tag": "transfer",
                       "response": [{"type": "transfer",
                                     "agent": pending_transfer}]}, "sm": sm}
  if not config_id:
    return {"action": {"tag": "no_config"}, "sm": sm}

  # The entry (agent/parent) config identity is the fixed point every in-pass
  # re-walk resolves FROM: _active_config derives the live config off _call_stack
  # relative to it, so a fire-path descent (frame ADDED -> child) and a repeated
  # return re-walk (frame REMOVED -> parent) both land correctly (§R2.4). Resetting
  # per iteration is a no-op for the fire path (config_id already == entry there).
  _entry_config_id = config_id
  _entry_raw_config = raw_config
  for _descent_iter in range(3):  # up to two in-pass re-walks: a repeated Mode-B return re-fires the component then descends the next element (§R2.4)
    # A repeated non-final _frame_return steers the re-walk to the PARENT config (which owns the component
    # task) so it re-fires and pushes the next element's frame this pass; without it the reset below lands on
    # the entry config, which LIVE is the CHILD (no component task) → the frame is never re-pushed and the
    # next turn mis-resolves to the router. One-shot: dropping raw_config forces a re-fetch of the parent.
    _rewalk = sm.pop("_rewalk_config", None)
    if _rewalk:
      config_id, raw_config = _rewalk, {}
    else:
      config_id = _entry_config_id
      raw_config = _entry_raw_config
    # ── Config FETCH. The live path passes only config_id, so the engine fetches
    # the config via a tool call ({config_id}_dag). Offline callers (oracle / unit
    # tests, no `tools` global) pass raw_config and skip the fetch. Validation is a
    # design-time linter, not a runtime step.
    # Component call-frame seam (§1): when a frame is active, resolve to the CHILD
    # config (and drop any caller-passed parent raw_config so the fetch re-resolves
    # the child). One point makes the whole pipeline frame-aware.
    config_id, raw_config = _active_config(sm, config_id, raw_config)
    if not raw_config:
      raw_config = _engine_load_config(config_id)

    # Surface the flow-level terminal-return declaration for before_model to apply at the
    # end (flows.end_params_handoff / Flow.on_end). The engine cannot deliver it itself —
    # its `from_state` is a SESSION variable, which lives in callback_context.state, not the
    # engine's sm — so the engine only forwards the config; before_model resolves it. Set
    # every turn from the ACTIVE config so it tracks a flow switch. Absent -> None (no-op).
    sm["_on_end"] = raw_config.get("on_end")

    # Resolve the delivery surface now that the config (and so any app-declared
    # `surfaces` / `default_surface`) is final. Everything downstream — conditions,
    # capability gates, the system-instruction block — reads the module handle
    # rather than threading a surface argument through forty call sites.
    _surface_ref = _resolve_surface(sm, raw_config, is_inactivity)

    # ── Config-derivation. On a config change, derive the control-tool / gate / flow
    # lookups from raw_config into sm. Runs BEFORE the intent inject (which reads
    # _cancel_tool / _route_cues / …) and before slot_intake (reads _bootstrap).
    # _first_engine_run is set here and consumed by render-capture below in the same
    # session's first in-flow turn.
    if sm.get("_config_id") != config_id:
      sm["_config_id"] = config_id
      sm["_first_engine_run"] = True
      # R1 rebind-hazard guard (§2.2): a Component descent/return arranges the scope
      # (seeded child / restored parent incl. the done-marker) and sets the one-shot
      # _frame_transition. That flag SUPPRESSES ONLY the task_results wipe — so the
      # restored done-marker survives a return, and the seeded child scope survives a
      # descent. On an ordinary cross-flow switch the flag is absent and task_results
      # is wiped as before.
      if not sm.pop("_frame_transition", False):
        sm["task_results"] = {}
      # Per-config setter/task/slot lookups are derived once by
      # _stash_after_tool_mappings (guarded on "_setter_slots" not in sm). They must
      # ALWAYS be cleared on a config change — a cross-flow switch, AND a component
      # descent (child needs ITS setters) / return (parent's are re-derived) — else
      # slot_intake would use the prior config's setters and silently drop values.
      for _k in ("_setter_slots", "_multi_setter_slots", "_slot_requires",
                 "_slot_validates", "_executor_tasks"):
        sm.pop(_k, None)
      sm["_bootstrap"] = raw_config.get("bootstrap")
      # Cancelable/escalatable by default for flows; a router has nothing to abandon
      # (override per config with explicit "cancelable"/"escalatable").
      _not_router = not raw_config.get("router", False)
      sm["_cancel_tool"] = (_CONTROL_TOOLS[_CANCEL_SLOT]
                            if raw_config.get("cancelable", _not_router) else "")
      sm["_escalate_tool"] = (_CONTROL_TOOLS[_ESCALATE_SLOT]
                              if raw_config.get("escalatable", _not_router) else "")
      # Authored destination for an intent change the caller did not name a target for
      # (`intent_change.switch`). Until now this key was read only by the transfer-map
      # diagram; the runtime ignored it, so the declaration was inert.
      sm["_intent_switch"] = (raw_config.get("intent_change") or {}).get("switch") or ""
      # The intent-change SETTER, from config. Kept beside _cancel_tool/_escalate_tool
      # because the SI advertises all three the same way — and, like them, it must be ""
      # when the app has not declared one.
      sm["_intent_changed_tool"] = (
          (raw_config.get("intent_change") or {}).get("tool") or "")
      sm["_gate_slot"] = raw_config.get("gate_slot")
      # Single-flow (gate-less standalone) mode: no host/router fills the gate, so
      # the engine self-seeds it on entry (see auto-seed-on-entry below). Stored
      # beside _gate_slot; every consumer guards on it, so the gated path is unchanged.
      sm["_single_flow"] = bool(raw_config.get("single_flow"))
      sm["_flow_types"] = list(raw_config.get("flow_types") or [])
      sm["_route_cues"] = raw_config.get("route_cues") or {}
      # DEFAULT/home-base flow: when routing matches no cue, the router activates this flow instead of
      # leaving the model to guess (or hallucinate "I can't access that flow"). An orchestrator whose
      # single primary intent is one flow (a fan-out/verdict) names itself here so every unmatched
      # utterance lands on it. Config-driven via bootstrap.default_flow (one blessed form, alongside its
      # siblings intent_first/auto_seed); absent ⇒ "" ⇒ routing is byte-for-byte unchanged for every
      # existing agent.
      sm["_default_flow"] = (raw_config.get("bootstrap") or {}).get("default_flow") or ""
      # Intent-first (two-pass) mode: classify each in-flow user turn (Pass A) before
      # the focused action turn (Pass B). Config-driven via bootstrap.intent_first so
      # baseline (flag off) is byte-for-byte unchanged. Routers never classify.
      sm["_intent_first"] = bool(
          (raw_config.get("bootstrap") or {}).get("intent_first"))

    # We have arrived at a specialist (non-router) agent — clear the router-dispatch
    # one-shot so a future Host turn can deterministically re-dispatch if needed.
    if not raw_config.get("router"):
      sm.pop("_router_dispatched", None)

    # ── Intent-first gate (two-pass classification). On a genuine in-flow user turn
    # (flow active, not terminal), the FIRST model pass is a classification pass: the
    # SI is rewritten to a classifier and only classify_turn_intent is visible (Pass
    # A). The classify tool's intake sets _pending_intent (+ _classified /
    # _correction_recollect for non-`continue` intents). The next pass consumes that
    # marker and falls through to the unchanged pipeline (Pass B). _intent_pass keeps
    # every later pass of the SAME user turn (the DAG cascade) in Pass B without
    # re-classifying; a new user turn (n_user_turns increments) re-classifies.
    if (sm.get("_intent_first") and not raw_config.get("router")
        and sm.get("status", "in_progress") not in (
            "complete", "zombie", "escalated")
        and raw_config.get("gate_slot")
        and sm.get("filled", {}).get(raw_config["gate_slot"])):
      pass_state = sm.get("_intent_pass") or {}
      if sm.get("_pending_intent") is not None:
        intent = sm.pop("_pending_intent")
        sm["_intent_pass"] = {"turn": n_user_turns, "intent": intent}
        sm["_classify_mode"] = False
        # fall through to Pass B (the classify intake already staged any control
        # state; _intent_directive / _correction_focus_directive route it, and arm
        # _skip_pass_a_once when they inject a transition — see _intent_directive).
      elif sm.pop("_skip_pass_a_once", False):
        sm["_classify_mode"] = False
        sm["_intent_pass"] = {"turn": n_user_turns, "intent": "continue"}
        # destination of a same-turn transition — proceed without re-classifying.
      elif pass_state.get("turn") == n_user_turns or is_inactivity:
        sm["_classify_mode"] = False
        # already classified this user turn — fall through to Pass B (cascade).
        # is_inactivity (#517): silence is not an intent to classify. Route an
        # inactivity turn straight to Pass B so the no_input silent-tick ladder
        # runs, regardless of pass_state/n_user_turns timing — never hand a
        # `<context>no user activity>` turn to the classifier (whose try_again
        # re-invoke would then recompute is_inactivity=False and lose the silence).
      else:
        # Deterministic slot-fill PRECEDES classification: if the awaited slot declares option_cues and
        # the utterance matches exactly one value, fill it and SKIP Pass A — a direct slot answer is not
        # an intent to classify. Without this, two-pass intent_first consumes the utterance in the
        # classifier before the (Pass-B) slot-fill sees it, so an enum router like journey_intent never
        # gets its deterministic value. Compile is cached, so this is cheap; it only diverges from the
        # classify path when option_cues actually matches → byte-identical otherwise.
        _ck = (config_id, _config_fingerprint(raw_config))
        if _ck not in _COMPILED_CONFIGS:
          _COMPILED_CONFIGS[_ck] = _compile_config(raw_config)
        _apply_option_cues(sm, _COMPILED_CONFIGS[_ck], last_user_text or fresh_scan,
                           is_routing=bool(init_user_text),
                           fresh=bool(init_user_text or last_user_text))
        if sm.get("_event_prefilled_this_turn"):
          sm["_classify_mode"] = False
          sm["_intent_pass"] = {"turn": n_user_turns, "intent": "continue"}
          # deterministic fill handled the turn → fall through to Pass B (deliver the next question).
        else:
          sm["_classify_mode"] = True
          return {"action": _arm_classify_filler(
              sm, raw_config, _pass_a_directive(sm, raw_config),
              last_user_text), "sm": sm}

    # ── Zombie transfer finalizer. A flow concluded to a zombie carrying an
    # un-delivered `transfer_to` (its terminal turn never emitted the transfer —
    # e.g. a stacked/flaky pass). _flow_clear emptied the gate slot so
    # _reap_zombie_on_reentry no-ops, and the status-blind route/switch backstops in
    # _intent_directive would re-derive the just-left flow -> empty render. Dispatch
    # the carried transfer ONCE (pop = idempotent) BEFORE any intent re-derivation.
    # A successfully-delivered transfer already popped transfer_to (see
    # _zombie_exit_parts / the after_model terminal fallback), so this only fires on
    # the genuinely-undelivered path. Zero new state — reuses _zombie/status.
    _zf = sm.get("_zombie") or {}
    if sm.get("status") == "zombie" and _zf.get("transfer_to"):
      dest = _zf.pop("transfer_to")
      _log("zombie_transfer_finalize", dest=dest)
      return {"action": {"preempt": True, "message": "",
                         "response": [{"type": "transfer", "agent": dest}],
                         "tag": "zombie_transfer_finalize"}, "sm": sm}

    # ── Deterministic intent inject (keyword classifier + resolver). Runs before
    # the router short-circuit so a router (the Host) still gets flow-control injects
    # (cancel / escalate / route). Reads the sm lookups derived just above. When it
    # injects, the turn is decided; otherwise fall through.
    # A control inject below (cancel / escalate) fires a SETTER, and slot_intake can
    # only record it through `_setter_slots` — which is stashed with the compiled config
    # further down, AFTER this early return. On the first turn of a flow that stash has
    # not happened yet, so a caller who opens with "I want a person" produced a
    # transfer_to_human call that nothing could map: the control slot never filled, the
    # `escalate` disposition never applied, and the DAG spoke a verdict over the top of
    # the hand-off. Stash first (non-router only — a router has no slots to compile).
    # Compile is cached and the stash is one-shot, so later turns pay nothing.
    if not raw_config.get("router"):
      _pre_key = (config_id, _config_fingerprint(raw_config))
      if _pre_key not in _COMPILED_CONFIGS:
        _COMPILED_CONFIGS[_pre_key] = _compile_config(raw_config)
      _stash_after_tool_mappings(sm, _COMPILED_CONFIGS[_pre_key])

    # ── Router re-entry forced-classify CONSUMPTION (Pass 2). On the pass AFTER the
    # re-entry classifier ran (armed in the router short-circuit below), the intake has
    # staged the model's verdict (_pending_intent, + _classified for switch/escalate);
    # after_model defaults a text turn to `continue`. Map the label to a deterministic
    # route HERE, BEFORE _intent_directive, so a `switch`/`continue` verdict flows through
    # the SAME _intent_inject switch branch as any other transition (set_active_flow
    # preempt). Gated on _classify_mode (armed only on a router re-entry turn) + a pending
    # label ⇒ a strict no-op for every non-router / non-armed turn.
    if (raw_config.get("router") and sm.get("_classify_mode")
        and sm.get("_pending_intent") is not None):
      _re_intent = sm.pop("_pending_intent")
      sm["_classify_mode"] = False
      sm["_intent_pass"] = {"turn": n_user_turns, "intent": _re_intent}
      _re_target = str(sm.pop("_router_reentry_intent", "") or "").strip()
      _re_ftypes = raw_config.get("flow_types") or []
      if _re_intent == "end":
        # Wind-down ("that's all, bye") maps to end_session — do NOT force the caller
        # back into the last flow (the adversary's over-route break).
        _log("reentry_end")
        return {"action": {"preempt": True, "tag": "reentry_end",
                           "function_call": {"name": "end_session", "args": {}}},
                "sm": sm}
      # switch:<flow> / escalate: the classify intake already staged _classified — leave
      # it for _intent_directive/_intent_inject below. continue / anything else (incl. a
      # defaulted text turn) re-routes to the recorded last intent while the bounded
      # counter allows; once spent (or the target is missing/unadvertised) we DON'T stage
      # a route, so the turn falls through to the plain router below and the shipped
      # steering disambiguate / on_exhaust net (after_model) owns the escalation.
      elif not sm.get("_classified"):
        if (int(sm.get("_reentry_count") or 0) <= _REENTRY_CAP
            and _re_target and _re_target in _re_ftypes):
          sm["_classified"] = {"intent": "switch", "target": _re_target}
          _log("reentry_continue_reroute", flow=_re_target,
               count=sm.get("_reentry_count"))
        else:
          _log("reentry_reroute_exhausted", target=_re_target,
               count=sm.get("_reentry_count"))

    intent_dir = _intent_directive(sm, last_user_text,
                                   no_input=raw_config.get("no_input"))
    if intent_dir is not None:
      return {"action": intent_dir, "sm": sm}


    # ── Phase: router agent (e.g. the Host) does flow-control only, no slot
    # collection — proceed so the agent's own instruction handles greeting/routing.
    # Returns before compile: a router has no slots/tasks to compile or stash.
    if raw_config.get("router"):
      # Deterministic router dispatch. When a flow is ALREADY active (gate slot
      # filled with a known flow) and in progress, route to its specialist
      # deterministically instead of letting the model do it. This is the post-reap
      # "bounce" turn: route_backstop just reaped a terminal flow and set the new
      # active_flow, then control re-entered the Host. Without this the Host MODEL
      # re-issues set_active_flow (a redundant 2nd call whose stacked transfer flakily
      # renders empty — "Hmm, I'm having trouble…"). A one-shot _router_dispatched
      # guard (cleared on specialist arrival, below) prevents any transfer loop.
      gate_slot = raw_config.get("gate_slot")
      active_flow = sm.get("filled", {}).get(gate_slot) if gate_slot else None
      if (active_flow and active_flow in (raw_config.get("flow_types") or [])
          and sm.get("status", "in_progress") not in (
              "complete", "zombie", "escalated")
          and not sm.get("_router_dispatched")):
        sm["_router_dispatched"] = True
        bootstrap_tool = (sm.get("_bootstrap") or {}).get("tool", "set_active_flow")
        _log("router_auto_dispatch", flow=active_flow)
        return {"action": {"preempt": True, "tag": "router_auto_dispatch",
                           "function_call": {"name": bootstrap_tool,
                                             "args": {"flow": active_flow}}},
                "sm": sm}
      # ── Router re-entry forced-classify ARMING. On the ONE clean router turn after a
      # defer handed off, before_agent stamped `_router_reentry_intent` (the last routed
      # intent) and bumped `_reentry_count` on the return-to-router reap. Here — gate
      # empty, in progress, intent-first — COMPEL the model to classify the caller's NEW
      # utterance rather than leaving it to volunteer set_active_flow (which it declines,
      # reading the follow-up as already handled → dead-end). CONSUMPTION above maps the
      # verdict on the next pass. Bounded by `_reentry_count`: once spent, DON'T arm — fall
      # to the plain router turn so the shipped steering disambiguate / on_exhaust net
      # (after_model) owns the hand-off. Inert unless the reap stamped the breadcrumb, so
      # every non-steering / cold / turn-1 router turn is byte-identical. A high-precision
      # route_cue still preempts first (via `_intent_directive` above), so a clearly-phrased
      # switch never reaches the classifier.
      _re = str(sm.get("_router_reentry_intent") or "").strip()
      if (sm.get("_intent_first") and _re
          and not sm.get("_classify_mode")
          and gate_slot and not sm.get("filled", {}).get(gate_slot)
          and sm.get("status", "in_progress") not in (
              "complete", "zombie", "escalated")
          and _re in (raw_config.get("flow_types") or [])
          and not is_inactivity
          and (sm.get("_intent_pass") or {}).get("turn") != n_user_turns
          and int(sm.get("_reentry_count") or 0) <= _REENTRY_CAP):
        sm["_classify_mode"] = True
        _log("reentry_classify_armed", flow=_re, count=sm.get("_reentry_count"))
        return {"action": _arm_classify_filler(
            sm, raw_config, _reentry_classify_directive(sm, raw_config, _re),
            last_user_text), "sm": sm}
      # ── Cold router turn: hide the Pass-A-only classify_turn_intent. The re-entry
      # forced-classify path returned ABOVE (with classify deliberately VISIBLE), so we
      # reach here only when re-entry is NOT armed. classify has no role on a cold routing
      # turn — the router's job is to volunteer set_active_flow — but a SINGLE-AGENT router
      # DECLARES classify_turn_intent (the re-entry classifier needs a tool to compel; see
      # _ROUTER_KEEP_TOOLS) even when the router is not itself intent-first (e.g. it has
      # intent-first SUB-FLOWS), so left visible the model volunteers it ("Output ONLY this
      # tool call") and burns ~1s. Hide it here (this path never runs `_hiding_policy`)
      # UNCONDITIONALLY — hiding a tool the agent never declared is a no-op, so this is safe
      # for every router; `not _classify_mode` preserves any in-flight re-entry classify.
      _router_action = {"tag": "router"}
      if not sm.get("_classify_mode"):
        _router_action["hide_tools"] = ["classify_turn_intent"]
      _welcome = _router_welcome(sm, raw_config, _router_action, last_user_text)
      if _welcome is not None:
        return {"action": _welcome, "sm": sm}
      return {"action": _arm_classify_filler(sm, raw_config, _router_action,
                                             last_user_text), "sm": sm}

    # ── Compile (cached per config_id) and stash the setter/task maps up front so
    # even the gate/terminal render turns leave slot_intake's lookups in place
    # before the model's tools fire (an entry turn can set a slot alongside the
    # bootstrap setter). Both are one-shot: compile is cached, stash is guarded.
    _cfg_key = (config_id, _config_fingerprint(raw_config))
    if _cfg_key not in _COMPILED_CONFIGS:
      _COMPILED_CONFIGS[_cfg_key] = _compile_config(raw_config)
    config = _COMPILED_CONFIGS[_cfg_key]
    _stash_after_tool_mappings(sm, config)

    gate_slot = raw_config.get("gate_slot")

    # ── Single-flow auto-seed on entry. A standalone gate-less agent has no host to
    # fill the gate, so the engine self-seeds it (mirrors the component-descent gate
    # auto-seed @415) → the flow is ALWAYS-ENTERED and collects slot #1 immediately.
    # Two cases, both single_flow-guarded (dead code on the gated path):
    #   1. Fresh entry (in_progress, gate empty): seed config_id (or bootstrap.auto_seed).
    #   2. Zombie re-entry after a terminal task: _terminate's _flow_clear wiped the
    #      gate, but the seed survives in _zombie["flow"]. Re-seed from it so the
    #      existing _reap_zombie_on_reentry (below) is armed — it then resets the
    #      scope to in_progress and a fresh request collects. (The host fills the gate
    #      on the gated reset path; single-flow has no host, so the engine does it.)
    if (sm.get("_single_flow") and gate_slot
        and not sm.get("filled", {}).get(gate_slot)):
      status = sm.get("status", "in_progress")
      seed_val = (raw_config.get("bootstrap") or {}).get("auto_seed") or config_id
      if status not in ("complete", "zombie", "escalated"):
        sm.setdefault("filled", {})[gate_slot] = seed_val
        _log("single_flow_auto_seed", gate_slot=gate_slot, val=seed_val)
      elif (status == "zombie"
            and (raw_config.get("bootstrap") or {}).get("reset_on_complete")):
        # Arm the reap from the surviving _zombie["flow"] (falls back to seed_val).
        sm.setdefault("filled", {})[gate_slot] = (
            (sm.get("_zombie") or {}).get("flow") or seed_val)
        _log("single_flow_reentry_seed", gate_slot=gate_slot)

    # ── Cross-agent transfer arrival: seed the gate so the destination flow opens ──
    # _terminate -> _flow_clear wipes `filled` INCLUDING the gate slot and sets
    # status=zombie. A native sibling `transfer_to` hands control straight to the
    # destination agent, so — unlike every host-routed hop — no set_active_flow runs to
    # refill the destination's gate. Every post-transfer turn therefore hit the gate
    # early-return below: the DAG never drove (no announce, no executor preempt, no
    # `requires` ever satisfiable) and the model improvised the whole call.
    # Guard: seed ONLY when the zombie belongs to a DIFFERENT flow (we truly arrived
    # from elsewhere). If THIS flow just completed, leave it closed — that is the
    # resume/reset_on_complete path, and forcing it open there ends the session twice
    # (400 SESSION_ALREADY_ENDED).
    if (gate_slot and not sm.get("filled", {}).get(gate_slot)
        and sm.get("status") == "zombie"
        and (raw_config.get("bootstrap") or {}).get("reset_on_complete")
        and (sm.get("_zombie") or {}).get("flow") != config_id):
      sm.setdefault("filled", {})[gate_slot] = config_id
      _log("transfer_arrival_gate_seed", gate_slot=gate_slot, val=config_id)

    # ── Post-termination resume offer ──
    offer = _resume_offer_directive(
        sm, gate_slot, n_user_turns, last_user_text)
    if offer is not None:
      return {"action": offer, "sm": sm}

    # ── Zombie-reap on re-entry, then render-capture: on gate/terminal render
    # turns stash the latest real user text as _gate_user_text (consumed as
    # init_user_text on the first in-flow turn). Reap runs first so is_render_turn
    # reflects the post-reap status. The contents scan runs in before_model and
    # arrives here as scanned_user_text (the engine has no llm_request). ──
    _reap_zombie_on_reentry(sm, raw_config, gate_slot)
    # The reap's _flow_clear strips the setter/task maps (they are not in the
    # keep-set). On the gated reset path the host's gate-filling setter forces a
    # fresh entry pass that re-stashes before the user's next setter; a gate-less
    # single-flow reset has no such pass, so re-derive here. Guarded on
    # "_setter_slots" not in sm → a no-op on every turn except right after a reap.
    _stash_after_tool_mappings(sm, config)
    is_render_turn = ((gate_slot and not sm.get("filled", {}).get(gate_slot))
                      or sm.get("status") in ("complete", "zombie", "escalated"))
    gate_writes = {"set": {}, "pop": []}
    if is_render_turn:
      # A gate/terminal render turn lets the model run WITHOUT going through
      # _route_payloads (the only place last turn's stash is cleared). So a
      # collection-question's chips stashed on the turn just before a cancel /
      # escalate / completion / re-route would otherwise survive and be injected by
      # after_model onto this terminal/gate turn (a stale "pick a value" chip riding
      # a "transferring you" / "cancel?" reply). Render turns never carry a
      # collection payload — clear the stash here, mirroring _route_payloads.
      sm.pop("_pending_payloads", None)
      sm.pop("_pending_question_payloads", None)
      if scanned_user_text:
        gate_writes["set"]["_gate_user_text"] = scanned_user_text
    else:
      init_user_text = carried_gate_text
      gate_writes["pop"].append("_gate_user_text")
      first_run = sm.pop("_first_engine_run", False)
      if not init_user_text and first_run:
        init_user_text = scanned_user_text

    # ── Phase: the gate and terminal turns render SI + the hide policy without
    # slot-filling. Gate takes precedence over terminal.
    #
    # Both hide EVERY slot setter, because neither phase collects a slot. They used to
    # apply `_hiding_policy` alone, which covers flow-control tools and no setters — so
    # on a turn before any flow started, or after one had already finished, the entire
    # app's setter surface sat callable in the function-calling schema while the prompt
    # advertised none of it. An unadvertised tool is a lure: the model reaches for the
    # nearest plausible one, invents a reason to have called it, and the engine then
    # rejects the value because the slot is inactive.
    if gate_slot and not sm.get("filled", {}).get(gate_slot):
      return {"action": _merge_writes(
          {"si": _render_si(sm, raw_config), "tag": "gate",
           "hide_tools": _phase_hidden_tools(sm, raw_config, "gate")},
          gate_writes, _publish_writes(sm, config)),
              "sm": sm}
    if sm.get("status") in ("complete", "zombie", "escalated"):
      return {"action": _merge_writes(
          {"si": _render_si(sm, raw_config), "tag": "terminal",
           "hide_tools": _phase_hidden_tools(sm, raw_config, "terminal")},
          gate_writes, _publish_writes(sm, config)),
              "sm": sm}

    _resolve_async_batch(sm, config.get("tasks", []))
    last_user_text = _apply_answer_first(
        sm, config, last_user_text,
        bool(input_data.get("async_completion_landed")))
    _apply_event_prefill(sm, config, event_data)
    _apply_dtmf_input(sm, config, last_user_text)
    # Deterministic cue→value routing for enum-ish slots (e.g. journey_intent). Runs AFTER event/dtmf
    # prefill so those keep precedence. The routing utterance arrives as init_user_text (carried gate
    # text) on the first in-flow pass, so match against it first; is_routing gates the override branch.
    # `fresh_scan` rather than the raw scan: on a turn the caller did not take, the scan
    # is a PREVIOUS turn's words and a cue match on them fills a slot nobody answered.
    _apply_option_cues(sm, config, init_user_text or last_user_text or fresh_scan,
                       is_routing=bool(init_user_text),
                       fresh=bool(init_user_text or last_user_text))

    # Value policy, LAST of the fill stages and before anything reads the DAG: every
    # producer (event prefill, dtmf, cues, and the task intake that ran on a previous
    # call) has had its say, so this is the one point where "what does this slot hold"
    # is finally answerable. Reject first, then default, so a sentinel falls through to
    # the fallback rather than blocking it.
    _apply_slot_rejects(sm, config)
    _apply_slot_defaults(sm, config)
    _stamp_fills(sm, n_user_turns)

    _stash_collection_hints(sm, config)
    correction_tool = config.get("correction_tool")

    # ── Two-phase intent recognition: defensive flag drop ──
    # The intent_changed flag (set by set_intent_changed) is consumed in
    # before_model — phase-2 deterministic injection or correction-focus. If a
    # stray flag reaches the engine, drop it so it does not trigger a spurious
    # readback of the passive intent_changed slot.
    sm.get("pending", {}).pop("intent_changed", None)

    # ── Transfer slots: consume Host-provided values into pending; the caller
    # persists the unconsumed remainder via the directive's state_writes.
    xfer_writes = None
    if transfer_slots:
      remaining = _consume_transfer_slots(sm, raw_config, transfer_slots)
      xfer_writes = ({"set": {"_transfer_slots": remaining}} if remaining
                     else {"pop": ["_transfer_slots"]})

    # ── Correction re-collect (phase 1): focused setter pass, if requested ──
    focus = _correction_focus_directive(
        sm, config, init_user_text, correction_tool)
    if focus is not None:
      _merge_writes(focus, xfer_writes, gate_writes)
      return {"action": focus, "sm": sm}

    # ── Correction apply (phase 2): stage the new value(s), clear downstream ──
    _apply_correction_pending(sm, config)

    # ── Run the DAG engine, then attach the SI directive ──
    # Intent-first: Pass B always runs AFTER the classify_turn_intent call, so
    # contents[-1] is that function_response (text=None) and `last_user_text` is
    # empty — which would silence everything keyed on the user's message (notably
    # steer-back, which early-returns on empty text and so never escalates off-topic
    # loops). Fall back to `scanned_user_text` (the latest real user message, scanned
    # None-safe from history). The _steer_last_text dedup prevents double-counting.
    effective_user_text = last_user_text
    # NOT on an inactivity turn (#517): before_model deliberately blanks
    # last_user_text on silence so the no_input silent-tick ladder fires. Falling
    # back to scanned_user_text here repopulates it with the LAST REAL utterance
    # ("hold on"), which _run_slot_filling's ladder reads as re-engaged speech —
    # the `if last_user_text` branch wins and the `elif is_inactivity` silent-tick
    # branch is never reached, so the model talks over a caller who asked for a
    # moment. Silence is not off-topic speech, so steer-back must NOT count it
    # either (it correctly early-returns on empty text).
    if (sm.get("_intent_first") and not effective_user_text
        and not is_inactivity):
      effective_user_text = scanned_user_text
    # `is_inactivity` guard: `init_user_text` is the last REAL utterance, and
    # feeding it to a silence tick would re-enter the speech branch and talk over
    # a caller who asked for a moment — the exact regression recorded just above
    # for `scanned_user_text`.
    action = _run_slot_filling(config, sm, last_user_text=effective_user_text,
                               is_inactivity=is_inactivity,
                               entry_user_text=(
                                   "" if is_inactivity else init_user_text),
                               is_barge_in=is_barge_in, barge_heard=barge_heard)
    # The DAG walk is a fill stage too, and the last one. An announce's `sets`, a rung's
    # latch and a task's outputs are all written HERE, after the sweep above — so without
    # a second sweep they carry no stamp until the next turn stamps them, and a
    # `since_turns` gate on a latch the walk wrote opens one turn later than it reads.
    # The shape that reaches a caller: an offer is made, they accept, and the acceptance
    # falls into a gate that is still shut because the offer counts as having happened
    # on the turn AFTER it was spoken.
    _stamp_fills(sm, n_user_turns)
    # Child failure-driven termination interception (§6.8 F2): a child task's retries
    # exhausting (status='escalated') or a child slot's end_session (status='complete')
    # while a Component frame is active is a child ABORT, not a conversation end. Route
    # it through on_abort like a cancel — _frame_abandon restores the parent scope and
    # arms the selector — and clear the child's terminal status so the next pass
    # re-walks the parent (or, on fail_flow, the deferred consumer terminates it). This
    # ONE guard covers every child-failure termination point uniformly. It does NOT
    # catch a genuine _terminate (status='zombie' — user escalate/cancel/completion),
    # which clears _call_stack and stays conversation-wide (F3); nor a normal child
    # return (handled by the terminal-branch fork before any status is set).
    if sm.pop(_DESCENT_CONTINUE, False) and _descent_iter < 2:
      continue  # in-pass descent: re-enter the selector, re-resolving from the entry config off _call_stack — a fresh child (fire) or the parent re-fire then next element (repeated return)
    # Ending the pass without another re-walk: drop any unconsumed re-walk hint so it can't leak the parent
    # config into the NEXT turn's first iteration (would override before_agent's resolution).
    sm.pop("_rewalk_config", None)
    if sm.get("_call_stack") and sm.get("status") in ("complete", "escalated"):
      _frame_abandon(sm, sm["_call_stack"][-1])
      sm.pop("status", None)
      action = dict(_DESCENT_END_PASS)
    action = _finalize_directive(sm, config, action, init_user_text)
    _merge_writes(action, xfer_writes, gate_writes, _publish_writes(sm, config))
    return {"action": action, "sm": sm}
