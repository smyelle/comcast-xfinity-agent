"""FLC140 and FLW005 — two defects found by building an agent with only the wheel.

Both shipped clean through validate, lint, emit, check and deploy, and both fail at run
time in the worst way available: silently, on the turn that mattered, with the caller
told it worked.

* FLC140 — a readback with no `correction_tool`. Driven live, a caller who answered
  "no, make it Friday" was booked for Monday and given a confirmation number.
* FLW005 — an `on_exhaust.then` naming a tool nothing registers. The framework's own
  docstring, docs and five examples all said `"then": "escalate"`, and there has never
  been an `escalate` tool.
"""

from __future__ import annotations

import pytest

from flows.lint.context import LintContext
from flows.lint.runner import run_rules


def lint(config: dict, *, select=None, bodies=None, available=None):
  ctx = LintContext(app=None, configs={"demo": config}, bodies=bodies or {},
                    available=available or [])
  return run_rules(ctx, select=select)


def codes(report) -> set[str]:
  return {f.code for f in report.findings}


def _slot(name="repair_day", **kw):
  return {"name": name, "source": "user", "ask": f"What {name}?",
          "setter": f"set_{name}", **kw}


# --- FLC140: the readback a caller cannot correct -----------------------------
def test_a_readback_without_a_correction_tool_is_an_error():
  report = lint({"slots": [_slot(requires_readback=True)], "tasks": []},
                select=["FLC140"])
  assert codes(report) == {"FLC140"}
  assert not report.ok(strict=False), "a mis-booked value must block, not warn"


def test_declaring_a_correction_tool_clears_it():
  report = lint({"slots": [_slot(requires_readback=True)], "tasks": [],
                 "correction_tool": "set_slot_change"}, select=["FLC140"])
  assert codes(report) == set()


def test_a_flow_with_no_readback_is_not_flagged():
  """Nothing is read back, so there is no gate at which to lose a correction."""
  report = lint({"slots": [_slot()], "tasks": []}, select=["FLC140"])
  assert codes(report) == set()


@pytest.mark.parametrize("field", ["requires_readback", "readback"])
def test_either_spelling_of_readback_triggers_it(field):
  report = lint({"slots": [_slot(**{field: True})], "tasks": []}, select=["FLC140"])
  assert codes(report) == {"FLC140"}


def test_the_finding_names_every_slot_at_risk():
  report = lint({"slots": [_slot("repair_day", requires_readback=True),
                           _slot("phone_number", requires_readback=True),
                           _slot("caller_name")],
                 "tasks": []}, select=["FLC140"])
  msg = report.findings[0].message
  assert "repair_day" in msg and "phone_number" in msg
  assert "caller_name" not in msg
  assert "set_slot_change" in msg, "the message must carry the one-line fix"


def test_one_finding_per_config_not_one_per_slot():
  """The fix is a single top-level key; three findings would be three times the noise."""
  report = lint({"slots": [_slot("a", requires_readback=True),
                           _slot("b", requires_readback=True)],
                 "tasks": []}, select=["FLC140"])
  assert len(report.findings) == 1


# --- FLW005: the exhaust that dispatches a ghost ------------------------------
def _exhaust_slot(then):
  return _slot(validation={"max_retries": 2,
                           "on_exhaust": {"say": "Sorry.", "then": then}})


def test_a_string_then_naming_no_tool_is_an_error():
  report = lint({"slots": [_exhaust_slot("escalate")], "tasks": []}, select=["FLW005"])
  assert codes(report) == {"FLW005"}
  assert "escalate" in report.findings[0].message
  assert "69-ghost-leg-hang" in report.findings[0].message, (
      "cite the probe — the reader needs to know the platform drops the whole call")


def test_a_dict_then_naming_no_tool_is_an_error():
  report = lint({"slots": [_exhaust_slot({"tool": "nope"})], "tasks": []},
                select=["FLW005"])
  assert codes(report) == {"FLW005"}


def test_a_framework_tool_resolves():
  report = lint({"slots": [_exhaust_slot({"tool": "transfer_to_human"})], "tasks": []},
                select=["FLW005"])
  assert codes(report) == set()


def test_end_session_resolves_because_the_emitter_registers_it():
  """The heart of the bug: the config layer groups `end_session` and `escalate` as
  the same kind of thing, and the emitter registers only one of them."""
  report = lint({"slots": [_exhaust_slot("end_session")], "tasks": []},
                select=["FLW005"])
  assert codes(report) == set()


def test_a_tool_the_app_declares_resolves():
  report = lint({"slots": [_exhaust_slot("page_a_human")], "tasks": []},
                select=["FLW005"], available=["page_a_human"])
  assert codes(report) == set()


def test_a_task_exhaust_is_checked_too():
  report = lint({"slots": [_slot()],
                 "tasks": [{"name": "Book", "tool": "book",
                            "on_failure": {"max_retries": 1,
                                           "on_exhaust": {"then": "escalate"}}}]},
                select=["FLW005"])
  assert codes(report) == {"FLW005"}


def test_the_flow_level_silence_ladder_is_checked_too():
  report = lint({"slots": [_slot()], "tasks": [],
                 "no_input": {"reprompts": ["Still there?"],
                              "on_exhaust": {"then": "escalate"}}},
                select=["FLW005"])
  assert codes(report) == {"FLW005"}
  assert report.findings[0].location.json_path == "no_input/on_exhaust/then"


def test_an_exhaust_with_no_then_is_not_flagged():
  report = lint({"slots": [_exhaust_slot(None)], "tasks": []}, select=["FLW005"])
  assert codes(report) == set()
