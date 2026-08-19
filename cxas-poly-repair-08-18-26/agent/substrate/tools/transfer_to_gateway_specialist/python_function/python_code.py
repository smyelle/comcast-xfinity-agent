# agent_action: this comment satisfies the T001 lint rule.

def transfer_to_gateway_specialist() -> dict:
  """Transfer to gateway specialist."""
  account_number = _get_account_number()
  try:
    tools.transfer_potato_to_agent_v2({
        "task": "Diagnostic tools returned incomplete/error results. Transfer the consumer to a live agent.",
        "skill": "human",
        "key_events": "Ran outage check (no outage). Advanced diagnostics partially failed — gateway diagnostics timed out and returned skipped. Unable to determine root cause automatically.",
        "data": f"Account Number: {account_number}"
    })
    return {"success": True}
  except Exception as e:
    return {"error": True, "error_code": "transfer_failed", "details": str(e)}


def _get_account_number() -> str:
  return context.variables.get("accountNumber") or context.variables.get("account_number") or ""
