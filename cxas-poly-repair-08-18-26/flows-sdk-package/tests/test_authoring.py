"""Authoring layer: DSL + YAML interop, pydantic @tool emission, end-to-end emit.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests
"""

from __future__ import annotations

import json
import os

import yaml
from pydantic import BaseModel, Field

import flows
from flows.authoring import tools


# --- a native CXAS tool: pydantic input AND output ---------------------------
class ShipmentRequest(BaseModel):
  tracking_number: str = Field(description="Eight-digit tracking number")


class ShipmentStatus(BaseModel):
  status_message: str = Field(description="Human-readable shipment status")
  success: bool = True


@flows.tool(flow="acme_tracking")
def lookup_shipment(req: ShipmentRequest) -> ShipmentStatus:
  """Look up the delivery status for a tracking number."""
  return ShipmentStatus(status_message="Out for delivery")


def _dsl_flow() -> flows.Flow:
  f = flows.Flow(
      "acme_tracking",
      root_agent="Acme_Tracking_Agent",
      bootstrap={"welcome_slot": "welcome"},
  )
  f.add(
      flows.event_slot("ani"),
      flows.announce("welcome", ["Sure, I can help you track that."], shared=True),
      flows.user_slot("tracking_number", "What's your tracking number?"),
      flows.result_slot("shipment_status_msg", "lookup_task"),
      flows.announce(
          "status", ["{shipment_status_msg}"], requires=["shipment_status_msg"]
      ),
      flows.announce(
          "goodbye", ["Thanks for choosing Acme. Have a great day."], end=True
      ),
  )
  f.task(
      "lookup_task",
      "lookup_shipment",
      ["tracking_number"],
      "shipment_status_msg",
      out_key="status_message",
      condition=flows.has("tracking_number"),
  )
  return f


def _app(flow: flows.Flow) -> flows.App:
  return flows.App(
      root_flow=flow,
      app_display_name="Acme Tracking (flows demo)",
      model="gemini-3.5-flash",
      variables=[
          {
              "name": "ACME_API_MODE",
              "description": "Tool data source: mock or real.",
              "schema": {"type": "STRING", "default": "mock"},
          }
      ],
  )


# --- condition compiler: YAML helper form == DSL helper ----------------------
def test_condition_helpers_compile_identically():
  assert flows.compile_condition("has(tracking_number)") == flows.has("tracking_number")
  assert flows.compile_condition("unset(armed)") == flows.unset("armed")
  assert flows.compile_condition("eq(wrap_up, 'no')") == flows.eq("wrap_up", "no")
  assert flows.compile_condition("ne(status, 'delivered')") == flows.ne("status", "delivered")
  # A raw lambda string passes through unchanged.
  raw = "lambda f: f.get('x') and not f.get('y')"
  assert flows.compile_condition(raw) == raw


def test_condition_helpers_handle_quoted_commas():
  # eq/ne arg-splitting must ignore commas inside quoted values (and slot names).
  assert flows.compile_condition('eq(city, "New York, NY")') == flows.eq("city", "New York, NY")
  assert flows.compile_condition('eq("a,b", c)') == flows.eq("a,b", "c")


def test_cli_deploy_registered_and_graceful():
  # `flows deploy` is a real subcommand and fails cleanly (rc=1, no crash) when
  # the cxas CLI isn't on PATH — rather than an argparse "invalid choice".
  from flows.cli import main

  rc = main(["deploy", "--app-dir", "/nonexistent", "--to", "projects/x/apps/y", "--cxas", "definitely-not-a-real-cli"])
  assert rc == 1


# --- YAML <-> DSL interop: same Config either way ----------------------------
def test_yaml_and_dsl_produce_identical_config():
  dsl_cfg = _dsl_flow().to_config()
  # Round-trip the DSL config through YAML and the loader.
  as_yaml = dict(dsl_cfg)
  as_yaml["config_id"] = "acme_tracking"
  as_yaml["root_agent"] = "Acme_Tracking_Agent"
  reloaded = flows.flow_from_dict(yaml.safe_load(yaml.safe_dump(as_yaml)))
  assert reloaded.to_config() == dsl_cfg
  assert reloaded.config_id == "acme_tracking"
  assert reloaded.root_agent == "Acme_Tracking_Agent"


# --- validation is clean -----------------------------------------------------
def test_validate_app_is_clean():
  errors, _warnings = flows.validate_app(_app(_dsl_flow()))
  assert errors == [], errors


# --- a tool that cannot report success is a hang, not a failure --------------
class _NoSuccessStatus(BaseModel):
  """A return model with no `success` field — the shape that wedges a flow."""

  status_message: str = Field(description="Human-readable shipment status")


@flows.tool(flow="no_success")
def lookup_without_success(tracking_number: str) -> _NoSuccessStatus:
  """Look a shipment up, but never report whether the lookup worked."""
  return _NoSuccessStatus(status_message="Out for delivery")


def test_a_tool_that_never_returns_its_success_key_is_refused():
  """Intake decides a task worked from `bool(response[success_check])` and applies its
  outputs only then, so a return model without that field fills NOTHING on any call and
  the flow waits out the rest of the conversation. The blessed validator only checks
  keys named in `outputs`, and the success key is not one of them (except on a verdict
  spine task, where the run-flag deliberately rides it)."""
  f = flows.Flow("no_success", root_agent="A")
  f.add(
      flows.user_slot("tracking_number", "What's your tracking number?"),
      flows.result_slot("status_message", "Lookup"),
      flows.announce("done", ["{status_message}"], requires=["status_message"],
                     end=True),
  )
  f.task("Lookup", "lookup_without_success", ["tracking_number"], "status_message")
  errors, _warnings = flows.validate_app(
      flows.App(root_flow=f, app_display_name="No Success"))
  assert any("checks success on key 'success'" in e for e in errors), errors


@flows.tool(flow="dict_no_success")
def dict_lookup_without_success(tracking_number: str) -> dict:
  """Look a shipment up and answer with a plain dict that omits `success`.

  Args:
    tracking_number: The shipment's tracking number.

  Returns:
    A status dict with no success key.
  """
  return {"status_message": "Out for delivery"}


@flows.tool(flow="dict_computed")
def dict_lookup_computed(tracking_number: str) -> dict:
  """Build the answer key by key, so its shape cannot be read statically.

  Args:
    tracking_number: The shipment's tracking number.

  Returns:
    A computed dict.
  """
  out = {}
  out["status_message"] = "Out for delivery"
  return out


def test_a_plain_dict_tool_that_never_returns_its_success_key_is_refused():
  """The same defect, one type annotation away, and previously invisible.

  The check above only looked at pydantic return models — a plain dict "declares its
  keys nowhere". But a `return {...}` literal declares them exactly as closedly as a
  model does, and skipping it let the failure ship: measured live on ces-probes 86, the
  flow said "An error occurred." on every turn for the rest of the call, because
  `on_failure.max_retries` defaults to 0 and the task looked failed on its first fire.
  """
  f = flows.Flow("dict_no_success", root_agent="A")
  f.add(
      flows.user_slot("tracking_number", "What's your tracking number?"),
      flows.result_slot("status_message", "Lookup"),
      flows.announce("done", ["{status_message}"], requires=["status_message"],
                     end=True),
  )
  f.task("Lookup", "dict_lookup_without_success", ["tracking_number"], "status_message")
  errors, _warnings = flows.validate_app(
      flows.App(root_flow=f, app_display_name="Dict No Success"))
  assert any("checks success on key 'success'" in e for e in errors), errors


def test_a_dict_tool_whose_keys_cannot_be_read_is_left_alone():
  """No false positive. A partial key set would be worse than none — it would refuse a
  tool for omitting a key it does return — so an unreadable body stays unchecked and
  the task's `outputs` mapping carries the contract, as before."""
  assert tools.registered_output_keys()["dict_lookup_computed"] == []


_LEFT_BEHIND = "pending"


def _helper_status() -> str:
  """A module-level helper, which the emitted file now carries."""
  return _LEFT_BEHIND


@flows.tool(flow="free_var")
def reads_a_module_constant(x: str = "") -> dict:
  """Read a module-level constant that the emitted file will not carry.

  Args:
    x: Unused.

  Returns:
    A status dict.
  """
  return {"success": True, "status": _helper_status()}


@flows.tool(flow="free_var")
def reads_only_sandbox_globals(x: str = "") -> dict:
  """Use a CES-injected global and a local, both of which are fine.

  Args:
    x: Unused.

  Returns:
    A status dict.
  """
  try:
    context.state["k"] = "v"  # noqa: F821
  except Exception:
    pass
  here = "pending"
  return {"success": True, "status": here}


_ANNOTATION_ONLY = dict  # module-level, referenced ONLY in a local annotation


@flows.tool(flow="free_var")
def reads_ces_internal_and_a_local_annotation(x: str = "") -> dict:
  """Touch the two shapes that used to be reported and should not be.

  Args:
    x: Unused.

  Returns:
    A status dict.
  """
  try:
    _ = ces_internal  # noqa: F821 - injected, see ces-probes 45
  except Exception:
    pass
  here: _ANNOTATION_ONLY = {"n": 1}
  return {"success": True, "status": str(len(here))}


def test_an_injected_name_and_a_local_annotation_are_not_reported():
  """Two false positives, both of which would refuse a tool that runs perfectly.

  `ces_internal` is in the sandbox namespace ces-probes 45 dumped, alongside the five
  already whitelisted. And a LOCAL variable annotation is never evaluated by Python, so
  the name in it does not have to be carried across — unlike a class-body or argument
  annotation, which are evaluated and so stay in the check.
  """
  assert "reads_ces_internal_and_a_local_annotation" not in (
      tools.registered_unresolved_globals())


def test_a_name_bound_by_a_match_arm_counts_as_bound():
  """`match` binds through the pattern, not a Store-context Name.

  A missed binding does not lose a finding, it INVENTS one — the name is reported as
  left behind when the body defines it — so the walk has to see pattern captures.
  """
  import ast as _ast

  bound = tools._bound_names(_ast.parse(               # noqa: SLF001
      "def f(v):\n"
      "  match v:\n"
      "    case {'k': captured, **rest}:\n"
      "      return captured, rest\n"
      "    case [*starred]:\n"
      "      return starred\n"
      "    case other:\n"
      "      return other\n"))
  assert {"captured", "rest", "starred", "other"} <= bound


def test_a_module_level_helper_is_carried_into_the_emitted_file():
  """A helper defined beside the function is INLINED, dependency-first, so the emitted
  file is self-contained.

  This used to be refused instead. The refusal existed because the tool died on its
  FIRST call with `name 'X' is not defined` — no build error, no syntax error, nothing
  until a caller reached it; cost of not catching it, measured, was one deploy and one
  live drive on ces-probes 86. Carrying the helper removes the failure rather than
  reporting it, and the guard below still covers what carrying cannot fix.
  """
  src = tools.render_tool(tools._REGISTRY["reads_a_module_constant"])  # noqa: SLF001
  assert "def _helper_status()" in src
  assert '_LEFT_BEHIND = "pending"' in src
  # DEPENDENCY FIRST: the constant must be defined above the helper that reads it, or
  # the emitted module raises at import in the sandbox.
  assert src.index('_LEFT_BEHIND = "pending"') < src.index("def _helper_status()")
  assert "reads_a_module_constant" not in tools.registered_unresolved_globals()


def test_sandbox_globals_and_locals_are_not_reported():
  """No false positive. `context`/`tools`/`requests` are injected by CES (ces-probes
  45/46) and a local binding is carried with the function, so neither is missing."""
  assert "reads_only_sandbox_globals" not in tools.registered_unresolved_globals()


# --- end-to-end emit ---------------------------------------------------------
def test_emit_produces_valid_cxas_app(tmp_path):
  out = str(tmp_path / "app")
  res = flows.build_app(_app(_dsl_flow()), out)
  assert res.ok, res.validation.errors if res.validation else res.error

  appj = json.loads(open(os.path.join(out, "app.json")).read())
  var_names = {v["name"] for v in appj["variableDeclarations"]}
  # Framework state auto-injected + the author's business var, without hand-listing.
  assert {"sm", "slot_filling_protocol", "system_directive"} <= var_names
  assert "ACME_API_MODE" in var_names
  assert appj["modelSettings"]["model"] == "gemini-3.5-flash"

  aj = json.loads(
      open(os.path.join(out, "agents", "Acme_Tracking_Agent",
                        "Acme_Tracking_Agent.json")).read()
  )
  tools = set(aj["tools"])
  # Scoped to this flow: the dag + engine + intake + the authored tool + setter.
  assert {"slot_filling_engine", "slot_intake", "acme_tracking_dag",
          "lookup_shipment", "set_tracking_number", "end_session"} <= tools
  # The "tools everywhere" routing killers are NOT scoped in for a plain flow.
  assert "set_active_flow" not in tools
  assert "classify_turn_intent" not in tools

  # The pydantic tool is emitted self-contained with derived output keys.
  tool_src = open(
      os.path.join(out, "tools", "lookup_shipment", "python_function",
                   "python_code.py")).read()
  assert "class ShipmentRequest(BaseModel)" in tool_src
  assert "class ShipmentStatus(BaseModel)" in tool_src
  assert "_DECLARED_OUTPUTS" in tool_src
  assert "status_message" in tool_src

  # The dag tool + auto-generated setter are present.
  assert os.path.isfile(
      os.path.join(out, "tools", "acme_tracking_dag", "python_function",
                   "python_code.py"))
  assert os.path.isfile(
      os.path.join(out, "tools", "set_tracking_number", "python_function",
                   "python_code.py"))


# --- Pass-A intent classifier: route_cues render as caller-phrasing hints -----
# `bootstrap.intent_first` runs a dedicated classification pass whose SI lists the
# switch:/new_request: labels. When the config carries `route_cues` (keyword hints
# per flow), the classifier renders them as a trailing parenthetical so the model
# disambiguates by what the caller SAYS — not just the bare flow name. The label
# itself stays verbatim (the value the model must emit), and a flow with no cues
# renders byte-identically to the pre-feature output (a strict no-op).
def _classifier_lines(cfg):
  from flows.engine import loader
  eng = loader.load_engine()
  si = eng._build_classifier_suffix({}, cfg)
  return [ln for ln in si.splitlines()
          if ln.lstrip().startswith("- switch:")
          or ln.lstrip().startswith("- new_request:")]


def test_route_cues_render_as_classifier_hints():
  cfg = {"flow_types": ["place_freeze", "lift_freeze"],
         "route_cues": {"place_freeze": ["place", "freeze it"],
                        "lift_freeze": ["lift", "unfreeze"]}}
  lines = _classifier_lines(cfg)
  # the LABEL is unchanged (emit-value intact) and the cues trail as a hint.
  assert any(ln.strip().startswith("- switch:place_freeze")
             and "caller might say" in ln and "freeze it" in ln for ln in lines)
  assert any(ln.strip().startswith("- new_request:lift_freeze")
             and "unfreeze" in ln for ln in lines)


def test_no_route_cues_is_byte_identical_noop():
  base = {"flow_types": ["place_freeze", "lift_freeze"]}
  no_cues = _classifier_lines(base)
  empty_cues = _classifier_lines({**base, "route_cues": {}})
  # neither the absent-key nor the empty-dict case adds any annotation.
  assert no_cues == empty_cues
  assert all("caller might say" not in ln for ln in no_cues)
  assert any(ln.strip() == "- switch:place_freeze" for ln in no_cues)


def test_flow_descriptions_render_in_classifier_labels():
  # The same by-meaning descriptions the router turn uses also annotate the mid-flow
  # switch/new_request labels, so a switch is judged by what each flow is FOR.
  cfg = {"flow_types": ["billing", "sales"],
         "flow_descriptions": {"billing": "dispute a charge on the bill",
                               "sales": "upgrade or add a plan"}}
  lines = _classifier_lines(cfg)
  assert any(ln.strip().startswith("- switch:billing")
             and "dispute a charge on the bill" in ln for ln in lines)
  assert any(ln.strip().startswith("- new_request:sales")
             and "upgrade or add a plan" in ln for ln in lines)


def test_flow_descriptions_absent_is_byte_identical():
  base = {"flow_types": ["billing", "sales"]}
  assert _classifier_lines(base) == _classifier_lines({**base, "flow_descriptions": {}})


def test_classifier_si_is_domain_neutral():
  # The mid-flow classifier template used to bake in restaurant wording (guest / dietary /
  # party-date / dish). It must be domain-neutral now — it runs for repair, billing, etc.
  from flows.engine import loader
  si = loader.load_engine()._build_classifier_suffix({}, {"flow_types": ["a", "b"]}).lower()
  for word in ("guest", "dietary", "seating", "party/date", "dish", "confirmation\n  number"):
    assert word not in si, word
  assert "caller" in si


def test_route_cues_only_annotate_their_own_flow():
  # a cue set for one flow must not leak onto the other flow's label.
  cfg = {"flow_types": ["place_freeze", "lift_freeze"],
         "route_cues": {"place_freeze": ["place"]}}
  lines = _classifier_lines(cfg)
  assert any(ln.strip().startswith("- switch:place_freeze") and "place" in ln
             for ln in lines)
  assert any(ln.strip() == "- switch:lift_freeze" for ln in lines)  # untouched
