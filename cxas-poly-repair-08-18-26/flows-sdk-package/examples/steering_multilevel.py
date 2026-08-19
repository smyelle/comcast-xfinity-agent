"""Multi-level (hierarchical) steering — one routing turn, then silent deeper levels.

`router_flow` classifies the opening utterance into a level-1 route (a model routing turn,
exactly as the single-level `steering.py` demo). A route with `subroutes` is an INTERNAL
node: once the caller is routed there, a SCOPED, SILENT classifier picks one child from the
SAME utterance, and recurses — so a single opening turn resolves a whole intent PATH
(`billing -> billing_dispute -> dispute_overcharge`), recorded as `detected_intent` (the
deepest leaf) + `detected_path` (the slash-joined ancestry).

Each level is generated from the routes' own fields (no hand-wiring):

  * a passive `intent_slot` classifies a node's children — its `option_cues` are each
    child's `cues` (a deterministic pre-model fast-path; at depth `backstop` folds in too),
    and its model exemplars are each child's `description`;
  * `disambiguate=` is INHERITED down the tree (None inherit / False force-silent /
    True|disambiguation(...) ask-when-unsure). An ASKED level still fills silently when the
    utterance is clear — it only asks on genuine ambiguity;
  * `default=` names the child a level falls back to (else auto-derived).

Everything is passive intent slots + `App.classifiers`, both of which the blessed engine
already runs — NO engine change.

Combinations this app demonstrates:
  * L1 handled     — "my internet is down"                 -> diagnostics (runs a DAG)
  * L1 -> L2 -> L3 — "I was charged more than my plan"      -> billing/billing_dispute/
                                                              dispute_overcharge (silent)
  * cue cascade    — "dispute a charge, it's a late fee"    -> billing/billing_dispute/
                                                              dispute_latefee (deterministic)
  * inherit budget — "there's a problem with a charge"      -> billing_dispute asks
                                                              latefee-or-overcharge, then routes
  * override       — billing_payment uses a tighter budget (max_turns=1)
  * force silent   — tech_tv never asks; falls to tv_nosignal (disambiguate=False, default=)

Build + validate offline:
    python -m examples.steering_multilevel        # emits ./steering_multilevel_app
"""

from pydantic import BaseModel, Field

import flows


class DiagnosticReport(BaseModel):
  summary: str = Field(description="One-sentence caller-facing result of the line check.")
  success: bool = Field(default=True)


class HandoffAck(BaseModel):
  summary: str = Field(description="One-sentence caller-facing hand-off line.")
  success: bool = Field(default=True)


@flows.tool(flow="diagnostics")
def run_line_check() -> DiagnosticReport:
  """Run the connection checks. Takes no arguments — nothing is asked first."""
  return DiagnosticReport(
      summary="I ran a quick check and your line looks degraded — let's try a few fixes.")


@flows.tool(flow="human_handoff")
def connect_to_agent() -> HandoffAck:
  """Hand the caller to a live agent (a real transfer would go here)."""
  return HandoffAck(summary="Okay — I'm connecting you to a specialist now. One moment.")


# ── Handled level-1 leaves: real local DAGs, reached directly from the routing turn.
diagnostics = flows.Flow("diagnostics", bootstrap={"reset_on_complete": True})
diagnostics.add(flows.result_slot("diag_summary", "RunLineCheck"))
diagnostics.task("RunLineCheck", "run_line_check", [], "diag_summary",
                 out_key="summary", terminal=True, then_say="{diag_summary}")

human_handoff = flows.Flow("human_handoff", bootstrap={"reset_on_complete": True})
human_handoff.add(flows.result_slot("handoff_summary", "ConnectAgent"))
human_handoff.task("ConnectAgent", "connect_to_agent", [], "handoff_summary",
                   out_key="summary", terminal=True, then_say="{handoff_summary}")


# ── The steering tree. `billing`, `tech`, and `account` are INTERNAL (they open deeper
# levels); `diagnostics` and `human` are handled leaves. The whole tree inherits the
# router's disambiguation budget unless a level overrides it.
steering = flows.router_flow(
    "steering",
    [
        # billing: three levels deep, all leaves DEFERRED (recognise + hand off).
        flows.route(
            "billing", "charges, invoices, payments, and disputes",
            backstop=["invoice", "statement"],                # L1 post-model net
            subroutes=[
                flows.route(
                    "billing_dispute", "believes a specific charge is wrong",
                    cues=["dispute a charge", "wrong charge"],
                    # inherits the router budget: asks latefee-vs-overcharge only when unclear
                    subroutes=[
                        flows.route("dispute_latefee", "a late fee they think is unfair",
                                    cues=["late fee", "late charge"]),
                        flows.route("dispute_overcharge", "charged more than the plan price",
                                    cues=["overcharged", "charged too much"],
                                    backstop=["higher than my plan"]),
                    ]),
                flows.route(
                    "billing_payment", "wants to pay a bill or set up a plan",
                    cues=["pay my bill", "make a payment"],
                    disambiguate=flows.disambiguation(max_turns=1),   # OVERRIDE: tighter budget
                    default="payment_make",
                    subroutes=[
                        flows.route("payment_make", "make a payment now",
                                    cues=["pay now", "pay today"]),
                        flows.route("payment_arrangement", "set up a payment arrangement",
                                    cues=["payment plan", "split it up", "arrangement"]),
                    ]),
                flows.route("billing_explain", "just wants their bill explained",
                            cues=["explain my bill", "understand my bill"]),
            ]),
        # tech: one branch forced SILENT with an explicit default, one deferred leaf.
        flows.route(
            "tech", "technical or service problems",
            cues=["no service", "outage"],
            subroutes=[
                flows.route(
                    "tech_tv", "television / video problems",
                    disambiguate=False, default="tv_nosignal",   # never asks; silently defaults
                    subroutes=[
                        flows.route("tv_nosignal", "no signal or a black screen",
                                    cues=["no signal", "black screen", "no picture"]),
                        flows.route("tv_channels", "channels are missing from the lineup",
                                    cues=["missing channels", "channel is gone", "lineup"]),
                    ]),
                flows.route("tech_phone", "home phone problems",
                            cues=["no dial tone", "phone is dead"]),
            ]),
        # account: single deeper level, all deferred leaves (inherits the router budget).
        flows.route(
            "account", "account, plan, and address changes",
            subroutes=[
                flows.route("account_move", "moving service to a new address",
                            cues=["moving", "new address", "relocate"]),
                flows.route("account_upgrade", "wants a faster plan or an upgrade",
                            cues=["upgrade", "faster plan", "more channels"]),
                flows.route("account_cancel", "wants to cancel service",
                            cues=["cancel", "disconnect service"],
                            backstop=["close my account"]),
            ]),
        # Handled level-1 leaves.
        flows.route("diagnostics", "the caller's internet is slow, down, or dropping",
                    flow=diagnostics, cues=["internet is down", "wifi not working"]),
        flows.route("human", "asks for a person, an agent, or to be transferred",
                    flow=human_handoff, cues=["agent", "representative"]),
    ],
    # The tree-wide default: ask up to 2 clarifying turns, then hand off to a human. Deeper
    # levels inherit this budget (an asked level fills silently when the utterance is clear).
    disambiguate=flows.disambiguation(max_turns=2, on_exhaust="human"),
    root_agent="Front_Desk",
)

app = flows.App(
    root_flow=steering,
    app_display_name="Multi-level Steering Demo",
    agent_instruction=(
        "You are a warm, concise front-desk agent for Riverline, an internet & TV "
        "provider. Greet the caller, then follow the routing rules exactly."),
)


if __name__ == "__main__":
  import json
  import os

  out = "./steering_multilevel_app"
  flows.build_app(app, out)
  print(f"built: {out}")
  with open(os.path.join(out, "app.json")) as f:
    decls = json.load(f).get("variableDeclarations", [])
  for v in decls:
    if v.get("name") == "flow_config_map":
      print("  flow_config_map =", v["schema"]["default"])
