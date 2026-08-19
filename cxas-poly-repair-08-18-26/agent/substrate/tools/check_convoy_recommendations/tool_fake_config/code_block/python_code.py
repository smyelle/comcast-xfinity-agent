from typing import Any, Optional

_DEFAULT_MOCK_MODE = "clear"
_VALID_MOCK_MODES = (
    "clear",
    "predictive_swap",
    "predictive_offline",
    "technician",
    "technician_8069",
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
    "technician_8069": {
        "status": "success",
        "routing_action": "technician",
        "repair_recommendations": [{
            "name": "OutsideHomeSRO",
            "activity_code": "15",
            "job_type": "PF",
            "activity_type": "TROUBLE_CALL",
            "description": (
                "I found an issue with the connection to your home. We'll need"
                " to send a technician to check the physical lines."
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


def _parse_query_param(query_string: str, key: str) -> Optional[str]:
  """Return the last value for `key` in an `a=b&c=d` query string, or None.

  Plain-string parsing only -- no library imports (the host disallows them).
  """
  value = None
  for pair in str(query_string).split("&"):
    if not pair:
      continue
    name, sep, raw = pair.partition("=")
    if sep and name.strip() == key:
      value = raw.strip()
  return value


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
    # pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long
  """Mock Convoy recommendations check. Returns success/error maps matching the real tool.

  Mode is read from the "convoy_status" param in the mock_config_string query
  string (e.g. "convoy_status=predictive_swap&outage_status=none"). Falls back
  to context.state["mock_convoy_mode"], else _DEFAULT_MOCK_MODE. Supported
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

  # Resolve the mock mode. Prefer the "convoy_status" param parsed out of the
  # mock_config_string query string (e.g. "convoy_status=predictive_swap&..."),
  # which may also carry params that configure other mocks. Fall back to the
  # legacy mock_convoy_mode state var, then to the default.
  try:
    mode = None
    mock_config_string = state.get("mock_config_string") or ""
    if mock_config_string:
      convoy_status_value = _parse_query_param(mock_config_string, "convoy_status")
      if convoy_status_value:
        candidate = convoy_status_value.lower()
        candidate = _MODE_ALIASES.get(candidate, candidate)
        if candidate in _VALID_MOCK_MODES:
          mode = candidate
        else:
          print(
              "[mock check_convoy_recommendations] Ignoring unsupported"
              f" convoy_status={convoy_status_value!r}; valid values:"
              f" {list(_VALID_MOCK_MODES)}"
          )
    if mode is None:
      if account_number == "8069100020079827":
        mode = "technician_8069"
      else:
        mode = str(state.get("mock_convoy_mode") or _DEFAULT_MOCK_MODE).lower()
        mode = _MODE_ALIASES.get(mode, mode)
        if mode not in _VALID_MOCK_MODES:
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
