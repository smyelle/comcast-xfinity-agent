"""How a SILENT turn is delivered — the shape of the response, not the decision.

The engine decides that a turn says nothing (a spent silence rung, an asynchronous wait
holding for a caller who is still talking). This file is about the other half: what the
callback hands back to the platform for such a turn, which is not the same question and
has a different answer depending on whether the caller DID something.

A response carrying no content at all is the right shape for a silence tick — it adds
nothing for a voice pipeline to receive, and the platform's own inactivity clock re-arms
and ticks again (ces-probes 42 and 83, the second driven in audio).

It is NOT the right shape for a turn that carried real caller speech. Driven on a voice
agent that asks a question during an asynchronous wait: the caller said "uh" mid-wait, the
engine held the wait silently, the callback answered with no content — and the request
then FROZE. Every later invocation was handed the same `last_user_text` ("uh"), the same
`scanned_user_text` and the same `n_user_turns`; the words the caller spoke afterwards
never entered `llm_request.contents` at all, and no later turn ever produced audio. The
silence is self-sustaining: the hold sees the same stale utterance, holds again, and the
turn it is answering is never closed. Measured 2 of 2, and reported by five independent
testers as the call simply dying.

So: same decision, two deliveries. A turn nobody took answers with nothing; a turn the
caller ACTED on answers with one EMPTY content — silent to the caller, but a turn the
platform can close.

"Acted on" is not "spoke". The caller can take a turn without speech: a DTMF keypress
arrives as `<context>user pressed 1 on keypad.</context>` and a barge-in as
`<context>agent speaking was interrupted…</context>`, and both leave the platform holding
a turn open. Only the two envelopes the platform authors on its OWN initiative — the
inactivity tick and an asynchronous completion delivery — are turns with nothing to close,
and those two must keep answering with no content at all or the inactivity clock stops
re-arming (ces-probes 42 and 83).

What these tests cannot reach: the freeze itself. `llm_request.contents` is built by the
platform out of the events a turn produced, and no offline harness has that hop — the
stub request holds whatever the test puts in it. These pin the rule the live measurement
produced; the measurement lives in the commit message and in the PR.
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

_CES_GLOBALS = ("CallbackContext", "LlmRequest", "Content", "Tool", "ces_internal")


class _Part:
  def __init__(self, kind, **data):
    self.kind = kind
    self.__dict__.update(data)

  @classmethod
  def from_text(cls, text=""):
    return cls("text", text=text)

  @classmethod
  def from_function_call(cls, name="", args=None):
    return cls("call", name=name, args=args or {})

  @classmethod
  def from_json(cls, payload=""):
    return cls("json", payload=payload)


class _LlmResponse:
  def __init__(self, parts):
    self.parts = parts
    self.partial = None

  @classmethod
  def from_parts(cls, parts):
    return cls(parts)


class _Config:
  def __init__(self):
    self.system_instruction = "base"
    self.hidden = []

  def hide_tool(self, name):
    self.hidden.append(name)


class _Content:
  def __init__(self, role, texts):
    self.role = role
    self.parts = [_Part("text", text=t) for t in texts]


class _Request:
  def __init__(self, contents=None):
    self.config = _Config()
    self.contents = contents if contents is not None else []


class _Ctx:
  def __init__(self, state=None):
    self.state = dict(state or {})


def _load():
  path = os.path.join(
      os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
      "src/flows/engine/framework/callbacks/before_model.py")
  spec = importlib.util.spec_from_file_location("_bm_silent", path)
  mod = importlib.util.module_from_spec(spec)
  for name in _CES_GLOBALS:
    setattr(mod, name, type(name, (), {}))
  mod.Part = _Part
  mod.LlmResponse = _LlmResponse
  mod.tools = type("tools", (), {})
  spec.loader.exec_module(mod)
  return mod


BM = _load()

# The turn shapes, as the platform presents them. A silence tick is a context marker and
# nothing else; a caller turn carries the utterance (and may carry a marker alongside it).
TICK = [_Content("user", ["<context>no user activity detected for 5 seconds.</context>"])]
SPOKE = [_Content("user", ["uh"])]
SPOKE_WITH_MARKER = [_Content(
    "user", ["uh", "<context>no user activity detected for 5 seconds.</context>"])]
SPOKE_INSIDE_MARKER_PART = [_Content(
    "user", ["<context>no user activity detected for 5 seconds.</context> uh"])]
DELIVERY = [_Content("user", [
    "<context>function [check_line] completed with response {\"ok\": true}</context>"])]
DELIVERY_WITH_SPEECH = [_Content("user", [
    "<context>function [check_line] completed with response {\"ok\": true}</context>"
    " the light is red"])]
# The caller acting WITHOUT speaking. Both are `<context>` wrappers, like the two above,
# and that surface similarity is exactly what made the first cut of this fix classify
# them as "nobody took this turn".
KEYPAD = [_Content("user", ["<context>user pressed 1 on keypad.</context>"])]
KEYPAD_LONG = [_Content("user", ["<context>user pressed 7231232 on keypad.</context>"])]
BARGE_IN = [_Content("user", [
    "<context>agent speaking was interrupted. user only heard 'Main'</context>"])]
BARGE_IN_WITH_SPEECH = [_Content("user", [
    "<context>agent speaking was interrupted. user only heard 'Main'</context>",
    "actually make it two"])]
BARGE_IN_WITH_KEYPAD = [_Content("user", [
    "<context>agent speaking was interrupted. user only heard 'Main'</context>",
    "<context>user pressed 2 on keypad.</context>"])]

# The wait's own hold: the engine decided this turn says nothing (`_async_idle_line` is
# cover for dead air, and a turn the caller spent talking is not that).
HOLD = {"preempt": True, "message": "", "silent": True, "speech_class": "await",
        "hide_tools": []}


def _apply(action, contents):
  ctx = _Ctx()
  req = _Request(contents=contents)
  return BM._apply_directive(ctx, req, {}, dict(action), "test")


def _texts(resp):
  return [getattr(p, "text", None) for p in resp.parts]


# ── A turn the caller spoke on ───────────────────────────────────────────────


def test_a_turn_the_caller_spoke_on_is_never_answered_with_no_content():
  """The measured defect. No content = the platform never closes the turn, and the
  caller's next words never reach the request — the call is over, silently."""
  resp = _apply(HOLD, SPOKE)
  assert resp.parts, "a turn carrying caller speech was answered with zero content"


def test_the_hold_is_still_silent_for_the_caller():
  """Closing the turn must not become SAYING something: the hold exists because the
  caller is mid-sentence, and one word over the top of them costs their answer."""
  resp = _apply(HOLD, SPOKE)
  assert not any((t or "").strip() for t in _texts(resp))


def test_speech_alongside_a_context_marker_still_counts_as_speech():
  """CES attaches a marker to the SAME turn the caller speaks in — the inactivity
  clock firing while they were mid-word. The speech is what matters."""
  assert _apply(HOLD, SPOKE_WITH_MARKER).parts


def test_speech_inside_the_same_part_as_a_marker_still_counts():
  """The marker and the utterance are not always separate parts. Asking only "is this
  whole string real speech" answers no for the joined shape and loses the turn, so the
  envelope is STRIPPED and the remainder is what decides."""
  assert _apply(HOLD, SPOKE_INSIDE_MARKER_PART).parts


def test_a_completion_that_lands_on_top_of_speech_still_closes():
  """A backend answering at the same moment the caller does. The completion half is
  the platform's; the words are the caller's, and they are the turn."""
  assert _apply(HOLD, DELIVERY_WITH_SPEECH).parts


def test_the_silent_delivery_records_which_shape_it_used():
  """A silent turn leaves no transcript, so the log entry is the only evidence there
  is; without the flag the two deliveries are indistinguishable after the fact."""
  ctx = _Ctx()
  BM._apply_directive(ctx, _Request(contents=SPOKE), {}, dict(HOLD), "test")
  log = ctx.state[BM._SM_KEY].get("_log") or []
  entries = [e for e in log if e["tag"] == "no_input_silent_tick"]
  assert entries, log
  assert entries[0]["data"]["closes_turn"] is True


# ── A turn the caller took WITHOUT speaking ──────────────────────────────────
#
# The hole in the first cut of this fix, and the reason it is worth its own section: a
# keypress and a barge-in are `<context>` wrappers, so a "did the caller speak" test
# reads them as silence and hands back zero content — the freeze, unchanged, for every
# caller who reaches for the keypad or talks over the agent during a wait.


def test_a_keypress_during_a_silent_hold_closes_the_turn():
  """DTMF is deliberate input with no speech in it at all. This repo unwraps keypad
  envelopes in two places and decodes them on live calls, so a wait that a caller
  answers with a key is a shipped path."""
  assert _apply(HOLD, KEYPAD).parts


def test_a_multi_digit_keypad_entry_closes_the_turn():
  """A whole number entry, not a menu key — the same envelope, longer payload."""
  assert _apply(HOLD, KEYPAD_LONG).parts


def test_a_barge_in_closes_the_turn():
  """The caller talked over the agent. The platform reports what they heard and holds
  a turn open; nothing about that turn is the platform acting on its own."""
  assert _apply(HOLD, BARGE_IN).parts


def test_a_barge_in_carrying_the_words_closes_the_turn():
  assert _apply(HOLD, BARGE_IN_WITH_SPEECH).parts


def test_a_keypress_alongside_a_barge_in_closes_the_turn():
  """Barge-in by keypad: two wrappers, no speech, and still the caller's turn."""
  assert _apply(HOLD, BARGE_IN_WITH_KEYPAD).parts


def test_closing_a_keypad_turn_is_still_silent():
  """Same guarantee as the spoken case: closing is not saying."""
  assert not any((t or "").strip() for t in _texts(_apply(HOLD, KEYPAD)))


# ── The A/B half: a turn the caller did NOT take is unchanged ────────────────


def test_a_silence_tick_is_still_answered_with_nothing_at_all():
  """The shape ces-probes 42 chose and 83 proved in audio: no content whatsoever, so
  the inactivity clock re-arms and the ladder gets its next tick. An empty content
  would reach TTS on every tick of every wait, for no reason."""
  assert _apply(HOLD, TICK).parts == []


def test_a_completion_delivery_the_caller_said_nothing_on_is_a_tick():
  """The async result is the whole content of the turn. Nobody spoke, so nothing is
  waiting to be closed."""
  assert _apply(HOLD, DELIVERY).parts == []


def test_a_spent_silence_rung_on_a_tick_is_unchanged():
  """The other producer of `silent`: an EMPTY `no_input` reprompt. Same delivery."""
  assert _apply({"preempt": True, "message": "", "silent": True,
                 "speech_class": "no_input", "hide_tools": []}, TICK).parts == []


def test_a_turn_with_no_contents_at_all_is_left_to_the_model():
  """Byte-identical to before: the silent branch has always required contents."""
  resp = _apply(HOLD, [])
  assert resp is None or getattr(resp, "parts", None) is None
