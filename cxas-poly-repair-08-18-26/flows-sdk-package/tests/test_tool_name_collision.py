"""Two apps in one process may each define a tool called `lookup_bill`.

Tool names are one namespace for the whole process, but a tool belongs to an APP. The
registry kept a single entry per name, so the second import silently became the first and
an app emitted a body its author never wrote. Nothing reported it: the first sign was a
validator complaining that a tool did not return an output key, naming keys from a
function in a different file.

Two apps sharing a process is not exotic — it is the example suite, a notebook, a
migration run, and any test that builds more than one agent.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_tool_name_collision.py
"""

from __future__ import annotations

import pytest

import flows
from flows.authoring import tools as _tools


@pytest.fixture(autouse=True)
def _isolated_registry():
  """Clear the tool registry either side of every test in this file.

  These tests deliberately register tools with NO `flow=`, which attach to every app in
  the process. Left behind they join the next test's agent, which is how this file first
  broke seven unrelated tests.
  """
  _tools.clear_registry()
  yield
  _tools.clear_registry()


def _billing_app():
  """App one. Its `lookup_bill` returns an amount."""

  @flows.tool(flow="billing", name="lookup_bill")
  def billing_lookup(account_id: str) -> dict:
    """Look up the balance on a billing account."""
    return {"amount_due": "42.00", "success": True}

  f = flows.Flow("billing", root_agent="Billing")
  f.add(flows.user_slot("account_id", ask="Account?"),
        flows.result_slot("amount", "Lookup"))
  f.task("Lookup", "lookup_bill", ["account_id"], "amount", out_key="amount_due")
  return f


def _arrears_app():
  """App two, imported LATER. A different function, the same tool name."""

  @flows.tool(flow="arrears", name="lookup_bill")
  def arrears_lookup(case_id: str) -> dict:
    """Look up the arrears history on a case."""
    return {"months_behind": "3", "success": True}

  f = flows.Flow("arrears", root_agent="Arrears")
  f.add(flows.user_slot("case_id", ask="Case?"),
        flows.result_slot("behind", "Lookup"))
  f.task("Lookup", "lookup_bill", ["case_id"], "behind", out_key="months_behind")
  return f


def test_each_app_gets_its_own_tool_under_a_shared_name():
  """The later import must not replace the earlier app's body."""
  billing, arrears = _billing_app(), _arrears_app()

  billing_body = _tools.collect_tools([billing.config_id])["lookup_bill"]
  arrears_body = _tools.collect_tools([arrears.config_id])["lookup_bill"]

  assert "amount_due" in billing_body and "months_behind" not in billing_body, (
      "the billing app emitted the arrears body under its own tool name")
  assert "months_behind" in arrears_body and "amount_due" not in arrears_body


def test_an_unattached_tool_does_not_displace_an_attached_one():
  """The exact shape that surfaced this, and the one that actually reaches an app.

  `@flows.tool()` with no `flow=` attaches to EVERY flow, so an unrelated module's tool
  of the same name is a candidate for every app in the process. Registered second it took
  the name outright, and the billing app then validated its `amount_due` mapping against
  a function that returns `case_number` — an error naming keys from a file its author
  never opened.
  """
  billing = _billing_app()

  @flows.tool(name="lookup_bill")            # no flow: attaches to everything
  def unrelated_lookup(summary: str) -> dict:
    """Open a support case from a description."""
    return {"case_number": "CS-1000", "success": True}

  errors, _warnings = flows.validate_app(
      flows.App(root_flow=billing, app_display_name="billing",
                agent_instruction="Help with billing."))
  assert errors == [], errors
  assert "amount_due" in _tools.collect_tools([billing.config_id])["lookup_bill"]


def test_a_reimport_of_one_module_is_not_a_collision():
  """The loaders import an example twice under two module names.

  That re-registers the SAME function from the same file. Recording it as a displaced
  spec would make every reloaded tool look like a rival to itself.
  """
  _billing_app()
  _billing_app()
  assert _tools._SHADOWED.get("lookup_bill") in (None, [])


def test_an_unattached_duplicate_keeps_the_last_wins_rule():
  """A tool attached to NO flow attaches to every one, so no app can choose between two.

  Pinned because it is the case the fix deliberately does not touch: nothing about the
  app distinguishes them, and failing the build would break agents that build today.
  """
  @flows.tool(name="shared_helper")
  def first() -> dict:
    """First."""
    return {"who": "first", "success": True}

  @flows.tool(name="shared_helper")
  def second() -> dict:
    """Second."""
    return {"who": "second", "success": True}

  body = _tools.collect_tools(["anything"])["shared_helper"]
  assert '"second"' in body or "'second'" in body


def test_an_unattached_shadowed_tool_still_attaches_to_other_flows():
  """The mirror of the case above, and the one review caught me missing.

  A tool with no `flow=` attaches to every app. Displaced by a flow-specific tool of the
  same name it used to vanish, so an app that matched NEITHER — it is not the arrears
  flow, and the unattached one was gone — resolved no tool at all and shipped a task
  wired to a name with no body.
  """
  billing = _billing_app()

  @flows.tool(name="reference_lookup")           # no flow: attaches to everything
  def everywhere(account_id: str) -> dict:
    """Look something up for any flow."""
    return {"reference": "R-1", "success": True}

  @flows.tool(flow="arrears", name="reference_lookup")   # displaces it
  def arrears_only(case_id: str) -> dict:
    """Look something up for arrears."""
    return {"reference": "R-2", "success": True}

  resolved = _tools.resolve_specs([billing.config_id])
  assert "reference_lookup" in resolved, (
      "the unattached tool was displaced and the billing app lost it entirely")
  assert resolved["reference_lookup"].func is everywhere
  # The flow-specific one still wins for the app it names.
  assert _tools.resolve_specs(["arrears"])["reference_lookup"].func is arrears_only


def test_a_generated_tool_is_resolved_per_app_too():
  """`register_source_tool` has no function to locate, so its body stands in as origin.

  Generated tools (an OpenAPI wrapper, an A2A unwrap reader) are named after the
  operation they call, so two toolsets in one process can collide exactly as two
  decorated tools can — and they used to overwrite each other with nothing recorded.
  """
  _tools.register_source_tool("fetch_record", "def fetch_record():\n  return {'a': 1}\n",
                              flows=["billing"], output_keys=["a"])
  _tools.register_source_tool("fetch_record", "def fetch_record():\n  return {'b': 2}\n",
                              flows=["arrears"], output_keys=["b"])

  assert _tools.resolve_specs(["billing"])["fetch_record"].output_keys == ["a"]
  assert _tools.resolve_specs(["arrears"])["fetch_record"].output_keys == ["b"]


def test_an_identical_generated_body_is_not_a_collision():
  """Re-registering the same generated tool (a rebuild) must not shadow itself."""
  body = "def fetch_record():\n  return {'a': 1}\n"
  _tools.register_source_tool("fetch_record", body, flows=["billing"])
  _tools.register_source_tool("fetch_record", body, flows=["billing"])
  assert _tools._SHADOWED.get("fetch_record") in (None, [])
