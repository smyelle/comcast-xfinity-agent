# pylint: disable=g-doc-args,g-doc-return-or-yield,g-docstring-missing-newline,g-no-space-after-docstring-summary,g-short-docstring-punctuation
"""Hand the conversation off to a human / live agent (escalation)."""


def transfer_to_human(reason: str = "") -> dict[str, object]:
  """Transfer to a human agent. Call when the user asks to speak to a
  person, human, agent, representative, or operator."""
  return {"stored": True, "value": True, "escalated": True, "reason": reason}
