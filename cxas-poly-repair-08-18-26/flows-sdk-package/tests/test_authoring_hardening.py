"""Hardening of the authoring layer — the public DSL an author writes against.

Each group here pins a defect that shipped SILENTLY: no exception, no warning, and
in most cases nothing visible until a live call.

  * a multi-line `@tool(...)` decorator emitted an unparseable tool file, and CES
    drops a tool it cannot compile without a word (`tools.py`),
  * the wrap-up setter read "another one" as "no" and hung up on the caller
    (`setters.py`),
  * `has(a) or has(b)` compiled to a permanently-false lambda on a slot named
    `a) or has(b` (`yaml_loader.py`),
  * `hold_and_wait` dropped an offer it was given, or emitted an exhaust path
    leading nowhere (`dsl.py`),
  * a third reprompt was discarded even though the engine can play it (`dsl.py`),
  * the executor stub's `hash()`-derived value changed every process, so an
    offline simulation could not be replayed (`setters.py`).

The generated bodies are EXECUTED rather than pattern-matched, since what matters is
what the deployed tool does with a caller's words. Everything is offline.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_authoring_hardening.py
"""

from __future__ import annotations

import ast
import subprocess
import sys
import warnings

import pytest

import flows
from flows.authoring import dsl
from flows.authoring import setters
from flows.authoring import tools as tools_mod
from flows.engine import loader as fb


HARDENING_FLOW = "authoring_hardening"


@pytest.fixture(autouse=True)
def registry():
  """Restore the tool registry exactly — it is a module global shared by every
  test file, so a tool registered here must not leak into another one."""
  saved = dict(tools_mod._REGISTRY)
  try:
    yield tools_mod._REGISTRY
  finally:
    tools_mod._REGISTRY.clear()
    tools_mod._REGISTRY.update(saved)


def _load(src: str, name: str):
  """Compile a generated body and hand back the named function."""
  namespace: dict = {}
  exec(compile(src, "<generated>", "exec"), namespace)  # noqa: S102
  return namespace[name]


# ===========================================================================
# A multi-line `@tool(...)` decorator must not emit an unparseable tool file
# ===========================================================================

def test_a_multiline_decorator_call_is_stripped_whole(registry):
  """Stripping only the lines that START with `@` left the decorator's argument
  lines at the top of the emitted module — a SyntaxError in the CES sandbox,
  which drops the tool at deploy with no error at all."""
  @flows.tool(
      flow=HARDENING_FLOW,
      name="hardening_multiline",
  )
  def lookup_delivery(tracking_number: str) -> dict:
    """A decorator CALL split over several lines."""
    return {"status_message": "Out for delivery", "success": True}

  src = tools_mod.render_tool(registry["hardening_multiline"])
  compile(src, "<rendered>", "exec")  # the whole emitted file must parse
  assert "@" not in src
  assert "flow=" not in src and "name=" not in src
  assert "def lookup_delivery(tracking_number: str) -> dict:" in src


def test_an_async_tool_with_a_multiline_decorator_also_renders(registry):
  @flows.tool(
      flow=HARDENING_FLOW,
      name="hardening_async_multiline",
  )
  async def lookup_async(tracking_number: str) -> dict:
    """An async tool behind a wrapped decorator."""
    return {"success": True}

  src = tools_mod.render_tool(registry["hardening_async_multiline"])
  compile(src, "<rendered>", "exec")
  assert "async def lookup_async(tracking_number: str) -> dict:" in src
  assert "@" not in src


def test_stacked_decorators_over_several_lines_are_all_stripped(registry):
  def _noop(fn):
    return fn

  @flows.tool(
      flow=HARDENING_FLOW,
      name="hardening_stacked",
  )
  @_noop
  def lookup_stacked(tracking_number: str) -> dict:
    """Two decorators, one of them wrapped."""
    return {"success": True}

  src = tools_mod.render_tool(registry["hardening_stacked"])
  compile(src, "<rendered>", "exec")
  assert "@" not in src and "_noop" not in src


def test_the_author_hook_renderer_strips_it_too():
  """`render_callable` backs the author-customization emitter, so a hook written
  with a wrapped decorator has to come out just as clean."""
  def _noop(fn):
    return fn

  @_noop
  def before_model_hook(
      callback_context,
      llm_request,
  ):
    """An author hook."""
    return None

  body = tools_mod.render_callable(before_model_hook)
  compile(body, "<rendered>", "exec")
  assert body.startswith("def before_model_hook(")


def test_a_body_that_will_not_parse_falls_back_to_the_line_scan(monkeypatch):
  """The `ast` lookup is the primary route; source that cannot be parsed still
  gets the old prefix scan rather than an exception at build time."""
  def target(a: str) -> dict:
    return {}

  monkeypatch.setattr(
      tools_mod.inspect, "getsource",
      lambda _fn: "@tool(\n@still_broken\ndef target(a: str) -> dict:\n  return {\n")
  cleaned = tools_mod._clean_func_source(target)
  assert cleaned.startswith("def target(a: str) -> dict:")


# ===========================================================================
# A `**kwargs` tool deploys with no parameters, and CES drops it
# ===========================================================================

def test_a_var_keyword_tool_warns_that_ces_will_drop_it(registry):
  with pytest.warns(UserWarning, match="silently dropped"):
    @flows.tool(flow=HARDENING_FLOW, name="hardening_kwargs")
    def takes_kwargs(**kwargs) -> dict:
      """CES derives the schema from the signature — this one declares none."""
      return {"success": True}

  assert "hardening_kwargs" in registry  # warned, not rejected


def test_a_var_positional_tool_warns_as_well(registry):
  with pytest.warns(UserWarning, match=r"\*args"):
    @flows.tool(flow=HARDENING_FLOW, name="hardening_varargs")
    def takes_varargs(*args) -> dict:
      """Same problem, spelled the other way."""
      return {"success": True}


def test_a_named_parameter_tool_does_not_warn(registry, recwarn):
  @flows.tool(flow=HARDENING_FLOW, name="hardening_named")
  def takes_named(tracking_number: str = "") -> dict:
    """The shape CES can actually build a schema from."""
    return {"success": True}

  assert [w for w in recwarn if issubclass(w.category, UserWarning)] == []


# ===========================================================================
# The wrap-up setter must not hear "more" as "done"
# ===========================================================================

def _wrap_up():
  return _load(setters.gen_wrap_up_setter("set_wrap_up", "wrap_up"), "set_wrap_up")


@pytest.mark.parametrize("said", [
    "another one",
    "I want to know more",
    "one more thing",
    "notice anything else",
])
def test_the_wrap_up_setter_matches_whole_words_only(said):
  """A phrase that merely CONTAINS a negation token ("a-no-ther", "k-no-w") is a
  caller asking for MORE. The old bare-substring test classified them as DONE,
  which ends the call on someone who was still talking."""
  assert _wrap_up()(said)["value"] == "yes"


@pytest.mark.parametrize("said", [
    "what's the context here", "the texture is wrong", "my messenger app",
])
def test_the_text_branch_matches_whole_words_too(said):
  """The same boundary rule guards the SMS branch: "con-text-", "-text-ure" and
  "message-nger" must not be read as "send me a text"."""
  assert _wrap_up()(said)["value"] == "yes"


@pytest.mark.parametrize("said", [
    "no", "nope", "nah", "not really", "no more", "nothing", "that's all",
    "I'm good", "all set", "  DONE  ", "no, thanks!", "that's it -- bye",
])
def test_a_real_negation_still_reads_as_done(said):
  """Word-boundary matching must not become exact-string matching: punctuation
  and surrounding filler are normalized away before the tokens are compared."""
  assert _wrap_up()(said)["value"] == "no"


def test_a_curly_apostrophe_is_normalized():
  # ASR text routinely carries U+2019 where the phrase list has "'".
  assert _wrap_up()("that’s all")["value"] == "no"
  assert _wrap_up()("I’m good")["value"] == "no"


@pytest.mark.parametrize("said", [
    "text me", "send it", "SMS please", "email me the details",
])
def test_the_text_branch_still_classifies_a_real_request(said):
  assert _wrap_up()(said)["value"] == "text"


def test_text_still_wins_over_done():
  # "no thanks, just text it to me" is an SMS request, not a hang-up.
  assert _wrap_up()("no thanks, just text it to me")["value"] == "text"


def test_the_wrap_up_setter_rejects_empty_and_stays_valid_python():
  src = setters.gen_wrap_up_setter("set_wrap_up", "wrap_up")
  ast.parse(src)
  assert _wrap_up()("") == {"error": True, "error_code": "missing"}
  assert _wrap_up()("   ") == {"error": True, "error_code": "missing"}


# ===========================================================================
# A composite condition must not compile to a permanently-false lambda
# ===========================================================================

def _predicate(expr: str):
  """Compile + eval a condition exactly as the engine does."""
  compiled = flows.compile_condition(expr)
  assert compiled.startswith("lambda f: "), compiled
  return eval(compiled, {"__builtins__": {"bool": bool}})  # noqa: S307


def test_a_composite_of_helpers_compiles_to_a_working_predicate():
  """`has(a) or has(b)` used to match a greedy `(.*)` and compile to
  `lambda f: bool(f.get('a) or has(b'))` — a gate on a slot that does not exist,
  so it was permanently false and the slot never activated. No error, no warning."""
  assert flows.compile_condition("has(a) or has(b)") == (
      "lambda f: (bool(f.get('a'))) or (bool(f.get('b')))")
  predicate = _predicate("has(a) or has(b)")
  assert predicate({"a": 1}) is True
  assert predicate({"b": 2}) is True
  assert predicate({}) is False


def test_composites_support_and_not_and_grouping():
  p = _predicate("has(a) and not has(b)")
  assert p({"a": 1}) is True and p({"a": 1, "b": 1}) is False

  p2 = _predicate("(has(a) or has(b)) and eq(mode, 'real')")
  assert p2({"a": 1, "mode": "real"}) is True
  assert p2({"mode": "real"}) is False
  assert p2({"a": 1, "mode": "mock"}) is False

  assert _predicate("not unset(z)")({"z": 1}) is True


def test_a_single_helper_is_byte_identical_to_the_dsl_builder():
  # The DSL and the flow-file loader must converge on the same Config.
  assert flows.compile_condition("has(x)") == flows.has("x")
  assert flows.compile_condition("unset(x)") == flows.unset("x")
  assert flows.compile_condition("eq(wrap_up, 'no')") == flows.eq("wrap_up", "no")
  assert flows.compile_condition("ne(status, 'sent')") == flows.ne("status", "sent")
  assert flows.compile_condition("has(x)\n") == flows.has("x")


@pytest.mark.parametrize("expr", [
    "has(a) has(b)",                # no operator between the two calls
    "has(a) or something_else(b)",  # not a helper on the right
    "has(a) +",                     # dangling operator
    "eq(a, 1) == True",             # a comparison is not boolean glue
    "has(a')) or has(b)",           # quote-mangled: no matching close paren
])
def test_a_helper_form_that_is_not_a_valid_composite_is_rejected(expr):
  # Silently-false is the one outcome that must not remain: an expression that
  # cannot be compiled names ITSELF in the error.
  with pytest.raises(ValueError, match="looks like a helper form"):
    flows.compile_condition(expr)
  with pytest.raises(ValueError, match=r"and/or/not"):
    flows.compile_condition(expr)


# --- security: composites must not open an injection path -------------------
_TRIPWIRE: list[str] = []


@pytest.mark.parametrize("expr", [
    # a payload appended after a well-formed value
    "eq(x, 'a') or _TRIPWIRE.append('pwned')",
    "ne(x, 'a') or _TRIPWIRE.append('pwned')",
    # a payload smuggled through the SLOT-NAME position by closing the quote early
    "has(x')) or _TRIPWIRE.append('pwned')",
    "unset(x')) or _TRIPWIRE.append('pwned')",
    # a payload as a conditional expression / an operand
    "eq(x, 1) if _TRIPWIRE.append('pwned') else eq(x, 2)",
    "eq(x, [1] + _TRIPWIRE.append('pwned'))",
    # a payload behind escaped quotes, and one inside a helper argument
    'eq(x, "a\\"); _TRIPWIRE.append(\\"pwned")',
    "has(__import__('os').system('echo pwned'))",
    "has(a) or _TRIPWIRE.append('pwned') or has(b)",
])
def test_helper_forms_cannot_inject_executable_code(expr):
  """A payload smuggled into a helper form must never become executable code.

  Two acceptable outcomes: the expression is REJECTED, or it compiles with the
  payload trapped inside a repr-quoted LITERAL. The compiled string is what the
  ENGINE evals, so it is eval'd here the same way and the tripwire must be
  untouched. `bool` is the only builtin a generated lambda needs, so nothing else
  is reachable even if a payload did escape.
  """
  _TRIPWIRE.clear()
  try:
    compiled = flows.compile_condition(expr)
  except ValueError as err:
    assert "looks like a helper form" in str(err)
    assert _TRIPWIRE == []
    return
  assert compiled.startswith("lambda f: "), compiled
  predicate = eval(compiled,  # noqa: S307
                   {"__builtins__": {"bool": bool}, "_TRIPWIRE": _TRIPWIRE})
  predicate({"x": "a"})
  predicate({})
  assert _TRIPWIRE == [], f"payload executed via {expr!r} -> {compiled!r}"


def test_a_composite_only_ever_emits_quoted_literals_and_boolean_glue():
  compiled = flows.compile_condition("eq(x, \"it's ) fine\") or has(y)")
  assert compiled == (
      'lambda f: (f.get(\'x\') == "it\'s ) fine") or (bool(f.get(\'y\')))')
  assert _predicate("eq(x, \"it's ) fine\") or has(y)")({"x": "it's ) fine"})


def test_a_non_helper_string_is_still_passed_through_verbatim():
  """DOCUMENTED and deliberate, and NOT changed by the composite support: a
  string that does not look like a helper form is handed to the engine unchanged
  (it is assumed to be an author-written `lambda f: ...`). Flow files are
  trusted build-time input; this pins that the trust model is what it was."""
  for raw in ("lambda f: f.get('x') and not f.get('y')",
              "lambda f: __import__('os').getcwd()",
              "not f.get('x')",
              "(f.get('a') or f.get('b'))"):
    assert flows.compile_condition(raw) == raw
  # ...and a name that merely STARTS with a helper name is not a helper call.
  assert flows.compile_condition("lambda f: has_more(f)") == "lambda f: has_more(f)"


# ===========================================================================
# `hold_and_wait` must honour its own contract
# ===========================================================================

def test_hold_and_wait_rejects_both_offers():
  # Both used to be accepted, silently dropping offer_component on the floor.
  with pytest.raises(ValueError, match=r"at most ONE of offer_slot"):
    dsl.hold_and_wait(reprompts=["?"], offer_slot="s", offer_component="c")


def test_hold_and_wait_rejects_an_exhaust_path_to_nowhere():
  # No offer AND no say used to emit `{"component": None}` — a ladder that
  # exhausts into nothing at all.
  with pytest.raises(ValueError) as err:
    dsl.hold_and_wait(reprompts=["?"])
  assert "needs somewhere to go" in str(err.value)
  for way_out in ("offer_slot=", "offer_component=", "say="):
    assert way_out in str(err.value)
  # An empty string is not an offer either.
  with pytest.raises(ValueError, match="needs somewhere to go"):
    dsl.hold_and_wait(reprompts=["?"], offer_slot="", offer_component="", say="")


def test_hold_and_wait_say_alone_is_a_complete_exhaust_path():
  """Closing the call gracefully with no escalation offer is a legitimate
  ladder end, so `say` on its own must keep working."""
  policy = dsl.hold_and_wait(reprompts=["?"], say="Okay, I'll let you go for now.")
  assert policy["on_exhaust"] == {"say": "Okay, I'll let you go for now."}
  assert "component" not in policy["on_exhaust"]


def test_hold_and_wait_each_offer_alone_still_builds_what_it_always_did():
  armed = dsl.hold_and_wait(reprompts=["Still there?"], offer_slot="offer_callback")
  assert armed["on_exhaust"] == {"open_slot": "offer_callback"}
  assert armed["hold_reprompts"] == dsl.DEFAULT_SILENT_TICKS
  assert armed["hold_phrases"] == dsl.DEFAULT_HOLD_PHRASES

  descended = dsl.hold_and_wait(reprompts=["?"], offer_component="offer_dag")
  assert descended["on_exhaust"] == {"component": "offer_dag"}


def test_hold_and_wait_inputs_and_outputs_still_ride_the_exhaust():
  policy = dsl.hold_and_wait(
      reprompts=["?"],
      offer_component="offer_dag",
      say="I'll let you go for now.",
      offer_inputs={"acct": "child_acct"},
      offer_outputs={"child_answer": "answer"},
  )
  assert policy["on_exhaust"] == {
      "component": "offer_dag",
      "say": "I'll let you go for now.",
      "inputs": {"acct": "child_acct"},
      "outputs": {"child_answer": "answer"},
  }


# ===========================================================================
# Every reprompt rung is kept — the two-rung cap was never an engine limit
# ===========================================================================

def test_user_slot_keeps_every_reprompt_rung():
  slot = dsl.user_slot("zip", "Zip code?", reprompts=["one", "two", "three"],
                       max_retries=4)
  assert slot["validation"]["reprompts"] == ["one", "two", "three"]


def test_user_slot_warns_when_a_rung_can_never_be_reached():
  """`on_exhaust` fires on attempt `max_retries`, so only `max_retries - 1` rungs
  can play. A longer ladder is KEPT but says so out loud instead of vanishing."""
  with pytest.warns(UserWarning, match=r"3 reprompts but max_retries=3"):
    slot = dsl.user_slot("zip", "Zip code?", reprompts=["one", "two", "three"])
  assert slot["validation"]["reprompts"] == ["one", "two", "three"]


def test_a_reachable_ladder_does_not_warn(recwarn):
  dsl.user_slot("zip", "Zip code?", reprompts=["one", "two"])
  dsl.user_slot("zip", "Zip code?", reprompts=["one"])
  dsl.user_slot("zip", "Zip code?")  # the generated default ladder
  assert [w for w in recwarn if issubclass(w.category, UserWarning)] == []


def test_a_short_ladder_is_still_padded_with_the_generated_rungs():
  one = dsl.user_slot("zip", "Zip code?", reprompts=["Say it again?"])
  assert one["validation"]["reprompts"] == [
      "Say it again?", "One more time. Zip code?"]
  none = dsl.user_slot("zip", "Zip code?", reprompts=[])
  assert none["validation"]["reprompts"] == [
      "Sorry, I didn't catch that. Zip code?", "One more time. Zip code?"]


def test_the_engine_plays_the_third_rung_offline():
  """The proof that the cap was a DSL invention: the engine indexes the ladder by
  the retry count and clamps to the last rung, so rung 3 is heard on attempt 3 and
  `on_exhaust` lands on attempt `max_retries`."""
  with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    flow = flows.Flow("rung_test", root_agent="Rung_Agent")
    flow.add(flows.user_slot("zip", "Zip code?", max_retries=4,
                             reprompts=["rung one", "rung two", "rung three"]))
  config = flow.to_config()

  spoken = []
  state = fb.run_engine(config, {}, config_id="rung_test")["sm"]
  for _attempt in range(4):
    state["_slot_errors"] = [{"slot": "zip", "code": "invalid"}]
    out = fb.run_engine(config, state, last_user_text="nnnn",
                        config_id="rung_test")
    state = out["sm"]
    spoken.append(out["action"]["message"])
  assert spoken == ["rung one", "rung two", "rung three",
                    "I'm still having trouble hearing you."]


# ===========================================================================
# The executor stub must be stable across processes
# ===========================================================================

def test_the_executor_stub_does_not_use_a_salted_hash():
  src = setters.gen_executor("lookup_order", ["order_number"], ["status_msg"])
  called = {n.func.id for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
  assert "hash" not in called, "hash() is PYTHONHASHSEED-salted; use a digest"


def test_the_executor_stub_is_deterministic_within_a_process():
  fn = _load(setters.gen_executor("t", ["a", "b"], ["x"]), "t")
  assert fn("1", "2") == fn("1", "2")
  again = _load(setters.gen_executor("t", ["a", "b"], ["x"]), "t")
  assert again("1", "2") == fn("1", "2")


def test_the_executor_stub_is_deterministic_across_processes():
  """The docstring's promise: repeatable offline simulation. `hash(str)` is
  salted per interpreter, so two runs of the same simulation disagreed."""
  src = setters.gen_executor("t", ["a"], ["x"])
  program = src + '\nprint(t("hello")["x"])\n'
  values = {
      subprocess.run([sys.executable, "-c", program], capture_output=True,
                     text=True, check=True,
                     env={"PYTHONHASHSEED": seed, "PATH": ""}).stdout.strip()
      for seed in ("0", "1", "random")
  }
  assert len(values) == 1, f"stub value varies by interpreter: {values}"
  value = values.pop()
  assert len(value) == 5 and value.isdigit()
  assert value == _load(src, "t")("hello")["x"]


def test_the_executor_stub_still_varies_with_its_inputs():
  fn = _load(setters.gen_executor("t", ["a", "b"], ["x"]), "t")
  assert fn("1", "2")["x"] != fn("2", "1")["x"]
  # The separator keeps the join unambiguous.
  assert fn("a", "bc")["x"] != fn("ab", "c")["x"]


def test_the_executor_stub_returns_every_out_key_plus_success():
  fn = _load(setters.gen_executor("t", ["a"], ["x", "y"]), "t")
  out = fn("v")
  assert set(out) == {"x", "y", "success"}
  assert out["success"] is True and out["x"] == out["y"]


def test_a_parameterless_executor_stub_is_still_stable():
  fn = _load(setters.gen_executor("t", [], ["x"]), "t")
  assert fn()["x"] == fn()["x"]


# ===========================================================================
# Flow-file loading: an error must name what is actually wrong
# ===========================================================================

def test_a_flow_file_setting_both_config_id_and_id_names_both():
  """Popping `id` only when `config_id` was absent left it behind as a policy
  key, and the failure came back as "unknown flow policy key 'id'"."""
  with pytest.raises(ValueError) as err:
    flows.load_flow({"config_id": "a", "id": "b", "slots": []})
  assert "config_id" in str(err.value) and "'b'" in str(err.value)


def test_id_alone_is_still_accepted_as_the_alias():
  assert flows.load_flow({"id": "aliased", "slots": []}).config_id == "aliased"
  assert flows.load_flow({"config_id": "named"}).config_id == "named"


def test_an_empty_flow_file_says_it_is_empty(tmp_path):
  """An empty (or comments-only) file parses to None, and `dict(None)` raised
  `TypeError: 'NoneType' object is not iterable` — naming nothing at all."""
  path = tmp_path / "empty.yaml"
  path.write_text("# just a comment\n", encoding="utf-8")
  with pytest.raises(ValueError, match="flow file is empty"):
    flows.load_flow(str(path))
  # ...and a file whose top level is not a mapping says THAT, rather than failing
  # somewhere further in on a key it never had.
  listed = tmp_path / "listed.yaml"
  listed.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
  with pytest.raises(ValueError, match="must be a mapping"):
    flows.load_flow(str(listed))


@pytest.mark.parametrize("key", ["slots", "tasks"])
def test_a_wrongly_typed_section_names_the_key(key):
  with pytest.raises(ValueError, match=rf"`{key}` must be a list of dicts"):
    flows.load_flow({"config_id": "c", key: {"name": "zip"}})


@pytest.mark.parametrize("key", ["slots", "tasks"])
def test_a_wrongly_typed_entry_names_the_key_and_the_index(key):
  # The bare `dict(item)` this replaces failed with "dictionary update sequence
  # element #0 has length 1", which named neither the section nor the entry.
  with pytest.raises(ValueError, match=rf"`{key}`\[1\] must be a dict"):
    flows.load_flow({"config_id": "c", key: [{"name": "ok"}, "oops"]})


def test_a_null_section_is_still_treated_as_absent():
  # `slots:` with nothing under it is how YAML spells "none of these".
  flow = flows.load_flow({"config_id": "c", "slots": None, "tasks": None})
  assert flow.to_config()["slots"] == [] and flow.to_config()["tasks"] == []


def test_a_condition_in_a_flow_file_is_compiled_per_entry():
  flow = flows.load_flow({
      "config_id": "c",
      "slots": [{"name": "zip", "source": "user", "setter": "set_zip",
                 "condition": "has(account) or has(phone)"}],
  })
  compiled = flow.to_config()["slots"][0]["condition"]
  predicate = eval(compiled, {"__builtins__": {"bool": bool}})  # noqa: S307
  assert predicate({"account": "1"}) is True and predicate({}) is False
