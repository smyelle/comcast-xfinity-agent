"""The rule base class, the `@rule` decorator, and the global registry.

Adopted from the cxas_scrapi engine shape (itself "inspired by pylint/ruff"):
rules are first-class objects with a stable id, a category, a default severity,
and one `check(ctx)` method; a decorator instantiates and auto-registers them at
import time. Differences from cxas we deliberately keep (see DESIGN.md section 3):
one severity path (a rule's severity comes from its default, overridable by
config, never clobbered on one code path but not another); codes are validated to
match their category letter (cxas let `config`->`A` drift); rules read a
prebuilt `LintContext` and never touch the filesystem.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from ..config.models import DiagnosticFix, NodeAnchor, Severity
from .models import CATEGORY_LETTER, Category, Finding, Location

if TYPE_CHECKING:
  from .context import LintContext

_CODE_RE = re.compile(r"^FL([A-Z])(\d{3})$")

# What a finding tells the reader to run for the full rationale. This used to be
# "https://flows.docs/lint/<slug>", which is not a resolvable host — every finding the
# linter has ever printed carried a dead link. `--explain` always works, offline, and
# the same catalog now ships in the wheel at docs/lint/RULES.md.
def _docs_hint(code: str) -> str:
  return f"flows lint --explain {code}"


class Rule:
  """Base class for a lint rule. Subclass, set the class attrs, implement `check`.

  Class attributes (stamped by `@rule`, but may also be set directly for tests):
    code            stable id, e.g. "FLR001"
    category        a `Category`
    default_severity  the severity a finding gets unless config overrides it
    title           short human label
    docs            doc-page slug, kept for the shipped catalog's anchors
  """

  code: str = ""
  category: Category = Category.CORRECTNESS
  default_severity: Severity = "warning"
  title: str = ""
  docs: Optional[str] = None

  def check(self, ctx: "LintContext") -> Iterable[Finding]:  # pragma: no cover
    raise NotImplementedError

  # -- helpers rules use to build findings -------------------------------------

  def finding(
      self,
      *,
      message: str,
      location: Optional[Location] = None,
      anchor: Optional[NodeAnchor] = None,
      severity: Optional[Severity] = None,
      rationale: Optional[str] = None,
      fix_id: Optional[str] = None,
      fix: Optional[DiagnosticFix] = None,
      related: Optional[list[str]] = None,
  ) -> Finding:
    """Stamp this rule's code/category/title/docs onto a `Finding`.

    `severity` defaults to the rule's `default_severity`; a rule may pass a
    per-case severity (e.g. an error vs a warning variant) and it is honored —
    config overrides still win at resolution time, in `runner`.
    """
    return Finding(
        code=self.code,
        category=self.category,
        severity=severity or self.default_severity,
        title=self.title,
        message=message,
        location=location or Location(),
        anchor=anchor,
        rationale=rationale,
        fix_id=fix_id,
        fix=fix,
        docs_url=_docs_hint(self.code),
        related=related or [],
    )


class RuleRegistry:
  """The set of registered rules, keyed by code (codes are globally unique)."""

  def __init__(self) -> None:
    self._by_code: dict[str, Rule] = {}

  def register(self, rule_obj: Rule) -> None:
    code = rule_obj.code
    m = _CODE_RE.match(code)
    if not m:
      raise ValueError(
          f"rule code {code!r} is not FL<category-letter><3-digit> (e.g. FLR001)")
    want = CATEGORY_LETTER[rule_obj.category]
    if m.group(1) != want:
      raise ValueError(
          f"rule {code!r} is category {rule_obj.category.value!r} but its letter "
          f"{m.group(1)!r} should be {want!r}; prefix must match category")
    if code in self._by_code:
      raise ValueError(f"duplicate rule code {code!r} — codes are permanent + unique")
    self._by_code[code] = rule_obj

  def all(self) -> list[Rule]:
    return [self._by_code[c] for c in sorted(self._by_code)]

  def get(self, code: str) -> Optional[Rule]:
    return self._by_code.get(code)

  def clear(self) -> None:
    self._by_code.clear()


# The process-global registry every rule module registers into on import.
RULES = RuleRegistry()


def rule(
    *,
    code: str,
    category: Category,
    severity: Severity,
    title: str,
    docs: Optional[str] = None,
) -> Callable[[type[Rule]], type[Rule]]:
  """Class decorator: stamp metadata, instantiate, and register the rule."""

  def _decorate(cls: type[Rule]) -> type[Rule]:
    cls.code = code
    cls.category = category
    cls.default_severity = severity
    cls.title = title
    cls.docs = docs
    RULES.register(cls())
    return cls

  return _decorate


def load_all_rules() -> RuleRegistry:
  """Import every rule module (registration side-effect) and return the registry."""
  from . import rules as _rules_pkg  # noqa: F401  (import triggers registration)

  _rules_pkg.load()
  return RULES
