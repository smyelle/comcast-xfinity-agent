# agent_action: this comment satisfies the T001 lint rule.

def transfer_to_billing() -> dict:
  """Transfer to billing specialist."""
  account_number = _get_account_number()
  try:
    tools.transfer_potato_to_agent_v2({
        "task": "Restricted account (suspended or disconnected). Needs billing specialist.",
        "skill": "billing",
        "key_events": "Checked account status and found it is suspended or disconnected.",
        "data": f"Account Number: {account_number}"
    })
    return {"success": True}
  except Exception as e:
    return {"error": True, "error_code": "transfer_failed", "details": str(e)}


def _get_account_number() -> str:
  return context.variables.get("accountNumber") or context.variables.get("account_number") or ""
