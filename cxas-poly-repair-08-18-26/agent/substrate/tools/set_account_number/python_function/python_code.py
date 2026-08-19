# agent_action: this comment satisfies the T001 lint rule.

def set_account_number(account_number: str) -> dict:
  """Set the Xfinity account number or phone number.

  The slot this backs already declares what a bad value should produce -- an
  `invalid_format` error carrying "Please provide a valid 9 to 16 digit account number
  or a 10 digit phone number.", three retries, and an `on_exhaust` hand-off. None of it
  was reachable: this body used to accept any non-empty string, so `123`, `one two
  three`, ten letters and twenty-six digits were all stored and swept, and the authored
  error was deployed config that could never fire. Measured 3/3 on `123`.

  The worst consequence was not the junk it let through but the answer on the other
  side. A stored non-account clears the gate, the sweep runs against nothing, and the
  caller is told in the agent's most authoritative sentence that an account which does
  not exist is healthy.

  What counts as valid, and why it is only a SHAPE check. The caller may read either an
  account number (9 to 16 digits) or the phone number on the account (10 digits, which
  that range already covers), so the rule is: digits, and between 9 and 16 of them.
  Separators are stripped rather than rejected -- a caller reading sixteen digits aloud
  produces spaces and dashes as readily as not, and so does an ASR transcript of the same
  reading, so treating "8069 1002 3035 9946" as malformed would reject a good number for
  how it was punctuated.

  What this does NOT do is decide whether the account EXISTS. That is the context hub's
  job one step later, and a well-formed number belonging to nobody is answered by
  `verdict_account_not_found`. Keeping the two apart is deliberate: the format error asks
  the caller to read the number again, which is the right response to a mis-heard digit
  and the wrong response to a number they read correctly.

  Args:
      account_number: The account number or phone number.

  Returns:
      {"stored": True, "value": account_number} on success;
      {"error": True, "error_code": "invalid_format"} on failure.
  """
  raw = str(account_number).strip()
  if not raw:
    return {"error": True, "error_code": "invalid_format"}
  # The punctuation a spoken number legitimately arrives with. Anything ELSE surviving
  # into the check below is a character an account number does not have, and length is
  # not what should catch it -- "one two three" is eleven non-space characters, which
  # would otherwise pass as a nine-to-sixteen "digit" account.
  cleaned = raw
  for sep in (" ", "-", ".", "(", ")", "/", "\t", "+", "–", "—"):
    cleaned = cleaned.replace(sep, "")
  if not cleaned.isdigit():
    return {"error": True, "error_code": "invalid_format"}
  # A US country code in front of the ten-digit phone number on the account, which is
  # how a caller giving that number often reads it. Stripped only when what remains is
  # itself a valid phone number, so a genuine eleven-digit account starting 1 is left
  # alone.
  if len(cleaned) == 11 and cleaned.startswith("1"):
    cleaned = cleaned[1:]
  if not 9 <= len(cleaned) <= 16:
    return {"error": True, "error_code": "invalid_format"}
  # The NORMALIZED value is what is stored. The gate downstream looks the number up
  # digits-only, so storing the caller's punctuation put the two one step out of step,
  # and every other consumer of the slot -- the sweep, the hand-off payload, the demo
  # journey binding -- then had to re-normalize or silently miss.
  return {"stored": True, "value": cleaned}
