# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught,redefined-builtin,unused-argument

"""PolySynth callback function optimized with real-time concurrent notification dispatches and dynamic MAC-Less fallbacks."""

# pylint: disable=undefined-variable

import json
from typing import Any, Optional

# Approved notification text — product team approved, do not change without approval.
APPROVED_STARTED_TEXT = {
    "outage_inprogress_message": "Checking for service outages...",
    "network_inprogress_message": "Checking network...",
    "device_inprogress_message": "Checking modem...",
}


def before_tool_callback(
    tool: Tool,
    input: dict[str, Any],
    callback_context: CallbackContext,
) -> Optional[dict[str, Any]]:
  """Executes *immediately before* a tool is executed by the agent model.

  We intercept sub-agent invocations to fire atomic dashboard checklist
  in-progress notifications. Also intercepts LLM-initiated post_user_notification
  calls to enforce approved text.

  Args:
    tool: The tool description object.
    input: The input arguments that will be passed to the tool.
    callback_context: The execution context containing state memory.

  Returns:
    The modified input dictionary, or None to proceed with unmodified arguments.
  """
  tool_id = _get_tool_identifier(tool)
  print(f"[orchestrator_before_tool] Tool '{tool_id}' is starting...")

  try:
    # Intercept LLM-initiated post_user_notification calls to enforce approved text
    if tool_id == "post_user_notification":
      tool_category = ""
      status_val = ""
      if isinstance(input, dict):
        tool_category = input.get("tool", "")
        status_val = input.get("status", "")
      # For "started" notifications, override with approved text
      if status_val == "started":
        key = tool_category + "_inprogress_message"
        if key in APPROVED_STARTED_TEXT:
          input["text"] = APPROVED_STARTED_TEXT[key]
          print(f"[orchestrator_before_tool] Overriding LLM text for {tool_category}/{status_val} with approved text.")
      return input

    # Override task for human skill transfer to static message (steering reads functionCall args)
    if tool_id == "transfer_potato_to_agent_v2":
      if isinstance(input, dict) and input.get("skill") == "human":
        original_task = input.get("task", "")
        input["task"] = "Connect the user to the live agent"
        # Arm same-turn post-tool suppression to prevent duplicate handoff text
        # from the second LLM pass after tool response.
        callback_context.state["_suppress_post_transfer_llm_text"] = "true"
        # Mark the session as escalated so subsequent follow-up turns are intercepted
        # deterministically by before_agent_callback. Without this, an LLM-initiated
        # human handoff (e.g. Wi-Fi troubleshooter cap, AGENT branch) leaves the flag
        # "off" on all-healthy sessions, so the LLM handles the next turn and echoes
        # stale user input (e.g. "on my macbook") instead of a contextual reply.
        callback_context.state["escalate_to_human_flag"] = "on"
        print(f"[orchestrator_before_tool] Overriding task for human transfer + set escalate_to_human_flag=on. Original: '{original_task}'")
      elif isinstance(input, dict) and input.get("skill") == "appointment":
        # The LLM builds the appointment transfer_data from instruction-template
        # variables ({problemCode}/{activityCode}/{jobType}/{intents}) that are
        # rendered from the PRE-TURN state snapshot — i.e. the convoy/cara defaults
        # (problemCode='', activityCode='H2', jobType='Test') set before the network
        # specialist resolved the real dispatch codes (install & repair -> 88653/
        # H3/AO/INTERNET_REPAIR). That stale LLM call would otherwise reach the
        # appointment agent with an EMPTY problemCode. Deterministically rebuild the
        # payload from authoritative state so the correct codes always flow through.
        state = callback_context.state
        transfer_data = {
            "source": "repair_gecx_agent",
            "activityType": state.get("activityType") or "TROUBLE_CALL",
            "activityCode": state.get("activityCode") or "H2",
            "jobType": state.get("jobType") or "Test",
            "problemCode": state.get("problemCode", "") or "",
            "intents": state.get("intents", "") or "",
        }
        input["data"] = json.dumps({"transfer_data": transfer_data})
        print(f"[orchestrator_before_tool] Overriding appointment transfer data from state: {input['data']}")
      return input

    # 1. Intercept Outage Sub-Agent Tool Call
    if tool_id == "outage_specialist_agent_as_a_tool":
      if callback_context.state.get("outage_notified") != "true":
        tools.post_user_notification({
            "tool": "outage",
            "status": "started",
            "text": APPROVED_STARTED_TEXT["outage_inprogress_message"],
            "calling_agent": "repair_orchestration_agent",
            "calling_tool": "before_tool_callback",
        })
        print("[orchestrator_before_tool] Posted in-progress notification for outage")

    # 2. Intercept Network Sub-Agent Tool Call
    elif tool_id == "network_specialist_agent_as_a_tool":
      if callback_context.state.get("network_notified") != "true":
        tools.post_user_notification({
            "tool": "network",
            "status": "started",
            "text": APPROVED_STARTED_TEXT["network_inprogress_message"],
            "calling_agent": "repair_orchestration_agent",
            "calling_tool": "before_tool_callback",
        })
        print("[orchestrator_before_tool] Posted in-progress notification for network")

    # 3. Intercept Gateway/Wifi Sub-Agent Tool Call
    elif tool_id == "gateway_specialist_agent_as_a_tool":
      if callback_context.state.get("gateway_notified") != "true":
        tools.post_user_notification({
            "tool": "device",
            "status": "started",
            "text": APPROVED_STARTED_TEXT["device_inprogress_message"],
            "calling_agent": "repair_orchestration_agent",
            "calling_tool": "before_tool_callback",
        })
        print("[orchestrator_before_tool] Posted in-progress notification for device")


  except Exception as e:
    print(f"[orchestrator_before_tool] Error executing callback: {e}")

  return None


def _get_tool_identifier(tool: Any) -> str:
  """Safely extracts the tool name or displayName in a resilient, error-free manner."""
  if tool is None:
    return ""
  # 1. Try getattr on name
  name = getattr(tool, "name", None)
  # 2. Try getattr on displayName
  display_name = getattr(tool, "displayName", None)
  # 3. If it is a dictionary, try standard get
  if not name and isinstance(tool, dict):
    name = tool.get("name") or tool.get("displayName")
  return str(name or display_name or "").strip()
