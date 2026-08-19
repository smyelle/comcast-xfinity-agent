"""A failed emit must be loud, fatal, and leave nothing deployable behind.

The incident these pin down: the shared SDK was mid-edit (the blessed callbacks and
`framework/manifest.json` were being written — exactly what the scaffold's byte-sync
gate compares), so `scaffold.build` returned `ok=False`. Three defects stacked:

  1. the reason was read out of `validation.errors` ALONE, which is EMPTY for a
     drift failure, so the operator got `scaffold failed:` and nothing else;
  2. the raise came AFTER the tree had been written, so a complete-LOOKING app dir
     stayed on disk — minus everything the post-emit steps add. It was pushed to a
     live CES app carrying the framework's 7 `variableDeclarations` instead of the
     app's 29 (no `mock_json`, no `ani`) and scored 24/43;
  3. nothing compared the emitted tree against what was asked for, so neither the
     emit nor the deploy noticed the 22 missing variables.

One test per defect, plus the deploy-side re-verify.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_emit_integrity.py
"""

from __future__ import annotations

import json
import os
import re

import pytest

import flows
from flows.authoring import build as _build
from flows.authoring import integrity as _integrity
from flows.emit.models import ScaffoldResult, ValidationReportLite
from flows.engine import blessed_source as _bs

_EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")


def _app() -> flows.App:
  """A single-agent app with a BUSINESS variable (the thing that went missing)."""
  f = flows.Flow("claim", root_agent="Claim_Agent")
  f.add(
      flows.user_slot("claim_id", "What's your claim number?"),
      flows.result_slot("claim_status", "lookup"),
      flows.announce("done", ["{claim_status}"], requires=["claim_status"], end=True),
  )
  f.task("lookup", "lookup_claim", ["claim_id"], "claim_status",
         condition=flows.has("claim_id"))
  return flows.App(
      root_flow=f,
      app_display_name="Claims",
      variables=[{
          "name": "mock_json",
          "description": "Mock payloads for the eval harness",
          "schema": {"type": "STRING", "default": ""},
      }],
  )


def _drift_the_manifest(monkeypatch) -> None:
  """Make the committed manifest disagree with the bundle bytes.

  The real drift was the other way round (the bundle was being rewritten under a
  stale manifest); the check compares hash-to-hash, so corrupting the manifest's
  `before_agent` hash reproduces the same verdict deterministically without
  touching the shipped bundle.
  """
  real = _bs.manifest()
  drifted = json.loads(json.dumps(real))
  drifted["callbacks"]["before_agent"] = "0" * 64
  monkeypatch.setattr(_bs, "manifest", lambda: drifted)


# --- defect 2 (the incident): fatal, non-zero, no usable app dir --------------
def test_drifted_manifest_leaves_no_app_dir_and_exits_non_zero(
    tmp_path, monkeypatch, capsys):
  from flows.cli import main

  out = str(tmp_path / "app")
  _drift_the_manifest(monkeypatch)

  rc = main(["emit", os.path.join(_EXAMPLES, "track_shipment.py"), "--out", out])
  captured = capsys.readouterr()

  assert rc == 1, "a drifted framework must fail the emit"
  assert "emit: ok" not in captured.out, "a failed emit must never claim success"
  assert "framework drift" in captured.err, captured.err
  # THE regression: nothing deployable survives a failed scaffold.
  assert not os.path.exists(out), f"a failed emit left a tree at {out}"


def test_failed_emit_does_not_delete_a_dir_it_never_wrote(tmp_path, monkeypatch):
  """The cleanup only ever removes what THIS emit wrote.

  With `--no-overwrite` the scaffolder refuses a non-empty target, so the failure
  happens before any write — deleting the target then would destroy the good app
  that caused the refusal.
  """
  out = tmp_path / "app"
  out.mkdir()
  (out / "app.json").write_text('{"keep": "me"}')
  _drift_the_manifest(monkeypatch)

  with pytest.raises(ValueError) as exc:
    flows.build_app(_app(), str(out), overwrite=False)

  assert (out / "app.json").read_text() == '{"keep": "me"}'
  assert "Refusing to overwrite" in str(exc.value)


def test_keep_failed_marks_the_tree_it_keeps(tmp_path, monkeypatch):
  out = str(tmp_path / "app")
  _drift_the_manifest(monkeypatch)

  with pytest.raises(_build.ScaffoldFailed):
    flows.build_app(_app(), out, keep_failed=True)

  marker = os.path.join(out, "EMIT_FAILED.txt")
  assert os.path.isfile(marker), "--keep-failed must stamp what it keeps"
  assert "DO NOT DEPLOY" in open(marker).read()


# --- defect 1: the reason is never empty --------------------------------------
def test_scaffold_failure_reason_is_never_empty(tmp_path, monkeypatch):
  """`scaffold failed:` with nothing after it is the bug; name the real cause."""
  out = str(tmp_path / "app")
  _drift_the_manifest(monkeypatch)

  with pytest.raises(_build.ScaffoldFailed) as exc:
    flows.build_app(_app(), out)

  msg = str(exc.value)
  assert not msg.rstrip().endswith("scaffold failed:"), msg
  assert "framework drift" in msg
  # Specific enough to act on: the file, and what to do about it.
  assert "before_agent_callbacks" in msg
  assert "write_manifest" in msg


def _result(**kw) -> ScaffoldResult:
  base = dict(
      ok=False, files=[], written_to=None,
      validation=ValidationReportLite(valid=True, errors=[], warnings=[]),
      callback_sync_ok=True, uuids_unique=True,
      framework_version=_bs.version(), error=None,
  )
  base.update(kw)
  return ScaffoldResult(**base)


@pytest.mark.parametrize("res,want", [
    (_result(uuids_unique=False), "duplicate resource UUIDs"),
    (_result(callback_sync_ok=False), "framework drift"),
    (_result(error="Scaffold failed: boom"), "boom"),
    (_result(validation=ValidationReportLite(valid=False, errors=["bad dag"],
                                             warnings=[])), "bad dag"),
    # ok=False with every reason field empty: say THAT rather than "".
    (_result(), "no reason recorded"),
])
def test_every_ok_false_shape_reports_a_reason(res, want):
  reason = _build._scaffold_failure(res)
  assert reason.strip(), "an ok=False envelope must always produce a reason"
  assert want in reason
  # The crash-envelope text is not double-prefixed by the caller.
  assert not reason.startswith("Scaffold failed:")


# --- defect 3: post-emit asked-vs-landed self-check ---------------------------
def test_emit_fails_when_a_declared_variable_never_lands(tmp_path, monkeypatch):
  """Exactly the incident's damage: the variable-injection step does not run."""
  out = str(tmp_path / "app")
  monkeypatch.setattr(_build, "_inject_variables", lambda *a, **k: None)

  with pytest.raises(_integrity.EmitIntegrityError) as exc:
    flows.build_app(_app(), out)

  assert "mock_json" in str(exc.value)
  assert not os.path.exists(out)


def test_emit_fails_when_a_tool_a_task_names_is_missing(tmp_path, monkeypatch):
  """A tool resource that a task names but that never landed fails the emit."""
  out = str(tmp_path / "app")
  real_emit_language = _build._emit_language

  def _sabotage(out_dir, app, agent_names):  # last post-emit step before verify
    real_emit_language(out_dir, app, agent_names)
    import shutil
    shutil.rmtree(os.path.join(out_dir, "tools", "lookup_claim"))

  monkeypatch.setattr(_build, "_emit_language", _sabotage)
  with pytest.raises(_integrity.EmitIntegrityError) as exc:
    flows.build_app(_app(), out)

  assert "lookup_claim" in str(exc.value)
  assert not os.path.exists(out)


def test_clean_emit_passes_its_own_self_check(tmp_path):
  out = str(tmp_path / "app")
  res = flows.build_app(_app(), out)
  assert res.ok
  landed = {v["name"] for v in json.load(
      open(os.path.join(out, "app.json")))["variableDeclarations"]}
  assert "mock_json" in landed
  assert _integrity.verify_dir(out).ok


# --- the deploy-side gate -----------------------------------------------------
def test_deploy_refuses_a_drifted_app_dir(tmp_path):
  """`flows deploy` is handed a PATH, so it re-checks before it pushes."""
  from flows.deploy.push import deploy

  out = str(tmp_path / "app")
  flows.build_app(_app(), out)
  assert _integrity.verify_dir(out).ok

  # A framework file edited after emit (an older SDK, a hand fix, a bad merge).
  cb = os.path.join(out, "agents", "Claim_Agent", "before_agent_callbacks",
                    "before_agent_callbacks_01", "python_code.py")
  with open(cb, "a") as f:
    f.write("\n# hand edit\n")
  report = _integrity.verify_dir(out)
  assert not report.ok
  assert "before_agent_callbacks" in report.summary()

  with pytest.raises(RuntimeError) as exc:
    deploy(out, "projects/p/locations/us/apps/a", cxas="definitely-not-a-real-cli")
  assert "refusing to push" in str(exc.value)


def test_deploy_refuses_an_agent_whose_tool_has_no_resource(tmp_path):
  from flows.deploy.push import deploy

  out = str(tmp_path / "app")
  flows.build_app(_app(), out)
  aj_path = os.path.join(out, "agents", "Claim_Agent", "Claim_Agent.json")
  aj = json.load(open(aj_path))
  aj["tools"].append("tool_that_does_not_exist")
  with open(aj_path, "w") as f:
    json.dump(aj, f, indent=2)

  report = _integrity.verify_dir(out)
  assert not report.ok
  assert "tool_that_does_not_exist" in report.summary()
  with pytest.raises(RuntimeError):
    deploy(out, "projects/p/locations/us/apps/a", cxas="definitely-not-a-real-cli",
           preserve_from_target=False)


def test_verify_dir_ignores_ces_builtins(tmp_path):
  """`end_session` is a CES built-in: named by every agent, resource by none."""
  out = str(tmp_path / "app")
  flows.build_app(_app(), out)
  aj = json.load(open(os.path.join(out, "agents", "Claim_Agent", "Claim_Agent.json")))
  assert "end_session" in aj["tools"]
  assert not os.path.exists(os.path.join(out, "tools", "end_session"))
  assert _integrity.verify_dir(out).ok


def _entry_body(app_dir, tool):
  return os.path.join(app_dir, "tools", tool, "python_function", "python_code.py")


def test_verify_catches_an_entry_function_that_is_not_the_one_declared(tmp_path):
  """`pythonFunction.name` is what CES calls, so the body has to define it.

  A `@tool(name=…)` override renames the resource and leaves the `def` alone. The result
  deploys clean and is dropped in silence on every fire (ces-probes 51) — the slot never
  fills while the model politely re-asks, which reads as a prompting problem.
  """
  out = str(tmp_path / "app")
  flows.build_app(_app(), out)
  tool = sorted(
      t for t in os.listdir(os.path.join(out, "tools"))
      if os.path.isfile(_entry_body(out, t)))[0]
  path = _entry_body(out, tool)
  with open(path, encoding="utf-8") as f:
    src = f.read()
  with open(path, "w", encoding="utf-8") as f:
    f.write(src.replace(f"def {tool}(", f"def not_{tool}(", 1))

  report = _integrity.verify_dir(out)
  assert not report.ok
  assert tool in report.summary()
  assert "never dispatch" in report.summary()


def test_verify_catches_a_missing_return_annotation(tmp_path):
  """An unannotated entry function is never registered (ces-probes 23).

  This is the defect that hit every generated executor in every migrated app: setters
  were annotated and task executors were not, so an agent routed and collected slots
  perfectly and never ran a single task.
  """
  out = str(tmp_path / "app")
  flows.build_app(_app(), out)
  tool = sorted(
      t for t in os.listdir(os.path.join(out, "tools"))
      if os.path.isfile(_entry_body(out, t)))[0]
  path = _entry_body(out, tool)
  with open(path, encoding="utf-8") as f:
    src = f.read()
  stripped = re.sub(rf"def {tool}\(([^)]*)\) -> [^:]+:", rf"def {tool}(\1):", src)
  assert stripped != src, "test seed did not change the signature"
  with open(path, "w", encoding="utf-8") as f:
    f.write(stripped)

  report = _integrity.verify_dir(out)
  assert not report.ok
  assert "no return annotation" in report.summary()


def test_verify_does_not_flag_a_required_parameter(tmp_path):
  """A parameter with no default is a property of the FIRE, not of the tool.

  `21` established that an empty-args fire cannot fill one, but whether a tool is ever
  fired that way depends on the task firing it. Five of the framework's own healthy tools
  declare required parameters, so flagging them here would cry wolf on `slot_filling_engine`
  and the check would stop being read.
  """
  out = str(tmp_path / "app")
  flows.build_app(_app(), out)
  assert _integrity.verify_dir(out).ok
  bodies = [_entry_body(out, t) for t in os.listdir(os.path.join(out, "tools"))]
  assert any("input_data" in open(p, encoding="utf-8").read()
             for p in bodies if os.path.isfile(p))


def _derive(payload):
  from flows.engine import loader as _loader
  return _loader.load_intake()._derive_error_code(payload)


def test_an_mcp_timeout_is_named_a_timeout():
  """A toolset call surfaces its own deadline, and it is a timeout like any other.

  Observed in a live RunSession trace. None of the python-function prefixes match it, so
  before this it fell to `_default` — a latency problem an author could not tell from a
  broken backend.
  """
  out = _derive({"status": "error", "error": (
      "Calling MCP tool 'mapstools_search_places' failed: "
      "java.util.concurrent.TimeoutException: Did not observe any item or terminal "
      "signal within 500ms in 'source(MonoDeferContextual)'")})
  assert out["error_code"] == "timeout"


def test_a_body_that_raises_its_own_timeout_is_still_a_crash():
  """Order matters: a body that RAN and raised is a crash, whatever it raised.

  The platform reports it as `Python function execution failed`, and the tool was not
  stopped from outside. An author wanting finer detail returns their own error_code.
  """
  out = _derive({"status": "error", "error": (
      "Python function execution failed: TimeoutException: upstream slow")})
  assert out["error_code"] == "tool_crash"


def test_an_unrecognized_error_keeps_no_code():
  """Never by elimination — an unmapped shape falls to the caller's `_default`."""
  assert "error_code" not in _derive(
      {"status": "error", "error": "Something nobody has catalogued yet"})


def _chain_config():
  """Two tasks, the first exhausting with `fill` so the second becomes eligible."""
  f = flows.Flow("chain", root_agent="A")
  f.add(flows.user_slot("gate", ask="say something"),
        flows.result_slot("first", "one"),
        flows.result_slot("second", "two"))
  f.task(flows.task("one", "step_one", ["gate"], "first", out_key="note",
                    on_failure={"max_retries": 1,
                                "fill": {"first": "filled by on_failure"},
                                "on_exhaust": {"say": "STEP ONE EXHAUSTED."}}))
  f.task(flows.task("two", "step_two", ["first"], "second", out_key="note",
                    condition=flows.has("first")))
  return f.to_config()


def test_a_task_that_exhausts_with_fill_does_not_fire_again():
  """`fill` says the task is finished; without a write-off it fires forever.

  A failed result is falsy under `success_check`, which is what keeps a task eligible so a
  retry can happen at all. So recording the failure alone left the selector dispatching the
  same task every turn: it failed, exhausted, spoke its disposition, and did it again on the
  next turn with no ladder left. Measured live as a wedged call in ces-probes 114.
  """
  from flows.engine import loader as _loader

  engine, intake = _loader.load_engine(), _loader.load_intake()
  config = _chain_config()
  sm = {"filled": {"gate": "x"}, "pending": {}, "task_results": {}}
  fired = []
  for n in range(1, 5):
    out = engine.slot_filling_engine({
        "raw_config": config, "sm": sm, "last_user_text": "hello",
        "scanned_user_text": "hello", "is_inactivity": False, "event_data": {},
        "config_id": "chain", "n_user_turns": n})
    sm = out.get("sm", sm)
    name = ((out["action"] or {}).get("function_call") or {}).get("name")
    fired.append(name)
    if name == "step_one":
      sm = intake.slot_intake({
          "tool_name": "step_one",
          "response_data": {"status": "error", "error": "Tool execution failed: boom"},
          "sm": sm, "current_agent": "", "channel": ""})["sm"]

  assert sm["filled"]["first"] == "filled by on_failure", "fill must still apply"
  assert "one" in (sm.get("_task_written_off") or []), "the exhausted task is written off"
  assert fired.count("step_one") == 1, (
      f"the exhausted task re-fired and wedged the call: {fired}")
  assert "step_two" in fired, f"the flow never advanced past the failure: {fired}"


def test_abandoning_a_journey_lets_a_written_off_task_run_again():
  """A write-off must not outlive the result it was recorded against.

  `_abandon_journey` clears a touched task's result and retries precisely so a corrected
  input gets another attempt. Leaving the task in `_task_written_off` would make that
  attempt impossible — the caller fixes the value and the task never runs again, which is
  the same wedge as ces-probes 114 wearing a different hat.
  """
  from flows.engine import loader as _loader

  engine = _loader.load_engine()
  config = _chain_config()
  sm = {"filled": {"gate": "x", "first": "filled by on_failure"}, "pending": {},
        "task_results": {"one": {"success": False, "error": "boom"}},
        "_task_written_off": ["one"], "_fanout_written_off": ["one"],
        "_retries": {"one": 1}}

  engine._abandon_journey(sm, config, ["gate"])

  assert "one" not in (sm.get("_task_written_off") or []), (
      "a corrected input must be able to re-run the task")
  assert "one" not in (sm.get("_fanout_written_off") or []), (
      "the fan-out write-off is the same shape and was never cleared either")
  assert "one" not in sm["task_results"], "the result itself is cleared as before"


def test_a_nested_entry_function_does_not_satisfy_the_check(tmp_path):
  """CES calls the module's declared name, so a nested definition is not callable.

  Walking the whole AST would find the nested one, pronounce the tool healthy, and miss
  exactly the silent drop this check exists to catch.
  """
  out = str(tmp_path / "app")
  flows.build_app(_app(), out)
  tool = sorted(
      t for t in os.listdir(os.path.join(out, "tools"))
      if os.path.isfile(_entry_body(out, t)))[0]
  path = _entry_body(out, tool)
  with open(path, encoding="utf-8") as f:
    src = f.read()
  # Bury the real entry inside a wrapper: present in the file, uncallable by CES.
  buried = src.replace(f"def {tool}(", "def _wrapper() -> None:\n  def " + tool + "(", 1)
  with open(path, "w", encoding="utf-8") as f:
    f.write(buried)

  report = _integrity.verify_dir(out)
  assert not report.ok, "a nested entry function must not pass as dispatchable"
  assert tool in report.summary()


# ── #513/#556: a tool whose entry return annotation is a Union incl. a custom class, or
# whose body raises on import, is SILENTLY DROPPED by CES (no function_response, no error,
# dead air). ces-probes/probes/153-tool-return-annotation-drop documents the platform
# defect; these pin the flows-side emit normalization + build-time lint that prevent it.
from pydantic import BaseModel as _BaseModel  # noqa: E402
from flows.authoring import tools as _tools  # noqa: E402


class _Status(_BaseModel):
  status: str = ""


def _lookup_union(x: str = "") -> dict | _Status:   # entry annotation under test (real BinOp)
  """doc."""
  return {"status": x}


def test_union_return_annotation_is_normalized_to_dict():
  """A `-> dict | Model` entry annotation is rewritten to plain `dict` on emit (CES
  registers the tool output from it and silently drops a Union-with-custom-class)."""
  spec = _tools.ToolSpec(name="_lookup_union", func=_lookup_union, flows=())
  src = _tools.render_tool(spec)
  assert ") -> dict:" in src
  assert "| _Status" not in src.split("def _lookup_union", 1)[1].splitlines()[0]


def test_bare_and_plain_return_annotations_are_left_alone():
  """Scoped to Unions: a plain `-> dict` and a bare `-> Model` are untouched."""
  src_plain = "def t(x: str = '') -> dict:\n    return {}\n"
  assert _tools._dict_return_if_union(src_plain, "t") == src_plain
  src_bare = "def t(x: str = '') -> Foo:\n    return {}\n"
  assert _tools._dict_return_if_union(src_bare, "t") == src_bare


def _write_tool(app_dir, name, body):
  tdir = os.path.join(app_dir, "tools", name, "python_function")
  os.makedirs(tdir, exist_ok=True)
  with open(os.path.join(tdir, "python_code.py"), "w", encoding="utf-8") as fh:
    fh.write(body)
  with open(os.path.join(app_dir, "tools", name, f"{name}.json"), "w", encoding="utf-8") as fh:
    json.dump({"name": name, "pythonFunction": {
        "name": name, "pythonCode": f"tools/{name}/python_function/python_code.py"}}, fh)


def test_undispatchable_flags_a_tool_that_raises_on_import(tmp_path):
  """A body that NameErrors on load (self-referential class, no `from __future__`) is
  flagged — CES would otherwise drop it silently (dead air)."""
  app = str(tmp_path / "app")
  _write_tool(app, "bad", (
      "from pydantic import BaseModel\n\n"
      "class Ship(BaseModel):\n"
      "    s: str = ''\n"
      "    @classmethod\n"
      "    def make(cls) -> Ship:\n"        # forward ref, no __future__ -> NameError on load
      "        return cls()\n\n"
      "def bad(x: str = '') -> dict:\n"
      "    return {'ok': True}\n"))
  flags = _integrity.undispatchable_tools(app)
  assert any("bad" in f and "module fails to load" in f for f in flags), flags


def test_undispatchable_passes_a_clean_tool(tmp_path):
  """A body that imports cleanly and has a `-> dict` annotation is not flagged."""
  app = str(tmp_path / "app")
  _write_tool(app, "good", (
      "def good(x: str = '') -> dict:\n"
      "    return {'ok': True}\n"))
  assert _integrity.undispatchable_tools(app) == []


def test_union_normalized_with_multibyte_char_before_annotation():
  """Owl #55319: col_offset is a UTF-8 byte offset — a multibyte char before the
  annotation must not corrupt the splice. The default 'café' (é = 2 bytes) survives."""
  src = 'def t(x: str = "café") -> dict | _Status:\n    return {}\n'
  out = _tools._dict_return_if_union(src, "t")
  assert '"café"' in out and ") -> dict:" in out and "_Status" not in out


def test_string_quoted_union_return_is_normalized():
  """Owl #55320: a string-quoted Union return annotation is also normalized to dict."""
  src = 'def t(x: str = "") -> "dict | _Status":\n    return {}\n'
  out = _tools._dict_return_if_union(src, "t")
  assert ") -> dict:" in out and "_Status" not in out


def test_string_quoted_forward_ref_is_not_flagged(tmp_path):
  """A self-referential class annotation that is STRING-QUOTED loads lazily, so CES does
  NOT drop it — the load-check must pass it (this is the fix we recommend)."""
  app = str(tmp_path / "app")
  _write_tool(app, "quoted", (
      "from pydantic import BaseModel\n\n"
      "class Ship(BaseModel):\n"
      "    s: str = ''\n"
      "    @classmethod\n"
      "    def make(cls) -> 'Ship':\n"
      "        return cls()\n\n"
      "def quoted(x: str = '') -> dict:\n"
      "    return {'ok': True}\n"))
  assert _integrity.undispatchable_tools(app) == []


def test_future_import_makes_forward_ref_safe(tmp_path):
  """`from __future__ import annotations` makes all annotations lazy, so a self-ref is
  not a load failure — the check must pass it."""
  app = str(tmp_path / "app")
  _write_tool(app, "fut", (
      "from __future__ import annotations\n"
      "from pydantic import BaseModel\n\n"
      "class Ship(BaseModel):\n"
      "    @classmethod\n"
      "    def make(cls) -> Ship:\n"
      "        return cls()\n\n"
      "def fut(x: str = '') -> dict:\n"
      "    return {'ok': True}\n"))
  assert _integrity.undispatchable_tools(app) == []
