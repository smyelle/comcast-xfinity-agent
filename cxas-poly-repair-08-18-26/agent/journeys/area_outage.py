"""There is an outage in the caller's area."""

# The caller asked one question. They get the answer, and then a choice — not a
# diagnostic ladder they never asked to be put on.
#
# No idiom in the closing question: the guidelines rule them out, and a member who learned
# English elsewhere has to translate one before they can answer.
SAY_INQUIRY_NO_OUTAGE = (
    "Good news, there's no outage reported in your area right now. Would you like me "
    "to run a full check on your connection?"
)

SAY_INQUIRY_DECLINED = (
    "No problem at all. If anything changes, we're here any time through the Xfinity "
    "app or website."
)

# The inquiry rungs gate on the caller's INTENT, matched from their own words, and are
# declared ahead of the ladder so they answer first. They cannot gate on "the sweep did
# not run": the hook runs before the engine cue-matches the turn, so it never knows the
# call is an inquiry in time to skip the sweep. What keeps the ladder from talking over
# the answer is that these rungs latch `verdict_delivered` themselves — and the hook
# clears it again, once, if the caller consents.
_INQUIRING = [{"slot": "call_intent", "eq": "outage_inquiry"},
              {"slot": "inquiry_answered", "filled": False}]

INQUIRY_OUTAGE_FOUND = {"all": _INQUIRING + [{"slot": "outage_status", "eq": "active"}]}

# `filled` as well as `neq`, because `neq` holds on an UNFILLED slot: without it this
# rung answers "there's no outage reported in your area right now" on the turn the caller
# asks, before the outage leg has run and with nothing at all checked. The outage-found
# rung needs no such guard -- `eq: "active"` cannot hold on an unset slot.
INQUIRY_NO_OUTAGE = {"all": _INQUIRING + [{"slot": "outage_status", "filled": True},
                                          {"slot": "outage_status", "neq": "active"}]}

# The consent answer, gated the same way the reboot and Wi-Fi answers are: a question
# asked and answered inside one turn is the model answering for the caller.
_INQUIRY_ANSWERABLE = [{"slot": "inquiry_answered", "filled": True},
                       {"slot": "full_check_allowed", "eq": "true"}]

FULL_CHECK_ASKABLE = {"all": _INQUIRY_ANSWERABLE}

FULL_CHECK_DECLINED = {"all": _INQUIRY_ANSWERABLE + [
    {"slot": "full_check", "eq": "DECLINE"},
    {"slot": "inquiry_closed", "filled": False}]}

# No RESOLVED branch, unlike the walkthrough offer: nothing has been attempted yet for
# the caller to report on.
FULL_CHECK_CLASSIFIER = {
    "ACCEPT": ["yes", "yeah", "sure", "ok", "okay", "please", "go ahead", "please do",
               "that would be great", "why not", "alright", "if you don't mind"],
    "DECLINE": ["no", "no thanks", "not now", "that's all", "that's it", "i'm good",
                "just wanted to know", "just checking", "don't have time",
                "another time", "that's all i needed"],
}

# The scope and reboot slots have no filler of their own: each is followed in the SAME
# engine turn by a rung that carries one, so a second buys no time to first audio and
# stacks acknowledgements into one breath. This slot is not.
#
# Every line here opens on a word no other live pool opens on, which is what
# `check_filler_pool_collisions` enforces.
FILLER_FULLCHECK = ["Of course.", "Yes, let's do that.", "Will do."]

__all__ = [
    'FILLER_FULLCHECK',
    'FULL_CHECK_ASKABLE',
    'FULL_CHECK_CLASSIFIER',
    'FULL_CHECK_DECLINED',
    'INQUIRY_NO_OUTAGE',
    'INQUIRY_OUTAGE_FOUND',
    'SAY_INQUIRY_DECLINED',
    'SAY_INQUIRY_NO_OUTAGE',
    '_INQUIRING',
    '_INQUIRY_ANSWERABLE',
]


import clarify
import flows
import scripts
from journeys.common.rungs import rung, say_rung
from journeys.common.waiting import with_filler


def inquiry_slots():
  """The caller who rang to ASK about an outage rather than report a fault."""
  return [
      # Cue-only, and read by the HOOK rather than by a condition: it decides whether the
      # sweep runs at all, which is a thing no rung can gate.
      flows.passive_slot("call_intent", setter="", kind="intent",
                         option_cues=clarify.CALL_INTENT_CUES),
      # Model-callable, unlike the slot above, because "that's all I needed" is the sort
      # of paraphrase a cue list is bad at — and the worst case here is running a
      # diagnostic the caller did not want, not acting on their equipment.
      with_filler(flows.intent_slot("full_check", FULL_CHECK_CLASSIFIER,
                                    condition=FULL_CHECK_ASKABLE),
                  FILLER_FULLCHECK),
  ]


# Outside the main ladder because these run on a DIFFERENT picture: the hook has checked
# the outage and nothing else, so `diagnostics_complete` is unset and every ladder rung is
# shut. These three are the only ones eligible until the caller consents to the full
# check, at which point the hook sweeps and the ladder takes over normally.
def inquiry_tasks():
  """Answer the caller who rang to ASK about an outage rather than report a fault."""
  return [
      # Same advisory the repair journey gives, because it is the same fact and the same
      # approved copy.
      say_rung("InquiryOutageFound", "verdict_inquiry_outage",
               INQUIRY_OUTAGE_FOUND, scripts.SAY_AREA_OUTAGE,
               latch="inquiry_answered"),
      # No outage: answer, then OFFER. Latching `inquiry_answered` opens the consent gate
      # on the NEXT turn.
      say_rung("InquiryNoOutage", "verdict_inquiry_no_outage",
               INQUIRY_NO_OUTAGE, SAY_INQUIRY_NO_OUTAGE,
               latch="inquiry_answered"),
      # Declined. A warm close, not a hand-off: nothing is wrong.
      say_rung("InquiryDeclined", "verdict_inquiry_declined",
               FULL_CHECK_DECLINED, SAY_INQUIRY_DECLINED,
               latch="inquiry_closed", ends=False),
  ]


def verdict():
  """There is an outage in the caller's area, so their line is not the problem."""
  return [
      rung("HandleAreaOutage", "verdict_area_outage", scripts.AREA_OUTAGE,
           scripts.SAY_AREA_OUTAGE),
  ]
