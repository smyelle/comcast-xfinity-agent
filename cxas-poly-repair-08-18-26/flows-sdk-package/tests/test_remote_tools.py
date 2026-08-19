"""A remote tool: declared here, implemented by a service that deploys separately.

The behaviour these pin is the whole point of the feature, and none of it is visible
from a green build:

* the START call answers with a job handle, and that must NOT complete the task. It is
  a perfectly successful HTTP call returning `success: true`, so left alone it completes
  the task on the turn it started with none of the task's slots filled.
* the job is then polled ONCE PER TURN. Once per PASS is the failure that matters: a
  task whose out_slot is unfilled stays fire-eligible, so an unguarded poll re-dispatches
  on every reasoning pass and burns the turn to the platform's ten-loop cap.
* every job in flight is polled TOGETHER, which is what makes remote tools parallel by
  construction rather than by arrangement.
* a terminal status completes the task exactly as a local tool would — same slots, same
  `then_say`, same `on_failure` ladder keyed on the service's own `error_code`.

Offline throughout: no service is deployed and none is needed, which is the same
property that lets an ordinary ladder oracle stay ignorant of the fact a tool is remote.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import flows  # noqa: E402
from flows.engine import loader as fb  # noqa: E402
from flows.authoring import openapi as _openapi  # noqa: E402
from toolset_testkit import run_wrapper  # noqa: E402
from toolset_testkit import source as _source  # noqa: E402

URL = "https://remote-report-demo.example.run.app"


def _app(timeout=600, on_failure=None, terminal=False, extra=()):
  """One remote task, plus any extra remote tasks the caller wants beside it."""
  _openapi._REMOTE_TOOLS.clear()
  _openapi._DECLARED.clear()
  ts = flows.openapi_toolset("report_service", base_url=URL, env_scoped=True,
                             auth=flows.service_agent_auth())
  build = flows.remote_tool("build_report", ts, "buildReport",
                            params={"account": str},
                            outputs={"headline": str, "rows": int}, timeout=timeout)
  f = flows.Flow("report", root_agent="agent")
  f.add(flows.user_slot("account", ask="Which account?"),
        flows.result_slot("headline", "BuildReport"),
        flows.result_slot("rows", "BuildReport"))
  f.task(flows.task("BuildReport", build, ["account"], "headline",
                    out_key="headline", extra_outputs={"rows": "rows"},
                    terminal=terminal, then_say="All done: {headline}.",
                    on_failure=on_failure))
  for name, tool_name, out in extra:
    rt = flows.remote_tool(tool_name, ts, tool_name,
                           params={"account": str}, outputs={out: str})
    f.add(flows.result_slot(out, name))
    f.task(flows.task(name, rt, ["account"], out, out_key=out,
                      then_say=f"{name} says {{{out}}}."))
  app = flows.App(root_flow=f, toolsets=[ts], app_display_name="t")
  from flows.authoring.build import _assemble  # noqa: PLC0415
  all_map, _bodies, _avail = _assemble(app)
  return all_map["report"]


def _sm(config, filled=None):
  """A slot machine the way the live one arrives at intake: ENGINE-SEEDED.

  `seed_sm` alone is not enough. `_executor_tasks` — the tool -> task map intake
  resolves everything through, and where a remote tool's registry entry is attached —
  is built by the engine on its first pass, so an sm that has never been through the
  engine makes intake a no-op and resets it.
  """
  sm = fb.seed_sm(config)
  sm["filled"] = dict(filled or {"account": "A-1042"})
  sm["pending"] = {}
  sm["_config_id"] = "report"
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "report"
    sm["filled"][gate] = "report"
  seeded = _drive(config, sm)["sm"]
  seeded["filled"] = dict(filled or {"account": "A-1042"})
  if gate:
    seeded[gate] = "report"
    seeded["filled"][gate] = "report"
  return seeded


def _drive(config, sm, turn=1, tick=False):
  """One engine pass. `tick` is the turn a SILENT caller produces.

  A CES inactivity tick is a turn in every sense this feature cares about and in no
  sense `n_user_turns` counts: nobody spoke, so the caller-turn counter does not move.
  Passing it as a distinct flag rather than by bumping `turn` is the point — a test
  that bumped the turn number would pass against the engine that only ever polled once.
  """
  return fb.load_engine().slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "",
      "scanned_user_text": "A-1042", "is_inactivity": tick, "event_data": {},
      "config_id": "report", "n_user_turns": turn,
  })


def _fired(result):
  """Tool names dispatched, from either an engine result or a bare action."""
  action = (result or {}).get("action", result) or {}
  calls = action.get("function_calls") or (
      [action["function_call"]] if action.get("function_call") else [])
  return [c["name"] for c in calls if c["name"] != "settle_guard"]


def _said(result):
  return ((result or {}).get("action") or {}).get("message") or ""


def _intake(sm, tool, payload):
  """Hand a tool result to intake the way after_tool does."""
  return fb.load_intake().slot_intake({
      "tool_name": tool, "response_data": payload, "sm": sm,
      "current_agent": "", "channel": "",
  })["sm"]


# ── the contract the config carries ──────────────────────────────────────────


def test_the_config_pairs_the_start_tool_with_its_status_tool():
  """Without this the engine cannot tell a remote task from any other API call."""
  cfg = _app()
  entry = cfg["remote_tools"]["build_report"]
  assert entry["status_tool"] == "build_report__status"
  assert entry["job_slot"] == "build_report__job"
  assert entry["timeout_seconds"] == 600
  assert sorted(entry["outputs"]) == ["headline", "rows"]


def test_the_job_slot_is_declared_and_mapped_without_the_author_naming_it():
  cfg = _app()
  assert any(s["name"] == "build_report__job" for s in cfg["slots"])
  task = next(t for t in cfg["tasks"] if t["name"] == "BuildReport")
  assert task["outputs"]["build_report__job"] == "build_report__job"


# ── the start call must not finish the task ──────────────────────────────────


def test_a_job_handle_does_not_complete_the_task():
  """THE defect this design exists to prevent.

  The start call is a successful HTTP request that returns `success: true`, so nothing
  in the ordinary path would stop it completing the task — with `headline` and `rows`
  never filled, and `then_say` speaking a result nobody computed.
  """
  cfg = _app()
  sm = _sm(cfg)
  sm = _intake(sm, "build_report",
               {"success": True, "build_report__job": "job-77"})
  assert sm.get("_task_just_completed") is None, (
      "the start call completed the task, so the caller is told the report is ready "
      "before the job has run")
  assert "headline" not in sm["filled"]
  assert sm["filled"]["build_report__job"] == "job-77"
  assert sm["_remote_started"]["BuildReport"]["job"] == "job-77"


def test_a_start_that_returns_no_handle_fails_by_name():
  """Otherwise the task waits out its budget for an answer that can never arrive."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report", {"success": True})
  assert sm["task_results"]["BuildReport"]["error_code"] == "remote_bad_handle"


# ── the poll ─────────────────────────────────────────────────────────────────


def test_the_job_is_polled_once_per_turn_and_not_once_per_pass():
  """A second pass in the same turn must not re-dispatch: that is the ten-loop cap."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  first = _drive(cfg, sm)
  assert "build_report__status" in _fired(first), _fired(first)
  again = _drive(cfg, first["sm"])
  assert "build_report__status" not in _fired(again), (
      "polled twice inside one turn — this is what burns the turn to the cap")


def test_a_later_turn_polls_again():
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  first = _drive(cfg, sm)
  sm = first["sm"]
  sm["_turn_n"] = int(sm.get("_turn_n") or 0) + 1
  assert "build_report__status" in _fired(_drive(cfg, sm, turn=2))


def test_running_keeps_the_job_open():
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status", {"success": True, "status": "running"})
  assert sm.get("_task_just_completed") is None
  assert "headline" not in sm["filled"]


def test_every_job_in_flight_is_polled_together():
  """Parallel BY CONSTRUCTION: three handles are three calls on one dispatch, so the
  caller waits once rather than three times."""
  cfg = _app(extra=[("Credit", "credit_check", "credit"),
                    ("Fraud", "fraud_check", "fraud")])
  sm = _sm(cfg)
  for tool, job in (("build_report", "j1"), ("credit_check", "j2"),
                    ("fraud_check", "j3")):
    sm = _intake(sm, tool, {"success": True, f"{tool}__job": job})
  fired = _fired(_drive(cfg, sm))
  assert sorted(fired) == ["build_report__status", "credit_check__status",
                           "fraud_check__status"], fired


# ── landing ──────────────────────────────────────────────────────────────────


def test_done_completes_the_task_exactly_as_a_local_tool_would():
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status",
               {"success": True, "status": "done", "headline": "Up 4%", "rows": 812})
  assert sm["_task_just_completed"] == "BuildReport"
  assert sm["filled"]["headline"] == "Up 4%"
  assert sm["filled"]["rows"] == 812


def test_the_in_flight_mark_is_released_when_the_job_lands():
  """Left marked, the task stays out of the selector forever and the flow wedges."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  assert "BuildReport" in (sm.get("_awaiting_async") or {})
  sm = _intake(sm, "build_report__status",
               {"success": True, "status": "done", "headline": "Up 4%", "rows": 812})
  sm = _drive(cfg, sm)["sm"]
  assert "BuildReport" not in (sm.get("_awaiting_async") or {})


def test_the_landing_turn_speaks_then_say():
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status",
               {"success": True, "status": "done", "headline": "Up 4%", "rows": 812})
  out = _drive(cfg, sm)
  assert "Up 4%" in _said(out), _said(out)


# ── failure, in the vocabulary tasks already use ─────────────────────────────


def test_a_failed_job_carries_its_error_code_into_on_failure():
  cfg = _app(on_failure={"max_retries": 0, "on_exhaust": {
      "say": {"remote_timeout": "That took far too long.",
              "_default": "I cannot pull that report."}}})
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status",
               {"success": True, "status": "timeout"})
  assert sm["task_results"]["BuildReport"]["error_code"] == "remote_timeout"
  assert not sm["task_results"]["BuildReport"]["success"]
  out = _drive(cfg, sm)
  assert "far too long" in _said(out), _said(out)


def test_a_crashed_job_is_told_apart_from_a_timed_out_one():
  """Both are failures; only a distinct code lets a flow answer them differently."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status", {"success": True, "status": "failed"})
  assert sm["task_results"]["BuildReport"]["error_code"] == "remote_failed"


def test_the_service_own_error_code_wins_over_the_derived_one():
  """`remote_job_lost` is the failure independent deploys create, and only the service
  can report it — a redeployed service no longer knows the handle."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status",
               {"success": True, "status": "failed", "error_code": "remote_job_lost"})
  assert sm["task_results"]["BuildReport"]["error_code"] == "remote_job_lost"


# ── an app with no remote tool is untouched ──────────────────────────────────


def test_a_config_with_no_remote_tool_carries_no_registry_and_no_poll():
  f = flows.Flow("plain", root_agent="agent")
  f.add(flows.user_slot("account", ask="Which account?"))
  cfg = flows.App(root_flow=f, app_display_name="t").root_flow.to_config()
  assert "remote_tools" not in cfg
  sm = _sm(cfg)
  assert not [n for n in _fired(_drive(cfg, sm)) if n.endswith("__status")]


# ── what the first live drive found (2026-08-10; REMOTE_TOOL_VERIFY.md) ──────


def test_the_status_tool_is_listed_on_the_agent():
  """No flow names it, so nothing else puts it there — and CES drops a dispatch to a
  tool the agent does not list WITHOUT an error. Live, that showed as a poll logged on
  every turn for six minutes that never became a single HTTP request."""
  from flows.authoring.build import scoped_agent_tools  # noqa: PLC0415
  cfg = _app()
  listed = scoped_agent_tools("report", [cfg], [])
  assert "build_report__status" in listed, listed
  assert "build_report" in listed


def test_the_status_tool_is_hidden_from_the_model():
  """Listed so the ENGINE can dispatch it; hidden so the model cannot."""
  cfg = _app()
  out = _drive(cfg, _sm(cfg))
  assert "build_report__status" in (out["action"].get("hide_tools") or [])


def test_a_numeric_param_reaches_the_wrapper_as_a_string():
  """A generated wrapper's parameters are all `str` and CES type-checks the call before
  the body runs, so a numeric slot is refused outright: "Expected `String`, received
  `kotlin.Double` (240.0)". The double, rather than the 240 the author wrote, is the
  config default coming back through a protobuf Struct."""
  _openapi._REMOTE_TOOLS.clear()
  _openapi._DECLARED.clear()
  ts = flows.openapi_toolset("report_service", base_url=URL, env_scoped=True,
                             auth=flows.service_agent_auth())
  build = flows.remote_tool("build_report", ts, "buildReport",
                            params={"account": str, "duration_seconds": int},
                            outputs={"headline": str})
  f = flows.Flow("report", root_agent="agent")
  f.add(flows.user_slot("account", ask="Which account?"),
        flows.event_slot("duration_seconds", default=240),
        flows.result_slot("headline", "BuildReport"))
  f.task(flows.task("BuildReport", build, ["account", "duration_seconds"], "headline",
                    out_key="headline"))
  from flows.authoring.build import _assemble  # noqa: PLC0415
  cfg = _assemble(flows.App(root_flow=f, toolsets=[ts], app_display_name="t"))[0]["report"]
  sm = _sm(cfg, filled={"account": "A-1042", "duration_seconds": 240.0})
  # A fresh turn. Seeding the sm already dispatched once, and a second dispatch inside
  # the same turn is the re-fire `_sync_fire_pending` exists to stop -- asking for one
  # here would be asking for the 10-loop cap back.
  action = _drive(cfg, sm, turn=2)["action"]
  args = (action.get("function_call") or {}).get("args") or {}
  assert args.get("duration_seconds") == "240", args
  assert args.get("account") == "A-1042", args


def _typed_wrapper(live, **types):
  """The generated START wrapper for a remote tool with declared parameter types.

  Executed rather than string-matched, because the coercion it does is emitted source
  that nothing else compiles — and the whole point of it is what the request carries.
  """
  _openapi._REMOTE_TOOLS.clear()
  _openapi._DECLARED.clear()
  ts = flows.openapi_toolset("report_service", base_url=URL, env_scoped=True,
                             auth=flows.service_agent_auth())
  flows.remote_tool("build_report", ts, "buildReport",
                    params={"account": str, **types}, outputs={"headline": str})
  return run_wrapper("build_report", symbol=ts.symbol("buildReport"), live=live)


def test_an_int_param_takes_the_integral_float_STRING_as_well_as_the_float():
  """`int("240.0")` RAISES, and the wrapper's coercion is the last thing standing
  between a slot and the wire. A numeric default crosses CES as a protobuf Struct where
  every number is a double, so an authored `240` can reach the wrapper as 240.0 or as
  the text "240.0" — and left uncoerced, the string is what a spec saying `integer`
  rejects, with the flow reporting only that the tool failed."""
  sent = {}
  wrapper = _typed_wrapper(lambda request: sent.update(request) or {"jobId": "j-1"},
                           duration_seconds=int)
  for given in ("240.0", "240", 240.0, 240):
    sent.clear()
    wrapper(account="A-1042", duration_seconds=given)
    assert sent["duration_seconds"] == 240, (given, sent)
    assert isinstance(sent["duration_seconds"], int), (given, sent)


def test_a_fractional_value_for_an_int_param_is_left_alone_not_truncated():
  """Silently sending 240 for an asked-for 240.5 would answer a question nobody put.
  It is a caller error, so it goes to the service and comes back as one."""
  sent = {}
  wrapper = _typed_wrapper(lambda request: sent.update(request) or {"jobId": "j-1"},
                           duration_seconds=int)
  for given in ("240.5", "not a number"):
    sent.clear()
    wrapper(account="A-1042", duration_seconds=given)
    assert sent["duration_seconds"] == given, (given, sent)


def test_a_numeric_slot_survives_the_whole_seam_from_engine_to_service():
  """The engine stringifies for CES's type check and the wrapper casts back for the
  API's, and neither half can be read as correct on its own — a value that clears one
  and not the other still fails the call. Driven through both, in that order, with the
  string shape that is exactly what falls between them."""
  _openapi._REMOTE_TOOLS.clear()
  _openapi._DECLARED.clear()
  ts = flows.openapi_toolset("report_service", base_url=URL, env_scoped=True,
                             auth=flows.service_agent_auth())
  build = flows.remote_tool("build_report", ts, "buildReport",
                            params={"account": str, "duration_seconds": int},
                            outputs={"headline": str})
  f = flows.Flow("report", root_agent="agent")
  f.add(flows.user_slot("account", ask="Which account?"),
        flows.event_slot("duration_seconds", default=240),
        flows.result_slot("headline", "BuildReport"))
  f.task(flows.task("BuildReport", build, ["account", "duration_seconds"], "headline",
                    out_key="headline"))
  from flows.authoring.build import _assemble  # noqa: PLC0415
  cfg = _assemble(flows.App(root_flow=f, toolsets=[ts], app_display_name="t"))[0]["report"]

  sent = {}
  wrapper = run_wrapper("build_report", symbol=ts.symbol("buildReport"),
                        live=lambda request: sent.update(request) or {"jobId": "j-1"})
  for held in (240.0, "240.0", 240):
    sm = _sm(cfg, filled={"account": "A-1042", "duration_seconds": held})
    # Turn 2: seeding already fired once, and `_sync_fire_pending` holds the re-fire
    # inside a turn until the result lands.
    args = ((_drive(cfg, sm, turn=2)["action"].get("function_call") or {}).get("args")) or {}
    assert isinstance(args["duration_seconds"], str), (held, args)
    sent.clear()
    wrapper(**args)
    assert sent["duration_seconds"] == 240, (held, args, sent)


def test_the_int_helper_is_only_emitted_for_a_tool_that_declares_one():
  """Emitted source is read by whoever debugs a rejected call; a helper no parameter
  uses is noise in the one place noise costs the most."""
  sent = {}
  _typed_wrapper(lambda request: sent.update(request) or {"jobId": "j-1"})
  assert "_as_int" not in _source("build_report")
  _typed_wrapper(lambda request: sent.update(request) or {"jobId": "j-1"},
                 duration_seconds=int)
  assert "_as_int" in _source("build_report")


def test_a_finished_job_is_not_read_as_a_failure_for_want_of_an_error_code():
  """The wrapper reports success as "every declared output came back", which on a
  status poll counts the `error_code` a healthy job does not have. Live, a job that had
  just succeeded went down its on_failure ladder with the report in the payload."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status",
               {"success": False, "error": "missing from response: error_code",
                "status": "done", "headline": "Up 4%", "rows": 812})
  assert sm["task_results"]["BuildReport"]["success"], sm["task_results"]
  assert sm["filled"]["headline"] == "Up 4%"


def test_a_done_answer_missing_its_outputs_is_a_contract_failure():
  """Finished, and not what was declared — which is a different thing from a job that
  ran and failed, and needs a different answer."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status", {"status": "done", "headline": "Up 4%"})
  result = sm["task_results"]["BuildReport"]
  assert not result["success"]
  assert result["error_code"] == "remote_contract", result


def test_max_retries_keyed_by_error_code_picks_the_branch():
  """A LOST job is worth starting over; a job that ran and failed is not. Reading this
  as a plain number crashed the engine outright — `'>=' not supported between instances
  of 'int' and 'dict'` — which reached the caller as "I wasn't able to complete that
  request" and nothing else."""
  ladder = {"max_retries": {"remote_job_lost": 2, "_default": 0},
            "retry_say": {"remote_job_lost": "Let me start that over."},
            "on_exhaust": {"say": "I cannot pull that report."}}
  cfg = _app(on_failure=ladder)
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status",
               {"success": True, "status": "failed", "error_code": "remote_job_lost"})
  out = _drive(cfg, sm)
  assert "start that over" in _said(out), _said(out)
  assert "build_report" in _fired(out), _fired(out)

  cfg = _app(on_failure=ladder)
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-78"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status",
               {"success": True, "status": "failed", "error_code": "remote_failed"})
  out = _drive(cfg, sm)
  assert "cannot pull that report" in _said(out), _said(out)


def test_a_remote_wait_speaks_its_opening_line():
  """A remote start call answers synchronously, so the wait never takes the `pending`
  turn `awaits.say` is normally spoken on. Live, the first thing said about a brand-new
  job was the second rung of the ladder."""
  cfg = _app()
  for task in cfg["tasks"]:
    task["awaits"] = {"max_turns": 40, "say": "Starting that now.",
                      "while_waiting": ["Still crunching the numbers."]}
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  # The turn the job starts: the poll goes out on the first pass and the line is spoken
  # on the pass after it, which is the shape every other idle hold has.
  poll = _drive(cfg, sm)
  assert _fired(poll) == ["build_report__status"], _fired(poll)
  opening = _drive(cfg, poll["sm"])
  assert "Starting that now." in _said(opening), _said(opening)
  # The turn after it takes the next rung down, not the opening line again.
  nxt = _drive(cfg, _drive(cfg, opening["sm"], turn=2)["sm"], turn=2)
  assert "Still crunching" in _said(nxt), _said(nxt)


# ── the silent caller ────────────────────────────────────────────────────────
# The channel the feature was designed for, and the one the turn counter cannot see.
# A caller who says nothing produces INACTIVITY TICKS and no utterances, so
# `n_user_turns` — and `_turn_n` with it — is frozen for the whole wait. Everything
# below was broken live and offline-invisible, because the reassurance ladder is not
# turn-guarded: it drained on every tick, so the call sounded exactly like a call that
# was polling while the engine made one request and then went quiet.


def _tick(cfg, sm):
  """A whole inactivity tick: the poll pass, then the pass that speaks."""
  polled = _drive(cfg, sm, tick=True)
  return polled, _drive(cfg, polled["sm"])


def test_an_inactivity_tick_polls_the_job():
  """THE defect. Measured live on a four-minute job with a silent caller: one GET in
  the service's request log for the whole call, against nine over text."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  first = _drive(cfg, sm)
  assert "build_report__status" in _fired(first)
  # No new caller turn — the caller has not spoken since. Only the tick.
  tick = _drive(cfg, first["sm"], tick=True)
  assert "build_report__status" in _fired(tick), (
      "a silent caller's tick did not poll, so the job's result is never collected "
      "and the call goes quiet for as long as the job runs")


def test_a_tick_polls_once_and_not_once_per_pass():
  """The guard the tick has to get past must not be removed, only re-based: the passes
  a tick goes on to make are still the same turn, and re-dispatching on each is what
  burns the turn to the platform's ten-loop cap."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  polled = _drive(cfg, sm, tick=True)
  assert "build_report__status" in _fired(polled)
  # The tick's own re-invoke: same tick, no fresh silence, so no second poll.
  again = _drive(cfg, polled["sm"])
  assert "build_report__status" not in _fired(again), _fired(again)


def test_every_tick_of_a_silent_wait_polls():
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  polls = 0
  for _ in range(6):
    polled, spoke = _tick(cfg, sm)
    polls += "build_report__status" in _fired(polled)
    sm = spoke["sm"]
  assert polls == 6, f"{polls} polls across six ticks of silence"


def test_a_job_that_lands_on_a_tick_is_spoken():
  """The whole call, from the caller's point of view: they say nothing after the
  account, and the report still reaches them."""
  cfg = _app()
  for task in cfg["tasks"]:
    task["awaits"] = {"max_turns": 40, "say": "Starting that now.",
                      "while_waiting": ["Still crunching the numbers."]}
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, _drive(cfg, sm)["sm"])["sm"]        # start turn: poll, then say
  for _ in range(3):                                    # silence, still running
    polled, spoke = _tick(cfg, sm)
    assert "build_report__status" in _fired(polled)
    sm = _intake(polled["sm"], "build_report__status",
                 {"success": True, "status": "running"})
    sm = _drive(cfg, sm)["sm"]
  polled = _drive(cfg, sm, tick=True)                   # the tick it lands on
  assert "build_report__status" in _fired(polled), (
      "nothing was polled on this tick, so the job's result is never asked for")
  sm = _intake(polled["sm"], "build_report__status",
               {"success": True, "status": "done", "headline": "Up 4%", "rows": 812})
  out = _drive(cfg, sm)
  assert sm["filled"]["rows"] == 812
  assert "Up 4%" in _said(out), _said(out)


def test_max_turns_is_reachable_on_a_silent_call():
  """`awaits.max_turns` is the only thing between a wedged backend and a wedged call.
  Measured against caller turns it could never fire on the channel that needs it."""
  cfg = _app()
  for task in cfg["tasks"]:
    task["awaits"] = {"max_turns": 3, "say": "Starting that now.",
                      "on_timeout": {"say": "That is taking far too long."}}
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  said = ""
  for _ in range(5):
    polled, spoke = _tick(cfg, sm)
    said = said or ("far too long" in _said(polled) and "polled") or (
        "far too long" in _said(spoke) and "spoke")
    sm = spoke["sm"]
  assert said, "the wait never gave up, so a hung job holds a silent call forever"


def test_the_two_wait_clocks_agree():
  """`_wait_clock` is duplicated into slot_intake because CES tools cannot import each
  other, and the two stamp/measure the SAME mark — a drift between them reads as a wait
  that started in the future and never times out."""
  eng = fb.load_engine()
  intake = fb.load_intake()
  for sm in ({}, {"_turn_n": 4}, {"_tick_n": 7}, {"_turn_n": 4, "_tick_n": 7},
             {"_turn_n": 2.0, "_tick_n": None}):
    assert eng._wait_clock(sm) == intake._wait_clock(sm), sm
  assert eng._wait_clock({"_turn_n": 4, "_tick_n": 7}) == 11
  # Identical to the caller-turn counter wherever there are no ticks, which is every
  # text call, every offline oracle and every other test in this suite.
  assert eng._wait_clock({"_turn_n": 9}) == 9


# ── the mock: what makes any of this drivable without a service ──────────────
#
# This whole section is a regression suite for one defect, and it is worth stating
# plainly because everything above it was green while it was true: `mock=` did not work.
# A plain value did, by riding the HTTP mock machinery. `after_turns(...)` and
# `remote_error(...)` did NOT — they emitted `{"turns": n, "result": ...}` into the
# config, which no line of the engine read, and emitted NO mock tool at all. So the
# shapes documented as the way to drive the waiting and the failure branches offline
# produced an app that quietly called the real service. Every test below fails against
# that engine.


def _mock_app(mock, *, params=None, outputs=None):
  """One remote tool with a declared mock, mocking ON, and its wrappers registered."""
  _openapi._REMOTE_TOOLS.clear()
  _openapi._DECLARED.clear()
  ts = flows.openapi_toolset("report_service", base_url=URL, env_scoped=True,
                             auth=flows.service_agent_auth())
  build = flows.remote_tool("build_report", ts, "buildReport",
                            params=params or {"account": str},
                            outputs=outputs or {"headline": str, "rows": int},
                            mock=mock)
  f = flows.Flow("report", root_agent="agent")
  f.add(flows.user_slot("account", ask="Which account?"),
        flows.result_slot("headline", "BuildReport"),
        flows.result_slot("rows", "BuildReport"))
  f.task(flows.task("BuildReport", build, ["account"], "headline",
                    out_key="headline", extra_outputs={"rows": "rows"}))
  app = flows.App(root_flow=f, toolsets=[ts], app_display_name="t", mock_apis=True)
  from flows.authoring.build import _assemble  # noqa: PLC0415
  all_map, _bodies, _avail = _assemble(app)
  return all_map["report"], ts


def _poll(ts, job="mock-build_report", variables=None):
  """One poll of the mocked status wrapper, exactly as the engine dispatches it."""
  wrapper = run_wrapper("build_report__status",
                        symbol=ts.symbol("buildReportStatus"),
                        variables={"mock_apis": True, **(variables or {})})
  return wrapper(jobId=job)


def _start(ts, variables=None):
  wrapper = run_wrapper("build_report", symbol=ts.symbol("buildReport"),
                        variables={"mock_apis": True, **(variables or {})})
  return wrapper(account="A-1042")


def test_a_plain_mock_answers_a_finished_job_with_its_outputs():
  _cfg, ts = _mock_app({"headline": "Revenue up 4%", "rows": 8213})
  assert _start(ts)["build_report__job"] == "mock-build_report"
  out = _poll(ts)
  assert out["status"] == "done"
  assert (out["headline"], out["rows"]) == ("Revenue up 4%", 8213)


def test_a_code_block_mock_answers_from_the_session():
  """The gap this section exists for. Every other CES tool fake is python with the
  session in scope; a remote tool's was static data, so an agent that selects its
  scenario through a variable could express exactly ONE of its scenarios."""
  _cfg, ts = _mock_app(_scenario_mock)
  healthy = _poll(ts, variables={"scenario": "healthy"})
  impaired = _poll(ts, variables={"scenario": "impaired"})
  assert healthy["status"] == impaired["status"] == "done"
  assert healthy["headline"] == "all clear"
  assert impaired["headline"] == "line impaired"
  assert (healthy["rows"], impaired["rows"]) == (1, 2)


def _scenario_mock():
  """A remote job's answer, chosen by a session variable."""
  scenario = (context.variables or {}).get("scenario")  # noqa: F821
  if scenario == "impaired":
    return {"headline": "line impaired", "rows": 2}
  return {"headline": "all clear", "rows": 1}


def test_a_code_block_may_answer_a_failure_instead_of_a_result():
  """`status` and `error_code` are refused as OUTPUT names, so a dict carrying one is
  unambiguously the status envelope rather than a job's payload."""
  _cfg, ts = _mock_app(_failing_mock)
  out = _poll(ts)
  assert out["status"] == "failed"
  assert out["error_code"] == "remote_job_lost"


def _failing_mock():
  return {"status": "failed", "error_code": "remote_job_lost"}


def test_a_code_block_that_wants_arguments_is_refused_at_build():
  """Calling it with none would raise inside the sandbox on the first poll, which looks
  like a tool that answered nothing rather than a mistake in the app file."""
  try:
    _mock_app(_needs_arguments)
  except ValueError as exc:
    assert "'account'" in str(exc)
    assert "context.variables" in str(exc)
  else:
    raise AssertionError("a mock the poll cannot call was accepted")


def _needs_arguments(account):
  return {"headline": account, "rows": 1}


def test_after_turns_holds_the_job_open_and_then_lands_it():
  _cfg, ts = _mock_app(flows.after_turns(3, {"headline": "late", "rows": 7}))
  assert _poll(ts, job="mock-build_report#0")["status"] == "running"
  assert _poll(ts, job="mock-build_report#2")["status"] == "running"
  landed = _poll(ts, job="mock-build_report#3")
  assert landed["status"] == "done"
  assert (landed["headline"], landed["rows"]) == ("late", 7)


def test_after_turns_takes_a_code_block_too():
  """So a scenario the session picks can also be held open for a few turns."""
  _cfg, ts = _mock_app(flows.after_turns(2, _scenario_mock))
  assert _poll(ts, job="mock-build_report#1")["status"] == "running"
  out = _poll(ts, job="mock-build_report#2", variables={"scenario": "impaired"})
  assert (out["status"], out["headline"]) == ("done", "line impaired")


def test_remote_error_fails_the_job_with_its_code():
  _cfg, ts = _mock_app(flows.remote_error("remote_job_lost"))
  out = _poll(ts, job="mock-build_report#1")
  assert out["status"] == "failed"
  assert out["error_code"] == "remote_job_lost"


def test_a_turn_based_mock_emits_a_mock_tool_at_all():
  """The defect itself, pinned at its narrowest. `after_turns(...)` registered no mock
  tool, so the emitted wrapper had no mock branch and every 'mocked' poll went to the
  real service — with the app validating clean and the config carrying a `mock` key
  that looked like proof it was honoured."""
  _cfg, _ts = _mock_app(flows.after_turns(3, {"headline": "late", "rows": 7}))
  assert "build_report__status_mock" in _source("build_report__status")
  assert "build_report_mock" in _source("build_report")


def test_the_registry_carries_no_mock_it_cannot_honour():
  """A config field nothing reads is how this went unnoticed for a whole feature."""
  cfg, _ts = _mock_app(flows.after_turns(3, {"headline": "late", "rows": 7}))
  assert "mock" not in cfg["remote_tools"]["build_report"]


def test_the_poll_carries_the_turn_count_only_for_a_synthetic_handle():
  """A real service's handle is a path parameter it will look up verbatim. The suffix
  rides on `mock-...`, which only the START wrapper's own mock ever produces."""
  eng = fb.load_engine()
  sm = {"_turn_n": 5}
  real = eng._remote_poll_handle(sm, {"job": "558514282aa1", "since": 1})
  synthetic = eng._remote_poll_handle(sm, {"job": "mock-build_report", "since": 1})
  assert real == "558514282aa1"
  assert synthetic == "mock-build_report#4"


def test_the_turn_count_is_the_wait_clock_so_a_silent_caller_advances_it():
  """Ticks are the only turns a silent voice caller produces. Counted on `_turn_n` a
  mocked wait would never advance on the channel the whole feature exists for."""
  eng = fb.load_engine()
  mark = {"job": "mock-build_report", "since": 2}
  assert eng._remote_poll_handle({"_turn_n": 2, "_tick_n": 6}, mark).endswith("#6")


def test_a_typed_param_reaches_the_mock_TOOL_as_a_string():
  """The type rule that refused the START call, one layer in.

  A mock is a tool of its own, so it has no schema and takes every parameter as `str` —
  and CES type-checks a tool-to-tool `function_call` against that signature before the
  body runs. By the time the wrapper calls its mock, a declared `integer` has already
  been coerced away from `str` for the request, so the mocked call was refused with
  `Expected String, received kotlin.Double (240.0)` and the wrapper reported it as
  `success: false, missing from response: build_report__job`. Driven live on
  `examples/remote_tool.py`: a mocked job that never started, wearing a missing-output's
  name.
  """
  _openapi._REMOTE_TOOLS.clear()
  _openapi._DECLARED.clear()
  ts = flows.openapi_toolset("report_service", base_url=URL, env_scoped=True,
                             auth=flows.service_agent_auth())
  flows.remote_tool("build_report", ts, "buildReport",
                    params={"account": str, "duration_seconds": int},
                    outputs={"headline": str}, mock={"headline": "hi"})
  start = _source("build_report")
  assert "tools.build_report_mock({'account': account, "
  assert "'duration_seconds': _mock_arg(duration_seconds)" in start
  # The untyped one is passed straight through: it is already a string.
  assert "'account': account" in start
  assert "def _mock_arg(" in start


def test_the_mock_arg_helper_is_only_emitted_where_it_is_needed():
  """A wrapper with no typed parameter, or no mock, carries neither the helper nor the
  call — dead source in an emitted tool is source somebody has to read."""
  _openapi._REMOTE_TOOLS.clear()
  _openapi._DECLARED.clear()
  ts = flows.openapi_toolset("report_service", base_url=URL, env_scoped=True,
                             auth=flows.service_agent_auth())
  flows.remote_tool("all_strings", ts, "allStrings", params={"account": str},
                    outputs={"headline": str}, mock={"headline": "hi"})
  flows.remote_tool("no_mock", ts, "noMock", params={"n": int},
                    outputs={"headline": str})
  assert "_mock_arg" not in _source("all_strings")
  assert "_mock_arg" not in _source("no_mock")


# ─────────────────────────────────────────────────────────────────────────────
# What a callable mock may BE. All three of these were emitted happily and failed
# after a deploy, at a distance from the `mock=` that caused them.
# ─────────────────────────────────────────────────────────────────────────────
def _remote_with_mock(mock):
  """One remote tool carrying `mock`, returning the emitted status wrapper's mock."""
  _openapi._REMOTE_TOOLS.clear()
  _openapi._DECLARED.clear()
  ts = flows.openapi_toolset("report_service", base_url=URL, env_scoped=True,
                             auth=flows.service_agent_auth())
  flows.remote_tool("build_report", ts, "buildReport", params={"account": str},
                    outputs={"headline": str}, mock=mock)
  return _openapi._remote_status_mock_source("build_report", mock)


def test_a_lambda_mock_is_refused_rather_than_emitted_as_unparseable_source():
  """`<lambda>` is not an identifier. Emitted, the tool carries
  `return _envelope(<lambda>())` and does not load at all — which CES reports as a tool
  that does not exist, three steps from the `mock=` that wrote it."""
  import pytest
  with pytest.raises(ValueError) as exc:
    _openapi._remote_status_mock_source("build_report", lambda: {"rows": 12})
  assert "lambda" in str(exc.value)


def test_a_class_mock_is_refused_rather_than_CONSTRUCTED_by_the_emitted_tool():
  """A class is callable, so it passed every check and was emitted as `MyMock()` — which
  builds an instance instead of calling `__call__`. The tool then answers something no
  JSON encoder will take, and the wrapper reports a missing output."""
  import pytest

  class _Mock:
    def __call__(self):
      return {"rows": 3}

  with pytest.raises(ValueError) as exc:
    _openapi._remote_status_mock_source("build_report", _Mock)
  assert "CLASS" in str(exc.value)


def test_a_mock_closing_over_a_free_variable_is_refused():
  """Only the function's OWN source is inlined, so an enclosing name is simply gone in
  the sandbox and the first poll raises NameError. Named here instead."""
  import pytest

  def _outer():
    rows = 7

    def _inner():
      return {"rows": rows}
    return _inner

  with pytest.raises(ValueError) as exc:
    _openapi._remote_status_mock_source("build_report", _outer())
  assert "closes over 'rows'" in str(exc.value)


def _module_level_mock():
  return {"headline": "hi"}


def test_a_plain_def_is_still_emitted_and_the_generated_tool_parses():
  """The guard above must not cost the shape the feature exists for."""
  import ast
  src = _remote_with_mock(_module_level_mock)
  ast.parse(src)  # the failure a lambda produced
  assert "return _envelope(_module_level_mock())" in src


def test_the_same_guard_covers_an_ordinary_toolset_mock():
  """`mock_tool_source` inlines and calls by name for exactly the same reason."""
  import pytest
  from flows.authoring import toolset_common as _tc
  with pytest.raises(ValueError):
    _tc.mock_tool_source("t_mock", {"a": "a"}, lambda a: {"x": a}, "named")


# ─────────────────────────────────────────────────────────────────────────────
# What reaches the mock TOOL, and what the mock's own signature still decides.
# ─────────────────────────────────────────────────────────────────────────────
def test_a_structured_argument_reaches_the_mock_tool_as_JSON_not_as_a_repr():
  """`str({'a': 1})` is `"{'a': 1}"` — single quotes, `True`, `None`: not JSON. A mock
  doing the obvious thing with a structured parameter and calling `json.loads` raised in
  the sandbox, and the wrapper reported the whole call as a missing output."""
  from flows.authoring import toolset_common as _tc
  src = _tc.wrapper_tool_source(
      "t", "sym", description="d", params={"payload": "payload"}, outputs={},
      mock={"ok": True}, param_types={"payload": "object"})
  ns = {}
  exec(compile(src, "<wrapper>", "exec"), ns)  # noqa: S102
  assert ns["_mock_arg"]({"a": 1, "b": True, "c": None}) == '{"a": 1, "b": true, "c": null}'
  assert ns["_mock_arg"](["a", "b"]) == '["a", "b"]'
  # The scalar coercions the helper already made are untouched.
  assert ns["_mock_arg"](240.0) == "240"
  assert ns["_mock_arg"](True) == "true"
  assert ns["_mock_arg"](None) == ""


def _mock_with_defaults(account: str = "94040", unit: str = "F"):
  return {"headline": account + unit}


def _mock_with_a_required_param(account, unit: str = "F"):
  return {"headline": str(account) + unit}


def test_an_unsupplied_argument_leaves_the_mocks_own_default_standing():
  """Every parameter of a generated mock tool defaults to `''`, and the wrapper used to
  pass all of them unconditionally — so `def fake(account="94040")` was handed an empty
  string and the default the author wrote was silently overridden. A mock answering for
  the wrong input, with nothing in the transcript to say so."""
  from flows.authoring import toolset_common as _tc
  src = _tc.mock_tool_source(
      "t_mock", {"account": "account", "unit": "unit"}, _mock_with_defaults, "named")
  ns = {}
  exec(compile(src, "<mock>", "exec"), ns)  # noqa: S102
  assert ns["t_mock"]() == {"headline": "94040F"}          # both defaults stand
  assert ns["t_mock"](account="10001") == {"headline": "10001F"}


def test_a_parameter_the_mock_REQUIRES_is_passed_whatever_its_value():
  """Omitting it would be a TypeError in the sandbox, which is worse than an empty
  string. Only a DEFAULTED parameter may be left out."""
  from flows.authoring import toolset_common as _tc
  src = _tc.mock_tool_source(
      "t_mock", {"account": "account", "unit": "unit"},
      _mock_with_a_required_param, "named")
  ns = {}
  exec(compile(src, "<mock>", "exec"), ns)  # noqa: S102
  assert ns["t_mock"]() == {"headline": "F"}               # account passed as ''


# ── a DEFERRED poll is a wait, not a broken contract ─────────────────────────


def test_a_deferred_poll_is_not_read_as_a_service_that_broke_its_contract():
  """A status resource deployed `executionType: ASYNCHRONOUS` is a supported shape, and
  it answers the poll with a placeholder rather than a status.

  CES replies to a deferred call at once with `{"result": "pending"}` and delivers the
  real payload later, in a completion envelope. That reply carries no `status`, so the
  four-value check read it as a service that broke its contract and stamped the task's
  payload `remote_contract` / "unknown status: (absent)" — a verdict about the service,
  recorded on the turn the job was merely started. Observed on a live voice call whose
  job then ran and reported perfectly well; it survived only because `awaits` opened the
  wait a moment later, on top of a payload already marked as a failure.
  """
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  # What after_tool hands intake for a deferred call: the bare scalar, not a dict.
  sm = _intake(sm, "build_report__status", "pending")
  result = sm["task_results"]["BuildReport"]
  assert result.get("error_code") != "remote_contract", result
  assert "unknown status" not in str(result.get("error", "")), result


def test_a_deferred_poll_still_leaves_the_job_outstanding():
  """The placeholder must not fill a slot or complete the task either — the answer has
  not arrived, it has only been promised."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status", "pending")
  assert not sm["task_results"]["BuildReport"].get("success")
  assert "headline" not in sm["filled"]


def test_a_status_the_contract_does_not_name_is_STILL_a_contract_failure():
  """The A/B half. Tolerating the placeholder must not tolerate a real answer whose
  status is a word nobody agreed on — that one the agent genuinely cannot follow."""
  cfg = _app()
  sm = _intake(_sm(cfg), "build_report",
               {"success": True, "build_report__job": "job-77"})
  sm = _drive(cfg, sm)["sm"]
  sm = _intake(sm, "build_report__status", {"status": "havering", "headline": "Up 4%"})
  result = sm["task_results"]["BuildReport"]
  assert result["error_code"] == "remote_contract", result
  assert "havering" in result["error"], result
