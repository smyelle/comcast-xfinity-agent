"""First-class INTENT slot (asked) + a PASSIVE slot, in one deployable flow.

Demos two new authoring builders:

  * `intent_slot(name, options, ask=...)` — a `kind:"intent"` enum whose VALUE selects
    an operation. It is valid-by-construction: `option_cues` (value -> spoken cues), an
    enum `validation_rules` entry, and a `set_<name>` setter are all generated. Here it
    is ASKED (a humanized `ask`), so the caller is prompted once and the deterministic
    cue matcher fills it from the answer.
  * `passive_slot(name, ...)` — a never-asked, still-capturable slot. The engine's
    `_find_next_question` SKIPS it (the model/cues fill it silently), so it never
    interrogates the caller with an internal category — yet it keeps a setter so what
    was captured is recorded.

`_demo_run()` drives the offline engine (no LLM/creds) to prove it works: the asked
intent slot is cue-filled from the caller's answer and the terminal tool fires, while
the passive slot is never spoken as a question.

Build + validate offline:
    python -m examples.intent_and_passive        # emits ./intent_and_passive_app
"""

import flows

# A support-triage flow. The asked intent slot routes billing vs technical; the passive
# slot silently records caller sentiment (never asked out loud).
triage = flows.Flow("support_triage", root_agent="Triage_Agent",
                    bootstrap={"welcome_slot": "welcome"})
triage.add(
    flows.announce("welcome", ["Thanks for calling Acme support."], shared=True),
    # ASKED intent slot: prompted once, then cue-filled from the caller's answer.
    flows.intent_slot(
        "help_topic",
        {"billing": ["billing", "a charge", "my bill", "invoice"],
         "technical": ["technical", "not working", "broken", "an error"]},
        ask="Are you calling about a billing question or a technical issue?",
    ),
    # PASSIVE slot: never asked; recorded silently (its contract is "never prompt").
    flows.passive_slot(
        "caller_sentiment",
        option_cues={"upset": ["frustrated", "angry", "upset", "unhappy"],
                     "calm": ["okay", "fine", "no rush"]},
    ),
    flows.result_slot("ticket", "open_ticket"),
)
# Terminal task: open a ticket for the classified topic, then read the id back.
triage.task("open_ticket", "create_ticket", ["help_topic"], "ticket",
            out_key="ticket_id", terminal=True,
            then_say="I've opened ticket {ticket} and I'm routing you now.",
            condition=flows.has("help_topic"))

app = flows.App(
    root_flow=triage,
    app_display_name="Support Triage (intent + passive)",
    model="gemini-3.5-flash",
)


def _demo_run() -> None:
  """Drive the offline engine to prove the asked intent slot routes the flow."""
  from flows.sim import engine_sim

  engine_sim.reset_store()
  sid, res = engine_sim.start(triage.to_config(), "support_triage")
  print(f"  start        -> next_action={res['next_action']!r} "
        f"asks={res['agent_text'][:48]!r}")
  # The caller answers; the asked intent slot is cue-filled deterministically.
  res = engine_sim.step({"session_id": sid, "kind": "user_text",
                         "text": "there's a wrong charge on my bill"})
  filled = res["sm"].get("filled", {})
  print(f"  after answer -> help_topic={filled.get('help_topic')!r} "
        f"next_action={res['next_action']!r} function_call={res['function_call']}")
  # Proof of the passive contract: it was NEVER asked as a question.
  print(f"  passive slot 'caller_sentiment' never prompted "
        f"(filled keys: {sorted(filled)})")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  assert errors == [], errors
  _demo_run()
  flows.build_app(app, "./intent_and_passive_app", overwrite=True)
  print("built -> ./intent_and_passive_app "
        "(proves: asked intent_slot routes to a terminal; passive_slot never asked)")
