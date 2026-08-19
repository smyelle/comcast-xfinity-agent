"""render_config_source: Config -> Flows-DSL source renderer.

Covers the three renderer contracts the migration backend relies on:
  * ROUND-TRIP: exec(source); ns['flow'].to_config() == config (order preserved).
  * DETERMINISM: two calls return byte-identical source.
  * IDIOMATIC: high-level builders (intent_slot/user_slot+validation=) are used, not
    raw({...}), when the builder reproduces the dict.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_render.py
"""

from __future__ import annotations

import flows
from flows import (
    Flow,
    Operation,
    announce,
    has,
    intent_slot,
    journey,
    result_slot,
    router_flow,
    task,
    user_slot,
)


def _exec_flow(src: str) -> flows.Flow:
  ns: dict = {}
  exec(src, ns)  # noqa: S102 — exercising the rendered module is the whole point
  return ns["flow"]


# --- fixtures: hand-built Flow configs across every rendered shape -----------
def _user_slot_flow() -> Flow:
  f = Flow("us_demo", root_agent="US_Agent", bootstrap={"welcome_slot": "welcome"})
  f.add(
      announce("welcome", ["Hi there."], shared=True),
      user_slot("tracking_number", "What's your tracking number?"),
      result_slot("status_msg", "lookup"),
      announce("status", ["{status_msg}"], requires=["status_msg"], end=True),
  )
  f.task("lookup", "do_lookup", ["tracking_number"], "status_msg",
         condition=has("tracking_number"))
  return f


def _intent_slot_flow() -> Flow:
  f = Flow("intent_demo", root_agent="Intent_Agent")
  f.add(intent_slot("journey_intent", {"pay": ["pay my bill"], "refund": ["refund"]}))
  return f


def _mined_validation_flow() -> Flow:
  # A mined error-code ladder (NOT the reprompts/max_retries/on_exhaust shape) — the
  # renderer must pass it verbatim via `validation=` rather than downgrade to raw({...}).
  mined = {
      "max_retries": 3,
      "errors": {"invalid_length": "That should be 5 digits."},
      "on_exhaust": {"say": "Let me get someone.", "then": "escalate"},
  }
  f = Flow("mined_demo", root_agent="Mined_Agent")
  f.add(user_slot("zip", "What's your ZIP?", validation=mined))
  return f


def _journey_flow() -> Flow:
  ops = [
      Operation("pay", ["pay my bill"],
                slots=[user_slot("amount", "How much?")],
                tasks=[task("pay_task", "do_pay", ["amount"], "pay_res", terminal=True)]),
      Operation("refund", ["refund"],
                tasks=[task("refund_task", "do_refund", ["acct"], "refund_res",
                            terminal=True)]),
  ]
  return journey("journey_demo", spine=[user_slot("acct", "Account?")],
                 operations=ops, parent="Host_Agent")


def _router_flow() -> Flow:
  return router_flow(
      "router_demo", ["pay", "refund"],
      route_cues={"pay": ["pay my bill"], "refund": ["get a refund"]},
      intent_slot=intent_slot("journey_intent",
                              {"pay": ["pay"], "refund": ["refund"]}, passive=True),
  )


_ALL = {
    "user_slot": _user_slot_flow,
    "intent_slot": _intent_slot_flow,
    "mined_validation": _mined_validation_flow,
    "journey": _journey_flow,
    "router": _router_flow,
}


# --- (a) ROUND-TRIP ---------------------------------------------------------
def test_round_trip_every_shape():
  for label, make in _ALL.items():
    f = make()
    cfg = f.to_config()
    src = flows.render_config_source(cfg, config_id=f.config_id,
                                     root_agent=f.root_agent)
    rebuilt = _exec_flow(src).to_config()
    # Order-sensitive equality — the renderer promises byte-for-byte, order preserved.
    assert list(rebuilt.items()) == list(cfg.items()), label


def test_round_trip_preserves_root_agent():
  f = _user_slot_flow()
  src = flows.render_config_source(f.to_config(), config_id=f.config_id,
                                   root_agent="US_Agent")
  assert 'root_agent="US_Agent"' in src


# --- (b) DETERMINISM --------------------------------------------------------
def test_determinism_identical_bytes():
  for make in _ALL.values():
    cfg = make().to_config()
    a = flows.render_config_source(cfg, config_id="x", root_agent="A")
    b = flows.render_config_source(cfg, config_id="x", root_agent="A")
    assert a == b


# --- (c) IDIOMATIC (builders used, not raw) ---------------------------------
def _imports_raw(src: str) -> bool:
  # The module docstring literally mentions "raw({...})", so grep the import block —
  # `raw` is imported only when a slot/task actually fell back to raw({...}).
  return "    raw,\n" in src


def test_intent_slot_is_idiomatic():
  cfg = _intent_slot_flow().to_config()
  src = flows.render_config_source(cfg, config_id="intent_demo")
  assert "intent_slot(" in src
  assert not _imports_raw(src)


def test_mined_validation_is_idiomatic():
  cfg = _mined_validation_flow().to_config()
  src = flows.render_config_source(cfg, config_id="mined_demo")
  assert "user_slot(" in src
  assert "validation=" in src
  assert not _imports_raw(src)


# --- (d) NO CODE INJECTION VIA THE ONE NON-LITERAL INTERPOLATION -------------
def test_config_id_cannot_break_out_of_the_module_docstring():
  """A `config_id` carrying `\"\"\"` must not become executable code.

  The renderer's output is fed straight to `exec(compile(...))` by the migration backend's
  round-trip gate and by `materialize.build_cxas_app`. Every Config *value* goes through
  `_str_lit`, but `config_id` is also interpolated into the module docstring, where `repr`
  alone does NOT escape a triple quote — so it used to close the docstring early and turn
  whatever followed into top-level statements. config_id is data (the migration backend builds
  it from an op name mined from the scanned agent), not a compiler constant, so this is a
  reachable path and not a hypothetical one.
  """
  cfg = _user_slot_flow().to_config()
  hit = []
  evil = 'boom""" and __hit__() or """'
  src = flows.render_config_source(cfg, config_id=evil, root_agent="A")
  ns = {"__hit__": lambda: hit.append(1)}
  exec(compile(src, "<evil>", "exec"), ns)  # noqa: S102 - that is the thing under test
  assert not hit, "config_id escaped the docstring and executed injected code"
  assert ns["flow"].config_id == evil     # ...and the id still round-trips verbatim
  assert ns["flow"].to_config() == cfg


def test_ordinary_config_ids_render_unchanged_by_the_docstring_escaping():
  """The escaping is inert for every id that does not contain a quote (i.e. all real ones)."""
  cfg = _user_slot_flow().to_config()
  src = flows.render_config_source(cfg, config_id="pay_bill_dag", root_agent="A")
  assert src.startswith('"""Flows-SDK authoring source for \'pay_bill_dag\' ')


# --- (e) TELEPHONY HAND-OFF: `handoff=`, not forty lines of raw({...}) -------
#
# A payload-bearing announce used to fall straight to `raw({...})`, because every
# kwarg the renderer could emit was a LITERAL and a hand-off is a nested builder
# call. That was left open on the grounds that rendering one is its own
# byte-stability surface — but the renderer's contract is builder-match-or-raw: it
# CALLS the real builder and compares `list(d.items())`, so a candidate that does
# not reproduce the dict exactly is rejected by construction. These pin both
# halves: the idiomatic form is emitted, and everything it cannot rebuild still
# falls back to raw.


def _handoff_flow(**announce_kw) -> Flow:
  f = Flow("ho_demo", root_agent="HO_Agent")
  f.add(
      flows.event_slot("account"),
      announce("human_transfer", ["Connecting you now. Please hold."],
               requires=["account"], **announce_kw),
  )
  return f


def _rendered(f: Flow) -> str:
  return flows.render_config_source(f.to_config(), config_id="ho_demo",
                                    root_agent="HO_Agent")


def _body(src: str) -> str:
  """Just the `flow.add(...)` block — the module docstring mentions raw({...})."""
  return src.split("flow.add(", 1)[1].split("\n)", 1)[0]


def test_a_ujet_handoff_announce_renders_as_handoff_not_raw():
  src = _rendered(_handoff_flow(handoff=flows.handoff(flows.ujet(menu_id="90"))))

  assert 'handoff=handoff(ujet(menu_id="90"))' in _body(src)
  assert "raw(" not in _body(src)
  # The nested builders are imported, and only the ones actually emitted.
  assert "    handoff,\n" in src and "    ujet,\n" in src
  assert "dialogflow_cx" not in src


def test_a_dialogflow_handoff_renders_through_its_own_builder():
  src = _rendered(_handoff_flow(handoff=flows.handoff(flows.dialogflow_cx(
      project="eqfx-prod", location="us", agent_id="0d1b-4a",
      parameters={"ani": "{ani}"}))))

  assert "dialogflow_cx(" in _body(src) and "raw(" not in _body(src)
  assert 'agent="projects/eqfx-prod/locations/us/agents/0d1b-4a"' in src
  assert 'parameters={"ani": "{ani}"}' in src


def test_a_cxas_handoff_renders_through_its_own_builder():
  src = _rendered(_handoff_flow(handoff=flows.handoff(flows.cxas(
      project="ces-deployment-dev", location="us", app_id="7a56090b",
      variables={"account_number": "{account_number}"}))))

  assert "cxas(" in _body(src) and "raw(" not in _body(src)
  assert 'app="projects/ces-deployment-dev/locations/us/apps/7a56090b"' in src
  assert 'variables={"account_number": "{account_number}"}' in src


def test_an_unrecognized_vendor_still_renders_as_a_paired_handoff():
  """The pair is the invariant, not the vendor: a payload with no builder rides as
  a raw dict INSIDE handoff(), which still emits the end_session with it."""
  src = _rendered(_handoff_flow(
      handoff=flows.handoff({"genesys": {"queue": "billing"}}, escalated=True)))

  assert ('handoff=handoff({"genesys": {"queue": "billing"}}, escalated=True)'
          in _body(src))
  assert "end_session" not in _body(src)  # the pair is the builder's job


def test_a_surface_gated_handoff_renders_the_surface_kwarg():
  src = _rendered(_handoff_flow(
      handoff=flows.handoff(flows.ujet(menu_id="90"), surface="voice")))

  assert 'surface="voice"' in _body(src) and "condition=" not in _body(src)


def test_non_default_vendor_fields_and_extras_survive_the_round_trip():
  f = _handoff_flow(handoff=flows.handoff(flows.ujet(
      menu_id="12", escalation_reason="fraud_hold", language="es",
      action="transfer", message_type="event", extra={"tenant": "eqfx"})))
  src = _rendered(f)

  assert "ujet(" in _body(src) and "raw(" not in _body(src)
  assert 'extra={"tenant": "eqfx"}' in _body(src)
  # `action="transfer"` is not an escalation, so the pair's end_session is not
  # marked escalated — and the renderer must not invent the flag back.
  assert "escalated" not in _body(src)
  assert _exec_flow(src).to_config() == f.to_config()


def test_every_handoff_shape_round_trips_byte_exact():
  """The contract that makes all of the above safe."""
  for ho in (
      flows.handoff(flows.ujet(menu_id="90")),
      flows.handoff(flows.ujet(menu_id="90"), surface="voice"),
      flows.handoff(flows.ujet(menu_id="90"), reason="completed", escalated=False),
      flows.handoff(flows.dialogflow_cx(agent="projects/p/locations/us/agents/a")),
      flows.handoff(flows.cxas(app="projects/p/locations/us/apps/a")),
      flows.handoff(flows.cxas(app="projects/p/locations/us/apps/a",
                              variables={"acct": "{acct}"})),
      flows.handoff({"five9": {"skill": "care"}}, escalated=False),
  ):
    f = _handoff_flow(handoff=ho)
    src = _rendered(f)
    assert _exec_flow(src).to_config() == f.to_config(), src


def test_a_split_pair_falls_back_to_raw():
  """`handoff(surface=/condition=)` gates BOTH parts on purpose, so a config whose
  payload and end_session carry different conditions has no builder form — and the
  renderer must say so with raw({...}) rather than quietly re-joining them."""
  f = _handoff_flow()
  slot = f.to_config()["slots"][1]
  slot["response"] = [
      {"type": "text", "text": "Connecting you now. Please hold."},
      {"type": "payload", "data": {"ujet": {"menu_id": "90", "action": "escalation"}},
       "condition": {"surface": "voice"}},
      {"type": "end_session", "reason": "transfer", "escalated": True},
  ]
  cfg = {**f.to_config(), "slots": [f.to_config()["slots"][0], slot]}
  src = flows.render_config_source(cfg, config_id="ho_demo", root_agent="HO_Agent")

  assert "raw(" in _body(src) and "handoff=" not in _body(src)
  assert _exec_flow(src).to_config() == cfg


def test_a_payload_with_no_end_session_falls_back_to_raw():
  """The defect `flows.handoff` exists to prevent. The renderer must not launder an
  unpaired payload into a builder call that would silently ADD the missing end."""
  f = _handoff_flow()
  base = f.to_config()
  slot = dict(base["slots"][1])
  slot["response"] = [
      {"type": "text", "text": "Connecting you now. Please hold."},
      {"type": "payload", "data": {"ujet": {"menu_id": "90"}}},
  ]
  cfg = {**base, "slots": [base["slots"][0], slot]}
  src = flows.render_config_source(cfg, config_id="ho_demo", root_agent="HO_Agent")

  assert "raw(" in _body(src) and "handoff=" not in _body(src)
  assert _exec_flow(src).to_config() == cfg


def test_rendering_a_handoff_is_deterministic():
  f = _handoff_flow(handoff=flows.handoff(flows.ujet(menu_id="90")))
  assert _rendered(f) == _rendered(f)


def test_a_builder_that_refuses_a_candidate_falls_back_instead_of_crashing():
  """A candidate is a GUESS; a builder rejecting it is a raw({...}), not a stack trace.

  `{"slot": x, "neq": y, "default": ""}` is a real mined shape — `default` is dead on
  a non-numeric comparison and the DSL refuses to pretend otherwise. The renderer
  used to call the builder unguarded, so ONE such condition took down the whole
  render of an agent whose slot was always going to fall back anyway.
  """
  cfg = {"slots": [{
      "name": "human_transfer",
      "source": "announce",
      "requires": ["agent_escalation"],
      "condition": {"slot": "after_hours", "neq": "true", "default": ""},
      "response": [
          {"type": "text", "text": "Connecting you now."},
          {"type": "payload", "data": {"ujet": {"menu_id": "90"}}},
          {"type": "end_session", "reason": "transfer", "escalated": True},
      ],
  }]}
  src = flows.render_config_source(cfg, config_id="human", root_agent="Human_Agent")

  assert "raw(" in _body(src)
  assert _exec_flow(src).to_config()["slots"] == cfg["slots"]


def test_a_builder_that_raises_a_keyerror_also_falls_back():
  """Same refusal, arriving as `KeyError` rather than `TypeError`/`ValueError`.

  Not every rejection is a deliberate `raise`. `user_slot` indexes the reprompt
  ladder it was promised is a list (`reprompts[0]`), so a mined `reprompts` that is
  a DICT refuses with `KeyError: 0` — and the guard, catching only
  `(TypeError, ValueError)`, let it escape and kill the whole render over one slot
  that was headed for `raw({...})` regardless.
  """
  slot = {
      "name": "acct",
      "source": "user",
      "setter": "set_acct",
      "hint": "acct",
      "ask": "Account number?",
      "validation": {"max_retries": 3, "reprompts": {"0": "Again?"},
                     "on_exhaust": {"say": "Sorry."}},
  }
  src = flows.render_config_source({"slots": [slot]}, config_id="acct",
                                   root_agent="Acct_Agent")

  assert "raw(" in _body(src)
  assert _exec_flow(src).to_config()["slots"] == [slot]


def test_a_candidate_that_cannot_read_the_dict_also_falls_back():
  """The reverser is guarded too — it reads keys a MINED config need not carry.

  `on_exhaust` as the bare string `"escalate"` (rather than `{"then": "escalate"}`)
  made `_candidate_user_slot` do `"escalate".get("say")`. That crash is one step
  ahead of the builder guard, so widening the builder guard alone would not have
  caught it.
  """
  slot = {
      "name": "pin",
      "source": "user",
      "setter": "set_pin",
      "hint": "pin",
      "ask": "PIN?",
      "validation": {"max_retries": 2, "reprompts": ["a", "b"],
                     "on_exhaust": "escalate"},
  }
  src = flows.render_config_source({"slots": [slot]}, config_id="pin",
                                   root_agent="Pin_Agent")

  assert "raw(" in _body(src)
  assert _exec_flow(src).to_config()["slots"] == [slot]


def test_an_unknown_builder_name_is_still_a_loud_bug():
  """Widening the guard must not swallow OUR errors: a missing registry row is a
  defect in this module, not a config the builder declined."""
  import pytest

  from flows.authoring import render as _render

  with pytest.raises(KeyError):
    _render._build("no_such_builder", [], {})
  with pytest.raises(KeyError):
    _render._expr("no_such_builder")
