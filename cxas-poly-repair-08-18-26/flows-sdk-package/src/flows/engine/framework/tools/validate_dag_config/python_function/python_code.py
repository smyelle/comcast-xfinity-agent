# pylint: disable=invalid-name,g-doc-args,g-doc-return-or-yield,g-docstring-missing-newline,g-no-space-after-docstring-summary,g-short-docstring-punctuation,line-too-long,missing-function-docstring,protected-access
"""Slot filling DAG config validator — reusable across projects.

FRAMEWORK CODE — shared across all agents using the slot-filling engine.
Do not add agent-specific logic here; this validates DAG config structure
and cross-config interactions generically.

Validates structure and references in a DAG configuration dict.
Catches misconfigurations (broken references, loop risks, missing
fields) before the engine encounters them at runtime.

Usable as a CES tool (via validate_dag_config()) or directly from
pytest (via DagConfigValidator / CrossConfigValidator).
"""

import ast
import dataclasses
import json
import re
import string
from typing import Any


@dataclasses.dataclass
class ValidationResult:
  """Structured output from DAG config validation."""

  valid: bool = True
  errors: list[str] = dataclasses.field(default_factory=list)
  warnings: list[str] = dataclasses.field(default_factory=list)
  # Structured twin of errors+warnings+blockers: one dict per emitted
  # diagnostic {severity, message, code, anchor, fix_id}. errors/warnings stay
  # byte-identical; this is additive so every existing consumer is unaffected.
  diagnostics: list[dict] = dataclasses.field(default_factory=list)
  # Third tier (ship-blocker): needs-review items that do NOT fail validation
  # but should block an unattended ship. `valid` stays == len(errors)==0.
  blockers: list[str] = dataclasses.field(default_factory=list)

  @property
  def shippable(self) -> bool:
    """Derived: safe to ship unattended = valid AND no needs-review blockers."""
    return self.valid and not self.blockers


class Codes:
  """Stable diagnostic codes. Scheme = a two-letter family + 3 digits whose
  HUNDREDS digit is the sub-domain (see MEMORY: stable-code-taxonomy):

    SF###  single-config (DagConfigValidator)
      SF0##  structural / config-level (unknown keys, duplicates, missing name)
      SF1##  slot-level               (wiring, reachability, ask/hint)
      SF2##  task-level               (tool / inputs / outputs / reachability)
      SF3##  setter / control-flow    (duplicate setter, gate, steer_back, ...)
    XF###  cross-config (CrossConfigValidator)

  Codes are APPEND-ONLY and MUST stay stable: a message-text tweak keeps the
  same code (the Studio client + fix_synth.py switch on it, not on prose). Only
  ~a dozen high-traffic sites are coded so far; every other _error/_warn call
  still passes code=None and is mapped by validation.py's regex fallback until
  backfilled incrementally.
  """
  # SF0## structural / config-level
  UNKNOWN_CONFIG_KEY = "SF001"
  UNKNOWN_SLOT_KEY = "SF002"
  UNKNOWN_TASK_KEY = "SF003"
  DUPLICATE_SLOT_NAME = "SF010"
  SLOT_MISSING_NAME = "SF011"
  # A telephony hand-off payload emitted without the end_session that gives up the
  # leg — the caller is told a person is coming and is then kept on a dead call.
  HANDOFF_PAYLOAD_UNPAIRED = "SF020"
  # SF1## slot-level
  ANNOUNCE_SLOT_NO_MESSAGE = "SF101"
  SLOT_REQUIRES_UNKNOWN = "SF102"
  SLOT_UNREACHABLE = "SF103"
  USER_SLOT_NO_ASK = "SF104"
  SWITCHABLE_WITHOUT_CUES = "SF105"
  SWITCHABLE_NOT_INTENT = "SF106"
  SKIP_READBACK_UNKNOWN_SLOT = "SF107"
  SKIP_READBACK_WITHOUT_READBACK = "SF108"
  SLOT_EXHAUST_NO_DISPOSITION = "SF109"
  # SF2## task-level
  TASK_NO_TOOL = "SF201"
  TASK_INPUT_UNKNOWN = "SF202"
  TASK_UNREACHABLE = "SF203"
  # SF3## setter / control-flow
  DUPLICATE_SETTER = "SF301"
  CANCEL_MENU_RETURN_NO_SLOTS = "SF302"
  # XF### cross-config
  CONTROL_BLOCK_NO_TRANSFER = "XF001"

# code -> canonical INVARIANT message signature (a substring guaranteed to be
# present in every message emitted under that code). The backward-compat golden
# test asserts that every diagnostic carrying a non-None code has its code in
# this dict AND that the signature is a substring of the emitted message, so a
# careless message edit that drops the signature is caught. Two codes MAY share
# a signature (e.g. slot vs task "is unreachable"); the code + anchor.kind
# disambiguate downstream.
_CODE_MESSAGES = {
    Codes.UNKNOWN_CONFIG_KEY: "Unknown top-level config keys",
    Codes.UNKNOWN_SLOT_KEY: "has unknown keys",
    Codes.UNKNOWN_TASK_KEY: "has unknown keys",
    Codes.DUPLICATE_SLOT_NAME: "Duplicate slot names",
    Codes.SLOT_MISSING_NAME: "has no 'name'",
    Codes.HANDOFF_PAYLOAD_UNPAIRED: "hand-off payload with no 'end_session'",
    Codes.ANNOUNCE_SLOT_NO_MESSAGE: "requires 'message' or 'response'",
    Codes.SLOT_REQUIRES_UNKNOWN: "requires unknown",
    Codes.SLOT_UNREACHABLE: "is unreachable",
    Codes.USER_SLOT_NO_ASK: "has source 'user' but no 'ask'",
    Codes.SWITCHABLE_WITHOUT_CUES: "declares no 'option_cues'",
    Codes.SWITCHABLE_NOT_INTENT: "is not kind:'intent'",
    Codes.SKIP_READBACK_UNKNOWN_SLOT: "skip_readback_if_matches",
    Codes.SKIP_READBACK_WITHOUT_READBACK: "skip_readback_if_matches",
    Codes.SLOT_EXHAUST_NO_DISPOSITION: "on_exhaust disposes of nothing",
    Codes.CANCEL_MENU_RETURN_NO_SLOTS: "clear_slots",
    Codes.TASK_NO_TOOL: "has no 'tool' key",
    Codes.TASK_INPUT_UNKNOWN: "not in slots",
    Codes.TASK_UNREACHABLE: "is unreachable",
    Codes.DUPLICATE_SETTER: "all map to setter",
    Codes.CONTROL_BLOCK_NO_TRANSFER: "control block omits 'transfer_to'",
}


_VALID_SOURCES = frozenset({"user", "announce", "event"})

_VALID_CONFIG_KEYS = frozenset({
    "slots", "tasks", "gate_slot", "correction_tool", "bootstrap",
    "no_input",
    # `continue_cues` — the caller's following-along noises ("mhmm", "got it"). Absorbs
    # the turn so agreement is not counted as a stall by steer_back. Absent means ON.
    # `on_interrupted` — what this flow does on a turn the platform reports as a
    # barge-in: an optional line ({heard}/{unheard}), the same action arms as
    # no_input.on_exhaust, and a flow-wide default `repair` for its announces.
    "continue_cues", "on_interrupted",
    # `answer` — a flow-level free-response policy (a LIST of grounded,
    # intent-scoped fallback configs), a sibling of steer_back/no_input. On an
    # off-menu, on-intent turn the engine emits an `answer_directive` instead of
    # steering, hands the model the intent's grounding + a whitelisted read/
    # compute tool surface, then leaves the DAG unchanged. Shape checked in
    # _check_answer (keys in _VALID_ANSWER_KEYS).
    "answer",
    "steer_back", "cancel", "escalate", "intent_change",
    "confirm_transition_prefix",
    "exit_status", "event_mappings", "readback_response",
    "channel_readback_response",
    "shared_slots", "flow_types", "router", "route_cues", "flow_descriptions",
    "single_flow",
    # remote_tools: `{start tool: {status_tool, job_slot, outputs, timeout_seconds}}`,
    # lowered by the SDK from `flows.remote_tool(...)`. A remote task's tool answers
    # with a job HANDLE rather than the task's outputs, so the engine has to be told not
    # to read that answer as the result — and which tool to poll until the real one
    # arrives. A declared mock is NOT here: it is emitted as the status tool's own
    # `<tool>__status_mock`, which is the only thing that could honour it.
    "remote_tools",
    "surfaces", "default_surface",
    # variable_maps: lowered by the SDK from App(variable_maps=[...]) — the ordered
    # session-variable shapes the generated ingress callback pre-fills slots from
    # before the conversation starts. Inert to the engine except for the reconcile
    # that re-checks a seeded slot's condition against the COMPILED config; the
    # matching itself happens in before_agent, which has the timing but not the
    # compiled conditions. Shape checked in _check_variable_maps.
    "variable_maps",
    # Read by the ENGINE in _compile_config (~1350) and at config load (~7016):
    # False suppresses the SYNTHESIZED cancel/escalate control slot and stops its
    # framework tool being advertised to the model at all. Distinct from
    # `escalate.condition`, which still synthesizes the slot (the model can still
    # call transfer_to_human; the disposition is merely declined afterwards).
    # Default: `not is_router`.
    "cancelable", "escalatable",
    # speech: which classes of canned utterance the model may reword instead of
    # the framework speaking them verbatim (engine _maybe_improvise).
    "speech",
    # Flow-level default latency filler, used on a turn handed to the model whose
    # slot carries none of its own (engine _arm_model_filler).
    "filler_say",
    # A flow-level terminal return delivered on the end_session tool call's params
    # (flows.end_params_handoff / Flow.on_end). The framework's deterministic terminal
    # emit folds it onto every terminal end. Shape is well-formed by construction
    # (the end_params_handoff builder validates envelope/from_state).
    "on_end",
})

_VALID_SLOT_KEYS = frozenset({
    "name", "source", "ask", "setter", "setter_field", "requires",
    # `repair` — announce only. How to recover the lines the caller never heard when they
    # talked over this announce (flows.repair: mode / lead_in / max_repairs). Opt-in: an
    # announce without it behaves exactly as it always has.
    "repair",
    # `push_back` — the slot re-offer ladder (a caller who keeps declining an offer): the
    # engine's _push_back_tick reads it. Sibling of `validation` (bad value) / flow-level
    # `no_input` (silence) / `steer_back` (stalled). Inner keys checked by _VALID_PUSH_BACK_KEYS.
    "validation", "push_back", "requires_readback", "readback_fmt", "hint",
    # `channel_responses` is PLURAL — the only spelling the engine resolves. The
    # singular was accepted here for years while being silently ignored at runtime,
    # so a channel override authored through Slot Studio or Specter did nothing at
    # all. Rejecting it now turns that silence into an error.
    "response", "channel_responses", "message", "preempt", "condition",
    # `sets` — announce only. Extra slots written when the announce fires, which is what
    # lets a ladder of mutually exclusive announces exist: the first to fire closes the
    # shared gate the rest are conditioned on, on the cascade's own recompute.
    "sets",
    # Polymorphic surfaces: alternative WORDINGS of the ask, each gated by a
    # capability condition. Unlike `response` (which the client appends to what
    # the model said) a surviving variant REPLACES the ask — see the engine's
    # _resolve_variant_text.
    "ask_variants", "channel_ask_variants",
    # `relatch`: a rung's `sets` may re-arm this latch while it is already filled,
    # refreshing the turn `since_turns` measures from.
    "shared", "relatch", "validate_against", "event_key",
    # Value policy, applied by the engine after intake and before the DAG is walked.
    #  names values that are PRESENT but mean "not answered yet" (an upstream
    # sentinel) — the slot is cleared as though the producer never spoke.  is
    # a list of {value, when?} fallbacks, first matching wins, filling a slot nothing
    # produced: an absent value is indistinguishable from a benign one, which is how a
    # low-priority branch wins a comparison it should have lost.  mirrors the
    # value out to session variables for tools that read state, not parameters.
    "reject", "default", "publish",
    # Conversation-design: `dtmf_map` ({digit: value}) deterministically fills the
    # slot from an IVR keypad press (B1). Silence handling (`no_input`) is
    # FLOW-LEVEL only (a top-level config key), never per-slot (B2).
    "dtmf_map",
    # Latency filler for the turn this slot is collected on. Same field a task takes;
    # with no tool call to ride it is spoken as a partial preempt instead.
    "filler_say",
    # Conversation-design: `cue_priority` ("unique" | "first") is the tiebreak when the
    # utterance matches more than one `option_cues` value. Default "unique" = fill
    # nothing (historical); "first" = earliest DECLARED value wins (engine ~4750).
    "cue_priority",
    "multi_fill",
    # Conversation-design: `switchable` (bool) lets a caller change the subject mid-flow.
    # An already-filled intent value is re-decided on an unambiguous later cue match and
    # its dependents are cleared (engine `_abandon_journey`). Opt-in: without it an
    # intent slot can only be filled once per flow instance.
    "switchable",
    # Conversation-design: `option_cues` ({canonical_value: [regex, ...]}) is the TEXT twin of dtmf_map —
    # it deterministically routes an enum-ish user slot (e.g. journey_intent) from the caller's utterance
    # when it matches exactly one value, so op selection doesn't depend on the LLM. `validation_rules`
    # ([{kind, detail}]) is the migration's slot-rule form that the setter generator lowers into an enum
    # check; inert at runtime. Both are optional per-slot keys (absent → unchanged behavior).
    "option_cues", "validation_rules",
    # Conversation-design: `kind` tags a slot's semantic role. `kind:"intent"` marks a first-class INTENT
    # slot — a source:user slot whose value SELECTS one operation from an enum. It MUST carry option_cues +
    # an enum validation_rule and MUST NOT carry a numeric/length rule (checked in _check_intent_slots).
    # Inert at runtime (validation/clarity only).
    "kind",
    # Conversation-design: `passive` marks a user slot that is never ASKED (skipped by _find_next_question)
    # but whose setter stays visible so the model fills it from context — e.g. a model-classified router/
    # dispatch intent the assistant categorizes silently (like the synthesized cancel/escalate/intent_changed
    # slots, but authored). Bool; inert/absent on normal slots.
    "passive",
    # Repeated slots (Mode A): `repeated` is a DICT whose presence marks the slot
    # as collecting N scalars into a list. Its nested keys (until/ask_more/
    # min_count) are shape-checked in _check_repeated_slots — _check_unknown_keys
    # only diffs top-level keys, so only `repeated` itself is whitelisted here.
    "repeated",
    # A terminal announce inside an offer/help component flagged `end_conversation`
    # tears down the WHOLE conversation (not just its frame) — see the engine
    # _cascade_announce hook. Inert on a non-terminal announce.
    "end_conversation",
    # DETERMINISM: `readback_verbatim` preempts the FIRST presentation of this slot's
    # readback, so the engine's own text (with `readback_fmt` already applied) reaches
    # TTS instead of being relayed for the LLM to paraphrase into a different question.
    # Pair it with `readback_fmt` — preempting speaks the value as stored, so an
    # unformatted digit string would be read as one enormous number.
    "readback_verbatim",
    # DETERMINISM: `skip_readback_if_matches` ([slot, ...]) suppresses THIS slot's
    # readback when the staged value is digit-identical to one of the NAMED slots'
    # already-filled values — the caller confirmed those digits earlier in the session,
    # so confirming them again is the third telling of the same number. A value that
    # matches nothing listed still gets its readback. See the engine
    # _auto_promote_and_route.
    "skip_readback_if_matches",
    # `verbatim` pins this slot's recovery lines (its no-match reprompts and
    # validation exhaust) to the literal channel even when config["speech"] opts
    # their class into improvisation. Inert without a `speech` policy.
    "verbatim",
    # The model-facing tool description of this slot's generated setter (see
    # intent_slot/passive_slot `description=`), emitted onto pythonFunction.description.
    "tool_description",
})

_VALID_TASK_KEYS = frozenset({
    "name", "tool", "inputs", "outputs", "requires", "success_check",
    "condition", "terminal", "then_say", "then_directive",
    "then_response", "channel_then_response", "on_complete",
    # Polymorphic surfaces: per-surface wording of `then_say` (replaces it),
    # as distinct from `then_response` (which accompanies it).
    "then_say_variants", "channel_then_say_variants",
    "on_failure", "readback_inputs",
    # An integer slot the engine bumps each time this task fires, so a cap can be a
    # condition (`{"slot": ..., "gte": 3}`) instead of a callback summing latches.
    "count_into",
    # Component task (references a child DAG instead of a tool). `component` is the
    # child config_id (mutually exclusive with `tool`); `on_abort` is skip|fail_flow.
    # Shape/xor checks live in _check_component_tasks (S2); whitelisted here so a
    # component-bearing config does not trip the unknown-key check.
    "component", "on_abort",
    # Latency masking: `while_running` plays audio (hold music) while the task's
    # tool runs; `filler_say` is a spoken "one moment" filler (C1/C2).
    "while_running", "filler_say",
    # Repeated components (Mode B): a component task carrying `repeated` re-descends
    # its child DAG once per element. `collect` names the list-valued slot to fill;
    # `element` maps child slot -> element field. Nested keys of `repeated` are
    # shape-checked in _check_repeated_components. `collect`/`element` on a
    # non-component task are rejected there.
    "repeated", "collect", "element",
    # A terminal component task flagged `end_conversation` tears down the whole
    # conversation via _terminate instead of frame-returning to the parent.
    "end_conversation",
    # `awaits` marks a task whose tool is declared ASYNCHRONOUS: the call returns a
    # platform "pending" placeholder and the real payload lands a turn or more later as
    # a synthetic user turn. Presence makes the engine hold rather than read the
    # placeholder as a failure (engine `_is_async_pending` / `_async_hold`). Shape is
    # checked in _check_awaits.
    "awaits",
    # `preempt_then_say` speaks a NON-terminal task's then_say verbatim (model
    # bypassed) instead of folding it into the next question for the model to
    # re-render — which right after a backend action is exactly where it invents
    # outcomes it never performed. Terminal tasks already preempt unconditionally.
    "preempt_then_say",
    # `verbatim` pins this task's on_failure retry/exhaust lines literal even when
    # config["speech"] opts their class in. Inert without a `speech` policy.
    "verbatim",
    # `parallel` names the fan-out group this task is a leg of. Every eligible leg of one
    # group is dispatched in a SINGLE action, and the runtime executes them concurrently
    # (ces-probes 33: three four-second legs cost four seconds), so a group costs the
    # caller its slowest leg rather than the sum. It is a batching hint, not a barrier:
    # a leg gated off by its condition, already complete, or awaiting an async result is
    # simply not in that pass's set. Group shape is checked in _check_parallel_groups.
    "parallel",
    # Set by `parallel(progressive=False)`. Keeps the group on the batch shape --
    # synchronous legs, one action, collected on the same pass -- instead of lowering it
    # to asynchronous legs with a peek/watch pair. One reasoning pass instead of one per
    # watch window, at the cost of per-leg narration.
    "parallel_batch",
})

# Names the ENGINE writes into `filled` itself. `cancel`/`escalate` are the control
# slots; `<block>_declined` is the refusal counter, and it holds an int — a slot declared
# under the same name would be overwritten with one, so the collision is worse than a
# shadowed value.
_RESERVED_SLOT_NAMES = frozenset({
    "cancel", "escalate", "cancel_declined", "escalate_declined",
})

def _ask_floor(ask):
  """The first rung of an ask LADDER, or the ask itself when it is a plain string.

  `ask` may be a list: one wording per re-ask, so a question the caller does not
  answer is not repeated word for word. Checks that reason about "the question" want
  the wording the caller hears FIRST.
  """
  if isinstance(ask, (list, tuple)):
    for rung in ask:
      if isinstance(rung, str) and rung.strip():
        return rung
    return ""
  # A non-string ask is reported by _check_user_slot_fields; readers get "" so
  # a .strip()/.format() on the result cannot raise.
  return ask if isinstance(ask, str) else ""


_VALID_AWAITS_KEYS = frozenset(
    {"say", "while_waiting", "answer_first", "max_turns", "on_timeout",
     "verbatim"})

# The utterance classes config["speech"]["improvise"] may name. Mirrors
# _IMPROVISE_CLASSES in the slot_filling_engine (the two framework tools are
# separate sandboxed files and cannot import from each other).
_IMPROVISE_CLASSES = frozenset(
    {"reprompt", "no_input", "exhaust", "retry", "control", "await", "filler"})

_BOOL_SLOT_FIELDS = frozenset({
    "requires_readback", "preempt", "shared", "end_conversation", "passive",
    "readback_verbatim", "multi_fill", "verbatim",
})

_BOOL_TASK_FIELDS = frozenset({
    "terminal", "readback_inputs", "end_conversation", "preempt_then_say",
})

# Fields whose value is an IDENTIFIER the engine keys a registry by (a setter
# name, a tool name, an event key, a child config id). A non-string one cannot
# be looked up — and, being unhashable when it is a list/dict from YAML, cannot
# even be put in the maps the checks build. Reported once by
# _check_identifier_fields; every reader guards with isinstance(..., str).
_ID_SLOT_FIELDS = ("setter", "setter_field", "event_key", "kind")
_ID_TASK_FIELDS = ("tool", "component", "setter", "collect", "count_into")

_VALID_READBACK_FMT_TYPES = frozenset({
    "prefix", "plural", "none_sub", "date", "time",
    # List-aware formatters for repeated slots: `join` renders each element via an
    # `each` template joined by `sep`; `count` renders a len()-based plural.
    "join", "count",
    # `digits` speaks a digit string one digit at a time ("2 1 2 …"), with an
    # optional `text` label. Required by any `readback_verbatim` slot holding a
    # phone/ZIP/SSN: preempted text goes straight to TTS, where an unspaced
    # "2124561234" is read as one enormous number.
    "digits",
})

_READBACK_FMT_REQUIRED_FIELDS = {
    "prefix": ["text"],
    "plural": ["one", "other"],
    "none_sub": ["default"],
    # `join` needs an `each` element template (`sep` is optional, defaults ", ").
    # `count` needs singular/plural forms like `plural`.
    "join": ["each"],
    "count": ["one", "other"],
}

# The OPTIONAL params each formatter reads, mirroring `_compile_formatter` in the
# engine. Required + optional + "type" is the complete allowed key set: the compiler
# reads named keys and ignores everything else, so a param that is misspelled, dropped
# in a hand-edit, or invented is silently discarded and the slot reads back in a
# DIFFERENT voice than the author wrote — `{"type": "date", "text": …}` losing its
# text, or a `prefix` losing its `values` map and speaking the raw slot value. That
# failure has no symptom until a live call.
_READBACK_FMT_OPTIONAL_FIELDS = {
    "prefix": ["values"],
    "date": ["text"],
    "digits": ["text"],
    "join": ["sep"],
    # plural / count / none_sub / time read nothing beyond their required fields.
}

# Repeated-slot nested-key whitelists (Mode A + Mode B share the same `repeated`
# dict shape). _check_unknown_keys only diffs TOP-LEVEL slot/task keys, so a typo
# nested inside `repeated`/`until` (e.g. `untill`, `max_cont`) is otherwise
# invisible — _check_repeated_shape errors on unknown inner keys.
# `over` (a parent LIST slot to iterate) + `each` ({child_slot: element_field}) drive Mode-B per-element
# INPUT binding: each child invocation is seeded with `over[iteration][field]` and the loop ends when the
# list is exhausted. Inert on a plain collect-loop (absent → unchanged).
_VALID_REPEATED_KEYS = frozenset({"until", "ask_more", "min_count", "over", "each"})
_VALID_REPEATED_UNTIL_KEYS = frozenset({"max_count", "done_setter"})

_VALID_RESPONSE_TYPES = frozenset({
    "text", "payload", "end_session", "transfer",
    # Conversation-design parts: `chips` (interactive options) and `audio`
    # (chime / brand audio / pre-recorded prompt / hold music).
    "chips", "audio",
})

# ── Telephony hand-off payloads ───────────────────────────────────
#
# A contact-center platform puts a caller in front of a human on a structured
# `payload` part, never on anything the agent SAYS — and that part is only half of the
# hand-off. The other half is the `end_session` that gives the leg up. Emit the payload
# alone and the platform is told to escalate while the agent keeps the call: the caller
# hears "connecting you now" and then waits for someone who never arrives. Emit the
# end alone (the generic escalate rail's old behavior) and the call simply drops. Both
# have shipped, which is why an unpaired hand-off payload is an ERROR rather than a
# warning.
#
# A hand-off payload is recognized STRUCTURALLY, by the vendor key inside its `data`.
# Nothing marks one on the wire and nothing should: these bytes are a live integration
# contract, and a marker key would change them. The cost is that this table has a TWIN
# in `flows.authoring.handoff` (a CES tool cannot import that module), and a vendor is
# only checked where it appears in both.
#
# `_HANDOFF_VENDORS` is one object rather than four loose constants precisely so the
# twins can be compared wholesale: `packages/flows/tests/test_handoff_vendor_sync.py`
# is a DRIFT GATE that fails when the two registries disagree on the vendor keys or on
# a vendor's required fields — the same job `test_framework_runtime_sync.py` does for
# the byte-synced framework copies. It cannot remove the duplication, but it makes the
# divergence impossible to miss.
_UJET_KEY = "ujet"
_DIALOGFLOW_KEY = "transferToDialogflow"
# CX Agent Studio — a transfer to another CES app. The wire directive is still
# `transferToNga` (the platform's older name for a CXAS app); the SDK builder is cxas().
_CXAS_KEY = "transferToNga"
_UJET_ESCALATION_ACTION = "escalation"
# vendor discriminator -> (label, required fields inside the vendor object).
# Dialogflow CX's and CXAS's payloads are the target STRING, so they have no inner fields.
_HANDOFF_VENDORS = {
    _UJET_KEY: ("UJET", ("menu_id", "action", "escalation_reason")),
    _DIALOGFLOW_KEY: ("Dialogflow CX", ()),
    _CXAS_KEY: ("CX Agent Studio", ()),
}
# The `end_session` reason a hand-off ends on: the call did not COMPLETE, it left for
# another system, and every containment report reads this field.
_HANDOFF_REASON = "transfer"


def _handoff_shape(data):
  """Identify a vendor hand-off payload -> (label, is_escalation, missing_fields).

  None for every other payload — a rich-content card, chips, an app's own structured
  data — so the pairing rules below can never fire on content that is merely
  displayed alongside what the agent said.
  """
  if not isinstance(data, dict):
    return None
  ujet = data.get(_UJET_KEY)
  if isinstance(ujet, dict):
    label, required = _HANDOFF_VENDORS[_UJET_KEY]
    missing = sorted(
        k for k in required
        if not str(ujet.get(k, "") or "").strip())
    action = str(ujet.get("action", "") or "").strip()
    return (label, action == _UJET_ESCALATION_ACTION, missing)
  target = data.get(_DIALOGFLOW_KEY)
  if isinstance(target, str) and target.strip():
    # A transfer to another automated platform, not a person: nobody was escalated.
    return (_HANDOFF_VENDORS[_DIALOGFLOW_KEY][0], False, [])
  cxas_target = data.get(_CXAS_KEY)
  if isinstance(cxas_target, str) and cxas_target.strip():
    # A transfer to another CES app, not a person: nobody was escalated, same as DFCX.
    return (_HANDOFF_VENDORS[_CXAS_KEY][0], False, [])
  return None


def _condition_reads_payloads(spec):
  """Whether a condition reads the `payloads` capability anywhere in its tree."""
  if isinstance(spec, dict):
    if spec.get("capability") == "payloads":
      return True
    return any(_condition_reads_payloads(v) for v in spec.values())
  if isinstance(spec, (list, tuple)):
    return any(_condition_reads_payloads(v) for v in spec)
  return False


_SAFE_EVAL_GLOBALS = {  # pylint: disable=unused-variable
    "__builtins__": {
        "int": int, "str": str, "len": len,
        "float": float, "bool": bool,
    },
}

# ── Declarative condition validation ──────────────────────────────
# Canonical source: evaluate_conditions tool. Copied here because
# CES tools cannot call each other.

_VALID_LEAF_KEYS = frozenset({
    "slot", "capability", "surface", "eq", "neq", "in", "not_in", "filled", "since_turns",
    "gte", "lte", "gt", "lt", "upper", "default",
})
# Capabilities a `{"capability": ...}` leaf may name; mirrors the engine's
# _BUILTIN_SURFACES capability set.
_VALID_CAPABILITIES = frozenset({
    "payloads", "brevity", "links", "filler", "keypad", "max_options",
})
_VALID_COMBINATOR_KEYS = frozenset({"all", "any", "not"})
_COMPARISON_OPS = frozenset({"gte", "lte", "gt", "lt"})
_VALUE_OPS = frozenset({"eq", "neq", "in", "not_in", "filled", "since_turns"})
_ALL_OPS = _COMPARISON_OPS | _VALUE_OPS


# ── Malformed-input degradation ───────────────────────────────────
# A linter that CRASHES on bad authoring input is worse than one that reports
# the error: the author gets a stack trace instead of a diagnostic, and every
# OTHER defect in the config goes unreported. So each shape below is reported
# ONCE by the check that owns the field, and every other reader degrades to
# "nothing to look at" through these helpers instead of raising.


def _is_hashable(value) -> bool:
  """True if `value` can be a set member / dict key.

  Condition values arrive straight from author YAML/JSON, where a list or dict
  is a legal literal but is unhashable in Python. The contradiction checker
  hashes eq/neq/in values, so every such value is screened through here first.
  """
  try:
    hash(value)
  except TypeError:
    return False
  return True


def _hashable_members(value):
  """`value` when it is a list whose members can all be set members, else None.

  in/not_in operands are set-differenced against each other; a non-list operand
  or an unhashable member is REPORTED by _validate_condition_recursive, so the
  set-reasoning passes skip that leaf ("None" == nothing to compare) instead of
  raising.
  """
  if not isinstance(value, list):
    return None
  return value if all(_is_hashable(v) for v in value) else None


def _sub_conditions(value):
  """The sub-conditions of an all/any combinator, or [] if malformed.

  _validate_condition_recursive REPORTS a non-list combinator as an error; the
  extraction/contradiction walkers must agree with it by degrading to "nothing
  to walk" instead of raising on a non-iterable.
  """
  if isinstance(value, (list, tuple)):
    return value
  return []


def _as_seq(value) -> list:
  """`value` as a list, or [] when it is not a sequence.

  A bare string is NOT a sequence here: iterating `requires: "res"` per
  character is how one bad field became three bogus "unknown slot" errors.
  """
  if isinstance(value, (list, tuple)):
    return list(value)
  return []


def _as_map(value) -> dict:
  """`value` as a dict, or {} when it is not one."""
  return value if isinstance(value, dict) else {}


def _config_entries(config, field: str) -> list[dict[str, Any]]:
  """The `slots`/`tasks` entries of a RAW config, malformed ones dropped.

  DagConfigValidator normalizes these once in __init__; the cross-config checks
  see the raw dicts straight from the caller, so they degrade the same way here
  — the per-config pass is what reports the shape.
  """
  if not isinstance(config, dict):
    return []
  return [e for e in _as_seq(config.get(field)) if isinstance(e, dict)]


def _entry_name(entry) -> str:
  """A slot/task's `name` as a usable string, "<unnamed>" when it is neither.

  Readers key their maps by this AND print it, so it must always be a hashable
  string; _check_slot_names/_check_task_names own the report for a missing or
  non-string name.
  """
  name = _as_map(entry).get("name")
  return name if isinstance(name, str) else "<unnamed>"


def _name_list(value) -> list[str]:
  """Slot names read from a `requires`/`inputs`/`clear_slots` value.

  Read-only consumers use this so a malformed value (reported by the check that
  OWNS the field) cannot raise on iteration or on an `in self._slot_set` lookup,
  and cannot be iterated per character.
  """
  if isinstance(value, dict):
    value = list(value.keys())
  return [n for n in _as_seq(value) if isinstance(n, str)]


def _task_outputs(task) -> dict[str, Any]:
  """A task's `outputs` map ({result_key: slot_name}), or {} if malformed.

  The type error is reported ONCE, by _check_duplicate_output_targets; every
  other reader degrades to "no outputs" so a list-shaped `outputs` cannot raise
  .items()/.values() from deep inside an unrelated check.
  """
  return _as_map(_as_map(task).get("outputs"))


def _validate_condition_recursive(spec, slot_set, context, errors):
  """Recursively validate a declarative condition spec."""
  if not isinstance(spec, dict):
    errors.append(
        f"{context}: condition must be a dict,"
        f" got {type(spec).__name__}")
    return

  keys = set(spec.keys())

  if "all" in keys:
    if keys - {"all"}:
      errors.append(
          f"{context}: 'all' combinator has extra keys:"
          f" {sorted(keys - {'all'})}")
    items = spec["all"]
    if not isinstance(items, list):
      errors.append(f"{context}: 'all' must be a list")
      return
    if len(items) < 2:
      errors.append(
          f"{context}: 'all' must have at least 2 sub-conditions")
    for i, sub in enumerate(items):
      _validate_condition_recursive(
          sub, slot_set, f"{context}.all[{i}]", errors)
    return

  if "any" in keys:
    if keys - {"any"}:
      errors.append(
          f"{context}: 'any' combinator has extra keys:"
          f" {sorted(keys - {'any'})}")
    items = spec["any"]
    if not isinstance(items, list):
      errors.append(f"{context}: 'any' must be a list")
      return
    if len(items) < 2:
      errors.append(
          f"{context}: 'any' must have at least 2 sub-conditions")
    for i, sub in enumerate(items):
      _validate_condition_recursive(
          sub, slot_set, f"{context}.any[{i}]", errors)
    return

  if "not" in keys:
    if keys - {"not"}:
      errors.append(
          f"{context}: 'not' combinator has extra keys:"
          f" {sorted(keys - {'not'})}")
    _validate_condition_recursive(
        spec["not"], slot_set, f"{context}.not", errors)
    return

  # Surface-aware leaves read the delivery surface rather than a slot value, and
  # their operator is OPTIONAL: {"capability": "payloads"} is a truthiness test and
  # {"surface": "voice"} an equality test against the surface name.
  if "capability" in keys or "surface" in keys:
    if "capability" in keys and "surface" in keys:
      errors.append(f"{context}: leaf reads 'capability' or 'surface', not both")
      return
    source = "capability" if "capability" in keys else "surface"
    if "slot" in keys:
      errors.append(f"{context}: leaf reads '{source}' or 'slot', not both")
      return
    if not isinstance(spec[source], str):
      errors.append(
          f"{context}: '{source}' must be a string,"
          f" got {type(spec[source]).__name__}")
      return
    if source == "capability" and spec[source] not in _VALID_CAPABILITIES:
      errors.append(
          f"{context}: unknown capability '{spec[source]}';"
          f" valid: {sorted(_VALID_CAPABILITIES)}")
    unknown = keys - _VALID_LEAF_KEYS
    if unknown:
      errors.append(f"{context}: unknown condition keys: {sorted(unknown)}")
    ops_present = keys & _ALL_OPS
    if len(ops_present) > 1:
      errors.append(
          f"{context}: leaf condition has multiple operators:"
          f" {sorted(ops_present)}")
    return

  if "slot" not in keys:
    errors.append(
        f"{context}: leaf condition missing 'slot' key"
        " (or 'capability'/'surface')")
    return

  slot_name = spec["slot"]
  if not isinstance(slot_name, str):
    errors.append(
        f"{context}: 'slot' must be a string,"
        f" got {type(slot_name).__name__}")
    return

  # The engine synthesizes a `<block>_declined` counter each time a control block's
  # condition refuses the request, so a gate may reference one even though no slot
  # declares it. Without this every "contain once" gate is a validation error.
  if slot_name in ("cancel_declined", "escalate_declined"):
    return
  if slot_set and slot_name not in slot_set:
    errors.append(
        f"{context}: condition references unknown slot"
        f" '{slot_name}'")

  unknown = keys - _VALID_LEAF_KEYS
  if unknown:
    errors.append(
        f"{context}: unknown condition keys: {sorted(unknown)}")

  ops_present = keys & _ALL_OPS
  if len(ops_present) == 0:
    errors.append(f"{context}: leaf condition has no operator")
    return
  if len(ops_present) > 1:
    errors.append(
        f"{context}: leaf condition has multiple operators:"
        f" {sorted(ops_present)}")
    return

  op = next(iter(ops_present))

  if "upper" in keys:
    if not isinstance(spec["upper"], bool):
      errors.append(
          f"{context}: 'upper' must be bool,"
          f" got {type(spec['upper']).__name__}")
    if op in _COMPARISON_OPS or op == "filled":
      errors.append(
          f"{context}: 'upper' not applicable to '{op}' operator")

  if op == "filled":
    if not isinstance(spec["filled"], bool):
      errors.append(
          f"{context}: 'filled' must be bool,"
          f" got {type(spec['filled']).__name__}")
  elif op in ("in", "not_in"):
    val = spec[op]
    if not isinstance(val, list):
      errors.append(
          f"{context}: '{op}' must be a list,"
          f" got {type(val).__name__}")
    else:
      for member in val:
        if not _is_hashable(member):
          errors.append(
              f"{context}: '{op}' member must be a scalar,"
              f" got {type(member).__name__}")
          break
  elif op in ("eq", "neq"):
    # A container operand can only ever compare unequal to a slot's scalar
    # value, so the leaf is dead however it is read — and it is unhashable,
    # which the contradiction/tautology passes would otherwise choke on.
    if not _is_hashable(spec[op]):
      errors.append(
          f"{context}: '{op}' must be a scalar,"
          f" got {type(spec[op]).__name__}")
  elif op in _COMPARISON_OPS:
    val = spec[op]
    if not isinstance(val, (int, float)):
      errors.append(
          f"{context}: '{op}' must be int or float,"
          f" got {type(val).__name__}")
    if "default" in keys:
      default = spec["default"]
      if not isinstance(default, (int, float)):
        errors.append(
            f"{context}: 'default' for '{op}' must be numeric,"
            f" got {type(default).__name__}")


def _validate_condition_spec(spec, slot_set, context="condition"):
  """Validate a declarative condition spec. Returns list of errors."""
  errors = []
  _validate_condition_recursive(spec, slot_set, context, errors)
  return errors


def _extract_condition_slots(spec):
  """Extract all slot names referenced in a declarative condition.

  Args:
    spec: Condition specification dict.

  Returns:
    Set of slot name strings.
  """
  if not isinstance(spec, dict):
    return set()
  slots = set()
  if "all" in spec:
    for sub in _sub_conditions(spec.get("all")):
      slots |= _extract_condition_slots(sub)
  elif "any" in spec:
    for sub in _sub_conditions(spec.get("any")):
      slots |= _extract_condition_slots(sub)
  elif "not" in spec:
    slots |= _extract_condition_slots(spec["not"])
  elif "slot" in spec:
    if isinstance(spec["slot"], str):
      slots.add(spec["slot"])
  return slots


# Declarative leaf ops that read the slot VALUE (not just its truthiness). On a
# list-valued (repeated/collect) slot every one of these misbehaves: gte/lte/gt/lt
# do int(list) -> TypeError (swallowed fail-open -> gate never gates); eq/neq
# compare against the whole list container; in/not_in test the list as a single
# member. All are author errors — a `filled:` truthiness check is the only
# declarative form that is list-safe. Use a len()-based lambda-string instead.
_LIST_UNSAFE_CONDITION_OPS = _COMPARISON_OPS | frozenset(
    {"eq", "neq", "in", "not_in"}
)


def _value_op_condition_slots(spec):
  """Slots read by a value op (gte/lte/gt/lt/eq/neq/in/not_in) in a declarative
  condition — the ops that misbehave on a list-valued slot. Used to reject that
  shape at author time (§R2.0/§7). A `filled:` truthiness leaf is list-safe and
  intentionally excluded."""
  if not isinstance(spec, dict):
    return set()
  slots = set()
  if "all" in spec:
    for sub in _sub_conditions(spec.get("all")):
      slots |= _value_op_condition_slots(sub)
  elif "any" in spec:
    for sub in _sub_conditions(spec.get("any")):
      slots |= _value_op_condition_slots(sub)
  elif "not" in spec:
    slots |= _value_op_condition_slots(spec["not"])
  elif "slot" in spec and (set(spec.keys()) & _LIST_UNSAFE_CONDITION_OPS):
    if isinstance(spec["slot"], str):
      slots.add(spec["slot"])
  return slots


def _repeat_affordance_ok(until):
  """A repeated/until dict declares a real termination affordance: a max_count key
  OR a NON-EMPTY done_setter string. An empty-string done_setter is falsy at
  runtime (_repeat_done gates on `until.get('done_setter') and done_signal`) so it
  never terminates — treat it as absent here (§R2.0)."""
  if not isinstance(until, dict):
    return False
  done_setter = until.get("done_setter")
  return "max_count" in until or (
      isinstance(done_setter, str) and done_setter.strip() != "")


def _extract_positive_condition_slots(spec):
  """Extract slots that expect a truthy/specific value.

  Excludes slots referenced only via filled:False or with a default
  key, since those don't require the slot to be filled first.

  Args:
    spec: Condition specification dict.

  Returns:
    Set of slot name strings requiring truthy values.
  """
  if not isinstance(spec, dict):
    return set()
  slots = set()
  if "all" in spec:
    for sub in _sub_conditions(spec.get("all")):
      slots |= _extract_positive_condition_slots(sub)
  elif "any" in spec:
    for sub in _sub_conditions(spec.get("any")):
      slots |= _extract_positive_condition_slots(sub)
  elif "not" in spec:
    slots |= _extract_positive_condition_slots(spec["not"])
  elif "slot" in spec:
    if spec.get("filled") is not None and not spec["filled"]:
      return slots
    if "default" in spec:
      return slots
    if isinstance(spec["slot"], str):
      slots.add(spec["slot"])
  return slots


def _find_contradictions(spec):
  """Find always-false patterns in 'all' combinators.

  Args:
    spec: Condition specification dict.

  Returns:
    List of error description strings.
  """
  if not isinstance(spec, dict):
    return []
  errors = []
  if "all" in spec:
    leaves = {}
    for sub in _sub_conditions(spec["all"]):
      if isinstance(sub, dict) and isinstance(sub.get("slot"), str):
        leaves.setdefault(sub["slot"], []).append(sub)
      errors.extend(_find_contradictions(sub))
    for slot_name, group in leaves.items():
      # Operands are screened for hashability (and in/not_in for list-ness):
      # they come straight from author YAML, where a container is a legal
      # literal. _validate_condition_recursive REPORTS those, so this pass has
      # nothing left to say about them — and must not hash them.
      eqs = [s["eq"] for s in group if "eq" in s and _is_hashable(s["eq"])]
      if len(eqs) >= 2 and len(set(eqs)) > 1:
        errors.append(
            f"Contradictory: slot '{slot_name}' cannot equal"
            f" both {eqs[0]!r} and {eqs[1]!r}")
      neqs = {s["neq"] for s in group
              if "neq" in s and _is_hashable(s["neq"])}
      for eq_val in eqs:
        if eq_val in neqs:
          errors.append(
              f"Contradictory: slot '{slot_name}' eq={eq_val!r}"
              f" and neq={eq_val!r}")
      filleds = [s["filled"] for s in group if "filled" in s]
      if True in filleds and False in filleds:
        errors.append(
            f"Contradictory: slot '{slot_name}' filled=True"
            f" and filled=False")
      in_sets = [s for s in group if _hashable_members(s.get("in")) is not None]
      not_in_sets = [s for s in group
                     if _hashable_members(s.get("not_in")) is not None]
      for in_spec in in_sets:
        for ni_spec in not_in_sets:
          remaining = set(in_spec["in"]) - set(ni_spec["not_in"])
          if not remaining:
            errors.append(
                f"Contradictory: slot '{slot_name}' in-set"
                f" fully excluded by not_in")
      # NEW: an eq value excluded by a sibling in/not_in on the same slot in
      # this 'all' — the required value is unreachable, so the branch is dead.
      for eq_val in eqs:
        if any(eq_val not in s["in"] for s in in_sets):
          errors.append(
              f"Contradictory: slot '{slot_name}' eq={eq_val!r} is not in"
              f" its 'in' set")
        if any(eq_val in s["not_in"] for s in not_in_sets):
          errors.append(
              f"Contradictory: slot '{slot_name}' eq={eq_val!r} is excluded"
              f" by 'not_in'")
      # NEW: numeric-range impossibility — the strongest lower bound (gt/gte)
      # exceeds the strongest upper bound (lt/lte), so no value satisfies both.
      lowers = [(s["gt"], False) for s in group
                if isinstance(s.get("gt"), (int, float))
                and not isinstance(s.get("gt"), bool)]
      lowers += [(s["gte"], True) for s in group
                 if isinstance(s.get("gte"), (int, float))
                 and not isinstance(s.get("gte"), bool)]
      uppers = [(s["lt"], False) for s in group
                if isinstance(s.get("lt"), (int, float))
                and not isinstance(s.get("lt"), bool)]
      uppers += [(s["lte"], True) for s in group
                 if isinstance(s.get("lte"), (int, float))
                 and not isinstance(s.get("lte"), bool)]
      if lowers and uppers:
        lo_val, lo_incl = max(lowers, key=lambda p: p[0])
        hi_val, hi_incl = min(uppers, key=lambda p: p[0])
        if lo_val > hi_val or (lo_val == hi_val and not (lo_incl and hi_incl)):
          errors.append(
              f"Contradictory: slot '{slot_name}' numeric range is empty"
              f" (lower bound {lo_val} vs upper bound {hi_val})")
  elif "any" in spec:
    for sub in _sub_conditions(spec["any"]):
      errors.extend(_find_contradictions(sub))
  elif "not" in spec:
    errors.extend(_find_contradictions(spec["not"]))
  return errors


def _is_component(task: dict[str, Any]) -> bool:
  """A task is a Component iff it carries a 'component' key (a child config_id).

  The presence of the key IS the marker; it is mutually exclusive with 'tool'.
  """
  return "component" in task


def _task_input_slots(inputs) -> list[str]:
  """Return the parent slot names a task's `inputs` reads.

  inputs is the same shape as a tool task's: a dict {slot: param} (keys are the
  slot names) or a bare list[str] of slot names. Mirrors the engine helper of
  the same name (§4.3) so a component's inputs resolve to parent slot names.
  A malformed value yields no names — _check_task_references owns that report.
  """
  return _name_list(inputs)


def _condition_leaves(spec):
  """Every leaf dict in a declarative condition tree (all / any / not / leaf)."""
  if not isinstance(spec, dict):
    return
  for comb in ("all", "any"):
    if comb in spec:
      for sub in _sub_conditions(spec[comb]):
        yield from _condition_leaves(sub)
      return
  if "not" in spec:
    yield from _condition_leaves(spec["not"])
    return
  if "slot" in spec:
    yield spec


def _accepts_value(leaf, value) -> bool:
  """Does this leaf accept `value` for its slot?

  Only the value-COMPARING operators can rule a value out. `filled`, and the numeric
  and case operators, either test presence or do not constrain the value set in a way
  a literal default can be checked against — so they accept, and the caller stays
  quiet rather than guessing.
  """
  if "eq" in leaf:
    return leaf["eq"] == value
  if "in" in leaf:
    return value in (leaf["in"] or [])
  if "neq" in leaf:
    return leaf["neq"] != value
  if "not_in" in leaf:
    return value not in (leaf["not_in"] or [])
  return True


def _output_targets(outputs) -> list:
  """Every slot an `outputs` map writes to.

  A result key may name ONE slot or SEVERAL — one backend field routinely lands under
  two slot names — so anything reasoning about "which slots does this task fill" has
  to flatten rather than read the values directly.
  """
  out = []
  for value in _as_map(outputs).values():
    for target in (value if isinstance(value, list) else [value]):
      # A non-string target is reported by _check_duplicate_output_targets; it
      # can never name a slot, and would not be hashable into the slot sets the
      # callers test it against.
      if isinstance(target, str):
        out.append(target)
  return out


def _normalize_sources(source) -> list[str]:
  """Normalize slot source to a list of source STRINGS.

  A non-string source (or member) is reported by _check_slot_sources, which
  reads the raw value; every other reader gets only the names it can act on.
  """
  if isinstance(source, (list, tuple)):
    return [s for s in source if isinstance(s, str)]
  if isinstance(source, str):
    return [source]
  return [] if source else ["user"]


def _extract_format_fields(template: str) -> set[str]:
  """Extract {field_name} placeholders from a format string.

  A placeholder carrying an inline fallback (`{app_name|that app}`) is DROPPED: it always
  renders, so an unknown name there is not a defect. Only the bare form can leak braces
  at the caller, and only that form is worth warning about.
  """
  try:
    return {
        fname for _, fname, _, _
        in string.Formatter().parse(template)
        if fname is not None and "|" not in fname
    }
  except (ValueError, KeyError):
    return set()


def _extract_dict_keys_from_source(source: str) -> set[str] | None:
  """Extract string keys from dict literals and subscript assignments.

  Parses Python source and collects keys from:
    - Dict literals: {"key": ...}
    - Subscript assignments: result["key"] = ...
    - values["key"] = ... (nested dict builds)

  Args:
    source: Python source code to parse.

  Returns:
    Tuple of (all_keys, nested_keys) or None if parsing fails. A name whose
    contents cannot be read off statically is recorded in nested_keys as None
    (see `_extract_values_dict_keys`, which treats that as "undetermined").
  """
  try:
    tree = ast.parse(source)
  except (SyntaxError, ValueError, TypeError):
    # TypeError: a non-str source; ValueError: embedded NULs. Both mean "this
    # source cannot be read", which is the same answer as a syntax error.
    return None
  keys: set[str] = set()
  # name -> the constant string keys written into it, or None once the name is
  # OPAQUE (built with a computed key, or rebound to something this reader cannot
  # see into). An opaque name must not be reported as a complete key set: the
  # caller asserts on absence, so a partial read is a false accusation.
  nested_keys: dict[str, set[str] | None] = {}

  def _mark(var_name: str, key: str | None) -> None:
    """Record one constant key for `var_name`, or mark the whole name opaque."""
    if key is None:
      nested_keys[var_name] = None
      return
    current = nested_keys.get(var_name, set())
    if current is None:
      return  # already opaque; a later constant key cannot make it complete
    current.add(key)
    nested_keys[var_name] = current

  for node in ast.walk(tree):
    if isinstance(node, ast.Dict):
      for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
          keys.add(k.value)
    if isinstance(node, ast.Assign):
      for target in node.targets:
        if isinstance(target, ast.Subscript):
          if not isinstance(target.value, ast.Name):
            continue
          if (isinstance(target.slice, ast.Constant)
              and isinstance(target.slice.value, str)):
            keys.add(target.slice.value)
            _mark(target.value.id, target.slice.value)
          else:
            # `values[field] = v` — the key is computed, so the written set is
            # unknowable here. Skipping it silently and then asserting on what
            # remains is how a per-field loop (set_kba_answer) gets accused of
            # never writing any of its fields.
            _mark(target.value.id, None)
        # A dict LITERAL bound to a name — `values = {"value": key}` — is the other
        # half of the multi-setter idiom, and the one the source app's hand-written
        # setters use. It was documented above but never read, so a setter that
        # built its payload in one expression instead of field-by-field subscripts
        # was reported as writing none of its fields.
        elif isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
          for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
              _mark(target.id, k.value)
            else:
              _mark(target.id, None)

  return keys, nested_keys


def _extract_values_dict_keys(source: str) -> set[str] | None:
  """Extract keys written into a 'values' dict in setter source.

  For multi-setters that build: values = {}; values["field"] = x
  or values = {"field": x}.

  Args:
    source: Python source code of the setter function.

  Returns:
    Set of keys from the values dict, or None if undetermined.
  """
  result = _extract_dict_keys_from_source(source)
  if result is None:
    return None
  _, nested = result
  if "values" in nested:
    return nested["values"]
  return None


def _response_text_placeholders(response) -> set[str]:
  """{field} placeholders in the text of every text-type part of a response list.

  A response part's strings are formatted with the filled slots at render time
  (task then_response via _resolve_response -> _substitute_response; slot response
  the same way). A KeyError there falls back to the RAW string
  (_substitute_response), so an unknown {placeholder} surfaces to the caller
  verbatim (never substituted) — the same author error _check_format_string_
  placeholders already flags on ask/message/then_say. Reaches into the list's
  text parts and defers field extraction to _extract_format_fields. Non-list
  input and non-text parts contribute nothing.
  """
  fields: set[str] = set()
  if not isinstance(response, list):
    return fields
  for part in response:
    if not isinstance(part, dict) or part.get("type") != "text":
      continue
    text = part.get("text")
    if isinstance(text, str) and text:
      fields |= _extract_format_fields(text)
  return fields


# Nested-block key whitelists. _check_unknown_keys only diffs TOP-LEVEL config/
# slot/task keys, so a typo NESTED inside these blocks (e.g. `reset_on_complte`,
# `hold_repromts`, `error_respones`) is otherwise invisible — the engine reads
# each field via .get() with a default and silently drops a mis-keyed one, so the
# intended behavior (a bounded retry ladder, a hold-silence prompt, a channel
# override) never activates. Each set is the EXACT union of keys the engine reads
# (grepped from slot_filling_engine / slot_intake) PLUS the fields the Pydantic
# loader (config/models.py) declares for that block. Pydantic uses extra="allow",
# so these frozensets are intentionally stricter than the loader (same policy as
# the existing top-level _VALID_*_KEYS) to catch author typos.
#
# bootstrap: slot/welcome_slot/tool/reset_on_complete (slot_intake + engine),
# pass_through_on_transfer (slot_intake ~498/508), auto_seed (engine ~5800),
# intent_first (engine ~5667); flow_types is a design-time transfer-map field
# declared on the Bootstrap model (models.py ~387). Omitting intent_first/
# flow_types would false-positive live gated/router configs.
_VALID_BOOTSTRAP_KEYS = frozenset({
    "slot", "welcome_slot", "tool", "reset_on_complete", "auto_seed",
    "pass_through_on_transfer", "flow_types", "intent_first",
    # Home-base flow: seeded into the gate by before_agent on a cold turn so a SILENT flow activates on
    # the turn carrying the user's utterance (engine reads it into sm._default_flow ~5776).
    "default_flow",
})

# steer_back: soft_after/hard_after/escalate_after/on_exhaust are read by
# _handle_steer_back (engine ~1721-1753); steer_back_directive is declared on the
# SteerBack model (models.py ~401). cancel_tool/cancel_say are deliberately NOT
# whitelisted — they have a dedicated "not supported" diagnostic in
# _check_steer_back, and the unknown-key diff subtracts them so they are not
# double-reported. (There are no bare `soft`/`hard` keys in the engine.)
_VALID_STEER_BACK_KEYS = frozenset({
    "soft_after", "hard_after", "escalate_after", "on_exhaust",
    "steer_back_directive",
})

# no_input: reprompts/hold_reprompts/hold_phrases/on_exhaust (engine ~4057 hold
# match, ~4301 reprompt ladder, ~4317 exhaust).
_VALID_NO_INPUT_KEYS = frozenset({
    "reprompts", "hold_reprompts", "hold_phrases", "on_exhaust", "verbatim",
    # hold_ack: spoken IN PLACE OF the pending ask on the turn a hold phrase is
    # heard, so the caller who asked for a moment is not immediately re-asked.
    "hold_ack",
    # hold_vetoes: what disqualifies an utterance that carries a hold phrase.
    "hold_vetoes",
})

# answer: a flow-level free-response policy — a LIST of grounded, intent-scoped
# fallback configs (name/scope/instruction + a grounds/tools grounding surface,
# max_turns budget, optional allow_math/condition/requires), consumed by the
# engine as `answer_directive` at the steer-back call site. The turn is
# NON-ADVANCING (fills no slot), so the slot-fill keys below are rejected with a
# dedicated "non-advancing" diagnostic and subtracted from the unknown-key diff —
# the same shape steer_back uses for its unsupported cancel_tool/cancel_say.
_VALID_ANSWER_KEYS = frozenset({
    "name", "scope", "instruction", "max_turns", "allow_math",
    "grounds", "tools", "condition", "requires",
})
# Slot-fill / DAG-advancing keys an answer entry must NOT carry: filling a slot
# on the answer turn would move the DAG the non-advancing turn is defined never
# to touch, so the next cue-match would no longer route to the rails.
_ANSWER_SLOT_FILL_KEYS = frozenset({
    "setter", "setter_field", "outputs", "fill", "collect", "element",
})
# Lint nudge (WARN, not error): a whitelisted `tools` entry whose NAME matches a
# mutating action is almost certainly a COMMIT tool, which belongs on the
# deterministic DAG cues (matched first) rather than on the answer turn's read/
# compute surface. Kept a warning so a legitimately-named read tool is not
# blocked — the safety invariant is the author keeping commit tools off the list.
_ANSWER_MUTATING_TOOL_RE = re.compile(
    r"(?i)(transfer|submit|enroll|change|waive|hangup|cancel|\bpay\b)")

# validation: max_retries/on_exhaust/reprompts/errors/error_responses/
# channel_error_responses (engine ~2616-2660; channel_error_responses is the
# channel override paired with error_responses).
_VALID_VALIDATION_KEYS = frozenset({
    "max_retries", "errors", "error_responses", "channel_error_responses",
    "on_exhaust", "reprompts",
})

# push_back: reprompts/max/say/then/fill/end_conversation/verbatim — all read by the
# engine's _push_back_tick (reprompts+max drive the re-offer count; say/then/fill/
# end_conversation are the on_exhaust disposition; verbatim pins the re-offer literal).
_VALID_PUSH_BACK_KEYS = frozenset({
    "reprompts", "max", "say", "then", "fill", "end_conversation", "verbatim",
})

# on_failure: clear_slots/max_retries/on_exhaust/retry_say (engine ~2127-2187)
# and retry_response/channel_retry_response (resolved via _resolve_response,
# engine ~2193 / ~860). All six are declared on the OnFailure model (models.py
# ~315). channel_retry_response is the channel override for retry_response.
_VALID_ON_FAILURE_KEYS = frozenset({
    "clear_slots", "max_retries", "retry_say", "retry_response",
    "channel_retry_response", "on_exhaust", "verbatim",
    # `then` lets a RETRY fire a tool, exactly as on_exhaust already can (same
    # `_resolve_exhaust_action` shape, read off on_failure) — so a task can ACT on a
    # failure instead of only re-asking it (e.g. invalidate a rejected OTP and send a
    # fresh one). Bounded by construction: the retry branch is only reachable while
    # retries < max_retries.
    "then",
    # `fill` ({slot: value}) resolves SEVERAL slots on exhaustion, so a flow that
    # arbitrates over a set of statuses still sees a complete picture when the task
    # feeding them failed. `open_slot` is the one-slot, always-True special case.
    "fill",
})


def _clear_slot_names(spec) -> list[str]:
  """All slot names an `on_failure.clear_slots` can clear, list or reason-keyed dict.

  A dict is keyed by the tool's error_code (values are slot lists), so flatten the
  values; the keys are error codes, not slots, and must not be validated as slots.
  """
  if isinstance(spec, dict):
    names: list[str] = []
    for v in spec.values():
      names.extend(_name_list(v))
    return names
  return _name_list(spec)


# on_exhaust: then (engine ~764 _resolve_exhaust_action), say (~1757/2175/4373),
# open_slot (~2135/4318), component + inputs + outputs (component descent, task
# on_failure ~2159-2165 and no_input ~4365-4367), end_conversation (~4384),
# response + channel_responses (_resolve_response(exhaust, "response"), ~1767/
# 2183/2634 -> channel_responses at ~860). open_slot/component are only
# MEANINGFUL where the reactive descent is implemented (task on_failure +
# no_input); the per-call diff drops them where the allow flag is False, but the
# existing site-gating branches in _check_on_exhaust already report those with a
# specific message, so the diff excludes an already-reported key to avoid a
# duplicate diagnostic.
_VALID_ON_EXHAUST_KEYS = frozenset({
    "then", "say", "open_slot", "component", "inputs", "outputs",
    "end_conversation", "response", "channel_responses",
    # `fill` resolves the awaited slot with an authored value and lets the flow CONTINUE
    # (engine `_no_match_tick`), instead of ending the attempt the way `then` does. Only
    # meaningful on a SLOT's validation.on_exhaust — see _check_on_exhaust.
    "fill",
    # `escalate: False` opts a task-exhaust `then` out of terminal escalation, for an
    # in-flow PIVOT that fires a tool but must let the flow CONTINUE (e.g. OTP -> KBA).
    # Only the TASK on_failure exhaust sets sm["status"], so it is the only site where
    # this is meaningful — see _check_on_exhaust's allow_escalate.
    "escalate",
})


def _slot_enum_options(slot: dict) -> set[str]:
  """Canonical enum values for a slot from its validation_rules (kind=='enum',
  detail pipe-split). Empty set if none."""
  opts = set()
  for rule in _as_seq(slot.get("validation_rules")):
    if isinstance(rule, dict) and rule.get("kind") == "enum":
      opts |= {o.strip() for o in str(rule.get("detail", "")).split("|") if o.strip()}
  return opts

def _looks_numeric(value) -> bool:
  """True if a value is (or parses as) a number — i.e. the int()/float() the
  engine applies in a numeric comparison would succeed. Used to decide whether an
  enum's options are ALL non-numeric (then gt/lt/gte/lte on it is a real crash)."""
  if isinstance(value, bool):
    return True
  if isinstance(value, (int, float)):
    return True
  try:
    float(str(value))
    return True
  except (ValueError, TypeError):
    return False

def _lambda_bound_names(lambda_node) -> set[str]:
  """Every name BOUND inside a lambda-string condition: the lambda's own args,
  plus names bound by any nested lambda arg, comprehension target, or walrus
  (:=) target anywhere in the body. Whitelists Name Loads in
  _validate_lambda_condition (b). Conservative — a union across the whole body —
  so the ERROR never false-positives on a name bound in some inner scope."""

  def _arg_names(args):
    names = set()
    for a in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)):
      names.add(a.arg)
    if args.vararg:
      names.add(args.vararg.arg)
    if args.kwarg:
      names.add(args.kwarg.arg)
    return names

  bound = _arg_names(lambda_node.args)
  for node in ast.walk(lambda_node.body):
    if isinstance(node, ast.Lambda):
      bound |= _arg_names(node.args)
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                           ast.GeneratorExp)):
      for gen in node.generators:
        for t in ast.walk(gen.target):
          if isinstance(t, ast.Name):
            bound.add(t.id)
    elif isinstance(node, ast.NamedExpr):
      if isinstance(node.target, ast.Name):
        bound.add(node.target.id)
  return bound

def _comparison_op_condition_slots(spec):
  """Slots read by a numeric comparison op (gt/lt/gte/lte) in a declarative
  condition. At runtime _eval_condition does int(filled[slot]) for these ops, so
  a provably non-numeric slot crashes the gate -> fail-open (rule c)."""
  if not isinstance(spec, dict):
    return set()
  slots = set()
  if "all" in spec:
    for sub in _sub_conditions(spec.get("all")):
      slots |= _comparison_op_condition_slots(sub)
  elif "any" in spec:
    for sub in _sub_conditions(spec.get("any")):
      slots |= _comparison_op_condition_slots(sub)
  elif "not" in spec:
    slots |= _comparison_op_condition_slots(spec["not"])
  elif "slot" in spec and (set(spec.keys()) & _COMPARISON_OPS):
    if isinstance(spec["slot"], str):
      slots.add(spec["slot"])
  return slots

def _eq_in_condition_leaves(spec):
  """(slot, op, value) for each eq/in leaf in a declarative condition — the
  value-equality ops whose operand should be one of the slot's enum options.
  Used by _check_condition_enum_values (rule e)."""
  out = []
  if not isinstance(spec, dict):
    return out
  if "all" in spec:
    for sub in _sub_conditions(spec.get("all")):
      out += _eq_in_condition_leaves(sub)
  elif "any" in spec:
    for sub in _sub_conditions(spec.get("any")):
      out += _eq_in_condition_leaves(sub)
  elif "not" in spec:
    out += _eq_in_condition_leaves(spec["not"])
  elif isinstance(spec.get("slot"), str):
    if "eq" in spec:
      out.append((spec["slot"], "eq", spec["eq"]))
    if "in" in spec:
      out.append((spec["slot"], "in", spec["in"]))
  return out

def _find_tautologies(spec, slot_map):
  """Always-true 'any' gates — an `any` combinator that can never be false, so
  the condition it guards is inert. Detected within one `any` group on the same
  slot: filled:True + filled:False (both states covered); eq:v + neq:v (one
  always holds); plus a neq / not_in leaf whose operand(s) all lie OUTSIDE the
  slot's declared enum (the slot can never hold that value, so the inequality is
  always satisfied). Heuristic (enum may be intentionally open) -> WARNING."""
  if not isinstance(spec, dict):
    return []
  errors = []
  if "any" in spec:
    items = spec["any"]
    if isinstance(items, list):
      leaves = {}
      for sub in items:
        if isinstance(sub, dict) and isinstance(sub.get("slot"), str):
          leaves.setdefault(sub["slot"], []).append(sub)
        errors.extend(_find_tautologies(sub, slot_map))
      for slot_name, group in leaves.items():
        filleds = [s["filled"] for s in group if "filled" in s]
        if True in filleds and False in filleds:
          errors.append(
              f"Tautological 'any': slot '{slot_name}' filled=True and"
              " filled=False cover all cases (always true)")
        # Unhashable operands are reported by _validate_condition_recursive;
        # this pass skips them rather than hashing them.
        eqs = {s["eq"] for s in group if "eq" in s and _is_hashable(s["eq"])}
        neqs = {s["neq"] for s in group
                if "neq" in s and _is_hashable(s["neq"])}
        both = eqs & neqs
        if both:
          val = sorted(both, key=repr)[0]
          errors.append(
              f"Tautological 'any': slot '{slot_name}' eq={val!r} and"
              f" neq={val!r} together are always true")
        opts = _slot_enum_options(slot_map.get(slot_name, {}))
        if opts:
          for s in group:
            if ("neq" in s and isinstance(s["neq"], str)
                and s["neq"] not in opts):
              errors.append(
                  f"Tautological 'any': slot '{slot_name}' neq={s['neq']!r} is"
                  " outside its enum, so it is always true")
            if (_hashable_members(s.get("not_in"))
                and not (set(s["not_in"]) & opts)):
              errors.append(
                  f"Tautological 'any': slot '{slot_name}' not_in excludes only"
                  " values outside its enum, so it is always true")
  elif "all" in spec:
    for sub in _sub_conditions(spec["all"]):
      errors.extend(_find_tautologies(sub, slot_map))
  elif "not" in spec:
    errors.extend(_find_tautologies(spec["not"], slot_map))
  return errors

# Names the engine's _SAFE_EVAL_GLOBALS exposes to a lambda-string condition. A
# free name in a lambda body that is not the lambda arg (or bound inside the
# body) and not one of these is a NameError at call time -> fail-open gate. KEEP
# IN SYNC with _SAFE_EVAL_GLOBALS["__builtins__"] above.
_LAMBDA_SAFE_NAMES = frozenset({"int", "str", "len", "float", "bool"})


def _source_builds_dynamic_dict(source: str) -> bool:
  """True if tool source builds its return dict with keys unknowable statically.

  Detects `{**x}` unpack, `d.update(...)`, `dict(...)` construction, and a
  subscript assignment with a non-Constant key (`d[k] = ...`). Used as an FP
  guard before flagging completion-text placeholder roots against a statically
  extracted key set. Parse failure returns True (treat keys as unknown).
  """
  try:
    tree = ast.parse(source)
  except (SyntaxError, ValueError, TypeError):
    # TypeError: a non-str source; ValueError: embedded NULs. Both mean "this
    # source cannot be read", which is the same answer as a syntax error.
    return True
  for node in ast.walk(tree):
    if isinstance(node, ast.Dict):
      if any(k is None for k in node.keys):  # {**x} unpack
        return True
    if isinstance(node, ast.Call):
      func = node.func
      if isinstance(func, ast.Attribute) and func.attr == "update":
        return True
      if isinstance(func, ast.Name) and func.id == "dict":
        return True
    if isinstance(node, ast.Assign):
      for target in node.targets:
        if isinstance(target, ast.Subscript):
          sl = target.slice
          if not (isinstance(sl, ast.Constant)
                  and isinstance(sl.value, str)):
            return True
  return False

def _response_format_fields(response) -> set[str]:
  """Collect {placeholder} fields from every string in a response spec.

  A ``then_response`` is a list of response-part dicts, each string of which the
  engine renders through _substitute_response -> str.format. Walks the nested
  list/dict structure and unions the format fields from every string, skipping
  the ``condition`` key (a lambda/DSL spec, not user-facing text).
  """
  fields: set[str] = set()

  def _walk(obj):
    if isinstance(obj, str):
      fields.update(_extract_format_fields(obj))
    elif isinstance(obj, dict):
      for k, v in obj.items():
        if k == "condition":
          continue
        _walk(v)
    elif isinstance(obj, list):
      for v in obj:
        _walk(v)

  _walk(response)
  return fields

def _routing_map(tables: dict, key: str) -> dict:
  """A routing sub-map (flow_config_map/agent_config_map/intent_config_map/
  flow_to_agent) as a plain {str: str} dict. Accepts a dict or a JSON string
  (before_agent._resolve_config_id stores these as JSON strings in SM state, so a
  tool caller may pass either); anything else -> {}. None values are dropped."""
  raw = (tables or {}).get(key)
  if isinstance(raw, str):
    try:
      raw = json.loads(raw)
    except Exception:
      return {}
  if not isinstance(raw, dict):
    return {}
  return {str(k): str(v) for k, v in raw.items() if v is not None}


def _diag_field(d, name):
  """Read `name` from a structured diagnostic entry (dict or object).

  w3-diagnostics-infra owns the concrete entry type; this accessor lets the
  dedupe/sort logic read severity/code/ref/message without hard-coding it.
  """
  if isinstance(d, dict):
    return d.get(name)
  return getattr(d, name, None)

def _dedupe_sort_diagnostics(diags):
  """Dedupe + stably order structured diagnostics; derive flat message lists.

  Dedupes on (code, ref, message) keeping the first occurrence, then stably
  sorts by (severity_rank, code, ref or '') so equal keys keep first-seen
  order. Returns (deduped, error_messages, warning_messages). Idempotent:
  feeding its own output back in yields the same lists.
  """
  seen = set()
  deduped = []
  for d in diags:
    key = (
        _diag_field(d, "code"),
        _diag_field(d, "ref"),
        _diag_field(d, "message"),
    )
    if key in seen:
      continue
    seen.add(key)
    deduped.append(d)
  deduped.sort(key=lambda d: (
      _SEVERITY_RANK.get(_diag_field(d, "severity"), 99),
      _diag_field(d, "code") or "",
      _diag_field(d, "ref") or "",
  ))
  errors = [
      _diag_field(d, "message") for d in deduped
      if _diag_field(d, "severity") == "error"
  ]
  warnings = [
      _diag_field(d, "message") for d in deduped
      if _diag_field(d, "severity") == "warning"
  ]
  return deduped, errors, warnings

# Ordering weight for _dedupe_sort_diagnostics: errors sort before warnings.
# Unknown severities fall to the end (rank 99) rather than crashing.
_SEVERITY_RANK = {"error": 0, "warning": 1}


class DagConfigValidator:
  """Validates a slot filling DAG configuration.

  Usage:
      result = DagConfigValidator(raw_config).validate()
      if not result.valid:
          print(result.errors)
  """

  def __init__(self, config: dict[str, Any],
               available_tools: list[str] | None = None,
               setter_sources: dict[str, str] | None = None,
               task_tool_sources: dict[str, str] | None = None):
    # Normalize ONCE, here, so no check downstream can raise on a malformed
    # authoring shape: a non-dict config, a non-list slots/tasks, or a
    # non-dict entry inside them. Each of those is REPORTED by
    # _check_top_level_structure, which is the only reader of the raw values.
    self._raw_config = config
    self._malformed_config: str | None = (
        None if isinstance(config, dict) else
        f"Config must be a dict, got {type(config).__name__}")
    self._config = config if isinstance(config, dict) else {}
    self._raw_slots = self._config.get("slots", [])
    self._raw_tasks = self._config.get("tasks", [])
    self._slots = [s for s in _as_seq(self._raw_slots)
                   if isinstance(s, dict)]
    self._tasks = [t for t in _as_seq(self._raw_tasks)
                   if isinstance(t, dict)]
    # Partition tasks ONCE at the source (S2): tool/inputs/outputs-reading checks
    # iterate self._normal_tasks (a component has no 'tool', so they would misfire
    # on it); _check_component_tasks is the only check over self._component_tasks;
    # genuinely all-task checks keep iterating self._tasks.
    self._component_tasks = [t for t in self._tasks if _is_component(t)]
    self._normal_tasks = [t for t in self._tasks if not _is_component(t)]
    # A non-string name cannot key these maps (and is reported by
    # _check_slot_names/_check_task_names), so it is dropped rather than hashed.
    self._slot_map = {s["name"]: s for s in self._slots
                      if isinstance(s.get("name"), str)}
    self._slot_names = [s["name"] for s in self._slots
                        if isinstance(s.get("name"), str)]
    self._slot_set = set(self._slot_names)
    # A `count_into` target is written by the ENGINE at dispatch, so a config may gate on
    # a counter that no slot declares — the same shape as the synthesized
    # `<block>_declined` counters below. Visible to CONDITIONS only: it is not a slot to
    # require, fill, announce or read back.
    self._counter_slots = {t["count_into"] for t in self._tasks
                           if isinstance(t.get("count_into"), str)}
    self._condition_slots = self._slot_set | self._counter_slots
    self._task_map = {t["name"]: t for t in self._tasks
                      if isinstance(t.get("name"), str)}
    self._task_names = {t["name"] for t in self._tasks
                        if isinstance(t.get("name"), str)}
    # A member that is not a tool NAME cannot be one of the agent's tools (and
    # is unhashable when it is a container from YAML) — the entry point reports
    # a non-list available_tools; a bad member is simply not a tool.
    self._available_tools = (
        {t for t in available_tools if isinstance(t, str)}
        if available_tools else None)
    self._setter_sources = _as_map(setter_sources)
    self._task_tool_sources = _as_map(task_tool_sources)
    self._fillable_slots: set[str] = set()
    self._reachable_tasks: set[str] = set()
    self._errors: list[str] = []
    self._warnings: list[str] = []
    self._diagnostics: list[dict] = []
    self._blockers: list[str] = []

  def validate(self) -> ValidationResult:
    """Run all checks and return results."""
    if self._malformed_config is not None:
      # Nothing to check INSIDE a config that is not a map — report the shape
      # once instead of emitting a cascade of "no slots"/"no tasks" noise about
      # fields the author never wrote.
      self._error(self._malformed_config)
      return ValidationResult(
          valid=False, errors=list(self._errors), warnings=[],
          diagnostics=list(self._diagnostics), blockers=[])
    self._check_component_tasks()
    self._check_repeated_components()
    self._check_top_level_structure()
    self._check_unknown_keys()
    self._check_bool_fields()
    self._check_identifier_fields()
    self._check_gate_bootstrap_consistency()
    self._check_duplicate_output_targets()
    self._check_duplicate_slots()
    self._check_slot_names()
    self._check_slot_references()
    self._check_shared_slots()
    self._check_value_policy()
    self._check_slot_sources()
    self._check_slot_readback_fmt()
    self._check_requires_readback_source()
    self._check_skip_readback_if_matches()
    self._check_slot_validation_config()
    self._check_slot_validate_against()
    self._check_slot_conditions()
    self._check_repeated_slots()
    self._check_intent_slots()
    self._check_dtmf_map()
    self._check_option_cues()
    self._check_cue_priority()
    self._check_switchable()
    self._check_cancel_menu_return()
    self._check_dtmf_optioncues_twins()
    self._check_repeated_list_conditions()
    self._check_task_names()
    self._check_task_references()
    self._check_task_on_failure()
    self._check_duplicate_executor_tool()
    self._check_awaits()
    self._check_parallel_groups()
    self._check_task_on_complete()
    self._check_loop_risks()
    self._check_circular_requires()
    self._check_bootstrap()
    self._check_gate_slot()
    self._check_steer_back()
    self._check_no_input()
    self._check_answer()
    self._check_control_block("cancel")
    self._check_control_block("escalate")
    self._check_speech()
    self._check_flow_types()
    self._check_route_cues()
    self._check_on_end()
    self._check_exit_status()
    self._check_event_mappings()
    self._check_response_parts()
    self._check_response_text_coverage()
    self._check_format_string_placeholders()
    self._check_orphaned_slots()
    self._check_reachability()
    self._check_ambiguous_conditionless_terminals()
    self._check_condition_slots_reachable()
    self._check_condition_slot_requires()
    self._check_contradictory_conditions()
    self._check_numeric_condition_sources()
    self._check_condition_enum_values()
    self._check_tautological_conditions()
    self._check_tool_availability()
    self._check_setter_output_keys()
    self._check_task_output_keys()
    self._check_completion_field_tool_outputs()
    self._check_duplicate_setter_mappings()
    self._check_clear_slots_subset()
    self._check_announce_dead_config()
    self._check_user_slot_fields()
    self._check_terminal_task_feedback()
    self._check_task_output_source_alignment()
    self._check_exhaust_tool_exists()
    self._check_options_from_ordering()
    self._check_empty_strings()
    self._check_ask_text_response_conflict()
    self._check_readback_inputs_has_readback()
    self._check_counter_writers()
    self._check_multi_setter_fields()
    self._check_on_complete_requires_terminal()
    self._check_ask_ladder()
    self._check_ask_format_requires()
    self._check_single_flow()
    self._finalize()
    return ValidationResult(
        valid=len(self._errors) == 0,
        errors=list(self._errors),
        warnings=list(self._warnings),
        diagnostics=list(self._diagnostics),
        blockers=list(self._blockers),
    )

  # ── Helpers ──────────────────────────────────────────────

  def _error(self, msg: str, code: str | None = None,
             anchor: dict | None = None, fix_id: str | None = None):
    self._errors.append(msg)
    self._diagnostics.append({
        "severity": "error", "message": msg,
        "code": code, "anchor": anchor, "fix_id": fix_id,
    })

  def _warn(self, msg: str, code: str | None = None,
            anchor: dict | None = None, fix_id: str | None = None):
    self._warnings.append(msg)
    self._diagnostics.append({
        "severity": "warning", "message": msg,
        "code": code, "anchor": anchor, "fix_id": fix_id,
    })

  def _blocker(self, msg: str, code: str | None = None,
               anchor: dict | None = None, fix_id: str | None = None):
    """Ship-blocker tier (needs_review): leaves valid (== len(errors)==0)
    unchanged; only shippable (valid and not blockers) flips to False."""
    self._blockers.append(msg)
    self._diagnostics.append({
        "severity": "needs_review", "message": msg,
        "code": code, "anchor": anchor, "fix_id": fix_id,
    })

  def _check_route_cues(self):
    """Validate the optional top-level 'route_cues' dict ({flow: [cue, ...]}).

    route_cues is the deterministic keyword backstop the router reads from state
    (sm['_route_cues']) to set the gate when the model calls the bootstrap tool
    with a bad/empty flow arg. The engine's _route_intent iterates
    route_cues.items() then `for cue in cues`, and the matched `flow` is emitted
    verbatim as the bootstrap `flow` arg. So each mis-shape is a real failure:
      - non-dict container -> route_cues.items() raises AttributeError,
      - non-string / empty flow key -> emitted as a bogus/empty bootstrap flow
        arg (the DAG never activates),
      - non-list cues -> a string value iterates its CHARACTERS (silent
        mis-routing on single letters), a non-iterable raises TypeError,
      - non-string / empty cue -> str(cue).strip() is either wrong or empty
        (skipped), never a real match.
    All ERROR (crash or silent mis-route). A flow key absent from flow_types is
    an independent namespace but loses the deterministic flow-SWITCH backstop
    for that flow (WARN). A router (config.router==True or a bootstrap.tool)
    offering >=2 flow_types with NO route_cues at all falls back to model-only
    routing (WARN) — _route_intent short-circuits on empty _route_cues, so the
    route/switch backstops are disabled.

    Mirrors models.RouterConfig.route_cues (dict[str, list[str]]) — a FLAT
    mapping with no nested keys, so nothing else to whitelist. Runs per-config,
    including for each config in a bundle.
    """
    rc = self._config.get("route_cues")
    flow_types = self._config.get("flow_types")
    ft_list = flow_types if isinstance(flow_types, list) else []
    bootstrap = self._config.get("bootstrap")
    boot_tool = (
        bootstrap.get("tool") if isinstance(bootstrap, dict) else None)
    is_router = bool(self._config.get("router")) or bool(boot_tool)
    no_cues = rc is None or (isinstance(rc, dict) and not rc)
    if is_router and len(ft_list) >= 2 and no_cues:
      self._warn(
          "router with >=2 flow_types has no 'route_cues' — routing is"
          " model-only; the deterministic route/switch backstop is disabled")
    if rc is None:
      return
    if not isinstance(rc, dict):
      self._error("'route_cues' must be a dict {flow: [cue, ...]}")
      return
    for flow, cues in rc.items():
      if not isinstance(flow, str) or not flow.strip():
        self._error(
            f"route_cues flow key must be a non-empty string, got {flow!r}")
      elif ft_list and flow not in ft_list:
        self._warn(
            f"route_cues flow '{flow}' not in flow_types — loses the"
            " deterministic flow-switch backstop for that flow")
      if not isinstance(cues, list):
        self._error(
            f"route_cues['{flow}'] must be a list of cue strings, got"
            f" {type(cues).__name__}")
        continue
      for cue in cues:
        if not isinstance(cue, str) or not cue.strip():
          self._error(
              f"route_cues['{flow}'] entries must be non-empty strings,"
              f" got {cue!r}")

  def _check_on_end(self):
    """Validate the optional top-level 'on_end' (flows.end_params_handoff / Flow.on_end).

    The authoring builder validates its own output, but a config loaded from static YAML/JSON
    never runs it — so a malformed block (e.g. `on_end: "broken"`) would slip past the
    unknown-key check and only crash at runtime, where before_model reads
    on_end['delivery']/['envelope']/['from_state']. Enforce the same shape statically:
    a dict with delivery=='end_params' and non-empty string envelope + identifier from_state.
    Absent -> nothing to check.
    """
    on_end = self._config.get("on_end")
    if on_end is None:
      return
    if not isinstance(on_end, dict):
      self._error(f"'on_end' must be a mapping, got {type(on_end).__name__}")
      return
    if on_end.get("delivery") != "end_params":
      self._error(
          "'on_end.delivery' must be 'end_params'"
          f" (the only supported delivery), got {on_end.get('delivery')!r}")
    envelope = on_end.get("envelope")
    if not isinstance(envelope, str) or not envelope.strip():
      self._error(
          f"'on_end.envelope' must be a non-empty string, got {envelope!r}")
    from_state = on_end.get("from_state")
    if not isinstance(from_state, str) or not from_state.strip():
      self._error(
          f"'on_end.from_state' must be a non-empty string, got {from_state!r}")
    elif not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", from_state):
      self._error(
          "'on_end.from_state' must be a valid variable identifier"
          f" (^[A-Za-z_][A-Za-z0-9_]*$), got {from_state!r}")

  def _check_ambiguous_conditionless_terminals(self):
    """Warn on >=2 reachable terminal tasks that share inputs and have no condition.

    The engine's task-selection loop fires the FIRST task in list order whose
    gating passes (engine _find_next_slot_action, `for task in tasks:`); a task
    with no `condition` key is always active (_is_task_active). So two reachable
    terminal tasks with the SAME input set and no condition are
    indistinguishable at runtime: once those inputs are filled the earlier one
    always fires and the rest are dead code — almost always a forgotten
    mutual-exclusivity condition. Heuristic (no mutual-exclusivity proof is
    attempted) so WARNING. Component tasks are excluded: a component is never
    terminal, and iterating self._normal_tasks already skips them (a
    component-frame terminal lives in the child config, not this one). Reads
    self._reachable_tasks, populated by _check_reachability (registered before).
    """
    groups: dict[frozenset, list[str]] = {}
    for task in self._normal_tasks:
      if not task.get("terminal"):
        continue
      name = _entry_name(task)
      if name not in self._reachable_tasks:
        continue
      if "condition" in task:
        continue
      key = frozenset(_task_input_slots(task.get("inputs")))
      groups.setdefault(key, []).append(name)
    for inputs, names in groups.items():
      if len(names) >= 2:
        self._warn(
            f"Ambiguous terminals {sorted(names)} share inputs"
            f" {sorted(inputs)} and have no condition — the first in task"
            " order always fires; the rest never do")

  def _validate_lambda_condition(self, cond_str, ctx):
    """Structurally validate a lambda-string condition (the
    ``eval(cond, _SAFE_EVAL_GLOBALS)`` form the engine compiles at
    _compile_config, then calls as ``fn(filled)``).

    Beyond a bare compile() syntax check this enforces two invariants the engine
    cannot — both fail OPEN (_is_slot_active / _is_task_active /
    _eval_part_condition swallow every error and return True), so a broken lambda
    SILENTLY makes the gate always-active:
      (a) the string must be a single-plain-positional-arg lambda expression —
          anything else (a bare expr, a def, extra/starred/kw/defaulted args)
          makes ``eval`` yield a non-callable or wrong-arity callable, so
          ``condition(filled)`` raises -> fail-open.
      (b) every free Name loaded in the body must be the lambda's own arg (or a
          name bound by a nested lambda / comprehension / walrus in the body) or
          one of _LAMBDA_SAFE_NAMES — any other name is a NameError under
          _SAFE_EVAL_GLOBALS at call time -> fail-open.
    """
    try:
      tree = ast.parse(cond_str, mode="eval")
    except (SyntaxError, ValueError) as e:
      # ValueError: source with an embedded NUL — unparseable, same as a syntax
      # error as far as the author is concerned.
      self._error(f"{ctx} condition syntax error: {e}")
      return
    body = tree.body
    if not isinstance(body, ast.Lambda):
      self._error(
          f"{ctx} condition string must be a lambda expression (e.g."
          " \"lambda f: ...\") — a non-lambda makes the gate fail open (always"
          " active)")
      return
    args = body.args
    positional = list(args.posonlyargs) + list(args.args)
    if (len(positional) != 1 or args.vararg or args.kwarg or args.kwonlyargs
        or args.defaults or args.kw_defaults):
      self._error(
          f"{ctx} condition lambda must take exactly one plain positional arg"
          " (the filled-slots dict) with no defaults/varargs/kwargs — the engine"
          " calls it as condition(filled)")
      return
    bound = _lambda_bound_names(body)
    for node in ast.walk(body.body):
      if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        if node.id not in bound and node.id not in _LAMBDA_SAFE_NAMES:
          self._error(
              f"{ctx} condition lambda references undefined name '{node.id}' —"
              f" only the lambda arg and {sorted(_LAMBDA_SAFE_NAMES)} are in"
              " scope; anything else is a NameError at runtime (gate fails open,"
              " always active)")

  def _check_numeric_condition_sources(self):
    """ERROR a numeric comparison (gt/lt/gte/lte) on a provably NON-numeric slot.

    _eval_condition does int(filled.get(slot, default)) for these ops (engine
    _eval_condition). If the slot only ever holds a non-numeric string, int()
    raises and _is_slot_active / _is_task_active swallow it -> fail-open (gate
    always active). A slot is provably non-numeric when it is kind:"intent", or
    its validation_rules carry a 'date_format' rule, or an 'enum' rule whose
    options are ALL non-numeric. length_digits slots hold digit strings
    (int()-able) and untyped slots are unknown -> both skipped (FP guard).
    """
    def _provably_non_numeric(slot):
      if slot.get("kind") == "intent":
        return True
      for rule in (slot.get("validation_rules") or []):
        if isinstance(rule, dict) and rule.get("kind") == "date_format":
          return True
      opts = _slot_enum_options(slot)
      if opts and not any(_looks_numeric(o) for o in opts):
        return True
      return False

    def _report(owner_kind, owner_name, cond):
      if not isinstance(cond, dict):
        return
      for ref in sorted(_comparison_op_condition_slots(cond)):
        slot = self._slot_map.get(ref)
        if slot and _provably_non_numeric(slot):
          self._error(
              f"{owner_kind} '{owner_name}' condition uses a numeric comparison"
              f" (gt/lt/gte/lte) on non-numeric slot '{ref}' — the engine does"
              " int(value) on it, which crashes and fails the gate open (always"
              " active)")

    for slot in self._slots:
      _report("Slot", _entry_name(slot), slot.get("condition"))
    for task in self._tasks:
      _report("Task", _entry_name(task), task.get("condition"))

  def _check_condition_enum_values(self):
    """Flag a declarative eq/in operand that is not one of the slot's enum
    options. A condition testing eq:"GOLD" against enum {"gold","silver"} can
    never match (case/typo) — the gate silently never opens. WARNING by default
    (the enum set may be incomplete / inert at runtime), but ERROR for an intent
    slot (kind:"intent") or any slot carrying option_cues, where the enum IS the
    authoritative closed set and a mismatch is a definite dead branch.
    """
    def _report(owner_kind, owner_name, cond):
      if not isinstance(cond, dict):
        return
      for slot_name, op, val in _eq_in_condition_leaves(cond):
        slot = self._slot_map.get(slot_name)
        if not slot:
          continue
        opts = _slot_enum_options(slot)
        if not opts:
          continue
        values = val if (op == "in" and isinstance(val, list)) else [val]
        strict = (slot.get("kind") == "intent"
                  or bool(slot.get("option_cues")))
        for v in values:
          if isinstance(v, str) and v not in opts:
            msg = (
                f"{owner_kind} '{owner_name}' condition {op}={v!r} on slot"
                f" '{slot_name}' is not one of its enum options"
                f" {sorted(opts)} — this branch can never match")
            if strict:
              self._error(msg)
            else:
              self._warn(msg)

    for slot in self._slots:
      _report("Slot", _entry_name(slot), slot.get("condition"))
    for task in self._tasks:
      _report("Task", _entry_name(task), task.get("condition"))

  def _check_tautological_conditions(self):
    """WARNING on always-true 'any' gates (see _find_tautologies). An inert gate
    is usually an author mistake — the branch it was meant to guard always fires.
    """
    for slot in self._slots:
      cond = slot.get("condition")
      if not isinstance(cond, dict) or not _extract_condition_slots(cond):
        continue
      name = _entry_name(slot)
      for err in _find_tautologies(cond, self._slot_map):
        self._warn(f"Slot '{name}': {err}")
    for task in self._tasks:
      cond = task.get("condition")
      if not isinstance(cond, dict) or not _extract_condition_slots(cond):
        continue
      tname = _entry_name(task)
      for err in _find_tautologies(cond, self._slot_map):
        self._warn(f"Task '{tname}': {err}")

  def _check_dtmf_map(self):
    """Validate per-slot dtmf_map ({digit: value}) keypad fills.

    _apply_dtmf_input fills the awaited slot via fill_slots(sm, config,
    {slot: dtmf_map[token]}) (engine ~E4594), and fill_slots writes the value
    VERBATIM into filled[name] (engine ~E1546) — so an off-enum value silently
    lands an invalid slot value that then fails its enum rule (or routes wrong).
    Only the currently-awaited USER slot is ever dtmf-filled (the engine reads
    dtmf_map off _find_next_question's slot), so a dtmf_map on a non-user slot
    is dead config.
    """
    for slot in self._slots:
      dtmf_map = slot.get("dtmf_map")
      if dtmf_map is None:
        continue
      name = _entry_name(slot)
      if not isinstance(dtmf_map, dict):
        self._error(
            f"Slot '{name}' dtmf_map must be a dict, got"
            f" {type(dtmf_map).__name__}")
        continue
      enum_opts = _slot_enum_options(slot)
      if enum_opts:
        for digit, value in dtmf_map.items():
          if not (isinstance(value, str) and value in enum_opts):
            self._error(
                f"Slot '{name}' dtmf_map['{digit}'] value '{value}' is not"
                f" an enum option {sorted(enum_opts)}")
      if "user" not in _normalize_sources(slot.get("source", "user")):
        self._warn(
            f"Slot '{name}' has dtmf_map but source is not 'user' — only an"
            " awaited user slot is ever keypad-filled (dead config)")

  def _check_option_cues(self):
    """Validate per-slot option_cues ({value: [regex, ...]}) on NON-intent slots.

    _apply_option_cues iterates `any(_cue_match(p, text) for p in (pats or []))`
    (engine ~E4657): a BARE-STRING cue list is character-iterated, so each
    character becomes its own regex and the intended phrase never matches
    (silent). The winning value is written VERBATIM by fill_slots (engine
    ~E1546), so an off-enum KEY lands an invalid value. Intent slots carry the
    same shape but are validated (with ERROR-severity key coverage) in
    _check_intent_slots, so they are skipped here.
    """
    for slot in self._slots:
      if slot.get("kind") == "intent":
        continue
      cues = slot.get("option_cues")
      if cues is None:
        continue
      name = _entry_name(slot)
      if not isinstance(cues, dict):
        self._error(
            f"Slot '{name}' option_cues must be a dict, got"
            f" {type(cues).__name__}")
        continue
      for value, pats in cues.items():
        if not isinstance(pats, list) or not all(
            isinstance(p, str) for p in pats):
          self._error(
              f"Slot '{name}' option_cues['{value}'] must be a list of"
              " strings (a bare string is char-iterated as regexes)")
      enum_opts = _slot_enum_options(slot)
      if enum_opts:
        for value in cues:
          if value not in enum_opts:
            self._warn(
                f"Slot '{name}' option_cues key '{value}' is not an enum"
                f" option {sorted(enum_opts)}")
      if "user" not in _normalize_sources(slot.get("source", "user")):
        self._warn(
            f"Slot '{name}' has option_cues but source is not 'user' —"
            " option_cues only fills user slots")

  def _check_switchable(self):  # noqa: D401 - see docstring below
    """`switchable` only does anything on an intent slot that has cues to match.

    The engine reaches it from the `option_cues` override branch, so a `switchable` slot
    with no `option_cues` is inert - and inert in the worst way, because the author has
    declared an intention to support mid-flow topic changes and will believe it works.
    """
    for slot in self._slots:
      if not slot.get("switchable"):
        continue
      name = _entry_name(slot)
      mode = slot.get("switchable")
      if mode not in (True, "defer"):
        self._error(
            f"Slot '{name}' switchable must be true or 'defer', got {mode!r}",
            code=Codes.SWITCHABLE_WITHOUT_CUES,
            anchor={"kind": "slot", "ref": name, "field": "switchable"})
      if not isinstance(slot.get("option_cues"), dict):
        self._error(
            f"Slot '{name}' sets 'switchable' but declares no 'option_cues': "
            f"nothing can ever match, so the caller can never change the subject.",
            code=Codes.SWITCHABLE_WITHOUT_CUES,
            anchor={"kind": "slot", "ref": name, "field": "switchable"})
      elif slot.get("kind") != "intent":
        self._warn(
            f"Slot '{name}' sets 'switchable' but is not kind:'intent'. Re-deciding "
            f"a non-intent slot mid-flow clears every slot downstream of it, which is "
            f"rarely what a data slot wants - use a correction instead.",
            code=Codes.SWITCHABLE_NOT_INTENT,
            anchor={"kind": "slot", "ref": name, "field": "switchable"})

  def _check_cancel_menu_return(self):
    """A menu-returning cancel has to name slots that exist, and name some.

    `clear_slots` is the whole mechanism: it is what un-decides the journey. A typo
    clears nothing, so cancel acknowledges the caller and then asks the very question
    they just backed out of — a loop that looks like the engine ignoring them.
    """
    block = self._config.get("cancel")
    if not isinstance(block, dict) or block.get("end_conversation") is not False:
      return
    names = {s.get("name") for s in self._slots}
    clear = _clear_slot_names(block.get("clear_slots"))
    if not clear:
      self._error(
          "'cancel' sets end_conversation: false but lists no 'clear_slots': the "
          "flow would re-ask the question the caller just cancelled out of.",
          code=Codes.CANCEL_MENU_RETURN_NO_SLOTS,
          anchor={"kind": "field", "ref": "cancel", "field": "clear_slots"})
    for slot_name in clear:
      if slot_name not in names:
        self._error(
            f"'cancel.clear_slots' names '{slot_name}', which is not a slot in "
            f"this flow.",
            code=Codes.CANCEL_MENU_RETURN_NO_SLOTS,
            anchor={"kind": "field", "ref": "cancel", "field": "clear_slots"})

  def _check_cue_priority(self):
    """Validate `cue_priority` — the option_cues ambiguity tiebreak.

    Only "unique" (default: an ambiguous match fills nothing) and "first" (earliest
    DECLARED value wins) are implemented (engine `_apply_option_cues`). On a slot with no
    `option_cues` it is a silent no-op that reads as intent, so flag it.
    """
    for slot in self._slots:
      mode = slot.get("cue_priority")
      if mode is None:
        continue
      name = _entry_name(slot)
      if mode not in ("unique", "first"):
        self._error(
            f"Slot '{name}' cue_priority must be 'unique' or 'first', got {mode!r}")
      elif not slot.get("option_cues"):
        self._warn(
            f"Slot '{name}' sets cue_priority but has no option_cues — no effect")

  def _check_dtmf_optioncues_twins(self):
    """Warn when a slot's dtmf_map values and option_cues keys diverge.

    dtmf_map ({digit: value}) and option_cues ({value: [cue, ...]}) are the
    keypad and text twins of the same enum choice — both fill the slot with a
    canonical value (engine ~E4594 / ~E4667). When both are present their value
    sets should match; a non-empty symmetric difference means one channel can
    select a value the other cannot. Heuristic → WARNING.
    """
    for slot in self._slots:
      dtmf_map = slot.get("dtmf_map")
      cues = slot.get("option_cues")
      if not isinstance(dtmf_map, dict) or not isinstance(cues, dict):
        continue
      name = _entry_name(slot)
      dtmf_vals = {v for v in dtmf_map.values() if isinstance(v, str)}
      cue_keys = {k for k in cues.keys() if isinstance(k, str)}
      diff = dtmf_vals ^ cue_keys
      if diff:
        self._warn(
            f"Slot '{name}' dtmf_map values and option_cues keys diverge:"
            f" {sorted(diff)} — the keypad and text twins select different"
            " value sets")

  def _check_value_policy(self):
    """Validate per-slot `reject` / `default` / `publish`.

    The engine SKIPS a default on a slot the caller is asked for, because defaulting a
    question is a question never asked. Skipping silently is the wrong failure: the
    author sees a slot that never defaults and no reason why, so say it here instead.
    """
    for slot in self._slots:
      if not isinstance(slot, dict):
        continue
      name = _entry_name(slot)
      default = slot.get("default")
      if default is not None:
        if not isinstance(default, list):
          self._error(
              f"slot '{name}': 'default' must be a LIST of {{value, when?}} fallbacks"
              " (the DSL's default= builds this from a value, a fallback(), or a list)")
        elif "user" in _normalize_sources(slot.get("source", "user")):
          self._error(
              f"slot '{name}': a user-filled slot cannot have a 'default' — the engine"
              " would resolve it before the caller was ever asked. Use"
              " validation.on_exhaust.fill, which fires only after they have had the"
              " chance.")
      if isinstance(default, list) and default:
        self._check_default_is_reachable(name, default)
      for key in ("reject", "publish"):
        value = slot.get(key)
        if value is not None and not isinstance(value, list):
          self._error(f"slot '{name}': '{key}' must be a list of strings")

  def _check_default_is_reachable(self, name, default):
    """A default nothing downstream accepts turns a hole into a dead end.

    Before defaults, an output that failed to map left a slot EMPTY, and an empty slot
    is a visible, diagnosable hole. A default makes the same failure produce a
    complete-LOOKING picture built from fallbacks — and if no branch accepts the
    fallback value, the flow has everything it needs and matches nothing. Live that
    surfaces as an engine with no task to fire and no question to ask, which CES
    retries until its reasoning-loop cap and reports as a stack trace naming none of
    this.

    Only flagged when EVERY reader of the slot is a value-comparing leaf and none of
    them accepts any of the defaults. A presence test (`filled`), a `requires`, a
    `{slot}` interpolation or a lambda-source condition all accept anything, so the
    check stays quiet rather than guessing.
    """
    values = [d.get("value") for d in default if isinstance(d, dict)]
    if not values:
      return
    readers = []
    opaque = False
    for owner in list(self._slots) + list(self._tasks):
      cond = owner.get("condition") if isinstance(owner, dict) else None
      if isinstance(cond, str):
        # A lambda body cannot be read for the values it accepts.
        opaque = opaque or (repr(name) in cond or f'"{name}"' in cond)
        continue
      for leaf in _condition_leaves(cond):
        if leaf.get("slot") == name:
          readers.append(leaf)
    if opaque or not readers:
      return
    if any(_accepts_value(leaf, v) for leaf in readers for v in values):
      return
    accepted = sorted({str(leaf.get("eq")) for leaf in readers if "eq" in leaf}
                      | {str(v) for leaf in readers for v in (leaf.get("in") or [])})
    self._warn(
        f"slot '{name}': defaults to {values[0]!r}, which none of the branches"
        f" reading it accept (they accept: {', '.join(accepted) or 'none'}). That is"
        " fine if some OTHER branch carries the defaulted case — but if every branch"
        " is gated on a slot in this state, the flow ends up holding every value it"
        " needs and matching nothing, which the engine can neither fire nor ask its"
        " way out of. Add a branch for the default, or default to a value a branch"
        " already handles.")

  def _check_shared_slots(self):
    """Validate the top-level 'shared_slots' list.

    The engine derives sm['_shared_slots'] purely from per-slot
    'shared: true' (slot_filling_engine ~L3938-3940) and never reads
    the top-level 'shared_slots' key (slot_intake reads sm too). So a
    name here that is not a real slot is dangling, and a listed slot
    lacking 'shared: true' is NOT actually treated as shared at
    runtime.
    """
    shared = self._config.get("shared_slots")
    if not isinstance(shared, list):
      return
    for name in shared:
      if not isinstance(name, str):
        continue
      if name not in self._slot_set:
        self._error(
            f"shared_slots lists '{name}' which is not a defined slot")
      elif not self._slot_map.get(name, {}).get("shared"):
        self._warn(
            f"shared_slots lists '{name}' but slot lacks shared:true"
            " — the engine derives sharing from per-slot shared:true,"
            " so it will not be treated as shared")

  def _check_requires_readback_source(self):
    """Warn when requires_readback is set on a slot with no user path.

    Readback confirmation only fires on the user setter path. Event,
    dtmf, task and programmatic fills go through fill_slots with
    skip_readback=True (slot_filling_engine ~L1516/1545, event fill
    ~L4560, dtmf fill ~L4594), writing straight to filled and skipping
    the confirmation entirely; the ask/deferred paths also gate on
    'user' (~L2316/2742). So requires_readback on a non-user slot is
    silently inert.
    """
    for slot in self._slots:
      if not slot.get("requires_readback"):
        continue
      # Repeated slots already ERROR on requires_readback elsewhere.
      if slot.get("repeated"):
        continue
      name = _entry_name(slot)
      sources = _normalize_sources(slot.get("source", "user"))
      if "user" not in sources:
        self._warn(
            f"Slot '{name}' sets requires_readback but its source"
            f" {sources} has no 'user' path — readback only fires on"
            " user fills, so it will be silently skipped")

  def _check_skip_readback_if_matches(self):
    """`skip_readback_if_matches` must name REAL slots, on a slot that reads back.

    The engine (`_auto_promote_and_route`) suppresses this slot's readback when the
    staged value is digit-identical to one of the NAMED slots' already-filled values.
    Everything about that is silent when it goes wrong: a misspelled source name is
    simply never in `filled`, so it never matches, so the readback the author believed
    they had suppressed keeps firing — and nothing says why. Naming the source is the
    whole safety property of the primitive (the engine deliberately refuses to go
    looking for a matching value on its own), so a name that resolves to nothing is an
    error, not a shrug.

    A listed source is NOT required to declare `requires_readback` itself. That is the
    normal case across a flow boundary: the value was confirmed in an earlier config and
    re-enters this one as a task output.
    """
    known = set(self._slot_set)
    for slot in self._slots:
      if "skip_readback_if_matches" not in slot:
        continue
      name = _entry_name(slot)
      anchor = {"kind": "slot", "ref": name,
                "field": "skip_readback_if_matches"}
      sources = slot.get("skip_readback_if_matches")
      if not isinstance(sources, list) or not sources or not all(
          isinstance(s, str) and s for s in sources):
        self._error(
            f"Slot '{name}' skip_readback_if_matches must be a non-empty list of"
            f" slot names, got {sources!r}",
            code=Codes.SKIP_READBACK_UNKNOWN_SLOT, anchor=anchor)
        continue
      for src in sources:
        if src == name:
          self._error(
              f"Slot '{name}' lists ITSELF in skip_readback_if_matches — the"
              " comparison is against a value confirmed EARLIER, so a self-reference"
              " can only ever suppress the readback of a re-stated answer.",
              code=Codes.SKIP_READBACK_UNKNOWN_SLOT, anchor=anchor)
        elif src not in known:
          self._error(
              f"Slot '{name}' skip_readback_if_matches names unknown slot"
              f" '{src}' — it can never be in 'filled', so the readback it was"
              " meant to suppress fires on every call.",
              code=Codes.SKIP_READBACK_UNKNOWN_SLOT, anchor=anchor)
      if not slot.get("requires_readback"):
        self._warn(
            f"Slot '{name}' sets skip_readback_if_matches but not"
            " 'requires_readback' — there is no readback to skip, so the key is"
            " inert.",
            code=Codes.SKIP_READBACK_WITHOUT_READBACK, anchor=anchor)

  def _check_completion_field_tool_outputs(self):
    """Check completion-text placeholders resolve to a real value.

    A normal task's then_say/then_directive are rendered with
    ``msg_template.format(**{**filled, **result})`` (engine ~L2056-2060):
    a placeholder ROOT that is neither a filled slot nor a key the tool
    returns raises KeyError and crashes the pass. then_response is
    substituted the same way (silently leaving a literal ``{root}`` in the
    spoken confirmation). For each normal task whose tool source parses to
    a known key set, ERROR any placeholder root that isn't a slot, an
    outputs value, in {success, error}, or a tool return key.
    """
    if not self._task_tool_sources:
      return
    for task in self._normal_tasks:
      tool_name = task.get("tool")
      if not tool_name or not isinstance(tool_name, str):
        continue
      source = self._task_tool_sources.get(tool_name)
      if not source:
        continue
      # FP guard: a dynamically-built return dict (** unpack, .update(),
      # dict(), non-Constant subscript key) has keys we can't know
      # statically — skip rather than false-positive.
      if _source_builds_dynamic_dict(source):
        continue
      result = _extract_dict_keys_from_source(source)
      if result is None:
        continue
      tool_keys, _ = result
      outputs = _task_outputs(task)
      valid_roots = (
          self._slot_set
          | set(_output_targets(outputs))
          | tool_keys
          | {"success", "error"})
      roots: set[str] = set()
      for field in ("then_say", "then_directive"):
        template = task.get(field)
        if isinstance(template, str) and template:
          roots |= _extract_format_fields(template)
      roots |= _response_format_fields(task.get("then_response"))
      task_name = _entry_name(task)
      for field in sorted(roots):
        root = field.split(".")[0].split("[")[0]
        if not root:
          continue
        if root not in valid_roots:
          self._error(
              f"Task '{task_name}' completion text references"
              f" '{{{field}}}' but root '{root}' is not a slot, an"
              f" outputs value, or a key tool '{tool_name}' returns")

  def _finalize(self):
    """Dedupe + stably order structured diagnostics; derive flat lists.

    Depends on w3-diagnostics-infra: reads self._diagnostics (structured
    entries carrying severity/code/ref/message). Dedupes on
    (code, ref, message) first-seen, sorts by (severity_rank, code, ref),
    and rebuilds self._diagnostics + self._errors + self._warnings so the
    flat output matches the structured list. Idempotent, so re-running
    validate() on the same instance yields identical ordering. No-op when
    infra is absent (self._diagnostics unset) so this can land safely.
    """
    diags = getattr(self, "_diagnostics", None)
    if diags is None:
      return
    self._diagnostics, self._errors, self._warnings = (
        _dedupe_sort_diagnostics(diags))

  # ── Top-level structure ────────────────────────────────

  def _check_top_level_structure(self):
    """Check that config has slots and optionally tasks.

    Without slots, the engine has nothing to collect and
    _compile_config raises KeyError on config["slots"]. Tasks are
    optional (warn-only) since a config could be announce-only.
    """
    # Router configs (e.g. a Host) do flow-control only — no slot collection —
    # so empty slots/tasks are expected and valid.
    for field, raw in (("slots", self._raw_slots), ("tasks", self._raw_tasks)):
      if not raw:
        continue  # absent/empty — reported below as "has no 'slots'/'tasks'"
      if not isinstance(raw, (list, tuple)):
        self._error(
            f"Config '{field}' must be a list,"
            f" got {type(raw).__name__}")
        continue
      for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
          self._error(
              f"{field[:-1].capitalize()} at index {i} must be a dict,"
              f" got {type(entry).__name__}")
    if self._config.get("router"):
      return
    if not self._slots and not self._tasks:
      self._error("Config has no 'slots' and no 'tasks'")
      return
    if not self._slots:
      self._error("Config has no 'slots'")
    if not self._tasks:
      self._warn("Config has no 'tasks'")

  def _check_unknown_keys(self):
    """Flag unknown keys at config, slot, and task levels.

    The engine accesses config fields via .get() with defaults,
    so a typo like 'input' instead of 'inputs' is silently
    ignored — the task sees an empty input list and fires
    immediately. Whitelisting known keys catches this.
    """
    config_keys = set(self._config.keys())
    unknown = config_keys - _VALID_CONFIG_KEYS
    if unknown:
      self._error(
          f"Unknown top-level config keys: {sorted(unknown)}",
          code=Codes.UNKNOWN_CONFIG_KEY,
          anchor={"kind": "field", "ref": "config", "field": None},
          fix_id="remove_unknown_config_keys")
    for slot in self._slots:
      name = _entry_name(slot)
      unknown = set(slot.keys()) - _VALID_SLOT_KEYS
      if unknown:
        self._error(
            f"Slot '{name}' has unknown keys:"
            f" {sorted(unknown)}",
            code=Codes.UNKNOWN_SLOT_KEY,
            anchor={"kind": "slot", "ref": name, "field": None},
            fix_id="remove_unknown_slot_keys")
      # `sets` is read only by the announce cascade. On any other slot it is accepted,
      # emitted, and then never looked at — the silent no-op this check exists to stop.
      if slot.get("sets") and "announce" not in _normalize_sources(
          slot.get("source", "user")):
        self._error(
            f"Slot '{name}' has 'sets' but is not an announce; only the announce"
            " cascade applies it, so it would be silently ignored",
            code=Codes.UNKNOWN_SLOT_KEY,
            anchor={"kind": "slot", "ref": name, "field": "sets"},
            fix_id="remove_unknown_slot_keys")
      # A FALSY value defeats the whole point. Conditions read a `filled` leaf as
      # `bool(filled.get(slot))`, so a latch written as "" / False / 0 still reads as
      # unfilled: the gate never closes, every lower rung speaks too, and the config
      # looks correct. Rejected rather than written, because the symptom (a caller
      # hearing four verdicts) is a long way from the cause.
      for _k, _v in (slot.get("sets") or {}).items():
        if not _v:
          self._error(
              f"Slot '{name}' sets '{_k}' to {_v!r}, which every condition reads as"
              " UNFILLED — the gate it is meant to close would stay open. Use a"
              " truthy value such as \"true\".",
              code=Codes.UNKNOWN_SLOT_KEY,
              anchor={"kind": "slot", "ref": name, "field": "sets"},
              fix_id="remove_unknown_slot_keys")
    for task in self._tasks:
      name = _entry_name(task)
      unknown = set(task.keys()) - _VALID_TASK_KEYS
      if unknown:
        self._error(
            f"Task '{name}' has unknown keys:"
            f" {sorted(unknown)}",
            code=Codes.UNKNOWN_TASK_KEY,
            anchor={"kind": "task", "ref": name, "field": None},
            fix_id="remove_unknown_task_keys")

  def _check_bool_fields(self):
    r"""Flag boolean fields that are truthy non-bools.

    'terminal: \"false\"' is truthy in Python — the engine
    treats the task as terminal. Same for 'requires_readback:
    \"no\"'. Only bool values behave correctly.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      for field in _BOOL_SLOT_FIELDS:
        val = slot.get(field)
        if val is not None and not isinstance(val, bool):
          self._error(
              f"Slot '{name}' field '{field}' must be"
              f" bool, got {type(val).__name__}: {val!r}")
    for task in self._tasks:
      name = _entry_name(task)
      for field in _BOOL_TASK_FIELDS:
        val = task.get(field)
        if val is not None and not isinstance(val, bool):
          self._error(
              f"Task '{name}' field '{field}' must be"
              f" bool, got {type(val).__name__}: {val!r}")

  def _check_identifier_fields(self):
    """Flag identifier fields (setter/tool/component/event_key) that are not
    strings. The engine keys its setter/tool/event registries by these names,
    so a non-string one is silently unreachable at runtime — and unhashable
    here, which is why every reader of them guards on isinstance."""
    for entries, fields, kind in (
        (self._slots, _ID_SLOT_FIELDS, "Slot"),
        (self._tasks, _ID_TASK_FIELDS, "Task")):
      for entry in entries:
        name = _entry_name(entry)
        name = name if isinstance(name, str) else "<unnamed>"
        for field in fields:
          val = entry.get(field)
          if val is not None and not isinstance(val, str):
            self._error(
                f"{kind} '{name}' field '{field}' must be"
                f" a name string, got {type(val).__name__}")

  def _check_gate_bootstrap_consistency(self):
    """Flag gate_slot != bootstrap.slot mismatch.

    The bootstrap tool fills bootstrap.slot. The engine gates
    on gate_slot. If they differ, the bootstrap fills one slot
    but the engine checks another — the gate never opens.
    """
    gate_slot = self._config.get("gate_slot")
    bootstrap = self._config.get("bootstrap")
    if not gate_slot or not bootstrap:
      return
    if not isinstance(bootstrap, dict):
      return
    boot_slot = bootstrap.get("slot")
    if boot_slot and gate_slot and boot_slot != gate_slot:
      self._error(
          f"gate_slot '{gate_slot}' != bootstrap.slot"
          f" '{boot_slot}' — bootstrap fills '{boot_slot}'"
          f" but engine gates on '{gate_slot}'")

  def _check_duplicate_output_targets(self):
    """Flag tasks with multiple output keys mapping to the same slot.

    outputs maps response keys to slot names. If two keys map
    to the same slot, the second overwrites the first — likely
    a copy-paste bug.
    """
    for task in self._tasks:
      name = _entry_name(task)
      outputs = task.get("outputs")
      if not outputs:
        continue
      if not isinstance(outputs, dict):
        # This is the ONE site that reports the shape; _task_outputs() degrades
        # everywhere else so a list-shaped outputs cannot raise .items() from
        # inside an unrelated check.
        self._error(
            f"Task '{name}' outputs must be a dict of"
            f" {{result_key: slot}}, got {type(outputs).__name__}")
        continue
      seen: dict[str, str] = {}
      for key, slot_name in outputs.items():
        # A key may name SEVERAL slots (one backend field under two slot names), so
        # the collision being checked for is still per TARGET, not per key.
        targets = slot_name if isinstance(slot_name, list) else [slot_name]
        for target in targets:
          if not isinstance(target, str):
            self._error(
                f"Task '{name}' outputs['{key}'] must name a slot (a string),"
                f" got {type(target).__name__}")
            continue
          if target in seen:
            self._error(
                f"Task '{name}' outputs: both"
                f" '{seen[target]}' and '{key}'"
                f" map to slot '{target}'")
          seen[target] = key

  def _check_duplicate_executor_tool(self):
    """Two executor tasks that share ONE tool silently collide.

    The runtime builds its executor registry keyed by TOOL NAME (last write wins) and
    routes a tool result to whichever task registered that tool last — so an earlier
    task's outputs are never committed and its completion gate (`not <output>`) never
    closes, which surfaces as the model re-calling the tool until the reasoning-loop cap
    ("Hmm, I'm having trouble"). Give each executor task a distinct tool, or use a
    `repeated` slot for a real N-times collection.
    """
    by_tool: dict[str, list[str]] = {}
    for task in self._normal_tasks:
      tool = task.get("tool")
      if isinstance(tool, str) and tool:
        by_tool.setdefault(tool, []).append(_entry_name(task))
    for tool, task_names in by_tool.items():
      if len(task_names) > 1:
        self._warn(
            f"Executor tasks {sorted(task_names)} all bind tool '{tool}'; the runtime"
            f" routes tool results by tool name (last write wins), so only one task's"
            f" outputs are committed and the others re-fire indefinitely. Give each a"
            f" distinct tool, or use a 'repeated' slot for an N-times collection.")

  # ── Slot checks ────────────────────────────────────────

  def _check_duplicate_slots(self):
    """Check for duplicate slot names.

    Two slots with the same name cause the second to shadow the
    first in slot_map. The engine silently uses only the last
    definition, losing the first slot's setter, validation, and
    readback config.
    """
    if len(self._slot_names) != len(self._slot_set):
      dupes = {
          n for n in self._slot_names
          if self._slot_names.count(n) > 1
      }
      self._error(f"Duplicate slot names: {dupes}",
                  code=Codes.DUPLICATE_SLOT_NAME,
                  anchor={"kind": "slot", "ref": None, "field": "name"})

  def _check_slot_names(self):
    """Check that every slot has a 'name' key and none use a reserved name.

    Slots without 'name' cause KeyError in slot_map construction
    and are invisible to every engine lookup. The name "cancel" is
    reserved for the synthesized passive cancellation slot (derived from
    the top-level `cancel` block); a user-declared slot of that name would
    collide with it.
    """
    for i, slot in enumerate(self._slots):
      if "name" not in slot:
        self._error(f"Slot at index {i} has no 'name'",
                    code=Codes.SLOT_MISSING_NAME,
                    anchor={"kind": "slot", "ref": None, "field": "name"})
      elif not isinstance(slot["name"], str):
        # The name keys slot_map and every lookup in the engine, so a non-string
        # one makes the slot invisible rather than merely oddly named.
        self._error(
            f"Slot at index {i} has a non-string 'name'"
            f" ({type(slot['name']).__name__})",
            code=Codes.SLOT_MISSING_NAME,
            anchor={"kind": "slot", "ref": None, "field": "name"})
      elif slot["name"] in _RESERVED_SLOT_NAMES:
        self._error(
            f"Slot name '{slot['name']}' is reserved for a synthesized"
            " control slot; rename this slot")

  def _check_slot_references(self):
    """Validate slot wiring for announce, requires, and options_from.

    Catches: announce without 'message' (KeyError at announce time),
    announce with 'setter' (conflicts with auto-fill), requires
    referencing unknown slot (KeyError in _find_next_slot_action),
    and options_from referencing unknown slot (empty chip lists).
    """
    for slot in self._slots:
      name = _entry_name(slot)
      sources = _normalize_sources(
          slot.get("source", "user"))
      if "announce" in sources:
        # `message` OR a non-empty `response` is required — a fully-composed,
        # conditionally-rendered welcome delivers its content via response parts
        # (chime + branched text) instead of a single message string.
        if not slot.get("message") and not slot.get("response"):
          self._error(
              f"Announce slot '{name}' requires 'message' or 'response'",
              code=Codes.ANNOUNCE_SLOT_NO_MESSAGE,
              anchor={"kind": "slot", "ref": name, "field": "message"})
        if slot.get("setter"):
          self._error(
              f"Announce slot '{name}'"
              " must not have 'setter'")
      self._check_name_list_field(slot.get("requires"), f"Slot '{name}'",
                                  "requires")
      for req in _name_list(slot.get("requires")):
        if req in ("cancel", "escalate"):
          self._error(
              f"Slot '{name}' requires '{req}' — nothing may depend on a"
              " control slot (it is never filled in normal flow)")
        elif req not in self._slot_set:
          self._error(
              f"Slot '{name}' requires unknown '{req}'",
              code=Codes.SLOT_REQUIRES_UNKNOWN,
              anchor={"kind": "slot", "ref": name, "field": "requires"})
      options_from = self._find_options_from(slot)
      if options_from and options_from not in self._slot_set:
        self._error(
            f"Slot '{name}' response has options_from"
            f" '{options_from}' not in slots")

  def _find_options_from(self, slot_or_task: dict[str, Any]) -> str | None:
    """Recursively search response parts for options_from."""
    for resp in _as_seq(slot_or_task.get("response")):
      found = self._search_options_from(resp)
      if found:
        return found
    return None

  def _search_options_from(self, obj) -> str | None:
    """Recursively search a response part for options_from."""
    if isinstance(obj, dict):
      if "options_from" in obj:
        return obj["options_from"]
      for v in obj.values():
        found = self._search_options_from(v)
        if found:
          return found
    elif isinstance(obj, list):
      for item in obj:
        found = self._search_options_from(item)
        if found:
          return found
    return None

  def _check_slot_sources(self):
    """Validate that slot sources are recognized and well-formed.

    Valid sources: 'user', 'announce', 'event', 'task:TaskName'.
    Also checks task:X references existing tasks, event source has
    event_key, and setter_field has a parent setter.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      raw_source = slot.get("source", "user")
      for entry in (raw_source if isinstance(raw_source, (list, tuple))
                    else [raw_source]):
        # The owning report for the field: every other reader goes through
        # _normalize_sources(), which hands back only the names it can act on.
        if entry is not None and not isinstance(entry, str):
          self._error(
              f"Slot '{name}' source must be a string or a list of strings,"
              f" got {type(entry).__name__}")
      sources = _normalize_sources(raw_source)
      for source in sources:
        if source.startswith("task:"):
          src_task = source[5:]
          if src_task not in self._task_names:
            self._error(
                f"Slot '{name}' references unknown task"
                f" '{src_task}'")
        elif source not in _VALID_SOURCES:
          self._error(
              f"Slot '{name}' has unknown source"
              f" '{source}'")
      if "event" in sources and not slot.get("event_key"):
        self._warn(
            f"Slot '{name}' has 'event' source but no"
            " 'event_key' — will default to slot name")
      if slot.get("setter_field") and not slot.get("setter"):
        self._error(
            f"Slot '{name}' has 'setter_field' without"
            " 'setter'")

  def _check_slot_readback_fmt(self):
    """Validate readback_fmt type, required fields, unknown params, and value type.

    readback_fmt can be a string shorthand, a dict with type+params,
    or a callable. Checks for unknown types, missing required fields
    (e.g. plural without "one"/"other"), and params the formatter
    does not read — the compiler reads named keys only, so an unknown
    one is discarded in silence and the slot reads back in a voice
    the author did not write.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      fmt = slot.get("readback_fmt")
      if fmt is None:
        continue
      if isinstance(fmt, str):
        if fmt not in _VALID_READBACK_FMT_TYPES:
          self._error(
              f"Slot '{name}' readback_fmt string"
              f" '{fmt}' not recognized — valid:"
              f" {sorted(_VALID_READBACK_FMT_TYPES)}")
        continue
      if isinstance(fmt, dict):
        fmt_type = fmt.get("type")
        if fmt_type is not None and not isinstance(fmt_type, str):
          self._error(
              f"Slot '{name}' readback_fmt 'type' must be a string,"
              f" got {type(fmt_type).__name__}")
          continue
        if not fmt_type:
          self._error(
              f"Slot '{name}' readback_fmt dict"
              " missing 'type'")
          continue
        if fmt_type not in _VALID_READBACK_FMT_TYPES:
          self._error(
              f"Slot '{name}' readback_fmt type"
              f" '{fmt_type}' not recognized — valid:"
              f" {sorted(_VALID_READBACK_FMT_TYPES)}")
          continue
        required = _READBACK_FMT_REQUIRED_FIELDS.get(
            fmt_type, [])
        for field_name in required:
          if field_name not in fmt:
            self._error(
                f"Slot '{name}' readback_fmt type"
                f" '{fmt_type}' missing required"
                f" field '{field_name}'")
        # An UNKNOWN param is an error, not a shrug. The compiler pulls the
        # keys it knows and drops the rest, so a typo or a param that belongs
        # to another type never reaches the formatter: the readback still
        # renders, just not the way it was authored. That is exactly how a
        # `date`'s `text` and a `prefix`'s `values` went missing before, and
        # nothing anywhere said so.
        allowed = ({"type"} | set(required)
                   | set(_READBACK_FMT_OPTIONAL_FIELDS.get(fmt_type, [])))
        unknown = sorted(set(fmt) - allowed)
        if unknown:
          self._error(
              f"Slot '{name}' readback_fmt type"
              f" '{fmt_type}' has unknown param(s)"
              f" {unknown} — the formatter ignores"
              " them at run time; valid:"
              f" {sorted(allowed)}")
        continue
      if not callable(fmt):
        self._error(
            f"Slot '{name}' readback_fmt must be"
            " string, dict, or callable")

  def _check_slot_validation_config(self):
    """Validate the validation block on each slot.

    Checks validation.errors is a dict, max_retries is a positive
    int, and on_exhaust structure is valid. Wrong types cause silent
    failures or TypeErrors at runtime.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      pb = slot.get("push_back")
      if pb is not None:
        if not isinstance(pb, dict):
          self._error(f"Slot '{name}' push_back must be a dict")
        else:
          pb_reprompts = pb.get("reprompts")
          if pb_reprompts is not None and not (
              isinstance(pb_reprompts, list)
              and all(isinstance(r, str) for r in pb_reprompts)):
            self._error(
                f"Slot '{name}' push_back.reprompts must be a list of strings")
          # Scalar keys: the engine does `k <= max` (a TypeError on a non-int),
          # `say.format(...)` (an AttributeError on a non-str), and reads
          # end_conversation/verbatim as booleans. A wrong type slips past an
          # unchecked validator and only surfaces as a crash mid-conversation.
          pb_max = pb.get("max")
          if pb_max is not None and not isinstance(pb_max, int):
            self._error(f"Slot '{name}' push_back.max must be an integer")
          pb_say = pb.get("say")
          if pb_say is not None and not isinstance(pb_say, str):
            self._error(f"Slot '{name}' push_back.say must be a string")
          pb_end = pb.get("end_conversation")
          if pb_end is not None and not isinstance(pb_end, bool):
            self._error(
                f"Slot '{name}' push_back.end_conversation must be a boolean")
          pb_verbatim = pb.get("verbatim")
          if pb_verbatim is not None and not isinstance(pb_verbatim, bool):
            self._error(f"Slot '{name}' push_back.verbatim must be a boolean")
          # A push_back that neither re-offers nor disposes is dead config (the same
          # "ladder with no bottom" trap the validation.on_exhaust check guards against).
          # An EMPTY `reprompts` list is not a re-offer — it exhausts at once — so it
          # has a bottom only when `fill`/`then`/`end_conversation` disposes of the
          # slot; otherwise the slot re-asks itself forever.
          has_reprompts = bool(pb.get("reprompts"))
          has_disposition = any(
              pb.get(k) is not None for k in ("fill", "then")
          ) or bool(pb.get("end_conversation"))
          if not (has_reprompts or has_disposition):
            self._error(
                f"Slot '{name}' push_back disposes of nothing: give a non-empty"
                " 'reprompts' to re-offer and/or 'fill'/'then'/'end_conversation'"
                " to dispose")
          pb_unknown = set(pb.keys()) - _VALID_PUSH_BACK_KEYS
          if pb_unknown:
            self._error(
                f"Slot '{name}' push_back has unknown keys: {sorted(pb_unknown)}")
      # validation_rules is `list[{kind, detail}]` (dsl.slot). A malformed one
      # is REPORTED here and dropped by every reader (_as_seq / isinstance),
      # rather than silently skipped or crashed into.
      rules = slot.get("validation_rules")
      if rules is not None:
        if not isinstance(rules, (list, tuple)):
          self._error(
              f"Slot '{name}' validation_rules must be a list of"
              f" {{kind, detail}} dicts, got {type(rules).__name__}")
        else:
          for rule in rules:
            if not isinstance(rule, dict):
              self._error(
                  f"Slot '{name}' validation_rules entry must be a"
                  f" {{kind, detail}} dict, got {type(rule).__name__}")
      validation = slot.get("validation")
      if not validation:
        continue
      if not isinstance(validation, dict):
        self._error(
            f"Slot '{name}' validation must be a dict")
        continue
      errors = validation.get("errors")
      if errors is not None and not isinstance(errors, dict):
        self._error(
            f"Slot '{name}' validation.errors"
            " must be a dict")
      max_retries = validation.get("max_retries")
      if max_retries is not None:
        if not isinstance(max_retries, int):
          self._error(
              f"Slot '{name}' validation.max_retries"
              " must be an int")
        elif max_retries < 1:
          self._warn(
              f"Slot '{name}' validation.max_retries"
              f" is {max_retries} — effectively no retries")
      on_exhaust = validation.get("on_exhaust")
      if on_exhaust is not None:
        self._check_on_exhaust(
            on_exhaust,
            f"Slot '{name}' validation.on_exhaust", allow_fill=True)
        # SF109. An exhaust is the BOTTOM of the retry ladder — the rung that disposes
        # of an attempt the caller could not complete. On a slot the engine offers
        # exactly three ways to do that: `fill` (resolve it and carry on), `then` (fire
        # a tool) and `response` (emit parts — a hand-off payload, an `end_session`, or
        # both). `open_slot` and `component` are rejected here, so those three are the
        # whole set.
        #
        # A block with `say` and none of them changes NO state: the slot stays unfilled,
        # `retries` is already past `max_retries`, `status` is untouched and no parts are
        # emitted. So the next turn re-asks the same slot, fails the same way and speaks
        # the same line, forever. It is not a quiet exhaust, it is a ladder with no
        # bottom — and it reads as deliberate, because the `say` is usually a goodbye.
        #
        # An ERROR, not a warning, and the sibling of SF020 (a hand-off payload with no
        # `end_session` after it): both are "this terminal speaks and never terminates".
        # Found on a real app that shipped five of them in one revision, one of which
        # said "I'll let you go for now" and then did not, on every subsequent turn.
        if isinstance(on_exhaust, dict) and not any(
            on_exhaust.get(k) is not None for k in ("fill", "then", "response")
        ):
          self._error(
              f"Slot '{name}' validation.on_exhaust disposes of nothing: it needs one"
              " of 'fill' (resolve the slot and continue), 'then' (fire a tool) or"
              " 'response' (e.g. an 'end_session' part). With only 'say' the slot is"
              " never resolved and nothing ends, so the exhaust line repeats on every"
              " later turn.",
              code=Codes.SLOT_EXHAUST_NO_DISPOSITION)
        # An authored fill must be one of the slot's own enum values, or it would
        # resolve the slot to something no condition can match.
        _fill = on_exhaust.get("fill") if isinstance(on_exhaust, dict) else None
        if isinstance(_fill, str):
          _opts = _slot_enum_options(slot) | set((slot.get("option_cues") or {}).keys())
          if _opts and _fill not in _opts:
            self._error(
                f"Slot '{name}' validation.on_exhaust.fill is '{_fill}', which is not"
                f" one of its values: {sorted(_opts)}")
      reprompts = validation.get("reprompts")
      if reprompts is not None and not (
          isinstance(reprompts, list)
          and all(isinstance(r, str) for r in reprompts)):
        self._error(
            f"Slot '{name}' validation.reprompts"
            " must be a list of strings")
      unknown = set(validation.keys()) - _VALID_VALIDATION_KEYS
      if unknown:
        self._error(
            f"Slot '{name}' validation has unknown keys:"
            f" {sorted(unknown)}")

  def _check_slot_validate_against(self):
    """Validate cross-slot validate_against configuration.

    Checks that response_field, filled_slot, and error_code are all
    present, and that filled_slot references a known slot.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      va = slot.get("validate_against")
      if not va:
        continue
      if not isinstance(va, dict):
        self._error(
            f"Slot '{name}' validate_against"
            " must be a dict")
        continue
      for required_field in ("response_field", "filled_slot",
                             "error_code"):
        if required_field not in va:
          self._error(
              f"Slot '{name}' validate_against"
              f" missing '{required_field}'")
      filled_slot = va.get("filled_slot")
      if filled_slot and filled_slot not in self._slot_set:
        self._error(
            f"Slot '{name}' validate_against.filled_slot"
            f" '{filled_slot}' not in slots")

  def _check_slot_conditions(self):
    """Validate slot conditions — declarative dicts or lambda strings.

    Declarative dicts are validated structurally (slot references,
    operator types, unknown keys). Lambda strings are syntax-checked
    via compile().
    """
    for slot in self._slots:
      name = _entry_name(slot)
      cond = slot.get("condition")
      if cond is None or callable(cond):
        continue
      if isinstance(cond, dict):
        for err in _validate_condition_spec(
            cond, self._condition_slots, f"Slot '{name}'"):
          self._error(err)
      elif isinstance(cond, str):
        self._validate_lambda_condition(cond, f"Slot '{name}'")
      else:
        self._error(
            f"Slot '{name}' condition must be dict, callable"
            f" or string, got {type(cond).__name__}")

  # ── Repeated slots / components ─────────────────────────

  def _check_repeated_shape(self, repeated, context):
    """Shape-check the nested keys of a `repeated` dict and its `until`.

    _check_unknown_keys only diffs top-level slot/task keys, so a typo nested
    inside `repeated` (`untill`, `ask_moar`) or `until` (`max_cont`) is
    otherwise invisible — the engine would silently drop the termination
    affordance and collection could never end. Errors on any unknown inner key.
    """
    unknown = set(repeated.keys()) - _VALID_REPEATED_KEYS
    if unknown:
      self._error(
          f"{context} repeated has unknown keys: {sorted(unknown)}")
    until = repeated.get("until")
    if until is not None:
      if not isinstance(until, dict):
        self._error(f"{context} repeated.until must be a dict")
      else:
        unknown_until = set(until.keys()) - _VALID_REPEATED_UNTIL_KEYS
        if unknown_until:
          self._error(
              f"{context} repeated.until has unknown keys:"
              f" {sorted(unknown_until)}")
        done_setter = until.get("done_setter")
        if done_setter is not None and not isinstance(done_setter, str):
          self._error(
              f"{context} repeated.until.done_setter must be a string,"
              f" got {type(done_setter).__name__}")

  def _check_repeated_counts(self, repeated, until, context):
    """Type-check repeated min_count/max_count, mirroring the engine's
    _repeat_done numeric handling (§R2.0).

    CES deserializes config numbers as floats, so `max_count: 3` arrives as 3.0
    and the engine compares it numerically (`isinstance(max_count, (int, float))
    and not isinstance(max_count, bool) and n >= max_count`). So accept an int OR
    an INTEGRAL float here — rejecting a valid 3.0 was a false ship-blocker.
    Still reject bool (True would read as 1) and a NON-integral float (3.5 — the
    engine's `n >= max_count` compare cannot terminate on it cleanly and it
    signals an author typo). max_count must be > 0; min_count >= 0 and
    <= max_count.
    """
    def _coerce(field, value):
      """Integral count for `value`, or None after emitting an error."""
      if isinstance(value, bool):
        self._error(f"{context} {field} must be an int, got bool")
        return None
      if isinstance(value, int):
        return value
      if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
          self._error(f"{context} {field} must be an int, got {value}")
          return None
        if value != int(value):
          self._error(
              f"{context} {field} must be a whole number, got {value}")
          return None
        return int(value)
      self._error(
          f"{context} {field} must be an int, got {type(value).__name__}")
      return None

    max_count = until.get("max_count")
    if max_count is not None:
      max_count = _coerce("repeated.until.max_count", max_count)
      if max_count is not None and max_count <= 0:
        self._error(
            f"{context} repeated.until.max_count must be > 0,"
            f" got {max_count}")
        max_count = None
    min_count = repeated.get("min_count")
    if min_count is not None:
      min_count = _coerce("repeated.min_count", min_count)
      if min_count is None:
        return
      if min_count < 0:
        self._error(
            f"{context} repeated.min_count must be >= 0,"
            f" got {min_count}")
      elif max_count is not None and min_count > max_count:
        self._error(
            f"{context} repeated.min_count ({min_count}) must be <="
            f" max_count ({max_count})")

  def _check_repeated_slots(self):
    """Validate Mode A repeated slots (scalar-collected list slots).

    A slot carrying a `repeated` dict collects N scalars into a list
    (filled[slot] is written once, at completion). It MUST be user-source
    with a setter (each element is staged via the setter), and MUST declare a
    termination affordance (repeated.until.max_count OR
    repeated.until.done_setter) so collection can end. A slot `condition` is
    the activation gate (_is_slot_active) — NOT accepted as a termination
    affordance: a len()-based gate would deactivate the slot mid-collection
    and it would never be asked. requires_readback is rejected (per-element
    readback is out of scope v1; the completed list is surfaced via a
    list-aware readback_fmt). Nested `repeated`/`until` keys are shape-checked
    here because _check_unknown_keys only diffs top-level keys.
    """
    for slot in self._slots:
      repeated = slot.get("repeated")
      if repeated is None:
        continue
      name = _entry_name(slot)
      if not isinstance(repeated, dict):
        self._error(
            f"Slot '{name}' repeated must be a dict, got"
            f" {type(repeated).__name__}")
        continue
      self._check_repeated_shape(repeated, f"Slot '{name}'")
      sources = _normalize_sources(slot.get("source", "user"))
      if "user" not in sources:
        self._error(
            f"Repeated slot '{name}' must have source 'user' —"
            " elements are collected from user input")
      if not slot.get("setter"):
        self._error(
            f"Repeated slot '{name}' must have a 'setter' to stage each"
            " element")
      if slot.get("requires_readback"):
        self._error(
            f"Repeated slot '{name}' must not set requires_readback —"
            " per-element readback is out of scope; surface the completed"
            " list via a list-aware readback_fmt instead")
      until = repeated.get("until")
      until = until if isinstance(until, dict) else {}
      if not _repeat_affordance_ok(until):
        self._error(
            f"Repeated slot '{name}' needs a termination affordance —"
            " repeated.until.max_count or a non-empty repeated.until.done_setter"
            " (a slot 'condition' is the activation gate, not a termination"
            " affordance)")
      self._check_repeated_counts(
          repeated, until, f"Repeated slot '{name}'")
      # (c) Mode A elements are SCALARS, so the engine's _format_join renders each
      # via `each.format(item=el)` (§R2.6) — the ONLY bound name is `item`. A
      # `join` each-template naming any other field KeyErrors at runtime and
      # silently falls back to _flatten_scalar (the raw value), dropping the
      # authored template. Warn (heuristic: a positional/other placeholder).
      fmt = slot.get("readback_fmt")
      if isinstance(fmt, dict) and fmt.get("type") == "join":
        each = fmt.get("each", "")
        try:
          placeholders = {
              fn for _, fn, _, _ in string.Formatter().parse(each) if fn
          }
        except (ValueError, AttributeError):
          placeholders = set()
        bad = sorted(placeholders - {"item"})
        if bad:
          self._warn(
              f"Repeated slot '{name}' readback_fmt `each` references {bad} but"
              " Mode A elements are scalars — only '{item}' is bound; other"
              " fields fall back to the raw value")
      # Mode A done_setter names a real registered tool. Guarded by the
      # available_tools path so tool-less / offline single-config runs stay
      # green (mirrors _check_tool_availability).
      done_setter = until.get("done_setter")
      if self._tool_unknown(done_setter):
        self._error(
            f"Repeated slot '{name}' repeated.until.done_setter"
            f" '{done_setter}' not in agent tool list")

  def _check_intent_slots(self):
    """Validate first-class INTENT slots (kind:"intent").

    An intent slot selects ONE operation from an enum. It MUST carry non-empty
    `option_cues` (deterministic cue->value routing) and an enum `validation_rules`
    entry, and MUST NOT carry a numeric/length rule (length_digits/date_format) —
    such a rule on an operation-choice enum causes a "that should be N digits"
    reject loop. Enforces valid-by-construction so a mis-mined rule never ships.
    """
    _NUMERIC = ("length_digits", "date_format")
    for slot in self._slots:
      if slot.get("kind") != "intent":
        continue
      name = slot.get("name")
      option_cues = slot.get("option_cues")
      if not option_cues:
        self._error(f"Intent slot '{name}' must have non-empty option_cues")
      elif not isinstance(option_cues, dict):
        self._error(
            f"Intent slot '{name}' option_cues must be a dict, got "
            f"{type(option_cues).__name__}")
      else:
        for val, cues in option_cues.items():
          if not isinstance(cues, list) or not all(isinstance(c, str) for c in cues):
            self._error(
                f"Intent slot '{name}' option_cues['{val}'] must be a list of strings")
      rules = _as_seq(slot.get("validation_rules"))
      if not any(isinstance(r, dict) and r.get("kind") == "enum"
                 for r in rules):
        self._error(f"Intent slot '{name}' must have an enum validation_rule")
      # option_cues<->enum coverage: every cue KEY must be a real enum option (an
      # off-enum key routes to a value the engine writes VERBATIM but the enum rule
      # then rejects), and every enum option should carry a cue so it can be
      # captured deterministically instead of only by the LLM. The no-cue warning
      # is SUPPRESSED for a passive (never-asked, model-classified) intent slot,
      # where the model — not a caller utterance — supplies the value.
      enum_opts = _slot_enum_options(slot)
      if enum_opts and isinstance(option_cues, dict):
        for val in option_cues:
          if val not in enum_opts:
            self._error(
                f"Intent slot '{name}' option_cues key '{val}' is not an enum"
                f" option {sorted(enum_opts)}")
        if not slot.get("passive"):
          for opt in sorted(enum_opts):
            if opt not in option_cues:
              self._warn(
                  f"Intent slot '{name}' enum option '{opt}' has no option_cues"
                  " entry — it can only be selected by the model, not"
                  " deterministically")
      bad = sorted({r.get("kind") for r in rules if isinstance(r, dict)
                    and r.get("kind") in _NUMERIC})
      if bad:
        self._error(
            f"Intent slot '{name}' must not carry a numeric/length rule {bad}")
      # Jargon-leak guard: an intent slot is filled either by the caller (an ASKED, humanized choice) or by
      # the model classifying silently (model-classified/router). A model-classified one — no humanized
      # `ask` — MUST be `passive`, else the engine auto-asks it and the model dumps the raw enum as a bogus
      # "choose one of: <internal categories>" question. And a `passive` intent slot is NEVER asked, so it
      # MUST NOT carry a caller-facing enum-listing `not_in_enum` retry (the model would speak it verbatim).
      passive = bool(slot.get("passive"))
      ask = (_ask_floor(slot.get("ask")) or "").strip()
      if not passive and not ask:
        self._error(
            f"Intent slot '{name}' has no `ask` but is not `passive` — a model-classified intent slot "
            "must be passive (never asked); an asked intent slot must carry a humanized `ask`")
      if passive:
        errs = (slot.get("validation") or {}).get("errors") or {}
        if "not_in_enum" in errs:
          self._error(
              f"Passive intent slot '{name}' declares a caller-facing 'not_in_enum' error — a never-asked "
              "slot must not surface its enum to the caller (internal-jargon leak)")

  def _check_repeated_components(self):
    """Validate Mode B repeated components (subflow-per-element).

    A component task carrying a `repeated` dict re-descends its child DAG once
    per element, accumulating each element (a dict) into the `collect` list
    slot. Requires: `collect` names a local slot; `element` is a non-empty
    dict (child_slot -> element field); a termination affordance; and a `join`
    readback_fmt on the collect slot (elements are dicts, so a list-aware
    `each`-template formatter is required to render them without repr leakage).
    `collect`/`element` on a NON-component task, or without `repeated` on a
    component, are rejected. Child-side name resolution (done_setter child
    slot, element child-slot keys) is left to CrossConfigValidator, where child
    configs are visible; single-config only checks shape + local `collect`.
    """
    for task in self._normal_tasks:
      name = _entry_name(task)
      if "collect" in task or "element" in task:
        self._error(
            f"Task '{name}' has 'collect'/'element' but is not a component"
            " task — element collection is Mode B (component) only")
    for task in self._component_tasks:
      name = _entry_name(task)
      repeated = task.get("repeated")
      if repeated is None:
        if "collect" in task or "element" in task:
          self._error(
              f"Component task '{name}' has 'collect'/'element' but no"
              " 'repeated' — element collection requires a repeated dict")
        continue
      if not isinstance(repeated, dict):
        self._error(
            f"Component task '{name}' repeated must be a dict, got"
            f" {type(repeated).__name__}")
        continue
      self._check_repeated_shape(repeated, f"Component task '{name}'")
      collect = task.get("collect")
      if not isinstance(collect, str) or not collect:
        self._error(
            f"Repeated component task '{name}' must name a 'collect' slot"
            " (the list-valued slot to fill)")
      elif collect not in self._slot_set:
        self._error(
            f"Repeated component task '{name}' collect slot '{collect}' not"
            " in slots")
      else:
        fmt = self._slot_map.get(collect, {}).get("readback_fmt")
        fmt_type = fmt.get("type") if isinstance(fmt, dict) else fmt
        if fmt_type != "join":
          self._error(
              f"Repeated component task '{name}' collect slot '{collect}'"
              " must declare a 'join' readback_fmt — elements are dicts and"
              " need a list-aware `each`-template formatter")
        elif isinstance(task.get("element"), dict):
          # The `each` template renders one element dict; every {field} it
          # references must be produced by the element mapping (its values),
          # else .format(**el) KeyErrors at runtime and falls back to raw repr.
          element_fields = set(task["element"].values())
          each = fmt.get("each", "")
          try:
            placeholders = {
                fn for _, fn, _, _ in string.Formatter().parse(each) if fn
            }
          except (ValueError, AttributeError):
            placeholders = set()
          missing = sorted(placeholders - element_fields)
          for ph in missing:
            self._error(
                f"Repeated component task '{name}' collect slot '{collect}'"
                f" readback_fmt `each` references '{{{ph}}}' which is not an"
                " element field (element maps to: "
                f"{sorted(element_fields)})")
      element = task.get("element")
      if not isinstance(element, dict) or not element:
        self._error(
            f"Repeated component task '{name}' must have a non-empty"
            " 'element' dict (child_slot -> element field)")
      until = repeated.get("until")
      until = until if isinstance(until, dict) else {}
      # `over` (a list slot iterated by per-element `each` binding) is itself a termination affordance:
      # the loop ends when the list is exhausted. So it satisfies the requirement on its own — but ONLY
      # if well-formed. A bare `over` without `each`, or a non-string `over`, would let the engine skip
      # list-exhaustion (infinite loop) or crash at runtime (unhashable/attribute errors), so validate it.
      over = repeated.get("over")
      if over is not None:
        if not isinstance(over, str) or not over:
          self._error(
              f"Repeated component task '{name}' repeated.over must be a non-empty"
              f" string (the list slot to iterate), got {type(over).__name__}")
        elif over not in self._slot_set:
          self._error(
              f"Repeated component task '{name}' repeated.over slot '{over}' not"
              " in slots")
        else:
          # (b) `over` must resolve to a LIST the engine iterates per element
          # (_component_fire_action ~L535: `_lst = filled.get(over) or []` then
          # `_lst[i]`). A definitely-scalar slot (kind intent / a length_digits
          # or date_format validation_rule) can NEVER be that list — the engine
          # walks a scalar string char-by-char (silent garbage) or TypeErrors on
          # an int (crash -> "having trouble"): ERROR. A plain user scalar or a
          # Mode-A repeated slot (elements are scalars, not the dicts an `each`
          # child_slot->field mapping expects) is a likely author error: WARN.
          over_slot = self._slot_map.get(over, {})
          rule_kinds = {
              r.get("kind")
              for r in _as_seq(over_slot.get("validation_rules"))
              if isinstance(r, dict) and _is_hashable(r.get("kind"))
          }
          over_sources = _normalize_sources(over_slot.get("source", "user"))
          if over_slot.get("kind") == "intent" or (
              rule_kinds & {"length_digits", "date_format"}):
            self._error(
                f"Repeated component task '{name}' repeated.over slot '{over}'"
                " is a single scalar value (intent/length/date) — not a list to"
                " iterate")
          elif over_slot.get("repeated"):
            self._warn(
                f"Repeated component task '{name}' repeated.over slot '{over}'"
                " is a Mode-A repeated slot (a list of scalars); `over` needs a"
                " list of dicts so each `each` field binds")
          elif "user" in over_sources:
            self._warn(
                f"Repeated component task '{name}' repeated.over slot '{over}'"
                " is a plain user scalar — `over` should be a task-produced list"
                " (a retrieved list), not one collected value")
          # (d) `over` must be PRODUCED before this component fires, else the
          # engine finalizes with 0 elements (_i >= len([]) -> immediate done).
          # A producer is: `over` in this task's `requires` (explicit dep), a
          # positive/truthiness leaf in its `condition` (gated on it being
          # filled), or another task whose `outputs` write `over`. None -> WARN.
          requires = _name_list(task.get("requires"))
          cond_positive = _extract_positive_condition_slots(
              task.get("condition"))
          produced = any(
              over in _output_targets(t.get("outputs"))
              for t in self._tasks if t is not task)
          if (over not in requires and over not in cond_positive
              and not produced):
            self._warn(
                f"Repeated component task '{name}' repeated.over slot '{over}'"
                " is not in requires, not gated by the task condition, and not"
                " produced by any task output — it may be empty when the"
                " component fires")
        each = repeated.get("each")
        if not isinstance(each, dict) or not each:
          self._error(
              f"Repeated component task '{name}' repeated.over requires a non-empty"
              " repeated.each dict (child_slot -> element field)")
      if not _repeat_affordance_ok(until) and not over:
        self._error(
            f"Repeated component task '{name}' needs a termination"
            " affordance — repeated.until.max_count / done_setter, or repeated.over (a list to iterate)")
      self._check_repeated_counts(
          repeated, until, f"Repeated component task '{name}'")

  def _check_repeated_list_conditions(self):
    """Reject a declarative value-op condition (gte/lte/gt/lt/eq/neq/in/not_in)
    against a list-valued (repeated slot or Mode-B `collect`) slot. At runtime
    the numeric ops do int(filled[slot]) -> TypeError (swallowed fail-open, so
    the gate silently never gates); eq/neq compare against the whole list
    container and in/not_in test the list as a single member — all author
    errors. Only a `filled:` truthiness leaf is list-safe; everything else must
    be a len()-based lambda-string condition (§R2.0/§7)."""
    list_slots = {s["name"] for s in self._slots
                  if s.get("repeated") and isinstance(s.get("name"), str)}
    for task in self._tasks:
      if task.get("repeated") and isinstance(task.get("collect"), str):
        list_slots.add(task["collect"])
    list_slots.discard(None)
    if not list_slots:
      return

    def _report(owner_kind, owner_name, cond):
      if not isinstance(cond, dict):
        return
      for ref in sorted(_value_op_condition_slots(cond) & list_slots):
        self._error(
            f"{owner_kind} '{owner_name}' condition uses a declarative value op"
            f" on list-valued slot '{ref}' — declarative ops misbehave on a"
            " list; use a len()-based lambda-string condition instead")

    for slot in self._slots:
      _report("Slot", _entry_name(slot), slot.get("condition"))
    for task in self._tasks:
      _report("Task", _entry_name(task), task.get("condition"))

  # ── Task checks ────────────────────────────────────────

  def _check_task_names(self):
    """Check that every task has a 'name' key.

    Tasks without 'name' cause KeyError in task_results tracking
    and cannot be referenced by task:X slot sources.
    """
    for i, task in enumerate(self._tasks):
      if "name" not in task:
        self._error(f"Task at index {i} has no 'name'")
      elif not isinstance(task["name"], str):
        # The name keys task_map/task_results, so a non-string one makes the
        # task unaddressable rather than merely oddly named.
        self._error(
            f"Task at index {i} has a non-string 'name'"
            f" ({type(task['name']).__name__})")

  _VALID_ON_ABORT = frozenset({"skip", "fail_flow"})

  _FORBIDDEN_COMPONENT_KEYS = (
      "success_check", "readback_inputs", "on_complete", "on_failure",
  )

  def _check_component_tasks(self):
    """Shape-validate Component tasks (single-config only).

    A Component is a Task carrying a 'component' key (a string child config_id),
    mutually exclusive with 'tool'; its inputs/outputs are the same shape as a
    tool task's. This is the ONLY check that iterates self._component_tasks.

    SINGLE-CONFIG BLIND SPOT: a component validated ALONE gets only these shape
    checks — never cross-DAG ref resolution (does the child config_id exist? do
    the child-input/result_key names match the child's slots?). That resolution
    lives in CrossConfigValidator (Section 5.5); a lone DagConfigValidator.validate
    cannot prove a component's wiring.

    Per component task this verifies:
      - a string 'component' ref (the child config_id),
      - tool XOR component: NO 'tool' (exactly one of tool/component),
      - on_abort in {'skip','fail_flow'} when present (optional; defaults 'skip'),
      - FORBIDDEN: 'success_check' (done-marker is always structural 'success'),
        'readback_inputs' (readback-off-by-default; keeps the readback check
        crash-proof), 'on_complete'/'on_failure' (deferred — the child handles
        failures, on_abort handles abort), terminal=True (a component is never the
        parent's terminal task),
      - parent-supplied input slots exist in self._slot_set; outputs target parent
        slots (reuses self._slot_set + the reserved-slot guard, forbidding
        'cancel'/'escalate' in any mapping).

    Component output slots are seeded as fillable for reachability in
    _check_reachability (Section 5.3).
    """
    for task in self._component_tasks:
      name = _entry_name(task)
      comp = task.get("component")
      if not isinstance(comp, str) or not comp:
        self._error(
            f"Component task '{name}' 'component' must be a non-empty"
            f" string child config_id, got {type(comp).__name__}")
      if task.get("tool"):
        self._error(
            f"Component task '{name}' has both 'tool' and 'component' —"
            " a task carries exactly one (tool XOR component)")
      on_abort = task.get("on_abort")
      if on_abort is not None and on_abort not in self._VALID_ON_ABORT:
        self._error(
            f"Component task '{name}' on_abort '{on_abort}' invalid —"
            f" must be one of {sorted(self._VALID_ON_ABORT)}")
      for key in self._FORBIDDEN_COMPONENT_KEYS:
        if key in task:
          self._error(
              f"Component task '{name}' must not have '{key}' —"
              " forbidden on a component task")
      if task.get("terminal"):
        self._error(
            f"Component task '{name}' must not be terminal=True —"
            " a component is never the parent's terminal task")
      for inp in _task_input_slots(task.get("inputs")):
        if inp in ("cancel", "escalate"):
          self._error(
              f"Component task '{name}' input references reserved"
              f" '{inp}' slot")
        elif inp not in self._slot_set:
          self._error(
              f"Component task '{name}' input '{inp}' not in slots")
      for sn in _output_targets(task.get("outputs")):
        if sn in ("cancel", "escalate"):
          self._error(
              f"Component task '{name}' output targets reserved"
              f" '{sn}' slot")
        elif sn not in self._slot_set:
          self._error(
              f"Component task '{name}' output '{sn}' not in slots")

  def _check_name_list_field(self, value, owner: str, field: str):
    """Report a `requires`/`inputs` that is not a list of slot names.

    A bare string is the trap: iterating `requires: "res"` yields characters,
    so ONE bad field became three bogus "unknown slot 'r'/'e'/'s'" errors. The
    readers use _name_list() and see nothing; this is the single report.
    """
    if value is None:
      return
    if isinstance(value, dict) and field == "inputs":
      value = list(value.keys())  # {slot: param} is a legal inputs shape
    if not isinstance(value, (list, tuple)):
      self._error(
          f"{owner} {field} must be a list of slot names,"
          f" got {type(value).__name__}")
      return
    for entry in value:
      if not isinstance(entry, str):
        self._error(
            f"{owner} {field} entry must be a slot name string,"
            f" got {type(entry).__name__}")

  def _check_task_references(self):
    """Validate task tool, inputs, outputs, requires, and conditions.

    Checks that tasks have a 'tool' key, inputs/outputs/requires
    reference known slots, and condition strings compile.
    """
    for task in self._normal_tasks:
      name = _entry_name(task)
      if not task.get("tool"):
        self._error(f"Task '{name}' has no 'tool' key",
                    code=Codes.TASK_NO_TOOL,
                    anchor={"kind": "task", "ref": name, "field": "tool"},
                    fix_id="add_task_tool")
      self._check_name_list_field(task.get("inputs"), f"Task '{name}'",
                                  "inputs")
      self._check_name_list_field(task.get("requires"), f"Task '{name}'",
                                  "requires")
      for inp in _name_list(task.get("inputs")):
        if inp in ("cancel", "escalate"):
          self._error(
              f"Task '{name}' input references reserved '{inp}' slot")
        elif inp not in self._slot_set:
          self._error(
              f"Task '{name}' input '{inp}' not in slots",
              code=Codes.TASK_INPUT_UNKNOWN,
              anchor={"kind": "task", "ref": name, "field": "inputs"})
      for sn in _output_targets(task.get("outputs")):
        if sn in ("cancel", "escalate"):
          self._error(
              f"Task '{name}' output targets reserved '{sn}' slot")
        elif sn not in self._slot_set:
          self._error(
              f"Task '{name}' output '{sn}' not in slots")
      for req in _name_list(task.get("requires")):
        if req in ("cancel", "escalate"):
          self._error(
              f"Task '{name}' requires '{req}' — nothing may depend on a"
              " control slot (it is never filled in normal flow)")
        elif req not in self._slot_set:
          self._error(
              f"Task '{name}' requires '{req}'"
              " not in slots")
      cond = task.get("condition")
      if cond is not None:
        if callable(cond):
          pass
        elif isinstance(cond, dict):
          for err in _validate_condition_spec(
              cond, self._condition_slots, f"Task '{name}'"):
            self._error(err)
        elif isinstance(cond, str):
          self._validate_lambda_condition(cond, f"Task '{name}'")
        else:
          self._error(
              f"Task '{name}' condition must be dict, callable"
              f" or string, got {type(cond).__name__}")

  def _check_task_on_failure(self):
    """Validate task on_failure clear_slots and on_exhaust.

    clear_slots referencing unknown names silently no-ops, so
    the intended retry flow never triggers. on_exhaust structure
    is validated by _check_on_exhaust.
    """
    for task in self._tasks:
      name = _entry_name(task)
      on_failure = task.get("on_failure")
      if not on_failure:
        continue
      if not isinstance(on_failure, dict):
        self._error(
            f"Task '{name}' on_failure must be a dict")
        continue
      for sn in _clear_slot_names(on_failure.get("clear_slots")):
        if sn not in self._slot_set:
          self._error(
              f"Task '{name}' on_failure.clear_slots"
              f" references unknown slot '{sn}'")
      on_exhaust = on_failure.get("on_exhaust")
      if on_exhaust is not None:
        self._check_on_exhaust(
            on_exhaust, f"Task '{name}' on_failure.on_exhaust",
            allow_component=True, allow_open_slot=True, allow_escalate=True,
            allow_reason_keys=True)
      unknown = set(on_failure.keys()) - _VALID_ON_FAILURE_KEYS
      if unknown:
        self._error(
            f"Task '{name}' on_failure has unknown keys:"
            f" {sorted(unknown)}")

  def _check_awaits(self):
    """Validate `awaits` — the ASYNCHRONOUS-tool wait policy.

    `max_turns` is required because CES has no platform-side timeout: a hung async tool
    never sends its completion turn, so without a bound the flow waits forever. A
    terminal task cannot await, because the completion lands on a later turn and the
    engine already defers a terminal fire on a turn carrying user text.
    """
    seen_tools = {}
    for task in self._tasks:
      awaits = task.get("awaits")
      if awaits is None:
        continue
      name = _entry_name(task)
      if not isinstance(awaits, dict):
        self._error(f"Task '{name}' awaits must be a dict")
        continue
      unknown = set(awaits.keys()) - _VALID_AWAITS_KEYS
      if unknown:
        self._error(f"Task '{name}' awaits has unknown keys: {sorted(unknown)}")
      max_turns = awaits.get("max_turns")
      if max_turns is None:
        self._error(
            f"Task '{name}' awaits requires 'max_turns' — CES has no timeout for an"
            " asynchronous tool, so an unbounded wait never ends")
      # An INTEGRAL FLOAT is accepted, not just an int. A config that has round-tripped
      # through JSON — every config the service loads over HTTP, and every one CES hands
      # back — deserializes 3 as 3.0, so rejecting floats here fails a config the engine
      # runs perfectly well. `_sweep_async_timeouts` already reads it as `(int, float)`,
      # and the same session numbers come back from CES as floats (see its `since`
      # comment), so this makes the validator agree with the runtime rather than being
      # stricter than it for no reason. A fractional value is still rejected: turns are
      # discrete, and `max_turns: 2.5` means the author misunderstood the unit.
      elif (not isinstance(max_turns, (int, float))
            or isinstance(max_turns, bool)
            or max_turns != max_turns  # NaN: int() raises, it is not integral
            or max_turns in (float("inf"), float("-inf"))
            or max_turns != int(max_turns)):
        self._error(
            f"Task '{name}' awaits.max_turns must be a whole number,"
            f" got {max_turns!r}")
      elif max_turns <= 0:
        self._error(
            f"Task '{name}' awaits.max_turns must be > 0, got {max_turns!r}")
      # `while_waiting` speaks on the IDLE turns of a wait. Bounded by max_turns for a
      # concrete reason: a line the wait ends before reaching is never spoken, so an
      # author who wrote it is reading a script the caller will not hear.
      lines = awaits.get("while_waiting")
      if lines is not None:
        if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
          self._error(
              f"Task '{name}' awaits.while_waiting must be a list of strings")
        elif (isinstance(max_turns, (int, float)) and not isinstance(max_turns, bool)
              and len(lines) > max_turns):
          self._error(
              f"Task '{name}' awaits.while_waiting has {len(lines)} lines but"
              f" max_turns is {max_turns} — the wait ends before the last is spoken")
      # `answer_first` keeps caller speech that lands on the completion turn, and bounds
      # the terminal deferral that then follows. Turns, like every other bound here.
      answer_first = awaits.get("answer_first")
      if answer_first is not None and (
          not isinstance(answer_first, (int, float))
          or isinstance(answer_first, bool)
          or answer_first != int(answer_first) or answer_first <= 0):
        self._error(
            f"Task '{name}' awaits.answer_first must be a positive whole number of"
            f" turns, got {answer_first!r}")
      on_timeout = awaits.get("on_timeout")
      if on_timeout is not None:
        self._check_on_exhaust(on_timeout, f"Task '{name}' awaits.on_timeout")
      else:
        # Giving up is otherwise SILENT: the sweep drops the wait, the task's output
        # slots stay empty, and anything downstream of them simply never becomes
        # eligible. The caller is left in a flow that has quietly stopped going
        # anywhere, which is worse than an explicit hand-off.
        self._warn(
            f"Task '{name}' awaits without on_timeout — when max_turns is reached the"
            " wait is dropped silently, its outputs never fill, and any task requiring"
            " them can never run. Give it a disposition (say / then).")
      if task.get("terminal"):
        self._error(
            f"Task '{name}' is terminal and awaits — the result arrives on a later"
            " turn, so the terminal fire would never carry it. Split the completion"
            " into a downstream terminal task.")
      # NOT warned about any more. A deferred failure — the platform's own kill, a body
      # that raised, a coded error the body returned — is delivered as a completion
      # envelope and routed into exactly this ladder, so `awaits` plus `on_failure` is
      # the supported way to handle one (ces-probes 116). What `awaits` intercepts is
      # only the `pending` PLACEHOLDER, which is not a failure and never was.
      tool = task.get("tool")
      if tool and isinstance(tool, str):
        if tool in seen_tools:
          self._warn(
              f"Tasks '{seen_tools[tool]}' and '{name}' both await tool '{tool}' — a"
              " completion envelope carries no call id, so only the tool name links it"
              " back and the two waits are indistinguishable")
        seen_tools[tool] = name

  def _check_parallel_groups(self):
    """Shape checks for fan-out groups.

    `parallel()` refuses most of these at build time, but a hand-written config or a
    machine-emitted one never goes through the builder, so the rules live here too.
    """
    groups = {}
    for task in self._tasks:
      group = task.get("parallel")
      if group and not isinstance(group, str):
        self._error(
            f"Task '{task.get('name', '<unnamed>')}' parallel group must be a"
            f" string, got {type(group).__name__}")
        continue
      if group:
        groups.setdefault(group, []).append(task)

    for group, legs in sorted(groups.items()):
      names = [_entry_name(leg) for leg in legs]
      if len(legs) < 2:
        self._error(
            f"Parallel group '{group}' has {len(legs)} leg — a group of one is a plain"
            " task carrying extra machinery. Drop the group, or add a second leg.")

      produced = {}
      for leg in legs:
        for slot in _output_targets(leg.get("outputs")):
          produced[slot] = _entry_name(leg)

      seen_tools = {}
      spoken = {"filler_say": None, "while_running": None}
      for leg in legs:
        name = _entry_name(leg)
        if _is_component(leg):
          self._error(
              f"Task '{name}' is a component and a leg of parallel group '{group}' — a"
              " component descends into a child DAG instead of calling a tool, and a"
              " descent ends the pass, so it cannot ride a shared dispatch. Fire it"
              " before or after the group.")
          continue
        if leg.get("terminal"):
          self._error(
              f"Task '{name}' is terminal and a leg of parallel group '{group}' — a"
              " terminal fire tears the flow down, so the sibling legs' results would"
              " land on a flow that has already ended. Put the terminal downstream of"
              " the group.")
        tool = leg.get("tool")
        # THE RULE THAT IS NOT OPTIONAL. A leg naming a tool that resolves to nothing
        # registered is SILENT AND FATAL: the dispatch survives neither a daemon thread
        # nor `join(timeout=10)`, nothing is logged, no error reaches any channel, and
        # the turn simply dies mid-call (ces-probes 69). Every other broken leg in this
        # method degrades into something a caller or a log can see; this one leaves an
        # app that looks healthy and a call that stops.
        #
        # `_check_tool_availability` already errors on a missing task tool, but it says
        # "not in agent tool list", which reads like a scoping nit. For a leg it is the
        # difference between a working call and a dead one, so it is worth saying twice
        # and worth saying what actually happens.
        if self._tool_unknown(tool):
          self._error(
              f"Parallel group '{group}' leg '{name}' calls tool '{tool}', which is not"
              " in the agent's tool list. A leg that resolves to no registered tool is"
              " silent and fatal — the dispatch yields no result, no error and no log"
              " line, and the turn dies mid-call. Register the tool on the agent, or"
              " correct the leg's tool name.")
        if not tool and not _is_component(leg):
          self._error(
              f"Parallel group '{group}' leg '{name}' names no tool. A group is a"
              " shared dispatch, so a leg with nothing to dispatch is a leg that dies"
              " silently — nothing surfaces and the turn ends mid-call.")
        if tool and isinstance(tool, str) and tool in seen_tools:
          self._error(
              f"Parallel group '{group}' legs '{seen_tools[tool]}' and '{name}' both"
              f" call tool '{tool}' — the whole group comes back in one batch keyed by"
              " tool name, so two calls to one tool cannot be told apart.")
        elif tool and isinstance(tool, str):
          seen_tools[tool] = name
        # A leg consuming a sibling's output can never be satisfied: they are dispatched
        # together, so the value is not filled when this one fires.
        for needed in (_task_input_slots(leg.get("inputs"))
                       + _name_list(leg.get("requires"))):
          if needed in produced and produced[needed] != name:
            self._error(
                f"Parallel group '{group}' leg '{name}' needs slot '{needed}', which"
                f" leg '{produced[needed]}' of the same group produces — they are"
                " dispatched together, so it is not filled when this leg fires. Move"
                f" '{name}' out of the group.")
        for key in spoken:
          if leg.get(key):
            if spoken[key]:
              self._error(
                  f"Parallel group '{group}' legs '{spoken[key]}' and '{name}' both"
                  f" declare '{key}' — the group fires in a single turn, so the caller"
                  " would hear it twice. Put it on one leg.")
            else:
              spoken[key] = name
        if (leg.get("awaits") or {}).get("say"):
          if spoken.get("await_say"):
            self._error(
                f"Parallel group '{group}' legs '{spoken['await_say']}' and '{name}'"
                " both declare a holding line — both waits start on the same turn, so"
                " the caller would hear two. Put it on one leg.")
          else:
            spoken["await_say"] = name

      if len(names) > 1 and not any(
          (leg.get("on_failure") or {}).get("on_exhaust", {}).get("say")
          for leg in legs):
        self._warn(
            f"No leg of parallel group '{group}' has an on_failure line — a leg that"
            " fails contributes nothing, so the caller hears one fewer sentence with"
            " no indication that a check did not happen.")

  _VALID_ON_COMPLETE_KEYS = frozenset({
      "clear_slots", "transfer_to", "auto_resume_deferred",
  })

  def _check_task_on_complete(self):
    """Validate task on_complete structure and references.

    Validates clear_slots slot references, transfer_to type,
    auto_resume_deferred type, and flags unknown keys.
    """
    for task in self._tasks:
      name = _entry_name(task)
      on_complete = task.get("on_complete")
      if not on_complete:
        continue
      if not isinstance(on_complete, dict):
        self._error(
            f"Task '{name}' on_complete must be a dict")
        continue
      unknown = (set(on_complete.keys())
                 - self._VALID_ON_COMPLETE_KEYS)
      if unknown:
        self._warn(
            f"Task '{name}' on_complete has unknown"
            f" keys {unknown}")
      for sn in _clear_slot_names(on_complete.get("clear_slots")):
        if sn not in self._slot_set:
          self._error(
              f"Task '{name}' on_complete.clear_slots"
              f" references unknown slot '{sn}'")
      transfer_to = on_complete.get("transfer_to")
      if transfer_to is not None and not isinstance(
          transfer_to, str):
        self._error(
            f"Task '{name}' on_complete.transfer_to"
            " must be a string")
      auto_resume = on_complete.get("auto_resume_deferred")
      if auto_resume is not None and not isinstance(
          auto_resume, bool):
        self._error(
            f"Task '{name}' on_complete.auto_resume_deferred"
            " must be a bool")

  # ── Loop risk checks ───────────────────────────────────

  def _check_loop_risks(self):
    """Detect task configurations that cause infinite loops.

    Checks two patterns: task with no inputs and not terminal
    (fires every call) and on_failure without max_retries (unbounded
    retries). A terminal task's on_complete NOT clearing its inputs is
    deliberately NOT flagged: a terminal task never re-fires — on
    completion the engine tears the flow down to a zombie via _terminate
    (or _frame_return for a component child).
    """
    for task in self._normal_tasks:
      name = _entry_name(task)
      if (not task.get("inputs", []) and not task.get("terminal")
          and not task.get("condition")):
        # a task gated by a `condition` does NOT fire unconditionally on entry — the condition
        # (e.g. "produce once, only while my output is unfilled") bounds it — so it is exempt from
        # the fire-immediately/loop check even with no inputs.
        self._error(
            f"Task '{name}' has no inputs and is not"
            " terminal — will fire immediately and may loop")
      # A non-dict on_failure is reported by _check_task_on_failure; reading
      # it here as well would raise on the value that check already rejected.
      on_failure = _as_map(task.get("on_failure"))
      if on_failure and "max_retries" not in on_failure:
        self._error(
            f"Task '{name}' has on_failure but no"
            " max_retries — retries would be unbounded")
      # A terminal task never re-fires: on completion the engine tears the flow
      # down to a zombie via _terminate (or _frame_return for a component child)
      # — see engine _handle_task_result. So a terminal on_complete that does
      # not clear its inputs is NOT a re-fire risk. This block only ever ran for
      # terminal tasks, so its 'may re-fire' warning was a pure false positive
      # and is intentionally omitted.

  def _check_circular_requires(self):
    """Detect circular requires chains that stall the flow.

    If slot A requires B and B requires A, neither can ever
    become eligible. Includes implicit edges from declarative
    conditions. Uses DFS cycle detection.

    A slot whose own condition names ITSELF is not a cycle: it is the framework's
    "never-ask receptacle" idiom (the slot is inactive while unfilled, so
    _find_next_question skips it, and a setter writes it from elsewhere). The engine
    has no topological sort and `requires` auto-satisfies an INACTIVE dependency, so
    such a slot stalls nothing. The self-edge is dropped when building `deps`.
    """
    visited, stack = set(), set()

    def has_cycle(name):
      visited.add(name)
      stack.add(name)
      slot_def = self._slot_map.get(name)
      if slot_def:
        deps = set(_name_list(slot_def.get("requires")))
        cond = slot_def.get("condition")
        if isinstance(cond, dict):
          # Self-reference in a CONDITION is the never-ask idiom, not a dependency.
          deps |= _extract_condition_slots(cond) - {name}
        for req in deps:
          if req not in visited:
            if has_cycle(req):
              # Unwind cleanly. Returning with `name` still on the stack left every
              # frame of the failing path in it forever, so every LATER slot whose
              # dependency closure touched one of those names was falsely reported
              # too — one real cycle surfaced as a cascade of unrelated errors.
              stack.discard(name)
              return True
          elif req in stack:
            stack.discard(name)
            return True
      stack.discard(name)
      return False

    for name in self._slot_names:
      if name not in visited:
        if has_cycle(name):
          self._error(
              f"Circular requires involving '{name}'")

  # ── Orphan / reachability / tool checks ─────────────────

  def _check_orphaned_slots(self):
    """Detect slots that have no mechanism to be filled."""
    for slot in self._slots:
      name = _entry_name(slot)
      sources = _normalize_sources(slot.get("source", "user"))
      if ("user" in sources and not slot.get("setter")
          and not slot.get("option_cues")):
        # A CUE-ONLY intent slot (option_cues, no model setter) is still
        # capturable: the engine's deterministic option-cue path fills it from the
        # utterance (and its validation.on_exhaust resolves the awaited-but-unmatched
        # case), with no model tool involved. Authors use this when the model must
        # NOT be able to classify a value itself — e.g. a Verizon-Assistant offer
        # where the model may only accept, and every non-accept rides the
        # reprompt->exhaust ladder rather than being mapped onto an enum value.
        self._error(
            f"Slot '{name}' has source 'user' but no setter"
            " — cannot be filled by user input")
      if "event" in sources and not slot.get("event_key"):
        self._error(
            f"Slot '{name}' has source 'event' but no"
            " event_key")
      for src in sources:
        if src.startswith("task:"):
          task_ref = src[5:]
          if task_ref not in self._task_names:
            self._error(
                f"Slot '{name}' source references unknown"
                f" task '{task_ref}'")

  def _check_reachability(self):
    """Graph walk from fillable roots to detect unreachable slots/tasks.

    Fillable roots: slots with setter, announce source, event+event_key,
    bootstrap.slot, bootstrap.welcome_slot, gate_slot. Fixed-point
    iteration propagates through requires and task inputs/outputs.
    """
    bootstrap = self._config.get("bootstrap", {})
    if not isinstance(bootstrap, dict):
      bootstrap = {}
    gate_slot = self._config.get("gate_slot")

    fillable: set[str] = set()
    for slot in self._slots:
      name = _entry_name(slot)
      sources = _normalize_sources(slot.get("source", "user"))
      if slot.get("setter") or slot.get("option_cues"):
        # option_cues is a fill mechanism in its own right (deterministic cue->value,
        # no setter needed) — a cue-only slot is a reachable root, same as a set one.
        fillable.add(name)
      if "announce" in sources:
        fillable.add(name)
      if "event" in sources and slot.get("event_key"):
        fillable.add(name)
    if isinstance(bootstrap.get("slot"), str):
      fillable.add(bootstrap["slot"])
    if isinstance(bootstrap.get("welcome_slot"), str):
      fillable.add(bootstrap["welcome_slot"])
    if gate_slot:
      if isinstance(gate_slot, str):
        fillable.add(gate_slot)

    changed = True
    reachable_tasks: set[str] = set()
    while changed:
      changed = False
      for slot in self._slots:
        name = _entry_name(slot)
        if name in fillable:
          continue
        reqs = _name_list(slot.get("requires"))
        if not reqs:
          continue
        if all(r in fillable for r in reqs):
          sources = _normalize_sources(
              slot.get("source", "user"))
          has_fill_mechanism = (
              slot.get("setter")
              or "announce" in sources
              or ("event" in sources and slot.get("event_key"))
          )
          if has_fill_mechanism:
            fillable.add(name)
            changed = True
      for task in self._normal_tasks:
        tname = _entry_name(task)
        if tname in reachable_tasks:
          continue
        inputs_ok = all(
            s in fillable for s in _name_list(task.get("inputs")))
        reqs_ok = all(
            s in fillable for s in _name_list(task.get("requires")))
        if inputs_ok and reqs_ok:
          reachable_tasks.add(tname)
          for slot_name in _output_targets(task.get("outputs")):
            if slot_name not in fillable:
              fillable.add(slot_name)
              changed = True
      # Component tasks (S2): a component runs a child DAG and merges its outputs
      # into parent slots. Seed those output slots as fillable once the component's
      # parent-supplied input slots (and requires) are fillable, so a downstream
      # parent slot fed by the component is not flagged unreachable (Section 5.3).
      for task in self._component_tasks:
        inputs_ok = all(
            s in fillable
            for s in _task_input_slots(task.get("inputs")))
        reqs_ok = all(
            s in fillable for s in _name_list(task.get("requires")))
        if inputs_ok and reqs_ok:
          for slot_name in _output_targets(task.get("outputs")):
            if slot_name not in fillable:
              fillable.add(slot_name)
              changed = True
          # Repeated component (Mode B): the `collect` slot (source
          # "task:<Comp>") is filled by per-element append, not by an `outputs`
          # mapping or a setter, so seed it fillable like an output once the
          # component's inputs/requires are — otherwise it is flagged
          # unreachable (Section 5.3 / R2.7).
          collect = task.get("collect")
          if isinstance(collect, str) and collect and collect not in fillable:
            fillable.add(collect)
            changed = True

    self._fillable_slots = fillable
    self._reachable_tasks = reachable_tasks

    for slot in self._slots:
      name = _entry_name(slot)
      if name not in fillable:
        missing = [
            r for r in _name_list(slot.get("requires"))
            if r not in fillable]
        if missing:
          self._error(
              f"Slot '{name}' is unreachable: requires"
              f" unfillable {missing}",
              code=Codes.SLOT_UNREACHABLE,
              anchor={"kind": "slot", "ref": name, "field": None})
        else:
          self._error(f"Slot '{name}' is unreachable",
                      code=Codes.SLOT_UNREACHABLE,
                      anchor={"kind": "slot", "ref": name, "field": None})

    for task in self._normal_tasks:
      tname = _entry_name(task)
      if tname not in reachable_tasks:
        missing_inputs = [
            s for s in _name_list(task.get("inputs"))
            if s not in fillable]
        missing_reqs = [
            s for s in _name_list(task.get("requires"))
            if s not in fillable]
        detail = []
        if missing_inputs:
          detail.append(f"unfillable inputs {missing_inputs}")
        if missing_reqs:
          detail.append(
              f"unfillable requires {missing_reqs}")
        suffix = ": " + ", ".join(detail) if detail else ""
        self._error(
            f"Task '{tname}' is unreachable{suffix}",
            code=Codes.TASK_UNREACHABLE,
            anchor={"kind": "task", "ref": tname, "field": None})

  def _check_condition_slots_reachable(self):
    """Error if a condition references a slot that is never fillable."""
    for slot in self._slots:
      cond = slot.get("condition")
      if not isinstance(cond, dict):
        continue
      name = _entry_name(slot)
      for ref in _extract_condition_slots(cond):
        if ref in self._slot_set and ref not in self._fillable_slots:
          self._error(
              f"Slot '{name}' condition references '{ref}'"
              f" which is unreachable — condition can never"
              f" be satisfied")
    for task in self._tasks:
      cond = task.get("condition")
      if not isinstance(cond, dict):
        continue
      tname = _entry_name(task)
      for ref in _extract_condition_slots(cond):
        if ref in self._slot_set and ref not in self._fillable_slots:
          self._error(
              f"Task '{tname}' condition references '{ref}'"
              f" which is unreachable — condition can never"
              f" be satisfied")

  def _check_condition_slot_requires(self):
    """Warn if condition-referenced slots are not in requires."""
    bootstrap = self._config.get("bootstrap", {})
    if not isinstance(bootstrap, dict):
      bootstrap = {}
    external = set()
    gate = self._config.get("gate_slot")
    if isinstance(gate, str) and gate:
      external.add(gate)
    if isinstance(bootstrap.get("slot"), str):
      external.add(bootstrap["slot"])
    if bootstrap.get("welcome_slot"):
      external.add(bootstrap["welcome_slot"])
    for s_name in _name_list(self._config.get("shared_slots")):
      external.add(s_name)
    for slot in self._slots:
      sources = _normalize_sources(slot.get("source", "user"))
      if "event" in sources:
        external.add(_entry_name(slot))

    for slot in self._slots:
      cond = slot.get("condition")
      if not isinstance(cond, dict):
        continue
      name = _entry_name(slot)
      refs = _extract_positive_condition_slots(cond)
      refs -= external
      refs -= set(_name_list(slot.get("requires")))
      refs.discard(name)
      for ref in sorted(refs):
        if ref in self._slot_set:
          self._warn(
              f"Slot '{name}' condition references '{ref}'"
              f" which is not in its requires list")

    for task in self._normal_tasks:
      cond = task.get("condition")
      if not isinstance(cond, dict):
        continue
      tname = _entry_name(task)
      refs = _extract_positive_condition_slots(cond)
      refs -= external
      refs -= set(_name_list(task.get("requires")))
      refs -= set(_name_list(task.get("inputs")))
      for ref in sorted(refs):
        if ref in self._slot_set:
          self._warn(
              f"Task '{tname}' condition references '{ref}'"
              f" which is not in its requires or inputs")

  def _check_contradictory_conditions(self):
    """Error on always-false condition patterns."""
    for slot in self._slots:
      cond = slot.get("condition")
      if not isinstance(cond, dict):
        continue
      name = _entry_name(slot)
      for err in _find_contradictions(cond):
        self._error(f"Slot '{name}': {err}")
    for task in self._tasks:
      cond = task.get("condition")
      if not isinstance(cond, dict):
        continue
      tname = _entry_name(task)
      for err in _find_contradictions(cond):
        self._error(f"Task '{tname}': {err}")

  def _tool_unknown(self, name) -> bool:
    """True when `name` names a tool the agent does not have.

    A non-string name is not a tool reference at all (_check_identifier_fields
    reports the type), and cannot be hashed into the available-tools set — so
    it is never reported as "missing" from here.
    """
    return (self._available_tools is not None
            and isinstance(name, str) and bool(name)
            and name not in self._available_tools)

  def _check_tool_availability(self):
    """Check that setter/task tools exist in the agent's tool list."""
    if self._available_tools is None:
      return
    for slot in self._slots:
      setter = slot.get("setter")
      if self._tool_unknown(setter):
        self._error(
            f"Slot '{slot.get('name', '<unnamed>')}' setter"
            f" '{setter}' not in agent tool list")
    for task in self._normal_tasks:
      tool = task.get("tool")
      if self._tool_unknown(tool):
        self._error(
            f"Task '{task.get('name', '<unnamed>')}' tool"
            f" '{tool}' not in agent tool list")
    bootstrap = self._config.get("bootstrap", {})
    if isinstance(bootstrap, dict):
      bt = bootstrap.get("tool")
      if self._tool_unknown(bt):
        self._error(
            f"Bootstrap tool '{bt}' not in agent tool list")
    ct = self._config.get("correction_tool")
    if self._tool_unknown(ct):
      self._error(f"correction_tool '{ct}' not in agent tool list")
    intent_change = self._config.get("intent_change", {})
    if isinstance(intent_change, dict):
      it = intent_change.get("tool")
      if self._tool_unknown(it):
        self._error(f"intent_change tool '{it}' not in agent tool list")

  def _check_setter_output_keys(self):
    """Check that setter source code returns the keys the config expects.

    For multi-setters (setter_field), verifies the field name appears
    as a key in the values dict. For simple setters, verifies "value"
    appears in return dicts. Skips if setter source is unavailable or
    unparseable.
    """
    if not self._setter_sources:
      return
    setter_fields: dict[str, list[str]] = {}
    simple_setters: set[str] = set()
    for slot in self._slots:
      setter = slot.get("setter")
      if not setter or not isinstance(setter, str):
        continue
      field = slot.get("setter_field")
      if field:
        setter_fields.setdefault(setter, []).append(field)
      else:
        simple_setters.add(setter)

    for setter_name, fields in setter_fields.items():
      source = self._setter_sources.get(setter_name)
      if not source:
        continue
      values_keys = _extract_values_dict_keys(source)
      if values_keys is None:
        continue
      for field in fields:
        if field not in values_keys:
          self._error(
              f"Setter '{setter_name}' config expects"
              f" setter_field '{field}' but source code"
              f" never writes values[\"{field}\"]")

    for setter_name in simple_setters:
      source = self._setter_sources.get(setter_name)
      if not source:
        continue
      result = _extract_dict_keys_from_source(source)
      if result is None:
        continue
      all_keys, _ = result
      if "value" not in all_keys:
        self._warn(
            f"Setter '{setter_name}' may not return"
            f" a 'value' key")

  def _check_task_output_keys(self):
    """Check that task tools return the keys declared in outputs.

    Config declares outputs: {result_key: slot_name}. The engine
    reads result[result_key] after the task fires. If the tool
    never returns that key, the output slot is silently never filled.
    """
    if not self._task_tool_sources:
      return
    for task in self._normal_tasks:
      tool_name = task.get("tool")
      outputs = _task_outputs(task)
      if not tool_name or not isinstance(tool_name, str) or not outputs:
        continue
      # A REMOTE task is answered by TWO tools, so its outputs come from two places:
      # the tool it names starts the job and returns only a handle, and the declared
      # outputs arrive later from the status tool the engine polls. Checking either one
      # alone rejects a correctly-authored task, so the union is what is checked.
      remote = _as_map(_as_map(self._config.get("remote_tools")).get(tool_name))
      sources = [tool_name] + ([remote["status_tool"]] if remote.get("status_tool")
                               else [])
      all_keys = set()
      seen_any = False
      for source_tool in sources:
        source = self._task_tool_sources.get(source_tool)
        if not source:
          continue
        result = _extract_dict_keys_from_source(source)
        if result is None:
          continue
        seen_any = True
        all_keys |= set(result[0])
      if not seen_any:
        continue
      for result_key in outputs:
        if not isinstance(result_key, str):
          continue  # reported by _check_duplicate_output_targets
        if result_key not in all_keys:
          self._error(
              f"Task '{task.get('name', '<unnamed>')}' expects"
              f" output key '{result_key}' but tool"
              f" '{tool_name}' never returns it")

  def _check_duplicate_setter_mappings(self):
    """Detect multiple slots mapped to the same setter without setter_field.

    The after_tool_callback maps one slot per setter name. If two
    slots point to the same setter without setter_field to
    disambiguate, only the last one in _setter_slots wins.
    """
    simple_setter_users: dict[str, list[str]] = {}
    for slot in self._slots:
      setter = slot.get("setter")
      if not setter or not isinstance(setter, str):
        continue
      if slot.get("setter_field"):
        continue
      name = _entry_name(slot)
      simple_setter_users.setdefault(setter, []).append(name)
    for setter, slot_names in simple_setter_users.items():
      if len(slot_names) > 1:
        self._error(
            f"Slots {slot_names} all map to setter"
            f" '{setter}' without setter_field —"
            f" only the last will receive values",
            code=Codes.DUPLICATE_SETTER,
            anchor={"kind": "field", "ref": setter, "field": "setter"},
            fix_id="split_setter")

  def _check_clear_slots_subset(self):
    """Check that on_failure.clear_slots are inputs of the failing task.

    Clearing a slot that isn't an input to the task doesn't help
    retry it — the task still won't re-fire because its inputs
    haven't changed. Likely a copy-paste error.
    """
    for task in self._normal_tasks:
      name = _entry_name(task)
      on_failure = task.get("on_failure")
      if not on_failure or not isinstance(on_failure, dict):
        continue
      clear_slots = set(_clear_slot_names(on_failure.get("clear_slots")))
      if not clear_slots:
        continue
      inputs = set(_name_list(task.get("inputs")))
      extra = clear_slots - inputs
      if extra:
        self._warn(
            f"Task '{name}' on_failure.clear_slots"
            f" {sorted(extra)} are not inputs of the task"
            f" — clearing them won't trigger a retry")

  def _check_announce_dead_config(self):
    """Flag fields on announce slots that have no effect.

    Announce slots auto-fill without user interaction, so ask,
    readback_fmt, validation, and scan_keywords are dead config.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      sources = _normalize_sources(slot.get("source", "user"))
      if "announce" not in sources:
        continue
      if slot.get("ask"):
        self._warn(
            f"Announce slot '{name}' has 'ask' —"
            " announce slots auto-fill, never prompt")
      if slot.get("readback_fmt"):
        self._warn(
            f"Announce slot '{name}' has 'readback_fmt' —"
            " announce slots are not confirmed by user")
      if slot.get("validation"):
        self._warn(
            f"Announce slot '{name}' has 'validation' —"
            " announce slots auto-fill, never validated")
      if slot.get("scan_keywords"):
        self._warn(
            f"Announce slot '{name}' has 'scan_keywords' —"
            " announce slots don't scan user messages")
      if slot.get("setter"):
        self._warn(
            f"Announce slot '{name}' has 'setter' —"
            " announce slots auto-fill, setter is never used")

  def _check_on_complete_requires_terminal(self):
    """Flag on_complete on non-terminal tasks.

    The engine only processes on_complete inside the terminal
    task block. on_complete on a non-terminal task is dead
    config — the author probably forgot terminal: True.
    """
    for task in self._tasks:
      if task.get("on_complete") and not task.get("terminal"):
        name = _entry_name(task)
        self._error(
            f"Task '{name}' has on_complete but is not"
            " terminal — on_complete is ignored for"
            " non-terminal tasks")

  def _check_ask_ladder(self):
    """An ask LADDER must be a non-empty list of non-empty strings.

    `ask=[]` is an authoring error rather than a silent no-question: the slot would be
    asked with an empty string, and the caller would hear the model improvise a
    question nobody wrote. Same call the control blocks make for `declined_say=[]` —
    omit the field to mean "no question", never an empty list.
    """
    for slot in self._slots:
      ask = slot.get("ask")
      if not isinstance(ask, list):
        continue
      name = _entry_name(slot)
      if not ask:
        self._error(
            f"Slot '{name}' has an empty `ask` ladder — omit `ask` entirely to leave"
            " the slot unasked; an empty list asks the caller nothing")
        continue
      for i, rung in enumerate(ask):
        if not isinstance(rung, str) or not rung.strip():
          self._error(
              f"Slot '{name}' ask ladder rung {i} is not a non-empty string"
              f" ({rung!r}) — every rung is a question the caller will hear")

  def _check_ask_format_requires(self):
    """Flag ask text that references slots not in requires.

    The engine only guarantees a slot is filled before another
    if the second requires the first. If ask uses {B} but the
    slot doesn't require B, the placeholder may render empty.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      ask = slot.get("ask")
      rungs = ask if isinstance(ask, list) else [ask]
      refs = set()
      for rung in rungs:
        if isinstance(rung, str) and rung:
          refs |= set(_extract_format_fields(rung))
      if not refs:
        continue
      requires = set(_name_list(slot.get("requires")))
      for ref in refs:
        if ref in self._slot_set and ref not in requires:
          self._warn(
              f"Slot '{name}' ask uses '{{{ref}}}' but"
              f" doesn't require '{ref}' — placeholder"
              " may be empty when prompt renders")

  def _check_user_slot_fields(self):
    """Check that user-source slots have ask and hint."""
    bootstrap = self._config.get("bootstrap", {})
    if not isinstance(bootstrap, dict):
      bootstrap = {}
    gate_slot = self._config.get("gate_slot")
    external_slots = set()
    if isinstance(bootstrap.get("slot"), str):
      external_slots.add(bootstrap["slot"])
    if isinstance(gate_slot, str) and gate_slot:
      external_slots.add(gate_slot)

    for slot in self._slots:
      # The ask is spoken (and .format()-ed) verbatim, so a non-string one is
      # not a question at all. Reported here, the check that owns `ask`;
      # _ask_floor() hands every other reader "" so it cannot raise on it.
      ask = slot.get("ask")
      for rung in (ask if isinstance(ask, (list, tuple)) else [ask]):
        if rung is not None and not isinstance(rung, str):
          self._error(
              f"Slot '{_entry_name(slot)}' ask must be a string or a list of"
              f" strings, got {type(rung).__name__}")
          break
    for slot in self._slots:
      name = _entry_name(slot)
      if name in external_slots:
        continue
      sources = _normalize_sources(slot.get("source", "user"))
      if "user" not in sources:
        continue
      if not slot.get("setter"):
        continue
      if slot.get("passive"):
        continue                    # passive slot is never asked (model/cues fill it) — no ask expected
      if not slot.get("ask"):
        self._warn(
            f"Slot '{name}' has source 'user' but no 'ask'"
            " — LLM gets no prompt hint for this slot",
            code=Codes.USER_SLOT_NO_ASK,
            anchor={"kind": "slot", "ref": name, "field": "ask"},
            fix_id="add_ask")
      if not slot.get("hint"):
        self._warn(
            f"Slot '{name}' has source 'user' but no 'hint'"
            " — gate mode shows raw slot name in tool list")

  def _check_terminal_task_feedback(self):
    """Check that terminal tasks provide user feedback."""
    for task in self._tasks:
      name = _entry_name(task)
      if not task.get("terminal"):
        continue
      has_then_say = bool(task.get("then_say"))
      has_directive = bool(task.get("then_directive"))
      has_response = bool(task.get("then_response"))
      if not has_then_say and not has_directive and not has_response:
        self._warn(
            f"Terminal task '{name}' has no 'then_say',"
            " 'then_directive', or 'then_response' —"
            " flow ends with no user feedback")

  def _check_task_output_source_alignment(self):
    """Check that task outputs and slot sources agree.

    If a task declares outputs: {key: slot_name}, that slot should
    have source including 'task:TaskName'. Conversely, a slot with
    source 'task:X' should appear in task X's outputs.

    The producer side counts COMPONENT tasks too, because a component fills parent
    slots exactly like a tool task does — `_frame_return` writes
    `filled[parent_slot] = child_filled[child_key]` for every `{child_key:
    parent_slot}` in the component's `outputs`, and a repeated component writes
    `filled[collect]` with the accumulated element list. Reading only
    `self._normal_tasks` here made the second loop below fire on EVERY
    component-filled slot: a 100% false-positive rate, so the warning carried no
    signal for that shape. It fires on the blessed corpus itself (concierge's
    `guest_address`/`guest_phone`, bella_notte's `party_guests`), and `task:<the
    component task>` is the only correct declaration for such a slot — `user`
    would make the engine ask the caller for it. The reverse (first) loop stays
    scoped to normal tasks: widening it would ADD a new warning class rather than
    remove a wrong one.
    """
    task_output_slots: dict[str, set[str]] = {}
    for task in self._tasks:
      tname = _entry_name(task)
      for slot_name in _output_targets(task.get("outputs")):
        task_output_slots.setdefault(tname, set()).add(slot_name)
      collect = task.get("collect")
      if (_is_component(task) and task.get("repeated")
          and isinstance(collect, str) and collect):
        task_output_slots.setdefault(tname, set()).add(collect)

    for task in self._normal_tasks:
      tname = _entry_name(task)
      for slot_name in _output_targets(task.get("outputs")):
        if slot_name not in self._slot_map:
          continue
        slot_def = self._slot_map[slot_name]
        sources = _normalize_sources(
            slot_def.get("source", "user"))
        expected = f"task:{tname}"
        if expected not in sources:
          self._warn(
              f"Task '{tname}' outputs to slot"
              f" '{slot_name}' but slot source"
              f" {sources} doesn't include '{expected}'")

    for slot in self._slots:
      name = _entry_name(slot)
      sources = _normalize_sources(slot.get("source", "user"))
      for src in sources:
        if not src.startswith("task:"):
          continue
        task_name = src[5:]
        if task_name not in self._task_names:
          continue
        output_slots = task_output_slots.get(task_name, set())
        if name not in output_slots:
          self._warn(
              f"Slot '{name}' declares source '{src}'"
              f" but task '{task_name}' has no output"
              f" pointing to '{name}'")

  def _check_exhaust_tool_exists(self):
    """Check that on_exhaust.then tool references exist."""
    if self._available_tools is None:
      return
    # `escalate`/`cancel` are engine dispositions, not agent tools — always allowed.
    known = self._available_tools | {
        "end_session", "transfer_to_agent", "escalate", "cancel",
    }

    def _check_then(exhaust, context):
      if not isinstance(exhaust, dict):
        return
      # `then` may be a {tool, args} dict OR a bare string tool name; both resolve
      # to a function_call at runtime (_resolve_exhaust_action), so both are checked.
      then = exhaust.get("then")
      if isinstance(then, str):
        tool = then
      elif isinstance(then, dict):
        tool = then.get("tool")
      else:
        return
      if tool and tool not in known:
        self._error(
            f"{context}.then tool '{tool}'"
            " not in agent tool list")

    for slot in self._slots:
      name = _entry_name(slot)
      validation = slot.get("validation")
      if isinstance(validation, dict):
        on_exhaust = validation.get("on_exhaust")
        if on_exhaust:
          _check_then(
              on_exhaust,
              f"Slot '{name}' validation.on_exhaust")
    for task in self._tasks:
      name = _entry_name(task)
      on_failure = task.get("on_failure")
      if isinstance(on_failure, dict):
        on_exhaust = on_failure.get("on_exhaust")
        if on_exhaust:
          _check_then(
              on_exhaust,
              f"Task '{name}' on_failure.on_exhaust")
    sb = self._config.get("steer_back")
    if isinstance(sb, dict):
      on_exhaust = sb.get("on_exhaust")
      if on_exhaust:
        _check_then(on_exhaust, "steer_back.on_exhaust")
    ni = self._config.get("no_input")
    if isinstance(ni, dict):
      on_exhaust = ni.get("on_exhaust")
      if on_exhaust:
        _check_then(on_exhaust, "no_input.on_exhaust")

  def _check_options_from_ordering(self):
    """Warn if options_from references a slot filled after this one.

    options_from reads a filled slot's value to build chip options.
    If the source slot requires this slot (or is otherwise ordered
    after it), the options will be empty when the prompt fires.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      options_from = self._find_options_from(slot)
      if not options_from or options_from not in self._slot_map:
        continue
      source_slot = self._slot_map[options_from]
      source_reqs = _name_list(source_slot.get("requires"))
      if name in source_reqs:
        self._warn(
            f"Slot '{name}' options_from '{options_from}'"
            f" but '{options_from}' requires '{name}'"
            " — options won't be filled yet")

  def _check_empty_strings(self):
    """Flag empty strings in user-facing fields."""
    for slot in self._slots:
      name = _entry_name(slot)
      for field in ("ask", "message"):
        val = slot.get(field)
        if val is not None and isinstance(val, str) and not val.strip():
          self._warn(
              f"Slot '{name}' has empty '{field}' string")
    for task in self._tasks:
      name = _entry_name(task)
      for field in ("then_say", "then_directive"):
        val = task.get(field)
        if val is not None and isinstance(val, str) and not val.strip():
          self._warn(
              f"Task '{name}' has empty '{field}' string")

  def _check_ask_text_response_conflict(self):
    """Flag slots that have both 'ask' and a text-type response part.

    If a slot has 'ask', the engine uses it as the user prompt. A
    text-type response part would also emit text, creating duplicate
    or conflicting prompts. Use one or the other.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      if not slot.get("ask"):
        continue
      for resp in _as_seq(slot.get("response")):
        if not isinstance(resp, dict):
          continue
        if resp.get("type") == "text":
          self._warn(
              f"Slot '{name}' has both 'ask' and a text-type"
              " response — these are redundant; use one or"
              " the other")
          break

  def _check_counter_writers(self):
    """A `count_into` slot must be the engine's alone to write.

    The engine reads the running total back with `int(...)` at dispatch, so any other
    writer puts a non-numeric value in its way: a spoken account number, another task's
    output. That raises mid-dispatch, inside a deployed agent, on a live call — the
    caller hears the platform's failure line and the trace points at the engine rather
    than at the config that caused it.

    The authoring DSL refuses the same collision, which catches it earlier and with a
    better message. This is the backstop for a config that never went through the DSL:
    hand-written JSON, a generated config, a YAML flow.
    """
    produced: dict[str, str] = {}
    for task in self._tasks:
      # A malformed `outputs` (a list, a string) has its own check; this one must not be
      # what raises on it.
      outputs = task.get("outputs")
      if not isinstance(outputs, dict):
        continue
      for out in outputs.values():
        # One out_key can map to SEVERAL slots, so a value is a name or a list of them.
        for slot in (out if isinstance(out, list) else [out]):
          if isinstance(slot, str):
            produced.setdefault(slot, _entry_name(task))
    spoken = set()
    for slot in self._slots:
      for src in _normalize_sources(slot.get("source", "user")):
        if isinstance(src, str) and src.startswith("user"):
          spoken.add(_entry_name(slot))

    for task in self._tasks:
      counter = task.get("count_into")
      if not isinstance(counter, str) or not counter:
        continue
      name = _entry_name(task)
      if counter in spoken:
        self._error(
            f"Task '{name}' counts into '{counter}', which is a slot the CALLER fills."
            " The count would be overwritten with speech and the engine would raise"
            " reading it back as a number. Count into a name of its own.")
      elif counter in produced:
        self._error(
            f"Task '{name}' counts into '{counter}', which is also an output slot of"
            f" task '{produced[counter]}'. A counter may have only one writer — count"
            " into a name of its own.")

  def _check_readback_inputs_has_readback(self):
    """Flag tasks with readback_inputs but no readback-eligible inputs.

    readback_inputs defers slot collection for grouped confirmation.
    If none of the task's input slots have requires_readback, the
    readback phase is empty and the task fires without confirmation.
    """
    for task in self._normal_tasks:
      name = _entry_name(task)
      if not task.get("readback_inputs"):
        continue
      inputs = _name_list(task.get("inputs"))
      if isinstance(inputs, dict):
        inputs = list(inputs.keys())
      has_readback = any(
          self._slot_map.get(s, {}).get("requires_readback")
          for s in inputs
      )
      if not has_readback:
        self._warn(
            f"Task '{name}' has readback_inputs but none"
            " of its input slots have requires_readback"
            " — confirmation will be skipped")

  def _check_multi_setter_fields(self):
    """Flag slots sharing a setter without setter_field.

    When 2+ slots use the same setter tool, each needs a
    setter_field so the engine can route the tool response
    to the correct slot. Without it, only one slot gets filled.
    """
    setter_slots: dict[str, list[str]] = {}
    for slot in self._slots:
      setter = slot.get("setter")
      if setter and isinstance(setter, str):
        setter_slots.setdefault(setter, []).append(_entry_name(slot))
    for setter, slots in setter_slots.items():
      if len(slots) < 2:
        continue
      missing = [
          s for s in slots
          if not self._slot_map.get(s, {}).get("setter_field")
      ]
      if missing:
        self._error(
            f"Slots {missing} share setter '{setter}'"
            " but lack setter_field — engine can't route"
            " the tool response to the correct slot")

  # ── Bootstrap / gate / top-level checks ────────────────

  def _check_bootstrap(self):
    """Validate bootstrap slot, welcome_slot, and tool references.

    bootstrap.slot and gate_slot are often filled externally (by
    the Root Agent) so missing from local slots is a warning.
    welcome_slot should point to an announce slot.
    """
    bootstrap = self._config.get("bootstrap")
    if not bootstrap:
      return
    if not isinstance(bootstrap, dict):
      self._error("'bootstrap' must be a dict")
      return
    for field in ("slot", "welcome_slot", "tool"):
      value = bootstrap.get(field)
      if value is not None and not isinstance(value, str):
        self._error(
            f"bootstrap.{field} must be a name string,"
            f" got {type(value).__name__}")
        return
    slot = bootstrap.get("slot")
    # A bootstrap WITH a tool fills its own slot, so the slot not being declared
    # in `slots` is the expected gate pattern, not a warning. Only warn when there
    # is no tool to fill it (it then truly relies on an external fill).
    if (slot and slot not in self._slot_set and not bootstrap.get("tool")
        and not self._config.get("single_flow")):
      self._warn(
          f"bootstrap.slot '{slot}' not in slots"
          " — may be filled externally")
    welcome = bootstrap.get("welcome_slot")
    if welcome and welcome not in self._slot_set:
      self._warn(
          f"bootstrap.welcome_slot '{welcome}'"
          " not in slots")
    if welcome and welcome in self._slot_map:
      ws = self._slot_map[welcome]
      sources = _normalize_sources(
          ws.get("source", "user"))
      if "announce" not in sources:
        self._warn(
            f"bootstrap.welcome_slot '{welcome}'"
            " is not an announce slot")
    if not bootstrap.get("tool") and not self._config.get("single_flow"):
      self._warn("bootstrap has no 'tool'")
    ptt = bootstrap.get("pass_through_on_transfer")
    if ptt is not None and not isinstance(ptt, bool):
      self._error(
          "bootstrap.pass_through_on_transfer must be a bool")
    # Nested typo guard: _check_unknown_keys only diffs top-level keys, so a
    # mis-keyed bootstrap field (e.g. reset_on_complte) is silently dropped by
    # the engine's .get() and the gate/welcome/reset never behaves as intended.
    unknown = set(bootstrap.keys()) - _VALID_BOOTSTRAP_KEYS
    if unknown:
      self._error(f"bootstrap has unknown keys: {sorted(unknown)}")

  def _check_gate_slot(self):
    """Validate that gate_slot references a known slot if present.

    gate_slot is typically filled by the Root Agent's bootstrap
    tool, so it may not exist in this DAG's slots list (warn).
    """
    gate_slot = self._config.get("gate_slot")
    if gate_slot is not None and not isinstance(gate_slot, str):
      # A non-string gate name can never match a slot, and cannot be hashed
      # into the slot set the comparison below uses.
      self._error(
          "gate_slot must be a slot name string,"
          f" got {type(gate_slot).__name__}")
      return
    bootstrap = self._config.get("bootstrap")
    boot = bootstrap if isinstance(bootstrap, dict) else {}
    # The gate is filled by the bootstrap setter when gate_slot == bootstrap.slot
    # and a bootstrap tool exists — the standard framework pattern, not a problem.
    # Single-flow counts as framework-filled: the engine self-seeds the gate on
    # entry, so a tool-less gate_slot == bootstrap.slot is expected, not external.
    gate_filled_by_bootstrap = (
        (gate_slot == boot.get("slot") and bool(boot.get("tool")))
        or bool(self._config.get("single_flow")))
    if (gate_slot and gate_slot not in self._slot_set
        and not gate_filled_by_bootstrap):
      self._warn(
          f"gate_slot '{gate_slot}' not in slots"
          " — may be filled externally")

  def _check_single_flow(self):
    """Validate the single-flow (gate-less standalone) contract.

    A standalone single-flow agent has NO host/router. It declares a
    self-seeded gate (gate_slot == bootstrap.slot, NO bootstrap.tool) which the
    engine auto-seeds on entry, plus bootstrap.reset_on_complete:true so the
    scope resets cleanly after a terminal task. Fires ONLY when single_flow is
    True, so it can never touch gated configs.
    """
    if self._config.get("single_flow") is not True:
      return
    bootstrap = self._config.get("bootstrap")
    boot = bootstrap if isinstance(bootstrap, dict) else {}
    if boot.get("tool"):
      self._error(
          "single_flow agent has no router — bootstrap must NOT declare a"
          " 'tool' (the engine self-seeds the gate)")
    gate_slot = self._config.get("gate_slot")
    if not gate_slot or not boot.get("slot") or gate_slot != boot.get("slot"):
      self._error(
          "single_flow requires a self-seeded gate: gate_slot and"
          " bootstrap.slot must both be present and equal")
    has_terminal = any(
        t.get("terminal") for t in self._tasks)
    if has_terminal and boot.get("reset_on_complete") is not True:
      self._error(
          "single_flow with a terminal task requires"
          " bootstrap.reset_on_complete:true to reset the scope")
    auto_seed = boot.get("auto_seed")
    if auto_seed is not None and not isinstance(auto_seed, str):
      self._error("bootstrap.auto_seed must be a string")


  def _check_steer_back(self):
    """Validate steer_back thresholds and ordering.

    Thresholds must be ordered (soft <= hard <= escalate) and
    must be ints. Non-int values cause TypeError at runtime.
    """
    sb = self._config.get("steer_back")
    if not sb:
      return
    if not isinstance(sb, dict):
      self._error("'steer_back' must be a dict")
      return
    for key in ("soft_after", "hard_after", "escalate_after"):
      val = sb.get(key)
      if val is not None and not isinstance(val, int):
        self._error(
            f"steer_back.{key} must be an int")
    soft = sb.get("soft_after", 2)
    hard = sb.get("hard_after", 4)
    escalate = sb.get("escalate_after", 6)
    if (isinstance(soft, int) and isinstance(hard, int)
        and isinstance(escalate, int)):
      if not (soft <= hard <= escalate):
        self._error(
            "steer_back ordering violated:"
            f" soft_after ({soft}) <= hard_after ({hard})"
            f" <= escalate_after ({escalate})")
    # Cancellation is a first-class concern: the framework cancel tool fires it
    # and the top-level 'cancel' block customizes the disposition. steer_back has
    # no cancel keys; reject cancel_tool/cancel_say here so a config that sets
    # them fails loudly instead of configuring a cancel that never fires.
    if sb.get("cancel_tool") is not None or sb.get("cancel_say") is not None:
      self._error(
          "steer_back.cancel_tool/cancel_say are not supported; cancellation is"
          " the framework cancel tool, customized by the top-level 'cancel' block")
    on_exhaust = sb.get("on_exhaust")
    if on_exhaust is not None:
      self._check_on_exhaust(
          on_exhaust, "steer_back.on_exhaust")
    # Nested typo guard. cancel_tool/cancel_say are subtracted — they have the
    # dedicated "not supported" diagnostic above, so this diff would double-report.
    unknown = (set(sb.keys()) - _VALID_STEER_BACK_KEYS
               - {"cancel_tool", "cancel_say"})
    if unknown:
      self._error(f"steer_back has unknown keys: {sorted(unknown)}")

  def _check_no_input(self):
    """Validate the optional top-level 'no_input' silence policy.

    Flow-level only (there is no per-slot no_input): the B2 silence reprompt
    ladder + on_exhaust, applied to whichever user slot is being asked. Plain
    no-input uses `reprompts` (spoken); when the caller asked to hold (speech
    matched `hold_phrases`) the ladder uses `hold_reprompts` (empty = silent tick).
    Shape: {reprompts: [...], hold_reprompts: [...], hold_phrases: [...], on_exhaust}.
    """
    ni = self._config.get("no_input")
    if ni is None:
      return
    if not isinstance(ni, dict):
      self._error("'no_input' must be a dict")
      return
    for key in ("reprompts", "hold_reprompts", "hold_phrases", "hold_vetoes"):
      val = ni.get(key)
      if val is None:
        continue
      if not isinstance(val, list):
        self._error(f"no_input.{key} must be a list")
        continue
      for i, item in enumerate(val):
        if not isinstance(item, str):
          self._error(f"no_input.{key}[{i}] must be a string")
    hold_ack = ni.get("hold_ack")
    if hold_ack is not None:
      if not isinstance(hold_ack, str):
        self._error("no_input.hold_ack must be a string")
      elif not ni.get("hold_phrases"):
        # Nothing can ever match, so the ack is dead config that reads as though
        # holds were handled. The engine's hold detection is driven entirely by
        # hold_phrases.
        self._error("no_input.hold_ack is set but no_input.hold_phrases is "
                    "empty, so no utterance can ever trigger it")
    on_exhaust = ni.get("on_exhaust")
    if on_exhaust is not None:
      # The say-string type check now lives in _check_on_exhaust (applied at every
      # call site); the inline check here was removed to avoid a duplicate error.
      self._check_on_exhaust(on_exhaust, "no_input.on_exhaust",
                             allow_component=True, allow_open_slot=True)
    unknown = set(ni.keys()) - _VALID_NO_INPUT_KEYS
    if unknown:
      self._error(f"no_input has unknown keys: {sorted(unknown)}")

  def _check_answer(self):
    """Validate the optional top-level 'answer' free-response policy.

    Flow-level only (a LIST of grounded, intent-scoped free-response configs), a
    sibling of steer_back/no_input: on an off-menu, on-intent turn the engine
    hands the model the intent's grounding plus a whitelisted read/compute tool
    surface and lets it compose a reply (`answer_directive`), then leaves the DAG
    UNCHANGED so the very next cue-match still routes to the rails. Each entry:
      {name, scope, instruction (non-empty strings), max_turns (int > 0),
       allow_math (bool, optional), grounds (slots/session vars) and/or tools
       (DECLARED tool names), condition (optional), requires (optional slots)}.
    The DSL guarantees at least one of grounds/tools; it is re-checked here.

    Non-advancing invariant: the answer turn fills NO slot, so a setter/slot-fill
    key is an error — a config that fills a slot here would move the DAG the turn
    must never touch. Commit tools stay OFF the whitelist (a lint WARN nudges the
    author when a whitelisted name looks mutating), so a commitment via this node
    is structurally impossible.
    """
    policy = self._config.get("answer")
    if policy is None:
      return
    if not isinstance(policy, list):
      self._error("'answer' must be a list of answer configs")
      return
    for i, entry in enumerate(policy):
      ctx = f"answer[{i}]"
      if not isinstance(entry, dict):
        self._error(f"{ctx} must be a dict")
        continue
      name = entry.get("name")
      if isinstance(name, str) and name.strip():
        # Prefer the authored name in messages once we know it is usable.
        ctx = f"answer '{name}'"
      for key in ("name", "scope", "instruction"):
        val = entry.get(key)
        if not isinstance(val, str) or not val.strip():
          self._error(f"{ctx} {key} must be a non-empty string")
      # Bounded caller-Q&A budget; bool is an int subclass, so exclude it.
      max_turns = entry.get("max_turns")
      if (not isinstance(max_turns, int) or isinstance(max_turns, bool)
          or max_turns <= 0):
        self._error(f"{ctx} max_turns must be an int > 0")
      # allow_math is optional; a truthy non-bool is the usual footgun (a "false"
      # string is truthy, so the model would do arithmetic the author disabled).
      allow_math = entry.get("allow_math")
      if allow_math is not None and not isinstance(allow_math, bool):
        self._error(
            f"{ctx} allow_math must be a bool,"
            f" got {type(allow_math).__name__}")
      # Grounding surface: at least one of grounds/tools, each a list of names.
      grounds = entry.get("grounds")
      if grounds is not None and not isinstance(grounds, list):
        self._error(f"{ctx} grounds must be a list of slot/var names")
        grounds = None
      tools = entry.get("tools")
      if tools is not None and not isinstance(tools, list):
        self._error(f"{ctx} tools must be a list of declared tool names")
        tools = None
      if not grounds and not tools:
        self._error(
            f"{ctx} must set at least one of 'grounds' or 'tools' — an answer"
            " turn with no grounding can only invent its reply")
      # grounds reference real slots / session vars — the same slot_set mechanism
      # a task's inputs/requires use (a grounding var captured via ground_key is a
      # declared slot, so it is in self._slot_set).
      for g in (grounds or []):
        if not isinstance(g, str) or not g.strip():
          self._error(f"{ctx} grounds entry must be a non-empty string")
        elif g not in self._slot_set:
          self._error(f"{ctx} grounds references unknown slot/var '{g}'")
      # tools reference DECLARED tools — the same check as a task's `tool` (only
      # meaningful when the agent tool list was supplied). An undeclared name is
      # an ERROR; a declared-but-mutating-looking name is a lint WARNING.
      for t in (tools or []):
        if not isinstance(t, str) or not t.strip():
          self._error(f"{ctx} tools entry must be a non-empty string")
          continue
        if self._tool_unknown(t):
          self._error(f"{ctx} tool '{t}' not in agent tool list")
        if _ANSWER_MUTATING_TOOL_RE.search(t):
          self._warn(
              f"{ctx} whitelists tool '{t}', whose name looks like a COMMIT"
              " action (transfer/submit/enroll/change/waive/hangup/cancel/pay)."
              " The answer turn is non-advancing and should expose read/compute"
              " tools only — keep commit tools off the whitelist so they stay"
              " deterministic DAG cues that match first.")
      # condition — the same declarative grammar as slot/task/control conditions.
      condition = entry.get("condition")
      if condition is not None:
        if not isinstance(condition, dict):
          self._error(f"{ctx} condition must be a dict")
        else:
          for err in _validate_condition_spec(condition, self._condition_slots, ctx):
            self._error(err)
      # requires — known slot names (the same mechanism as a task's requires).
      requires = entry.get("requires")
      if requires is not None and not isinstance(requires, list):
        self._error(f"{ctx} requires must be a list of slot names")
        requires = None
      for req in (requires or []):
        if not isinstance(req, str) or not req.strip():
          self._error(f"{ctx} requires entry must be a non-empty string")
        elif req not in self._slot_set:
          self._error(f"{ctx} requires unknown slot '{req}'")
      # Non-advancing invariant: reject any slot-fill key with a dedicated
      # message, then subtract it from the unknown-key diff so it is not
      # double-reported (the pattern steer_back uses for cancel_tool/cancel_say).
      fill_keys = set(entry.keys()) & _ANSWER_SLOT_FILL_KEYS
      if fill_keys:
        self._error(
            f"{ctx} sets slot-fill key(s) {sorted(fill_keys)} — the answer turn"
            " is non-advancing and must not fill a slot; drop them and let the"
            " rails own every slot")
      unknown = set(entry.keys()) - _VALID_ANSWER_KEYS - _ANSWER_SLOT_FILL_KEYS
      if unknown:
        self._error(f"{ctx} has unknown keys: {sorted(unknown)}")

  def _check_speech(self):
    """Validate the optional top-level 'speech' improvisation policy.

    Shape: {improvise: [<class>, ...], improvise_style: "..."}. Each class names a
    family of canned utterance the model may reword instead of the framework
    speaking it verbatim. Absent, every utterance stays literal.

    The runtime guards are structural — a turn carrying a tool call, non-text
    response parts, or a terminal status keeps its literal line whatever the policy
    says — so the job here is to reject a policy that CANNOT do what it appears to,
    not to re-state those guards.
    """
    speech = self._config.get("speech")
    if speech is None:
      return
    if not isinstance(speech, dict):
      self._error("'speech' must be a dict")
      return
    unknown = set(speech.keys()) - {"improvise", "improvise_style"}
    if unknown:
      self._error(f"speech has unknown keys: {sorted(unknown)}")
    style = speech.get("improvise_style")
    if style is not None and not isinstance(style, str):
      self._error("speech.improvise_style must be a string")
    classes = speech.get("improvise")
    if classes is None:
      if style:
        self._error("speech.improvise_style is set but speech.improvise names no"
                    " classes, so nothing is ever improvised")
      return
    if not isinstance(classes, list):
      self._error("speech.improvise must be a list")
      return
    for i, cls in enumerate(classes):
      if not isinstance(cls, str):
        self._error(f"speech.improvise[{i}] must be a string")
      elif cls not in _IMPROVISE_CLASSES:
        self._error(f"speech.improvise[{i}] is not a known class: {cls!r}"
                    f" (expected one of {sorted(_IMPROVISE_CLASSES)})")
    if "filler" in classes:
      # Only means anything where a filler exists to reword. A filler can now be
      # authored in three places — on a task (spoken with the tool call), on a slot,
      # or flow-wide (both spoken as a partial preempt on a model turn) — so the
      # opt-in only reaches nothing when ALL THREE are absent.
      if not (any(t.get("filler_say") for t in self._tasks
                  if isinstance(t, dict))
              or any(s.get("filler_say") for s in self._slots
                     if isinstance(s, dict))
              or self._config.get("filler_say")):
        self._warn(
            "speech.improvise includes 'filler' but no task or slot has a"
            " `filler_say` and the flow sets no default, so there is no holding"
            " line for the model to reword.")
    if "control" in classes:
      # Of a control block's three spoken lines only `declined_say` can improvise.
      # `say` rides an already-terminated turn, and `confirm_say` is asked with the
      # control slot pending, which routes the turn down the readback protocol
      # instead of the directive fold. Both verified by driving them.
      blocks = [n for n in ("cancel", "escalate")
                if isinstance(self._config.get(n), dict)]
      if blocks and not any(self._config[n].get("declined_say") for n in blocks):
        self._warn(
            "speech.improvise includes 'control' but no control block has a"
            " `declined_say`, which is the only line the class reaches — `say` is"
            " spoken on a terminating turn and `confirm_say` while a value is"
            " pending readback, and both always stay literal.")

  def _check_flow_types(self):
    """Validate the optional top-level 'flow_types' list.

    Agent-specific list of flow-type names (e.g. the services a router offers).
    The framework's deterministic flow-switch backstop reads it from state to
    recognize "switch to <flow>" without hardcoding names. Must be a list of
    non-empty strings.
    """
    flow_types = self._config.get("flow_types")
    if flow_types is None:
      return
    if not isinstance(flow_types, list):
      self._error("'flow_types' must be a list")
      return
    for ft in flow_types:
      if not isinstance(ft, str) or not ft.strip():
        self._error(f"flow_types entries must be non-empty strings, got {ft!r}")

  def _check_control_block(self, name):
    """Validate a top-level terminal control block ('cancel' or 'escalate').

    A disposition-only customization of a terminal exit (the triggering tool is
    the framework control setter, not declared here); the engine synthesizes a
    passive control slot for every flow. The block is optional. Optional keys:
    'transfer_to' (the agent to return to — omit for a single-agent app, where the
    disposition is an escalated/cancelled end with no downstream agent), 'say',
    'outcome', 'confirm_say', 'hint' (strings), 'exit_status' (dict),
    'requires_readback' (bool — confirm before terminating), and 'response' (parts
    delivered on the disposition turn — how a telephony hand-off payload reaches the
    platform from this rail).
    """
    block = self._config.get(name)
    if block is None:
      return
    if not isinstance(block, dict):
      self._error(f"'{name}' must be a dict")
      return
    # Footgun: omitting transfer_to in a MULTI-agent app silently emits an
    # end_session disposition instead of transferring back to the parent agent.
    transfer_to = block.get("transfer_to")
    if transfer_to is not None and not isinstance(transfer_to, str):
      self._error(f"{name}.transfer_to must be a string")
    for key in ("say", "outcome", "confirm_say", "hint"):
      val = block.get(key)
      if val is not None and not isinstance(val, str):
        self._error(f"{name}.{key} must be a string")
    if not isinstance(block.get("verbatim", False), bool):
      self._error(f"{name}.verbatim must be a bool")
    # `condition` gates whether the disposition may run at all; when it is false
    # the request is dropped and `declined_say` (if any) explains why. Slot refs
    # are resolved against the declared slots, exactly as for a task condition —
    # a typo here would otherwise silently make the block always-available.
    #
    # `declared` is built before the `declined_say` check because a refusal REASON
    # carries a condition of its own and has to be resolved against the same set.
    declared = set(self._slot_set)
    # The engine synthesizes `<block>_declined` every time a control block's
    # condition refuses a request, so a gate may reference one although no slot
    # declares it. It is the only thing that makes "contain the first ask, honour
    # the second" expressible, and without this every such gate is an error.
    declared |= {"cancel_declined", "escalate_declined"}
    self._check_declined_say(name, block.get("declined_say"), declared)
    condition = block.get("condition")
    if condition is not None:
      if not isinstance(condition, dict):
        self._error(f"{name}.condition must be a dict")
      else:
        for ref in sorted(_extract_condition_slots(condition)):
          if ref not in declared:
            self._error(
                f"{name}.condition references undeclared slot '{ref}'")
    if block.get("declined_say") and condition is None:
      self._error(f"{name}.declined_say is set without {name}.condition, so it "
                  "can never be spoken")
    # `tool` on a control block is read by the derived GRAPH but never by the
    # runtime: the setter is the framework control tool (`cancel_flow` /
    # `transfer_to_human`), and _terminate_control consumes only say / outcome /
    # transfer_to / exit_status / requires_readback / response. So the diagram draws
    # an edge to a tool that is never called, and an author reasonably reads the
    # config as "escalating runs my tool" when it does nothing of the kind.
    if block.get("tool"):
      self._warn(
          f"{name}.tool ('{block['tool']}') is ignored at runtime — the "
          f"{name} setter is the framework control tool, and the block "
          "customizes only the disposition (say/outcome/transfer_to/"
          "exit_status/requires_readback/response). It affects the derived graph "
          f"only. To run a tool on {name}, fire it from the disposition's target "
          "instead.")
    requires_readback = block.get("requires_readback")
    if requires_readback is not None and not isinstance(
        requires_readback, bool):
      self._error(f"{name}.requires_readback must be a bool")
    exit_status = block.get("exit_status")
    if exit_status is not None and not isinstance(exit_status, dict):
      self._error(f"{name}.exit_status must be a dict")
    # `response` parts ride the disposition turn (engine `_terminate_control`). This is
    # how a telephony hand-off reaches the platform from the generic escalate rail:
    # without it the disposition is a friendly line and a bare end_session, so the
    # caller is told a person is coming and is then disconnected with nothing routing
    # them anywhere.
    self._check_control_block_response(name, block)
    if "tasks" in block:
      self._check_control_block_tasks(name, block["tasks"])
    if "component" in block:
      self._check_control_block_component(name, block)

  def _check_declined_say(self, name, dsay, declared):
    """Validate a control block's refusal line, in any of its three shapes.

    A line; a LADDER indexed by how many times the request has been refused, so a
    second ask does not hear the first answer again; or a list of REASONS —
    `{"when": <condition>, "say": ...}` entries evaluated in order, because a flow
    may refuse for more than one reason and the caller has to hear the right one.
    A list is read as reasons only when it actually carries a dict, so every
    existing ladder validates exactly as before.

    Every rule here rejects config that reads as though it does something. The
    sharpest is the unreachable entry: a reason with no `when` matches everything,
    so anything below it never speaks — and what an author puts below a catch-all
    is precisely their most specific wording.
    """
    if dsay is None or isinstance(dsay, str):
      return
    if not isinstance(dsay, list):
      self._error(f"{name}.declined_say must be a line, a ladder of lines, or a list"
                  " of {'when': ..., 'say': ...} reasons")
      return
    if not dsay:
      self._error(f"{name}.declined_say is an empty list, which says nothing on a"
                  f" refusal; omit it to make the block silent")
      return
    if not any(isinstance(x, dict) for x in dsay):
      if not all(isinstance(x, str) for x in dsay):
        self._error(f"{name}.declined_say list must hold strings")
      return
    catch_all = None
    for i, entry in enumerate(dsay):
      if isinstance(entry, str):
        unconditional, when = True, None
      elif isinstance(entry, dict):
        when = entry.get("when")
        unconditional = when is None
        say_ = entry.get("say")
        if not say_ or not (isinstance(say_, str)
                            or (isinstance(say_, list)
                                and all(isinstance(x, str) and x for x in say_))):
          self._error(f"{name}.declined_say[{i}] needs a `say` line or ladder —"
                      " matching it would otherwise refuse the caller in silence")
        if when is not None and not isinstance(when, dict):
          self._error(f"{name}.declined_say[{i}].when must be a condition dict")
          when = None
      else:
        self._error(f"{name}.declined_say[{i}] must be a line or a"
                    " {'when': ..., 'say': ...} reason")
        continue
      if isinstance(when, dict):
        # Resolved against the same slot set as the block's own gate: a typo here
        # silently never matches, and the caller hears the next reason down —
        # which is the wrong explanation, delivered with confidence.
        for ref in sorted(_extract_condition_slots(when)):
          if ref not in declared:
            self._error(f"{name}.declined_say[{i}].when references undeclared slot"
                        f" '{ref}'")
      if catch_all is not None:
        self._error(f"{name}.declined_say[{i}] can never be reached —"
                    f" declined_say[{catch_all}] has no `when`, so it matches every"
                    " refusal. Put the catch-all last.")
        return
      if unconditional:
        catch_all = i
    if catch_all is None:
      self._warn(
          f"{name}.declined_say has no catch-all reason (an entry without `when`),"
          " so a refusal none of its conditions match is silent — and a refusal is"
          " an answer to a direct question.")

  def _check_control_block_component(self, name, block):
    """Validate an `escalate.component` in-DAG deflection sub-flow reference.

    Routes a detected human request into an interactive, returnable child DAG
    instead of the fixed chain-then-terminate disposition. The child OWNS the
    disposition, so it cannot be combined with a chain (`tasks`), an in-app
    `transfer_to`, or a platform hand-off (`response`). Cross-config existence and
    the child's way-out are checked by the multi-config pass (`_check_component_refs`
    / `_check_component_child_terminal`)."""
    if name != "escalate":
      self._error(f"{name}.component is only valid on 'escalate', not '{name}'")
      return
    child = block.get("component")
    if not isinstance(child, str) or not child:
      self._error(f"{name}.component must be a non-empty child-config id string")
    for clash in ("tasks", "transfer_to"):
      if block.get(clash):
        self._error(
            f"{name}.component cannot be combined with {name}.{clash} — the child DAG"
            " owns the disposition (deflect-and-return, transfer, or hang up). Model"
            f" the {clash} inside the child instead.")
    if block.get("response") or block.get("channel_response"):
      self._error(
          f"{name}.component cannot be combined with a hand-off `response`/"
          "`channel_response` — model the hand-off on the child's terminal branch"
          " instead.")
    on_abort = block.get("on_abort", "skip")
    if on_abort not in ("skip", "fail_flow"):
      self._error(
          f"{name}.on_abort must be 'skip' or 'fail_flow', got {on_abort!r}")
    for io_key in ("inputs", "outputs"):
      io = block.get(io_key)
      if io is not None and not isinstance(io, dict):
        self._error(f"{name}.{io_key} must be a dict")

  def _check_control_block_response(self, name, block):
    """Validate a control block's `response` parts + the hand-off/transfer_to clash."""
    response = block.get("response")
    if response is None:
      return
    self._validate_response_list(response, f"'{name}'")
    if not isinstance(response, list):
      return
    if block.get("transfer_to") and any(
        isinstance(p, dict) and _handoff_shape(p.get("data")) for p in response):
      self._error(
          f"{name} carries both a hand-off payload and transfer_to"
          f" '{block['transfer_to']}'. transfer_to returns control to another agent"
          " inside this app and emits no session end; a hand-off ends the leg and"
          " gives the caller to the contact-center platform. Only one of them can"
          " happen, and which one is left to the client.")

  def _check_control_block_tasks(self, name, chain):
    """Validate an `escalate.tasks` pre-terminal chain.

    The chain runs BEFORE the disposition, under a walk restricted to its own
    members. Every rule here rejects a member that cannot survive that: one that
    ends the flow itself, one that descends into another DAG, or one the ordinary
    spine walk would have fired first anyway.
    """
    if name != "escalate":
      self._error(
          f"'{name}' does not support a task chain; only escalate.tasks runs"
          " tasks before its disposition")
      return
    if (not isinstance(chain, list)
        or not all(isinstance(t, str) and t.strip() for t in chain)):
      self._error("escalate.tasks must be a list of task names")
      return
    members = []
    for task_name in chain:
      task = self._task_map.get(task_name)
      if task is None:
        self._error(f"escalate.tasks names unknown task '{task_name}'")
        continue
      members.append(task)
      if task.get("terminal"):
        self._error(
            f"escalate.tasks member '{task_name}' is terminal; it would tear the"
            " flow down before the escalate disposition runs")
      if _is_component(task):
        self._error(
            f"escalate.tasks member '{task_name}' is a component; the escalate"
            " chain fires tools, not DAG descents")
      if task.get("awaits"):
        self._error(
            f"escalate.tasks member '{task_name}' awaits an ASYNCHRONOUS tool. The"
            " chain runs on the one rail that must never trap a caller, and an"
            " awaited result can be several turns out — hold the caller for it and"
            " the hand-off they asked for is exactly what they do not get. Build the"
            " summary synchronously.")
      if (not task.get("condition")
          and not task.get("inputs") and not task.get("requires")):
        self._error(
            f"escalate.tasks member '{task_name}' has no condition, inputs or"
            " requires, so the ordinary walk fires it before any escalate;"
            " gate it (e.g. flows.escalated())")
    chain_names = {t["name"] for t in members}
    chain_outputs = set()
    for task in members:
      chain_outputs |= set(_output_targets(task.get("outputs")))
    outside = set()
    for task in self._tasks:
      if task.get("name") not in chain_names:
        outside |= set(_output_targets(task.get("outputs")))
    for task in members:
      needed = set(_task_input_slots(task.get("inputs")))
      needed |= set(_name_list(task.get("requires")))
      unmet = sorted((needed & outside) - chain_outputs)
      if unmet:
        self._warn(
            f"escalate.tasks member '{task['name']}' needs {unmet}, produced only"
            " outside the chain — the caller can escalate before those land, and"
            " the chain would then be skipped")

  def _check_exit_status(self):
    """Validate top-level exit_status config.

    exit_status maps session state keys to slot names or literal
    strings. Only meaningful when a terminal task exists.
    """
    es = self._config.get("exit_status")
    if not es:
      return
    if not isinstance(es, dict):
      self._error("'exit_status' must be a dict")
      return
    for key, val in es.items():
      if not isinstance(val, str):
        self._error(
            f"exit_status['{key}'] must be a string"
            " (slot name or literal)")
    has_terminal = any(
        t.get("terminal") for t in self._tasks)
    if not has_terminal:
      self._warn(
          "exit_status is defined but no task is"
          " terminal — exit_status will never be used")

  def _check_event_mappings(self):
    """Validate top-level event_mappings config.

    event_mappings maps event names to dicts of
    mapping_key → value. The engine writes
    event_data[mapping_key] = value (E4542), then the
    prefill loop reads event_data[event_key or name] for
    each source:event slot (E4555). A mapping_key that is
    not some event slot's effective key never lands.
    """
    em = self._config.get("event_mappings")
    if not em:
      return
    if not isinstance(em, dict):
      self._error("'event_mappings' must be a dict")
      return
    # Index source:event slots by their EFFECTIVE key (event_key or name) — the
    # key the prefill loop reads from event_data (engine E4555). The engine writes
    # event_data[mapping_key] = value (E4542), so a mapping value only lands when
    # mapping_key equals some event slot's effective key.
    event_by_key = {}
    for slot in self._slots:
      if "event" in _normalize_sources(slot.get("source", "user")):
        key = slot.get("event_key") or slot.get("name")
        if isinstance(key, str):
          event_by_key.setdefault(key, slot)
    for event_name, mapping in em.items():
      if not isinstance(mapping, dict):
        self._error(
            f"event_mappings['{event_name}'] must be a dict")
        continue
      for slot_name, value in mapping.items():
        target = event_by_key.get(slot_name)
        if target is not None:
          # Resolved target: a value outside the slot's enum is silently dropped
          # by fill_slots (engine E4560), so the event never prefills.
          opts = _slot_enum_options(target)
          if opts and isinstance(value, str) and value not in opts:
            self._error(
                f"event_mappings['{event_name}']['{slot_name}'] value"
                f" '{value}' not in enum {sorted(opts)}")
          continue
        slot = self._slot_map.get(slot_name)
        if slot is None:
          self._warn(
              f"event_mappings['{event_name}'] targets"
              f" unknown slot '{slot_name}'")
        elif "event" not in _normalize_sources(slot.get("source", "user")):
          # Engine sets event_data[name] but the prefill loop only reads
          # source:event slots (E4551) → this target never prefills.
          self._error(
              f"event_mappings['{event_name}'] target '{slot_name}' must be a"
              f" source:event slot; never prefills")
        else:
          # source:event but its effective key (event_key) differs from its name:
          # engine writes event_data['{slot_name}'] yet prefill reads event_key.
          self._error(
              f"event_mappings['{event_name}'] key '{slot_name}' must equal"
              f" target event_key '{slot.get('event_key')}'")

  # ── Shared helpers ─────────────────────────────────────

  def _check_on_exhaust(self, exhaust, context, allow_component=False,
                        allow_open_slot=False, allow_fill=False,
                        allow_escalate=False, allow_reason_keys=False):
    """Validate on_exhaust structure and 'then' action.

    Args:
      exhaust: The on_exhaust config dict to validate.
      context: Human-readable label for error messages.
      allow_component: whether `component` (descend into a reusable offer/help
        child DAG) is meaningful at this call site. Only the task on_failure and
        no_input exhausts implement the reactive descent; it is rejected on
        slot-validation / steer_back exhausts. Cross-config existence + the
        "offer must terminate" rule are checked in the cross-config pass.
      allow_escalate: whether `escalate` (opt a `then` out of terminal escalation)
        is meaningful here. Only the TASK on_failure exhaust sets
        sm["status"] = "escalated", so it is the only site the engine reads it.

    'then' can be a string action name or a dict with tool+args.
    A dict without 'tool' produces {"name": None} at runtime.
    """
    if not isinstance(exhaust, dict):
      self._error(f"{context} must be a dict")
      return
    # fill: resolve the awaited slot and CONTINUE. Only a slot's own validation exhaust
    # has a slot to resolve, and it contradicts `then` (which ends the attempt).
    fill = exhaust.get("fill")
    if fill is not None:
      if not allow_fill:
        self._error(
            f"{context}.fill is only supported on a slot's validation.on_exhaust")
      elif not isinstance(fill, str):
        self._error(f"{context}.fill must be a string")
      elif exhaust.get("then") is not None:
        self._error(
            f"{context} sets both `fill` and `then` — they are contradictory"
            " dispositions (fill resolves the slot and continues; then ends the"
            " attempt). Use one.")
    # open_slot: arm an in-flow offer slot (the DAG advances to it). Keeps the offer
    # in the flow scope so a value spoken at the offer is captured. Only where the
    # engine implements the arming (task on_failure + no_input).
    open_slot = exhaust.get("open_slot")
    if open_slot is not None:
      if not allow_open_slot:
        self._error(
            f"{context}.open_slot is only supported on task on_failure and"
            f" no_input exhausts")
      elif not isinstance(open_slot, str):
        self._error(f"{context}.open_slot must be a string")
      elif open_slot not in self._slot_map:
        self._error(
            f"{context}.open_slot '{open_slot}' is not a declared slot")
    # component: alternatively, descend into a reusable child DAG (offer/help
    # Component). Cross-config existence + "offer must terminate" checked in S2.
    component = exhaust.get("component")
    if component is not None:
      if not allow_component:
        self._error(
            f"{context}.component is only supported on task on_failure and"
            f" no_input exhausts")
      elif not isinstance(component, str) or not component:
        self._error(f"{context}.component must be a non-empty string")
    # escalate: False keeps a `then` from terminating the flow (an in-flow pivot).
    # Only the task on_failure exhaust escalates, so it is the only site that reads it.
    escalate = exhaust.get("escalate")
    if escalate is not None:
      if not allow_escalate:
        self._error(
            f"{context}.escalate is only supported on a task's"
            f" on_failure.on_exhaust")
      elif not isinstance(escalate, bool):
        self._error(f"{context}.escalate must be a bool")
    then = exhaust.get("then")
    if then is not None:
      if isinstance(then, str):
        pass
      elif isinstance(then, dict):
        if not then.get("tool"):
          self._error(
              f"{context}.then dict missing 'tool'")
      else:
        self._error(
            f"{context}.then must be string or dict")
    say = exhaust.get("say")
    if isinstance(say, dict) and allow_reason_keys:
      # The reason-keyed form: one line per `error_code`, `_default` for the rest. Only
      # a task's on_failure has a failing tool to read a code from — a slot's validation
      # ladder and steer_back exhaust on the CALLER, so a dict there could never match
      # and would be rendered into a line the caller hears.
      if not say:
        self._error(f"{context}.say is an empty reason map")
      for code, line in say.items():
        if not isinstance(code, str) or not isinstance(line, str):
          self._error(
              f"{context}.say reason map must be {{error_code: line}} strings")
          break
    elif say is not None and not isinstance(say, str):
      self._error(
          f"{context}.say must be a string"
          + (" or a reason map keyed by error_code" if allow_reason_keys else ""))
    # An exhaust's `response` parts ride the same preempting turn as `say` (engine
    # `_resolve_response(exhaust, "response", ...)`), so they get the same checks as
    # any other response list — including the hand-off pairing rule, since an exhaust
    # is one of the two rungs a live-agent hand-off is normally emitted from.
    self._validate_response_list(exhaust.get("response"), context)
    channel_responses = exhaust.get("channel_responses")
    if channel_responses is not None:
      if not isinstance(channel_responses, dict):
        self._error(
            f"{context}.channel_responses must be a dict of channel -> response"
            f" parts, got {type(channel_responses).__name__}")
      else:
        for chan, parts in channel_responses.items():
          self._validate_response_list(
              parts, f"{context} channel_responses[{chan}]")
    end_conversation = exhaust.get("end_conversation")
    if end_conversation is not None and not isinstance(
        end_conversation, bool):
      self._error(f"{context}.end_conversation must be a bool")
    # Per-call unknown-key diff. open_slot/component are dropped from the allowed
    # set where their allow flag is False; but the site-gating branches above
    # already reported such a key with a specific message, so exclude it here to
    # avoid a duplicate diagnostic.
    allowed = set(_VALID_ON_EXHAUST_KEYS)
    already = set()
    if not allow_open_slot:
      allowed.discard("open_slot")
      if open_slot is not None:
        already.add("open_slot")
    if not allow_component:
      allowed.discard("component")
      if component is not None:
        already.add("component")
    if not allow_escalate:
      allowed.discard("escalate")
      if escalate is not None:
        already.add("escalate")
    unknown = set(exhaust.keys()) - allowed - already
    if unknown:
      self._error(f"{context} has unknown keys: {sorted(unknown)}")

  def _iter_channel_response_overrides(self):
    """Yield (context, field_name, override) for every channel_* response
    override present across slots, tasks (+ on_failure), and the config.

    The engine resolves these via _resolve_response (~E842): the base
    "response" field pairs with "channel_responses", every other field F with
    "channel_<F>" (channel_then_response, channel_retry_response,
    channel_readback_response). Each override is a dict {channel: [response
    parts]} and channel_overrides.get(channel) is called at ~E863, so the shape
    matters. Only present (non-None) keys are yielded, so an absent override is
    inert.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      for field_name in ("channel_responses", "channel_ask_variants"):
        override = slot.get(field_name)
        if override is not None:
          yield (f"Slot '{name}'", field_name, override)
    for task in self._tasks:
      name = _entry_name(task)
      for field_name in ("channel_then_response", "channel_then_say_variants"):
        override = task.get(field_name)
        if override is not None:
          yield (f"Task '{name}'", field_name, override)
      on_failure = task.get("on_failure")
      if isinstance(on_failure, dict):
        override = on_failure.get("channel_retry_response")
        if override is not None:
          yield (f"Task '{name}' on_failure", "channel_retry_response",
                 override)
    override = self._config.get("channel_readback_response")
    if override is not None:
      yield ("Config", "channel_readback_response", override)

  def _check_response_parts(self):
    """Validate response part types and required fields.

    Each part must have a 'type' field. Payload parts need 'data'
    to be a dict.
    """
    for slot in self._slots:
      name = _entry_name(slot)
      self._validate_response_list(
          slot.get("response"), f"Slot '{name}'")
      # end_conversation only fires on a terminal (end_session / transfer) part;
      # on any other slot it is inert.
      if slot.get("end_conversation") and not any(
          isinstance(p, dict) and p.get("type") in ("end_session", "transfer")
          for p in _as_seq(slot.get("response"))):
        self._warn(
            f"Slot '{name}' has end_conversation but no end_session/transfer"
            " part — the flag is inert here")
    for task in self._tasks:
      name = _entry_name(task)
      self._validate_response_list(
          task.get("then_response"), f"Task '{name}'")
      on_failure = task.get("on_failure", {})
      if isinstance(on_failure, dict):
        self._validate_response_list(
            on_failure.get("retry_response"),
            f"Task '{name}' on_failure")
    # channel_* overrides must each be a dict {channel: [parts]}. A non-dict
    # override crashes _resolve_response at channel_overrides.get(channel)
    # (~E863, AttributeError) on any channel-bearing (voice) turn; a per-channel
    # non-list value is fed straight to _substitute_response and renders
    # malformed. Per-channel parts get the SAME _validate_response_list checks as
    # a base response. A None channel value is a legitimate "fall back to base"
    # (engine: None or definition.get(field)) and passes, since
    # _validate_response_list returns early on None.
    for context, field_name, override in (
        self._iter_channel_response_overrides()):
      if not isinstance(override, dict):
        self._error(
            f"{context} {field_name} must be a dict of channel -> response"
            f" parts, got {type(override).__name__}")
        continue
      for chan, parts in override.items():
        self._validate_response_list(
            parts, f"{context} {field_name}[{chan}]")

  def _validate_response_list(
      self, response: Any, context: str,
  ):
    """Validate a list of response parts."""
    if response is None:
      return
    if not isinstance(response, list):
      self._error(f"{context} response must be a list")
      return
    for i, part in enumerate(response):
      if not isinstance(part, dict):
        self._error(
            f"{context} response[{i}] must be a dict")
        continue
      rp_type = part.get("type")
      if not rp_type:
        self._error(
            f"{context} response[{i}] missing 'type'")
      elif rp_type not in _VALID_RESPONSE_TYPES:
        self._warn(
            f"{context} response[{i}] type"
            f" '{rp_type}' not standard")
      if rp_type == "payload" and "data" in part:
        if not isinstance(part["data"], dict):
          self._error(
              f"{context} response[{i}] payload"
              " 'data' must be a dict")
      if rp_type == "audio" and not part.get("audioUri"):
        self._error(
            f"{context} response[{i}] audio part"
            " requires a non-empty 'audioUri'")
      # Conditional response part (inline if/else): validate the same DSL as a
      # slot/task condition so a per-part condition can never reference an unknown
      # slot or ship an uncompilable lambda.
      self._validate_part_condition(
          part.get("condition"), f"{context} response[{i}]")
    self._check_handoff_pairing(response, context)

  def _check_handoff_pairing(self, response, context):
    """A telephony hand-off payload must carry the `end_session` that ends the leg.

    The pair is one act: the payload asks the contact-center platform to route the
    caller, and the `end_session` is what actually gives up the call so the platform
    can. Half of it is not half a hand-off — it is a caller stranded on a line nobody
    is coming to, which is the live defect this check exists for.

    Everything here keys on a RECOGNIZED vendor shape (`_handoff_shape`), so a payload
    part carrying a card, chips or an app's own data is untouched.
    """
    for i, part in enumerate(response):
      if not isinstance(part, dict) or part.get("type") != "payload":
        continue
      shape = _handoff_shape(part.get("data"))
      if shape is None:
        continue
      label, is_escalation, missing = shape
      where = f"{context} response[{i}]"
      if missing:
        self._error(
            f"{where} is a {label} hand-off payload missing {missing} — the platform"
            " reads those to decide where the caller goes, so the hand-off either"
            " fails or lands in the wrong queue")
      end = None
      for later in response[i + 1:]:
        if isinstance(later, dict) and later.get("type") == "end_session":
          end = later
          break
      if end is None:
        self._error(
            f"{where} is a {label} hand-off payload with no 'end_session' part after"
            " it. The payload asks the platform to route the caller, but nothing gives"
            " up the leg — the agent keeps the call and the caller waits for a person"
            " who never arrives. Emit both as a unit (flows.handoff() does).",
            code=Codes.HANDOFF_PAYLOAD_UNPAIRED)
        continue
      cond = part.get("condition")
      if _condition_reads_payloads(cond):
        self._error(
            f"{where} gates a {label} hand-off payload on the 'payloads' capability."
            " Voice declares payloads:False, so this drops the hand-off on exactly the"
            ' surface it exists for. Gate it on {"surface": "voice"} (on BOTH parts),'
            " or leave it unconditional.")
      elif cond != end.get("condition"):
        self._error(
            f"{where} and its 'end_session' carry different conditions, so one can"
            " survive without the other: a filtered payload leaves the call ending"
            " with nothing routing the caller, and a filtered end leaves the platform"
            " escalating a call the agent still holds. Put the SAME condition on both"
            " (flows.handoff(surface=...) does).")
      if is_escalation and not end.get("escalated"):
        self._warn(
            f"{where} is a {label} live-agent escalation but its 'end_session' is not"
            " marked escalated:True, so the call reports as a plain transfer and every"
            " containment number that reads the flag is wrong")
      elif not is_escalation and end.get("escalated"):
        self._warn(
            f"{where} is a {label} platform transfer, not a live-agent escalation, but"
            " its 'end_session' is marked escalated:True — nobody was escalated, and"
            " the flag overstates every escalation count that reads it")
      if end.get("reason") != _HANDOFF_REASON:
        self._warn(
            f"{where} hands the caller to {label} but its 'end_session' reason is"
            f" {end.get('reason')!r}; a hand-off is a {_HANDOFF_REASON!r} — the call"
            " did not finish here, it left for another system")

  def _validate_part_condition(self, cond, context):
    """Validate an optional response-part ``condition`` (inline if/else).

    Same DSL and checks as a slot/task condition (`_check_slot_conditions`):
    a declarative dict is structurally validated against the slot set; a lambda
    string is syntax-checked via compile(); anything else errors. None/callable
    (already compiled) pass.
    """
    if cond is None or callable(cond):
      return
    if isinstance(cond, dict):
      for err in _validate_condition_spec(cond, self._condition_slots, context):
        self._error(err)
    elif isinstance(cond, str):
      self._validate_lambda_condition(cond, context)
    else:
      self._error(
          f"{context} condition must be dict, callable or string,"
          f" got {type(cond).__name__}")

  def _check_response_text_coverage(self):
    """Check that slots and tasks have text to display.

    Payload-only responses work in rich clients but produce no
    visible output on text-only channels. Having neither text nor
    payload nor audio means the turn will be completely silent.
    """
    def _has_text_part(response):
      if not isinstance(response, list):
        return False
      return any(
          isinstance(r, dict) and r.get("type") == "text"
          for r in response
      )

    def _has_payload_part(response):
      if not isinstance(response, list):
        return False
      return any(
          isinstance(r, dict) and r.get("type") in ("payload", "audio")
          for r in response
      )

    def _has_terminal_part(response):
      # A transfer or end_session part IS the turn's purpose: control hands off to
      # another agent (who then speaks) or the session ends. Such a turn is
      # legitimately text-less — e.g. a seamless internal agent-to-agent transfer
      # should NOT speak "connecting you now" — so it is not "silent" in the sense
      # this check guards against.
      if not isinstance(response, list):
        return False
      return any(
          isinstance(r, dict) and r.get("type") in ("transfer", "end_session")
          for r in response
      )

    for slot in self._slots:
      name = _entry_name(slot)
      source = slot.get("source", "")
      sources = (
          source if isinstance(source, list) else [source]
      )
      response = _as_seq(slot.get("response"))
      if not response:
        continue
      if _has_terminal_part(response):
        continue  # transfer/end_session turn — intentionally may carry no text
      is_announce = "announce" in sources
      has_text = (
          bool(slot.get("message")) if is_announce
          else bool(slot.get("ask"))
      )
      has_text = has_text or _has_text_part(response)
      has_payload = _has_payload_part(response)
      if not has_text and not has_payload:
        self._error(
            f"Slot '{name}' response has neither text"
            " nor payload — turn will be silent")
      elif not has_text:
        self._warn(
            f"Slot '{name}' response has no text —"
            " payload-only responses won't display"
            " on text-only channels")

    for task in self._tasks:
      name = _entry_name(task)
      has_text = bool(task.get("then_say"))
      then_response = task.get("then_response", [])
      if not then_response and has_text:
        continue
      if not then_response and not has_text:
        continue
      if _has_terminal_part(then_response):
        continue  # transfer/end_session turn — intentionally may carry no text
      has_text = has_text or _has_text_part(then_response)
      has_payload = _has_payload_part(then_response)
      if not has_text and not has_payload:
        self._error(
            f"Task '{name}' then_response has neither"
            " text nor payload — turn will be silent")
      elif not has_text:
        self._warn(
            f"Task '{name}' then_response has no text"
            " — payload-only responses won't display"
            " on text-only channels")

  def _check_format_string_placeholders(self):
    """Warn on format string placeholders referencing unknown names.

    Format strings like ask, message, then_say, then_directive and the text
    parts of a slot `response` / task `then_response` use {slot_name}
    placeholders. Warn-only since some come from task outputs (the all_known
    pool over-approximates the names available at render time).
    """
    all_known = self._slot_set | {"success", "error"}
    for task in self._tasks:
      for sn in _output_targets(task.get("outputs")):
        all_known.add(sn)
      for key in _task_outputs(task):
        all_known.add(key)

    for slot in self._slots:
      name = _entry_name(slot)
      for field_name in ("ask", "message"):
        template = slot.get(field_name)
        if not template or not isinstance(template, str):
          continue
        fields = _extract_format_fields(template)
        for f in fields:
          if f not in all_known:
            self._warn(
                f"Slot '{name}' {field_name} references"
                f" unknown placeholder '{{{f}}}'")
      for f in _response_text_placeholders(slot.get("response")):
        if f not in all_known:
          self._warn(
              f"Slot '{name}' response references"
              f" unknown placeholder '{{{f}}}'")

    for task in self._tasks:
      name = _entry_name(task)
      for field_name in ("then_say", "then_directive"):
        template = task.get(field_name)
        if not template or not isinstance(template, str):
          continue
        fields = _extract_format_fields(template)
        for f in fields:
          if f not in all_known:
            self._warn(
                f"Task '{name}' {field_name} references"
                f" unknown placeholder '{{{f}}}'")
      for f in _response_text_placeholders(task.get("then_response")):
        if f not in all_known:
          self._warn(
              f"Task '{name}' then_response references"
              f" unknown placeholder '{{{f}}}'")

    # channel_* override text parts use the same {slot} substitution as any
    # response text (_substitute_response ~E809), so hold them to the same
    # known-name pool. Warn-only, matching the base placeholder pass. Skips
    # malformed overrides (already ERRORed in _check_response_parts) and
    # non-text parts.
    for context, field_name, override in (
        self._iter_channel_response_overrides()):
      if not isinstance(override, dict):
        continue
      for chan, parts in override.items():
        if not isinstance(parts, list):
          continue
        for part in parts:
          if not isinstance(part, dict) or part.get("type") != "text":
            continue
          text_val = part.get("text")
          if not isinstance(text_val, str):
            continue
          for f in _extract_format_fields(text_val):
            if f not in all_known:
              self._warn(
                  f"{context} {field_name}[{chan}] references"
                  f" unknown placeholder '{{{f}}}'")


class CrossConfigValidator:
  """Validates interactions between multiple DAG configs sharing one SM.

  Multiple DAG configs share a single state machine dict (sm) that
  persists across agent transfers. This validator catches cross-config
  failure modes that single-config validation cannot detect.

  Usage:
      configs = {"bella_notte": {...}, "takeout": {...}}
      result = CrossConfigValidator(configs).validate()
  """

  def __init__(self, configs: dict[str, dict[str, Any]], routing_tables=None):
    # A malformed child is REPORTED by the per-config pass; keep its id in the
    # map (so a component ref to it still resolves) but read it as an empty
    # config, rather than raise .get() on a None/list from any check below.
    self._configs = {
        cid: (cfg if isinstance(cfg, dict) else {})
        for cid, cfg in _as_map(configs).items()}
    self._routing_tables = _as_map(routing_tables)
    self._errors: list[str] = []
    self._warnings: list[str] = []
    self._diagnostics: list[dict] = []
    self._blockers: list[str] = []

  def validate(self) -> ValidationResult:
    """Run all cross-config checks and return results."""
    # Component cross-config checks (S2) run BEFORE the < 2 early-return
    # (hazard H-XGUARD): they need only self._configs, which is non-empty, so a
    # self-referential component (length-1 cycle) or a ref to a missing child in
    # a single-config map is still caught.
    self._check_component_refs()
    self._check_component_io()
    self._check_component_child_terminal()
    self._check_offer_component_terminates()
    self._check_repeated_component_refs()
    self._check_component_cycles_depth()
    self._check_routing_chain()
    if len(self._configs) < 2:
      self._finalize()
      return ValidationResult(
          valid=len(self._errors) == 0,
          errors=list(self._errors),
          warnings=list(self._warnings),
          diagnostics=list(self._diagnostics),
          blockers=list(self._blockers),
      )
    self._check_status_contamination()

    self._check_welcome_slot_shadow()
    self._check_shared_slot_no_condition()
    self._check_retry_counter_leakage()
    self._check_steer_back_counter_carryover()
    self._check_gate_slot_consistency()
    self._check_bootstrap_tool_consistency()
    self._check_control_block_transfer()
    self._finalize()
    return ValidationResult(
        valid=len(self._errors) == 0,
        errors=list(self._errors),
        warnings=list(self._warnings),
        diagnostics=list(self._diagnostics),
        blockers=list(self._blockers),
    )

  def _error(self, msg: str, code: str | None = None,
             anchor: dict | None = None, fix_id: str | None = None):
    self._errors.append(msg)
    self._diagnostics.append({
        "severity": "error", "message": msg,
        "code": code, "anchor": anchor, "fix_id": fix_id,
    })

  def _warn(self, msg: str, code: str | None = None,
            anchor: dict | None = None, fix_id: str | None = None):
    self._warnings.append(msg)
    self._diagnostics.append({
        "severity": "warning", "message": msg,
        "code": code, "anchor": anchor, "fix_id": fix_id,
    })

  def _blocker(self, msg: str, code: str | None = None,
               anchor: dict | None = None, fix_id: str | None = None):
    """Ship-blocker tier (needs_review): leaves valid (== len(errors)==0)
    unchanged; only shippable (valid and not blockers) flips to False."""
    self._blockers.append(msg)
    self._diagnostics.append({
        "severity": "needs_review", "message": msg,
        "code": code, "anchor": anchor, "fix_id": fix_id,
    })

  def _finalize(self):
    """Dedupe + stably order structured diagnostics; derive flat lists.

    Cross-config twin of DagConfigValidator._finalize. Reads the structured
    self._diagnostics populated by w3-diagnostics-infra, dedupes on
    (code, ref, message) first-seen, sorts by (severity_rank, code, ref),
    and rebuilds self._diagnostics + self._errors + self._warnings.
    No-op when infra is absent. Must be called before every return in
    validate() (the len<2 early return and the final return).
    """
    diags = getattr(self, "_diagnostics", None)
    if diags is None:
      return
    self._diagnostics, self._errors, self._warnings = (
        _dedupe_sort_diagnostics(diags))

  def _check_routing_chain(self):
    """Cross-config routing-chain integrity for a single-agent router bundle.

    routing_tables (flow_config_map / agent_config_map / intent_config_map /
    default_config_id / flow_to_agent) is the map before_agent._resolve_config_id
    (before_agent.py L120-158) uses to pick the active config each turn. A target
    that is not a bundled config, a router flow_type that resolves to no config, or
    a flow whose target gates on a different slot than the router silently mis-routes
    (unresolved -> falls through to the router / stale cached config, so the flow's
    DAG never runs). No-op when routing_tables is absent/empty -> byte-identical for
    every bundle that does not ship a router table.
    """
    tables = self._routing_tables
    if not tables:
      return
    bundle_ids = set(self._configs)
    flow_map = _routing_map(tables, "flow_config_map")
    agent_map = _routing_map(tables, "agent_config_map")
    intent_map = _routing_map(tables, "intent_config_map")
    flow_to_agent = _routing_map(tables, "flow_to_agent")
    default_id = tables.get("default_config_id")

    # (a) referential integrity: every config_id a table names must be bundled.
    #     (flow_to_agent values are AGENT names, not config_ids -> not checked here.)
    if default_id and default_id not in bundle_ids:
      self._error(
          f"default_config_id '{default_id}' not in bundled configs"
          f" {sorted(bundle_ids)} — router entry resolves to a missing config")
    for label, m in (("flow_config_map", flow_map),
                     ("agent_config_map", agent_map),
                     ("intent_config_map", intent_map)):
      for key, cid in m.items():
        if cid not in bundle_ids:
          self._error(
              f"{label} target '{cid}' (for '{key}') not in bundled configs"
              f" {sorted(bundle_ids)} — routing to it resolves to nothing")

    # Router = default_config_id's config; skip (b)/(c) if it is missing (already
    # flagged by (a)) so a dangling default does not cascade false positives.
    router_cfg = self._configs.get(default_id) if default_id else None
    if not isinstance(router_cfg, dict):
      return

    # (b) flow-type reachability: every router flow_type must resolve to a config,
    #     directly (flow_config_map) or via its agent (flow_to_agent ->
    #     agent_config_map). A route_cue is NOT required.
    for ft in (router_cfg.get("flow_types") or []):
      ft = str(ft).strip()
      if not ft:
        continue
      via_flow = ft in flow_map
      via_agent = ft in flow_to_agent and flow_to_agent[ft] in agent_map
      if not (via_flow or via_agent):
        self._error(
            f"router flow_type '{ft}' has no config: absent from flow_config_map"
            f" and not routed via flow_to_agent/agent_config_map — selecting it"
            f" leaves the caller on the router")

    # (c) target gate-slot alignment: the router gates on Gr; a flow target that
    #     gates on a different slot won't see the router's gate value set, so its
    #     DAG never activates. WARNING (a target may intentionally self-seed its
    #     gate). Guarded on both gates present; missing targets are caught by (a).
    router_gate = router_cfg.get("gate_slot")
    if router_gate:
      for ft, cid in flow_map.items():
        tgt = self._configs.get(cid)
        if not isinstance(tgt, dict):
          continue
        tgt_gate = tgt.get("gate_slot")
        if tgt_gate and tgt_gate != router_gate:
          self._warn(
              f"flow '{ft}' target '{cid}' gate_slot '{tgt_gate}' != router"
              f" gate_slot '{router_gate}' — the router's gate value won't"
              f" activate the target flow's DAG")

  def _check_control_block_transfer(self):
    """Multi-agent footgun: a cancel/escalate control block that omits
    transfer_to. transfer_to is optional (a single-agent app ends with an
    escalated/cancelled disposition), but in a multi-agent app the omission
    silently emits an end_session instead of returning to the parent agent.
    Only reachable with >= 2 configs (guarded by the caller)."""
    for config_id, config in self._configs.items():
      for name in ("cancel", "escalate"):
        block = config.get(name)
        if isinstance(block, dict) and not block.get("transfer_to"):
          self._warn(
              f"[{config_id}] '{name}' control block omits 'transfer_to' in a "
              f"multi-agent app: the disposition ends the session instead of "
              f"transferring back to the parent agent. Set 'transfer_to' if a "
              f"return is intended.",
              code=Codes.CONTROL_BLOCK_NO_TRANSFER,
              anchor={"kind": "field", "ref": name, "field": "transfer_to"})

  # ── Component cross-config checks (S2) ─────────────────────

  def _iter_component_tasks(self):
    """Yield (parent_config_id, task) for every component task across configs."""
    for config_id, config in self._configs.items():
      for task in _config_entries(config, "tasks"):
        # A non-string child id is reported per-config (_check_identifier_fields)
        # and cannot key the config map, so it never reaches a cross-config check.
        if _is_component(task) and isinstance(task.get("component"), str):
          yield config_id, task

  def _iter_exhaust_component_refs(self):
    """Yield (parent_config_id, context, child_id) for every on_exhaust that
    descends into a component — the no_input exhaust and each task on_failure
    exhaust. These are reactive descents (silence / lookup give-up), not walk-fired
    component tasks, so _iter_component_tasks does not see them."""
    for config_id, config in self._configs.items():
      ni = _as_map(config).get("no_input")
      if isinstance(ni, dict):
        comp = (ni.get("on_exhaust") or {}).get("component")
        if isinstance(comp, str) and comp:
          yield config_id, f"Config '{config_id}' no_input.on_exhaust", comp
      for task in _config_entries(config, "tasks"):
        exhaust = _as_map(_as_map(task.get("on_failure")).get("on_exhaust"))
        comp = exhaust.get("component")
        if isinstance(comp, str) and comp:
          name = _entry_name(task)
          yield config_id, (
              f"Config '{config_id}' task '{name}' on_failure.on_exhaust"), comp

  def _iter_escalate_component_refs(self):
    """Yield (parent_config_id, context, child_id) for every escalate control block
    that routes into an in-DAG deflection component. Like the exhaust refs these are
    reactive descents (a detected human request), not walk-fired component tasks — but
    UNLIKE them the child may deflect-and-RETURN to the parent (it need not end the
    conversation), so it is checked for existence and a way-out but NOT for
    conversation termination."""
    for config_id, config in self._configs.items():
      block = config.get("escalate")
      if isinstance(block, dict):
        comp = block.get("component")
        if isinstance(comp, str) and comp:
          yield config_id, f"Config '{config_id}' escalate", comp

  def _child_terminates_conversation(self, child_config: dict[str, Any]) -> bool:
    """True when a child config can END THE WHOLE CONVERSATION (not just its
    frame): an announce slot flagged `end_conversation` carrying an end_session /
    transfer part, or a terminal task flagged `end_conversation`. An offer/help
    component reached via on_exhaust MUST do this — a plain child terminal just
    frame-returns to the exhausted slot, which would loop the silence forever."""
    def _ends(parts):
      return any(isinstance(p, dict) and p.get("type") in ("end_session", "transfer")
                 for p in (parts or []))
    for slot in _config_entries(child_config, "slots"):
      if not slot.get("end_conversation"):
        continue
      # The terminating part may live on `response` OR on a channel-specific override
      # (`channel_responses`) — a child that only ends the call on, say, the voice
      # channel still terminates, so inspect the overrides too or this false-positives.
      if _ends(slot.get("response")):
        return True
      for parts in (slot.get("channel_responses") or {}).values():
        if _ends(parts):
          return True
    return any(t.get("terminal") and t.get("end_conversation")
               for t in _config_entries(child_config, "tasks"))

  def _check_offer_component_terminates(self):
    """Every component reached via an on_exhaust descent must terminate the
    conversation on its terminal branch(es). Replaces the old open_slot reselect
    safety-net: the reactive descent has no parent slot to fall back to, so a child
    that only frame-returns would re-ask the just-exhausted slot and loop."""
    for parent_id, context, child_id in self._iter_exhaust_component_refs():
      child = self._configs.get(child_id)
      if child is None:
        continue  # missing child already reported by _check_component_refs
      if not self._child_terminates_conversation(child):
        self._error(
            f"{context}.component '{child_id}' must END the conversation on its"
            " terminal branch(es) — add `end_conversation: True` to its"
            " terminating announce(s). Otherwise the offer frame-returns to the"
            " exhausted slot and the silence loops.")

  def _child_produced_keys(self, child_config: dict[str, Any]) -> set[str]:
    """Result keys a CHILD config produces: its tasks' output result_keys.

    A component's outputs ({child_result_key: parent_slot}) read the keys a child
    task writes into a slot via outputs ({result_key: child_slot}). The set of
    result_keys the child produces is the union of its tasks' output keys.
    """
    keys: set[str] = set()
    for task in _config_entries(child_config, "tasks"):
      keys |= set(_task_outputs(task).keys())
    return keys

  def _check_component_refs(self):
    """Each component task's 'component' ref resolves to a known config_id.

    Single-config blind spot is resolved HERE: a lone DagConfigValidator never
    checks the ref exists; this cross-config check does (and runs even for a
    1-config map, before the < 2 early-return).
    """
    for parent_id, task in self._iter_component_tasks():
      name = _entry_name(task)
      child_id = task.get("component")
      if not isinstance(child_id, str) or not child_id:
        continue
      if child_id not in self._configs:
        self._error(
            f"Config '{parent_id}' component task '{name}' references"
            f" unknown child config '{child_id}'")
    # on_exhaust component descents (no_input / task on_failure) must also resolve.
    for parent_id, context, child_id in self._iter_exhaust_component_refs():
      if child_id not in self._configs:
        self._error(
            f"{context}.component references unknown child config '{child_id}'")
    # escalate.component deflection descents must resolve too.
    for parent_id, context, child_id in self._iter_escalate_component_refs():
      if child_id not in self._configs:
        self._error(
            f"{context}.component references unknown child config '{child_id}'")

  def _check_component_child_terminal(self):
    """Every component's child config must have a way out — either a terminal task
    (-> _frame_return, returns to the parent) OR a conversation-ending terminal
    (`end_conversation`, ends the whole call, e.g. a leaf offer/help component). A
    child with neither reaches "all_done" with a live frame and stalls. Resolvable
    only cross-config, so it lives here (guarded by the child-exists lookup)."""
    for parent_id, task in self._iter_component_tasks():
      name = _entry_name(task)
      child_id = task.get("component")
      child = self._configs.get(child_id)
      if child is None:
        continue
      if (not any(t.get("terminal") for t in child.get("tasks", []))
          and not self._child_terminates_conversation(child)):
        self._error(
            f"Config '{parent_id}' component task '{name}' child config"
            f" '{child_id}' has no terminal task and does not end the"
            " conversation — a sub-flow needs a \"terminal\": True task to return"
            " to its parent, or an `end_conversation` terminal to end the call")
    # escalate.component deflection children need the same way-out (return OR end).
    for parent_id, context, child_id in self._iter_escalate_component_refs():
      child = self._configs.get(child_id)
      if child is None:
        continue
      if (not any(t.get("terminal") for t in child.get("tasks", []))
          and not self._child_terminates_conversation(child)):
        self._error(
            f"{context}.component child config '{child_id}' has no terminal task"
            " and does not end the conversation — a deflection sub-flow needs a"
            " \"terminal\": True task to return to its parent, or an"
            " `end_conversation` terminal to end/transfer the call")

  def _check_io_against_child(self, label, child_id, inputs, outputs):
    """Shared I/O check for any component reference (task or escalate.component).

    Input child-slot names (the binding VALUES — {parent_slot: child_slot}) must be
    slots in the CHILD config; output result_keys (the binding KEYS — {child_result_key:
    parent_slot}) must be produced by the CHILD. No-op when the child is missing (a
    missing child is reported by `_check_component_refs`)."""
    child = self._configs.get(child_id)
    if child is None:
      return
    child_slots = {s["name"] for s in _config_entries(child, "slots")
                   if isinstance(s.get("name"), str)}
    child_inputs = (list(inputs.values()) if isinstance(inputs, dict)
                    else _as_seq(inputs))
    for child_slot in child_inputs:
      if not isinstance(child_slot, str):
        continue  # reported per-config by _check_name_list_field
      if child_slot not in child_slots:
        self._error(
            f"{label} input maps to child slot '{child_slot}' not in child config"
            f" '{child_id}'")
    produced = self._child_produced_keys(child)
    for result_key in _as_map(outputs):
      if result_key not in produced:
        self._error(
            f"{label} output key '{result_key}' is not produced by child config"
            f" '{child_id}'")

  def _check_component_io(self):
    """Component inputs/outputs names match the CHILD config's surface — for both
    walk-fired component TASKS and `escalate.component` deflection references."""
    for parent_id, task in self._iter_component_tasks():
      name = _entry_name(task)
      self._check_io_against_child(
          f"Config '{parent_id}' component task '{name}'",
          task.get("component"), task.get("inputs", {}), task.get("outputs", {}))
    for config_id, config in self._configs.items():
      block = config.get("escalate")
      if isinstance(block, dict) and isinstance(block.get("component"), str):
        self._check_io_against_child(
            f"Config '{config_id}' escalate.component",
            block["component"], block.get("inputs", {}), block.get("outputs", {}))

  def _check_repeated_component_refs(self):
    """Mode-B repeated component: done_setter and element keys resolve to child.

    A repeated component's done_setter (repeated.until.done_setter) names a
    CHILD slot whose truthy value at frame-return ends collection; its
    `element` keys are CHILD slot names mapped to element fields. These resolve
    only where child configs are visible, so the single-config validator checks
    shape only and this cross-config pass checks the child-side names. Guarded
    by the child-exists lookup (a missing child is reported by
    _check_component_refs), so tool-less / offline single-config runs where the
    child is absent stay green.
    """
    for parent_id, task in self._iter_component_tasks():
      repeated = task.get("repeated")
      if not isinstance(repeated, dict):
        continue
      name = _entry_name(task)
      child_id = task.get("component")
      child = self._configs.get(child_id)
      if child is None:
        continue
      child_slots = {
          s["name"] for s in child.get("slots", []) if "name" in s
      }
      until = repeated.get("until")
      until = until if isinstance(until, dict) else {}
      done_setter = until.get("done_setter")
      if (isinstance(done_setter, str) and done_setter
          and done_setter not in child_slots):
        self._error(
            f"Config '{parent_id}' repeated component task '{name}'"
            f" done_setter '{done_setter}' is not a slot in child config"
            f" '{child_id}'")
      element = task.get("element")
      if isinstance(element, dict):
        for child_slot in element:
          if child_slot not in child_slots:
            self._error(
                f"Config '{parent_id}' repeated component task '{name}'"
                f" element key '{child_slot}' is not a slot in child config"
                f" '{child_id}'")

  def _check_component_cycles_depth(self):
    """Forbid cycles in the config-ref graph and cap call depth at 3.

    Node = config_id, edge = a component task's child ref. Self-reference is a
    length-1 cycle and is caught even in a 1-config map. Same visited/stack DFS
    shape as DagConfigValidator._check_circular_requires; the depth bound falls
    out of the recursion.
    """
    graph: dict[str, set[str]] = {}
    for parent_id, task in self._iter_component_tasks():
      child_id = task.get("component")
      if isinstance(child_id, str) and child_id:
        graph.setdefault(parent_id, set()).add(child_id)
    # on_exhaust descents are edges too (a self-referential offer -> offer loops).
    for parent_id, _context, child_id in self._iter_exhaust_component_refs():
      graph.setdefault(parent_id, set()).add(child_id)
    # escalate.component deflection descents are edges too. Unlike the other refs an
    # escalate child may deflect-and-RETURN (it need not end the conversation), so
    # nothing downstream forbids a self-referential (escalate.component == own id) or
    # A->B->A escalate cycle — only this graph catches it.
    for parent_id, _context, child_id in self._iter_escalate_component_refs():
      graph.setdefault(parent_id, set()).add(child_id)

    max_depth = 3
    cycle_reported: set[str] = set()
    depth_reported: set[str] = set()

    def walk(node, stack):
      for child in graph.get(node, ()):
        if child in stack:
          key = child
          if key not in cycle_reported:
            cycle_reported.add(key)
            self._error(
                f"Component config cycle detected involving '{child}'")
          continue
        path = stack + [child]
        # path length N nodes = N-1 edges = call depth N-1.
        if len(path) - 1 > max_depth and child not in depth_reported:
          depth_reported.add(child)
          self._error(
              f"Component call depth exceeds {max_depth} on path"
              f" {' -> '.join(path)}")
          continue
        walk(child, path)

    for root in list(graph.keys()):
      walk(root, [root])

  def _check_status_contamination(self):
    """Ensure configs with terminal tasks have reset_on_complete.

    Terminal tasks set sm.status='zombie'. Before_model_callback
    checks status AFTER config_id switch but does NOT reset it,
    so a subsequent config's engine never runs.
    """
    for config_id, config in self._configs.items():
      has_terminal = any(
          t.get("terminal") for t in _config_entries(config, "tasks")
      )
      if not has_terminal:
        continue
      bootstrap = config.get("bootstrap", {})
      if not isinstance(bootstrap, dict):
        bootstrap = {}
      if not bootstrap.get("reset_on_complete"):
        others = [c for c in self._configs if c != config_id]
        self._error(
            f"Config '{config_id}' has terminal tasks but"
            f" bootstrap.reset_on_complete is not True —"
            f" after completion, configs {others} will see"
            " status='zombie' and their engine will not run")

  def _check_welcome_slot_shadow(self):
    """Warn if announce slots with the same name exist in 2+ configs.

    Announce slots auto-fill filled[name]=True. The second config
    sees it as already filled and skips its announcement.
    reset_on_complete does NOT clear filled.
    """
    slot_announce_owners: dict[str, list[str]] = {}
    for config_id, config in self._configs.items():
      for slot in _config_entries(config, "slots"):
        name = slot.get("name")
        sources = _normalize_sources(
            slot.get("source", "user"))
        if isinstance(name, str) and name and "announce" in sources:
          slot_announce_owners.setdefault(
              name, []).append(config_id)
    for slot_name, owners in slot_announce_owners.items():
      if len(owners) > 1:
        self._warn(
            f"Announce slot '{slot_name}' in configs {owners}"
            " — second config's announcement will be skipped"
            f" because filled['{slot_name}'] persists")

  def _check_shared_slot_no_condition(self):
    """Warn if shared user slots lack scoping conditions.

    User-source slots sharing a name across configs risk unintended
    data reuse. A scoping condition prevents this.
    """
    slot_owners: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for config_id, config in self._configs.items():
      for slot in _config_entries(config, "slots"):
        name = slot.get("name")
        sources = _normalize_sources(
            slot.get("source", "user"))
        if isinstance(name, str) and name and "user" in sources:
          slot_owners.setdefault(name, []).append(
              (config_id, slot))
    for slot_name, entries in slot_owners.items():
      if len(entries) < 2:
        continue
      unconditioned = [
          cid for cid, s in entries if not s.get("condition")
      ]
      if len(unconditioned) == len(entries):
        configs = [cid for cid, _ in entries]
        self._warn(
            f"Slot '{slot_name}' shared by configs {configs}"
            " without scoping conditions — data filled by"
            " one config will be silently reused by the other")
      elif unconditioned:
        conditioned = [
            cid for cid, s in entries if s.get("condition")
        ]
        self._warn(
            f"Slot '{slot_name}' shared: {unconditioned} have"
            f" no condition, {conditioned} do — asymmetric"
            " scoping may cause unintended reuse")

  def _check_retry_counter_leakage(self):
    """Warn if shared slots both define max_retries.

    _retries is keyed by 'slot:{name}' and persists across config
    switches, so shared slots share retry budgets.
    """
    slot_retry_owners: dict[str, list[str]] = {}
    for config_id, config in self._configs.items():
      for slot in _config_entries(config, "slots"):
        name = slot.get("name")
        validation = slot.get("validation")
        if (isinstance(name, str) and name and validation
            and isinstance(validation, dict)
            and validation.get("max_retries")):
          slot_retry_owners.setdefault(
              name, []).append(config_id)
    for slot_name, owners in slot_retry_owners.items():
      if len(owners) > 1:
        self._warn(
            f"Slot '{slot_name}' has validation.max_retries"
            f" in configs {owners} — retry counter"
            f" 'slot:{slot_name}' carries over between them")

  def _check_steer_back_counter_carryover(self):
    """Warn if steer_back thresholds differ across configs.

    _steer_back_turns persists across config switches, so
    different thresholds can cause premature escalation.
    """
    steer_configs: dict[str, dict[str, Any]] = {}
    for config_id, config in self._configs.items():
      sb = config.get("steer_back")
      if sb and isinstance(sb, dict):
        steer_configs[config_id] = sb
    if len(steer_configs) < 2:
      return
    thresholds = set()
    for sb in steer_configs.values():
      thresholds.add((
          sb.get("soft_after", 2),
          sb.get("hard_after", 4),
          sb.get("escalate_after", 6),
      ))
    if len(thresholds) > 1:
      self._warn(
          "steer_back thresholds differ across configs"
          f" {list(steer_configs.keys())} —"
          " _steer_back_turns carries over and may trigger"
          " premature escalation in the receiving config")

  def _check_gate_slot_consistency(self):
    """Warn if configs use different gate_slot names.

    The Root Agent's bootstrap tool typically fills a single
    gate slot name, so differing names may leave a gate unfilled.
    """
    gate_slots: dict[str, list[str]] = {}
    for config_id, config in self._configs.items():
      gs = config.get("gate_slot")
      if gs and isinstance(gs, str):
        gate_slots.setdefault(gs, []).append(config_id)
    if len(gate_slots) > 1:
      self._warn(
          f"Different gate_slot names across configs:"
          f" {dict(gate_slots)} — Root Agent's bootstrap"
          " may only fill one of them")

  def _check_bootstrap_tool_consistency(self):
    """Warn if configs use different bootstrap tools.

    The after_tool callback's tool.name==bootstrap['tool'] check
    won't match for configs with a different bootstrap tool,
    so reset_on_complete can't fire.
    """
    bootstrap_tools: dict[str, list[str]] = {}
    for config_id, config in self._configs.items():
      bootstrap = config.get("bootstrap", {})
      if isinstance(bootstrap, dict):
        tool = bootstrap.get("tool")
        if tool and isinstance(tool, str):
          bootstrap_tools.setdefault(
              tool, []).append(config_id)
    if len(bootstrap_tools) > 1:
      self._warn(
          f"Different bootstrap tools across configs:"
          f" {dict(bootstrap_tools)} — reset_on_complete"
          " may not fire for configs with a different"
          " bootstrap tool than the one called by Root Agent")


# ── CES tool entry point ──────────────────────────────────


def _entry_error(message: str) -> dict[str, Any]:
  """The documented result shape carrying one entry-point type error."""
  return {
      "valid": False,
      "errors": [message],
      "warnings": [],
      "diagnostics": [{
          "severity": "error", "message": message,
          "code": None, "anchor": None, "fix_id": None,
      }],
      "blockers": [],
      "shippable": False,
  }


def validate_dag_config(
    input_data: dict[str, Any],
) -> dict[str, Any]:
  """Validate DAG config(s) for structural and cross-config issues.

  Args:
    input_data: Dict with either 'raw_config' (single config) or
      'all_configs' (dict of config_id to config for cross-config).

  Returns:
    Dict with 'valid', 'errors', 'warnings'. When all_configs is
    provided, also includes 'per_config' and 'cross_config' dicts.

    A malformed `input_data` is REPORTED in that same shape, never raised: the
    caller is an authoring surface, and a stack trace out of the linter tells
    the author nothing about their config.
  """
  if not isinstance(input_data, dict):
    return _entry_error(
        "input_data must be a dict with 'raw_config' or 'all_configs',"
        f" got {type(input_data).__name__}")
  all_configs = input_data.get("all_configs")
  available_tools = input_data.get("available_tools")
  if available_tools is not None and not isinstance(
      available_tools, (list, tuple, set, frozenset)):
    return _entry_error(
        "'available_tools' must be a list of tool names,"
        f" got {type(available_tools).__name__}")
  setter_sources = _as_map(input_data.get("setter_sources"))
  task_tool_sources = _as_map(input_data.get("task_tool_sources"))
  routing_tables = _as_map(input_data.get("routing_tables"))
  if all_configs:
    if not isinstance(all_configs, dict):
      return _entry_error(
          "'all_configs' must be a dict of config_id -> config,"
          f" got {type(all_configs).__name__}")
    per_config = {}
    combined_errors: list[str] = []
    combined_warnings: list[str] = []
    combined_blockers: list[str] = []
    for config_id, config in all_configs.items():
      r = DagConfigValidator(
          config, available_tools=available_tools,
          setter_sources=setter_sources,
          task_tool_sources=task_tool_sources,
      ).validate()
      per_config[config_id] = {
          "valid": r.valid,
          "errors": r.errors,
          "warnings": r.warnings,
          "diagnostics": list(getattr(r, "diagnostics", [])),
          "blockers": list(getattr(r, "blockers", [])),
          "shippable": getattr(r, "shippable", r.valid),
      }
      combined_errors.extend(
          f"[{config_id}] {e}" for e in r.errors)
      combined_warnings.extend(
          f"[{config_id}] {w}" for w in r.warnings)
      combined_blockers.extend(
          f"[{config_id}] {b}" for b in getattr(r, "blockers", []))
    cross = CrossConfigValidator(
        all_configs, routing_tables=routing_tables).validate()
    combined_errors.extend(cross.errors)
    combined_warnings.extend(cross.warnings)
    combined_blockers.extend(getattr(cross, "blockers", []))
    return {
        "valid": len(combined_errors) == 0,
        "errors": combined_errors,
        "warnings": combined_warnings,
        "blockers": combined_blockers,
        "shippable": len(combined_errors) == 0 and not combined_blockers,
        "per_config": per_config,
        "cross_config": {
            "valid": cross.valid,
            "errors": cross.errors,
            "warnings": cross.warnings,
        },
    }

  raw_config = input_data.get("raw_config", {})
  result = DagConfigValidator(
      raw_config, available_tools=available_tools,
      setter_sources=setter_sources,
      task_tool_sources=task_tool_sources,
  ).validate()
  return {
      "valid": result.valid,
      "errors": result.errors,
      "warnings": result.warnings,
      # Additive structured twin (backward-compatible): coded diagnostics, the
      # needs_review ship-blocker tier, and the derived shippable flag.
      "diagnostics": list(getattr(result, "diagnostics", [])),
      "blockers": list(getattr(result, "blockers", [])),
      "shippable": getattr(result, "shippable", result.valid),
  }
