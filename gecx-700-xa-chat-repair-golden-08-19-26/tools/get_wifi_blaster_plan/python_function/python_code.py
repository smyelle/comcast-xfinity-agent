# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""get_wifi_blaster_plan -- wrapper for the WiFi blaster plan OpenAPI call.

Gets a plan ID from the xFi Speed Platform blaster endpoint through the Apigee
auth-proxy and stores it in session state for the follow-up operation that starts
a device-specific WiFi blaster test.
"""

import json
import time
from typing import Any
from urllib.parse import urlencode

_BLASTER_SCOPE = (
    "x1:xfi-speed-platform:gateway,"
    "x1:xfi-speed-platform:blaster:poll,"
    "x1:xfi-speed-platform:blaster,"
    "x1:xfi-coverage-platform:read"
)


def get_wifi_blaster_plan(
    xbo_id: str = "",
    gateway_mac: str = "",
    device_default_nickname: str = "",
) -> dict[str, Any]:
  """Gets and stores the WiFi blaster plan ID.

  Args:
      xbo_id: Customer XBO ID. Optional -- defaults to session context/state
        xbo_id.
      gateway_mac: Gateway MAC address. Optional -- defaults to session
        cable_modem_mac.
      device_default_nickname: Optional customer-selected device nickname from
        gateway_device_connection_details. When provided, the wrapper resolves
        it to the stored MAC address and requests a targeted blaster plan.

  Returns:
      dict: Small status result. The plan ID is stored in context.state.
  """
  _mark_wifi_troubleshooting_owner()
  xbo_id = _resolve_xbo_id(xbo_id)
  if not gateway_mac:
    gateway_mac = context.state.get("cable_modem_mac") or ""

  if not xbo_id:
    return {
        "status": "error",
        "error": "xbo_id is required to get a WiFi blaster plan.",
        "agent_action": (
            "Continue with safe WiFi troubleshooting or connect the customer"
            " with someone who can help."
        ),
    }

  if not gateway_mac or gateway_mac == "NOT_FOUND":
    return {
        "status": "error",
        "error": "gateway_mac (cable_modem_mac) is required to get a WiFi blaster plan.",
        "agent_action": (
            "Continue with safe WiFi troubleshooting or connect the customer"
            " with someone who can help."
        ),
    }

  coverage_platform_server = str(
      context.state.get("coverage_platform_server")
      or "https://gw.api.dh.comcast.com"
  ).rstrip("/")
  target_url = f"{coverage_platform_server}/speed-test/blaster/api/blast/{gateway_mac}"
  target_device = {}
  if device_default_nickname:
    resolved_device = _resolve_target_device_by_nickname(device_default_nickname)
    if resolved_device.get("status") != "success":
      return resolved_device
    target_device = resolved_device["device"]
    target_url = f"{target_url}?{urlencode({'device_id': target_device['macAddress']})}"

  tool_args = {
      "accept": "application/json",
      "content-type": "application/json",
      "account-id": xbo_id,
      "agent_name": "gecx_repair_agent",
      "session-id": context.session_id,
      "x-auth": "ST-SAT-XAXLR",
      "x-scope": _BLASTER_SCOPE,
      "x-url": target_url,
      "x-flow-trace-id": context.session_id,
  }

  _audit_request = {
      "xbo_id": xbo_id,
      "gateway_mac": gateway_mac,
      "targeted_device": _audit_target_device(target_device),
      "x-url": target_url,
  }
  print("[AUDIT] [get_wifi_blaster_plan] >>> Request Payload:", f" {_audit_request}")

  try:
    _api_start = time.time()
    response = tools.xfi_blaster_plan_auth_proxy_getWifiBlasterPlan(tool_args)
    _api_latency_ms = int((time.time() - _api_start) * 1000)
    print(f"[AUDIT] [LATENCY] [get_wifi_blaster_plan] API took: {_api_latency_ms} ms")
    if hasattr(response, "status_code"):
      print(
          "[AUDIT] [HTTP STATUS] [get_wifi_blaster_plan] :"
          f" {response.status_code} - {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [get_wifi_blaster_plan] <<<: {response.text}")

    data = _coerce_to_dict(response)
    if not isinstance(data, dict) or not data:
      return {
          "status": "error",
          "error": "WiFi blaster plan API returned no usable result.",
          "agent_action": (
              "Continue with safe WiFi troubleshooting or connect the customer"
              " with someone who can help."
          ),
      }

    plan_id = _find_plan_id(data)
    if not plan_id:
      _audit_response = {
          "status": "error",
          "error": "WiFi blaster plan response did not include a planId.",
          "agent_action": (
              "Continue with safe WiFi troubleshooting or connect the customer"
              " with someone who can help."
          ),
      }
      print("[AUDIT] [get_wifi_blaster_plan] <<< Response Payload:", f" {_audit_response}")
      return _audit_response

    context.state["wifi_blaster_plan_id"] = plan_id
    context.state["wifi_blaster_target_device"] = _state_target_device(target_device)
    _audit_response = {
        "status": "success",
        "plan_id_available": True,
        "targeted_device_selected": bool(target_device),
        "target_device_label": _safe_string(target_device.get("defaultNickname")),
        "target_device_type": _safe_string(target_device.get("deviceType")),
        "agent_action": (
            "Use the stored wifi_blaster_plan_id for the follow-up WiFi blaster"
            " start-test tool when it is available."
        ),
    }
    print("[AUDIT] [get_wifi_blaster_plan] stored wifi_blaster_plan_id:", f" {plan_id}")
    print("[AUDIT] [get_wifi_blaster_plan] <<< Response Payload:", f" {_audit_response}")
    return _audit_response

  except Exception as e:
    _audit_response = {
        "status": "error",
        "error": f"Failed to get WiFi blaster plan: {str(e)}",
        "agent_action": (
            "Continue with safe WiFi troubleshooting or connect the customer"
            " with someone who can help."
        ),
    }
    print("[AUDIT] [get_wifi_blaster_plan] <<< Response Payload:", f" {_audit_response}")
    return _audit_response


def _coerce_to_dict(response: Any) -> Any:
  if response is None:
    return {}
  if isinstance(response, dict):
    return response
  if hasattr(response, "body") and isinstance(response.body, dict):
    return response.body
  if hasattr(response, "json"):
    try:
      return response.json()
    except Exception:
      pass
  if hasattr(response, "text") and response.text:
    try:
      return json.loads(response.text)
    except Exception:
      return {}
  if isinstance(response, str):
    try:
      return json.loads(response)
    except Exception:
      return {}
  return {}


def _mark_wifi_troubleshooting_owner() -> None:
  """Keep the tool-guided WiFi sub-agent in control of the next customer turn."""
  try:
    context.state["wifi_troubleshooting_agent_active"] = "true"
    context.state["wifi_flow_active"] = "false"
    context.state["wifi_offer_pending"] = "false"
    context.state["wifi_scoping_pending"] = "false"
    context.state["wifi_pod_help_pending"] = "false"
  except Exception as e:
    print(f"[get_wifi_blaster_plan] Could not set WiFi ownership state: {e}")


def _resolve_target_device_by_nickname(device_default_nickname: str) -> dict[str, Any]:
  target = _normalize_device_label(device_default_nickname)
  devices = context.state.get("gateway_device_connection_details", {}).get("devices", [])
  if not target or not isinstance(devices, list):
    return {
        "status": "error",
        "error": "Target device nickname was not available.",
        "agent_action": (
            "Ask the customer to choose one of the displayed device names or say"
            " the issue affects all devices."
        ),
    }

  matches = [
      device for device in devices
      if (
          isinstance(device, dict)
          and _normalize_device_label(device.get("defaultNickname")) == target
          and _safe_string(device.get("macAddress"))
      )
  ]
  if not matches:
    available_nicknames = _available_device_nicknames(devices)
    return {
        "status": "error",
        "error": "No stored device matched the selected nickname.",
        "available_device_nicknames": available_nicknames,
        "agent_action": (
            "Ask the customer to choose one of the displayed device names or say"
            " the issue affects all devices. Do not ask for or expose MAC addresses."
        ),
    }
  active_matches = [device for device in matches if device.get("isActive") is True]
  if len(matches) > 1 and len(active_matches) == 1:
    matches = active_matches
  if len(matches) > 1:
    return {
        "status": "error",
        "error": "Multiple stored devices matched the selected nickname.",
        "ambiguous_device_nickname": _safe_string(device_default_nickname),
        "matching_device_count": len(matches),
        "agent_action": (
            "Tell the customer that more than one device has that name, then ask"
            " them to choose a different listed device name or say all devices."
            " Do not ask for or expose MAC addresses."
        ),
    }
  return {"status": "success", "device": matches[0]}


def _available_device_nicknames(devices: Any, limit: int = 10) -> list[str]:
  if not isinstance(devices, list):
    return []
  nicknames = []
  for device in devices:
    if not isinstance(device, dict):
      continue
    nickname = _safe_string(device.get("defaultNickname"))
    if not nickname or nickname in nicknames:
      continue
    nicknames.append(nickname)
    if len(nicknames) >= limit:
      break
  return nicknames


def _normalize_device_label(value: Any) -> str:
  return " ".join(_safe_string(value).lower().split())


def _state_target_device(device: dict[str, Any]) -> dict[str, Any]:
  if not device:
    return {}
  return {
      "defaultNickname": _safe_string(device.get("defaultNickname")),
      "macAddress": _safe_string(device.get("macAddress")),
      "deviceType": _safe_string(device.get("deviceType")),
      "deviceModel": _safe_string(device.get("deviceModel")),
      "hostName": _safe_string(device.get("hostName")),
      "isActive": device.get("isActive") if isinstance(device.get("isActive"), bool) else "",
  }


def _audit_target_device(device: dict[str, Any]) -> dict[str, Any]:
  if not device:
    return {}
  return {
      "defaultNickname": _safe_string(device.get("defaultNickname")),
      "deviceType": _safe_string(device.get("deviceType")),
      "isActive": device.get("isActive") if isinstance(device.get("isActive"), bool) else "",
      "macAddress_present": bool(_safe_string(device.get("macAddress"))),
  }


def _safe_string(value: Any) -> str:
  if value in ("", None):
    return ""
  return str(value).strip()


def _resolve_xbo_id(provided_xbo_id: Any = "") -> str:
  keys = ("xbo_id", "xboId", "xboID", "customer_xbo_id", "customerXboId")
  for value in (provided_xbo_id, *(_lookup_session_value(key) for key in keys)):
    if value not in ("", None, "NOT_FOUND"):
      return str(value).strip()
  return ""


def _lookup_session_value(key: str) -> Any:
  ctx = globals().get("context")
  for source_name in ("state", "session_context", "sessionContext", "session_params", "parameters"):
    source = getattr(ctx, source_name, None)
    if source is None:
      continue
    if isinstance(source, dict):
      value = source.get(key)
    elif hasattr(source, "get"):
      value = source.get(key)
    else:
      value = getattr(source, key, None)
    if value not in ("", None):
      return value
  return None


def _find_plan_id(value: Any) -> str:
  if isinstance(value, dict):
    for key in ("planId", "planID", "plan_id"):
      plan_id = value.get(key)
      if plan_id not in ("", None):
        return str(plan_id)
    for candidate in value.values():
      plan_id = _find_plan_id(candidate)
      if plan_id:
        return plan_id
  elif isinstance(value, list):
    for candidate in value:
      plan_id = _find_plan_id(candidate)
      if plan_id:
        return plan_id
  return ""
