"""A 2-agent HostRouter with explicit, order-preserving `route_cues`.

Demos `HostRouter.route_cues` (feature 12): a map of flow key -> its spoken cue phrases
(the phrasings that mean "I want THIS route"). It is threaded VERBATIM and
ORDER-PRESERVING by the build layer and takes PRECEDENCE over alias-derived cues. Runtime
matching is earliest-utterance-position wins; the dict/list order is only the same-offset
tiebreak, so it is NEVER sorted — authored order is the tiebreak contract.

The host uses the engine strategy so the synthesized router config carries the merged
`route_cues` (see `_router_config_for` in build.py). `_show_route_cues()` prints the
emitted router config's `route_cues` to prove they are threaded verbatim + in order.

Build + validate offline:
    python -m examples.host_route_cues        # emits ./host_route_cues_app
"""

from pydantic import BaseModel, Field

import flows


class ReservationResult(BaseModel):
  confirmation_number: str = Field(description="The booking confirmation number")
  success: bool = True


@flows.tool(flow="reservation")
def book_reservation(party_size: str, reservation_time: str) -> ReservationResult:
  """Book a table and return a confirmation number."""
  return ReservationResult(confirmation_number="RES-4821")


class TakeoutResult(BaseModel):
  order_number: str = Field(description="The takeout order number")
  success: bool = True


@flows.tool(flow="takeout")
def place_takeout_order(items: str, pickup_time: str) -> TakeoutResult:
  """Place a takeout order and return an order number."""
  return TakeoutResult(order_number="TKO-2213")


# --- Reservation specialist --------------------------------------------------
reservation = flows.Flow("reservation", root_agent="Reservation_Agent")
reservation.add(
    flows.user_slot("party_size", "How many people are in your party?"),
    flows.user_slot("reservation_time", "What time would you like?"),
    flows.result_slot("confirmation_number", "book"),
)
reservation.task("book", "book_reservation", ["party_size", "reservation_time"],
                 "confirmation_number", out_key="confirmation_number", terminal=True,
                 then_say="You're all set — confirmation {confirmation_number}.",
                 condition=flows.has("reservation_time"))

# --- Takeout specialist ------------------------------------------------------
takeout = flows.Flow("takeout", root_agent="Takeout_Agent")
takeout.add(
    flows.user_slot("items", "What would you like to order?"),
    flows.user_slot("pickup_time", "What time will you pick it up?"),
    flows.result_slot("order_number", "order"),
)
takeout.task("order", "place_takeout_order", ["items", "pickup_time"],
             "order_number", out_key="order_number", terminal=True,
             then_say="Order {order_number} will be ready then.",
             condition=flows.has("pickup_time"))

reservation_agent = flows.Agent("Reservation_Agent", flow=reservation)
takeout_agent = flows.Agent("Takeout_Agent", flow=takeout)

# Explicit route_cues: each flow key -> its spoken cues, in authored order. These OVERRIDE
# any alias-derived cues and are threaded verbatim (never sorted).
host = flows.HostRouter(
    "Bistro_Host",
    routes={"reservation": reservation_agent, "takeout": takeout_agent},
    strategy="engine",
    route_cues={
        "reservation": ["book a table", "reserve a table", "dine in", "sit down"],
        "takeout": ["takeout", "to go", "pickup order", "carry out"],
    },
    welcome_message="Thanks for calling Bella Bistro. How can I help?",
)

app = flows.App(
    host=host,
    agents=[reservation_agent, takeout_agent],
    app_display_name="Bistro Host (route_cues)",
    model="gemini-3.5-flash",
)


def _show_route_cues() -> None:
  """Print the emitted router config's route_cues to prove verbatim + ordered threading."""
  from flows.authoring.build import _router_config_for

  _cid, cfg = _router_config_for(host)
  print(f"  router route_cues (verbatim, ordered): {list(cfg['route_cues'].items())}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  assert errors == [], errors
  _show_route_cues()
  flows.build_app(app, "./host_route_cues_app", overwrite=True)
  print("built -> ./host_route_cues_app "
        "(proves: HostRouter.route_cues threaded verbatim + order-preserving)")
