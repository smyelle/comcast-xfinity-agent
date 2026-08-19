"""`flows.deploy` — the gates, the orchestration and the CLI wrapper, offline.

This file extends `test_deploy.py` rather than repeating it: that one drives the
service end to end through a faked runner; this one goes after the parts of the
deploy layer that a happy-path push never reaches — every pre-push gate in BOTH
directions, the local (on-disk) halves of those gates, the interpreter probe, and
each refusal branch in `service.push` / `flows.deploy.push.deploy`.

NOTHING here touches a network, a GCP project or a real `cxas`. Every outside edge
is a declared seam and every test goes through it:

  * the push subprocess          -> `runner.set_runner(...)`      (the `fake_cxas` fixture)
  * the interpreter probe        -> `runner.subprocess.run`       (monkeypatched)
  * the `cxas` CLI in `push.py`  -> `push_cli._run`               (monkeypatched)
  * the on-disk framework/app    -> `DeployEnv(agent_dir=..., framework_root=...)`
                                    pointed at `tmp_path`

A gate gets tested in both directions on purpose. A gate that stops firing is
invisible — the push it should have blocked just succeeds — and a gate that fires on
a valid app blocks every release until someone deletes it. Only asserting the
failing direction catches exactly one of those two.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import types
from pathlib import Path

import pytest

from flows.config import models
from flows.deploy import argv as argv_mod
from flows.deploy import gates, render, runner, service
from flows.deploy import push as push_cli
from flows.deploy.env import DeployEnv
from flows.deploy.errors import DagUnresolvedError, RenderFailedError
from flows.deploy.models import (
    PrePushCheck,
    PrePushReport,
    PrePushRequest,
    PushConfigEntry,
    PushSpec,
)
from flows.emit.models import ScaffoldFile
from flows.engine import blessed_source


# ======================================================================================
# Fixtures / builders
# ======================================================================================

def _code(tool: str) -> str:
    return f"tools/{tool}/python_function/python_code.py"


_STORED_SETTER = (
    'def set_pin(pin):\n'
    '    """Store the pin.\n'
    '\n'
    '    Args:\n'
    '        pin: the caller\'s pin.\n'
    '    """\n'
    '    return {"stored": True, "value": pin}\n'
)


def _config(slot: str = "pin", setter: str = "set_pin") -> models.Config:
    return models.Config.model_validate(
        {
            "slots": [
                {"name": slot, "source": "user", "setter": setter,
                 "ask": f"what is your {slot}?", "hint": slot}
            ],
            "tasks": [],
        }
    )


def _dag_source(dag: str, config: models.Config) -> str:
    """The rendered `*_dag` python for `config` — what the app actually ships."""
    return render.render_one(dag, config)


def _callback_files(agent: str = "Main", drift: str | None = None) -> list[ScaffoldFile]:
    """One agent's four framework callbacks, blessed-identical unless `drift` names one."""
    out: list[ScaffoldFile] = []
    for cb, data in blessed_source.callbacks().items():
        content = data.decode("utf-8")
        if cb == drift:
            content += "\n# hand-edited\n"
        out.append(ScaffoldFile(
            path=f"agents/{agent}/{cb}_callbacks/{cb}_callbacks_01/python_code.py",
            content=content,
        ))
    return out


def _tool_json(tool: str, uuid_name: str) -> ScaffoldFile:
    return ScaffoldFile(
        path=f"tools/{tool}/{tool}.json",
        content=json.dumps({"name": uuid_name, "displayName": tool}),
    )


def _healthy_app(config: models.Config | None = None) -> list[ScaffoldFile]:
    """A whole-app payload that every gate should pass."""
    cfg = config or _config()
    return [
        ScaffoldFile(path="app.json", content="{}"),
        ScaffoldFile(path=_code("f_dag"), content=_dag_source("f_dag", cfg)),
        _tool_json("f_dag", "11111111-1111-1111-1111-111111111111"),
        ScaffoldFile(path=_code("set_pin"), content=_STORED_SETTER),
        _tool_json("set_pin", "22222222-2222-2222-2222-222222222222"),
        *_callback_files(),
    ]


def _fmap(files) -> dict[str, str]:
    return {f.path: f.content for f in files}


class FakeRunner:
    """Stands in for `cxas`. Records argv; returns a canned (rc, stdout, stderr)."""

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = ""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.calls: list[list[str]] = []

    async def __call__(self, argv_):
        self.calls.append(list(argv_))
        return self.rc, self.stdout, self.stderr


@pytest.fixture()
def fake_cxas():
    """Install a fake push runner and restore the default afterwards.

    The default runner shells out; leaking one of these would let a later test try a
    real deploy. Restoring it is the entire point of the fixture.
    """
    r = FakeRunner(stdout="Successfully pushed to: projects/p/locations/us/apps/abc123")
    runner.set_runner(r)
    try:
        yield r
    finally:
        runner.set_runner(runner.default_push_runner)


@pytest.fixture()
def clean_cxas_probe():
    """Reset (and restore) the module-global interpreter-probe cache.

    `resolve_cxas_python` memoizes, so a test that leaves a value behind decides the
    answer for every test after it.
    """
    saved = runner._CXAS_PY
    runner._CXAS_PY = None
    try:
        yield
    finally:
        runner._CXAS_PY = saved


def _green_gates(_req) -> PrePushReport:
    return PrePushReport(ok=True, checks=[], target="create", target_label="x")


# ======================================================================================
# gates: assembling the effective tree (hosted payload vs on-disk)
# ======================================================================================

def test_a_hosted_payload_is_read_as_a_path_to_content_map():
    """Both shapes the wire delivers — pydantic ScaffoldFiles and plain dicts — have to
    land in the same map, because every gate below indexes it by path."""
    req = types.SimpleNamespace(app_files=[
        ScaffoldFile(path="a.py", content="A"),
        {"path": "b.py", "content": "B"},
        {"path": "c.py", "content": None},  # a null body is "", never a KeyError
        {"content": "orphan"},              # no path at all -> dropped
    ])
    assert gates._files_from_app_files(req) == {"a.py": "A", "b.py": "B", "c.py": ""}


def test_no_payload_means_local_mode_not_an_empty_app():
    """None and {} are different answers: None sends the gates to disk, {} would tell
    them the app is empty and fail checks that read it."""
    assert gates._files_from_app_files(types.SimpleNamespace(app_files=None)) is None
    assert gates._files_from_app_files(types.SimpleNamespace(app_files=[])) is None
    assert gates._files_from_app_files(types.SimpleNamespace()) is None


def test_the_local_app_root_prefers_the_agent_dir(tmp_path):
    assert gates._app_root(DeployEnv(agent_dir=str(tmp_path))) == tmp_path


def test_the_local_app_root_falls_back_to_the_parent_of_the_tools_dir(tmp_path):
    """`framework_root` points at `tools/`; the app root is its parent. Getting this
    wrong makes the callback check look for `agents/` inside `tools/` and find none."""
    tools = tmp_path / "tools"
    tools.mkdir()
    assert gates._app_root(DeployEnv(framework_root=str(tools))) == tmp_path
    other = tmp_path / "custom"
    other.mkdir()
    assert gates._app_root(DeployEnv(framework_root=str(other))) == other


def test_the_tools_dir_resolves_from_the_framework_root_then_the_app_root(
        tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    (tools / "set_pin").mkdir(parents=True)
    assert gates._tools_dir(DeployEnv(framework_root=str(tools))) == tools

    # With no resolvable framework root, fall back to <agent_dir>/tools.
    monkeypatch.setattr(
        gates.framework_bridge, "resolve_framework_root",
        lambda _root=None: Path(tmp_path / "does-not-exist"))
    assert gates._tools_dir(DeployEnv(agent_dir=str(tmp_path))) == tools
    assert gates._tools_dir(DeployEnv()) is None


def test_a_tools_dir_that_cannot_be_resolved_is_not_an_exception(tmp_path, monkeypatch):
    """The gates never raise: a resolver blowing up has to read as "no local tree"."""
    def _boom(_root=None):
        raise RuntimeError("no framework root")

    monkeypatch.setattr(gates.framework_bridge, "resolve_framework_root", _boom)
    assert gates._tools_dir(DeployEnv()) is None
    assert gates._app_root(DeployEnv()) is None


def test_the_dag_tool_name_is_taken_from_the_config_id_or_inferred():
    q = lambda cid: types.SimpleNamespace(config_id=cid)  # noqa: E731
    assert gates._dag_config_id(q("checkout"), None) == "checkout_dag"
    assert gates._dag_config_id(q("checkout_dag"), None) == "checkout_dag"
    # No config_id: infer, but only when exactly one dag is in the payload.
    assert gates._dag_config_id(q(None), {_code("checkout_dag"): "x"}) == "checkout_dag"
    assert gates._dag_config_id(q(None), {_code("a_dag"): "", _code("b_dag"): ""}) is None
    assert gates._dag_config_id(q(None), None) is None


def test_the_canonical_dag_config_name_counts_as_a_dag():
    """A single-flow round trip keeps the source's own tool name, `dag_config`. Reading
    only the `<id>_dag` convention makes that app look like it has no flow at all."""
    assert gates._is_dag_toolname("checkout_dag") is True
    assert gates._is_dag_toolname("dag_config") is True
    assert gates._is_dag_toolname("set_pin") is False
    assert gates._dag_config_id(
        types.SimpleNamespace(config_id=None), {_code("dag_config"): "x"}) == "dag_config"


def test_a_bodyless_tool_resource_still_counts_as_an_available_tool():
    """An agent/A2A/search tool is called by the PLATFORM and ships no python. Scanning
    for python files alone reads every one of them as a tool that does not exist, and a
    config that fires one failed this gate with nothing wrong with it."""
    files = {
        _code("f_dag"): "x",
        _code("set_pin"): "y",
        "tools/specialist/specialist.json": json.dumps({"agentTool": {"agent": "a"}}),
        "tools/partner/partner.json": json.dumps({"remoteAgentTool": {"url": "u"}}),
        "tools/lookup/lookup.json": json.dumps({"googleSearchTool": {}}),
        "tools/plain/plain.json": json.dumps({"pythonFunction": {"name": "plain"}}),
        "tools/broken/broken.json": "{not json",
    }
    available = gates._available_tools_from_files(files)
    assert set(available) >= {"specialist", "partner", "lookup"}
    # A python-backed tool with no code file is NOT available (that is the real defect).
    assert "plain" not in available
    assert "broken" not in available
    # The framework builtins ship no py file but are always callable.
    assert {"end_session", "set_active_flow"} <= set(available)


def test_no_payload_means_no_tool_allowlist_at_all():
    """None disables the validator's availability check (local mode falls back to the
    on-disk scan); an empty list would tell it NOTHING is callable."""
    assert gates._available_tools_from_files(None) is None
    assert gates._available_tools_from_files({}) is None


# ======================================================================================
# gates: validate_dag
# ======================================================================================

def _prepush(files, config=None, config_id="f", strict=False) -> PrePushRequest:
    return PrePushRequest(
        app_files=files, config=config, config_id=config_id, strict=strict)


def test_validate_dag_passes_a_config_that_is_actually_valid():
    files = _healthy_app()
    check = gates._check_validate_dag(
        _prepush(files, _config()), _fmap(files), DeployEnv())
    assert check.ok is True and "valid" in check.detail


def test_validate_dag_blocks_a_structurally_broken_config():
    """The blocking half. A task whose input names no slot can never run, so the agent
    collects forever — exactly the thing a deploy gate exists to catch."""
    cfg = models.Config.model_validate(
        {"slots": [], "tasks": [{"name": "t", "tool": "do", "inputs": ["nope"]}]})
    files = _healthy_app()
    check = gates._check_validate_dag(_prepush(files, cfg), _fmap(files), DeployEnv())
    assert check.ok is False
    assert "nope" in check.detail


def test_only_a_strict_push_demands_the_referenced_tool_be_in_the_payload():
    """An existing hosted app legitimately references tools that live in CES but not in
    the local payload, so tool-availability is strict-only. Structural errors still
    block in both modes — this asserts the DIFFERENCE, not just the strict failure."""
    cfg = _config()
    files = [f for f in _healthy_app() if f.path != _code("set_pin")]

    lenient = gates._check_validate_dag(
        _prepush(files, cfg, strict=False), _fmap(files), DeployEnv())
    strict = gates._check_validate_dag(
        _prepush(files, cfg, strict=True), _fmap(files), DeployEnv())

    assert lenient.ok is True
    assert strict.ok is False and "set_pin" in strict.detail


def test_a_multi_flow_app_validates_every_flow_rather_than_skipping():
    """With a host router plus per-flow dags there is no single resolvable config. The
    gate must validate them ALL — returning "nothing to check" here would pass every
    multi-flow app vacuously."""
    good = _dag_source("a_dag", _config("pin"))
    files = [
        ScaffoldFile(path=_code("a_dag"), content=good),
        ScaffoldFile(path=_code("b_dag"), content=_dag_source("b_dag", _config("zip_code"))),
    ]
    check = gates._check_validate_dag(
        _prepush(files, None, config_id=None), _fmap(files), DeployEnv())
    assert check.ok is True and "2 flow config(s)" in check.detail


def test_one_bad_flow_in_a_multi_flow_app_blocks_the_whole_push():
    bad = (
        "def b_dag():\n"
        "    return {'slots': [], 'tasks': [{'name': 't', 'tool': 'do',\n"
        "            'inputs': ['nope']}]}\n"
    )
    files = [
        ScaffoldFile(path=_code("a_dag"), content=_dag_source("a_dag", _config("pin"))),
        ScaffoldFile(path=_code("b_dag"), content=bad),
    ]
    check = gates._check_validate_dag(
        _prepush(files, None, config_id=None), _fmap(files), DeployEnv())
    assert check.ok is False and check.detail.startswith("b_dag:")


def test_an_unparseable_flow_source_is_reported_not_swallowed():
    """`_all_dag_configs` logs and skips a config it cannot import. If that were the
    only dag the app has, the payload must still be refused."""
    files = [ScaffoldFile(path=_code("a_dag"), content="def a_dag(:\n  syntax error")]
    assert gates._all_dag_configs(_fmap(files)) == {}
    check = gates._check_validate_dag(
        _prepush(files, None, config_id=None), _fmap(files), DeployEnv())
    assert check.ok is False and "No DAG config found" in check.detail


def test_a_whole_app_payload_with_no_dag_at_all_is_refused():
    files = [ScaffoldFile(path="app.json", content="{}")]
    check = gates._check_validate_dag(
        _prepush(files, None, config_id=None), _fmap(files), DeployEnv())
    assert check.ok is False and "missing or unparseable" in check.detail


def test_a_pure_local_run_with_no_config_defers_instead_of_failing():
    """The one legitimate skip: no payload and no config means the on-disk tree is the
    source of truth and this gate has nothing to read."""
    check = gates._check_validate_dag(
        _prepush(None, None, config_id=None), None, DeployEnv())
    assert check.ok is True and "local mode" in check.detail


def test_the_config_is_imported_out_of_the_payload_when_none_is_inlined():
    """A hosted re-push carries no inline config, only the rendered dag. The gate has to
    read the config back out of the source or it would never validate anything."""
    files = _healthy_app()
    cfg = gates._config_dict(_prepush(files, None), _fmap(files))
    assert cfg is not None and cfg["slots"][0]["name"] == "pin"


def test_an_unimportable_dag_source_yields_no_config_rather_than_raising():
    files = [ScaffoldFile(path=_code("f_dag"), content="def f_dag(:\n bad")]
    assert gates._config_dict(_prepush(files, None), _fmap(files)) is None


def test_a_crashing_validator_becomes_a_failing_check_not_a_traceback(monkeypatch):
    """Crash-envelope: gates.run() must never raise at its caller, which is a UI, an
    autonomous loop and a batch job."""
    def _boom(*_a, **_k):
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(gates.validation, "raw_validate_single", _boom)
    files = _healthy_app()
    check = gates._check_validate_dag(
        _prepush(files, _config()), _fmap(files), DeployEnv())
    assert check.ok is False and "validator exploded" in check.detail


def test_a_crashing_validator_in_the_multi_flow_sweep_is_also_contained(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(gates.validation, "raw_validate_single", _boom)
    files = [
        ScaffoldFile(path=_code("a_dag"), content=_dag_source("a_dag", _config("pin"))),
        ScaffoldFile(path=_code("b_dag"), content=_dag_source("b_dag", _config("zip_code"))),
    ]
    check = gates._check_validate_dag(
        _prepush(files, None, config_id=None), _fmap(files), DeployEnv())
    assert check.ok is False and "validator crashed" in check.detail


def test_an_unreadable_setter_source_does_not_stop_the_validation(monkeypatch):
    """Source-aware extras are optional; losing them must not turn a valid app away."""
    def _boom(_cfg):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(gates.tool_scan, "read_setter_sources", _boom)
    files = _healthy_app()
    check = gates._check_validate_dag(
        _prepush(files, _config()), _fmap(files), DeployEnv())
    assert check.ok is True


# ======================================================================================
# gates: tool_bodies + setter_shape
# ======================================================================================

def test_tool_bodies_passes_when_every_referenced_tool_is_implemented():
    files = _healthy_app()
    check = gates._check_tool_bodies(
        _prepush(files, _config()), _fmap(files), DeployEnv())
    assert check.ok is True


@pytest.mark.parametrize(
    "body, why",
    [
        ("def set_pin(pin):\n    pass\n", "pass-only"),
        ("def set_pin(pin):\n    ...\n", "ellipsis-only"),
        ('def set_pin(pin):\n    """Docs only."""\n', "docstring-only"),
        ("", "empty file"),
        ("PIN = 1\n", "no function at all"),
        ("def set_pin(pin:\n", "unparseable"),
    ],
)
def test_a_tool_whose_body_does_nothing_is_a_stub(body, why):
    """File-exists is not implemented. Each of these passes the availability check and
    then no-ops at runtime, which reads to the author as the agent ignoring them."""
    assert gates._is_stub_source(body) is True, why


def test_a_tool_with_a_docstring_and_real_code_is_not_a_stub():
    assert gates._is_stub_source(_STORED_SETTER) is False


def test_an_async_tool_body_counts_as_a_real_implementation():
    assert gates._is_stub_source(
        "async def fetch(a):\n    return {'ok': a}\n") is False


def test_tool_bodies_ignores_a_tool_whose_source_is_not_in_the_payload():
    """A missing file is validate_dag's job. Reporting it here too would tell the author
    to "author the tool code" for a tool that lives in CES."""
    files = [f for f in _healthy_app() if f.path != _code("set_pin")]
    check = gates._check_tool_bodies(
        _prepush(files, _config()), _fmap(files), DeployEnv())
    assert check.ok is True


def test_tool_bodies_has_nothing_to_say_without_a_config_or_a_payload():
    assert gates._check_tool_bodies(_prepush(None, None, config_id=None), None,
                                    DeployEnv()).ok is True
    files = _healthy_app()
    assert gates._check_tool_bodies(_prepush(None, _config()), None, DeployEnv()).ok is True
    assert gates._check_tool_bodies(
        _prepush(files, None, config_id=None), {}, DeployEnv()).ok is True


def test_setter_shape_passes_a_setter_that_returns_the_stored_envelope():
    files = _healthy_app()
    check = gates._check_setter_shape(
        _prepush(files, _config()), _fmap(files), DeployEnv())
    assert check.ok is True


def test_setter_shape_catches_a_setter_that_returns_a_bare_value():
    """Without `{"stored": ...}` the slot never fills and the agent asks forever. It
    passes validate_dag — the defect is in the source, which the validator can't see."""
    files = [
        f for f in _healthy_app() if f.path != _code("set_pin")
    ] + [ScaffoldFile(path=_code("set_pin"), content="def set_pin(pin):\n    return pin\n")]
    check = gates._check_setter_shape(
        _prepush(files, _config()), _fmap(files), DeployEnv())
    assert check.ok is False
    assert "set_pin" in check.detail and "pin" in check.detail


def test_setter_shape_leaves_task_sourced_slots_alone():
    """A `task:`-sourced slot is filled by the engine from the task result, not by a
    user-slot setter, so the stored-envelope rule does not apply to it."""
    cfg = models.Config.model_validate({
        "slots": [{"name": "balance", "source": "task:lookup", "setter": "set_balance"}],
        "tasks": [{"name": "lookup", "tool": "lookup_tool", "inputs": [],
                   "outputs": {"balance": "balance"}}],
    })
    files = [ScaffoldFile(path=_code("set_balance"),
                          content="def set_balance(balance):\n    return balance\n")]
    check = gates._check_setter_shape(_prepush(files, cfg), _fmap(files), DeployEnv())
    assert check.ok is True


def test_setter_shape_skips_slots_with_no_setter_and_junk_entries():
    cfg = {"slots": [{"name": "a", "source": "user"}, "not-a-dict"], "tasks": []}
    req = types.SimpleNamespace(app_files=None, config=None, config_id="f", strict=False)
    files = {_code("f_dag"): "x"}
    check = gates._check_setter_shape(
        types.SimpleNamespace(**{**req.__dict__, "config": cfg}), files, DeployEnv())
    assert check.ok is True


def test_setter_shape_defers_a_setter_whose_source_is_absent():
    files = [f for f in _healthy_app() if f.path != _code("set_pin")]
    check = gates._check_setter_shape(
        _prepush(files, _config()), _fmap(files), DeployEnv())
    assert check.ok is True


def test_setter_shape_has_nothing_to_say_without_a_config_or_a_payload():
    assert gates._check_setter_shape(
        _prepush(None, None, config_id=None), None, DeployEnv()).ok is True


# ======================================================================================
# gates: lint
# ======================================================================================

def test_the_lint_gate_has_nothing_to_lint_without_a_config():
    check = gates._check_lint(_prepush(None, None, config_id=None), None, DeployEnv())
    assert check.ok is True and check.detail == "No config to lint."


# ======================================================================================
# gates: callback_sync
# ======================================================================================

def test_callback_sync_passes_a_freshly_scaffolded_agent():
    files = _healthy_app()
    check = gates._check_callback_sync(_prepush(files), _fmap(files), DeployEnv())
    assert check.ok is True and "4 agent callbacks" in check.detail


def test_callback_sync_names_the_agent_and_the_callback_that_drifted():
    files = [
        ScaffoldFile(path="app.json", content="{}"),
        *_callback_files("Main", drift="after_tool"),
    ]
    check = gates._check_callback_sync(_prepush(files), _fmap(files), DeployEnv())
    assert check.ok is False and "Main/after_tool" in check.detail


def test_callback_sync_refuses_a_payload_with_no_callbacks_to_verify():
    """Zero checked is not "all fine" — an app with no framework callbacks runs no
    engine at all."""
    check = gates._check_callback_sync(_prepush([]), {}, DeployEnv())
    assert check.ok is False and "No agent callbacks" in check.detail


def test_callback_sync_reads_the_on_disk_agent_tree_in_local_mode(tmp_path):
    for cb, data in blessed_source.callbacks().items():
        d = tmp_path / "agents" / "Main" / f"{cb}_callbacks" / f"{cb}_callbacks_01"
        d.mkdir(parents=True)
        (d / "python_code.py").write_bytes(data)
    check = gates._check_callback_sync(
        _prepush(None), None, DeployEnv(agent_dir=str(tmp_path)))
    assert check.ok is True and "4 agent callbacks" in check.detail


def test_callback_sync_spots_a_hand_edited_callback_on_disk(tmp_path):
    for cb, data in blessed_source.callbacks().items():
        d = tmp_path / "agents" / "Main" / f"{cb}_callbacks" / f"{cb}_callbacks_01"
        d.mkdir(parents=True)
        (d / "python_code.py").write_bytes(
            data + b"\n# hand-edited\n" if cb == "before_model" else data)
    check = gates._check_callback_sync(
        _prepush(None), None, DeployEnv(agent_dir=str(tmp_path)))
    assert check.ok is False and "Main/before_model" in check.detail


def test_callback_sync_says_so_when_there_is_no_local_app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gates.framework_bridge, "resolve_framework_root",
        lambda _root=None: Path(tmp_path / "nope"))
    check = gates._check_callback_sync(_prepush(None), None, DeployEnv())
    assert check.ok is False and "No app directory" in check.detail


def test_an_app_dir_with_no_agents_folder_reads_as_zero_callbacks(tmp_path):
    assert gates._agent_callback_bytes_from_disk(tmp_path) == {
        "before_agent": [], "before_model": [], "after_model": [], "after_tool": []}


def test_callback_sync_fails_loudly_when_the_blessed_source_is_unavailable(monkeypatch):
    """If we cannot load what "correct" is, we cannot certify a match. Saying OK here
    would silently turn the check off for everyone."""
    files = _healthy_app()

    def _boom():
        raise RuntimeError("blessed bundle missing")

    monkeypatch.setattr(gates.blessed_source, "callbacks", _boom)
    check = gates._check_callback_sync(_prepush(files), _fmap(files), DeployEnv())
    assert check.ok is False and "Blessed source unavailable" in check.detail


def test_windows_style_separators_in_a_payload_still_match_a_callback():
    files = {
        "agents\\Main\\after_tool_callbacks\\after_tool_callbacks_01\\python_code.py": "x"
    }
    found = gates._agent_callback_bytes_from_files(files)
    assert found["after_tool"] == [("Main", b"x")]


# ======================================================================================
# gates: dup_uuid
# ======================================================================================

def test_dup_uuid_passes_an_app_whose_tool_names_are_all_distinct():
    files = _healthy_app()
    check = gates._check_dup_uuid(_prepush(files), _fmap(files), DeployEnv())
    assert check.ok is True and "unique" in check.detail


def test_two_tools_sharing_a_uuid_are_blocked_with_the_404_they_would_cause():
    """This is the push that comes back "404 Tools not found" with nothing in the app
    obviously wrong. The detail has to name both tools and offer a fresh UUID."""
    shared = "33333333-3333-3333-3333-333333333333"
    files = [_tool_json("alpha", shared), _tool_json("beta", shared)]
    check = gates._check_dup_uuid(_prepush(files), _fmap(files), DeployEnv())
    assert check.ok is False
    assert "alpha, beta" in check.detail and "Tools not found" in check.detail
    assert check.fix is not None and check.fix.patch["op"] == "mint_uuid"
    assert check.fix.patch["value"] != shared


def test_dup_uuid_ignores_unparseable_and_unnamed_tool_resources():
    files = {
        "tools/a/a.json": json.dumps({"name": "u-1"}),
        "tools/b/b.json": "{not json",
        "tools/c/c.json": json.dumps({"displayName": "c"}),   # no name
        "tools/d/d.json": json.dumps(["not", "a", "dict"]),
        _code("e"): "def e(): return 1",                       # not a tool json
    }
    check = gates._check_dup_uuid(_prepush(None), files, DeployEnv())
    assert check.ok is True and "All 1 tool UUIDs" in check.detail


def test_dup_uuid_reads_the_tool_jsons_off_disk_in_local_mode(tmp_path):
    tools = tmp_path / "tools"
    shared = "44444444-4444-4444-4444-444444444444"
    for name in ("alpha", "beta"):
        d = tools / name
        d.mkdir(parents=True)
        (d / f"{name}.json").write_text(json.dumps({"name": shared}))
    # A dir with no <tool>.json, and one with unreadable json — both skipped.
    (tools / "empty").mkdir()
    (tools / "broken").mkdir()
    (tools / "broken" / "broken.json").write_text("{not json")

    env = DeployEnv(framework_root=str(tools))
    assert sorted(l for l, _ in gates._tool_json_entries(_prepush(None), None, env)) == [
        "alpha", "beta"]
    check = gates._check_dup_uuid(_prepush(None), None, env)
    assert check.ok is False and "alpha, beta" in check.detail


def test_no_local_tools_dir_means_no_entries_rather_than_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gates.framework_bridge, "resolve_framework_root",
        lambda _root=None: Path(tmp_path / "nope"))
    assert gates._tool_json_entries(_prepush(None), None, DeployEnv()) == []


# ======================================================================================
# gates: docstring_sig
# ======================================================================================

def test_docstring_sig_passes_when_the_args_block_matches_the_signature():
    files = _healthy_app()
    check = gates._check_docstring_sig(_prepush(files), _fmap(files), DeployEnv())
    assert check.ok is True and "1 tools checked" in check.detail


def test_docstring_sig_reports_both_directions_of_a_mismatch():
    src = (
        'def charge(amount):\n'
        '    """Charge.\n'
        '\n'
        '    Args:\n'
        '        amount: how much.\n'
        '        currency: which one.\n'
        '    """\n'
        '    return amount\n'
    )
    check = gates._check_docstring_sig(
        _prepush(None), {_code("charge"): src}, DeployEnv())
    assert check.ok is False
    assert "charge" in check.detail and "doc-only ['currency']" in check.detail


def test_a_signature_arg_the_docstring_forgot_is_reported_as_sig_only():
    src = (
        'def charge(amount, currency):\n'
        '    """Charge.\n'
        '\n'
        '    Args:\n'
        '        amount: how much.\n'
        '    """\n'
        '    return amount\n'
    )
    check = gates._check_docstring_sig(
        _prepush(None), {_code("charge"): src}, DeployEnv())
    assert check.ok is False and "sig-only ['currency']" in check.detail


def test_a_tool_that_documents_nothing_is_not_subject_to_the_parity_gate():
    """No `Args:` block documents nothing, which is allowed. Only a docstring that
    CLAIMS an argument list has to be right about it."""
    check = gates._check_docstring_sig(
        _prepush(None), {_code("t"): "def t(a, b):\n    return a\n"}, DeployEnv())
    assert check.ok is True and "0 tools checked" in check.detail


def test_docstring_sig_reads_tool_sources_off_disk_in_local_mode(tmp_path):
    tools = tmp_path / "tools"
    d = tools / "charge" / "python_function"
    d.mkdir(parents=True)
    d.joinpath("python_code.py").write_text(
        'def charge(amount):\n'
        '    """Charge.\n\n    Args:\n        currency: wrong.\n    """\n'
        '    return amount\n'
    )
    (tools / "no_code").mkdir()
    env = DeployEnv(framework_root=str(tools))
    assert [l for l, _ in gates._tool_code_sources(_prepush(None), None, env)] == ["charge"]
    check = gates._check_docstring_sig(_prepush(None), None, env)
    assert check.ok is False and "charge" in check.detail


def test_no_local_tools_dir_means_no_sources_rather_than_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gates.framework_bridge, "resolve_framework_root",
        lambda _root=None: Path(tmp_path / "nope"))
    assert gates._tool_code_sources(_prepush(None), None, DeployEnv()) == []


# ======================================================================================
# gates: the aggregator (run) and the create-vs-update target
# ======================================================================================

def test_a_healthy_app_clears_every_gate():
    """The direction that matters most: seven gates that all fire on a good app would
    block every release, and the pressure would be to delete them rather than fix them."""
    files = _healthy_app()
    report = gates.run(_prepush(files, _config()))
    assert report.ok is True
    assert {c.id for c in report.checks} == {
        "validate_dag", "tool_bodies", "setter_shape", "callback_sync", "dup_uuid",
        "docstring_sig", "lint",
    }
    assert all(c.ok for c in report.checks)


def test_a_duplicate_uuid_blocks_the_report():
    shared = "55555555-5555-5555-5555-555555555555"
    files = [f for f in _healthy_app() if not f.path.endswith(".json")
             or f.path == "app.json"]
    files += [_tool_json("f_dag", shared), _tool_json("set_pin", shared)]
    report = gates.run(_prepush(files, _config()))
    assert report.ok is False
    dup = next(c for c in report.checks if c.id == "dup_uuid")
    assert dup.ok is False and dup.severity == "error"


def test_drifted_callbacks_are_advisory_and_do_not_block():
    """An existing CES app the author did not write here ships callbacks from another
    framework version. Worth saying; not worth refusing to update their app over."""
    files = [f for f in _healthy_app()
             if "_callbacks/" not in f.path] + _callback_files(drift="before_agent")
    report = gates.run(_prepush(files, _config()))
    cb = next(c for c in report.checks if c.id == "callback_sync")
    assert cb.ok is False and cb.severity == "warning"
    assert report.ok is True


def test_a_docstring_mismatch_is_advisory_because_ces_does_not_enforce_it():
    files = [f for f in _healthy_app() if f.path != _code("set_pin")]
    files.append(ScaffoldFile(
        path=_code("set_pin"),
        content='def set_pin(pin):\n    """S.\n\n    Args:\n        nope: x\n    """\n'
                '    return {"stored": True, "value": pin}\n'))
    report = gates.run(_prepush(files, _config()))
    doc = next(c for c in report.checks if c.id == "docstring_sig")
    assert doc.ok is False and doc.severity == "warning" and report.ok is True


def test_setter_shape_is_advisory_by_default_and_blocking_under_strict():
    """Specter authors every tool it ships, so a wrong setter there is its own bug and
    must block. The UI's legacy push path must not start refusing existing apps."""
    files = [f for f in _healthy_app() if f.path != _code("set_pin")]
    files.append(ScaffoldFile(path=_code("set_pin"),
                              content="def set_pin(pin):\n    return pin\n"))

    lenient = gates.run(_prepush(files, _config(), strict=False))
    strict = gates.run(_prepush(files, _config(), strict=True))

    lenient_check = next(c for c in lenient.checks if c.id == "setter_shape")
    strict_check = next(c for c in strict.checks if c.id == "setter_shape")
    assert lenient_check.severity == "warning" and lenient.ok is True
    assert strict_check.severity == "error" and strict.ok is False


def test_a_check_that_raises_becomes_a_failing_check_not_an_exception(monkeypatch):
    """gates.run() is documented as never raising: its callers are a UI, an agent loop
    and a batch job, none of which can do anything with a traceback."""
    def _check_dup_uuid(_req, _files, _env):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(gates, "_check_dup_uuid", _check_dup_uuid)
    files = _healthy_app()
    report = gates.run(_prepush(files, _config()))
    dup = next(c for c in report.checks if c.id == "dup_uuid")
    assert dup.ok is False and "Check crashed: gate exploded" in dup.detail
    assert report.ok is False


def test_run_supplies_a_default_env_when_the_caller_omits_one():
    files = _healthy_app()
    assert gates.run(_prepush(files, _config()), None).ok is True


def test_the_target_is_update_when_a_deployment_is_already_known():
    req = types.SimpleNamespace(deployed_app_id="app-123", display_name="X", config_id="f")
    assert gates._target(req) == ("update", "app-123")


def test_the_target_is_create_and_labelled_by_the_best_name_available():
    assert gates._target(types.SimpleNamespace(
        deployed_app_id=None, display_name="Billing Agent", config_id="f")) == (
            "create", "Billing Agent")
    assert gates._target(types.SimpleNamespace(
        deployed_app_id=None, display_name=None, config_id="checkout")) == (
            "create", "checkout")
    assert gates._target(types.SimpleNamespace()) == ("create", "new app")


def test_the_error_classifier_is_reachable_through_the_gates_module():
    """Every caller reached for it here, so the re-export is part of the contract."""
    assert gates.classify_push_error is argv_mod.classify_push_error


# ======================================================================================
# runner: resolving the interpreter, without ever launching one
# ======================================================================================

def test_the_interpreter_probe_returns_the_first_venv_that_can_import_the_cli(
        tmp_path, monkeypatch, clean_cxas_probe):
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\n")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "venv"))
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)

    probed: list[list[str]] = []

    def _fake_run(cmd, **_kw):
        probed.append(list(cmd))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", _fake_run)
    py, err = runner.resolve_cxas_python()
    assert py == str(venv / "python") and err == ""
    assert probed == [[str(venv / "python"), "-c", "import cxas_scrapi"]]


def test_a_resolved_interpreter_is_cached_so_the_probe_runs_once(
        tmp_path, monkeypatch, clean_cxas_probe):
    runner._CXAS_PY = ("/already/resolved/python", "")

    def _never(*_a, **_k):
        raise AssertionError("the probe must not re-run once it has an answer")

    monkeypatch.setattr(runner.subprocess, "run", _never)
    assert runner.resolve_cxas_python() == ("/already/resolved/python", "")


def test_a_failed_probe_is_NOT_cached_so_a_hiccup_cannot_wedge_every_deploy(
        tmp_path, monkeypatch, clean_cxas_probe):
    """Caching the failure would leave the server refusing to deploy until restarted."""
    fake_py = tmp_path / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path))
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(runner.sys, "executable", str(fake_py))
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *_a, **_k: types.SimpleNamespace(returncode=1))

    py, err = runner.resolve_cxas_python()
    assert py is None
    assert "cxas_scrapi" in err and "uv sync --group service" in err
    assert runner._CXAS_PY is None


def test_a_probe_that_blows_up_moves_on_to_the_next_candidate(
        tmp_path, monkeypatch, clean_cxas_probe):
    bad = tmp_path / "bad" / "bin" / "python"
    bad.parent.mkdir(parents=True)
    bad.write_text("")
    good = tmp_path / "good" / "bin" / "python"
    good.parent.mkdir(parents=True)
    good.write_text("")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "bad"))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "good"))

    def _fake_run(cmd, **_kw):
        if cmd[0] == str(bad):
            raise OSError("exec format error")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", _fake_run)
    assert runner.resolve_cxas_python()[0] == str(good)


def test_the_argv_prefix_prefers_the_module_over_a_console_script(
        monkeypatch, clean_cxas_probe):
    """`Path(sys.executable).resolve()` follows the venv symlink to the macOS framework
    python and picks up a BROKEN `cxas` next to it, so the importable module wins."""
    monkeypatch.setattr(runner, "resolve_cxas_python", lambda: ("/venv/bin/python", ""))
    assert runner.cxas_argv_prefix() == (
        ["/venv/bin/python", "-m", "cxas_scrapi.cli.main"], "")


def test_a_working_console_script_is_still_better_than_refusing(
        tmp_path, monkeypatch, clean_cxas_probe):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").write_text("")
    (bindir / "cxas").write_text("#!/bin/sh\n")
    monkeypatch.setattr(runner, "resolve_cxas_python", lambda: (None, "nope"))
    monkeypatch.setattr(runner.sys, "executable", str(bindir / "python"))
    assert runner.cxas_argv_prefix() == ([str(bindir / "cxas")], "")


def test_with_no_interpreter_and_no_script_the_reason_is_reported(
        tmp_path, monkeypatch, clean_cxas_probe):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").write_text("")
    monkeypatch.setattr(runner, "resolve_cxas_python", lambda: (None, "toolchain missing"))
    monkeypatch.setattr(runner.sys, "executable", str(bindir / "python"))
    assert runner.cxas_argv_prefix() == (None, "toolchain missing")


def test_a_missing_toolchain_surfaces_as_rc_127_not_an_exception(monkeypatch):
    monkeypatch.setattr(runner, "cxas_argv_prefix", lambda: (None, "toolchain missing"))
    rc, out, err = asyncio.run(runner.default_push_runner(["push"]))
    assert (rc, out, err) == (127, "", "toolchain missing")


def test_an_unlaunchable_interpreter_also_surfaces_as_rc_127(monkeypatch):
    monkeypatch.setattr(runner, "cxas_argv_prefix", lambda: (["/no/such/python"], ""))

    def _boom(*_a, **_k):
        raise FileNotFoundError("/no/such/python")

    monkeypatch.setattr(runner.subprocess, "Popen", _boom)
    rc, out, err = asyncio.run(runner.default_push_runner(["push"]))
    assert rc == 127 and "cxas interpreter not found" in err


def test_the_deploy_subprocess_never_inherits_a_warning_channel(monkeypatch):
    """cxas_scrapi transitively imports pydub, whose import-time "Couldn't find ffmpeg"
    warning is the single most confidently misdiagnosed deploy failure."""
    seen: dict = {}

    class _Proc:
        returncode = 0
        pid = 1

        def communicate(self, timeout=None):
            return "done", ""

    def _fake_popen(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env", {})
        seen["new_session"] = kw.get("start_new_session")
        return _Proc()

    monkeypatch.setattr(runner, "cxas_argv_prefix", lambda: (["/venv/bin/python"], ""))
    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen)
    rc, out, err = asyncio.run(runner.default_push_runner(["push", "--app-dir", "/a"]))
    assert (rc, out, err) == (0, "done", "")
    assert seen["cmd"] == ["/venv/bin/python", "push", "--app-dir", "/a"]
    assert seen["env"]["PYTHONWARNINGS"] == "ignore"
    assert seen["new_session"] is True


def test_a_process_with_no_returncode_is_reported_as_a_failure(monkeypatch):
    class _Proc:
        returncode = None
        pid = 1

        def communicate(self, timeout=None):
            return None, None

    monkeypatch.setattr(runner, "cxas_argv_prefix", lambda: (["/venv/bin/python"], ""))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_a, **_k: _Proc())
    assert asyncio.run(runner.default_push_runner(["push"])) == (1, "", "")


def test_a_deploy_abandoned_between_the_fork_and_the_wait_is_still_reaped(monkeypatch):
    """The narrow race the `abandoned` flag exists for: the caller's timeout fires while
    the worker thread is still inside `Popen`. Nothing has been published for the
    coroutine to kill yet, so the thread has to check on its way past — otherwise a
    deploy we already reported as timed out keeps writing to the target.
    """
    forked = threading.Event()
    reaped: list = []

    class _Proc:
        pid = 9999
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def _slow_popen(*_a, **_k):
        time.sleep(0.5)           # the caller's 0.05s timeout fires in here
        forked.set()
        return _Proc()

    monkeypatch.setattr(runner, "cxas_argv_prefix", lambda: (["/venv/bin/python"], ""))
    monkeypatch.setattr(runner.subprocess, "Popen", _slow_popen)
    monkeypatch.setattr(runner, "kill_process_group", lambda p: reaped.append(p))

    async def _drive():
        with pytest.raises(asyncio.TimeoutError):
            await runner.run_push(["push"], timeout_s=0.05)
        assert forked.wait(10.0), "the worker thread never got past the fork"

    asyncio.run(_drive())
    # Reaped twice is correct and harmless: once by the abandoning coroutine (which
    # found nothing published), once by the thread when it saw the flag.
    assert reaped, "the abandoned subprocess was left running"
    assert all(isinstance(p, _Proc) for p in reaped)


def test_killing_a_process_group_falls_back_to_the_lead_process(monkeypatch):
    """No killpg (non-POSIX) or an already-reaped tree must not raise out of teardown."""
    killed: list[bool] = []

    class _Proc:
        pid = 4242

        def kill(self):
            killed.append(True)

    def _no_pgid(_pid):
        raise ProcessLookupError

    monkeypatch.setattr(runner.os, "getpgid", _no_pgid)
    runner.kill_process_group(_Proc())
    assert killed == [True]


def test_teardown_never_raises_even_if_the_fallback_kill_fails(monkeypatch):
    class _Proc:
        pid = 4242

        def kill(self):
            raise RuntimeError("already gone")

    monkeypatch.setattr(runner.os, "getpgid",
                        lambda _pid: (_ for _ in ()).throw(PermissionError))
    runner.kill_process_group(_Proc())  # must not raise


def test_benign_import_warnings_are_stripped_but_the_real_error_is_kept():
    noisy = (
        "RuntimeWarning: Couldn't find ffmpeg or avconv\n"
        "  warnings.warn(\n"
        "\n"
        "ERROR: permission denied on projects/example\n"
    )
    assert runner.strip_benign_stderr(noisy) == (
        "\nERROR: permission denied on projects/example").strip()
    assert runner.strip_benign_stderr(noisy, drop_blank=True) == (
        "ERROR: permission denied on projects/example")
    assert runner.strip_benign_stderr("") == ""
    assert runner.strip_benign_stderr("  warnings.warn(\n", drop_blank=True) == ""


def test_the_runner_seam_can_be_swapped_and_read_back(fake_cxas):
    assert runner.get_runner() is fake_cxas
    assert asyncio.run(runner.run_push(["push"]))[0] == 0
    assert fake_cxas.calls == [["push"]]


# ======================================================================================
# render: the pure config -> app_files half
# ======================================================================================

def test_a_file_set_of_plain_dicts_is_accepted_wherever_scaffoldfiles_are():
    """The wire delivers dicts and in-process callers pass models; both reach here."""
    files = [{"path": "agents/Main/Main.json",
              "content": json.dumps({"tools": ["root_dag"]})}]
    out = render.register_dag_in_agents(files, "child_dag", "root_dag")
    assert json.loads(out[0].content)["tools"] == ["child_dag", "root_dag"]


def test_registering_a_child_dag_is_idempotent():
    content = json.dumps({"tools": ["root_dag", "child_dag"]})
    files = [ScaffoldFile(path="agents/Main/Main.json", content=content)]
    out = render.register_dag_in_agents(files, "child_dag", "root_dag")
    assert json.loads(out[0].content)["tools"] == ["root_dag", "child_dag"]


def test_a_malformed_agent_json_is_left_exactly_as_it_was():
    files = [ScaffoldFile(path="agents/Main/Main.json", content="{not json")]
    out = render.register_dag_in_agents(files, "child_dag", "root_dag")
    assert out[0].content == "{not json"


def test_a_minted_tool_json_carries_a_fresh_uuid_and_no_parameters_block():
    """CES derives the call schema from the python signature, so a `parameters` block
    here is redundant at best and wrong at worst. Two mints must never collide — that
    is the dup_uuid 404 the gate above exists to catch."""
    a = render.mint_tool_json("checkout_dag", "Component sub-DAG flow.")
    b = render.mint_tool_json("checkout_dag")
    assert a["name"] != b["name"]
    assert "parameters" not in a
    assert a["pythonFunction"]["pythonCode"] == render.code_path("checkout_dag")
    assert a["pythonFunction"]["description"] == "Component sub-DAG flow."
    assert b["pythonFunction"]["description"] == ""
    assert a["displayName"] == "checkout_dag"


def test_bare_to_dag_is_idempotent():
    assert render.bare_to_dag("checkout") == "checkout_dag"
    assert render.bare_to_dag("checkout_dag") == "checkout_dag"


def test_a_bundle_renders_every_config_into_its_own_dag():
    files = [
        ScaffoldFile(path="agents/Main/Main.json",
                     content=json.dumps({"tools": ["root_dag"]})),
        ScaffoldFile(path=render.code_path("root_dag"),
                     content="def root_dag():\n    return {}\n"),
    ]
    entries = [
        PushConfigEntry(config_id="root", config=_config("pin")),
        PushConfigEntry(config_id="child", config=_config("zip_code", "set_zip")),
    ]
    out, root_dag = render.render_bundle_into_app_files(files, entries, "root")

    assert root_dag == "root_dag"
    by_path = {f.path: f.content for f in out}
    # The existing dag was overwritten in place…
    assert "pin" in by_path[render.code_path("root_dag")]
    # …and the absent child was CREATED: code + a minted json + agent registration.
    assert "zip_code" in by_path[render.code_path("child_dag")]
    assert json.loads(by_path["tools/child_dag/child_dag.json"])["displayName"] == "child_dag"
    assert json.loads(by_path["agents/Main/Main.json"])["tools"] == ["child_dag", "root_dag"]


def test_a_bundle_render_failure_ships_nothing_at_all(monkeypatch):
    """Per-config failure raises rather than shipping a partially-updated bundle: half
    a component workspace deployed is worse than none of it."""
    monkeypatch.setattr(render.config_io, "export_python",
                        lambda cfg, dag: "def d():\n    pass\n")
    entries = [PushConfigEntry(config_id="root", config=_config("pin"))]
    with pytest.raises(RenderFailedError) as exc:
        render.render_bundle_into_app_files(
            [ScaffoldFile(path=render.code_path("root_dag"), content="x")], entries, "root")
    assert "missing slots" in str(exc.value)


def test_a_renderer_that_throws_is_wrapped_as_a_typed_refusal(monkeypatch):
    def _boom(*_a, **_k):
        raise ValueError("codegen blew up")

    monkeypatch.setattr(render.config_io, "import_config", _boom)
    with pytest.raises(RenderFailedError) as exc:
        render.render_one("f_dag", _config())
    assert "codegen blew up" in str(exc.value)


def test_an_app_with_no_dag_tool_at_all_names_no_candidates():
    with pytest.raises(DagUnresolvedError) as exc:
        render.resolve_dag_tool([ScaffoldFile(path="app.json", content="{}")], "checkout")
    assert exc.value.candidates == []


def test_the_bundle_tool_walk_and_the_missing_report_agree():
    entries = [PushConfigEntry(config_id="root", config=_config("pin"))]
    referenced = render.referenced_setter_task_tools(entries)
    assert "set_pin" in referenced and not any(n.endswith("_dag") for n in referenced)
    assert render.missing_tools([], referenced) == ["set_pin"]
    assert render.missing_tools(
        [{"path": render.code_path("set_pin"), "content": "x"}], referenced) == []


# ======================================================================================
# service.push: every refusal, with the subprocess faked
# ======================================================================================

def _push(spec, env=None, **kw):
    return asyncio.run(service.push(spec, env or DeployEnv(), **kw))


def test_a_config_only_push_that_cannot_be_rendered_is_a_client_error(fake_cxas):
    out = _push(PushSpec(), DeployEnv(agent_dir="/tmp/app"))
    assert out.ok is False
    assert "Config could not be rendered" in out.error
    assert fake_cxas.calls == [], "a bad config must never reach the subprocess"


def test_a_config_only_push_sends_the_on_disk_agent_dir(fake_cxas, tmp_path):
    out = _push(
        PushSpec(config=_config(), config_id="f", to="my-agent"),
        DeployEnv(agent_dir=str(tmp_path), project="proj", location="us-central1"),
    )
    assert out.ok is True
    assert fake_cxas.calls == [[
        "push", "--app-dir", str(tmp_path), "--to", "my-agent",
        "--project-id", "proj", "--location", "us-central1",
    ]]


def test_nothing_staged_says_the_right_thing_in_each_mode(fake_cxas):
    local = _push(PushSpec(config=_config(), config_id="f"), DeployEnv(mode="local"))
    hosted = _push(PushSpec(config=_config(), config_id="f"), DeployEnv(mode="hosted"))
    assert local.ok is False and "--agent-dir" in local.error
    assert hosted.ok is False and "re-open" in hosted.error
    assert fake_cxas.calls == []


def test_an_unresolvable_dag_is_refused_with_the_real_candidates(fake_cxas):
    """Fabricating `{id}_dag` here is the silent "the push didn't take" bug: CES accepts
    a stray tool nobody calls and the author's edit is simply not live."""
    files = [
        ScaffoldFile(path=_code("a_dag"), content="def a_dag():\n    return {}\n"),
        ScaffoldFile(path=_code("b_dag"), content="def b_dag():\n    return {}\n"),
    ]
    out = _push(
        PushSpec(app_files=files, config=_config(), config_id="nope", run_gates=False,
                 to="app-1"),
    )
    assert out.ok is False and out.error_kind == "dag_unresolved"
    assert "a_dag, b_dag" in out.error
    assert fake_cxas.calls == []


def test_an_unresolvable_dag_with_no_candidates_still_says_something_useful(fake_cxas):
    out = _push(PushSpec(app_files=[ScaffoldFile(path="app.json", content="{}")],
                         config=_config(), config_id="nope", run_gates=False, to="app-1"))
    assert out.ok is False and "the app's dag tool" in out.error


def test_a_failed_render_refuses_rather_than_shipping_the_stale_dag(
        fake_cxas, monkeypatch):
    files = _healthy_app()
    monkeypatch.setattr(render.config_io, "export_python",
                        lambda cfg, dag: "def d():\n    pass\n")
    out = _push(PushSpec(app_files=files, config=_config(), config_id="f",
                         run_gates=False, to="app-1"))
    assert out.ok is False and out.error_kind == "render_failed"
    assert fake_cxas.calls == []


def test_a_bundle_render_failure_is_reported_as_render_failed(fake_cxas, monkeypatch):
    monkeypatch.setattr(render.config_io, "export_python",
                        lambda cfg, dag: "def d():\n    pass\n")
    files = [
        ScaffoldFile(path=render.code_path("root_dag"), content="def root_dag(): return {}"),
        ScaffoldFile(path=render.code_path("set_pin"), content=_STORED_SETTER),
    ]
    out = _push(PushSpec(
        app_files=files, config_id="root", run_gates=False, to="app-1",
        configs=[PushConfigEntry(config_id="root", config=_config("pin"))]))
    assert out.ok is False and out.error_kind == "render_failed"
    assert fake_cxas.calls == []


def test_a_valid_bundle_materializes_the_whole_workspace_and_pushes_it(
        fake_cxas, tmp_path, monkeypatch):
    files = [
        ScaffoldFile(path="agents/Main/Main.json",
                     content=json.dumps({"tools": ["root_dag"]})),
        ScaffoldFile(path=render.code_path("root_dag"), content="def root_dag(): return {}"),
        ScaffoldFile(path=render.code_path("set_pin"), content=_STORED_SETTER),
        ScaffoldFile(path=render.code_path("set_zip"), content=_STORED_SETTER),
    ]
    seen: dict = {}
    real_materialize = service.workdir.materialize

    def _spy(effective):
        seen["paths"] = sorted(f.path for f in effective)
        return real_materialize(effective)

    monkeypatch.setattr(service.workdir, "materialize", _spy)
    out = _push(PushSpec(
        app_files=files, config_id="root", run_gates=False, to="app-1", overwrite=True,
        configs=[PushConfigEntry(config_id="root", config=_config("pin")),
                 PushConfigEntry(config_id="child_dag", config=_config("zip_code", "set_zip"))],
    ))
    assert out.ok is True and out.dag == "root_dag"
    assert render.code_path("child_dag") in seen["paths"]
    assert "--overwrite" in fake_cxas.calls[0]
    # The scratch dir is always discarded.
    app_dir = fake_cxas.calls[0][fake_cxas.calls[0].index("--app-dir") + 1]
    assert not Path(app_dir).exists()


def test_a_bundle_entry_id_survives_the_dag_suffix_round_trip():
    """The bundle is keyed by FLOW id: a `_dag`-suffixed entry silently fails to satisfy
    the cross-config reference that names it."""
    assert PushConfigEntry(config_id="child_dag", config=_config()).config_id == "child"
    assert PushConfigEntry(config_id="child", config=_config()).config_id == "child"


def test_a_missing_cxas_is_explained_rather_than_reported_as_rc_127(fake_cxas):
    fake_cxas.rc, fake_cxas.stdout, fake_cxas.stderr = 127, "", "No module named cxas_scrapi"
    out = _push(PushSpec(config=_config(), config_id="f"), DeployEnv(agent_dir="/tmp/a"))
    assert out.ok is False and out.error_kind == "cxas_missing"
    assert "Install cxas-scrapi" in out.error


def test_a_caller_can_inject_its_own_error_classifier(fake_cxas):
    fake_cxas.rc, fake_cxas.stdout = 2, "everything is on fire"
    out = _push(
        PushSpec(config=_config(), config_id="f"), DeployEnv(agent_dir="/tmp/a"),
        error_classifier=lambda _o, _e, _rc: "custom_kind",
    )
    assert out.ok is False and out.error_kind == "custom_kind"
    assert out.error == "everything is on fire"


def test_the_gate_runner_is_injectable_and_a_green_report_lets_the_push_through(
        fake_cxas):
    calls: list = []

    def _gates(req):
        calls.append(req)
        return PrePushReport(ok=True, checks=[], target="create", target_label="x")

    out = _push(
        PushSpec(app_files=_healthy_app(), config=_config(), config_id="f",
                 display_name="Example Agent", strict=True),
        gate_runner=_gates,
    )
    assert out.ok is True
    assert len(calls) == 1 and calls[0].strict is True
    assert "--display-name" in fake_cxas.calls[0]


def test_a_blocked_gate_returns_the_report_and_never_runs_the_subprocess(fake_cxas):
    report = PrePushReport(
        ok=False,
        checks=[PrePushCheck(id="dup_uuid", ok=False, detail="two tools share a UUID")],
        target="update", target_label="app-1")
    out = _push(
        PushSpec(app_files=_healthy_app(), config=_config(), config_id="f", to="app-1"),
        gate_runner=lambda _req: report,
    )
    assert out.ok is False and out.error_kind == "gate_failed"
    assert out.gate_report is report
    assert fake_cxas.calls == []


def test_a_successful_whole_app_push_reports_the_dag_and_the_slots_that_shipped(
        fake_cxas):
    out = _push(
        PushSpec(app_files=_healthy_app(), config=_config(), config_id="f", to="app-1"),
        gate_runner=_green_gates,
    )
    assert out.ok is True
    assert out.dag == "f_dag" and out.rendered_slots == ["pin"]
    assert out.deployed_app_id == "abc123"
    assert out.app_name == "projects/p/locations/us/apps/abc123"


def test_a_deploy_never_pins_a_version_so_a_live_call_is_untouched(fake_cxas):
    """A push writes the app's DRAFT. Nothing in the argv selects or moves a serving
    version, which is why an author who pushes and then phones in still hears the old
    agent. If a version flag ever appears here, that expectation changes."""
    _push(PushSpec(app_files=_healthy_app(), config=_config(), config_id="f", to="app-1"),
          gate_runner=_green_gates)
    sent = fake_cxas.calls[0]
    assert sent[0] == "push"
    assert not any(a.startswith("--version") or a in {"--publish", "--promote", "--pin"}
                   for a in sent)


# ======================================================================================
# flows.deploy.push: the app-dir CLI wrapper
# ======================================================================================

def test_the_cli_wrapper_returns_stdout_on_success(monkeypatch):
    monkeypatch.setattr(
        push_cli.subprocess, "run",
        lambda argv, **_kw: types.SimpleNamespace(returncode=0, stdout="pushed", stderr=""))
    assert push_cli._run(["cxas", "push"]) == "pushed"


def test_the_cli_wrapper_raises_with_both_streams_on_failure(monkeypatch):
    monkeypatch.setattr(
        push_cli.subprocess, "run",
        lambda argv, **_kw: types.SimpleNamespace(
            returncode=3, stdout="out-detail", stderr="err-detail"))
    with pytest.raises(RuntimeError) as exc:
        push_cli._run(["cxas", "push", "--app-dir", "/a"])
    message = str(exc.value)
    assert "command failed (3)" in message
    assert "cxas push --app-dir /a" in message
    assert "out-detail" in message and "err-detail" in message


def test_a_dir_that_fails_its_integrity_check_is_refused_before_any_cxas_call(
        tmp_path, monkeypatch):
    """The last gate before a half-built agent takes live traffic. `deploy` is handed a
    PATH, so the tree may have come from an older SDK, a hand edit or a failed emit."""
    calls: list = []
    monkeypatch.setattr(push_cli, "_run", lambda a: calls.append(list(a)) or "")
    with pytest.raises(RuntimeError) as exc:
        push_cli.deploy(str(tmp_path / "not-an-app"), "projects/p/locations/us/apps/a")
    assert "refusing to push" in str(exc.value)
    assert "flows emit" in str(exc.value)
    assert calls == [], "the refusal must land before the pull AND the push"


def test_a_verified_dir_is_pushed_with_overwrite_and_nothing_else(monkeypatch, tmp_path):
    from flows.authoring import integrity

    monkeypatch.setattr(
        integrity, "verify_dir",
        lambda d: types.SimpleNamespace(ok=True, summary=lambda: "ok"))
    sent: list[list[str]] = []
    monkeypatch.setattr(push_cli, "_run", lambda a: sent.append(list(a)) or "pushed")

    out = push_cli.deploy(str(tmp_path), "projects/p/locations/us/apps/a",
                          preserve_from_target=False)
    assert out == "pushed"
    assert sent == [["cxas", "push", "--app-dir", str(tmp_path), "--to",
                     "projects/p/locations/us/apps/a", "--overwrite"]]


def test_a_preserving_deploy_pulls_the_target_first_and_throws_the_pull_away(
        monkeypatch, tmp_path, capsys):
    """`--overwrite` would strip the live app's console-configured settings, so the
    target is pulled and merged in first. The scratch pull dir must not survive."""
    from flows.authoring import integrity

    monkeypatch.setattr(
        integrity, "verify_dir",
        lambda d: types.SimpleNamespace(ok=True, summary=lambda: "ok"))
    sent: list[list[str]] = []
    monkeypatch.setattr(push_cli, "_run", lambda a: sent.append(list(a)) or "pushed")

    merged: dict = {}

    def _merge(pulled, built, **kw):
        merged["pulled"], merged["built"], merged["kw"] = pulled, built, kw
        return types.SimpleNamespace(
            preserved=["loggingSettings"], declared=["timeZone"],
            overridden=["timeZone"], warnings=["bargeInAwareness came from the target"])

    monkeypatch.setattr(push_cli, "merge_live_settings", _merge)

    push_cli.deploy(str(tmp_path), "app-display-name", cxas="cxas-dev",
                    audio_bucket="gs://example-bucket", inactivity_timeout="12s",
                    barge_in_awareness=True)

    pull, push = sent
    assert pull[:3] == ["cxas-dev", "pull", "app-display-name"]
    assert pull[3] == "--target-dir" and pull[5] == "--overwrite"
    assert push == ["cxas-dev", "push", "--app-dir", str(tmp_path), "--to",
                    "app-display-name", "--overwrite"]

    assert merged["pulled"] == pull[4] and merged["built"] == str(tmp_path)
    assert merged["kw"] == {"audio_bucket": "gs://example-bucket",
                            "inactivity_timeout": "12s", "barge_in_awareness": True}
    assert not Path(pull[4]).exists(), "the pulled scratch dir leaked"

    printed = capsys.readouterr().out
    assert "merged live settings ['loggingSettings']" in printed
    assert "overriding the target's ['timeZone']" in printed
    assert "WARN bargeInAwareness came from the target" in printed


def test_the_pull_scratch_dir_is_cleaned_up_even_when_the_merge_explodes(
        monkeypatch, tmp_path):
    from flows.authoring import integrity

    monkeypatch.setattr(
        integrity, "verify_dir",
        lambda d: types.SimpleNamespace(ok=True, summary=lambda: "ok"))
    pulled: list[str] = []

    def _run(a):
        if a[1] == "pull":
            pulled.append(a[4])
        return "ok"

    monkeypatch.setattr(push_cli, "_run", _run)

    def _boom(*_a, **_k):
        raise FileNotFoundError("built app.json not found")

    monkeypatch.setattr(push_cli, "merge_live_settings", _boom)
    with pytest.raises(FileNotFoundError):
        push_cli.deploy(str(tmp_path), "app-1")
    assert pulled and not Path(pulled[0]).exists()


def test_the_defaults_the_deploy_ships_with_are_the_documented_ones():
    """`inactivity_timeout` drives the hold-and-wait countdown, so a silent change to it
    changes how long a caller waits before the agent speaks again."""
    assert push_cli.DEFAULT_INACTIVITY_TIMEOUT == "8s"
    assert push_cli.DEFAULT_AUDIO_BUCKET is None


def test_verify_is_on_by_default_and_can_be_waived(monkeypatch, tmp_path):
    """`--no-verify` exists for "I know better"; the default must still be the check."""
    from flows.authoring import integrity

    called: list = []
    monkeypatch.setattr(
        integrity, "verify_dir",
        lambda d: called.append(d) or types.SimpleNamespace(ok=True, summary=lambda: ""))
    monkeypatch.setattr(push_cli, "_run", lambda a: "pushed")

    push_cli.deploy(str(tmp_path), "app-1", preserve_from_target=False)
    assert called == [str(tmp_path)]
    push_cli.deploy(str(tmp_path), "app-1", preserve_from_target=False, verify=False)
    assert called == [str(tmp_path)], "verify=False must not re-check"
