"""Blessed framework source + version stamp (WP-F1).

The single pinned source of framework code that scaffolding copies and the hosted
container can use. Everything is read from the vendored ``blessed/`` data
directory (byte-copied from the canonical tree), so both local and hosted modes
scaffold identical, version-stamped framework code.

Public API (other WPs import this):
    version() -> str
    callbacks() -> dict[str, bytes]
    framework_tool_files() -> list[dict]   # ScaffoldFile shape: {"path", "content"}
    docstring_args(src: str) -> set[str]
    sig_args_from_source(src: str, func_name: str | None = None) -> set[str]

Sourcing (recorded for the "blessed" guarantee):
  * callbacks   <- bella_notte/framework_callbacks/{before_agent,before_model,
                   after_model,after_tool}.py
  * framework   <- bella_notte/cxas_app/tools/<tool>/{<tool>.json,
    tools          python_function/python_code.py}
  Both were byte-copied verbatim into ``blessed/`` (see the WP-F1 vendoring step).
"""

import ast
import hashlib
import importlib.util
import json
import logging
import os
import re
from dataclasses import dataclass, field

from . import packing as _packing
from .framework import _docstrings

logger = logging.getLogger(__name__)

# Stable version stamp for the pinned blessed framework bundle. Bump when the
# vendored callbacks/tools are re-synced from the canonical tree.
#
# This is NOT cosmetic: `framework_staleness` compares an app's pinned
# `framework.lock` version against this STRING, not against the file hashes. Leave it
# alone after changing the bundle and every already-deployed app reports "up to date"
# while running different engine behaviour, and nobody is ever told to run
# `framework upgrade`.
_VERSION = "2026.08.18.4+manufactured-turn"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BLESSED_DIR = os.path.join(_HERE, "framework")
_CALLBACKS_DIR = os.path.join(_BLESSED_DIR, "callbacks")
_HOST_CALLBACKS_DIR = os.path.join(_BLESSED_DIR, "host_callbacks")
_TOOLS_DIR = os.path.join(_BLESSED_DIR, "tools")
_MANIFEST_PATH = os.path.join(_BLESSED_DIR, "manifest.json")

# Provenance recorded in the manifest (the framework subset is byte-copied here).
_GENERATED_FROM = "bella_notte/cxas_app (framework subset)"

# The 4 framework callbacks, canonical name -> vendored filename.
_CALLBACK_NAMES = ("before_agent", "before_model", "after_model", "after_tool")

# A host/steering router runs NO slot-filling engine — it only greets, classifies
# intent (set_active_flow), and transfers. It carries just these two custom
# callbacks (dispatch a pending transfer + capture set_active_flow -> target).
# They deliberately differ from the canonical engine callbacks and are NOT tracked
# by the manifest: they only ever land on the router, which the drift checks skip
# (a non-slot-filling agent). See `host_router_files`.
_HOST_CALLBACK_NAMES = ("before_model", "after_tool")

# Canonical per-agent subdirectory for each callback's python_code.py. The single
# source for where the 4 callbacks land under an agent dir (scaffold + the CLI
# sync + drift verification all resolve callback paths through this).
_CALLBACK_SUBDIRS = {
    "before_agent": "before_agent_callbacks/before_agent_callbacks_01",
    "before_model": "before_model_callbacks/before_model_callbacks_01",
    "after_model": "after_model_callbacks/after_model_callbacks_01",
    "after_tool": "after_tool_callbacks/after_tool_callbacks_01",
}

# Runtime framework tools the blessed bundle ships into every agent. NOT here:
#  * set_active_flow — agent-specific flows/routing; scaffold generates per-agent.
#  * validate_dag_config — a DESIGN-TIME linter, not a runtime tool; it lives in
#    blessed/tools/ for the linter to load but is never deployed into an agent.
_FRAMEWORK_TOOLS = (
    "slot_filling_engine",
    "slot_intake",
    "cancel_flow",
    "transfer_to_human",
    "confirm_pending",
    "reject_pending",
    "set_slot_change",
    "classify_turn_intent",
    "try_again",
    "new_flow_instance",
    "resume_flow",
    "settle_guard",
)


def _current_version() -> str:
  """The live framework version: the committed manifest's, else the bootstrap."""
  try:
    return manifest()["version"]
  except (OSError, ValueError, KeyError):
    return _VERSION


def version() -> str:
  """Return the stable version stamp of the blessed framework bundle."""
  return _current_version()


def callbacks() -> dict[str, bytes]:
  """Return the 4 framework callbacks as ``name -> bytes`` (byte-identical copies).

  Keys are ``before_agent``, ``before_model``, ``after_model``, ``after_tool``.
  Bytes are read verbatim from the vendored blessed callback files.
  """
  out: dict[str, bytes] = {}
  for name in _CALLBACK_NAMES:
    path = os.path.join(_CALLBACKS_DIR, f"{name}.py")
    with open(path, "rb") as f:
      out[name] = f.read()
  return out


def framework_tool_files() -> list[dict]:
  """Return the framework tool files as ScaffoldFile dicts.

  Each entry is ``{"path": <app-root-relative>, "content": <str>}`` for both the
  ``<tool>.json`` and the ``python_function/python_code.py`` of every framework
  tool. Plain dicts (not a pydantic model) so this module has no cross-WP
  dependency; the shape matches ARCHITECTURE §4 ScaffoldFile.
  """
  files: list[dict] = []
  for tool in _FRAMEWORK_TOOLS:
    json_rel = f"tools/{tool}/{tool}.json"
    code_rel = f"tools/{tool}/python_function/python_code.py"
    json_path = os.path.join(_TOOLS_DIR, tool, f"{tool}.json")
    code_path = os.path.join(_TOOLS_DIR, tool, "python_function", "python_code.py")
    with open(json_path, "r", encoding="utf-8") as f:
      files.append({"path": json_rel, "content": f.read()})
    with open(code_path, "r", encoding="utf-8") as f:
      files.append({"path": code_rel, "content": f.read()})
  return files


# --- Manifest: the machine-readable framework contract ----------------------
#
# manifest.json pins exactly which files are "framework" (owned by the canonical
# bundle, emitted verbatim) and the sha256 of each, so any consumer can detect
# drift with a hash compare. The framework tool .json files already carry stable
# hand-assigned UUIDs, so every emitted framework file is byte-identical across
# agents — the manifest just records those bytes.


def _sha256_text(text: str) -> str:
  """Hex sha256 of UTF-8 text (the unit the framework files are read/written as)."""
  return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _framework_file_paths() -> list[str]:
  """App-root-relative paths of every byte-stable framework file, sorted.

  Two files per framework tool (the ``<tool>.json`` + its ``python_code.py``).
  Callbacks are tracked separately (they land at per-agent paths).
  """
  paths: list[str] = []
  for tool in _FRAMEWORK_TOOLS:
    paths.append(f"tools/{tool}/{tool}.json")
    paths.append(f"tools/{tool}/python_function/python_code.py")
  return sorted(paths)


def _read_blessed(rel_path: str) -> str:
  """Read a blessed file by its app-root-relative path."""
  with open(os.path.join(_BLESSED_DIR, rel_path), "r", encoding="utf-8") as f:
    return f.read()


def _read_callback(name: str) -> str:
  """Read a blessed callback's source by canonical name."""
  with open(os.path.join(_CALLBACKS_DIR, f"{name}.py"), "r", encoding="utf-8") as f:
    return f.read()


def compute_manifest(version: str | None = None) -> dict:
  """Deterministically compute the framework contract from the blessed bytes.

  Pure function of the on-disk blessed bundle + the version — no timestamps or
  other nondeterminism, so the output is byte-stable and ``promote`` can
  regenerate + diff it. ``version`` defaults to the live version (the committed
  manifest's), so regenerating without an explicit bump preserves integrity;
  ``promote`` passes a new version to bump. The committed ``manifest.json`` must
  equal ``compute_manifest()`` (a test asserts manifest integrity).
  """
  version = version or _current_version()
  files = [
      {"path": p, "sha256": _sha256_text(_read_blessed(p))}
      for p in _framework_file_paths()
  ]
  callbacks_h = {
      name: _sha256_text(_read_callback(name)) for name in _CALLBACK_NAMES
  }
  return {
      "version": version,
      "generated_from": _GENERATED_FROM,
      "framework_tools": list(_FRAMEWORK_TOOLS),
      "callbacks": dict(sorted(callbacks_h.items())),
      "files": files,
  }


def manifest() -> dict:
  """Load the committed framework contract (``blessed/manifest.json``)."""
  with open(_MANIFEST_PATH, "r", encoding="utf-8") as f:
    return json.load(f)


def manifest_json(version: str | None = None) -> str:
  """Serialize the computed manifest deterministically (for writing the file)."""
  return json.dumps(compute_manifest(version), indent=2, ensure_ascii=False) + "\n"


def write_manifest(version: str | None = None) -> str:
  """(Re)generate and write ``blessed/manifest.json`` from the current bundle.

  Used after the blessed bytes change (``promote``). Returns the new version.
  """
  m = compute_manifest(version)
  with open(_MANIFEST_PATH, "w", encoding="utf-8") as f:
    f.write(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
  return m["version"]


# --- Composer: the one place that emits framework files for an agent ---------


def callback_rel_path(root_agent: str, name: str) -> str:
  """App-root-relative path of a callback's ``python_code.py`` under an agent."""
  return f"agents/{root_agent}/{_CALLBACK_SUBDIRS[name]}/python_code.py"


def app_framework_files(agent_names) -> list[dict]:
  """Every framework file for an app as ScaffoldFile dicts (verbatim).

  The 12 framework tools (``tools/<tool>/*`` — byte-identical across agents, one
  copy per app) plus the 4 callbacks placed under EACH agent dir (a multi-agent
  app carries the callbacks once per agent). This is the single composer used by
  both the scaffold/wizard and the ``framework`` CLI, so every consumer emits
  identical bytes. Content is always ``str`` (callbacks decoded from utf-8).
  """
  if isinstance(agent_names, str):
    agent_names = [agent_names]
  files: list[dict] = list(framework_tool_files())
  cbs = callbacks()
  for agent in agent_names:
    for name in _CALLBACK_NAMES:
      files.append(
          {"path": callback_rel_path(agent, name), "content": cbs[name].decode("utf-8")}
      )
  return files


def framework_files(root_agent: str) -> list[dict]:
  """Framework files for a single-agent app (convenience over app_framework_files)."""
  return app_framework_files([root_agent])


def host_router_callbacks() -> dict[str, bytes]:
  """Return the host-router callbacks as ``name -> bytes`` (before_model, after_tool).

  These are the custom callbacks a non-slot-filling steering agent carries (dispatch
  a pending transfer; capture set_active_flow -> target_agent). Byte-read verbatim
  from the vendored ``host_callbacks/`` source.
  """
  out: dict[str, bytes] = {}
  for name in _HOST_CALLBACK_NAMES:
    path = os.path.join(_HOST_CALLBACKS_DIR, f"{name}.py")
    with open(path, "rb") as f:
      out[name] = f.read()
  return out


def host_router_files(host_agent: str) -> list[dict]:
  """The host-router's callback files as ScaffoldFile dicts, under its agent dir.

  Only the two custom router callbacks (``before_model`` + ``after_tool``) placed at
  the host agent's canonical per-agent callback paths. Content is ``str`` (decoded
  from utf-8). The 12 shared framework tools come from ``framework_tool_files`` once
  per app — a host router references only ``set_active_flow`` + ``end_session`` of
  them, but the files themselves are app-level.
  """
  cbs = host_router_callbacks()
  return [
      {"path": callback_rel_path(host_agent, name), "content": cbs[name].decode("utf-8")}
      for name in _HOST_CALLBACK_NAMES
  ]


# --- Drift verification: does a target match the framework contract? ---------


@dataclass
class DriftReport:
  """Result of comparing a target's framework files against the manifest."""

  ok: bool
  missing: list[str] = field(default_factory=list)
  mismatched: list[str] = field(default_factory=list)

  def summary(self) -> str:
    if self.ok:
      return "framework in sync"
    parts = []
    if self.missing:
      parts.append(f"missing: {', '.join(self.missing)}")
    if self.mismatched:
      parts.append(f"out of date: {', '.join(self.mismatched)}")
    return "; ".join(parts)


def _canonical(content: str) -> str:
  """The SOURCE a provided file stands for, unpacking a bytecode-packed module first.

  A deployed framework tool may ship as precompiled bytecode (``engine/packing.py``),
  because CES re-parses a tool's source on every invocation and the engine costs 98-153ms
  a call to parse. The manifest deliberately keeps describing SOURCE: packing is a
  deployment encoding, not a change to the framework contract, so the checked-in bundle
  and the studio mirror stay readable.

  That split only holds because a packed module embeds its own original source, letting the
  exact blessed bytes be recovered and hashed here. Without it the drift check would go
  blind on the one file most worth protecting -- and it would go blind by PASSING, which is
  the failure mode that gets noticed late.
  """
  if _packing.is_packed(content):
    recovered = _packing.unpack(content)
    if recovered is not None:
      return recovered
  return content


def verify_files(provided: dict) -> DriftReport:
  """Check a ``path -> content`` map against the framework contract (manifest).

  Reports framework files that are missing or whose content hash differs from
  the canonical bundle. EVERY callback copy (one set per agent in a multi-agent
  app) is checked against its name's canonical hash. Files outside the framework
  set are ignored (the agent layer is not the framework's concern).

  A packed module is compared by its embedded source (see ``_canonical``), so an app
  deployed as bytecode is still drift-checked byte-exactly against the manifest.
  """
  m = manifest()
  missing: list[str] = []
  mismatched: list[str] = []
  for entry in m["files"]:
    path, want = entry["path"], entry["sha256"]
    if path not in provided:
      missing.append(path)
    elif _sha256_text(_canonical(provided[path])) != want:
      mismatched.append(path)
  for name, want in m["callbacks"].items():
    suffix = f"{_CALLBACK_SUBDIRS[name]}/python_code.py"
    matches = [p for p in provided if p.endswith(suffix)]
    if not matches:
      missing.append(f"<callback:{name}>")
    for path in matches:
      if _sha256_text(_canonical(provided[path])) != want:
        mismatched.append(path)
  return DriftReport(
      ok=not missing and not mismatched,
      missing=missing,
      mismatched=sorted(mismatched),
  )


# --- Lenient deployed-app staleness (for the design-time warning) -----------
#
# ``verify_files`` above is byte-exact and used by CI/promote + scaffold self-
# checks (where the bytes MUST match). The Problems-tab staleness warning, by
# contrast, runs against a real deployed/migrated app's working copy, where two
# kinds of drift are NOT staleness and were causing permanent false positives:
#
#   1. Tool ``.json`` files whose only difference is the editable, model-facing
#      ``pythonFunction.description`` text (blessed reworded it; the executable
#      ``python_code.py`` is byte-identical). Not stale — the framework code is
#      current.
#   2. Lifecycle callbacks on NON slot-filling agents (a router/host/leaf agent
#      legitimately carries its own custom before_model/after_model/etc. logic).
#      Only agents that actually run the slot-filling engine use the framework's
#      canonical callbacks, so only those should be checked.
#
# ``check_deployed_framework`` keeps the real signals (framework tool CODE drift,
# missing framework files, and a slot-filling agent's engine callback drifting
# from canonical) while ignoring the two false-positive sources above.

_AGENT_JSON_RE = re.compile(r"^agents/([^/]+)/[^/]+\.json$")
_AGENT_DIR_RE = re.compile(r"^agents/([^/]+)/")
# An agent runs the slot-filling engine (and thus uses the canonical framework
# callbacks) iff it declares the engine tool.
_ENGINE_TOOL = "slot_filling_engine"


def _agent_dir_of(path: str) -> str | None:
  m = _AGENT_DIR_RE.match(path)
  return m.group(1) if m else None


def _slot_filling_agent_dirs(provided: dict) -> set:
  """Agent directories whose agent.json declares the slot-filling engine tool.

  These are the only agents whose lifecycle callbacks are the framework's
  canonical ones; router/host/leaf agents carry custom callbacks and are skipped.
  """
  dirs: set = set()
  for path, content in provided.items():
    m = _AGENT_JSON_RE.match(path)
    if not m:
      continue
    try:
      j = json.loads(content)
    except (ValueError, TypeError):
      continue
    tools = j.get("tools")
    if isinstance(tools, list) and _ENGINE_TOOL in tools:
      dirs.add(m.group(1))
  return dirs


def _norm_tool_json(text: str) -> dict | None:
  """Parse a tool ``.json`` and drop cosmetic fields (the model-facing
  ``description``), leaving only the load-bearing identity/wiring keys. Returns
  ``None`` if it can't be parsed."""
  try:
    d = json.loads(text)
  except (ValueError, TypeError):
    return None
  if not isinstance(d, dict):
    return None
  d = dict(d)
  d.pop("description", None)
  pf = d.get("pythonFunction")
  if isinstance(pf, dict):
    d["pythonFunction"] = {k: v for k, v in pf.items() if k != "description"}
  return d


def _tool_json_is_stale(provided_text: str, blessed_text: str) -> bool:
  """True when a framework tool ``.json`` differs in a load-bearing way (not just
  the editable ``description`` text). Unparseable content is treated as stale."""
  a, b = _norm_tool_json(provided_text), _norm_tool_json(blessed_text)
  if a is None or b is None:
    return True
  return a != b


def check_deployed_framework(provided: dict) -> DriftReport:
  """Lenient framework-staleness check for a deployed/edited app's working copy.

  Like ``verify_files`` but ignores the two non-staleness drift sources described
  above: cosmetic tool-``.json`` ``description`` rewording, and custom lifecycle
  callbacks on non-slot-filling agents. Framework tool CODE, missing framework
  files, and slot-filling agents' engine-callback drift are still reported.
  """
  m = manifest()
  missing: list = []
  mismatched: list = []
  for entry in m["files"]:
    path, want = entry["path"], entry["sha256"]
    if path not in provided:
      missing.append(path)
    elif _sha256_text(provided[path]) != want:
      if path.endswith(".json"):
        # Only flag if the load-bearing structure drifted (ignore description).
        if _tool_json_is_stale(provided[path], _read_blessed(path)):
          mismatched.append(path)
      else:
        mismatched.append(path)
  engine_dirs = _slot_filling_agent_dirs(provided)
  for name, want in m["callbacks"].items():
    suffix = f"{_CALLBACK_SUBDIRS[name]}/python_code.py"
    for path in provided:
      if not path.endswith(suffix):
        continue
      if _agent_dir_of(path) not in engine_dirs:
        continue  # custom callback on a non-engine agent — not the framework's
      if _sha256_text(provided[path]) != want:
        mismatched.append(path)
  return DriftReport(
      ok=not missing and not mismatched,
      missing=missing,
      mismatched=sorted(mismatched),
  )


def verify_app_dir(app_dir: str) -> DriftReport:
  """Drift-check an app on disk: read its framework files, compare to manifest.

  Callbacks under a NON slot-filling agent dir are excluded: a host/steering router
  carries its OWN custom before_model/after_tool (they legitimately differ from the
  canonical engine callbacks), and only engine agents use the framework's. Framework
  TOOLS + engine-agent callbacks are still compared byte-exactly.
  """
  provided: dict = {}
  agent_jsons: dict = {}
  for root, _dirs, names in os.walk(app_dir):
    for fn in names:
      abs_path = os.path.join(root, fn)
      rel = os.path.relpath(abs_path, app_dir).replace(os.sep, "/")
      # Only the framework surface: tool files + callback python_code.py.
      if rel.startswith("tools/") or rel.endswith("/python_code.py"):
        try:
          with open(abs_path, "r", encoding="utf-8") as f:
            provided[rel] = f.read()
        except (OSError, UnicodeDecodeError):
          continue
      elif _AGENT_JSON_RE.match(rel):
        try:
          with open(abs_path, "r", encoding="utf-8") as f:
            agent_jsons[rel] = f.read()
        except (OSError, UnicodeDecodeError):
          continue
  engine_dirs = _slot_filling_agent_dirs(agent_jsons)
  filtered = {}
  for rel, content in provided.items():
    if rel.endswith("/python_code.py"):
      d = _agent_dir_of(rel)
      if d is not None and d not in engine_dirs:
        continue  # router/host callback — not a framework-canonical file
    filtered[rel] = content
  return verify_files(filtered)


# --- framework.lock: the version an agent is pinned to -----------------------


def lock_contents(agent_names) -> str:
  """The ``framework.lock`` body for an app: pinned version + per-file hashes.

  Records the canonical version and the sha256 of every framework file at the
  app's paths (callbacks under each agent dir), so a later ``check`` knows exactly
  what was synced. Accepts a single agent name or a list.
  """
  if isinstance(agent_names, str):
    agent_names = [agent_names]
  m = manifest()
  files = {entry["path"]: entry["sha256"] for entry in m["files"]}
  for agent in agent_names:
    for name, h in m["callbacks"].items():
      files[callback_rel_path(agent, name)] = h
  lock = {"framework_version": m["version"], "files": dict(sorted(files.items()))}
  return json.dumps(lock, indent=2, ensure_ascii=False) + "\n"


# --- Design-time linter (validate_dag_config) -------------------------------
#
# The DAG validator is a DESIGN-TIME linter, not a runtime framework tool — the
# engine never calls it during a conversation. It lives in blessed/tools/ only so
# the linter (scaffold gate, CLI, conformance) can load it from the canonical
# bundle. It is deliberately absent from _FRAMEWORK_TOOLS, so it's never deployed
# into an agent.

_LINTER_TOOL = "validate_dag_config"


def load_linter():
  """Load the canonical design-time DAG linter module from the bundle."""
  path = os.path.join(_TOOLS_DIR, _LINTER_TOOL, "python_function", "python_code.py")
  spec = importlib.util.spec_from_file_location("blessed_validate_dag_config", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def lint_config(config: dict, available_tools) -> dict:
  """Run the design-time linter on a DAG config.

  Returns ``{valid, errors, warnings}``. Use this everywhere a config is
  validated at design time (scaffold gate, CLI report, conformance) so there is
  ONE linter — the canonical one — regardless of what tools an agent ships.
  """
  return load_linter().validate_dag_config(
      {"raw_config": config, "available_tools": sorted(available_tools)}
  )


def framework_staleness(app_dir: str) -> str | None:
  """Design-time staleness check: warn if an app's framework is behind canonical.

  Reads ``<app_dir>/framework.lock`` and compares its pinned version to the
  canonical version. Returns a human-readable warning string when the app has no
  lock or is out of date, else None. This is a LINT (design-time), never a
  runtime/framework feature.
  """
  canonical = version()
  lock_path = os.path.join(app_dir, "framework.lock")
  if not os.path.isfile(lock_path):
    return (
        f"no framework.lock — framework version unknown; run "
        f"`framework sync`/`upgrade` to pin v{canonical}"
    )
  try:
    with open(lock_path, "r", encoding="utf-8") as f:
      pinned = json.load(f).get("framework_version")
  except (OSError, ValueError):
    return "framework.lock is unreadable — can't determine framework version"
  if pinned != canonical:
    return (
        f"framework v{pinned} is out of date (canonical v{canonical}) — "
        f"run `framework upgrade`"
    )
  return None


def docstring_args(src: str) -> set[str]:
  """Names documented in the Args: section of the first function in ``src``.

  Parses ``src`` and reads the first function's docstring Args: block via the
  vendored ``check_docstrings`` logic. Returns an empty set when there is no
  function, no docstring, or no Args: section.
  """
  try:
    tree = ast.parse(src)
  except SyntaxError:
    logger.warning("docstring_args: could not parse source")
    return set()
  node = _docstrings._first_funcdef(tree)
  if node is None:
    return set()
  documented = _docstrings.documented_args(ast.get_docstring(node))
  return set(documented or [])


def sig_args_from_source(src: str, func_name: str | None = None) -> set[str]:
  """Signature parameter names (excluding self/cls) of a function in ``src``.

  Parses ``src`` and returns the parameter names of the function named
  ``func_name`` (or the first function if ``func_name`` is None). Returns an
  empty set when the source can't be parsed or the function isn't found.
  """
  try:
    tree = ast.parse(src)
  except SyntaxError:
    logger.warning("sig_args_from_source: could not parse source")
    return set()
  node = _docstrings._first_funcdef(tree, func_name)
  if node is None:
    return set()
  return set(_docstrings.sig_args(node))
