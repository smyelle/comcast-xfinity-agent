"""Hardening pins for the small blessed CONTROL tools in the framework bundle.

These are the levers the model is handed to steer a live conversation:
`confirm_pending`, `reject_pending`, `new_flow_instance`, `resume_flow`,
`set_slot_change`, `classify_turn_intent`. Each lives at
`src/flows/engine/framework/tools/<tool>/python_function/python_code.py` and is
NOT importable as a normal module (CES injects globals at runtime), so every
test loads it BY PATH through `flows.engine.loader`.

Scope: the defects a sweep of the bundle turned up — a consent bug where a
readback "yes" committed a terminal control slot, a KeyError mid-commit, an
example app's flow names baked into code that ships verbatim to every agent,
missing argument validation that reached the caller as a raw traceback, and
demo vocabulary on the MODEL-VISIBLE surfaces. The engine-side consequences
these tools trigger belong to other suites.

Everything runs fully offline: no LLM, no creds, no network.

State hazard: the loader keeps a process-global module cache and binds a
`context` shim onto the cached readback modules, so the autouse fixture drops
the cache after every test.
"""

from __future__ import annotations

import inspect
import json
import typing

import pytest

import flows  # noqa: F401  the package must import before tools load by path
from flows.engine import loader as fb


# The packaged blessed bundle, unaffected by the env/settings override other
# suites in this tree set globally.
ROOT = fb.default_framework_root()

# The tools this file owns.
TOOLS = [
    "confirm_pending",
    "reject_pending",
    "new_flow_instance",
    "resume_flow",
    "set_slot_change",
    "classify_turn_intent",
]

# The passive terminal control slots: staged into `pending` by cancel_flow /
# transfer_to_human, committed ONLY by the engine's terminal gate.
CONTROL_SLOTS = ["cancel", "escalate"]


@pytest.fixture(autouse=True)
def _drop_module_cache():
  yield
  fb.clear_cache()


def _call(tool: str, *args, **kwargs):
  return fb.load_tool_callable(tool, str(ROOT))(*args, **kwargs)


def _readback(tool: str, sm: dict):
  return fb.call_readback_tool(tool, sm, str(ROOT))


def _spec(tool: str) -> dict:
  with open(ROOT / tool / f"{tool}.json", "r", encoding="utf-8") as f:
    return json.load(f)


def _source(tool: str) -> str:
  path = ROOT / tool / "python_function" / "python_code.py"
  return path.read_text(encoding="utf-8")


# --- Signature contract -----------------------------------------------------


@pytest.mark.parametrize("tool", TOOLS)
def test_tool_declares_no_var_args_or_kwargs(tool):
  """A tool taking `**kwargs` (or `*args`) is silently DROPPED at deploy."""
  sig = inspect.signature(fb.load_tool_callable(tool, str(ROOT)))
  bad = [p.name for p in sig.parameters.values()
         if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL)]
  assert not bad, f"{tool} declares var-args {bad}; CES would drop the tool"


@pytest.mark.parametrize("tool", TOOLS)
def test_spec_wiring_names_the_function_the_python_file_defines(tool):
  """CES derives the deployed schema from the signature the spec points at."""
  spec = _spec(tool)["pythonFunction"]
  assert spec["name"] == tool
  assert spec["pythonCode"] == f"tools/{tool}/python_function/python_code.py"
  assert (spec.get("description") or "").strip()


# --- confirm_pending: the terminal-control consent gate (D2) ----------------


def test_confirm_pending_control_slot_list_matches_the_engine():
  """The tool duplicates `_CONTROL_BLOCKS` (CES tools cannot import each other)."""
  tool = fb.load_dag("confirm_pending", str(ROOT))
  engine = fb.load_dag("slot_filling_engine", str(ROOT))
  assert list(tool._CONTROL_BLOCKS) == list(engine._CONTROL_BLOCKS)  # noqa: SLF001
  assert list(tool._CONTROL_BLOCKS) == CONTROL_SLOTS  # noqa: SLF001


@pytest.mark.parametrize("block", CONTROL_SLOTS)
def test_confirm_pending_does_not_commit_a_terminal_control_slot(block):
  """A readback "yes" is NOT consent to cancel/escalate.

  `cancel` / `escalate` sit in the same `pending` dict as ordinary data. A blind
  `filled.update(pending)` moved them into `filled`, where the engine's
  `_handle_terminal_slots` short-circuits (`if block in filled: terminate`) and
  the `requires_readback` "shall I go ahead?" turn is never reached — an
  irreversible teardown on a turn the user only confirmed their details on.
  The request must stay in `pending` so the engine's own gate runs next pass.
  """
  sm = {"filled": {}, "pending": {"amount": 4, block: True}}
  assert _readback("confirm_pending", sm) == {
      "committed": ["amount"], "stored": True,
  }
  assert sm["filled"] == {"amount": 4}
  assert sm["pending"] == {block: True}
  assert sm["_readback_transition"] is True


def test_confirm_pending_holds_back_every_control_slot_at_once():
  sm = {"filled": {}, "pending": {"a": 1, "cancel": True, "escalate": True}}
  assert _readback("confirm_pending", sm)["committed"] == ["a"]
  assert sm["pending"] == {"cancel": True, "escalate": True}


@pytest.mark.parametrize("block", CONTROL_SLOTS)
def test_confirm_pending_errors_when_only_a_control_slot_is_pending(block):
  """Nothing ordinary to commit -> the empty-pending error, sm untouched."""
  sm = {"filled": {}, "pending": {block: True}}
  assert _readback("confirm_pending", sm) == {"error": True}
  assert sm["pending"] == {block: True}
  assert sm["filled"] == {}
  assert "_readback_transition" not in sm


def test_confirm_pending_still_commits_ordinary_pending_values():
  sm = {"filled": {"a": 1}, "pending": {"b": 2, "c": 3}}
  assert _readback("confirm_pending", sm) == {
      "committed": ["b", "c"], "stored": True,
  }
  assert sm["filled"] == {"a": 1, "b": 2, "c": 3}
  assert sm["pending"] == {}
  assert sm["_readback_transition"] is True


def test_confirm_pending_errors_when_nothing_is_pending():
  sm = {"filled": {"a": 1}, "pending": {}}
  assert _readback("confirm_pending", sm) == {"error": True}
  assert "_readback_transition" not in sm
  assert _readback("confirm_pending", {}) == {"error": True}


# --- confirm_pending: KeyError mid-commit (D12) -----------------------------


def test_confirm_pending_seeds_filled_when_the_sm_has_no_filled_key():
  """`filled` used to be indexed directly, crashing mid-commit with KeyError.

  An sm can reach readback without a seeded `filled` map; the commit must land
  rather than raise a traceback out of the tool (CES turns that into a failed
  turn the caller hears as the platform error line).
  """
  sm = {"pending": {"b": 2}}
  assert _readback("confirm_pending", sm) == {
      "committed": ["b"], "stored": True,
  }
  assert sm["filled"] == {"b": 2}
  assert sm["pending"] == {}


def test_confirm_pending_tolerates_a_none_pending_value():
  assert _readback("confirm_pending", {"pending": None}) == {"error": True}


# --- new_flow_instance: no baked-in flow names (D13) ------------------------


def test_new_flow_instance_carries_no_baked_in_flow_names():
  """FRAMEWORK code ships verbatim to every agent, so it cannot name flows.

  It used to carry `_VALID_FLOWS = {...}` — an allow-list from the example app —
  which made the tool answer `invalid_flow` for every flow of every other agent.
  The valid set is agent-specific and unknown here; the engine's gate is what
  rejects an unknown id.
  """
  assert "_VALID_FLOWS" not in _source("new_flow_instance")
  assert not hasattr(fb.load_dag("new_flow_instance", str(ROOT)), "_VALID_FLOWS")


@pytest.mark.parametrize(
    "flow", ["billing", "order_status", "appointment", "claim"]
)
def test_new_flow_instance_accepts_any_agents_flow_name(flow):
  """Any non-empty id is captured; existence is the engine gate's call."""
  assert _call("new_flow_instance", flow=flow) == {
      "stored": True, "value": flow, "new_instance": True,
  }


@pytest.mark.parametrize("bad", ["", "   ", None, 0, False])
def test_new_flow_instance_rejects_only_a_missing_or_blank_flow_id(bad):
  """`None` must not become the literal label "none"."""
  assert _call("new_flow_instance", flow=bad) == {
      "error": True, "error_code": "invalid_flow",
  }


def test_new_flow_instance_normalizes_case_and_whitespace():
  assert _call("new_flow_instance", flow="  FLOW_A  ")["value"] == "flow_a"


def test_new_flow_instance_error_shape_omits_the_success_keys():
  bad = _call("new_flow_instance", flow="   ")
  assert set(bad) == {"error", "error_code"}


# --- resume_flow: argument validation (D40) ---------------------------------


def test_resume_flow_with_no_arguments_is_the_bare_resume_request():
  assert _call("resume_flow") == {"resume_request": True}


@pytest.mark.parametrize("raw,expected", [("7", 7), (" 7 ", 7), (7.0, 7), (7, 7)])
def test_resume_flow_coerces_the_instance_id_to_int(raw, expected):
  """The resolver matches ids with `==` against real ints, so "7" never matched."""
  result = _call("resume_flow", instance_id=raw)
  assert result["instance_id"] == expected
  assert isinstance(result["instance_id"], int)


@pytest.mark.parametrize("bad", ["seven", "", "  ", None, "1.5"])
def test_resume_flow_handles_an_unusable_instance_id(bad):
  """Uncoercible -> the tool's error dict; blank/absent -> simply not supplied."""
  result = _call("resume_flow", instance_id=bad)
  if result.get("error"):
    assert result == {"error": True, "error_code": "invalid_instance_id"}
  else:
    assert result == {"resume_request": True}


def test_resume_flow_rejects_a_negative_instance_id():
  assert _call("resume_flow", instance_id=-1) == {
      "error": True, "error_code": "invalid_instance_id",
  }


@pytest.mark.parametrize("field", ["flow", "slot_name", "slot_value"])
@pytest.mark.parametrize("empty", [None, "", "   ", 0])
def test_resume_flow_drops_an_empty_text_argument(field, empty):
  assert _call("resume_flow", **{field: empty}) == {"resume_request": True}


@pytest.mark.parametrize("field", ["flow", "slot_name", "slot_value"])
def test_resume_flow_rejects_a_container_argument(field):
  """`str(["a"])` would capture the literal "['a']" as an identifier."""
  assert _call("resume_flow", **{field: ["a"]}) == {
      "error": True, "error_code": f"invalid_{field}",
  }


def test_resume_flow_trims_and_stringifies_the_text_arguments():
  assert _call("resume_flow", flow="  flow_a  ", slot_name=" s ",
               slot_value=4) == {
      "resume_request": True, "flow": "flow_a", "slot_name": "s",
      "slot_value": "4",
  }


def test_resume_flow_error_shape_omits_the_request_key():
  bad = _call("resume_flow", instance_id="seven")
  assert set(bad) == {"error", "error_code"}


# --- set_slot_change: error handling (D39) ----------------------------------


def test_set_slot_change_accepts_names_as_a_list_tuple_or_joined_string():
  for given in (["a", "b"], ("a", "b"), "a, b", "a,,  ,b,"):
    assert _call("set_slot_change", slots=given) == {
        "success": True, "slots": ["a", "b"],
    }


@pytest.mark.parametrize("empty", [None, [], (), "", "  ", ",", [""], ["   "]])
def test_set_slot_change_reports_missing_slots_for_empty_input(empty):
  assert _call("set_slot_change", slots=empty) == {
      "error": True, "error_code": "missing_slots",
  }


@pytest.mark.parametrize("bad", [5, 4.2, True, object()])
def test_set_slot_change_reports_invalid_slots_for_a_non_list_value(bad):
  """Wrong type in -> the tool's error dict; a raw TypeError would fail the turn."""
  assert _call("set_slot_change", slots=bad) == {
      "error": True, "error_code": "invalid_slots",
  }


@pytest.mark.parametrize("bad", [[123], ["a", 123], [None], [["a"]]])
def test_set_slot_change_reports_invalid_slots_for_a_list_of_non_strings(bad):
  """`[123]` used to reach `.strip()` and raise AttributeError."""
  assert _call("set_slot_change", slots=bad) == {
      "error": True, "error_code": "invalid_slots",
  }


def test_set_slot_change_rejects_a_dict():
  """A dict must not be silently reduced to its KEYS — that invents intent."""
  assert _call("set_slot_change", slots={"a": 1}) == {
      "error": True, "error_code": "invalid_slots",
  }


def test_set_slot_change_success_and_error_shapes_are_disjoint():
  ok = _call("set_slot_change", slots=["a"])
  assert set(ok) == {"success", "slots"}
  assert set(_call("set_slot_change", slots=[])) == {"error", "error_code"}
  assert set(_call("set_slot_change", slots=5)) == {"error", "error_code"}


# --- Demo vocabulary on the MODEL-VISIBLE surfaces (D50) --------------------

# Nouns from the example app that must never ship in the shared bundle.
_DEMO_WORDS = ("reservation", "takeout", "restaurant", "guest", "diner",
               "pizza", "topping", "bella", "notte")


def _field_descriptions(tool: str) -> list[str]:
  """Every pydantic `Field(description=...)` on the tool's parameters.

  That text is generated straight into the function-calling schema, so it is
  as model-visible as the spec JSON.
  """
  fn = fb.load_tool_callable(tool, str(ROOT))
  out = []
  for annotation in typing.get_type_hints(fn, include_extras=True).values():
    for meta in getattr(annotation, "__metadata__", ()):
      description = getattr(meta, "description", None)
      if description:
        out.append(description)
  return out


@pytest.mark.parametrize("tool", TOOLS)
def test_no_tool_spec_advertises_demo_vocabulary_to_the_model(tool):
  """Tool specs are MODEL-VISIBLE, so demo nouns there steer a live call.

  Wrong-domain instruction for every agent that is not the example app.
  Internal comments are exempt — they are not shipped to the model.
  """
  lowered = _spec(tool)["pythonFunction"]["description"].lower()
  assert not [w for w in _DEMO_WORDS if w in lowered], (
      f"demo vocabulary in the {tool} spec description")


@pytest.mark.parametrize("tool", TOOLS)
def test_no_tool_field_description_advertises_demo_vocabulary(tool):
  """`classify_turn_intent`'s `intent` param used to name "the guest"."""
  for description in _field_descriptions(tool):
    lowered = description.lower()
    assert not [w for w in _DEMO_WORDS if w in lowered], (
        f"demo vocabulary in a {tool} field description: {description!r}")


def test_classify_turn_intent_field_description_survives_the_rename():
  """The rename must not have dropped the instruction the classifier needs."""
  descriptions = _field_descriptions("classify_turn_intent")
  assert len(descriptions) == 1
  assert "user's latest message" in descriptions[0]
  assert "verbatim" in descriptions[0]


def test_classify_turn_intent_spec_still_documents_the_taxonomy():
  description = _spec("classify_turn_intent")["pythonFunction"]["description"]
  for token in ("continue", "switch:", "new_request:", "resume:", "correct:",
                "cancel", "escalate"):
    assert token in description
