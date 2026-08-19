"""Deploy an authored app to a live CES app — the single implementation.

`flows.authoring.build.emit` turns a `flows` App into an app dir; this package turns
an app dir (or an in-memory whole-app file set) into a DEPLOYED app. It is the shared
home for the "config dict -> deployed CXAS app" recipe that used to exist three times:
once inside a Slot Studio FastAPI route handler, once hand-rolled in Specter's builder,
once hand-rolled again in slotfill_migration.

Layout:

* :mod:`~flows.deploy.plan` — build the ``ScaffoldRequest`` / ``PushSpec``. Every
  product goes through here, so a new contract field is filled the same way for all.
* :mod:`~flows.deploy.render` — pure config -> app_files folding (dag resolution,
  bundle rendering, child-dag registration, tool-presence checks).
* :mod:`~flows.deploy.gates` — the pre-push checks (was ``slot_studio.prepush``).
* :mod:`~flows.deploy.target` — create-vs-update resolution.
* :mod:`~flows.deploy.runner` — the ONE subprocess seam (injectable; tests fake it).
* :mod:`~flows.deploy.service` — the orchestration: gates -> render -> push -> parse.
* :mod:`~flows.deploy.prep` / :mod:`~flows.deploy.push` — the app-dir CLI path
  (pull-and-merge live settings, then ``cxas push --overwrite``).

Everything except the runner's default implementation is pure or injectable, so the
whole pipeline is unit-testable with no subprocess, no CES and no network.
"""

from __future__ import annotations

from .env import DeployEnv
from .errors import (
    ComponentInvalidError,
    DagUnresolvedError,
    DeployError,
    RenderFailedError,
    ToolUnresolvedError,
)
from .models import (
    PrePushCheck,
    PrePushReport,
    PrePushRequest,
    PushConfigEntry,
    PushOutcome,
    PushSpec,
)
from .plan import build_push_spec, build_scaffold_request
from .runner import (
    DEFAULT_PUSH_TIMEOUT_S,
    PushRunner,
    default_push_runner,
    get_runner,
    run_push,
    set_runner,
)
from .target import FirstPushNeedsDisplayName

# NOT re-exported: `service.push`. `flows.deploy.push` is already a SUBMODULE (the
# app-dir CLI path), so binding the function to that name here makes
# `from flows.deploy import push` mean the function in a fresh process and the module
# once anything has imported the submodule. Call `flows.deploy.service.push`.

__all__ = [
    "DEFAULT_PUSH_TIMEOUT_S",
    "ComponentInvalidError",
    "DagUnresolvedError",
    "DeployEnv",
    "DeployError",
    "FirstPushNeedsDisplayName",
    "PrePushCheck",
    "PrePushReport",
    "PrePushRequest",
    "PushConfigEntry",
    "PushOutcome",
    "PushRunner",
    "PushSpec",
    "RenderFailedError",
    "ToolUnresolvedError",
    "build_push_spec",
    "build_scaffold_request",
    "default_push_runner",
    "get_runner",
    "run_push",
    "set_runner",
]
