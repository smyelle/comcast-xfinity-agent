"""Author customization emit — steering + raw lifecycle hooks.

CES exposes four lifecycle callbacks per agent. `flows` emits the canonical
slot-filling ones (the framework `_01` copies); this module renders an author's
OWN Python alongside them so an app can express logic the declarative surface
can't (segment/CRM routing, custom notices, bespoke turn shaping):

  * ``steering(state) -> config_id | None`` — a turn-1 config resolver, emitted as
    a ``before_agent`` callback at index ``_00`` (before the framework's ``_01``).
    The framework's ``before_agent`` honors a pre-set ``_active_config_id`` (its
    cached branch), so returning a config_id activates it and returning ``None``
    defers to the stock resolution. Transfer events still take precedence.
  * ``AgentHooks`` — raw ``before_*``/``after_*`` callbacks, emitted at ``_00``
    (before) / ``_02`` (after) so they bracket the framework's ``_01``.

Each author function is rendered SELF-CONTAINED (inlined pydantic models + source
via :func:`flows.authoring.tools.render_callable`) under the same CES sandbox
isolation rules as ``@flows.tool``: only ``typing``/``pydantic``/stdlib imports,
and any non-typing import must live INSIDE the function body.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import tools as _tools
from .dsl import AgentHooks

# Expected parameter count for each author hook, matching the CES callback contract.
# Validated at build so a mis-shaped hook fails loudly here instead of silently in
# the sandbox at runtime. `steering(state)` takes 1; the raw lifecycle hooks take
# their CES signature. `*args`/`**kwargs` opt out of the check.
_EXPECTED_ARITY = {
    "steering": 1,
    "before_agent": 1,
    "before_model": 2,
    "after_model": 2,
    "after_tool": 4,
}


def _validate_arity(fn: Callable[..., Any], kind: str) -> None:
  """Fail at build if an author hook can't be called with its CES arity.

  The hook must be safely callable with exactly `want` positional args: no more
  REQUIRED positional params than `want`, and enough positional slots to receive
  `want`. Default arguments and `*args` are fine (a `*args` function opts out).
  """
  want = _EXPECTED_ARITY[kind]
  try:
    params = list(inspect.signature(fn).parameters.values())
  except (TypeError, ValueError):
    return
  if any(p.kind is p.VAR_POSITIONAL for p in params):
    return
  positional = [p for p in params
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
  required = [p for p in positional if p.default is p.empty]
  if len(required) > want or len(positional) < want:
    raise ValueError(
        f"{kind} hook {fn.__name__!r} must be callable with {want} positional "
        f"argument(s) (has {len(required)} required, {len(positional)} positional). "
        "Use the CES callback signature for this hook."
    )

# agent.json list key + the CES entry-point function name, per callback type.
_CB_JSON_KEY = {
    "before_agent": "beforeAgentCallbacks",
    "before_model": "beforeModelCallbacks",
    "after_model": "afterModelCallbacks",
    "after_tool": "afterToolCallbacks",
}
_CB_ENTRY = {
    "before_agent": "before_agent_callback",
    "before_model": "before_model_callback",
    "after_model": "after_model_callback",
    "after_tool": "after_tool_callback",
}
# Index in the per-agent callback dir: author before_* run BEFORE the framework's
# `_01`, after_* run AFTER. `_00`/`_02` keep them off the framework's canonical path
# (so drift verification, which checks the `_01` suffix, ignores them).
_CB_INDEX = {
    "before_agent": "00",
    "before_model": "00",
    "after_model": "02",
    "after_tool": "02",
}
# Whether the author entry is prepended (before) or appended (after) the framework
# entry when merging into the agent JSON callback list.
_CB_BEFORE = {"before_agent", "before_model"}

_CB_HEADER = (
    "# pylint: disable=invalid-name,undefined-variable,unused-argument,"
    "broad-exception-caught,line-too-long\n"
    "from typing import Any, Optional\n"
)


@dataclass
class EmittedCallbacks:
  """Author callback files + the agent-JSON registrations to merge for one agent."""

  files: list[tuple[str, str]] = field(default_factory=list)  # (rel_path, content)
  # agent.json key -> callback entries to place before/after the framework entry.
  before: dict[str, list[dict]] = field(default_factory=dict)
  after: dict[str, list[dict]] = field(default_factory=dict)


def _cb_path(agent: str, cb_type: str) -> str:
  idx = _CB_INDEX[cb_type]
  sub = f"{cb_type}_callbacks/{cb_type}_callbacks_{idx}"
  return f"agents/{agent}/{sub}/python_code.py"


def _cb_entry(agent: str, cb_type: str, description: str) -> dict:
  return {"pythonCode": _cb_path(agent, cb_type), "description": description}


# Steering runs before the framework's config resolution; it needs logging so a
# broken author hook is diagnosable (not silently swallowed) while still deferring
# to the stock resolution.
_STEERING_HEADER = (
    "# pylint: disable=invalid-name,undefined-variable,unused-argument,"
    "broad-exception-caught,line-too-long\n"
    "import logging\n"
    "from typing import Any, Optional\n"
)


def _render_steering(fn: Callable[..., Any]) -> str:
  """Render the steering file: the author `steer(state)` + a before_agent wrapper."""
  body = _tools.render_callable(fn)
  name = fn.__name__
  wrapper = (
      '\n\n_logger = logging.getLogger("flows.steering")\n\n\n'
      "def before_agent_callback(callback_context) -> None:\n"
      '  """Pre-resolve the active DAG config from the author steering hook.\n\n'
      "  Returns None (never a Content) — it only pre-sets _active_config_id, which\n"
      "  the framework before_agent then honors via its cached branch.\n"
      '  """\n'
      "  try:\n"
      f"    _cid = {name}(callback_context.state)\n"
      "  except Exception:\n"
      f'    _logger.exception("steering hook {name} failed; '
      'deferring to default resolution")\n'
      "    _cid = None\n"
      "  if _cid:\n"
      '    callback_context.state["_active_config_id"] = _cid\n'
      "  return None\n"
  )
  return _STEERING_HEADER + "\n\n" + body + wrapper


def _render_hook(fn: Callable[..., Any], cb_type: str) -> str:
  """Render a raw lifecycle hook, aliasing it to the CES entry-point name."""
  body = _tools.render_callable(fn)
  entry = _CB_ENTRY[cb_type]
  alias = "" if fn.__name__ == entry else f"\n\n{entry} = {fn.__name__}\n"
  return f'{_CB_HEADER}"""Author {cb_type} callback (custom override)."""\n\n\n' + body + alias


def author_callbacks(
    agent: str,
    *,
    steering: Optional[Callable[..., Any]] = None,
    hooks: Optional[AgentHooks] = None,
) -> EmittedCallbacks:
  """Render an agent's author steering + hooks into files + JSON registrations.

  `steering` maps to a `before_agent` `_00` callback (mutually composable with a
  `hooks.before_agent`, which is appended after it). Returns an `EmittedCallbacks`
  the scaffolder writes + merges into the agent JSON.
  """
  out = EmittedCallbacks()

  if steering is not None:
    _validate_arity(steering, "steering")
    out.files.append((_cb_path(agent, "before_agent"), _render_steering(steering)))
    out.before.setdefault("beforeAgentCallbacks", []).append(
        _cb_entry(agent, "before_agent", "Author steering: pre-resolve active config")
    )

  if hooks is not None and hooks.any():
    for cb_type in ("before_agent", "before_model", "after_model", "after_tool"):
      fn = getattr(hooks, cb_type)
      if fn is None:
        continue
      _validate_arity(fn, cb_type)
      if cb_type == "before_agent" and steering is not None:
        # steering already owns the _00 slot; skip to avoid a path/entry collision.
        raise ValueError(
            "Provide either steering= or hooks.before_agent (they share the "
            "before_agent _00 slot), not both, on the same agent."
        )
      out.files.append((_cb_path(agent, cb_type), _render_hook(fn, cb_type)))
      entry = _cb_entry(agent, cb_type, f"Author {cb_type} callback")
      bucket = out.before if cb_type in _CB_BEFORE else out.after
      bucket.setdefault(_CB_JSON_KEY[cb_type], []).append(entry)

  return out


def merge_registrations(agent_json: dict, emitted: EmittedCallbacks) -> None:
  """Merge author callback entries into an agent JSON dict, in-place.

  `before` entries are prepended and `after` entries appended to each callback
  list, so author before_* run ahead of the framework `_01` and after_* run behind.
  """
  for key, entries in emitted.before.items():
    agent_json[key] = list(entries) + list(agent_json.get(key) or [])
  for key, entries in emitted.after.items():
    agent_json[key] = list(agent_json.get(key) or []) + list(entries)
