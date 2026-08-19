from typing import Any, Optional

_DEFAULT_MOCK_MODE = "healthy"
_VALID_MOCK_MODES = ("healthy", "impaired", "error")

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


def _get_mock_config(state) -> dict:
  """Return mock_config_dict (platform already deserializes it to a dict)."""
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
    # pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long
  """Mock the network_specialist_agent_as_a_tool (agent-as-a-tool).

  Faking at this boundary short-circuits the network_specialist_agent
  sub-agent (and its OpenAPI sendA2AMessageViaAuthProxy call), which tool-fake
  mode cannot reach. Returns the sub-agent's diagnostic-summary string under
  the "response" key, which the orchestrator after_tool_callback parses.

  Mode is read from the "network_status" key in the mock_config_dict (e.g.
  {"network_status": "impaired", "outage_status": "none"}), else
  _DEFAULT_MOCK_MODE. Supported modes:
      - "healthy":  Technician Type "No Technician Required"
      - "impaired": Technician Type "Network Tech" -> network_status impaired
      - "error":    network_status error

  Aliases: "clear"/"none"/"ok" -> "healthy"; "impairment"/"network_tech" ->
  "impaired".
  """

  input = input or {}
  state = callback_context.state

  # Resolve the mock mode from the unified mock_config_dict (key "network_status").
  # Falls back to this tool's default scenario when the key is absent.
  try:
    cfg = _get_mock_config(state)
    candidate = str(
        cfg.get("network_status")
        or _DEFAULT_MOCK_MODE
    ).lower()
    candidate = _MODE_ALIASES.get(candidate, candidate)
    if candidate in _VALID_MOCK_MODES:
      mode = candidate
    else:
      print(
          "[mock network_specialist_agent_as_a_tool] Ignoring unsupported"
          f" network_status={candidate!r}; valid values: {list(_VALID_MOCK_MODES)}"
      )
      mode = _DEFAULT_MOCK_MODE
  except Exception:  # pylint: disable=broad-exception-caught
    mode = _DEFAULT_MOCK_MODE

  print(f"[mock network_specialist_agent_as_a_tool] mode: {mode}")

  if mode == "impaired":
    report = _IMPAIRED_REPORT
  elif mode == "error":
    report = _ERROR_REPORT
  else:
    report = _HEALTHY_REPORT

  return {"response": report}
