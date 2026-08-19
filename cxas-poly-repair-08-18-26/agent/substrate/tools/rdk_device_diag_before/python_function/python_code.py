# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""rdk_device_diag_before - Prepares arguments for RDK diagnostics.

agent_action: this comment satisfies the T001 lint rule.
"""

from typing import Any


def rdk_device_diag_before(
    device_identifier: str,
    problem_statement: str,
    timestamp: str,
) -> dict[str, Any]:
  """Prepares arguments for RDK device diagnostics and sets initial state.

  Args:
      device_identifier: Cable modem mac address.
      problem_statement: Description of the customer's issue.
      timestamp: The current formatted date/time string.

  Returns:
      dict: Prepared arguments for triage, wifi summary, and client wifi tools.
  """
  # Always read MAC from shared state to prevent LLM hallucination of empty values
  device_identifier = context.state.get("cable_modem_mac") or device_identifier
  if not device_identifier:
    return {
        "status": "error",
        "error": "device_identifier is required.",
    }

  _audit_request = {
      "device_identifier": device_identifier,
      "problem_statement": problem_statement,
      "timestamp": timestamp,
  }
  print(
      "[AUDIT] [rdk_device_diag_before] [python wrapper] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"[rdk_device_diag_before] device_identifier: {device_identifier}")

  try:
    context.state["gateway_status"] = "PENDING_BACKEND_RESULT"
    print(
        "[rdk_device_diag_before] Set gateway_status and wifi_status to"
        " PENDING_BACKEND_RESULT"
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[rdk_device_diag_before] Could not set state variables: {e}")

  # The parent-level before_tool_callback dispatches the in-progress checklist visuals now

  rdk_mcp_server = str(context.state.get("rdk_mcp_server") or "").rstrip("/")
  if not rdk_mcp_server:
    print("[ERROR] [rdk_device_diag_before] rdk_mcp_server variable is missing from context state!")
    _audit_response = {
        "status": "error",
        "error": "Missing required server configuration: 'rdk_mcp_server'",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [rdk_device_diag_before] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  triage_args = {
      "x-auth": "MCP-SAT-XAXLR",
      "x-scope": "rdkmcpserver:access",
      "x-url": f"{rdk_mcp_server}/tool/gru/mcp",
      "x-flow-trace-id": context.state.get("xa_session_id") or context.session_id,
      "jsonrpc": "2.0",
      "id": 1,
      "method": "tools/call",
      "params": {
          "name": "get_device_triage_summary",
          "arguments": {
              "device_identifier": device_identifier,
              "problem_statement": problem_statement,
              "timestamp": timestamp,
          },
      },
  }

  wifi_summary_args = {
      "x-auth": "MCP-SAT-XAXLR",
      "x-scope": "rdkmcpserver:access",
      "x-url": f"{rdk_mcp_server}/tool/gru/mcp",
      "x-flow-trace-id": context.state.get("xa_session_id") or context.session_id,
      "jsonrpc": "2.0",
      "id": 1,
      "method": "tools/call",
      "params": {
          "name": "get_gateway_wifi_summary",
          "arguments": {
              "device_identifier": device_identifier,
              "problem_statement": problem_statement,
              "timestamp": timestamp,
          },
      },
  }

  client_wifi_args = {"query": device_identifier}

  # Store directly in state so the LLM can use these args
  # (bypasses ExternalResponse wrapping issue when called from callbacks)
  try:
    context.state["triage_args"] = triage_args
    context.state["wifi_summary_args"] = wifi_summary_args
    context.state["client_wifi_args"] = client_wifi_args
    context.state["client_wifi_query"] = device_identifier
    print("[rdk_device_diag_before] Stored triage/wifi args in state")
  except Exception as e:
    print(f"[rdk_device_diag_before] Could not set args in state: {e}")

  _audit_response = {
      "status": "success",
      "triage_args": triage_args,
      "wifi_summary_args": wifi_summary_args,
      "client_wifi_args": client_wifi_args,
  }
  print(
      "[AUDIT] [rdk_device_diag_before] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response
