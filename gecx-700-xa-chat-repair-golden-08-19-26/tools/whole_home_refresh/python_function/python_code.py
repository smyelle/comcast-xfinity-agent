# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

import json
import time
import traceback
from typing import Any


def whole_home_refresh(account_number: str = "") -> dict[str, Any]:
  """Trigger a WHOLE_HOME_REFRESH of the customer's TV boxes via the auth-proxy toolset.

  Args:
      account_number: Customer billing account number. Falls back to session
          account context when empty.

  Returns:
      dict: { status: started|throttled|error, tracking_id, last_refresh_time, message }.
  """
  if not account_number:
    account_number = context.state.get("accountNumber") or context.state.get("account_id", "")
  if not account_number:
    return {
        "status": "error",
        "error": "account_number is required.",
        "agent_action": "Fetch customer context to load the account details first.",
    }

  selfhelp_server = str(context.state.get("selfhelp_server") or "https://csp-prod.codebig2.net").rstrip("/")
  tool_args = {
      "x-url": f"{selfhelp_server}/selfhelp/account/{account_number}/refresh?profile=WHOLE_HOME_REFRESH",
      "agent_name": "gecx_repair_agent",
      "accept": "application/json.v2+json",
      "client": "aiQ",
      "x-auth": "CSP-CIMA-XAXLR",
      "x-scope": "urn:csp:scope:self-help urn:csp:scope:self-help:systemrefresh urn:convoy:security:token:client-credentials",
      "x-flow-trace-id": context.session_id,
  }
  print(f"[AUDIT] [whole_home_refresh] >>> account={account_number}")

  try:
    _t0 = time.time()
    response = tools.whole_home_refresh_ViaAuthProxy_wholeHomeRefreshViaAuthProxy(tool_args)
    print(f"[AUDIT] [LATENCY] [whole_home_refresh] {int((time.time()-_t0)*1000)} ms")
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [whole_home_refresh] <<<: {response.text}")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[whole_home_refresh] API call failed: {e}")
    _set_status("refresh_error")
    return {"status": "error", "error": f"Failed to start whole-home refresh: {str(e)}", "agent_action": "transfer_to_human"}

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
    print(f"[whole_home_refresh] Parse error: {e}")
    traceback.print_exc()
    data = {}

  data = data or {}
  # Throttle: CSP returns SH400.59 "Too early to make another refresh request".
  if str(data.get("code", "")) == "SH400.59" or "too early" in str(data.get("message", "")).lower():
    _set_status("refresh_throttled")
    _resp = {"status": "throttled", "message": "too_early", "tracking_id": "", "last_refresh_time": ""}
  elif data.get("trackingId"):
    _set_status("refresh_started")
    _resp = {"status": "started", "tracking_id": data.get("trackingId", ""),
             "last_refresh_time": data.get("lastRefreshTime", ""), "message": ""}
  else:
    _set_status("refresh_error")
    _resp = {"status": "error", "message": "unexpected_response", "agent_action": "transfer_to_human"}
  print(f"[AUDIT] [whole_home_refresh] <<< {_resp}")
  return _resp


def _set_status(video_status: str) -> None:
  try:
    context.state["video_status"] = video_status
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[whole_home_refresh] Could not set state: {e}")

