from typing import Any

_DEFAULT_MOCK_MODE = "healthy"
_VALID_MOCK_MODES = (
    "healthy",
    "reboot",
    "swap",
    "no_telemetry",
    "unsupported_device",
    "error",
    "skipped",
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
    "skipped": (
        '{"gateway_status": "skipped", "wifi_status": "skipped",'
        ' "gateway_details": "Gateway check skipped."}'
    ),
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
  """Mock the gateway_specialist_agent_as_a_tool (agent-as-a-tool).

  Faking at this boundary short-circuits the gateway_specialist_agent
  sub-agent (and its RDK device diagnostics), which tool-fake mode cannot
  reach. Returns the sub-agent's diagnostic-summary string under the "response"
  key, which the orchestrator after_tool_callback parses for gateway_status.

  Mode is read from the "gateway_status" param in the mock_config_string query
  string (e.g. "gateway_status=reboot&network_status=healthy"); falls back to
  context.state["mock_gateway_mode"], else _DEFAULT_MOCK_MODE. Supported modes:
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
  sm = state.get("sm")
  if sm and isinstance(sm, dict):
    # Log state variable gateway_status
    entry = {
        "src": "gateway_specialist_mock",
        "tag": "gateway_specialist_state_inspect",
        "level": "INFO",
        "data": {
            "gateway_status": str(state.get("gateway_status")),
            "mock_config_string": str(state.get("mock_config_string")),
        }
    }
    sm.setdefault("_log", []).append(entry)
    state["sm"] = sm

  pre_gateway = str(state.get("gateway_status") or "").strip()
  if pre_gateway and pre_gateway != "PENDING_BACKEND_RESULT":
    candidate = pre_gateway.lower()
    candidate = _MODE_ALIASES.get(candidate, candidate)
    if candidate in _VALID_MOCK_MODES:
      if sm and isinstance(sm, dict):
        entry = {
            "src": "gateway_specialist_mock",
            "tag": "gateway_specialist_preserved",
            "level": "INFO",
            "data": {"candidate": candidate}
        }
        sm.setdefault("_log", []).append(entry)
        state["sm"] = sm
      print(f"[mock gateway_specialist] Using pre-populated state gateway_status: {candidate}")
      return {"response": _REPORTS[candidate]}

  # Resolve the mock mode (mirrors the other configurable mocks).
  try:
    mode = None
    mock_config_string = state.get("mock_config_string") or ""
    if mock_config_string:
      gateway_status_value = _parse_query_param(mock_config_string, "gateway_status")
      if gateway_status_value:
        candidate = gateway_status_value.lower()
        candidate = _MODE_ALIASES.get(candidate, candidate)
        if candidate in _VALID_MOCK_MODES:
          mode = candidate
        else:
          print(
              "[mock gateway_specialist_agent_as_a_tool] Ignoring unsupported"
              f" gateway_status={gateway_status_value!r}; valid values:"
              f" {list(_VALID_MOCK_MODES)}"
          )
    if mode is None:
      mode = str(state.get("mock_gateway_mode") or _DEFAULT_MOCK_MODE).lower()
      mode = _MODE_ALIASES.get(mode, mode)
      if mode not in _VALID_MOCK_MODES:
        mode = _DEFAULT_MOCK_MODE
  except Exception:  # pylint: disable=broad-exception-caught
    mode = _DEFAULT_MOCK_MODE

  print(f"[mock gateway_specialist_agent_as_a_tool] mode: {mode}")

  return {"response": _REPORTS[mode]}
