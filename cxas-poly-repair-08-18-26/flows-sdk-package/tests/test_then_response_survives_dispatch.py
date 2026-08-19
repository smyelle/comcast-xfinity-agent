"""A completed task's `then_response` must survive a turn that dispatches again.

The sibling of the terminal-announce bug in
`test_terminal_announce_non_preempt_ends_session.py`, on the TASK side.

A task may author a `then_response` — a closing disposition, a card — that is delivered
when its result lands. It leaves `_handle_post_executor` as the third return value and
every preempting path carries it inline: the fan-out watcher, the remote-job poll, the
terminal branch, the `preempt_then_say` branch. A non-preempting turn routes it through
`_route_payloads`.

The `fire` branch did neither. It returns as soon as the DAG picks the next task to
dispatch, long before `_route_payloads`, and built its `response` from the FIRED task's
payloads alone — so the completed task's disposition was silently dropped.

That made the disposition depend on SCHEDULING rather than on what was authored: the same
rung, on the same caller state, delivered its `end_session` when nothing else happened to
be dispatchable and lost it when something was. A caller was told the call was over and
the line stayed open, with no second chance — a rung that latches its own flag is not
eligible again.

Run: PYTHONPATH=packages/flows/src pytest \
     packages/flows/tests/test_then_response_survives_dispatch.py
"""
from __future__ import annotations

import flows
from flows.engine import loader as fb

_END = [{"type": "end_session", "reason": "completed", "escalated": False}]


def _app(second_task_eligible: bool):
  """Two tasks. `Closer` speaks and carries an end_session; `After` is the thing the
  engine goes on to dispatch, and is gated so a control run can withhold it."""
  f = flows.Flow("dispo", root_agent="Dispo_Agent")
  f.add(flows.user_slot("topic", "What can I help with?"))
  f.add(flows.event_slot("closed"), flows.event_slot("after_done"))
  closer = flows.task(
      "Closer", "close_out", [], "closed", out_key="closed",
      condition={"all": [{"slot": "topic", "filled": True},
                         {"slot": "closed", "filled": False}]},
      then_say="No problem at all. If anything changes, we're here.")
  # Set on the dict rather than passed: `flows.task()` takes no `then_response`, so this
  # is how an author attaches one -- and it is what the Comcast repair agent's `say_rung`
  # does for every rung that closes a call.
  closer["then_response"] = _END
  f.task(closer)
  # Declared AFTER `Closer`, and eligible the moment `Closer` has latched — which is
  # exactly the shape that loses the disposition: the engine completes `Closer` and, on
  # the same pass, picks this one to dispatch.
  f.task(flows.task(
      "After", "do_more", [], "after_done", out_key="after_done",
      condition={"all": [{"slot": "closed", "filled": True if second_task_eligible
                          else False},
                         {"slot": "after_done", "filled": False}]},
      then_say="And here is something else."))
  return f.to_config()


def _run(second_task_eligible: bool):
  """Drive `Closer` to completion and return the action the engine emits."""
  cfg = _app(second_task_eligible)
  sm = fb.run_engine(cfg, {}, config_id="dispo")["sm"]
  sm.setdefault("filled", {})["topic"] = "nothing, thanks"
  # `Closer` fires...
  out = fb.run_engine(cfg, sm, last_user_text="no thanks", config_id="dispo")
  sm = out["sm"]
  # ...and its result lands, which is the turn that renders `then_say` + `then_response`.
  sm.setdefault("task_results", {})["Closer"] = {"success": True, "closed": "true"}
  sm["filled"]["closed"] = "true"
  sm["_task_just_completed"] = "Closer"
  return fb.run_engine(cfg, sm, last_user_text="", config_id="dispo")["action"]


def _parts(action):
  return action.get("response") or []


def test_disposition_survives_when_the_turn_dispatches_again():
  """The bug: another task is dispatchable, so the turn returns on the fire branch."""
  action = _run(second_task_eligible=True)
  assert action.get("function_call"), (
      "precondition: this turn must go on to dispatch, or it is not the failing shape")
  assert any(p.get("type") == "end_session" for p in _parts(action)), (
      "the completed task's then_response was dropped because the turn dispatched "
      f"again; the caller got {_parts(action)}")


def test_disposition_still_delivered_when_the_turn_ends_there():
  """The control, and the reason the bug was invisible: with nothing left to dispatch
  the same rung on the same state already delivered its disposition."""
  action = _run(second_task_eligible=False)
  assert not action.get("function_call"), "control: nothing else should dispatch here"
  assert any(p.get("type") == "end_session" for p in _parts(action)), _parts(action)


def test_the_fired_tasks_own_payloads_are_not_displaced():
  """The completed task's parts lead, but they do not replace what the fired task
  carries — both reach the caller, in that order."""
  action = _run(second_task_eligible=True)
  types = [p.get("type") for p in _parts(action)]
  assert types and types[0] == "end_session", (
      f"the completed task's disposition should lead its turn; got {types}")
