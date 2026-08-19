"""Pass-A intent classification must not deadlock a language switch.

Regression for the FedEx CXAS report `flows-passA-language-switching-bug`. With an
app configured `language_switching="auto"` AND intent-first specialists
(`robust_switching=True` / `bootstrap.intent_first`), a caller turn in the non-default
language put the intent-first Pass A into a bind: the injected `<language_detection>`
block tells the model to call `update_language` BEFORE it speaks, while the Pass-A
classifier SI tells it to emit ONLY `classify_turn_intent`. On such a turn the model was
pulled off the classifier, and — worse — if it DID honor the language block and called
`update_language`, the after_model Pass-A handler superseded that call with `try_again`,
silently DROPPING the caller's requested language switch.

Two framework changes fix it (the report's preferred option 1):

* before_model reconciles the two contracts in Pass A: when the agent carries a
  `<language_detection>` block, the classifier SI gains a `<language_in_pass_a>` note
  that keeps `update_language` allowed while STILL requiring `classify_turn_intent`.
* after_model THREADS an `update_language` call through Pass A (executes it + defaults
  the intent to `continue` so the same turn proceeds to Pass B) instead of dropping it.

Both are strict no-ops for every non-language agent, driven here against the real
callback source with the CES globals stubbed (the pattern from `test_supersede.py` /
`test_progressive_fanout_callback.py`).
"""

from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


class _Part:
  def __init__(self, kind, **d):
    self.kind = kind
    self.text = d.get("text")
    self.function_call = d.get("function_call")
    self.__dict__.update({k: v for k, v in d.items() if k != "function_call"})

  @classmethod
  def from_text(cls, text=""):
    return cls("text", text=text)

  @classmethod
  def from_function_call(cls, name="", args=None):
    fc = type("FC", (), {"name": name, "args": args or {}})
    return cls("call", function_call=fc, name=name, args=args or {})

  @classmethod
  def from_agent_transfer(cls, agent=""):
    return cls("transfer", text=agent)

  @classmethod
  def from_json(cls, payload=""):
    return cls("json", payload=payload)


class _Resp:
  def __init__(self, parts):
    self.content = type("C", (), {"parts": parts})
    self.parts = parts
    self.partial = None

  @classmethod
  def from_parts(cls, parts):
    return cls(parts)


class _Ctx:
  def __init__(self, state=None):
    self.state = dict(state or {})


def _load(rel, name):
  path = os.path.join(_ROOT, rel)
  spec = importlib.util.spec_from_file_location(name, path)
  mod = importlib.util.module_from_spec(spec)
  for g in ("CallbackContext", "LlmRequest", "Content", "Tool", "ces_internal"):
    setattr(mod, g, type(g, (), {}))
  mod.Part = _Part
  mod.LlmResponse = _Resp
  mod.tools = type("tools", (), {})
  spec.loader.exec_module(mod)
  return mod


_AM = _load("src/flows/engine/framework/callbacks/after_model.py", "_am_lang")
_BM = _load("src/flows/engine/framework/callbacks/before_model.py", "_bm_lang")


# --------------------------------------------------------------------------- #
# after_model (Part B): thread an update_language call through Pass A.

def _classify_turn(sm, parts):
  return _AM.after_model_callback(_Ctx({"sm": sm}), _Resp(parts))


def test_lone_update_language_in_pass_a_is_threaded_not_dropped():
  """The core bug: the caller switched language, the model called update_language and
  not the classifier. The switch must be EXECUTED (passed through), the turn defaulted
  to `continue`, and try_again must NOT stand in for it."""
  sm = {"_classify_mode": True, "_model": "gemini-3.1-flash-live",
        "_log_level": "DEBUG"}  # the threaded-through marker logs at DEBUG
  resp = _classify_turn(sm, [_Part.from_function_call(
      "update_language", {"new_language": "Spanish"})])
  names = [getattr(getattr(p, "function_call", None), "name", "")
           for p in resp.content.parts]
  assert names == ["update_language"]  # the switch is threaded through, not dropped
  assert "try_again" not in names
  # its args survive, so CES actually flips to the requested language
  assert resp.content.parts[0].function_call.args == {"new_language": "Spanish"}
  # the same turn proceeds into Pass B as an ordinary answer
  assert sm["_pending_intent"] == "continue"
  assert any(e.get("tag") == "classify_update_language_threaded"
             for e in sm.get("_log", []))


def test_update_language_with_other_tools_discards_them_threads_only_the_switch():
  """The model honored the language block but ALSO emitted noise — a custom/invalid tool
  or two (anything that is NOT classify_turn_intent) on the same Pass-A turn. Threading
  rebuilds the response from just the switch, so the stray calls are safely discarded and
  only update_language reaches CES; the turn still defaults to `continue` for Pass B."""
  sm = {"_classify_mode": True, "_model": "gemini-3.1-flash-live",
        "_log_level": "DEBUG"}
  resp = _classify_turn(sm, [
      _Part.from_function_call("do_something_custom", {"foo": "bar"}),
      _Part.from_function_call("update_language", {"new_language": "Spanish"}),
      _Part.from_function_call("not_a_real_tool", {})])
  names = [getattr(getattr(p, "function_call", None), "name", "")
           for p in resp.content.parts]
  assert names == ["update_language"]  # ONLY the switch survives
  assert "do_something_custom" not in names  # the stray calls are discarded
  assert "not_a_real_tool" not in names
  assert "try_again" not in names
  # the switch's args are preserved so CES flips to the requested language
  assert resp.content.parts[0].function_call.args == {"new_language": "Spanish"}
  assert sm["_pending_intent"] == "continue"  # same turn proceeds into Pass B
  assert any(e.get("tag") == "classify_update_language_threaded"
             for e in sm.get("_log", []))


def test_update_language_alongside_classify_passes_both_through():
  """When the model honors BOTH contracts (the reconciled Pass-A note asks for this),
  every call is let through untouched so CES runs the switch and the classifier."""
  sm = {"_classify_mode": True}
  resp = _classify_turn(sm, [
      _Part.from_function_call("update_language", {"new_language": "Spanish"}),
      _Part.from_function_call("classify_turn_intent", {"intent": "continue"})])
  assert resp is None  # None => the model's own calls stand
  # classify present -> after_model must NOT default the intent itself
  assert "_pending_intent" not in sm


def test_classify_only_still_passes_through():
  """The ordinary intent-first turn is unchanged: a bare classify is let through."""
  sm = {"_classify_mode": True}
  resp = _classify_turn(sm, [
      _Part.from_function_call("classify_turn_intent", {"intent": "switch:pickup"})])
  assert resp is None
  assert "_pending_intent" not in sm


def test_text_only_still_defaults_to_continue_via_try_again():
  """No language call, no classify — the pre-existing behavior is preserved: default to
  `continue` and hop to Pass B via try_again (never a burned turn)."""
  sm = {"_classify_mode": True, "_model": "gemini-composite-v1"}
  resp = _classify_turn(sm, [_Part.from_text("necesito rastrear mi paquete")])
  names = [getattr(getattr(p, "function_call", None), "name", "")
           for p in resp.content.parts]
  assert names == ["try_again"]
  assert sm["_pending_intent"] == "continue"


def test_non_classify_turn_is_untouched_by_the_language_thread():
  """The Pass-A branch only runs under _classify_mode; a normal turn is not affected."""
  sm = {}  # not in classify mode
  resp = _classify_turn(sm, [_Part.from_function_call(
      "update_language", {"new_language": "Spanish"})])
  # falls through the classify branch entirely (fc present -> returns None)
  assert resp is None
  assert "_pending_intent" not in sm


# --------------------------------------------------------------------------- #
# before_model (Part A): reconcile the classifier SI with <language_detection>.

class _FnDecl:
  def __init__(self, name):
    self.name = name


class _ToolDecl:
  """A google-genai `Tool`: a bag of function_declarations, each with a .name."""
  def __init__(self, *names):
    self.function_declarations = [_FnDecl(n) for n in names]


class _Config:
  def __init__(self, system_instruction="base", tools=None):
    self.system_instruction = system_instruction
    self.hidden = []
    self.tools = tools

  def hide_tool(self, name):
    self.hidden.append(name)


class _Request:
  def __init__(self, system_instruction="base", contents=None, tools=None):
    # `tools` is a list of declared function-tool names; modelled as the
    # google-genai `config.tools` surface (a list of Tool objects, each carrying
    # function_declarations[].name) that _has_tool now reads. None => config.tools
    # is None, the absent path (the live CES surface we have not confirmed).
    cfg_tools = [_ToolDecl(*tools)] if tools is not None else None
    self.config = _Config(system_instruction, tools=cfg_tools)
    self.contents = contents if contents is not None else []


_LANG_BLOCK = (
    "You are a tracking agent.\n\n"
    "<language_detection>\n"
    "  <action>Call update_language with the target language BEFORE you speak.</action>\n"
    "</language_detection>\n")

_PASS_A = {"si": "<intent_classifier>classify only</intent_classifier>",
           "hide_tools": [], "tag": "pass_a_classify"}


def _apply(action, base_si, tools=None):
  ctx = _Ctx({})
  req = _Request(system_instruction=base_si, tools=tools)
  _BM._apply_directive(ctx, req, {}, action, "test")
  return req.config.system_instruction


def test_pass_a_note_is_appended_when_language_detection_is_present():
  """The reconciliation: with a <language_detection> block, Pass A keeps the classifier
  AND permits the orthogonal language switch, so the two contracts stop contradicting."""
  si = _apply(_PASS_A, _LANG_BLOCK)
  assert "<intent_classifier>" in si            # the classifier SI is still applied
  assert "<language_in_pass_a>" in si           # ...reconciled with the language block
  assert "update_language" in si
  assert "classify_turn_intent" in si


def test_no_language_block_is_byte_identical_noop():
  """Every non-language agent is untouched: no note, just the classifier suffix. The
  request has NO tools (config.tools is None), the state we cannot yet confirm on the
  live CES surface — so this also pins that an absent config.tools is a safe no-op."""
  plain = "You are a tracking agent.\n"
  si = _apply(_PASS_A, plain)
  assert "<language_in_pass_a>" not in si
  assert "<intent_classifier>" in si


def test_has_tool_reads_config_tools_and_degrades_to_false():
  """_has_tool scans config.tools' function_declarations[].name, and any absence/shape
  surprise on the polymorphic surface degrades to False (never raises) so it can only
  OR-ADD detection."""
  assert _BM._has_tool(_Request(tools=["update_language"]),
                       "update_language") is True
  # one Tool can declare several functions — a match anywhere counts
  assert _BM._has_tool(_Request(tools=["set_slot", "update_language"]),
                       "update_language") is True
  assert _BM._has_tool(_Request(tools=["set_slot"]),
                       "update_language") is False
  assert _BM._has_tool(_Request(), "update_language") is False  # config.tools is None
  assert _BM._has_tool(_Request(tools=[]), "update_language") is False  # empty list
  # a non-iterable config.tools must not raise — it degrades to False
  bad = _Request()
  bad.config.tools = object()
  assert _BM._has_tool(bad, "update_language") is False
  # a tool without function_declarations (a callable / MCP / built-in) is skipped
  weird = _Request()
  weird.config.tools = [object()]
  assert _BM._has_tool(weird, "update_language") is False


def test_pass_a_note_added_via_config_tools_without_the_si_marker():
  """The semantic path: a language agent whose base SI does NOT carry the verbatim
  <language_detection> marker still gets the Pass-A note when update_language is a
  declared tool (config.tools) — the robustness the reviewer asked for."""
  plain = "You are a tracking agent.\n"  # no <language_detection> substring
  si = _apply(_PASS_A, plain, tools=["update_language"])
  assert "<language_in_pass_a>" in si
  assert "<intent_classifier>" in si


def test_pass_a_note_added_via_si_marker_when_config_tools_absent():
  """Regression safety: with the SI marker present but NO tools on the request
  (today's unconfirmed CES surface), the note still fires — OR, not AND."""
  si = _apply(_PASS_A, _LANG_BLOCK, tools=None)
  assert "<language_in_pass_a>" in si


def test_pass_a_no_note_when_neither_signal_present():
  """Neither the marker nor a declared update_language tool -> strict no-op, even with
  unrelated tools declared."""
  plain = "You are a tracking agent.\n"
  si = _apply(_PASS_A, plain, tools=["set_slot", "classify_turn_intent"])
  assert "<language_in_pass_a>" not in si


def test_note_is_pass_a_only_not_ordinary_phases():
  """A non-classifier phase (Pass B / collection) that happens to carry the language
  block must NOT get the Pass-A note — its full instruction already handles the switch."""
  pass_b = {"si": "<collection>ask the next question</collection>",
            "hide_tools": [], "tag": "collect"}
  si = _apply(pass_b, _LANG_BLOCK)
  assert "<language_in_pass_a>" not in si
