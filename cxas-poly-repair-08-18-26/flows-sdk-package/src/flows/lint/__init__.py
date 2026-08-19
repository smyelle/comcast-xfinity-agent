"""`flows lint` — the build-time authoring linter.

Public API:
    from flows.lint import lint_app
    report = lint_app(app)           # -> LintReport (pure data; never prints/exits)
    if not report.ok(): ...

See DESIGN.md and RULES.md under packages/flows/docs/lint/.
"""

from __future__ import annotations

from .context import LintContext, build_context
from .models import (
    Category,
    Finding,
    LintReport,
    Location,
    Summary,
    SCHEMA_VERSION,
)
from .registry import RULES, Rule, load_all_rules, rule
from .runner import lint_app, run_rules

__all__ = [
    "lint_app",
    "run_rules",
    "build_context",
    "LintContext",
    "LintReport",
    "Finding",
    "Location",
    "Summary",
    "Category",
    "Rule",
    "rule",
    "RULES",
    "load_all_rules",
    "SCHEMA_VERSION",
]
