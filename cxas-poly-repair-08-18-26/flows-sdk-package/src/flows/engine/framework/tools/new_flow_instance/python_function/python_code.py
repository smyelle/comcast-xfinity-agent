"""Start a fresh, separate instance of the current flow type.

FRAMEWORK CODE — shared verbatim across all agents using the slot-filling
engine. Do not add agent-specific logic (or an agent's flow names) here.

This tool is hidden until a request is already in progress, so it never adds to
the (fragile) entry-turn tool surface. It lets the user run several concurrent
instances of the same flow type: the after_tool callback stashes the current
instance and begins a clean one. Same agent — no transfer (use set_active_flow
to switch to a DIFFERENT flow).

VALIDATION: the set of real flow ids is agent-specific and unknown to framework
code, and the deployed model phrases the flow free-form from what the user
said — so any non-empty id is captured here and the engine's gate is what
rejects an unknown one. Only a missing/blank id is refused outright.
"""

from typing import Any


def new_flow_instance(flow: str) -> dict[str, Any]:
  """Start a SEPARATE, additional instance of the same flow type.

  Call when the user wants another request of the SAME kind they are already
  working on — NOT a correction to the current one and NOT a switch to a
  different flow. The current request is saved and can be resumed later with
  resume_flow.

  Args:
    flow: The flow type to start a fresh instance of (same as the current flow).

  Returns:
    Dict with stored=True, value=flow, and new_instance=True, or error=True
    with error_code="invalid_flow" when no flow id was supplied.
  """
  flow = str(flow or "").lower().strip()
  if not flow:
    return {"error": True, "error_code": "invalid_flow"}
  return {"stored": True, "value": flow, "new_instance": True}
