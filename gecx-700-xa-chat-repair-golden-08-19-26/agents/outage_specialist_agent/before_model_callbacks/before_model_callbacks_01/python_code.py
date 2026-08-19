"""Before model callback for Outage Specialist Agent."""

# pylint: disable=undefined-variable
# pylint: disable=unused-argument
from typing import Optional


def before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
  """Intercepts Outage Specialist before LLM run to log inputs for GECX observability."""
  state = callback_context.state

  print("=== GECX Observability [outage_specialist_agent] ===")
  acc_num = state.get("accountNumber") or state.get("account_id", "not_set")
  print(f"account_number: {acc_num}")
  print(f"outage_status: {state.get('outage_status', 'not_set')}")
  rdk_date = state.get("RDK_CURRENT_DATE_FORMATTED", "not_set")
  print(f"RDK_CURRENT_DATE_FORMATTED: {rdk_date}")
  print("========================================================")

  return None  # Continue and execute model normally
