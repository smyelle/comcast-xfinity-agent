"""`_classify_turn` — who took this turn, read off the request contents.

This is the one thing the engine cannot work out for itself. Four situations reach it
byte-identical on every input it has (empty `last_user_text`, unchanged `n_user_turns`,
`is_inactivity` false): a post-setter re-invoke, an asynchronous completion push, the
engine's own re-invoke inside that push, and a push whose ingest was skipped. Telling
them apart needs `llm_request.contents`, which only this callback sees.

The rule is inverted from "is this speech", for the reason `_part_requires_closing`
spells out: a keypress and a barge-in are `<context>` wrappers AND the caller acting.
So everything counts as the caller EXCEPT the two envelopes the platform authors on its
own initiative — the inactivity tick and a completion delivery.

CES globals are runtime-injected, so the module is exec'd and the NameError on the first
annotated def is swallowed (same shape as the other callback hardening suites).
"""
from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace

from flows.engine import blessed_source as _bs


def _load():
  path = os.path.join(_bs._CALLBACKS_DIR, "before_model.py")
  spec = importlib.util.spec_from_file_location("_bm_classify", path)
  mod = importlib.util.module_from_spec(spec)
  try:
    spec.loader.exec_module(mod)
  except Exception:  # CES globals are undefined at import time; expected.
    pass
  return mod


_BM = _load()

# The wire shapes, verbatim enough to match the module's own patterns.
TICK = "<context>no user activity for 5 seconds</context>"
DONE = ('<context>function [check_line] completed with response '
        '{"status": "ok"}</context>')
FAILED = "<context>function [check_line] failed with error timeout</context>"
KEYPAD = "<context>user pressed 1 on keypad.</context>"
BARGE = ("<context>agent speaking was interrupted. user only heard "
         "'which device is'</context>")
TRANSFER = "transfer_to_agent"
SPEECH = "the tv box is out"


def _turn(*texts, role="user"):
  """One content with a part per text; `None` is a function_call/response part."""
  parts = [SimpleNamespace(text=t) for t in texts]
  return SimpleNamespace(role=role, parts=parts)


def _kind(*contents):
  return _BM._classify_turn(SimpleNamespace(contents=list(contents)))


# ---------------------------------------------------------------------------
# The caller acting
# ---------------------------------------------------------------------------


def test_speech_is_the_caller():
  assert _kind(_turn(SPEECH)) == "caller"


def test_a_keypress_is_the_caller():
  """A `<context>` wrapper, and still the caller: they pressed a key. Reading this as
  "nobody spoke" is what once left a keypad caller on a dead line."""
  assert _kind(_turn(KEYPAD)) == "caller"


def test_a_barge_in_is_the_caller():
  """They talked over us, which is about as much of a turn as there is."""
  assert _kind(_turn(BARGE)) == "caller"


def test_a_transfer_marker_is_the_caller():
  """Invisible to `n_user_turns`, which is why that counter cannot stand in for this:
  a transferred-in caller is present and waiting."""
  assert _kind(_turn(TRANSFER)) == "caller"


def test_speech_co_present_with_an_envelope_is_the_caller():
  """CES attaches a completion to the turn the caller spoke in when a backend answers
  at the same moment they do. The caller wins."""
  assert _kind(_turn(f"{DONE} {SPEECH}")) == "caller"


def test_a_barge_riding_beside_a_tick_is_the_caller():
  """The case that made the engine branch WIDEN rather than swap: this classifies as
  the caller while `is_inactivity` is still set, because that flag is derived from
  `last_user_text` after the fallback picks the envelope up."""
  assert _kind(_turn(TICK, BARGE)) == "caller"


# ---------------------------------------------------------------------------
# The platform acting
# ---------------------------------------------------------------------------


def test_an_inactivity_tick_is_manufactured():
  assert _kind(_turn(TICK)) == "manufactured"


def test_a_completion_push_is_manufactured():
  assert _kind(_turn(DONE)) == "manufactured"


def test_a_failed_completion_push_is_manufactured():
  """`async_completion_landed` is overwritten to whether the ingest produced anything,
  so a push whose ingest was skipped reads as False there. It is still the platform's
  turn, and this is the row that signal gets wrong."""
  assert _kind(_turn(FAILED)) == "manufactured"


def test_two_envelopes_on_one_turn_are_still_manufactured():
  assert _kind(_turn(TICK, DONE)) == "manufactured"


# ---------------------------------------------------------------------------
# Nobody — another pass inside a turn that already happened
# ---------------------------------------------------------------------------


def test_a_function_response_is_a_continuation():
  """THE distinction, and the reason `_turn_requires_closing` could not just be
  reused: it answers False here too, which is right for deciding there is nothing to
  close and wrong for deciding nobody spoke. Conflating them would mute the agent on
  a caller who had only just finished talking."""
  assert _kind(_turn(SPEECH), _turn(None)) == "continuation"


def test_an_empty_part_is_a_continuation():
  assert _kind(_turn("   ")) == "continuation"


def test_no_contents_at_all_is_a_continuation():
  assert _kind() == "continuation"


def test_a_model_turn_last_is_a_continuation():
  """Only the last content, and only a user one — earlier turns are history."""
  assert _kind(_turn(SPEECH), _turn("sure thing", role="model")) == "continuation"


# ---------------------------------------------------------------------------
# Wired through
# ---------------------------------------------------------------------------


def test_the_engine_is_actually_told():
  """A classifier nothing reads is worth nothing. `_extract` builds the engine's whole
  input dict here, and `run_engine` mirrors that contract offline."""
  import inspect

  from flows.engine import loader

  extract = inspect.getsource(_BM._extract)
  assert "_classify_turn" in extract and "turn_kind" in extract
  assert "turn_kind" in inspect.signature(loader.run_engine).parameters
