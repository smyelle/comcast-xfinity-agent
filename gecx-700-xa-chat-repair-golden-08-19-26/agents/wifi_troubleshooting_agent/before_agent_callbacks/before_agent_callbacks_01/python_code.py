# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""WiFi troubleshooting before-agent callback.

Keeps the WiFi sub-agent sticky across turns and resolves the account's Titan
XBO id before the first WiFi tool call. The WiFi blaster APIs require this XBO
id as the auth-proxy `account-id` header, so caching it here avoids relying on a
separate speed-test flow to have already populated `context.state["xbo_id"]`.

This callback is silent: it must never return a customer-visible message.
"""

import json
from typing import Any, Optional


def before_agent_callback(
    callback_context: CallbackContext,
) -> Optional[Content]:
  """Populate WiFi session bookkeeping before the sub-agent starts."""
  try:
    callback_context.state["wifi_troubleshooting_agent_active"] = "true"
  except Exception as e:
    print(f"[wifi_before_agent_callback] could not set ownership flag: {e}")

  try:
    cached = str(callback_context.state.get("xbo_id") or "").strip()
    if cached:
      return None

    account_number = (
        callback_context.state.get("accountNumber")
        or callback_context.state.get("account_id")
        or ""
    )
    xbo_id = _fetch_xbo_id(callback_context, account_number)
    if xbo_id:
      callback_context.state["xbo_id"] = xbo_id
      print("[wifi_before_agent_callback] Resolved and cached xbo_id for WiFi tools.")
    else:
      print("[wifi_before_agent_callback] No xbo_id resolved before WiFi tools.")
  except Exception as e:
    # Do not block the WiFi agent. The wrapper tools still fail safely if xbo_id
    # remains unavailable.
    print(f"[wifi_before_agent_callback] xbo lookup skipped after error: {e}")

  return None


def _fetch_xbo_id(callback_context: CallbackContext, account_number: str) -> str:
  """Resolve the Titan XBO id for a billing account via xbo_lookup auth-proxy."""
  account_number = str(account_number or "").strip()
  if not account_number:
    print("[wifi_before_agent_callback] Missing account_number; cannot resolve xbo_id.")
    return ""

  titan_server = str(callback_context.state.get("titan_server") or "").rstrip("/")
  if not titan_server:
    print("[wifi_before_agent_callback] titan_server variable is missing from context state.")
    return ""

  target_url = f"{titan_server}/accounts?billingAccountId={account_number}"
  tool_args = {
      "accept": "application/json",
      "content-type": "application/json",
      "agent_name": "gecx_repair_agent",
      "x-auth": "TITAN-SAT-XAXLR",
      "x-scope": "x1:xbo:titan:read",
      "x-url": target_url,
      "x-flow-trace-id": str(getattr(callback_context, "session_id", "") or ""),
  }

  try:
    response = tools.xbo_lookup_ViaAuthProxy_getXboByAccount(tool_args)
    print(
        "[wifi_before_agent_callback] xbo lookup raw"
        f" type={type(response).__name__} preview={str(response)[:200]!r}"
    )
    for rec in _coerce_to_list(response):
      if isinstance(rec, dict):
        xbo_id = str(rec.get("id") or "").strip()
        if xbo_id:
          return xbo_id
    return ""
  except Exception as e:
    print(f"[wifi_before_agent_callback] xbo lookup failed: {e}")
    return ""


def _records_from_dict(data: dict[str, Any]) -> list[Any]:
  """Unwrap account records from common CES/Titan response envelope keys."""
  for key in ("result", "data", "results", "accounts", "items"):
    val = data.get(key)
    if isinstance(val, list):
      return val
  return [data]


def _coerce_to_list(response: Any) -> list[Any]:
  """Best-effort conversion of ExternalResponse/list/dict/JSON text to records."""
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
