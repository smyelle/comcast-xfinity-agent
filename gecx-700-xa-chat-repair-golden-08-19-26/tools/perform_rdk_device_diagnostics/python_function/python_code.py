# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

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

"""perform_rdk_device_diagnostics — Retry-capable wrapper for the execute_rdk_device_diagnostics_ViaAuthProxy OpenAPI toolset.

Invokes the RDK MCP server through the Apigee auth-proxy using a JSON-RPC
tools/call request, with exponential-backoff retry logic to handle
intermittent 500 errors.
"""

import json
import time
from typing import Any

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1

VALID_OPERATIONS = (
    "get_device_triage_summary",
    "get_gateway_wifi_summary",
)


def perform_rdk_device_diagnostics(
    device_identifier: str,
    problem_statement: str,
    operation_id: str,
    timestamp: str,
) -> dict[str, Any]:
  """Retry-capable wrapper for RDK device diagnostics via OpenAPI auth-proxy.

  Args:
      device_identifier: The gateway's MAC address (e.g. "10:56:11:8A:66:ED").
      problem_statement: Description of the customer's issue (e.g. "slow
        internet").
      operation_id: The MCP tool name to invoke (e.g.
        "get_device_triage_summary", "get_gateway_wifi_summary").
      timestamp: The current formatted date/time string.

  Returns:
      dict[str, Any]: The JSON-RPC response from the upstream MCP server or an
      error dict.
  """
  if not device_identifier:
    return {
        "status": "error",
        "error": "device_identifier is required.",
        "agent_action": (
            "Ask the customer to confirm their gateway MAC address."
        ),
    }

  if not operation_id or operation_id not in VALID_OPERATIONS:
    return {
        "status": "error",
        "error": (
            f"Invalid operation_id '{operation_id}'. Must be one of:"
            f" {VALID_OPERATIONS}"
        ),
    }

  # Build the JSON-RPC request arguments for the OpenAPI toolset
  _audit_request = {
      "device_identifier": device_identifier,
      "problem_statement": problem_statement,
      "operation_id": operation_id,
      "timestamp": timestamp,
  }
  print(
      "[AUDIT] [perform_rdk_device_diagnostics] >>> Request Payload:",
      f" {_audit_request}",
  )
  rdk_mcp_server = str(context.state.get("rdk_mcp_server") or "").rstrip("/")
  if not rdk_mcp_server:
    print("[ERROR] [perform_rdk_device_diagnostics] rdk_mcp_server variable is missing from context state!")
    _audit_response = {
        "status": "error",
        "error": "Missing required server configuration: 'rdk_mcp_server'",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [perform_rdk_device_diagnostics] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  xa_session_id = context.state.get("xa_session_id") or ""
  tool_args = {
      "x-auth": "MCP-SAT-XAXLR",
      "x-scope": "rdkmcpserver:access",
      "x-url": f"{rdk_mcp_server}/tool/gru/mcp",
      "agent_name": "gecx_repair_agent",
      "x-flow-trace-id": xa_session_id,
      "jsonrpc": "2.0",
      "id": 1,
      "method": "tools/call",
      "params": {
          "name": operation_id,
          "arguments": {
              "device_identifier": device_identifier,
              "problem_statement": problem_statement,
              "timestamp": timestamp,
          },
      },
  }

  print(
      f"[perform_rdk_device_diagnostics] Calling operation: {operation_id} for"
      f" device: {device_identifier}"
  )

  last_error = None
  for attempt in range(1, MAX_RETRIES + 1):
    try:
      if operation_id == "get_device_triage_summary":
        # Record API call performance & raw payloads
        _api_start = time.time()
        response = tools.execute_rdk_device_diagnostics_ViaAuthProxy_getDeviceTriageSummaryViaAuthProxy(
            tool_args
        )
        _api_end = time.time()
        _api_latency_ms = int((_api_end - _api_start) * 1000)
        print(
            "[AUDIT] [LATENCY] [perform_rdk_device_diagnostics] API took:"
            f" {_api_latency_ms} ms"
        )
        print(
            "[AUDIT] [API REQUEST] [perform_rdk_device_diagnostics] >>>:"
            f" {tool_args}"
        )
        if hasattr(response, "status_code"):
          print(
              "[AUDIT] [HTTP STATUS] [perform_rdk_device_diagnostics] :"
              f" {response.status_code} - {getattr(response, 'reason', 'N/A')}"
          )
        if hasattr(response, "text"):
          print(
              "[AUDIT] [API RESPONSE] [perform_rdk_device_diagnostics] <<<:"
              f" {response.text}"
          )
      elif operation_id == "get_gateway_wifi_summary":
        # Record API call performance & raw payloads
        _api_start = time.time()
        response = tools.execute_rdk_gateway_wifisummary_ViaAuthProxy_getGatewayWifiSummary(
            tool_args
        )
        _api_end = time.time()
        _api_latency_ms = int((_api_end - _api_start) * 1000)
        print(
            "[AUDIT] [LATENCY] [perform_rdk_device_diagnostics] API took:"
            f" {_api_latency_ms} ms"
        )
        print(
            "[AUDIT] [API REQUEST] [perform_rdk_device_diagnostics] >>>:"
            f" {tool_args}"
        )
        if hasattr(response, "status_code"):
          print(
              "[AUDIT] [HTTP STATUS] [perform_rdk_device_diagnostics] :"
              f" {response.status_code} - {getattr(response, 'reason', 'N/A')}"
          )
        if hasattr(response, "text"):
          print(
              "[AUDIT] [API RESPONSE] [perform_rdk_device_diagnostics] <<<:"
              f" {response.text}"
          )

      # Serialize to JSON-safe dict
      serialized = _serialize_response(response)

      # Check if response is a retryable error
      if _is_retryable_error(serialized):
        last_error = serialized
        if attempt < MAX_RETRIES:
          backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
          print(
              f"[perform_rdk_device_diagnostics] Attempt {attempt} failed with"
              f" transient error, retrying in {backoff}s..."
          )
          time.sleep(backoff)
          continue
        else:
          break

      # Successful response
      print(f"[perform_rdk_device_diagnostics] Success on attempt {attempt}")
      _audit_response = serialized
      print(
          "[AUDIT] [perform_rdk_device_diagnostics] <<< Response Payload:",
          f" {_audit_response}",
      )
      return _audit_response

    except Exception as e:
      last_error = str(e)
      if attempt < MAX_RETRIES:
        backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
        print(
            f"[perform_rdk_device_diagnostics] Attempt {attempt} raised"
            f" exception: {e}, retrying in {backoff}s..."
        )
        time.sleep(backoff)
      else:
        break

  # All retries exhausted
  _audit_response = {
      "status": "error",
      "error": (
          f"RDK device diagnostics ({operation_id}) failed after {MAX_RETRIES}"
          f" attempts. Last error: {last_error}"
      ),
      "agent_action": (
          "Inform the customer that device diagnostics are temporarily"
          " unavailable. Offer to proceed with available information or try"
          " again shortly."
      ),
  }
  print(
      "[AUDIT] [perform_rdk_device_diagnostics] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response


def _serialize_response(response: str) -> dict[str, Any]:
  """Convert a ces_internal.ExternalResponse (or any non-dict) to a JSON-serializable dict."""
  if response is None:
    return {"status": "error", "error": "Received null response from tool."}
  if isinstance(response, dict):
    return response
  if hasattr(response, "to_dict"):
    return response.to_dict()
  if hasattr(response, "text"):
    try:
      return json.loads(response.text)
    except (json.JSONDecodeError, TypeError):
      return {"status": "success", "raw_response": str(response.text)}
  if hasattr(response, "__dict__"):
    return {k: v for k, v in vars(response).items() if not k.startswith("_")}
  return {"status": "success", "raw_response": str(response)}


def _is_retryable_error(response: dict[str, Any]) -> bool:
  """Check if the response indicates a transient/retryable failure."""
  if isinstance(response, dict):
    error_msg = str(response.get("error", ""))
    if "statusCode=500" in error_msg:
      return True
    if "Failed to send message" in error_msg:
      return True
    if "DummyEvent" in error_msg:
      return True
    # JSON-RPC error with upstream failure
    if (
        response.get("error", {})
        if isinstance(response.get("error"), dict)
        else None
    ):
      err_obj = response["error"]
      code = err_obj.get("code", 0)
      if code in (-32000, -32603):  # server error / internal error
        return True
  return False
