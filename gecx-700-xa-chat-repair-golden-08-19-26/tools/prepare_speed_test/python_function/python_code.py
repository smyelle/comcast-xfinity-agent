# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""prepare_speed_test -- confirmation-turn helper for the gateway speed test.

Resolves the account's Titan XBO id (via the xbo_lookup_ViaAuthProxy auth-proxy
toolset) and caches it in session state as `xbo_id`, so a subsequent
perform_speed_test can pass it as the required `account-id` header. Also reports
whether a speed test can be run at all: some accounts have no XBO id, and for
those the speed test cannot run -- the assistant must NOT offer it.

This is called on the turn the customer asks for a speed test (before offering to
run it), so the ~30s test itself never pays the xbo lookup latency.
"""

import json
from typing import Any


def prepare_speed_test() -> dict[str, Any]:
  """Resolve + cache the account's XBO id and report speed-test eligibility.

  Returns:
      {"status": "available"} when an XBO id was found (cached in state['xbo_id']),
      or {"status": "unavailable", "agent_action": ...} when the account has none.
  """
  # Reuse a value already resolved earlier in this session.
  cached = str(context.state.get("xbo_id") or "").strip()
  if cached:
    return {"status": "available"}

  account_number = (
      context.state.get("accountNumber")
      or context.state.get("account_id")
      or ""
  )
  xbo_id = _fetch_xbo_id(account_number)
  if xbo_id:
    context.state["xbo_id"] = xbo_id
    print("[prepare_speed_test] Resolved and cached xbo_id for the account.")
    return {"status": "available"}

  print("[prepare_speed_test] No xbo_id available for this account.")
  return {
      "status": "unavailable",
      "agent_action": (
          "A speed test can't be run on this account. Do NOT offer or promise a speed"
          " test. Briefly let the customer know a speed test isn't available for their"
          " account and offer to continue with other troubleshooting or connect them"
          " with someone who can help. Do NOT tell them to try again."
      ),
  }


def _fetch_xbo_id(account_number: str) -> str:
  """Resolve the Titan XBO id for a billing account via the xbo_lookup auth-proxy
  toolset. Returns the XBO id string, or '' when unavailable/not found."""
  account_number = str(account_number or "").strip()
  if not account_number:
    print("[prepare_speed_test] Missing account_number; cannot resolve xbo_id.")
    return ""

  titan_server = str(context.state.get("titan_server") or "").rstrip("/")
  if not titan_server:
    print("[prepare_speed_test] titan_server variable is missing from context state!")
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
    print(
        "[prepare_speed_test] xbo lookup raw"
        f" type={type(response).__name__} preview={str(response)[:200]!r}"
    )
    for rec in _coerce_to_list(response):
      if isinstance(rec, dict):
        xbo_id = str(rec.get("id") or "").strip()
        if xbo_id:
          return xbo_id
    return ""
  except Exception as e:
    print(f"[prepare_speed_test] xbo lookup failed: {e}")
    return ""


def _records_from_dict(d: dict) -> list:
  """Unwrap the account records list from a response envelope dict. CES wraps a
  bare JSON array under "result"; Titan may also nest under data/results/etc. If no
  list envelope is found, treat the dict itself as a single record."""
  for key in ("result", "data", "results", "accounts", "items"):
    val = d.get(key)
    if isinstance(val, list):
      return val
  return [d]


def _coerce_to_list(response: Any) -> list:
  """Best-effort convert a CXAS ExternalResponse (or str/dict) into the list of
  account records returned by the Titan accounts endpoint. Handles the response
  arriving as a raw list, a plain dict envelope, an ExternalResponse (.body/.json/
  .text), or a JSON string -- always unwrapping the "result"/data envelope."""
  if response is None:
    return []
  if isinstance(response, list):
    return response
  if isinstance(response, dict):
    return _records_from_dict(response)
  # ExternalResponse-like: try .body, then .json(), then .text.
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

