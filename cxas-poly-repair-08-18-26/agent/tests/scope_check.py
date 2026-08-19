#!/usr/bin/env python3
"""Offline oracle for the Wi-Fi SCOPE CORRECTION, and for the copy of the cue map it needs.

Two things are checked here, and the second is the reason the first can be trusted.

**The correction itself.** A caller says "just one device", then a turn later "actually
it's the whole house". Nothing in the flow could hear the second sentence:
`wifi_scope_early` is latched (a filled slot is not collected again), and `wifi_scope` was
filled by the promotion in `before_agent` at the top of that very turn -- from a value
written BEFORE the engine saw the correction. So the stale answer won, and the caller
whose whole house was down was walked through moving one device closer to the gateway.
Measured 3/3 text and 3/3 voice before the fix. `before_model` is the only hook with this
turn's utterance in hand, so the correction lives there, and this file drives it directly.

**The cue map's copy.** Hook bodies are rendered VERBATIM into the deployed callback and
module-level references do not survive the emission, so the correction cannot read
`wifi_walkthrough.WIFI_SCOPE_CUES` and has to carry its own copy of it. A copy that can drift is a
copy that will, and the drift would be invisible: the agent would go on answering scope
questions correctly while silently refusing to hear a correction phrased with whichever
cue had been added to only one of the two. So the copy is compared, byte for byte,
against the real map -- pulled out of the hook's own source rather than re-typed here,
because a third copy would be a third thing to drift.

    python tests/scope_check.py
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app as comcast_app  # noqa: E402,F401  -- kept: importing it builds the config
# The cue map moved out of `app.py` with the journey that owns it. Referenced
# through the journey module rather than re-exported, so there is one definition.
from journeys import wifi_walkthrough  # noqa: E402
import hooks  # noqa: E402


class _Part:
  def __init__(self, text):
    self.text = text


class _Content:
  def __init__(self, text, role="user"):
    self.role = role
    self.parts = [_Part(text)]


class _Request:
  def __init__(self, text):
    self.contents = [_Content(text)]


class _Ctx:
  def __init__(self, state):
    self.state = state


def _inlined_cue_map():
  """The `_SCOPE_CUES` literal out of `before_model_callback`'s own source."""
  src = textwrap.dedent(inspect.getsource(hooks.before_model_callback))
  tree = ast.parse(src)
  for node in ast.walk(tree):
    if (isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "_SCOPE_CUES"):
      return ast.literal_eval(node.value)
  raise AssertionError(
      "before_model_callback no longer carries a `_SCOPE_CUES` literal. The scope "
      "correction cannot work without one, and it cannot import the real map -- see "
      "the note at its site.")


def _drive(held, said):
  """Run the correction with `wifi_scope` already holding `held`, on utterance `said`.

  Returns the value `wifi_scope` holds afterwards. `diagnostics_triggered` is set so the
  callback returns before its sweep, which is a different feature and not under test.
  """
  state = {
      "diagnostics_triggered": "true",
      "sm": {"filled": ({"wifi_scope": held} if held else {})},
  }
  hooks.before_model_callback(_Ctx(state), _Request(said))
  return str((state.get("sm") or {}).get("filled", {}).get("wifi_scope") or "")


# (name, value already held, what the caller says, what should be held afterwards)
#
# Every row is a rule from the three guards at the correction's site, and the three
# "unchanged" families are as load-bearing as the corrections: a slot that flips on
# anything is worse than one that never flips, because it flips on the turns nobody is
# watching.
CASES = [
    # The correction itself, in both directions.
    ("one device -> whole house", "ONE_DEVICE", "actually it's the whole house",
     "ALL_DEVICES"),
    ("one device -> nothing works", "ONE_DEVICE", "wait, nothing works",
     "ALL_DEVICES"),
    ("one device -> everything", "ONE_DEVICE", "actually everything is down",
     "ALL_DEVICES"),
    ("one device -> all my devices", "ONE_DEVICE", "no, all my devices are affected",
     "ALL_DEVICES"),
    ("whole house -> one device", "ALL_DEVICES", "sorry, it's just my laptop",
     "ONE_DEVICE"),
    ("whole house -> just one", "ALL_DEVICES", "actually just one device",
     "ONE_DEVICE"),
    ("mid-walkthrough correction still lands", "ONE_DEVICE",
     "hang on, the whole house is out", "ALL_DEVICES"),

    # Guard 1: it only ever OVERWRITES. An unfilled slot belongs to the ordinary capture
    # path -- the engine's own cue match and the `before_agent` promotion -- and this must
    # not race it.
    ("nothing held: left for the engine", "", "it's the whole house", ""),

    # Guard 2: the value must DIFFER. Repeating yourself is not a correction, and a
    # rewrite here would clear latches for no reason.
    ("same answer repeated", "ALL_DEVICES", "yes, the whole house", "ALL_DEVICES"),
    ("same answer, other wording", "ONE_DEVICE", "yes, just one device", "ONE_DEVICE"),

    # Guard 3: exactly ONE value must match. A turn that could be read either way is not
    # evidence enough to overturn an answer the caller already gave.
    ("both scopes named", "ONE_DEVICE",
     "my laptop is fine but everything else is down", "ONE_DEVICE"),

    # No scope vocabulary at all: the commonest turns in the walkthrough.
    ("a tip answer", "ONE_DEVICE", "that didn't help", "ONE_DEVICE"),
    ("a fee question", "ALL_DEVICES", "will I be charged for this?", "ALL_DEVICES"),
    ("plain yes", "ONE_DEVICE", "yes please", "ONE_DEVICE"),
    ("a resolution", "ALL_DEVICES", "oh, it's working now", "ALL_DEVICES"),

    # A silent turn is an inactivity tick, not a caller. `real_user_text()` discards it,
    # and it must not be read as anything about scope.
    ("an inactivity tick", "ONE_DEVICE", "<context>inactivity</context>", "ONE_DEVICE"),
    ("a bare opener", "ONE_DEVICE", "hello", "ONE_DEVICE"),
    ("empty turn", "ALL_DEVICES", "", "ALL_DEVICES"),

    # The carve-out the cue map itself carries: "everything else works" is the plainest
    # way there is to say ONE device, and matching it as ALL_DEVICES was a live defect on
    # the scope question. The correction inherits the carve-out because it inherits the
    # map -- which is what the copy check above is for.
    ("everything ELSE works is not everything", "ONE_DEVICE",
     "everything else works fine", "ONE_DEVICE"),
]


def main() -> int:
  failures = 0

  inlined = _inlined_cue_map()
  if inlined != wifi_walkthrough.WIFI_SCOPE_CUES:
    failures += 1
    print("FAIL cue map copy has drifted from wifi_walkthrough.WIFI_SCOPE_CUES")
    for value in sorted(set(inlined) | set(wifi_walkthrough.WIFI_SCOPE_CUES)):
      mine = inlined.get(value, [])
      theirs = wifi_walkthrough.WIFI_SCOPE_CUES.get(value, [])
      if mine != theirs:
        print(f"       {value}: only in the hook {sorted(set(mine) - set(theirs))}, "
              f"only in app.py {sorted(set(theirs) - set(mine))}")
  else:
    print(f"ok   cue map copy matches wifi_walkthrough.WIFI_SCOPE_CUES "
          f"({sum(len(v) for v in inlined.values())} cues)")

  for name, held, said, want in CASES:
    got = _drive(held, said)
    if got == want:
      print(f"ok   {name:<45} {held or '(none)':<12} -> {got or '(none)'}")
    else:
      failures += 1
      print(f"FAIL {name:<45} {held or '(none)':<12} -> {got or '(none)'}  "
            f"want={want or '(none)'}")

  total = len(CASES) + 1
  print(f"\n{total - failures}/{total} scope corrections correct with no LLM")
  return 1 if failures else 0


if __name__ == "__main__":
  raise SystemExit(main())
