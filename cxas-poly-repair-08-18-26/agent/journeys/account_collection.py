"""Ask for the account number, and keep asking patiently if nothing comes back."""

import build_config
import flows
import scripts


def _account_ask():
  """The account ask, greeting-gated. `{welcome_lead}` is filled by before_agent per call
  (greeting on a direct opening turn, bare otherwise). The `--skip-greeting` BUILD flag
  bakes it OFF instead: the placeholder is resolved to the bare lead-in at build time, so
  even a direct call to that build never greets and nothing depends on the runtime seed."""
  if build_config.current().skip_greeting:
    return scripts.ASK_ACCOUNT_NUMBER.replace("{welcome_lead}",
                                              scripts.WELCOME_LEAD_HANDOFF)
  return scripts.ASK_ACCOUNT_NUMBER


# No `reason_for_call` slot: the router's own gate slot owns the opening turn, and a
# second opening question glues two greetings into one breath.
def slots():
  """Ask for the account number, and keep asking patiently if nothing comes back."""
  return [
      flows.user_slot(
          "accountNumber",
          ask=_account_ask(),
          # `verbatim`: the engine speaks the ask exactly, ahead of the model. This is the
          # opening turn, and the model, handed a warm persona and no prior context, opened
          # it with "Welcome to Xfinity" however firmly the instruction said not to. That
          # improvised greeting is the whole thing the `skip_greeting` flag exists to drop,
          # and an instruction cannot drop it reliably -- so the ask is made deterministic
          # and the greeting rides `{welcome_lead}` (see _account_ask), which a hand-off or
          # the build flag gates off. A phone/account prompt loses nothing read verbatim.
          verbatim=True,
          # No `filler_say`: the sweep is dispatched as soon as the account lands, so its
          # own filler covers this same turn and a second one stacks two acknowledgements
          # into one breath.
          hint="Xfinity account number or phone number",
          setter="set_account_number",
          validation={
              "max_retries": 3,
              "errors": {
                  # A LADDER, not one sentence three times. `errors` takes a list per
                  # code, one rung per attempt, clamped to the last, so a caller who
                  # is struggling hears three different things instead of the same
                  # sentence read back at them. Keyed per code rather than through
                  # `validation.reprompts`, because reprompts index on attempt ALONE
                  # and would throw away which failure this was.
                  #
                  # Rung 1 says what was wrong with what it heard, so the caller knows
                  # what to change. Rung 2 changes the ROUTE rather than repeating the
                  # request: a phone number is ten digits the caller knows by heart,
                  # and it is the one they are most likely to get through cleanly.
                  # Rung 3 asks for pace, which is the last thing left to try before
                  # the hand-off below.
                  "invalid_format": [
                      "That didn't sound like a full account number. It's 9 to 16 "
                      "digits, or you can give me the 10 digit phone number on the "
                      "account.",
                      "I'm still not getting it. Let's try the phone number on the "
                      "account instead. That's 10 digits.",
                      "One more try. Read me the account number slowly, one digit at "
                      "a time.",
                  ]
              },
              "on_exhaust": {
                  "say": (
                      "I'm having trouble finding your account. Let me connect you "
                      "with someone who can help."
                  ),
                  "then": {"tool": "verdict_account_block"},
              },
          },
      )
  ]
