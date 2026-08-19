# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

import json
import time
import traceback
from typing import Any

# ---------------------------------------------------------------------------
# SINGLE SOURCE OF TRUTH for Internet (repair) XIT/Convoy recommendations.
#
# TO ONBOARD A NEW RECOMMENDATION: add ONE entry below. Nothing else in the
# codebase needs to change.
#   "<recommendationName>": "technician"       -> recommendedAction=CreateAppointment
#                                                 (truck roll / appointment transfer)
#   "<recommendationName>": "predictive_swap"  -> gateway swap offer
#   "<recommendationName>": "device_offline"   -> reboot path
#   "<recommendationName>": None               -> recognized & parsed, but never
#                                                 drives routing on its own
#
# NOTE: priority is driven by the ORDER CONVOY RETURNS the recs (first rec with a
# routing action wins), NOT by the order of this dict — so appending is safe.
# ---------------------------------------------------------------------------
INTERNET_RECOMMENDATIONS = {
    "PREDICTIVE_GATEWAYSWAP": "predictive_swap",
    "OutsideHomeSRO": "technician",
    "XITNetworkImpairment": "technician",
    "RFCxel": "technician",
    "XIModemOfflineDigital": "technician",
    "DigitalFailingWAN": "technician",
    "XIT_AIQ_PREDICTIVE_WAN_SCORE": "technician",
    "PHTAllOut": "technician",
    "PHTPartialChannelBonding": "technician",
    "PHTLite": "technician",
    # --- Onboarded 2026-08-17: all recommendedAction=CreateAppointment ---
    "DigitalGWSTFail": "technician",
    "DIGITAL_SROIngressLeakage": "technician",
    # Prior truck-roll / TC-widget IVR outcomes that still warrant an appointment.
    "PreviouslyCompletedTruckRollIVR": "technician",
    "PreviouslyCancelledTruckRollIVR": "technician",
    "PreviouslyDeclinedTruckRollIVR": "technician",
    "PreviouslyUnauthorizedTruckRollIVR": "technician",
    "CustNotHomeTruckRollIVR": "technician",
    "TCWidgetAbandonedIVR": "technician",
    # Recognized/parsed only — intentionally no routing action.
    "BootfileTroubleshootingDigital": None,
    "CURRENT_OUTAGE": None,
}

# Repair-relevant recommendation IDs to look for (derived — do not edit).
REPAIR_RECOMMENDATION_IDS = list(INTERNET_RECOMMENDATIONS)

# Recommendation name -> routing action (derived — do not edit).
# First repair rec with a mapping wins.
ROUTING_MAP = {
    _name: _action
    for _name, _action in INTERNET_RECOMMENDATIONS.items()
    if _action
}

# VIDEO recommendation IDs (kept SEPARATE from the Internet REPAIR_RECOMMENDATION_IDS
# and NOT added to ROUTING_MAP, so recognizing them can NEVER change Internet routing
# or Internet state). These are read additively and only populate {video_xit_recommendation},
# which is consumed by the Video flow (video_specialist_agent) — see
# docs/video-troubleshooting-spec.md §4.2. Same Convoy call, no extra API request.
VIDEO_RECOMMENDATION_IDS = [
    "XIT_AIQ_PREDICTIVE_RFVIDEO",
]

# ---------------------------------------------------------------------------
# XIT RECOMMENDATION CUSTOMER COPY (headline + four-part explanation)
# ---------------------------------------------------------------------------
# Product-authored copy, one entry per recommendation ID. Each entry supplies:
#   headline  -> the visible message (shown OUTSIDE the collapsible card)
#   happening / why / doing / whatYouNeedToDo -> the four parts shown INSIDE the card
#
# The headline REPLACES the raw adkCustomerMessage as the customer-visible text.
# adkCustomerMessage is still parsed and kept in {convoy_customer_message} /
# {video_customer_message} and is used as the FALLBACK headline for any rec that has
# no authored copy here — so no recommendation can ever lose its message.
#
# HOW THIS COPY IS PRODUCED (design-time, not runtime):
# the "message generation prompt" is an AUTHORING tool — run it once per new
# recommendation ID against that rec's XIT definition, review the output, and paste the
# five fields here ("Output maps 1:1 to the scenario fields in code"). It is deliberately
# NOT called at request time:
#   - the Internet XIT verdict is dispatched from before_agent_callback (message + transfer
#     tool in ONE turn), so there is no LLM turn available on that path;
#   - customer-visible copy stays deterministic, reviewable and stable for evals;
#   - it cannot drift from the "Truthful only — never invent causes, dates, durations,
#     speeds or dollar charges" rule on a live call.
#
# TO ONBOARD A NEW RECOMMENDATION: add it to INTERNET_RECOMMENDATIONS (routing) and, when
# product has authored copy, add an entry here. Without an entry the rec still works: it
# falls back to adkCustomerMessage as the headline plus the action-level default card.
_DEFAULT_SUMMARY_COPY = {
    "technician": {
        "happening": (
            "We found a problem affecting the service coming into your home, so your"
            " connection hasn't been working the way it should."
        ),
        "why": (
            "Our checks picked this up on the line and equipment serving your address."
            " It's typically caused by signal levels being out of range, damaged or loose"
            " cabling, or a fault on the line outside the home. It isn't something we can"
            " correct remotely."
        ),
        "doing": (
            "We're flagging your account for technical support so a technician can locate"
            " and fix the issue."
        ),
        "todo": (
            "Please schedule a technician visit. The technician will test your signal"
            " levels and inspect the wiring serving your home."
        ),
    },
    "predictive_swap": {
        "happening": (
            "Your gateway is failing intermittently, which shows up as drops or slowdowns"
            " that come and go."
        ),
        "why": (
            "Its diagnostics report a hardware fault. A restart clears the symptom for a"
            " while but doesn't fix the underlying problem, so the unit needs replacing."
        ),
        "doing": (
            "We've made a replacement gateway available to you so you can swap it at your"
            " convenience."
        ),
        "todo": (
            "Order a replacement to be shipped, or pick one up at an Xfinity store, then"
            " return the old gateway."
        ),
    },
    "device_offline": {
        "happening": "Your gateway isn't staying connected the way it should.",
        "why": (
            "Its diagnostics show it dropping offline. This is often cleared by a restart,"
            " which reloads the gateway's settings and reconnects it to our network."
        ),
        "doing": "We're restarting your gateway now.",
        "todo": (
            "Give the gateway a few minutes to come back online, then check your"
            " connection again."
        ),
    },
}

# Per-recommendation copy, VERBATIM as authored by product. Keys MUST exist in
# INTERNET_RECOMMENDATIONS or VIDEO_RECOMMENDATION_IDS.
_RECOMMENDATION_SUMMARY_COPY = {
    "XITNetworkImpairment": {
        "headline": (
            "Over the past few days your connection has been losing data and slowing down."
            " It isn't something we can fix from here, so we recommend scheduling a"
            " technician visit."
        ),
        "happening": (
            "Over the past three days, we detected significant data loss, delays, and"
            " repeated retransmissions on your connection, causing slow speeds, buffering,"
            " and service interruptions."
        ),
        "why": (
            "These are signs that the coaxial cable or signal reaching your modem is"
            " degraded, typically from a loose or damaged cable inside or outside your home."
        ),
        "doing": (
            "We are flagging your line for a technical inspection to correct the signal"
            " problem and restore stable, reliable service."
        ),
        "todo": (
            "Please schedule a technician visit. A technician needs to check your signal"
            " quality, test your modem, and inspect the wiring both outside and inside your"
            " home."
        ),
    },
    "PHTAllOut": {
        "headline": (
            "Your service is fully out, and it looks like the cable connection to your home"
            " was interrupted. We can't fix that remotely, so we recommend scheduling a"
            " technician visit."
        ),
        "happening": (
            "We have completely lost communication with the equipment inside your home,"
            " causing a full service outage."
        ),
        "why": (
            "The physical cable connection carrying your service has been interrupted. This"
            " is caused by a disconnected, loose, or damaged cable somewhere between our"
            " network and your home."
        ),
        "doing": (
            "We are dispatching technical support to locate the break in the connection and"
            " restore your service."
        ),
        "todo": (
            "Please schedule a technician visit so they can inspect the wiring outside and"
            " inside your home to fix the issue."
        ),
    },
    "OutsideHomeSRO": {
        "headline": (
            "We found damage on the line outside your home that's hurting your service. A"
            " technician needs to repair it, and the good news is you won't need to be home"
            " for the visit."
        ),
        "happening": (
            "We detected severe signal interference on your line, causing poor internet"
            " performance and instability."
        ),
        "why": (
            "The cable running outside your home\u2014between your house and the utility pole or"
            " street box\u2014is damaged or degraded and isn't delivering a clear signal."
        ),
        "doing": (
            "We are sending a technician to inspect, repair, or replace the damaged section"
            " of outdoor cable to restore your signal quality."
        ),
        "todo": (
            "Nothing. Since all the work is outside, you do not need to be home for this"
            " repair."
        ),
    },
    "XIModemOfflineDigital": {
        "headline": (
            "We can't reach your modem right now and your service is down. It looks like a"
            " line problem we can't fix from here, so we recommend scheduling a technician"
            " visit."
        ),
        "happening": (
            "We are currently unable to reach or communicate with your cable modem,"
            " resulting in a service loss."
        ),
        "why": (
            "The cable line delivering your service is interrupted. This usually happens"
            " when a wire is loose, disconnected, or damaged\u2014either outside or within your"
            " home's internal wiring."
        ),
        "doing": (
            "We are ready to assign a technician to trace the connection, find the exact"
            " spot where the line was interrupted, and restore your service."
        ),
        "todo": (
            "Please schedule a technician visit so they can inspect the wiring both outside"
            " and inside your home to fix the break."
        ),
    },
    "DigitalFailingWAN": {
        "headline": (
            "Your connection keeps dropping because your modem keeps losing signal. It isn't"
            " something we can fix remotely, so we recommend scheduling a technician visit."
        ),
        "happening": (
            "We detected several unexpected dropouts on your internet connection over the"
            " past 24 hours, causing your service to cut in and out."
        ),
        "why": (
            "Your modem is repeatedly losing its connection, which is typically caused by a"
            " loose wire, damaged cable, or a fault in the equipment itself."
        ),
        "doing": (
            "We are flagging your line for a technical inspection to identify why your"
            " connection keeps dropping and to ensure your service is stabilized."
        ),
        "todo": (
            "Please schedule a technician visit. They will need to inspect the cable wiring"
            " inside and outside your home, as well as test your modem, to fix the issue."
        ),
    },
    "PHTLite": {
        "headline": (
            "Some of the channels your modem uses to connect aren't syncing, which can slow"
            " your internet. We can't fix that from here, so we recommend scheduling a"
            " technician visit."
        ),
        "happening": (
            "We detected that several of the channels your modem uses to connect are not"
            " syncing correctly, which can cause slower speeds, service interruptions, and"
            " data loss."
        ),
        "why": (
            "This is typically caused by a damaged or degraded coaxial cable inside or"
            " outside your home, or a problem with the modem itself."
        ),
        "doing": (
            "We are flagging your line for a technical inspection to correct the connection"
            " issue and restore full internet speeds."
        ),
        "todo": (
            "Please schedule a technician visit. A technician needs to inspect the wiring"
            " and signal both outside and inside your home and test your equipment."
        ),
    },
    "RFCxel": {
        "headline": (
            "We found a signal problem on your line that's slowing things down. It isn't"
            " something we can fix from here, so we recommend scheduling a technician visit."
        ),
        "happening": (
            "Our system detected significant data loss on your internet connection for at"
            " least six hours today, which causes slower speeds, buffering, and random drops."
        ),
        "why": (
            "The signal traveling through your coaxial cable is not reaching your modem"
            " reliably. This is usually caused by weak signal levels, loose or damaged"
            " cables, or an issue with the neighborhood line."
        ),
        "doing": (
            "We are flagging your account for technical support so we can resolve the signal"
            " issue and restore a stable connection to your home."
        ),
        "todo": (
            "Please schedule a technician visit. A technician needs to test your signal"
            " strength and inspect the wiring both outside and inside your home."
        ),
    },
    "XIT_AIQ_PREDICTIVE_WAN_SCORE": {
        "headline": (
            "Your connection quality has been unstable, which can cause drops and slowdowns."
            " It looks like a signal problem on your line that we can't fix from here, so we"
            " recommend scheduling a technician visit."
        ),
        "happening": (
            "We've been seeing unstable connection quality on your line, which can cause your"
            " internet to drop out or run slower than it should."
        ),
        "why": (
            "This usually points to a signal problem on the cable line coming into your home."
            " It takes testing on site to pin down exactly where."
        ),
        "doing": (
            "We're flagging your line for a technical inspection so we can find the source of"
            " the instability and get your connection steady again."
        ),
        "todo": (
            "Please schedule a technician visit. A technician needs to test the signal on your"
            " line and inspect the wiring both outside and inside your home."
        ),
    },
    # PURELY ADDITIVE: no "headline" key on purpose. This rec routes to predictive_swap,
    # where the dispatch does `swap_msg = fields.pop("headline", "") or swap_msg` — supplying
    # a headline would REPLACE the shipped swap message (and its Equipment Update link) and
    # change the recommended action's copy. We are only adding the alert summary card here,
    # so the visible swap message stays exactly as it ships today and only the four card
    # parts are new. Product's authored headline for this rec is intentionally NOT applied.
    "PREDICTIVE_GATEWAYSWAP": {
        "happening": (
            "Your gateway has been failing intermittently, which can drop your internet"
            " without warning and then bring it back on its own."
        ),
        "why": (
            "This is a fault in the gateway hardware itself, not a problem with your line or"
            " your settings. Restarting it won't clear a fault like this."
        ),
        "doing": (
            "We've cleared your gateway for a replacement so you can get a working one right"
            " away. You don't need a technician visit for this."
        ),
        "todo": (
            "Order your replacement gateway. You can have a new one shipped to you, or swap it"
            " at an Xfinity store."
        ),
    },
    "PHTPartialChannelBonding": {
        "headline": (
            "Some of the channels your modem uses to connect aren't coming up, which can hold"
            " your speeds down. We can't fix that from here, so we recommend scheduling a"
            " technician visit."
        ),
        "happening": (
            "Your modem isn't connecting on all of the channels it normally uses, which can"
            " hold your speeds down and make your service feel inconsistent."
        ),
        "why": (
            "This is typically caused by a damaged or degraded coaxial cable inside or outside"
            " your home, or by a problem with the modem itself."
        ),
        "doing": (
            "We're flagging your line for a technical inspection so we can get all of your"
            " channels connecting again and restore full speeds."
        ),
        "todo": (
            "Please schedule a technician visit. A technician needs to inspect the wiring and"
            " signal both outside and inside your home and test your equipment."
        ),
    },
    "XIT_AIQ_PREDICTIVE_RFVIDEO": {
        "headline": (
            "We've noticed picture problems on your TV over the past couple of days. It's"
            " likely a signal issue we can't fix remotely, so we recommend scheduling a"
            " technician visit."
        ),
        "happening": (
            "Over the past two days, we detected repeated picture problems on your TV"
            " service, including channel-tuning issues and pixelation."
        ),
        "why": (
            "This is usually caused by a damaged or degraded coaxial cable or a weak signal"
            " reaching your TV equipment."
        ),
        "doing": (
            "We are sending a technician to inspect your cable, signal levels, and video"
            " equipment so we can restore a clear picture."
        ),
        "todo": (
            "Please schedule a technician visit. A technician needs to check the wiring and"
            " signal both outside and inside your home, as well as your TV equipment."
        ),
    },
}

# The four card fields, in the exact order the card renders them (mirrors _TS_KEYS in
# the orchestrator callbacks — keep in sync). 'headline' is deliberately NOT here: it is
# the visible message, not a card section.
SUMMARY_KEYS = ("happening", "why", "doing", "todo")


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
      "agent_name": "gecx_repair_agent",
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
        # Optional appointment-routing attributes; may be absent for some recs.
        problem_code = aspect_data.get("problemCode", "") or additional_data.get(
            "problemCode", ""
        )
        intents = aspect_data.get("intents", "") or additional_data.get(
            "intents", ""
        )

        repair_recs.append({
            "name": rec_name,
            "activity_code": activity_code,
            "job_type": job_type,
            "activity_type": activity_type,
            "description": description,
            "recommended_action": recommended_action,
            "problem_code": problem_code,
            "intents": intents,
        })

    print(f"[check_convoy] Found {len(repair_recs)} repair recommendations")

    # --- VIDEO XIT recommendation (additive, isolated) ---
    # Scan the SAME recommendations payload for video recs (e.g. XIT_AIQ_PREDICTIVE_RFVIDEO)
    # and cache them in {video_xit_recommendation}. This does NOT touch any Internet routing
    # or Internet state variables; it only populates a new video-specific state var that the
    # Video flow reads. Kept in its own try/block so a parsing issue here never affects the
    # Internet recommendation handling above.
    try:
      video_recs = []
      for rec in recommendations:
        if not isinstance(rec, dict):
          continue
        rec_name = (
            rec.get("id", "")
            or rec.get("recommendationsName", "")
            or rec.get("name", "")
            or rec.get("key", "")
            or ""
        )
        if rec_name not in VIDEO_RECOMMENDATION_IDS:
          continue
        aspect_data = {}
        for info_block in rec.get("additionalInformation", []) or []:
          if isinstance(info_block, dict):
            for aspect in info_block.get("aspect", []) or []:
              if isinstance(aspect, dict) and aspect.get("attribute"):
                aspect_data[aspect.get("attribute")] = aspect.get("value", "")
        add_data = rec.get("additionalData", {}) or {}
        video_recs.append({
            "name": rec_name,
            "recommended_action": aspect_data.get("recommendedAction", "") or add_data.get("recommendedAction", ""),
            "description": (
                aspect_data.get("adkCustomerMessage", "")
                or aspect_data.get("adkDescription", "")
                or add_data.get("adkCustomerMessage", "")
                or rec.get("shortDescription", "")
            ),
            "activity_code": aspect_data.get("activityCode", "") or add_data.get("activityCode", ""),
            "job_type": aspect_data.get("jobType", "") or add_data.get("jobType", ""),
            "activity_type": aspect_data.get("activityType", "") or add_data.get("activityType", ""),
            "problem_code": aspect_data.get("problemCode", "") or add_data.get("problemCode", ""),
            "intents": aspect_data.get("intents", "") or add_data.get("intents", ""),
        })
      context.state["video_xit_recommendation"] = json.dumps(video_recs) if video_recs else ""
      print(f"[check_convoy] Found {len(video_recs)} VIDEO recommendation(s); set video_xit_recommendation")

      # Propagate the FIRST video rec's appointment attributes + adkCustomerMessage
      # into DEDICATED video_* state vars. This mirrors how activityCode/jobType/
      # convoy_customer_message are set for the Internet rec (below), but is kept
      # SEPARATE so the Video appointment transfer uses the VIDEO rec's own codes
      # and message even when an Internet rec (e.g. XIModemOfflineDigital) is ALSO
      # present in the SAME payload. These never touch Internet routing/state.
      if video_recs:
        _v = video_recs[0]
        _v_activity_code = _v.get("activity_code", "") or "H2"
        _v_job_type = _v.get("job_type", "") or "Test"
        _v_activity_type = _v.get("activity_type", "") or "TROUBLE_CALL"
        _v_problem_code = _v.get("problem_code", "")
        _v_intents = _v.get("intents", "")
        context.state["video_activityCode"] = _v_activity_code
        context.state["video_jobType"] = _v_job_type
        context.state["video_activityType"] = _v_activity_type
        context.state["video_problemCode"] = _v_problem_code
        context.state["video_intents"] = _v_intents
        # adkCustomerMessage for the video rec — shown VERBATIM to the customer,
        # exactly like the Internet flow uses convoy_customer_message.
        context.state["video_customer_message"] = _v.get("description", "")
        # Four-part explanation for the Video collapsible card (ADDITIVE — the headline
        # remains the adkCustomerMessage above). Kept in a DEDICATED video_* var so it can
        # never be crossed with the Internet rec's copy when both are in the same payload.
        _v_summary_fields = _build_summary_fields(_v.get("name", ""), "technician")
        context.state["video_summary_fields"] = (
            json.dumps(_v_summary_fields) if _v_summary_fields else ""
        )
        # Ready-to-use transfer_data string the Video agent copies VERBATIM into
        # the transfer_potato_to_agent_v2 `data` arg (same shape/source as
        # _build_appointment_transfer_data in the before_agent_callback).
        context.state["video_appointment_transfer_data"] = json.dumps({
            "transfer_data": {
                "source": "repair_gecx_agent",
                "activityType": _v_activity_type,
                "activityCode": _v_activity_code,
                "jobType": _v_job_type,
                "problemCode": _v_problem_code,
                "intents": _v_intents,
            }
        })
        # Truck-roll signal for the Video flow (recommended_action == CreateAppointment).
        if str(_v.get("recommended_action", "")).lower() == "createappointment":
          context.state["video_convoy_status"] = "truck_roll"
        else:
          context.state["video_convoy_status"] = "no_recommendation"
        print(
            "[check_convoy] Propagated VIDEO transfer values:"
            f" video_activityCode='{_v_activity_code}',"
            f" video_jobType='{_v_job_type}',"
            f" video_activityType='{_v_activity_type}',"
            f" video_convoy_status='{context.state.get('video_convoy_status')}'"
        )
      else:
        # No video rec this session — clear so a stale value can never drive a transfer.
        context.state["video_customer_message"] = ""
        context.state["video_appointment_transfer_data"] = ""
        context.state["video_convoy_status"] = ""
        context.state["video_summary_fields"] = ""
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(f"[check_convoy] Video recommendation parse error (non-fatal): {e}")


    # Determine routing action based on priority and capture matched ticket codes
    routing_action = "none"
    matched_rec = {}

    for rec in repair_recs:
      action = ROUTING_MAP.get(rec["name"])
      if action:
        routing_action = action
        matched_rec = rec
        break

    matched_activity_code = matched_rec.get("activity_code", "")
    matched_job_type = matched_rec.get("job_type", "")
    matched_activity_type = matched_rec.get("activity_type", "")
    matched_description = matched_rec.get("description", "")
    matched_recommended_action = matched_rec.get("recommended_action", "")
    matched_problem_code = matched_rec.get("problem_code", "")
    matched_intents = matched_rec.get("intents", "")

    # Set state variables
    _summary_fields = {}
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

      # Optional appointment-routing attributes — propagated the same way as
      # activityCode/jobType (flow through as empty when the rec omits them).
      context.state["problemCode"] = matched_problem_code
      context.state["intents"] = matched_intents
      print(
          "[check_convoy] Optional transfer attributes:"
          f" problemCode='{matched_problem_code}', intents='{matched_intents}'"
      )

      # Store customer-facing message from convoy
      if matched_description:
        context.state["convoy_customer_message"] = matched_description
        print(f"[check_convoy] Set convoy_customer_message: '{matched_description}'")

      # Four-part explanation for the collapsible "View troubleshooting summary" card.
      # ADDITIVE ONLY: the headline stays the adkCustomerMessage above; this just gives
      # the card a body. Empty string when the matched rec has no card copy, so the
      # renderer skips the card entirely and behaviour is unchanged.
      _summary_fields = _build_summary_fields(matched_rec.get("name", ""), routing_action)
      context.state["convoy_summary_fields"] = (
          json.dumps(_summary_fields) if _summary_fields else ""
      )
      print(
          "[check_convoy] Set convoy_summary_fields for"
          f" rec='{matched_rec.get('name', '')}' action='{routing_action}':"
          f" {'present' if _summary_fields else 'none'}"
      )

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

    # RETURNED (not just written to context.state) on purpose: this tool is invoked
    # from repair_orchestration_agent's before_agent_callback, and a tool's state
    # delta is NOT visible to that callback's own `callback_context.state` during the
    # same turn. The callback therefore mirrors these fields off the RETURN value
    # (exactly as it already does for description/activityCode/jobType); without this
    # the XIT dispatch read an empty {convoy_summary_fields} and rendered no card.
    _audit_response = {
        "status": "success",
        "repair_recommendations": repair_recs,
        "routing_action": routing_action,
        "summary_fields": _summary_fields,
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


def _build_summary_fields(rec_name: str, action: str) -> dict:
  """Returns the customer copy for a recommendation.

  Shape: {headline?, happening, why, doing, whatYouNeedToDo}.

  Resolution order: product-authored per-recommendation copy -> action-level default
  (four card parts only, NO headline) -> {}.

  'headline' is present ONLY when product authored copy for this rec. When it is
  absent the caller keeps using adkCustomerMessage as the visible message, so a rec
  without authored copy behaves exactly as it does today.

  Returns {} when the action has no card at all (e.g. recognized-but-non-routing
  recs), so callers simply skip rendering and the message is left unchanged.

  Defined AFTER the tool entry point on purpose: the CES linter (T004) expects the
  FIRST top-level function in a tool file to be the tool itself.
  """
  copy = _RECOMMENDATION_SUMMARY_COPY.get(rec_name)
  if not copy:
    copy = _DEFAULT_SUMMARY_COPY.get(action or "")
  if not copy:
    return {}
  out = {k: copy[k] for k in SUMMARY_KEYS if copy.get(k)}
  if copy.get("headline"):
    out["headline"] = copy["headline"]
  return out
