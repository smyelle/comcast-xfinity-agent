"""A backend that runs for ninety seconds, and the one line that stops it vanishing.

The shape this exists for. `async_tool.py` covers a backend that answers turns later, and
`async_timeout.py` covers one that never answers at all. Both assume the thing eventually
either reports or does not. This example is the case in between, and it is the one that
costs an afternoon to diagnose:

    A tool body is killed at 60 seconds unless its resource says otherwise,
    and being killed is SILENT.

No exception. No failed result. No partial write. A synchronous caller gets nothing back
and an asynchronous one never sends its completion turn — so `awaits` has no completion to
time out on, `on_timeout` never fires, and the flow waits for something that is not coming.
From the outside it reads as a tool that does nothing, which sends you looking at
dispatch, at registration, at the DAG: everywhere except the clock.

The fix is one argument:

    @flows.tool(flow="claim_review", asynchronous=True, timeout=180)

`timeout` is emitted as `timeout: "180s"` on the tool resource. It applies to synchronous
and asynchronous bodies alike, and each tool is enforced against its own value even when
several run at once, so a slow tool's allowance neither leaks to the tools beside it nor
constrains them.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "I need to check my claim"      asks for the claim number
    "four four one nine"            fires the review -> "pending", says the
                                    holding line, and hands the turn back
    "any news?"                     still running: the wait ladder speaks
    (completion lands, ~90s)        reads the verdict out and closes

Two things worth copying:

* **`timeout` and `awaits` are not alternatives.** They bound different things.
  `timeout` is how long the BODY may run before the platform kills it, in seconds.
  `awaits.max_turns` is how long the FLOW will wait for a completion, in caller turns,
  because the engine has no clock. A tool needs both: raise the timeout so the work can
  finish, and bound the wait so a genuinely wedged backend cannot hold the call open.
* **Size the timeout above the backend's p99, not above its average.** The cost of an
  over-generous timeout is nothing — it is a ceiling, not a delay, and a tool that
  finishes early reports early. The cost of one set too low is a result that silently
  never arrives.

Driven live, this app, over the text channel:

    t=  5.3s  "four four one nine"
              -> I'm re-reading the documents on that claim now - it takes a
                 minute or two, so stay with me.          (awaits.say)
    t= 30.8s  "any news?"
              -> Still going through them, thanks for waiting.   (while_waiting)
    t=108.5s  "any news?"
              -> Good news - everything on file checks out, so the claim is
                 cleared to pay.                          (completion, then_say)

The body ran its full ninety seconds and reported. Delete the `timeout=180` and the same
app goes quiet at sixty: the review never completes, no envelope arrives, and the flow
rides its `while_waiting` ladder to `on_timeout` having learned nothing.

Not claimed: offline, no timeout is ever enforced. `validate_app` and the engine simulator
both run this body instantly, and a body sleeping for an hour validates clean, builds
clean and deploys clean. The cross-cutting evidence — that the field bounds in both
directions, governs synchronous bodies too, and holds per tool while several overlap — is
`ces-probes` 105 and 107.

Build + validate offline:
    python -m examples.tool_timeout      # emits ./tool_timeout_app
"""

from pydantic import BaseModel, Field

import flows


class ReviewVerdict(BaseModel):
  verdict: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="claim_review", asynchronous=True, timeout=180)
def run_document_review(claim_number: str = "") -> ReviewVerdict:
  """Re-read every document on the claim and score it. About ninety seconds.

  Declared ASYNCHRONOUS so the caller keeps the floor, and `timeout=180` because the body
  outruns the 60-second default. Without that argument this function is killed at sixty
  seconds and never reports — the flow then sits on its wait ladder until `max_turns`
  runs out, having been told nothing at all.
  """
  # Imports go INSIDE a tool body: only the function is rendered into the CES tool file,
  # so a module-level `import time` is not carried and the body dies with a NameError.
  import time
  time.sleep(90)
  return ReviewVerdict(
      verdict="everything on file checks out, so the claim is cleared to pay")


@flows.tool(flow="claim_review")
def close_review(verdict: str = "") -> ReviewVerdict:
  """Report a verdict that arrived in time."""
  return ReviewVerdict(verdict=verdict)


def build() -> flows.App:
  """The review flow: one slow backend, bounded twice.

  Returns:
    The assembled app.
  """
  review = flows.Flow("claim_review", root_agent="Acme_Claims")
  review.add(
      flows.user_slot(
          "claim_number",
          ask="What's the claim number?",
          hint="the number on the claim being reviewed"),
      flows.result_slot("verdict", "review"),
      flows.result_slot("closing", "close"),
  )

  review.task(flows.task(
      "review", "run_document_review", ["claim_number"], "verdict",
      out_key="verdict",
      awaits=flows.awaits(
          say="I'm re-reading the documents on that claim now — it takes a minute or"
              " two, so stay with me.",
          # Turns, not seconds: the engine has no clock. This bounds the WAIT; the
          # tool's own `timeout` bounds the BODY.
          max_turns=6,
          while_waiting=["Still going through them, thanks for waiting."],
          on_timeout={
              "say": "That review is taking longer than it should. Let me get you to"
                     " someone who can look at it directly.",
              "then": {"tool": "transfer_to_human"},
          },
      ),
  ))

  review.task(flows.task(
      "close", "close_review", ["verdict"], "closing",
      out_key="verdict", terminal=True,
      then_say="Good news — {verdict}."))

  return flows.App(root_flow=review, app_display_name="Acme Claims Review")


app = build()


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for warning in warnings:
    print("warning:", warning)
  for error in errors:
    print("error:", error)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./tool_timeout_app", overwrite=True)
    print("built: ./tool_timeout_app")
