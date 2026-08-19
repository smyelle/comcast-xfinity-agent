"""App-LEVEL CES settings: declaring them, emitting them, and who wins at deploy.

The incident, twice. `flows.App` could not express `timeZoneSettings`, `guardrails`
or `loggingSettings`, so they survived a deploy only because `deploy/prep.PRESERVE`
merged them back from the LIVE target. A freshly created app has nothing to preserve:
ours came up on `America/Los_Angeles` against a source that ran `America/New_York`,
while four ported tools computed temporary-lift start/end dates off `current_date` —
a silently wrong window on a caller's credit file. The same app came up with no
guardrails where the source carried two. A second migration (Comcast) grew its own
`patch_app_json` carrying `toolExecutionMode` + `timeZoneSettings` back by hand.

So: declare them on the `App`, and settle the deploy question explicitly.

THE RULE — declared beats preserved; undeclared falls back to preserved. Four cases,
one test each:

  1. declared + the live target agrees   -> the app's value stands (no-op)
  2. declared + the live target DIFFERS  -> the app's value stands, target overridden
  3. undeclared + the live target has it -> preserved (as always), and WARNED about
  4. undeclared + the live target lacks  -> nothing, and warned: the platform default
                                            is about to decide it for you

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_app_settings.py
"""

from __future__ import annotations

import json
import os

import pytest

import flows
from flows.authoring import build as _build
from flows.authoring import integrity as _integrity
from flows.deploy.prep import merge_live_settings

EASTERN = "America/New_York"
PACIFIC = "America/Los_Angeles"
SAFETY = "Default Safety Guardrail"
PROMPT = "Default Prompt Guardrail"


def _flow(cid: str = "freeze") -> flows.Flow:
  f = flows.Flow(cid, root_agent=f"{cid.title()}_Agent")
  f.add(
      flows.user_slot("ssn_last4", "What are the last four digits of your SSN?"),
      flows.announce("done", ["All set."], end=True),
  )
  return f


# `barge_in_awareness` defaults to True, so every app now declares one setting. This file
# is about the DECLARATION MACHINERY -- what an author's choices produce, and how declared
# beats preserved at deploy -- so the helper pins it off and every "declares nothing" case
# below keeps testing what it was written to test. The default itself is contracted
# separately, in the barge-in block at the bottom of this file.
def _app(**kw) -> flows.App:
  kw.setdefault("barge_in_awareness", False)
  return flows.App(root_flow=_flow(), app_display_name="Security Freeze", **kw)


def _emit(app: flows.App, tmp_path, name: str = "app") -> str:
  out = str(tmp_path / name)
  res = flows.build_app(app, out)
  assert res.ok, res.validation.errors if res.validation else res.error
  return out


def _app_json(out: str) -> dict:
  return json.load(open(os.path.join(out, "app.json")))


def _pulled(tmp_path, **settings) -> str:
  """A stand-in for `cxas pull` of the live target: a dir with one app.json."""
  live = tmp_path / "pulled"
  live.mkdir(exist_ok=True)
  payload = {"name": "live-uuid", "displayName": "Live App", "rootAgent": "Live_Agent"}
  payload.update(settings)
  (live / "app.json").write_text(json.dumps(payload, indent=2))
  return str(live)


# --- the authoring surface ----------------------------------------------------
def test_declared_settings_are_the_app_json_shapes():
  app = _app(
      time_zone=EASTERN,
      guardrails=[SAFETY, PROMPT],
      app_settings={"loggingSettings": {
          "conversationLoggingSettings": {"retentionWindow": "31536000s"}}},
  )
  assert app.declared_settings == {
      "timeZoneSettings": {"timeZone": EASTERN},
      "guardrails": [SAFETY, PROMPT],
      "loggingSettings": {
          "conversationLoggingSettings": {"retentionWindow": "31536000s"}},
  }
  assert app.declared_setting_keys == [
      "timeZoneSettings", "guardrails", "loggingSettings"]


def test_an_app_that_declares_nothing_declares_nothing():
  app = _app()
  assert app.declared_settings == {}
  assert app.declared_setting_keys == []


def test_languages_count_as_a_declared_setting():
  """`languageSettings` is emitted by the language step, but it is just as authored."""
  app = _app(languages=["en-US", "es-US"], language_switching="explicit")
  assert app.declared_settings == {}  # not the app-settings step's to write
  assert app.declared_setting_keys == ["languageSettings"]


def test_a_mistyped_time_zone_is_rejected_at_authoring_time():
  """A wrong zone is exactly as silent as no zone — it just shifts every date."""
  with pytest.raises(ValueError, match="IANA tz database"):
    _app(time_zone="America/New_york")


def test_time_zone_must_be_a_non_empty_string():
  with pytest.raises(ValueError, match="non-empty IANA zone name"):
    _app(time_zone="  ")


def test_guardrails_must_be_a_list_not_a_bare_name():
  with pytest.raises(ValueError, match="must be a list"):
    _app(guardrails=SAFETY)


def test_empty_guardrails_declares_that_the_app_runs_with_none():
  """`[]` is a decision; `None` is "not mine". They must not be the same thing."""
  assert _app(guardrails=[]).declared_settings == {"guardrails": []}
  assert _app(guardrails=[]).declared_setting_keys == ["guardrails"]
  assert _app().declared_settings == {}


@pytest.mark.parametrize("key,hint", [
    ("modelSettings", "model="),
    ("languageSettings", "languages="),
    ("timeZoneSettings", "time_zone="),
    ("guardrails", "guardrails="),
    ("variableDeclarations", "variables="),
    ("rootAgent", "root_flow="),
])
def test_app_settings_rejects_what_the_emitter_owns(key, hint):
  """Two ways to say one thing means one of them silently loses at emit."""
  with pytest.raises(ValueError) as exc:
    _app(app_settings={key: {"whatever": True}})
  assert hint in str(exc.value)


# --- emit ---------------------------------------------------------------------
def test_declared_settings_land_in_app_json(tmp_path):
  out = _emit(_app(time_zone=EASTERN, guardrails=[SAFETY, PROMPT],
                   app_settings={"toolExecutionMode": "PARALLEL"}), tmp_path)
  aj = _app_json(out)
  assert aj["timeZoneSettings"] == {"timeZone": EASTERN}
  assert aj["guardrails"] == [SAFETY, PROMPT]
  assert aj["toolExecutionMode"] == "PARALLEL"


def test_declaring_nothing_emits_exactly_what_it_always_did(tmp_path):
  """Backwards compatibility, byte for byte: no new keys, no new files."""
  plain = _emit(_app(), tmp_path, "plain")
  assert list(_app_json(plain)) == [
      "name", "displayName", "rootAgent", "modelSettings", "variableDeclarations"]
  assert not os.path.exists(
      os.path.join(plain, _integrity.DECLARED_SETTINGS_FILE))
  assert sorted(os.listdir(plain)) == ["agents", "app.json", "gecx-config.json", "tools"]


def test_emit_records_which_settings_the_author_owns(tmp_path):
  out = _emit(_app(time_zone=EASTERN, languages=["en-US", "es-US"],
                   language_switching="explicit"), tmp_path)
  assert _integrity.declared_setting_keys(out) == [
      "timeZoneSettings", "languageSettings"]


def test_multi_agent_apps_declare_them_too(tmp_path):
  auth = flows.Agent("Auth_Agent", _flow("auth"))
  action = flows.Agent("Action_Agent", _flow("action"))
  host = flows.HostRouter("Freeze_Host", routes={"auth": auth, "action": action})
  app = flows.App(host=host, agents=[auth, action],
                  app_display_name="Security Freeze", time_zone=EASTERN,
                  guardrails=[SAFETY])
  out = _emit(app, tmp_path)
  assert _app_json(out)["timeZoneSettings"] == {"timeZone": EASTERN}
  # Built through `flows.App` directly rather than the pinned `_app` helper, so this one
  # also carries the default-on barge-in flag — which is the point: a multi-agent app
  # declares it too, on the host, not just single-agent apps.
  assert _integrity.declared_setting_keys(out) == [
      "timeZoneSettings", "guardrails", "audioProcessingConfig"]


# --- the integrity check ------------------------------------------------------
def test_emit_fails_when_a_declared_setting_never_lands(tmp_path, monkeypatch):
  """The asked-vs-landed gate now covers settings, not just variables and tools."""
  monkeypatch.setattr(_build, "_emit_app_settings", lambda *a, **k: None)
  with pytest.raises(_integrity.EmitIntegrityError) as exc:
    flows.build_app(_app(time_zone=EASTERN), str(tmp_path / "app"))
  assert "timeZoneSettings" in str(exc.value)
  assert not os.path.exists(str(tmp_path / "app"))


def test_emit_fails_when_a_declared_setting_lands_wrong(tmp_path, monkeypatch):
  real = _build._emit_app_settings

  def _wrong(out_dir, app):
    real(out_dir, app)
    path = os.path.join(out_dir, "app.json")
    aj = json.load(open(path))
    aj["timeZoneSettings"] = {"timeZone": PACIFIC}  # not what was asked for
    json.dump(aj, open(path, "w"), indent=2)

  monkeypatch.setattr(_build, "_emit_app_settings", _wrong)
  with pytest.raises(_integrity.EmitIntegrityError) as exc:
    flows.build_app(_app(time_zone=EASTERN), str(tmp_path / "app"))
  assert EASTERN in str(exc.value) and PACIFIC in str(exc.value)


def test_check_catches_a_declared_setting_edited_out_of_app_json(tmp_path):
  """`flows check` / `flows deploy` on a dir alone: the sidecar is the witness."""
  out = _emit(_app(time_zone=EASTERN), tmp_path)
  assert _integrity.verify_dir(out).ok
  path = os.path.join(out, "app.json")
  aj = json.load(open(path))
  del aj["timeZoneSettings"]
  json.dump(aj, open(path, "w"), indent=2)

  report = _integrity.verify_dir(out)
  assert not report.ok
  assert "timeZoneSettings" in report.summary()


def test_a_dir_with_no_sidecar_still_checks_clean(tmp_path):
  """Trees emitted before this existed declare nothing and are held to nothing."""
  out = _emit(_app(), tmp_path)
  assert _integrity.declared_setting_keys(out) == []
  assert _integrity.verify_dir(out).ok


# --- the deploy rule: declared beats preserved --------------------------------
def test_case1_declared_and_the_live_target_agrees(tmp_path):
  out = _emit(_app(time_zone=EASTERN), tmp_path)
  live = _pulled(tmp_path, timeZoneSettings={"timeZone": EASTERN})

  report = merge_live_settings(live, out)

  assert _app_json(out)["timeZoneSettings"] == {"timeZone": EASTERN}
  assert "timeZoneSettings" in report.declared
  assert "timeZoneSettings" not in report.preserved
  assert report.overridden == []  # nothing to correct
  assert not [w for w in report.warnings if w.startswith("timeZoneSettings")]


def test_case2_declared_and_the_live_target_differs(tmp_path):
  """THE regression. Preserve-always handed back the target's zone and said nothing."""
  out = _emit(_app(time_zone=EASTERN), tmp_path)
  live = _pulled(tmp_path, timeZoneSettings={"timeZone": PACIFIC})

  report = merge_live_settings(live, out)

  assert _app_json(out)["timeZoneSettings"] == {"timeZone": EASTERN}
  assert "timeZoneSettings" in report.declared
  assert "timeZoneSettings" in report.overridden
  assert "timeZoneSettings" not in report.preserved


def test_case3_undeclared_and_the_live_target_has_it(tmp_path):
  """Unchanged behaviour — plus the warning that says where the value came from."""
  out = _emit(_app(), tmp_path)
  live = _pulled(tmp_path, timeZoneSettings={"timeZone": PACIFIC})

  report = merge_live_settings(live, out)

  assert _app_json(out)["timeZoneSettings"] == {"timeZone": PACIFIC}
  assert "timeZoneSettings" in report.preserved
  assert "timeZoneSettings" not in report.declared
  warning = next(w for w in report.warnings if w.startswith("timeZoneSettings"))
  assert "property of the TARGET" in warning
  assert "flows.App(time_zone=" in warning


def test_case4_undeclared_and_the_live_target_has_none(tmp_path):
  """The fresh-app case that started all this: nobody set it, so CES decides."""
  out = _emit(_app(), tmp_path)
  live = _pulled(tmp_path)

  report = merge_live_settings(live, out)

  assert "timeZoneSettings" not in _app_json(out)
  assert "timeZoneSettings" not in report.preserved
  warnings = " ".join(report.warnings)
  assert "timeZoneSettings: set by NEITHER" in warnings
  assert "guardrails: set by NEITHER" in warnings


def test_declared_guardrails_survive_a_target_that_has_none(tmp_path):
  out = _emit(_app(guardrails=[SAFETY, PROMPT]), tmp_path)
  live = _pulled(tmp_path)

  report = merge_live_settings(live, out)

  assert _app_json(out)["guardrails"] == [SAFETY, PROMPT]
  assert "guardrails" in report.declared
  assert "guardrails: set by NEITHER" not in " ".join(report.warnings)


def test_declared_empty_guardrails_are_not_refilled_from_the_target(tmp_path):
  """`guardrails=[]` is a decision the deploy has to respect, not an empty slot."""
  out = _emit(_app(guardrails=[]), tmp_path)
  live = _pulled(tmp_path, guardrails=[SAFETY, PROMPT])

  report = merge_live_settings(live, out)

  assert _app_json(out)["guardrails"] == []
  assert "guardrails" in report.overridden


def test_a_guardrail_name_with_no_resource_is_warned_about(tmp_path):
  """A name the target cannot resolve is a guardrail that quietly never applies."""
  out = _emit(_app(guardrails=[SAFETY, "Equifax PII Guardrail"]), tmp_path)
  live = _pulled(tmp_path)
  gr = tmp_path / "pulled" / "guardrails" / "Default_Safety_Guardrail"
  gr.mkdir(parents=True)
  (gr / "Default_Safety_Guardrail.json").write_text(
      json.dumps({"name": "uuid", "displayName": SAFETY}))

  report = merge_live_settings(live, out)

  warning = next(w for w in report.warnings if "never applies" in w)
  assert "Equifax PII Guardrail" in warning
  assert SAFETY not in warning.split("has no resource")[0]


def test_every_warning_points_at_advice_that_actually_works(tmp_path):
  """A hint naming a field that REJECTS the key is worse than no hint at all.

  `app_settings` refuses anything an `App` field owns, so the generic
  "declare it in app_settings" advice is wrong for exactly those keys.
  """
  from flows.deploy import prep

  for key in prep.BEHAVIOURAL:
    hint = prep._declare_hint(key)
    snippet = f"app_settings={{{key!r}"
    if snippet not in hint:
      continue  # a typed field or a bespoke sentence — nothing to check
    _app(app_settings={key: {}})  # must be accepted, or the advice is a lie


def test_the_model_warning_does_not_send_you_to_a_field_that_rejects_it():
  """`App.model` cannot be a declaration (it always has a default), and the
  warning has to say so rather than point at `app_settings`."""
  from flows.deploy import prep

  advice = prep._declare_hint("modelSettings")
  assert "app_settings" not in advice
  assert "--no-preserve" in advice
  with pytest.raises(ValueError):  # ...because app_settings really does refuse it
    _app(app_settings={"modelSettings": {"model": "gemini-3-flash"}})


# --- backwards compatibility --------------------------------------------------
def test_an_undeclared_app_preserves_everything_exactly_as_before(tmp_path):
  """No sidecar, no declarations: the old merge, key for key."""
  out = _emit(_app(), tmp_path)
  live = _pulled(
      tmp_path,
      audioProcessingConfig={"inactivityTimeout": "12s"},
      loggingSettings={"audioRecordingConfig": {"gcsBucket": "gs://ops"}},
      guardrails=[SAFETY],
      timeZoneSettings={"timeZone": PACIFIC},
      toolExecutionMode="PARALLEL",
      errorHandlingSettings={"retry": 2},
      defaultChannelProfile="voice",
  )

  report = merge_live_settings(live, out)
  aj = _app_json(out)

  assert report.declared == []
  assert aj["name"] == "live-uuid" and aj["displayName"] == "Live App"
  assert aj["audioProcessingConfig"] == {"inactivityTimeout": "12s"}
  assert aj["guardrails"] == [SAFETY]
  assert aj["timeZoneSettings"] == {"timeZone": PACIFIC}
  assert aj["toolExecutionMode"] == "PARALLEL"
  # ...and OUR half is untouched.
  assert aj["rootAgent"] == "Freeze_Agent"
  assert any(v["name"] == "sm" for v in aj["variableDeclarations"])


def test_a_dir_emitted_before_the_sidecar_existed_preserves_everything(tmp_path):
  """An app dir from an older SDK: declared-settings.json simply isn't there."""
  out = _emit(_app(time_zone=EASTERN), tmp_path)
  os.remove(os.path.join(out, _integrity.DECLARED_SETTINGS_FILE))
  live = _pulled(tmp_path, timeZoneSettings={"timeZone": PACIFIC})

  report = merge_live_settings(live, out)

  assert _app_json(out)["timeZoneSettings"] == {"timeZone": PACIFIC}
  assert report.declared == []


def test_language_settings_keep_their_pre_sidecar_exception(tmp_path):
  """`App.languages` won over the live target before this existed; it still does."""
  out = _emit(_app(languages=["en-US", "es-US"], language_switching="explicit"),
              tmp_path)
  os.remove(os.path.join(out, _integrity.DECLARED_SETTINGS_FILE))
  live = _pulled(tmp_path, languageSettings={"defaultLanguageCode": "fr-CA"})

  report = merge_live_settings(live, out)

  assert _app_json(out)["languageSettings"]["defaultLanguageCode"] == "en-US"
  assert "languageSettings" in report.declared


def test_declared_can_be_forced_off(tmp_path):
  """`declared=[]` reproduces the old preserve-everything merge on any dir."""
  out = _emit(_app(time_zone=EASTERN), tmp_path)
  live = _pulled(tmp_path, timeZoneSettings={"timeZone": PACIFIC})

  merge_live_settings(live, out, declared=[])

  assert _app_json(out)["timeZoneSettings"] == {"timeZone": PACIFIC}


def test_audio_bucket_and_inactivity_timeout_still_apply(tmp_path):
  out = _emit(_app(), tmp_path)
  live = _pulled(tmp_path)

  merge_live_settings(live, out, audio_bucket="gs://calls",
                      inactivity_timeout="8s")
  aj = _app_json(out)

  assert aj["loggingSettings"]["audioRecordingConfig"]["gcsBucket"] == "gs://calls"
  assert aj["audioProcessingConfig"]["inactivityTimeout"] == "8s"


def test_the_report_still_iterates_as_the_preserved_key_list(tmp_path):
  """`merge_live_settings` used to return that list; callers that treat it as one
  keep working."""
  out = _emit(_app(), tmp_path)
  live = _pulled(tmp_path, toolExecutionMode="PARALLEL")

  report = merge_live_settings(live, out)

  assert list(report) == report.preserved
  assert "toolExecutionMode" in list(report)


# --- barge-in awareness: on unless the author says otherwise -------------------
# The flag does NOT decide whether the caller can interrupt (`disableBargeIn` does, and
# the platform cuts the agent off either way). It decides whether the agent is TOLD, and
# told what the caller actually heard. Measured live: ces-probes 161 / 162. Leaving it
# unset is the dangerous default, which is why it is on and why these are contract tests.
_BARGE_ON = {"audioProcessingConfig": {"bargeInConfig": {"bargeInAwareness": True}}}


def _plain(**kw) -> flows.App:
  """An app with NOTHING pinned — the shape a real author writes."""
  return flows.App(root_flow=_flow(), app_display_name="Security Freeze", **kw)


def test_barge_in_awareness_is_declared_by_default():
  app = _plain()
  assert app.declared_settings == _BARGE_ON
  assert app.declared_setting_keys == ["audioProcessingConfig"]


def test_barge_in_awareness_can_be_turned_off():
  """Off means the key is not declared at all, not declared false: an app that opts out
  keeps whatever the live target already had."""
  assert _plain(barge_in_awareness=False).declared_settings == {}


def test_barge_in_awareness_reaches_the_emitted_app_json(tmp_path):
  out = _emit(_plain(), tmp_path, "barge")
  assert _app_json(out)["audioProcessingConfig"] == _BARGE_ON["audioProcessingConfig"]
  assert _integrity.declared_setting_keys(out) == ["audioProcessingConfig"]


def test_an_authors_own_audio_config_survives_the_merge():
  """The author owns `audioProcessingConfig` for other reasons too (the inactivity
  timeout). Adding the flag must not take the key away from them."""
  app = _plain(app_settings={"audioProcessingConfig": {"inactivityTimeout": "8s"}})
  assert app.declared_settings == {"audioProcessingConfig": {
      "bargeInConfig": {"bargeInAwareness": True}, "inactivityTimeout": "8s"}}


def test_an_author_can_still_set_sibling_barge_in_keys():
  """`disableBargeIn` is the real on/off and is a different key; setting it must not
  clobber the awareness flag, nor be clobbered by it."""
  app = _plain(app_settings={
      "audioProcessingConfig": {"bargeInConfig": {"disableBargeIn": True}}})
  assert app.declared_settings["audioProcessingConfig"]["bargeInConfig"] == {
      "bargeInAwareness": True, "disableBargeIn": True}


def test_declared_beats_the_live_target_for_barge_in(tmp_path):
  """A target with the flag OFF must not silently switch a new deploy back off."""
  out = _emit(_plain(), tmp_path, "barge2")
  live = _pulled(tmp_path, audioProcessingConfig={
      "bargeInConfig": {"bargeInAwareness": False}, "inactivityTimeout": "12s"})

  report = merge_live_settings(live, out)

  assert _app_json(out)["audioProcessingConfig"][
      "bargeInConfig"]["bargeInAwareness"] is True
  assert "audioProcessingConfig" in report.declared
