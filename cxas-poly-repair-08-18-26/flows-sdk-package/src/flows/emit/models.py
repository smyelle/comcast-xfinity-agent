"""Emit/push contract models — the whole-app file unit and scaffold I/O shape.

These pydantic models are the stable contract between the emitter (`scaffold.build`)
and its consumers (the Studio push router, migration handoff, Specter, external
`flows` CLI). They live in `flows` (not the Labs serving layer) so the emitter is
self-contained: a `ScaffoldFile` is app-root-relative `{path, content}`; `build`
takes a `ScaffoldRequest` and returns a `ScaffoldResult`.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator


def _check_config_id(value: str, *, where: str = "config_id") -> str:
    """Reject a config id that cannot be a Python function name.

    Every config id is rendered into ``def <config_id>_dag()`` source. Anything
    that is not an identifier emits a tool whose module does not parse — a
    `SyntaxError` that would otherwise only surface at deploy time, long after
    ``build`` reported ``ok=True`` with zero validation errors.
    """
    if not value.isidentifier():
        raise ValueError(
            f"{where} {value!r} is not a Python identifier; it is rendered as the"
            " function name `<config_id>_dag()`")
    return value


class ScaffoldFile(BaseModel):
    """The universal whole-app file unit: scaffold output, push input, working
    copy entry. ``path`` is app-root-relative."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str


class ValidationReportLite(BaseModel):
    """A ``validate_dag_config`` verdict reduced to bools + message lists."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[str] = []
    warnings: list[str] = []


class ScaffoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_display_name: str
    config_id: str  # -> <config_id>_dag; identifier-validated
    root_agent: str
    gcp_project: str
    location: str = "us"
    # Default to a voice/live-capable model so scaffolded agents work in audio/live
    # mode out of the box (a non-live model can't drive bidi audio sessions).
    model: str = "gemini-3.1-flash-live"
    mode: Literal["local", "hosted"] = "local"
    target_path: Optional[str] = None  # local only: where to write the app dir
    # Optional starter template key (scaffold.TEMPLATES). When set, the starter
    # agent is populated with that template's user slots + hand-written setters
    # instead of the empty gate-only floor. None -> "Start blank".
    template: Optional[str] = None
    # Accept path for AI "describe your own": an explicit dag config + its
    # setters ({name: python_code}) to build instead of a template. The server
    # still owns app.json/callbacks/framework wiring/UUIDs/validation.
    config_override: Optional[dict[str, Any]] = None
    setters_override: Optional[dict[str, str]] = None
    # MULTI-DAG bundle: additional {bare_config_id: config} rendered as their own
    # <id>_dag tools alongside the primary config_id (e.g. a migration's router +
    # carve children + shared components). Omitted → single-DAG (unchanged).
    extra_configs: Optional[dict[str, Any]] = None
    # Arbitrary pre-authored tool bodies {tool_name: python_code} beyond setters —
    # task executors, prerequisite tools, etc. Merged with setters for emission.
    # Omitted → only setters are emitted (unchanged).
    tools_override: Optional[dict[str, str]] = None
    # Tool names to emit with `executionType: ASYNCHRONOUS`. CES then defers the body
    # and answers the call with a `{"result": "pending"}` placeholder, delivering the
    # real payload a turn or more later as a synthetic user turn. Omitted → the key is
    # absent from every tool resource, exactly as before.
    async_tools: Optional[list[str]] = None
    # {tool_name: seconds} to emit as `timeout: "<n>s"` on the tool resource. CES kills
    # a body at 60s unless told otherwise, and an overrun is SILENT — the body never
    # reports, so no error surfaces and nothing times out. Omitted → the key is absent
    # and the platform default applies, exactly as before.
    tool_timeouts: Optional[dict[str, int]] = None
    # {setter_name: description} to emit as the tool's model-facing
    # `pythonFunction.description`, from a slot's `description=`. A generated setter
    # otherwise gets the useless default "Record the value for <name>."; this is the only
    # way to give the model a real purpose/when-to-call for a model-classified slot.
    # Omitted / a name absent -> the default, exactly as before.
    tool_descriptions: Optional[dict[str, str]] = None
    # Remote A2A agents: one `remoteAgentTool` payload per entry
    # (`{name, description, agentCard}`). Each is emitted as a BODY-LESS tool resource
    # — a `<name>.json` and no `python_function/` — because the platform, not the
    # sandbox, performs the call. Omitted → no A2A tools (unchanged).
    remote_agent_tools: Optional[list[dict[str, Any]]] = None
    # Google Search grounding tools: one `googleSearchTool` payload per entry, each
    # emitted as a body-less `tools/<name>/<name>.json` (see flows.authoring.search).
    search_tools: Optional[list[dict[str, Any]]] = None
    # Agents callable as tools: one `agentTool` payload per entry
    # (`{name, description, agent, asynchronous}`), emitted body-less like the A2A ones.
    # Unlike those, `executionType` follows `asynchronous` — this is the only agent-tool
    # flavour the platform will defer (ces-probes 133/134).
    agent_tools: Optional[list[dict[str, Any]]] = None
    # Names of agent tools this app ALREADY carries (declared `emit=False`). Nothing is
    # written for them; they are named so availability checks know the tool exists.
    carried_agent_tools: Optional[list[str]] = None
    # Plain in-app agents an `agentTool` can name: one `{name, displayName, tools,
    # instruction}` per entry, emitted as `agents/<name>/` + its instruction file.
    helper_agents: Optional[list[dict[str, Any]]] = None
    # Toolsets: one `{name, resource, spec?}` per entry, OpenAPI or MCP. Each is emitted
    # as a TOOLSET (its own resource kind, not a tool) — `toolsets/<name>/<name>.json`,
    # plus, for OpenAPI, the spec at
    # `toolsets/<name>/open_api_toolset/open_api_schema.yaml` (an MCP toolset carries no
    # `spec`). No agent references these; the wrappers that call them are ordinary tools.
    # Omitted → no toolsets dir (unchanged).
    toolsets: Optional[list[dict[str, Any]]] = None
    # Guardrails: one `{name, dir, resource}` per entry, emitted as
    # `guardrails/<dir>/<dir>.json`. A guardrail is its OWN resource kind — not a tool
    # and not a callback — attached BY DISPLAY NAME from `app.json`'s `guardrails` array
    # (written by the app-settings step) and from an agent's. Omitted → no guardrails
    # dir (unchanged).
    guardrails: Optional[list[dict[str, Any]]] = None
    # Verbatim `environment.json` for env-scoped toolsets (URLs + secret versions CES
    # merges over the committed resources at import). Omitted → the file is not written.
    environment_json: Optional[str] = None
    # Optional PINNED resource UUIDs for the app + root agent. Omitted → a fresh
    # uuid4 (legacy behavior). Pinning keeps a redeploy updating the SAME agent/app
    # resource instead of creating a new one each push (which otherwise leaves
    # stale orphaned agents in the deployed app).
    app_uuid: Optional[str] = None
    agent_uuid: Optional[str] = None

    @field_validator("config_id")
    @classmethod
    def _config_id_is_an_identifier(cls, v: str) -> str:
        return _check_config_id(v)

    @field_validator("extra_configs")
    @classmethod
    def _extra_config_ids_are_identifiers(
            cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        # Each key is emitted as its own `<key>_dag` tool, same hazard as above.
        for key in (v or {}):
            _check_config_id(key, where="extra_configs key")
        return v


class ChildAgentSpec(BaseModel):
    """One slot-filling sub-agent in a multi-agent app: its display name, its
    instruction text, and its fully-scoped ``tools`` (dag + engine + setters +
    set_active_flow + control tools). Gets the canonical 4 framework callbacks."""

    model_config = ConfigDict(extra="forbid")

    name: str
    instruction: str
    tools: list[str]
    # Display names of guardrails scoped to THIS agent (see Agent.guardrails).
    guardrails: list[str] = []


class HostAgentSpec(BaseModel):
    """The steering/root agent. ``strategy="transfer"`` (receptionist) emits a
    non-slot-filling router (custom before_model/after_tool, ``tools`` =
    ``[set_active_flow, end_session]`` plus any author-scoped extras);
    ``"engine"`` emits an engine-running host (the 4 framework callbacks + its router
    DAG). ``child_agents`` is the ADK ``childAgents`` hierarchy (both strategies)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    instruction: str
    strategy: Literal["transfer", "engine"] = "transfer"
    child_agents: list[str]
    tools: list[str]
    # Display names of guardrails scoped to the host agent only.
    guardrails: list[str] = []


class MultiAgentScaffoldRequest(BaseModel):
    """Emit contract for a host + N sub-agents app (sibling of ScaffoldRequest).

    ``all_configs`` holds every ``<config_id>_dag`` across the host + sub-agents
    (emitted once each at app root). ``agent_config_map`` (name -> config_id) is
    written to app.json; ``default_config_id``/``intent_config_map`` are set only
    for the ``engine`` strategy (the transfer strategy needs neither)."""

    model_config = ConfigDict(extra="forbid")

    app_display_name: str
    gcp_project: str
    location: str = "us"
    model: str = "gemini-3.1-flash-live"
    mode: Literal["local", "hosted"] = "local"
    target_path: Optional[str] = None
    host: HostAgentSpec
    agents: list[ChildAgentSpec]
    # {config_id: config dict} for EVERY dag across host+agents (emitted once each).
    all_configs: dict[str, Any]
    # {tool_name: python_code} for setters/executors + the app's set_active_flow.
    tools_override: dict[str, str] = {}
    # Tool names to emit with `executionType: ASYNCHRONOUS` (see ScaffoldRequest).
    async_tools: Optional[list[str]] = None
    # {tool_name: seconds} for `timeout: "<n>s"` (see ScaffoldRequest).
    tool_timeouts: Optional[dict[str, int]] = None
    # {setter_name: description} for the tool's model-facing description (see
    # ScaffoldRequest).
    tool_descriptions: Optional[dict[str, str]] = None
    # Remote A2A agent tool payloads, emitted body-less (see ScaffoldRequest). Emitted
    # once each at app root; which agent references them is decided by the host's /
    # each sub-agent's own `tools` list.
    remote_agent_tools: Optional[list[dict[str, Any]]] = None
    # Google Search grounding tools: one `googleSearchTool` payload per entry, each
    # emitted as a body-less `tools/<name>/<name>.json` (see flows.authoring.search).
    search_tools: Optional[list[dict[str, Any]]] = None
    # Agents callable as tools: one `agentTool` payload per entry
    # (`{name, description, agent, asynchronous}`), emitted body-less like the A2A ones.
    # Unlike those, `executionType` follows `asynchronous` — this is the only agent-tool
    # flavour the platform will defer (ces-probes 133/134).
    agent_tools: Optional[list[dict[str, Any]]] = None
    # Names of agent tools this app ALREADY carries (declared `emit=False`). Nothing is
    # written for them; they are named so availability checks know the tool exists.
    carried_agent_tools: Optional[list[str]] = None
    # Plain in-app agents an `agentTool` can name: one `{name, displayName, tools,
    # instruction}` per entry, emitted as `agents/<name>/` + its instruction file.
    helper_agents: Optional[list[dict[str, Any]]] = None
    # Toolsets + their environment.json (see ScaffoldRequest). Emitted once each at app
    # root: a toolset is app-scoped, and the per-agent choice is which wrapper each agent
    # lists.
    toolsets: Optional[list[dict[str, Any]]] = None
    guardrails: Optional[list[dict[str, Any]]] = None
    environment_json: Optional[str] = None
    # {agent_name: config_id} written to app.json's agent_config_map variable.
    agent_config_map: dict[str, str]
    # engine strategy only: the host's router config id + upstream-intent routing.
    default_config_id: Optional[str] = None
    intent_config_map: Optional[dict[str, str]] = None
    app_uuid: Optional[str] = None

    @field_validator("all_configs")
    @classmethod
    def _all_config_ids_are_identifiers(
            cls, v: dict[str, Any]) -> dict[str, Any]:
        # Each key is emitted as its own `<key>_dag` tool, same hazard as above.
        for key in (v or {}):
            _check_config_id(key, where="all_configs key")
        return v


class ScaffoldResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    files: list[ScaffoldFile] = []  # the whole app SET (always returned)
    written_to: Optional[str] = None  # local: on-disk app dir; hosted: None
    validation: ValidationReportLite
    callback_sync_ok: bool
    uuids_unique: bool
    # The canonical framework version this agent was generated against (the
    # blessed manifest version), so authors/UI can see what they're pinned to.
    framework_version: Optional[str] = None
    # The starter <config_id>_dag config (slots/tasks/gate) as a dict, so the
    # client can load the new agent into the editor/graph immediately after
    # scaffolding (otherwise "New project" succeeds but nothing opens).
    config: Optional[dict[str, Any]] = None
    # A starter conversation the Simulator can offer as a one-click first message
    # (from the chosen template). None when starting blank.
    suggested_message: Optional[str] = None
    error: Optional[str] = None
