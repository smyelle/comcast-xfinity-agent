"""A `preempt=False`, `end=True` terminal announce must END the call — even when
it cascades on a NON-preempting turn.

Regression for ggh issue #719. The engine walks a terminal announce authored
`end=True, preempt=False` (the standard `... -> set_wrap_up -> goodbye` wrap-up
shape). On a preempting turn the `end_session` part rides inline on the directive
and CES tears the session down. On a NON-preempting turn (a plain user turn, no
event / task / readback preemption) the engine instead STASHES the announce parts
in `sm["_pending_announce_payloads"]` for `after_model` to deliver — and
`_extract_response_parts` kept only `type == "payload"` parts, silently dropping
`end_session` (and `transfer`). The engine marked `sm["status"] = "complete"`, so
offline sims read the call as ended while the live call hung: the caller's next
turn was accepted against a "complete" session and the agent looped.

These tests EXECUTE both halves rather than compare source:
  * the real engine (`flows.engine.loader.run_engine`) drives the cascade, and
  * the real `after_model_callback` (loaded off disk with CES's Part/LlmResponse
    globals stubbed, exactly as test_supersede.py does) delivers the stash.

Run: PYTHONPATH=packages/flows/src pytest \
     packages/flows/tests/test_terminal_announce_non_preempt_ends_session.py
"""
from __future__ import annotations

import importlib.util
import json
import os

import flows
from flows.engine import loader as fb


# ── CES-global stubs (mirror tests/test_supersede.py) ────────────────────────
class _Part:
  def __init__(self, kind, **d):
    self.kind = kind
    self.text = d.get("text")
    self.function_call = d.get("function_call")
    self.data = d.get("data")

  @classmethod
  def from_text(cls, text=""):
    return cls("text", text=text)

  @classmethod
  def from_end_session(cls, reason="", escalated=False):
    return cls("end_session", data={"reason": reason, "escalated": escalated})

  @classmethod
  def from_agent_transfer(cls, agent=""):
    return cls("transfer", data={"agent": agent})

  @classmethod
  def from_function_call(cls, name="", args=None):
    return cls("call",
               function_call=type("FC", (), {"name": name, "args": args or {}}))

  @classmethod
  def from_json(cls, s):
    return cls("payload", data=json.loads(s))


class _Resp:
  def __init__(self, parts, finish_reason=None):
    self.content = type("C", (), {"parts": parts})
    self.finish_reason = finish_reason

  @classmethod
  def from_parts(cls, parts, finish_reason=None):
    return cls(parts, finish_reason)


class _Ctx:
  def __init__(self, state):
    self.state = dict(state)


def _load_after_model():
  path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "src/flows/engine/framework/callbacks/after_model.py")
  spec = importlib.util.spec_from_file_location("_am_719", path)
  mod = importlib.util.module_from_spec(spec)
  for name in ("CallbackContext", "LlmRequest", "Content", "Tool", "ces_internal"):
    setattr(mod, name, type(name, (), {}))
  mod.Part = _Part
  mod.LlmResponse = _Resp
  spec.loader.exec_module(mod)
  return mod


_AM = _load_after_model()


def _emitted_kinds(sm, model_text):
  """Run the real after_model_callback with a normal model text render and
  return the KINDS of the parts CES would actually receive."""
  ctx = _Ctx({"sm": sm})
  out = _AM.after_model_callback(ctx, _Resp.from_parts([_Part.from_text(model_text)]))
  if out is None:
    # after_model injected nothing -> only the model's own render reaches CES.
    return ["text"]
  return [getattr(p, "kind", "?") for p in out.content.parts]


def _drive_to_non_preempting_terminal(terminal_slot):
  """Run the engine so `terminal_slot` (a preempt=False terminal announce)
  cascades on a plain user turn, and return the resulting sm."""
  f = flows.Flow("wrapup_test", root_agent="Wrap_Agent")
  f.add(flows.user_slot("topic", "What can I help with?"), terminal_slot)
  cfg = f.to_config()

  sm = fb.run_engine(cfg, {}, config_id="wrapup_test")["sm"]
  # The model filled the user_slot `topic` on the prior turn.
  sm.setdefault("filled", {})["topic"] = "track a package"
  # A plain user turn (no event / task / readback) -> the announce cascades.
  return fb.run_engine(cfg, sm, last_user_text="no thanks",
                       config_id="wrapup_test")


def test_engine_stashes_end_session_and_marks_complete_on_non_preempt():
  """The engine half: the terminal turn is non-preempting, so end_session is
  stashed (not inline) and the leg is marked complete."""
  out = _drive_to_non_preempting_terminal(
      flows.announce("goodbye", ["Thanks for calling. Goodbye!"],
                     requires=["topic"], end=True, preempt=False))
  inline = out["action"].get("response") or []
  assert not any(r.get("type") == "end_session" for r in inline), (
      "precondition: end_session must NOT be inline on a non-preempting turn")
  assert out["sm"]["status"] == "complete"
  stash = out["sm"].get("_pending_announce_payloads") or []
  assert any(r.get("type") == "end_session" for r in stash), (
      "precondition: end_session must be stashed for after_model to deliver")


def test_non_preempting_terminal_announce_emits_end_session():
  """The bug: after_model must deliver the stashed end_session to CES."""
  sm = _drive_to_non_preempting_terminal(
      flows.announce("goodbye", ["Thanks for calling. Goodbye!"],
                     requires=["topic"], end=True, preempt=False))["sm"]
  kinds = _emitted_kinds(sm, "Thanks for calling. Goodbye!")
  assert "end_session" in kinds, (
      f"end_session dropped on a non-preempting terminal announce; CES got {kinds}")


def test_non_preempting_terminal_transfer_emits_transfer():
  """The twin: a `transfer_to=` terminal announce on a non-preempting turn must
  deliver the transfer part, not drop it."""
  sm = _drive_to_non_preempting_terminal(
      flows.announce("route", ["One moment, connecting you."],
                     requires=["topic"], transfer_to="Billing_Agent",
                     preempt=False))["sm"]
  kinds = _emitted_kinds(sm, "One moment, connecting you.")
  assert "transfer" in kinds, (
      f"transfer dropped on a non-preempting terminal announce; CES got {kinds}")


def test_stashed_chip_payloads_still_delivered():
  """Guard: the original payload path (question / announce chips) is unchanged —
  a stashed payload part is still converted and delivered."""
  ctx = _Ctx({"sm": {"_pending_payloads": [
      {"type": "payload", "data": {"chips": ["Yes", "No"]}}]}})
  out = _AM.after_model_callback(ctx, _Resp.from_parts([_Part.from_text("Anything else?")]))
  kinds = [getattr(p, "kind", "?") for p in out.content.parts]
  assert kinds == ["text", "payload"], kinds
