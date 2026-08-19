from typing import Any, Optional


_RESULT_PASS = {
    "status": "success",
    "result_available": True,
    "result_state": "COMPLETE",
    "download_mbps": 924.8,
    "upload_mbps": 168.0,
    "latency_ms": 15.7,
    "overall_result": "FULL_PASS",
    "recommendation": "none",
    "summary": (
        "Download came in at 924.8 Mbps (116% of your 800.0 Mbps plan)."
        " Upload came in at 168.0 Mbps (112% of your 150.0 Mbps plan). Latency was 15.7 ms."
    ),
    "agent_action": "Use this grounded async speed-test summary.",
}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  state = callback_context.state
  cfg = state.get("mock_config_dict") or {}
  mode = str(cfg.get("async_speed_test_result") or "complete").lower() if isinstance(cfg, dict) else "complete"
  print(f"[mock get_async_speed_test_result] mode: {mode}")

  if mode in ("pending", "running", "in_progress"):
    state["speed_test_async_result_pending"] = "true"
    state["async_speed_test_result"] = {
        "status": "pending",
        "result_state": "IN_PROGRESS",
        "result_available": False,
        "completed_streams": ["DOWNLOAD"],
        "missing_streams": ["UPLOAD"],
    }
    return {
        "status": "success",
        "result_available": False,
        "result_state": "IN_PROGRESS",
        "completed_streams": ["DOWNLOAD"],
        "missing_streams": ["UPLOAD"],
        "agent_action": "Poll get_async_speed_test_result again before summarizing the speed test.",
    }
  if mode in ("error", "fail"):
    return {
        "status": "error",
        "reason": "api_error",
        "error": "Failed to get async speed-test result.",
        "agent_action": (
            "Tell the customer the speed-test result couldn't be retrieved right now"
            " and offer to try again or connect them with someone who can help."
        ),
    }

  state["speed_test_async_result_pending"] = "false"
  state["async_speed_test_result"] = dict(_RESULT_PASS)
  return dict(_RESULT_PASS)
