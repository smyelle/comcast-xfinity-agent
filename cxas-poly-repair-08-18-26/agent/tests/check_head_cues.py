#!/usr/bin/env python3
"""Assert HEAD_CUES never substring-match a golden OR held-out eval utterance, so the
deterministic L2 backstop can't preempt the model on scored inputs — the head-intent
eval stays a MODEL measure, and the cues only add production coverage."""
import json, os, re
HERE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src=open(os.path.join(HERE,"head_intents.py")).read()
block=re.search(r"HEAD_CUES\s*=\s*\{.*?\n\}", src, re.S).group(0)
# (leaf, cue) pairs — skip the sentinel (cue == a leaf key)
cues=re.findall(r'"([a-z_]+)":\s*\[([^\]]*)\]', block)
pairs=[]
for leaf,vals in cues:
    for c in re.findall(r'"([^"]+)"', vals):
        pairs.append((leaf,c.lower()))
utts=[]
for f in ("routing_corpus.json","head_intent_heldout.json","routing_heldout.json"):
    p=os.path.join(HERE,"tests",f)
    if os.path.exists(p):
        utts+=[t.lower() for s in json.load(open(p))["scenarios"] for t in s["user_utterances"]]
bad=[(leaf,c,u) for leaf,c in pairs for u in utts if c in u]
print(f"{len(pairs)} cue phrases checked against {len(utts)} eval utterances")
if bad:
    print("COLLISIONS (cue substring-matches an eval utterance):")
    for leaf,c,u in bad: print(f"  {leaf}: {c!r} in {u!r}")
    raise SystemExit(1)
print("OK — no HEAD_CUES phrase matches any golden/held-out utterance (held-out stays a model measure)")
