# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""get_gateway_device_connections -- wrapper for xFi Coverage device connections.

Builds the dynamic auth-proxy x-url from the customer's XBO ID and gateway
MAC, calls the xfi_gateway_conn_auth_proxy OpenAPI operation, and
normalizes the response into a small result that the WiFi troubleshooting agent can
use to choose the next customer action.
"""

import json
import time
from typing import Any

_COVERAGE_SCOPE = (
    "x1:xfi-speed-platform:gateway,"
    "x1:xfi-speed-platform:blaster:poll,"
    "x1:xfi-speed-platform:blaster,"
    "x1:xfi-coverage-platform:read"
)


def get_gateway_device_connections(
    xbo_id: str = "", gateway_mac: str = ""
) -> dict[str, Any]:
  """Gets gateway device connection and coverage details.

  Args:
      xbo_id: Customer XBO ID. Optional -- defaults to session context/state
        xbo_id.
      gateway_mac: Gateway MAC address. Optional -- defaults to session
        cable_modem_mac.

  Returns:
      dict: Normalized gateway device-connections result or an error dict.
  """
  _mark_wifi_troubleshooting_owner()
  xbo_id = _resolve_xbo_id(xbo_id)
  if not gateway_mac:
    gateway_mac = context.state.get("cable_modem_mac") or ""

  if not xbo_id:
    return {
        "status": "error",
        "error": "xbo_id is required to get gateway device connections.",
        "agent_action": (
            "Continue with safe WiFi troubleshooting or connect the customer"
            " with someone who can help."
        ),
    }

  if not gateway_mac or gateway_mac == "NOT_FOUND":
    return {
        "status": "error",
        "error": "gateway_mac (cable_modem_mac) is required to get gateway device connections.",
        "agent_action": (
            "Tell the customer gateway-specific WiFi connection details are not"
            " available, then continue with safe WiFi self-help or connect them"
            " with someone who can help."
        ),
    }

  coverage_platform_server = str(
      context.state.get("coverage_platform_server")
      or "https://gw.api.dh.comcast.com"
  ).rstrip("/")
  target_url = (
      f"{coverage_platform_server}/coverage/api/ip_gateway_device_connections"
      f"/accounts/{xbo_id}/gateways/{gateway_mac}?partnerId=comcast"
  )

  tool_args = {
      "accept": "application/json",
      "content-type": "application/json",
      "account-id": xbo_id,
      "agent_name": "gecx_repair_agent",
      "session-id": context.session_id,
      "x-auth": "ST-SAT-XAXLR",
      "x-scope": _COVERAGE_SCOPE,
      "x-url": target_url,
      "x-flow-trace-id": context.session_id,
  }

  _audit_request = {
      "xbo_id": xbo_id,
      "gateway_mac": gateway_mac,
      "x-url": target_url,
  }
  print(
      "[AUDIT] [get_gateway_device_connections] >>> Request Payload:",
      f" {_audit_request}",
  )

  try:
    _api_start = time.time()
    response = tools.xfi_gateway_conn_auth_proxy_getGatewayDeviceConnections(tool_args)
    _api_latency_ms = int((time.time() - _api_start) * 1000)
    print(
        "[AUDIT] [LATENCY] [get_gateway_device_connections] API took:"
        f" {_api_latency_ms} ms"
    )
    if hasattr(response, "status_code"):
      print(
          "[AUDIT] [HTTP STATUS] [get_gateway_device_connections] :"
          f" {response.status_code} - {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(
          "[AUDIT] [API RESPONSE] [get_gateway_device_connections] <<<:"
          f" {response.text}"
      )

    data = _coerce_to_dict(response)
    if not isinstance(data, dict) or not data:
      return {
          "status": "error",
          "error": "Gateway device-connections API returned no usable result.",
          "agent_action": (
              "Continue with safe WiFi troubleshooting steps or connect the"
              " customer with someone who can help."
          ),
      }

    _store_gateway_device_connection_details(data)
    result = _normalize_device_connections(data)
    stored_devices = context.state.get("gateway_device_connection_details", {}).get("devices", [])
    result["device_nicknames"] = _build_device_nickname_list(stored_devices)
    result["active_device_nicknames"] = _build_device_nickname_list(
        stored_devices,
        active_only=True,
    )
    result["stored_device_detail_count"] = len(
        stored_devices
    )
    print(
        "[AUDIT] [get_gateway_device_connections] <<< Response Payload:",
        f" {result}",
    )
    return result

  except Exception as e:
    _audit_response = {
        "status": "error",
        "error": f"Failed to get gateway device connections: {str(e)}",
        "agent_action": (
            "Continue with safe WiFi troubleshooting steps or connect the"
            " customer with someone who can help."
        ),
    }
    print(
        "[AUDIT] [get_gateway_device_connections] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response


def _coerce_to_dict(response: Any) -> Any:
  """Best-effort convert a CXAS ExternalResponse (or str) to a dict."""
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
    print(f"[get_gateway_device_connections] Could not set WiFi ownership state: {e}")


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


def _find_first_device_list(value: Any) -> list[Any]:
  """Finds the first plausible device/client list in a nested response."""
  if isinstance(value, list):
    return value
  if not isinstance(value, dict):
    return []
  preferred_keys = (
      "devices",
      "connectedDevices",
      "deviceConnections",
      "gatewayDeviceConnections",
      "clients",
      "items",
  )
  for key in preferred_keys:
    candidate = value.get(key)
    if isinstance(candidate, list):
      return candidate
  for candidate in value.values():
    found = _find_first_device_list(candidate)
    if found:
      return found
  return []


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
  for key in keys:
    if key in data and data.get(key) not in ("", None):
      return data.get(key)
  return ""


def _extract_gateway_device_connection_details(data: dict[str, Any]) -> list[dict[str, Any]]:
  device_connections = data.get("deviceConnections")
  if not isinstance(device_connections, list):
    device_connections = _find_first_device_list(data)

  details = []
  for device in device_connections:
    if not isinstance(device, dict):
      continue
    fingerprint_data = device.get("fingerprintData")
    if not isinstance(fingerprint_data, dict):
      fingerprint_data = {}
    detail = {
        "defaultNickname": _first_present(device, ("defaultNickname",)),
        "macAddress": _first_present(device, ("macAddress",)),
        "lastSeenOnlineTime": _first_present(device, ("lastSeenOnlineTime",)),
        "isActive": device.get("isActive") if isinstance(device.get("isActive"), bool) else "",
        "hostName": _first_present(device, ("hostName",)),
        "deviceType": _first_present(fingerprint_data, ("deviceType",)),
        "deviceModel": _first_present(fingerprint_data, ("model",)),
    }
    if any(value not in ("", None) for value in detail.values()):
      details.append(detail)
  return details


def _store_gateway_device_connection_details(data: dict[str, Any]) -> None:
  details = _extract_gateway_device_connection_details(data)
  state_payload = {
      "device_count": len(details),
      "devices": details,
  }
  context.state["gateway_device_connection_details"] = state_payload
  print(
      "[AUDIT] [get_gateway_device_connections] stored gateway_device_connection_details:",
      f" {state_payload}",
  )


def _build_device_nickname_list(
    devices: Any,
    active_only: bool = False,
    limit: int = 10,
) -> list[str]:
  if not isinstance(devices, list):
    return []
  nicknames = []
  sorted_devices = sorted(
      [device for device in devices if isinstance(device, dict)],
      key=lambda device: device.get("isActive") is not True,
  )
  for device in sorted_devices:
    if active_only and device.get("isActive") is not True:
      continue
    nickname = _safe_string(device.get("defaultNickname"))
    if not nickname or nickname in nicknames:
      continue
    nicknames.append(nickname)
    if len(nicknames) >= limit:
      break
  return nicknames


def _summarize_device(device: Any) -> dict[str, Any]:
  if not isinstance(device, dict):
    return {"name": "", "connection_type": "", "band": "", "signal_strength": "", "rssi": "", "status": ""}
  return {
      "name": _first_present(device, ("deviceName", "hostName", "hostname", "name")),
      "connection_type": _first_present(device, ("connectionType", "connection_type", "type")),
      "band": _first_present(device, ("band", "wifiBand", "radio")),
      "signal_strength": _first_present(device, ("signalStrength", "signal_strength", "rssiCategory")),
      "rssi": _first_present(device, ("rssi", "RSSI")),
      "status": _first_present(device, ("status", "connectionStatus", "state")),
  }


def _safe_string(value: Any) -> str:
  if value in ("", None):
    return ""
  return str(value).strip()


def _normalize_device_connections(data: dict[str, Any]) -> dict[str, Any]:
  devices = _find_first_device_list(data)
  summarized_devices = [_summarize_device(d) for d in devices[:20]]
  connected_count = len(devices)

  weak_signal_devices = []
  offline_devices = []
  for device in summarized_devices:
    signal = str(device.get("signal_strength") or "").lower()
    status = str(device.get("status") or "").lower()
    rssi = device.get("rssi")
    try:
      rssi_num = float(rssi)
    except (TypeError, ValueError):
      rssi_num = None
    if "weak" in signal or "poor" in signal or (rssi_num is not None and rssi_num <= -70):
      weak_signal_devices.append(device)
    if status and status not in ("connected", "online", "active", "true"):
      offline_devices.append(device)

  troubleshooting_hints = []
  if weak_signal_devices:
    troubleshooting_hints.append(
        "One or more connected devices appears to have weak WiFi signal; start with distance, placement, or obstruction checks."
    )
  if offline_devices:
    troubleshooting_hints.append(
        "One or more devices appears offline or not actively connected; start with device WiFi toggle or forget/rejoin steps."
    )
  if not troubleshooting_hints:
    troubleshooting_hints.append(
        "No obvious device-connection issue was summarized; continue with normal one-step-at-a-time WiFi troubleshooting."
    )

  return {
      "status": "success",
      "connected_device_count": connected_count,
      "devices": summarized_devices,
      "weak_signal_device_count": len(weak_signal_devices),
      "offline_device_count": len(offline_devices),
      "troubleshooting_hints": troubleshooting_hints,
      "source_payload_received": True,
  }
