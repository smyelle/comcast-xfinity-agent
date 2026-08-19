"""A hand-off payload without its `end_session` is a hang-up.

Reaching a human on a contact-center platform takes a structured payload on the turn.
The spoken line is for the caller; the payload is for the platform, and only one of the
two puts a person on the line. Both halves of that have shipped broken:

* a payload with no session end — the platform escalates while the agent keeps the
  call, so the caller waits for someone who never arrives; and
* a session end with no payload — the generic escalate rail's old behavior, which
  closed the call with a friendly line and routed nobody anywhere.

`flows.handoff` emits the pair as a unit and the validator rejects a config that split
them. These tests are the regression for both halves.
"""

from __future__ import annotations

import pytest

import flows
from flows.config.validation import raw_validate_single
from flows.engine import loader as fb

# The shape a live app emits, byte for byte — the fixture every equivalence below is
# measured against.
LIVE_UJET_PART = {
    "type": "payload",
    "data": {"ujet": {
        "menu_id": "90", "escalation_reason": "by_virtual_agent",
        "type": "action", "action": "escalation", "language": "en"}},
}
LIVE_UJET_END = {"type": "end_session", "reason": "transfer", "escalated": True}
LIVE_DFCX_PART = {
    "type": "payload",
    "data": {
        "transferToDialogflow":
            "projects/{project_id}/locations/us/agents/{dialogflow_agent_id}",
        "parameters": {"head_intent": "{head_intent}",
                       "transaction_id": "{transaction_id}"},
    },
}
LIVE_DFCX_END = {"type": "end_session", "reason": "transfer"}
LIVE_CXAS_PART = {
    "type": "payload",
    "data": {
        "transferToNga":
            "projects/{project_id}/locations/us/apps/{repair_app_id}",
        "variables": {"account_number": "{account_number}"},
    },
}
LIVE_CXAS_END = {"type": "end_session", "reason": "transfer"}


def _human():
  return flows.handoff(flows.ujet(menu_id="90"))


# ── the emitted bytes ────────────────────────────────────────────────────────


def test_a_ujet_handoff_emits_the_shape_the_platform_already_accepts():
  """Byte equivalence with a live app. A vendor payload is an integration contract:
  a reordered key or a renamed field is a hand-off that silently does nothing."""
  assert _human().parts() == [LIVE_UJET_PART, LIVE_UJET_END]


def test_a_dialogflow_handoff_is_a_transfer_and_not_an_escalation():
  """Nobody was escalated — the caller moved to another automated system. Marking the
  end escalated would overstate every escalation count that reads the flag."""
  parts = flows.handoff(flows.dialogflow_cx(
      project="{project_id}", location="us", agent_id="{dialogflow_agent_id}",
      parameters={"head_intent": "{head_intent}",
                  "transaction_id": "{transaction_id}"})).parts()
  assert parts == [LIVE_DFCX_PART, LIVE_DFCX_END]
  assert "escalated" not in parts[1]


def test_the_payload_comes_before_the_end():
  """Order is the hand-off: the platform has to be told where the caller goes before
  the leg is given up."""
  types = [p["type"] for p in _human().parts()]
  assert types == ["payload", "end_session"]


def test_a_raw_vendor_payload_still_works_for_a_platform_with_no_builder():
  h = flows.handoff({"genesys": {"queue": "billing"}}, escalated=True)
  assert h.parts() == [
      {"type": "payload", "data": {"genesys": {"queue": "billing"}}},
      {"type": "end_session", "reason": "transfer", "escalated": True},
  ]


def test_a_raw_vendor_payload_must_say_whether_it_escalates():
  """No default is right for a shape the SDK cannot read, and guessing is how a
  containment metric quietly becomes wrong."""
  with pytest.raises(ValueError, match="escalated=True"):
    flows.handoff({"genesys": {"queue": "billing"}})


def test_a_whole_response_part_is_not_a_vendor_payload():
  with pytest.raises(ValueError, match="whole response part"):
    flows.handoff(dict(LIVE_UJET_PART), escalated=True)


def test_a_ujet_menu_id_is_required():
  """The menu id picks the queue, so it picks which team answers."""
  with pytest.raises(ValueError, match="menu_id"):
    flows.ujet(menu_id="")


def test_a_dialogflow_target_must_be_a_real_agent_path():
  with pytest.raises(ValueError, match="projects/"):
    flows.dialogflow_cx(agent="my-agent")


def test_a_dialogflow_location_is_never_defaulted():
  """A wrong region is a transfer into a different agent, or none at all."""
  with pytest.raises(ValueError, match="project/location/agent_id"):
    flows.dialogflow_cx(project="p", agent_id="a")


def test_a_cxas_handoff_is_a_transfer_and_not_an_escalation():
  """The caller moves to another CES app, not a person — the same shape as a Dialogflow
  transfer, one app over. Marking the end escalated would overstate containment."""
  parts = flows.handoff(flows.cxas(
      project="{project_id}", location="us", app_id="{repair_app_id}",
      variables={"account_number": "{account_number}"})).parts()
  assert parts == [LIVE_CXAS_PART, LIVE_CXAS_END]
  assert "escalated" not in parts[1]


def test_a_cxas_app_can_be_given_as_a_full_resource():
  """The full resource and the composed parts reach the same payload."""
  composed = flows.cxas(project="p", location="us", app_id="a").data
  whole = flows.cxas(app="projects/p/locations/us/apps/a").data
  assert composed == whole == {"transferToNga": "projects/p/locations/us/apps/a"}


def test_a_cxas_target_must_be_a_real_app_resource():
  """A CXAS target is a CES app: the resource ends in apps/, and a Dialogflow agent path
  is not one — the platform routes on this string, so a wrong shape goes nowhere."""
  with pytest.raises(ValueError, match="app must be a projects/"):
    flows.cxas(app="my-app")
  with pytest.raises(ValueError, match="app must be a projects/"):
    flows.cxas(app="projects/p/locations/us/agents/a")


def test_a_cxas_location_is_never_defaulted():
  """A wrong region is a transfer into a different app, or none at all."""
  with pytest.raises(ValueError, match="project/location/app_id"):
    flows.cxas(project="p", app_id="a")


def test_a_cxas_full_path_and_parts_are_mutually_exclusive():
  with pytest.raises(ValueError, match="not both"):
    flows.cxas(app="projects/p/locations/us/apps/a", project="p")


def test_a_cxas_variables_dict_is_omitted_when_empty():
  """No variables means no `variables` key at all, exactly as dialogflow_cx omits
  empty `parameters`."""
  assert flows.cxas(app="projects/p/locations/us/apps/a").data == {
      "transferToNga": "projects/p/locations/us/apps/a"}
  assert flows.cxas(app="projects/p/locations/us/apps/a", variables={}).data == {
      "transferToNga": "projects/p/locations/us/apps/a"}


# ── the four sites ───────────────────────────────────────────────────────────


def test_an_announce_emits_both_parts_after_its_text():
  a = flows.announce("human_transfer", ["Connecting you now. Please hold."],
                     handoff=_human(), requires=["agent_escalation"])
  assert a["response"] == [
      {"type": "text", "text": "Connecting you now. Please hold."},
      LIVE_UJET_PART, LIVE_UJET_END,
  ]


def test_an_announce_cannot_end_twice():
  with pytest.raises(ValueError, match="handoff= or end=True"):
    flows.announce("t", ["Hold please."], handoff=_human(), end=True)


def test_an_announce_cannot_both_hand_off_and_transfer_to_a_sibling_agent():
  with pytest.raises(ValueError, match="handoff= or transfer_to="):
    flows.announce("t", ["Hold please."], handoff=_human(), transfer_to="Billing")


def test_the_escalate_rail_carries_the_payload():
  """The live defect: the rail emitted `say` + a bare end_session, so the caller was
  told a person was coming and was then disconnected with nothing routing them."""
  block = flows.escalate(say="Let me get you to someone.", handoff=_human())
  assert block["response"] == [LIVE_UJET_PART, LIVE_UJET_END]


def test_a_task_exhaust_hands_off_instead_of_apologizing():
  exhaust = _human().on_exhaust("I can't verify that, so I'll connect you.")
  assert exhaust == {
      "say": "I can't verify that, so I'll connect you.",
      "response": [LIVE_UJET_PART, LIVE_UJET_END],
  }
  t = flows.task("validate_otp", "validate_otp", ["otp"], "otp_ok",
                 on_failure={"max_retries": 3, "on_exhaust": exhaust})
  assert t["on_failure"]["on_exhaust"]["response"][0] == LIVE_UJET_PART


def test_a_slot_exhaust_hands_off_instead_of_marking_a_request():
  """`then: transfer_to_human` only RECORDS the request. On a platform that routes on
  a payload, the marker alone reaches nobody."""
  s = flows.user_slot("ssn", "What are the last four of your SSN?",
                      on_exhaust="I'll connect you with someone. Please hold.",
                      on_exhaust_handoff=_human())
  exhaust = s["validation"]["on_exhaust"]
  assert "then" not in exhaust
  assert exhaust["response"] == [LIVE_UJET_PART, LIVE_UJET_END]


def test_a_second_disposition_on_the_same_rung_is_rejected():
  with pytest.raises(ValueError, match="competing dispositions"):
    flows.user_slot("ssn", "Last four?", on_exhaust_handoff=_human(),
                    on_exhaust_then={"tool": "transfer_to_human"})
  with pytest.raises(ValueError, match="second one"):
    _human().on_exhaust("Connecting you.", then="escalate")


def test_a_verbatim_validation_ladder_would_have_swallowed_the_handoff():
  with pytest.raises(ValueError, match="silently dropped"):
    flows.user_slot("ssn", "Last four?", on_exhaust_handoff=_human(),
                    validation={"max_retries": 3})


# ── surfaces ─────────────────────────────────────────────────────────────────


def test_a_surface_gate_lands_on_both_parts_or_neither():
  """Gating the payload while the end survives IS the hang-up. The pair moves together
  or not at all."""
  parts = flows.handoff(flows.ujet(menu_id="90"), surface="voice").parts()
  assert all(p["condition"] == {"surface": "voice"} for p in parts)


def test_the_payloads_capability_is_the_one_gate_a_handoff_cannot_use():
  """Voice declares payloads:False, so this gate drops the hand-off on exactly the
  surface it exists for."""
  with pytest.raises(ValueError, match="payloads"):
    flows.handoff(flows.ujet(menu_id="90"),
                  condition={"capability": "payloads"})


# ── the validator ────────────────────────────────────────────────────────────


def _lint(slot_response):
  cfg = {
      "slots": [
          {"name": "topic", "source": "user", "setter": "set_topic",
           "ask": "What can I help with?"},
          {"name": "bye", "source": "announce", "requires": ["topic"],
           "response": slot_response},
      ],
      "tasks": [],
  }
  _valid, errors, warnings = raw_validate_single(cfg)
  return errors, warnings


def test_the_validator_rejects_a_payload_that_never_ends_the_leg():
  """THE regression. A hand-written config that emits the payload and forgets the end
  is the live defect, and it validated clean for as long as it existed."""
  errors, _ = _lint([{"type": "text", "text": "Connecting you now."},
                     dict(LIVE_UJET_PART)])
  assert any("hand-off payload with no 'end_session'" in e for e in errors), errors


def test_the_validator_accepts_the_pair():
  errors, warnings = _lint([{"type": "text", "text": "Connecting you now."},
                            dict(LIVE_UJET_PART), dict(LIVE_UJET_END)])
  assert errors == []
  assert not [w for w in warnings if "hand-off" in w], warnings


def test_the_validator_rejects_an_end_that_comes_first():
  """Parts are delivered in order; a leg given up before the payload lands takes the
  payload with it."""
  errors, _ = _lint([dict(LIVE_UJET_END), dict(LIVE_UJET_PART)])
  assert any("no 'end_session' part after it" in e for e in errors), errors


def test_a_card_payload_is_not_a_handoff():
  """The rule keys on the vendor shape, so ordinary structured content — which has no
  business ending anything — is untouched."""
  errors, _ = _lint([{"type": "text", "text": "Here are your options."},
                     {"type": "payload",
                      "data": {"richContent": [[{"type": "info", "title": "T"}]]}}])
  assert errors == []


def test_the_validator_rejects_a_pair_split_by_a_condition():
  errors, _ = _lint([
      {**LIVE_UJET_PART, "condition": {"surface": "voice"}},
      dict(LIVE_UJET_END),
  ])
  assert any("different conditions" in e for e in errors), errors


def test_the_validator_rejects_a_handoff_gated_on_payloads():
  errors, _ = _lint([
      {**LIVE_UJET_PART, "condition": {"capability": "payloads"}},
      {**LIVE_UJET_END, "condition": {"capability": "payloads"}},
  ])
  assert any("payloads" in e and "voice" in e.lower() for e in errors), errors


def test_the_validator_warns_when_an_escalation_is_not_reported_as_one():
  _errors, warnings = _lint([dict(LIVE_UJET_PART),
                             {"type": "end_session", "reason": "transfer"}])
  assert any("escalated:True" in w for w in warnings), warnings


def test_the_validator_warns_when_a_platform_transfer_claims_an_escalation():
  _errors, warnings = _lint([dict(LIVE_DFCX_PART),
                             {"type": "end_session", "reason": "transfer",
                              "escalated": True}])
  assert any("nobody was escalated" in w for w in warnings), warnings


def test_the_validator_warns_when_the_call_is_reported_as_completed():
  _errors, warnings = _lint([dict(LIVE_UJET_PART),
                             {"type": "end_session", "reason": "completed",
                              "escalated": True}])
  assert any("did not finish here" in w for w in warnings), warnings


def test_the_validator_reads_a_ujet_payload_missing_its_routing_fields():
  errors, _ = _lint([{"type": "payload", "data": {"ujet": {"type": "action"}}},
                     dict(LIVE_UJET_END)])
  assert any("missing ['action', 'escalation_reason', 'menu_id']" in e
             for e in errors), errors


def test_the_pairing_rule_reaches_an_exhaust_response():
  """A task exhaust and a slot exhaust are two of the four places a hand-off is
  emitted from, and neither response list was validated at all before."""
  cfg = {
      "slots": [{"name": "otp", "source": "user", "setter": "set_otp",
                 "ask": "What's the code?",
                 "validation": {"max_retries": 3,
                                "on_exhaust": {"say": "Connecting you.",
                                               "response": [dict(LIVE_UJET_PART)]}}}],
      "tasks": [],
  }
  _valid, errors, _warnings = raw_validate_single(cfg)
  assert any("hand-off payload with no 'end_session'" in e for e in errors), errors


def test_the_pairing_rule_reaches_the_escalate_block():
  cfg = {
      "slots": [{"name": "topic", "source": "user", "setter": "set_topic",
                 "ask": "What can I help with?"}],
      "tasks": [],
      "escalate": {"say": "Connecting you.", "response": [dict(LIVE_UJET_PART)]},
  }
  _valid, errors, _warnings = raw_validate_single(cfg)
  assert any("hand-off payload with no 'end_session'" in e for e in errors), errors


def test_a_handoff_and_a_sibling_transfer_cannot_both_be_the_disposition():
  cfg = {
      "slots": [{"name": "topic", "source": "user", "setter": "set_topic",
                 "ask": "What can I help with?"}],
      "tasks": [],
      "escalate": {"say": "Connecting you.", "transfer_to": "Host",
                   "response": [dict(LIVE_UJET_PART), dict(LIVE_UJET_END)]},
  }
  _valid, errors, _warnings = raw_validate_single(cfg)
  assert any("transfer_to 'Host'" in e for e in errors), errors


def test_an_authored_handoff_validates_clean_end_to_end():
  f = flows.Flow("support", root_agent="Support_Agent")
  f.add(flows.user_slot("topic", ask="What can I help with?"))
  f.set("escalate", flows.escalate(
      say="Let me get you to someone who can help.", handoff=_human()))
  app = flows.App(root_flow=f, app_display_name="t")
  errors, _warnings = flows.validate_app(app)
  assert errors == [], errors


# ── the engine ───────────────────────────────────────────────────────────────


def _escalate_now(config):
  """Drive the escalate rail and return the disposition action."""
  engine, sm = fb.load_engine(), fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = config.get("config_id", "support")
    sm["filled"][gate] = config.get("config_id", "support")
  sm["pending"]["escalate"] = True
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "",
      "scanned_user_text": "", "is_inactivity": False, "event_data": {},
      "config_id": "support", "n_user_turns": 1,
  })["action"]


def _support_flow(escalate_block):
  f = flows.Flow("support", root_agent="Support_Agent")
  f.add(flows.user_slot("topic", ask="What can I help with?"))
  f.set("escalate", escalate_block)
  return flows.App(root_flow=f, app_display_name="t").root_flow.to_config()


def test_the_escalate_disposition_delivers_the_payload():
  """Without this the rail spoke a friendly line and closed the call, and nothing ever
  told the platform a human was needed."""
  action = _escalate_now(_support_flow(flows.escalate(
      say="Let me get you to someone who can help.", handoff=_human())))
  assert action["message"] == "Let me get you to someone who can help."
  assert action["response"] == [LIVE_UJET_PART, LIVE_UJET_END]


def test_an_escalate_without_a_handoff_ends_exactly_as_it_always_did():
  """The whole feature is opt-in: a block with no `response` emits the same bytes."""
  action = _escalate_now(_support_flow(flows.escalate(say="Sorry about that.")))
  assert action["response"] == [
      {"type": "end_session", "reason": "transfer", "escalated": True}]


def test_the_engine_appends_the_end_a_hand_written_block_forgot():
  """Belt and braces behind the validator: a payload that reaches the engine without
  its end still cannot leave the caller on a call nobody is coming to."""
  config = _support_flow({"say": "Connecting you.", "outcome": "escalated",
                          "response": [dict(LIVE_UJET_PART)]})
  action = _escalate_now(config)
  assert action["response"][0] == LIVE_UJET_PART
  assert action["response"][-1]["type"] == "end_session"


def test_the_payload_interpolates_the_slots_it_was_authored_with():
  """A Dialogflow target is routinely loaded from a backend on the call, so the parts
  have to be resolved BEFORE the teardown throws the flow scope away."""
  config = _support_flow(flows.escalate(
      say="Transferring you now.",
      handoff=flows.handoff(flows.dialogflow_cx(
          project="{project_id}", location="us", agent_id="a"))))
  engine, sm = fb.load_engine(), fb.seed_sm(config)
  sm["filled"], sm["pending"] = {"project_id": "acme-prod"}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "support"
    sm["filled"][gate] = "support"
  sm["pending"]["escalate"] = True
  action = engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "",
      "scanned_user_text": "", "is_inactivity": False, "event_data": {},
      "config_id": "support", "n_user_turns": 1,
  })["action"]
  assert action["response"][0]["data"]["transferToDialogflow"] == (
      "projects/acme-prod/locations/us/agents/a")
