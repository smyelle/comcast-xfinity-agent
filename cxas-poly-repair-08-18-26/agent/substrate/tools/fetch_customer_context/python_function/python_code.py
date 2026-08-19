# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function."""

# pylint: disable=undefined-variable

import json
import time
import traceback
from typing import Any


def fetch_customer_context(account_number: str) -> dict[str, Any]:
  """Invokes the context_hub_api toolset (fetchCustomerContext operation).

  as a CXAS tool, passing account_number and standard event names.

  Uses tool_context.call_tool() so the platform handles auth, URL, and all
  OpenAPI spec defaults -- no direct HTTP calls are made here.

  Args:
      account_number: The customer account number to fetch context for.

  Returns:
      dict: The customer context response containing account, device,
          and interactions data.
  """
  if not account_number:
    return {
        "status": "error",
        "error": "account_number is required.",
        "agent_action": "Retrieve the account number from session variables.",
    }

  _audit_request = {
      "account_number": account_number,
  }
  print(
      "[AUDIT] [fetch_customer_context] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"account_number: {account_number}")

  convoy_api_server = str(context.state.get("convoy_api_server") or "").rstrip(
      "/"
  )
  if not convoy_api_server:
    print(
        "[ERROR] [fetch_customer_context] convoy_api_server variable is missing"
        " from context state!"
    )
    _audit_response = {
        "status": "error",
        "error": "Missing required server configuration: 'convoy_api_server'",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [fetch_customer_context] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  tool_args = {
      "x-url": f"{convoy_api_server}/customer/context",
      "x-auth": "GECXCONVOY-SAT-XAXLR",
      "x-scope": "ceconvoy:context_hub",
      "x-cache-refresh": "FORCE-REFRESH",
      "x-flow-trace-id": context.session_id,
      "eventNames": [
          "call.getContext.account",
          "call.getContext.device",
          "call.getContext.interactions",
      ],
      "data": {
          "metadata": {
              "eventId": "abcd-1234-efgh-5678",
              "source": "auth-service",
          },
          "messageContext": {
              "accountNumber": account_number,
          },
      },
  }

  # Record API call performance & raw payloads
  try:
    _api_start = time.time()
    response = tools.context_hub_api_fetchCustomerContext(tool_args)
    _api_end = time.time()
    _api_latency_ms = int((_api_end - _api_start) * 1000)
    print(
        "[AUDIT] [LATENCY] [fetch_customer_context] API took:"
        f" {_api_latency_ms} ms"
    )
    print(f"[AUDIT] [API REQUEST] [fetch_customer_context] >>>: {tool_args}")
    if hasattr(response, "status_code"):
      print(
          "[AUDIT] [HTTP STATUS] [fetch_customer_context] :"
          f" {response.status_code} - {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(
          "[AUDIT] [API RESPONSE] [fetch_customer_context] <<<:"
          f" {response.text}"
      )
    print(
        "tools.context_hub_api_fetchCustomerContext response type:"
        f" {type(response)}"
    )
    print(
        "tools.context_hub_api_fetchCustomerContext response dir:"
        f" {dir(response)}"
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[fetch_customer_context] API call failed: {e}")
    _audit_response = {
        "status": "error",
        "error": f"Failed to fetch customer context: {str(e)}",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [fetch_customer_context] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  # Extract cable modem MAC from the response
  cable_modem_mac = None
  try:
    data = response
    # Try various ways to get the dict from ExternalResponse
    if hasattr(response, "body"):
      data = response.body
    elif hasattr(response, "json"):
      data = response.json()
    elif hasattr(response, "text"):
      data = json.loads(response.text)
    elif hasattr(response, "__getitem__"):
      data = response

    # If data is a string, parse it
    if isinstance(data, str):
      data = json.loads(data)

    print(f"Parsed data type: {type(data)}")
    print(
        "Parsed data keys:"
        f" {data.keys() if isinstance(data, dict) else 'not a dict'}"
    )

    # The response may be wrapped in a "result" key
    if isinstance(data, dict) and "result" in data:
      data = data["result"]

    equipment = []
    account_status = "A"
    if isinstance(data, dict):
      equipment = data.get("deviceContext", {}).get("equipment", [])
      account_ctx = data.get("accountContext", {})
      account_status = account_ctx.get("status", "A")

      # Resilient check: Check linked accounts for HSD suspension/disconnection
      linked_accounts = account_ctx.get("linkedAccounts", [])
      for la in linked_accounts:
        la_acc = la.get("accountNumber", "")
        la_acc_clean = la_acc.replace(".", "").replace("*", "")
        if la_acc_clean and account_number.endswith(la_acc_clean):
          hsd_status = la.get("lineOfBusinessStatus", {}).get("hsdStatus")
          if hsd_status == "S":
            print(
                "[fetch_customer_context] HSD status is Suspended ('S') in"
                " linked account. Overriding account_status!"
            )
            account_status = "S"
            break
          elif hsd_status == "D":
            print(
                "[fetch_customer_context] HSD status is Disconnected ('D') in"
                " linked account. Overriding account_status!"
            )
            account_status = "D"
            break

      # Resilient double-gated standing check: if internet subscription status is explicitly disconnected, override to "D"
      internet_service = account_ctx.get("services", {}).get("INTERNET", {})
      if internet_service.get(
          "status"
      ) == "DISCONNECTED" or not internet_service.get("subscribed"):
        print(
            "[fetch_customer_context] Internet service status is DISCONNECTED."
            " Overriding account_status to 'D'!"
        )
        account_status = "D"

    for device in equipment:
      if (
          device.get("deviceStatus") == "ACTIVE"
          and device.get("itemTypeCode") != "STB"
      ):
        mac = device.get("macaddress")
        if mac:
          cable_modem_mac = mac.lower()
          break
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"Error extracting MAC: {e}")
    traceback.print_exc()

  print(f"Extracted cable_modem_mac: {cable_modem_mac}")
  print(f"Extracted account_status: {account_status}")

  # Set state variable if context is available
  try:
    if cable_modem_mac and cable_modem_mac != "NOT_FOUND":
      context.state["cable_modem_mac"] = cable_modem_mac
      print(f"Set context.state['cable_modem_mac'] = {cable_modem_mac}")
    else:
      print(
          "Context Hub did not return a valid MAC. Preserving existing state."
      )
    context.state["accountNumber"] = account_number
    context.state["account_id"] = account_number
    print(
        "Set context.state['accountNumber'] and ['account_id'] ="
        f" {account_number}"
    )

    # Map status char code to human-readable GECX status strings!
    status_mappings = {
        "A": "clear",
        "S": "suspended",
        "D": "disconnected",
        "C": "pending_activation",
    }
    mapped_status = status_mappings.get(account_status, "clear")
    context.state["account_status"] = mapped_status
    print(
        "Set context.state['account_status'] ="
        f" {context.state['account_status']}"
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"Could not set state variable: {e}")

  _audit_response = {
      "cable_modem_mac": cable_modem_mac or "NOT_FOUND",
      "account_status": mapped_status,
      "status": "success" if cable_modem_mac else "no_modem_found",
      "success": True if cable_modem_mac else False,
  }
  print(
      "[AUDIT] [fetch_customer_context] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response
