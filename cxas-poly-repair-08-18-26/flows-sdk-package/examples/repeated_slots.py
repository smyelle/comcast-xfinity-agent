"""Repeated slots (Mode A): collect N scalars into one list-valued slot.

A repeated slot iterates its ask — `ask` for the first element, `ask_more` for the
rest — until a termination affordance fires: `until.max_count` (a hard cap) or
`until.done_setter` (the caller says "that's all"), with `min_count` AND-ed in so
"done" before the minimum re-asks. The collected list surfaces via a list-aware
`readback_fmt`; a repeated slot must NOT set per-element `requires_readback`.

Build + validate offline:
    python -m examples.repeated_slots        # emits ./repeated_slots_app

Deploy + drive (from a creds-enabled shell):
    flows deploy --app-dir ./repeated_slots_app --to <app-resource> --no-preserve
    cxas run-session text <app-resource>
"""

import flows

# A single flow. `bootstrap` seeds the welcome announce the moment the flow starts;
# without it the engine has nothing to run on turn 1.
pizza = flows.Flow("pizza_order", root_agent="Pizza_Agent",
                   bootstrap={"welcome_slot": "welcome"})

# The repeated slot. `user_slot` gives us the setter + No-Match ladder; the raw
# `repeated` block turns it into a collector (there is no DSL sugar for it yet).
topping = flows.user_slot("topping", "What topping would you like?")
topping["repeated"] = {
    "min_count": 1,                          # require at least one
    "until": {"max_count": 3},               # stop after three
    "ask_more": "Got it. Any other topping?",
}
topping["readback_fmt"] = {"type": "count", "one": "topping", "other": "toppings"}

pizza.add(
    flows.announce("welcome", ["Welcome to Tony's Pizza. Let's build your order."],
                   shared=True),
    topping,
    # Gate the terminal announce on the collected slot so it can't fire before
    # collection completes.
    flows.announce("done", ["Perfect. Your pizza order is complete. Enjoy!"],
                   requires=["topping"], end=True),
)

app = flows.App(
    root_flow=pizza,
    app_display_name="Repeated Slots — Pizza Toppings",
    model="gemini-3.5-flash",
)


if __name__ == "__main__":
    errors, warnings = flows.validate_app(app)
    assert errors == [], errors
    flows.build_app(app, "./repeated_slots_app", overwrite=True)
    print("built -> ./repeated_slots_app")
