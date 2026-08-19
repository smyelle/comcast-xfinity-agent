"""Residual error paths of the two central authoring modules (`build.py`, `dsl.py`).

Both modules are already exercised broadly by the rest of the suite; what is left
uncovered is almost entirely REFUSAL — the branch that names a mis-wired app before it
can reach a caller. Those are the branches worth pinning, because an untested refusal
is indistinguishable from no refusal at all until the day it lets something through.

The bias here is deliberately object-level: build the dict/dataclass and read it back,
or call the private checker with the smallest input that trips it. Full emits are used
only where the line under test lives in the emit path itself (writing
`global_instruction`, scoping tools, injecting `languageSettings`). Every test is
offline and writes only under `tmp_path`.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_build_dsl_coverage.py
"""

from __future__ import annotations

import json
import os

import pytest

import flows
from flows.authoring import build as _build
from flows.authoring import dsl
from flows.authoring import tools as _tools


# ===========================================================================
# fixtures / builders
# ===========================================================================

@pytest.fixture(autouse=True)
def registry():
  """The tool registry is a process-wide global shared by every test file — a tool
  registered here must not leak into another one."""
  saved = dict(_tools._REGISTRY)  # noqa: SLF001
  try:
    yield _tools._REGISTRY  # noqa: SLF001
  finally:
    _tools._REGISTRY.clear()  # noqa: SLF001
    _tools._REGISTRY.update(saved)  # noqa: SLF001


# Read by `acme_left_behind` below. It has to be a REAL module-level name: the check
# reports a name only when it resolves in the author's own module, so that a typo or a
# forward reference is not mistaken for a constant being left behind.
from os import sep as _ACME_IMPORTED_SEP  # noqa: E402

_ACME_LIMIT = 5


def _flow(cid: str = "acme_cov", agent: str = "Widget_Agent") -> flows.Flow:
  """A minimal, VALID single-agent flow: ask one thing, do one thing, say so."""
  f = flows.Flow(cid, root_agent=agent)
  f.add(
      flows.user_slot(f"{cid}_ref", "What's your reference number?"),
      flows.result_slot(f"{cid}_result", f"{cid}_lookup"),
      flows.announce("done", ["Reference {%s_ref} is on file." % cid],
                     requires=[f"{cid}_result"], end=True),
  )
  f.task(f"{cid}_lookup", f"look_up_{cid}", [f"{cid}_ref"], f"{cid}_result",
         condition=flows.has(f"{cid}_ref"))
  return f


def _app(**kw) -> flows.App:
  kw.setdefault("root_flow", _flow())
  kw.setdefault("app_display_name", "Acme Coverage")
  return flows.App(**kw)


def _sub_agent(name: str, cid: str, **kw) -> flows.Agent:
  return flows.Agent(name, flow=_flow(cid, name), **kw)


def _multi_app(**kw) -> flows.App:
  a = _sub_agent("Widget_Agent", "widgets")
  b = _sub_agent("Gadget_Agent", "gadgets")
  host = flows.HostRouter("Acme_Host", routes={"widgets": a, "gadgets": b})
  kw.setdefault("host", host)
  kw.setdefault("agents", [a, b])
  kw.setdefault("app_display_name", "Acme Multi")
  return flows.App(**kw)


# ===========================================================================
# dsl.py — declarative condition grammar (`gate`)
# ===========================================================================

def test_a_non_dict_fragment_inside_a_combinator_is_named_by_position():
  """The recursion has to reject a non-dict SUB-condition, and say which one: an
  `all` whose second arm is a bare string reads as a gate on a slot named by that
  string, and would otherwise reach the config as a permanently-shut leaf."""
  with pytest.raises(ValueError) as exc:
    flows.gate({"all": [{"slot": "verified", "eq": "yes"}, "balance_due"]})
  assert "<root>.all[1]" in str(exc.value)
  assert "expected a dict, got str" in str(exc.value)


def test_gate_rejects_a_bare_non_dict():
  with pytest.raises(ValueError, match="expected a dict, got list"):
    flows.gate(["slot", "eq"])


def test_a_capability_leaf_must_name_its_capability_as_a_string():
  with pytest.raises(ValueError) as exc:
    flows.gate({"capability": ["payloads"]})
  assert "'capability' must be a string, got list" in str(exc.value)


def test_a_surface_leaf_must_name_its_surface_as_a_string():
  with pytest.raises(ValueError, match="'surface' must be a string, got int"):
    flows.gate({"surface": 7})


def test_a_surface_leaf_rejects_an_unknown_key():
  """The leaf-key whitelist runs on the surface/capability branch too — a typo'd
  operator there is a gate that reads as conditional and is in fact constant."""
  with pytest.raises(ValueError) as exc:
    flows.gate({"surface": "voice", "equals": "voice"})
  assert "unknown condition key(s) ['equals']" in str(exc.value)


def test_a_capability_leaf_takes_at_most_one_operator():
  with pytest.raises(ValueError) as exc:
    flows.gate({"capability": "payloads", "eq": True, "neq": False})
  assert "leaf condition has 2 operators ['eq', 'neq']" in str(exc.value)


def test_the_surface_leaf_rules_are_not_vacuous():
  """The three refusals above all sit on the same branch as these, which must pass
  unchanged and come back byte-identical (`gate` returns the dict, not a copy)."""
  for spec in ({"surface": "voice"},
               {"capability": "payloads"},
               {"capability": "keypad", "eq": True}):
    assert flows.gate(spec) is spec


def test_a_capability_leaf_cannot_also_read_a_slot():
  with pytest.raises(ValueError, match="reads 'capability' or 'slot', not both"):
    flows.gate({"capability": "payloads", "slot": "verified"})


def test_an_unknown_capability_names_the_valid_ones():
  with pytest.raises(ValueError) as exc:
    flows.gate({"capability": "telepathy"})
  assert "unknown capability 'telepathy'" in str(exc.value)
  assert "'payloads'" in str(exc.value)


def test_eq_ne_has_unset_compose_only_by_and():
  """The four helpers are lambda SOURCE, and the only composition the engine can
  evaluate is a conjunction spliced into one lambda."""
  assert flows.eq("a", 1) == "lambda f: f.get('a') == 1"
  assert flows.ne("a", 1) == "lambda f: f.get('a') != 1"
  assert flows.has("a") == "lambda f: bool(f.get('a'))"
  assert flows.unset("a") == "lambda f: not f.get('a')"
  both = dsl._and_conditions(flows.has("a"), flows.eq("b", "x"))  # noqa: SLF001
  assert both == "lambda f: (bool(f.get('a'))) and (f.get('b') == 'x')"
  # A lambda-source string and a declarative dict cannot be ANDed together.
  assert dsl._and_conditions(None, flows.has("a")) == flows.has("a")  # noqa: SLF001


# ===========================================================================
# dsl.py — slot factories
# ===========================================================================

def test_a_user_slot_can_opt_out_of_automatic_fillers():
  """A build-time marker, stripped before validation — but it has to be ON the slot
  for the pass to see it, and its absence silently opts the slot back in."""
  s = flows.user_slot("acme_pin", "What's your PIN?", automatic_fillers=False)
  assert s["automatic_fillers"] is False
  assert "automatic_fillers" not in flows.user_slot("acme_pin", "What's your PIN?")


def test_an_intent_slot_carries_its_dtmf_map():
  s = flows.intent_slot("acme_topic", {"pay": ["pay"], "refund": ["refund"]},
                        dtmf={"1": "pay", "2": "refund"})
  assert s["dtmf_map"] == {"1": "pay", "2": "refund"}


def test_an_intent_slot_exhaust_fill_must_be_one_of_the_options():
  """`on_exhaust_fill` lands the slot somewhere when the cues cannot resolve it. A
  value outside the enum lands it on something the enum rule then rejects."""
  with pytest.raises(ValueError) as exc:
    flows.intent_slot("acme_topic", {"pay": ["pay"], "refund": ["refund"]},
                      on_exhaust_fill="cancel")
  assert "on_exhaust_fill 'cancel' is not one of ['pay', 'refund']" in str(exc.value)


def test_an_intent_slot_exhaust_fill_inside_the_enum_is_accepted():
  s = flows.intent_slot("acme_topic", {"pay": ["pay"], "refund": ["refund"]},
                        on_exhaust_fill="pay")
  assert s["validation"]["on_exhaust"]["fill"] == "pay"


def test_a_passive_slot_carries_cue_priority_multi_fill_and_requires():
  s = flows.passive_slot(
      "acme_reason",
      option_cues={"billing": ["bill"], "tech": ["broken"]},
      kind="intent",
      cue_priority="high",
      multi_fill=True,
      requires=["acme_cov_ref"],
  )
  assert s["cue_priority"] == "high"
  assert s["multi_fill"] is True
  assert s["requires"] == ["acme_cov_ref"]
  assert s["kind"] == "intent"
  assert s["validation_rules"] == [{"kind": "enum", "detail": "billing|tech"}]
  assert s["setter"] == "set_acme_reason"  # a falsy setter is DROPPED by the engine


def test_a_passive_slot_emits_none_of_the_three_unless_asked():
  s = flows.passive_slot("acme_reason")
  assert "cue_priority" not in s and "multi_fill" not in s and "requires" not in s


# ===========================================================================
# dsl.py — announce
# ===========================================================================

def _handoff():
  return flows.handoff(flows.ujet(menu_id="4242"))


def test_an_announce_handoff_refuses_a_competing_disposition_reason():
  """`reason`/`escalated` describe the `end=True` end_session, which a hand-off
  supplies itself. Accepting them here would read as configured and be ignored."""
  with pytest.raises(ValueError) as exc:
    flows.announce("bye", ["One moment."], handoff=_handoff(), escalated=True)
  assert "reason=/escalated= describe the end=True end_session" in str(exc.value)
  with pytest.raises(ValueError, match="reason=/escalated="):
    flows.announce("bye", ["One moment."], handoff=_handoff(), reason="transfer")


def test_an_announce_handoff_without_a_competing_reason_is_accepted():
  a = flows.announce("bye", ["One moment."], handoff=_handoff())
  assert any(p.get("type") == "end_session" for p in a["response"])


def test_barge_in_false_marks_every_text_part_uninterruptable():
  a = flows.announce("legal", ["Line one.", "Line two."], barge_in=False)
  assert [p["interruptable"] for p in a["response"]] == [False, False]
  assert all("interruptable" not in p
             for p in flows.announce("legal", ["Line one."])["response"])


def test_announce_escalated_without_end_is_inert():
  """REPORTED, not fixed: `escalated=True` with no `end=True` emits no end_session
  and no escalated marker anywhere — the flag is silently dropped, so an author who
  writes it gets an ordinary completed disposition."""
  inert = flows.announce("bye", ["Goodbye."], escalated=True)
  live = flows.announce("bye", ["Goodbye."], escalated=True, end=True)
  assert inert == flows.announce("bye", ["Goodbye."])  # the flag changed nothing
  assert any(p.get("escalated") for p in live["response"])


# ===========================================================================
# dsl.py — readback / cancel / escalate / no_input / speech
# ===========================================================================

def test_readback_refuses_an_unknown_format_and_names_the_valid_ones():
  with pytest.raises(ValueError) as exc:
    flows.readback("phonetic", text="x")
  assert "unknown fmt_type 'phonetic'" in str(exc.value)
  assert "'digits'" in str(exc.value)


def test_readback_names_the_field_a_known_format_is_missing():
  with pytest.raises(ValueError, match=r"missing required field\(s\) \['other'\]"):
    flows.readback("plural", one="item")


def test_cancel_and_escalate_can_be_pinned_verbatim_against_a_speech_policy():
  assert flows.cancel(say="No problem.", verbatim=True)["verbatim"] is True
  assert flows.escalate(say="Connecting you.", verbatim=True)["verbatim"] is True
  assert flows.awaits(max_turns=3, verbatim=True)["verbatim"] is True
  assert "verbatim" not in flows.cancel(say="No problem.")
  assert "verbatim" not in flows.escalate(say="Connecting you.")
  assert "verbatim" not in flows.awaits(max_turns=3)


def test_cancel_as_a_step_back_records_what_it_un_decides():
  block = flows.cancel(say="Back to the menu.", end_conversation=False)
  assert block["end_conversation"] is False
  assert block["clear_slots"] == []  # empty means "ask the same thing again"


@pytest.mark.parametrize(
    "bad, fragment",
    [
        (42, "got int"),
        ({"when": "x", "say": "y"}, "got dict"),
    ],
)
def test_declined_say_must_be_a_line_a_ladder_or_a_list_of_reasons(bad, fragment):
  with pytest.raises(ValueError) as exc:
    flows.escalate(say="Connecting you.", declined_say=bad)
  assert fragment in str(exc.value)
  assert "escalate():" in str(exc.value)


def test_an_empty_declined_say_list_is_refused_rather_than_silent():
  with pytest.raises(ValueError, match=r"declined_say=\[\] says nothing"):
    flows.escalate(say="Connecting you.", declined_say=[])


def test_a_declined_say_ladder_must_hold_non_empty_lines():
  with pytest.raises(ValueError, match="ladder must hold non-empty lines"):
    flows.escalate(say="Connecting you.",
                   declined_say=["I can't do that.", ""])


def test_a_reason_say_ladder_must_hold_non_empty_lines():
  with pytest.raises(ValueError) as exc:
    flows.escalate(say="Connecting you.", declined_say=[
        {"when": flows.has("acme_fault"), "say": ["No agent can fix this.", ""]},
    ])
  assert "declined_say[0]['say'] must be a line or a ladder of" in str(exc.value)


def test_a_reason_say_must_be_a_line_or_a_ladder():
  with pytest.raises(ValueError) as exc:
    flows.escalate(say="Connecting you.", declined_say=[
        {"when": flows.has("acme_fault"), "say": 7},
    ])
  assert "declined_say[0]['say'] must be a line or a ladder of lines" in str(exc.value)


def test_a_reason_list_entry_must_be_a_line_or_a_reason():
  """One dict in the list turns the whole thing into a reason list, so a stray
  non-string, non-dict entry is refused by position."""
  with pytest.raises(ValueError) as exc:
    flows.escalate(say="Connecting you.", declined_say=[
        {"when": flows.has("acme_fault"), "say": "No agent can fix this."},
        ["not", "a", "reason"],
    ])
  assert "declined_say[1] must be a line or a" in str(exc.value)


def test_a_well_formed_reason_list_survives_all_of_that():
  block = flows.escalate(say="Connecting you.", declined_say=[
      {"when": flows.has("acme_fault"), "say": "No agent can fix this."},
      {"say": "I can't transfer you right now."},
  ])
  assert len(block["declined_say"]) == 2


def test_escalate_refuses_a_handoff_and_a_transfer_together():
  """One leaves the app, one stays inside it. Both cannot happen."""
  with pytest.raises(ValueError) as exc:
    flows.escalate(say="Connecting you.", handoff=_handoff(),
                   transfer_to="Acme_Host")
  assert "pass handoff= or transfer_to=, not both" in str(exc.value)


def test_no_input_hold_phrases_are_taken_verbatim_when_given():
  policy = flows.no_input(
      reprompts=["Are you still there?"],
      on_exhaust={"say": "I'll let you go.", "end_conversation": True},
      hold_phrases=["hold on", "give me a second"],
  )
  assert policy["hold_phrases"] == ["hold on", "give me a second"]


def test_no_input_defaults_the_hold_phrases_when_only_an_ack_is_given():
  """An ack nothing can trigger is dead config, so the phrases are supplied."""
  policy = flows.no_input(
      reprompts=["Are you still there?"],
      on_exhaust={"end_conversation": True},
      hold_ack="Take your time.",
  )
  assert policy["hold_phrases"] == list(dsl.DEFAULT_HOLD_PHRASES)
  assert policy["hold_ack"] == "Take your time."


def test_hold_and_wait_carries_the_ack_it_was_given():
  policy = flows.hold_and_wait(
      reprompts=["Are you still there?"],
      hold_ack="Take your time — I'm here.",
      say="I'll let you go for now.",
  )
  assert policy["hold_ack"] == "Take your time — I'm here."
  assert "hold_ack" not in flows.hold_and_wait(reprompts=["Still there?"],
                                               say="I'll let you go.")


def test_a_speech_policy_that_improvises_nothing_is_refused():
  """An empty `improvise` reads as "a speech policy is configured" and does nothing."""
  with pytest.raises(ValueError) as exc:
    flows.speech(improvise=[])
  assert "names no classes, so nothing would be improvised" in str(exc.value)


def test_a_speech_policy_naming_a_real_class_is_accepted():
  assert flows.speech(improvise=["reprompt"])["improvise"] == ["reprompt"]


# ===========================================================================
# dsl.py — task / parallel
# ===========================================================================

def test_a_task_carries_its_success_side_effects_and_latency_cover():
  t = flows.task(
      "acme_pay", "take_payment", ["acme_amount"], "acme_receipt",
      clear_slots_on_success=["acme_amount"],
      while_running={"kind": "hold_music"},
      automatic_fillers=False,
  )
  assert t["clear_slots_on_success"] == ["acme_amount"]
  assert t["while_running"] == {"kind": "hold_music"}
  assert t["automatic_fillers"] is False
  plain = flows.task("acme_pay", "take_payment", ["acme_amount"], "acme_receipt")
  assert not {"clear_slots_on_success", "while_running",
              "automatic_fillers"} & set(plain)


def _leg(name: str, tool: str, **kw) -> dict:
  return flows.task(name, tool, ["acme_ref"], f"{name}_out", **kw)


def test_a_parallel_group_of_one_is_refused():
  with pytest.raises(ValueError) as exc:
    flows.parallel("acme_fanout", tasks=[_leg("a", "look_up_a")])
  assert "a group needs at least two legs, got 1" in str(exc.value)


def test_a_parallel_leg_with_no_tool_is_refused():
  """A component descends into a child DAG and ends the pass, so it cannot ride a
  shared dispatch."""
  with pytest.raises(ValueError) as exc:
    flows.parallel("acme_fanout", tasks=[
        _leg("a", "look_up_a"),
        flows.component("acme_child", "acme_child_flow"),
    ])
  assert "has no tool" in str(exc.value)


def test_a_terminal_parallel_leg_is_refused():
  with pytest.raises(ValueError) as exc:
    flows.parallel("acme_fanout", tasks=[
        _leg("a", "look_up_a"),
        _leg("b", "look_up_b", terminal=True),
    ])
  assert "is terminal" in str(exc.value)


def test_two_parallel_legs_calling_one_tool_are_refused():
  """Results come back keyed by tool name, so two calls cannot be told apart."""
  with pytest.raises(ValueError) as exc:
    flows.parallel("acme_fanout", tasks=[
        _leg("a", "look_up_thing"),
        _leg("b", "look_up_thing"),
    ])
  assert "both call 'look_up_thing'" in str(exc.value)


def test_a_leg_deadline_that_disagrees_with_the_group_is_refused():
  with pytest.raises(ValueError) as exc:
    flows.parallel("acme_fanout", deadline=5, tasks=[
        _leg("a", "look_up_a", awaits=flows.awaits(max_turns=2)),
        _leg("b", "look_up_b"),
    ])
  assert "sets max_turns 2 but the group's deadline is 5" in str(exc.value)


def test_a_group_puts_its_latency_cover_on_the_first_leg():
  group = flows.parallel("acme_fanout", while_running={"kind": "hold_music"},
                         filler_say="Let me check a couple of things.", tasks=[
                             _leg("a", "look_up_a"),
                             _leg("b", "look_up_b"),
                         ])
  assert group.tasks[0]["while_running"] == {"kind": "hold_music"}
  assert group.tasks[0]["filler_say"] == "Let me check a couple of things."
  assert "while_running" not in group.tasks[1]


# ===========================================================================
# dsl.py — Flow.add / Flow.task / Flow.set
# ===========================================================================

def test_flow_set_refuses_an_unknown_policy_key_and_lists_the_valid_ones():
  with pytest.raises(ValueError) as exc:
    flows.Flow("acme_cov").set("cancle", {})
  assert "unknown flow policy key 'cancle'" in str(exc.value)
  assert "'cancel'" in str(exc.value)


def test_a_flow_policy_kwarg_goes_through_the_same_check():
  with pytest.raises(ValueError, match="unknown flow policy key 'escallate'"):
    flows.Flow("acme_cov", escallate={})


def test_add_preserves_order_and_keeps_a_duplicate_name():
  """`add` is an append, not a merge: two slots of one name both reach the config
  (the framework validator is what refuses them), and ask order IS list order."""
  f = flows.Flow("acme_cov")
  f.add(flows.user_slot("acme_a", "A?"), flows.user_slot("acme_b", "B?"))
  f.add(flows.user_slot("acme_a", "A again?"))
  assert [s["name"] for s in f.to_config()["slots"]] == ["acme_a", "acme_b", "acme_a"]


def test_a_spliced_task_dict_hands_its_search_tool_and_agent_tool_to_the_flow():
  """A `research()`/agent-tool call task rides its declaration in on a private key.
  Left on the dict it would put a python object into the emitted JSON."""
  searcher = flows.search_tool("acme_search", description="Search acme docs.")
  caller = flows.agent_tool("ask_acme", agent="Acme_Helper",
                            description="Answers acme policy questions.")
  spliced = {
      "name": "acme_research", "tool": "acme_search",
      "inputs": ["acme_q"], "outputs": {"result": "acme_answer"},
      dsl._SEARCH_TOOL_KEY: searcher,  # noqa: SLF001
      dsl._AGENT_TOOL_KEY: caller,  # noqa: SLF001
  }
  f = flows.Flow("acme_cov")
  f.task(spliced)
  assert f._search_tools == [searcher]  # noqa: SLF001
  assert f._agent_tools == [caller]  # noqa: SLF001
  emitted = f.to_config()["tasks"][0]
  assert dsl._SEARCH_TOOL_KEY not in emitted  # noqa: SLF001
  assert dsl._AGENT_TOOL_KEY not in emitted  # noqa: SLF001


def test_a_built_task_handed_an_agent_tool_declaration_lifts_it_off_too():
  """The `task(...)`-args branch of `Flow.task`, which is the one an author uses."""
  caller = flows.agent_tool("ask_acme", agent="Acme_Helper",
                            description="Answers acme policy questions.")
  f = flows.Flow("acme_cov")
  f.task("acme_ask", caller, ["acme_q"], "acme_answer")
  assert f._agent_tools == [caller]  # noqa: SLF001
  emitted = f.to_config()["tasks"][0]
  assert emitted["tool"] == "ask_acme"
  assert dsl._AGENT_TOOL_KEY not in emitted  # noqa: SLF001


# ===========================================================================
# dsl.py — router_flow / journey
# ===========================================================================

def test_author_route_cues_are_merged_over_the_generated_ones():
  """The route objects generate cues from their own `cues`; an explicit
  `route_cues` for the same key REPLACES that key's list rather than appending."""
  r1 = flows.route("widgets", "the caller wants a widget", cues=["widget"])
  r2 = flows.route("gadgets", "the caller wants a gadget", cues=["gadget"])
  f = flows.router_flow("acme_router", [r1, r2],
                        route_cues={"widgets": ["widget", "doohickey"]})
  cues = f.to_config()["route_cues"]
  assert cues["widgets"] == ["widget", "doohickey"]
  assert cues["gadgets"] == ["gadget"]


def test_a_router_over_no_flows_is_the_bare_key_form_with_nothing_in_it():
  """`_as_routes([])` has nothing to duck-type, so the bare-key path runs and the
  router emits an empty `flow_types` rather than raising."""
  cfg = flows.router_flow("acme_router", []).to_config()
  assert cfg["router"] is True
  assert cfg["flow_types"] == []
  assert cfg["bootstrap"]["tool"] == "set_active_flow"


def test_a_journey_spine_given_as_a_component_id_becomes_a_component_task():
  f = flows.journey(
      "acme_journey",
      spine="acme_identity",
      parent="Acme_Host",
      operations=[
          flows.Operation(
              value="pay", cues=["pay my bill"],
              tasks=[flows.task("acme_pay", "take_payment", ["acme_amount"],
                                "acme_receipt", terminal=True)],
              slots=[flows.user_slot("acme_amount", "How much?")]),
          flows.Operation(
              value="balance", cues=["what do I owe"],
              tasks=[flows.task("acme_balance", "read_balance", [], "acme_due",
                                terminal=True)]),
      ],
  )
  cfg = f.to_config()
  spine = cfg["tasks"][0]
  assert spine["name"] == "acme_journey_spine"
  assert spine["component"] == "acme_identity"
  # and the derived gates the journey exists to guarantee
  gates = {t["name"]: t.get("condition") for t in cfg["tasks"]}
  assert gates["acme_pay"] == flows.eq("journey_intent", "pay")
  assert gates["acme_balance"] == flows.eq("journey_intent", "balance")


# ===========================================================================
# dsl.py — HostRouter / App wiring + defaults
# ===========================================================================

def test_a_host_router_strategy_must_be_one_of_the_two():
  with pytest.raises(ValueError) as exc:
    flows.HostRouter("Acme_Host", routes={"widgets": _sub_agent("W", "w")},
                     strategy="receptionist")
  assert "must be 'transfer' or 'engine', got 'receptionist'" in str(exc.value)


def test_a_host_router_with_no_routes_is_refused():
  with pytest.raises(ValueError, match="requires at least one route"):
    flows.HostRouter("Acme_Host", routes={})


def test_host_router_defaults():
  host = flows.HostRouter("Acme_Host", routes={"widgets": _sub_agent("W", "w")})
  assert host.strategy == "transfer"
  assert host.robust_switching is True
  assert host.route_cues is None and host.extra_tools == []


def test_agent_hooks_default_to_nothing_attached():
  assert flows.AgentHooks().any() is False
  assert flows.AgentHooks(before_agent=lambda **_: None).any() is True


def test_an_app_declaring_neither_shape_is_refused():
  with pytest.raises(ValueError) as exc:
    flows.App(app_display_name="Acme")
  assert "provide root_flow (single-agent) or host+agents" in str(exc.value)


def test_a_multi_agent_app_needs_both_halves():
  """`agents=[...]` alone makes the app multi-agent, and a multi-agent app with no
  host has nothing to route it."""
  with pytest.raises(ValueError) as exc:
    flows.App(agents=[_sub_agent("Widget_Agent", "widgets")],
              app_display_name="Acme")
  assert "require both host= and agents=[...]" in str(exc.value)


def test_an_app_needs_a_display_name():
  with pytest.raises(ValueError, match="app_display_name is required"):
    flows.App(root_flow=_flow())


def test_app_settings_keys_must_be_app_json_names():
  with pytest.raises(ValueError) as exc:
    _app(app_settings={7: {"level": "INFO"}})
  assert "keys must be app.json top-level names, got 7" in str(exc.value)


def test_an_app_setting_the_emitter_owns_points_at_the_field_that_sets_it():
  with pytest.raises(ValueError) as exc:
    _app(app_settings={"languageSettings": {}})
  assert "is emitted by flows" in str(exc.value)


def test_language_switching_must_be_one_of_the_four_modes():
  with pytest.raises(ValueError) as exc:
    _app(languages=["en-US", "es-US"], language_switching="sometimes")
  assert "must be 'off', 'explicit', 'auto', or 'select'" in str(exc.value)


def test_a_machine_with_no_tz_database_is_not_failed_for_a_zone_it_cannot_check(
    monkeypatch):
  """An unknown zone is refused, but only where there IS a database to ask."""
  monkeypatch.setattr(dsl, "_tz_names", lambda: frozenset())
  assert dsl._known_time_zone("Mars/Olympus_Mons") is None  # noqa: SLF001
  _app(time_zone="Mars/Olympus_Mons")  # ... and the App is built anyway


def test_a_multi_agent_app_has_no_single_config_id_and_roots_at_its_host():
  app = _multi_app()
  assert app.is_multi_agent is True
  assert app.root_agent == "Acme_Host"
  with pytest.raises(AttributeError) as exc:
    _ = app.config_id
  assert "use each Agent.config_id" in str(exc.value)


def test_a_single_agent_app_roots_at_its_flow():
  app = _app()
  assert app.is_multi_agent is False
  assert app.config_id == "acme_cov"
  assert app.root_agent == "Widget_Agent"
  assert flows.App(root_flow=flows.Flow("acme_cov"),
                   app_display_name="Acme").root_agent == "acme_cov_agent"


# ===========================================================================
# build.py — tool collection + scoping
# ===========================================================================

def test_a_grouped_setter_shadows_a_stray_single_field_default_of_the_same_name():
  """A slot that forgot its `setter_field` would otherwise emit a single-field
  setter OVER the multi-field one, and the group's other fields would never
  record."""
  cfg = {
      "slots": [
          {"name": "acme_first", "source": "user",
           "setter": "set_acme_inputs", "setter_field": "first"},
          {"name": "acme_last", "source": "user",
           "setter": "set_acme_inputs", "setter_field": "last"},
          # the stray one — same setter, no setter_field
          {"name": "acme_middle", "source": "user", "setter": "set_acme_inputs"},
      ],
      "tasks": [],
  }
  bodies, _available = _build.collect([cfg], {})
  body = bodies["set_acme_inputs"]
  assert "first" in body and "last" in body
  # the multi-field body took the name; the single-field generator did not run
  assert body == _build._setters.gen_multi_setter(  # noqa: SLF001
      "set_acme_inputs",
      [{"name": "first", "validation_rules": []},
       {"name": "last", "validation_rules": []}])


def test_a_wrap_up_setter_gets_the_wrap_up_generator():
  """The wrap-up setter reads "another one" as a yes; the plain value-recording
  setter does not, and the difference is a caller hung up on."""
  cfg = {"slots": [{"name": "acme_more", "source": "user",
                    "setter": "set_wrap_up_more"}],
         "tasks": []}
  bodies, _available = _build.collect([cfg], {})
  assert bodies["set_wrap_up_more"] == _build._setters.gen_wrap_up_setter(  # noqa: SLF001
      "set_wrap_up_more", "acme_more")


def test_a_correction_tool_scopes_itself_and_set_slot_change():
  """Both are named only by the `correction_tool` policy — no slot, no task — so
  the ordinary scan cannot see them, and CES drops a dispatch to a tool the agent
  does not list."""
  cfg = {"correction_tool": "correct_acme_slot", "slots": [], "tasks": []}
  scoped = _build.scoped_agent_tools("acme_cov", [cfg], [])
  assert "correct_acme_slot" in scoped
  assert "set_slot_change" in scoped
  # not vacuous: without the policy neither name appears
  plain = _build.scoped_agent_tools("acme_cov", [{"slots": [], "tasks": []}], [])
  assert "correct_acme_slot" not in plain and "set_slot_change" not in plain


def test_the_router_child_gate_is_a_no_op_on_a_flow_that_already_has_one():
  """Three ways a flow already owns its gate; each must come back UNCHANGED (the
  same object), because re-gating a router would overwrite its bootstrap."""
  for cfg in ({"router": True}, {"gate_slot": "acme_stage"}, {"single_flow": True}):
    assert _build._apply_router_child_gate(cfg) is cfg  # noqa: SLF001
  # not vacuous: an ungated child DOES get one
  gated = _build._apply_router_child_gate({"slots": []})  # noqa: SLF001
  assert gated["gate_slot"] == "active_flow"
  assert gated["bootstrap"]["tool"] == "set_active_flow"


# ===========================================================================
# build.py — multi-agent wiring refusals
# ===========================================================================

def test_a_host_routing_to_an_agent_that_is_not_in_agents_is_named():
  a = _sub_agent("Widget_Agent", "widgets")
  ghost = _sub_agent("Ghost_Agent", "ghosts")
  host = flows.HostRouter("Acme_Host", routes={"widgets": a, "ghosts": ghost})
  app = flows.App(host=host, agents=[a], app_display_name="Acme Multi")
  with pytest.raises(ValueError) as exc:
    flows.validate_app(app)
  assert "host routes reference agents not in agents=[...]: ['Ghost_Agent']" in str(
      exc.value)


def test_two_agents_of_one_name_are_refused_by_name():
  a = _sub_agent("Widget_Agent", "widgets")
  b = flows.Agent("Widget_Agent", flow=_flow("gadgets", "Widget_Agent"))
  host = flows.HostRouter("Acme_Host", routes={"widgets": a, "gadgets": b})
  app = flows.App(host=host, agents=[a, b], app_display_name="Acme Multi")
  with pytest.raises(ValueError) as exc:
    flows.validate_app(app)
  assert "duplicate agent names: ['Widget_Agent']" in str(exc.value)


def test_an_alias_claimed_by_two_sub_agents_is_refused():
  a = _sub_agent("Widget_Agent", "widgets", aliases=["thingy"])
  b = _sub_agent("Gadget_Agent", "gadgets", aliases=["Thingy"])
  host = flows.HostRouter("Acme_Host", routes={"widgets": a, "gadgets": b})
  app = flows.App(host=host, agents=[a, b], app_display_name="Acme Multi")
  with pytest.raises(ValueError) as exc:
    flows.validate_app(app)
  assert "overlapping route phrasing/aliases" in str(exc.value)
  assert "'thingy'" in str(exc.value)


def test_a_blank_alias_claims_no_phrasing():
  """A whitespace-only alias is dropped by the collision check rather than becoming a
  phrasing every agent that has one would collide on. (It is still refused further
  downstream, as an empty entry in the DERIVED route_cues — a different rule.)"""
  a = _sub_agent("Widget_Agent", "widgets", aliases=["   "])
  b = _sub_agent("Gadget_Agent", "gadgets", aliases=[""])
  host = flows.HostRouter("Acme_Host", routes={"widgets": a, "gadgets": b})
  _build._check_route_phrasings(host)  # no collision on '' # noqa: SLF001
  # not vacuous: a real shared alias IS a collision
  host.routes["widgets"].aliases = ["thingy"]
  host.routes["gadgets"].aliases = ["thingy"]
  with pytest.raises(ValueError, match="overlapping route phrasing"):
    _build._check_route_phrasings(host)  # noqa: SLF001


def _cue_app(route_cues) -> flows.App:
  a = _sub_agent("Widget_Agent", "widgets")
  b = _sub_agent("Gadget_Agent", "gadgets")
  host = flows.HostRouter("Acme_Host", routes={"widgets": a, "gadgets": b},
                          route_cues=route_cues)
  return flows.App(host=host, agents=[a, b], app_display_name="Acme Multi")


def test_a_route_cue_for_an_unknown_flow_key_names_the_valid_keys():
  with pytest.raises(ValueError) as exc:
    flows.validate_app(_cue_app({"widgits": ["widget"]}))
  assert "route_cues maps to unknown flow key 'widgits'" in str(exc.value)
  assert "['gadgets', 'widgets']" in str(exc.value)


def test_an_empty_route_cue_list_is_refused():
  with pytest.raises(ValueError) as exc:
    flows.validate_app(_cue_app({"widgets": []}))
  assert "route_cues['widgets'] is an empty cue list" in str(exc.value)


def test_one_cue_mapped_to_two_flows_is_refused():
  """Overlapping cues make a mid-call switch non-deterministic."""
  with pytest.raises(ValueError) as exc:
    flows.validate_app(_cue_app({"widgets": ["thingy"], "gadgets": ["Thingy "]}))
  assert "maps to two different flows (widgets and gadgets)" in str(exc.value)


def test_a_blank_cue_is_skipped_rather_than_claimed():
  """A blank cue would otherwise be `claimed` by the first flow and then collide
  with every other flow that has one."""
  app = _cue_app({"widgets": ["widget", "  "], "gadgets": ["gadget", ""]})
  assert flows.validate_app(app)[0] == []


def test_a_sub_agents_extra_flow_is_assembled_alongside_its_primary():
  """A specialist's second DAG has to reach the config map, or its `_dag` tool is
  scoped onto an agent that cannot load it."""
  a = _sub_agent("Widget_Agent", "widgets")
  a.extra_flows = [_flow("widget_faq", "Widget_Agent")]
  b = _sub_agent("Gadget_Agent", "gadgets")
  host = flows.HostRouter("Acme_Host", routes={"widgets": a, "gadgets": b})
  app = flows.App(host=host, agents=[a, b], app_display_name="Acme Multi")
  all_map, _bodies, _available, _routes, _host_cid = _build._assemble_multi(app)  # noqa: SLF001
  assert set(all_map) == {"widgets", "widget_faq", "gadgets"}
  # the extra flow is NOT re-gated as a router child (it is not a transfer target)
  assert "gate_slot" not in all_map["widget_faq"]
  assert all_map["widgets"]["gate_slot"] == "active_flow"


def test_host_and_agent_extras_are_empty_for_a_single_agent_app():
  """Both are multi-agent concepts; a single-agent app scopes extras the other way
  (`App.extra_agent_tools` straight onto the one agent)."""
  app = _app(extra_agent_tools=["some_tool"])
  assert _build._host_extra_tools(app) == []  # noqa: SLF001
  assert _build._agent_extra_tools(app) == []  # noqa: SLF001


def test_host_extras_union_the_app_level_ones_order_preserving():
  a = _sub_agent("Widget_Agent", "widgets", extra_tools=["agent_only"])
  b = _sub_agent("Gadget_Agent", "gadgets")
  host = flows.HostRouter("Acme_Host", routes={"widgets": a, "gadgets": b},
                          extra_tools=["host_faq", "shared"])
  app = flows.App(host=host, agents=[a, b], app_display_name="Acme Multi",
                  extra_agent_tools=["shared", "app_level"])
  assert _build._host_extra_tools(app) == [  # noqa: SLF001
      "host_faq", "shared", "app_level"]
  assert _build._agent_extra_tools(app) == ["agent_only"]  # noqa: SLF001


def test_assemble_for_lint_takes_the_single_agent_path():
  """The linter's entry point; a single-agent app has no host config id."""
  all_map, bodies, available, host_cid = _build.assemble_for_lint(_app())
  assert host_cid is None
  assert set(all_map) == {"acme_cov"}
  assert "look_up_acme_cov" in bodies
  assert "acme_cov_dag" in available


# ===========================================================================
# build.py — the journey-gate oracle (`_check_journey_gates`)
# ===========================================================================

def _journey_cfg(tasks, values=("pay", "refund")) -> dict:
  return {
      "slots": [{"name": "acme_intent", "kind": "intent",
                 "option_cues": {v: [v] for v in values}}],
      "tasks": tasks,
  }


def test_a_journey_terminal_gated_on_no_intent_at_all_is_named():
  errors = _build._check_journey_gates(_journey_cfg([  # noqa: SLF001
      {"name": "acme_pay", "terminal": True,
       "condition": flows.eq("acme_intent", "pay")},
      {"name": "acme_refund", "terminal": True},  # no gate at all
  ]))
  assert any("terminal task 'acme_refund' is not gated on intent slot" in e
             for e in errors)
  assert any("intent value 'refund' has no operation terminal" in e for e in errors)


def test_a_non_terminal_task_may_carry_any_gate():
  """Only terminals are the oracle's business — an intermediate `has(...)` gate on a
  journey flow must not be reported."""
  errors = _build._check_journey_gates(_journey_cfg([  # noqa: SLF001
      {"name": "acme_verify", "condition": flows.has("acme_pin")},
      {"name": "acme_pay", "terminal": True,
       "condition": flows.eq("acme_intent", "pay")},
      {"name": "acme_refund", "terminal": True,
       "condition": flows.eq("acme_intent", "refund")},
  ]))
  assert errors == []


def test_a_declarative_all_gate_pins_the_intent_through_its_conjunction():
  """A dict gate is read STRUCTURALLY — without it the oracle would report every
  declaratively gated terminal as ungated."""
  errors = _build._check_journey_gates(_journey_cfg([  # noqa: SLF001
      {"name": "acme_pay", "terminal": True,
       "condition": {"all": [{"slot": "acme_verified", "eq": "yes"},
                             {"slot": "acme_intent", "eq": "pay"}]}},
      {"name": "acme_refund", "terminal": True,
       "condition": {"slot": "acme_intent", "eq": "refund"}},
  ]))
  assert errors == []


def test_a_declarative_gate_that_pins_no_intent_value_is_reported():
  """`any`/`not` express a set or a complement, and a non-dict arm pins nothing —
  none of them identify ONE operation."""
  errors = _build._check_journey_gates(_journey_cfg([  # noqa: SLF001
      {"name": "acme_pay", "terminal": True,
       "condition": {"all": ["not a dict", {"slot": "acme_verified", "eq": "yes"}]}},
      {"name": "acme_refund", "terminal": True,
       "condition": {"any": [{"slot": "acme_intent", "eq": "pay"},
                             {"slot": "acme_intent", "eq": "refund"}]}},
  ]))
  assert sum("is not gated on intent slot" in e for e in errors) == 2


def test_a_gate_on_a_value_outside_the_enum_is_reported():
  errors = _build._check_journey_gates(_journey_cfg([  # noqa: SLF001
      {"name": "acme_pay", "terminal": True,
       "condition": flows.eq("acme_intent", "pay")},
      {"name": "acme_cancel", "terminal": True,
       "condition": flows.eq("acme_intent", "cancel")},
  ]))
  assert any("gates on acme_intent=='cancel', which is not an option" in e
             for e in errors)


def test_a_single_operation_journey_gives_its_lone_terminal_to_the_one_value():
  """One value, two ungated terminals: the first is adopted by the single op, and
  the DUPLICATE is what gets reported — an ungated-terminal error would be noise
  when there is only one operation it could possibly serve."""
  errors = _build._check_journey_gates(_journey_cfg([  # noqa: SLF001
      {"name": "acme_pay", "terminal": True},
      {"name": "acme_pay_alt", "terminal": True},
  ], values=("pay",)))
  assert len(errors) == 1
  assert "intent value 'pay' has 2 operation terminals" in errors[0]
  assert "acme_pay" in errors[0] and "acme_pay_alt" in errors[0]


def test_a_flow_with_no_intent_slot_is_not_journey_shaped():
  assert _build._check_journey_gates({"slots": [], "tasks": [  # noqa: SLF001
      {"name": "a", "terminal": True}, {"name": "b", "terminal": True}]}) == []


def test_a_classification_flow_with_one_terminal_is_not_a_journey():
  """An intent slot plus ONE terminal is "classify, then do one thing" — the
  per-value-terminal invariant does not apply."""
  assert _build._check_journey_gates(_journey_cfg([  # noqa: SLF001
      {"name": "acme_do_it", "terminal": True}])) == []


def test_a_condition_that_is_neither_a_string_nor_a_dict_pins_nothing():
  assert _build._intent_eq_value(None, "acme_intent") is None  # noqa: SLF001
  assert _build._intent_eq_value(["acme_intent"], "acme_intent") is None  # noqa: SLF001
  assert _build._intent_eq_value("", "acme_intent") is None  # noqa: SLF001


# ===========================================================================
# build.py — validation refusals over an assembled app
# ===========================================================================

def test_a_tool_that_reads_a_module_level_name_now_carries_it(registry):
  """`render_tool` carries the constant beside the function, so validation is clean.

  This asserted a validation ERROR until helper inlining landed. The error existed
  because the name was left behind and the tool died on its first call; it is now
  emitted above the function instead. `test_a_tool_reading_an_unbindable_name_still_
  fails_validation` below keeps the error path covered for the names inlining cannot
  carry, so this is a narrowing of the guard rather than its removal.
  """
  @flows.tool(flow="acme_cov")
  def acme_left_behind(acme_ref: str) -> dict:
    """Reads a name that the emitted file now carries."""
    return {"success": True, "limit": _ACME_LIMIT}

  assert "_ACME_LIMIT" in flows.authoring.tools.render_tool(
      flows.authoring.tools._REGISTRY["acme_left_behind"])  # noqa: SLF001
  errors, _warnings = _build._run_validation(  # noqa: SLF001
      {}, {"acme_left_behind": "..."}, [])
  assert errors == []


def test_a_tool_reading_an_unbindable_name_still_fails_validation(registry):
  """The build error survives for a name that has no source to copy.

  A module-level IMPORT binds the name but is not an assignment, so there is nothing to
  emit above the function and the sandbox still cannot resolve it. Catching that at
  build time is the point of the check (ces-probes 86: one deploy and one live drive to
  find a `SENTINEL` defined ten lines above the function).
  """
  @flows.tool(flow="acme_cov")
  def acme_unbindable(acme_ref: str) -> dict:
    """Reads an imported name, which cannot be carried."""
    return {"success": True, "sep": _ACME_IMPORTED_SEP}

  errors, _warnings = _build._run_validation(  # noqa: SLF001
      {}, {"acme_unbindable": "..."}, [])
  assert len(errors) == 1
  assert "Tool 'acme_unbindable' reads ['_ACME_IMPORTED_SEP']" in errors[0]
  assert "name '_ACME_IMPORTED_SEP' is not defined" in errors[0]
  # not vacuous: the same tool is silent when it is not one of THIS app's bodies
  assert _build._run_validation({}, {}, [])[0] == []  # noqa: SLF001


def test_agent_tools_must_be_built_with_the_factory():
  app = _app(agent_tools=["ask_acme"])
  with pytest.raises(ValueError) as exc:
    _build._agent_tools(app)  # noqa: SLF001
  assert "agent_tools must be built with flows.agent_tool(...), got str" in str(
      exc.value)


def test_two_different_agent_tools_of_one_name_are_refused():
  """One tool resource is emitted per name, so the second declaration would be lost."""
  one = flows.agent_tool("ask_acme", agent="Helper_A", description="Answers A.")
  two = flows.agent_tool("ask_acme", agent="Helper_B", description="Answers B.")
  with pytest.raises(ValueError) as exc:
    _build._agent_tools(_app(agent_tools=[one, two]))  # noqa: SLF001
  assert "two different agent tools are both named 'ask_acme'" in str(exc.value)


def test_the_same_agent_tool_declared_twice_is_deduped_not_refused():
  one = flows.agent_tool("ask_acme", agent="Helper_A", description="Answers A.")
  two = flows.agent_tool("ask_acme", agent="Helper_A", description="Answers A.")
  assert _build._agent_tools(_app(agent_tools=[one, two])) == [one]  # noqa: SLF001


def test_a_task_firing_an_agent_tool_with_two_inputs_is_refused():
  """An agent takes exactly one argument, and it is called `request`."""
  tool = flows.agent_tool("ask_acme", agent="Widget_Agent",
                          description="Answers acme policy questions.")
  all_map = {"acme_cov": {"tasks": [{
      "name": "acme_ask", "tool": "ask_acme",
      "inputs": {"acme_q": "request", "acme_ctx": "context"},
      "success_check": "response",
  }]}}
  errors, _warnings = _build._check_agent_tasks(  # noqa: SLF001
      all_map, _app(agent_tools=[tool]))
  assert any("with ['context']" in e and "takes exactly one argument" in e
             for e in errors)
  assert any("with 2 inputs — an agent takes one" in e for e in errors)


def test_a_task_firing_something_that_is_not_an_agent_tool_is_left_alone():
  """The declared map is non-empty, so every task is inspected; one firing an
  ordinary tool must produce nothing."""
  tool = flows.agent_tool("ask_acme", agent="Widget_Agent", description="Answers.")
  all_map = {"acme_cov": {"tasks": [
      {"name": "acme_lookup", "tool": "look_up_acme_cov", "inputs": ["acme_ref"]},
  ]}}
  errors, warnings = _build._check_agent_tasks(  # noqa: SLF001
      all_map, _app(agent_tools=[tool]))
  assert errors == []
  # the agent it names IS this app's root agent, so no unresolvable-name warning
  assert warnings == []


def test_a_timeout_on_an_agent_tool_is_reported_as_decorative(registry):
  """The platform makes this call; a timeout on a platform-executed tool is
  accepted, persisted and IGNORED, which reads back as though it bound something."""
  @flows.tool(flow="acme_cov", name="ask_acme", timeout=5)
  def _ask_acme(request: str) -> dict:
    """A declared timeout on the name an agent tool also claims."""
    return {"response": "ok"}

  tool = flows.agent_tool("ask_acme", agent="Widget_Agent", description="Answers.")
  _errors, warnings = _build._check_agent_tasks({}, _app(agent_tools=[tool]))  # noqa: SLF001
  assert any("declares a timeout, which does not bound it" in w for w in warnings)
  # not vacuous: drop the timeout and the warning goes with it
  registry.pop("ask_acme")
  assert _build._check_agent_tasks({}, _app(agent_tools=[tool]))[1] == []  # noqa: SLF001


def test_app_level_tool_timeouts_never_reach_the_agent_tool_warning(registry):
  """REPORTED, not fixed (build.py:1883): `_check_agent_tasks` calls
  `_tool_timeout_map()` with no `app=`, so it reads only the DECORATOR registry. An
  `App(tool_timeouts={...})` entry — the documented escape hatch for a tool with no
  decorator, which is exactly what an agent tool is — is therefore never reported."""
  tool = flows.agent_tool("ask_acme", agent="Widget_Agent", description="Answers.")
  app = _app(agent_tools=[tool], tool_timeouts={"ask_acme": 5})
  assert app.tool_timeouts == {"ask_acme": 5}
  assert _build._tool_timeout_map(app=app) == {"ask_acme": 5}  # noqa: SLF001
  assert _build._tool_timeout_map() == {}  # ... but this is what the check asks # noqa: SLF001
  assert _build._check_agent_tasks({}, app)[1] == []  # noqa: SLF001


_ACME_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Acme", "version": "1.0.0"},
    "paths": {
        "/widgets/{id}": {
            "get": {
                "operationId": "getWidget",
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


def _toolset():
  return flows.openapi_toolset("acme_api", spec=_ACME_SPEC,
                               base_url="https://api.example.test")


def test_toolset_names_are_the_declared_display_names():
  assert _build._toolset_names(_app(toolsets=[_toolset()])) == {"acme_api"}  # noqa: SLF001
  assert _build._toolset_names(_app()) == set()  # noqa: SLF001


def test_the_toolset_check_ignores_a_task_with_no_tool_and_an_unrelated_one():
  """A component task fires nothing, and a task firing a tool whose name does not
  carry a toolset's prefix is none of this check's business."""
  ts = _toolset()
  all_map = {"acme_cov": {"tasks": [
      {"name": "acme_child", "component": "acme_child_flow"},   # no tool
      {"name": "acme_other", "tool": "look_up_something_else"},  # no prefix match
  ]}}
  errors, warnings = _build._check_toolset_tasks(all_map, _app(toolsets=[ts]))  # noqa: SLF001
  assert (errors, warnings) == ([], [])
  # not vacuous: firing the toolset itself IS refused
  fires = {"acme_cov": {"tasks": [{"name": "acme_call", "tool": "acme_api"}]}}
  assert any("which is an OpenAPI TOOLSET" in e
             for e in _build._check_toolset_tasks(fires, _app(toolsets=[ts]))[0])  # noqa: SLF001


# ===========================================================================
# build.py — emit-path helpers, driven directly against tmp_path
# ===========================================================================

def test_scaffolded_variable_names_survive_an_unreadable_app_json():
  """`_asked_variables` compares what was ASKED for against what landed. An app.json
  that will not parse must degrade to "nothing scaffolded" rather than explode
  before the integrity check can report the real problem."""
  class _F:
    path = "app.json"
    content = "{not json at all"

  class _Res:
    files = [_F()]

  asked = _build._asked_variables(  # noqa: SLF001
      _Res(), [{"name": "ACME_ACCOUNT", "schema": {"type": "STRING"}}])
  assert asked == ["ACME_ACCOUNT"]


def test_injecting_variables_before_the_scaffold_ran_says_so(tmp_path):
  with pytest.raises(RuntimeError) as exc:
    _build._inject_variables(str(tmp_path), [  # noqa: SLF001
        {"name": "ACME_ACCOUNT", "schema": {"type": "STRING"}}])
  assert "cannot inject variables" in str(exc.value)
  assert "scaffold.build must run and succeed" in str(exc.value)


def test_injecting_app_settings_before_the_scaffold_ran_says_so(tmp_path):
  app = _app(time_zone="America/New_York")
  with pytest.raises(RuntimeError) as exc:
    _build._emit_app_settings(str(tmp_path), app)  # noqa: SLF001
  assert "cannot inject app settings" in str(exc.value)


def test_injecting_language_settings_before_the_scaffold_ran_says_so(tmp_path):
  app = _app(languages=["en-US", "es-US"], language_switching="explicit")
  with pytest.raises(RuntimeError) as exc:
    _build._inject_language_settings(str(tmp_path), app)  # noqa: SLF001
  assert "cannot inject languageSettings" in str(exc.value)


def test_the_language_block_skips_an_agent_with_no_instruction_file(tmp_path):
  """Post-emit passes walk agent names, not the disk; a name with no emitted
  instruction is skipped rather than crashing the build after the tree is written."""
  (tmp_path / "app.json").write_text("{}")
  app = _app(languages=["en-US", "es-US"], language_switching="explicit")
  _build._emit_language(str(tmp_path), app, ["Nobody_Home"])  # noqa: SLF001
  settings = json.loads((tmp_path / "app.json").read_text())["languageSettings"]
  assert settings["defaultLanguageCode"] == "en-US"
  assert settings["supportedLanguageCodes"] == ["es-US"]
  assert settings["enableMultilingualSupport"] is True


def test_the_language_nudge_writes_its_hooks_but_registers_nothing_without_an_agent(
    tmp_path):
  app = _app(languages=["en-US", "es-US"], language_switching="select")
  _build._emit_language_nudge(str(tmp_path), "Widget_Agent", app)  # noqa: SLF001
  before = tmp_path / "agents/Widget_Agent/before_model_callbacks"
  after = tmp_path / "agents/Widget_Agent/after_model_callbacks"
  assert (before / "before_model_callbacks_03/python_code.py").is_file()
  assert (after / "after_model_callbacks_03/python_code.py").is_file()
  assert not (tmp_path / "agents/Widget_Agent/Widget_Agent.json").exists()


def test_the_variable_map_ingress_skips_an_agent_with_no_json_and_never_doubles(
    tmp_path):
  """Registered FIRST on every slot-filling agent, and exactly once — a second pass
  over an already-registered agent must not stack a duplicate callback."""
  vmap = flows.variable_map("acme_known_caller", {"acme_cov_ref": "ACME_ACCOUNT"})
  app = _app(variable_maps=[vmap])
  # (a) no agent JSON on disk -> the source is still written, registration skipped
  _build._emit_variable_map_ingress(str(tmp_path), app, ["Widget_Agent"])  # noqa: SLF001
  rel = "agents/Widget_Agent/%s/python_code.py" % _build._variable_maps.INGRESS_SUBDIR  # noqa: SLF001
  assert (tmp_path / rel).is_file()

  # (b) with an agent JSON -> registered at index 0, ahead of the author bucket
  aj = tmp_path / "agents/Widget_Agent/Widget_Agent.json"
  aj.write_text(json.dumps({"beforeAgentCallbacks": [
      {"pythonCode": "agents/Widget_Agent/before_agent_callbacks/x/python_code.py"}]}))
  _build._emit_variable_map_ingress(str(tmp_path), app, ["Widget_Agent"])  # noqa: SLF001
  cbs = json.loads(aj.read_text())["beforeAgentCallbacks"]
  assert cbs[0]["pythonCode"] == rel

  # (c) idempotent
  _build._emit_variable_map_ingress(str(tmp_path), app, ["Widget_Agent"])  # noqa: SLF001
  cbs = json.loads(aj.read_text())["beforeAgentCallbacks"]
  assert [c["pythonCode"] for c in cbs].count(rel) == 1


def test_scoping_agent_tools_returns_the_list_even_with_no_agent_json(tmp_path):
  """The list is the return value the caller records; the JSON patch is a side
  effect, and a missing file must not lose the list."""
  app = _app()
  all_map, _bodies, _available = _build._assemble(app)  # noqa: SLF001
  tools = _build._scope_agent_tools(str(tmp_path), app, all_map)  # noqa: SLF001
  assert "acme_cov_dag" in tools and "slot_filling_engine" in tools
  assert not (tmp_path / "agents").exists()


def test_the_second_language_falls_back_when_every_code_is_the_default():
  """`select` mode needs a `press 9` target; a one-language list has none, and
  returning the default beats an IndexError inside the emit."""
  assert _build._second_language_code(  # noqa: SLF001
      _app(languages=["en-US"])) == "en-US"
  assert _build._second_language_code(  # noqa: SLF001
      _app(languages=["en-US", "es-US"], default_language="en-US")) == "es-US"


def test_a_router_with_no_resolvable_flow_types_emits_no_runtime_vars():
  """`flow_types` is filtered against the configs that actually assembled — a
  router naming only flows the app does not carry has nothing to switch between,
  and an empty `flow_config_map` would pin the router config for the whole call."""
  router = flows.router_flow("acme_router", ["nowhere", "also_nowhere"],
                             root_agent="Widget_Agent")
  app = flows.App(root_flow=router, app_display_name="Acme Router")
  all_map = {"acme_router": router.to_config()}
  assert _build._router_runtime_vars(app, all_map, ["look_up_x"]) == []  # noqa: SLF001


def test_a_non_router_root_emits_no_runtime_vars():
  app = _app()
  all_map, _bodies, _available = _build._assemble(app)  # noqa: SLF001
  assert _build._router_runtime_vars(app, all_map, ["look_up_x"]) == []  # noqa: SLF001


# ===========================================================================
# build.py — emit refusals + the everything-at-once app
# ===========================================================================

def test_emit_refuses_a_flow_that_fails_validation_and_writes_nothing(tmp_path):
  """A mis-gated journey assembles fine and is caught by the authoring oracle, so
  emit must stop BEFORE the scaffold writes a tree."""
  f = flows.Flow("acme_bad", root_agent="Widget_Agent")
  f.add(flows.intent_slot("acme_intent", {"pay": ["pay"], "refund": ["refund"]}))
  f.add(flows.result_slot("acme_receipt", "acme_pay"))
  f.add(flows.result_slot("acme_credit", "acme_refund"))
  f.task("acme_pay", "take_payment", [], "acme_receipt", terminal=True,
         condition=flows.eq("acme_intent", "pay"))
  f.task("acme_refund", "issue_refund", [], "acme_credit", terminal=True)  # ungated
  app = flows.App(root_flow=f, app_display_name="Acme Bad")
  out = str(tmp_path / "app")
  with pytest.raises(ValueError) as exc:
    flows.build_app(app, out)
  assert "flow validation failed" in str(exc.value)
  assert "journey-gate" in str(exc.value)
  assert "acme_refund" in str(exc.value)
  assert not os.path.exists(out)


def test_multi_agent_emit_refuses_a_flow_that_fails_validation(tmp_path):
  a = _sub_agent("Widget_Agent", "widgets")
  a.flow.add(flows.intent_slot("acme_intent", {"pay": ["pay"], "refund": ["ref"]}))
  a.flow.add(flows.result_slot("acme_receipt", "acme_pay"),
             flows.result_slot("acme_credit", "acme_refund"))
  a.flow.task("acme_pay", "take_payment", [], "acme_receipt", terminal=True,
              condition=flows.eq("acme_intent", "pay"))
  a.flow.task("acme_refund", "issue_refund", [], "acme_credit", terminal=True)
  b = _sub_agent("Gadget_Agent", "gadgets")
  host = flows.HostRouter("Acme_Host", routes={"widgets": a, "gadgets": b})
  app = flows.App(host=host, agents=[a, b], app_display_name="Acme Multi")
  with pytest.raises(ValueError) as exc:
    flows.build_app(app, str(tmp_path / "app"))
  assert "flow validation failed" in str(exc.value)
  assert "journey-gate" in str(exc.value)


def test_language_select_is_refused_on_a_multi_agent_app():
  """Turn-1 menu + hard lock is single-agent only; accepting it would emit a lock
  the specialists never carry."""
  with pytest.raises(ValueError) as exc:
    flows.validate_app(_multi_app(languages=["en-US", "es-US"],
                                  language_switching="select"))
  assert "language_switching='select'" in str(exc.value)
  assert "currently single-agent only" in str(exc.value)


def test_a_multi_agent_scaffold_failure_aborts_and_removes_the_tree(
    tmp_path, monkeypatch):
  """A transfer host carries its OWN callbacks by design, so the drift gate skips
  that dir — and the abort's reason must skip it too, or the author is sent looking
  at files that are supposed to differ."""
  seen: dict = {}

  def _fake_build_multi_agent(req):
    seen["target"] = req.target_path
    os.makedirs(req.target_path, exist_ok=True)

    class _FailedResult:
      ok = False
      error = "Scaffold failed: the disk caught fire"
      validation = None
      files = []
      written_to = req.target_path

    return _FailedResult()

  monkeypatch.setattr(_build._scaffold, "build_multi_agent",  # noqa: SLF001
                      _fake_build_multi_agent)
  out = str(tmp_path / "app")
  with pytest.raises(_build.ScaffoldFailed):
    flows.build_app(_multi_app(), out)
  assert seen["target"] == out
  assert not os.path.exists(out)  # keep_failed defaults False


def test_a_single_agent_app_with_everything_at_once_emits(tmp_path):
  """extra_flows + variables + a global instruction + an agent instruction +
  steering + hooks, all on one app. Each is a separate post-emit pass over the same
  tree, and they have to compose."""
  root = _flow()
  faq = _flow("acme_faq", "Widget_Agent")

  def _steering(callback_context):
    """Author steering: nudge the model to stay on task."""
    return None

  def _before_model(callback_context, llm_request):
    """Author hook: runs on every model turn."""
    return None

  app = _app(
      root_flow=root,
      extra_flows=[faq],
      variables=[{"name": "ACME_ACCOUNT",
                  "description": "The account the call arrived with",
                  "schema": {"type": "STRING"}}],
      global_instruction="You are Acme's virtual assistant.",
      agent_instruction="Collect the reference, then look it up.",
      steering=_steering,
      hooks=flows.AgentHooks(before_model=_before_model),
  )
  out = str(tmp_path / "app")
  res = flows.build_app(app, out)
  assert res.ok, res.validation.errors if res.validation else res.error

  gi = os.path.join(out, "global_instruction.txt")
  assert open(gi).read() == "You are Acme's virtual assistant.\n"
  instr = open(os.path.join(out, "agents/Widget_Agent/instruction.txt")).read()
  assert "Collect the reference, then look it up." in instr
  names = _build._integrity.variable_names(  # noqa: SLF001
      json.load(open(os.path.join(out, "app.json")))["variableDeclarations"])
  assert "ACME_ACCOUNT" in names
  agent_json = json.load(
      open(os.path.join(out, "agents/Widget_Agent/Widget_Agent.json")))
  assert "acme_faq_dag" in agent_json["tools"]
  assert agent_json["beforeAgentCallbacks"]


def test_overwrite_false_refuses_to_write_into_an_existing_directory(tmp_path):
  out = tmp_path / "app"
  out.mkdir()
  (out / "keep_me.txt").write_text("prior work")
  with pytest.raises(_build.ScaffoldFailed) as exc:
    flows.build_app(_app(), str(out), overwrite=False)
  assert "Refusing to overwrite non-empty directory" in str(exc.value)
  assert str(out) in str(exc.value)
  assert (out / "keep_me.txt").read_text() == "prior work"  # untouched


def test_app_level_search_and_remote_agents_scope_onto_the_host(tmp_path):
  """The host is the agent that talks to the caller between transfers, so an
  app-level search tool (or remote agent) is ITS to call — and search visibility
  cannot be narrowed per turn, so scoping is the only gate there is."""
  searcher = flows.search_tool("acme_store_hours",
                               "Store hours, holidays and closures.")
  remote = flows.remote_agent("acme_specialist",
                              description="An external specialist.",
                              url="https://agents.example.test/a2a")
  app = _multi_app(search_tools=[searcher], remote_agents=[remote])
  out = str(tmp_path / "app")
  res = flows.build_app(app, out)
  assert res.ok, res.validation.errors if res.validation else res.error
  host = json.load(open(os.path.join(out, "agents/Acme_Host/Acme_Host.json")))
  assert "acme_store_hours" in host["tools"]
  assert "acme_specialist" in host["tools"]
  # ... and NOT onto a specialist that did not declare them
  widget = json.load(
      open(os.path.join(out, "agents/Widget_Agent/Widget_Agent.json")))
  assert "acme_store_hours" not in widget["tools"]


def test_a_multi_agent_app_with_language_switching_and_a_global_instruction(tmp_path):
  """The host and every specialist get `update_language` scoped in (a switch has to
  survive a transfer), and the app-level global instruction is written once."""
  app = _multi_app(languages=["en-US", "es-US"], language_switching="explicit",
                   global_instruction="You are Acme's virtual assistant.")
  out = str(tmp_path / "app")
  res = flows.build_app(app, out)
  assert res.ok, res.validation.errors if res.validation else res.error
  assert open(os.path.join(out, "global_instruction.txt")).read() == (
      "You are Acme's virtual assistant.\n")
  for name in ("Acme_Host", "Widget_Agent", "Gadget_Agent"):
    aj = json.load(open(os.path.join(out, "agents", name, f"{name}.json")))
    assert "update_language" in aj["tools"], name
