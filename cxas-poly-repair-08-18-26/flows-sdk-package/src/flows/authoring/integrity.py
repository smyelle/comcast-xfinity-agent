"""Post-emit integrity: did what the author ASKED for actually LAND on disk?

Emission is two phases — `scaffold.build` writes the file SET, then `emit`'s
post-emit steps scope the agent's tools, inject the business
`variableDeclarations`, write instructions and language settings. Nothing used to
compare the two ends. A first phase that half-succeeded, or a second phase that
was skipped, produced a tree that LOOKS like an app (app.json, agents/, tools/,
the framework files) and deploys cleanly, because the only thing missing is
content nobody re-reads.

That is not hypothetical: a mid-edit framework bundle failed the scaffold's
byte-sync gate, the post-emit steps never ran, and the tree that stayed on disk
carried the framework's own 7 `variableDeclarations` instead of the app's 29 —
no `mock_json`, no `ani`. It pushed to CES and ran evals at half score before
anyone looked at the emit log.

So the emit path now ends with a cheap asked-vs-landed diff (`verify_emitted`):
every declared variable, every agent, every tool a task or a slot names, every
app-LEVEL setting the author declared, and the framework files. `verify_dir` is
the same check for a dir alone — no `App` in hand — which is what `flows deploy`
can do for a tree emitted by someone else. Both are stat + one read of
app.json/the agent jsons + the framework hashes; they run on every emit, so they
stay in that budget.

This module also owns the `declared-settings.json` sidecar: the list of app.json
TOP-LEVEL keys (`timeZoneSettings`, `guardrails`, `loggingSettings`, ...) the
author declared on their `App`. It exists because the two ends of that promise are
in different processes — `flows emit` knows what was declared, `flows deploy` is
handed only a PATH — and the deploy's merge has to tell an author's timezone from
a timezone it back-filled off the live target (see `flows/deploy/prep.py`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..engine import blessed_source as _bs

# CES provides these to every agent; they are legitimately named in an agent's
# `tools[]` with no `tools/<name>/` resource in the app dir, so an unresolved-tool
# check must not flag them.
CES_BUILTIN_TOOLS = frozenset({"end_session"})

_TOOL_JSON_RE = re.compile(r"^tools/([^/]+)/\1\.json$")

# The app-dir sidecar naming which app.json TOP-LEVEL settings the author declared.
# Written only when the App declares at least one, so an app that declares none emits
# exactly the tree it always did.
DECLARED_SETTINGS_FILE = "declared-settings.json"

_DECLARED_SETTINGS_NOTE = (
    "flows: the app.json TOP-LEVEL settings this app's source DECLARES (flows.App "
    "time_zone= / guardrails= / app_settings= / languages=). `flows deploy` keeps "
    "these and does NOT overwrite them with the live target's values; anything not "
    "listed here is still preserved from the target. Generated — do not hand-edit."
)


class EmitIntegrityError(ValueError):
  """The emitted app dir does not match what the emit asked for.

  A `ValueError` so the CLI's existing `except ValueError` on the emit path keeps
  catching it, and so an author's `try: build_app(...) except ValueError` keeps
  working; the distinct type is for callers that want to tell "your app is wrong"
  (validation) from "the emitter did not land what it promised".
  """


@dataclass
class IntegrityReport:
  """What was asked for and did not land. Empty everywhere == ok."""

  missing_variables: list[str] = field(default_factory=list)
  missing_agents: list[str] = field(default_factory=list)
  missing_tools: list[str] = field(default_factory=list)
  # "<agent> -> <tool>": the agent json lists a tool the app dir does not contain.
  unresolved_agent_tools: list[str] = field(default_factory=list)
  # App-LEVEL settings the author declared that app.json does not carry (or carries
  # with a different value) — a declared timezone that never reached the deploy.
  unlanded_settings: list[str] = field(default_factory=list)
  framework_missing: list[str] = field(default_factory=list)
  framework_stale: list[str] = field(default_factory=list)
  # Structural breakage: a file that must exist/parse and does not.
  broken: list[str] = field(default_factory=list)
  # Tools that landed but that CES will silently refuse to run — see
  # `undispatchable_tools`. Present and correct-looking is not the same as callable.
  undispatchable: list[str] = field(default_factory=list)

  @property
  def ok(self) -> bool:
    return not any((
        self.missing_variables, self.missing_agents, self.missing_tools,
        self.unresolved_agent_tools, self.unlanded_settings,
        self.framework_missing, self.framework_stale, self.broken,
        self.undispatchable,
    ))

  def summary(self) -> str:
    if self.ok:
      return "framework in sync; every declared variable, agent and tool present"
    parts: list[str] = []
    if self.broken:
      parts.append(f"unusable: {', '.join(self.broken)}")
    if self.missing_variables:
      parts.append(
          f"{len(self.missing_variables)} declared variable(s) never landed in "
          f"app.json: {', '.join(self.missing_variables)}")
    if self.missing_agents:
      parts.append(f"missing agent(s): {', '.join(self.missing_agents)}")
    if self.missing_tools:
      parts.append(f"missing tool resource(s): {', '.join(self.missing_tools)}")
    if self.unresolved_agent_tools:
      parts.append(
          "agent lists a tool the app does not contain: "
          + ", ".join(self.unresolved_agent_tools))
    if self.unlanded_settings:
      parts.append(
          "declared app-level setting(s) never landed in app.json: "
          + "; ".join(self.unlanded_settings))
    if self.undispatchable:
      parts.append(
          "tool(s) CES will never dispatch: " + "; ".join(self.undispatchable))
    if self.framework_missing:
      parts.append(f"missing framework file(s): {', '.join(self.framework_missing)}")
    if self.framework_stale:
      parts.append(
          f"framework file(s) off the blessed manifest v{_bs.version()}: "
          + ", ".join(self.framework_stale))
    return "; ".join(parts)


def undispatchable_tools(app_dir: str) -> list[str]:
  """Tools that landed intact and that CES will never run.

  Two ways a tool resource can be well-formed, pass every schema check, deploy without
  complaint, and then be dropped in silence when it is fired. Both are measured
  behaviors, both are visible right here in the emitted directory, and both present
  identically at runtime: no error, no `function_response`, no tool result. The
  engine re-fires until the turn dies at the reasoning cap, which reads as a sequencing or
  prompting problem and is neither.

  * **The entry function is not the one named.** `pythonFunction.name` is what CES calls,
    so the body has to define it. A `@tool(name=…)` override renames the resource and
    leaves the `def` alone, so the two part company (ces-probes `51`). Symptom: a slot
    that never fills while the model politely re-asks forever.
  * **No return annotation.** An unannotated entry function is not registered at all
    (ces-probes `23`). This hit every generated executor in every migrated app, and cost
    days precisely because setters were annotated and task executors were not — so an
    agent routed and collected slots perfectly and never ran a single task.
  Deliberately NOT checked here: a parameter with no default. `21` established that an
  empty-args fire cannot fill one, but that is a property of the FIRE, not of the tool —
  whether a given tool is ever fired with no arguments depends on the task that fires it.
  Checking the tool alone flags five of the framework's own healthy tools, and a check
  that cries wolf on `slot_filling_engine` is worse than no check at all.

  Args:
    app_dir: The emitted app directory.

  Returns:
    One human-readable line per defect, empty when every tool is dispatchable.
  """
  import ast  # noqa: PLC0415 - only needed on this path

  out: list[str] = []
  tools_dir = os.path.join(app_dir, "tools")
  if not os.path.isdir(tools_dir):
    return out
  for name in sorted(os.listdir(tools_dir)):
    doc_path = os.path.join(tools_dir, name, f"{name}.json")
    if not os.path.isfile(doc_path):
      continue
    try:
      with open(doc_path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    except (OSError, ValueError):
      continue  # an unreadable resource is already reported as `broken`
    fn = doc.get("pythonFunction") or {}
    entry = fn.get("name")
    if not entry:
      continue  # a remoteAgentTool or a builtin carries no python body
    try:
      with open(os.path.join(app_dir, fn.get("pythonCode") or ""),
                "r", encoding="utf-8") as fh:
        src = fh.read()
      tree = ast.parse(src)
    except (OSError, ValueError, SyntaxError) as exc:
      out.append(f"{name}: entry body is unreadable or will not parse ({exc})")
      continue
    # Top-level only, not `ast.walk`. CES calls the module's own `pythonFunction.name`, so
    # a function of that name nested inside another is not callable — and walking the whole
    # tree would find it, report the tool as healthy, and miss the very defect this exists
    # to catch. The same mistake would read a nested function's return annotation in place
    # of the absent top-level one.
    defs = {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    node = defs.get(entry)
    if node is None:
      out.append(
          f"{name}: declares entry function '{entry}' but the body defines "
          f"{sorted(defs) or 'nothing'} — CES calls the declared name and drops the "
          "call when it is absent")
      continue
    if node.returns is None:
      out.append(
          f"{name}: entry function '{entry}' has no return annotation, so CES will not "
          "register the tool")
    # A body that NameErrors on LOAD is dropped by CES SILENTLY — no error, no
    # function_response, dead air (ces-probes 153, flows #513/#556). The common shape:
    # an inlined self-referential pydantic class annotated `-> Cls` with no
    # `from __future__ import annotations` (which flows omits on purpose, see
    # tools._HEADER), so the annotation is evaluated eagerly at class-body time and the
    # name is not yet bound. Detected STATICALLY (never exec the body — that would run
    # author code at build/deploy and leak imports into the host).
    ref = _eager_forward_ref(tree)
    if ref:
      out.append(
          f"{name}: {ref} — CES silently drops a tool whose module fails to load. "
          "String-quote the annotation (e.g. `-> \"Cls\"`) or add "
          "`from __future__ import annotations` to the source.")
  return out


def _eager_forward_ref(tree) -> Optional[str]:
  """Return a description if the module would NameError on import from an EAGERLY-
  evaluated forward reference in an annotation, else None.

  Only real when there is no `from __future__ import annotations` (with it, annotations
  are lazy strings and never evaluated at import). Flags an annotation that references a
  class/function DEFINED IN THIS MODULE at or after the definition it sits in — i.e. a
  self-reference (`class C: def m(self) -> C`) or a forward reference to a later
  definition. Imports, builtins and typing/pydantic names are never in the local-def set,
  so they are never flagged; a string-quoted annotation is an `ast.Constant`, not a
  `Name`, so it is correctly treated as safe.
  """
  import ast  # noqa: PLC0415
  for n in tree.body:
    if (isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)):
      return None
  local_at = {}
  for i, n in enumerate(tree.body):
    if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
      local_at.setdefault(n.name, i)

  def _anns_of(defn):
    if isinstance(defn, ast.ClassDef):
      for m in defn.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
          a = m.args
          for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs,
                      a.vararg, a.kwarg]:
            if arg and arg.annotation:
              yield arg.annotation
          if m.returns:
            yield m.returns
        elif isinstance(m, ast.AnnAssign) and m.annotation:
          yield m.annotation
    else:  # a top-level function
      a = defn.args
      for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
        if arg and arg.annotation:
          yield arg.annotation
      if defn.returns:
        yield defn.returns

  for i, n in enumerate(tree.body):
    if not isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
      continue
    for ann in _anns_of(n):
      for sub in ast.walk(ann):
        if isinstance(sub, ast.Name) and local_at.get(sub.id, -1) >= i:
          kind = "self-reference" if sub.id == n.name else "forward reference"
          return (f"{sub.id!r} in a `{n.name}` annotation is a {kind} not yet bound at "
                  "import")
  return None


def _read_json(path: str, rep: IntegrityReport, label: str) -> Optional[dict]:
  try:
    with open(path, "r", encoding="utf-8") as f:
      return json.load(f)
  except FileNotFoundError:
    rep.broken.append(f"{label} is missing")
  except (OSError, ValueError) as exc:
    rep.broken.append(f"{label} is unreadable ({exc})")
  return None


def _has_tool(app_dir: str, name: str) -> bool:
  return os.path.isfile(os.path.join(app_dir, "tools", name, f"{name}.json"))


def _agent_jsons(app_dir: str) -> dict[str, dict]:
  """`{agent_name: parsed <agent>.json}` for every agent dir that has one."""
  out: dict[str, dict] = {}
  agents_dir = os.path.join(app_dir, "agents")
  if not os.path.isdir(agents_dir):
    return out
  for name in sorted(os.listdir(agents_dir)):
    path = os.path.join(agents_dir, name, f"{name}.json")
    if not os.path.isfile(path):
      continue
    try:
      with open(path, "r", encoding="utf-8") as f:
        out[name] = json.load(f)
    except (OSError, ValueError):
      continue
  return out


def _unresolved_agent_tools(app_dir: str, agent_jsons: dict[str, dict]) -> list[str]:
  """Tools an emitted agent CLAIMS to have but that have no resource in the dir.

  The symptom of the miss is a failed tool call mid-call, on the one turn that
  needed it — the same failure `_check_extra_tools` guards against at authoring
  time, checked here against what actually landed.
  """
  bad: list[str] = []
  for agent, aj in agent_jsons.items():
    tools = aj.get("tools")
    if not isinstance(tools, list):
      continue
    for tool in tools:
      if not isinstance(tool, str) or tool in CES_BUILTIN_TOOLS:
        continue
      if not _has_tool(app_dir, tool):
        bad.append(f"{agent} -> {tool}")
  return bad


def variable_names(declarations: Iterable[Any]) -> list[str]:
  """The `name` of each `variableDeclarations` entry, order-preserving, deduped."""
  names: list[str] = []
  for v in declarations or ():
    name = v.get("name") if isinstance(v, dict) else v
    if isinstance(name, str) and name and name not in names:
      names.append(name)
  return names


def config_tool_names(configs: Iterable[dict]) -> set[str]:
  """Every tool a config NAMES: task executors and slot setters.

  Read straight off the author's configs rather than off the scaffold's own file
  list, so the two sides of the diff have independent sources — a tool the
  emitter dropped is exactly what this is for.
  """
  names: set[str] = set()
  for cfg in configs or ():
    if not isinstance(cfg, dict):
      continue
    for task in cfg.get("tasks") or ():
      tool = task.get("tool") if isinstance(task, dict) else None
      if isinstance(tool, str) and tool:
        names.add(tool)
    for slot in cfg.get("slots") or ():
      setter = slot.get("setter") if isinstance(slot, dict) else None
      if isinstance(setter, str) and setter:
        names.add(setter)
  return names


def write_declared_settings(app_dir: str, keys: Iterable[str]) -> None:
  """Record the app.json top-level keys the author OWNS (`DECLARED_SETTINGS_FILE`).

  A no-op for an app that declares none, so the emitted tree is unchanged for
  everything written before this existed.
  """
  ordered = [k for k in dict.fromkeys(keys) if k]
  if not ordered:
    return
  path = os.path.join(app_dir, DECLARED_SETTINGS_FILE)
  with open(path, "w", encoding="utf-8") as f:
    json.dump({"_comment": _DECLARED_SETTINGS_NOTE, "declared": ordered}, f, indent=2)
    f.write("\n")


def declared_setting_keys(app_dir: str) -> list[str]:
  """The declared app-level keys recorded in an app dir, `[]` when there are none.

  Deliberately forgiving: a dir emitted by an older SDK has no sidecar, and a
  corrupt one must not break a deploy — both mean "nothing is declared", which is
  the pre-existing preserve-everything behaviour.
  """
  path = os.path.join(app_dir, DECLARED_SETTINGS_FILE)
  try:
    with open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
  except (OSError, ValueError):
    return []
  keys = data.get("declared") if isinstance(data, dict) else None
  if not isinstance(keys, list):
    return []
  return [k for k in keys if isinstance(k, str) and k]


def brief(value: Any, limit: int = 60) -> str:
  """A JSON value shortened to fit in one line of a report or a deploy log."""
  try:
    text = json.dumps(value, sort_keys=True)
  except (TypeError, ValueError):
    text = repr(value)
  return text if len(text) <= limit else text[: limit - 1] + "…"


def unlanded_settings(app_json: dict, declared: Any) -> list[str]:
  """Declared app-LEVEL settings that app.json does not carry as declared.

  `declared` is either `{key: value}` (an emit, which knows both) or an iterable of
  keys (a dir check, which knows only the names): with values it is an equality
  diff, with names alone it is a presence check.
  """
  bad: list[str] = []
  items = declared.items() if isinstance(declared, dict) else (
      (k, None) for k in (declared or ()))
  for key, want in items:
    if key not in app_json:
      bad.append(f"{key} (declared, app.json has none)")
    elif want is not None and app_json[key] != want:
      bad.append(
          f"{key} (declared {brief(want)}, app.json has {brief(app_json[key])})")
  return bad


def emitted_tool_names(files: Iterable[Any]) -> set[str]:
  """Tool resources a `ScaffoldResult.files` set intended to write."""
  names: set[str] = set()
  for f in files or ():
    path = getattr(f, "path", None)
    m = _TOOL_JSON_RE.match(path) if isinstance(path, str) else None
    if m:
      names.add(m.group(1))
  return names


def verify_emitted(
    app_dir: str,
    *,
    agents: Iterable[str],
    variables: Iterable[str],
    tools: Iterable[str],
    settings: Optional[dict[str, Any]] = None,
    framework: bool = True,
) -> IntegrityReport:
  """Diff what an emit ASKED for against what LANDED at `app_dir`.

  `agents` / `variables` / `tools` / `settings` are the emit's own intent (agent
  names, declared variable names, tool resource names, and the app-LEVEL app.json
  settings the `App` declared). Framework files are compared to the blessed manifest
  unless `framework=False`.
  """
  rep = IntegrityReport()
  app_json = _read_json(os.path.join(app_dir, "app.json"), rep, "app.json")
  if app_json is not None:
    landed = set(variable_names(app_json.get("variableDeclarations") or []))
    rep.missing_variables = [
        n for n in dict.fromkeys(variables) if n and n not in landed]
    rep.unlanded_settings = unlanded_settings(app_json, settings or {})
  agent_jsons = _agent_jsons(app_dir)
  rep.missing_agents = [a for a in dict.fromkeys(agents) if a not in agent_jsons]
  rep.missing_tools = sorted(
      t for t in set(tools) if t not in CES_BUILTIN_TOOLS and not _has_tool(app_dir, t))
  rep.unresolved_agent_tools = _unresolved_agent_tools(app_dir, agent_jsons)
  rep.undispatchable = undispatchable_tools(app_dir)
  if framework:
    drift = _bs.verify_app_dir(app_dir)
    rep.framework_missing = list(drift.missing)
    rep.framework_stale = list(drift.mismatched)
  return rep


def verify_dir(app_dir: str) -> IntegrityReport:
  """Check an app dir on its own terms — no `App`, no emit intent.

  What a dir can still be held to without the source that produced it: app.json
  parses and names a root agent that exists, every agent's declared tools resolve
  to a resource, every app-level setting the sidecar says the author declared is
  actually in app.json, and the framework files match the blessed manifest. This is
  the check `flows deploy` runs, because it can be handed a tree emitted by anything.
  """
  rep = IntegrityReport()
  if not os.path.isdir(app_dir):
    rep.broken.append(f"{app_dir} is not a directory")
    return rep
  app_json = _read_json(os.path.join(app_dir, "app.json"), rep, "app.json")
  agent_jsons = _agent_jsons(app_dir)
  if not agent_jsons:
    rep.broken.append("no agents/<name>/<name>.json in the app dir")
  if app_json is not None:
    root = app_json.get("rootAgent")
    if not root:
      rep.broken.append("app.json declares no rootAgent")
    elif root not in agent_jsons:
      rep.missing_agents.append(str(root))
    # Names only — the sidecar records WHICH settings are the author's, not their
    # values, so a dir check can catch a declared setting stripped out of app.json
    # (a hand edit, a bad merge) but not one edited to a different value.
    rep.unlanded_settings = unlanded_settings(
        app_json, declared_setting_keys(app_dir))
  rep.unresolved_agent_tools = _unresolved_agent_tools(app_dir, agent_jsons)
  rep.undispatchable = undispatchable_tools(app_dir)
  drift = _bs.verify_app_dir(app_dir)
  rep.framework_missing = list(drift.missing)
  rep.framework_stale = list(drift.mismatched)
  return rep


# ---------------------------------------------------------------------------
# A scaffold that said ok=False: say WHY, and leave nothing deployable behind.
#
# `scaffold.build` obeys a crash-envelope rule — it never raises, every failure
# comes back as `ScaffoldResult(ok=False, ...)` — and `build` writes the tree
# BEFORE it computes `ok`. Both halves of that contract are the CALLER's to
# honour, and every caller got them wrong the same way:
#
#   * `"; ".join(res.validation.errors) if res.validation else <fallback>` reads
#     ONE of the four reason sources, and since `validation` is a pydantic model
#     it is always truthy — so the fallback is dead code that can never run. The
#     two failures that are not dag-validation failures (a framework bundle that
#     drifted from its manifest, and duplicate UUIDs) leave `errors` EMPTY, and
#     the operator is told `scaffold failed:` and nothing at all.
#   * Returning/raising after the write leaves a complete-LOOKING app dir on
#     disk. One of those was pushed to a live CES app and scored 24/43 before
#     anyone read the log.
#
# These live here rather than in `build.py` so the studio's `framework new` CLI
# (which cannot import the authoring builder) shares the one implementation.
# ---------------------------------------------------------------------------

FAILED_MARKER = "EMIT_FAILED.txt"


def scaffold_failure_reason(res: Any, exclude: tuple[str, ...] = ()) -> str:
  """Why the scaffold said `ok=False`, read from the WHOLE envelope.

  `ok` is `validation.valid and callback_sync_ok and uuids_unique`, and a crashed
  build reports its exception in `error`; three of those four sources used to be
  dropped on the floor. `exclude` mirrors the path prefixes the scaffold's own
  drift gate skips (a transfer host's custom callbacks), so the reason names the
  same files the gate judged and no others.

  Duck-typed on `ScaffoldResult` rather than importing it: this module is on the
  import path of the framework CLI, which must stay off the emit stack.
  """
  parts: list[str] = []
  error = getattr(res, "error", None)
  validation = getattr(res, "validation", None)
  if error:  # crash-envelope text, already prefixed by the scaffolder
    parts.append(re.sub(r"^Scaffold failed:\s*", "", str(error)))
  if validation is not None and getattr(validation, "errors", None):
    parts.append("dag validation: " + "; ".join(validation.errors))
  if not getattr(res, "callback_sync_ok", True):
    provided = {f.path: f.content for f in (getattr(res, "files", None) or ())
                if not f.path.startswith(exclude or ())}
    drift = _bs.verify_files(provided) if provided else None
    detail = f": {drift.summary()}" if drift is not None and not drift.ok else ""
    parts.append(
        "framework drift — the emitted framework files do not match the blessed "
        f"manifest (v{getattr(res, 'framework_version', None) or _bs.version()})"
        f"{detail}. If the blessed bundle changed on purpose, regenerate the "
        "manifest with blessed_source.write_manifest(<new version>)")
  if not getattr(res, "uuids_unique", True):
    parts.append("duplicate resource UUIDs across the emitted app/agent/tool jsons")
  if not parts:  # ok=False with every reason field empty — say that, don't say "".
    parts.append(
        "scaffold reported ok=False with no reason recorded (validation.valid="
        f"{bool(validation is not None and getattr(validation, 'valid', False))}, "
        f"callback_sync_ok={getattr(res, 'callback_sync_ok', None)}, "
        f"uuids_unique={getattr(res, 'uuids_unique', None)})")
  return "; ".join(parts)


def discard_tree(
    path: Optional[str], reason: str = "", keep: bool = False, *,
    what: str = "flows emit",
) -> str:
  """Remove the half-built tree (or mark it), returning a note for the message.

  Only ever called with a path THIS run wrote (`ScaffoldResult.written_to`, or the
  out_dir once the post-emit steps have run) — never with a directory the caller
  declined to touch, or `--no-overwrite` would delete the good app that made it
  decline. `keep` trades the guarantee for a debuggable carcass, and stamps it so
  nothing can mistake it for a finished app.
  """
  if not path or not os.path.isdir(path):
    return ""
  if keep:
    with open(os.path.join(path, FAILED_MARKER), "w") as f:
      f.write(
          f"INCOMPLETE APP — `{what}` FAILED and the tree was kept for debugging. "
          "DO NOT DEPLOY IT.\n\n" + reason + "\n")
    return f"; kept the INCOMPLETE tree at {path} ({FAILED_MARKER}) — do not deploy it"
  shutil.rmtree(path, ignore_errors=True)
  return f"; removed the incomplete app dir {path}"
