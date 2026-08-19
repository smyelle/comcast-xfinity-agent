"""Deploy an emitted app to a live CES app via the `cxas` CLI (the [deploy] extra).

`deploy()` pulls the live target, reconciles app-level settings with the built app
(deploy_prep: declared beats preserved), then `cxas push --overwrite`. Requires
cxas-scrapi + GCP creds/ADC (install `flows[deploy]` and run from a creds-enabled
environment).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile

from .prep import merge_live_settings

DEFAULT_AUDIO_BUCKET = None
DEFAULT_INACTIVITY_TIMEOUT = "8s"  # drives the hold-and-wait countdown


def _run(argv: list[str]) -> str:
  proc = subprocess.run(argv, capture_output=True, text=True)
  if proc.returncode != 0:
    raise RuntimeError(
        f"command failed ({proc.returncode}): {' '.join(argv)}\n{proc.stdout}\n{proc.stderr}"
    )
  return proc.stdout


def deploy(
    app_dir: str,
    to: str,
    *,
    cxas: str = "cxas",
    preserve_from_target: bool = True,
    audio_bucket: str | None = DEFAULT_AUDIO_BUCKET,
    inactivity_timeout: str | None = DEFAULT_INACTIVITY_TIMEOUT,
    barge_in_awareness: bool | None = None,
    verify: bool = True,
) -> str:
  """Deploy `app_dir` to CES app resource `to` (--overwrite).

  When `preserve_from_target`, pulls `to` first and merges its app-level settings
  (natural voice, logging, ...) into `app_dir` so the overwrite doesn't strip them —
  except the ones the app's source DECLARED (`flows.App(time_zone=...)`,
  `guardrails=`, `app_settings=`, `languages=`), which win over the live target's.
  Anything behavioural that comes from the target instead of the source is logged as
  a WARN, because that is the setting nobody reviews. Returns the push stdout.

  `barge_in_awareness=True` writes `audioProcessingConfig.bargeInConfig`. The flag does
  NOT decide whether the caller can interrupt — `disableBargeIn` does, and on
  gemini-composite-v1 the speech is cut either way (probe 162: an interrupted turn ran
  29.16s against a 44.56s control with no `audioProcessingConfig` at all). What it
  decides is whether the AGENT is told, and told what the caller actually heard (probe
  161). An earlier reading of probe 79 had this backwards; that was measured on
  flash-live, and only its generalization is withdrawn.

  So leaving it unset is the lossy default, not the safe one. It matters most to a
  progressive fan-out, which holds the floor across several findings: an interrupted
  group's remaining lines are still generated into a stream nobody is receiving, so
  without the report they are LOST rather than deferred. Left unset (None) the target's
  own setting is untouched.

  `verify` (default on) re-checks the dir before the pull, because deploy is handed
  a PATH, not an emit: the tree may have come from an older SDK, a hand edit, or an
  emit that failed. An app that fails its integrity check is refused rather than
  pushed — the last gate before a half-built agent is live traffic.
  """
  if verify:
    from ..authoring.integrity import verify_dir

    report = verify_dir(app_dir)
    if not report.ok:
      raise RuntimeError(
          f"refusing to push {app_dir}: {report.summary()}. Re-emit it "
          "(`flows emit`), or pass --no-verify if you know better.")
  if preserve_from_target:
    tmp = tempfile.mkdtemp(prefix="flows_pull_")
    try:
      _run([cxas, "pull", to, "--target-dir", tmp, "--overwrite"])
      report = merge_live_settings(
          tmp, app_dir, audio_bucket=audio_bucket,
          inactivity_timeout=inactivity_timeout,
          barge_in_awareness=barge_in_awareness,
      )
      print(f"deploy: merged live settings {report.preserved}")
      if report.declared:
        note = (f" (overriding the target's {report.overridden})"
                if report.overridden else "")
        print(f"deploy: kept the app's own {report.declared}{note}")
      # The silent path: a behavioural setting the app never declared, so it comes
      # from whatever the target happens to carry. Loud, but not fatal — a deploy
      # onto a console-configured app is a legitimate way to work.
      for warning in report.warnings:
        print(f"deploy: WARN {warning}")
    finally:
      shutil.rmtree(tmp, ignore_errors=True)
  argv = [cxas, "push", "--app-dir", app_dir, "--to", to, "--overwrite"]
  print(f"deploy: {' '.join(argv)}")
  return _run(argv)
