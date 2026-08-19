"""Covering a wait, whether the wait is a tool or the model.

`filler_say` used to mean one thing: a line spoken as a tool is called. It rides the
same turn as the call, so it costs nothing and lands before the backend is even asked.

But a turn the framework hands to the MODEL waits too, and nothing covered that. It
could not: the only way to speak ahead of the model is to preempt, and a text-only
preempt ENDS the turn, so the caller would have heard "one moment" and then had to ask
again to get an answer. Marking that preempt `partial` speaks the line and keeps the
floor, so the model's reply arrives in the same breath:

    caller   my internet keeps dropping in the evenings
    agent    Let me take a look.                             <- filler, partial preempt
    agent    That sounds like peak-time congestion. How...   <- the model, SAME turn

So it is still one field. An author says "there is a wait here, cover it" and the engine
picks the delivery from the turn it is already in — which matters because they often
cannot know: a task with a `condition` may or may not dispatch on any given turn.

Two things to know before switching it on:

  * The model-turn delivery costs one of the ten reasoning passes. The engine arms it at
    most once per caller turn and stands down when the budget is nearly gone, but on a
    flow where every slot carries one you are trading depth for smoothness.
  * Keep the line CONTENTLESS. "Let me take a look." is safe; "Let me check why your
    account is suspended." hands the model a conclusion it has not reached, and a
    prefix that carries a diagnosis has been observed to change what the model says
    next.

A pool is the point of the feature, not a flourish: the same five words on every wait is
what makes an agent sound scripted. `None` inside the pool is silence — an ordinary
member, so "sometimes say nothing" is written the same way as "sometimes say this", and
either is weighted by writing it more than once. Lines may reference filled slots; one
whose slot is not filled yet is skipped rather than spoken with its braces.
"""

from __future__ import annotations

import flows


@flows.tool
def lookup_line_status(area_code: str) -> dict:
  """Check the network status for an area (slow: a real backend round trip).

  Args:
    area_code: The dialling code for the caller's area.

  Returns:
    A dict carrying a success flag and the current status text.
  """
  return {"success": True,
          "status": f"congestion reported in {area_code} between 7pm and 10pm"}


support = flows.Flow(
    "support",
    root_agent="support_agent",
    # Flow-level default: any model turn whose slot carries none of its own. A pool,
    # because one line across a whole flow is exactly the repetition to avoid.
    filler_say=["One moment.", "Let me look into that.", None],
)

support.add(
    # No tool on this turn — the model composes the reply, and the filler is spoken as a
    # partial preempt to cover it.
    flows.user_slot(
        "problem", "Tell me what's going on with your service.",
        filler_say=["Let me take a look.", "One sec.", None]),
    flows.user_slot("area_code", "What's your area code?"),
    flows.result_slot("line_status", "check_line"),
    flows.announce("verdict", ["Here's what I can see: {line_status}."],
                   requires=["line_status"], preempt=True),
)

# The original delivery, unchanged: this line rides the turn that calls the tool, so it
# is free. It rotates now too — a repetitive tool filler is just as grating.
support.task(
    "check_line", "lookup_line_status", ["area_code"], "line_status",
    out_key="status",
    filler_say=["Let me check the network in {area_code}.",
                "One moment while I check your line.", None],
)

app = flows.App(root_flow=support, app_display_name="Filler demo")


def _show_deliveries() -> None:
  """Print which wait each authored filler covers, and what it costs."""
  cfg = app.root_flow.to_config()
  slot = next(s for s in cfg["slots"] if s["name"] == "problem")
  task = cfg["tasks"][0]
  print(f"  flow default   {cfg['filler_say']}")
  print(f"  slot 'problem' {slot['filler_say']}")
  print("                 -> partial preempt, model answers in the same turn, 1 pass")
  print(f"  task 'check_line' {task['filler_say']}")
  print("                 -> rides the tool call, free")
  print("  None in a pool -> that turn stays silent")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./filler_say_app")
    print("built: ./filler_say_app")
    _show_deliveries()
