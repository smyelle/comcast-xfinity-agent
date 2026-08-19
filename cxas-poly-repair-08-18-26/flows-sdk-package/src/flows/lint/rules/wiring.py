"""FLW — wiring & dependencies."""

from __future__ import annotations

from typing import Iterable

from ..context import LintContext
from ..models import Category, Finding, Location
from ..registry import Rule, rule
from ...config.models import NodeAnchor


def _declared_extra_tools(app) -> set[str]:
  """Tools the author DELIBERATELY attached for the model to call ad hoc.

  An extra tool is legitimately named by no task/slot, so it must not be flagged
  dead. Gathered from the app + host + every sub-agent.
  """
  out: set[str] = set()
  for name in getattr(app, "extra_agent_tools", None) or ():
    out.add(name)
  host = getattr(app, "host", None)
  if host is not None:
    for name in getattr(host, "extra_tools", None) or ():
      out.add(name)
  for ag in getattr(app, "agents", None) or ():
    for name in getattr(ag, "extra_tools", None) or ():
      out.add(name)
  return {n for n in out if isinstance(n, str)}


@rule(
    code="FLW003",
    category=Category.WIRING,
    severity="needs_review",
    title="tool has a body but is never referenced (dead / unwired)",
    docs="FLW003",
)
class DeadTool(Rule):
  """A tool with a python body that no task, setter, correction_tool, exhaust
  `then`, or announce ever references — and that was not deliberately attached as
  a model-callable extra tool. Almost always a wiring mistake (the author meant to
  call it); occasionally intentional (WIP), which is why this is `needs_review`.

  This is issue #596 case 1: `set_transfer_subject` sat dormant across sessions
  because a `@flows.tool` with no `flows=` filter auto-attaches to every flow, so
  nothing complained that no task called it.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    referenced = ctx.referenced_tool_names()
    reserved = ctx.reserved_tool_names()
    extras = _declared_extra_tools(ctx.app)
    for name in sorted(ctx.bodies):
      if name in referenced or name in reserved or name in extras:
        continue
      if name.endswith("_dag") or name == "dag_config":
        continue
      yield self.finding(
          message=(
              f"Tool {name!r} has a body but is referenced by no task, setter, "
              "correction_tool, on_exhaust.then, or announce, and is not a declared "
              "extra tool. Wire it to the task/slot that should call it, or remove "
              "it. If it is meant to be model-callable, add it to the agent's "
              "extra_tools; if it is intentionally dormant, suppress with "
              "lint_ignore=['FLW003: <reason>']."),
          location=Location(node=name),
          anchor=NodeAnchor(kind="field", ref=name, field="tool"),
          rationale=("An unreferenced tool body is dead weight and usually a wiring "
                     "slip; a @flows.tool auto-attaches to every flow, so nothing "
                     "else flags that no task calls it."),
          fix_id="wire_or_remove_tool",
      )
