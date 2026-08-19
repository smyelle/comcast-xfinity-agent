"""The conditions every journey gates on."""

import flows  # noqa: F401  -- re-exported for journeys that build their own gates


# A rung may only speak once the sweep has RESOLVED, not merely once every leg reported:
# before the statuses have values, a rung testing an unfilled slot `in` a list cannot
# pass and one testing it `filled: False` passes for the wrong reason. `Settle` fills
# every status before setting this, which is why the flag and the question agree.
SWEPT = {"slot": "diagnostics_complete", "filled": True}
# `escalate` is deliberately NOT tested here: it is a control slot the ENGINE compiles at
# runtime, so it is not in the config's slot set and the validator rejects a reference to
# it. It is also redundant — the escalate disposition terminates the flow before the
# ladder is reached.
NOT_YET_ANSWERED = {"slot": "verdict_delivered", "filled": False}

# The two legs of the escalate gate, declared together because the first is the same
# question the ladder asks.
#
# During an area outage no hand-off helps, so the request is declined rather than queued.
_OUTAGE_NOW = {"slot": "outage_status", "in": ["active", "degradation"]}
# ...and the HOLD. A hand-off before the diagnostics land carries nothing, so the request
# waits rather than being refused. `escalate_declined` is the engine's own counter, and
# the only leg here that moves on a call where the sweep never answers at all.
_DIAGNOSED_OR_DONE_WAITING = {"any": [
    SWEPT,
    {"slot": "verdict_delivered", "filled": True},
    {"slot": "escalate_declined", "gte": 3},
]}

# An outage inquiry has not asked to be diagnosed, so the whole ladder stays shut until
# they consent. `neq` holds on an UNFILLED slot, which is what makes this inert for every
# repair caller — the same shape `clarify.CLARIFIED` relies on.
INQUIRY_SETTLED = {"any": [{"slot": "call_intent", "neq": "outage_inquiry"},
                           {"slot": "full_check", "eq": "ACCEPT"}]}
