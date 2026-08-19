"""The Config -> Flows-DSL source renderer (round-trips byte-for-byte).

Demos the migration deliverable (feature 1):

  * `render_config_source(config, config_id=..., root_agent=...)` — turn a plain Config
    dict back into an importable, human-readable `flows` authoring module. The ROUND-TRIP
    contract holds: exec(source); ns["flow"].to_config() == config (order preserved).
  * `raw({...})` — the greppable identity marker the renderer falls back to for any
    slot/task a high-level builder can't reproduce byte-for-byte.
  * `render_app_source(app_spec)` — a thin single-flow App wrapper module.

This demo does NOT build an app dir; it proves the renderer round-trips and prints the
generated source. `build()` exposes the round-trip result for the test harness.

Run:
    python -m examples.render_source
"""

import flows

# A small flow exercising several builders (announce/user_slot/intent_slot/result_slot +
# a task) plus a slot the high-level builders can't reproduce, forcing a `raw({...})`
# fallback — so the demo shows BOTH the idiomatic-builder and raw paths round-trip.
flow = flows.Flow("account_help", root_agent="Account_Agent",
                  bootstrap={"welcome_slot": "welcome"})
flow.add(
    flows.announce("welcome", ["Hi! I can help with your account."], shared=True),
    flows.intent_slot("help_topic",
                      {"reset": ["reset my password"], "unlock": ["unlock my account"]},
                      ask="Do you need a password reset or an account unlock?"),
    flows.user_slot("email", "What's the email on the account?"),
    # A slot no high-level builder reproduces (an internal flag) -> renders as raw({...}).
    flows.raw({"name": "consent_flag", "source": "user",
               "setter": "set_consent_flag", "condition": "lambda f: False"}),
    flows.result_slot("outcome", "resolve"),
)
flow.task("resolve", "resolve_account", ["help_topic", "email"], "outcome",
          out_key="status", terminal=True, then_say="Done — {outcome}.",
          condition=flows.has("email"))


def build() -> dict:
  """Render the flow to source, exec it, and return the round-trip result.

  Returns a dict with the original config, the rendered source, and the round-tripped
  config so the test harness can assert equality.
  """
  config = flow.to_config()
  source = flows.render_config_source(config, config_id=flow.config_id,
                                      root_agent=flow.root_agent)
  ns: dict = {}
  exec(source, ns)  # noqa: S102 — exercising the rendered module is the whole point
  round_tripped = ns["flow"].to_config()
  return {"config": config, "source": source, "round_tripped": round_tripped}


def _demo_run() -> None:
  result = build()
  # ROUND-TRIP: order-sensitive equality — byte-for-byte, order preserved.
  assert list(result["round_tripped"].items()) == list(result["config"].items()), (
      "round-trip mismatch")
  print("round-trip OK: exec(render_config_source(cfg)) -> flow.to_config() == cfg")
  # The raw fallback is present + greppable in the generated source.
  assert "raw(" in result["source"], "expected a raw({...}) fallback in the source"
  print("raw({...}) fallback present for the non-builder slot")
  # render_app_source wraps the same flow with a flows.App(...) binding.
  app_src = flows.render_app_source(
      {"config": result["config"], "config_id": flow.config_id,
       "root_agent": flow.root_agent, "app_display_name": "Account Help"})
  assert "flows.App(root_flow=flow" in app_src
  print("render_app_source() emits a flows.App(...) wrapper module\n")
  print("=" * 78)
  print(result["source"])
  print("=" * 78)


if __name__ == "__main__":
  _demo_run()
  print("done -> render_config_source round-trips; raw() fallback + render_app_source shown")
