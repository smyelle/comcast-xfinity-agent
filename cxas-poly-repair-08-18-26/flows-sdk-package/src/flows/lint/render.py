"""Renderers: human TTY and machine JSON, plus `--list-rules` and `--explain`.

One `Finding` renders to both a human line and a JSON record from the same data.
JSON is versioned and single-shape (DESIGN.md section 3, principle 6); human output
groups by severity and leads each finding with its structured location.
"""

from __future__ import annotations

import json
import textwrap
from typing import Optional

from .models import Finding, LintReport, severity_rank
from .registry import Rule, RuleRegistry

_SEV_LABEL = {"error": "error", "warning": "warning", "info": "info",
              "needs_review": "review"}


def render_json(report: LintReport) -> str:
  """The stable machine format: `{schema_version, findings, summary, ran_rules}`."""
  return json.dumps(report.model_dump(mode="json"), indent=2)


def _wrap(text: str, indent: str = "         ") -> str:
  lines = textwrap.wrap(text, width=88 - len(indent)) or [""]
  return "\n".join(indent + ln for ln in lines)


def render_human(report: LintReport, *, show_suppressed: bool = False) -> str:
  """Grouped, indented TTY output. No ANSI (stable for tests); the CLI may colorize."""
  out: list[str] = []
  live = [f for f in report.findings if show_suppressed or not f.suppressed_by]
  if not live:
    out.append("lint: clean — no findings")
  for f in sorted(live, key=lambda f: (severity_rank(f.severity),
                                       f.location.config_id or "",
                                       f.location.json_path or "", f.code)):
    loc = f.location.label()
    header = f"{loc}" if loc else "(app)"
    out.append(header)
    sev = _SEV_LABEL.get(f.severity, f.severity)
    tag = "  suppressed" if f.suppressed_by else ""
    out.append(f"  {sev:<8}{f.code}  {f.title}{tag}")
    out.append(_wrap(f.message))
    if f.fix is not None:
      out.append(f"         fix: {f.fix.label}  (autofixable)")
    if f.docs_url:
      out.append(f"         docs: {f.docs_url}")
    out.append("")

  s = report.summary
  parts = []
  for sev in ("error", "warning", "info", "needs_review"):
    n = s.by_severity.get(sev, 0)
    if n:
      parts.append(f"{n} {_SEV_LABEL[sev]}{'s' if n != 1 else ''}")
  tail = ""
  if s.fixable:
    tail += f"  ({s.fixable} autofixable)"
  if s.suppressed:
    tail += f"  ({s.suppressed} suppressed)"
  out.append("Summary: " + (", ".join(parts) if parts else "clean") + tail)
  return "\n".join(out)


def render_list_rules(registry: RuleRegistry, *, as_json: bool = False) -> str:
  rules = registry.all()
  if as_json:
    return json.dumps([
        {"code": r.code, "category": r.category.value,
         "default_severity": r.default_severity, "title": r.title,
         "docs": f"flows lint --explain {r.code}",
         "catalog": "docs/lint/RULES.md"}
        for r in rules
    ], indent=2)
  out: list[str] = [f"{len(rules)} rules:"]
  cur: Optional[str] = None
  for r in rules:
    if r.category.value != cur:
      cur = r.category.value
      out.append(f"\n[{cur}]")
    out.append(f"  {r.code}  {r.default_severity:<12}  {r.title}")
  return "\n".join(out)


def render_explain(r: Rule) -> str:
  doc = (r.__class__.__doc__ or "").strip()
  return (
      f"{r.code}  ({r.category.value}, default {r.default_severity})\n"
      f"  {r.title}\n"
      f"  more: flows lint --explain {r.code}   (catalog: docs/lint/RULES.md)\n\n"
      + textwrap.indent(doc, "  ")
  )
