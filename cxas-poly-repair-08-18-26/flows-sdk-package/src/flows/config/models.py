"""Canonical Pydantic data contracts for Slot Studio.

FROZEN (S0). This module is the single source of truth for every wire contract
in TDD section 5. Wave subtasks (S1-S11) consume these models but MUST NOT edit
them; a contract change requires an integrator amendment.

The models mirror `client/src/domain.ts` 1:1 (TDD section 5) and the framework's
key whitelists (validate_dag_config `_VALID_*_KEYS`). Where a config dict field
can hold a declarative dict OR a lambda *source string*, the lambda form is kept
as a plain `str` so the contract round-trips to JSON losslessly (TDD section 3.4).

Pydantic v2. Models permit extra keys on the open-ended config sub-objects
(payload `data`, response parts with `[k: str]: any`, sm snapshots) but are
strict where the framework whitelist is fixed.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Framework key whitelists (verbatim mirror of validate_dag_config
# `_VALID_CONFIG_KEYS` / `_VALID_SLOT_KEYS` / `_VALID_TASK_KEYS`, TDD section 5.1).
# Exposed as constants so wave code can validate against the framework surface
# without re-reading the framework source. NOT used to restrict the models below:
# the authored Config/Slot/Task contracts intentionally model the *extended*
# document (control blocks, flow types, router) that the engine derives, per the
# TDD section 5 TS document model. These constants are the framework's accepted
# raw-dict keys.
# ---------------------------------------------------------------------------

FRAMEWORK_VALID_CONFIG_KEYS: frozenset[str] = frozenset({
    "slots", "tasks", "gate_slot", "correction_tool", "bootstrap",
    "steer_back", "cancel", "confirm_transition_prefix", "exit_status",
    "event_mappings", "readback_response", "channel_readback_response",
    "shared_slots",
    # Polymorphic surfaces: the app's delivery surfaces and which one an absent or
    # unrecognized channel resolves to. Both optional — the engine ships built-in
    # `voice` and `chat`.
    "surfaces", "default_surface",
    # Lowered from App(variable_maps=[...]): the ordered session-variable shapes the
    # generated ingress callback pre-fills slots from before the conversation starts.
    "variable_maps",
    # False suppresses the SYNTHESIZED cancel/escalate control slot and its framework
    # tool. Default `not is_router`.
    "cancelable", "escalatable",
    # Which classes of canned utterance the model may reword (engine
    # _maybe_improvise). Absent, every utterance is spoken verbatim.
    "speech",
    # Flow-level default latency filler, used on any turn handed to the model that
    # does not carry its own. See Config.filler_say.
    "filler_say",
})

FRAMEWORK_VALID_SLOT_KEYS: frozenset[str] = frozenset({
    "name", "source", "ask", "setter", "setter_field", "requires",
    "validation", "requires_readback", "readback_fmt", "hint",
    "response", "channel_responses", "message", "preempt", "condition",
    "shared", "validate_against", "event_key",
    # Value policy: reject a sentinel, default what nothing produced, publish back out.
    "reject", "default", "publish",
    # Conversation-design: DTMF keypad mapping (B1). Silence (`no_input`) is
    # FLOW-LEVEL only, never per-slot (see Config.no_input).
    "dtmf_map",
    # Latency filler for the turn this slot is collected on, when that turn is handed
    # to the model rather than dispatching a tool. Same field as a task's.
    "filler_say",
    # Conversation-design: `option_cues` (text twin of dtmf_map) + `validation_rules`
    # (lowered to setter checks). `kind:"intent"` marks a first-class intent slot
    # (enum-selecting op) — see validate_dag_config._check_intent_slots. `passive` marks a
    # never-asked user slot the model/cues fill (a model-classified router intent).
    "option_cues", "validation_rules", "kind", "passive",
    # `switchable` lets a caller change the subject mid-flow: an already-filled intent
    # value is re-decided on an unambiguous later cue match, and everything derived from
    # it is cleared (engine `_abandon_journey`).
    "switchable",
    # `cue_priority` ("unique" | "first") is the tiebreak when an utterance matches more
    # than one `option_cues` value; default "unique" keeps the historical "fill nothing".
    "cue_priority",
    "multi_fill",
    # Polymorphic surfaces: alternative WORDINGS of the ask, each gated by a
    # capability condition. A surviving variant REPLACES the ask — unlike
    # `response`, which the client appends to what the model said.
    "ask_variants", "channel_ask_variants",
    # Preempt the FIRST presentation of this slot's readback (engine text straight to
    # TTS, model bypassed). Pair with `readback_fmt`.
    "readback_verbatim",
    # Suppress this slot's readback when the staged value is digit-identical to one of
    # the NAMED slots' already-filled values (engine `_auto_promote_and_route`) — the
    # caller confirmed those digits earlier, so a second readback is the same number a
    # third time. A value matching nothing listed is still read back.
    "skip_readback_if_matches",
    # Pins this slot's recovery lines literal against Config.speech.
    "verbatim",
    # The model-facing tool description of this slot's generated setter, emitted onto
    # `pythonFunction.description` (see intent_slot/passive_slot `description=`). Carried
    # here, not runtime state, so `slot_intake` strips it from the live slot.
    "tool_description",
})

FRAMEWORK_VALID_TASK_KEYS: frozenset[str] = frozenset({
    "name", "tool", "inputs", "outputs", "requires", "success_check",
    "condition", "terminal", "then_say", "then_directive",
    "then_response", "channel_then_response", "on_complete",
    "on_failure", "readback_inputs",
    # Component task: a Task that references another DAG (`component`) instead of
    # a `tool`, with `on_abort` controlling parent disposition on sub-flow abort.
    "component", "on_abort",
    # Latency masking: hold music while the tool runs + a spoken filler.
    "while_running", "filler_say",
    # `awaits` marks a task whose tool is ASYNCHRONOUS: the call returns a platform
    # "pending" placeholder and the real payload arrives a turn or more later as a
    # synthetic user turn. Presence makes the engine hold instead of failing.
    "awaits",
    # Polymorphic surfaces: per-surface wording of `then_say` (replaces it), as
    # distinct from `then_response` (which accompanies it).
    "then_say_variants", "channel_then_say_variants",
    # Speak a NON-terminal task's then_say verbatim instead of relaying it.
    "preempt_then_say",
    # An integer slot the engine bumps each time this task fires, so "at most N of these
    # tasks" is a condition (`{"slot": ..., "gte": 3}`) rather than a callback.
    "count_into",
    # Pins this task's on_failure lines literal against Config.speech.
    "verbatim",
    # Names the fan-out group this task is a leg of: every eligible leg of one group is
    # dispatched in a single action and the runtime runs them concurrently, so the group
    # costs the caller its slowest leg rather than the sum.
    "parallel",
    # Set by `parallel(progressive=False)`. Keeps the group on the batch shape --
    # synchronous legs, one action, collected on the same pass -- instead of lowering it
    # to asynchronous legs with a peek/watch pair. One reasoning pass instead of one per
    # watch window, at the cost of per-leg narration.
    "parallel_batch",
})


# ---------------------------------------------------------------------------
# 5.1 Document model (the config) -- mirrors the framework dict exactly.
# ---------------------------------------------------------------------------

# SourceKind = "user" | "event" | "announce" | f"task:{name}".  A source value
# is one kind, or an ordered list of kinds.  Kept as str | list[str] because
# the `task:<name>` form is open-ended.
Source = Union[str, list[str]]

# A spoken latency filler: one line, or a pool to pick from at random. A `None`
# entry in the pool is SILENCE — an ordinary member, so "sometimes say nothing" is
# expressed the same way as "sometimes say this", and either is weighted by writing
# the entry more than once. An agent that always says "one moment" sounds scripted.
FillerSay = Union[str, list[Optional[str]]]


class ConditionLeaf(BaseModel):
    """A single declarative condition predicate over one slot."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    eq: Optional[Any] = None
    neq: Optional[Any] = None
    in_: Optional[list[Any]] = Field(default=None, alias="in")
    not_in: Optional[list[Any]] = None
    filled: Optional[bool] = None
    gte: Optional[float] = None
    lte: Optional[float] = None
    gt: Optional[float] = None
    lt: Optional[float] = None
    upper: Optional[bool] = None
    default: Optional[Any] = None


class ConditionAll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all: list["Condition"]


class ConditionAny(BaseModel):
    model_config = ConfigDict(extra="forbid")
    any: list["Condition"]


class ConditionNot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    not_: "Condition" = Field(alias="not")


# Condition = ConditionLeaf | {all} | {any} | {not} | lambda-source string.
# Note: the boolean-combinator forms are checked before the leaf because a leaf
# requires a `slot` key.
Condition = Union[ConditionAll, ConditionAny, ConditionNot, ConditionLeaf, str]


class ReadbackPlural(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["plural"]
    one: str
    other: str


class ReadbackPrefix(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["prefix"]
    text: str
    # {stored_value: spoken_form}. An enum slot holding "remove_lift" would otherwise
    # reach TTS as its key. An unmapped value falls through to itself.
    values: Optional[dict[str, str]] = None


class ReadbackDate(BaseModel):
    # `text` replaces the default "on" lead-in. Parses ISO (YYYY-MM-DD) and MMDDYYYY;
    # the latter is spoken with the year, since a raw "12012026" is read by TTS as one
    # twelve-million-something number.
    model_config = ConfigDict(extra="forbid")
    type: Literal["date"]
    text: Optional[str] = None


class ReadbackDigits(BaseModel):
    # Speak a digit string one digit at a time, with an optional `text` label. Required
    # by any `readback_verbatim` phone/ZIP/SSN slot: preempted text goes straight to
    # TTS, where an unspaced "2124561234" is one enormous number.
    model_config = ConfigDict(extra="forbid")
    type: Literal["digits"]
    text: Optional[str] = None


class ReadbackNoneSub(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["none_sub"]
    default: str


class ReadbackJoin(BaseModel):
    # List-aware formatter for a repeated (list-valued) slot: render each element
    # via `each` (a dict element by keyword `{field}`, a scalar by `{item}`) and
    # join with `sep`.
    model_config = ConfigDict(extra="forbid")
    type: Literal["join"]
    each: str
    sep: str = ", "


class ReadbackCount(BaseModel):
    # List-aware count formatter for a repeated slot: "<n> <one|other>".
    model_config = ConfigDict(extra="forbid")
    type: Literal["count"]
    one: str
    other: str


# ReadbackFmt = "date" | "time" | "digits"
#             | {plural|prefix|none_sub|join|count|date|digits} | lambda str.
ReadbackFmt = Union[
    ReadbackPlural, ReadbackPrefix, ReadbackNoneSub,
    ReadbackJoin, ReadbackCount, ReadbackDate, ReadbackDigits, str,
]


# Conversation-design fields shared across response parts (all optional, additive
# to the S0 contract): `condition` = inline if/else (same DSL as slot/task
# conditions; parts whose condition is False are dropped at render, see engine
# `_filter_response_parts`); `interruptable=false` disables barge-in on a spoken
# part; `partial=true` speaks a deterministic prefix then lets the model continue.
class TextPart(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["text"]
    text: str
    condition: Optional[Condition] = None
    interruptable: Optional[bool] = None
    partial: Optional[bool] = None


class PayloadPart(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["payload"]
    data: Any = None
    condition: Optional[Condition] = None
    partial: Optional[bool] = None


class ChipOption(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str


class ChipsPart(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["chips"]
    options: Optional[list[ChipOption]] = None
    options_from: Optional[str] = None
    event_name: Optional[str] = None
    condition: Optional[Condition] = None


class AudioPart(BaseModel):
    """Audio playback part — chime / brand audio / pre-recorded prompt / hold
    music. Rendered by the callbacks as a JSON payload the CES audio player
    consumes (`_build_response_part`)."""

    model_config = ConfigDict(extra="allow")
    type: Literal["audio"]
    audioUri: str
    transcript: Optional[str] = None       # gives the model the audio's content
    interruptable: Optional[bool] = None   # barge-in control
    cancellable: Optional[bool] = None     # stops on next response (hold music)
    condition: Optional[Condition] = None
    partial: Optional[bool] = None


class TransferPart(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["transfer"]
    agent: str
    condition: Optional[Condition] = None
    # Pre-transfer disclaimer / context handoff (D1): spoken before the hand-off,
    # and structured state passed to the receiving agent.
    disclaimer: Optional[str] = None
    context: Optional[dict[str, Any]] = None


class EndSessionPart(BaseModel):
    model_config = ConfigDict(extra="allow")  # { type: "end_session"; [k]: any }
    type: Literal["end_session"]
    condition: Optional[Condition] = None


# ResponsePart discriminated union on `type`.
ResponsePart = Annotated[
    Union[TextPart, PayloadPart, ChipsPart, AudioPart, TransferPart,
          EndSessionPart],
    Field(discriminator="type"),
]


class OnExhaust(BaseModel):
    """validation.on_exhaust / steer_back.on_exhaust / on_failure.on_exhaust."""

    model_config = ConfigDict(extra="allow")
    # A dict is the reason-keyed form (`{"timeout": "...", "_default": "..."}`), the same
    # VALUE-shape widening `OnFailure.clear_slots` carries. Only meaningful under
    # `on_failure`, which is the one context with a failing tool's `error_code` to key on;
    # the validator rejects it under `validation` and `steer_back`, where nothing could
    # ever match and the dict would be formatted into a caller-facing line.
    say: Optional[Union[str, dict[str, str]]] = None
    then: Optional[Union[str, "ToolRef"]] = None
    # Resolve the slot with this value and CONTINUE, instead of ending the attempt the
    # way `then` does. Slot `validation.on_exhaust` only; mutually exclusive with `then`.
    fill: Optional[str] = None
    # TASK on_failure.on_exhaust only: False opts a `then` out of terminal escalation,
    # for an in-flow PIVOT that fires a tool but must let the flow CONTINUE.
    escalate: Optional[bool] = None


class ToolRef(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool: str
    args: Optional[dict[str, Any]] = None


class Validation(BaseModel):
    model_config = ConfigDict(extra="allow")
    max_retries: Optional[int] = None
    # A value may be a LADDER as well as a line: one rung per attempt, clamped to the
    # last, the same shape `reprompts` has — except indexed by error CODE too, so
    # "I only caught four digits" and "I didn't hear a number" can escalate separately.
    # Widened for the tenth fork primitive; the engine walks it in `_handle_slot_errors`.
    #
    # NOTE this is a VALUE shape, not a new key, so `test_whitelist_drift.py` — which
    # compares the validator's key SETS against this file and `domain.ts` — cannot see
    # it. Nothing would have failed had this line stayed `dict[str, str]`, except a real
    # config being rejected by its own model.
    errors: Optional[dict[str, Union[str, list[str]]]] = None
    error_responses: Optional[dict[str, list[ResponsePart]]] = None
    channel_error_responses: Optional[
        dict[str, dict[str, list[ResponsePart]]]
    ] = None
    on_exhaust: Optional[OnExhaust] = None
    # B4: escalating no-match ladder — one message per attempt (Invalid1 →
    # Invalid2 …), clamped to the last entry, then on_exhaust. Overrides `errors`.
    reprompts: Optional[list[str]] = None


class ValidateAgainst(BaseModel):
    model_config = ConfigDict(extra="allow")
    response_field: str
    filled_slot: str
    error_code: str


class SlotDefault(BaseModel):
    """One fallback value and the flow state it applies in.

    Ordered within a slot's `default` list: the first whose `when` holds is used, and
    an entry with no `when` is the last resort.
    """

    model_config = ConfigDict(extra="allow")

    value: Any = None
    when: Optional[Condition] = None


class Slot(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    source: Optional[Source] = None
    setter: Optional[str] = None
    setter_field: Optional[str] = None
    ask: Optional[str] = None
    hint: Optional[str] = None
    event_key: Optional[str] = None
    message: Optional[str] = None          # announce
    preempt: Optional[bool] = None         # announce
    requires: Optional[list[str]] = None
    requires_readback: Optional[bool] = None
    readback_fmt: Optional[ReadbackFmt] = None
    # Preempt the FIRST presentation of this slot's readback: the engine's own text
    # (with readback_fmt already applied) goes straight to TTS instead of being relayed
    # for the model to paraphrase — which can come back as a different question
    # entirely. Pair with `readback_fmt`; preempting speaks the value as stored.
    readback_verbatim: Optional[bool] = None
    # Names EARLIER slots whose already-confirmed value makes this slot's readback
    # redundant: when the staged value is digit-identical to any one of them the
    # readback is skipped and the value accepted outright. A value matching none of
    # them is read back as usual. Meaningless without `requires_readback`.
    skip_readback_if_matches: Optional[list[str]] = None
    condition: Optional[Condition] = None
    validation: Optional[Validation] = None
    validate_against: Optional[ValidateAgainst] = None
    response: Optional[list[ResponsePart]] = None
    # NOTE the PLURAL. The engine resolves a slot's response override via
    # `channel_responses` and has always ignored the singular spelling this model
    # used to declare, so an override authored through Slot Studio or Specter was
    # silently inert — no error, no effect.
    channel_responses: Optional[dict[str, list[ResponsePart]]] = None
    # Polymorphic surfaces: per-surface WORDINGS of the ask. A surviving variant
    # replaces `ask`, where `response` would merely be appended to it.
    ask_variants: Optional[list[ResponsePart]] = None
    channel_ask_variants: Optional[dict[str, list[ResponsePart]]] = None
    passive: Optional[bool] = None
    shared: Optional[bool] = None
    # Value policy (see the DSL's event_slot / result_slot). `reject` clears a value
    # that is present but means "not answered yet"; `default` is an ordered list of
    # {value, when?} fallbacks applied when nothing filled the slot; `publish` mirrors
    # the value out to the named session variables.
    reject: Optional[list[str]] = None
    default: Optional[list[SlotDefault]] = None
    publish: Optional[list[str]] = None
    # DTMF keypad mapping: {digit: value} deterministically fills the slot from a
    # keypad press (B1). Silence (no_input) is FLOW-LEVEL only — see Config.no_input.
    dtmf_map: Optional[dict[str, str]] = None
    # Conversation-design intent-slot fields (see validate_dag_config._check_intent_slots and
    # the FRAMEWORK_VALID_SLOT_KEYS whitelist above). `kind:"intent"` marks an enum-selecting
    # operation-choice slot; `option_cues` ({canonical_value: [regex, ...]}) is the text twin of
    # dtmf_map used for deterministic cue->value fill; `validation_rules` are lowered to setter checks.
    kind: Optional[str] = None
    option_cues: Optional[dict[str, list[str]]] = None
    validation_rules: Optional[list[dict]] = None
    # Tiebreak when the utterance matches MORE THAN ONE option_cues value. "unique"
    # (default) is the historical behaviour — an ambiguous match fills nothing. "first"
    # takes the earliest DECLARED value, the same authored-order contract route_cues uses.
    cue_priority: Optional[Literal["unique", "first"]] = None
    # Latency filler for the turn this slot is collected on. A task's `filler_say`
    # covers a TOOL round trip and rides the dispatching preempt; here there is no
    # tool, so the line is spoken as a `partial` preempt and the model's own reply
    # lands in the same turn. One field, one meaning: "there is a wait here".
    filler_say: Optional[FillerSay] = None


class OnComplete(BaseModel):
    model_config = ConfigDict(extra="allow")
    transfer_to: Optional[str] = None
    auto_resume_deferred: Optional[bool] = None
    clear_slots: Optional[list[str]] = None


class OnFailure(BaseModel):
    model_config = ConfigDict(extra="allow")
    # Reason-keyed like `clear_slots` below: a slow backend and a broken one deserve
    # different lines, and telling them apart is the whole point of an `error_code`.
    retry_say: Optional[Union[str, dict[str, str]]] = None
    max_retries: Optional[int] = None
    # Which slots to clear before a retry. A plain list clears the same slots for every
    # failure; a dict keyed by the tool's returned `error_code` clears a different set per
    # reason (with `_default` for the rest), so a verify-and-correct task can drop exactly
    # the invalid field — e.g. {"bad_dob": ["date_of_birth"], "wrong_member": ["member_id"]}.
    # Like `Validation.errors`, this is a VALUE-shape widening (same key), invisible to
    # `test_whitelist_drift.py`; the engine resolves it in `_resolve_clear_slots`.
    clear_slots: Optional[Union[list[str], dict[str, list[str]]]] = None
    retry_response: Optional[list[ResponsePart]] = None
    channel_retry_response: Optional[dict[str, list[ResponsePart]]] = None
    on_exhaust: Optional[OnExhaust] = None
    # Fire a tool on a RETRY (same shape as on_exhaust.then), so a task can ACT on a
    # failure instead of only re-asking — e.g. invalidate a rejected OTP and send a
    # fresh one. Bounded: the retry branch only runs while retries < max_retries.
    then: Optional[Union[str, "ToolRef"]] = None
    # Pin these retry/exhaust lines literal even when config.speech opts their
    # class into improvisation.
    verbatim: Optional[bool] = None
    # {slot: value} resolved on exhaustion, so a flow that arbitrates over a set of
    # statuses still sees a complete picture when the task feeding them failed.
    # `on_exhaust.open_slot` is the one-slot, always-True special case.
    fill: Optional[dict[str, Any]] = None


class Awaits(BaseModel):
    """Wait policy for a task whose tool is declared ASYNCHRONOUS."""

    model_config = ConfigDict(extra="allow")
    # Spoken once, on the turn the "pending" placeholder comes back. Omit for a silent
    # wait (the engine then emits the same silent tick an empty no_input reprompt does).
    say: Optional[str] = None
    # Lines for the IDLE turns after the wait starts, one per turn, drained not cycled.
    # Only reached when the turn has nothing else to do — a wait busy collecting an
    # unrelated slot asks the question instead of talking over it.
    while_waiting: Optional[list[str]] = None
    # Turns to spend answering caller speech that arrives on the SAME turn as the
    # completion, before the downstream terminal may fire. Omitted → that turn is a
    # pure delivery and the utterance is discarded (what every agent does today).
    answer_first: Optional[int] = None
    # REQUIRED. Turns, not seconds — the engine has no clock, and CES has no timeout of
    # its own, so this is the only bound on a backend that never answers.
    max_turns: Optional[int] = None
    on_timeout: Optional[OnExhaust] = None
    # Pin the waiting lines literal even when config.speech opts `await` in.
    verbatim: Optional[bool] = None


class Task(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    # A Task is tool-backed (`tool`) XOR a Component (`component`, a child
    # config_id). Exactly one is required; the validator enforces the XOR.
    tool: Optional[str] = None
    component: Optional[str] = None
    # Component-only: parent disposition when the sub-flow aborts (cancel inside
    # the child). Defaults to "skip" when omitted. Ignored for tool tasks.
    on_abort: Optional[Literal["skip", "fail_flow"]] = None
    # list[str] (ordered slots) OR {slot: param} mapping.
    inputs: Optional[Union[list[str], dict[str, str]]] = None
    outputs: Optional[dict[str, str]] = None  # {result_key: slot}
    requires: Optional[list[str]] = None
    success_check: Optional[str] = None
    condition: Optional[Condition] = None
    terminal: Optional[bool] = None
    on_complete: Optional[OnComplete] = None
    readback_inputs: Optional[bool] = None
    then_say: Optional[str] = None
    # Speak a NON-terminal task's then_say verbatim (model bypassed) instead of folding
    # it into the next question for the model to re-render — which right after a backend
    # action is where it invents outcomes it never performed.
    preempt_then_say: Optional[bool] = None
    then_directive: Optional[str] = None
    then_response: Optional[list[ResponsePart]] = None
    channel_then_response: Optional[dict[str, list[ResponsePart]]] = None
    # Polymorphic surfaces: per-surface wording of `then_say` (replaces it), as
    # distinct from `then_response` (which accompanies it).
    then_say_variants: Optional[list[ResponsePart]] = None
    channel_then_say_variants: Optional[dict[str, list[ResponsePart]]] = None
    on_failure: Optional[OnFailure] = None
    # Latency masking (C1/C2): `while_running` plays audio (hold music) while the
    # tool runs — {audioUri, cancellable}; `filler_say` is a spoken filler.
    while_running: Optional[dict[str, Any]] = None
    filler_say: Optional[FillerSay] = None
    # The tool is declared ASYNCHRONOUS: its call returns a platform "pending"
    # placeholder and the real payload arrives a turn or more later as a synthetic user
    # turn. Presence makes the engine hold rather than route the placeholder into the
    # failure ladder. `while_running` / `filler_say` still apply to the firing turn.
    awaits: Optional["Awaits"] = None
    # Names the fan-out group this task is a leg of. Every eligible leg of one group is
    # dispatched in a single action and the runtime runs them concurrently, so the group
    # costs the caller its slowest leg rather than the sum.
    parallel: Optional[str] = None


class ControlBlock(BaseModel):
    """cancel / escalate control block (TDD section 5.1)."""

    model_config = ConfigDict(extra="allow")
    # Read by the derived graph only — the runtime setter is the framework control
    # tool, so this draws an edge that is never traversed. The validator warns.
    tool: Optional[str] = None
    transfer_to: Optional[str] = None
    say: Optional[str] = None
    response: Optional[list[ResponsePart]] = None
    channel_response: Optional[dict[str, list[ResponsePart]]] = None
    # `cancel` only: False turns the teardown into a step back to the flow's next open
    # question, after un-deciding `clear_slots` and everything derived from them.
    end_conversation: Optional[bool] = None
    clear_slots: Optional[list[str]] = None
    # escalate only: an ordered pre-terminal task chain run BEFORE the disposition, so
    # the receiving human gets a summary instead of a cold transfer (engine
    # `_escalate_path_turn`). Documentation — the model is extra="allow".
    tasks: Optional[list[str]] = None
    # escalate only: route the detected human request into an INTERACTIVE, in-DAG,
    # returnable deflection sub-flow (a child config id) instead of the fixed
    # chain-then-terminate disposition (engine `_escalate_disposition` ->
    # `_component_fire_action`). Owns the whole disposition, so it is mutually
    # exclusive with tasks/transfer_to/response. `inputs`/`outputs` seed/read the
    # child scope like a component task; `on_abort` governs a child that aborts.
    component: Optional[str] = None
    inputs: Optional[dict[str, str]] = None
    outputs: Optional[dict[str, str]] = None
    on_abort: Optional[str] = None
    # Gate on whether the disposition may run at all; when false the request is
    # dropped (so `tasks` never arms) and `declined_say` (if given) explains why.
    condition: Optional[dict[str, Any]] = None
    # A single line; a ladder indexed by refusal count (clamped to the last); or a
    # list of `{"when": <condition>, "say": ...}` REASONS evaluated in order, so a
    # flow that can refuse for more than one reason says the right thing for each.
    declined_say: Optional[Union[str, list[Union[str, dict[str, Any]]]]] = None
    # The disposition written to the zombie's exit_status.flow_outcome (flow-level
    # disposition), NOT the end_session part. `escalate` defaults it to "escalated".
    outcome: Optional[str] = None
    # The `reason` on the terminal end_session PART a downstream contract reads. Absent,
    # `_terminate_control` keeps its default ("transfer" for escalate, "cancelled" for
    # cancel). Distinct from `outcome` above; set via escalate(reason=)/cancel(reason=).
    reason: Optional[str] = None


class IntentChange(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool: str
    hint: Optional[str] = None
    # Hand off to another flow on an intent change. transfer_map.py reads
    # `switch ?? transfer_to` to draw the intent-change "switch" edge; the
    # runtime engine ignores both. `transfer_to` is a legacy alias — always
    # authored as `switch` going forward.
    switch: Optional[str] = None
    transfer_to: Optional[str] = None


class Bootstrap(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool: Optional[str] = None
    slot: Optional[str] = None
    reset_on_complete: Optional[bool] = None
    welcome_slot: Optional[str] = None
    # Design-time transfer-map child flows only — NOT runtime routing (that reads
    # top-level Config.flow_types).
    flow_types: Optional[list[str]] = None
    pass_through_on_transfer: Optional[bool] = None
    # single_flow self-seed value for the gate slot (engine ~5048; defaults to the
    # config id when blank). Meaningful only for single_flow configs.
    auto_seed: Optional[str] = None
    # Two-pass classify: classify every in-flow turn before acting (engine ~4930).
    intent_first: Optional[bool] = None


class SteerBack(BaseModel):
    model_config = ConfigDict(extra="allow")
    soft_after: Optional[int] = None
    hard_after: Optional[int] = None
    escalate_after: Optional[int] = None
    steer_back_directive: Optional[str] = None
    on_exhaust: Optional[OnExhaust] = None


class Speech(BaseModel):
    """Which canned utterances the model may reword, and how (flow-level).

    The framework normally speaks its recovery lines verbatim, preempting the model
    so the caller hears exactly the authored sentence. Naming a class here moves
    that family of utterance onto the directive channel instead: the line becomes
    an instruction the model rewords, the way a slot `ask` already works.

    Absent, nothing is improvised. Structural guards still keep a line literal when
    its turn also carries a tool call, non-text response parts, or a terminal
    status, so opting a class in is a request, not an override.
    """

    model_config = ConfigDict(extra="allow")
    # Any of: reprompt, no_input, exhaust, retry, control, await, filler.
    # `filler` is the odd one: on a TASK it hands the tool call to the model along
    # with the line, so it only works where the engine can give up the turn; on a
    # model turn the filler is an ordinary partial preempt and rewording it is free.
    improvise: Optional[list[str]] = None
    # Appended to the directive on improvised turns only, e.g. "Warm and brief.
    # Never reuse your previous phrasing."
    improvise_style: Optional[str] = None


class NoInput(BaseModel):
    """Flow-level silence (no-input) policy (B2): an escalating reprompt ladder
    (one line per consecutive silent turn) + a terminal on_exhaust. Applied to
    whichever user slot is being asked. Mirrors domain.ts `Config.no_input`."""
    model_config = ConfigDict(extra="allow")
    reprompts: Optional[list[str]] = None
    on_exhaust: Optional[OnExhaust] = None
    # Caller-asked-to-hold handling. `hold_phrases` is what the engine matches;
    # `hold_reprompts` replaces the silence ladder once holding (empty = silent
    # tick); `hold_ack` is spoken in place of the pending ask on the turn the
    # request is heard, so the caller is not immediately re-asked.
    hold_phrases: Optional[list[str]] = None
    hold_reprompts: Optional[list[str]] = None
    hold_ack: Optional[str] = None
    # What disqualifies an utterance that CARRIES a marker: a question about the ask, a
    # request for a person, a statement that the caller cannot answer. Unset uses the
    # engine's defaults; `[]` matches on markers alone.
    hold_vetoes: Optional[list[str]] = None
    # Pin the silence lines literal even when config.speech opts `no_input` in.
    verbatim: Optional[bool] = None


class VariableMapAlt(BaseModel):
    """One candidate source: a top-level variable, plus a path if it is an OBJECT."""

    model_config = ConfigDict(extra="allow")

    var: str
    path: list[str] = Field(default_factory=list)


class VariableMapBinding(BaseModel):
    """One slot and the alternatives it may be filled from, tried in order."""

    model_config = ConfigDict(extra="allow")

    slot: str
    alts: list[VariableMapAlt] = Field(default_factory=list)
    # Resolved at build from the target slot, so the ingress callback needs no config.
    shape: str = "scalar"
    readback: bool = False
    # Values that are present but mean "not answered yet" (an upstream sentinel).
    reject: list[str] = Field(default_factory=list)


class VariableMapSpec(BaseModel):
    """One named variable shape. Every binding must resolve for it to be chosen."""

    model_config = ConfigDict(extra="allow")

    name: str
    bindings: list[VariableMapBinding] = Field(default_factory=list)


class Config(BaseModel):
    """The slot-filling flow document. Mirrors the framework `*_dag()` dict."""

    model_config = ConfigDict(extra="allow")

    slots: list[Slot] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    gate_slot: Optional[str] = None
    correction_tool: Optional[str] = None
    bootstrap: Optional[Bootstrap] = None
    cancel: Optional[ControlBlock] = None
    escalate: Optional[ControlBlock] = None
    intent_change: Optional[IntentChange] = None
    steer_back: Optional[SteerBack] = None
    # Flow-level silence (no-input) policy (B2); flow-level only (no per-slot form).
    no_input: Optional[NoInput] = None
    # Which classes of canned utterance the model may reword, and how.
    speech: Optional[Speech] = None
    # Flow-level default latency filler: used on any turn handed to the model whose
    # slot/task does not carry its own. A pool here is the usual shape — one line
    # repeated across a whole flow is exactly what makes an agent sound scripted.
    filler_say: Optional[FillerSay] = None
    confirm_transition_prefix: Optional[list[str]] = None
    readback_response: Optional[list[ResponsePart]] = None
    channel_readback_response: Optional[dict[str, list[ResponsePart]]] = None
    # Polymorphic surfaces: {surface_name: {capability: value}} overlaid on the
    # engine's built-in `voice`/`chat`, and the surface an absent or unrecognized
    # channel resolves to. Both optional; almost every app declares neither.
    surfaces: Optional[dict[str, dict[str, Any]]] = None
    default_surface: Optional[str] = None
    # Session-variable ingress, lowered per config from App(variable_maps=[...]).
    # Ordered: the first shape whose every binding resolves is the one that fills.
    variable_maps: Optional[list[VariableMapSpec]] = None
    exit_status: Optional[dict[str, str]] = None
    event_mappings: Optional[dict[str, dict[str, Any]]] = None
    shared_slots: Optional[list[str]] = None
    # Runtime routing vocabulary the router can dispatch to (engine ~4925). Distinct
    # from bootstrap.flow_types (design-time transfer map only).
    flow_types: Optional[list[str]] = None
    # A flow with no host to fill the gate; the engine self-seeds via
    # bootstrap.auto_seed (engine ~4924).
    single_flow: Optional[bool] = None
    router: Optional[bool] = None
    # Dedicated keyword router: {flow_name: [keyword, ...]}. Tightened authoring
    # contract; readers stay defensive for legacy loose values (bare string/None).
    route_cues: Optional[dict[str, list[str]]] = None


# ---------------------------------------------------------------------------
# 5.2 Graph (derived, never exported)
# ---------------------------------------------------------------------------

NodeRole = Literal[
    "user_slot", "task_slot", "event_slot", "announce_slot", "task", "component",
    "control_cancel", "control_escalate", "control_intent",
    "control_correction", "control_router",
]
BadgeKind = str  # open-ended badge identifiers (TDD section 4.2)
EdgeKind = Literal["input", "requires", "output"]


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    role: NodeRole
    ref: str  # slot/task name
    questionIndex: Optional[int] = None
    badges: list[BadgeKind] = Field(default_factory=list)
    conditional: Optional[bool] = None
    setterGroup: Optional[str] = None
    position: Optional[Position] = None


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    kind: EdgeKind
    source: str
    target: str
    label: Optional[str] = None  # {slot:param} mapping


class GraphCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setter: str
    members: list[str]


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    clusters: list[GraphCluster] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 5.3 Diagnostics
# ---------------------------------------------------------------------------

Severity = Literal["error", "warning", "info", "needs_review"]
AnchorKind = Literal["slot", "task", "edge", "flow", "field"]


class NodeAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: AnchorKind
    ref: Optional[str] = None
    field: Optional[str] = None


# ConfigPatch is a client-owned reversible edit command (undo stack). At the
# wire level it is an opaque object; the canonical shape lives in domain.ts and
# is filled in by S7. Kept open here so a Diagnostic.fix can carry one.
ConfigPatch = dict[str, Any]


class DiagnosticFix(BaseModel):
    model_config = ConfigDict(extra="allow")
    label: str
    patch: ConfigPatch


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="allow")
    severity: Severity
    message: str
    raw: str
    anchor: Optional[NodeAnchor] = None
    fix: Optional[DiagnosticFix] = None
    group: Optional[Literal["across_flows"]] = None


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    valid: bool
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    shippable: bool = True


class CrossValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    valid: bool
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    shippable: bool = True
    perConfig: dict[str, ValidationReport] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 5.3 Engine (Engine-mode simulator)
# ---------------------------------------------------------------------------

# Opaque, server-owned slot-machine state. JSON-able dict (TDD section 3.3).
SmSnapshot = dict[str, Any]

NextAction = Literal[
    "announce", "fire", "next_question", "readback", "terminal", "gate",
    "preempt",
]


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class EngineStepResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    agent_text: str = ""
    response_parts: list[ResponsePart] = Field(default_factory=list)
    hide_tools: list[str] = Field(default_factory=list)
    function_call: Optional[FunctionCall] = None
    next_action: NextAction = "next_question"
    active_nodes: list[str] = Field(default_factory=list)
    slot_inspection: Any = None  # same shape as Live SlotInspector
    sm: SmSnapshot = Field(default_factory=dict)
    status: str = "in_progress"  # in_progress|complete|zombie|escalated
    step_index: int = 0
    can_step_back: bool = False
    # Component descent: BARE id of the flow the engine is currently in (root or a
    # drilled-into child) + the call-stack depth (0 at the root).
    active_config_id: Optional[str] = None
    call_depth: int = 0


# EngineStepRequest discriminated union on `kind` (TDD section 4.4).
class UserTextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["user_text"]
    text: str
    session_id: str


class SetterCallStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["setter_call"]
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    session_id: str


class ConfirmStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["confirm"]
    session_id: str


class RejectStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["reject"]
    session_id: str


class TaskResultStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["task_result"]
    task_name: str
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    session_id: str


class EventPrefillStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["event_prefill"]
    event_data: dict[str, Any] = Field(default_factory=dict)
    session_id: str


EngineStepRequest = Annotated[
    Union[
        UserTextStep, SetterCallStep, ConfirmStep, RejectStep,
        TaskResultStep, EventPrefillStep,
    ],
    Field(discriminator="kind"),
]


class EngineStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    config_id: str
    channel: Optional[str] = None
    event_data: Optional[dict[str, Any]] = None
    # Component children (BARE id -> config) so the sim can descend into a
    # sub-flow. Omitted for single-flow agents.
    configs: Optional[dict[str, Config]] = None


class EngineSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    step: EngineStepResult


class SessionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str


class SetterSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool: str
    fields: list[str] = Field(default_factory=list)


class HiddenSetter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    reason: str


class VisibleSettersResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visible: list[SetterSpec] = Field(default_factory=list)
    hidden: list[HiddenSetter] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 5.3 Chat-step (Live mode)
# ---------------------------------------------------------------------------

class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    agent: str = ""


class TurnMetrics(BaseModel):
    """Per-turn latency + token metrics (populated when --with-metrics).

    Mirrors cxas `_enrich_result` metrics: cxas
    `{duration_ms, tokens:{input, output, total}}` maps to these fields.
    All fields Optional so an absent/partial metrics blob stays schema-valid.
    """

    model_config = ConfigDict(extra="allow")
    latency_ms: Optional[float] = None
    in_tokens: Optional[int] = None
    out_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


class ChatStepResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    turn_index: int = 0
    user_text: str = ""
    agent_text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_responses: list[Any] = Field(default_factory=list)
    agent_transfer: Optional[Any] = None
    session_ended: bool = False
    state: Any = None
    session_id: str = ""
    payloads: list[Any] = Field(default_factory=list)
    slot_inspection: Optional[Any] = None
    sm_log: Optional[list[Any]] = None
    si_trace: Optional[list[Any]] = None
    flow_context: Optional[Any] = None
    metrics: Optional[TurnMetrics] = None  # populated when --with-metrics
    trace: Optional[str] = None            # per-turn trace text when requested


ErrorKind = Literal[
    "not_pushed", "wrong_app_name", "auth_expired", "timeout", "network",
    "cxas_missing", "parse_error",
]


class LiveError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ErrorKind
    message: str
    remediation: Optional[str] = None
    raw: Optional[str] = None


# Live-mode REST/WS request bodies (the WS wraps these; TDD section 4.5).
class LiveStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_name: Optional[str] = None
    channel: Optional[str] = None
    flags: dict[str, Any] = Field(default_factory=dict)


class LiveSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    app_name: str


class LiveStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    text: str
    flags: dict[str, Any] = Field(default_factory=dict)


class LiveResetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool = True


class AppNameResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_name: str


# Trace / Bug / Event live RPCs (TDD section 2.3) -- additive, S0-safe.
class TraceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    format: Literal["text", "md", "json", "html"] = "md"


class TraceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str
    content: str


class BugRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    reason: str
    severity: Literal["low", "medium", "high"] = "medium"


class BugResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    uri: Optional[str] = None
    error: Optional[str] = None


class EventRequest(BaseModel):
    """A `send_event` turn (welcome/entry + interactive event buttons).

    Reuses the step path with an event message; the result is a normal
    `ChatStepResult`.
    """

    model_config = ConfigDict(extra="forbid")
    session_id: str
    name: str
    data: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 4.6 Eval replay
# ---------------------------------------------------------------------------

ReplayFormat = Literal["golden", "scenario", "json"]


class ReplayTurn(BaseModel):
    model_config = ConfigDict(extra="allow")
    user_text: str
    expected_tool: Optional[str] = None
    expected_text: Optional[str] = None


class ReplayLoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Optional[str] = None
    content: Optional[str] = None
    format: ReplayFormat = "json"


class ReplayLoadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turns: list[ReplayTurn] = Field(default_factory=list)


class ReplayStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    turn_index: int


class ReplayComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_match: bool
    text_match: bool
    diverged: bool


class ReplayStepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: ChatStepResult
    comparison: ReplayComparison


# ---------------------------------------------------------------------------
# 4.1 Config import / export
# ---------------------------------------------------------------------------

class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_id: Optional[str] = None  # load from agent dir by name
    dict_: Optional[dict[str, Any]] = Field(default=None, alias="dict")
    source: Optional[str] = None  # python-dict literal / module source
    app: Optional[str] = None  # hosted mode: the selected CES app resource name


class ImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    config_id: Optional[str] = None
    graph: Optional[Graph] = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class BundleEntry(BaseModel):
    """One config in a multi-config workspace bundle: a root DAG plus every
    reachable Component child. `config_id` is the BARE engine-domain id (e.g.
    `address_capture`; the source tool is `<id>_dag`)."""

    model_config = ConfigDict(extra="forbid")
    config_id: str
    config: Config
    graph: Optional[Graph] = None
    is_root: bool = False


class ImportBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_id: str  # root config id (bare or `_dag`-suffixed; normalized)
    app: Optional[str] = None  # hosted mode: the selected CES app resource name
    max_depth: int = 3


class ImportBundleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_id: str  # BARE root id
    configs: list[BundleEntry]  # root first, then reachable children
    diagnostics: list[Diagnostic] = Field(default_factory=list)


ExportFormat = Literal["json", "python"]


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    format: ExportFormat = "json"
    config_id: Optional[str] = None
    allow_errors: bool = False


class ExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    filename: str
    warnings: list[str] = Field(default_factory=list)


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    force: bool = False  # regenerate even if cached
    # Which slice to generate: "overview" (whole agent), "nodes" (the given refs,
    # for parallel chunking), or "all" (both, back-compat).
    scope: Literal["overview", "nodes", "all"] = "all"
    refs: Optional[list[str]] = None  # node refs for scope="nodes"


class ExplainResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available: bool
    overview: str = ""
    # node ref -> plain-English explanation (keyed by the graph node `ref`).
    nodes: dict[str, str] = Field(default_factory=dict)
    reason: Optional[str] = None  # why unavailable (no project / call failed)


class NormalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config


class NormalizeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    graph: Graph


# ---------------------------------------------------------------------------
# 4.1 Agent discovery
# ---------------------------------------------------------------------------

class AgentConfigInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_id: str
    display_name: str


class AgentConfigsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configs: list[AgentConfigInfo] = Field(default_factory=list)


class AgentApp(BaseModel):
    """A selectable hosted CES app (the hosted-mode picker)."""

    model_config = ConfigDict(extra="forbid")
    id: str  # full app resource name (projects/.../apps/...)
    display_name: str


class AgentAppsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apps: list[AgentApp] = Field(default_factory=list)


class AgentToolsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[str] = Field(default_factory=list)


class ScanDagsRequest(BaseModel):
    """A batch of apps to scan for `*_dag` configs (the background picker scan).
    The server splits them across a thread pool and scans in parallel."""

    model_config = ConfigDict(extra="forbid")
    apps: list[str] = Field(default_factory=list)


class ScanDagsResult(BaseModel):
    """Per-app `*_dag` count for the scanned batch. Count is 0 for apps with
    none (or unreadable apps, e.g. no permission)."""

    model_config = ConfigDict(extra="forbid")
    counts: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 4.2 Validation requests
# ---------------------------------------------------------------------------

class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    available_tools: Optional[list[str]] = None
    setter_sources: Optional[dict[str, str]] = None
    task_tool_sources: Optional[dict[str, str]] = None


class ValidateCrossRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configs: dict[str, Config]
    available_tools: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# 4.3 Condition / preview services
# ---------------------------------------------------------------------------

class ConditionRenderRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    condition: Union[dict[str, Any], str]


class ConditionRenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    parseable: bool


class ConditionLintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lambda_: str = Field(alias="lambda")


class LintError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    offset: Optional[int] = None


class ConditionLintResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    error: Optional[LintError] = None


class ReadbackPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    readback_fmt: ReadbackFmt
    sample_value: Any = None


class ReadbackPreviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview: str


class ResponsePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    response_parts: list[ResponsePart] = Field(default_factory=list)
    filled_sample: dict[str, Any] = Field(default_factory=dict)


class ResponsePreviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parts: list[ResponsePart] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 4.7 Transfer map
# ---------------------------------------------------------------------------

class TransferMapNode(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    label: Optional[str] = None


class TransferMapEdge(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    source: str
    target: str
    kind: Optional[str] = None  # transfer|resume|pause|escalate|cancel|switch
    label: Optional[str] = None


class TransferMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[TransferMapNode] = Field(default_factory=list)
    edges: list[TransferMapEdge] = Field(default_factory=list)


class TransferMapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configs: dict[str, Config]
    flow_context: Optional[Any] = None


# ---------------------------------------------------------------------------
# 4.8 Health / meta
# ---------------------------------------------------------------------------

class HealthResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    framework_root: str
    cxas_available: bool
    agent_dir: Optional[str] = None
    # Source mode + hosted target (so the client adapts: local vs hosted UI).
    mode: str = "local"
    project: Optional[str] = None
    location: Optional[str] = None


class ThemeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: str


# ---------------------------------------------------------------------------
# 4.9 Error envelope
# ---------------------------------------------------------------------------

class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: str
    message: str
    remediation: Optional[str] = None
    raw: Optional[str] = None
    anchor: Optional[NodeAnchor] = None


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: ErrorBody


# Resolve forward references for the recursive Condition / OnExhaust models.
ConditionAll.model_rebuild()
ConditionAny.model_rebuild()
ConditionNot.model_rebuild()
OnExhaust.model_rebuild()
