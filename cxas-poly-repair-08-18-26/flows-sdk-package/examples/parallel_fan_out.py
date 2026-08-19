"""Broadband fault: four checks at once, instead of four turns.

The shape this exists for. A caller reports a fault and the agent needs several
unrelated things: is there an outage in the area, is the account in good standing, is
an engineer already booked, and what does a line test say. None of them depends on any
other — the DAG says so, since none consumes another's output — but the engine fires
one task per pass, so they cost four re-invocations and, because each blocks its turn,
four lookups' worth of the caller's time.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "internet keeps dropping out"   asks for the phone number
    "07700 900123"                  fires ALL FOUR in one action. The three fast
                                    ones answer in the same turn; the line test is
                                    deferred by the platform
    "same one's fine"               ASKS FOR THE CALLBACK NUMBER while the line
                                    test runs — the wait parks the join, not the
                                    conversation
    (completion lands)              the line test speaks its own line, then the
                                    group's all-done line closes the group

Measured, not asserted: the legs run concurrently, so three four-second lookups cost
the caller four seconds rather than twelve (ces-probes 33).

Each piece earns its place here:

* `flows.parallel(...)` groups the legs. Every eligible one is dispatched in a single
  action; a leg that is gated off, already complete, or still awaiting is simply not in
  that pass's set, so the group is a batching hint rather than a barrier.
* `then_say` on each LEG, not on the group, so the line lives next to the task that
  produces it. The synchronous lines are concatenated into one utterance in declaration
  order; arrival order is neither stable nor observable and is never used.
* `on_failure` on the billing leg, because a failed leg contributes no line. Without it
  the caller hears three sentences instead of four and cannot tell which check is
  missing — and `all_done_say` would close over the gap.
* `deadline` / `waiting_say` / `on_timeout` are declared once on the GROUP and land only
  on the deferred leg. The author never says which leg that is: it is inferred from
  `@flows.tool(asynchronous=True)`.
* `callback_number` has no dependency on the line test, so the engine asks it during the
  wait rather than leaving the caller in silence.
* The closing task is separate and terminal. A terminal fire is deferred off any turn
  carrying user text, and the completion lands on a later turn, so the close cannot be
  a leg.

On narration. This group DOES speak a line per finding, because narrating is the default
and every well-formed group is lowered for it. That was not true when this example was
written: a synchronous group has one observation point, after the slowest leg
(ces-probes 40), and back then that was the only shape there was.

The older behaviour is still available and is sometimes the right choice --
`parallel(progressive=False)` keeps the legs synchronous and costs ONE reasoning pass
instead of one per check, which matters on a flow near the ten-pass ceiling. See
`batch_fan_out.py`.
"""

from typing import Optional

from pydantic import BaseModel

import flows


class OutageReport(BaseModel):
  success: bool = True
  outage_status: str


class AccountReport(BaseModel):
  success: bool = True
  account_status: str


class BookingReport(BaseModel):
  success: bool = True
  appointment: str


class LineTestReport(BaseModel):
  success: bool = True
  line_result: str


class RepairBooking(BaseModel):
  success: bool = True
  closing: str


@flows.tool(flow="fault")
def check_area_outage(phone_number: str = "") -> OutageReport:
  """Look for a reported outage covering this line."""
  return OutageReport(outage_status="no outage reported in your area")


@flows.tool(flow="fault")
def check_account(phone_number: str = "") -> AccountReport:
  """Check the account is active and in good standing."""
  return AccountReport(account_status="active and up to date")


@flows.tool(flow="fault")
def check_appointment(phone_number: str = "") -> BookingReport:
  """Look for an engineer visit already booked against this line."""
  return BookingReport(appointment="no engineer visit booked")


# The whole point of the fourth leg: a real line test outlives the turn, so CES defers
# the body and answers the call with a pending placeholder.
@flows.tool(flow="fault", asynchronous=True)
def run_line_test(phone_number: str = "") -> LineTestReport:
  """Run a full line test. Declared ASYNCHRONOUS."""
  # Imports belong INSIDE a tool body: only the decorated function is rendered into the
  # CES tool file, so a module-level import is not carried and the body dies.
  import time
  time.sleep(20)
  return LineTestReport(line_result="a fault on the line into your property")


@flows.tool(flow="fault")
def book_repair(line_result: str = "", callback_number: str = "") -> RepairBooking:
  """Book the repair and confirm how the caller will hear about it."""
  tail = f" I'll text confirmation to {callback_number}." if callback_number else ""
  return RepairBooking(
      closing=f"I've booked an engineer for Thursday morning.{tail}")


def build() -> flows.App:
  """The fault flow, with its four checks grouped."""
  fault = flows.Flow("fault", root_agent="Acme_Broadband")
  fault.add(flows.user_slot(
      "phone_number", ask="What's the phone number on the account?"))
  for slot in ("outage_status", "account_status", "appointment", "line_result"):
    fault.add(flows.result_slot(slot, slot))

  fault.task(flows.parallel(
      "checks",
      tasks=[
          flows.task("outage_status", tool="check_area_outage",
                     inputs=["phone_number"], out_slot="outage_status",
                     then_say="There's {outage_status}."),
          flows.task("account_status", tool="check_account",
                     inputs=["phone_number"], out_slot="account_status",
                     then_say="Your account is {account_status}.",
                     # No retry: the other three legs have already answered by the
                     # time this one fails, and re-firing it alone would hold the
                     # group's all-done line for a second round trip.
                     on_failure={"max_retries": 0, "on_exhaust": {
                         "say": "I couldn't reach billing just now."}}),
          flows.task("appointment", tool="check_appointment",
                     inputs=["phone_number"], out_slot="appointment",
                     then_say="You have {appointment}."),
          flows.task("line_result", tool="run_line_test",
                     inputs=["phone_number"], out_slot="line_result",
                     then_say="The line test found {line_result}."),
      ],
      deadline=6,
      waiting_say="I've started a line test — that takes a couple of minutes.",
      on_timeout={"say": "The line test isn't coming back.",
                  "then": {"tool": "transfer_to_human"}},
      all_done_say="That's everything checked on your line.",
  ))

  # No dependency on the line test, so it is asked DURING the wait.
  fault.add(flows.user_slot(
      "callback_number",
      ask="What's the best number to text the result to?"))
  fault.add(flows.result_slot("closing", "wrap"))
  fault.task(flows.task("wrap", tool="book_repair",
                        inputs=["line_result", "callback_number"],
                        out_slot="closing", terminal=True, then_say="{closing}"))

  return flows.App(root_flow=fault, app_display_name="Acme Broadband")


app = build()


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for warning in warnings:
    print("warning:", warning)
  for error in errors:
    print("error:", error)
  if not errors:
    flows.build_app(app, "./parallel_fan_out_app", overwrite=True)
    print("built: ./parallel_fan_out_app")
