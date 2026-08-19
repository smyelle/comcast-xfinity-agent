# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

import json
import traceback
from typing import Any


def check_outage_after(response: Any) -> dict[str, Any]:
  """Parses EDE outage response and updates state.

  Args:
      response: Raw response from checkOutageEde.

  Returns:
      dict with status and outage details.
  """
  _audit_request = {
      "response": response,
  }
  print(
      "[AUDIT] [check_outage_after] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"[check_outage_after] received response: {response}")

  if response is None:
    _audit_response = handle_error("Response is None")
    print(
        "[AUDIT] [check_outage_after] <<< Response Payload:",
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
      try:
        data = json.loads(data)
      except json.JSONDecodeError:
        _audit_response = handle_error(f"Failed to decode string response: {data}")
        print(
            "[AUDIT] [check_outage_after] <<< Response Payload:",
            f" {_audit_response}",
        )
        return _audit_response

    print(f"[check_outage_after] Parsed data: {data}")

    if not isinstance(data, dict):
      _audit_response = handle_error(f"Expected dict response, got {type(data)}")
      print(
          "[AUDIT] [check_outage_after] <<< Response Payload:",
          f" {_audit_response}",
      )
      return _audit_response

    # Check for API error response
    if "error" in data or "errorCode" in data or "errors" in data:
      _audit_response = handle_error(f"API returned error: {data}")
      print(
          "[AUDIT] [check_outage_after] <<< Response Payload:",
          f" {_audit_response}",
      )
      return _audit_response

    # Extract recommendations
    recommendations = []
    result = data.get("result")
    if result is not None:
      if isinstance(result, list):
        for item in result:
          if isinstance(item, dict):
            ede_response = item.get("edeResponse", {})
            if isinstance(ede_response, dict):
              recs = ede_response.get("recommendations", [])
              if isinstance(recs, list):
                recommendations.extend(recs)
      elif isinstance(result, dict):
        ede_response = result.get("edeResponse", {})
        if isinstance(ede_response, dict):
          recs = ede_response.get("recommendations", [])
          if isinstance(recs, list):
            recommendations.extend(recs)
    else:
      # Try alternative paths
      data_array = data.get("data", [])
      if isinstance(data_array, list):
        for item in data_array:
          if isinstance(item, dict):
            ede_response = item.get("edeResponse", {})
            if isinstance(ede_response, dict):
              recs = ede_response.get("recommendations", [])
              if isinstance(recs, list):
                recommendations.extend(recs)

      if not recommendations:
        top_recs = data.get("recommendations", [])
        if isinstance(top_recs, list):
          recommendations = top_recs

    print(f"[check_outage_after] Found {len(recommendations)} recommendations")

    outage_detected = False
    outage_message = ""
    customer_message = ""
    impacted_services = []

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
            "Your area is currently experiencing a service outage. Our teams"
            " are working to restore your services."
        )
      if not customer_message:
        customer_message = (
            "During an outage, we are unable to connect you with a live agent,"
            " as any troubleshooting would not bring your services back online."
        )

    print(f"[check_outage_after] outage_detected: {outage_detected}")

    # Set state variables
    try:
      context.state["outage_detected"] = "true" if outage_detected else "false"
      context.state["outage_message"] = outage_message
      context.state["customer_message"] = customer_message
      context.state["impacted_services"] = (
          ",".join(impacted_services) if impacted_services else ""
      )
      context.state["outage_status"] = "active" if outage_detected else "none"
      context.state["outage_notified"] = "true"
      print("[check_outage_after] Set context.state variables successfully")
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(f"[check_outage_after] Could not set state variables: {e}")

    # Post notification
    try:
      if outage_detected:
        tools.post_user_notification(
            tool="outage",
            status="failure",
            text="Active service outage detected in neighborhood.",
        )
      else:
        tools.post_user_notification(
            tool="outage",
            status="success",
            text="No local connection outages found.",
        )
      print("[check_outage_after] Posted user notification")
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(f"[check_outage_after] Could not post user notification: {e}")

    _audit_response = {
        "status": "success",
        "outage_detected": outage_detected,
        "outage_message": outage_message,
        "customer_message": customer_message,
        "impacted_services": impacted_services,
    }
    print(
        "[AUDIT] [check_outage_after] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  except Exception as e:  # pylint: disable=broad-exception-caught
    traceback.print_exc()
    _audit_response = handle_error(f"Failed to parse EDE response: {str(e)}")
    print(
        "[AUDIT] [check_outage_after] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response


def handle_error(error_msg: str) -> dict[str, Any]:
  print(f"[check_outage_after] Error: {error_msg}")
  try:
    context.state["outage_status"] = "error"
    context.state["outage_notified"] = "true"
    # Set default values for other variables to avoid undefined issues
    context.state["outage_detected"] = "false"
    context.state["outage_message"] = ""
    context.state["customer_message"] = ""
    context.state["impacted_services"] = ""
    print("[check_outage_after] Set outage_status to error")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[check_outage_after] Could not set state variables: {e}")

  try:
    tools.post_user_notification(
        tool="outage",
        status="error",
        text="Something went wrong while checking for outages.",
    )
    print("[check_outage_after] Posted user error notification")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[check_outage_after] Could not post user notification: {e}")

  return {
      "status": "error",
      "error": error_msg,
  }
