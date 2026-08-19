"""Packing a framework tool to bytecode must be transparent, verifiable and reversible.

The saving is real but the failure modes are quiet: a packed module that loses its
signature crashes the whole app with no log naming the tool (ces-probes 165), one that
silently takes its source fallback reports a successful deploy and delivers none of the
speed, and one that defeats the drift check does so by PASSING. Each of those gets a test.

Everything is packed for `sys.version_info[:2]` rather than for the production target, so
the suite exercises the real code path on whatever interpreter CI runs. The production
target is asserted separately.
"""

from __future__ import annotations

import ast
import re
import sys
from typing import Any

import pytest

from flows.engine import blessed_source as bs
from flows.engine import packing

HERE_PY = sys.version_info[:2]

SAMPLE = '''"""A tool that looks like a framework tool."""

import json as json_lib
from typing import Any

_LOOKUP = {"a": 1, "b": 2}


def _helper(x: int) -> int:
  """Something the entry point needs."""
  return x * 2 + len(_LOOKUP)


def sample_tool(input_data: dict[str, Any]) -> dict[str, Any]:
  """Run the thing.

  Longer prose that the schema depends on.
  """
  return {"ok": True, "n": _helper(int(input_data.get("n", 0))), "j": json_lib.dumps([1])}
'''


def _pack(src: str = SAMPLE, entry: str = "sample_tool") -> str:
  return packing.pack(src, entry, target_py=HERE_PY)


def _exec(module_src: str, injected: dict | None = None) -> dict:
  ns: dict = dict(injected or {})
  exec(compile(module_src, "python_code.py", "exec"), ns)  # noqa: S102
  return ns


def _with_foreign_magic(packed: str) -> str:
  """Rewrite the embedded magic so the load-time gate sees a foreign interpreter.

  Rewrites the whole assignment rather than splicing bytes into the source: an earlier
  version pasted an escaped `\\x00` into the module text and produced a literal NUL, which
  `compile()` rejects outright -- a broken test that looks like a broken loader.
  """
  # A LAMBDA replacement, not a plain string: re.sub interprets backslash escapes in a
  # string replacement and rejects `\x` as a bad escape.
  out = re.sub(r"^_MAGIC = .*$", lambda _m: "_MAGIC = b'\\xff\\xff\\r\\n'", packed,
               count=1, flags=re.MULTILINE)
  assert out != packed, "failed to perturb _MAGIC; the test would pass vacuously"
  return out


# --- reversibility: the drift check depends on it ---------------------------


def test_unpack_recovers_the_exact_source():
  assert packing.unpack(_pack()) == SAMPLE


def test_is_packed_discriminates():
  assert not packing.is_packed(SAMPLE)
  assert packing.is_packed(_pack())
  assert packing.unpack(SAMPLE) is None


def test_packing_is_deterministic():
  """Byte-identical emit tests are worthless if the packer is not a pure function."""
  assert _pack() == _pack()


# --- transparency: same behaviour, same namespace ---------------------------


def test_packed_module_exposes_the_same_public_names():
  plain = _exec(SAMPLE)
  packed = _exec(_pack())
  interesting = {n for n in plain if not n.startswith("__")}
  assert interesting <= set(packed)


def test_packed_entry_behaves_identically():
  plain = _exec(SAMPLE)["sample_tool"]
  packed = _exec(_pack())["sample_tool"]
  for n in (0, 1, 7):
    assert plain({"n": n}) == packed({"n": n})


def test_packed_entry_is_the_blob_not_the_stub():
  """ces-probes 166: the exec must rebind the stub, or the agent answers with placeholders."""
  entry = _exec(_pack())["sample_tool"]
  assert entry({"n": 2}) == {"ok": True, "n": 6, "j": "[1]"}


def test_blob_reaches_injected_globals():
  """The engine reaches for CES-injected `context`/`tools`, so exec must target globals()."""
  src = SAMPLE.replace(
      'return {"ok": True',
      'context.append("seen")\n  return {"ok": True')  # noqa: F821 - injected at exec
  sink: list = []
  ns = _exec(packing.pack(src, "sample_tool", target_py=HERE_PY), {"context": sink})
  ns["sample_tool"]({"n": 1})
  assert sink == ["seen"]


def test_loads_via_bytecode_not_the_fallback():
  """A silent fall back to source is a deploy that reports success and saves nothing."""
  assert _exec(_pack())["_FLOWS_LOADED_VIA"] == "bytecode"


def test_falls_back_to_source_on_interpreter_mismatch():
  """A future CES upgrade must degrade to today's cost, not break every agent."""
  ns = _exec(_with_foreign_magic(_pack()))
  assert ns["_FLOWS_LOADED_VIA"] == "source"
  assert ns["sample_tool"]({"n": 2}) == {"ok": True, "n": 6, "j": "[1]"}


# --- shape: what CES parses -------------------------------------------------


def test_entry_is_the_first_top_level_annotated_def():
  """ces-probes 165 + the undispatchable_tools / docstring_sig gates all read it."""
  defs = [n for n in ast.parse(_pack()).body if isinstance(n, ast.FunctionDef)]
  assert defs and defs[0].name == "sample_tool"
  assert defs[0].returns is not None


def test_signature_and_docstring_survive_verbatim():
  """The docstring_sig push gate compares the stub's Args block to its parameters."""
  original = next(n for n in ast.parse(SAMPLE).body
                  if isinstance(n, ast.FunctionDef) and n.name == "sample_tool")
  stub = next(n for n in ast.parse(_pack()).body
              if isinstance(n, ast.FunctionDef) and n.name == "sample_tool")
  assert ast.unparse(stub.args) == ast.unparse(original.args)
  assert ast.unparse(stub.returns) == ast.unparse(original.returns)
  assert ast.get_docstring(stub, clean=False) == ast.get_docstring(original, clean=False)


def test_node_count_collapses_on_the_real_engine():
  """The entire point: a literal is one node however long it is.

  Measured on the ENGINE, not on `SAMPLE`. The loader is a fixed ~160 nodes, so packing a
  toy module makes it bigger -- the collapse is a property of scale, and asserting it on a
  fixture would either fail or force the fixture to grow until it passed.
  """
  files, path = _framework_map()
  before = sum(1 for _ in ast.walk(ast.parse(files[path])))
  after = sum(1 for _ in ast.walk(ast.parse(
      packing.pack(files[path], "slot_filling_engine", target_py=HERE_PY))))
  assert before > 40_000, "engine unexpectedly small; re-check what this is measuring"
  assert after < 500


def test_refuses_to_pack_for_a_foreign_interpreter():
  """Refuse at BUILD time. The alternative is a deploy that reports success and is slow."""
  with pytest.raises(RuntimeError, match="no version check"):
    packing.pack(SAMPLE, "sample_tool", target_py=(3, 0))


def test_refuses_a_missing_entry_point():
  with pytest.raises(ValueError, match="no top-level"):
    packing.pack(SAMPLE, "not_a_function", target_py=HERE_PY)


def test_annotations_survive_as_REAL_OBJECTS_not_strings():
  """The bug that broke production, as a test.

  `packing.py` carries `from __future__ import annotations`, and a bare `compile()`
  INHERITS the caller's __future__ flags -- so the marshalled code object came out with
  CO_FUTURE_ANNOTATIONS and every annotation became a string. CES builds a
  `pydantic.TypeAdapter` per call and evaluates those strings in a namespace that has no
  `Any`, so every engine call died with `NameError: name 'Any' is not defined`, surfacing
  as `KeyError('result')` and an agent that asked callers for their account number.

  It hid from every probe because probe entries were annotated `-> dict`: `dict` is a
  builtin and resolves even as a string. `Any` is not. The fix is `dont_inherit=True`.
  """
  src = SAMPLE.replace("def sample_tool(input_data: dict[str, Any]) -> dict[str, Any]:",
                       "def sample_tool(input_data: dict[str, Any]) -> dict[str, Any]:")
  ns = _exec(packing.pack(src, "sample_tool", target_py=HERE_PY))
  ann = ns["sample_tool"].__annotations__
  assert ann, "entry lost its annotations entirely"
  strings = {k: v for k, v in ann.items() if isinstance(v, str)}
  assert not strings, (
      "annotations came back as strings %s -- the __future__ flag leaked into the "
      "compiled blob again; compile() needs dont_inherit=True" % strings)
  assert ann["input_data"] == dict[str, Any]


def test_source_fallback_also_yields_real_annotations():
  """The fallback path must not be the only one that happens to be correct."""
  ns = _exec(_with_foreign_magic(_pack()))
  assert ns["_FLOWS_LOADED_VIA"] == "source"
  assert not any(isinstance(v, str) for v in ns["sample_tool"].__annotations__.values())


def test_magic_mismatch_takes_the_source_fallback():
  """The gate is the bytecode MAGIC NUMBER, which is what CPython validates on a .pyc.

  marshal performs no equivalent check of its own, so without this an incompatible blob
  would load silently and misbehave later rather than degrading to source.
  """
  ns = _exec(_with_foreign_magic(_pack()))
  assert ns["_FLOWS_LOADED_VIA"] == "source"
  assert ns["sample_tool"]({"n": 2}) == {"ok": True, "n": 6, "j": "[1]"}


def test_build_host_minor_version_is_checked():
  """ces-probes 163 measured `py=3.12.5`. If CES moves, this is the one line to change.

  Minor precision, deliberately. An exact micro pin was tried and was wrong: it was added
  to explain a live failure that turned out to be the __future__ annotations leak, and
  3.12.13 bytecode demonstrably runs correctly on CES's 3.12.5. Correctness is enforced by
  the magic-number gate, not by this.
  """
  assert packing.TARGET_PY == (3, 12)


# --- the drift contract -----------------------------------------------------


def _framework_map() -> tuple[dict, str]:
  files = {f["path"]: f["content"] for f in bs.framework_tool_files()}
  return files, "tools/slot_filling_engine/python_function/python_code.py"


def test_packed_engine_still_passes_the_drift_check():
  files, path = _framework_map()
  files[path] = packing.pack(files[path], "slot_filling_engine", target_py=HERE_PY)
  assert path not in bs.verify_files(files).mismatched


def test_tampering_inside_a_packed_engine_is_still_caught():
  """Packing must not become a way to smuggle an off-manifest engine past the gate."""
  files, path = _framework_map()
  files[path] = packing.pack(files[path] + "\n_SNEAKY = 1\n", "slot_filling_engine",
                             target_py=HERE_PY)
  assert path in bs.verify_files(files).mismatched


def test_real_engine_round_trips():
  files, path = _framework_map()
  packed = packing.pack(files[path], "slot_filling_engine", target_py=HERE_PY)
  assert packing.unpack(packed) == files[path]
  assert len(packed) < len(files[path])
