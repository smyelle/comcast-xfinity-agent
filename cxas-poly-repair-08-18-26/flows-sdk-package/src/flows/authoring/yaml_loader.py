"""Load a flow authored in YAML/JSON into the same `Flow`/`Config` the DSL builds.

A flow file is the declarative form of the validated config dict — slots, tasks,
and flow-level policies — plus `config_id`/`root_agent`. Slot/task dicts are used
as-is (full fidelity with the framework schema); only `condition` fields are
compiled: a helper form (`has(slot)`, `unset(slot)`, `eq(slot, val)`, `ne(slot,
val)`) — alone or combined with `and`/`or`/`not` — is turned into the engine's
string-lambda, and any other string is passed through unchanged (already a
lambda). A string that starts like a helper form but is not a valid one is
REJECTED rather than compiled into something that quietly never fires. YAML and
DSL therefore converge on an identical `Config`.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Optional

from . import dsl
from .dsl import App, Flow

_LAMBDA_PREFIX = "lambda f: "
# A helper CALL, anchored where the scan currently stands. The open paren is matched
# here; its partner is found by `_close_paren` (quote- and depth-aware) so a greedy
# `(.*)` can never swallow a following `or has(b)` into the slot NAME.
_CALL_RE = re.compile(r"(has|unset|eq|ne)\s*\(")
# An expression the author clearly meant as a helper form: it STARTS with a helper
# call, a `not`, or a parenthesized group. Anything else (notably `lambda f: ...`) is
# left alone — see the module docstring's trust model.
_HELPER_HEAD_RE = re.compile(r"^\s*(?:not\b|\(|(?:has|unset|eq|ne)\s*\()")
# ...and contains at least one helper call somewhere, so `(f.get('a'))` and other raw
# predicates still pass through untouched.
_ANY_CALL_RE = re.compile(r"(?<![A-Za-z0-9_.])(has|unset|eq|ne)\s*\(")


def compile_condition(expr: Any) -> Any:
  """Compile a condition helper form to a string lambda; pass through otherwise.

  A single helper (`has(x)`, `eq(x, 1)`, ...) and any `and`/`or`/`not` COMPOSITE of
  helpers compile to the engine's string lambda. A helper-LOOKING expression that is
  neither raises: `has(a) or has(b)` used to match a greedy regex and compile to
  `lambda f: bool(f.get('a) or has(b'))` — a permanently-false gate on a slot that
  does not exist, with no error and no warning, so the slot simply never activated.
  """
  if isinstance(expr, dict):
    # A YAML-authored gate is naturally the declarative form; check it at LOAD time, since
    # these slot/task dicts are used verbatim and never pass through a DSL builder.
    return dsl.gate(expr)
  if not isinstance(expr, str):
    return expr
  if not _HELPER_HEAD_RE.match(expr) or not _ANY_CALL_RE.search(expr):
    return expr  # already a raw "lambda f: ..." string
  compiled = _compile_helper_expr(expr)
  if compiled is None:
    raise ValueError(
        f"condition {expr!r} looks like a helper form but is not one: combine"
        ' helpers with and/or/not (e.g. "has(a) or has(b)"), or write the whole'
        ' predicate as a "lambda f: ..." string.')
  return compiled


def _compile_one(fn: str, argstr: str) -> str:
  """Compile a single `has|unset|eq|ne` call body to its string lambda.

  Every author-supplied fragment lands inside a `dsl.*` `!r`, i.e. as a quoted
  LITERAL — which is what keeps the helper forms out of injection range.
  """
  argstr = argstr.strip()
  if fn in ("has", "unset"):
    slot = _unquote(argstr)
    return dsl.has(slot) if fn == "has" else dsl.unset(slot)
  # eq/ne take (slot, value)
  slot_s, val_s = _split_two(argstr)
  slot = _unquote(slot_s)
  val = _parse_value(val_s)
  return dsl.eq(slot, val) if fn == "eq" else dsl.ne(slot, val)


def _close_paren(s: str, open_at: int) -> int:
  """Index of the `)` matching `s[open_at]`, or -1. Quote- and depth-aware."""
  depth = 0
  quote = ""
  escaped = False
  for i in range(open_at, len(s)):
    ch = s[i]
    if quote:
      if escaped:
        escaped = False
      elif ch == "\\":
        escaped = True
      elif ch == quote:
        quote = ""
    elif ch in "\"'":
      quote = ch
    elif ch in "([{":
      depth += 1
    elif ch in ")]}":
      depth -= 1
      if depth == 0:
        return i
  return -1


def _compile_helper_expr(expr: str) -> Optional[str]:
  """A single helper, or an and/or/not composite of helpers, to a string lambda.

  Returns None when `expr` is neither (the caller turns that into an error). Each
  helper call is replaced by a `_h<n>` placeholder, and the SKELETON left over must
  parse as a pure boolean expression over those placeholders — so the only glue that
  can reach the emitted lambda is `and`/`or`/`not`/parens, and every author fragment
  still arrives via `dsl.*`'s repr-quoting. Composite support therefore opens no
  path an author did not already have.
  """
  bodies: list[str] = []
  skeleton: list[str] = []
  i = 0
  while i < len(expr):
    m = _CALL_RE.match(expr, i)
    if m and (i == 0 or not (expr[i - 1].isalnum() or expr[i - 1] in "_.")):
      close = _close_paren(expr, m.end() - 1)
      if close < 0:
        return None  # unbalanced / quote-mangled: not a helper form
      try:
        bodies.append(_compile_one(m.group(1), expr[m.end():close]))
      except ValueError:
        return None  # e.g. a one-arg eq(): report it as "not a helper form"
      skeleton.append(f"_h{len(bodies) - 1}")
      i = close + 1
      continue
    skeleton.append(expr[i])
    i += 1
  if not bodies:
    return None
  text = "".join(skeleton).strip()
  if len(bodies) == 1 and text == "_h0":
    return bodies[0]  # a plain single helper — emit exactly what the DSL builds
  try:
    tree = ast.parse(text, mode="eval")
  except SyntaxError:
    return None
  names = {f"_h{n}" for n in range(len(bodies))}
  if not _is_boolean_skeleton(tree.body, names):
    return None
  # Substituted into the SKELETON text, not the AST: the emitted lambda then holds
  # only repr-quoted literals from `dsl.*` plus and/or/not/parens.
  return _LAMBDA_PREFIX + re.sub(
      r"_h(\d+)", lambda m: f"({_body_of(bodies[int(m.group(1))])})", text)


def _is_boolean_skeleton(node: ast.AST, names: set[str]) -> bool:
  """True only for `and`/`or`/`not` over the placeholder names — no calls, no
  attributes, no literals. The grammar check that keeps a composite safe."""
  if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
    return all(_is_boolean_skeleton(v, names) for v in node.values)
  if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
    return _is_boolean_skeleton(node.operand, names)
  return isinstance(node, ast.Name) and node.id in names


def _body_of(compiled: str) -> str:
  """The predicate body of a `lambda f: ...` string."""
  return compiled[len(_LAMBDA_PREFIX):]


def _unquote(s: str) -> str:
  s = s.strip()
  if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
    return s[1:-1]
  return s


def _split_two(s: str) -> tuple[str, str]:
  # Split on the first top-level comma, ignoring commas inside quotes or brackets
  # (e.g. eq("a,b", c) or eq(x, "New York, NY")).
  depth = 0
  quote = ""
  for i, ch in enumerate(s):
    if quote:
      if ch == quote and (i == 0 or s[i - 1] != "\\" or (i > 1 and s[i - 2] == "\\")):
        quote = ""
    elif ch in "\"'":
      quote = ch
    elif ch in "([{":
      depth += 1
    elif ch in ")]}":
      depth -= 1
    elif ch == "," and depth == 0:
      return s[:i], s[i + 1 :]
  raise ValueError(f"eq/ne need two args: {s!r}")


def _parse_value(s: str) -> Any:
  s = s.strip()
  if s and s[0] in "\"'":
    return _unquote(s)
  try:
    return json.loads(s)  # numbers / true / false / null
  except ValueError:
    return s


def _compile_conditions(items: Any, key: str) -> list[dict[str, Any]]:
  """Copy each slot/task dict with its `condition` compiled.

  A type error NAMES the offending key and index: the bare `dict(item)` this
  replaces failed with "dictionary update sequence element #0 has length 1",
  which never said which entry of which section was wrong.
  """
  if items is None:
    return []
  if not isinstance(items, list):
    raise ValueError(
        f"flow file `{key}` must be a list of dicts, got {type(items).__name__}")
  out = []
  for n, it in enumerate(items):
    if not isinstance(it, dict):
      raise ValueError(
          f"flow file `{key}`[{n}] must be a dict, got {type(it).__name__}: {it!r}")
    it = dict(it)
    if "condition" in it:
      it["condition"] = compile_condition(it["condition"])
    out.append(it)
  return out


def flow_from_dict(data: dict[str, Any]) -> Flow:
  """Build a `Flow` from a parsed flow-file dict."""
  if data is None:
    # An empty (or comments-only) file parses to None, and `dict(None)` raised
    # `TypeError: 'NoneType' object is not iterable` — which named no file and no key.
    raise ValueError("flow file is empty — it must set `config_id`")
  if not isinstance(data, dict):
    raise ValueError(
        "flow file must be a mapping with a `config_id`, got"
        f" {type(data).__name__}")
  data = dict(data)
  config_id = data.pop("config_id", None)
  alias = data.pop("id", None)
  if config_id and alias is not None:
    # Popping `id` only when `config_id` was absent left it behind as a flow policy
    # key, and the failure came back as "unknown flow policy key 'id'".
    raise ValueError(
        f"flow file sets both `config_id` ({config_id!r}) and its alias `id`"
        f" ({alias!r}) — keep one.")
  config_id = config_id or alias
  if not config_id:
    raise ValueError("flow file must set `config_id`")
  root_agent = data.pop("root_agent", "")
  slots = _compile_conditions(data.pop("slots", None), "slots")
  tasks = _compile_conditions(data.pop("tasks", None), "tasks")
  f = Flow(config_id, root_agent=root_agent)
  f.add(*slots)
  for t in tasks:
    f.task(t)
  # Everything left is a flow-level policy key.
  for k, v in data.items():
    f.set(k, v)
  return f


def load_flow(path_or_data) -> Flow:
  """Load a flow from a YAML/JSON path or an already-parsed dict."""
  data = _read(path_or_data)
  return flow_from_dict(data)


def load_app(path_or_data, root_flow: Flow, extra_flows=None) -> App:
  """Build an `App` from an app-config file (a full app.json superset) + flows."""
  data = _read(path_or_data)
  return App(
      root_flow=root_flow,
      extra_flows=list(extra_flows or []),
      app_display_name=data.get("app_display_name") or data.get("displayName") or root_flow.config_id,
      gcp_project=data.get("gcp_project", "ces-deployment-dev"),
      location=data.get("location", "us"),
      model=data.get("model") or (data.get("modelSettings") or {}).get("model") or "gemini-3.1-flash-live",
      variables=data.get("variableDeclarations", []) or data.get("variables", []),
      agent_instruction=data.get("agent_instruction"),
      global_instruction=data.get("global_instruction"),
      app_uuid=data.get("app_uuid"),
      agent_uuid=data.get("agent_uuid"),
  )


def _read(path_or_data) -> dict[str, Any]:
  if isinstance(path_or_data, dict):
    return path_or_data
  import yaml  # local import: pyyaml is a core dep but keep import surface tight

  with open(path_or_data, "r", encoding="utf-8") as f:
    return yaml.safe_load(f)
