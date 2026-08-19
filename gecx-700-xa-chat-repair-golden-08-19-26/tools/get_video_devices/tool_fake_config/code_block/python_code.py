from typing import Any, Optional

# Offline fake for get_video_devices. Returns the two-device sample from the ask
# (a non-X1 "TV 1" and an X1 "Living Room") so multi-device selection is testable.
# Drive scenarios via mock_config_dict:
#   { "video_devices": "single" }  -> one active X1 box
#   { "video_devices": "none" }    -> no video devices
_MULTI = [
    {"friendlyName": "TV 1", "mac": "f0:46:3b:58:f3:ea", "activationStatus": "ACTIVE", "x1": False, "model": "SCXI1BEIT"},
    {"friendlyName": "Living Room", "mac": "60:c5:ad:1e:d7:05", "activationStatus": "ACTIVE", "x1": True, "model": "SX022ANM"},
]
_SINGLE = [
    {"friendlyName": "Living Room", "mac": "60:c5:ad:1e:d7:05", "activationStatus": "ACTIVE", "x1": True, "model": "SX022ANM"},
]


def _get_mock_config(state) -> dict:
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  input = input or {}
  state = callback_context.state
  mode = str(_get_mock_config(state).get("video_devices") or "multi").lower()
  print(f"[mock get_video_devices] mode={mode}")

  if mode == "none":
    devices = []
  elif mode == "single":
    devices = [dict(d) for d in _SINGLE]
  else:
    devices = [dict(d) for d in _MULTI]

  try:
    state["video_devices"] = devices
    state["video_status"] = "devices_found" if devices else "no_devices"
  except Exception:  # pylint: disable=broad-exception-caught
    pass

  return {
      "status": "success" if devices else "no_devices",
      "video_devices": devices,
      "device_count": len(devices),
  }

