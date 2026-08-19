"""Shared helpers for the toolset test suites (`test_openapi.py` + `test_mcp.py`).

Both suites exercise the same generated-wrapper machinery in `toolset_common`, so the
registry snapshot and the exec-the-wrapper runner live here rather than being copied into
each. The per-kind `_toolset` / `_app` builders stay in each suite — they differ by
resource kind. Not a `test_*` module, so pytest does not collect it.
"""

from __future__ import annotations

import types
from contextlib import contextmanager

from flows.authoring import tools as _tools


@contextmanager
def registry_snapshot():
  """Snapshot the tool registry and restore it afterwards.

  `api_tool` / `mcp_tool` register their generated wrappers process-wide, like
  `@flows.tool`, and other test modules register at IMPORT time (pytest imports every
  module before running any test). So snapshot-and-restore — not clear — is what keeps
  the suites independent without dropping registrations other tests still need.
  """
  saved = {k: v for k, v in _tools._REGISTRY.items()}
  try:
    yield
  finally:
    _tools._REGISTRY.clear()
    _tools._REGISTRY.update(saved)


def source(name: str) -> str:
  """The rendered source of a registered generated tool."""
  return _tools._REGISTRY[name].source


def run_wrapper(name, *, symbol, variables=None, live=None):
  """Exec a generated wrapper with the CES-injected `context` / `tools` globals faked.

  CES provides both at call time; nothing in the emitted module imports them, so this is
  the only way to find out whether the generated body actually runs. A declared mock is
  emitted as its OWN tool, so wire the real generated one rather than stubbing it —
  including CES's `{"result": ...}` envelope around a python tool's return, and the
  one-dict-of-named-arguments tool-to-tool calling convention.
  """
  def _boom(_request):
    raise AssertionError("the live path was taken when it should not have been")

  context = types.SimpleNamespace(variables=dict(variables or {}))
  toolsmod = types.SimpleNamespace(**{symbol: live or _boom})
  mocked = f"{name}_mock"
  if mocked in _tools._REGISTRY:
    mns: dict = {"context": context, "tools": toolsmod}
    exec(compile(source(mocked), f"{mocked}.py", "exec"), mns)  # noqa: S102
    setattr(toolsmod, mocked,
            lambda request, _fn=mns[mocked]: {"result": _fn(**request)})
  ns: dict = {"context": context, "tools": toolsmod}
  exec(compile(source(name), f"{name}.py", "exec"), ns)  # noqa: S102
  return ns[name]
