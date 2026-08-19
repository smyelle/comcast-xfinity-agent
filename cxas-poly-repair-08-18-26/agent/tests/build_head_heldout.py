#!/usr/bin/env python3
"""Held-out head-intent set: NOVEL phrasings per covered leaf, asserted disjoint from the
curated cues (head_intents.CURATED_CUES) AND the golden utterances — so route_check
measures whether level-2 head-intent detection GENERALIZES rather than just matching the
cues we hand-wrote. (The golden-eval head-intent number is in-sample; this is the honest
one.)

    python tests/build_head_heldout.py     # -> tests/head_intent_heldout.json
"""
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # flows-sdk
OUT = os.path.join(HERE, "tests", "head_intent_heldout.json")
SEED = {
    "accountNumber": "1234567890", "account_id": "1234567890",
    "mock_config_string": "account_status=active;gateway_status=online;"
                          "network_status=healthy;outage_status=none",
}
# (expected_flow, expected_head_intent, novel utterance) — novel phrasings the cues do
# NOT cover, incl. several leaves with no curated cue at all (pure generalization test).
HELD = [
    ("billing", "billing_toohigh", "my statement looks really inflated this cycle"),
    ("billing", "billing_toohigh", "this charge is outrageous this month"),
    ("billing", "billing_discuss", "walk me through these line items please"),
    ("billing", "billing_refund_inquiry", "I want my money back for that erroneous charge"),
    ("billing", "payments_balance_due", "am I behind on anything on the account"),
    ("payments", "payments_make_payment", "I'd like to settle my account today"),
    ("payments", "payments_make_payment", "let me square up what's on the account"),
    ("payments", "payments_manage_autopay", "stop the automatic withdrawals from my bank"),
    ("technical_television", "tv_troubleshoot_channels", "a bunch of my stations vanished"),
    ("technical_television", "tv_troubleshoot_dvr", "my recordings won't play back"),
    ("technical_phone", "voice_troubleshoot", "my home phone has no dial tone at all"),
    ("sales", "plan_upgrade", "bump me to a bigger package"),
    ("sales", "plan_upgrade", "step up to a quicker connection"),
    ("sales", "account_transfer_service", "we're changing houses soon and need service there"),
    ("appointments", "appointment_change_schedule", "can we move my technician visit to Friday"),
    ("disambiguation_main_menu", "plan_manage_temporary_disconnect",
     "shut it down while I'm traveling for a few months"),
]


def main() -> int:
  src = open(os.path.join(HERE, "head_intents.py")).read()
  block = re.search(r"CURATED_CUES\s*=\s*\{.*?\n\}", src, re.S).group(0)
  cues = [m.lower() for m in re.findall(r'"([^"]+)"', block)]
  golden = {t.lower()
            for s in json.load(open(os.path.join(HERE, "tests", "routing_corpus.json")))["scenarios"]
            for t in s["user_utterances"]}
  bad = []
  for _, _, u in HELD:
    ul = u.lower()
    bad += [(u, c) for c in cues if c in ul]
    if ul in golden:
      bad.append((u, "GOLDEN"))
  if bad:
    raise SystemExit(f"held-out utterances collide with cues/golden: {bad}")
  scen = [{"id": f"hh_{hl}_{i}", "kind": "heldout", "seeded_variables": dict(SEED),
           "user_utterances": [u], "expected_flow": fl, "acceptable_flows": [],
           "expected_head_intent": hl, "tag": "heldout"}
          for i, (fl, hl, u) in enumerate(HELD)]
  json.dump({"source": "held-out head-intent paraphrases", "scenarios": scen},
            open(OUT, "w"), indent=2)
  print(f"wrote {len(scen)} held-out head-intent scenarios (disjoint from cues + golden)")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
