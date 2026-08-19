"""A diagnostic sweep whose verdict is only as good as the picture it arbitrates over.

A line-check agent runs one backend sweep and then picks the highest-priority thing
wrong with the connection. The ladder is ordinary condition-gated tasks; what makes it
CORRECT is the value policy on the slots it reads.

Three ways the picture goes wrong, and what each one costs:

* **A status the sweep did not return.** `wifi_status` has no producer at all here —
  the sweep does not measure it. Absent, it compares exactly like a healthy value, so
  the all-clear rung wins over the rung that should have caught it. `default="skipped"`
  makes "nobody looked" a value the ladder can see.
* **A status that came back as a placeholder.** This upstream pre-sets each check to
  `AWAITING_SCAN` while the backend is still working. Present, non-empty, and not an
  answer — so it matches no rung and the flow falls through to the model, which then
  improvises a diagnosis. `reject` says the value was never real, and the default then
  applies.
* **A sweep that failed outright.** `on_failure.fill` resolves every status to
  `error` so the ladder still arbitrates over a complete picture and the dispatch rung
  takes it, rather than leaving holes for a lower rung to win on.

`publish` is the way back out: the transfer tool reads the ticket reference off session
state rather than taking it as a parameter, which is the ordinary shape for a carried
or legacy tool, and it used to mean hand-mirroring the slot in a callback.

Build + validate offline:
    python -m examples.slot_value_policy      # emits ./slot_value_policy_app
"""

import flows

# What this upstream writes into a status while the backend is still working.
AWAITING = ["AWAITING_SCAN"]

# The checks the ladder gates on. Every one must hold a value — even "skipped" —
# before a rung is evaluated.
CHECKS = ("line_status", "signal_status", "router_status", "wifi_status")

line_check = flows.Flow("line_check", root_agent="Line_Check_Agent")

line_check.add(
    flows.user_slot("service_id", ask="What's the service reference on your bill?"),
)

for _check in CHECKS:
    line_check.add(flows.event_slot(_check, reject=AWAITING, default="skipped",
                                    shared=True))

line_check.add(
    # The sweep reports one identifier that two rungs read under different names.
    flows.event_slot("router_serial", reject=AWAITING, default="NOT_FOUND",
                     shared=True),
    flows.event_slot("hardware_ref", reject=AWAITING, default="NOT_FOUND",
                     shared=True),
    # Spoken by the line-fault rung and nothing else, so an empty one is silence.
    flows.event_slot("fault_note", reject=AWAITING, shared=True, default=[
        flows.fallback(
            "We can see the fault from here, so there's nothing you need to check.",
            when=flows.eq("line_status", "fault")),
    ]),
    # Read off session state by the transfer tool, so it is published as well as filled.
    flows.event_slot("ticket_ref", default="UNASSIGNED", publish=["ticket_ref"],
                     shared=True),
    flows.event_slot("swept", shared=True),
    flows.event_slot("verdict_given"),
    flows.event_slot("scan_offered"),
    flows.event_slot("scan_run"),
)

line_check.task(
    "RunSweep", "run_line_sweep", {"service_id": "service_id"}, "swept",
    out_key="success",
    extra_outputs={
        "line_status": "line_status",
        "signal_status": "signal_status",
        "router_status": "router_status",
        # One reported serial, two slots.
        "router_serial": ["router_serial", "hardware_ref"],
        "fault_note": "fault_note",
        "ticket_ref": "ticket_ref",
    },
    condition=flows.unset("swept"),
    on_failure={"max_retries": 0,
                "fill": {**{c: "error" for c in CHECKS}, "swept": "true"}},
)

_SWEPT = {"slot": "swept", "filled": True}
_UNSPOKEN = {"slot": "verdict_given", "filled": False}


def rung(name, tool, when, say):
  """One ladder rung: gated on a complete picture, speaks once, and ends the call.

  NOT terminal, and that is load-bearing: the engine DEFERS a terminal fire on any
  turn carrying fresh user text, expecting a setter call to produce a re-invoke that
  carries it. A ladder has nothing to collect, so no setter is ever called, the
  deferred fire never lands, and the flow spins until CES's reasoning-loop cap. A
  non-terminal rung fires on the same turn and renders the identical `then_say`; the
  agent instruction then handles whatever the caller says next.
  """
  return flows.task(name, tool, [], "verdict_given", out_key="verdict_given",
                    condition={"all": [_SWEPT, _UNSPOKEN, when]}, then_say=say)


for _rung in [
    rung("LineFault", "note_line_fault", {"slot": "line_status", "eq": "fault"},
         "There's a fault on the line into your building. {fault_note}"),
    rung("SignalLoss", "note_signal_loss", {"slot": "signal_status", "eq": "degraded"},
         "Your signal is degraded, so an engineer needs to look at it."),
    rung("RouterSwap", "note_router_swap", {"slot": "router_status", "eq": "failed"},
         "Your router has failed and needs replacing. Reference {hardware_ref}."),
    rung("AllClear", "note_all_clear", {"slot": "line_status", "eq": "clear"},
         "Everything checks out from our side."),
    # The catch-all, and it is not optional. Every check above defaults to "skipped",
    # so a sweep whose results do not land leaves a picture that is COMPLETE and
    # matches nothing — the flow then has no branch to take and no question to ask.
    # `flows validate` says so at build time; this rung is the answer it asks for.
    rung("NothingConclusive", "note_inconclusive",
         {"slot": "line_status", "in": ["skipped", "error"]},
         "I couldn't get a clear reading, so I'll book an engineer to take a look."),
]:
    line_check.task(_rung)

# The verdict is not the end of the call. Offering a deeper scan latches `scan_offered`
# as it SPEAKS the offer — so a follow-up gated on `{"slot": "scan_offered", "filled":
# True}` would be satisfiable on that very turn, and the model, holding both the
# question and the tool that answers it, would run the scan itself. The caller never
# gets asked. `since` is the difference.
line_check.task(
    "OfferScan", "offer_deep_scan", [], "scan_offered", out_key="success",
    condition={"all": [{"slot": "verdict_given", "filled": True},
                       {"slot": "scan_offered", "filled": False}]},
    then_say="Would you like me to run a deeper scan?")

line_check.task(
    "RunScan", "run_deep_scan", [], "scan_run", out_key="success",
    condition={"all": [flows.since("scan_offered"),
                       {"slot": "scan_run", "filled": False}]},
    then_say="Running the deeper scan now.")


@flows.tool(flow="line_check")
def offer_deep_scan() -> dict:
  """Record that a deeper scan was offered.

  Returns:
    dict with `success`.
  """
  return {"success": True}


@flows.tool(flow="line_check")
def run_deep_scan() -> dict:
  """Record that the deeper scan was started.

  Returns:
    dict with `success`.
  """
  return {"success": True}


app = flows.App(
    root_flow=line_check,
    app_display_name="slot-value-policy-demo",
    variables=[{"name": "ticket_ref", "schema": {"type": "STRING", "default": ""}}],
    agent_instruction=(
        "You are a broadband line-check assistant. Speak only what the slot-filling "
        "framework directs you to speak. Once a verdict has been given, answer any "
        "follow-up briefly and offer to arrange an engineer visit."
    ),
)


@flows.tool(flow="line_check")
def run_line_sweep(service_id: str = "") -> dict:
  """Run every line check for a service reference.

  Args:
    service_id: The service reference from the bill.

  Returns:
    dict with `success` and whichever checks resolved.
  """
  # `wifi_status` is deliberately absent: this sweep does not measure it, which is
  # what its slot default is for. `fault_note` comes back empty on a clear line, so
  # the conditional default only speaks when there is actually a fault to describe.
  return {"success": True, "line_status": "clear", "signal_status": "clear",
          "router_status": "clear", "router_serial": "RS-88120",
          "fault_note": "", "ticket_ref": "TK-4471"}


# A say-only rung still needs a tool to hang its script on. Written out rather than
# generated in a loop: `@flows.tool` renders the DECORATED FUNCTION'S OWN SOURCE, so a
# closure emits `def _fn()` under every tool name, and CES resolves a tool by function
# name — every one of them is then missing at runtime. Nothing fires, the engine has no
# branch to take and no question to ask, and CES retries the empty turn to its
# reasoning-loop cap. Offline this is invisible: validate and build both pass.


@flows.tool(flow="line_check")
def note_line_fault() -> dict:
  """Record that a line fault was reported.

  Returns:
    dict with `success` and `verdict_given`.
  """
  return {"success": True, "verdict_given": "true"}


@flows.tool(flow="line_check")
def note_signal_loss() -> dict:
  """Record that signal degradation was reported.

  Returns:
    dict with `success` and `verdict_given`.
  """
  return {"success": True, "verdict_given": "true"}


@flows.tool(flow="line_check")
def note_router_swap() -> dict:
  """Record that a router replacement was reported.

  Returns:
    dict with `success` and `verdict_given`.
  """
  return {"success": True, "verdict_given": "true"}


@flows.tool(flow="line_check")
def note_all_clear() -> dict:
  """Record that the line checked out clear.

  Returns:
    dict with `success` and `verdict_given`.
  """
  return {"success": True, "verdict_given": "true"}


@flows.tool(flow="line_check")
def note_inconclusive() -> dict:
  """Record that the checks were inconclusive.

  Returns:
    dict with `success` and `verdict_given`.
  """
  return {"success": True, "verdict_given": "true"}


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  if not errors:
    flows.build_app(app, "./slot_value_policy_app", overwrite=True)
    print("emitted ./slot_value_policy_app")
