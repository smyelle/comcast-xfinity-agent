# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

import json
import time
import traceback
from typing import Any

# Repair-relevant recommendation IDs to look for
REPAIR_RECOMMENDATION_IDS = [
    "PREDICTIVE_GATEWAYSWAP",
    "XIT_AIQ_PREDICTIVE_WAN_SCORE",
    "BootfileTroubleshootingDigital",
    "PHTPartialChannelBonding",
    "DigitalFailingWAN",
    "RFCxel",
    "PHTAllOut",
    "OutsideHomeSRO",
    "PHTLite",
    "XIModemOfflineDigital",
    "XITNetworkImpairment",
    "CURRENT_OUTAGE",
]


def check_convoy_recommendations(account_number: str = "") -> dict[str, Any]:
  """Fetches Convoy recommendations and extracts repair-relevant intents.

  Args:
      account_number: The customer billing account number.

  Returns:
      dict with:
          - status: success/error
          - repair_recommendations: list of relevant recs with name,
          activityCode, jobType, description
          - routing_action: suggested action (technician, swap, reboot, or none)
  """
  if not account_number:
    account_number = context.state.get("accountNumber") or context.state.get(
        "account_id", ""
    )

  if not account_number:
    return {
        "status": "error",
        "repair_recommendations": [],
        "routing_action": "none",
        "error": "account_number is required.",
    }

  _audit_request = {
      "account_number": account_number,
  }
  print(
      "[AUDIT] [check_convoy_recommendations] >>> Request Payload:",
      f" {_audit_request}",
  )
  print(f"[check_convoy] account_number: {account_number}")

  convoy_api_server = str(context.state.get("convoy_api_server") or "").rstrip("/")
  if not convoy_api_server:
    print("[ERROR] [check_convoy_recommendations] convoy_api_server variable is missing from context state!")
    _audit_response = {
        "status": "error",
        "repair_recommendations": [],
        "routing_action": "none",
        "error": "Missing required server configuration: 'convoy_api_server'",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [check_convoy_recommendations] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  tool_args = {
      "x-url": f"{convoy_api_server}/convoy/cara/intents/{account_number}",
      "x-auth": "CONVOY-CIMA-XAXLR",
      "x-scope": (
          "urn:convoy:security:token:client-credentials urn:convoy:session#read"
      ),
      "x-cache-refresh": "FORCE-REFRESH",
      "x-flow-trace-id": context.session_id,
      "convoyIntents": ["XA_NEXT_REPAIR"],
      "agentCallCenter": "XA Next",
  }

  try:
    # Record API call performance & raw payloads
    _api_start = time.time()
    response = tools.convoy_recs_account_getRecommendationsByAccount(tool_args)
    _api_end = time.time()
    _api_latency_ms = int((_api_end - _api_start) * 1000)
    print(
        "[AUDIT] [LATENCY] [check_convoy_recommendations] API took:"
        f" {_api_latency_ms} ms"
    )
    print(
        f"[AUDIT] [API REQUEST] [check_convoy_recommendations] >>>: {tool_args}"
    )
    if hasattr(response, "status_code"):
      print(
          "[AUDIT] [HTTP STATUS] [check_convoy_recommendations] :"
          f" {response.status_code} - {getattr(response, 'reason', 'N/A')}"
      )
    if hasattr(response, "text"):
      print(
          "[AUDIT] [API RESPONSE] [check_convoy_recommendations] <<<:"
          f" {response.text}"
      )
    print(f"[check_convoy] response type: {type(response)}")
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[check_convoy] Tool call failed: {e}")
    _audit_response = {
        "status": "error",
        "repair_recommendations": [],
        "routing_action": "none",
        "error": f"Convoy tool call failed: {str(e)}",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [check_convoy_recommendations] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  try:
    data = response
    if hasattr(response, "body"):
      data = response.body
    elif hasattr(response, "text"):
      data = json.loads(response.text)
    elif hasattr(response, "__getitem__"):
      data = response

    if isinstance(data, str):
      data = json.loads(data)

    print(f"[check_convoy] Parsed data type: {type(data)}")

    # Extract recommendations from response
    recommendations = []

    if isinstance(data, dict):
      # Direct structure: { recommendations: [...] }
      recommendations = data.get("recommendations", [])
      if not recommendations and "result" in data:
        result = data["result"]
        if isinstance(result, list):
          recommendations = result
        elif isinstance(result, dict):
          recommendations = result.get("recommendations", [])

    elif isinstance(data, list):
      recommendations = data

    if not isinstance(recommendations, list):
      recommendations = []

    print(f"[check_convoy] Found {len(recommendations)} total recommendations")

    # Filter to repair-relevant recommendations
    repair_recs = []
    for rec in recommendations:
      if not isinstance(rec, dict):
        continue
      # Convoy uses "id" or "recommendationsName" for the rec identifier
      rec_name = (
          rec.get("id", "")
          or rec.get("recommendationsName", "")
          or rec.get("name", "")
          or rec.get("key", "")
          or ""
      )
      if rec_name in REPAIR_RECOMMENDATION_IDS:
        # Extract additional data from additionalInformation aspect array
        additional_info = rec.get("additionalInformation", [])
        aspect_data = {}
        if isinstance(additional_info, list):
          for info_block in additional_info:
            if isinstance(info_block, dict):
              aspects = info_block.get("aspect", [])
              if isinstance(aspects, list):
                for aspect in aspects:
                  if isinstance(aspect, dict):
                    attr = aspect.get("attribute", "")
                    val = aspect.get("value", "")
                    if attr:
                      aspect_data[attr] = val

        # Also check additionalData dict (EDE-style response)
        additional_data = rec.get("additionalData", {}) or {}

        activity_code = aspect_data.get(
            "activityCode", ""
        ) or additional_data.get("activityCode", "")
        job_type = aspect_data.get("jobType", "") or additional_data.get(
            "jobType", ""
        )
        activity_type = aspect_data.get(
            "activityType", ""
        ) or additional_data.get("activityType", "")
        description = (
            aspect_data.get("adkCustomerMessage", "")
            or aspect_data.get("adkDescription", "")
            or additional_data.get("adkCustomerMessage", "")
            or rec.get("shortDescription", "")
        )
        recommended_action = aspect_data.get(
            "recommendedAction", ""
        ) or additional_data.get("recommendedAction", "")

        repair_recs.append({
            "name": rec_name,
            "activity_code": activity_code,
            "job_type": job_type,
            "activity_type": activity_type,
            "description": description,
            "recommended_action": recommended_action,
        })

    print(f"[check_convoy] Found {len(repair_recs)} repair recommendations")

    # Determine routing action based on priority and capture matched ticket codes
    routing_action = "none"
    matched_activity_code = ""
    matched_job_type = ""
    matched_activity_type = ""
    matched_description = ""
    matched_recommended_action = ""

    for rec in repair_recs:
      name = rec["name"]
      if name == "PREDICTIVE_GATEWAYSWAP":
        routing_action = "predictive_swap"
        matched_activity_code = rec.get("activity_code", "")
        matched_job_type = rec.get("job_type", "")
        matched_activity_type = rec.get("activity_type", "")
        matched_description = rec.get("description", "")
        matched_recommended_action = rec.get("recommended_action", "")
        break
      elif name in ("OutsideHomeSRO", "XITNetworkImpairment"):
        routing_action = "technician"
        matched_activity_code = rec.get("activity_code", "")
        matched_job_type = rec.get("job_type", "")
        matched_activity_type = rec.get("activity_type", "")
        matched_description = rec.get("description", "")
        matched_recommended_action = rec.get("recommended_action", "")
        break
      elif name == "RFCxel":
        routing_action = "technician"
        matched_activity_code = rec.get("activity_code", "")
        matched_job_type = rec.get("job_type", "")
        matched_activity_type = rec.get("activity_type", "")
        matched_description = rec.get("description", "")
        matched_recommended_action = rec.get("recommended_action", "")
        break
      elif name == "XIModemOfflineDigital":
        routing_action = "technician"
        matched_activity_code = rec.get("activity_code", "")
        matched_job_type = rec.get("job_type", "")
        matched_activity_type = rec.get("activity_type", "")
        matched_description = rec.get("description", "")
        matched_recommended_action = rec.get("recommended_action", "")
        break
      elif name in ("DigitalFailingWAN", "XIT_AIQ_PREDICTIVE_WAN_SCORE"):
        routing_action = "technician"
        matched_activity_code = rec.get("activity_code", "")
        matched_job_type = rec.get("job_type", "")
        matched_activity_type = rec.get("activity_type", "")
        matched_description = rec.get("description", "")
        matched_recommended_action = rec.get("recommended_action", "")
        break
      elif name in ("PHTAllOut", "PHTPartialChannelBonding", "PHTLite"):
        routing_action = "technician"
        matched_activity_code = rec.get("activity_code", "")
        matched_job_type = rec.get("job_type", "")
        matched_activity_type = rec.get("activity_type", "")
        matched_description = rec.get("description", "")
        matched_recommended_action = rec.get("recommended_action", "")
        break

    # Set state variables
    try:
      context.state["convoy_recommendations"] = (
          json.dumps(repair_recs) if repair_recs else ""
      )
      context.state["convoy_routing_action"] = routing_action

      # Propagate matching ticket properties dynamically to context.state
      if not matched_activity_code:
        matched_activity_code = "H2"
      if not matched_job_type:
        matched_job_type = "Test"
      if not matched_activity_type:
        matched_activity_type = "TROUBLE_CALL"

      context.state["activityCode"] = matched_activity_code
      context.state["jobType"] = matched_job_type
      context.state["activityType"] = matched_activity_type
      print(
          "[check_convoy] Propagated dynamic transfer values to context.state:"
          f" activityCode='{matched_activity_code}',"
          f" jobType='{matched_job_type}',"
          f" activityType='{matched_activity_type}'"
      )

      # Store customer-facing message from convoy
      if matched_description:
        context.state["convoy_customer_message"] = matched_description
        print(f"[check_convoy] Set convoy_customer_message: '{matched_description}'")

      # Determine convoy_status based on recommendedAction from additional properties
      if matched_recommended_action.lower() == "createappointment" and routing_action in ("technician", "device_offline"):
        context.state["convoy_status"] = "truck_roll"
        print("[check_convoy] Set convoy_status = 'truck_roll'")
      else:
        context.state["convoy_status"] = "no_recommendation"

      # Set gateway/network status based on routing (for non-truck_roll paths)
      if routing_action == "predictive_swap":
        context.state["gateway_status"] = "predictive_swap"
      elif routing_action == "device_offline":
        context.state["gateway_status"] = "reboot"

      print("[check_convoy] Set context.state variables successfully")
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(f"[check_convoy] Could not set state variables: {e}")

    convoy_status = "clear"
    # `predictive_swap` as well as `swap`. The priority table above is the only producer
    # of `routing_action` and it writes "predictive_swap" -- the bare "swap" spelling is
    # written nowhere, so this branch was unreachable and a PREDICTIVE_GATEWAYSWAP
    # recommendation made the leg publish convoy_status = "clear": a gateway that needs
    # replacing, reported as healthy. It was masked, not harmless: `settle_diagnostics`
    # re-derives the status from `routing_action` downstream and handles both spellings,
    # so the wrong value was overwritten before any rung could read it. Masked by
    # ordering is not fixed, and the two vocabularies had to be reconciled somewhere.
    if routing_action in ("swap", "predictive_swap"):
      convoy_status = "predictive_swap"
    elif routing_action == "technician":
      convoy_status = "predictive_impairment"
    elif routing_action == "device_offline":
      convoy_status = "predictive_offline"

    _audit_response = {
        "status": "success",
        "repair_recommendations": repair_recs,
        "routing_action": routing_action,
        "convoy_status": convoy_status,
        "success": True,
    }
    print(
        "[AUDIT] [check_convoy_recommendations] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"[check_convoy] Parse error: {e}")
    traceback.print_exc()
    _audit_response = {
        "status": "error",
        "repair_recommendations": [],
        "routing_action": "none",
        "error": f"Failed to parse Convoy response: {str(e)}",
        "agent_action": "transfer_to_human",
    }
    print(
        "[AUDIT] [check_convoy_recommendations] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
