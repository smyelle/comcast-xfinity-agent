"""A slot that already has a value, and an async tool about to replace it.

The subtle failure this exists to prevent. The in-flight marker keeps the AWAITING task
out of the fire selector — but a task that merely *reads* the slot that wait will fill
is a different task, and it is perfectly eligible on whatever value is sitting there
right now.

The value sitting there is usually a placeholder. A quick check writes "still checking"
so the caller is not left with an empty field; the slow check replaces it with the real
answer a few turns later. Without the guard, the task that acts on the verdict fires on
the placeholder — and gives the caller a confident answer to the wrong question.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "my internet keeps dropping"    asks for the account
    "8069100230361049"              quick_check writes verdict = "still checking"
                                    deep_scan (ASYNC) fires; it will REPLACE verdict
    "ok"                            `advise` reads verdict, so it is HELD. Without
                                    the guard it fires here and reads out
                                    "still checking" as though it were the answer.
    (scan completes)                verdict becomes the real finding, and only now
                                    does `advise` fire on it

Both `quick_check` and `deep_scan` declare the SAME output slot. That is the whole
setup: it is what makes `verdict` stale rather than merely empty, and an empty slot
would have blocked `advise` on its own without needing any of this.

The guard is scoped to task eligibility, not to slot collection — so an unrelated
question is still asked during the wait. `contact_pref` is here to prove that: it has
nothing to do with the scan, so the flow keeps collecting it while the backend works.

Build + validate offline:
    python -m examples.async_stale_consumer      # emits ./async_stale_consumer_app
"""

from pydantic import BaseModel, Field

import flows


class VerdictResult(BaseModel):
  verdict: str = ""
  success: bool = Field(default=True)


class AdviceResult(BaseModel):
  advice: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="linecheck")
def quick_check(account: str = "") -> VerdictResult:
  """Instant placeholder verdict, so the field is never empty."""
  return VerdictResult(verdict="still checking")


# Declares the SAME output slot as quick_check, and takes long enough that the
# placeholder is genuinely readable in between.
@flows.tool(flow="linecheck", asynchronous=True)
def deep_scan(account: str = "") -> VerdictResult:
  """Full line scan. Declared ASYNCHRONOUS; replaces the placeholder verdict."""
  import time
  time.sleep(25)
  return VerdictResult(verdict="a faulty splitter on the street cabinet")


@flows.tool(flow="linecheck")
def advise(verdict: str = "", contact_pref: str = "") -> AdviceResult:
  """Tell the caller what to do about the verdict. MUST NOT run on the placeholder."""
  return AdviceResult(
      advice=f"The scan found {verdict}. I've booked an engineer and we'll "
             f"confirm by {contact_pref}.")


linecheck = flows.Flow("linecheck", root_agent="linecheck_agent")

linecheck.add(
    flows.user_slot(
        "account",
        ask="What's your account number?",
        hint="the account number on the bill",
    ),
    # TWO producers, so it is authored as a raw dict — `result_slot` names a single
    # task, and the validator (rightly) wants every writer declared.
    {"name": "verdict", "source": ["task:QuickCheck", "task:DeepScan"]},
    # Unrelated to the scan, so it is collected DURING the wait — the guard must not
    # stall ordinary collection.
    flows.user_slot(
        "contact_pref",
        ask="While the full scan runs, would you prefer a text or an email?",
        hint="text or email",
    ),
    flows.result_slot("advice", "Advise"),
)

linecheck.task(flows.task(
    "QuickCheck", "quick_check", ["account"], "verdict",
    out_key="verdict",
))

linecheck.task(flows.task(
    "DeepScan", "deep_scan", ["account"], "verdict",
    out_key="verdict",
    awaits=flows.awaits(
        say="Let me run the full scan on your line.",
        while_waiting=["Still scanning, thanks for bearing with me."],
        max_turns=6,
        on_timeout={
            "say": "That scan is taking too long to finish here.",
            "then": {"tool": "transfer_to_human"},
        },
    ),
))

# The consumer. `verdict` is ALREADY filled (by QuickCheck) while DeepScan is in flight,
# so without the stale-output guard this fires early and reads the placeholder out as
# though it were the finding.
linecheck.task(flows.task(
    "Advise", "advise", ["verdict", "contact_pref"], "advice",
    out_key="advice", terminal=True, then_say="{advice}",
))

app = flows.App(
    root_flow=linecheck,
    app_display_name="Async Stale Consumer",
)


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./async_stale_consumer_app")
    print("built: ./async_stale_consumer_app")
