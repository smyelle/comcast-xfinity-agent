"""Getting the caller to a person."""

import flows


# Nothing reads this slot — the task exists for its side effect, the hand-off payload —
# but a task must map an output, and a slot the flow does not declare is a build error
# rather than a silent no-op.
def slots():
  """Where the hand-off summary lands."""
  return [
      flows.event_slot("escalate_summary"),
  ]


def tasks():
  """Put the diagnosis on the hand-off, once the escalate rail has fired."""
  return [
      flows.task(
        "EscalateHandoffSummary", "verdict_human_request",
        ["account_status", "outage_status", "network_status", "gateway_status"],
        "escalate_summary", out_key="success", condition=flows.escalated()),
  ]
