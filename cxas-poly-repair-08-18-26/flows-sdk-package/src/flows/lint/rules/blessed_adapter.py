"""FLX001 — surface the blessed DAG validator's diagnostics as lint findings.

The blessed `DagConfigValidator` / `CrossConfigValidator` stay the source of truth
for framework correctness and must never drift from CES (see DESIGN.md section 2).
Rather than reimplement any of that, this adapter runs the real validator over the
assembled configs and re-emits each diagnostic as a `Finding`, preserving the
validator's own structured `code` in the message + `related`. Category is
`correctness`; severity is taken per-diagnostic (error vs warning).

This is why `flows lint` reports framework-correctness problems too, not only the
native authoring/best-practice rules. (The authoring `_check_*` in `build.py` are
NOT run here yet; they remain in `flows validate`/`emit`. Porting them into native
rules is a later phase.)
"""

from __future__ import annotations

from typing import Iterable

from ..context import LintContext
from ..models import Category, Finding, Location
from ..registry import Rule, rule


@rule(
    code="FLX001",
    category=Category.CORRECTNESS,
    severity="error",
    title="framework DAG validator diagnostic",
    docs="FLX001",
)
class BlessedValidator(Rule):
  """Run the packaged validator and lift its diagnostics into findings."""

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    if not ctx.configs:
      return
    from ...config import validation as sv
    try:
      from ...authoring.build import FRAMEWORK_ROOT
    except Exception:  # noqa: BLE001 — fall back to the validator's default root
      FRAMEWORK_ROOT = None

    for cid, cfg in ctx.configs.items():
      _valid, errors, warnings = sv.raw_validate_single(
          cfg, available_tools=ctx.available, setter_sources=ctx.bodies,
          task_tool_sources=ctx.bodies, framework_root=FRAMEWORK_ROOT)
      for diag in sv.map_diagnostics(list(errors), list(warnings), cfg):
        yield self._from_diag(cid, diag)

    if len(ctx.configs) > 1:
      _cv, cerr, cwarn = sv.raw_validate_cross(ctx.configs, framework_root=FRAMEWORK_ROOT)
      for diag in sv.map_diagnostics(list(cerr), list(cwarn), None):
        yield self._from_diag(None, diag)

  def _from_diag(self, cid, diag) -> Finding:
    anchor = diag.anchor
    node = anchor.ref if anchor else None
    return self.finding(
        message=diag.message,
        severity=diag.severity if diag.severity in ("error", "warning") else "warning",
        location=Location(config_id=cid, node=node),
        anchor=anchor,
        related=[f"validator: {diag.raw}"] if diag.raw != diag.message else [],
    )
