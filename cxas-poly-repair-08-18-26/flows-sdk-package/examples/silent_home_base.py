"""A SINGLE-AGENT router whose home-base flow is SILENT (nothing can route into it).

Most flows are entered because the caller says something that matches a cue, and the
model calls that flow's setter. A SILENT flow has no setter at all — every slot is
written by a task's output, so there is nothing the model can call to enter it. A
diagnostic fan-out is the canonical case: the caller says "my internet is out" and the
agent should immediately run its checks and report, without asking anything first.

Because nothing can route INTO such a flow, it has to be the flow a cold turn already
resolves to. That is what `router_flow(default_flow=...)` declares, and what the build
layer wires up:

  * `flow_config_map` — lets the blessed before_agent resolver switch the active config
    off the router when the `active_flow` gate is set. WITHOUT this a single-agent router
    never leaves the router config and no child DAG ever drives.
  * `default_flow` — the home-base flow the resolver seeds the gate to on a cold turn, so
    the diagnostic runs on the turn carrying the caller's opening utterance instead of
    costing a separate routing turn.
  * `silent_flow_configs` — INFERRED (a flow with no setter is silent). Keeps the other
    flows' setters hidden while the silent flow is active, so the engine drives it
    end-to-end instead of the model calling a sibling flow's setter mid-diagnostic.
  * `router_hide_tools` — on a router turn, hide the flow-specific tools so the model
    routes instead of "doing" the request by calling a flow tool directly.

`billing` is an ordinary collection flow alongside it: it HAS a setter, so it is not
silent, it is reached by cue ("my bill"), and it is not the home base.

Build + validate offline:
    python -m examples.silent_home_base        # emits ./silent_home_base_app
"""

from pydantic import BaseModel, Field

import flows


class DiagnosticReport(BaseModel):
  """What the diagnostic sweep found."""

  line_status: str = Field(description="healthy | degraded | down")
  summary: str = Field(description="One-sentence caller-facing summary.")
  success: bool = Field(default=True, description="Whether the sweep completed.")


class BillSummary(BaseModel):
  amount_due: str = Field(description="Formatted amount, e.g. '$84.20'.")
  success: bool = Field(default=True, description="Whether the lookup succeeded.")


@flows.tool(flow="diagnose")
def run_diagnostics() -> DiagnosticReport:
  """Run the line checks. Takes NO arguments — the caller is never asked anything."""
  return DiagnosticReport(line_status="degraded",
                          summary="I'm seeing degraded signal on your line.")


@flows.tool(flow="billing")
def lookup_bill(account_number: str) -> BillSummary:
  """Look up the current balance for an account."""
  return BillSummary(amount_due="$84.20")


# ── The SILENT home-base flow. Note there is no user_slot and no setter anywhere:
# `line_status`/`summary` are written by the task's outputs, so the model has no way to
# enter this flow. It runs because it is the router's `default_flow`.
diagnose = flows.Flow("diagnose", bootstrap={"reset_on_complete": True})
diagnose.add(flows.result_slot("line_status", "RunDiagnostics"),
             flows.result_slot("summary", "RunDiagnostics"))
diagnose.task("RunDiagnostics", "run_diagnostics", [], "line_status",
              out_key="line_status", extra_outputs={"summary": "summary"},
              terminal=True, then_say="{summary}")

# ── An ordinary collection flow: it HAS a setter, so it is not silent and is entered
# the normal way, by cue.
billing = flows.Flow("billing", bootstrap={"reset_on_complete": True})
billing.add(flows.user_slot("account_number", ask="What's your account number?"),
            flows.result_slot("amount_due", "LookupBill"))
billing.task("LookupBill", "lookup_bill", ["account_number"], "amount_due",
             out_key="amount_due", terminal=True,
             then_say="Your balance is {amount_due}.")

router = flows.router_flow(
    "support_host",
    ["diagnose", "billing"],
    default_flow="diagnose",          # <- home base: the silent flow
    route_cues={"billing": ["my bill", "balance", "invoice", "how much do I owe"]},
    root_agent="Support_Agent",
)

app = flows.App(
    root_flow=router,
    extra_flows=[diagnose, billing],
    app_display_name="Silent Home Base",
    agent_instruction=(
        "You are a support agent. Follow the slot-filling framework directives exactly."
    ),
)


def _show_runtime_vars(out_dir: str) -> None:
  """Print the routing state vars the build layer emitted, to show the wiring."""
  import json
  import os

  with open(os.path.join(out_dir, "app.json")) as f:
    decls = json.load(f).get("variableDeclarations", [])
  interesting = ("flow_config_map", "default_flow", "silent_flow_configs",
                 "router_hide_tools")
  for v in decls:
    if v.get("name") in interesting:
      print(f"  {v['name']:20} = {v['schema']['default']}")


if __name__ == "__main__":
  out = flows.build_app(app, "./silent_home_base_app")
  print(f"built: {out.app_dir if hasattr(out, 'app_dir') else './silent_home_base_app'}")
  _show_runtime_vars("./silent_home_base_app")
