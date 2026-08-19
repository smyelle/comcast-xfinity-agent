"""The recovery layer, in the model's own words.

An agent's questions already sound like a person asked them. Its RECOVERY does not.
Ask for an order number, get a cough, and the caller hears "Sorry, I didn't catch
that. What's your 8-digit order number?" — then, on the next miss, exactly the same
sentence again. Nothing in the flow is broken; it just stops sounding like anyone is
listening, which is the point at which people start saying "agent".

That is a channel choice, not a limitation. The framework has always had two ways to
put a line on the wire:

  * VERBATIM  — the engine preempts, renders the sentence itself, and the model never
                runs. The caller hears exactly the authored words.
  * DIRECTIVE — the engine hands the line to the model as an instruction, wrapped in
                <system_directive> under a guard that forbids reciting it. The model
                says the same thing in its own words.

A slot `ask` has always been on the second channel — that is why the question below
does not come out robotic. Everything in the recovery layer was pinned to the first.
`flows.speech(...)` is the one knob that moves a class of recovery utterance across.

Note what is NOT in the flow: no rewritten copy, no extra slots, no branching. The
authored lines stay exactly as they were and keep doing their old job — offline, or
on a turn a guard holds back, they are still what the caller hears. Opting a class in
promotes them from script to brief.

`improvise_style` is not decoration. Without it the model varies wording with no
brief, which is often worse than the canned line; with it the variation has a
direction. Treat it as part of turning the feature on.

Build + validate offline:
    python -m examples.improvise_recovery      # emits ./improvise_recovery_app
"""

import flows

# The one knob. Absent, every line below is spoken verbatim exactly as before.
recovery = flows.speech(
    improvise=["reprompt", "no_input", "exhaust"],
    improvise_style=(
        "Warm and brief. Never reuse your previous phrasing, and apologize at"
        " most once — a second apology sounds like the agent is stalling."),
)

# `validation` carries the no-match ladder verbatim. Under the policy above these
# lines stop being read out and start being briefs: the caller hears a different
# apology each time instead of the same sentence twice.
#
# The pair goes through `setter_group` because that is what makes the ladder
# REACHABLE. A plain `user_slot` setter stores whatever it is handed, so
# `validation_rules` never rejects anything and no-match never fires; the shared
# multi-field setter a group generates lowers those rules into real checks. Driven
# live before the group was added, "banana" was accepted as an order number and the
# whole recovery layer this example is about sat unused.
tracking = flows.setter_group("track", [
    flows.user_slot(
        "order_number", "What's your 8-digit order number?",
        validation={
            "max_retries": 3,
            "reprompts": [
                "Sorry, I didn't catch that. What's your 8-digit order number?",
                "One more time — the 8-digit order number, from your receipt.",
            ],
            "on_exhaust": {
                "say": "I'm still not getting that. Let me find someone who can help.",
                "then": {"tool": "transfer_to_human"},
            },
        },
        validation_rules=[{"kind": "length_digits", "detail": "8"}],
    ),
    # The postcode is the one line held back on purpose. A caller reading digits
    # back needs the same words every time to check themselves against, so the slot
    # opts out of a policy the rest of the flow opts in to.
    flows.user_slot(
        "postcode", "And the delivery postcode?",
        verbatim=True,
        validation={
            "max_retries": 3,
            "reprompts": ["Let's try that again. The delivery postcode, please."],
            "on_exhaust": {"say": "Let me get someone to help with that.",
                           "then": {"tool": "transfer_to_human"}},
        },
        validation_rules=[{"kind": "length_digits", "detail": "5"}],
    ),
])

track = flows.Flow(
    "track_order", root_agent="Tracking_Agent",
    speech=recovery,
    bootstrap={"welcome_slot": "welcome"},
    # Silence is a flow-level policy, so `no_input` improvises as one class. The
    # exhaust line here deliberately has no `then`: a disposition that fires a tool
    # would keep the line literal anyway, because the model's turn cannot carry a
    # tool call.
    no_input=flows.no_input(
        reprompts=["Are you still there?", "I'll wait a moment longer."],
        on_exhaust={"say": "I'll let you go for now. Call back any time."},
    ),
    escalate=flows.escalate(say="Let me put you through to someone who can help."),
)
track.add(
    flows.announce("welcome", ["I can help you track an order."], shared=True),
    *tracking,
    flows.result_slot("track_res", "track_task"),
)
track.task("track_task", "track_order_tool", ["order_number", "postcode"],
           "track_res", out_key="status", terminal=True,
           then_say="Your order is {status}.")

app = flows.App(
    root_flow=track,
    app_display_name="flows-improvise-demo Recovery",
    agent_instruction=(
        "You are an order-tracking agent. Follow the slot-filling framework"
        " directives exactly. Speak only what the framework gives you."
    ),
)


def _show_channels() -> None:
  """Print which lines the model may reword and which stay literal."""
  cfg = track.to_config()
  classes = cfg["speech"]["improvise"]
  print(f"  improvising: {', '.join(classes)}")
  for slot in cfg["slots"]:
    if not slot.get("validation"):
      continue
    pinned = slot.get("verbatim")
    print(f"  {slot['name']:14} recovery lines: "
          f"{'VERBATIM (pinned)' if pinned else 'model-reworded'}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./improvise_recovery_app")
    print("built: ./improvise_recovery_app")
    _show_channels()
