"""Ship a framework tool to CES as precompiled bytecode instead of source.

CES re-parses a python tool's module on EVERY invocation. The slot-filling engine is 555KB
and 49,860 AST nodes, and that parse is pure overhead on the path to the first spoken word:
it happens before any filler can be armed, on every one of the ~13 engine calls in a
session. Measured on CES at engine node-parity, packing removes 98-153ms per invocation
(`ces-probes/probes/164-bytecode-latency`).

The saving comes from one property: a string literal is a single AST node however long it
is. Packing the module body into one base64 literal takes it from 49,860 nodes to a few
dozen. This is the same trick, and the same reasoning, as `emit/scaffold._starter_dag_code`
emitting `json.loads(_CONFIG)` rather than a dict literal.

Four platform facts shape the output, each measured rather than assumed:

* `165-pure-blob-no-def` — CES resolves a tool's schema by PARSING the source. A module with
  no ast-visible annotated `def` does not degrade to a missing tool; it takes the whole app
  down. So the entry point is emitted as a real stub.
* `166-exec-overwrites-def` — CES resolves the function from the module namespace at CALL
  time, so the blob's `exec` can rebind that stub. No wrapper, so no extra frame and no risk
  of the engine's internals recursing into a wrapper.
* `163-tool-bytecode-loader` — the sandbox is Python 3.12.5 and permits `marshal` and
  `zlib`; a blob exec'd into `globals()` reaches the CES-injected `context` and `tools`,
  which a private namespace would not.
* Encoding is base64, measured against the real engine: base64 literals make the artifact
  499,962 B against 555,399 B of source today, where `repr()` literals would be 1,072,638 B.
  Base64 costs 0.2ms more to decode than `repr`. Base85 was rejected outright — its decoder
  is pure Python and cost more than the compile it was replacing.

The packed file also carries the ORIGINAL source, compressed. That is not redundancy for
its own sake: it is what lets `verify_app_dir` recover the exact blessed bytes and hash them
against the manifest, so packing does not blind the drift check, and it doubles as the
fallback if the deployed interpreter ever stops matching the one that built the blob.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import marshal
import sys
import zlib

#: Marker constant emitted into packed modules. `is_packed` keys on this rather than on a
#: filename or a size heuristic, so an unpacked engine is never mistaken for a packed one.
MARKER = "_FLOWS_PACKED"

#: The interpreter CES runs, measured by probe 163 (`py=3.12.5`). Checked at MINOR
#: precision as a build-host sanity check only -- the real compatibility gate is the
#: bytecode MAGIC NUMBER below, which is what CPython itself validates.
#:
#: An exact major.minor.micro pin was tried here and was WRONG. It was introduced to
#: explain a live failure -- bytecode built on 3.12.13 broke on CES's 3.12.5 -- but the
#: real cause was `from __future__ import annotations` leaking into the compiled code (see
#: `pack`). 3.12.13 bytecode runs correctly on 3.12.5, so the micro pin was masking a bug
#: rather than preventing an incompatibility, and it made building impossible off a
#: bit-identical host for no gain.
TARGET_PY = (3, 12)

_TEMPLATE = '''"""{title}

GENERATED -- do not edit. The readable source is embedded below in `_SRC` and is the
authoritative copy; see `flows/engine/packing.py`.

This module is precompiled bytecode because CES re-parses a tool's source on every
invocation, which costs 98-153ms per call at this module's size and lands squarely in
front of the first spoken word (ces-probes 164).
"""

import base64
import importlib.util
import marshal
import sys
import zlib

{imports}

{marker} = True

#: The bytecode MAGIC NUMBER of the interpreter that built the blob. This is the check
#: CPython itself performs on a `.pyc` header, and `marshal` performs no equivalent of its
#: own -- so without it, incompatible bytecode would load silently and misbehave later.
#: A mismatch takes the source fallback.
_MAGIC = {magic!r}

#: zlib(marshal(code)), base64. ONE ast node -- the entire point of this file.
_CODE = {code_b64!r}

#: zlib(utf-8 source), base64. The authoritative original, kept so `verify_app_dir` can
#: recover and hash the exact blessed bytes, and so a version mismatch degrades to today's
#: behaviour instead of breaking the agent.
_SRC = {src_b64!r}


{stub}


# Installed at MODULE level, with a BARE exec. Do not wrap this in a helper.
#
# ces-probes/166 proved the rebinding works in exactly this shape: a module-level exec
# replacing an ast-visible stub. The first cut of this emitter wrapped the same exec in a
# `_flows_load()` helper instead -- so the probe validated one shape and the emitter
# shipped another. Deployed, every engine call then failed with CES's error envelope
# (`before_model_CRASH KeyError('result')` on every turn) and the agent degraded into a
# plain model asking the caller for their account number. Offline it was undetectable:
# the app's full 89-journey suite passed against the packed build, because a local
# `exec(src, ns)` uses ONE mapping and the helper is then equivalent.
#
# `globals()` explicitly, because that is the form ces-probes/166 actually validated, and
# because the engine reaches for the CES-injected `context` and `tools`: a blob whose
# functions resolve names somewhere other than the module globals would see neither.
#
# Two other combinations have been tried on the real agent and both failed: `globals()`
# from inside a `_flows_load()` helper, and a BARE module-level exec. Probe 166 proved
# module-level-plus-globals(); shipping either variation of it was the mistake, twice.
try:
  if importlib.util.MAGIC_NUMBER != _MAGIC:
    raise ValueError("bytecode magic mismatch")
  exec(marshal.loads(zlib.decompress(base64.b64decode(_CODE))), globals())  # noqa: S102
  _FLOWS_LOADED_VIA = "bytecode"
except Exception:  # noqa: BLE001 - any failure must degrade to source, never break the app
  # dont_inherit=True here too. This compile runs INSIDE the packed module, so without it
  # the fallback inherits whatever __future__ flags the host used to compile this file --
  # the same leak that broke the bytecode path, arriving by a different route.
  exec(compile(zlib.decompress(base64.b64decode(_SRC)).decode("utf-8"),  # noqa: S102
               "python_code.py", "exec", 0, True), globals())
  _FLOWS_LOADED_VIA = "source"
'''


def _entry_def(tree: ast.Module, entry: str) -> ast.FunctionDef:
  """The top-level `def entry`, or raise."""
  for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == entry:
      return node
  raise ValueError(f"no top-level `def {entry}` to pack")


def _stub_for(fn: ast.FunctionDef) -> str:
  """A standalone `def` carrying the entry's exact signature and docstring.

  This is the only thing CES parses, so it has to satisfy every source-level gate:
  `undispatchable_tools` wants a top-level def with a non-None return annotation, and the
  `docstring_sig` push gate compares the FIRST top-level funcdef's `Args:` block against its
  parameters. Copying the signature and docstring verbatim from the real function makes both
  true by construction rather than by a rule someone has to remember.

  The body raises: if both the bytecode and the source fallback somehow failed to load, a
  loud error beats a tool that silently answers with a placeholder.
  """
  doc = ast.get_docstring(fn, clean=False)
  body: list[ast.stmt] = []
  if doc is not None:
    body.append(ast.Expr(value=ast.Constant(value=doc)))
  body.append(
      ast.Raise(exc=ast.Call(
          func=ast.Name(id="RuntimeError", ctx=ast.Load()),
          args=[ast.Constant(value=f"{fn.name}: packed module body failed to load")],
          keywords=[]), cause=None))
  stub = ast.FunctionDef(
      name=fn.name, args=fn.args, body=body, decorator_list=[],
      returns=fn.returns, type_comment=None, type_params=[])
  return ast.unparse(ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[])))


def _imports(tree: ast.Module) -> str:
  """The module's top-level imports, replayed in the packed file.

  The stub's annotations are evaluated when its `def` executes, which happens BEFORE the
  blob is loaded. `dict[str, Any]` would raise NameError without `Any` in scope, so the
  imports have to precede it. They are a handful of nodes and cost nothing measurable.
  """
  return "\n".join(
      ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)))


def pack(source: str, entry: str, title: str = "Packed framework tool.",
         target_py: tuple[int, ...] = TARGET_PY) -> str:
  """Return `source` as a packed module exposing `entry` with an identical signature.

  Deterministic for a fixed source and interpreter, so byte-identical-emit tests stay
  meaningful.

  Refuses to build bytecode for an interpreter other than the one running, because the
  result would be a module that silently takes its source fallback on every invocation --
  a deploy that reports success and delivers none of the saving. `target_py` exists so the
  tests can exercise packing on whatever interpreter they happen to run under; production
  callers leave it at `TARGET_PY` and are expected to build on a matching host.
  """
  if sys.version_info[:len(target_py)] != tuple(target_py):
    raise RuntimeError(
        "refusing to pack on python %s for %s: marshal carries no version check, so "
        "mismatched bytecode loads fine and then misbehaves at call time. Build on the "
        "matching interpreter."
        % (".".join(str(v) for v in sys.version_info[:len(target_py)]),
           ".".join(str(v) for v in target_py)))
  tree = ast.parse(source)
  fn = _entry_def(tree, entry)
  # `dont_inherit=True`, and it is load-bearing. A bare `compile()` inherits the __future__
  # flags of THIS module, which has `annotations` on -- so the packed code object would give
  # every function STRING annotations, while the source fallback (compiled inside the
  # deployed module, which has no future import) gives real objects. CES resolves a tool's
  # arguments through `pydantic.TypeAdapter(function)` at CALL time and evaluates a string
  # annotation against a namespace that does not hold the module's imports, so
  # `dict[str, Any]` raises `NameError: name 'Any' is not defined`. The module loads, every
  # symbol is present, and every invocation fails before the function is even entered.
  code = compile(source, "python_code.py", "exec", 0, True)
  packed = _TEMPLATE.format(
      title=title,
      imports=_imports(tree),
      marker=MARKER,
      magic=importlib.util.MAGIC_NUMBER,
      code_b64=base64.b64encode(zlib.compress(marshal.dumps(code), 9)),
      src_b64=base64.b64encode(zlib.compress(source.encode("utf-8"), 9)),
      stub=_stub_for(fn),
  )
  _assert_shape(packed, entry)
  return packed


def _assert_shape(packed: str, entry: str) -> None:
  """Fail at BUILD time if the packed module would not schematize on CES.

  Probe 165's failure mode is an app that returns nothing but the crash envelope, with no
  log naming the tool. That is expensive to diagnose from a deployment and trivial to catch
  here.
  """
  tree = ast.parse(packed)
  defs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
  if not defs or defs[0].name != entry:
    raise RuntimeError(f"packed module's first top-level def must be {entry!r}")
  if defs[0].returns is None:
    raise RuntimeError(f"packed {entry!r} lost its return annotation (probe 23)")


def is_packed(text: str) -> bool:
  """True if `text` is a module produced by `pack`."""
  return MARKER in text and "_SRC = " in text


def unpack(text: str) -> str | None:
  """Recover the original source embedded in a packed module, or None if not packed.

  Lets the drift gates keep hashing SOURCE while the deployed artifact is bytecode, so
  packing does not cost the integrity guarantee.
  """
  if not is_packed(text):
    return None
  for node in ast.parse(text).body:
    if (isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "_SRC"
        and isinstance(node.value, ast.Constant)):
      return zlib.decompress(base64.b64decode(node.value.value)).decode("utf-8")
  return None
