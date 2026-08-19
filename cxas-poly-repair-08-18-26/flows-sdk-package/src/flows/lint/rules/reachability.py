"""FLR — reachability & flow control."""

from __future__ import annotations

from typing import Any, Iterable

from ..context import LintContext, _slot_is_user_askable
from ..models import Category, Finding, Location
from ..registry import Rule, rule
from ...config.models import NodeAnchor


def _clear_slots(on_failure: dict) -> set[str]:
  """`clear_slots` is a list, or a {error_code: [slots]} dict — union the names."""
  cs = on_failure.get("clear_slots")
  if isinstance(cs, list):
    return {s for s in cs if isinstance(s, str)}
  if isinstance(cs, dict):
    out: set[str] = set()
    for v in cs.values():
      if isinstance(v, list):
        out.update(s for s in v if isinstance(s, str))
    return out
  return set()


def _task_input_names(task: dict) -> set[str]:
  inputs = task.get("inputs")
  if isinstance(inputs, dict):
    names = set(inputs.keys())
  elif isinstance(inputs, list):
    names = {s for s in inputs if isinstance(s, str)}
  else:
    names = set()
  names.update(s for s in (task.get("requires") or []) if isinstance(s, str))
  return names


@rule(
    code="FLR001",
    category=Category.REACHABILITY,
    severity="error",
    title="on_exhaust open_slot has no reachable next question",
    docs="FLR001",
)
class ExhaustOpenSlotDeadEnd(Rule):
  """A task exhaust that opens a slot from which no further question can be asked.

  This is issue #596 case 2. At runtime the engine arms `open_slot`, calls
  `_find_next_question`, and when nothing is reachable it degrades to
  `on_exhaust.say` (default "An error occurred.") and logs
  `task_exhaust_open_slot_unreachable`. The blessed validator only checks that
  `open_slot` names a declared slot, not that a next question is reachable.

  Fires conservatively (precision over recall): only when `open_slot` is not
  itself a user-askable slot, no `say` is set (so the caller really would hear the
  generic error), and no other user-askable slot remains after the exhaust.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for cid in ctx.config_ids():
      askable = {s["name"] for s in ctx.user_askable_slots(cid) if s.get("name")}
      slot_map = ctx.slot_map(cid)
      for i, task in enumerate(ctx.tasks(cid)):
        of = task.get("on_failure")
        if not isinstance(of, dict):
          continue
        ex = of.get("on_exhaust")
        if not isinstance(ex, dict):
          continue
        opened = ex.get("open_slot")
        if not isinstance(opened, str) or not opened:
          continue
        if opened not in slot_map:
          continue  # blessed validator flags an undeclared open_slot; not our job
        if ex.get("say"):
          continue  # the author controls the terminal line; not the generic error
        if _slot_is_user_askable(slot_map[opened]):
          continue  # arming an askable slot IS the next question -> no dead-end
        remaining = (askable - _task_input_names(task)) | (
            _clear_slots(of) & askable)
        remaining.discard(opened)
        if remaining:
          continue  # a next question is plausibly reachable -> don't false-positive
        tname = task.get("name", f"<task {i}>")
        yield self.finding(
            message=(
                f"Task {tname!r} exhaust opens slot {opened!r}, but from the exhaust "
                "state no further askable question is reachable, so the engine speaks "
                "\"An error occurred.\" instead (engine log tag "
                "task_exhaust_open_slot_unreachable). Replace "
                "on_exhaust:{open_slot} with on_exhaust:{say: '<terminal line>', "
                "then: <tool>} to control the terminal message and action."),
            location=Location(config_id=cid, node=tname,
                              json_path=f"tasks[{i}].on_failure.on_exhaust.open_slot"),
            anchor=NodeAnchor(kind="task", ref=tname,
                              field="on_failure.on_exhaust.open_slot"),
            rationale=("open_slot only falls through when a NEXT user question is "
                       "reachable; at a terminal failure it degrades to the engine "
                       "error fallback."),
            related=["engine log tag: task_exhaust_open_slot_unreachable"],
            fix_id="exhaust_replace_open_slot_with_say",
        )
