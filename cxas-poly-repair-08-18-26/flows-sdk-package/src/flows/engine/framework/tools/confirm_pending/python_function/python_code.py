"""Tool to commit pending slot values after user confirms readback.

FRAMEWORK CODE — shared across all agents using the slot-filling engine.
Do not add agent-specific logic here; customize behavior
via the per-agent {config_id}_dag tool.
"""

from typing import Any


# The passive terminal control slots. They are staged into `pending` by
# cancel_flow / transfer_to_human, but COMMITTING them belongs to the engine
# (_handle_terminal_slots owns the "shall I go ahead?" gate): confirming a
# readback of ordinary data must never double as consent to tear the flow down.
# Held back in `pending` here so the engine still sees the request next pass.
# Mirrors _CONTROL_BLOCKS in slot_filling_engine (CES tools cannot import each
# other); a unit test asserts the two lists agree.
_CONTROL_BLOCKS = ("cancel", "escalate")


def confirm_pending() -> dict[str, Any]:
  """Commit all pending slot values to filled.

  Call when the user affirms the readback is correct.

  Returns:
    Dict with committed slot names and stored=True, or error=True if there is
    no ordinary pending value to commit.
  """
  sm = context.state["sm"]  # pylint: disable=undefined-variable
  pending = sm.get("pending") or {}
  committed = [name for name in pending if name not in _CONTROL_BLOCKS]
  if not committed:
    return {"error": True}

  # `filled` may not be seeded yet; indexing it raised KeyError mid-commit.
  filled = sm.setdefault("filled", {})
  for name in committed:
    filled[name] = pending[name]
  sm["pending"] = {
      name: value for name, value in pending.items()
      if name in _CONTROL_BLOCKS
  }
  sm["_readback_transition"] = True
  return {"committed": committed, "stored": True}
