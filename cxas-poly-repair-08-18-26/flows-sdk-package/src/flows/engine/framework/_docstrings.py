"""Vendored docstring/signature parity logic (blessed copy).

These are byte-for-logic copies of ``_documented_args`` and ``_sig_args`` from the
canonical ``bella_notte/check_docstrings.py``. They are vendored into the blessed
bundle so the docstring-vs-signature lint rule is importable inside the hosted
container, where the original repo file is not on the path.

Source: /usr/local/google/home/fsamuel/ps_experiment/bella_notte/check_docstrings.py
Keep these in sync with that file (the canonical DoD gate).
"""

import ast
import re


def documented_args(docstring):
  """Names listed under the docstring's Args: section (or None if no section)."""
  if not docstring:
    return None
  lines = docstring.split("\n")
  try:
    start = next(i for i, l in enumerate(lines) if l.strip() == "Args:")
  except StopIteration:
    return None
  base = len(lines[start]) - len(lines[start].lstrip())
  names = []
  token = re.compile(r"^\*{0,2}(\w+)$")
  for l in lines[start + 1:]:
    if not l.strip():
      continue
    indent = len(l) - len(l.lstrip())
    if indent <= base:  # next section (Returns:, Raises:, …) or end
      break
    if indent > base + 4 or ":" not in l:  # wrapped description line, skip
      continue
    # An arg entry: "name: desc", "name (type): desc", or a grouped
    # "a, b, c: desc". Take the head before the first ':', drop any type
    # annotation, and accept only if every comma-token is a bare identifier
    # (so prose containing a colon isn't mistaken for an arg entry).
    head = re.sub(r"\([^)]*\)", "", l.split(":", 1)[0]).strip()
    toks = [t.strip() for t in head.split(",") if t.strip()]
    matched = [token.match(t).group(1) for t in toks if token.match(t)]
    if toks and len(matched) == len(toks):
      names.extend(matched)
  return names


def sig_args(node):
  """Parameter names of a function def node, excluding self/cls."""
  a = node.args
  names = [p.arg for p in (a.posonlyargs + a.args + a.kwonlyargs)]
  if a.vararg:
    names.append(a.vararg.arg)
  if a.kwarg:
    names.append(a.kwarg.arg)
  return [n for n in names if n not in ("self", "cls")]


def _first_funcdef(tree, func_name=None):
  """Return the first matching FunctionDef/AsyncFunctionDef node, or None."""
  for node in ast.walk(tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      continue
    if func_name is None or node.name == func_name:
      return node
  return None
