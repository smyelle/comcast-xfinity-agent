from typing import Any, Optional

# Offline fake for restart_ip_stb. Default = success. Drive scenarios via
# mock_config_dict { "stb_restart": "success" | "warning" | "failure" } OR by MAC:
# a MAC starting with "a4" simulates the "unrecognized device" warning, and an
# obviously malformed MAC simulates the invalid-MAC failure.


def _get_mock_config(state) -> dict:
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  input = input or {}
  state = callback_context.state
  mac = (input.get("mac") or state.get("video_selected_device_mac") or state.get("video_device_mac") or "60:c5:ad:1e:d7:05").strip()
  mode = str(_get_mock_config(state).get("stb_restart") or "").lower()

  # MAC-derived defaults when no explicit mode is set.
  if not mode:
    compact = mac.replace(":", "")
    if len(compact) != 12 or any(c not in "0123456789abcdefABCDEF" for c in compact):
      mode = "failure"
    elif mac.lower().startswith("a4"):
      mode = "warning"
    else:
      mode = "success"
  print(f"[mock restart_ip_stb] mac={mac}, mode={mode}")

  if mode == "warning":
    _set(state, "restart_failed_offline")
    return {"status": "warning", "status_code": "2", "mac": mac, "action": "restartIpStb", "message": "device_not_found_or_unrecognized"}
  if mode == "failure":
    _set(state, "restart_error")
    return {"status": "failure", "status_code": "1", "mac": mac, "action": "restartIpStb", "message": "restart_failed"}
  _set(state, "restart_issued")
  return {"status": "success", "status_code": "0", "mac": mac, "action": "restartIpStb"}


def _set(state, video_status: str) -> None:
  try:
    state["video_status"] = video_status
  except Exception:  # pylint: disable=broad-exception-caught
    pass

