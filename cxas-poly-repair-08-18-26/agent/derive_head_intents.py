#!/usr/bin/env python3
"""Port the GECX golden steering agent's head-intent taxonomy into head_intents.json.

The golden agent picks a leaf `head_intent` within each `intent` category via its
`get_agent_instructions_for_selecting_head_intent` tool, whose module-level `INTENT_CONFIG`
holds the authoritative allowlists (leaf -> description), target flows, and per-category
disambiguation questions. We exec that module (its `context`/`tools` refs are all INSIDE
the function body, so module load is side-effect-free) and emit the taxonomy as JSON — the
single source of truth for level-2 classification, kept in step with the golden export.

    python flows-sdk/derive_head_intents.py            # reads the golden export in ~/Downloads
    GOLDEN_HEAD_TOOL=/path/to/python_code.py python flows-sdk/derive_head_intents.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.expanduser(
    "~/Downloads/gecx-ivr-golden-dev/tools/"
    "get_agent_instructions_for_selecting_head_intent/python_function/python_code.py")
GOLDEN = os.environ.get("GOLDEN_HEAD_TOOL", DEFAULT)
OUT = os.path.join(HERE, "head_intents.json")


def main() -> int:
  ns: dict = {}
  exec(compile(open(GOLDEN).read(), GOLDEN, "exec"), ns)  # noqa: S102 - trusted golden export
  cfg = ns["INTENT_CONFIG"]
  out = {}
  for cat, c in cfg.items():
    out[cat] = {
        "target_flow": c.get("target_flow"),
        "lob": c.get("lob"),
        "head_intents": dict(c["head_intents"]),
        "disambiguation_question": c.get("disambiguation_question"),
    }
  with open(OUT, "w") as fh:
    json.dump({"source": "gecx-ivr-golden-dev get_agent_instructions_for_selecting_head_intent",
               "categories": out}, fh, indent=2)
  total = sum(len(v["head_intents"]) for v in out.values())
  print(f"wrote {len(out)} categories / {total} head intents -> {os.path.relpath(OUT, HERE)}")
  for cat, v in out.items():
    print(f"  {cat:26s} {len(v['head_intents']):2d} leaves -> {v['target_flow']}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
