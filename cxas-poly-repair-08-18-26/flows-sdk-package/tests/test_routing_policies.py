"""Structured routing policies on `router_flow`: default_route, catch_all_route,
explicit_only, the primary-intent tie-break, and the routing_notes escape hatch.

These render canonical, tuned guidance at the tail of the generated `<routing>` block (see
steering.routing_block), replacing hand-written prose an author would otherwise stuff into
a persona. They must render only when set, name real routes, and refuse a low-confidence
fallback that is marked explicit_only.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_routing_policies.py
"""

from __future__ import annotations

import pytest

import flows
from flows.authoring import steering as _steering
from flows.authoring import tools as _tools


@pytest.fixture(autouse=True)
def _clean_registry():
  _tools.clear_registry()
  yield
  _tools.clear_registry()


def _routes():
  return [
      flows.route("billing", "the bill or money on the account"),
      flows.route("repair", "internet is down, slow, or dropping"),
      flows.route("human", "reach a live person", explicit_only=True),
      flows.route("main_menu", "a known account task with no specific home"),
  ]


def _si(**kw) -> str:
  f = flows.router_flow("steering", _routes(), **kw)
  return _steering.routing_instruction(f._steering)


# --- rendering --------------------------------------------------------------------

def test_default_route_and_catch_all_render():
  si = _si(default_route="repair", catch_all_route="main_menu")
  assert 'not confident which flow fits' in si and '"repair"' in si
  assert 'none of the flows above fit' in si and '"main_menu"' in si
  assert 'do not send them to a person' in si  # catch-all beats escalation


def test_explicit_only_renders_and_is_precise():
  si = _si()
  assert 'Choose "human" only when the caller explicitly asks' in si
  assert 'never route there by inference' in si


def test_primary_tie_break_default_on_off():
  assert 'route on the caller\'s primary intent' in _si().lower()
  assert 'primary intent' not in _si(tie_break="none").lower()


def test_routing_notes_are_the_escape_hatch():
  si = _si(routing_notes=["Checking a balance is billing, not payments."])
  assert 'Checking a balance is billing, not payments.' in si


def test_policies_land_inside_the_routing_block_after_the_routes():
  si = _si(default_route="repair", catch_all_route="main_menu")
  # order: the route list, then "Route on the caller's actual goal.", then the policies,
  # all before the closing tag.
  assert si.index('flow="main_menu"') < si.index('Route on the caller') \
      < si.index('not confident which flow fits') < si.index('</routing>')


# --- validation -------------------------------------------------------------------

def test_default_route_must_name_a_real_route():
  with pytest.raises(ValueError, match="default_route"):
    flows.router_flow("steering", _routes(), default_route="nope")


def test_catch_all_route_must_name_a_real_route():
  with pytest.raises(ValueError, match="catch_all_route"):
    flows.router_flow("steering", _routes(), catch_all_route="nope")


def test_fallback_cannot_be_an_explicit_only_route():
  with pytest.raises(ValueError, match="explicit_only"):
    flows.router_flow("steering", _routes(), default_route="human")


def test_tie_break_rejects_unknown_value():
  with pytest.raises(ValueError, match="tie_break"):
    flows.router_flow("steering", _routes(), tie_break="whatever")


def test_bare_key_form_rejects_structured_knobs():
  with pytest.raises(ValueError, match="route-object form"):
    flows.router_flow("steering", ["billing", "repair"], default_route="repair")


def test_classifier_style_rejects_unknown_value():
  with pytest.raises(ValueError, match="classifier_style"):
    flows.router_flow("steering", _routes(), classifier_style="bogus")
