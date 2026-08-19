"""Once a router picks a flow, only THAT flow is reachable.

A single-agent router emits every flow's DAG onto one agent. That is what makes routing
cheap — no transfer, no second agent — but it also means the agent is carrying the tool
surface of every flow it can route to, on every turn.

The engine hides the flow-specific surface on the ROUTING turn, so the model routes
instead of "doing" the request directly. The turns AFTER the routing turn are the
interesting ones: the caller is inside `diagnose`, and `reboot_device` belongs to a
sibling flow the router did not choose. Nothing the caller says should be able to reach
it, and no free turn the model takes should either.

Three flows, deliberately chosen so a leak is unmistakable rather than cosmetic:

  * `diagnose`  — the home base. Silent (no setter), so a cold turn runs the checks.
  * `reboot`    — restarts the caller's equipment. Entering this flow uninvited is a
                  real action taken on a real account, not a wording slip.
  * `billing`   — an ordinary collection flow, reached by cue.

What keeps them apart, all emitted by the build layer with nothing to author:

  * `router_hide_tools`   — on a routing turn, the flow-specific tools are hidden.
  * `silent_flow_configs` — while the silent flow drives, sibling setters stay hidden.
  * the `{flow}_dag` config loaders are hidden on EVERY turn, for every flow rather
    than only the active one. They are how the engine fetches a flow's config, and a
    visible loader is an entrance: a model given a free turn will call one and arrive
    inside a flow nobody routed to.

Build + validate offline:
    python -m examples.router_flow_isolation      # emits ./router_flow_isolation_app
"""

from pydantic import BaseModel, Field

import flows


class DiagnosticReport(BaseModel):
  """What the line checks found."""

  line_status: str = Field(description="healthy | degraded | down")
  summary: str = Field(description="One-sentence caller-facing summary.")
  success: bool = Field(default=True, description="Whether the sweep completed.")


class RebootResult(BaseModel):
  """The outcome of restarting the caller's equipment."""

  rebooted: bool = Field(description="Whether the restart was actually sent.")
  message: str = Field(description="One-sentence caller-facing outcome.")
  success: bool = Field(default=True, description="Whether the call completed.")


class BillSummary(BaseModel):
  amount_due: str = Field(description="Formatted amount, for example '$84.20'.")
  success: bool = Field(default=True, description="Whether the lookup succeeded.")


@flows.tool(flow="diagnose")
def run_diagnostics() -> DiagnosticReport:
  """Run the line checks. Takes NO arguments — the caller is never asked anything."""
  return DiagnosticReport(line_status="healthy",
                          summary="Everything on our side looks healthy.")


@flows.tool(flow="reboot")
def reboot_device() -> RebootResult:
  """Restart the caller's equipment. Only ever correct when the caller asked for it."""
  return RebootResult(rebooted=True,
                      message="I've sent the restart. It takes about five minutes.")


@flows.tool(flow="billing")
def lookup_bill(account_number: str) -> BillSummary:
  """Look up the current balance for an account."""
  return BillSummary(amount_due="$84.20")


# ── Home base. No setter anywhere, so nothing can route INTO it and the checks run on
# the turn carrying the caller's opening words.
diagnose = flows.Flow("diagnose", bootstrap={"reset_on_complete": True})
diagnose.add(flows.result_slot("line_status", "RunDiagnostics"),
             flows.result_slot("summary", "RunDiagnostics"))
diagnose.task("RunDiagnostics", "run_diagnostics", [], "line_status",
              out_key="line_status", extra_outputs={"summary": "summary"},
              terminal=True, then_say="{summary}")

# ── The flow that must never be entered uninvited — but MUST still be reachable when
# the caller asks for it. It carries a `user_slot`, so it has a setter and is an
# ordinary routable flow. A flow with no setter at all is inferred SILENT, which makes
# it unreachable by routing: correct for a home base, wrong for an action the caller
# requests by name.
reboot = flows.Flow("reboot", bootstrap={"reset_on_complete": True})
reboot.add(flows.user_slot("confirm_reboot",
                           ask="Restarting takes about five minutes and your service "
                               "will drop while it happens. Shall I go ahead?"),
           flows.result_slot("reboot_message", "RebootDevice"))
reboot.task("RebootDevice", "reboot_device", [], "reboot_message",
            out_key="message", terminal=True, then_say="{reboot_message}",
            condition={"slot": "confirm_reboot", "filled": True})

billing = flows.Flow("billing", bootstrap={"reset_on_complete": True})
billing.add(flows.user_slot("account_number", ask="What's your account number?"),
            flows.result_slot("amount_due", "LookupBill"))
billing.task("LookupBill", "lookup_bill", ["account_number"], "amount_due",
             out_key="amount_due", terminal=True,
             then_say="Your balance is {amount_due}.")

router = flows.router_flow(
    "support_host",
    ["diagnose", "reboot", "billing"],
    default_flow="diagnose",
    route_cues={
        "reboot": ["restart my router", "reboot my modem", "power cycle"],
        "billing": ["my bill", "balance", "invoice", "how much do I owe"],
    },
    root_agent="Support_Agent",
)

app = flows.App(
    root_flow=router,
    extra_flows=[diagnose, reboot, billing],
    app_display_name="Router Flow Isolation",
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
  out_dir = "./router_flow_isolation_app"
  flows.build_app(app, out_dir)
  print(f"built: {out_dir}")
  _show_runtime_vars(out_dir)
