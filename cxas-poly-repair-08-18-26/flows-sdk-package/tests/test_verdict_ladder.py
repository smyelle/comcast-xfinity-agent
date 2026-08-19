"""Verdict ladders + the announce channels they need (dsl.py).

A verdict is a SILENT diagnostic spine followed by an ORDERED priority ladder in
which exactly ONE branch speaks. Covers the derived halt-at-first-match gating,
the run-once spine, the announce `message`/`preempt`/`transfer_to` channels, and
`content_announce` — plus an offline engine drive proving that two simultaneously
matching branches still produce ONE verdict.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_verdict_ladder.py
"""

from __future__ import annotations

import pytest

import flows
from flows import VerdictBranch, announce, content_announce, task, verdict
from flows.sim import engine_sim


# --- announce delivery channels ---------------------------------------------
def test_announce_always_emits_preempt():
  """The engine defaults a missing `preempt` to True, so it must never be omitted.

  Shape only — see the behavioural drives at the bottom of this file for the tests
  that can tell a WRONG value apart from a missing key.
  """
  assert "preempt" in announce("a", ["hi"])
  assert announce("a", ["hi"])["preempt"] is False
  assert announce("a", ["hi"], preempt=True)["preempt"] is True


def test_announce_message_is_model_rendered_and_has_no_response():
  a = announce("offer", [], message="Mention the protection plan.")
  assert a["message"] == "Mention the protection plan."
  assert "response" not in a


def test_announce_carries_both_channels():
  a = announce("both", ["Verbatim."], message="Model-rendered.")
  assert a["message"] == "Model-rendered."
  assert a["response"] == [{"type": "text", "text": "Verbatim."}]


def test_announce_transfer_part_precedes_end_session():
  a = announce("bye", ["Connecting you."], transfer_to="Human_Agent", end=True)
  assert [p["type"] for p in a["response"]] == ["text", "transfer", "end_session"]
  assert a["response"][1]["agent"] == "Human_Agent"


def test_announce_with_neither_channel_raises():
  with pytest.raises(ValueError, match="says nothing"):
    announce("silent", [])


# --- content_announce --------------------------------------------------------
def test_content_announce_is_non_preempting_and_gated_on_the_result():
  a = content_announce("plan_offer", "Pitch the protection plan.",
                       after="confirmation_number")
  assert a == {
      "name": "plan_offer",
      "source": "announce",
      "message": "Pitch the protection plan.",
      "requires": ["confirmation_number"],
      "preempt": False,
  }


# --- verdict: the silent spine ----------------------------------------------
def _spine() -> list[dict]:
  return [
      task("line_check", "check_line", [], "line_status"),
      task("acct_check", "check_account", [], "account_status"),
  ]


def _branches() -> list[VerdictBranch]:
  return [
      VerdictBranch(condition=flows.eq("line_status", "outage"),
                    say="There's an outage in your area.", reads=["line_status"]),
      VerdictBranch(condition=flows.eq("account_status", "suspended"),
                    say="Your account is suspended.", reads=["account_status"]),
      VerdictBranch(condition=flows.has("line_status"), say="Everything looks healthy."),
  ]


def _verdict_config() -> dict:
  f = verdict("diagnose", spine=_spine(), branches=_branches(),
              root_agent="Diag_Agent")
  f.set("single_flow", True)
  f.set("gate_slot", "active_flow")
  f.set("bootstrap", {"slot": "active_flow", "reset_on_complete": True})
  return f.to_config()


def test_spine_task_gets_a_run_once_flag():
  """The flag rides the success_check key — see the run-flag test below for why."""
  cfg = verdict("d", spine=_spine(), branches=_branches()).to_config()
  line = next(t for t in cfg["tasks"] if t["name"] == "line_check")
  assert line["outputs"] == {"line_status": "line_status",
                             "success": "line_check_ran"}
  assert line["condition"] == flows.unset("line_check_ran")
  assert line["requires"] == []


def test_run_flag_rides_a_key_the_tool_actually_returns():
  """Intake applies a task's outputs only when EVERY declared key is in the tool
  response, so a synthetic `<task>_ran` key would discard the diagnostic's REAL
  outputs too and wedge the whole ladder on run-flags that never arrive."""
  cfg = verdict("d", spine=_spine(), branches=_branches()).to_config()
  for t in cfg["tasks"]:
    assert f"{t['name']}_ran" not in t["outputs"]  # never a made-up result key
    assert t["outputs"][t["success_check"]] == f"{t['name']}_ran"


def test_spine_task_that_already_maps_its_success_key_is_refused():
  with pytest.raises(ValueError, match="run-once flag"):
    verdict("d", spine=[task("t", "tool", [], "ok", out_key="success")],
            branches=_branches())


def test_run_once_gate_matches_a_dict_authored_spine_condition():
  cfg = verdict("d", spine=[
      task("line_check", "check_line", [], "line_status",
           condition={"slot": "segment", "eq": "vip"}),
  ], branches=_branches()).to_config()
  assert cfg["tasks"][0]["condition"] == {
      "all": [{"slot": "segment", "eq": "vip"},
              {"slot": "line_check_ran", "filled": False}]}


def test_spine_task_with_requires_is_refused():
  """Cleared silently before: the spine fires on the entry turn with nothing filled,
  so the prerequisite is unmeetable and every rung waits on the run-flag behind it."""
  with pytest.raises(ValueError, match="no-prerequisite fan-out"):
    verdict("d", spine=[task("t", "tool", [], "r", requires=["account_id"])],
            branches=_branches())


def test_a_conditional_spine_tasks_outputs_carry_its_gate():
  """The engine waives a `requires` whose slot is INACTIVE, so the derived gate on the
  output slots is what stops a skipped diagnostic from wedging the ladder."""
  cond = {"slot": "segment", "eq": "vip"}
  cfg = verdict("d", spine=[
      task("line_check", "check_line", [], "line_status"),
      task("vip_check", "check_vip", [], "vip_status", condition=cond),
  ], branches=_branches()).to_config()
  by_name = {s["name"]: s for s in cfg["slots"]}
  assert by_name["vip_status"]["condition"] == cond
  assert by_name["vip_check_ran"]["condition"] == cond
  # NOT the run-once gate: "flag not yet filled" would waive the wait exactly when the
  # flag lands, and demand it for as long as it is missing — backwards on both counts.
  assert "condition" not in by_name["line_status"]
  assert "condition" not in by_name["line_check_ran"]


def test_spine_outputs_are_declared_as_result_slots():
  cfg = verdict("d", spine=_spine(), branches=_branches()).to_config()
  by_name = {s["name"]: s for s in cfg["slots"]}
  assert by_name["line_status"]["source"] == "task:line_check"
  assert by_name["line_check_ran"]["source"] == "task:line_check"
  assert by_name["account_status"]["source"] == "task:acct_check"


def test_spine_task_with_inputs_is_refused():
  with pytest.raises(ValueError, match="no user turn"):
    verdict("d", spine=[task("t", "tool", ["account_id"], "r")],
            branches=_branches())


def test_terminal_spine_task_is_refused():
  with pytest.raises(ValueError, match="terminal"):
    verdict("d", spine=[task("t", "tool", [], "r", terminal=True)],
            branches=_branches())


def test_empty_spine_or_ladder_is_refused():
  with pytest.raises(ValueError, match="spine"):
    verdict("d", spine=[], branches=_branches())
  with pytest.raises(ValueError, match="branches"):
    verdict("d", spine=_spine(), branches=[])


# --- verdict: the ladder -----------------------------------------------------
def _ladder_of(cfg: dict, config_id: str) -> list[dict]:
  return [s for s in cfg["slots"] if s["name"].startswith(f"{config_id}_branch_")]


def _ladder(cfg: dict) -> list[dict]:
  return _ladder_of(cfg, "d")


def test_ladder_is_ordered_and_verbatim_rungs_preempt():
  """Verbatim rungs must ride inline: otherwise the verdict is queued for the NEXT
  turn while the model narrates the spine's raw outputs — every matching rule at
  once — which is the contradiction the halt gating exists to prevent."""
  cfg = verdict("d", spine=_spine(), branches=_branches()).to_config()
  rungs = _ladder(cfg)
  assert [s["name"] for s in rungs] == ["d_branch_0", "d_branch_1", "d_branch_2"]
  assert all(s["preempt"] is True for s in rungs)


def test_each_branch_waits_for_the_whole_spine():
  cfg = verdict("d", spine=_spine(), branches=_branches()).to_config()
  for rung in _ladder(cfg):
    assert "line_check_ran" in rung["requires"]
    assert "acct_check_ran" in rung["requires"]


def test_branch_is_gated_on_every_higher_rung_being_unfired():
  cfg = verdict("d", spine=_spine(), branches=_branches()).to_config()
  first, second, third = _ladder(cfg)
  assert "d_branch_" not in first["condition"]
  assert "d_branch_0" in second["condition"]
  assert "d_branch_0" in third["condition"] and "d_branch_1" in third["condition"]


def test_halt_gating_matches_the_branch_condition_form():
  """A dict-authored branch gets dict unfired leaves — the two forms do not nest."""
  cfg = verdict(
      "d", spine=_spine(),
      branches=[
          VerdictBranch(condition={"slot": "line_status", "eq": "outage"},
                        say="Outage.", reads=["line_status"]),
          VerdictBranch(condition={"slot": "line_status", "filled": True},
                        say="Checked."),
      ]).to_config()
  second = _ladder(cfg)[1]
  assert second["condition"]["all"][-1] == {"slot": "d_branch_0", "filled": False}


def test_generative_branch_speaks_through_message():
  cfg = verdict("d", spine=_spine(), branches=[
      VerdictBranch(condition=flows.has("line_status"),
                    say="Summarize what the checks found.", generative=True),
  ]).to_config()
  rung = _ladder(cfg)[0]
  assert rung["message"] == "Summarize what the checks found."
  assert "response" not in rung
  # A generative rung must NOT preempt — preempting skips the model, and the model
  # is the thing that turns this directive into a sentence.
  assert rung["preempt"] is False


def test_transfer_branch_carries_a_transfer_part():
  cfg = verdict("d", spine=_spine(), branches=[
      VerdictBranch(condition=flows.has("line_status"), say="Handing you over.",
                    transfer_to="Field_Ops_Agent"),
  ]).to_config()
  parts = _ladder(cfg)[0]["response"]
  assert {"type": "transfer", "agent": "Field_Ops_Agent"} in parts


def test_unproduced_gate_is_declared_but_not_required():
  """A var the spine never writes still has to be DECLARED (the engine looks its
  slot def up when checking `requires`) — but requiring it would wedge the rung."""
  cfg = verdict("d", spine=_spine(), branches=[
      VerdictBranch(condition=flows.eq("segment", "vip"), say="VIP path.",
                    reads=["segment"]),
  ]).to_config()
  seg = next(s for s in cfg["slots"] if s["name"] == "segment")
  assert seg == {"name": "segment", "source": "event", "event_key": "segment"}
  assert "segment" not in _ladder(cfg)[0]["requires"]


# --- the emitted app is legal ------------------------------------------------
def test_verdict_app_validates():
  f = verdict("diagnose", spine=_spine(), branches=_branches(),
              root_agent="Diag_Agent")
  app = flows.App(root_flow=f, app_display_name="Diagnostics")
  errors, _warnings = flows.validate_app(app)
  assert errors == []


# --- behaviour: exactly ONE branch speaks ------------------------------------
def _drive_spine(config: dict) -> dict:
  """Run both diagnostics, each returning a status that matches its own branch."""
  engine_sim.reset_store()
  session_id, _ = engine_sim.start(config, "diagnose")
  for name, payload in (
      ("line_check", {"line_status": "outage", "line_check_ran": True}),
      ("acct_check", {"account_status": "suspended", "acct_check_ran": True}),
  ):
    result = engine_sim.step({
        "session_id": session_id, "kind": "task_result",
        "task_name": name, "result": payload, "success": True,
    })
  return result


def _spoken(result) -> list[str]:
  """Every text part the caller gets: a preempting announce rides inline on the
  directive, a non-preempting one waits in the pending queue for the next turn."""
  return [p["text"]
          for p in (result["response_parts"]
                    + result["sm"].get("_pending_announce_payloads", []))
          if p.get("type") == "text"]


def test_only_the_highest_priority_matching_branch_speaks():
  """Both rung 0 and rung 1 match; the caller must hear rung 0 and nothing else."""
  result = _drive_spine(_verdict_config())
  assert _spoken(result) == ["There's an outage in your area."]


def test_without_halt_gating_both_matching_branches_speak():
  """The negative control: strip the derived gating and the caller hears BOTH
  verdicts concatenated. This is what `verdict()` exists to prevent."""
  cfg = _verdict_config()
  raw = {b.say: b.condition for b in _branches()}
  for rung in _ladder_of(cfg, "diagnose"):
    text = rung["response"][0]["text"]
    rung["condition"] = raw[text]
  result = _drive_spine(cfg)
  assert _spoken(result) == ["There's an outage in your area.",
                             "Your account is suspended.",
                             "Everything looks healthy."]


def test_lower_rungs_stay_open_once_a_higher_one_wins():
  result = _drive_spine(_verdict_config())
  status = {s["name"]: s["status"] for s in result["slot_inspection"]["slots"]}
  assert status["diagnose_branch_0"] == "filled"
  assert status["diagnose_branch_1"] == "open"
  assert status["diagnose_branch_2"] == "open"


# --- behaviour: a SKIPPED diagnostic must not wedge the ladder ---------------
# Every rung requires every spine run-flag, and a condition-gated diagnostic that is
# skipped never reports one. Without the derived gate on its output slots the turn
# dead-ends: no rung can fire, no question is left to ask, and the caller hears
# nothing at all on the turn the verdict was supposed to land.

_VIP_GATE = {"slot": "segment", "eq": "vip"}


def _conditional_spine_flow() -> flows.Flow:
  """A two-check spine where `vip_check` only runs for a VIP — and does not here."""
  return verdict("diagnose", spine=[
      task("line_check", "check_line", [], "line_status"),
      task("vip_check", "check_vip", [], "vip_status", condition=_VIP_GATE),
  ], branches=[
      VerdictBranch(condition={"slot": "line_status", "eq": "outage"},
                    say="There's an outage in your area.",
                    reads=["line_status", "segment"]),
  ], root_agent="Diag_Agent")


def _conditional_spine_config() -> dict:
  f = _conditional_spine_flow()
  f.set("single_flow", True)
  f.set("gate_slot", "active_flow")
  f.set("bootstrap", {"slot": "active_flow", "reset_on_complete": True})
  return f.to_config()


def _drive_line_check(config: dict, event_data: dict | None = None) -> dict:
  """Report the unconditional diagnostic only — `vip_check` is gated off."""
  engine_sim.reset_store()
  session_id, _ = engine_sim.start(config, "diagnose")
  if event_data:
    engine_sim.step({"session_id": session_id, "kind": "event_prefill",
                     "event_data": event_data})
  return engine_sim.step({
      "session_id": session_id, "kind": "task_result", "task_name": "line_check",
      "result": {"line_status": "outage"}, "success": True,
  })


def test_a_skipped_spine_task_does_not_hang_the_ladder():
  result = _drive_line_check(_conditional_spine_config())
  assert _spoken(result) == ["There's an outage in your area."]


def test_without_the_derived_gate_a_skipped_spine_task_hangs_the_turn():
  """The negative control for the test above: strip the gate off the skipped
  diagnostic's output slots and the ladder can never resolve."""
  cfg = _conditional_spine_config()
  for slot in cfg["slots"]:
    if slot["name"].startswith("vip_"):
      slot.pop("condition", None)
  result = _drive_line_check(cfg)
  assert _spoken(result) == []
  assert result["next_action"] != "preempt"


def test_a_conditional_spine_still_emits_a_legal_app():
  """The derived slot gate is a real condition the framework validator reads — it
  must resolve against a declared slot, not just satisfy the engine."""
  errors, _warnings = flows.validate_app(
      flows.App(root_flow=_conditional_spine_flow(), app_display_name="Diagnostics"))
  assert errors == [], errors


def test_the_ladder_still_waits_for_a_diagnostic_whose_gate_HOLDS():
  """The waiver is the gate, not a blanket exemption: with `segment` = vip the
  diagnostic will run, so the rung must wait for it exactly as before."""
  result = _drive_line_check(_conditional_spine_config(), event_data={"segment": "vip"})
  assert _spoken(result) == []


def test_the_spine_fires_without_a_user_turn():
  """No question is ever asked: the verdict lands on the diagnostics alone."""
  engine_sim.reset_store()
  _, opening = engine_sim.start(_verdict_config(), "diagnose")
  assert opening["next_action"] == "fire"
  assert opening["sm"].get("_awaiting") in (None, "")


# --- behaviour: what `preempt` does to the TURN ------------------------------
# `test_announce_always_emits_preempt` above only proves the key is THERE. These drive
# the emitted config through the engine and pin what each value does, so a regression
# that emitted the wrong value — not merely a missing key — is caught too. Live, the
# difference is whether the model runs at all: a non-preempting announce leaves the
# model free to render the next question and keep calling setters in the same turn; a
# preempting one is dispatched straight to the caller and the turn ends there. See
# examples/announce_preempt.py for the deployed conversations.

_ASK = "In store, or delivered?"
_NOTICE = "Ready within two hours."


def _announce_turn(announce_slot: dict) -> dict:
  """Fill the announce's prerequisite via a task result and return that turn."""
  f = flows.Flow("notice_demo", root_agent="Notice_Agent")
  f.add(flows.result_slot("route", "check"), announce_slot,
        flows.user_slot("follow_up", ask=_ASK, requires=["route"]))
  f.task("check", "check_route", [], "route")
  engine_sim.reset_store()
  session_id, _ = engine_sim.start(f.to_config(), "notice_demo")
  return engine_sim.step({
      "session_id": session_id, "kind": "task_result", "task_name": "check",
      "result": {"route": "ok"}, "success": True,
  })


def _inline(result: dict) -> list[str]:
  """Text the ENGINE speaks itself, bypassing the model."""
  return [p["text"] for p in result["response_parts"] if p.get("type") == "text"]


def test_non_preempting_announce_lets_the_turn_continue():
  """The model keeps the turn: it is handed the next question and the announce is
  queued behind whatever it says. Nothing is dispatched over its head — which is what
  leaves it free to also capture a value the caller volunteered in the same breath."""
  result = _announce_turn(announce("notice", [_NOTICE], requires=["route"],
                                   preempt=False))
  assert result["next_action"] == "next_question"
  assert result["agent_text"] == _ASK           # the directive reaches the model
  assert _inline(result) == []                  # ...and the engine says nothing itself
  assert [p["text"] for p in result["sm"]["_pending_announce_payloads"]] == [_NOTICE]


def test_preempting_announce_stops_the_turn():
  """The engine speaks and the model never runs: the announce goes out verbatim, the
  question is folded in behind it as canned text, and no model turn remains in which a
  setter could fire."""
  result = _announce_turn(announce("notice", [_NOTICE], requires=["route"],
                                   preempt=True))
  assert result["next_action"] == "preempt"
  assert result["agent_text"] == ""             # nothing is handed to the model
  assert _inline(result) == [_NOTICE, _ASK]     # the engine reads both out
  assert "_pending_announce_payloads" not in result["sm"]


def test_omitting_preempt_is_read_as_preempting():
  """Why the key must ALWAYS be emitted: the engine's `slot_def.get("preempt", True)`
  turns a dropped key into the stop-the-turn behaviour, silently inverting an author's
  `preempt=False`. Same drive as the non-preempting test above, key removed."""
  slot = announce("notice", [_NOTICE], requires=["route"], preempt=False)
  slot.pop("preempt", None)
  result = _announce_turn(slot)
  assert result["next_action"] == "preempt"
  assert _inline(result) == [_NOTICE, _ASK]
