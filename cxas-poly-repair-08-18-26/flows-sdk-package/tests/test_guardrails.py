"""Guardrails: authoring the resource, emitting it, scoping it, and surviving deploy.

`App(guardrails=[...])` has always taken NAMES — references to resources somebody made
in the console, which `flows` could point at but never produce. A name with no resource
behind it is a guardrail that never applies, so the field could silently mean nothing.
These tests cover the half that was missing: building the resource itself.

The behavioural claims encoded here come from ces-probes `101`-`103`, which measured
guardrails live on both models:

  * a session variable interpolates into an `llmPolicy` prompt, and emptying it disables
    the rule (`101`)
  * a `scope="agent"` policy does NOT prevent on `gemini-3.1-flash-live` — the caller
    hears the offending line and THEN the action (`102`)
  * `scope="user"` prevents on both models (`103`)

The second is why `_check_guardrails` warns about `scope="agent"` paired with an action
that only replaces text.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_guardrails.py
"""

from __future__ import annotations

import json
import os

import pytest

import flows
from flows.deploy.prep import merge_live_settings


def _flow(cid: str = "orders") -> flows.Flow:
  f = flows.Flow(cid, root_agent=f"{cid.title()}_Agent")
  f.add(
      flows.user_slot("order_id", "What's your order number?"),
      flows.announce("done", ["All set."], end=True),
  )
  return f


def _app(**kw) -> flows.App:
  return flows.App(root_flow=_flow(), app_display_name="Order Status", **kw)


def _emit(app: flows.App, tmp_path, name: str = "app") -> str:
  out = str(tmp_path / name)
  res = flows.build_app(app, out)
  assert res.ok, res.validation.errors if res.validation else res.error
  return out


def _resource(out: str, stem: str) -> dict:
  with open(os.path.join(out, "guardrails", stem, f"{stem}.json")) as f:
    return json.load(f)


def _app_json(out: str) -> dict:
  with open(os.path.join(out, "app.json")) as f:
    return json.load(f)


# ── The resource each constructor lowers to ─────────────────────────────────


def test_safety_level_sets_every_harm_category():
  body = flows.safety("Safety", level="strict").resource_body()
  settings = body["modelSafety"]["safetySettings"]
  assert {s["category"] for s in settings} == {
      "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT",
      "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_HARASSMENT"}
  assert {s["threshold"] for s in settings} == {"BLOCK_LOW_AND_ABOVE"}


def test_safety_override_tunes_one_category_only():
  body = flows.safety(
      level="balanced",
      overrides={"HARM_CATEGORY_HARASSMENT": "BLOCK_NONE"}).resource_body()
  by_cat = {s["category"]: s["threshold"] for s in body["modelSafety"]["safetySettings"]}
  assert by_cat["HARM_CATEGORY_HARASSMENT"] == "BLOCK_NONE"
  assert by_cat["HARM_CATEGORY_HATE_SPEECH"] == "BLOCK_MEDIUM_AND_ABOVE"


@pytest.mark.parametrize("scope,key", [
    ("user", "bannedContentsInUserInput"),
    ("agent", "bannedContentsInAgentResponse"),
    ("both", "bannedContents"),
])
def test_blocklist_scope_picks_the_side_that_is_matched(scope, key):
  body = flows.blocklist("PII", [r"\d{3}"], match="regex", scope=scope).resource_body()
  assert body["contentFilter"][key] == [r"\d{3}"]
  assert body["contentFilter"]["matchType"] == "REGEXP_MATCH"


def test_policy_defaults_to_user_scope():
  """`scope="user"` is the only scope that PREVENTS on a live model (probe 103), so it
  is what an author gets without asking."""
  body = flows.policy("r", "FLAG x.").resource_body()
  assert body["llmPolicy"]["policyScope"] == "USER_QUERY"
  assert body["llmPolicy"]["maxConversationMessages"] == 1
  assert body["llmPolicy"]["failOpen"] is True


def test_prompt_guard_defaults_to_the_platform_settings():
  assert flows.prompt_guard().resource_body()["llmPromptSecurity"] == {
      "defaultSettings": {}}


def test_prompt_guard_custom_replaces_the_default_screening():
  block = flows.prompt_guard("G", custom="Classify.").resource_body()["llmPromptSecurity"]
  assert block["customPolicy"]["prompt"] == "Classify."
  assert "defaultSettings" not in block


@pytest.mark.parametrize("action,key", [
    (flows.respond("No."), "respondImmediately"),
    (flows.generate("Refuse politely."), "generativeAnswer"),
    (flows.transfer_to("Live Agent"), "transferAgent"),
])
def test_each_action_lowers_to_its_trigger_action_shape(action, key):
  body = flows.policy("r", "FLAG x.", on_trigger=action).resource_body()
  assert key in body["action"]


@pytest.mark.parametrize("g", [
    flows.safety(),
    flows.prompt_guard(),
    flows.blocklist("B", ["x"]),
    flows.policy("r", "FLAG x."),
])
def test_every_guardrail_carries_an_action_even_when_none_was_asked_for(g):
  """CES requires an action, and the failure mode is RUNTIME, not deploy.

  A guardrail with no action imports fine and then makes every turn fail with
  `400 Trigger action type is not supported: ACTION_NOT_SET`. The proto field is
  optional, so neither the emitter nor the proto oracle below catches it — this was
  found by driving a deployed demo, and this test is what stops it coming back.
  `{"generativeAnswer": {}}` is the console's own "generate a response" default.
  """
  assert g.resource_body()["action"] == {"generativeAnswer": {}}


def test_an_explicit_action_wins_over_the_default():
  body = flows.safety(on_trigger=flows.respond("No.")).resource_body()
  assert body["action"] == {"respondImmediately": {"responses": [{"text": "No."}]}}


def test_emitted_resources_are_valid_ces_guardrail_protos():
  """The schema oracle: parse each emitted resource as the real proto.

  Every other assertion here checks the JSON against what this module BELIEVES CES
  wants. This one checks it against CES's own definition, so a renamed or mistyped
  field fails here rather than as a silent no-op on a deployed agent.
  """
  types = pytest.importorskip("google.cloud.ces_v1beta").types
  from google.protobuf.json_format import ParseDict

  for g in [
      flows.safety(),
      flows.prompt_guard(),
      flows.prompt_guard("Custom", custom="Classify."),
      flows.blocklist("PII", [r"\d{3}"], match="regex", scope="agent", diacritics=False),
      flows.blocklist("Words", ["foo"], match="word", scope="user"),
      flows.policy("r", "FLAG x.", scope="agent", window=3, fail_open=False,
                   on_trigger=flows.transfer_to("Live Agent")),
      flows.policy("r2", "FLAG y.", on_trigger=flows.respond("No.")),
      flows.policy("r3", "FLAG z.", on_trigger=flows.generate("Refuse.")),
  ]:
    ParseDict({"name": "projects/p/locations/us/apps/a/guardrails/g",
               **g.resource_body()}, types.Guardrail()._pb)


# ── Emit ────────────────────────────────────────────────────────────────────


def test_emit_writes_the_resource_and_names_it_in_app_json(tmp_path):
  out = _emit(_app(guardrails=[flows.safety("Safety"), flows.prompt_guard()]), tmp_path)
  assert _app_json(out)["guardrails"] == ["Safety", "Prompt Guard"]
  assert _resource(out, "Safety")["displayName"] == "Safety"
  # CES's own on-disk convention: the dir/file stem is the display name, spaces to `_`.
  assert _resource(out, "Prompt_Guard")["displayName"] == "Prompt Guard"


def test_every_resource_gets_a_distinct_uuid(tmp_path):
  out = _emit(_app(guardrails=[flows.safety("A"), flows.safety("B")]), tmp_path)
  assert _resource(out, "A")["name"] != _resource(out, "B")["name"]


def test_the_resource_id_is_stable_across_builds(tmp_path):
  """A fresh id per build makes CES CREATE a second guardrail and orphan the first
  rather than update it — the failure `ScaffoldRequest.app_uuid` exists to avoid. Every
  guardrail pulled from a live app carries a stable id."""
  first = _emit(_app(guardrails=[flows.safety("Safety")]), tmp_path, "one")
  second = _emit(_app(guardrails=[flows.safety("Safety")]), tmp_path, "two")
  assert _resource(first, "Safety")["name"] == _resource(second, "Safety")["name"]


def test_the_resource_id_follows_the_display_name(tmp_path):
  """Keyed on the display name because that IS the identity — app.json and the agent
  JSONs reference a guardrail by name, never by id. Renaming is a new resource."""
  a = _emit(_app(guardrails=[flows.safety("Safety")]), tmp_path, "a")
  b = _emit(_app(guardrails=[flows.safety("Renamed")]), tmp_path, "b")
  assert _resource(a, "Safety")["name"] != _resource(b, "Renamed")["name"]
  # Content changes must NOT move it, or an edit would orphan the deployed resource.
  c = _emit(_app(guardrails=[flows.safety("Safety", level="strict")]), tmp_path, "c")
  assert _resource(a, "Safety")["name"] == _resource(c, "Safety")["name"]


def test_names_colliding_on_the_on_disk_stem_are_rejected():
  """`Card Numbers` and `Card_Numbers` are different display names that emit to the same
  `guardrails/Card_Numbers/` — one would silently overwrite the other."""
  app = _app(guardrails=[flows.safety("Card Numbers"), flows.safety("Card_Numbers")])
  with pytest.raises(ValueError, match="both emit to"):
    flows.validate_app(app)


def test_bare_strings_still_reference_a_resource_we_do_not_emit(tmp_path):
  """The pre-existing meaning of this field has to keep working: a name alone says
  "the target already has this one"."""
  out = _emit(_app(guardrails=["Default Safety Guardrail"]), tmp_path)
  assert _app_json(out)["guardrails"] == ["Default Safety Guardrail"]
  assert not os.path.isdir(os.path.join(out, "guardrails"))


def test_names_and_resources_can_be_mixed(tmp_path):
  out = _emit(_app(guardrails=["Console One", flows.safety("Mine")]), tmp_path)
  assert _app_json(out)["guardrails"] == ["Console One", "Mine"]
  assert os.path.isfile(os.path.join(out, "guardrails", "Mine", "Mine.json"))


def test_an_app_without_guardrails_emits_no_guardrails_dir_and_no_key(tmp_path):
  """Absent stays byte-identical — `None` means "say nothing", which is not `[]`."""
  out = _emit(_app(), tmp_path)
  assert "guardrails" not in _app_json(out)
  assert not os.path.isdir(os.path.join(out, "guardrails"))


def test_empty_list_declares_that_the_app_runs_with_none(tmp_path):
  assert _app_json(_emit(_app(guardrails=[]), tmp_path))["guardrails"] == []


# ── Per-agent scoping ───────────────────────────────────────────────────────


def _multi(**kw):
  billing = flows.Agent(name="Billing", flow=_flow("billing"),
                        guardrails=kw.pop("billing_guardrails", None))
  orders = flows.Agent(name="Orders", flow=_flow("orders"))
  host = flows.HostRouter(name="Front", routes={"billing": billing, "orders": orders},
                          guardrails=kw.pop("host_guardrails", None))
  return flows.App(host=host, agents=[billing, orders],
                   app_display_name="Multi", **kw)


def _agent_json(out: str, name: str) -> dict:
  with open(os.path.join(out, "agents", name, f"{name}.json")) as f:
    return json.load(f)


def test_an_agent_guardrail_is_named_on_that_agent_only(tmp_path):
  gr = flows.policy("billing_only", "FLAG x.")
  out = _emit(_multi(billing_guardrails=[gr]), tmp_path, "multi")
  assert _agent_json(out, "Billing")["guardrails"] == ["billing_only"]
  assert "guardrails" not in _agent_json(out, "Orders")
  # The resource itself is emitted once, app-wide.
  assert _resource(out, "billing_only")["displayName"] == "billing_only"


def test_a_host_guardrail_lands_on_the_host_agent(tmp_path):
  out = _emit(_multi(host_guardrails=[flows.prompt_guard("Router Guard")]),
              tmp_path, "multi")
  assert _agent_json(out, "Front")["guardrails"] == ["Router Guard"]


def test_a_guardrail_on_both_app_and_agent_is_emitted_once(tmp_path):
  gr = flows.safety("Shared")
  out = _emit(_multi(guardrails=[gr], billing_guardrails=[gr]), tmp_path, "multi")
  assert _app_json(out)["guardrails"] == ["Shared"]
  assert _agent_json(out, "Billing")["guardrails"] == ["Shared"]
  assert len(os.listdir(os.path.join(out, "guardrails"))) == 1


def test_two_different_guardrails_under_one_name_is_a_build_error():
  app = _multi(guardrails=[flows.safety("Dup", level="strict")],
               billing_guardrails=[flows.safety("Dup", level="relaxed")])
  with pytest.raises(ValueError, match="two different guardrails"):
    flows.validate_app(app)


# ── Validation ──────────────────────────────────────────────────────────────


def test_transfer_target_must_be_an_agent_in_this_app():
  billing = flows.Agent(name="Billing", flow=_flow("billing"))
  app = flows.App(
      host=flows.HostRouter(name="Front", routes={"billing": billing}),
      agents=[billing], app_display_name="Multi",
      guardrails=[flows.policy("r", "FLAG x.", on_trigger=flows.transfer_to("Nobody"))])
  errors, _ = flows.validate_app(app)
  assert any("is not an agent in this app" in e for e in errors)


def test_transfer_to_accepts_the_agent_object():
  billing = flows.Agent(name="Billing", flow=_flow("billing"))
  app = flows.App(
      host=flows.HostRouter(name="Front", routes={"billing": billing}),
      agents=[billing], app_display_name="Multi",
      guardrails=[flows.policy("r", "FLAG x.", on_trigger=flows.transfer_to(billing))])
  errors, _ = flows.validate_app(app)
  assert not [e for e in errors if "guardrail" in e]


def test_agent_scope_with_a_text_action_warns_about_the_live_model():
  """probe 102: on flash-live the caller hears the offending line, THEN this action."""
  app = _app(guardrails=[
      flows.policy("r", "FLAG x.", scope="agent", on_trigger=flows.respond("No."))])
  _, warnings = flows.validate_app(app)
  assert any("ces-probes 102" in w for w in warnings)


def test_an_agent_scoped_BLOCKLIST_does_not_warn():
  """The warning is about a JUDGED rule, and a filter is not one.

  probe 104: a deterministic `contentFilter` at scope='agent' PREVENTS on both models,
  where an `llmPolicy` at the same scope only detects on flash-live. Warning here would
  steer authors off the one response-side control that works on the live model — which is
  exactly what the first version of this check did.
  """
  app = _app(guardrails=[
      flows.blocklist("B", ["x"], scope="agent", on_trigger=flows.respond("No."))])
  _, warnings = flows.validate_app(app)
  assert not [w for w in warnings if "ces-probes 102" in w]


def test_agent_scope_with_transfer_does_not_warn():
  app = _app(guardrails=[
      flows.policy("r", "FLAG x.", scope="agent",
                   on_trigger=flows.transfer_to("Order Status_agent"))])
  _, warnings = flows.validate_app(app)
  assert not [w for w in warnings if "ces-probes 102" in w]


def test_user_scope_never_warns():
  app = _app(guardrails=[
      flows.policy("r", "FLAG x.", scope="user", on_trigger=flows.respond("No."))])
  _, warnings = flows.validate_app(app)
  assert not [w for w in warnings if "ces-probes 102" in w]


def test_a_name_with_no_resource_behind_it_warns():
  _, warnings = flows.validate_app(_app(guardrails=["Ghost"]))
  assert any("nothing here emits it" in w for w in warnings)


@pytest.mark.parametrize("bad", [
    "Safety",                      # a bare string, not a list
    [""],                          # empty name
    [123],                         # neither a name nor a resource
])
def test_malformed_guardrail_lists_are_rejected_at_construction(bad):
  with pytest.raises(ValueError, match="guardrails"):
    _app(guardrails=bad)


@pytest.mark.parametrize("call,match", [
    (lambda: flows.safety(level="nope"), "level="),
    (lambda: flows.safety(overrides={"NOT_A_CATEGORY": "BLOCK_NONE"}), "harm category"),
    (lambda: flows.safety(overrides={"HARM_CATEGORY_HARASSMENT": "MAYBE"}), "threshold"),
    (lambda: flows.blocklist("B", []), "at least one"),
    (lambda: flows.blocklist("B", ["x"], match="nope"), "match="),
    (lambda: flows.blocklist("B", ["x"], scope="nope"), "scope="),
    (lambda: flows.policy("r", ""), "needs a prompt"),
    (lambda: flows.policy("r", "x", window=0), "positive integer"),
    (lambda: flows.policy("r", "x", scope="nope"), "scope="),
    (lambda: flows.policy("r", "x", on_trigger="respond"), "on_trigger="),
    (lambda: flows.respond(""), "needs the text"),
    (lambda: flows.transfer_to(""), "transfer_to"),
])
def test_constructor_argument_errors_name_the_argument(call, match):
  with pytest.raises(ValueError, match=match):
    call()


# ── Deploy ──────────────────────────────────────────────────────────────────


def _live_app_dir(tmp_path, *, guardrail_names, resource_stems):
  """A pulled copy of a live target carrying its own guardrail resources."""
  root = tmp_path / "live"
  (root).mkdir()
  with open(root / "app.json", "w") as f:
    json.dump({"displayName": "Live", "guardrails": list(guardrail_names)}, f)
  for stem in resource_stems:
    d = root / "guardrails" / stem
    d.mkdir(parents=True)
    with open(d / f"{stem}.json", "w") as f:
      json.dump({"displayName": stem.replace("_", " "), "enabled": True}, f)
  return str(root)


def test_deploy_does_not_clobber_an_emitted_guardrail(tmp_path):
  """The bug this feature would otherwise ship with.

  `prep` rmtree'd the built `guardrails/` and replaced it with the live target's, so an
  SDK-authored guardrail was deleted on the way to the push — silently, because a
  guardrail that is merely absent raises nothing, it just never applies.
  """
  out = _emit(_app(guardrails=[flows.safety("Mine", level="strict")]), tmp_path)
  live = _live_app_dir(tmp_path, guardrail_names=["Theirs"], resource_stems=["Theirs"])
  merge_live_settings(live, out, declared=["guardrails"])
  body = _resource(out, "Mine")
  assert body["modelSafety"]["safetySettings"][0]["threshold"] == "BLOCK_LOW_AND_ABOVE"


def test_deploy_keeps_a_console_authored_guardrail_we_did_not_emit(tmp_path):
  """The other half: the merge must not become a delete for resources we never knew
  about."""
  out = _emit(_app(guardrails=[flows.safety("Mine")]), tmp_path)
  live = _live_app_dir(tmp_path, guardrail_names=["Theirs"], resource_stems=["Theirs"])
  merge_live_settings(live, out, declared=["guardrails"])
  assert os.path.isfile(os.path.join(out, "guardrails", "Theirs", "Theirs.json"))
  assert os.path.isfile(os.path.join(out, "guardrails", "Mine", "Mine.json"))


def test_declared_guardrail_names_still_beat_the_live_targets(tmp_path):
  out = _emit(_app(guardrails=[flows.safety("Mine")]), tmp_path)
  live = _live_app_dir(tmp_path, guardrail_names=["Theirs"], resource_stems=["Theirs"])
  merge_live_settings(live, out, declared=["guardrails"])
  assert _app_json(out)["guardrails"] == ["Mine"]
