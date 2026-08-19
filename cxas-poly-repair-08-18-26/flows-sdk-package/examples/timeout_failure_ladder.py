"""Answer a slow backend differently from a broken one.

Every way a tool can fail arrives as the same shape: a result with no `success` key, which
routes into `on_failure`. So the ladder fires correctly and says the wrong thing — "let me
try once more" to a backend that is down, or "I can't reach that system" to one that merely
needed another ten seconds. Those are opposite responses and the caller can hear the
difference.

The platform does say which happened. A killed body reports `Tool execution failed: Code
execution timed out`, a raising one `Python function execution failed`, and a fire missing
a required parameter `Missing parameter '<name>'`. The framework turns each into an
`error_code`, and a disposition may be keyed by it:

    on_failure={"retry_say": {"timeout": "...", "_default": "..."}}

`_default` is the branch for a reason you did not name — including every platform failure
whose shape nobody has enumerated, which is why the mapping never guesses by elimination.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "policy 4417"                   deep_assessment is killed at its own bound
                                    -> timeout branch: offers a faster check
    (retries, fails again)          -> timeout exhaust line, not the generic one
    "run the estimate"              quick_estimate raises
                                    -> tool_crash branch: does not offer to wait
    "check the archive"             archive_lookup returns an unnamed code
                                    -> _default branch

Each piece earns its place here:

* **Three arms, not two.** A keyed dispatch is only proven when the unmatched case goes
  somewhere different from the matched ones. Without `archive_lookup` — an authored code
  the config deliberately does not list — "`_default` fired" cannot be told apart from
  "the keyed lookup did nothing at all".
* **The same three on a SLOT.** A setter reaches a slow backend as often as a task does,
  and `validation.errors` has been keyed by `error_code` all along. What is new there is
  `_default`; before it, an unnamed reason fell to a built-in line an author could not
  replace.
* **Short timeouts.** `timeout=20` rather than a realistic 180, so the demo is drivable.
  The bound is enforced identically at any value.

The deferred half works the same way, and the app proves it: a killed ASYNCHRONOUS body
comes back as a `failed with error` completion on a later turn carrying the same payload,
so the same reason map applies (`ces-probes` 116). Detection is not a property of the
execution mode — only the latency is.

Not claimed: none of this is visible offline. `validate_app` and the simulator run every
body instantly, so all three arms succeed and no branch is ever chosen. The evidence that
the platform reports these three shapes distinctly is `ces-probes` 110 and 113, and the
live drive for this example is recorded in the timeout verification note beside it.

Build + validate offline:

    python -m examples.timeout_failure_ladder   # emits ./timeout_failure_ladder_app
"""

from pydantic import BaseModel, Field

import flows


class Assessment(BaseModel):
  summary: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="claims", timeout=20)
def run_check(policy_number: str = "") -> Assessment:
  """Assess the policy, failing a different way for each SECOND digit.

  One backend, three failures, chosen by the caller. A tool cannot remember which attempt
  it is on — a body that does not return successfully leaves nothing behind (`110`) — so
  the branch has to come from the input, which also makes each one drivable on demand.
  """
  import time
  digits = "".join(c for c in str(policy_number) if c.isdigit())
  marker = digits[1:2]
  if marker == "9":
    time.sleep(45)
  if marker == "8":
    raise ValueError("assessment engine unavailable")
  if marker == "7":
    return {"success": False, "error": True, "error_code": "archive_offline"}
  return Assessment(summary="no open issues on the policy")


@flows.tool(flow="claims", timeout=20)
def set_policy_number(value: str = "") -> dict:
  """Validate a policy number, failing a different way for each FIRST digit.

  Named for the slot rather than renamed with `name=`: a tool resource whose name does not
  match its Python function is dropped silently, and the symptom is a slot that never
  fills while the model politely re-asks forever.
  """
  import time
  digits = "".join(c for c in str(value) if c.isdigit())
  marker = digits[:1]
  if marker == "9":
    time.sleep(45)
  if marker == "8":
    raise ValueError("directory refused the lookup")
  if marker == "7":
    return {"error": True, "error_code": "archive_offline"}
  return {"stored": True, "value": digits}


@flows.tool(flow="claims", asynchronous=True, timeout=20)
def deferred_check(policy_number: str = "") -> Assessment:
  """The same failures, deferred. Outruns its bound, so the platform stops it.

  A deferred kill is NOT silent: it comes back as a `failed with error` completion on a
  later turn, carrying the same payload a synchronous kill returns. So the reason map
  below works here too — the only difference is that the caller hears it a turn later.
  """
  import time
  digits = "".join(c for c in str(policy_number) if c.isdigit())
  marker = digits[2:3]
  if marker == "8":
    raise ValueError("deferred engine unavailable")
  if marker == "7":
    return {"success": False, "error": True, "error_code": "archive_offline"}
  time.sleep(45)
  return Assessment(summary="deferred assessment complete")


def build() -> flows.App:
  """A claims flow whose every failure names itself.

  Returns:
    The assembled app.
  """
  claims = flows.Flow("claims", root_agent="Acme_Claims")
  claims.add(
      flows.user_slot(
          "policy_number",
          ask="What's your policy number?",
          hint="the ten digits on the policy card",
          validation={
              "max_retries": 4,
              "errors": {
                  "timeout": "Our policy directory is slow right now. Read me the"
                             " number again and I'll retry it.",
                  "tool_crash": "The directory refused that lookup. Let's try the"
                                " number once more.",
                  "_default": "I didn't get that one. What's the policy number?",
              },
          }),
      flows.result_slot("assessment", "assess"),
      flows.result_slot("deferred", "deferred_assess"),
      # Requires BOTH halves, or it ends the call the moment the synchronous task
      # resolves and the deferred one never gets a turn to come back on.
      flows.announce(
          "wrap_up",
          ["That's everything on the policy: {assessment}, and {deferred}."],
          requires=["assessment", "deferred"], preempt=True, end=True),
  )

  claims.task(flows.task(
      "assess", "run_check", ["policy_number"], "assessment",
      out_key="summary",
      on_failure={
          "max_retries": 2,
          "retry_say": {
              "timeout": "That check is taking longer than it should. Let me give it"
                         " one more go.",
              "tool_crash": "The assessment engine refused that outright, which is not"
                            " a waiting problem. Trying once more anyway.",
              "_default": "That didn't come back. Let me try once more.",
          },
          "on_exhaust": {
              "say": {
                  "timeout": "Our assessment system is running slowly today, so I'll"
                             " have someone call you back with the result.",
                  "tool_crash": "The assessment engine is broken rather than busy, so"
                                " waiting will not help. Let me get you to someone.",
                  "_default": "I can't reach our assessment system right now.",
              },
          },
      }))

  # The deferred half. Same reason map, same three codes — proof that detection is not a
  # property of the execution mode. `awaits` still earns its place beside it: the ladder
  # handles a leg that came back FAILED, `max_turns` bounds one that never comes back.
  claims.task(flows.task(
      "deferred_assess", "deferred_check", ["policy_number"], "deferred",
      out_key="summary",
      condition=flows.has("assessment"),
      awaits=flows.awaits(
          say="I'm running the deferred check now — give me a moment.",
          max_turns=6,
          while_waiting=["Still waiting on that one."],
          on_timeout={"say": "That check never came back at all."}),
      on_failure={
          "max_retries": 0,
          "on_exhaust": {
              "say": {
                  "timeout": "DEFERRED: over its own budget.",
                  "tool_crash": "DEFERRED: refused outright.",
                  "_default": "DEFERRED: unnamed reason.",
              },
          },
      }))

  return flows.App(root_flow=claims, app_display_name="Acme Claims Assessment")


app = build()


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  for line in errors + warnings:
    print(" ", line)
  if not errors:
    flows.build_app(app, "./timeout_failure_ladder_app", overwrite=True)
