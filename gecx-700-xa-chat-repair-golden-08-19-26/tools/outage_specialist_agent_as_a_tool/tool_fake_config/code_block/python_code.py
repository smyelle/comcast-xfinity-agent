from typing import Any, Optional

_DEFAULT_MOCK_MODE = "none"
_VALID_MOCK_MODES = ("active", "none", "error")


def _get_mock_config(state) -> dict:
  """Return mock_config_dict (platform already deserializes it to a dict)."""
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


# The orchestrator after_tool_callback reads tool_response["response"] as a
# string, extracts the JSON block, then reads report.outage_status /
# outage_detected / outage_message / customer_message / impacted_services. So
# the fake returns {"response": "<outage-summary JSON string>"} -- exactly the
# shape the real outage_specialist_agent emits. Faking at this boundary
# short-circuits the sub-agent and its live EDE call so the outage message is
# deterministic (instead of the real, per-account EDE text).
_ACTIVE_RESPONSE = (
    '{"outage_status": "active", "outage_detected": true, "outage_message":'
    ' "An outage in your area is affecting Internet and TV service. Our teams'
    ' are working to restore service as quickly as possible.",'
    ' "customer_message": "During an outage, we are unable to connect you with'
    ' a live agent, as any troubleshooting would not bring your services back'
    ' online.", "impacted_services": "Internet,TV"}'
)
_NONE_RESPONSE = (
    '{"outage_status": "none", "outage_detected": false, "outage_message": "",'
    ' "customer_message": "", "impacted_services": ""}'
)
_ERROR_RESPONSE = (
    '{"outage_status": "error", "outage_detected": false, "outage_message": "",'
    ' "customer_message": "", "impacted_services": ""}'
)


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
    # pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long
  """Mock the outage_specialist_agent_as_a_tool (agent-as-a-tool).

  Faking here short-circuits the outage_specialist_agent sub-agent (and its live
  EDE checkOutageEde call), which tool-fake mode cannot otherwise reach. Returns
  the sub-agent's outage-summary string under the "response" key, which the
  orchestrator after_tool_callback parses.

  Mode is read from the "outage_status" key in the mock_config_dict (e.g.
  {"outage_status": "active"}), else _DEFAULT_MOCK_MODE. Supported modes:
      - "active": neighborhood outage detected
      - "none":   no outage
      - "error":  outage check failed
  """

  input = input or {}
  state = callback_context.state

  # Resolve the mock mode from the unified mock_config_dict (key "outage_status").
  # Falls back to this tool's default scenario when the key is absent.
  try:
    cfg = _get_mock_config(state)
    candidate = str(
        cfg.get("outage_status")
        or _DEFAULT_MOCK_MODE
    ).lower()
    if candidate in _VALID_MOCK_MODES:
      mode = candidate
    else:
      print(
          "[mock outage_specialist_agent_as_a_tool] Ignoring unsupported"
          f" outage_status={candidate!r}; valid values: {list(_VALID_MOCK_MODES)}"
      )
      mode = _DEFAULT_MOCK_MODE
  except Exception:  # pylint: disable=broad-exception-caught
    mode = _DEFAULT_MOCK_MODE

  print(f"[mock outage_specialist_agent_as_a_tool] mode: {mode}")

  if mode == "active":
    _outage_message = (
        "An outage in your area is affecting Internet and TV service. Our teams"
        " are working to restore service as quickly as possible."
    )
    _customer_message = (
        "During an outage, we are unable to connect you with a live agent, as"
        " any troubleshooting would not bring your services back online."
    )
    _impacted = "Internet,TV"
    _detected, _status = "true", "active"
  elif mode == "error":
    _outage_message = _customer_message = _impacted = ""
    _detected, _status = "false", "error"
  else:  # none
    _outage_message = _customer_message = _impacted = ""
    _detected, _status = "false", "none"

  # Persist to the real session state (callback_context.state) so the outage
  # verdict variables are populated deterministically. This replaces what the
  # real sub-agent's after_tool_callback would have written from the live EDE
  # response, keeping {outage_message}/{customer_message} stable across runs.
  try:
    state["outage_detected"] = _detected
    state["outage_message"] = _outage_message
    state["customer_message"] = _customer_message
    state["impacted_services"] = _impacted
    state["outage_status"] = _status
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[mock outage_specialist_agent_as_a_tool] Could not set state: {e}")

  if mode == "active":
    return {"response": _ACTIVE_RESPONSE}
  if mode == "error":
    return {"response": _ERROR_RESPONSE}
  return {"response": _NONE_RESPONSE}
