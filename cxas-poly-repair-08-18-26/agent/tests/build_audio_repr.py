#!/usr/bin/env python3
"""A small, representative slice of the honest L2 test set for an AUDIO run.

Stratified + deterministic: the first N scenarios (by id) from EACH of the 15 defer
categories, so every category — the fixed catch-all and the strong ones alike — is
exercised through the TTS->STT path. Small enough to run through audio quickly, broad
enough to show whether the L1-routing gains survive speech.

    python tests/build_audio_repr.py            # -> tests/audio_repr.json  (N=3)
    N=2 python tests/build_audio_repr.py
"""
import collections
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "head_intent_testset.json")
OUT = os.path.join(HERE, "audio_repr.json")
N = int(os.environ.get("N", "3"))


def main() -> int:
  scen = json.load(open(SRC))["scenarios"]
  by_cat = collections.defaultdict(list)
  for s in scen:
    by_cat[s["expected_flow"]].append(s)
  pick = []
  for cat in sorted(by_cat):
    pick += sorted(by_cat[cat], key=lambda s: s["id"])[:N]
  pick.sort(key=lambda s: s["id"])
  json.dump({"source": f"representative audio slice (first {N}/category of "
                        "head_intent_testset.json)", "scenarios": pick},
            open(OUT, "w"), indent=2)
  print(f"wrote {len(pick)} scenarios across {len(by_cat)} categories -> "
        f"{os.path.relpath(OUT, HERE)}")
  for cat in sorted(by_cat):
    print(f"  {cat:30s} {sum(1 for s in pick if s['expected_flow'] == cat)}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
