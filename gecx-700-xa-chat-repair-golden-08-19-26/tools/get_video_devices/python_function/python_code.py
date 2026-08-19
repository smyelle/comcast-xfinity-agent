# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

import json
import time
import traceback
from typing import Any


def get_video_devices(account_number: str = "") -> dict[str, Any]:
  """Fetch the account's video device inventory via the auth-proxy toolset.

  Args:
      account_number: Customer billing account number. Falls back to session
          account context when empty.

  Returns:
      dict: { video_devices: [...], device_count: n, status }.
  """
  if not account_number:
    account_number = context.state.get("accountNumber") or context.state.get("account_id", "")
  if not account_number:
    return {
        "status": "error",
        "error": "account_number is required.",
        "agent_action": "Fetch customer context to load the account details first.",
    }

  convoy_device_server = str(context.state.get("convoy_device_server") or "https://ce-convoy-prod.codebig2.net").rstrip("/")
  x_url = (
      f"{convoy_device_server}/account/{account_number}/device/context"
      "?contextLight=true&cacheRefresh=DEFAULT&friendlyName=true&status=false"
  )
  tool_args = {
      "x-url": x_url,
      "agent_name": "gecx_repair_agent",
      "x-auth": "CONVOY-CIMA-XAXLR",
      "x-scope": "urn:convoy:security:token:client-credentials urn:convoy:session#read",
      "x-cache-refresh": "FORCE-REFRESH",
      "x-flow-trace-id": context.session_id,
  }
  print(f"[AUDIT] [get_video_devices] >>> {tool_args}")

  try:
    _t0 = time.time()
    response = tools.get_video_devices_ViaAuthProxy_getVideoDevicesViaAuthProxy(tool_args)
    print(f"[AUDIT] [LATENCY] [get_video_devices] {int((time.time()-_t0)*1000)} ms")
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [get_video_devices] <<<: {response.text}")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[get_video_devices] API call failed: {e}")
    return {
        "status": "error",
        "video_devices": [],
        "device_count": 0,
        "error": f"Failed to fetch video devices: {str(e)}",
        "agent_action": "transfer_to_human",
    }

  video_devices = []
  try:
    data = response
    if hasattr(response, "body"):
      data = response.body
    elif hasattr(response, "text"):
      data = json.loads(response.text)
    if isinstance(data, str):
      data = json.loads(data)
    if isinstance(data, dict) and "result" in data:
      data = data["result"]

    device_contexts = (data or {}).get("deviceContexts", {}) if isinstance(data, dict) else {}
    for dev in (device_contexts.get("video", {}) or {}).get("device", []) or []:
      video_devices.append({
          "friendlyName": dev.get("friendlyName", ""),
          "mac": (dev.get("mac") or "").lower(),
          "activationStatus": dev.get("activationStatus", ""),
          "x1": bool(dev.get("x1", False)),
          "model": dev.get("model", ""),
      })
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[get_video_devices] Parse error: {e}")
    traceback.print_exc()

  try:
    context.state["video_devices"] = video_devices
    context.state["video_status"] = "devices_found" if video_devices else "no_devices"
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[get_video_devices] Could not set state: {e}")

  _resp = {
      "status": "success" if video_devices else "no_devices",
      "video_devices": video_devices,
      "device_count": len(video_devices),
  }
  print(f"[AUDIT] [get_video_devices] <<< {_resp}")
  return _resp

