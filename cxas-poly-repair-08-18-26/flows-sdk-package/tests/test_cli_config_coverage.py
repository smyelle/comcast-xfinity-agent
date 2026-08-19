"""The `flows` CLI and the config import/scan/validate layer, driven end to end.

Three surfaces that CI keys on and almost nothing else exercises:

  * **`flows.cli`** — `main(argv)` IS the console entry point, so its integer return
    code and what it writes to stdout/stderr are the contract. Every subcommand is
    driven through `main` here: the offline ones (`version`/`new`/`validate`/`lint`/
    `emit`/`check`/`cujs`/`cuj-apply`) run for real against `tmp_path`, and the two
    that reach the network (`deploy`, `chat`) are driven through their lazily
    imported seams — `flows.deploy.push.deploy` and `flows.drive.chat` — so no test
    shells out to `cxas`, touches GCP, or opens a session.
  * **`flows.config.config_io`** — three import paths, one normal form, two exports.
  * **`flows.config.tool_scan`** — a FILESYSTEM scanner over
    `<tool>/python_function/python_code.py` dirs; it never imports a tool.
  * **`flows.config.validation`** — the verdict is the framework validator's, the
    anchoring is ours, and the validator's structured `diagnostics` twin (when it
    supplies one) outranks the regex mapper.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_cli_config_coverage.py
"""

from __future__ import annotations

import json
import os
import sys
import types

import pytest
import yaml

import flows
from flows.authoring import tools as authoring_tools
from flows.cli import main
from flows.config import config_io, models, tool_scan, validation
from flows.engine import loader as fb

SUBCOMMANDS = ["version", "new", "validate", "lint", "emit", "check", "deploy",
               "cujs", "chat", "cuj-apply"]

CUJS_FILE = {
    "variable_aliases": {"account": ["accountNumber", "account_id"]},
    "querystring_variables": ["mock_config_string"],
    "defaults": {"variables": {"mock_config_string": {"outage": "none"}}},
    "cujs": {
        "widget_reboot": {
            "description": "Widget fault, reboot offered.",
            "aliases": ["reboot"],
            "variables": {"account": "1234",
                          "mock_config_string": {"gateway": "reboot"}},
        },
        "plain": {"description": "Nothing special."},
    },
}

# `widget_reboot` resolved: aliases fanned out, the mapping querystring-serialized.
REBOOT_VARS = {
    "accountNumber": "1234",
    "account_id": "1234",
    "mock_config_string": "outage=none&gateway=reboot",
}

GOOD_APP = '''\
import flows

flow = flows.Flow("widget", root_agent="Widget_Agent")
flow.add(
    flows.user_slot("item", "Which item?"),
    flows.announce("bye", ["Goodbye."], end=True),
)
app = flows.App(root_flow=flow, app_display_name="Widget_Agent")
'''

# `res` points at a task nobody declared: validation errors, and lint FLX001s.
BAD_APP = '''\
import flows

flow = flows.Flow("widget", root_agent="Widget_Agent")
flow.add(
    flows.user_slot("item", "Which item?"),
    flows.result_slot("res", "nosuchtask"),
    flows.announce("bye", ["Goodbye."], end=True),
)
app = flows.App(root_flow=flow, app_display_name="Widget_Agent")
'''


@pytest.fixture(autouse=True)
def isolate_process_state(monkeypatch, tmp_path_factory):
  """`_load_module` mutates sys.path/sys.modules and `@flows.tool` writes a global.

  Only modules loaded out of a tmp dir are unregistered: dropping a lazily imported
  `flows.*` submodule would leave a stale attribute on the package and silently
  defeat the `monkeypatch.setattr` seams below. The framework root is pinned to the
  packaged default so no ambient env/settings value leaks into a scan test.
  """
  monkeypatch.delenv("FLOWS_CUJS", raising=False)
  monkeypatch.delenv(fb.ENV_FRAMEWORK_ROOT, raising=False)
  monkeypatch.setattr(fb, "_SETTINGS_FRAMEWORK_ROOT", None)
  monkeypatch.setattr(sys, "path", list(sys.path))
  before = set(sys.modules)
  saved_registry = dict(authoring_tools._REGISTRY)  # noqa: SLF001
  yield
  tmp_root = str(tmp_path_factory.getbasetemp())
  for name in set(sys.modules) - before:
    origin = getattr(sys.modules.get(name), "__file__", None) or ""
    if name == "_flows_user_module" or origin.startswith(tmp_root):
      del sys.modules[name]
  authoring_tools._REGISTRY.clear()  # noqa: SLF001
  authoring_tools._REGISTRY.update(saved_registry)  # noqa: SLF001


@pytest.fixture
def cujs_file(tmp_path):
  path = tmp_path / "cujs.yaml"
  path.write_text(yaml.safe_dump(CUJS_FILE, sort_keys=False), encoding="utf-8")
  return str(path)


@pytest.fixture
def app_json_dir(tmp_path):
  """An emitted-app-shaped dir whose app.json declares the CUJ's variables."""
  d = tmp_path / "built"
  d.mkdir()
  (d / "app.json").write_text(json.dumps({
      "displayName": "Widget_Agent",
      "variableDeclarations": [
          {"name": "accountNumber", "schema": {"type": "STRING"}},
          {"name": "account_id", "schema": {"type": "STRING", "default": "stale"}},
          {"name": "mock_config_string", "schema": {"type": "STRING"}},
      ],
  }), encoding="utf-8")
  return d


def _write(path, text: str) -> str:
  path.write_text(text, encoding="utf-8")
  return str(path)


def _scaffold(tmp_path, name: str = "Widget Agent") -> str:
  """A starter project on disk (what `validate`/`lint`/`emit`/`check` run against)."""
  proj = tmp_path / "proj"
  assert main(["new", str(proj), "--name", name]) == 0
  return str(proj / "app.py")


# ===========================================================================
# cli: top level
# ===========================================================================

def test_top_level_help_lists_every_subcommand(capsys):
  with pytest.raises(SystemExit) as exc:
    main(["--help"])
  assert exc.value.code == 0
  out = capsys.readouterr().out
  assert "usage: flows" in out
  for cmd in SUBCOMMANDS:
    assert cmd in out


def test_help_describes_the_tool_from_the_module_docstring(capsys):
  with pytest.raises(SystemExit):
    main(["--help"])
  assert "command-line interface" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", SUBCOMMANDS)
def test_every_subcommand_has_help(cmd, capsys):
  with pytest.raises(SystemExit) as exc:
    main([cmd, "--help"])
  assert exc.value.code == 0
  assert f"usage: flows {cmd}" in capsys.readouterr().out


def test_a_subcommand_is_required():
  # `required=True` on the subparsers: bare `flows` is a usage error, not a no-op.
  with pytest.raises(SystemExit) as exc:
    main([])
  assert exc.value.code == 2


def test_unknown_subcommand_is_a_usage_error(capsys):
  with pytest.raises(SystemExit) as exc:
    main(["frobnicate"])
  assert exc.value.code == 2
  assert "invalid choice" in capsys.readouterr().err


# ===========================================================================
# cli: version / new
# ===========================================================================

def test_version_prints_the_pinned_framework_version(capsys):
  assert main(["version"]) == 0
  assert capsys.readouterr().out.strip() == flows.version()


def test_new_scaffolds_the_documented_layout(tmp_path, capsys):
  dest = tmp_path / "starter"
  assert main(["new", str(dest)]) == 0
  assert (dest / "app.py").is_file()
  assert (dest / "README.md").is_file()
  for sub in ("flows", "tools", "evals"):
    assert (dest / sub).is_dir()
  out = capsys.readouterr().out
  assert f"new: created starter project at {dest}" in out
  assert "flows validate app.py" in out            # the next-step hint


def test_new_default_name_is_used_when_no_flag_is_passed(tmp_path):
  dest = tmp_path / "starter"
  assert main(["new", str(dest)]) == 0
  assert "My Agent" in (dest / "app.py").read_text(encoding="utf-8")


def test_new_name_flag_reaches_the_scaffold(tmp_path):
  dest = tmp_path / "starter"
  assert main(["new", str(dest), "--name", "Widget_Agent"]) == 0
  assert "Widget_Agent" in (dest / "app.py").read_text(encoding="utf-8")
  assert "Widget_Agent" in (dest / "README.md").read_text(encoding="utf-8")


def test_new_requires_a_destination():
  with pytest.raises(SystemExit) as exc:
    main(["new"])
  assert exc.value.code == 2


# ===========================================================================
# cli: module loading (shared by validate / lint / emit)
# ===========================================================================

def test_a_module_can_be_named_by_dotted_name(tmp_path, monkeypatch, capsys):
  _write(tmp_path / "widget_app_mod.py", GOOD_APP)
  monkeypatch.syspath_prepend(str(tmp_path))
  assert main(["validate", "widget_app_mod"]) == 0
  assert "validate: clean" in capsys.readouterr().out


def test_a_package_structured_agent_is_imported_as_its_package(tmp_path, capsys):
  pkg = tmp_path / "src" / "widget_agent"
  pkg.mkdir(parents=True)
  (pkg / "__init__.py").write_text("", encoding="utf-8")
  (pkg / "cues.py").write_text('ASK = "Which item?"\n', encoding="utf-8")
  (pkg / "agent.py").write_text(
      "import flows\n\nfrom . import cues\n\n"
      'flow = flows.Flow("widget", root_agent="Widget_Agent")\n'
      'flow.add(flows.user_slot("item", cues.ASK),\n'
      '         flows.announce("bye", ["Goodbye."], end=True))\n'
      'app = flows.App(root_flow=flow, app_display_name="Widget_Agent")\n',
      encoding="utf-8")
  assert main(["validate", str(pkg / "agent.py")]) == 0
  assert "validate: clean" in capsys.readouterr().out


def test_a_package_that_will_not_import_falls_back_to_the_by_path_loader(
    tmp_path, capsys):
  """A stray `__init__.py` must not make a loadable file unloadable."""
  pkg = tmp_path / "src" / "broken_pkg"
  pkg.mkdir(parents=True)
  (pkg / "__init__.py").write_text(
      "import a_module_that_does_not_exist_anywhere\n", encoding="utf-8")
  (pkg / "agent.py").write_text(GOOD_APP, encoding="utf-8")
  assert main(["validate", str(pkg / "agent.py")]) == 0
  assert "validate: clean" in capsys.readouterr().out


def test_the_package_walk_stops_at_the_filesystem_root(tmp_path, monkeypatch):
  """Every directory claiming an `__init__.py` must still terminate the walk."""
  from flows import cli

  monkeypatch.setattr(cli.os.path, "isfile", lambda _p: True)
  path_root, dotted = cli._package_context(str(tmp_path / "a" / "b" / "m.py"))  # noqa: SLF001
  assert path_root == os.path.sep
  assert dotted.endswith(".a.b.m")


def test_package_root_detection_for_the_ordinary_shapes(tmp_path):
  from flows import cli

  pkg = tmp_path / "src" / "a" / "b"
  pkg.mkdir(parents=True)
  (tmp_path / "src" / "a" / "__init__.py").write_text("", encoding="utf-8")
  (pkg / "__init__.py").write_text("", encoding="utf-8")
  assert cli._package_context(str(pkg / "m.py")) == (  # noqa: SLF001
      str(tmp_path / "src"), "a.b.m")
  # `agent/__init__.py` IS the package, not a submodule of it.
  assert cli._package_context(str(pkg / "__init__.py")) == (  # noqa: SLF001
      str(tmp_path / "src"), "a.b")
  lone = tmp_path / "lone.py"
  lone.write_text("", encoding="utf-8")
  assert cli._package_context(str(lone)) == ("", "")  # noqa: SLF001


def test_a_module_without_an_app_exits_with_an_actionable_message(tmp_path):
  ref = _write(tmp_path / "no_app.py", "x = 1\n")
  with pytest.raises(SystemExit) as exc:
    main(["validate", ref])
  assert "must define a top-level `app = flows.App(...)`" in str(exc.value.code)


def test_an_unloadable_path_exits_rather_than_tracebacks(tmp_path):
  ref = _write(tmp_path / "notamodule.txt", "hello")
  with pytest.raises(SystemExit) as exc:
    main(["validate", ref])
  assert str(exc.value.code) == f"cannot load module from {ref}"


# ===========================================================================
# cli: validate
# ===========================================================================

def test_validate_of_the_scaffold_is_clean(tmp_path, capsys):
  assert main(["validate", _scaffold(tmp_path)]) == 0
  assert capsys.readouterr().out.strip().endswith("validate: clean")


def test_validate_reports_errors_and_warnings_and_returns_one(tmp_path, capsys):
  ref = _write(tmp_path / "bad_app.py", BAD_APP)
  assert main(["validate", ref]) == 1
  out = capsys.readouterr().out
  assert "warn: [widget] Config has no 'tasks'" in out
  assert "error: [widget] Slot 'res' references unknown task 'nosuchtask'" in out
  assert "error(s) — invalid." in out


def test_validate_requires_a_module():
  with pytest.raises(SystemExit) as exc:
    main(["validate"])
  assert exc.value.code == 2


# ===========================================================================
# cli: lint
# ===========================================================================

def test_lint_of_the_scaffold_is_advisory_only(tmp_path, capsys):
  assert main(["lint", _scaffold(tmp_path)]) == 0
  out = capsys.readouterr().out
  assert "Summary:" in out


def test_lint_returns_one_when_a_rule_errors(tmp_path, capsys):
  ref = _write(tmp_path / "bad_app.py", BAD_APP)
  assert main(["lint", ref]) == 1
  out = capsys.readouterr().out
  assert "FLX001" in out
  assert "Slot 'res' references unknown task 'nosuchtask'" in out


def test_lint_strict_makes_a_warning_blocking(tmp_path, capsys):
  # GOOD_APP declares no tasks: a WARNING, so plain lint passes and --strict does not.
  ref = _write(tmp_path / "good_app.py", GOOD_APP)
  assert main(["lint", ref]) == 0
  assert main(["lint", ref, "--strict"]) == 1
  assert "Config has no 'tasks'" in capsys.readouterr().out


def test_lint_json_is_machine_readable(tmp_path, capsys):
  ref = _write(tmp_path / "bad_app.py", BAD_APP)
  assert main(["lint", ref, "--format", "json"]) == 1
  report = json.loads(capsys.readouterr().out)
  assert report["summary"]["by_severity"]["error"] >= 1
  assert "FLX001" in report["ran_rules"]


def test_lint_select_runs_only_the_named_rules(tmp_path, capsys):
  ref = _write(tmp_path / "bad_app.py", BAD_APP)
  assert main(["lint", ref, "--select", "FLC101", "--format", "json"]) == 0
  report = json.loads(capsys.readouterr().out)
  assert report["ran_rules"] == ["FLC101"]


def test_lint_ignore_drops_the_named_rule(tmp_path, capsys):
  ref = _write(tmp_path / "bad_app.py", BAD_APP)
  # FLX001 is the only error source here, so ignoring it flips the exit code.
  assert main(["lint", ref, "--ignore", "FLX001", "--format", "json"]) == 0
  report = json.loads(capsys.readouterr().out)
  assert "FLX001" not in report["ran_rules"]


def test_lint_select_and_ignore_ignore_blank_entries(tmp_path, capsys):
  ref = _write(tmp_path / "bad_app.py", BAD_APP)
  assert main(["lint", ref, "--select", " FLC101 , ,", "--ignore", "",
               "--format", "json"]) == 0
  assert json.loads(capsys.readouterr().out)["ran_rules"] == ["FLC101"]


def test_lint_show_suppressed_still_renders(tmp_path, capsys):
  ref = _write(tmp_path / "good_app.py", GOOD_APP)
  assert main(["lint", ref, "--show-suppressed"]) == 0
  assert "Summary:" in capsys.readouterr().out


def test_lint_list_rules_prints_the_catalog_and_exits_zero(capsys):
  assert main(["lint", "--list-rules"]) == 0
  out = capsys.readouterr().out
  assert "rules:" in out
  assert "FLX001" in out


def test_lint_list_rules_json(capsys):
  assert main(["lint", "--list-rules", "--format", "json"]) == 0
  rules = json.loads(capsys.readouterr().out)
  assert {"code", "category", "default_severity", "title", "docs"} <= set(rules[0])


def test_lint_explain_prints_one_rules_rationale(capsys):
  assert main(["lint", "--explain", "FLX001"]) == 0
  out = capsys.readouterr().out
  assert out.startswith("FLX001  (")
  # "more:", not "docs:" — the pointer is a command you can run offline rather than
  # the unresolvable https://flows.docs/... host it used to print.
  assert "more: flows lint --explain FLX001" in out


def test_lint_explain_of_an_unknown_rule_returns_two(capsys):
  assert main(["lint", "--explain", "FLZ999"]) == 2
  assert "no such rule 'FLZ999'" in capsys.readouterr().err


def test_lint_without_a_module_returns_two(capsys):
  assert main(["lint"]) == 2
  assert "a module is required" in capsys.readouterr().err


def test_lint_rejects_an_unknown_format():
  with pytest.raises(SystemExit) as exc:
    main(["lint", "app.py", "--format", "xml"])
  assert exc.value.code == 2


# ===========================================================================
# cli: emit
# ===========================================================================

def test_emit_writes_a_deployable_app_dir(tmp_path, capsys):
  out_dir = tmp_path / "app"
  assert main(["emit", _scaffold(tmp_path), "--out", str(out_dir)]) == 0
  assert (out_dir / "app.json").is_file()
  assert (out_dir / "tools").is_dir()
  assert (out_dir / "agents").is_dir()
  line = capsys.readouterr().out.strip().splitlines()[-1]
  assert line.startswith(f"emit: ok -> {out_dir}")
  assert f"(framework v{flows.version()})" in line


def test_emit_overwrites_an_existing_dir_by_default(tmp_path):
  out_dir = tmp_path / "app"
  out_dir.mkdir()
  (out_dir / "stale.txt").write_text("from a previous build", encoding="utf-8")
  assert main(["emit", _scaffold(tmp_path), "--out", str(out_dir)]) == 0
  assert not (out_dir / "stale.txt").exists()


def test_emit_no_overwrite_refuses_a_non_empty_dir(tmp_path, capsys):
  out_dir = tmp_path / "app"
  out_dir.mkdir()
  (out_dir / "stale.txt").write_text("from a previous build", encoding="utf-8")
  assert main(["emit", _scaffold(tmp_path), "--out", str(out_dir),
               "--no-overwrite"]) == 1
  assert "emit: FAILED" in capsys.readouterr().err
  assert (out_dir / "stale.txt").exists()          # nothing was destroyed


def test_emit_of_an_invalid_app_returns_one_and_writes_nothing(tmp_path, capsys):
  ref = _write(tmp_path / "bad_app.py", BAD_APP)
  out_dir = tmp_path / "app"
  assert main(["emit", ref, "--out", str(out_dir)]) == 1
  err = capsys.readouterr().err
  # LOUD: the real reason on stderr, never a bare `emit: ok`.
  assert err.startswith("emit: FAILED — ")
  assert "flow validation failed" in err
  assert not out_dir.exists()


def test_emit_forwards_overwrite_and_keep_failed_to_the_builder(tmp_path, monkeypatch):
  from flows.authoring import build

  seen = {}

  def fake_emit(app, out, *, overwrite, keep_failed):
    seen.update(out=out, overwrite=overwrite, keep_failed=keep_failed)
    return types.SimpleNamespace(written_to=out, framework_version="x")

  monkeypatch.setattr(build, "emit", fake_emit)
  ref = _write(tmp_path / "good_app.py", GOOD_APP)
  assert main(["emit", ref, "--out", "/out", "--keep-failed"]) == 0
  assert seen == {"out": "/out", "overwrite": True, "keep_failed": True}


def test_emit_requires_out():
  with pytest.raises(SystemExit) as exc:
    main(["emit", "app.py"])
  assert exc.value.code == 2


def test_emit_requires_a_module():
  with pytest.raises(SystemExit) as exc:
    main(["emit", "--out", "/out"])
  assert exc.value.code == 2


# ===========================================================================
# cli: check
# ===========================================================================

def test_check_passes_on_a_freshly_emitted_app(tmp_path, capsys):
  out_dir = tmp_path / "app"
  assert main(["emit", _scaffold(tmp_path), "--out", str(out_dir)]) == 0
  capsys.readouterr()
  assert main(["check", "--app-dir", str(out_dir)]) == 0
  assert capsys.readouterr().out.strip().startswith("framework in sync")


def test_check_reports_an_empty_dir_and_returns_one(tmp_path, capsys):
  empty = tmp_path / "nothing_here"
  empty.mkdir()
  assert main(["check", "--app-dir", str(empty)]) == 1
  out = capsys.readouterr().out
  assert "unusable: app.json is missing" in out
  assert "tools/slot_filling_engine/python_function/python_code.py" in out


def test_check_reports_a_path_that_is_not_a_directory(tmp_path, capsys):
  assert main(["check", "--app-dir", str(tmp_path / "absent")]) == 1
  assert "is not a directory" in capsys.readouterr().out


def test_check_detects_an_edited_framework_file(tmp_path, capsys):
  out_dir = tmp_path / "app"
  assert main(["emit", _scaffold(tmp_path), "--out", str(out_dir)]) == 0
  edited = (out_dir / "tools" / "slot_filling_engine" / "python_function"
            / "python_code.py")
  edited.write_text(edited.read_text(encoding="utf-8") + "\n# a local hack\n",
                    encoding="utf-8")
  capsys.readouterr()
  assert main(["check", "--app-dir", str(out_dir)]) == 1
  assert "off the blessed manifest" in capsys.readouterr().out


def test_check_requires_app_dir():
  with pytest.raises(SystemExit) as exc:
    main(["check"])
  assert exc.value.code == 2


# ===========================================================================
# cli: deploy (the `cxas` shell-out is the seam)
# ===========================================================================

@pytest.fixture
def fake_push(monkeypatch):
  """Replace the one function that shells out to `cxas`; record how it was called."""
  from flows.deploy import push

  calls = []

  def fake(app_dir, to, **kwargs):
    calls.append({"app_dir": app_dir, "to": to, **kwargs})
    return "pushed 1 app"

  monkeypatch.setattr(push, "deploy", fake)
  return calls


@pytest.fixture
def no_deploy_extra(monkeypatch):
  """Make `from .deploy.push import deploy` fail the way a core-only install does."""
  monkeypatch.setitem(sys.modules, "flows.deploy.push",
                      types.ModuleType("flows.deploy.push"))


def test_deploy_forwards_the_defaults_and_prints_the_push_output(fake_push, capsys):
  assert main(["deploy", "--app-dir", "/built", "--to", "projects/p/apps/a"]) == 0
  assert fake_push == [{"app_dir": "/built", "to": "projects/p/apps/a",
                        "cxas": "cxas", "preserve_from_target": True,
                        "audio_bucket": None, "inactivity_timeout": "8s",
                        "barge_in_awareness": None, "verify": True}]
  assert capsys.readouterr().out.strip() == "pushed 1 app"


def test_deploy_flags_reach_the_push(fake_push):
  assert main(["deploy", "--app-dir", "/built", "--to", "app-a",
               "--cxas", "/opt/bin/cxas", "--no-preserve", "--no-verify",
               "--barge-in-awareness",
               "--audio-bucket", "gs://recordings",
               "--inactivity-timeout", "20s"]) == 0
  assert fake_push[0] == {"app_dir": "/built", "to": "app-a",
                          "cxas": "/opt/bin/cxas", "preserve_from_target": False,
                          "audio_bucket": "gs://recordings",
                          "inactivity_timeout": "20s",
                          "barge_in_awareness": True, "verify": False}


def test_barge_in_awareness_is_none_unless_declared(fake_push):
  """Left unset the target's own setting is untouched — None, not False."""
  assert main(["deploy", "--app-dir", "/built", "--to", "a"]) == 0
  assert fake_push[0]["barge_in_awareness"] is None


def test_deploy_reports_a_missing_cxas_on_stderr(monkeypatch, capsys):
  from flows.deploy import push

  def boom(*_a, **_kw):
    raise FileNotFoundError("No such file or directory: 'cxas'")

  monkeypatch.setattr(push, "deploy", boom)
  assert main(["deploy", "--app-dir", "/built", "--to", "a", "--cxas", "cxas"]) == 1
  err = capsys.readouterr().err
  assert err.startswith("deploy: ")
  assert "is the [deploy] extra installed and `cxas` on PATH?" in err


def test_deploy_reports_a_failed_push_on_stderr(monkeypatch, capsys):
  from flows.deploy import push

  def boom(*_a, **_kw):
    raise RuntimeError("command failed (1): cxas push")

  monkeypatch.setattr(push, "deploy", boom)
  assert main(["deploy", "--app-dir", "/built", "--to", "a"]) == 1
  assert capsys.readouterr().err.strip() == "deploy: command failed (1): cxas push"


def test_deploy_without_the_extra_returns_one(no_deploy_extra, capsys):
  assert main(["deploy", "--app-dir", "/built", "--to", "a"]) == 1
  assert "requires the [deploy] extra" in capsys.readouterr().err


def test_deploy_requires_app_dir_and_to():
  for argv in (["deploy", "--to", "a"], ["deploy", "--app-dir", "/built"]):
    with pytest.raises(SystemExit) as exc:
      main(argv)
    assert exc.value.code == 2


# ===========================================================================
# cli: cujs
# ===========================================================================

def test_cujs_lists_every_preset_with_its_aliases_aligned(cujs_file, capsys):
  assert main(["cujs", "--file", cujs_file]) == 0
  lines = capsys.readouterr().out.splitlines()
  assert len(lines) == 2
  assert lines[0].startswith("  widget_reboot (reboot)")     # file order, not sorted
  assert lines[0].endswith("  Widget fault, reboot offered.")
  assert lines[1].startswith("  plain ")
  assert lines[1].endswith("  Nothing special.")
  # The label column is padded to the widest label so descriptions line up.
  assert lines[0].index("Widget fault") == lines[1].index("Nothing special")


def test_cujs_json_dumps_every_preset(cujs_file, capsys):
  assert main(["cujs", "--json", "--file", cujs_file]) == 0
  assert json.loads(capsys.readouterr().out) == {
      "widget_reboot": REBOOT_VARS,
      "plain": {"mock_config_string": "outage=none"},
  }


def test_cujs_with_a_name_prints_that_cujs_variables(cujs_file, capsys):
  assert main(["cujs", "widget_reboot", "--file", cujs_file]) == 0
  out = capsys.readouterr().out
  assert "  accountNumber = 1234" in out
  assert "  mock_config_string = outage=none&gateway=reboot" in out


def test_cujs_resolves_an_alias(cujs_file, capsys):
  assert main(["cujs", "reboot", "--json", "--file", cujs_file]) == 0
  assert json.loads(capsys.readouterr().out) == REBOOT_VARS


def test_cujs_discovers_the_file_from_the_environment(cujs_file, monkeypatch, capsys):
  monkeypatch.setenv("FLOWS_CUJS", cujs_file)
  assert main(["cujs", "reboot", "--json"]) == 0
  assert json.loads(capsys.readouterr().out) == REBOOT_VARS


def test_cujs_with_no_presets_prints_nothing(tmp_path, capsys):
  path = _write(tmp_path / "cujs.yaml", yaml.safe_dump({"cujs": {}}))
  assert main(["cujs", "--file", path]) == 0
  assert capsys.readouterr().out == ""


def test_cujs_unknown_name_exits_with_the_available_list(cujs_file):
  with pytest.raises(SystemExit) as exc:
    main(["cujs", "nope", "--file", cujs_file])
  message = str(exc.value.code)
  assert message.startswith("cujs: no CUJ 'nope'")
  assert "Available: plain, reboot, widget_reboot" in message


def test_cujs_missing_file_exits_with_a_message(tmp_path):
  with pytest.raises(SystemExit) as exc:
    main(["cujs", "--file", str(tmp_path / "absent.yaml")])
  assert str(exc.value.code).startswith("cujs: ")


def test_cujs_malformed_file_exits_with_a_message(tmp_path):
  path = _write(tmp_path / "cujs.yaml", yaml.safe_dump({"cuj": {}}))
  with pytest.raises(SystemExit) as exc:
    main(["cujs", "--file", path])
  assert "unknown top-level key(s) cuj" in str(exc.value.code)


# ===========================================================================
# cli: chat (the live session is the seam)
# ===========================================================================

@pytest.fixture
def fake_chat(monkeypatch):
  """Replace the driver that opens a live session; record how it was called."""
  from flows import drive

  calls = []

  def fake(cuj, app, **kwargs):
    calls.append({"cuj": cuj, "app": app, **kwargs})
    return 0

  monkeypatch.setattr(drive, "chat", fake)
  return calls


def test_chat_hands_the_driver_the_resolved_cuj_and_the_defaults(cujs_file, fake_chat):
  assert main(["chat", "--cuj", "reboot", "--app", "abc-123",
               "--file", cujs_file]) == 0
  call = fake_chat[0]
  assert call["cuj"].name == "widget_reboot"        # the alias was resolved
  assert call["cuj"].variables == REBOOT_VARS
  assert call["app"] == "abc-123"
  assert call["say"] is None                        # no --say means a REPL
  assert call["location"] == "us"
  assert call["use_tool_fakes"] is True             # fakes are on unless refused


def test_chat_say_is_repeatable_and_flags_reach_the_driver(cujs_file, fake_chat):
  assert main(["chat", "--cuj", "plain", "--app", "abc-123", "--file", cujs_file,
               "--say", "hello", "--say", "reboot it", "--no-fakes",
               "--project", "a-dev-project", "--location", "eu"]) == 0
  call = fake_chat[0]
  assert call["say"] == ["hello", "reboot it"]
  assert call["use_tool_fakes"] is False
  assert (call["project"], call["location"]) == ("a-dev-project", "eu")


def test_chat_an_empty_say_list_is_normalized_to_a_repl(cujs_file, monkeypatch):
  """`--say ""` collapses to a falsy list; the driver must see None, not []."""
  from flows import drive

  seen = {}
  monkeypatch.setattr(drive, "chat",
                      lambda cuj, app, **kw: seen.update(kw) or 0)
  assert main(["chat", "--cuj", "plain", "--app", "a", "--file", cujs_file]) == 0
  assert seen["say"] is None


def test_chat_returns_the_drivers_exit_code(cujs_file, monkeypatch):
  from flows import drive

  monkeypatch.setattr(drive, "chat", lambda *_a, **_kw: 3)
  assert main(["chat", "--cuj", "plain", "--app", "a", "--file", cujs_file]) == 3


def test_chat_reports_a_missing_session_backend_on_stderr(
    cujs_file, monkeypatch, capsys):
  from flows import drive

  def boom(*_a, **_kw):
    raise ImportError("flows.drive needs a ChatSession backend")

  monkeypatch.setattr(drive, "chat", boom)
  # 3, not 1: a missing runtime dependency is an ENVIRONMENT failure, and a caller
  # scripting the CLI has to tell "install the extra" apart from "the turn failed".
  assert main(["chat", "--cuj", "plain", "--app", "a", "--file", cujs_file]) == 3
  assert capsys.readouterr().err.strip() == (
      "chat: flows.drive needs a ChatSession backend")


def test_chat_unknown_cuj_exits_before_opening_a_session(cujs_file, fake_chat):
  with pytest.raises(SystemExit) as exc:
    main(["chat", "--cuj", "nope", "--app", "a", "--file", cujs_file])
  assert str(exc.value.code).startswith("chat: no CUJ 'nope'")
  assert fake_chat == []


def test_chat_requires_an_app_but_no_longer_a_cuj(fake_chat):
  """A CUJ seeds variables; it is not a precondition for talking to an app. Requiring
  one meant an app with no cujs.yaml could not be driven from the CLI at all."""
  with pytest.raises(SystemExit) as exc:
    main(["chat", "--cuj", "plain"])
  assert exc.value.code == 2

  assert main(["chat", "--app", "a", "--say", "hello"]) == 0
  assert fake_chat and fake_chat[0]["cuj"] == {}


# ===========================================================================
# cli: cuj-apply
# ===========================================================================

def test_cuj_apply_dry_run_writes_nothing(cujs_file, app_json_dir, capsys):
  before = (app_json_dir / "app.json").read_text(encoding="utf-8")
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file, "--dry-run"]) == 0
  out = capsys.readouterr().out
  assert out.startswith(
      f"would set in {app_json_dir / 'app.json'} (cuj: widget_reboot):")
  assert "  accountNumber.schema.default = 1234" in out
  assert (app_json_dir / "app.json").read_text(encoding="utf-8") == before


def test_cuj_apply_sets_each_variables_schema_default(cujs_file, app_json_dir, capsys):
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file]) == 0
  decls = {d["name"]: d for d in json.loads(
      (app_json_dir / "app.json").read_text(encoding="utf-8"))[
          "variableDeclarations"]}
  assert decls["accountNumber"]["schema"]["default"] == "1234"
  assert decls["account_id"]["schema"]["default"] == "1234"   # a stale default goes
  assert decls["mock_config_string"]["schema"]["default"] == "outage=none&gateway=reboot"
  out = capsys.readouterr().out
  assert out.startswith("cuj-apply: set defaults for ")
  assert "from cuj 'widget_reboot'" in out


def test_cuj_apply_warns_when_nothing_reads_mock_config_string(
    cujs_file, app_json_dir, capsys):
  # A CES console session never fires toolFakeConfig, so this build ignores the CUJ.
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file]) == 0
  assert "WARNING" in capsys.readouterr().err


def test_cuj_apply_is_quiet_when_a_tool_reads_mock_config_string(
    cujs_file, app_json_dir, capsys):
  tool = app_json_dir / "tools" / "lookup" / "python_function"
  tool.mkdir(parents=True)
  (tool / "python_code.py").write_text(
      "def run(mock_config_string=''):\n  return {}\n", encoding="utf-8")
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file]) == 0
  assert capsys.readouterr().err == ""


def test_cuj_apply_skips_unreadable_sources_rather_than_crashing(
    cujs_file, app_json_dir, capsys):
  """A broken symlink named `*.py` is not an answer either — and must not raise."""
  (app_json_dir / "dangling.py").symlink_to(app_json_dir / "nowhere.py")
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file]) == 0
  assert "WARNING" in capsys.readouterr().err


def test_cuj_apply_ignores_non_python_files_when_hunting_for_the_fake_reader(
    cujs_file, app_json_dir, capsys):
  (app_json_dir / "notes.txt").write_text("mock_config_string", encoding="utf-8")
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file]) == 0
  assert "WARNING" in capsys.readouterr().err


def test_cuj_apply_finds_a_nested_app_json(cujs_file, tmp_path, capsys):
  # `cxas pull` lands the app one level down; apply follows it there — and the
  # dry run names the file it would actually write, not `<app-dir>/app.json`.
  nested = tmp_path / "built" / "Widget_Agent"
  nested.mkdir(parents=True)
  (nested / "app.json").write_text(json.dumps({
      "variableDeclarations": [{"name": n} for n in REBOOT_VARS]}),
      encoding="utf-8")
  app_dir = str(tmp_path / "built")

  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", app_dir,
               "--file", cujs_file, "--dry-run"]) == 0
  out = capsys.readouterr().out
  assert f"would set in {nested / 'app.json'} " in out
  assert f"would set in {app_dir}/app.json" not in out
  assert not (tmp_path / "built" / "app.json").exists()

  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", app_dir,
               "--file", cujs_file]) == 0
  decls = json.loads((nested / "app.json").read_text(
      encoding="utf-8"))["variableDeclarations"]
  assert {d["name"]: d["schema"]["default"] for d in decls} == REBOOT_VARS


def test_cuj_apply_refuses_an_undeclared_variable(cujs_file, tmp_path, capsys):
  d = tmp_path / "built"
  d.mkdir()
  (d / "app.json").write_text(
      json.dumps({"variableDeclarations": [{"name": "account_id"}]}),
      encoding="utf-8")
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(d),
               "--file", cujs_file]) == 1
  err = capsys.readouterr().err
  assert err.startswith("cuj-apply: ")
  assert "declares no variable(s) accountNumber, mock_config_string" in err


def test_cuj_apply_reports_a_missing_app_json(cujs_file, tmp_path, capsys):
  d = tmp_path / "empty"
  d.mkdir()
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(d),
               "--file", cujs_file]) == 1
  assert "no app.json under" in capsys.readouterr().err


def test_cuj_apply_dry_run_reports_a_missing_app_json_too(cujs_file, tmp_path, capsys):
  """A dry run can only name a real target, so it fails the same way a real one does."""
  d = tmp_path / "empty"
  d.mkdir()
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(d),
               "--file", cujs_file, "--dry-run"]) == 1
  captured = capsys.readouterr()
  assert "no app.json under" in captured.err
  assert "would set in" not in captured.out


def test_cuj_apply_to_pushes_the_draft_and_says_so(
    cujs_file, app_json_dir, fake_push, capsys):
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file, "--to", "projects/p/apps/a",
               "--cxas", "/opt/bin/cxas", "--no-preserve"]) == 0
  assert fake_push == [{"app_dir": str(app_json_dir), "to": "projects/p/apps/a",
                        "cxas": "/opt/bin/cxas", "preserve_from_target": False}]
  out = capsys.readouterr().out
  assert "pushed 1 app" in out
  # The push updates the DRAFT only — a pinned phone/GTP version keeps the old build.
  assert "cuj-apply: pushed the DRAFT of projects/p/apps/a." in out
  assert "will NOT until you promote this version" in out


def test_cuj_apply_without_to_does_not_push(cujs_file, app_json_dir, fake_push):
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file]) == 0
  assert fake_push == []


def test_cuj_apply_reports_a_failed_push(cujs_file, app_json_dir, monkeypatch, capsys):
  from flows.deploy import push

  def boom(*_a, **_kw):
    raise RuntimeError("command failed (1): cxas push")

  monkeypatch.setattr(push, "deploy", boom)
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file, "--to", "a"]) == 1
  assert "cuj-apply: command failed (1): cxas push" in capsys.readouterr().err


def test_cuj_apply_reports_a_missing_cxas_on_the_push(
    cujs_file, app_json_dir, monkeypatch, capsys):
  from flows.deploy import push

  def boom(*_a, **_kw):
    raise FileNotFoundError("No such file or directory: 'cxas'")

  monkeypatch.setattr(push, "deploy", boom)
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file, "--to", "a"]) == 1
  assert "cuj-apply: " in capsys.readouterr().err


def test_cuj_apply_to_without_the_extra_returns_one(
    cujs_file, app_json_dir, no_deploy_extra, capsys):
  assert main(["cuj-apply", "--cuj", "reboot", "--app-dir", str(app_json_dir),
               "--file", cujs_file, "--to", "a"]) == 1
  assert "--to requires the [deploy] extra" in capsys.readouterr().err


def test_cuj_apply_unknown_cuj_exits_before_touching_the_app(cujs_file, app_json_dir):
  before = (app_json_dir / "app.json").read_text(encoding="utf-8")
  with pytest.raises(SystemExit) as exc:
    main(["cuj-apply", "--cuj", "nope", "--app-dir", str(app_json_dir),
          "--file", cujs_file])
  assert str(exc.value.code).startswith("cuj-apply: no CUJ 'nope'")
  assert (app_json_dir / "app.json").read_text(encoding="utf-8") == before


def test_cuj_apply_requires_cuj_and_app_dir():
  for argv in (["cuj-apply", "--app-dir", "/built"], ["cuj-apply", "--cuj", "plain"]):
    with pytest.raises(SystemExit) as exc:
      main(argv)
    assert exc.value.code == 2


# ===========================================================================
# config_io: three import paths, one normal form, two exports
# ===========================================================================

def _framework_root(tmp_path, tool_name: str, code: str) -> str:
  """Write `code` as a framework tool's `python_code.py`; return the root path."""
  pkg = tmp_path / tool_name / "python_function"
  pkg.mkdir(parents=True)
  (pkg / "python_code.py").write_text(code, encoding="utf-8")
  return str(tmp_path)


RICH_RAW = {
    "slots": [
        {"name": "size", "source": "user", "ask": "What size?",
         "dtmf_map": {"1": "small"},
         "validation": {"max_retries": 2, "errors": {"bad": "Nope."}},
         "response": [{"type": "text", "text": "Got it."},
                      {"type": "chips", "options": [{"text": "Small"}]}],
         "readback_fmt": {"type": "plural", "one": "{n} item", "other": "{n} items"},
         "condition": {"all": [{"slot": "member", "filled": True},
                               {"not": {"slot": "blocked", "eq": True}}]},
         "vendor_extra": {"deep": [1, 2]}},
    ],
    "tasks": [
        {"name": "place", "tool": "place_order", "inputs": ["size"],
         "outputs": {"id": "order_id"}, "terminal": True,
         "condition": {"slot": "size", "gte": 5, "lt": 10}},
    ],
    "bootstrap": {"slot": "active_flow", "reset_on_complete": True},
    "route_cues": {"orders": ["order", "buy"]},
    "unknown_top_level_key": 7,
}


def test_the_import_error_synthesizes_a_diagnostic_when_none_is_given():
  err = config_io.ConfigImportError("boom")
  assert [d.severity for d in err.diagnostics] == ["error"]
  assert err.diagnostics[0].message == "boom"
  assert err.diagnostics[0].raw == "boom"
  assert str(err) == "boom"


def test_the_import_error_keeps_diagnostics_it_was_handed():
  diag = models.Diagnostic(severity="warning", message="m", raw="r",
                           anchor=models.NodeAnchor(kind="slot", ref="size"))
  err = config_io.ConfigImportError("boom", [diag])
  assert err.diagnostics == [diag]


# --- import_by_id --------------------------------------------------------------

def test_import_by_id_calls_the_function_named_after_the_tool(tmp_path):
  root = _framework_root(tmp_path, "acme_dag",
                         "def acme_dag():\n  return {'slots': [{'name': 'size'}]}\n")
  assert config_io.import_by_id("acme_dag", root) == {"slots": [{"name": "size"}]}


def test_import_by_id_falls_back_to_a_slots_bearing_callable(tmp_path):
  # The named function exists but yields a non-dict, so the scan takes over.
  root = _framework_root(tmp_path, "acme_dag",
                         "def acme_dag():\n  return None\n\n\n"
                         "def build_it():\n  return {'slots': [{'name': 'b'}]}\n")
  assert config_io.import_by_id("acme_dag", root) == {"slots": [{"name": "b"}]}


def test_import_by_id_ignores_private_and_slotless_callables(tmp_path):
  root = _framework_root(
      tmp_path, "acme_dag",
      "def acme_dag():\n  return None\n\n\n"
      "def _private():\n  return {'slots': [{'name': 'private'}]}\n\n\n"
      "def no_slots():\n  return {'tasks': []}\n\n\n"
      "def real():\n  return {'slots': [{'name': 'real'}]}\n")
  assert config_io.import_by_id("acme_dag", root) == {"slots": [{"name": "real"}]}


def test_import_by_id_fallback_executes_module_level_callables(tmp_path):
  """SHARP EDGE, pinned deliberately: the fallback scan CALLS every public
  module-level callable, so importing by id EXECUTES arbitrary agent code.
  A caller ingesting untrusted source must use `import_from_source` (AST only)."""
  root = _framework_root(
      tmp_path, "acme_dag",
      "SIDE_EFFECTS = []\n\n\n"
      "def acme_dag():\n  return None\n\n\n"
      "def a_side_effect():\n  SIDE_EFFECTS.append('ran')\n  return 1\n\n\n"
      "def z_config():\n"
      "  return {'slots': [{'name': 'x'}], 'ran': list(SIDE_EFFECTS)}\n")
  assert config_io.import_by_id("acme_dag", root)["ran"] == ["ran"]


def test_import_from_source_never_executes_the_module(tmp_path):
  """The companion guarantee: the AST path parses, it does not run."""
  marker = tmp_path / "written_by_the_import.txt"
  src = (f"def side_effect():\n  open({str(marker)!r}, 'w').write('x')\n\n\n"
         "def acme_dag():\n  return {'slots': [{'name': 'x'}]}\n")
  assert config_io.import_from_source(src) == {"slots": [{"name": "x"}]}
  assert not marker.exists()


def test_import_by_id_skips_callables_that_raise_or_need_arguments(tmp_path):
  root = _framework_root(
      tmp_path, "acme_dag",
      "def boom():\n  raise RuntimeError('nope')\n\n\n"
      "def needs_arg(a):\n  return {'slots': []}\n\n\n"
      "def good():\n  return {'slots': [{'name': 'ok'}]}\n")
  assert config_io.import_by_id("acme_dag", root) == {"slots": [{"name": "ok"}]}


def test_import_by_id_raises_when_nothing_yields_a_config(tmp_path):
  root = _framework_root(tmp_path, "acme_dag",
                         "VALUE = 3\n\n\ndef boom():\n  raise RuntimeError('x')\n")
  with pytest.raises(config_io.ConfigImportError) as exc:
    config_io.import_by_id("acme_dag", root)
  assert "acme_dag" in str(exc.value)
  assert exc.value.diagnostics[0].severity == "error"


def test_import_by_id_propagates_a_missing_tool(tmp_path):
  root = _framework_root(tmp_path, "other_dag", "def other_dag():\n  return {}\n")
  with pytest.raises(FileNotFoundError):
    config_io.import_by_id("acme_dag", root)


# --- import_from_source --------------------------------------------------------

def test_a_bare_dict_literal_parses():
  assert config_io.import_from_source("{'slots': [{'name': 'size'}]}") == {
      "slots": [{"name": "size"}]}


def test_leading_comments_and_whitespace_are_tolerated():
  assert config_io.import_from_source("\n  # a comment\n{'slots': []}\n") == {
      "slots": []}


def test_a_module_body_yields_its_returned_dict():
  src = "def acme_dag():\n  return {'slots': [{'name': 'size'}]}\n"
  assert config_io.import_from_source(src) == {"slots": [{"name": "size"}]}


def test_the_two_function_form_is_followed():
  src = ("def acme_dag():\n  return _acme()\n\n\n"
         "def _acme():\n  return {'slots': [{'name': 'size'}]}\n")
  assert config_io.import_from_source(src) == {"slots": [{"name": "size"}]}


def test_the_last_dict_returning_function_wins():
  """SHARP EDGE: the LAST `return <dict literal>` is taken, so a trailing helper
  beats the real `*_dag` function."""
  src = ("def acme_dag():\n  return {'slots': [{'name': 'the_real_one'}]}\n\n\n"
         "def _a_trailing_helper():\n  return {'slots': [{'name': 'the_helper'}]}\n")
  assert config_io.import_from_source(src) == {"slots": [{"name": "the_helper"}]}


def test_lambdas_in_dynamic_fields_are_lifted_to_source_strings():
  src = ("def acme_dag():\n"
         "  return {\n"
         "    'slots': [{'name': 'size',\n"
         "               'condition': lambda s: s.get('x') == 1,\n"
         "               'readback_fmt': lambda v: f'{v}!'}],\n"
         "    'tasks': [{'name': 't', 'tool': 'do_it',\n"
         "               'success_check': lambda r: r.get('ok')}],\n"
         "  }\n")
  raw = config_io.import_from_source(src)
  assert raw["slots"][0]["condition"] == "lambda s: s.get('x') == 1"
  assert raw["slots"][0]["readback_fmt"] == "lambda v: f'{v}!'"
  assert raw["tasks"][0]["success_check"] == "lambda r: r.get('ok')"


def test_a_lifted_lambda_is_the_last_entry_of_its_dict():
  raw = config_io.import_from_source(
      "{'slots': [{'name': 'a', 'condition': lambda s: s.get('x')}]}")
  assert raw["slots"][0]["condition"] == "lambda s: s.get('x')"


def test_a_lifted_lambda_keeps_commas_inside_brackets_and_strings():
  raw = config_io.import_from_source(
      "{'slots': [{'condition': lambda s: max(s.get('a'), s.get('b')) > 1,"
      " 'name': 'a'}]}")
  assert raw["slots"][0]["condition"] == "lambda s: max(s.get('a'), s.get('b')) > 1"
  assert raw["slots"][0]["name"] == "a"

  raw2 = config_io.import_from_source(
      "{'slots': [{'condition': lambda s: s.get('a') == 'x, y}', 'name': 'a'}]}")
  assert raw2["slots"][0]["condition"] == "lambda s: s.get('a') == 'x, y}'"
  assert raw2["slots"][0]["name"] == "a"


def test_a_lifted_lambda_survives_an_escaped_quote():
  raw = config_io.import_from_source(
      r"{'slots': [{'condition': lambda s: s.get('a') == 'it\'s', 'name': 'a'}]}")
  assert raw["slots"][0]["condition"] == r"lambda s: s.get('a') == 'it\'s'"


def test_double_quoted_dynamic_keys_are_lifted_too():
  raw = config_io.import_from_source(
      '{"slots": [{"condition": lambda s: True, "name": "a"}]}')
  assert raw["slots"][0]["condition"] == "lambda s: True"


def test_a_lambda_in_an_unlisted_field_is_not_lifted():
  # Only condition/readback_fmt/success_check are lifted; anything else stays a
  # live lambda and the literal parse rejects it.
  with pytest.raises(config_io.ConfigImportError):
    config_io.import_from_source("{'slots': [{'name': 'a', 'setter': lambda s: 1}]}")


def test_source_with_no_lambda_is_passed_through_unchanged():
  assert config_io._lift_lambdas_to_strings("{'a': 1}") == "{'a': 1}"  # noqa: SLF001


def test_a_lambda_running_off_the_end_of_the_source_is_a_parse_error():
  # Truncated paste: the lambda body has no terminating comma or brace, so the
  # capture consumes the rest of the text and the literal parse then fails.
  with pytest.raises(config_io.ConfigImportError) as exc:
    config_io.import_from_source("{'condition': lambda s: True")
  assert "Could not parse config source:" in exc.value.diagnostics[0].message


def test_empty_source_raises():
  with pytest.raises(config_io.ConfigImportError) as exc:
    config_io.import_from_source("   \n  ")
  assert "Empty source." in str(exc.value)


def test_a_non_dict_literal_raises():
  with pytest.raises(config_io.ConfigImportError) as exc:
    config_io.import_from_source("[1, 2, 3]")
  assert "did not evaluate to a config dict" in str(exc.value)


def test_a_syntax_error_is_anchored_and_carries_its_source_line():
  with pytest.raises(config_io.ConfigImportError) as exc:
    config_io.import_from_source("{'slots': [")
  diag = exc.value.diagnostics[0]
  assert diag.severity == "error"
  assert diag.message.startswith("Could not parse config source:")
  # A parse failure has no node to point at, so it anchors to the config...
  assert (diag.anchor.kind, diag.anchor.ref) == ("field", "config")
  # ...and the SyntaxError's `lineno` rides along as an extra field.
  assert (diag.model_extra or {}).get("line") == 1


def test_a_non_literal_value_gets_a_field_anchor_with_no_line():
  with pytest.raises(config_io.ConfigImportError) as exc:
    config_io.import_from_source("{'slots': some_name}")
  diag = exc.value.diagnostics[0]
  assert (diag.anchor.kind, diag.anchor.ref) == ("field", "config")
  assert "line" not in (diag.model_extra or {})


def test_a_module_level_assignment_is_not_a_supported_form():
  with pytest.raises(config_io.ConfigImportError):
    config_io.import_from_source("CONFIG = {'slots': []}")


def test_a_function_returning_a_non_dict_is_not_extracted():
  with pytest.raises(config_io.ConfigImportError):
    config_io.import_from_source("def acme_dag():\n  return 5\n")


def test_extract_returned_dict_gives_up_on_unparseable_text():
  assert config_io._extract_returned_dict("def broken(:\n") is None  # noqa: SLF001
  assert config_io._extract_returned_dict("x = 1\n") is None  # noqa: SLF001


# --- normalize / config_to_dict ------------------------------------------------

def test_an_empty_dict_normalizes_to_empty_slots_and_tasks():
  assert config_io.config_to_dict(config_io.normalize({})) == {
      "slots": [], "tasks": []}


def test_condition_bounds_are_coerced_to_float():
  cfg = config_io.normalize({"tasks": [
      {"name": "t", "tool": "x", "condition": {"slot": "n", "gte": 5, "lt": 10}}]})
  cond = config_io.config_to_dict(cfg)["tasks"][0]["condition"]
  assert cond["gte"] == 5.0 and isinstance(cond["gte"], float)
  assert cond["lt"] == 10.0 and isinstance(cond["lt"], float)


def test_the_float_coercion_is_idempotent():
  once = config_io.config_to_dict(config_io.normalize(RICH_RAW))
  twice = config_io.config_to_dict(config_io.normalize(once))
  assert once == twice


def test_unknown_keys_survive_normalization():
  out = config_io.config_to_dict(config_io.normalize(RICH_RAW))
  assert out["unknown_top_level_key"] == 7
  assert out["slots"][0]["vendor_extra"] == {"deep": [1, 2]}


def test_config_to_dict_drops_none_valued_keys():
  out = config_io.config_to_dict(config_io.normalize({"slots": [{"name": "size"}]}))
  assert out["slots"] == [{"name": "size"}]   # no `ask: None`, `hint: None`, ...
  assert "gate_slot" not in out


def test_config_to_dict_emits_the_reserved_word_aliases():
  cfg = config_io.normalize({"slots": [
      {"name": "a", "condition": {"slot": "s", "in": ["x", "y"]}},
      {"name": "b", "condition": {"not": {"slot": "s", "eq": 1}}},
  ]})
  out = config_io.config_to_dict(cfg)
  assert out["slots"][0]["condition"] == {"slot": "s", "in": ["x", "y"]}
  assert out["slots"][1]["condition"] == {"not": {"slot": "s", "eq": 1}}


def test_normalize_raises_on_a_schema_violation():
  with pytest.raises(config_io.ConfigImportError) as exc:
    config_io.normalize({"slots": "not-a-list"})
  assert "does not match the schema" in str(exc.value)
  assert exc.value.diagnostics[0].severity == "error"


# --- import_config (path precedence) -------------------------------------------

def test_import_config_normalizes_a_raw_dict():
  cfg = config_io.import_config(raw_dict={"slots": [{"name": "size"}]})
  assert isinstance(cfg, models.Config)
  assert cfg.slots[0].name == "size"


def test_import_config_normalizes_source():
  assert config_io.import_config(
      source="{'slots': [{'name': 'size'}]}").slots[0].name == "size"


def test_import_config_loads_by_id(tmp_path):
  root = _framework_root(tmp_path, "acme_dag",
                         "def acme_dag():\n  return {'slots': [{'name': 'size'}]}\n")
  cfg = config_io.import_config(config_id="acme_dag", framework_root=root)
  assert cfg.slots[0].name == "size"


def test_a_dict_beats_source_and_config_id():
  cfg = config_io.import_config(raw_dict={"slots": [{"name": "from_dict"}]},
                                source="{'slots': [{'name': 'from_source'}]}",
                                config_id="does_not_exist")
  assert cfg.slots[0].name == "from_dict"


def test_source_beats_config_id():
  cfg = config_io.import_config(source="{'slots': [{'name': 'from_source'}]}",
                                config_id="does_not_exist")
  assert cfg.slots[0].name == "from_source"


def test_an_empty_dict_still_takes_the_dict_path():
  # `{}` is falsy but not None — it must not fall through to the id path.
  cfg = config_io.import_config(raw_dict={}, config_id="does_not_exist")
  assert cfg.slots == [] and cfg.tasks == []


def test_import_config_with_no_input_raises():
  with pytest.raises(config_io.ConfigImportError) as exc:
    config_io.import_config()
  assert "config_id, dict, source" in str(exc.value)


def test_every_import_path_lands_on_the_same_config(tmp_path):
  raw = {"slots": [{"name": "size", "ask": "What size?"}],
         "tasks": [{"name": "t", "tool": "do_it"}]}
  root = _framework_root(tmp_path, "acme_dag",
                         f"def acme_dag():\n  return {raw!r}\n")
  by_dict = config_io.import_config(raw_dict=raw)
  by_source = config_io.import_config(source=repr(raw))
  by_id = config_io.import_config(config_id="acme_dag", framework_root=root)
  assert by_dict == by_source == by_id


# --- export --------------------------------------------------------------------

def test_export_json_is_indented_and_unicode_safe():
  cfg = config_io.normalize({"slots": [{"name": "n", "ask": "Café ☕?"}]})
  text = config_io.export_json(cfg)
  assert "\n  \"slots\"" in text                       # indent=2
  assert "Café ☕?" in text                             # ensure_ascii=False
  assert json.loads(text)["slots"][0]["ask"] == "Café ☕?"


def test_export_python_renders_a_named_zero_arg_function():
  cfg = config_io.normalize({"slots": [{"name": "size"}]})
  text = config_io.export_python(cfg, "acme_dag")
  assert "def acme_dag() -> dict[str, Any]:" in text
  assert '"""Return the DAG config for acme_dag."""' in text
  assert text.endswith("\n")


def test_the_rendered_python_executes_back_to_the_same_dict():
  cfg = config_io.normalize(RICH_RAW)
  namespace: dict = {}
  exec(compile(config_io.export_python(cfg, "acme_dag"), "<export>", "exec"),  # noqa: S102
       namespace)
  assert namespace["acme_dag"]() == config_io.config_to_dict(cfg)


def test_the_json_export_round_trips_exactly():
  cfg = config_io.normalize(RICH_RAW)
  assert config_io.normalize(json.loads(config_io.export_json(cfg))) == cfg


def test_the_python_export_round_trips_exactly():
  cfg = config_io.normalize(RICH_RAW)
  assert config_io.import_config(source=config_io.export_python(cfg, "acme_dag")) == cfg


def test_lambda_source_strings_stay_strings_across_a_python_round_trip():
  cfg = config_io.normalize(config_io.import_from_source(
      "{'slots': [{'name': 'a', 'condition': lambda s: s.get('x') == 1}]}"))
  reimported = config_io.import_config(
      source=config_io.export_python(cfg, "acme_dag"))
  assert reimported == cfg
  assert reimported.slots[0].condition == "lambda s: s.get('x') == 1"


def test_export_config_json_names_the_file_after_the_id():
  result = config_io.export_config(config_io.normalize({}), "json", "acme_dag")
  assert isinstance(result, models.ExportResult)
  assert result.filename == "acme_dag.json"
  assert json.loads(result.content) == {"slots": [], "tasks": []}
  assert result.warnings == []


def test_export_config_python_always_writes_python_code_py():
  result = config_io.export_config(config_io.normalize({}), "python", "acme_dag")
  assert result.filename == "python_code.py"
  assert "def acme_dag()" in result.content
  assert result.warnings == []


def test_export_config_defaults_the_id_to_config():
  assert config_io.export_config(
      config_io.normalize({}), "json").filename == "config.json"
  assert "def config()" in config_io.export_config(
      config_io.normalize({}), "python").content


def test_export_config_rejects_an_unknown_format():
  with pytest.raises(config_io.ConfigImportError) as exc:
    config_io.export_config(config_io.normalize({}), "yaml")
  assert "Unknown export format" in str(exc.value)


def test_the_corpus_shaped_config_reports_no_lossy_warnings():
  # The model carries no callable type, so nothing can fail source capture.
  cfg = config_io.normalize(RICH_RAW)
  for fmt in ("json", "python"):
    assert config_io.export_config(cfg, fmt, "acme_dag").warnings == []


# ===========================================================================
# tool_scan: a FILESYSTEM scanner, not a signature inspector
# ===========================================================================

def _tools_root(tmp_path, tools: dict[str, str]):
  """A framework root holding `{tool_name: python source}`. Returns its path."""
  root = tmp_path / "tools"
  for name, src in tools.items():
    code = root / name / "python_function" / "python_code.py"
    code.parent.mkdir(parents=True)
    code.write_text(src, encoding="utf-8")
  root.mkdir(exist_ok=True)
  return root


# --- what counts as a tool dir --------------------------------------------------

def test_only_dirs_holding_python_code_are_tools(tmp_path):
  root = _tools_root(tmp_path, {"set_order_id": "def set_order_id(): ...\n"})
  (root / "not_a_tool").mkdir()                              # dir, no python_code.py
  (root / "readme.md").write_text("x", encoding="utf-8")     # a plain file
  assert tool_scan.discover_tool_names(str(root)) == ["set_order_id"]


def test_python_function_must_be_a_dir_holding_the_file(tmp_path):
  root = _tools_root(tmp_path, {"good": "x = 1\n"})
  (root / "half_baked").mkdir()
  (root / "half_baked" / "python_function").write_text("", encoding="utf-8")
  assert tool_scan.discover_tool_names(str(root)) == ["good"]


def test_discovery_does_not_recurse_into_subdirs(tmp_path):
  root = _tools_root(tmp_path, {"top": "x = 1\n"})
  buried = root / "group" / "buried" / "python_function" / "python_code.py"
  buried.parent.mkdir(parents=True)
  buried.write_text("x = 1\n", encoding="utf-8")
  assert tool_scan.discover_tool_names(str(root)) == ["top"]


def test_a_missing_root_yields_no_tools(tmp_path):
  absent = str(tmp_path / "nope")
  assert tool_scan.discover_tool_names(absent) == []
  assert tool_scan.discover_available_tools(absent) == []
  assert tool_scan.discover_dag_configs(absent) == []


def test_a_root_that_is_a_file_yields_no_tools(tmp_path):
  f = tmp_path / "root.txt"
  f.write_text("not a dir", encoding="utf-8")
  assert tool_scan.discover_tool_names(str(f)) == []


def test_tool_names_are_sorted_by_path_byte_order(tmp_path):
  root = _tools_root(tmp_path, {"b_tool": "", "a_tool": "", "C_tool": ""})
  assert tool_scan.discover_tool_names(str(root)) == ["C_tool", "a_tool", "b_tool"]


# --- the available-tools surface ------------------------------------------------

def test_available_tools_excludes_infra_and_dag_configs(tmp_path):
  root = _tools_root(tmp_path, {
      "slot_filling_engine": "", "validate_dag_config": "", "slot_intake": "",
      "acme_dag": "", "set_order_id": "", "lookup_order": "",
  })
  assert tool_scan.discover_tool_names(str(root)) == [
      "acme_dag", "lookup_order", "set_order_id",
      "slot_filling_engine", "slot_intake", "validate_dag_config",
  ]
  assert tool_scan.discover_available_tools(str(root)) == [
      "lookup_order", "set_order_id"]


def test_infra_exclusion_is_an_exact_name_not_a_prefix(tmp_path):
  root = _tools_root(tmp_path, {"slot_intake_helper": "", "my_slot_intake": ""})
  assert tool_scan.discover_available_tools(str(root)) == [
      "my_slot_intake", "slot_intake_helper"]


def test_dag_exclusion_is_a_suffix_not_a_substring(tmp_path):
  root = _tools_root(tmp_path, {"dag_builder": "", "a_dag": "", "dagger": ""})
  assert tool_scan.discover_available_tools(str(root)) == ["dag_builder", "dagger"]


# --- dag configs + display names ------------------------------------------------

def test_dag_configs_are_id_display_pairs(tmp_path):
  root = _tools_root(tmp_path, {
      "widget_support_dag": "", "acme_dag": "", "set_order_id": ""})
  assert tool_scan.discover_dag_configs(str(root)) == [
      ("acme_dag", "Acme"), ("widget_support_dag", "Widget Support")]


def test_display_name_strips_one_dag_suffix_and_titles_the_rest():
  assert tool_scan._display_name("acme_dag") == "Acme"  # noqa: SLF001
  assert tool_scan._display_name("widget_support_dag") == "Widget Support"  # noqa: SLF001
  # Non-`_dag` ids are title-cased in place (the helper is total, not guarded).
  assert tool_scan._display_name("plain_tool") == "Plain Tool"  # noqa: SLF001
  # Only the trailing four chars are stripped, so a doubled suffix keeps one.
  assert tool_scan._display_name("acme_dag_dag") == "Acme Dag"  # noqa: SLF001


# --- reading source -------------------------------------------------------------

def test_read_tool_source_returns_the_file_text_verbatim(tmp_path):
  src = "def set_order_id(order_id: str) -> dict:\n    return {'order_id': order_id}\n"
  root = _tools_root(tmp_path, {"set_order_id": src})
  assert tool_scan.read_tool_source("set_order_id", str(root)) == src


def test_read_tool_source_is_none_for_an_unknown_tool(tmp_path):
  root = _tools_root(tmp_path, {"set_order_id": "x = 1\n"})
  assert tool_scan.read_tool_source("no_such_tool", str(root)) is None


def test_read_tool_source_is_none_when_the_dir_has_no_code_file(tmp_path):
  root = _tools_root(tmp_path, {"real": "x = 1\n"})
  (root / "empty_shell").mkdir()
  assert tool_scan.read_tool_source("empty_shell", str(root)) is None


def test_read_tool_source_reads_syntactically_broken_source(tmp_path):
  # The scanner is a reader, not a parser: malformed python comes back as text.
  bad = "def set_order_id(:\n  return\n"
  root = _tools_root(tmp_path, {"set_order_id": bad})
  assert tool_scan.read_tool_source("set_order_id", str(root)) == bad


def test_read_tool_source_propagates_undecodable_bytes(tmp_path):
  root = _tools_root(tmp_path, {"set_order_id": ""})
  (root / "set_order_id" / "python_function" / "python_code.py").write_bytes(
      b"\xff\xfe def x(): pass")
  # Not swallowed into None — only *absence* maps to None.
  with pytest.raises(UnicodeDecodeError):
    tool_scan.read_tool_source("set_order_id", str(root))


# --- setter / task source maps ---------------------------------------------------

def _sources_root(tmp_path):
  return _tools_root(tmp_path, {
      "set_order_id": "SET_ORDER = 1\n",
      "set_zip": "SET_ZIP = 1\n",
      "lookup_order": "LOOKUP = 1\n",
  })


def test_setter_sources_dedupe_and_skip_unknown_setters(tmp_path):
  root = _sources_root(tmp_path)
  config = {"slots": [
      {"name": "order_id", "setter": "set_order_id"},
      {"name": "order_id_again", "setter": "set_order_id"},   # duplicate
      {"name": "zip", "setter": "set_zip"},
      {"name": "ghost", "setter": "set_missing"},             # no file -> skipped
      {"name": "announce_only"},                              # no setter key
      {"name": "blank", "setter": ""},                        # falsy setter
  ]}
  assert tool_scan.read_setter_sources(config, str(root)) == {
      "set_order_id": "SET_ORDER = 1\n", "set_zip": "SET_ZIP = 1\n"}


def test_setter_sources_tolerate_missing_or_null_slots(tmp_path):
  root = _sources_root(tmp_path)
  assert tool_scan.read_setter_sources({}, str(root)) == {}
  assert tool_scan.read_setter_sources({"slots": None}, str(root)) == {}
  assert tool_scan.read_setter_sources({"slots": []}, str(root)) == {}


def test_task_tool_sources_dedupe_and_skip_unknown_tools(tmp_path):
  root = _sources_root(tmp_path)
  config = {"tasks": [
      {"name": "lookup", "tool": "lookup_order"},
      {"name": "lookup_retry", "tool": "lookup_order"},       # duplicate
      {"name": "ghost", "tool": "no_such_tool"},              # no file -> skipped
      {"name": "toolless"},
      {"name": "blank", "tool": ""},
  ]}
  assert tool_scan.read_task_tool_sources(config, str(root)) == {
      "lookup_order": "LOOKUP = 1\n"}


def test_task_tool_sources_tolerate_missing_or_null_tasks(tmp_path):
  root = _sources_root(tmp_path)
  assert tool_scan.read_task_tool_sources({}, str(root)) == {}
  assert tool_scan.read_task_tool_sources({"tasks": None}, str(root)) == {}


def test_setter_and_task_reads_ignore_the_other_section(tmp_path):
  root = _sources_root(tmp_path)
  config = {"slots": [{"name": "order_id", "setter": "set_order_id"}],
            "tasks": [{"name": "lookup", "tool": "lookup_order"}]}
  assert list(tool_scan.read_setter_sources(config, str(root))) == ["set_order_id"]
  assert list(tool_scan.read_task_tool_sources(config, str(root))) == ["lookup_order"]


# --- root resolution: arg > env > settings > packaged default --------------------

def test_the_env_var_supplies_the_root_when_no_arg_is_passed(tmp_path, monkeypatch):
  root = _tools_root(tmp_path, {"set_order_id": "SET = 1\n"})
  monkeypatch.setenv(fb.ENV_FRAMEWORK_ROOT, str(root))
  assert tool_scan.discover_tool_names() == ["set_order_id"]
  assert tool_scan.read_tool_source("set_order_id") == "SET = 1\n"


def test_the_explicit_root_argument_beats_the_env_var(tmp_path, monkeypatch):
  env_root = _tools_root(tmp_path / "env", {"from_env": ""})
  arg_root = _tools_root(tmp_path / "arg", {"from_arg": ""})
  monkeypatch.setenv(fb.ENV_FRAMEWORK_ROOT, str(env_root))
  assert tool_scan.discover_tool_names(str(arg_root)) == ["from_arg"]


def test_the_env_var_beats_the_settings_root(tmp_path, monkeypatch):
  env_root = _tools_root(tmp_path / "env", {"from_env": ""})
  settings_root = _tools_root(tmp_path / "settings", {"from_settings": ""})
  monkeypatch.setenv(fb.ENV_FRAMEWORK_ROOT, str(env_root))
  monkeypatch.setattr(fb, "_SETTINGS_FRAMEWORK_ROOT", str(settings_root))
  assert tool_scan.discover_tool_names() == ["from_env"]


def test_the_settings_root_is_used_when_env_is_unset(tmp_path, monkeypatch):
  root = _tools_root(tmp_path, {"from_settings": ""})
  monkeypatch.setattr(fb, "_SETTINGS_FRAMEWORK_ROOT", str(root))
  assert tool_scan.discover_tool_names() == ["from_settings"]


def test_the_packaged_bundle_is_the_zero_config_default():
  names = tool_scan.discover_tool_names()
  available = tool_scan.discover_available_tools()
  for infra in ("slot_filling_engine", "validate_dag_config", "slot_intake"):
    assert infra in names            # the blessed bundle ships all three
    assert infra not in available    # ...and none is an authorable tool
  assert "transfer_to_human" in available
  # The bundle is framework code only — flow definitions come from the author.
  assert tool_scan.discover_dag_configs() == []


# ===========================================================================
# validation: the verdict is the framework's, the anchoring is ours
# ===========================================================================

def _clean_config(slot: str = "item", task: str = "submit") -> dict:
  """A config the framework validator accepts with zero errors and zero warnings."""
  return {
      "slots": [
          {"name": slot, "source": "user", "ask": "What item?",
           "setter": "set_item", "hint": "the item"},
          {"name": "order_id", "source": f"task:{task}"},
      ],
      "tasks": [
          {"name": task, "tool": "submit_order", "inputs": [slot],
           "outputs": {"id": "order_id"}, "terminal": True,
           "then_say": "All set."},
      ],
  }


_CLEAN_TOOLS = ["set_item", "submit_order"]

# A setter whose source writes values["second"], so a config declaring
# setter_field "first" is provably wrong at the SOURCE level.
_SETTER_SRC = '''
def set_pair(second=None):
  values = {}
  values["second"] = second
  return {"success": True, "values": values}
'''

# A task tool whose source never returns the "id" key the config declares.
_TASK_SRC = '''
def submit_order(item=None):
  return {"success": True, "ok": True}
'''


def _mini_framework_root(tmp_path, tools: dict[str, str]) -> str:
  """The real validator plus exactly the given fake tool sources, nothing else."""
  root = tmp_path / "tools"
  root.mkdir()
  (root / "validate_dag_config").symlink_to(
      fb.default_framework_root() / "validate_dag_config")
  for name, src in tools.items():
    pkg = root / name / "python_function"
    pkg.mkdir(parents=True)
    (pkg / "python_code.py").write_text(src, encoding="utf-8")
  return str(root)


def _raws(report) -> list[str]:
  return [d.raw for d in report.diagnostics]


def _stub_validator_module(**attrs):
  """A stand-in `validate_dag_config` module whose result carries `attrs`."""
  class _Result:
    pass

  result = _Result()
  for key, value in attrs.items():
    setattr(result, key, value)

  class _Validator:
    def __init__(self, config, **kwargs):
      self.config = config
      self.kwargs = kwargs

    def validate(self):
      return result

  return types.SimpleNamespace(DagConfigValidator=_Validator)


# --- _to_plain -------------------------------------------------------------------

def test_to_plain_dumps_a_pydantic_config_by_alias():
  # `in` is a python keyword, so ConditionLeaf stores it as `in_`; the framework
  # only understands the wire name.
  cfg = models.Config(slots=[{"name": "a", "source": "user",
                              "condition": {"slot": "b", "in": [1, 2]}}])
  assert validation._to_plain(cfg)["slots"][0]["condition"] == {  # noqa: SLF001
      "slot": "b", "in": [1, 2]}


def test_to_plain_drops_none_fields():
  # An unset optional must be ABSENT, not `None` — the framework's key checks
  # treat a present-but-null key as authored.
  plain = validation._to_plain(models.Config(slots=[{"name": "a"}]))  # noqa: SLF001
  assert plain["slots"][0] == {"name": "a"}
  assert "gate_slot" not in plain and "bootstrap" not in plain


def test_to_plain_accepts_any_model_dump_object():
  # The duck-typed branch: not a Config, but exposes model_dump.
  assert validation._to_plain(  # noqa: SLF001
      models.Slot(name="a", source="user")) == {"name": "a", "source": "user"}


def test_to_plain_copies_a_plain_dict():
  src = {"slots": [], "tasks": []}
  out = validation._to_plain(src)  # noqa: SLF001
  assert out == src and out is not src


# --- raw_validate_single ----------------------------------------------------------

def test_a_clean_config_is_accepted_with_no_diagnostics():
  # The companion to every rejection below: the rules are not vacuous.
  assert validation.raw_validate_single(
      _clean_config(), available_tools=_CLEAN_TOOLS) == (True, [], [])


def test_an_empty_config_is_rejected():
  valid, errors, _w = validation.raw_validate_single({})
  assert valid is False
  assert "Config has no 'slots' and no 'tasks'" in errors


def test_a_user_slot_without_a_setter_is_rejected():
  valid, errors, _w = validation.raw_validate_single(
      {"slots": [{"name": "a", "source": "user", "ask": "?"}], "tasks": []})
  assert valid is False
  assert any("Slot 'a' has source 'user' but no setter" in e for e in errors)


def test_missing_tasks_is_a_warning_not_an_error():
  # The severity split matters: a warning must NOT flip `valid`.
  valid, errors, warnings = validation.raw_validate_single(
      {"slots": [{"name": "a", "source": "announce", "message": "Hi"}]})
  assert "Config has no 'tasks'" in warnings
  assert "Config has no 'tasks'" not in errors
  assert (valid, errors) == (True, [])


def test_an_unknown_tool_is_flagged_only_when_available_tools_is_supplied():
  cfg = _clean_config()
  assert validation.raw_validate_single(cfg)[0] is True      # no list -> no check
  valid, errors, _w = validation.raw_validate_single(
      cfg, available_tools=["something_else"])
  assert valid is False
  assert "Slot 'item' setter 'set_item' not in agent tool list" in errors
  assert "Task 'submit' tool 'submit_order' not in agent tool list" in errors


def test_source_aware_checks_are_a_strict_superset():
  cfg = _clean_config()
  cfg["slots"][0]["setter"] = "set_pair"
  cfg["slots"][0]["setter_field"] = "first"
  tools = ["set_pair", "submit_order"]

  _v, plain_errors, _w = validation.raw_validate_single(cfg, available_tools=tools)
  assert plain_errors == []                                  # invisible without sources

  valid, errors, _w = validation.raw_validate_single(
      cfg, available_tools=tools,
      setter_sources={"set_pair": _SETTER_SRC},
      task_tool_sources={"submit_order": _TASK_SRC})
  assert valid is False
  assert set(plain_errors).issubset(errors)                  # superset, never a swap
  assert any('never writes values["first"]' in e for e in errors)
  assert any("expects output key 'id'" in e for e in errors)


def test_the_returned_lists_are_copies_not_validator_internals():
  _v, errors, warnings = validation.raw_validate_single({})
  errors.append("mutated")
  warnings.append("mutated")
  _v2, errors2, warnings2 = validation.raw_validate_single({})
  assert "mutated" not in errors2 and "mutated" not in warnings2


def test_a_missing_framework_root_raises_file_not_found(tmp_path):
  with pytest.raises(FileNotFoundError, match="validate_dag_config"):
    validation.raw_validate_single({}, framework_root=str(tmp_path))


# --- raw_validate_cross ------------------------------------------------------------

def _corpus_member(slot: str, task: str, gate: str = "active_flow",
                   reset: bool = True) -> dict:
  cfg = _clean_config(slot, task)
  cfg["gate_slot"] = gate
  cfg["bootstrap"] = {"tool": "set_active_flow", "slot": gate,
                      "reset_on_complete": reset}
  return cfg


def test_a_consistent_corpus_is_accepted_by_cross_validation():
  assert validation.raw_validate_cross({
      "alpha": _corpus_member("item", "submit"),
      "beta": _corpus_member("other", "finish"),
  }) == (True, [], [])


def test_differing_gate_slots_across_configs_is_a_warning():
  valid, errors, warnings = validation.raw_validate_cross({
      "alpha": _corpus_member("item", "submit", gate="active_flow"),
      "beta": _corpus_member("other", "finish", gate="other_gate"),
  })
  assert (valid, errors) == (True, [])                       # warning only
  assert any(w.startswith("Different gate_slot names across configs")
             for w in warnings)


def test_a_component_task_referencing_an_unknown_child_is_an_error():
  parent = _clean_config()
  parent["tasks"].append({"name": "sub", "component": "missing_child",
                          "inputs": ["item"]})
  valid, errors, _w = validation.raw_validate_cross({"parent": parent})
  assert valid is False
  assert any("references unknown child config 'missing_child'" in e for e in errors)


# --- validate_single ----------------------------------------------------------------

def test_validate_single_reports_valid_for_a_clean_config():
  req = models.ValidateRequest(config=_clean_config(),
                               available_tools=_CLEAN_TOOLS,
                               setter_sources={}, task_tool_sources={})
  report = validation.validate_single(req)
  assert isinstance(report, models.ValidationReport)
  assert report.valid is True and report.diagnostics == []


def test_validate_single_anchors_and_preserves_every_diagnostic():
  req = models.ValidateRequest(config=_clean_config(),
                               available_tools=["nothing_real"],
                               setter_sources={}, task_tool_sources={})
  report = validation.validate_single(req)
  assert report.valid is False
  by_kind = {d.anchor.kind: d for d in report.diagnostics}
  assert (by_kind["slot"].anchor.ref, by_kind["slot"].anchor.field) == (
      "item", "setter")
  assert (by_kind["task"].anchor.ref, by_kind["task"].anchor.field) == (
      "submit", "tool")
  assert all(d.raw == d.message for d in report.diagnostics)  # no prefix, lossless


def test_validate_single_reports_every_simultaneous_violation():
  # Independent rules broken at once; all of them survive to the report, each
  # with its own anchor — the mapper never collapses or drops.
  bad = {"slots": [{"name": "a", "source": "user", "ask": "?"},
                   {"name": "greet", "source": "announce"}],
         "bogus_key": 1}
  report = validation.validate_single(models.ValidateRequest(config=bad),
                                      auto_sources=False)
  raws = _raws(report)
  assert report.valid is False
  assert "Unknown top-level config keys: ['bogus_key']" in raws
  assert "Announce slot 'greet' requires 'message' or 'response'" in raws
  assert "Config has no 'tasks'" in raws
  assert any("has source 'user' but no setter" in r for r in raws)
  assert [d.severity for d in report.diagnostics].count("warning") == 1


def test_validate_single_accepts_a_pydantic_config_object():
  cfg = models.Config(**_clean_config())
  req = models.ValidateRequest(config=cfg, available_tools=_CLEAN_TOOLS,
                               setter_sources={}, task_tool_sources={})
  assert validation.validate_single(req).valid is True


def test_auto_sources_discovers_tools_and_sources_from_the_framework_root(tmp_path):
  # `submit_order`'s real source never returns "id", so the source-aware check
  # fires WITHOUT the caller supplying anything.
  root = _mini_framework_root(tmp_path, {
      "set_item":
          'def set_item(item=None):\n  return {"success": True, "value": item}\n',
      "submit_order": _TASK_SRC,
  })
  report = validation.validate_single(
      models.ValidateRequest(config=_clean_config()), framework_root=root)
  assert report.valid is False
  assert _raws(report) == [
      "Task 'submit' expects output key 'id' but tool 'submit_order' never returns it"]


def test_auto_sources_false_skips_the_tool_and_source_checks(tmp_path):
  root = _mini_framework_root(tmp_path, {
      "set_item":
          'def set_item(item=None):\n  return {"success": True, "value": item}\n',
      "submit_order": _TASK_SRC,
  })
  report = validation.validate_single(
      models.ValidateRequest(config=_clean_config()),
      framework_root=root, auto_sources=False)
  assert report.valid is True and report.diagnostics == []


def test_request_available_tools_win_over_discovery(monkeypatch):
  monkeypatch.setattr(tool_scan, "discover_available_tools",
                      lambda _root=None: ["nothing_real"])
  req = models.ValidateRequest(config=_clean_config(),
                               available_tools=_CLEAN_TOOLS,
                               setter_sources={}, task_tool_sources={})
  assert validation.validate_single(req).valid is True
  # Companion: omit them and the (sentinel) discovery result is what gets used.
  bare = models.ValidateRequest(config=_clean_config(),
                                setter_sources={}, task_tool_sources={})
  assert any("not in agent tool list" in r
             for r in _raws(validation.validate_single(bare)))


def test_request_sources_win_over_discovery(monkeypatch):
  monkeypatch.setattr(tool_scan, "read_task_tool_sources",
                      lambda _cfg, _root=None: {"submit_order": _TASK_SRC})
  req = models.ValidateRequest(config=_clean_config(),
                               available_tools=_CLEAN_TOOLS,
                               setter_sources={}, task_tool_sources={})
  assert validation.validate_single(req).valid is True       # the request's {} wins
  bare = models.ValidateRequest(config=_clean_config(),
                                available_tools=_CLEAN_TOOLS,
                                setter_sources={})
  assert any("expects output key 'id'" in r
             for r in _raws(validation.validate_single(bare)))


# --- the validator's structured diagnostics twin -------------------------------------

def test_a_structured_anchor_and_code_beat_the_regex_mapper(monkeypatch):
  msg = "Slot 'a' has source 'user' but no 'ask'"
  monkeypatch.setattr(fb, "load_validator", lambda _root=None: _stub_validator_module(
      valid=False, errors=[msg], warnings=[],
      diagnostics=[{"severity": "error", "message": msg, "code": "DAG042",
                    "fix_id": "add_ask",
                    "anchor": {"kind": "task", "ref": "elsewhere",
                               "field": "tool"}}]))
  report = validation.validate_single(
      models.ValidateRequest(config={"slots": [{"name": "a"}]}), auto_sources=False)
  d = report.diagnostics[0]
  assert (d.anchor.kind, d.anchor.ref, d.anchor.field) == ("task", "elsewhere", "tool")
  assert (d.model_extra or {}).get("code") == "DAG042"
  assert (d.model_extra or {}).get("fix_id") == "add_ask"
  assert d.raw == msg                                        # still lossless


def test_a_structured_entry_without_an_anchor_falls_back_to_the_regex(monkeypatch):
  msg = "Slot 'a' has source 'user' but no 'ask'"
  monkeypatch.setattr(fb, "load_validator", lambda _root=None: _stub_validator_module(
      valid=False, errors=[msg], warnings=[],
      diagnostics=[{"severity": "error", "message": msg, "code": "DAG042"}]))
  report = validation.validate_single(
      models.ValidateRequest(config={"slots": [{"name": "a"}]}), auto_sources=False)
  d = report.diagnostics[0]
  assert (d.anchor.kind, d.anchor.ref, d.anchor.field) == ("slot", "a", "ask")
  assert (d.model_extra or {}).get("code") == "DAG042"


def test_a_structured_entry_is_matched_on_severity_as_well_as_message():
  # The same text as an error and as a warning are different entries; a warning's
  # structured anchor must not colour the error.
  msg = "Slot 'a' is odd"
  out = validation.map_diagnostics(
      [msg], [msg], None,
      [{"severity": "warning", "message": msg,
        "anchor": {"kind": "flow", "ref": "beta"}}])
  error, warning = out
  assert error.severity == "error" and error.anchor.kind == "slot"
  assert warning.severity == "warning" and warning.anchor.kind == "flow"


def test_a_structured_anchor_without_a_kind_is_ignored():
  out = validation.map_diagnostics(
      ["Slot 'a' has source 'user' but no 'ask'"], [], None,
      [{"severity": "error", "message": "Slot 'a' has source 'user' but no 'ask'",
        "anchor": {"ref": "a"}}])
  assert out[0].anchor.kind == "slot"                        # regex fallback ran


def test_blockers_and_shippable_ride_through_to_the_report(monkeypatch):
  monkeypatch.setattr(fb, "load_validator", lambda _root=None: _stub_validator_module(
      valid=True, errors=[], warnings=[], blockers=["needs a human review"],
      shippable=False))
  report = validation.validate_single(
      models.ValidateRequest(config={"slots": [{"name": "a"}]}), auto_sources=False)
  assert report.valid is True
  assert report.blockers == ["needs a human review"]
  assert report.shippable is False


def test_shippable_defaults_to_valid_when_the_validator_reports_none(monkeypatch):
  monkeypatch.setattr(fb, "load_validator", lambda _root=None: _stub_validator_module(
      valid=False, errors=["boom"], warnings=[]))
  report = validation.validate_single(
      models.ValidateRequest(config={"slots": [{"name": "a"}]}), auto_sources=False)
  assert report.shippable is False and report.blockers == []


def test_a_null_blockers_list_becomes_an_empty_one(monkeypatch):
  monkeypatch.setattr(fb, "load_validator", lambda _root=None: _stub_validator_module(
      valid=True, errors=[], warnings=[], blockers=None))
  report = validation.validate_single(
      models.ValidateRequest(config={"slots": [{"name": "a"}]}), auto_sources=False)
  assert report.blockers == []


# --- validate_cross -------------------------------------------------------------------

def test_validate_cross_reports_per_config_and_combined():
  req = models.ValidateCrossRequest(
      configs={"alpha": _corpus_member("item", "submit"),
               "beta": _corpus_member("other", "finish")},
      available_tools=_CLEAN_TOOLS + ["set_active_flow"])
  report = validation.validate_cross(req)
  assert isinstance(report, models.CrossValidationReport)
  assert report.valid is True
  assert sorted(report.perConfig) == ["alpha", "beta"]
  assert all(isinstance(r, models.ValidationReport)
             for r in report.perConfig.values())
  assert report.diagnostics == []


def test_an_empty_corpus_is_valid():
  report = validation.validate_cross(models.ValidateCrossRequest(configs={}))
  assert report.valid is True
  assert report.diagnostics == [] and report.perConfig == {}


def test_per_config_diagnostics_are_flow_prefixed_but_keep_their_anchor():
  bad = {"slots": [{"name": "a", "source": "user", "ask": "?"}], "tasks": []}
  report = validation.validate_cross(
      models.ValidateCrossRequest(configs={"acme": bad}))
  setter_diag = next(d for d in report.diagnostics if "no setter" in d.raw)
  assert setter_diag.message.startswith("[acme] ")
  assert setter_diag.raw.startswith("[acme] ")
  assert (setter_diag.anchor.kind, setter_diag.anchor.ref) == ("slot", "a")
  assert setter_diag.group is None
  # The per-config report itself keeps the UNprefixed string.
  assert all(not d.raw.startswith("[")
             for d in report.perConfig["acme"].diagnostics)


def test_cross_only_diagnostics_are_grouped_across_flows():
  req = models.ValidateCrossRequest(
      configs={"alpha": _corpus_member("item", "submit", gate="active_flow"),
               "beta": _corpus_member("other", "finish", gate="other_gate")},
      available_tools=_CLEAN_TOOLS + ["set_active_flow"])
  grouped = [d for d in validation.validate_cross(req).diagnostics
             if d.group == "across_flows"]
  assert len(grouped) == 1
  assert grouped[0].severity == "warning"
  assert grouped[0].raw.startswith("Different gate_slot names across configs")
  assert grouped[0].anchor.kind == "flow"


def test_the_corpus_is_invalid_when_only_the_cross_check_fails():
  # Each config validates clean on its own; only the CORPUS is broken.
  configs = {"alpha": _corpus_member("item", "submit", reset=False),
             "beta": _corpus_member("other", "finish", reset=False)}
  tools = _CLEAN_TOOLS + ["set_active_flow"]
  assert validation.raw_validate_single(
      configs["alpha"], available_tools=tools)[0] is True
  report = validation.validate_cross(
      models.ValidateCrossRequest(configs=configs, available_tools=tools))
  assert report.valid is False
  assert all(r.valid for r in report.perConfig.values())
  assert all(d.group == "across_flows" for d in report.diagnostics)


def test_the_corpus_is_invalid_when_only_one_config_fails():
  configs = {"alpha": _corpus_member("item", "submit"),
             "beta": {"slots": [{"name": "b", "source": "user", "ask": "?"}],
                      "tasks": [], "gate_slot": "active_flow",
                      "bootstrap": {"tool": "set_active_flow",
                                    "slot": "active_flow",
                                    "reset_on_complete": True}}}
  report = validation.validate_cross(models.ValidateCrossRequest(
      configs=configs, available_tools=_CLEAN_TOOLS + ["set_active_flow"]))
  assert report.valid is False
  assert report.perConfig["alpha"].valid is True
  assert report.perConfig["beta"].valid is False


def test_the_cross_per_config_pass_never_runs_source_aware_checks(monkeypatch):
  # validate_cross documents a PLAIN per-config call; source discovery must not
  # sneak in (it would make the corpus verdict differ from the framework's).
  def _boom(*_a, **_k):
    raise AssertionError("validate_cross must not scan sources")

  monkeypatch.setattr(tool_scan, "read_setter_sources", _boom)
  monkeypatch.setattr(tool_scan, "read_task_tool_sources", _boom)
  monkeypatch.setattr(tool_scan, "discover_available_tools", _boom)
  report = validation.validate_cross(models.ValidateCrossRequest(
      configs={"alpha": _corpus_member("item", "submit")}))
  assert report.valid is True


def test_with_flow_prefix_preserves_severity_and_anchor():
  d = models.Diagnostic(severity="warning", message="m", raw="m",
                        anchor=models.NodeAnchor(kind="slot", ref="a"))
  out = validation._with_flow_prefix(d, "acme")  # noqa: SLF001
  assert out.severity == "warning"
  assert (out.message, out.raw) == ("[acme] m", "[acme] m")
  assert out.anchor == d.anchor


# --- map_diagnostics ---------------------------------------------------------------

def test_map_diagnostics_orders_errors_before_warnings():
  out = validation.map_diagnostics(["e1", "e2"], ["w1"], None)
  assert [d.severity for d in out] == ["error", "error", "warning"]
  assert [d.message for d in out] == ["e1", "e2", "w1"]


def test_map_diagnostics_on_empty_input():
  assert validation.map_diagnostics([], [], None) == []


def test_an_unrecognised_message_is_kept_unanchored_never_dropped():
  out = validation.map_diagnostics(["a framework message we have never seen"], [], None)
  assert len(out) == 1
  assert out[0].anchor is None
  assert out[0].raw == "a framework message we have never seen"


def test_the_flow_prefix_is_stripped_from_message_but_kept_in_raw():
  d = validation.map_diagnostics(
      ["[alpha] Slot 'x' has source 'user' but no 'ask'"], [], None)[0]
  assert d.message == "Slot 'x' has source 'user' but no 'ask'"
  assert d.raw == "[alpha] Slot 'x' has source 'user' but no 'ask'"
  assert d.anchor.ref == "x"                                 # anchored past the prefix


# --- _anchor_for branches ------------------------------------------------------------

def _anchor(body):
  return validation._anchor_for(body, None)  # noqa: SLF001


def test_anchor_slot_and_announce_slot_messages():
  a = _anchor("Slot 'a' has source 'user' but no 'ask'")
  assert (a.kind, a.ref, a.field) == ("slot", "a", "ask")
  b = _anchor("Announce slot 'greet' requires 'message' or 'response'")
  assert (b.kind, b.ref, b.field) == ("slot", "greet", "message")


def test_anchor_unnamed_slot_and_task_by_index():
  a = _anchor("Slot at index 0 has no 'name'")
  assert (a.kind, a.ref, a.field) == ("slot", None, "name")
  b = _anchor("Task at index 2 has no 'name'")
  assert (b.kind, b.ref, b.field) == ("task", None, "name")


def test_anchor_task_messages():
  a = _anchor("Task 'submit' tool 'do_it' not in agent tool list")
  assert (a.kind, a.ref, a.field) == ("task", "submit", "tool")
  b = _anchor("Task 't': condition references unknown slot 'zzz'")
  assert (b.kind, b.ref, b.field) == ("task", "t", "condition")


def test_anchor_lambda_condition_context_prefixes():
  # A lambda-source SyntaxError is reported with the compile() filename.
  a = _anchor("<slot:zip:condition> invalid syntax")
  assert (a.kind, a.ref, a.field) == ("slot", "zip", "condition")
  b = _anchor("<task:submit:condition> invalid syntax")
  assert (b.kind, b.ref, b.field) == ("task", "submit", "condition")


def test_anchor_setter_messages():
  a = _anchor("Setter 'set_x' may not return a 'value' key")
  assert (a.kind, a.ref, a.field) == ("field", "set_x", "setter")
  b = _anchor("Slots ['a', 'b'] all map to setter 's1' without setter_field")
  assert (b.kind, b.ref, b.field) == ("field", "s1", "setter")


def test_anchor_cross_config_flow_messages():
  a = _anchor("Config 'alpha' has terminal tasks but bootstrap...")
  assert (a.kind, a.ref) == ("flow", "alpha")
  for body in ("Different gate_slot names across configs: {}",
               "Different bootstrap tools across configs: {}"):
    assert (_anchor(body).kind, _anchor(body).ref) == ("flow", None)
  # A "Different ..." message that is not one of the two known ones stays bare.
  assert _anchor("Different something else entirely") is None


def test_anchor_top_level_field_tokens():
  a = _anchor("bootstrap.slot 'missing' not in slots")
  assert (a.kind, a.ref, a.field) == ("field", "bootstrap", "slot")
  b = _anchor("bootstrap has no 'tool'")
  assert (b.kind, b.ref, b.field) == ("field", "bootstrap", None)
  c = _anchor("gate_slot 'active_flow' not in slots")
  assert (c.kind, c.ref) == ("field", "gate_slot")
  d = _anchor("'exit_status' must be a dict")                # quoted token form
  assert (d.kind, d.ref) == ("field", "exit_status")


def test_anchor_whole_config_messages():
  for body in ("Config has no 'slots' and no 'tasks'",
               "Unknown top-level config keys: ['x']"):
    assert (_anchor(body).kind, _anchor(body).ref) == ("field", "config")


def test_anchor_returns_none_for_an_unknown_message():
  assert _anchor("Some totally unrecognised message") is None


def test_an_announce_slot_without_a_quoted_name_is_unanchored():
  # The quoted form is claimed by the slot regex above; the unquoted form has no
  # flow to point at, so it stays bare rather than being mis-anchored.
  assert _anchor("Announce slot in configs blah") is None


# --- field pickers ---------------------------------------------------------------------

def test_the_named_missing_field_wins_over_an_earlier_field_name():
  # "source" appears first, but the sentence is complaining about 'ask'.
  assert validation._slot_field(  # noqa: SLF001
      "Slot 'a' has source 'user' but no 'ask'") == "ask"
  assert validation._task_field(  # noqa: SLF001
      "Task 't' outputs but missing 'inputs'") == "inputs"


def test_the_field_falls_back_to_the_first_mentioned_one():
  assert validation._slot_field(  # noqa: SLF001
      "Slot 'a' requires unknown 'nope'") == "requires"
  assert validation._task_field(  # noqa: SLF001
      "Task 't' expects output key 'id' but tool 'x' never returns it") == "tool"


def test_the_field_is_none_when_no_known_field_is_mentioned():
  assert validation._slot_field("Slot 'a': 'all' must be a list") is None  # noqa: SLF001
  assert validation._task_field("Task 't' is weird") is None  # noqa: SLF001


def test_dotted_field_parsing():
  assert validation._dotted_field(  # noqa: SLF001
      "steer_back.soft_after must be an int", "steer_back") == "soft_after"
  assert validation._dotted_field("'cancel'.tool missing", "cancel") == "tool"  # noqa: SLF001
  assert validation._dotted_field("cancel has no tool", "cancel") is None  # noqa: SLF001
