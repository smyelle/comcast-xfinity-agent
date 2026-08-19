"""Progressive fan-out: a line per leg, the moment that leg lands, inside one turn.

`parallel()` already fixed the DISPATCH half — the legs go out in one action and the
runtime runs them concurrently. The reporting half is what these cover: a synchronous
group hands the whole batch back after its slowest leg, so three checks of 8s/18s/30s
buy the caller half a minute of silence and then a wall of results.

The lowering keeps the authoring surface exactly as it was and changes what the group
compiles to: each leg becomes an ASYNCHRONOUS tool publishing to its own state key, the
emitter adds a peek/watch pair, and the engine narrates each finding as a PARTIAL preempt
that speaks without ending the turn.

WHAT THESE TESTS CANNOT SHOW. Offline, every leg answers synchronously and instantly, and
nothing here dispatches a real tool or plays audio. The two behaviours the feature rests
on — that CES runs N dispatched parts concurrently, and that a tool can poll another
through the injected `tools` global off the reasoning-pass budget — are faked by the
harness in both directions. So these pin the LOWERING (which action the engine returns,
which tools the emitter writes, which config the app ships) and the state machine around
it. They do not, and cannot, prove the caller hears three lines. That takes the voice
channel.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import flows  # noqa: E402
from flows.authoring import build  # noqa: E402
from flows.authoring import tools as _tools  # noqa: E402
from flows.emit import fanout  # noqa: E402
from flows.engine import loader as fb  # noqa: E402

PENDING = {"result": "pending"}

_LEGS = (
    ("inventory", "check_stock", "stock"),
    ("shipping", "check_shipping", "eta"),
    ("billing", "check_billing", "balance"),
)


def _config(group="diagnostics", say=True, all_done=None, awaits=None):
  """Three lookups that depend only on the order number, grouped (or not)."""
  f = flows.Flow("orders", root_agent="agent")
  f.add(flows.user_slot("order_id", ask="What is your order number?"))
  legs = []
  for name, tool, out in _LEGS:
    f.add(flows.result_slot(out, name))
    task = flows.task(name, tool=tool, inputs=["order_id"], out_slot=out,
                      then_say=(f"{name} says {{{out}}}." if say else None))
    if group:
      task["parallel"] = group
    if awaits and name == _LEGS[0][0]:
      task["awaits"] = dict(awaits)
    legs.append(task)
    f.task(task)
  if all_done and group:
    f.add(flows.event_slot(f"{group}_done"))
    f.add(flows.announce(f"{group}_all_done", [all_done],
                         requires=[f"{group}_done"], preempt=True))
  return flows.App(root_flow=f, app_display_name="t").root_flow.to_config()


def _sm(config, filled=None, task_results=None):
  sm = fb.seed_sm(config)
  sm["filled"] = dict(filled or {"order_id": "A-1042"})
  sm["pending"] = {}
  sm["_config_id"] = "orders"
  if task_results:
    sm["task_results"] = task_results
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "orders"
    sm["filled"][gate] = "orders"
  return sm


def _drive(config, sm):
  return fb.load_engine().slot_filling_engine({
      "raw_config": config,
      "sm": sm,
      "last_user_text": "",
      "scanned_user_text": "A-1042",
      "is_inactivity": False,
      "event_data": {},
      "config_id": "orders",
      "n_user_turns": 1,
  })


def _fired(action):
  calls = action.get("function_calls") or (
      [action["function_call"]] if action.get("function_call") else [])
  return [c["name"] for c in calls if c["name"] != "settle_guard"]


def _fire(config):
  """Drive the dispatch turn and hand back `(action, sm)`."""
  out = _drive(config, _sm(config))
  return out["action"], out["sm"]


def _all_pending(sm):
  """What `before_model` leaves behind when every leg answered `pending`.

  The callback drops the placeholder rather than handing it to intake — a placeholder
  recorded as a result routes the leg into its on_failure ladder, where max_retries
  defaults to 0, so the group would escalate the flow on its own first fire with
  nothing actually failed.
  """
  sm = dict(sm)
  sm["_fanout_pending"] = list((sm.get("_fanout") or {}).get("legs") or [])
  sm.pop("_parallel_firing", None)
  return sm


def _land(sm, *names, results=None):
  """What `before_model` leaves behind when `names` published to their state keys."""
  results = results or {
      "inventory": {"success": True, "stock": "2 in stock"},
      "shipping": {"success": True, "eta": "Friday"},
      "billing": {"success": True, "balance": "nothing owed"},
  }
  sm = dict(sm)
  task_results = dict(sm.get("task_results") or {})
  filled = dict(sm.get("filled") or {})
  by_out = {name: out for name, _tool, out in _LEGS}
  for name in names:
    task_results[name] = results[name]
    for key, value in results[name].items():
      if key != "success":
        filled[by_out[name]] = value
  sm["task_results"] = task_results
  sm["filled"] = filled
  sm["_completed_batch"] = list(names)
  fan = dict(sm.get("_fanout") or {})
  fan["done_legs"] = list(fan.get("done_legs") or []) + list(names)
  sm["_fanout"] = fan
  return sm


# ── The dispatch turn ────────────────────────────────────────────────────────


def test_every_eligible_leg_fires_in_one_action():
  """The dispatch half, unchanged by the lowering.

  Three legs, one action. Firing them one per pass is not merely slower to write down
  — each pass is a separate runtime re-invocation and the caller waits for all three in
  series.
  """
  action, _sm_out = _fire(_config())
  assert _fired(action) == ["check_stock", "check_shipping", "check_billing"]


def test_the_watcher_does_not_ride_out_with_the_legs():
  """The dispatch action is byte-identical to the pre-feature one.

  The watcher goes out on the NEXT pass, once the legs are genuinely running. Racing it
  out of the same preempt would have it poll a set of tools that had not started.
  """
  action, _ = _fire(_config())
  assert "diagnostics_watch" not in _fired(action)


def test_the_dispatch_clears_the_previous_runs_publications():
  """State outlives the group, so a re-fire would read the last run's result as this
  one's — instantly, and narrate a stale finding before the backend had been asked."""
  action, _ = _fire(_config())
  assert {"diagnostics_billing", "diagnostics_inventory", "diagnostics_shipping"
          }.issubset(set(action["state_writes"]["pop"]))


def test_a_config_with_no_parallel_group_produces_a_byte_identical_action():
  """Back-compat. Every app that declares no group must be untouched by this."""
  config = _config(group=None)
  action = _drive(config, _sm(config))["action"]
  assert action.get("function_calls") is None
  assert action.get("partial") is None
  assert action["function_call"]["name"] == "check_stock"
  # No leg state keys to clear, because there are no legs.
  assert not [k for k in (action.get("state_writes") or {}).get("pop", [])
              if k.startswith("diagnostics")]


def test_an_ungrouped_config_starts_no_fanout():
  """Nothing in the slot machine changes for an app with no group."""
  config = _config(group=None)
  assert "_fanout" not in _drive(config, _sm(config))["sm"]


# ── The watch loop ───────────────────────────────────────────────────────────


def test_pending_legs_are_watched_rather_than_re_dispatched():
  """A leg answering `pending` is running, not un-fired.

  Without the in-flight mark the selector finds no result for it and dispatches the
  whole group again on the very next pass — three more backends, forever.
  """
  config = _config()
  _action, sm = _fire(config)
  out = _drive(config, _all_pending(sm))
  assert out["action"]["function_call"]["name"] == "diagnostics_watch"
  assert sorted(out["sm"]["_awaiting_async"]) == ["billing", "inventory", "shipping"]


def test_each_legs_then_say_is_spoken_on_its_own_pass_not_only_the_last():
  """The regression the feature exists to fix.

  A synchronous group concatenates all three lines into one utterance after the
  slowest leg. Here each one is a turn's worth of speech on its own, while the others
  are still running.
  """
  config = _config()
  _action, sm = _fire(config)
  sm = _all_pending(sm)
  sm = _drive(config, sm)["sm"]

  first = _drive(config, _land(sm, "inventory"))
  assert first["action"]["message"] == "inventory says 2 in stock."
  second = _drive(config, _land(first["sm"], "shipping"))
  assert second["action"]["message"] == "shipping says Friday."


def test_a_narration_is_partial_so_the_floor_is_never_handed_back():
  """`partial` is what speaks the line without ending the turn.

  A full preempt would end it, and the legs still in flight would be abandoned — the
  caller would hear the first finding and never the other two.
  """
  config = _config()
  _action, sm = _fire(config)
  sm = _drive(config, _all_pending(sm))["sm"]
  action = _drive(config, _land(sm, "inventory"))["action"]
  assert action["partial"] is True
  assert action["function_call"]["name"] == "diagnostics_watch"


def test_a_narration_carries_the_next_watch_with_what_it_has_already_said():
  """`seen` is how the watcher knows not to report the same leg twice."""
  config = _config()
  _action, sm = _fire(config)
  sm = _drive(config, _all_pending(sm))["sm"]
  action = _drive(config, _land(sm, "inventory"))["action"]
  assert action["function_call"]["args"] == {"seen": "inventory"}


def test_an_empty_watch_is_re_dispatched_without_speaking():
  """A gap longer than one window costs a pass, not the group.

  The window is chunked because the deadline is per CALL, not per turn — so a watcher
  that saw nothing simply goes out again.
  """
  config = _config()
  _action, sm = _fire(config)
  sm = _drive(config, _all_pending(sm))["sm"]
  out = _drive(config, sm)
  assert out["action"]["function_call"]["name"] == "diagnostics_watch"
  assert not out["action"]["message"]
  assert out["action"].get("partial") is None


def test_the_group_is_written_off_after_too_many_empty_windows():
  """A wedged backend must not hold the floor forever.

  The stalled legs are recorded as FAILED rather than left outstanding: outstanding
  keeps them out of the selector and keeps `<group>_done` unfilled, so the flow would
  sit on them for the rest of the call.
  """
  config = _config()
  _action, sm = _fire(config)
  sm = _drive(config, _all_pending(sm))["sm"]
  for _ in range(fanout_max_empty_waits(config) + 1):
    sm = _drive(config, sm)["sm"]
  assert "_fanout" not in sm
  assert "_awaiting_async" not in sm
  assert all(sm["task_results"][n]["error"] == "fanout_no_result"
             for n, _t, _o in _LEGS)


def fanout_max_empty_waits(_config_unused):
  """The engine's own ladder length, read off the loaded module so the test cannot
  drift from it."""
  return fb.load_engine()._FANOUT_MAX_EMPTY_WAITS  # pylint: disable=protected-access


def test_the_last_leg_hands_the_turn_back_and_the_join_speaks_once():
  """`<group>_done` fills only when every leg has REPORTED, and the all-done line then
  closes the group — after the last finding, in the same breath, exactly once."""
  config = _config(all_done="That's everything.")
  _action, sm = _fire(config)
  sm = _drive(config, _all_pending(sm))["sm"]
  sm = _drive(config, _land(sm, "inventory"))["sm"]
  sm = _drive(config, _land(sm, "shipping"))["sm"]
  out = _drive(config, _land(sm, "billing"))
  assert out["sm"]["filled"]["diagnostics_done"] is True
  assert "_fanout" not in out["sm"]
  spoken = " ".join(
      [out["action"].get("message", "")]
      + [p.get("text", "") for p in (out["action"].get("response") or [])]).strip()
  assert spoken.count("That's everything.") == 1
  assert spoken.startswith("billing says nothing owed.")
  # The turn is over: no watcher, and the floor goes back to the caller.
  assert out["action"].get("partial") is None
  assert (out["action"].get("function_call") or {}).get("name") != "diagnostics_watch"


def test_a_failed_leg_releases_the_group_instead_of_holding_it_open():
  """Reported, not succeeded. A leg that fails is done with — left in flight it would
  hold `<group>_done` open for the rest of the call and the closing line would never
  speak, which is exactly the hostage-taking the legs were grouped to avoid."""
  config = _config(all_done="That's everything.")
  _action, sm = _fire(config)
  sm = _drive(config, _all_pending(sm))["sm"]
  failed = {"inventory": {"success": False},
            "shipping": {"success": True, "eta": "Friday"},
            "billing": {"success": True, "balance": "nothing owed"}}
  sm = _drive(config, _land(sm, "inventory", results=failed))["sm"]
  assert "inventory" not in (sm.get("_awaiting_async") or {})
  sm = _drive(config, _land(sm, "shipping", results=failed))["sm"]
  out = _drive(config, _land(sm, "billing", results=failed))
  assert out["sm"]["filled"]["diagnostics_done"] is True


def test_the_group_is_not_done_while_a_leg_is_outstanding():
  """Two of three back is not done, or the closing line speaks over live work."""
  config = _config(all_done="That's everything.")
  _action, sm = _fire(config)
  sm = _drive(config, _all_pending(sm))["sm"]
  out = _drive(config, _land(sm, "inventory", "shipping"))
  assert "diagnostics_done" not in out["sm"]["filled"]


def test_a_holding_line_survives_the_lowering():
  """`parallel(waiting_say=…)` lands as `awaits.say`, which the ordinary async path
  speaks from its pending turn. This path never takes that turn, so the line rides the
  first watch dispatch instead of being silently dropped by the lowering."""
  config = _config(awaits={"say": "This will take a moment.", "max_turns": 4})
  _action, sm = _fire(config)
  action = _drive(config, _all_pending(sm))["action"]
  assert action["message"] == "This will take a moment."
  assert action["partial"] is True


def test_the_holding_line_is_spoken_once():
  config = _config(awaits={"say": "This will take a moment.", "max_turns": 4})
  _action, sm = _fire(config)
  sm = _drive(config, _all_pending(sm))["sm"]
  assert not _drive(config, sm)["action"]["message"]


def _legs():
  return [flows.task(name, tool=tool, inputs=["order_id"], out_slot=out)
          for name, tool, out in _LEGS]


def _group_with(monkeypatch, **kw):
  """A group built through `parallel()` itself, so the LOWERING is what is under test.

  `_config` sets `awaits` straight onto a leg, which is the right fixture for engine
  behaviour and the wrong one here: it skips the argument being checked.

  Which legs are deferred is read from the `@tool(asynchronous=True)` REGISTRY, not from
  the leg dict, so the registry is what has to say so. Patched rather than decorated
  because the registry is module-global and a real decorator would leak these three tool
  names into every other test in the process.
  """
  monkeypatch.setattr(_tools, "registered_async_tools",
                      lambda: {tool for _n, tool, _o in _LEGS})
  return flows.parallel("diagnostics", tasks=_legs(), **kw)


def test_reassurance_reaches_the_first_leg_only(monkeypatch):
  """`while_waiting` is the alternative to a per-leg `then_say`: reassurance on the idle
  turns instead of a status line per finding, for a group whose leg names mean nothing
  to the caller.

  Only the FIRST deferred leg may carry it, for the same reason `waiting_say` may not be
  on all of them — every leg's line is produced on the same turn, so N legs would speak
  the same reassurance N times in one breath.
  """
  group = _group_with(monkeypatch, while_waiting=["Still going.", "Nearly there."])
  carried = [(leg.get("awaits") or {}).get("while_waiting") for leg in group.tasks]
  assert carried[0] == ["Still going.", "Nearly there."]
  assert not any(carried[1:]), (
      "more than one leg carries the reassurance, so the caller hears it once per leg")


def test_reassurance_and_the_opening_line_coexist(monkeypatch):
  """They cover different moments — `say` is the turn the wait starts, `while_waiting`
  the idle turns after it — so setting one must not displace the other."""
  group = _group_with(monkeypatch, waiting_say="One moment.", while_waiting=["Still going."])
  first = group.tasks[0]["awaits"]
  assert first["say"] == "One moment."
  assert first["while_waiting"] == ["Still going."]


def test_reassurance_without_a_deferred_leg_is_refused():
  """A `progressive=False` group hands the whole batch back after its slowest leg, so
  there are no idle turns to reassure ON. Accepting the argument would emit an app whose
  reassurance is simply never spoken, which is the silent kind of wrong.

  A PROGRESSIVE group is the opposite case and must be accepted: it lowers every leg to
  an asynchronous wrapper, so the legs wait whatever the decorator registry says. That
  distinction is the whole reason this knob was unusable on a converted agent."""
  try:
    flows.parallel("diagnostics", tasks=_legs(), progressive=False,
                   while_waiting=["Still going."])
  except ValueError as exc:
    assert "while_waiting" in str(exc)
  else:
    raise AssertionError("a group with no deferred leg accepted `while_waiting`")


def test_an_awaits_leg_is_still_lowered():
  """The silent-exclusion trap. `deadline`, `waiting_say` and `on_timeout` all merge
  into `awaits`, so an eligibility rule that excluded it would make the most natural
  way to write a slow group the one way to opt out of narrating it."""
  config = _config(awaits={"max_turns": 4})
  assert "diagnostics" in fanout.progressive_groups(config)
  _action, sm = _fire(config)
  assert (sm.get("_fanout") or {}).get("group") == "diagnostics"


# ── Tool surface ─────────────────────────────────────────────────────────────


def test_the_watcher_is_hidden_from_the_model_on_an_ordinary_turn():
  """A model call on the watcher spends a reasoning pass and a twenty-second window
  achieving nothing, and there are only ten passes."""
  config = _config()
  action = _drive(config, _sm(config, filled={}))["action"]
  assert "diagnostics_watch" in action["hide_tools"]
  assert "diagnostics_peek" in action["hide_tools"]


def test_the_turn_the_watcher_fires_does_not_hide_it():
  """A firing tool left in hide_tools renders empty — "having trouble"."""
  config = _config()
  _action, sm = _fire(config)
  action = _drive(config, _all_pending(sm))["action"]
  assert "diagnostics_watch" not in action["hide_tools"]
  assert "diagnostics_peek" in action["hide_tools"]


# ── The emitter ──────────────────────────────────────────────────────────────


def _emit(app, tmp_path):
  out = str(tmp_path / "app")
  build.emit(app, out)
  return out


def _example_app():
  f = flows.Flow("orders", root_agent="agent")
  f.add(flows.user_slot("order_id", ask="What is your order number?"))
  for name, tool, out in _LEGS:
    f.add(flows.result_slot(out, name))
  f.task(flows.parallel("diagnostics", tasks=[
      flows.task(name, tool=tool, inputs=["order_id"], out_slot=out,
                 then_say=f"{name} says {{{out}}}.")
      for name, tool, out in _LEGS
  ], all_done_say="That's everything."))
  return flows.App(root_flow=f, app_display_name="t")


def test_the_emitter_repoints_every_leg_at_a_publishing_wrapper(tmp_path, dag_config):
  """The leg the flow fires is the generated wrapper, not the author's tool — the
  wrapper is what writes the result where the watcher can see it."""
  out = _emit(_example_app(), tmp_path)
  src = open(os.path.join(  # pylint: disable=consider-using-with
      out, "tools/orders_dag/python_function/python_code.py")).read()
  wired = {t.get("tool") for t in dag_config(src, "orders")["tasks"]}
  for name, tool, _o in _LEGS:
    assert f"diagnostics_{name}_leg" in wired
    assert tool not in wired


def test_every_leg_wrapper_is_emitted_asynchronous(tmp_path):
  """A synchronous leg blocks its dispatch, so the runtime could not hand the
  framework back a pass to narrate on until every leg had finished."""
  out = _emit(_example_app(), tmp_path)
  for name, _t, _o in _LEGS:
    tool = f"diagnostics_{name}_leg"
    doc = json.load(open(os.path.join(  # pylint: disable=consider-using-with
        out, f"tools/{tool}/{tool}.json")))
    assert doc["executionType"] == "ASYNCHRONOUS"


def test_every_leg_publishes_to_its_own_state_key(tmp_path):
  """Never a shared object: concurrent writes to one structure lose all but the last,
  values included (ces-probes 37/38). Separate keys have no conflict to lose."""
  out = _emit(_example_app(), tmp_path)
  keys = set()
  for name, _t, _o in _LEGS:
    tool = f"diagnostics_{name}_leg"
    body = open(os.path.join(  # pylint: disable=consider-using-with
        out, f"tools/{tool}/python_function/python_code.py")).read()
    assert f'context.state["diagnostics_{name}"]' in body
    keys.add(f"diagnostics_{name}")
  assert len(keys) == len(_LEGS)


def test_the_wrapper_declares_named_parameters_never_kwargs(tmp_path):
  """CES derives a tool's schema from its signature and silently DROPS a tool that
  takes only `**kwargs` — which would make the leg a name resolving to nothing, the
  one failure with no symptom at all."""
  out = _emit(_example_app(), tmp_path)
  body = open(os.path.join(  # pylint: disable=consider-using-with
      out, "tools/diagnostics_inventory_leg/python_function/python_code.py")).read()
  assert "def diagnostics_inventory_leg(order_id: str = \"\") -> dict:" in body
  assert "**kwargs" not in body


def test_peek_is_a_separate_tool_from_watch(tmp_path):
  """A running tool body's view of state is frozen at the moment it started
  (ces-probes 61); only a FRESH invocation sees a new write (71). A watcher re-reading
  its own state could spin for a minute and see nothing."""
  out = _emit(_example_app(), tmp_path)
  peek = open(os.path.join(  # pylint: disable=consider-using-with
      out, "tools/diagnostics_peek/python_function/python_code.py")).read()
  watch = open(os.path.join(  # pylint: disable=consider-using-with
      out, "tools/diagnostics_watch/python_function/python_code.py")).read()
  assert "def diagnostics_peek(" in peek
  assert "def diagnostics_watch(" in watch
  assert "context.state" not in watch


def test_the_watcher_polls_through_the_injected_tools_global(tmp_path):
  """Load-bearing, not stylistic. Sub-calls made that way never enter the transcript
  and cost no reasoning pass (ces-probes 70); polling as ordinary tool calls would
  spend the ten-pass budget before a word was spoken."""
  out = _emit(_example_app(), tmp_path)
  watch = open(os.path.join(  # pylint: disable=consider-using-with
      out, "tools/diagnostics_watch/python_function/python_code.py")).read()
  assert 'globals()["tools"]' in watch
  assert "diagnostics_peek" in watch


def test_the_watch_window_is_chunked_under_the_per_call_deadline(tmp_path):
  """A single call is safe to ~29s and fails at or below 60s, while cumulative time in
  a turn is fine to at least 82s. So the watcher returns and is re-dispatched."""
  out = _emit(_example_app(), tmp_path)
  watch = open(os.path.join(  # pylint: disable=consider-using-with
      out, "tools/diagnostics_watch/python_function/python_code.py")).read()
  assert fanout.WATCH_WINDOW_SECONDS <= 29
  assert f"< {fanout.WATCH_WINDOW_SECONDS}" in watch


# CES kills a tool body at this many seconds unless its resource declares otherwise.
# Bisected live against the example app: 45/50/55/58 land, 60/70/90 do not, and raising
# the resource's `timeout` lets a 90s leg through. Nothing offline can see either half,
# which is why both are pinned here.
_DEFAULT_TOOL_TIMEOUT_SECONDS = 60


def test_the_long_leg_example_stays_under_the_default_tool_timeout():
  """`long_leg_fan_out.py` narrates a leg that outlives the per-call deadline. It stays
  under the 60s default the TOOL is killed at, and under the group's fixed ladder, because
  neither failure is visible offline — an over-long leg validates clean and then either
  never reports or is written off mid-flight."""
  src = open(os.path.join(  # pylint: disable=consider-using-with
      os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
      "examples/long_leg_fan_out.py")).read()
  landings = sorted(int(s) for s in re.findall(r"time\.sleep\((\d+)\)", src))
  assert landings == [5, 15, 50], "the example's documented timeline changed"
  # Past the per-call deadline the watcher lives under, which is the example's point,
  # and under the default the TOOL is killed at, which is why it declares no timeout.
  assert 29 < landings[-1] < _DEFAULT_TOOL_TIMEOUT_SECONDS


def test_a_legs_declared_timeout_reaches_the_generated_wrapper(tmp_path):
  """The wrapper is the resource CES enforces a timeout against, so an author's
  `@tool(timeout=…)` has to be copied onto it. Emitted on the author's tool alone it
  would sit on a resource nothing dispatches, and the leg would silently take the 60s
  default — indistinguishable from the platform ignoring the setting."""
  _tools.clear_registry()

  @flows.tool(flow="orders", timeout=150)
  def check_stock(order_id: str = "") -> dict:
    """Slow.

    Args:
      order_id: The order.

    Returns:
      The stock.
    """
    return {"success": True, "stock": "2"}

  @flows.tool(flow="orders")
  def check_shipping(order_id: str = "") -> dict:
    """Fast.

    Args:
      order_id: The order.

    Returns:
      The eta.
    """
    return {"success": True, "eta": "Friday"}

  out = _emit(_example_app(), tmp_path)
  slow = json.load(open(os.path.join(  # pylint: disable=consider-using-with
      out, "tools/diagnostics_inventory_leg/diagnostics_inventory_leg.json")))
  fast = json.load(open(os.path.join(  # pylint: disable=consider-using-with
      out, "tools/diagnostics_shipping_leg/diagnostics_shipping_leg.json")))
  assert slow.get("timeout") == "150s"
  # And only the leg that asked for one: an undeclared leg must stay byte-identical.
  assert "timeout" not in fast


def test_the_agent_lists_the_watcher_it_has_to_dispatch(tmp_path):
  """peek/watch are named by no task and no slot, so nothing scoping an agent's tools
  from its config can find them — and an agent that does not list the watcher cannot
  have it dispatched, silently."""
  out = _emit(_example_app(), tmp_path)
  agent = json.load(open(os.path.join(  # pylint: disable=consider-using-with
      out, "agents/agent/agent.json")))
  assert "diagnostics_watch" in agent["tools"]
  assert "diagnostics_peek" in agent["tools"]
  for name, _t, _o in _LEGS:
    assert f"diagnostics_{name}_leg" in agent["tools"]


def test_an_app_with_no_group_emits_nothing_extra(tmp_path):
  """Back-compat at the emitter: the lowering returns the same objects untouched."""
  f = flows.Flow("orders", root_agent="agent")
  f.add(flows.user_slot("order_id", ask="What is your order number?"))
  f.add(flows.result_slot("stock", "inventory"))
  f.task(flows.task("inventory", tool="check_stock", inputs=["order_id"],
                    out_slot="stock"))
  app = flows.App(root_flow=f, app_display_name="t")
  out = _emit(app, tmp_path)
  assert not [d for d in os.listdir(os.path.join(out, "tools"))
              if d.endswith(("_leg", "_peek", "_watch"))]


def test_the_marker_ends_its_line_so_the_wrapped_body_is_not_commented_out(tmp_path):
  """A literal `\\n` in the generator welded the body's first line onto the marker
  comment, so the wrapped tool's `from typing import ...` shipped commented out.

  Nothing offline noticed. The file still parses -- a longer comment is valid Python --
  and the annotations that would then raise `NameError` are only evaluated in the
  sandbox, live. So this asserts the shape of the emitted TEXT rather than that it
  compiles, because compiling is exactly what failed to discriminate.
  """
  out = _emit(_example_app(), tmp_path)
  tools = os.path.join(out, "tools")
  legs = [d for d in os.listdir(tools) if d.endswith("_leg")]
  assert legs, "the example app should emit leg tools"
  for leg in legs:
    path = os.path.join(tools, leg, "python_function", "python_code.py")
    with open(path, encoding="utf-8") as fh:
      lines = fh.read().splitlines()
    assert f"{fanout.MARKER} leg" in [ln.strip() for ln in lines], (
        f"{leg}: the marker must terminate its own line, or whatever follows it on that"
        " line is commented out")
    assert not [ln for ln in lines if ln.lstrip().startswith("#") and "\\n" in ln], (
        f"{leg}: a comment carries a literal backslash-n, so a generated newline was"
        " escaped and the lines after it were swallowed")


def test_lowering_does_not_rewrite_the_program_it_compiled():
  """The task dicts can be the author's own `Flow` objects. A build that left them
  repointed would make the second build of the same app a build of a different app."""
  app = _example_app()
  all_map, bodies, available = build._assemble(app)  # pylint: disable=protected-access
  before = [t.get("tool") for t in all_map["orders"]["tasks"]]
  fanout.apply(all_map, bodies, available)
  assert [t.get("tool") for t in all_map["orders"]["tasks"]] == before


# ── The deploy flag ──────────────────────────────────────────────────────────


def _pulled_and_built(tmp_path):
  live = tmp_path / "live"
  live.mkdir()
  (live / "app.json").write_text(json.dumps({"name": "live", "displayName": "x"}))
  built = tmp_path / "built"
  built.mkdir()
  (built / "app.json").write_text(json.dumps({"name": "built", "displayName": "x"}))
  return str(live), str(built)


def test_barge_in_awareness_is_off_unless_the_deploy_declares_it(tmp_path):
  """The MERGE writes nothing unless the deploy asks — which is all this asserts.

  It used to claim the caller could not interrupt an app without the flag. That reading
  came from probe `79` on flash-live and is withdrawn: `162` cuts an agent that has no
  `audioProcessingConfig` at all (29.16s interrupted against a 44.56s control). The flag
  decides whether the agent is TOLD, so an app without it is interrupted silently.
  """
  from flows.deploy.prep import merge_live_settings  # noqa: PLC0415

  live, built = _pulled_and_built(tmp_path)
  merge_live_settings(live, built, declared=[])
  app = json.loads(open(os.path.join(built, "app.json")).read())  # pylint: disable=consider-using-with
  assert "bargeInConfig" not in (app.get("audioProcessingConfig") or {})


def test_a_null_setting_on_the_live_target_does_not_crash_the_merge(tmp_path):
  """CES hands back `"audioProcessingConfig": null` for a setting nobody has touched,
  and the PRESERVE loop copies that null straight into the built app. `setdefault` only
  fills a MISSING key, so it returned None here and the next line raised — a deploy
  dying on the console's default rather than on anything the author wrote."""
  from flows.deploy.prep import merge_live_settings  # noqa: PLC0415

  live, built = _pulled_and_built(tmp_path)
  live_json = os.path.join(live, "app.json")
  payload = json.loads(open(live_json).read())  # pylint: disable=consider-using-with
  payload["audioProcessingConfig"] = None
  payload["loggingSettings"] = None
  with open(live_json, "w") as fh:
    json.dump(payload, fh)

  merge_live_settings(live, built, barge_in_awareness=True, inactivity_timeout="8s",
                      audio_bucket="gs://ops", declared=[])

  app = json.loads(open(os.path.join(built, "app.json")).read())  # pylint: disable=consider-using-with
  assert app["audioProcessingConfig"]["bargeInConfig"]["bargeInAwareness"] is True
  assert app["audioProcessingConfig"]["inactivityTimeout"] == "8s"
  assert app["loggingSettings"]["audioRecordingConfig"]["gcsBucket"] == "gs://ops"


def test_barge_in_awareness_is_written_when_the_deploy_asks_for_it(tmp_path):
  from flows.deploy.prep import merge_live_settings  # noqa: PLC0415

  live, built = _pulled_and_built(tmp_path)
  merge_live_settings(live, built, barge_in_awareness=True, declared=[])
  app = json.loads(open(os.path.join(built, "app.json")).read())  # pylint: disable=consider-using-with
  assert app["audioProcessingConfig"]["bargeInConfig"]["bargeInAwareness"] is True


# ── The validator rule ───────────────────────────────────────────────────────


def test_a_leg_naming_an_unregistered_tool_is_an_error():
  """Mandatory, not optional. A leg that resolves to no registered tool is SILENT AND
  FATAL — it survives neither a daemon thread nor a join, nothing is logged, and the
  turn simply dies mid-call (ces-probes 69)."""
  from flows.config import validation as sv  # noqa: PLC0415

  config = _config()
  ok, errors, _warnings = sv.raw_validate_single(
      config,
      available_tools=["check_stock", "check_shipping", "set_order_id",
                       "slot_filling_engine", "slot_intake", "orders_dag"],
      framework_root=build.FRAMEWORK_ROOT)
  assert not ok
  assert any("leg 'billing' calls tool 'check_billing'" in e
             and "silent and fatal" in e for e in errors)


def test_a_well_formed_group_passes_the_leg_rule():
  """The rule must not fire on the shape it exists to protect."""
  from flows.config import validation as sv  # noqa: PLC0415

  config = _config()
  _ok, errors, _warnings = sv.raw_validate_single(
      config,
      available_tools=["check_stock", "check_shipping", "check_billing",
                       "set_order_id", "slot_filling_engine", "slot_intake",
                       "orders_dag"],
      framework_root=build.FRAMEWORK_ROOT)
  assert not [e for e in errors if "silent and fatal" in e]


def test_a_progressive_group_takes_reassurance_without_a_decorated_tool():
  """The converted-agent case, and the reason the registry check was not enough.

  A grafted leg is a tool resource the SDK did not write, so no `@tool(asynchronous=True)`
  ever ran for it. Progressive lowering makes it asynchronous regardless — so refusing
  the knob because the registry is silent refuses it on exactly the apps that need it.
  """
  group = flows.parallel("diagnostics", tasks=_legs(), progressive=True,
                         waiting_say="One moment.",
                         while_waiting=["Still going."])
  first = group.tasks[0]["awaits"]
  assert first["say"] == "One moment."
  assert first["while_waiting"] == ["Still going."]
