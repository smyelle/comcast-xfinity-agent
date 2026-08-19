# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function."""

# pylint: disable=undefined-variable

import json


def transfer_potato_to_agent_v2(
    task: str, key_events: str, skill: str, data: str = ""
) -> str:
  """Signals the steering orchestrator to transfer the conversation to a specialized agent.

  After calling this tool, do NOT send any further messages or take any actions.

  Args:
      task: Concise description of what the user needs RIGHT NOW that this agent
        cannot handle.
      key_events: Brief summary of what this agent has accomplished so far.
      skill: REQUIRED. One of: nlu, human, appointment, billing,
        troubleshooting, payment_and_credit, ecm.
      data: OPTIONAL. JSON string of additional key-value pairs for the target
        agent.

  Returns:
      A JSON string confirming the transfer signal.
  """
  _audit_request = {
      "task": task,
      "key_events": key_events,
      "skill": skill,
      "data": data,
  }
  print(
      "[AUDIT] [transfer_potato_to_agent_v2] >>> Request Payload:",
      f" {_audit_request}",
  )
  valid_skills = [
      "nlu",
      "human",
      "appointment",
      "billing"
  ]

  if skill not in valid_skills:
    _audit_response = json.dumps({
        "status": "error",
        "message": (
            f"Invalid skill '{skill}'. Must be one of:"
            f" {', '.join(valid_skills)}"
        ),
        "agent_action": (
            "Choose a valid transfer target from the list of supported agent"
            " skills."
        ),
    })
    print(
        "[AUDIT] [transfer_potato_to_agent_v2] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response

  # For human agent transfers, use a static task message to avoid
  # misleading/dynamic text on the steering side.
  original_task = task
  if skill == "human":
    task = "Connect the user to the live agent"
    print(f"[AUDIT] [transfer_potato_to_agent_v2] Overriding task for human skill. Original: '{original_task}'")

  try:
    result = {
        "status": "transfer_initiated",
        "task": task,
        "key_events": key_events,
        "skill": skill,
    }

    if data:
      result["data"] = data

    _audit_response = json.dumps(result)
    print(
        "[AUDIT] [transfer_potato_to_agent_v2] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
  except Exception as e:  # pylint: disable=broad-exception-caught
    _audit_response = json.dumps({
        "status": "error",
        "message": f"Serialization failed: {str(e)}",
        "agent_action": (
            "Confirm that all inputs are valid strings and the optional data"
            " payload is correctly formatted."
        ),
    })
    print(
        "[AUDIT] [transfer_potato_to_agent_v2] <<< Response Payload:",
        f" {_audit_response}",
    )
    return _audit_response
