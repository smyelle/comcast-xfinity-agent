#!/usr/bin/env python3
"""Build the held-out routing corpus: novel phrasings per destination, disjoint from BOTH
the ROUTE_CUES and the golden corpus, so route_check.py measures model generalization
rather than cue-matching or memorization of the golden utterances.

    python tests/build_heldout.py            # writes tests/routing_heldout.json

The disjointness is asserted at build time: no held-out utterance may contain any cue as
a substring, and none may equal a golden utterance.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # flows-sdk
OUT = os.path.join(HERE, "tests", "routing_heldout.json")

# A benign, all-clear account so the sweep resolves cleanly and never muddies the route.
SEED = {
    "accountNumber": "1234567890", "account_id": "1234567890",
    "mock_config_string": "account_status=active;gateway_status=online;"
                          "network_status=healthy;outage_status=none",
}

# (expected_flow, novel utterance). Two per destination; none is a golden string.
HELDOUT = [
    ("repair", "the internet keeps dropping every few minutes"),
    ("repair", "nothing will load on any of my devices"),
    ("reboot", "can you give my gateway a restart"),
    ("reboot", "I'd like to power cycle the box myself"),
    ("human", "get me a real person please"),
    ("human", "I need to speak with someone in support"),
    ("billing", "why did my charges go up this month"),
    ("billing", "I see a fee I don't recognize"),
    ("payments", "I'd like to put a card on file"),
    ("payments", "can I schedule a payment for next week"),
    ("sales", "I'm interested in a faster internet tier"),
    ("sales", "I'd like to add a premium sports package"),
    # TV and Xfinity Home are no longer separate categories — a broken box, remote or
    # camera is the same intent as a broken connection and `repair` answers it. Kept in
    # the corpus, relabelled, so the router is still measured on them.
    ("repair", "the picture on my television is frozen"),
    ("repair", "the on-screen guide won't come up"),
    ("technical_phone", "I can't hear anything when I pick up the phone"),
    ("technical_phone", "my voice mail box is completely full"),
    ("xfinity_mobile", "my cell service through xfinity is down"),
    ("xfinity_mobile", "I want to add a new phone line to my plan"),
    ("repair", "my doorbell camera stopped recording"),
    ("repair", "the motion sensor keeps going off"),
    ("appointments", "I need to push back the technician visit"),
    ("appointments", "when is my installation scheduled for"),
    ("activations", "I just received my new box and need to set it up"),
    ("activations", "how do I turn on the service I ordered"),
    ("service_center", "where can I drop off my old equipment"),
    ("service_center", "is there an xfinity location near me"),
    ("accessibility", "I need subtitles enabled for the hearing impaired"),
    ("accessibility", "do you support relay services for the deaf"),
    ("equipment_swap", "I'd like to trade in my old gateway for a newer one"),
    ("equipment_swap", "can I get a different model of router"),
    ("phone_security", "someone transferred my number to another carrier without asking"),
    ("phone_security", "I think my phone number was hijacked"),
    ("transfers", "please add my daughter to the account"),
    ("transfers", "what's the status of my recent order"),
    ("disambiguation_main_menu", "change the name of my wifi"),
    ("disambiguation_main_menu", "put my account on hold while I'm away for the winter"),
]


def _cues():
    src = open(os.path.join(HERE, "app.py")).read()
    return eval(re.search(r"ROUTE_CUES\s*=\s*(\{.*?\n\})", src, re.S).group(1))


def main() -> int:
    cues = [c.lower() for cl in _cues().values() for c in cl]
    golden = {t.lower()
              for s in json.load(open(os.path.join(HERE, "tests", "routing_corpus.json")))["scenarios"]
              for t in s["user_utterances"]}
    collisions = []
    for flow, u in HELDOUT:
        ul = u.lower()
        if any(c in ul for c in cues):
            collisions.append(("cue", u))
        if ul in golden:
            collisions.append(("golden", u))
    if collisions:
        raise SystemExit(f"held-out utterances collide with cues/golden: {collisions}")

    scenarios = [{
        "id": f"heldout_{flow}_{i}", "kind": "heldout", "seeded_variables": dict(SEED),
        "user_utterances": [u], "expected_flow": flow, "acceptable_flows": [],
        "tag": "heldout",
    } for i, (flow, u) in enumerate(HELDOUT)]
    json.dump({"source": "held-out paraphrases (generalization test)",
               "scenarios": scenarios}, open(OUT, "w"), indent=2)
    print(f"wrote {len(scenarios)} held-out scenarios (disjoint from cues + golden) -> "
          f"{os.path.relpath(OUT, HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
