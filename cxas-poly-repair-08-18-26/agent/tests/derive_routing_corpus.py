#!/usr/bin/env python3
"""Derive the routing eval corpus from the GECX golden steering agent's own evals.

The golden agent (vendored under tests/golden_evals/, exported from the latest
`gecx-ivr-golden-dev`) is the SOURCE OF TRUTH for what each call should route to.
Rather than hand-label expectations, this reads each golden eval's own annotations and
emits tests/routing_corpus.json for route_check.py:

  * user turns          <- every `userInput.text` in order
  * golden intent(s)    <- every `steering_handle_next_step` `intent` arg, in order
  * terminal handoff    <- an `agentTransfer.targetAgent == "agent_handoff"` (golden's
                           live-agent queue: cancellation, retention, an explicit agent
                           request) means the caller should end with a person
  * knowledge base      <- `action=general_product_or_services_question` / `search_web`
  * language            <- `agent_language` in the seeded variables

Our flows are NAMED for golden's intent categories, so the mapping is mostly identity;
the only bridges are technical_internet->diagnostics (the slice we resolve), agent/rage->
human, and the global behaviours->diagnostics. Regenerate whenever the golden export
changes:

    python tests/derive_routing_corpus.py            # reads tests/golden_evals/
    GOLDEN_EVALS=/path/to/gecx-ivr-golden-dev/evaluations python tests/derive_routing_corpus.py
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.environ.get("GOLDEN_EVALS", os.path.join(HERE, "golden_evals"))
OUT = os.path.join(HERE, "routing_corpus.json")

# golden intent category -> our router flow key. Identity for every category we defer;
# the three bridges are the flows we HANDLE (internet) or fold in (agent/rage->human,
# global behaviours->diagnostics). `downgrade`/`service` are the extra intent tokens the
# golden evals emit for cancellation and equipment-return.
CAT2FLOW = {
    "billing": "billing", "payments": "payments", "sales": "sales",
    "technical_television": "technical_television", "technical_phone": "technical_phone",
    "xfinity_mobile": "xfinity_mobile", "technical_xfinity_home": "technical_xfinity_home",
    "appointments": "appointments", "activations": "activations",
    "service_center": "service_center", "accessibility": "accessibility",
    "equipment_swap": "equipment_swap", "phone_security": "phone_security",
    "transfers": "transfers", "disambiguation_main_menu": "disambiguation_main_menu",
    "technical_internet": "repair",
    "agent": "human", "rage": "human", "downgrade": "human", "service": "service_center",
    "unintelligible": "repair", "unrelated": "repair", "incidental": "repair",
}
GLOBALS = {"unintelligible", "unrelated", "incidental"}

# Seed a clean account so the diagnostic sweep resolves benignly and does not muddy the
# routing signal — routing is on the utterance's meaning, not the sweep result.
SEED = {
    "accountNumber": "1234567890", "account_id": "1234567890",
    "mock_config_string": "account_status=active;gateway_status=online;"
                          "network_status=healthy;outage_status=none",
}


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _unwrap(v):
    """A golden arg may be a plain value or a match-spec dict; return the value."""
    if isinstance(v, dict):
        return v.get("$originalValue") or v.get("$matchValue") or ""
    return v


def signals(data: dict) -> dict:
    """Pull the source-of-truth routing signals out of one golden eval."""
    utterances, intents, actions, head_intents = [], [], [], []
    handoff, search_web, lang = False, False, "en"
    for node in _walk(data):
        ui = node.get("userInput")
        if isinstance(ui, dict) and isinstance(ui.get("text"), str) and ui["text"].strip():
            utterances.append(ui["text"].strip())
        if node.get("tool") == "steering_handle_next_step" and isinstance(node.get("args"), dict):
            a = node["args"]
            if a.get("intent"):
                intents.append(a["intent"])
            if a.get("action"):
                actions.append(a["action"])
            hi = _unwrap(a.get("head_intent"))
            if hi:
                head_intents.append(hi)
        at = node.get("agentTransfer")
        if isinstance(at, dict) and at.get("targetAgent") == "agent_handoff":
            handoff = True
        uv = node.get("updatedVariables")
        if isinstance(uv, dict) and uv.get("agent_language"):
            lang = uv["agent_language"]
    if "general_product_or_services_question" in actions:
        search_web = True
    return {"utterances": utterances, "intents": intents, "actions": actions,
            "head_intents": head_intents, "handoff": handoff,
            "search_web": search_web, "lang": lang}


def primary_category(intents):
    """The first substantive intent (skip a leading 'agent'/global escalation token)."""
    for i in intents:
        if i not in GLOBALS and i != "agent":
            return i
    return intents[0] if intents else None


def derive(sig: dict):
    """(expected_flow, acceptable_flows, tag) for one eval, from its golden signals."""
    intents, actions = sig["intents"], sig["actions"]
    primary = primary_category(intents)

    if not intents and not actions and not sig["search_web"]:
        tag = "proxy"  # transferred-in scenario: the golden eval does no steering routing
    elif sig["lang"] and sig["lang"] != "en":
        tag = "bilingual"
    elif sig["search_web"]:
        tag = "kb_gap"
    elif any(i in GLOBALS for i in intents):
        tag = "global_behavior"
    elif "service_disambiguate" in actions:
        tag = "disambiguation"  # golden asks which LOB; our router commits on turn 1
    else:
        tag = "clean"

    acceptable = []
    if sig["handoff"]:
        expected = "human"
        if primary and CAT2FLOW.get(primary) not in (None, "human"):
            acceptable.append(CAT2FLOW[primary])
    elif sig["search_web"]:
        expected = CAT2FLOW.get(primary, "disambiguation_main_menu")
        acceptable += ["disambiguation_main_menu", "human"]
    elif not primary or primary in GLOBALS:
        expected = "repair"
    else:
        expected = CAT2FLOW.get(primary, "repair")
    # secondary intents are acceptable alternates (e.g. payments+billing utterances)
    for i in intents[1:]:
        f = CAT2FLOW.get(i)
        if f and f != expected and f not in acceptable:
            acceptable.append(f)
    return expected, acceptable, tag


def main() -> int:
    scenarios, skipped = [], []
    for name in sorted(os.listdir(GOLDEN)):
        path = os.path.join(GOLDEN, name, name + ".json")
        if not os.path.isfile(path):
            continue
        sig = signals(json.load(open(path)))
        if not sig["utterances"]:
            skipped.append(name)
            continue
        expected, acceptable, tag = derive(sig)
        scenarios.append({
            "id": name,
            "kind": "golden",
            "seeded_variables": dict(SEED),
            "user_utterances": sig["utterances"],
            "expected_flow": expected,
            "acceptable_flows": acceptable,
            "tag": tag,
            "golden_intents": sig["intents"],
            "golden_head_intents": sig["head_intents"],
            # The leaf golden picked (first head_intent), for level-2 scoring. Empty when
            # the eval had no head-intent pass (agent/global/proxy/kb).
            "expected_head_intent": sig["head_intents"][0] if sig["head_intents"] else "",
            "golden_handoff": sig["handoff"],
        })
    json.dump({"source": "tests/golden_evals (gecx-ivr-golden-dev)",
               "scenarios": scenarios}, open(OUT, "w"), indent=2)
    print(f"derived {len(scenarios)} scenarios -> {os.path.relpath(OUT, HERE)}")
    if skipped:
        print("skipped (no utterance):", skipped)
    for s in scenarios:
        alt = (" | accept " + ",".join(s["acceptable_flows"])) if s["acceptable_flows"] else ""
        print(f"  {s['id']:38s} -> {s['expected_flow']:24s} [{s['tag']}]{alt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
