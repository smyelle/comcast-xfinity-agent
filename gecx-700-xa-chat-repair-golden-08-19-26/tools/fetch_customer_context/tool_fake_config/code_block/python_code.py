from typing import Any, Optional

_DEFAULT_MOCK_MODE = "clear_with_mac"

_MOCK_MAC = "aa:bb:cc:dd:ee:ff"


def _get_mock_config(state) -> dict:
  """Return mock_config_dict (platform already deserializes it to a dict)."""
  cfg = state.get("mock_config_dict") or {}
  return cfg if isinstance(cfg, dict) else {}


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
  input = input or {}
  state = callback_context.state

  account_number = (
      input.get("account_number")
      or state.get("accountNumber")
      or state.get("account_id")
      or ""
  )
  if not account_number:
    return {
        "status": "error",
        "error": "account_number is required.",
        "agent_action": "Retrieve the account number from session variables.",
    }

  # Resolve the mock mode from the unified mock_config_dict (key "account_status").
  # Falls back to this tool's default scenario when the key is absent.
  cfg = _get_mock_config(state)
  mode = str(
      cfg.get("account_status")
      or _DEFAULT_MOCK_MODE
  ).lower()
  print(f"[mock fetch_customer_context] account_number: {account_number}, mode: {mode}")

  if mode == "error":
    return {
        "status": "error",
        "error": "Mock Context Hub failure: simulated API error.",
        "agent_action": "transfer_to_human",
    }

  if mode == "suspended":
    account_status, cable_modem_mac = "S", _MOCK_MAC
  elif mode == "disconnected":
    account_status, cable_modem_mac = "D", _MOCK_MAC
  elif mode == "pending":
    account_status, cable_modem_mac = "C", _MOCK_MAC
  elif mode == "no_mac":
    account_status, cable_modem_mac = "A", None
  else:  # "clear_with_mac"
    account_status, cable_modem_mac = "A", _MOCK_MAC

  try:
    if cable_modem_mac and cable_modem_mac != "NOT_FOUND":
      state["cable_modem_mac"] = cable_modem_mac
    state["accountNumber"] = account_number
    state["account_id"] = account_number
    status_mappings = {"A": "clear", "S": "suspended", "D": "disconnected", "C": "pending_activation"}
    state["account_status"] = status_mappings.get(account_status, "clear")
    # --- Wi-Fi extenders / pods (eval stub) ---
    # The real fetch_customer_context reads pods from deviceContext; goldens/sims run this FAKE,
    # so mirror that here. Enable the pod (EXTENDER-FIRST) branch by adding e.g.
    # "has_wifi_extenders=true" (or "wifi_pods=2") to mock_config_string. Defaults to no pods so
    # existing non-pod Wi-Fi sims are unchanged.
    pods_raw = cfg.get("has_wifi_extenders")
    if pods_raw is None:
      pods_raw = cfg.get("wifi_pods")
    pods_str = str(pods_raw).lower() if pods_raw is not None else ""
    if pods_str in ("true", "yes"):
      pod_count = 1
    elif pods_str.isdigit():
      pod_count = int(pods_str)
    else:
      pod_count = 0
    wifi_extenders = [
        {"mac": f"po:d0:00:00:00:{i:02x}", "model": "XE2", "status": "ACTIVE"}
        for i in range(pod_count)
    ]
    state["wifi_extenders"] = {
        "count": pod_count,
        "items": wifi_extenders,
    }
    state["wifi_extender_count"] = str(pod_count)
    state["has_wifi_extenders"] = "true" if pod_count > 0 else "false"
    print("[mock fetch_customer_context] Set context.state variables "
          f"(has_wifi_extenders={state['has_wifi_extenders']}, pods={pod_count})")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[mock fetch_customer_context] Could not set state: {e}")

  return {
      "cable_modem_mac": cable_modem_mac or "NOT_FOUND",
      "account_status": account_status,
      "status": "success" if cable_modem_mac else "no_modem_found",
  }
