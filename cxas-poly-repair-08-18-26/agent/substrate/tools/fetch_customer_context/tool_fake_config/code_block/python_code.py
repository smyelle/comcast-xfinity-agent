_MOCK_MACS = {
  "8069100020079827": "AB:AB:B2:AA:11:01",
  "8069100230361049": "14:C0:3E:9C:46:23",
  "8344200010126021": "NOT_FOUND",
  "8535101580025909": "54:07:7D:0B:EF:B8",
  "8069100021415004": "AC:4C:A5:62:28:A8"
}
_DEFAULT_MOCK_MAC = "aa:bb:cc:dd:ee:ff"

_VALID_MOCK_MODES = ("suspended", "disconnected", "pending", "no_mac", "clear_with_mac", "error")


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
  input = input or {}
  state = callback_context.state

  account_number = str(
      input.get("account_number")
      or state.get("accountNumber")
      or state.get("account_id")
      or ""
  ).strip()
  if not account_number:
    return {
        "status": "error",
        "error": "account_number is required.",
        "agent_action": "Retrieve the account number from session variables.",
    }

  mode = None
  mock_config_string = state.get("mock_config_string") or ""
  if mock_config_string:
    context_status = _parse_query_param(mock_config_string, "context_status")
    if context_status is not None:
      mapping = {
          "suspended": "suspended",
          "disconnected": "disconnected",
          "pending": "pending",
          "no_mac": "no_mac",
          "clear": "clear_with_mac",
          "error": "error",
      }
      mapped_mode = mapping.get(context_status)
      if mapped_mode in _VALID_MOCK_MODES:
        mode = mapped_mode
      else:
        print(
            f"[mock fetch_customer_context] Warning: Unsupported context_status '{context_status}'"
        )

  _DEFAULT_MOCK_MODE = "clear_with_mac"
  if mode is None:
    mode = state.get("mock_context_mode") or _DEFAULT_MOCK_MODE

  mode = str(mode)
  print(f"[mock fetch_customer_context] account_number: {account_number}, mode: {mode}")

  if mode == "error":
    return {
        "status": "error",
        "error": "Mock Context Hub failure: simulated API error.",
        "agent_action": "transfer_to_human",
    }

  resolved_mac = _MOCK_MACS.get(account_number, _DEFAULT_MOCK_MAC)
  if mode == "suspended":
    account_status, cable_modem_mac = "S", resolved_mac
  elif mode == "disconnected":
    account_status, cable_modem_mac = "D", resolved_mac
  elif mode == "pending":
    account_status, cable_modem_mac = "C", resolved_mac
  elif mode == "no_mac":
    account_status, cable_modem_mac = "A", None
  else:  # "clear_with_mac"
    account_status, cable_modem_mac = "A", resolved_mac

  try:
    if cable_modem_mac and cable_modem_mac != "NOT_FOUND":
      state["cable_modem_mac"] = cable_modem_mac
    state["accountNumber"] = account_number
    state["account_id"] = account_number
    status_mappings = {"A": "clear", "S": "suspended", "D": "disconnected", "C": "pending_activation"}
    state["account_status"] = status_mappings.get(account_status, "clear")
    print("[mock fetch_customer_context] Set context.state variables")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[mock fetch_customer_context] Could not set state: {e}")

  return {
      "cable_modem_mac": cable_modem_mac or "NOT_FOUND",
      "account_status": account_status,
      "status": "success" if cable_modem_mac else "no_modem_found",
  }