"""The guardrails this agent runs behind, and what each one says when it fires.

A guardrail is a CES resource the platform evaluates around every turn — not a tool and
not a callback.

**Every rule here is deterministic, and that is a latency decision backed by measurement.**
A judged `policy` costs an LLM call on the critical path, and on this app's model an
agent-scoped one withholds the response until the judge returns. Read off CES span
durations on a live prose turn (`tests/latency_attribution.py`):

    Competitor Mention   336 ms      judged
    Prompt Guardrail     335 ms      judged
    Unprompted Credit    238 ms      judged
    Competitor Names       1 ms      matched
    Agent Profanity        0 ms      matched
    Internal Markup        0 ms      matched

So the three judged rules were 909ms of a 3761ms turn and the three matchers were free.
The judges are gone. They were also the three rules `guard_check.py` had never been able
to provoke a true positive from, so the measured cost was certain and the benefit was not.

What that gives up is recorded at each site below rather than pretended away: tier-2
competitor brands and unprompted-credit offers are now UNCOVERED, and injection screening
falls back to nothing. None is expressible as a matcher without false positives, and a
false positive is the worse failure here — this agent leads with a verdict and is measured
on exact first-turn text.

A guardrail fires on content. It cannot see a silent turn, a tool call or a slot value, so
the defects that turn on those belong elsewhere.
"""

# Harm categories. Fixed text is right here: by definition the turn is out of scope, and
# there is nothing to re-answer.
#
# Deliberately NOT the sentence 2.13.3 suggests: that one is the corporate register C6 and
# 2.4 rule out, it says "operational guidelines" to a member, and it stops dead where 2.1
# and 4.1 require a way forward. This refuses just as flatly and then hands the call back
# to the caller.
SAY_GUARDRAIL_REFUSAL = (
    "I'm not able to help with that. If you're having trouble with your internet, "
    "tell me what you're seeing and I'll take a look."
)

# C4: XA does not apologize on any channel and there is no exemption. Every directive
# below is spoken to the caller as regenerated speech, and each one fires on exactly the
# kind of turn where a model reaches for "sorry" unprompted: a refusal, a retraction, a
# cleanup, a withheld answer. Appended to all of them rather than written out each time,
# so it cannot be dropped from one of them by accident. What replaces an apology is the
# next step, which each directive already asks for.
_NO_APOLOGY = " Do not apologize. Do not say sorry, apologies, or any other regret phrasing."

GEN_GUARDRAIL_COMPETITOR = (
    "Answer the caller's question about their own internet service without naming, "
    "recommending or comparing any other provider. Plain conversational speech."
) + _NO_APOLOGY

# Warm and plain, never formal: that is the register 2.4 and 2.3.1 ask for, and the
# profanity clause carries the whole job of this rail on its own.
GEN_GUARDRAIL_PROFANITY = (
    "Say the same thing again in plain, everyday language, with no profanity."
) + _NO_APOLOGY

GEN_GUARDRAIL_MARKUP = (
    "The response you were about to give contained internal system markup and was "
    "withheld. Give the caller the same finding in plain conversational speech, with no "
    "angle brackets, variable names, status values or tool names. Do not mention that "
    "anything was withheld."
) + _NO_APOLOGY

# 2.13.1: the regenerated line has to carry the SAME finding, or a caller loses a real
# answer to a privacy rail. Naming the box is what makes that possible.
GEN_GUARDRAIL_SENSITIVE = (
    "The response you were about to give contained a device or network identifier and was "
    "withheld. Give the caller the same finding in plain conversational speech, naming the "
    "equipment instead of its address: say \"your gateway\" or \"your modem\", never a "
    "network address, a hardware address or a serial number. Do not mention that anything "
    "was withheld."
) + _NO_APOLOGY


import flows


# Competitors come in three tiers, not one list, because several competitor brands are
# ordinary words in network repair and a blunt deny-list would break real turns.
#
# Tier 1 — no other meaning in a repair call, so a deterministic match is safe.
_COMPETITORS = [
    "AT&T", "ATT", "Verizon", "Fios", "T-Mobile", "TMobile", "Starlink",
    "CenturyLink", "DirecTV", "Google Fiber", "EarthLink", "Mediacom",
    "WideOpenWest", "RCN", "Ziply", "Metronet", "Windstream", "HughesNet",
    "Viasat", "Sparklight", "Brightspeed", "Breezeline", "Astound",
    "US Cellular", "Cricket Wireless", "Mint Mobile",
]

# Tier 2 — brands that are ALSO ordinary words: Spectrum ("radio spectrum"), Optimum
# ("optimum signal strength"), Boost ("boost your signal"), Dish ("your satellite dish"),
# Charter, Frontier, Visible, Sonic, Kinetic, WOW, and Cox (a customer surname). These are
# now UNCOVERED. They were the judge's job, and the judge cost 336ms on every prose turn.
# Adding them to the list above is the one thing not to do: matching "spectrum" breaks
# "the five gigahertz spectrum", which is ordinary repair speech, and a false positive
# replaces a correct verdict with a refusal.
#
# Tier 3 — streaming brands are not blocked at all, and that is unchanged. They are
# symptoms this agent diagnoses, and "Netflix keeps buffering" is the normal case.

# Profanity is matched on the agent side only. A caller whose internet is broken may well
# swear, and blocking THEM would be a worse product than the problem it solves.
_PROFANITY = [
    "damn", "damned", "goddamn", "hell", "crap", "crappy", "bullshit", "bullcrap",
    "shit", "shitty", "piss", "pissed", "ass", "asshole", "bastard", "bitch",
    "dick", "prick", "screwed", "sucks", "fuck", "fucking", "fucked",
]


GUARDRAILS = [
    # The platform baseline the source app runs behind. The display names are byte-identical
    # to the source's, so the deployed app keeps the same two guardrails instead of gaining
    # two renamed ones. `level="strict"` is BLOCK_LOW_AND_ABOVE on all four harm categories.
    # Kept: `safety` is a `modelSafety` setting on generation itself, not a judge call, and
    # it cost no measurable time — it does not even appear as a span. Dropping it would buy
    # nothing and would lose the source app's own harm-category configuration.
    flows.safety("Safety Guardrail 1778685469753", level="strict",
                 on_trigger=flows.respond(SAY_GUARDRAIL_REFUSAL)),

    # Injection screening is GONE with the judge, and there is no matcher for it: an attack
    # is defined by what it is aimed at, not by any phrase. What remains against it is the
    # engine itself — the model does not choose this agent's dispositions, so a caller who
    # talks it into a different persona still cannot make it skip a rung, call a tool it
    # was not offered, or speak a line the ladder did not author.
    flows.blocklist(
        "Competitor Names",
        _COMPETITORS,
        match="word",
        scope="agent",
        on_trigger=flows.generate(GEN_GUARDRAIL_COMPETITOR),
    ),

    flows.blocklist(
        "Agent Profanity",
        _PROFANITY,
        match="word",
        scope="agent",
        on_trigger=flows.generate(GEN_GUARDRAIL_PROFANITY),
    ),

    # Nothing internal is ever spoken. Insurance rather than a fix for an observed defect,
    # and cheap: a deterministic filter prevents on both models, and unlike a callback it
    # is OBSERVABLE, so a leak shows up as a triggered span rather than being silently
    # repaired.
    #
    # Tag-shaped, NOT `<[^>]+>`: this agent talks about signal levels, and a bare
    # `<[^>]+>` matches the middle of "signal is < -70 dBm > the threshold" and would
    # replace a real answer with a refusal.
    flows.blocklist(
        "Internal Markup",
        [r"</?[a-zA-Z][a-zA-Z0-9:_-]*(\s[^<>]*)?/?>"],
        match="regex",
        scope="agent",
        on_trigger=flows.generate(GEN_GUARDRAIL_MARKUP),
    ),

    # 2.13.1 treats a hardware or network address as never-speakable, and both have a fixed
    # shape, so they gate deterministically here rather than being left to a judge — the
    # same reasoning as Internal Markup above.
    #
    # Account and phone digits are deliberately NOT matched. This agent legitimately says
    # "9 to 16 digit" and reads signal levels back, and a bare digit-run pattern would
    # replace real answers with a regeneration. Four dotted numbers, on the other hand, are
    # an IP address and nothing else in a repair call: a signal reading is two parts
    # ("-7.5 dBm"), never four. If this agent is ever taught to send a caller to the gateway
    # admin page, revisit the second pattern before doing it.
    flows.blocklist(
        "Sensitive Identifiers",
        [r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b",
         r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"],
        match="regex",
        scope="agent",
        on_trigger=flows.generate(GEN_GUARDRAIL_SENSITIVE),
    ),

    # Unprompted credit is UNCOVERED, and deliberately so rather than matched. A word list
    # on "credit" cannot tell a prohibited offer from a legitimate answer to a caller who
    # raised money first, and it would fire on "depending on the type of issue found, a
    # service charge may apply" — required wording this agent must be able to say. The
    # judge that could tell those apart cost 238ms on every prose turn.
    #
    # What holds the line instead is the instruction and the ladder: no rung offers money,
    # and the money question routes to a billing hand-off. `guard_check.py`'s
    # `network_impaired` bait case is the regression test for the required wording.
]
