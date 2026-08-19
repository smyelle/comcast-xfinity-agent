"""Escalate routed into an in-DAG deflection component (`escalate.component`).

The default escalate rail detects a "reach a human" turn and disposes it before the
author's own DAG sees the turn — it fires `transfer_to_human`, speaks `escalate.say`
and ends the session. That is a one-shot hand-off: it cannot deflect to self-service,
offer an SMS assistant, branch on the caller's reason, and only THEN hand off / hang up.

`escalate={"component": <child_id>}` routes the detected request into an interactive,
in-DAG, returnable child DAG instead — the SAME `_component_fire_action` descent an
`on_exhaust.component` or a `component(...)` task uses. Every human-request path funnels
through `_escalate_disposition`, so the deflection fires no matter HOW the request was
detected, and the child owns the disposition (deflect-and-return, transfer, or hang up).

Offline via `flows.sim.engine_sim` (no LLM / no creds) and the cross-config validator.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_escalate_component.py
"""

from __future__ import annotations

import flows
from flows.engine import loader as fb
from flows.sim import engine_sim


ACCOUNT = {"account": "8069100230361049"}
TOOLS = ["diagnose", "send_ece_link", "note_deflection", "do_handoff"]


def _child_terminal() -> dict:
    """A deflection child that ends the call: send an SMS link, ask about the
    assistant, then transfer + end the conversation."""
    c = flows.Flow("deflect", root_agent="Esc_Agent")
    c.add(
        flows.result_slot("ece_sent", "SendEce"),
        flows.user_slot("va_choice", ask="Would you like to try the Verizon Assistant?"),
    )
    c.task("SendEce", "send_ece_link", [], "ece_sent")
    c.add(flows.announce("handoff", ["Connecting you to an agent now."],
                         requires=["va_choice"], transfer_to="live_agent",
                         end=True, end_conversation=True))
    cfg = c.to_config()
    # The child IS the escalation handler; a repeat ask inside it must be handled by
    # its own DAG, not re-preempted by a nested escalate rail.
    cfg["escalatable"] = False
    return cfg


def _child_returnable() -> dict:
    """A deflection child that DEFLECTS and returns to the parent (a terminal task,
    no end_conversation) — the caller stayed with the bot."""
    c = flows.Flow("deflect", root_agent="Esc_Agent")
    c.add(flows.result_slot("deflected", "Deflect"))
    c.task("Deflect", "note_deflection", [], "deflected", terminal=True)
    cfg = c.to_config()
    cfg["escalatable"] = False
    return cfg


def _child_no_way_out() -> dict:
    """A malformed deflection child: no terminal task and it never ends the call, so
    it would reach all-done with a live frame and stall."""
    c = flows.Flow("deflect", root_agent="Esc_Agent")
    c.add(flows.result_slot("ece_sent", "SendEce"))
    c.task("SendEce", "send_ece_link", [], "ece_sent")
    cfg = c.to_config()
    cfg["escalatable"] = False
    return cfg


def _parent(*, component: str | None = "deflect",
            escalate_block: dict | None = None) -> dict:
    f = flows.Flow("esc_test", root_agent="Esc_Agent")
    f.add(
        flows.event_slot("account"),
        flows.result_slot("diagnosis", "Diagnostics"),
    )
    f.task("Diagnostics", "diagnose", ["account"], "diagnosis")
    f.add(flows.announce("verdict", ["Here is what I found: {diagnosis}."],
                         requires=["diagnosis"], end=True))
    if escalate_block is not None:
        f.set("escalate", escalate_block)
    elif component is not None:
        f.set("escalate", flows.escalate(component=component))
    else:
        f.set("escalate", flows.escalate(say="Let me get you to someone."))
    return f.to_config()


def _start(parent: dict, child: dict | None) -> tuple[str, dict]:
    engine_sim.reset_store()
    configs = {"deflect": child} if child is not None else None
    return engine_sim.start(parent, "esc_test", event_data=ACCOUNT, configs=configs)


def _ask_for_a_human(session_id: str) -> dict:
    return engine_sim.step({"session_id": session_id, "kind": "setter_call",
                            "tool": "transfer_to_human",
                            "args": {"reason": "wants a person"}})


def _fired(result: dict) -> str:
    return ((result.get("function_call") or {}).get("name")) or ""


def _stack(result: dict) -> list[str]:
    return [f.get("child_config") for f in result["sm"].get("_call_stack", [])]


# ── Runtime: the descent ──────────────────────────────────────────────────────


def test_asking_for_a_human_descends_into_the_component_not_the_terminate():
    """The whole point: instead of firing transfer_to_human and ending, the engine
    pushes the deflection frame and runs its first task on the same turn."""
    session_id, opening = _start(_parent(), _child_terminal())
    assert _fired(opening) == "diagnose", "the spine should be eligible at the start"

    out = _ask_for_a_human(session_id)

    assert _fired(out) == "send_ece_link", "the child's first task should fire"
    assert out["status"] == "in_progress", "the call is NOT torn down"
    assert out["next_action"] != "terminal"
    assert _stack(out) == ["deflect"], "the deflection frame is active"


def test_the_escalate_detection_is_consumed_so_it_cannot_re_trigger():
    """The escalate slot is popped before the frame is pushed, so the parent scope the
    frame stashes carries no escalate=True — otherwise the restored scope on return
    would re-enter the terminal handler and re-fire forever."""
    session_id, _ = _start(_parent(), _child_terminal())
    out = _ask_for_a_human(session_id)

    assert "escalate" not in out["sm"].get("filled", {})
    assert "escalate" not in out["sm"].get("pending", {})


def test_the_child_dag_drives_the_following_turns():
    """Once descended, the child's own DAG runs — after its first task resolves it
    asks its own question, exactly like any component sub-flow."""
    session_id, _ = _start(_parent(), _child_terminal())
    _ask_for_a_human(session_id)

    asked = engine_sim.step({"session_id": session_id, "kind": "task_result",
                             "task_name": "SendEce", "success": True,
                             "result": {"ece_sent": True}})
    assert asked["agent_text"] == "Would you like to try the Verizon Assistant?"
    assert _stack(asked) == ["deflect"]


def test_a_returnable_child_hands_control_back_to_the_parent_flow():
    """A child that completes on an ordinary terminal task (no end_conversation)
    frame-returns: the caller was deflected and the parent flow carries on, with no
    lingering escalate to re-trigger."""
    session_id, _ = _start(_parent(), _child_returnable())
    assert _fired(_ask_for_a_human(session_id)) == "note_deflection"

    returned = engine_sim.step({"session_id": session_id, "kind": "task_result",
                                "task_name": "Deflect", "success": True,
                                "result": {"deflected": True}})
    assert _stack(returned) == [], "the frame popped back to the parent"
    assert "escalate" not in returned["sm"].get("filled", {})
    assert returned["status"] == "in_progress"


# ── Runtime: backwards compatibility (the regression pin) ─────────────────────


def test_escalate_without_a_component_still_terminates_unchanged():
    """Every deployed agent has no escalate.component, so this path must be
    byte-for-byte the plain transfer_to_human disposition."""
    session_id, _ = _start(_parent(component=None), child=None)
    out = _ask_for_a_human(session_id)

    assert out["agent_text"] == "Let me get you to someone."
    assert out["function_call"] is None
    assert out["status"] == "zombie"
    assert out["next_action"] == "terminal"
    assert out["response_parts"] == [
        {"type": "end_session", "reason": "transfer", "escalated": True},
    ]
    assert "_call_stack" not in out["sm"] or out["sm"]["_call_stack"] == []


# ── Cross-config validation ───────────────────────────────────────────────────


def _cross_errors(configs: dict[str, dict]) -> list[str]:
    return fb.load_validator().CrossConfigValidator(configs).validate().errors


def test_a_well_formed_deflection_lints_clean():
    errs = _cross_errors({"esc_test": _parent(), "deflect": _child_terminal()})
    assert errs == [], errs


def test_a_returnable_deflection_is_allowed_not_forced_to_end_the_call():
    """Unlike an on_exhaust component (which MUST end the conversation or it loops on
    the exhausted slot), an escalate.component MAY return to its parent — the
    way-out check accepts a plain terminal task, so it raises no
    'must END the conversation' error the way the exhaust path would."""
    errs = _cross_errors({"esc_test": _parent(), "deflect": _child_returnable()})
    assert not any("must END the conversation" in e for e in errs), errs
    assert not any("no terminal task" in e for e in errs), errs


def test_an_unknown_deflection_child_is_rejected():
    errs = _cross_errors({"esc_test": _parent(component="nope")})
    assert any("unknown child config 'nope'" in e for e in errs), errs


def test_a_deflection_child_with_no_way_out_is_rejected():
    errs = _cross_errors({"esc_test": _parent(), "deflect": _child_no_way_out()})
    assert any("no terminal task" in e and "deflect" in e for e in errs), errs


def test_a_self_referential_escalate_component_is_rejected_as_a_cycle():
    """An escalate.component that routes back into its OWN config is a length-1 cycle.
    An escalate child may deflect-and-RETURN, so the way-out check passes and nothing
    else forbids it — the config-ref cycle graph must fold in escalate edges to catch
    it. The child is otherwise well-formed (it ends the call), isolating the cycle."""
    c = flows.Flow("deflect", root_agent="Esc_Agent")
    c.add(
        flows.result_slot("ece_sent", "SendEce"),
        flows.user_slot("va_choice", ask="Would you like to try the Verizon Assistant?"),
    )
    c.task("SendEce", "send_ece_link", [], "ece_sent")
    c.add(flows.announce("handoff", ["Connecting you to an agent now."],
                         requires=["va_choice"], transfer_to="live_agent",
                         end=True, end_conversation=True))
    c.set("escalate", flows.escalate(component="deflect"))  # routes into itself
    errs = _cross_errors({"esc_test": _parent(), "deflect": c.to_config()})
    assert any("cycle detected" in e and "deflect" in e for e in errs), errs


# ── Per-config validation ─────────────────────────────────────────────────────


def _config_errors(config: dict) -> list[str]:
    return fb.load_validator().DagConfigValidator(config).validate().errors


def test_component_cannot_be_combined_with_a_chain_or_transfer_or_handoff():
    # Authored as a raw block to bypass the dsl guard and hit the validator directly.
    for clash in ({"tasks": ["X"]}, {"transfer_to": "parent"},
                  {"response": [{"type": "end_session"}]}):
        cfg = _parent(escalate_block={"component": "deflect", **clash})
        errs = _config_errors(cfg)
        assert any("component cannot be combined" in e for e in errs), (clash, errs)


def test_component_is_only_valid_on_escalate_not_cancel():
    cfg = _parent()
    cfg["cancel"] = {"component": "deflect"}
    errs = _config_errors(cfg)
    assert any("component is only valid on 'escalate'" in e for e in errs), errs


def test_a_bad_on_abort_is_rejected():
    cfg = _parent(escalate_block={"component": "deflect", "on_abort": "explode"})
    errs = _config_errors(cfg)
    assert any("on_abort must be 'skip' or 'fail_flow'" in e for e in errs), errs


# ── Authoring surface ─────────────────────────────────────────────────────────


def test_escalate_component_emits_the_block():
    block = flows.escalate(component="deflect",
                           inputs={"mdn": "mdn"}, outputs={"done": "resolved"},
                           on_abort="fail_flow")
    assert block["component"] == "deflect"
    assert block["inputs"] == {"mdn": "mdn"}
    assert block["outputs"] == {"done": "resolved"}
    assert block["on_abort"] == "fail_flow"
    assert "say" not in block, "say is optional when a component is set"


def test_escalate_component_defaults_are_empty_io_and_skip():
    block = flows.escalate(component="deflect")
    assert block["inputs"] == {}
    assert block["outputs"] == {}
    assert block["on_abort"] == "skip"


def test_escalate_component_clashes_raise_at_authoring():
    import pytest
    for clash in (dict(tasks=["X"]), dict(transfer_to="parent")):
        with pytest.raises(ValueError, match="component"):
            flows.escalate(component="deflect", **clash)


def test_escalate_requires_say_unless_component_is_set():
    import pytest
    with pytest.raises(ValueError, match="say="):
        flows.escalate()  # neither say nor component
    # component alone is fine (the child speaks)
    assert flows.escalate(component="deflect")["component"] == "deflect"


def test_plain_escalate_authoring_is_unchanged():
    """The emitted block for every existing agent must be byte-identical."""
    assert flows.escalate(say="Let me get you to someone.") == {
        "requires_readback": False, "outcome": "escalated",
        "say": "Let me get you to someone."}


# ── Review follow-ups: say/component exclusivity, I/O mapping, abort routing ───


def test_say_with_component_is_rejected():
    """`say` is dropped at runtime when a component descends, so passing both is a
    footgun — reject it (the child speaks its own prelude)."""
    import pytest
    with pytest.raises(ValueError, match="say"):
        flows.escalate(say="One moment.", component="deflect")


def _child_io() -> dict:
    """A returnable child that reads a seeded input and produces an output."""
    c = flows.Flow("deflect", root_agent="Esc_Agent")
    c.add(flows.event_slot("acct"), flows.result_slot("deflected", "Deflect"))
    c.task("Deflect", "note_deflection", ["acct"], "deflected", terminal=True)
    cfg = c.to_config()
    cfg["escalatable"] = False
    return cfg


def _child_abortable() -> dict:
    """A child that asks a question, so a cancel_flow mid-child aborts the sub-flow."""
    c = flows.Flow("deflect", root_agent="Esc_Agent")
    c.add(flows.result_slot("ece", "SendEce"), flows.user_slot("confirm", ask="Proceed?"))
    c.task("SendEce", "send_ece_link", [], "ece", out_key="success")
    c.add(flows.announce("done", ["Done."], requires=["confirm"],
                         end=True, end_conversation=True))
    cfg = c.to_config()
    cfg["escalatable"] = False
    return cfg


def _parent_io(escalate_block: dict) -> dict:
    f = flows.Flow("esc_test", root_agent="Esc_Agent")
    f.add(flows.event_slot("account"), flows.result_slot("diagnosis", "Diagnostics"),
          flows.result_slot("resolved", "Diagnostics"))
    f.task("Diagnostics", "diagnose", ["account"], "diagnosis")
    f.add(flows.announce("verdict", ["Found."], requires=["diagnosis"], end=True))
    f.set("escalate", escalate_block)
    return f.to_config()


def test_component_seeds_inputs_and_merges_outputs():
    """`inputs` seed the child scope from parent slots; `outputs` merge back on return."""
    engine_sim.reset_store()
    parent = _parent_io(flows.escalate(
        component="deflect", inputs={"account": "acct"}, outputs={"deflected": "resolved"}))
    sid, _ = engine_sim.start(parent, "esc_test", event_data=ACCOUNT,
                              configs={"deflect": _child_io()})
    descended = _ask_for_a_human(sid)
    assert descended["sm"]["filled"].get("acct") == ACCOUNT["account"], "input not seeded"

    returned = engine_sim.step({"session_id": sid, "kind": "task_result",
                                "task_name": "Deflect", "success": True,
                                "result": {"deflected": "YES"}})
    assert _stack(returned) == [], "child should have returned"
    assert returned["sm"]["filled"].get("resolved") == "YES", "output not merged back"


def test_fail_flow_abort_terminates_the_parent_as_escalated_not_cancelled():
    """A deflection that aborts with on_abort='fail_flow' tears the parent down through
    the ESCALATE block (escalated end_session), not the default 'cancel' — otherwise the
    outcome logs 'cancelled' for a caller who asked for a human."""
    engine_sim.reset_store()
    parent = _parent_io(flows.escalate(component="deflect", on_abort="fail_flow"))
    sid, _ = engine_sim.start(parent, "esc_test", event_data=ACCOUNT,
                              configs={"deflect": _child_abortable()})
    _ask_for_a_human(sid)
    engine_sim.step({"session_id": sid, "kind": "task_result", "task_name": "SendEce",
                     "success": True, "result": {"success": True}})
    aborting = engine_sim.step({"session_id": sid, "kind": "setter_call",
                                "tool": "cancel_flow", "args": {}})
    assert aborting["sm"].get("_fail_parent_flow") == "escalate", (
        "fail_flow abort should route the parent teardown through the escalate block")
    terminal = engine_sim.step({"session_id": sid, "kind": "user_text", "text": ""})
    assert terminal["status"] == "zombie"
    assert terminal["response_parts"] == [
        {"type": "end_session", "reason": "transfer", "escalated": True}]


def test_skip_abort_does_not_fail_the_parent():
    """The default on_abort='skip' must NOT tear the parent down (the deflection just
    ends and the flow carries on)."""
    engine_sim.reset_store()
    parent = _parent_io(flows.escalate(component="deflect", on_abort="skip"))
    sid, _ = engine_sim.start(parent, "esc_test", event_data=ACCOUNT,
                              configs={"deflect": _child_abortable()})
    _ask_for_a_human(sid)
    engine_sim.step({"session_id": sid, "kind": "task_result", "task_name": "SendEce",
                     "success": True, "result": {"success": True}})
    aborting = engine_sim.step({"session_id": sid, "kind": "setter_call",
                                "tool": "cancel_flow", "args": {}})
    assert not aborting["sm"].get("_fail_parent_flow")


def test_component_bad_input_slot_is_rejected():
    """A typo in escalate.component inputs (mapping to a child slot that doesn't exist)
    is caught by the cross-config I/O check, same as a component task's."""
    parent = _parent_io(flows.escalate(
        component="deflect", inputs={"account": "no_such_child_slot"}))
    errs = _cross_errors({"esc_test": parent, "deflect": _child_io()})
    assert any("no_such_child_slot" in e and "escalate.component" in e for e in errs), errs


# ── RC2: no_input.on_exhaust fires its `then` tool alongside end_conversation ──


def test_no_input_exhaust_fires_then_tool_and_ends():
    """A silence exhaust carrying BOTH a `then` tool and end_conversation must fire the
    tool AND end the call — the tool used to be dropped whenever end_conversation was set
    (the deflection's silence->hangup branch)."""
    f = flows.Flow("ni_test", root_agent="Ni_Agent", single_flow=True)
    f.add(flows.user_slot("answer", ask="What would you like to do?"))
    f.add(flows.announce("done", ["All set."], requires=["answer"], end=True))
    f.set("no_input", {
        "reprompts": [""],
        "on_exhaust": {
            "say": "Closing the call now.",
            "then": {"tool": "hangup_call", "args": {"action": "hangup"}},
            "end_conversation": True,
        },
    })
    engine_sim.reset_store()
    sid, _ = engine_sim.start(f.to_config(), "ni_test")
    fc, parts = None, []
    for _ in range(6):
        r = engine_sim.step({"session_id": sid, "kind": "user_text", "text": "",
                             "is_inactivity": True})
        fc = (r.get("function_call") or {}).get("name") or fc
        parts = r.get("response_parts") or parts
        if r.get("status") in ("complete", "zombie"):
            break
    assert fc == "hangup_call", f"the on_exhaust `then` tool must fire, got {fc!r}"
    assert any(p.get("type") == "end_session" for p in parts), "the call must also end"
