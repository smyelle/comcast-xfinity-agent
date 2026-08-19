# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught,redefined-builtin,unused-argument

"""PolySynth callback function optimized with real-time concurrent notification dispatches and dynamic MAC-Less fallbacks."""

# pylint: disable=undefined-variable

import json
import traceback
from typing import Any, Dict, Optional


def _squash(value: Any) -> str:
  """Lowercase and strip all non-alphanumeric chars so technician-type phrasing
  variants collapse to a comparable form.

  Handles "Install and Repair Tech" -> "installandrepairtech",
  "Install & Repair Tech" -> "installrepairtech",
  "Network Technician" -> "networktechnician", etc.
  """
  s = str(value).lower()
  return "".join(ch for ch in s if ch.isalnum())


def _detect_network_tech(squashed: str) -> bool:
  return "networktech" in squashed


def _detect_install_repair(squashed: str) -> bool:
  # "installandrepair" (…and…) OR "installrepair" (…&…, /, etc.)
  return "installandrepair" in squashed or "installrepair" in squashed


def _extract_technician_type(data: Any) -> str:
  """Format-tolerant extraction of the recommended technician type.

  The network specialist LLM emits inconsistent JSON shapes: sometimes the
  instructed lowercase form ('recommendation'/'technician_type'), sometimes a
  capitalized form ('Recommendation'/'Technician Type' alongside
  'Analysis'/'Severity'). Match keys case-insensitively and ignore
  spaces/underscores so either shape resolves correctly.
  """
  if not isinstance(data, dict):
    return ""
  rec: Dict[str, Any] = {}
  for k, v in data.items():
    if isinstance(k, str) and k.strip().lower() == "recommendation" and isinstance(v, dict):
      rec = v
      break
  for k, v in rec.items():
    if isinstance(k, str) and k.strip().lower().replace(" ", "").replace("_", "") == "techniciantype":
      return str(v or "").strip()
  return ""


def _build_appointment_transfer_data(
    activity_type, activity_code, job_type, problem_code, intents
) -> str:
  """Builds the appointment transfer_data payload.

  All fields are passed in explicitly and always included, mirroring how
  activityType/activityCode/jobType are handled — empty/absent values simply
  flow through as empty strings. Note: problemCode/intents are sourced from the
  convoy/cara recommendation (read from state at the call site) since the
  network specialist does not produce them.
  """
  inner = {
      "source": "repair_gecx_agent",
      "activityType": activity_type,
      "activityCode": activity_code,
      "jobType": job_type,
      "problemCode": problem_code,
      "intents": intents,
  }
  return json.dumps({"transfer_data": inner})


def _coerce_dict(value: Any) -> Dict[str, Any]:
  """Best-effort coercion of an arbitrary response object into a dict."""
  if isinstance(value, dict):
    return value
  if isinstance(value, str):
    try:
      parsed = json.loads(value)
      return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
      return {}
  if hasattr(value, "to_dict"):
    try:
      d = value.to_dict()
      return d if isinstance(d, dict) else {}
    except Exception:
      return {}
  if hasattr(value, "text"):
    return _coerce_dict(getattr(value, "text"))
  return {}


def _clip(text: Any, limit: int = 900) -> str:
  """Trim long grounded text so it doesn't bloat the model context."""
  s = str(text or "").strip()
  return s if len(s) <= limit else (s[:limit] + " …")


def _extract_network_analysis(state: Any) -> Dict[str, str]:
  """Pull the connect/network agent's Analysis/Severity/Remediation from the
  raw A2A response stored in state (network_api_response)."""
  resp = _coerce_dict(state.get("network_api_response"))
  result = resp.get("result", {})
  texts = []
  if isinstance(result, dict):
    for part in (result.get("parts") or []):
      if isinstance(part, dict) and part.get("text"):
        texts.append(part["text"])
  out: Dict[str, str] = {}
  for t in texts:
    blk = _extract_json_block(t)
    if not blk:
      continue
    for k, v in blk.items():
      lk = str(k).strip().lower()
      if lk == "analysis":
        out["analysis"] = _clip(v)
      elif lk == "severity":
        out["severity"] = _clip(v, 60)
      elif lk == "recommendation" and isinstance(v, dict):
        for rk, rv in v.items():
          if str(rk).strip().lower().replace(" ", "").replace("_", "") == "remediation":
            out["remediation"] = _clip(rv)
    if out:
      break
  return out


def _extract_rdk_summary(state: Any) -> Dict[str, Any]:
  """Pull the RDK/gateway agent's HighLevelSummary/Assessment/DeviceStatus from
  the raw triage response saved by the gateway specialist callback."""
  resp = _coerce_dict(
      state.get(
          "execute_rdk_device_diagnostics_ViaAuthProxy_getDeviceTriageSummaryViaAuthProxy_response"
      )
  )
  result = resp.get("result", resp)
  content = result.get("content", []) if isinstance(result, dict) else []
  out: Dict[str, Any] = {}
  for item in content:
    if not (isinstance(item, dict) and item.get("text")):
      continue
    blk = _extract_json_block(item["text"])
    if not blk:
      continue
    for k, v in blk.items():
      lk = str(k).strip().lower()
      if lk == "highlevelsummary":
        if isinstance(v, list):
          out["high_level_summary"] = _clip(" ".join(str(x) for x in v))
        else:
          out["high_level_summary"] = _clip(v)
      elif lk == "devicestatus":
        out["device_status"] = _clip(v, 80)
      elif lk.replace(" ", "").replace("_", "") == "rdkrecommendation" and isinstance(v, dict):
        for rk, rv in v.items():
          rlk = str(rk).strip().lower()
          if rlk == "assessment":
            out["assessment"] = _clip(rv, 200)
          elif rlk == "justification":
            out["justification"] = _clip(rv)
    if out:
      break
  return out


def _collect_summary_source(callback_context: Any) -> Dict[str, Any]:
  """Assemble the grounded facts the orchestrator LLM uses to write the
  post-diagnostics 'View troubleshooting summary' block. Reads accumulated
  shared session state, so whichever diagnostic tool finishes last carries the
  most complete source."""
  st = callback_context.state
  src: Dict[str, Any] = {
      "account_status": st.get("account_status", ""),
      "outage_status": st.get("outage_status", ""),
      "outage_message": _clip(st.get("outage_message", ""), 400),
      "network_status": st.get("network_status", ""),
      "technician_type": st.get("technician_type", ""),
      "gateway_status": st.get("gateway_status", ""),
      "wifi_status": st.get("wifi_status", ""),
      "has_wifi_extenders": st.get("has_wifi_extenders", ""),
  }
  try:
    src["network"] = _extract_network_analysis(st)
  except Exception:
    src["network"] = {}
  try:
    src["gateway"] = _extract_rdk_summary(st)
  except Exception:
    src["gateway"] = {}
  return src


def after_tool_callback(
    tool: Tool,
    input: dict[str, Any],
    callback_context: CallbackContext,
    tool_response: dict[str, Any],
) -> Optional[dict[str, Any]]:
  """Executes *immediately after* a subagent tool runs."""
  print(f"[orchestrator_after_tool] Tool '{tool.name}' finished.")

  response = tool_response
  # Storing the tool request and response in state for tracking
  # try:
  #   request_name = tool.name + "_request"
  #   response_name = tool.name + "_response"
  #   callback_context.state[request_name] = input
  #   callback_context.state[response_name] = response
  # except Exception as e:
  #   traceback.print_exc()
  #   print(f"[orchestrator_after_tool] Error Saving the tool request and response in state")

  if response is None:
    return None

  try:
    # Extract subagent dialogue string
    subagent_output = ""
    if isinstance(response, dict):
      subagent_output = response.get("response", "")
    elif isinstance(response, str):
      try:
        parsed_res = json.loads(response)
        subagent_output = parsed_res.get("response", "")
      except json.JSONDecodeError:
        subagent_output = response

    print(f"[orchestrator_after_tool] Subagent text response: {subagent_output}")

    if not subagent_output or len(subagent_output.strip()) == 0:
      print("[orchestrator_after_tool] Subagent response text is completely empty. Skip mapping.")
      _send_error_notification_for_tool(tool, callback_context)
      return None

    # Parse the inner report JSON block
    report_data = _extract_json_block(subagent_output)
    if not report_data:
      print("[orchestrator_after_tool] Could not extract valid JSON diagnostics report from response.")
      _send_error_notification_for_tool(tool, callback_context)
      return None

    print(f"[orchestrator_after_tool] Extracted report dictionary: {report_data}")

    # --------------------------------------------------------
    # 1. Parse Outage Specialist Report
    # --------------------------------------------------------
    if tool.name == "outage_specialist_agent_as_a_tool":
      outage_status = _sanitize_status("outage", report_data.get("outage_status") or "none")
      print(f"[orchestrator_after_tool] Outage Subagent text response: {outage_status}")
      outage_detected = str(outage_status).lower() in ("active", "degradation")

      # Safe cache sync mapping directly to parent memory state context!
      callback_context.state["outage_status"] = outage_status
      callback_context.state["outage_detected"] = "true" if outage_detected else "false"
      if report_data.get("outage_message"):
        callback_context.state["outage_message"] = report_data.get("outage_message")
      if report_data.get("customer_message"):
        callback_context.state["customer_message"] = report_data.get("customer_message")
      if report_data.get("impacted_services"):
        callback_context.state["impacted_services"] = report_data.get("impacted_services")

      # Dispatch visual card
      if callback_context.state.get("outage_notified") != "true":
        if outage_status in ("active", "degradation"):
          tools.post_user_notification({
              "tool": "outage",
              "status": "failure",
              "text": "Service outage found in your neighborhood",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        else:
          tools.post_user_notification({
              "tool": "outage",
              "status": "success",
              "text": "No service outages in your area",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        callback_context.state["outage_notified"] = "true"

      # Surface the grounded summary source (additive) so the orchestrator LLM
      # can build the post-diagnostics "View troubleshooting summary" block.
      return {
          "response": subagent_output,
          "troubleshooting_summary_source": _collect_summary_source(callback_context),
      }

    # --------------------------------------------------------
    # 2. Parse Network Specialist Report
    # --------------------------------------------------------
    elif tool.name == "network_specialist_agent_as_a_tool":
      network_status = _sanitize_status("network", report_data.get("network_status") or "healthy")
      print(f"[orchestrator_after_tool] Network Subagent text response: {network_status}")
      # Safe cache sync mapping to parent memory state context!
      callback_context.state["network_status"] = network_status

      # Deterministically set transfer ticket values based on technician type.
      # Resolve the technician type from the LLM report, falling back to the
      # deterministic value set by the network specialist's own
      # after_tool_callback (derived from the actual connect-agent API response),
      # since the LLM report text is not a reliable source.
      matched_activity_type = ""
      matched_activity_code = ""
      matched_job_type = ""
      matched_problem_code = ""
      matched_intents = ""

      tech_type = _extract_technician_type(report_data)
      state_tech = str(callback_context.state.get("technician_type", "")).lower()
      # Robust, punctuation-tolerant phrase detection. Use the structured
      # technician type and the upstream state signal, but also scan the raw
      # report — the specialist LLM emits the recommendation in inconsistent
      # shapes/spellings ("Install & Repair Tech", top-level technician_type,
      # etc.) that exact matching misses.
      norm_squash = _squash(tech_type)
      try:
        report_squash = _squash(json.dumps(report_data))
      except Exception:
        report_squash = _squash(report_data)

      is_network_tech = (
          _detect_network_tech(norm_squash)
          or state_tech == "network_tech"
          or _detect_network_tech(report_squash)
      )
      is_install_repair = (
          _detect_install_repair(norm_squash)
          or state_tech == "install_repair_tech"
          or _detect_install_repair(report_squash)
      )
      # If both matched (e.g. remediation text mentions a "network technician"),
      # prefer the explicit structured technician type; otherwise default to the
      # appointment (install & repair) path.
      if is_network_tech and is_install_repair:
        struct_net = _detect_network_tech(norm_squash)
        struct_ir = _detect_install_repair(norm_squash)
        if struct_ir and not struct_net:
          is_network_tech = False
        elif struct_net and not struct_ir:
          is_install_repair = False
        else:
          is_network_tech = False

      if str(network_status).lower() == "impaired" or is_network_tech or is_install_repair:
        callback_context.state["network_status"] = "impaired"

      # A "Network Tech" recommendation is a LIVE-HUMAN handoff (skill=human):
      # like any other human transfer it needs no appointment/truck-roll ticket
      # data, so we only record the technician type and route to a live agent.
      # Everything else goes to the appointment agent with the resolved codes.
      if is_network_tech:
        callback_context.state["technician_type"] = "network_tech"
      else:
        if is_install_repair:
          matched_activity_type = "TROUBLE_CALL"
          matched_activity_code = "H3"
          matched_job_type = "AO"
          matched_problem_code = "88653"
          matched_intents = "INTERNET_REPAIR"
          callback_context.state["technician_type"] = "install_repair_tech"
        else:
          # Ambiguous impairment (no technician type resolved): keep any
          # appointment values already in state (e.g. convoy/cara) rather than
          # clobbering them with defaults.
          matched_activity_code = callback_context.state.get("activityCode") or "H2"
          matched_job_type = callback_context.state.get("jobType") or "Test"
          matched_activity_type = callback_context.state.get("activityType") or "TROUBLE_CALL"
          matched_problem_code = callback_context.state.get("problemCode", "")
          matched_intents = callback_context.state.get("intents", "")
        callback_context.state["activityCode"] = matched_activity_code
        callback_context.state["jobType"] = matched_job_type
        callback_context.state["activityType"] = matched_activity_type
        callback_context.state["problemCode"] = matched_problem_code
        callback_context.state["intents"] = matched_intents

      print(
          "[orchestrator_after_tool] Network routing:"
          f" is_network_tech={is_network_tech},"
          f" activityCode='{matched_activity_code}',"
          f" jobType='{matched_job_type}',"
          f" activityType='{matched_activity_type}',"
          f" problemCode='{matched_problem_code}',"
          f" intents='{matched_intents}'"
      )

      # Dispatch visual card
      if callback_context.state.get("network_notified") != "true":
        resolved_network_status = callback_context.state.get("network_status", network_status)
        if resolved_network_status == "impaired":
          tools.post_user_notification({
              "tool": "network",
              "status": "failure",
              "text": "Network issue may be affecting your service",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        elif resolved_network_status == "error":
          tools.post_user_notification({
              "tool": "network",
              "status": "error",
              "text": "Unable to check network",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        else:
          tools.post_user_notification({
              "tool": "network",
              "status": "success",
              "text": "Network is stable",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        callback_context.state["network_notified"] = "true"

      # Execute transfer when network is impaired (same pattern as convoy technician dispatch)
      resolved_network = callback_context.state.get("network_status", network_status)
      if resolved_network == "impaired":
        # Mark gateway/wifi as skipped so LLM doesn't wait for or act on gateway results
        callback_context.state["gateway_status"] = "skipped"
        callback_context.state["wifi_status"] = "skipped"
        callback_context.state["network_transfer_initiated"] = "true"
        print("[orchestrator_after_tool] Network impaired -> marking gateway_status/wifi_status as 'skipped'")

        if is_network_tech:
          # Network-side signal fault: hand off to a live human agent (NOT the
          # appointment/truck-roll flow). Escalate so follow-up turns are
          # intercepted deterministically.
          callback_context.state["escalate_to_human_flag"] = "on"
          try:
            tools.transfer_potato_to_agent_v2({
                "task": "Connect the user to the live agent",
                "skill": "human",
                "key_events": "Completed our checks and found a signal issue with the connection to the customer's home requiring a network technician. Connecting the customer with a live agent.",
            })
            print("[orchestrator_after_tool] Network tech -> transfer_potato_to_agent_v2 (skill=human)")
          except Exception as e:
            print(f"[orchestrator_after_tool] transfer_potato_to_agent_v2 (human) failed: {e}")
        else:
          # Install & Repair (or default) -> appointment/truck-roll dispatch.
          # problemCode/intents are resolved above from the network technician-type
          # mapping (install_repair -> 88653/INTERNET_REPAIR) or from convoy/cara
          # values already in state; they flow through as empty when neither applies.
          transfer_data = _build_appointment_transfer_data(
              matched_activity_type,
              matched_activity_code,
              matched_job_type,
              matched_problem_code,
              matched_intents,
          )
          try:
            tools.transfer_potato_to_agent_v2({
                "task": "Network analysis identified a line signal issue requiring technician dispatch.",
                "skill": "appointment",
                "key_events": "We've completed our checks and found a signal issue with the connection to your home. We're now looking for the earliest available appointment to send a technician your way.",
                "data": transfer_data,
            })
            print(
                "[orchestrator_after_tool] Network impaired -> transfer_potato_to_agent_v2"
                f" (skill=appointment) called with activityCode='{matched_activity_code}',"
                f" jobType='{matched_job_type}',"
                f" activityType='{matched_activity_type}'"
            )
          except Exception as e:
            print(f"[orchestrator_after_tool] transfer_potato_to_agent_v2 failed: {e}")

        # Feed the freshly-resolved dispatch codes back to the LLM via the tool
        # response. GECX freezes the instruction-template variables
        # ({problemCode}/{activityCode}/{jobType}/{intents}) at the pre-diagnostics
        # state snapshot, so the model's OWN transfer_potato_to_agent_v2 functionCall
        # (the payload steering actually reads) would otherwise carry the stale convoy
        # defaults (H2/Test/empty). Returning the ready-made transfer_data string here
        # lets the instruction tell the model to pass it through verbatim.
        if not is_network_tech:
          appointment_transfer_data = _build_appointment_transfer_data(
              matched_activity_type,
              matched_activity_code,
              matched_job_type,
              matched_problem_code,
              matched_intents,
          )
          return {
              "response": subagent_output,
              "appointment_transfer_data": appointment_transfer_data,
              "troubleshooting_summary_source": _collect_summary_source(callback_context),
          }

      # Healthy / non-impaired network: surface the grounded summary source
      # (additive) alongside the subagent's own report text.
      return {
          "response": subagent_output,
          "troubleshooting_summary_source": _collect_summary_source(callback_context),
      }

    # --------------------------------------------------------
    # 3. Parse Gateway/Wifi Specialist Report
    # --------------------------------------------------------
    elif tool.name == "gateway_specialist_agent_as_a_tool":
      # PRIORITY GUARD: If network agent already identified impairment and initiated transfer,
      # skip gateway processing entirely to avoid overriding network recommendations.
      # Checks both the transfer flag AND network_status to handle either callback ordering.
      if (callback_context.state.get("network_transfer_initiated") == "true"
          or callback_context.state.get("network_status") == "impaired"):
        print("[orchestrator_after_tool] Network impairment detected. Skipping gateway processing.")
        callback_context.state["gateway_status"] = "skipped"
        callback_context.state["wifi_status"] = "skipped"
        if callback_context.state.get("gateway_notified") != "true":
          tools.post_user_notification({
              "tool": "device",
              "status": "success",
              "text": "Modem check skipped",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
          callback_context.state["gateway_notified"] = "true"
        return {
            "response": "Gateway diagnostics skipped. Network impairment already detected and technician dispatch initiated.",
            "troubleshooting_summary_source": _collect_summary_source(callback_context),
        }

      gateway_status = _sanitize_status("gateway", report_data.get("gateway_status") or "healthy")
      wifi_status = _sanitize_status("wifi", report_data.get("wifi_status") or "healthy")
      print(f"[orchestrator_after_tool] RDK Subagent text response: {gateway_status, wifi_status}")
      # Safe cache sync mapping directly to parent memory state context!
      callback_context.state["gateway_status"] = gateway_status
      callback_context.state["wifi_status"] = wifi_status

      if gateway_status in ("swap", "no_telemetry", "unsupported_device"):
        callback_context.state["activityCode"] = ""
        callback_context.state["jobType"] = ""

      # Dispatch Device visual card
      if callback_context.state.get("gateway_notified") != "true":
        if gateway_status == "reboot":
          tools.post_user_notification({
              "tool": "device",
              "status": "failure",
              "text": "We have detected an issue with your gateway",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        elif gateway_status == "swap":
          tools.post_user_notification({
              "tool": "device",
              "status": "failure",
              "text": "Modem has a hardware issue, replacement needed",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        elif gateway_status == "no_telemetry":
          tools.post_user_notification({
              "tool": "device",
              "status": "failure",
              "text": "Modem is offline or disconnected",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        elif gateway_status == "unsupported_device":
          tools.post_user_notification({
              "tool": "device",
              "status": "failure",
              "text": "Your device model is not supported for automated analysis",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        elif gateway_status == "error":
          tools.post_user_notification({
              "tool": "device",
              "status": "error",
              "text": "Unable to check modem",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        else:
          tools.post_user_notification({
              "tool": "device",
              "status": "success",
              "text": "Modem is connected",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "after_tool_callback",
          })
        callback_context.state["gateway_notified"] = "true"

      # Return sanitized response to LLM — only status values, no raw details text
      # that could confuse the LLM into inferring impairment from descriptive text.
      # The grounded RDK HighLevelSummary is surfaced ONLY under
      # troubleshooting_summary_source (used to write the collapsible summary),
      # never as an authoritative status signal.
      sanitized_response = json.dumps({
          "gateway_status": gateway_status,
          "wifi_status": wifi_status,
      })
      print(f"[orchestrator_after_tool] Returning sanitized gateway response to LLM: {sanitized_response}")
      return {
          "response": sanitized_response,
          "troubleshooting_summary_source": _collect_summary_source(callback_context),
      }

  except Exception as e:
    traceback.print_exc()
    print(f"[orchestrator_after_tool] Error parsing report payload: {e}")
    _send_error_notification_for_tool(tool, callback_context)

  return None


def _send_error_notification_for_tool(tool: Any, callback_context: Any) -> None:
  """Sends a failure notification for the subagent tool so the UI doesn't stay stuck on 'started'."""
  try:
    tool_name = getattr(tool, "name", "") or ""
    if tool_name == "network_specialist_agent_as_a_tool":
      if callback_context.state.get("network_notified") != "true":
        tools.post_user_notification({
            "tool": "network",
            "status": "error",
            "text": "Unable to check network",
            "calling_agent": "repair_orchestration_agent",
            "calling_tool": "after_tool_callback",
        })
        callback_context.state["network_notified"] = "true"
    elif tool_name == "gateway_specialist_agent_as_a_tool":
      if callback_context.state.get("gateway_notified") != "true":
        tools.post_user_notification({
            "tool": "device",
            "status": "error",
            "text": "Unable to check modem",
            "calling_agent": "repair_orchestration_agent",
            "calling_tool": "after_tool_callback",
        })
        callback_context.state["gateway_notified"] = "true"
    elif tool_name == "outage_specialist_agent_as_a_tool":
      if callback_context.state.get("outage_notified") != "true":
        tools.post_user_notification({
            "tool": "outage",
            "status": "error",
            "text": "Unable to check for service outages",
            "calling_agent": "repair_orchestration_agent",
            "calling_tool": "after_tool_callback",
        })
        callback_context.state["outage_notified"] = "true"
  except Exception as e:
    print(f"[orchestrator_after_tool] Failed to send error notification: {e}")


def _find_balanced_json(text: str) -> str:
  """Return the first balanced ``{...}`` JSON object substring, or ''.

  Tolerates trailing prose (e.g. a "## Tool Calls Summary" block) the specialist
  appends after the JSON payload by matching braces while respecting strings.
  """
  if not isinstance(text, str):
    return ""
  start = text.find("{")
  if start == -1:
    return ""
  depth = 0
  in_str = False
  esc = False
  for i in range(start, len(text)):
    c = text[i]
    if in_str:
      if esc:
        esc = False
      elif c == "\\":
        esc = True
      elif c == '"':
        in_str = False
    else:
      if c == '"':
        in_str = True
      elif c == "{":
        depth += 1
      elif c == "}":
        depth -= 1
        if depth == 0:
          return text[start:i + 1]
  return ""


def _extract_json_block(text: str) -> Dict[str, Any]:
  """Best-effort extraction of a JSON object from a specialist's report text.

  Specialist LLMs often return "not completely JSON" payloads: a ```json ... ```
  fence with prose before it and/or a trailing "## Tool Calls Summary" block.
  Parse leniently instead of requiring the whole string to be valid JSON.
  """
  if not text:
    return {}
  cleaned = text.strip()

  # Strip a ```json / ``` fence located anywhere, dropping trailing prose.
  if "```json" in cleaned:
    json_start = cleaned.index("```json") + len("```json")
    rest = cleaned[json_start:]
    json_end = rest.index("```") if "```" in rest else len(rest)
    cleaned = rest[:json_end].strip()
  elif cleaned.startswith("```"):
    cleaned = cleaned[3:]
    if "```" in cleaned:
      cleaned = cleaned[:cleaned.index("```")]
    cleaned = cleaned.strip()

  # First attempt: parse the cleaned payload directly.
  try:
    parsed = json.loads(cleaned)
    if isinstance(parsed, dict):
      return parsed
  except json.JSONDecodeError:
    pass

  # Fallback: extract the first balanced {...} object, dropping any trailing
  # prose (e.g. "## Tool Calls Summary") the model appended after the JSON.
  for candidate in (_find_balanced_json(cleaned), _find_balanced_json(text)):
    if candidate:
      try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
          return parsed
      except json.JSONDecodeError:
        continue
  return {}


def _sanitize_status(category: str, raw_val: str) -> str:
  if not raw_val:
    return "healthy"
  val = str(raw_val).strip().lower()

  if category == "outage":
    if val in ("active", "degradation", "outage"):
      return "active"
    elif val in ("error", "failed"):
      return "error"
    return "none"

  elif category == "network":
    if val in ("impaired", "degraded", "failed", "down", "critical"):
      return "impaired"
    elif val in ("error", "failed"):
      return "error"
    return "healthy"

  elif category == "gateway":
    if val in ("error", "failed", "skipped"):
      return "error"
    elif val in ("reboot", "restart", "powercycle"):
      return "reboot"
    elif val in ("swap", "replace", "exchange"):
      return "swap"
    elif val in ("no_telemetry", "no telemetry"):
      return "no_telemetry"
    elif val in ("unsupported_device", "not supported"):
      return "unsupported_device"
    elif val in ("wan_issue", "wan", "network issue", "offline", "unreachable"):
      return "healthy"
    return "healthy"

  #elif category == "wifi":
  #  if val == "skipped" or val in ("error", "failed"):
  #    return "skipped"
  #  elif val in ("interference", "congestion", "overlap", "crowded"):
  #    return "interference"
  #  elif val in ("coverage_gap", "weak", "distance", "pod", #"extender"):
  #    return "coverage_gap"
  #  elif val in ("reboot", "restart", "radio_failure", "offline"):
  #    return "interference"
  #  return "healthy"

  return "healthy"


