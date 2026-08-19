"""Coverage sweep: the driver, the offline simulator, and the emit layer.

Companion to test_drive.py / test_sim_app_tools.py / test_toolkit_hardening.py —
this file targets what those leave uncovered:

  * `flows.drive`      — the interactive `chat()` REPL and the lazy resolution of
                         the in-package default driver (every branch stubbed).
  * `flows.sim.engine_sim` — unknown-session degradation, confirm/reject through
                         the real readback tools, an agent handoff, step-back as
                         snapshot-not-replay, visible_setters (both branches) and
                         the task/tool lookup fallbacks.
  * `flows.emit.scaffold` — the TEMPLATE path (starter_config + template tools +
                         the golden eval files), the atomic writer's edge cases
                         and the multi-agent crash envelope.
  * `flows.emit.golden_evals` — the whole module: every template's smoke set,
                         parsed back and asserted on.

NOTHING here touches a network or a deployed agent. Every driver test goes through
the `session_factory` seam with a local fake, imports are stubbed where the real
factory is exercised, and every emit lands in `tmp_path`.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_drive_emit_coverage.py
"""

from __future__ import annotations

import ast
import builtins
import importlib
import json
import os
import sys
import types
import uuid

import pytest

from flows import drive
from flows.emit import golden_evals, scaffold
from flows.emit.models import (ChildAgentSpec, HostAgentSpec,
                               MultiAgentScaffoldRequest, ScaffoldRequest)
from flows.engine import loader as fb
from flows.sim import engine_sim

# Obviously-fake identifiers everywhere: no real project, account or customer.
PROJECT = "proj-under-test"


# =============================================================================
# flows.drive — fakes (no network, no Studio; `session_factory` is the only seam)
# =============================================================================
class FakeTransport:
  """Stands in for ChatSession's `_sessions`; records every run() kwarg bundle."""

  def __init__(self):
    self.calls = []

  def run(self, **kw):
    self.calls.append(kw)
    return {}


class FakeTurn:
  def __init__(self, text="The order is on its way.", tool_calls=None):
    self.agent_text = text
    self.tool_calls = tool_calls


class FakeSession:
  """A local stand-in for a live CES session. Records what WOULD be sent."""

  def __init__(self, app_name="app", initial_variable_state=None, **kw):
    self.app_name = app_name
    self.seeded = initial_variable_state
    self.factory_kwargs = kw
    self.sent = []
    self.is_ended = False
    self.turns = None       # optional scripted list of FakeTurn
    self._sessions = FakeTransport()

  def send(self, text):
    self.sent.append(text)
    self._sessions.run(text=text)
    if self.turns:
      return self.turns.pop(0)
    return FakeTurn()


def _factory(captured, **preset):
  def make(app_name, initial_variable_state=None, **kw):
    session = FakeSession(app_name, initial_variable_state, **kw)
    for key, value in preset.items():
      setattr(session, key, value)
    captured.append(session)
    return session
  return make


def _inputs(*values):
  """A fake input() yielding `values`, then EOFError like a closed stdin."""
  it = iter(values)

  def fake_input(prompt=""):
    try:
      return next(it)
    except StopIteration:
      raise EOFError from None
  return fake_input


# --- open_session: the default-driver seam -----------------------------------
def test_open_session_without_a_factory_consults_the_default_resolver(monkeypatch):
  """No `session_factory`: the lazy resolver is asked once, and its result used."""
  made, asked = [], []

  def fake_resolver():
    asked.append(True)
    return _factory(made)

  monkeypatch.setattr(drive, "_default_session_factory", fake_resolver)
  drive.open_session({"order_id": "0001"}, "app-uuid", project=PROJECT, location="eu")

  assert asked == [True]
  assert made[0].app_name == f"projects/{PROJECT}/locations/eu/apps/app-uuid"
  assert made[0].seeded == {"order_id": "0001"}


# --- chat(): the REPL path ----------------------------------------------------
def test_chat_repl_drives_the_opening_then_exits_on_eof(monkeypatch, capsys):
  made = []
  factory = _factory(made, turns=[FakeTurn("Which order?",
                                           tool_calls=[{"action": "ask_order"}])])
  monkeypatch.setattr(builtins, "input", _inputs())

  assert drive.chat({"order_id": "0001"}, "app-uuid", opening="where is my order",
                    session_factory=factory) == 0

  out = capsys.readouterr().out
  assert "you   > where is my order" in out
  assert "agent > Which order?" in out
  assert "[tools: ask_order]" in out
  # WHAT WOULD BE SENT: exactly the opening, once, and nothing after EOF.
  assert made[0].sent == ["where is my order"]
  assert made[0]._sessions.calls == [{"use_tool_fakes": True,
                                      "text": "where is my order"}]


def test_chat_repl_sends_typed_lines_stripped_and_skips_blank_ones(monkeypatch,
                                                                   capsys):
  made = []
  monkeypatch.setattr(builtins, "input", _inputs("   ", "  hello  ", ""))
  assert drive.chat({"order_id": "0001"}, "app-uuid",
                    session_factory=_factory(made)) == 0
  assert made[0].sent == ["hello"]
  assert "agent > The order is on its way." in capsys.readouterr().out


@pytest.mark.parametrize("word", ["quit", "exit", "QUIT", "Exit"])
def test_chat_repl_quits_on_the_exit_words_without_sending(monkeypatch, word):
  made = []
  monkeypatch.setattr(builtins, "input", _inputs(word, "never sent"))
  assert drive.chat({"order_id": "0001"}, "app-uuid",
                    session_factory=_factory(made)) == 0
  assert made[0].sent == []


def test_chat_repl_stops_when_the_session_has_already_ended(monkeypatch, capsys):
  made = []
  monkeypatch.setattr(builtins, "input", _inputs("hello"))
  assert drive.chat({"order_id": "0001"}, "app-uuid",
                    session_factory=_factory(made, is_ended=True)) == 0
  assert "agent > (the session has ended)" in capsys.readouterr().out
  assert made[0].sent == []


def test_chat_repl_collapses_a_mirrored_agent_text(monkeypatch, capsys):
  factory = _factory([], turns=[FakeTurn("Your order ships tonight. "
                                         "Your order ships tonight.")])
  monkeypatch.setattr(builtins, "input", _inputs("go"))
  assert drive.chat({"order_id": "0001"}, "app-uuid", session_factory=factory) == 0
  out = capsys.readouterr().out
  assert "agent > Your order ships tonight.\n" in out
  assert out.count("Your order ships tonight.") == 1


def test_chat_repl_prints_the_tool_line_only_when_a_turn_fired_tools(monkeypatch,
                                                                     capsys):
  factory = _factory([], turns=[FakeTurn("One moment.", tool_calls=None),
                                FakeTurn("Found it.",
                                         tool_calls=[{"action": "lookup"},
                                                     {"action": "notify"}])])
  monkeypatch.setattr(builtins, "input", _inputs("hi", "and now"))
  assert drive.chat({"order_id": "0001"}, "app-uuid", session_factory=factory) == 0
  out = capsys.readouterr().out
  assert out.count("[tools:") == 1
  assert "[tools: lookup, notify]" in out


# --- the default driver resolver ---------------------------------------------
def test_default_factory_is_the_in_package_chat_session():
  """No host, no Labs checkout: the driver `flows` itself ships is what you get."""
  from flows.live.session import ChatSession

  assert drive._default_session_factory() is ChatSession


def test_resolving_the_default_factory_never_needs_scrapi(monkeypatch):
  """Authoring imports must not pay for the runtime stack — nor fail without it."""
  real = builtins.__import__

  def refuse(name, *a, **kw):
    if name == "cxas_scrapi" or name.startswith("cxas_scrapi."):
      raise ImportError("No module named 'cxas_scrapi'")
    return real(name, *a, **kw)

  for mod in ("flows.live.session", "flows.live.clients", "flows.live"):
    monkeypatch.delitem(sys.modules, mod, raising=False)
  monkeypatch.setattr(builtins, "__import__", refuse)

  assert drive._default_session_factory() is not None


def test_building_a_client_explains_the_missing_deploy_extra(monkeypatch):
  """cxas-scrapi absent is the common case; say which extra installs it."""
  from flows.live import clients

  def refuse(name):
    raise ImportError("No module named 'cxas_scrapi'")

  monkeypatch.setattr(importlib, "import_module", refuse)

  for make in (clients.make_sessions, clients.make_traces):
    with pytest.raises(ImportError, match=r'flows\[deploy\]') as excinfo:
      make(app_name="projects/p/locations/us/apps/a")
    assert isinstance(excinfo.value.__cause__, ImportError)


def test_open_session_prefers_an_explicit_factory_over_the_default(monkeypatch):
  """A host injects its own driver; the in-package default is never consulted."""
  made = []

  def explode():
    raise AssertionError("the default resolver must not be consulted")

  monkeypatch.setattr(drive, "_default_session_factory", explode)
  drive.open_session({"order_id": "0002"}, "app-uuid", project=PROJECT,
                     location="us", session_factory=_factory(made))

  assert made[0].app_name == f"projects/{PROJECT}/locations/us/apps/app-uuid"


# =============================================================================
# flows.sim.engine_sim
# =============================================================================
SET_ACTIVE_FLOW = '''
def set_active_flow(flow: str = ""):
    if flow != "booking":
        return {"error": True, "error_code": "invalid_flow"}
    return {"stored": True, "value": flow}
'''

SET_ACTIVE_FLOW_HANDOFF = '''
def set_active_flow(flow: str = ""):
    return {"stored": True, "value": flow, "target_agent": "Booking_Agent"}
'''

SET_SEAT = '''
def set_seat(seat: str = ""):
    value = str(seat).strip()
    if not value:
        return {"error": True, "error_code": "missing"}
    return {"stored": True, "value": value}
'''

CONFIRM_BOOKING = '''
def confirm_booking(seat: str = ""):
    return {"success": True, "booking_ref": "REF-" + str(seat)}
'''


def _booking_config() -> dict:
  """A gate + one read-back slot + a terminal task, all backed by real tools."""
  return {
      "_config_id": "sim_cov_booking",
      "gate_slot": "active_flow",
      "bootstrap": {"tool": "set_active_flow", "slot": "active_flow"},
      "slots": [
          {"name": "active_flow", "source": "user", "setter": "set_active_flow",
           "requires_readback": False},
          {"name": "seat", "source": "user", "setter": "set_seat",
           "ask": "Which seat would you like?", "requires_readback": True},
          {"name": "booking_ref", "source": "task", "task": "Book"},
      ],
      "readback_response": [{"type": "text", "text": "Did I get that right?"}],
      "tasks": [{"name": "Book", "tool": "confirm_booking", "inputs": ["seat"],
                 "result_key": "booking_ref", "success_check": "success"}],
  }


@pytest.fixture(autouse=True)
def _isolated_session_store():
  """`engine_sim` holds a process-global store; drop it either side of a test."""
  engine_sim.reset_store()
  yield
  engine_sim.reset_store()


@pytest.fixture
def booking_root(tmp_path):
  """A tools root carrying the app's OWN setters + executor (no framework copies)."""
  path = fb.materialize_tools_root(
      {"set_active_flow": SET_ACTIVE_FLOW, "set_seat": SET_SEAT,
       "confirm_booking": CONFIRM_BOOKING},
      parent=str(tmp_path))
  yield path
  fb.clear_cache()


def _gate(session_id: str) -> dict:
  return engine_sim.step({"session_id": session_id, "kind": "setter_call",
                          "tool": "set_active_flow", "args": {"flow": "booking"}})


def _seat(session_id: str, seat: str = "12A") -> dict:
  return engine_sim.step({"session_id": session_id, "kind": "setter_call",
                          "tool": "set_seat", "args": {"seat": seat}})


# --- an unknown session degrades gracefully on every entry point ---------------
def test_every_entry_point_degrades_for_an_unknown_session():
  """The frozen contract: a dead session id is a schema-valid empty step, not a
  crash. All five readers have to agree on that."""
  empty = engine_sim.step({"session_id": "no-such-session", "kind": "user_text",
                           "text": "hello"})
  assert empty == {"agent_text": "", "response_parts": [], "hide_tools": [],
                   "function_call": None, "next_action": "next_question",
                   "active_nodes": [], "slot_inspection": None, "sm": {},
                   "status": "in_progress", "step_index": 0,
                   "can_step_back": False}
  assert engine_sim.back("no-such-session") == empty
  assert engine_sim.reset("no-such-session") == empty
  assert engine_sim.visible_setters("no-such-session") == {"visible": [],
                                                           "hidden": []}
  assert engine_sim.session_sm("no-such-session") == {}


def test_an_unknown_sessions_context_state_is_a_fresh_writable_dict():
  """A tool may write to it harmlessly; two reads must not share one dict."""
  first = engine_sim.session_context_state("no-such-session")
  first["written"] = True
  assert engine_sim.session_context_state("no-such-session") == {}


def test_a_live_sessions_context_state_is_the_one_tools_write_through(booking_root):
  session_id, _ = engine_sim.start(_booking_config(), "sim_cov_booking",
                                   framework_root=booking_root)
  state = engine_sim.session_context_state(session_id)
  state["appointment"] = "held"
  assert engine_sim._SESSIONS[session_id].ces_state == {"appointment": "held"}
  assert engine_sim.session_context_state(session_id) is state


# --- confirm / reject run through the REAL readback tools ----------------------
def test_confirm_commits_the_pending_readback(booking_root):
  session_id, _ = engine_sim.start(_booking_config(), "sim_cov_booking",
                                   framework_root=booking_root)
  _gate(session_id)
  pending = _seat(session_id)
  assert pending["next_action"] == "readback"
  assert pending["sm"]["pending"] == {"seat": "12A"}

  committed = engine_sim.step({"session_id": session_id, "kind": "confirm"})
  assert committed["sm"]["filled"]["seat"] == "12A"
  assert committed["sm"]["pending"] == {}
  # A confirm is a CALLER turn: the engine's turn counter has to see it.
  assert engine_sim._SESSIONS[session_id].n_user_turns == 3


def test_reject_never_commits_the_read_back_value(booking_root):
  """`reject_pending` is the REAL framework tool, run through the context shim —
  its whole job is that a value the caller said no to is not filled."""
  session_id, _ = engine_sim.start(_booking_config(), "sim_cov_booking",
                                   framework_root=booking_root)
  _gate(session_id)
  _seat(session_id, "14C")

  rejected = engine_sim.step({"session_id": session_id, "kind": "reject"})
  assert "seat" not in rejected["sm"]["filled"]
  assert rejected["sm"]["_rejection_requested"] is True
  assert engine_sim.session_sm(session_id)["filled"].get("seat") is None
  # A reject is a caller turn (start + gate + seat + reject).
  assert engine_sim._SESSIONS[session_id].n_user_turns == 3


# --- an agent handoff is surfaced, not dropped ---------------------------------
def test_a_bootstrap_handoff_surfaces_as_a_pending_transfer(tmp_path):
  """`set_active_flow` returning a `target_agent` is a multi-agent handoff; the
  step result has to carry it or the turn looks like nothing happened."""
  root = fb.materialize_tools_root({"set_active_flow": SET_ACTIVE_FLOW_HANDOFF},
                                   parent=str(tmp_path))
  try:
    config = {
        "_config_id": "sim_cov_router",
        "gate_slot": "active_flow",
        "bootstrap": {"tool": "set_active_flow", "slot": "active_flow"},
        "slots": [{"name": "active_flow", "source": "user",
                   "setter": "set_active_flow", "requires_readback": False}],
        "tasks": [],
    }
    session_id, _ = engine_sim.start(config, "sim_cov_router", framework_root=root)
    handed = _gate(session_id)
    assert handed["pending_transfer"] == "Booking_Agent"
  finally:
    fb.clear_cache()


# --- step-back is a snapshot, not a replay -------------------------------------
def test_stepping_forward_three_and_back_two_restores_the_exact_earlier_state(
    booking_root):
  session_id, opening = engine_sim.start(_booking_config(), "sim_cov_booking",
                                         framework_root=booking_root)
  after_gate = _gate(session_id)                                   # step 1
  _seat(session_id)                                                # step 2
  engine_sim.step({"session_id": session_id, "kind": "confirm"})   # step 3
  session = engine_sim._SESSIONS[session_id]
  assert (session.step_index, len(session.history)) == (3, 3)

  engine_sim.back(session_id)
  restored = engine_sim.back(session_id)

  # Back to exactly the render step 1 produced — restored, not re-run, so the
  # whole result (sm included) is reproduced verbatim; only `can_step_back` is
  # refreshed for the new top-of-stack.
  assert restored == {**after_gate, "can_step_back": True}
  assert restored["step_index"] == 1
  assert (session.step_index, len(session.history)) == (1, 1)
  # ...and the turn counter rewound with it (an uncounted rewind inflates awaits).
  assert session.n_user_turns == 1
  assert opening["can_step_back"] is False


def test_stepping_back_at_the_start_turn_returns_the_current_state_unchanged(
    booking_root):
  session_id, opening = engine_sim.start(_booking_config(), "sim_cov_booking",
                                         framework_root=booking_root)
  session = engine_sim._SESSIONS[session_id]
  assert session.history == []

  assert engine_sim.back(session_id) == opening
  assert session.step_index == 0

  # And with no rendered result at all it still degrades to the empty shape.
  session.last_result = {}
  assert engine_sim.back(session_id)["status"] == "in_progress"


def test_reset_returns_to_the_opening_turn_and_clears_the_history(booking_root):
  session_id, opening = engine_sim.start(_booking_config(), "sim_cov_booking",
                                         framework_root=booking_root)
  _gate(session_id)
  _seat(session_id)
  engine_sim._SESSIONS[session_id].ces_state["scratch"] = "left over"

  reset = engine_sim.reset(session_id)
  session = engine_sim._SESSIONS[session_id]

  assert reset == {**opening, "can_step_back": False}
  assert (session.step_index, session.history, session.n_user_turns) == (0, [], 0)
  assert session.ces_state == {}


# --- visible_setters -----------------------------------------------------------
def test_visible_setters_reads_the_latest_render_without_advancing_state(
    booking_root):
  session_id, _ = engine_sim.start(_booking_config(), "sim_cov_booking",
                                   framework_root=booking_root)
  _gate(session_id)
  before = engine_sim._SESSIONS[session_id]
  snapshot = fb.deep_copy_sm(before.sm)

  split = engine_sim.visible_setters(session_id)

  assert sorted(split) == ["hidden", "visible"]
  named = {entry["tool"] for entry in split["visible"] + split["hidden"]}
  assert named == {"set_active_flow", "set_seat"}
  # A read must not advance the session.
  assert before.sm == snapshot
  assert before.step_index == 1


def test_visible_setters_falls_back_to_a_throwaway_engine_run(booking_root):
  """With nothing rendered yet, `hide_tools` is derived from a COPY of sm."""
  session_id, _ = engine_sim.start(_booking_config(), "sim_cov_booking",
                                   framework_root=booking_root)
  session = engine_sim._SESSIONS[session_id]
  session.last_result = {}
  snapshot = fb.deep_copy_sm(session.sm)

  split = engine_sim.visible_setters(session_id)

  assert {e["tool"] for e in split["visible"] + split["hidden"]} == {
      "set_active_flow", "set_seat"}
  assert session.sm == snapshot, "the probe run leaked into the live sm"


# --- pure derivation helpers ---------------------------------------------------
def test_active_nodes_maps_a_fired_setter_back_to_its_slot():
  action = {"function_call": {"name": "set_seat"}}
  sm = {"filled": {"active_flow": "booking"}, "pending": {},
        "_setter_slots": {"set_seat": "seat"}}
  assert engine_sim._active_nodes(action, sm) == ["active_flow", "seat"]


def test_active_nodes_skips_private_keys_and_never_repeats_a_node():
  action = {"function_call": {"name": "set_seat"}}
  sm = {"filled": {"seat": "12A", "_internal": 1}, "pending": {"seat": "12A"},
        "_setter_slots": {"set_seat": "seat"}}
  assert engine_sim._active_nodes(action, sm) == ["seat"]


def test_active_nodes_is_empty_when_nothing_has_fired_or_filled():
  assert engine_sim._active_nodes({}, {}) == []


def test_slot_inspection_reports_every_slot_status_and_its_value():
  config = {"slots": [{"name": "seat"}, {"name": "meal"}, {"name": "bags"},
                      {"name": "row"}]}
  sm = {"filled": {"seat": "12A"}, "pending": {"meal": "vegetarian"},
        "deferred": {"bags": 2}, "status": "in_progress",
        "task_results": {}, "_awaiting_async": {}}

  inspection = engine_sim._slot_inspection(config, sm)

  assert inspection["slots"] == [
      {"name": "seat", "status": "filled", "value": "12A"},
      {"name": "meal", "status": "pending", "value": "vegetarian"},
      {"name": "bags", "status": "deferred", "value": 2},
      {"name": "row", "status": "open", "value": None},
  ]
  assert inspection["status"] == "in_progress"
  assert inspection["awaiting_tasks"] == []


def test_slot_inspection_lists_the_outstanding_async_tasks_sorted():
  sm = {"filled": {}, "pending": {}, "deferred": {},
        "_awaiting_async": {"z_task": 1, "a_task": 1}}
  assert engine_sim._slot_inspection({"slots": []}, sm)["awaiting_tasks"] == [
      "a_task", "z_task"]


# --- task -> tool / success-check lookup fallbacks ------------------------------
def _bare_session(config: dict, sm: dict) -> engine_sim._Session:
  return engine_sim._Session(session_id="s", config_id="c", flow_id="f",
                             config=config, sm=sm)


def test_the_task_tool_is_read_off_the_slot_machine_first():
  session = _bare_session(
      {"tasks": [{"name": "Book", "tool": "stale_from_the_config"}]},
      {"_executor_tasks": {"confirm_booking": {"task_name": "Book",
                                               "success_check": "ok"}}})
  assert engine_sim._tool_for_task(session, "Book") == "confirm_booking"
  assert engine_sim._success_check(session, "Book") == "ok"


def test_the_task_tool_falls_back_to_the_config_when_sm_has_no_executors():
  session = _bare_session(
      {"tasks": [{"name": "Book", "tool": "confirm_booking",
                  "success_check": "booked"}]},
      {"_executor_tasks": {}})
  assert engine_sim._tool_for_task(session, "Book") == "confirm_booking"
  assert engine_sim._success_check(session, "Book") == "booked"


def test_an_unknown_task_falls_back_to_its_own_name_and_plain_success():
  session = _bare_session({"tasks": []}, {})
  assert engine_sim._tool_for_task(session, "NoSuchTask") == "NoSuchTask"
  assert engine_sim._success_check(session, "NoSuchTask") == "success"


def test_a_config_task_without_a_tool_key_is_not_matched():
  """A task declaring no tool must not resolve to a falsy tool name."""
  session = _bare_session({"tasks": [{"name": "Book"}]}, {})
  assert engine_sim._tool_for_task(session, "Book") == "Book"
  assert engine_sim._success_check(session, "Book") == "success"


# =============================================================================
# flows.emit.golden_evals
# =============================================================================
TEMPLATES_WITH_EVALS = ["reservation", "appointment", "lead_capture",
                        "support_triage"]
TEMPLATES_WITH_VALIDATION = ["reservation", "lead_capture"]


def _parsed_evals(template: str, config_id: str = "demo_flow"):
  """{displayName: parsed json} for a template's emitted eval files."""
  files = golden_evals.golden_eval_files(template, config_id)
  return {os.path.basename(f["path"]): json.loads(f["content"]) for f in files}


@pytest.mark.parametrize("template", [None, "", "no_such_template"])
def test_a_template_without_a_golden_spec_emits_nothing(template):
  assert golden_evals.golden_eval_files(template, "demo_flow") == []


@pytest.mark.parametrize("template", TEMPLATES_WITH_EVALS)
def test_every_emitted_eval_file_is_valid_json_at_the_ces_on_disk_path(template):
  files = golden_evals.golden_eval_files(template, "demo_flow")
  for entry in files:
    directory, filename = os.path.split(entry["path"])
    assert filename == os.path.basename(directory) + ".json", entry["path"]
    assert entry["content"].endswith("\n")
    json.loads(entry["content"])          # raises if the emitted file is not JSON
  assert len(files) == (4 if template in TEMPLATES_WITH_VALIDATION else 3)


@pytest.mark.parametrize("template", TEMPLATES_WITH_EVALS)
def test_the_suite_lists_exactly_the_evals_that_were_emitted(template):
  parsed = _parsed_evals(template)
  suite = next(v for k, v in parsed.items() if k.startswith("demo_flow"))
  evals = [v for k, v in parsed.items() if not k.startswith("demo_flow")]

  assert suite["displayName"] == "demo_flow — Smoke Suite"
  assert sorted(suite["evaluations"]) == sorted(e["displayName"] for e in evals)
  for ev in evals:
    assert ev["evaluationDatasets"] == ["demo_flow — Smoke Suite"]
    assert uuid.UUID(ev["name"])          # a real UUID, not a slug


@pytest.mark.parametrize("template", TEMPLATES_WITH_EVALS)
def test_the_happy_path_gates_then_collects_every_slot_then_fires_the_task(template):
  spec = golden_evals._GOLDEN_SPECS[template]
  happy = _parsed_evals(template)["Smoke_-_Happy_Path.json"]
  turns = happy["golden"]["turns"]

  # Turn 0 is always the routing gate.
  assert turns[0]["steps"][0]["userInput"]["text"] == spec["intent"]
  assert turns[0]["steps"][1]["expectation"]["toolCall"]["tool"] == "set_active_flow"
  # One turn per slot, in the declared order, each expecting its own setter.
  collected = [(t["steps"][0]["userInput"]["text"],
                t["steps"][1]["expectation"]["toolCall"]["tool"])
               for t in turns[1:-1]]
  assert collected == [(utterance, setter)
                       for setter, utterance in spec["slots"]]
  # ...and the terminal executor fires last.
  assert turns[-1]["steps"][1]["expectation"]["toolCall"]["tool"] == spec["task"]
  assert happy["tags"] == ["smoke", "happy-path", "slot-filling"]


@pytest.mark.parametrize("template", TEMPLATES_WITH_EVALS)
def test_the_reject_eval_expects_the_rejected_value_to_stay_unfilled(template):
  setter, utterance = golden_evals._GOLDEN_SPECS[template]["slots"][0]
  reject = _parsed_evals(template)["Smoke_-_Readback_Reject_Recollects.json"]
  turns = reject["golden"]["turns"]

  assert len(turns) == 3
  assert turns[1]["steps"][0]["userInput"]["text"] == utterance
  assert turns[1]["steps"][1]["expectation"]["toolCall"]["tool"] == setter
  assert turns[2]["steps"][1]["expectation"]["updatedVariables"] == {
      "sm": {"filled": {}}}


@pytest.mark.parametrize("template", TEMPLATES_WITH_VALIDATION)
def test_the_validation_eval_rejects_then_recaptures_with_the_real_args(template):
  spec = golden_evals._GOLDEN_SPECS[template]["validation"]
  validation = _parsed_evals(template)["Smoke_-_Invalid_Input_Recovers.json"]
  bad_turn, good_turn = validation["golden"]["turns"][1:]

  assert bad_turn["steps"][0]["userInput"]["text"] == spec["bad"]
  assert bad_turn["steps"][1]["expectation"]["toolCall"]["tool"] == spec["setter"]
  assert bad_turn["steps"][2]["expectation"]["updatedVariables"] == {
      "sm": {"filled": {}}}
  assert good_turn["steps"][1]["expectation"]["toolCall"] == {
      "tool": spec["setter"], "args": spec["good_args"]}


@pytest.mark.parametrize("template", ["appointment", "support_triage"])
def test_a_template_without_a_validation_spec_ships_no_validation_eval(template):
  assert "Smoke_-_Invalid_Input_Recovers.json" not in _parsed_evals(template)


@pytest.mark.parametrize("template", TEMPLATES_WITH_EVALS)
def test_regenerating_a_template_produces_byte_identical_evals(template):
  """Stable uuid5 ids, so a regenerated template is not a diff of uuid4 churn."""
  assert (golden_evals.golden_eval_files(template, "demo_flow")
          == golden_evals.golden_eval_files(template, "demo_flow"))


def test_two_config_ids_get_different_eval_ids():
  first = _parsed_evals("reservation", "flow_one")["Smoke_-_Happy_Path.json"]
  second = _parsed_evals("reservation", "flow_two")["Smoke_-_Happy_Path.json"]
  assert first["name"] != second["name"]
  assert first["golden"] == second["golden"]      # only the identity differs


def test_the_stable_id_is_a_uuid5_over_the_joined_parts():
  assert golden_evals._stable_id("a", "b") == str(
      uuid.uuid5(golden_evals._NS, "a/b"))
  assert golden_evals._stable_id("a", "b") != golden_evals._stable_id("a", "c")
  assert golden_evals._suite_name("demo_flow") == "demo_flow — Smoke Suite"


def test_the_suite_filename_strips_the_em_dash_and_the_spaces():
  files = golden_evals.golden_eval_files("reservation", "demo_flow")
  suite_path = files[-1]["path"]
  assert suite_path == ("evaluationDatasets/demo_flow_-_Smoke_Suite/"
                        "demo_flow_-_Smoke_Suite.json")
  assert "—" not in suite_path and " " not in suite_path


# =============================================================================
# flows.emit.scaffold — templates, the writer, and the multi-agent envelope
# =============================================================================
ALL_TEMPLATES = sorted(scaffold.TEMPLATES)


@pytest.mark.parametrize("template", [None, "", "no_such_template"])
def test_an_absent_template_has_no_tools_setters_or_starter_message(template):
  assert scaffold.template_tools(template) == {}
  assert scaffold.template_setters(template) == {}
  assert scaffold.template_suggested_message(template) == ""


@pytest.mark.parametrize("template", ALL_TEMPLATES)
def test_a_templates_tools_are_its_setters_plus_its_executors(template):
  entry = scaffold.TEMPLATES[template]
  tools = scaffold.template_tools(template)
  assert set(tools) == set(entry["setters"]) | set(entry["executors"])
  assert scaffold.template_setters(template) == entry["setters"]
  # Every emitted body is real, parseable Python defining the tool it is named for.
  for name, code in tools.items():
    tree = ast.parse(code)
    assert any(isinstance(n, ast.FunctionDef) and n.name == name
               for n in tree.body), name


def test_the_setter_map_is_a_copy_the_caller_cannot_mutate_the_template_through():
  setters = scaffold.template_setters("reservation")
  setters.pop("set_party_size")
  assert "set_party_size" in scaffold.TEMPLATES["reservation"]["setters"]


@pytest.mark.parametrize("template", ALL_TEMPLATES)
def test_every_template_ships_a_simulator_starter_message(template):
  assert scaffold.template_suggested_message(template).strip()


def test_the_blank_starter_config_is_the_gate_floor_and_nothing_else():
  cfg = scaffold.starter_config("demo_flow")
  assert cfg["gate_slot"] == "active_flow"
  assert cfg["bootstrap"] == {"tool": "set_active_flow", "slot": "active_flow",
                              "reset_on_complete": True}
  assert [s["name"] for s in cfg["slots"]] == ["active_flow"]
  assert cfg["tasks"] == []
  assert "readback_response" not in cfg


def test_an_unknown_template_falls_back_to_the_blank_floor():
  assert scaffold.starter_config("demo_flow", template="no_such_template") == (
      scaffold.starter_config("demo_flow"))


@pytest.mark.parametrize("template", ALL_TEMPLATES)
def test_a_template_config_stacks_its_slots_and_task_on_the_gate_floor(template):
  entry = scaffold.TEMPLATES[template]
  cfg = scaffold.starter_config("demo_flow", template=template)

  names = [s["name"] for s in cfg["slots"]]
  assert names[0] == "active_flow", "the gate slot must stay first"
  assert names[1:] == ([s["name"] for s in entry["slots"]]
                       + [s["name"] for s in entry["result_slots"]])
  assert [t["name"] for t in cfg["tasks"]] == [t["name"]
                                               for t in entry["tasks"]]
  # A template with a read-back slot has to carry the read-back copy.
  assert any(s.get("requires_readback") for s in entry["slots"])
  assert cfg["readback_response"][0]["text"].endswith("does this look right?")


def test_the_template_config_is_a_deep_enough_copy_to_edit_safely():
  first = scaffold.starter_config("demo_flow", template="reservation")
  first["slots"][1]["ask"] = "mutated"
  first["tasks"][0]["name"] = "mutated"
  second = scaffold.starter_config("demo_flow", template="reservation")
  assert second["slots"][1]["ask"] != "mutated"
  assert second["tasks"][0]["name"] != "mutated"


def test_the_rendered_dag_function_parses_and_returns_the_config():
  """The rendered source is EXECUTED by the engine, so it must both parse and return.

  This used to also assert the source contained no `true`/`null`, back when the config
  was emitted as a Python literal and JSON keywords would have been a NameError. The
  config is now a JSON STRING parsed by `json.loads` at call time — for load speed, since
  CES recompiles the module on every invocation — so JSON spelling inside that string is
  correct and the old assertion tested the serialization rather than the behavior. What
  matters is unchanged and is asserted below: the function returns the config it was
  built from.
  """
  source = scaffold._starter_dag_code("demo_flow")
  ast.parse(source)
  assert "def demo_flow_dag()" in source

  namespace: dict = {}
  exec(compile(source, "<demo_flow_dag>", "exec"), namespace)   # noqa: S102
  assert namespace["demo_flow_dag"]() == scaffold.starter_config("demo_flow")


def test_the_rendered_dag_function_can_be_handed_an_explicit_config():
  cfg = scaffold.starter_config("demo_flow", template="reservation")
  source = scaffold._starter_dag_code("demo_flow", cfg)
  namespace: dict = {}
  exec(compile(source, "<demo_flow_dag>", "exec"), namespace)   # noqa: S102
  assert namespace["demo_flow_dag"]() == cfg


# --- build(): the template path -------------------------------------------------
def _request(**overrides) -> ScaffoldRequest:
  base = {"app_display_name": "Coverage Demo", "config_id": "demo_flow",
          "root_agent": "Demo_Agent", "gcp_project": PROJECT, "mode": "hosted"}
  return ScaffoldRequest(**{**base, **overrides})


def _by_path(result) -> dict:
  return {f.path: f.content for f in result.files}


@pytest.mark.parametrize("template", ALL_TEMPLATES)
def test_building_from_a_template_emits_its_tools_dag_and_golden_evals(template):
  result = scaffold.build(_request(template=template))
  assert result.ok, result.validation.errors
  files = _by_path(result)

  for tool in scaffold.template_tools(template):
    body = f"tools/{tool}/python_function/python_code.py"
    assert body in files, tool
    ast.parse(files[body])
    assert json.loads(files[f"tools/{tool}/{tool}.json"])["displayName"] == tool

  # The golden smoke set rides along with the template.
  for entry in golden_evals.golden_eval_files(template, "demo_flow"):
    assert files[entry["path"]] == entry["content"]

  # The DAG tool renders the template's config verbatim.
  namespace: dict = {}
  exec(compile(files["tools/demo_flow_dag/python_function/python_code.py"],
               "<dag>", "exec"), namespace)                      # noqa: S102
  assert namespace["demo_flow_dag"]() == scaffold.starter_config(
      "demo_flow", template=template)


def test_a_blank_build_ships_no_golden_evals_and_only_the_gate_setter():
  result = scaffold.build(_request())
  assert result.ok, result.validation.errors
  files = _by_path(result)
  assert not [p for p in files if p.startswith("evaluations/")]
  assert "tools/set_active_flow/python_function/python_code.py" in files
  assert not [p for p in files if p.startswith("tools/set_party_size/")]


def test_every_emitted_json_parses_and_every_emitted_py_compiles():
  result = scaffold.build(_request(template="reservation"))
  for path, content in _by_path(result).items():
    if path.endswith(".json"):
      json.loads(content)
    elif path.endswith(".py"):
      ast.parse(content)


# --- _tool_names_from_files -----------------------------------------------------
def test_a_malformed_or_nameless_tool_json_is_skipped_not_fatal():
  """A framework bundle read is not the place to raise; skip what cannot be read."""
  names = scaffold._tool_names_from_files([
      {"path": "tools/broken/broken.json", "content": "{ not json"},
      {"path": "tools/nameless/nameless.json", "content": json.dumps({"a": 1})},
      {"path": "tools/good/good.json",
       "content": json.dumps({"displayName": "good"})},
      {"path": "tools/good/python_function/python_code.py", "content": "x = 1"},
  ])
  assert names == ["good"]


# --- _write_tree ----------------------------------------------------------------
def _files(*pairs):
  return [types.SimpleNamespace(path=p, content=c) for p, c in pairs]


def test_the_writer_lays_the_tree_down_under_an_existing_empty_directory(tmp_path):
  target = tmp_path / "app"
  target.mkdir()                       # empty: the writer replaces it wholesale
  written = scaffold._write_tree(
      str(target), _files(("app.json", '{"displayName": "Demo"}'),
                          ("tools/t/python_function/python_code.py", "x = 1")),
      [("agents/Demo_Agent/before_model.py", b"# callback\n")])

  assert written == str(target)
  assert json.loads((target / "app.json").read_text())["displayName"] == "Demo"
  assert (target / "tools/t/python_function/python_code.py").read_text() == "x = 1"
  assert (target / "agents/Demo_Agent/before_model.py").read_bytes() == b"# callback\n"
  assert not [p for p in os.listdir(tmp_path) if p.startswith(".scaffold_")]


def test_the_writer_refuses_to_overwrite_a_non_empty_directory(tmp_path):
  target = tmp_path / "app"
  target.mkdir()
  (target / "keep_me.txt").write_text("prior work")

  with pytest.raises(FileExistsError, match="Refusing to overwrite"):
    scaffold._write_tree(str(target), _files(("app.json", "{}")), [])
  assert (target / "keep_me.txt").read_text() == "prior work"


def test_a_failed_write_leaves_no_temp_dir_and_no_target(tmp_path):
  """On any failure the staging dir goes; a half-written app is worse than none."""
  target = tmp_path / "app"
  exploding = _files(("app.json", "{}"))
  exploding.append(types.SimpleNamespace(path="boom.py", content=object()))

  with pytest.raises(AttributeError):
    scaffold._write_tree(str(target), exploding, [])

  assert not target.exists()
  assert [p for p in os.listdir(tmp_path) if p.startswith(".scaffold_")] == []


def test_the_writer_expands_a_user_relative_target(tmp_path, monkeypatch):
  monkeypatch.setenv("HOME", str(tmp_path))
  written = scaffold._write_tree("~/emitted_app", _files(("app.json", "{}")), [])
  assert written == str(tmp_path / "emitted_app")
  assert (tmp_path / "emitted_app" / "app.json").exists()


def test_build_in_local_mode_writes_the_tree_it_reports(tmp_path):
  target = tmp_path / "written_app"
  result = scaffold.build(_request(mode="local", target_path=str(target),
                                   template="appointment"))
  assert result.ok, result.validation.errors
  assert result.written_to == str(target)
  for path in _by_path(result):
    assert (target / path).exists(), path


# --- _collect_uuids -------------------------------------------------------------
def test_uuid_collection_skips_non_json_unparseable_and_non_uuid_names():
  stable = str(uuid.uuid4())
  collected = scaffold._collect_uuids(_files(
      ("tools/a/a.json", "{ not json"),
      ("tools/b/b.json", json.dumps({"name": "plain-string-name"})),
      ("tools/c/c.json", json.dumps({"name": stable})),
      ("tools/d/python_function/python_code.py", "x = 1"),
  ))
  assert collected == [stable]
  assert scaffold._looks_like_uuid(stable)
  assert not scaffold._looks_like_uuid("plain-string-name")
  assert not scaffold._looks_like_uuid(None)


# --- build_multi_agent ----------------------------------------------------------
def _multi_request(configs=None, **overrides) -> MultiAgentScaffoldRequest:
  configs = configs or {"child_flow": scaffold.starter_config("child_flow")}
  base = {
      "app_display_name": "Coverage Multi",
      "gcp_project": PROJECT,
      "mode": "hosted",
      "host": HostAgentSpec(name="Host_Agent", instruction="Route the caller.",
                            child_agents=["Child_Agent"],
                            tools=["set_active_flow", "end_session"]),
      "agents": [ChildAgentSpec(name="Child_Agent", instruction="Collect.",
                                tools=["child_flow_dag", "set_active_flow"])],
      "all_configs": configs,
      "tools_override": {
          "set_active_flow": scaffold._active_flow_setter(["child_flow"])},
      "agent_config_map": {"Child_Agent": "child_flow"},
  }
  return MultiAgentScaffoldRequest(**{**base, **overrides})


def test_a_multi_agent_build_is_ok_and_emits_one_dag_per_config():
  result = scaffold.build_multi_agent(_multi_request())
  assert result.ok, result.validation.errors
  assert result.validation.errors == []
  files = _by_path(result)
  assert "tools/child_flow_dag/python_function/python_code.py" in files
  # A sub-agent runs the canonical 4 callbacks; a `transfer` host carries only
  # the two it overrides.
  for name in ("before_agent", "before_model", "after_model", "after_tool"):
    assert (f"agents/Child_Agent/{name}_callbacks/{name}_callbacks_01/"
            "python_code.py") in files, name
  host = [p for p in files if p.startswith("agents/Host_Agent/")
          and p.endswith("python_code.py")]
  assert sorted(host) == [
      "agents/Host_Agent/after_tool_callbacks/after_tool_callbacks_01/python_code.py",
      "agents/Host_Agent/before_model_callbacks/before_model_callbacks_01/"
      "python_code.py",
  ]


def test_an_unresolvable_setter_makes_the_multi_agent_build_invalid():
  """Validation runs per config, and every error is prefixed with its config id."""
  broken = {
      "gate_slot": "active_flow",
      "bootstrap": {"tool": "set_active_flow", "slot": "active_flow"},
      "slots": [
          {"name": "active_flow", "source": "user", "setter": "set_active_flow"},
          {"name": "seat", "source": "user", "setter": "set_seat_that_is_missing"},
      ],
      "tasks": [],
  }
  result = scaffold.build_multi_agent(_multi_request({"child_flow": broken}))

  assert result.ok is False
  assert result.validation.valid is False
  assert any(e.startswith("[child_flow] ") and "set_seat_that_is_missing" in e
             for e in result.validation.errors), result.validation.errors
  assert result.error is None, "a validation failure is reported, not an exception"


def test_the_multi_agent_builder_never_raises_out(monkeypatch):
  """Crash envelope: a bundle read that blows up becomes a reported failure."""
  monkeypatch.setattr(scaffold.blessed_source, "callbacks",
                      lambda: (_ for _ in ()).throw(RuntimeError("bundle is gone")))

  result = scaffold.build_multi_agent(_multi_request())

  assert result.ok is False
  assert result.files == []
  assert result.written_to is None
  assert result.error == "Scaffold failed: bundle is gone"
  assert result.validation.errors == ["bundle is gone"]
  assert (result.callback_sync_ok, result.uuids_unique) == (False, False)


def test_a_failed_multi_agent_build_writes_nothing_to_disk(tmp_path, monkeypatch):
  target = tmp_path / "never_written"
  monkeypatch.setattr(scaffold.blessed_source, "callbacks",
                      lambda: (_ for _ in ()).throw(RuntimeError("bundle is gone")))
  result = scaffold.build_multi_agent(
      _multi_request(mode="local", target_path=str(target)))
  assert result.ok is False
  assert not target.exists()


def test_the_engine_host_declares_its_default_config_and_intent_routing():
  request = _multi_request(
      host=HostAgentSpec(name="Host_Agent", instruction="Route.",
                         strategy="engine", child_agents=["Child_Agent"],
                         tools=["host_flow_dag", "set_active_flow"]),
      configs={"child_flow": scaffold.starter_config("child_flow"),
               "host_flow": scaffold.starter_config("host_flow")},
      agent_config_map={"Child_Agent": "child_flow", "Host_Agent": "host_flow"},
      default_config_id="host_flow",
      intent_config_map={"ENTRY_INTENT_BILLING": "child_flow"})

  declared = {v["name"]: v for v in
              json.loads(_by_path(scaffold.build_multi_agent(request))["app.json"])[
                  "variableDeclarations"]}

  assert declared["default_config_id"]["schema"]["default"] == "host_flow"
  assert json.loads(declared["intent_config_map"]["schema"]["default"]) == {
      "ENTRY_INTENT_BILLING": "child_flow"}
  assert json.loads(declared["agent_config_map"]["schema"]["default"]) == {
      "Child_Agent": "child_flow", "Host_Agent": "host_flow"}


def test_a_transfer_host_omits_the_default_config_and_the_intent_map():
  declared = {v["name"] for v in
              json.loads(_by_path(scaffold.build_multi_agent(_multi_request()))[
                  "app.json"])["variableDeclarations"]}
  assert "default_config_id" not in declared
  assert "intent_config_map" not in declared
  assert "agent_config_map" in declared
