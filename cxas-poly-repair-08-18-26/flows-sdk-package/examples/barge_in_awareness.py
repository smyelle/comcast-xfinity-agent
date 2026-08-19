"""Barge-in awareness: keep what the caller missed, and don't derail on "mhmm".

On a real call people talk over the agent. The platform cuts the agent's speech when they
do — always, whatever the app is configured with — and unless the app asks to be told, the
agent carries on believing it delivered the whole line.

A four-part disclosure makes that visible, because the announce cascade speaks every
announce it can reach as ONE response. A caller who says "mhmm" three seconds in hears
part of the first line and NOTHING of the other three, and all four are recorded as
delivered. Nothing in the transcript shows it: the text was complete, only the audio was
cut. It is invisible to every text-channel test.

This app demonstrates the three primitives that fix it:

  * `flows.repair(...)` on an announce — replay the parts the caller never reached. Parts
    after the cut come back verbatim; only the part that was cut mid-way needs any
    guessing, and `mode="parts"` avoids even that by restarting that one sentence.
  * `flows.continue_cues(...)` — "mhmm" is agreement, not an answer and not a stall. On by
    default; the policy is here only to widen the vocabulary.
  * `flows.on_interrupted(...)` — what to say when the caller genuinely cuts in. `{unheard}`
    is what they missed, and `say_unknown` covers the turns where that cannot be worked out
    confidently.

`App.barge_in_awareness` is True by default, so the config that makes any of this possible
is emitted without asking. The A/B control below turns it off to show the difference.

Build:  PYTHONPATH=src python -m examples.barge_in_awareness
        PYTHONPATH=src python -m examples.barge_in_awareness --control

Drive live: see BARGE_IN_VERIFY.md.
"""

import sys

import flows


@flows.tool(flow="signup")
def open_account(account_type: str = "", email: str = "") -> dict:
  """Open the account once the caller has heard the terms and given their details."""
  return {"success": True, "confirmation": "AC-4417", "account_type": account_type}


# The disclosure. FOUR announces, so the DAG cascades them into one spoken response —
# which is exactly the shape that loses everything after the interruption today.
#
# Each is its own announce (rather than one announce with four texts) because that is how
# a real agent grows: separate lines, separately conditional, separately reusable. It is
# also the harder case for repair, since the parts belong to different slots.
DISCLOSURE = [
    ("terms_intro", "Before I can open the account I need to read you a few terms."),
    ("terms_record", "Calls are recorded for training and quality."),
    ("terms_retain", "Your personal data is retained for ninety days after the call."),
    ("terms_optout", "You can opt out of marketing at any time by calling us back."),
]


def build(control: bool = False) -> flows.App:
  """The demo app. `control=True` is the A/B arm with the feature switched off."""
  signup = flows.Flow("signup", root_agent="Signup_Agent",
                      bootstrap={"welcome_slot": "welcome"})

  # `preempt=True` on every one of these. An announce's `texts` are DROPPED unless it
  # preempts — the slot still fills, but the line is never spoken. A disclosure that must
  # be read verbatim is exactly the case that cannot survive being reworded by the model,
  # and a demo whose lines never reach the caller proves nothing about recovering them.
  signup.add(flows.announce(
      "welcome", ["Thanks for calling Northwind. I can open a new account for you."],
      shared=True, preempt=True))

  for name, line in DISCLOSURE:
    # The ONE thing the treatment arm adds to each announce.
    repair = None if control else flows.repair(
        mode="parts", lead_in="Sorry — as I was saying,", max_repairs=2)
    signup.add(flows.announce(name, [line], preempt=True,
                              **({} if repair is None else {"repair": repair})))

  signup.add(
      flows.user_slot("account_type", "Would you like a checking or a savings account?"),
      flows.user_slot("email", "And what's the best email address for you?"),
      flows.result_slot("confirmation", "open_task"),
  )
  signup.task(flows.task(
      "open_task", "open_account", ["account_type", "email"], "confirmation",
      out_key="confirmation", requires=["account_type", "email"], terminal=True,
      then_say="You're all set — your confirmation is {confirmation}."))

  if not control:
    # Widen the default vocabulary for this domain, and say what to do when the caller
    # genuinely interrupts rather than merely agreeing.
    signup.set("continue_cues", flows.continue_cues(
        extra=["yeah yeah", "keep going", "i'm with you", "sounds fine"]))
    signup.set("on_interrupted", flows.on_interrupted(
        say="Sorry — you may not have caught this: {unheard}",
        say_unknown="Sorry, let me go over that last part again.",
        min_unheard_chars=20))

  return flows.App(
      root_flow=signup,
      app_display_name=("Barge-in A/B CONTROL (feature off)" if control
                        else "Barge-in awareness demo"),
      # The demo runs on composite because that is where the behavior was measured
      # (ces-probes 161/162). `App` otherwise defaults to gemini-3.1-flash-live.
      model="gemini-composite-v1",
      # The control arm also gives up the platform's interruption report, which is what
      # makes it the honest before-picture rather than a half-disabled treatment.
      barge_in_awareness=not control,
  )


# The deployable app, at module level so the eval harness and the coverage gate can both
# find it. `build(control=True)` is the A/B arm and is not itself an example.
app = build()


if __name__ == "__main__":
  is_control = "--control" in sys.argv
  out = "barge_in_control_app" if is_control else "barge_in_awareness_app"
  emitted = build(control=True) if is_control else app
  result = flows.build_app(emitted, out, overwrite=True)
  if not result.ok:
    raise SystemExit(result.validation.errors if result.validation else result.error)
  print(f"emitted {out}  (barge_in_awareness={emitted.barge_in_awareness})")
