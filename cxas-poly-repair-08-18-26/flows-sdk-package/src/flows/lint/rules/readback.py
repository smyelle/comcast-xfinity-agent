"""FLC140 — a readback gate the caller cannot correct.

The engine hides every value setter while a readback is pending, on purpose: its own
comment says "corrections route through the correction tool". So a flow that reads a
value back but declares no `correction_tool` leaves the model with no tool that can
record a correction.

A bare "no" still works — that is `reject_pending`, and the slot re-asks. What breaks is
"no, make it Friday", which is how people actually answer. Driven live against a
deployed booking agent:

    agent > Just to make sure I've got that right, you're coming in on monday?
    caller> no, make it Friday
    agent > Just to confirm — coming in on monday. Is that correct?
    caller> yes
    agent > You're all set. Your confirmation number is BR4482.

The caller said Friday and was booked for Monday. No exception, no fallback, no trace
signal — the correction simply had nowhere to go. `validate`, `lint`, `emit`, `check`
and the deploy were all clean on that config.
"""

from __future__ import annotations

from typing import Iterable

from ..context import LintContext
from ..models import Category, Finding, Location
from ..registry import Rule, rule
from ...config.models import NodeAnchor


def _reads_back(slot: dict) -> bool:
  """True when this slot triggers a confirmation gate."""
  return bool(slot.get("requires_readback") or slot.get("readback"))


@rule(
    code="FLC140",
    category=Category.CONVERSATION,
    severity="error",
    title="readback with no correction_tool silently discards a caller's correction",
    docs="FLC140",
)
class ReadbackWithoutCorrectionTool(Rule):
  """A slot reads its value back, and the config declares no `correction_tool`.

  Error rather than warning, on the strength of the failure: the wrong value is
  committed and the caller is told it succeeded. A defect that produces a confident
  wrong answer costs more than one that fails loudly, and this one reaches the customer
  — it books the wrong day, ships to the wrong address, pays the wrong account.

  The fix is one line at the top of the config. It is only missing because nothing ever
  said it was needed: the readback docs describe the confirmation, not the correction.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for cid in sorted(ctx.configs):
      config = ctx.configs.get(cid) or {}
      if config.get("correction_tool"):
        continue
      reading = [s for s in ctx.slots(cid) if _reads_back(s)]
      if not reading:
        continue
      names = ", ".join(sorted(s.get("name", "?") for s in reading))
      first = reading[0].get("name", "?")
      yield self.finding(
          message=(
              f"{len(reading)} slot(s) read a value back ({names}) but this config "
              f"declares no correction_tool. The engine hides every setter while a "
              f"readback is pending, so a caller who answers \"no, make it Friday\" has "
              f"no tool that can record Friday — the correction is dropped and the "
              f"original value is committed. A bare \"no\" still re-asks. Add "
              f"correction_tool='set_slot_change' to the config."),
          location=Location(config_id=cid, node=first, json_path="correction_tool"),
          anchor=NodeAnchor(kind="field", ref=first, field="correction_tool"),
          rationale=("Driven live: a caller who corrected the day at the gate was booked "
                     "for the original day and told it succeeded."),
          fix_id="declare_correction_tool",
      )
