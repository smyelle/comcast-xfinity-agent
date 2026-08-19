"""Repeated collection (Mode A): collect N scalars into ONE list-valued slot.

Demos the `repeated(...)` builder and the list-aware `readback(...)` formats:

  * `repeated(until_max=..., done_setter=..., min_count=...)` — the termination
    affordance is EXPLICIT: a hard cap (`until_max`), a caller-said-"that's all" signal
    (`done_setter`), floored by `min_count`. `repeated()` raises if neither cap is given
    (the framework rejects an unbounded loop). `ask_more` is the follow-up ask for
    elements after the first.
  * `readback("count", one=..., other=...)` — len()-based plural of the collected list
    ("You've added 3 toppings").
  * `readback("join", each=..., sep=...)` — join the list elements into a spoken phrase.

The `done_setter` must be a real tool, so it is authored with `@flows.tool`. A repeated
slot must NOT set per-element `requires_readback`; the list surfaces via `readback_fmt`.

The terminal `place_pizza_order` task fires once the loop completes (at `until_max`, or on
"that's all"). Because it consumes the repeated (list-valued) slot, its executor types the
input as `list[str]` — a scalar `str` param raises a runtime type error at the tool call.

Build + validate offline:
    python -m examples.repeated_collection        # emits ./repeated_collection_app
"""

import flows


@flows.tool(flow="pizza_order")
def set_toppings_done(done: str = "") -> dict:
  """Record that the caller is finished adding toppings ("that's all")."""
  return {"stored": True, "value": True}


@flows.tool(flow="pizza_order")
def place_pizza_order(topping: list[str] | None = None) -> dict:
  """Place the pizza order for the collected toppings; return a confirmation code.

  `topping` is the repeated (list-valued) slot, so the parameter is a `list[str]`, not a
  scalar — a terminal task consuming a repeated slot must type its input as a list.
  """
  toppings = topping or []
  return {"success": True, "confirmation": "PZ-7788", "count": len(toppings)}


# The repeated (list-valued) slot. `user_slot` gives the setter + No-Match ladder;
# the `repeated` block turns it into a collector with an explicit termination affordance.
topping = flows.user_slot("topping", "What's the first topping you'd like?")
topping["repeated"] = flows.repeated(
    until_max=3,                       # hard cap: the loop completes after three toppings
    done_setter="set_toppings_done",   # caller can also say "that's all" to finish early
    min_count=1,                       # require at least one before "done" is honored
    ask_more="Got it. Any other topping?",
)
# Read the collected list back as a spoken, comma-joined phrase.
topping["readback_fmt"] = flows.readback("join", each="{item}", sep=", ")

pizza = flows.Flow("pizza_order", root_agent="Pizza_Agent",
                   bootstrap={"welcome_slot": "welcome"})
pizza.add(
    flows.announce("welcome", ["Welcome to Tony's. Let's build your pizza."],
                   shared=True),
    topping,
    flows.result_slot("order_conf", "place_task"),
)
# Terminal task: once the loop completes (at until_max, or when the caller says "that's
# all"), place the order for the collected toppings and close with the confirmation.
pizza.task(
    flows.task("place_task", "place_pizza_order", ["topping"], "order_conf",
               out_key="confirmation", requires=["topping"], terminal=True,
               then_say="You're all set — order confirmation {order_conf}. Enjoy!"))

app = flows.App(
    root_flow=pizza,
    app_display_name="Repeated Collection — Pizza Toppings",
    model="gemini-3.5-flash",
)

# Both list-aware readback formats are valid builders (count is the plural-by-len form).
_COUNT_FMT = flows.readback("count", one="topping", other="toppings")


def _demo_run() -> None:
  """Show the flow opens on the first-topping ask (offline engine, no LLM/creds)."""
  from flows.sim import engine_sim

  engine_sim.reset_store()
  sid, res = engine_sim.start(pizza.to_config(), "pizza_order")
  print(f"  start -> asks={res['agent_text'][:48]!r}")
  print(f"  readback join  = {topping['readback_fmt']}")
  print(f"  readback count = {_COUNT_FMT}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  assert errors == [], errors
  _demo_run()
  flows.build_app(app, "./repeated_collection_app", overwrite=True)
  print("built -> ./repeated_collection_app "
        "(proves: repeated() until_max+done_setter+min_count + readback count/join)")
