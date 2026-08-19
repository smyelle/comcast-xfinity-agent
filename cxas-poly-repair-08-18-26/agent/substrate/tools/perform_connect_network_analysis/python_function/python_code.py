# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""PolySynth Tool function."""

# pylint: disable=undefined-variable

import json
from typing import Any


def perform_connect_network_analysis(cable_modem_mac: str) -> dict[str, Any]:
  """Invokes the connect_agent_rest_call toolset.

  It runs the sendA2AMessageViaAuthProxy operation as a CXAS tool,
  passing cable_modem_mac as the sole input.

  Uses tool_context.call_tool() so the platform handles auth, URL, and all
  OpenAPI spec defaults -- no direct HTTP calls are made here.

  Args:
      cable_modem_mac: The modem MAC address to pass as the cable_modem_mac
        query parameter (e.g. "xx:xx:xx:xx:xx:xx").

  Returns:
      dict: The JSON-RPC response from the upstream A2A agent.
  """
  if not cable_modem_mac:
    return {
        "status": "error",
        "error": "cable_modem_mac is required.",
        "agent_action": "Ask the customer for their cable modem MAC address.",
    }
  # set the variable from the context.state
  # cable_modem_mac = context.state.get("cable_modem_mac", "")
  # debug info
  _audit_request = {
      "cable_modem_mac": cable_modem_mac,
  }
  print(
      "[AUDIT] [perform_connect_network_analysis] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"cable_modem_mac: {cable_modem_mac}")
  # prepare the request parameters
  xme_server = str(context.state.get("xme_server") or "").rstrip("/")
  if not xme_server:
    print("[ERROR] [perform_connect_network_analysis] xme_server variable is missing from context state!")
    _audit_response = {
        "status": "error",
        "error": "Missing required server configuration: 'xme_server'",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [perform_connect_network_analysis] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  tool_args = {
      "cable_modem_mac": cable_modem_mac,
      "contextId": context.state.get("xa_session_id") or "",
      "x-auth": "XME-SAT-XAXLR",
      "x-scope": "xme:access xme:ai-agent:access",
      "x-url": f"{xme_server}/agent/a2a/jsonrpc",
      "x-flow-trace-id": context.session_id,
      "id": "1",
      "jsonrpc": "2.0",
      "method": "message/send",
      "params": {
          "message": {
              "kind": "message",
              "messageId": "msg-1",
              "role": "user",
              "parts": [{
                  "kind": "text",
                  "text": (
                      f"""A customer is contacting support regarding a potential issue with their internet service. Investigate the following devices and identify the root cause, if any, and the appropriate dispatch decision.
 Devices in the customer's home: {cable_modem_mac} .

 Respond ONLY with valid JSON — no extra fields.
 Use EXACTLY this structure:
 {{
 Analysis: <high-level summary: scope, impairment pattern, and root cause only — no recommendation>,
 Severity: <Critical | High | Medium | Low>,
 Recommendation: {{
 Technician Type: <Network Tech | Install and Repair Tech | No Technician Required>,
 Remediation: <brief action to resolve the issue>
 }}
}}

"""
                  ),
              }],
          }
      },
  }
  # make a call to the tool
  try:
    response = tools.connect_agent_rest_call_sendA2AMessageViaAuthProxy(
        tool_args
    )
    # print response for debugging
    print(
        "tools.connect_agent_rest_call_sendA2AMessageViaAuthProxy response:"
        f" {response} "
    )
  except Exception as e:
    print(f"[perform_connect_network_analysis] Tool call failed: {e}")
    _audit_response = {
        "status": "error",
        "error": f"A2A tool call failed: {str(e)}",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [perform_connect_network_analysis] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  # Parse response
  data = response
  if hasattr(response, "json") and callable(response.json):
    try:
      data = response.json()
    except Exception:
      pass
  elif hasattr(response, "body"):
    data = response.body
  elif hasattr(response, "text"):
    try:
      data = json.loads(response.text)
    except Exception:
      pass

  if isinstance(data, str):
    try:
      data = json.loads(data)
    except Exception:
      pass

  # Extract the text message from JSON-RPC result
  a2a_text = ""
  if isinstance(data, dict):
    # Try top-level message
    msg = data.get("message", {})
    if isinstance(msg, dict):
      parts = msg.get("parts", [])
      if parts and isinstance(parts, list) and isinstance(parts[0], dict):
        a2a_text = parts[0].get("text", "")

    # If not found, try nested result.message
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

  # Try parsing the inner message text as JSON
  inner_data = {}
  if a2a_text:
    try:
      cleaned_text = a2a_text.strip()
      if cleaned_text.startswith("```json"):
        cleaned_text = cleaned_text[7:]
      if cleaned_text.endswith("```"):
        cleaned_text = cleaned_text[:-3]
      cleaned_text = cleaned_text.strip()
      inner_data = json.loads(cleaned_text)
    except Exception as e:
      print(
          "[perform_connect_network_analysis] Failed to parse inner A2A"
          f" text: {e}"
      )

  if not inner_data and isinstance(data, dict):
    if "Severity" in data or "Recommendation" in data:
      inner_data = data

  # Determine network status based on response
  network_status = "healthy"
  if isinstance(inner_data, dict):
    rec = inner_data.get("Recommendation", {})
    tech_type = ""
    if isinstance(rec, dict):
      tech_type = str(rec.get("Technician Type", "")).strip()

    if tech_type.lower() in ("network tech", "install and repair tech"):
      network_status = "impaired"

  print(f"[perform_connect_network_analysis] network_status: {network_status}")

  # Deterministically set activityType, activityCode, jobType based on technician type
  matched_activity_type = ""
  matched_activity_code = ""
  matched_job_type = ""

  if isinstance(inner_data, dict):
    rec = inner_data.get("Recommendation", {})
    if isinstance(rec, dict):
      tech = str(rec.get("Technician Type", "")).strip().lower()
      if tech == "network tech":
        matched_activity_type = "SPECIAL_REQUEST"
        matched_activity_code = "PR"
        matched_job_type = "PR"
      elif tech == "install and repair tech":
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
    context.state["network_status"] = network_status
    context.state["activityCode"] = matched_activity_code
    context.state["jobType"] = matched_job_type
    context.state["activityType"] = matched_activity_type
    print(
        "[perform_connect_network_analysis] Propagated dynamic transfer values to context.state:"
        f" activityCode='{matched_activity_code}',"
        f" jobType='{matched_job_type}',"
        f" activityType='{matched_activity_type}'"
    )
  except Exception as e:
    print(f"[perform_connect_network_analysis] Failed to set state: {e}")

  _audit_response = {
      "status": "success",
      "network_status": network_status,
      "analysis_response": inner_data,
  }
  print(
      "[AUDIT] [perform_connect_network_analysis] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response
