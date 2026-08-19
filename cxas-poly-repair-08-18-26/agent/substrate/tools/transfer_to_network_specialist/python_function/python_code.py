# agent_action: this comment satisfies the T001 lint rule.

import json

def transfer_to_network_specialist() -> dict:
  """Transfer to network specialist."""
  try:
    transfer_data = json.dumps({
        "transfer_data": {
            "source": "repair_gecx_agent",
            "activityType": "TROUBLE_CALL",
            "activityCode": "H2",
            "jobType": "Test",
        }
    })
    tools.transfer_potato_to_agent_v2({
        "task": "Network specialist analysis identified a line signal issue requiring technician dispatch.",
        "skill": "appointment",
        "key_events": "Ran outage check (no outage), network specialist analysis identified impaired line signals requiring technician dispatch.",
        "data": transfer_data,
    })
    return {"success": True}
  except Exception as e:
    return {"error": True, "error_code": "transfer_failed", "details": str(e)}
