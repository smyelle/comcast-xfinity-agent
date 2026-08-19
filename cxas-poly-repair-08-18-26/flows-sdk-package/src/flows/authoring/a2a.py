"""A2A (Agent2Agent) remote agents — call another agent as a CES tool.

CES models a remote A2A agent as a first-class tool: the `Tool` resource carries a
`remoteAgentTool` (a `tool_type` oneof sibling of `pythonFunction`) holding an A2A
**agent card**. So an A2A tool is a BODY-LESS tool — a `tools/<name>/<name>.json` and
nothing else. There is no `python_code.py` to write, and none may be emitted: the
platform, not the sandbox, performs the call.

    billing = flows.remote_agent(
        "billing-agent",
        description="Answers billing and invoice questions.",
        url="https://billing.example.com/a2a/v1",
        skills=[flows.agent_skill("lookup_invoice", description="Look up an invoice.")],
    )
    app = flows.App(root_flow=flow, remote_agents=[billing], ...)

Declaring a remote agent emits its tool resource AND scopes it onto the agent, so the
model can call it the same way the reference app does (`{@TOOL: billing-agent}` in the
instruction). `ces_agent(...)` is the same thing for the common case where the remote
agent is itself a CES app — that URL is formulaic.

The wire contract (verified live against the reference app
`[REFERENCE] A2A Protocol Inbound and Outbound`):

* **Input** — the tool takes `task` (a natural-language request string) and an optional
  `contextId` (an earlier reply's context, to continue that A2A conversation). It does
  NOT take the remote skill's own parameters; the remote agent parses the request.
* **Output** — the A2A `SendMessageResponse` oneof, which arrives as one of two shapes:

      {"message": {"parts": [{"text": "..."}], "contextId": "..."}}   # immediate
      {"task":    {"id": ..., "status": {"state": "TASK_STATE_SUBMITTED"}, ...}}  # deferred

  and, when CES defers the call itself, its placeholder `{"result": "pending"}`. A call
  that cannot be made at all answers `{"error": "..."}` — commonly the remote agent's
  own IAM. None of those three is either kind, so they route to the call task's `on_failure`.

That oneof is why `A2A_REPLY_KINDS` exists and why firing one of these from a `task()` needs
care. Intake reads `success = bool(response_data.get(success_check))` and maps `outputs`
by FLAT top-level key (`slot_intake._intake_executor`). An A2A response has no `success`
key at all, and its text sits at `message.parts[0].text` — out of reach of a flat map. A
naive `task(tool=<remote agent>)` therefore looks failed on every fire and, with
`on_failure.max_retries` defaulting to zero, escalates the flow the first time it runs.
`delegate()` is the primitive that gets this right; `build` errors on the shapes that
don't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

from . import tools as _tools

# The two kinds of the A2A SendMessageResponse oneof, as they land in `response_data`.
# `message` is an immediate reply and carries the text; `task` is a long-running task
# whose terminal payload arrives later.
A2A_MESSAGE_REPLY = "message"
A2A_TASK_REPLY = "task"
A2A_REPLY_KINDS = (A2A_MESSAGE_REPLY, A2A_TASK_REPLY)

# The remote agent tool's own parameter names (the platform defines these, not us).
A2A_REQUEST_PARAM = "task"
A2A_CONTEXT_PARAM = "contextId"

# Card defaults. HTTP+JSON / 1.0 are what the reference app's cards declare.
DEFAULT_PROTOCOL_BINDING = "HTTP+JSON"
DEFAULT_PROTOCOL_VERSION = "1.0"
DEFAULT_AGENT_VERSION = "1.0.0"

# CES apps are themselves addressable as A2A agents at this URL (the inbound half of
# the protocol — it needs no app-side config, which is why `ces_agent` is pure sugar).
_CES_APP_URL = "https://ces.googleapis.com/v1/projects/{project}/locations/{location}/apps/{app_id}"

# A tool name reaches CES as a `displayName` and is referenced from instructions as
# `{@TOOL: name}`; dots/spaces/slashes break that reference.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _require(value: Any, what: str) -> str:
  """A required card string, rejected when blank (the API requires all of these)."""
  text = "" if value is None else str(value).strip()
  if not text:
    raise ValueError(f"{what} is required and cannot be empty")
  return text


@dataclass(frozen=True)
class AgentSkill:
  """One unit of ability the remote agent advertises (A2A `AgentSkill`).

  Skills are how the model decides an agent is worth calling — they are the only
  per-capability text on the card, so a card with vague skills gets called wrongly or
  not at all.
  """

  id: str
  name: str
  description: str
  tags: tuple[str, ...] = ()
  examples: tuple[str, ...] = ()
  input_modes: tuple[str, ...] = ()
  output_modes: tuple[str, ...] = ()

  def to_dict(self) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": self.id,
        "name": self.name,
        "description": self.description,
        "tags": list(self.tags),
    }
    # Optional repeated fields are omitted entirely when empty, so an emitted card
    # round-trips byte-identically against one pulled from a live app.
    if self.examples:
      d["examples"] = list(self.examples)
    if self.input_modes:
      d["inputModes"] = list(self.input_modes)
    if self.output_modes:
      d["outputModes"] = list(self.output_modes)
    return d


def agent_skill(
    skill_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    tags: Sequence[str] = (),
    examples: Sequence[str] = (),
    input_modes: Sequence[str] = (),
    output_modes: Sequence[str] = (),
) -> AgentSkill:
  """One skill on a remote agent's card.

  `name` defaults to `skill_id` and `description` to `name` — the API requires all
  three, and for the common ADK/CXAS case where the skill is a named tool
  (`get_weather`) they are genuinely the same string.
  """
  sid = _require(skill_id, "agent_skill(): id")
  nm = _require(name or sid, "agent_skill(): name")
  return AgentSkill(
      id=sid,
      name=nm,
      description=_require(description or nm, "agent_skill(): description"),
      tags=tuple(tags),
      examples=tuple(examples),
      input_modes=tuple(input_modes),
      output_modes=tuple(output_modes),
  )


@dataclass(frozen=True)
class RemoteAgent:
  """A remote A2A agent, emitted as one body-less `remoteAgentTool` resource.

  `name` is the tool name the model calls and the instruction references. The card's
  own `name`/`description` default to the tool's, which is what the reference app does.

  A `ces_agent` that was not told a project leaves `url` empty and carries the app id
  instead; the App resolves it at build (see `resolved_for`). Everything else is
  complete the moment it is constructed.
  """

  name: str
  description: str
  url: str
  skills: tuple[AgentSkill, ...]
  version: str = DEFAULT_AGENT_VERSION
  protocol_binding: str = DEFAULT_PROTOCOL_BINDING
  protocol_version: str = DEFAULT_PROTOCOL_VERSION
  tenant: Optional[str] = None
  card_name: Optional[str] = None
  card_description: Optional[str] = None
  # Deferred CES-app target: set by `ces_agent` when project/location were omitted.
  ces_app_id: Optional[str] = None
  ces_project: Optional[str] = None
  ces_location: Optional[str] = None

  @property
  def is_resolved(self) -> bool:
    """Whether this agent has an endpoint yet (a deferred `ces_agent` has none)."""
    return bool(self.url)

  def resolved_for(self, project: str, location: str) -> "RemoteAgent":
    """This agent with its endpoint filled in from the app's project/location.

    A no-op unless it is a `ces_agent` that was left to inherit them. Returns a new
    instance — the same declaration can be shared by two apps in two projects.
    """
    if self.is_resolved:
      return self
    return replace(self, url=_CES_APP_URL.format(
        project=self.ces_project or project,
        location=self.ces_location or location,
        app_id=self.ces_app_id,
    ))

  def agent_card(self) -> dict[str, Any]:
    """The A2A `AgentCard` for this agent."""
    if not self.is_resolved:
      raise ValueError(
          f"remote agent {self.name!r} has no endpoint yet: it is a ces_agent() with no "
          "project, so its URL comes from the App at build. Pass it to "
          "App(remote_agents=[...]), or give ces_agent(project=...) explicitly"
      )
    interface: dict[str, Any] = {
        "url": self.url,
        "protocolBinding": self.protocol_binding,
        "protocolVersion": self.protocol_version,
    }
    if self.tenant is not None:
      interface["tenant"] = self.tenant
    return {
        "name": self.card_name or self.name,
        "description": self.card_description or self.description,
        "supportedInterfaces": [interface],
        "version": self.version,
        "skills": [s.to_dict() for s in self.skills],
    }

  def tool_payload(self) -> dict[str, Any]:
    """The `remoteAgentTool` block of the emitted tool resource.

    The emitter wraps this with the resource-level `name` (a fresh UUID) and
    `displayName`, exactly as it does for a python tool.
    """
    return {
        "name": self.name,
        "description": self.description,
        "agentCard": self.agent_card(),
    }


def _build_agent(
    caller: str,
    name: str,
    *,
    description: str,
    url: str = "",
    skills: Sequence[AgentSkill] = (),
    version: str = DEFAULT_AGENT_VERSION,
    protocol_binding: str = DEFAULT_PROTOCOL_BINDING,
    protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    tenant: Optional[str] = None,
    card_name: Optional[str] = None,
    card_description: Optional[str] = None,
    **deferred: Any,
) -> RemoteAgent:
  """Shared construction + validation for `remote_agent` and `ces_agent`.

  Checked here rather than at push because a card CES rejects fails a deploy that has
  already overwritten the target app, which is a much worse place to find out.
  """
  nm = _require(name, f"{caller}(): name")
  if not _NAME_RE.match(nm):
    raise ValueError(
        f"{caller}(): name {nm!r} must contain only letters, digits, '_' or '-' — "
        "it is the tool's displayName and is referenced as {@TOOL: name}"
    )
  desc = _require(description, f"{caller}({nm!r}): description")
  if url and not url.startswith("https://"):
    raise ValueError(
        f"{caller}(): url must be an absolute https:// URL, got {url!r}"
    )
  bad = [s for s in skills if not isinstance(s, AgentSkill)]
  if bad:
    raise ValueError(
        f"{caller}({nm!r}): skills must be built with flows.agent_skill(), got "
        f"{type(bad[0]).__name__}"
    )
  # The API requires at least one skill. For a single-purpose agent the tool name and
  # description already say what it does, so derive that one skill rather than making
  # the author write it twice. Naming skills explicitly is still better when an agent
  # does several distinct things — they are the model's per-capability routing signal.
  resolved_skills = tuple(skills) or (agent_skill(nm, description=desc),)
  return RemoteAgent(
      name=nm,
      description=desc,
      url=url,
      skills=resolved_skills,
      version=_require(version, f"{caller}({nm!r}): version"),
      protocol_binding=_require(
          protocol_binding, f"{caller}({nm!r}): protocol_binding"),
      protocol_version=_require(
          protocol_version, f"{caller}({nm!r}): protocol_version"),
      tenant=tenant,
      card_name=card_name,
      card_description=card_description,
      **deferred,
  )


def remote_agent(
    name: str,
    *,
    description: str,
    url: str,
    skills: Sequence[AgentSkill] = (),
    version: str = DEFAULT_AGENT_VERSION,
    protocol_binding: str = DEFAULT_PROTOCOL_BINDING,
    protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    tenant: Optional[str] = None,
    card_name: Optional[str] = None,
    card_description: Optional[str] = None,
) -> RemoteAgent:
  """Declare a remote A2A agent as a callable tool.

  Only `name`, `description` and `url` are needed — the rest of the card is defaulted
  to what a single-purpose HTTP+JSON agent wants.

  Args:
    name: The tool name. Letters, digits, `_` and `-` only — it reaches CES as a
      `displayName` and is referenced from instructions as `{@TOOL: name}`.
    description: What the agent is for. This is the model's primary signal for whether
      to call it, so describe the domain, not the transport. Required precisely
      because no default could carry that signal.
    url: The agent's A2A endpoint. Must be absolute HTTPS.
    skills: What the agent can do. Defaults to one skill derived from the name +
      description; name them when the agent does several distinct things.
    version: The remote agent's version string. Defaults to `1.0.0`.
    protocol_binding: `HTTP+JSON` (default), `JSONRPC`, or `GRPC`.
    protocol_version: The A2A protocol version the endpoint speaks. Defaults to `1.0`.
    tenant: Tenant id to send with the call, when the agent is multi-tenant.
    card_name: Card `name`, when it differs from the tool name.
    card_description: Card `description`, when it differs from the tool description.

  Returns:
    A `RemoteAgent` to pass to `App(remote_agents=[...])`.
  """
  _require(url, "remote_agent(): url")
  return _build_agent(
      "remote_agent", name, description=description, url=url, skills=skills,
      version=version, protocol_binding=protocol_binding,
      protocol_version=protocol_version, tenant=tenant, card_name=card_name,
      card_description=card_description)


def ces_agent(
    name: str,
    *,
    description: str,
    app_id: str,
    skills: Sequence[AgentSkill] = (),
    project: Optional[str] = None,
    location: Optional[str] = None,
    **kwargs: Any,
) -> RemoteAgent:
  """A remote agent that is itself a CES app, addressed by its app id.

  A deployed CES app is an A2A endpoint with no extra configuration — that is the
  inbound half of the protocol, and it is why this is sugar over `remote_agent`
  rather than a second mechanism. Use it to call one CXAS agent from another.

  `project` and `location` default to the calling App's, since the common case is two
  apps deployed side by side. Whichever is omitted is filled in at build, because the
  App does not exist yet when the agent is declared — and deferring lets one
  declaration be built into two projects. Give them explicitly to reach an app
  elsewhere (which also needs `ces.sessions.runSession` on that app).
  """
  app = _require(app_id, "ces_agent(): app_id")
  agent = _build_agent(
      "ces_agent", name, description=description, skills=skills,
      ces_app_id=app, ces_project=project or None, ces_location=location or None,
      **kwargs)
  # Nothing left to inherit -> resolve now, so `.url` / `.agent_card()` work on the
  # spot. Note a project WITHOUT a location still defers: silently defaulting that to
  # "us" would point an app deployed in eu at the wrong region.
  if project and location:
    return agent.resolved_for(project, location)
  return agent


# ---------------------------------------------------------------------------
# Slot-filling delegation: fire a remote agent from a task and land its reply
# in a slot. See the module docstring for why this cannot be a bare `task()`.
# ---------------------------------------------------------------------------


def unwrap_tool_source(tool_name: str, envelope_param: str) -> str:
  """Source for the generated tool that reads reply text out of an A2A envelope.

  Runs in the CES sandbox (stdlib only, no `from __future__` — see `tools._HEADER`).
  It is a pure dict walk: a sandboxed tool cannot make the A2A call itself, so the
  platform makes it, intake parks the raw reply in a slot, and this reads it back.

  Both kinds are handled because a slot filled by the `message` reply and one filled by
  a completed `task` reply reach here identically — the caller cannot tell which the
  remote agent used, and neither should the flow.
  """
  return (
      f'"""Read the reply text out of an A2A response envelope (generated)."""\n'
      "from typing import Any\n\n\n"
      "def _texts(parts: Any) -> list:\n"
      '  """Text of every text-bearing part, in order."""\n'
      "  out = []\n"
      "  for part in parts or []:\n"
      "    if isinstance(part, dict) and part.get('text'):\n"
      "      out.append(str(part['text']))\n"
      "  return out\n\n\n"
      f"def {tool_name}({envelope_param}: Any = None) -> dict[str, Any]:\n"
      '  """Extract the remote agent\'s reply from its A2A envelope.\n\n'
      "  Args:\n"
      f"    {envelope_param}: The A2A `message` or `task` reply parked by the call task.\n\n"
      "  Returns:\n"
      "    Dict with `reply` (the text), `state`, and a `success` flag.\n"
      '  """\n'
      f"  env = {envelope_param} if isinstance({envelope_param}, dict) else {{}}\n"
      "  # `message` reply: the text is the message's parts.\n"
      "  parts = _texts(env.get('parts'))\n"
      "  state = ''\n"
      "  if not parts:\n"
      "    # `task` reply: the terminal text rides the status message's content, and\n"
      "    # anything the agent produced rides its artifacts.\n"
      "    status = env.get('status') or {}\n"
      "    state = str(status.get('state') or '')\n"
      "    parts = _texts((status.get('message') or {}).get('content'))\n"
      "    if not parts:\n"
      "      for artifact in env.get('artifacts') or []:\n"
      "        if isinstance(artifact, dict):\n"
      "          parts.extend(_texts(artifact.get('parts')))\n"
      "  reply = ' '.join(p.strip() for p in parts if p.strip())\n"
      "  return {'reply': reply, 'state': state, 'success': bool(reply)}\n"
  )


@dataclass
class Delegation:
  """What `delegate()` produces: the slots to add and the tasks to run.

  Splice both into the flow — `flow.add(*d.slots).task(*d.tasks)`. The generated
  unwrap tool registers itself, so nothing else needs wiring.
  """

  slots: list[dict[str, Any]] = field(default_factory=list)
  tasks: list[dict[str, Any]] = field(default_factory=list)
  envelope_slot: str = ""
  reply_slot: str = ""
  unwrap_tool: str = ""

  def __iter__(self):
    """Unpack as `slots, tasks = delegate(...)`."""
    return iter((self.slots, self.tasks))


def delegate(
    name: str,
    agent: RemoteAgent,
    *,
    request_slot: str,
    reply_slot: Optional[str] = None,
    envelope_slot: Optional[str] = None,
    context_slot: Optional[str] = None,
    expect: str = A2A_MESSAGE_REPLY,
    awaits: Optional[dict[str, Any]] = None,
    on_failure: Optional[dict[str, Any]] = None,
    condition: Optional[Any] = None,
    requires: Optional[list[str]] = None,
    then_say: Any = None,
    terminal: bool = False,
) -> Delegation:
  """Hand a collected request to a remote A2A agent and land its reply in a slot.

  Two tasks, because the platform's answer is an envelope and a slot wants text:

  1. `<name>` fires the remote agent with the contents of `request_slot` and parks
     the raw reply (by default the `message` one) in `envelope_slot`.
  2. `<name>_read` runs a generated, sandbox-safe unwrap tool over that reply and
     fills `reply_slot` with the text.

  `expect` says which kind of reply to consume, and it has to be
  exactly one of them: intake requires every key in `outputs` to be present, so
  mapping both would demand a response carrying both at once — which a oneof never is.

  The default is the `message` reply, the one that carries a finished answer. An agent
  that replies with the `task` reply has accepted the work but not done it, so under the
  default that is a miss and routes to `on_failure` rather than filling a slot with a
  receipt. Pass `expect="task"` for a remote agent that answers with COMPLETED `task` replies:
  the unwrap then reads the text out of the status message or the artifacts, and a
  task still in flight yields no text, so the read fails instead of filling the slot
  with an empty string.

  `awaits` is narrower here than it looks, and worth being exact about. The engine
  enters a wait ONLY for CES's own `{"result": "pending"}` placeholder
  (`slot_filling_engine._is_async_pending`) — the deferral CES performs when it will
  not answer the call this turn. It does not engage for the `task` reply, which is a
  real response and is judged by `success_check` like any other. So `awaits` covers
  "CES deferred the call", not "the remote agent is thinking".

  Args:
    name: The call task's name. The `<name>_read` task and the generated
      `<name>_unwrap` tool are derived from it.
    agent: The `RemoteAgent` to call. Splicing the returned tasks into a flow declares
      it, so it does not need repeating on `App(remote_agents=[...])` — put it there
      as well only to ALSO make it model-callable from the instruction.
    request_slot: Slot holding the natural-language request. Its value is sent as
      the tool's `task` parameter.
    reply_slot: Slot to fill with the remote agent's reply text. Defaults to
      `<name>_reply`.
    envelope_slot: Slot to park the raw A2A reply in. Defaults to `<reply_slot>_envelope`.
      It is an intermediate, not something to speak.
    context_slot: Slot holding an A2A `contextId` from an earlier reply, to continue
      that conversation instead of starting a new one.
    expect: Which kind of reply to consume — `"message"` (default, a finished
      answer) or `"task"` (for an agent that answers with completed `task` replies).
    awaits: `flows.awaits(...)` policy, engaged when CES defers the call itself with
      its `{"result": "pending"}` placeholder. See above for what it does not cover.
    on_failure: Standard failure ladder for the call task.
    condition: Condition guarding the call task.
    requires: Prerequisites for the call task (defaults to its inputs).
    then_say: Spoken when the reply task completes.
    terminal: Mark the reply task terminal.

  Returns:
    A `Delegation` carrying `slots` and `tasks` to splice into the flow.
  """
  from . import dsl as _dsl  # local: dsl imports this module's App field type

  if not isinstance(agent, RemoteAgent):
    raise ValueError(
        "delegate(): agent must be a flows.remote_agent(...) / flows.ces_agent(...), "
        f"got {type(agent).__name__}"
    )
  if expect not in A2A_REPLY_KINDS:
    raise ValueError(
        f"delegate(): expect must be one of {A2A_REPLY_KINDS}, got {expect!r} — those are the two "
        "halves of the A2A reply oneof, and a task can consume exactly one"
    )
  task_name = _require(name, "delegate(): name")
  req_slot = _require(request_slot, "delegate(): request_slot")
  out_slot = reply_slot or f"{task_name}_reply"
  env_slot = envelope_slot or f"{out_slot}_envelope"
  if env_slot == out_slot:
    raise ValueError(
        f"delegate({task_name!r}): envelope_slot and reply_slot must differ — the "
        "envelope is the raw A2A reply and the reply is the text read out of it"
    )
  unwrap_name = f"{task_name}_unwrap"
  # `_read`, not `_reply`: the default reply SLOT is `<name>_reply`, and a task and a
  # slot sharing one name reads as a mistake even where nothing collides.
  reply_task_name = f"{task_name}_read"

  # The remote agent tool's parameters are the platform's (`task`/`contextId`), not
  # the slot names, so inputs go in the dict form the engine maps by name.
  inputs: dict[str, str] = {req_slot: A2A_REQUEST_PARAM}
  if context_slot:
    inputs[context_slot] = A2A_CONTEXT_PARAM

  call = _dsl.task(
      task_name,
      agent.name,
      inputs,
      env_slot,
      out_key=expect,
      success_check=expect,
      requires=requires if requires is not None else [req_slot],
      condition=condition,
      awaits=awaits,
      on_failure=on_failure,
  )
  # Carry the agent itself on the call task. `Flow.task` lifts it off and the build
  # unions it with `App(remote_agents=...)`, so splicing the delegation is enough to
  # declare the agent. Naming it in both places is still fine and still deduplicates —
  # but forgetting the second used to emit a python STUB under the agent's name, which
  # answers in its place and is invisible until someone talks to the deployed app.
  call[_dsl._REMOTE_AGENT_KEY] = agent

  # The unwrap tool is generated source, so it registers by source rather than by
  # decorating a function — `collect_tools` renders both the same way.
  _tools.register_source_tool(
      unwrap_name,
      unwrap_tool_source(unwrap_name, env_slot),
      output_keys=["reply", "state", "success"],
  )
  read = _dsl.task(
      reply_task_name,
      unwrap_name,
      [env_slot],
      out_slot,
      out_key="reply",
      requires=[env_slot],
      then_say=then_say,
      terminal=terminal,
  )
  return Delegation(
      slots=[_dsl.result_slot(env_slot, task_name),
             _dsl.result_slot(out_slot, reply_task_name)],
      tasks=[call, read],
      envelope_slot=env_slot,
      reply_slot=out_slot,
      unwrap_tool=unwrap_name,
  )
