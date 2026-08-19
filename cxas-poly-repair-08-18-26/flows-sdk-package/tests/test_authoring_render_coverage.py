"""Authoring-layer coverage sweep: render / openapi / integrity / tools / handoff.

These five modules sit on the AUTHORING side of the SDK — the code that turns what an
author wrote into the bytes that get deployed. A defect here is invisible offline and
expensive live, so the contracts exercised below are the exact ones:

* `render.py` — the migration renderer. Its promise is a round trip: `exec` the emitted
  module, and `flow.to_config()` reproduces the source config byte-for-byte with key
  order preserved. Every test that renders therefore also re-executes, and the emitted
  text is asserted verbatim wherever the contract is exact (the builder-match-or-raw
  rule means an author reads `user_slot(...)` or `raw({...})`, never something in
  between).
* `openapi.py` — the spec/toolset builders, and the refusals that keep a malformed spec
  from reaching CES (which drops a bad toolset at import, silently).
* `integrity.py` — the asked-vs-landed diff. An integrity check that never fires is
  worthless, so every violation is driven, not just the passing case.
* `handoff.py` — the payload/end_session pair, and each way of asking for one wrongly.
* `tools.py` — the remaining edge branches of `@tool` and the emitted-source analysis.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_authoring_render_coverage.py
"""

from __future__ import annotations

import ast
import json
import os
import re
import warnings
from typing import Optional

import pytest
from pydantic import BaseModel

import flows
from flows.authoring import handoff as H
from flows.authoring import integrity as I
from flows.authoring import openapi as O
from flows.authoring import render as R
from flows.authoring import tools as T


# ===========================================================================
# render.py
# ===========================================================================


def _render(config: dict, *, config_id: str = "cov", root_agent: str = "") -> str:
  """Render, prove the result is importable Python, and prove the round trip."""
  src = R.render_config_source(config, config_id=config_id, root_agent=root_agent)
  ast.parse(src)  # emitted Python must parse — it is exec'd by the migration backend
  ns: dict = {}
  exec(src, ns)  # noqa: S102 — running the emitted module IS the contract
  rebuilt = ns["flow"].to_config()
  assert list(rebuilt.items()) == list(config.items())
  return src


def _slots(*slot_dicts) -> dict:
  return {"slots": list(slot_dicts), "tasks": []}


def _expr_for(src: str, head: str) -> str:
  """The emitted call beginning with `head`, up to its closing line."""
  assert head in src, src
  return src[src.index(head):]


def _fell_back_to_raw(src: str) -> bool:
  """Whether anything downgraded to `raw({...})`.

  Grepped off the IMPORT block: the module docstring mentions `raw({...})` by name, so
  searching the whole file would match every render.
  """
  return "    raw,\n" in src


# --- literals ---------------------------------------------------------------
# Driven through a `raw({...})` slot, which is the one place the renderer emits an
# arbitrary author value as a literal with no builder in the way.


def _literal_for(value) -> str:
  src = _render(_slots({"name": "lit", "source": "system", "v": value}), config_id="c")
  m = re.search(r'raw\(\{"name": "lit", "source": "system", "v": (.*)\}\)', src)
  assert m is not None, src
  return m.group(1)


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (None, "None"),
        (True, "True"),
        (False, "False"),
        (0, "0"),
        (-3, "-3"),
        (1.5, "1.5"),
        ([], "[]"),
        ({}, "{}"),
        ([1, [2, {"k": None}]], '[1, [2, {"k": None}]]'),
        ({"nested": {"deep": [True]}}, '{"nested": {"deep": [True]}}'),
    ],
)
def test_every_scalar_and_container_literal_is_emitted_exactly(value, want):
  assert _literal_for(value) == want


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("", '""'),
        ("plain", '"plain"'),
        ('has "quotes"', "'has \"quotes\"'"),          # switch quotes, avoid escapes
        ("it's", '"it\'s"'),                           # apostrophe: stay double-quoted
        ('both \' and "', '"both \' and \\""'),        # both present: escape the double
        ("line\nbreak", '"line\\nbreak"'),
        ("tab\there", '"tab\\there"'),
        ("back\\slash", '"back\\\\slash"'),
        ("carriage\rreturn", '"carriage\\rreturn"'),
        ("unicode: café — £", '"unicode: café — £"'),
        ('multi\nline "quoted"', "'multi\\nline \"quoted\"'"),
    ],
)
def test_string_literals_are_escaped_exactly(text, want):
  assert _literal_for(text) == want


def test_a_non_string_dict_key_is_stringified_and_quoted():
  # Every emitted mapping key is a quoted STRING, whatever the config carried — the
  # emitted module has to be readable Python, and a CES config's keys are JSON keys.
  src = R.render_config_source(
      _slots({"name": "lit", "source": "system", "v": {1: "one"}}), config_id="c")
  ast.parse(src)
  assert '"v": {"1": "one"}' in src


def test_a_value_no_literal_can_express_is_a_loud_typeerror():
  # A set has no deterministic literal form the renderer will emit, and emitting a
  # WRONG one would ship broken source. Refusing loudly is the contract.
  with pytest.raises(TypeError, match="cannot render literal"):
    R.render_config_source(_slots({"name": "x", "source": "system", "v": {1, 2}}),
                           config_id="c")


def test_a_flow_level_policy_key_becomes_a_set_call():
  cfg = {"single_flow": True, "bootstrap": None, "slots": [], "tasks": []}
  src = _render(cfg, config_id="c")
  assert 'flow.set("single_flow", True)' in src
  assert 'flow.set("bootstrap", None)' in src


def test_a_long_policy_value_wraps_and_still_round_trips():
  value = {f"cue_{i}": [f"phrase number {i}"] for i in range(8)}
  cfg = {"route_cues": value, "slots": [], "tasks": []}
  src = _render(cfg, config_id="c")
  assert 'flow.set(\n    "route_cues",\n    {\n' in src
  assert all(len(line) <= 88 for line in src.splitlines())


def test_unicode_survives_the_module_docstring_and_the_flow_id():
  src = _render({"slots": [], "tasks": []}, config_id="café_dag")
  assert 'Flow("café_dag")' in src


def test_a_very_long_config_id_wraps_the_flow_constructor():
  long_id = "op_" + "x" * 90 + "_dag"
  src = _render({"slots": [], "tasks": []}, config_id=long_id)
  assert f'flow = Flow(\n    "{long_id}"\n)' in src


def test_an_empty_config_still_renders_a_runnable_module():
  src = R.render_config_source({}, config_id="empty")
  ast.parse(src)
  ns: dict = {}
  exec(src, ns)  # noqa: S102
  assert ns["flow"].to_config() == {"slots": [], "tasks": []}
  assert 'flow = Flow("empty")' in src
  assert "flow.add(" not in src
  assert src.endswith('    print("valid" if valid else errors)\n')


def test_null_slot_and_task_lists_are_treated_as_absent():
  src = R.render_config_source({"slots": None, "tasks": None}, config_id="n")
  ast.parse(src)
  assert "flow.add(" not in src


# --- conditions -------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "want"),
    [
        (flows.has("acct"), 'has("acct")'),
        (flows.unset("acct"), 'unset("acct")'),
        (flows.eq("acct", "gold"), 'eq("acct", "gold")'),
        (flows.eq("acct", 7), 'eq("acct", 7)'),
        (flows.ne("acct", "gold"), 'ne("acct", "gold")'),
    ],
)
def test_each_condition_helper_is_re_emitted_as_itself(spec, want):
  src = _render(_slots(flows.user_slot("x", "Ask?", condition=spec)), config_id="c")
  assert f"condition={want}" in src
  # ...and only the helper it used is imported.
  assert f"    {want.split('(')[0]},\n" in src


def test_a_hand_written_lambda_condition_is_passed_through_as_a_string():
  hand = "lambda f: bool(f.get('a')) and bool(f.get('b'))"
  src = _render(_slots(flows.user_slot("x", "Ask?", condition=hand)), config_id="c")
  assert f"condition={hand!s}" not in src  # not bare — it is a string literal
  assert '"lambda f: bool(f.get(\'a\')) and bool(f.get(\'b\'))"' in src


def test_a_helper_shaped_lambda_whose_argument_is_not_a_literal_stays_a_string():
  # Matches _HAS_RE but `slot_name` is a NAME, not a literal — ast.literal_eval refuses
  # it, so there is no `has(...)` call to re-emit and the source is kept verbatim.
  hand = "lambda f: bool(f.get(slot_name))"
  src = _render(_slots(flows.user_slot("x", "Ask?", condition=hand)), config_id="c")
  assert '"lambda f: bool(f.get(slot_name))"' in src
  assert "    has,\n" not in src


def test_a_lambda_that_only_looks_like_eq_stays_a_string():
  hand = "lambda f: f.get('a') == f.get('b')"
  src = _render(_slots(flows.user_slot("x", "Ask?", condition=hand)), config_id="c")
  assert "    eq,\n" not in src


# --- user_slot --------------------------------------------------------------


def test_a_plain_user_slot_is_a_one_line_builder_call():
  src = _render(_slots(flows.user_slot("zip", "ZIP?")), config_id="c")
  assert '    user_slot("zip", "ZIP?"),\n' in src


def test_every_optional_user_slot_field_is_emitted_as_its_own_kwarg():
  slot = flows.user_slot(
      "acct", "Account?",
      setter="read_account",
      reprompts=["Again?"],
      max_retries=5,
      on_exhaust="No luck.",
      on_exhaust_then={"tool": "escalate_now"},
      dtmf={"1": "one"},
      requires=["intent"],
      condition=flows.has("intent"),
      readback=True,
      skip_readback_if_matches=["known"],
      hint="account number",
      verbatim=True,
      filler_say="One moment.",
      automatic_fillers=False,
  )
  src = _render(_slots(slot), config_id="c")
  for kwarg in ('setter="read_account"',
                'reprompts=["Again?", "One more time. Account?"]', "max_retries=5",
                'on_exhaust="No luck."', 'on_exhaust_then={"tool": "escalate_now"}',
                'dtmf={"1": "one"}', 'requires=["intent"]', 'condition=has("intent")',
                "readback=True", 'skip_readback_if_matches=["known"]',
                'hint="account number"', "verbatim=True",
                'filler_say="One moment."', "automatic_fillers=False"):
    assert kwarg in src, kwarg
  assert not _fell_back_to_raw(src)


def test_a_default_hint_is_not_re_emitted():
  src = _render(_slots(flows.user_slot("tracking_number", "Number?")), config_id="c")
  assert "hint=" not in src


def test_a_user_slot_the_builder_cannot_reproduce_falls_back_to_raw():
  # `source: user` with no `ask` — _candidate_user_slot cannot read it.
  src = _render(_slots({"name": "x", "source": "user", "kind": "boolean"}),
                config_id="c")
  assert 'raw({"name": "x", "source": "user", "kind": "boolean"})' in src


def test_a_long_raw_slot_wraps_one_key_per_line():
  blob = {"name": "wide", "source": "system",
          "notes": "long enough to force the renderer past its budget",
          "extra": ["one", "two", "three"]}
  src = _render(_slots(blob), config_id="c")
  assert "    raw(\n        {\n" in src
  assert '            "extra": ["one", "two", "three"],\n' in src
  assert all(len(line) <= 88 for line in src.splitlines())


# --- intent_slot / passive_slot --------------------------------------------


def test_an_intent_slot_emits_every_optional_kwarg_in_signature_order():
  slot = flows.intent_slot(
      "journey_intent", {"pay": ["pay"], "refund": ["refund"]},
      ask="What can I do?", passive=True, setter="classify_intent",
      dtmf={"1": "pay"}, requires=["greeted"], condition=flows.has("greeted"))
  src = _render(_slots(slot), config_id="c")
  call = _expr_for(src, "intent_slot(")
  order = [call.index(k) for k in ("passive=", "setter=", "dtmf=",
                                   "requires=", "condition=")]
  assert order == sorted(order)
  assert not _fell_back_to_raw(src)


def test_an_intent_slot_without_option_cues_falls_back_to_raw():
  src = _render(_slots({"name": "i", "source": "user", "kind": "intent"}),
                config_id="c")
  assert "raw({" in src
  assert "intent_slot(" not in src


def test_a_passive_slot_is_emitted_through_its_own_builder():
  slot = flows.passive_slot("acct", setter="read_acct",
                            option_cues={"gold": ["gold"]}, kind="string",
                            requires=["intent"], condition=flows.has("intent"))
  src = _render(_slots(slot), config_id="c")
  assert 'passive_slot(\n        "acct",\n' in src
  for kwarg in ('setter="read_acct"', 'option_cues={"gold": ["gold"]}',
                'kind="string"', 'requires=["intent"]', 'condition=has("intent")'):
    assert kwarg in src, kwarg


def test_a_passive_slot_whose_name_is_not_a_string_falls_back_to_raw():
  src = _render(_slots({"name": 7, "source": "user", "passive": True}), config_id="c")
  assert "raw({" in src


def test_a_passive_slot_with_the_default_setter_omits_the_kwarg():
  src = _render(_slots(flows.passive_slot("acct", setter="set_acct")), config_id="c")
  assert 'passive_slot("acct")' in src


# --- announce ---------------------------------------------------------------


def test_an_announce_emits_each_of_its_flags():
  slot = flows.announce("bye", ["Goodbye."], message="model line",
                        requires=["done"], condition=flows.has("done"),
                        end=True, escalated=True, reason="escalated",
                        barge_in=False, shared=True, preempt=True)
  src = _render(_slots(slot), config_id="c")
  for kwarg in ('message="model line"', 'requires=["done"]', 'condition=has("done")',
                "end=True", "escalated=True", 'reason="escalated"', "barge_in=False",
                "shared=True", "preempt=True"):
    assert kwarg in src, kwarg


def test_an_announce_that_transfers_to_a_sibling_agent_keeps_the_kwarg():
  src = _render(_slots(flows.announce("go", ["One moment."], transfer_to="Billing")),
                config_id="c")
  assert 'transfer_to="Billing"' in src


def test_an_announce_that_ends_the_whole_conversation_keeps_that_kwarg():
  src = _render(_slots(flows.announce("bye", ["Bye."], end=True,
                                      end_conversation=True)), config_id="c")
  assert "end_conversation=True" in src


def test_a_model_rendered_announce_with_no_response_parts_is_still_a_builder_call():
  # No `response` at all — the content is a `message` the model renders. An absent
  # response is a valid shape rather than a mismatch.
  src = _render(_slots(flows.announce("a", [], message="say it")), config_id="c")
  assert 'announce("a", [], message="say it")' in src
  assert not _fell_back_to_raw(src)


@pytest.mark.parametrize(
    "response",
    [
        "not-a-list",                                          # response is not a list
        ["a bare string part"],                                # part is not a dict
        [{"type": "text", "text": "hi", "colour": "red"}],     # unknown key on a text
        [{"type": "transfer", "agent": "B", "why": "x"}],      # unknown key, transfer
        [{"type": "end_session", "reason": "done", "x": 1}],   # unknown key, end
        [{"type": "card", "data": {}}],                        # unknown part type
    ],
)
def test_an_announce_shape_no_builder_reproduces_falls_back_to_raw(response):
  src = _render(_slots({"name": "a", "source": "announce", "response": response}),
                config_id="c")
  assert "raw({" in src


def test_an_announce_whose_name_is_not_a_string_falls_back_to_raw():
  src = _render(_slots({"name": 3, "source": "announce", "response": []}),
                config_id="c")
  assert "raw({" in src


def test_a_non_interruptable_text_part_becomes_barge_in_false():
  src = _render(_slots(flows.announce("a", ["Hold."], barge_in=False)), config_id="c")
  assert 'announce("a", ["Hold."], barge_in=False)' in src
  assert not _fell_back_to_raw(src)


# --- hand-off ---------------------------------------------------------------


def _announce_with(parts: list) -> dict:
  """The dict `announce()` produces for a spoken line plus a trailing part list."""
  return {"name": "human", "source": "announce",
          "response": [{"type": "text", "text": "Hold on."}] + parts,
          "preempt": False}


_UJET_DATA = {"ujet": {"menu_id": "90", "escalation_reason": "by_virtual_agent",
                       "type": "action", "action": "escalation", "language": "en"}}


@pytest.mark.parametrize(
    ("tail", "why"),
    [
        ([{"type": "payload", "data": _UJET_DATA}, "not a dict"], "end is not a dict"),
        ([{"type": "payload", "data": _UJET_DATA},
          {"type": "text", "text": "bye"}], "second part is not an end_session"),
        ([{"type": "payload", "data": _UJET_DATA, "hint": "x"},
          {"type": "end_session", "reason": "transfer"}], "extra key on the payload"),
        ([{"type": "payload", "data": _UJET_DATA},
          {"type": "end_session", "reason": "transfer", "note": "x"}],
         "extra key on the end"),
        ([{"type": "payload", "data": {}},
          {"type": "end_session", "reason": "transfer"}], "empty vendor data"),
        ([{"type": "payload", "data": "nope"},
          {"type": "end_session", "reason": "transfer"}], "data is not a dict"),
        ([{"type": "payload", "data": _UJET_DATA},
          {"type": "end_session", "reason": ""}], "the end carries no reason"),
        ([{"type": "payload", "data": _UJET_DATA},
          {"type": "end_session"}], "the end carries no reason at all"),
        ([{"type": "payload", "data": _UJET_DATA}], "a payload with no end at all"),
    ],
)
def test_a_handoff_pair_that_cannot_be_rebuilt_falls_back_to_raw(tail, why):
  src = _render(_slots(_announce_with(tail)), config_id="c")
  assert "raw({" in src, why
  assert "handoff(" not in src, why


def test_a_handoff_the_builder_itself_refuses_falls_back_to_raw():
  # `{"capability": "payloads"}` is the one gate handoff() rejects outright, so the
  # nested builder call cannot be constructed and the slot drops to raw({...}).
  gate = {"capability": "payloads"}
  src = _render(
      _slots(_announce_with([
          {"type": "payload", "data": _UJET_DATA, "condition": gate},
          {"type": "end_session", "reason": "transfer", "escalated": True,
           "condition": gate},
      ])),
      config_id="c")
  assert "raw({" in src
  assert "handoff(" not in src


def test_a_ujet_payload_the_vendor_builder_refuses_still_renders_as_a_raw_handoff():
  # menu_id is blank, so ujet() raises and there is no vendor call to emit — but the
  # PAIR is still the invariant, so handoff() wraps the raw data.
  data = {"ujet": {"menu_id": "", "escalation_reason": "by_virtual_agent",
                   "type": "action", "action": "escalation", "language": "en"}}
  src = _render(
      _slots(_announce_with([
          {"type": "payload", "data": data},
          {"type": "end_session", "reason": "transfer", "escalated": True},
      ])),
      config_id="c")
  assert "handoff(" in src
  assert "ujet(" not in src
  assert "escalated=True" in src  # a raw payload must always be told


def test_a_handoff_gated_on_a_non_surface_condition_emits_condition_not_surface():
  gate = {"variable": "channel", "equals": "phone"}
  src = _render(
      _slots(_announce_with([
          {"type": "payload", "data": _UJET_DATA, "condition": gate},
          {"type": "end_session", "reason": "transfer", "escalated": True,
           "condition": gate},
      ])),
      config_id="c")
  assert 'condition={"variable": "channel", "equals": "phone"}' in src
  assert "surface=" not in src


def test_a_handoff_that_also_ends_the_session_has_no_builder_form():
  # announce() rejects two competing dispositions, so nothing can rebuild this.
  src = _render(
      _slots({"name": "human", "source": "announce",
              "response": [
                  {"type": "end_session", "reason": "completed"},
                  {"type": "payload", "data": _UJET_DATA},
                  {"type": "end_session", "reason": "transfer", "escalated": True},
              ],
              "preempt": False}),
      config_id="c")
  assert "raw({" in src


def test_the_ordinary_ujet_handoff_still_renders_through_both_builders():
  src = _render(_slots(flows.announce("human", ["Hold on."],
                                      handoff=flows.handoff(flows.ujet(menu_id="90")))),
                config_id="c")
  assert 'handoff=handoff(ujet(menu_id="90"))' in src
  assert not _fell_back_to_raw(src)


# --- event_slot / result_slot ----------------------------------------------


def test_an_event_slot_with_a_matching_key_takes_one_argument():
  src = _render(_slots(flows.event_slot("ani")), config_id="c")
  assert 'event_slot("ani")' in src


def test_an_event_slot_with_a_different_key_takes_two():
  src = _render(_slots(flows.event_slot("caller_id", "ani")), config_id="c")
  assert 'event_slot("caller_id", "ani")' in src


def test_an_event_slot_carrying_anything_extra_falls_back_to_raw():
  src = _render(_slots(flows.event_slot("ani", shared=True)), config_id="c")
  assert "raw({" in src


def test_a_result_slot_names_its_task():
  src = _render(_slots(flows.result_slot("status", "lookup")), config_id="c")
  assert 'result_slot("status", "lookup")' in src


def test_a_result_slot_with_a_default_falls_back_to_raw():
  src = _render(_slots(flows.result_slot("status", "lookup", default="none")),
                config_id="c")
  assert "raw({" in src


def test_an_unknown_slot_source_always_falls_back_to_raw():
  src = _render(_slots({"name": "x", "source": "system"}), config_id="c")
  assert 'raw({"name": "x", "source": "system"})' in src


# --- tasks ------------------------------------------------------------------


def test_a_short_task_renders_on_one_line():
  cfg = {"slots": [], "tasks": [flows.task("t", "do_it", ["a"], "b")]}
  src = _render(cfg, config_id="c")
  assert 'flow.task(task("t", "do_it", ["a"], "b"))\n' in src


def test_every_optional_task_field_is_emitted_in_signature_order():
  t = flows.task(
      "pay", "do_pay", ["amount"], "pay_result",
      out_key="result", extra_outputs={"conf": "confirmation"},
      requires=["amount", "acct"], condition=flows.has("amount"),
      success_check="ok", terminal=True, then_say="All set.",
      on_failure={"max_retries": 1}, clear_slots_on_success=["amount"])
  src = _render({"slots": [], "tasks": [t]}, config_id="c")
  call = _expr_for(src, "flow.task(")
  names = ["out_key=", "extra_outputs=", "requires=", "condition=", "success_check=",
           "terminal=", "then_say=", "on_failure=", "clear_slots_on_success="]
  order = [call.index(n) for n in names]
  assert order == sorted(order)
  assert not _fell_back_to_raw(src)


def test_the_concurrent_task_kwargs_are_used_when_the_builder_accepts_them():
  t = flows.task("pay", "do_pay", ["amount"], "pay_result",
                 transfer_to="Billing", readback_inputs=["amount"],
                 then_directive="wrap up", verbatim=True, automatic_fillers=False)
  src = _render({"slots": [], "tasks": [t]}, config_id="c")
  for kwarg in ('transfer_to="Billing"', 'readback_inputs=["amount"]',
                'then_directive="wrap up"', "verbatim=True", "automatic_fillers=False"):
    assert kwarg in src, kwarg
  assert not _fell_back_to_raw(src)


def test_an_on_complete_that_is_more_than_a_transfer_stays_on_complete():
  t = flows.task("pay", "do_pay", ["amount"], "pay_result",
                 on_complete={"transfer_to": "Billing", "say": "Transferring."})
  src = _render({"slots": [], "tasks": [t]}, config_id="c")
  assert 'on_complete={"transfer_to": "Billing", "say": "Transferring."}' in src


@pytest.mark.parametrize(
    "task_dict",
    [
        {"name": "t"},                                            # no tool
        {"name": "t", "tool": 5, "inputs": [], "outputs": {"a": "a"}},
        {"name": "t", "tool": "do", "inputs": "nope", "outputs": {"a": "a"}},
        {"name": "t", "tool": "do", "inputs": [], "outputs": {}},  # no outputs
        {"name": "t", "tool": "do", "inputs": [], "outputs": {"a": 7}},  # non-str slot
    ],
)
def test_a_task_no_builder_reproduces_falls_back_to_raw(task_dict):
  src = _render({"slots": [], "tasks": [task_dict]}, config_id="c")
  assert "flow.task(raw(" in src


def test_a_long_raw_task_wraps_and_still_round_trips():
  blob = {"name": "wide_task", "inputs": ["one", "two", "three"],
          "note": "long enough to force wrapping past the budget"}
  src = _render({"slots": [], "tasks": [blob]}, config_id="c")
  assert "flow.task(\n    raw(\n        {\n" in src
  assert all(len(line) <= 88 for line in src.splitlines())


def test_a_long_builder_task_wraps_one_kwarg_per_line():
  t = flows.task("collect_and_verify_the_account", "do_the_verification_lookup",
                 ["account_number", "zip_code"], "verification_result",
                 then_say="Thanks, that all checks out on my end.")
  src = _render({"slots": [], "tasks": [t]}, config_id="c")
  assert "flow.task(\n    task(\n" in src
  assert '        then_say="Thanks, that all checks out on my end.",\n' in src
  assert all(len(line) <= 88 for line in src.splitlines())


# --- components -------------------------------------------------------------


def test_a_component_task_is_emitted_through_the_component_builder():
  c = flows.component("verify", "identity_dag", inputs={"acct": "account"},
                      outputs={"verified": "is_verified"}, on_abort="fail",
                      requires=["acct"], condition=flows.has("acct"))
  src = _render({"slots": [], "tasks": [c]}, config_id="c")
  assert "component(" in src
  for kwarg in ('inputs={"acct": "account"}', 'outputs={"verified": "is_verified"}',
                'on_abort="fail"', 'requires=["acct"]', 'condition=has("acct")'):
    assert kwarg in src, kwarg
  assert not _fell_back_to_raw(src)


def test_a_minimal_component_takes_only_its_two_names():
  src = _render({"slots": [], "tasks": [flows.component("verify", "identity_dag")]},
                config_id="c")
  assert 'flow.task(component("verify", "identity_dag"))' in src


def test_a_component_whose_child_is_not_a_string_falls_back_to_raw():
  src = _render({"slots": [], "tasks": [{"name": "v", "component": 7}]}, config_id="c")
  assert "flow.task(raw(" in src


# --- module shape -----------------------------------------------------------


def test_the_import_block_lists_flow_first_then_used_builders_sorted():
  cfg = _slots(flows.user_slot("x", "Ask?", condition=flows.has("y")),
               flows.announce("a", ["Hi."]))
  src = _render(cfg, config_id="c")
  block = src[src.index("from flows import ("):src.index(")\n\nflow = ")]
  names = [line.strip().rstrip(",") for line in block.splitlines()[1:]]
  assert names[0] == "Flow"
  assert names[1:] == sorted(names[1:])
  assert set(names) == {"Flow", "announce", "has", "user_slot"}


def test_rendering_is_byte_stable_across_calls_for_every_shape_here():
  cfg = _slots(flows.user_slot("x", "Ask?"), flows.announce("a", ["Hi."]))
  cfg["tasks"] = [flows.task("t", "do", ["x"], "y")]
  a = R.render_config_source(cfg, config_id="c", root_agent="A")
  b = R.render_config_source(cfg, config_id="c", root_agent="A")
  assert a == b


# --- render_app_source ------------------------------------------------------


def test_render_app_source_splices_an_app_binding_before_the_main_footer():
  cfg = _slots(flows.user_slot("x", "Ask?"))
  src = R.render_app_source({"config": cfg, "config_id": "app_dag",
                             "root_agent": "Root", "app_display_name": "My App"})
  ast.parse(src)
  assert "import flows\n" in src
  assert 'app = flows.App(root_flow=flow, app_display_name="My App")' in src
  assert src.index("app = flows.App") < src.index('if __name__ == "__main__":')
  assert 'root_agent="Root"' in src


def test_render_app_source_defaults_the_display_name_to_the_config_id():
  src = R.render_app_source({"config": {"slots": [], "tasks": []},
                             "config_id": "billing_dag"})
  ast.parse(src)
  assert 'app_display_name="billing_dag"' in src


def test_the_rendered_app_module_executes_and_binds_a_real_app():
  cfg = _slots(flows.user_slot("zip", "ZIP?"))
  src = R.render_app_source({"config": cfg, "config_id": "zip_dag"})
  ns: dict = {}
  exec(src, ns)  # noqa: S102
  assert isinstance(ns["app"], flows.App)
  assert list(ns["flow"].to_config().items()) == list(cfg.items())


def test_raw_is_an_identity_passthrough():
  d = {"name": "x"}
  assert R.raw(d) is d


# ===========================================================================
# openapi.py
# ===========================================================================


_MIN_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/things/{id}": {
            "get": {
                "operationId": "getThing",
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {"sku": {"type": "string"}}}}}}},
            }
        }
    },
}


def test_a_spec_that_is_not_valid_yaml_says_so():
  with pytest.raises(ValueError, match="not valid YAML/JSON"):
    O.openapi_toolset("t", spec="{unclosed: [1, 2")


def test_a_spec_that_parses_to_something_other_than_a_mapping_is_refused():
  with pytest.raises(ValueError, match="must be an OpenAPI document"):
    O.openapi_toolset("t", spec="- one\n- two\n")


def test_a_base_url_must_be_absolute():
  with pytest.raises(ValueError, match="must be an absolute URL"):
    O.openapi_toolset("t", spec=_MIN_SPEC, base_url="api.example.test")


def test_a_declared_toolset_refuses_spec_keyed_mocks():
  with pytest.raises(ValueError, match="declares its operations instead"):
    O.openapi_toolset("t", mocks={"getThing": {"sku": "x"}})


def test_a_declared_toolset_with_no_remote_tools_refuses_to_render_a_spec():
  ts = O.openapi_toolset("empty_declared")
  with pytest.raises(ValueError, match="no `remote_tool"):
    ts.spec_document()


def test_a_ref_that_walks_off_the_document_resolves_to_nothing():
  spec = json.loads(json.dumps(_MIN_SPEC))
  spec["paths"]["/things/{id}"]["get"]["parameters"] = [
      {"$ref": "#/components/parameters/notThere/deeper"}]
  ts = O.openapi_toolset("t", spec=spec)
  assert ts.operations["getThing"].params == ()


def test_ref_pointer_escapes_are_decoded():
  spec = json.loads(json.dumps(_MIN_SPEC))
  spec["components"] = {"parameters": {"a/b~c": {"name": "escaped", "in": "query"}}}
  spec["paths"]["/things/{id}"]["get"]["parameters"] = [
      {"$ref": "#/components/parameters/a~1b~0c"}]
  ts = O.openapi_toolset("t", spec=spec)
  assert ts.operations["getThing"].params == ("escaped",)


def test_a_duplicate_parameter_name_is_only_listed_once():
  spec = json.loads(json.dumps(_MIN_SPEC))
  spec["paths"]["/things/{id}"]["parameters"] = [
      {"name": "id", "in": "path", "schema": {"type": "string"}}]
  spec["paths"]["/things/{id}"]["get"]["parameters"].append({"in": "query"})  # no name
  ts = O.openapi_toolset("t", spec=spec)
  assert ts.operations["getThing"].params == ("id",)


def test_a_deeply_nested_all_of_chain_stops_composing_rather_than_recursing_forever():
  spec = json.loads(json.dumps(_MIN_SPEC))
  schema: dict = {"type": "object", "properties": {"leaf": {"type": "string"}}}
  for _ in range(12):
    schema = {"allOf": [schema]}
  spec["paths"]["/things/{id}"]["get"]["responses"]["200"]["content"][
      "application/json"]["schema"] = schema
  ts = O.openapi_toolset("t", spec=spec)
  assert ts.operations["getThing"].response_paths == ()


def test_a_deeply_nested_response_object_stops_at_the_depth_bound():
  spec = json.loads(json.dumps(_MIN_SPEC))
  schema: dict = {"type": "string"}
  for i in range(12):
    schema = {"type": "object", "properties": {f"l{i}": schema}}
  spec["paths"]["/things/{id}"]["get"]["responses"]["200"]["content"][
      "application/json"]["schema"] = schema
  ts = O.openapi_toolset("t", spec=spec)
  assert ts.operations["getThing"].response_paths == ()


def test_a_non_2xx_response_is_not_read_for_the_output_schema():
  spec = json.loads(json.dumps(_MIN_SPEC))
  responses = spec["paths"]["/things/{id}"]["get"]["responses"]
  responses["404"] = {"description": "gone", "content": {"application/json": {
      "schema": {"type": "object", "properties": {"oops": {"type": "string"}}}}}}
  spec["paths"]["/things/{id}"]["get"]["responses"] = {
      "404": responses["404"], "200": responses["200"]}
  ts = O.openapi_toolset("t", spec=spec)
  assert ts.operations["getThing"].response_paths == ("sku",)


def test_a_2xx_response_with_no_json_media_type_yields_no_paths():
  spec = json.loads(json.dumps(_MIN_SPEC))
  spec["paths"]["/things/{id}"]["get"]["responses"]["200"] = {
      "description": "ok", "content": {"text/plain": {"schema": {"type": "string"}}}}
  ts = O.openapi_toolset("t", spec=spec)
  assert ts.operations["getThing"].response_paths == ()


def test_junk_beside_the_operations_is_skipped_by_every_scan():
  spec = json.loads(json.dumps(_MIN_SPEC))
  spec["paths"]["/junk"] = "not a path item"          # path item is not a mapping
  spec["paths"]["/things/{id}"]["summary"] = "a shared summary, not a method"
  spec["paths"]["/things/{id}"]["put"] = "not an operation"
  ts = O.openapi_toolset("t", spec=spec)
  assert set(ts.operations) == {"getThing"}


def test_a_document_with_no_paths_at_all_says_nothing_in_it_is_reachable():
  with pytest.raises(ValueError, match="Add an operationId to: \\(none\\)"):
    O.openapi_toolset("t", spec={"openapi": "3.0.3", "paths": {}})


def test_an_operation_with_no_operation_id_is_named_and_junk_beside_it_is_skipped():
  spec = {"openapi": "3.0.3", "paths": {
      "/junk": "not a path item",
      "/a": {"summary": "not a method", "put": "not an operation",
             "get": {"responses": {}}}}}
  with pytest.raises(ValueError, match=r"Add an operationId to: GET /a$"):
    O.openapi_toolset("t", spec=spec)


def test_an_operation_missing_a_description_names_the_offender_including_junk_paths():
  spec = json.loads(json.dumps(_MIN_SPEC))
  spec["paths"]["/junk"] = "not a path item"
  spec["paths"]["/things/{id}"]["put"] = "not an operation"
  del spec["paths"]["/things/{id}"]["get"]["responses"]["200"]["description"]
  with pytest.raises(ValueError, match="GET /things/\\{id\\} -> 200"):
    O.openapi_toolset("t", spec=spec)


def test_an_api_tool_stringifies_to_the_name_a_task_would_name(monkeypatch):
  T.clear_registry()
  ts = O.openapi_toolset("things", spec=_MIN_SPEC)
  at = O.api_tool("fetch_thing", ts, "getThing", outputs={"sku": "sku"})
  assert str(at) == "fetch_thing" == at.name
  T.clear_registry()


# --- remote tools -----------------------------------------------------------


@pytest.fixture()
def declared():
  """A fresh declared toolset, with the process-wide remote registries cleaned up."""
  T.clear_registry()
  O._DECLARED.clear()
  O._REMOTE_TOOLS.clear()
  ts = O.openapi_toolset("jobs", base_url="https://jobs.example.test")
  yield ts
  T.clear_registry()
  O._DECLARED.clear()
  O._REMOTE_TOOLS.clear()


def test_after_turns_refuses_a_zero_or_negative_wait():
  with pytest.raises(ValueError, match="at least 1"):
    O.after_turns(0, {"a": 1})


def test_remote_tool_requires_a_real_toolset():
  with pytest.raises(ValueError, match="must be a flows.openapi_toolset"):
    O.remote_tool("r", "not a toolset", "runJob", params={"a": str},
                  outputs={"b": str})


def test_remote_tool_refuses_a_toolset_built_from_someone_elses_spec():
  ts = O.openapi_toolset("consumed", spec=_MIN_SPEC)
  with pytest.raises(ValueError, match="its operations are fixed"):
    O.remote_tool("r", ts, "runJob", params={"a": str}, outputs={"b": str})


def test_a_remote_tool_name_must_be_a_python_identifier(declared):
  with pytest.raises(ValueError, match="must be a python identifier"):
    O.remote_tool("run-job", declared, "runJob", params={"a": str},
                  outputs={"b": str})


def test_a_remote_tool_needs_at_least_one_param(declared):
  with pytest.raises(ValueError, match="declare at least one param"):
    O.remote_tool("run_job", declared, "runJob", params={}, outputs={"b": str})


def test_a_remote_tool_needs_at_least_one_output(declared):
  with pytest.raises(ValueError, match="declare at least one output"):
    O.remote_tool("run_job", declared, "runJob", params={"a": str}, outputs={})


@pytest.mark.parametrize("key", ["success", "error", "response", "status",
                                 "error_code"])
def test_reserved_output_names_are_refused_on_a_remote_tool(declared, key):
  with pytest.raises(ValueError, match="is reserved"):
    O.remote_tool("run_job", declared, "runJob", params={"a": str},
                  outputs={key: str})


def test_a_remote_tool_timeout_must_be_positive(declared):
  with pytest.raises(ValueError, match="timeout must be positive"):
    O.remote_tool("run_job", declared, "runJob", params={"a": str},
                  outputs={"b": str}, timeout=0)


def test_a_wire_type_openapi_cannot_express_is_refused(declared):
  # The generated spec is what the SERVICE implements, so a type OpenAPI cannot
  # express has nothing to generate. Caught when the document is rendered.
  O.remote_tool("run_job", declared, "runJob", params={"a": str},
                outputs={"b": dict})
  with pytest.raises(ValueError, match="wire types must be one of"):
    declared.spec_document()


def test_two_remote_tools_cannot_share_one_operation_id(declared):
  O.remote_tool("run_job", declared, "runJob", params={"a": str}, outputs={"b": str})
  with pytest.raises(ValueError, match="is already declared"):
    O.remote_tool("run_again", declared, "runJob", params={"a": str},
                  outputs={"b": str})


def test_a_declared_remote_tool_is_in_the_registry_the_build_reads(declared):
  rt = O.remote_tool("run_job", declared, "runJob", params={"a": str},
                     outputs={"b": str}, timeout=45)
  registry = O.registered_remote_tools()
  assert registry["run_job"] is rt
  entry = O.remote_registry(["run_job"])["run_job"]
  assert entry["status_tool"] == rt.status_tool
  assert entry["outputs"] == ["b"]
  assert entry["timeout_seconds"] == 45


def test_a_remote_mock_must_be_json_serializable(declared):
  with pytest.raises(ValueError, match="must be JSON-serializable"):
    O.remote_tool("run_job", declared, "runJob", params={"a": str},
                  outputs={"b": str}, mock={"when": {1, 2}})


def test_a_remote_mock_code_block_takes_no_parameters(declared):
  def answer(job_id):  # noqa: ARG001 — the point is that it asks for an argument
    return {"b": "done"}

  with pytest.raises(ValueError, match="a poll cannot supply it"):
    O.remote_tool("run_job", declared, "runJob", params={"a": str},
                  outputs={"b": str}, mock=answer)


def test_a_remote_mock_whose_source_cannot_be_read_is_refused(declared):
  ns: dict = {}
  exec("def answer():\n  return {'b': 'done'}\n", ns)  # noqa: S102 — no source on disk
  with pytest.raises(ValueError, match="no source that can be read"):
    O.remote_tool("run_job", declared, "runJob", params={"a": str},
                  outputs={"b": str}, mock=ns["answer"])


def test_a_mock_that_is_a_class_is_refused_before_its_signature_is_read(declared):
  with pytest.raises(ValueError, match="is a CLASS"):
    O.remote_tool("run_job", declared, "runJob", params={"a": str},
                  outputs={"b": str}, mock=dict)


def test_a_mock_whose_signature_cannot_be_read_is_accepted_rather_than_crashing(
    declared):
  # `inspect.signature` gives up on some callables (C functions, anything carrying a
  # bogus `__signature__`). There is nothing to check, so the poll's no-parameters
  # rule must step aside rather than take the build down.
  def opaque_answer():
    return {"b": "done"}

  opaque_answer.__signature__ = "not a signature"
  O.remote_tool("run_job", declared, "runJob", params={"a": str},
                outputs={"b": str}, mock=opaque_answer)
  assert "run_job" in O.registered_remote_tools()


def test_a_declared_spec_carries_both_the_start_and_the_status_operation(declared):
  O.remote_tool("run_job", declared, "runJob", params={"a": str},
                outputs={"b": str}, mock=O.after_turns(2, {"b": "done"}))
  doc = declared.spec_document()
  assert "/runJob" in doc
  assert set(declared.operations) == {"runJob", "runJobStatus"}


# ===========================================================================
# integrity.py
# ===========================================================================


def _write(path: str, text: str) -> None:
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w", encoding="utf-8") as fh:
    fh.write(text)


def test_a_clean_report_says_so_and_lists_nothing():
  rep = I.IntegrityReport()
  assert rep.ok
  assert rep.summary() == (
      "framework in sync; every declared variable, agent and tool present")


def test_every_violation_class_reaches_the_summary():
  rep = I.IntegrityReport(
      missing_variables=["ani"], missing_agents=["Billing"],
      missing_tools=["do_pay"], unresolved_agent_tools=["Root -> faq"],
      unlanded_settings=["timeZoneSettings (declared, app.json has none)"],
      framework_missing=["tools/engine/engine.py"], framework_stale=["a.py"],
      broken=["app.json is missing"], undispatchable=["t: no return annotation"])
  assert not rep.ok
  text = rep.summary()
  for fragment in ("unusable: app.json is missing", "1 declared variable(s)",
                   "missing agent(s): Billing", "missing tool resource(s): do_pay",
                   "agent lists a tool the app does not contain: Root -> faq",
                   "declared app-level setting(s) never landed",
                   "tool(s) CES will never dispatch",
                   "missing framework file(s): tools/engine/engine.py",
                   "framework file(s) off the blessed manifest"):
    assert fragment in text, fragment


@pytest.mark.parametrize(
    "field",
    ["missing_variables", "missing_agents", "missing_tools",
     "unresolved_agent_tools", "unlanded_settings", "framework_missing",
     "framework_stale", "broken", "undispatchable"],
)
def test_any_single_finding_flips_ok_to_false(field):
  assert not I.IntegrityReport(**{field: ["x"]}).ok


def test_a_dir_with_no_tools_dir_has_nothing_undispatchable(tmp_path):
  assert I.undispatchable_tools(str(tmp_path)) == []


def test_a_tool_dir_with_no_resource_json_is_skipped(tmp_path):
  os.makedirs(tmp_path / "tools" / "orphan")
  assert I.undispatchable_tools(str(tmp_path)) == []


def test_an_unreadable_tool_resource_is_left_to_the_broken_check(tmp_path):
  _write(str(tmp_path / "tools" / "bad" / "bad.json"), "{not json")
  assert I.undispatchable_tools(str(tmp_path)) == []


def test_a_tool_resource_with_no_python_body_is_not_flagged(tmp_path):
  _write(str(tmp_path / "tools" / "remote" / "remote.json"),
         json.dumps({"displayName": "remote", "remoteAgentTool": {}}))
  assert I.undispatchable_tools(str(tmp_path)) == []


def test_a_tool_whose_body_file_is_missing_is_reported_as_unreadable(tmp_path):
  _write(str(tmp_path / "tools" / "gone" / "gone.json"),
         json.dumps({"pythonFunction": {"name": "gone",
                                        "pythonCode": "tools/gone/nope.py"}}))
  found = I.undispatchable_tools(str(tmp_path))
  assert len(found) == 1
  assert "unreadable or will not parse" in found[0]


def test_an_eager_forward_reference_in_a_top_level_function_is_flagged(tmp_path):
  _write(str(tmp_path / "tools" / "fwd" / "fwd.json"),
         json.dumps({"pythonFunction": {"name": "fwd",
                                        "pythonCode": "tools/fwd/code.py"}}))
  _write(str(tmp_path / "tools" / "fwd" / "code.py"),
         "def fwd(payload: Later) -> dict:\n"
         "  return {'ok': True}\n"
         "\n"
         "\n"
         "class Later:\n"
         "  pass\n")
  found = I.undispatchable_tools(str(tmp_path))
  assert len(found) == 1
  assert "forward reference" in found[0]


def test_a_class_attribute_annotation_naming_a_later_class_is_flagged(tmp_path):
  _write(str(tmp_path / "tools" / "cls" / "cls.json"),
         json.dumps({"pythonFunction": {"name": "run",
                                        "pythonCode": "tools/cls/code.py"}}))
  _write(str(tmp_path / "tools" / "cls" / "code.py"),
         "class Order:\n"
         "  line: LineItem\n"
         "\n"
         "\n"
         "class LineItem:\n"
         "  pass\n"
         "\n"
         "\n"
         "def run() -> dict:\n"
         "  return {'ok': True}\n")
  found = I.undispatchable_tools(str(tmp_path))
  assert any("forward reference" in line for line in found)


def test_a_method_argument_annotation_that_self_references_is_flagged(tmp_path):
  _write(str(tmp_path / "tools" / "sr" / "sr.json"),
         json.dumps({"pythonFunction": {"name": "run",
                                        "pythonCode": "tools/sr/code.py"}}))
  _write(str(tmp_path / "tools" / "sr" / "code.py"),
         "class Node:\n"
         "  def link(self, other: Node) -> None:\n"
         "    pass\n"
         "\n"
         "\n"
         "def run() -> dict:\n"
         "  return {'ok': True}\n")
  found = I.undispatchable_tools(str(tmp_path))
  assert any("self-reference" in line for line in found)


def test_a_missing_app_json_is_reported_as_broken(tmp_path):
  rep = I.IntegrityReport()
  assert I._read_json(str(tmp_path / "app.json"), rep, "app.json") is None
  assert rep.broken == ["app.json is missing"]


def test_an_unreadable_app_json_is_reported_as_broken(tmp_path):
  _write(str(tmp_path / "app.json"), "{nope")
  rep = I.IntegrityReport()
  assert I._read_json(str(tmp_path / "app.json"), rep, "app.json") is None
  assert len(rep.broken) == 1
  assert "unreadable" in rep.broken[0]


def test_agent_scanning_survives_a_missing_dir_a_bare_dir_and_bad_json(tmp_path):
  assert I._agent_jsons(str(tmp_path)) == {}
  os.makedirs(tmp_path / "agents" / "Bare")
  _write(str(tmp_path / "agents" / "Broken" / "Broken.json"), "{nope")
  _write(str(tmp_path / "agents" / "Good" / "Good.json"), json.dumps({"tools": []}))
  assert set(I._agent_jsons(str(tmp_path))) == {"Good"}


def test_an_agent_whose_tools_key_is_not_a_list_is_skipped(tmp_path):
  found = I._unresolved_agent_tools(str(tmp_path), {"A": {"tools": "do_pay"},
                                                    "B": {"tools": [7, "end_session"]}})
  assert found == []


def test_config_tool_names_reads_task_executors_and_slot_setters():
  names = I.config_tool_names([
      "not a config",
      {"tasks": [{"tool": "do_pay"}, "junk", {"tool": ""}],
       "slots": [{"setter": "set_zip"}, "junk", {"name": "x"}]},
      None,
  ])
  assert names == {"do_pay", "set_zip"}


def test_variable_names_dedupes_and_keeps_source_order():
  assert I.variable_names([{"name": "b"}, "a", {"name": "b"}, {"nope": 1}, ""]) == [
      "b", "a"]


def test_an_app_declaring_no_settings_writes_no_sidecar(tmp_path):
  I.write_declared_settings(str(tmp_path), [])
  assert not os.path.exists(tmp_path / I.DECLARED_SETTINGS_FILE)
  assert I.declared_setting_keys(str(tmp_path)) == []


def test_the_sidecar_round_trips_the_declared_keys(tmp_path):
  I.write_declared_settings(str(tmp_path), ["guardrails", "timeZoneSettings",
                                            "guardrails", ""])
  assert I.declared_setting_keys(str(tmp_path)) == ["guardrails", "timeZoneSettings"]


@pytest.mark.parametrize("body", ['{"declared": "guardrails"}', '["guardrails"]',
                                  "{not json"])
def test_a_corrupt_sidecar_means_nothing_is_declared(tmp_path, body):
  _write(str(tmp_path / I.DECLARED_SETTINGS_FILE), body)
  assert I.declared_setting_keys(str(tmp_path)) == []


def test_brief_shortens_and_falls_back_to_repr():
  assert I.brief({"a": 1}) == '{"a": 1}'
  assert I.brief("x" * 200).endswith("…")
  assert len(I.brief("x" * 200)) == 60
  assert I.brief({1, 2}) in ("{1, 2}", "{2, 1}")


def test_unlanded_settings_diffs_values_when_it_has_them_and_presence_when_it_does_not():
  app_json = {"guardrails": {"a": 1}, "loggingSettings": {}}
  assert I.unlanded_settings(app_json, {"guardrails": {"a": 1}}) == []
  assert I.unlanded_settings(app_json, {"guardrails": {"a": 2}}) == [
      'guardrails (declared {"a": 2}, app.json has {"a": 1})']
  assert I.unlanded_settings(app_json, ["timeZoneSettings"]) == [
      "timeZoneSettings (declared, app.json has none)"]
  assert I.unlanded_settings(app_json, ["guardrails"]) == []
  assert I.unlanded_settings(app_json, None) == []


def test_emitted_tool_names_reads_only_the_tool_resource_paths():
  class _F:

    def __init__(self, path):
      self.path = path

  assert I.emitted_tool_names([_F("tools/do_pay/do_pay.json"),
                               _F("tools/do_pay/python_code.py"),
                               _F("agents/Root/Root.json"),
                               _F(None), None]) == {"do_pay"}


def test_verify_dir_on_a_path_that_is_not_a_directory(tmp_path):
  rep = I.verify_dir(str(tmp_path / "nope"))
  assert not rep.ok
  assert "is not a directory" in rep.broken[0]


def test_verify_dir_reports_an_app_with_no_agents_at_all(tmp_path):
  _write(str(tmp_path / "app.json"), json.dumps({"rootAgent": "Root"}))
  rep = I.verify_dir(str(tmp_path))
  assert "no agents/<name>/<name>.json in the app dir" in rep.broken


def test_verify_dir_reports_an_app_json_with_no_root_agent(tmp_path):
  _write(str(tmp_path / "app.json"), json.dumps({"displayName": "x"}))
  _write(str(tmp_path / "agents" / "Root" / "Root.json"), json.dumps({"tools": []}))
  rep = I.verify_dir(str(tmp_path))
  assert "app.json declares no rootAgent" in rep.broken


def test_verify_dir_reports_a_root_agent_that_is_not_in_the_tree(tmp_path):
  _write(str(tmp_path / "app.json"), json.dumps({"rootAgent": "Missing"}))
  _write(str(tmp_path / "agents" / "Root" / "Root.json"), json.dumps({"tools": []}))
  rep = I.verify_dir(str(tmp_path))
  assert rep.missing_agents == ["Missing"]


def test_verify_emitted_reports_a_declared_setting_that_never_landed(tmp_path):
  _write(str(tmp_path / "app.json"),
         json.dumps({"rootAgent": "Root", "variableDeclarations": [{"name": "ani"}]}))
  _write(str(tmp_path / "agents" / "Root" / "Root.json"), json.dumps({"tools": []}))
  rep = I.verify_emitted(str(tmp_path), agents=["Root"], variables=["ani", "mock_json"],
                         tools=["end_session"],
                         settings={"timeZoneSettings": {"tz": "UTC"}},
                         framework=False)
  assert rep.missing_variables == ["mock_json"]
  assert rep.unlanded_settings == ["timeZoneSettings (declared, app.json has none)"]
  assert rep.missing_tools == []       # end_session is a CES builtin
  assert rep.missing_agents == []


def test_verify_emitted_reports_a_tool_resource_that_never_landed(tmp_path):
  _write(str(tmp_path / "app.json"), json.dumps({"rootAgent": "Root"}))
  rep = I.verify_emitted(str(tmp_path), agents=["Root", "Billing"], variables=[],
                         tools=["do_pay"], framework=False)
  assert rep.missing_tools == ["do_pay"]
  assert rep.missing_agents == ["Root", "Billing"]


def test_discard_tree_is_a_no_op_for_a_path_that_is_not_there(tmp_path):
  assert I.discard_tree(None) == ""
  assert I.discard_tree(str(tmp_path / "nope")) == ""


def test_discard_tree_removes_the_carcass_by_default(tmp_path):
  target = tmp_path / "app"
  os.makedirs(target)
  note = I.discard_tree(str(target), "because")
  assert not target.exists()
  assert "removed the incomplete app dir" in note


def test_discard_tree_stamps_a_kept_tree_so_nobody_deploys_it(tmp_path):
  target = tmp_path / "app"
  os.makedirs(target)
  note = I.discard_tree(str(target), "the reason", keep=True, what="flows deploy")
  marker = (target / I.FAILED_MARKER).read_text(encoding="utf-8")
  assert "`flows deploy` FAILED" in marker
  assert "DO NOT DEPLOY IT." in marker
  assert "the reason" in marker
  assert I.FAILED_MARKER in note


# ===========================================================================
# handoff.py
# ===========================================================================


def test_a_ujet_extra_cannot_shadow_a_named_argument():
  with pytest.raises(ValueError, match="already a named argument"):
    H.ujet(menu_id="90", extra={"menu_id": "91"})


def test_a_ujet_extra_field_rides_along_after_the_named_ones():
  payload = H.ujet(menu_id="90", extra={"priority": "high"})
  assert list(payload.data["ujet"]) == ["menu_id", "escalation_reason", "type",
                                        "action", "language", "priority"]


def test_a_dialogflow_target_is_either_the_path_or_the_three_parts_never_both():
  with pytest.raises(ValueError, match="not both"):
    H.dialogflow_cx(agent="projects/p/locations/us/agents/a", project="p")


def test_dialogflow_parameters_must_be_a_mapping():
  with pytest.raises(TypeError, match="expected a dict of session parameters"):
    H.dialogflow_cx(agent="projects/p/locations/us/agents/a", parameters=["a"])


def test_empty_dialogflow_parameters_are_left_out_of_the_payload():
  payload = H.dialogflow_cx(agent="projects/p/locations/us/agents/a", parameters={})
  assert payload.data == {H.DIALOGFLOW_KEY: "projects/p/locations/us/agents/a"}


def test_a_handoff_unpacks_as_its_two_parts():
  h = H.handoff(H.ujet(menu_id="90"))
  first, second = [*h]
  assert first["type"] == "payload"
  assert second["type"] == "end_session"
  assert list(h) == h.parts()


def test_a_condition_naming_payloads_anywhere_in_a_list_is_still_refused():
  with pytest.raises(ValueError, match="`payloads` capability"):
    H.handoff(H.ujet(menu_id="90"),
              condition={"any": [{"surface": "voice"},
                                 {"capability": "payloads"}]})


def test_handoff_refuses_a_handoff():
  h = H.handoff(H.ujet(menu_id="90"))
  with pytest.raises(TypeError, match="already a Handoff"):
    H.handoff(h)


def test_handoff_refuses_an_empty_vendor_payload():
  with pytest.raises(ValueError, match="nothing for the\n?\\s*platform to route on"):
    H.handoff({}, escalated=True)


def test_handoff_refuses_something_that_is_not_a_payload_at_all():
  with pytest.raises(TypeError, match="expected flows.ujet"):
    H.handoff(42, escalated=True)


def test_handoff_refuses_surface_and_condition_together():
  with pytest.raises(ValueError, match="not both"):
    H.handoff(H.ujet(menu_id="90"), surface="voice", condition={"surface": "chat"})


def test_handoff_refuses_a_condition_that_is_not_a_dict():
  with pytest.raises(TypeError, match="expected a declarative condition dict"):
    H.handoff(H.ujet(menu_id="90"), condition="voice")


def test_handoff_refuses_a_blank_reason():
  with pytest.raises(ValueError, match="reason is required"):
    H.handoff(H.ujet(menu_id="90"), reason="  ")


def test_a_raw_payload_keeps_the_escalated_flag_it_was_given():
  h = H.handoff({"genesys": {"queue": "7"}}, escalated=False)
  assert h.escalated is False
  assert h.parts()[1] == {"type": "end_session", "reason": H.HANDOFF_REASON}


def test_as_handoff_passes_a_handoff_through_and_promotes_a_payload():
  h = H.handoff(H.ujet(menu_id="90"))
  assert H.as_handoff(h, "announce()") is h
  promoted = H.as_handoff(H.ujet(menu_id="90"), "announce()")
  assert isinstance(promoted, H.Handoff)
  assert promoted.escalated is True


def test_as_handoff_refuses_a_raw_dict_and_says_how_to_wrap_it():
  with pytest.raises(TypeError, match="Wrap a raw vendor payload"):
    H.as_handoff({"genesys": {}}, "announce()")


def test_as_handoff_refuses_anything_else_by_type_name():
  with pytest.raises(TypeError, match="got int"):
    H.as_handoff(7, "announce()")


def test_the_payload_part_is_a_fresh_copy_each_time():
  payload = H.ujet(menu_id="90")
  first = payload.payload_part()
  first["data"]["ujet"]["menu_id"] = "tampered"
  assert payload.payload_part()["data"]["ujet"]["menu_id"] == "90"


def test_a_conditioned_pair_carries_independent_copies_of_the_gate():
  h = H.handoff(H.ujet(menu_id="90"), surface="voice")
  payload_part, end = h.parts()
  payload_part["condition"]["surface"] = "chat"
  assert end["condition"] == {"surface": "voice"}
  assert h.parts()[0]["condition"] == {"surface": "voice"}


@pytest.mark.parametrize("key", ["then", "response", "fill"])
def test_a_handoff_exhaust_refuses_a_competing_disposition(key):
  h = H.handoff(H.ujet(menu_id="90"))
  with pytest.raises(ValueError, match="cannot be combined with a hand-off"):
    h.on_exhaust("Sorry.", **{key: "x"})


def test_a_handoff_exhaust_with_no_say_is_just_the_parts():
  block = H.handoff(H.ujet(menu_id="90")).on_exhaust()
  assert "say" not in block
  assert [p["type"] for p in block["response"]] == ["payload", "end_session"]


# ===========================================================================
# tools.py
# ===========================================================================


@pytest.fixture()
def registry():
  T.clear_registry()
  yield
  T.clear_registry()


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "30"])
def test_a_tool_timeout_must_be_a_positive_whole_number_of_seconds(registry, bad):
  with pytest.raises(ValueError, match="positive whole number of SECONDS"):
    T.tool(timeout=bad)


def test_a_valid_timeout_and_async_flag_reach_the_registry(registry):
  @T.tool(flow="f", asynchronous=True, timeout=90)
  def slow_job(job_id: str) -> dict:
    return {"success": True}

  assert T.registered_async_tools() == {"slow_job"}
  assert T.registered_tool_timeouts() == {"slow_job": 90}


def test_a_var_kwargs_tool_still_registers_and_still_renders(registry):
  # The WARNING itself is pinned in test_authoring_hardening.py. What matters here is
  # the other half of that decision: authoring only warns, so the tool is registered
  # and rendered exactly as before — CES is what drops it, at deploy.
  with pytest.warns(UserWarning, match="silently dropped"):

    @T.tool(flow="f")
    def anything(**kwargs) -> dict:
      return {"success": True}

  assert "def anything(**kwargs) -> dict:" in T.collect_tools(["f"])["anything"]


def test_a_tool_whose_signature_cannot_be_inspected_does_not_warn(registry):
  class _Opaque:
    __name__ = "opaque_tool"

    def __call__(self):
      return {}

  opaque = _Opaque()
  with warnings.catch_warnings():
    warnings.simplefilter("error")
    T._warn_on_var_params(object(), "not_callable")   # no signature to read
  assert T.tool(name="opaque_tool", flow="f")(opaque) is opaque


def test_output_keys_come_off_a_literal_dict_return(registry):
  @T.tool(flow="f")
  def literal_tool(a: str) -> dict:
    if a:
      return {"success": True, "value": a}
    return {"success": False}

  assert T.registered_output_keys()["literal_tool"] == ["success", "value"]


def test_a_spread_in_a_returned_dict_gives_up_on_reading_keys(registry):
  extra = {"b": 2}

  @T.tool(flow="f")
  def spread_tool() -> dict:
    return {"success": True, **extra}

  assert T.registered_output_keys()["spread_tool"] == []


def test_a_computed_key_in_a_returned_dict_gives_up_too(registry):
  key = "success"

  @T.tool(flow="f")
  def computed_tool() -> dict:
    return {key: True}

  assert T.registered_output_keys()["computed_tool"] == []


def test_a_nested_helpers_returns_are_not_read_as_the_tools_own(registry):
  @T.tool(flow="f")
  def outer_tool() -> dict:

    def helper():
      return {"not": "mine"}

    return {"success": True, "inner": helper()}

  assert T.registered_output_keys()["outer_tool"] == ["inner", "success"]


def test_a_tool_with_no_readable_source_declares_no_output_keys(registry):
  ns: dict = {}
  exec("def made_up():\n  return {'success': True}\n", ns)  # noqa: S102
  spec = T.tool(name="made_up", flow="f")(ns["made_up"])
  assert T.registered_output_keys()["made_up"] == []
  assert spec is ns["made_up"]


def test_a_lambda_tool_declares_no_output_keys(registry):
  fn = T.tool(name="lambda_tool", flow="f")(lambda: {"success": True})
  assert T.registered_output_keys()["lambda_tool"] == []
  assert callable(fn)


def test_an_unresolvable_annotation_falls_back_to_the_raw_annotation_dict(registry):
  def fn(a: NotAThing) -> AlsoNotAThing:  # noqa: F821 — deliberately unresolvable
    return {"success": True}

  assert T._hints(fn) == {"a": "NotAThing", "return": "AlsoNotAThing"}
  T.tool(name="unresolvable", flow="f")(fn)
  assert T.registered_output_keys()["unresolvable"] == ["success"]


def test_a_source_registered_tool_is_emitted_verbatim(registry):
  spec = T.register_source_tool("gen", "def gen() -> dict:\n  return {}\n",
                                flows=["f"], output_keys=["a"],
                                meta={"operation": "getThing"})
  assert T.render_tool(spec) == "def gen() -> dict:\n  return {}\n"
  assert T.registered_meta() == {"gen": {"operation": "getThing"}}
  assert T.unresolved_globals(spec) == []


def test_collect_tools_pulls_an_agent_scoped_tool_in_by_name(registry):
  @T.tool(flow="other")
  def faq_lookup(q: str) -> dict:
    return {"success": True}

  assert "faq_lookup" not in T.collect_tools(["f"])
  assert "faq_lookup" in T.collect_tools(["f"], names=["faq_lookup"])


def test_a_model_reached_twice_is_inlined_once_dependency_first(registry):
  @T.tool(flow="f")
  def walk_tree(node: TreeNode, other: TreeReply) -> TreeReply:
    return TreeReply(root=node, also=node)

  rendered = T.render_tool(T._REGISTRY["walk_tree"])
  assert rendered.count("class TreeNode(BaseModel):") == 1
  assert rendered.index("class TreeNode") < rendered.index("class TreeReply")
  assert "_DECLARED_OUTPUTS = {'root': None, 'also': None}" in rendered


def test_a_self_referential_model_does_not_loop_forever(registry):
  @T.tool(flow="f")
  def walk_chain(node: ChainNode) -> ChainNode:
    return node

  rendered = T.render_tool(T._REGISTRY["walk_chain"])
  assert rendered.count("class ChainNode(BaseModel):") == 1


@pytest.mark.parametrize(
    ("annotation", "normalized"),
    [("Optional[dict]", True), ("Union[dict, str]", True), ("dict | str", True),
     ('"dict | str"', True), ("dict", False), ("list", False),
     ('"dict |"', False), ('"not valid ***"', False)],
)
def test_only_union_return_annotations_are_rewritten_to_plain_dict(annotation,
                                                                  normalized):
  src = (f"def entry(a: str) -> {annotation}:\n"
         "  return {'success': True}\n")
  out = T._dict_return_if_union(src, "entry")
  assert ("-> dict:" in out) is (normalized or annotation == "dict")
  ast.parse(out)


def test_a_body_that_will_not_parse_is_returned_unchanged():
  broken = "def entry(:\n  pass\n"
  assert T._dict_return_if_union(broken, "entry") == broken


def test_a_body_with_no_matching_entry_function_is_returned_unchanged():
  src = "def other() -> dict | str:\n  return {}\n"
  assert T._dict_return_if_union(src, "entry") == src


def test_a_global_the_body_closes_over_is_carried_with_it(registry):
  """A module-level name the body reads is inlined, so it is no longer unresolved."""
  @T.tool(flow="f")
  def leaky_tool(a: str) -> dict:
    return {"success": True, "sentinel": _COV_SENTINEL}

  assert "_COV_SENTINEL" in T.render_tool(T._REGISTRY["leaky_tool"])
  assert T.unresolved_globals(T._REGISTRY["leaky_tool"]) == []
  assert T.registered_unresolved_globals() == {}


def test_a_name_that_cannot_be_carried_is_still_reported(registry):
  """The guard survives, narrowed to what inlining cannot fix.

  `sep` is bound at module level by an IMPORT, so there is no assignment to copy and no
  source to emit. That is the case the sandbox still cannot resolve, and it must still
  be caught at build time rather than on a live call — which is the whole reason this
  check exists (ces-probes 86: one deploy and one live drive to find a `SENTINEL`).
  """
  @T.tool(flow="f")
  def imports_tool(a: str) -> dict:
    return {"success": True, "sep": _COV_IMPORTED_SEP}

  assert T.unresolved_globals(T._REGISTRY["imports_tool"]) == ["_COV_IMPORTED_SEP"]


def test_every_way_a_body_binds_a_name_counts_as_bound(registry):
  @T.tool(flow="f")
  def binding_tool(a: str) -> dict:
    total = 0
    try:
      total += 1
    except ValueError as exc:            # ExceptHandler binds `exc`
      return {"success": False, "why": str(exc)}

    def inner():
      nonlocal total                     # Nonlocal binds `total`
      total += 1

    inner()
    match a:
      case {"kind": _, **rest}:          # MatchMapping binds `rest`
        found = rest
      case [*items]:                     # MatchStar binds `items`
        found = items
      case other:                        # MatchAs binds `other`
        found = other
    return {"success": True, "found": found, "total": total}

  assert T.unresolved_globals(T._REGISTRY["binding_tool"]) == []


def test_a_pep695_type_parameter_is_bound_not_reported(registry):
  ns: dict = {}
  src = ("def generic_tool[T](a: T) -> dict:\n"
         "  value: T = a\n"
         "  return {'success': True, 'value': value}\n")
  exec(compile(src, "<generic>", "exec"), ns)  # noqa: S102 — 3.12+ syntax
  fn = ns["generic_tool"]
  fn.__globals__.setdefault("T", str)
  tree = ast.parse(src)
  assert "T" in T._bound_names(tree)


def test_a_local_variable_annotation_is_never_reported(registry):
  @T.tool(flow="f")
  def annotated_local(a: str) -> dict:
    value: _COV_LOCAL_MODEL = a
    return {"success": True, "value": value}

  assert T.unresolved_globals(T._REGISTRY["annotated_local"]) == []


# Module-level names the tools above deliberately close over / annotate against.
from os import sep as _COV_IMPORTED_SEP  # noqa: E402

_COV_SENTINEL = object()
_COV_LOCAL_MODEL = str


class ChainNode(BaseModel):
  """Self-referential, so `_referenced_models` has a cycle to break."""

  label: str
  next_node: Optional["ChainNode"] = None


class TreeNode(BaseModel):
  label: str


class TreeReply(BaseModel):
  root: TreeNode
  also: TreeNode


ChainNode.model_rebuild()


# --- helper inlining: the cases that raise in the sandbox, not at build ------
_HI_LEAF = "leaf"


def _hi_mid():
  """Reads a constant, so the constant must be emitted above THIS."""
  return _HI_LEAF


def _hi_ping(n):
  """Mutually recursive with `_hi_pong` — a cycle the walk must survive."""
  return n if n <= 0 else _hi_pong(n - 1)


def _hi_pong(n):
  return _hi_ping(n - 1)


def test_helper_inlining_is_transitive_and_ordered(registry):
  """A helper's own dependencies come with it, defined first.

  Emitting `_hi_mid` without `_hi_LEAF` is source that raises `NameError` on the first
  call and nowhere else, since nothing imports the emitted file.
  """
  @T.tool(flow="f")
  def transitive_tool(a: str) -> dict:
    return {"success": True, "v": _hi_mid()}

  src = T.render_tool(T._REGISTRY["transitive_tool"])
  assert src.index('_HI_LEAF = "leaf"') < src.index("def _hi_mid()")
  assert src.index("def _hi_mid()") < src.index("def transitive_tool")
  assert T.unresolved_globals(T._REGISTRY["transitive_tool"]) == []


def test_helper_inlining_survives_a_cycle(registry):
  """Mutual recursion must not hang or emit a helper twice."""
  @T.tool(flow="f")
  def cyclic_tool(a: str) -> dict:
    return {"success": True, "v": _hi_ping(3)}

  src = T.render_tool(T._REGISTRY["cyclic_tool"])
  assert src.count("def _hi_ping(") == 1
  assert src.count("def _hi_pong(") == 1


def test_a_helper_used_only_inside_a_nested_function_is_still_carried(registry):
  """A name read from an inner function is exactly as undefined as a direct read."""
  @T.tool(flow="f")
  def nested_tool(a: str) -> dict:
    def inner():
      return _HI_LEAF
    return {"success": True, "v": inner()}

  assert '_HI_LEAF = "leaf"' in T.render_tool(T._REGISTRY["nested_tool"])


# --- a CLASS helper: callable, but no __code__ -------------------------------
_HI_CLS_CONST = "from-a-class"


class _HiHelperClass:
  """A module-level class helper, whose METHODS carry the references."""

  def plain(self):
    return _HI_CLS_CONST

  @classmethod
  def as_class(cls):
    return _HI_CLS_CONST

  @staticmethod
  def as_static():
    return _HI_CLS_CONST

  @property
  def as_property(self):
    return _HI_CLS_CONST


def test_a_class_helper_is_carried_with_its_own_dependencies(registry):
  """A class is callable but has no `__code__`.

  Reading one off it raises `AttributeError` and takes the whole build down, and the
  naive fix -- skipping classes -- silently drops the constants their methods read,
  which is the same sandbox `NameError` this feature exists to remove. Every descriptor
  shape is covered because each hides the function somewhere different:
  `classmethod`/`staticmethod` behind `__func__`, `property` behind `fget`.
  """
  @T.tool(flow="f")
  def class_tool(a: str) -> dict:
    return {"success": True, "v": _HiHelperClass().plain()}

  src = T.render_tool(T._REGISTRY["class_tool"])
  assert "class _HiHelperClass:" in src
  assert '_HI_CLS_CONST = "from-a-class"' in src
  assert src.index("_HI_CLS_CONST =") < src.index("class _HiHelperClass:")
  assert T.unresolved_globals(T._REGISTRY["class_tool"]) == []


def test_every_descriptor_shape_on_a_class_is_searched_for_references(registry):
  """`_code_objects` must find the function inside each wrapper, not just plain defs."""
  names = T._global_names(_HiHelperClass)  # noqa: SLF001
  assert "_HI_CLS_CONST" in names
  # four methods, and the property contributes its getter
  assert len(T._code_objects(_HiHelperClass)) == 4  # noqa: SLF001
