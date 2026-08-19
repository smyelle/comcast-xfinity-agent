"""CI guarantee for the authoring-feature DEMOS under `examples/`.

For every app-building example this: imports the module (which must NOT build on import —
build is guarded under `__main__`), runs `flows.validate_app` and asserts NO errors, then
`flows.build_app` into a `TemporaryDirectory` and asserts the `ScaffoldResult.ok` AND that
a real CXAS agent was written (`app.json` + an `agents/` dir with a per-agent JSON). For
`render_source` it asserts the renderer round-trips byte-for-byte.

Fast + deterministic + offline (no LLM / no creds): every build targets a temp dir, so no
build output is left in the tree.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_examples.py
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import os
import sys
import tempfile

import pytest

import flows

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))          # tests/ on path
from evals import harness as _H  # noqa: E402

# Discovered dynamically (shared with the eval suite) so the two can never disagree about what
# an example is — and a new example is picked up here automatically, no hand-list to update.
_APP_EXAMPLES = _H.discover_app_examples()

_EXAMPLES_DIR = _H._EXAMPLES_DIR
_load = _H._load


def _assert_real_cxas_agent(out_dir: str) -> None:
  """A real CXAS app dir has an app.json and at least one agent under agents/."""
  assert os.path.isfile(os.path.join(out_dir, "app.json")), "missing app.json"
  agents_dir = os.path.join(out_dir, "agents")
  assert os.path.isdir(agents_dir), "missing agents/ dir"
  agent_dirs = [d for d in os.listdir(agents_dir)
                if os.path.isdir(os.path.join(agents_dir, d))]
  assert agent_dirs, "agents/ has no agent"
  # Each agent carries its own <name>.json (the CXAS agent definition).
  for d in agent_dirs:
    assert os.path.isfile(os.path.join(agents_dir, d, f"{d}.json")), f"missing {d}.json"


@pytest.mark.parametrize("name", _APP_EXAMPLES)
def test_example_builds_valid_cxas_agent(name: str) -> None:
  mod = _load(name)
  app = mod.app  # exposed at module scope; import did NOT build it
  errors, _warnings = flows.validate_app(app)
  assert errors == [], f"{name}: {errors}"
  with tempfile.TemporaryDirectory() as tmp:
    out = os.path.join(tmp, "app")
    res = flows.build_app(app, out, overwrite=True)
    assert res.ok, res.validation.errors if res.validation else res.error
    _assert_real_cxas_agent(out)


_DRIVERS = sorted(n[:-3] for n in os.listdir(_EXAMPLES_DIR) if n.endswith("_drive.py"))


@pytest.mark.parametrize("name", _DRIVERS)
def test_example_driver_still_runs(name: str) -> None:
  """A `*_drive.py` demo must actually run, not just have run once.

  These drive the real engine and their output is quoted verbatim in the docs and the
  VERIFY notes, so a driver that has rotted makes those transcripts fiction. Nothing
  covered them before: they are not apps, so `_APP_EXAMPLES` skips them, and nothing
  else imports them.
  """
  assert _DRIVERS, "no drivers discovered — the glob is wrong"
  # Drivers do `from examples.<demo> import app`, so they need the examples PACKAGE on
  # the path — `_load`'s by-path import gives the module a synthetic name and that
  # relative import would not resolve.
  pkg_root = os.path.dirname(_EXAMPLES_DIR)
  if pkg_root not in sys.path:
    sys.path.insert(0, pkg_root)
  mod = importlib.import_module(f"examples.{name}")
  main = getattr(mod, "main", None)
  assert callable(main), f"{name}: a driver must expose main()"
  with contextlib.redirect_stdout(io.StringIO()) as out:
    main()
  assert out.getvalue().strip(), f"{name}: driver printed nothing"


def test_render_source_round_trips() -> None:
  mod = _load("render_source")
  result = mod.build()
  # Order-sensitive equality — the renderer promises byte-for-byte, order preserved.
  assert list(result["round_tripped"].items()) == list(result["config"].items())
  # The non-builder slot fell back to a greppable raw({...}) in the generated source.
  assert "raw(" in result["source"]
