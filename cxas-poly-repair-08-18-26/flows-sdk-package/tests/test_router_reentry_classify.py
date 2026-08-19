"""Router re-entry forced-classify + bounded re-route guard.

A steering router DEFERS a category (e.g. billing) by recording the intent, speaking a
hand-off, and terminating (`bootstrap.reset_on_complete` -> status="zombie"); the shipped
reap returns control to the router in a CLEAN state (`before_agent`). On that ONE clean
router turn the caller's follow-up ("how much do I owe?") is byte-identical to a cold
routing turn, so the model reads it as already-handled and declines to volunteer
`set_active_flow` -> the router never leaves -> dead-end.

The fix COMPELS a classification on that turn (router-scoped Pass A: hide every tool but
`classify_turn_intent`, inject a re-entry-framed classifier SI) and routes the ENGINE
deterministically on the verdict: `continue` -> re-route to the recorded last intent (no
re-ask), `switch:<flow>` -> that flow, `escalate` -> human, `end` -> `end_session`; an
undeterminable turn defaults to a bounded re-route, and once a dedicated `_reentry_count`
counter is spent it falls to the shipped `steering_disambiguate`/`on_exhaust` net.

The REAL emitted callbacks/engine are compiled/loaded and driven — not reimplemented.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_router_reentry_classify.py
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

import flows

# --------------------------------------------------------------------------- #
# Compile the REAL before_agent + slot_intake modules (compile-and-exec, like
# test_switch_into_flow.py); load the REAL engine via the offline loader.
# --------------------------------------------------------------------------- #

_SRC = pathlib.Path(flows.__file__).parent


def _load(rel: str, **inject) -> dict:
  src = _SRC / rel
  ns: dict = dict(inject)
  exec(compile(src.read_text(), str(src), "exec"), ns)  # noqa: S102
  return ns


_BA = _load("engine/framework/callbacks/before_agent.py",
            CallbackContext=object, Content=object)
_resolve_config_id = _BA["_resolve_config_id"]

_INTAKE = _load("engine/framework/tools/slot_intake/python_function/python_code.py")
_intake_classify_turn_intent = _INTAKE["_intake_classify_turn_intent"]

from flows.engine import loader as fb  # noqa: E402

fb.set_framework_root(str(_SRC / "engine/framework/tools"))
_ENGINE = fb.load_engine()

_FMAP = {"billing": "billing_cfg", "tech": "tech_cfg"}


class _Ctx:
  def __init__(self, state, events=None):
    self.state = state
    self.events = events or []


def _router_state(detected_intent=None, detected_path=None, active_config_id=None):
  state = {"flow_config_map": json.dumps(_FMAP), "default_config_id": "router_cfg"}
  if detected_intent is not None:
    state["detected_intent"] = detected_intent
  if detected_path is not None:
    state["detected_path"] = detected_path
  if active_config_id is not None:
    state["_active_config_id"] = active_config_id
  return state


def _zombie_sm(gate_value, *, status="zombie", flow_state=None, **extra):
  sm = {
      "_gate_slot": "active_flow",
      "filled": {"active_flow": gate_value} if gate_value else {},
      "status": status,
      "_zombie": {"flow": ""},
  }
  if flow_state is not None:
    sm["_flow_state"] = flow_state
  sm.update(extra)
  return sm


# ═══════════════════════════════════════════════════════════════════════════
# before_agent._resolve_config_id — breadcrumb stamp + counter reset
# ═══════════════════════════════════════════════════════════════════════════


def test_return_to_router_stamps_reentry_breadcrumb():
  """Defer terminated, gate EMPTY (return-to-router), a recorded detected_intent survives
  -> stamp _router_reentry_intent + bump _reentry_count; resolve to the router."""
  sm = _zombie_sm("")  # gate empty
  cfg, source = _resolve_config_id(
      _Ctx(_router_state(detected_intent="billing")), sm)

  assert (cfg, source) == ("router_cfg", "active_flow_router")
  assert sm["_router_reentry_intent"] == "billing"
  assert sm["_reentry_count"] == 1
  assert sm["status"] == "in_progress"      # reaped so the router runs clean
  assert "_zombie" not in sm


def test_reentry_count_increments_across_repeated_defers():
  """A record-and-terminate defer that keeps re-zombie-ing climbs the counter."""
  sm = _zombie_sm("", _reentry_count=1)
  _resolve_config_id(_Ctx(_router_state(detected_intent="billing")), sm)
  assert sm["_reentry_count"] == 2


def test_detected_path_root_is_used_for_the_breadcrumb():
  """Multi-level: the breadcrumb keys off the L1 root of detected_path (the gate key),
  not the deepest leaf, so `continue` re-routes to the flow the L1 gate names."""
  sm = _zombie_sm("")
  _resolve_config_id(
      _Ctx(_router_state(detected_intent="late_fee",
                         detected_path="billing/late_fee")), sm)
  assert sm["_router_reentry_intent"] == "billing"


def test_reroute_at_turn_start_resets_counter_and_breadcrumb():
  """Gate REFILLED to a flow at turn start (a route the model volunteered) -> the flow
  dispatches and the re-entry loop is over: drop the counter + breadcrumb, don't re-stamp."""
  sm = _zombie_sm("billing", _reentry_count=2, _router_reentry_intent="billing")
  cfg, source = _resolve_config_id(
      _Ctx(_router_state(detected_intent="billing")), sm)

  assert (cfg, source) == ("billing_cfg", "active_flow")
  assert "_reentry_count" not in sm
  assert "_router_reentry_intent" not in sm


def test_live_flow_dispatch_resets_counter():
  """An in-progress flow (not a zombie) dispatching also clears any stale counter."""
  sm = {"_gate_slot": "active_flow", "filled": {"active_flow": "billing"},
        "status": "in_progress", "_reentry_count": 2, "_router_reentry_intent": "billing"}
  cfg, source = _resolve_config_id(_Ctx(_router_state()), sm)

  assert (cfg, source) == ("billing_cfg", "active_flow")
  assert "_reentry_count" not in sm
  assert "_router_reentry_intent" not in sm


def test_no_detected_intent_leaves_no_breadcrumb():
  """A return-to-router with nothing recorded -> nothing to re-enter -> no marker."""
  sm = _zombie_sm("")
  _resolve_config_id(_Ctx(_router_state()), sm)
  assert "_router_reentry_intent" not in sm
  assert "_reentry_count" not in sm


def test_no_flow_config_map_stamps_nothing():
  """A non-router app has no flow_config_map, so the whole reap branch — and the stamp
  inside it — is unreachable (byte-identical)."""
  state = {"default_config_id": "solo_cfg", "detected_intent": "billing"}
  sm = {"status": "zombie", "filled": {}, "_zombie": {"flow": "x"}}
  cfg, source = _resolve_config_id(_Ctx(state), sm)
  assert (cfg, source) == ("solo_cfg", "default")
  assert "_router_reentry_intent" not in sm
  assert sm["status"] == "zombie"          # untouched


def test_paused_flow_is_not_stamped():
  """A paused flow (_flow_state) is left to the resume path — no reap, no breadcrumb."""
  sm = _zombie_sm("", flow_state=[{"flow": "billing", "slots": {}}])
  _resolve_config_id(_Ctx(_router_state(detected_intent="billing")), sm)
  assert "_router_reentry_intent" not in sm
  assert sm["status"] == "zombie"


# ═══════════════════════════════════════════════════════════════════════════
# slot_filling_engine — arming + consumption (REAL engine, offline)
# ═══════════════════════════════════════════════════════════════════════════


_CFG = {
    "router": True,
    "gate_slot": "active_flow",
    "flow_types": ["billing", "tech"],
    "route_cues": {"billing": ["bill", "charge"], "tech": ["internet", "box"]},
    "flow_descriptions": {"billing": "billing and payments",
                          "tech": "internet/tv troubleshooting"},
    "bootstrap": {"tool": "set_active_flow", "slot": "active_flow",
                  "intent_first": True},
    "slots": [],
    "tasks": [],
}


def _drive(sm, text, n=1, inactivity=False):
  return _ENGINE.slot_filling_engine({
      "raw_config": _CFG, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": inactivity,
      "event_data": {}, "config_id": "router", "n_user_turns": n,
  })["action"]


def _reentry_sm(count=1, intent="billing"):
  return {"filled": {}, "pending": {}, "status": "in_progress",
          "_router_reentry_intent": intent, "_reentry_count": count}


def test_arming_fires_on_a_router_reentry_turn():
  """Breadcrumb present + gate empty -> COMPEL a classification: classifier SI injected,
  set_active_flow hidden, classify_turn_intent left visible, _classify_mode latched."""
  sm = _reentry_sm()
  out = _drive(sm, "how much do I owe?")

  assert out["tag"] == "reentry_classify"
  assert sm["_classify_mode"] is True
  assert out["si"] and "classify_turn_intent" in out["si"]
  assert "set_active_flow" in out["hide_tools"]
  assert "classify_turn_intent" not in out["hide_tools"]


def test_cold_router_turn_hides_classify_turn_intent():
  """A PLAIN cold router turn (no re-entry breadcrumb) now HIDES classify_turn_intent. It is
  a Pass-A-only tool the model would otherwise volunteer ("Output ONLY this tool call"),
  burning an extra LLM inference before it routes. Contrast with the arming test above: the
  re-entry classifier keeps classify VISIBLE; a cold turn hides it. set_active_flow — the
  tool the router is actually there to call — stays visible."""
  # A neutral opener with NO route_cue match, so it reaches the plain router turn (a cue
  # match would preempt via route_backstop before the router block).
  sm = {"filled": {}, "pending": {}, "status": "in_progress"}
  out = _drive(sm, "hi there, can you help me")
  assert out["tag"] == "router"
  assert "classify_turn_intent" in out.get("hide_tools", [])
  assert "set_active_flow" not in out.get("hide_tools", [])


def test_reentry_exhausted_falls_to_cold_router_and_hides_classify():
  """Once the bounded counter is spent, arming does NOT fire and the turn drops to the plain
  cold router — where classify is hidden like any other cold turn (the re-entry path is over,
  so keeping classify visible would just re-leak)."""
  sm = _reentry_sm(count=3)  # over the cap → no arming
  out = _drive(sm, "still not sure")
  assert out["tag"] == "router"
  assert "classify_turn_intent" in out.get("hide_tools", [])


def test_cold_router_hides_classify_even_when_not_intent_first():
  """The cold-router hide is UNCONDITIONAL, not gated on _intent_first. A single-agent router
  DECLARES classify_turn_intent even when the router itself is not intent-first (e.g. it has
  intent-first SUB-flows), so gating the hide on _intent_first would leak it there. Drive a
  router config with NO bootstrap.intent_first and confirm classify is still hidden on a cold
  turn (hiding an undeclared tool is a harmless no-op, so this is safe for every router)."""
  cfg = {**_CFG, "bootstrap": {"tool": "set_active_flow", "slot": "active_flow"}}  # no intent_first
  out = _ENGINE.slot_filling_engine({
      "raw_config": cfg, "sm": {"filled": {}, "pending": {}, "status": "in_progress"},
      "last_user_text": "hi there, can you help me",
      "scanned_user_text": "hi there, can you help me", "is_inactivity": False,
      "event_data": {}, "config_id": "router", "n_user_turns": 1,
  })["action"]
  assert out["tag"] == "router"
  assert "classify_turn_intent" in out.get("hide_tools", [])


def test_consume_continue_reroutes_to_the_reentry_target():
  """continue -> re-route to the recorded last intent with NO re-ask (set_active_flow)."""
  sm = _reentry_sm()
  _drive(sm, "how much do I owe?")                     # Pass 1: arm
  _intake_classify_turn_intent(sm, {"intent": "continue"}, {})  # real intake
  out = _drive(sm, "", n=1)                            # Pass 2: consume

  assert out["function_call"] == {"name": "set_active_flow", "args": {"flow": "billing"}}
  assert sm.get("_classify_mode") is False
  assert "_router_reentry_intent" not in sm            # one-shot breadcrumb consumed


def test_consume_switch_routes_to_the_named_flow():
  """switch:tech (a weakly-phrased pivot the classifier reads by meaning) -> tech, NOT the
  last intent — the exact case a blind re-route-to-last mis-handles."""
  sm = _reentry_sm()
  _drive(sm, "wait, my box is acting up")
  _intake_classify_turn_intent(sm, {"intent": "switch:tech"}, {})
  out = _drive(sm, "", n=1)

  assert out["function_call"] == {"name": "set_active_flow", "args": {"flow": "tech"}}


def test_consume_end_ends_the_session():
  """Wind-down ("that's all, bye") -> end_session, NOT a force-route back into billing."""
  sm = _reentry_sm()
  _drive(sm, "okay that's all, thanks, bye")
  _intake_classify_turn_intent(sm, {"intent": "end"}, {})
  out = _drive(sm, "", n=1)

  assert out["function_call"] == {"name": "end_session", "args": {}}
  assert out["tag"] == "reentry_end"


def test_end_label_survives_the_intake_as_pending_intent():
  """OPEN QUESTION check: classify_turn_intent's intake passes an arbitrary `end` label
  through as _pending_intent (free string, no enum) and stages NO _classified for it."""
  sm = {}
  _intake_classify_turn_intent(sm, {"intent": "end"}, {})
  assert sm["_pending_intent"] == "end"
  assert "_classified" not in sm


def test_bounded_counter_falls_through_after_the_cap():
  """Once _reentry_count exceeds the cap (2), arming does NOT fire — the turn drops to the
  plain router so the shipped disambiguate/on_exhaust net (after_model) owns the hand-off."""
  sm = _reentry_sm(count=3)
  out = _drive(sm, "still not sure")

  assert out["tag"] == "router"
  assert sm.get("_classify_mode") in (None, False)


def test_arming_holds_at_the_cap_boundary():
  """At the cap boundary (count == CAP) arming still fires (the last bounded re-route)."""
  sm = _reentry_sm(count=2)
  out = _drive(sm, "one more thing")
  assert out["tag"] == "reentry_classify"


def test_inert_without_a_breadcrumb():
  """No _router_reentry_intent (no flow_config_map reap ran) -> a plain router turn."""
  sm = {"filled": {}, "pending": {}, "status": "in_progress"}
  out = _drive(sm, "hello")
  assert out["tag"] == "router"
  assert sm.get("_classify_mode") in (None, False)


def test_inert_on_a_non_router_config():
  """A breadcrumb on a NON-router config never arms: the arming/consumption blocks are
  gated on raw_config.router."""
  non_router = {
      "gate_slot": "active_flow",
      "slots": [{"name": "amount", "source": "user", "setter": "set_amount",
                 "ask": "How much?"}],
      "tasks": [],
      "bootstrap": {"tool": "set_active_flow", "slot": "active_flow",
                    "intent_first": True},
  }
  sm = {"filled": {"active_flow": "billing"}, "pending": {}, "status": "in_progress",
        "_router_reentry_intent": "billing", "_reentry_count": 1}
  out = _ENGINE.slot_filling_engine({
      "raw_config": non_router, "sm": sm, "last_user_text": "how much",
      "scanned_user_text": "how much", "is_inactivity": False,
      "event_data": {}, "config_id": "billing", "n_user_turns": 1,
  })["action"]
  assert out.get("tag") != "reentry_classify"


def test_reentry_target_not_in_flow_types_does_not_arm():
  """A stale/unadvertised recorded intent (not a flow_types key) never arms a re-route."""
  sm = _reentry_sm(intent="cancelled_service")   # not in flow_types
  out = _drive(sm, "hi again")
  assert out["tag"] == "router"


def test_a_strong_route_cue_preempts_before_classify():
  """A high-precision route_cue on the re-entry utterance routes deterministically BEFORE
  the classifier ever runs (route_backstop wins in _intent_directive)."""
  sm = _reentry_sm()                     # breadcrumb billing
  out = _drive(sm, "my internet is down")
  assert out["tag"] == "route_backstop"
  assert out["function_call"] == {"name": "set_active_flow", "args": {"flow": "tech"}}


def test_inactivity_turn_does_not_arm():
  """Silence is not an intent to classify — an inactivity turn falls through to the
  router, never the re-entry classifier."""
  sm = _reentry_sm()
  out = _drive(sm, "", n=1, inactivity=True)
  assert out["tag"] != "reentry_classify"


# ═══════════════════════════════════════════════════════════════════════════
# _hiding_policy — classify hidden on child turns even without intent_first
# ═══════════════════════════════════════════════════════════════════════════

_hiding_policy = _ENGINE._hiding_policy


def test_hiding_policy_hides_classify_without_intent_first():
  """A single-agent steering router's DEFER children are NOT intent_first, yet classify is
  declared on the one shared agent — so it must be hidden on every child (Pass-B) turn
  regardless of _intent_first, else it leaks an extra inference per child turn."""
  cfg = {"gate_slot": "active_flow", "slots": [], "tasks": []}
  for phase in ("gate", "in_flow", "terminal"):
    hide = _hiding_policy({"_intent_first": False}, cfg, phase)
    assert "classify_turn_intent" in hide, phase


def test_hiding_policy_leaves_transition_tools_visible_without_intent_first():
  """Only classify moved out of the _intent_first gate. set_intent_changed is the transition
  mechanism for a NON-intent-first flow and try_again is its retry — both must stay VISIBLE
  when the flow is in progress and not intent-first (hiding them would break transitions)."""
  cfg = {"gate_slot": "active_flow", "slots": [], "tasks": []}
  # `account_number` filled (not the gate slot) => _flow_in_progress is True, so the
  # separate not-in-progress rule does not hide set_intent_changed.
  sm = {"_intent_first": False, "filled": {"account_number": "123"},
        "status": "in_progress"}
  hide = _hiding_policy(sm, cfg, "in_flow")
  assert "classify_turn_intent" in hide
  assert "set_intent_changed" not in hide
  assert "try_again" not in hide


def test_hiding_policy_still_hides_pass_a_companions_with_intent_first():
  """Under intent_first, classify + try_again + set_intent_changed are all still hidden on a
  Pass-B turn — the existing behavior is unchanged for intent-first flows."""
  cfg = {"gate_slot": "active_flow", "slots": [], "tasks": []}
  sm = {"_intent_first": True, "filled": {"account_number": "123"},
        "status": "in_progress"}
  hide = _hiding_policy(sm, cfg, "in_flow")
  assert {"classify_turn_intent", "try_again", "set_intent_changed"} <= set(hide)


# ═══════════════════════════════════════════════════════════════════════════
# build parity — the linchpin: classify_turn_intent must be DECLARED on a router
# ═══════════════════════════════════════════════════════════════════════════


def _router_app():
  collect = flows.Flow("billing", bootstrap={"reset_on_complete": True})
  collect.add(flows.user_slot("account_number", ask="Account number?"),
              flows.result_slot("amount_due", "LookupBill"))
  collect.task("LookupBill", "lookup_bill", ["account_number"], "amount_due",
               terminal=True, then_say="Found it.")
  tech = flows.Flow("tech", bootstrap={"reset_on_complete": True})
  tech.add(flows.user_slot("symptom", ask="What's wrong?"),
           flows.result_slot("fix", "Diagnose"))
  tech.task("Diagnose", "diagnose", ["symptom"], "fix", terminal=True,
            then_say="Try that.")
  router = flows.router_flow("host", ["billing", "tech"], root_agent="Host_Agent")
  return flows.App(root_flow=router, extra_flows=[tech],
                   app_display_name="rt")


def test_router_agent_declares_classify_turn_intent():
  """Without this declaration the engine's forced-classify has no tool to compel and
  silently degrades to a blind re-route (the decisive break in every forced-classify
  variant). It is already in _ROUTER_KEEP_TOOLS, so router_hide_tools keeps it visible."""
  with tempfile.TemporaryDirectory() as d:
    flows.build_app(_router_app(), d)
    with open(os.path.join(d, "agents", "Host_Agent", "Host_Agent.json")) as f:
      aj = json.load(f)
    assert "classify_turn_intent" in aj["tools"]
    # and it stays visible on a router turn (never hidden away).
    with open(os.path.join(d, "app.json")) as f:
      decls = {v["name"]: v["schema"]["default"]
               for v in json.load(f)["variableDeclarations"]}
    assert "classify_turn_intent" not in json.loads(decls["router_hide_tools"])
