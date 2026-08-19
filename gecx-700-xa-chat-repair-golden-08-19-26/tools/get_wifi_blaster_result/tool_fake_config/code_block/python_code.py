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


def _poll_metadata(state, plan_id: str = "mock-wifi-blaster-plan-id") -> dict[str, Any]:
  previous = state.get("wifi_blaster_result")
  attempts = 0
  if isinstance(previous, dict) and previous.get("_poll_plan_id") == plan_id:
    try:
      attempts = int(previous.get("_poll_attempt_count") or 0)
    except (TypeError, ValueError):
      attempts = 0
  return {
      "_poll_plan_id": plan_id,
      "_last_poll_epoch_seconds": 0,
      "_poll_attempt_count": attempts + 1,
      "_poll_delay_seconds": 5,
  }


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  state = callback_context.state
  _mark_wifi_troubleshooting_owner(state)
  cfg = _get_mock_config(state)
  mode = str(cfg.get("wifi_blaster_result") or "success").lower()
  print(f"[mock get_wifi_blaster_result] mode: {mode}")

  if mode in ("error", "fail", "missing"):
    return {
        "status": "error",
        "error": "Mock WiFi blaster result failure.",
        "agent_action": (
            "Continue with safe WiFi troubleshooting or connect the customer"
            " with someone who can help."
        ),
    }

  if mode in ("pending", "running", "in_progress", "in-progress"):
    state["wifi_blaster_result"] = {
        "state": "IN_PROGRESS",
        "is_complete": False,
        "blast_result_count": 0,
        "observations": [],
        "contextual_results": [],
        "observation_summary": "",
        **_poll_metadata(state),
    }
    return {
        "status": "success",
        "result_available": False,
        "result_state": "IN_PROGRESS",
        "blast_result_count": 0,
        "observations": [],
        "observation_summary": "",
        "agent_action": (
            "Poll again according to the WiFi troubleshooting flow. Do not"
            " expose raw result JSON to the customer."
        ),
    }

  state["wifi_blaster_result"] = {
      "state": "COMPLETE",
      "is_complete": True,
      "blast_result_count": 1,
      "observations": [{
          "device_label": "Watch",
          "device_type": "watch",
          "result_type": "great",
          "status_code": "RESULT_CODE_SUCCEED",
          "activity_state": "PASS",
          "has_concern": False,
          "band": "5.0 GHZ",
          "channel": 44,
          "signal_strength_dbm": -67,
          "snr_db": 31,
          "tx_phy_rate_mbps": 43,
          "rx_phy_rate_mbps": 72,
          "throughput_mbps": 0.0,
          "observation": (
              "Watch shows a strong WiFi result on 5.0 GHZ with signal -67 dBm,"
              " SNR 31 dB, and activity PASS."
          ),
      }],
      "contextual_results": [{
          "resultType": "great",
          "deviceType": "watch",
          "deviceLabel": "Watch",
      }],
      "observation_summary": (
          "1 device path result(s) returned; result types: great; status codes:"
          " RESULT_CODE_SUCCEED; 1 visible activity check(s) passed; contextual"
          " results: 1 great; device types: 1 watch; devices checked: Watch."
      ),
      **_poll_metadata(state),
  }
  return {
      "status": "success",
      "result_available": True,
      "result_state": "COMPLETE",
      "blast_result_count": 1,
      "observations": state["wifi_blaster_result"]["observations"],
      "contextual_results": state["wifi_blaster_result"]["contextual_results"],
      "observation_summary": state["wifi_blaster_result"]["observation_summary"],
      "agent_action": (
          "Use the stored observation summary for follow-up WiFi troubleshooting"
          " decisions. Do not expose raw result JSON to the customer."
      ),
  }
