"""Offline behavioural regression gate for the apps under `examples/`.

Every scenario in every `examples/evals/*.eval.yaml` (tier offline/both) is expanded to one
pytest case `<example>::<scenario>` and driven through the LLM-free engine simulator
(`tests/evals/harness.py`). A scenario that regresses turns its case RED and blocks the
release. ERROR (the harness could not run it — missing tool fake, unknown setter) also fails,
and is reported distinctly from a behavioural FAIL.

Fast + deterministic + offline (no LLM / no creds / no network). Runs in build-checks.yml.

Coverage (does every example HAVE an eval?) is a separate concern — see
`test_example_eval_coverage.py`.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_example_evals.py
"""

from __future__ import annotations

import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))          # tests/ on path
from evals import harness as H  # noqa: E402

_OFFLINE_TIERS = {"offline", "both"}


def _load_registry() -> dict:
    with open(os.path.join(H._EVALS_DIR, "registry.yaml"), encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("examples", {})


def _offline_specs() -> list[tuple[str, dict]]:
    """(example, scenario) for every offline/both example that has an eval file."""
    registry = _load_registry()
    out: list[tuple[str, dict]] = []
    for name in H.discover_app_examples():
        entry = registry.get(name) or {}
        if entry.get("tier") not in _OFFLINE_TIERS:
            continue
        path = os.path.join(H._EVALS_DIR, f"{name}.eval.yaml")
        if not os.path.isfile(path):
            continue                       # missing eval is the coverage gate's job, not ours
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
        for scenario in spec.get("scenarios") or []:
            out.append((name, scenario))
    return out


_SPECS = _offline_specs()
_IDS = [f"{name}::{scn.get('name', 'unnamed')}" for name, scn in _SPECS]


@pytest.mark.parametrize("example,scenario", _SPECS, ids=_IDS)
def test_example_eval_scenario(example: str, scenario: dict) -> None:
    app = H.load_app(example)
    result = H.run_scenario(app, scenario)
    if result.verdict == H.ERROR:
        pytest.fail(f"{example}::{scenario.get('name')} instrument ERROR:\n{result.summary()}")
    assert result.ok, f"{example}::{scenario.get('name')}\n{result.summary()}"


def test_at_least_one_offline_scenario_is_wired() -> None:
    """Guardrail: if this drops to zero, discovery/registry wiring broke (not a real green)."""
    assert _SPECS, "no offline eval scenarios discovered — check registry.yaml / evals dir"
