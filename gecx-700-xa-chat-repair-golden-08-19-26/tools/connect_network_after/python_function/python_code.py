# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

import json
import re
import traceback
from typing import Any


def connect_network_after(response: Any) -> dict[str, Any]:
  """Parses Network analysis response and updates state.

  Args:
      response: Raw response from sendA2AMessageViaAuthProxy.

  Returns:
      dict with status and network details.
  """
  _audit_request = {
      "response": response,
  }
  print(
      "[AUDIT] [connect_network_after] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"[connect_network_after] received response: {response}")

  if response is None:
    _audit_response = handle_error("Response is None")
    print(
        "[AUDIT] [connect_network_after] <<< Response Payload:",
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
            "[AUDIT] [connect_network_after] <<< Response Payload:",
            f" {_audit_response}",
        )
        return _audit_response

    print(f"[connect_network_after] Parsed data: {data}")

    if not isinstance(data, dict):
      _audit_response = handle_error(f"Expected dict response, got {type(data)}")
      print(
          "[AUDIT] [connect_network_after] <<< Response Payload:",
          f" {_audit_response}",
      )
      return _audit_response

    # Check for API error response
    if "error" in data or "errorCode" in data or "errors" in data:
      _audit_response = handle_error(f"API returned error: {data}")
      print(
          "[AUDIT] [connect_network_after] <<< Response Payload:",
          f" {_audit_response}",
      )
      return _audit_response

    # Extract the text message from JSON-RPC result
    a2a_text = ""
    msg = data.get("message", {})
    if isinstance(msg, dict):
      parts = msg.get("parts", [])
      if parts and isinstance(parts, list) and isinstance(parts[0], dict):
        a2a_text = parts[0].get("text", "")

    if not a2a_text:
      result = data.get("result", {})
      if isinstance(result, dict):
        msg = result.get("message", {})
        if isinstance(msg, dict):
          parts = msg.get("parts", [])
          if parts and isinstance(parts, list) and isinstance(parts[0], dict):
            a2a_text = parts[0].get("text", "")

        if not a2a_text:
          parts = result.get("parts", [])
          if parts and isinstance(parts, list) and isinstance(parts[0], dict):
            a2a_text = parts[0].get("text", "")

    # Try parsing the inner message text as JSON robustly
    inner_data = {}
    if a2a_text:
      try:
        json_match = re.search(r"```json\s*(\{.*?\})\s*```", a2a_text, re.DOTALL)
        if json_match:
          cleaned_text = json_match.group(1).strip()
        else:
          braces_match = re.search(r"(\{.*?\})", a2a_text, re.DOTALL)
          if braces_match:
            cleaned_text = braces_match.group(1).strip()
          else:
            cleaned_text = a2a_text.strip()
        inner_data = json.loads(cleaned_text)
      except Exception as e:  # pylint: disable=broad-exception-caught
        print(
            "[connect_network_after] Failed to parse inner A2A"
            f" text: {e}"
        )

    if not inner_data and isinstance(data, dict):
      if "Severity" in data or "Recommendation" in data:
        inner_data = data

    if isinstance(inner_data, dict) and inner_data.get("network_status") == "error":
      remediation = inner_data.get("recommendation", {}).get("remediation") or "A2A network diagnostics failed."
      _audit_response = handle_error(remediation)
      print(
          "[AUDIT] [connect_network_after] <<< Response Payload:",
          f" {_audit_response}",
      )
      return _audit_response

    if not inner_data:
      _audit_response = handle_error(
          "Failed to extract network analysis data from response"
      )
      print(
          "[AUDIT] [connect_network_after] <<< Response Payload:",
          f" {_audit_response}",
      )
      return _audit_response

    # Determine network status based on response
    parsed_status = "healthy"
    rec = inner_data.get("Recommendation", {})
    tech_type = ""
    if isinstance(rec, dict):
      tech_type = str(rec.get("Technician Type", "")).strip()

    if tech_type.lower() in ("network tech", "install and repair tech"):
      parsed_status = "impaired"

    print(
        "[connect_network_after] Parsed network status:"
        f" {parsed_status}"
    )

    # Deterministically set activityType, activityCode, jobType based on technician type
    matched_activity_type = ""
    matched_activity_code = ""
    matched_job_type = ""

    if tech_type.lower() == "network tech":
      matched_activity_type = "SPECIAL_REQUEST"
      matched_activity_code = "PR"
      matched_job_type = "PR"
    elif tech_type.lower() == "install and repair tech":
      matched_activity_type = "TROUBLE_CALL"
      matched_activity_code = "H3"
      matched_job_type = "AO"

    # Apply defaults if not matched (same pattern as check_convoy_recommendations)
    if not matched_activity_code:
      matched_activity_code = "H2"
    if not matched_job_type:
      matched_job_type = "Test"
    if not matched_activity_type:
      matched_activity_type = "TROUBLE_CALL"

    try:
      context.state["activityCode"] = matched_activity_code
      context.state["jobType"] = matched_job_type
      context.state["activityType"] = matched_activity_type
      print(
          "[connect_network_after] Propagated dynamic transfer values to context.state:"
          f" activityCode='{matched_activity_code}',"
          f" jobType='{matched_job_type}',"
          f" activityType='{matched_activity_type}'"
      )
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(f"[connect_network_after] Could not set transfer state variables: {e}")

    # Set state variables
    try:
      current_status = context.state.get("network_status", "")

      if parsed_status == "healthy" and current_status == "impaired":
        print(
            "[connect_network_after] Network status is already"
            " 'impaired'. Not overwriting with 'healthy'."
        )
      else:
        context.state["network_status"] = parsed_status

      context.state["network_notified"] = "true"
      print(
          "[connect_network_after] Set context.state variables"
          " successfully"
      )
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(
          f"[connect_network_after] Could not set state"
          f" variables: {e}"
      )

    # Post notification
    try:
      resolved_status = context.state.get("network_status", parsed_status)
      if resolved_status == "impaired":
        tools.post_user_notification(
            tool="network",
            status="failure",
            text="Network line signals are impaired.",
        )
      else:
        tools.post_user_notification(
            tool="network",
            status="success",
            text="Network line signals are healthy.",
        )
      print("[connect_network_after] Posted user notification")
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(
          f"[connect_network_after] Could not post user"
          f" notification: {e}"
      )

    _audit_response = {
        "status": "success",
        "network_status": context.state.get("network_status", parsed_status),
        "analysis_response": inner_data,
    }
    print(
        "[AUDIT] [connect_network_after] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  except Exception as e:  # pylint: disable=broad-exception-caught
    traceback.print_exc()
    _audit_response = handle_error(f"Failed to parse Network response: {str(e)}")
    print(
        "[AUDIT] [connect_network_after] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response


def handle_error(error_msg: str) -> dict[str, Any]:
  print(f"[connect_network_after] Error: {error_msg}")
  try:
    context.state["network_status"] = "error"
    context.state["network_notified"] = "true"
    print("[connect_network_after] Set network_status to error")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(
        f"[connect_network_after] Could not set state"
        f" variables: {e}"
    )

  try:
    tools.post_user_notification(
        tool="network",
        status="error",
        text="Something went wrong while checking network signals.",
    )
    print(
        "[connect_network_after] Posted user error notification"
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(
        f"[connect_network_after] Could not post user"
        f" notification: {e}"
    )

  return {
      "status": "error",
      "error": error_msg,
  }
