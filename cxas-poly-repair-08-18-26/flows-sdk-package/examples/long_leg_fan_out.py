"""Claim triage: one leg runs 50 seconds, and still speaks inside the same turn.

The shape this exists for. `progressive_fan_out.py` narrates each finding as it lands, and
its slowest leg is 30 seconds. That fits comfortably: the watcher polls in twenty-second
windows, so a 30s leg lands inside the SECOND window and every window the group ever opens
comes back with something new. Real backends are not so tidy. An underwriting rerun, a
fraud rescore, a model pass over uploaded photographs — these take the better part of a
minute, while the cheap lookups beside them take five seconds.

Fifty seconds is past the point where a single tool CALL is safe (about twenty-nine
seconds), and the group narrates it anyway. The reason is the one thing to take from this
file:

    The per-call deadline binds the WATCHER, not the LEG.

A leg is dispatched as a deferred asynchronous tool, so nothing holds a call open while it
runs. The watcher is the thing making calls, and it is chunked to twenty seconds and
re-dispatched every pass, which keeps each of its calls well inside the limit. A gap
longer than one window costs a reasoning pass; it does not cost the group.

    t     what happens                                            pass
    ----  ------------------------------------------------------  ----
    0s    all three legs dispatched, filler spoken                   1
    5s    cover check lands, spoken                                  2
    15s   payments check lands, spoken                               3
    15s   a watch window opens, and expires with NOTHING new         4
    50s   assessment lands, spoken; the all-done line closes         5

WHY FIFTY, AND NOT MORE. Two further limits sit above this one, and an author reaching for
a slower leg meets them in this order.

At sixty seconds the TOOL is killed. That is not the fan-out's doing and not a platform
constant: `timeout` is a field on the tool resource and 60s is merely its default, applying
to synchronous and asynchronous bodies alike. Overrunning it is silent — the body never
reports, so there is no error and nothing for `on_timeout` to fire on. Raise it where a
backend needs longer:

    @flows.tool(flow="claims", timeout=180)

Above roughly sixty seconds of GAP between two landings, the group gives up first. Its
empty-window ladder is a fixed three windows, so a leg that lands more than about a minute
after its last sibling is written off while CES is still happily running it — and the
all-done line then speaks over a hole. Deriving that ladder from the legs' declared
timeouts is the obvious fix and is NOT yet in place: widening it kills the live turn for
reasons nobody has explained. See `ces-probes/probes/106-fanout-patience`.

So: keep a narrated leg under about fifty seconds. Work that genuinely needs longer wants
a different shape rather than a bigger number — shard it across several legs, have the leg
start a job and return a handle, or chunk the body and relaunch it.

Each piece earns its place here:

* The legs are ORDINARY tools. Nothing is declared `asynchronous=True`, because the
  asynchrony is a property of the lowering rather than of the author's intent. The
  emitter defers each leg either way, and carries any `@tool(timeout=…)` onto the wrapper
  it generates — the wrapper being the resource CES enforces against.
* `filler_say` is spoken on the firing turn and promises the narration, because five
  seconds of silence after a caller finishes talking already feels like a dropped call.
* The payments leg's `then_say` does double duty. It reports its own finding AND warns
  that the next one is slow. That is the honest answer to what this shape costs: between
  t=15 and t=50 there is nothing to say, so the caller sits through thirty-five seconds of
  held line. The framework has no per-window holding line — a window that returns empty
  returns quietly — so the only place to set that expectation is the line before it. At
  this length a real agent should also be playing hold audio with `while_running`.
* `on_failure` on the payments leg, because a failed leg contributes no line at all.
  Without it the caller hears two findings instead of three and cannot tell which check
  went missing, and `all_done_say` would close over the gap.
* `all_done_say` is a summary, not a question. The question belongs to the next slot's
  `ask`, so the two never collide on one turn.
* `open_claim` is separate and terminal. A terminal fire tears the flow down, so it
  cannot be a leg — its siblings' results would land on a flow that had already ended.
* No `deadline`. It counts TURNS, and this group lives entirely inside one, so it could
  not fire however long the narration runs.

Not claimed: **offline green proves nothing here.** `validate_app`, the engine simulator
and pytest each fake at least one of the two things this depends on. Offline every leg
answers instantly and in order, so a 50-second leg and a 50-millisecond one produce an
identical trace, the empty watch window never happens, and no timeout is ever enforced.
The behavior is verified by driving the real voice channel and listening. See
`PROGRESSIVE_FAN_OUT_VERIFY.md`, next to this file, for the recipe.

Build + validate offline:
    python -m examples.long_leg_fan_out    # emits ./long_leg_fan_out_app
"""

from pydantic import BaseModel

import flows


class CoverFinding(BaseModel):
  success: bool = True
  cover_status: str = ""


class PaymentFinding(BaseModel):
  success: bool = True
  payment_status: str = ""


class AssessmentFinding(BaseModel):
  success: bool = True
  damage_estimate: str = ""


class ClaimReference(BaseModel):
  success: bool = True
  closing: str = ""


# Three checks whose durations straddle the twenty-second watch window on purpose: one
# inside the first window, one inside the second, and one that leaves several windows
# empty before it lands. Imports belong INSIDE a tool body — only the decorated function
# is rendered into the CES tool file, so a module-level import is not carried and the
# body dies with a NameError.


@flows.tool(flow="claim_triage")
def check_policy_cover(policy_number: str = "") -> CoverFinding:
  """Confirm the policy covers this kind of claim. ~5 seconds."""
  import time
  time.sleep(5)
  return CoverFinding(
      cover_status="storm damage to the roof is covered, with a two hundred and fifty"
                   " dollar deductible")


@flows.tool(flow="claim_triage")
def check_recent_payments(policy_number: str = "") -> PaymentFinding:
  """Check the premium is paid up and the policy is in force. ~15 seconds."""
  import time
  time.sleep(15)
  return PaymentFinding(
      payment_status="your premiums are all paid up, so the policy is in force")


@flows.tool(flow="claim_triage", timeout=180)
def run_damage_assessment(policy_number: str = "") -> AssessmentFinding:
  """Rerun the damage model over the submitted photographs. ~50 seconds.

  The slow one, and the reason this example exists. Longer than a single tool call may
  run, which is fine: the lowering defers it, so no call is held open while it works.
  Past the 60s the resource allows by default, which is exactly why the decorator above
  declares one -- without it this body is killed and never reports, silently.
  """
  import time
  time.sleep(50)
  return AssessmentFinding(
      damage_estimate="the model puts the repair somewhere between four and five"
                      " thousand dollars, which is well inside your cover")


@flows.tool(flow="claim_triage")
def open_claim(damage_estimate: str = "", callback_number: str = "") -> ClaimReference:
  """Open the claim and confirm how the caller will hear about it."""
  tail = f" I'll text the reference to {callback_number}." if callback_number else ""
  return ClaimReference(closing=f"Your claim is open and an adjuster will be in"
                                f" touch.{tail}")


def build() -> flows.App:
  """The triage flow, with a 45-second leg narrated in the same turn as the fast ones.

  Returns:
    The assembled app.
  """
  triage = flows.Flow("claim_triage", root_agent="Acme_Insurance")
  triage.add(flows.user_slot(
      "policy_number",
      ask="What's the policy number on the claim?",
      hint="the policy number the claim is being made against"))
  for slot in ("cover_status", "payment_status", "damage_estimate"):
    triage.add(flows.result_slot(slot, slot))

  triage.task(flows.parallel(
      "triage",
      tasks=[
          flows.task("cover_status", tool="check_policy_cover",
                     inputs=["policy_number"], out_slot="cover_status",
                     then_say="Good news on the cover: {cover_status}."),
          flows.task("payment_status", tool="check_recent_payments",
                     inputs=["policy_number"], out_slot="payment_status",
                     # Reports its own finding AND sets up the long silence behind it.
                     # There is no per-window holding line, so this is the last thing
                     # the caller hears for thirty seconds.
                     then_say="And {payment_status}. The damage assessment is the slow"
                              " one — it takes a minute or two, so stay with me and"
                              " I'll read it out the moment it lands.",
                     # A failed leg contributes no line. Without this the caller hears
                     # two findings instead of three with nothing to say which check
                     # went missing, and the summary below would close over the gap.
                     on_failure={"max_retries": 0, "on_exhaust": {
                         "say": "I couldn't get the payment history up just now."}}),
          flows.task("damage_estimate", tool="run_damage_assessment",
                     inputs=["policy_number"], out_slot="damage_estimate",
                     then_say="Right, the assessment is back: {damage_estimate}."),
      ],
      # Spoken on the firing turn. It is the only thing between the caller and the first
      # five seconds, and it promises the narration on purpose.
      filler_say="Let me pull up three things on that claim — I'll talk you through them"
                 " as they come back.",
      # A summary, not a question: the question belongs to the next slot's `ask`, so the
      # caller is never asked the same thing twice on one turn.
      all_done_say="So your cover is good, the policy is in force, and we have an"
                   " estimate to work from.",
  ))

  # No dependency on any check, so it is collected once the group has closed rather than
  # being interleaved with the findings.
  triage.add(flows.user_slot(
      "callback_number",
      ask="Shall I open the claim? If so, what's the best number to text the reference"
          " to?",
      hint="a number to text the claim reference to"))
  triage.add(flows.result_slot("closing", "open_the_claim"))
  triage.task(flows.task("open_the_claim", tool="open_claim",
                         inputs=["damage_estimate", "callback_number"],
                         out_slot="closing", terminal=True, then_say="{closing}"))

  return flows.App(root_flow=triage, app_display_name="Acme Insurance Claim Triage")


app = build()


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for warning in warnings:
    print("warning:", warning)
  for error in errors:
    print("error:", error)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./long_leg_fan_out_app", overwrite=True)
    print("built: ./long_leg_fan_out_app")
