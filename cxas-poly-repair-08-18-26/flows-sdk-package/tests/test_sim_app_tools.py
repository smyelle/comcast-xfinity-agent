"""Simulating a DEPLOYED agent: the engine must run the AGENT's tools, not ours.

`engine_sim` loads every setter and executor from a framework root, and the packaged
root only holds the framework's own tools. A fetched agent's `set_active_flow` is the
*app's* tool, so the first setter call used to die with

    FileNotFoundError: Framework tool 'set_active_flow' not found under .../framework/tools

which is the whole difference between "we can simulate what this agent does" and "we
can only write dialogue about it". These tests pin the two pieces that close it:
a tools root carrying the app's own sources, and a per-session root so two agents
never share a module cache.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows.engine import loader as fb  # noqa: E402
import flows.sim.engine_sim as engine_sim  # noqa: E402

# An app's setter. Note the parameter is `flow` while the config's bootstrap names
# the SLOT `active_flow` -- the mismatch that makes `tool_parameters` necessary.
SET_ACTIVE_FLOW = '''
def set_active_flow(flow: str = ""):
    if flow not in ("tracking", "pickup"):
        return {"error": True, "error_code": "invalid_flow"}
    return {"stored": True, "value": flow}
'''

SET_TRACKING_NUMBER = '''
def set_tracking_number(tracking_number: str = ""):
    v = str(tracking_number).strip()
    if not v:
        return {"error": True, "error_code": "missing"}
    return {"stored": True, "value": v}
'''

# Same NAME as a framework tool, different behaviour: the deployed copy must win.
TRY_AGAIN = '''
def try_again():
    return {"marker": "the app's own try_again"}
'''


def app_sources():
    return {
        "set_active_flow": SET_ACTIVE_FLOW,
        "set_tracking_number": SET_TRACKING_NUMBER,
        "try_again": TRY_AGAIN,
    }


def config():
    return {
        "_config_id": "tracking",
        "gate_slot": "active_flow",
        "bootstrap": {"tool": "set_active_flow", "slot": "active_flow"},
        "slots": [
            {"name": "active_flow", "source": "user", "setter": "set_active_flow"},
            {"name": "tracking_number", "source": "user",
             "setter": "set_tracking_number", "ask": "What's your tracking number?"},
        ],
        "tasks": [],
    }


@pytest.fixture
def root(tmp_path):
    # `parent=` so pytest owns the cleanup: materialize_tools_root mkdtemp()s and
    # its docstring puts the directory on the caller, so a bare call leaks a tools
    # root (framework symlinks + the app's sources) per test, every run.
    path = fb.materialize_tools_root(app_sources(), parent=str(tmp_path))
    yield path
    fb.clear_cache()


def test_app_tools_are_loadable_alongside_the_framework(root):
    """The root carries BOTH: the app's setters and the framework's own tools."""
    assert fb.load_tool_callable("set_active_flow", root)("tracking") == {
        "stored": True, "value": "tracking"}
    # `slot_filling_engine` is infrastructure a CES fetch filters out, so it can only
    # come from the framework bundle -- if the overlay dropped it there is no engine.
    assert fb.load_engine(root) is not None


def test_the_deployed_copy_wins_a_name_collision(root):
    """An agent ships its own `try_again`; the deployed one is what we must run."""
    assert fb.load_tool_callable("try_again", root)() == {
        "marker": "the app's own try_again"}


def test_tool_parameters_reads_the_signature_not_the_config(root):
    """The config's slot is `active_flow`; the setter's parameter is `flow`.

    Calling with the config's name raises TypeError and the simulated turn silently
    does nothing, so a caller building a setter call has to ask the function.
    """
    assert fb.tool_parameters("set_active_flow", root) == ["flow"]


def test_a_session_steps_through_the_app_s_own_setters(root):
    """The failing case, end to end: start, gate, then a real setter call."""
    engine_sim.reset_store()
    cfg = config()
    session_id, first = engine_sim.start(
        cfg, flow_id="tracking", configs={"tracking": cfg}, framework_root=root)
    assert first["next_action"] == "gate"

    gated = engine_sim.step({
        "session_id": session_id, "kind": "setter_call",
        "tool": "set_active_flow", "args": {"flow": "tracking"},
    })
    assert gated["sm"]["filled"]["active_flow"] == "tracking"
    assert "What's your tracking number?" in gated["agent_text"]

    answered = engine_sim.step({
        "session_id": session_id, "kind": "setter_call",
        "tool": "set_tracking_number", "args": {"tracking_number": "482938471023"},
    })
    assert answered["sm"]["filled"]["tracking_number"] == "482938471023"


def test_without_a_root_the_app_s_setter_is_simply_not_there():
    """The regression this fixes: the packaged root cannot serve an app's tool."""
    engine_sim.reset_store()
    cfg = config()
    session_id, _ = engine_sim.start(cfg, flow_id="tracking", configs={"tracking": cfg})
    with pytest.raises(FileNotFoundError):
        engine_sim.step({
            "session_id": session_id, "kind": "setter_call",
            "tool": "set_active_flow", "args": {"flow": "tracking"},
        })


def test_two_agents_do_not_share_a_module_cache(tmp_path):
    """A root is the cache key, so same-named tools in two agents must not collide."""
    one = fb.materialize_tools_root({"try_again": TRY_AGAIN}, parent=str(tmp_path))
    two = fb.materialize_tools_root(
        {"try_again": 'def try_again():\n    return {"marker": "the other agent"}\n'},
        parent=str(tmp_path))
    assert fb.load_tool_callable("try_again", one)()["marker"] == \
        "the app's own try_again"
    assert fb.load_tool_callable("try_again", two)()["marker"] == "the other agent"
    fb.clear_cache()


def test_a_tool_name_cannot_escape_the_root(tmp_path):
    """Tool names come off a third-party deployment; they are directory names."""
    root = fb.materialize_tools_root(
        {"../evil": "x = 1", "": "x = 1", "ok": "y = 2"}, parent=str(tmp_path))
    assert os.path.isdir(os.path.join(root, "ok"))
    assert not os.path.exists(os.path.join(os.path.dirname(root), "evil"))
