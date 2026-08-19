"""Static checks on a generated tool's body — `flows.lint.toolcode` + its rules.

These read the tool SOURCE rather than the DAG, which is where a class of failure
lives that no config check can see. The defects are ranked by how quietly they fail:
`**kwargs` makes CES drop the tool with no error anywhere, a forbidden construct
crashes at call time, a stale docstring misroutes an argument, and a syntax error
deploys cleanly and dies on first use.

The source is parsed, never executed, so a body here can be as broken as it likes.
"""

from __future__ import annotations

import ast

import pytest

from flows.lint.toolcode import ToolCodeIssue, tool_source_issues

GOOD = '''
def set_zip_code(zip_code: str) -> dict:
  """Store the caller's ZIP.

  Args:
    zip_code: five digits.
  """
  return {"stored": True, "value": zip_code}
'''


def kinds(name: str, src: str) -> list[str]:
  return sorted(i.kind for i in tool_source_issues(name, src))


def only(name: str, src: str, kind: str) -> ToolCodeIssue:
  found = [i for i in tool_source_issues(name, src) if i.kind == kind]
  assert len(found) == 1, f"expected one {kind}, got {tool_source_issues(name, src)}"
  return found[0]


# --- the clean case -----------------------------------------------------------
def test_a_well_formed_tool_reports_nothing():
  assert tool_source_issues("set_zip_code", GOOD) == []


def test_no_args_section_is_not_a_defect():
  """Not every setter documents its params; only a section that DISAGREES is wrong."""
  src = 'def set_x(x: str) -> dict:\n  """Store x."""\n  return {"stored": True}\n'
  assert tool_source_issues("set_x", src) == []


# --- kwargs -------------------------------------------------------------------
def test_kwargs_is_reported_with_its_line():
  src = "def set_x(**kwargs) -> dict:\n  return {}\n"
  issue = only("set_x", src, "kwargs")
  assert "silently drops" in issue.message
  assert issue.line == 1


def test_kwargs_on_a_nested_helper_is_still_reported():
  """CES reads the module, not just the entry point."""
  src = "def set_x(x: str) -> dict:\n  return {}\n\ndef _helper(**kw):\n  return kw\n"
  assert only("set_x", src, "kwargs").line == 4


def test_a_kwargs_tool_is_not_also_reported_as_a_docstring_mismatch():
  """Two findings for one defect trains people to ignore the second."""
  src = ('def set_x(x: str, **kwargs) -> dict:\n'
         '  """Store x.\n\n  Args:\n    x: the value.\n  """\n'
         '  return {"stored": True}\n')
  assert kinds("set_x", src) == ["kwargs"]


# --- forbidden constructs -----------------------------------------------------
def test_touching_slot_machine_state_is_forbidden():
  src = 'def set_x(x: str) -> dict:\n  sm["filled"]["x"] = x\n  return {}\n'
  assert "'sm' is forbidden" in only("set_x", src, "forbidden").message


@pytest.mark.parametrize("base", ["callback_context", "context", "ctx"])
def test_reading_session_off_the_context_is_forbidden(base):
  """`.session` is ADK-only and crashes CES at runtime."""
  src = f"def set_x({base}) -> dict:\n  return {base}.session\n"
  assert ".session' is forbidden" in only("set_x", src, "forbidden").message


def test_session_on_an_unrelated_object_is_left_alone():
  """Only the callback context is the hazard; a `requests.session` is ordinary."""
  src = "def set_x(client) -> dict:\n  return client.session\n"
  assert tool_source_issues("set_x", src) == []


@pytest.mark.parametrize("line", ["import tools.other",
                                  "from tools.other import helper",
                                  "from tools import other"])
def test_importing_another_tool_is_forbidden(line):
  src = f"{line}\n\ndef set_x(x) -> dict:\n  return {{}}\n"
  assert "cannot import each other" in only("set_x", src, "forbidden").message


def test_an_ordinary_import_is_left_alone():
  src = "import json\nfrom datetime import date\n\ndef set_x(x) -> dict:\n  return {}\n"
  assert tool_source_issues("set_x", src) == []


# --- docstring ----------------------------------------------------------------
def test_a_documented_param_that_does_not_exist_is_reported():
  src = ('def set_x(x: str) -> dict:\n  """Store.\n\n  Args:\n    x: v.\n    y: gone.\n  """\n'
         '  return {}\n')
  assert "documents unknown params ['y']" in only("set_x", src, "docstring").message


def test_a_signature_param_that_is_not_documented_is_reported():
  src = ('def set_x(x: str, y: str) -> dict:\n  """Store.\n\n  Args:\n    x: v.\n  """\n'
         '  return {}\n')
  assert "missing docs for ['y']" in only("set_x", src, "docstring").message


# --- syntax -------------------------------------------------------------------
def test_a_syntax_error_short_circuits_the_other_checks():
  """A cascade of follow-on noise would bury the one line the author must fix."""
  src = "def set_x(**kwargs)\n  return sm\n"
  assert kinds("set_x", src) == ["syntax"]


def test_nothing_here_executes_the_source():
  """The body is written by a model and read by this before anyone has seen it."""
  src = ('import sys\n'
         'raise SystemExit("this must never run")\n'
         'def set_x(x) -> dict:\n  return {}\n')
  assert tool_source_issues("set_x", src) == []


def test_a_broken_check_does_not_hide_the_others(monkeypatch):
  from flows.lint import toolcode

  monkeypatch.setattr(toolcode, "_docstring_issues",
                      lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
  found = tool_source_issues("set_x", "def set_x(**kw) -> dict:\n  return {}\n")
  assert "kwargs" in {i.kind for i in found}
  assert any("check failed" in i.message for i in found)


# --- the rules ----------------------------------------------------------------
def _run(bodies: dict[str, str], select=None):
  from flows.lint.context import LintContext
  from flows.lint.runner import run_rules

  ctx = LintContext(app=None, configs={}, bodies=bodies, available=list(bodies))
  return run_rules(ctx, select=select)


def test_each_defect_gets_its_own_code():
  report = _run({
      "set_a": "def set_a(**kw) -> dict:\n  return {}\n",
      "set_b": "def set_b(x) -> dict:\n  return sm\n",
      "set_c": "def set_c(broken\n",
  })
  by_code = {f.code for f in report.findings}
  assert {"FLW004", "FLX002", "FLX004"} <= by_code


def test_a_clean_tool_produces_no_toolcode_finding():
  report = _run({"set_zip_code": GOOD})
  assert [f.code for f in report.findings
          if f.code in {"FLW004", "FLX002", "FLX003", "FLX004"}] == []


def test_the_kwargs_finding_blocks_and_the_docstring_one_does_not():
  """Severity is the whole point of splitting these: one stops a deploy, one nags."""
  kwargs = _run({"a": "def a(**kw) -> dict:\n  return {}\n"}, select=["FLW004"])
  docs = _run({"b": 'def b(x) -> dict:\n  """D.\n\n  Args:\n    y: no.\n  """\n  return {}\n'},
              select=["FLX003"])
  assert not kwargs.ok(strict=False)
  assert docs.ok(strict=False)
  assert not docs.ok(strict=True)


def test_a_finding_points_at_the_file_and_line():
  report = _run({"set_a": "def set_a(x) -> dict:\n  return sm\n"}, select=["FLX002"])
  assert report.findings[0].location.json_path == "tools/set_a.py:2"


def test_rules_can_be_selected_individually():
  bodies = {"set_a": "def set_a(**kw) -> dict:\n  return sm\n"}
  assert {f.code for f in _run(bodies, select=["FLW004"]).findings} == {"FLW004"}
