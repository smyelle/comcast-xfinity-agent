"""There is no gateway on the account, so there is nothing to measure."""

SAY_MISSING_HARDWARE_LEAD = (
    "I'm not seeing an Xfinity Gateway on your account, so I can't run any more checks."
)

SAY_MISSING_HARDWARE_REST = "Let me connect you with someone who can help."

# An absent slot reads as "" here, which is exactly the no-gateway case.
MISSING_HARDWARE = {"slot": "cable_modem_mac", "in": ["NOT_FOUND", ""]}

__all__ = [
    'MISSING_HARDWARE',
    'SAY_MISSING_HARDWARE_LEAD',
    'SAY_MISSING_HARDWARE_REST',
]


import scripts
from journeys.common.rungs import rung


def tasks():
  """There is no gateway on the account, so there is nothing to measure."""
  return [
      # P3 — no gateway on the account, so nothing further can be measured.
      rung("HandleMissingHardware", "verdict_missing_hardware",
           MISSING_HARDWARE, SAY_MISSING_HARDWARE_REST,
           say_first=SAY_MISSING_HARDWARE_LEAD),
  ]
