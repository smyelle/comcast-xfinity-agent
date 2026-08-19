# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""start_async_speed_test -- wrapper for the async xFi gateway speed-test start call."""

import json
import time
from typing import Any


def start_async_speed_test(device_id: str = "") -> dict[str, Any]:
  """Start an asynchronous gateway speed test and cache its execution id.

  Args:
      device_id: Optional gateway MAC. Defaults to context.state["cable_modem_mac"].

  Returns:
      A compact dict with status and execution_id, or a safe error response.
  """
  if not device_id:
    device_id = str(context.state.get("cable_modem_mac") or "").strip()

  if not device_id or device_id == "NOT_FOUND":
    return {
        "status": "error",
        "reason": "missing_gateway_mac",
        "error": "cable_modem_mac is required to start an async speed test.",
        "agent_action": (
            "Tell the customer a speed test can't be started because no gateway was"
            " found on the account, and offer to connect them with someone who can help."
        ),
    }

  speed_test_server = str(context.state.get("speed_test_server") or "").rstrip("/")
  if not speed_test_server:
    print("[start_async_speed_test] speed_test_server variable is missing.")
    return {
        "status": "error",
        "reason": "missing_speed_test_server",
        "error": "Missing required server configuration: speed_test_server.",
        "agent_action": "transfer_to_human",
    }

  xbo_id = str(context.state.get("xbo_id") or "").strip()
  if not xbo_id:
    account_number = (
        context.state.get("accountNumber")
        or context.state.get("account_id")
        or ""
    )
    xbo_id = _fetch_xbo_id(account_number)
    if xbo_id:
      context.state["xbo_id"] = xbo_id

  if not xbo_id:
    print("[start_async_speed_test] No xbo_id available; cannot start async speed test.")
    return {
        "status": "error",
        "reason": "no_xbo",
        "error": "No XBO id available for this account; async speed test cannot be started.",
        "agent_action": (
            "Tell the customer a speed test isn't available on their account and offer"
            " to continue with other troubleshooting or connect them with someone who"
            " can help. Do NOT suggest trying again."
        ),
    }

  target_url = f"{speed_test_server}/xfispeedtest/api/gateway/speed_test/{device_id}"
  tool_args = {
      "accept": "application/json",
      "content-type": "application/json",
      "agent_name": "gecx_repair_agent",
      "account-id": xbo_id,
      "session-id": context.session_id,
      "x-auth": "ST-SAT-XAXLR",
      "x-scope": "x1:xfi-speed-platform:gateway",
      "x-url": target_url,
      "x-flow-trace-id": context.session_id,
  }

  print("[AUDIT] [start_async_speed_test] >>> Request Payload:", {
      "device_id_present": bool(device_id),
      "account_id_present": bool(xbo_id),
      "x-url": target_url,
  })

  try:
    api_start = time.time()
    response = tools.xfi_speed_test_ViaAuthProxy_startAsyncSpeedTest(tool_args)
    api_latency_ms = int((time.time() - api_start) * 1000)
    print(f"[AUDIT] [LATENCY] [start_async_speed_test] API took: {api_latency_ms} ms")
    if hasattr(response, "status_code"):
      print(
          f"[AUDIT] [HTTP STATUS] [start_async_speed_test]: {response.status_code} -"
          f" {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [start_async_speed_test] <<<: {response.text}")

    data = _coerce_to_dict(response)
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
      print("[AUDIT] [start_async_speed_test] <<< Response Payload:", result)
      return result

    context.state["async_speed_test_execution_id"] = execution_id
    result = {
        "status": "success",
        "execution_id": execution_id,
        "pollingIntervalInSeconds": _safe_number(data.get("pollingIntervalInSeconds")),
        "suggestedTotalPollingDurationInSeconds": _safe_number(
            data.get("suggestedTotalPollingDurationInSeconds")
        ),
        "agent_action": (
            "The async speed test has started. Use the async speed-test result tool"
            " with the stored execution id to poll for completion before summarizing."
        ),
    }
    print("[AUDIT] [start_async_speed_test] <<< Response Payload:", result)
    return result
  except Exception as e:
    result = {
        "status": "error",
        "reason": "api_error",
        "error": f"Failed to start async speed test: {str(e)}",
        "agent_action": (
            "Tell the customer the speed test couldn't be started right now and"
            " offer to try again or connect them with someone who can help."
        ),
    }
    print("[AUDIT] [start_async_speed_test] <<< Response Payload:", result)
    return result


def _fetch_xbo_id(account_number: str) -> str:
  account_number = str(account_number or "").strip()
  if not account_number:
    return ""
  titan_server = str(context.state.get("titan_server") or "").rstrip("/")
  if not titan_server:
    print("[start_async_speed_test] titan_server variable is missing.")
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
    for rec in _coerce_to_list(response):
      if isinstance(rec, dict):
        xbo_id = str(rec.get("id") or "").strip()
        if xbo_id:
          return xbo_id
    return ""
  except Exception as e:
    print(f"[start_async_speed_test] xbo lookup failed: {e}")
    return ""


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


def _records_from_dict(data: dict[str, Any]) -> list[Any]:
  for key in ("result", "data", "results", "accounts", "items"):
    value = data.get(key)
    if isinstance(value, list):
      return value
  return [data]


def _coerce_to_list(response: Any) -> list[Any]:
  if response is None:
    return []
  if isinstance(response, list):
    return response
  if isinstance(response, dict):
    return _records_from_dict(response)
  body = getattr(response, "body", None)
  if isinstance(body, list):
    return body
  if isinstance(body, dict):
    return _records_from_dict(body)
  if hasattr(response, "json"):
    try:
      parsed = response.json()
      if isinstance(parsed, str):
        parsed = json.loads(parsed)
      if isinstance(parsed, list):
        return parsed
      if isinstance(parsed, dict):
        return _records_from_dict(parsed)
    except Exception:
      pass
  text = getattr(response, "text", None) or (response if isinstance(response, str) else "")
  if text:
    try:
      parsed = json.loads(text)
      if isinstance(parsed, list):
        return parsed
      if isinstance(parsed, dict):
        return _records_from_dict(parsed)
    except Exception:
      return []
  return []


def _safe_number(value: Any) -> Any:
  try:
    if value is None:
      return None
    return float(value)
  except (TypeError, ValueError):
    return None
