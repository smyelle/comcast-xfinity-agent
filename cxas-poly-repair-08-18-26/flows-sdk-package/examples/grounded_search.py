"""Ground an answer in Google Search, inside a slot-filling flow.

The flow collects a shipment the ordinary way. Alongside it the agent carries a search
tool, so a question the flow never modelled ("do I need a customs form for Canada?") gets
a real, sourced answer instead of a deflection — and collection picks up where it left off.

Two ways to reach the tool, and the choice is about whether the answer must be SPOKEN or
CAPTURED:

  * `App(search_tools=[...])` / `Agent(search_tools=[...])` scopes it onto an agent. The
    model decides when to search and summarises what it finds, so the answer reads like an
    answer. Nothing lands in a slot. This is what the example uses.
  * An ordinary `task()` handed the `SearchTool` itself fires the search from the flow and
    lands the raw `snippets` in a slot, for a flow that must gate on the result or carry it
    onward. Pair it with `then_directive`, never `then_say`: a directive hands the result
    to the model to compose from, while a `then_say` is spoken verbatim and reads like a
    pasted search-results page.

Both paths are live-verified against CES (ces-probes 24-28). Note what is deliberately
absent: there is no per-turn "searchable now" switch, because `hide_tool()` does not gate a
managed tool — it breaks the turn (probe 26). Scope search to an agent, not to a moment.

Build + validate offline:
    python -m examples.grounded_search        # emits ./grounded_search_app
"""

from pydantic import BaseModel

import flows

# `preferred_domains` is the difference between a support agent and a web search box.
# `voice_prompt` matters more than it looks: the platform's default summariser asks for
# markdown lists and permits URLs, neither of which survives being spoken aloud.
support = flows.search_tool(
    "support_search",
    "Answer questions about shipping policies, delivery times, and customs rules.",
    preferred_domains=["acme-shipping.com"],
    voice_prompt=(
        "Answer in one or two short sentences. Never read out a URL or a list."
    ),
)


class Quote(BaseModel):
    quote_message: str
    success: bool = True


@flows.tool(flow="shipping")
def price_shipment(destination: str, weight_kg: str) -> Quote:
    """Quote the cost of shipping a parcel of a given weight to a destination."""
    return Quote(quote_message=f"Shipping {weight_kg}kg to {destination} costs $24.00.")


shipping = flows.Flow(
    "shipping",
    root_agent="Shipping_Desk",
    bootstrap={"welcome_slot": "welcome", "reset_on_complete": True},
)
shipping.add(
    flows.announce(
        "welcome",
        ["Shipping desk. I can quote a parcel for you."],
        shared=True,
    ),
    flows.user_slot("destination", "Where is the parcel going?"),
    flows.user_slot("weight_kg", "How much does it weigh, in kilograms?"),
    flows.result_slot("quote_msg", "quote"),
)
shipping.task(
    "quote", "price_shipment", ["destination", "weight_kg"], "quote_msg",
    out_key="quote_message", terminal=True, then_say="{quote_msg}",
)

# The flow-driven half: pass the SearchTool itself and `task` fills in what is not the
# author's to know — the platform's `query` parameter, `success_check="snippets"` (a search
# response has no `success` key), and the tool's own declaration.
customs = flows.Flow("customs", root_agent="Shipping_Desk",
                     bootstrap={"reset_on_complete": True})
customs.add(
    flows.user_slot("customs_question", "What would you like to know about customs?"),
    flows.result_slot("findings", "lookup"),
)
customs.task(
    "lookup", support, ["customs_question"], "findings", terminal=True,
    then_directive=("Answer the caller's question using the search results. "
                    "Two short sentences. Never read out a URL."),
)

app = flows.App(
    root_flow=shipping,
    extra_flows=[customs],
    app_display_name="Grounded Search Demo",
    model="gemini-3.5-flash",
    # The whole point: an off-script question mid-collection is answerable, and the model
    # summarises the search itself rather than reciting snippets.
    search_tools=[support],
)


if __name__ == "__main__":
    errors, warnings = flows.validate_app(app)
    assert errors == [], errors
    flows.build_app(app, "./grounded_search_app", overwrite=True)
    print(f"built -> ./grounded_search_app (warnings: {warnings})")
