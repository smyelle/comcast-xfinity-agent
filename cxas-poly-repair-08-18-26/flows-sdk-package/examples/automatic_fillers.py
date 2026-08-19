"""Letting the compiler find the filler you already wrote.

`filler_say` covers a wait. But authors keep writing the covering line in the wrong
place — at the front of `then_say`, which is spoken once the wait is already over:

    then_say="Thanks for holding. Your balance is {balance}."

The caller sits through the whole backend round trip in silence and is then thanked
for holding. `App(automatic_fillers=True)` turns on a build-time pass that moves that
first sentence to `filler_say`, so it rides the tool call instead:

    filler_say="Thanks for holding."          <- spoken AS the tool is called
    then_say="Your balance is {balance}."     <- spoken when it answers

Nothing is reworded and nothing is invented: the two halves rejoin to the authored
string byte for byte. Measured over real audio, this took the caller's dead air on the
firing turn from 6.30s to 0.99s — while the balance itself still landed at the same
moment (6.30s against 6.34s). It does not make the agent faster by any amount; it
makes the wait audible.

For a task, only the schedule changes. A slot's opener can also move to a LATER turn:
the engine arms a slot filler only on a turn it hands to the model, so a greeting or
announce that preempts pushes it to the next one. Driven here, "Okay." stopped
prefacing the question and started acknowledging the answer.

## What it will and will not move

A hoisted line can be DROPPED — the chat surface has no use for it, a pool entry may
be silence, the engine allows one per caller turn. So the pass only moves a sentence
it can prove carries nothing, by matching it against a closed list of acknowledgement
phrases. "Thanks for holding." moves. "Your card was declined." does not, however
short and fixed it looks: losing that sentence loses a fact. Nor does "All good." — a
gate built from allowed WORDS rather than whole phrases lets that one through, which
is why this one matches phrases.

Widen the vocabulary per app when your house style needs it:

    flows.App(..., automatic_fillers={"extra_ack": ["righto"]})

## Opting out

Off by default. Once on, a node opts out with `automatic_fillers=False`, and several
shapes are skipped automatically — `verbatim` copy, per-surface variants, component
tasks, fan-out legs, and anything that already sets `filler_say`. Run `flows lint` to
see which of your nodes had a hoistable opener that a rule blocked (FLV004).
"""

from __future__ import annotations

import flows


@flows.tool
def fetch_balance(account_number: str) -> dict:
  """Look up an account balance (a genuinely slow backend round trip).

  Args:
    account_number: The caller's account number.

  Returns:
    A dict carrying a success flag and the formatted balance.
  """
  # A real wait, not a mocked one: with an instant tool there is no silence to cover,
  # so a fakes harness cannot show this feature working at all. Imported inside the
  # body because only the function itself is inlined into the emitted tool.
  import time
  time.sleep(3)
  return {"success": True, "balance": "42 dollars and 10 cents"}


@flows.tool
def close_account(account_number: str) -> dict:
  """Close an account.

  Args:
    account_number: The caller's account number.

  Returns:
    A dict carrying a success flag and the confirmation code.
  """
  import time
  time.sleep(3)
  return {"success": True, "code": "CX-8841"}


billing = flows.Flow("billing", root_agent="billing_agent")

billing.add(
    # The ask opens with an acknowledgement, so it is hoisted too — spoken as a partial
    # preempt while the model composes the question, in the same turn.
    flows.user_slot("account_number", "Okay. What's your account number?"),
    flows.result_slot("balance", "read_balance"),
    flows.user_slot("confirm_close", "Got it. Do you want me to close the account?"),
    flows.result_slot("close_code", "do_close"),
)

# HOISTED. "Thanks for holding." moves to filler_say; the rest stays in then_say and
# is spoken when the tool answers. Only those two fields change — the turn's shape is
# the engine's business and the pass leaves it alone.
billing.task(
    "read_balance", "fetch_balance", ["account_number"], "balance",
    out_key="balance",
    then_say="Thanks for holding. Your balance is {balance}.",
)

# NOT HOISTED. "Your account is now closed." is short and fixed, but it reports what
# the tool did. Spoken before the call it would be a claim the backend has not made.
billing.task(
    "do_close", "close_account", ["account_number"], "close_code",
    out_key="code", terminal=True,
    then_say="Your account is now closed. Your confirmation code is {close_code}.",
)

app = flows.App(
    root_flow=billing,
    app_display_name="Latency hiding demo",
    automatic_fillers=True,
)


def _show_hoists() -> None:
  """Print what the pass moved, and prove the halves rejoin to the authored line."""
  from flows.authoring import build

  authored = {t["name"]: t.get("then_say") for t in billing.to_config()["tasks"]}
  cfg = build._assemble(app)[0]["billing"]

  for task in cfg["tasks"]:
    filler = task.get("filler_say")
    print(f"  task {task['name']}")
    if not filler:
      print(f"    not hoisted   then_say={task['then_say']!r}")
      continue
    print(f"    filler_say    {filler!r}   <- rides the tool call")
    print(f"    then_say      {task['then_say']!r}")
    assert f"{filler} {task['then_say']}" == authored[task["name"]]
    print("    rejoins to the authored line byte for byte")

  slot = next(s for s in cfg["slots"] if s["name"] == "account_number")
  print(f"  slot {slot['name']}")
  print(f"    filler_say    {slot['filler_say']!r}   <- partial preempt")
  print(f"    ask           {slot['ask']!r}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./automatic_fillers_app")
    print("built: ./automatic_fillers_app")
    _show_hoists()
