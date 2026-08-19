"""The deploy contract: what a push asks for, what the gates report, what came back.

These models used to be defined inside Slot Studio's FastAPI routers, which meant
Specter and slotfill_migration reached across into a route module to build a request
(and could each drift in what they filled in). They live here now, next to the code
that consumes them, so all three products construct the SAME object. Slot Studio's
``routers/push.py`` and ``routers/agent.py`` re-export these names, so the HTTP wire
contract is literally this model — there is no second definition to keep in step.

Pure pydantic; no FastAPI, no I/O.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import models
from ..emit.models import ScaffoldFile


# ---------------------------------------------------------------------------
# Pre-push gates
# ---------------------------------------------------------------------------

class PrePushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_files: Optional[list[ScaffoldFile]] = None  # hosted/forced
    config: Optional[models.Config] = None
    config_id: Optional[str] = None
    # Opt-in stricter, more aggressive gates (e.g. setter return-shape) — OFF by
    # default so the UI / legacy push paths are unaffected; programmatic builders
    # (Specter) turn it on to demand a fully-correct agent before deploy.
    strict: bool = False


class PrePushCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Literal["validate_dag", "tool_bodies", "setter_shape", "callback_sync", "dup_uuid", "docstring_sig", "lint"]
    ok: bool
    # "error" blocks the push; "warning" is advisory (e.g. an existing app's
    # callbacks legitimately differ from our blessed source — that must not block
    # updating it). report.ok is computed from error-severity checks only.
    severity: Literal["error", "warning"] = "error"
    detail: str = ""
    fix: Optional[models.DiagnosticFix] = None


class PrePushReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    checks: list[PrePushCheck]
    target: Literal["create", "update"]
    target_label: str  # display name or resource name


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

class PushConfigEntry(BaseModel):
    """One config in a multi-DAG bundle push: a BARE config id + its document.
    The target tool is ``<config_id>_dag`` (created/registered if absent)."""

    model_config = ConfigDict(extra="forbid")
    config_id: str
    config: models.Config

    @field_validator("config_id")
    @classmethod
    def _bare(cls, v: str) -> str:
        """Strip a ``_dag`` suffix: the bundle is keyed by FLOW id, not tool name.

        Enforced on the model rather than in the constructor because the wire path
        (FastAPI parsing a request body) skips constructors entirely — which is how
        the Studio and migration ended up keying the same bundle two different ways.
        It is not cosmetic: cross-config validation resolves a component reference by
        this key, so a ``_dag``-suffixed entry silently fails to satisfy the ref that
        names it.
        """
        return v[:-4] if v.endswith("_dag") else v


class PushSpec(BaseModel):
    """Everything a deploy needs, independent of who asked for it.

    ``config`` is the edited document (preferred — it is what the author sees);
    ``config_id`` names the export file / rendered function and, when ``config``
    is omitted, the flow is re-imported from the repo by id instead.

    Slot Studio exposes this as the ``PushRequest`` body of ``POST /api/agent/push``;
    Specter and slotfill_migration build the same object in-process. All three go
    through :func:`flows.deploy.plan.build_push_spec`, so a field added here is
    filled the same way for everyone.
    """

    model_config = ConfigDict(extra="forbid")

    config_id: Optional[str] = None
    config: Optional[models.Config] = None
    # Multi-DAG BUNDLE push: the whole component workspace (root + every Component
    # child, BARE ids). When present (whole-app payload only), every config renders
    # into its own ``<id>_dag`` tool and child dags are created/registered as
    # needed, so a component agent ships in one push. Omitted → legacy single push.
    configs: Optional[list[PushConfigEntry]] = None
    # Optional explicit deploy target (resource name or display name). When set,
    # it is passed through to ``cxas push --to``.
    to: Optional[str] = None
    # The whole-app payload (hosted, or a forced local override). When present,
    # the server materializes a scratch dir and pushes that instead of agent_dir.
    app_files: Optional[list[ScaffoldFile]] = None
    # CREATE (explicit display name) — never defaulted; required on first push.
    display_name: Optional[str] = None
    # create-vs-update hint read from gecx-config.json (local) / CES (hosted).
    deployed_app_id: Optional[str] = None
    # Run the pre-push gates server-side before the subprocess (block on error).
    run_gates: bool = True
    # Opt into the stricter, more aggressive gates (setter return-shape, …). OFF by
    # default; programmatic builders (Specter) set it to demand a correct agent.
    strict: bool = False
    # Full reconcile on UPDATE (CES conflict_strategy=OVERWRITE). Required when the
    # app_files payload is the COMPLETE authoritative app (Specter): without it a
    # re-push to an existing app does a partial merge that never creates tools added
    # since the last push, so the deployed config references unregistered tools.
    overwrite: bool = False


class PushOutcome(BaseModel):
    """Outcome of a push attempt. Serialized by alias so the client sees the
    camelCase ``appName`` the feature spec requires.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ok: bool
    app_name: Optional[str] = Field(default=None, alias="appName")
    output: str = ""
    error: Optional[str] = None
    # Typed failure class: dup_uuid|auth|labs_terraform|tf_lock|cxas_missing|
    # gate_failed|timeout.
    error_kind: Optional[str] = None
    # Populated when a pre-push gate blocks (subprocess never ran).
    gate_report: Optional[PrePushReport] = None
    # Parsed from stdout for the gecx-config write (create-vs-update next time).
    deployed_app_id: Optional[str] = None
    # --- Push verification: what actually shipped (honest success UX) ---------
    # The dag tool the canvas config was rendered into + the slot names rendered
    # there, so the client can say exactly what was pushed (and to which flow).
    dag: Optional[str] = None
    rendered_slots: Optional[list[str]] = None
