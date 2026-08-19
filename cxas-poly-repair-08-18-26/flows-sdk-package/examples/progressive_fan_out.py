"""Broadband fault: three checks at once, each one SPOKEN THE MOMENT IT LANDS.

The shape this exists for. `parallel_fan_out.py` fixed the dispatch half — the legs go
out in one action and the runtime runs them concurrently, so three lookups cost the
slowest one rather than their sum. What it could not fix is the REPORTING half, and its
own docstring is blunt about it: a synchronous group hands the whole batch back after the
slowest leg, so a line per result as it arrives needs asynchronous legs, and those cost
one caller turn each.

That leaves the caller with two bad options. Wait in silence for thirty seconds, or hand
the floor back between findings and pay a turn per sentence. Neither is what a human
agent does. A human says "line test is back, that's your fault right there" while still
waiting on the other two.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "internet keeps dropping out"   asks for the phone number
    "07700 900123"                  fires ALL THREE in one action and speaks the
                                    filler — one dispatch, not three
    (8s in)                         "the line test is back: you're dropping every
                                    ten minutes" — while the other two still run
    (18s in)                        "your account is all in order, by the way"
    (30s in)                        "and Thursday morning is free", then the
                                    group's all-done line closes it out
    "yes please"                    books the visit and confirms

The three narration points between the dispatch and the close are the whole example, and
the caller never got the floor back in between. The legs finish 8, 18 and 30 seconds
apart on purpose: if they finished together there would be nothing to narrate
progressively and the demo would prove nothing.

Each piece earns its place here:

* `flows.parallel(...)` groups the legs, exactly as in `parallel_fan_out.py`. **The
  authoring surface is unchanged** — no new kwargs and no new task keys. What changes is
  the lowering underneath: the same three lines that cost three caller turns are narrated
  inside one.
* `then_say` on each LEG, so the line lives next to the task that produces it. Under the
  progressive lowering these are no longer concatenated in declaration order — each is
  spoken on its own, in ARRIVAL order, as its leg publishes its result.
* The legs are ORDINARY tools. Nothing here is declared `asynchronous=True`, because the
  asynchrony is a property of the LOWERING, not of the author's intent: the emitter turns
  each leg into an asynchronous CES tool that publishes to its own state key, and the
  engine narrates from those keys. Declaring it by hand would say the same thing twice
  and, when the emitter has not lowered the group, would leave the legs deferred with
  nothing watching them.
* `deadline`, `waiting_say` and `on_timeout` are all fine on the group, and this example
  simply does not need them. They land as `awaits` on a leg, which an earlier draft of the
  lowering treated as a disqualifier — so declaring a deadline silently returned the group
  to the batch shape, with nothing said about it. That exclusion is gone: the predicate is
  now just "two or more legs, every leg fires a tool", which `parallel()` and the validator
  already enforce, so no kwarg can opt a well-formed group out. `waiting_say` is spoken on
  the first watch dispatch, and `deadline`/`on_timeout` remain the cross-turn backstop for
  a group that outlives the held floor.
* `filler_say` carries the opening line instead. It is spoken on the FIRING turn, which
  is the only thing between the caller and the first eight seconds of silence, so it
  promises the narration that follows. Kept verbatim rather than improvised: a group's
  filler is spoken while three backends are in flight, which is not a moment to hand the
  wording to the model.
* `on_failure` on the account leg, because a failed leg contributes no line at all.
  Without it the caller hears two findings instead of three and cannot tell which check
  is missing — and the all-done summary would close over the gap.
* `all_done_say` is a SUMMARY, not a question. The question is the next slot's `ask`, so
  the two never collide on one turn and the caller is asked exactly once.
* `book_visit` is separate and terminal. A terminal fire tears the flow down, so it
  cannot be a leg — its siblings' results would land on a flow that had already ended.

Not claimed, and this is the important part: **offline green proves nothing here.**
`validate_app`, the engine simulator and pytest all fake the two things this feature
depends on — real concurrency and real speech. Offline every leg answers synchronously
and instantly, which is exactly the case the lowering is designed to leave alone, so an
offline run cannot tell a progressive group from an ordinary one. A byte count is no
better: ten runs of one line measured an identical 4.6s, which says only that the stream
is the same length every time. The behaviour has to be verified by driving the real voice
channel and LISTENING to the result. `PROGRESSIVE_FAN_OUT_VERIFY.md`, next to this file,
is the recipe.

Build + validate offline:
    python -m examples.progressive_fan_out    # emits ./progressive_fan_out_app
"""

from pydantic import BaseModel

import flows


class LineFinding(BaseModel):
  success: bool = True
  line_finding: str = ""


class AccountFinding(BaseModel):
  success: bool = True
  account_finding: str = ""


class EngineerFinding(BaseModel):
  success: bool = True
  engineer_slot: str = ""


class VisitBooking(BaseModel):
  success: bool = True
  closing: str = ""


# Three checks with deliberately different durations, so there is something to narrate
# progressively. Imports belong INSIDE a tool body: only the decorated function is
# rendered into the CES tool file, so a module-level import is not carried and the body
# dies with a NameError.


@flows.tool(flow="broadband_fault")
def diagnose_line(phone_number: str = "") -> LineFinding:
  """Run a full line test. ~8 seconds, like the real backend."""
  import time
  time.sleep(8)
  return LineFinding(
      line_finding="your connection is dropping about every ten minutes, so there is a"
                   " genuine fault here")


@flows.tool(flow="broadband_fault")
def diagnose_account(phone_number: str = "") -> AccountFinding:
  """Check the account is active and unrestricted. ~18 seconds."""
  import time
  time.sleep(18)
  return AccountFinding(
      account_finding="your account is all in order — nothing on our side is restricting"
                      " the service")


@flows.tool(flow="broadband_fault")
def find_engineer_slot(phone_number: str = "") -> EngineerFinding:
  """Look for the first engineer visit available. ~30 seconds."""
  import time
  time.sleep(30)
  return EngineerFinding(engineer_slot="Thursday morning, between eight and midday")


@flows.tool(flow="broadband_fault")
def book_engineer_visit(engineer_slot: str = "", callback_number: str = "") -> VisitBooking:
  """Book the engineer visit and confirm how the caller will hear about it."""
  tail = f" I'll text confirmation to {callback_number}." if callback_number else ""
  return VisitBooking(closing=f"You're booked in for {engineer_slot}.{tail}")


def build() -> flows.App:
  """The fault flow, with its three diagnostics narrated as they land."""
  fault = flows.Flow("broadband_fault", root_agent="Acme_Broadband")
  fault.add(flows.user_slot(
      "phone_number",
      ask="What's the phone number on the account?",
      hint="the landline or mobile number on the account"))
  for slot in ("line_finding", "account_finding", "engineer_slot"):
    fault.add(flows.result_slot(slot, slot))

  fault.task(flows.parallel(
      "diagnostics",
      tasks=[
          flows.task("line_finding", tool="diagnose_line",
                     inputs=["phone_number"], out_slot="line_finding",
                     then_say="Right, the line test is back: {line_finding}."),
          flows.task("account_finding", tool="diagnose_account",
                     inputs=["phone_number"], out_slot="account_finding",
                     then_say="{account_finding}.",
                     # A failed leg contributes no line. Without this the caller hears
                     # two findings instead of three with nothing to say which check
                     # went missing, and the summary below would close over the gap.
                     on_failure={"max_retries": 0, "on_exhaust": {
                         "say": "I couldn't reach billing just now."}}),
          flows.task("engineer_slot", tool="find_engineer_slot",
                     inputs=["phone_number"], out_slot="engineer_slot",
                     then_say="And I have engineer availability: {engineer_slot}."),
      ],
      # Spoken on the firing turn — the only thing standing between the caller and the
      # first eight seconds. It promises the narration on purpose.
      filler_say="Let me run a few checks on your line — I'll talk you through them as"
                 " they come back.",
      # A summary, not a question: the question belongs to the next slot's `ask`, so the
      # caller is never asked the same thing twice on one turn.
      all_done_say="So that's a real fault on the line, your account is fine, and"
                   " {engineer_slot} is free.",
  ))

  # No dependency on any diagnostic, so it is collected once the group has closed rather
  # than being interleaved with the findings.
  fault.add(flows.user_slot(
      "callback_number",
      ask="Shall I book that engineer? If so, what's the best number to text the"
          " confirmation to?",
      hint="a number to text the confirmation to"))
  fault.add(flows.result_slot("closing", "book_visit"))
  fault.task(flows.task("book_visit", tool="book_engineer_visit",
                        inputs=["engineer_slot", "callback_number"],
                        out_slot="closing", terminal=True, then_say="{closing}"))

  return flows.App(root_flow=fault, app_display_name="Acme Broadband Diagnostics")


app = build()


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for warning in warnings:
    print("warning:", warning)
  for error in errors:
    print("error:", error)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./progressive_fan_out_app", overwrite=True)
    print("built: ./progressive_fan_out_app")
