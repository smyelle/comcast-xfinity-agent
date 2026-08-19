"""Work that outlives the call: a job on a service the agent does not deploy.

The shape this exists for. A caller asks for something that takes minutes — a report to
crunch, a batch to reconcile, an export to build. The agent cannot hold the line for it
and must not pretend to: a blocking tool yields no turns, so nothing can be said while it
runs, and CES kills a tool body at sixty seconds anyway.

    caller                             what the flow does
    ---------------------------------  --------------------------------------------
    "I need the Q1 numbers"            asks which account
    "A-1042"                           starts the job, gets a handle back in under a
                                       second, says it will take a few minutes
    (says nothing)                     the turn ended, so the platform's inactivity
                                       timeout produces a tick; the engine polls the
                                       job and speaks the next reassurance line
    (says nothing)                     polls again, still running, next line
    "any luck?"                        a caller turn is a turn too — polls on that one
    (says nothing)                     the job lands; the report is read out

Each piece earns its place here:

* `openapi_toolset` with NO `spec=`. That inversion is the point: the agent declares the
  contract and the service implements it, rather than the agent consuming a spec someone
  else published. The emitted spec is generated from the declaration below, which is
  what the service team builds against.
* `remote_tool(...)` rather than a tool with a body. There is no `def` here and nowhere
  to put one — the implementation deploys on its own schedule, in its own repository.
* `timeout=600` in SECONDS, not turns. Everywhere else in the engine a wait is bounded
  in turns because the engine has no clock; a service has one, so the budget is stated
  the way an author thinks about it and enforced where it can actually stop the work.
* `awaits(while_waiting=[...])` supplies only the copy. The polling itself is the
  engine's and appears nowhere in this file.
* `inactivityTimeout` on the app is what makes the whole thing move. Ticks are the
  engine's clock; with none, nothing polls and no reassurance is ever spoken.
* `mock=` so this file is drivable, and CI-checkable, with no service deployed at all.

Not claimed: that the job's own failure modes are invisible. They are not, deliberately
— a service can lose a job when it redeploys, and that is a different thing from the job
failing. `on_failure` branches on the code below.

    PYTHONPATH=src python -m examples.remote_tool
"""

import flows
from pydantic import BaseModel, Field

# The service. Its URL differs per environment, so it lives in `environment.json`
# rather than in this file; `env_scoped` writes the marker and keeps the real value out
# of the emitted app dir.
SERVICE = "https://remote-report-demo-555355609568.us-central1.run.app"

reports = flows.openapi_toolset(
    "report_service",
    base_url=SERVICE,
    auth=flows.service_agent_auth(),
    env_scoped=True,
)


def quarter_report():
  """What the job answers with, chosen by the session driving it.

  Inlined into the emitted `build_report__status_mock` tool, where CES provides
  `context` — so this is the same code whether it runs here or on the platform, and it
  is editable in the console without a rebuild.
  """
  if (context.variables or {}).get("scenario") == "empty":  # noqa: F821
    return {"headline": "No activity on the quarter", "rows": 0}
  return {"headline": "Revenue up 4% on the quarter", "rows": 8213}


# The contract. Parameters are the slots in, outputs the slots out, and the values are
# TYPES — unlike `api_tool`, where they rename onto an existing spec's wire names and
# the spec supplies the types. Here the declaration IS the spec.
build_report = flows.remote_tool(
    "build_report", reports, "buildReport",
    params={"account": str, "since": str, "duration_seconds": int},
    outputs={"headline": str, "rows": int},
    description="Build the quarterly revenue report for one account.",
    timeout=600,
    # Resolves after three turns rather than at once, so a drive of this example
    # exercises the WAIT — the reassurance ladder, and the landing — with no service
    # deployed. A plain dict would resolve inline and prove only the mapping.
    #
    # A CODE BLOCK rather than data, because the answer depends on the session and a
    # static payload can only ever be one answer. It takes no arguments: a poll carries
    # the handle and nothing else, so what it reads is `context`. Engaged by
    # `mock_apis` on the App below, which is also how one deployed app is driven mocked
    # by an eval and live by a caller.
    mock=flows.after_turns(3, quarter_report),
)

report = flows.Flow("report", root_agent="report_agent")
report.add(
    flows.user_slot("account", ask="Which account should I run the report for?"),
    # Fixed inputs the caller never supplies. `default` fills them before the
    # task is eligible, so the job starts on the turn the account arrives.
    flows.event_slot("since", default="2026-01-01"),
    flows.event_slot("duration_seconds", default=240),
    flows.result_slot("headline", "BuildReport"),
    flows.result_slot("rows", "BuildReport"),
    flows.result_slot("delivered", "Deliver"),
)

report.task(flows.task(
    "BuildReport", build_report, ["account", "since", "duration_seconds"], "headline",
    out_key="headline", extra_outputs={"rows": "rows"},
    awaits=flows.awaits(
        # Turns, not seconds — and on voice a silent caller still produces them, because
        # the inactivity timeout ticks. Forty is generous against a ten-minute job.
        max_turns=40,
        say="Starting that now. It usually takes a few minutes.",
        while_waiting=[
            "Still crunching the numbers.",
            "Not long now, thanks for holding.",
        ],
        on_timeout={"say": "That is taking longer than it should. Let me get someone "
                           "who can look into it.",
                    "then": {"tool": "transfer_to_human"}},
    ),
    # Every branch is keyed on the service's own `error_code`, in the same vocabulary
    # any other tool's failures use. A LOST job is the one worth retrying: nobody said
    # the work failed, only that this attempt is gone — which is what a service
    # redeploying mid-job looks like from here.
    on_failure={
        "max_retries": {"remote_job_lost": 2, "_default": 0},
        "retry_say": {"remote_job_lost": "Let me start that over.", "_default": ""},
        "on_exhaust": {
            "say": {
                "remote_timeout": "That report is taking far longer than it should "
                                  "today.",
                "remote_bad_handle": "I am not able to start that report right now.",
                "_default": "I cannot pull that report right now.",
            },
            "then": {"tool": "transfer_to_human"},
        },
    },
))

report.task(flows.task(
    "Deliver", "deliver_report", ["headline", "rows"], "delivered",
    terminal=True,
    then_say="Here it is: {headline}, across {rows} rows.",
))


class Delivered(BaseModel):
  delivered: str = ""
  success: bool = Field(default=True)


@flows.tool(flow="report")
def deliver_report(headline: str, rows: int) -> Delivered:
  """Hand the finished report to the caller."""
  return Delivered(delivered="true")


app = flows.App(
    root_flow=report,
    toolsets=[reports],
    app_display_name="remote-tool-demo",
    model="gemini-composite-v1",
    # The clock the whole feature runs on. A held job produces no turns by itself: the
    # platform emits one only when this elapses, and every poll and every reassurance
    # line rides those turns. With no timeout declared the job is never checked on and
    # the waiting copy below is never spoken.
    app_settings={"audioProcessingConfig": {"inactivityTimeout": "20s"}},
    # The mock above answers only when this is on. It is a session VARIABLE with this as
    # its default, not a build-time constant, so the same pushed app is driven mocked by
    # an eval (`mock_apis: true`) and against the real service by a caller who sets it
    # false — which is how this example was both CI-checked and driven live.
    mock_apis=True,
    # DECLARED, or the mock cannot see it. A session variable the caller seeds only
    # reaches `context.variables` if the app declares it — driven live without this and
    # the `empty` arm answered with the busy quarter, silently, which reads exactly like
    # a mock that ignores the session.
    variables=[{"name": "scenario",
                "description": "Which recorded quarter the mocked report answers with.",
                "schema": {"type": "STRING", "default": ""}}],
)


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./remote_tool_app")
    print("built: ./remote_tool_app")
