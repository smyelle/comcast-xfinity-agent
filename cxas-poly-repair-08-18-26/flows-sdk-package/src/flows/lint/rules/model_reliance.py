"""FLM — model reliance & determinism."""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..context import LintContext
from ..models import Category, Finding, Location
from ..registry import Rule, rule
from ...config.models import NodeAnchor

def _condition_mentions(cond: Any, name: str) -> bool:
  """True if a condition READS the slot `name` (string or dict condition form).

  flows conditions are lambdas that reference a slot as `f.get('name')` / `f['name']`
  (what `has`/`eq`/`ne` compile to). Matching the REFERENCE, not a bare word, is what
  distinguishes a slot read from a VALUE literal: `f.get('coverage') == 'active'`
  mentions slot `coverage` but NOT a slot named `active`, and a genuine
  `f.get('active')` still counts. The quote requirement also prevents `procedure`
  from matching `f.get('procedure_code')`.
  """
  if not cond:
    return False
  text = cond if isinstance(cond, str) else str(cond)
  ref = rf"f\s*(?:\.get\(\s*|\[\s*)['\"]{re.escape(name)}['\"]"
  return re.search(ref, text) is not None


def _has_steering(task: dict) -> bool:
  """Does this task steer the proceed turn deterministically?"""
  return bool(task.get("then_directive") or task.get("then_say")
              or task.get("then_response") or task.get("preempt_then_say"))


@rule(
    code="FLM001",
    category=Category.MODEL_RELIANCE,
    severity="warning",
    title="multi-outcome branch with no proceed-turn directive",
    docs="FLM001",
)
class MultiOutcomeNoDirective(Rule):
  """A slot that fans into >=2 conditional outcomes whose branch tasks give the
  model no `then_directive`/verbatim steering, so on the proceed turn the model
  must infer the next question and tends to improvise the wrong branch's.

  This is issue #596 case 4. On a proceed turn the engine computes the
  deterministic next-question message but does not deliver it (it is stashed only
  as an empty-render backstop, `_render_fallback`); `then_directive` is the task
  fix. There is no slot->slot equivalent yet — see issue #599 — so for a slot->slot
  proceed this rule can only flag, not fully remedy.

  Heuristic (default `warning`, suppressible): fires when a branch slot has >=2
  outcome-gated tasks and none of them steers the proceed turn.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for cid in ctx.config_ids():
      tasks = ctx.tasks(cid)
      for i, slot in enumerate(ctx.slots(cid)):
        name = slot.get("name")
        if not name:
          continue
        gated = [t for t in tasks if _condition_mentions(t.get("condition"), name)]
        is_intent = slot.get("kind") == "intent" and len(slot.get("option_cues") or {}) >= 2
        if len(gated) < 2 and not (is_intent and len(gated) >= 1):
          continue
        if any(_has_steering(t) for t in gated):
          continue  # at least one branch steers the proceed turn
        outcomes = len(slot.get("option_cues") or {}) or len(gated)
        yield self.finding(
            message=(
                f"Slot {name!r} branches into {outcomes} outcomes but none of the "
                f"{len(gated)} tasks gated on it provides then_directive/verbatim "
                "steering, so on the proceed turn the model must infer the next "
                "question and may improvise the wrong branch. Add a then_directive "
                "on the branch task(s) (for a slot->slot proceed a slot-level "
                "directive is not available yet - see issue #599)."),
            location=Location(config_id=cid, node=name, json_path=f"slots[{i}]"),
            anchor=NodeAnchor(kind="slot", ref=name, field="condition"),
            rationale=("Proceed turns leave the next question to the model; after a "
                       "multi-outcome branch the model defaults to the dominant "
                       "instruction path unless a directive steers it."),
            related=["issue #599 (slot-level then_directive)"],
            fix_id="add_then_directive",
        )
