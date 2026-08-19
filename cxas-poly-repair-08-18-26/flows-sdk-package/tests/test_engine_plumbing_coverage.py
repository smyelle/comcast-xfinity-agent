"""Framework-bundle plumbing: the blessed contract, the import shell, the lint helpers.

Three modules sit between the authoring layer and the vendored framework bytes,
and nothing else in the suite exercises their edges:

* `flows.engine.blessed_source` — the manifest IS the contract. `version()` is
  whatever the committed manifest stamps (with a bootstrap fallback when the file
  is missing/corrupt/stampless), the enumerations must cover every tool on disk
  byte-for-byte and silently drop none, a ONE-byte edit anywhere must surface as
  drift, and the lenient deployed-app check must relax exactly two things
  (cosmetic tool-json `description` rewording, custom callbacks on non-engine
  agents) and nothing more.
* `flows.engine.loader` — the ONLY module that reaches into
  `framework/tools/<tool>/python_function/python_code.py`, loading each tool BY
  PATH under a synthetic module name, plus the typed wrappers that reproduce the
  live `before_model -> after_tool -> before_model` pipeline offline.
* `flows.engine.framework._docstrings` — the vendored docstring/signature parity
  logic the lint rule is built on.

Everything runs fully offline: no LLM, no creds, no network.

Two state hazards, both fenced by autouse fixtures:

1. `blessed_source.write_manifest()` rewrites the TRACKED
   `src/flows/engine/framework/manifest.json`, and a stale manifest makes
   `scaffold.build*` fail with an empty error list. Every writing test runs
   against a `tmp_path` COPY of the framework tree, and a module-scoped fixture
   fails the run if the real file is touched.
2. `loader` holds a process-global module cache and a mutable settings-level
   framework root — and several other modules in this tree set that root at
   IMPORT time and never restore it. The autouse fixture snapshots the root
   (env + settings) and the cache, and puts both back exactly as found, so
   neither this file nor the modules that ran before it are disturbed.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_engine_plumbing_coverage.py
"""

from __future__ import annotations

import ast
import json
import os
import shutil
from pathlib import Path

import pytest

import flows
from flows.engine import blessed_source as bs
from flows.engine import loader
from flows.engine.framework import _docstrings as ds

# Captured at import, before any test can monkeypatch the module attribute.
_REAL_MANIFEST = bs._MANIFEST_PATH  # noqa: SLF001


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _tracked_manifest_is_never_written():
  """Fail loudly if anything in this file rewrites the checked-in manifest."""
  with open(_REAL_MANIFEST, "rb") as f:
    before = f.read()
  yield
  with open(_REAL_MANIFEST, "rb") as f:
    assert f.read() == before, "a test mutated the tracked manifest.json"


@pytest.fixture(autouse=True)
def _isolate_loader_globals():
  """Snapshot/restore every process-global `loader` owns.

  The cache is RESTORED rather than cleared: other suites hold module-level
  references to modules loaded through it, and re-executing the engine would
  hand them a second copy with empty `_RAW_CONFIGS`/`_COMPILED_CONFIGS`.
  Restoring keeps object identity while dropping anything this file added.
  """
  prior_settings = loader._SETTINGS_FRAMEWORK_ROOT  # noqa: SLF001
  prior_env = os.environ.get(loader.ENV_FRAMEWORK_ROOT)
  prior_cache = {k: dict(v) for k, v in loader._MODULE_CACHE.items()}  # noqa: SLF001
  try:
    yield
  finally:
    loader.set_framework_root(prior_settings)
    if prior_env is None:
      os.environ.pop(loader.ENV_FRAMEWORK_ROOT, None)
    else:
      os.environ[loader.ENV_FRAMEWORK_ROOT] = prior_env
    loader._MODULE_CACHE.clear()  # noqa: SLF001
    loader._MODULE_CACHE.update(prior_cache)  # noqa: SLF001


@pytest.fixture
def blessed_copy(tmp_path, monkeypatch):
  """Point `blessed_source` at a byte-copy of the framework tree under tmp_path."""
  root = tmp_path / "framework"
  shutil.copytree(bs._BLESSED_DIR, root,  # noqa: SLF001
                  ignore=shutil.ignore_patterns("__pycache__"))
  monkeypatch.setattr(bs, "_BLESSED_DIR", str(root))
  monkeypatch.setattr(bs, "_CALLBACKS_DIR", str(root / "callbacks"))
  monkeypatch.setattr(bs, "_HOST_CALLBACKS_DIR", str(root / "host_callbacks"))
  monkeypatch.setattr(bs, "_TOOLS_DIR", str(root / "tools"))
  monkeypatch.setattr(bs, "_MANIFEST_PATH", str(root / "manifest.json"))
  return root


def _app_files(agents=("Widget_Agent",)) -> dict:
  """`path -> content` for a well-formed app carrying the framework verbatim."""
  return {f["path"]: f["content"] for f in bs.app_framework_files(list(agents))}


def _agent_json(tools) -> str:
  return json.dumps({"name": "a", "tools": list(tools)})


def _deployed(agents=("Widget_Agent",), engine=True) -> dict:
  """A composed app plus an agent.json per agent (engine-declaring or not)."""
  provided = _app_files(agents)
  for a in agents:
    provided[f"agents/{a}/{a}.json"] = _agent_json(
        ["slot_filling_engine"] if engine else ["set_active_flow"])
  return provided


def _write_app(root: str, provided: dict) -> str:
  for rel, content in provided.items():
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
      f.write(content)
  return root


def _write_tool(root: Path, tool_name: str, source: str) -> Path:
  """Materialize a synthetic framework tool under `root`; return its code path."""
  code_path = root / tool_name / "python_function" / "python_code.py"
  code_path.parent.mkdir(parents=True, exist_ok=True)
  code_path.write_text(source)
  return code_path


def _fake_root(tmp_path: Path) -> Path:
  """A tools root holding the synthetic tools the loader edge cases need."""
  root = tmp_path / "tools"
  _write_tool(root, "widget_dag",
              'def widget_dag():\n  return {"slots": [], "id": "widget"}\n')
  _write_tool(root, "ces_dag",
              'def ces_dag(input_data):\n  return {"slots": [], "ces": True}\n')
  _write_tool(root, "bad_dag", 'def bad_dag():\n  return ["not", "a", "dict"]\n')
  _write_tool(root, "solo_tool", 'def only_public(v=3):\n  return {"v": v}\n')
  _write_tool(root, "ambiguous_tool",
              "def alpha():\n  return 1\n\n\ndef beta():\n  return 2\n")
  _write_tool(root, "no_callable", "VALUE = 1\n")
  _write_tool(root, "kwargs_tool", "def kwargs_tool(**inputs):\n  return inputs\n")
  _write_tool(root, "named_tool",
              'def named_tool(first, second=""):\n  return {"first": first}\n')
  return root


def _build_config() -> dict:
  """A minimal three-slot flow: announce -> ask -> terminal announce."""
  f = flows.Flow(
      "widget_flow",
      root_agent="Widget_Agent",
      bootstrap={"welcome_slot": "welcome"},
  )
  f.add(
      flows.announce("welcome", ["Hi, I can help with widgets."], shared=True),
      flows.user_slot("widget_id", "What is your widget id?"),
      flows.announce("bye", ["Thanks, all set."], requires=["widget_id"], end=True),
  )
  return f.to_config()


# =============================================================================
# blessed_source — version stamp
# =============================================================================


def test_version_is_the_committed_manifest_stamp():
  assert bs.version() == bs.manifest()["version"]
  assert bs.version() == bs._VERSION  # noqa: SLF001  bootstrap has not drifted
  assert flows.blessed_source is bs and flows.version() == bs.version()


def test_version_falls_back_to_the_bootstrap_when_the_manifest_is_missing(
    blessed_copy, monkeypatch):
  monkeypatch.setattr(bs, "_MANIFEST_PATH", str(blessed_copy / "gone.json"))
  with pytest.raises(OSError):
    bs.manifest()
  assert bs.version() == bs._VERSION  # noqa: SLF001
  assert bs._current_version() == bs._VERSION  # noqa: SLF001


def test_version_falls_back_when_the_manifest_is_corrupt_json(blessed_copy):
  (blessed_copy / "manifest.json").write_text("{not json", encoding="utf-8")
  with pytest.raises(ValueError):
    bs.manifest()
  assert bs.version() == bs._VERSION  # noqa: SLF001


def test_version_falls_back_when_the_manifest_carries_no_version_key(blessed_copy):
  (blessed_copy / "manifest.json").write_text('{"files": []}', encoding="utf-8")
  assert bs.manifest() == {"files": []}  # loads fine, just has no stamp
  assert bs.version() == bs._VERSION  # noqa: SLF001


def test_compute_manifest_defaults_to_the_live_version_and_takes_an_override():
  assert bs.compute_manifest()["version"] == bs.version()
  bumped = bs.compute_manifest("9999.01.01+test")
  assert bumped["version"] == "9999.01.01+test"
  assert bumped["files"] == bs.compute_manifest()["files"]  # bytes unchanged


def test_manifest_json_is_byte_identical_to_the_committed_file():
  # So regenerating an unchanged tree is a genuine no-op (nothing to diff).
  with open(_REAL_MANIFEST, encoding="utf-8") as f:
    assert bs.manifest_json() == f.read()
  assert bs.manifest_json().endswith("\n")
  assert json.loads(bs.manifest_json("9999.01.01+x"))["version"] == "9999.01.01+x"


# =============================================================================
# blessed_source — file enumeration
# =============================================================================


def test_framework_tool_files_emits_json_plus_code_for_every_tool_verbatim():
  files = bs.framework_tool_files()
  paths = [f["path"] for f in files]
  assert len(files) == 2 * len(bs._FRAMEWORK_TOOLS)  # noqa: SLF001
  for tool in bs._FRAMEWORK_TOOLS:  # noqa: SLF001
    assert f"tools/{tool}/{tool}.json" in paths
    assert f"tools/{tool}/python_function/python_code.py" in paths
  assert sorted(paths) == bs._framework_file_paths()  # noqa: SLF001
  for f in files:
    assert isinstance(f["content"], str) and f["content"]
    assert f["content"] == bs._read_blessed(f["path"])  # noqa: SLF001


def test_no_tool_directory_is_silently_dropped_from_the_enumeration():
  on_disk = {d for d in os.listdir(bs._TOOLS_DIR)  # noqa: SLF001
             if os.path.isdir(os.path.join(bs._TOOLS_DIR, d))  # noqa: SLF001
             and d != "__pycache__"}
  assert on_disk == set(bs._FRAMEWORK_TOOLS) | {bs._LINTER_TOOL}  # noqa: SLF001


def test_the_design_time_linter_is_on_disk_but_never_enumerated():
  # KNOWN + INTENDED gotcha: validate_dag_config lives in the bundle so the
  # linter can load it, but it is design-time and must never reach an agent.
  assert bs._LINTER_TOOL not in bs._FRAMEWORK_TOOLS  # noqa: SLF001
  paths = [f["path"] for f in bs.framework_tool_files()]
  assert not any(bs._LINTER_TOOL in p for p in paths)  # noqa: SLF001
  assert not any(bs._LINTER_TOOL in e["path"] for e in bs.manifest()["files"])  # noqa: SLF001
  assert hasattr(bs.load_linter(), "validate_dag_config")


def test_callbacks_and_host_callbacks_are_read_verbatim_and_differ():
  engine, host = bs.callbacks(), bs.host_router_callbacks()
  assert sorted(engine) == sorted(bs._CALLBACK_NAMES)  # noqa: SLF001
  assert sorted(host) == sorted(bs._HOST_CALLBACK_NAMES)  # noqa: SLF001
  for name, blob in engine.items():
    assert isinstance(blob, bytes) and blob
    assert blob.decode("utf-8") == bs._read_callback(name)  # noqa: SLF001
  for name, blob in host.items():
    assert blob and blob != engine[name]  # router callbacks are their own thing


def test_every_reader_raises_when_the_bundle_root_is_gone(blessed_copy, monkeypatch):
  gone = str(blessed_copy / "vanished")
  monkeypatch.setattr(bs, "_BLESSED_DIR", gone)
  monkeypatch.setattr(bs, "_CALLBACKS_DIR", os.path.join(gone, "callbacks"))
  monkeypatch.setattr(bs, "_HOST_CALLBACKS_DIR", os.path.join(gone, "host_callbacks"))
  monkeypatch.setattr(bs, "_TOOLS_DIR", os.path.join(gone, "tools"))
  for call in (bs.callbacks, bs.framework_tool_files, bs.host_router_callbacks,
               bs.load_linter, bs.compute_manifest):
    with pytest.raises((OSError, ImportError)):
      call()


# =============================================================================
# blessed_source — the composers
# =============================================================================


def test_callback_rel_path_is_the_canonical_per_agent_location():
  assert bs.callback_rel_path("Widget_Agent", "after_tool") == (
      "agents/Widget_Agent/after_tool_callbacks/after_tool_callbacks_01/"
      "python_code.py")
  for name in bs._CALLBACK_NAMES:  # noqa: SLF001
    p = bs.callback_rel_path("A", name)
    assert p.startswith("agents/A/") and p.endswith("/python_code.py")


def test_app_framework_files_adds_one_callback_set_per_agent():
  n_tools = 2 * len(bs._FRAMEWORK_TOOLS)  # noqa: SLF001
  one, two = bs.app_framework_files(["A"]), bs.app_framework_files(["A", "B"])
  assert len(one) == n_tools + 4
  assert len(two) == n_tools + 8
  paths = {f["path"] for f in two}
  for agent in ("A", "B"):
    for name in bs._CALLBACK_NAMES:  # noqa: SLF001
      assert bs.callback_rel_path(agent, name) in paths
  cbs = bs.callbacks()
  by_path = {f["path"]: f["content"] for f in two}
  for agent in ("A", "B"):
    for name, blob in cbs.items():
      content = by_path[bs.callback_rel_path(agent, name)]
      assert isinstance(content, str)
      assert content == blob.decode("utf-8")


def test_app_framework_files_accepts_a_bare_agent_name():
  assert bs.app_framework_files("A") == bs.app_framework_files(["A"])
  assert bs.framework_files("A") == bs.app_framework_files(["A"])


def test_host_router_files_place_only_the_two_router_callbacks():
  files = bs.host_router_files("Host_Agent")
  assert [f["path"] for f in files] == [
      bs.callback_rel_path("Host_Agent", n) for n in bs._HOST_CALLBACK_NAMES]  # noqa: SLF001
  host = bs.host_router_callbacks()
  for f, name in zip(files, bs._HOST_CALLBACK_NAMES):  # noqa: SLF001
    assert f["content"] == host[name].decode("utf-8")
    assert isinstance(f["content"], str)


# =============================================================================
# blessed_source — byte-exact drift verification
# =============================================================================


def test_a_freshly_composed_app_verifies_clean():
  report = bs.verify_files(_app_files(["A", "B"]))
  assert report.ok and not report.missing and not report.mismatched
  assert report.summary() == "framework in sync"


def test_one_byte_of_tool_code_is_detected_as_drift():
  provided = _app_files()
  path = "tools/slot_filling_engine/python_function/python_code.py"
  provided[path] = provided[path] + " "  # a single added byte
  report = bs.verify_files(provided)
  assert not report.ok
  assert report.mismatched == [path] and not report.missing
  assert report.summary() == f"out of date: {path}"


def test_a_drifted_callback_copy_is_named_per_agent():
  provided = _app_files(["A", "B"])
  drifted = bs.callback_rel_path("B", "before_model")
  provided[drifted] = provided[drifted] + "\n# edit\n"
  assert bs.verify_files(provided).mismatched == [drifted]  # only B's copy


def test_missing_tool_file_and_missing_callback_are_both_reported():
  provided = _app_files()
  del provided["tools/cancel_flow/cancel_flow.json"]
  del provided[bs.callback_rel_path("Widget_Agent", "after_model")]
  report = bs.verify_files(provided)
  assert "tools/cancel_flow/cancel_flow.json" in report.missing
  assert "<callback:after_model>" in report.missing
  assert "missing:" in report.summary()


def test_verify_files_ignores_everything_outside_the_framework_set():
  provided = _app_files()
  provided["agents/Widget_Agent/Widget_Agent.json"] = "{}"
  provided["tools/my_own_tool/my_own_tool.json"] = "{}"
  assert bs.verify_files(provided).ok


def test_drift_report_summary_joins_both_kinds():
  r = bs.DriftReport(ok=False, missing=["m1", "m2"], mismatched=["x1"])
  assert r.summary() == "missing: m1, m2; out of date: x1"
  assert bs.DriftReport(ok=True).summary() == "framework in sync"


# =============================================================================
# blessed_source — the lenient deployed-app check
# =============================================================================


def test_cosmetic_tool_json_description_reword_is_not_staleness():
  provided = _deployed()
  path = "tools/cancel_flow/cancel_flow.json"
  d = json.loads(provided[path])
  d["pythonFunction"]["description"] = "Reworded by a human editor."
  d["description"] = "also reworded"
  provided[path] = json.dumps(d, indent=2)
  assert not bs.verify_files(provided).ok          # byte-exact check flags it
  assert bs.check_deployed_framework(provided).ok  # the lenient one does not


def test_a_load_bearing_tool_json_change_is_staleness():
  provided = _deployed()
  path = "tools/cancel_flow/cancel_flow.json"
  d = json.loads(provided[path])
  d["displayName"] = "renamed"
  provided[path] = json.dumps(d)
  assert bs.check_deployed_framework(provided).mismatched == [path]


def test_unparseable_or_non_object_tool_json_is_staleness():
  for bad in ("{not json", "[1, 2]", "null"):
    provided = _deployed()
    provided["tools/cancel_flow/cancel_flow.json"] = bad
    assert bs.check_deployed_framework(provided).mismatched == [
        "tools/cancel_flow/cancel_flow.json"], bad


def test_tool_code_drift_is_still_staleness():
  provided = _deployed()
  path = "tools/slot_intake/python_function/python_code.py"
  provided[path] += "\n# edited\n"
  assert bs.check_deployed_framework(provided).mismatched == [path]


def test_a_missing_framework_file_is_still_reported_leniently():
  provided = _deployed()
  del provided["tools/try_again/try_again.json"]
  assert bs.check_deployed_framework(provided).missing == [
      "tools/try_again/try_again.json"]


def test_callback_drift_counts_only_on_slot_filling_agents():
  engine_app = _deployed(["Engine_Agent"], engine=True)
  drifted = bs.callback_rel_path("Engine_Agent", "before_model")
  engine_app[drifted] = "# custom\n"
  assert bs.check_deployed_framework(engine_app).mismatched == [drifted]

  router_app = _deployed(["Router_Agent"], engine=False)
  router_app[bs.callback_rel_path("Router_Agent", "before_model")] = "# custom\n"
  assert bs.check_deployed_framework(router_app).ok


def test_norm_tool_json_drops_only_the_cosmetic_description_fields():
  raw = json.dumps({
      "displayName": "cancel_flow",
      "description": "model-facing prose",
      "pythonFunction": {"name": "cancel_flow", "description": "more prose"},
  })
  assert bs._norm_tool_json(raw) == {  # noqa: SLF001
      "displayName": "cancel_flow", "pythonFunction": {"name": "cancel_flow"}}
  assert bs._norm_tool_json("{oops") is None  # noqa: SLF001
  assert bs._norm_tool_json("[1]") is None  # noqa: SLF001
  # A non-dict pythonFunction is left exactly as found (nothing to strip).
  assert bs._norm_tool_json('{"pythonFunction": 7}') == {"pythonFunction": 7}  # noqa: SLF001


def test_tool_json_is_stale_treats_unparseable_sides_as_stale():
  same = json.dumps({"a": 1, "description": "x"})
  reworded = json.dumps({"a": 1, "description": "y"})
  assert bs._tool_json_is_stale(same, reworded) is False  # noqa: SLF001
  assert bs._tool_json_is_stale(json.dumps({"a": 2}), same) is True  # noqa: SLF001
  assert bs._tool_json_is_stale("{broken", same) is True  # noqa: SLF001
  assert bs._tool_json_is_stale(same, "{broken") is True  # noqa: SLF001


def test_slot_filling_agent_dirs_needs_the_engine_tool_in_a_list():
  assert bs._slot_filling_agent_dirs(  # noqa: SLF001
      {"agents/A/A.json": _agent_json(["slot_filling_engine"])}) == {"A"}
  # A string (not a list) of tools does not count.
  assert bs._slot_filling_agent_dirs(  # noqa: SLF001
      {"agents/A/A.json": json.dumps({"tools": "slot_filling_engine"})}) == set()
  # Unparseable agent.json, and paths that are not an agent.json at all.
  assert bs._slot_filling_agent_dirs({"agents/A/A.json": "<<nope>>"}) == set()  # noqa: SLF001
  assert bs._slot_filling_agent_dirs({"A.json": "{}"}) == set()  # noqa: SLF001
  assert bs._slot_filling_agent_dirs(  # noqa: SLF001
      {"agents/A/sub/A.json": _agent_json(["slot_filling_engine"])}) == set()


def test_an_unparseable_agent_json_makes_its_callbacks_unchecked():
  provided = _deployed()
  provided["agents/Widget_Agent/Widget_Agent.json"] = "<<not json>>"
  provided[bs.callback_rel_path("Widget_Agent", "after_tool")] = "# custom\n"
  assert bs.check_deployed_framework(provided).ok


def test_agent_dir_of_returns_none_outside_the_agents_tree():
  assert bs._agent_dir_of("agents/Widget_Agent/x/python_code.py") == "Widget_Agent"  # noqa: SLF001
  assert bs._agent_dir_of("tools/cancel_flow/cancel_flow.json") is None  # noqa: SLF001


# =============================================================================
# blessed_source — drift verification on disk
# =============================================================================


def test_verify_app_dir_is_clean_for_a_scaffolded_app(tmp_path):
  app = _write_app(str(tmp_path / "app"), _deployed(["Engine_Agent"]))
  assert bs.verify_app_dir(app).ok


def test_verify_app_dir_detects_a_one_byte_edit_on_disk(tmp_path):
  app = _write_app(str(tmp_path / "app"), _deployed(["Engine_Agent"]))
  target = os.path.join(app, "tools/resume_flow/python_function/python_code.py")
  with open(target, "a", encoding="utf-8") as f:
    f.write("#")
  assert bs.verify_app_dir(app).mismatched == [
      "tools/resume_flow/python_function/python_code.py"]


def test_verify_app_dir_skips_a_non_engine_agents_custom_callbacks(tmp_path):
  provided = _deployed(["Engine_Agent"])
  provided["agents/Router_Agent/Router_Agent.json"] = _agent_json(["set_active_flow"])
  for name in bs._HOST_CALLBACK_NAMES:  # noqa: SLF001
    provided[bs.callback_rel_path("Router_Agent", name)] = "# custom router\n"
  app = _write_app(str(tmp_path / "app"), provided)
  assert bs.verify_app_dir(app).ok


def test_verify_app_dir_reports_a_missing_framework_file(tmp_path):
  provided = _deployed(["Engine_Agent"])
  del provided["tools/confirm_pending/confirm_pending.json"]
  app = _write_app(str(tmp_path / "app"), provided)
  assert bs.verify_app_dir(app).missing == [
      "tools/confirm_pending/confirm_pending.json"]


def test_verify_app_dir_skips_files_it_cannot_decode(tmp_path):
  app = _write_app(str(tmp_path / "app"), _deployed(["Engine_Agent"]))
  with open(os.path.join(app, "tools", "blob.bin"), "wb") as f:
    f.write(b"\xff\xfe\x00binary")
  os.makedirs(os.path.join(app, "agents", "Bad_Agent"))
  with open(os.path.join(app, "agents", "Bad_Agent", "Bad_Agent.json"), "wb") as f:
    f.write(b"\xff\xfe\x00not-utf8")
  assert bs.verify_app_dir(app).ok


# =============================================================================
# blessed_source — framework.lock and the staleness lint
# =============================================================================


def test_lock_contents_pins_the_version_and_every_app_path():
  lock = json.loads(bs.lock_contents(["A", "B"]))
  assert lock["framework_version"] == bs.version()
  paths = list(lock["files"])
  assert paths == sorted(paths)
  assert len(paths) == 2 * len(bs._FRAMEWORK_TOOLS) + 8  # noqa: SLF001
  for f in bs.app_framework_files(["A", "B"]):
    assert lock["files"][f["path"]] == bs._sha256_text(f["content"])  # noqa: SLF001


def test_lock_contents_accepts_a_bare_agent_name_and_ends_with_a_newline():
  body = bs.lock_contents("A")
  assert body == bs.lock_contents(["A"])
  assert body.endswith("\n")


def test_framework_staleness_is_silent_when_the_lock_is_current(tmp_path):
  (tmp_path / "framework.lock").write_text(bs.lock_contents("A"), encoding="utf-8")
  assert bs.framework_staleness(str(tmp_path)) is None


def test_framework_staleness_warns_when_there_is_no_lock(tmp_path):
  warning = bs.framework_staleness(str(tmp_path))
  assert "no framework.lock" in warning and bs.version() in warning


def test_framework_staleness_warns_when_the_lock_is_unreadable(tmp_path):
  (tmp_path / "framework.lock").write_text("{broken", encoding="utf-8")
  assert bs.framework_staleness(str(tmp_path)) == (
      "framework.lock is unreadable — can't determine framework version")


def test_framework_staleness_warns_when_the_pinned_version_is_behind(tmp_path):
  (tmp_path / "framework.lock").write_text(
      json.dumps({"framework_version": "1999.01.01+old", "files": {}}),
      encoding="utf-8")
  warning = bs.framework_staleness(str(tmp_path))
  assert "1999.01.01+old" in warning and "out of date" in warning
  assert bs.version() in warning and "framework upgrade" in warning


# =============================================================================
# blessed_source — write_manifest (tmp_path COPY ONLY, never the tracked tree)
# =============================================================================


def test_write_manifest_round_trip_detects_then_absorbs_a_one_byte_edit(blessed_copy):
  # 1. the copy starts in sync with itself
  assert bs.manifest() == bs.compute_manifest()
  assert bs.verify_files(_app_files()).ok

  # 2. one byte changes in the bundle -> the committed manifest now drifts
  edited = blessed_copy / "tools" / "try_again" / "python_function" / "python_code.py"
  edited.write_text(edited.read_text(encoding="utf-8") + "#", encoding="utf-8")
  assert bs.manifest() != bs.compute_manifest()
  assert bs.verify_files(_app_files()).mismatched == [
      "tools/try_again/python_function/python_code.py"]

  # 3. rewriting the manifest absorbs the new bytes; it verifies clean again
  assert bs.write_manifest() == bs.version()  # version preserved, not bumped
  assert bs.manifest() == bs.compute_manifest()
  assert bs.verify_files(_app_files()).ok


def test_write_manifest_bumps_the_version_when_asked(blessed_copy):
  assert bs.write_manifest("2099.12.31+promoted") == "2099.12.31+promoted"
  assert bs.version() == "2099.12.31+promoted"
  assert json.loads((blessed_copy / "manifest.json").read_text(
      encoding="utf-8")) == bs.compute_manifest()


def test_write_manifest_output_is_byte_stable(blessed_copy):
  bs.write_manifest()
  first = (blessed_copy / "manifest.json").read_text(encoding="utf-8")
  bs.write_manifest()
  assert (blessed_copy / "manifest.json").read_text(encoding="utf-8") == first
  assert first.endswith("\n") and first == bs.manifest_json()


def test_a_callback_edit_in_the_copy_is_caught_then_rewritten(blessed_copy):
  cb = blessed_copy / "callbacks" / "after_tool.py"
  cb.write_text(cb.read_text(encoding="utf-8") + "\n# tweak\n", encoding="utf-8")
  before = bs.manifest()["callbacks"]["after_tool"]
  assert bs.compute_manifest()["callbacks"]["after_tool"] != before
  bs.write_manifest()
  assert bs.manifest()["callbacks"]["after_tool"] != before
  assert bs.verify_files(_app_files()).ok


# =============================================================================
# blessed_source — design-time linter passthrough + parity helpers
# =============================================================================


def test_lint_config_sorts_available_tools_before_handing_them_over(monkeypatch):
  seen = {}

  class _Fake:
    def validate_dag_config(self, payload):
      seen.update(payload)
      return {"valid": True, "errors": [], "warnings": []}

  monkeypatch.setattr(bs, "load_linter", lambda: _Fake())
  bs.lint_config({"slots": []}, {"z_tool", "a_tool"})
  assert seen["available_tools"] == ["a_tool", "z_tool"]
  assert seen["raw_config"] == {"slots": []}


_PARITY_SRC = '''
def handler(alpha, beta, *rest, **extra):
  """Do a thing.

  Args:
    alpha: the first.
    beta (str): the second.

  Returns:
    Nothing.
  """
  return None


def other(gamma):
  """No Args section."""
'''


def test_docstring_args_reads_the_first_functions_args_section():
  assert bs.docstring_args(_PARITY_SRC) == {"alpha", "beta"}


def test_docstring_args_is_empty_without_a_function_or_an_args_section():
  assert bs.docstring_args("x = 1") == set()
  assert bs.docstring_args("def f(a):\n  pass\n") == set()
  assert bs.docstring_args('def f(a):\n  """Just prose."""\n') == set()


def test_docstring_args_is_empty_on_unparseable_source(caplog):
  with caplog.at_level("WARNING", logger=bs.logger.name):
    assert bs.docstring_args("def broken(:\n") == set()
  assert "docstring_args" in caplog.text


def test_sig_args_from_source_covers_varargs_and_a_named_lookup():
  assert bs.sig_args_from_source(_PARITY_SRC) == {"alpha", "beta", "rest", "extra"}
  assert bs.sig_args_from_source(_PARITY_SRC, "other") == {"gamma"}
  assert bs.sig_args_from_source(_PARITY_SRC, "no_such_function") == set()


def test_sig_args_from_source_drops_self_and_survives_bad_source(caplog):
  assert bs.sig_args_from_source(
      "class C:\n  def m(self, a, b=1):\n    pass\n") == {"a", "b"}
  with caplog.at_level("WARNING", logger=bs.logger.name):
    assert bs.sig_args_from_source("def broken(:\n") == set()
  assert "sig_args_from_source" in caplog.text


def test_the_parity_helpers_agree_on_the_real_blessed_callbacks():
  # These two exist to police docstring/signature parity on blessed code; run
  # them over the real bundle so the pairing is exercised end to end.
  for name, blob in bs.callbacks().items():
    src = blob.decode("utf-8")
    documented, actual = bs.docstring_args(src), bs.sig_args_from_source(src)
    assert actual, name
    assert documented <= actual, name


# =============================================================================
# _docstrings — the vendored parity logic
# =============================================================================


_ARGS_DOC = "\n".join([
    "Summary line.",
    "",
    "Args:",
    "  alpha: the first.",
    "  beta (str): the second.",
    "",
    "  gamma, delta: a grouped entry.",
    "        a wrapped description line: skipped.",
    "  no colon on this line",
    "  Note that this: is prose, not an arg entry.",
    "  *rest: varargs.",
    "  **extra: kwargs.",
    "",
    "Returns:",
    "  epsilon: not an arg.",
])


def test_documented_args_reads_every_entry_shape_and_stops_at_the_next_section():
  assert ds.documented_args(_ARGS_DOC) == [
      "alpha", "beta", "gamma", "delta", "rest", "extra"]


def test_documented_args_returns_none_without_a_docstring_or_args_section():
  assert ds.documented_args(None) is None
  assert ds.documented_args("") is None
  assert ds.documented_args("Just a summary.\n\nReturns:\n  None.\n") is None


def test_documented_args_is_empty_when_the_args_section_has_no_entries():
  assert ds.documented_args("Args:\n") == []
  assert ds.documented_args("Args:\n\n\nReturns:\n  x: y\n") == []


def test_documented_args_honours_the_indent_of_the_args_header():
  # An indented Args: (a nested docstring) sets the base; a line back at or
  # below that indent ends the section.
  doc = "\n".join([
      "  Summary.",
      "",
      "  Args:",
      "    alpha: first.",
      "  Returns:",
      "    beta: not an arg.",
  ])
  assert ds.documented_args(doc) == ["alpha"]


def test_sig_args_covers_every_parameter_kind_and_drops_self():
  node = ds._first_funcdef(  # noqa: SLF001
      ast.parse("def f(a, /, b, c=1, *args, d, e=2, **kw):\n  pass\n"))
  assert ds.sig_args(node) == ["a", "b", "c", "d", "e", "args", "kw"]

  method = ds._first_funcdef(  # noqa: SLF001
      ast.parse("class C:\n  def m(self, x):\n    pass\n"))
  assert ds.sig_args(method) == ["x"]

  classmethod_node = ds._first_funcdef(  # noqa: SLF001
      ast.parse("class C:\n  @classmethod\n  def m(cls, y):\n    pass\n"))
  assert ds.sig_args(classmethod_node) == ["y"]

  assert ds.sig_args(ds._first_funcdef(ast.parse("def f():\n  pass\n"))) == []  # noqa: SLF001


def test_first_funcdef_finds_the_first_by_name_or_position():
  tree = ast.parse("def one():\n  pass\n\n\ndef two(x):\n  pass\n")
  assert ds._first_funcdef(tree).name == "one"  # noqa: SLF001
  assert ds._first_funcdef(tree, "two").name == "two"  # noqa: SLF001
  assert ds._first_funcdef(tree, "three") is None  # noqa: SLF001
  assert ds._first_funcdef(ast.parse("VALUE = 1\n")) is None  # noqa: SLF001


def test_first_funcdef_matches_async_and_nested_definitions():
  assert ds._first_funcdef(  # noqa: SLF001
      ast.parse("async def go(a):\n  pass\n")).name == "go"
  nested = ds._first_funcdef(  # noqa: SLF001
      ast.parse("class C:\n  def inner(self, z):\n    pass\n"), "inner")
  assert ds.sig_args(nested) == ["z"]


# =============================================================================
# loader — framework-root resolution
# =============================================================================


def test_default_framework_root_is_the_packaged_bundle():
  root = loader.default_framework_root()
  assert root.name == "tools" and root.parent.name == "framework"
  assert (root / "slot_filling_engine" / "python_function"
          / "python_code.py").is_file()


def test_resolve_prefers_an_explicit_arg_over_env_and_settings(tmp_path):
  explicit = tmp_path / "explicit"
  explicit.mkdir()
  os.environ[loader.ENV_FRAMEWORK_ROOT] = str(tmp_path / "from_env")
  loader.set_framework_root(str(tmp_path / "from_settings"))
  assert loader.resolve_framework_root(str(explicit)) == explicit.resolve()


def test_resolve_prefers_env_over_settings(tmp_path):
  os.environ[loader.ENV_FRAMEWORK_ROOT] = str(tmp_path / "from_env")
  loader.set_framework_root(str(tmp_path / "from_settings"))
  assert loader.resolve_framework_root() == (tmp_path / "from_env").resolve()


def test_resolve_uses_settings_when_the_env_var_is_unset(tmp_path):
  os.environ.pop(loader.ENV_FRAMEWORK_ROOT, None)
  loader.set_framework_root(str(tmp_path / "from_settings"))
  assert loader.resolve_framework_root() == (tmp_path / "from_settings").resolve()


def test_resolve_falls_back_to_the_packaged_default_when_nothing_is_set(tmp_path):
  # Other suites in this tree set the settings root at import time and never put
  # it back, so the zero-config path is only reachable once both are cleared.
  os.environ.pop(loader.ENV_FRAMEWORK_ROOT, None)
  loader.set_framework_root(str(tmp_path / "from_settings"))
  assert loader.resolve_framework_root() != loader.default_framework_root()
  loader.set_framework_root(None)
  assert loader.resolve_framework_root() == loader.default_framework_root()
  assert loader.resolve_framework_root() is loader._DEFAULT_FRAMEWORK_ROOT  # noqa: SLF001


def test_default_framework_root_ignores_env_and_settings(tmp_path):
  os.environ[loader.ENV_FRAMEWORK_ROOT] = str(tmp_path / "from_env")
  loader.set_framework_root(str(tmp_path / "from_settings"))
  assert loader.default_framework_root() == loader._DEFAULT_FRAMEWORK_ROOT  # noqa: SLF001


def test_resolve_expands_a_user_home_shorthand():
  resolved = loader.resolve_framework_root("~/some_framework_root")
  assert "~" not in str(resolved) and resolved.is_absolute()


def test_framework_root_exists_both_ways(tmp_path):
  assert loader.framework_root_exists(str(loader.default_framework_root())) is True
  assert loader.framework_root_exists(str(tmp_path / "definitely_missing")) is False
  loader.set_framework_root(str(tmp_path / "definitely_missing"))
  assert loader.framework_root_exists() is False
  loader.set_framework_root(str(tmp_path))
  assert loader.framework_root_exists() is True


# =============================================================================
# loader — the typed loaders and the module cache
# =============================================================================


def test_load_engine_validator_intake_and_dag_expose_their_entrypoints():
  root = str(loader.default_framework_root())
  assert callable(loader.load_engine(root).slot_filling_engine)
  validator = loader.load_validator(root)
  assert callable(validator.validate_dag_config)
  assert hasattr(validator, "DagConfigValidator")
  assert hasattr(validator, "CrossConfigValidator")
  assert callable(loader.load_intake(root).slot_intake)
  assert callable(loader.load_dag("cancel_flow", root).cancel_flow)


def test_load_dag_loads_an_arbitrary_tool_by_directory_name(tmp_path):
  module = loader.load_dag("widget_dag", str(_fake_root(tmp_path)))
  assert callable(module.widget_dag)


def test_the_module_cache_returns_the_same_object_until_cleared(tmp_path):
  root = str(_fake_root(tmp_path))
  first = loader.load_dag("widget_dag", root)
  assert loader.load_dag("widget_dag", root) is first
  loader.clear_cache()
  assert loader._MODULE_CACHE == {}  # noqa: SLF001
  assert loader.load_dag("widget_dag", root) is not first


def test_the_cache_is_keyed_per_root(tmp_path):
  root = _fake_root(tmp_path)
  _write_tool(root, "cancel_flow",
              'def cancel_flow(reason=""):\n  return {"fake": True}\n')
  real_root = str(loader.default_framework_root())
  real = loader.load_dag("cancel_flow", real_root)
  fake = loader.load_dag("cancel_flow", str(root))
  assert real is not fake
  assert fake.cancel_flow() == {"fake": True}
  assert loader.load_dag("cancel_flow", real_root) is real


def test_clear_cache_is_safe_when_already_empty():
  loader.clear_cache()
  loader.clear_cache()
  assert loader._MODULE_CACHE == {}  # noqa: SLF001


# =============================================================================
# loader — error paths
# =============================================================================


def test_a_missing_tool_raises_filenotfound_naming_the_expected_path(tmp_path):
  root = _fake_root(tmp_path)
  with pytest.raises(FileNotFoundError) as exc:
    loader.load_dag("no_such_tool", str(root))
  assert "no_such_tool" in str(exc.value)
  assert "python_code.py" in str(exc.value)


def test_a_nonexistent_framework_root_raises_filenotfound(tmp_path):
  with pytest.raises(FileNotFoundError):
    loader.load_engine(str(tmp_path / "not_a_root"))


def test_a_tool_dir_without_python_code_raises_filenotfound(tmp_path):
  root = tmp_path / "tools"
  (root / "hollow_tool" / "python_function").mkdir(parents=True)
  with pytest.raises(FileNotFoundError):
    loader.load_dag("hollow_tool", str(root))


def test_an_unloadable_spec_raises_importerror(tmp_path, monkeypatch):
  root = _fake_root(tmp_path)
  monkeypatch.setattr(
      loader.importlib.util, "spec_from_file_location", lambda *a, **k: None)
  with pytest.raises(ImportError) as exc:
    loader.load_dag("widget_dag", str(root))
  assert "import spec" in str(exc.value)


# =============================================================================
# loader — load_tool_callable / tool_parameters / tool_signature
# =============================================================================


def test_load_tool_callable_prefers_the_eponymous_function():
  fn = loader.load_tool_callable(
      "cancel_flow", str(loader.default_framework_root()))
  assert fn(reason="the caller asked")["cancelled"] is True


def test_load_tool_callable_falls_back_to_the_single_public_callable(tmp_path):
  root = str(_fake_root(tmp_path))
  assert loader.load_tool_callable("solo_tool", root)() == {"v": 3}


def test_load_tool_callable_rejects_ambiguous_and_callable_less_modules(tmp_path):
  root = str(_fake_root(tmp_path))
  with pytest.raises(AttributeError) as exc:
    loader.load_tool_callable("ambiguous_tool", root)
  assert "2 candidates" in str(exc.value)
  with pytest.raises(AttributeError) as exc:
    loader.load_tool_callable("no_callable", root)
  assert "0 candidates" in str(exc.value)


def test_tool_parameters_reports_the_callables_own_names(tmp_path):
  root = str(_fake_root(tmp_path))
  assert loader.tool_parameters("named_tool", root) == ["first", "second"]
  # A config names the SLOT, which is not always the parameter — hence the ask.
  assert loader.tool_parameters("kwargs_tool", root) == ["inputs"]


def test_tool_signature_tells_kwargs_apart_from_a_plain_parameter(tmp_path):
  import inspect

  root = str(_fake_root(tmp_path))
  kwargs_sig = loader.tool_signature("kwargs_tool", root)
  assert list(kwargs_sig) == ["inputs"]
  assert kwargs_sig["inputs"].kind is inspect.Parameter.VAR_KEYWORD
  named_sig = loader.tool_signature("named_tool", root)
  assert named_sig["first"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
  assert named_sig["second"].default == ""


# =============================================================================
# loader — load_config
# =============================================================================


def test_load_config_accepts_a_bare_id_and_the_full_tool_name(tmp_path):
  root = str(_fake_root(tmp_path))
  assert loader.load_config("widget", root) == {"slots": [], "id": "widget"}
  assert loader.load_config("widget_dag", root) == {"slots": [], "id": "widget"}


def test_load_config_tolerates_a_ces_style_single_arg_dag(tmp_path):
  assert loader.load_config("ces", str(_fake_root(tmp_path)))["ces"] is True


def test_load_config_rejects_a_dag_that_does_not_return_a_dict(tmp_path):
  with pytest.raises(TypeError) as exc:
    loader.load_config("bad", str(_fake_root(tmp_path)))
  assert "did not return a dict" in str(exc.value)


def test_load_config_propagates_a_missing_dag_tool(tmp_path):
  with pytest.raises(FileNotFoundError):
    loader.load_config("ghost", str(_fake_root(tmp_path)))


# =============================================================================
# loader — evict_raw_configs
# =============================================================================


def test_evict_raw_configs_noop_on_an_empty_list_does_not_load_the_engine(tmp_path):
  # A bogus root would raise on load; the empty-list guard must return first.
  loader.evict_raw_configs([], str(tmp_path / "not_a_root"))


def test_evict_raw_configs_prefers_an_engine_evict_configs_api(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "slot_filling_engine", (
      "SEEN = []\n"
      "_RAW_CONFIGS = {'a': 1}\n"
      "def evict_configs(config_ids):\n"
      "  SEEN.append(list(config_ids))\n"))
  loader.evict_raw_configs(["a"], str(root))
  engine = loader.load_engine(str(root))
  assert engine.SEEN == [["a"]]
  assert engine._RAW_CONFIGS == {"a": 1}  # the preferred API short-circuits


def test_evict_raw_configs_falls_back_to_engine_evict_raw_configs(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "slot_filling_engine", (
      "SEEN = []\n"
      "def evict_raw_configs(config_ids):\n"
      "  SEEN.append(list(config_ids))\n"))
  loader.evict_raw_configs(["b", "c"], str(root))
  assert loader.load_engine(str(root)).SEEN == [["b", "c"]]


def test_evict_raw_configs_reaches_into_the_private_caches(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "slot_filling_engine", (
      "_RAW_CONFIGS = {'keep': 1, 'drop': 2}\n"
      "_COMPILED_CONFIGS = {('drop', 'fp1'): 1, ('keep', 'fp2'): 2, 'odd': 3}\n"))
  loader.evict_raw_configs(["drop"], str(root))
  engine = loader.load_engine(str(root))
  assert engine._RAW_CONFIGS == {"keep": 1}
  # Compiled entries are keyed (config_id, fingerprint); only `drop` goes, and a
  # non-tuple key is left untouched.
  assert set(engine._COMPILED_CONFIGS) == {("keep", "fp2"), "odd"}


def test_evict_raw_configs_warns_loudly_when_the_cache_api_is_gone(tmp_path, caplog):
  root = tmp_path / "tools"
  _write_tool(root, "slot_filling_engine", "VALUE = 1\n")
  with caplog.at_level("WARNING", logger=loader.logger.name):
    loader.evict_raw_configs(["x"], str(root))
  assert "evict_raw_configs" in caplog.text
  assert "cache API may have changed" in caplog.text


# =============================================================================
# loader — seed_sm
# =============================================================================


def test_seed_sm_shape_from_a_built_config():
  sm = loader.seed_sm(_build_config())
  assert sm["_config_id"] == "slot_studio"
  assert sm["task_results"] == {}
  assert sm["_bootstrap"] == {"welcome_slot": "welcome"}
  assert sm["_cancel_tool"] == ""
  assert sm["_gate_slot"] is None
  assert sm["_setter_slots"] == {"set_widget_id": "widget_id"}
  assert sm["_multi_setter_slots"] == {}
  assert sm["_slot_requires"] == {"bye": ["widget_id"]}
  assert sm["_slot_validates"] == {}
  assert sm["_executor_tasks"] == {}


def test_seed_sm_seeds_in_place_and_is_idempotent():
  sm = {"channel": "audio"}
  assert loader.seed_sm(_build_config(), sm) is sm
  assert sm["channel"] == "audio"
  sm["filled"] = {"widget_id": "W-1"}
  again = loader.seed_sm({"slots": [{"name": "other", "source": "user"}]}, sm)
  assert again is sm
  assert again["filled"] == {"widget_id": "W-1"}
  assert again["_setter_slots"] == {"set_widget_id": "widget_id"}  # not re-derived


def test_seed_sm_registers_the_cancel_setter_and_the_gate_slot():
  config = {
      "bootstrap": {"tool": "set_active_flow", "slot": "flow"},
      "gate_slot": "flow",
      "cancel": {"tool": "cancel_flow", "say": "Okay, cancelled."},
      "slots": [{"name": "widget_id", "source": "user", "setter": "set_widget_id"}],
  }
  sm = loader.seed_sm(config)
  assert sm["_gate_slot"] == "flow"
  assert sm["_cancel_tool"] == "cancel_flow"
  assert sm["_setter_slots"] == {
      "set_widget_id": "widget_id",
      "cancel_flow": loader._CANCEL_SLOT,  # noqa: SLF001
  }


def test_seed_sm_groups_multi_field_setters_and_records_requires_validates():
  config = {
      "slots": [
          {"name": "street", "source": "user", "setter": "set_address",
           "setter_field": "street"},
          {"name": "zip_code", "source": "user", "setter": "set_address",
           "setter_field": "zip"},
          {"name": "plan", "source": "user", "setter": "set_plan",
           "requires": ["street"], "validate_against": ["basic", "pro"]},
          {"name": "no_setter", "source": "announce"},
      ],
  }
  sm = loader.seed_sm(config)
  assert sm["_multi_setter_slots"] == {
      "set_address": {"street": "street", "zip": "zip_code"}}
  assert sm["_setter_slots"] == {"set_plan": "plan"}
  assert sm["_slot_requires"] == {"plan": ["street"]}
  assert sm["_slot_validates"] == {"plan": ["basic", "pro"]}


def test_seed_sm_builds_executor_tasks_including_terminal_and_remote_metadata():
  config = {
      "slots": [],
      "remote_tools": {"start_job": {"status_tool": "poll_job", "kind": "async"}},
      "tasks": [
          {"name": "lookup", "tool": "lookup_widget", "inputs": ["widget_id"],
           "outputs": {"status": "widget_status"}},
          {"name": "finish", "tool": "submit_order", "terminal": True,
           "then_say": "You're all set.", "then_response": [{"type": "text"}]},
          {"name": "kickoff", "tool": "start_job"},
          {"name": "toolless"},
      ],
  }
  sm = loader.seed_sm(config)
  tasks = sm["_executor_tasks"]
  assert set(tasks) == {"lookup_widget", "submit_order", "start_job", "poll_job"}
  assert tasks["lookup_widget"] == {
      "task_name": "lookup", "inputs": ["widget_id"],
      "outputs": {"status": "widget_status"}, "success_check": "success",
      "terminal": False,
  }
  assert "then_say" not in tasks["lookup_widget"]
  assert tasks["submit_order"]["then_say"] == "You're all set."
  assert tasks["submit_order"]["then_response"] == [{"type": "text"}]
  # A remote task is answered by TWO tools and both resolve to the same task.
  assert tasks["start_job"]["remote"] == {"status_tool": "poll_job", "kind": "async"}
  assert tasks["poll_job"]["remote_status"] is True
  assert tasks["poll_job"]["task_name"] == "kickoff"


# =============================================================================
# loader — run_engine / run_intake round trip
# =============================================================================


def test_run_engine_asks_then_intake_then_run_engine_completes_the_flow():
  config = _build_config()
  sm = loader.seed_sm(config)
  turn1 = loader.run_engine(config, sm, config_id="plumbing_cov_rt")
  assert set(turn1) == {"action", "sm"}
  assert turn1["action"]["message"] == "What is your widget id?"
  assert turn1["sm"] is sm  # the engine mutates in place AND returns it
  assert sm["_awaiting"] == "widget_id"

  intake = loader.run_intake(
      "set_widget_id", {"stored": True, "value": "W-42"}, turn1["sm"])
  assert set(intake) == {"sm", "transfer_slots", "pending_transfer"}
  assert intake["sm"]["pending"] == {"widget_id": "W-42"}

  turn2 = loader.run_engine(config, intake["sm"], config_id="plumbing_cov_rt")
  assert turn2["sm"]["filled"]["widget_id"] == "W-42"
  assert turn2["sm"]["filled"]["bye"] is True  # the terminal announce fired
  loader.evict_raw_configs(["plumbing_cov_rt"])


def test_run_engine_builds_the_input_data_the_engine_expects(tmp_path):
  """The loader's whole job here is assembling `input_data`; echo it back."""
  root = tmp_path / "tools"
  _write_tool(root, "slot_filling_engine", (
      "def slot_filling_engine(input_data):\n"
      "  return {'action': dict(input_data), 'sm': input_data['sm']}\n"))
  sm, config = {"seeded": True}, {"slots": []}
  echoed = loader.run_engine(
      config, sm, last_user_text="hi there", event_data={"widget_id": "W-9"},
      config_id="plumbing_echo", framework_root=str(root),
      configs={"child_flow": {"slots": []}}, is_inactivity=True,
      scanned_user_text="hi there, again", n_user_turns=3)["action"]
  assert echoed == {
      "raw_config": config,
      "sm": sm,
      "last_user_text": "hi there",
      "is_inactivity": True,
      "event_data": {"widget_id": "W-9"},
      "config_id": "plumbing_echo",
      "n_user_turns": 3,
      "scanned_user_text": "hi there, again",
      "configs": {"child_flow": {"slots": []}},
  }


def test_run_engine_defaults_omit_configs_and_scanned_text(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "slot_filling_engine", (
      "def slot_filling_engine(input_data):\n"
      "  return {'action': dict(input_data), 'sm': input_data['sm']}\n"))
  echoed = loader.run_engine({"slots": []}, {}, framework_root=str(root))["action"]
  assert "configs" not in echoed and "scanned_user_text" not in echoed
  assert echoed["event_data"] == {}
  assert echoed["last_user_text"] == ""
  assert echoed["is_inactivity"] is False
  assert echoed["config_id"] == "slot_studio"
  assert echoed["n_user_turns"] == 0


def test_run_intake_passes_the_async_outcome_through(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "slot_intake", (
      "def slot_intake(input_data):\n"
      "  return {'sm': input_data, 'transfer_slots': [], "
      "'pending_transfer': None}\n"))
  out = loader.run_intake(
      "poll_job", {"done": True}, {"filled": {}}, current_agent="Widget_Agent",
      channel="audio", framework_root=str(root), outcome="completed")
  assert out["sm"] == {
      "tool_name": "poll_job",
      "response_data": {"done": True},
      "sm": {"filled": {}},
      "current_agent": "Widget_Agent",
      "channel": "audio",
      "outcome": "completed",
  }


# =============================================================================
# loader — call_setter and the context shim
# =============================================================================


def test_call_setter_runs_the_real_tool_and_drops_none_args():
  root = str(loader.default_framework_root())
  result = loader.call_setter("cancel_flow", {"reason": "done", "unset": None}, root)
  assert result["cancelled"] is True and result["reason"] == "done"
  assert loader.call_setter("cancel_flow", {}, root)["cancelled"] is True
  assert loader.call_setter("cancel_flow", None, root)["cancelled"] is True


def test_call_setter_wraps_a_non_dict_return(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "scalar_setter",
              'def scalar_setter(x=""):\n  return "plain-string"\n')
  assert loader.call_setter("scalar_setter", {"x": "a"}, str(root)) == {
      "result": "plain-string"}


def test_call_setter_on_an_unknown_tool_raises(tmp_path):
  with pytest.raises(FileNotFoundError):
    loader.call_setter("set_nothing_at_all", {}, str(_fake_root(tmp_path)))


def test_call_setter_binds_context_and_writes_land_in_the_session_state(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "reads_context", (
      "def reads_context(v=''):\n"
      "  context.state['last_v'] = v\n"
      "  context.variables['digits'] = v[-4:]\n"
      "  return {'stored': True, 'value': v, 'sm_seen': 'filled' in "
      "context.state['sm']}\n"))
  session, sm = {}, {"filled": {}}
  out = loader.call_setter(
      "reads_context", {"v": "555-0100"}, str(root), sm=sm, state=session)
  assert out == {"stored": True, "value": "555-0100", "sm_seen": True}
  # The write outlives the call, so the next turn's tool can read it back.
  assert session["last_v"] == "555-0100"
  assert session["_ces_variables"] == {"digits": "0100"}
  assert session["sm"] is sm
  # The shim is removed again when the module had no prior binding.
  module = loader._load_module("reads_context", str(root))  # noqa: SLF001
  assert "context" not in module.__dict__


def test_call_setter_restores_a_pre_existing_context_binding(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "reads_context",
              "def reads_context(v=''):\n  return {'value': v}\n")
  module = loader._load_module("reads_context", str(root))  # noqa: SLF001
  sentinel = object()
  module.__dict__["context"] = sentinel
  try:
    assert loader.call_setter(
        "reads_context", {"v": "x"}, str(root), sm={"filled": {}}) == {"value": "x"}
    assert module.__dict__["context"] is sentinel
  finally:
    module.__dict__.pop("context", None)


def test_call_setter_wraps_a_non_dict_return_under_the_context_binding(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "odd_setter", "def odd_setter():\n  return 7\n")
  assert loader.call_setter("odd_setter", {}, str(root), sm={}) == {"result": 7}


def test_the_context_shim_threads_sm_without_making_it_self_referential():
  sm = {"filled": {}}
  shim = loader._ContextShim(sm)  # noqa: SLF001
  assert shim.state["sm"] is sm
  assert shim.variables == {}
  assert "context" not in sm and "state" not in sm  # a deep copy stays finite
  session = {"prior": 1}
  shared = loader._ContextShim(sm, session)  # noqa: SLF001
  assert shared.state is session and session["sm"] is sm


# =============================================================================
# loader — call_readback_tool
# =============================================================================


def test_call_readback_tool_requires_an_eponymous_callable(tmp_path):
  root = str(_fake_root(tmp_path))
  with pytest.raises(AttributeError) as exc:
    loader.call_readback_tool("no_callable", {}, root)
  assert "no_callable" in str(exc.value)


def test_call_readback_tool_wraps_a_non_dict_return(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "odd_readback", "def odd_readback():\n  return 7\n")
  assert loader.call_readback_tool("odd_readback", {}, str(root)) == {"result": 7}


def test_call_readback_tool_restores_a_pre_existing_context_binding(tmp_path):
  root = tmp_path / "tools"
  _write_tool(root, "odd_readback", "def odd_readback():\n  return {'ok': True}\n")
  module = loader._load_module("odd_readback", str(root))  # noqa: SLF001
  sentinel = object()
  module.__dict__["context"] = sentinel
  try:
    assert loader.call_readback_tool("odd_readback", {}, str(root)) == {"ok": True}
    assert module.__dict__["context"] is sentinel
  finally:
    module.__dict__.pop("context", None)


# =============================================================================
# loader — derive_visible_setters
# =============================================================================


def test_derive_visible_setters_on_a_built_config():
  visible, hidden = loader.derive_visible_setters(_build_config(), [])
  assert visible == [{"tool": "set_widget_id", "fields": ["widget_id"]}]
  assert hidden == []


def test_derive_visible_setters_splits_on_hide_tools():
  config = {
      "slots": [
          {"name": "widget_id", "source": "user", "setter": "set_widget_id"},
          {"name": "plan", "source": "user", "setter": "set_plan"},
      ],
  }
  visible, hidden = loader.derive_visible_setters(config, ["set_plan"])
  assert visible == [{"tool": "set_widget_id", "fields": ["widget_id"]}]
  assert hidden == [{"tool": "set_plan", "reason": "hidden_by_engine"}]


def test_derive_visible_setters_groups_multi_field_setters_without_duplicates():
  config = {
      "slots": [
          {"name": "street", "source": "user", "setter": "set_address",
           "setter_field": "street"},
          {"name": "zip_code", "source": "user", "setter": "set_address",
           "setter_field": "zip"},
          {"name": "zip_again", "source": "user", "setter": "set_address",
           "setter_field": "zip"},
          {"name": "no_setter", "source": "announce"},
      ],
  }
  assert loader.derive_visible_setters(config, []) == (
      [{"tool": "set_address", "fields": ["street", "zip"]}], [])


def test_derive_visible_setters_adds_the_gate_and_cancel_tools():
  config = {
      "bootstrap": {"tool": "set_active_flow", "slot": "flow"},
      "cancel": {"tool": "cancel_flow"},
      "slots": [{"name": "widget_id", "source": "user", "setter": "set_widget_id"}],
  }
  visible, _ = loader.derive_visible_setters(config, [])
  by_tool = {v["tool"]: v["fields"] for v in visible}
  assert by_tool["set_active_flow"] == ["flow"]
  assert by_tool["cancel_flow"] == []


def test_derive_visible_setters_defaults_the_gate_field_and_never_clobbers_a_slot():
  assert loader.derive_visible_setters(
      {"bootstrap": {"tool": "set_active_flow"}, "slots": []}, []) == (
          [{"tool": "set_active_flow", "fields": ["flow"]}], [])
  config = {
      "bootstrap": {"tool": "set_active_flow", "slot": "flow"},
      "slots": [{"name": "flow_choice", "source": "user",
                 "setter": "set_active_flow"}],
  }
  visible, _ = loader.derive_visible_setters(config, [])
  assert visible == [{"tool": "set_active_flow", "fields": ["flow_choice"]}]


def test_derive_visible_setters_handles_an_empty_config_and_none_hide_tools():
  assert loader.derive_visible_setters({}, None) == ([], [])
  assert loader.derive_visible_setters({"slots": [], "cancel": None}, []) == ([], [])


# =============================================================================
# loader — deep_copy_sm
# =============================================================================


def test_deep_copy_sm_isolates_every_nested_mutation():
  sm = {
      "filled": {"widget_id": "W-1", "tags": ["a", "b"]},
      "pending": {},
      "task_results": {"lookup": {"status": {"code": 200}}},
      "_log": [{"tag": "seed"}],
  }
  copied = loader.deep_copy_sm(sm)
  assert copied == sm and copied is not sm

  copied["filled"]["widget_id"] = "W-999"
  copied["filled"]["tags"].append("c")
  copied["task_results"]["lookup"]["status"]["code"] = 500
  copied["_log"].append({"tag": "mutated"})
  copied["pending"]["new"] = True

  assert sm["filled"] == {"widget_id": "W-1", "tags": ["a", "b"]}
  assert sm["task_results"] == {"lookup": {"status": {"code": 200}}}
  assert sm["_log"] == [{"tag": "seed"}]
  assert sm["pending"] == {}
  assert loader.deep_copy_sm({}) == {}


def test_deep_copy_sm_of_a_driven_sm_is_a_true_snapshot():
  config = _build_config()
  sm = loader.seed_sm(config)
  turn1 = loader.run_engine(config, sm, config_id="plumbing_cov_snap")
  live = loader.run_intake(
      "set_widget_id", {"stored": True, "value": "W-7"}, turn1["sm"])["sm"]
  snapshot = loader.deep_copy_sm(live)
  # Committing the pending value must not reach back into the snapshot.
  loader.run_engine(config, live, config_id="plumbing_cov_snap")
  assert live["filled"]["widget_id"] == "W-7"
  assert live["pending"] == {}
  assert snapshot["pending"] == {"widget_id": "W-7"}
  assert "widget_id" not in snapshot["filled"]
  loader.evict_raw_configs(["plumbing_cov_snap"])
