"""How long the caller waits, and whether the model is on the critical path.

The target is a median of about a second from the caller finishing their utterance to the
agent starting to speak. On a voice call that is time-to-FIRST-AUDIO, not time-to-whole-
turn: CES streams a turn's parts, so a turn whose first part is engine-emitted starts
speaking while the model is still working, and one whose first part is model-generated
cannot start until the model has produced it.

A text drive cannot see the audio, so this measures the two things it honestly can:

  wall clock per turn   the whole turn, which is an UPPER bound on time-to-first-audio
  engine-opened         whether the first sentence is a literal the engine emits --
                        a filler, a `then_say`, an ask -- rather than model prose

`engine-opened` is the one that matters for the median, and it is the reason a filler
exists at all. A turn that is engine-opened starts speaking in roughly the engine's own
round trip regardless of how long the model then takes; a model-opened turn pays the whole
wall clock before the caller hears anything.

Literals are collected from the modules that hold the spoken copy, so this stays true as
the copy changes and never needs a second list kept in step with it.

  uv run python tests/latency_check.py            # or APP_ID=<id> to point elsewhere
"""

from __future__ import annotations

import ast
import glob
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuj_drive import APP, ChatSession  # noqa: E402
from try_journeys import JOURNEYS  # noqa: E402

# Globbed, not listed. This is an AST scan of source FILES, and the loop that reads them
# skips anything missing -- so when the spoken copy moved into `journeys/`, a hardcoded
# list would have kept finding `scripts.py`, kept finding almost no strings in it, and
# quietly under-reported engine-opened turns as model-opened. That is the exact direction
# this instrument's own docstring warns about.
_COPY_MODULES = (("scripts.py", "clarify.py", "source_tools.py", "hooks.py")
                 + tuple(sorted(glob.glob("journeys/*.py")))
                 + tuple(sorted(glob.glob("journeys/common/*.py"))))


def _engine_first_sentences() -> list:
  """Every first sentence the engine can speak, as PATTERNS, read from the copy modules.

  Patterns rather than literals because a script may interpolate: `SAY_AREA_OUTAGE` opens
  with `{outage_message}`, and comparing the rendered turn against the raw literal never
  matches. Read as literals this under-reported engine-opened turns as model-opened --
  65% against a true 100% -- which is the wrong direction for a measurement whose whole
  job is to say whether the model is on the critical path.
  """
  firsts: list = []
  here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  for mod in _COPY_MODULES:
    path = os.path.join(here, mod)
    if not os.path.exists(path):
      continue
    for node in ast.walk(ast.parse(open(path).read())):
      if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value.strip()
        if len(text) > 3:
          first = re.split(r"(?<=[.!?])\s+", text)[0].strip()
          firsts.append(re.compile(
              "^" + re.sub(r"\\\{[^}]*\\\}", ".+", re.escape(first)) + "$"))
  return firsts


def _first_sentence(text: str) -> str:
  return re.split(r"(?<=[.!?])\s+", text.strip())[0].strip() if text.strip() else ""


def main() -> int:
  firsts = _engine_first_sentences()
  rows: list[tuple[float, bool, str, str, bool]] = []

  for key, _description, (account, substrate), utterances in JOURNEYS:
    seed = {"mock_config_string": substrate}
    if account:
      seed.update({"accountNumber": account, "account_id": account})
    session = ChatSession(app_name=APP, initial_variable_state=seed)
    # FAKES=1 substitutes the sub-tool fakes, which removes the BACKEND from the number
    # and leaves engine + model. Worth having both: the backend is what makes the sweep
    # turn slow, and engine+model is what every other turn costs.
    if os.environ.get("FAKES"):
      _orig = session._sessions.run
      session._sessions.run = lambda **kw: _orig(use_tool_fakes=True, **kw)
    # The agent speaks first on a real call, and that greeting turn has no caller waiting
    # on it, so it is driven but not measured.
    session.send_event("session start", event_vars=dict(seed))
    for utterance in utterances:
      start = time.monotonic()
      turn = session.send(utterance)
      elapsed = time.monotonic() - start
      opener = _first_sentence(turn.agent_text)
      # A turn whose EVERY sentence is an engine literal was preempted: the engine
      # answered it and the model was never called. For those, wall clock IS the time to
      # the first spoken word, not merely an upper bound on it -- there is nothing else
      # in the turn. That is the number the 1s target is about.
      sentences = [x for x in re.split(r"(?<=[.!?])\s+", (turn.agent_text or "").strip())
                   if x]
      pure = bool(sentences) and all(
          any(p.match(x.strip()) for p in firsts) for x in sentences)
      rows.append((elapsed, any(p.match(opener) for p in firsts), key, opener, pure))
      # A defer or hand-off journey ends the session mid-list; the rest of its utterances
      # are not turns a caller could take.
      if turn.session_ended:
        break

  if not rows:
    print("no turns measured")
    return 1

  times = sorted(r[0] for r in rows)
  engine = [r for r in rows if r[1]]
  model = [r for r in rows if not r[1]]
  pure = sorted(r[0] for r in rows if r[4])

  print(f"\nturns measured        {len(rows)}")
  print(f"median whole turn     {statistics.median(times):.1f}s   (upper bound on "
        f"time-to-first-audio)")
  print(f"p90 whole turn        {times[int(len(times) * 0.9)]:.1f}s")
  print(f"engine-opened         {len(engine)}/{len(rows)}  "
        f"({100 * len(engine) // len(rows)}%)  <- these start speaking pre-model")
  if pure:
    print(f"\nengine-only turns     {len(pure)}/{len(rows)}  (model never called)")
    print(f"  median              {statistics.median(pure):.2f}s  <- TIME TO FIRST WORD, "
          f"not an upper bound")
    print(f"  p90                 {pure[int(len(pure) * 0.9)]:.2f}s")

  if model:
    print(f"\nmodel-opened turns ({len(model)}) — the caller waits the whole turn for "
          f"these:")
    seen: set[str] = set()
    for elapsed, _ok, key, opener, _pure in sorted(model, reverse=True):
      if opener in seen:
        continue
      seen.add(opener)
      print(f"  {elapsed:5.1f}s  [{key}]  {opener[:88]}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
