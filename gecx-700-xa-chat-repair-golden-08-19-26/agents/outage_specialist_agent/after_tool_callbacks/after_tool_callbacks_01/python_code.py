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

  The actual result from the tool is provided in the 'tool_response' parameter.
  By returning 'None', we signal that the 'tool_response' is approved and
  should be passed back to the LLM without modification.

  If we were to return a dictionary here, it would *replace* the original
  'tool_response'. This is useful for sanitizing, formatting, or enriching
  the data returned from a tool.

  Args:
    tool: The tool description object.
    input: The input arguments to the tool.
    callback_context: The execution context containing state memory.
    tool_response: The actual response returned from the tool execution.

  Returns:
    The modified response dictionary, or None to approve the response as-is.
  """
  # Storing the tool request and response in state for tracking
  try:
    request_name = tool.name + "_request"
    response_name = tool.name + "_response"
    callback_context.state[request_name] = input
    callback_context.state[response_name] = tool_response
  except Exception as e:
    traceback.print_exc()
    print(f"[check_outage_after] Error Saving the tool request and response in state")

      # Storing the tool request and response in state for tracking
  try:
    request_name = tool.name + "_request"
    response_name = tool.name + "_response"
    callback_context.state[request_name] = input
    callback_context.state[response_name] = response
  except Exception as e:
    traceback.print_exc()
    print(f"[check_outage_after] Error Saving the tool request and response in state")
    
  if tool.name == "ede_outage_api_checkOutageEde":
    response = tool_response

    print(f"[check_outage_after] received response: {response}")

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

      print(f"[check_outage_after] Parsed data: {data}")

      if not isinstance(data, dict):
        return handle_error(
            callback_context, f"Expected dict response, got {type(data)}"
        )

      # Check for API error response
      if "error" in data or "errorCode" in data or "errors" in data:
        return handle_error(callback_context, f"API returned error: {data}")

      # Extract recommendations
      recommendations = []
      result = data.get("result")
      if result is not None:
        if isinstance(result, list):
          for item in result:
            if isinstance(item, dict):
              ede_response = item.get("edeResponse", {})
              if isinstance(ede_response, dict):
                recs = ede_response.get("recommendations", [])
                if isinstance(recs, list):
                  recommendations.extend(recs)
        elif isinstance(result, dict):
          ede_response = result.get("edeResponse", {})
          if isinstance(ede_response, dict):
            recs = ede_response.get("recommendations", [])
            if isinstance(recs, list):
              recommendations.extend(recs)
      else:
        # Try alternative paths
        data_array = data.get("data", [])
        if isinstance(data_array, list):
          for item in data_array:
            if isinstance(item, dict):
              ede_response = item.get("edeResponse", {})
              if isinstance(ede_response, dict):
                recs = ede_response.get("recommendations", [])
                if isinstance(recs, list):
                  recommendations.extend(recs)

        if not recommendations:
          top_recs = data.get("recommendations", [])
          if isinstance(top_recs, list):
            recommendations = top_recs

      print(f"[check_outage_after] Found {len(recommendations)} recommendations")

      outage_detected = False
      outage_message = ""
      customer_message = ""
      impacted_services = []

      # Search for ACTIVE_INTERNET_OUTAGE
      for rec in recommendations:
        if not isinstance(rec, dict):
          continue
        rec_name = rec.get("name", "") or rec.get("key", "") or ""
        if "ACTIVE_INTERNET_OUTAGE" in rec_name.upper():
          outage_detected = True
          additional_data = rec.get("additionalData", {})
          if isinstance(additional_data, dict):
            outage_message = additional_data.get("adkOutageDetailsMessage", "")
            customer_message = additional_data.get("adkCustomerMessage", "")
            impacted_services_str = additional_data.get("impactedServices", "")
            if isinstance(impacted_services_str, str) and impacted_services_str:
              impacted_services = [
                  s.strip() for s in impacted_services_str.split(",")
              ]
            elif isinstance(impacted_services_str, list):
              impacted_services = impacted_services_str
          break

      # Apply fallbacks
      if outage_detected:
        if not outage_message:
          outage_message = (
              "Your area is currently experiencing a service outage. Our teams"
              " are working to restore your services."
          )
        if not customer_message:
          customer_message = (
              "During an outage, we are unable to connect you with a live agent,"
              " as any troubleshooting would not bring your services back"
              " online."
          )

      print(f"[check_outage_after] outage_detected: {outage_detected}")

      # Set state variables
      try:
        callback_context.state["outage_detected"] = (
            "true" if outage_detected else "false"
        )
        callback_context.state["outage_message"] = outage_message
        callback_context.state["customer_message"] = customer_message
        callback_context.state["impacted_services"] = (
            ",".join(impacted_services) if impacted_services else ""
        )
        callback_context.state["outage_status"] = (
            "active" if outage_detected else "none"
        )
        callback_context.state["outage_notified"] = "true"
        print("[check_outage_after] Set callback_context.state variables successfully")
      except Exception as e:
        print(f"[check_outage_after] Could not set state variables: {e}")

      # The parent-level after_tool_callback manages the checklist notifications now

      return {
          "status": "success",
          "outage_detected": outage_detected,
          "outage_message": outage_message,
          "customer_message": customer_message,
          "impacted_services": impacted_services,
      }

    except Exception as e:
      traceback.print_exc()
      return handle_error(callback_context, f"Failed to parse EDE response: {str(e)}")


def handle_error(
    callback_context: CallbackContext, error_msg: str
) -> dict[str, Any]:
  print(f"[check_outage_after] Error: {error_msg}")
  try:
    callback_context.state["outage_status"] = "error"
    callback_context.state["outage_notified"] = "true"
    # Set default values for other variables to avoid undefined issues
    callback_context.state["outage_detected"] = "false"
    callback_context.state["outage_message"] = ""
    callback_context.state["customer_message"] = ""
    callback_context.state["impacted_services"] = ""
    print("[check_outage_after] Set outage_status to error")
  except Exception as e:
    print(f"[check_outage_after] Could not set state variables: {e}")

  # The parent-level after_tool_callback manages the error notifications now

  return {
      "status": "error",
      "error": error_msg,
  }
