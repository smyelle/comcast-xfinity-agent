"""Parallel fan-out: several independent tasks dispatched in one action.

The engine fires one task per pass. Three independent lookups therefore cost three
runtime re-invocations and, because each one blocks its turn, three lookups' worth of
the caller's time. Nothing about them depends on anything else — the DAG already says
so, since none of them consumes another's output.

A `parallel` group says "dispatch these together". The runtime then executes them
concurrently, so the group costs the caller its slowest leg rather than the sum
(measured: three four-second legs in four seconds, ces-probes 33).

These tests drive the real engine through the offline loader, like `test_ask_ladder.py`.
They assert on the action the engine returns, because that action IS the dispatch — the
parts assembly in `before_model` reads `function_calls` straight off it.
"""

import flows
from flows.engine import loader as fb

_LEGS = (
    ("inventory", "check_stock", "stock"),
    ("shipping", "check_shipping", "eta"),
    ("billing", "check_billing", "balance"),
)


def _config(group="diagnostics", conditions=None, say=False):
  """A flow whose three lookups all depend only on the order number."""
  conditions = conditions or {}
  f = flows.Flow("orders", root_agent="agent")
  f.add(flows.user_slot("order_id", ask="What is your order number?"))
  for name, tool, out in _LEGS:
    f.add(flows.result_slot(out, name))
    task = flows.task(name, tool=tool, inputs=["order_id"], out_slot=out,
                      condition=conditions.get(name),
                      then_say=(f"{name} says {{{out}}}." if say else None))
    if group:
      task["parallel"] = group
    f.task(task)
  return flows.App(root_flow=f, app_display_name="t").root_flow.to_config()


def _sm(config, filled=None, task_results=None):
  sm = fb.seed_sm(config)
  sm["filled"] = dict(filled or {"order_id": "A-1042"})
  sm["pending"] = {}
  # The engine reads a `_config_id` that differs from the config it is running as a
  # cross-flow switch and wipes `task_results` (engine `_run_slot_filling`). Pinning it
  # is what makes a pre-completed task stay completed here; without it a seeded result
  # silently vanishes and the task looks eligible again.
  sm["_config_id"] = "orders"
  if task_results:
    sm["task_results"] = task_results
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "orders"
    sm["filled"][gate] = "orders"
  return sm


def _drive(config, sm):
  """The full engine result, so a test can assert on the slot machine too."""
  engine = fb.load_engine()
  return engine.slot_filling_engine({
      "raw_config": config,
      "sm": sm,
      "last_user_text": "A-1042",
      "scanned_user_text": "A-1042",
      "is_inactivity": False,
      "event_data": {},
      "config_id": "orders",
      "n_user_turns": 1,
  })


def _action(config, sm):
  return _drive(config, sm)["action"]


def _fired(action):
  """The tool names this action dispatches, in order.

  `settle_guard` is filtered out: it is the framework holding the turn open for the
  deferred legs, not a leg, and these tests are about which LEGS were dispatched. That it
  IS dispatched is pinned separately, in test_settle_guard.py.
  """
  calls = action.get("function_calls") or (
      [action["function_call"]] if action.get("function_call") else [])
  return [c["name"] for c in calls if c["name"] != "settle_guard"]


def test_a_group_fires_every_eligible_leg_in_one_action():
  """The point of the feature. Three legs, one dispatch, one turn.

  Firing them one per pass is not merely slower to write down — each pass is a
  separate runtime re-invocation, and the caller waits for all three in series.
  """
  action = _action(*(lambda c: (c, _sm(c)))(_config()))
  assert _fired(action) == ["check_stock", "check_shipping", "check_billing"]


def test_each_leg_is_called_with_its_own_arguments():
  """A group is not one call with merged arguments; each leg keeps its own."""
  action = _action(*(lambda c: (c, _sm(c)))(_config()))
  # The guard is dropped for the same reason `_fired` drops it: it is not a leg and
  # carries no arguments of its own.
  assert [c["args"] for c in action["function_calls"]
          if c["name"] != "settle_guard"] == [
      {"order_id": "A-1042"}] * 3


def test_a_config_with_no_group_produces_a_byte_identical_action():
  """Back-compat. Every app that declares no group must be untouched by this.

  `function_calls` is emitted only for a real group, so an ungrouped config cannot
  tell the difference between this engine and the one before it.
  """
  config = _config(group=None)
  action = _action(config, _sm(config))
  assert action.get("function_calls") is None
  assert action["function_call"]["name"] == "check_stock"


def test_the_singular_function_call_still_names_the_first_leg():
  """Everything downstream that predates fan-out reads the singular key.

  The improvisation livelock guard, the preempt gate, the offline simulator and the
  docs transcript driver all check `function_call`. Leaving it set to the first leg is
  what lets them keep working without knowing groups exist.
  """
  action = _action(*(lambda c: (c, _sm(c)))(_config()))
  assert action["function_call"] == action["function_calls"][0]


def test_a_leg_gated_off_by_its_condition_is_not_dispatched():
  """A group is a batching hint, not a barrier.

  Holding the whole group until every leg is eligible would let one permanently
  ungated leg wedge the others forever.
  """
  config = _config(conditions={"shipping": flows.has("never_filled")})
  action = _action(config, _sm(config))
  assert _fired(action) == ["check_stock", "check_billing"]


def test_a_leg_already_complete_is_not_redispatched():
  """A leg that has already reported drops out; the rest still go together."""
  config = _config()
  sm = _sm(config, task_results={"inventory": {"success": True, "stock": "2"}})
  assert _fired(_action(config, sm)) == ["check_shipping", "check_billing"]


def test_a_lone_surviving_leg_fires_as_an_ordinary_single_call():
  """With one leg left there is no fan-out, so no plural key is emitted."""
  config = _config()
  sm = _sm(config, task_results={"inventory": {"success": True, "stock": "2"},
                                 "shipping": {"success": True, "eta": "Friday"}})
  action = _action(config, sm)
  assert action.get("function_calls") is None
  assert action["function_call"]["name"] == "check_billing"


def test_the_firing_turn_hides_no_leg_of_the_group():
  """A hidden firing tool renders empty, so exempting only the first leg would take
  the other two down with it."""
  config = _config()
  action = _action(config, _sm(config))
  hidden = set(action.get("hide_tools") or [])
  assert hidden.isdisjoint({"check_stock", "check_shipping", "check_billing"})


_RESULTS = {
    "inventory": {"success": True, "stock": "2 in stock"},
    "shipping": {"success": True, "eta": "Friday"},
    "billing": {"success": True, "balance": "nothing owed"},
}


def _landed(config, results=None, batch=None):
  """The slot machine as it looks once a group's legs have been ingested.

  `before_model` records the batch: it runs once per pass rather than once per leg, so
  it is the only writer, which is what keeps concurrent legs from losing each other's
  results (ces-probes 37/38). This mirrors what it leaves behind.
  """
  results = _RESULTS if results is None else results
  sm = _sm(config, task_results=dict(results))
  for name, res in results.items():
    for key, value in res.items():
      if key != "success":
        sm["filled"][key] = value
  sm["_completed_batch"] = list(batch or results)
  return sm


def test_every_legs_result_gets_its_own_disposition_not_just_the_last():
  """The regression the feature exists to fix.

  The completed task is recorded in a SCALAR, so before this every leg but one was
  ingested and then silently skipped when it came to speaking.
  """
  config = _config(say=True)
  spoken = _action(config, _landed(config))["message"]
  assert spoken == ("inventory says 2 in stock. shipping says Friday."
                    " billing says nothing owed.")


def test_the_legs_speak_in_declaration_order_not_arrival_order():
  """Arrival order is unstable and unobservable, so it is never acted on.

  The legs run concurrently and come back in whatever order they finish; the batch is
  reordered against the config so the wording is identical every run.
  """
  config = _config(say=True)
  sm = _landed(config, batch=["billing", "inventory", "shipping"])
  assert _action(config, sm)["message"].startswith("inventory says")


def test_a_group_is_done_once_every_leg_has_reported():
  """`<group>_done` is what an all-done line waits on."""
  config = _config()
  out = _drive(config, _landed(config))
  assert out["sm"]["filled"]["diagnostics_done"] is True


def test_a_group_whose_legs_can_never_run_stays_open_and_stays_quiet():
  """A group where NO leg is eligible is not done, it never started — and must not say so.

  `<group>_done` exists to gate ONE thing: the `all_done_say` line. Closing a group that
  ran nothing would make the agent announce "all checks are complete" having performed
  no check, which is the live defect this whole change exists to fix (ces-probes 86,
  where a flow said both checks were done before the caller had given an account
  number). Silence is the correct outcome here, so it is pinned.

  The cost is real and accepted: anything an author hand-gates on `<group>_done` waits
  for a group that will never close. There is no answer that serves both — "did the
  group finish?" has no true answer when it never began — and of the two, a wrong
  spoken claim is worse than a gate that does not open.

  Note the group still closes normally when ANY leg runs, even if the rest are gated
  off; that case is covered below and is the one that occurs in practice.
  """
  conditions = {name: {"slot": "order_id", "eq": "NEVER"} for name, _, _ in _LEGS}
  config = _config(conditions=conditions)
  out = _drive(config, _sm(config))
  assert "diagnostics_done" not in out["sm"]["filled"]


def test_a_group_closes_on_the_legs_that_did_run():
  """One eligible leg reporting closes the group; permanently gated legs do not hold it.

  This is the shape the guard above must not break — a group is held only while nothing
  at all has happened, not while some legs are inapplicable.
  """
  live, gated = _LEGS[0], _LEGS[1:]
  conditions = {name: {"slot": "order_id", "eq": "NEVER"} for name, _, _ in gated}
  config = _config(conditions=conditions)
  sm = _sm(config, task_results={live[0]: {"success": True, live[2]: "x"}})
  sm["filled"][live[2]] = "x"
  assert _drive(config, sm)["sm"]["filled"]["diagnostics_done"] is True


def test_a_group_is_not_done_while_a_leg_is_outstanding():
  """Two of three back is not done, or the closing line speaks over live work."""
  config = _config()
  partial = {k: v for k, v in _RESULTS.items() if k != "billing"}
  out = _drive(config, _landed(config, results=partial))
  assert "diagnostics_done" not in out["sm"]["filled"]


def test_a_failed_leg_still_counts_as_reported():
  """Done means reported, not succeeded.

  Gating the group on success would let one flaky backend hold the other two results
  hostage, which is the opposite of why the legs were grouped.
  """
  config = _config()
  results = dict(_RESULTS, billing={"success": False})
  out = _drive(config, _landed(config, results=results))
  assert out["sm"]["filled"]["diagnostics_done"] is True


def test_a_failed_leg_does_not_silence_its_siblings():
  """The other two legs still say their lines."""
  config = _config(say=True)
  results = dict(_RESULTS, billing={"success": False})
  spoken = _action(config, _landed(config, results=results))["message"]
  assert "inventory says 2 in stock." in spoken
  assert "shipping says Friday." in spoken


def test_an_ungrouped_task_keeps_the_single_disposition_path():
  """Back-compat: no batch key, so the scalar path runs exactly as before."""
  config = _config(group=None, say=True)
  sm = _sm(config, task_results={"inventory": _RESULTS["inventory"]})
  sm["filled"]["stock"] = "2 in stock"
  sm["_task_just_completed"] = "inventory"
  assert _action(config, sm)["message"] == "inventory says 2 in stock."


def test_legs_of_different_groups_do_not_fire_together():
  """Group membership is by name; two groups are two dispatches."""
  f = flows.Flow("orders", root_agent="agent")
  f.add(flows.user_slot("order_id", ask="What is your order number?"))
  for name, tool, out, group in (
      ("inventory", "check_stock", "stock", "goods"),
      ("shipping", "check_shipping", "eta", "goods"),
      ("billing", "check_billing", "balance", "money"),
  ):
    f.add(flows.result_slot(out, name))
    task = flows.task(name, tool=tool, inputs=["order_id"], out_slot=out)
    task["parallel"] = group
    f.task(task)
  config = flows.App(root_flow=f, app_display_name="t").root_flow.to_config()
  assert _fired(_action(config, _sm(config))) == ["check_stock", "check_shipping"]


def _bad(mutate):
  """Build a two-leg group, let the caller break it, and return the errors."""
  f = flows.Flow("orders", root_agent="agent")
  f.add(flows.user_slot("q", ask="Q?"))
  f.add(flows.result_slot("s1", "a1"))
  f.add(flows.result_slot("s2", "a2"))
  one = flows.task("a1", tool="t1", inputs=["q"], out_slot="s1", parallel="g")
  two = flows.task("a2", tool="t2", inputs=["q"], out_slot="s2", parallel="g")
  mutate(one, two)
  f.task(one)
  f.task(two)
  errors, _ = flows.validate_app(flows.App(root_flow=f, app_display_name="t"))
  return " ".join(errors)


def test_two_legs_may_not_share_a_tool():
  """The batch comes back keyed by tool name, so two calls to one tool are the
  same call as far as anything downstream can tell."""
  assert "both call tool 't1'" in _bad(lambda a, b: b.__setitem__("tool", "t1"))


def test_a_terminal_leg_is_an_authoring_error():
  """A terminal fire tears the flow down under its own siblings."""
  assert "is terminal and a leg" in _bad(lambda a, b: b.__setitem__("terminal", True))


def test_a_leg_may_not_consume_a_siblings_output():
  """They are dispatched together, so the value is not filled when it fires."""
  assert "of the same group produces" in _bad(
      lambda a, b: b.__setitem__("inputs", ["s1"]))


def test_two_legs_may_not_both_carry_a_filler():
  """One turn, so the caller would hear the holding line twice."""
  assert "both declare 'filler_say'" in _bad(
      lambda a, b: (a.__setitem__("filler_say", "one moment"),
                    b.__setitem__("filler_say", "just a sec")))


def test_a_well_formed_group_validates_clean():
  """The checks must not fire on the shape they exist to protect."""
  assert _bad(lambda a, b: None) == ""


def test_a_group_round_trips_through_render():
  """Config -> source -> config, with the group reproduced idiomatically."""
  from flows.authoring import render
  config = _config()
  source = render.render_config_source(config, config_id="orders")
  assert 'parallel="diagnostics"' in source
