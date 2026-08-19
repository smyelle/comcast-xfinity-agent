from typing import Any, Optional

_DEFAULT_MOCK_MODE = "healthy"
_VALID_MOCK_MODES = (
    "healthy",
    "reboot",
    "swap",
    "no_telemetry",
    "unsupported_device",
    "error",
)

# Friendly aliases accepted for the gateway_status param.
_MODE_ALIASES = {
    "clear": "healthy",
    "ok": "healthy",
    "restart": "reboot",
    "powercycle": "reboot",
    "replace": "swap",
    "exchange": "swap",
    "predictive_swap": "swap",
    "no telemetry": "no_telemetry",
    "not_supported": "unsupported_device",
    "unsupported": "unsupported_device",
    "failed": "error",
}

# The orchestrator's after_tool_callback reads tool_response["response"] as a
# string, extracts the JSON block, then reads report.gateway_status (and
# wifi_status). So the fake returns {"response": "<diagnostic JSON string>"} --
# the shape the real gateway_specialist_agent emits. (wifi sanitization in the
# orchestrator is disabled, so wifi_status always resolves to healthy.)
_REPORTS = {
    "healthy": (
        '{"gateway_status": "healthy", "wifi_status": "healthy",'
        ' "gateway_details": "Gateway telemetry healthy; no hardware or'
        ' software faults detected."}'
    ),
    "reboot": (
        '{"gateway_status": "reboot", "wifi_status": "healthy",'
        ' "gateway_details": "Gateway software fault detected; a reboot should'
        ' resolve it."}'
    ),
    "swap": (
        '{"gateway_status": "swap", "wifi_status": "healthy",'
        ' "gateway_details": "Gateway hardware fault detected; replacement'
        ' recommended."}'
    ),
    "no_telemetry": (
        '{"gateway_status": "no_telemetry", "wifi_status": "healthy",'
        ' "gateway_details": "No recent telemetry available from the gateway."}'
    ),
    "unsupported_device": (
        '{"gateway_status": "unsupported_device", "wifi_status": "healthy",'
        ' "gateway_details": "Device model not supported for automated'
        ' diagnostics."}'
    ),
    "error": (
        '{"gateway_status": "error", "wifi_status": "healthy",'
        ' "gateway_details": "Gateway diagnostics failed."}'
    ),
}


def _get_mock_config(state) -> dict:
  """Return mock_config_dict (platform already deserializes it to a dict)."""
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
    # pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long
  """Mock the gateway_specialist_agent_as_a_tool (agent-as-a-tool).

  Faking at this boundary short-circuits the gateway_specialist_agent
  sub-agent (and its RDK device diagnostics), which tool-fake mode cannot
  reach. Returns the sub-agent's diagnostic-summary string under the "response"
  key, which the orchestrator after_tool_callback parses for gateway_status.

  Mode is read from the "gateway_status" key in the mock_config_dict (e.g.
  {"gateway_status": "reboot", "network_status": "healthy"}), else
  _DEFAULT_MOCK_MODE. Supported modes:
      - "healthy":            no gateway issue
      - "reboot":             software fault -> Priority 5 reboot offer
      - "swap":               hardware fault -> Priority 6 swap recommendation
      - "no_telemetry":       no telemetry -> Priority 7 transfer
      - "unsupported_device": model unsupported -> Priority 8 transfer
      - "error":              diagnostics failed -> Priority 9 transfer

  Aliases: "clear"/"ok" -> "healthy"; "restart"/"powercycle" -> "reboot";
  "replace"/"exchange"/"predictive_swap" -> "swap"; "not_supported"/
  "unsupported" -> "unsupported_device"; "failed" -> "error".
  """

  input = input or {}
  state = callback_context.state

  # Resolve the mock mode from the unified mock_config_dict (key "gateway_status").
  # Falls back to this tool's default scenario when the key is absent.
  try:
    cfg = _get_mock_config(state)
    candidate = str(
        cfg.get("gateway_status")
        or _DEFAULT_MOCK_MODE
    ).lower()
    candidate = _MODE_ALIASES.get(candidate, candidate)
    if candidate in _VALID_MOCK_MODES:
      mode = candidate
    else:
      print(
          "[mock gateway_specialist_agent_as_a_tool] Ignoring unsupported"
          f" gateway_status={candidate!r}; valid values: {list(_VALID_MOCK_MODES)}"
      )
      mode = _DEFAULT_MOCK_MODE
  except Exception:  # pylint: disable=broad-exception-caught
    mode = _DEFAULT_MOCK_MODE

  print(f"[mock gateway_specialist_agent_as_a_tool] mode: {mode}")

  return {"response": _REPORTS[mode]}
