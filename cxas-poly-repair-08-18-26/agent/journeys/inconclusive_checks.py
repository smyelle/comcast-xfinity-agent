"""The checks could not tell us anything, so hand off rather than guess."""

SAY_NO_TELEMETRY = (
    "I just ran a few checks but wasn't able to get all the info I need. Let me get "
    "you to someone who can help."
)

# The limit is OURS, and the sentence has to say so: a line that reports a system state
# blames the caller's own hardware by implication, which the repair guidance rules out.
# No internal name for the capability either. "Yet" stays, because the gap is real and is
# being closed.
SAY_UNSUPPORTED_DEVICE = (
    "I can't run my checks on that model of gateway yet. Let me get you to someone who "
    "can help."
)

NO_TELEMETRY = {"slot": "gateway_status",
                "in": ["no_telemetry", "offline", "error", "skipped"]}

UNSUPPORTED_DEVICE = {"slot": "gateway_status", "eq": "unsupported_device"}

DIAGNOSTIC_ERROR = {"any": [{"slot": s, "eq": "error"} for s in
                            ("outage_status", "network_status", "gateway_status",
                             "wifi_status")]}

__all__ = [
    'DIAGNOSTIC_ERROR',
    'NO_TELEMETRY',
    'SAY_NO_TELEMETRY',
    'SAY_UNSUPPORTED_DEVICE',
    'UNSUPPORTED_DEVICE',
]


import scripts
from journeys.common.rungs import rung


def tasks():
  """The checks could not tell us anything, so hand off rather than guess."""
  return [
      # P8 — device model we cannot triage automatically.
      rung("HandleUnsupportedDevice", "verdict_unsupported_device",
           UNSUPPORTED_DEVICE, SAY_UNSUPPORTED_DEVICE),

      # P9 — telemetry missing or a tool errored: hand off rather than guess.
      rung("HandleNoTelemetry", "verdict_no_telemetry",
           NO_TELEMETRY, SAY_NO_TELEMETRY),
      rung("HandleDiagnosticError", "verdict_diagnostic_failure",
           DIAGNOSTIC_ERROR, SAY_NO_TELEMETRY),
  ]
