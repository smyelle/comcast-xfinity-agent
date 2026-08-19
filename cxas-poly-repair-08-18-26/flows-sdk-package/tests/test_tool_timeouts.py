"""A raw-source tool body needs a way to say how long it takes.

`@flows.tool(timeout=...)` is the route for a DECORATED tool. An app that hands the
builder a body as SOURCE — `App(tool_bodies=...)`, the escape hatch for a tool grafted
from another app or generated at build time — had no route at all, and so silently took
the platform's 60s default.

That default is not generous for the tools most likely to be written as raw source. The
case this came from wraps a diagnostic fan-out measured at 19.6s whose every leg retries
with exponential backoff; a bad-but-recoverable window stacks retries on top of an already
slow call. And on an ASYNCHRONOUS body the kill is quiet — nothing reports, so the wait
runs to `awaits.max_turns` and the caller hears a timeout line for a backend that was only
slow.

`App.tool_timeouts` is the escape hatch matching `App.async_tools`, exactly as
`tool_bodies` matches `@flows.tool`.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

import flows

_BODY = '''"""A slow tool written as raw source."""


def slow_lookup(account: str = "") -> dict:
  """Look something up.

  Args:
    account: The account.

  Returns:
    The result.
  """
  return {"success": True, "found": account}
'''


def _emit(app) -> dict:
  with tempfile.TemporaryDirectory() as d:
    flows.build_app(app, d)
    path = os.path.join(d, "tools", "slow_lookup", "slow_lookup.json")
    with open(path) as fh:
      return json.load(fh)


def _app(**kw):
  f = flows.Flow("probe", root_agent="Agent")
  f.add(flows.user_slot("account", ask="Account?"),
        flows.result_slot("found", "Lookup"))
  f.task("Lookup", "slow_lookup", ["account"], "found", out_key="success",
         terminal=True, then_say="Found {found}.")
  return flows.App(root_flow=f, app_display_name="t",
                   tool_bodies={"slow_lookup": _BODY}, **kw)


def test_without_a_declared_timeout_the_resource_says_nothing():
  """Absent means the platform default. Pinned so the feature cannot start emitting a
  timeout onto apps that never asked for one."""
  assert "timeout" not in _emit(_app())


def test_a_declared_timeout_reaches_the_tool_resource():
  assert _emit(_app(tool_timeouts={"slow_lookup": 180})).get("timeout") == "180s"


def test_a_timeout_for_an_unknown_tool_is_simply_unused():
  """Naming a tool the app does not emit is a typo, not a crash — and the build should
  not fail on it, because the same mapping may be shared across several apps."""
  emitted = _emit(_app(tool_timeouts={"slow_lookup": 90, "not_a_tool": 30}))
  assert emitted.get("timeout") == "90s"


def test_the_decorator_still_wins_for_a_decorated_tool():
  """`App.tool_timeouts` is the escape hatch for bodies that cannot use the decorator; a
  decorated tool keeps saying what it always said, so adding the field cannot silently
  change an existing app.

  The registry is PATCHED rather than described. An earlier version of this test built
  the registered value into a local and asserted the result was `in (45, 5)` — with an
  empty registry that is satisfied by the app's own 5, so it passed while proving the
  opposite of its name.
  """
  from unittest import mock

  from flows.authoring import build as _build
  with mock.patch("flows.authoring.tools.registered_tool_timeouts",
                  return_value={"decorated": 45}):
    got = _build._tool_timeout_map(None, _app(tool_timeouts={"decorated": 5}))
  assert got.get("decorated") == 45, "the app's value overrode the decorator's"


def test_a_nonsense_timeout_is_refused_where_it_was_written():
  """A timeout is emitted as the string `"<n>s"`, so a bad value does not fail at build
  — it deploys as `"-5s"` and the tool quietly keeps the default. `bool` is called out
  separately because it is an `int` in Python, and `True` would emit a 1s budget."""
  for bad in (0, -5, 1.5, True, "90", None):
    with pytest.raises(ValueError, match="positive whole number"):
      _app(tool_timeouts={"slow_lookup": bad})
  with pytest.raises(ValueError, match="tool names"):
    _app(tool_timeouts={"": 90})
