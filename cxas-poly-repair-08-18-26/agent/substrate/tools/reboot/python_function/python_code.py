# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function."""

# pylint: disable=undefined-variable

import json
import time
from typing import Any


def reboot(
    device_id: str = "",
    restart: bool = True,
    override_timeline_check: bool = False,
) -> dict[str, Any]:
  """Executes a gateway reboot operation or timeline check to restore connectivity.

  Args:
      device_id: The gateway's MAC address or hardware identifier.
      restart: Set True to issue reboot command, False to only check timeline.
      override_timeline_check: Set True to force reboot, ignoring timeline gate
        checks.

  Returns:
      dict: Execution status of the reboot or timeline check request.
  """
  # Extract cable_modem_mac if device_id is not provided
  if not device_id:
    device_id = context.state.get("cable_modem_mac") or ""

  if not device_id:
    return {
        "status": "error",
        "error": "device_id (cable_modem_mac) is required.",
        "agent_action": (
            "Confirm the gateway device identifier before attempting a reboot."
        ),
    }

  # Extract account number from context
  account_number = (
      context.state.get("accountNumber")
      or context.state.get("account_id")
      or ""
  )
  if not account_number:
    return {
        "status": "error",
        "error": "account_number is required but not found in context.",
        "agent_action": (
            "Fetch customer context to load the account details first."
        ),
    }

  convoy_api_server = str(context.state.get("convoy_api_server") or "").rstrip("/")
  if not convoy_api_server:
    print("[ERROR] [reboot] convoy_api_server variable is missing from context state!")
    _audit_response = {
        "status": "error",
        "error": "Missing required server configuration: 'convoy_api_server'",
        "agent_action": "transfer_to_human",
    }
    print("[AUDIT] [reboot] <<< Response Payload:", f" {_audit_response}")
    return _audit_response

  # --- Timeline Check Phase (restart=false) ---
  # When restart=false, we first check if there was a recent reboot.
  # If no recent restart is found, we automatically proceed to issue the actual reboot
  # in the same call, so the agent doesn't need to make a second tool call.
  if not restart:
    timeline_result = _call_convoy_api(
        device_id=device_id,
        account_number=account_number,
        convoy_api_server=convoy_api_server,
        restart=False,
        override_timeline_check=override_timeline_check,
    )

    if timeline_result.get("status") == "error":
      return timeline_result

    backend_response = timeline_result.get("backend_response", {})
    status_text = ""
    if isinstance(backend_response, dict):
      status_text = str(backend_response.get("status", "")).lower()
    elif isinstance(backend_response, str):
      status_text = backend_response.lower()

    # Check if a recent restart was found (timeline block)
    # IMPORTANT: Check for negative indicators FIRST since "no recent restarts"
    # contains the substring "recent restart" which would false-positive match.
    # Known API responses:
    #   - No recent restart: "No recent restarts on the provided mac address found"
    #   - Recent restart found: "Found Recent Restart"
    #   - Reboot issued: "reboot"
    no_recent_restart_indicators = [
        "no recent restarts",
        "no recent restart",
        "no restarts found",
    ]
    has_no_recent_restart = any(
        indicator in status_text for indicator in no_recent_restart_indicators
    )

    if has_no_recent_restart:
      # No recent restart — automatically proceed with actual reboot
      print("[AUDIT] [reboot] No recent restart found. Auto-proceeding with actual reboot...")
      reboot_result = _call_convoy_api(
          device_id=device_id,
          account_number=account_number,
          convoy_api_server=convoy_api_server,
          restart=True,
          override_timeline_check=False,
      )
      return reboot_result

    # If we get here, check for positive recent restart indicators
    # API returns "Found Recent Restart" when device was recently restarted
    recent_restart_indicators = [
        "found recent restart",
        "restricted",
        "recent restart",
    ]
    has_recent_restart = any(
        indicator in status_text for indicator in recent_restart_indicators
    )

    if has_recent_restart:
      # Recent restart found — return to LLM so it can ask customer for confirmation
      print("[AUDIT] [reboot] Timeline check found recent restart. Returning for confirmation.")
      return {
          "status": "timeline_blocked",
          "reboot_issued": False,
          "message": "Gateway was recently restarted within the last hour. Ask customer if they want to force another restart.",
          "backend_response": backend_response,
      }

    # Default fallback: if status text is unrecognized, proceed with reboot (safe default)
    print("[AUDIT] [reboot] Unrecognized timeline response. Defaulting to proceed with actual reboot...")
    reboot_result = _call_convoy_api(
        device_id=device_id,
        account_number=account_number,
        convoy_api_server=convoy_api_server,
        restart=True,
        override_timeline_check=False,
    )
    return reboot_result

  # --- Direct Reboot Phase (restart=true) ---
  # Called directly when: customer confirms force-reboot, or auto-proceed from above.
  return _call_convoy_api(
      device_id=device_id,
      account_number=account_number,
      convoy_api_server=convoy_api_server,
      restart=True,
      override_timeline_check=override_timeline_check,
  )


def _call_convoy_api(
    device_id: str,
    account_number: str,
    convoy_api_server: str,
    restart: bool,
    override_timeline_check: bool,
) -> dict[str, Any]:
  """Makes the actual Convoy API call for reboot or timeline check.

  Args:
      device_id: Gateway MAC address.
      account_number: Customer account number.
      convoy_api_server: Base URL for Convoy API.
      restart: Whether to issue actual reboot.
      override_timeline_check: Whether to override timeline gating.

  Returns:
      dict with status, reboot_issued flag, and backend response.
  """
  restart_val = "true" if restart else "false"
  override_val = "true" if override_timeline_check else "false"

  _audit_request = {
      "device_id": device_id,
      "restart": restart,
      "override_timeline_check": override_timeline_check,
  }
  print("[AUDIT] [reboot] >>> Request Payload:", f" {_audit_request}")

  target_url = (
      f"{convoy_api_server}/device/{account_number}/timeline-check/restart"
      f"?mac={device_id}&restart={restart_val}&overrideTimelineCheck={override_val}"
  )

  # Prepare tool args for the Apigee proxy gateway_restart_api
  tool_args = {
      "x-auth": "GECXCONVOY-SAT-XAXLR",
      "x-scope": "ceconvoy:device:status",
      "x-url": target_url,
      "x-flow-trace-id": context.session_id,
  }

  try:
    _api_start = time.time()
    response = tools.gateway_restart_api_restartDevice(tool_args)
    _api_end = time.time()
    _api_latency_ms = int((_api_end - _api_start) * 1000)
    print(f"[AUDIT] [LATENCY] [reboot] API took: {_api_latency_ms} ms")
    print(f"[AUDIT] [API REQUEST] [reboot] >>>: {tool_args}")
    if hasattr(response, "status_code"):
      print(
          f"[AUDIT] [HTTP STATUS] [reboot] : {response.status_code} -"
          f" {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [reboot] <<<: {response.text}")

    # In CXAS, OpenAPI tools return the parsed response body or a wrapper
    data = response
    if hasattr(response, "body"):
      data = response.body
    elif hasattr(response, "text") and response.text:
      data = json.loads(response.text)

    _audit_response = {
        "status": "success",
        "reboot_issued": restart,
        "message": (
            "Reboot command successfully sent to the gateway."
            if restart
            else "Timeline check completed."
        ),
        "backend_response": data,
        "success": True,
    }
    print("[AUDIT] [reboot] <<< Response Payload:", f" {_audit_response}")
    return _audit_response

  except Exception as e:  # pylint: disable=broad-exception-caught
    _audit_response = {
        "status": "error",
        "reboot_issued": False,
        "error": f"Failed to {'issue reboot command' if restart else 'check timeline'}: {str(e)}",
        "agent_action": (
            "Inform the customer that the remote reboot command failed and"
            " guide them to manually unplug their gateway for 30 seconds."
        ),
    }
    print("[AUDIT] [reboot] <<< Response Payload:", f" {_audit_response}")
    return _audit_response


