"""Regression: a terminal task-exhaust must NOT wedge the CES reasoning-loop cap.

Guards the fix for the "400 Agent has reached the limit of 10 reasoning loops"
deadlock. Everything here runs against the REAL engine (`slot_filling_engine`), the
REAL intake (`slot_intake`) and the REAL `before_model` callback via
`flows.engine.loader`; CES itself is simulated by a small re-invoke driver (engine ->
before_model -> dispatch any function_call preempt -> re-invoke WITHOUT a model turn
-> repeat, capped at 10 like CES's reasoning-loop cap).

THE BUG (two interacting sites, both fixed here):

  A terminal task fires, its executor result does NOT satisfy `success_check` (a
  failed/async downstream tool), so the engine's task-exhaust path returns a
  MESSAGE-ONLY preempt ("An error occurred.", ``speech_class == "exhaust"``). On a
  post-routing AUTO-TURN the caller's utterance was already consumed by the routing
  turn, so `llm_request.contents` is EMPTY. Before the fix:

    1. before_model.py gate dropped a message-only preempt on empty contents (it only
       honored a preempt riding a function_call), so the turn-ending exhaust line was
       muted and the model ran without progress; and
    2. python_code.py's bare-exhaust branch never latched `_task_written_off` (only the
       `fill` branch did), so the still-eligible failed task re-fired next pass.

  fire -> drop -> re-fire -> ... -> reasoning-loop cap -> "400 reasoning loops".

THE FIX: before_model honors an ``exhaust`` verdict on empty contents (so the terminal
line is delivered), AND the bare-exhaust branch writes the task off (so it stops
re-firing). Either alone breaks the loop; both are asserted below.
"""

from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

from flows.engine import loader as fb  # noqa: E402


# --------------------------------------------------------------------------- #
# CES-shaped stubs (the pattern from tests/test_pass_a_language_switch.py).

class _Part:
  def __init__(self, kind, **d):
    self.kind = kind
    self.text = d.get("text")
    self.function_call = d.get("function_call")
    self.__dict__.update({k: v for k, v in d.items() if k != "function_call"})

  @classmethod
  def from_text(cls, text=""):
    return cls("text", text=text)

  @classmethod
  def from_function_call(cls, name="", args=None):
    fc = type("FC", (), {"name": name, "args": args or {}})
    return cls("call", function_call=fc, name=name, args=args or {})

  @classmethod
  def from_agent_transfer(cls, agent=""):
    return cls("transfer", text=agent)

  @classmethod
  def from_json(cls, payload=""):
    return cls("json", payload=payload)

  @classmethod
  def from_end_session(cls, reason="", escalated=False):
    return cls("end_session", reason=reason, escalated=escalated)


class _Resp:
  def __init__(self, parts):
    self.content = type("C", (), {"parts": parts})
    self.parts = parts
    self.partial = None

  @classmethod
  def from_parts(cls, parts):
    return cls(parts)


class _Ctx:
  def __init__(self, state=None):
    self.state = dict(state or {})


class _Config:
  def __init__(self, system_instruction="base"):
    self.system_instruction = system_instruction
    self.hidden = []
    self.tools = None

  def hide_tool(self, name):
    self.hidden.append(name)


class _Request:
  def __init__(self, system_instruction="base", contents=None):
    self.config = _Config(system_instruction)
    self.contents = contents if contents is not None else []
    self.model = "gemini-3.1-flash-live"


def _load_abs(path, name):
  spec = importlib.util.spec_from_file_location(name, path)
  mod = importlib.util.module_from_spec(spec)
  for g in ("CallbackContext", "LlmRequest", "Content", "Tool", "ces_internal"):
    setattr(mod, g, type(g, (), {}))
  mod.Part = _Part
  mod.LlmResponse = _Resp
  mod.tools = type("tools", (), {})
  spec.loader.exec_module(mod)
  return mod


_BM = _load_abs(
    os.path.join(_ROOT, "src/flows/engine/framework/callbacks/before_model.py"),
    "_bm_loop")


# --------------------------------------------------------------------------- #
# A minimal FedEx-shaped flow: one collected slot + one terminal task whose
# executor result never satisfies `success_check` (a failed/async downstream tool).

def _failing_task_cfg():
  return {
      "slots": [
          {"name": "pickup_type", "source": "user",
           "setter": "set_pickup_type", "ask": "What type of pickup?"},
          {"name": "confirm_msg", "source": "task:finalize"},
      ],
      "tasks": [{
          "name": "finalize", "tool": "fedex_dag", "inputs": ["pickup_type"],
          "outputs": {"message": "confirm_msg"},
          "success_check": "success", "terminal": True,
          "requires": ["pickup_type"],
          "condition": "lambda f: bool(f.get('pickup_type'))",
          "then_say": "{confirm_msg}",
      }],
      "gate_slot": "active_flow",
  }


def _seed(cfg):
  sm = fb.seed_sm(cfg)
  sm["filled"] = {"active_flow": "pickup", "pickup_type": "caja"}
  sm["pending"] = {}
  sm["active_flow"] = "pickup"
  return sm


CAP = 10  # CES aborts after 10 reasoning passes per turn.


def _drive(cfg, sm, empty_contents, bm=_BM):
  """Run the CES re-invoke cycle for one turn. Returns (outcome, passes, sm, trace)."""
  trace = []
  for i in range(1, CAP + 2):
    out = fb.run_engine(cfg, sm, last_user_text="", scanned_user_text="",
                        config_id="pickup", n_user_turns=2)
    action, sm = out["action"], out["sm"]
    ctx = _Ctx({"sm": sm})
    req = _Request()
    # empty_contents models the post-routing auto-turn (utterance already consumed).
    req.contents = [] if empty_contents else [
        type("C", (), {"role": "user", "parts": [_Part.from_text("hi")]})]
    res = bm._apply_directive(ctx, req, sm, action, action.get("tag") or "e")
    sm = ctx.state["sm"]
    if isinstance(res, dict) and res.get("decision") == "OK":
      # The preempt was DROPPED -> model runs. On an empty auto-turn the model has
      # nothing to react to and makes no progress, so CES re-invokes the engine.
      trace.append(("dropped->model", str(action.get("message"))[:24]))
      continue
    fc_names = [getattr(getattr(p, "function_call", None), "name", None)
                for p in res.parts]
    real = [n for n in fc_names if n]
    if real:
      # CES dispatches the executor; the result lacks `success` (failed tool).
      trace.append(("preempt-fire", real[0]))
      sm = fb.run_intake(real[0], {"message": "x"}, sm)["sm"]
      continue
    trace.append(("preempt-speaks", str(action.get("message"))[:24]))
    return "YIELD", i, sm, trace
  return "DEADLOCK", CAP + 1, sm, trace


# --------------------------------------------------------------------------- #
# 1. The gate: an ``exhaust`` verdict is honored on empty contents.

def _apply(action, contents):
  ctx = _Ctx({"sm": {}})
  req = _Request()
  req.contents = contents
  res = _BM._apply_directive(ctx, req, {}, action, action.get("tag", "t"))
  if isinstance(res, dict) and res.get("decision") == "OK":
    return "OK"          # preempt dropped, model runs
  return "PREEMPT"       # preempt delivered


def test_gate_honors_exhaust_verdict_on_empty_contents():
  """before_model must DELIVER a terminal exhaust line even on empty contents.

  The widening is narrow on purpose: only ``speech_class == "exhaust"`` (a terminal
  give-up verdict) and a function_call preempt ride an empty auto-turn; a plain
  message-only preempt with no terminal marker is still dropped, so a non-terminal
  line never speaks to a caller who said nothing.
  """
  uc = [type("C", (), {"role": "user", "parts": [_Part.from_text("hi")]})]
  exhaust = {"preempt": True, "message": "An error occurred.",
             "speech_class": "exhaust"}
  plain = {"preempt": True, "message": "one moment"}
  fc_pre = {"preempt": True, "message": "",
            "function_call": {"name": "fedex_dag", "args": {}}}

  assert _apply(exhaust, []) == "PREEMPT"     # <-- terminal line now delivered (the fix)
  assert _apply(exhaust, uc) == "PREEMPT"     # unchanged when there IS user contents
  assert _apply(plain, []) == "OK"            # non-terminal message still dropped (narrow)
  assert _apply(plain, uc) == "PREEMPT"       # ...but delivered when contents are present
  assert _apply(fc_pre, []) == "PREEMPT"      # function_call still honored when empty


# --------------------------------------------------------------------------- #
# 2. The full turn: a failing terminal task yields cleanly instead of looping.

def test_failing_task_yields_on_empty_contents_auto_turn():
  """The reported deadlock, now fixed: on an empty-contents auto-turn a failing
  terminal task exhausts, the engine writes it off (so it stops re-firing) and
  before_model delivers the exhaust line (so the turn ends). No reasoning-loop cap."""
  cfg = _failing_task_cfg()
  outcome, passes, sm, trace = _drive(cfg, _seed(cfg), empty_contents=True)
  assert outcome == "YIELD", trace
  assert passes <= CAP, trace
  # the exhausted task is latched off, so a re-invoke cannot re-dispatch it
  assert "finalize" in (sm.get("_task_written_off") or []), sm.get("_task_written_off")


def test_non_terminal_failing_task_is_not_written_off():
  """The write-off is gated on `terminal`. A NON-terminal task that keeps failing is a
  legitimate retry-out-loud ladder — it SPEAKS its disposition and yields to the caller
  each turn, so it never wedges the empty-contents auto-turn loop the way a silent terminal
  give-up does. Writing it off would silence it, which a live sim reports as a stalled
  agent (service `test_uj_live_sim.test_an_agent_that_keeps_retrying_out_loud_is_not_a_stall`).
  Same failure as test 2, but on a non-terminal task -> stays eligible."""
  cfg = _failing_task_cfg()
  cfg["tasks"][0].pop("terminal", None)   # the one change: keep retrying out loud
  outcome, passes, sm, trace = _drive(cfg, _seed(cfg), empty_contents=True)
  assert outcome == "YIELD", trace
  assert passes <= CAP, trace
  assert "finalize" not in (sm.get("_task_written_off") or []), sm.get("_task_written_off")


# --------------------------------------------------------------------------- #
# 3. The write-off must SELF-HEAL: a corrected input re-enables the task.

def test_correction_after_exhaust_re_enables_the_written_off_task():
  """Companion to the write-off. The bare-exhaust write-off latches a failed terminal
  task off so it stops re-firing (test 2) — but a caller who then CORRECTS one of that
  task's inputs must get it to run again, exactly as `_abandon_journey` re-opens a task on
  an intent switch. Without the self-heal in `_apply_correction_pending` the write-off
  outlives the cleared result and `_task_fireable` bails, silently DROPPING the corrected
  value. This pins that `_apply_correction_pending` clears both write-off sets alongside
  the task_results/_retries it already clears."""
  eng = fb.load_engine()
  config = {
      "slots": [
          {"name": "active_flow"},
          {"name": "pickup_type", "source": "user", "setter": "set_pickup_type"},
          {"name": "confirm_msg", "source": "task:finalize"},
      ],
      "tasks": [{
          "name": "finalize", "tool": "fedex_dag", "inputs": ["pickup_type"],
          "outputs": {"message": "confirm_msg"}, "success_check": "success",
          "terminal": True, "requires": ["pickup_type"],
      }],
  }
  sm = {
      "filled": {"active_flow": "pickup", "pickup_type": "caja", "confirm_msg": ""},
      "pending": {}, "deferred": {},
      "task_results": {"finalize": {"message": "x"}},
      "_retries": {"finalize": 1},
      "_task_written_off": ["finalize"],       # exhausted + latched off (the test-2 state)
      "_fanout_written_off": ["finalize"],     # the sibling set heals the same way
      "_correction_pending": [
          {"slot": "pickup_type", "value": "sobre", "old_type": "str"}],
      "status": "in_progress",
      "_slot_requires": {"finalize": ["pickup_type"]},
  }
  eng._apply_correction_pending(sm, config)
  # the correction re-decides the input, so the exhausted task is no longer written off
  assert not sm.get("_task_written_off"), sm.get("_task_written_off")
  assert not sm.get("_fanout_written_off"), sm.get("_fanout_written_off")
  # ...and its stale result/retries were cleared, so it will actually re-run
  assert sm.get("task_results") == {}
  assert sm.get("_retries") == {}
  assert sm.get("pending", {}).get("pickup_type") == "sobre"


def test_failing_task_yields_when_contents_are_present():
  """The control: with user contents present the exhaust line was always deliverable,
  so this path yielded even before the fix. It stays green -- the fix does not regress
  the contents-present behavior."""
  cfg = _failing_task_cfg()
  outcome, passes, _sm, trace = _drive(cfg, _seed(cfg), empty_contents=False)
  assert outcome == "YIELD", trace
  assert passes <= 3, trace


# --------------------------------------------------------------------------- #
# 4. The write-off is SCOPED: a clear_slots ladder stays eligible (fill-branch parity).

def _clear_slots_task_cfg():
  """A terminal task whose `on_failure` DROPS its input on failure and asks again."""
  return {
      "slots": [
          {"name": "zip_code", "source": "user", "setter": "set_zip", "ask": "Zip?"},
          {"name": "rate_msg", "source": "task:quote"},
      ],
      "tasks": [{
          "name": "quote", "tool": "fedex_dag", "inputs": ["zip_code"],
          "outputs": {"message": "rate_msg"}, "success_check": "success",
          "terminal": True, "requires": ["zip_code"],
          "condition": "lambda f: bool(f.get('zip_code'))",
          "on_failure": {"max_retries": 0, "clear_slots": ["zip_code"],
                         "retry_say": "That didn't work, try another."},
      }],
      "gate_slot": "active_flow",
  }


def test_clear_slots_ladder_is_not_written_off_on_exhaust():
  """The write-off is scoped exactly like the `fill` write-off — NOT a blanket rule about
  exhausted tasks. A `clear_slots` ladder means the opposite of `fill`: "drop the bad input
  and let the caller supply another", so it MUST stay eligible. It clears its input on
  failure, so its `requires` is unmet and it cannot wedge; being written off would instead
  drop it from `_task_fireable` and strand the re-fire a fresh value is meant to trigger
  (a normal setter fill, which `_apply_correction_pending` never heals). This pins the
  carve-out: after the SAME exhaust that writes a plain terminal task off (test 2), a
  clear_slots ladder is left OUT of `_task_written_off`, and its input is cleared."""
  cfg = _clear_slots_task_cfg()
  sm = fb.seed_sm(cfg)
  sm["filled"] = {"active_flow": "quote", "zip_code": "00000"}
  sm["pending"] = {}
  sm["active_flow"] = "quote"
  # fire -> fail -> exhaust (max_retries=0)
  out = fb.run_engine(cfg, sm, last_user_text="", config_id="quote", n_user_turns=1)
  act, sm = out["action"], out["sm"]
  if act.get("function_call"):
    sm = fb.run_intake(act["function_call"]["name"], {"message": "x"}, sm)["sm"]
  out = fb.run_engine(cfg, sm, last_user_text="", config_id="quote", n_user_turns=1)
  act, sm = out["action"], out["sm"]
  # the ladder cleared its input and is NOT written off (unlike a plain terminal task)
  assert "quote" not in (sm.get("_task_written_off") or []), sm.get("_task_written_off")
  assert not sm.get("filled", {}).get("zip_code"), sm.get("filled")
