"""Work out what is actually broken before diagnosing it."""

# A slot's `filler_say` goes out as a partial preempt with the model's own reply
# following in the same turn, so it covers MODEL latency rather than a tool round trip —
# which is the wait every classifier-backed slot here incurs.
#
# The rules these lines follow:
#   * No dash. FLV001; dashes chop TTS.
#   * No audio tags. FLV003 makes a tag in a partial part an ERROR, and a filler IS a
#     partial part, so a tag truncates the very line meant to cover the wait.
#   * No claim the model has not made yet. A deterministic prefix steers what follows.
#   * Short. The line only has to reach the ear first, not fill the whole wait.
#   * No content word shared with the line that follows it, or the caller hears the same
#     word twice in one breath. `check_filler_echo` in ladder_check.py pins this.
#   * Lexically disjoint from every other live pool, including the six fixed tip fillers
#     (let / next / try / right / here / one). A pool is sampled at random, so
#     overlapping vocabularies eventually open two turns of one call the same way.
#     `check_filler_pool_collisions` in ladder_check.py enforces it.
#   * No idioms. The guidelines ban them outright: an idiom is the phrase a non-native
#     listener has to translate before they can hear the question that follows. Say it
#     literally instead.
FILLER_CLARIFY = ["Won't be long.", "Give me a second.", "Give me one moment."]

__all__ = [
    'FILLER_CLARIFY',
]


import clarify
import flows
import scripts
from journeys.common.rungs import advice_rung
from journeys.common.waiting import with_filler


# The intent clarification gate (see clarify.py). Deterministic: the engine matches the
# cue sets against the caller's own words, so "My Netflix doesn't work" asks the
# clarifying question while "nothing will load" goes straight to diagnostics.
def slots():
  """Work out what is actually broken before diagnosing it."""
  return [
      flows.passive_slot("complaint_scope", kind="intent",
                         option_cues=clarify.SCOPE_CUES),
      # cue_priority: a real product name beats the generic catch-all declared last.
      flows.passive_slot("app_name", option_cues=clarify.APP_CATALOGUE,
                         cue_priority="first"),
      with_filler(flows.intent_slot(
          "clarify_reply", clarify.REPLY_CUES,
          # An ask LADDER, not a sentence: the engine puts an outstanding question again
          # on every turn the caller has not answered it on, including the ones the
          # PLATFORM manufactured rather than the caller taking. One rung per turn,
          # clamped to the last, so nothing is ever said twice in the same words.
          ask=[clarify.ASK_CLARIFY, clarify.ASK_CLARIFY_AGAIN,
               clarify.SAY_CLARIFY_STILL_HERE],
          # A PRE-diagnostic question, and it must stay one: without the last two legs a
          # stray product name late in the call flips `complaint_scope` and re-opens the
          # gate mid-walkthrough. Equipment is excluded and asked by
          # `clarify_reply_device` below, because a pod is not "that app" and the contrast
          # it needs is against the connection.
          condition={"all": [clarify.APP_SPECIFIC_NOT_DEVICE,
                             scripts.WALKTHROUGH_NOT_OFFERED,
                             {"slot": "verdict_delivered", "filled": False}]},
          # Overlapping cue sets resolve by authored order (UNSURE is declared first), so
          # "I only tried Netflix" lands on UNSURE instead of matching two values and
          # filling nothing.
          cue_priority="first",
          # An answer neither the cues nor the model can resolve must not re-ask forever.
          # Two tries, then take the branch that just runs the diagnostics — the safe one.
          max_retries=2,
          # The reprompt names the miss as OURS and then puts the question again. No
          # apology: the guidelines allow none anywhere, error recovery included.
          reprompts=["I didn't catch that. Is it just that one, or are other sites "
                     "having trouble too?"],
          on_exhaust="No worries. Let me run a quick check on your connection.",
          on_exhaust_fill="UNSURE",
      ), FILLER_CLARIFY),
      # The equipment wording of the same question: same cue sets, same three branches,
      # only the sentence differs.
      with_filler(flows.intent_slot(
          "clarify_reply_device", clarify.REPLY_CUES,
          # The same ladder as its twin above, and the path the repeat was reported on:
          # this question is asked DURING the sweep, so the checks' own completion push
          # is the very next turn.
          ask=[clarify.ASK_CLARIFY_DEVICE, clarify.ASK_CLARIFY_DEVICE_AGAIN,
               clarify.SAY_CLARIFY_STILL_HERE],
          condition=clarify.DEVICE_SPECIFIC,
          cue_priority="first",
          max_retries=2,
          # Two sentences rather than one joined by a dash, which chops the audio at the
          # break (FLV001). The `on_exhaust` is word for word the twin's above.
          reprompts=["I didn't catch that. Is it just those, or is your internet having "
                     "trouble too?"],
          on_exhaust="No worries. Let me run a quick check on your connection.",
          on_exhaust_fill="UNSURE",
      ), FILLER_CLARIFY),
  ]


# Announces cascade onto the same action, so the bridge and the verdict land in one turn
# — which is what the source means by "respond, then proceed immediately to the
# diagnostic turn".
def bridges():
  """Acknowledge the answer, then carry straight on to the checks."""
  return [
      flows.announce("bridge_everything_down", [clarify.SAY_EVERYTHING_DOWN],
                     condition=clarify.REPLY_EVERYTHING_DOWN),
      flows.announce("bridge_unsure", [clarify.SAY_UNSURE],
                     condition=clarify.REPLY_UNSURE),
  ]


def advice():
  """Only one app is affected and the line is clean, so advise and stop."""
  return [
      # P6b — only one app is affected and every measured plant check came back clean.
      # Below the plant faults, because a real fault explains the app too; above the
      # reboot offer, because a gateway restart is the wrong thing to propose here.
      #
      # `WALKTHROUGH_NOT_OFFERED` because the all-clear does not latch
      # `verdict_delivered` (it leaves the ladder open for the answer), so without it this
      # rung stays eligible for the rest of the call and contradicts what was just said.
      #
      # Never for EQUIPMENT: the engine keeps walking tasks within a turn, and this
      # verbatim `then_say` beats the device search's directive.
      advice_rung("AdviseAppSpecific", "verdict_app_specific",
                  {"all": [clarify.ONLY_APP,
                           scripts.WALKTHROUGH_NOT_OFFERED,
                           {"not": clarify.DEVICE_NAMED},
                           {"slot": "device_searched", "filled": False}]},
                  clarify.SAY_ONLY_APP),
  ]
