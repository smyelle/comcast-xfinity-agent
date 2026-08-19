"""No example may name a real company.

The docs site enforces this over its own pages (`content.test.ts`, "names no real
companies"), but the examples are Python and were outside that net — a search example
shipped naming two real carriers in `preferred_domains` before this existed. `Acme` is
the house placeholder; invent an obviously-fictional name for anything else.
"""

import os
import re

_EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")

# Customer/brand names in example SCENARIOS — not the platform vendor, which examples
# legitimately name (Google Cloud, gcloud). A denylist can never be complete; this
# catches the names an author actually reaches for. Names that are also ordinary words
# are matched case-sensitively and separately (see _CASE_SENSITIVE below).
_BRANDS = re.compile(
    r"\b(fedex|dhl|usps|netflix|hulu|spotify|disney|verizon|comcast|xfinity|equifax|"
    r"experian|transunion|t-mobile|at&t|vodafone|rogers|telus|amazon|walmart|costco|"
    r"chase|citibank|paypal|stripe|uber|lyft|doordash|geico|allstate|aetna|cigna|cvs|"
    r"walgreens|microsoft|facebook|instagram|whatsapp|tiktok|twitter|linkedin|"
    r"salesforce|servicenow|ibm|nvidia|samsung|nintendo|xbox|playstation|"
    r"airbnb|expedia|ebay|etsy|kroger|safeway)\b",
    re.IGNORECASE,
)
# Names that are also ordinary words, so they are matched case-sensitively: "follow-ups"
# is not the courier, and an "offline oracle" is a test double rather than the database.
_CASE_SENSITIVE = re.compile(r"\b(UPS|Oracle)\b")


def test_no_example_names_a_real_company():
  hits = []
  # `.md` too: the VERIFY notes sit beside the examples and quote real transcripts,
  # which is exactly where a customer name gets copied in by hand.
  for name in sorted(os.listdir(_EXAMPLES)):
    if not name.endswith((".py", ".md")):
      continue
    path = os.path.join(_EXAMPLES, name)
    with open(path, encoding="utf-8") as fh:
      for i, line in enumerate(fh, 1):
        for m in _BRANDS.finditer(line):
          hits.append(f"{name}:{i} {m.group(0)!r}")
        for m in _CASE_SENSITIVE.finditer(line):
          hits.append(f"{name}:{i} {m.group(0)!r}")
  assert hits == [], hits
