# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

import json
import time
import traceback
from typing import Any


def restart_ip_stb(mac: str = "", tracking_id: str = "") -> dict[str, Any]:
  """Restart an IP set-top box by MAC via the auth-proxy toolset.

  Args:
      mac: The set-top box MAC. Falls back to {video_selected_device_mac} then
          {video_device_mac}.
      tracking_id: Optional tracking id. Falls back to {convoy_tracking_id} then
          the session id.

  Returns:
      dict: Normalized restart result.
  """
  if str(context.state.get("video_status", "")).lower() == "no_devices":
    return {
        "status": "warning",
        "status_code": "2",
        "message": "no_video_devices",
        "agent_action": (
            "No video devices were found on the account. Do NOT attempt a TV-box restart. "
            "If appropriate for the symptom, you may offer a whole-home refresh; "
            "otherwise connect the customer to a live agent."
        ),
    }

  mac = (mac or context.state.get("video_selected_device_mac") or context.state.get("video_device_mac") or "").strip()
  if not mac:
    return {
        "status": "error",
        "error": "mac is required.",
        "agent_action": "Identify the impacted TV box (get_video_devices) before restarting.",
    }
  tracking_id = tracking_id or context.state.get("convoy_tracking_id") or context.session_id or "gecx-repair"

  deviceservices_stb_server = str(context.state.get("deviceservices_stb_server") or "https://deviceservices-stb-reset-prod.codebig2.net").rstrip("/")
  tool_args = {
      "x-url": f"{deviceservices_stb_server}/v1.0/api/restartIpStb",
      "agent_name": "gecx_repair_agent",
      "x-auth": "DEVICE-SAT-XAXLR",
      "x-scope": "deviceservices:ipstb:reset deviceservices:stb:reset",
      "x-flow-trace-id": context.session_id,
      "mac": mac,
      "trackingId": tracking_id,
  }
  print(f"[AUDIT] [restart_ip_stb] >>> mac={mac} trackingId={tracking_id}")

  try:
    _t0 = time.time()
    response = tools.restart_ip_stb_ViaAuthProxy_restartIpStbViaAuthProxy(tool_args)
    print(f"[AUDIT] [LATENCY] [restart_ip_stb] {int((time.time()-_t0)*1000)} ms")
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [restart_ip_stb] <<<: {response.text}")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[restart_ip_stb] API call failed: {e}")
    _set_status("restart_error")
    return {"status": "error", "mac": mac, "error": f"Failed to restart set-top box: {str(e)}", "agent_action": "transfer_to_human"}

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
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[restart_ip_stb] Parse error: {e}")
    traceback.print_exc()
    data = {}

  status = str((data or {}).get("status", "")).lower()
  status_code = str((data or {}).get("statusCode", ""))
  # Never surface raw DS-#### provider codes to the customer.
  if status_code == "0" or status == "success":
    _set_status("restart_issued")
    norm = {"status": "success", "status_code": "0", "mac": mac, "action": "restartIpStb",
            "agent_action": "Tell the customer the box is restarting now (a few minutes, screen may go dark), then ask them to check if the picture comes back."}
  elif status_code == "2" or status == "warning":
    _set_status("restart_failed_offline")
    norm = {"status": "warning", "status_code": "2", "mac": mac, "action": "restartIpStb",
            "message": "device_not_found_or_unrecognized",
            "agent_action": "DO NOT say the restart was triggered or successful. The TV box did not respond on the network (it appears offline). Tell the customer the box looks offline, and ask them to check it is powered on and the coax/power cables are snug. If it stays offline, offer to connect them to a live agent."}
  else:  # failure / invalid mac / unknown
    _set_status("restart_error")
    norm = {"status": "failure", "status_code": status_code or "1", "mac": mac, "action": "restartIpStb",
            "message": "restart_failed",
            "agent_action": "DO NOT claim the restart was triggered or successful. The restart could not be completed. Apologize briefly and connect the customer to a live agent."}
  print(f"[AUDIT] [restart_ip_stb] <<< {norm}")
  return norm


def _set_status(video_status: str) -> None:
  try:
    context.state["video_status"] = video_status
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[restart_ip_stb] Could not set state: {e}")

