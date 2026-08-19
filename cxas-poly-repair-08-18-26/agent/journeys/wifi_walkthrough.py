"""Walking the caller round their own house, tip by tip."""

# A plain either/or with a concrete subject, which a member can answer on the first
# listen. This slot's own reprompt repeats it verbatim after naming the miss, so the ask
# and the re-ask cannot diverge.
ASK_WIFI_SCOPE = (
    "Is it everything in the house, or just the one device?"
)

# The SECOND time of asking, and the reason it exists is that the first time is the
# ordinary case: the question is hoisted into the sweep on nearly every call, so a caller
# who did not answer it there hears it again the moment the walkthrough starts. Measured
# live, that was `ASK_WIFI_SCOPE` word for word, two turns apart, which is what makes an
# agent sound like a recording rather than someone who was listening.
#
# It says out loud that we are coming back to something, because we are. That is what a
# person does with a question they have already put, and it costs one short clause.
#
# The question itself is REWORDED rather than repeated. Both wordings name the same two
# answers and both sit in `WIFI_SCOPE_CUES` verbatim ("the whole house", "one device"), so
# the capture is no weaker for it.
ASK_WIFI_SCOPE_AGAIN = (
    "Back to the one thing I asked earlier. Is it the whole house, or just one device?"
)

# The same question, asked while the diagnostics job is still running. It is the only
# part of the walkthrough that may be hoisted into the wait, because it is the only part
# that ASSERTS NOTHING: scope is a symptom, and the answer is worth having whichever way
# the checks land. The offer, the consent and every tip depend on the checks coming back
# clean and stay where they are.
#
# Standalone, because the approved line is not: `ASK_WIFI_SCOPE` follows the offer to
# walk the caller through a few things, and reaches them as a non sequitur without it.
#
# Commas, not dashes: dashes are chopped by the voice pipeline, and check_journeys bans
# them across the corpus.
ASK_WIFI_SCOPE_EARLY = (
    "While those checks run, one thing that helps either way. "
    "Is it everything in the house, or just the one device?"
)

# When that question may be asked. The last leg is the clarification carve-out, and it is
# why the question cannot simply always ride the bridge line: a caller who named a single
# app is asked the CLARIFICATION question, which asks nearly the same thing.
EARLY_SCOPE_ASKABLE = {"all": [
    # Still running. `network_status` is the honest test for "the specialists have not
    # answered" -- `diagnostics_complete` alone is also unfilled on a path where the gate
    # short circuited and there is no wait to fill.
    {"slot": "diagnostics_complete", "filled": False},
    {"slot": "network_status", "filled": False},
    # Nothing to ask if the answer is already in hand.
    {"slot": "wifi_scope", "filled": False},
    # A caller asking about one device is not asked a whole-house question.
    {"slot": "device_searched", "filled": False},
    {"any": [{"slot": "complaint_scope", "neq": "app_specific"},
             {"slot": "clarify_reply", "filled": True},
             {"slot": "clarify_reply_device", "filled": True}]},
    # ...and never to a caller who has just ANSWERED the equipment question with "only
    # that". `device_searched` above says the same thing one turn too late: the search
    # fires on this very turn, so the announce is evaluated while the latch is still
    # empty, and the caller heard "is it everything in the house, or just the one
    # device?" one breath after telling us it was only their TV box and the internet was
    # fine. Measured live, cold, on the demo build.
    #
    # Nothing consumes the answer on this path either. A device search shuts the
    # walkthrough out (both all-clear rungs are gated on `device_searched`), so the
    # question is asked, answered and thrown away.
    #
    # `neq` holds on an unfilled slot, which is what keeps the question for everyone
    # else. Only the DEVICE reply is named: the app reply reaches the same value, but
    # `AdviseAppSpecific` is gated on the sweep having finished, so on the app path this
    # question is the only thing that owns the turn the caller answers on.
    {"slot": "clarify_reply_device", "neq": "ONLY_APP"},
]}

# The same offer to a caller who said it was EVERYTHING. One clause differs: "on that
# device" is a promise about a device the caller has just said does not exist, so this
# wording describes what the quick things actually are for them.
#
# The opening words are shared with the one-device wording deliberately:
# `tests/scope_voice_probe.py` reads them off a live call to tell an answer that landed
# from one that was lost, and both answers have to be visible to it.
#
# No idiom in the closing question: the guidelines ban them outright, and the answer here
# has to be immediate rather than translated first.
SAY_SCOPE_NOTED_ALL_DEVICES = (
    "Got it, that helps. While those checks finish, we could try a couple of quick "
    "things around the house. Want to try them now?"
)

# The third answer to that question, and until now the only one nobody replied to.
# Measured live, cold: asked whether it was everything or one device, the caller said
# "I'm not sure to be honest" and the agent said NOTHING AT ALL. The two rungs above own
# the turn for an answer the cues resolve; an answer of "I don't know" resolves to no
# scope, so no rung was eligible and the turn fell through to a model with nothing to do.
#
# What it may say is limited by what is true. The checks measure our plant and say
# nothing about which of the caller's devices are affected, so this must not promise that
# they will settle the question, and it must not promise to ask again either -- a fault
# verdict ends the walkthrough and no second ask ever happens. It acknowledges, and it
# names what we are actually doing now.
#
# No offer, unlike its two siblings. The offer's two wordings are picked BY the scope
# answer ("things on that device" against "things around the house"), so there is no
# honest way to make it without one.
SAY_SCOPE_UNSURE = (
    "That's fine. Let's see what those checks say first."
)

# Four candidates for at most three turns, so a tip the caller has already tried can be
# skipped rather than spent. One action or one question per turn, never both.
#
# "WiFi", one word, capital W and capital F. The brand spells it that way, and the
# hyphenated form is a compound hyphen the voice pipeline chops (AGENTS.md rule 6).
SAY_WIFI_TIP_REJOIN = (
    "On the device that's struggling, forget the home network in its WiFi settings, "
    "then join it again. Did that help?"
)

SAY_WIFI_TIP_CLOSER = (
    "Try moving closer to your gateway, and check nothing large or metal is sitting "
    "right against it. Did that make a difference?"
)

SAY_WIFI_TIP_TOGGLE = (
    "Turn the device's WiFi off and back on again. On a phone, airplane mode for a "
    "few seconds does the same thing. Any change?"
)

SAY_WIFI_TIP_NEARBY = (
    "Try one device right next to the gateway and see if it behaves any differently "
    "there. That tells us whether it's the coverage around the house or the connection "
    "itself. How does it look up close?"
)

SAY_WIFI_TIP_RESTART = (
    "Restart the device itself, then let it reconnect. How does it look after that?"
)

SAY_WIFI_FIXED = (
    "That's good to hear. If anything else comes up, we're here."
)

# The caller has just gone and done something and come back to say how it went, and on
# this one turn the checks report as well. Both are owed, and the answer is owed FIRST:
# the verdict is about our plant and says nothing about the thing they were asked to try,
# so leading with it reads as not having listened.
#
# ONE short sentence, because everything it leads is long: the all-clear is three
# sentences and the technician verdict four, and a fuller acknowledgement here would push
# the turn past what a listener can hold. It thanks them for the ACTION rather than
# reacting to the outcome, so the same line is true whether the tip helped or not, and
# ahead of a healthy verdict or a fault one.
#
# It must not promise anything about what follows. Whether the walkthrough carries on is
# the verdict's business, not this line's: on an all-clear the next tip follows in the
# same breath, and on a measured fault every tip goes dark.
SAY_WIFI_TIP_ACKNOWLEDGED = (
    "Thanks for trying that."
)

# Declining is not a dead end: the requirement is explicit that a caller who does not
# want to troubleshoot is handed to a person rather than closed on.
#
# No idiom, and the handover is not left vague: naming what the person will do is both
# plainer and warmer.
SAY_WIFI_DECLINED = (
    "No problem at all. Let me get you to someone who can help you with this."
)

SAY_WIFI_EXHAUSTED = (
    "That's everything I can try from here. Let me get you to someone who can take a "
    "closer look at your home setup."
)

# Rung fillers, covering the MODEL wait on a walkthrough turn. No slot is pending on
# these turns, so a slot filler cannot reach them and the only lever is the task's own
# `filler_say`. The repo's ban on splitting a say-only rung is about TOOL latency, and
# these tools are instant; the wait here is the model round trip.
#
# A single fixed line per rung, deliberately NOT a pool: `ladder_check` pins the two
# halves by comparing the rejoined string to the approved sentence, and a rotating first
# half would make that unpinnable. Distinct wording per rung is what avoids the tic.
FILLER_ASK_SCOPE = "Got it."

FILLER_TIP_REJOIN = "Let's start simple."

FILLER_TIP_CLOSER = "Next one."

FILLER_TIP_TOGGLE = "Try this one."

FILLER_TIP_PLACEMENT = "Right then."

FILLER_TIP_NEARBY = "Here's another."

FILLER_TIP_RESTART = "One more thing."

__all__ = [
    'ASK_WIFI_SCOPE',
    'ASK_WIFI_SCOPE_AGAIN',
    'ASK_WIFI_SCOPE_EARLY',
    'EARLY_SCOPE_ASKABLE',
    'FILLER_ASK_SCOPE',
    'FILLER_TIP_CLOSER',
    'FILLER_TIP_NEARBY',
    'FILLER_TIP_PLACEMENT',
    'FILLER_TIP_REJOIN',
    'FILLER_TIP_RESTART',
    'FILLER_TIP_TOGGLE',
    'SAY_SCOPE_NOTED_ALL_DEVICES',
    'SAY_SCOPE_UNSURE',
    'SAY_WIFI_DECLINED',
    'SAY_WIFI_EXHAUSTED',
    'SAY_WIFI_FIXED',
    'SAY_WIFI_TIP_ACKNOWLEDGED',
    'SAY_WIFI_TIP_CLOSER',
    'SAY_WIFI_TIP_NEARBY',
    'SAY_WIFI_TIP_REJOIN',
    'SAY_WIFI_TIP_RESTART',
    'SAY_WIFI_TIP_TOGGLE',
]


import flows
import scripts
from journeys.common.rungs import say_rung
from journeys.common.waiting import with_filler


def _scope_announce_slot():
  """The scoping question, asked while the diagnostics job runs."""
  # An ANNOUNCE rather than a rung, because only one TASK speaks per turn: a rung could
  # not be heard until the next one, and on a silent line that is an inactivity tick away.
  # The engine joins announce output into the SAME turn as the task message. An announce
  # also takes `condition` directly, which `then_say` cannot, and latches its own name
  # once it fires.
  #
  # `texts`, not `message`: texts are delivered VERBATIM while `message` is handed to the
  # model to reword, and this is approved copy. `preempt` is explicit because the engine
  # reads it as `.get("preempt", True)` while the DSL defaults it to False, so omitting
  # the key inverts the meaning.
  return flows.announce(
      "AskScopeEarly", [ASK_WIFI_SCOPE_EARLY],
      # `has_mac` is ContextGate's own output, so requiring it is what puts this on the
      # turn the gate answers rather than the turn it is dispatched.
      requires=["has_mac"],
      condition=EARLY_SCOPE_ASKABLE,
      preempt=True)


# An announce is a slot rather than a task. `repair` only: the walkthrough it feeds does
# not exist in `reboot`, and its condition reads slots that flow does not declare.
def scope_announce():
  """Ask how much of the house is affected, while the checks are still running."""
  return [
      _scope_announce_slot(),
  ]


# One cue map shared by both slots that can capture the scope answer, because the two
# must agree: a phrase that reads as ONE_DEVICE early and ALL_DEVICES later would make the
# answer depend on when the caller happened to say it.
#
# No phrase may plausibly mean both, or the deterministic path confidently picks the wrong
# branch — which is far worse than paying for a model round trip. "Only my pod, everything
# else is fine" is the plainest way there is to say ONE device and matches both values, so
# "everything" carries the lookahead; `cue_priority` would otherwise hand it to the
# earliest declared. `clarify.REPLY_CUES` carries the same carve-out.
WIFI_SCOPE_CUES = {
    "ALL_DEVICES": [r"\beverything\b(?!\s+else)", "all of them", "all devices",
                    "every device",
                    "the whole house", "nothing works", "none of them",
                    "all of it", "everywhere", "the whole place",
                    "the entire house", "all my devices", "everything in the house",
                    "nothing is working", "no devices"],
    "ONE_DEVICE": ["one device", "just one", "just my", "only my", "only one",
                   "a single", "my laptop", "my phone", "my tv",
                   "just the one", "only the", "this one", "my tablet",
                   "my computer", "my ipad", "one of them"],
}

# "I don't know" is an ANSWER, and this is the cue set that hears it as one.
#
# Cue-only and deterministic, for the reason `acknowledgements.py` gives about the other
# two acknowledgements: whether the caller is stuck is not the model's to decide, and a
# consoling line fired on a guess is worse than none. Nothing here is a hedge that could
# equally be an answer -- "maybe" is deliberately absent, because "maybe everything" is a
# scope answer and `WIFI_SCOPE_CUES` should have it.
#
# WORD-ANCHORED, like every other cue set in this file, and for the same reason: "no
# idea" unanchored is inside "no ideal", and the short ones are substrings of ordinary
# words.
SCOPE_UNSURE_CUES = {
    "UNSURE": [r"\bnot sure\b", r"\bnot really sure\b", r"\bunsure\b",
               r"\bno idea\b", r"\bno clue\b", r"\bnot certain\b",
               r"\bdon'?t know\b", r"\bdo not know\b", r"\bdunno\b",
               r"\bhard to say\b", r"\bcan'?t tell\b", r"\bcannot tell\b",
               r"\bcouldn'?t say\b", r"\bhaven'?t checked\b"],
}


# `wifi_walkthrough` and `wifi_scope` are asked by RUNGS, not by these slots, because the
# model will answer a slot's own ask on the caller's behalf. The slots exist to CONSUME
# the answer, which is why both are gated shut until the turn after the question is
# spoken.
def slots():
  """Walk the caller round their own house, tip by tip."""
  return [
      with_filler(flows.intent_slot(
          "wifi_walkthrough",
          {
              # Cues are a LATENCY feature as much as an accuracy one: a cue-matched fill
              # is deterministic and skips the model setter pass entirely. The obvious
              # phrasings are worth listing even though the model would get them right.
              #
              # EVERY CUE IS WORD-ANCHORED. These are unanchored regexes by default, and
              # the short ones are substrings of ordinary words: "no" is inside "now",
              # "know", "nothing" and "another", and "ok" is inside "broken". Unanchored,
              # "my router is broken" reads as ACCEPT and "i know" as DECLINE.
              #
              # RESOLVED is declared FIRST because `cue_priority="first"` takes the
              # earliest declared value when several match, and a caller saying it is
              # fixed is answering the question rather than accepting or refusing:
              # "it's fine now" matches ACCEPT's "fine" too, and "it's working now"
              # matched DECLINE's "no" inside "now" before the anchors went on.
              "RESOLVED": [r"\bit's working\b", r"\bits working\b", r"\bworking now\b",
                           r"\bfixed now\b", r"\bit's fine now\b", r"\bits fine now\b",
                           r"\bsorted itself\b"],
              "ACCEPT": [r"\byes\b", r"\byeah\b", r"\byep\b", r"\byup\b", r"\bok\b",
                         r"\bokay\b", r"\bsure\b", r"\bplease do\b", r"\byes please\b",
                         r"\bgo ahead\b", r"\blet's try\b", r"\blets try\b",
                         r"\bwhy not\b", r"\bsounds good\b", r"\bthat works\b",
                         r"\balright\b", r"\bdefinitely\b", r"\bof course\b",
                         r"\bi guess\b", r"\bfine\b", r"\blet's do it\b",
                         r"\blets do it\b"],
              # "not right now" is listed because the anchors REMOVE it: it used to match
              # only through "no" inside "not", which is the accident being fixed.
              "DECLINE": [r"\bno\b", r"\bnope\b", r"\bno thanks\b", r"\bnot now\b",
                          r"\bnot right now\b", r"\bdon't want\b", r"\bdont want\b",
                          r"\bcan't right now\b",
                          r"\bcant right now\b", r"\brather not\b", r"\bnot really\b",
                          r"\bi'd rather not\b", r"\bid rather not\b", r"\bskip it\b",
                          r"\bno thank you\b"],
          },
          # `ask` and `hint` are not optional decoration: without them the slot-filling
          # protocol never names this setter to the model, which is then handed a free
          # turn with nothing to do. The offer rung speaks the real question; this is the
          # shorter re-ask.
          ask="Would you like to try a few things with me?",
          condition=scripts.WIFI_ANSWERABLE,
          cue_priority="first",
          max_retries=2,
          # No apology, anywhere, error recovery included: the reprompt owns the miss and
          # puts the question again, without a tag that reads as impatience with a caller
          # who has simply not been heard.
          reprompts=["I didn't catch that. Would you like to try a few things with me?"],
          on_exhaust_fill="DECLINE",
      ), scripts.FILLER_WALKTHROUGH),
      # No `filler_say` here: every tip rung that can follow this answer carries its own,
      # dispatched in the SAME engine turn, so a slot filler buys no time to first audio
      # and only stacks a second acknowledgement.
      flows.intent_slot(
          "wifi_scope",
          WIFI_SCOPE_CUES,
          ask=ASK_WIFI_SCOPE,
          condition=scripts.WIFI_SCOPE_ASKABLE,
          cue_priority="first",
          max_retries=2,
          # It leads with the miss and then repeats the ask verbatim. A bare re-ask sounds
          # to a caller like the agent did not hear itself, let alone them.
          reprompts=["I didn't catch that. Is it everything in the house, or just the "
                     "one device?"],
          on_exhaust_fill="ALL_DEVICES",
      ),
      # The same answer, captured while the diagnostics job is still out. A SEPARATE slot
      # rather than opening `wifi_scope` early, because of the retry ladder: during the
      # wait, a chase talks over the reassurance the wait is covered by, and a force-fill
      # records a whole-house answer from a caller who simply said nothing. Passive and
      # cue-only, so it does exactly one thing: listen. If the cues miss, `AskWifiScope`
      # puts the question again after the verdict — a missed capture costs one turn, a
      # wrong capture costs the diagnosis.
      flows.passive_slot(
          "wifi_scope_early", setter="", kind="intent",
          option_cues=WIFI_SCOPE_CUES, cue_priority="first",
          # `requires`, NOT `condition`, and that is the difference between capturing the
          # answer and throwing it away: `_deactivate_conditional_slots` skips any slot
          # with no `condition` KEY, so a conditional slot here is deactivated the moment
          # a leg goes false and takes the caller's answer with it. `requires` gates ask
          # and capture the same way and never triggers retention.
          #
          # The window has to outlive the thing it was asked during, so it stays open
          # until the answer is CONSUMED rather than until the checks come back. One leg
          # is enough: a filled slot is not collected again, and the promotion refuses to
          # overwrite an answer `wifi_scope` already holds.
          requires=["AskScopeEarly"]),
      # "I don't know", heard as the answer it is. Same `requires` as the slot above and
      # for the same two reasons: it must not be deactivated mid-wait, and it must not
      # listen before the question has been put -- a caller hunting for their account
      # number says "I don't know" about a completely different thing.
      flows.passive_slot(
          "wifi_scope_unsure", setter="", kind="intent",
          option_cues=SCOPE_UNSURE_CUES, requires=["AskScopeEarly"]),
      # Cue-only. Nothing asks these; they listen. `wifi_fixed` ends the loop from any tip
      # turn, and `wifi_tried` is what makes a tip skippable.
      #
      # `setter=""` is load-bearing: only the engine's own cue match can fill these. Given
      # a model-callable setter the model marks the problem fixed on "no that didn't help".
      flows.passive_slot("wifi_fixed", setter="", kind="intent", option_cues={
          "yes": [r"\bthat worked\b", r"\bit'?s working\b", r"\bworking now\b",
                  r"\bthat did it\b", r"\bthat fixed\b", r"\ball good now\b"]}),
      flows.passive_slot("wifi_tried", setter="", kind="intent", option_cues={
          "rejoin": [r"\bforgot(ten)? the network\b", r"\brejoin", r"\breconnected\b"],
          "closer": [r"\bmoved closer\b", r"\bnext to the (router|gateway|box)\b"],
          "toggle": [r"\bairplane mode\b", r"\btoggled\b", r"\bwifi off and on\b"],
          "restart": [r"\brestarted (my|the) (phone|laptop|device|tv)\b",
                      r"\brebooted (my|the) (phone|laptop|device|tv)\b"],
          # The whole-house tips need their own "already did that" values, or the
          # validator rightly refuses a branch that can never match.
          "placement": [r"\bmoved the (gateway|router|modem|box)\b",
                        r"\b(gateway|router|modem) is (already )?(out in the open|on a shelf)\b",
                        r"\bit'?s not in a cabinet\b"],
          "nearby": [r"\b(sat|sitting|stood|right) next to the (gateway|router|modem)\b",
                     r"\btried it (right )?next to\b"]}),
  ]


# The one part of the walkthrough declared ABOVE the ladder, which is why it is its own
# fragment rather than another entry in `tasks()` below. It is not a verdict competing for
# the diagnostic turn and it consumes nothing: it latches its own flag and leaves the
# ladder open, so the verdict still speaks on the same turn, second. That is the shape
# `acknowledgements.py` describes, and this sits alongside those two rungs.
#
# An ordering expressed as declaration order rather than as a preempt, deliberately. A
# non-terminal `then_say` already preempts, so marking this one would cost the turn the
# question that follows it -- which here is the verdict itself.
def tip_ack():
  """Acknowledge an answer to a tip before the sweep's own result lands on top of it."""
  return [
      # `requires` for the reason every tip carries one: an input-free rung is held back
      # on a turn the caller has spoken, and this rung's whole job is to speak on exactly
      # such a turn. `wifi_scope` is filled by definition here, since a tip has already
      # been given and every tip requires it.
      say_rung("AckWifiTipAnswer", "verdict_ack_wifi_tip",
               scripts.WIFI_TIP_ANSWER_BEFORE_VERDICT,
               SAY_WIFI_TIP_ACKNOWLEDGED, latch="wifi_tip_ack",
               requires=["wifi_scope"]),
  ]


# The SAME shape, one question earlier in the walkthrough, and it is here rather than in
# `tip_ack` above only because the two are answers to different questions: this one owns
# the turn a caller answers the SCOPING question on when the checks report a fault on that
# same turn. Everything the comment above says about ordering applies unchanged --
# declaration order, no preempt, its own latch, the ladder left open so the verdict still
# speaks second.
#
# Mutually exclusive with `tip_ack` by construction: that rung requires
# `wifi_offered_early` FILLED and this one requires it unfilled, so a turn cannot collect
# both acknowledgements.
def scope_ack():
  """Acknowledge an answer to the scoping question before the verdict lands on top of it."""
  return [
      # `requires` for the reason the tip acknowledgement carries one: an input-free rung
      # is held back on a turn the caller has spoken, and speaking on exactly such a turn
      # is this rung's whole job. `accountNumber` rather than `wifi_scope`, matching the
      # other scope acknowledgements -- the promotion into `wifi_scope` lands a turn after
      # the answer, so requiring it here would hold the rung back for a turn and put it
      # behind the verdict again, which is the defect.
      say_rung("AckScopeBeforeVerdict", "verdict_scope_noted_same_turn",
               scripts.SCOPE_ANSWER_BEFORE_VERDICT,
               scripts.SAY_SCOPE_NOTED_WITH_VERDICT, latch="scope_noted_late",
               requires=["accountNumber"]),
  ]


# Declared AFTER the ladder so none of these can outrank a diagnostic verdict: they only
# exist on a call where every check already came back healthy. None carries
# `NOT_YET_ANSWERED`, because the verdict has been spoken by definition, so each rung
# latches its own flag instead and the next turn's rung gates on that flag.
def tasks():
  """Walk the caller round the house, one tip per turn, until something works."""
  return [
      # These own the turn the caller ANSWERS the early scoping question on, when there is
      # no verdict yet for any ladder rung to speak: hoisting a question into a wait
      # creates an answer turn, and an answer turn needs an owner or the model takes it.
      # Gated on `diagnostics_complete`, so on a fast sweep the verdict lands here instead.
      #
      # `requires` because these fire on a turn the caller has just spoken on BY
      # DEFINITION, and an executor with no inputs and no requires is held back on exactly
      # those turns so it cannot preempt the model's setter.
      #
      # Two rungs, one per scope answer: whole house first, one device as the fallback, so
      # the pair covers every value rather than naming two.
      say_rung("AckScopeEarlyAll", "verdict_scope_noted_all",
               scripts.SCOPE_NOTED_ALL_DEVICES,
               SAY_SCOPE_NOTED_ALL_DEVICES, latch="wifi_offered_early",
               requires=["accountNumber"]),
      say_rung("AckScopeEarly", "verdict_scope_noted",
               scripts.SCOPE_NOTED_ONE_DEVICE,
               # This rung IS the offer, so the latch has to be the one the answer gate
               # and the all-clear both read. Both rungs latch it, so whichever speaks
               # closes the other and the caller is offered once, in one of two wordings.
               scripts.SAY_SCOPE_NOTED, latch="wifi_offered_early",
               requires=["accountNumber"]),

      # The third answer, and the one that used to reach silence. Declared AFTER the two
      # above because they are the specific cases: an answer the cues resolve is an
      # answer, and only when there is no scope to note does this speak. The three are
      # mutually exclusive on `wifi_scope_early`, which `order_check` pins -- they are not
      # terminal, so an overlap would stack two acknowledgements in one breath.
      say_rung("AckScopeUnsure", "verdict_scope_unsure", scripts.SCOPE_UNSURE,
               SAY_SCOPE_UNSURE, latch="scope_unsure_ack",
               requires=["accountNumber"]),

      # The caller answers the scoping question AFTER the verdict has landed, which the
      # two rungs above cannot see: they are gated on the walkthrough not having been
      # offered yet, and by this point it has.
      say_rung("AckScopeAfterVerdict", "verdict_scope_noted_late",
               scripts.SCOPE_NOTED_AFTER_VERDICT,
               scripts.SAY_SCOPE_NOTED_AFTER_VERDICT, latch="scope_noted_late",
               requires=["accountNumber"]),

      # The ask, in two wordings, split on whether the caller has heard the question
      # before. That is the ORDINARY case rather than the exception -- the question is
      # hoisted into the sweep on nearly every call -- so the re-ask is declared first and
      # the two are kept apart by `AskScopeEarly`, the announce's own latch. Both latch
      # `wifi_scope_asked`, so whichever speaks closes the other and the caller is asked
      # once more, not twice.
      say_rung("AskWifiScopeAgain", "verdict_wifi_scope_again",
               {"all": [scripts.WIFI_ACCEPTED,
                        {"not": scripts.WIFI_RESOLVED},
                        {"slot": "wifi_scope", "filled": False},
                        {"slot": "wifi_scope_asked", "filled": False},
                        {"slot": "AskScopeEarly", "filled": True}]},
               ASK_WIFI_SCOPE_AGAIN, latch="wifi_scope_asked", filler=FILLER_ASK_SCOPE),

      # The FIRST time of asking, which is reached when the question was never hoisted:
      # the clarification gate was still open while the checks ran, so the announce's
      # window closed before its condition came true.
      say_rung("AskWifiScope", "verdict_wifi_scope",
               {"all": [scripts.WIFI_ACCEPTED,
                        {"not": scripts.WIFI_RESOLVED},
                        {"slot": "wifi_scope", "filled": False},
                        {"slot": "wifi_scope_asked", "filled": False},
                        {"slot": "AskScopeEarly", "filled": False}]},
               ASK_WIFI_SCOPE, latch="wifi_scope_asked", filler=FILLER_ASK_SCOPE),

      # Resolved, from either the offer turn or any tip turn. A warm close, no transfer.
      say_rung("WifiFixed", "verdict_wifi_fixed",
               {"all": [scripts.WALKTHROUGH_OFFERED,
                        scripts.WIFI_RESOLVED,
                        {"slot": "wifi_closed", "filled": False}]},
               SAY_WIFI_FIXED, latch="wifi_closed", ends=False, requires=["accountNumber"]),

      say_rung("WifiDeclined", "verdict_wifi_declined",
               {"all": [scripts.WIFI_DECLINED,
                        {"slot": "wifi_closed", "filled": False}]},
               SAY_WIFI_DECLINED, latch="wifi_closed", ends=True, requires=["accountNumber"]),

      # The tips. Order is the contract: rejoining costs nothing and fixes the commonest
      # fault, and moving closer is meaningless to a caller whose whole house is offline,
      # so it is scoped to the one-device answer.
      #
      # Every one declares `requires=["wifi_scope"]`, and that is about WHEN, not whether.
      # An input-free rung is held back on any turn the caller SPOKE while an askable slot
      # is unfilled, so without it the first tip cannot speak on the turn the walkthrough
      # is accepted and has to wait for a quiet one. Declaring the dependency is safe as
      # well as true: every tip is gated on WIFI_ACCEPTED, so none can fire before the
      # acceptance it would otherwise preempt has been captured.
      say_rung("WifiTipRejoin", "verdict_wifi_tip_rejoin", scripts.WIFI_TIP_REJOIN,
               SAY_WIFI_TIP_REJOIN, latch="wifi_tip_given", filler=FILLER_TIP_REJOIN,
               requires=["wifi_scope"]),
      say_rung("WifiTipCloser", "verdict_wifi_tip_closer", scripts.WIFI_TIP_CLOSER,
               SAY_WIFI_TIP_CLOSER, latch="wifi_tip_given", filler=FILLER_TIP_CLOSER,
               requires=["wifi_scope"]),
      say_rung("WifiTipToggle", "verdict_wifi_tip_toggle", scripts.WIFI_TIP_TOGGLE,
               SAY_WIFI_TIP_TOGGLE, latch="wifi_tip_given", filler=FILLER_TIP_TOGGLE,
               requires=["wifi_scope"]),
      # The whole-house pair, for the caller who said it is everything.
      say_rung("WifiTipPlacement", "verdict_wifi_tip_placement",
               scripts.WIFI_TIP_PLACEMENT, scripts.SAY_WIFI_TIP_PLACEMENT,
               latch="wifi_tip_given", filler=FILLER_TIP_PLACEMENT,
               requires=["wifi_scope"]),
      say_rung("WifiTipNearby", "verdict_wifi_tip_nearby", scripts.WIFI_TIP_NEARBY,
               SAY_WIFI_TIP_NEARBY, latch="wifi_tip_given", filler=FILLER_TIP_NEARBY,
               requires=["wifi_scope"]),
      say_rung("WifiTipRestart", "verdict_wifi_tip_restart", scripts.WIFI_TIP_RESTART,
               SAY_WIFI_TIP_RESTART, latch="wifi_tip_given", filler=FILLER_TIP_RESTART,
               requires=["wifi_scope"]),

      # Three turns spent and still broken. Hands off with what was tried.
      say_rung("WifiExhausted", "verdict_wifi_exhausted", scripts.WIFI_EXHAUSTED,
               SAY_WIFI_EXHAUSTED, latch="wifi_closed", ends=True, requires=["accountNumber"]),
  ]
