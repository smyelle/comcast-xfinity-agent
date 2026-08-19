"""The transcripts in the docs must be what the engine actually says.

A hand-written sample conversation is the first thing a reader trusts and the first
thing to rot: the feature changes, the page does not, and the docs quietly describe an
agent that no longer exists. This drives the documented example through the real engine
and compares the agent lines against the page, so the two cannot diverge silently.

Scoped to pages whose transcript is fully deterministic — engine-spoken text with no
model in the loop. A page whose sample depends on what an LLM chooses to say is not
checkable this way and is deliberately not listed.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import types

import pytest

from flows.engine import loader as fb


_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLES = os.path.join(os.path.dirname(_HERE), "examples")
_DOCS = os.path.abspath(os.path.join(
    _HERE, "..", "..", "..", "service", "web", "src", "products",
    "flows-docs", "content"))


def _load_example(name: str) -> types.ModuleType:
  path = os.path.join(_EXAMPLES, f"{name}.py")
  spec = importlib.util.spec_from_file_location(f"_doc_example_{name}", path)
  assert spec and spec.loader, path
  mod = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = mod
  spec.loader.exec_module(mod)
  return mod


def _agent_lines(markdown: str) -> list[list[str]]:
  """The agent lines of every ```transcript fence, one list per fence.

  Both glyphs are agent turns: `<` is a directive the model words itself, `<<` is
  a line the engine preempts and speaks verbatim. Stripping only one leading `<`
  left the marker on every `<<` line and compared it against engine text that
  never contains one, so a page written in correct house style failed as "drifted".
  What is compared is the spoken words, so the reader-facing annotations come off
  too.
  """
  fences = re.findall(r"```transcript\n(.*?)```", markdown, re.S)
  out = []
  for fence in fences:
    lines = []
    for raw in fence.splitlines():
      if not raw.startswith("<"):
        continue
      said = raw[2:] if raw.startswith("<<") else raw[1:]
      # "  <- note" (old style) and the trailing "  [config_key]" chip are for the
      # reader, not the caller.
      said = re.sub(r"\s+<-\s.*$", "", said)
      said = re.sub(r"\s*\[[^\]]*\]\s*$", "", said)
      lines.append(said.strip())
    out.append(lines)
  return out


def _caller_lines(markdown: str) -> list[list[str]]:
  fences = re.findall(r"```transcript\n(.*?)```", markdown, re.S)
  return [[l[1:].strip() for l in f.splitlines() if l.startswith(">")]
          for f in fences]


def _spoken(action) -> str:
  parts = [action.get("message") or ""]
  for part in action.get("response") or []:
    if isinstance(part, dict) and part.get("type") == "text":
      parts.append(part.get("text") or "")
  return " ".join(p for p in parts if p).strip()


def _drive(config, caller_turns: list[str], seed: dict,
           prelude: tuple[str, ...] = (),
           tool_results: dict | None = None) -> list[str]:
  """Everything the agent says on each caller turn.

  A caller turn is NOT one engine pass. When a task fires, the platform runs the tool
  and re-invokes the engine with the result inside the SAME turn, and the line the
  caller hears is everything said across those passes. Driving one pass per turn would
  capture the holding line and drop the answer that follows it — the transcript would
  be real but truncated, which is worse than wrong because it looks right.
  """
  engine = fb.load_engine()
  results = tool_results or {}
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = config.get("config_id") or "j"
    sm["filled"][gate] = sm[gate]
  # Slots the page treats as already handled. There is no model in this harness, so a
  # value the LLM would have captured has to be seeded — otherwise the transcript would
  # have to open with a question the page is not about.
  sm["filled"].update(seed)

  def settle(text, n):
    heard = []
    for pass_n in range(8):
      action = engine.slot_filling_engine({
          "raw_config": config, "sm": sm,
          "last_user_text": text if pass_n == 0 else "",
          "scanned_user_text": text if pass_n == 0 else "",
          "is_inactivity": False, "event_data": {},
          "config_id": "j", "n_user_turns": n,
      })["action"]
      said = _spoken(action)
      if said:
        heard.append(said)
      fired = (action.get("function_call") or {}).get("name") or ""
      if not fired:
        break
      if fired not in results:
        heard.append(f"!! no tool result for {fired}")
        break
      sm.update(fb.run_intake(fired, results[fired], sm)["sm"])
    return " ".join(heard)

  # Caller turns that happen BEFORE the fence starts. A page may reasonably open
  # mid-conversation; driving the lead-in without showing it keeps the fence honest
  # rather than padding it with an exchange the page is not about. It also matters for
  # correctness: the ROUTING turn fills every cue-matched slot regardless of
  # `multi_fill`, so a fence that began on turn 1 would pass whether it worked or not.
  turn = 0
  for turn, text in enumerate(prelude, start=1):
    settle(text, turn)
  return [settle(text, n)
          for n, text in enumerate(caller_turns, start=turn + 1)]


@pytest.mark.parametrize("page,example,fence,seed,prelude,tools", [
    # The SECOND fence on the ask-ladder page is the ladder itself; the first is the
    # "before" picture it exists to fix, which by definition the engine no longer does.
    ("examples/ask-ladder", "ask_ladder", 1, {"topic": "statement"}, (), {}),
    # multi-fill: the two tone branches, then the ambiguous case that fills neither.
    # Same shape as ask-ladder above — fence 0 is the "before" picture, so the three
    # checkable fences start at 1.
    ("examples/multi-fill", "multi_fill", 1, {"account": "123456"},
     ("I need help with a charge",), {}),
    ("examples/multi-fill", "multi_fill", 2, {"account": "123456"},
     ("I need help with a charge",), {}),
    ("examples/multi-fill", "multi_fill", 3, {"account": "123456"},
     ("I need help with a charge",), {}),
])
def test_documented_transcript_matches_the_engine(
    page, example, fence, seed, prelude, tools):
  with open(os.path.join(_DOCS, f"{page}.md"), encoding="utf-8") as fh:
    body = fh.read()
  wanted = _agent_lines(body)[fence]
  caller = _caller_lines(body)[fence]
  assert wanted and caller, f"{page}: no transcript found in fence {fence}"

  mod = _load_example(example)
  spoken = _drive(mod.app.root_flow.to_config(), caller, seed, prelude, tools)

  assert spoken == wanted, (
      f"{page}.md fence {fence} has drifted from the engine.\n"
      f"  documented: {wanted}\n  actual:     {spoken}")
