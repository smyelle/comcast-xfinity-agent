"""Render an in-memory whole-app file SET to a real directory `cxas push` can upload.

The working copy is the universal ``list[ScaffoldFile]`` ({path, content}) unit: the
client holds it (hosted), a builder produces it (Specter, migration), or it mirrors
disk (local). ``cxas push`` only speaks ``--app-dir``, so somebody has to write it
out and then throw it away.

The interesting part is the sandbox: entries come from a payload, so absolute
components and ``..`` traversal are stripped and anything that still resolves outside
the scratch root is dropped. A file set is data, not a trusted path list.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)


def materialize(files: Sequence) -> str:
    """Write a whole-app file SET to a fresh scratch dir; return its path.

    ``files`` is a sequence of objects exposing ``.path`` (app-root-relative) and
    ``.content`` (str) — i.e. ``ScaffoldFile`` instances or anything duck-typed
    the same. Paths are joined under the scratch root; parent dirs are created so
    nested entries (e.g. ``tools/foo/python_function/python_code.py``) land in
    place.
    """
    # The prefix predates the move and is asserted by an existing test; renaming it
    # would be a behaviour change in a refactor, so it keeps the old name.
    scratch = tempfile.mkdtemp(prefix="slot_studio_push_")
    root = Path(scratch).resolve()
    for rel, content in _iter_files(files):
        # Confine every entry to the scratch root: drop leading slashes/drive and
        # any ".." segments so a hostile path can't escape the sandbox.
        parts = [p for p in Path(rel).parts if p not in ("", "/", "..") and not p.endswith(":")]
        if not parts:
            continue
        dest = root.joinpath(*parts)
        # Defense in depth: skip anything that still resolves outside the root.
        try:
            dest.resolve().relative_to(root)
        except ValueError:
            logger.warning("workdir.materialize skipping out-of-tree path: %s", rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return scratch


def cleanup(path: str | None) -> None:
    """Best-effort remove a scratch dir created by :func:`materialize`.

    Never raises (cleanup runs in a ``finally``); logs at warning on failure.
    """
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # never raise from cleanup
        logger.warning("workdir.cleanup failed for %s: %s", path, exc)


def _iter_files(files: Iterable) -> list[tuple[str, str]]:
    """Normalize a file SET to ``[(path, content)]``."""
    out: list[tuple[str, str]] = []
    for f in files or []:
        path = getattr(f, "path", None)
        content = getattr(f, "content", "")
        if path:
            out.append((str(path), str(content)))
    return out
