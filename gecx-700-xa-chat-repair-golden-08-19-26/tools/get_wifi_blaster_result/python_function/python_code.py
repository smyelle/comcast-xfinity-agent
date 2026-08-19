# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""get_wifi_blaster_result -- wrapper for WiFi blaster result polling.

Reads the WiFi blaster plan ID from session state, calls the xFi Speed Platform
blaster result endpoint through Apigee auth-proxy, and stores a normalized,
customer-safe result summary in session state for follow-up troubleshooting
logic.
"""

import json
import time
from typing import Any

_BLASTER_SCOPE = (
    "x1:xfi-speed-platform:gateway,"
    "x1:xfi-speed-platform:blaster:poll,"
    "x1:xfi-speed-platform:blaster,"
    "x1:xfi-coverage-platform:read"
)
_POLL_DELAY_SECONDS = 5.0


def get_wifi_blaster_result(
    wifi_blaster_plan_id: str = "", xbo_id: str = ""
) -> dict[str, Any]:
  """Gets and stores WiFi blaster test results.

  Args:
      wifi_blaster_plan_id: Plan ID from get_wifi_blaster_plan. Optional --
        defaults to session wifi_blaster_plan_id.
      xbo_id: Customer XBO ID. Optional -- defaults to session context/state
        xbo_id.

  Returns:
      dict: Small result summary. The parsed result is stored in context.state.
  """
  _mark_wifi_troubleshooting_owner()
  xbo_id = _resolve_xbo_id(xbo_id)
  if not wifi_blaster_plan_id:
    wifi_blaster_plan_id = context.state.get("wifi_blaster_plan_id") or ""

  if not xbo_id:
    return {
        "status": "error",
        "error": "xbo_id is required to get WiFi blaster results.",
        "agent_action": (
            "Continue with safe WiFi troubleshooting or connect the customer"
            " with someone who can help."
        ),
    }

  if not wifi_blaster_plan_id:
    return {
        "status": "error",
        "error": "wifi_blaster_plan_id is required to get WiFi blaster results.",
        "agent_action": (
            "Call get_wifi_blaster_plan first, then retry the WiFi blaster result"
            " lookup if the plan is available."
        ),
    }

  coverage_platform_server = str(
      context.state.get("coverage_platform_server")
      or "https://gw.api.dh.comcast.com"
  ).rstrip("/")
  target_url = (
      f"{coverage_platform_server}/speed-test/blaster/api/blast/result"
      f"/{wifi_blaster_plan_id}"
  )

  tool_args = {
      "accept": "application/json",
      "content-type": "application/json",
      "account-id": xbo_id,
      "agent_name": "gecx_repair_agent",
      "session-id": context.session_id,
      "x-auth": "ST-SAT-XAXLR",
      "x-scope": _BLASTER_SCOPE,
      "x-url": target_url,
      "x-flow-trace-id": context.session_id,
  }

  _audit_request = {
      "xbo_id": xbo_id,
      "wifi_blaster_plan_id": wifi_blaster_plan_id,
      "x-url": target_url,
  }
  print("[AUDIT] [get_wifi_blaster_result] >>> Request Payload:", f" {_audit_request}")

  try:
    _enforce_poll_delay(wifi_blaster_plan_id)
    _api_start = time.time()
    response = tools.xfi_blaster_result_auth_proxy_getWifiBlasterResult(tool_args)
    _api_latency_ms = int((time.time() - _api_start) * 1000)
    print(f"[AUDIT] [LATENCY] [get_wifi_blaster_result] API took: {_api_latency_ms} ms")
    if hasattr(response, "status_code"):
      print(
          "[AUDIT] [HTTP STATUS] [get_wifi_blaster_result] :"
          f" {response.status_code} - {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(f"[AUDIT] [API RESPONSE] [get_wifi_blaster_result] <<<: {response.text}")

    data = _coerce_to_dict(response)
    if not isinstance(data, dict) or not data:
      return {
          "status": "error",
          "error": "WiFi blaster result API returned no usable result.",
          "agent_action": (
              "Continue with safe WiFi troubleshooting or connect the customer"
              " with someone who can help."
          ),
      }

    result_state = _normalize_state(data.get("state"))
    is_complete = result_state == "COMPLETE"
    poll_metadata = _build_poll_metadata(wifi_blaster_plan_id)
    if is_complete:
      result_summary = _summarize_blast_results(data)
      result_summary.update(poll_metadata)
    else:
      result_summary = {
          "state": result_state,
          "is_complete": False,
          "blast_result_count": _count_blast_results(data),
          "observations": [],
          "contextual_results": [],
          "observation_summary": "",
          **poll_metadata,
      }

    context.state["wifi_blaster_result"] = result_summary
    _audit_response = {
        "status": "success",
        "result_available": is_complete,
        "result_state": result_state,
        "blast_result_count": result_summary["blast_result_count"],
        "observations": result_summary["observations"] if is_complete else [],
        "contextual_results": result_summary["contextual_results"] if is_complete else [],
        "observation_summary": result_summary["observation_summary"] if is_complete else "",
        "agent_action": (
            "If result_available is false, poll again according to the WiFi"
            " troubleshooting flow. If true, use the stored observation summary"
            " for follow-up decisions without exposing raw result JSON."
        ),
    }
    print(
        "[AUDIT] [get_wifi_blaster_result] stored wifi_blaster_result:",
        f" state={result_state}, complete={is_complete}, count={result_summary['blast_result_count']}",
    )
    print("[AUDIT] [get_wifi_blaster_result] <<< Response Payload:", f" {_audit_response}")
    return _audit_response

  except Exception as e:
    _audit_response = {
        "status": "error",
        "error": f"Failed to get WiFi blaster result: {str(e)}",
        "agent_action": (
            "Continue with safe WiFi troubleshooting or connect the customer"
            " with someone who can help."
        ),
    }
    print("[AUDIT] [get_wifi_blaster_result] <<< Response Payload:", f" {_audit_response}")
    return _audit_response


def _coerce_to_dict(response: Any) -> Any:
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


def _mark_wifi_troubleshooting_owner() -> None:
  """Keep the tool-guided WiFi sub-agent in control of the next customer turn."""
  try:
    context.state["wifi_troubleshooting_agent_active"] = "true"
    context.state["wifi_flow_active"] = "false"
    context.state["wifi_offer_pending"] = "false"
    context.state["wifi_scoping_pending"] = "false"
    context.state["wifi_pod_help_pending"] = "false"
  except Exception as e:
    print(f"[get_wifi_blaster_result] Could not set WiFi ownership state: {e}")


def _enforce_poll_delay(wifi_blaster_plan_id: str) -> None:
  """Space repeated polls for the same blaster plan by at least 5 seconds."""
  previous = context.state.get("wifi_blaster_result")
  if not isinstance(previous, dict):
    return
  if str(previous.get("_poll_plan_id") or "") != str(wifi_blaster_plan_id or ""):
    return
  last_poll_epoch = _optional_number(previous.get("_last_poll_epoch_seconds"))
  if last_poll_epoch is None:
    return
  elapsed = time.time() - float(last_poll_epoch)
  remaining = _POLL_DELAY_SECONDS - elapsed
  if remaining <= 0:
    return
  print(
      "[get_wifi_blaster_result] Waiting before repeat poll:"
      f" {round(remaining, 2)} seconds"
  )
  time.sleep(remaining)


def _build_poll_metadata(wifi_blaster_plan_id: str) -> dict[str, Any]:
  previous = context.state.get("wifi_blaster_result")
  previous_attempts = 0
  if (
      isinstance(previous, dict)
      and str(previous.get("_poll_plan_id") or "") == str(wifi_blaster_plan_id or "")
  ):
    attempts = _optional_number(previous.get("_poll_attempt_count"))
    previous_attempts = int(attempts) if attempts is not None else 0
  return {
      "_poll_plan_id": str(wifi_blaster_plan_id or ""),
      "_last_poll_epoch_seconds": time.time(),
      "_poll_attempt_count": previous_attempts + 1,
      "_poll_delay_seconds": int(_POLL_DELAY_SECONDS),
  }


def _resolve_xbo_id(provided_xbo_id: Any = "") -> str:
  keys = ("xbo_id", "xboId", "xboID", "customer_xbo_id", "customerXboId")
  for value in (provided_xbo_id, *(_lookup_session_value(key) for key in keys)):
    if value not in ("", None, "NOT_FOUND"):
      return str(value).strip()
  return ""


def _lookup_session_value(key: str) -> Any:
  ctx = globals().get("context")
  for source_name in ("state", "session_context", "sessionContext", "session_params", "parameters"):
    source = getattr(ctx, source_name, None)
    if source is None:
      continue
    if isinstance(source, dict):
      value = source.get(key)
    elif hasattr(source, "get"):
      value = source.get(key)
    else:
      value = getattr(source, key, None)
    if value not in ("", None):
      return value
  return None


def _normalize_state(value: Any) -> str:
  if value in ("", None):
    return "UNKNOWN"
  return str(value).strip().upper() or "UNKNOWN"


def _count_blast_results(data: dict[str, Any]) -> int:
  blast_results = data.get("blastResults")
  return len(blast_results) if isinstance(blast_results, list) else 0


def _summarize_blast_results(data: dict[str, Any]) -> dict[str, Any]:
  blast_results = data.get("blastResults")
  if not isinstance(blast_results, list):
    blast_results = []

  observations = []
  contextual_results = []
  for result in blast_results:
    if not isinstance(result, dict):
      continue
    observations.append(_summarize_single_blast_result(result))
    contextual_result = _extract_contextual_result(result)
    if any(contextual_result.values()):
      contextual_results.append(contextual_result)

  result_types = sorted(
      {obs["result_type"] for obs in observations if obs.get("result_type")}
  )
  status_codes = sorted(
      {obs["status_code"] for obs in observations if obs.get("status_code")}
  )
  concern_count = sum(1 for obs in observations if obs.get("has_concern"))
  pass_count = sum(1 for obs in observations if obs.get("activity_state") == "PASS")

  summary_parts = []
  if observations:
    summary_parts.append(f"{len(observations)} device path result(s) returned")
  else:
    summary_parts.append("No device path results returned")
  if result_types:
    summary_parts.append(f"result types: {', '.join(result_types)}")
  if status_codes:
    summary_parts.append(f"status codes: {', '.join(status_codes)}")
  if pass_count:
    summary_parts.append(f"{pass_count} visible activity check(s) passed")
  if concern_count:
    summary_parts.append(f"{concern_count} result(s) may need follow-up")
  contextual_summary = _summarize_contextual_results(contextual_results)
  if contextual_summary:
    summary_parts.append(contextual_summary)

  return {
      "state": _normalize_state(data.get("state")),
      "is_complete": True,
      "blast_result_count": len(observations),
      "observations": observations,
      "contextual_results": contextual_results,
      "observation_summary": "; ".join(summary_parts) + ".",
  }


def _summarize_single_blast_result(result: dict[str, Any]) -> dict[str, Any]:
  metrics = result.get("blastMetrics") if isinstance(result.get("blastMetrics"), dict) else {}
  contextual = (
      result.get("contextualMessaging")
      if isinstance(result.get("contextualMessaging"), dict)
      else {}
  )
  status = (
      result.get("blastResultStatus")
      if isinstance(result.get("blastResultStatus"), dict)
      else {}
  )
  title = contextual.get("title") if isinstance(contextual.get("title"), dict) else {}
  args = title.get("args") if isinstance(title.get("args"), dict) else {}
  activities = contextual.get("activities")
  activity_states = _extract_visible_activity_states(activities)
  band = _format_band(metrics.get("frequencyBand"))
  throughput = _first_number(result.get("throughputFromRoot"), metrics.get("throughput"))

  result_type = _safe_string(contextual.get("resultType")).lower()
  status_code = _safe_string(status.get("code"))
  has_concern = _has_concern(result_type, status_code, activity_states)

  return {
      "device_label": _safe_string(args.get("arg1")),
      "device_type": _safe_string(title.get("deviceType")),
      "result_type": result_type,
      "status_code": status_code,
      "activity_state": _join_unique(activity_states),
      "has_concern": has_concern,
      "band": band,
      "channel": _optional_number(metrics.get("channel")),
      "signal_strength_dbm": _optional_number(metrics.get("signalStrength")),
      "snr_db": _optional_number(metrics.get("snr")),
      "tx_phy_rate_mbps": _optional_number(metrics.get("txPhyRate")),
      "rx_phy_rate_mbps": _optional_number(metrics.get("rxPhyRate")),
      "throughput_mbps": _round_number(throughput),
      "observation": _build_observation_sentence(
          _safe_string(args.get("arg1")),
          result_type,
          status_code,
          activity_states,
          band,
          metrics,
          throughput,
      ),
  }


def _extract_contextual_result(result: dict[str, Any]) -> dict[str, str]:
  contextual = (
      result.get("contextualMessaging")
      if isinstance(result.get("contextualMessaging"), dict)
      else {}
  )
  title = contextual.get("title") if isinstance(contextual.get("title"), dict) else {}
  args = title.get("args") if isinstance(title.get("args"), dict) else {}
  return {
      "resultType": _safe_string(contextual.get("resultType")).lower(),
      "deviceType": _safe_string(title.get("deviceType")),
      "deviceLabel": _safe_string(args.get("arg1")),
  }


def _summarize_contextual_results(contextual_results: list[dict[str, str]]) -> str:
  valid_results = [
      item for item in contextual_results
      if item.get("resultType") or item.get("deviceType") or item.get("deviceLabel")
  ]
  if not valid_results:
    return ""

  result_counts: dict[str, int] = {}
  device_type_counts: dict[str, int] = {}
  device_labels = []
  for item in valid_results:
    result_type = item.get("resultType") or "unknown"
    device_type = item.get("deviceType") or "unknown device"
    result_counts[result_type] = result_counts.get(result_type, 0) + 1
    device_type_counts[device_type] = device_type_counts.get(device_type, 0) + 1
    label = item.get("deviceLabel")
    if label and label not in device_labels:
      device_labels.append(label)

  result_text = ", ".join(
      f"{count} {result_type}" for result_type, count in sorted(result_counts.items())
  )
  device_type_text = ", ".join(
      f"{count} {device_type}" for device_type, count in sorted(device_type_counts.items())
  )
  label_text = ", ".join(device_labels[:5])
  if len(device_labels) > 5:
    label_text += f", and {len(device_labels) - 5} more"

  parts = [f"contextual results: {result_text}"]
  if device_type_text:
    parts.append(f"device types: {device_type_text}")
  if label_text:
    parts.append(f"devices checked: {label_text}")
  return "; ".join(parts)


def _extract_visible_activity_states(activities: Any) -> list[str]:
  if not isinstance(activities, list):
    return []
  states = []
  for activity in activities:
    if not isinstance(activity, dict) or activity.get("visible") is False:
      continue
    state = _safe_string(activity.get("state")).upper()
    if state:
      states.append(state)
  return states


def _format_band(frequency_band: Any) -> str:
  if not isinstance(frequency_band, dict):
    return ""
  band = frequency_band.get("band")
  unit = _safe_string(frequency_band.get("bandUnit"))
  if band in ("", None):
    return ""
  return f"{band} {unit}".strip()


def _first_number(*values: Any) -> Any:
  for value in values:
    number = _optional_number(value)
    if number is not None:
      return number
  return None


def _optional_number(value: Any) -> Any:
  if isinstance(value, bool) or value in ("", None):
    return None
  if isinstance(value, (int, float)):
    return value
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def _round_number(value: Any) -> Any:
  number = _optional_number(value)
  if number is None:
    return None
  return round(number, 2)


def _safe_string(value: Any) -> str:
  if value in ("", None):
    return ""
  return str(value).strip()


def _join_unique(values: list[str]) -> str:
  unique_values = []
  for value in values:
    if value and value not in unique_values:
      unique_values.append(value)
  return ",".join(unique_values)


def _has_concern(result_type: str, status_code: str, activity_states: list[str]) -> bool:
  concerning_result_types = {"poor", "bad", "fair", "weak", "failed", "failure", "error"}
  if result_type in concerning_result_types:
    return True
  if status_code and status_code != "RESULT_CODE_SUCCEED":
    return True
  return any(state not in ("PASS", "PASSED") for state in activity_states)


def _build_observation_sentence(
    device_label: str,
    result_type: str,
    status_code: str,
    activity_states: list[str],
    band: str,
    metrics: dict[str, Any],
    throughput: Any,
) -> str:
  subject = device_label or "Device"
  if result_type == "great" and status_code == "RESULT_CODE_SUCCEED":
    verdict = "shows a strong WiFi result"
  elif _has_concern(result_type, status_code, activity_states):
    verdict = "may need follow-up"
  else:
    verdict = "returned a WiFi result"

  details = []
  if band:
    details.append(f"on {band}")
  signal_strength = _optional_number(metrics.get("signalStrength"))
  snr = _optional_number(metrics.get("snr"))
  throughput_value = _round_number(throughput)
  if signal_strength is not None:
    details.append(f"signal {signal_strength} dBm")
  if snr is not None:
    details.append(f"SNR {snr} dB")
  if throughput_value is not None:
    details.append(f"throughput {throughput_value} Mbps")
  if activity_states:
    details.append(f"activity {', '.join(activity_states)}")

  if details:
    return f"{subject} {verdict} ({'; '.join(details)})."
  return f"{subject} {verdict}."
  return ""
