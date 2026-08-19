"""Setter for the mandatory per-turn intent classification (intent-first Pass A).

FRAMEWORK CODE -- shared across all agents using the slot-filling engine in
intent-first mode.

On every in-flow user turn the engine first runs a Pass-A classification turn:
the framework SI is rewritten to a classifier prompt and every tool except this
one is hidden, so the model's only move is to call classify_turn_intent with the
single intent that best describes the user's latest message. The intake handler
(slot_intake._intake_classify_turn_intent) records the gate marker
(_pending_intent) plus, for non-`continue` intents, the same control state the
opportunistic set_intent_changed setter produces (_classified for transitions,
_correction_recollect for corrections), so the existing Pass-B machinery routes
it unchanged.

The set of valid intent labels is NOT fixed here: it is config-derived and listed
in the classifier instructions the engine builds for each turn (it depends on the
configured flow_types and slots). This tool stays taxonomy-agnostic — it accepts
whatever single label the classifier prompt offered, normalizes it, and lets the
engine own authoritative routing.
"""

from typing import Annotated, Any

from pydantic import Field


def classify_turn_intent(
    intent: Annotated[
        str,
        Field(
            description=(
                "Exactly one intent label for the user's latest message, chosen"
                " from the labels enumerated in the classifier instructions for"
                " THIS turn. Emit the label verbatim and nothing else."
            )
        ),
    ],
) -> dict[str, Any]:
  """Classify the user's latest message into a single turn intent.

  Args:
    intent: One taxonomy label (see the field description).

  Returns:
    Dict with stored=True (setter-shaped) and the normalized intent label.
  """
  return {"stored": True, "value": True, "intent": str(intent).lower().strip()}
