"""We need to send someone out."""

# The lead keeps the diagnosis and the dispatch together: split any earlier, the pause
# falls between "there's a problem" and "we'll send someone", where a hesitation reads
# as bad news.
SAY_NETWORK_TECH_LEAD = (
    "It looks like there's a problem with the network signal going to your home. "
    "We'll need to send a technician out to fix it."
)

# The good news first, the exception second, one sentence each. A negation with a nested
# exception hanging off it is not survivable on one listen: the caller would have to hold
# "don't need to be home" in mind until the exception finally resolved.
SAY_NETWORK_TECH_REST = (
    "You don't need to be home for this. You only need to be there if the technician "
    "has to get onto your property, for example through a locked gate."
)

SAY_NETWORK_GENERIC_LEAD = "We found an issue with the connection to your home."

# Two sentences, and both of them active. A bare passive with the actor deleted is how
# money news ends up sounding like weather: nobody decides the charge, it just applies.
# Naming the technician as the one doing the finding says the same thing and owns it. The
# "may" stays, because whether there is a charge is a genuine unknown at this point.
SAY_NETWORK_GENERIC_REST = (
    "A technician will take a closer look. Depending on what they find, there may be "
    "a service charge."
)

CONVOY_IMPAIRMENT = {"slot": "convoy_status", "eq": "predictive_impairment"}

# `upper` normalizes before comparing: the specialist reports a spaced, lower-case value
# ("network tech"), while the hook's own fallback writes the underscored one when the
# specialist reports nothing, so both spellings have to be accepted.
NETWORK_TECH = {"all": [{"slot": "network_status", "eq": "impaired"},
                        {"slot": "technician_type", "upper": True,
                         "in": ["NETWORK TECH", "NETWORK_TECH"]}]}

NETWORK_GENERIC = {"slot": "network_status", "eq": "impaired"}

__all__ = [
    'CONVOY_IMPAIRMENT',
    'NETWORK_GENERIC',
    'NETWORK_TECH',
    'SAY_NETWORK_GENERIC_LEAD',
    'SAY_NETWORK_GENERIC_REST',
    'SAY_NETWORK_TECH_LEAD',
    'SAY_NETWORK_TECH_REST',
]


import scripts
from journeys.common.rungs import rung


def predicted():
  """A technician, because the outage system PREDICTED an impairment."""
  return [
      # P4 — convoy predicted an impairment; it carries its own customer-facing wording.
      # Not split despite handing off, because the script is a runtime placeholder: the
      # wording arrives from the outage system and there is no sentence here to divide.
      rung("HandleConvoyImpairment", "verdict_convoy_impairment",
           CONVOY_IMPAIRMENT, "{convoy_customer_message}"),
  ]


def measured():
  """A technician, because we MEASURED a fault on the line."""
  return [
      # P6 — line/network impairment, split on who gets dispatched.
      rung("HandleNetworkTech", "verdict_network_tech",
           NETWORK_TECH, SAY_NETWORK_TECH_REST,
           say_first=SAY_NETWORK_TECH_LEAD),
      rung("HandleNetworkImpairment", "verdict_network_generic",
           NETWORK_GENERIC, SAY_NETWORK_GENERIC_REST,
           say_first=SAY_NETWORK_GENERIC_LEAD),
  ]
