"""Two before_model defects: a crash on `hide_tools: None`, and an SI that grows.

  * `_apply_directive` read the hide list as `action.get("hide_tools", [])`. A
    `.get` default only covers an ABSENT key — an explicit `"hide_tools": None`
    (which an engine action can carry, and which serializes through the tool
    round-trip) was iterated and raised TypeError. The turn dies inside the
    callback and the caller hears the platform "I'm having trouble" envelope.
    Every sibling list in the same function already reads `or []`.
  * `_inject_phase_suffix` REPLACES the framework suffix each pass, but it kept
    the "\\n\\n" separator the previous injection had appended and added another —
    so a multi-pass turn grew the system instruction by two blank lines per pass.

CES globals are runtime-injected, so the module is exec'd and the `NameError` on
the first annotated def is swallowed.
"""
from __future__ import annotations

import importlib.util
import os


from flows.engine import blessed_source as _bs


def _load():
  path = os.path.join(_bs._CALLBACKS_DIR, "before_model.py")
  spec = importlib.util.spec_from_file_location("_bm_hardening", path)
  mod = importlib.util.module_from_spec(spec)
  try:
    spec.loader.exec_module(mod)
  except Exception:  # CES globals are undefined at import time; expected.
    pass
  return mod


_BM = _load()
_SENTINEL = "<!-- slot-framework -->"


class _Cfg:
  def __init__(self, system_instruction=None):
    self.system_instruction = system_instruction
    self.hidden = []

  def hide_tool(self, name):
    self.hidden.append(name)


class _Req:
  def __init__(self, contents=None, system_instruction=None):
    self.contents = [] if contents is None else contents
    self.config = _Cfg(system_instruction)


class _Ctx:
  def __init__(self, **state):
    self.state = dict(state)


# =========================================================================== #
# hide_tools: None
# =========================================================================== #
def test_an_explicit_none_hide_list_does_not_take_the_turn_down():
  req, ctx = _Req(), _Ctx()
  out = _BM._apply_directive(ctx, req, {}, {"hide_tools": None}, "t")
  assert out == {"decision": "OK"}
  assert req.config.hidden == []


def test_an_absent_hide_list_is_still_fine():
  req = _Req()
  _BM._apply_directive(_Ctx(), req, {}, {}, "t")
  assert req.config.hidden == []


def test_a_real_hide_list_still_hides():
  req = _Req()
  _BM._apply_directive(_Ctx(), req, {}, {"hide_tools": ["set_acct", "cancel"]}, "t")
  assert req.config.hidden == ["set_acct", "cancel"]


def test_a_none_hide_list_does_not_stop_the_other_hide_sources():
  """The crash took the whole function with it, including the engine-task and
  router hides below it — every one of which is a safety mechanism."""
  req = _Req()
  ctx = _Ctx(engine_task_tools=["do_lookup"])
  _BM._apply_directive(ctx, req, {}, {"hide_tools": None}, "t")
  assert req.config.hidden == ["do_lookup"]


# =========================================================================== #
# The system instruction stopped growing
# =========================================================================== #
def test_re_injecting_the_suffix_does_not_add_blank_lines():
  req = _Req(system_instruction="BASE INSTRUCTION")
  _BM._inject_phase_suffix(req, "PHASE ONE")
  first = req.config.system_instruction
  _BM._inject_phase_suffix(req, "PHASE ONE")
  assert req.config.system_instruction == first


def test_the_instruction_is_the_same_size_after_ten_passes():
  req = _Req(system_instruction="BASE INSTRUCTION")
  _BM._inject_phase_suffix(req, "PHASE")
  after_one = req.config.system_instruction
  for _ in range(9):
    _BM._inject_phase_suffix(req, "PHASE")
  assert req.config.system_instruction == after_one


def test_the_base_and_the_newest_suffix_both_survive_a_re_injection():
  req = _Req(system_instruction="BASE INSTRUCTION")
  _BM._inject_phase_suffix(req, "PHASE ONE")
  _BM._inject_phase_suffix(req, "PHASE TWO")
  text = req.config.system_instruction
  assert text == f"BASE INSTRUCTION\n\n{_SENTINEL}\nPHASE TWO"


def test_the_first_injection_is_unchanged():
  """The rstrip applies only when a previous suffix is present, so an SI that
  legitimately ends in newlines keeps them on the way in."""
  req = _Req(system_instruction="BASE\n\n")
  _BM._inject_phase_suffix(req, "PHASE")
  assert req.config.system_instruction == f"BASE\n\n\n\n{_SENTINEL}\nPHASE"


def test_it_still_declines_when_there_is_no_instruction_to_inject_into():
  req = _Req(system_instruction=None)
  _BM._inject_phase_suffix(req, "PHASE")
  assert req.config.system_instruction is None
