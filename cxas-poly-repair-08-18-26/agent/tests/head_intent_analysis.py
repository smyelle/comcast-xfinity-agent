#!/usr/bin/env python3
"""Honest three-number breakdown of the level-2 head-intent classifier.

Reuses route_check.drive (identical model/session path) to run a corpus once, then
splits the conflated "head-intent accuracy" into the numbers that matter:

  (a) L1 routing accuracy        route == expected_flow
  (b) overall head-intent acc.   leaf == expected_head_intent   (== route_check's level-2)
  (c) L2 | correct L1            leaf correct AMONG rows where L1 routed correctly
                                 (the honest within-category classifier number)

Plus a sibling-confusion breakdown (which leaf gets mistaken for which, within a
correctly-routed category) and per-leaf accuracy.

    APP_ID=<id> python tests/head_intent_analysis.py --corpus tests/head_intent_testset.json \
        --tag ts_resume --workers 8 [--save tests/_ts_resume_results.json]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import route_check  # noqa: E402  (reuse drive/resource/route_of — same path route_check runs)
from cxas_scrapi.core.sessions import Modality  # noqa: E402


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--app", default=os.environ.get("APP_ID", ""))
  ap.add_argument("--corpus", default=os.path.join(HERE, "head_intent_testset.json"))
  ap.add_argument("--tag", default="hi_analysis")
  ap.add_argument("--workers", type=int, default=8)
  ap.add_argument("--modality", choices=["text", "audio"], default="text")
  ap.add_argument("--save", default="")
  ap.add_argument("--results", default="",
                  help="skip driving; analyze a previously --save'd results file")
  a = ap.parse_args(argv)

  if a.results:
    results = json.load(open(a.results))
  else:
    if not a.app:
      ap.error("--app (or APP_ID) required")
    app = route_check.resource(a.app)
    modality = Modality.AUDIO if a.modality == "audio" else Modality.TEXT
    scenarios = json.load(open(a.corpus))["scenarios"]
    print(f"driving {len(scenarios)} scenarios ({a.modality}) against {app.split('/')[-1]}",
          flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
      futs = [ex.submit(route_check.drive, s, app, a.tag, modality) for s in scenarios]
      for i, fut in enumerate(as_completed(futs), 1):
        results.append(fut.result())
        if i % 25 == 0:
          print(f"  ... {i}/{len(scenarios)} done", flush=True)
    results.sort(key=lambda r: r["id"])
    if a.save:
      json.dump(results, open(a.save, "w"), indent=2)
      print(f"saved raw results -> {a.save}")

  n = len(results)
  errs = [r for r in results if r.get("error") or not r.get("route")]
  l1_ok = [r for r in results if r.get("route") == r["expected_flow"]]
  hi_ok = [r for r in results if r.get("head_intent") == r["expected_head_intent"]]
  # (c) among correctly-routed rows, leaf correct
  l2_given_l1 = [r for r in l1_ok if r.get("head_intent") == r["expected_head_intent"]]

  print("\n" + "=" * 68)
  print("HONEST HEAD-INTENT BREAKDOWN")
  print("=" * 68)
  print(f"  scenarios              : {n}")
  print(f"  errors / no-route      : {len(errs)}")
  print(f"  (a) L1 routing acc     : {len(l1_ok)}/{n} = {len(l1_ok)/n:.1%}")
  print(f"  (b) overall head-intent: {len(hi_ok)}/{n} = {len(hi_ok)/n:.1%}   "
        f"(== route_check level-2)")
  denom = len(l1_ok)
  print(f"  (c) L2 | correct L1    : {len(l2_given_l1)}/{denom} = "
        f"{(len(l2_given_l1)/denom if denom else 0):.1%}   <-- honest within-category")

  # ---- sibling confusion (within a correctly-routed category) ----
  conf = collections.Counter()
  for r in l1_ok:
    want, got = r["expected_head_intent"], r.get("head_intent") or "(none)"
    if want != got:
      conf[(want, got)] += 1
  print("\n-- top sibling confusions (correct L1, wrong leaf) --")
  if conf:
    for (want, got), c in conf.most_common(20):
      print(f"  {c:2d}  {want}  ->  {got}")
  else:
    print("  none")

  # ---- per-leaf accuracy (overall), worst first ----
  by_leaf_tot = collections.Counter(r["expected_head_intent"] for r in results)
  by_leaf_hit = collections.Counter(r["expected_head_intent"] for r in hi_ok)
  # also track L1-miss per leaf so we separate 'wrong category' from 'wrong sibling'
  by_leaf_l1miss = collections.Counter(
      r["expected_head_intent"] for r in results if r.get("route") != r["expected_flow"])
  rows = []
  for leaf, tot in by_leaf_tot.items():
    hit = by_leaf_hit.get(leaf, 0)
    rows.append((hit / tot, hit, tot, by_leaf_l1miss.get(leaf, 0), leaf))
  rows.sort()
  print("\n-- weakest leaves (overall head-intent acc; l1miss = routed to wrong category) --")
  for acc, hit, tot, l1miss, leaf in rows:
    if acc < 1.0:
      cat = route_check_l1(leaf)
      print(f"  {hit}/{tot} ({acc:.0%})  l1miss={l1miss}  {leaf}  [{cat}]")

  # ---- L1 confusion (which category routed where) ----
  l1conf = collections.Counter()
  for r in results:
    if r.get("route") != r["expected_flow"]:
      l1conf[(r["expected_flow"], r.get("route") or "(none)")] += 1
  print("\n-- L1 routing misses (expected_flow -> got route) --")
  for (want, got), c in l1conf.most_common(20):
    print(f"  {c:2d}  {want}  ->  {got}")
  return 0


_TAX = None


def route_check_l1(leaf: str) -> str:
  global _TAX
  if _TAX is None:
    _TAX = json.load(open(os.path.join(os.path.dirname(HERE), "head_intents.json")))["categories"]
  for cat, spec in _TAX.items():
    if leaf in spec["head_intents"]:
      return cat
  return "?"


if __name__ == "__main__":
  raise SystemExit(main())
