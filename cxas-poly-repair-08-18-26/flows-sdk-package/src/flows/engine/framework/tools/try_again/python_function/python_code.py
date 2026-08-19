"""No-op retry primitive for intent-first Pass A.

FRAMEWORK CODE -- shared across all agents using the slot-filling engine in
intent-first mode.

This tool does nothing. It exists only so the after_model callback can force
another model pass when the model failed to call classify_turn_intent in Pass A:
the callback returns a function_call to try_again, CES executes it (a no-op), and
the resulting re-invocation re-presents the classifier SI. The model is never
shown this tool; only the framework emits the call.
"""

from typing import Any


def try_again() -> dict[str, Any]:
  """Do nothing; its execution simply triggers another model pass."""
  return {"ok": True}
