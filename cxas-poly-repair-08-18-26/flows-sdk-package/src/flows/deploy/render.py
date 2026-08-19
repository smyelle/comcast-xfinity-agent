"""Config -> app_files: fold the authored flow into the app that gets pushed.

This is the half of the push that has nothing to do with subprocesses, CES or HTTP:
given a whole-app file set and one or more configs, produce the file set that should
actually ship. It was ~250 lines inside a FastAPI route handler, which is why Specter
and slotfill_migration each had to call the route function to get at it.

Every function here is pure (files in, files out) and deterministic: the same
(app_files, config) always renders the same bytes, which is what makes the
three-product parity test possible.

The guarantees the refusals encode — all three learned from a push that "didn't take":

* a dag is resolved from the app itself, never fabricated
  (:class:`~flows.deploy.errors.DagUnresolvedError`);
* a failed render raises rather than silently shipping the stale dag
  (:class:`~flows.deploy.errors.RenderFailedError`);
* the rendered dag is self-checked to contain every slot the author wrote.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Iterable, Optional, Sequence

from ..config import config_io, models, tool_refs, validation
from ..emit.models import ScaffoldFile
from .errors import DagUnresolvedError, RenderFailedError
from .models import PushConfigEntry

logger = logging.getLogger(__name__)

_DAG_CODE_RE = re.compile(r"^tools/([^/]+_dag)/python_function/python_code\.py$")
_AGENT_JSON_RE = re.compile(r"^agents/[^/]+/[^/]+\.json$")


# ---------------------------------------------------------------------------
# File-set helpers (a "file" is a ScaffoldFile or a {path, content} dict).
# ---------------------------------------------------------------------------

def _path(f: Any) -> str:
    return f["path"] if isinstance(f, dict) else f.path


def _content(f: Any) -> str:
    return f["content"] if isinstance(f, dict) else f.content


def _as_scaffold_files(files: Iterable[Any]) -> list[ScaffoldFile]:
    return [ScaffoldFile(**f) if isinstance(f, dict) else f for f in files]


def code_path(tool: str) -> str:
    """Where a tool's python body lives in an app file set."""
    return f"tools/{tool}/python_function/python_code.py"


def bare_to_dag(bare: str) -> str:
    """``checkout`` -> ``checkout_dag`` (idempotent)."""
    return bare if bare.endswith("_dag") else f"{bare}_dag"


def mint_tool_json(tool_name: str, description: str = "") -> dict[str, Any]:
    """A fresh ``<tool>.json`` body: new UUID name, no ``parameters`` block.

    CES derives a tool's call schema from the python signature, so a ``parameters``
    block here is at best redundant and at worst wrong.
    """
    return {
        "name": str(uuid.uuid4()),
        "pythonFunction": {
            "name": tool_name,
            "pythonCode": code_path(tool_name),
            "description": description,
        },
        "displayName": tool_name,
    }


# ---------------------------------------------------------------------------
# Dag resolution + single-config render.
# ---------------------------------------------------------------------------

def resolve_dag_tool(app_files: Sequence[Any], config_id: Optional[str]) -> str:
    """Which EXISTING ``*_dag`` tool in the app the canvas config renders into.

    Resolution (never fabricates a tool that isn't in the app):
      1. config_id's tool, when it actually exists in the app.
      2. else, if the app has exactly ONE ``*_dag`` tool, that one (so a missing
         config_id still lands on the real dag for single-flow apps).
      3. else (multi-dag app, config_id names no existing tool) → refuse:
         raise DagUnresolvedError. Fabricating ``{id}_dag`` here is exactly the
         bug where the edit rendered into a stray tool CES ignores.

    Returns the resolved (existing) dag tool name.
    """
    paths = [_path(f) for f in app_files]
    if config_id:
        cand = bare_to_dag(config_id)
        if code_path(cand) in paths:
            return cand
    existing = sorted({m.group(1) for p in paths if (m := _DAG_CODE_RE.match(p))})
    if len(existing) == 1:
        return existing[0]
    raise DagUnresolvedError(existing)


def render_one(dag: str, config: Any) -> str:
    """Render one config into its dag python (with the slot self-check). Shared by
    the single-config and multi-DAG bundle render paths."""
    raw = config.model_dump(by_alias=True, exclude_none=True)
    try:
        cfg = config_io.import_config(config_id=dag, raw_dict=raw)
        rendered = config_io.export_python(cfg, dag)
    except Exception as exc:
        logger.warning("push: rendering config into %s failed: %s", dag, exc)
        raise RenderFailedError(str(exc)) from exc
    # Self-check: the author's slot names must appear in the rendered dag.
    slot_names = [s.get("name") for s in (raw.get("slots") or []) if s.get("name")]
    missing = [n for n in slot_names if n not in rendered]
    if missing:
        raise RenderFailedError(f"rendered dag {dag} is missing slots: {missing}")
    return rendered


def render_canvas_into_app_files(
    app_files: Sequence[Any], config: Any, config_id: Optional[str]
) -> tuple[list[Any], Optional[str]]:
    """Render the CURRENT canvas config into the app's real ``*_dag`` python so
    canvas edits (new slots, etc.) actually ship. Returns ``(files, dag)``.

    ``config is None`` is a legitimate no-op (a re-push of an unchanged app): the
    caller's own sequence comes back, unwrapped and unmodified.
    """
    if config is None:
        return app_files, None
    dag = resolve_dag_tool(app_files, config_id)  # may raise DagUnresolvedError
    rendered = render_one(dag, config)  # may raise RenderFailedError

    target = code_path(dag)
    out: list[Any] = []
    for f in app_files:
        if _path(f) == target:
            out.append(ScaffoldFile(path=target, content=rendered))
        else:
            out.append(f if not isinstance(f, dict) else ScaffoldFile(**f))
    return out, dag


# ---------------------------------------------------------------------------
# Multi-DAG BUNDLE push: render the root + every Component child into its own
# ``<id>_dag`` tool, create/register child dags as needed, verify referenced
# tools exist, and gate on cross-config validation. BARE ids throughout.
# ---------------------------------------------------------------------------

def referenced_setter_task_tools(entries: Sequence[PushConfigEntry]) -> set[str]:
    """Every tool any bundle config CALLS: slot setters, task executors, the control
    blocks (bootstrap/cancel/escalate/correction/intent_change) and on-exhaust
    handlers. Excludes component refs and ``*_dag`` flows (those are rendered, not
    gathered) and reserved framework tools (they ship with the blessed bundle).

    One walk, shared with codegen and the pre-push gates
    (:func:`flows.config.tool_refs.referenced_tools`). It used to be a local copy
    that was the ONLY one aware of ``cancel``/``escalate``, so a custom cancel tool
    was demanded here and generated nowhere.
    """
    out: set[str] = set()
    for e in entries:
        raw = e.config.model_dump(by_alias=True, exclude_none=True)
        for name in tool_refs.referenced_tools(raw):
            if not name.endswith("_dag"):
                out.add(name)
    return out


def missing_tools(app_files: Sequence[Any], referenced: set[str]) -> list[str]:
    """Referenced setter/task tools not present in the app (V1 same-app scope)."""
    paths = {_path(f) for f in app_files}
    return sorted(name for name in referenced if code_path(name) not in paths)


def cross_validate_bundle(
    entries: Sequence[PushConfigEntry],
) -> models.CrossValidationReport:
    """Run the framework CrossConfigValidator over the BARE-keyed bundle map
    (component ref/io/cycle/depth checks)."""
    configs = {e.config_id: e.config for e in entries}
    return validation.validate_cross(models.ValidateCrossRequest(configs=configs))


def register_dag_in_agents(files: Sequence[Any], dag_id: str, anchor_dag: str) -> list[ScaffoldFile]:
    """Add ``dag_id`` to the ``tools[]`` of the agent(s) that already host the
    parent flow (i.e. already list ``anchor_dag``, the root dag), so a created
    child flow tool is callable by the agent that references it — NOT bolted onto
    every agent in the app. Falls back to all agents only if none list the anchor
    (so the child is never left unregistered). Idempotent."""
    def _agent_lists(content: str, dag: str) -> bool:
        try:
            tools = json.loads(content).get("tools")
            return isinstance(tools, list) and dag in tools
        except (json.JSONDecodeError, TypeError):
            return False

    agent_paths = [(_path(f), _content(f)) for f in files]
    owners = {
        p for p, c in agent_paths
        if _AGENT_JSON_RE.match(p) and _agent_lists(c, anchor_dag)
    }
    # No agent claims the anchor (e.g. the root dag itself is new) -> register
    # everywhere rather than leave the child unreachable.
    register_all = not owners

    out: list[ScaffoldFile] = []
    for f in files:
        path, content = _path(f), _content(f)
        is_agent = bool(_AGENT_JSON_RE.match(path))
        if is_agent and (register_all or path in owners):
            try:
                data = json.loads(content)
                tools = data.get("tools")
                if isinstance(tools, list) and dag_id not in tools:
                    data["tools"] = sorted([*tools, dag_id])
                    content = json.dumps(data, indent=2)
            except (json.JSONDecodeError, TypeError):
                pass  # leave a malformed agent json untouched
        out.append(ScaffoldFile(path=path, content=content))
    return out


def render_bundle_into_app_files(
    app_files: Sequence[Any], entries: Sequence[PushConfigEntry], root_config_id: str
) -> tuple[list[ScaffoldFile], str]:
    """Render each bundle config into its ``<id>_dag`` tool. Existing dags are
    overwritten; absent ones (create-new-inline children) are created (python +
    minted json) and registered in the root-owning agent's ``tools[]``. Returns
    ``(files, root_dag)``. Per-config render failure raises (no partial ship)."""
    root_dag = bare_to_dag(root_config_id)
    files = _as_scaffold_files(app_files)
    path_set = {f.path for f in files}
    for e in entries:
        dag = bare_to_dag(e.config_id)
        rendered = render_one(dag, e.config)  # may raise RenderFailedError
        target = code_path(dag)
        if target in path_set:
            files = [
                ScaffoldFile(path=f.path, content=(rendered if f.path == target else f.content))
                for f in files
            ]
        else:
            # create-when-absent: code + minted json + register on the root-owning agent.
            files.append(ScaffoldFile(path=target, content=rendered))
            files.append(ScaffoldFile(
                path=f"tools/{dag}/{dag}.json",
                content=json.dumps(mint_tool_json(dag, "Component sub-DAG flow."), indent=2),
            ))
            files = register_dag_in_agents(files, dag, root_dag)
            path_set = {f.path for f in files}
    return files, root_dag
