"""Search at most three of eight record systems — a budget the engine counts.

A missing order can be traced through eight systems. Each is a billed API call and a
round trip the caller waits through, so the contract allows three per case: try three,
then open a case and stop making them wait.

The ladder was always easy to declare. The budget was not, because a condition can
compare numbers but nothing in a flow produced one — a slot holds what the caller said or
a tool returned, and a latch holds `"true"`. So the budget was derived in a callback,
which put the one rule the contract is most explicit about in the one place the validator
cannot read and no offline test covers.

`count_into` names an integer slot the engine bumps whenever the task fires:

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "where's my order?"             asks for the case id
    "C-4471"                        searches core, then billing, then the CRM
                                    the remaining five are GATED OFF — the budget
                                    is spent — and a case is opened instead

## Why not just write the condition

This *is* expressible without the primitive, and at four systems you should. "Fewer than
three of the others have run" is a condition over their out-slots:

    condition={"not": {"all": [{"slot": "tried_core", "filled": True},
                               {"slot": "tried_billing", "filled": True},
                               {"slot": "tried_crm", "filled": True}]}}

That form was measured against this one through the eval harness at eight systems: same
tools fired, same case opened. It cost **1008 condition leaves against 9**.

"At most k have run" over n tasks has no `filled` shorthand — you have to enumerate the
subsets. Each task asks whether any k of the *other* n-1 already ran, so it carries
C(n-1, k) groups of k leaves, and the terminal gate carries C(n, k). At three of four
that is a single group and the counter is not worth reaching for. At three of eight it is
35 groups per task.

The other cost does not need a big n. The condition form makes every gated task name
every other task's out-slot, so adding a ninth system means editing eight existing
conditions. The counter is O(1): add it to the list below and it participates.

## Two details worth knowing

* The count happens at DISPATCH, not on success. A search that comes back empty still
  spent the billed call and the caller's wait, so it still costs a unit of budget.
* `found` is a field of the result, not the task's `success`. `success` says the CALL
  worked, and a system answering "no record" answered. Conflating them would exhaust the
  first search's failure ladder and end the flow before the second ever ran.

Build + validate offline:
    python -m examples.task_fire_cap        # emits ./task_fire_cap_app
"""

from pydantic import BaseModel, Field

import flows

# Eight places to look, three searches allowed. Not a typo: the budget is the point.
BUDGET = 3


class SearchResult(BaseModel):
  status_message: str = Field(description="What the search turned up")
  # "yes" only when the order was located. Distinct from `success`, which says the CALL
  # worked: a system answering "no record" answered.
  found: str = Field(default="no", description="yes when the order was located")
  success: bool = True


# A tool apiece, not one `search(system)` shared by all eight: the runtime routes tool
# results by TOOL name, so tasks sharing a tool have their outputs overwritten by
# whichever wrote last, and the losers never see their out-slot fill — so they re-fire
# forever. `validate_app` warns about it, which is how this example learned it.
@flows.tool(flow="tracing")
def search_core(case_id: str) -> SearchResult:
  """Search the core order system for a case."""
  return SearchResult(status_message="the core system has no record of it")


@flows.tool(flow="tracing")
def search_billing(case_id: str) -> SearchResult:
  """Search the billing ledger for a case."""
  return SearchResult(status_message="billing shows no charge for it")


@flows.tool(flow="tracing")
def search_crm(case_id: str) -> SearchResult:
  """Search the CRM for a case."""
  return SearchResult(status_message="the CRM has nothing filed under it")


@flows.tool(flow="tracing")
def search_carrier(case_id: str) -> SearchResult:
  """Search the carrier's manifests for a case."""
  return SearchResult(status_message="the carrier never scanned it")


@flows.tool(flow="tracing")
def search_warehouse(case_id: str) -> SearchResult:
  """Search the warehouse feed for a case."""
  return SearchResult(status_message="the warehouse feed is offline")


@flows.tool(flow="tracing")
def search_returns(case_id: str) -> SearchResult:
  """Search the returns system for a case."""
  return SearchResult(status_message="returns has no record of it")


@flows.tool(flow="tracing")
def search_partner(case_id: str) -> SearchResult:
  """Search the partner marketplace for a case."""
  return SearchResult(status_message="the partner has no record of it")


@flows.tool(flow="tracing")
def search_archive(case_id: str) -> SearchResult:
  """Search the cold archive for a case."""
  return SearchResult(status_message="the archive has nothing that old")


@flows.tool(flow="tracing")
def open_trace_case(case_id: str) -> SearchResult:
  """Open a case for an order no automated search could find."""
  return SearchResult(status_message=f"a case is open for {case_id}")


# Order matters: the budget is spent top-down, so the cheapest and likeliest go first.
# Everything from the fourth entry on is gated off on a cold case, which is the whole
# demonstration — they are declared so the budget has something to stop.
SYSTEMS = ["core", "billing", "crm", "carrier",
           "warehouse", "returns", "partner", "archive"]


def build() -> flows.App:
  """One flow: collect a case id, spend the search budget, then open a case."""
  flow = flows.Flow("tracing", root_agent="tracing_agent")

  flow.add(flows.user_slot(
      "case_id",
      ask="What's the case id?",
      hint="the caller's case id",
  ))
  for name in SYSTEMS:
    flow.add(flows.result_slot(f"tried_{name}", name))
  # Written by whichever search ran last. A search needs its OWN out-slot — the engine
  # stops re-firing a task once that slot fills, and one shared slot would stop them all
  # after the first — so the shared answer rides along as an extra output.
  #
  # Sourced "event" as well as from each search: a gate reading a slot that may not be
  # there yet has to say so, or the build warns that the condition depends on something
  # the task never waits for. Here that absence is the normal first case.
  flow.add({"name": "outcome",
            "source": ["event"] + [f"task:{n}" for n in SYSTEMS],
            "event_key": "outcome"})
  flow.add(flows.result_slot("case_msg", "exhausted"))

  for name in SYSTEMS:
    flow.task(
        name, f"search_{name}", ["case_id"], f"tried_{name}",
        out_key="status_message",
        extra_outputs={"found": "outcome"},
        count_into="searches",
        condition={"all": [
            # Read against an ABSENT slot on the first turn, which is zero — so nothing
            # needs seeding, and a budget of 0 would correctly allow nothing through.
            {"slot": "searches", "lt": BUDGET},
            # Stop early when a search actually finds the order; spending the rest of
            # the budget on an answer already in hand is the other way to waste it.
            {"slot": "outcome", "neq": "yes"},
        ]},
    )

  flow.task(
      "exhausted", "open_trace_case", ["case_id"], "case_msg",
      out_key="status_message",
      condition={"all": [{"slot": "searches", "gte": BUDGET},
                         {"slot": "outcome", "neq": "yes"}]},
      terminal=True,
      then_say="I couldn't find it in the systems I'm allowed to search, so {case_msg}.",
  )

  return flows.App(
      root_flow=flow,
      app_display_name="task-fire-cap",
      agent_instruction=(
          "You help callers trace a missing order. The engine decides which systems to "
          "search and when to stop searching — never invent another place to look, and "
          "never promise a case yourself."
      ),
  )


app = build()

if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  if not errors:
    flows.build_app(app, "./task_fire_cap_app", overwrite=True)
    print("built: ./task_fire_cap_app")
