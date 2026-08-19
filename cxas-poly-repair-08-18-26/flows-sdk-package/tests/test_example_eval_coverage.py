"""Release-blocking coverage gate for the example regression evals.

Separate from `test_example_evals.py` (which grades the evals that exist), this asserts that
evals EXIST for every example — so a NEW example cannot ship without one.

Enforced immediately (these pass today):
  * every discovered `examples/<name>.py` app is listed in `registry.yaml`
    -> a new example with no registry entry FAILS the build (the core requirement);
  * no orphan eval files (an eval or registry entry with no matching example).

Warn-only for now, flipped to hard-fail once the offline evals are authored (see the plan's
rollout). Set FLOWS_EVAL_COVERAGE_ENFORCE=1 to enforce locally, or flip `_ENFORCE_FILES`:
  * every offline/both example has `examples/evals/<name>.eval.yaml`;
  * that file has >=1 positive AND >=1 negative scenario;
  * every live/both example has an `evals_live/<name>.live.yaml` spec.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_example_eval_coverage.py
"""

from __future__ import annotations

import os
import sys
import warnings

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evals import harness as H  # noqa: E402

_PKG_ROOT = H._PKG_ROOT
_EVALS_DIR = H._EVALS_DIR
_LIVE_DIR = os.path.join(_PKG_ROOT, "evals_live")

# Offline coverage is COMPLETE (every offline example has a both-polarity eval), so it is
# hard-enforced now: a new offline example without an eval blocks the release. Live specs are
# not authored yet, so the live-coverage check stays warn-only until they exist (flip
# _ENFORCE_LIVE, or export FLOWS_EVAL_COVERAGE_ENFORCE=1, once they are).
_ENFORCE_OFFLINE = os.environ.get("FLOWS_EVAL_COVERAGE_ENFORCE", "1") == "1"
_ENFORCE_LIVE = os.environ.get("FLOWS_EVAL_COVERAGE_ENFORCE") == "1"

_OFFLINE_TIERS = {"offline", "both"}
_LIVE_TIERS = {"live", "both"}
_VALID_TIERS = {"offline", "live", "both"}


def _registry() -> dict:
    with open(os.path.join(_EVALS_DIR, "registry.yaml"), encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("examples", {})


def _fail_or_warn(problems: list[str], enforce: bool) -> None:
    if not problems:
        return
    msg = f"{len(problems)} eval-coverage gap(s):\n  - " + "\n  - ".join(problems)
    if enforce:
        pytest.fail(msg)
    warnings.warn(f"[eval-coverage WARN-ONLY] {msg}", stacklevel=2)


# --- enforced now ------------------------------------------------------------


def test_every_example_is_registered() -> None:
    """A new example must be added to registry.yaml in the same PR, or the release is blocked."""
    registry = _registry()
    missing = [n for n in H.discover_app_examples() if n not in registry]
    assert not missing, (
        "examples missing from registry.yaml (add a {tier, why} entry):\n  - "
        + "\n  - ".join(missing))


def test_registry_entries_are_valid() -> None:
    """Every entry needs a valid tier AND a non-empty `why` (the template promises both)."""
    bad_tier = {n: (e or {}).get("tier") for n, e in _registry().items()
                if (e or {}).get("tier") not in _VALID_TIERS}
    assert not bad_tier, f"registry entries with an invalid tier: {bad_tier}"
    missing_why = [n for n, e in _registry().items()
                   if not str((e or {}).get("why", "")).strip()]
    assert not missing_why, f"registry entries missing a non-empty 'why': {missing_why}"


def test_no_orphan_registry_entries() -> None:
    """A registry entry (or eval file) with no matching example is dead weight — flag it."""
    examples = set(H.discover_app_examples())
    orphans = [n for n in _registry() if n not in examples]
    assert not orphans, f"registry lists examples that no longer exist: {orphans}"


def test_no_orphan_eval_files() -> None:
    examples = set(H.discover_app_examples())
    if not os.path.isdir(_EVALS_DIR):
        return
    orphans = [f for f in os.listdir(_EVALS_DIR)
               if f.endswith(".eval.yaml") and f[: -len(".eval.yaml")] not in examples]
    assert not orphans, f"eval files with no matching example: {orphans}"


# --- warn-only until offline evals are authored, then enforced ---------------


_POLARITIES = {"positive", "negative"}


def test_offline_examples_have_evals_with_both_polarities() -> None:
    registry = _registry()
    problems: list[str] = []
    for name in H.discover_app_examples():
        if (registry.get(name) or {}).get("tier") not in _OFFLINE_TIERS:
            continue
        path = os.path.join(_EVALS_DIR, f"{name}.eval.yaml")
        if not os.path.isfile(path):
            problems.append(f"{name}: no examples/evals/{name}.eval.yaml")
            continue
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
        # meta must match the file (catches copy-paste) and carry a claim.
        meta = spec.get("meta") or {}
        if meta.get("example") != name:
            problems.append(f"{name}: meta.example is {meta.get('example')!r}, expected {name!r}")
        if not str(meta.get("claim", "")).strip():
            problems.append(f"{name}: meta.claim is empty")
        scenarios = spec.get("scenarios") or []
        polarities = [s.get("polarity") for s in scenarios]
        typoed = sorted({p for p in polarities if p not in _POLARITIES})
        if typoed:
            problems.append(f"{name}: invalid polarity value(s) {typoed} (use positive/negative)")
        if "positive" not in polarities:
            problems.append(f"{name}: no positive scenario (need >=1)")
        if "negative" not in polarities:
            problems.append(f"{name}: no negative scenario (need >=1)")
    _fail_or_warn(problems, enforce=_ENFORCE_OFFLINE)


def test_live_examples_have_live_specs() -> None:
    registry = _registry()
    problems: list[str] = []
    for name in H.discover_app_examples():
        if (registry.get(name) or {}).get("tier") not in _LIVE_TIERS:
            continue
        path = os.path.join(_LIVE_DIR, f"{name}.live.yaml")
        if not os.path.isfile(path):
            problems.append(f"{name}: no evals_live/{name}.live.yaml")
            continue
        # A present-but-empty/malformed placeholder must not pass the gate.
        try:
            with open(path, encoding="utf-8") as fh:
                spec = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            problems.append(f"{name}: live spec does not parse ({exc})")
            continue
        if not (spec.get("scenarios") or []):
            problems.append(f"{name}: live spec has no scenarios")
    _fail_or_warn(problems, enforce=_ENFORCE_LIVE)
