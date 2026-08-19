from typing import Any, Optional


def _get_mock_config(state) -> dict:
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  """Deterministic fake for goldens/sims. Defaults to 'available' and caches a
  sample xbo_id so a downstream perform_speed_test (fake) stays consistent.
  Set mock_config_dict["xbo_status"] = "unavailable" to exercise the no-xbo path."""
  state = callback_context.state
  cfg = _get_mock_config(state)
  mode = str(cfg.get("xbo_status") or "available").lower()

  if mode in ("unavailable", "none", "missing", "false"):
    print("[mock prepare_speed_test] xbo_status: unavailable")
    return {
        "status": "unavailable",
        "agent_action": (
            "A speed test isn't available on this account; let the customer know and"
            " offer other help. Do NOT suggest trying again."
        ),
    }

  state["xbo_id"] = "5830718522526773902"
  print("[mock prepare_speed_test] xbo_status: available")
  return {"status": "available"}

