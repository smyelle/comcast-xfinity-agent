from typing import Any, Optional


def _get_mock_config(state) -> dict:
  """Return mock_config_dict (platform already deserializes it to a dict)."""
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


_RESULT_HEALTHY = {
    "status": "success",
    "connected_device_count": 2,
    "devices": [
        {
            "name": "Living Room TV",
            "connection_type": "wifi",
            "band": "5GHz",
            "signal_strength": "good",
            "rssi": -55,
            "status": "connected",
        },
        {
            "name": "Laptop",
            "connection_type": "wifi",
            "band": "5GHz",
            "signal_strength": "good",
            "rssi": -58,
            "status": "connected",
        },
    ],
    "weak_signal_device_count": 0,
    "offline_device_count": 0,
    "troubleshooting_hints": [
        "No obvious device-connection issue was summarized; continue with normal one-step-at-a-time WiFi troubleshooting."
    ],
    "source_payload_received": True,
}

_RESULT_WEAK_SIGNAL = {
    "status": "success",
    "connected_device_count": 2,
    "devices": [
        {
            "name": "Bedroom Phone",
            "connection_type": "wifi",
            "band": "2.4GHz",
            "signal_strength": "weak",
            "rssi": -76,
            "status": "connected",
        },
        {
            "name": "Kitchen Tablet",
            "connection_type": "wifi",
            "band": "5GHz",
            "signal_strength": "good",
            "rssi": -57,
            "status": "connected",
        },
    ],
    "weak_signal_device_count": 1,
    "offline_device_count": 0,
    "troubleshooting_hints": [
        "One or more connected devices appears to have weak WiFi signal; start with distance, placement, or obstruction checks."
    ],
    "source_payload_received": True,
}

_RESULT_ERROR = {
    "status": "error",
    "error": "Mock gateway device-connections failure.",
    "agent_action": (
        "Continue with safe WiFi troubleshooting steps or connect the customer"
        " with someone who can help."
    ),
}


def _store_mock_gateway_device_connection_details(state, result: dict[str, Any]) -> None:
  if result.get("status") != "success":
    return
  devices = []
  for device in result.get("devices", []):
    if not isinstance(device, dict):
      continue
    name = device.get("name") or ""
    devices.append({
        "defaultNickname": name,
        "macAddress": f"MOCK{name.replace(' ', '').upper()[:8]}",
        "lastSeenOnlineTime": "2026-07-26T08:58:49-04:00",
        "isActive": device.get("status") == "connected",
        "hostName": name.replace(" ", "-").lower(),
        "deviceType": "mockDevice",
        "deviceModel": name,
    })
  state["gateway_device_connection_details"] = {
      "device_count": len(devices),
      "devices": devices,
  }


def _add_mock_device_nicknames(state, result: dict[str, Any]) -> None:
  devices = state.get("gateway_device_connection_details", {}).get("devices", [])
  result["device_nicknames"] = [
      device.get("defaultNickname")
      for device in devices[:10]
      if isinstance(device, dict) and device.get("defaultNickname")
  ]
  result["active_device_nicknames"] = [
      device.get("defaultNickname")
      for device in devices[:10]
      if (
          isinstance(device, dict)
          and device.get("isActive") is True
          and device.get("defaultNickname")
      )
  ]


def _mark_wifi_troubleshooting_owner(state) -> None:
  state["wifi_troubleshooting_agent_active"] = "true"
  state["wifi_flow_active"] = "false"
  state["wifi_offer_pending"] = "false"
  state["wifi_scoping_pending"] = "false"
  state["wifi_pod_help_pending"] = "false"


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  state = callback_context.state
  _mark_wifi_troubleshooting_owner(state)
  cfg = _get_mock_config(state)
  mode = str(cfg.get("gateway_device_connections") or "healthy").lower()
  print(f"[mock get_gateway_device_connections] mode: {mode}")

  if mode in ("error", "fail"):
    return dict(_RESULT_ERROR)
  if mode in ("weak", "weak_signal", "coverage_gap"):
    result = dict(_RESULT_WEAK_SIGNAL)
    _store_mock_gateway_device_connection_details(state, result)
    _add_mock_device_nicknames(state, result)
    result["stored_device_detail_count"] = state["gateway_device_connection_details"]["device_count"]
    return result
  result = dict(_RESULT_HEALTHY)
  _store_mock_gateway_device_connection_details(state, result)
  _add_mock_device_nicknames(state, result)
  result["stored_device_detail_count"] = state["gateway_device_connection_details"]["device_count"]
  return result
