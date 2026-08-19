"""`flows.deploy` — the whole pipeline, with no subprocess, no CES and no network.

Every seam that touches the outside world is injected: the `cxas` invocation goes
through `runner.set_runner`, the gates through `service.push(gate_runner=…)`, and the
ambient project/location/agent_dir through a `DeployEnv`. So these tests drive the real
deploy path end to end and the only thing they stub is the CLI.

The refusals get the most attention here, because each one exists to prevent a SILENT
wrong deploy — a stray dag CES ignores, a stale flow shipped after a failed render, a
second app created because nobody named the first one. A refusal that stops refusing
looks exactly like success.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time

import pytest

from flows.config import models
from flows.deploy import argv, plan, render, runner, service, target, workdir
from flows.deploy.env import DeployEnv
from flows.deploy.errors import DagUnresolvedError, RenderFailedError
from flows.deploy.models import PrePushCheck, PrePushReport, PushConfigEntry
from flows.emit.models import ScaffoldFile


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------

def _code(tool: str) -> str:
    return f"tools/{tool}/python_function/python_code.py"


def _config(slot: str = "account_number") -> models.Config:
    return models.Config.model_validate(
        {"slots": [{"name": slot, "source": "user", "setter": f"set_{slot}"}], "tasks": []}
    )


def _app_files(*dags: str) -> list[ScaffoldFile]:
    files = [ScaffoldFile(path="app.json", content="{}")]
    for d in dags:
        files.append(ScaffoldFile(
            path=_code(d), content=f"def {d}():\n    return {{'slots': [], 'tasks': []}}\n"))
    return files


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
    """Install a fake runner for the duration of a test and restore the default.

    The default runner shells out, so leaking one of these would make a later test
    deploy for real. Restoring it is the point of the fixture.
    """
    r = FakeRunner(stdout="Successfully pushed to: projects/p/locations/us/apps/abc123")
    runner.set_runner(r)
    try:
        yield r
    finally:
        runner.set_runner(runner.default_push_runner)


def _green_gates(_req) -> PrePushReport:
    return PrePushReport(
        ok=True,
        checks=[PrePushCheck(id="validate_dag", ok=True, detail="ok")],
        target="create",
        target_label="x",
    )


# --------------------------------------------------------------------------------------
# Rendering is deterministic
# --------------------------------------------------------------------------------------

def test_rendering_the_same_config_twice_produces_the_same_bytes():
    """The parity test compares serialized requests, and a deploy is reproducible only
    if the render is. A renderer that embedded a timestamp/uuid/set-iteration order
    would make both claims false while every other test still passed."""
    files = _app_files("checkout_dag")
    first, dag_a = render.render_canvas_into_app_files(files, _config(), "checkout_dag")
    second, dag_b = render.render_canvas_into_app_files(files, _config(), "checkout_dag")
    assert dag_a == dag_b == "checkout_dag"
    assert [(f.path, f.content) for f in first] == [(f.path, f.content) for f in second]


def test_the_rendered_dag_actually_carries_the_authors_slot():
    """The 'I added a slot but it never deploys' bug: the dag shipped without the edit."""
    out, _dag = render.render_canvas_into_app_files(
        _app_files("checkout_dag"), _config("large_party_email"), "checkout_dag")
    body = next(f.content for f in out if f.path == _code("checkout_dag"))
    assert "large_party_email" in body


def test_a_render_that_drops_the_slot_is_refused(monkeypatch):
    """The self-check, not the happy path. If the renderer silently omits the author's
    slot we must RAISE — shipping the stale dag is the failure that looks like success."""
    from flows.config import config_io

    monkeypatch.setattr(config_io, "export_python", lambda cfg, dag: "def d():\n    pass\n")
    with pytest.raises(RenderFailedError) as exc:
        render.render_canvas_into_app_files(
            _app_files("checkout_dag"), _config("pin"), "checkout_dag")
    assert "pin" in str(exc.value)


def test_no_config_is_a_no_op_that_returns_the_caller_s_files():
    files = _app_files("checkout_dag")
    out, dag = render.render_canvas_into_app_files(files, None, None)
    assert out is files and dag is None


# --------------------------------------------------------------------------------------
# Dag resolution: never fabricate
# --------------------------------------------------------------------------------------

def test_a_single_dag_app_resolves_even_without_a_config_id():
    assert render.resolve_dag_tool(_app_files("only_dag"), None) == "only_dag"


def test_a_multi_dag_app_with_an_unknown_config_id_refuses():
    """Fabricating `{id}_dag` here is the silent 'push didn't take' bug: the edit lands
    in a tool CES has never heard of and the live agent is unchanged."""
    with pytest.raises(DagUnresolvedError) as exc:
        render.resolve_dag_tool(_app_files("a_dag", "b_dag"), "nope")
    assert exc.value.candidates == ["a_dag", "b_dag"]


def test_a_created_child_dag_is_registered_on_the_agent_that_owns_the_root():
    """A child flow tool nobody lists is uncallable. It goes on the agent hosting the
    parent — not bolted onto every agent in the app."""
    files = [
        ScaffoldFile(path="agents/Host/Host.json", content=json.dumps({"tools": ["root_dag"]})),
        ScaffoldFile(path="agents/Other/Other.json", content=json.dumps({"tools": ["misc"]})),
    ]
    out = render.register_dag_in_agents(files, "child_dag", "root_dag")
    host = json.loads(next(f.content for f in out if f.path == "agents/Host/Host.json"))
    other = json.loads(next(f.content for f in out if f.path == "agents/Other/Other.json"))
    assert host["tools"] == ["child_dag", "root_dag"]
    assert other["tools"] == ["misc"]  # untouched


def test_a_child_dag_is_registered_everywhere_when_no_agent_claims_the_root():
    """Fallback: the root dag is itself new, so no agent lists it yet. Registering
    nowhere would leave the child permanently unreachable."""
    files = [ScaffoldFile(path="agents/Only/Only.json", content=json.dumps({"tools": []}))]
    out = render.register_dag_in_agents(files, "child_dag", "root_dag")
    assert json.loads(out[0].content)["tools"] == ["child_dag"]


# --------------------------------------------------------------------------------------
# The tool walk the push gate uses
# --------------------------------------------------------------------------------------

def test_the_push_walk_sees_a_custom_cancel_tool():
    """The latent bug this refactor closes: the walk feeding codegen and the gates
    ignored cancel/escalate while THIS check demanded them, so a custom cancel tool
    could never be shipped. One walk now, and it sees all of them."""
    cfg = models.Config.model_validate({
        "slots": [{"name": "pin", "source": "user", "setter": "set_pin"}],
        "tasks": [{"name": "go", "tool": "go_tool", "terminal": True}],
        "cancel": {"tool": "custom_cancel", "requires_readback": False},
    })
    found = render.referenced_setter_task_tools([PushConfigEntry(config_id="f", config=cfg)])
    assert {"set_pin", "go_tool", "custom_cancel"} <= found


def test_the_push_walk_now_also_demands_on_exhaust_handlers():
    """A DELIBERATE widening, pinned so it is a decision and not an accident.

    The old push-gate walk stopped at setters/tasks/control-blocks, so a config whose
    retry handler names a tool the app doesn't have deployed happily and crashed the
    first time a slot exhausted. It is the same set codegen builds from now, so what
    the gate demands is exactly what the generator produces.
    """
    cfg = models.Config.model_validate({
        "slots": [{"name": "pin", "source": "user", "setter": "set_pin",
                   "validation": {"on_exhaust": {"then": {"tool": "bail_out"}}}}],
        "tasks": [],
    })
    assert "bail_out" in render.referenced_setter_task_tools(
        [PushConfigEntry(config_id="f", config=cfg)])


def test_a_string_then_names_a_tool_just_as_a_dict_one_does():
    """`then` is a string OR a dict — the validator accepts both and the engine
    resolves both (`"then": "recover"` -> `{"name": "recover", "args": {}}`). Only the
    dict form was walked, so a custom tool spelled the string way was generated by
    nobody, stub-checked by nobody, and then refused at push time for being absent."""
    cfg = models.Config.model_validate({
        "slots": [{"name": "pin", "source": "user", "setter": "set_pin",
                   "validation": {"on_exhaust": {"then": "recover_tool"}}}],
        "tasks": [],
    })
    assert "recover_tool" in render.referenced_setter_task_tools(
        [PushConfigEntry(config_id="f", config=cfg)])


def test_a_framework_disposition_spelled_as_a_string_is_not_a_phantom_tool():
    """The other side of that widening: `then: escalate` / `then: end_session` are the
    framework's OWN dispositions, not app tools. Demanding them would refuse every
    config that has ever used the common spelling."""
    cfg = models.Config.model_validate({
        "slots": [{"name": "pin", "source": "user", "setter": "set_pin",
                   "validation": {"on_exhaust": {"then": "escalate"}}},
                  {"name": "zip", "source": "user", "setter": "set_zip",
                   "validation": {"on_exhaust": {"then": "end_session"}}}],
        "tasks": [],
        "steer_back": {"on_exhaust": {"then": "transfer_to_human"}},
    })
    found = render.referenced_setter_task_tools([PushConfigEntry(config_id="f", config=cfg)])
    assert found == {"set_pin", "set_zip"}


def test_the_flow_level_silence_ladder_s_exhaust_tool_is_demanded():
    """`config.no_input` is a config-level key of its own — no slot or task walk
    reaches it — and its exhaust fires a tool exactly like any other
    (`_no_input_fc = _resolve_exhaust_action(...)` in the engine)."""
    cfg = models.Config.model_validate({
        "slots": [], "tasks": [],
        "no_input": {"reprompts": ["Still there?"],
                     "on_exhaust": {"say": "Bye", "then": {"tool": "silence_tool"}}},
    })
    assert "silence_tool" in render.referenced_setter_task_tools(
        [PushConfigEntry(config_id="f", config=cfg)])


def test_an_async_task_s_timeout_handler_is_demanded():
    """`awaits.on_timeout` carries the same disposition shape as `on_exhaust` — the
    validator runs `_check_on_exhaust` over it verbatim — so the tool it names has to
    be built and shipped like every other."""
    cfg = models.Config.model_validate({
        "slots": [],
        "tasks": [{"name": "poll", "tool": "poll_tool", "terminal": True,
                   "awaits": {"max_turns": 3,
                              "on_timeout": {"then": {"tool": "timeout_tool"}}}}],
    })
    found = render.referenced_setter_task_tools([PushConfigEntry(config_id="f", config=cfg)])
    assert {"poll_tool", "timeout_tool"} <= found


def test_the_push_walk_never_demands_a_dag_that_is_being_rendered():
    """A task whose tool is a sibling flow's dag is rendered in the same push; asking
    for it up front would refuse every component bundle."""
    cfg = models.Config.model_validate({
        "slots": [], "tasks": [{"name": "sub", "tool": "child_dag", "terminal": True}]})
    assert render.referenced_setter_task_tools(
        [PushConfigEntry(config_id="f", config=cfg)]) == set()


def test_missing_tools_reports_exactly_what_is_absent():
    files = _app_files("f_dag") + [ScaffoldFile(path=_code("set_pin"), content="def set_pin(): ...")]
    assert render.missing_tools(files, {"set_pin", "go_tool"}) == ["go_tool"]


def test_a_bundle_entry_is_keyed_by_flow_id_however_it_was_spelled():
    """Cross-config validation resolves a component reference by this key, so a
    ``_dag``-suffixed entry silently fails to satisfy the ref that names it. The
    normalization is on the model because the HTTP path never runs a constructor."""
    assert PushConfigEntry(config_id="checkout_dag", config=_config()).config_id == "checkout"
    assert PushConfigEntry(config_id="checkout", config=_config()).config_id == "checkout"


# --------------------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------------------

def test_a_stub_bodied_custom_cancel_tool_is_now_caught():
    """The other half of the unified walk. This gate used to enumerate tools with its
    own copy that skipped cancel/escalate, so a cancel tool that was a bare `pass`
    passed the gate and no-op'd at runtime."""
    from flows.deploy import gates
    from flows.deploy.models import PrePushRequest

    cfg = models.Config.model_validate({
        "slots": [{"name": "pin", "source": "user", "setter": "set_pin"}],
        "tasks": [],
        "cancel": {"tool": "custom_cancel", "requires_readback": False},
    })
    files = _app_files("f_dag") + [
        ScaffoldFile(path=_code("set_pin"),
                     content='def set_pin(pin):\n    return {"stored": True, "value": pin}\n'),
        ScaffoldFile(path=_code("custom_cancel"), content="def custom_cancel():\n    pass\n"),
    ]
    check = gates._check_tool_bodies(
        PrePushRequest(app_files=files, config=cfg, config_id="f"),
        {f.path: f.content for f in files},
        DeployEnv(),
    )
    assert check.ok is False and "custom_cancel" in check.detail


def test_the_lint_gate_blocks_a_tagged_partial_part():
    """FLV003 is the one voice finding that must stop a push: a tagged partial part
    truncates the utterance on composite, so the line meant to cover a wait is cut off.
    """
    from flows.deploy import gates
    from flows.deploy.models import PrePushRequest

    cfg = models.Config.model_validate({
        "slots": [{"name": "pin", "source": "user", "ask": "your pin?",
                   "response": [{"type": "text", "text": "[calm] one moment",
                                 "partial": True}]}],
        "tasks": [],
    })
    files = _app_files("f_dag")
    check = gates._check_lint(
        PrePushRequest(app_files=files, config=cfg, config_id="f"),
        {f.path: f.content for f in files},
        DeployEnv(),
    )
    assert check.ok is False
    assert check.severity == "error"
    assert "FLV003" in check.detail


def test_the_lint_gate_warns_but_does_not_block_a_plain_audio_tag():
    """A tag outside a partial part is model-dependent, not broken — worth saying,
    not worth refusing the push over."""
    from flows.deploy import gates
    from flows.deploy.models import PrePushRequest

    cfg = models.Config.model_validate({
        "slots": [{"name": "pin", "source": "user", "ask": "[calm] your pin?"}],
        "tasks": [],
    })
    files = _app_files("f_dag")
    check = gates._check_lint(
        PrePushRequest(app_files=files, config=cfg, config_id="f"),
        {f.path: f.content for f in files},
        DeployEnv(),
    )
    assert check.ok is True
    assert check.severity == "warning"
    assert "FLV002" in check.detail


def test_the_lint_gate_is_quiet_on_clean_copy():
    from flows.deploy import gates
    from flows.deploy.models import PrePushRequest

    cfg = models.Config.model_validate({
        "slots": [{"name": "pin", "source": "user", "ask": "what is your pin?"}],
        "tasks": [],
    })
    files = _app_files("f_dag")
    check = gates._check_lint(
        PrePushRequest(app_files=files, config=cfg, config_id="f"),
        {f.path: f.content for f in files},
        DeployEnv(),
    )
    assert check.ok is True and check.severity == "error"


def test_the_framework_s_own_control_tools_are_not_demanded():
    """Widening the walk must not start flagging `cancel_flow` — it ships with the
    blessed bundle and is not the author's to implement."""
    from flows.deploy import gates
    from flows.deploy.models import PrePushRequest

    cfg = models.Config.model_validate({
        "slots": [], "tasks": [],
        "cancel": {"tool": "cancel_flow", "requires_readback": False},
    })
    files = _app_files("f_dag") + [
        ScaffoldFile(path=_code("cancel_flow"), content="def cancel_flow():\n    pass\n")]
    check = gates._check_tool_bodies(
        PrePushRequest(app_files=files, config=cfg, config_id="f"),
        {f.path: f.content for f in files},
        DeployEnv(),
    )
    assert check.ok is True


# --------------------------------------------------------------------------------------
# Create vs update
# --------------------------------------------------------------------------------------

_LEAF = "5661c4de-b36f-484a-a1c7-7eda691460a8"


def test_a_bare_app_id_expands_to_a_full_resource_name():
    """`cxas push --to` needs a resource name or a display name; a bare leaf is neither
    and the push silently targets nothing."""
    env = DeployEnv(project="p", location="us")
    assert target.resolve_to_target(_LEAF, env) == f"projects/p/locations/us/apps/{_LEAF}"


def test_a_resource_name_or_display_name_passes_through_untouched():
    env = DeployEnv(project="p", location="us")
    full = f"projects/p/locations/us/apps/{_LEAF}"
    assert target.resolve_to_target(full, env) == full
    assert target.resolve_to_target("My Display Name", env) == "My Display Name"
    assert target.resolve_to_target(None, env) is None


def test_a_known_deployment_updates_and_a_named_one_creates():
    env = DeployEnv(project="p", location="us")
    to, name = target.decide(to=None, deployed_app_id=_LEAF, display_name="X", env=env)
    assert to == f"projects/p/locations/us/apps/{_LEAF}" and name is None
    to, name = target.decide(to=None, deployed_app_id=None, display_name="X", env=env)
    assert to is None and name == "X"


def test_a_first_push_with_no_name_refuses_rather_than_guessing_one():
    """A defaulted name is how you end up with three apps called 'config' and no idea
    which one Live mode is driving."""
    with pytest.raises(target.FirstPushNeedsDisplayName):
        target.decide(to=None, deployed_app_id=None, display_name=None,
                      env=DeployEnv(project="p", location="us"))


def test_the_legacy_config_only_path_still_passes_to_through_verbatim():
    """strict=False is the pre-existing behaviour for a config-only push: no create
    gate, `to` (even None) forwarded as-is."""
    assert target.decide(to=None, deployed_app_id=None, display_name=None,
                         env=DeployEnv(), strict=False) == (None, None)


# --------------------------------------------------------------------------------------
# service.push — the refusals, end to end
# --------------------------------------------------------------------------------------

def _push(spec, env=None, **kw):
    return asyncio.run(service.push(spec, env or DeployEnv(project="p", location="us"), **kw))


def test_a_first_push_without_a_display_name_is_refused_before_the_subprocess(fake_cxas):
    spec = plan.build_push_spec(app_files=_app_files("f_dag"), run_gates=False)
    out = _push(spec)
    assert out.ok is False
    assert "display name" in out.error
    assert fake_cxas.calls == []  # never reached the CLI


def test_a_bundle_referencing_an_absent_tool_is_refused(fake_cxas):
    cfg = models.Config.model_validate({
        "slots": [{"name": "pin", "source": "user", "setter": "set_pin"}], "tasks": []})
    spec = plan.build_push_spec(
        app_files=_app_files("f_dag"), configs=[{"config_id": "f", "config": cfg}],
        config_id="f", display_name="X", run_gates=False)
    out = _push(spec)
    assert out.error_kind == "tool_unresolved"
    assert "set_pin" in out.error
    assert fake_cxas.calls == []


def test_a_bundle_that_fails_cross_validation_is_refused(fake_cxas):
    """component_invalid: an unresolvable component ref would deploy an app whose flow
    calls a flow that isn't there."""
    cfg = models.Config.model_validate({
        "slots": [{"name": "sub", "source": "component:nope", "component": "nope"}],
        "tasks": []})
    spec = plan.build_push_spec(
        app_files=_app_files("f_dag"), configs=[{"config_id": "f", "config": cfg}],
        config_id="f", display_name="X", run_gates=False)
    out = _push(spec)
    assert out.error_kind == "component_invalid"
    assert fake_cxas.calls == []


def test_failing_gates_block_the_push(fake_cxas):
    def _red(_req):
        return PrePushReport(
            ok=False,
            checks=[PrePushCheck(id="validate_dag", ok=False, detail="broken")],
            target="create", target_label="X")

    spec = plan.build_push_spec(app_files=_app_files("f_dag"), display_name="X")
    out = _push(spec, gate_runner=_red)
    assert out.error_kind == "gate_failed"
    assert out.gate_report.checks[0].detail == "broken"
    assert fake_cxas.calls == []


def test_a_successful_push_reports_what_shipped_and_cleans_up(fake_cxas, monkeypatch):
    """The scratch dir is a tempdir we own; leaking one per push fills the disk of a
    long-running control plane."""
    made: list[str] = []
    real_materialize = workdir.materialize
    monkeypatch.setattr(workdir, "materialize",
                        lambda files: made.append(real_materialize(files)) or made[-1])

    spec = plan.build_push_spec(
        app_files=_app_files("checkout_dag"), config=_config(), config_id="checkout_dag",
        display_name="Checkout", overwrite=True)
    out = _push(spec, gate_runner=_green_gates)

    assert out.ok is True
    assert out.dag == "checkout_dag"
    assert out.rendered_slots == ["account_number"]
    assert out.deployed_app_id == "abc123"
    assert fake_cxas.calls[0][:3] == ["push", "--app-dir", made[0]]
    assert "--display-name" in fake_cxas.calls[0] and "--overwrite" in fake_cxas.calls[0]
    assert not os.path.exists(made[0])


def test_a_nonzero_exit_is_classified_and_surfaced():
    r = FakeRunner(rc=1, stdout="Error: Tools not found (404)")
    runner.set_runner(r)
    try:
        spec = plan.build_push_spec(
            app_files=_app_files("checkout_dag"), config=_config(),
            config_id="checkout_dag", display_name="X")
        out = _push(spec, gate_runner=_green_gates)
    finally:
        runner.set_runner(runner.default_push_runner)
    assert out.ok is False and out.error_kind == "dup_uuid"


def test_a_failure_reports_the_real_error_and_not_an_import_time_warning():
    """A custom runner's stderr never passed through the default runner's filter, and
    the copy of the noise list here knew about fewer warning classes than that filter
    did — so a DeprecationWarning became the error the author was shown. One list now."""
    r = FakeRunner(rc=1, stdout="", stderr=(
        "/x/pydub/utils.py:170: RuntimeWarning: Couldn't find ffmpeg\n"
        "\n"
        "/x/p.py:1: DeprecationWarning: ssl module is deprecated\n"
        "  warnings.warn('deprecated')\n"
        "PermissionDenied: caller lacks aiplatform.apps.create\n"))
    runner.set_runner(r)
    try:
        spec = plan.build_push_spec(
            app_files=_app_files("checkout_dag"), config=_config(),
            config_id="checkout_dag", display_name="X")
        out = _push(spec, gate_runner=_green_gates)
    finally:
        runner.set_runner(runner.default_push_runner)
    assert out.ok is False
    assert out.error == "PermissionDenied: caller lacks aiplatform.apps.create"


def test_a_timeout_is_reported_rather_than_hanging_the_caller():
    async def _slow(_argv):
        await asyncio.sleep(10)
        return 0, "", ""

    runner.set_runner(_slow)
    try:
        spec = plan.build_push_spec(
            app_files=_app_files("checkout_dag"), config=_config(),
            config_id="checkout_dag", display_name="X")
        out = _push(spec, gate_runner=_green_gates, timeout_s=0.01)
    finally:
        runner.set_runner(runner.default_push_runner)
    assert out.ok is False and out.error_kind == "timeout"


# --------------------------------------------------------------------------------------
# argv + output parsing
# --------------------------------------------------------------------------------------

def test_overwrite_is_only_emitted_when_asked_for():
    """Without --overwrite an UPDATE is a partial MERGE: CES never creates the tools
    added since the last push, so the deployed config references setters that do not
    exist. With it, the tool set is reconciled."""
    assert "--overwrite" not in argv.build_push_argv("/app", to="x")
    assert "--overwrite" in argv.build_push_argv("/app", to="x", overwrite=True)


def test_the_deployed_app_id_is_read_back_out_of_the_push_output():
    out = "Successfully pushed to: projects/p/locations/us/apps/xyz-1"
    assert argv.parse_deployed_app_id(out) == "xyz-1"
    assert argv.parse_app_name(out) == "projects/p/locations/us/apps/xyz-1"


# --------------------------------------------------------------------------------------
# The subprocess seam: a killed deploy leaves nothing running
# --------------------------------------------------------------------------------------

def _spawns_a_grandchild(tmp_path):
    """A shell that forks a long-lived GRANDCHILD, records its pid, and then waits.

    Stands in for `cxas push`, which shells out to terraform: what has to die on a
    timeout is the TREE, not just the process we launched.
    """
    pid_file = tmp_path / "grandchild.pid"
    return f"sleep 30 & echo $! > {pid_file}; sleep 30", pid_file


def _read_pid(pid_file, deadline: float = 5.0) -> int:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text().strip())
        time.sleep(0.02)
    raise AssertionError(f"{pid_file} never got a pid")


def _is_dead(pid: int, deadline: float = 5.0) -> bool:
    """True once `pid` is gone. Polled: after the group is signalled the orphan is
    reparented and reaped by init, which is not instantaneous."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.02)
    return False


def test_killing_the_deploy_reaps_the_whole_tree_not_just_the_lead_process(tmp_path):
    """`start_new_session=True` gives the deploy its own process group. Signalling only
    the lead process (all `Popen.kill` — and so `subprocess.run`'s timeout — does)
    orphans everything it spawned: terraform would keep applying against the target
    long after we reported the push as timed out."""
    script, pid_file = _spawns_a_grandchild(tmp_path)
    proc = subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True)
    try:
        grandchild = _read_pid(pid_file)
        runner.kill_process_group(proc)
        assert _is_dead(grandchild), "the spawned child outlived the kill"
        # The lead process too — it stays a zombie until we reap it, so check the status.
        assert proc.wait(timeout=5) == -signal.SIGKILL
    finally:
        runner.kill_process_group(proc)
        proc.wait()


def test_a_subprocess_timeout_kills_the_children_it_spawned(monkeypatch, tmp_path):
    script, pid_file = _spawns_a_grandchild(tmp_path)
    monkeypatch.setattr(runner, "cxas_argv_prefix", lambda: (["/bin/sh", "-c", script], ""))
    monkeypatch.setattr(runner, "_PUSH_SUBPROCESS_TIMEOUT_S", 0.5)
    rc, _out, err = asyncio.run(runner.default_push_runner(["push"]))
    assert rc == 124 and "timed out" in err
    assert _is_dead(_read_pid(pid_file)), "the deploy timed out but its children ran on"


def test_a_caller_that_gives_up_first_also_kills_the_subprocess(monkeypatch, tmp_path):
    """The worker thread is un-cancellable, so when `run_push`'s own (shorter) timeout
    fires, nothing was stopping the subprocess — it kept deploying after the caller had
    already been told it timed out."""
    script, pid_file = _spawns_a_grandchild(tmp_path)
    monkeypatch.setattr(runner, "cxas_argv_prefix", lambda: (["/bin/sh", "-c", script], ""))

    async def _drive():
        with pytest.raises(asyncio.TimeoutError):
            await runner.run_push(["push"], timeout_s=1.0)
        # Checked INSIDE the loop: `asyncio.run` shuts its default executor down on the
        # way out, which waits for the un-cancellable thread — i.e. for the subprocess
        # to end on its own. Asserting after that would pass however this behaved.
        assert _is_dead(_read_pid(pid_file), deadline=3.0), (
            "the abandoned deploy was left running")

    asyncio.run(_drive())


# --------------------------------------------------------------------------------------
# The materializer's sandbox
# --------------------------------------------------------------------------------------

def test_the_deploy_package_does_not_shadow_its_own_push_submodule():
    """`flows.deploy.push` is the app-dir CLI path. Re-exporting `service.push` under
    that name made `from flows.deploy import push` resolve to the function in a fresh
    process and to the module once anything had imported the submodule — a bug that
    only shows up in whichever import order production happens to have."""
    import flows.deploy as deploy
    import flows.deploy.push as push_module

    assert "push" not in deploy.__all__
    assert deploy.push is push_module


def test_a_null_optional_is_dropped_but_a_null_required_field_still_raises():
    """Parity must not be bought by making `location=None` mean `"us"`."""
    from pydantic import ValidationError

    base = dict(app_display_name="A", config_id="c", root_agent="R", gcp_project="p")
    assert plan.build_scaffold_request({**base, "target_path": None}).target_path is None
    with pytest.raises(ValidationError):
        plan.build_scaffold_request({**base, "location": None})


def test_a_payload_cannot_write_outside_the_scratch_dir():
    """App files are DATA, not a trusted path list."""
    scratch = workdir.materialize([
        ScaffoldFile(path="../escaped.txt", content="no"),
        ScaffoldFile(path="/abs.txt", content="also no"),
        ScaffoldFile(path="tools/t/python_function/python_code.py", content="yes"),
    ])
    try:
        found = {os.path.relpath(os.path.join(dp, f), scratch)
                 for dp, _dn, fn in os.walk(scratch) for f in fn}
        assert found == {"escaped.txt", "abs.txt",
                         "tools/t/python_function/python_code.py"}
        assert not os.path.exists(os.path.join(os.path.dirname(scratch), "escaped.txt"))
    finally:
        workdir.cleanup(scratch)
