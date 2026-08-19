"""Native-channel return on end_session.params: declared via flows.end_params_handoff +
Flow.on_end, delivered by the framework's deterministic terminal emit.

A native telephony channel (e.g. FIVE9) reads its outbound SIP headers ONLY from the
`end_session` tool call's `params[<envelope>]` — a payload part never reaches it, a session
var never reaches the wire, and on the live model a `message=` terminal announce is rendered
and the model then FREEFORM-ends with a BARE end_session, dropping any staged return
(measured on the FIVE9 dev line). So a flow declares `flow.on_end(end_params_handoff(...))`;
the engine surfaces that config into sm["_on_end"]; and before_model, on a terminal turn,
resolves `{envelope: state[from_state]}` and PREEMPTS with the close + the end_session
carrying it. The model never runs.

This lives in before_model ONLY: the preempt consumes and clears the terminal stash, so
after_model never sees a staged-return end (proven by the integration test below).

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_end_session_params.py
"""
from __future__ import annotations

import importlib.util
import json
import os

import flows
from flows.engine import loader as fb

_ON_END = {"delivery": "end_params", "envelope": "LIVE_AGENT_HANDOFF", "from_state": "LIVE_AGENT_HANDOFF"}
_STAGED = {"endSession": True, "xHeaders": {"nextappcode": "1016", "user_intent": "no_additional_information"}}
_PARAMS = {"LIVE_AGENT_HANDOFF": _STAGED}


# ── CES-global stubs. end_session models a mutable function_call.args (the real Part shape
#    the wire is read from: e2e_test reads args.params.LIVE_AGENT_HANDOFF.xHeaders). ──────────
class _FC:
  def __init__(self, name, args):
    self.name = name
    self.args = dict(args or {})


class _Part:
  def __init__(self, kind, **d):
    self.kind = kind
    self.text = d.get("text")
    self.function_call = d.get("function_call")
    self.data = d.get("data")
    self.disable_barge_in = d.get("disable_barge_in", False)

  @classmethod
  def from_text(cls, text=""):
    return cls("text", text=text)

  @classmethod
  def from_end_session(cls, reason="", escalated=False):
    # Production ces_internal builds the tool call's args with the WIRE key `session_escalated`
    # (matches end_session({"session_escalated": ...}) and the DAG payloads), not `escalated`.
    return cls("end_session",
               function_call=_FC("end_session",
                                 {"reason": reason, "session_escalated": escalated}))

  @classmethod
  def from_agent_transfer(cls, agent=""):
    return cls("transfer", data={"agent": agent})

  @classmethod
  def from_json(cls, s):
    return cls("payload", data=json.loads(s))

  @classmethod
  def from_customized_response(cls, content="", disable_barge_in=False):
    # A non-bargeable segment (e.g. a legal disclaimer). Record disable_barge_in so a test can
    # prove interruptable:False survives the terminal emit instead of flattening to plain text.
    return cls("text", text=content, disable_barge_in=disable_barge_in)


class _Resp:
  def __init__(self, parts):
    self.content = type("C", (), {"parts": parts})

  @classmethod
  def from_parts(cls, parts, finish_reason=None):
    return cls(parts)


class _Ctx:
  def __init__(self, state):
    self.state = dict(state)


def _load_before_model():
  path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "src/flows/engine/framework/callbacks/before_model.py")
  spec = importlib.util.spec_from_file_location("_bm_end_params", path)
  mod = importlib.util.module_from_spec(spec)
  for name in ("CallbackContext", "LlmRequest", "Content", "Tool", "ces_internal"):
    setattr(mod, name, type(name, (), {}))
  mod.Part = _Part
  mod.LlmResponse = _Resp
  spec.loader.exec_module(mod)
  return mod


_BM = _load_before_model()


# ── authoring: end_params_handoff -> config; Flow.on_end -> config key ───────────────────────
def test_end_params_handoff_config():
  h = flows.end_params_handoff(envelope="LIVE_AGENT_HANDOFF", from_state="LIVE_AGENT_HANDOFF")
  assert h.to_config() == _ON_END


def test_flow_on_end_emits_config():
  f = flows.Flow("t", root_agent="A")
  f.on_end(flows.end_params_handoff(envelope="LIVE_AGENT_HANDOFF", from_state="LIVE_AGENT_HANDOFF"))
  assert f.to_config()["on_end"] == _ON_END


def test_end_params_handoff_requires_envelope_and_from_state():
  import pytest
  with pytest.raises(ValueError):
    flows.end_params_handoff(envelope="", from_state="X")
  with pytest.raises(ValueError):
    flows.end_params_handoff(envelope="X", from_state="")


def test_flow_on_end_rejects_non_handoff():
  import pytest
  with pytest.raises(TypeError):
    flows.Flow("t", root_agent="A").on_end({"delivery": "end_params"})


# ── before_model: resolve on_end config + staged var -> end_session params ───────────────────
def test_resolve_end_params_from_config_and_state():
  assert _BM._resolve_end_params({"LIVE_AGENT_HANDOFF": _STAGED}, {"_on_end": _ON_END}) == _PARAMS


def test_resolve_end_params_is_channel_agnostic():
  # The engine gates on "a non-empty dict was staged", NOT on channel-specific keys — a flow
  # may stage any envelope shape and the engine folds it through verbatim.
  staged = {"someOtherChannelKey": {"foo": "bar"}}
  assert _BM._resolve_end_params(
      {"LIVE_AGENT_HANDOFF": staged}, {"_on_end": _ON_END}) == {"LIVE_AGENT_HANDOFF": staged}


def test_resolve_end_params_none_cases():
  assert _BM._resolve_end_params({"LIVE_AGENT_HANDOFF": _STAGED}, {}) is None            # no on_end
  assert _BM._resolve_end_params({}, {"_on_end": _ON_END}) is None                        # unstaged
  assert _BM._resolve_end_params({"LIVE_AGENT_HANDOFF": "x"}, {"_on_end": _ON_END}) is None  # not a dict
  assert _BM._resolve_end_params({"LIVE_AGENT_HANDOFF": {}}, {"_on_end": _ON_END}) is None   # empty dict
  assert _BM._resolve_end_params(  # unknown delivery -> no-op
      {"LIVE_AGENT_HANDOFF": _STAGED}, {"_on_end": {**_ON_END, "delivery": "payload"}}) is None


def test_end_session_part_folds_extra_params():
  part = _BM._end_session_part({"type": "end_session", "reason": "completed"}, _PARAMS)
  assert part.function_call.args["params"]["LIVE_AGENT_HANDOFF"]["xHeaders"]["nextappcode"] == "1016"


def test_end_session_part_bare_when_none():
  part = _BM._end_session_part({"type": "end_session", "reason": "completed"})
  assert "params" not in part.function_call.args


def test_terminal_emit_from_message_and_stash():
  action = {"message": "Thanks for choosing FedEx, and have a great day."}
  sm = {"_pending_announce_payloads": [{"type": "end_session", "reason": "completed"}]}
  parts, keys = _BM._terminal_emit_parts(action, sm, _PARAMS)
  assert [p.kind for p in parts] == ["text", "end_session"]
  assert "great day" in parts[0].text
  assert parts[1].function_call.args["params"]["LIVE_AGENT_HANDOFF"]["xHeaders"]["nextappcode"] == "1016"
  assert "_pending_announce_payloads" in keys


def test_terminal_emit_reads_inline_response_text():
  action = {"response": [{"type": "text", "text": "No problem, call back with your number."},
                        {"type": "end_session", "reason": "completed"}]}
  parts, _ = _BM._terminal_emit_parts(action, {}, {"LIVE_AGENT_HANDOFF": {"xHeaders": {"nextappcode": "1017"}}})
  assert [p.kind for p in parts] == ["text", "end_session"]
  assert parts[1].function_call.args["params"]["LIVE_AGENT_HANDOFF"]["xHeaders"]["nextappcode"] == "1017"


def test_terminal_emit_none_when_not_terminal():
  assert _BM._terminal_emit_parts({"message": "Here's your status."}, {}, _PARAMS) == (None, None)


# ── integration: the REAL before_model_callback is the SOLE deliverer, config-driven ─────────
class _EngResp:
  def __init__(self, payload):
    self._p = payload

  def json(self):
    return self._p


class _Config:
  def __init__(self):
    self.system_instruction = type("SI", (), {"parts": [type("PT", (), {"text": "SI"})()]})()
    self.tools = []

  def hide_tool(self, name):
    pass


class _Req:
  def __init__(self):
    self.config = _Config()
    self.model = "gemini-3.1-flash-live"
    self.contents = []


def test_before_model_is_sole_deliverer_and_clears_the_stash():
  """Drive the real before_model on a staged-return terminal turn whose flow declares
  on_end(end_params_handoff(...)). It must PREEMPT with [close, end_session(params)] AND
  consume the terminal stash — proving after_model never delivers a staged-return end, so
  the emit rightly lives in before_model only."""
  engine = fb.load_engine()

  f = flows.Flow("t", root_agent="A")
  f.add(flows.user_slot("topic", "?"),
        flows.announce("bye", [], message="Thanks, goodbye.", requires=["topic"],
                       end=True, preempt=False))
  f.on_end(flows.end_params_handoff(envelope="LIVE_AGENT_HANDOFF", from_state="LIVE_AGENT_HANDOFF"))
  cfg = f.to_config()

  sm = fb.run_engine(cfg, {}, config_id="t")["sm"]
  sm.setdefault("filled", {})["topic"] = "done"

  class _Tools:
    @staticmethod
    def slot_filling_engine(arg):
      d = dict(arg["input_data"])
      d["raw_config"] = cfg  # offline: hand the engine the config (it surfaces sm["_on_end"])
      return _EngResp({"result": engine.slot_filling_engine(d)})

    @staticmethod
    def slot_intake(arg):
      return _EngResp({"result": {"sm": arg["input_data"]["sm"]}})

  _BM.tools = _Tools()
  # The app has staged its return into the declared from_state var (a session variable).
  ctx = _Ctx({"sm": sm, "LIVE_AGENT_HANDOFF": _STAGED, "_active_config_id": "t"})
  out = _BM.before_model_callback(ctx, _Req())

  assert out is not None and hasattr(out, "content"), "before_model did not preempt the terminal turn"
  end = [p for p in out.content.parts if p.kind == "end_session"]
  assert end, f"no end_session in the preempt: {[p.kind for p in out.content.parts]}"
  assert end[0].function_call.args["params"]["LIVE_AGENT_HANDOFF"]["xHeaders"]["nextappcode"] == "1016"

  final_sm = ctx.state.get("sm", {})
  assert not final_sm.get("_pending_announce_payloads"), "stash left for after_model — emit not sole"
  assert not final_sm.get("_pending_payloads")


def test_no_on_end_is_a_no_op():
  """A flow WITHOUT on_end never preempts on the terminal turn — after_model's #719 path
  still delivers the (bare) end. byte-identical to before the feature."""
  engine = fb.load_engine()
  f = flows.Flow("t2", root_agent="A")
  f.add(flows.user_slot("topic", "?"),
        flows.announce("bye", [], message="Goodbye.", requires=["topic"], end=True, preempt=False))
  cfg = f.to_config()
  assert "on_end" not in cfg
  sm = fb.run_engine(cfg, {}, config_id="t2")["sm"]
  sm.setdefault("filled", {})["topic"] = "done"

  class _Tools:
    @staticmethod
    def slot_filling_engine(arg):
      d = dict(arg["input_data"]); d["raw_config"] = cfg
      return _EngResp({"result": engine.slot_filling_engine(d)})

    @staticmethod
    def slot_intake(arg):
      return _EngResp({"result": {"sm": arg["input_data"]["sm"]}})

  _BM.tools = _Tools()
  # Even with a staged var, no on_end config -> no preempt (the emit is opt-in via on_end).
  ctx = _Ctx({"sm": sm, "LIVE_AGENT_HANDOFF": _STAGED, "_active_config_id": "t2"})
  out = _BM.before_model_callback(ctx, _Req())
  # No deterministic emit: the terminal end is left to after_model's stash path (#719).
  if out is not None and hasattr(out, "content"):
    assert not [p for p in out.content.parts if p.kind == "end_session"], \
        "preempted an end without on_end declared"


# ── authoring: envelope/from_state validation (non-empty + identifier) ────────────────────────
def test_end_params_handoff_rejects_whitespace_and_bad_identifier():
  import pytest
  with pytest.raises(ValueError):
    flows.end_params_handoff(envelope="   ", from_state="X")        # whitespace-only envelope
  with pytest.raises(ValueError):
    flows.end_params_handoff(envelope="X", from_state="   ")        # whitespace-only from_state
  for bad in ("has space", "1leading", "dot.name", "dash-name", "a\tb"):
    with pytest.raises(ValueError):
      flows.end_params_handoff(envelope="ENV", from_state=bad)      # not a variable identifier


def test_end_params_handoff_accepts_valid_identifier():
  h = flows.end_params_handoff(envelope="LIVE_AGENT_HANDOFF", from_state="_ok_Name9")
  assert h.to_config()["from_state"] == "_ok_Name9"


# ── static validator: on_end schema is enforced even for a YAML/JSON-loaded config ────────────
def _minimal_flow():
  f = flows.Flow("v", root_agent="A")
  f.add(flows.user_slot("topic", "?"),
        flows.announce("bye", [], message="Bye.", requires=["topic"], end=True))
  return f


def _on_end_errors(cfg):
  res = fb.load_validator().DagConfigValidator(cfg).validate()
  return [e for e in res.errors if "on_end" in e]


def test_validator_accepts_wellformed_on_end():
  f = _minimal_flow()
  f.on_end(flows.end_params_handoff(envelope="LIVE_AGENT_HANDOFF", from_state="LIVE_AGENT_HANDOFF"))
  assert _on_end_errors(f.to_config()) == []


def test_validator_rejects_malformed_on_end():
  base = _minimal_flow().to_config()
  for bad in ("broken",                                              # not a mapping
              {"delivery": "payload", "envelope": "E", "from_state": "s"},   # unknown delivery
              {"delivery": "end_params", "envelope": "  ", "from_state": "s"},  # blank envelope
              {"delivery": "end_params", "envelope": "E", "from_state": "1x"}, # bad identifier
              {"delivery": "end_params", "envelope": "E"}):          # missing from_state
    cfg = dict(base)
    cfg["on_end"] = bad
    assert _on_end_errors(cfg), f"static validator should reject on_end={bad!r}"


# ── terminal emit: preserve sibling parts + per-part metadata; bail on a transfer ─────────────
def test_terminal_emit_preserves_sibling_payload_and_metadata():
  action = {"response": [
      {"type": "text", "text": "This call is recorded.", "interruptable": False},
      {"type": "payload", "data": {"card": "receipt"}},
      {"type": "end_session", "reason": "completed"}]}
  parts, _ = _BM._terminal_emit_parts(action, {}, _PARAMS)
  kinds = [p.kind for p in parts]
  assert "payload" in kinds, f"payload sibling was dropped: {kinds}"
  disclaimer = next(p for p in parts if p.kind == "text")
  assert disclaimer.disable_barge_in is True, "interruptable:False was flattened to plain text"
  assert kinds[-1] == "end_session"
  assert parts[-1].function_call.args["params"]["LIVE_AGENT_HANDOFF"]["xHeaders"]["nextappcode"] == "1016"


def test_terminal_emit_bails_on_transfer():
  # A transfer means the call continues, not a clean end: the deterministic emit must NOT preempt
  # (covers a custom/telephony redirect, not just the zombie transfer the caller guards).
  action = {"response": [
      {"type": "text", "text": "One moment."},
      {"type": "transfer", "agent": "human"},
      {"type": "end_session", "reason": "completed"}]}
  assert _BM._terminal_emit_parts(action, {}, _PARAMS) == (None, None)


# ── no silent drop: a failing state backend / read-only args must be LOGGED, not swallowed ─────
def test_resolve_end_params_logs_on_state_backend_error(caplog):
  import logging

  class _BadState:
    def get(self, _k):
      raise RuntimeError("state backend down")

  with caplog.at_level(logging.WARNING):
    assert _BM._resolve_end_params(_BadState(), {"_on_end": _ON_END}) is None
  assert caplog.records, "a failing state.get() must be logged, not silently swallowed"


def test_end_session_part_attaches_params_via_update_fallback(monkeypatch):
  # A protobuf-shaped args mapping that rejects []= but honours update() must still receive
  # the return (protobuf-safe mutation), with no warning.
  class _UpdateOnlyArgs:
    def __init__(self):
      self.data = {}

    def __setitem__(self, _k, _v):
      raise TypeError("no item assignment")

    def update(self, m):
      self.data.update(m)

  fc = _FC("end_session", {})
  fc.args = _UpdateOnlyArgs()
  part = _Part("end_session", function_call=fc)
  monkeypatch.setattr(_BM.Part, "from_end_session",
                      classmethod(lambda cls, reason="", escalated=False: part))
  out = _BM._end_session_part({"type": "end_session", "reason": "completed"}, _PARAMS)
  headers = out.function_call.args.data["params"]["LIVE_AGENT_HANDOFF"]["xHeaders"]
  assert headers["nextappcode"] == "1016"


def test_end_session_part_logs_when_params_cannot_attach(caplog, monkeypatch):
  import logging

  # Neither item assignment NOR update() takes -> the drop must be WARNED, not swallowed.
  class _ReadOnlyArgs:
    def __setitem__(self, _k, _v):
      raise TypeError("read-only args mapping")

    def update(self, *_a, **_k):
      raise TypeError("read-only args mapping")

  ro_fc = _FC("end_session", {})
  ro_fc.args = _ReadOnlyArgs()
  ro_part = _Part("end_session", function_call=ro_fc)
  monkeypatch.setattr(_BM.Part, "from_end_session",
                      classmethod(lambda cls, reason="", escalated=False: ro_part))
  with caplog.at_level(logging.WARNING):
    out = _BM._end_session_part({"type": "end_session", "reason": "completed"}, _PARAMS)
  assert out is ro_part, "must still return the end part (a bare end beats a crash)"
  assert caplog.records, "a dropped return must be logged, not silently swallowed"
