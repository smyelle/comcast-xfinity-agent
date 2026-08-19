# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""PolySynth Tool function."""

# pylint: disable=undefined-variable

import json
import time
from typing import Any


def post_user_notification(
    tool: str,
    status: str,
    text: str,
) -> dict[str, Any]:
  """Pushes a live diagnostic progress notification to the customer UI.

  Args:
      tool: Diagnostic category ('account', 'outage', 'network', 'device',
        'wifi')
      status: Result status ('started', 'success', 'failure', 'error')
      text: User-friendly description of progress (e.g., 'Your gateway looks
        healthy')

  Returns:
      dict: Status of the push notification request.
  """
  # Validate parameters
  _audit_request = {
      "tool": tool,
      "status": status,
      "text": text,
  }
  print(
      "[AUDIT] [post_user_notification] >>> Request Payload:",
      f" {_audit_request}",
  )
  valid_tools = {"account", "outage", "network", "device"}
  valid_statuses = {"started", "success", "failure", "error"}

  if tool == "gateway":
    tool = "device"

  if tool not in valid_tools:
    _audit_response = {
        "status": "error",
        "error": (
            f"Invalid tool value '{tool}'. Must be one of: {list(valid_tools)}."
        ),
        "agent_action": (
            "Provide a valid diagnostic tool category ('account', 'outage',"
            " 'network', 'device', 'wifi')."
        ),
    }
    print(
        "[AUDIT] [post_user_notification] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  if status not in valid_statuses:
    _audit_response = {
        "status": "error",
        "error": (
            f"Invalid status value '{status}'. Must be one of:"
            f" {list(valid_statuses)}."
        ),
        "agent_action": (
            "Provide a valid status ('started', 'success', 'failure', 'error')."
        ),
    }
    print(
        "[AUDIT] [post_user_notification] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  if not text:
    _audit_response = {
        "status": "error",
        "error": "text field is required.",
        "agent_action": (
            "Provide a non-empty text description for the notification."
        ),
    }
    print(
        "[AUDIT] [post_user_notification] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  # Extract session_id directly from GECX native session context
  session_id = context.session_id or "E-sandbox-placeholder-session-id"

  # Extract account_number from context state
  account_number = (
      context.state.get("accountNumber")
      or context.state.get("account_id")
      or ""
  )

  # Extract dynamic parameters with variable_not_set fallback for production tracking
  convoy_tracking_id = (
      context.state.get("convoy_tracking_id") or "variable_not_set"
  )
  cust_guid = context.state.get("cust_guid") or "variable_not_set"
  channel = context.state.get("channel") or "variable_not_set"
  platform = context.state.get("platform") or "variable_not_set"
  agent_id = context.state.get("agent_id") or "variable_not_set"
  xa_session_id = context.state.get("xa_session_id") or "variable_not_set"

  # Construct dynamic Apigee auth-proxy routing headers (apikey is dynamically injected by GECX)
  x_auth = "SSE-SAT-XAXLR"
  x_scope = (
      "urn:convoy:security:token:client-credentials urn:convoy:session#read"
  )
  post_user_notification_server = str(context.state.get("post_user_notification_server") or "").rstrip("/")
  if not post_user_notification_server:
    print("[ERROR] [post_user_notification] post_user_notification_server variable is missing from context state!")
    _audit_response = {
        "status": "error",
        "error": "Missing required server configuration: 'post_user_notification_server'",
        "agent_action": "Log the error and continue the diagnostic flow.",
    }
    print(
        "[AUDIT] [post_user_notification] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  x_url = f"{post_user_notification_server}/api/sse/{xa_session_id}"

  # Construct the unified OpenAPI arguments dictionary matching Comcast Apigee dynamic proxy requirements
  # (GECX dynamically appends the apikey from Google Secret Manager as configured in the toolset)
  tool_args = {
      "type": "ai-message",
      "fulfillmentAgentId": agent_id,
      "session_id": xa_session_id,
      "sessionId": xa_session_id,
      "x-auth": x_auth,
      "x-scope": x_scope,
      "x-url": x_url,
      "agent_name": "gecx_repair_agent",
      "x-convoy-channel": channel,
      "x-convoy-content-version": "3.0",
      "x-convoy-customer-guid": cust_guid,
      "x-convoy-partner-id": "comcast",
      "x-convoy-platform": platform,
      "x-convoy-session-id": xa_session_id,
      "x-convoy-tracking-id": convoy_tracking_id,
      "response": {
          "jsonrpc": "2.0",
          "taskId": None,
          "updateId": None,
          "sessionId": xa_session_id,
          "fulfillmentAgentId": None,
          "accountId": account_number,
          "cust_guid": cust_guid,
          "platform": platform,
          "channel": channel,
          "result": {
              "status": {"state": "completed", "final": False},
              "message": {
                  "parts": [{
                      "kind": "tool-message",
                      "text": text,
                      "status": status,
                      "tool": tool,
                  }]
              },
          },
      },
  }

  try:
    # Print the fully constructed payload to stdout to capture and verify in SCRAPI logs
    print(
        "[post_user_notification] Dispatched tool_args:"
        f" {json.dumps(tool_args)}"
    )
    # Call the live toolset endpoint through the Apigee dynamic proxy
    # Record API call performance & raw payloads
    _api_start = time.time()
    response = tools.xa_customer_notification_api_postNotification(tool_args)
    _api_end = time.time()
    _api_latency_ms = int((_api_end - _api_start) * 1000)
    print(
        "[AUDIT] [LATENCY] [post_user_notification] API took:"
        f" {_api_latency_ms} ms"
    )
    print(f"[AUDIT] [API REQUEST] [post_user_notification] >>>: {tool_args}")
    if hasattr(response, "status_code"):
      print(
          "[AUDIT] [HTTP STATUS] [post_user_notification] :"
          f" {response.status_code} - {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(
          "[AUDIT] [API RESPONSE] [post_user_notification] <<<:"
          f" {response.text}"
      )

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

    _audit_response = {
        "status": "success",
        "notification_sent": True,
        "backend_response": data,
    }
    print(
        "[AUDIT] [post_user_notification] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  except Exception as e:
    print(f"[post_user_notification] Failed to push notification: {e}")
    _audit_response = {
        "status": "error",
        "error": (
            f"Failed to dispatch notification to Comcast Apigee Proxy: {str(e)}"
        ),
        "agent_action": "Log the error and continue the diagnostic flow.",
    }
    print(
        "[AUDIT] [post_user_notification] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
