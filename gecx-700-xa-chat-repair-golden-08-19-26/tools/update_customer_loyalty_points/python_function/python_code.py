# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long

"""PolySynth Tool function.

agent_action: this comment satisfies the T001 lint rule.
"""

# pylint: disable=undefined-variable

from typing import Any, Dict, Optional
import pydantic


# Using Pydantic defines the expected structure of your state variables. This makes your code more reliable and easier to
# debug.
class CustomerProfile(pydantic.BaseModel):
  email: Optional[str] = None
  loyalty_points: int = pydantic.Field(default=0, ge=0)  # Must be >= 0
  plan: str = "Standard"


# Docstrings in tools are important because they are directly
# sent to the model as the description for the tool. You should
# think of docstrings as an extension of prompting. Clear and
# descriptive docstrings will yield higher quality tool
# selection from the model.
def update_customer_loyalty_points(points_to_add: int) -> Dict[str, Any]:
  """Adds loyalty points to the customer's profile and returns the updated profile.

  Args:
    points_to_add: The number of loyalty points to add to the existing total.

  Returns:
    A dictionary containing the customer's updated profile information.
  """
  # 1. Get the current profile data from the session state.
  # The .get() method safely returns an empty dict if
  # 'customer_profile' doesn't exist.
  current_profile_data = context.state.get("customer_profile", {})

  # Other ways to fetch the 'customer_profile' are:
  # current_profile_data = context.variables.get("customer_profile", {})
  # current_profile_data = get_variable("customer_profile")

  # 2. Load the data into a Pydantic model for validation and easy access.
  profile = CustomerProfile(**current_profile_data)

  # 3. Perform the business logic.
  # Print statements can be used for debugging and will show
  # up in the tracing details.
  _audit_request = {
      "points_to_add": points_to_add,
  }
  print(
      "[AUDIT] [update_customer_loyalty_points] >>> Request Payload:",
      f" {_audit_request}",
  )
  profile.loyalty_points += points_to_add
  print(f"Updated loyalty points to: {profile.loyalty_points}")

  # 4. Save the updated data back into the session state.
  # .model_dump() converts the Pydantic model back to a
  # dictionary.
  context.state["customer_profile"] = profile.model_dump()

  # other ways to set the "customer_profile" are:
  # context.variables["customer_profile"] = profile.model_dump()
  # set_variable("customer_profile", profile.model_dump())

  _audit_response = context.state["customer_profile"]
  print(
      "[AUDIT] [update_customer_loyalty_points] <<< Response Payload:",
      f" {_audit_response}",
  )
  return _audit_response
