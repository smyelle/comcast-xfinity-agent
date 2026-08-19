"""Slot-level validation, a shared multi-field setter, readback, and a task handoff.

Demos the validation/sensitivity + terminal-disposition surface:

  * `user_slot(validation=...)` — the FULL No-Match ladder verbatim (a mined error-code
    map), REPLACING the reprompt ladder built from reprompts/max_retries.
  * `user_slot(validation_rules=...)` — field-level checks the SETTER enforces on the
    captured value (length_digits, etc.), orthogonal to the No-Match ladder.
  * `user_slot(sensitive=True)` — a BUILD-TIME PHI/PCI marker. It is stripped before the
    validator/emit and gates terminal readback so the value is never spoken back.
  * `setter_group(op, slots)` — re-point N user slots at ONE shared `set_<op>_inputs`
    multi-field validating setter (CES's one-setter-per-flow shape).
  * `readback(fmt_type, **fields)` — the `readback_fmt` builder (here `prefix`).
  * `task(transfer_to=..., readback_inputs=...)` — terminal disposition: hand off to
    another agent on completion, and explicitly suppress input readback (PHI/PCI).

Build + validate offline:
    python -m examples.validation_and_sensitive   # emits ./validation_and_sensitive_app
"""

import flows

# Three identity fields validated TOGETHER by one shared `set_verify_inputs` setter
# (setter_group re-points each slot's setter + records its own field). Each carries its
# own field-level `validation_rules`; ssn_last4 is PHI (sensitive), so it is never spoken.
identity = flows.setter_group("verify", [
    flows.user_slot("account_id", "What's your 9-digit account number?",
                    validation_rules=[{"kind": "length_digits", "detail": "9"}]),
    flows.user_slot("ssn_last4", "And the last four digits of your SSN?",
                    sensitive=True,
                    validation_rules=[{"kind": "length_digits", "detail": "4"}]),
    # A MINED error-code ladder (not the reprompts/max_retries shape) passed verbatim
    # via `validation=` — it REPLACES the default No-Match ladder.
    flows.user_slot("zip_code", "What's the billing ZIP code on the account?",
                    validation={
                        "max_retries": 2,
                        "errors": {"invalid_length": "That should be 5 digits."},
                        "on_exhaust": {"say": "Let me get an agent to help.",
                                       "then": {"tool": "transfer_to_human"}},
                    }),
])

# A confirmed callback number, read back with a `prefix` readback_fmt ("ending in 1234").
callback = flows.user_slot("callback_phone", "What's the best callback number?",
                           readback=True,
                           validation_rules=[{"kind": "length_digits", "detail": "10"}])
callback["readback_fmt"] = flows.readback("prefix", text="ending in")

verify = flows.Flow("verify_identity", root_agent="Verify_Agent",
                    bootstrap={"welcome_slot": "welcome"})
verify.add(
    flows.announce("welcome", ["I can help once I verify your identity."], shared=True),
    *identity,
    callback,
    flows.result_slot("verify_res", "verify_task"),
)
# Terminal task: verify, then HAND OFF to the billing specialist. readback_inputs=False
# because the inputs include PHI (ssn_last4) that must not be spoken back.
verify.task("verify_task", "verify_identity_tool",
            ["account_id", "ssn_last4", "zip_code"], "verify_res",
            out_key="verified", terminal=True,
            then_say="You're verified. Connecting you to billing now.",
            transfer_to="Billing_Agent", readback_inputs=False,
            condition=flows.has("zip_code"))

app = flows.App(
    root_flow=verify,
    app_display_name="Identity Verification (validation + sensitive)",
    model="gemini-3.5-flash",
)


def _demo_run() -> None:
  """Show the flow opens on identity collection (offline engine, no LLM/creds)."""
  from flows.sim import engine_sim

  engine_sim.reset_store()
  sid, res = engine_sim.start(verify.to_config(), "verify_identity")
  print(f"  start -> next_action={res['next_action']!r} asks={res['agent_text'][:52]!r}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  assert errors == [], errors
  _demo_run()
  flows.build_app(app, "./validation_and_sensitive_app", overwrite=True)
  print("built -> ./validation_and_sensitive_app "
        "(proves: setter_group + validation/validation_rules/sensitive + "
        "readback + task transfer_to/readback_inputs)")
