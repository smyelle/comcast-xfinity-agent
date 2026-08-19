"""Before model callback for Network Specialist Agent."""

# pylint: disable=undefined-variable
# pylint: disable=unused-argument
from typing import Optional


def before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
  """Intercepts Network Specialist before LLM run to log inputs for GECX observability."""
  state = callback_context.state

  print("=== GECX Observability [network_specialist_agent] ===")
  net_analysis = state.get("network_analysis_args", "not_set")
  print(f"network_analysis_args: {net_analysis}")
  rdk_date = state.get("RDK_CURRENT_DATE_FORMATTED", "not_set")
  print(f"RDK_CURRENT_DATE_FORMATTED: {rdk_date}")
  print("========================================================")

  return None  # Continue and execute model normally
