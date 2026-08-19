"""Native CXAS tool authoring via the `@tool` decorator.

Author a tool exactly like a CXAS tool (see the canonical `examples/pydantic_cxas_tool.py`):
a plain function whose signature + docstring are the model-facing schema. The input
may be a **pydantic model** (not just kwargs), and the return may be a pydantic
model — `flows` derives the tool's declared output keys from the return model, so
pydantic tools validate without a hand-written shim.

    from pydantic import BaseModel, Field
    from flows import tool

    class ShipmentRequest(BaseModel):
        tracking_number: str = Field(description="Eight-digit tracking number")

    class ShipmentStatus(BaseModel):
        status_message: str
        success: bool = True

    @tool(flow="acme_tracking")
    def lookup_shipment(req: ShipmentRequest) -> ShipmentStatus:
        \"\"\"Look up the delivery status for a tracking number.\"\"\"
        return ShipmentStatus(status_message="Out for delivery")

At emit, `flows` renders each tool into the CES tool file (the referenced pydantic
models are inlined so the file is self-contained) and attaches it to its flow(s).
"""

from __future__ import annotations

import ast
import builtins
import inspect
import sys
import textwrap
import types
import typing
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
  from pydantic import BaseModel
except Exception:  # pragma: no cover - pydantic is a core dep, but stay defensive
  BaseModel = None  # type: ignore


@dataclass
class ToolSpec:
  """A registered tool: the function, the flows it attaches to, and derived meta."""

  name: str
  func: Optional[Callable[..., Any]]
  flows: tuple[str, ...]
  output_keys: list[str] = field(default_factory=list)
  # Emits `executionType: ASYNCHRONOUS` on the tool resource. CES then defers the body
  # and hands the caller a `{"result": "pending"}` placeholder, delivering the real
  # payload a turn or more later as a synthetic user turn — pair with `task(awaits=…)`.
  asynchronous: bool = False
  # Emits `timeout: "<n>s"` on the tool resource. CES kills a tool execution at 60
  # seconds unless the resource says otherwise, and the failure is silent: an
  # asynchronous body simply never reports, so nothing times out and nothing errors.
  # None omits the key and takes the platform default.
  timeout_seconds: Optional[int] = None
  # Pre-rendered, self-contained source for a GENERATED tool (no function to
  # introspect). Set by `register_source_tool`; when present it is the emitted body
  # verbatim and `func` is None.
  source: Optional[str] = None
  # Free-form metadata a generator attaches to its own tools (an OpenAPI wrapper
  # records which operation it calls and whether it declared a mock). Kept generic so
  # the registry's lifecycle — including `clear_registry` — covers it without this
  # module knowing what any particular generator means.
  meta: dict[str, Any] = field(default_factory=dict)


# Module-global registry populated by the decorator (keyed by tool name).
_REGISTRY: dict[str, ToolSpec] = {}

# Specs a later registration displaced from `_REGISTRY` under the same name.
#
# Tool names are one namespace for the whole PROCESS, but a tool belongs to an APP. Two
# apps built in one process — the example suite, a notebook, a migration run — routinely
# reuse a plain name like `lookup_bill`, and the second registration used to silently
# become the first: the losing app emitted a body its author never wrote. Nothing said
# so, and the first sign was a validator naming output keys from a function in another
# file entirely.
#
# Keeping the displaced specs lets `resolve_specs` pick the one attached to the flows of
# the app being built, which makes the outcome independent of import order.
_SHADOWED: dict[str, list[ToolSpec]] = {}


def _is_model(t: Any) -> bool:
  return BaseModel is not None and isinstance(t, type) and issubclass(t, BaseModel)


def _hints(func: Callable[..., Any]) -> dict[str, Any]:
  """Resolved type hints, tolerant of `from __future__ import annotations` (PEP 563
  makes raw ``__annotations__`` strings). Falls back to the raw dict on failure."""
  try:
    return typing.get_type_hints(func)
  except Exception:
    return dict(getattr(func, "__annotations__", {}))


def _literal_return_keys(func: Callable[..., Any]) -> list[str]:
  """Keys a plain-dict tool returns, when EVERY return is a constant-keyed literal.

  A pydantic model's field set is closed and statically known, which is why the caller
  trusts it. `return {"success": True, "status": s}` is closed in exactly the same way —
  the keys are right there in the source — so reading them turns the success-key check
  from "pydantic tools only" into "any tool whose answer can be read".

  This is not academic. A tool returning a plain dict without its `success_check` key
  looks FAILED on every call, fills none of its outputs, and (with
  `on_failure.max_retries` defaulting to 0) leaves the flow saying "An error occurred."
  on every turn for the rest of the call. Measured live on ces-probes 86 — and the
  existing check could not see it, because it skips exactly this kind of tool.

  Deliberately all-or-nothing: one return that is a bare name, a comprehension, a
  `**spread` or a computed key and this gives up entirely. A PARTIAL key set is worse
  than none, because the caller would report a missing key that the tool does return.

  Args:
    func: The tool function.

  Returns:
    The union of the literal returns' keys, or [] when they cannot all be read.
  """
  try:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
  except (OSError, TypeError, SyntaxError, IndentationError, ValueError):
    return []
  outer = next((n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
  if outer is None:
    return []
  # Returns belonging to a NESTED def are that helper's, not this tool's.
  nested = {id(n) for d in ast.walk(outer)
            if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef)) and d is not outer
            for n in ast.walk(d)}
  keys: set[str] = set()
  seen_any = False
  for node in ast.walk(outer):
    if not isinstance(node, ast.Return) or id(node) in nested:
      continue
    seen_any = True
    if not isinstance(node.value, ast.Dict):
      return []
    for key in node.value.keys:
      if key is None or not isinstance(key, ast.Constant) or not isinstance(
          key.value, str):
        return []  # `**spread`, or a computed key
      keys.add(key.value)
  return sorted(keys) if seen_any else []


def _derive_output_keys(func: Callable[..., Any]) -> list[str]:
  """Output keys a tool declares: its pydantic return model's fields, else its
  literal dict returns' keys.

  Empty only when neither can be read — a tool whose returns are computed declares its
  keys nowhere, and the task's `outputs` mapping carries the contract instead.
  """
  ret = _hints(func).get("return")
  if _is_model(ret):
    return list(ret.model_fields.keys())  # type: ignore[union-attr]
  return _literal_return_keys(func)


def _warn_on_var_params(fn: Callable[..., Any], tool_name: str) -> None:
  """Warn when a tool's signature is `*args`/`**kwargs`.

  CES derives a tool's call schema FROM THE SIGNATURE, so a var-param tool declares
  no parameters and is silently dropped at deploy — the agent ships listing a tool
  that is not there. A warning rather than an error, so an app that already builds
  keeps building while its author is told what will happen to it.

  Args:
    fn: The decorated function.
    tool_name: Its deployed name (for the message).
  """
  try:
    params = inspect.signature(fn).parameters.values()
  except (TypeError, ValueError):  # not introspectable — nothing to say
    return
  bad = [
      f"*{p.name}" if p.kind is inspect.Parameter.VAR_POSITIONAL else f"**{p.name}"
      for p in params
      if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
  ]
  if bad:
    warnings.warn(
        f"@tool {tool_name!r} takes {', '.join(bad)}: CES builds a tool's schema"
        " from its signature, so a var-args/var-kwargs tool deploys with NO"
        " parameters and is silently dropped. Declare named parameters (or a"
        " pydantic model) instead.",
        UserWarning,
        stacklevel=3,
    )


def tool(
    _fn: Optional[Callable[..., Any]] = None,
    *,
    flow: Optional[str] = None,
    flows: Optional[list[str]] = None,
    name: Optional[str] = None,
    asynchronous: bool = False,
    timeout: Optional[int] = None,
):
  """Mark a function as a CXAS tool and attach it to one or more flows.

  Usage: `@tool`, `@tool(flow="my_flow")`, or `@tool(flows=["a", "b"])`. The tool's
  deployed name defaults to the function name (override with `name=`).

  `asynchronous=True` emits `executionType: ASYNCHRONOUS`, which tells CES to defer the
  body and answer the call with a placeholder. The task that fires it needs a matching
  `awaits=` block, or the engine reads that placeholder as a failure.

  `timeout=` is the seconds CES allows the BODY to run, emitted as `timeout: "<n>s"`.
  **The platform default is 60, and overrunning it is silent** — measured live, a
  deferred body at 60s or beyond never reports at all: no error, no failed result, and
  `on_timeout` never fires because there is no completion to time out. Declare it
  whenever a tool can take the better part of a minute. Nothing offline can catch the
  omission, since a body that sleeps for an hour validates clean.

  Args:
    _fn: The function, when used bare as `@tool`.
    flow: A single flow to attach to.
    flows: Several flows to attach to.
    name: Deployed tool name, defaulting to the function name.
    asynchronous: Emit `executionType: ASYNCHRONOUS`.
    timeout: Seconds the body may run. Omitted means the platform default of 60.

  Returns:
    The decorated function, or the decorator when called with arguments.

  Raises:
    ValueError: If `timeout` is not a positive whole number of seconds.
  """
  if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool)
                              or timeout <= 0):
    raise ValueError(
        f"tool(timeout={timeout!r}): a timeout is a positive whole number of SECONDS."
        " CES takes a duration string, which flows renders for you.")
  attach = tuple(flows or ([flow] if flow else []))

  def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    _warn_on_var_params(fn, name or fn.__name__)
    spec = ToolSpec(
        name=name or fn.__name__,
        func=fn,
        flows=attach,
        output_keys=_derive_output_keys(fn),
        asynchronous=asynchronous,
        timeout_seconds=timeout,
    )
    _shadow(spec)
    _REGISTRY[spec.name] = spec
    fn.__flows_tool__ = spec  # type: ignore[attr-defined]
    return fn

  return _wrap(_fn) if _fn is not None else _wrap


def _origin(spec: ToolSpec) -> tuple[Any, Any]:
  """Where a spec came from: source file and qualified name, or its rendered body.

  A GENERATED tool (`register_source_tool` — an OpenAPI wrapper, an A2A unwrap reader)
  has no function to locate, so its own source stands in. Two generators emitting the
  same body under one name really are the same tool; emitting different bodies is the
  collision this exists to see.
  """
  if spec.func is not None:
    code = getattr(spec.func, "__code__", None)
    return (getattr(code, "co_filename", None), getattr(spec.func, "__qualname__", None))
  return ("<generated>", spec.source)


def _shadow(spec: ToolSpec) -> None:
  """Remember the spec `spec` is about to displace, if it came from somewhere else.

  Re-importing one module under a second name re-registers the SAME function from the
  same file — the example loaders do exactly that — and replacing a spec with itself is
  not a collision worth recording.
  """
  prev = _REGISTRY.get(spec.name)
  if prev is None or _origin(prev) == _origin(spec):
    return
  kept = _SHADOWED.setdefault(spec.name, [])
  kept[:] = [s for s in kept if _origin(s) != _origin(prev)] + [prev]


def register_source_tool(
    name: str,
    source: str,
    *,
    flows: Optional[list[str]] = None,
    output_keys: Optional[list[str]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> ToolSpec:
  """Register a GENERATED tool from rendered source instead of a function.

  `@tool` derives everything by introspecting a function; a tool the SDK generates
  (an A2A unwrap reader, say) has source but no function to introspect. Registering
  it here puts it in the same registry, so `collect_tools` picks it up and the author
  does not have to thread a body through `App(tool_bodies=...)` by hand.
  """
  spec = ToolSpec(
      name=name,
      func=None,
      flows=tuple(flows or ()),
      output_keys=list(output_keys or []),
      source=source,
      meta=dict(meta or {}),
  )
  _shadow(spec)
  _REGISTRY[spec.name] = spec
  return spec


def registered_meta() -> dict[str, dict[str, Any]]:
  """`{tool_name: meta}` for every registered tool that carries generator metadata."""
  return {s.name: dict(s.meta) for s in _REGISTRY.values() if s.meta}


def clear_registry() -> None:
  """Drop all registered tools (used between builds / in tests)."""
  _REGISTRY.clear()
  _SHADOWED.clear()


def _models_in(annotation: Any) -> list[type]:
  """Pydantic models named anywhere in an annotation — bare, or inside `Optional[X]`,
  `list[X]`, `dict[str, X]` and the like."""
  if _is_model(annotation):
    return [annotation]
  out: list[type] = []
  for arg in typing.get_args(annotation):
    out.extend(_models_in(arg))
  return out


def _referenced_models(func: Callable[..., Any]) -> list[type]:
  """Pydantic models the emitted source needs, DEPENDENCY FIRST and deduplicated.

  A model's own fields are followed, not just the function's annotations: emitting
  `class Order(BaseModel): line: LineItem` without `LineItem` is source that raises
  `NameError` in the CES sandbox the first time the model is constructed — and only
  there, since nothing imports the emitted file.
  """
  ordered: list[type] = []
  visiting: set[type] = set()

  def visit(model: type) -> None:
    if model in ordered or model in visiting:
      return  # already emitted, or a cycle — a forward reference handles that
    visiting.add(model)
    for field_info in model.model_fields.values():  # type: ignore[attr-defined]
      for nested in _models_in(field_info.annotation):
        visit(nested)
    visiting.discard(model)
    ordered.append(model)

  for hint in _hints(func).values():
    for model in _models_in(hint):
      visit(model)
  return ordered


def _code_objects(obj: Any) -> list[types.CodeType]:
  """The code objects belonging to a function, or to a class's own methods.

  A class is callable but has no `__code__`, so reading one off it raises — and a class
  helper would take that path, because `_referenced_helpers` inlines classes too. Its
  methods carry the references that matter (a method reading a module-level constant
  needs that constant emitted just as much as a plain function does), so they are what
  gets walked. `classmethod`/`staticmethod` wrap the function in a descriptor, and a
  `property` hides up to three, so each is unwrapped rather than skipped.
  """
  if isinstance(obj, types.FunctionType):
    return [obj.__code__]
  if not isinstance(obj, type):
    return []
  out: list[types.CodeType] = []
  for member in vars(obj).values():
    for candidate in ((member.fget, member.fset, member.fdel)
                      if isinstance(member, property)
                      else (getattr(member, "__func__", member),)):
      if isinstance(candidate, types.FunctionType):
        out.append(candidate.__code__)
  return out


def _global_names(obj: Any) -> set[str]:
  """Every global name a function or class reads, including from nested functions.

  Nested code objects are walked because a helper referenced only from an inner
  function is exactly as undefined in the sandbox as one referenced directly.
  """
  seen: set[str] = set()
  stack: list[types.CodeType] = list(_code_objects(obj))
  while stack:
    code = stack.pop()
    seen.update(code.co_names)
    seen.update(code.co_freevars)
    for const in code.co_consts:
      if isinstance(const, types.CodeType):
        stack.append(const)
  return seen


def _module_assignment(module: Any, name: str) -> Optional[str]:
  """Source of a module-level `NAME = ...`, or None if it is not a simple assignment.

  Constants are inlined by source rather than by `repr(value)` so a cue map stays the
  readable literal the author wrote, and a value `repr` cannot round-trip (a compiled
  regex, say) is skipped instead of being emitted as something that will not parse.
  """
  try:
    src = inspect.getsource(module)
  except (OSError, TypeError):
    return None
  try:
    tree = ast.parse(src)
  except SyntaxError:
    return None
  lines = src.splitlines()
  for node in tree.body:
    targets = (node.targets if isinstance(node, ast.Assign)
               else [node.target] if isinstance(node, ast.AnnAssign) else [])
    if any(isinstance(t, ast.Name) and t.id == name for t in targets):
      return "\n".join(lines[node.lineno - 1:node.end_lineno])
  return None


def _referenced_helpers(func: Callable[..., Any]) -> list[str]:
  """Module-level helpers the emitted source needs, DEPENDENCY FIRST and deduplicated.

  Only the function's own source is rendered into the deployed file, so a module-level
  helper it calls is simply undefined there. Every migration works around that by hand:
  the helper is copied into the body, or written out twice when two callbacks need it,
  and the copies drift. This is the same problem `_referenced_models` already solves for
  pydantic models, extended to the functions and constants beside them.

  Inlined: functions, classes and simple assignments defined in the SAME module as
  `func`. Skipped: imports, builtins, pydantic models (`_referenced_models` owns those,
  and emitting them twice would be a redefinition), and anything whose source cannot be
  read. Skipping is silent and safe — it leaves exactly today's behaviour.
  """
  module = sys.modules.get(getattr(func, "__module__", "") or "")
  if module is None:
    return []
  ordered: list[str] = []
  emitted: set[str] = set()
  visiting: set[str] = set()

  def visit(name: str) -> None:
    if name in emitted or name in visiting:
      return  # already inlined, or a cycle — mutual recursion resolves at call time
    obj = getattr(module, name, None)
    if obj is None or name == getattr(func, "__name__", None):
      return
    if isinstance(obj, types.ModuleType) or hasattr(builtins, name):
      return
    if _is_model(obj):
      return  # `_referenced_models` emits these, and twice would redefine
    same_module = getattr(obj, "__module__", None) == func.__module__

    visiting.add(name)
    if isinstance(obj, (types.FunctionType, type)) and same_module:
      for nested in sorted(_global_names(obj)):
        visit(nested)
      try:
        source = textwrap.dedent(inspect.getsource(obj)).rstrip()
      except (OSError, TypeError):
        source = None
    else:
      source = _module_assignment(module, name)
    visiting.discard(name)

    if source:
      emitted.add(name)
      ordered.append(source)

  for referenced in sorted(_global_names(func)):
    visit(referenced)
  return ordered


def _clean_func_source(func: Callable[..., Any]) -> str:
  """The function source with its decorator(s) stripped and dedented.

  The `def`/`async def` line is located with `ast`, not by skipping lines that
  literally start with `@`: a decorator whose CALL is split over several lines

      @tool(
          flow="package_tracking",
      )
      def lookup(...): ...

  leaves `flow=...` and `)` behind under the line-prefix heuristic, and those
  fragments are a SyntaxError in the CES sandbox — which drops the tool at deploy
  with no error at all, so the agent ships without it and the first anyone hears
  of it is a live call. The line scan stays as the fallback for source that will
  not parse (a decorator applied to an already-transformed function, say).
  """
  src = textwrap.dedent(inspect.getsource(func))
  lines = src.splitlines()
  try:
    node = ast.parse(src).body[0]
  except (SyntaxError, IndexError, ValueError):
    node = None
  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
    start = node.lineno - 1  # `lineno` is the `def`; decorators carry their own
  else:
    start = 0
    while start < len(lines) and lines[start].lstrip().startswith("@"):
      start += 1
  return "\n".join(lines[start:]).rstrip() + "\n"


# CES runs each tool in an isolated sandbox; ONLY pydantic / typing / stdlib imports
# are allowed (no cross-module app imports). We deliberately do NOT emit
# `from __future__ import annotations` — with it, pydantic field types become strings
# that must be resolved at model creation, which fails in the sandbox and makes CES
# silently drop the tool.
_HEADER = (
    "from typing import Any, Dict, List, Literal, Optional\n\n"
    "from pydantic import BaseModel, Field\n"
)


def _is_union_annotation(node: ast.AST) -> bool:
  """True if the annotation AST is a Union — `X | Y`, `Union[...]`, or `Optional[...]`,
  including a STRING-quoted form (`"dict | Model"`, common once forward refs are quoted).
  """
  if isinstance(node, ast.Constant) and isinstance(node.value, str):
    try:                                    # a quoted annotation: parse its contents
      return _is_union_annotation(ast.parse(node.value, mode="eval").body)
    except SyntaxError:
      return False
  if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
    return True
  if isinstance(node, ast.Subscript):
    base = node.value
    nm = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
    return nm in ("Union", "Optional")
  return False


def _dict_return_if_union(src: str, entry: str) -> str:
  """Rewrite the entry function's return annotation to plain `dict` when it is a UNION
  (`X | Y`, `Union[...]`, `Optional[...]`, or a string-quoted one).

  CES registers a tool's output from the entry function's return annotation, and it
  SILENTLY DROPS the tool — no `function_response`, no error, dead air — when that
  annotation is a Union that includes a custom class (e.g. `dict | ShipmentStatusDomain`;
  ces-probes 153, flows #513/#556). A flows tool always hands back a dict (a pydantic
  return is `model_dump()`ed and its real keys are declared separately via
  `_DECLARED_OUTPUTS`), so `dict` is the correct CES-facing shape and nothing downstream
  reads this annotation. Scoped to Union returns only: a plain `-> dict` or a bare
  `-> Model` are left untouched. No-op on a body that will not parse.
  """
  try:
    tree = ast.parse(src)
  except SyntaxError:
    return src
  node = next((n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == entry), None)
  if node is None or node.returns is None or not _is_union_annotation(node.returns):
    return src
  ret = node.returns
  # `col_offset`/`end_col_offset` are UTF-8 BYTE offsets, so splice on the encoded bytes
  # (character indexing corrupts a line that has a multibyte char before the annotation).
  data = src.encode("utf-8")
  bstarts = [0]
  for ln in src.splitlines(keepends=True):
    bstarts.append(bstarts[-1] + len(ln.encode("utf-8")))
  begin = bstarts[ret.lineno - 1] + ret.col_offset
  end = bstarts[ret.end_lineno - 1] + ret.end_col_offset
  return (data[:begin] + b"dict" + data[end:]).decode("utf-8")


def render_tool(spec: ToolSpec) -> str:
  """Render a tool's `python_code.py` — inlined pydantic models + the function.

  For a pydantic-returning tool, appends a `_DECLARED_OUTPUTS` dict literal so the
  framework validator's static output-key check sees the returned fields (the
  `Model(...).model_dump()` gap) without the author writing a shim.

  A source-registered tool is already a complete module (it carries its own imports),
  so it is returned verbatim.
  """
  if spec.source is not None:
    return spec.source
  body = render_callable(spec.func)
  if spec.func is not None:
    body = _dict_return_if_union(body, spec.func.__name__)
  parts: list[str] = [_HEADER, body]
  if spec.output_keys:
    decl = ", ".join(f"{k!r}: None" for k in spec.output_keys)
    parts.append(
        "# Declared output keys (derived from the pydantic return model) so the\n"
        "# framework validator sees them even though the body returns model_dump().\n"
        f"_DECLARED_OUTPUTS = {{{decl}}}\n"
    )
  return "\n".join(parts)


def render_callable(func: Callable[..., Any]) -> str:
  """The self-contained BODY of a function: what it references, then its own source.

  Referenced pydantic models first, then module-level helpers it calls, then the
  function. Both are dependency-first, so a model or helper is always defined above the
  thing that uses it — the sandbox imports nothing, so order is the only thing keeping
  the emitted file from raising `NameError` on its first call.

  No import header — callers prepend their own (a `@flows.tool` uses `_HEADER`; an
  author callback uses a CES-callback header). Shared by `render_tool` and the
  author-customization emitter so both inline the same way and strip decorators
  identically.
  """
  parts: list[str] = []
  for model in _referenced_models(func):
    parts.append(textwrap.dedent(inspect.getsource(model)).rstrip() + "\n")
  for helper in _referenced_helpers(func):
    parts.append(helper + "\n")
  parts.append(_clean_func_source(func))
  return "\n".join(parts)


def collect_tools(flow_ids, names=()) -> dict[str, str]:
  """`{tool_name: rendered_source}` for every registered tool attached to any of
  `flow_ids` (a tool with no explicit flow attaches to all).

  `names` additionally pulls a registered tool in BY NAME, whatever it is attached to.
  An AGENT-scoped tool (a `HostRouter.extra_tools` FAQ lookup, say) belongs to no flow
  — no task fires it, no slot sets it — so flow attachment alone leaves its body
  unemitted and the agent ships listing a tool that does not exist.
  """
  return {name: render_tool(spec)
          for name, spec in resolve_specs(flow_ids, names).items()}


def resolve_specs(flow_ids, names=()) -> dict[str, ToolSpec]:
  """`{tool_name: spec}` for the tools of ONE app, resolved against its own flows.

  Selection is unchanged: a tool attached to one of `flow_ids`, a tool attached to no
  flow at all (which attaches to every flow), or one named in `names`.

  What is new is which spec answers to a name several functions registered. The registry
  keeps one entry per name for the whole process, so the last import won and an app could
  emit a body from an unrelated module. Here the candidates are the current entry plus
  everything it displaced, and one ATTACHED TO THIS APP's flows is preferred — so two
  independent apps may each define a `lookup_bill` and each still gets its own.

  Deliberately no error when several candidates attach. Flow ids are reused across apps
  as freely as tool names are, so "attached to a flow this app names" does not prove two
  functions are in the same app, and refusing them would fail builds that are fine. This
  only ever changes the outcome where the old rule was certainly wrong: the process-wide
  entry belongs to another app's flow and a displaced one belongs to this one.
  """
  wanted = set(flow_ids)
  by_name = set(names or ())
  out: dict[str, ToolSpec] = {}
  for name, current in _REGISTRY.items():
    # Newest first, so a tie within either tier keeps the old last-wins answer.
    candidates = [current, *reversed(_SHADOWED.get(name, []))]
    # Attached to a flow this app names, then attached to NO flow (which is attached to
    # every flow, including this app's). Both tiers search the displaced specs, so it
    # does not matter which of the two was imported last: an app that owns one of them
    # gets it either way.
    chosen = (next((s for s in candidates if wanted & set(s.flows)), None)
              or next((s for s in candidates if not s.flows), None))
    if chosen is not None:
      out[name] = chosen
    elif name in by_name:
      # Pulled in by name whatever it is attached to. Nothing about the app can choose
      # between candidates here, so this keeps last-wins.
      out[name] = current
  return out


def registered_output_keys() -> dict[str, list[str]]:
  """`{tool_name: [output_keys]}` for all registered tools (for validation wiring)."""
  return {s.name: list(s.output_keys) for s in _REGISTRY.values()}


# What CES puts in a tool body's namespace. A body may use these without defining
# them; everything else it references has to come with it. Confirmed live by
# ces-probes 45/46, which dumped the sandbox namespace:
#
#     globals: ...,async_tools,ces_internal,ces_requests,context,requests,tools,...
#
# That dump is the whole list. Names from other CES surfaces (`get_variable`, `Part`,
# `Blob`, a pre-imported `json`) are NOT in it — a tool body that wants json imports it,
# which is why the emitted header carries only `typing`. Add to this only what a probe
# has actually seen, or the check stops catching the thing it exists for.
_SANDBOX_GLOBALS = frozenset({
    "context", "tools", "async_tools", "requests", "ces_requests", "ces_internal",
})


def _bound_names(tree: ast.AST) -> set[str]:
  """Every name the rendered module binds anywhere — imports, defs, assignments.

  Scope-insensitive on purpose. This feeds a check that only ever reports a name bound
  NOWHERE, so being generous about what counts as bound can only lose a finding, never
  invent one.

  Args:
    tree: Parsed rendered module.

  Returns:
    The set of bound names.
  """
  bound: set[str] = set()
  for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
      bound.add(node.name)
      args = getattr(node, "args", None)
      if args is not None:
        for a in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                  + [args.vararg, args.kwarg]):
          if a is not None:
            bound.add(a.arg)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
      for alias in node.names:
        bound.add((alias.asname or alias.name).split(".")[0])
    elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
      bound.add(node.id)
    elif isinstance(node, ast.ExceptHandler) and node.name:
      bound.add(node.name)
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
      bound.update(node.names)
    # `match` binds through the PATTERN, not through a Store-context Name, so a name
    # captured by a case arm looks unbound to the walk above. Missing a binding is the
    # one error this function must not make: it does not lose a finding, it INVENTS one.
    elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
      bound.add(node.name)
    elif isinstance(node, ast.MatchMapping) and node.rest:
      bound.add(node.rest)
  # PEP 695 type parameters (`def f[T](...)`, `class C[T]`) bind their names too, and
  # `ast.TypeVar` only exists on 3.12+, so it is looked up rather than referenced.
  _type_param = getattr(ast, "TypeVar", None)
  if _type_param is not None:
    for node in ast.walk(tree):
      if isinstance(node, (_type_param, getattr(ast, "ParamSpec", ()),
                           getattr(ast, "TypeVarTuple", ()))):
        bound.add(node.name)
  return bound


def unresolved_globals(spec: "ToolSpec") -> list[str]:
  """Names the emitted body READS but nothing carries — a sandbox `NameError`.

  `render_callable` inlines the referenced pydantic models and the function source, and
  nothing else. A module-level constant, a helper function or a module-level import the
  body closes over is simply left behind, and the tool dies on its first call with
  `Python function execution failed: name 'X' is not defined`. Nothing catches it today:
  the build is happy, the emitted file is syntactically fine, and the failure only
  appears once a caller reaches that tool.

  Cost of not having this, measured: one deploy and one live drive on ces-probes 86 to
  find a `SENTINEL` the author had defined ten lines above the function.

  Precise rather than exhaustive. A name is reported only when all of:
    * the rendered body reads it and binds it nowhere,
    * it is not a builtin and not one of the CES sandbox globals,
    * it RESOLVES in the author's own module — so it is a real thing being left behind
      rather than a typo, a forward reference, or something this analysis misread.

  Args:
    spec: The registered tool.

  Returns:
    Sorted names that will not resolve in the sandbox.
  """
  if spec.func is None or spec.source is not None:
    return []  # a generated tool carries its own complete module
  try:
    tree = ast.parse(render_tool(spec))
  except (OSError, TypeError, SyntaxError, ValueError):
    return []
  bound = _bound_names(tree) | _SANDBOX_GLOBALS | set(dir(builtins))
  author_globals = getattr(spec.func, "__globals__", {}) or {}
  inert = _unevaluated_annotation_nodes(tree)
  missing = {
      n.id for n in ast.walk(tree)
      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
      and id(n) not in inert
      and n.id not in bound and n.id in author_globals
  }
  return sorted(missing)


def _unevaluated_annotation_nodes(tree: ast.AST) -> set[int]:
  """`id()`s of nodes in annotations Python never evaluates: LOCAL variable ones.

  `x: Model = make()` inside a function body does not evaluate `Model` at all — the
  annotation is discarded — so a tool that reads it runs perfectly well without `Model`
  being carried across, and reporting it blocks a working deploy.

  Only local ones. A CLASS body's `x: Model` is evaluated and stored in
  `__annotations__` (the inlined pydantic models are exactly this), and an ARGUMENT
  annotation is evaluated when the `def` executes. Both genuinely need the name, so
  both are left in the check.

  Args:
    tree: Parsed rendered module.

  Returns:
    The set of `id()`s to ignore.
  """
  inert: set[int] = set()

  def walk(node, in_function: bool):
    for child in ast.iter_child_nodes(node):
      if isinstance(child, ast.AnnAssign) and in_function and child.annotation:
        inert.update(id(n) for n in ast.walk(child.annotation))
      if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
        walk(child, True)
      elif isinstance(child, ast.ClassDef):
        walk(child, False)          # a class body re-enters evaluated territory
      else:
        walk(child, in_function)

  walk(tree, False)
  return inert


def registered_unresolved_globals() -> dict[str, list[str]]:
  """`{tool_name: [names]}` for every registered tool that leaves a global behind."""
  found = {s.name: unresolved_globals(s) for s in _REGISTRY.values()}
  return {name: names for name, names in found.items() if names}


def registered_async_tools() -> set[str]:
  """Names of tools declared `@tool(asynchronous=True)` — emitted with
  `executionType: ASYNCHRONOUS`."""
  return {s.name for s in _REGISTRY.values() if s.asynchronous}


def registered_tool_timeouts() -> dict[str, int]:
  """Seconds per tool declared `@tool(timeout=…)`, emitted as `timeout: "<n>s"`.

  Absent means the platform default of 60. Kept as a mapping rather than a set because
  unlike `asynchronous` this is not a flag — the fan-out lowering has to read the
  author's number back to size the group's patience against it.
  """
  return {s.name: s.timeout_seconds
          for s in _REGISTRY.values() if s.timeout_seconds}
