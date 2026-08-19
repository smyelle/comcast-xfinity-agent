# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught,redefined-builtin,unused-argument

"""PolySynth callback function optimized with real-time concurrent notification dispatches and dynamic MAC-Less fallbacks."""

# pylint: disable=undefined-variable

import json
import traceback
from typing import Any, Dict, Optional


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


def _extract_network_status(data: Any) -> str:
  """Case/format-tolerant extraction of an explicit `network_status` field.

  Some network specialist payloads report status directly (e.g.
  {"network_status": "error", "recommendation": {...}}) rather than via the
  Severity/Recommendation analysis shape.
  """
  if not isinstance(data, dict):
    return ""
  for k, v in data.items():
    if isinstance(k, str) and k.strip().lower().replace(" ", "").replace("_", "") == "networkstatus":
      return str(v or "").strip().lower()
  return ""


def _find_balanced_json(text: str) -> str:
  """Return the first balanced ``{...}`` JSON object substring, or ''.

  Tolerates trailing prose (e.g. a "## Tool Calls Summary" block) that the LLM
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


def _parse_json_lenient(text: Any) -> Optional[Dict[str, Any]]:
  """Best-effort parse of a JSON object from text that is "not completely JSON".

  Handles: markdown ```json ... ``` fences, prose before/after the fence,
  trailing summaries (e.g. "## Tool Calls Summary"), and payloads where the JSON
  is embedded within surrounding text. Returns a dict on success, else None.
  """
  if not isinstance(text, str) or not text.strip():
    return None
  cleaned = text.strip()

  # Strip a leading ```json / ``` fence and drop everything after the close.
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
    obj = json.loads(cleaned)
    if isinstance(obj, dict):
      return obj
  except Exception:
    pass

  # Fallback: extract the first balanced {...} object, dropping any trailing
  # prose the model appended after the JSON payload.
  for candidate in (_find_balanced_json(cleaned), _find_balanced_json(text)):
    if candidate:
      try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
          return obj
      except Exception:
        continue
  return None


def after_tool_callback(
    tool: Tool,
    input: dict[str, Any],
    callback_context: CallbackContext,
    tool_response: dict[str, Any],
) -> Optional[dict[str, Any]]:
  """Executes *after* a tool has run and returned a result.

  Args:
    tool: The tool description object.
    input: The input arguments to the tool.
    callback_context: The execution context containing state memory.
    tool_response: The actual response returned from the tool execution.

  Returns:
    The modified response dictionary, or None to approve the response as-is.
  """
  if tool.name == "connect_agent_rest_call_sendA2AMessageViaAuthProxy":
    response = tool_response

    print(f"[connect_network_after] received response: {response}")

    # Storing the tool request and response in state for tracking
    try:
      request_name = tool.name + "_request"
      response_name = tool.name + "_response"
      callback_context.state[request_name] = input
      callback_context.state[response_name] = response
    except Exception as e:
      traceback.print_exc()
      print(f"[connect_network_after] Error Saving the tool request and response in state")

    if response is None:
      return handle_error(callback_context, "Response is None")

    # Parse the response
    try:
      data = response
      if hasattr(response, "body"):
        data = response.body
      elif hasattr(response, "text"):
        data = json.loads(response.text)
      elif hasattr(response, "__getitem__"):
        data = response

      if isinstance(data, str):
        try:
          data = json.loads(data)
        except json.JSONDecodeError:
          return handle_error(
              callback_context, f"Failed to decode string response: {data}"
          )

      print(f"[connect_network_after] Parsed data: {data}")

      if not isinstance(data, dict):
        return handle_error(
            callback_context, f"Expected dict response, got {type(data)}"
        )

      # Check for API error response
      if "error" in data or "errorCode" in data or "errors" in data:
        return handle_error(callback_context, f"API returned error: {data}")

      # Extract the text message from JSON-RPC result
      # CES OpenAPI toolsets may return the full JSON-RPC envelope OR just
      # the unwrapped result object depending on runtime behavior.
      a2a_text = ""

      # Path 1: data.message.parts[0].text
      msg = data.get("message", {})
      if isinstance(msg, dict):
        parts = msg.get("parts", [])
        if parts and isinstance(parts, list) and isinstance(parts[0], dict):
          a2a_text = parts[0].get("text", "")

      # Path 2: data.result.message.parts[0].text (full JSON-RPC envelope)
      if not a2a_text:
        result = data.get("result", {})
        if isinstance(result, dict):
          msg = result.get("message", {})
          if isinstance(msg, dict):
            parts = msg.get("parts", [])
            if parts and isinstance(parts, list) and isinstance(parts[0], dict):
              a2a_text = parts[0].get("text", "")

          # Path 3: data.result.parts[0].text
          if not a2a_text:
            parts = result.get("parts", [])
            if parts and isinstance(parts, list) and isinstance(parts[0], dict):
              a2a_text = parts[0].get("text", "")

      # Path 4: data.parts[0].text (CES may unwrap and pass result directly)
      if not a2a_text:
        parts = data.get("parts", [])
        if parts and isinstance(parts, list) and isinstance(parts[0], dict):
          a2a_text = parts[0].get("text", "")

      # Path 5: data.response (string) or data.response.response / .text
      # Some A2A envelopes wrap the payload under a `response` key (optionally
      # nested a second level) rather than the message.parts structure.
      if not a2a_text:
        resp = data.get("response")
        if isinstance(resp, str):
          a2a_text = resp
        elif isinstance(resp, dict):
          for _key in ("response", "text", "message", "output", "result", "content"):
            _val = resp.get(_key)
            if isinstance(_val, str) and _val.strip():
              a2a_text = _val
              break

      print(f"[connect_network_after] Extracted a2a_text length: {len(a2a_text)}, first 200 chars: {a2a_text[:200]}")

      # Try parsing the inner message text as JSON. The network specialist LLM
      # frequently wraps its JSON in a ```json ... ``` fence and appends trailing
      # prose (e.g. a "## Tool Calls Summary" block), so parse leniently rather
      # than requiring the whole payload to be valid JSON.
      inner_data = _parse_json_lenient(a2a_text) or {}

      if not inner_data and isinstance(data, dict):
        if "Severity" in data or "Recommendation" in data:
          inner_data = data

      # Fallback: check if the result object itself contains the analysis fields
      if not inner_data:
        result = data.get("result", data)
        if isinstance(result, dict) and ("Severity" in result or "Recommendation" in result):
          inner_data = result

      # Last resort: scan every string value in the envelope (one level deep) for
      # an embedded JSON object. Covers unexpected envelope shapes and payloads
      # that are only partially JSON.
      if not inner_data and isinstance(data, dict):
        for _v in data.values():
          if isinstance(_v, str):
            _candidate = _parse_json_lenient(_v)
            if _candidate:
              inner_data = _candidate
              break
          elif isinstance(_v, dict):
            for _vv in _v.values():
              if isinstance(_vv, str):
                _candidate = _parse_json_lenient(_vv)
                if _candidate:
                  inner_data = _candidate
                  break
            if inner_data:
              break

      if not inner_data:
        print(
            "[connect_network_after] No parseable analysis found."
            f" a2a_text[:500]={a2a_text[:500]!r}"
            f" data_keys={list(data.keys()) if isinstance(data, dict) else type(data)}"
        )
        return handle_error(
            callback_context,
            f"Failed to extract network analysis data from response. Data keys: {list(data.keys()) if isinstance(data, dict) else 'not_dict'}",
        )

      # Determine network status based on response
      parsed_status = "healthy"
      tech_type = _extract_technician_type(inner_data)
      explicit_status = _extract_network_status(inner_data)
      print(
          f"[connect_network_after] Extracted Technician Type: '{tech_type}',"
          f" explicit network_status: '{explicit_status}'"
      )

      if tech_type.lower() in ("network tech", "install and repair tech"):
        parsed_status = "impaired"
      elif explicit_status in ("impaired", "unhealthy", "degraded"):
        parsed_status = "impaired"
      elif explicit_status == "error":
        parsed_status = "error"

      # Robust, punctuation-tolerant phrase detection: use the structured
      # technician type, and also scan the raw analysis payload. The connect agent
      # emits the recommendation in inconsistent shapes/spellings
      # ("Install & Repair Tech", "Install and Repair Tech", etc.) that exact
      # matching misses. Collapse to alphanumerics before comparing.
      def _squash(value):
        return "".join(ch for ch in str(value).lower() if ch.isalnum())

      def _has_network_tech(s):
        return "networktech" in s

      def _has_install_repair(s):
        return "installandrepair" in s or "installrepair" in s

      try:
        analysis_squash = _squash(json.dumps(inner_data))
      except Exception:
        analysis_squash = _squash(inner_data)
      norm_squash = _squash(tech_type)

      is_network_tech = _has_network_tech(norm_squash) or _has_network_tech(analysis_squash)
      is_install_repair = _has_install_repair(norm_squash) or _has_install_repair(analysis_squash)
      if is_network_tech and is_install_repair:
        struct_net = _has_network_tech(norm_squash)
        struct_ir = _has_install_repair(norm_squash)
        if struct_ir and not struct_net:
          is_network_tech = False
        elif struct_net and not struct_ir:
          is_install_repair = False
        else:
          is_network_tech = False

      matched_activity_type = ""
      matched_activity_code = ""
      matched_job_type = ""
      matched_problem_code = ""
      matched_intents = ""

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

        # Apply appointment defaults (same pattern as check_convoy_recommendations)
        if not matched_activity_code:
          matched_activity_code = "H2"
        if not matched_job_type:
          matched_job_type = "Test"
        if not matched_activity_type:
          matched_activity_type = "TROUBLE_CALL"

        callback_context.state["activityCode"] = matched_activity_code
        callback_context.state["jobType"] = matched_job_type
        callback_context.state["activityType"] = matched_activity_type
        # Optional appointment-routing attributes — propagated the same way as
        # activityCode/jobType (flow through as empty when the rec omits them).
        callback_context.state["problemCode"] = matched_problem_code
        callback_context.state["intents"] = matched_intents
      print(
          "[connect_network_after] Propagated dynamic transfer values to state:"
          f" is_network_tech={is_network_tech},"
          f" activityCode='{matched_activity_code}',"
          f" jobType='{matched_job_type}',"
          f" activityType='{matched_activity_type}',"
          f" problemCode='{matched_problem_code}',"
          f" intents='{matched_intents}'"
      )

      print(f"[connect_network_after] Parsed network status: {parsed_status}")

      # Set state variables
      try:
        current_status = callback_context.state.get("network_status", "")

        if parsed_status == "healthy" and current_status == "impaired":
          print(
              "[connect_network_after] Network status is already 'impaired'."
              " Not overwriting with 'healthy'."
          )
        else:
          callback_context.state["network_status"] = parsed_status

        # NOTE: Do NOT set network_notified here — the orchestrator's
        # after_tool_callback manages notifications to the UI.
        print("[connect_network_after] Set state variables successfully")
      except Exception as e:
        print(f"[connect_network_after] Could not set state variables: {e}")

      # The parent-level after_tool_callback manages the checklist notifications now

      return {
          "status": "success",
          "network_status": callback_context.state.get(
              "network_status", parsed_status
          ),
          "analysis_response": inner_data,
      }

    except Exception as e:
      traceback.print_exc()
      return handle_error(
          callback_context, f"Failed to parse Network response: {str(e)}"
      )


def handle_error(
    callback_context: CallbackContext, error_msg: str
) -> dict[str, Any]:
  print(f"[connect_network_after] Error: {error_msg}")
  try:
    callback_context.state["network_status"] = "error"
    # NOTE: Do NOT set network_notified here — the orchestrator's
    # after_tool_callback manages notifications to the UI.
    print("[connect_network_after] Set network_status to error")
  except Exception as e:
    print(f"[connect_network_after] Could not set state variables: {e}")

  # The parent-level after_tool_callback manages the error notifications now

  return {
      "status": "error",
      "error": error_msg,
  }
