# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

from typing import Any


def check_outage_before(account_number: str) -> dict[str, Any]:
  """Prepares arguments for EDE outage check and sets initial state.

  Args:
      account_number: The customer billing account number.

  Returns:
      dict with outage_api_args.
  """
  if not account_number:
    return {
        "status": "error",
        "error": "account_number is required.",
    }

  _audit_request = {
      "account_number": account_number,
  }
  print(
      "[AUDIT] [check_outage_before] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"[check_outage_before] account_number: {account_number}")

  try:
    context.state["outage_status"] = "PENDING_BACKEND_RESULT"
    print("[check_outage_before] Set outage_status to PENDING_BACKEND_RESULT")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[check_outage_before] Could not set state variable: {e}")

  # The parent-level before_tool_callback dispatches the in-progress checklist visuals now

  ede_outage_server = str(context.state.get("ede_outage_server") or "").rstrip("/")
  if not ede_outage_server:
    print("[ERROR] [check_outage_before] ede_outage_server variable is missing from context state!")
    _audit_response = {
        "status": "error",
        "error": "Missing required server configuration: 'ede_outage_server'",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [check_outage_before] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  outage_api_args = {
      "x-url": f"{ede_outage_server}/resicustomer/api/ede/execute",
      "agent_name": "gecx_repair_agent",
      "x-auth": "CECMT-SAT-XAXLR",
      "x-scope": "ceconvoy:acre:execution",
      "x-cache-refresh": "FORCE-REFRESH",
      "context": {
          "accountNumber": account_number,
          "cacheRefresh": "FORCE_REFRESH",
          "customerGuid": "",
          "placementInfo": [{
              "property": "Xfinity Assistant",
              "section": "Repair",
              "placement": "Troubleshooting",
              "group": 1,
              "sequence": 1,
          }],
      },
  }

  _audit_response = {
      "status": "success",
      "outage_api_args": outage_api_args,
  }
  print(
      "[AUDIT] [check_outage_before] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response
