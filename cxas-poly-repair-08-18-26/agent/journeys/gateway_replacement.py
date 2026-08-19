"""The box itself has to be swapped."""

import scripts
from journeys.common.rungs import rung


def tasks():
  """The box itself has to be swapped."""
  return [
      # P5 — hardware fault. A reboot cannot fix it, so it outranks the reboot offer, and
      # no visit fixes a dead box, so it outranks the measured half of `technician_visit`.
      rung("HandleConvoySwap", "verdict_convoy_swap",
           scripts.HARDWARE_SWAP_CONVOY, scripts.SAY_HARDWARE_SWAP_CONVOY),
      rung("HandleHardwareSwap", "verdict_hardware_swap",
           scripts.HARDWARE_SWAP_GATEWAY, scripts.SAY_HARDWARE_SWAP_GATEWAY),
  ]
