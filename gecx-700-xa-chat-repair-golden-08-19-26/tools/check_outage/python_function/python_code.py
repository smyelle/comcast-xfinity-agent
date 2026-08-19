# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

import json
import time
import traceback
from typing import Any


def check_outage(account_number: str) -> dict[str, Any]:
  """Calls EDE outage check and parses response for active outage info.

  Args:
      account_number: The customer billing account number.

  Returns:
      dict with outage_detected, outage_message, customer_message,
      impacted_services.
  """
  if not account_number:
    return {
        "status": "error",
        "outage_detected": False,
        "error": "account_number is required.",
    }

  _audit_request = {
      "account_number": account_number,
  }
  print(
      "[AUDIT] [check_outage] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"[check_outage] account_number: {account_number}")

  ede_outage_server = str(context.state.get("ede_outage_server") or "").rstrip("/")
  if not ede_outage_server:
    print("[ERROR] [check_outage] ede_outage_server variable is missing from context state!")
    _audit_response = {
        "status": "error",
        "outage_detected": False,
        "error": "Missing required server configuration: 'ede_outage_server'",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [check_outage] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  tool_args = {
      "x-url": f"{ede_outage_server}/resicustomer/api/ede/execute",
      "agent_name": "gecx_repair_agent",
      "x-auth": "CECMT-SAT-XAXLR",
      "x-scope": "ceconvoy:acre:execution",
      "x-cache-refresh": "FORCE-REFRESH",
      "x-flow-trace-id": context.session_id,
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

  try:
    # Record API call performance & raw payloads
    _api_start = time.time()
    response = tools.ede_outage_api_checkOutageEde(tool_args)
    _api_end = time.time()
    _api_latency_ms = int((_api_end - _api_start) * 1000)
    print(f"[AUDIT] [LATENCY] [check_outage] API took: {_api_latency_ms} ms")
    print(f"[AUDIT] [API REQUEST] [check_outage] >>>: {tool_args}")
    if hasattr(response, "status_code"):
      print(
          f"[AUDIT] [HTTP STATUS] [check_outage] : {response.status_code} -"
          f" {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [check_outage] <<<: {response.text}")
    print(f"[check_outage] response type: {type(response)}")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[check_outage] Tool call failed: {e}")
    _audit_response = {
        "status": "error",
        "outage_detected": False,
        "error": f"EDE tool call failed: {str(e)}",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [check_outage] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  # Parse the response
  try:
    data = response
    if hasattr(response, "body"):
      data = response.body
    elif hasattr(response, "text"):
      data = json.loads(response.text)
    elif hasattr(response, "__getitem__"):
      data = response

    if isinstance(data, str):
      data = json.loads(data)

    print(f"[check_outage] Parsed data type: {type(data)}")

    outage_detected = False
    outage_message = ""
    customer_message = ""
    impacted_services = []
    recommendations = []

    if isinstance(data, dict):
      if "result" in data:
        data = data["result"]

      # If data is now a list (result was a list), iterate it
      if isinstance(data, list):
        for item in data:
          if isinstance(item, dict):
            ede_response = item.get("edeResponse", {})
            if isinstance(ede_response, dict):
              recs = ede_response.get("recommendations", [])
              if isinstance(recs, list):
                recommendations.extend(recs)
      elif isinstance(data, dict):
        # Navigate: data[] -> edeResponse.recommendations[]
        data_array = data.get("data", [])
        if isinstance(data_array, list):
          for item in data_array:
            if isinstance(item, dict):
              ede_response = item.get("edeResponse", {})
              if isinstance(ede_response, dict):
                recs = ede_response.get("recommendations", [])
                if isinstance(recs, list):
                  recommendations.extend(recs)

        # Fallback: top-level recommendations
        if not recommendations:
          top_recs = data.get("recommendations", [])
          if isinstance(top_recs, list):
            recommendations = top_recs

    print(f"[check_outage] Found {len(recommendations)} recommendations")

    # Search for ACTIVE_INTERNET_OUTAGE
    for rec in recommendations:
      if not isinstance(rec, dict):
        continue
      rec_name = rec.get("name", "") or rec.get("key", "") or ""
      if "ACTIVE_INTERNET_OUTAGE" in rec_name.upper():
        outage_detected = True
        additional_data = rec.get("additionalData", {})
        if isinstance(additional_data, dict):
          outage_message = additional_data.get("adkOutageDetailsMessage", "")
          customer_message = additional_data.get("adkCustomerMessage", "")
          impacted_services_str = additional_data.get("impactedServices", "")
          if isinstance(impacted_services_str, str) and impacted_services_str:
            impacted_services = [
                s.strip() for s in impacted_services_str.split(",")
            ]
          elif isinstance(impacted_services_str, list):
            impacted_services = impacted_services_str
        break

    # Apply fallbacks
    if outage_detected:
      if not outage_message:
        outage_message = (
            "Your area is currently experiencing a service outage. Our teams are working to restore your services as quickly as possible."
        )
      if not customer_message:
        customer_message = (
            "During an outage, we are unable to connect you with a live agent,"
            " as any troubleshooting would not bring your services back online."
        )

    print(f"[check_outage] outage_detected: {outage_detected}")

    # Set state variables for use by other agents/tools
    try:
      context.state["outage_detected"] = "true" if outage_detected else "false"
      context.state["outage_message"] = outage_message
      context.state["customer_message"] = customer_message
      context.state["impacted_services"] = (
          ",".join(impacted_services) if impacted_services else ""
      )
      context.state["outage_status"] = "active" if outage_detected else "none"
      print("[check_outage] Set context.state variables successfully")
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(f"[check_outage] Could not set state variables: {e}")

    _audit_response = {
        "status": "success",
        "outage_status": "active" if outage_detected else "none",
        "outage_detected": outage_detected,
        "outage_message": outage_message,
        "customer_message": customer_message,
        "impacted_services": impacted_services,
    }
    print(
        "[AUDIT] [check_outage] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[check_outage] Parse error: {e}")
    traceback.print_exc()
    _audit_response = {
        "status": "error",
        "outage_detected": False,
        "error": f"Failed to parse EDE response: {str(e)}",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [check_outage] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
