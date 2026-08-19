"""Steering — a first-class, model-classified intent router built from `route(...)` objects.

`router_flow` here takes `flows.route(...)` objects instead of bare flow-key strings. Each
route carries its `name`, a semantic `description` the model classifies on, the child
`flow` it runs when handled locally (or none, to DEFER), and deterministic `cues` that
skip the model. From them the build layer GENERATES:

  * the `<routing>` classifier instruction, from the descriptions (no hand-written block);
  * ONE shared deferral flow every deferred route maps to — the route key survives on the
    gate, so `detected_intent` is right per route (no N near-identical defer flows);
  * the `route_cues` fast-path, folded from each route's `cues`.

Routing is model-first: `cues` are a deterministic shortcut UNDER the model. Low-confidence
routing is handled ONE way — `disambiguate` tells the model to ask a brief clarifying
question whenever it can't confidently route (ambiguous, unclear, or off-topic) rather than
guessing, and `disambiguation(max_turns, on_exhaust)` hands off after a bounded number of
unresolved turns.

Combinations this app demonstrates:
  * cue-hit      — "power cycle my modem"            -> reboot (deterministic, pre-model)
  * model-route  — "my wifi keeps dropping"          -> diagnostics (semantic)
  * defer        — "I want to dispute a charge"      -> billing (recorded + handed off)
  * disambiguate — "I have a problem with my account"-> asks internet-or-billing, then routes
  * filler       — every routed turn speaks before it has decided, so the caller is not
                   left in silence through the slowest turn of the call

Build + validate offline:
    python -m examples.steering        # emits ./steering_app
"""

from pydantic import BaseModel, Field

import flows


class DiagnosticReport(BaseModel):
  summary: str = Field(description="One-sentence caller-facing result of the line check.")
  success: bool = Field(default=True)


class RebootAck(BaseModel):
  summary: str = Field(description="One-sentence caller-facing reboot confirmation.")
  success: bool = Field(default=True)


class HandoffAck(BaseModel):
  summary: str = Field(description="One-sentence caller-facing hand-off line.")
  success: bool = Field(default=True)


@flows.tool(flow="diagnostics")
def run_line_check() -> DiagnosticReport:
  """Run the connection checks. Takes no arguments — nothing is asked first."""
  return DiagnosticReport(
      summary="I ran a quick check and your line looks degraded — let's try a few fixes.")


@flows.tool(flow="reboot")
def send_reboot() -> RebootAck:
  """Send the restart signal to the caller's gateway."""
  return RebootAck(
      summary="I've started a restart on your gateway — it'll be back in about two minutes.")


@flows.tool(flow="human_handoff")
def connect_to_agent() -> HandoffAck:
  """Hand the caller to a live agent (a real transfer would go here)."""
  return HandoffAck(summary="Okay — I'm connecting you to a specialist now. One moment.")


# ── Handled route flows. Each fires a rung (a tool call with a spoken result) so it
# renders on the turn it is routed to, and re-arms the router on completion.
diagnostics = flows.Flow("diagnostics", bootstrap={"reset_on_complete": True})
diagnostics.add(flows.result_slot("diag_summary", "RunLineCheck"))
diagnostics.task("RunLineCheck", "run_line_check", [], "diag_summary",
                 out_key="summary", terminal=True, then_say="{diag_summary}")

reboot = flows.Flow("reboot", bootstrap={"reset_on_complete": True})
reboot.add(flows.result_slot("reboot_summary", "SendReboot"))
reboot.task("SendReboot", "send_reboot", [], "reboot_summary",
            out_key="summary", terminal=True, then_say="{reboot_summary}")

# ── A REAL hand-off flow (a live-agent transfer would go in connect_to_agent). The
# `human` route points at it, and the disambiguation budget's on_exhaust routes here too —
# no `handoff_say` string, just a flow that owns its wording (and could transfer / A2A).
human_handoff = flows.Flow("human_handoff", bootstrap={"reset_on_complete": True})
human_handoff.add(flows.result_slot("handoff_summary", "ConnectAgent"))
human_handoff.task("ConnectAgent", "connect_to_agent", [], "handoff_summary",
                   out_key="summary", terminal=True, then_say="{handoff_summary}")


# ── The router, built from Route objects. billing + payments are DEFERRED (no local
# flow): they are recognised and handed off through the ONE auto-generated deferral flow
# (records detected_intent + a default line). `human` instead points at a REAL shared
# hand-off flow — the disambiguation on_exhaust routes there too.
steering = flows.router_flow(
    "steering",
    [
        flows.route(
            "diagnostics",
            "the caller's internet or WiFi is down, slow, or dropping — troubleshoot it",
            flow=diagnostics),
        flows.route(
            "reboot",
            "the caller explicitly wants to restart, reset, or power-cycle their gateway",
            flow=reboot,
            cues=["power cycle the modem", "reset my router", "restart my gateway"]),
        flows.route(
            "billing",
            "the caller wants to understand, question, or dispute a charge on their bill",
            backstop=["my bill", "a charge", "my statement", "overcharged"]),
        flows.route(
            "payments",
            "the caller wants to make a payment, pay a balance, or set up autopay",
            backstop=["make a payment", "pay my bill", "autopay"]),
        flows.route(
            "human",
            "the caller asks for a person, an agent, or to be transferred to someone",
            flow=human_handoff),
    ],
    # The single low-confidence path: ask one clarifying question whenever the model
    # can't confidently route (ambiguous, unclear, or off-topic); after 2 unresolved
    # turns, hand off to a human (the post-model disambiguation budget — Phase B).
    disambiguate=flows.disambiguation(max_turns=2, on_exhaust="human"),
    # Routing is the slowest turn of the call — it spends several serialized round trips
    # where an ordinary in-flow turn spends one — so the caller sits in silence through
    # the turn that sets the tone. This line goes out as a partial preempt: it speaks
    # straight away and the routing decision still lands in the same turn behind it.
    # Intent-neutral on purpose: "let me get you to the right place" is a promise made
    # before anyone knows where that is. `None` in the pool is a turn that stays quiet.
    filler_say=["One moment.", "Okay.", "Sure thing.", None],
    root_agent="Support_Agent",
)

app = flows.App(
    root_flow=steering,
    app_display_name="Steering Demo",
    agent_instruction=(
        "You are a warm, concise support agent for Acme Internet. Greet the caller, then "
        "follow the routing rules exactly."),
)


if __name__ == "__main__":
  import json
  import os

  out = "./steering_app"
  flows.build_app(app, out)
  print(f"built: {out}")
  with open(os.path.join(out, "app.json")) as f:
    decls = json.load(f).get("variableDeclarations", [])
  for v in decls:
    if v.get("name") == "flow_config_map":
      print("  flow_config_map =", v["schema"]["default"])
