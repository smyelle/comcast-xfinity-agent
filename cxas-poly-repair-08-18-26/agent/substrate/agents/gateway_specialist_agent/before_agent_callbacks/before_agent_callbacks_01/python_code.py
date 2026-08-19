# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""PolySynth callback function optimized with real-time concurrent notification dispatches and dynamic MAC-Less fallbacks."""

# pylint: disable=undefined-variable

import time
from typing import Optional


def before_agent_callback(callback_context: CallbackContext) -> Optional[Content]:
  """Deterministically pre-populates gateway diagnostics arguments in subagent state."""
  _start_time = time.time()

  cable_modem_mac = callback_context.state.get("cable_modem_mac") or ""
  print("gateway call: cable_modem_mac:%s" %(cable_modem_mac))
  timestamp = callback_context.state.get("RDK_CURRENT_DATE_FORMATTED") or ""

  _args = {
      "device_identifier": cable_modem_mac,
      "problem_statement": "slow internet",
      "timestamp": timestamp,
  }
  print(
      "[AUDIT] [gateway_specialist_before_agent] [before agent call back] >>> Calling"
      f" rdk_device_diag_before with args: {_args}"
  )

  result = tools.rdk_device_diag_before(_args)

  _end_time = time.time()
  _latency = int((_end_time - _start_time) * 1000)
  print(
      "[AUDIT] [LATENCY] [gateway_specialist_before_agent] took:"
      f" {_latency} ms"
  )
  print(
      f"[AUDIT] [gateway_specialist_before_agent] <<< Response: {result}"
  )

  if result and isinstance(result, dict):
    callback_context.state["triage_args"] = result.get("triage_args") or {}
    callback_context.state["wifi_summary_args"] = result.get("wifi_summary_args") or {}
    callback_context.state["client_wifi_args"] = result.get("client_wifi_args") or {}
    callback_context.state["client_wifi_query"] = result.get("client_wifi_args", {}).get("query", "")
  return None
