"""Luhn (mod-10) card-number validation: the `luhn_valid` helper, the emitted
setter checks (multi-field + standalone), and the build.py dispatch that routes a
lone card slot to a validating setter.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_luhn.py
"""

from __future__ import annotations

import json

import pytest

from flows import luhn_valid, user_slot
from flows.authoring import build as _build
from flows.authoring import setters as _setters

# Well-known test PANs (all Luhn-valid) and a broken twin.
VALID = ["4242424242424242", "4111111111111111", "5555555555554444",
         "378282246310005", "6011111111111117"]
INVALID = ["4242424242424241", "1234567812345678", "4111111111111112"]


def _exec(src: str, fn_name: str):
  ns: dict = {}
  exec(compile(src, "<generated>", "exec"), ns)  # noqa: S102 — the emitted body IS the contract
  return ns[fn_name]


# --- helper -----------------------------------------------------------------
@pytest.mark.parametrize("pan", VALID)
def test_luhn_valid_accepts_real_check_digits(pan):
  assert luhn_valid(pan) is True


@pytest.mark.parametrize("pan", INVALID)
def test_luhn_valid_rejects_bad_check_digits(pan):
  assert luhn_valid(pan) is False


def test_luhn_valid_strips_separators():
  assert luhn_valid("4242 4242 4242 4242") is True
  assert luhn_valid("4242-4242-4242-4242") is True


@pytest.mark.parametrize("bad", ["", "123", "not a card", "0000000000000000"[:10]])
def test_luhn_valid_rejects_empty_short_or_nondigit(bad):
  assert luhn_valid(bad) is False


# --- emitted multi-field setter (setter_group / payment op) -----------------
def _card_multi():
  return _exec(_setters.gen_multi_setter(
      "set_pay_inputs",
      [{"name": "card_number",
        "validation_rules": [{"kind": "length_digits", "detail": "16"},
                             {"kind": "luhn"}]}]),
      "set_pay_inputs")


def test_multi_setter_stores_valid_card_normalized():
  out = _card_multi()(card_number="4242 4242 4242 4242")
  assert out == {"stored": True, "values": {"card_number": "4242424242424242"},
                 "field_errors": {}}


def test_multi_setter_rejects_bad_checksum():
  out = _card_multi()(card_number="4242424242424241")
  assert out["field_errors"] == {"card_number": "invalid_card"}
  assert out["stored"] is False


def test_multi_setter_length_rule_still_applies():
  # 15 digits fails the length rule before Luhn is reached.
  out = _card_multi()(card_number="424242424242424")
  assert out["field_errors"] == {"card_number": "invalid_length"}


# --- emitted standalone validating setter -----------------------------------
def _card_single():
  return _exec(_setters.gen_validating_setter(
      "set_card_number", "card_number",
      [{"kind": "length_digits", "detail": "16"}, {"kind": "luhn"}]),
      "set_card_number")


def test_single_setter_valid_invalid_missing():
  fn = _card_single()
  assert fn(card_number="4242-4242-4242-4242") == {"stored": True, "value": "4242424242424242"}
  assert fn(card_number="1234567812345678") == {"error": True, "error_code": "invalid_card"}
  assert fn(card_number="") == {"error": True, "error_code": "missing"}


# --- helper and emitted setter agree (can't drift) --------------------------
@pytest.mark.parametrize("pan", VALID + INVALID)
def test_emitted_luhn_matches_helper(pan):
  fn = _exec(_setters.gen_validating_setter(
      "set_card_number", "card_number", [{"kind": "luhn"}]), "set_card_number")
  stored = fn(card_number=pan).get("stored") is True
  assert stored == luhn_valid(pan)


# --- build.py dispatch: a lone card slot gets a VALIDATING setter -----------
def test_build_routes_standalone_card_slot_to_validating_setter():
  slot = user_slot("card_number", "card number?",
                   validation_rules=[{"kind": "length_digits", "detail": "16"},
                                     {"kind": "luhn"}])
  slot["setter"] = "set_card_number"
  cfg = {"slots": [slot], "tasks": []}
  bodies, _ = _build.collect([cfg], tool_bodies={})
  body = bodies["set_card_number"]
  assert "invalid_card" in body  # not the inert plain gen_setter
  fn = _exec(body, "set_card_number")
  assert fn(card_number="4242424242424242")["stored"] is True
  assert fn(card_number="4242424242424241") == {"error": True, "error_code": "invalid_card"}


def test_user_slot_luhn_rule_attaches():
  s = user_slot("card_number", "card?", validation_rules=[{"kind": "luhn"}])
  assert s["validation_rules"] == [{"kind": "luhn"}]


# --- review hardening -------------------------------------------------------
def test_helper_rejects_unicode_digits_type_and_overlong():
  assert luhn_valid("٤٢٤٢٤٢٤٢٤٢٤٢٤٢٤٢") is False   # Arabic-Indic "4242..." must not slip through
  assert luhn_valid(["4242424242424242"]) is False  # type guard: list
  assert luhn_valid(True) is False                   # type guard: bool
  assert luhn_valid("4" * 10000) is False            # length gate before the sum loop


def test_emitted_luhn_rejects_unicode_and_overlong():
  # luhn-only (no length rule) so the failure is attributed to the Luhn check itself.
  fn = _exec(_setters.gen_validating_setter(
      "set_card_number", "card_number", [{"kind": "luhn"}]), "set_card_number")
  assert fn(card_number="٤٢٤٢٤٢٤٢٤٢٤٢٤٢٤٢") == {"error": True, "error_code": "invalid_card"}
  assert fn(card_number="4" * 10000) == {"error": True, "error_code": "invalid_card"}


def test_slot_named_values_or_field_errors_does_not_shadow_and_crash():
  # The generated locals are `_values`/`_field_errors`, so a slot literally named
  # `values` or `field_errors` can't collide (which caused a circular-ref JSON crash).
  single = _exec(_setters.gen_validating_setter(
      "set_values", "values", [{"kind": "length_digits", "detail": "16"}]), "set_values")
  out = single(values="4242424242424242")
  assert out == {"stored": True, "value": "4242424242424242"}
  assert json.dumps(out)  # serializes — no circular reference

  multi = _exec(_setters.gen_multi_setter(
      "set_x", [{"name": "field_errors", "validation_rules": [{"kind": "luhn"}]}]), "set_x")
  out2 = multi(field_errors="4242424242424242")
  assert out2["values"] == {"field_errors": "4242424242424242"}
  assert json.dumps(out2)
