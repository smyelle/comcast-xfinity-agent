"""Two signals in one breath.

One utterance fills ONE intent slot. That default is right: an utterance usually
expresses one intent, and letting overlapping vocabulary set several slots at once makes
silent decisions the author never asked for.

Some signals genuinely travel together, though. "That's a lot, can you waive the fee"
carries two things at once —

    WHAT the caller wants     -> drives the flow
    HOW they feel about it    -> picks the wording that answers them

— and they belong in different slots, because only the first one decides what happens
next. With a single winner the second never fills, and the agent answers a concern the
caller did not raise: told "that's a lot", it replies "I understand this charge is
unexpected". Close enough to sound right, wrong enough to sound like it was not
listening.

Merging them into one enum only works while the signals are mutually exclusive. These
are not: any request can arrive in any tone, so a combined enum needs a value per pair
and grows multiplicatively.

`multi_fill=True` opts a slot in to filling from an utterance that ALSO fills another.

    caller                            what fills
    --------------------------------  ------------------------------------------
    "can you waive the fee"           request=waive            (tone unmatched)
    "that's a lot, can you waive it"  request=waive, tone=cost (both, one breath)
    "that's expensive"                tone=cost                (alone is fine too)

Each piece earns its place:

* The cue sets must be DISJOINT. `multi_fill` lets two slots fill from one utterance; it
  does not decide what a phrase means. If "waive" appeared in both sets, one word would
  set two slots and the author would not be able to say which reading was intended.
* An AMBIGUOUS match still fills nothing. If the caller hits two values of the same
  slot, that slot is skipped exactly as before — the guard that makes filling several
  slots safe at all is that each one's own match is unambiguous.
* It is per-slot and off by default, so nothing that exists today changes.

Build + validate offline:
    python -m examples.multi_fill      # emits ./multi_fill_app
"""

import flows


# WHAT the caller is asking for. This one drives the flow.
REQUEST = {
    "waive": [r"\bwaive\b", r"\bremove\b", r"\btake .{0,10}off\b"],
    "explain": [r"\bwhy\b", r"\bwhat is\b", r"\bexplain\b"],
}
# HOW they framed it. Never asked, never drives anything — it only chooses which
# acknowledgement is true to what they said. Disjoint from REQUEST on purpose.
TONE = {
    "cost": [r"\bthat's a lot\b", r"\bexpensive\b", r"\btoo much\b",
             r"\bcan'?t afford\b"],
    "surprise": [r"\bdidn'?t expect\b", r"\bunexpected\b", r"\bnever agreed\b"],
}


def build() -> flows.App:
  """A flow that answers the request and matches the caller's framing."""
  flow = flows.Flow("charge_help", root_agent="charge_agent")

  flow.add(flows.user_slot(
      "account",
      ask="What's the account number?",
      hint="the account number",
  ))
  flow.add(flows.intent_slot("request", REQUEST, passive=True,
                             requires=["account"]))
  # The opt-in. Without it this slot never fills on a turn that also names a request,
  # which is the only turn it is ever likely to arrive on.
  flow.add(flows.intent_slot("tone", TONE, passive=True, multi_fill=True,
                             requires=["account"]))

  # Mutually exclusive, because announces cascade — an overlap would say both.
  flow.add(flows.announce(
      "ack_cost", ["I understand a charge like that can be stressful."],
      requires=["request"], condition={"slot": "tone", "eq": "cost"}))
  flow.add(flows.announce(
      "ack_surprise", ["I understand this charge is unexpected."],
      requires=["request"], condition={"slot": "tone", "eq": "surprise"}))
  flow.add(flows.announce(
      "offer", ["Let me see what I can do about it."],
      requires=["request"], end=True))

  return flows.App(
      root_flow=flow,
      app_display_name="multi-fill",
      agent_instruction=(
          "You help with charges on an account. Let the engine decide what to say "
          "about the charge — never offer to remove one yourself."
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
    flows.build_app(app, "./multi_fill_app", overwrite=True)
    print("built: ./multi_fill_app")
