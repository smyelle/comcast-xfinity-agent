"""Emit the slot-filling engine tool as precompiled bytecode, on a build host that can.

WHY. CES re-parses a python tool's module on EVERY invocation. The engine is 555KB and
49,860 AST nodes, and that parse sits in front of the first spoken word on each of the ~11
engine calls in a session. `flows.engine.packing` compresses the module body into a single
base64 literal -- one AST node, ~161 in the emitted file -- and measured on this agent it
took an engine call from 165ms to 74ms and from 169ms to 70ms.

THE INTERPRETER PROBLEM, which is the whole reason this file is not three lines in
`build.py`. `marshal` carries no version check: bytecode built by the wrong interpreter
loads without complaint and then misbehaves at call time, so `packing.pack` refuses to
build for an interpreter other than the one running. CES runs python 3.12.5; this repo's
venv is 3.12's successor and so is CI, so an in-process pack would refuse on every
developer machine and `--pack-engine` could not be the default.

So the packing step -- and ONLY the packing step -- runs under a 3.12 interpreter of its
own, found through `uv`, which already gates every other python command in this repo.
`flows.engine.packing` is stdlib-only and self-contained, so that interpreter needs no
project venv and no dependency install: it is handed the module's path and loads it from
there. On this machine the hop costs about a tenth of a second.

WHAT HAPPENS WHEN THERE IS NO 3.12. The build emits source and says so, in two places that
cannot be missed: a banner on stdout, and `engine_packing: "skipped"` in
`build_manifest.json`, which is distinct from both `packed` and `off`. That is not
politeness. An app was deployed to CES named `comcast-DEMO-packed` while serving source,
and nothing in the artifact contradicted the name; the manifest is what makes that
impossible to repeat.

Also runs AS a script, which is the 3.12 child half:

    python engine_packing.py --packing <flows/engine/packing.py> --target <python_code.py>
"""

import argparse
import os
import shutil
import subprocess
import sys

#: The one packable framework tool, its entry point and its title. Mirrors the SDK's own
#: `emit.scaffold._PACKABLE`, which is private. Duplicated deliberately rather than reached
#: into: if the SDK moves the engine, the build must FAIL here (`missing`) rather than
#: quietly pack nothing, and a private import would move with it and hide that.
ENGINE_TOOL = os.path.join(
    "tools", "slot_filling_engine", "python_function", "python_code.py")
ENGINE_ENTRY = "slot_filling_engine"
ENGINE_TITLE = "Slot-filling DAG engine."

#: What CES runs, per `flows.engine.packing.TARGET_PY` (probe 163 measured `py=3.12.5`).
#: The real compatibility gate is the bytecode magic number the packed module checks at
#: load time; this is which interpreter to go looking for.
TARGET_PY = (3, 12)


# --- the child: pack one file, under an interpreter of the right version -----

def _load_packing(path: str):
  """`flows.engine.packing`, loaded from a path rather than imported.

  The child interpreter has no project venv -- that is the point of it -- so the module is
  loaded as a file. It is stdlib-only, which is what makes that legitimate.
  """
  import importlib.util  # noqa: PLC0415

  spec = importlib.util.spec_from_file_location("_flows_packing", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def pack_file(packing_path: str, target: str, entry: str, title: str) -> None:
  """Rewrite `target` as its packed form, or raise.

  The round-trip assertion is not ceremony: the packed module carries the original source
  compressed, and that copy is what `blessed_source._canonical` hashes for the framework
  drift gate and what the deployed module falls back to if the interpreter ever stops
  matching. A blob whose embedded source does not recover the bytes that were packed would
  pass every shape check and quietly break both.
  """
  packing = _load_packing(packing_path)
  with open(target) as fh:
    source = fh.read()
  if packing.is_packed(source):
    raise RuntimeError(f"{target} is already packed; the build emits source and packs "
                       "once, so this means the step ran twice")
  packed = packing.pack(source, entry, title)
  if packing.unpack(packed) != source:
    raise RuntimeError(f"{target}: the packed module does not carry back the source it "
                       "was built from")

  # LOADED, HERE, ON THE RIGHT INTERPRETER. The packed module degrades to its embedded
  # source on any failure, by design -- which means a blob that does not load costs the
  # whole saving and looks identical from the outside. Nothing else in the build can catch
  # that: this repo's oracles run on the venv's python, where the magic number deliberately
  # does NOT match and the fallback is the correct path. So the one interpreter that can
  # tell the difference is this child, and it asks while it is here.
  namespace: dict = {}
  exec(compile(packed, "python_code.py", "exec", 0, True), namespace)  # noqa: S102
  if namespace.get("_FLOWS_LOADED_VIA") != "bytecode":
    raise RuntimeError(
        f"{target}: the packed module took its {namespace.get('_FLOWS_LOADED_VIA')!r} "
        "path on the interpreter that built it, so a deploy would pay the parse it was "
        "meant to remove")
  if not callable(namespace.get(entry)):
    raise RuntimeError(f"{target}: the packed module exposes no callable {entry!r}")

  with open(target, "w") as fh:
    fh.write(packed)
  # The parent cannot introspect the interpreter it delegated to, and which one built the
  # bytecode is the fact worth having in a build log.
  print(f"python {sys.version.split()[0]}")


# --- the parent: choose an interpreter, then hold the result to the claim ----

def _packing_module():
  """The SDK's packing module. Its `__file__` is what the child is handed."""
  import labs_paths  # noqa: PLC0415

  labs_paths.add_sdk_paths()
  from flows.engine import packing  # noqa: PLC0415
  if tuple(packing.TARGET_PY) != TARGET_PY:
    raise SystemExit(
        f"build: the SDK packs for python {packing.TARGET_PY} and this build goes looking "
        f"for {TARGET_PY}. CES has changed interpreter; update "
        "`engine_packing.TARGET_PY`, because the interpreter that gets found would be "
        "refused by `pack` anyway.")
  return packing


def interpreter() -> list:
  """How to reach a python `TARGET_PY`, as an argv prefix, or [] if there is none.

  In preference order, and the order matters:

    * THIS interpreter, when it already matches. No process, no `uv`, no network.
    * `uv run --no-project --python 3.12`. `--no-project` is load-bearing: without it `uv`
      tries to resolve THIS project against 3.12, which means an install, a lockfile that
      does not want to move, and a minute of waiting for a step that takes a tenth of a
      second. `uv` fetches a managed 3.12 if the machine has none.
    * a `python3.12` on PATH, for a host with no `uv`.

  Not cached. A build runs this once, and a cache would make the config gate's
  availability probe disagree with the build it is grading.
  """
  if sys.version_info[:2] == TARGET_PY:
    return [sys.executable]
  version = ".".join(str(v) for v in TARGET_PY)
  if shutil.which("uv") and _runs(["uv", "run", "--no-project", "--python", version,
                                   "python"]):
    return ["uv", "run", "--no-project", "--python", version, "python"]
  found = shutil.which(f"python{version}")
  if found and _runs([found]):
    return [found]
  return []


def _runs(argv: list) -> bool:
  """Whether `argv` really is a working interpreter of the right version.

  Executed rather than trusted. A downloaded interpreter that endpoint security kills on
  first exec looks exactly like a present one to `which`, and the failure would otherwise
  surface as a packing step that died with no explanation.
  """
  probe = "import sys;print(sys.version_info[0], sys.version_info[1])"
  try:
    done = subprocess.run(argv + ["-c", probe], capture_output=True, text=True,
                          timeout=180, check=False)
  except (OSError, subprocess.SubprocessError):
    return False
  return (done.returncode == 0
          and done.stdout.split() == [str(v) for v in TARGET_PY])


def pack_app_dir(out_dir: str, config) -> str:
  """Pack the emitted engine tool if asked to. Returns one of `build_config.PACKING_STATES`.

  Runs LAST in the build, after every step that rewrites a tool body: packing replaces the
  engine module with a blob, and a later text patch against it would either miss or corrupt
  it.
  """
  import build_config  # noqa: PLC0415

  target = os.path.join(out_dir, ENGINE_TOOL)
  if not os.path.exists(target):
    raise SystemExit(
        f"build: no engine tool at {ENGINE_TOOL} to pack. The SDK has moved or renamed it, "
        "so `engine_packing.ENGINE_TOOL` needs updating -- refusing to emit rather than "
        "report a packing outcome for a file that is not there.")
  if not config.pack_engine:
    print(f"  engine packing   : off, source ({os.path.getsize(target):,} B)")
    return build_config.PACK_OFF

  packing = _packing_module()
  argv = interpreter()
  if not argv:
    version = ".".join(str(v) for v in TARGET_PY)
    print("\n" + "!" * 79)
    print(f"  ENGINE PACKING SKIPPED. --pack-engine is on and this build emitted the "
          f"engine as\n  SOURCE, because no python {version} interpreter could be found "
          f"(this one is\n  {sys.version.split()[0]}, and CES runs {version}). The app "
          "works and is slower: about\n  95ms of re-parsing on every engine call, roughly "
          "eleven times a session.\n\n  build_manifest.json records "
          f"{build_config.PACKING_KEY}=\"{build_config.PACK_SKIPPED}\". Install uv, or "
          f"build on python {version}.")
    print("!" * 79 + "\n")
    print(f"  engine packing   : SKIPPED -- asked for, emitted SOURCE "
          f"({os.path.getsize(target):,} B)")
    return build_config.PACK_SKIPPED

  with open(target) as fh:
    before = fh.read()
  before_size = os.path.getsize(target)
  done = subprocess.run(
      argv + [os.path.abspath(__file__), "--packing", os.path.abspath(packing.__file__),
              "--target", os.path.abspath(target), "--entry", ENGINE_ENTRY,
              "--title", ENGINE_TITLE],
      capture_output=True, text=True, check=False)
  if done.returncode != 0:
    raise SystemExit("build: packing the engine failed under "
                     f"{' '.join(argv)}:\n{done.stdout[-2000:]}{done.stderr[-2000:]}")

  # VERIFIED FROM THE FILE, in the parent, against the bytes that went in. The child could
  # have reported success and written nothing; this is what makes `packed` in the manifest
  # a measurement rather than a claim. Cheap enough to be unconditional -- `unpack` is a
  # decompress and one `ast.parse`.
  with open(target) as fh:
    after = fh.read()
  if not packing.is_packed(after) or packing.unpack(after) != before:
    raise SystemExit(
        f"build: {ENGINE_TOOL} was packed and does not carry back the source it was built "
        "from. Refusing to emit: a manifest that says `packed` has to mean it.")
  built_by = (done.stdout.strip().splitlines() or ["python ?"])[-1]
  print(f"  engine packing   : bytecode, {os.path.getsize(target):,} B from "
        f"{before_size:,} B ({built_by} via {argv[0]})")
  return build_config.PACKED


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  ap.add_argument("--packing", required=True, help="path to flows/engine/packing.py")
  ap.add_argument("--target", required=True, help="the tool body to rewrite in place")
  ap.add_argument("--entry", default=ENGINE_ENTRY)
  ap.add_argument("--title", default=ENGINE_TITLE)
  args = ap.parse_args()
  pack_file(args.packing, args.target, args.entry, args.title)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
