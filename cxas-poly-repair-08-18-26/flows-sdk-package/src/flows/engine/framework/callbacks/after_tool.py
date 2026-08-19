# pylint: disable=invalid-name,undefined-variable,unused-argument,broad-exception-caught,line-too-long
"""After-tool callback — thin CES adapter over the slot_intake tool.

FRAMEWORK CODE — fully generic across all agents. Extracts the tool result and
context from CES, delegates the (sm, tool_result) -> sm reduction to the
slot_intake tool (the shared intake logic), and applies its outputs back to CES
state. All routing logic lives in slot_intake; this file only crosses the CES
boundary (CES types can't cross a tool call, and CES has no imports — a tool is
the only way to share the logic across agents).
"""

import json as json_lib
import logging
import traceback
from typing import Any, Optional


_SM_KEY = "sm"

_logger = logging.getLogger("bella_notte.after_tool")
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}


# sm["_log"] is serialized into session state every turn; cap it (ring buffer).
_LOG_CAP = 200


def _log(sm, tag, level="INFO", **data):
  """Emit a structured log entry to sm["_log"] (mirrors the other callbacks)."""
  min_level = sm.get("_log_level", "INFO")
  if _LEVEL_ORDER.get(level, 1) < _LEVEL_ORDER.get(min_level, 1):
    return
  entry = {"src": "after_tool", "tag": tag, "level": level,
           "data": {k: v for k, v in data.items() if v is not None}}
  _logger.log(logging.INFO, json_lib.dumps(entry, default=str))
  _lst = sm.setdefault("_log", [])
  _lst.append(entry)
  if len(_lst) > _LOG_CAP:
    del _lst[:-_LOG_CAP]


def _get_current_agent_name(callback_context) -> str:
  """Derive current agent display name dynamically from active_config_id."""
  config_id = callback_context.state.get("_active_config_id")
  if not config_id:
    return ""
  raw_map = callback_context.state.get("agent_config_map", "{}")
  try:
    config_map = (
        json_lib.loads(raw_map) if isinstance(raw_map, str) else raw_map
    )
  except Exception:
    config_map = {}
  for name, cid in config_map.items():
    if cid == config_id:
      return name
  return ""


def after_tool_callback(
    tool: Tool,
    tool_input: dict[str, Any],
    callback_context: CallbackContext,
    tool_response: dict[str, Any],
) -> Optional[dict[str, Any]]:
  """Delegate setter/executor/flow result routing to the slot_intake tool."""
  sm = callback_context.state.get(_SM_KEY, {})
  # A leg of a parallel group is ingested by before_model, not here. The legs run
  # CONCURRENTLY, so this callback is invoked once per leg with all of them racing on
  # one state key: read the slot machine, fold this result in, write it back. Measured,
  # that keeps ONE leg of three and drops the rest — values and all, with no error
  # (ces-probes 37, 38). before_model runs once per pass rather than once per leg, so it
  # can ingest the whole batch with a single writer. Returning early here writes nothing
  # at all, which is what makes that safe. Absent marker -> unchanged behaviour.
  if tool.name in (sm.get("_parallel_firing") or []):
    return None
  # Crash envelope: this is the least-controllable input path (arbitrary tool
  # output -> slot_intake -> the engine), and an uncaught exception here surfaces
  # to the user as the empty "having trouble" render. Mirror the before_model
  # envelope: log the crash to sm._log, persist it, and leave state intact rather
  # than propagate. (before_model is wrapped; this path historically was not.)
  try:
    result = tools.slot_intake(
        {"input_data": {
            "tool_name": tool.name,
            "response_data": tool_response.get("result", tool_response),
            "sm": sm,
            "current_agent": _get_current_agent_name(callback_context),
            "channel": callback_context.state.get("channel", ""),
        }},
    ).json()["result"]
  except Exception as _e:  # pylint: disable=broad-except
    _log(sm, "after_tool_CRASH", level="ERROR",
         tool=getattr(tool, "name", "?"), err=repr(_e),
         tail=traceback.format_exc().strip().splitlines()[-1][:160])
    callback_context.state[_SM_KEY] = sm  # persist the log; leave state intact
    return None
  callback_context.state[_SM_KEY] = result["sm"]
  # Side-effect outputs the next before_model pass reads. Both are optional —
  # set them only when slot_intake produced one (leave any prior value untouched).
  if result.get("transfer_slots"):
    callback_context.state["_transfer_slots"] = result["transfer_slots"]
  if result.get("pending_transfer"):
    callback_context.state["_pending_transfer"] = result["pending_transfer"]
  return None
