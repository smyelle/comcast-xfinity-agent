"""A variable map may SKIP a question. It may never invent an answer.

Every rule below exists to keep that true. The dangerous failure is not a map that
declines to match — the flow then simply asks, which is what it would have done
anyway. It is a map that matches on something that is not an answer: an unseeded CES
variable arriving as its declared default, an upstream sentinel meaning "the backend
has not replied yet", an object where a scalar was meant. Each of those fills a slot
with a value no downstream branch matches, and a flow that has been told the question
is answered cannot ask it again.

The ordering tests defend the other half. Ingress is registered AHEAD of any author
`before_agent` hook, because a hook whose job is to act on what the session arrived
with (sweep a backend off the account number, say) cannot do it on turn 0 if the
account only reaches the slot machine afterwards. That registration order IS the
feature for such an app; a passing unit test that ignores it proves nothing.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_variable_map.py
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

import flows
from flows.authoring import variable_maps as _vm

# ---------------------------------------------------------------------------
# The generated ingress callback, driven directly against a fake CES context. This is
# the real emitted source, compiled — not a reimplementation of it.
# ---------------------------------------------------------------------------

_NS: dict = {}
exec(compile(_vm.ingress_source(), "ingress.py", "exec"), _NS)  # noqa: S102
_INGRESS = _NS["before_agent_callback"]


class _Ctx:
  """The one attribute the callback touches."""

  def __init__(self, state):
    self.state = state


def _binding(slot, alts, *, shape="scalar", readback=False, reject=()):
  return {"slot": slot, "shape": shape, "readback": readback, "reject": list(reject),
          "alts": [{"var": v, "path": p} for v, p in alts]}


_ACCOUNT = _binding("account_number", [("accountNumber", []), ("account_id", [])],
                    reject=["PENDING_BACKEND_RESULT"])
_PARCEL = _binding("tracking_number", [("parcel", ["tracking_id"])])

_TABLE = {"track": [
    {"name": "by_parcel", "bindings": [_PARCEL, _ACCOUNT]},
    {"name": "by_account", "bindings": [_ACCOUNT]},
]}


def _seed(variables, table=None, sm=None, config_id="track"):
  """Run one ingress pass; return the resulting slot machine."""
  state = {"variable_maps_by_config": json.dumps(table or _TABLE),
           "default_config_id": config_id}
  state.update(variables)
  if sm is not None:
    state["sm"] = sm
  _INGRESS(_Ctx(state))
  return state.get("sm", {})


# ---------------------------------------------------------------------------
# Choosing a shape
# ---------------------------------------------------------------------------


def test_the_first_declared_map_that_fits_is_the_one_used():
  """Both shapes resolve; the earlier declaration wins and fills its extra slot."""
  sm = _seed({"parcel": {"tracking_id": "AC-40219"}, "accountNumber": "8069"})
  assert sm["_variable_map"]["name"] == "by_parcel", sm["_variable_map"]
  assert sm["filled"] == {"tracking_number": "AC-40219", "account_number": "8069"}


def test_a_narrower_map_is_used_when_the_wider_one_cannot_resolve():
  """No parcel, so `by_parcel` fails entirely and `by_account` fills the account."""
  sm = _seed({"accountNumber": "8069"})
  assert sm["_variable_map"]["name"] == "by_account"
  assert sm["filled"] == {"account_number": "8069"}


def test_a_map_is_all_or_nothing():
  """A map missing ONE binding fills none of its slots — half a shape is not a shape."""
  sm = _seed({"parcel": {"tracking_id": "AC-40219"}})
  # by_parcel also needs an account, which is absent, so by_account is tried and it
  # cannot resolve either. Nothing at all is filled, tracking number included.
  assert sm.get("filled", {}) == {}


def test_no_match_is_a_clean_no_op():
  """Nothing filled, nothing raised, and the attempt is NOT spent."""
  sm = _seed({})
  assert sm.get("filled", {}) == {}
  assert sm.get("_variable_map", {}) == {}


# ---------------------------------------------------------------------------
# What counts as an answer
# ---------------------------------------------------------------------------


def test_synonyms_fall_through_to_the_next_spelling():
  """One fact under two names: the second is used when the first is unset."""
  sm = _seed({"account_id": "8069"})
  assert sm["filled"] == {"account_number": "8069"}


def test_the_first_listed_spelling_wins_when_both_arrive():
  sm = _seed({"accountNumber": "AAA", "account_id": "BBB"})
  assert sm["filled"] == {"account_number": "AAA"}


@pytest.mark.parametrize("value", ["", "   ", {}, []])
def test_an_unseeded_variable_is_not_an_answer(value):
  """CES gives every declared variable a default, so empty is indistinguishable from
  absent — and treating it as a value would make every map match every session."""
  assert _seed({"accountNumber": value}).get("filled", {}) == {}


def test_a_rejected_sentinel_is_not_an_answer():
  """An upstream's "no reply yet" marker is present, non-empty, and still not a value."""
  assert _seed({"accountNumber": "PENDING_BACKEND_RESULT"}).get("filled", {}) == {}


def test_a_rejected_value_falls_through_to_the_next_spelling():
  sm = _seed({"accountNumber": "PENDING_BACKEND_RESULT", "account_id": "8069"})
  assert sm["filled"] == {"account_number": "8069"}


def test_a_dotted_path_reaches_into_an_object_variable():
  sm = _seed({"parcel": {"tracking_id": "AC-40219"}, "accountNumber": "8069"})
  assert sm["filled"]["tracking_number"] == "AC-40219"


def test_an_unresolvable_path_does_not_match():
  """The object arrived but not the field, so the shape does not fit."""
  sm = _seed({"parcel": {"other": 1}, "accountNumber": "8069"})
  assert sm["_variable_map"]["name"] == "by_account"
  assert "tracking_number" not in sm["filled"]


def test_an_object_does_not_satisfy_a_scalar_binding():
  assert _seed({"accountNumber": {"nested": 1}}).get("filled", {}) == {}


def test_a_list_slot_takes_a_list_and_refuses_a_scalar():
  table = {"track": [{"name": "m", "bindings": [
      _binding("items", [("items", [])], shape="list")]}]}
  assert _seed({"items": "one"}, table=table).get("filled", {}) == {}
  assert _seed({"items": ["a", "b"]}, table=table)["filled"] == {"items": ["a", "b"]}


# ---------------------------------------------------------------------------
# Writing, and not re-writing
# ---------------------------------------------------------------------------


def test_an_already_filled_slot_is_never_overwritten():
  """A value the caller gave outranks one the session arrived with."""
  sm = _seed({"accountNumber": "NEW"}, sm={"filled": {"account_number": "OLD"}})
  assert sm["filled"] == {"account_number": "OLD"}


def test_writing_nothing_does_not_spend_the_attempt():
  """A map can resolve and write nothing. Marking that done would strand a larger
  shape that has not arrived yet, so the marker is only set on a real write."""
  sm = _seed({"accountNumber": "NEW"}, sm={"filled": {"account_number": "OLD"}})
  assert sm.get("_variable_map", {}) == {}


# ---------------------------------------------------------------------------
# Routed apps: the config is not resolved yet when ingress runs
# ---------------------------------------------------------------------------


def _emitted_table(app, tmp_path):
  """The `variable_maps_by_config` default out of a REAL build, as a dict."""
  out = str(tmp_path / "app")
  flows.build_app(app, out)
  with open(os.path.join(out, "app.json"), encoding="utf-8") as fh:
    for var in (json.load(fh).get("variableDeclarations") or []):
      if var.get("name") == "variable_maps_by_config":
        return json.loads(var["schema"]["default"])
  return {}


def _account_map():
  return flows.variable_map("by_account", {"account_number": ["accountNumber"]})


def _track_flow():
  track = flows.Flow("track", root_agent="Agent")
  track.add(flows.user_slot("account_number", ask="What is the account number?"))
  return track


_ACCOUNT_VAR = [{"name": "accountNumber", "schema": {"type": "STRING"}}]


def test_a_single_agent_router_inherits_the_shapes_its_flows_declare(tmp_path):
  """Ingress runs BEFORE routing, so the only id it can see is the ROUTER's. A router
  holds no user slots, so projection keeps nothing for it — and keyed strictly on that
  id, a routed app was never seeded on the turn the variables arrive. The maps only
  became reachable once `_active_config_id` named a real flow, a turn or more after
  routing, by which point that flow has already asked for the value the map exists to
  supply."""
  app = flows.App(
      root_flow=flows.router_flow("hub", ["track"], route_cues={"track": ["parcel"]},
                                  root_agent="Agent"),
      extra_flows=[_track_flow()], app_display_name="Routed",
      variables=_ACCOUNT_VAR, variable_maps=[_account_map()])
  table = _emitted_table(app, tmp_path)
  assert "hub" in table, f"the router has no entry, so ingress cannot fire: {list(table)}"
  assert [m["name"] for m in table["hub"]] == ["by_account"]
  assert table["hub"][0]["bindings"][0]["slot"] == "account_number"


def test_a_single_flow_app_is_unchanged_by_the_router_fallback(tmp_path):
  """The inheritance is for routers only, so nothing about the ordinary case moves."""
  app = flows.App(root_flow=_track_flow(), app_display_name="Single",
                  variables=_ACCOUNT_VAR, variable_maps=[_account_map()])
  assert list(_emitted_table(app, tmp_path)) == ["track"]


def test_a_router_that_declares_its_own_shapes_keeps_them(tmp_path):
  """Inheritance is a fallback. A router holding a slot of its own gets the projection
  it earned, and does not have its flows' wider shapes written over it."""
  hub = flows.router_flow("hub", ["track"], route_cues={"track": ["parcel"]},
                          root_agent="Agent")
  hub.add(flows.user_slot("account_number", ask="What is the account number?"))
  app = flows.App(root_flow=hub, extra_flows=[_track_flow()], app_display_name="Own",
                  variables=_ACCOUNT_VAR, variable_maps=[_account_map()])
  table = _emitted_table(app, tmp_path)
  assert "hub" in table and [m["name"] for m in table["hub"]] == ["by_account"]


def test_ingress_is_idempotent_across_turns():
  """Turn two must not re-assert a seed over whatever the conversation has since done."""
  sm = _seed({"accountNumber": "8069"})
  again = _seed({"accountNumber": "CHANGED"}, sm=sm)
  assert again["filled"] == {"account_number": "8069"}


def test_a_readback_slot_is_staged_not_filled():
  """Accepting a seeded value as CONFIRMED is worse than asking for it cold."""
  table = {"track": [{"name": "m", "bindings": [
      _binding("mobile", [("ani", [])], readback=True)]}]}
  sm = _seed({"ani": "5551234"}, table=table)
  assert sm.get("filled", {}) == {}
  assert sm["pending"] == {"mobile": "5551234"}


def test_ingress_does_not_initialize_the_slot_machine():
  """`_initialized` guards the real initializer, which runs in the NEXT callback."""
  assert "_initialized" not in _seed({"accountNumber": "8069"})


def test_ingress_does_not_set_the_preempt_flag():
  """`_event_prefilled_this_turn` suppresses the option-cue switch branch, so a seeded
  turn would silently swallow a caller changing the subject."""
  assert "_event_prefilled_this_turn" not in _seed({"accountNumber": "8069"})


def test_a_config_with_no_maps_is_untouched():
  assert _seed({"accountNumber": "8069"}, config_id="other").get("filled", {}) == {}


def test_a_missing_table_is_a_no_op():
  ctx = _Ctx({"accountNumber": "8069"})
  _INGRESS(ctx)
  assert ctx.state.get("sm") is None


# ---------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------


def test_synonyms_belong_in_one_binding_not_two():
  """Two bindings means BOTH must arrive, which is the opposite of a synonym."""
  with pytest.raises(ValueError, match="bound to both"):
    flows.variable_map("m", {"a": "acct", "b": "acct"})


@pytest.mark.parametrize("bad", ["", ".acct", "acct.", "a..b"])
def test_a_malformed_source_is_rejected(bad):
  with pytest.raises(ValueError):
    flows.variable_map("m", {"slot": bad})


def test_an_empty_map_is_rejected():
  """It would match every session vacuously and shadow everything after it."""
  with pytest.raises(ValueError, match="at least one binding"):
    flows.variable_map("m", {})


def test_a_bare_string_lowers_the_same_as_a_one_element_list():
  slots = [{"name": "s"}]
  one = _vm.project([flows.variable_map("m", {"s": "v"})], slots)
  many = _vm.project([flows.variable_map("m", {"s": ["v"]})], slots)
  assert one == many


def test_lowering_drops_bindings_for_slots_this_flow_does_not_hold():
  m = flows.variable_map("m", {"a": "va", "b": "vb"})
  lowered = _vm.project([m], [{"name": "a"}])
  assert [b["slot"] for b in lowered[0]["bindings"]] == ["a"]


def test_a_map_that_keeps_no_bindings_is_dropped_from_the_config():
  m = flows.variable_map("m", {"a": "va"})
  assert _vm.project([m], [{"name": "unrelated"}]) == []


def test_shadowing_is_judged_on_the_lowered_maps():
  """Lowering drops conjuncts, so a map that discriminates app-wide may not here."""
  wide = flows.variable_map("wide", {"a": "va", "b": "vb"})
  narrow = flows.variable_map("narrow", {"a": "va"})
  # Both keep only `a`, so whichever is declared second can never be chosen.
  lowered = _vm.project([narrow, wide], [{"name": "a"}])
  assert _vm.shadowed(lowered) == [("wide", "narrow")]
  # Declared the other way round, and with both slots present, both stay reachable.
  lowered = _vm.project([wide, narrow], [{"name": "a"}, {"name": "b"}])
  assert _vm.shadowed(lowered) == []


# ---------------------------------------------------------------------------
# Build: validation, emission, and the registration order that is the whole point
# ---------------------------------------------------------------------------


def _app(variable_maps, *, variables=None, slots=None, hooks=None):
  f = flows.Flow("track", root_agent="Track_Agent")
  f.add(*(slots or [
      flows.user_slot("account_number", ask="Account number?"),
      flows.user_slot("tracking_number", ask="Tracking number?"),
  ]))
  return flows.App(
      root_flow=f, app_display_name="vm-test",
      variables=variables if variables is not None else [
          {"name": "accountNumber", "schema": {"type": "STRING", "default": ""}},
          {"name": "account_id", "schema": {"type": "STRING", "default": ""}},
      ],
      variable_maps=variable_maps, hooks=hooks)


def test_a_source_the_app_does_not_declare_is_an_error():
  """CES only surfaces DECLARED variables, so this source could never resolve — and
  the binding still matches through its sibling, so nothing would look wrong."""
  app = _app([flows.variable_map("m", {"account_number": ["accountNumber", "typo"]})])
  errors, _ = flows.validate_app(app)
  assert any("'typo'" in e and "does not declare" in e for e in errors), errors


def test_a_map_no_flow_can_use_is_an_error():
  app = _app([flows.variable_map("m", {"nonexistent_slot": "accountNumber"})])
  errors, _ = flows.validate_app(app)
  assert any("never fill anything" in e for e in errors), errors


def test_a_conditional_slot_cannot_be_seeded():
  """Whether it may be filled depends on the conversation, which has not happened."""
  app = _app(
      [flows.variable_map("m", {"account_number": "accountNumber"})],
      slots=[flows.user_slot("account_number", ask="Account number?",
                             condition={"slot": "other", "filled": True}),
             flows.user_slot("other", ask="Other?")])
  errors, _ = flows.validate_app(app)
  assert any("has a `condition`" in e for e in errors), errors


def test_an_unreachable_ordering_warns():
  app = _app([
      flows.variable_map("narrow", {"account_number": "accountNumber"}),
      flows.variable_map("wide", {"account_number": "accountNumber",
                                  "tracking_number": "account_id"}),
  ])
  _, warnings = flows.validate_app(app)
  assert any("can never be chosen" in w and "'wide'" in w for w in warnings), warnings


def test_a_well_ordered_app_validates_clean():
  app = _app([
      flows.variable_map("wide", {"account_number": "accountNumber",
                                  "tracking_number": "account_id"}),
      flows.variable_map("narrow", {"account_number": "accountNumber"}),
  ])
  errors, warnings = flows.validate_app(app)
  assert errors == []
  assert not [w for w in warnings if "variable_map" in w]


def _build(app):
  out = tempfile.mkdtemp()
  res = flows.build_app(app, out, overwrite=True)
  assert res.ok, res.error
  return out


def test_ingress_is_registered_ahead_of_the_author_hook():
  """THE contract. An author `before_agent` hook that acts on what the session arrived
  with — sweeping a backend off a seeded account number — can only do so on turn 0 if
  ingress has already run. Author entries keep their position relative to the
  framework's own `_01`; only the ingress entry is new, and it is first."""
  app = _app([flows.variable_map("m", {"account_number": "accountNumber"})],
             hooks=flows.AgentHooks(before_agent=_author_hook))
  out = _build(app)
  with open(os.path.join(out, "agents/Track_Agent/Track_Agent.json")) as f:
    order = [c["pythonCode"] for c in json.load(f)["beforeAgentCallbacks"]]
  assert order == [
      "agents/Track_Agent/before_agent_callbacks/before_agent_callbacks_00pre/python_code.py",
      "agents/Track_Agent/before_agent_callbacks/before_agent_callbacks_00/python_code.py",
      "agents/Track_Agent/before_agent_callbacks/before_agent_callbacks_01/python_code.py",
  ], order


def _author_hook(callback_context) -> None:
  """Stand-in for a hook that reads an already-seeded slot machine."""
  return None


def test_an_app_without_maps_emits_no_ingress_and_no_table():
  """The feature is additive: an app that declares none is what it was before."""
  app = _app([])
  out = _build(app)
  with open(os.path.join(out, "agents/Track_Agent/Track_Agent.json")) as f:
    order = [c["pythonCode"] for c in json.load(f)["beforeAgentCallbacks"]]
  assert order == [
      "agents/Track_Agent/before_agent_callbacks/before_agent_callbacks_01/python_code.py"]
  with open(os.path.join(out, "app.json")) as f:
    names = {v["name"] for v in json.load(f)["variableDeclarations"]}
  assert "variable_maps_by_config" not in names


def test_the_emitted_table_is_keyed_by_config_and_carries_the_lowered_bindings():
  app = _app([flows.variable_map(
      "m", {"account_number": flows.bind(["accountNumber", "account_id"],
                                         reject=["PENDING"])})])
  out = _build(app)
  with open(os.path.join(out, "app.json")) as f:
    decls = {v["name"]: v for v in json.load(f)["variableDeclarations"]}
  table = json.loads(decls["variable_maps_by_config"]["schema"]["default"])
  assert list(table) == ["track"]
  assert table["track"] == [{"name": "m", "bindings": [{
      "slot": "account_number", "shape": "scalar", "readback": False,
      "reject": ["PENDING"],
      "alts": [{"var": "accountNumber", "path": []},
               {"var": "account_id", "path": []}]}]}]


def test_the_documented_demo_still_says_what_the_docs_quote():
  """The examples page quotes this driver's four sessions. Pin the outcomes here, so
  a change to matching shows up as a failing test rather than as prose that has
  quietly stopped being true."""
  # The driver is documented as `python -m examples.variable_maps_drive` from
  # packages/flows, so reproduce that import root rather than loading it by path.
  import sys
  root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  if root not in sys.path:
    sys.path.insert(0, root)
  from examples import variable_maps_drive as drive

  expected = {
      "handed over from the tracking page": (
          "from_tracking", {"tracking_number", "account_number"},
          "And what's gone wrong with it?"),
      "handed over from the account line": (
          "from_account", {"account_number"}, "What's the tracking number?"),
      "the placeholder an upstream writes while a backend is thinking": (
          None, set(), "What's the tracking number?"),
      "a cold call, nothing seeded": (
          None, set(), "What's the tracking number?"),
  }
  for label, variables in drive.SCENARIOS:
    sm, chosen = drive._seed(variables)  # noqa: SLF001
    want_map, want_filled, want_ask = expected[label]
    assert chosen == want_map, label
    assert set(sm.get("filled", {})) == want_filled, label
    assert drive._first_question(sm) == want_ask, label  # noqa: SLF001


def test_the_emitted_table_round_trips_through_the_generated_callback():
  """The build's output and the callback's input are the same contract, so pin them
  together rather than trusting two hand-written fixtures to agree."""
  app = _app([flows.variable_map("m", {"account_number": ["accountNumber"]})])
  out = _build(app)
  with open(os.path.join(out, "app.json")) as f:
    decls = {v["name"]: v for v in json.load(f)["variableDeclarations"]}
  table = json.loads(decls["variable_maps_by_config"]["schema"]["default"])
  sm = _seed({"accountNumber": "8069"}, table=table)
  assert sm["filled"] == {"account_number": "8069"}


# ---------------------------------------------------------------------------
# Shapes that are not what the author meant. Each of these used to raise from
# inside the framework, one frame away from the mistake, or pass silently.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [123, True, None, {"a": 1}])
def test_a_source_that_is_not_a_name_is_rejected_at_authoring(bad):
  with pytest.raises(ValueError, match="bind"):
    flows.variable_map("m", {"slot": bad})


def test_a_list_of_non_names_is_rejected():
  with pytest.raises(ValueError, match="variable NAME"):
    flows.variable_map("m", {"slot": ["ok", 7]})


def test_a_raw_dict_instead_of_a_builder_is_reported_not_crashed():
  app = _app([{"name": "m", "bindings": {"a": "b"}}])
  errors, _ = flows.validate_app(app)
  assert any("takes VariableMap objects" in e for e in errors), errors


def test_a_slot_that_exists_in_no_flow_is_an_error_even_beside_a_valid_one():
  """Lowering drops the binding per config, and a map that lands elsewhere still
  passes the map-level check — so the typo is invisible and the question it should
  have skipped is asked instead."""
  app = _app([flows.variable_map("m", {"account_number": "accountNumber",
                                       "typo_slot": "account_id"})])
  errors, _ = flows.validate_app(app)
  assert any("'typo_slot' exists in no flow" in e for e in errors), errors


@pytest.mark.parametrize("table", [{"track": "nonsense"}, {"track": {"a": 1}},
                                   {"track": [None]}, {"track": ["str"]}])
def test_a_malformed_table_skips_ingress_rather_than_crashing_the_turn(table):
  """The table is a session variable, so whoever opens the session can overwrite it.
  A callback that raises takes the whole conversation down, and the flow would have
  run perfectly well with no seeding at all."""
  assert _seed({"accountNumber": "8069"}, table=table).get("filled", {}) == {}


@pytest.mark.parametrize("sm", ["not a dict", ["also", "not"], 7])
def test_a_non_dict_slot_machine_skips_ingress(sm):
  state = {"variable_maps_by_config": json.dumps(_TABLE),
           "default_config_id": "track", "accountNumber": "8069", "sm": sm}
  _INGRESS(_Ctx(state))
  assert state["sm"] != sm or state["sm"] == sm  # no exception is the assertion


def test_a_tuple_is_treated_as_a_list_not_a_scalar():
  """JSON turns a tuple into a list, so accepting one as a scalar fills the slot on
  the turn it arrives and rejects the identical value on the next."""
  scalar = {"track": [{"name": "m", "bindings": [
      _binding("account_number", [("accountNumber", [])])]}]}
  assert _seed({"accountNumber": ("a", "b")}, table=scalar).get("filled", {}) == {}
  listy = {"track": [{"name": "m", "bindings": [
      _binding("items", [("items", [])], shape="list")]}]}
  assert _seed({"items": ("a",)}, table=listy)["filled"] == {"items": ("a",)}
