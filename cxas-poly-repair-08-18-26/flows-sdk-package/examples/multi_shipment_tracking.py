"""Track N shipments with a DETERMINISTIC end — the framework decides when to hang up.

This is the Mode B repeated-component pattern. Instead of relying on the model to
call `end_session` when the caller is done (unreliable), the model only EXTRACTS
the caller's "another / all set" reply into a slot; the ENGINE reads it and
deterministically loops or terminates. That is the framework's job: steering is
deterministic, the model does extraction/generation.

Shape:
  * child DAG `track_one`  — track ONE shipment, then ask "another or all set?" and
    convert the reply to a boolean `done_flag` via the terminal `Finish` task.
  * parent `track_trip`    — a repeated component over `track_one` with
    `until.done_setter = done_flag`. done_flag False -> re-descend (next shipment);
    True (min_count met) -> collection completes -> terminal announce ends the call.

The classifying `set_more` setter maps free speech ("yes"/"no"/"that's all"/...) to
the canonical keys the tool converts to `done_flag`.

Build + validate offline:
    python -m examples.multi_shipment_tracking     # emits ./multi_shipment_tracking_app
"""

from typing import Literal

from pydantic import BaseModel, Field

import flows


class TrackResult(BaseModel):
    status_message: str = Field(description="Human-readable delivery status")
    success: bool = True


@flows.tool(flow="track_one")
def lookup_shipment(tracking_number: str) -> TrackResult:
    """Look up the delivery status of a shipment by its tracking number."""
    return TrackResult(
        status_message=f"Shipment {tracking_number} is out for delivery, arriving today by 8 PM."
    )


class FinishResult(BaseModel):
    done_flag: bool = Field(description="True when the caller is finished tracking")
    # `success` (not `ok`) — the task's default success_check looks for "success".
    success: bool = True


# The `more` setter is MODEL-classified, not keyword-matched: the model reads the
# caller's reply and picks the enum. The docstring is the prompt that tells it how.
# (A classifying setter over keywords is the brittle fallback we deliberately avoid —
# it errored on a bare tracking number like "track 10101122".)
@flows.tool(flow="track_one")
def set_more(more: Literal["another", "done"]) -> dict:
    """Record whether the caller wants to track another shipment or is finished.

    Use "another" if they want to continue — including when they simply give a new
    tracking number or say things like "track 123", "one more", or "yes". Use "done"
    ONLY when they clearly indicate they are finished (e.g. "no", "that's all",
    "I'm good", "goodbye").
    """
    return {"stored": True, "value": more}


@flows.tool(flow="track_one")
def finish_track(more: str) -> FinishResult:
    """Convert the caller's 'another'/'done' choice into the terminal done signal."""
    return FinishResult(done_flag=(more == "done"))


class StoreHoursResult(BaseModel):
    hours_message: str = Field(description="The store hours to read back")


@flows.tool(flows=["track_trip", "track_one"])
def store_hours(day: str) -> StoreHoursResult:
    """Answer a question about store / pickup-counter hours for a given day."""
    return StoreHoursResult(hours_message="Our stores are open 8 AM to 9 PM every day.")


# Child DAG: track ONE shipment, then capture "another vs done" as a boolean.
child = flows.Flow("track_one", root_agent="Track_Agent",
                   bootstrap={"reset_on_complete": True})
child.add(
    flows.user_slot("tracking_number", "What's your tracking number?"),
    flows.result_slot("status", "Lookup"),
    flows.user_slot("more", "Would you like to track another shipment, or are you all set?"),
    flows.result_slot("done_flag", "Finish"),
)
child.task("Lookup", "lookup_shipment", ["tracking_number"], "status",
           out_key="status_message", then_say="{status}", condition=flows.has("tracking_number"))
child.task("Finish", "finish_track", ["more"], "done_flag",
           out_key="done_flag", terminal=True, condition=flows.has("more"))

# Parent: a repeated component over `track_one`. `until.done_setter` reads the
# child's `done_flag`; min_count=1 requires at least one shipment.
parent = flows.Flow("track_trip", root_agent="Track_Agent",
                    single_flow=True, gate_slot="active_flow",
                    bootstrap={"slot": "active_flow", "reset_on_complete": True,
                               "welcome_slot": "welcome"})
parent.add(
    flows.announce("welcome", ["Thanks for calling. I can track your shipments."], shared=True),
    {"name": "shipments", "source": "task:CollectShipments",
     "readback_fmt": {"type": "join", "sep": "; ", "each": "{status}"}},
    # preempt=True or the sign-off is dropped and the call ends on silence: the
    # collection completing does not itself preempt the turn.
    flows.announce("done", ["All set. Have a great day!"], requires=["shipments"],
                   end=True, preempt=True),
)
parent.task({"name": "CollectShipments", "component": "track_one", "collect": "shipments",
             "element": {"status": "status"},
             "repeated": {"until": {"done_setter": "done_flag"}, "min_count": 1},
             "inputs": {}, "on_abort": "skip"})

app = flows.App(
    root_flow=parent, extra_flows=[child],
    app_display_name="Track Shipments (deterministic end)",
    model="gemini-3.5-flash",
    extra_agent_tools=["store_hours"],
)


if __name__ == "__main__":
    errors, warnings = flows.validate_app(app)
    assert errors == [], errors
    flows.build_app(app, "./multi_shipment_tracking_app", overwrite=True)
    print("built -> ./multi_shipment_tracking_app")
