#!/usr/bin/env python3
"""Offline proof that the rungs are still in the order the agent depends on.

Declaration order IS the priority ladder -- first match wins -- and around thirty of the
orderings are load-bearing while existing only as line adjacency. A swap builds cleanly,
leaves every condition correct, and changes what a caller is told.

Three tables, and the last two are what make the first mean anything:

  ORDER     the intended sequence, asserted as a SUBSEQUENCE of the emitted task list.
  OVERLAPS  a state on which BOTH rungs of a pair are active, so the order between them
            decides what the caller hears. Without it an order assertion only restates
            the file: rungs that can never both match may sit in any order, and pinning
            them blocks a legitimate change for no reason. A pair that stops being a
            contest FAILS rather than passing quietly.
  EXCLUSIVE pairs kept apart by their conditions instead. Their order is not worth
            pinning; their exclusivity is, since it is all that stands between them.

Every row carries its reason, printed on failure.

    python tests/order_check.py [--app-dir ./built]
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import harness  # noqa: E402
from flows.engine import loader  # noqa: E402

# --- The intended sequence ---------------------------------------------------
# One list per flow, asserted as a subsequence rather than an equality: a new rung
# between two of these is a legitimate change, and a swap of two of them is not.

ORDER = {
    "repair": [
        # The inquiry answers on a different picture -- before `diagnostics_complete` --
        # so it has to be able to speak before the ladder can.
        "InquiryOutageFound", "InquiryNoOutage", "InquiryDeclined",
        # The sweep.
        "ContextGate", "Specialists", "Settle",
        # Acknowledgements and the fee answer LEAD a turn: they latch their own flags and
        # are meant to be heard before the diagnosis, not after it. `AckWifiTipAnswer` and
        # `AckScopeBeforeVerdict` are the walkthrough's two rungs up here, for the same
        # reason and no other: the sweep reports on the turn the caller answers a tip or
        # the scoping question, and below the ladder either answer could only be
        # acknowledged after the result had already talked over it.
        "AckFrustration", "AckAlreadyTried", "AckWifiTipAnswer",
        "AckScopeBeforeVerdict",
        "AnswerFeeAgain", "AnswerServiceFee", "AnswerNoCharge",
        # The verdict ladder itself, P1 to P10.
        "HandleBillingBlock", "HandleAccountNotFound", "HandleAreaOutage",
        "HandleMissingHardware",
        "HandleConvoyImpairment", "HandleConvoySwap", "HandleHardwareSwap",
        "HandleNetworkTech", "HandleNetworkImpairment",
        "AdviseAppSpecific",
        "OfferReboot", "ExecuteReboot", "DeclineRebootTransfer",
        "HandleUnsupportedDevice", "HandleNoTelemetry", "HandleDiagnosticError",
        "HandleAllClearAlreadyTrying", "HandleAllClear",
        # The walkthrough sits below the whole ladder, so none of it can outrank a
        # diagnostic verdict.
        # The three answers to the scope question, and the order between them is the same
        # rule as everywhere else here: the specific cases first, the "no scope at all"
        # case last. They are not terminal, so a reorder does not silence one -- it stacks
        # two acknowledgements of one answer into a single breath.
        "AckScopeEarlyAll", "AckScopeEarly", "AckScopeUnsure",
        # ...and the two wordings of asking it again. `AskWifiScopeAgain` first, because
        # the pair share `wifi_scope_asked` and whichever fires closes the other: reversed,
        # the caller who has already heard the question hears it verbatim a second time,
        # which is exactly the defect the second wording exists to fix.
        "AskWifiScopeAgain", "AskWifiScope", "WifiFixed", "WifiDeclined",
        "WifiTipRejoin", "WifiTipCloser", "WifiTipToggle",
        "WifiTipPlacement", "WifiTipNearby", "WifiTipRestart",
        "WifiExhausted",
    ],
    "reboot": [
        "ContextGate", "Specialists", "Settle",
        "RebootBlockedOutage", "RebootBlockedAccount",
        "RebootBlockedSwapGateway", "RebootBlockedSwapConvoy",
        "DoReboot", "RebootNoGateway",
    ],
}

#: Rungs whose absolute position matters, not just their order relative to a neighbour.
LAST = {
    "repair": ("EscalateHandoffSummary",
               "inert on every ordinary turn -- it exists only to carry the hand-off "
               "payload, so anything below it would never be reached"),
}


# --- The contests that give the order its meaning ----------------------------
# `(A, B, state, why)`: on `state` both A and B match, and A must win. `state` is the
# `filled` dict a real turn would present -- the same vocabulary the sweep writes.

_SWEPT = {
    "diagnostics_complete": "true", "caller_spoke": "true",
    "accountNumber": "8344200010126021",
}


def _state(**overrides) -> dict:
  base = dict(_SWEPT, account_status="clear", outage_status="none",
              convoy_status="none", network_status="healthy",
              gateway_status="healthy", wifi_status="healthy",
              cable_modem_mac="AA:BB:CC:DD:EE:FF")
  return {k: v for k, v in {**base, **overrides}.items() if v is not None}


OVERLAPS = [
    ("HandleBillingBlock", "HandleAreaOutage",
     _state(account_status="suspended", outage_status="active"),
     "account standing beats an active outage: there is no point diagnosing a line the "
     "account is not entitled to"),

    ("HandleAreaOutage", "HandleMissingHardware",
     _state(outage_status="active", cable_modem_mac="NOT_FOUND"),
     "an outage with no gateway on file matches both, and reporting the missing gateway "
     "would shadow a live outage the caller is actually in"),

    ("HandleMissingHardware", "HandleNoTelemetry",
     _state(cable_modem_mac="NOT_FOUND", gateway_status="offline"),
     "the gateway-less branch writes gateway_status=offline, which NO_TELEMETRY also "
     "matches: 'there is nothing to measure' must beat 'we measured nothing'"),

    ("HandleConvoySwap", "HandleHardwareSwap",
     _state(convoy_status="predictive_swap", gateway_status="swap"),
     "Settle sets BOTH for routing_action=swap, so declaration order alone picks which "
     "wording the caller hears"),

    ("HandleNetworkTech", "HandleNetworkImpairment",
     _state(network_status="impaired", technician_type="network tech"),
     "NETWORK_TECH is a strict subset of NETWORK_GENERIC; reversed, the technician-type "
     "split dies silently and every impairment gets the charge warning"),

    ("HandleNoTelemetry", "HandleDiagnosticError",
     _state(gateway_status="error"),
     "gateway_status=error satisfies both. Same words, different tool, so a reorder "
     "flips the telemetry label on every gateway error without changing a syllable"),

    ("HandleNoTelemetry", "HandleAllClear",
     _state(gateway_status="skipped"),
     "ALL_CLEAR accepts a skipped gateway and so does NO_TELEMETRY; reversed, a gateway "
     "that was never checked is reported to the caller as healthy"),

    ("AckFrustration", "HandleNetworkTech",
     _state(network_status="impaired", technician_type="network tech",
            frustration="yes"),
     "the apology leads the turn; below the verdict it would be an afterthought to a "
     "caller who has just been told they need a technician"),

    ("AnswerServiceFee", "HandleNetworkTech",
     _state(network_status="impaired", technician_type="network tech",
            cost_question="asked", technician_fee="$100"),
     "the caller asked what it costs, so answer that before the diagnosis rather than "
     "leaving the question hanging behind it"),

    # The walkthrough started during the sweep, one tip has been given, and the caller has
    # just answered it on the turn the job reports. Both verdict shapes are pinned,
    # because the acknowledgement has to lead a healthy result and a fault one alike and
    # they sit at opposite ends of the ladder.
    ("AckWifiTipAnswer", "HandleAllClearAlreadyTrying",
     _state(wifi_offered_early="true", wifi_tip_spent="true", wifi_scope="ONE_DEVICE"),
     "the caller went and did something and came back to say how it went; leading with "
     "the line checks answers a question they did not ask and ignores the one they did"),

    ("AckWifiTipAnswer", "HandleNetworkTech",
     _state(network_status="impaired", technician_type="network tech",
            wifi_offered_early="true", wifi_tip_spent="true", wifi_scope="ONE_DEVICE"),
     "a measured fault ends the walkthrough, so this is the last turn the answer can be "
     "acknowledged at all -- behind the verdict it never is"),

    # One question earlier, and the same contest. The caller answered the scoping question
    # asked during the wait, and the job reported a fault on that same turn. Both fault
    # shapes are pinned: the swap is the one this was reported from, and the dispatch sits
    # at the other end of the ladder.
    ("AckScopeBeforeVerdict", "HandleHardwareSwap",
     _state(gateway_status="swap", AskScopeEarly=True, wifi_scope_early="ALL_DEVICES"),
     "the caller answered the question we asked them; hearing the gateway verdict first "
     "reads as not having listened, and the answer then trails it as an afterthought"),

    ("AckScopeBeforeVerdict", "HandleNetworkTech",
     _state(network_status="impaired", technician_type="network tech",
            AskScopeEarly=True, wifi_scope_early="ONE_DEVICE"),
     "a measured fault ends the walkthrough, so this is the only turn the scope answer "
     "can be acknowledged on at all -- behind the verdict it is an afterthought"),

]


# Pairs that look like an ordering contest and are not: their conditions are mutually
# exclusive, so whichever comes first the caller hears the same thing. Pinning their ORDER
# would be noise; pinning their EXCLUSIVITY is not, because it is all that stands between
# them. Widen either condition and the pair becomes an ordering contest with no ordering
# rule written for it.
EXCLUSIVE = [
    ("AnswerFeeAgain", "AnswerServiceFee",
     _state(network_status="impaired", technician_type="network tech",
            cost_question="asked", fee_answered_once="true", technician_fee="$100"),
     "the fee has been answered once already, so only the short answer may speak"),

    ("HandleAllClearAlreadyTrying", "HandleAllClear",
     _state(wifi_offered_early="true"),
     "the walkthrough was already offered during the wait, so only the rung that knows "
     "that may speak -- otherwise the caller is offered it twice"),

    ("AckScopeEarlyAll", "AckScopeEarly",
     _state(diagnostics_complete=None, network_status=None, gateway_status=None,
            wifi_status=None, convoy_status=None, AskScopeEarly=True,
            wifi_scope_early="ALL_DEVICES"),
     "the caller said everything, so the one-device wording must not also match"),

    # An answer the cues resolved is an answer, so the "we don't know yet" line must not
    # also match it. None of the three is terminal, so an overlap is heard rather than
    # merely ranked: the caller would be acknowledged twice for one reply.
    ("AckScopeEarly", "AckScopeUnsure",
     _state(diagnostics_complete=None, network_status=None, gateway_status=None,
            wifi_status=None, convoy_status=None, AskScopeEarly=True,
            wifi_scope_early="ONE_DEVICE", wifi_scope_unsure="UNSURE"),
     "the caller gave a scope, and 'that's fine, let's see what the checks say' would "
     "put a second acknowledgement of one answer in the same breath"),

    # The two halves of ONE acknowledgement, kept apart by `verdict_delivered`: the answer
    # arrived either on the verdict's turn or after it, never both. They also share
    # `scope_noted_late`, so exclusivity is what stands between the caller and being
    # thanked twice for one answer, in two wordings, one of which points back at a step
    # the other has not described yet.
    ("AckScopeBeforeVerdict", "AckScopeAfterVerdict",
     _state(network_status="impaired", technician_type="network tech",
            AskScopeEarly=True, wifi_scope_early="ONE_DEVICE"),
     "the verdict has not been spoken yet, so only the wording that leads it may speak"),

    # The two wordings of the SAME question, kept apart by the announce's own latch.
    # Their order is pinned above as well, because they share `wifi_scope_asked` and a
    # widened condition would make them an ordering contest rather than an exclusive pair.
    ("AskWifiScopeAgain", "AskWifiScope",
     _state(wifi_offered="true", wifi_answer_allowed="true", wifi_walkthrough="ACCEPT",
            AskScopeEarly=True),
     "the question was already put during the checks, so only the wording that says so "
     "may speak"),
]


def _idx(config: dict) -> dict[str, int]:
  return {t["name"]: i for i, t in enumerate(config.get("tasks", []))}


def check_order(flow: str, config: dict) -> int:
  """The intended sequence must be a subsequence of the emitted one."""
  index = _idx(config)
  failures = 0
  present = [n for n in ORDER[flow] if n in index]
  for missing in [n for n in ORDER[flow] if n not in index]:
    print(f"  FAIL {flow}: {missing} is in the intended order but not in the agent")
    failures += 1
  for earlier, later in zip(present, present[1:]):
    if index[earlier] >= index[later]:
      print(f"  FAIL {flow}: {earlier} (#{index[earlier]}) must be declared before "
            f"{later} (#{index[later]})")
      failures += 1
  if not failures:
    print(f"  ok   {flow}: {len(present)} rungs in the intended order")

  for name, why in ((n, w) for f, (n, w) in LAST.items() if f == flow):
    if name in index and index[name] != len(config["tasks"]) - 1:
      print(f"  FAIL {flow}: {name} must be declared last -- {why}")
      failures += 1
    elif name in index:
      print(f"  ok   {flow}: {name} is last")
  return failures


def check_overlaps(config: dict) -> int:
  """Each pinned pair must be a real contest, and the winner must be the earlier one."""
  index = _idx(config)
  engine = loader.load_engine()
  compiled = engine._compile_config(config)
  by_name = {t["name"]: t for t in compiled["tasks"]}
  failures = 0
  for winner, loser, state, why in OVERLAPS:
    missing = [n for n in (winner, loser) if n not in by_name]
    if missing:
      print(f"  FAIL {winner} vs {loser}: no such rung {missing}")
      failures += 1
      continue
    active = [n for n in (winner, loser) if engine._is_task_active(by_name[n], state)]
    if len(active) < 2:
      # Not automatically wrong -- a condition may have been narrowed on purpose -- but
      # the order assertion above has stopped proving anything, so it needs a decision.
      print(f"  FAIL {winner} vs {loser}: not a real contest any more (active: "
            f"{active or 'neither'}) -- re-derive the state or drop the pair")
      failures += 1
      continue
    if index[winner] >= index[loser]:
      print(f"  FAIL {winner} must outrank {loser} -- {why}")
      failures += 1
    else:
      print(f"  ok   {winner:28} outranks {loser}")
  return failures


def check_exclusive(config: dict) -> int:
  """A pair kept apart by its conditions must still be kept apart by them."""
  engine = loader.load_engine()
  compiled = engine._compile_config(config)
  by_name = {t["name"]: t for t in compiled["tasks"]}
  failures = 0
  for a, b, state, why in EXCLUSIVE:
    active = [n for n in (a, b) if engine._is_task_active(by_name[n], state)]
    if len(active) != 1:
      print(f"  FAIL {a} / {b}: {len(active)} of the pair are active "
            f"({active or 'neither'}) -- {why}")
      failures += 1
    else:
      print(f"  ok   {a:28} excludes {b}")
  return failures


def run(app_dir: str) -> int:
  configs = {flow: harness.load_config(app_dir, flow) for flow in ORDER}
  loader.set_framework_root(harness.framework_root(app_dir))
  failures = 0

  print("the intended sequence")
  for flow, config in configs.items():
    failures += check_order(flow, config)

  print("\nthe contests that give it meaning")
  failures += check_overlaps(configs["repair"])

  print("\nand the pairs kept apart by their conditions rather than their order")
  failures += check_exclusive(configs["repair"])

  total = sum(len(ORDER[f]) for f in ORDER) + len(OVERLAPS) + len(EXCLUSIVE)
  print(f"\n{total - failures}/{total} ordering invariants hold")
  return 1 if failures else 0


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("--app-dir", default="built")
  raise SystemExit(run(parser.parse_args().app_dir))
