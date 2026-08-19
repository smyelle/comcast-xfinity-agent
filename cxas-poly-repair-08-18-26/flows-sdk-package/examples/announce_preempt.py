"""`preempt` is what buys the announce's turn back for the MODEL — heard, not inspected.

A pharmacy refill line looks the prescription up and then has exactly one thing to say
about it. Which thing depends on the drug, and the two notices want opposite turn shapes:

  * routine refill  — "ready within two hours". An ASIDE. It should arrive folded into the
                      agent's own next sentence, and the agent should carry straight on:
                      ask how they want it, and take the answer if they already gave one.
  * controlled drug — "a pharmacist has to sign this off". A FULL STOP. The caller hears
                      that and nothing else; there is nothing left to ask, because the
                      refill is not happening on this call.

Both sit in the SAME position in the DAG, gated on the same `refill_class`. The only
difference between them is `preempt`, which makes this a controlled experiment you can
hear — and the thing being controlled is whether the MODEL gets to run at all.

  preempt=True  -> the announce rides INLINE on the directive and `before_model` returns
                   it straight to the caller. The model is skipped. Whatever else the
                   caller said this turn is never looked at, and no further tool fires.
  preempt=False -> the model runs. It renders the announce's `message` in its own words,
                   relays the next question, and can still call setters — so a value the
                   caller volunteered in the same breath is captured on the same turn.

That also decides which announce CHANNEL works. Verbatim `texts` only reach the caller on
a preempting turn (`after_model._extract_response_parts` keeps payload parts and drops
text ones), so the non-preempting notice speaks through `message` and the preempting one
through `texts`. Swap them and one of the two says nothing at all.

Driven live against the real platform, one deployed app, three conversations:

  A. routine, number only ("It's RX2291.")
     -> "O.K. Just so you know, your lisinopril refill will be ready for pickup within
         two hours. Will you be picking it up in store, or would you like it delivered?"
        The notice landed AND the turn carried on into the question.

  B. routine, everything in one breath ("It's RX2291, and I'll pick it up in store.")
     -> tools: set_prescription_number, look_up_prescription, set_pickup_method,
               queue_refill
        "You're all set — your refill is in the queue as Q-118."
        The turn ran the whole flow to completion, because the model still had a turn
        after the announce.

  C. controlled ("It's RX4417, and I'll pick it up in store.")
     -> tools: set_prescription_number, look_up_prescription        # and stop
        "That one needs a pharmacist's sign-off, so I can't refill it from here. I've
         flagged it for the pharmacy team and they'll call you back within the hour."
        Nothing else. The turn ends on the notice.

Which is what makes the key's DEFAULT a trap, and why `announce()` always emits it. The
engine reads `slot_def.get("preempt", True)`, so an announce that merely OMITS the key is
a PREEMPTING one, and nothing warns you. Rebuilt against a dropped key and redeployed, the
same two routine utterances come back wearing case C's behaviour:

  A (key omitted) -> "Work in that their lisinopril refill will be ready for pickup within
                      two hours. Will you be picking it up in store, or would you like it
                      delivered?"
     The model never ran, so the caller is read the DIRECTIVE — the instruction written
     for the model, in the third person, out loud.

  B (key omitted) -> tools: set_prescription_number, look_up_prescription        # and stop
                     (same directive read out, asking for a pickup method)
     "I'll pick it up in store" was thrown away and the caller is asked a question they
     had already answered. C is unchanged, since `preempt=True` was always emitted.

Two things in this flow are load-bearing for the experiment and easy to get wrong:

* `LookUpPrescription` has NO `then_say`. A task message preempts the turn on its own
  (`preempt = bool(task_msg) or any_announce_preempt`), which would flatten both branches
  onto the stop-dead shape and hide the announce's contribution entirely.
* `pickup_method` is gated on `refill_class`, NOT on either notice. Requiring a notice
  would defer the question by construction, which is the very thing preempting does — the
  comparison only means something while the question is free to land on the same turn.

Build + validate offline:
    python -m examples.announce_preempt          # emits ./announce_preempt_app
"""

from pydantic import BaseModel, Field

import flows


class PrescriptionRecord(BaseModel):
  """What the pharmacy system knows about one prescription."""

  drug_name: str = Field(description="Dispensing name of the drug.")
  refill_class: str = Field(
      description="'routine' (refills straight through) or 'controlled' "
                  "(needs a pharmacist's sign-off).")
  success: bool = Field(default=True, description="Whether the lookup found it.")


@flows.tool(flow="refill")
def look_up_prescription(prescription_number: str) -> PrescriptionRecord:
  """Look a prescription up on file and say how it is allowed to be refilled."""
  # Prescriptions a pharmacist must personally release. Inline, not a module-level
  # constant: only the function's own source is shipped to the platform, so a global
  # it closes over is a NameError at the far end (and reads as "An error occurred").
  controlled = {"RX4417"}
  number = prescription_number.upper().replace("-", "").replace(" ", "")
  if number in controlled:
    return PrescriptionRecord(drug_name="oxycodone", refill_class="controlled")
  return PrescriptionRecord(drug_name="lisinopril", refill_class="routine")


@flows.tool(flow="refill")
def queue_refill(prescription_number: str, pickup_method: str) -> dict:
  """Put the refill in the pharmacy's queue."""
  return {"queue_number": "Q-118", "success": True}


refill = flows.Flow("refill", root_agent="Pharmacy_Agent",
                    bootstrap={"reset_on_complete": True})

refill.add(
    flows.user_slot("prescription_number",
                    ask="What's the prescription number you'd like to refill?"),
    flows.result_slot("refill_class", "LookUpPrescription"),
    flows.result_slot("drug_name", "LookUpPrescription"),
    # THE ASIDE. `preempt=False`, so the model still runs: it folds this notice into
    # its own sentence, asks `pickup_method`, and — on the same turn — picks up an
    # answer the caller already volunteered. MODEL-RENDERED on purpose: verbatim
    # `texts` only reach the caller on a PREEMPTING turn (after_model keeps payload
    # parts and drops text ones), so a non-preempting announce that speaks through
    # `texts` says nothing at all.
    flows.announce(
        "ready_notice",
        [],
        message="Work in that their {drug_name} refill will be ready for pickup"
                " within two hours.",
        requires=["refill_class"],
        condition=flows.eq("refill_class", "routine"),
        preempt=False,
    ),
    # THE FULL STOP. `preempt=True`, so this rides inline on the directive, the model is
    # skipped, and the turn ends on the notice. Same position, same gate, opposite key.
    # VERBATIM `texts` here, which is the channel a preempting turn actually delivers —
    # and the right one anyway for a line about what the pharmacy will and won't do.
    flows.announce(
        "signoff_notice",
        ["That one needs a pharmacist's sign-off, so I can't refill it from here."
         " I've flagged it for the pharmacy team and they'll call you back within"
         " the hour."],
        requires=["refill_class"],
        condition=flows.eq("refill_class", "controlled"),
        preempt=True,
    ),
    # Gated on the lookup, NOT on either notice — see the module docstring. Only the
    # routine branch reaches it; the controlled branch's notice is the last thing the
    # DAG has to offer, which is what leaves that turn with nothing to say after it.
    flows.user_slot("pickup_method",
                    ask="Will you be picking it up in store, or would you like it"
                        " delivered?",
                    requires=["refill_class"],
                    condition=flows.eq("refill_class", "routine")),
    flows.result_slot("queue_number", "QueueRefill"),
)

# No `then_say`: a task message preempts the turn by itself and would mask the announce.
refill.task("LookUpPrescription", "look_up_prescription", ["prescription_number"],
            "refill_class", out_key="refill_class",
            extra_outputs={"drug_name": "drug_name"})
refill.task("QueueRefill", "queue_refill", ["prescription_number", "pickup_method"],
            "queue_number", out_key="queue_number", terminal=True,
            condition=flows.eq("refill_class", "routine"),
            then_say="You're all set — your refill is in the queue as"
                     " {queue_number}.")

app = flows.App(
    root_flow=refill,
    app_display_name="flows-sdk-demo Announce Preempt",
    agent_instruction=(
        "You are a pharmacy refill agent. Follow the slot-filling framework directives "
        "exactly. Speak only what the framework gives you."
    ),
)


def _show_turn_shape() -> None:
  """Print what each notice does to its turn — the point of the example."""
  by_name = {s["name"]: s for s in refill.to_config()["slots"]}
  for name in ("ready_notice", "signoff_notice"):
    s = by_name[name]
    shape = ("stops the turn (model skipped)" if s["preempt"]
             else "rides along (model still asks the next question)")
    print(f"  {name:16} preempt={str(s['preempt']):5} {shape}")


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    flows.build_app(app, "./announce_preempt_app")
    print("built: ./announce_preempt_app")
    _show_turn_shape()
