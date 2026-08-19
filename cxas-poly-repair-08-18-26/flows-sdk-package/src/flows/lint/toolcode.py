"""Non-executing static checks on a generated tool's Python source.

Three defects that a config-level check cannot see, because they live in the tool
BODY rather than in the DAG:

* ``**kwargs`` on the entry function — CES derives a tool's schema from its
  signature and silently DROPS a tool it cannot bind arguments for. Nothing errors;
  the tool simply never fires.
* a docstring ``Args:`` section that disagrees with the signature — the docstring is
  the model-facing contract, so a stale one teaches the model to pass the wrong
  argument.
* constructs CES forbids: touching ``sm`` (setters are pure validators and must
  never write slot-machine state), ``.session`` on the callback context (ADK-only,
  crashes CES), and importing another tool (CES tools cannot import each other).

The source is never executed — every check is `ast` and string work, so this is safe
to run over code an LLM just wrote and nobody has read.

Moved here from Slot Studio so the checks travel with the package that generates the
tools. `flows.lint.rules.toolcode` exposes them as lint rules; `tool_source_issues`
is the plain-data entry point for a caller that wants them without the lint machinery.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = ["ToolCodeIssue", "tool_source_issues", "SM", "SESSION_ATTR"]

#: Slot-machine state. A setter that touches it is writing state the engine owns.
SM = "sm"
#: ADK-only attribute; reading it on the callback context crashes CES.
SESSION_ATTR = "session"
#: Identifiers a tool would plausibly dot `.session` off.
_CONTEXT_NAMES = ("callback_context", "context", "ctx")


@dataclass(frozen=True)
class ToolCodeIssue:
  """One defect found in a tool body.

  `kind` groups issues so a caller can route them to different rules without
  matching on message text.
  """

  kind: str          # "syntax" | "kwargs" | "docstring" | "forbidden"
  message: str
  line: int | None = None


def _first_funcdef(tree: ast.AST, func_name: str | None = None):
  """First FunctionDef/AsyncFunctionDef (optionally by name), or None."""
  for node in ast.walk(tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      continue
    if func_name is None or node.name == func_name:
      return node
  return None


def _is_tool_import(module: str) -> bool:
  return module == "tools" or module.startswith("tools.")


def _docstring_issues(src: str, tool_name: str, tree: ast.AST) -> list[ToolCodeIssue]:
  from ..engine import blessed_source

  node = _first_funcdef(tree, tool_name) or _first_funcdef(tree)
  if node is None:
    return []
  documented = blessed_source.docstring_args(src)
  # No `Args:` section at all is fine — not every setter documents its params.
  # Only a section that DISAGREES with the signature is a defect.
  if not documented:
    return []
  signature = blessed_source.sig_args_from_source(src, node.name)
  # `*args`/`**kwargs` are not ordinary documentable params, and kwargs has its own
  # rule; excluding them stops a kwargs tool being reported twice.
  a = node.args
  signature -= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
  if documented == signature:
    return []
  parts = []
  if missing := sorted(signature - documented):
    parts.append(f"missing docs for {missing}")
  if extra := sorted(documented - signature):
    parts.append(f"documents unknown params {extra}")
  return [ToolCodeIssue(
      "docstring",
      f"Docstring Args: does not match the signature of {node.name!r} "
      f"({'; '.join(parts)}).",
      node.lineno)]


def _kwargs_issues(tree: ast.AST) -> list[ToolCodeIssue]:
  out = []
  for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      kwarg = node.args.kwarg
      if kwarg is not None:
        out.append(ToolCodeIssue(
            "kwargs",
            f"'**{kwarg.arg}' is not allowed: CES derives the tool schema from the "
            "signature and silently drops a tool it cannot bind arguments for.",
            getattr(kwarg, "lineno", node.lineno)))
  return out


def _forbidden_issues(tree: ast.AST) -> list[ToolCodeIssue]:
  out = []
  for node in ast.walk(tree):
    if isinstance(node, ast.Name) and node.id == SM:
      out.append(ToolCodeIssue(
          "forbidden",
          "Access to 'sm' is forbidden: setters are pure validators and must never "
          "touch slot-machine state.",
          node.lineno))
    elif isinstance(node, ast.Attribute) and node.attr == SESSION_ATTR:
      base = node.value
      base_name = base.id if isinstance(base, ast.Name) else None
      if base_name in _CONTEXT_NAMES:
        out.append(ToolCodeIssue(
            "forbidden",
            f"'{base_name}.session' is forbidden: .session is ADK-only and crashes "
            "CES. Use .state / .variables.",
            node.lineno))
    elif isinstance(node, ast.Import):
      for alias in node.names:
        if _is_tool_import(alias.name):
          out.append(ToolCodeIssue(
              "forbidden",
              f"Import of another tool {alias.name!r} is forbidden: CES tools cannot "
              "import each other.",
              node.lineno))
    elif isinstance(node, ast.ImportFrom):
      if _is_tool_import(node.module or ""):
        out.append(ToolCodeIssue(
            "forbidden",
            f"Import from another tool {node.module!r} is forbidden: CES tools cannot "
            "import each other.",
            node.lineno))
  return out


def tool_source_issues(tool_name: str, source: str) -> list[ToolCodeIssue]:
  """Every static defect in one tool body. Never raises, never executes the code.

  A syntax error short-circuits: the remaining checks all need a parse tree, and a
  cascade of follow-on noise would bury the one line the author has to fix.
  """
  try:
    tree = ast.parse(source)
  except SyntaxError as exc:
    return [ToolCodeIssue("syntax", f"Syntax error: {exc.msg}", exc.lineno)]

  issues: list[ToolCodeIssue] = []
  for check in (lambda: _docstring_issues(source, tool_name, tree),
                lambda: _kwargs_issues(tree),
                lambda: _forbidden_issues(tree)):
    try:
      issues.extend(check())
    except Exception as exc:  # one broken check must not hide the others
      issues.append(ToolCodeIssue("error", f"Tool lint check failed: {exc}", None))
  return issues
