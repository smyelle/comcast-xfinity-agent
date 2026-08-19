"""Engine-mode simulator: server-side session store + step pipeline (TDD 4.4).

This module owns the offline Engine-mode simulator. A *session* is server-side
state holding the threaded slot-machine `sm`, the source config, and a `history`
of prior `sm` snapshots for instant step-back (TDD section 4.4 / section 11 Q2 --
snapshot, not replay).

Each `step()` handles the `EngineStepRequest` discriminated union and reproduces
the live `before_model -> after_tool -> before_model` order via the
`framework_bridge` typed wrappers:

- `user_text`     -> engine(last_user_text)                         (no setter)
- `setter_call`   -> setter fn -> intake -> engine                  (real validation)
- `confirm`       -> engine(last_user_text="yes")                   (auto/inline confirm)
- `reject`        -> engine(last_user_text="no")
- `task_result`   -> intake (executor branch) -> engine
- `event_prefill` -> engine(event_data=...)

It NEVER calls an LLM: free text is fed to the engine's deterministic
affirmative/steer-back logic only. `EngineStepResult` is derived from the engine
`action` + the threaded `sm`.

Concurrency & State Boundaries: this module does NOT acquire locks itself; the API router
serializes every simulation request behind `state.engine_lock`. This lock discipline is
mandatory because the underlying engine holds process-global caches (`_COMPILED_CONFIGS`,
`_sm_ref`) that must not be accessed concurrently. `_SESSIONS` holds process-global state,
binding session lifetimes to the active server worker process (single-worker execution model).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..engine import loader as fb

# Engine `config_id` used for the compiled-config cache key. Per-session unique
# so two sessions with different configs never collide in the engine's
# process-global `_COMPILED_CONFIGS` (TDD section 3.3 -- compiled configs hold
# lambdas and are server-internal).
_CONFIG_ID_PREFIX = "slot_studio_sim__"

# Default display name for the offline "current agent" intake passes (only
# affects the transfer decision, which Engine mode surfaces but does not act on).
_DEFAULT_AGENT = "Slot_Studio"


@dataclass
class _Snapshot:
    """A prior turn captured for instant, exact step-back (no replay).

    Everything a step advances belongs here, not just `sm`. Two pieces of turn
    state live on the session because their lifetime is the session: the caller
    turn count (published by the engine as `sm["_turn_n"]`, and what
    `awaits.max_turns` measures against) and the CES session state tools read and
    write as `context.state`. Omitted from the snapshot they survive a step-back,
    so re-running the same step counts an extra turn and hands a tool state from a
    future that was undone -- and a tool branching on `context.state` then returns
    something different the second time, which is step-back producing a
    conversation the caller could not have had.
    """

    sm: dict[str, Any]
    result: dict[str, Any]
    step_index: int
    n_user_turns: int
    ces_state: dict[str, Any]
    #: Which flow the session is RUNNING. A router handoff swaps these, so leaving
    #: them out let a step-back rewind the sm while the session stayed in the child
    #: — the caller returns to the router's question and the engine answers it with
    #: the child's config.
    config: dict[str, Any]
    config_id: str


@dataclass
class _Session:
    """One Engine-mode simulator session (server-owned)."""

    session_id: str
    config_id: str  # the engine cache key (unique per session)
    flow_id: str  # the author-facing flow id (e.g. bella_notte_dag)
    config: dict[str, Any]
    sm: dict[str, Any]
    channel: str = ""
    # Component children (BARE id -> raw config) so the engine can DESCEND into a
    # Component sub-flow offline. Empty for a single-flow agent.
    configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Router `flow key -> config id` map. Live, `before_agent` resolves the active
    # flow to its config THROUGH this map (so many routes can share one config, e.g. a
    # steering router's deferred keys all pointing at one deferral flow). Empty ⇒ resolve
    # by exact id, the prior behaviour. Emitted as the `flow_config_map` state var.
    flow_config_map: dict[str, str] = field(default_factory=dict)
    # Stack of prior (sm, rendered result) snapshots; back() pops the top and
    # restores both verbatim -- the sm is the source of truth and is NOT re-run,
    # so step-back is exact (TDD section 11 Q2 -- snapshot, not replay).
    history: list["_Snapshot"] = field(default_factory=list)
    step_index: int = 0
    initial_sm: dict[str, Any] = field(default_factory=dict)
    initial_result: dict[str, Any] = field(default_factory=dict)
    #: The flow the session STARTED on, so `reset` returns to the router rather
    #: than to whichever child a handoff last left it in.
    initial_config: dict[str, Any] = field(default_factory=dict)
    initial_config_id: str = ""
    last_result: dict[str, Any] = field(default_factory=dict)
    # The latest REAL user utterance, carried across turns. Live, before_model
    # scans it from history and hands it to the engine every turn; the sim mirrors
    # that so the intent-first Pass-B path that reads scanned_user_text behaves
    # like live — notably the silence-after-speech no_input ladder (#517).
    last_real_user_text: str = ""
    # Turns the CALLER has taken. Live this is scanned off the conversation contents;
    # here it is counted, because only this module knows which steps are the caller
    # speaking (`user_text`, a setter the caller's answer triggered, a confirm) and
    # which are the engine's own cascade (`task_result`, `event_prefill`). The engine
    # publishes it as `sm["_turn_n"]`, and `awaits.max_turns` measures against it: an
    # uncounted sim holds an asynchronous task open forever, since 0 - 0 never reaches
    # any bound.
    n_user_turns: int = 0
    # The CES session state a deployed tool reads and writes as `context.state`.
    # Session-lifetime, because that is its lifetime live: tools in the same app talk
    # to each other through it (one stores the appointment the caller picked; the next
    # reads it back). Held here rather than on sm so that threading sm into it, which
    # the framework readback tools require, does not make sm self-referential.
    ces_state: dict[str, Any] = field(default_factory=dict)
    # Tools root for THIS session. None means the packaged framework bundle (the
    # authoring case). A session simulating a DEPLOYED agent passes a root built by
    # `loader.materialize_tools_root` so the agent's OWN setters and executors are
    # the ones that run — without it the first setter call raises FileNotFoundError,
    # because an app's tools are not framework tools.
    framework_root: Optional[str] = None


# Process-global session store. Bound to a single-worker server process. Session lookup
# and mutation must be serialized by the router under `state.engine_lock`.
_SESSIONS: dict[str, _Session] = {}


def reset_store() -> None:
    """Drop all sessions (test isolation)."""
    _SESSIONS.clear()


# --- Result derivation -------------------------------------------------------


def _status(sm: dict[str, Any]) -> str:
    """The simulator status surfaced to the client."""
    return sm.get("status") or "in_progress"


def _next_action(action: dict[str, Any], sm: dict[str, Any]) -> str:
    """Map the engine action + sm onto the contract `NextAction` enum.

    The worktree engine action does NOT carry a `tag`; we infer the phase from
    the action's shape (function_call/message/response) and sm (gate filled,
    pending readback, terminal status), staying within the frozen enum:
    announce|fire|next_question|readback|terminal|gate|preempt.
    """
    status = _status(sm)
    if status in ("complete", "zombie", "escalated"):
        return "terminal"
    gate_slot = sm.get("_gate_slot")
    if gate_slot and not sm.get("filled", {}).get(gate_slot):
        return "gate"
    if action.get("function_call"):
        return "fire"
    if sm.get("pending"):
        return "readback"
    if action.get("preempt") or action.get("force_preempt"):
        return "preempt"
    return "next_question"


def _active_nodes(action: dict[str, Any], sm: dict[str, Any]) -> list[str]:
    """Node refs to highlight: filled + pending slots, plus a fired tool's slot.

    Derived from sm (the canvas keys nodes by slot/task ref). Function-call
    targets map back through `_setter_slots`/`_executor_tasks` to the slot/task
    ref the node uses.
    """
    nodes: list[str] = []
    for name in list(sm.get("filled", {})) + list(sm.get("pending", {})):
        if isinstance(name, str) and not name.startswith("_") and name not in nodes:
            nodes.append(name)
    fc = action.get("function_call") or {}
    tool = fc.get("name")
    if tool:
        slot = sm.get("_setter_slots", {}).get(tool)
        if slot and slot not in nodes:
            nodes.append(slot)
        task = (sm.get("_executor_tasks", {}).get(tool) or {}).get("task_name")
        if task and task not in nodes:
            nodes.append(task)
    return nodes


def _slot_inspection(config: dict[str, Any], sm: dict[str, Any]) -> dict[str, Any]:
    """A SlotInspector-shaped view for parity with Live mode (TDD section 4.4).

    Mirrors the Live `slot_inspection` shape closely enough to drive the state
    inspector: per-slot status (filled/pending/deferred/open) + value source.
    """
    filled = sm.get("filled", {})
    pending = sm.get("pending", {})
    deferred = sm.get("deferred", {})
    slots = []
    for slot_def in config.get("slots", []):
        name = slot_def.get("name")
        if name in filled:
            status, value = "filled", filled[name]
        elif name in pending:
            status, value = "pending", pending[name]
        elif name in deferred:
            status, value = "deferred", deferred[name]
        else:
            status, value = "open", None
        slots.append({"name": name, "status": status, "value": value})
    return {
        "slots": slots,
        "status": _status(sm),
        # Copied, never handed out live: this helper is called with the LIVE `sm`
        # (unlike the deep-copied `sm` snapshot beside it in `_derive_result`), and
        # `sm["task_results"]` keeps mutating in place across turns — so a result
        # already returned to a caller must not grow a later task's output.
        "task_results": fb.deep_copy_sm(sm.get("task_results", {})),
        # Async tasks dispatched with their result still outstanding. The engine's key
        # is `_awaiting_async`; a test pins the two together, because a rename on
        # either side degrades to a silently-always-empty field rather than an error.
        "awaiting_tasks": sorted(sm.get("_awaiting_async") or {}),
    }


def _active_config(session: _Session, sm: dict[str, Any]) -> dict[str, Any]:
    """The raw config the engine is CURRENTLY in (root or a drilled-into child),
    so slot inspection / visible setters reflect the active sub-flow during a
    Component descent rather than the root."""
    stack = sm.get("_call_stack") or []
    if stack:
        child = stack[-1].get("child_config")
        if isinstance(child, str) and child in session.configs:
            return session.configs[child]
    return session.config


def _active_config_id(session: _Session, sm: dict[str, Any]) -> str:
    """The BARE id of the flow the engine is currently in, derived from the call
    stack (the robust invariant): a non-empty stack means we're in the top frame's
    child; an empty stack means the root. (sm["_config_id"] lags a turn on RETURN
    under the END-THE-PASS model, so we don't read it here.)"""
    stack = sm.get("_call_stack") or []
    if stack:
        child = stack[-1].get("child_config")
        if isinstance(child, str) and child:
            return child  # bare child id
    fid = session.flow_id
    return fid[:-4] if fid.endswith("_dag") else fid


def _derive_result(
    session: _Session, engine_out: dict[str, Any]
) -> dict[str, Any]:
    """Build the `EngineStepResult` dict from a raw engine `{action, sm}`."""
    action = engine_out.get("action", {}) or {}
    sm = engine_out.get("sm", {}) or {}
    session.sm = sm
    # The result carries an IMMUTABLE snapshot of sm: the live `session.sm` keeps
    # mutating in place across turns, but each step's emitted result must reflect
    # the state at THAT step (and the client/tests must be able to keep it).
    sm_snapshot = fb.deep_copy_sm(sm)

    fc = action.get("function_call")
    function_call = None
    if isinstance(fc, dict) and fc.get("name"):
        function_call = {"name": fc["name"], "args": fc.get("args", {}) or {}}

    result = {
        "agent_text": action.get("message", "") or "",
        "response_parts": action.get("response", []) or [],
        "hide_tools": action.get("hide_tools", []) or [],
        "function_call": function_call,
        "next_action": _next_action(action, sm),
        "active_nodes": _active_nodes(action, sm),
        "slot_inspection": _slot_inspection(_active_config(session, sm), sm),
        "sm": sm_snapshot,
        "status": _status(sm),
        "step_index": session.step_index,
        "can_step_back": bool(session.history),
        # Component descent signals (BARE active id + call depth) so the canvas can
        # auto-follow into / back out of a sub-flow as the engine descends/returns.
        "active_config_id": _active_config_id(session, sm),
        "call_depth": len(sm.get("_call_stack") or []),
    }
    session.last_result = result
    return result


# --- Session lifecycle -------------------------------------------------------


def _engine_configs(session: _Session) -> Optional[dict[str, Any]]:
    """The `configs=` map for run_engine: the root (keyed by its engine cache id,
    so RETURN can reload it) plus every Component child (BARE id). None for a
    single-flow session so the legacy single-config path is byte-for-byte unchanged.
    """
    if not session.configs:
        return None
    return {session.config_id: session.config, **session.configs}


def _push_history(session: _Session) -> None:
    """Snapshot the current (sm, rendered result) before this step mutates them.

    Deep-copied so a later in-place engine mutation cannot reach back into the
    snapshot -- this is what makes step-back EXACT.
    """
    session.history.append(_Snapshot(
        sm=fb.deep_copy_sm(session.sm),
        result=fb.deep_copy_sm(session.last_result),
        step_index=session.step_index,
        n_user_turns=session.n_user_turns,
        ces_state=fb.deep_copy_sm(session.ces_state),
        # Not deep-copied: configs are read-only here, and one is ~20KB.
        config=session.config,
        config_id=session.config_id,
    ))


def start(
    config: dict[str, Any],
    flow_id: str,
    channel: Optional[str] = None,
    event_data: Optional[dict[str, Any]] = None,
    configs: Optional[dict[str, dict[str, Any]]] = None,
    framework_root: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Create a session and run the initial gate/announce turn.

    `configs` (BARE id -> raw config) are the Component children so the engine can
    descend into a sub-flow during the sim. `framework_root` is the tools root every
    turn of this session loads from — pass one built by
    :func:`flows.engine.loader.materialize_tools_root` to simulate a DEPLOYED agent
    with its own tools. Returns `(session_id, EngineStepResult-dict)`.
    """
    session_id = uuid.uuid4().hex
    config_id = _CONFIG_ID_PREFIX + session_id
    sm: dict[str, Any] = {}
    if channel:
        sm["channel"] = channel
    fb.seed_sm(config, sm)
    session = _Session(
        session_id=session_id,
        config_id=config_id,
        flow_id=flow_id,
        config=config,
        sm=sm,
        channel=channel or "",
        configs=configs or {},
        framework_root=framework_root,
    )
    # Evict any stale cached copies of THIS run's Component children so an edited
    # child reloads from the freshly-injected config (the engine only consults the
    # per-turn map on a cache miss, and its raw-config cache is never reset in the
    # long-lived server). The root is salted by the session-unique config_id, so it
    # never collides; only the bare-keyed children need eviction.
    if session.configs:
        fb.evict_raw_configs(list(session.configs.keys()), framework_root)
    # The opening turn has no user text; event_data may pre-fill on entry.
    engine_out = fb.run_engine(
        config, sm, last_user_text="", event_data=event_data,
        config_id=config_id, framework_root=framework_root,
        configs=_engine_configs(session),
    )
    session.sm = engine_out["sm"]
    _SESSIONS[session_id] = session
    result = _derive_result(session, engine_out)
    # The post-start state is the reset target: capture both sm and its render.
    session.initial_sm = fb.deep_copy_sm(session.sm)
    session.initial_result = fb.deep_copy_sm(result)
    session.initial_config = session.config
    session.initial_config_id = session.config_id
    return session_id, result


def _empty_result() -> dict[str, Any]:
    """A schema-valid default for an unknown session (graceful, 200).

    The frozen S0 contract test hits step/back/reset/visible-setters with a
    session id that no longer exists and asserts a schema-valid 2xx; an unknown
    session degrades to an empty in-progress step rather than an HTTP error.
    """
    return {
        "agent_text": "",
        "response_parts": [],
        "hide_tools": [],
        "function_call": None,
        "next_action": "next_question",
        "active_nodes": [],
        "slot_inspection": None,
        "sm": {},
        "status": "in_progress",
        "step_index": 0,
        "can_step_back": False,
    }


def _follow_flow_switch(session: "_Session", sm: dict[str, Any]) -> Optional[str]:
    """Hand the session to the FLOW a router just routed to.

    A router fills its gate slot with the flow the caller chose and then has
    nothing else to do -- it collects nothing and runs no task. The engine only
    swaps config for a COMPONENT descent (`_call_stack`), so without this the
    session stays on the host and every further turn re-offers the same choice:
    the caller picks a destination and never arrives.

    Live, CES does this outside the engine -- `before_agent` resolves the active
    flow to its config and invokes the engine with that config and the SAME sm.
    This is that, and no more: same sm, different config, applied from the next
    invocation, which is where a turn boundary puts it live.

    Resolution is by exact id, never by guessing. An agent maps flow types to
    configs through app-level state a DAG does not carry -- Bella Notte routes to
    `reservation`, whose config is `bella_notte` -- so an unresolvable target is
    left alone rather than matched to something that looks close. Returns the
    flow it switched to, or None.
    """
    gate = session.config.get("gate_slot")
    if isinstance(gate, str) and gate:
        # (A) On the ROUTER: route FORWARD to the flow the gate now names.
        filled = sm.get("filled")
        target = filled.get(gate) if isinstance(filled, dict) else None
        if not isinstance(target, str) or not target:
            return None
        # Resolve the gate value to a config id THROUGH the router's flow_config_map,
        # exactly as live `before_agent` does (`fmap.get(active_flow)`) — so many routes can
        # share one config. Absent a map, or a key it does not list, resolve by exact id as
        # before. The gate keeps `target` (the chosen route KEY), so a shared config still
        # sees its label.
        resolved = session.flow_config_map.get(target, target)
        child = session.configs.get(resolved)
        if child is None or child is session.config:
            return None
        session.config = child
        # The engine caches compiled configs by id, so the new config needs a new key
        # or it would run the host's compiled form under the child's content.
        session.config_id = f"{session.config_id}~{resolved}"
        return target
    # (B) On a gate-less CHILD that has now COMPLETED: hand back to the router the session
    #     STARTED on. Live, a router child's `reset_on_complete` re-arms the router and
    #     `before_agent` re-resolves the (now cleared) gate to it, so a multi-turn router
    #     session can route again; the sim mirrors that. Fires only for a router-rooted
    #     session (initial config has a gate) — component/plain-flow sessions are untouched.
    init = session.initial_config
    init_gate = init.get("gate_slot") if isinstance(init, dict) else None
    if (init is not session.config and isinstance(init_gate, str) and init_gate
            and _status(sm) in ("complete", "zombie")):
        session.config = init
        session.config_id = session.initial_config_id
        # Clear the stale gate value so the router re-routes on the next turn instead of
        # immediately re-entering the flow that just finished (live: reset_on_complete).
        if isinstance(sm.get("filled"), dict):
            sm["filled"].pop(init_gate, None)
        return session.flow_id or "__router__"
    return None


def step(req: dict[str, Any]) -> dict[str, Any]:
    """Advance a session one step (discriminated on `kind`). Returns a result dict.

    `req` is the validated `EngineStepRequest` as a plain dict (the router passes
    `model_dump()`). Unknown sessions degrade to an empty result.
    """
    session = _SESSIONS.get(req.get("session_id", ""))
    if session is None:
        return _empty_result()

    kind = req.get("kind")
    # Snapshot + advance BEFORE the step runs (the step mutates `sm` in place, so
    # there is nothing to capture afterwards) -- but undo both if the step raises.
    # A step that never completed must leave the session exactly as it was, not a
    # phantom history entry the caller can `back()` into and an inflated
    # `step_index`; only the unknown-kind branch used to roll them back.
    _push_history(session)
    session.step_index += 1
    turns_before = session.n_user_turns
    try:
        return _run_step(session, req, kind)
    except Exception:
        session.history.pop()
        session.step_index -= 1
        session.n_user_turns = turns_before
        raise


def _run_step(session: "_Session", req: dict[str, Any],
              kind: Optional[str]) -> dict[str, Any]:
    """One step's engine passes + render (history already pushed by `step`)."""
    # An agent HANDOFF, which intake reports and this module used to drop on the
    # floor. A multi-agent app routes by handing the caller to another AGENT, not
    # by switching flow: Bella Notte's `set_active_flow` returns
    # `target_agent: Reservation_Agent`. Dropped, the turn looked like nothing
    # happened and the caller was re-offered the same choice until the stall guard
    # fired -- with a message about routers that never mentioned the handoff.
    pending_transfer: Optional[str] = None

    cfg, sm, cid = session.config, session.sm, session.config_id
    root = session.framework_root
    cfgs = _engine_configs(session)  # None for single-flow; bundle map for components
    # A step the CALLER took, as opposed to the engine's own cascade. A setter call is
    # one: live the model only calls a setter because the caller just answered.
    if kind in ("setter_call", "confirm", "reject") or (
            kind == "user_text" and not req.get("is_inactivity")):
        session.n_user_turns += 1
        # Intent-first agents classify each new caller turn in a MODEL pass (Pass A:
        # SI rewritten to a classifier, every tool hidden but classify_turn_intent)
        # before the focused Pass B that actually collects. This module never calls an
        # LLM, so it cannot take that pass -- left to run it renders an empty turn with
        # nothing the caller can do. `_skip_pass_a_once` is the engine's own way to say
        # a turn is already classified as `continue`, which is what a caller answering
        # the question in front of them has done. It was previously got by accident: an
        # uncounted `n_user_turns` made every pass look like one already classified.
        # Saying it out loud is what lets the count be real.
        sm["_skip_pass_a_once"] = True
    turns = session.n_user_turns

    if kind == "user_text":
        _text = req.get("text", "")
        _inactivity = bool(req.get("is_inactivity"))
        # Carry the latest real utterance forward (live before_model scans it from
        # history). A silence step's own text is empty, so scanned_user_text keeps
        # the PRIOR real utterance — exactly what the intent-first Pass-B path sees
        # live (#517).
        if _text.strip() and not _inactivity:
            session.last_real_user_text = _text
        engine_out = fb.run_engine(
            cfg, sm, last_user_text=_text, config_id=cid, configs=cfgs,
            framework_root=root, is_inactivity=_inactivity,
            scanned_user_text=session.last_real_user_text, n_user_turns=turns,
        )
    elif kind in ("confirm", "reject"):
        # Readback commit/discard runs through the REAL framework tool
        # (confirm_pending/reject_pending), which mutates sm via an injected
        # context shim -- not via free text (the LLM normally calls these tools).
        tool = "confirm_pending" if kind == "confirm" else "reject_pending"
        fb.call_readback_tool(tool, sm, root)
        engine_out = fb.run_engine(cfg, sm, last_user_text="", config_id=cid,
                                   configs=cfgs, framework_root=root,
                                   n_user_turns=turns)
    elif kind == "setter_call":
        # `sm=` binds the CES-injected `context` global: a deployed setter may read
        # `context.state`, and unbound it dies on a NameError that reaches the caller
        # as a 502 rather than as anything about the agent.
        result = fb.call_setter(req["tool"], req.get("args", {}), root, sm=sm,
                                state=session.ces_state)
        intake_out = fb.run_intake(
            req["tool"], result, sm,
            current_agent=_DEFAULT_AGENT, channel=session.channel,
            framework_root=root,
        )
        sm = intake_out["sm"]
        pending_transfer = intake_out.get("pending_transfer") or None
        engine_out = fb.run_engine(cfg, sm, last_user_text="", config_id=cid,
                                   configs=cfgs, framework_root=root,
                                   n_user_turns=turns)
    elif kind == "task_result":
        # The intake executor branch keys off the TOOL name; map task -> tool.
        tool = _tool_for_task(session, req["task_name"])
        response_data = dict(req.get("result", {}))
        success_key = _success_check(session, req["task_name"])
        response_data.setdefault(success_key, bool(req.get("success", False)))
        intake_out = fb.run_intake(
            tool, response_data, sm,
            current_agent=_DEFAULT_AGENT, channel=session.channel,
            framework_root=root,
        )
        sm = intake_out["sm"]
        pending_transfer = intake_out.get("pending_transfer") or None
        engine_out = fb.run_engine(cfg, sm, last_user_text="", config_id=cid,
                                   configs=cfgs, framework_root=root,
                                   n_user_turns=turns)
    elif kind == "event_prefill":
        engine_out = fb.run_engine(
            cfg, sm, last_user_text="", event_data=req.get("event_data", {}),
            config_id=cid, configs=cfgs, framework_root=root, n_user_turns=turns,
        )
    else:  # pragma: no cover - guarded by the discriminated-union model
        # Unknown kind: do not advance; roll back the history push.
        session.history.pop()
        session.step_index -= 1
        return _derive_result(session, {"action": {}, "sm": sm})

    # After the engine has had its say: if a router just filled its gate, the next
    # invocation belongs to the flow it chose.
    switched = _follow_flow_switch(session, engine_out.get("sm") or sm)
    result = _derive_result(session, engine_out)
    if switched:
        result["switched_to_flow"] = switched
    if pending_transfer:
        result["pending_transfer"] = pending_transfer
    return result


def back(session_id: str) -> dict[str, Any]:
    """Pop a snapshot, restoring the EXACT prior sm + render (instant step-back).

    The snapshot is restored verbatim -- the engine is NOT re-run, so the prior
    sm is reproduced byte-for-byte (no `_invoke_n`/`_last_state` drift) and the
    step is instant (TDD section 11 Q2 -- snapshot, not replay). With no history
    (already at the start turn) the current state is returned unchanged.
    """
    session = _SESSIONS.get(session_id)
    if session is None:
        return _empty_result()
    if not session.history:
        return session.last_result or _empty_result()
    snap = session.history.pop()
    session.sm = fb.deep_copy_sm(snap.sm)
    session.step_index = snap.step_index
    session.n_user_turns = snap.n_user_turns
    session.ces_state = fb.deep_copy_sm(snap.ces_state)
    session.config = snap.config
    session.config_id = snap.config_id
    # Restore the render verbatim; refresh can_step_back for the new top-of-stack.
    result = fb.deep_copy_sm(snap.result)
    result["can_step_back"] = bool(session.history)
    session.last_result = result
    return result


def reset(session_id: str) -> dict[str, Any]:
    """Reset the session to its initial (post-start) sm + render; clear history.

    Restores the captured start-turn snapshot verbatim (no engine re-run), so a
    reset reproduces the exact opening state -- which means the session-lifetime
    turn count and CES `context.state` go back to their opening values too, or the
    reset conversation is not the opening one (see :class:`_Snapshot`).
    """
    session = _SESSIONS.get(session_id)
    if session is None:
        return _empty_result()
    session.sm = fb.deep_copy_sm(session.initial_sm)
    session.history = []
    session.step_index = 0
    session.n_user_turns = 0
    session.ces_state = {}
    session.config = session.initial_config
    session.config_id = session.initial_config_id
    result = fb.deep_copy_sm(session.initial_result)
    result["can_step_back"] = False
    session.last_result = result
    return result


def session_sm(session_id: str) -> dict[str, Any]:
    """The session's LIVE slot machine (not a snapshot). Empty for an unknown id.

    For a caller that has to run a tool the way CES would: `loader.call_setter(sm=...)`
    binds the injected `context` global to it, and a tool that WRITES through
    `context.state` must write into the state the next turn reads. The step results
    carry deep copies for exactly the opposite reason — they must not change under a
    later turn — so they are the wrong thing to bind.
    """
    session = _SESSIONS.get(session_id)
    return session.sm if session is not None else {}


def session_context_state(session_id: str) -> dict[str, Any]:
    """The session's CES `context.state`, for a caller running a tool the way CES
    would (`loader.call_setter(state=...)`). A fresh dict for an unknown id, which a
    tool may write to harmlessly."""
    session = _SESSIONS.get(session_id)
    return session.ces_state if session is not None else {}


def visible_setters(session_id: str) -> dict[str, Any]:
    """Derive enabled/greyed setters from the latest `hide_tools` + config.

    Re-runs the engine once on the current sm to obtain the up-to-date
    `hide_tools` (it is sm-dependent), then splits the config's setters. Returns
    `{visible, hidden}` (TDD section 4.4). Unknown session -> empty.
    """
    session = _SESSIONS.get(session_id)
    if session is None:
        return {"visible": [], "hidden": []}
    # Prefer the hide_tools from the latest rendered step (no engine re-run, so
    # the live threaded sm is untouched -- a read must not advance state). Fall
    # back to a throwaway-copy engine run only if no step has rendered yet.
    # Derive setters from the flow the engine is CURRENTLY in (the active child
    # during a Component descent), so the visible setters are the child's.
    active_config = _active_config(session, session.sm)
    hide_tools = (session.last_result or {}).get("hide_tools")
    if hide_tools is None:
        probe_sm = fb.deep_copy_sm(session.sm)
        engine_out = fb.run_engine(
            session.config, probe_sm, last_user_text="",
            config_id=session.config_id, configs=_engine_configs(session),
            framework_root=session.framework_root,
        )
        hide_tools = engine_out.get("action", {}).get("hide_tools", []) or []
    visible, hidden = fb.derive_visible_setters(active_config, hide_tools)
    return {"visible": visible, "hidden": hidden}


# --- Helpers -----------------------------------------------------------------


def _tool_for_task(session: _Session, task_name: str) -> str:
    """The executor tool name for a task (intake keys off the tool name)."""
    for tool, info in (session.sm.get("_executor_tasks", {}) or {}).items():
        if info.get("task_name") == task_name:
            return tool
    for task_def in session.config.get("tasks", []):
        if task_def.get("name") == task_name and task_def.get("tool"):
            return task_def["tool"]
    return task_name


def _success_check(session: _Session, task_name: str) -> str:
    """The success-check key for a task (defaults to 'success')."""
    for info in (session.sm.get("_executor_tasks", {}) or {}).values():
        if info.get("task_name") == task_name:
            return info.get("success_check", "success")
    for task_def in session.config.get("tasks", []):
        if task_def.get("name") == task_name:
            return task_def.get("success_check", "success")
    return "success"
