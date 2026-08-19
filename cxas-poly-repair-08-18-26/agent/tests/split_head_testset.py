#!/usr/bin/env python3
"""Split the honest L2 test set into TRAIN + frozen HELD-OUT for the L1 hill-climb.

The climb edits the router's ROUTE_CATALOGUE descriptions to fix L1 routing. To keep
the final accuracy number honest, we only ever INSPECT train misses when editing, and
report the held-out number from a slice we never looked at while editing.

Deterministic + stratified by expected_flow (so every category — including the big
`disambiguation_main_menu` catch-all — is represented in both splits): within each
category, sort by id and send every 3rd scenario to held-out, the rest to train.

    python tests/split_head_testset.py
      -> tests/head_intent_train.json   (~2/3)
      -> tests/head_intent_heldout_big.json (~1/3, FROZEN — do not inspect misses)
"""
import collections
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "head_intent_testset.json")
TRAIN = os.path.join(HERE, "head_intent_train.json")
HELD = os.path.join(HERE, "head_intent_heldout_big.json")


def main() -> int:
  scen = json.load(open(SRC))["scenarios"]
  by_cat = collections.defaultdict(list)
  for s in scen:
    by_cat[s["expected_flow"]].append(s)
  train, held = [], []
  for cat in sorted(by_cat):
    rows = sorted(by_cat[cat], key=lambda s: s["id"])
    for i, s in enumerate(rows):
      (held if i % 3 == 2 else train).append(s)
  train.sort(key=lambda s: s["id"])
  held.sort(key=lambda s: s["id"])
  json.dump({"source": "split of head_intent_testset.json (train)", "scenarios": train},
            open(TRAIN, "w"), indent=2)
  json.dump({"source": "split of head_intent_testset.json (FROZEN held-out)",
             "scenarios": held}, open(HELD, "w"), indent=2)
  print(f"train {len(train)} | held-out {len(held)} | total {len(scen)}")
  for cat in sorted(by_cat):
    t = sum(1 for s in train if s["expected_flow"] == cat)
    h = sum(1 for s in held if s["expected_flow"] == cat)
    print(f"  {cat:30s} train {t:3d}  held {h:3d}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
