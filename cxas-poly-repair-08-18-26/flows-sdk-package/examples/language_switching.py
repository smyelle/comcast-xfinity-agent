"""A multilingual single-agent app that showcases in-conversation language switching.

The caller reaches a simple order-status flow in English and can switch to Spanish
(es-US) or Canadian French (fr-CA) at any point ("can you speak Spanish?", "en
français") and the agent continues in that language for the rest of the call.

`flows` wires the whole language layer from the three `App` fields below:
  * app.json `languageSettings` (default en-US + supported es-US/fr-CA + multilingual),
  * the `update_language` tool (gates + persists the switch into `active_language`),
  * a `<language_detection>` instruction block appended to the agent instruction
    (explicit-switch-only guardrails; the live model does not reliably auto-detect
    a switch), and the `active_language` state variable.

Build + validate offline:
    python -m examples.language_switching     # emits ./language_switching_app
"""

from pydantic import BaseModel, Field

import flows


class OrderStatus(BaseModel):
    status_message: str = Field(description="A caller-facing sentence with the order status")
    success: bool = True


@flows.tool(flow="order_status")
def lookup_order_status(order_number: str, language_choice: str = "") -> OrderStatus:
    """Look up the delivery status for an order number."""
    # The lock governs MODEL-generated text; deterministic/tool strings must be localized
    # at the tool boundary. The chosen language is passed in as a slot (the
    # translate-around-tool pattern), so the status is returned already in-language.
    low = str(language_choice).strip().lower()
    if low.startswith("span") or low.startswith("es"):
        return OrderStatus(status_message=f"El pedido {order_number} esta en camino y llega hoy.")
    if low.startswith("fren") or low.startswith("fr"):
        return OrderStatus(status_message=f"La commande {order_number} est en route et arrive aujourd'hui.")
    return OrderStatus(status_message=f"Order {order_number} is out for delivery and arrives today.")


# --- Order-status flow -------------------------------------------------------
# In "select" mode flows prepends a turn-1 language menu as the first user slot, so the
# flow itself opens straight into the business question (no separate welcome announce).
order = flows.Flow("order_status", root_agent="Order_Status_Agent")
order.add(
    # readback=True confirms the number before lookup (avoids mis-captured digits).
    flows.user_slot("order_number", "What's your order number?", readback=True),
    flows.result_slot("status_message", "lookup"),
    # preempt=True or the localized sentence is dropped: only a preempting announce
    # renders its own `texts`, and the point of the translate-around-tool pattern is
    # that the caller hears the tool's in-language string, not a model rewording.
    flows.announce("status", ["{status_message}"], requires=["status_message"],
                   end=True, preempt=True),
)
order.task(
    "lookup",
    "lookup_order_status",
    ["order_number", "language_choice"],  # pass the locked language for tool-side localization
    "status_message",
    out_key="status_message",
    requires=["order_number"],
    condition=flows.has("order_number"),
)


app = flows.App(
    root_flow=order,
    app_display_name="Flows Language Switching Demo",
    gcp_project="ces-deployment-dev",
    # gemini-3.5-flash drives the bidi voice path reliably here; gemini-3.1-flash-live
    # is the alternative live model (richer native multilingual audio, but flakier
    # end-of-turn over the scrapi bidi harness in this environment).
    model="gemini-3.5-flash",
    # The language layer. "select" = a turn-1 language menu (press 9 / say Spanish),
    # then the chosen language is HARD-LOCKED for the rest of the call.
    languages=["en-US", "es-US", "fr-CA"],
    default_language="en-US",
    language_switching="select",
    language_prompt=(
        "Welcome to Acme. Para espanol, marque nueve, or say Spanish. "
        "Otherwise, I'll continue in English. How can I help with your order?"
    ),
)


if __name__ == "__main__":
    errors, warnings = flows.validate_app(app)
    for w in warnings:
        print(f"warn: {w}")
    assert errors == [], errors
    flows.build_app(app, "./language_switching_app", overwrite=True)
    print("built -> ./language_switching_app")
