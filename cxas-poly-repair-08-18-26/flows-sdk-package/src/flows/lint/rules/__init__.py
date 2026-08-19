"""Rule modules. Importing this package (via `load()`) registers every rule.

Each submodule defines `@rule`-decorated classes that register into
`flows.lint.registry.RULES` at import time. Keep the import list here in sync when
adding a module — a rule module that nobody imports never registers.
"""

from __future__ import annotations

_LOADED = False


def load() -> None:
  """Idempotently import every rule module so its rules register."""
  global _LOADED
  if _LOADED:
    return
  from . import blessed_adapter  # noqa: F401
  from . import voice            # noqa: F401
  # Phase 1 / Phase 2 rule modules are added here as they land:
  from . import wiring           # noqa: F401  (FLW*)
  from . import reachability     # noqa: F401  (FLR*)
  from . import model_reliance   # noqa: F401  (FLM*)
  from . import conversation     # noqa: F401  (FLC*)
  from . import toolcode         # noqa: F401  (FLW004, FLX002-FLX004)
  from . import exhaust          # noqa: F401  (FLW005)
  from . import readback         # noqa: F401  (FLC140)
  _LOADED = True
