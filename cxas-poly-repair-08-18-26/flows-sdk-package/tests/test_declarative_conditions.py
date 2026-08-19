"""Declarative (dict) conditions: every `condition=` site accepts them, verbatim + validated.

A condition has two interchangeable forms — the `lambda f: ...` source string `eq/ne/has/unset`
build, and the declarative dict the engine evaluates natively. A GENERATOR (a compiler lowering
an IR, a config editor) holds the gate as structure; the string form is a lossy one-way encoding
of it. So the builders take either.

What this module proves:

* the dict reaches the emitted Config UNCHANGED, from every builder that takes `condition=`,
  for every leaf operator and combinator in the grammar;
* the grammar the DSL accepts is EXACTLY the one the blessed framework validator + engine
  implement — asserted against their own tables, not a copy of them, so the two cannot drift;
* a malformed gate is rejected at AUTHORING time naming the offending fragment (a bad gate that
  reaches the Config is a gate that silently never opens, and the op never fires);
* the lambda-string form is byte-for-byte untouched.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows import (  # noqa: E402
    Flow,
    announce,
    component,
    eq,
    gate,
    intent_slot,
    journey,
    load_flow,
    passive_slot,
    result_slot,
    task,
    user_slot,
)
from flows.authoring import build as _build  # noqa: E402
from flows.authoring import render as _render  # noqa: E402
from flows.authoring.dsl import App, Operation, _and_conditions  # noqa: E402
from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)


# Every leaf operator + combinator shape in the grammar, once each.
GRAMMAR_FORMS = [
    {"slot": "status", "eq": "open"},
    {"slot": "status", "neq": "closed"},
    {"slot": "state", "in": ["CA", "NY"]},
    {"slot": "state", "not_in": ["HI", "AK"]},
    {"slot": "account", "filled": True},
    {"slot": "account", "filled": False},
    {"slot": "balance", "gt": 0},
    {"slot": "balance", "gte": 100, "default": 0},
    {"slot": "attempts", "lt": 3},
    {"slot": "attempts", "lte": 2},
    {"slot": "state", "eq": "CA", "upper": True},
    {"all": [{"slot": "verified", "eq": "yes"}, {"slot": "account", "filled": True}]},
    {"any": [{"slot": "tier", "eq": "gold"}, {"slot": "tier", "eq": "platinum"}]},
    {"not": {"slot": "blocked", "filled": True}},
    {"all": [{"slot": "verified", "eq": "yes"},
             {"any": [{"slot": "tier", "eq": "gold"},
                      {"not": {"slot": "balance", "gt": 0}}]}]},
]


# --- the dict reaches the Config unchanged, from every site -----------------

@pytest.mark.parametrize("spec", GRAMMAR_FORMS, ids=range(len(GRAMMAR_FORMS)))
def test_every_grammar_form_round_trips_to_the_config(spec):
  builders = {
      "user_slot": user_slot("x", "X?", condition=spec),
      "intent_slot": intent_slot("i", {"a": ["a"]}, condition=spec),
      "passive_slot": passive_slot("p", condition=spec),
      "announce": announce("a", ["hi"], condition=spec),
      "task": task("t", "tool", ["x"], "r", condition=spec),
      "component": component("c", "child", condition=spec),
  }
  for site, built in builders.items():
    assert built["condition"] == spec, site
    assert built["condition"] is spec, f"{site}: the gate must pass through, not be rebuilt"


def test_dict_condition_survives_flow_to_config():
  spec = {"all": [{"slot": "verified", "eq": "yes"}, {"slot": "account", "filled": True}]}
  f = Flow("billing", root_agent="A")
  f.add(user_slot("account", "Account?", condition=spec))
  f.task(task("pay", "pay_bill", ["account"], "conf", condition=spec))
  cfg = f.to_config()
  assert cfg["slots"][0]["condition"] == spec
  assert cfg["tasks"][0]["condition"] == spec


def test_dict_condition_passes_the_framework_validator():
  """End of the pipeline: the emitted Config validates clean, gate included."""
  f = Flow("billing", root_agent="Billing_Agent", bootstrap={"welcome_slot": "welcome"})
  f.add(
      announce("welcome", ["Sure."], shared=True),
      user_slot("verified", "Verified?"),
      user_slot("account", "Account?",
                condition={"slot": "verified", "eq": "yes"}),
      result_slot("conf", "pay"),
      announce("done", ["All set, {conf}."], requires=["conf"], end=True),
  )
  f.task(task("pay", "pay_bill", ["account"], "conf", terminal=True, then_say="Paid.",
              condition={"all": [{"slot": "verified", "eq": "yes"},
                                 {"slot": "account", "filled": True}]}))
  errors, _warnings = _build.validate_app(App(root_flow=f, app_display_name="billing"))
  assert errors == [], errors


def test_dict_condition_survives_the_source_round_trip():
  """The rendered authoring source must rebuild the identical Config — gates included."""
  f = Flow("billing", root_agent="Billing_Agent")
  f.add(user_slot("account", "Account?", condition={"slot": "verified", "filled": True}))
  f.task(task("pay", "pay_bill", ["account"], "conf",
              condition={"any": [{"slot": "tier", "eq": "gold"},
                                 {"slot": "tier", "eq": "platinum"}]}))
  cfg = f.to_config()
  src = _render.render_config_source(cfg, config_id="billing", root_agent="Billing_Agent")
  ns: dict = {}
  exec(compile(src, "<rendered>", "exec"), ns)  # noqa: S102 — the round-trip IS the contract
  assert ns["flow"].to_config() == cfg


# --- the grammar is the ENGINE's grammar, not a lookalike -------------------

def test_accepted_grammar_matches_the_blessed_validator_tables():
  """Asserted against the framework's OWN tables: the two definitions cannot drift apart."""
  from flows.authoring import dsl

  v = fb.load_validator()
  assert dsl._CONDITION_LEAF_KEYS == v._VALID_LEAF_KEYS
  assert dsl._CONDITION_COMPARISON_OPS == v._COMPARISON_OPS
  assert dsl._CONDITION_VALUE_OPS == v._VALUE_OPS
  assert dsl._CONDITION_OPS == v._ALL_OPS


@pytest.mark.parametrize("spec", GRAMMAR_FORMS, ids=range(len(GRAMMAR_FORMS)))
def test_every_accepted_form_is_evaluable_by_the_engine(spec):
  """Non-vacuity: whatever the DSL lets through, the engine can actually evaluate."""
  engine = fb.load_engine()
  filled = {"status": "open", "state": "ca", "account": "123",
            "balance": 50, "attempts": 1, "verified": "yes", "tier": "gold"}
  assert isinstance(engine._eval_condition(spec, filled), bool)


# --- malformed gates are refused at AUTHORING time --------------------------

# (a bad gate, the substring the message must name)
MALFORMED = [
    ({"slot": "x", "equals": "y"}, "unknown condition key(s) ['equals']"),
    ({"eq": "y"}, "leaf condition has no 'slot' key"),
    ({"slot": "x"}, "leaf condition has no operator"),
    ({"slot": "x", "eq": "a", "neq": "b"}, "multiple operators"),
    ({"slot": 7, "eq": "a"}, "'slot' must be a string"),
    ({"all": {"slot": "x", "eq": "y"}}, "'all' must be a list"),
    ({"any": {"slot": "x", "eq": "y"}}, "'any' must be a list"),
    ({"all": [{"slot": "x", "eq": "y"}]}, "'all' needs at least 2 sub-conditions"),
    ({"any": []}, "'any' needs at least 2 sub-conditions"),
    ({"all": [{"slot": "x", "eq": 1}, {"slot": "y", "eq": 2}], "slot": "z"},
     "'all' combinator takes no other keys"),
    ({"not": {"slot": "x", "eq": 1}, "eq": 2}, "'not' combinator takes no other keys"),
    ({"slot": "x", "in": "CA"}, "'in' must be a list"),
    ({"slot": "x", "filled": "yes"}, "'filled' must be a bool"),
    ({"slot": "x", "gt": "many"}, "'gt' must be an int"),
    ({"slot": "x", "gte": 1, "default": "none"}, "'default' must be an int"),
    # The engine truncates the left-hand side (`int(filled.get(slot, default))`), so a
    # fractional bound or default is decided against a value that can never carry one.
    ({"slot": "x", "gt": 12.8}, "'gt' must be an int"),
    ({"slot": "x", "gte": 1, "default": 12.8}, "'default' must be an int"),
    # `default` is read ONLY on the comparison path; every other operator falls through
    # to `filled.get(slot, "")` and ignores it.
    ({"slot": "x", "neq": "v", "default": "v"}, "'default' only applies to a numeric"),
    ({"slot": "x", "eq": "v", "default": "v"}, "'default' only applies to a numeric"),
    ({"slot": "x", "filled": True, "default": 0}, "'default' only applies to a numeric"),
    ({"slot": "x", "eq": "a", "upper": "yes"}, "'upper' must be a bool"),
    ({"slot": "x", "filled": True, "upper": True}, "'upper' does not apply"),
    ({}, "leaf condition has no 'slot' key"),
]


@pytest.mark.parametrize("spec,needle", MALFORMED, ids=[n for _, n in MALFORMED])
def test_malformed_gate_is_rejected_naming_the_fragment(spec, needle):
  with pytest.raises(ValueError) as ei:
    user_slot("x", "X?", condition=spec)
  msg = str(ei.value)
  assert needle in msg, msg
  assert repr(spec) in msg, f"the message must quote the offending fragment: {msg}"


def test_rejection_path_points_at_the_nested_fragment():
  """A gate is a tree; the message must locate the bad node inside it, not just say 'bad gate'."""
  bad = {"slot": "tier", "equals": "gold"}
  with pytest.raises(ValueError) as ei:
    task("t", "tool", ["x"], "r",
         condition={"all": [{"slot": "verified", "eq": "yes"},
                            {"any": [bad, {"slot": "tier", "eq": "platinum"}]}]})
  msg = str(ei.value)
  assert "<root>.all[1].any[0]" in msg, msg
  assert repr(bad) in msg, msg


@pytest.mark.parametrize("site", [
    lambda c: user_slot("x", "X?", condition=c),
    lambda c: intent_slot("i", {"a": ["a"]}, condition=c),
    lambda c: passive_slot("p", condition=c),
    lambda c: announce("a", ["hi"], condition=c),
    lambda c: task("t", "tool", ["x"], "r", condition=c),
    lambda c: component("c", "child", condition=c),
])
def test_every_site_rejects_a_malformed_gate(site):
  """The check is on the shared normalizer, so no builder can be the one that lets it through."""
  with pytest.raises(ValueError):
    site({"slot": "x", "equals": "y"})


def test_empty_dict_is_an_error_not_a_silent_drop():
  """`{}` is falsy: the pre-existing truthiness test would have dropped the gate on the floor."""
  with pytest.raises(ValueError):
    task("t", "tool", ["x"], "r", condition={})


def test_a_non_condition_type_is_rejected():
  with pytest.raises(TypeError) as ei:
    user_slot("x", "X?", condition=["slot", "x"])
  assert "declarative dict" in str(ei.value)


# --- the lambda-string form is untouched ------------------------------------

def test_helper_lambdas_are_unchanged():
  assert eq("x", "v") == "lambda f: f.get('x') == 'v'"
  assert user_slot("s", "S?", condition=eq("x", "v"))["condition"] == eq("x", "v")
  assert task("t", "tool", ["s"], "r", condition="lambda f: bool(f.get('s'))")[
      "condition"] == "lambda f: bool(f.get('s'))"


def test_absent_and_empty_conditions_still_omit_the_key():
  assert "condition" not in user_slot("s", "S?")
  assert "condition" not in user_slot("s", "S?", condition=None)
  assert "condition" not in user_slot("s", "S?", condition="")


# --- gate(): the declarative escape hatch -----------------------------------

def test_gate_returns_the_spec_unchanged():
  spec = {"all": [{"slot": "a", "eq": 1}, {"slot": "b", "filled": True}]}
  assert gate(spec) is spec


def test_gate_rejects_eagerly_at_the_point_of_construction():
  with pytest.raises(ValueError) as ei:
    gate({"slot": "a", "equals": 1})
  assert "unknown condition key(s) ['equals']" in str(ei.value)


# --- composition: journeys, and the two forms not mixing --------------------

def test_and_conditions_composes_dicts_into_a_flat_all():
  a = {"slot": "x", "eq": 1}
  b = {"slot": "y", "eq": 2}
  c = {"slot": "z", "filled": True}
  assert _and_conditions(a, b) == {"all": [a, b]}
  assert _and_conditions({"all": [a, b]}, c) == {"all": [a, b, c]}


def test_and_conditions_refuses_to_mix_the_two_forms():
  with pytest.raises(ValueError) as ei:
    _and_conditions({"slot": "x", "eq": 1}, eq("y", 2))
  assert "do not nest" in str(ei.value)


def test_and_conditions_checks_an_operand_that_no_builder_has_seen_yet():
  """`_and_conditions` runs BEFORE the gate reaches a builder — a `VerdictBranch`'s
  condition is only validated when its announce is built — so it cannot assume a
  well-formed dict. Taking a bad one apart used to raise a bare KeyError/TypeError
  from inside the combinator, naming neither the fragment nor which side it was."""
  with pytest.raises(ValueError) as ei:
    _and_conditions({"slot": "x", "equals": 1}, {"slot": "y", "eq": 2})
  assert "<and:left>" in str(ei.value) and "['equals']" in str(ei.value)

  with pytest.raises(ValueError) as ei:
    _and_conditions({"slot": "x", "eq": 1}, {"slot": "y", "equals": 2})
  assert "<and:right>" in str(ei.value)


def test_and_conditions_rejects_an_operand_that_is_not_a_condition_at_all():
  with pytest.raises(TypeError) as ei:
    _and_conditions(7, eq("y", 2))
  assert "<and:left>" in str(ei.value) and "int" in str(ei.value)


def test_a_lambda_string_without_a_colon_is_named_not_an_indexerror():
  """The commonest near-miss: the expression, not the whole lambda. Splitting it on
  ':' to AND it raised IndexError; the message must say what the format is."""
  with pytest.raises(ValueError) as ei:
    _and_conditions("f.get('x') == 1", eq("y", 2))
  assert "lambda f: ..." in str(ei.value)
  # ...and it never gets that far, because every `condition=` site checks the form.
  with pytest.raises(ValueError) as ei:
    user_slot("x", "X?", condition="f.get('x') == 1")
  assert "lambda f: ..." in str(ei.value)


def test_journey_gates_a_declarative_op_slot_in_its_own_form():
  """A journey ANDs its intent gate onto each op slot; a dict-gated slot gets the dict twin."""
  own = {"slot": "enrolled", "eq": "yes"}
  ops = [
      Operation(value="pay", cues=["pay"],
                slots=[user_slot("amount", "How much?", condition=own)],
                tasks=[task("Pay", "pay", ["amount"], "conf", terminal=True)]),
      Operation(value="balance", cues=["balance"],
                slots=[user_slot("acct", "Account?")],
                tasks=[task("Bal", "balance", ["acct"], "bal", terminal=True)]),
  ]
  cfg = journey("billing", spine=[], operations=ops, parent="Host").to_config()
  amount = next(s for s in cfg["slots"] if s["name"] == "amount")
  assert amount["condition"] == {"all": [own, {"slot": "journey_intent", "eq": "pay"}]}
  # the string-gated sibling keeps the string form
  acct = next(s for s in cfg["slots"] if s["name"] == "acct")
  assert acct["condition"] == eq("journey_intent", "balance")


def test_journey_oracle_reads_a_declarative_terminal_gate():
  """`_check_journey_gates` must not report a dict-gated terminal as ungated."""
  cfg = {
      "slots": [intent_slot("journey_intent", {"pay": ["pay"], "balance": ["balance"]})],
      "tasks": [
          task("Pay", "pay", ["a"], "conf", terminal=True,
               condition={"slot": "journey_intent", "eq": "pay"}),
          task("Bal", "balance", ["a"], "bal", terminal=True,
               condition={"all": [{"slot": "journey_intent", "eq": "balance"},
                                  {"slot": "a", "filled": True}]}),
      ],
  }
  assert _build._check_journey_gates(cfg) == []


# --- YAML interop -----------------------------------------------------------

def test_yaml_declarative_gate_loads_and_is_validated(tmp_path):
  """YAML slot/task dicts are used verbatim (no builder runs), so the loader must check them."""
  good = tmp_path / "ok.yaml"
  good.write_text(
      "config_id: billing\n"
      "root_agent: A\n"
      "slots:\n"
      "  - name: account\n"
      "    source: user\n"
      "    setter: set_account\n"
      "    ask: Account?\n"
      "    condition:\n"
      "      slot: verified\n"
      "      eq: 'yes'\n"
  )
  cfg = load_flow(str(good)).to_config()
  assert cfg["slots"][0]["condition"] == {"slot": "verified", "eq": "yes"}

  bad = tmp_path / "bad.yaml"
  bad.write_text(
      "config_id: billing\n"
      "slots:\n"
      "  - name: account\n"
      "    source: user\n"
      "    condition:\n"
      "      slot: verified\n"
      "      equals: 'yes'\n"
  )
  with pytest.raises(ValueError) as ei:
    load_flow(str(bad))
  assert "unknown condition key(s) ['equals']" in str(ei.value)
