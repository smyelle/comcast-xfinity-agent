"""Live-drive regression tier for model-dependent examples (pre-release only).

The offline suite (`test_example_evals.py`) cannot prove model-decided routing, improvised
wording, real timeouts, or true concurrency — the simulator never calls an LLM and fakes the
clock. Those examples are marked `tier: live` in `examples/evals/registry.yaml`; this tier
deploys each one and drives it against the real CES runtime via `flows.drive.run_steps`,
grading the transcript with the same expectation vocabulary as the offline harness (limited to
what a live transcript exposes: `said_contains`, `tool_called`, ordering).

It is SKIPPED unless `FLOWS_LIVE_EVALS=1` (and GCP creds are present), and is marked `live` so
PR CI — which runs `-m "not live"` — never triggers a deploy. It runs in the pre-release
(publish) workflow. Live specs live in `packages/flows/evals_live/<name>.live.yaml`.

Run: FLOWS_LIVE_EVALS=1 PYTHONPATH=packages/flows/src pytest -m live \
        packages/flows/tests/test_example_evals_live.py
"""

from __future__ import annotations

import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evals import harness as H  # noqa: E402

_LIVE_DIR = os.path.join(H._PKG_ROOT, "evals_live")
_ENABLED = os.environ.get("FLOWS_LIVE_EVALS") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _ENABLED,
                       reason="live evals disabled (set FLOWS_LIVE_EVALS=1 + GCP creds)"),
]


def _live_specs() -> list[tuple[str, dict]]:
    if not os.path.isdir(_LIVE_DIR):
        return []
    out: list[tuple[str, dict]] = []
    for fname in sorted(os.listdir(_LIVE_DIR)):
        if not fname.endswith(".live.yaml"):
            continue
        name = fname[: -len(".live.yaml")]
        with open(os.path.join(_LIVE_DIR, fname), encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
        for scenario in spec.get("scenarios") or []:
            out.append((name, scenario))
    return out


_SPECS = _live_specs()
_IDS = [f"{name}::{scn.get('name', 'unnamed')}" for name, scn in _SPECS]


@pytest.mark.parametrize("example,scenario", _SPECS, ids=_IDS)
def test_example_live_scenario(example: str, scenario: dict) -> None:  # pragma: no cover - live
    # Deploy the example, drive it via flows.drive.run_steps, and grade the transcript.
    # Implemented alongside the first authored live specs; see the plan's Tier-2 section.
    pytest.skip("live-drive execution not yet wired for this spec")
