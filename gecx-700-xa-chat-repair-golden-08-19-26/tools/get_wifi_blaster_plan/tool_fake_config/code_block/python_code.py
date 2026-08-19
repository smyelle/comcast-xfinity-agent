from typing import Any, Optional


def _get_mock_config(state) -> dict:
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


def _mark_wifi_troubleshooting_owner(state) -> None:
  state["wifi_troubleshooting_agent_active"] = "true"
  state["wifi_flow_active"] = "false"
  state["wifi_offer_pending"] = "false"
  state["wifi_scoping_pending"] = "false"
  state["wifi_pod_help_pending"] = "false"


def _normalize_device_label(value: Any) -> str:
  if value in ("", None):
    return ""
  return " ".join(str(value).strip().lower().split())


def _resolve_target_device_by_nickname(state, input: dict[str, Any]) -> dict[str, Any]:
  nickname = input.get("device_default_nickname") or ""
  if not nickname:
    return {"status": "success", "device": {}}
  devices = state.get("gateway_device_connection_details", {}).get("devices", [])
  target = _normalize_device_label(nickname)
  matches = [
      device for device in devices
      if (
          isinstance(device, dict)
          and _normalize_device_label(device.get("defaultNickname")) == target
          and device.get("macAddress")
      )
  ]
  if not matches:
    return {
        "status": "error",
        "error": "No stored device matched the selected nickname.",
        "agent_action": (
            "Ask the customer to choose one of the displayed device names or say"
            " the issue affects all devices. Do not ask for or expose MAC addresses."
        ),
    }
  if len(matches) > 1:
    return {
        "status": "error",
        "error": "Multiple stored devices matched the selected nickname.",
        "matching_device_count": len(matches),
        "agent_action": (
            "Tell the customer that more than one device has that name, then ask"
            " them to choose a different listed device name or say all devices."
            " Do not ask for or expose MAC addresses."
        ),
    }
  return {"status": "success", "device": matches[0]}


def _target_device_state(device: dict[str, Any]) -> dict[str, Any]:
  if not device:
    return {}
  return {
      "defaultNickname": device.get("defaultNickname") or "",
      "macAddress": device.get("macAddress") or "",
      "deviceType": device.get("deviceType") or "",
      "deviceModel": device.get("deviceModel") or "",
      "hostName": device.get("hostName") or "",
      "isActive": device.get("isActive") if isinstance(device.get("isActive"), bool) else "",
  }


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  state = callback_context.state
  _mark_wifi_troubleshooting_owner(state)
  cfg = _get_mock_config(state)
  mode = str(cfg.get("wifi_blaster_plan") or "success").lower()
  print(f"[mock get_wifi_blaster_plan] mode: {mode}")

  if mode in ("error", "fail", "missing"):
    return {
        "status": "error",
        "error": "Mock WiFi blaster plan failure.",
        "agent_action": (
            "Continue with safe WiFi troubleshooting or connect the customer"
            " with someone who can help."
        ),
    }

  resolved_device = _resolve_target_device_by_nickname(state, input)
  if resolved_device.get("status") != "success":
    return resolved_device
  target_device = resolved_device.get("device") or {}
  state["wifi_blaster_plan_id"] = "mock-wifi-blaster-plan-id"
  state["wifi_blaster_target_device"] = _target_device_state(target_device)
  return {
      "status": "success",
      "plan_id_available": True,
      "targeted_device_selected": bool(target_device),
      "target_device_label": target_device.get("defaultNickname") or "",
      "target_device_type": target_device.get("deviceType") or "",
      "agent_action": (
          "Use the stored wifi_blaster_plan_id for the follow-up WiFi blaster"
          " start-test tool when it is available."
      ),
  }
