# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught,redefined-builtin,unused-argument

"""PolySynth callback function optimized with real-time concurrent notification dispatches and dynamic MAC-Less fallbacks."""

# pylint: disable=undefined-variable

import json
import traceback
from typing import Any, Optional


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

      print(f"[connect_network_after] Extracted a2a_text length: {len(a2a_text)}, first 200 chars: {a2a_text[:200]}")

      # Try parsing the inner message text as JSON
      inner_data = {}
      if a2a_text:
        try:
          cleaned_text = a2a_text.strip()
          # Handle case where there's prose before the ```json block
          if "```json" in cleaned_text:
            json_start = cleaned_text.index("```json") + 7
            json_end = cleaned_text.index("```", json_start) if "```" in cleaned_text[json_start:] else len(cleaned_text)
            cleaned_text = cleaned_text[json_start:json_end].strip()
          elif cleaned_text.startswith("```"):
            cleaned_text = cleaned_text[3:]
            if cleaned_text.endswith("```"):
              cleaned_text = cleaned_text[:-3]
            cleaned_text = cleaned_text.strip()
          inner_data = json.loads(cleaned_text)
        except Exception as e:
          print(f"[connect_network_after] Failed to parse inner A2A text: {e}")

      if not inner_data and isinstance(data, dict):
        if "Severity" in data or "Recommendation" in data:
          inner_data = data

      # Fallback: check if the result object itself contains the analysis fields
      if not inner_data:
        result = data.get("result", data)
        if isinstance(result, dict) and ("Severity" in result or "Recommendation" in result):
          inner_data = result

      # Last resort: try to find JSON in the entire string representation
      if not inner_data and a2a_text:
        print(f"[connect_network_after] All parse attempts failed. a2a_text: {a2a_text[:500]}")
        return handle_error(
            callback_context,
            f"Failed to extract network analysis data. a2a_text present but unparseable. Length: {len(a2a_text)}",
        )

      if not inner_data:
        print(f"[connect_network_after] No a2a_text found. Full data keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        return handle_error(
            callback_context,
            f"Failed to extract network analysis data from response. No text found in any path. Data keys: {list(data.keys()) if isinstance(data, dict) else 'not_dict'}",
        )

      # Determine network status based on response
      parsed_status = "healthy"
      rec = inner_data.get("Recommendation", {})
      tech_type = ""
      if isinstance(rec, dict):
        tech_type = str(rec.get("Technician Type", "")).strip()
      print(f"[connect_network_after] Extracted Technician Type: '{tech_type}'")

      if tech_type.lower() in ("network tech", "install and repair tech"):
        parsed_status = "impaired"

      # Deterministically set activityType, activityCode, jobType based on technician type
      matched_activity_type = ""
      matched_activity_code = ""
      matched_job_type = ""

      if tech_type.lower() == "network tech":
        matched_activity_type = "SPECIAL_REQUEST"
        matched_activity_code = "PR"
        matched_job_type = "PR"
      elif tech_type.lower() == "install and repair tech":
        matched_activity_type = "TROUBLE_CALL"
        matched_activity_code = "H3"
        matched_job_type = "AO"

      # Apply defaults if not matched (same pattern as check_convoy_recommendations)
      if not matched_activity_code:
        matched_activity_code = "H2"
      if not matched_job_type:
        matched_job_type = "Test"
      if not matched_activity_type:
        matched_activity_type = "TROUBLE_CALL"

      callback_context.state["activityCode"] = matched_activity_code
      callback_context.state["jobType"] = matched_job_type
      callback_context.state["activityType"] = matched_activity_type
      print(
          "[connect_network_after] Propagated dynamic transfer values to state:"
          f" activityCode='{matched_activity_code}',"
          f" jobType='{matched_job_type}',"
          f" activityType='{matched_activity_type}'"
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
