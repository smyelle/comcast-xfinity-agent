"""Line diagnostics: what happens when an asynchronous backend never answers.

The companion to `async_tool.py`, which shows the happy path. This one shows the two
things that example deliberately leaves out — a SILENT wait, and the deadline.

CES enforces no timeout of its own on an ASYNCHRONOUS tool. If the backend hangs, the
completion turn simply never arrives, and without a bound the flow would wait forever.
`awaits.max_turns` is that bound, which is why the builder refuses to omit it.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "my line is dead"               asks for the number
    "5551234567"                    fires run_line_test -> "pending"
                                    says NOTHING (silent wait)
    "hello? are you there"          deadline reached -> apologises, hands to a human

Two differences from `async_tool.py`:

* **No `say`.** The wait is silent: the engine emits the same tick an empty `no_input`
  reprompt produces, so `before_model` returns an empty response, the caller hears
  nothing and the model is suppressed rather than filling the gap with invention. Right
  for a wait measured in a second or two; wrong for a long one, which is why the
  activation example speaks.
* **`max_turns=1`, and the tool sleeps past it.** `run_line_test` sleeps well beyond one
  turn, so the deadline always wins and the give-up path is what you actually observe.
  In a real agent you would size `max_turns` to the backend's p99, not to force this.

`on_timeout` takes the ordinary `on_exhaust` vocabulary, so giving up routes through the
same disposition machinery as the no-input and no-match ladders: a line to speak and a
tool to fire.

Build + validate offline:
    python -m examples.async_timeout      # emits ./async_timeout_app
"""

from pydantic import BaseModel, Field

import flows


class LineTestResult(BaseModel):
  verdict: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="diagnostics", asynchronous=True)
def run_line_test(msisdn: str = "") -> LineTestResult:
  """Run a physical line test. Declared ASYNCHRONOUS, and deliberately slow."""
  # Imports go INSIDE a tool body: only the function is rendered into the CES tool file,
  # so a module-level `import time` is not carried and the body dies with a NameError —
  # which, for an async tool, surfaces as a `failed with error` completion envelope.
  import time
  # Far longer than the one turn `awaits.max_turns` allows, so the completion cannot
  # beat the deadline. This is the demo's whole point; a real tool would not do this.
  time.sleep(90)
  return LineTestResult(verdict="The line tested clean.")


@flows.tool(flow="diagnostics")
def report_verdict(verdict: str = "") -> LineTestResult:
  """Report a line-test verdict that arrived in time."""
  return LineTestResult(verdict=verdict)


diagnostics = flows.Flow("diagnostics", root_agent="diagnostics_agent")

diagnostics.add(
    flows.user_slot(
        "msisdn",
        ask="What's the number you're having trouble with?",
        hint="the affected mobile number",
    ),
    flows.result_slot("verdict", "line_test"),
    flows.result_slot("reported", "report"),
)

diagnostics.task(flows.task(
    "line_test", "run_line_test", ["msisdn"], "verdict",
    out_key="verdict",
    awaits=flows.awaits(
        # No `say`: hold silently. The caller hears nothing while the test runs.
        max_turns=1,
        on_timeout={
            "say": "Sorry — that test is taking longer than it should. "
                   "Let me get you to someone who can look at it directly.",
            "then": {"tool": "transfer_to_human"},
        },
    ),
))

# Only reached when the backend beats the deadline. Kept so the flow is well-formed
# either way — the awaiting task cannot itself be terminal.
diagnostics.task(flows.task(
    "report", "report_verdict", ["verdict"], "reported",
    out_key="verdict", terminal=True, then_say="{verdict}",
))

app = flows.App(
    root_flow=diagnostics,
    app_display_name="Async Timeout",
)


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./async_timeout_app")
    print("built: ./async_timeout_app")
