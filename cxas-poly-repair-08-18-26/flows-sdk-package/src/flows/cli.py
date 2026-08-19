"""`flows` command-line interface.

    flows version                     # the pinned framework version
    flows new <dir>                   # scaffold a starter project
    flows validate <module>           # compile + run the real framework validator
    flows emit <module> --out <dir>   # write a deployable CXAS app dir
    flows check --app-dir <dir>       # integrity + framework drift check on an app dir
    flows deploy --app-dir <dir> --to <ces-app>   # push to a live CES app ([deploy] extra)
    flows cujs [name]                 # list CUJ presets, or show one's variables
    flows turn --app <id> --text "..."            # ONE turn, resumable, --json
    flows chat --app <id> --say "..."             # drive a whole conversation
    flows cuj-apply --cuj <name> --app-dir <dir>  # bake a CUJ into the app's defaults

`<module>` is an importable module (dotted or a .py path) that defines a top-level
`app` (a `flows.App`) and imports its `@flows.tool` tools. `flows deploy` shells out
to the `cxas` CLI from the optional `[deploy]` extra. The CUJ commands read a
`cujs.yaml` found next to the app (or above it); see `flows.cujs`.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
from typing import Any


def _package_context(path: str) -> tuple[str, str]:
  """`("<dir to put on sys.path>", "dotted.module.name")` for a file inside a package.

  A path INSIDE a package cannot be loaded as a lone file: `spec_from_file_location`
  gives the module no `__package__`, so the first `from . import cues` in it dies with
  "attempted relative import with no known parent package". Any agent split across more
  than one file hits this, which is every non-trivial one — and the documented workflow,
  the Makefile and CI all pass a path (`flows validate src/my_agent/agent.py`).

  The package root is derived the way Python itself derives one: walk up while the
  directory has an `__init__.py`, and the first directory that does NOT is the one that
  belongs on `sys.path` (`src/` for the src-layout). Returns `("", "")` for a file that
  is in no package, so the caller keeps the by-path behavior for a single-file agent.
  """
  directory, filename = os.path.split(path)
  stem = filename[:-3] if filename.endswith(".py") else filename
  parts: list[str] = []
  while os.path.isfile(os.path.join(directory, "__init__.py")):
    directory, pkg = os.path.split(directory)
    if not pkg:
      break
    parts.insert(0, pkg)
  if not parts:
    return "", ""
  # `agent/__init__.py` IS the package; anything else is a submodule of it.
  dotted = ".".join(parts) if stem == "__init__" else ".".join([*parts, stem])
  return directory, dotted


def _load_module(ref: str) -> Any:
  """Import a module by dotted name or filesystem path."""
  if ref.endswith(".py") or os.path.sep in ref:
    path = os.path.abspath(ref)
    pkg_root, dotted = _package_context(path)
    if dotted:
      # Import it as what it IS — a module of its package — so relative imports,
      # `__package__` and `importlib.resources` all behave as they do at run time.
      # sys.path[0] so the checkout wins over an installed copy of the same name.
      sys.path.insert(0, pkg_root)
      try:
        return importlib.import_module(dotted)
      except ImportError:
        # Not importable as a package after all (a stray `__init__.py`, a namespace
        # collision): fall through to the by-path loader rather than failing outright.
        pass
    spec = importlib.util.spec_from_file_location("_flows_user_module", path)
    if spec is None or spec.loader is None:
      raise SystemExit(f"cannot load module from {ref}")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.dirname(path))
    # Register before exec so inspect.getsource() can find tool/model source
    # (getsource resolves a class via sys.modules[cls.__module__]).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod
  return importlib.import_module(ref)


def _get_app(ref: str):
  mod = _load_module(ref)
  app = getattr(mod, "app", None)
  if app is None:
    raise SystemExit(f"module {ref!r} must define a top-level `app = flows.App(...)`")
  return app


def _cmd_version(_args) -> int:
  from . import version

  print(version())
  return 0


def _cmd_validate(args) -> int:
  from .authoring.build import validate_app

  app = _get_app(args.module)
  errors, warnings = validate_app(app)
  for w in warnings:
    print(f"warn: {w}")
  for e in errors:
    print(f"error: {e}")
  if errors:
    print(f"\n{len(errors)} error(s) — invalid.")
    return 1
  print("validate: clean")
  return 0


def _cmd_lint(args) -> int:
  from .lint import lint_app, load_all_rules
  from .lint.render import (
      render_explain, render_human, render_json, render_list_rules)

  if args.list_rules:
    reg = load_all_rules()
    print(render_list_rules(reg, as_json=(args.format == "json")))
    return 0
  if args.explain:
    reg = load_all_rules()
    r = reg.get(args.explain)
    if r is None:
      print(f"lint: no such rule {args.explain!r} (try --list-rules)", file=sys.stderr)
      return 2
    print(render_explain(r))
    return 0
  if not args.module:
    print("lint: a module is required (or use --list-rules / --explain)", file=sys.stderr)
    return 2

  def _split(v):
    return [x.strip() for x in v.split(",") if x.strip()] if v else None

  app = _get_app(args.module)
  report = lint_app(app, select=_split(args.select), ignore=_split(args.ignore))
  if args.format == "json":
    print(render_json(report))
  else:
    print(render_human(report, show_suppressed=args.show_suppressed))
  return 0 if report.ok(strict=args.strict) else 1


def _cmd_emit(args) -> int:
  from .authoring.build import emit

  app = _get_app(args.module)
  try:
    res = emit(app, args.out, overwrite=not args.no_overwrite,
               keep_failed=args.keep_failed)
  except ValueError as e:
    # LOUD: the real reason, on stderr, non-zero — never an `emit: ok` line and
    # never a bare `scaffold failed:` with the cause swallowed. Everything
    # downstream (`flows deploy`, CI) reads this exit code.
    print(f"emit: FAILED — {e}", file=sys.stderr)
    return 1
  print(f"emit: ok -> {res.written_to or args.out}  (framework v{res.framework_version})")
  return 0


def _cmd_check(args) -> int:
  from .authoring.integrity import verify_dir

  report = verify_dir(args.app_dir)
  print(report.summary())
  return 0 if report.ok else 1


def _cmd_new(args) -> int:
  from .templates import scaffold_project

  dest = scaffold_project(args.dir, name=args.name)
  print(f"new: created starter project at {dest}")
  print("  edit flows/, tools/, agent.yaml — then: flows validate app.py && flows emit app.py --out ./app")
  return 0


def _cmd_deploy(args) -> int:
  # Lazy: the deploy path shells out to `cxas` (the [deploy] extra), so keep it
  # out of the lightweight core import surface.
  try:
    from .deploy.push import deploy
  except ImportError as e:
    print(f"deploy: requires the [deploy] extra — `pip install 'flows[deploy]'` ({e})", file=sys.stderr)
    return 1
  try:
    out = deploy(
        args.app_dir,
        args.to,
        cxas=args.cxas,
        preserve_from_target=not args.no_preserve,
        audio_bucket=args.audio_bucket,
        inactivity_timeout=args.inactivity_timeout,
        barge_in_awareness=args.barge_in_awareness,
        verify=not args.no_verify,
    )
  except FileNotFoundError as e:
    # `cxas` not on PATH, or the app-dir is missing.
    print(f"deploy: {e} — is the [deploy] extra installed and `{args.cxas}` on PATH?", file=sys.stderr)
    return 1
  except RuntimeError as e:
    print(f"deploy: {e}", file=sys.stderr)
    return 1
  print(out)
  return 0


def _load_cujs_or_exit(args, cmd: str):
  """Resolve the cujs.yaml, reporting a bad file/name as a message not a traceback."""
  from .cujs import load_cujs

  try:
    cujs = load_cujs(args.file)
  except (FileNotFoundError, ValueError) as e:
    raise SystemExit(f"{cmd}: {e}")
  name = getattr(args, "cuj", None) or getattr(args, "name", None)
  if not name:
    return cujs, None
  try:
    return cujs, cujs[name]
  except KeyError as e:
    raise SystemExit(f"{cmd}: {e.args[0]}")


def _cmd_cujs(args) -> int:
  cujs, cuj = _load_cujs_or_exit(args, "cujs")
  if not args.name:
    if args.json:
      print(json.dumps({c.name: c.variables for c in cujs}, indent=2))
      return 0
    labels = {c.name: c.name + (f" ({', '.join(c.aliases)})" if c.aliases else "")
              for c in cujs}
    width = max((len(v) for v in labels.values()), default=0)
    for c in cujs:
      print(f"  {labels[c.name]:{width}}  {c.description}")
    return 0
  if args.json:
    print(json.dumps(cuj.variables, indent=2))
  else:
    for k, v in cuj.variables.items():
      print(f"  {k} = {v}")
  return 0


def _key_values(pairs: list[str] | None, flag: str) -> dict:
  """`--flag k=v` repeated -> a dict. A JSON value is decoded, else kept a string."""
  out: dict[str, Any] = {}
  for pair in pairs or []:
    key, sep, raw = pair.partition("=")
    if not sep:
      raise SystemExit(f"{flag}: expected k=v, got {pair!r}")
    try:
      out[key] = json.loads(raw)
    except ValueError:
      out[key] = raw
  return out


def _cmd_turn(args) -> int:
  from .drive import turn

  # `is not None`, not truthiness: an EMPTY --text is a real input. It is how you
  # drive the turn a silent caller produces, which is the one thing a no_input ladder
  # exists to handle — and rejecting it told the caller they had passed no input when
  # they had passed exactly one.
  inputs = [args.text is not None, args.dtmf is not None, args.event is not None]
  if sum(inputs) != 1:
    raise SystemExit("turn: pass exactly one of --text, --dtmf, --event")

  try:
    result = turn(
        args.app, session_id=args.session, text=args.text, dtmf=args.dtmf,
        event=args.event, event_vars=_key_values(args.event_var, "--event-var") or None,
        variables=_key_values(args.var, "--var") or None, modality=args.modality,
        use_tool_fakes=not args.no_fakes, project=args.project, location=args.location)
  except ImportError as e:
    print(f"turn: {e}", file=sys.stderr)
    return 3

  if args.json:
    print(json.dumps(result, indent=2, default=str))
    return 0

  print(f"you   > {result['input']}", file=sys.stderr)
  print(f"agent > {result['agent_text']}")
  tools = [c.get("action") for c in result["tool_calls"]]
  if tools:
    print(f"        [tools: {', '.join(t for t in tools if t)}]", file=sys.stderr)
  print(f"        [session: {result['session_id']}]", file=sys.stderr)
  return 0


def _cmd_chat(args) -> int:
  from .drive import chat, open_session, run_steps

  # A CUJ is how you seed variables, not a precondition for talking to an app —
  # requiring one meant an app with no cujs.yaml could not be driven at all.
  cuj = None
  if args.cuj:
    _cujs, cuj = _load_cujs_or_exit(args, "chat")
  variables = cuj if cuj is not None else {}

  try:
    if args.json:
      if not args.say:
        raise SystemExit("chat: --json needs at least one --say (a REPL has no result)")
      session = open_session(variables, args.app, project=args.project,
                             location=args.location,
                             use_tool_fakes=not args.no_fakes)
      results = run_steps(variables, args.app, args.say, session=session)
      print(json.dumps({
          "session_id": session.session_id,
          "turns": [{"input": r.utterance, "agent_text": r.text,
                     "tool_calls": r.tool_calls} for r in results],
          "session_ended": session.is_ended,
          "filled_slots": session.get_state()["filled_slots"],
      }, indent=2, default=str))
      return 0
    return chat(variables, args.app, say=args.say or None, project=args.project,
                location=args.location, use_tool_fakes=not args.no_fakes)
  except ImportError as e:
    print(f"chat: {e}", file=sys.stderr)
    return 3


def _cmd_cuj_apply(args) -> int:
  from .cujs import _find_app_json, apply_to_app_dir

  _cujs, cuj = _load_cujs_or_exit(args, "cuj-apply")
  if args.dry_run:
    # Name the file the REAL run would write. `_find_app_json` also accepts an
    # app.json one directory down (the shape `cxas pull` produces), so printing
    # `<app-dir>/app.json` names a path that need not even exist while the real
    # run quietly writes the nested one.
    try:
      target = _find_app_json(args.app_dir)
    except OSError as e:
      print(f"cuj-apply: {e}", file=sys.stderr)
      return 1
    print(f"would set in {target} (cuj: {cuj.name}):")
    for k, v in cuj.variables.items():
      print(f"  {k}.schema.default = {v}")
    return 0

  try:
    written = apply_to_app_dir(args.app_dir, cuj)
  except (ValueError, FileNotFoundError) as e:
    print(f"cuj-apply: {e}", file=sys.stderr)
    return 1
  print(f"cuj-apply: set defaults for {', '.join(written)} from cuj {cuj.name!r}")
  _warn_if_no_console_fakes(args.app_dir)

  if not args.to:
    return 0
  try:
    from .deploy.push import deploy
  except ImportError as e:
    print(f"cuj-apply: --to requires the [deploy] extra — `pip install 'flows[deploy]'` ({e})",
          file=sys.stderr)
    return 1
  try:
    out = deploy(args.app_dir, args.to, cxas=args.cxas,
                 preserve_from_target=not args.no_preserve)
  except (FileNotFoundError, RuntimeError) as e:
    print(f"cuj-apply: {e}", file=sys.stderr)
    return 1
  print(out)
  # A push updates the DRAFT. The console reads the draft; a phone/GTP deployment
  # serves a pinned version and keeps running the old build until it is promoted.
  print(f"\ncuj-apply: pushed the DRAFT of {args.to}."
        "\n  The CES console will now open on this CUJ."
        "\n  A phone/GTP number will NOT until you promote this version.")
  return 0


def _warn_if_no_console_fakes(app_dir: str) -> None:
  """A console session never fires toolFakeConfig, so a CUJ needs a demo build."""
  for root, _dirs, files in os.walk(app_dir):
    for name in files:
      if not name.endswith(".py"):
        continue
      try:
        with open(os.path.join(root, name), encoding="utf-8", errors="ignore") as f:
          hit = "mock_config_string" in f.read()
      except OSError:
        continue  # a broken symlink or an unreadable file is not the answer either
      if hit:
        return
  print("cuj-apply: WARNING — nothing in this app dir reads `mock_config_string`."
        " A CES console session does not fire toolFakeConfig mocks, so the seeded"
        " variables may have no effect. Build with the demo sweep.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
  p = argparse.ArgumentParser(prog="flows", description=__doc__.splitlines()[0])
  sub = p.add_subparsers(dest="cmd", required=True)

  sub.add_parser("version", help="print the pinned framework version")

  sp = sub.add_parser("new", help="scaffold a starter project")
  sp.add_argument("dir")
  sp.add_argument("--name", default="My Agent")

  sp = sub.add_parser("validate", help="validate a flows App module")
  sp.add_argument("module")

  sp = sub.add_parser("lint", help="build-time authoring linter (rules, severities, fixes)")
  sp.add_argument("module", nargs="?", help="the flows App module (omit with --list-rules/--explain)")
  sp.add_argument("--format", choices=("human", "json"), default="human")
  sp.add_argument("--select", default=None,
                  help="comma-separated rule codes/categories to run (default: all)")
  sp.add_argument("--ignore", default=None,
                  help="comma-separated rule codes/categories to skip")
  sp.add_argument("--strict", action="store_true",
                  help="treat warnings as blocking (exit 1) too")
  sp.add_argument("--show-suppressed", action="store_true",
                  help="include suppressed findings in human output")
  sp.add_argument("--list-rules", action="store_true", help="print the rule catalog and exit")
  sp.add_argument("--explain", default=None, metavar="CODE",
                  help="print one rule's rationale + docs and exit")

  sp = sub.add_parser("emit", help="emit a deployable CXAS app dir")
  sp.add_argument("module")
  sp.add_argument("--out", required=True)
  sp.add_argument("--no-overwrite", action="store_true")
  sp.add_argument("--keep-failed", action="store_true",
                  help="on failure keep the INCOMPLETE tree (marked EMIT_FAILED.txt) "
                       "for debugging instead of removing it")

  sp = sub.add_parser("check", help="integrity + framework drift check on an app dir")
  sp.add_argument("--app-dir", required=True)

  sp = sub.add_parser("deploy", help="deploy an emitted app dir to a CES app ([deploy] extra)")
  sp.add_argument("--app-dir", required=True)
  sp.add_argument("--to", required=True, help="target CES app resource to --overwrite")
  sp.add_argument("--cxas", default="cxas", help="path to the cxas CLI")
  sp.add_argument("--no-preserve", action="store_true",
                  help="skip pulling+merging the live target's app-level settings")
  sp.add_argument("--no-verify", action="store_true",
                  help="skip the pre-push integrity/framework-drift check on --app-dir")
  sp.add_argument("--audio-bucket", default=None, help="enforce a call-recording GCS bucket")
  sp.add_argument("--inactivity-timeout", default="8s", help="hold-and-wait countdown timeout")
  sp.add_argument("--barge-in-awareness", dest="barge_in_awareness",
                  action="store_true", default=None,
                  help="tell the agent what the caller heard before interrupting it "
                       "(the cut happens either way; this only adds the report)")

  sp = sub.add_parser("cujs", help="list CUJ presets, or show one's variables")
  sp.add_argument("name", nargs="?", help="a CUJ name or alias")
  sp.add_argument("--file", default=None, help="path to a cujs.yaml (default: discovered)")
  sp.add_argument("--json", action="store_true")

  sp = sub.add_parser("turn", help="send ONE input to a deployed app; resumable")
  sp.add_argument("--app", required=True, help="app UUID or full resource name")
  sp.add_argument("--session", default=None,
                  help="continue this session id (omit to start one)")
  sp.add_argument("--text", default=None, help="what the caller says")
  sp.add_argument("--dtmf", default=None, help="keypad digits, e.g. 1 or 1234#")
  sp.add_argument("--event", default=None, help="fire a named event instead of speaking")
  sp.add_argument("--event-var", action="append", metavar="K=V",
                  help="event variable (repeatable); a JSON value is decoded")
  sp.add_argument("--var", action="append", metavar="K=V",
                  help="session variable (repeatable); a JSON value is decoded")
  sp.add_argument("--modality", default=None, choices=["text", "audio"])
  sp.add_argument("--no-fakes", action="store_true", help="do not force use_tool_fakes")
  sp.add_argument("--project", default="ces-deployment-dev")
  sp.add_argument("--location", default="us")
  sp.add_argument("--json", action="store_true", help="one JSON object on stdout")

  sp = sub.add_parser("chat", help="drive a deployed app, scripted or as a REPL")
  sp.add_argument("--cuj", default=None, help="seed variables from this CUJ")
  sp.add_argument("--app", required=True, help="app UUID or full resource name")
  sp.add_argument("--file", default=None)
  sp.add_argument("--say", action="append", help="scripted turn (repeatable); omit for a REPL")
  sp.add_argument("--no-fakes", action="store_true", help="do not force use_tool_fakes")
  sp.add_argument("--project", default="ces-deployment-dev")
  sp.add_argument("--location", default="us")
  sp.add_argument("--json", action="store_true",
                  help="one JSON object on stdout (needs --say)")

  sp = sub.add_parser("cuj-apply", help="bake a CUJ into an app dir's variable defaults")
  sp.add_argument("--cuj", required=True)
  sp.add_argument("--app-dir", required=True)
  sp.add_argument("--file", default=None)
  sp.add_argument("--to", default=None, help="also push to this CES app resource ([deploy] extra)")
  sp.add_argument("--cxas", default="cxas", help="path to the cxas CLI")
  sp.add_argument("--no-preserve", action="store_true")
  sp.add_argument("--dry-run", action="store_true", help="print the defaults, write nothing")

  args = p.parse_args(argv)
  return {
      "version": _cmd_version,
      "new": _cmd_new,
      "validate": _cmd_validate,
      "lint": _cmd_lint,
      "emit": _cmd_emit,
      "check": _cmd_check,
      "deploy": _cmd_deploy,
      "cujs": _cmd_cujs,
      "turn": _cmd_turn,
      "chat": _cmd_chat,
      "cuj-apply": _cmd_cuj_apply,
  }[args.cmd](args)


if __name__ == "__main__":
  raise SystemExit(main())
