# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught,redefined-builtin,unused-argument

"""PolySynth callback function optimized with real-time concurrent notification dispatches and dynamic MAC-Less fallbacks."""

# pylint: disable=undefined-variable

import json
import traceback
from typing import Any, Dict, Optional


def after_tool_callback(
    tool: Tool,
    input: dict[str, Any],
    callback_context: CallbackContext,
    tool_response: dict[str, Any],
) -> Optional[dict[str, Any]]:
  """Executes *after* each individual diagnostic tool completes."""
  response = tool_response
  print(f"[gateway_specialist_after] tool '{tool.name}' finished with response: {response}")

  # Storing the tool request and response in state for tracking
  try:
    request_name = tool.name + "_request"
    response_name = tool.name + "_response"
    callback_context.state[request_name] = input
    callback_context.state[response_name] = response
  except Exception as e:
    traceback.print_exc()
    print(f"[gateway_specialist_after] Error Saving the tool request and response in state")
  
  #if response is None:
  #  return None

  try:
    # 1. Parse Triage Tool
    if tool.name == "execute_rdk_device_diagnostics_ViaAuthProxy_getDeviceTriageSummaryViaAuthProxy":
      gateway_val = _parse_triage_status(response)

      # Update gateway status safely
      current_gateway_status = callback_context.state.get("gateway_status") or ""
      if _should_update_gateway_status(current_gateway_status, gateway_val):
        callback_context.state["gateway_status"] = gateway_val
        print(f"[gateway_specialist_after] Updated gateway_status to '{gateway_val}'")

    # 2. Parse Gateway WiFi Tool
    #elif tool.name == "execute_rdk_gateway_wifisummary_ViaAuthProxy_getGatewayWifiSummary":
    #  wifi_val, is_err = _parse_wifi_text(response)
    #  callback_context.state["gateway_wifi_status"] = "skipped" if is_err else wifi_val
    #  callback_context.state["gateway_wifi_resolved"] = "true"
    #  print(f"[gateway_specialist_after] Resolved gateway_wifi_status: '{wifi_val}' (error={is_err})")
    #  _evaluate_wifi(callback_context)

    # 3. Parse Client WiFi Tool
    #elif tool.name == "run_rdk_client_wifi_analysis_query_wifi_agent":
     # wifi_val, is_err = _parse_wifi_text(response)
      #callback_context.state["client_wifi_status"] = "skipped" if is_err else wifi_val
      #callback_context.state["client_wifi_resolved"] = "true"
      #print(f"[gateway_specialist_after] Resolved client_wifi_status: '{wifi_val}' (error={is_err})")
      #_evaluate_wifi(callback_context)

  except Exception as e:
    traceback.print_exc()
    print(f"[gateway_specialist_after] Error executing callback for tool '{tool.name}': {e}")

  return None


def _evaluate_wifi(callback_context: CallbackContext):
  # Only evaluate when both WiFi diagnostic tools have successfully resolved!
  if callback_context.state.get("gateway_wifi_resolved") != "true" or callback_context.state.get("client_wifi_resolved") != "true":
    return

  gw_val = callback_context.state.get("gateway_wifi_status") or "healthy"
  cl_val = callback_context.state.get("client_wifi_status") or "healthy"

  # Combine outcomes
  if gw_val == "skipped" or cl_val == "skipped":
    wifi_val = "skipped"
  elif gw_val in ("interference", "coverage_gap"):
    wifi_val = gw_val
  elif cl_val in ("interference", "coverage_gap"):
    wifi_val = cl_val
  else:
    wifi_val = "healthy"

  callback_context.state["wifi_status"] = wifi_val
  print(f"[gateway_specialist_after] Unified wifi_status resolved: '{wifi_val}'")


def _safe_parse_to_dict(response: Any) -> Dict[str, Any]:
  if response is None:
    return {}
  if hasattr(response, "json") and callable(response.json):
    try:
      parsed = response.json()
      if isinstance(parsed, str):
        parsed = json.loads(parsed)
      if isinstance(parsed, dict):
        return parsed
    except Exception:
      pass
  if isinstance(response, dict):
    return response
  if isinstance(response, str):
    try:
      parsed = json.loads(response)
      if isinstance(parsed, dict):
        return parsed
    except json.JSONDecodeError:
      return {"text": response}
  return {}


def _unwrap_result(res_dict: Dict[str, Any]) -> Dict[str, Any]:
  if not isinstance(res_dict, dict):
    return {}
  res = res_dict.get("result", res_dict)
  if isinstance(res, str):
    res = _safe_parse_to_dict(res)
  if isinstance(res, dict) and "result" in res:
    res = res["result"]
    if isinstance(res, str):
      res = _safe_parse_to_dict(res)
  return res if isinstance(res, dict) else {}


def _is_error_response(data: Dict[str, Any]) -> bool:
  if not data:
    return True
  if "error" in data or "errorCode" in data or "errors" in data:
    return True
  if isinstance(data.get("error"), dict):
    return True
  return False


def _parse_triage_status(triage_response: Any) -> str:
  """Parses the RDK triage response and maps the Assessment to a gateway action status.

  Assessment mapping:
    - "WAN / Network Issue impacting device" -> "healthy"
    - "Device Healthy from software stand point" -> "healthy"
    - "No telemetry data available for last 24 hours" -> "no_telemetry"
    - "Device reboot based on software issues" -> "reboot"
    - "Device Swap based on Hardware Issues" -> "swap"
    - "Device model not supported for analysis" -> "unsupported_device"
  """
  if triage_response is None:
    return "error"
  triage_data = _safe_parse_to_dict(triage_response)
  if _is_error_response(triage_data):
    return "error"

  # Try to extract the Assessment from structured RDK response
  assessment = _extract_rdk_assessment(triage_data)
  if assessment:
    return _map_assessment_to_status(assessment)

  # Fallback: parse from content text (legacy path)
  res = _unwrap_result(triage_data)
  content_items = res.get("content", [])
  content_text = "".join([item.get("text", "") for item in content_items if isinstance(item, dict)]).lower()

  if not content_text:
    return "error"

  # Check for assessment phrases in content text
  if "wan" in content_text and "network issue" in content_text:
    return "healthy"
  elif "no telemetry" in content_text:
    return "no_telemetry"
  elif "not supported" in content_text:
    return "unsupported_device"
  elif "swap" in content_text or "hardware issue" in content_text:
    return "healthy"
  elif "reboot" in content_text or "restart" in content_text:
    return "reboot"
  elif "healthy" in content_text:
    return "healthy"
  return "error"


def _extract_rdk_assessment(triage_data: Dict[str, Any]) -> str:
  """Extracts the Assessment string from an RDK triage response structure."""
  # Handle list response: [{"mac": ..., "result": {"RDK Recommendation": {"Assessment": ...}}}]
  if isinstance(triage_data, list):
    for item in triage_data:
      if isinstance(item, dict):
        result = item.get("result", {})
        if isinstance(result, dict):
          rec = result.get("RDK Recommendation", {})
          if isinstance(rec, dict) and rec.get("Assessment"):
            return rec["Assessment"]
    return ""

  # Handle direct dict with "result" key
  result = triage_data.get("result", triage_data)
  if isinstance(result, str):
    result = _safe_parse_to_dict(result)
  if isinstance(result, dict):
    rec = result.get("RDK Recommendation", {})
    if isinstance(rec, dict) and rec.get("Assessment"):
      return rec["Assessment"]

  # Try unwrapping content text that might contain JSON
  res = _unwrap_result(triage_data)
  content_items = res.get("content", [])
  for item in content_items:
    if isinstance(item, dict) and item.get("text"):
      try:
        parsed = json.loads(item["text"])
        if isinstance(parsed, list):
          for entry in parsed:
            if isinstance(entry, dict):
              r = entry.get("result", {})
              if isinstance(r, dict):
                rec = r.get("RDK Recommendation", {})
                if isinstance(rec, dict) and rec.get("Assessment"):
                  return rec["Assessment"]
        elif isinstance(parsed, dict):
          rec = parsed.get("RDK Recommendation", {})
          if isinstance(rec, dict) and rec.get("Assessment"):
            return rec["Assessment"]
      except (json.JSONDecodeError, TypeError):
        pass
  return ""


def _map_assessment_to_status(assessment: str) -> str:
  """Maps an RDK Assessment string to a gateway action status."""
  a = assessment.lower().strip()
  if "wan" in a or "network issue" in a:
    return "healthy"
  elif "healthy" in a or "device healthy" in a:
    return "healthy"
  elif "no telemetry" in a:
    return "no_telemetry"
  elif "reboot" in a:
    return "reboot"
  elif "swap" in a or "hardware" in a:
    return "healthy"
  elif "not supported" in a:
    return "unsupported_device"
  return "error"


def _parse_wifi_text(wifi_response: Any) -> tuple[str, bool]:
  """Parses a wifi response and returns (status, is_error)."""
  if wifi_response is None:
    return "skipped", True
  wifi_data = _safe_parse_to_dict(wifi_response)
  if _is_error_response(wifi_data):
    return "skipped", True

  res = _unwrap_result(wifi_data)
  content_items = res.get("content", [])
  content_text = "".join([item.get("text", "") for item in content_items if isinstance(item, dict)]).lower()

  if not content_text:
    return "skipped", True

  if "interference" in content_text or "congestion" in content_text:
    return "interference", False
  elif "coverage gap" in content_text or "pod" in content_text or "extender" in content_text:
    return "coverage_gap", False
  return "healthy", False


def _should_update_gateway_status(current: str, new_val: str) -> bool:
  # Priority order: swap > no_telemetry/unsupported_device > error > reboot > healthy
  priority = {"swap": 6, "no_telemetry": 5, "unsupported_device": 5, "error": 4, "reboot": 3, "healthy": 1}
  return priority.get(new_val, 0) >= priority.get(current, 0)


def _should_update_wifi_status(current: str, new_val: str) -> bool:
  if current in ("interference", "coverage_gap", "error") and new_val == "healthy":
    return False
  return True
