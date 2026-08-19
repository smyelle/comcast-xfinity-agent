"""Spend what the session already knows instead of asking for it again.

Conversations rarely start from nothing. A caller reaches a support line from the
order-tracking page, so the session carries the parcel. Another reaches the same line
from the account IVR, so it carries an account number instead — under whichever of two
spellings the upstream system happens to use. Nothing about the flow changes; what
changes is how much of it is already answered on turn one.

A `variable_map` names ONE WAY a conversation can start, and the slots that knowledge
fills. Declare one per real entry path, most specific first:

    caller arrives with                what the flow does
    ---------------------------------  ------------------------------------------
    {"parcel": {...}, "account": "A1"}  both bindings resolve -> `from_tracking`
                                        wins, tracking number AND account filled,
                                        so the flow opens on the actual question
    {"account_number": "A1"}            `from_tracking` cannot resolve (no parcel)
                                        -> `from_account` fills the account only
    {"customer_ref": "A1"}              the same fact under the other spelling,
                                        so the same binding still resolves
    {"account_number": ""}              an unseeded CES variable arrives as its
                                        declared default -> nothing matches and
                                        the flow asks, exactly as it would have
    {"account_number": "AWAITING_SYNC"} present, but the upstream's "no answer
                                        yet" sentinel -> rejected, so the flow
                                        asks rather than believing it

Each piece earns its place:

* Bindings are keyed on the SLOT, because that is the name this flow chose and
  controls. The right-hand side is whatever the upstream calls it, which is where the
  mess lives — two spellings of one fact go in one list, not in two maps.
* A map is ALL OR NOTHING. Half a shape is not a shape: `from_tracking` naming both a
  parcel and an account means a session with only an account is a different entry
  path, and should be described as one.
* The most specific map is declared FIRST, because the first whose bindings all
  resolve is the one used. `flows validate` rejects the reverse order, where the
  narrow map would swallow every session and the wide one could never be chosen.
* `reject` is not a validator. It says "this value is not an answer" — the sentinel an
  upstream writes while a backend is still thinking. Believing it fills a slot with
  something no branch matches, which is worse than not filling it at all.
* No match fills nothing. The worst a misdeclared map can do is leave a question that
  could have been skipped, never invent an answer.

Build + validate offline:
    python -m examples.variable_maps      # emits ./variable_maps_app
"""

import flows

support = flows.Flow(
    "support",
    root_agent="Support_Agent",
    bootstrap={"welcome_slot": "welcome"},
)

support.add(
    flows.announce("welcome", ["Support here. I can look into a delivery for you."]),
    # Filled from the session on the tracking-page path; asked on every other.
    flows.user_slot("tracking_number", ask="What's the tracking number?"),
    flows.user_slot("account_number", ask="What's the account number?"),
    flows.user_slot("issue", ask="And what's gone wrong with it?"),
    flows.result_slot("outcome", "raise_ticket"),
)

support.task(
    "raise_ticket", "open_support_ticket",
    ["tracking_number", "account_number", "issue"], "outcome",
    out_key="summary", terminal=True, then_say="{outcome}",
)

app = flows.App(
    root_flow=support,
    app_display_name="variable-maps-demo",
    variables=[
        # An OBJECT the tracking page hands over whole.
        {"name": "parcel", "schema": {"type": "OBJECT", "default": {}}},
        # The same fact under the two spellings two upstream systems use.
        {"name": "account_number", "schema": {"type": "STRING", "default": ""}},
        {"name": "customer_ref", "schema": {"type": "STRING", "default": ""}},
    ],
    variable_maps=[
        flows.variable_map(
            "from_tracking",
            {
                "tracking_number": "parcel.tracking_id",
                "account_number": ["account_number", "customer_ref"],
            },
            description="Handed off from the order-tracking page.",
        ),
        flows.variable_map(
            "from_account",
            {
                "account_number": flows.bind(
                    ["account_number", "customer_ref"],
                    reject=["AWAITING_SYNC"],
                ),
            },
            description="Handed off from the account IVR, identified but not scoped.",
        ),
    ],
)


@flows.tool(flow="support")
def open_support_ticket(tracking_number: str, account_number: str,
                        issue: str) -> dict:
  """Raise a support ticket for a delivery.

  Args:
    tracking_number: The parcel's tracking number.
    account_number: The account the parcel belongs to.
    issue: What the caller says has gone wrong.

  Returns:
    dict with `success` and a `summary` line to read back.
  """
  return {
      "success": True,
      "summary": f"Ticket raised for {tracking_number}. Someone will call back.",
  }


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  if not errors:
    flows.build_app(app, "./variable_maps_app", overwrite=True)
    print("emitted ./variable_maps_app")
