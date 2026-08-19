"""Data model for `flows lint`: `Finding`, `LintReport`, and the shared vocab.

The linter deliberately reuses the diagnostic vocabulary already defined in
`flows.config.models` — `Severity` (error/warning/info/needs_review), `NodeAnchor`
(the structured slot/task/field anchor a Studio UI highlights) and `DiagnosticFix`
(the autofix patch) — so a `Finding` slots into the same UI/CLI channel as the
framework validator's own diagnostics. `Finding.to_diagnostic()` bridges the two.

A `Finding` adds what a *linter* (as opposed to a one-off validator) needs: a
stable `code`, a `category`, a `title`, the "why it matters" `rationale`, a
`docs_url`, and a `location` carrying a JSON path a coding agent can navigate or
patch. The whole thing serializes to one versioned JSON shape (`LintReport`) —
one record shape, always (see DESIGN.md section 3, principle 6).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ..config.models import DiagnosticFix, NodeAnchor, Severity

# Bump on any BREAKING change to the JSON record shape (a removed/renamed field).
# Additive fields do not require a bump. Agents guard on this.
SCHEMA_VERSION = 1

# Severity ordering, most severe first — for deterministic sort + gating.
_SEVERITY_RANK: dict[Severity, int] = {
    "error": 0,
    "warning": 1,
    "info": 2,
    "needs_review": 3,
}


def severity_rank(sev: Severity) -> int:
  return _SEVERITY_RANK.get(sev, 99)


class Category(str, Enum):
  """The rule families. The letter after `FL` in a code names the category."""

  WIRING = "wiring"                # FLW — tool/slot/task resolution + dependencies
  REACHABILITY = "reachability"    # FLR — dead-ends, unreachable states, gating
  ROBUSTNESS = "robustness"        # FLB — failure/exhaust handling, tool-shape
  MODEL_RELIANCE = "model_reliance"  # FLM — determinism vs improvisation
  CONVERSATION = "conversation"    # FLC — caller-experience / conversation design
  VOICE = "voice"                  # FLV — spoken-copy quality
  MULTI_AGENT = "multi_agent"      # FLA — routing + handoff wiring
  PERFORMANCE = "performance"      # FLP — narration/latency/efficiency
  CORRECTNESS = "correctness"      # FLX — surfaced from the blessed DAG validator


# The category letter used in a code (`FL` + letter + 3 digits), for validating
# that a rule's code matches its declared category.
CATEGORY_LETTER: dict[Category, str] = {
    Category.WIRING: "W",
    Category.REACHABILITY: "R",
    Category.ROBUSTNESS: "B",
    Category.MODEL_RELIANCE: "M",
    Category.CONVERSATION: "C",
    Category.VOICE: "V",
    Category.MULTI_AGENT: "A",
    Category.PERFORMANCE: "P",
    Category.CORRECTNESS: "X",
}


class Location(BaseModel):
  """Where a finding lives, for humans and for machine navigation/patching."""

  model_config = ConfigDict(extra="forbid")
  config_id: Optional[str] = None   # which flow/config
  node: Optional[str] = None        # slot/task/group name, human-facing
  json_path: Optional[str] = None   # e.g. "tasks[3].on_failure.on_exhaust.open_slot"

  def label(self) -> str:
    """`member_flow > task 'verify' > on_failure.on_exhaust` for the TTY view."""
    parts: list[str] = []
    if self.config_id:
      parts.append(self.config_id)
    if self.node:
      parts.append(self.node)
    if self.json_path and self.json_path != self.node:
      parts.append(self.json_path)
    return " > ".join(parts)


class Finding(BaseModel):
  """One lint result. Self-contained: everything a human or agent needs to act."""

  model_config = ConfigDict(extra="allow")

  code: str                                  # stable, e.g. "FLR001"
  category: Category
  severity: Severity                         # resolved (default or overridden)
  title: str                                 # short human label
  message: str                               # full message; ENDS with a fix imperative
  location: Location = Field(default_factory=Location)
  anchor: Optional[NodeAnchor] = None        # structured node anchor (Studio highlight)
  rationale: Optional[str] = None            # the "why it matters"
  fix_id: Optional[str] = None               # stable id a fix synthesizer dispatches on
  fix: Optional[DiagnosticFix] = None        # optional autofix patch (Phase 3)
  docs_url: Optional[str] = None
  related: list[str] = Field(default_factory=list)  # engine log tags, sibling findings
  suppressed_by: Optional[str] = None        # set (not dropped) when a suppression matched

  def to_diagnostic(self):
    """Downcast to a `flows.config.models.Diagnostic` for Studio/deploy consumers."""
    from ..config.models import Diagnostic

    return Diagnostic(
        severity=self.severity,
        message=self.message,
        raw=f"[{self.code}] {self.message}",
        anchor=self.anchor,
        fix=self.fix,
    )


class Summary(BaseModel):
  model_config = ConfigDict(extra="forbid")
  total: int = 0
  by_severity: dict[str, int] = Field(default_factory=dict)
  by_category: dict[str, int] = Field(default_factory=dict)
  fixable: int = 0
  suppressed: int = 0

  @classmethod
  def of(cls, findings: list[Finding]) -> "Summary":
    live = [f for f in findings if not f.suppressed_by]
    by_sev: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    for f in live:
      by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
      by_cat[f.category.value] = by_cat.get(f.category.value, 0) + 1
    return cls(
        total=len(live),
        by_severity=by_sev,
        by_category=by_cat,
        fixable=sum(1 for f in live if f.fix is not None),
        suppressed=sum(1 for f in findings if f.suppressed_by),
    )


class LintReport(BaseModel):
  """The versioned, single-shape result of a lint run."""

  model_config = ConfigDict(extra="forbid")
  schema_version: int = SCHEMA_VERSION
  findings: list[Finding] = Field(default_factory=list)
  summary: Summary = Field(default_factory=Summary)
  ran_rules: list[str] = Field(default_factory=list)  # codes actually executed

  def blocking(self, strict: bool = False) -> list[Finding]:
    """Findings that should fail the build: errors always, warnings under strict."""
    levels = {"error"} | ({"warning"} if strict else set())
    return [f for f in self.findings if not f.suppressed_by and f.severity in levels]

  def ok(self, strict: bool = False) -> bool:
    return not self.blocking(strict)
