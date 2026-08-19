"""Will this cost me anything."""

# R5, the source's Priority 13. `{technician_fee}` stays a placeholder because
# the fee is an app variable; the hook seeds it on every turn, since an unresolvable
# `{placeholder}` makes the engine RAISE while rendering and this rung can fire on any
# turn of the call.
#
# No em-dashes in spoken copy (FLV001): a dash can cut the audio at the break.
SAY_NO_CHARGE = (
    "No, nothing we're doing here costs anything. This call and any troubleshooting we "
    "try together are free. The only thing that can carry a charge is a technician "
    "visit, and I'd tell you before we booked one."
)

# THE FEE SCHEDULE. It is the source policy rewritten for the ear, not the source
# paragraph, so a fidelity diff against the source agent is expected to show it.
#
# THREE COMMITMENTS, and they are the reason this string is reviewed rather than edited.
# Each one maps one-to-one onto a sentence, and none may be softened, widened or
# narrowed:
#   1. an install visit is chargeable         -> sentence 2
#   2. a visit that finds a fault which is not ours is chargeable -> sentence 3
#   3. an existing member with a fault that IS ours pays nothing  -> sentence 4
# "our service" and "equipment you rent from us" carry the whole scope of the policy: the
# first-person plural is the voice the guidelines ask for, and it also keeps the word
# "Xfinity" from landing twice in one answer.
#
# Deliberately NOT merged with `SAY_FEE_AGAIN` below: the short version drops the install
# charge and the waiver, so it answers a narrower question and cannot stand in for the
# schedule.
SAY_SERVICE_FEE = (
    "A visit can carry a fee. If a technician comes out to finish setting up your "
    "service, that's a {technician_fee} charge. If they find the problem isn't with our "
    "service or with equipment you rent from us, that's a {technician_fee} charge too. "
    "If the problem is ours and you're already a member, there's no charge."
)

# The SECOND time they ask. A completed task does not fire again, so re-answering needs
# a rung of its own, and the short version is the right thing for it to speak.
#
# Three sentences, so the promise to warn them lands on its own rather than at the tail
# of a conditional the caller is still parsing. The closing clause matches `SAY_NO_CHARGE`
# word for word so the two no-charge answers cannot drift apart.
SAY_FEE_AGAIN = (
    "To be clear, there's no charge for this call or anything we try together. A "
    "charge only applies if a technician comes out and finds the problem isn't ours. "
    "I'd tell you before we booked one."
)

FEE_ASKED_AGAIN = {"all": [{"slot": "cost_question", "filled": True},
                           {"slot": "cost_answered", "filled": False},
                           {"slot": "fee_answered_once", "filled": True}]}

# Two answers, because there are two questions and the caller means one of them: the fee
# schedule only applies once a visit is actually on the table, and otherwise the honest
# answer is one sentence.
#
# A SWAP IS NOT A VISIT. Both swap scripts send the caller to a store or the website, so
# nothing chargeable has been proposed and neither belongs here. An impairment underneath
# a swap still selects the schedule, through the `network_status` leg: which verdict
# SPEAKS is the ladder's business, and is a different question from whether a technician
# is coming.
_TECH_ON_THE_TABLE = {"any": [{"slot": "network_status", "eq": "impaired"},
                              {"slot": "convoy_status", "eq": "predictive_impairment"}]}

_COST_ASKED_BASE = [{"slot": "cost_question", "filled": True},
                    {"slot": "cost_answered", "filled": False},
                    {"slot": "fee_answered_once", "filled": False}]

COST_ASKED_VISIT = {"all": _COST_ASKED_BASE + [_TECH_ON_THE_TABLE]}

COST_ASKED_NO_VISIT = {"all": _COST_ASKED_BASE + [{"not": _TECH_ON_THE_TABLE}]}

__all__ = [
    'COST_ASKED_NO_VISIT',
    'COST_ASKED_VISIT',
    'FEE_ASKED_AGAIN',
    'SAY_FEE_AGAIN',
    'SAY_NO_CHARGE',
    'SAY_SERVICE_FEE',
    '_COST_ASKED_BASE',
    '_TECH_ON_THE_TABLE',
]


import clarify
import flows
import scripts
from journeys.common.rungs import say_rung


def question_slot():
  """Hearing the caller ask whether this will cost them."""
  return [
      # `setter=""`: pricing is not the model's to decide, and the answer is a verbatim
      # the engine speaks. Cue-only means the model can neither mark the question asked
      # nor mark it unasked.
      flows.passive_slot("cost_question", setter="", kind="intent",
                         option_cues=clarify.COST_CUES),
  ]


def answer_slots():
  """Remembering that the fee question has been answered."""
  return [
      # The rung's own latch, so answering the fee question does not close the diagnostic
      # ladder behind it.
      flows.event_slot("cost_answered"),
      # Durable, unlike `cost_answered` which the hook clears each turn: this is what
      # tells a later ask that the full schedule has already been heard once.
      flows.event_slot("fee_answered_once"),
  ]


def tasks():
  """Will this cost me anything."""
  # Not a verdict, and not gated on the ladder being open: these latch `cost_answered`
  # rather than `verdict_delivered`, so the fee answer and the diagnosis can land in the
  # same breath, and the question can still be answered after a technician verdict.
  return [
      # Asked again. Declared first so it wins over the two first-time answers, which are
      # gated on `fee_answered_once` being unset.
      say_rung("AnswerFeeAgain", "verdict_fee_again", FEE_ASKED_AGAIN,
               SAY_FEE_AGAIN, latch="cost_answered",
               requires=["cost_question"]),
      # A technician IS on the table, so the whole fee schedule applies and is spoken.
      say_rung("AnswerServiceFee", "verdict_service_fee", COST_ASKED_VISIT,
               SAY_SERVICE_FEE, latch="cost_answered",
               requires=["cost_question"]),
      # Nothing chargeable has been proposed, so the answer is simply no.
      say_rung("AnswerNoCharge", "verdict_no_charge", COST_ASKED_NO_VISIT,
               SAY_NO_CHARGE, latch="cost_answered",
               requires=["cost_question"]),
  ]
