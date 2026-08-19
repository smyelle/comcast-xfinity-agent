"""Escalate with a pre-terminal task chain.

When a caller asks for a human, the engine's escalate rail fires the marker tool
`transfer_to_human`, speaks `escalate.say` and ends the session. It never runs the
app's own hand-off pair, so the human on the other end receives no summary.

`escalate={"say": ..., "tasks": [...]}` inserts those tasks BEFORE the existing
disposition: while the chain is in flight the DAG walk is restricted to exactly its
members, and once the chain is spent the unchanged `_terminate_control` runs.

Everything here is offline via `flows.sim.engine_sim` (no LLM / no creds). A `fire`
in the simulator does not execute the tool, which is what lets these drive each
member's result — or withhold it — one turn at a time.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_escalate_path.py
"""

from __future__ import annotations

import flows
from flows.engine import blessed_source
from flows.sim import engine_sim


ACCOUNT = {"account": "8069100230361049"}

SAY = "Let me get you to someone who can help."

CHAIN = ["PrepareHandoff", "HandOff"]

TOOLS = ["diagnose", "prepare_handoff", "hand_off"]


def _build_config(
    *,
    tasks: list[str] | None = None,
    requires_readback: bool = False,
    prepare_condition: str | None = None,
    prepare_then_say: str | None = None,
    handoff_filler_say: str | None = None,
    handoff_on_failure: dict | None = None,
    handoff_awaits: dict | None = None,
    handoff_terminal: bool = False,
) -> dict:
    """A spine plus a hand-off pair, shaped like a cable-repair flow.

    `Diagnostics` is eligible the moment the account is prefilled, so in an
    unrestricted walk it wins the declaration-order race against anything the chain
    wants to fire. That is what makes the scoping observable rather than incidental.
    """
    f = flows.Flow("esc_test", root_agent="Esc_Agent")
    f.add(
        flows.event_slot("account"),
        flows.result_slot("diagnosis", "Diagnostics"),
        flows.result_slot("summary", "PrepareHandoff"),
        flows.result_slot("handoff_ack", "HandOff"),
    )
    f.task("Diagnostics", "diagnose", ["account"], "diagnosis")
    f.task("PrepareHandoff", "prepare_handoff", ["account"], "summary",
           condition=prepare_condition or flows.escalated(),
           then_say=prepare_then_say)
    # Built as a raw dict, not via task(): `filler_say` is a runtime task key the
    # builder does not expose, so this is the form an author uses for it too.
    handoff = flows.task("HandOff", "hand_off", ["summary"], "handoff_ack",
                         on_failure=handoff_on_failure,
                         terminal=handoff_terminal)
    if handoff_filler_say is not None:
      handoff["filler_say"] = handoff_filler_say
    if handoff_awaits is not None:
      handoff["awaits"] = handoff_awaits
    f.task(handoff)
    f.add(flows.announce("verdict", ["Here is what I found: {diagnosis}."],
                         requires=["diagnosis"], end=True))
    f.set("escalate", flows.escalate(
        say=SAY, tasks=tasks, requires_readback=requires_readback))
    return f.to_config()


def _start(config: dict) -> tuple[str, dict]:
    engine_sim.reset_store()
    return engine_sim.start(config, "esc_test", event_data=ACCOUNT)


def _step(session_id: str, **kw) -> dict:
    return engine_sim.step({"session_id": session_id, **kw})


def _fired(result: dict) -> str:
    return ((result.get("function_call") or {}).get("name")) or ""


def _ask_for_a_human(session_id: str) -> dict:
    return _step(session_id, kind="setter_call", tool="transfer_to_human",
                 args={"reason": "wants a person"})


def _resolve(session_id: str, task: str, **outputs) -> dict:
    return _step(session_id, kind="task_result", task_name=task,
                 success=True, result=outputs)


def _assert_transferred(result: dict) -> None:
    """The disposition, byte-for-byte as the chain-less rail produces it."""
    assert result["agent_text"] == SAY
    assert result["function_call"] is None
    assert result["status"] == "zombie"
    assert result["next_action"] == "terminal"
    assert result["response_parts"] == [
        {"type": "end_session", "reason": "transfer", "escalated": True},
    ]
    assert "_escalate_path" not in result["sm"]
    assert "_escalate_ticks" not in result["sm"]


# ── The unchanged rail ───────────────────────────────────────────────────────


def test_plain_escalate_is_unchanged():
    """The regression pin, captured from the engine BEFORE the chain existed.

    Every deployed agent takes this path (no `escalate.tasks`), so the literals
    below are the backwards-compatibility contract, not a description.
    """
    session_id, _opening = _start(_build_config())
    out = _ask_for_a_human(session_id)

    assert out["agent_text"] == SAY
    assert out["function_call"] is None
    assert out["status"] == "zombie"
    assert out["next_action"] == "terminal"
    assert out["response_parts"] == [
        {"type": "end_session", "reason": "transfer", "escalated": True},
    ]
    # classify_turn_intent is now hidden on every Pass-B / gate / terminal turn (it is a
    # Pass-A-only tool — see _hiding_policy). It is added UNCONDITIONALLY, so it appears here
    # even for this non-intent-first flow that never declares it; at runtime hiding a name the
    # agent did not declare is a no-op, so the behavior for deployed agents is byte-identical.
    assert out["hide_tools"] == [
        "classify_turn_intent", "end_session", "new_flow_instance", "resume_flow",
        "set_intent_changed",
    ]
    assert "_escalate_path" not in out["sm"]


def test_cancel_never_takes_the_chain():
    """`cancel` has no chain (the validator forbids one); it must still terminate
    on the turn its setter lands."""
    session_id, _opening = _start(_build_config(tasks=CHAIN))
    out = _step(session_id, kind="setter_call", tool="cancel_flow", args={})

    assert out["function_call"] is None
    assert out["status"] == "zombie"
    assert "_escalate_path" not in out["sm"]


# ── The chain ────────────────────────────────────────────────────────────────


def test_the_first_member_fires_on_the_turn_the_caller_asks():
    """A top-level executor call, not a nested one — an evaluation that asserts
    the hand-off tool ran can only see it if the engine issues it itself."""
    session_id, _opening = _start(_build_config(tasks=CHAIN))
    out = _ask_for_a_human(session_id)

    assert out["function_call"] == {
        "name": "prepare_handoff", "args": {"account": ACCOUNT["account"]}}
    assert out["status"] == "in_progress"
    assert out["sm"]["_escalate_path"] == CHAIN


def test_the_chain_runs_in_the_authored_order_then_transfers():
    session_id, _opening = _start(_build_config(tasks=CHAIN))
    assert _fired(_ask_for_a_human(session_id)) == "prepare_handoff"

    handing_off = _resolve(session_id, "PrepareHandoff", summary="gateway offline")
    assert handing_off["function_call"] == {
        "name": "hand_off", "args": {"summary": "gateway offline"}}

    _assert_transferred(_resolve(session_id, "HandOff", handoff_ack="ok"))


def test_the_spine_cannot_fire_while_the_chain_is_in_flight():
    """`Diagnostics` is fireable on the escalate turn and is declared first, so
    without walk scoping it — not the chain — takes the fire."""
    session_id, opening = _start(_build_config(tasks=CHAIN))
    assert _fired(opening) == "diagnose", "the spine is not actually eligible"

    escalating = _ask_for_a_human(session_id)
    assert _fired(escalating) == "prepare_handoff"
    mid_chain = _resolve(session_id, "PrepareHandoff", summary="s")
    assert _fired(mid_chain) == "hand_off"


def test_the_chain_speaks_a_members_then_say_and_the_next_filler():
    """The members are ordinary tasks, so the ordinary post-executor copy runs."""
    session_id, _opening = _start(_build_config(
        tasks=CHAIN,
        prepare_then_say="I have your details.",
        handoff_filler_say="One moment."))
    _ask_for_a_human(session_id)

    handing_off = _resolve(session_id, "PrepareHandoff", summary="s")
    assert handing_off["agent_text"] == "I have your details. One moment."


def test_a_chain_that_cannot_start_falls_straight_through_to_the_disposition():
    """No member is eligible (the gate is false and nothing produces `summary`),
    so the caller reaches a human on the same turn rather than stalling."""
    session_id, _opening = _start(_build_config(
        tasks=CHAIN, prepare_condition=flows.eq("account", "someone else")))

    _assert_transferred(_ask_for_a_human(session_id))


def test_requires_readback_confirms_before_the_chain_arms():
    session_id, _opening = _start(
        _build_config(tasks=CHAIN, requires_readback=True))

    confirming = _ask_for_a_human(session_id)
    assert confirming["function_call"] is None
    assert confirming["status"] == "in_progress"
    assert "_escalate_path" not in confirming["sm"]

    agreed = _step(session_id, kind="user_text", text="yes")
    assert _fired(agreed) == "prepare_handoff"


def test_the_chain_is_disarmed_once_it_is_spent():
    session_id, _opening = _start(_build_config(tasks=CHAIN))
    _ask_for_a_human(session_id)
    _resolve(session_id, "PrepareHandoff", summary="s")
    done = _resolve(session_id, "HandOff", handoff_ack="ok")

    assert "_escalate_path" not in done["sm"]
    assert "_escalate_ticks" not in done["sm"]


# ── When the chain misbehaves ────────────────────────────────────────────────


def test_a_wedged_chain_is_capped_and_the_caller_still_reaches_a_human():
    """A member whose result never lands re-fires every turn. Escalation is the
    one rail that must not be able to trap a caller, so the tick bound gives up
    and runs the disposition."""
    session_id, _opening = _start(_build_config(tasks=CHAIN))
    result = _ask_for_a_human(session_id)

    for _ in range(_max_ticks() + 2):
        if result["status"] == "zombie":
            break
        result = _step(session_id, kind="user_text", text="")

    _assert_transferred(result)


def test_a_failing_member_runs_its_on_failure_ladder():
    session_id, _opening = _start(_build_config(
        tasks=CHAIN,
        handoff_on_failure={"max_retries": 1,
                            "on_exhaust": {"say": "I couldn't reach the desk."}}))
    _ask_for_a_human(session_id)
    _resolve(session_id, "PrepareHandoff", summary="s")

    exhausted = _step(session_id, kind="task_result", task_name="HandOff",
                      success=False, result={})
    assert exhausted["agent_text"] == "I couldn't reach the desk."
    assert exhausted["status"] == "in_progress"


def _max_ticks() -> int:
    from flows.engine import loader as fb
    return fb.load_engine()._ESCALATE_PATH_MAX_TICKS


# ── Why the chain must reuse the app's own task ──────────────────────────────


def test_two_tasks_sharing_a_tool_collapse_in_the_result_router():
    """`_executor_tasks` is keyed by TOOL, last write wins. Adding a second
    transfer task beside the app's own would silently mis-route its result — which
    is why the chain names existing tasks instead of declaring its own.
    """
    config = _build_config(tasks=CHAIN)
    config["tasks"].append({
        "name": "EscalateHandOff", "tool": "hand_off",
        "inputs": ["summary"], "outputs": {"success": "handoff_ack"},
        "condition": flows.escalated(),
    })
    session_id, _opening = _start(config)

    routed = engine_sim._SESSIONS[session_id].sm["_executor_tasks"]["hand_off"]
    assert routed["task_name"] == "EscalateHandOff", (
        "two tasks on one tool no longer collapse; revisit the chain's design")


# ── Validation ───────────────────────────────────────────────────────────────


def _lint(config: dict, extra_tools: list[str] | None = None) -> dict:
    return blessed_source.lint_config(config, TOOLS + (extra_tools or []))


def _escalate_messages(verdict: dict, key: str) -> list[str]:
    return [m for m in verdict[key] if "escalate.tasks" in m or "'escalate'" in m]


def test_a_well_formed_chain_lints_clean():
    verdict = _lint(_build_config(tasks=CHAIN))
    assert verdict["errors"] == []
    assert verdict["warnings"] == []


def test_an_awaiting_member_is_rejected_rather_than_delaying_the_hand_off():
    """The two primitives are deliberately not composable here.

    An awaited result is turns away by construction, and escalation is the one rail a
    caller must never be held on — so a chain member that awaits is rejected at build
    rather than silently degrading (either into a hang, or into a transfer that skips
    the summary the chain existed to produce). Build the summary synchronously.
    """
    config = _build_config(
        tasks=CHAIN,
        handoff_awaits=flows.awaits(max_turns=4, say="One moment."))
    errors = _lint(config)["errors"]
    assert any("awaits an ASYNCHRONOUS tool" in e for e in errors), errors


def test_cancel_may_not_carry_a_chain():
    config = _build_config()
    config["cancel"] = {"say": "No problem.", "tasks": ["PrepareHandoff"]}
    assert any("does not support a task chain" in e
               for e in _lint(config)["errors"])


def test_a_chain_that_is_not_a_list_of_names_is_rejected():
    config = _build_config()
    config["escalate"]["tasks"] = "PrepareHandoff"
    assert "escalate.tasks must be a list of task names" in _lint(config)["errors"]

    config["escalate"]["tasks"] = ["PrepareHandoff", 7]
    assert "escalate.tasks must be a list of task names" in _lint(config)["errors"]


def test_an_unknown_member_is_rejected():
    config = _build_config(tasks=["NoSuchTask"])
    assert any("unknown task 'NoSuchTask'" in e for e in _lint(config)["errors"])


def test_a_terminal_member_is_rejected():
    """It would tear the flow down mid-chain, so the disposition never runs."""
    config = _build_config(tasks=CHAIN, handoff_terminal=True)
    assert _escalate_messages(_lint(config), "errors")


def test_a_component_member_is_rejected():
    config = _build_config(tasks=["Sub"])
    config["tasks"].append({
        "name": "Sub", "component": "child",
        "inputs": {"account": "account"}, "outputs": {},
    })
    errors = _escalate_messages(_lint(config, ["child_dag"]), "errors")
    assert any("component" in e for e in errors), errors


def test_an_ungated_member_is_rejected():
    """With no condition, inputs or requires it is fireable from turn one, so the
    spine runs it long before anyone asks for a human."""
    config = _build_config(tasks=CHAIN)
    for task in config["tasks"]:
        if task["name"] == "PrepareHandoff":
            task.pop("condition")
            task["inputs"] = []
            task["requires"] = []
    errors = _escalate_messages(_lint(config), "errors")
    assert any("gate it" in e for e in errors), errors


def test_a_member_needing_a_slot_produced_outside_the_chain_warns():
    """The caller can ask for a human before the spine has produced it, and the
    chain would then be skipped entirely."""
    config = _build_config(tasks=CHAIN)
    for task in config["tasks"]:
        if task["name"] == "PrepareHandoff":
            task["inputs"] = ["diagnosis"]
    warnings = _escalate_messages(_lint(config), "warnings")
    assert any("diagnosis" in w for w in warnings), warnings


# ── Authoring surface ────────────────────────────────────────────────────────


def test_escalate_without_a_chain_emits_no_tasks_key():
    """Every existing agent's emitted block must be unchanged."""
    assert flows.escalate(say=SAY) == {
        "requires_readback": False, "say": SAY, "outcome": "escalated"}


def test_escalate_emits_the_chain_it_was_given():
    assert flows.escalate(say=SAY, tasks=CHAIN)["tasks"] == CHAIN


def test_escalated_gates_on_the_synthesized_control_slot():
    assert flows.escalated() == "lambda f: bool(f.get('escalate'))"
    assert eval(flows.escalated())({"escalate": True}) is True  # noqa: S307
    assert eval(flows.escalated())({}) is False  # noqa: S307
