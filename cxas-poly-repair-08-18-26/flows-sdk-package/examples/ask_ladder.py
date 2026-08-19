"""Ask the same question a different way each time it goes unanswered.

A question the caller does not answer gets asked again. As a single fixed string it is
asked again WORD FOR WORD, which reads as the agent not listening, and the closing
"anything else?" question is the worst case, because it is usually the last open slot
and therefore the flow's idle prompt: every turn the flow cannot use re-asks it, for
the rest of the call.

`reprompts` does not cover this. Those are indexed by the validation-retry count and
only fire when a value was offered and REJECTED. A caller who says something the flow
cannot use at all ("hmm", "hold on", a complaint) produces no value, no error, no
retry, and so no new wording.

An `ask` may be a LIST instead: one rung per re-ask.

    caller                          what the flow does
    ------------------------------  --------------------------------------------
    "my statement looks wrong"      captures the topic, then asks rung ONE
    "hmm"                           nothing to capture -> asks rung TWO, which is
                                    worded differently rather than repeated
    "I'm not sure"                  still nothing -> rung THREE, which stops
                                    asking an open question and offers a menu
    "let's do the plan"             captured; the ladder is irrelevant now

Each piece earns its place:

* The rungs ESCALATE IN KIND, not just in wording. Rung one is open, rung two offers
  an exit, rung three narrows to a menu. Three paraphrases of one question help nobody;
  a caller who did not answer an open question twice usually needs choices.
* The ladder CLAMPS to its last rung rather than draining to silence. A question the
  caller is expected to answer must always be asked; running out mid-conversation would
  leave the turn empty and the model would invent something to fill it.
* The rung is claimed ONCE PER TURN, not once per engine pass. One caller turn drives
  several passes when a tool fires, and each re-derives the question, counting passes
  would skip rungs and, worse, make two passes of the same turn say different things.
* `ask=[]` is an authoring error, not a silent no-question. Omit `ask` to leave a slot
  unasked.

Build + validate offline:
    python -m examples.ask_ladder      # emits ./ask_ladder_app
"""

import flows


# One rung per re-ask. The last one is where the ladder stays, so it is the one that
# has to work indefinitely, which is why it names concrete options instead of asking
# the same open question a third time.
CLOSING = [
    "Anything else I can help you with?",
    "Was there anything else, or shall I let you go?",
    "I can look at your billing, your plan, or your equipment, which would help most?",
]


def build() -> flows.App:
  """A two-slot flow whose closing question varies as it is re-asked."""
  flow = flows.Flow("account_help", root_agent="account_agent")

  flow.add(flows.user_slot(
      "topic",
      ask="What can I help you with today?",
      hint="what the caller wants help with",
  ))
  # The ladder. This is the LAST slot, so it is also the flow's idle prompt: any turn
  # the flow cannot use lands back here. That is exactly the slot a ladder is for.
  flow.add(flows.user_slot(
      "anything_else",
      ask=CLOSING,
      hint="whether the caller needs anything else",
      requires=["topic"],
  ))

  return flows.App(
      root_flow=flow,
      app_display_name="ask-ladder",
      agent_instruction=(
          "You help with account questions. Answer what you can, and let the engine "
          "decide what to ask next, never invent a closing question of your own."
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
    flows.build_app(app, "./ask_ladder_app", overwrite=True)
    print("built: ./ask_ladder_app")
