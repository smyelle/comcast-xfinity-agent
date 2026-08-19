from pydantic import BaseModel, Field
from typing import Dict, Any, Literal

# Using Pydantic defines the expected structure of your state variables. This makes your code more reliable and easier to
# debug.
class ShipmentRequest(BaseModel):
  tracking_number: int = Field(description="eight digit tracking number", required=True)
  location: str = Field(description="Location like city name or state/province name. Ex:NY, Ontario, ON, Toronto etc")
  country: Literal["US","Canada"] = Field(description="One of US or Canada")

# Docstrings in tools are important because they are directly
# sent to the model as the description for the tool. You should
# think of docstrings as an extension of prompting. Clear and
# descriptive docstrings will yield higher quality tool
# selection from the model.
def get_shipment_info(shipment_request: ShipmentRequest) -> Dict[str, Any]:
  """
  Get shipment info for a provided shipment request containing tracking number, location and country

  Args:
    shipment_request: An ShipmentRequest object containing tracking_number, location and country

  Returns:
    A dictionary containing the shipment information
  """
  print(shipment_request)
  return {
    "agent_instruction": f"Say this VERBATIM `Your shipment with tracking number {shipment_request.tracking_number} is received at nearest shipment office. You will receive it soon`"
  }

  