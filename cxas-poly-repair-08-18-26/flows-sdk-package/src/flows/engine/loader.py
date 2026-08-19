"""Import-loader shell for the real repo framework code (TDD section 3.3, S0).

This is the ONLY module that touches the repo framework's `python_code.py`
files. Everything else depends on its typed surface, so the coupling to repo
internals is confined here (Risk section 12).

S0 scope: implement the LOADING fully -- resolve a configurable framework root
(default `bella_notte/cxas_app/tools`, overridable via the
`SLOT_STUDIO_FRAMEWORK_ROOT` env var or a settings value) and `importlib`-load
`slot_filling_engine`, `validate_dag_config`, `slot_intake`, and any
`<id>_dag`/setter module by name, with caching. The thin
`load_engine()/load_validator()/load_intake()/load_dag(config_id)` loaders
return the loaded modules.

S1 scope (this file is the ONLY one that touches the framework): the typed call
wrappers that turn the engine/intake/setter modules into the offline analogue of
the live `before_model -> after_tool -> before_model` pipeline (TDD section 3.2).
These are deterministic, JSON-in/JSON-out functions:

- `seed_sm(config)`             -- replicate the one-time sm seeding the live
                                   before_model callback does (`_bootstrap`,
                                   `_cancel_tool`, `_gate_slot`, and registering
                                   the cancel setter in `_setter_slots`) so
                                   intake can route bootstrap/cancel calls. The
                                   engine itself only derives the plain
                                   setter/task maps, not these control keys.
- `run_engine(config, sm, ...)` -- one engine turn; returns `{action, sm}`.
- `run_intake(tool_name, ...)`  -- one intake reduction; returns the intake out.
- `call_setter(tool, args)`     -- run a real setter/task function offline so
                                   validation (out_of_range, etc.) is real.
- `load_config(config_id)`      -- load + call a `<id>_dag()` and return its dict.

All wrappers acquire NO lock themselves -- the caller (the router) serializes
them behind `state.engine_lock`, because the engine holds process-global caches
(`_COMPILED_CONFIGS`, `_sm_ref`) that must not be raced (TDD section 3.3).

The default root is resolved relative to the repo root that contains this
server package: `<repo_root>/bella_notte/cxas_app/tools`, where `<repo_root>` is
the directory four levels up from this file
(`server/slot_studio/framework_bridge.py` -> server -> slot_studio -> repo_root).
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import logging
import os
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

# Synthesized passive cancel slot the engine adds in `_compile_config`; the live
# before_model registers the cancel setter against it so intake can route a
# cancel_flow call. Mirrored here (see `seed_sm`). Kept in sync with the
# framework's `_CANCEL_SLOT` ("cancel").
_CANCEL_SLOT = "cancel"

# Env var override for the framework root (TDD D5).
ENV_FRAMEWORK_ROOT = "SLOT_STUDIO_FRAMEWORK_ROOT"

# Default framework root, relative to the repo root that holds the server.
# framework_bridge.py is at <repo>/slot_studio/server/slot_studio/, so the repo
# root is three .parent hops up from the package dir.
_PACKAGE_DIR = Path(__file__).resolve().parent              # .../slot_studio (pkg)
_SERVER_DIR = _PACKAGE_DIR.parent                            # .../server
_SLOT_STUDIO_DIR = _SERVER_DIR.parent                        # .../slot_studio
_REPO_ROOT = _SLOT_STUDIO_DIR.parent                         # repo root
_DEFAULT_FRAMEWORK_ROOT = _PACKAGE_DIR / "framework" / "tools"  # packaged blessed bundle

# Module cache: framework-root-string -> { tool_name -> loaded module }.
_MODULE_CACHE: dict[str, dict[str, ModuleType]] = {}

# Optional process-level settings override (lowest precedence after env).
_SETTINGS_FRAMEWORK_ROOT: Optional[str] = None


def set_framework_root(path: Optional[str]) -> None:
    """Set the settings-level framework root override (None clears it).

    Precedence (highest first): explicit `framework_root` arg to a loader >
    `SLOT_STUDIO_FRAMEWORK_ROOT` env var > this settings value > the default.
    """
    global _SETTINGS_FRAMEWORK_ROOT
    _SETTINGS_FRAMEWORK_ROOT = path


def default_framework_root() -> Path:
    """The zero-config default root (TDD D5), unaffected by env/settings."""
    return _DEFAULT_FRAMEWORK_ROOT


def resolve_framework_root(framework_root: Optional[str] = None) -> Path:
    """Resolve the active framework root per the precedence rules.

    Args:
      framework_root: explicit override; wins over env/settings/default.
    """
    chosen = (
        framework_root
        or os.environ.get(ENV_FRAMEWORK_ROOT)
        or _SETTINGS_FRAMEWORK_ROOT
    )
    if chosen:
        return Path(chosen).expanduser().resolve()
    return _DEFAULT_FRAMEWORK_ROOT


def _tool_python_code_path(root: Path, tool_name: str) -> Path:
    """Path to a tool's `python_function/python_code.py` under a tools root."""
    return root / tool_name / "python_function" / "python_code.py"


def _load_module(
    tool_name: str, framework_root: Optional[str] = None
) -> ModuleType:
    """`importlib`-load a single tool module by name, with caching.

    Each tool's `python_code.py` is a pure, stdlib-only module (TDD section 3.1),
    loaded under a unique synthetic module name so multiple tools coexist.
    """
    root = resolve_framework_root(framework_root)
    cache_key = str(root)
    per_root = _MODULE_CACHE.setdefault(cache_key, {})
    if tool_name in per_root:
        return per_root[tool_name]

    code_path = _tool_python_code_path(root, tool_name)
    if not code_path.is_file():
        raise FileNotFoundError(
            f"Framework tool '{tool_name}' not found under {root} "
            f"(expected {code_path})."
        )

    mod_name = f"slot_studio_framework__{tool_name}"
    spec = importlib.util.spec_from_file_location(mod_name, code_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create import spec for {code_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    per_root[tool_name] = module
    return module


def clear_cache() -> None:
    """Drop all cached framework modules (e.g. after a root change)."""
    _MODULE_CACHE.clear()


def materialize_tools_root(
    sources: Mapping[str, str], *, parent: Optional[str] = None
) -> str:
    """Build a tools root holding an APP's own tools over the framework bundle.

    Every loader here reads a tool from ``<root>/<tool>/python_function/python_code.py``,
    and the packaged root only holds the framework's own tools. A DEPLOYED agent's
    setters and executors are the *app's* tools, so simulating one offline fails on
    the first setter (``Framework tool 'set_active_flow' not found``). Give it a root
    that has both: the framework tools are symlinked in, then `sources`
    (``{tool name -> python source}``, e.g. from a CES fetch) is written on top.

    The app WINS on a name collision — an agent ships its own ``try_again`` /
    ``transfer_to_human``, and the deployed copy is the one whose behaviour we are
    trying to reproduce. The framework's engine/intake are not overridable this way
    because a CES fetch filters them out as infrastructure; they stay symlinked.

    The caller owns the returned directory (``tempfile.mkdtemp``) and should remove
    it when the session ends. Roots are cache keys, so one root per agent keeps two
    agents' same-named tools from colliding in `_MODULE_CACHE`.
    """
    root = Path(tempfile.mkdtemp(prefix="flows_tools_", dir=parent))
    for entry in _DEFAULT_FRAMEWORK_ROOT.iterdir():
        if entry.is_dir():
            (root / entry.name).symlink_to(entry, target_is_directory=True)
    for tool, source in sources.items():
        # A tool name is a directory name: refuse anything that could escape the root.
        if not tool or "/" in tool or "\\" in tool or tool.startswith("."):
            logger.warning(
                "materialize_tools_root: skipping unusable tool name %r", tool
            )
            continue
        slot = root / tool
        if slot.is_symlink():
            slot.unlink()
        (slot / "python_function").mkdir(parents=True, exist_ok=True)
        (slot / "python_function" / "python_code.py").write_text(source)
    return str(root)


def tool_parameters(tool: str, framework_root: Optional[str] = None) -> list[str]:
    """The parameter names a tool's callable actually accepts.

    A config names the SLOT a setter fills, which is not always the setter's
    parameter: a bootstrap declares ``slot: active_flow`` while the deployed
    ``set_active_flow`` takes ``flow``. Calling with the config's name raises
    ``TypeError`` and the simulated turn silently does nothing, so callers building
    a setter call must ask the function, not the config.
    """
    fn = load_tool_callable(tool, framework_root)
    return list(inspect.signature(fn).parameters)


def tool_signature(
    tool: str, framework_root: Optional[str] = None
) -> Mapping[str, inspect.Parameter]:
    """A tool callable's parameters, with their KINDS.

    :func:`tool_parameters` flattens to names, which cannot distinguish an ordinary
    argument from ``**kwargs``: ``f(**inputs)`` reports the single name ``inputs``
    and so reads as a function declaring one plain parameter. That distinction
    decides whether a tool is deployable at all — CES derives a tool's schema from
    its signature, and a ``**kwargs`` tool registers none, so it is silently not
    deployed (see :mod:`flows.authoring.setters`). Callers that must tell the two
    apart ask for the kinds; callers that only need names keep using the simpler
    function.
    """
    fn = load_tool_callable(tool, framework_root)
    return inspect.signature(fn).parameters


def evict_raw_configs(
    config_ids: list[str], framework_root: Optional[str] = None
) -> None:
    """Drop specific config ids from the loaded engine's process-global raw/compiled
    config caches (`_RAW_CONFIGS` / `_COMPILED_CONFIGS`).

    Concurrency contract: callers must serialize eviction calls behind the session
    lock (`state.engine_lock`) to prevent concurrent reads or compilation races on
    process-global engine state.

    The engine caches a Component child's raw config under its (stable) id and only
    consults the per-turn injected `configs=` map on a cache MISS. In the long-lived
    server the cache is never reset, so a re-run of the simulator after EDITING a
    child would serve the stale cached child. The Engine-mode sim evicts its child
    ids at session start so each run reloads the freshly-injected child config.
    """
    if not config_ids:
        return
    eng = load_engine(framework_root)
    # Check for an explicit eviction helper API method first, if provided by the engine.
    try:
        eng.evict_configs(config_ids)
        return
    except AttributeError:
        pass
    try:
        eng.evict_raw_configs(config_ids)
        return
    except AttributeError:
        pass

    # Fallback: reach into the engine's private process-global caches. If a framework
    # bump renames or restructures them, warn loudly (a silent no-op would serve
    # stale child configs after an edit) rather than fail quietly.
    try:
        raw = eng._RAW_CONFIGS
    except AttributeError:
        raw = None
    try:
        compiled = eng._COMPILED_CONFIGS
    except AttributeError:
        compiled = None
    if not isinstance(raw, dict) and not isinstance(compiled, dict):
        logger.warning(
            "evict_raw_configs: engine exposes neither _RAW_CONFIGS nor "
            "_COMPILED_CONFIGS as dicts (framework cache API may have changed); "
            "stale child configs may be served after an edit.")
        return
    if isinstance(raw, dict):
        for cid in config_ids:
            raw.pop(cid, None)
    if isinstance(compiled, dict):
        # _COMPILED_CONFIGS is keyed by (config_id, fingerprint); drop every entry
        # whose config_id is being evicted so it recompiles from the fresh raw.
        for key in [k for k in compiled if isinstance(k, tuple) and k and k[0] in config_ids]:
            compiled.pop(key, None)


# --- Thin typed loaders (the S0 deliverable). -------------------------------

def load_engine(framework_root: Optional[str] = None) -> ModuleType:
    """Load `slot_filling_engine` (exposes `slot_filling_engine(input_data)`)."""
    return _load_module("slot_filling_engine", framework_root)


def load_validator(framework_root: Optional[str] = None) -> ModuleType:
    """Load `validate_dag_config`.

    Exposes `validate_dag_config(input_data)`, `DagConfigValidator`,
    `CrossConfigValidator`.
    """
    return _load_module("validate_dag_config", framework_root)


def load_intake(framework_root: Optional[str] = None) -> ModuleType:
    """Load `slot_intake` (exposes `slot_intake(input_data)`)."""
    return _load_module("slot_intake", framework_root)


def load_dag(config_id: str, framework_root: Optional[str] = None) -> ModuleType:
    """Load a `<config_id>` DAG/setter tool module by name.

    `config_id` is the tool directory name, e.g. `bella_notte_dag`,
    `set_reservation_basics`. The caller invokes the contained function.
    """
    return _load_module(config_id, framework_root)


def framework_root_exists(framework_root: Optional[str] = None) -> bool:
    """True if the resolved framework root is a directory on disk."""
    return resolve_framework_root(framework_root).is_dir()


def load_tool_callable(
    tool_name: str, framework_root: Optional[str] = None
) -> Callable[..., Any]:
    """Return the public callable a tool module exposes.

    By convention a tool's `python_code.py` defines a top-level function named
    after its directory (e.g. `bella_notte_dag`, `set_active_flow`). Fall back to
    the single public callable when the names differ.
    """
    module = _load_module(tool_name, framework_root)
    fn = getattr(module, tool_name, None)
    if callable(fn):
        return fn
    candidates = [
        getattr(module, n)
        for n in dir(module)
        if not n.startswith("_") and callable(getattr(module, n))
        and getattr(getattr(module, n), "__module__", None) == module.__name__
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise AttributeError(
        f"Tool '{tool_name}' exposes no callable named '{tool_name}' and the "
        f"public callable is ambiguous ({len(candidates)} candidates)."
    )


def load_config(
    config_id: str, framework_root: Optional[str] = None
) -> dict[str, Any]:
    """Load a `<config_id>_dag` (or `<config_id>`) tool and return its config dict.

    Accepts either the bare flow id (`bella_notte`) or the full tool dir name
    (`bella_notte_dag`). The DAG function takes zero required args and returns a
    plain dict (TDD section 3.4).
    """
    tool_name = config_id if config_id.endswith("_dag") else f"{config_id}_dag"
    fn = load_tool_callable(tool_name, framework_root)
    try:
        cfg = fn()
    except TypeError:
        # Some DAG tools accept a CES-style `{}` arg; tolerate either signature.
        cfg = fn({})
    if not isinstance(cfg, dict):
        raise TypeError(f"DAG tool '{tool_name}' did not return a dict.")
    return cfg


# --- Typed call wrappers (the S1 deliverable). ------------------------------


def seed_sm(
    config: dict[str, Any], sm: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Seed a fresh sm with the control keys the live before_model installs once.

    The engine derives the plain setter/task maps on its first call, but the
    `_bootstrap`/`_cancel_tool`/`_gate_slot` control keys and the cancel-setter
    registration live in the before_model callback (not the engine). Intake needs
    them to route a bootstrap (`set_active_flow`) or cancel (`cancel_flow`) call,
    so the offline pipeline must install them too (TDD section 3.2 -- "exactly the
    after_tool -> before_model order the live system uses").

    Idempotent: if `sm` already carries `_config_id`, it is returned unchanged.
    """
    sm = sm if sm is not None else {}
    if sm.get("_config_id"):
        return sm
    sm["_config_id"] = "slot_studio"
    sm.setdefault("task_results", {})
    sm["_bootstrap"] = config.get("bootstrap")
    sm["_cancel_tool"] = (config.get("cancel") or {}).get("tool", "")
    sm["_gate_slot"] = config.get("gate_slot")

    setter_slots: dict[str, str] = {}
    multi_setter_slots: dict[str, dict[str, str]] = {}
    slot_requires: dict[str, list] = {}
    slot_validates: dict[str, Any] = {}
    for slot_def in config.get("slots", []):
        setter = slot_def.get("setter")
        setter_field = slot_def.get("setter_field")
        if setter:
            if setter_field:
                multi_setter_slots.setdefault(setter, {})[setter_field] = slot_def[
                    "name"
                ]
            else:
                setter_slots[setter] = slot_def["name"]
        if slot_def.get("requires"):
            slot_requires[slot_def["name"]] = slot_def["requires"]
        if slot_def.get("validate_against"):
            slot_validates[slot_def["name"]] = slot_def["validate_against"]
    if sm["_cancel_tool"]:
        setter_slots[sm["_cancel_tool"]] = _CANCEL_SLOT
    sm["_setter_slots"] = setter_slots
    sm["_multi_setter_slots"] = multi_setter_slots
    sm["_slot_requires"] = slot_requires
    sm["_slot_validates"] = slot_validates

    executor_tasks: dict[str, Any] = {}
    for task_def in config.get("tasks", []):
        tool = task_def.get("tool")
        if tool:
            info = {
                "task_name": task_def["name"],
                "inputs": task_def.get("inputs", []),
                "outputs": task_def.get("outputs", {}),
                "success_check": task_def.get("success_check", "success"),
                "terminal": task_def.get("terminal", False),
            }
            if info["terminal"]:
                info["then_say"] = task_def.get("then_say", "")
                info["then_response"] = task_def.get("then_response")
            executor_tasks[tool] = info
            # Kept in step with the engine's own seeding: a REMOTE task is answered by
            # two tools, and both resolve to the same task. Omitted here, an offline
            # drive silently loses the remote path while the live one keeps it — the
            # worst shape of divergence, since the tests would still be green.
            remote = (config.get("remote_tools") or {}).get(tool)
            if remote:
                info["remote"] = dict(remote)
                executor_tasks[remote["status_tool"]] = {
                    **info, "remote_status": True, "remote": dict(remote)}
    sm["_executor_tasks"] = executor_tasks
    return sm


def run_engine(
    config: dict[str, Any],
    sm: dict[str, Any],
    last_user_text: str = "",
    event_data: Optional[dict[str, Any]] = None,
    config_id: str = "slot_studio",
    framework_root: Optional[str] = None,
    configs: Optional[dict[str, dict[str, Any]]] = None,
    is_inactivity: bool = False,
    scanned_user_text: str = "",
    n_user_turns: int = 0,
    is_barge_in: bool = False,
    barge_heard: str = "",
    turn_kind: str = "",
) -> dict[str, Any]:
    """Run one engine turn offline; returns the raw `{action, sm}` dict.

    This is the offline analogue of the live before_model engine call -- direct
    function invocation with the plain `input_data` dict, no `{"input_data": ...}`
    CES wrap and no `.json()["result"]` unwrap (TDD section 3.2). The engine
    mutates `sm` in place AND returns it; callers thread the returned `sm`.

    `configs` is an optional `{config_id: raw DAG config}` map used to resolve a
    Component's CHILD config offline (no `tools` global) -- e.g. tests and the
    studio sim pass the parent plus every reachable child. Omitting it leaves
    every existing single-config call byte-for-byte unchanged.

    `n_user_turns` is how many turns the CALLER has taken. Live, before_model counts
    them off the conversation contents and the engine republishes the count as
    `sm["_turn_n"]`; every ladder measured in turns rather than retries reads it --
    the no-match backstop, and `awaits.max_turns`, which is the only thing between an
    asynchronous tool that never answers and a wedged call. An offline caller that
    does not supply it pins the engine at turn 0, where no such ladder can ever
    advance. Defaulted to 0, so a caller that does not count is unchanged.

    `turn_kind` is WHO took this turn -- "caller", "manufactured" (an inactivity tick or
    an asynchronous completion push) or "continuation" (another pass inside a turn that
    already happened). Live, before_model classifies it off the request contents, which is
    the only place it can be seen: the engine's own inputs cannot tell a completion push
    from a post-setter re-invoke. An offline caller that omits it gets the pre-existing
    reading, "manufactured" when `is_inactivity` is set and "caller" otherwise, so every
    existing call is unchanged.
    """
    engine = load_engine(framework_root)
    input_data: dict[str, Any] = {
        "raw_config": config,
        "sm": sm,
        "last_user_text": last_user_text,
        "is_inactivity": is_inactivity,
        "event_data": event_data or {},
        "config_id": config_id,
        "n_user_turns": n_user_turns,
    }
    # Barge-in, the live shape: before_model lifts the `<context>agent speaking was
    # interrupted …</context>` envelope into these two scalars. Passing them here lets an
    # offline caller exercise that path exactly. A caller that instead leaves the raw
    # envelope in `last_user_text` is also fine -- the engine matches it itself
    # (`_BARGE_ENVELOPE`) -- so both the live and the naive offline shapes are covered.
    if is_barge_in:
        input_data["is_barge_in"] = True
        input_data["barge_heard"] = barge_heard
    # The latest real user utterance scanned from history. Live, before_model
    # always supplies this; passing it here lets an offline caller (sim/tests)
    # faithfully exercise the intent-first Pass-B path that reads it (#517).
    if scanned_user_text:
        input_data["scanned_user_text"] = scanned_user_text
    if turn_kind:
        input_data["turn_kind"] = turn_kind
    if configs:
        input_data["configs"] = configs
    return engine.slot_filling_engine(input_data)


def run_intake(
    tool_name: str,
    response_data: dict[str, Any],
    sm: dict[str, Any],
    current_agent: str = "",
    channel: str = "",
    framework_root: Optional[str] = None,
    outcome: str = "",
) -> dict[str, Any]:
    """Apply a tool result to sm via the real `slot_intake` (the intake half).

    `outcome` mirrors what `before_model` passes when it ingests an async completion
    envelope ("completed" / "failed"); leave it empty for an ordinary tool result.

    Returns `{"sm", "transfer_slots", "pending_transfer"}` (TDD section 3.2).
    """
    intake = load_intake(framework_root)
    return intake.slot_intake({
        "tool_name": tool_name,
        "response_data": response_data,
        "sm": sm,
        "current_agent": current_agent,
        "channel": channel,
        "outcome": outcome,
    })


def call_setter(
    tool: str,
    args: dict[str, Any],
    framework_root: Optional[str] = None,
    sm: Optional[dict[str, Any]] = None,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run a real setter/task function offline with the author-supplied args.

    Validation (out_of_range, past_date, ...) is therefore real, not faked (TDD
    section 3.2). Returns the tool's raw result dict (the `response_data` intake
    consumes). A `args` value of `None`/`""` is dropped for fields whose setter
    omits-vs-empty distinction matters (matches the live tool-call shape where an
    unset parameter is simply absent).

    `sm` binds the CES-injected `context` global for the duration of the call, the
    same shim `call_readback_tool` uses (and with the same requirement that callers
    serialize: the binding lives on a shared cached module). CES injects `context`
    into EVERY tool, not only the readback pair, so a deployed setter is free to read
    `context.state` -- one reaches for the caller's account data to price a payment
    option. Without the binding that setter dies on `NameError: name 'context' is not
    defined`, which reaches the caller as a 502 saying nothing about the agent.
    Omitting `sm` leaves the call exactly as it was.

    `state` is where those tools WRITE. Live, `context.state` is the CES session
    state and outlives the call, and deployed tools talk to each other through it --
    one stores the appointment the caller picked for the next tool to read back.
    A shim built fresh per call swallows every such write, which is a quieter failure
    than the NameError it replaced. Pass a dict that lives as long as the session and
    the writes land where the next turn looks for them.
    """
    call_args = {k: v for k, v in (args or {}).items() if v is not None}
    if sm is None:
        result = load_tool_callable(tool, framework_root)(**call_args)
        return result if isinstance(result, dict) else {"result": result}
    # Binding `context` into the MODULE namespace mutates an object held in
    # `_MODULE_CACHE`, so it is safe only while no two callers can be inside this
    # block for the same module at once. Two things make that true, and both are
    # load-bearing rather than incidental:
    #
    #   * `_MODULE_CACHE` is keyed by ROOT, and a caller simulating a deployed agent
    #     takes a fresh root per session from `materialize_tools_root`. Two sessions
    #     therefore hold two different module objects for the same tool name.
    #   * A caller that DOES share one root across concurrent work must serialize
    #     itself. `uj_studio.live_sim` takes a single engine lock across every public
    #     method, so one session's steps never overlap.
    #
    # Sharing a root across threads without a lock would race here: the save/restore
    # below would interleave and a tool could observe another caller's context. If
    # that ever becomes a requirement, bind through a thread-local proxy instead.
    module = _load_module(tool, framework_root)
    fn = load_tool_callable(tool, framework_root)
    had_context = "context" in module.__dict__
    prior = module.__dict__.get("context")
    module.__dict__["context"] = _ContextShim(sm, state)
    try:
        result = fn(**call_args)
    finally:
        if had_context:
            module.__dict__["context"] = prior
        else:
            module.__dict__.pop("context", None)
    return result if isinstance(result, dict) else {"result": result}


class _ContextShim:
    """Minimal stand-in for the CES-injected `context` global.

    `confirm_pending`/`reject_pending` are framework tools that mutate sm through
    `context.state["sm"]` (they are NOT pure offline-callable functions like the
    setters). Offline we inject this shim into the tool module's namespace so the
    real framework readback logic runs against the session sm -- we never
    reimplement the commit/discard rules (TDD section 3 -- import, don't reimplement).
    """

    def __init__(self, sm: dict[str, Any],
                 state: Optional[dict[str, Any]] = None):
        # `state` is the caller's session-lifetime dict when there is one, so a tool
        # that writes through `context.state` is still writing there on the next turn.
        # `sm` is threaded into it because the framework readback tools address it as
        # `context.state["sm"]`; it is deliberately NOT stored the other way round,
        # which would make sm self-referential and a deep copy of it recursive.
        self.state = state if state is not None else {}
        self.state["sm"] = sm
        # CES injects `variables` beside `state`, and a deployed tool reads it: one
        # writes the last four digits of the appointment phone to both. Without the
        # attribute the tool dies on AttributeError, which an executor reports as the
        # tool having FAILED -- a fabricated failure. Kept inside `state` under a
        # reserved key so it has the same session lifetime without a second parameter.
        self.variables = self.state.setdefault("_ces_variables", {})


def call_readback_tool(
    tool: str, sm: dict[str, Any], framework_root: Optional[str] = None
) -> dict[str, Any]:
    """Run a readback tool (`confirm_pending`/`reject_pending`) against `sm`.

    These tools read/mutate the CES `context.state["sm"]` global rather than
    taking sm as an argument, so we temporarily bind a `context` shim in the tool
    module's namespace, call the real function, and restore the prior binding.
    Returns the tool's raw result dict (sm is mutated in place).

    NOT thread-safe: the temporary `context` binding lives on the shared cached
    module, so concurrent calls would clobber each other. Callers must serialize
    (Slot Studio does, behind `state.engine_lock`) — the engine's process-global
    caches require single-threaded access anyway.
    """
    module = _load_module(tool, framework_root)
    fn = getattr(module, tool, None)
    if not callable(fn):
        raise AttributeError(f"Readback tool '{tool}' exposes no '{tool}' callable.")
    had_context = "context" in module.__dict__
    prior = module.__dict__.get("context")
    module.__dict__["context"] = _ContextShim(sm)
    try:
        result = fn()
    finally:
        if had_context:
            module.__dict__["context"] = prior
        else:
            module.__dict__.pop("context", None)
    return result if isinstance(result, dict) else {"result": result}


def derive_visible_setters(
    config: dict[str, Any], hide_tools: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Split a config's setters into visible/hidden by the engine's `hide_tools`.

    Returns `(visible, hidden)` where visible items are `{tool, fields}` and
    hidden items are `{tool, reason}` (TDD section 4.4, UX section 15.4). Drives
    the Engine-mode setter-call builder. A single-slot setter contributes one
    field (its slot); a multi-field setter contributes its `setter_field`s.
    """
    hide = set(hide_tools or [])
    # tool -> ordered list of fields it fills.
    setter_fields: dict[str, list[str]] = {}
    for slot_def in config.get("slots", []):
        setter = slot_def.get("setter")
        if not setter:
            continue
        field = slot_def.get("setter_field") or slot_def["name"]
        setter_fields.setdefault(setter, [])
        if field not in setter_fields[setter]:
            setter_fields[setter].append(field)
    # The gate setter is keyed off bootstrap.tool, not a slot.setter.
    bootstrap = config.get("bootstrap") or {}
    gate_tool = bootstrap.get("tool")
    if gate_tool:
        setter_fields.setdefault(gate_tool, [bootstrap.get("slot", "flow")])
    cancel_tool = (config.get("cancel") or {}).get("tool")
    if cancel_tool:
        setter_fields.setdefault(cancel_tool, [])

    visible: list[dict[str, Any]] = []
    hidden: list[dict[str, str]] = []
    for tool, fields in setter_fields.items():
        if tool in hide:
            hidden.append({"tool": tool, "reason": "hidden_by_engine"})
        else:
            visible.append({"tool": tool, "fields": fields})
    return visible, hidden


def deep_copy_sm(sm: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy an sm snapshot (for history/step-back). JSON-able dict."""
    return copy.deepcopy(sm)
