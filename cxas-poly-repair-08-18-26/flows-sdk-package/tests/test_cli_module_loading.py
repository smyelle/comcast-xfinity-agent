"""`flows validate|emit <path>` on a PACKAGE-structured agent.

The documented workflow, the Makefile and CI all pass a path
(`flows validate src/my_agent/agent.py`). Loading that path with
`spec_from_file_location` gives the module no `__package__`, so the first
`from . import cues` in it dies with "attempted relative import with no known parent
package" — i.e. the CLI worked only for an agent that fits in ONE file. These tests
pin both shapes: a package (relative imports, src-layout) and a lone file.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_cli_module_loading.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

import flows as _flows

# Whatever `flows` this run imported — the subprocesses must exercise the SAME one.
SRC = os.path.dirname(os.path.dirname(os.path.abspath(_flows.__file__)))

# One tool, in its own module, imported RELATIVELY by agent.py — the thing that broke.
TOOLS_PY = '''\
"""Tool bodies, in their own module (the cxas-verizon/-equifax decomposition)."""

import flows


@flows.tool()
def lookup_order(order_id: str = "") -> dict:
  """Look an order up."""
  return {"success": True, "status": "shipped", "order_id": order_id}
'''

CUES_PY = '''\
"""Deterministic copy, in its own module."""

ASK_ORDER = "What's your order number?"
'''

AGENT_PY = '''\
"""The app module — imports its siblings RELATIVELY, like any package module."""

import flows

from . import cues
from .tools import lookup_order  # noqa: F401  (registers the @flows.tool body)

flow = flows.Flow("orders", root_agent="Orders_Agent")
flow.add(
    flows.user_slot("order_id", cues.ASK_ORDER),
    flows.result_slot("status", "lookup"),
    flows.announce("done", ["Your order is {status}."], requires=["status"], end=True),
)
flow.task("lookup", "lookup_order", ["order_id"], "status",
          condition=flows.has("order_id"))

app = flows.App(root_flow=flow, app_display_name="Orders", gcp_project="p")
'''

SINGLE_FILE_PY = AGENT_PY.replace(
    "from . import cues\nfrom .tools import lookup_order  # noqa: F401  "
    "(registers the @flows.tool body)\n",
    textwrap.dedent(
        '''\
        @flows.tool()
        def lookup_order(order_id: str = "") -> dict:
          """Look an order up."""
          return {"success": True, "status": "shipped", "order_id": order_id}


        class cues:
          ASK_ORDER = "What's your order number?"
        '''
    ),
)


@pytest.fixture()
def pkg_agent(tmp_path):
  """A src-layout package agent: `<tmp>/src/my_agent/{__init__,agent,tools,cues}.py`."""
  pkg = tmp_path / "src" / "my_agent"
  pkg.mkdir(parents=True)
  (pkg / "__init__.py").write_text("")
  (pkg / "cues.py").write_text(CUES_PY)
  (pkg / "tools.py").write_text(TOOLS_PY)
  (pkg / "agent.py").write_text(AGENT_PY)
  return str(pkg / "agent.py")


@pytest.fixture()
def flat_agent(tmp_path):
  """A single-file agent in a directory with no `__init__.py` (today's happy path)."""
  path = tmp_path / "solo" / "agent.py"
  path.parent.mkdir(parents=True)
  path.write_text(SINGLE_FILE_PY)
  return str(path)


def _run(*args: str) -> subprocess.CompletedProcess:
  """A FRESH process per invocation: module loading mutates sys.path/sys.modules, and
  the point of the test is what the CLI does on its own, not what the suite left."""
  env = dict(os.environ, PYTHONPATH=SRC)
  return subprocess.run(
      [sys.executable, "-m", "flows.cli", *args],
      capture_output=True, text=True, env=env, check=False,
  )


# --- the regression ----------------------------------------------------------
def test_validate_by_path_loads_a_package_structured_agent(pkg_agent):
  """REGRESSION: this died on `from . import cues` before the package-aware loader."""
  res = _run("validate", pkg_agent)
  assert "attempted relative import" not in res.stderr
  assert res.returncode == 0, res.stdout + res.stderr
  assert "validate: clean" in res.stdout


def test_emit_by_path_loads_a_package_structured_agent(pkg_agent, tmp_path):
  out = str(tmp_path / "built")
  res = _run("emit", pkg_agent, "--out", out)
  assert res.returncode == 0, res.stdout + res.stderr
  # The relatively-imported tool's BODY made it into the app, so the import really ran.
  body = os.path.join(out, "tools", "lookup_order", "python_function", "python_code.py")
  assert os.path.isfile(body)
  assert "def lookup_order(" in open(body).read()


def test_the_package_and_the_dotted_form_agree(pkg_agent):
  """The dotted form always worked; the two must now do the same thing."""
  by_path = _run("validate", pkg_agent)
  env = dict(os.environ, PYTHONPATH=os.pathsep.join(
      [SRC, os.path.dirname(os.path.dirname(pkg_agent))]))
  by_dots = subprocess.run(
      [sys.executable, "-m", "flows.cli", "validate", "my_agent.agent"],
      capture_output=True, text=True, env=env, check=False,
  )
  assert (by_path.returncode, by_path.stdout) == (by_dots.returncode, by_dots.stdout)


# --- no regression for the single-file shape ---------------------------------
def test_a_lone_file_still_loads_by_path(flat_agent):
  res = _run("validate", flat_agent)
  assert res.returncode == 0, res.stdout + res.stderr
  assert "validate: clean" in res.stdout


def test_package_root_detection(tmp_path):
  """The unit under it: walk up while there is an `__init__.py`, stop at the first
  directory without one (that is the path entry) — and `__init__.py` IS its package."""
  from flows.cli import _package_context

  pkg = tmp_path / "src" / "a" / "b"
  pkg.mkdir(parents=True)
  (tmp_path / "src" / "a" / "__init__.py").write_text("")
  (pkg / "__init__.py").write_text("")
  (pkg / "m.py").write_text("")

  assert _package_context(str(pkg / "m.py")) == (str(tmp_path / "src"), "a.b.m")
  assert _package_context(str(pkg / "__init__.py")) == (str(tmp_path / "src"), "a.b")
  # No `__init__.py` anywhere above -> no package, keep the by-path loader.
  lone = tmp_path / "lone.py"
  lone.write_text("")
  assert _package_context(str(lone)) == ("", "")
