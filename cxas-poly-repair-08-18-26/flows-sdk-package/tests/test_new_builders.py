"""New public slot/task/control builders (dsl.py).

Covers task() terminal disposition, intent_slot/passive_slot, setter_group, user_slot
attachments, repeated, readback, and cancel/escalate/no_input — asserting the plain-dict
shape each returns (the exact wire shape the validator + engine consume).

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_new_builders.py
"""

from __future__ import annotations

import pytest

from flows import (
    cancel,
    escalate,
    intent_slot,
    no_input,
    passive_slot,
    readback,
    repeated,
    setter_group,
    task,
    user_slot,
)


# --- task() terminal disposition --------------------------------------------
def test_task_transfer_to_becomes_on_complete():
  t = task("n", "tool", ["a"], "r", transfer_to="Parent_Agent")
  assert t["on_complete"] == {"transfer_to": "Parent_Agent"}


def test_task_transfer_to_merges_into_explicit_on_complete():
  t = task("n", "tool", ["a"], "r", transfer_to="P",
           on_complete={"clear_slots": ["a"]})
  assert t["on_complete"] == {"clear_slots": ["a"], "transfer_to": "P"}


def test_task_on_complete_passthrough():
  t = task("n", "tool", ["a"], "r", on_complete={"auto_resume_deferred": True})
  assert t["on_complete"] == {"auto_resume_deferred": True}


def test_task_readback_inputs_passthrough():
  assert task("n", "tool", ["a"], "r", readback_inputs=False)["readback_inputs"] is False
  assert task("n", "tool", ["a"], "r", readback_inputs=True)["readback_inputs"] is True
  # Never auto-derived: absent unless the author passes it.
  assert "readback_inputs" not in task("n", "tool", ["a"], "r")


def test_task_transfer_to_collision_raises():
  with pytest.raises(ValueError, match="not both"):
    task("n", "tool", ["a"], "r", transfer_to="P",
         on_complete={"transfer_to": "Q"})


# --- intent_slot ------------------------------------------------------------
def test_intent_slot_shape():
  s = intent_slot("journey_intent", {"pay": ["pay bill"], "refund": ["refund me"]})
  assert s["kind"] == "intent"
  assert s["source"] == "user"
  # option_cues verbatim (order + values preserved).
  assert s["option_cues"] == {"pay": ["pay bill"], "refund": ["refund me"]}
  # enum rule, pipe-joined values, discriminator key is `kind` (not `type`).
  assert s["validation_rules"] == [{"kind": "enum", "detail": "pay|refund"}]
  # setter defaults set_<name>.
  assert s["setter"] == "set_journey_intent"
  # asked (not passive) -> carries an ask.
  assert s["ask"] == "Which would you like?"


def test_intent_slot_passive_omits_ask():
  s = intent_slot("intent", {"a": ["a"], "b": ["b"]}, passive=True)
  assert s["passive"] is True
  assert "ask" not in s


def test_intent_slot_empty_options_raises():
  with pytest.raises(ValueError, match="non-empty"):
    intent_slot("x", {})


def test_intent_slot_empty_cue_list_raises():
  with pytest.raises(ValueError, match="empty cue list"):
    intent_slot("x", {"a": []})


# --- passive_slot -----------------------------------------------------------
def test_passive_slot_shape():
  s = passive_slot("intent_type")
  assert s["passive"] is True
  assert s["source"] == "user"
  assert s["setter"] == "set_intent_type"
  assert "ask" not in s


def test_passive_slot_intent_kind_gets_enum_rule():
  s = passive_slot("it", option_cues={"a": ["a"], "b": ["b"]}, kind="intent")
  assert s["validation_rules"] == [{"kind": "enum", "detail": "a|b"}]


# --- setter_group -----------------------------------------------------------
def test_setter_group_repoints_and_preserves_inputs():
  slots = [user_slot("a", "a?"), user_slot("b", "b?")]
  grp = setter_group("pay", slots)
  assert [s["setter"] for s in grp] == ["set_pay_inputs", "set_pay_inputs"]
  assert [s["setter_field"] for s in grp] == ["a", "b"]
  # inputs are not mutated (copies returned).
  assert slots[0]["setter"] == "set_a"
  assert "setter_field" not in slots[0]


# --- user_slot attachments --------------------------------------------------
def test_user_slot_validation_rules_attaches():
  s = user_slot("phone", "phone?",
                validation_rules=[{"kind": "length_digits", "detail": "10"}])
  assert s["validation_rules"] == [{"kind": "length_digits", "detail": "10"}]


def test_user_slot_luhn_rule_attaches():
  s = user_slot("card_number", "card number?",
                validation_rules=[{"kind": "length_digits", "detail": "16"},
                                  {"kind": "luhn"}])
  assert s["validation_rules"] == [{"kind": "length_digits", "detail": "16"},
                                   {"kind": "luhn"}]


def test_user_slot_validation_replaces_default_ladder():
  mined = {"max_retries": 2, "errors": {"bad": "nope"},
           "on_exhaust": {"say": "bye", "then": "escalate"}}
  s = user_slot("zip", "zip?", validation=mined)
  # `validation=` REPLACES the built reprompt ladder verbatim.
  assert s["validation"] == mined
  assert "reprompts" not in s["validation"]


def test_user_slot_default_validation_ladder():
  s = user_slot("x", "x?")
  assert set(s["validation"]) == {"max_retries", "reprompts", "on_exhaust"}


def test_user_slot_sensitive_and_repeated_attach():
  s = user_slot("ssn", "ssn?", sensitive=True)
  assert s["sensitive"] is True
  rep = {"until": {"max_count": 3}}
  s2 = user_slot("items", "item?", repeated=rep)
  assert s2["repeated"] == rep


# --- repeated ---------------------------------------------------------------
def test_repeated_shape():
  b = repeated(until_max=5, done_setter="set_done", min_count=1,
               over="lines", each={"line": "value"})
  assert b == {
      "until": {"max_count": 5, "done_setter": "set_done"},
      "min_count": 1,
      "over": "lines",
      "each": {"line": "value"},
  }


def test_repeated_unbounded_raises():
  with pytest.raises(ValueError, match="unbounded loop"):
    repeated(min_count=1)


# --- readback ---------------------------------------------------------------
def test_readback_shape():
  # readback_fmt keys on "type" (framework validator + formatter) with per-type fields.
  assert readback("join", each="{items}", sep=", ") == {
      "type": "join", "each": "{items}", "sep": ", "}
  assert readback("count", one="item", other="items") == {
      "type": "count", "one": "item", "other": "items"}
  assert readback("date") == {"type": "date"}
  import pytest
  with pytest.raises(ValueError):  # missing required 'each'
    readback("join")


# --- cancel / escalate / no_input -------------------------------------------
def test_cancel_defaults():
  b = cancel(say="Okay, cancelling.")
  assert b["outcome"] == "cancelled"
  assert b["say"] == "Okay, cancelling."
  assert b["requires_readback"] is False
  assert "transfer_to" not in b


def test_cancel_transfer_to_optional():
  assert cancel(say="x", transfer_to="Host")["transfer_to"] == "Host"


def test_escalate_defaults():
  b = escalate(say="Let me get someone.")
  assert b["outcome"] == "escalated"
  assert "transfer_to" not in b


def test_no_input_shape():
  b = no_input(reprompts=["Still there?"], on_exhaust={"then": "escalate"})
  assert b == {"reprompts": ["Still there?"], "on_exhaust": {"then": "escalate"}}
