"""Create-vs-update: which app are we pushing to, and are we allowed to make one?

`cxas push` takes EITHER ``--to <resource-or-display-name>`` (update an existing app)
OR ``--display-name <name>`` (create a new one). Getting this wrong doesn't fail
loudly — it quietly creates a second app and the author's "deployed" agent is a
duplicate nobody is calling. So the decision is one function with one refusal.
"""

from __future__ import annotations

import re
from typing import Optional

from .env import DeployEnv

# A bare CES app id (UUID leaf). `cxas push --to` needs a FULL resource name or a
# display name, so a bare leaf must be expanded to projects/.../apps/<id>.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class FirstPushNeedsDisplayName(Exception):
    """No existing deployment and no explicit name: refuse rather than guess one.

    A defaulted display name is how you end up with three apps called "config" and
    no idea which one Live mode is talking to.
    """

    def __init__(self) -> None:
        super().__init__("First push needs a display name (create).")


def resolve_to_target(to: Optional[str], env: DeployEnv) -> Optional[str]:
    """Expand a bare app-id leaf into a full resource name for ``--to`` (a full
    resource or a display name is passed through unchanged)."""
    if not to or "/" in to or not _UUID_RE.match(to):
        return to
    if env.project and env.location:
        return f"projects/{env.project}/locations/{env.location}/apps/{to}"
    return to


def decide(
    *,
    to: Optional[str],
    deployed_app_id: Optional[str],
    display_name: Optional[str],
    env: DeployEnv,
    strict: bool = True,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(to_target, display_name)`` — exactly one of which is set on a
    whole-app push.

    An existing deployment (``deployed_app_id`` or a resolvable ``to``) updates via
    ``--to``; otherwise a create REQUIRES an explicit display name. Neither, with
    ``strict``, raises :class:`FirstPushNeedsDisplayName` (no silent create).

    ``strict=False`` is the legacy config-only path: push the on-disk agent dir with
    ``to`` passed through verbatim (which may be None), which is what it always did.
    """
    to_target = resolve_to_target(to or deployed_app_id, env)
    if not strict:
        return to_target, None
    if to_target:
        return to_target, None  # update an existing app
    if display_name:
        return None, display_name  # explicit create
    raise FirstPushNeedsDisplayName()
