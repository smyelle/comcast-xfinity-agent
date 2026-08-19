"""Engine hardening — three ways rendering a value KILLED the turn instead of degrading.

Every one of these reaches the caller as the platform crash envelope ("I'm having
trouble with that"), which says nothing about the agent and cannot be recovered from.
A renderer on the spoken surface has one job it must never fail at: produce a string.

  * `_format_plural` bare `int(v)`. `_build_readback` calls a readback formatter
    UNGUARDED, so a party-size slot holding None / "" / "abc" / a list — anything a
    setter or a task output can legitimately produce — raised out of the engine and
    took the whole confirmation turn with it. Its sibling `_format_count` already
    tolerated exactly these; this matches it.

  * `_safe_format` did not catch `AttributeError`. `_SafeFmt.__missing__` only covers
    an UNKNOWN key, so a DOTTED reference to a known slot ("{order.id}" over a dict)
    resolved the root and then raised. Every crash-safe render site funnels through
    here — ask/hint, a task's then_say/then_directive, an announce, and
    `_substitute_response` — so one uncaught class un-hardened all of them at once.

  * `_format_time` never range-checked, so `h % 12` spoke "25:00" as "at 1:00 PM" —
    inventing a time the producer never supplied, which is worse than saying nothing.

Fully offline: no network, no creds, no LLM.

Run:
  cd /Users/fsamuel/Labs/cxas-labs
  PYTHONPATH=packages/flows/src .venv/bin/python -m pytest \
      packages/flows/tests/test_engine_hardening_render.py -q
"""

from __future__ import annotations

import pytest

from flows.engine import loader as fb

eng = fb.load_engine()


@pytest.fixture(autouse=True)
def _drop_engine_caches():
  """The engine caches compiled configs process-globally, keyed by config id."""
  yield
  fb.clear_cache()


# --------------------------------------------------------------------------- #
# _format_plural — a readback formatter must never raise


@pytest.mark.parametrize("value,expected", [
    (None, "0 guests"),
    ("", "0 guests"),
    ("abc", "1 guest"),
    (["a"], "1 guest"),
])
def test_a_non_numeric_party_size_renders_instead_of_raising(value, expected):
  """None/"" read as nothing (0); any other non-numeric scalar as one thing — the
  same degradation `_format_count` already made, so the two stay interchangeable."""
  assert eng._format_plural(value, one="guest", other="guests") == expected


def test_a_numeric_party_size_is_untouched():
  assert eng._format_plural(1, one="guest", other="guests") == "1 guest"
  assert eng._format_plural("4", one="guest", other="guests") == "4 guests"


def test_the_confirmation_turn_survives_a_non_numeric_plural_slot():
  """The reason it matters: `_build_readback` calls the formatter with no guard, so
  the exception escaped the engine and the caller heard the crash envelope instead of
  "just to confirm"."""
  fmt = eng._compile_formatter({"type": "plural", "one": "guest", "other": "guests"})
  slots = [{"name": "party_size", "readback_fmt": fmt}]

  result = eng._build_readback(slots, {"party_size": "abc"}, {})

  assert result["action"] == "awaiting_readback"
  assert "1 guest" in result["system_message"]


# --------------------------------------------------------------------------- #
# _safe_format — a dotted reference is text, not a crash


def test_a_dotted_reference_degrades_to_the_literal_template():
  """`{order.id}` over a dict resolves the root then does getattr on it. Left
  uncaught it raised past every ask/announce/then_say render site."""
  assert eng._safe_format("Order {order.id} is ready.",
                          {"order": {"id": "A1"}}) == "Order {order.id} is ready."


def test_the_already_tolerated_shapes_still_degrade_the_same_way():
  assert eng._safe_format("{unfilled}", {}) == "{unfilled}"
  assert eng._safe_format("a {malformed", {}) == "a {malformed"
  assert eng._safe_format("{indexed[0]}", {"indexed": {"a": 1}}) == "{indexed[0]}"


def test_a_resolvable_placeholder_is_unaffected():
  assert eng._safe_format("Hi {name}.", {"name": "Sam"}) == "Hi Sam."
  assert eng._safe_format("Hi {name|there}.", {}) == "Hi there."


def test_a_response_part_with_a_dotted_reference_is_still_delivered():
  """`_substitute_response` renders every announce/readback response part. One bad
  reference used to take the entire turn rather than just that placeholder."""
  parts = eng._substitute_response(
      [{"type": "text", "text": "Your order {order.id} shipped."},
       {"type": "text", "text": "Thanks {name}."}],
      {"order": {"id": "A1"}, "name": "Sam"})

  assert parts[0]["text"] == "Your order {order.id} shipped."
  assert parts[1]["text"] == "Thanks Sam."


# --------------------------------------------------------------------------- #
# _format_time — never speak a time nobody supplied


@pytest.mark.parametrize("value", ["25:00", "12:75", "-1:00", "24:00"])
def test_an_out_of_range_time_degrades_to_the_raw_value(value):
  """`h % 12 or 12` happily turned 25 into 1, so an out-of-range hour was read back
  as a plausible, wrong time — the caller has no way to notice."""
  assert eng._format_time(value) == f"at {value}"


@pytest.mark.parametrize("value,spoken", [
    ("09:30", "at 9:30 AM"),
    ("00:00", "at 12:00 AM"),
    ("12:00", "at 12:00 PM"),
    ("23:59", "at 11:59 PM"),
])
def test_an_in_range_time_is_unchanged(value, spoken):
  assert eng._format_time(value) == spoken


def test_an_unparseable_time_still_degrades_as_before():
  assert eng._format_time("whenever") == "at whenever"
