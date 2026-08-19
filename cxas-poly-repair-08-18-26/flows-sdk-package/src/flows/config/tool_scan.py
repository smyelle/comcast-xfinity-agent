"""Discover framework tools under a framework root (TDD section 4.1 / 7, S2).

The framework root (default `bella_notte/cxas_app/tools`, TDD D5) is a directory
of tool subdirs, each holding `python_function/python_code.py`. This module
scans that layout to provide:

  * `discover_dag_configs()`   -> the multi-flow `*_dag` tools (Project view).
  * `discover_available_tools()` -> every tool name, for `available_tools`
    validation (the validator checks setter/task tool references against it).
  * `read_setter_sources()` / `read_task_tool_sources()` -> maps of
    `tool_name -> python source text`, feeding the validator's source-aware
    output-key checks (TDD section 3.2; `DagConfigValidator(setter_sources=...,
    task_tool_sources=...)`).

All discovery is rooted at the resolved framework root from `framework_bridge`
(read-only; we never import the tool modules here, just list dirs / read text).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..engine import loader as fb

# A tool dir is recognised by holding this file under it.
_PY_CODE_REL = ("python_function", "python_code.py")

# Tools that are framework infrastructure, never authored flows/setters/tasks.
_INFRA_TOOLS = frozenset({
    "slot_filling_engine",
    "validate_dag_config",
    "slot_intake",
})


def _tool_dirs(root: Path) -> list[Path]:
    """All tool subdirs of `root` that contain a `python_code.py`."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child.joinpath(*_PY_CODE_REL)).is_file():
            out.append(child)
    return out


def _python_code_path(root: Path, tool_name: str) -> Path:
    return root.joinpath(tool_name, *_PY_CODE_REL)


def discover_tool_names(framework_root: Optional[str] = None) -> list[str]:
    """Every tool dir name under the root (sorted), infra included."""
    root = fb.resolve_framework_root(framework_root)
    return [d.name for d in _tool_dirs(root)]


def discover_available_tools(framework_root: Optional[str] = None) -> list[str]:
    """Tool names usable as setter/task tools (infra + `*_dag` excluded).

    This is the `available_tools` list the validator checks setter/task `tool`
    references against. `*_dag` configs are flow definitions, not callable
    setter/task tools, so they are not part of the available-tool surface; the
    three engine/validator/intake infra tools are excluded too.
    """
    names = discover_tool_names(framework_root)
    return [
        n for n in names
        if n not in _INFRA_TOOLS and not n.endswith("_dag")
    ]


def discover_dag_configs(
    framework_root: Optional[str] = None,
) -> list[tuple[str, str]]:
    """The `*_dag` flow tools as `(config_id, display_name)` pairs (sorted).

    `config_id` is the tool dir name (e.g. `bella_notte_dag`); the display name
    is the dir name minus the trailing `_dag`, title-cased with spaces
    (`bella_notte_dag` -> `Bella Notte`).
    """
    out: list[tuple[str, str]] = []
    for name in discover_tool_names(framework_root):
        if name.endswith("_dag"):
            out.append((name, _display_name(name)))
    return out


def _display_name(config_id: str) -> str:
    base = config_id[:-4] if config_id.endswith("_dag") else config_id
    return base.replace("_", " ").title()


def read_tool_source(
    tool_name: str, framework_root: Optional[str] = None
) -> Optional[str]:
    """The raw python source of a tool's `python_code.py`, or None if absent."""
    root = fb.resolve_framework_root(framework_root)
    path = _python_code_path(root, tool_name)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def read_setter_sources(
    config: dict, framework_root: Optional[str] = None
) -> dict[str, str]:
    """`setter -> source` for every setter named by a slot in `config`.

    Only setters that resolve to a real tool file under the root are included;
    unknown setters are skipped (the validator's reference check flags those).
    """
    sources: dict[str, str] = {}
    for slot in config.get("slots", []) or []:
        setter = slot.get("setter")
        if not setter or setter in sources:
            continue
        src = read_tool_source(setter, framework_root)
        if src is not None:
            sources[setter] = src
    return sources


def read_task_tool_sources(
    config: dict, framework_root: Optional[str] = None
) -> dict[str, str]:
    """`tool -> source` for every task tool named in `config`."""
    sources: dict[str, str] = {}
    for task in config.get("tasks", []) or []:
        tool = task.get("tool")
        if not tool or tool in sources:
            continue
        src = read_tool_source(tool, framework_root)
        if src is not None:
            sources[tool] = src
    return sources
