"""Guardrails on a two-specialist app — and why scope is the decision that matters.

An order-status host routes to a billing specialist and a returns specialist. Four
guardrails, chosen to show each type doing the job it is actually good at:

  * `safety` + `prompt_guard` — the baseline every app should carry.
  * `blocklist` — a deterministic stop for a card number the agent must never read back.
    No model, no latency, no false positives.
  * `policy` — a rule that needs judgement, scoped to the CALLER's turn so it prevents.

The one an author gets wrong
----------------------------
`scope="agent"` looks like the obvious choice for "don't let the agent say X". It is
judged after the model has already produced the line, and on `gemini-3.1-flash-live` the
caller has heard it by then — they get the offending sentence AND then the refusal
(measured in audio, ces-probes `102`). `scope="user"` is judged before the model runs and
prevents on both models (`103`).

So: prefer `scope="user"` when the rule can be decided from what the caller asked. When
it genuinely cannot — a hallucinated confirmation is not predictable from the caller's
turn — use `scope="agent"` with `transfer_to(...)`, which changes what happens next
rather than trying to un-say a line. `flows lint` warns when it sees the other pairing.

Build + validate offline:
    python -m examples.guarded_orders        # emits ./guarded_orders_app
"""

from pydantic import BaseModel, Field

import flows


class LookupResult(BaseModel):
  summary: str = Field(description="What to tell the caller")
  success: bool = True


@flows.tool(flow="billing")
def load_account(account_id: str) -> LookupResult:
  """Load a billing account so its balance can be discussed."""
  return LookupResult(summary=f"account {account_id} is open and in good standing")


@flows.tool(flow="returns")
def start_return(order_id: str) -> LookupResult:
  """Start a return for an order."""
  return LookupResult(summary=f"a return is started for order {order_id}")


# ── Flows ────────────────────────────────────────────────────────────────────
#
# Each specialist collects one slot and then RUNS A TASK that reads a result back. The
# task is not decoration: a config with slots and no task fills the slot and then has
# nowhere to go, and the caller gets the platform's "Hmm, I'm having trouble with that"
# — which `validate_app` warns about ("Config has no 'tasks'") and which no offline
# check can see, because the app builds perfectly.

billing_flow = flows.Flow("billing", root_agent="Billing_Agent")
billing_flow.add(
    flows.user_slot("account_id", "What's your account number?"),
    flows.result_slot("account_summary", "load"),
)
billing_flow.task("load", "load_account", ["account_id"], "account_summary",
                  out_key="summary", terminal=True,
                  then_say="Thanks — {account_summary}.",
                  condition=flows.has("account_id"))

returns_flow = flows.Flow("returns", root_agent="Returns_Agent")
returns_flow.add(
    flows.user_slot("order_id", "What's the order number you'd like to return?"),
    flows.result_slot("return_summary", "start"),
)
returns_flow.task("start", "start_return", ["order_id"], "return_summary",
                  out_key="summary", terminal=True,
                  then_say="All set — {return_summary}.",
                  condition=flows.has("order_id"))

# ── Agents ───────────────────────────────────────────────────────────────────

# A human to hand off to when a guardrail catches something the agent should not have
# said. Declared as a route so it is a real agent in the app — `transfer_to` takes the
# object, so a typo is a build error rather than a guardrail that fires and goes nowhere.
human_flow = flows.Flow("human", root_agent="Human_Agent")
human_flow.add(flows.announce("wait", ["Connecting you to someone now."], end=True))
human = flows.Agent(name="Human", flow=human_flow)

billing = flows.Agent(
    name="Billing",
    flow=billing_flow,
    # Scoped to the billing specialist only: the returns agent has no business being
    # judged against a payments rule, and the host router even less so.
    guardrails=[
        flows.policy(
            "no_unverified_balance",
            "### CRITICAL RULE\n"
            "- Trigger when the caller asks for balance or payment detail before they\n"
            "  have given an account number. Err on the side of triggering.\n\n"
            "### TRIGGER CRITERIA\n"
            "FLAG the message if it asks for account-specific financial information.\n"
            "**Explicit:** 'what's my balance', 'how much do I owe'.\n"
            "**Implicit (CRITICAL):** 'am I paid up', 'did my payment go through'.\n\n"
            "### DO NOT FLAG\n"
            "General questions about billing dates, fees or how to pay — none of those\n"
            "need an account.",
            scope="user",
            on_trigger=flows.generate(
                "Explain that you need their account number first, and ask for it."),
        ),
    ],
)

returns = flows.Agent(name="Returns", flow=returns_flow)

host = flows.HostRouter(
    name="Front_Desk",
    routes={"billing": billing, "returns": returns, "human": human},
    welcome_message="Thanks for calling. Are you calling about billing or a return?",
)

# ── The app ──────────────────────────────────────────────────────────────────

app = flows.App(
    host=host,
    agents=[billing, returns, human],
    app_display_name="Guarded Orders",
    guardrails=[
        # The baseline. Both are what the console creates by default; declaring them
        # here means a freshly created app has them without anyone remembering to.
        flows.safety("Safety", level="balanced"),
        flows.prompt_guard("Prompt Guard"),

        # Deterministic and free: a card number must never be read back, and there is
        # no judgement call to make, so no model is involved.
        flows.blocklist(
            "Card Numbers",
            [r"\b(?:\d[ -]*?){13,16}\b"],
            match="regex",
            scope="agent",
            on_trigger=flows.respond("Sorry — I can't read a card number back."),
        ),

        # The case `scope="user"` CANNOT cover: whether the agent invented a refund
        # confirmation is not knowable from what the caller asked. So it is judged on
        # the response, and paired with a transfer — on a live model the line is already
        # out, and getting the caller to a human is the part still worth doing.
        flows.policy(
            "no_false_refund",
            "### CRITICAL RULE\n"
            "- Trigger when the agent states or implies a refund has been issued,\n"
            "  processed or confirmed. Err on the side of triggering.\n\n"
            "### TRIGGER CRITERIA\n"
            "FLAG the message if it asserts a completed refund.\n"
            "**Explicit:** 'your refund has been processed', 'I've issued the refund'.\n"
            "**Implicit (CRITICAL):** 'that's all taken care of', 'you'll see the money\n"
            "in 3-5 days', quoting a confirmation number for a refund.\n\n"
            "### DO NOT FLAG\n"
            "Describing the refund POLICY, or saying a return has been STARTED — a\n"
            "started return is not an issued refund.",
            scope="agent",
            on_trigger=flows.transfer_to(human),
        ),
    ],
)


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print(f"warning: {w}")
  if errors:
    raise SystemExit("\n".join(errors))
  flows.build_app(app, "guarded_orders_app")
  print("emitted ./guarded_orders_app")
