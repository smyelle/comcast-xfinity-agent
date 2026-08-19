"""Multi-agent steering — routing by description AND mid-call intent SWITCHING.

Two slot-filling specialists behind a steering host:

    Support_Host  (routes by the routes' descriptions; runs no engine)
      ├─ Billing_Agent       (looks up the account balance)
      └─ Tech_Support_Agent  (runs a connection check)

Two behaviours this shows, from ONE set of route descriptions:

* Intent DETECTION — the host routes the opening turn to the right specialist by the
  MEANING of each `Agent.description` (the same `<routing>` block the single-agent router
  emits; see examples/steering.py).
* Intent SWITCHING — `robust_switching` (default) makes each specialist run the intent-first
  classifier every turn, so "actually, my internet is down" MID-billing is detected as a
  switch by meaning and hands off to Tech_Support instead of cancelling. The classifier is
  description-driven and domain-neutral (it renders the same `flow="X" — <description>`
  lines the router turn uses). `account_number` is a SHARED slot, so an account already
  collected before a switch carries across (the caller is not re-asked for it).

Build + validate offline:
    python -m examples.steering_multi_agent
"""

from pydantic import BaseModel, Field

import flows


class Balance(BaseModel):
  amount_due: str = Field(description="Formatted balance, e.g. '$72.40'.")
  success: bool = True


class LineCheck(BaseModel):
  summary: str = Field(description="One-sentence connection result.")
  success: bool = True


@flows.tool(flow="billing")
def lookup_balance(account_number: str) -> Balance:
  """Look up the current balance for an account."""
  return Balance(amount_due="$72.40")


@flows.tool(flow="tech_support")
def run_line_check(account_number: str) -> LineCheck:
  """Run a connection check for the account."""
  return LineCheck(summary="I ran a check and your line looks degraded — let's restart it.")


billing = flows.Flow("billing", root_agent="Billing_Agent")
billing.add(
    flows.user_slot("account_number", "What's your account number?"),
    flows.result_slot("amount_due", "lookup"),
)
billing.task("lookup", "lookup_balance", ["account_number"], "amount_due",
             out_key="amount_due", terminal=True,
             then_say="Your balance is {amount_due}.",
             condition=flows.has("account_number"))

tech = flows.Flow("tech_support", root_agent="Tech_Support_Agent")
tech.add(
    # Shared slot name with billing ON PURPOSE — an account given before a switch carries
    # across, so the caller is not re-asked (a build warning notes the shared slot).
    flows.user_slot("account_number", "What's your account number?"),
    flows.result_slot("line_summary", "check"),
)
tech.task("check", "run_line_check", ["account_number"], "line_summary",
          out_key="summary", terminal=True, then_say="{line_summary}",
          condition=flows.has("account_number"))

billing_agent = flows.Agent(
    "Billing_Agent", flow=billing,
    description="the caller wants to understand, question, or pay a charge on their bill")
tech_agent = flows.Agent(
    "Tech_Support_Agent", flow=tech,
    description="the caller's internet or WiFi is down, slow, or dropping")

host = flows.HostRouter(
    "Support_Host",
    routes={"billing": billing_agent, "tech_support": tech_agent},
    # robust_switching defaults True: each specialist runs the intent-first classifier so a
    # mid-call switch is detected by MEANING (not just a keyword).
)

app = flows.App(
    host=host,
    agents=[billing_agent, tech_agent],
    app_display_name="Steering Multi-Agent Demo",
    model="gemini-3.5-flash",
)


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  assert errors == [], errors
  flows.build_app(app, "./steering_multi_agent_app", overwrite=True)
  print("built -> ./steering_multi_agent_app")
