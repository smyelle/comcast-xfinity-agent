"""Setter capturing a request to resume a previously paused flow.

FRAMEWORK CODE — shared across all agents using the slot-filling engine.

The LLM handles language (extracting which paused flow the user means);
the after_tool callback resolves the lookup deterministically against
``sm["_flow_state"]``. This tool only captures the structured request.

It cannot know which flows or slots exist (no config in scope), so it validates
SHAPE, not existence: identifiers are normalized (``instance_id`` coerced to the
int the lookup compares with ``==``, text trimmed), blank values are dropped so
the resolver sees "not supplied", and anything uncoercible comes back as this
tool's error dict rather than a raw exception.
"""

from typing import Any


def resume_flow(
    instance_id: int = 0,
    flow: str = "",
    slot_name: str = "",
    slot_value: str = "",
) -> dict[str, Any]:
  """Resume a previously paused flow.

  Call when the user wants to return to an earlier request that was set
  aside. Extract the identifying detail from what the user said:

    "go back to my <service> request"  -> flow="<service>"
    "go back to the one for <detail>"
        -> slot_name="<slot>", slot_value="<detail>"
    "what about my <detail> <service> request"
        -> flow="<service>", slot_name="<slot>", slot_value="<detail>"
    "resume my other request"  (exactly one paused) -> no args

  Prefer instance_id when the paused-flows list gives you one.

  Args:
    instance_id: The paused flow's instance id, if known (a whole number).
    flow: The flow type to resume (e.g. when the user names the service).
    slot_name: Name of a slot to match against paused flows.
    slot_value: Substring to match within that slot's value.

  Returns:
    Dict capturing the resume request for the engine to resolve, or error=True
    with error_code "invalid_instance_id" / "invalid_<param>" when an argument
    cannot be used as an identifier.
  """
  result: dict[str, Any] = {"resume_request": True}
  # The resolver matches ids with `==` against real ints, so a stringy "7" would
  # silently never match. Coerce; absent/blank/0 means "not supplied".
  if instance_id not in (None, "", 0):
    try:
      parsed = int(instance_id)
    except (TypeError, ValueError):
      return {"error": True, "error_code": "invalid_instance_id"}
    if parsed < 0:
      return {"error": True, "error_code": "invalid_instance_id"}
    if parsed:
      result["instance_id"] = parsed
  for key, raw in (("flow", flow), ("slot_name", slot_name),
                   ("slot_value", slot_value)):
    if not raw:
      continue
    # `str(["a"])` would capture the literal "['a']" as an identifier.
    if isinstance(raw, (list, tuple, set, dict)):
      return {"error": True, "error_code": f"invalid_{key}"}
    text = str(raw).strip()
    if text:
      result[key] = text
  return result
