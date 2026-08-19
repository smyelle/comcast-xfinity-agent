"""Reusable value validators for hand-authored `@flows.tool` setters.

The framework's generated setters lower `validation_rules` into INLINE checks (the
emitted body is sandbox-safe stdlib-only, so it can't import this module). This
module is the same logic exposed as an importable helper for hand-written tools —
and the shared reference implementation the emitted `luhn` check mirrors. Keep the
two in lockstep: `packages/flows/tests/test_luhn.py` asserts the generated setter
and `luhn_valid` agree, so a drift fails the suite.
"""

from __future__ import annotations

import re


def luhn_valid(number: str) -> bool:
  """True iff `number` passes the Luhn (mod-10) checksum used by payment cards.

  Non-digits (spaces, dashes) are stripped first, so "4242 4242 4242 4242" and
  "4242-4242-4242-4242" both validate. This is a FORMAT/typo gate only — it catches
  every single-digit error and almost all adjacent transpositions — NOT a check that
  the card is real, active, or funded (that needs an issuer/gateway authorization).

  Args:
    number: The raw card number as spoken/typed (may contain separators).

  Returns:
    True if the digit string is 12–19 digits and its Luhn checksum is 0 mod 10.
  """
  # Type guard: only a string or int is a meaningful card number. A bool
  # (str(True)=="True") or a list/dict must not be str()-coerced into digits.
  if not isinstance(number, (str, int)) or isinstance(number, bool):
    return False
  # `[^0-9]` (not `\D`, which keeps Unicode digits like Arabic-Indic ١٢٣ that would
  # corrupt `ord(ch) - 48`) — strip to ASCII digits only. Length gate before the
  # loop so an absurdly long input is rejected without summing every digit.
  digits = re.sub(r"[^0-9]", "", str(number))
  if not 12 <= len(digits) <= 19:
    return False
  total, alt = 0, False
  for ch in reversed(digits):
    n = ord(ch) - 48
    if alt:
      n *= 2
      if n > 9:
        n -= 9
    total += n
    alt = not alt
  return total % 10 == 0
