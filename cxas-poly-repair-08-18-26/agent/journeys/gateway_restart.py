"""Restarting the caller's gateway: on request, or by offer."""

# ONE noun for the thing being rebooted, all the way through the offer: a second noun for
# the same box reads as a second piece of hardware. The noun is repeated rather than
# pronouned, because "reboot it" would collide with the "it" one clause earlier, which is
# the ISSUE and not the box.
#
# "me", not "us": this is XA's own action, and the assistant speaks for itself when it is
# the one doing something.
SAY_REBOOT_ASK = (
    "I found an issue with your gateway and a reboot should fix it. Would you like me "
    "to reboot your gateway now?"
)

# The RE-ask, deliberately not the same sentence the OfferReboot rung speaks. When the
# clarification gate runs first the sweep lands a turn earlier than the offer, so answers
# are already allowed when the rung fires and the engine joins both lines into one turn;
# a shorter fallback reads as a nudge there rather than as a stutter.
SAY_REBOOT_REASK = "Would you like me to go ahead with that reboot?"

# The holding line, and the whole point of it is that it promises NOTHING. `filler_say`
# is spoken as the call is DISPATCHED, so the approved sentence — which asserts the
# signal has gone — belongs in `then_say` behind `success_check`, where a refused reboot
# never reaches it. This line has to exist rather than the filler simply being dropped:
# a rung with `filler_say` and no `then_say` is the mute shape, and a rung with neither
# leaves the Convoy round trip as dead air.
SAY_REBOOT_HOLD = "Okay, give me just a moment."

# The account and outage blockers are the source's own Priority-0 bypass, carried here
# because ladder position cannot cover them: this rung is not gated on the ladder being
# open. The two swap guards are a deliberate ADDITION — a reboot cannot fix a gateway
# that needs replacing, and the caller still gets the swap verdict.
REBOOT_REQUESTED = {"all": [
    {"slot": "reboot_request", "filled": True},
    {"slot": "reboot_done", "filled": False},
    {"slot": "account_status", "not_in": ["suspended", "disconnected",
                                          "pending activation"]},
    {"slot": "outage_status", "not_in": ["active", "degradation"]},
    {"slot": "gateway_status", "neq": "swap"},
    {"slot": "convoy_status", "neq": "predictive_swap"},
]}

# The confirm_reboot slot only exists once the offer has been spoken.
REBOOT_ANSWERABLE = {"all": [{"slot": "reboot_offered", "filled": True},
                             {"slot": "reboot_answer_allowed", "eq": "true"}]}

__all__ = [
    'REBOOT_ANSWERABLE',
    'REBOOT_REQUESTED',
    'SAY_REBOOT_ASK',
    'SAY_REBOOT_HOLD',
    'SAY_REBOOT_REASK',
]


import clarify
import flows
import scripts
from journeys.common.rungs import offer_rung, reboot_rung, rung


def request_slot():
  """Hearing the caller ask for a restart outright."""
  return [
      # `setter=""` matters more here than anywhere else, because this slot causes a
      # REBOOT: cue-only means the caller's own words are the only thing that can trigger
      # one, and the model cannot decide on their behalf that they asked.
      flows.passive_slot("reboot_request", setter="", kind="intent",
                         option_cues=clarify.REBOOT_REQUEST_CUES),
  ]


# Not shared: the hook re-derives `reboot_answer_allowed` every turn from the offer
# having actually been spoken, and nothing that survives a re-arm reads the other two.
def gate_slots():
  """Guards that decide whether the restart question may be answered yet."""
  return [flows.event_slot(_status) for _status in (
      "convoy_routing_action",
      # Guards against the model answering the reboot question itself.
      "reboot_answer_allowed", "reboot_offered")]


# The one branching question, armed only when a reboot is actually on the table.
def confirm_slot():
  """Consume the caller's yes or no to the restart offer."""
  return [
      flows.user_slot(
          "confirm_reboot",
          ask=SAY_REBOOT_REASK,
          # No `filler_say`: the reboot rung's own filler is dispatched in the same engine
          # turn, so a second one buys no time to first audio and stacks openers.
          hint="confirm reboot",
          condition=REBOOT_ANSWERABLE,
          validation={
              "max_retries": 3,
              "errors": {
                  # A spoken re-ask, not a form-field error: on the phone the form
                  # register lands as a telling-off for a caller whose answer was simply
                  # not understood. Offering the two words back is what gets the turn
                  # moving.
                  "invalid_format": (
                      "You can say yes to reboot your gateway, or no to skip."
                  )
              },
              "on_exhaust": {
                  # Say what happened, not that the machine is struggling. Naming the
                  # actual gap, no clear yes or no, is what makes the hand-off make
                  # sense to the caller.
                  "say": (
                      "I'm not getting a clear yes or no. Let me connect you with "
                      "someone who can help."
                  ),
                  "then": {"tool": "verdict_no_telemetry"},
              },
          },
      )
  ]


def on_request():
  """The caller asked for a restart outright, without being offered one."""
  return [
      # R6 — the caller asked for a reboot outright. Sits below every measured plant fault
      # so an outage or a pending swap is still what they hear about, and names those
      # states in its condition too: unlike the rungs above it is not gated on the ladder
      # being open, because the request can arrive on any turn.
      reboot_rung("RebootOnRequest", "verdict_reboot_on_request",
                  REBOOT_REQUESTED, scripts.SAY_REBOOT_WHOLE,
                  inputs=["device_id"], say_first=SAY_REBOOT_HOLD,
                  latch="reboot_done", gated=False),
  ]


def handshake():
  """We offer a restart, and act on the answer."""
  return [
      # P7 — reboot, once the caller has answered. The question itself is the
      # confirm_reboot slot above; these two consume the answer.
      offer_rung("OfferReboot", "verdict_offer_reboot", scripts.REBOOT_OFFER, SAY_REBOOT_ASK),
      # The approved sentence is the `then_say`, behind `success_check="rebooted"`: the
      # engine maps outputs only when the success key is truthy, so a gateway that refuses
      # the reboot never latches `verdict_delivered` and drops into the failure ladder.
      #
      # `max_retries: 0`, because a retry re-sends identical inputs to a gateway that just
      # refused them and spends another turn hitting the same block. `on_exhaust.then`
      # fires the transfer TOOL rather than the declined RUNG, so the blocked script
      # carries its own hand-off sentence.
      reboot_rung("ExecuteReboot", "verdict_execute_reboot", scripts.REBOOT_CONFIRMED,
                  scripts.SAY_REBOOT_WHOLE, inputs=["device_id"],
                  say_first=SAY_REBOOT_HOLD),
      rung("DeclineRebootTransfer", "verdict_reboot_declined",
           scripts.REBOOT_DECLINED, scripts.SAY_REBOOT_DECLINED),
  ]
