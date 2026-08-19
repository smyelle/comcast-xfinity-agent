"""Calling another agent as a tool, by name rather than by URL.

`Tool.tool_type` has two ways to make an agent callable. `a2a.py` covers the first —
`remoteAgentTool`, an A2A card addressed by URL. This is the second: `agentTool`, which
names an agent in the SAME app. No card to keep in sync, no protocol, no inline copy of
the target's capabilities to drift out of date.

It is also the only one of the pair that can be ASYNCHRONOUS. An async `remoteAgentTool`
is dropped at deploy without a word (ces-probes 133), while this flavour defers and
completes normally (ces-probes 134) — which is the reason to reach for it on a voice call,
where a specialist that thinks for nine seconds otherwise holds the line.

Its wire contract is its own, and every part of it differs from A2A's:

    argument     `request`               (A2A: `task`)
    reply        {"response": "<text>"}  (A2A: a SendMessageResponse oneof)
    placeholder  {"response": "pending"} (a python tool: {"result": "pending"})

An author should not have to know any of that. `flows.task(...)` handed an `AgentTool`
fills all three in, which is what `SearchTool` already does for a search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Union

# The argument name the platform insists on. Learned the hard way: an `agentTool` called
# with A2A's `task` is rejected with "Missing request parameter in the agent tool call."
AGENT_REQUEST_PARAM = "request"
# The key an agent answers under — its result, its deferral placeholder, and (per the
# engine's verb fallback) the key a successful verdict is recorded against.
AGENT_REPLY_KEY = "response"

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _check_name(caller: str, kind: str, name: str) -> str:
  if not _NAME_RE.match(name or ""):
    raise ValueError(
        f"{caller}: {kind} name {name!r} must start with a letter and contain only "
        "letters, digits and underscores — it is used verbatim as a resource name")
  return name


@dataclass(frozen=True)
class HelperAgent:
  """A plain agent in this app, declared so something can call it.

  Deliberately not `flows.Agent`, which is the multi-agent form: that one needs a Flow of
  its own and is mutually exclusive with `root_flow`, so a single-flow app could not have
  a specialist at all. This is the smaller thing the platform already accepts — a name, an
  instruction, and optionally its own tools.
  """

  name: str
  instruction: str
  tools: tuple[str, ...] = ()

  def agent_json(self) -> dict[str, Any]:
    """The `agents/<name>/<name>.json` body, minus the instruction file path."""
    return {"name": self.name, "displayName": self.name, "tools": list(self.tools)}


@dataclass(frozen=True)
class AgentTool:
  """An agent, callable as a tool.

  `emit=False` declares a tool this app already carries — a converted agent's grafted
  resources, which arrive with their own `toolFakeConfig` and must not be overwritten.
  The declaration is then authoring metadata only: it still teaches `task()` the wire
  contract and still gives the validator something to check, but nothing is written.
  """

  name: str
  agent: str
  description: str
  asynchronous: bool = False
  emit: bool = True

  def tool_payload(self) -> dict[str, Any]:
    """The `agentTool` block of the emitted tool resource."""
    return {
        "name": self.name,
        "description": self.description,
        "agent": self.agent,
    }


def helper_agent(
    name: str,
    instruction: str,
    *,
    tools: Sequence[str] = (),
) -> HelperAgent:
  """Declare a plain agent in this app, for an `agent_tool` to call.

  Args:
    name: Its display name, used verbatim as the resource name.
    instruction: Its system instruction. A specialist reached as a tool answers one
      question and returns, so say what to answer and how briefly.
    tools: Tool names it may call.
  """
  _check_name("helper_agent", "agent", name)
  if not (instruction or "").strip():
    raise ValueError(f"helper_agent({name!r}): an agent with no instruction has nothing "
                     "to answer with")
  return HelperAgent(name=name, instruction=instruction, tools=tuple(tools))


def agent_tool(
    name: str,
    *,
    agent: Union[HelperAgent, str],
    description: str,
    asynchronous: bool = False,
    emit: bool = True,
) -> AgentTool:
  """Declare an agent as a callable tool.

  Args:
    name: The tool name a task fires and the model sees.
    agent: The `helper_agent` to call, or the display name of an agent in this app. A
      name is accepted because a converted app's agents can arrive by graft, after emit,
      where nothing can resolve them yet.
    description: What it answers. This is what the model reads.
    asynchronous: Emit `executionType: ASYNCHRONOUS`, so the call defers and the answer
      arrives a turn or more later. Give the task an `awaits` policy to cover the wait.
    emit: False for a tool this app already carries — declare it, do not write it.
  """
  _check_name("agent_tool", "tool", name)
  target = agent.name if isinstance(agent, HelperAgent) else agent
  _check_name("agent_tool", "agent", target)
  if not (description or "").strip():
    raise ValueError(
        f"agent_tool({name!r}): a description is what the model routes on; without one "
        "it cannot tell this tool from any other")
  return AgentTool(name=name, agent=target, description=description,
                   asynchronous=bool(asynchronous), emit=bool(emit))


def resolve_target(agent: Union[HelperAgent, str, None]) -> Optional[str]:
  """The display name an `agent=` argument refers to."""
  if agent is None:
    return None
  return agent.name if isinstance(agent, HelperAgent) else str(agent)
