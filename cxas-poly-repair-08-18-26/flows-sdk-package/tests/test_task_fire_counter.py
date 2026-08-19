"""`count_into`: an integer slot the engine bumps each time a task fires.

The condition grammar compares numbers (`gt`/`gte`) but nothing produced one — a latch
holds `"true"`, not a count — so "stop after three" had no declarative form and every
agent that needed one derived it in a callback, invisible to the validator and to every
offline oracle.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_task_fire_counter.py
"""

from __future__ import annotations

import pytest

import flows
from flows.engine import loader

TIPS = ("Rejoin", "Closer", "Toggle", "Reseat")


def _config(cap: int = 3):
  """FOUR tips sharing one counter with a cap of three, then an exhausted line.

  Four, not three, is what makes the cap provable: with exactly `cap` tips the run ends
  because the agent ran out of things to say, and a counter that did nothing at all would
  look identical. The fourth tip is the one the cap has to stop.

  This is also the shape the primitive exists for. A cap is rarely "one task N times" — a
  task stops re-firing once its out-slot fills — it is N different tasks that together may
  only happen so often. Counting each into the same slot is what a hook used to do by
  summing latches.
  """
  f = flows.Flow("tips", root_agent="Agent")
  f.add(flows.user_slot("account", ask="Account number?"),
        flows.result_slot("done", "Exhausted"))
  for name in TIPS:
    f.add(flows.result_slot(f"tip_{name.lower()}", name))
    # NOT terminal: a terminal completes the flow and the reset clears `filled`, so the
    # count would restart and the cap could never be reached.
    f.task(name, f"tip_{name.lower()}", [], f"tip_{name.lower()}", out_key="success",
           count_into="tips_given",
           condition={"slot": "tips_given", "lt": cap},
           then_say=f"Try {name}.")
  f.task("Exhausted", "hand_off", [], "done", out_key="success",
         condition={"slot": "tips_given", "gte": cap},
         then_say="That's everything I can try.")
  return f.to_config()


def _fired(config, action):
  """The task behind the tool call in an engine action, if it called one."""
  call = (action.get("action") or action).get("function_call") or {}
  return next((t["name"] for t in config["tasks"] if t.get("tool") == call.get("name")),
              None)


def _turn(config, sm, said):
  """One caller turn, answered to a standstill. Returns the tasks that fired.

  A turn is a CHAIN of fires, not one: a tool result re-invokes the engine, which may
  dispatch the next task in the same exchange. Looking only at the caller's own call sees
  roughly half the fires, and reads a working cap as a broken one.
  """
  fired = []
  for _ in range(12):
    name = _fired(config, loader.run_engine(config, sm, last_user_text=said,
                                            config_id="tips"))
    said = ""
    if not name:
      return fired
    fired.append(name)
    sm["task_results"][name] = {"success": True}
    sm["_task_just_completed"] = name
  raise AssertionError(f"the turn never settled, fired {fired}")


def test_the_engine_counts_each_fire_and_the_cap_becomes_a_condition():
  """Three of four tips, then the cap flips the branch — no callback anywhere."""
  config = _config(cap=3)
  sm = {"filled": {"account": "111"}, "task_results": {}}

  fired = []
  for _ in range(6):
    fired += _turn(config, sm, "still not working")
    if "Exhausted" in fired:
      break

  assert [n for n in fired if n != "Exhausted"] == ["Rejoin", "Closer", "Toggle"], fired
  assert "Reseat" not in fired, f"the cap did not stop the fourth tip: {fired}"
  assert fired[-1] == "Exhausted", fired
  assert int(sm["filled"]["tips_given"]) == 3


def test_the_count_is_absent_until_the_task_first_fires():
  """An unset counter must read as zero to a `lt`, or the first tip never fires."""
  config = _config(cap=3)
  sm = {"filled": {"account": "111"}, "task_results": {}}
  assert "tips_given" not in sm["filled"]

  assert _fired(config, loader.run_engine(config, sm, last_user_text="still not working",
                                          config_id="tips")) == "Rejoin"
  assert int(sm["filled"]["tips_given"]) == 1


def test_a_cap_of_zero_fires_nothing_it_guards():
  """The degenerate cap, pinned: `lt: 0` is false against an absent counter too.

  An off-by-one here would read the unset slot as "not yet counted, so allowed" and let
  exactly one tip through a cap that forbids all of them.
  """
  config = _config(cap=0)
  sm = {"filled": {"account": "111"}, "task_results": {}}
  fired = _turn(config, sm, "still not working")
  assert fired == ["Exhausted"], fired
  assert "tips_given" not in sm["filled"], "nothing fired, so nothing should be counted"


def test_only_the_task_that_asked_for_a_count_is_counted():
  """`Exhausted` fires without `count_into`, and must not move the counter."""
  config = _config(cap=3)
  sm = {"filled": {"account": "111"}, "task_results": {}}
  for _ in range(6):
    if "Exhausted" in _turn(config, sm, "still not working"):
      break
  assert int(sm["filled"]["tips_given"]) == 3


def test_a_second_writer_on_the_counter_is_refused_at_build_time():
  """The one way this primitive fails badly: a counter something else also fills.

  The engine reads the total with `int(...)`, so a spoken account number or another
  task's output in that slot raises at DISPATCH — deep inside a deployed agent, on a live
  call, surfacing as the platform's failure line. Refused while it is still a build.
  """
  spoken = flows.Flow("collide", root_agent="Agent")
  spoken.add(flows.user_slot("count", ask="How many?"),
             flows.result_slot("out", "T"))
  spoken.task("T", "do_it", [], "out", out_key="success", count_into="count")
  with pytest.raises(ValueError, match="the CALLER fills"):
    spoken.to_config()

  produced = flows.Flow("collide2", root_agent="Agent")
  produced.add(flows.result_slot("out", "T"), flows.result_slot("tally", "Other"))
  produced.task("T", "do_it", [], "out", out_key="success", count_into="tally")
  produced.task("Other", "other", [], "tally", out_key="success")
  with pytest.raises(ValueError, match="output slot of task 'Other'"):
    produced.to_config()


def test_the_second_writer_check_reads_a_multi_slot_output():
  """One out_key can name SEVERAL slots, and the check has to see every one of them.

  Written after the guard crashed on `{"router_serial": ["router_serial",
  "hardware_ref"]}` — the list form — which is both the collision it must catch and the
  shape it must not choke on.
  """
  f = flows.Flow("multi", root_agent="Agent")
  f.add(flows.result_slot("out", "T"), flows.event_slot("serial"),
        flows.event_slot("hardware_ref"))
  f.task("Other", "lookup", [], "out", out_key="success",
         extra_outputs={"serial": ["serial", "hardware_ref"]})
  f.task("T", "do_it", [], "out", out_key="success", count_into="hardware_ref")
  with pytest.raises(ValueError, match="output slot of task 'Other'"):
    f.to_config()


def test_declaring_the_counter_is_allowed():
  """Only a second WRITER is refused — declaring it is how you default or share it."""
  f = flows.Flow("declared", root_agent="Agent")
  f.add(flows.event_slot("tally"), flows.result_slot("out", "T"))
  f.task("T", "do_it", [], "out", out_key="success", count_into="tally")
  assert f.to_config()["tasks"][0]["count_into"] == "tally"


def test_a_task_without_count_into_writes_no_counter():
  """The key is emitted only when asked for, so an untouched config is unchanged."""
  f = flows.Flow("plain", root_agent="Agent")
  f.add(flows.result_slot("out", "Plain"))
  f.task("Plain", "do_it", [], "out", out_key="success")
  task = f.to_config()["tasks"][0]
  assert "count_into" not in task


def _hand_written(counter: str, spoken: bool):
  """A config as JSON, never built through the DSL — the path review asked about."""
  slot = ({"name": counter, "source": "user", "setter": f"set_{counter}",
           "ask": "How many?"} if spoken else
          {"name": "out2", "source": "task:Other"})
  return {
      "config_id": "raw",
      "root_agent": "Agent",
      "slots": [{"name": "out", "source": "task:T"}, slot],
      "tasks": [
          {"name": "T", "tool": "do_it", "inputs": [], "outputs": {"success": "out"},
           "count_into": counter},
          {"name": "Other", "tool": "other", "inputs": [],
           "outputs": {"success": counter if not spoken else "out2"}},
      ],
  }


def test_the_engine_validator_refuses_a_second_writer_in_hand_written_json():
  """The DSL guard is bypassable; deploying raw JSON must not be.

  `to_config()` only runs when the config was authored in Python. A hand-written or
  generated config with a counter something else fills reaches the engine, which reads
  the running total with `int(...)` and raises mid-dispatch on a live call — the caller
  hears the platform's failure line.
  """
  vdc = loader.load_validator()

  errors = vdc.validate_dag_config({"raw_config": _hand_written("count", spoken=True)})["errors"]
  assert any("count" in e and "CALLER" in e for e in errors), errors

  errors = vdc.validate_dag_config({"raw_config": _hand_written("tally", spoken=False)})["errors"]
  assert any("tally" in e and "output slot of" in e for e in errors), errors


def test_the_engine_validator_accepts_a_counter_with_one_writer():
  """The same config with the counter left to the engine passes."""
  vdc = loader.load_validator()

  config = _hand_written("tally", spoken=False)
  config["tasks"][1]["outputs"] = {"success": "out2"}
  errors = vdc.validate_dag_config({"raw_config": config})["errors"]
  assert not [e for e in errors if "tally" in e], errors
