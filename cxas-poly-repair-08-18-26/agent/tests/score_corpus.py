"""Score a drive_app.py corpus run against the goldens; flag router misroutes.

Companion to `drive_app.py --kind golden` + `build_oracle.py`: reads the per-scenario
JSONs drive_app wrote to <outdir> and reports how many first agent turns match their
golden (whitespace/punctuation-normalized, with a token-overlap fallback), plus any
MISROUTE — a turn that landed on the handoff/wrong flow where a real verdict was expected.

    python tests/score_corpus.py [<outdir>]      # default ./baseline
"""
import glob
import json
import os
import re
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "baseline")
HANDOFF = "let me get you to the right place"


def _norm(s):
  return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).split()


def _match(pred, gold):
  p, g = _norm(pred), _norm(gold)
  if not g:
    return None  # no golden expectation to compare
  ps, gs = " ".join(p), " ".join(g)
  if gs in ps or ps in gs:
    return True
  common = sum(min(p.count(w), g.count(w)) for w in set(g))
  return common / max(1, len(g)) >= 0.8


def run(out_dir):
  rows = []
  for f in sorted(glob.glob(os.path.join(out_dir, "*.json"))):
    d = json.load(open(f))
    gold = d.get("golden_agent_text") or ""
    turns = d.get("turns") or []
    pred = (turns[0].get("agent_text") if turns else "") or ""
    m = _match(pred, gold)
    misroute = (HANDOFF in pred.lower()) and (HANDOFF not in gold.lower())
    rows.append((d.get("scenario"), m, misroute, bool(d.get("error")), pred, gold))

  ok = sum(1 for r in rows if r[1] is True)
  nogold = sum(1 for r in rows if r[1] is None)
  misroutes = [r for r in rows if r[2]]
  errs = [r for r in rows if r[3]]
  scored = len(rows) - nogold
  print(f"MATCH {ok}/{scored} scored ({nogold} had no golden text); "
        f"misroutes={len(misroutes)} errors={len(errs)}\n")
  if misroutes:
    print("== MISROUTES (a golden sent to handoff/wrong flow) ==")
    for sid, _m, _mis, _e, pred, gold in misroutes:
      print(f"  {sid}: pred={pred[:70]!r} gold={gold[:60]!r}")
  print("== MISMATCHES ==")
  for sid, m, mis, e, pred, gold in rows:
    if m is False and not mis:
      print(f"  {'ERR ' if e else '    '}{sid}: pred={pred[:60]!r} gold={gold[:55]!r}")
  return 1 if (misroutes or errs) else 0


if __name__ == "__main__":
  raise SystemExit(run(OUT))
