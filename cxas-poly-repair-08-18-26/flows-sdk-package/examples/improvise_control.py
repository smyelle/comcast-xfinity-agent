"""Improvised and pinned copy in the same agent, and how to tell which is which.

Turning improvisation on flow-wide is only safe if some lines can be held back, and
the lines that need holding back are not the ones an author would guess. A single
escalation path says three different kinds of thing:

  1. "Let me try to help first."          — deflection. Reword it; saying it the same
                                            way twice is exactly what makes a caller
                                            escalate harder.
  2. "Are you sure you want an agent?"    — courtesy, asked while the request sits
                                            pending confirmation.
  3. "Connecting you. This call may be
     recorded for quality."               — contractual. Not one word different.

The policy opts `control` in, and only case 1 actually moves. That is not a
limitation to work around — it is the structure being honest, and it lands the right
way round:

  * Case 3 rides a turn that has already terminated and carries a transfer part, and
    the model's turn can deliver neither. The contractual line is the one that most
    needs pinning, and it is pinned whatever anyone writes.
  * Case 2 is asked with the control slot PENDING, which routes the turn down the
    readback protocol rather than the directive fold. Also pinned, structurally.
  * So `control` means the deflection line — which is the one that most needs to stop
    repeating. The validator warns if you opt the class in with no `declined_say`,
    because then it reaches nothing.

Note what this example does NOT do: pass `verbatim=True` to `escalate()`. The flag is
BLOCK-level, so pinning the contractual line that way would pin the deflection copy
with it — the one line here that had something to gain. The runtime already holds
case 3 literal, so the flag would buy nothing and cost the feature.

Per-SLOT the flag earns its keep, and the SSN slot uses it: a caller reading digits
back needs a fixed sentence to check themselves against, not a fresh one each time.

Rule of thumb: improvise what the agent says to be pleasant, pin what it says to be
accurate — and check which of the two a line actually is before reaching for a flag.

Build + validate offline:
    python -m examples.improvise_control       # emits ./improvise_control_app
"""

import flows

policy = flows.speech(
    improvise=["reprompt", "control"],
    improvise_style="Warm and brief. Do not repeat your previous wording.",
)

# `declined_say` is a LADDER precisely because a repeated refusal is the failure
# mode; the policy varies the wording on top of that. The other two lines stay
# literal on their own, and NOT passing `verbatim=True` here is deliberate: the flag
# is block-level, so setting it would pin the deflection copy too — the one line in
# this block that had something to gain.
human = flows.escalate(
    say="Connecting you now. This call may be recorded for quality.",
    confirm_say="Just to confirm — you'd like me to bring in a colleague?",
    declined_say=[
        "Let me try to help first — what's going wrong?",
        "Understood. Putting you through.",
    ],
    requires_readback=True,
    condition=flows.gate({"slot": "escalate_declined", "gte": 1}),
)

account = flows.user_slot(
    "account_id", "What's your account number?",
    validation={
        "max_retries": 3,
        "reprompts": ["Sorry, I missed that. Your account number?"],
        "on_exhaust": {"say": "Let me get someone to help.", "then": {"tool": "transfer_to_human"}},
    },
)

# Pinned: a fixed sentence is what a caller checks their own digits against.
ssn = flows.user_slot(
    "ssn_last4", "And the last four digits of your SSN?",
    sensitive=True, verbatim=True,
    validation={
        "max_retries": 3,
        "reprompts": ["Let's try again — the last four digits only."],
        "on_exhaust": {"say": "Let me get someone to help.", "then": {"tool": "transfer_to_human"}},
    },
    validation_rules=[{"kind": "length_digits", "detail": "4"}],
)

support = flows.Flow(
    "account_support", root_agent="Support_Agent",
    speech=policy,
    bootstrap={"welcome_slot": "welcome"},
    escalate=human,
    cancel=flows.cancel(say="No problem, I've stopped that.",
                        confirm_say="Just to check — cancel this and start over?",
                        requires_readback=True),
    # `control` reaches `declined_say` and nothing else, so the escalate block above
    # is what the class is for; cancel here is an ordinary teardown.
)
support.add(
    flows.announce("welcome", ["I can help with your account."], shared=True),
    account,
    ssn,
    flows.result_slot("lookup_res", "lookup_task"),
)
support.task("lookup_task", "account_lookup_tool", ["account_id", "ssn_last4"],
             "lookup_res", out_key="summary", terminal=True,
             then_say="Here's what I have: {summary}.",
             readback_inputs=False)

app = flows.App(
    root_flow=support,
    app_display_name="flows-improvise-demo Control",
    agent_instruction=(
        "You are an account support agent. Follow the slot-filling framework"
        " directives exactly. Speak only what the framework gives you."
    ),
)


def _show_split() -> None:
  """Print the improvised/pinned split — the point of the example."""
  cfg = support.to_config()
  print(f"  improvising: {', '.join(cfg['speech']['improvise'])}")
  print("  escalate    say + confirm_say PINNED structurally, "
        "declined_say reworded")
  for slot in cfg["slots"]:
    if slot.get("validation"):
      print(f"  {slot['name']:12} "
            f"{'PINNED' if slot.get('verbatim') else 'reworded'}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./improvise_control_app")
    print("built: ./improvise_control_app")
    _show_split()
