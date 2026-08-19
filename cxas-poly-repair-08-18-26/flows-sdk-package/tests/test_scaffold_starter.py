"""`flows new` must scaffold an agent that actually talks.

The starter shipped broken and nothing caught it: every announce was authored with
the default `preempt=False`, whose `texts` the engine drops rather than speaks, and
the terminal `goodbye` carried no `requires=`. An announce is eligible the moment its
`requires` are met, and an empty `requires` is met immediately — so on the opening
turn the engine filled `welcome` and `goodbye`, returned `status: complete` with an
empty message and no response, and hung up. A new user's very first run produced an
agent that said nothing and ended the call.

These drive the real scaffold through the real engine rather than reading the
template, because the template is a string and every interesting failure here is a
runtime one.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

import flows
from flows.config import config_io
from flows.engine import loader
from flows.emit import scaffold as scaffold_mod
from flows.templates import scaffold_project


def _starter(tmp_path: pathlib.Path) -> types.ModuleType:
  scaffold_project(str(tmp_path / "demo"), name="Demo Agent")
  path = tmp_path / "demo" / "app.py"
  spec = importlib.util.spec_from_file_location("_starter_app", path)
  assert spec and spec.loader
  mod = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = mod
  spec.loader.exec_module(mod)
  return mod


def _opening_turn(mod: types.ModuleType):
  sm = {"filled": {}, "pending": {}, "status": "in_progress", "task_results": {}}
  out = loader.run_engine(mod.flow.to_config(), sm, last_user_text="",
                          config_id="my_agent")
  return out["action"], out["sm"]


@pytest.fixture()
def starter(tmp_path):
  mod = _starter(tmp_path)
  yield mod
  sys.modules.pop("_starter_app", None)


def test_starter_validates_clean(starter):
  errors, warnings = flows.validate_app(starter.app)
  assert errors == []
  assert warnings == []


def test_opening_turn_does_not_end_the_call(starter):
  _, sm = _opening_turn(starter)
  assert sm["status"] == "in_progress"
  assert not sm["filled"].get("goodbye"), (
      "the terminal announce fired on the opening turn — it needs a `requires=` "
      "gate, or the starter hangs up before asking anything")


def test_opening_turn_speaks_the_welcome_and_the_ask(starter):
  action, _ = _opening_turn(starter)
  spoken = " ".join(p.get("text", "") for p in (action.get("response") or []))
  assert "I can check on an order for you" in spoken, (
      "the welcome text never reached the caller — an announce only delivers its "
      "`texts` verbatim when `preempt=True`")
  assert "order id" in spoken


def test_caller_reply_fills_the_slot(starter):
  _, sm = _opening_turn(starter)
  out = loader.run_engine(starter.flow.to_config(), sm,
                          last_user_text="it's A-1042", config_id="my_agent")
  assert out["sm"]["status"] == "in_progress"


def test_the_task_can_actually_call_its_tool(starter):
  """A task dispatches one keyword argument per input slot, flat.

  The starter shipped a tool taking a single pydantic wrapper
  (`def lookup_order(req: LookupRequest)`), and the emitted executor keeps that
  signature — so the engine's `lookup_order(order_id=...)` could never bind, and
  the one task in the starter agent raised TypeError the first time it fired.
  Neither `validate_app` nor `emit` says anything, because the mismatch is between
  the engine's dispatch convention and a Python signature nothing cross-checks.
  """
  sm = {"filled": {"order_id": "A-1042"}, "pending": {}, "status": "in_progress",
        "task_results": {}}
  out = loader.run_engine(starter.flow.to_config(), sm, last_user_text="",
                          config_id="my_agent")
  call = out["action"].get("function_call") or {}
  assert call.get("name") == "lookup_order", out["action"]

  result = starter.lookup_order(**call.get("args", {}))
  assert result.status_message


def test_emitted_dag_returns_exactly_the_config_given():
  """The dag module must hand back the config it was built from, unchanged.

  It is emitted as a compact JSON string parsed at call time rather than as a Python
  literal, because CES reloads the module on every invocation and a large literal costs
  real milliseconds to compile each time. That is only sound if the round trip is exact,
  so this asserts equality — not merely that the module imports.
  """
  cfg = {
      "bootstrap": {"reset_on_complete": True, "max_retries": 3},
      "slots": {"acct": {"ask": "Account number?", "optional": False, "hint": None}},
      "tasks": {"go": {"say": "unicode survives: café, 5 GHz", "when": "lambda sm: True"}},
      "gate": [],
      "empty_map": {},
  }
  code = scaffold_mod._starter_dag_code("my_agent", cfg)
  ns: dict = {}
  exec(compile(code, "<emitted>", "exec"), ns)  # noqa: S102
  assert ns["my_agent_dag"]() == cfg


def test_emit_refuses_a_config_json_would_alter():
  """A tuple would come back as a list, so the emitter must refuse rather than corrupt.

  This is the failure the round trip cannot survive and the one a reader would never spot
  in a diff: the emitted agent simply runs with a different config.
  """
  with pytest.raises(ValueError, match="not JSON-representable"):
    scaffold_mod._starter_dag_code("my_agent", {"gate": ("a", "b")})


def test_emitted_dag_reimports_statically_not_just_at_runtime():
  """The emitted dag must parse back to its config WITHOUT being executed.

  Running the module is the easy half. The push gates, the Studio agent picker, the UJ
  journey map and both migration bridges never run it — they re-read the source
  statically. When the emitter moved to a JSON constant and only the runtime round trip
  was covered, all of them broke at once: the gates said "No DAG config found in the app
  payload" and the two bridges skipped every config in silence.
  """
  cfg = {
      "bootstrap": {"reset_on_complete": True, "max_retries": 3, "unset": None},
      "slots": [{"name": "acct", "ask": "Account number?", "optional": False}],
      "tasks": [{"name": "go", "tool": "t", "condition": "lambda sm: True"}],
      "gate": [],
      "empty_map": {},
  }
  code = scaffold_mod._starter_dag_code("my_agent", cfg)
  assert config_io.import_from_source(code) == cfg


def test_import_still_reads_the_dict_literal_shape():
  """`export_python` is a separate emitter that still renders a literal, so the reader
  has to understand BOTH shapes — teaching it JSON must not cost it the old form."""
  cfg = {"slots": [{"name": "acct", "ask": "Account number?"}],
         "tasks": [{"name": "go", "tool": "t", "condition": "lambda sm: True"}]}
  conf = config_io.normalize(cfg)
  literal_source = config_io.export_python(conf, "my_agent")
  assert "json.loads" not in literal_source
  assert config_io.import_from_source(literal_source) == config_io.config_to_dict(conf)
