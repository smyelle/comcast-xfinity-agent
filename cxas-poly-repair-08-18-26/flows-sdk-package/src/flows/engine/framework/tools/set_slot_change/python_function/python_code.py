"""Flag which value(s) the user wants to change OR newly provide.

FRAMEWORK CODE -- shared across all agents using the slot-filling engine.

This is an INTENT signal only: it names which slot(s) the user wants to set —
either correcting one already given OR volunteering a brand-new detail out of
order — NOT the new values. The framework then runs a focused follow-up (showing
only those slots' setters) and asks the model for the new value(s), so the typed
setter does the parsing + validation and nothing else gets dropped.

The parameter schema (an array of slot names) is declared with a pydantic Field
so the model-facing description lives in code and is generated straight into the
function-calling schema — no separate tool description to keep in sync.
"""

from typing import Annotated, Any

from pydantic import Field


def set_slot_change(
    slots: Annotated[
        list[str],
        Field(
            description=(
                "Slot name(s) the user wants to set — either CORRECTING a "
                "value already given OR ADDING a brand-new detail volunteered "
                "out of order (works for slots not yet provided too). Slot "
                "names ONLY, never the values."
            )
        ),
    ],
) -> dict[str, Any]:
  """Flag which value(s) the user wants to change OR newly provide.

  Returns:
    Dict with success=True and the cleaned slot names, or error=True with
    error_code "missing_slots" (nothing usable given) / "invalid_slots" (not a
    list of slot-name strings). A malformed call must come back as this error
    dict, never as a raw exception — CES surfaces a tool traceback as a turn
    failure. Whether the names EXIST is the engine's call: no config is in
    scope here.
  """
  # Schema is list[str], but tolerate a model that emits a comma-joined string.
  if slots is None:
    raw: Any = []
  elif isinstance(slots, str):
    raw = slots.split(",")
  elif isinstance(slots, (list, tuple)):
    raw = slots
  else:
    # A dict, a bare int, ... — iterating a dict would silently keep its KEYS.
    return {"error": True, "error_code": "invalid_slots"}
  if any(not isinstance(s, str) for s in raw):
    return {"error": True, "error_code": "invalid_slots"}
  names = [s.strip() for s in raw if s and s.strip()]
  if not names:
    return {"error": True, "error_code": "missing_slots"}
  return {"success": True, "slots": names}
