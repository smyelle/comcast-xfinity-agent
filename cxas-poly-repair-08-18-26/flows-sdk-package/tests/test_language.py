"""Language authoring: languageSettings emit + update_language tool + the
<language_detection> instruction block + the active_language variable, plus the
back-compat guarantee that a no-language app emits exactly as before.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_language.py
"""

from __future__ import annotations

import json
import os

import pytest

import flows
from flows.engine import blessed_source as _bs


def _solo() -> flows.Flow:
  f = flows.Flow("solo", root_agent="Solo_Agent")
  f.add(flows.user_slot("x", "What do you need?"), flows.announce("d", ["ok"], end=True))
  return f


def _lang_app(**kw) -> flows.App:
  opts = dict(
      languages=["en-US", "es-US", "fr-CA"],
      default_language="en-US",
      language_switching="explicit",
  )
  opts.update(kw)
  return flows.App(root_flow=_solo(), app_display_name="Lang Demo", **opts)


def _emit(app: flows.App, tmp_path) -> str:
  out = str(tmp_path / "app")
  res = flows.build_app(app, out)
  assert res.ok, res.validation.errors if res.validation else res.error
  return out


def _load(out: str, *parts: str) -> dict:
  return json.loads(open(os.path.join(out, *parts)).read())


def _read(out: str, *parts: str) -> str:
  return open(os.path.join(out, *parts)).read()


# --- validation --------------------------------------------------------------
def test_language_app_validates_clean():
  errors, _warnings = flows.validate_app(_lang_app())
  assert errors == [], errors


def test_switching_requires_two_languages():
  with pytest.raises(ValueError, match="at least 2 languages"):
    flows.App(root_flow=_solo(), app_display_name="X",
              languages=["en-US"], language_switching="explicit")


def test_default_language_must_be_supported():
  with pytest.raises(ValueError, match="not in languages"):
    flows.App(root_flow=_solo(), app_display_name="X",
              languages=["en-US", "es-US"], default_language="de-DE",
              language_switching="explicit")


# --- languageSettings --------------------------------------------------------
def test_language_settings_emitted(tmp_path):
  appj = _load(_emit(_lang_app(), tmp_path), "app.json")
  ls = appj["languageSettings"]
  assert ls["defaultLanguageCode"] == "en-US"
  assert ls["supportedLanguageCodes"] == ["es-US", "fr-CA"]
  assert ls["enableMultilingualSupport"] is True


def test_single_language_sets_settings_without_switching(tmp_path):
  # languages set but switching off: languageSettings emitted, but no tool/var/block.
  app = flows.App(root_flow=_solo(), app_display_name="Mono", languages=["en-US"])
  out = _emit(app, tmp_path)
  appj = _load(out, "app.json")
  assert appj["languageSettings"]["defaultLanguageCode"] == "en-US"
  assert appj["languageSettings"]["supportedLanguageCodes"] == []
  assert not os.path.isdir(os.path.join(out, "tools", "update_language"))
  assert not any(v["name"] == "active_language"
                 for v in appj["variableDeclarations"])


# --- update_language tool ----------------------------------------------------
def test_update_language_tool_emitted_and_scoped(tmp_path):
  out = _emit(_lang_app(), tmp_path)
  src = _read(out, "tools", "update_language", "python_function", "python_code.py")
  assert "def update_language(new_language: str" in src
  # The switch must persist to context.state (what callback_context.state reads), NOT
  # context.variables (a separate read-oriented namespace the callbacks never see).
  assert 'ctx.state["active_language"]' in src
  assert 'context.variables["active_language"]' not in src
  for name in ("English", "Spanish", "French"):
    assert name in src
  agent = _load(out, "agents", "Solo_Agent", "Solo_Agent.json")
  assert "update_language" in agent["tools"]


class _ShimCtx:
  """Stand-in for the CES-injected `context` global: `.state` is the session state
  that callback_context.state reads; `.variables` is the separate read-oriented
  namespace. Mirrors flows.engine.loader._ContextShim's split without importing it."""

  def __init__(self):
    self.state: dict = {}
    self.variables: dict = {}


def _run_update_language(src: str, new_language: str, *, as_global: bool):
  """Exec the emitted update_language body and call it, binding `context` either as the
  CES-injected module GLOBAL (as_global=True, the live convention) or as the kwarg the
  defaulted param would receive. Returns (result, shim)."""
  ns: dict = {}
  exec(compile(src, "<update_language>", "exec"), ns)  # noqa: S102 - trusted emitted src
  shim = _ShimCtx()
  if as_global:
    ns["context"] = shim
    result = ns["update_language"](new_language)
  else:
    result = ns["update_language"](new_language, context=shim)
  return result, shim


def test_update_language_persists_to_state_via_injected_global(tmp_path):
  # The switch must land in context.state (top-level), where the language-lock hook and
  # the framework relay read it. Writing context.variables (the old behaviour) left
  # callback_context.state['active_language'] unset, so every caller-driven switch leaked.
  out = _emit(_lang_app(), tmp_path)
  src = _read(out, "tools", "update_language", "python_function", "python_code.py")
  result, shim = _run_update_language(src, "Spanish", as_global=True)
  assert result["success"] is True
  assert result["active_language"] == "Spanish"
  assert shim.state.get("active_language") == "Spanish"  # durable, callbacks see it
  assert "active_language" not in shim.variables  # NOT the dead namespace


def test_update_language_persists_when_context_passed_as_param(tmp_path):
  # Robustness: if CES fills the defaulted `context` param instead of binding the global,
  # the write still lands in .state (not .variables).
  out = _emit(_lang_app(), tmp_path)
  src = _read(out, "tools", "update_language", "python_function", "python_code.py")
  _result, shim = _run_update_language(src, "French", as_global=False)
  assert shim.state.get("active_language") == "French"


def test_update_language_rejects_unsupported_without_writing(tmp_path):
  out = _emit(_lang_app(), tmp_path)
  src = _read(out, "tools", "update_language", "python_function", "python_code.py")
  result, shim = _run_update_language(src, "Klingon", as_global=True)
  assert result["success"] is False
  assert "active_language" not in shim.state  # no partial write on a rejected switch


def test_active_language_variable_declared(tmp_path):
  appj = _load(_emit(_lang_app(), tmp_path), "app.json")
  var = next(v for v in appj["variableDeclarations"] if v["name"] == "active_language")
  assert var["schema"]["default"] == "English"


# --- instruction block -------------------------------------------------------
def test_language_detection_block_appended(tmp_path):
  instr = _read(_emit(_lang_app(), tmp_path),
                "agents", "Solo_Agent", "instruction.txt")
  assert "<language_detection>" in instr
  assert "update_language" in instr
  assert "English" in instr and "Spanish" in instr and "French" in instr
  # explicit mode: the block forbids switching on non-explicit input.
  assert "ONLY switch" in instr


def test_explicit_and_auto_modes_differ(tmp_path):
  from flows.authoring import language as _lang
  langs = ["en-US", "es-US"]
  explicit = _lang.language_detection_block(langs, "en-US", "explicit")
  auto = _lang.language_detection_block(langs, "en-US", "auto")
  assert "ONLY switch" in explicit
  assert "unambiguous sentence" in auto


# --- back-compat -------------------------------------------------------------
def test_no_language_app_unchanged(tmp_path):
  app = flows.App(root_flow=_solo(), app_display_name="Plain")
  out = _emit(app, tmp_path)
  appj = _load(out, "app.json")
  assert "languageSettings" not in appj
  assert not os.path.isdir(os.path.join(out, "tools", "update_language"))
  assert not any(v["name"] == "active_language"
                 for v in appj["variableDeclarations"])
  instr = _read(out, "agents", "Solo_Agent", "instruction.txt")
  assert "<language_detection>" not in instr


def test_framework_in_sync_with_language(tmp_path):
  out = _emit(_lang_app(), tmp_path)
  report = _bs.verify_app_dir(out)
  assert report.ok, report.summary()


# --- select mode: turn-1 menu + hard lock ------------------------------------
def _select_app(**kw) -> flows.App:
  opts = dict(
      languages=["en-US", "es-US", "fr-CA"],
      default_language="en-US",
      language_switching="select",
      language_prompt="Welcome to Acme. Para espanol, marque nueve, or say Spanish.",
  )
  opts.update(kw)
  return flows.App(root_flow=_solo(), app_display_name="Select Demo", **opts)


def test_select_mode_validates_clean():
  errors, _warnings = flows.validate_app(_select_app())
  assert errors == [], errors


def test_select_emits_menu_slot_with_dtmf(tmp_path, dag_config):
  out = _emit(_select_app(), tmp_path)
  cfg = dag_config(
      _read(out, "tools", "solo_dag", "python_function", "python_code.py"), "solo")
  menu = next(s for s in cfg["slots"] if s["name"] == "language_choice")
  assert "es-US" in str(menu) and "9" in str(menu)  # press 9 -> Spanish
  # the menu is the first USER slot (before the flow's own 'x' user slot)
  user_slots = [s["name"] for s in cfg["slots"] if s.get("source") == "user"]
  assert user_slots.index("language_choice") < user_slots.index("x")


def test_select_emits_lock_block_not_detection(tmp_path):
  instr = _read(_emit(_select_app(), tmp_path), "agents", "Solo_Agent", "instruction.txt")
  assert "<language_lock>" in instr
  assert "<language_detection>" not in instr
  assert "ONLY in that selected language" in instr


def test_select_emits_nudge_hooks(tmp_path):
  out = _emit(_select_app(), tmp_path)
  base = os.path.join(out, "agents", "Solo_Agent")
  assert os.path.isfile(os.path.join(
      base, "before_model_callbacks", "before_model_callbacks_03", "python_code.py"))
  assert os.path.isfile(os.path.join(
      base, "after_model_callbacks", "after_model_callbacks_03", "python_code.py"))
  aj = _load(out, "agents", "Solo_Agent", "Solo_Agent.json")
  before = [c["pythonCode"] for c in aj["beforeModelCallbacks"]]
  after = [c["pythonCode"] for c in aj["afterModelCallbacks"]]
  assert any("before_model_callbacks_01" in c for c in before)  # framework intact
  assert any("before_model_callbacks_03" in c for c in before)  # nudge added
  assert any("after_model_callbacks_03" in c for c in after)


def test_select_scopes_try_again_not_update_language(tmp_path):
  aj = _load(_emit(_select_app(), tmp_path), "agents", "Solo_Agent", "Solo_Agent.json")
  assert "try_again" in aj["tools"]
  assert "update_language" not in aj["tools"]  # select = lock, no caller switch
  assert not os.path.isdir(os.path.join(
      _emit(_select_app(), tmp_path), "tools", "update_language"))


def test_select_multi_agent_rejected():
  a = flows.Agent("A_Agent", flow=flows.Flow("aa", root_agent="A_Agent"))
  b = flows.Agent("B_Agent", flow=flows.Flow("bb", root_agent="B_Agent"))
  host = flows.HostRouter("H", routes={"aa": a, "bb": b})
  app = flows.App(host=host, agents=[a, b], app_display_name="Multi",
                  languages=["en-US", "es-US"], language_switching="select")
  with pytest.raises(ValueError, match="single-agent only"):
    flows.validate_app(app)


def test_select_framework_in_sync(tmp_path):
  # extra _03 hooks must not trip the framework drift check.
  assert _bs.verify_app_dir(_emit(_select_app(), tmp_path)).ok


# --- the language-detection heuristic used by the nudge ----------------------
def test_response_language_ok_heuristic():
  from flows.authoring import language as _lang
  # clearly English while locked to Spanish -> mismatch (nudge).
  assert _lang._response_language_ok("What is your order number please?", "es-US") is False
  # Spanish text -> ok.
  assert _lang._response_language_ok("Cual es su numero de pedido por favor", "es-US") is True
  # target diacritics -> ok.
  assert _lang._response_language_ok("¿Cuál es su número?", "es-US") is True
  # too short to judge -> fail open (no nudge).
  assert _lang._response_language_ok("Okay", "es-US") is True
  # base language (English) is never enforced.
  assert _lang._response_language_ok("What is your order number?", "en-US") is True
  # French lock: English reply -> mismatch; French reply -> ok.
  assert _lang._response_language_ok("What is your order number please?", "fr-CA") is False
  assert _lang._response_language_ok("Quel est votre numero de commande", "fr-CA") is True


def test_lang_family_generalizes_to_any_locale():
  # Regression (PR #430 review): _lang_family must normalize ANY locale/name, not
  # just es/fr/en, so a configured German/Italian locale does not resolve to "".
  from flows.authoring import language as _lang
  assert _lang._lang_family("de-DE") == "de"
  assert _lang._lang_family("German") == "de"
  assert _lang._lang_family("it-IT") == "it"
  assert _lang._lang_family("es-US") == "es" and _lang._lang_family("Spanish") == "es"
  assert _lang._lang_family("fr-CA") == "fr" and _lang._lang_family("French") == "fr"
  assert _lang._lang_family("") == ""


def test_response_language_ok_no_crash_for_marker_less_language():
  # Regression (PR #430 review): a language without _LANG_MARKERS must fail-open,
  # not KeyError.
  from flows.authoring import language as _lang
  assert _lang._response_language_ok("Ihre Bestellnummer lautet wie folgt", "de-DE") is True
  assert _lang._response_language_ok("qualsiasi frase in italiano qui", "it-IT") is True


def test_emitted_hooks_are_self_contained():
  # Regression (PR #430): the emitted hooks inline _lang_family, so every name it
  # uses (LOCALE_NAMES) must be inlined too, else the callback NameErrors at runtime
  # (surfaces as the "Hmm, I'm having trouble" crash-envelope). exec the sources.
  from flows.authoring import language as _lang
  before = _lang.gen_language_before_model(["en-US", "es-US", "de-DE"], "en-US")
  after = _lang.gen_language_after_model()
  for src in (before, after):
    ns: dict = {}
    exec(compile(src, "<hook>", "exec"), ns)  # must not raise (all names resolve)
    assert ns["_lang_family"]("de-DE") == "de"  # references inlined LOCALE_NAMES
  # the after hook's heuristic must also run without KeyError for a marker-less lang
  ns2: dict = {}
  exec(compile(after, "<hook>", "exec"), ns2)
  assert ns2["_response_language_ok"]("irgendein satz auf deutsch hier", "de-DE") is True


def test_select_maps_non_es_fr_en_language(tmp_path):
  # A German/Italian select app builds, and the before_model hook maps de -> German
  # (not the default), so selecting German locks German, not English.
  app = flows.App(root_flow=_solo(), app_display_name="DE Demo",
                  languages=["en-US", "de-DE"], default_language="en-US",
                  language_switching="select")
  errors, _w = flows.validate_app(app)
  assert errors == [], errors
  out = _emit(app, tmp_path)
  hook = _read(out, "agents", "Solo_Agent",
               "before_model_callbacks", "before_model_callbacks_03", "python_code.py")
  assert "'de': 'German'" in hook or '"de": "German"' in hook
