"""Cancel the current active flow."""


def cancel_flow(reason: str = "") -> dict[str, object]:
  """Cancel the current flow. Call when the user wants to stop or cancel."""
  return {"stored": True, "value": True, "cancelled": True, "reason": reason}
