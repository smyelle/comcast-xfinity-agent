"""Everything we can measure looks healthy."""

import scripts
from journeys.common.rungs import offer_rung


def tasks():
  """Everything we can measure looks healthy."""
  return [
      # P10 — everything healthy. Last rung, so it only speaks when nothing else matched.
      #
      # Both are gated on `device_searched`: the engine keeps walking tasks within a turn,
      # and a verbatim `then_say` here beats the device search's directive, so an all-clear
      # about the LINE would be spoken over an answer about a BOX.
      #
      # The already-trying wording is declared FIRST so it outranks the offering version:
      # both match the same statuses, and only this one knows not to offer twice.
      offer_rung("HandleAllClearAlreadyTrying", "verdict_all_clear_already_trying",
                 {"all": [scripts.ALL_CLEAR_ALREADY_TRYING,
                          {"slot": "device_searched", "filled": False}]},
                 scripts.SAY_ALL_CLEAR_ALREADY_TRYING, latch="all_clear_told"),

      offer_rung("HandleAllClear", "verdict_all_clear",
                 {"all": [scripts.ALL_CLEAR,
                          {"slot": "device_searched", "filled": False}]},
                 scripts.SAY_ALL_CLEAR, latch="wifi_offered"),
  ]
