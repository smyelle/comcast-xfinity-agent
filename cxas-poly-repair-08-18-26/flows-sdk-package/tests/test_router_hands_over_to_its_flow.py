"""A router's caller must ARRIVE somewhere.

A router fills its gate with the flow the caller chose and then has nothing left
to do — it collects nothing and runs no task. The engine only swaps config for a
COMPONENT descent (`_call_stack`), so a session that starts on a host stayed on
the host: every further turn re-offered the same choice, and the caller picked a
destination forever without reaching one.

Live, CES resolves this OUTSIDE the engine — `before_agent` maps the active flow
to its config and invokes the engine with that config and the same `sm`. These
pin the simulator doing the same, and only that.

Run: PYTHONPATH=packages/flows/src pytest \
    packages/flows/tests/test_router_hands_over_to_its_flow.py
"""

from __future__ import annotations

from flows.engine import loader as fb
from flows.sim import engine_sim


def _host() -> dict:
    """A router: no slots, no tasks, a gate and the flows it can reach."""
    return {
        "router": True,
        "gate_slot": "active_flow",
        "flow_types": ["billing"],
        "bootstrap": {"tool": "set_active_flow", "slot": "active_flow"},
        "slots": [],
        "tasks": [],
    }


def _child() -> dict:
    return {
        "slots": [{"name": "account", "source": "user", "setter": "set_account",
                   "ask": "What is your account number?"}],
        "tasks": [],
    }


def _start():
    return engine_sim.start(
        _host(), flow_id="host", configs={"host": _host(), "billing": _child()},
    )


def test_filling_the_gate_hands_the_session_to_that_flow():
    engine_id, _ = _start()
    session = engine_sim._SESSIONS[engine_id]
    assert session.config is not None and not session.config.get("slots")

    session.sm.setdefault("filled", {})["active_flow"] = "billing"
    switched = engine_sim._follow_flow_switch(session, session.sm)

    assert switched == "billing"
    # The session is now RUNNING the child, not the host.
    assert [s["name"] for s in session.config.get("slots") or []] == ["account"]


def test_the_compiled_config_key_changes_with_the_config():
    """The engine caches compiled configs by id. Reusing the host's key would run
    the host's compiled form under the child's content."""
    engine_id, _ = _start()
    session = engine_sim._SESSIONS[engine_id]
    before = session.config_id
    session.sm.setdefault("filled", {})["active_flow"] = "billing"
    engine_sim._follow_flow_switch(session, session.sm)
    assert session.config_id != before
    assert "billing" in session.config_id


def test_an_unresolvable_destination_is_left_alone():
    """An agent maps flow types to configs through app-level state a DAG does not
    carry — Bella Notte routes to `reservation`, whose config is `bella_notte`. A
    target we cannot resolve by exact id must not be matched to something close."""
    engine_id, _ = _start()
    session = engine_sim._SESSIONS[engine_id]
    host = session.config
    session.sm.setdefault("filled", {})["active_flow"] = "reservation"
    assert engine_sim._follow_flow_switch(session, session.sm) is None
    assert session.config is host


def test_it_does_not_switch_again_once_it_has_arrived():
    """The gate stays filled after the handoff; re-switching every turn would
    reset the child's compiled config forever."""
    engine_id, _ = _start()
    session = engine_sim._SESSIONS[engine_id]
    session.sm.setdefault("filled", {})["active_flow"] = "billing"
    assert engine_sim._follow_flow_switch(session, session.sm) == "billing"
    key = session.config_id
    assert engine_sim._follow_flow_switch(session, session.sm) is None
    assert session.config_id == key


def test_a_flow_with_no_gate_never_switches():
    """Only a gated flow routes. An ordinary journey must be untouched by this."""
    engine_id, _ = engine_sim.start(_child(), flow_id="billing")
    session = engine_sim._SESSIONS[engine_id]
    session.sm.setdefault("filled", {})["active_flow"] = "billing"
    assert engine_sim._follow_flow_switch(session, session.sm) is None


SET_ACTIVE_FLOW = (
    "def set_active_flow(flow: str = \"\"):\n"
    "    return {'stored': True, 'value': flow}\n"
)


def test_stepping_the_router_setter_actually_hands_over(tmp_path):
    """The wiring, not just the helper.

    The tests above call `_follow_flow_switch` directly, so they stay green even
    if nothing ever calls it. This drives a real `step`, which is the thing that
    was broken: the caller picked a destination and the next turn re-offered the
    same choice.

    `set_active_flow` is the APP's tool, not a framework one, so the session needs
    a tools root carrying it. `parent=tmp_path` because materialize_tools_root
    mkdtemp()s and hands the directory to the caller.
    """
    root = fb.materialize_tools_root({"set_active_flow": SET_ACTIVE_FLOW},
                                     parent=str(tmp_path))
    engine_id, _ = engine_sim.start(
        _host(), flow_id="host", configs={"host": _host(), "billing": _child()},
        framework_root=root,
    )
    session = engine_sim._SESSIONS[engine_id]

    out = engine_sim.step({
        "session_id": engine_id, "kind": "setter_call",
        "tool": "set_active_flow", "args": {"flow": "billing"},
    })
    fb.clear_cache()

    assert out.get("switched_to_flow") == "billing"
    assert [s["name"] for s in session.config.get("slots") or []] == ["account"]


def _switched_session():
    """A session that has already been handed over to `billing`."""
    engine_id, _ = _start()
    session = engine_sim._SESSIONS[engine_id]
    session.sm.setdefault("filled", {})["active_flow"] = "billing"
    assert engine_sim._follow_flow_switch(session, session.sm) == "billing"
    return engine_id, session


def test_stepping_back_past_the_handoff_returns_to_the_ROUTER():
    """Which flow is running is turn state, so step-back has to rewind it.

    `_Snapshot` says everything a step advances belongs in it. The handoff swaps
    `config`/`config_id` and they were left out, so a step-back rewound the sm
    while the session stayed in the child: the caller is put back at the router's
    question and the engine answers it out of the child's config.
    """
    engine_id, session = _switched_session()
    # A snapshot taken BEFORE the switch is what a real step-back pops.
    engine_sim._push_history(session)
    session.config, session.config_id = _child(), "pretend-child"

    engine_sim.back(engine_id)
    assert session.config_id != "pretend-child"
    assert [s["name"] for s in session.config.get("slots") or []] == ["account"]


def test_reset_returns_to_the_flow_the_session_STARTED_on():
    """Otherwise a reset drops the caller into whichever child they last chose,
    which is not the opening of the conversation."""
    engine_id, session = _switched_session()
    engine_sim.reset(engine_id)
    assert not session.config.get("slots"), "reset should be back on the router"
    assert session.config.get("router") is True


def test_a_non_dict_filled_does_not_crash_the_turn():
    """`sm` comes back from the engine; a truthy non-dict `filled` would have hit
    `.get` on a list and taken the turn down with an AttributeError."""
    engine_id, _ = _start()
    session = engine_sim._SESSIONS[engine_id]
    session.sm["filled"] = ["not", "a", "dict"]
    assert engine_sim._follow_flow_switch(session, session.sm) is None
