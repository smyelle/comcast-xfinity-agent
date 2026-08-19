"""`readback_fmt` params the formatter does not read are a VALIDATION error.

`_compile_formatter` pulls the keys it knows off the dict and ignores the rest, so a
param that is misspelled, invented, or borrowed from another type is discarded in
silence: the readback still renders, just not the way the author wrote it. That is
precisely how `{"type": "date", "text": …}` lost its label and a `prefix` lost its
`values` map — the config looked right and the call sounded wrong.

The check is an ERROR rather than a warning because it has no false-positive class: the
allowed set is derived from what the compiler actually reads. It was run against all
five Equifax production configs first — every `readback_fmt` there uses known params
only, so nothing that ships today newly fails.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_readback_fmt_params.py
"""

from __future__ import annotations

import pytest

from flows.engine import loader

vdc = loader.load_validator()
eng = loader.load_engine()


def _errors(fmt):
  cfg = {"config_id": "x", "slots": [
      {"name": "phone", "source": "user", "ask": "Number?", "readback_fmt": fmt},
  ]}
  return vdc.validate_dag_config({"raw_config": cfg})["errors"]


def _fmt_errors(fmt):
  return [e for e in _errors(fmt) if "readback_fmt" in e]


# --- the regression ----------------------------------------------------------
def test_an_unknown_param_is_rejected():
  errs = _fmt_errors({"type": "digits", "txt": "the ZIP code"})
  assert any("unknown param(s) ['txt']" in e for e in errs), errs


def test_a_param_borrowed_from_another_type_is_rejected():
  """`values` is real — on `prefix`. On `digits` it is silently dropped."""
  errs = _fmt_errors({"type": "digits", "values": {"a": "b"}})
  assert any("unknown param(s) ['values']" in e for e in errs), errs


def test_the_error_names_the_valid_params():
  errs = _fmt_errors({"type": "prefix", "text": "you'd like me to", "value": {}})
  assert any("valid: ['text', 'type', 'values']" in e for e in errs), errs


def test_a_dropped_param_is_the_failure_this_prevents():
  """The point, stated as behavior: the compiler ignores the typo'd key entirely, so
  without the check the slot reads back with NO label and nothing says why."""
  good = eng._compile_formatter({"type": "digits", "text": "the ZIP code"})
  typo = eng._compile_formatter({"type": "digits", "txt": "the ZIP code"})
  assert "the ZIP code" in good("30301")
  assert "the ZIP code" not in typo("30301")  # silently lost — hence the error
  assert _fmt_errors({"type": "digits", "txt": "the ZIP code"})


# --- every valid shape still passes ------------------------------------------
@pytest.mark.parametrize("fmt", [
    {"type": "digits"},
    {"type": "digits", "text": "the ZIP code"},
    {"type": "date"},
    {"type": "date", "text": "the lift will start on"},
    {"type": "prefix", "text": "you'd like me to"},
    {"type": "prefix", "text": "you'd like me to", "values": {"a": "b"}},
    {"type": "plural", "one": "guest", "other": "guests"},
    {"type": "count", "one": "item", "other": "items"},
    {"type": "none_sub", "default": "none"},
    {"type": "join", "each": "{item}"},
    {"type": "join", "each": "{item}", "sep": " and "},
    {"type": "time"},
    "digits",
])
def test_valid_readback_fmts_are_accepted(fmt):
  assert _fmt_errors(fmt) == []


def test_the_allowed_set_matches_what_the_compiler_reads():
  """The table is only trustworthy if it tracks `_compile_formatter`. Every declared
  param must survive compilation — i.e. actually change the rendered output — so a
  formatter that grows a param and forgets the table is caught here."""
  probes = {
      ("digits", "text"): ({"type": "digits", "text": "ZED"}, "212", "ZED"),
      ("date", "text"): ({"type": "date", "text": "ZED"}, "2027-01-15", "ZED"),
      ("prefix", "text"): ({"type": "prefix", "text": "ZED"}, "x", "ZED"),
      ("prefix", "values"): (
          {"type": "prefix", "text": "t", "values": {"x": "ZED"}}, "x", "ZED"),
      ("join", "sep"): ({"type": "join", "each": "{item}", "sep": "ZED"},
                        ["a", "b"], "ZED"),
  }
  for (fmt_type, param), (fmt, value, marker) in probes.items():
    assert _fmt_errors(fmt) == [], f"{fmt_type}.{param} rejected by the validator"
    assert marker in eng._compile_formatter(fmt)(value), (
        f"{fmt_type}.{param} is allowed but the compiler ignores it")
