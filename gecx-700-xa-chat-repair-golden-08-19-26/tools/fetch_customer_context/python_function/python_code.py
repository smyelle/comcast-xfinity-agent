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

  convoy_api_server = str(context.state.get("convoy_api_server") or "").rstrip("/")
  if not convoy_api_server:
    print("[ERROR] [fetch_customer_context] convoy_api_server variable is missing from context state!")
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
      "agent_name": "gecx_repair_agent",
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
          f"[AUDIT] [API RESPONSE] [fetch_customer_context] <<<: {response.text}"
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

  # --- Non-2xx HTTP guard ---
  # Context Hub returning a non-success HTTP status (e.g. 4xx/5xx) is an UPSTREAM
  # ERROR, not a disconnected account. Detect it explicitly (the body is otherwise
  # parsed blindly) and return the standard error shape so the caller takes the
  # soft "couldn't get all the info" human-transfer path -- never the disconnected
  # or "no gateway" path. FAKE eval responses have no status_code, so this is inert
  # for goldens/sims.
  _status_code = getattr(response, "status_code", None)
  try:
    _status_code = int(_status_code) if _status_code is not None else None
  except (TypeError, ValueError):
    _status_code = None
  if _status_code is not None and not (200 <= _status_code < 300):
    print(
        "[fetch_customer_context] Non-success HTTP status from Context Hub:"
        f" {_status_code}. Treating as upstream error (NOT disconnected)."
    )
    try:
      context.state["customer_context_status"] = "empty"
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(f"[fetch_customer_context] Could not set non-2xx state: {e}")
    _audit_response = {
        # Same customer-facing handling as an empty context: a soft, account-info
        # human transfer. The raw HTTP status is kept ONLY in the internal 'error'
        # field for the human-agent handoff -- it is NEVER shown to the customer.
        "status": "empty_context",
        "customer_context_status": "empty",
        "customer_message": (
            "I'm unable to get information about your account right now. Let me"
            " connect you with someone who can help."
        ),
        "error": f"Context Hub returned HTTP {_status_code}",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [fetch_customer_context] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  # Extract cable modem MAC from the response
  cable_modem_mac = None
  data = {}
  account_status = "A"
  # Compact service-subscription flags from accountContext.services.
  xfinity_internet_subscribed = "false"
  xfinity_video_subscribed = "false"
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

    # --- Empty / unusable Context Hub payload guard ---
    # Context Hub occasionally returns HTTP 200 with an empty body ("{}") or a body
    # that carries NEITHER accountContext NOR deviceContext. That is NOT a
    # disconnected account -- a missing 'subscribed'/'status' field must never be
    # read as "disconnected". Flag this distinctly (customer_context_status="empty")
    # so the caller transfers to a human with a soft "can't access your account
    # right now" message instead of emitting a false "Account is disconnected"
    # notification. This is intentionally narrow: it fires ONLY when the payload has
    # no usable account/device context at all, so the legitimate disconnected-account
    # detection below is preserved unchanged.
    context_is_empty = (
        not isinstance(data, dict)
        or len(data) == 0
        or (not data.get("accountContext") and not data.get("deviceContext"))
    )
    if context_is_empty:
      print(
          "[fetch_customer_context] EMPTY/unusable Context Hub payload (HTTP 200 but"
          " no accountContext/deviceContext). NOT treating as disconnected."
      )
      try:
        context.state["customer_context_status"] = "empty"
        context.state["accountNumber"] = account_number
        context.state["account_id"] = account_number
      except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"[fetch_customer_context] Could not set empty-context state: {e}")
      _audit_response = {
          "status": "empty_context",
          "customer_context_status": "empty",
          "customer_message": (
              "I'm unable to get information about your account right now. Let me"
              " connect you with someone who can help."
          ),
          "agent_action": "transfer_to_human",
      }
      print(
          "[AUDIT] [fetch_customer_context] <<< Response Payload:",
          f" {_audit_response}",
      )
      return _audit_response

    if isinstance(data, dict):
      account_ctx = data.get("accountContext", {})
      account_status = account_ctx.get("status", "A")

      # Extract compact subscription flags from accountContext.services.
      services = account_ctx.get("services", {}) if isinstance(account_ctx, dict) else {}
      internet_service = services.get("INTERNET", {}) if isinstance(services, dict) else {}
      video_service = services.get("VIDEO", {}) if isinstance(services, dict) else {}

      internet_service_status = str(internet_service.get("status", "") or "").upper()
      _internet_subscribed = internet_service.get("subscribed")
      if _internet_subscribed is True:
        xfinity_internet_subscribed = "true"

      _video_subscribed = video_service.get("subscribed")
      if _video_subscribed is True:
        xfinity_video_subscribed = "true"

      # Resilient standing override: only explicit disconnected signals force "D".
      if internet_service_status == "DISCONNECTED" or _internet_subscribed is False:
        print("[fetch_customer_context] Internet service status is DISCONNECTED. Overriding account_status to 'D'!")
        account_status = "D"

    # Resolve the gateway/cable-modem MAC with fallbacks. Context Hub sometimes omits
    # deviceContext.equipment[] (e.g. the basicInfo task fails), so _extract_cable_modem_mac
    # also looks under deviceForActivation (IpGateway) and statesConnectionLossResponse.
    cable_modem_mac = _extract_cable_modem_mac(data)
    if cable_modem_mac:
      print(f"[fetch_customer_context] Resolved cable_modem_mac via fallbacks: {cable_modem_mac}")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"Error extracting MAC: {e}")
    traceback.print_exc()

  print(f"Extracted cable_modem_mac: {cable_modem_mac}")
  print(f"Extracted account_status: {account_status}")

  # Extract video device MAC and Wi-Fi extender/pod details.
  # These are stored in state for upcoming video-troubleshooting and
  # extender-aware Wi-Fi features. Kept in a separate try block so a parsing
  # issue here never affects the cable_modem_mac / account_status extraction.
  video_device_mac = None
  wifi_extenders = []
  wifi_extender_count = 0
  try:
    device_context = data.get("deviceContext", {}) if isinstance(data, dict) else {}

    # --- Video set-top box MAC ---
    # In deviceContext.equipment[], a video box carries "VIDEO" in its
    # services list and exposes the box MAC via setTopBoxMacAddress
    # (unitAddress is a fallback for the same value).
    for eq in device_context.get("equipment", []) or []:
      services = [str(s).upper() for s in (eq.get("services") or [])]
      if "VIDEO" in services:
        stb_mac = eq.get("setTopBoxMacAddress") or eq.get("unitAddress")
        if stb_mac:
          video_device_mac = stb_mac.lower()
          break

    # Fallback: Context Hub may omit equipment[] but still list the set-top box under
    # deviceForActivation as a QamIpStb/IpStb. Use its cmMacAddress for video troubleshooting.
    if not video_device_mac:
      for dev in (device_context.get("deviceForActivation", {}) or {}).get("devices", []) or []:
        if str(dev.get("deviceType", "")).upper() in ("QAMIPSTB", "IPSTB", "STB"):
          stb_mac = dev.get("cmMacAddress")
          if stb_mac:
            video_device_mac = str(stb_mac).lower()
            break

    # --- Wi-Fi extenders / pods ---
    # In deviceContext.device[], pods are equipmentType == "WifiExtender".
    # Capture each pod's MAC, model and status (ACTIVE/INACTIVE).
    for dev in device_context.get("device", []) or []:
      if str(dev.get("equipmentType", "")).lower() == "wifiextender":
        wifi_extenders.append({
            "model": dev.get("model"),
            "macAddress": (dev.get("macAddress") or "").lower(),
            "status": dev.get("status"),
            "equipmentType": dev.get("equipmentType"),
        })

    # Prefer the API-provided count; fall back to what we actually parsed.
    wifi_extender_count = device_context.get("extenderDeviceCount") or len(wifi_extenders)
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"Error extracting video device / Wi-Fi extenders: {e}")
    traceback.print_exc()

  has_video_device = bool(video_device_mac)
  has_wifi_extenders = len(wifi_extenders) > 0
  wifi_extenders_state = {
      "count": wifi_extender_count,
      "items": wifi_extenders,
  }
  print(f"Extracted video_device_mac: {video_device_mac}")
  print(
      f"Extracted wifi_extenders (count={wifi_extender_count}): {wifi_extenders}"
  )

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

    # Video set-top box MAC (for upcoming video-troubleshooting features).
    if video_device_mac:
      context.state["video_device_mac"] = video_device_mac
      print(f"Set context.state['video_device_mac'] = {video_device_mac}")
    else:
      print("No video device MAC found in context. Preserving existing state.")
    context.state["has_video_device"] = "true" if has_video_device else "false"

    # Compact service-subscription flags for deterministic gating/routing.
    context.state["xfinityInternetSubscribed"] = xfinity_internet_subscribed
    context.state["xfinityVideoSubscribed"] = xfinity_video_subscribed

    # Wi-Fi extenders / pods (for extender-aware Wi-Fi troubleshooting).
    # has_wifi_extenders is stored as the lowercase string "true"/"false" so it
    # matches the agent instruction's `is "true"` flag convention.
    context.state["wifi_extenders"] = wifi_extenders_state
    context.state["wifi_extender_count"] = str(wifi_extender_count)
    context.state["has_wifi_extenders"] = "true" if has_wifi_extenders else "false"
    print(
        "Set context.state['wifi_extenders'] (count="
        f"{wifi_extender_count}, has_wifi_extenders={has_wifi_extenders})"
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
    # We reached here with a usable Context Hub payload, so mark context as OK
    # (distinguishes a real account read from the empty-context guard above).
    context.state["customer_context_status"] = "ok"
    print(
        "Set context.state['account_status'] ="
        f" {context.state['account_status']}"
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"Could not set state variable: {e}")

  _audit_response = {
      "cable_modem_mac": cable_modem_mac or "NOT_FOUND",
      "account_status": account_status,
      "customer_context_status": "ok",
      "video_device_mac": video_device_mac or "NOT_FOUND",
      "has_video_device": has_video_device,
      "xfinityInternetSubscribed": (xfinity_internet_subscribed == "true"),
      "xfinityVideoSubscribed": (xfinity_video_subscribed == "true"),
      "wifi_extenders": wifi_extenders,
      "wifi_extender_count": wifi_extender_count,
      "has_wifi_extenders": has_wifi_extenders,
      "status": "success" if cable_modem_mac else "no_modem_found",
  }
  print(
      "[AUDIT] [fetch_customer_context] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response


def _extract_cable_modem_mac(data: Any) -> Any:
  """Resolve the gateway / cable-modem MAC (lowercased) from a Context Hub response.

  Context Hub does NOT always populate deviceContext.equipment[] -- e.g. when the
  basicInfo task fails it returns a message like "null value for (non-nullable)
  List<Equipment> at BasicInfoContext.equipment" and an empty/absent equipment list.
  In that case the gateway MAC is still available elsewhere in deviceContext, so we
  try several sources in priority order (gateway-only, never a set-top box):

    1. deviceContext.equipment[]     -- an ACTIVE, non-STB device's `macaddress` (original path).
    2. deviceContext.deviceForActivation.devices[] -- the `IpGateway` entry's `cmMacAddress`.
    3. deviceContext.statesConnectionLossResponse[] -- a lone `CM_MAC_ADDRESS` entityId, ONLY
       when exactly one distinct MAC is present (so a set-top box is never mistaken for the gateway).

  Returns the lowercased MAC string, or None if none can be resolved.
  """
  if not isinstance(data, dict):
    return None
  device_ctx = data.get("deviceContext", {}) or {}

  # 1) equipment[] (ACTIVE, non-STB)
  for device in device_ctx.get("equipment", []) or []:
    if (
        device.get("deviceStatus") == "ACTIVE"
        and device.get("itemTypeCode") != "STB"
    ):
      mac = device.get("macaddress")
      if mac:
        return str(mac).lower()

  # 2) deviceForActivation -> IpGateway.cmMacAddress
  device_for_activation = device_ctx.get("deviceForActivation", {}) or {}
  for dev in device_for_activation.get("devices", []) or []:
    if str(dev.get("deviceType", "")).upper() == "IPGATEWAY":
      mac = dev.get("cmMacAddress")
      if mac:
        return str(mac).lower()

  # 3) statesConnectionLossResponse[] -- only a single, unambiguous CM_MAC_ADDRESS
  distinct_macs = []
  for loss_state in device_ctx.get("statesConnectionLossResponse", []) or []:
    if (
        str(loss_state.get("entityType", "")).upper() == "CM_MAC_ADDRESS"
        and loss_state.get("entityId")
    ):
      mac = str(loss_state["entityId"]).lower()
      if mac not in distinct_macs:
        distinct_macs.append(mac)
  if len(distinct_macs) == 1:
    return distinct_macs[0]

  return None
