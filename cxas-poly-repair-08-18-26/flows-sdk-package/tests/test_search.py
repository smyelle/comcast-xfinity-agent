"""Google Search grounding — declaration, emission, scoping and the intake guard.

The behavioural facts these assert against are live-probe results (ces-probes 24-28), not
inferences from the protos: a body-less tool dir deploys and fires, the engine can fire it
too, and its response is `{search_query, snippets, instructions}` with no `success` key.
"""

import json

import pytest

import flows
from flows.authoring import build as _build


def _app(**kw):
  f = flows.Flow("faq", root_agent="Helper")
  f.add(flows.user_slot("question", "What would you like to know?"))
  return flows.App(root_flow=f, app_display_name="Search Test", **kw)


def _files(app, tmp_path):
  out = str(tmp_path / "app")
  flows.build_app(app, out, overwrite=True)
  return out


def test_payload_carries_every_declared_field():
  tool = flows.search_tool(
      "web", "Search the web.",
      context_urls=["https://example.com/a"],
      preferred_domains=["example.com"],
      exclude_domains=["spam.example"],
      text_prompt="Be brief.",
      voice_prompt="One sentence.",
  )
  assert tool.tool_payload() == {
      "name": "web",
      "description": "Search the web.",
      "contextUrls": ["https://example.com/a"],
      "preferredDomains": ["example.com"],
      "excludeDomains": ["spam.example"],
      "promptConfig": {"textPrompt": "Be brief.", "voicePrompt": "One sentence."},
  }


def test_payload_omits_what_was_not_declared():
  """An empty list is not the same as an unset field — CES round-trips the difference."""
  assert flows.search_tool("web", "Search.").tool_payload() == {
      "name": "web", "description": "Search.",
  }


@pytest.mark.parametrize("kwargs,message", [
    ({"name": "", "description": "d"}, "name is required"),
    ({"name": "web search", "description": "d"}, "letters, digits"),
    ({"name": "web", "description": ""}, "description is required"),
])
def test_declaration_errors(kwargs, message):
  with pytest.raises(ValueError, match=message):
    flows.search_tool(**kwargs)


def test_domain_caps_are_enforced():
  with pytest.raises(ValueError, match="at most 20"):
    flows.search_tool("web", "d", preferred_domains=[f"d{i}.com" for i in range(21)])


def test_emits_a_bodyless_tool_dir(tmp_path):
  """The whole premise: JSON only, no python_function dir (ces-probes 24)."""
  out = _files(_app(search_tools=[flows.search_tool("web", "Search.")]), tmp_path)
  with open(f"{out}/tools/web/web.json") as fh:
    doc = json.load(fh)
  assert doc["googleSearchTool"]["name"] == "web"
  assert "pythonFunction" not in doc
  import os
  assert not os.path.exists(f"{out}/tools/web/python_function")


def test_no_python_stub_is_generated_for_it(tmp_path):
  """A body-less tool must not collect a generated executor stub under its own name —
  the stub would answer in the search tool's place, and only a live call would show it."""
  out = _files(_app(search_tools=[flows.search_tool("web", "Search.")]), tmp_path)
  import os
  assert os.listdir(f"{out}/tools/web") == ["web.json"]


def test_scoped_onto_the_agent(tmp_path):
  out = _files(_app(search_tools=[flows.search_tool("web", "Search.")]), tmp_path)
  with open(f"{out}/agents/Helper/Helper.json") as fh:
    assert "web" in json.load(fh)["tools"]


def test_validate_accepts_a_declared_search_tool():
  errors, _ = flows.validate_app(_app(search_tools=[flows.search_tool("web", "S.")]))
  assert errors == []


def test_two_different_tools_under_one_name_is_an_error():
  app = _app(search_tools=[flows.search_tool("web", "One."),
                           flows.search_tool("web", "Two.")])
  with pytest.raises(ValueError, match="two different search tools"):
    _build._search_tools(app)


def test_wrong_type_is_rejected():
  with pytest.raises(ValueError, match="flows.search_tool"):
    _build._search_tools(_app(search_tools=["web"]))


# --- firing a search from an ordinary task ------------------------------------------


def _task_app(**task_kw):
  tool = flows.search_tool("web", "Search.")
  f = flows.Flow("faq", root_agent="Helper")
  f.add(flows.user_slot("question", "What would you like to know?"),
        flows.result_slot("findings", "lookup"))
  f.task("lookup", tool, ["question"], "findings",
         then_directive="Answer from the results.", **task_kw)
  return flows.App(root_flow=f, app_display_name="Task Test"), f


def test_task_maps_the_slot_onto_the_platform_param():
  """The tool's parameter is `query`, not the slot's name — the author should not have
  to know that."""
  _, f = _task_app()
  assert f.to_config()["tasks"][0]["inputs"] == {"question": "query"}


def test_task_succeeds_on_snippets_not_success():
  """A search response has no `success` key; `snippets` is empty exactly when the search
  found nothing, so it is both the output and the honest success check."""
  _, f = _task_app()
  t = f.to_config()["tasks"][0]
  assert t["success_check"] == "snippets"
  assert t["outputs"] == {"snippets": "findings"}


def test_task_declares_its_tool_without_repeating_it():
  """Passing the object to `task()` is enough — it need not also be on the App."""
  app, _ = _task_app()
  assert {t.name for t in _build._search_tools(app)} == {"web"}


def test_task_tool_reference_never_reaches_config():
  _, f = _task_app()
  for task in f.to_config()["tasks"]:
    assert "_search_tool" not in task


def test_task_validates_clean():
  app, _ = _task_app()
  errors, _ = flows.validate_app(app)
  assert errors == []


def test_explicit_dict_inputs_are_respected():
  """An author who writes the mapping themselves is not overridden."""
  tool = flows.search_tool("web", "Search.")
  f = flows.Flow("faq", root_agent="Helper")
  f.add(flows.user_slot("q", "Ask?"), flows.result_slot("findings", "lookup"))
  f.task("lookup", tool, {"q": "query"}, "findings", then_directive="Answer.")
  assert f.to_config()["tasks"][0]["inputs"] == {"q": "query"}


def test_multiple_inputs_are_rejected():
  """A search takes exactly one `query`; silently dropping the rest would search on
  whichever slot happened to be first."""
  tool = flows.search_tool("web", "Search.")
  f = flows.Flow("faq", root_agent="Helper")
  f.add(flows.user_slot("q", "Ask?"), flows.user_slot("where", "Where?"),
        flows.result_slot("findings", "lookup"))
  with pytest.raises(ValueError, match="exactly one"):
    f.task("lookup", tool, ["q", "where"], "findings")


# --- the intake guard ------------------------------------------------------------------


def test_hand_rolled_task_with_default_success_check_is_rejected():
  """Intake reads `success = bool(response_data.get(success_check))`, so the default
  `"success"` reads every search as failed and escalates on the first fire."""
  tool = flows.search_tool("web", "Search.")
  f = flows.Flow("faq", root_agent="Helper")
  f.add(flows.user_slot("question", "Ask?"), flows.result_slot("out", "hunt"))
  f.task("hunt", "web", {"question": "query"}, "out", out_key="snippets")
  errors, _ = flows.validate_app(
      flows.App(root_flow=f, app_display_name="Bad", search_tools=[tool]))
  assert any("success_check" in e and "OBJECT as the task's tool" in e for e in errors)


def test_nested_output_key_is_rejected():
  """Intake maps by FLAT top-level key, so a snippet's `text` maps nothing."""
  tool = flows.search_tool("web", "Search.")
  f = flows.Flow("faq", root_agent="Helper")
  f.add(flows.user_slot("question", "Ask?"), flows.result_slot("out", "hunt"))
  f.task("hunt", "web", {"question": "query"}, "out",
         out_key="text", success_check="snippets")
  errors, _ = flows.validate_app(
      flows.App(root_flow=f, app_display_name="Bad", search_tools=[tool]))
  assert any("flat top-level key" in e for e in errors)
