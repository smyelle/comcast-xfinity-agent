"""`since` on a SLOT is what stops the model answering its own question.

An agent that OFFERS something has a problem the moment it offers. The rung that speaks
"shall I rebook it?" latches a slot as it speaks, so a plain `filled` gate on the slot
that captures the answer is already open on that same turn — and the model, holding both
the question and the setter, fills it in. The caller is never asked. They hear the offer
and the confirmation in one breath, about a decision they did not make.

`since_turns` is the gate that closes that. It measures CALLER turns, so on the offer's
own turn the slot stays shut and the model has nothing to fill; on the next turn it opens
and the caller's "yes please" is captured by its cue like any other answer.

    flows.intent_slot("rebook", {"YES": [...], "NO": [...]},
                      condition=flows.since("rebook_offered"))

WHERE THE CONDITION IS READ, and why that is the whole point. A condition on a TASK is
evaluated during the DAG walk. A condition on a SLOT is evaluated much earlier, by the cue
matcher, which asks whether a slot is active before letting an utterance fill it. Those are
different points in a turn, and a turn-relative gate has to mean the same thing at both.

DRIVEN LIVE against the real platform, one deployed app per arm, same three utterances.
Verbatim from the drive; SINCE_SLOT_GATE_VERIFY.md has the commands and the app ids.

  TREATMENT (`flows.since`) — one question, asked once.
     caller: "my parcel never turned up"
     agent:  "I'm sorry to hear that your parcel hasn't arrived. Could you please
              provide your tracking number so I can look into this for you?"
     caller: "seven seven three"
     agent:  "That one's back at the depot. Shall I book a redelivery for tomorrow?"
     caller: "yes please"
     agent:  "Done — it's booked for tomorrow."

  CONTROL (`filled: True`, identical but for that one key) — same utterances.
     caller: "seven seven three"
     agent:  "That one's back at the depot. Shall I book a redelivery for tomorrow?
              Would you like me to book that redelivery?"
     caller: "yes please"
     agent:  "Done — it's booked for tomorrow."
     The offer lands, and because the slot is already open on that turn the engine puts
     its own ask straight after it. The caller is asked the same question twice in one
     breath, in two different wordings.

The control is worth keeping in the file rather than describing, because the failure is
not an error: it is a well-formed turn that asks one question twice, which no assertion
about tools or slots would catch.

Build + validate offline:
    PYTHONPATH=src python -m examples.since_slot_gate            # treatment
    PYTHONPATH=src python -m examples.since_slot_gate --control  # the A/B control
"""

import sys

from pydantic import BaseModel, Field

import flows

CONTROL = "--control" in sys.argv


class ParcelStatus(BaseModel):
  """Where a parcel is, and whether it can be sent out again."""

  location: str = Field(description="Where the parcel is now, in plain words.")
  redeliverable: str = Field(description="'yes' if it can go out again tomorrow.")
  success: bool = Field(default=True, description="Whether the parcel was found.")


@flows.tool(flow="redelivery")
def look_up_parcel(tracking_number: str) -> ParcelStatus:
  """Find a parcel and say whether it can be sent out again."""
  # Inline rather than a module-level constant: only the function's own source ships to
  # the platform, so a global it closes over is a NameError at the far end.
  if "773" in tracking_number.replace(" ", ""):
    return ParcelStatus(location="back at the depot", redeliverable="yes")
  return ParcelStatus(location="out for delivery", redeliverable="no")


@flows.tool(flow="redelivery")
def book_redelivery(tracking_number: str) -> dict:
  """Book the parcel out again for the next working day."""
  return {"success": True, "booked": "tomorrow"}


redelivery = flows.Flow("redelivery", root_agent="Redelivery_Agent",
                        bootstrap={"reset_on_complete": True})

# The gate, and the only difference between the arms.
#
# TREATMENT: the offer's latch, read through `since_turns` — true from the turn AFTER the
# offer was spoken. CONTROL: the same latch read with `filled`, which is true the instant
# the offer speaks, on that turn, while the model still holds the floor.
ANSWERABLE = ({"slot": "rebook_offered", "filled": True} if CONTROL
              else flows.since("rebook_offered"))

redelivery.add(
    flows.user_slot("tracking_number", ask="What's the tracking number?"),
    flows.result_slot("location", "LookUpParcel"),
    flows.result_slot("redeliverable", "LookUpParcel"),

    # The latch the offer writes as it speaks. An `event_slot` is the never-asked,
    # engine-written flag this wants, and the validator rejects a condition naming a slot
    # the flow never declares.
    flows.event_slot("rebook_offered"),
    flows.result_slot("booked", "BookRedelivery"),

    # THE OFFER. `sets` writes the latch in the same breath as the line, so there is no
    # tool and no round trip between asking and the gate being armed — which is exactly
    # what makes the same-turn answer possible in the control arm.
    flows.announce(
        "OfferRedelivery",
        ["That one's {location}. Shall I book a redelivery for tomorrow?"],
        condition={"slot": "redeliverable", "eq": "yes"},
        requires=["redeliverable"],
        sets={"rebook_offered": "true"},
        preempt=True),

    # THE ANSWER, and the slot this demo is about. Its condition is read by the CUE
    # MATCHER, before any task is considered, so this is the gate that decides whether
    # "yes please" is heard as the caller's answer or supplied by the model.
    flows.intent_slot(
        "rebook",
        {"YES": ["yes please", "yes", "go ahead", "please do", "book it"],
         "NO": ["no thanks", "no", "not now", "leave it"]},
        setter="set_rebook",
        # The re-ask wording, and it has to be here even though the announce above has
        # already put the question: a slot with no `ask` leaves the engine's next-step
        # directive empty on the turn it opens, and the model fills that silence with a
        # question of its own — driven, it invented one asking for the caller's name.
        ask="Would you like me to book that redelivery?",
        cue_priority="first",
        condition=ANSWERABLE),
)

# The consuming rung carries the gate too. A value can reach a slot without passing the
# slot's own gate — carried in from an earlier flow, seeded from a variable, cue-matched
# on a turn the gate happened to be open — and this rung is what refuses to act on one.
redelivery.task(flows.task(
    "BookRedelivery", "book_redelivery", ["tracking_number"], "booked",
    requires=["rebook"],
    condition={"all": [{"slot": "rebook", "eq": "YES"}, ANSWERABLE]},
    then_say="Done — it's booked for tomorrow."))

redelivery.task(flows.task(
    "LookUpParcel", "look_up_parcel", ["tracking_number"], "location",
    extra_outputs={"redeliverable": "redeliverable"}))

app = flows.App(root_flow=redelivery,
                app_display_name="Since slot gate" + (" CONTROL" if CONTROL else ""),
                model="gemini-composite-v1")


def _show_gate() -> None:
  """Print the emitted gate, so the arms are distinguishable before either is pushed."""
  cfg = redelivery.to_config()
  arm = "CONTROL (filled)" if CONTROL else "TREATMENT (since_turns)"
  print(f"\n  arm: {arm}")
  for slot in cfg["slots"]:
    if slot["name"] == "rebook":
      print(f"  rebook slot gate : {slot.get('condition')}")
  for task in cfg["tasks"]:
    if task["name"] == "BookRedelivery":
      print(f"  consuming rung   : {task.get('condition')}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    out = "./since_slot_gate_control_app" if CONTROL else "./since_slot_gate_app"
    flows.build_app(app, out)
    print(f"built: {out}")
    _show_gate()
