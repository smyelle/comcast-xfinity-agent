from typing import Any, Optional

_DEFAULT_MOCK_MODE = "none"
_VALID_MOCK_MODES = ("active", "none", "error")


def _get_mock_config(state) -> dict:
  """Return mock_config_dict (platform already deserializes it to a dict)."""
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}

def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
    # pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long
  """Mock EDE outage check. Returns success/error maps matching the real tool.

  Mode is read from the "outage_status" key in the mock_config_dict, else
  falls back to _DEFAULT_MOCK_MODE. Supported modes:
      - "active": outage detected, success shape
      - "none":   no outage, success shape
      - "error":  error shape with agent_action

  Args:
      account_number: The customer billing account number.

  Returns:
      dict matching the real check_outage shapes.
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
    account_number = context.state.get("accountNumber") or context.state.get(
        "account_id", ""
    )
  if not account_number:
    return {
        "status": "error",
        "outage_detected": False,
        "error": "account_number is required.",
    }

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
          "[mock check_outage] Ignoring unsupported outage_status="
          f"{candidate!r}; valid values: {list(_VALID_MOCK_MODES)}"
      )
      mode = _DEFAULT_MOCK_MODE
  except Exception:  # pylint: disable=broad-exception-caught
    mode = _DEFAULT_MOCK_MODE

  print(f"[mock check_outage] account_number: {account_number}, mode: {mode}")

  # ---- ERROR SHAPE (matches tool-failure / parse-error paths) ----
  if mode == "error":
    # Real tool does NOT write outage_status to state on error paths.
    return {
        "status": "error",
        "outage_detected": False,
        "error": "Mock EDE failure: simulated tool/parse error.",
        "agent_action": "transfer_to_human",
    }

  # ---- SUCCESS SHAPES ----
  if mode == "active":
    outage_detected = True
    outage_message = (
        "An outage in your area is affecting Internet and TV service. Our teams"
        " are working to restore service as quickly as possible."
    )
    customer_message = (
        "During an outage, we are unable to connect you with a live agent, as"
        " any troubleshooting would not bring your services back online."
    )
    impacted_services = ["Internet", "TV"]
  else:  # "none"
    outage_detected = False
    outage_message = ""
    customer_message = ""
    impacted_services = []

  # Write the same state variables the real tool sets on success.
  try:
    context.state["outage_detected"] = "true" if outage_detected else "false"
    context.state["outage_message"] = outage_message
    context.state["customer_message"] = customer_message
    context.state["impacted_services"] = (
        ",".join(impacted_services) if impacted_services else ""
    )
    context.state["outage_status"] = "active" if outage_detected else "none"
    print("[mock check_outage] Set context.state variables successfully")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[mock check_outage] Could not set state variables: {e}")

  return {
      "status": "success",
      "outage_status": "active" if outage_detected else "none",
      "outage_detected": outage_detected,
      "outage_message": outage_message,
      "customer_message": customer_message,
      "impacted_services": impacted_services,
  }