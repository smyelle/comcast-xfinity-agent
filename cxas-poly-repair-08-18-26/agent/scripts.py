"""The customer-facing scripts and the conditions that select them.

Every script here is verbatim from the source agent, so a fidelity diff against it is a
string comparison rather than a hunt through a DAG. The conditions are DECLARATIVE dicts
(see below), which the engine evaluates against the MERGED slot state (filled + pending +
deferred).

Rungs that hand off speak in two parts, because a rung says nothing on the turn its tool
fires and the caller would otherwise wait out the whole round trip in silence. `_LEAD` is
spoken as the tool is dispatched and `_REST` once it returns; the undecorated `_WHOLE`
name is the approved sentence entire, and `ladder_check` asserts the halves rejoin to
exactly it. A `_WHOLE` is written out rather than joined from its halves, because a
derived one would agree with any paraphrase and check only that the split mechanism
works. The rule is about the TOOL, not the sentence: split a rung whose tool is slow,
leave one whose tool is quick, and do not split a say-only rung at all — it has no round
trip to cover.
"""

SAY_ACCOUNT_BLOCK = (
    "I see an issue with your account status that's interrupting your internet "
    "service. Let me get you to someone who can help with your account."
)
# Spoken when the caller ASKS for a person during an outage: there is nothing a live agent
# can do, so the hand-off is declined rather than queued.
#
# The ONLY place the refusal is spoken. The advisory never raises it unasked, because a
# refusal nobody asked for is a dead end, so this line carries the whole job: say why in
# one short sentence, give the caller something they can actually do, and leave the door
# open rather than closing the call.
#
# It deliberately does NOT repeat the Status Center: SAY_AREA_OUTAGE has already named it
# on this call, and hearing the same sentence twice in two turns is what makes the agent
# sound like a recording.
SAY_OUTAGE_NO_AGENT = (
    "During an outage, a live agent can't get your service back any sooner. You can "
    "sign up for text alerts to hear the moment it's fixed. I'm here if anything else "
    "comes up."
)
# A hold rather than a refusal: the diagnostics are still out, so a hand-off made now
# arrives with nothing in it and the receiving human starts the conversation over.
#
# TWO RUNGS, because the same sentence twice is what makes someone ask a third time
# harder. Both promise the hand-off explicitly — this holds the caller, it does not
# decline them, and the escalate gate opens by itself the moment the checks land.
#
# Worded to be true BEFORE the sweep starts as well as during it: a caller can ask for a
# person on their very first breath, before the account number is even known.
SAY_HOLD_FOR_CHECKS = (
    "I can do that. I'd just like the check on your line to finish first, so whoever "
    "picks up already knows what's actually wrong. Give me a moment."
)
SAY_HOLD_FOR_CHECKS_AGAIN = (
    "Almost there. As soon as those results are back I'll get you over to someone, "
    "and they'll know what we're dealing with."
)
# Three lines and no more: the outage message, the status-alerts line, and the closing
# question. Nothing may lead them — `{outage_message}` already opens with an outage in your
# area and already says teams are working to restore it, so anything in front of it is the
# advisory saying the same thing twice in one breath.
#
# No `{customer_message}`. Measured on a live outage, the backend fills it with the
# live-agent refusal word for word, so including it refuses every outage caller a person
# they never asked for. That refusal speaks on the escalate rail the moment they DO ask
# (see SAY_OUTAGE_NO_AGENT). Leaving it out also holds the whole turn to four sentences,
# since `{outage_message}` alone is two.
#
# The status line is one sentence rather than two so the four-sentence budget holds, and it
# says what to do before where to do it.
SAY_AREA_OUTAGE = (
    "{outage_message}\nYou can sign up for text alerts and check the Xfinity Status "
    "Center online for updates.\nIs there anything else I can help with?"
)
SAY_MISSING_HARDWARE = (
    "I'm not seeing an Xfinity Gateway on your account, so I can't run any more "
    "checks. Let me connect you with someone who can help."
)
# One outcome, two sentences, and which one the caller gets tracks how the swap was
# discovered:
#   * convoy predicted it -> `before_agent` seeds convoy_status, the DAG rung matches, and
#     the DAG's wording ships (note the comma).
#   * the gateway specialist found it -> gateway_status never reaches the slot machine, no
#     DAG rung matches, and the prose ladder words it differently.
#
# Neither carries a term like "hardware fault" or "intermittently": they tell the caller
# nothing they can act on, and a word a listener has to decode costs them the sentence
# after it.
#
# The two differ ONLY in their last sentence, so the discovery paths stay as
# distinguishable as the note above requires.
SAY_HARDWARE_SWAP_GATEWAY = (
    "Your gateway is failing on and off, and a restart won't fix it. It needs "
    "replacing. You can swap it at a local store or request a replacement on the "
    "Xfinity website."
)
SAY_HARDWARE_SWAP_CONVOY = (
    "Your gateway is failing on and off, and a restart won't fix it. It needs "
    "replacing. You can swap at a local store, or request a replacement on the "
    "Xfinity website."
)
SAY_NETWORK_TECH = (
    "It looks like there's a problem with the network signal going to your home. "
    "We'll need to send a technician out to fix it. You don't need to be home for "
    "this. You only need to be there if the technician has to get onto your "
    "property, for example through a locked gate."
)
SAY_NETWORK_GENERIC = (
    "We found an issue with the connection to your home. A technician will take a "
    "closer look. Depending on what they find, there may be a service charge."
)
# The approved sentence, split across the two sides of the reboot call: the caller hears
# the same words in the same order, and only the Convoy round trip moves.
#
# Keeping the duration on the far side is what gives the rung a `then_say`. A rung whose
# only copy is `filler_say` leaves the task without one, and a rung with no `then_say`
# goes MUTE on every following turn — so "How long until it's back?" asked straight after
# the reboot would return no agent text at all.
SAY_REBOOT_STARTED = "Alright, I'm sending a signal to reboot your gateway now."
SAY_REBOOT_DURATION = "This usually takes about 5 to 7 minutes to complete."
# The same sentence unsplit, as it was signed off — a checksum of the approved copy, so
# editing either half alone fails the oracle.
SAY_REBOOT_WHOLE = (
    "Alright, I'm sending a signal to reboot your gateway now. This usually takes "
    "about 5 to 7 minutes to complete."
)
# What `ladder_check` pins the two halves of the held reboot to.
SAY_REBOOT_HOLD_WHOLE = (
    "Okay, give me just a moment. Alright, I'm sending a signal to reboot your gateway "
    "now. This usually takes about 5 to 7 minutes to complete."
)
SAY_REBOOT_DECLINED = (
    "I understand. Let me connect you to a gateway specialist to help troubleshoot "
    "further."
)
# The account is known and the sweep has not run. Not gated on `SWEPT` — being the
# opposite of swept is the whole point.
#
# `caller_spoke` covers the latency of a sweep that is ABOUT TO RUN, and the hook only
# sweeps once the caller has said what is wrong, so without it the rung fires on an
# opening turn where no sweep is happening at all.
#
# `async_sweep_armed` unfilled keeps this rung to the SYNCHRONOUS path. On the async path
# `awaits.say` is this same sentence, and a fire-eligible task makes the engine return
# before the `all_done` branch, which is the ONLY place `while_waiting` is consulted — so
# an armed rung repeats verbatim on every waiting turn and the `while_waiting` ladder is
# never reached.
BRIDGE_TO_SWEEP = {"all": [{"slot": "accountNumber", "filled": True},
                           {"slot": "caller_spoke", "filled": True},
                           {"slot": "async_sweep_armed", "filled": False},
                           {"slot": "diagnostics_complete", "filled": False},
                           {"slot": "sweep_bridged", "filled": False}]}

# The first question of essentially every call, so it gets the most care of anything here.
#
# Two short sentences rather than one long one: the question stands alone and the second
# input arrives as a reassurance, not as a second thing to hold in your head. It is still
# ONE question, which is what the caller has to answer.
#
# "Xfinity" is deliberately absent. The greeting has already said it one turn earlier, and
# repeating a brand name a caller just heard buys nothing and lengthens the ask. Nothing of
# the office register belongs here either: this is how someone asks the question out loud.
# `{welcome_lead}` is filled by `before_agent`, never left to the caller, and is why this
# is a `verbatim` slot: the model used to open the account ask with an improvised "Welcome
# to Xfinity" however firmly the instruction said not to, and that greeting is the one thing
# the hand-off flag exists to drop. Reading the ask ahead of the model makes the greeting
# deterministic; the lead-in decides whether it is there. The two forms are `WELCOME_LEAD`
# and `WELCOME_LEAD_HANDOFF` below (kept byte-identical to the hook's inline copy by
# greeting_check). The lead-in is ALWAYS one of them and never empty -- a falsy slot reads
# as unfilled and the engine re-asks it forever.
ASK_ACCOUNT_NUMBER = (
    "{welcome_lead}what's your account number? The phone number on the account "
    "works too."
)

# The account ask's lead-in, and the whole of the greeting difference. On a DIRECT call's
# opening turn the caller has heard nothing yet, so the lead-in carries the greeting;
# `WELCOME_LEAD` folded ahead of `ASK_ACCOUNT_NUMBER` is the "Welcome to Xfinity. To get
# started, what's your account number?..." a direct caller hears in one breath.
WELCOME_LEAD = "Welcome to Xfinity. To get started, "
# When a steering agent HANDS THIS CALL OVER it seeds `skip_greeting`; the caller was
# welcomed one agent ago, so the lead-in is the bare "To get started," -- no second hello.
# The same lead-in `reboot` gets, since its account ask only ever happens mid-call. NOT a
# `SAY_`/`ASK_` constant on purpose: these are structural lead-ins, not lines the copy gate
# tracks, and greeting_check pins the rendered opener instead.
WELCOME_LEAD_HANDOFF = "To get started, "

# The in-home WiFi walkthrough, reached when every check comes back healthy.
#
# The healthy result, the framing and the offer are ONE turn, with no separate "are you
# still having trouble?" first — that question has nowhere to go, because nothing consumes
# the answer. The framing is deliberately "the most likely spot left" and never "the
# cause": every check we can run came back clean, which is evidence about our plant, not
# about the caller's living room. It hedges ONCE, because a second hedge stacked on "most
# likely" makes a confident finding sound like a guess.
#
# THREE things read back, not four, and the last one is marked. A spoken list runs out of
# room at three, so the read-back keeps the items a caller can picture and leaves out the
# line into their home, which is the one they have no word for. Nothing is lost from the
# finding itself, only from the recital.
SAY_ALL_CLEAR = (
    "Everything on our side looks healthy. Your account, your area, and finally your "
    "gateway all check out. So the most likely spot left is the WiFi inside your home. "
    "Would you like me to walk you through a few things to try?"
)
# The all-clear for a caller who is ALREADY trying things. Same finding, no second offer:
# they accepted one while the checks ran.
#
# The first two sentences are SAY_ALL_CLEAR's, deliberately, and the same three-item
# read-back rule applies to both — see the note above. Only the tail differs, because on
# this path the walkthrough is already running.
SAY_ALL_CLEAR_ALREADY_TRYING = (
    "Everything on our side looks healthy. Your account, your area, and finally your "
    "gateway all check out. So the most likely spot left is the WiFi inside your home, "
    "which is what we're already looking at."
)
# The acknowledgement AND the offer, in one turn, spoken when the caller answers the
# scoping question before the checks are back.
#
# The turn belongs to nobody otherwise, and this model does not leave a turn empty: it
# fills the gap with a confident in-home diagnosis the checks have not earned. Hoisting a
# question into the wait creates an answer turn, and an answer turn needs an owner.
#
# Acknowledging and offering separately does not work: the offer would be gated on the
# promoted `wifi_scope`, which lands a turn after the answer, so it needs the job to
# outlive both the answer and the promotion. Folding the two together removes the race,
# because the caller's answer always creates this turn.
#
# TWO LINES, one per scope answer, for the same reason the tips are two sets — see
# SAY_WIFI_TIP_PLACEMENT. This is the ONE-DEVICE one and it is also the fallback, so an
# answer neither line recognises is still acknowledged rather than left to the model.
#
# The opening words are pinned by a live probe, so "Got it, that helps" stays exactly as
# it is. No idiom in the closing question: an idiom costs a listener a translation step,
# and the answer here has to be immediate.
SAY_SCOPE_NOTED = (
    "Got it, that helps. While those checks finish, we could try a couple of quick "
    "things on that device. Want to try them now?"
)
# Whole-house tips. The device-specific ones are wrong for this caller: telling someone
# whose laptop, TV and console are ALL struggling to "forget the home network on the
# device that's struggling" asks them to do it on every device they own. When everything
# is affected the useful checks are about the gateway and the environment.
#
# ONE question, and it is the one that matters. Asking where the gateway sits AND whether
# moving it helped puts two questions in one turn, and the caller can only answer one. The
# placement question stays out rather than taking a turn of its own: someone whose gateway
# is already in the open will say so, which costs a turn only when it happens.
SAY_WIFI_TIP_PLACEMENT = (
    "Since it's everything, let's look at the gateway itself. Try moving it somewhere "
    "clear and upright, out of a cabinet and up off the floor. Did that change anything?"
)

# Conditions — DECLARATIVE dicts, not lambda strings. The engine accepts either, but a
# dict is parsed rather than eval'd, so every slot it names is visible to the blessed
# validator, which errors on a reference to a slot the flow never declared. A lambda
# string is opaque to it, so a typo there is a silent no-match at runtime.
#
# Grammar: all / any / not for composition; per-leaf eq / neq / in / not_in / filled
# (plus gte/lte/gt/lt for numbers, and `upper` to normalize a string before comparing).

RESTRICTED_ACCOUNT = {"slot": "account_status",
                      "in": ["suspended", "disconnected", "pending activation",
                             "pending_activation"]}

# The number was the right shape and matched no account. Its own condition rather than a
# leg of RESTRICTED_ACCOUNT: those three are accounts we CAN see and will not diagnose,
# this is one we cannot see at all, and the two want different words and a different desk.
ACCOUNT_NOT_FOUND = {"slot": "account_status", "eq": "not_found"}

AREA_OUTAGE = {"slot": "outage_status", "in": ["active", "degradation"]}

# The swap outcome is spoken two ways depending on how it was found, so the two discovery
# paths are separate rungs (see SAY_HARDWARE_SWAP_* above).
HARDWARE_SWAP_CONVOY = {"slot": "convoy_status", "eq": "predictive_swap"}
HARDWARE_SWAP_GATEWAY = {"slot": "gateway_status", "eq": "swap"}

# `reboot`/`predictive_offline` arm the offer; the offer is made once.
_REBOOT_BASE = {"any": [{"slot": "gateway_status", "eq": "reboot"},
                        {"slot": "convoy_status", "eq": "predictive_offline"}]}
REBOOT_OFFER = {"all": [_REBOOT_BASE, {"slot": "reboot_offered", "filled": False}]}

# The setter records the caller's SPOKEN answer, so the comparison is on normalized text —
# `upper` folds case, and the booleans cover a programmatic fill. Testing `is True` here
# would match only a real boolean and silently die on a spoken "no".
_YES = [True, "TRUE", "YES", "Y", "YEAH", "YEP", "YUP", "SURE", "OK", "OKAY", "PLEASE"]
_NO = [False, "FALSE", "NO", "N", "NOPE", "NAH", "NO THANKS", "SKIP"]
# An answer only counts once the question was asked AND the caller had a turn to answer.
_REBOOT_ANSWERABLE = [_REBOOT_BASE,
                      {"slot": "reboot_offered", "filled": True},
                      {"slot": "reboot_answer_allowed", "eq": "true"}]
REBOOT_CONFIRMED = {"all": _REBOOT_ANSWERABLE
                    + [{"slot": "confirm_reboot", "upper": True, "in": _YES}]}
REBOOT_DECLINED = {"all": _REBOOT_ANSWERABLE
                   + [{"slot": "confirm_reboot", "upper": True, "in": _NO}]}

# The caller asked for a person. Spoken by the flow's OWN `escalate` control block when the
# engine's detector recognises the request, and by the `Escalate` rung when the router got
# there on vocabulary the detector does not carry ("supervisor", "manager") — one line
# whichever door they came in by.
#
# Short on purpose, and one sentence: the caller has asked for a person, and every word
# before the hand-off is a word between them and it.
#
# It must not open on "Of course.", which 4.6 suggests, because that is exactly a member of
# FILLER_FULLCHECK: a caller who accepted the full check and then asked for a person would
# hear the same two words twice, and the journey-walk coverage oracle counts the filler as
# spoken whenever this line is. The verb does the acknowledging instead.
SAY_HUMAN_ESCALATE = "Let me get you to someone who can help with that."

# The all-clear when the offer was already made while the checks ran: same statuses, its
# own latch, and no second offer (see SAY_ALL_CLEAR_ALREADY_TRYING).
#
# Last rung, so it must be exclusive of every earlier one — including account and convoy.
# A `predictive_offline` convoy never writes gateway_status, so an all-clear testing only
# outage/network/gateway/wifi would swallow a caller owed a reboot offer.
ALL_CLEAR_ALREADY_TRYING = {"all": [
    {"slot": "account_status", "in": ["clear", "skipped"]},
    {"slot": "outage_status", "in": ["none", "skipped"]},
    {"slot": "convoy_status", "in": ["clear", "none", "skipped"]},
    {"slot": "network_status", "in": ["healthy", "skipped"]},
    {"slot": "gateway_status", "in": ["healthy", "skipped"]},
    {"slot": "wifi_status", "in": ["healthy", "skipped"]},
    {"slot": "wifi_offered_early", "filled": True},
    {"slot": "all_clear_told", "filled": False},
]}

# The six statuses, all healthy or skipped. Factored out because three things ask the same
# question: the two all-clear rungs, and the walkthrough itself.
_ALL_CLEAR_STATUSES = [
    {"slot": "account_status", "in": ["clear", "skipped"]},
    {"slot": "outage_status", "in": ["none", "skipped"]},
    {"slot": "convoy_status", "in": ["clear", "none", "skipped"]},
    {"slot": "network_status", "in": ["healthy", "skipped"]},
    {"slot": "gateway_status", "in": ["healthy", "skipped"]},
    {"slot": "wifi_status", "in": ["healthy", "skipped"]},
]

# What every walkthrough turn must satisfy: the checks have NOT reported yet, or they came
# back clean. This is the guard that makes in-home advice safe once the walkthrough can
# start during the sweep — the early offer latches on the scope answer alone, which says
# nothing about the line, so without this a caller whose whole street is down is told to
# forget their Wi-Fi network and rejoin.
#
# Written as "unreported OR clean" rather than "clean" so the tips can still run during the
# wait, which is the whole point of starting early. The moment a real fault lands, every
# rung below goes dark and the verdict owns the turn.
WALKTHROUGH_SAFE = {"any": [{"slot": "diagnostics_complete", "filled": False},
                            {"all": list(_ALL_CLEAR_STATUSES)}]}

ALL_CLEAR = {"all": _ALL_CLEAR_STATUSES + [
    # ...and NOT when the offer was already made during the sweep. Without this leg the two
    # all-clear rungs are not mutually exclusive, both match, and the caller hears the
    # finding twice in one breath — the second time with an offer they already accepted.
    {"slot": "wifi_offered_early", "filled": False},
    # Speaks once. The all-clear latches `wifi_offered` rather than `verdict_delivered`,
    # because it is an offer and the ladder has to stay open for the answer.
    {"slot": "wifi_offered", "filled": False},
    # These two legs are deliberately NOT `WALKTHROUGH_NOT_OFFERED`. This is the one place
    # entitled to tell the two latches apart, because telling them apart is its whole job:
    # this rung and ALL_CLEAR_ALREADY_TRYING are the two wordings of the same finding, and
    # which one speaks depends on whether the EARLY offer was the one that fired.
]}

# Wi-Fi walkthrough state, mirroring the reboot handshake for the same reason: the offer is
# SPOKEN by a rung so the caller is guaranteed to hear it, and the answer slot stays shut
# until the turn after, so the model cannot answer the question on the caller's behalf.

# ONE name for "the walkthrough has been offered, by whichever of the two rungs got there
# first", and the rule it encodes is that no condition anywhere may name one latch on its
# own. Which rung made the offer is bookkeeping for the all-clear, so it can pick the
# wording that does not offer twice; the rungs downstream are not entitled to care, and one
# that does leaves the caller with no words at all on one of the two routes.
WALKTHROUGH_OFFERED = {"any": [{"slot": "wifi_offered", "filled": True},
                               {"slot": "wifi_offered_early", "filled": True}]}
# The negative. The rungs that use it are asking "is the walkthrough still un-offered", and
# asking it of one latch leaves them eligible right through a walkthrough that is already
# running on the early path.
WALKTHROUGH_NOT_OFFERED = {"all": [{"slot": "wifi_offered", "filled": False},
                                   {"slot": "wifi_offered_early", "filled": False}]}

WIFI_ANSWERABLE = {"all": [WALKTHROUGH_OFFERED,
                           {"slot": "wifi_answer_allowed", "eq": "true"}]}

# The model-invoked half of each question. Cues are engine-side and exact; a classifier
# maps everything else onto the same enum. Without one the setter receives raw speech and
# the enum rejects it, which surfaces to the caller as a failed turn.
WIFI_WALKTHROUGH_CLASSIFIER = {
    "ACCEPT": ["yes", "yeah", "sure", "ok", "please", "go ahead", "let's try",
               "worth a go", "why not", "alright"],
    "DECLINE": ["no", "not now", "rather not", "no thanks", "don't have time",
                "another time"],
    "RESOLVED": ["it's working", "working now", "fixed itself", "sorted now",
                 "it came back", "all good now"],
}
WIFI_SCOPE_CLASSIFIER = {
    "ONE_DEVICE": ["one device", "just one", "just my", "only my", "only one",
                   "a single device", "my laptop", "my phone", "my tv", "this one"],
    "ALL_DEVICES": ["everything", "all of them", "all devices", "the whole house",
                    "nothing works", "none of them", "every device"],
}

# An answer only counts once the question was ASKED and the caller has had a turn to
# answer it -- the two-latch gate, on the RUNG, exactly as `_REBOOT_ANSWERABLE` puts it
# on the reboot's answer rungs.
#
# The slot gate (`WIFI_ANSWERABLE`, above) is weaker than it looks: it decides when
# `wifi_walkthrough` may be FILLED and says nothing about a value that arrives another way
# -- carried in from an earlier flow, seeded, or classified out of an utterance that merely
# contained "ok". Any of those makes `AskWifiScope` eligible on the OFFER's own turn, and
# the rungs are not terminal, so the engine walks straight on and asks the follow-up
# question in the same breath as the offer the caller has not answered yet.
_WIFI_ANSWERED = [WALKTHROUGH_OFFERED,
                  {"slot": "wifi_answer_allowed", "eq": "true"}]
WIFI_ACCEPTED = {"all": _WIFI_ANSWERED + [{"slot": "wifi_walkthrough", "eq": "ACCEPT"},
                                          WALKTHROUGH_SAFE]}

# The early offer, made once the caller has told us the scope while the job is still out.
# Gated on `wifi_scope` rather than on the sweep alone, so it only happens in a
# conversation that is already about their Wi-Fi; offering cold would be the agent
# volunteering a diagnosis.
#
# UNREFERENCED: the early offer is `_scope_noted` below, and no rung reads this.
OFFER_WHILE_CHECKING = {"all": [
    {"slot": "diagnostics_complete", "filled": False},
    {"slot": "network_status", "filled": False},
    {"slot": "wifi_scope", "filled": True},
    WALKTHROUGH_NOT_OFFERED,
    {"slot": "device_searched", "filled": False},
]}
WIFI_RESOLVED = {"any": [{"slot": "wifi_walkthrough", "eq": "RESOLVED"},
                         {"slot": "wifi_fixed", "filled": True}]}


# When the acknowledgement-and-offer may speak, per scope answer. A factory rather than two
# hand-copied five-leg literals, so the legs that decide WHETHER the offer is safe cannot
# drift apart between the two lines it can be made in.
def _scope_noted(scope_leg):
  return {"all": [{"slot": "AskScopeEarly", "filled": True},
                  {"slot": "wifi_scope_early", "filled": True},
                  {"slot": "diagnostics_complete", "filled": False},
                  {"slot": "wifi_offered_early", "filled": False},
                  WALKTHROUGH_SAFE,
                  scope_leg]}


# Whole house is the SPECIFIC case and one device is the fallback (`neq`), so between them
# the two cover every value the slot can hold. A pair of `eq` legs would leave the answer
# turn unowned for any value the cue map grows later, which is the gap the model fills with
# an in-home diagnosis the checks have not earned.
SCOPE_NOTED_ALL_DEVICES = _scope_noted(
    {"slot": "wifi_scope_early", "eq": "ALL_DEVICES"})
SCOPE_NOTED_ONE_DEVICE = _scope_noted(
    {"slot": "wifi_scope_early", "neq": "ALL_DEVICES"})

# No answer at all, from a caller who plainly gave one. `wifi_scope_unsure` is the cue
# slot that hears "I don't know" as the reply it is, and this is what lets a rung own the
# turn it arrives on -- which nothing did, so the caller was met with silence.
#
# `wifi_scope_early` UNFILLED is the leg that keeps this to the caller who really did not
# know. The rungs are not terminal, so on "not sure, maybe the whole house" the engine
# would otherwise walk straight through the whole-house acknowledgement and this one, and
# the caller would hear both in one breath.
#
# `diagnostics_complete` UNFILLED for the same reason `_scope_noted` carries it: once the
# checks are back, the verdict owns the turn and "let's see what those checks say" is a
# sentence about something that has already happened.
#
# `scope_unsure_ack` is the once-per-call latch, and it is a guard rather than a load. The
# cue slot stays filled for the rest of the call, so nothing else in the condition would
# stop this speaking again; that it does not is only because the reassurance ladder owns
# the remaining waiting turns, which is a fact about those turns and not about this rung.
SCOPE_UNSURE = {"all": [
    {"slot": "AskScopeEarly", "filled": True},
    {"slot": "wifi_scope_unsure", "filled": True},
    {"slot": "wifi_scope_early", "filled": False},
    {"slot": "diagnostics_complete", "filled": False},
    {"slot": "scope_unsure_ack", "filled": False},
]}

# The same answer, arriving AFTER a fault verdict has already been spoken.
#
# `_scope_noted` above cannot cover it: it requires `diagnostics_complete` UNFILLED so the
# acknowledgement steps aside on a fast sweep and lets the verdict own the turn. When the
# verdict landed a turn EARLIER, the caller's answer arrives with no rung left anywhere --
# `verdict_delivered` has shut the ladder and `WALKTHROUGH_SAFE` has disarmed every tip,
# correctly, because in-home advice is wrong on a measured plant fault -- so the turn falls
# to the model.
#
# It acknowledges and CLOSES: no tip, no offer, no second question, because the walkthrough
# is not on the table on this path. ONE line covers both scope answers, since nothing
# follows that has to be worded by the answer.
#
# What it may NOT say is anything about where the fault is. `WALKTHROUGH_SAFE` is false on
# an area outage, a plant impairment, a gateway that needs swapping and an account we
# could not check, and "the fault is on the line coming into your home" is true for only
# two of those. It points at the verdict already spoken instead, which is true on all.
SAY_SCOPE_NOTED_AFTER_VERDICT = (
    "Thanks, that's useful to know and I've made a note of it. It doesn't change what "
    "the checks found, so the next step is still the one I just described."
)

# When it may speak: the early question was asked and answered, a verdict has landed, and
# this is NOT an all-clear -- an all-clear keeps the walkthrough, which has rungs of its
# own for every turn on that path.
#
# `wifi_offered_early` UNFILLED is the leg that stops the caller being acknowledged twice.
# The rung is for the calls where the early acknowledgement never got to speak, which is
# exactly the calls where nobody owns the answer; where it did speak, the verdict alone is
# the complete reply.
SCOPE_NOTED_AFTER_VERDICT = {"all": [
    {"slot": "AskScopeEarly", "filled": True},
    {"slot": "wifi_scope_early", "filled": True},
    {"slot": "wifi_offered_early", "filled": False},
    {"slot": "verdict_delivered", "filled": True},
    {"not": WALKTHROUGH_SAFE},
    {"slot": "scope_noted_late", "filled": False},
]}

# The SAME answer and the SAME fault, arriving on ONE turn, which is the ordinary shape
# rather than the exception: the question is hoisted into the wait, so the answer turn and
# the turn the specialists report on are frequently the same one.
#
# Measured live over voice, cold. Asked whether it was everything or one device the caller
# said "Honestly, I think it's everything", and heard "Your gateway is failing on and off,
# and a restart won't fix it..." first, with the acknowledgement of their answer trailing
# behind it -- and trailing in words written for a turn that had already happened, so it
# pointed back at "the next step I just described" one breath after describing it.
#
# The line below is what LEADS that turn instead, with the verdict speaking second. ONE
# short sentence, for the reason SAY_WIFI_TIP_ACKNOWLEDGED gives: the fault verdicts are
# three and four sentences, and a fuller acknowledgement in front of one pushes the turn
# past what a listener can hold.
#
# It promises nothing and asserts nothing, so the same words are true ahead of every
# verdict this can collide with -- an outage, a swap, a dispatch, an account we could not
# check. Nothing about WHERE the fault is, for the reason spelled out above: that is the
# verdict's to say, and it is about to.
SAY_SCOPE_NOTED_WITH_VERDICT = (
    "Thanks for that, it's useful to know."
)

# When it may speak, and every leg is about confining it to that one turn. An
# acknowledgement that is not confined thanks the caller through the whole call, which is
# a worse agent than one that answers slightly out of order.
#
# `verdict_delivered` UNFILLED is the leg that means SAME TURN. It is the exact
# complement of `SCOPE_NOTED_AFTER_VERDICT`'s, so between them the two rungs cover the one
# population -- a caller whose early answer nobody has acknowledged -- split by whether
# the result has already been spoken. Both latch `scope_noted_late`, so whichever gets
# there closes the other and the answer is acknowledged exactly once.
#
# `diagnostics_complete` FILLED is what makes this the collision rather than the ordinary
# mid-sweep answer: with the checks still out, `_scope_noted` owns the turn and offers the
# walkthrough, which is a better reply than this one.
#
# `{"not": WALKTHROUGH_SAFE}` keeps it to a FAULT, and it is the same carve-out its
# after-verdict sibling carries. On an all-clear the walkthrough survives the result and
# has rungs of its own for this turn, so an acknowledgement stepping in front of them
# would lead an offer nobody has heard yet with a thank-you.
SCOPE_ANSWER_BEFORE_VERDICT = {"all": [
    {"slot": "AskScopeEarly", "filled": True},
    {"slot": "wifi_scope_early", "filled": True},
    {"slot": "wifi_offered_early", "filled": False},
    {"slot": "diagnostics_complete", "filled": True},
    {"slot": "verdict_delivered", "filled": False},
    {"not": WALKTHROUGH_SAFE},
    {"slot": "scope_noted_late", "filled": False},
]}

# The scoping question gets the same two-part gate as the offer, and for the same
# reason: asked and answered on one turn is the model answering for the caller.
#
# It also CLOSES once the caller says it is working. An askable slot left open suppresses
# the verdict rungs — an input-free task does not preempt the model on a turn the caller
# has spoken, so any unanswered ask outranks the ladder. This is the slot still unanswered
# when the caller reports the fault gone, so without the third leg the warm close never
# speaks and they are asked "is it everything, or just one device?" about a problem they
# have just told us is over.
WIFI_SCOPE_ASKABLE = {"all": [{"slot": "wifi_scope_asked", "filled": True},
                              {"slot": "wifi_scope_allowed", "eq": "true"},
                              {"not": WIFI_RESOLVED}]}
WIFI_DECLINED = {"all": _WIFI_ANSWERED + [{"slot": "wifi_walkthrough", "eq": "DECLINE"}]}

# A tip may be spoken when the caller accepted, has not said it is fixed, we have not
# already spent three turns on tips, and they have not told us they tried this one.
# `wifi_tips_exhausted` is derived in the hook by counting the latches — the engine has
# no counter, and a fourth rung gated on "three of these are set" is not expressible.
def _wifi_tip(latch, tag, scope=None):
  legs = [WIFI_ACCEPTED,
          # Unreported or clean. See WALKTHROUGH_SAFE: without this a tip is reachable on
          # a call whose checks came back with an outage.
          WALKTHROUGH_SAFE,
          {"not": WIFI_RESOLVED},
          # The scope must be ANSWERED, not merely asked. The engine keeps walking tasks
          # within a turn, so gating on the question having been spoken fires the first tip
          # in the same breath as the question, before the caller can reply.
          {"slot": "wifi_scope", "filled": True},
          # Shared, and cleared by the hook at the start of every turn: this is what stops
          # a second tip firing in the same breath as the first. Only an OUTPUT slot blocks
          # a later task within a turn — a state write lands too late to be seen.
          {"slot": "wifi_tip_given", "filled": False},
          # Not on a turn where the caller asked about money: a tip and the fee schedule in
          # one breath are two unrelated things, neither of which gets a clear answer.
          {"slot": "cost_answered", "filled": False},
          {"slot": "wifi_tips_exhausted", "filled": False},
          {"slot": latch, "filled": False},
          {"not": {"slot": "wifi_tried", "in": [tag]}}]
  if scope:
    legs.append({"slot": "wifi_scope", "in": scope})
  return {"all": legs}


# Order is the contract here too. Rejoining is first because it costs nothing and fixes
# the commonest fault; moving closer is meaningless for a caller whose whole house is
# offline, hence the one-device scope.
WIFI_TIP_REJOIN = _wifi_tip("wifi_tip_rejoin", "rejoin", scope=["ONE_DEVICE"])
WIFI_TIP_CLOSER = _wifi_tip("wifi_tip_closer", "closer", scope=["ONE_DEVICE"])
WIFI_TIP_TOGGLE = _wifi_tip("wifi_tip_toggle", "toggle", scope=["ONE_DEVICE"])
# The whole-house pair. Same machinery, same per-turn latch, different advice: the caller
# told us it was everything, so the agent stops talking about "the device that's
# struggling".
WIFI_TIP_PLACEMENT = _wifi_tip("wifi_tip_placement", "placement",
                               scope=["ALL_DEVICES"])
WIFI_TIP_NEARBY = _wifi_tip("wifi_tip_nearby", "nearby", scope=["ALL_DEVICES"])
# Restarting the device is worth a go either way.
WIFI_TIP_RESTART = _wifi_tip("wifi_tip_restart", "restart")

# The caller answers a tip and the sweep reports on the SAME turn, which is the ordinary
# case rather than a rare one: the walkthrough starts during the wait, so every early call
# has a settle landing somewhere inside it. Two things are then owed on one turn, and
# without a rung for the first the verdict takes the turn on its own and the caller's
# answer is never mentioned.
#
# `wifi_offered_early` is what confines this to that path, and it is load-bearing rather
# than descriptive. On the ORDINARY path the sweep settled before the offer, so
# `diagnostics_complete` is filled on every tip turn and the remaining legs hold on all of
# them -- this rung would acknowledge every tip answer for the rest of the call, stacking
# a thank-you in front of each tip that follows.
#
# `wifi_tip_spent` is what makes this an answer to a TIP rather than to anything else the
# walkthrough asks: derived in the hook by counting the same latches the cap counts, and
# true only from the turn after a tip was actually given. Without it the caller who says
# "yes please" to the offer is thanked for trying something nobody has suggested yet.
#
# `wifi_tip_given` unfilled is the leg that means "the caller has REPLIED". The hook
# releases that latch on a real caller turn only, so on an inactivity tick the outstanding
# tip still holds it and nobody is thanked for an answer they have not given -- the same
# guard, for the same reason, as `WIFI_EXHAUSTED` below.
#
# The three "nothing has spoken yet" legs are what keep it to one turn: `all_clear_told`
# and `verdict_delivered` are the latches of every rung that can report the sweep, so once
# one of them has, the result is old news and the acknowledgement has nothing to lead.
WIFI_TIP_ANSWER_BEFORE_VERDICT = {"all": [
    {"slot": "wifi_offered_early", "filled": True},
    {"slot": "wifi_tip_spent", "eq": "true"},
    {"slot": "wifi_tip_given", "filled": False},
    {"slot": "diagnostics_complete", "filled": True},
    {"slot": "all_clear_told", "filled": False},
    {"slot": "verdict_delivered", "filled": False},
    # A caller reporting success is answered by `WifiFixed`, in warmer words. Thanking
    # them for trying first would put a flat line in front of it.
    {"not": WIFI_RESOLVED},
    {"slot": "wifi_tip_ack", "filled": False},
]}

# `wifi_tip_given` unfilled is what makes this wait for the caller: the hook clears that
# latch on a caller TURN rather than on every turn, so while the last tip is outstanding
# this cannot fire. Without it the hand-off rides an inactivity tick and ends the call on
# someone who is off restarting the device they were just asked to restart — and the tips
# are the one place in the script where the answer legitimately takes minutes.
WIFI_EXHAUSTED = {"all": [WIFI_ACCEPTED,
                          {"not": WIFI_RESOLVED},
                          {"slot": "wifi_tip_given", "filled": False},
                          {"slot": "wifi_tips_exhausted", "eq": "true"}]}


FILLER_WALKTHROUGH = ["Sure thing.", "Absolutely.", "Happy to."]
# The routing turn is the slowest of the whole call, because routing spends several
# serialized round trips where an ordinary turn spends one. These lines are long enough to
# cover most of that gap, where a one-word filler leaves the caller waiting in silence for
# the route.
#
# Intent-neutral, necessarily: this is spoken BEFORE anyone knows what the caller wants, so
# it can neither acknowledge the problem nor promise a destination.
#
# The opening words are `check_filler_pool_collisions`'s business, not taste. A caller who
# routes and then accepts the walkthrough draws from both pools, and two lines that start
# on the same word make the agent sound like it has one word for everything.
FILLER_ROUTING = [
    "Okay, let me take a look at that for you.",
    "Alright, give me just a moment to check on that.",
    "Thanks, let me see what's going on there.",
]
# The opening line, spoken verbatim by the ENGINE from the `welcome` announce on the
# router. Pinning it in the instruction instead costs head-intent detection, because it
# spends the opening turn's attention on reciting; leaving it to the model gives the caller
# a different opening every call.
SAY_WELCOME = "Thanks for calling Xfinity. What's going on with your service today?"

# The number was well formed and matched no account. Says what happened and, crucially,
# claims nothing about a line it never looked at. It names the number as the thing to
# check, because one mistyped digit is much the likeliest cause, and then hands off rather
# than looping on a re-read the caller has already given twice.
SAY_ACCOUNT_NOT_FOUND = (
    "I'm not finding an account with that number, so I haven't been able to check your "
    "line. Let me get you to someone who can track it down with you."
)

# The caller who wants to STOP, which is a different person from the caller who wants a
# human. The engine matches "forget it", "never mind", "stop", "I give up" and the rest
# itself and ends the call on them, so without a `cancel` disposition they get the
# framework's neutral default, written for someone whose business is finished.
#
# It CONFIRMS first, and that is the judgement in this pair. Every phrase in that set is
# also something a frustrated caller says without meaning to hang up, and the two mistakes
# cost very different amounts: a needless question costs one turn, while a wrong hang-up
# costs the entire call and they start again from the greeting with nothing carried over.
# The engine resumes the flow on anything that is not a clear yes, so the recovery is free.
#
# It must not sound like a goodbye. The likeliest answer on this turn is "no", so an
# opener that announces an ending is announcing it to a caller who is staying; the close
# belongs in SAY_CANCELLED, one turn later, if they confirm. A plain read-back opener
# commits to nothing.
SAY_CONFIRM_CANCEL = "Just to confirm, would you like me to stop here?"

# The close itself. Ends the call warmly and leaves the door open, and deliberately
# borrows SAY_WIFI_FIXED's closing clause: the caller who stops early and the caller
# whose Wi-Fi came back are both leaving with nothing outstanding.
SAY_CANCELLED = ("Okay, I'll stop there. If anything else comes up, we're here.")

# The rest of the copy, re-exported from the journeys that own it, so `import scripts`
# reaches every spoken line: the oracles and the recorded goldens all name it.
#
# Bound eagerly, and NOT through a PEP-562 `__getattr__`. `ladder_check.py` finds the
# latency-filler pools with `vars(scripts)`, and a lazy facade leaves that empty — so the
# check would find zero pools, report zero failures and pass.
#
# A line two journeys speak stays defined HERE, because the alternative is two definitions
# of one spoken sentence. Each journey declares `__all__`, so only copy crosses this
# boundary: not its slots and tasks, and not its own imports.

from journeys import (acknowledgements as _acknowledgements,
                      area_outage as _area_outage,
                      diagnostics_sweep as _diagnostics_sweep,
                      gateway_restart as _gateway_restart,
                      inconclusive_checks as _inconclusive_checks,
                      missing_equipment as _missing_equipment,
                      problem_clarification as _problem_clarification,
                      service_fees as _service_fees,
                      technician_visit as _technician_visit,
                      wifi_walkthrough as _wifi_walkthrough)

# Two lines live in `journeys/common` rather than in a journey: each has exactly one
# consumer there, and `common` is imported by every journey, so leaving them here would
# make the re-export a cycle.
from journeys.common import rungs as _rungs, waiting as _waiting

_SOURCES = (_rungs, _waiting,
            _acknowledgements,
            _area_outage,
            _diagnostics_sweep,
            _gateway_restart,
            _inconclusive_checks,
            _missing_equipment,
            _problem_clarification,
            _service_fees,
            _technician_visit,
            _wifi_walkthrough)

# Two journeys defining one name would otherwise resolve by import order, silently, and
# the loser's copy would simply never be spoken.
_seen = {}
for _mod in _SOURCES:
    for _name in _mod.__all__:
        if _name in _seen:
            raise AssertionError(
                f"{_name} is defined by both {_seen[_name]} and {_mod.__name__}; "
                "spoken copy has exactly one owner")
        _seen[_name] = _mod.__name__
        globals()[_name] = getattr(_mod, _name)
del _mod, _name, _seen
