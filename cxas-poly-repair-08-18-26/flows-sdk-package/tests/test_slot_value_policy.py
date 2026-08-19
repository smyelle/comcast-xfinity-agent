"""Absence is not neutral, and a sentinel is not a value.

A flow that arbitrates between branches compares slots. An unset slot compares the
same as a resolved benign one, so a low-priority branch can win a comparison it should
have lost and the flow speaks a conclusion drawn from half a picture. Worse, an
upstream that pre-sets a status to a placeholder ("the backend has not replied yet")
hands the flow a third state it has no vocabulary for: present, non-empty, and not an
answer — which matches no branch at all, so the flow falls through to the model and
the failure reads as a modelling problem rather than a value that was never real.

`reject` and `default` give both states a name, and they compose: a rejected value is
cleared, so the default then applies. `publish` closes the matching gap on the way out
— the engine reasons over `filled` while a carried or legacy tool reads session state,
and nothing bound the two, so every migration hand-wrote the mirror.

Each of these replaced hand-written callback code in a live agent, and the tests below
are written against the cases that code existed to handle.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_slot_value_policy.py
"""

from __future__ import annotations

import pytest

import flows
from flows.engine import loader as fb


def _cfg(slots, tasks=None):
  return {"slots": slots, "tasks": tasks or [], "single_flow": True}


def _run(config, sm=None, text=""):
  return fb.run_engine(config, {} if sm is None else sm, last_user_text=text,
                       config_id="t", n_user_turns=1)


def _filled(config, sm=None):
  return _run(config, sm)["sm"].get("filled", {})


def _writes(result):
  return (result["action"].get("state_writes") or {}).get("set", {})


# ---------------------------------------------------------------------------
# default — naming what "the producer said nothing" means
# ---------------------------------------------------------------------------


def test_a_default_fills_a_slot_no_producer_supplied():
  assert _filled(_cfg([flows.event_slot("wifi_status", default="skipped")])) == {
      "wifi_status": "skipped"}


def test_a_real_value_is_never_overwritten_by_a_default():
  config = _cfg([flows.event_slot("wifi_status", default="skipped")])
  assert _filled(config, {"filled": {"wifi_status": "clear"}})["wifi_status"] == "clear"


def test_a_conditional_default_applies_only_in_the_state_it_names():
  """The advisory line only makes sense during an outage; anywhere else its absence
  is correct, and filling it would put a sentence in the flow's mouth."""
  config = _cfg([
      flows.event_slot("outage_status", default="skipped"),
      flows.event_slot("outage_message", default=[
          flows.fallback("An outage in your area is affecting service.",
                         when=flows.eq("outage_status", "active"))]),
  ])
  assert "outage_message" in _filled(config, {"filled": {"outage_status": "active"}})
  assert "outage_message" not in _filled(config)


def test_fallbacks_are_ordered_and_a_bare_value_is_the_last_resort():
  config = _cfg([
      flows.event_slot("network_status", default="skipped"),
      flows.event_slot("technician_type", default=[
          flows.fallback("network_tech", when=flows.eq("network_status", "impaired")),
          "unknown",
      ]),
  ])
  assert _filled(config, {"filled": {"network_status": "impaired"}})[
      "technician_type"] == "network_tech"
  assert _filled(config)["technician_type"] == "unknown"


def test_a_user_slot_is_never_defaulted():
  """Defaulting a question is a question never asked. `validation.on_exhaust.fill` is
  the affordance for resolving one without an answer, and it fires only after the
  caller has actually been given the chance."""
  slot = dict(flows.user_slot("account_number", ask="Account number?"),
              default=[{"value": "X"}])
  assert "account_number" not in _filled(_cfg([slot]))


def test_a_default_is_visible_to_a_later_default_condition():
  """Defaults resolve in slot order, so one can gate on another — which is what makes
  a chain of derived fallbacks expressible at all."""
  config = _cfg([
      flows.event_slot("network_status", default="impaired"),
      flows.event_slot("technician_type", default=[
          flows.fallback("network_tech", when=flows.eq("network_status", "impaired"))]),
  ])
  assert _filled(config)["technician_type"] == "network_tech"


# ---------------------------------------------------------------------------
# reject — the third state
# ---------------------------------------------------------------------------


def test_a_rejected_sentinel_is_cleared_and_the_default_applies():
  config = _cfg([flows.event_slot("outage_status", reject=["PENDING_BACKEND_RESULT"],
                                  default="skipped")])
  got = _filled(config, {"filled": {"outage_status": "PENDING_BACKEND_RESULT"}})
  assert got["outage_status"] == "skipped"


def test_a_real_value_survives_a_reject_list():
  config = _cfg([flows.event_slot("outage_status", reject=["PENDING_BACKEND_RESULT"],
                                  default="skipped")])
  assert _filled(config, {"filled": {"outage_status": "active"}})[
      "outage_status"] == "active"


def test_reject_without_a_default_leaves_the_slot_absent():
  """Rejecting says "this was never an answer" — it does not invent one."""
  config = _cfg([flows.event_slot("s", reject=["NOPE"])])
  assert "s" not in _filled(config, {"filled": {"s": "NOPE"}})


def test_reject_ignores_surrounding_whitespace():
  config = _cfg([flows.event_slot("s", reject=["NOPE"])])
  assert "s" not in _filled(config, {"filled": {"s": "  NOPE  "}})


# ---------------------------------------------------------------------------
# publish — the way out
# ---------------------------------------------------------------------------


def test_publish_mirrors_a_slot_to_every_named_variable():
  config = _cfg([flows.event_slot("accountNumber", default="A-1",
                                  publish=["accountNumber", "account_id"])])
  assert _writes(_run(config)) == {"accountNumber": "A-1", "account_id": "A-1"}


def test_the_mirror_is_restated_every_turn():
  """Not diffed. A diff is a cache of a value the engine cannot see — anything else
  writing the variable makes the cache wrong, and the engine then skips the very
  write that would have corrected it."""
  config = _cfg([flows.event_slot("a", default="1", publish=["a_out"])])
  first = _run(config)
  assert _writes(first) == {"a_out": "1"}
  sm = first["sm"]
  assert _writes(_run(config, sm)) == {"a_out": "1"}
  sm["filled"]["a"] = "2"
  assert _writes(_run(config, sm)) == {"a_out": "2"}


def test_publish_accepts_a_bare_name():
  config = _cfg([flows.event_slot("a", default="1", publish="a_out")])
  assert _writes(_run(config)) == {"a_out": "1"}


# ---------------------------------------------------------------------------
# The composition, and the shape of an app that uses none of it
# ---------------------------------------------------------------------------


def test_reject_then_default_then_publish_compose_in_one_slot():
  config = _cfg([flows.event_slot("status", reject=["PENDING"], default="skipped",
                                  publish=["status_out"])])
  result = _run(config, {"filled": {"status": "PENDING"}})
  assert result["sm"]["filled"]["status"] == "skipped"
  assert _writes(result) == {"status_out": "skipped"}


def test_an_app_declaring_no_policy_is_unchanged():
  result = _run(_cfg([flows.event_slot("plain")]))
  assert result["sm"].get("filled", {}) == {}
  assert _writes(result) == {}


def test_a_slot_builder_without_policy_emits_what_it_always_did():
  assert flows.event_slot("s") == {"name": "s", "source": "event", "event_key": "s"}
  assert flows.result_slot("s", "t") == {"name": "s", "source": "task:t"}


# ---------------------------------------------------------------------------
# Task results: one key to several slots, and the envelopes CES really sends
# ---------------------------------------------------------------------------


def _intake(config, response):
  """Run the engine once so it stashes its task map, then apply a tool result."""
  sm = _run(config)["sm"]
  return fb.run_intake("sweep_tool", response, sm)["sm"].get("filled", {})


_SWEEP = _cfg(
    [flows.event_slot("cable_modem_mac"), flows.event_slot("device_id"),
     flows.event_slot("account_status")],
    [{"name": "sweep", "tool": "sweep_tool", "inputs": [],
      "outputs": {"cable_modem_mac": ["cable_modem_mac", "device_id"],
                  "account_status": "account_status"}}],
)


def test_one_result_key_can_fill_several_slots():
  """One backend field under two slot names. Declaring a second key no tool returns
  would map nothing, because intake skips an absent key."""
  got = _intake(_SWEEP, {"success": True, "cable_modem_mac": "AA:BB",
                         "account_status": "clear"})
  assert got["cable_modem_mac"] == "AA:BB"
  assert got["device_id"] == "AA:BB"


@pytest.mark.parametrize("response,label", [
    ({"success": True, "account_status": "clear"}, "a plain dict"),
    ({"result": {"success": True, "account_status": "clear"}}, "one result envelope"),
    ({"result": '{"success": true, "account_status": "clear"}'}, "a stringified one"),
    ('{"success": true, "account_status": "clear"}', "a bare JSON string"),
])
def test_the_envelope_shapes_ces_really_sends_all_map(response, label):
  """after_tool peels exactly one envelope and never parses a body, so a tool that
  wraps its own payload or serializes it maps NOTHING — and because the task already
  succeeded there is no failure to escalate and no slot to ask for."""
  assert _intake(_SWEEP, response).get("account_status") == "clear", label


def test_a_payload_carrying_its_own_result_field_is_not_peeled():
  """`result` is only an envelope when it is the sole key; otherwise it is data."""
  got = _intake(_SWEEP, {"success": True, "result": "ok", "account_status": "clear"})
  assert got.get("account_status") == "clear"


def test_a_scalar_response_still_does_not_crash_intake():
  assert _intake(_SWEEP, "pending") == {}


def test_a_default_no_branch_accepts_is_warned_about():
  """A default turns a visible hole into a complete-looking picture that matches
  nothing, and the flow then has no branch to fire and no question to ask. Cheap to
  see at build time; expensive to see live, where it surfaces as a reasoning-loop cap."""
  f = flows.Flow("t", root_agent="A")
  f.add(flows.event_slot("status", default="skipped"),
        flows.user_slot("q", ask="Q?"),
        flows.event_slot("done"))
  f.task("Act", "act_tool", [], "done", out_key="success",
         condition={"slot": "status", "eq": "clear"})
  _, warnings = flows.validate_app(
      flows.App(root_flow=f, app_display_name="x", tool_bodies={
          "act_tool": "def act_tool() -> dict:\n  return {'success': True}\n"}))
  assert any("none of the branches reading it accept" in w for w in warnings), warnings


def test_a_default_a_branch_does_accept_is_quiet():
  f = flows.Flow("t", root_agent="A")
  f.add(flows.event_slot("status", default="skipped"),
        flows.user_slot("q", ask="Q?"),
        flows.event_slot("done"))
  f.task("Act", "act_tool", [], "done", out_key="success",
         condition={"slot": "status", "in": ["clear", "skipped"]})
  _, warnings = flows.validate_app(
      flows.App(root_flow=f, app_display_name="x", tool_bodies={
          "act_tool": "def act_tool() -> dict:\n  return {'success': True}\n"}))
  assert not [w for w in warnings if "none of the branches" in w], warnings


def test_defaulted_slots_are_marked_so_a_trace_can_tell_them_apart():
  config = _cfg([flows.event_slot("a", default="x"), flows.event_slot("b")])
  assert _run(config)["sm"].get("_defaulted") == ["a"]


# ---------------------------------------------------------------------------
# since() — "and not on the turn it appeared"
# ---------------------------------------------------------------------------


def _gated(cond):
  return {"slots": [flows.event_slot("offered"), flows.event_slot("done")],
          "tasks": [{"name": "Act", "tool": "act", "inputs": [],
                     "outputs": {"success": "done"},
                     "condition": {"all": [cond, {"slot": "done", "filled": False}]}}],
          "single_flow": True}


def _fires(config, sm, turn):
  r = fb.run_engine(config, sm, config_id="t", n_user_turns=turn)
  return (r["action"].get("function_call") or {}).get("name"), r["sm"]


def test_since_is_inert_on_the_turn_the_slot_was_filled():
  """A branch that OFFERS something latches its slot as it speaks, so a plain `filled`
  test is satisfiable on that same turn — and the model, holding both the question and
  the tool to answer it, answers itself and the caller is never asked."""
  fired, _ = _fires(_gated(flows.since("offered")), {"filled": {"offered": True}}, 1)
  assert fired is None


def test_since_is_live_on_the_next_turn():
  config = _gated(flows.since("offered"))
  _, sm = _fires(config, {"filled": {"offered": True}}, 1)
  fired, _ = _fires(config, sm, 2)
  assert fired == "act"


def test_plain_filled_would_have_fired_immediately():
  """The contrast that makes the primitive worth having."""
  fired, _ = _fires(_gated({"slot": "offered", "filled": True}),
                    {"filled": {"offered": True}}, 1)
  assert fired == "act"


def test_since_waits_the_number_of_turns_asked_for():
  config = _gated(flows.since("offered", turns=2))
  _, sm = _fires(config, {"filled": {"offered": True}}, 1)
  assert _fires(config, sm, 2)[0] is None
  assert _fires(config, sm, 3)[0] == "act"


def test_a_cleared_slot_forgets_its_stamp():
  """Re-offering must restart the clock, or the second offer is answerable at once."""
  config = _gated(flows.since("offered"))
  _, sm = _fires(config, {"filled": {"offered": True}}, 1)
  sm["filled"].pop("offered")
  _, sm = _fires(config, sm, 2)
  sm["filled"]["offered"] = True
  assert _fires(config, sm, 3)[0] is None


# The tests above all gate a TASK, which is evaluated during the DAG walk. A slot
# CONDITION is read much earlier -- by the cue matcher, before the walk -- and that is a
# different point in the turn, so it needs its own coverage. It was the gap: the turn
# context was published only at the end of the fill stages, so cue matching measured
# every `since_turns` gate against the PREVIOUS turn's counter and a caller's answer was
# dropped on exactly the turn they gave it.


def _slot_gated(cond):
  """A slot whose CAPTURE is gated, answered by a cue rather than by a task."""
  return {"slots": [
      flows.event_slot("offered"),
      flows.intent_slot("answer", {"YES": ["yes please"], "NO": ["no thanks"]},
                        setter="set_answer", condition=cond),
  ], "tasks": [], "single_flow": True}


def _says(config, sm, text, turn):
  r = fb.run_engine(config, sm, last_user_text=text, config_id="t", n_user_turns=turn)
  return (r["sm"].get("filled") or {}).get("answer"), r["sm"]


def test_a_since_gated_slot_captures_the_answer_on_the_very_next_turn():
  """The regression. The caller is offered something on turn 1 and accepts on turn 2;
  the cue matcher has to see the gate as OPEN on turn 2, or the acceptance is thrown
  away and the agent asks again."""
  config = _slot_gated(flows.since("offered"))
  _, sm = _says(config, {"filled": {"offered": True}}, "", 1)
  answer, _ = _says(config, sm, "yes please", 2)
  assert answer == "YES"


def test_a_since_gated_slot_still_refuses_the_turn_the_offer_landed():
  """The other half: the gate must not open on the offer's own turn, or the model
  answers its own question before the caller is asked."""
  config = _slot_gated(flows.since("offered"))
  answer, _ = _says(config, {"filled": {"offered": True}}, "yes please", 1)
  assert answer is None


def test_a_latch_written_by_the_walk_is_stamped_on_the_turn_it_was_written():
  """The other half of the same ordering. An announce writes its latch during the DAG
  WALK, which runs after the fill-stage sweep, so without a second sweep the latch
  carries no stamp until the next turn stamps it — and `since_turns` then opens a turn
  later than it reads, which on a live call is an offer whose acceptance is refused."""
  config = {"slots": [
      flows.event_slot("offered"),
      flows.announce("Offer", ["Shall I?"], sets={"offered": "true"}, preempt=True),
      flows.intent_slot("answer", {"YES": ["yes please"]}, setter="set_answer",
                        condition=flows.since("offered")),
  ], "tasks": [], "single_flow": True}
  r = fb.run_engine(config, {}, last_user_text="hello", config_id="t", n_user_turns=1)
  sm = r["sm"]
  assert sm["filled"].get("offered") == "true", "the announce did not fire"
  assert sm["_filled_turn"]["offered"] == 1, (
      f"latch stamped {sm['_filled_turn']['offered']}, not the turn it was written")
  r = fb.run_engine(config, sm, last_user_text="yes please", config_id="t",
                    n_user_turns=2)
  assert (r["sm"].get("filled") or {}).get("answer") == "YES"


def test_the_turn_context_does_not_outlive_the_turn():
  """`_TURN_CTX` is per-turn state like `_sm_ref`, so it is cleared on every exit path.
  Reset rather than dropped: turn 0 with no stamps reads FALSE for every turn-relative
  predicate, so a stale read outside a turn refuses a gate instead of opening one."""
  engine = fb.load_engine()
  config = _gated(flows.since("offered"))
  fb.run_engine(config, {"filled": {"offered": True}}, config_id="t", n_user_turns=7)
  assert engine._TURN_CTX == {"stamps": {}, "now": 0}


def test_turns_below_one_is_rejected():
  with pytest.raises(ValueError, match="at least 1"):
    flows.since("offered", turns=0)



def test_a_payload_whose_only_key_is_a_MAPPED_result_is_not_peeled():
  """A tool whose payload is legitimately one `result` field looks exactly like an
  envelope. The config is what tells them apart, and peeling a declared one leaves
  its slot silently empty."""
  config = _cfg([flows.event_slot("summary")],
                [{"name": "t", "tool": "sweep_tool", "inputs": [],
                  "outputs": {"result": "summary"}}])
  sm = _run(config)["sm"]
  filled = fb.run_intake("sweep_tool", {"success": True, "result": "ok"},
                         sm)["sm"].get("filled", {})
  assert filled.get("summary") == "ok"
