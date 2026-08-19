"""Hardening for the author-facing toolkit: scaffolding, emit contract, drive, sim, CLI.

Each test here pins a defect found by sweeping the toolkit with hostile-but-ordinary
inputs — an agent name with a quote in it, a config id that is not an identifier, a
caller who passes the keyword the wrapper hardcodes. They share a theme: the failure
was silent at the point of the mistake and loud somewhere much later (a `SyntaxError`
at deploy, a `TypeError` inside a transport, a result that changes after you have it).

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_toolkit_hardening.py
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib

import pytest
from pydantic import ValidationError

import flows
from flows import VerdictBranch, task, verdict
from flows.config import config_io, validation
from flows.emit import models as emit_models
from flows.engine import blessed_source
from flows.sim import engine_sim
from flows.templates import scaffold_project


# --- `flows new`: the emitted project must always parse -----------------------
# The name was interpolated raw into a double-quoted Python string literal, so a
# quote in it closed the literal early and a newline left it unterminated. `flows
# new` exited 0 claiming success and `flows validate` then died on a SyntaxError
# in a file the author never wrote.
HOSTILE_NAMES = [
    'Acme "Pro" Bot',
    "Line one\nLine two",
    "O'Brien \\ Co",
    'ends with a backslash \\',
    "Café Ünïcode",
]


def _scaffolded(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
  scaffold_project(str(tmp_path / "proj"), name=name)
  return tmp_path / "proj"


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_agent_name_still_emits_parseable_app_py(tmp_path, name):
  src = (_scaffolded(tmp_path, name) / "app.py").read_text(encoding="utf-8")
  ast.parse(src)  # the whole point: `flows validate` must not hit a SyntaxError


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_the_agent_name_survives_the_round_trip_verbatim(tmp_path, name):
  """Escaping must not change the name — the app's display name is user-visible."""
  src = (_scaffolded(tmp_path, name) / "app.py").read_text(encoding="utf-8")
  values = [ast.literal_eval(kw.value)
            for kw in ast.walk(ast.parse(src))
            if isinstance(kw, ast.keyword) and kw.arg == "app_display_name"]
  assert values == [name]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_the_readme_h1_stays_a_single_heading_line(tmp_path, name):
  """Markdown escapes nothing, but a heading IS line-terminated: a newline in the
  name splits the H1 and demotes the rest of it to body text."""
  lines = (_scaffolded(tmp_path, name) / "README.md").read_text(
      encoding="utf-8").splitlines()
  assert lines[0].startswith("# ")
  assert lines[0][2:] == " ".join(name.split())
  assert lines[1] == ""  # the H1 is still followed by the blank line, not by leftovers


def test_an_ordinary_name_is_unchanged(tmp_path):
  """The escaping is invisible for the names everybody actually uses."""
  src = (_scaffolded(tmp_path, "Demo Agent") / "app.py").read_text(encoding="utf-8")
  assert 'app_display_name="Demo Agent"' in src


# --- the emit contract: a config id becomes a function name -------------------
# `config_id` is documented "identifier-validated" and was not. The scaffolder
# renders it straight into `def <config_id>_dag()`, so `"my-config id"` emitted a
# module that does not parse — reported as ok=True with zero validation errors,
# and discovered at deploy.
BAD_IDS = ["my-config id", "my-config", "2fast", "", "my config", "dag.tool", "a-b"]


def _request(**over):
  base = dict(app_display_name="Demo", config_id="demo", root_agent="Demo_Agent",
              gcp_project="a-project")
  base.update(over)
  return emit_models.ScaffoldRequest(**base)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_a_non_identifier_config_id_is_rejected(bad):
  with pytest.raises(ValidationError, match="not a Python identifier"):
    _request(config_id=bad)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_a_non_identifier_extra_config_key_is_rejected(bad):
  """Every `extra_configs` key is emitted as its own `<key>_dag` — same hazard."""
  with pytest.raises(ValidationError, match="not a Python identifier"):
    _request(extra_configs={bad: {"slots": []}})


@pytest.mark.parametrize("bad", BAD_IDS)
def test_a_non_identifier_all_configs_key_is_rejected(bad):
  """`MultiAgentScaffoldRequest.all_configs` keys are rendered the same way."""
  host = emit_models.HostAgentSpec(name="Host", instruction="i", child_agents=[],
                                   tools=[])
  with pytest.raises(ValidationError, match="not a Python identifier"):
    emit_models.MultiAgentScaffoldRequest(
        app_display_name="Demo", gcp_project="a-project", host=host, agents=[],
        all_configs={bad: {"slots": []}}, agent_config_map={})


def test_a_rejected_config_id_would_really_have_broken_the_module():
  """Not a style rule: the rendered source genuinely fails to parse."""
  with pytest.raises(SyntaxError):
    ast.parse("def my-config id_dag() -> dict:\n  return {}\n")


def test_a_legal_config_id_still_builds_the_request():
  req = _request(config_id="demo_flow_2", extra_configs={"child_flow": {"slots": []}})
  assert req.config_id == "demo_flow_2"
  assert ast.parse(f"def {req.config_id}_dag() -> dict:\n  return {{}}\n")


# --- drive: the tool-fakes wrapper must not fight its caller ------------------
class _FakeTransport:
  def __init__(self):
    self.calls = []

  def run(self, **kw):
    self.calls.append(kw)
    return kw


class _FakeSession:
  def __init__(self, app_name=None, initial_variable_state=None, **_kw):
    self.app_name = app_name
    self.seeded = initial_variable_state
    self.is_ended = False
    self._sessions = _FakeTransport()


def _factory(app_name=None, initial_variable_state=None, **kw):
  return _FakeSession(app_name, initial_variable_state, **kw)


def test_a_caller_supplied_use_tool_fakes_does_not_collide():
  """The wrapper hardcoded `run(use_tool_fakes=True, **kw)`, so a caller passing
  that keyword got `TypeError: got multiple values for keyword argument`."""
  from flows import drive

  session = drive.open_session({"a": "1"}, "abc-123", session_factory=_factory)
  session._sessions.run(text="hi", use_tool_fakes=False)
  assert session._sessions.calls[0]["use_tool_fakes"] is False, (
      "an explicit caller value must win over the default")


def test_opening_a_session_twice_does_not_double_wrap():
  """A second `open_session` over the same session re-wrapped the already-wrapped
  `run`, and the two hardcoded `use_tool_fakes=True` collided on the next call."""
  from flows import drive

  session = drive.open_session({"a": "1"}, "abc-123", session_factory=_factory)
  wrapped_once = session._sessions.run
  again = drive.open_session({"a": "1"}, "abc-123", session_factory=lambda **_k: session)

  assert again is session
  assert again._sessions.run is wrapped_once, "the wrap must be idempotent"
  again._sessions.run(text="hi")
  assert again._sessions.calls[0]["use_tool_fakes"] is True


# --- the sim must not hand out live state, or advance on a step that raised ---
def _two_task_config() -> dict:
  f = verdict(
      "diagnose",
      spine=[task("line_check", "check_line", [], "line_status"),
             task("acct_check", "check_account", [], "account_status")],
      branches=[VerdictBranch(condition=flows.has("line_status"), say="All checked.")],
      root_agent="Diag_Agent")
  f.set("single_flow", True)
  f.set("gate_slot", "active_flow")
  f.set("bootstrap", {"slot": "active_flow", "reset_on_complete": True})
  return f.to_config()


def _task_step(session_id: str, name: str, payload: dict) -> dict:
  return engine_sim.step({"session_id": session_id, "kind": "task_result",
                          "task_name": name, "result": payload, "success": True})


def test_slot_inspection_task_results_do_not_change_under_a_later_turn():
  """`_slot_inspection` is called with the LIVE `sm` (unlike the deep-copied `sm`
  snapshot beside it), and it returned `sm["task_results"]` by reference — so a
  result already handed to a caller grew a later task's output retroactively."""
  engine_sim.reset_store()
  session_id, _ = engine_sim.start(_two_task_config(), "diagnose")

  first = _task_step(session_id, "line_check", {"line_status": "outage"})
  handed_out = first["slot_inspection"]["task_results"]
  assert sorted(handed_out) == ["line_check"]

  _task_step(session_id, "acct_check", {"account_status": "suspended"})
  assert sorted(handed_out) == ["line_check"], (
      "the earlier step's result mutated after it was returned")
  # ...and it is the LIVE dict that grew, which is what the caller used to hold.
  assert sorted(engine_sim.session_sm(session_id)["task_results"]) == [
      "acct_check", "line_check"]
  assert handed_out is not engine_sim.session_sm(session_id)["task_results"]


def test_a_step_that_raises_leaves_no_phantom_history_or_inflated_counter():
  """The history push and the step-index bump ran BEFORE the kind dispatch, and
  only the unknown-kind branch rolled them back — so a raising step left a
  snapshot the caller could `back()` into and a step counter that had advanced
  past a turn that never happened."""
  engine_sim.reset_store()
  session_id, _ = engine_sim.start(_two_task_config(), "diagnose")
  _task_step(session_id, "line_check", {"line_status": "outage"})

  session = engine_sim._SESSIONS[session_id]
  before = (len(session.history), session.step_index, session.n_user_turns)

  with pytest.raises(Exception):
    engine_sim.step({"session_id": session_id, "kind": "setter_call",
                     "tool": "no_such_setter_at_all", "args": {}})

  assert (len(session.history), session.step_index, session.n_user_turns) == before


def test_a_normal_step_still_advances():
  """The rollback must not eat the ordinary case."""
  engine_sim.reset_store()
  session_id, _ = engine_sim.start(_two_task_config(), "diagnose")
  session = engine_sim._SESSIONS[session_id]

  _task_step(session_id, "line_check", {"line_status": "outage"})
  assert (len(session.history), session.step_index) == (1, 1)
  result = _task_step(session_id, "acct_check", {"account_status": "ok"})
  assert (len(session.history), session.step_index) == (2, 2)
  assert result["can_step_back"] is True


# --- a parse failure must carry the anchor its docstring promises -------------
def test_a_syntax_error_is_anchored_and_carries_its_line():
  """The anchor was attached only when the exception had NO `lineno`, so a
  SyntaxError — the one failure that knows where it is — came back unanchored,
  contradicting ConfigImportError's "line-anchored diagnostics"."""
  with pytest.raises(config_io.ConfigImportError) as excinfo:
    config_io.import_from_source("{\n  'slots': [],,\n}")

  diag = excinfo.value.diagnostics[0]
  assert diag.anchor is not None, "a SyntaxError produced a diagnostic with no anchor"
  assert diag.anchor.kind == "field"
  assert (diag.model_extra or {}).get("line"), (
      "the source line survived only inside the message text")


def test_a_lineless_parse_failure_is_still_anchored():
  """A ValueError from `literal_eval` carries no `lineno`; it anchors all the same."""
  with pytest.raises(config_io.ConfigImportError) as excinfo:
    config_io.import_from_source("{'slots': open('x')}")
  assert excinfo.value.diagnostics[0].anchor is not None


# --- the cross-config anchor table --------------------------------------------
def test_a_quoted_announce_slot_anchors_to_the_slot():
  """"Announce slot" sat in a cross-config `startswith` tuple where it could never
  produce an anchor: the quoted form already returned from the slot regex above it,
  and the unquoted form matched neither inner branch. Dropping the dead entry must
  not change either answer."""
  anchor = validation._anchor_for(
      "Announce slot 'welcome' in configs a, b differs", None)
  assert anchor is not None and anchor.kind == "slot" and anchor.ref == "welcome"


def test_an_unquoted_announce_slot_message_is_still_unanchored():
  assert validation._anchor_for("Announce slot in configs a, b differs", None) is None


@pytest.mark.parametrize("body,ref", [
    ("Config 'takeout' declares no gate_slot", "takeout"),
    ("Different bootstrap tools across configs", None),
    ("Different gate_slot across configs", None),
])
def test_the_cross_config_flow_anchors_survive(body, ref):
  anchor = validation._anchor_for(body, None)
  assert anchor is not None and anchor.kind == "flow" and anchor.ref == ref


# --- `cuj-apply --dry-run` must name the file the real run writes -------------
CUJS_YAML = """\
cujs:
  reboot:
    variables:
      account_id: "8069100230361003"
"""

APP_JSON = {"variableDeclarations": [
    {"name": "account_id", "schema": {"type": "string"}}]}


def _cuj_args(tmp_path, app_dir):
  cujs = tmp_path / "cujs.yaml"
  cujs.write_text(CUJS_YAML, encoding="utf-8")
  return argparse.Namespace(file=str(cujs), cuj="reboot", app_dir=str(app_dir),
                            dry_run=True, to=None)


def test_dry_run_names_the_nested_app_json_the_real_run_writes(tmp_path, capsys):
  """`cxas pull` produces `<app-dir>/<app>/app.json`, which the writer's finder
  accepts — but the dry run printed `<app-dir>/app.json`, a path that need not
  even exist, while the real run wrote the nested one."""
  from flows import cli

  app_dir = tmp_path / "pulled"
  nested = app_dir / "My_App"
  nested.mkdir(parents=True)
  (nested / "app.json").write_text(json.dumps(APP_JSON), encoding="utf-8")

  assert cli._cmd_cuj_apply(_cuj_args(tmp_path, app_dir)) == 0
  printed = capsys.readouterr().out
  assert str(nested / "app.json") in printed
  assert str(app_dir / "app.json") not in printed


def test_dry_run_still_names_a_flat_app_json(tmp_path, capsys):
  from flows import cli

  app_dir = tmp_path / "flat"
  app_dir.mkdir()
  (app_dir / "app.json").write_text(json.dumps(APP_JSON), encoding="utf-8")

  assert cli._cmd_cuj_apply(_cuj_args(tmp_path, app_dir)) == 0
  assert str(app_dir / "app.json") in capsys.readouterr().out


def test_dry_run_reports_a_missing_app_json_instead_of_inventing_one(tmp_path, capsys):
  from flows import cli

  app_dir = tmp_path / "empty"
  app_dir.mkdir()
  assert cli._cmd_cuj_apply(_cuj_args(tmp_path, app_dir)) == 1
  assert "cuj-apply:" in capsys.readouterr().err


def test_dry_run_writes_nothing(tmp_path):
  from flows import cli

  app_dir = tmp_path / "flat"
  app_dir.mkdir()
  path = app_dir / "app.json"
  before = json.dumps(APP_JSON)
  path.write_text(before, encoding="utf-8")

  cli._cmd_cuj_apply(_cuj_args(tmp_path, app_dir))
  assert path.read_text(encoding="utf-8") == before


# --- docstrings that count things must count the real thing -------------------
def test_the_framework_tool_count_in_the_docstrings_is_the_real_one():
  """Two docstrings hard-code the number of framework tools. Left to drift they
  mislead every reader about what an app carries, and nothing else notices."""
  source = pathlib.Path(blessed_source.__file__).read_text(encoding="utf-8")
  real = len(blessed_source._FRAMEWORK_TOOLS)
  claims = [line for line in source.splitlines() if "framework tools" in line
            and any(ch.isdigit() for ch in line)]
  assert claims, "the counted-tools docstrings vanished — retarget this test"
  for line in claims:
    assert f"The {real} " in line, f"stale tool count in: {line.strip()}"


def test_every_named_framework_tool_actually_ships():
  """The count is only meaningful if the tuple is the real inventory."""
  root = os.path.join(os.path.dirname(blessed_source.__file__), "framework", "tools")
  for tool in blessed_source._FRAMEWORK_TOOLS:
    assert os.path.isdir(os.path.join(root, tool)), f"{tool} is named but not shipped"
