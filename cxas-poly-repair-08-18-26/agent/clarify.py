"""The Intent Clarification Gate, expressed as slots rather than prose.

Before running diagnostics, if the caller names a SPECIFIC app or service rather than
describing a broad outage, ask whether it is only that app, then branch three ways. The
classification is deterministic and the branch is a slot value, so three slots do the
whole job:

  complaint_scope   passive intent slot. `option_cues` are matched by the ENGINE against
                    the raw utterance with no model involvement, so "My Netflix doesn't
                    work" and "nothing will load" separate before the model ever runs.
  app_name          a second cue slot whose KEYS are display strings, so the question can
                    say the caller's own app back to them.
  clarify_reply     the question, and the three-way branch on the answer.

Two properties of cue matching shape everything below:

  * Cues are case-insensitive UNANCHORED REGEXES over the raw text (no normalization —
    hence every apostrophe is written `'?`).
  * If TWO values match, the slot is left UNFILLED. There is no priority and no
    longest-match tiebreak. So the cue sets must be mutually exclusive, which is why the
    broad set is anchored on broad NOUNS (internet / wi-fi / everything / nothing) rather
    than on the verbs it shares with the app-specific set ("won't load", "keeps
    dropping").

The gate's AGENT branch lives in the flow's `escalate` block instead (see app.py): the
engine's own escalate detector fires on human-request phrasings before the DAG sees the
turn. That is also a rescue — "can I just talk to someone" contains "just", which would
otherwise be captured as ONLY_APP.
"""

# "Will this cost me anything?" — engine-side cues, no model involved, so the answer is
# the approved verbatim rather than an improvisation about pricing.
#
# One value only, so the two-values-means-unfilled rule above cannot bite. Deliberately
# narrow: a bare `\bcharge\b` fires on "my phone won't charge", and a caller who said
# that would get a fee schedule read at them. Every cue here carries money context,
# either in the word itself (fee, cost, price, bill) or in the phrasing around it.
COST_CUES = {
    "asked": [
        r"\bhow much\b", r"\bcosts?\b", r"\bprice\b", r"\bfees?\b",
        r"\bcharge (me|us|for|extra|anything)\b",
        # Adverbs get in the way: "will I ACTUALLY be charged".
        r"\b(be|get|am i|are we|will i|will we)\s+(?:\w+\s+){0,2}charged\b",
        r"\bservice charge\b", r"\bextra charge\b",
        r"\b(is|are) (it|this|that|they) free\b", r"\bfor free\b",
        r"\bdo i (have to |need to )?pay\b", r"\bwho pays\b",
        r"\bon my bill\b", r"\bbilled\b",
    ],
}

# The caller is fed up, or has just told us they already did that. Every rung in this
# agent speaks a verbatim script and a preempted turn gives the model no chance to say
# anything of its own, so without cues the agent cannot acknowledge either.
#
# Two separate signals, because they need different answers. FRUSTRATION wants the
# feeling named back and nothing more: say plainly that this is frustrating, then get
# straight back to fixing it. It does NOT want an apology, because XA does not say sorry
# on any channel (C4) and there is no error-recovery exemption. ALREADY_TRIED wants us to
# notice and move on. Repeating a step the caller has just said they performed is the
# fastest way to sound like a machine.
FRUSTRATION_CUES = {
    "yes": [
        r"\bwaste of (my )?time\b", r"\bfrustrat(ing|ed)\b", r"\bridiculous\b",
        r"\bannoying\b", r"\bdriving (me|us) (crazy|mad|nuts)\b",
        r"\bfed up\b", r"\bsick of\b", r"\bnot good enough\b",
        r"\bstill haven'?t\b", r"\byou keep\b", r"\bthis is (a )?joke\b",
        r"\bgetting nowhere\b", r"\bgoing (round|around) in circles\b",
        r"\bevery (single )?time\b", r"\bhonestly\b.{0,30}\b(useless|hopeless)\b",
        r"\bcome on\b", r"\bseriously\?", r"\bunacceptable\b",
        # Distress stated plainly, rather than as a complaint about us. Every cue above
        # waits for a stock phrase of annoyance, so "I'm really upset" and "at my wit's
        # end" reach none of them, and 2.3.1 asks for the feeling to be named back
        # whichever way it arrives.
        # The emotion word needs a first-person frame. Bare "upset" also has a transitive
        # sense ("I upset the router by moving it") that has nothing to do with feeling.
        r"\b(i'?m|i am|we'?re|we are|feeling|getting|so|really|very|pretty|quite)"
        r"\s+(\w+\s+){0,2}(upset|angry|furious|livid|distressed|stressed|desperate)\b",
        r"\bwit'?s?'? end\b", r"\bend of my (rope|tether)\b",
        # What the fault has cost, which is how a working caller says the same thing.
        r"\blos(t|ing) .{0,20}\b(work|business|money|income|pay|customers)\b",
        r"\bcan'?t work\b", r"\bmissed .{0,15}\b(meeting|deadline|call)\b",
        r"\b(hours|days|weeks) (on hold|without|of this)\b",
    ],
}
# "I already did that." Distinct from `wifi_tried`, which names WHICH tip so it can be
# skipped; this one only has to notice that the caller is repeating themselves.
ALREADY_TRIED_CUES = {
    "yes": [
        r"\b(i|we)'?ve already\b", r"\b(i|we) already (did|tried|done)\b",
        r"\balready tried\b", r"\balready done\b", r"\bdid that already\b",
        r"\btried (that|this) (already|before)\b",
        r"\blike i (said|told you)\b", r"\bas i said\b",
        r"\bi just (said|told you)\b",
    ],
}

# "Is there an outage in my area?" — a different call from a repair call. This caller is
# not reporting a fault and has not asked to be diagnosed; they want one fact, so the
# outage is answered first and the rest offered.
#
# Only the inquiry value is cued. "repair" is the absence of this, not a rival cue set —
# two matching values leave the slot unfilled (see above), and a second broad set of
# fault words would collide with SCOPE_CUES on almost every real opening line.
CALL_INTENT_CUES = {
    "outage_inquiry": [
        r"\b(is|are)\s+(there|you|we)\s+(an?\s+)?(outage|outages)\b",
        r"\bany\s+(known\s+)?outages?\b",
        r"\boutage\s+in\s+(my|our|the)\s+area\b",
        r"\bis\s+(the\s+)?(service|internet)\s+(down|out)\s+in\b",
        r"\b(service|internet)\s+(down|out)\s+in\s+(my|our|the)\s+(area|neighou?rhood)\b",
        r"\bknown\s+(issue|problem)s?\s+in\s+(my|our|the)\s+area\b",
        r"\bare\s+other\s+people\b.*\b(down|out|affected)\b",
    ],
}

# "Just reboot my modem" — the caller asking for a restart outright, rather than being
# offered one.
#
# Every cue names the EQUIPMENT. Bare "reboot" and "restart" are not enough: they
# collide with the walkthrough's own tip about restarting a phone or laptop, and with a
# caller reporting what they have already tried. Past tense is deliberately absent for
# the same reason — "I restarted my router" is a report, not a request, and answering it
# by rebooting the gateway would be acting on the opposite of what was said.
REBOOT_REQUEST_CUES = {
    "asked": [
        r"\b(reboot|restart|reset|power ?cycle)\s+(my|the|our)\s+"
        r"(modem|router|gateway|box|equipment)\b",
        r"\b(can|could|will|would)\s+you\s+(reboot|restart|reset|power ?cycle)\b",
        r"\bpower ?cycle\b",
        r"\b(just|please)\s+(reboot|restart|reset)\s+it\b",
    ],
}

# Opening complaint: app-specific vs broad outage.
#
# TWO named apps is not "one app", and `cue_priority="first"` on `app_name` would pick one
# of them — right when a real product name races the generic catch-all, wrong when two
# real products race. Matching two apps into the BROAD set makes `complaint_scope`
# ambiguous, and two matching values leave a cue slot UNFILLED, which is exactly the
# outcome wanted: unfilled passes `CLARIFIED` (its `neq` holds), so the gate is skipped and
# the sweep runs immediately. The caller has already said it is more than one thing.
#
# The vocabulary is derived from APP_CATALOGUE below so the two cannot drift apart.
_APP_WORD = r"(?:playstation|the service|smart home|my printer|disney\+?|you ?tube|instagram|my email|facebook|fortnite|netflix|my game|spotify|twitch|tiktok|roblox|amazon|google|my app|gmail|steam|teams|slack|zoom|xbox|hulu|ps5)"
_TWO_APPS = _APP_WORD + r"\b.{0,80}?\b" + _APP_WORD

SCOPE_CUES = {
    "app_specific": [
        r"\bnetflix\b", r"\bfacebook\b", r"\byou ?tube\b", r"\bhulu\b",
        r"\bdisney\+?\b", r"\bspotify\b", r"\btiktok\b", r"\binstagram\b",
        r"\bzoom\b", r"\bteams\b", r"\bslack\b", r"\bgoogle\b", r"\bgmail\b",
        r"\bxbox\b", r"\bplaystation\b", r"\bps5\b", r"\bsteam\b", r"\btwitch\b",
        r"\broblox\b", r"\bfortnite\b", r"\bamazon\b",
        r"\bmy game\b", r"\bmy email\b", r"\bmy printer\b",
        r"\bbank(ing)? (web ?site|app)\b", r"\bsmart home\b",
        # Generic: the caller blames one named thing without naming a known product
        # ("just the service", "only that app"). The matching APP_CATALOGUE entry is
        # declared LAST so cue_priority="first" lets a real product name win.
        r"\bthe service\b", r"\b(the|this|that) app\b", r"\bmy app\b",
        r"\b(this|that) (site|website)\b",
        # Xfinity equipment. One misbehaving box is as app-specific as one misbehaving
        # app, and routing it through the gate establishes "not the network, according to
        # the customer" — the precondition for looking device help up. The GATEWAY is
        # deliberately absent: diagnosing it is the ladder's entire job, so a gateway
        # complaint must reach diagnostics, not this branch.
        r"\bx-?fi pods?\b", r"\bpods?\b", r"\bwi-?fi extenders?\b", r"\bextenders?\b",
        r"\bx1\b", r"\bcable box(es)?\b", r"\btv box(es)?\b", r"\bset-?top box(es)?\b",
        r"\bremotes?\b", r"\bclickers?\b", r"\bcameras?\b", r"\bdoorbells?\b",
        r"\bxfinity app\b",
        r"\btvs?\b", r"\btelevisions?\b", r"\btelly\b", r"\bdvr\b",
        r"\bon.?demand\b", r"\b(on.?screen|tv|channel) guide\b",
        r"\bmotion sensors?\b", r"\bhome security\b", r"\balarm system\b",
    ],
    # Anchored on the BROAD noun, never on the shared verb, so "The internet keeps
    # dropping" is broad while "My Zoom keeps dropping" is app-specific.
    "broad": [
        _TWO_APPS,
        # Bare "it's everything" / "the whole house". Every other broad cue pairs a noun
        # with a fault word ("everything is down"), so without these a caller saying "it
        # seems like it's everything" matches nothing broad, and a stray product name
        # elsewhere in the sentence makes the whole complaint app-specific.
        r"\bit'?s everything\b", r"\bit is everything\b",
        r"\beverything('?s| is)? (affected|slow|struggling|playing up)\b",
        r"\bthe whole (house|place|lot)\b", r"\ball of it\b",
        r"\bevery(thing| device) in the (house|home)\b",
        r"\b(my |the )?internet\b", r"\bwi-?fi\b", r"\bno connection\b",
        r"\ball my devices\b", r"\beverything('s| is)? (down|offline|out)\b",
        r"\bnothing (will |can |does )?load", r"\bnothing works?\b",
        r"\bcan'?t connect to anything\b", r"\bmy service is out\b",
        r"\bno service\b",
    ],
}

# Keys are DISPLAY strings — this slot exists so the question can name the caller's own
# app. An app outside this set leaves both slots empty, which skips the gate and runs
# diagnostics: the safe outcome.
APP_CATALOGUE = {
    "Netflix": [r"\bnetflix\b"],
    "Facebook": [r"\bfacebook\b"],
    "YouTube": [r"\byou ?tube\b"],
    "Hulu": [r"\bhulu\b"],
    "Disney Plus": [r"\bdisney\+?\b"],
    "Spotify": [r"\bspotify\b"],
    "TikTok": [r"\btiktok\b"],
    "Instagram": [r"\binstagram\b"],
    "Zoom": [r"\bzoom\b"],
    "Teams": [r"\bteams\b"],
    "Slack": [r"\bslack\b"],
    "Google": [r"\bgoogle\b"],
    "Gmail": [r"\bgmail\b"],
    "Amazon": [r"\bamazon\b"],
    "Xbox Live": [r"\bxbox\b"],
    "PlayStation": [r"\bplaystation\b", r"\bps5\b"],
    "Steam": [r"\bsteam\b"],
    "Twitch": [r"\btwitch\b"],
    "Roblox": [r"\broblox\b"],
    "Fortnite": [r"\bfortnite\b"],
    "your game": [r"\bmy game\b"],
    "your email": [r"\bmy email\b"],
    "your printer": [r"\bmy printer\b"],
    "your bank's website": [r"\bbank(ing)? (web ?site|app)\b"],
    "your smart home devices": [r"\bsmart home\b"],
    # LAST on purpose: only reached when no real product name matched (cue_priority).
    # Keyed "the service" because it is a DISPLAY string — it renders straight into the
    # question and the advice ("the issue is likely with the service itself").
    "the service": [r"\bthe service\b", r"\b(the|this|that) app\b", r"\bmy app\b",
                     r"\b(this|that) (site|website)\b"],
}

# The reply. The sets overlap out of the box — "I only tried Netflix" belongs to UNSURE
# but leads with "only", the headline ONLY_APP cue. Rather than hand-write negative
# lookaheads to keep them disjoint, UNSURE is declared FIRST and `cue_priority="first"`
# resolves the overlap by authored order.
REPLY_CUES = {
    # These cues are the only path into `clarify_reply` under the router: the
    # `set_clarify_reply` setter the model would otherwise call is in `router_hide_tools`,
    # and an in-flow turn runs Pass-A classification with every other tool hidden. So a
    # phrasing these cues miss is a stuck turn, not a graceful fallback — hence the width,
    # including the adverb that a plain `\bnot sure\b` cannot see.
    "UNSURE": [
        r"\bnot (really |entirely |totally |quite |too |all that |terribly )?sure\b",
        r"\bunsure\b", r"\bdon'?t know\b", r"\bno idea\b", r"\bno clue\b",
        r"\bcouldn'?t tell you\b", r"\bcan'?t tell\b", r"\bhard to say\b",
        r"\bhaven'?t checked\b", r"\bhave ?n'?t tried\b",
        r"\bonly tried\b", r"\bonly (used|tested|checked)\b",
        r"\bi guess\b", r"\bmaybe\b",
    ],
    "ONLY_APP": [
        # `only` / `just` need something to attach to. Bare, they are two of the commonest
        # filler words in English and they fire on speech that says the OPPOSITE ("it just
        # sits there spinning").
        #
        # No capitalisation-based pattern either: cues are matched case-INSENSITIVELY (see
        # the header), so `[A-Z]` also matches lowercase and `(only|just)\s+[A-Z]\w+`
        # silently degrades to "only|just followed by any word".
        #
        # A DENIAL is not an assertion: "it's not just the app" means the opposite of this
        # branch. The lookbehinds are fixed-width, so they are legal, and cover the three
        # ways it gets said.
        r"(?<!not )(?<!n't )(?<!nor )"
        r"\b(only|just)\s+(that|this|the)?\s*\w*\s*(app|one|site|thing|service|"
        r"channel|stream)\b",
        r"(?<!not )(?<!n't )(?<!nor )" r"\bit'?s (only|just)\b",
        # "only Facebook", "just my printer" — derived from the app list rather than a
        # second copy of it, so a product added to SCOPE_CUES is covered here too.
        *[r"(?<!not )(?<!n't )(?<!nor )" + r"\b(only|just)\s+" + _p.lstrip("\\b")
          for _p in SCOPE_CUES["app_specific"]],
        r"\beverything else (works|is fine|loads|is working)",
        r"\bother (stuff|sites|things|apps) (works?|loads?|are fine|is fine)",
        r"\beverything else is (ok|okay|fine)",
    ],
    "EVERYTHING_DOWN": [
        # `not just X` in ANY form: the X is whatever they named, and constraining it to
        # app/one/thing misses "not just Netflix", the commonest way of all to say it.
        r"\bnot just\b", r"\bnot only\b",
        # A bare "everything" answers "is it only that app?" on its own. Guarded against
        # "everything else works", which is the ONLY_APP answer and must not collide:
        # two matching values leave the slot UNFILLED, which is the loop all over again.
        r"\beverything\b(?!\s+else)", r"\ball (of )?(our|my|the) devices\b",
        r"\ball our other\b", r"\bevery device\b",
        r"\bmore than (just )?(the |one )\b",
        # A second named device is the clearest signal a caller gives that this is not one
        # app. Matched in either order, and with a generous window: the second half of the
        # sentence can run long before the "too" lands.
        r"\b(also|too)\b[^.]{0,60}\b(xbox|playstation|ps5|tv|telly|laptop|phone|"
        r"tablet|console|computer)\b",
        r"\b(xbox|playstation|ps5|tv|telly|laptop|phone|tablet|console|computer)\b"
        r"[^.]{0,60}\b(too|as well)\b",
        r"\bnothing works?\b", r"\bnothing else works?\b",
        r"\bother (sites|apps|things|stuff) (are|is) (slow|down|broken)",
        r"\bcan'?t get on anything\b", r"\bcan'?t get to anything\b",
        r"\beverything('s| is) (down|slow|broken)",
        r"\bother sites too\b", r"\bslow too\b", r"\bthose too\b",
        r"\bthey'?re all\b",
    ],
}

# Model-called backstop for a reply the cues cannot resolve. Unlike `option_cues` a
# classifier is ORDERED first-hit-wins and has a DEFAULT, so it always yields a value and
# cannot deadlock. UNSURE is both first and the default, because its branch runs
# diagnostics — the safe outcome.
REPLY_CLASSIFIER = {
    "UNSURE": ["not sure", "dont know", "don't know", "no idea", "havent checked",
               "haven't checked", "only tried", "only used", "i guess", "maybe"],
    "ONLY_APP": ["only", "just", "everything else works", "other stuff loads fine",
                 "everything else is fine"],
    "EVERYTHING_DOWN": ["nothing works", "other sites too", "everything is down",
                        "cant get on anything", "can't get on anything", "slow too"],
}

# The spoken scripts.

ASK_CLARIFY = (
    "Just so I check the right thing. Is it only {app_name|that app} that's not working, or "
    "are other apps and websites also having trouble?"
)
# The same question for EQUIPMENT, which needs its own wording twice over: a pod is not an
# app, and the contrast that matters for a device is against the connection rather than
# against "other apps and websites".
#
# One wording covers singular and plural because `device_subject` is empty exactly when
# two devices were named: "is it only your xFi pod" / "is it only those".
#
# No dash, which chops the audio (AGENTS.md rule 6), and no idiom (2.10.2). Two short
# sentences rather than one long breath, matching ASK_CLARIFY above and this slot's own
# reprompt.
ASK_CLARIFY_DEVICE = (
    "Just so I check the right thing. Is it only {device_subject|those} giving you "
    "trouble, or is your internet having trouble too?"
)

# The SECOND time of asking, one per wording above, and the reason they exist is that
# the caller is asked again without having been given a turn to answer. The platform
# MANUFACTURES turns nobody took -- an ASYNCHRONOUS check publishing its result, an
# inactivity tick -- and an outstanding question is put again on each of them. Measured
# on the deployed demo over voice: the device question was spoken at 20.3s, 30.3s and
# 45.6s, word for word, while the caller was still thinking about the first one.
#
# Shorter than the first ask, and it opens by naming the wait, so a caller who DID hear
# the question is not asked it a second time as though they had said nothing. Same three
# branches, same cue sets: only the sentence differs.
ASK_CLARIFY_AGAIN = (
    "Take your time. Is it only {app_name|that app}, or are other sites having trouble "
    "too?"
)
ASK_CLARIFY_DEVICE_AGAIN = (
    "Take your time. Is it only that, or is your internet having trouble too?"
)
# The LAST rung of both ladders, and it asks for nothing. `intent_slot(ask=[...])` clamps
# to the final rung, so this is what every turn after the second one would speak, and a
# question repeated a third time is not a question any more. The `no_input` ladder owns
# the turns after this -- a silent tick, then "I didn't catch that" -- so all this has to
# do is leave the floor with the caller.
SAY_CLARIFY_STILL_HERE = "No rush. I'm here when you're ready."

# Every sentence starts with its subject, because a caller cannot tell what a sentence
# opening on a subordinate clause is about until it is half over. The two things to try are steps joined by a discourse
# marker (4.3.6) rather than run together, and nothing hedges what the agent is certain
# of (2.10.2). "probably" stays, because that uncertainty is real. Longest line on this
# path, so it earns the care.
SAY_ONLY_APP = (
    "Your other apps and websites are working, so the trouble is probably with "
    "{app_name|that app}, not your internet. Here's what to try. Close "
    "{app_name|that app} and open it again, then check its status page. If other sites "
    "start having trouble too, get back in touch and I'll run a full check on your "
    "connection."
)
SAY_EVERYTHING_DOWN = (
    "Got it. Sounds like it's your internet connection, not just the app. Let me "
    "check your service now."
)
SAY_UNSURE = (
    "No worries. Let me run a quick check on your connection to rule that out."
)

# Xfinity equipment: what the caller is holding, so device help can be looked up.
#
# ONE SLOT PER FAMILY, each with a single value, which is what makes "my pod and my remote
# are both playing up" expressible: the engine's fill loop writes EVERY non-intent cue slot
# whose set matches unambiguously, so N one-value slots capture N devices from one
# utterance. A single catalogue slot could not — two matching values in one slot leaves it
# UNFILLED. One value per slot also means the two-match rule can never bite here, so unlike
# APP_CATALOGUE these need no `cue_priority`.
#
# Keys are DISPLAY strings: they are read back to the caller and go into the search query.
EQUIPMENT = {
    "dev_pod": {"xFi pod": [r"\bx-?fi pods?\b", r"\bpods?\b",
                            r"\bwi-?fi extenders?\b", r"\bextenders?\b"]},
    # `\bx1\b` needs the lookahead. "X1" brands the whole product line, so without it "my
    # X1 remote stopped pairing" matches the TV BOX as well as the remote — two devices
    # from one, which reports "those" for a single complaint and searches the wrong one.
    "dev_tv_box": {"X1 TV Box": [r"\bx1\b(?!\s+(remote|voice\s*remote|app))",
                                 r"\bcable box(es)?\b", r"\btv box(es)?\b",
                                 r"\bset-?top box(es)?\b",
                                 # TV itself, and the things only a TV box has.
                                 r"\btvs?\b", r"\btelevisions?\b", r"\btelly\b",
                                 r"\bdvr\b", r"\bon.?demand\b",
                                 r"\b(on.?screen|tv|channel) guide\b"]},
    "dev_remote": {"remote": [r"\bremotes?\b", r"\bclickers?\b"]},
    "dev_camera": {"camera": [r"\bcameras?\b", r"\bdoorbells?\b",
                              # Xfinity Home is folded into repair too, so its other
                              # hardware needs naming or it falls through to the ladder.
                              r"\bmotion sensors?\b", r"\bsensors?\b",
                              r"\bhome security\b", r"\balarm system\b"]},
    "dev_app": {"Xfinity app": [r"\bxfinity app\b", r"\bmy account app\b"]},
    # Gateway is deliberately NOT in SCOPE_CUES above. The ladder's whole job is to
    # diagnose the gateway, so "my modem is broken" must still run diagnostics rather
    # than being treated as a one-device complaint. The slot still fills, which is what
    # lets a post-swap follow-up ("where do I return the old gateway?") build a query.
    "dev_gateway": {"gateway": [r"\bgateways?\b", r"\bmodems?\b", r"\brouters?\b"]},
}

# What is wrong with it, and what they want to do about it. Both feed the query, so the
# search reads like the caller's own question rather than a generic article lookup.
# Multi-valued, so both need `cue_priority="first"` on the slot.
DEVICE_SYMPTOM = {
    "won't pair": [r"\bwon'?t pair\b", r"\bnot pairing\b", r"\bstopped pairing\b",
                   r"\bwon'?t sync\b", r"\bnot responding\b"],
    "keeps going offline": [r"\bgo(es|ing)? offline\b", r"\bkeeps? disconnect",
                            r"\boffline\b"],
    "keeps dropping": [r"\bkeeps? dropping\b", r"\bdrops? out\b",
                       r"\bdropping off\b", r"\bkeeps? cutting out\b"],
    "won't turn on": [r"\bwon'?t turn on\b", r"\bno power\b", r"\bwon'?t power\b",
                      r"\bis dead\b"],
    "blinking light": [r"\bblinking\b", r"\bflashing\b", r"\borange light\b",
                       r"\bred light\b"],
    "black screen": [r"\bblack screen\b", r"\bno picture\b", r"\bno signal\b"],
    "frozen": [r"\bfrozen\b", r"\bfreez(es|ing)\b", r"\bstuck\b",
               r"\bpixelat(ed|ing)\b", r"\bwon'?t come up\b"],
    "not recording": [r"\bnot recording\b", r"\bstopped recording\b",
                      r"\bwon'?t record\b"],
    "is slow": [r"\bis slow\b", r"\brunning slow\b", r"\bso slow\b"],
    # The catch-all, which keeps the query from falling back to a bare product lookup. It
    # is LAST because `cue_priority="first"` means a specific symptom above always wins.
    "not working": [r"\bplaying up\b", r"\bacting up\b", r"\bnot working\b",
                    r"\bisn'?t working\b", r"\bwon'?t work\b", r"\bis broken\b",
                    r"\bhaving (trouble|issues|problems)\b", r"\bon the fritz\b"],
}

DEVICE_NEED = {
    "return": [r"\breturn\b", r"\bsend (it )?back\b", r"\bdrop (it )?off\b"],
    "self install": [r"\bself.?install\b", r"\binstall\b", r"\bset ?up\b",
                     r"\bactivate\b"],
    "factory reset": [r"\bfactory reset\b", r"\breset\b"],
    "restart": [r"\brestart\b", r"\breboot\b", r"\bpower cycle\b"],
    "connect": [r"\bconnect\b", r"\bpair\b"],
    "fix": [r"\bfix\b", r"\btroubleshoot\b", r"\bwhat do I do\b", r"\bhow do I\b"],
}

# The caller has named equipment. Either half of the search gate needs this.
DEVICE_NAMED = {"any": [{"slot": name, "filled": True} for name in EQUIPMENT]}

# The same equipment cues collapsed into ONE slot, so the clarifying question can say the
# device back to the caller. Being one slot is the trick: the engine leaves a slot UNFILLED
# when two of its values match, so this holds the device's name when exactly one was named
# and nothing when two were — which is precisely when the question should say "those".
# Keys are second-person display strings because they render mid-sentence.
DEVICE_SUBJECT = {
    "your xFi pod": EQUIPMENT["dev_pod"]["xFi pod"],
    "your X1 TV Box": EQUIPMENT["dev_tv_box"]["X1 TV Box"],
    "your remote": EQUIPMENT["dev_remote"]["remote"],
    "your camera": EQUIPMENT["dev_camera"]["camera"],
    "the Xfinity app": EQUIPMENT["dev_app"]["Xfinity app"],
    "your gateway": EQUIPMENT["dev_gateway"]["gateway"],
}

# The gate is asked by ONE of two slots — `clarify_reply` for a third-party app,
# `clarify_reply_device` for Xfinity equipment — because the two need different wording
# and a slot carries exactly one `ask`. They are mutually exclusive by condition, so
# every downstream test reads "whichever one was asked".
_REPLY_SLOTS = ("clarify_reply", "clarify_reply_device")


def _either_reply(*values):
  return {"any": [{"slot": s, "in": list(values)} for s in _REPLY_SLOTS]}


# The gate is settled when the caller never named an app, or answered the question —
# with ANY of the three answers. Every diagnostic rung requires this, so a verdict can
# never jump the queue ahead of the clarifying question.
#
# "Answered", not "answered in a way that sends us to diagnostics": admitting only
# EVERYTHING_DOWN and UNSURE makes an ONLY_APP caller ineligible for every diagnostic rung,
# so an area outage or a restricted account the sweep has already measured can never be
# spoken to them. With every answer admitting the diagnostics, the app-specific advice rung
# sits BELOW them and speaks only when nothing else matched — which is what "the issue is
# likely the app itself" has to mean in order to be true.
CLARIFIED = {"any": [{"slot": "complaint_scope", "neq": "app_specific"},
                     {"any": [{"slot": s, "filled": True} for s in _REPLY_SLOTS]}]}
ONLY_APP = _either_reply("ONLY_APP")
APP_SPECIFIC = {"slot": "complaint_scope", "eq": "app_specific"}
# An app complaint is one that named no equipment — that is what routes the caller to the
# app-worded question rather than the device-worded one.
APP_SPECIFIC_NOT_DEVICE = {"all": [APP_SPECIFIC, {"not": DEVICE_NAMED}]}
DEVICE_SPECIFIC = {"all": [APP_SPECIFIC, DEVICE_NAMED]}
REPLY_EVERYTHING_DOWN = _either_reply("EVERYTHING_DOWN")
REPLY_UNSURE = _either_reply("UNSURE")
