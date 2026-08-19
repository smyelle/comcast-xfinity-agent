# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""get_async_speed_test_result -- wrapper for async xFi gateway speed-test result polling."""

import json
import time
from typing import Any


def get_async_speed_test_result(execution_id: str = "") -> dict[str, Any]:
  """Fetch and normalize an asynchronous gateway speed-test result.

  Args:
      execution_id: Optional async execution id. Defaults to
        context.state["async_speed_test_execution_id"].

  Returns:
      A compact polling/result summary. The normalized payload is stored in
      context.state["async_speed_test_result"].
  """
  execution_id = str(
      execution_id or context.state.get("async_speed_test_execution_id") or ""
  ).strip()
  if not execution_id:
    return {
        "status": "error",
        "reason": "missing_execution_id",
        "error": "async_speed_test_execution_id is required to fetch async speed-test results.",
        "agent_action": "Call start_async_speed_test first, then poll for the result.",
    }

  xbo_id = str(context.state.get("xbo_id") or "").strip()
  if not xbo_id:
    return {
        "status": "error",
        "reason": "no_xbo",
        "error": "xbo_id is required to fetch async speed-test results.",
        "agent_action": (
            "Tell the customer the speed-test result is not available right now and"
            " offer to continue troubleshooting or connect them with someone who can help."
        ),
    }

  speed_test_server = str(context.state.get("speed_test_server") or "").rstrip("/")
  if not speed_test_server:
    print("[get_async_speed_test_result] speed_test_server variable is missing.")
    return {
        "status": "error",
        "reason": "missing_speed_test_server",
        "error": "Missing required server configuration: speed_test_server.",
        "agent_action": "transfer_to_human",
    }

  target_url = f"{speed_test_server}/xfispeedtest/api/gateway/result/{execution_id}"
  tool_args = {
      "accept": "application/json",
      "account-id": xbo_id,
      "content-type": "application/json",
      "x-auth": "ST-SAT-XAXLR",
      "x-scope": "x1:xfi-speed-platform:gateway",
      "x-url": target_url,
      "x-flow-trace-id": context.session_id,
  }

  print("[AUDIT] [get_async_speed_test_result] >>> Request Payload:", {
      "execution_id_present": bool(execution_id),
      "account_id_present": bool(xbo_id),
      "x-url": target_url,
  })

  try:
    api_start = time.time()
    response = tools.xfi_async_speed_result_auth_proxy_getAsyncSpeedTestResult(tool_args)
    api_latency_ms = int((time.time() - api_start) * 1000)
    print(f"[AUDIT] [LATENCY] [get_async_speed_test_result] API took: {api_latency_ms} ms")
    if hasattr(response, "status_code"):
      print(
          f"[AUDIT] [HTTP STATUS] [get_async_speed_test_result]: {response.status_code} -"
          f" {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [get_async_speed_test_result] <<<: {response.text}")

    data = _unwrap_result(_coerce_to_dict(response))
    if not isinstance(data, dict) or not data:
      return {
          "status": "error",
          "reason": "empty_result",
          "error": "Async speed-test result API returned no usable result.",
          "agent_action": (
              "Tell the customer the speed-test result couldn't be retrieved right now"
              " and offer to try again or connect them with someone who can help."
          ),
      }

    result_state = _normalize_result_state(data)
    completed_events = _completed_stream_events(data)
    missing_streams = [
        stream for stream in ("DOWNLOAD", "UPLOAD") if stream not in completed_events
    ]
    is_complete = _is_complete_result(data, result_state, completed_events)
    if is_complete and result_state == "UNKNOWN":
      result_state = "COMPLETE"
    if not is_complete:
      result = {
          "status": "success",
          "result_available": False,
          "result_state": result_state,
          "completed_streams": sorted(completed_events),
          "missing_streams": missing_streams,
          "agent_action": "Poll get_async_speed_test_result again before summarizing the speed test.",
      }
      context.state["speed_test_async_result_pending"] = "true"
      context.state["async_speed_test_result"] = {
          "status": "pending",
          "result_state": result_state,
          "result_available": False,
          "completed_streams": sorted(completed_events),
          "missing_streams": missing_streams,
      }
      print("[AUDIT] [get_async_speed_test_result] <<< Response Payload:", result)
      return result

    normalized = _normalize_speed_test(_normalize_event_speed_payload(data, completed_events))
    normalized["result_state"] = result_state
    normalized["result_available"] = True
    context.state["speed_test_async_result_pending"] = "false"
    context.state["async_speed_test_result"] = normalized
    result = {
        "status": "success",
        "result_available": True,
        "result_state": result_state,
        "download_mbps": normalized["download_mbps"],
        "upload_mbps": normalized["upload_mbps"],
        "latency_ms": normalized["latency_ms"],
        "overall_result": normalized["overall_result"],
        "recommendation": normalized["recommendation"],
        "summary": normalized["summary"],
        "agent_action": (
            "Use this grounded async speed-test summary. Do not expose the execution id"
            " or raw result JSON to the customer."
        ),
    }
    print("[AUDIT] [get_async_speed_test_result] <<< Response Payload:", result)
    return result
  except Exception as e:
    result = {
        "status": "error",
        "reason": "api_error",
        "error": f"Failed to get async speed-test result: {str(e)}",
        "agent_action": (
            "Tell the customer the speed-test result couldn't be retrieved right now"
            " and offer to try again or connect them with someone who can help."
        ),
    }
    print("[AUDIT] [get_async_speed_test_result] <<< Response Payload:", result)
    return result


def _coerce_to_dict(response: Any) -> dict[str, Any]:
  if response is None:
    return {}
  if isinstance(response, dict):
    return response
  body = getattr(response, "body", None)
  if isinstance(body, dict):
    return body
  if hasattr(response, "json"):
    try:
      parsed = response.json()
      if isinstance(parsed, str):
        parsed = json.loads(parsed)
      return parsed if isinstance(parsed, dict) else {}
    except Exception:
      pass
  text = getattr(response, "text", None) or (response if isinstance(response, str) else "")
  if text:
    try:
      parsed = json.loads(text)
      return parsed if isinstance(parsed, dict) else {}
    except Exception:
      return {}
  return {}


def _unwrap_result(data: dict[str, Any]) -> dict[str, Any]:
  for key in ("result", "data", "speedTestResult", "gatewaySpeedTestResult"):
    value = data.get(key)
    if isinstance(value, dict):
      return value
  return data


def _normalize_result_state(data: dict[str, Any]) -> str:
  for key in ("state", "status", "executionStatus", "testStatus", "resultState"):
    value = data.get(key)
    if value not in ("", None):
      return str(value).strip().upper()
  return "COMPLETE" if _has_speed_numbers(data) else "UNKNOWN"


def _is_complete_result(data: dict[str, Any], result_state: str, completed_events: dict[str, dict[str, Any]]) -> bool:
  if isinstance(data.get("resultEvents"), list):
    return "DOWNLOAD" in completed_events and "UPLOAD" in completed_events
  if result_state in ("COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED", "DONE", "FINISHED"):
    return True
  if result_state in ("PENDING", "RUNNING", "IN_PROGRESS", "STARTED", "QUEUED", "PROCESSING"):
    return False
  return _has_speed_numbers(data)


def _has_speed_numbers(data: dict[str, Any]) -> bool:
  return any(data.get(key) is not None for key in ("actualDownloadSpeed", "actualUploadSpeed", "latencyMs"))


def _completed_stream_events(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
  completed: dict[str, dict[str, Any]] = {}
  events = data.get("resultEvents")
  if not isinstance(events, list):
    return completed
  for event in events:
    if not isinstance(event, dict):
      continue
    event_type = str(event.get("eventType") or "").strip().upper()
    stream_type = str(event.get("streamType") or "").strip().upper()
    if event_type == "COMPLETED" and stream_type in ("DOWNLOAD", "UPLOAD"):
      completed[stream_type] = event
  return completed


def _normalize_event_speed_payload(data: dict[str, Any], completed_events: dict[str, dict[str, Any]]) -> dict[str, Any]:
  normalized = dict(data)
  download_event = completed_events.get("DOWNLOAD") or {}
  upload_event = completed_events.get("UPLOAD") or {}
  if normalized.get("actualDownloadSpeed") is None and download_event.get("MbitsPerSec") is not None:
    normalized["actualDownloadSpeed"] = download_event.get("MbitsPerSec")
  if normalized.get("actualUploadSpeed") is None and upload_event.get("MbitsPerSec") is not None:
    normalized["actualUploadSpeed"] = upload_event.get("MbitsPerSec")
  return normalized


def _round1(value: Any) -> Any:
  try:
    return round(float(value), 1)
  except (TypeError, ValueError):
    return None


def _normalize_speed_test(data: dict[str, Any]) -> dict[str, Any]:
  download = _round1(data.get("actualDownloadSpeed"))
  upload = _round1(data.get("actualUploadSpeed"))
  latency = _round1(data.get("latencyMs"))
  plan_download = _round1(data.get("planDownloadSpeed"))
  plan_upload = _round1(data.get("planUploadSpeed"))

  dl_ctx = data.get("gatewayContextualDownloadResult") or {}
  ul_ctx = data.get("gatewayContextualUploadResult") or {}
  overall_ctx = data.get("gatewayContextualResult") or {}

  download_percent = _pct(dl_ctx, download, plan_download)
  upload_percent = _pct(ul_ctx, upload, plan_upload)
  download_result = str(dl_ctx.get("resultType") or "").upper() if isinstance(dl_ctx, dict) else ""
  upload_result = str(ul_ctx.get("resultType") or "").upper() if isinstance(ul_ctx, dict) else ""

  needs_restart = "RESTART" in (download_result, upload_result)
  stream_results = [r for r in (download_result, upload_result) if r]
  all_pass = bool(stream_results) and all(r == "FULL_PASS" for r in stream_results)
  if needs_restart:
    overall_result = "RESTART"
    recommendation = "restart_gateway"
    recommendation_steps = ["Restart your gateway to clear up the slower speed -- this usually helps."]
  elif all_pass:
    overall_result = "FULL_PASS"
    recommendation = "none"
    recommendation_steps = []
  else:
    overall_result = str(overall_ctx.get("resultType") or "").upper() if isinstance(overall_ctx, dict) else ""
    overall_result = overall_result or "UNKNOWN"
    recommendation = "restart_gateway" if overall_result == "RESTART" else "none"
    recommendation_steps = (
        ["Restart your gateway to clear up the slower speed -- this usually helps."]
        if recommendation == "restart_gateway" else []
    )

  result_title = str(overall_ctx.get("title") or "") if isinstance(overall_ctx, dict) else ""
  result_message = str(overall_ctx.get("message") or "") if isinstance(overall_ctx, dict) else ""
  summary = _build_summary(download, upload, latency, plan_download, plan_upload, download_percent, upload_percent)
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


def _pct(ctx: Any, actual: Any, plan: Any) -> Any:
  value = ctx.get("planSpeedPercent") if isinstance(ctx, dict) else None
  if value is not None:
    try:
      return int(round(float(value)))
    except (TypeError, ValueError):
      pass
  if actual is not None and plan:
    try:
      return int(round((float(actual) / float(plan)) * 100))
    except (TypeError, ValueError, ZeroDivisionError):
      return None
  return None


def _build_summary(download: Any, upload: Any, latency: Any, plan_download: Any, plan_upload: Any, download_percent: Any, upload_percent: Any) -> str:
  parts = []
  if download is not None:
    if download_percent is not None and plan_download:
      parts.append(f"Download came in at {download} Mbps ({download_percent}% of your {plan_download} Mbps plan).")
    else:
      parts.append(f"Download came in at {download} Mbps.")
  if upload is not None:
    if upload_percent is not None and plan_upload:
      parts.append(f"Upload came in at {upload} Mbps ({upload_percent}% of your {plan_upload} Mbps plan).")
    else:
      parts.append(f"Upload came in at {upload} Mbps.")
  if latency is not None:
    parts.append(f"Latency was {latency} ms.")
  return " ".join(parts)
