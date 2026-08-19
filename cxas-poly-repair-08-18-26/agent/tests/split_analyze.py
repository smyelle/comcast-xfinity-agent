#!/usr/bin/env python3
"""Bucket a saved route_check/head_intent_analysis results file by the train/held-out
split and print the three honest numbers (L1, overall, L2|correct-L1) per split, plus
the L1 miss confusion for TRAIN only (held-out misses stay unseen during the climb).

    python tests/split_analyze.py --results tests/_ts_resume_results.json
    python tests/split_analyze.py --results <file> --show-held   # only for the FINAL report
"""
import argparse
import collections
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def ids(path):
  return {s["id"] for s in json.load(open(path))["scenarios"]}


def stats(rows):
  n = len(rows)
  l1 = [r for r in rows if r.get("route") == r["expected_flow"]]
  overall = [r for r in rows if r.get("head_intent") == r["expected_head_intent"]]
  l2gl1 = [r for r in l1 if r.get("head_intent") == r["expected_head_intent"]]
  return n, len(l1), len(overall), len(l2gl1)


def l1_conf(rows):
  c = collections.Counter()
  for r in rows:
    if r.get("route") != r["expected_flow"]:
      c[(r["expected_flow"], r.get("route") or "(none)")] += 1
  return c


def main(argv=None) -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--results", required=True)
  ap.add_argument("--train", default=os.path.join(HERE, "head_intent_train.json"))
  ap.add_argument("--held", default=os.path.join(HERE, "head_intent_heldout_big.json"))
  ap.add_argument("--show-held", action="store_true",
                  help="print held-out miss detail (use ONLY for the final report)")
  a = ap.parse_args(argv)

  results = json.load(open(a.results))
  train_ids, held_ids = ids(a.train), ids(a.held)
  train = [r for r in results if r["id"] in train_ids]
  held = [r for r in results if r["id"] in held_ids]

  for name, rows in (("TRAIN", train), ("HELD-OUT (frozen)", held)):
    n, l1, ov, l2 = stats(rows)
    print(f"== {name}: {n} rows ==")
    print(f"  (a) L1 routing      : {l1}/{n} = {l1/n:.1%}")
    print(f"  (b) overall intent  : {ov}/{n} = {ov/n:.1%}")
    print(f"  (c) L2 | correct L1 : {l2}/{l1} = {(l2/l1 if l1 else 0):.1%}")
    print()

  print("== TRAIN L1 misses (expected -> got) ==")
  for (w, g), c in l1_conf(train).most_common(40):
    print(f"  {c:2d}  {w}  ->  {g}")

  if a.show_held:
    print("\n== HELD-OUT L1 misses (expected -> got) [FINAL REPORT ONLY] ==")
    for (w, g), c in l1_conf(held).most_common(40):
      print(f"  {c:2d}  {w}  ->  {g}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
