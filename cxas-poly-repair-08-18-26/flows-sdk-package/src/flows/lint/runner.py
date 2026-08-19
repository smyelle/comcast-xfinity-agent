"""Run the rules over an assembled `LintContext` and assemble a `LintReport`.

Crash-envelope: a single rule raising never wedges the whole lint — its failure
becomes a `warning` finding under that rule's code and the run continues.
Deterministic: findings are sorted (severity, config, json-path, code) so re-runs
and diffs are byte-stable (a coding-agent affordance, DESIGN.md section 8).
"""

from __future__ import annotations

from typing import Iterable, Optional

from .context import LintContext, build_context
from .models import Category, Finding, LintReport, Location, Summary, severity_rank
from .registry import Rule, RuleRegistry, load_all_rules


def _selected(r: Rule, select: Optional[set[str]], ignore: set[str]) -> bool:
  """Rule passes selection if in `select` (or select is None) and not in `ignore`.

  Both sets accept a rule code (FLR001) or a category value (reachability), matched
  case-insensitively so `flr001` / `VOICE` work as well as the canonical forms.
  """
  keys = {r.code.lower(), r.category.value.lower()}
  if select is not None and not (keys & {s.lower() for s in select}):
    return False
  if keys & {s.lower() for s in ignore}:
    return False
  return True


def _suppressions(ctx: LintContext) -> tuple[set[str], dict[tuple, set[str]]]:
  """`(global_codes, {(config_id, node): codes})` from app- + node-level ignores.

  A `lint_ignore` entry may be a bare code or `"CODE: reason"`; only the code is
  matched. App-level lives on `App.lint_ignore`; node-level rides as a stripped
  `_lint_ignore` on a slot/task dict.
  """
  def codes(raw) -> set[str]:
    # A bare string is a single entry, not an iterable of characters; codes are
    # upper-cased so a lowercase `lint_ignore=["flr001"]` still matches f.code.
    if isinstance(raw, str):
      raw = [raw]
    out: set[str] = set()
    for item in raw or ():
      if isinstance(item, str) and item:
        out.add(item.split(":", 1)[0].strip().upper())
    return out

  global_codes = codes(getattr(ctx.app, "lint_ignore", None))
  by_node: dict[tuple, set[str]] = {}
  for cid in ctx.config_ids():
    for node in (*ctx.slots(cid), *ctx.tasks(cid)):
      ig = codes(node.get("_lint_ignore"))
      if ig and node.get("name"):
        by_node[(cid, node["name"])] = ig
  return global_codes, by_node


def _apply_suppression(f: Finding, global_codes: set[str], by_node: dict) -> Finding:
  if f.code in global_codes:
    f.suppressed_by = "app.lint_ignore"
    return f
  key = (f.location.config_id, f.location.node)
  if f.code in by_node.get(key, set()):
    f.suppressed_by = f"{f.location.node}._lint_ignore"
  return f


def _sort_key(f: Finding):
  return (severity_rank(f.severity), f.location.config_id or "",
          f.location.json_path or "", f.code)


def run_rules(
    ctx: LintContext,
    *,
    registry: Optional[RuleRegistry] = None,
    select: Optional[Iterable[str]] = None,
    ignore: Optional[Iterable[str]] = None,
) -> LintReport:
  """Run selected rules over `ctx` and return the assembled report."""
  reg = registry or load_all_rules()
  sel = set(select) if select is not None else None
  ign = set(ignore or ())

  findings: list[Finding] = []
  ran: list[str] = []

  # A config that would not even assemble: one blocking finding, nothing else can run.
  if ctx.assembly_error is not None:
    findings.append(Finding(
        code="FLX001", category=Category.CORRECTNESS, severity="error",
        title="app does not assemble",
        message=(f"The app could not be assembled for linting: {ctx.assembly_error}"),
        location=Location(),
    ))
    return _finalize(findings, ran)

  glob, by_node = _suppressions(ctx)
  for r in reg.all():
    if not _selected(r, sel, ign):
      continue
    ran.append(r.code)
    try:
      for f in r.check(ctx):
        findings.append(_apply_suppression(f, glob, by_node))
    except Exception as exc:  # noqa: BLE001 — one bad rule must not wedge the lint
      findings.append(Finding(
          code=r.code, category=r.category, severity="warning",
          title=r.title or r.code,
          message=(f"internal: rule {r.code} raised {type(exc).__name__}: {exc} — "
                   "this is a linter bug; the rule was skipped."),
          location=Location()))
  return _finalize(findings, ran)


def _finalize(findings: list[Finding], ran: list[str]) -> LintReport:
  findings.sort(key=_sort_key)
  return LintReport(findings=findings, summary=Summary.of(findings), ran_rules=sorted(ran))


def lint_app(
    app,
    *,
    select: Optional[Iterable[str]] = None,
    ignore: Optional[Iterable[str]] = None,
) -> LintReport:
  """Lint a `flows.App`: assemble it, run every rule, return a `LintReport`.

  Pure: returns data, never prints, never exits. The CLI decides the exit code.
  """
  ctx = build_context(app)
  return run_rules(ctx, select=select, ignore=ignore)
