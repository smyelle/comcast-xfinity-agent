# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

from typing import Any

def connect_network_before(
    cable_modem_mac: str,
) -> dict[str, Any]:
  """Prepares arguments for Network analysis and sets initial state.

  Args:
      cable_modem_mac: The customer cable modem MAC address.

  Returns:
      dict with network_analysis_args.
  """
  # Always read MAC from shared state to prevent LLM hallucination of empty values
  cable_modem_mac = context.state.get("cable_modem_mac") or cable_modem_mac
  if not cable_modem_mac:
    return {
        "status": "error",
        "error": "cable_modem_mac is required.",
    }
  # XME A2A agent requires lowercase MAC format
  cable_modem_mac = cable_modem_mac.lower()
  _audit_request = {
      "cable_modem_mac": cable_modem_mac,
  }
  print(
      "[AUDIT] [connect_network_before] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"[connect_network_before] MAC: {cable_modem_mac}")

  try:
    context.state["network_status"] = "PENDING_BACKEND_RESULT"
    print(
        "[connect_network_before] Set network_status to PENDING_BACKEND_RESULT"
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[connect_network_before] Could not set state variable: {e}")

  # The parent-level before_tool_callback dispatches the in-progress checklist visuals now

  xme_server = str(context.state.get("xme_server") or "").rstrip("/")
  if not xme_server:
    print("[ERROR] [connect_network_before] xme_server variable is missing from context state!")
    _audit_response = {
        "status": "error",
        "error": "Missing required server configuration: 'xme_server'",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [connect_network_before] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  tool_args = {
      "cable_modem_mac": cable_modem_mac,
      "contextId": context.state.get("xa_session_id") or "",
      "x-auth": "XME-SAT-XAXLR",
      "x-scope": "xme:access xme:ai-agent:access",
      "x-url": f"{xme_server}/agent/a2a/jsonrpc",
      "agent_name": "gecx_repair_agent",
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
                      "A customer is contacting support regarding a potential"
                      " issue with their internet service. Investigate the"
                      " following devices and identify the root cause, if any,"
                      " and the appropriate dispatch decision.\n Devices in"
                      f" the customer's home: {cable_modem_mac} .\n\n Respond"
                      " ONLY with valid JSON — no extra fields.\n Use EXACTLY"
                      " this structure:\n {\n Analysis: <high-level summary:"
                      " scope, impairment pattern, and root cause only — no"
                      " recommendation>,\n Severity: <Critical | High | Medium"
                      " | Low>,\n Recommendation: {\n Technician Type:"
                      " <Network Tech | Install and Repair Tech | No"
                      " Technician Required>,\n Remediation: <brief action to"
                      " resolve the issue>\n }\n}\n\n"
                  ),
              }],
          }
      },
  }

  # Store directly in state so the LLM can use {network_analysis_args}
  # (bypasses ExternalResponse wrapping issue when called from callbacks)
  try:
    context.state["network_analysis_args"] = tool_args
    print("[connect_network_before] Stored network_analysis_args in state")
  except Exception as e:
    print(f"[connect_network_before] Could not set network_analysis_args in state: {e}")

  _audit_response = {
      "status": "success",
      "network_analysis_args": tool_args,
  }
  print(
      "[AUDIT] [connect_network_before] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response