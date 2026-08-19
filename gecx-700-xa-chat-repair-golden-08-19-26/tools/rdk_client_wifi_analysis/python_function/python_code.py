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

"""rdk_client_wifi_analysis — Retry-capable wrapper for RDK client WiFi analysis MCP toolset.

Wraps run_rdk_client_wifi_analysis toolset operation (query_wifi_agent)
with exponential-backoff retry logic to handle intermittent 500 errors
import time
from the Apigee auth-proxy.
"""

import json
import time
from typing import Any

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1


def rdk_client_wifi_analysis(query: str) -> dict[str, Any]:
  """Retry-capable wrapper for RDK client WiFi analysis MCP toolset.

  Args:
      query: The device MAC address or query string for WiFi analysis.

  Returns:
      dict[str, Any]: The MCP tool response or an error dict with retry details.
  """
  if not query:
    return {
        "status": "error",
        "error": "query is required.",
        "agent_action": (
            "Ask the customer to confirm their gateway MAC address."
        ),
    }

  _audit_request = {
      "query": query,
  }
  print(
      "[AUDIT] [rdk_client_wifi_analysis] >>> Request Payload:",
      f" {_audit_request}",
  )
  tool_args = {"query": query}

  last_error = None
  for attempt in range(1, MAX_RETRIES + 1):
    try:
      # Record API call performance & raw payloads
      _api_start = time.time()
      response = tools.run_rdk_client_wifi_analysis_query_wifi_agent(tool_args)
      _api_end = time.time()
      _api_latency_ms = int((_api_end - _api_start) * 1000)
      print(
          "[AUDIT] [LATENCY] [rdk_client_wifi_analysis] API took:"
          f" {_api_latency_ms} ms"
      )
      print(
          f"[AUDIT] [API REQUEST] [rdk_client_wifi_analysis] >>>: {tool_args}"
      )
      if hasattr(response, "status_code"):
        print(
            "[AUDIT] [HTTP STATUS] [rdk_client_wifi_analysis] :"
            f" {response.status_code} - {getattr(response, 'reason', 'N/A')}"
        )
      if hasattr(response, "text"):
        print(
            "[AUDIT] [API RESPONSE] [rdk_client_wifi_analysis] <<<:"
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
              f"[rdk_client_wifi_analysis] Attempt {attempt} failed with"
              f" transient error, retrying in {backoff}s..."
          )
          time.sleep(backoff)
          continue
        else:
          break

      # Successful response
      _audit_response = serialized
      print(
          "[AUDIT] [rdk_client_wifi_analysis] <<< Response Payload:",
          f" {_audit_response}",
      )
      return _audit_response

    except Exception as e:
      last_error = str(e)
      if attempt < MAX_RETRIES:
        backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
        print(
            f"[rdk_client_wifi_analysis] Attempt {attempt} raised exception:"
            f" {e}, retrying in {backoff}s..."
        )
        time.sleep(backoff)
      else:
        break

  # All retries exhausted
  _audit_response = {
      "status": "error",
      "error": (
          f"RDK client WiFi analysis failed after {MAX_RETRIES} attempts. Last"
          f" error: {last_error}"
      ),
      "agent_action": (
          "Inform the customer that WiFi analysis is temporarily unavailable."
          " Offer to proceed with other diagnostic results or try again"
          " shortly."
      ),
  }
  print(
      "[AUDIT] [rdk_client_wifi_analysis] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response


def _serialize_response(response: Any) -> dict[str, Any]:
  """Convert a ces_internal.ExternalResponse (or any non-dict) to a JSON-serializable dict."""
  if response is None:
    return {"status": "error", "error": "Received null response from tool."}
  if isinstance(response, dict):
    return response
  # Try common serialization approaches
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
    error_msg = response.get("error", "")
    if "statusCode=500" in str(error_msg):
      return True
    if "Failed to send message" in str(error_msg):
      return True
    if "DummyEvent" in str(error_msg):
      return True
  return False
