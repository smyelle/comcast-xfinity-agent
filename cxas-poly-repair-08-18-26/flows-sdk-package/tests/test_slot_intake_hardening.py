"""`slot_intake` hardening — five ways the WRITE path lost or invented a value.

Everything a caller ever says reaches persistent state through `slot_intake`, so a
defect here is silent by construction: the tool succeeded, the log says so, and the
slot is simply not there. Each section below pins one such loss.

  * D14 — an unknown flow id LATCHED the gate. The gate then reads as filled, so the
    engine's hiding policy hides the gate setter along with every other setter, and
    the turn renders with no callable move and no message. Reproduced end to end in
    `test_a_latched_unknown_id_is_what_wedges_the_call`. The emitted enum setter
    refuses this upstream (`test_router_gate_is_an_enum`); this is the write path's
    own copy of the invariant, for the id that reaches intake some other way (a
    hand-authored bootstrap body, a customized setter, an already-deployed tool).

  * D25 — a shut gate DROPPED a value the caller gave in the same turn. Two paths to
    the same loss, one per tool order: the `reset_on_complete` re-arm overwrote
    `pending` wholesale, and clearing per-flow internals took the config-derived
    setter maps with it so intake could no longer route the setter at all.

  * D23 — a setter reporting `stored: True` with NO value staged `None`, which reads
    back to the caller as a confirmed empty slot.

  * D-multi — a multi-field setter reporting BOTH an error and a value for one field
    staged the value it had just rejected.

  * D37 — rejecting a correction left the slot EMPTY. Intake pops the previously
    confirmed value when the phase-2 setter returns; nothing outlived the engine's
    one-shot `_correction_pending`, so a reject re-asked a value the caller had
    already given. Intake's half is the `_correction_prior` mirror pinned here; the
    engine settles it (see the mirror's docstring).

Fully offline: no network, no creds, no LLM.

Run:
  cd /Users/fsamuel/Labs/cxas-labs
  PYTHONPATH=packages/flows/src .venv/bin/python -m pytest \
      packages/flows/tests/test_slot_intake_hardening.py -q
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)


@pytest.fixture(autouse=True)
def _drop_engine_caches():
  """The engine caches compiled configs process-globally, keyed by config id."""
  yield
  fb.clear_cache()


def _tags(sm: dict) -> list[str]:
  return [e.get("tag") for e in sm.get("_log", [])]


def _entry(sm: dict, tag: str) -> dict:
  return next(e for e in sm["_log"] if e.get("tag") == tag)


# --------------------------------------------------------------------------- #
# D14 — an invalid flow id must never latch the gate
# --------------------------------------------------------------------------- #

def _routed_cfg(config_id: str, **bootstrap_over) -> dict:
  """One agent, two flows, each flow's slot conditioned on the gate value.

  The shape the wedge needs: activating a flow is what makes its slots collectable,
  so a gate value no flow answers to leaves EVERY slot inactive.
  """
  boot = {"tool": "set_active_flow", "slot": "active_flow"}
  boot.update(bootstrap_over)
  return {
      "config_id": config_id,
      "gate_slot": "active_flow",
      "flow_types": ["billing", "triage"],
      "bootstrap": boot,
      "slots": [
          {"name": "account_number", "source": "user", "ask": "Account number?",
           "setter": "set_account_number",
           "condition": "lambda f: f.get('active_flow') == 'billing'"},
          {"name": "scope", "source": "user", "ask": "Everything, or one app?",
           "setter": "set_scope",
           "condition": "lambda f: f.get('active_flow') == 'triage'"},
      ],
      "tasks": [],
  }


def _entered(config_id: str, **bootstrap_over):
  """A live sm one engine turn in, so `_flow_types` / `_default_flow` are derived."""
  cfg = _routed_cfg(config_id, **bootstrap_over)
  sm = fb.seed_sm(copy.deepcopy(cfg))
  sm = fb.run_engine(copy.deepcopy(cfg), sm, "hello", config_id=config_id)["sm"]
  return cfg, sm


def test_a_latched_unknown_id_is_what_wedges_the_call():
  """WHY the guard exists, driven rather than asserted from the code.

  Writing an id no flow answers to leaves the model nothing to call: the gate reads
  as filled so the hiding policy hides `set_active_flow`, and no flow is active so
  every setter is inactive and hidden too. The turn renders empty and repeats.
  """
  cfg, sm = _entered("si_h_wedge")
  sm.setdefault("filled", {})["active_flow"] = "Troubleshoot Internet"

  action = fb.run_engine(copy.deepcopy(cfg), sm, "", config_id="si_h_wedge")["action"]
  hidden = set(action.get("hide_tools") or [])
  assert {"set_active_flow", "set_account_number", "set_scope"} <= hidden, (
      "a latched unknown id must be the thing that leaves no callable move; if this "
      f"fails the scenario has changed, not the fix. hidden={sorted(hidden)}")
  assert not action.get("message"), (
      "no tool to call AND nothing to say is the empty turn the caller sits through")


def test_an_unknown_flow_is_refused_rather_than_latched():
  """The fix: intake returns BEFORE writing the gate, so the turn stays recoverable."""
  _cfg, sm = _entered("si_h_refuse")

  sm = fb.run_intake(
      "set_active_flow", {"stored": True, "value": "Troubleshoot Internet"}, sm)["sm"]

  assert "active_flow" not in sm.get("filled", {}), (
      "an id no flow answers to filled the gate; every setter then goes inactive "
      "while the filled gate hides the one tool that could fix it")
  assert "route_rejected_unknown_flow" in _tags(sm)
  assert _entry(sm, "route_rejected_unknown_flow")["level"] == "WARN"


def test_after_a_refusal_the_gate_is_still_collectable():
  """Recoverable, not merely un-corrupted: the gate setter stays visible."""
  cfg, sm = _entered("si_h_recover")
  sm = fb.run_intake(
      "set_active_flow", {"stored": True, "value": "Troubleshoot Internet"}, sm)["sm"]

  action = fb.run_engine(copy.deepcopy(cfg), sm, "", config_id="si_h_recover")["action"]
  assert "set_active_flow" not in set(action.get("hide_tools") or []), (
      "the route/default backstops and the model both need the gate setter to "
      "still be callable after a bad id")


def test_a_refusal_does_not_disturb_the_flow_already_active():
  """A bad SWITCH must leave the caller where they were, mid-collection."""
  _cfg, sm = _entered("si_h_intact")
  sm.setdefault("filled", {})["active_flow"] = "billing"
  sm["filled"]["account_number"] = "12345"

  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "nonsense"}, sm)["sm"]

  assert sm["filled"]["active_flow"] == "billing"
  assert sm["filled"]["account_number"] == "12345"


def test_a_mis_cased_but_advertised_id_is_canonicalized_not_refused():
  """A hand-written setter that skips the normalize the emitted one does still
  names a real flow. Rejecting it would fail a caller who asked for exactly this."""
  _cfg, sm = _entered("si_h_case")

  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "Billing"}, sm)["sm"]

  assert sm["filled"]["active_flow"] == "billing", (
      "the canonical id is what `flow_config_map` is keyed by; storing 'Billing' "
      "misses it exactly as an invented label would")
  assert "route_rejected_unknown_flow" not in _tags(sm)


def test_an_unknown_id_is_coerced_when_a_home_base_is_configured():
  """PINNED: with `default_flow` set, an unadvertised id lands on the home base —
  the refusal branch must not have replaced that."""
  _cfg, sm = _entered("si_h_default", default_flow="triage")

  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "nonsense"}, sm)["sm"]

  assert sm["filled"]["active_flow"] == "triage"
  assert _entry(sm, "route_coerced_to_default")["data"]["requested"] == "nonsense"


def test_a_real_flow_id_stores_untouched():
  _cfg, sm = _entered("si_h_ok")
  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "triage"}, sm)["sm"]
  assert sm["filled"]["active_flow"] == "triage"
  assert "route_rejected_unknown_flow" not in _tags(sm)


def test_an_agent_that_advertises_no_flow_types_stores_as_before():
  """The valid set is discoverable only from `_flow_types`. An ordinary (non-router)
  gate takes free text, and narrowing it would refuse perfectly good values."""
  cfg = {"config_id": "si_h_free", "gate_slot": "topic",
         "bootstrap": {"tool": "set_topic", "slot": "topic"},
         "slots": [], "tasks": []}
  sm = fb.seed_sm(copy.deepcopy(cfg))
  sm = fb.run_intake("set_topic", {"stored": True, "value": "anything at all"},
                     sm)["sm"]
  assert sm["filled"]["topic"] == "anything at all"


# --------------------------------------------------------------------------- #
# D25 — a shut gate must not drop a value the caller already gave
# --------------------------------------------------------------------------- #

D25_CFG = {
    "config_id": "si_h_d25",
    "gate_slot": "active_flow",
    "bootstrap": {"tool": "set_active_flow", "slot": "active_flow",
                  "reset_on_complete": True},
    "slots": [{"name": "widget_id", "source": "user", "ask": "Which widget?",
               "setter": "set_widget_id"}],
    "tasks": [],
}


def _completed_sm(config_id: str) -> dict:
  """A flow that has just finished — the shut gate the next request re-arms."""
  sm = fb.seed_sm(copy.deepcopy(D25_CFG))
  sm["_config_id"] = config_id
  sm["filled"] = {}
  sm["pending"] = {}
  sm["status"] = "complete"
  return sm


def test_a_value_given_as_the_gate_re_arms_survives_the_reset():
  """"Thanks — now I need a widget, it's W-5." The setter lands first, then the gate
  re-arms; the re-arm used to overwrite `pending` wholesale and W-5 was re-asked."""
  sm = _completed_sm("si_h_d25_a")

  sm = fb.run_intake("set_widget_id", {"stored": True, "value": "W-5"}, sm)["sm"]
  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "widgets"}, sm)["sm"]

  assert sm["pending"].get("widget_id") == "W-5", (
      "the caller answered in the same breath as the switch and was asked again")
  assert sm["filled"]["active_flow"] == "widgets"


def test_the_same_value_survives_when_the_tools_come_back_in_the_other_order():
  """Second path to the same loss: the gate re-arms FIRST, and clearing per-flow
  internals took the config-derived setter maps with it — so intake had nothing to
  route `set_widget_id` by and the value went nowhere at all."""
  sm = _completed_sm("si_h_d25_b")

  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "widgets"}, sm)["sm"]
  assert sm.get("_setter_slots", {}).get("set_widget_id") == "widget_id", (
      "the setter maps describe the CONFIG, not the flow; wiping them on a "
      "transition strands every tool of the same turn")

  sm = fb.run_intake("set_widget_id", {"stored": True, "value": "W-5"}, sm)["sm"]
  assert sm["pending"].get("widget_id") == "W-5"


def test_a_re_deferred_shared_value_still_wins_its_own_key():
  """PINNED: the shared values the completed flow re-defers are merged OVER what is
  staged, so a once-per-session value is not clobbered by a stale stage."""
  sm = _completed_sm("si_h_d25_c")
  sm["_shared_slots"] = ["greeted"]
  sm["pending"] = {"widget_id": "W-5", "greeted": False}
  sm["deferred"] = {"greeted": True}

  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "widgets"}, sm)["sm"]

  assert sm["pending"] == {"widget_id": "W-5", "greeted": True}


def test_a_value_staged_before_the_first_entry_promotes():
  """The working case the fix must preserve, driven all the way to `filled`: on the
  FIRST entry there is no reset at all, and the engine promotes the staged value."""
  sm = fb.seed_sm(copy.deepcopy(D25_CFG))
  sm = fb.run_intake("set_widget_id", {"stored": True, "value": "W-5"}, sm)["sm"]
  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "widgets"}, sm)["sm"]

  sm = fb.run_engine(copy.deepcopy(D25_CFG), sm, "I need a widget, it's W-5",
                     config_id="si_h_d25_d")["sm"]

  assert sm["filled"].get("widget_id") == "W-5"


def test_a_switch_still_discards_the_prior_flows_private_state():
  """The transition clear is what stops cross-flow leaks; keeping the setter maps
  must not have kept anything else."""
  sm = _completed_sm("si_h_d25_e")
  sm["_retries"] = {"Lookup": 2}
  sm["_correction_recollect"] = ["widget_id"]

  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "widgets"}, sm)["sm"]

  assert "_retries" not in sm and "_correction_recollect" not in sm


# --------------------------------------------------------------------------- #
# D23 — `stored: True` with no value
# --------------------------------------------------------------------------- #

D23_CFG = {
    "config_id": "si_h_d23",
    "slots": [{"name": "guest_name", "source": "user", "ask": "Your name?",
               "setter": "set_guest_name"}],
    "tasks": [],
}


def _d23_sm(config_id: str) -> dict:
  sm = fb.seed_sm(copy.deepcopy(D23_CFG))
  sm["_config_id"] = config_id
  return sm


@pytest.mark.parametrize("result", [
    {"stored": True},
    {"stored": True, "value": None},
])
def test_a_setter_reporting_success_with_no_value_stages_nothing(result):
  """Staging the `None` would read back as a confirmed empty slot and count as
  progress, so the flow moves on having collected nothing."""
  sm = _d23_sm("si_h_d23_a")

  sm = fb.run_intake("set_guest_name", result, sm)["sm"]

  assert "guest_name" not in sm.get("pending", {})
  assert "guest_name" not in sm.get("filled", {})
  assert _entry(sm, "setter_stored_without_value")["level"] == "WARN"


@pytest.mark.parametrize("value", [False, 0, "", 0.0])
def test_a_falsey_but_real_value_still_lands(value):
  """The guard tests `is None`, not falsiness — `False`/`0`/`""` are real answers
  a caller gave, and refusing them would re-ask a question already answered."""
  sm = _d23_sm("si_h_d23_b")

  sm = fb.run_intake("set_guest_name", {"stored": True, "value": value}, sm)["sm"]

  assert sm["pending"]["guest_name"] == value
  assert "setter_stored_without_value" not in _tags(sm)


# --------------------------------------------------------------------------- #
# D-multi — an error and a value for the SAME field
# --------------------------------------------------------------------------- #

MULTI_CFG = {
    "config_id": "si_h_multi",
    "slots": [
        {"name": "party_size", "source": "user", "ask": "How many?",
         "setter": "set_booking", "setter_field": "party_size"},
        {"name": "booking_time", "source": "user", "ask": "What time?",
         "setter": "set_booking", "setter_field": "time"},
    ],
    "tasks": [],
}


def _multi_sm(config_id: str) -> dict:
  sm = fb.seed_sm(copy.deepcopy(MULTI_CFG))
  sm["_config_id"] = config_id
  return sm


def test_a_field_reported_as_an_error_does_not_also_stage_its_value():
  """A self-contradictory result. `field_errors` was applied first and `values` then
  iterated unconditionally, so the rejected value was staged anyway — the caller
  heard the correction question AND the bad value read back as accepted."""
  sm = _multi_sm("si_h_multi_a")

  sm = fb.run_intake("set_booking", {
      "stored": True,
      "values": {"party_size": 99},
      "field_errors": {"party_size": "too_large"},
  }, sm)["sm"]

  assert "party_size" not in sm.get("pending", {}), (
      "the write path honored the value it had just rejected")
  assert sm["_slot_errors"] == [{"slot": "party_size", "code": "too_large"}]
  assert _entry(sm, "multi_setter_value_rejected")["level"] == "WARN"


def test_a_contradiction_on_one_field_does_not_block_a_clean_sibling():
  """Partial progress is the point of a multi-field setter: the good field lands."""
  sm = _multi_sm("si_h_multi_b")

  sm = fb.run_intake("set_booking", {
      "stored": True,
      "values": {"party_size": 99, "time": "7pm"},
      "field_errors": {"party_size": "too_large"},
  }, sm)["sm"]

  assert sm["pending"] == {"booking_time": "7pm"}


def test_an_error_on_a_confirmed_field_still_clears_it():
  """PINNED: a validation failure on a FILLED slot clears it so the retry flows
  through the now-visible setter. Skipping the value must not skip the clear."""
  sm = _multi_sm("si_h_multi_c")
  sm["filled"] = {"party_size": 4}

  sm = fb.run_intake("set_booking", {
      "stored": True,
      "values": {"party_size": 99},
      "field_errors": {"party_size": "too_large"},
  }, sm)["sm"]

  assert "party_size" not in sm["filled"]
  assert "party_size" not in sm.get("pending", {})


# --------------------------------------------------------------------------- #
# D37 — the value a rejected correction would otherwise lose
# --------------------------------------------------------------------------- #

def test_the_phase_2_setter_mirrors_the_prior_confirmed_value():
  """Intake pops the confirmed value once the new one exists. `_correction_pending`
  is popped by the engine on the very next pass, so without a durable mirror a
  REJECTED readback leaves the slot empty and re-asks a value already given."""
  sm = _d23_sm("si_h_d37_a")
  sm["filled"] = {"guest_name": "Ada"}
  sm["_correction_recollect"] = ["guest_name"]

  sm = fb.run_intake("set_guest_name", {"stored": True, "value": "Grace"}, sm)["sm"]

  assert "guest_name" not in sm["filled"], "the old value is popped here, as before"
  assert sm["_correction_prior"] == {"guest_name": "Ada"}
  assert sm["_correction_pending"][0]["value"] == "Grace"


def test_the_mirror_outlives_the_one_shot_correction_pending():
  """The whole point: `_correction_pending` is consumed by the engine's apply pass,
  and the mirror is what is still there when the caller answers the readback."""
  sm = _d23_sm("si_h_d37_b")
  sm["filled"] = {"guest_name": "Ada"}
  sm["_correction_recollect"] = ["guest_name"]
  sm = fb.run_intake("set_guest_name", {"stored": True, "value": "Grace"}, sm)["sm"]

  sm.pop("_correction_pending")  # what the engine's apply pass does

  assert sm["_correction_prior"] == {"guest_name": "Ada"}


def test_an_ordinary_setter_leaves_no_correction_mirror():
  """Only a phase-2 correction pops a confirmed value, so only it needs the mirror —
  an unconditional stash would restore a value nobody corrected."""
  sm = _d23_sm("si_h_d37_c")

  sm = fb.run_intake("set_guest_name", {"stored": True, "value": "Ada"}, sm)["sm"]

  assert "_correction_prior" not in sm


def test_correcting_a_slot_that_was_never_confirmed_stashes_nothing():
  """`set_slot_change` may name an UNFILLED slot to add it. There is no prior value
  to restore, and stashing a `None` would restore an empty confirmation."""
  sm = _d23_sm("si_h_d37_d")
  sm["_correction_recollect"] = ["guest_name"]

  sm = fb.run_intake("set_guest_name", {"stored": True, "value": "Ada"}, sm)["sm"]

  assert "_correction_prior" not in sm
  assert sm["pending"]["guest_name"] == "Ada"


def test_the_mirror_is_per_flow_and_does_not_survive_a_switch():
  """A restore into another flow's scope would write the value into the wrong
  request; the mirror carries no keep-set entry, so a transition discards it."""
  sm = fb.seed_sm(copy.deepcopy(D25_CFG))
  sm["_config_id"] = "si_h_d37_e"
  sm["filled"] = {"active_flow": "widgets", "widget_id": "W-5"}
  sm["_correction_recollect"] = ["widget_id"]
  sm = fb.run_intake("set_widget_id", {"stored": True, "value": "W-6"}, sm)["sm"]
  assert sm["_correction_prior"] == {"widget_id": "W-5"}

  sm = fb.run_intake("set_active_flow", {"stored": True, "value": "gadgets"}, sm)["sm"]

  assert "_correction_prior" not in sm
