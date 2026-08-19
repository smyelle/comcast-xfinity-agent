from typing import Any

_DEFAULT_MOCK_MODE = "healthy"
_VALID_MOCK_MODES = ("healthy", "impaired", "error", "skipped")

# Friendly aliases accepted for the network_status param.
_MODE_ALIASES = {
    "clear": "healthy",
    "none": "healthy",
    "ok": "healthy",
    "impairment": "impaired",
    "network_tech": "impaired",
}

# The orchestrator's after_tool_callback reads tool_response["response"] as a
# string, extracts the JSON block, then derives network_status from
# report.network_status / report.recommendation.technician_type. So the fake
# returns {"response": "<diagnostic-summary JSON string>"} -- exactly the shape
# the real network_specialist_agent emits.
_HEALTHY_REPORT = (
    '{"network_status": "healthy", "recommendation": {"technician_type":'
    ' "No Technician Required", "remediation": "No DOCSIS-related impairment'
    ' detected."}}'
)
_IMPAIRED_REPORT = (
    '{"network_status": "impaired", "recommendation": {"technician_type":'
    ' "Network Tech", "remediation": "Dispatch a network technician to inspect'
    ' the drop and tap."}}'
)
_ERROR_REPORT = (
    '{"network_status": "error", "recommendation": {"technician_type": "No'
    ' Technician Required", "remediation": "Network analysis could not be'
    ' completed."}}'
)
_SKIPPED_REPORT = (
    '{"network_status": "skipped", "recommendation": {"technician_type": "No'
    ' Technician Required", "remediation": "Network analysis skipped."}}'
)


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
  """Mock the network_specialist_agent_as_a_tool (agent-as-a-tool).

  Faking at this boundary short-circuits the network_specialist_agent
  sub-agent (and its OpenAPI sendA2AMessageViaAuthProxy call), which tool-fake
  mode cannot reach. Returns the sub-agent's diagnostic-summary string under
  the "response" key, which the orchestrator after_tool_callback parses.

  Mode is read from the "network_status" param in the mock_config_string query
  string (e.g. "network_status=impaired&outage_status=none"); falls back to
  context.state["mock_network_mode"], else _DEFAULT_MOCK_MODE. Supported modes:
      - "healthy":  Technician Type "No Technician Required"
      - "impaired": Technician Type "Network Tech" -> network_status impaired
      - "error":    network_status error

  Aliases: "clear"/"none"/"ok" -> "healthy"; "impairment"/"network_tech" ->
  "impaired".
  """

  input = input or {}
  state = callback_context.state
  sm = state.get("sm")
  if sm and isinstance(sm, dict):
    # Log state variable network_status
    entry = {
        "src": "network_specialist_mock",
        "tag": "network_specialist_state_inspect",
        "level": "INFO",
        "data": {
            "network_status": str(state.get("network_status")),
            "mock_config_string": str(state.get("mock_config_string")),
        }
    }
    sm.setdefault("_log", []).append(entry)
    state["sm"] = sm

  pre_network = str(state.get("network_status") or "").strip()
  if pre_network and pre_network != "PENDING_BACKEND_RESULT":
    candidate = pre_network.lower()
    candidate = _MODE_ALIASES.get(candidate, candidate)
    if candidate in _VALID_MOCK_MODES:
      if sm and isinstance(sm, dict):
        entry = {
            "src": "network_specialist_mock",
            "tag": "network_specialist_preserved",
            "level": "INFO",
            "data": {"candidate": candidate}
        }
        sm.setdefault("_log", []).append(entry)
        state["sm"] = sm
      print(f"[mock network_specialist] Using pre-populated state network_status: {candidate}")
      if candidate == "impaired":
        return {"response": _IMPAIRED_REPORT}
      elif candidate == "error":
        return {"response": _ERROR_REPORT}
      else:
        return {"response": _HEALTHY_REPORT}

  # Resolve the mock mode (mirrors the other configurable mocks).
  try:
    mode = None
    mock_config_string = state.get("mock_config_string") or ""
    if mock_config_string:
      network_status_value = _parse_query_param(mock_config_string, "network_status")
      if network_status_value:
        candidate = network_status_value.lower()
        candidate = _MODE_ALIASES.get(candidate, candidate)
        if candidate in _VALID_MOCK_MODES:
          mode = candidate
        else:
          print(
              "[mock network_specialist_agent_as_a_tool] Ignoring unsupported"
              f" network_status={network_status_value!r}; valid values:"
              f" {list(_VALID_MOCK_MODES)}"
          )
    if mode is None:
      mode = str(state.get("mock_network_mode") or _DEFAULT_MOCK_MODE).lower()
      mode = _MODE_ALIASES.get(mode, mode)
      if mode not in _VALID_MOCK_MODES:
        mode = _DEFAULT_MOCK_MODE
  except Exception:  # pylint: disable=broad-exception-caught
    mode = _DEFAULT_MOCK_MODE

  print(f"[mock network_specialist_agent_as_a_tool] mode: {mode}")

  if mode == "impaired":
    report = _IMPAIRED_REPORT
  elif mode == "error":
    report = _ERROR_REPORT
  elif mode == "skipped":
    report = _SKIPPED_REPORT
  else:
    report = _HEALTHY_REPORT

  return {"response": report}
