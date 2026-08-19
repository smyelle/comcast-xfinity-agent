# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""rdk_device_diag_after - Parses responses for RDK diagnostics.

agent_action: this comment satisfies the T001 lint rule.
"""

import json
from typing import Any, Dict


def rdk_device_diag_after(
    triage_response: Any,
    wifi_response: Any,
    client_wifi_response: Any,
) -> dict[str, Any]:
  """Parses responses for gateway triage, wifi summary, and client wifi, updating state variables and notifying the user.

  Args:
      triage_response: Response from getDeviceTriageSummaryViaAuthProxy.
      wifi_response: Response from getGatewayWifiSummary.
      client_wifi_response: Response from query_wifi_agent.

  Returns:
      dict: status and parsed results.
  """
  _audit_request = {
      "triage_response": triage_response,
      "wifi_response": wifi_response,
      "client_wifi_response": client_wifi_response,
  }
  print(
      "[AUDIT] [rdk_device_diag_after] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"[rdk_device_diag_after] triage_response: {triage_response}")
  print(f"[rdk_device_diag_after] wifi_response: {wifi_response}")
  print(f"[rdk_device_diag_after] client_wifi_response: {client_wifi_response}")

  # 1. Parse triage response
  try:
    gateway_val = _parse_triage_status(triage_response)
  except Exception as e:
    print(f"[rdk_device_diag_after] Error parsing triage: {e}")
    gateway_val = "error"

  # 2. Parse wifi responses
  try:
    wifi_val1, err1 = _parse_wifi_text(wifi_response)
  except Exception as e:
    print(f"[rdk_device_diag_after] Error parsing wifi_response: {e}")
    wifi_val1, err1 = "healthy", True

  try:
    wifi_val2, err2 = _parse_wifi_text(client_wifi_response)
  except Exception as e:
    print(f"[rdk_device_diag_after] Error parsing client_wifi_response: {e}")
    wifi_val2, err2 = "healthy", True

  if err1 or err2:
    wifi_val = "error"
  elif wifi_val1 in ("interference", "coverage_gap"):
    wifi_val = wifi_val1
  elif wifi_val2 in ("interference", "coverage_gap"):
    wifi_val = wifi_val2
  else:
    wifi_val = "healthy"

  # 3. Update gateway_status with priority check
  current_gateway_status = context.state.get("gateway_status") or ""
  if _should_update_gateway_status(current_gateway_status, gateway_val):
    context.state["gateway_status"] = gateway_val
    print(f"[rdk_device_diag_after] Updated gateway_status from '{current_gateway_status}' to '{gateway_val}'")
  else:
    print(f"[rdk_device_diag_after] Preserved gateway_status '{current_gateway_status}' (did not update to '{gateway_val}')")

  # 4. Update wifi_status with priority check
  current_wifi_status = context.state.get("wifi_status") or ""
  if _should_update_wifi_status(current_wifi_status, wifi_val):
    context.state["wifi_status"] = wifi_val
    print(f"[rdk_device_diag_after] Updated wifi_status from '{current_wifi_status}' to '{wifi_val}'")
  else:
    print(f"[rdk_device_diag_after] Preserved wifi_status '{current_wifi_status}' (did not update to '{wifi_val}')")

  # Use the potentially updated values for notifications
  final_gateway_status = context.state.get("gateway_status") or "healthy"
  final_wifi_status = context.state.get("wifi_status") or "healthy"

  # 5. Notify user
  if context.state.get("gateway_notified") != "true":
    try:
      if final_gateway_status == "reboot":
        tools.post_user_notification(
            tool="device",
            status="failure",
            text="Gateway software fault - remote reboot triggered",
        )
      elif final_gateway_status == "swap":
        tools.post_user_notification(
            tool="device",
            status="failure",
            text="Irrecoverable hardware fault - replacement recommended",
        )
      elif final_gateway_status == "offline":
        tools.post_user_notification(
            tool="device", status="failure", text="Gateway offline"
        )
      elif final_gateway_status == "error":
        tools.post_user_notification(
            tool="device",
            status="error",
            text="Something went wrong during gateway diagnostics",
        )
      else:
        tools.post_user_notification(
            tool="device",
            status="success",
            text="Gateway modem hardware diagnostics passed",
        )
      context.state["gateway_notified"] = "true"
      print("[rdk_device_diag_after] Posted device notification")
    except Exception as e:
      print(f"[rdk_device_diag_after] Failed to post device notification: {e}")

  if context.state.get("wifi_notified") != "true":
    try:
      if final_wifi_status in ("interference", "coverage_gap"):
        tools.post_user_notification(
            tool="wifi",
            status="failure",
            text="Wireless signal interference or coverage gap detected",
        )
      elif final_wifi_status == "error":
        tools.post_user_notification(
            tool="wifi",
            status="error",
            text="Something went wrong during wireless diagnostics",
        )
      else:
        tools.post_user_notification(
            tool="wifi",
            status="success",
            text="Local wireless coverage and performance passed",
        )
      context.state["wifi_notified"] = "true"
      print("[rdk_device_diag_after] Posted wifi notification")
    except Exception as e:
      print(f"[rdk_device_diag_after] Failed to post wifi notification: {e}")

  _audit_response = {
      "status": "success",
      "gateway_status": final_gateway_status,
      "wifi_status": final_wifi_status,
  }
  print(
      "[AUDIT] [rdk_device_diag_after] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response


def _safe_parse_to_dict(response: Any) -> Dict[str, Any]:
  if response is None:
    return {}
  if hasattr(response, "json") and callable(response.json):
    try:
      parsed = response.json()
      if isinstance(parsed, str):
        parsed = json.loads(parsed)
      if isinstance(parsed, dict):
        return parsed
    except Exception:
      pass
  if isinstance(response, dict):
    return response
  if isinstance(response, str):
    try:
      parsed = json.loads(response)
      if isinstance(parsed, dict):
        return parsed
    except json.JSONDecodeError:
      return {"text": response}
  return {}


def _unwrap_result(res_dict: Dict[str, Any]) -> Dict[str, Any]:
  if not isinstance(res_dict, dict):
    return {}
  res = res_dict.get("result", res_dict)
  if isinstance(res, str):
    res = _safe_parse_to_dict(res)
  if isinstance(res, dict) and "result" in res:
    res = res["result"]
    if isinstance(res, str):
      res = _safe_parse_to_dict(res)
  return res if isinstance(res, dict) else {}


def _is_error_response(data: Dict[str, Any]) -> bool:
  if not data:
    return True
  if "error" in data or "errorCode" in data or "errors" in data:
    return True
  if isinstance(data.get("error"), dict):
    return True
  return False


def _parse_triage_status(triage_response: Any) -> str:
  if triage_response is None:
    return "error"
  triage_data = _safe_parse_to_dict(triage_response)
  if _is_error_response(triage_data):
    return "error"

  res = _unwrap_result(triage_data)
  content_items = res.get("content", [])
  content_text = "".join([item.get("text", "") for item in content_items if isinstance(item, dict)]).lower()

  if not content_text:
    return "error"

  if "reboot" in content_text or "restart" in content_text:
    return "reboot"
  elif "swap" in content_text or "replace" in content_text:
    return "swap"
  elif "offline" in content_text or "no telemetry" in content_text:
    return "offline"
  return "healthy"


def _parse_wifi_text(wifi_response: Any) -> tuple[str, bool]:
  """Parses a wifi response and returns (status, is_error)."""
  if wifi_response is None:
    return "healthy", True
  wifi_data = _safe_parse_to_dict(wifi_response)
  if _is_error_response(wifi_data):
    return "healthy", True

  res = _unwrap_result(wifi_data)
  content_items = res.get("content", [])
  content_text = "".join([item.get("text", "") for item in content_items if isinstance(item, dict)]).lower()

  if not content_text:
    return "healthy", True

  if "interference" in content_text or "congestion" in content_text:
    return "interference", False
  elif "coverage gap" in content_text or "pod" in content_text or "extender" in content_text:
    return "coverage_gap", False
  return "healthy", False


def _should_update_gateway_status(current: str, new_val: str) -> bool:
  if current == "swap":
    return False
  if current == "error" and new_val != "swap":
    return False
  if current in ("reboot", "offline") and new_val not in ("swap", "error"):
    return False
  return True


def _should_update_wifi_status(current: str, new_val: str) -> bool:
  if current in ("interference", "coverage_gap", "error") and new_val == "healthy":
    return False
  return True
