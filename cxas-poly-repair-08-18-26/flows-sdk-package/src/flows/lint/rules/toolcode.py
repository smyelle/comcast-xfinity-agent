"""FLW004 / FLX002 / FLX003 — static defects in a generated tool's Python body.

The config-level rules read the DAG. These read the tool SOURCE, which is where a
whole class of failure lives that the DAG cannot show you: a tool CES will refuse to
bind, a docstring that lies to the model about its own arguments, and constructs the
runtime forbids.

One code per defect rather than one bundled "tool hygiene" code, because the three
have different severities, different fixes, and — for `**kwargs` — a failure mode
(the tool is silently dropped, so it simply never fires) that deserves to be findable
on its own.
"""

from __future__ import annotations

from typing import Iterable

from ..context import LintContext
from ..models import Category, Finding, Location
from ..registry import Rule, rule
from ..toolcode import tool_source_issues
from ...config.models import NodeAnchor


def _issues_by_kind(ctx: LintContext, kind: str):
  """`(tool_name, issue)` for every issue of one kind, in a stable order.

  Each rule re-derives the issues for the tools it cares about. That repeats the
  parse across three rules, which is cheap next to being able to run one rule in
  isolation with `--select`.
  """
  for name in sorted(ctx.bodies):
    for issue in tool_source_issues(name, ctx.bodies[name]):
      if issue.kind == kind:
        yield name, issue


def _location(name: str, issue) -> Location:
  return Location(node=name, json_path=f"tools/{name}.py:{issue.line}"
                  if issue.line else f"tools/{name}.py")


@rule(
    code="FLW004",
    category=Category.WIRING,
    severity="error",
    title="tool takes **kwargs, so CES silently drops it",
    docs="FLW004",
)
class ToolTakesKwargs(Rule):
  """CES builds a tool's schema from its signature. It cannot describe `**kwargs`,
  so it drops the tool rather than failing — no deploy error, no runtime error, just
  a tool that never fires and a flow that stalls on the slot it was meant to fill.

  Wiring rather than correctness: the code is valid Python, but the tool never
  reaches the agent.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for name, issue in _issues_by_kind(ctx, "kwargs"):
      yield self.finding(
          message=(f"{issue.message} Replace **kwargs in {name!r} with the named "
                   "parameters the DAG actually passes."),
          location=_location(name, issue),
          anchor=NodeAnchor(kind="field", ref=name, field="tool"),
          rationale=("A dropped tool produces no error anywhere — the first symptom "
                     "is a slot that never fills."),
          fix_id="declare_named_params",
      )


@rule(
    code="FLX002",
    category=Category.CORRECTNESS,
    severity="error",
    title="tool body uses a construct CES forbids",
    docs="FLX002",
)
class ForbiddenToolConstruct(Rule):
  """Touching `sm`, reading `.session` off the callback context, or importing
  another tool. The first corrupts state the engine owns, the second crashes CES at
  runtime (`.session` is ADK-only), and the third cannot resolve because CES tools
  are deployed as isolated modules.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for name, issue in _issues_by_kind(ctx, "forbidden"):
      yield self.finding(
          message=f"In tool {name!r}: {issue.message}",
          location=_location(name, issue),
          anchor=NodeAnchor(kind="field", ref=name, field="tool"),
          rationale="Each of these fails at runtime, not at deploy time.",
          fix_id="remove_forbidden_construct",
      )


@rule(
    code="FLX003",
    category=Category.CORRECTNESS,
    severity="warning",
    title="docstring Args: disagrees with the signature",
    docs="FLX003",
)
class DocstringSignatureMismatch(Rule):
  """The docstring is the model-facing contract for a tool's arguments, so a stale
  `Args:` section teaches the model to pass something the function does not take.

  A warning, not an error: the tool still binds and still runs. Only a section that
  DISAGREES is reported — omitting `Args:` entirely is fine.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for name, issue in _issues_by_kind(ctx, "docstring"):
      yield self.finding(
          message=(f"In tool {name!r}: {issue.message} The docstring is what the "
                   "model reads to decide what to pass."),
          location=_location(name, issue),
          anchor=NodeAnchor(kind="field", ref=name, field="tool"),
          rationale=("The model calls the tool from its docstring, so a stale Args: "
                     "section misroutes arguments rather than failing loudly."),
          fix_id="sync_docstring_args",
      )


@rule(
    code="FLX004",
    category=Category.CORRECTNESS,
    severity="error",
    title="tool body does not parse",
    docs="FLX004",
)
class ToolSyntaxError(Rule):
  """A tool whose Python does not parse. Deploy accepts the app and the tool fails
  the first time it is called, so catching it here is the difference between a lint
  line and a failed call."""

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for name, issue in _issues_by_kind(ctx, "syntax"):
      yield self.finding(
          message=f"Tool {name!r} does not parse — {issue.message}",
          location=_location(name, issue),
          anchor=NodeAnchor(kind="field", ref=name, field="tool"),
          rationale="An unparseable tool deploys cleanly and fails when called.",
          fix_id="fix_tool_syntax",
      )
