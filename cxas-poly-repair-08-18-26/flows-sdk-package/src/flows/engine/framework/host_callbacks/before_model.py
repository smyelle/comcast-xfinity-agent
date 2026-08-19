# pylint: disable=invalid-name,undefined-variable,unused-argument,broad-exception-caught,line-too-long
"""Before-model callback — HOST ROUTER (non-slot-filling steering agent).

FRAMEWORK CODE — generic across all host routers. A steering agent runs no DAG
engine: it only greets, classifies intent (via set_active_flow), and hands the
caller to a specialist. This callback is the router half of that:

  * keep the engine internals hidden from the model (slot_filling_engine +
    transfer_to_agent are framework-driven, never model-called), and
  * when after_tool has queued a `_pending_transfer` (set_active_flow returned a
    target_agent), emit the ADK agent transfer so the specialist takes over.

It is the minimal router extracted from the slot-filling before_model: the host
never resolves an `_active_config_id`, so the full engine path never runs here.
"""

from typing import Optional


_SM_KEY = "sm"


def before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
  """Dispatch a queued specialist transfer; otherwise let the host model reply."""
  llm_request.config.hide_tool("slot_filling_engine")
  llm_request.config.hide_tool("transfer_to_agent")

  agent = callback_context.state.pop("_pending_transfer", "")
  if agent:
    return LlmResponse.from_parts(
        parts=[Part.from_agent_transfer(agent=agent)],
    )
  return {"decision": "OK"}
