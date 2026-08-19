from typing import Any, Optional

# Offline fake for whole_home_refresh. Default = started. Drive scenarios via
# mock_config_dict { "whole_home_refresh": "started" | "throttled" | "error" }.


def _get_mock_config(state) -> dict:
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  input = input or {}
  state = callback_context.state
  mode = str(_get_mock_config(state).get("whole_home_refresh") or "started").lower()
  print(f"[mock whole_home_refresh] mode={mode}")

  if mode == "throttled":
    _set(state, "refresh_throttled")
    return {"status": "throttled", "message": "too_early", "tracking_id": "", "last_refresh_time": ""}
  if mode == "error":
    _set(state, "refresh_error")
    return {"status": "error", "message": "unexpected_response", "agent_action": "transfer_to_human"}
  _set(state, "refresh_started")
  return {
      "status": "started",
      "tracking_id": "sr.c68ee03c-2ecc-4217-a109-ba681e88dcf1",
      "last_refresh_time": "2026-07-29T00:46:54+0000",
      "message": "",
  }


def _set(state, video_status: str) -> None:
  try:
    state["video_status"] = video_status
  except Exception:  # pylint: disable=broad-exception-caught
    pass

