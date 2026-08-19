from typing import Any, Optional

_DEFAULT_MOCK_MODE = "clear"
_VALID_MOCK_MODES = (
    "clear",
    "predictive_swap",
    "predictive_offline",
    "technician",
    "error",
)

# Friendly aliases accepted for the convoy_status param.
_MODE_ALIASES = {
    "none": "clear",
    "no_recommendation": "clear",
    "predictive_impairment": "technician",
}

# Each mode maps to the return dict the real check_convoy_recommendations would
# produce. The repair_orchestration_agent before_agent_callback consumes this
# return value and derives the {convoy_status} session variable from
# routing_action: "swap" -> predictive_swap, "technician" ->
# predictive_impairment, "device_offline" -> predictive_offline, else -> clear;
# a status of "error" -> convoy_status "error".
_MOCK_SCENARIOS = {
    "clear": {
        "status": "success",
        "routing_action": "none",
        "repair_recommendations": [],
    },
    "predictive_swap": {
        "status": "success",
        "routing_action": "swap",
        "repair_recommendations": [{
            "name": "PREDICTIVE_GATEWAYSWAP",
            "activity_code": "GWSWAP",
            "job_type": "Swap",
            "activity_type": "TROUBLE_CALL",
            "description": (
                "Your gateway is failing intermittently and a replacement is"
                " recommended."
            ),
            "recommended_action": "ReplaceDevice",
        }],
    },
    "predictive_offline": {
        "status": "success",
        "routing_action": "device_offline",
        "repair_recommendations": [{
            "name": "XIModemOfflineDigital",
            "activity_code": "H2",
            "job_type": "Test",
            "activity_type": "TROUBLE_CALL",
            "description": (
                "Your gateway has been offline and a reboot is recommended."
            ),
            "recommended_action": "RebootDevice",
        }],
    },
    "technician": {
        "status": "success",
        "routing_action": "technician",
        "repair_recommendations": [{
            "name": "XITNetworkImpairment",
            "activity_code": "H2",
            "job_type": "Test",
            "activity_type": "TROUBLE_CALL",
            "description": (
                "We found an issue with the connection to your home that needs"
                " a technician."
            ),
            "recommended_action": "CreateAppointment",
        }],
    },
    "error": {
        "status": "error",
        "routing_action": "none",
        "repair_recommendations": [],
        "error": "Mock Convoy failure: simulated tool/parse error.",
        "agent_action": "transfer_to_human",
    },
}


def _get_mock_config(state) -> dict:
  """Return mock_config_dict (platform already deserializes it to a dict)."""
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
    # pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long
  """Mock Convoy recommendations check. Returns success/error maps matching the real tool.

  Mode is read from the "convoy_status" key in the mock_config_dict (e.g.
  {"convoy_status": "predictive_swap", "outage_status": "none"}), else
  _DEFAULT_MOCK_MODE. Supported
  modes:
      - "clear":              no repair recommendations (healthy)
      - "predictive_swap":    predictive gateway swap recommendation
      - "predictive_offline": predictive offline / reboot recommendation
      - "technician":         network impairment requiring a technician
      - "error":              error shape with agent_action

  Aliases: "none"/"no_recommendation" -> "clear",
  "predictive_impairment" -> "technician".

  Args:
      account_number: The customer billing account number.

  Returns:
      dict matching the real check_convoy_recommendations shapes.
  """

  input = input or {}
  state = callback_context.state
  account_number = (
      input.get("account_number")
      or state.get("accountNumber")
      or state.get("account_id")
      or ""
  )

  # Mirror the real tool's first guard exactly (note: no agent_action here).
  if not account_number:
    return {
        "status": "error",
        "repair_recommendations": [],
        "routing_action": "none",
        "error": "account_number is required.",
    }

  # Resolve the mock mode from the unified mock_config_dict (key "convoy_status").
  # Falls back to this tool's default scenario when the key is absent.
  try:
    cfg = _get_mock_config(state)
    candidate = str(
        cfg.get("convoy_status")
        or _DEFAULT_MOCK_MODE
    ).lower()
    candidate = _MODE_ALIASES.get(candidate, candidate)
    if candidate in _VALID_MOCK_MODES:
      mode = candidate
    else:
      print(
          "[mock check_convoy_recommendations] Ignoring unsupported"
          f" convoy_status={candidate!r}; valid values: {list(_VALID_MOCK_MODES)}"
      )
      mode = _DEFAULT_MOCK_MODE
  except Exception:  # pylint: disable=broad-exception-caught
    mode = _DEFAULT_MOCK_MODE

  print(
      f"[mock check_convoy_recommendations] account_number: {account_number},"
      f" mode: {mode}"
  )

  scenario = _MOCK_SCENARIOS[mode]
  routing_action = scenario["routing_action"]
  # Copy the recommendation dicts so callers can't mutate the scenario table.
  repair_recommendations = [dict(rec) for rec in scenario["repair_recommendations"]]

  # Mirror the real tool's success-path state write. The before_agent_callback
  # derives {convoy_status} from routing_action, so we don't set it here.
  try:
    state["convoy_routing_action"] = routing_action
    print(
        "[mock check_convoy_recommendations] Set context.state variables"
        " successfully"
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(
        f"[mock check_convoy_recommendations] Could not set state variables: {e}"
    )

  # ---- VIDEO XIT recommendation (additive; mirrors the real tool's video parse) ----
  # Enable via mock_config_dict {"video_xit": "truck_roll"} (or "present") to simulate
  # an XIT_AIQ_PREDICTIVE_RFVIDEO recommendation for Video goldens. Default: none.
  try:
    import json as _json  # local import; keeps the module top unchanged
    video_xit = str(cfg.get("video_xit") or "").lower()
    if video_xit in ("truck_roll", "present", "appointment", "true"):
      state["video_xit_recommendation"] = _json.dumps([{
          "name": "XIT_AIQ_PREDICTIVE_RFVIDEO",
          "recommended_action": "CreateAppointment",
          "description": "A predictive video/RF issue was detected that needs a technician visit.",
          "activity_code": "H2",
          "job_type": "Test",
          "problem_code": "",
          "intents": "",
      }])
    else:
      state["video_xit_recommendation"] = ""
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[mock check_convoy_recommendations] Could not set video_xit_recommendation: {e}")

  # ---- ERROR SHAPE (matches tool-failure / parse-error paths) ----
  if scenario["status"] == "error":
    return {
        "status": "error",
        "repair_recommendations": [],
        "routing_action": "none",
        "error": scenario.get("error", "Mock Convoy failure."),
        "agent_action": scenario.get("agent_action", "transfer_to_human"),
    }

  # ---- SUCCESS SHAPE ----
  return {
      "status": "success",
      "repair_recommendations": repair_recommendations,
      "routing_action": routing_action,
  }
