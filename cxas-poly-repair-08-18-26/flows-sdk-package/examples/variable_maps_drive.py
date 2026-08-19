"""Drive `examples/variable_maps` offline: seed a session, watch the flow skip ahead.

A variable map is easy to reason about wrongly, because the interesting behaviour is
what the flow does NOT say. This driver makes that visible: it runs the real ingress
callback the build emits, hands the resulting slot machine to the real engine, and
prints the first question each entry path actually reaches.

Nothing here is mocked. `ingress_source()` is the exact code that ships into the app,
compiled and called with the session state CES would hand it, and the engine is the
blessed one. What changes between runs is only which variables the session arrived
with.

    python -m examples.variable_maps_drive
"""

import json
import re

from flows.authoring import build as _build
from flows.authoring import variable_maps as _vm
from flows.engine import loader as fb

from examples.variable_maps import app

# The lowered table and the flow config, exactly as `build_app` would emit them.
_ALL_MAP, _, _ = _build._assemble(app)  # noqa: SLF001
CONFIG = _ALL_MAP[app.config_id]
TABLE = {cid: cfg["variable_maps"] for cid, cfg in _ALL_MAP.items()
         if cfg.get("variable_maps")}

_NS: dict = {}
exec(compile(_vm.ingress_source(), "ingress.py", "exec"), _NS)  # noqa: S102
_INGRESS = _NS["before_agent_callback"]


class _Ctx:
  def __init__(self, state):
    self.state = state


def _seed(variables):
  """Run the ingress callback over a session's variables; return (sm, chosen)."""
  state = {"variable_maps_by_config": json.dumps(TABLE),
           "default_config_id": app.config_id}
  state.update(variables)
  _INGRESS(_Ctx(state))
  sm = state.get("sm", {})
  return sm, sm.get("_variable_map", {}).get("name")


def _first_question(sm):
  """What the flow asks first, given what it already knows."""
  result = fb.run_engine(CONFIG, sm, last_user_text="my delivery hasn't turned up",
                         config_id=app.config_id, n_user_turns=1)
  si = result["action"].get("si") or ""
  found = re.search(r"<system_directive>(.*?)</system_directive>", si, re.S)
  if not found:
    return "(nothing to ask)"
  # The directive is the question plus a standing instruction to the model; the
  # first line is the question itself.
  return found.group(1).strip().splitlines()[0]


SCENARIOS = [
    ("handed over from the tracking page", {
        "parcel": {"tracking_id": "AC-40219"},
        "account_number": "A-1187",
    }),
    ("handed over from the account line", {"customer_ref": "A-1187"}),
    ("the placeholder an upstream writes while a backend is thinking", {
        "account_number": "AWAITING_SYNC",
    }),
    ("a cold call, nothing seeded", {}),
]


def main() -> None:
  for label, variables in SCENARIOS:
    sm, chosen = _seed(variables)
    filled = sm.get("filled", {})
    print(f"\n=== {label}")
    print(f"    variables : {variables or '(none)'}")
    print(f"    map chosen: {chosen or '(none matched)'}")
    print(f"    pre-filled: {filled or '(nothing)'}")
    question = _first_question(sm)
    # The engine hands back a directive; the caller-facing line is the tail of it.
    print(f"    asks first: {question.strip().splitlines()[-1][:96]}")


if __name__ == "__main__":
  main()
