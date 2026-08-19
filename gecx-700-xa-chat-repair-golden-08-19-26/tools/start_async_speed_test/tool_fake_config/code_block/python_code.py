from typing import Any, Optional


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  state = callback_context.state
  cfg = state.get("mock_config_dict") or {}
  mode = str(cfg.get("async_speed_test_start") or "success").lower() if isinstance(cfg, dict) else "success"
  print(f"[mock start_async_speed_test] mode: {mode}")

  if mode in ("error", "fail"):
    return {
        "status": "error",
        "reason": "api_error",
        "error": "Failed to start async speed test.",
        "agent_action": (
            "Tell the customer the speed test couldn't be started right now and offer"
            " to try again or connect them with someone who can help."
        ),
    }

  execution_id = "mock-speedtest-execution-123"
  state["async_speed_test_execution_id"] = execution_id
  return {
      "status": "success",
      "execution_id": execution_id,
      "pollingIntervalInSeconds": 3.0,
      "suggestedTotalPollingDurationInSeconds": 130.0,
      "agent_action": (
          "The async speed test has started. Use the async speed-test result tool"
          " with the stored execution id to poll for completion before summarizing."
      ),
  }
