"""The `cxas push` command line, and how to read what comes back.

Pure string work: build the argv, scrape the deployed app out of stdout, classify a
nonzero exit into something a UI (or an agent loop) can act on. No subprocess here —
that's :mod:`flows.deploy.runner` — so all of it is testable without a CLI.
"""

from __future__ import annotations

import re
from typing import Optional

# App resource name (projects/.../apps/<id>) or a bare deployed_app_id token,
# scraped from push stdout. The resource-name form wins when both appear.
_APP_RESOURCE_RE = re.compile(
    r"projects/[^/\s]+/locations/[^/\s]+/apps/[A-Za-z0-9_-]+"
)
_APP_ID_RE = re.compile(
    r"(?:deployed[_ ]?app[_ ]?id|app[_ ]?id|app name)\s*[:=]\s*([^\s,]+)",
    re.IGNORECASE,
)


def build_push_argv(
    app_dir: str,
    *,
    to: Optional[str] = None,
    display_name: Optional[str] = None,
    project_id: Optional[str] = None,
    location: Optional[str] = None,
    overwrite: bool = False,
) -> list[str]:
    """Build the ``push`` argv (excluding the cxas program).

    Always targets a local ``--app-dir``. ``--to`` updates an existing app
    (resource name or display name); ``--display-name`` CREATES a new app (use
    exactly one). ``--project-id``/``--location`` are appended when supplied.

    ``overwrite`` adds ``--overwrite`` (CES ``conflict_strategy=OVERWRITE``). On an
    UPDATE (``--to``) WITHOUT it, CES does a partial merge that does NOT create
    tools newly added since the last push — so a re-push of a revised app ships a
    config referencing tools that were never registered. Callers whose ``--app-dir``
    is the COMPLETE authoritative app (Specter's scaffold) should set it so CES
    fully reconciles the tool set.
    """
    argv: list[str] = ["push", "--app-dir", app_dir]
    if to:
        argv += ["--to", to]
    if display_name:
        argv += ["--display-name", display_name]
    if overwrite:
        argv += ["--overwrite"]
    if project_id:
        argv += ["--project-id", project_id]
    if location:
        argv += ["--location", location]
    return argv


def parse_app_name(stdout: str, stderr: str = "") -> Optional[str]:
    """Best-effort extract the deployed app name from push output.

    Prefers a full ``projects/.../apps/<id>`` resource name; falls back to an
    ``app id: <id>`` style token. Returns None when neither is present (the push
    may still have succeeded — the client can read gecx-config.json instead).
    """
    combined = f"{stdout}\n{stderr}"
    m = _APP_RESOURCE_RE.search(combined)
    if m:
        return m.group(0)
    m = _APP_ID_RE.search(combined)
    if m:
        return m.group(1)
    return None


def parse_deployed_app_id(stdout: str, stderr: str = "") -> Optional[str]:
    """Extract the bare deployed app id from a ``Successfully pushed to: …`` line.

    cxas prints ``Successfully pushed to: projects/<p>/locations/<l>/apps/<id>`` on
    a create/update. We return just the ``<id>`` trailing segment for the
    gecx-config write (create-vs-update next time). Falls back to the resource name
    parsed by :func:`parse_app_name` when no explicit success line is present.
    """
    combined = f"{stdout}\n{stderr}"
    m = re.search(
        r"Successfully pushed to:\s*projects/[^/\s]+/locations/[^/\s]+/apps/([A-Za-z0-9_-]+)",
        combined,
    )
    if m:
        return m.group(1)
    resource = _APP_RESOURCE_RE.search(combined)
    if resource:
        return resource.group(0).rsplit("/", 1)[-1]
    return None


def classify_push_error(stdout: str, stderr: str, rc: int) -> Optional[str]:
    """Map a nonzero ``cxas push`` result to a typed ``error_kind``.

    Returns one of ``dup_uuid`` | ``auth`` | ``labs_terraform`` | ``tf_lock`` |
    ``cxas_missing`` | ``quota`` (or ``None`` when unrecognized). Never raises.
    """
    blob = f"{stdout or ''}\n{stderr or ''}".lower()

    # rc=127: the cxas console-script/module wasn't found at all.
    if rc == 127 or "command not found" in blob or "no module named" in blob:
        return "cxas_missing"

    # Duplicate tool-name UUID surfaces as a 404 "Tools not found" on push.
    if "tools not found" in blob or ("404" in blob and "tool" in blob):
        return "dup_uuid"

    # Auth: gcert / expired credentials / ADC.
    if (
        "gcert" in blob
        or "reauth" in blob
        or "credentials" in blob
        or "permission denied" in blob
        or "unauthenticated" in blob
        or "loas" in blob
    ):
        return "auth"

    # Orphaned terraform state lock (cancelled mid-apply).
    if "state lock" in blob or "state blob is already locked" in blob or (
        "lock" in blob and "terraform" in blob
    ):
        return "tf_lock"

    # Terraform aborted before the Cloud Run update (Labs deploy pitfall).
    if "terraform" in blob or "firestore rules" in blob or "tf apply" in blob:
        return "labs_terraform"

    # CES per-minute read/write quota — transient; the author should just wait.
    if (
        "resource_exhausted" in blob
        or "quota exhausted" in blob
        or "quota exceeded" in blob
        or "429" in blob
    ):
        return "quota"

    return None
