"""Substituting a response over the model's line SUPERSEDES it; it does not suppress it.

Five after_model paths return a response on a turn where the model already produced
text, and every one of them was written believing that replaces it. It does on
`gemini-composite-v1`, where nothing has reached the caller yet. It does NOT on
`gemini-3.1-flash-live`, where the words were streamed before the callback ran — the
caller hears the model's line and then ours. Measured on both models and in both
shapes, text and `function_call`, in ces-probes 91; the `function_call` shape was the
hopeful case and is no better.

The framework cannot fix that from here, so it reports it instead: `_supersede` logs
once per affected turn, and the log is what tells us which paths are worth moving into
before_model, the only place left that can stop the model speaking.

Reuses the CES-global stubbing pattern from `test_filed_issue_fixes.py`.
"""

from __future__ import annotations

import importlib.util
import os


class _Part:
  def __init__(self, kind, **d):
    self.kind = kind
    self.text = d.get("text")
    self.function_call = d.get("function_call")

  @classmethod
  def from_text(cls, text=""):
    return cls("text", text=text)

  @classmethod
  def from_function_call(cls, name="", args=None):
    return cls("call", function_call=type("FC", (), {"name": name, "args": args or {}}))

  @classmethod
  def from_agent_transfer(cls, agent=""):
    return cls("transfer", text=agent)


class _Resp:
  def __init__(self, parts, finish_reason=None):
    self.content = type("C", (), {"parts": parts})
    self.finish_reason = finish_reason

  @classmethod
  def from_parts(cls, parts, finish_reason=None):
    return cls(parts, finish_reason)


class _Ctx:
  def __init__(self, state):
    self.state = dict(state)


def _load():
  path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "src/flows/engine/framework/callbacks/after_model.py")
  spec = importlib.util.spec_from_file_location("_am_supersede", path)
  mod = importlib.util.module_from_spec(spec)
  for name in ("CallbackContext", "LlmRequest", "Content", "Tool", "ces_internal"):
    setattr(mod, name, type(name, (), {}))
  mod.Part = _Part
  mod.LlmResponse = _Resp
  spec.loader.exec_module(mod)
  return mod


_AM = _load()


def _warnings(sm):
  return [e for e in sm.get("_log", [])
          if e.get("tag") == "supersede_not_retractable"]


def test_it_reports_the_double_speak_on_flash_live():
  sm = {"_model": "gemini-3.1-flash-live"}
  ctx = _Ctx({})
  _AM._supersede(sm, ctx, [_Part.from_text("next question")], "steer_reask")
  assert [w["data"]["path"] for w in _warnings(sm)] == ["steer_reask"]


def test_it_stays_quiet_on_composite_where_suppression_really_happens():
  sm = {"_model": "gemini-composite-v1"}
  _AM._supersede(sm, _Ctx({}), [_Part.from_text("next question")], "steer_reask")
  assert _warnings(sm) == []


def test_an_unknown_model_is_treated_as_composite():
  """The non-destructive read: the only cost is a warning we cannot substantiate, and
  an offline caller (a test, the directive oracle) has no model at all."""
  for model in ("", None, "something-else"):
    sm = {"_model": model}
    _AM._supersede(sm, _Ctx({}), [_Part.from_text("x")], "terminal_fallback")
    assert _warnings(sm) == [], model


def test_the_response_is_returned_whatever_the_model():
  """Reporting must not change what the caller hears: the deterministic next step is
  still the more useful of the two lines they end up with."""
  for model in ("gemini-3.1-flash-live", "gemini-composite-v1"):
    resp = _AM._supersede({"_model": model}, _Ctx({}),
                          [_Part.from_text("next question")], "error_parrot")
    assert [p.text for p in resp.content.parts] == ["next question"]


def test_it_persists_sm_so_the_log_survives_the_turn():
  sm = {"_model": "gemini-3.1-flash-live"}
  ctx = _Ctx({})
  _AM._supersede(sm, ctx, [_Part.from_text("x")], "classify_defaulted_continue")
  assert ctx.state["sm"] is sm


def test_a_function_call_substitute_reports_too():
  """The shape probe 91 was built for: it carries no text to append, which looked like
  it might drop the model's line with it. It does not."""
  sm = {"_model": "gemini-3.1-flash-live"}
  _AM._supersede(sm, _Ctx({}), [_Part.from_function_call(name="try_again")],
                 "classify_defaulted_continue")
  assert [w["data"]["path"] for w in _warnings(sm)] == ["classify_defaulted_continue"]
