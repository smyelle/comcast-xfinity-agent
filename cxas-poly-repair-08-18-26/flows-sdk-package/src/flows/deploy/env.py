"""The ambient facts a deploy needs that the request itself doesn't carry.

Slot Studio kept these in a process-global ``state.settings`` and every deploy
helper read it directly, which is precisely what made the pipeline un-extractable:
a pure function that reaches for a server singleton isn't pure and can't be reused
by a product that has no such singleton. They're passed in now.

Nothing here does I/O; ``agent_dir`` / ``framework_root`` are only *read* by the
local-mode fallbacks in :mod:`flows.deploy.gates`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DeployEnv:
    """Where we are deploying from and to.

    * ``project`` / ``location`` — the CES target, used to expand a bare app-id leaf
      into a full resource name and to pass ``--project-id`` / ``--location``.
    * ``agent_dir`` — the on-disk app to push when there is no whole-app payload.
    * ``framework_root`` — the ``tools/`` dir the local-mode gates read from.
    * ``mode`` — "local" or "hosted"; only changes the wording of the "nothing
      staged to push" refusal, which has to tell the author the right thing to do.
    """

    project: Optional[str] = None
    location: Optional[str] = None
    agent_dir: Optional[str] = None
    framework_root: Optional[str] = None
    mode: str = "local"
