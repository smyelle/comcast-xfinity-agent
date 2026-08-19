"""Waiting out loud: which holding lines the model may vary, and which it cannot.

A slow backend produces three spoken moments, and they look interchangeable until you
ask what else is riding on the turn:

  * `filler_say`    — "one moment while I check", said as the tool is called.
  * `awaits.say`    — said once when an ASYNCHRONOUS tool comes back "pending".
  * `while_waiting` — said on the idle turns after that, one line per turn.

(`filler_say` covers a second wait too — a turn handed to the model, where it is spoken
as a partial preempt instead of riding a call. That delivery is not what this example is
about; see `filler_say.py`. Everything below concerns the tool-call one.)

All three can be improvised, but the filler gets there a different way, and the
difference is worth understanding before switching it on.

`awaits.say` and `while_waiting` are their own turns with nothing but text on them,
so they cross to the directive channel the ordinary way: the engine hands the line
over and the model rewords it.

A filler cannot do that. It rides the SAME engine action as the tool's
`function_call`, and a `function_call` only becomes a response part on the preempt
path — hand that turn to the model and the call is not delayed, it is dropped. So
the engine hands over the CALL as well: it stops preempting and asks the model for a
reply containing both its own holding line and the call, with the arguments spelled
out. Measured across live sessions in `ces-probes/probes/27`-`32`, that lands 12/12
with the arguments exact, survives the framework's own "output only tool calls" rule,
and picks the right tool out of a crowded schema.

What it buys is not variety but FIT: the line can mention what the caller actually
asked about, which an authored string cannot. What it costs is that the model now
issues the call. Hence the guards — a task whose inputs include a `sensitive` slot is
pinned verbatim at build time, long or non-scalar arguments keep the engine's own
dispatch, and if the model answers without calling, the engine takes the turn back
and fires it the ordinary way.

Build + validate offline:
    python -m examples.improvise_await         # emits ./improvise_await_app
"""

from pydantic import BaseModel, Field

import flows


class CarrierStatus(BaseModel):
  """What the carrier says about a parcel."""

  location: str = Field(description="Where the parcel was last scanned.")
  eta: str = Field(description="Expected delivery, in words.")
  success: bool = Field(default=True, description="Whether the lookup worked.")


class ParcelSummary(BaseModel):
  """The closing line's inputs."""

  summary: str = Field(description="Where the parcel is and when it lands.")
  success: bool = Field(default=True)


@flows.tool(flow="parcel_status", asynchronous=True)
def slow_carrier_lookup(tracking_number: str) -> CarrierStatus:
  """Ask the carrier where a parcel is. Slow — the carrier answers out of band."""
  return CarrierStatus(location="the Leeds depot", eta="tomorrow before 6pm")


@flows.tool(flow="parcel_status")
def summarize_parcel(location: str = "", eta: str = "") -> ParcelSummary:
  """Close the call out once the carrier has answered."""
  return ParcelSummary(summary=f"last seen at {location}, arriving {eta}")


policy = flows.speech(
    improvise=["await", "filler"],
    improvise_style=(
        "Reassure without repeating yourself. One short sentence, and do not"
        " promise a time the system has not given you."),
)

tracking = flows.user_slot(
    "tracking_number", "What's the tracking number?",
    validation_rules=[{"kind": "length_digits", "detail": "10"}])

parcel = flows.Flow(
    "parcel_status", root_agent="Parcel_Agent",
    speech=policy,
    bootstrap={"welcome_slot": "welcome"},
)
parcel.add(
    flows.announce("welcome", ["I can check where your parcel is."], shared=True),
    tracking,
    flows.result_slot("location", "carrier_task"),
    flows.result_slot("eta", "carrier_task"),
    flows.result_slot("summary", "closing_task"),
)

# The wait itself. It cannot be terminal — an asynchronous result lands a turn or
# more later, so a terminal fire here would close the call before the answer arrived.
parcel.task(
    "carrier_task", "slow_carrier_lookup", ["tracking_number"], "location",
    out_key="location", extra_outputs={"eta": "eta"},

    # Improvised by handing the model the CALL as well as the line — this rides the
    # same turn as the tool call, so it cannot cross over on its own. Still authored:
    # it is what the caller hears if the model declines, or if a guard pins the task.
    filler_say="One moment while I check with the carrier.",

    # Their own turns, so these cross over the ordinary way.
    awaits=flows.awaits(
        max_turns=6,
        say="The carrier is a little slow today — bear with me.",
        while_waiting=["Still waiting on the carrier.",
                       "Nearly there, thanks for holding."],
        on_timeout={"say": "The carrier isn't answering. Let me take a message."},
    ),
)

# The closing line, once the carrier has actually answered. Composed from the tool
# result rather than templated: `then_directive` has worked on the wire for a long
# time with no kwarg to reach it, and this is that kwarg. Ignored if `then_say` is
# also set — a task speaks one way or the other.
parcel.task(
    "closing_task", "summarize_parcel", ["location", "eta"], "summary",
    out_key="summary", terminal=True,
    then_directive=("Tell the caller where the parcel is and when it should"
                    " arrive, using the tool result."),
)

app = flows.App(
    root_flow=parcel,
    app_display_name="flows-improvise-demo Await",
    agent_instruction=(
        "You are a parcel-tracking agent. Follow the slot-filling framework"
        " directives exactly. Speak only what the framework gives you."
    ),
)


def _show_boundary() -> None:
  """Print which holding lines cross to the model and which cannot."""
  task = parcel.to_config()["tasks"][0]
  print(f"  improvising: {', '.join(parcel.to_config()['speech']['improvise'])}")
  print("  filler_say          reworded  (model issues the call too)")
  print("  awaits.say          reworded  (its own turn)")
  print(f"  awaits.while_waiting reworded ({len(task['awaits']['while_waiting'])} lines)")
  print("  then_directive      model-composed from the tool result")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./improvise_await_app")
    print("built: ./improvise_await_app")
    _show_boundary()
