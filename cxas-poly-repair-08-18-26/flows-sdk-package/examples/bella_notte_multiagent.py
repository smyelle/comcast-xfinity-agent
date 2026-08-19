"""A bella_notte-equivalent multi-agent app, authored entirely from flows.App.

Reproduces the shipped cxas-scrapi `examples/bella_notte` topology — a
steering host that silently routes to two slot-filling specialists that can hand
off to each other mid-call — with NO hand-authoring:

    Bella_Notte_Host  (receptionist: greets, routes via set_active_flow, no engine)
      ├─ Reservation_Agent  (books a table)
      └─ Takeout_Agent      (places a takeout order)

The host emits `childAgents` + the two custom router callbacks; each specialist is a
full slot-filling agent (its `<config>_dag` + setters + engine + the 4 callbacks)
and carries `set_active_flow`, so "actually I'd like takeout" mid-reservation hops
to the sibling. `set_active_flow` (generated from `routes`) returns `target_agent`.

Build + validate offline:
    python -m examples.bella_notte_multiagent    # emits ./bella_notte_multiagent_app
"""

from pydantic import BaseModel, Field

import flows


class ReservationResult(BaseModel):
    confirmation_number: str = Field(description="The booking confirmation number")
    success: bool = True


@flows.tool(flow="reservation")
def book_reservation(party_size: str, reservation_time: str, guest_name: str) -> ReservationResult:
    """Book a table and return a confirmation number."""
    return ReservationResult(confirmation_number="RES-4821")


class TakeoutResult(BaseModel):
    order_number: str = Field(description="The takeout order number")
    success: bool = True


@flows.tool(flow="takeout")
def place_takeout_order(items: str, pickup_time: str, order_name: str) -> TakeoutResult:
    """Place a takeout order and return an order number."""
    return TakeoutResult(order_number="TKO-2213")


# --- Reservation specialist --------------------------------------------------
# The terminal task's `then_say` reads the confirmation back (the final turn), so
# no separate announce is needed.
reservation = flows.Flow("reservation", root_agent="Reservation_Agent")
reservation.add(
    flows.user_slot("party_size", "How many people are in your party?"),
    flows.user_slot("reservation_time", "What time would you like?"),
    flows.user_slot("guest_name", "What name should I put it under?"),
    flows.result_slot("confirmation_number", "book"),
)
reservation.task("book", "book_reservation",
                 ["party_size", "reservation_time", "guest_name"],
                 "confirmation_number", out_key="confirmation_number",
                 terminal=True, then_say="You're all set — confirmation {confirmation_number}.",
                 condition=flows.has("guest_name"))

# --- Takeout specialist ------------------------------------------------------
takeout = flows.Flow("takeout", root_agent="Takeout_Agent")
takeout.add(
    flows.user_slot("items", "What would you like to order?"),
    flows.user_slot("pickup_time", "What time will you pick it up?"),
    # Distinct slot name (not reservation's `guest_name`) so a name given in one
    # flow doesn't bleed into the other via the shared session state.
    flows.user_slot("order_name", "What name should I put the order under?"),
    flows.result_slot("order_number", "order"),
)
takeout.task("order", "place_takeout_order", ["items", "pickup_time", "order_name"],
             "order_number", out_key="order_number", terminal=True,
             then_say="Order {order_number} will be ready then.",
             condition=flows.has("order_name"))

# --- Agents + steering host --------------------------------------------------
# `aliases` are the spoken phrasings that mean "I want this agent" — they become
# route_cues, so a mid-call switch is detected WITHOUT a lead-in (real voice says
# "takeout order", not "actually, switch me to takeout").
reservation_agent = flows.Agent(
    "Reservation_Agent", flow=reservation,
    aliases=["reservation", "reserve a table", "book a table", "dine in", "sit down"],
)
takeout_agent = flows.Agent(
    "Takeout_Agent", flow=takeout,
    aliases=["takeout", "take out", "to go", "pickup order", "order food", "carry out"],
)

host = flows.HostRouter(
    "Bella_Notte_Host",
    routes={"reservation": reservation_agent, "takeout": takeout_agent},
    # strategy="transfer" is the default (receptionist router). entry_var weaves an
    # upstream intent tag into the host's silent-routing instruction.
    entry_var="ENTRY_INTENT",
)

app = flows.App(
    host=host,
    agents=[reservation_agent, takeout_agent],
    app_display_name="Bella Notte (flows multi-agent demo)",
    model="gemini-3.5-flash",
)


if __name__ == "__main__":
    errors, warnings = flows.validate_app(app)
    assert errors == [], errors
    flows.build_app(app, "./bella_notte_multiagent_app", overwrite=True)
    print("built -> ./bella_notte_multiagent_app")
