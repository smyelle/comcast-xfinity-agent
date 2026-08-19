"""Before model callback for Gateway Specialist Agent."""

# pylint: disable=undefined-variable
# pylint: disable=unused-argument
from typing import Optional


def before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
  """Intercepts Gateway Specialist before LLM run to log inputs for GECX observability."""
  state = callback_context.state

  print("=== GECX Observability [gateway_specialist_agent] ===")
  print(f"triage_args: {state.get('triage_args', 'not_set')}")
  print(f"wifi_summary_args: {state.get('wifi_summary_args', 'not_set')}")
  print(f"client_wifi_args: {state.get('client_wifi_args', 'not_set')}")
  rdk_date = state.get("RDK_CURRENT_DATE_FORMATTED", "not_set")
  print(f"RDK_CURRENT_DATE_FORMATTED: {rdk_date}")
  print("========================================================")

  return None  # Continue and execute model normally
