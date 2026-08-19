"""`flows.__all__` is a PROMISE: every name on it must actually resolve.

The authoring surface is exposed lazily (PEP 562 `__getattr__` over `_LAZY`), so
`__all__` and the table that backs it are two lists a human keeps in step — and one
drifted. `"emit"` sat in `__all__` with no `_LAZY` entry, which does not fail loudly:

  * `import flows; flows.emit` raised `AttributeError` (nothing to fall back to);
  * `from flows import *` SUCCEEDED and bound `emit` to the `flows.emit` SUBPACKAGE,
    because star-import falls back to importing an unresolvable `__all__` name as a
    submodule. `emit(app)` then failed with "module object is not callable" — a
    different error, in the caller's code, one import later.

Either way the intended name was `build_app`. The checks below are the invariant, not
that one name: they run in a SUBPROCESS because an in-process check is worthless here
— once any test has imported `flows.emit.scaffold`, the import system binds `emit`
onto the parent package and `getattr(flows, "emit")` starts passing.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_public_surface.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import flows

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(flows.__file__)))

_PROBE = """
import json, types
import flows

unresolved, modules = [], []
for name in flows.__all__:
  try:
    value = getattr(flows, name)
  except AttributeError as exc:
    unresolved.append([name, str(exc)])
    continue
  if isinstance(value, types.ModuleType):
    modules.append([name, value.__name__])
print(json.dumps({"unresolved": unresolved, "modules": modules,
                  "all": list(flows.__all__), "lazy": sorted(flows._LAZY)}))
"""


def _probe() -> dict:
  """`import flows` in a clean interpreter and report on every `__all__` name."""
  env = dict(os.environ, PYTHONPATH=_SRC)
  out = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True,
                       text=True, env=env, check=True, cwd=os.path.dirname(_SRC))
  return json.loads(out.stdout.strip().splitlines()[-1])


def test_every_public_name_resolves():
  """No name in `__all__` may raise on a bare `import flows`."""
  assert _probe()["unresolved"] == []


def test_no_public_name_silently_resolves_to_a_submodule():
  """A name in `__all__` must be an EXPORT, not a subpackage it happens to shadow.

  `blessed_source` is the one intentional module export. Anything else arriving as a
  module means `__all__` named something `_LAZY` does not provide and the import
  system supplied a same-named subpackage instead — which is how `emit` (the
  `flows.emit` scaffold/models package) stood in for `flows.authoring.build.emit`.
  """
  assert _probe()["modules"] == [["blessed_source", "flows.engine.blessed_source"]]


def test_lazy_table_covers_every_lazy_public_name():
  """The structural half: `__all__` minus the eagerly-bound names IS `_LAZY`'s keys.

  Cheaper than the resolution checks and it points straight at the missing row.
  """
  probe = _probe()
  eager = {"version", "blessed_source"}  # defined in flows/__init__.py itself
  assert sorted(set(probe["all"]) - eager - set(probe["lazy"])) == []


def test_build_app_is_the_exported_build_entry_point():
  """...and it is the authoring function, not the `flows.emit` package."""
  from flows.authoring import build as _build

  assert "build_app" in flows.__all__
  assert "emit" not in flows.__all__
  assert flows.build_app is _build.emit
