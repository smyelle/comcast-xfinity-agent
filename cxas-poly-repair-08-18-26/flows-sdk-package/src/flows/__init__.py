"""flows — the slot-filling framework + authoring toolkit for CXAS/CES agents.

`flows` is the single source of truth for the slot-filling framework used by both
external agent builders and Labs (Slot Studio). It owns the runtime framework (the
engine, validator, intake, blessed control tools + callbacks) and the build-time
authoring surface: config models, offline simulator, and the CXAS app emitter.

It is the build-time companion to `cxas-scrapi` (the runtime/interaction library):
`flows` *authors* an agent; `cxas-scrapi` *drives/deploys* it.

Core surface (stable):
    from flows.config import models, validation, config_io
    from flows.engine import blessed_source, loader
    from flows.sim import engine_sim
    from flows.emit import scaffold

Authoring surface (see flows.authoring): Flow/DSL, YAML loader, and the @tool
decorator are re-exported at the top level as they land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .engine import blessed_source

# The authoring surface (Flow/DSL/@tool/emit/YAML) is exposed LAZILY via PEP 562
# `__getattr__`. Keeping `import flows` (and importing any single submodule, which
# runs this __init__) light is essential: Slot Studio's compatibility shims import
# `flows.emit.scaffold` / `flows.engine.loader` directly, and eagerly pulling the
# authoring stack here would create an import cycle (build -> emit.scaffold) and
# clash with other products' own `emit` modules under pytest's sys.path.
if TYPE_CHECKING:  # for type-checkers/IDEs only; no runtime import cost or cycle
  from .authoring.build import emit as build_app  # noqa: F401
  from .authoring.build import validate_app  # noqa: F401
  from .authoring.dsl import (  # noqa: F401
      Agent,
      AgentHooks,
      App,
      Flow,
      HostRouter,
      Operation,
      VerdictBranch,
      announce,
      answer,
      cancel,
      component,
      content_announce,
      eq,
      escalate,
      escalated,
      event_slot,
      gate,
      has,
      hold_and_wait,
      intent_slot,
      journey,
      ne,
      no_input,
      on_interrupted,
      continue_cues,
      push_back,
      passive_slot,
      awaits,
      readback,
      repeated,
      result_slot,
      router_flow,
      setter_group,
      task,
      unset,
      user_slot,
      verdict,
  )
  from .authoring.steering import (  # noqa: F401
      Disambiguation,
      Route,
      SteeringSpec,
      disambiguation,
      route,
  )
  from .authoring.a2a import (  # noqa: F401
      AgentSkill,
      RemoteAgent,
      agent_skill,
      ces_agent,
      delegate,
      remote_agent,
  )
  from .authoring.search import (  # noqa: F401
      SearchTool,
      search_tool,
      AgentTool,
      agent_tool,
      HelperAgent,
      helper_agent,
  )
  from .authoring.handoff import (  # noqa: F401
      EndParamsHandoff,
      Handoff,
      HandoffPayload,
      dialogflow_cx,
      end_params_handoff,
      handoff,
      cxas,
      ujet,
  )
  from .authoring.mcp import (  # noqa: F401
      McpTool,
      McpToolset,
      mcp_tool,
      mcp_toolset,
  )
  from .authoring.openapi import (  # noqa: F401
      ApiTool,
      OpenApiToolset,
      after_turns,
      api_tool,
      openapi_toolset,
      remote_error,
      remote_tool,
  )
  from .authoring.toolset_common import (  # noqa: F401
      ToolsetAuth,
      api_key_auth,
      bearer_auth,
      oauth_auth,
      service_agent_auth,
  )
  from .authoring.guardrails import (  # noqa: F401
      Guardrail,
      blocklist,
      generate,
      policy,
      prompt_guard,
      respond,
      safety,
      transfer_to,
  )
  from .authoring.render import (  # noqa: F401
      raw,
      render_app_source,
      render_config_source,
  )
  from .authoring.tools import tool  # noqa: F401
  from .authoring.validators import luhn_valid  # noqa: F401
  from .authoring.yaml_loader import (  # noqa: F401
      compile_condition,
      flow_from_dict,
      load_app,
      load_flow,
  )
  from .cujs import (  # noqa: F401
      CUJ,
      CUJSet,
      apply_to_app_dir,
      cuj_variables,
      find_cujs_file,
      load_cujs,
  )
  from .authoring.dsl import Fallback, fallback, since  # noqa: F401
  from .authoring.variable_maps import (  # noqa: F401
      Bind,
      VariableMap,
      bind,
      variable_map,
  )
  from .drive import chat, open_session, run_steps  # noqa: F401

# name -> (submodule, attribute) for lazy resolution. NOTE: the public author
# function is `build_app` (not `emit`) because `flows.emit` is a SUBPACKAGE
# (scaffold/models) — a top-level `emit` attribute would resolve to that package.
_LAZY = {
    "build_app": ("flows.authoring.build", "emit"),
    "validate_app": ("flows.authoring.build", "validate_app"),
    "App": ("flows.authoring.dsl", "App"),
    "Agent": ("flows.authoring.dsl", "Agent"),
    "AgentHooks": ("flows.authoring.dsl", "AgentHooks"),
    "HostRouter": ("flows.authoring.dsl", "HostRouter"),
    "Operation": ("flows.authoring.dsl", "Operation"),
    "VerdictBranch": ("flows.authoring.dsl", "VerdictBranch"),
    "verdict": ("flows.authoring.dsl", "verdict"),
    "content_announce": ("flows.authoring.dsl", "content_announce"),
    "Flow": ("flows.authoring.dsl", "Flow"),
    "router_flow": ("flows.authoring.dsl", "router_flow"),
    # Steering — first-class model-classified routing: route(...) objects for router_flow.
    "route": ("flows.authoring.steering", "route"),
    "Route": ("flows.authoring.steering", "Route"),
    "SteeringSpec": ("flows.authoring.steering", "SteeringSpec"),
    "disambiguation": ("flows.authoring.steering", "disambiguation"),
    "Disambiguation": ("flows.authoring.steering", "Disambiguation"),
    "journey": ("flows.authoring.dsl", "journey"),
    "user_slot": ("flows.authoring.dsl", "user_slot"),
    "intent_slot": ("flows.authoring.dsl", "intent_slot"),
    "passive_slot": ("flows.authoring.dsl", "passive_slot"),
    "setter_group": ("flows.authoring.dsl", "setter_group"),
    "awaits": ("flows.authoring.dsl", "awaits"),
    "repeated": ("flows.authoring.dsl", "repeated"),
    "readback": ("flows.authoring.dsl", "readback"),
    "cancel": ("flows.authoring.dsl", "cancel"),
    "escalate": ("flows.authoring.dsl", "escalate"),
    "escalated": ("flows.authoring.dsl", "escalated"),
    "no_input": ("flows.authoring.dsl", "no_input"),
    "on_interrupted": ("flows.authoring.dsl", "on_interrupted"),
    "continue_cues": ("flows.authoring.dsl", "continue_cues"),
    "repair": ("flows.authoring.dsl", "repair"),
    "DEFAULT_CONTINUER_PHRASES": ("flows.authoring.continuers", "DEFAULT_CONTINUER_PHRASES"),
    "push_back": ("flows.authoring.dsl", "push_back"),
    "answer": ("flows.authoring.dsl", "answer"),
    "speech": ("flows.authoring.dsl", "speech"),
    "IMPROVISE_CLASSES": ("flows.authoring.dsl", "IMPROVISE_CLASSES"),
    "announce": ("flows.authoring.dsl", "announce"),
    "event_slot": ("flows.authoring.dsl", "event_slot"),
    "result_slot": ("flows.authoring.dsl", "result_slot"),
    "parallel": ("flows.authoring.dsl", "parallel"),
    "ParallelGroup": ("flows.authoring.dsl", "ParallelGroup"),
    "task": ("flows.authoring.dsl", "task"),
    "component": ("flows.authoring.dsl", "component"),
    "eq": ("flows.authoring.dsl", "eq"),
    "gate": ("flows.authoring.dsl", "gate"),
    "ne": ("flows.authoring.dsl", "ne"),
    "has": ("flows.authoring.dsl", "has"),
    "unset": ("flows.authoring.dsl", "unset"),
    "hold_and_wait": ("flows.authoring.dsl", "hold_and_wait"),
    # A2A (Agent2Agent) — call another agent as a tool.
    "remote_agent": ("flows.authoring.a2a", "remote_agent"),
    "ces_agent": ("flows.authoring.a2a", "ces_agent"),
    "agent_skill": ("flows.authoring.a2a", "agent_skill"),
    "delegate": ("flows.authoring.a2a", "delegate"),
    "RemoteAgent": ("flows.authoring.a2a", "RemoteAgent"),
    "AgentSkill": ("flows.authoring.a2a", "AgentSkill"),
    # Google Search grounding — answer from the web, not from the model's priors.
    "search_tool": ("flows.authoring.search", "search_tool"),
    "agent_tool": ("flows.authoring.agent_tool", "agent_tool"),
    "AgentTool": ("flows.authoring.agent_tool", "AgentTool"),
    "helper_agent": ("flows.authoring.agent_tool", "helper_agent"),
    "HelperAgent": ("flows.authoring.agent_tool", "HelperAgent"),
    "SearchTool": ("flows.authoring.search", "SearchTool"),
    # Telephony hand-off — the vendor payload + end_session that reach a human.
    "handoff": ("flows.authoring.handoff", "handoff"),
    "ujet": ("flows.authoring.handoff", "ujet"),
    "dialogflow_cx": ("flows.authoring.handoff", "dialogflow_cx"),
    "cxas": ("flows.authoring.handoff", "cxas"),
    "Handoff": ("flows.authoring.handoff", "Handoff"),
    "HandoffPayload": ("flows.authoring.handoff", "HandoffPayload"),
    # Native-channel return delivered on end_session.params (flow.on_end).
    "end_params_handoff": ("flows.authoring.handoff", "end_params_handoff"),
    "EndParamsHandoff": ("flows.authoring.handoff", "EndParamsHandoff"),
    # Toolsets — call an external service from a flow. Auth is shared across kinds.
    "openapi_toolset": ("flows.authoring.openapi", "openapi_toolset"),
    "api_tool": ("flows.authoring.openapi", "api_tool"),
    "remote_tool": ("flows.authoring.openapi", "remote_tool"),
    "after_turns": ("flows.authoring.openapi", "after_turns"),
    "remote_error": ("flows.authoring.openapi", "remote_error"),
    "OpenApiToolset": ("flows.authoring.openapi", "OpenApiToolset"),
    "ApiTool": ("flows.authoring.openapi", "ApiTool"),
    "mcp_toolset": ("flows.authoring.mcp", "mcp_toolset"),
    "mcp_tool": ("flows.authoring.mcp", "mcp_tool"),
    "McpToolset": ("flows.authoring.mcp", "McpToolset"),
    "McpTool": ("flows.authoring.mcp", "McpTool"),
    "api_key_auth": ("flows.authoring.toolset_common", "api_key_auth"),
    "oauth_auth": ("flows.authoring.toolset_common", "oauth_auth"),
    "bearer_auth": ("flows.authoring.toolset_common", "bearer_auth"),
    "service_agent_auth": ("flows.authoring.toolset_common", "service_agent_auth"),
    "ToolsetAuth": ("flows.authoring.toolset_common", "ToolsetAuth"),
    # Guardrails — the platform's own checks on the caller's turn and the agent's reply.
    "safety": ("flows.authoring.guardrails", "safety"),
    "blocklist": ("flows.authoring.guardrails", "blocklist"),
    "policy": ("flows.authoring.guardrails", "policy"),
    "prompt_guard": ("flows.authoring.guardrails", "prompt_guard"),
    "respond": ("flows.authoring.guardrails", "respond"),
    "generate": ("flows.authoring.guardrails", "generate"),
    "transfer_to": ("flows.authoring.guardrails", "transfer_to"),
    "Guardrail": ("flows.authoring.guardrails", "Guardrail"),
    # Polymorphic surfaces — one agent definition, many delivery surfaces.
    "say": ("flows.authoring.say", "say"),
    "card": ("flows.authoring.say", "card"),
    "chips": ("flows.authoring.say", "chips"),
    "action": ("flows.authoring.say", "action"),
    "link": ("flows.authoring.say", "link"),
    "Say": ("flows.authoring.say", "Say"),
    "Surface": ("flows.surfaces", "Surface"),
    "VOICE": ("flows.surfaces", "VOICE"),
    "CHAT": ("flows.surfaces", "CHAT"),
    "raw": ("flows.authoring.render", "raw"),
    "render_config_source": ("flows.authoring.render", "render_config_source"),
    "render_app_source": ("flows.authoring.render", "render_app_source"),
    "tool": ("flows.authoring.tools", "tool"),
    # Reusable value validators for hand-authored setters.
    "luhn_valid": ("flows.authoring.validators", "luhn_valid"),
    "load_flow": ("flows.authoring.yaml_loader", "load_flow"),
    "load_app": ("flows.authoring.yaml_loader", "load_app"),
    "flow_from_dict": ("flows.authoring.yaml_loader", "flow_from_dict"),
    "compile_condition": ("flows.authoring.yaml_loader", "compile_condition"),
    # Slot value policy — a conditional default for a slot nothing produced.
    "fallback": ("flows.authoring.dsl", "fallback"),
    "since": ("flows.authoring.dsl", "since"),
    "Fallback": ("flows.authoring.dsl", "Fallback"),
    # Variable maps — session variables the conversation arrives with, onto slots.
    "variable_map": ("flows.authoring.variable_maps", "variable_map"),
    "VariableMap": ("flows.authoring.variable_maps", "VariableMap"),
    "bind": ("flows.authoring.variable_maps", "bind"),
    "Bind": ("flows.authoring.variable_maps", "Bind"),
    # CUJ presets — `drive` is lazy for a second reason: it reaches into Slot Studio.
    "CUJ": ("flows.cujs", "CUJ"),
    "CUJSet": ("flows.cujs", "CUJSet"),
    "load_cujs": ("flows.cujs", "load_cujs"),
    "cuj_variables": ("flows.cujs", "cuj_variables"),
    "find_cujs_file": ("flows.cujs", "find_cujs_file"),
    "apply_to_app_dir": ("flows.cujs", "apply_to_app_dir"),
    "open_session": ("flows.drive", "open_session"),
    "run_steps": ("flows.drive", "run_steps"),
    "chat": ("flows.drive", "chat"),
}


def __getattr__(name: str):
  """Lazily import authoring symbols on first access (PEP 562)."""
  target = _LAZY.get(name)
  if target is None:
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
  import importlib

  value = getattr(importlib.import_module(target[0]), target[1])
  globals()[name] = value  # cache so subsequent access is direct
  return value


def __dir__():
  return sorted(list(globals()) + list(_LAZY))


def version() -> str:
  """The pinned framework (blessed bundle) version this wheel ships."""
  return blessed_source.version()


__all__ = [
    "version",
    "blessed_source",
    # authoring — DSL
    "App",
    "Agent",
    "AgentHooks",
    "HostRouter",
    "Operation",
    "VerdictBranch",
    "Flow",
    "router_flow",
    "route",
    "Route",
    "SteeringSpec",
    "disambiguation",
    "Disambiguation",
    "journey",
    "verdict",
    "user_slot",
    "intent_slot",
    "passive_slot",
    "setter_group",
    "awaits",
    "repeated",
    "readback",
    "cancel",
    "escalate",
    "no_input",
    "on_interrupted",
    "continue_cues",
    "repair",
    "DEFAULT_CONTINUER_PHRASES",
    "push_back",
    "answer",
    "speech",
    "IMPROVISE_CLASSES",
    "announce",
    "content_announce",
    "event_slot",
    "result_slot",
    "parallel",
    "ParallelGroup",
    "task",
    "component",
    "eq",
    "ne",
    "has",
    "unset",
    "escalated",
    "gate",
    # authoring — A2A remote agents
    "remote_agent",
    "ces_agent",
    "agent_skill",
    "delegate",
    "RemoteAgent",
    "AgentSkill",
    # authoring — Google Search grounding
    "search_tool",
    "agent_tool",
    "AgentTool",
    "helper_agent",
    "HelperAgent",
    "SearchTool",
    # authoring — telephony hand-off (live-agent / platform transfer payloads)
    "handoff",
    "ujet",
    "dialogflow_cx",
    "cxas",
    "Handoff",
    "HandoffPayload",
    "end_params_handoff",
    "EndParamsHandoff",
    # authoring — toolsets (OpenAPI + MCP; auth shared across kinds)
    "openapi_toolset",
    "remote_tool",
    "after_turns",
    "remote_error",
    "api_tool",
    "OpenApiToolset",
    "ApiTool",
    "mcp_toolset",
    "mcp_tool",
    "McpToolset",
    "McpTool",
    "api_key_auth",
    "oauth_auth",
    "bearer_auth",
    "service_agent_auth",
    "ToolsetAuth",
    # authoring — guardrails (platform-enforced checks, attached by display name)
    "safety",
    "blocklist",
    "policy",
    "prompt_guard",
    "respond",
    "generate",
    "transfer_to",
    "Guardrail",
    # authoring — Config -> DSL source renderer (migration deliverable)
    "raw",
    "render_config_source",
    "render_app_source",
    # authoring — YAML interop
    "load_flow",
    "load_app",
    "flow_from_dict",
    "compile_condition",
    # authoring — tools + build
    "tool",
    "luhn_valid",
    # The build entry point is `build_app`, NOT `emit`: `flows.emit` is a SUBPACKAGE
    # (scaffold/models), so exporting the name `emit` here does not reach
    # `flows.authoring.build.emit` at all. `__getattr__` below is only consulted when
    # ordinary attribute lookup FAILS, and for a package the import system binds an
    # imported submodule onto its parent — so `emit` resolved to whatever the import
    # order made it: `AttributeError` from a bare `import flows`, and the MODULE
    # object (not the callable) from `from flows import *`, which falls back to
    # importing the name as a submodule. Adding `"emit"` to `_LAZY` would be worse
    # still — order-dependent shadowing of a real subpackage. `test_public_surface.py`
    # pins that every name here has a binding.
    "build_app",
    "validate_app",
    # slot value policy
    "fallback",
    "Fallback",
    "since",
    # variable maps
    "variable_map",
    "VariableMap",
    "bind",
    "Bind",
    # CUJ presets
    "CUJ",
    "CUJSet",
    "load_cujs",
    "cuj_variables",
    "find_cujs_file",
    "apply_to_app_dir",
    "open_session",
    "run_steps",
    "chat",
]
