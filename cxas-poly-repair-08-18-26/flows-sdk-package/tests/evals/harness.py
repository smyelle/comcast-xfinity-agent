"""Offline behavioural-eval harness for the apps under `examples/`.

This is the engine for the *regression* suite (as opposed to the build-only guarantee in
`test_examples.py`). It drives an example `App` through the deterministic, LLM-free
Engine-mode simulator (`flows.sim.engine_sim`) and grades each turn against hand-authored
expectations. No LLM, no network, no GCP: the exact same offline path the framework's own
tests use (`test_filed_issue_fixes.py`, `test_multi_agent.py`).

An eval spec is one YAML file per example (`examples/evals/<name>.eval.yaml`) holding many
`scenarios`; see `EVAL_FORMAT.md` in this directory for the schema. Each scenario is a list
of turns; every turn is one of:

    - say:    "<text>"                 # a caller utterance (engine_sim `user_text`)
    - silence: true                    # a silent/inactivity turn (drives the no_input ladder)
    - answer: {slot: <name>, value: X} # answer the asked slot via its generated setter
    - task_result: {task: <name>, success: bool, result: {...}}   # inject a tool result
    - confirm: true | reject: true     # commit / discard a readback
    - event:  {<slot>: <value>, ...}   # an on-entry event prefill

After every turn the harness *auto-drains* fired executors: when the engine returns a
`fire` action for a tool that has a declared `tool_fakes` entry, it injects that fake as a
`task_result` and re-runs, exactly as the deployed platform would run the tool body. A fire
with no declared fake is an ERROR (the instrument is incomplete), never a silent PASS.

Verdicts mirror the ces-probes model: PASS (all expectations held), FAIL (an expectation was
contradicted), ERROR (the harness could not run the scenario — no action, missing fake,
unknown setter). ERROR is never folded into PASS.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import sys
import tempfile
import types
from dataclasses import dataclass, field
from typing import Any, Optional

import flows
from flows.authoring import build as _build
from flows.engine import loader as fb
from flows.sim import engine_sim

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(os.path.dirname(_HERE))          # packages/flows
_EXAMPLES_DIR = os.path.join(_PKG_ROOT, "examples")
_EVALS_DIR = os.path.join(_EXAMPLES_DIR, "evals")

PASS, FAIL, ERROR = "PASS", "FAIL", "ERROR"

# A fired executor with a declared fake is re-driven until the engine stops firing; this
# caps a pathological fire-loop so a broken app fails loudly instead of hanging CI.
_MAX_DRAIN = 12


# --- example discovery -------------------------------------------------------


def _defines_module_level_app(source: str) -> bool:
    """True if the module assigns a top-level `app` (plain OR annotated), by AST.

    AST rather than a regex so it is unfazed by inline comments (`app = X  # import`),
    whitespace, and typed assignments (`app: flows.App = ...`), and so a re-export
    `from examples.x import app` — an `ImportFrom`, not an assignment — is never mistaken
    for a definition (that is how the `*_drive` helpers pull one in). Import-free by design:
    this runs at pytest COLLECTION time and must not populate the global `@flows.tool`
    registry (which would break other tests). A syntactically broken example is left for
    `test_examples.py` to fail on, not silently dropped here.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:                      # top-level statements only
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "app" for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "app" \
                    and node.value is not None:
                return True
    return False


def _load(name: str) -> types.ModuleType:
    """Import an example by file path WITHOUT triggering its `__main__` build block.

    Load-once: an already-loaded example is returned from `sys.modules` rather than re-exec'd.
    Some examples register process-global state at import (a remote agent's operations, an
    A2A/openapi toolset) and RAISE on a second import — so a re-exec would break, and every
    consumer (discovery, test_examples, the eval runner) must share the one module.
    """
    modname = f"_flows_example_{name}"
    if modname in sys.modules:
        return sys.modules[modname]
    path = os.path.join(_EXAMPLES_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(modname, path)
    assert spec and spec.loader, path
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # so @flows.tool return models resolve at build
    spec.loader.exec_module(mod)
    return mod


_DISCOVERED: Optional[list[str]] = None


def discover_app_examples() -> list[str]:
    """Every `examples/<name>.py` that defines a deployable app as module-level `app`.

    SOURCE-ONLY (no import): an example is identified by a top-level `app = ...` assignment
    that is not a re-export `from ... import app` (how the `*_drive` helpers pull one in). This
    is deliberately import-free — it runs at pytest COLLECTION time (to parametrize the suites),
    and importing every example then would populate the process-global `@flows.tool` registry
    before other tests run, breaking any test that defines a same-named tool. Examples are
    imported lazily, inside test bodies, via `load_app`/`_load`. Shared with `test_examples.py`
    so the two suites can never disagree about what an example is.
    """
    global _DISCOVERED
    if _DISCOVERED is not None:
        return _DISCOVERED
    names: list[str] = []
    for fname in sorted(os.listdir(_EXAMPLES_DIR)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        with open(os.path.join(_EXAMPLES_DIR, fname), encoding="utf-8") as fh:
            if _defines_module_level_app(fh.read()):
                names.append(fname[:-3])
    _DISCOVERED = names
    return names


def load_app(name: str) -> flows.App:
    return _load(name).app


# --- simulator context -------------------------------------------------------


@dataclass
class _Ctx:
    """Everything a driven session needs, assembled once per scenario."""

    config: dict[str, Any]
    configs: Optional[dict[str, dict[str, Any]]]
    framework_root: str
    setter_of: dict[str, str]                  # slot name -> generated setter tool name
    tmp_root: str = ""                          # temp parent of framework_root, to clean up
    session_id: str = ""


def _build_ctx(app: flows.App) -> _Ctx:
    """Assemble the deployed config + a materialized tools root, exactly as `build_app` emits.

    `_assemble` lowers the app to `(all_configs, tool_bodies, _available)`; the bodies are the
    real generated setters + `@flows.tool` executors, so app setters/executors actually run
    offline (a bare framework root would `FileNotFoundError` on the first app setter). The
    temp parent is recorded on the ctx so `run_scenario` can delete it afterwards.
    """
    all_map, bodies, _available = _build._assemble(app)
    tmp_root = tempfile.mkdtemp(prefix="flows_eval_")
    root = fb.materialize_tools_root(bodies, parent=tmp_root)
    cfg = all_map[app.config_id]
    configs = all_map if len(all_map) > 1 else None
    return _Ctx(config=cfg, configs=configs, framework_root=root, setter_of={},
                tmp_root=tmp_root)


# --- turn driving ------------------------------------------------------------


def _spoken(result: dict[str, Any]) -> str:
    """All caller-facing text of a turn: the message plus any text response parts."""
    parts = [result.get("agent_text") or ""]
    for part in result.get("response_parts") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text") or "")
    return "\n".join(p for p in parts if p).strip()


def _end_reason(result: dict[str, Any]) -> Optional[str]:
    for part in result.get("response_parts") or []:
        if isinstance(part, dict) and part.get("type") == "end_session":
            return part.get("reason")
    return None


def _task_for_tool(ctx: _Ctx, tool: str) -> Optional[str]:
    sm = engine_sim.session_sm(ctx.session_id)
    info = (sm.get("_executor_tasks") or {}).get(tool)
    return info.get("task_name") if info else None


def _drain_fires(ctx: _Ctx, result: dict[str, Any], tool_fakes: dict[str, Any],
                 fired: list[str], fired_args: dict[str, list[dict]]) -> dict[str, Any]:
    """While the engine wants a tool fired, inject its declared fake and re-run.

    Records every fired tool in `fired`, and appends each call's args to
    `fired_args[tool]` (a LIST, so a tool fired twice in one turn keeps both calls —
    `tool_args` then matches if ANY call satisfies it). Captured HERE because the terminal
    result after the drain no longer carries the function_call. A fire whose tool has no
    `tool_fakes` entry raises `_HarnessError` — the eval author must declare what the backend
    returns.
    """
    for _ in range(_MAX_DRAIN):
        fc = result.get("function_call")
        if not fc or result.get("next_action") != "fire":
            return result
        tool = fc.get("name")
        fired.append(tool)
        fired_args.setdefault(tool, []).append(fc.get("args") or {})
        task = _task_for_tool(ctx, tool)
        if task is None:
            # A framework control tool (cancel/transfer) fired — not an app executor to fake.
            return result
        if tool not in tool_fakes:
            raise _HarnessError(
                f"tool {tool!r} fired but no tool_fakes entry declared for it")
        fake = tool_fakes[tool] or {}
        result = engine_sim.step({
            "session_id": ctx.session_id, "kind": "task_result", "task_name": task,
            "success": bool(fake.get("success", True)),
            "result": {k: v for k, v in fake.items() if k != "success"},
        })
    raise _HarnessError(f"tool fire did not settle after {_MAX_DRAIN} injections")


def _resolve_setter(ctx: _Ctx, slot: str) -> str:
    """The generated setter tool for a user slot, from the live `_setter_slots` map."""
    if not ctx.setter_of:
        sm = engine_sim.session_sm(ctx.session_id)
        ctx.setter_of = {v: k for k, v in (sm.get("_setter_slots") or {}).items()}
    setter = ctx.setter_of.get(slot)
    if setter is None:
        raise _HarnessError(
            f"no setter for slot {slot!r}; known: {sorted(ctx.setter_of)}")
    return setter


def _drive_turn(ctx: _Ctx, turn: dict[str, Any], tool_fakes: dict[str, Any]
                ) -> tuple[dict[str, Any], list[str], dict[str, list[dict]]]:
    """Issue the one engine_sim step a turn describes, then drain any fired executors."""
    fired: list[str] = []
    fired_args: dict[str, list[dict]] = {}
    if "say" in turn:
        result = engine_sim.step({"session_id": ctx.session_id, "kind": "user_text",
                                  "text": str(turn["say"])})
    elif "silence" in turn:
        # A silent (inactivity) turn: what the `no_input` reprompt ladder measures against.
        result = engine_sim.step({"session_id": ctx.session_id, "kind": "user_text",
                                  "text": "", "is_inactivity": True})
    elif "answer" in turn:
        spec = turn["answer"]
        slot = spec["slot"]
        setter = _resolve_setter(ctx, slot)
        args = spec.get("args") or {slot: spec.get("value")}
        result = engine_sim.step({"session_id": ctx.session_id, "kind": "setter_call",
                                  "tool": setter, "args": args})
    elif "task_result" in turn:
        spec = turn["task_result"]
        result = engine_sim.step({
            "session_id": ctx.session_id, "kind": "task_result",
            "task_name": spec["task"], "success": bool(spec.get("success", True)),
            "result": spec.get("result", {})})
    elif "confirm" in turn:
        result = engine_sim.step({"session_id": ctx.session_id, "kind": "confirm"})
    elif "reject" in turn:
        result = engine_sim.step({"session_id": ctx.session_id, "kind": "reject"})
    elif "event" in turn:
        result = engine_sim.step({"session_id": ctx.session_id, "kind": "event_prefill",
                                  "event_data": turn["event"]})
    else:
        raise _HarnessError(f"turn has no recognized kind: {sorted(turn)}")
    result = _drain_fires(ctx, result, tool_fakes, fired, fired_args)
    return result, fired, fired_args


# --- expectations ------------------------------------------------------------


def _ask_texts(ctx: _Ctx, slot: str) -> list[str]:
    for slot_def in ctx.config.get("slots", []):
        if slot_def.get("name") == slot:
            ask = slot_def.get("ask")
            if isinstance(ask, str):
                return [ask]
            if isinstance(ask, list):
                return [a for a in ask if isinstance(a, str)]
    return []


def _check(ctx: _Ctx, clause: dict[str, Any], result: dict[str, Any],
           fired: list[str], fired_args: dict[str, list[dict]]) -> Optional[str]:
    """Return a failure string if `clause` does not hold against the turn, else None."""
    if not isinstance(clause, dict) or len(clause) != 1:
        raise _HarnessError(
            f"an expectation must be a single-key mapping, got {clause!r}")
    (kind, want), = clause.items()
    said = _spoken(result)
    sm = result.get("sm") or {}
    filled = sm.get("filled") or {}

    if kind == "said_contains":
        return None if str(want).lower() in said.lower() else \
            f"said_contains {want!r} not in {said!r}"
    if kind == "said_equals":
        return None if said == want else f"said_equals {want!r} != {said!r}"
    if kind == "asked_slot":
        asks = _ask_texts(ctx, want)
        if not asks:
            return f"asked_slot {want!r}: slot has no `ask` in config"
        return None if any(a.lower() in said.lower() for a in asks) else \
            f"asked_slot {want!r}: none of {asks} in {said!r}"
    if kind == "tool_called":
        return None if want in fired else f"tool_called {want!r} not in fired {fired}"
    if kind == "no_tools_called":
        return None if not fired else f"no_tools_called but fired {fired}"
    if kind == "tool_not_called":
        # The assertion a negative scenario usually wants: a task that COULD have run
        # and did not. `no_tools_called` is too blunt for a turn where other tools fire.
        return None if want not in fired else f"tool_not_called {want!r} but fired {fired}"
    if kind == "tool_args":                       # {tool: name, args: {...}}; matches ANY call
        calls = fired_args.get(want.get("tool"), [])
        exp = want.get("args", {})
        if any(all(call.get(k) == v for k, v in exp.items()) for call in calls):
            return None
        return f"tool_args {want.get('tool')!r} expected {exp}, calls were {calls}"
    if kind == "slot_filled":                     # {slot: value}
        bad = {k: (filled.get(k)) for k, v in want.items() if filled.get(k) != v}
        return None if not bad else f"slot_filled expected {want}, got {bad} (filled={filled})"
    if kind == "slot_status":                     # {slot: open|filled|pending|deferred}
        by = {s["name"]: s["status"] for s in
              (result.get("slot_inspection") or {}).get("slots", [])}
        bad = {k: by.get(k) for k, v in want.items() if by.get(k) != v}
        return None if not bad else f"slot_status expected {want}, got {bad}"
    if kind == "next_action":
        got = result.get("next_action")
        return None if got == want else f"next_action {want!r} != {got!r}"
    if kind == "status":
        got = result.get("status")
        return None if got == want else f"status {want!r} != {got!r}"
    if kind == "disposition":                     # complete|transfer|escalate|cancel|handoff
        return _check_disposition(result, want)
    if kind == "active_flow":
        got = result.get("switched_to_flow") or result.get("active_config_id")
        return None if got == want else f"active_flow {want!r} != {got!r}"
    return f"unknown expectation {kind!r}"


def _check_disposition(result: dict[str, Any], want: str) -> Optional[str]:
    """A flow's disposition is its terminal `outcome`, carried in sm (`_zombie` / exit_status).

    A control block ends the flow as status `zombie` with an `outcome` — `escalated`,
    `cancelled`, or (for a plain terminal task) `completed`/absent. `transfer_to` names the
    live-agent target on an escalate/handoff. `status == complete` is the explicit-completion
    path. These are the robust signals; `end_session` parts are not always present offline.
    """
    status = result.get("status")
    sm = result.get("sm") or {}
    zombie = sm.get("_zombie") or {}
    outcome = (zombie.get("outcome")
               or zombie.get("exit_status", {}).get("flow_outcome")
               or sm.get("exit_status", {}).get("flow_outcome"))
    transfer_to = zombie.get("transfer_to") or result.get("pending_transfer")
    reason = _end_reason(result)
    terminal = status in ("complete", "zombie", "escalated")

    got = None
    if want == "complete":
        got = "complete" if terminal and outcome not in ("escalated", "cancelled") else None
    elif want == "escalate":
        got = "escalate" if (outcome == "escalated" or status == "escalated"
                             or reason == "escalate") else None
    elif want == "cancel":
        got = "cancel" if outcome == "cancelled" else None
    elif want == "transfer":
        got = "transfer" if (reason == "transfer" or transfer_to) else None
    elif want == "handoff":
        got = "handoff" if transfer_to else None
    return None if got == want else (
        f"disposition {want!r}: status={status} outcome={outcome!r} "
        f"transfer_to={transfer_to!r} end_reason={reason!r}")


# --- scenario runner ---------------------------------------------------------


class _HarnessError(Exception):
    """The instrument broke — surfaced as an ERROR verdict, never a FAIL/PASS."""


@dataclass
class ScenarioResult:
    name: str
    verdict: str                       # PASS | FAIL | ERROR
    failures: list[str] = field(default_factory=list)
    error: Optional[str] = None
    transcript: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == PASS

    def summary(self) -> str:
        if self.verdict == ERROR:
            return f"ERROR: {self.error}\n" + "\n".join(self.transcript)
        if self.verdict == FAIL:
            return ("FAIL:\n  " + "\n  ".join(self.failures) + "\n--- transcript ---\n"
                    + "\n".join(self.transcript))
        return "PASS"


def run_scenario(app: flows.App, scenario: dict[str, Any]) -> ScenarioResult:
    """Drive one scenario end to end and grade every turn. Isolated session per call."""
    name = scenario.get("name", "unnamed")
    tool_fakes = scenario.get("tool_fakes") or {}
    result = ScenarioResult(name=name, verdict=PASS)
    engine_sim.reset_store()
    ctx: Optional[_Ctx] = None
    try:
        ctx = _build_ctx(app)
        sid, start_result = engine_sim.start(
            ctx.config, flow_id=app.config_id, configs=ctx.configs,
            framework_root=ctx.framework_root,
            event_data=scenario.get("seed") or None)
        ctx.session_id = sid
        result.transcript.append(f"[start] {_spoken(start_result)!r}")
        # Expectations may attach to the opening turn via `on_start`.
        for clause in scenario.get("on_start") or []:
            fail = _check(ctx, clause, start_result, [], {})
            if fail:
                result.failures.append(f"on_start: {fail}")

        for i, turn in enumerate(scenario.get("turns") or []):
            turn_result, fired, fired_args = _drive_turn(ctx, turn, tool_fakes)
            label = next((k for k in ("say", "silence", "answer", "task_result",
                                      "confirm", "reject", "event") if k in turn), "?")
            result.transcript.append(
                f"[{i}:{label}] fired={fired} say={_spoken(turn_result)!r}")
            for clause in turn.get("expect") or []:
                fail = _check(ctx, clause, turn_result, fired, fired_args)
                if fail:
                    result.failures.append(f"turn {i} ({label}): {fail}")
    except _HarnessError as exc:
        result.verdict = ERROR
        result.error = str(exc)
        return result
    except Exception as exc:                       # any engine/build explosion is an ERROR
        result.verdict = ERROR
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        fb.clear_cache()
        if ctx is not None and ctx.tmp_root:
            shutil.rmtree(ctx.tmp_root, ignore_errors=True)

    if result.failures:
        result.verdict = FAIL
    return result


# --- authoring aid (dev only) ------------------------------------------------


def record(name: str, turns: list[dict[str, Any]]) -> None:
    """Print the engine trace for a scripted run, to help HAND-AUTHOR expectations.

    Never writes eval files — goldens are hand-written (see the plan). Usage:
        python -m tests.evals.harness <example>   # drives a trivial say-loop probe
    """
    app = load_app(name)
    res = run_scenario(app, {"name": "record", "turns": turns})
    print(f"# {name}: {res.verdict}")
    for line in res.transcript:
        print(line)
    if res.error:
        print("ERROR:", res.error)


if __name__ == "__main__":                         # pragma: no cover - dev aid
    _name = sys.argv[1] if len(sys.argv) > 1 else "ask_ladder"
    _probe = [{"say": s} for s in sys.argv[2:]] or [{"say": "hello"}]
    record(_name, _probe)
