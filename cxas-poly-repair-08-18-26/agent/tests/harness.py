#!/usr/bin/env python3
"""Walk a whole call offline: the real hooks, the real engine, the real tool bodies.

Reads the EMITTED config, so rebuild first: `python build.py --out ./built`.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths()

import hooks  # noqa: E402
from flows.engine import loader  # noqa: E402
from flows.sim import engine_sim  # noqa: E402

#: Passes `set_account_number`'s validation (16 digits).
ACCOUNT = "8344200010126021"

# Tool bodies `drive()` runs for real, because their RETURN is not their only effect.
# Every `verdict_*` body writes `context.state` -- `wifi_tip_rejoin`, `reboot_offered` --
# which the hook reads back as the tip counter and the reboot gate, and
# `settle_diagnostics` derives all six statuses from the legs. A hand-fed result looks
# identical on the turn and leaves those unset for the rest of the call.
_RUNNABLE_PREFIXES = ("verdict_",)
_RUNNABLE_EXACT = frozenset({"settle_diagnostics", "build_device_query"})

# The tools that reach a backend. `drive()` stops on these and the scenario supplies the
# payload, so the status vocabulary is an explicit input to each test rather than
# whatever a fake returns today.
BACKEND_TOOLS = frozenset({
    "resolve_account_context",
    "SweepLegs_leg_outage_leg",
    "SweepLegs_leg_convoy_leg",
    "resolve_specialists_remote",
    "xfinity_faq_search",
})

# What CES puts in `llm_request.contents` on a turn IT made rather than the caller.
# Verbatim from a recorded voice session, because the hooks tell a manufactured turn from
# a caller's by reading these, and a driver that hands them a bare "" grades a shape the
# platform never sends.
INACTIVITY_MARKER = "<context>no user activity detected for 5 seconds.</context>"
COMPLETION_MARKER = ('<context>function [{tool}] completed with response '
                     '{{\n  "result": {{}}\n}}</context>')


def load_config(app_dir: str = "built", config_id: str = "repair") -> dict:
  """The emitted DAG config for one flow."""
  path = os.path.join(app_dir, "tools", f"{config_id}_dag", "python_function",
                      "python_code.py")
  namespace: dict = {}
  with open(path) as fh:
    exec(compile(fh.read(), path, "exec"), namespace)  # noqa: S102 - our own emitted file
  return namespace[f"{config_id}_dag"]()


def framework_root(app_dir: str = "built") -> str:
  """The app's OWN tools: its setters and executors are not in the framework bundle."""
  return os.path.join(app_dir, "tools")


# --- The CES callback fakes --------------------------------------------------
# The shape `scope_check.py` drives `before_model` with.


class _Part:

  def __init__(self, text):
    self.text = text


class _Content:

  def __init__(self, text, role="user"):
    self.role = role
    self.parts = [_Part(text)]


class _Request:

  def __init__(self, text):
    self.contents = [_Content(text)]


class _Ctx:

  def __init__(self, state):
    self.state = state
    self.events = []


class Call:
  """One offline call, walked step by step.

  One session per scenario, never shared: `ces_state` carries the verdict bodies' latches
  and the hook reads them back, so a reused call arrives with a spent tip counter and an
  armed reboot gate.
  """

  def __init__(self, config: dict, flow: str = "repair", account: str | None = ACCOUNT,
               channel: str | None = None, app_dir: str = "built"):
    self.config = config
    self.flow = flow
    self._root = framework_root(app_dir)
    self.task_to_tool = {t["name"]: t["tool"] for t in config.get("tasks", [])
                         if t.get("tool")}
    self._compiled = loader.load_engine()._compile_config(config)
    #: Everything the caller heard, in order, across the whole call. The coverage gate
    #: mines this; a scenario never has to opt in.
    self.transcript: list[str] = []
    self.session_id, self.result = engine_sim.start(config, flow, channel=channel,
                                                    framework_root=self._root)
    self._session = engine_sim._SESSIONS[self.session_id]
    if account is not None:
      # What upstream hands over on a real call. Without it `before_agent` early-returns
      # at `if not account_number` (hooks.py:489) on the account turn, so
      # `diagnostics_triggered` lags a turn and the already-swept branch -- the only
      # writer of `reboot_answer_allowed` -- is late. Scenario E covers the spoken path.
      self._session.ces_state["accountNumber"] = account
    self._record(self.result)

  # --- state ---------------------------------------------------------------

  @property
  def sm(self) -> dict:
    return self._session.sm

  @property
  def state(self) -> dict:
    """The CES session state a deployed tool reads and writes as `context.state`."""
    return self._session.ces_state

  @property
  def filled(self) -> dict:
    return self.sm.get("filled", {})

  @property
  def status(self) -> str:
    return self.result.get("status", "")

  def pending_tool(self) -> str | None:
    """The tool the engine is asking for right now, if any."""
    return (self.result.get("function_call") or {}).get("name")

  def task_for_tool(self, tool: str) -> str | None:
    """Which rung a dispatched tool belongs to.

    Two rungs may share one tool -- the device answer and its multi-device twin run the
    same search -- so the tool name alone cannot say which spoke, and naming the wrong
    one would have the coverage gate credit a rung nobody walked. Separated by the
    condition that distinguishes them, falling back to the first declared, which is the
    one the engine would have picked.
    """
    candidates = [t for t in self.config.get("tasks", []) if t.get("tool") == tool]
    if len(candidates) <= 1:
      return candidates[0]["name"] if candidates else None
    engine = loader.load_engine()
    by_name = {t["name"]: t for t in self._compiled["tasks"]}
    for task in candidates:
      if engine._is_task_active(by_name[task["name"]], self.filled):
        return task["name"]
    return candidates[0]["name"]

  def ended(self) -> dict | None:
    """The `end_session` part of the latest step, if the call ended on it."""
    for part in self.result.get("response_parts") or []:
      if isinstance(part, dict) and part.get("end_session"):
        return part
      if isinstance(part, dict) and "escalated" in part:
        return part
    return None

  # --- steps ---------------------------------------------------------------

  def turn(self, text: str = "", inactivity: bool = False,
           marker: str | None = None) -> dict:
    """A caller turn: both callbacks, the engine, then the in-flight-job poll.

    `marker` is what the CALLBACKS see when the platform, not the caller, made the turn.
    The engine still gets `text` (blank on a tick), but the hooks read
    `llm_request.contents`, and a hook that has to tell a manufactured turn from a
    caller's cannot be graded against a bare "" the platform never sends.
    """
    self._hooks(text if marker is None else marker)
    self._step({"kind": "user_text", "text": text, "is_inactivity": inactivity})
    return self._drain_poll()

  def silence(self) -> dict:
    """An inactivity tick. Walks the `no_input` ladder rather than the caller's."""
    return self.turn("", inactivity=True, marker=INACTIVITY_MARKER)

  def delivered(self, tool: str = "SweepLegs_leg_outage_leg") -> dict:
    """The turn CES manufactures to deliver an ASYNCHRONOUS tool's completion.

    Neither a caller turn nor an inactivity tick, and that is the whole point: the
    platform pushes the tool's result as a `<context>` part on a turn of its own, the
    caller says nothing, and the caller-turn counter does not move. Both sweep legs are
    ASYNCHRONOUS, so on this agent one of these lands immediately after the turn the
    checks were dispatched on -- the turn the clarifying question is asked on.

    The payload is deliberately empty: the leg bodies run inline and everything they
    publish has already been ingested by the time this arrives, so the engine learns
    nothing from it. What it SAYS on a turn that told it nothing is what this pins.
    """
    self._hooks(COMPLETION_MARKER.format(tool=tool))
    out = loader.run_engine(self.config, self._session.sm, last_user_text="",
                            config_id=self._session.config_id,
                            framework_root=self._root,
                            n_user_turns=self._session.n_user_turns)
    self._session.sm = out["sm"]
    self.result = engine_sim._derive_result(self._session, out)
    self._record(self.result)
    return self.result

  def turn_settling(self, text: str, **payload) -> dict:
    """A caller turn on which the outstanding remote job answers THIS turn's poll.

    `turn()` followed by `remote_returns()` is a different sequence, not a shorthand for
    this one: `_drain_poll` answers the poll with `running`, so the DAG is walked once
    before the result exists and again after it, and a rung dispatched on that first pass
    has already spoken its filler by the time the result arrives. Live, the poll is
    answered before the DAG is walked at all -- which is the shape a rung that has to lead
    the verdict on the caller's OWN turn must be graded against.
    """
    self._hooks(text)
    self._step({"kind": "user_text", "text": text, "is_inactivity": False})
    return self.remote_returns(**payload)

  def setter(self, tool: str, **args) -> dict:
    """Call a setter as the model would, running the real body and its validation."""
    self._hooks("")
    return self._step({"kind": "setter_call", "tool": tool, "args": args})

  def task_returns(self, task: str, success: bool = True, **outputs) -> dict:
    """Hand a backend task its payload."""
    return self._step({"kind": "task_result", "task_name": task, "success": success,
                       "result": outputs})

  def _drain_poll(self) -> dict:
    """Answer the platform's poll for a job still in flight, so the turn can go on.

    While a job is outstanding the engine returns EARLY, before the DAG is walked, with a
    partial action carrying `function_calls` (plural) for the job's status tool. That is
    a round trip inside the turn, not the end of it: the poll is guarded to fire once per
    turn (`_remote_polled_turn` against the wait clock), so once the status tool answers,
    the next pass skips the poll and walks the DAG on the caller's own turn.

    A driver that does not complete the round trip loses that turn, and every rung that
    can only fire while the checks are running reads as unreachable. `engine_sim` cannot
    complete it alone: `_derive_result` reads `function_call`, singular.

    `status: "running"` is the contract's word for still-in-flight and resolves nothing
    (`slot_intake:880-882`), so the wait stays a wait.
    """
    for _ in range(4):
      in_flight = (self.sm.get("_awaiting_async") or {}).items()
      waiting = {name: mark for name, mark in in_flight if (mark or {}).get("remote")}
      if not waiting or self.pending_tool():
        break
      remotes = self.config.get("remote_tools") or {}
      answered = False
      for task, mark in sorted(waiting.items()):
        status_tool = (remotes.get(self.task_to_tool.get(task) or "")
                       or {}).get("status_tool")
        job = (mark.get("remote") or {}).get("job")
        if not status_tool or not job:
          continue
        self._deliver(status_tool, {"status": "running"})
        answered = True
      if not answered:
        break
    return self.result

  def _deliver(self, status_tool: str, payload: dict) -> dict:
    """Push a status-tool answer through intake, then re-invoke the engine."""
    intake = loader.run_intake(status_tool, payload, self.sm,
                               framework_root=self._root)
    self._session.sm = intake["sm"]
    out = loader.run_engine(self.config, self._session.sm, last_user_text="",
                            config_id=self._session.config_id,
                            framework_root=self._root,
                            n_user_turns=self._session.n_user_turns)
    self._session.sm = out["sm"]
    self.result = engine_sim._derive_result(self._session, out)
    self._record(self.result)
    return self.result

  def remote_returns(self, **payload) -> dict:
    """Finish a remote job by delivering its payload through the STATUS tool.

    `Specialists` is two-phase: the start call books a handle and returns without
    completing, and only the status tool carries the answer. Feeding the outputs to the
    start tool is stamped `remote_bad_handle`, so a healthy line reports a failed sweep.
    `engine_sim` always maps a task to its START tool, so this cannot go through `step`.
    """
    payload.setdefault("status", "done")
    return self._deliver("resolve_specialists_remote__status", payload)

  def drive(self, limit: int = 16) -> tuple[list[str], list[str]]:
    """Run the engine's cascade out, returning `(rungs fired, lines heard)` in order.

    The assertion unit, because a rung's `then_say` renders only once its result is fed
    back: neither half of a split rung is visible from the fire step alone. Stops on a
    backend tool rather than calling it.
    """
    rungs: list[str] = []
    lines: list[str] = list(self._lines(self.result))
    for _ in range(limit):
      call = self.result.get("function_call")
      if not call:
        break
      tool = call["name"]
      if tool in BACKEND_TOOLS or not self._runnable(tool):
        break
      task = self.task_for_tool(tool)
      if task is None:
        break
      returned = loader.call_setter(tool, call.get("args") or {}, self._root,
                                    sm=self.sm, state=self.state)
      rungs.append(task)
      self.task_returns(task, **(returned if isinstance(returned, dict) else {}))
      lines.extend(self._lines(self.result))
    return rungs, [ln for ln in lines if ln]

  # --- internals -----------------------------------------------------------

  @staticmethod
  def _runnable(tool: str) -> bool:
    return tool in _RUNNABLE_EXACT or tool.startswith(_RUNNABLE_PREFIXES)

  def _hooks(self, text: str) -> None:
    state = self._session.ces_state
    state["sm"] = self._session.sm
    hooks.before_agent_callback(_Ctx(state))
    hooks.before_model_callback(_Ctx(state), _Request(text))
    self._session.sm = state["sm"]

  def _step(self, req: dict) -> dict:
    self.result = engine_sim.step(dict(req, session_id=self.session_id))
    self._record(self.result)
    return self.result

  @staticmethod
  def _lines(result: dict) -> list[str]:
    """What the caller hears on one step: the message, then any announce parts."""
    out = []
    text = (result.get("agent_text") or "").strip()
    if text:
      out.append(text)
    for part in result.get("response_parts") or []:
      if isinstance(part, dict):
        extra = (part.get("text") or "").strip()
        if extra and extra != text:
          out.append(extra)
    return out

  def _record(self, result: dict) -> None:
    self.transcript.extend(self._lines(result))


# --- The step language scenarios are written in --------------------------------


class Step:
  """One move in a scenario, plus what must be true after it.

  Assertions are keyword arguments, so a step reads as a sentence: `say("no thanks",
  text=scripts.SAY_WIFI_DECLINED)`. Copy assertions match approved text verbatim; a
  presence-only check cannot see a split rung whose halves reorder or go silent.

  Recognised assertions:
    `text`       the whole of what the caller hears on this step, exactly
    `says`       lines that must each appear, exactly, among this step's lines
    `joined`     the step's lines, space-joined, equal this (for a split rung)
    `rungs`      the ordered rung names a `walk()` fired
    `fc`         the tool the engine is asking for
    `next`       the engine's next action
    `status`     the session status
    `escalated`  the `end_session` disposition, True or False
    `filled`     slot values that must hold now; `None` asserts UNfilled
    `never`      lines that must NOT be spoken on this step
    `silent`     nothing is spoken on this step
  """

  KEYS = frozenset({"text", "says", "joined", "rungs", "fc", "next", "status",
                    "escalated", "filled", "silent", "never"})

  def __init__(self, kind: str, label: str, payload: dict, asserts: dict):
    unknown = set(asserts) - self.KEYS
    if unknown:
      raise ValueError(f"unknown assertion(s) {sorted(unknown)} on {kind} step")
    self.kind = kind
    self.label = label
    self.payload = payload
    self.asserts = asserts

  def __repr__(self):
    return f"{self.kind}({self.label})"


def say(utterance: str, **asserts) -> Step:
  """The caller speaks; cue-bearing slots fill from it deterministically.

  The parameter is `utterance` so `text=` stays free for what the AGENT says back.
  """
  return Step("say", repr(utterance), {"text": utterance}, asserts)


def quiet(**asserts) -> Step:
  """The caller says nothing. Walks the `no_input` ladder, not the caller's."""
  return Step("quiet", "silence", {}, asserts)


def delivered(tool: str = "SweepLegs_leg_outage_leg", **asserts) -> Step:
  """An ASYNCHRONOUS tool's completion push: a turn the platform made, carrying nothing."""
  return Step("delivered", tool, {"tool": tool}, asserts)


def fill(tool: str, _label: str | None = None, **kwargs) -> Step:
  """The model calls a setter, with the real body and real validation.

  For the slots no cue map reaches offline: classifier-backed intent slots, plain
  `user_slot`s, and the control setters. Anything a cue CAN fill belongs in `say()`, so
  the cue map is graded too.
  """
  asserts = {k: v for k, v in kwargs.items() if k in Step.KEYS}
  args = {k: v for k, v in kwargs.items() if k not in Step.KEYS}
  return Step("fill", _label or tool, {"tool": tool, "args": args}, asserts)


def gate(account: str = "clear", mac: str = "AA:BB:CC:DD:EE:FF", **asserts) -> Step:
  """`ContextGate` answers, with the same two skip branches the real tool has.

  Derived here rather than spelled out per scenario: a restricted account skips every
  check and a MAC-less one cannot reach the hardware, and both branches return statuses
  the ordinary path leaves to the legs (`resolve_account_context:147-157`). Omit them and
  `Settle` never becomes eligible, so the turn goes quiet and the scenario passes by
  asserting nothing.
  """
  has_mac = bool(mac and mac != "NOT_FOUND")
  out = {"mock_config_string": "{}", "account_status": account,
         "cable_modem_mac": mac or "NOT_FOUND",
         "has_mac": "true" if has_mac else "false"}
  if account != "clear":
    out.update(diagnostics_complete=True, network_status="skipped",
               gateway_status="skipped", outage_status="none", convoy_status="none",
               wifi_status="skipped")
  elif not has_mac:
    out.update(network_status="healthy", gateway_status="offline")
  return Step("task", f"gate({account})",
              {"task": "ContextGate", "success": True, "outputs": out}, asserts)


def legs(outage: str = "none", action: str = "none", **asserts) -> Step:
  """Both sweep legs answer.

  `action` is the convoy RECOMMENDATION, not the status: `settle_diagnostics:26-38` maps
  one to the other.
  """
  return Step("legs", f"legs(outage={outage}, action={action})",
              {"outage": outage, "action": action}, asserts)


def specialists(net: str = "healthy", gw: str = "healthy", wifi: str = "healthy",
                tech: str = "", **asserts) -> Step:
  """The remote specialist job starts and then answers, through its STATUS tool."""
  return Step("specialists", f"specialists(net={net}, gw={gw}, tech={tech!r})",
              {"net": net, "gw": gw, "wifi": wifi, "tech": tech}, asserts)


def remote(status: str = "done", **kwargs) -> Step:
  """Deliver a remote job's payload through its status tool, or report it failed.

  Separate from `specialists()` so the two phases can be tested apart: a job that starts
  and never answers is a different failure from one that answers badly, and they reach
  the caller as the same line by different routes.
  """
  asserts = {k: v for k, v in kwargs.items() if k in Step.KEYS}
  payload = {k: v for k, v in kwargs.items() if k not in Step.KEYS}
  payload["status"] = status
  return Step("remote", f"remote({status})", payload, asserts)


def say_settling(utterance: str, net: str = "healthy", gw: str = "healthy",
                 wifi: str = "healthy", tech: str = "", **asserts) -> Step:
  """The caller speaks, and the outstanding specialist job reports on that same turn.

  The turn a walkthrough answer and a diagnostic result collide on, which is the ordinary
  case rather than a rare one once the walkthrough can start during the sweep. `say()`
  followed by `remote()` is a different sequence -- see `Call.turn_settling`.
  """
  return Step("say_settling", f"say_settling({utterance!r})",
              {"text": utterance, "net": net, "gw": gw, "wifi": wifi, "tech": tech},
              asserts)


def task_fails(task: str, **kwargs) -> Step:
  """A backend task returns a failure, dropping it into its `on_failure` ladder."""
  asserts = {k: v for k, v in kwargs.items() if k in Step.KEYS}
  outputs = {k: v for k, v in kwargs.items() if k not in Step.KEYS}
  return Step("task", f"{task} fails",
              {"task": task, "success": False, "outputs": outputs}, asserts)


def task_returns(task: str, **kwargs) -> Step:
  """A backend task answers successfully."""
  asserts = {k: v for k, v in kwargs.items() if k in Step.KEYS}
  outputs = {k: v for k, v in kwargs.items() if k not in Step.KEYS}
  return Step("task", task, {"task": task, "success": True, "outputs": outputs},
              asserts)


def walk(**asserts) -> Step:
  """Run the engine's cascade out and assert on the whole of it.

  Where a journey is really graded: `rungs=` pins which rungs spoke and in what order,
  `says=` pins their approved copy.
  """
  return Step("walk", "walk", {}, asserts)


class Scenario:
  """One call, and the ordered steps that walk it."""

  def __init__(self, sid: str, title: str, steps: list[Step], flow: str = "repair",
               account: str | None = ACCOUNT, endings: tuple[str, ...] = ()):
    self.sid = sid
    self.title = title
    self.steps = steps
    self.flow = flow
    self.account = account
    # Endings that are not a rung -- an escalate disposition, a cancel, a slot's
    # on_exhaust -- named so the coverage gate can count them.
    self.endings = endings


def new_call(config: dict, app_dir: str = "built", **kwargs) -> Call:
  """A fresh call, with the session store cleared first.

  `engine_sim._SESSIONS`, the compiled-config cache and `loader`'s module cache are all
  process-global, and `call_setter` binds `context` into a tool module's globals for the
  duration of the call. Scenarios run SERIALLY, one session at a time.
  """
  engine_sim.reset_store()
  loader.set_framework_root(framework_root(app_dir))
  return Call(config, app_dir=app_dir, **kwargs)
