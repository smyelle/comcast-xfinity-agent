"""Track a shipment, answer a store-hours FAQ mid-conversation, then track another.

Exercises three things in one re-enterable flow:
  * a task-backed lookup (collect a tracking number -> tool -> read the status back),
  * an out-of-flow FAQ the agent can answer any turn (store hours), and
  * re-entry: a TERMINAL task completes the flow, and `single_flow` + a self-seeding
    `gate_slot` + `bootstrap.reset_on_complete` re-arm it, so "track another one"
    restarts collection instead of ending the call.

Build + validate offline:
    python -m examples.track_shipment        # emits ./track_shipment_app
"""

from pydantic import BaseModel, Field

import flows


class TrackResult(BaseModel):
    status_message: str = Field(description="Human-readable delivery status")
    success: bool = True


# Flat scalar input (not a pydantic wrapper): a task tool's input is a single slot,
# so a flat param keeps the model's call unambiguous. The pydantic RETURN still
# gives `flows` the declared output keys.
@flows.tool(flow="track_shipment")
def lookup_shipment(tracking_number: str) -> TrackResult:
    """Look up the delivery status of a shipment by its tracking number."""
    return TrackResult(
        status_message=f"Shipment {tracking_number} is out for delivery, arriving today by 8 PM."
    )


class StoreHoursResult(BaseModel):
    hours_message: str = Field(description="The store hours to read back")


@flows.tool(flow="track_shipment")
def store_hours(day: str) -> StoreHoursResult:
    """Answer a question about store / pickup-counter hours for a given day."""
    return StoreHoursResult(hours_message="Our stores are open 8 AM to 9 PM every day.")


# A single-flow app is auto-gated at build (self-seeding gate + reset_on_complete),
# so it starts on turn 1 and re-arms after completion — a follow-up "track another"
# restarts it. Completion is driven by the TERMINAL `lookup` task below.
track = flows.Flow("track_shipment", root_agent="Track_Agent",
                   bootstrap={"welcome_slot": "welcome"})
track.add(
    flows.announce("welcome", ["Thanks for calling. I can track a shipment for you."],
                   shared=True),
    flows.user_slot("tracking_number", "What's your tracking number?"),
    flows.result_slot("status_msg", "lookup"),
)
# `terminal=True` completes the flow after the lookup; `then_say` reads the tool's
# status back (interpolating the result slot).
track.task("lookup", "lookup_shipment", ["tracking_number"], "status_msg",
           out_key="status_message", terminal=True, then_say="{status_msg}",
           condition=flows.has("tracking_number"))

app = flows.App(
    root_flow=track,
    app_display_name="Track Shipment + Store Hours",
    model="gemini-3.5-flash",
    # Scope the FAQ tool onto the agent so it can answer store-hours any turn.
    extra_agent_tools=["store_hours"],
)


if __name__ == "__main__":
    errors, warnings = flows.validate_app(app)
    assert errors == [], errors
    flows.build_app(app, "./track_shipment_app", overwrite=True)
    print("built -> ./track_shipment_app")
