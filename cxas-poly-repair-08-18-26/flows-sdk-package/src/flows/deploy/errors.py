"""Typed refusals from the deploy pipeline.

Each one exists because the alternative was a SILENT wrong deploy: fabricating a dag
tool CES then ignores, shipping the stale dag after a failed render, or pushing a
bundle whose tools aren't there. They carry the data the caller needs to explain
itself (candidates / missing names / diagnostics) rather than just a message.
"""

from __future__ import annotations

from typing import Any, List


class DeployError(Exception):
    """Base for every refusal below, so a caller can catch the family."""


class DagUnresolvedError(DeployError):
    """The canvas config couldn't be matched to a single dag tool in the app.

    Raised for a multi-dag app when config_id names no existing tool — we refuse
    to fabricate a stray ``{id}_dag`` (the silent "push didn't take" bug). Carries
    the real candidate dag names so the UI can tell the author what to pick.
    """

    def __init__(self, candidates: List[str]):
        self.candidates = candidates
        super().__init__(f"could not resolve a dag tool; candidates: {candidates}")


class RenderFailedError(DeployError):
    """Rendering the canvas config into the dag python failed — the push MUST NOT
    proceed and silently ship the stale dag."""


class ToolUnresolvedError(DeployError):
    """A bundle config references a setter/task tool that isn't in the app.

    V1 scope: a Component's children may only pull in tools that already live in
    the same app (or are authored inline). A ref to a tool from another app can't
    be satisfied (there's no cross-app tool export) — block with the missing names
    rather than ship a broken bundle. The V2 gallery is the cross-source path.
    """

    def __init__(self, missing: List[str]):
        self.missing = missing
        super().__init__(f"unresolved tools: {missing}")


class ComponentInvalidError(DeployError):
    """The bundle failed cross-config validation (unresolved component ref, I/O
    mismatch, cycle, or over-depth). Carries the blocking diagnostics."""

    def __init__(self, diagnostics: List[Any]):
        self.diagnostics = diagnostics
        super().__init__("bundle failed cross-config validation")
