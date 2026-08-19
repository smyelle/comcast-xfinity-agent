"""A routed call is seeded on the turn it ARRIVES, not a turn after it is routed.

A `variable_map` spends what a session already knows. On a single-flow app that is
straightforward: ingress runs, the slot is filled, the flow never asks. On a ROUTER the
ordering is the whole problem — seeding happens before the first turn is answered, which
is earlier than routing, so the config the ingress can see is the router. A router holds
no user slots, so nothing projects onto it, and keyed strictly on that id the maps its
flows declare are unreachable exactly when they are needed.

The failure is quiet, which is what makes it worth a demo. Nothing errors. The map is
declared, the build emits it, `validate_app` passes — and the agent asks the caller for
the account number the call arrived with. It resolves a turn or more later, once the
route has landed and `_active_config_id` names a real flow, by which point the question
has already been asked.

A router is now given the shapes its flows declare, so it seeds on turn 0.

DRIVEN LIVE against the real platform, one deployed app per arm, same variables and the
same opening line. Verbatim from the drive; VARIABLE_MAP_ROUTED_VERIFY.md has the
commands and the app ids.

  TREATMENT (router inherits the shapes) — the account is spent, not re-asked.
     caller: "my parcel hasn't turned up"        [arrives with accountNumber=A-4471]
     agent:  "Could you please provide your tracking number?"

  CONTROL (`--control`, the same app with the router's table entry removed again) —
  same variables, same line.
     caller: "my parcel hasn't turned up"        [arrives with accountNumber=A-4471]
     agent:  "Could you please provide your account number?"

One turn, and the whole difference is which question the caller is asked. The number is
sitting in the session in BOTH arms; only the treatment spends it.

The control is the same app — same flows, same map, same cues, same model — with the
router's entry stripped back out of the emitted table, which is exactly the table every
routed app used to ship. So the A/B isolates the fix and nothing else.

Build + validate offline:
    PYTHONPATH=src python -m examples.variable_map_routed            # treatment
    PYTHONPATH=src python -m examples.variable_map_routed --control  # the A/B control
"""

import sys

from pydantic import BaseModel, Field

import flows

CONTROL = "--control" in sys.argv


class ParcelRecord(BaseModel):
  """What the courier system knows about one parcel."""

  status: str = Field(description="Where the parcel is, in plain words.")
  success: bool = Field(default=True, description="Whether the parcel was found.")


@flows.tool(flow="tracking")
def look_up_parcel(account_number: str, tracking_number: str) -> ParcelRecord:
  """Find a parcel on an account."""
  return ParcelRecord(status="out for delivery today")


@flows.tool(flow="billing")
def look_up_balance(account_number: str) -> dict:
  """Read the balance on an account."""
  return {"success": True, "balance": "$18.40"}


def _account_slot():
  """The slot the arriving call should make unnecessary to ask for."""
  # `shared`, because an account number is a fact about the CALL rather than about one
  # journey through it — and both flows want the same one. Without it the cross-config
  # check rightly warns that two flows are filling one name with no scoping between them.
  return flows.user_slot("account_number", ask="What's the account number?",
                         hint="account number", shared=True)


tracking = flows.Flow("tracking", root_agent="Front_Desk")
tracking.add(
    _account_slot(),
    flows.user_slot("tracking_number", ask="What's the tracking number?"),
    flows.result_slot("status", "LookUpParcel"),
)
tracking.task(flows.task(
    "LookUpParcel", "look_up_parcel", ["account_number", "tracking_number"], "status",
    then_say="That one's {status}."))

billing = flows.Flow("billing", root_agent="Front_Desk")
billing.add(
    _account_slot(),
    flows.result_slot("balance", "LookUpBalance"),
)
billing.task(flows.task(
    "LookUpBalance", "look_up_balance", ["account_number"], "balance",
    then_say="Your balance is {balance}."))

# The router. It holds no user slots — routing is all it does — which is exactly why the
# ingress had nothing to seed from when it keyed strictly on the active config.
front_desk = flows.router_flow(
    "front_desk", ["tracking", "billing"],
    route_cues={"tracking": ["parcel", "delivery", "package", "tracking"],
                "billing": ["bill", "balance", "charge", "payment"]},
    root_agent="Front_Desk")

# ONE map, identical in both arms. The arms differ only in the emitted TABLE — see
# `_strip_router_entry` — so the A/B isolates the fix and nothing else.
MAPS = [flows.variable_map("by_account", {"account_number": ["accountNumber"]},
                           description="The call arrived identified: do not ask.")]

app = flows.App(
    root_flow=front_desk,
    extra_flows=[tracking, billing],
    app_display_name="Variable map routed" + (" CONTROL" if CONTROL else ""),
    model="gemini-composite-v1",
    variables=[
        {"name": "accountNumber", "schema": {"type": "STRING"}},
        {"name": "balance_ref", "schema": {"type": "STRING"}},
    ],
    variable_maps=MAPS,
)


def _strip_router_entry(out: str) -> None:
  """Put the emitted table back the way it looked BEFORE the fix.

  The control has to be the same app: same flows, same map, same cues, same model. The
  only thing that changed is that a router is now given the shapes its flows declare, so
  the honest control is this app with the router's entry removed again — which is
  precisely the table every routed app used to ship.
  """
  import json
  import os
  path = os.path.join(out, "app.json")
  with open(path, encoding="utf-8") as fh:
    doc = json.load(fh)
  for var in (doc.get("variableDeclarations") or []):
    if var.get("name") == "variable_maps_by_config":
      table = json.loads(var["schema"]["default"])
      table.pop("front_desk", None)
      var["schema"]["default"] = json.dumps(table)
  with open(path, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2)


def _show_table() -> None:
  """Print which configs the emitted table reaches, so the arms differ offline."""
  import json
  import os
  out = "./variable_map_routed_control_app" if CONTROL else "./variable_map_routed_app"
  with open(os.path.join(out, "app.json"), encoding="utf-8") as fh:
    for var in (json.load(fh).get("variableDeclarations") or []):
      if var.get("name") == "variable_maps_by_config":
        table = json.loads(var["schema"]["default"])
        arm = "CONTROL" if CONTROL else "TREATMENT"
        print(f"\n  arm: {arm}")
        for cid in sorted(table):
          names = [m["name"] for m in table[cid]]
          mark = "  <- the router" if cid == "front_desk" else ""
          print(f"  {cid:12} {names}{mark}")
        if "front_desk" not in table:
          print("  front_desk   (no entry — ingress cannot fire before routing)")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    out = "./variable_map_routed_control_app" if CONTROL else "./variable_map_routed_app"
    flows.build_app(app, out)
    if CONTROL:
      _strip_router_entry(out)
    print(f"built: {out}")
    _show_table()
