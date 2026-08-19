"""after_model's terminal-fallback path, hardened — three of these are audible.

`_try_terminal_fallback` renders a terminal task's closing line when CES did not
re-invoke before_model after the task's tool returned. Three defects lived there:

  * a `then_say` interpolating a value the task never produced was spoken anyway,
    so the caller heard "All done, your confirmation number is {conf}." — a false
    completion AND a raw placeholder. The engine's own terminal path value-gates
    exactly this (`_template_missing_field`); this path had no gate at all.
  * substitution ran over the payload's JSON *text*, so a slot value containing a
    `"` produced invalid JSON and the `json.loads` after it raised uncaught —
    the caller gets the platform "I'm having trouble" crash envelope.
  * `interruptable: false` was dropped while resolving a text descriptor, which
    made the branch that consumes it unreachable: an author who asked to disable
    barge-in (a legal / recording disclaimer) silently did not get it on a call.

Plus two quiet ones: `{{escaped braces}}` stopped unescaping as soon as any key
was missing, and several `_log` calls used the level `"WARNING"`, which is in
neither of the module's level tables — so under `_log_level: "WARN"` they were
silently dropped, exactly when they matter most.

CES globals are runtime-injected, so the module is exec'd and the `NameError` on
the first annotated def is swallowed; `Part` / `LlmResponse` are stubbed.
"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

from flows.engine import blessed_source as _bs


def _load():
  path = os.path.join(_bs._CALLBACKS_DIR, "after_model.py")
  spec = importlib.util.spec_from_file_location("_am_hardening", path)
  mod = importlib.util.module_from_spec(spec)
  try:
    spec.loader.exec_module(mod)
  except Exception:  # CES globals are undefined at import time; expected.
    pass
  return mod


_AM = _load()


class _StubPart:
  def __init__(self, kind, **fields):
    self.kind = kind
    self.fields = fields
    self.text = fields.get("text")


class _Part:
  @staticmethod
  def from_text(text):
    return _StubPart("text", text=text)

  @staticmethod
  def from_customized_response(content, disable_barge_in):
    return _StubPart("customized", content=content,
                     disable_barge_in=disable_barge_in)

  @staticmethod
  def from_json(payload):
    return _StubPart("json", payload=payload)

  @staticmethod
  def from_audio(audio_uri, cancellable=False, interruptable=True):
    return _StubPart("audio", audioUri=audio_uri, cancellable=cancellable,
                     interruptable=interruptable)

  @staticmethod
  def from_end_session(reason, escalated):
    return _StubPart("end_session", reason=reason, escalated=escalated)

  @staticmethod
  def from_agent_transfer(agent):
    return _StubPart("transfer", agent=agent)


class _LlmResponse:
  @staticmethod
  def from_parts(parts):
    return type("R", (), {"parts": list(parts)})


class _Ctx:
  def __init__(self, **state):
    self.state = dict(state)


@pytest.fixture(autouse=True)
def _ces_globals():
  _AM.Part = _Part
  _AM.LlmResponse = _LlmResponse
  yield
  for name in ("Part", "LlmResponse"):
    if hasattr(_AM, name):
      delattr(_AM, name)


def _sm(task, **task_info):
  """A slot machine parked exactly where the fallback fires: a terminal task that
  just completed successfully, with before_model never having been re-invoked."""
  info = {"task_name": task, "terminal": True, "success_check": "success"}
  info.update(task_info)
  return {
      "_task_just_completed": task,
      "_executor_tasks": {f"do_{task}": info},
      "task_results": {task: {"success": True, **task_info.pop("_result", {})}},
      "filled": {},
  }


# =========================================================================== #
# The false "done" line (value-gated, as the engine's terminal path already is)
# =========================================================================== #
def test_it_says_nothing_when_the_closing_line_names_a_value_the_task_never_made():
  sm = _sm("book", then_say="All done, your confirmation number is {conf}.")
  parts = _AM._try_terminal_fallback(sm, _Ctx())
  assert parts is None, "a false completion (and a raw {conf}) reached the caller"


def test_the_gated_turn_does_not_go_zombie_either():
  """Saying nothing is only half the fix: `status = zombie` would end the flow on
  a job that did not demonstrably finish."""
  sm = _sm("book", then_say="Done, confirmation {conf}.")
  _AM._try_terminal_fallback(sm, _Ctx())
  assert sm.get("status") != "zombie"
  assert sm["_task_just_completed"] == "book", "the task must stay unconsumed"


def test_the_gate_is_recorded_so_it_is_diagnosable():
  sm = _sm("book", then_say="Done, confirmation {conf}.")
  _AM._try_terminal_fallback(sm, _Ctx())
  gated = [e for e in sm["_log"] if e["tag"] == "then_say_value_gated"]
  assert [e["level"] for e in gated] == ["WARN"]


def test_an_empty_string_counts_as_not_produced():
  """The executor answered, but with nothing in the field — the caller must not be
  told "your confirmation number is ." either."""
  sm = _sm("book", then_say="Done, confirmation {conf}.")
  sm["task_results"]["book"]["conf"] = ""
  assert _AM._try_terminal_fallback(sm, _Ctx()) is None


def test_the_ordinary_completion_is_untouched():
  sm = _sm("book", then_say="All done, your confirmation number is {conf}.")
  sm["task_results"]["book"]["conf"] = "AB12"
  parts = _AM._try_terminal_fallback(sm, _Ctx())
  assert [p.text for p in parts] == [
      "All done, your confirmation number is AB12. "]
  assert sm["status"] == "zombie"


def test_a_closing_line_with_no_tokens_at_all_is_never_gated():
  sm = _sm("book", then_say="You're all set.")
  parts = _AM._try_terminal_fallback(sm, _Ctx())
  assert [p.text for p in parts] == ["You're all set. "]


def test_a_value_filled_from_a_slot_rather_than_the_result_still_speaks():
  sm = _sm("book", then_say="Thanks {name}, you're all set.")
  sm["filled"]["name"] = "Alex"
  parts = _AM._try_terminal_fallback(sm, _Ctx())
  assert [p.text for p in parts] == ["Thanks Alex, you're all set. "]


def test_a_gated_then_say_still_lets_a_then_response_line_render():
  """The gate suppresses the false claim, not the whole turn."""
  sm = _sm("book", then_say="Done, confirmation {conf}.",
           then_response=[{"type": "text", "text": "Sorry, I couldn't finish."}])
  parts = _AM._try_terminal_fallback(sm, _Ctx())
  assert [p.text for p in parts] == ["Sorry, I couldn't finish. "]


# =========================================================================== #
# A quote in a slot value used to crash the turn
# =========================================================================== #
def test_a_quote_in_a_value_no_longer_takes_the_turn_down():
  """Substituting into JSON TEXT made `"` invalid JSON; the json.loads that
  followed raised, and CES rendered its crash envelope."""
  resolved = _AM._resolve_terminal_response(
      [{"type": "payload", "data": {"label": "{nickname}"}}],
      {"nickname": 'the "big" one'})
  assert resolved[0]["data"] == {"label": 'the "big" one'}


@pytest.mark.parametrize("value", ['a "quote"', "back\\slash", "line\nbreak",
                                   "brace } here", "unicode – dash"])
def test_any_awkward_character_survives_a_payload_substitution(value):
  resolved = _AM._resolve_terminal_response(
      [{"type": "payload", "data": {"v": "{x}"}}], {"x": value})
  assert resolved[0]["data"]["v"] == value
  json.dumps(resolved[0]["data"])  # still a serializable payload


def test_a_payload_is_substituted_all_the_way_down_and_keeps_its_scalars():
  resolved = _AM._resolve_terminal_response(
      [{"type": "payload", "data": {
          "card": {"title": "{who}", "rows": ["{who}", 7, True, None]}}}],
      {"who": 'Al "AJ" Jones'})
  assert resolved[0]["data"] == {
      "card": {"title": 'Al "AJ" Jones', "rows": ['Al "AJ" Jones', 7, True, None]}}


def test_the_crashing_payload_renders_through_the_terminal_fallback():
  sm = _sm("book", then_say="You're all set.",
           then_response=[{"type": "payload", "data": {"ref": "{conf}"}}])
  sm["task_results"]["book"]["conf"] = 'ID "42"'
  parts = _AM._try_terminal_fallback(sm, _Ctx())
  payloads = [json.loads(p.fields["payload"]) for p in parts if p.kind == "json"]
  assert payloads == [{"ref": 'ID "42"'}]


# =========================================================================== #
# `interruptable: false` reached the renderer at last
# =========================================================================== #
def test_a_non_interruptable_line_keeps_its_flag_through_resolution():
  resolved = _AM._resolve_terminal_response(
      [{"type": "text", "text": "This call may be recorded.",
        "interruptable": False}], {})
  assert resolved[0]["interruptable"] is False


def test_barge_in_is_actually_disabled_on_the_rendered_part():
  sm = _sm("close", then_response=[
      {"type": "text", "text": "This call may be recorded, {name}.",
       "interruptable": False}])
  sm["filled"]["name"] = "Alex"
  parts = _AM._try_terminal_fallback(sm, _Ctx())
  assert [(p.kind, p.fields.get("disable_barge_in")) for p in parts] == [
      ("customized", True)]
  assert parts[0].fields["content"] == "This call may be recorded, Alex. "


def test_an_ordinary_text_line_is_still_interruptable():
  sm = _sm("close", then_response=[{"type": "text", "text": "Goodbye."}])
  parts = _AM._try_terminal_fallback(sm, _Ctx())
  assert [p.kind for p in parts] == ["text"]


# =========================================================================== #
# Escaped braces, and the level that was never in the table
# =========================================================================== #
def test_escaped_braces_unescape_even_when_a_key_is_missing():
  """`.format()` unescapes `{{`/`}}` on the happy path. The moment one key was
  absent the whole template fell to the regex fallback, which did not — so the
  caller heard doubled braces only on the error path."""
  assert _AM._substitute("{{literal}} {gone}", {}) == "{literal} {gone}"


def test_a_present_key_still_resolves_beside_an_escaped_brace_and_a_missing_one():
  assert _AM._substitute("{{x}} {a} {b}", {"a": "1"}) == "{x} 1 {b}"


def test_the_happy_path_is_unchanged():
  assert _AM._substitute("{{x}} {a}", {"a": "1"}) == "{x} 1"


def test_a_literal_escaped_brace_is_not_reported_as_a_missing_key(caplog):
  with caplog.at_level("WARNING", logger="slot_filling.after_model"):
    _AM._substitute("{{word}} {gone}", {})
  assert "'gone'" in caplog.text and "'word'" not in caplog.text


def test_a_gated_warning_survives_a_warn_log_threshold():
  """`"WARNING"` is in neither `_LEVEL_MAP` nor `_LEVEL_ORDER`, so it scored as
  INFO and was dropped by an operator who had raised the floor to WARN."""
  sm = _sm("book", then_say="Done, confirmation {conf}.")
  sm["_log_level"] = "WARN"
  _AM._try_terminal_fallback(sm, _Ctx())
  assert [e["tag"] for e in sm.get("_log", [])] == ["then_say_value_gated"]


def test_no_call_site_uses_a_level_the_tables_do_not_know():
  with open(os.path.join(_bs._CALLBACKS_DIR, "after_model.py"),
            encoding="utf-8") as fh:
    src = fh.read()
  assert '"WARNING"' not in src
  for level in ("DEBUG", "INFO", "WARN", "ERROR"):
    assert level in _AM._LEVEL_MAP and level in _AM._LEVEL_ORDER
