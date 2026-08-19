"""Regression tests for the filed slot-filling framework fixes.

Covers, one focused test each:
  #585   a self-gating user slot (`condition: ... and not f.get(<self>)`) keeps its own
         just-filled value instead of deactivating it — while a GENUINE cross-slot
         condition still deactivates.
  bonus  a cross-slot `validate_against` list matches case/space-insensitively.
  #587   `on_failure.clear_slots` keyed by the tool's error_code clears different slots per
         failure reason; a plain list still clears the same slots (backward compat).
  #586   `escalate(reason=...)` sets the terminal end_session reason; the default stays
         "transfer".
  #513   the engine marks a steer-back increment speculative so after_model can roll it back
         on a covered empty turn.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_filed_issue_fixes.py
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import flows  # noqa: E402
from flows.engine import loader as fb  # noqa: E402
from flows.sim import engine_sim  # noqa: E402

_FW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src/flows/engine/framework/tools")


# ── #585 + bonus: intake-level, driven through app setters via engine_sim ──────────
_SETTERS = {
    "set_coverage_status": (
        "def set_coverage_status(coverage_status=''):\n"
        "    return {'stored':True,'value':str(coverage_status).strip().lower()}"
        " if str(coverage_status).strip() else {'error':True,'error_code':'missing'}\n"),
    "set_service_setting": (
        "def set_service_setting(service_setting=''):\n"
        "    v=str(service_setting).strip().lower()\n"
        "    if not v: return {'error':True,'error_code':'missing'}\n"
        "    if 'out' in v: return {'stored':True,'value':'outpatient'}\n"
        "    if 'in' in v: return {'stored':True,'value':'inpatient'}\n"
        "    return {'error':True,'error_code':'unrecognized_selection'}\n"),
    "set_allowed": (
        "def set_allowed(allowed=''):\n"
        "    return {'stored':True,'value':str(allowed)}\n"),
    "do_task": "def do_task():\n    return {'success':True,'result':'ok'}\n",
}


def _run_service_setting(*, self_gate: bool, allowed_list=None):
    root = fb.materialize_tools_root(_SETTERS, parent=tempfile.mkdtemp())
    cond = ("lambda f: f.get('coverage_status') == 'active' and not"
            " f.get('service_setting')") if self_gate else (
        "lambda f: f.get('coverage_status') == 'active'")
    slots = [
        {"name": "coverage_status", "source": "user",
         "setter": "set_coverage_status", "ask": "coverage?"},
        {"name": "service_setting", "source": "user", "setter": "set_service_setting",
         "ask": "inpatient or outpatient?", "requires": ["coverage_status"],
         "condition": cond},
    ]
    if allowed_list is not None:
        slots.insert(1, {"name": "allowed_settings", "source": "user",
                         "setter": "set_allowed", "ask": "allowed?"})
        slots[-1]["validate_against"] = {
            "filled_slot": "allowed_settings", "response_field": "value",
            "error_code": "not_allowed"}
    cfg = {"_config_id": "t", "bootstrap": {"welcome_slot": "w"},
           "correction_tool": "set_slot_change",
           "slots": [{"name": "w", "source": "announce",
                      "response": [{"type": "text", "text": "hi"}],
                      "shared": True, "preempt": True}] + slots,
           "tasks": [{"name": "task", "tool": "do_task", "inputs": [], "outputs": {},
                      "requires": ["service_setting"],
                      "condition": "lambda f: f.get('service_setting') == 'outpatient'"}]}
    engine_sim.reset_store()
    sid, _ = engine_sim.start(cfg, flow_id="t", configs={"t": cfg}, framework_root=root)
    engine_sim.step({"session_id": sid, "kind": "setter_call",
                     "tool": "set_coverage_status", "args": {"coverage_status": "active"}})
    if allowed_list is not None:
        engine_sim.step({"session_id": sid, "kind": "setter_call",
                         "tool": "set_allowed", "args": {"allowed": allowed_list}})
    out = engine_sim.step({"session_id": sid, "kind": "setter_call",
                           "tool": "set_service_setting",
                           "args": {"service_setting": "Outpatient"}})
    fb.clear_cache()
    return out


def test_585_self_gating_slot_keeps_its_own_value():
    """A `... and not f.get(self)` gate must not delete the value it just stored."""
    out = _run_service_setting(self_gate=True)
    assert out["sm"]["filled"].get("service_setting") == "outpatient", out["sm"]["filled"]


def test_585_cross_slot_condition_still_deactivates():
    """The fix is SCOPED: a slot gated on ANOTHER slot is still deactivated when that
    condition goes false. Seed service_setting filled while its cross-slot gate
    (coverage_status == 'active') is False, run one engine pass, and assert it is popped."""
    fb.set_framework_root(_FW)
    cfg = {
        "config_id": "t",
        "slots": [
            {"name": "coverage_status", "source": "user", "ask": "c?", "setter": "set_cov"},
            {"name": "service_setting", "source": "user", "ask": "s?", "setter": "set_ss",
             "condition": "lambda f: f.get('coverage_status') == 'active'"},
        ],
        "tasks": [],
    }
    sm = fb.seed_sm(cfg)
    sm["filled"] = {"coverage_status": "inactive", "service_setting": "outpatient"}
    sm["pending"] = {}
    out = fb.run_engine(cfg, sm, config_id="t")["sm"]
    assert "service_setting" not in out["filled"], out["filled"]


def test_bonus_validate_against_is_case_insensitive():
    """A canonicalized value ("outpatient") matches a Title-case allowed-list token."""
    out = _run_service_setting(self_gate=False, allowed_list="Inpatient,Outpatient")
    assert out["sm"]["filled"].get("service_setting") == "outpatient", out["sm"]["filled"]


# ── #587: reason-aware clear_slots via the loader run harness ──────────────────────
def _member_verify_cfg(clear_slots):
    return {
        "config_id": "verify",
        "slots": [
            {"name": "member_id", "source": "user", "ask": "id?", "setter": "set_member_id"},
            {"name": "date_of_birth", "source": "user", "ask": "dob?", "setter": "set_dob"},
            {"name": "verify_status", "source": "system"},
        ],
        "tasks": [{
            "name": "member_verify", "tool": "verify_member",
            "inputs": ["member_id", "date_of_birth"],
            "requires": ["member_id", "date_of_birth"], "success_check": "success",
            "outputs": {"verify_status": "verify_status"},
            "on_failure": {"clear_slots": clear_slots, "max_retries": 3,
                           "retry_say": "again", "on_exhaust": {"say": "no"}},
        }],
    }


def _fail_member_verify(cfg, error_code):
    fb.set_framework_root(_FW)
    sm = fb.seed_sm(copy.deepcopy(cfg))
    sm["filled"] = {"member_id": "M-1", "date_of_birth": "1990-01-01"}
    sm["pending"] = {}
    sm = fb.run_engine(cfg, sm, config_id="verify")["sm"]
    payload = {"success": False, "error_code": error_code, "message": "fail"}
    sm = fb.run_intake("verify_member", payload, sm)["sm"]
    return dict(fb.run_engine(cfg, sm, config_id="verify")["sm"]["filled"])


def test_587_keyed_clear_slots_is_reason_aware():
    cfg = _member_verify_cfg({"bad_dob": ["date_of_birth"], "wrong_member": ["member_id"],
                              "_default": ["member_id", "date_of_birth"]})
    assert _fail_member_verify(cfg, "bad_dob") == {"member_id": "M-1"}
    assert _fail_member_verify(cfg, "wrong_member") == {"date_of_birth": "1990-01-01"}
    assert _fail_member_verify(cfg, "other") == {}          # _default clears both


def test_587_plain_list_clear_slots_is_unchanged():
    cfg = _member_verify_cfg(["date_of_birth"])
    assert _fail_member_verify(cfg, "bad_dob") == {"member_id": "M-1"}
    assert _fail_member_verify(cfg, "wrong_member") == {"member_id": "M-1"}


# ── #586: app-set terminal end_session reason on the escalate rail ─────────────────
def _escalate_end(reason=None):
    f = flows.Flow("esc", root_agent="A")
    f.add(flows.event_slot("acct"), flows.result_slot("d", "Diag"))
    f.task("Diag", "diagnose", ["acct"], "d")
    f.add(flows.announce("v", ["done {d}."], requires=["d"], end=True))
    kw = {"say": "one moment"}
    if reason is not None:
        kw["reason"] = reason
    f.set("escalate", flows.escalate(**kw))
    engine_sim.reset_store()
    sid, _ = engine_sim.start(f.to_config(), "esc", event_data={"acct": "1"})
    res = engine_sim.step({"session_id": sid, "kind": "setter_call",
                           "tool": "transfer_to_human", "args": {"reason": "person"}})
    return [p for p in res["response_parts"] if p.get("type") == "end_session"][0]


def test_586_escalate_reason_is_app_settable():
    assert _escalate_end("escalate") == {
        "type": "end_session", "reason": "escalate", "escalated": True}


def test_586_escalate_reason_default_is_transfer():
    assert _escalate_end(None) == {
        "type": "end_session", "reason": "transfer", "escalated": True}


# ── #513: after_model rolls back a speculative steer-back increment on a covered empty ──
import importlib.util  # noqa: E402


class _Part513:
    def __init__(self, kind, **d):
        self.kind = kind
        self.text = d.get("text")
        self.function_call = d.get("function_call")

    @classmethod
    def from_text(cls, text=""):
        return cls("text", text=text)

    @classmethod
    def from_function_call(cls, name="", args=None):
        fc = type("FC", (), {"name": name, "args": args or {}})
        return cls("call", function_call=fc)


class _Resp513:
    def __init__(self, parts, finish_reason=None):
        self.content = type("C", (), {"parts": parts})
        self.finish_reason = finish_reason

    @classmethod
    def from_parts(cls, parts, finish_reason=None):
        return cls(parts, finish_reason)


class _Ctx513:
    def __init__(self, state):
        self.state = dict(state)


def _load_after_model():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src/flows/engine/framework/callbacks/after_model.py")
    spec = importlib.util.spec_from_file_location("_am513", path)
    mod = importlib.util.module_from_spec(spec)
    for name in ("CallbackContext", "LlmRequest", "Content", "Tool", "ces_internal"):
        setattr(mod, name, type(name, (), {}))
    mod.Part = _Part513
    mod.LlmResponse = _Resp513
    spec.loader.exec_module(mod)
    return mod


_AM = _load_after_model()


def test_513_empty_turn_rolls_back_speculative_steer_back():
    """An empty completion covered by render_empty_backstop must un-count the speculative
    steer-back increment the engine made this turn (so fragmented reads don't march the
    off-topic ladder)."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": "What's your tracking number?",
        "_steer_back_turns": 2, "_steer_back_speculative": True,
    }})
    out = _AM.after_model_callback(ctx, _Resp513.from_parts([]))  # empty completion
    assert out is not None                                        # backstop rendered
    assert ctx.state["sm"]["_steer_back_turns"] == 1             # 2 -> 1 rolled back
    assert "_steer_back_speculative" not in ctx.state["sm"]


def test_513_no_rollback_without_speculative_marker():
    """A genuine off-topic empty turn (no speculative marker) keeps its steer-back count."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": "What's your tracking number?", "_steer_back_turns": 2,
    }})
    _AM.after_model_callback(ctx, _Resp513.from_parts([]))
    assert ctx.state["sm"]["_steer_back_turns"] == 2


# ── #590: a no-text re-call of an already-filled setter speaks the pending question ──
_DOB_Q = "And what is the member's date of birth?"


def test_590_redundant_setter_recall_speaks_pending_question():
    """Continuation pass: model re-calls set_member_id (already filled) with no text; the
    engine armed _render_fallback (the DOB question). Speak it, not the platform fallback."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": _DOB_Q,
        "filled": {"member_id": "259W17936"},
        "_setter_slots": {"set_member_id": "member_id"},
    }})
    out = _AM.after_model_callback(
        ctx, _Resp513.from_parts([_Part513.from_function_call(
            "set_member_id", {"member_id": "259W17936"})]))
    assert out is not None
    assert out.content.parts[0].text == _DOB_Q
    assert "_render_fallback" not in ctx.state["sm"]


def test_590_real_first_setter_still_proceeds():
    """A REAL first setter (target not yet filled — after_model runs before intake) must
    NOT be intercepted; the setter proceeds (return None)."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": "Please say the Member ID.",
        "filled": {},  # member_id not filled yet
        "_setter_slots": {"set_member_id": "member_id"},
    }})
    out = _AM.after_model_callback(
        ctx, _Resp513.from_parts([_Part513.from_function_call(
            "set_member_id", {"member_id": "259W17936"})]))
    assert out is None


def test_590_multitool_second_setter_for_unfilled_slot_proceeds():
    """A genuine second value in one turn (setter for a still-unfilled slot) proceeds."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": _DOB_Q,
        "filled": {"member_id": "259W17936"},  # date_of_birth NOT filled
        "_setter_slots": {"set_member_id": "member_id", "set_dob": "date_of_birth"},
    }})
    out = _AM.after_model_callback(
        ctx, _Resp513.from_parts([_Part513.from_function_call(
            "set_dob", {"date_of_birth": "1985-11-16"})]))
    assert out is None


def test_590_correction_tool_proceeds():
    """set_slot_change is a framework control tool (actionable) -> never intercepted."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": _DOB_Q,
        "filled": {"member_id": "259W17936"},
        "_setter_slots": {"set_member_id": "member_id"},
    }})
    out = _AM.after_model_callback(
        ctx, _Resp513.from_parts([_Part513.from_function_call(
            "set_slot_change", {"slot": "member_id"})]))
    assert out is None


def test_590_malformed_unknown_call_speaks_pending_question():
    """The live 9/9 case: a MALFORMED continuation surfaces as a function_call with a name
    that is not any real tool (parser-error). With the next question armed, speak it instead
    of letting CES render its platform "having trouble"."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": _DOB_Q,
        "filled": {"member_id": "259W17936"},
        "_setter_slots": {"set_member_id": "member_id"},
        "_executor_tasks": {"unified_member_verification": {}},
    }})
    out = _AM.after_model_callback(
        ctx, _Resp513.from_parts([_Part513.from_function_call(
            "« malformed »", {})]))
    assert out is not None
    assert out.content.parts[0].text == _DOB_Q


def test_590_error_parrot_speaks_pending_question():
    """The dominant live case (confirmed on a deployed Elevance trace): on the continuation
    pass after a setter fires, the model emits the platform no-match/error line verbatim AS
    ITS OWN TEXT ("Hmm, I'm having trouble with that. Do you want me to try again?") instead
    of the armed next question. There is no finish_reason on the ADK LlmResponse to key off,
    and _has_text is True so the empty backstop misses it -> match the platform error stem and
    speak the armed fallback."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": _DOB_Q,
        "filled": {"member_id": "259W17936"},
        "_setter_slots": {"set_member_id": "member_id"},
    }})
    resp = _Resp513.from_parts([_Part513.from_text(
        "Hmm, I'm having trouble with that. Do you want me to try again?")])
    out = _AM.after_model_callback(ctx, resp)
    assert out is not None
    assert out.content.parts[0].text == _DOB_Q


def test_590_error_parrot_hearing_you_variant_speaks_pending_question():
    """The second platform-fallback variant ("I'm still having trouble hearing you.") is also
    intercepted."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": _DOB_Q,
        "filled": {"member_id": "259W17936"},
        "_setter_slots": {"set_member_id": "member_id"},
    }})
    resp = _Resp513.from_parts([_Part513.from_text(
        "I'm still having trouble hearing you.")])
    out = _AM.after_model_callback(ctx, resp)
    assert out is not None
    assert out.content.parts[0].text == _DOB_Q


def test_590_natural_having_trouble_phrase_not_intercepted():
    """CONTROL: a natural agent line that incidentally contains 'having trouble' but is NOT
    the platform error phrase (no 'try again' / 'hearing you' suffix) must render as-is."""
    ctx = _Ctx513({"sm": {"_render_fallback": _DOB_Q, "filled": {"member_id": "X"},
                          "_setter_slots": {"set_member_id": "member_id"}}})
    resp = _Resp513.from_parts([_Part513.from_text(
        "I see you're having trouble with your claim; what's the date of birth?")])
    out = _AM.after_model_callback(ctx, resp)
    assert out is None


def test_590_normal_finish_text_render_proceeds():
    """A normal proceed-turn text render (finish=STOP) must NOT be intercepted — the model's
    natural question wording is preserved."""
    ctx = _Ctx513({"sm": {"_render_fallback": _DOB_Q, "filled": {"member_id": "X"},
                          "_setter_slots": {"set_member_id": "member_id"}}})
    resp = _Resp513.from_parts(
        [_Part513.from_text("Got it. And what's the member's date of birth?")],
        finish_reason="STOP")
    out = _AM.after_model_callback(ctx, resp)
    assert out is None


def test_590_legit_executor_tool_call_proceeds():
    """CRITICAL control: a real executor/business tool call (in _executor_tasks) must NOT be
    intercepted even with a fallback armed — it advances the flow."""
    ctx = _Ctx513({"sm": {
        "_render_fallback": _DOB_Q,
        "filled": {"member_id": "259W17936", "date_of_birth": "1985-11-16"},
        "_setter_slots": {"set_member_id": "member_id"},
        "_executor_tasks": {"unified_member_verification": {}},
    }})
    out = _AM.after_model_callback(
        ctx, _Resp513.from_parts([_Part513.from_function_call(
            "unified_member_verification", {})]))
    assert out is None


# ── Guard: two executor tasks sharing one tool (the check_precertification footgun) ──
def _warnings(cfg):
    from flows.engine import loader
    return loader.load_validator().validate_dag_config({"raw_config": cfg})["warnings"]


def test_guard_warns_on_duplicate_executor_tool():
    """Two executor tasks binding the SAME tool silently collide at runtime (last write
    wins) -> the validator must warn, naming both tasks and the tool."""
    cfg = {
        "config_id": "dup",
        "slots": [
            {"name": "code1", "source": "user", "ask": "c1?", "setter": "set_c1"},
            {"name": "code2", "source": "user", "ask": "c2?", "setter": "set_c2"},
            {"name": "out1", "source": "task:precert_a"},
            {"name": "out2", "source": "task:precert_b"},
        ],
        "tasks": [
            {"name": "precert_a", "tool": "check_precert", "inputs": ["code1"],
             "outputs": {"outcome": "out1"}, "requires": ["code1"]},
            {"name": "precert_b", "tool": "check_precert", "inputs": ["code2"],
             "outputs": {"outcome": "out2"}, "requires": ["code2"]},
        ],
    }
    warns = " ".join(_warnings(cfg))
    assert "check_precert" in warns and "precert_a" in warns and "precert_b" in warns


def test_585_input_active_ignores_self_gate_but_not_cross_slot():
    """#585 completeness: _task_fireable's input-active check must count a FILLED self-gated
    slot (so a task consuming it can fire) while still deactivating on real cross-slot gates.
    """
    from flows.engine import loader
    eng = loader.load_engine()
    self_gate = {"name": "code", "condition": (lambda f: not f.get("code"))}
    # Filled self-gated input reads as ACTIVE (was inactive before the fix -> task stranded).
    assert eng._is_slot_active_ignoring_self(self_gate, "code", {"code": "X"}) is True
    assert eng._is_slot_active(self_gate, {"code": "X"}) is False  # old check
    # A genuine cross-slot gate is unaffected: still inactive when the other slot is wrong.
    cross = {"name": "b", "condition": (lambda f: f.get("a") == "x")}
    assert eng._is_slot_active_ignoring_self(cross, "b", {"a": "y", "b": "1"}) is False


def test_585_readback_includes_a_self_gated_pending_slot():
    """#585 completeness (Owl review): a self-gated slot alone in pending must still appear in
    the readback. Before the fix _build_readback judged it inactive (its value is in
    merged_state) -> empty fragments -> returns None -> the engine skips the readback
    confirmation entirely and the slot is stranded in pending."""
    from flows.engine import loader
    eng = loader.load_engine()
    slots = [{"name": "member_id", "source": "user", "ask": "id?",
              "condition": (lambda f: not f.get("member_id"))}]
    out = eng._build_readback(slots, {"member_id": "259W17936"}, {})
    assert out is not None and "259W17936" in out["system_message"]


def test_guard_silent_when_tools_distinct():
    """Distinct tools per executor task -> no collision warning."""
    cfg = {
        "config_id": "ok",
        "slots": [
            {"name": "code1", "source": "user", "ask": "c1?", "setter": "set_c1"},
            {"name": "code2", "source": "user", "ask": "c2?", "setter": "set_c2"},
            {"name": "out1", "source": "task:precert_a"},
            {"name": "out2", "source": "task:precert_b"},
        ],
        "tasks": [
            {"name": "precert_a", "tool": "check_precert", "inputs": ["code1"],
             "outputs": {"outcome": "out1"}, "requires": ["code1"]},
            {"name": "precert_b", "tool": "check_precert_2", "inputs": ["code2"],
             "outputs": {"outcome": "out2"}, "requires": ["code2"]},
        ],
    }
    assert not any("bind tool" in w for w in _warnings(cfg))
