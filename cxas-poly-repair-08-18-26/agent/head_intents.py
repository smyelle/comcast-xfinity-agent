"""Level-2 head-intent TAXONOMY + shared data (the hand-rolled L2 builders now live elsewhere).

The steering router (L1) picks the golden `intent` CATEGORY (billing, sales, ...). Within a
category there is a SECOND pass: classify the caller's utterance into a leaf `head_intent`
(billing_toohigh, payments_make_payment, ...). The taxonomy is head_intents.json (derived from
the golden agent — see derive_head_intents.py), so the ~84 leaves and their descriptions stay
in step with golden.

The L2 CLASSIFICATION is now expressed as the first-class flows `route(subroutes=)` steering
primitive — see steering_tree.py, which reads the same taxonomy and HEAD_CUES. The old
hand-rolled slot/setter builders (`head_intent_slot`, `head_setter_bodies`, `head_hint`,
`_setter_source`) were removed with that migration.

What remains here is DATA consumed by the test tooling: the taxonomy load (`HEAD_INTENTS`),
the leaf->L1 map (`l1_of`), the per-category `default_leaf`, the shared slot name (`SLOT`),
and the deterministic `HEAD_CUES` phrasings (tests/check_head_cues.py and
tests/gen_head_intent_testset.py regex-extract the HEAD_CUES block from this source).
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = json.load(open(os.path.join(HERE, "head_intents.json")))
# {category: {target_flow, lob, head_intents:{leaf:description}, disambiguation_question}}
HEAD_INTENTS = _DATA["categories"]

# The slot name is shared across category flows; the SETTER is per-category so each flow
# validates against its OWN leaf set (a different allowlist).
SLOT = "head_intent"

# leaf head_intent -> L1 category. Inverted from the taxonomy so a detected leaf ALWAYS
# rolls up to exactly one L1 we already route to — the head intent can never drift out of
# our L1 set, and the eval can check the detected leaf against the routed category.
HEAD_TO_L1 = {
    leaf: cat
    for cat, spec in HEAD_INTENTS.items()
    for leaf in spec["head_intents"]
}


def l1_of(leaf: str) -> str:
  """The L1 category a leaf rolls up to (or '' if unknown)."""
  return HEAD_TO_L1.get(leaf, "")


def _leaves(cat: str) -> dict:
  return HEAD_INTENTS[cat]["head_intents"]


def default_leaf(cat: str) -> str:
  """A safe fallback leaf when the model's output matches nothing — the least-committal
  (a general/discuss/troubleshoot leaf), else the first declared."""
  leaves = list(_leaves(cat))
  for kw in ("_general", "discuss", "troubleshoot", "_gen", "miscellaneous", "general"):
    for leaf in leaves:
      if kw in leaf:
        return leaf
  return leaves[0]


# High-precision deterministic cues — the L2 backstop, mirroring L1's ROUTE_CUES. A matched
# cue fills the leaf in the engine (no model round-trip). Kept FEW and UNAMBIGUOUS: L2 leaves
# overlap within a category, so a broad cue would deterministically preempt the LLM with the
# WRONG leaf. Every phrase is a generic production phrasing that is NOT a substring of any eval
# utterance (golden or held-out) — asserted by tests/check_head_cues.py — so the held-out set
# still measures the MODEL, not the cues. Cues are scoped per-category (only a category's own
# leaves appear in its slot), so they never fire cross-category. Grow from real traffic.
HEAD_CUES = {
    "payments_make_payment": ["pay online", "one-time payment", "submit a payment"],
    "payments_manage_autopay": ["enroll in autopay", "manage autopay", "set up auto pay"],
    "payments_method_update": ["update my card on file", "change my payment method"],
    "billing_refund_inquiry": ["get a refund", "refund status", "issue a refund"],
    "billing_toohigh": ["bill is too expensive", "charged way more than usual"],
    "billing_manage_ecobill": ["paperless billing", "go paperless"],
    "plan_upgrade": ["upgrade my plan", "upgrade my package"],
    "plan_downgrade": ["downgrade my plan", "cheaper plan"],
    "tv_troubleshoot_channels": ["missing channels", "channels are gone"],
    "tv_troubleshoot_dvr": ["dvr not recording", "dvr playback"],
    "account_transfer_service": ["moving my service", "transfer my service to a new home"],
    "plan_manage_temporary_disconnect": ["seasonal hold", "vacation hold", "suspend my service"],
    "appointment_change_schedule": ["reschedule my appointment", "change my appointment time"],
    "appointment_schedule_appointment": ["schedule a technician", "book an appointment"],
    "appointment_cancel_service": ["cancel my appointment"],
    "voice_troubleshoot": ["phone line is dead", "static on the line"],
    "voice_voicemail_troubleshoot": ["voicemail not working", "set up my voicemail"],
    "account_equipment_return": ["return my equipment", "drop off my equipment"],
    "customer_support_locate_service_center": ["nearest store", "closest service center"],
}
