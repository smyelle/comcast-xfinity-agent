"""Telling the caller they were heard, without spending the turn on it."""

# Spoken BEFORE the substantive answer, on the same turn: these rungs latch their own
# flag and leave the ladder open, and the engine keeps walking tasks within a turn. That
# co-firing is the whole mechanism; as a rung of its own it would spend a turn saying
# nothing useful. Each fires once per call.
#
# NEITHER LINE APOLOGIZES. C4 bans "sorry" and every equivalent, in every channel, with
# no exemption for a consoling turn. These are the only two consoling turns in the call,
# so the warmth has to come from somewhere else: each line names what the caller is
# feeling and then takes the next step.
SAY_ACK_FRUSTRATION = (
    "I can hear how frustrating this has been, and I've got you. Let's get it fixed."
)

# The second sentence is a promise the walkthrough actually keeps: `wifi_tried` records
# WHICH tip the caller has already done, and the ladder skips it. Do not widen it into a
# promise the code cannot keep.
SAY_ACK_ALREADY_TRIED = (
    "Thanks for telling me. I'll skip what you've already tried."
)

# Never on a turn that is still collecting the account number. A cue slot that FILLS on a
# turn suppresses the pending slot's setter, so an acknowledgement there costs the number
# and the caller is asked for it twice.
_ACCOUNT_KNOWN = {"slot": "accountNumber", "filled": True}

FRUSTRATED = {"all": [{"slot": "frustration", "filled": True},
                      {"slot": "frustration_ack", "filled": False},
                      _ACCOUNT_KNOWN]}

ALREADY_TRIED = {"all": [{"slot": "already_tried", "filled": True},
                         {"slot": "already_tried_ack", "filled": False},
                         _ACCOUNT_KNOWN,
                         # If they are BOTH fed up and repeating themselves, the
                         # acknowledgement above covers it; two in one turn is worse.
                         {"not": {"slot": "frustration", "filled": True}}]}

__all__ = [
    'ALREADY_TRIED',
    'FRUSTRATED',
    'SAY_ACK_ALREADY_TRIED',
    'SAY_ACK_FRUSTRATION',
    '_ACCOUNT_KNOWN',
]


import clarify
import flows
import scripts
from journeys.common.rungs import say_rung


def slots():
  """Noticing that the caller is fed up, or has already tried the obvious."""
  return [
      # Cue-only: whether the caller is fed up is not the model's to decide, and an
      # acknowledgement fired on a guess is worse than none. Both listen only once
      # collection is done, for the reason recorded at `_ACCOUNT_KNOWN`.
      flows.passive_slot("frustration", setter="", kind="intent",
                         condition={"slot": "accountNumber", "filled": True},
                         option_cues=clarify.FRUSTRATION_CUES),
      flows.passive_slot("already_tried", setter="", kind="intent",
                         condition={"slot": "accountNumber", "filled": True},
                         option_cues=clarify.ALREADY_TRIED_CUES),
      flows.event_slot("frustration_ack"),
      flows.event_slot("already_tried_ack"),
  ]


def tasks():
  """Tell the caller they were heard, without spending the turn on it."""
  return [
      say_rung("AckFrustration", "verdict_ack_frustration", FRUSTRATED,
               SAY_ACK_FRUSTRATION, latch="frustration_ack"),
      say_rung("AckAlreadyTried", "verdict_ack_already_tried", ALREADY_TRIED,
               SAY_ACK_ALREADY_TRIED, latch="already_tried_ack"),
  ]
