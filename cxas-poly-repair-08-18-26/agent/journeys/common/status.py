"""The status vocabulary the whole repair flow reads and writes."""

import flows


# Sharing is per-SLOT at runtime — the engine derives `sm["_shared_slots"]` from
# `shared: true` and never reads the top-level key — so the flow-level policy is
# declaration, not mechanism, and the validator checks the two agree.
SHARED_STATUS = (
    # Shared because it is a fact about the CALL, not about a flow: a caller who spoke
    # before being routed has still spoken afterwards.
    "caller_spoke",
    "account_status", "outage_status", "convoy_status", "network_status",
    "gateway_status", "wifi_status", "cable_modem_mac", "device_id",
    "outage_message", "customer_message", "convoy_customer_message",
    "technician_type",
    # The fee question is usually asked AFTER a verdict, so the value has to survive the
    # `reset_on_complete` re-arm that empties `filled`. The hook seeds it every turn from
    # the app's own `technician_fee` variable.
    "technician_fee",
    # The only thing stopping a second reboot, and an unshared flag would be emptied by
    # the re-arm — so a caller who asked twice would get two restarts.
    "reboot_done",
    # The inquiry's latches — one answer, one close, one consent gate. All shared: the
    # journey spans several turns and a re-arm in the middle would restart it and re-offer
    # the full check to someone who already declined.
    "inquiry_answered", "inquiry_closed", "full_check_allowed",
    "activityType", "activityCode", "jobType",
    # The Wi-Fi walkthrough's own latches. Each tip sets one so it speaks once, and the
    # hook counts them to decide when three turns have been spent.
    "wifi_offered", "wifi_answer_allowed", "wifi_tips_exhausted",
    "wifi_scope_asked", "wifi_scope_allowed",
    # `wifi_offered_early` records that the offer was made DURING the sweep,
    # `all_clear_told` is the latch of the all-clear that does not offer twice, and
    # `scope_noted_late` keeps the acknowledgement of an early answer to one turn.
    # `scope_unsure_ack` does the same for the caller who answered "I don't know".
    "wifi_offered_early", "all_clear_told", "scope_noted_late", "scope_unsure_ack",
    "wifi_tip_given",
    "wifi_tip_rejoin", "wifi_tip_closer", "wifi_tip_toggle", "wifi_tip_restart",
    "wifi_tip_placement", "wifi_tip_nearby",
    # `wifi_tip_spent` is the hook's "a tip has been given at all", derived from the six
    # above; `wifi_tip_ack` keeps the acknowledgement of an answer to one to the single
    # turn the sweep reports on.
    "wifi_tip_spent", "wifi_tip_ack",
    "wifi_closed",
    # Written by whichever rung fires; read by every rung's condition.
    "verdict_delivered",
    # Set by the hook once the sweep has run; every rung requires it.
    "diagnostics_complete",
)


# Values a rung INTERPOLATES, and which therefore must never be absent: an unresolvable
# `{placeholder}` makes the engine raise while rendering `then_say`, and the caller hears
# the CES crash envelope rather than the verdict. `default` resolves them during the fill
# stages, before the DAG walk, so the render always has something; `publish` re-states
# them into session state every turn, which is where the carried transfer tools read them.
#
# `technician_fee`'s floor is the same literal `substrate/app.json` declares. It is a
# floor for a deployment that dropped the declaration, not a second source of truth.
_INTERPOLATED = {
    "activityType": "TROUBLE_CALL",
    "activityCode": "",
    "jobType": "",
    "technician_fee": "$100",
}


def shared_status_slots():
  """The status vocabulary every journey reads, shared across flows."""
  # No default on `diagnostics_complete`: `Settle` fills it as a real tool output, and a
  # default would falsify `Settle`'s own "not yet complete" gate before it is eligible.
  out = []
  for _status in SHARED_STATUS:
    if _status in _INTERPOLATED:
      out.append(dict(flows.event_slot(_status, default=_INTERPOLATED[_status],
                                       publish=[_status]), shared=True))
    else:
      out.append(dict(flows.event_slot(_status), shared=True))
  return out
