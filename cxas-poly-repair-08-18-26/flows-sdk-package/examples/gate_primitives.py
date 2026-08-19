"""A clarification gate built entirely from slots — the four gate primitives together.

The shape this exists for: the caller opens with a complaint, and the agent has to decide
whether to ask a clarifying question before doing any work. That reads like something only
an LLM can do, and it is usually written as instruction prose. It doesn't have to be.

  "My Streamly won't load"        -> app-specific, so ASK
  "nothing loads"                -> broad, so skip the question and just run the check
  "just Streamly"                 -> only that app: advise, don't diagnose
  "other sites too"              -> it's the connection: run the check
  "mmm, hard to say"             -> unresolvable; after 2 tries take the safe branch

Each primitive earns its place here:

  option_cues                classify the opening with NO model involvement
  cue_priority: "first"      "I only tried Streamly" hits both an "only" cue and an
                             "only tried" cue. Without a tiebreak that ambiguity fills
                             NOTHING and the question re-asks; declaration order fixes it
                             without hand-written negative lookaheads.
  {app_name|that app}        the question names the caller's app, but an app outside the
                             catalogue must not leak a literal "{app_name}" at them
  on_exhaust.fill            an answer the cues cannot resolve is not an error — no setter
                             reports anything, so the retry counter never moves and the
                             slot re-asks forever. `fill` lands it in the safe branch and
                             CONTINUES, rather than escalating out of the flow

`intent_change.switch` is implemented and unit-tested (tests/test_gate_primitives.py) but
is NOT exercised here: wiring the model-called intent setter into a demo needs more than
this example should carry, so it is not claimed as live-proven.

Build + validate offline:
    python -m examples.gate_primitives         # emits ./gate_primitives_app
"""

from pydantic import BaseModel, Field

import flows


class LineCheck(BaseModel):
  """What the connection check found."""

  summary: str = Field(description="One-sentence caller-facing result.")
  success: bool = Field(default=True)


class BillSummary(BaseModel):
  amount_due: str = Field(description="Formatted amount, e.g. '$84.20'.")
  success: bool = Field(default=True)


@flows.tool(flow="support")
def check_line() -> LineCheck:
  """Run the connection checks. Takes no arguments — nothing is collected first."""
  return LineCheck(summary="I ran a full check and your connection looks healthy.")


@flows.tool(flow="billing")
def lookup_bill() -> BillSummary:
  """Look up the current balance."""
  return BillSummary(amount_due="$84.20")


# ── Cue sets. Both are matched by the ENGINE against the raw utterance, case-insensitive
# regex, no model. The broad set is anchored on broad NOUNS rather than on the verbs it
# shares with the app set, so "My Meetly keeps dropping" and "The internet keeps dropping"
# separate cleanly.
SCOPE_CUES = {
    "app_specific": [r"\bstreamly\b", r"\bclip ?cast\b", r"\bmeetly\b", r"\btunely\b",
                     r"\bplaybox\b", r"\bmy email\b", r"\bmy printer\b",
                     # Deliberately app-specific but ABSENT from APP_NAMES below: the
                     # catalogue is open-ended, and this is the case the question's
                     # {app_name|that app} fallback exists for.
                     r"\bmy (smart ?tv|tablet|laptop)\b"],
    "broad": [r"\b(my |the )?internet\b", r"\bwi-?fi\b", r"\bnothing (will )?loads?\b",
              r"\beverything('s| is)? down\b", r"\bcan'?t connect to anything\b"],
}
APP_NAMES = {
    "Streamly": [r"\bstreamly\b"], "Clipcast": [r"\bclip ?cast\b"], "Meetly": [r"\bmeetly\b"],
    "Tunely": [r"\btunely\b"], "Playbox": [r"\bplaybox\b"],
    "your email": [r"\bmy email\b"], "your printer": [r"\bmy printer\b"],
}
# UNSURE is declared FIRST so that with cue_priority="first" it wins the "I only tried
# Streamly" overlap — the priority a plain cue dict cannot express.
REPLY_CUES = {
    "UNSURE": [r"\bnot sure\b", r"\bdon'?t know\b", r"\bonly tried\b", r"\bmaybe\b"],
    "ONLY_APP": [r"\bonly\b", r"\bjust\b", r"\beverything else (works|is fine)"],
    "EVERYTHING_DOWN": [r"\bnothing works?\b", r"\bother sites too\b", r"\bslow too\b"],
}

support = flows.Flow(
    "support",
    root_agent="Support_Agent",
    bootstrap={"reset_on_complete": True},
)

support.add(
    flows.passive_slot("complaint_scope", kind="intent", option_cues=SCOPE_CUES),
    flows.passive_slot("app_name", option_cues=APP_NAMES),
    flows.intent_slot(
        "clarify_reply", REPLY_CUES,
        ask=("Just so I check the right thing — is it only {app_name|that app} that's "
             "not working, or are other apps and websites also having trouble?"),
        condition=flows.eq("complaint_scope", "app_specific"),
        cue_priority="first",
        max_retries=2,
        reprompts=["Sorry — is it just that one, or are other sites having trouble too?"],
        on_exhaust="No problem — let me just run a quick check on your connection.",
        on_exhaust_fill="UNSURE",
    ),
    # Only that app: advise and stop. Declared BEFORE the check so it wins.
    flows.announce(
        "say_only_app",
        ["Since your other apps and sites are working, the issue is likely with "
         "{app_name|that app} itself rather than your connection. Try its status page, "
         "or closing and reopening it."],
        condition=flows.eq("clarify_reply", "ONLY_APP"), end=True),
    flows.result_slot("line_summary", "CheckLine"),
)
support.task("CheckLine", "check_line", [], "line_summary", out_key="summary",
             then_say="{line_summary}",
             # Not terminal: the engine defers a terminal fire on a turn carrying user
             # text, and nothing is collected here to produce the setter round trip that
             # would carry it — so a terminal would simply never speak.
             condition=("lambda f: f.get('clarify_reply') in ('EVERYTHING_DOWN', "
                        "'UNSURE') or f.get('complaint_scope') == 'broad'"))

billing = flows.Flow("billing", bootstrap={"reset_on_complete": True})
billing.add(flows.result_slot("amount_due", "LookupBill"))
billing.task("LookupBill", "lookup_bill", [], "amount_due", out_key="amount_due",
             terminal=True, then_say="Your balance is {amount_due}.")

router = flows.router_flow(
    "support_host", ["support", "billing"],
    default_flow="support",
    route_cues={"billing": ["my bill", "balance", "invoice", "how much do I owe"]},
    root_agent="Support_Agent",
)

app = flows.App(
    root_flow=router,
    extra_flows=[support, billing],
    app_display_name="Gate Primitives Demo",
    agent_instruction=(
        "You are a support agent. Follow the slot-filling framework directives exactly. "
        "Speak only what the framework gives you."
    ),
)


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./gate_primitives_app")
    print("built: ./gate_primitives_app")
