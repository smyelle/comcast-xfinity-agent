"""Build-time automatic fillers — detection gates, skip rules, and the marker strip.

Every gate is tested BOTH ways: a case that hoists and a near-miss that must not, so
a gate that stops working fails a test rather than silently widening what the pass
will move in front of a tool call.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_automatic_fillers.py
"""

from __future__ import annotations

import types

import pytest

from flows.authoring import autofill, build

# ── detection ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,filler,remainder", [
    ("Thanks for holding. Your balance is {bal}.",
     "Thanks for holding.", "Your balance is {bal}."),
    ("Okay, let me check that for you. One sec.",
     "Okay, let me check that for you.", "One sec."),
    ("Got it. Balance is {bal}.", "Got it.", "Balance is {bal}."),
    ("Alright! Your refund is processing.", "Alright!", "Your refund is processing."),
    ("I'll be right back with you. Your order is {id}.",
     "I'll be right back with you.", "Your order is {id}."),
    # Curly apostrophe is the same word.
    ("I’ll be right back. Your order is {id}.",
     "I’ll be right back.", "Your order is {id}."),
    # Digits and currency in the REMAINDER are fine; only the opener is gated.
    ("Sure. That comes to $40.", "Sure.", "That comes to $40."),
])
def test_it_hoists_a_contentless_opener(text, filler, remainder):
  hoist = autofill.split_leading_filler(text)
  assert hoist is not None
  assert (hoist.filler, hoist.remainder) == (filler, remainder)


@pytest.mark.parametrize("text,why", [
    ("Your card was declined. Try another one.", "opener carries substance"),
    ("I found three options. Which works?", "opener carries substance"),
    ("All set. Your refund is processing.", "'set' reports an outcome"),
    ("Done. Your order shipped.", "'done' reports an outcome"),
    ("One moment.", "no remainder — hoisting would leave the turn silent"),
    ("Okay.", "no terminator and no remainder"),
    ("Okay ", "no sentence terminator at all"),
    ("Thanks, {name}. Your balance is {bal}.", "opener interpolates a slot"),
    ("Just a second, 42. Your balance is {bal}.", "opener carries a digit"),
    ("Okay, that's $5. Your balance is {bal}.", "opener carries currency"),
    ("Mr. Smith will call. Your order shipped.", "abbreviation is not a filler"),
    ("Okay sure alright right fine great good perfect lovely. Balance is {b}.",
     "opener is longer than MAX_ACK_WORDS"),
])
def test_it_leaves_everything_else_alone(text, why):
  assert autofill.split_leading_filler(text) is None, why


def test_a_say_object_or_ladder_is_not_a_candidate():
  assert autofill.split_leading_filler(["Okay. Hi.", "Hello."]) is None
  assert autofill.split_leading_filler(None) is None
  assert autofill.split_leading_filler(object()) is None


@pytest.mark.parametrize("text", [
    "Thanks for holding. Your balance is {bal}.",
    "Okay, let me check that for you. One sec.",
    "Alright!  Your refund is processing.",
    "Got it.\nYour balance is {bal}.",
])
def test_the_split_never_edits_only_cuts(text):
  """The hoisted halves must rejoin to the authored string, byte for byte."""
  hoist = autofill.split_leading_filler(text)
  assert hoist is not None
  assert hoist.rejoin() == text


def test_an_app_may_widen_the_lexicon_but_gates_still_apply():
  text = "Righto. Your balance is {bal}."
  assert autofill.split_leading_filler(text) is None
  hoist = autofill.split_leading_filler(text, extra_ack=["righto"])
  assert hoist is not None and hoist.filler == "Righto."
  # extra_ack widens the vocabulary; it cannot buy past the structural gates.
  assert autofill.split_leading_filler(
      "Righto, 42. Balance is {bal}.", extra_ack=["righto"]) is None


def test_a_registered_phrase_may_contain_a_comma():
  """The candidate is split on commas, so a comma'd `extra_ack` entry would never be
  compared against anything unless the whole opener is tried first."""
  text = "Righto, mate. Your balance is {b}."
  assert autofill.split_leading_filler(text) is None
  hoist = autofill.split_leading_filler(text, extra_ack=["righto, mate"])
  assert hoist is not None and hoist.filler == "Righto, mate."


def test_a_bare_string_extra_ack_is_not_iterated_into_letters():
  """`extra_ack="righto"` must not teach the pass that "a" is a hold phrase."""
  app = types.SimpleNamespace(automatic_fillers={"extra_ack": "righto"})
  enabled, extra = autofill.filler_policy(app)
  assert enabled and extra == ("righto",)
  assert "a" not in autofill.allowed_phrases(extra)
  assert autofill.split_leading_filler("A. Your balance is {b}.",
                                       extra_ack=extra) is None


@pytest.mark.parametrize("node", [
    {"name": "t", "tool": "x", "awaits": ["pending"]},
    {"name": "t", "tool": "x", "awaits": "pending"},
    {"name": "s", "ask": "Okay. What is it?", "repeated": ["more"]},
])
def test_a_mistyped_field_does_not_crash_the_build(node):
  """This pass runs BEFORE validation, so a bad type must fall through to the
  validator's clean error rather than surfacing as an AttributeError traceback."""
  autofill.hoist_blocked_by(node, {}, is_task="tool" in node)


def test_only_the_first_sentence_is_hoisted():
  """Documented: a second opener sentence is left to be spoken after the tool, and
  joining the two with a comma moves both."""
  split = autofill.split_leading_filler("Okay. One moment. Balance is {b}.")
  assert (split.filler, split.remainder) == ("Okay.", "One moment. Balance is {b}.")
  joined = autofill.split_leading_filler("Okay, one moment. Balance is {b}.")
  assert (joined.filler, joined.remainder) == ("Okay, one moment.", "Balance is {b}.")


def test_an_absurdly_long_opener_exits_before_the_parses():
  """MAX_ACK_CHARS is an O(1) backstop in front of the format parse and tokenizer."""
  long_open = "Okay " * 40 + ". Balance is {b}."
  assert len(long_open.split(".")[0]) > autofill.MAX_ACK_CHARS
  assert autofill.split_leading_filler(long_open) is None
  # The longest legitimate phrase is nowhere near the bound, so nothing real is lost.
  assert max(len(p) for p in autofill.ACK_PHRASES) < autofill.MAX_ACK_CHARS
  assert autofill.split_leading_filler(
      "Okay, let me check that for you. Balance is {b}.") is not None


def test_a_malformed_format_string_is_treated_as_dynamic():
  assert autofill.split_leading_filler("Okay {. Your balance is 5.") is None


# ── the build pass ───────────────────────────────────────────────────────────


def _app(*, automatic_fillers=True, slots=None, tasks=None, **policy):
  """A bare App whose config we can drive `_apply_automatic_fillers` over directly."""
  import flows
  flow = flows.Flow("demo", root_agent="a")
  return flows.App(root_flow=flow, app_display_name="D",
                   automatic_fillers=automatic_fillers)


def _run(cfg, *, automatic_fillers=True, report=None):
  return build._apply_automatic_fillers(
      cfg, _app(automatic_fillers=automatic_fillers), report)


_THEN_SAY = "Thanks for holding. Your balance is {bal}."
_ASK = "Okay. What is your account number?"


def _task(**kw):
  return {"name": "t", "tool": "check", "then_say": _THEN_SAY, **kw}


def _slot(**kw):
  return {"name": "s", "ask": _ASK, **kw}


def test_it_hoists_a_task_then_say_onto_the_fire_turn():
  out = _run({"tasks": [_task(terminal=True)]})
  task = out["tasks"][0]
  assert task["filler_say"] == "Thanks for holding."
  assert task["then_say"] == "Your balance is {bal}."


def test_it_hoists_a_slot_ask():
  out = _run({"slots": [_slot()]})
  slot = out["slots"][0]
  assert slot["filler_say"] == "Okay."
  assert slot["ask"] == "What is your account number?"


@pytest.mark.parametrize("extra", [{}, {"terminal": True}])
def test_it_never_touches_preempt_then_say(extra):
  """It once did, and that dropped the next question out of the result turn.

  A non-terminal then_say is NOT relayed to the model — the turn already preempts. The
  ordinary path folds the line together with whatever comes next; preempting returns
  before that fold. See `test_the_following_question_survives_the_hoist`.
  """
  out = _run({"tasks": [_task(**extra)]})
  assert "preempt_then_say" not in out["tasks"][0]


def test_off_by_default_leaves_the_config_untouched():
  cfg = {"tasks": [_task()], "slots": [_slot()]}
  out = _run(cfg, automatic_fillers=False)
  assert out["tasks"][0]["then_say"] == _THEN_SAY
  assert out["slots"][0]["ask"] == _ASK
  assert "filler_say" not in out["tasks"][0]


@pytest.mark.parametrize("extra,why", [
    ({"verbatim": True}, "approved copy is reviewed for delivery order too"),
    ({"filler_say": "One sec."}, "the wait is already covered"),
    ({"parallel": "grp"}, "a group's filler comes from the first leg"),
    ({"then_say_variants": [{"type": "text", "text": "x"}]}, "surfaces would desync"),
    ({"channel_then_say_variants": {"voice": []}}, "surfaces would desync"),
    ({"tool": None, "component": "child"}, "a component never speaks its filler"),
    ({"awaits": {"max_turns": 5, "say": "This can take a minute."}},
     "awaits.say already covers this wait; two hold phrases would stack"),
])
def test_task_skip_rules(extra, why):
  out = _run({"tasks": [_task(**extra)]})
  task = out["tasks"][0]
  assert task["then_say"] == _THEN_SAY, why
  assert task.get("filler_say") == extra.get("filler_say"), why


@pytest.mark.parametrize("extra,cfg_extra,why", [
    ({"readback_verbatim": True}, {}, "author asked for deterministic delivery"),
    ({"ask_variants": [{"type": "text", "text": "x"}]}, {}, "surfaces would desync"),
    ({"repeated": {"ask_more": "And another?"}}, {}, "ask_more is a second ask"),
    ({"passive": True}, {}, "a passive slot is never asked"),
    ({"source": "announce"}, {}, "a preempting announce never arms a filler"),
    ({"source": "task"}, {}, "a result slot is never asked"),
    ({}, {"filler_say": ["One sec.", None]}, "would shadow the flow's rotating pool"),
])
def test_slot_skip_rules(extra, cfg_extra, why):
  out = _run({"slots": [_slot(**extra)], **cfg_extra})
  slot = out["slots"][0]
  assert slot["ask"] == _ASK, why
  assert "filler_say" not in slot, why


def test_an_ask_ladder_is_left_alone():
  ladder = ["Okay. What is your account number?", "One more time. Account number?"]
  out = _run({"slots": [{"name": "s", "ask": ladder}]})
  assert out["slots"][0]["ask"] == ladder


def test_a_flow_that_improvises_the_filler_is_skipped_entirely():
  """Improvised filler hands the tool call itself to the model, not just the line."""
  cfg = {"tasks": [_task()], "speech": {"improvise": ["filler", "retry"]}}
  out = _run(cfg)
  assert out["tasks"][0]["then_say"] == _THEN_SAY
  assert "filler_say" not in out["tasks"][0]


def test_a_node_can_opt_out():
  out = _run({"tasks": [_task(automatic_fillers=False)],
              "slots": [_slot(automatic_fillers=False)]})
  assert out["tasks"][0]["then_say"] == _THEN_SAY
  assert out["slots"][0]["ask"] == _ASK


@pytest.mark.parametrize("enabled", [True, False])
def test_the_marker_never_survives_the_pass(enabled):
  """It is not in the validator's key whitelist, so leaving it breaks the build.

  Critically this holds when the app never opted in: annotating a node must not turn
  into a build failure for an agent that does not use the feature at all.
  """
  out = _run({"tasks": [_task(automatic_fillers=False)],
              "slots": [_slot(automatic_fillers=False)]}, automatic_fillers=enabled)
  assert "automatic_fillers" not in out["tasks"][0]
  assert "automatic_fillers" not in out["slots"][0]


def test_the_pass_is_idempotent():
  """emit re-runs assembly after validate; a second pass must change nothing."""
  once = _run({"tasks": [_task()], "slots": [_slot()]})
  assert _run(once) == once


def test_it_only_ever_changes_three_keys():
  """The pass reschedules text. Anything else it touches changes turn STRUCTURE.

  An earlier draft also set `preempt_then_say`, which reads as a delivery detail and is
  not one: it returns before the engine folds the then_say together with the next
  question, so a following slot never got asked and an `escalate` chain stopped
  mid-walk. This invariant is cheaper than re-driving every shape.
  """
  cfg = {
      "slots": [_slot(), _slot(name="s2", readback_verbatim=True),
                {"name": "ann", "source": "announce", "preempt": True,
                 "response": [{"type": "text", "text": "Okay. Here it is."}]}],
      "tasks": [_task(), _task(name="t2", terminal=True),
                _task(name="t3", awaits={"max_turns": 5, "say": "Still working."}),
                _task(name="t4", tool=None, component="child"),
                _task(name="t5", parallel="grp")],
      "escalate": {"say": "Let me get someone.", "tasks": ["t2"]},
  }
  before, after = _run(cfg, automatic_fillers=False), _run(cfg)
  for key in ("slots", "tasks"):
    for old, new in zip(before[key], after[key]):
      assert old["name"] == new["name"]
      differing = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
      assert differing <= {"then_say", "ask", "filler_say"}, (old["name"], differing)
  # Everything outside slots/tasks is passed through untouched.
  assert {k: v for k, v in after.items() if k not in ("slots", "tasks")} == \
         {k: v for k, v in before.items() if k not in ("slots", "tasks")}


def test_it_reports_what_it_hoisted():
  report: list = []
  _run({"tasks": [_task()], "slots": [_slot()]}, report=report)
  assert sorted(report) == [("s", "Okay."), ("t", "Thanks for holding.")]


# ── driven against the engine ────────────────────────────────────────────────


def _drive(enabled, *, trailing_slot=False):
  """Run the real engine over a demo config: ask turn, fire turn, result turn."""
  import flows
  from flows.engine import loader as fb

  flow = flows.Flow("billing", root_agent="a")
  nodes = [flows.user_slot("account_number", "Okay. What's your account number?"),
           flows.result_slot("balance", "read_balance")]
  if trailing_slot:
    nodes.append(flows.user_slot("email_it", "Would you like that emailed to you?",
                                 requires=["balance"]))
  flow.add(*nodes)
  flow.task("read_balance", "fetch_balance", ["account_number"], "balance",
            out_key="balance",
            then_say="Thanks for holding. Your balance is {balance}.")
  app = flows.App(root_flow=flow, app_display_name="t", automatic_fillers=enabled)
  cfg = build._assemble(app)[0]["billing"]

  engine = fb.load_engine()
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or cfg.get("gate_slot")
  if gate:
    sm[gate] = sm["filled"][gate] = "billing"

  def turn(text, n):
    return engine.slot_filling_engine({
        "raw_config": cfg, "sm": sm, "last_user_text": text,
        "scanned_user_text": text, "is_inactivity": False, "event_data": {},
        "config_id": "billing", "n_user_turns": n})["action"]

  ask = turn("balance", 1)
  sm["filled"]["account_number"] = "5551234"
  fire = turn("5551234", 2)
  sm.update(fb.run_intake(
      "fetch_balance", {"success": True, "balance": "42 dollars"}, sm)["sm"])
  return ask, fire, turn("", 3)


def test_the_wait_is_silent_without_the_pass():
  ask, fire, result = _drive(False)
  assert ask.get("filler_partial") is None
  assert fire["message"] == ""          # the caller hears nothing while the tool runs
  assert fire["function_call"]["name"] == "fetch_balance"
  assert result["message"] == "Thanks for holding. Your balance is 42 dollars."


def test_the_hoisted_line_rides_the_tool_call():
  """The words are identical to the un-hoisted build; only the schedule moves."""
  ask, fire, result = _drive(True)
  assert ask["filler_partial"] == "Okay."
  assert fire["message"] == "Thanks for holding."
  assert fire["function_call"]["name"] == "fetch_balance"   # same turn as the line
  assert fire["preempt"] is True
  assert result["message"] == "Your balance is 42 dollars."
  # What the caller hears end to end is what the author wrote.
  assert (f"{fire['message']} {result['message']}"
          == "Thanks for holding. Your balance is 42 dollars.")


def test_the_following_question_survives_the_hoist():
  """The regression an earlier draft shipped: pinning `preempt_then_say` returned
  before the engine folds the then_say together with the next question, so the agent
  answered and then went silent until the caller spoke again."""
  _ask, _fire, off = _drive(False, trailing_slot=True)
  assert off["message"] == ("Thanks for holding. Your balance is 42 dollars. "
                            "Would you like that emailed to you?")
  _ask, fire, on = _drive(True, trailing_slot=True)
  assert fire["message"] == "Thanks for holding."
  assert on["message"] == ("Your balance is 42 dollars. "
                           "Would you like that emailed to you?")


def test_a_widened_lexicon_reaches_the_pass():
  import flows
  app = flows.App(root_flow=flows.Flow("demo", root_agent="a"),
                  app_display_name="D",
                  automatic_fillers={"extra_ack": ["righto"]})
  cfg = {"tasks": [_task(then_say="Righto. Your balance is {bal}.")]}
  out = build._apply_automatic_fillers(cfg, app)
  assert out["tasks"][0]["filler_say"] == "Righto."
