"""Pre-push gates: everything we can prove about an app before spending a deploy on it.

``run(req, env)`` runs six checks over the effective app tree (``validate_dag``,
``tool_bodies``, ``setter_shape``, ``callback_sync``, ``dup_uuid``, ``docstring_sig``)
and returns a :class:`PrePushReport` with a create-vs-update ``target``.

The effective tree is sourced from ``req.app_files`` (hosted/forced whole-app
payload) when present, else read from disk under ``env.agent_dir`` + the framework
root (local). Every check reuses an existing, blessed primitive: ``validation``
(the real ``validate_dag_config``), ``blessed_source.callbacks()`` for the byte-sync
check, a ``tools/*/*.json`` ``name``-UUID scan, and ``blessed_source.docstring_args``
/ ``sig_args_from_source`` for docstring<->sig.

Two of the six are ADVISORY by default and the reasoning is worth keeping: an
existing CES app the author didn't write here legitimately ships callbacks from a
different framework version, and CES does not enforce docstring<->signature parity —
neither should block updating somebody else's app. ``setter_shape`` joins them unless
the caller asks for ``strict`` (Specter, which authors every tool it ships).

This was ``slot_studio/studio/prepush.py``; it moved down into `flows` so Specter and
slotfill_migration stop reaching through a FastAPI router to gate their own deploys.
The only thing it lost in the move is the process-global ``state.settings`` — the
ambient bits now arrive as :class:`flows.deploy.env.DeployEnv`.

Never raises (crash-envelope): any internal failure is surfaced as a failing
check, not an exception.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from ..config import config_io, models, tool_refs, tool_scan, validation
from ..engine import blessed_source
from ..engine import loader as framework_bridge
from .env import DeployEnv
from .models import PrePushCheck, PrePushReport

# Matches a tool's python source path: tools/<name>/python_function/python_code.py
_TOOL_PY_RE = re.compile(r"^tools/([^/]+)/python_function/python_code\.py$")


def _is_dag_toolname(name: str) -> bool:
    """A DAG config tool: the ``<id>_dag`` convention OR the canonical ``dag_config`` name (used by
    single-flow agents, e.g. an in-place round-trip that keeps the source's tool name)."""
    return name.endswith("_dag") or name == "dag_config"

logger = logging.getLogger(__name__)

# The four framework callbacks, in the on-disk agent layout. Each agent carries a
# byte-identical copy at agents/<Agent>/<cb>_callbacks/<cb>_callbacks_01/python_code.py.
_CALLBACK_NAMES = ("before_agent", "before_model", "after_model", "after_tool")


# ===========================================================================
# Effective-tree assembly (hosted app_files OR on-disk local).
# ===========================================================================


def _files_from_app_files(req) -> Optional[dict[str, str]]:
    """``{app-root-relative path -> content}`` from a hosted whole-app payload.

    Returns None when the request carries no ``app_files`` (local mode).
    """
    app_files = getattr(req, "app_files", None)
    if not app_files:
        return None
    out: dict[str, str] = {}
    for f in app_files:
        # ScaffoldFile (pydantic) or a plain dict, both with path/content.
        path = getattr(f, "path", None) if not isinstance(f, dict) else f.get("path")
        content = (
            getattr(f, "content", None) if not isinstance(f, dict) else f.get("content")
        )
        if path is not None:
            out[str(path)] = content if content is not None else ""
    return out


def _app_root(env: DeployEnv) -> Optional[Path]:
    """The on-disk app root (parent of ``tools/``) for local mode.

    Prefers ``env.agent_dir``; falls back to the parent of the resolved framework
    root (``.../tools`` -> app root).
    """
    agent_dir = env.agent_dir
    if agent_dir:
        return Path(agent_dir)
    try:
        root = framework_bridge.resolve_framework_root(env.framework_root)
    except Exception:  # pragma: no cover - resolver failure -> no local root
        return None
    root = Path(root)
    # framework_root is the tools/ dir; the app root is its parent.
    if root.name == "tools":
        return root.parent
    return root


def _tools_dir(env: DeployEnv) -> Optional[Path]:
    """The on-disk ``tools/`` directory for local mode."""
    try:
        root = framework_bridge.resolve_framework_root(env.framework_root)
    except Exception:  # pragma: no cover
        root = None
    if root:
        root = Path(root)
        if root.is_dir():
            return root
    app_root = _app_root(env)
    if app_root is not None:
        cand = app_root / "tools"
        if cand.is_dir():
            return cand
    return None


# ===========================================================================
# Check 1: validate_dag (the real validate_dag_config, green => ok).
# ===========================================================================


def _dag_config_id(req, files: Optional[dict[str, str]]) -> Optional[str]:
    """The DAG tool dir name (``<config_id>_dag``). From ``req.config_id`` (append
    ``_dag`` if absent) else the lone ``tools/*_dag/`` dir in the payload."""
    cid = getattr(req, "config_id", None)
    if cid:
        return cid if cid.endswith("_dag") else f"{cid}_dag"
    if files:
        dags = sorted(
            m.group(1) for p in files if (m := _TOOL_PY_RE.match(p)) and _is_dag_toolname(m.group(1))
        )
        if len(dags) == 1:
            return dags[0]
    return None


# Framework builtins that are ALWAYS available at runtime but ship no tool py file
# (the platform provides them), so they won't appear in the rendered payload.
_FRAMEWORK_BUILTINS = ("end_session", "set_active_flow")

# A tool resource with no python body at all. Several `tool_type` oneof branches are
# body-less because the PLATFORM makes the call, not the sandbox: an agent addressed by
# resource name, an A2A agent addressed by URL, and Google Search. They are perfectly
# callable, and scanning for python files alone reads every one of them as a tool that
# does not exist — so a config firing one failed this gate with nothing wrong.
_TOOL_JSON_RE = re.compile(r"^tools/([^/]+)/\1\.json$")
_BODYLESS_TOOL_KEYS = ("agentTool", "remoteAgentTool", "googleSearchTool")


def _bodyless_tool_names(files: dict[str, str]) -> set[str]:
    """Tool names declared by a body-less resource JSON in the payload."""
    found: set[str] = set()
    for path, content in files.items():
        m = _TOOL_JSON_RE.match(path)
        if not m:
            continue
        try:
            resource = json.loads(content)
        except (TypeError, ValueError):
            continue
        if isinstance(resource, dict) and any(
                k in resource for k in _BODYLESS_TOOL_KEYS):
            found.add(m.group(1))
    return found


def _available_tools_from_files(files: Optional[dict[str, str]]) -> Optional[list[str]]:
    """Tool names whose source is actually present in the rendered payload (every
    ``tools/<name>/python_function/python_code.py``), PLUS the framework builtins that ship no py
    file (``end_session``, ``set_active_flow``). This is GROUND TRUTH for the validator's
    tool-availability check — a config that references a business tool with no file here is
    incomplete and MUST fail. ``None`` only when there are no app_files (local mode, where the
    validator falls back to the on-disk framework scan)."""
    if not files:
        return None
    tools = {m.group(1) for p in files if (m := _TOOL_PY_RE.match(p))}
    tools.update(_FRAMEWORK_BUILTINS)
    tools.update(_bodyless_tool_names(files))
    return sorted(tools)


def _config_dict(req, files: Optional[dict[str, str]]) -> Optional[dict[str, Any]]:
    """The DAG config dict to validate.

    Prefers ``req.config`` (the working DAG the UI holds). For a hosted/whole-app
    payload with no inline ``config``, imports the ``<config_id>_dag`` source out of
    ``app_files`` so the gate ALWAYS has a config to validate (no vacuous skip).
    """
    cfg = getattr(req, "config", None)
    if cfg is not None:
        return validation._to_plain(cfg)
    dag = _dag_config_id(req, files)
    if files and dag:
        src = files.get(f"tools/{dag}/python_function/python_code.py")
        if src:
            try:
                return config_io.import_from_source(src)
            except Exception as exc:  # best-effort; a render bug surfaces below
                logger.warning("prepush: could not import dag config from app_files: %s", exc)
    return None


def _all_dag_configs(files: Optional[dict[str, str]]) -> dict[str, dict]:
    """Every ``tools/*_dag/`` config in the payload, imported from source. A multi-flow app has
    more than one (a host router + per-flow configs); each must validate."""
    out: dict[str, dict] = {}
    if not files:
        return out
    for p, src in files.items():
        m = _TOOL_PY_RE.match(p)
        if not m or not _is_dag_toolname(m.group(1)):
            continue
        try:
            out[m.group(1)] = config_io.import_from_source(src)
        except Exception as exc:  # a render bug surfaces as an invalid/absent config below
            logger.warning("prepush: could not import %s: %s", m.group(1), exc)
    return out


def _check_validate_dag(req, files, env):

    # Tool-availability (task/setter tool must have code in the payload) is enforced only
    # for a STRICT push (Specter, which authors every tool). An existing HOSTED app being
    # re-pushed (e.g. just changing the model) legitimately references tools that live in
    # CES but aren't in Slot Studio's local payload — enforcing it there blocks a push the
    # user didn't break (same reasoning as callback_sync/docstring_sig being advisory).
    # Structural DAG errors still block in both modes.
    strict = bool(getattr(req, "strict", False))

    cfg = _config_dict(req, files)
    if cfg is None:
        # No single resolvable config. For a MULTI-FLOW app (host + several *_dag flows) there
        # isn't one — validate EVERY flow config instead of failing vacuously.
        dags = _all_dag_configs(files)
        if dags:
            available_tools = _available_tools_from_files(files) if strict else None
            bad = []
            for name, dcfg in sorted(dags.items()):
                try:
                    valid, errors, _w = validation.raw_validate_single(
                        dcfg, available_tools=available_tools)
                except Exception as exc:
                    valid, errors = False, [f"validator crashed: {exc}"]
                if not (valid and not errors):
                    bad.append(f"{name}: {'; '.join(errors[:3]) if errors else 'invalid'}")
            if bad:
                return PrePushCheck(id="validate_dag", ok=False, detail=" | ".join(bad[:5]))
            return PrePushCheck(id="validate_dag", ok=True,
                                detail=f"All {len(dags)} flow config(s) are valid.")
        # A whole-app payload with no resolvable config is itself a defect — do NOT
        # pass vacuously. Only a pure local run (no app_files) legitimately defers.
        if files:
            return PrePushCheck(
                id="validate_dag",
                ok=False,
                detail="No DAG config found in the app payload (the *_dag tool is "
                "missing or unparseable).",
            )
        return PrePushCheck(
            id="validate_dag",
            ok=True,
            detail="No config supplied; skipped DAG validation (local mode).",
        )
    try:
        setter_sources = None
        try:
            setter_sources = tool_scan.read_setter_sources(cfg)
        except Exception:  # source-aware extras are optional
            setter_sources = None
        # Derive available_tools from the rendered payload so the validator's
        # tool-availability check runs (it early-returns when None) — this catches a
        # config that references setters/executors with no code. Only in STRICT mode: an
        # existing hosted app re-push may reference CES-native tools not in the payload.
        available_tools = _available_tools_from_files(files) if strict else None
        valid, errors, _warnings = validation.raw_validate_single(
            cfg, available_tools=available_tools, setter_sources=setter_sources
        )
    except Exception as exc:
        logger.warning("validate_dag check crashed: %s", exc)
        return PrePushCheck(
            id="validate_dag", ok=False, detail=f"Validator crashed: {exc}"
        )
    if valid and not errors:
        return PrePushCheck(id="validate_dag", ok=True, detail="DAG config is valid.")
    detail = "; ".join(errors[:5]) if errors else "DAG config is invalid."
    return PrePushCheck(id="validate_dag", ok=False, detail=detail)


# ===========================================================================
# Check 1b: tool_bodies — referenced tools have a REAL implementation.
# A tool dir can exist with an empty/stub body (file-exists != implemented); that
# passes the availability check but is a runtime no-op. Parse each referenced
# tool's source and flag empty / stub-only (`pass` / `...` / docstring-only) bodies.
# ===========================================================================


def _is_stub_source(src: str) -> bool:
    """True when `src` has no real top-level function (unparseable, no def, or every
    top-level function body is trivially empty: only pass / ... / a docstring)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return True
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not funcs:
        return True

    def _trivial(fn: ast.AST) -> bool:
        # Drop bare constant expressions (docstrings / `...`); what remains is the
        # real body. Empty or pass-only => trivial stub.
        body = [
            s for s in fn.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        ]
        return not body or all(isinstance(s, ast.Pass) for s in body)

    return all(_trivial(f) for f in funcs)


def _check_tool_bodies(req, files, env):
    cfg = _config_dict(req, files)
    if not cfg or not files:
        return PrePushCheck(
            id="tool_bodies", ok=True, detail="No config/app payload to inspect."
        )
    # The SHARED walk (flows.config.tool_refs) — the same enumeration codegen builds
    # from and the push gate checks against. This check used to carry its own copy
    # that missed `cancel.tool` / `escalate.tool`, so a custom cancel tool with an
    # empty body sailed through here and then failed at runtime.
    referenced: set[str] = set(tool_refs.referenced_tools(cfg))
    # Only inspect tools whose source is IN the payload; a missing file is the
    # availability check's job (validate_dag), not a stub.
    stubs = sorted(
        name for name in referenced
        if (src := files.get(f"tools/{name}/python_function/python_code.py")) is not None
        and _is_stub_source(src)
    )
    if stubs:
        return PrePushCheck(
            id="tool_bodies",
            ok=False,
            detail=(
                "Tool(s) referenced by the flow have no real implementation "
                f"(empty/stub body): {', '.join(stubs)}. Author the tool code or "
                "remove the reference."
            ),
        )
    return PrePushCheck(
        id="tool_bodies", ok=True, detail="All referenced tools have implementations."
    )


# ===========================================================================
# Check 1c: setter_shape — user-slot setters must return the framework's
# stored-envelope, else the slot never fills and the agent loops. Passes
# validate_dag but is source-opaque to it, so checked here. Advisory by default,
# blocking under strict. Scoped to slots[].setter (non-task) tools only.
# ===========================================================================


def _check_setter_shape(req, files, env):

    cfg = _config_dict(req, files)
    if not cfg or not files:
        return PrePushCheck(
            id="setter_shape", ok=True, detail="No config/app payload to inspect."
        )
    # user-slot setters: a slot with a setter whose source is NOT a task.
    offenders: list[str] = []
    for s in cfg.get("slots", []) or []:
        if not isinstance(s, dict):
            continue
        setter = s.get("setter")
        if not setter or str(s.get("source") or "user").startswith("task:"):
            continue
        src = files.get(f"tools/{setter}/python_function/python_code.py")
        if src is None:
            continue  # missing source is validate_dag's job
        if '"stored"' not in src and "'stored'" not in src:
            offenders.append(f"{setter} (fills '{s.get('name', '?')}')")
    if offenders:
        return PrePushCheck(
            id="setter_shape",
            ok=False,
            detail=(
                "User-slot setter(s) don't return the framework shape "
                '{"stored": True, "value": ...} so the slot never fills (the agent '
                f"would loop): {', '.join(sorted(offenders))}."
            ),
        )
    return PrePushCheck(
        id="setter_shape", ok=True, detail="User-slot setters return the stored-shape."
    )


def _check_lint(req, files, env):
    """Voice-hazard lint over the emitted config.

    Only the rules that can be decided from the config alone and describe an AUDIBLE
    defect, not the whole linter: a push gate is the wrong place to argue about copy
    style, and `report.ok` counts error-severity checks only, so a broad selection
    would either block on taste or be ignored.

    Runs `run_rules` rather than `lint_app`: the gate holds emitted FILES, and
    `lint_app` starts from an `App` it would have to re-assemble. `LintContext` takes
    the config dicts directly, so rules must `getattr`-default anything off `ctx.app`.
    """
    from ..lint.context import LintContext
    from ..lint.runner import run_rules

    cfg = _config_dict(req, files)
    if not cfg:
        return PrePushCheck(id="lint", ok=True, detail="No config to lint.")
    cid = getattr(req, "config_id", None) or "config"
    report = run_rules(
        LintContext(app=None, configs={cid: cfg}, bodies={}, available=[]),
        select=["FLV002", "FLV003"],
    )
    live = [f for f in report.findings if not f.suppressed_by]
    errors = [f for f in live if f.severity == "error"]
    if not live:
        return PrePushCheck(id="lint", ok=True, detail="No voice hazards found.")
    return PrePushCheck(
        id="lint",
        ok=not errors,
        severity="error" if errors else "warning",
        detail="; ".join(f"{f.code}: {f.message}" for f in live),
    )


# ===========================================================================
# Check 2: callback_sync (all 4 callbacks byte-identical to blessed source).
# ===========================================================================


def _agent_callback_bytes_from_files(
    files: dict[str, str],
) -> dict[str, list[tuple[str, bytes]]]:
    """``cb_name -> [(agent, bytes), ...]`` from a hosted file map."""
    out: dict[str, list[tuple[str, bytes]]] = {n: [] for n in _CALLBACK_NAMES}
    for path, content in files.items():
        norm = path.replace("\\", "/")
        for cb in _CALLBACK_NAMES:
            marker = f"/{cb}_callbacks/"
            if (
                norm.startswith("agents/")
                and marker in norm
                and norm.endswith("python_code.py")
            ):
                agent = norm.split("/")[1]
                out[cb].append((agent, (content or "").encode("utf-8")))
    return out


def _agent_callback_bytes_from_disk(
    app_root: Path,
) -> dict[str, list[tuple[str, bytes]]]:
    """``cb_name -> [(agent, bytes), ...]`` read from the on-disk agent tree."""
    out: dict[str, list[tuple[str, bytes]]] = {n: [] for n in _CALLBACK_NAMES}
    agents_dir = app_root / "agents"
    if not agents_dir.is_dir():
        return out
    for agent_dir in sorted(p for p in agents_dir.iterdir() if p.is_dir()):
        for cb in _CALLBACK_NAMES:
            path = (
                agent_dir
                / f"{cb}_callbacks"
                / f"{cb}_callbacks_01"
                / "python_code.py"
            )
            if path.is_file():
                out[cb].append((agent_dir.name, path.read_bytes()))
    return out


def _check_callback_sync(req, files, env):

    try:
        blessed = blessed_source.callbacks()
    except Exception as exc:
        logger.warning("callback_sync: could not load blessed callbacks: %s", exc)
        return PrePushCheck(
            id="callback_sync", ok=False, detail=f"Blessed source unavailable: {exc}"
        )

    if files is not None:
        present = _agent_callback_bytes_from_files(files)
    else:
        app_root = _app_root(env)
        if app_root is None or not app_root.is_dir():
            return PrePushCheck(
                id="callback_sync",
                ok=False,
                detail="No app directory to check callbacks against.",
            )
        present = _agent_callback_bytes_from_disk(app_root)

    drift: list[str] = []
    checked = 0
    for cb in _CALLBACK_NAMES:
        want = blessed.get(cb, b"")
        copies = present.get(cb, [])
        for agent, data in copies:
            checked += 1
            if data != want:
                drift.append(f"{agent}/{cb}")
    if not checked:
        return PrePushCheck(
            id="callback_sync",
            ok=False,
            detail="No agent callbacks found to verify.",
        )
    if drift:
        return PrePushCheck(
            id="callback_sync",
            ok=False,
            detail="Callbacks drifted from blessed source: " + ", ".join(drift[:6]),
        )
    return PrePushCheck(
        id="callback_sync",
        ok=True,
        detail=f"All {checked} agent callbacks match blessed source.",
    )


# ===========================================================================
# Check 3: dup_uuid (no two tools/*/*.json share a `name` UUID -> 404 cause).
# ===========================================================================


def _tool_json_entries(req, files, env) -> list[tuple[str, dict[str, Any]]]:
    """``[(tool_label, parsed_json), ...]`` for every ``tools/*/*.json``.

    The tool label is the tool dir name. Unparseable JSON is skipped.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    if files is not None:
        for path, content in files.items():
            norm = path.replace("\\", "/")
            parts = norm.split("/")
            # tools/<tool>/<tool>.json (the tool-def, not python_code.py)
            if (
                len(parts) == 3
                and parts[0] == "tools"
                and parts[2].endswith(".json")
            ):
                try:
                    out.append((parts[1], json.loads(content or "{}")))
                except Exception:
                    continue
        return out

    tools_dir = _tools_dir(env)
    if tools_dir is None:
        return out
    for tool_dir in sorted(p for p in tools_dir.iterdir() if p.is_dir()):
        json_path = tool_dir / f"{tool_dir.name}.json"
        if not json_path.is_file():
            continue
        try:
            out.append((tool_dir.name, json.loads(json_path.read_text("utf-8"))))
        except Exception:
            continue
    return out


def _check_dup_uuid(req, files, env):

    entries = _tool_json_entries(req, files, env)
    by_uuid: dict[str, list[str]] = {}
    for label, data in entries:
        name = data.get("name") if isinstance(data, dict) else None
        if not name:
            continue
        by_uuid.setdefault(str(name), []).append(label)

    collisions = {uid: tools for uid, tools in by_uuid.items() if len(tools) > 1}
    if not collisions:
        return PrePushCheck(
            id="dup_uuid",
            ok=True,
            detail=f"All {len(by_uuid)} tool UUIDs are unique.",
        )

    parts = [
        f"{uid} shared by {', '.join(sorted(tools))}"
        for uid, tools in sorted(collisions.items())
    ]
    fix = models.DiagnosticFix(
        label="Mint a fresh UUID for one of the colliding tools",
        patch={"op": "mint_uuid", "value": str(uuid.uuid4())},
    )
    return PrePushCheck(
        id="dup_uuid",
        ok=False,
        detail="Duplicate tool UUID (push 404 'Tools not found'): " + "; ".join(parts),
        fix=fix,
    )


# ===========================================================================
# Check 4: docstring_sig (every tool's docstring Args <-> signature).
# ===========================================================================


def _tool_code_sources(req, files, env) -> list[tuple[str, str]]:
    """``[(tool_label, python_code), ...]`` for every tool's python_code.py."""
    out: list[tuple[str, str]] = []
    if files is not None:
        for path, content in files.items():
            norm = path.replace("\\", "/")
            parts = norm.split("/")
            if (
                len(parts) == 4
                and parts[0] == "tools"
                and parts[2] == "python_function"
                and parts[3] == "python_code.py"
            ):
                out.append((parts[1], content or ""))
        return out

    tools_dir = _tools_dir(env)
    if tools_dir is None:
        return out
    for tool_dir in sorted(p for p in tools_dir.iterdir() if p.is_dir()):
        code_path = tool_dir / "python_function" / "python_code.py"
        if code_path.is_file():
            out.append((tool_dir.name, code_path.read_text("utf-8")))
    return out


def _check_docstring_sig(req, files, env):

    sources = _tool_code_sources(req, files, env)
    mismatches: list[str] = []
    checked = 0
    for label, src in sources:
        documented = blessed_source.docstring_args(src)
        signature = blessed_source.sig_args_from_source(src)
        # Only tools with a documented Args: block are subject to the parity gate
        # (a function with no Args: section documents nothing, which is fine).
        if not documented:
            continue
        checked += 1
        if documented != signature:
            extra = sorted(documented - signature)
            missing = sorted(signature - documented)
            bits = []
            if extra:
                bits.append(f"doc-only {extra}")
            if missing:
                bits.append(f"sig-only {missing}")
            mismatches.append(f"{label}: {'; '.join(bits)}")
    if mismatches:
        return PrePushCheck(
            id="docstring_sig",
            ok=False,
            detail="Docstring Args <-> signature mismatch: " + "; ".join(mismatches[:6]),
        )
    return PrePushCheck(
        id="docstring_sig",
        ok=True,
        detail=f"Docstrings match signatures ({checked} tools checked).",
    )


# ===========================================================================
# Aggregator + create-vs-update target.
# ===========================================================================


def _target(req):
    """``(target, target_label)`` from whether a deployed app id is known."""
    deployed = getattr(req, "deployed_app_id", None)
    if deployed:
        return "update", str(deployed)
    label = (
        getattr(req, "display_name", None)
        or getattr(req, "config_id", None)
        or "new app"
    )
    return "create", str(label)


def run(req, env: Optional[DeployEnv] = None) -> PrePushReport:
    """Run the pre-push gates for ``req`` (a :class:`PrePushRequest`).

    ``env`` supplies the on-disk fallbacks the local-mode checks need; omit it for a
    pure whole-app payload (``req.app_files``), where nothing reads disk. Never
    raises: a failure inside any check becomes that check's ``ok=False`` detail.
    """
    env = env or DeployEnv()
    files = _files_from_app_files(req)

    check_fns = [
        _check_validate_dag,
        _check_tool_bodies,
        _check_setter_shape,
        _check_callback_sync,
        _check_dup_uuid,
        _check_docstring_sig,
        _check_lint,
    ]

    checks = []
    for fn in check_fns:
        try:
            checks.append(fn(req, files, env))
        except Exception as exc:  # crash-envelope per check
            logger.warning("prepush check %s crashed: %s", fn.__name__, exc)
            cid = fn.__name__.replace("_check_", "")
            checks.append(
                PrePushCheck(id=cid, ok=False, detail=f"Check crashed: {exc}")
            )

    # callback_sync + docstring_sig are ADVISORY: an existing CES app legitimately
    # ships callbacks from a different framework version, and CES doesn't enforce
    # docstring<->signature — neither should block updating an app the user didn't
    # author here. dup_uuid (404 cause) + validate_dag (broken DAG) stay blocking.
    # setter_shape is advisory by default; strict (Specter) makes it blocking.
    #
    # `lint` is deliberately NOT here: it sets its own severity per finding — warning
    # for a model-dependent audio tag, error only for the one that truncates the
    # utterance — and listing it would flatten that back to advisory, which is the
    # whole thing the gate exists to prevent.
    _WARN = {"callback_sync", "docstring_sig"}
    if not getattr(req, "strict", False):
        _WARN = _WARN | {"setter_shape"}
    checks = [
        c.model_copy(update={"severity": "warning"}) if c.id in _WARN else c
        for c in checks
    ]
    target, target_label = _target(req)
    ok = all(c.ok for c in checks if c.severity == "error")
    if not ok:
        blocking = [(c.id, c.detail) for c in checks if c.severity == "error" and not c.ok]
        logger.warning("prepush BLOCKED — failing checks: %s", blocking)
    return PrePushReport(ok=ok, checks=checks, target=target, target_label=target_label)


# `classify_push_error` lives in `flows.deploy.argv` (it reads push OUTPUT, not the app
# tree). Re-exported here because every caller reached for it through this module.
from .argv import classify_push_error  # noqa: E402,F401
