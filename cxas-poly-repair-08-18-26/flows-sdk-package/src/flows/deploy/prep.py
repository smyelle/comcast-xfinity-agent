"""Prepare an emitted app for a `--overwrite` push, reconciling the author's
app-level settings with the live target's.

`cxas push --overwrite` replaces the whole app from the app-dir, so app-LEVEL
settings must be reconciled with a freshly pulled copy of the live target before
the push. This keeps OUR rootAgent, variableDeclarations (slot-filling), and the
generated agent/tools intact. Ported from the proven per-agent deploy_prep.

THE RULE: **declared beats preserved; undeclared falls back to preserved.**

  * DECLARED — the author said it on their `flows.App` (`time_zone=`, `guardrails=`,
    `app_settings=`, `languages=`), so emit wrote it into app.json and recorded the
    key in `declared-settings.json`. The merge leaves it alone: the source is the
    truth, and a live target that disagrees is drift the deploy is there to correct.
  * UNDECLARED — nobody said it, so the live target's value is the only one there
    is. Merge it in (this is the whole reason `PRESERVE` exists: audioProcessingConfig
    / natural voice, the app's resource `name`, an ops-set logging bucket) and WARN,
    because that setting is now a property of the target rather than of the source.

The alternative — preserve always wins, which is what this did — reproduces the
incident it was meant to prevent: an author who writes `time_zone="America/New_York"`
and deploys onto an app sitting on the platform default gets the platform default,
silently, and four tools that compute a temporary-lift window off `current_date`
shift a caller's credit-file window by three hours' worth of date boundary.

The declared set comes from the app dir, not from an `App`: `flows deploy` is handed
a PATH. A dir with no `declared-settings.json` (an older SDK, a tree emitted by
something else) declares nothing and behaves exactly as it did before — preserve
everything, and no warning about it either, since there is no source to blame.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field

from ..authoring.integrity import brief, declared_setting_keys

# App-LEVEL settings carried over from the live app (everything except the agent
# graph, tools, and slot-filling variableDeclarations, which are ours).
PRESERVE = [
    "name", "displayName", "audioProcessingConfig", "loggingSettings", "guardrails",
    "modelSettings", "languageSettings", "timeZoneSettings", "toolExecutionMode",
    "errorHandlingSettings", "defaultChannelProfile",
]
COPY_DIRS = ["guardrails", "pythonEnvFiles"]

# Directories where the live target's copy is MERGED under ours rather than replacing
# it. `flows` can now emit guardrail resources, so a plain rmtree+copytree would delete
# every SDK-authored one on the way to the push and the app would deploy with whatever
# the console happened to have — silently, since a guardrail that is merely absent
# throws no error, it just never applies. Ours win per resource; the target's others are
# carried over untouched, so a console-authored guardrail is not collateral damage.
MERGE_DIRS = {"guardrails"}

# Settings whose value CHANGES WHAT THE AGENT DOES, as opposed to how the platform is
# wired around it. Taking one of these from the target instead of the source is the
# silent path that hurt us twice, so it is worth a line on the deploy log every time.
# (`name` is the target's resource id, `displayName` deliberately does not rename a
# live app, and `audioProcessingConfig` / `defaultChannelProfile` are tuned in the
# console by design — none of those are surprises worth shouting about.)
BEHAVIOURAL = frozenset({
    "timeZoneSettings", "guardrails", "loggingSettings", "modelSettings",
    "toolExecutionMode", "errorHandlingSettings",
})

# Behavioural settings where NEITHER side having one is itself the finding: a
# brand-new CES app has nothing to preserve, so the platform default silently
# applies. This is exactly how the migrated app came up on `America/Los_Angeles`
# with no guardrails at all. Value = what that default actually is.
WARN_WHEN_ABSENT = {
    "timeZoneSettings": "a fresh app defaults to America/Los_Angeles",
    "guardrails": "a fresh app runs with none — no safety, no prompt guardrail",
}

# What an author would write to take ownership of each behavioural setting.
DECLARE_WITH = {
    "timeZoneSettings": 'flows.App(time_zone="America/New_York")',
    "guardrails": 'flows.App(guardrails=["Default Safety Guardrail"])',
    "languageSettings": "flows.App(languages=[...])",
}
_APP_SETTINGS_HINT = "flows.App(app_settings={{{key!r}: ...}})"

# `modelSettings` is the one behavioural setting with NO declaration available, and
# saying so is better than pointing at a field that rejects it. `App.model` always
# has a value, so flows cannot tell a chosen model from an untouched default —
# honouring it as a declaration would silently re-model every existing deployment
# on its next deploy. So the target keeps winning, and the log says which model won.
_ADVICE = {
    "modelSettings": (
        "`App.model` is deliberately NOT a declaration — it always has a default, so "
        "honoring it would re-model every existing deployment on its next push. "
        "Change the model on the target app, or push with --no-preserve."),
}


def _declare_hint(key: str) -> str:
  """The closing sentence of a warning: how to stop depending on the target."""
  if key in _ADVICE:
    return _ADVICE[key]
  snippet = DECLARE_WITH.get(key) or _APP_SETTINGS_HINT.format(key=key)
  return f"Declare it: {snippet}."


@dataclass
class MergeReport:
  """What the merge did to each app-LEVEL setting, and what to worry about."""

  # Keys taken from the live target (the author declared none of these).
  preserved: list[str] = field(default_factory=list)
  # Keys the author declared, which the merge left alone.
  declared: list[str] = field(default_factory=list)
  # Declared keys where the live target DISAGREED — the deploy is changing the live
  # app to match the source. Informational, and the point of declaring.
  overridden: list[str] = field(default_factory=list)
  # Behavioural settings that came from the target, or from nowhere at all.
  warnings: list[str] = field(default_factory=list)

  def __iter__(self):
    """Back-compat: this used to return the preserved-key list."""
    return iter(self.preserved)


def _sub(parent: dict, key: str) -> dict:
  """`parent[key]` as a dict, replacing a non-dict (usually an explicit `null`).

  NOT `setdefault`. A pulled app.json is the live target's, and CES is content to hand
  back `"audioProcessingConfig": null` for a setting nobody has touched. `setdefault`
  only fills a MISSING key, so on a present-but-null one it returns `None` and the next
  line raises `AttributeError` — a deploy that dies on the console's default rather than
  on anything the author did.
  """
  value = parent.get(key)
  if not isinstance(value, dict):
    value = {}
    parent[key] = value
  return value


def _find_app_json(root: str) -> str:
  if os.path.isfile(os.path.join(root, "app.json")):
    return os.path.join(root, "app.json")
  for d in os.listdir(root):
    p = os.path.join(root, d, "app.json")
    if os.path.isfile(p):
      return p
  raise FileNotFoundError(f"no app.json under {root}")


def merge_live_settings(
    pulled_dir: str,
    built_dir: str,
    *,
    audio_bucket: str | None = None,
    inactivity_timeout: str | None = None,
    barge_in_awareness: bool | None = None,
    declared: list[str] | None = None,
) -> MergeReport:
  """Reconcile app-LEVEL settings between a pulled live app and the built app.

  Declared settings (see the module docstring) stay as the author emitted them;
  everything else in `PRESERVE` is merged in from the live target. Optionally
  enforces an audio-recording bucket (so `--overwrite` never drops call recording),
  an `inactivityTimeout` (drives the hold-and-wait countdown) and
  `bargeInAwareness` (whether the agent is TOLD what the caller heard before
  interrupting it — not whether the caller can interrupt).

  `declared` overrides the app dir's `declared-settings.json` — pass `[]` to force
  the old preserve-everything behaviour.
  """
  with open(_find_app_json(pulled_dir)) as f:
    live = json.load(f)
  built_path = os.path.join(built_dir, "app.json")
  if not os.path.isfile(built_path):
    raise FileNotFoundError(
        f"built app.json not found at {built_path} — emit the app dir before deploying")
  with open(built_path) as f:
    built = json.load(f)

  declared_keys = set(
      declared if declared is not None else declared_setting_keys(built_dir))
  rep = MergeReport()

  for k in PRESERVE:  # excludes rootAgent -> ours stays intact
    # languageSettings predates the sidecar: a built app that already has one was
    # authored with `App.languages`, so it is declared whether or not the dir was
    # emitted by an SDK new enough to say so.
    if k in declared_keys or (k == "languageSettings" and built.get(k)):
      rep.declared.append(k)
      if k in live and live[k] != built.get(k):
        rep.overridden.append(k)
      continue
    if k not in live:
      if k in WARN_WHEN_ABSENT and k not in built:
        rep.warnings.append(
            f"{k}: set by NEITHER your app nor the live target, so the CES platform "
            f"default decides it ({WARN_WHEN_ABSENT[k]}). {_declare_hint(k)}")
      continue
    was = built.get(k)
    built[k] = live[k]
    rep.preserved.append(k)
    if k in BEHAVIOURAL and live[k] != was:
      rep.warnings.append(
          f"{k}: the live target's {brief(live[k])} replaced your app's "
          f"{'nothing' if was is None else brief(was)} — this setting is a property "
          f"of the TARGET, not of your source. {_declare_hint(k)}")

  if audio_bucket:
    ls = _sub(built, "loggingSettings")
    _sub(ls, "audioRecordingConfig")["gcsBucket"] = audio_bucket
  if inactivity_timeout:
    apc = _sub(built, "audioProcessingConfig")
    apc["inactivityTimeout"] = inactivity_timeout
  if barge_in_awareness is not None:
    # This flag does NOT decide whether the caller can interrupt — `disableBargeIn`
    # does, and on gemini-composite-v1 the cut happens either way (probe 162: an
    # interrupted turn ran 29.16s against a 44.56s control with NO audioProcessingConfig
    # at all). What it decides is whether the agent finds out: with it set, the next
    # user turn is prefixed with `<context>agent speaking was interrupted. user only
    # heard '<verbatim prefix>' ...</context>` (probe 161, same on flash-live).
    #
    # So leaving it unset is the dangerous default, not the safe one: the caller is cut
    # off mid-sentence and the model context still asserts the whole line was delivered.
    # Probe 79 read this flag as the enable; that was measured on flash-live and 162
    # corrects the generalization.
    _sub(_sub(built, "audioProcessingConfig"),
         "bargeInConfig")["bargeInAwareness"] = bool(barge_in_awareness)

  with open(built_path, "w") as f:
    json.dump(built, f, indent=2)

  live_root = os.path.dirname(_find_app_json(pulled_dir))
  for d in COPY_DIRS:
    src = os.path.join(live_root, d)
    if os.path.isdir(src):
      dst = os.path.join(built_dir, d)
      if d in MERGE_DIRS:
        _merge_resource_dir(src, dst)
      else:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
  if "guardrails" in declared_keys:
    rep.warnings.extend(_unresolved_guardrails(built_dir, built.get("guardrails")))
  return rep


def _merge_resource_dir(src: str, dst: str) -> None:
  """Copy the live target's resource sub-dirs in WITHOUT displacing ours.

  One sub-directory per resource, so the merge is per resource: a sub-dir we emitted
  stays exactly as emitted, and one only the target has is carried over. That keeps both
  halves true at once — an author who declares a guardrail in code gets theirs deployed,
  and one someone added in the console is not deleted by a deploy that never knew about
  it.
  """
  os.makedirs(dst, exist_ok=True)
  for entry in sorted(os.listdir(src)):
    target = os.path.join(dst, entry)
    if os.path.exists(target):
      continue  # ours — emitted from the App, and authoritative
    source = os.path.join(src, entry)
    if os.path.isdir(source):
      shutil.copytree(source, target)
    else:
      shutil.copy2(source, target)


def _guardrail_display_names(app_dir: str) -> set[str]:
  """The `displayName` of every `guardrails/<dir>/<dir>.json` resource in an app dir."""
  names: set[str] = set()
  root = os.path.join(app_dir, "guardrails")
  if not os.path.isdir(root):
    return names
  for entry in sorted(os.listdir(root)):
    path = os.path.join(root, entry, f"{entry}.json")
    if not os.path.isfile(path):
      continue
    try:
      with open(path) as f:
        names.add(json.load(f).get("displayName") or entry)
    except (OSError, ValueError):
      names.add(entry)
  return names


def _unresolved_guardrails(app_dir: str, declared: object) -> list[str]:
  """Declared guardrail names with no resource behind them.

  `app.json`'s `guardrails` is a list of DISPLAY NAMES; the resource that gives each one
  teeth lives in `guardrails/<Name>/<Name>.json`. `flows` now emits those for guardrails
  built with `flows.safety(...)` and friends, and the merge adds any the target has that
  we did not — so what is left over here is a name that resolves to nothing on EITHER
  side. That is not an error anywhere; it is a guardrail that simply never applies.
  """
  if not isinstance(declared, list) or not declared:
    return []
  have = _guardrail_display_names(app_dir)
  if not have:
    return [f"guardrails: {brief(declared)} declared, but the target has no "
            "guardrails/ resources at all — nothing will enforce them"]
  missing = [g for g in declared if isinstance(g, str) and g not in have]
  if not missing:
    return []
  return [f"guardrails: {brief(missing)} has no resource in the target's "
          f"guardrails/ (it has {brief(sorted(have))}) — a name with no resource "
          "behind it is a guardrail that never applies"]
