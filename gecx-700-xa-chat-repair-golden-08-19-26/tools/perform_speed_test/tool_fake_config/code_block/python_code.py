from typing import Any, Optional


def _get_mock_config(state) -> dict:
  """Return mock_config_dict (platform already deserializes it to a dict)."""
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


_RESULT_STARTED = {
    "status": "success",
    "result_available": False,
    "async_started": True,
    "execution_id_present": True,
    "pollingIntervalInSeconds": 5,
    "suggestedTotalPollingDurationInSeconds": 60,
    "agent_action": (
        "Tell the customer the speed test has started and may take a minute or so"
        " to complete. Ask them to reply in about a minute so you can check the result."
    ),
}

_RESULT_ERROR = {
    "status": "error",
    "error": "Speed test start returned no usable result.",
    "agent_action": (
        "Tell the customer the speed test couldn't be started right now and offer"
        " to try again or connect them with someone who can help."
    ),
}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  state = callback_context.state
  cfg = _get_mock_config(state)
  mode = str(cfg.get("speed_test_result") or "restart").lower()
  print(f"[mock perform_speed_test] mode: {mode}")

  if mode in ("error", "fail"):
    return dict(_RESULT_ERROR)
  state["async_speed_test_execution_id"] = "mock-speed-test-execution-id"
  state["speed_test_async_result_pending"] = "true"
  return dict(_RESULT_STARTED)
