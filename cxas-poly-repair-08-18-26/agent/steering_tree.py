"""Build the multi-level (hierarchical) steering router from the golden taxonomy.

Level 1 is the model routing turn: the router classifies the opening utterance into a
golden CATEGORY (`set_active_flow`). Each DEFER category is an INTERNAL node whose
children are that category's golden head-intent LEAVES (from `head_intents.json`); once
the caller is routed there, a scoped classifier picks the leaf from the SAME utterance
and the generated recorder writes `detected_intent` (the leaf) + `detected_path`
(`<category>/<leaf>`) for the downstream GECX orchestration to route on. The three
HANDLED categories (`repair`/`reboot`/`human`) are leaves that run a local DAG.

This is the flows-SDK `flows.route(name, description, *, flow=, cues=, subroutes=,
disambiguate=, default=)` primitive (flows>=0.11.0) recreating cxas-comcast PR #14's
hand-rolled `head_intents.py` L2 detector — same taxonomy, same eval set, expressed as a
first-class steering tree instead of per-flow `head_intent` slots + bespoke setters.

Why the L2 levels are ASKED, not silent
---------------------------------------
A SILENT (passive) sub-intent slot gets no model directive, so it fills only from its
deterministic `cues`. The head-intent eval corpus is deliberately DISJOINT from those
cues (it measures the model's generalization, not memorized phrasings), and only ~19 of
the ~84 leaves carry a cue at all — a silent level would collapse to the default leaf on
almost every row. So each internal node is given `disambiguate=disambiguation(max_turns=1)`,
which makes its slot an ASKED slot: it still fills SILENTLY when the utterance is clear
(the eval case), and only asks a single clarifying question on genuine ambiguity before
falling to `default=`. This mirrors exactly what PR #14 did (a non-passive slot with
`max_retries=1` + `on_exhaust_fill`), which is what earned its ~100% conditional L2.
"""
from __future__ import annotations

import json
import os

import flows

from source_tools import ROUTE_CATALOGUE

HERE = os.path.dirname(os.path.abspath(__file__))

# The golden 84-leaf / 16-category taxonomy, keyed by L1 category. Carried verbatim from
# PR #14 (derive_head_intents.py regenerates it from the GECX golden export). 15 of the 16
# categories are `defer` and get an L2 classifier here; `technical_internet` is resolved at
# L1 by the diagnostics/reboot handlers, so it has no defer flow and no L2 slot.
_TAX = json.load(open(os.path.join(HERE, "head_intents.json")))["categories"]


# Deterministic L2 fast-path: a matched cue fills the leaf with no model round-trip, the
# level-2 counterpart to the L1 ROUTE_CUES. Kept few and high-precision, and asserted
# DISJOINT from every eval utterance (tests/check_head_cues.py) so the measured L2 number
# is the model's generalization. Verbatim from PR #14's head_intents.HEAD_CUES.
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

# The least-committal leaf a silent/asked level falls back to when nothing classifies.
# Same priority order PR #14's default_leaf() used, so the fallback leaf per category is
# identical (several are counterintuitive, e.g. billing -> billing_discuss, main-menu ->
# business_general): scan leaves in JSON order for the first containing each hint in turn.
_DEFAULT_HINTS = ("_general", "discuss", "troubleshoot", "_gen", "miscellaneous", "general")


def default_leaf(cat: str) -> str:
  """The fallback child for a category (PR #14's default_leaf order); else the first leaf."""
  leaves = list(_TAX[cat]["head_intents"])
  for hint in _DEFAULT_HINTS:
    for leaf in leaves:
      if hint in leaf:
        return leaf
  return leaves[0]


def _leaf_route(leaf: str, desc: str) -> "flows.route":
  """One L2 leaf: a deferred child route carrying its golden description + any L2 cues."""
  return flows.route(leaf, desc, cues=HEAD_CUES.get(leaf, []))


def _defer_route(cat: str, l1_desc: str, l1_cues: list[str]) -> "flows.route":
  """An INTERNAL node: the L1 category, with its golden leaves as silent-when-clear children."""
  leaves = _TAX[cat]["head_intents"]
  return flows.route(
      cat, l1_desc,
      cues=l1_cues,
      subroutes=[_leaf_route(leaf, desc) for leaf, desc in leaves.items()],
      # ASKED with a 1-turn budget -> the model classifies the leaf silently from a clear
      # utterance and only asks on genuine ambiguity, then lands on `default`. See module
      # docstring for why this beats a silent/passive level on the cue-disjoint eval.
      disambiguate=flows.disambiguation(max_turns=1),
      default=default_leaf(cat),
  )


# Routes the model must never reach by INFERENCE — only on an explicit request. `human`
# owns the live-agent hand-off and cancel/disconnect, both of which should be caller-stated,
# never guessed; marking it explicit_only is the generic guard against over-escalation.
_EXPLICIT_ONLY = {"human"}


def build_routes(handled_flows: dict, route_cues: dict) -> list:
  """The full L1 route list, in ROUTE_CATALOGUE order.

  `handled_flows` maps each `handle` key to its DAG Flow (repair/reboot/human); `route_cues`
  is the L1 deterministic-backstop map (a handle/defer key -> cue phrases). Handled keys
  become leaves that run their flow; defer keys become internal nodes (see `_defer_route`).
  """
  routes = []
  for key, kind, desc in ROUTE_CATALOGUE:
    if kind == "handle":
      routes.append(flows.route(key, desc, flow=handled_flows[key],
                                cues=route_cues.get(key, []),
                                explicit_only=(key in _EXPLICIT_ONLY)))
    else:
      routes.append(_defer_route(key, desc, route_cues.get(key, [])))
  return routes
