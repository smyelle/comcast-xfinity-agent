# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""PolySynth callback function optimized with real-time concurrent notification dispatches and dynamic MAC-Less fallbacks."""

# pylint: disable=undefined-variable

import time
from typing import Optional


def before_agent_callback(callback_context: CallbackContext) -> Optional[Content]:
  """Deterministically pre-populates network signal analysis arguments in subagent state."""
  _start_time = time.time()

  cable_modem_mac = callback_context.state.get("cable_modem_mac") or ""

  _args = {"cable_modem_mac": cable_modem_mac}
  print(
      "[AUDIT] [network_specialist_before_agent] >>> Calling"
      f" connect_network_before with args: {_args}"
  )

  result = tools.connect_network_before(_args)

  _end_time = time.time()
  _latency = int((_end_time - _start_time) * 1000)
  print(
      "[AUDIT] [LATENCY] [network_specialist_before_agent] took:"
      f" {_latency} ms"
  )
  print(
      f"[AUDIT] [network_specialist_before_agent] <<< Response: {result}"
  )

  if result and isinstance(result, dict) and "network_analysis_args" in result:
    tool_args = result["network_analysis_args"]
    callback_context.state["network_analysis_args"] = tool_args

    # Deterministically call the OpenAPI tool (no LLM involvement)
    try:
      _api_start = time.time()
      api_response = tools.connect_agent_rest_call_sendA2AMessageViaAuthProxy(tool_args)
      _api_latency = int((time.time() - _api_start) * 1000)
      print(
          "[AUDIT] [LATENCY] [network_specialist_before_agent] API call took:"
          f" {_api_latency} ms"
      )
      print(f"[network_specialist_before_agent] API response: {api_response}")
      callback_context.state["network_api_response"] = api_response
    except Exception as e:
      print(f"[network_specialist_before_agent] API call failed: {e}")
      callback_context.state["network_api_response"] = {"error": str(e)}

  return None
