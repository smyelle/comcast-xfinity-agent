"""Build drive_app's eval oracle from the comcast evaluations/ goldens.

Each evaluations/<name>/<name>.json has golden.turns[].steps[], where a step is one of:
  {"userInput": {"variables": {...}}}   -> seed
  {"userInput": {"text": "..."}}        -> a user turn
  {"expectation": {"agentResponse": {"chunks": [{"text": ...}]}}}  -> expected agent text

We flatten to the oracle scenario shape drive_app reads:
  {id, kind, seeded_variables, user_utterances, expected_agent_text}
expected_agent_text = the FIRST agent expectation (drive_app compares the first agent turn).
"""
import glob
import json
import os

EVAL_DIR = os.path.expanduser("~/cxas-comcast/evaluations")
OUT = os.environ.get("ORACLE_OUT", "/Users/ygupta/.claude/jobs/ae81a2a6/tmp/eval_oracle.json")


def chunks_text(resp):
  return " ".join(c.get("text", "") for c in (resp.get("chunks") or []) if c.get("text"))


scenarios = []
for f in sorted(glob.glob(os.path.join(EVAL_DIR, "*", "*.json"))):
  d = json.load(open(f))
  golden = d.get("golden") or {}
  sid = os.path.basename(os.path.dirname(f))  # unique per eval; tags can collide (mvp)
  seeded, utts, expected = {}, [], []
  for turn in golden.get("turns") or []:
    for step in turn.get("steps") or []:
      ui = step.get("userInput") or {}
      if "variables" in ui:
        seeded.update(ui["variables"] or {})
      if "text" in ui:
        utts.append(ui["text"])
      exp = (step.get("expectation") or {}).get("agentResponse")
      if exp:
        expected.append(chunks_text(exp))
  if not utts:
    continue
  scenarios.append({
      "id": sid,
      "kind": "golden",
      "seeded_variables": seeded,
      "user_utterances": utts,
      "expected_agent_text": expected[0] if expected else "",
      "all_expected": expected,
  })

os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump({"scenarios": scenarios}, open(OUT, "w"), indent=1)
print(f"wrote {len(scenarios)} scenarios -> {OUT}")
print("sample ids:", [s["id"] for s in scenarios[:6]])
