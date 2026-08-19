# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""perform_speed_test -- wrapper for the xfi_speed_test_ViaAuthProxy OpenAPI toolset.

Starts an asynchronous Xfinity gateway speed test via the xFi Speed Platform through
the Apigee auth-proxy, caches the returned execution id in session state, and returns
a compact "started" result. The follow-up turn must poll get_async_speed_test_result
before any speed numbers or recommendations are shown to the customer.
"""

import json
import time
from typing import Any


def perform_speed_test(device_id: str = "") -> dict[str, Any]:
  """Starts an asynchronous gateway speed test and stores its execution id.

  Args:
      device_id: The gateway's MAC address. Optional -- defaults to the session's
        cable_modem_mac.

  Returns:
      dict: A compact async start status, or an error dict.
  """
  # Extract the gateway MAC if not provided.
  if not device_id:
    device_id = context.state.get("cable_modem_mac") or ""

  if not device_id or device_id == "NOT_FOUND":
    return {
        "status": "error",
        "error": "device_id (cable_modem_mac) is required to run a speed test.",
        "agent_action": (
            "Let the customer know a speed test can't be run because no gateway"
            " was found on the account, and offer to connect them with someone"
            " who can help."
        ),
    }

  speed_test_server = str(context.state.get("speed_test_server") or "").rstrip("/")
  if not speed_test_server:
    print("[ERROR] [perform_speed_test] speed_test_server variable is missing from context state!")
    return {
        "status": "error",
        "error": "Missing required server configuration: 'speed_test_server'",
        "agent_action": "transfer_to_human",
    }

  account_number = (
      context.state.get("accountNumber")
      or context.state.get("account_id")
      or ""
  )

  # The xFi Speed Platform expects the account's XBO id (NOT the billing account
  # number) as the 'account-id' header. It is normally resolved + cached on the
  # confirmation turn by prepare_speed_test; fall back to an on-demand lookup here
  # so a direct route still works. If there is no XBO id for this account, a speed
  # test cannot be run -- return a clean, distinct error (never send the wrong id).
  xbo_id = str(context.state.get("xbo_id") or "").strip()
  if not xbo_id:
    xbo_id = _fetch_xbo_id(account_number)
    if xbo_id:
      context.state["xbo_id"] = xbo_id
  if not xbo_id:
    print("[perform_speed_test] No xbo_id available; cannot run speed test.")
    return {
        "status": "error",
        "reason": "no_xbo",
        "error": "No XBO id available for this account; speed test cannot be run.",
        "agent_action": (
            "Tell the customer a speed test isn't available on their account and offer"
            " to continue with other troubleshooting or connect them with someone who"
            " can help. Do NOT suggest trying again."
        ),
    }

  target_url = f"{speed_test_server}/xfispeedtest/api/gateway/speed_test/{device_id}"

  tool_args = {
      "accept": "application/json",
      "agent_name": "gecx_repair_agent",
      "account-id": xbo_id,
      "session-id": context.session_id,
      "x-auth": "ST-SAT-XAXLR",
      "x-scope": "x1:xfi-speed-platform:gateway",
      "x-url": target_url,
      "x-flow-trace-id": context.session_id,
  }

  _audit_request = {"device_id": device_id, "x-url": target_url}
  print("[AUDIT] [perform_speed_test] >>> Request Payload:", f" {_audit_request}")

  try:
    _api_start = time.time()
    response = tools.xfi_speed_test_ViaAuthProxy_startAsyncSpeedTest(tool_args)
    _api_latency_ms = int((time.time() - _api_start) * 1000)
    print(f"[AUDIT] [LATENCY] [perform_speed_test] API took: {_api_latency_ms} ms")
    if hasattr(response, "status_code"):
      print(
          f"[AUDIT] [HTTP STATUS] [perform_speed_test] : {response.status_code} -"
          f" {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [perform_speed_test] <<<: {response.text}")

    data = _coerce_to_dict(response)
    if not isinstance(data, dict) or not data:
      return {
          "status": "error",
          "error": "Speed test start returned no usable result.",
          "agent_action": (
              "Tell the customer the speed test couldn't be started right now"
              " and offer to try again or connect them with someone who can help."
          ),
      }

    execution_id = _extract_execution_id(data)
    if not execution_id:
      result = {
          "status": "error",
          "reason": "missing_execution_id",
          "error": "Async speed-test start response did not include executionId.",
          "agent_action": (
              "Tell the customer the speed test couldn't be started right now and"
              " offer to try again or connect them with someone who can help."
          ),
      }
      print("[AUDIT] [perform_speed_test] <<< Response Payload:", f" {result}")
      return result

    context.state["async_speed_test_execution_id"] = execution_id
    context.state["speed_test_async_result_pending"] = "true"
    result = {
        "status": "success",
        "result_available": False,
        "async_started": True,
        "execution_id_present": True,
        "pollingIntervalInSeconds": _safe_number(data.get("pollingIntervalInSeconds")),
        "suggestedTotalPollingDurationInSeconds": _safe_number(
            data.get("suggestedTotalPollingDurationInSeconds")
        ),
        "agent_action": (
            "Tell the customer the speed test has started and may take a minute or"
            " so to complete. Ask them to reply in about a minute so you can check"
            " the result. Do not expose the execution id."
        ),
    }
    print("[AUDIT] [perform_speed_test] <<< Response Payload:", f" {result}")
    return result

  except Exception as e:
    _audit_response = {
        "status": "error",
        "error": f"Failed to start speed test: {str(e)}",
        "agent_action": (
            "Tell the customer the speed test couldn't be started right now and"
            " offer to try again or connect them with someone who can help."
        ),
    }
    print("[AUDIT] [perform_speed_test] <<< Response Payload:", f" {_audit_response}")
    return _audit_response


def _coerce_to_dict(response: Any) -> Any:
  """Best-effort convert a CXAS ExternalResponse (or str) to a dict."""
  if response is None:
    return {}
  if isinstance(response, dict):
    return response
  if hasattr(response, "body") and isinstance(response.body, dict):
    return response.body
  if hasattr(response, "json"):
    try:
      return response.json()
    except Exception:
      pass
  if hasattr(response, "text") and response.text:
    try:
      return json.loads(response.text)
    except Exception:
      return {}
  if isinstance(response, str):
    try:
      return json.loads(response)
    except Exception:
      return {}
  return {}


def _extract_execution_id(data: Any) -> str:
  if isinstance(data, dict):
    for key in ("executionId", "execution_id", "id"):
      value = str(data.get(key) or "").strip()
      if value:
        return value
    for key in ("result", "data"):
      nested = data.get(key)
      if isinstance(nested, dict):
        value = _extract_execution_id(nested)
        if value:
          return value
  return ""


def _safe_number(value: Any) -> Any:
  try:
    if value is None:
      return None
    number = float(value)
    return int(number) if number.is_integer() else number
  except (TypeError, ValueError):
    return None


def _fetch_xbo_id(account_number: str) -> str:
  """Fallback resolver for the Titan XBO id (normally prepare_speed_test caches it
  on the confirmation turn). Returns the XBO id string, or '' when unavailable."""
  account_number = str(account_number or "").strip()
  if not account_number:
    return ""
  titan_server = str(context.state.get("titan_server") or "").rstrip("/")
  if not titan_server:
    print("[perform_speed_test] titan_server variable is missing from context state!")
    return ""
  target_url = f"{titan_server}/accounts?billingAccountId={account_number}"
  tool_args = {
      "accept": "application/json",
      "content-type": "application/json",
      "agent_name": "gecx_repair_agent",
      "x-auth": "TITAN-SAT-XAXLR",
      "x-scope": "x1:xbo:titan:read",
      "x-url": target_url,
      "x-flow-trace-id": context.session_id,
  }
  try:
    response = tools.xbo_lookup_ViaAuthProxy_getXboByAccount(tool_args)
    records = response if isinstance(response, list) else _coerce_to_dict(response)
    if isinstance(records, dict):
      # Unwrap the list from a common envelope key ("result" is the CES OpenAPI
      # wrapper; Titan itself may also nest under "data").
      unwrapped = None
      for key in ("result", "data", "results", "accounts", "items"):
        val = records.get(key)
        if isinstance(val, list):
          unwrapped = val
          break
      records = unwrapped if unwrapped is not None else [records]
    for rec in records or []:
      if isinstance(rec, dict):
        xbo_id = str(rec.get("id") or "").strip()
        if xbo_id:
          return xbo_id
    return ""
  except Exception as e:
    print(f"[perform_speed_test] xbo fallback lookup failed: {e}")
    return ""


def _round1(value: Any) -> Any:
  try:
    return round(float(value), 1)
  except (TypeError, ValueError):
    return None


def _normalize_speed_test(data: dict[str, Any]) -> dict[str, Any]:
  """Turn the raw xFi Speed Platform payload into a small, grounded, summarizable dict.

  The overall verdict is the WORST of the download and upload contextual results:
  if either the download or the upload contextual result asks for a RESTART, the
  overall verdict is RESTART and we recommend restarting the gateway.
  """
  download = _round1(data.get("actualDownloadSpeed"))
  upload = _round1(data.get("actualUploadSpeed"))
  latency = _round1(data.get("latencyMs"))
  plan_download = _round1(data.get("planDownloadSpeed"))
  plan_upload = _round1(data.get("planUploadSpeed"))

  dl_ctx = data.get("gatewayContextualDownloadResult") or {}
  ul_ctx = data.get("gatewayContextualUploadResult") or {}
  overall_ctx = data.get("gatewayContextualResult") or {}

  def _pct(ctx, actual, plan):
    p = ctx.get("planSpeedPercent") if isinstance(ctx, dict) else None
    if p is not None:
      try:
        return int(round(float(p)))
      except (TypeError, ValueError):
        pass
    if actual is not None and plan:
      try:
        return int(round((float(actual) / float(plan)) * 100))
      except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None

  download_percent = _pct(dl_ctx, download, plan_download)
  upload_percent = _pct(ul_ctx, upload, plan_upload)

  download_result = str(dl_ctx.get("resultType") or "").upper() if isinstance(dl_ctx, dict) else ""
  upload_result = str(ul_ctx.get("resultType") or "").upper() if isinstance(ul_ctx, dict) else ""

  # Overall = worst of the two streams. RESTART (or any non-pass) wins over FULL_PASS.
  needs_restart = "RESTART" in (download_result, upload_result)
  stream_results = [r for r in (download_result, upload_result) if r]
  all_pass = bool(stream_results) and all(r == "FULL_PASS" for r in stream_results)

  if needs_restart:
    overall_result = "RESTART"
    recommendation = "restart_gateway"
    recommendation_steps = [
        "Restart your gateway to clear up the slower speed -- this usually helps.",
    ]
  elif all_pass:
    overall_result = "FULL_PASS"
    recommendation = "none"
    recommendation_steps = []
  else:
    # Unknown / mixed contextual verdicts -- fall back to the top-level result.
    overall_result = str(overall_ctx.get("resultType") or "").upper() or "UNKNOWN"
    if overall_result == "RESTART":
      recommendation = "restart_gateway"
      recommendation_steps = [
          "Restart your gateway to clear up the slower speed -- this usually helps.",
      ]
    else:
      recommendation = "none"
      recommendation_steps = []

  result_title = ""
  result_message = ""
  if isinstance(overall_ctx, dict):
    result_title = str(overall_ctx.get("title") or "")
    result_message = str(overall_ctx.get("message") or "")

  # Grounded plain-language summary (numbers come ONLY from the measured result).
  summary_parts = []
  if download is not None:
    if download_percent is not None:
      summary_parts.append(
          f"Download came in at {download} Mbps ({download_percent}% of your"
          f" {plan_download} Mbps plan)." if plan_download else
          f"Download came in at {download} Mbps."
      )
    else:
      summary_parts.append(f"Download came in at {download} Mbps.")
  if upload is not None:
    if upload_percent is not None:
      summary_parts.append(
          f"Upload came in at {upload} Mbps ({upload_percent}% of your"
          f" {plan_upload} Mbps plan)." if plan_upload else
          f"Upload came in at {upload} Mbps."
      )
    else:
      summary_parts.append(f"Upload came in at {upload} Mbps.")
  if latency is not None:
    summary_parts.append(f"Latency was {latency} ms.")
  summary = " ".join(summary_parts)

  return {
      "status": "success",
      "download_mbps": download,
      "upload_mbps": upload,
      "latency_ms": latency,
      "plan_download_mbps": plan_download,
      "plan_upload_mbps": plan_upload,
      "download_percent_of_plan": download_percent,
      "upload_percent_of_plan": upload_percent,
      "download_result": download_result,
      "upload_result": upload_result,
      "overall_result": overall_result,
      "result_title": result_title,
      "result_message": result_message,
      "recommendation": recommendation,
      "recommendation_steps": recommendation_steps,
      "summary": summary,
  }
