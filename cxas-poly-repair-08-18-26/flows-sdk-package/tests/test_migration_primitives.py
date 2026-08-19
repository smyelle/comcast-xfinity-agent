"""Primitives a real production slot-filling agent needs, ported from a live fork.

A deployed CES agent was found running a DIVERGED copy of the framework engine that
implements seven things the blessed bundle did not. Migrating it onto `flows` without
changing a single spoken line meant porting them, so these tests pin the RENDERED
STRINGS and the engine's turn-level disposition, not the shape of the code:

  * `readback_fmt: {"type": "digits"}`  — used to raise ValueError out of
    `_compile_formatter`, which `before_model` catches as `before_model_CRASH` and
    turns into `return None`: slot filling silently off for the whole flow, every turn.
  * `readback_fmt: {"type": "date", "text": ...}` and `{"type": "prefix", "values": ...}`
    — the extra params were silently DROPPED. Those validate clean and change what the
    caller hears: an MMDDYYYY date reaching TTS as one twelve-million-something number,
    an enum key ("remove_lift") read out as itself.
  * `readback_verbatim` / `preempt_then_say` — determinism flags. Both hand a scripted
    line to TTS instead of to the model, which is the difference between a readback and
    an invented question.
  * `on_failure.then` / `on_failure.on_exhaust.escalate` — act on a failure (send a
    fresh OTP) and pivot in-flow without terminating (OTP -> KBA).
  * `escalatable` / `cancelable` — both engines have always read these; only the
    validator's whitelist was missing them.

Plus the `_check_circular_requires` stack-pollution bug the port surfaced: one real
cycle was reported as a cascade of unrelated slots.

Every expected string here was produced by executing the live fork side by side with
this engine on the same input.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows.authoring.dsl import Flow, readback, user_slot  # noqa: E402
from flows.authoring.render import render_config_source  # noqa: E402
from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)


def fmt(spec):
  """Compile a readback_fmt spec exactly as the engine does at config-compile time."""
  return fb.load_engine()._compile_formatter(spec)  # noqa: SLF001


def errors_for(cfg):
  return fb.load_validator().DagConfigValidator(cfg).validate().errors


def drive(cfg, sm, text="", turn=1):
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False,
      "event_data": {}, "config_id": "t", "n_user_turns": turn,
  })


# --------------------------------------------------------------------------- #
# 1. readback_fmt "digits"
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("spec,value,expected", [
    # The SSN readback the fork ships. Single spaces, one per digit, label first.
    ({"type": "digits", "text": "the Social Security Number"}, "123456789",
     "the Social Security Number 1 2 3 4 5 6 7 8 9"),
    ({"type": "digits", "text": "the ZIP code"}, "94105", "the ZIP code 9 4 1 0 5"),
    # No label -> no leading space.
    ({"type": "digits"}, "2124561234", "2 1 2 4 5 6 1 2 3 4"),
    # Non-digits are dropped, so a punctuated value still reads cleanly.
    ({"type": "digits"}, "212-456-1234", "2 1 2 4 5 6 1 2 3 4"),
    # A value with NO digits falls back to itself, LABEL AND ALL DROPPED. Stripping
    # non-digits from a sentinel would otherwise empty a line that (being preempted)
    # the model cannot rescue — suppression must never create silence.
    ({"type": "digits", "text": "the mobile number"}, "declined", "declined"),
    ({"type": "digits"}, "", ""),
])
def test_digits_renders_one_digit_at_a_time(spec, value, expected):
  assert fmt(spec)(value) == expected


def test_digits_no_longer_crashes_the_whole_turn():
  """It used to raise out of _compile_formatter -> before_model_CRASH -> return None."""
  assert fmt({"type": "digits"}) is not None
  assert fmt("digits")("5309") == "5 3 0 9"


def test_an_unknown_fmt_type_still_raises():
  with pytest.raises(ValueError, match="Unknown readback_fmt type"):
    fmt({"type": "morse"})


# --------------------------------------------------------------------------- #
# 2. readback_fmt "date" — `text` lead-in + MMDDYYYY
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("spec,value,expected", [
    # MMDDYYYY carries the YEAR: the lift slots book a range up to a year out, where
    # "December 1st" alone is ambiguous. Verbatim, this is what TTS receives.
    ({"type": "date", "text": "the temporary lift will start on"}, "12012026",
     "the temporary lift will start on December 1st, 2026"),
    ({"type": "date"}, "01032026", "on January 3rd, 2026"),
    # ISO keeps the historical "on Month Nth" with no year.
    ({"type": "date"}, "2026-12-01", "on December 1st"),
    ({"type": "date", "text": "starting"}, "2026-12-11", "starting December 11th"),
    # 11th/12th/13th take "th", not "st"/"nd"/"rd".
    ({"type": "date"}, "2026-12-13", "on December 13th"),
    # Unparseable degrades to the lead-in + the raw value rather than raising.
    ({"type": "date"}, "not-a-date", "on not-a-date"),
    ({"type": "date", "text": "on"}, "not-a-date", "on not-a-date"),
])
def test_date_speaks_mmddyyyy_and_honours_text(spec, value, expected):
  assert fmt(spec)(value) == expected


def test_date_string_shorthand_is_unchanged():
  """The bare "date" shorthand must keep its old output — no `text`, no year."""
  assert fmt("date")("2026-12-01") == "on December 1st"


# --------------------------------------------------------------------------- #
# 3. readback_fmt "prefix" — `values` lookup
# --------------------------------------------------------------------------- #

_ACTIONS = {
    "remove_lift": "cancel your temporary lift and put your security freeze back on",
    "place_freeze": "place a security freeze",
}


@pytest.mark.parametrize("value,expected", [
    ("remove_lift",
     "you'd like me to cancel your temporary lift and put your security freeze back on"),
    ("place_freeze", "you'd like me to place a security freeze"),
    # An unmapped value falls through to itself, so adding a sixth enum key degrades
    # to the old behaviour instead of going silent.
    ("thaw", "you'd like me to thaw"),
])
def test_prefix_values_speaks_the_enum_not_its_key(value, expected):
  spec = {"type": "prefix", "text": "you'd like me to", "values": _ACTIONS}
  assert fmt(spec)(value) == expected


def test_prefix_without_values_is_unchanged():
  assert fmt({"type": "prefix", "text": "ending in"})("1234") == "ending in 1234"


# --------------------------------------------------------------------------- #
# 4. readback_verbatim
# --------------------------------------------------------------------------- #

def _verbatim_cfg(flag):
  slot = {
      "name": "phone", "source": "user", "setter": "set_phone",
      "ask": "What's your phone number?", "requires_readback": True,
      "readback_fmt": {"type": "digits", "text": "the phone number"},
  }
  if flag:
    slot["readback_verbatim"] = True
  return {"slots": [slot], "tasks": [], "gate_slot": None}


def _readback_turn(flag):
  cfg = _verbatim_cfg(flag)
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  drive(cfg, sm, "", turn=1)
  sm = dict(sm, pending={"phone": "2124561234"})
  out = drive(cfg, sm, "my number is 212 456 1234", turn=2)
  return out["action"], out["sm"]


READBACK_LINE = (
    "Just to confirm — the phone number 2 1 2 4 5 6 1 2 3 4. Is that correct?")


def test_readback_verbatim_preempts_the_first_presentation():
  action, sm = _readback_turn(True)
  # Model bypassed: this exact sentence is what TTS speaks.
  assert action["preempt"] is True
  assert action["message"] == READBACK_LINE
  # And it is recorded as spoken, so a second invocation on the same user turn
  # cannot preempt it twice.
  assert sm["_readback_spoken"] == ["phone"]


def test_without_the_flag_the_readback_is_still_relayed_to_the_model():
  action, sm = _readback_turn(False)
  assert not action.get("preempt")
  assert action["message"] == READBACK_LINE
  assert "_readback_spoken" not in sm


def test_a_second_invocation_on_the_same_turn_still_preempts_an_unspoken_readback():
  """`fresh_pending` asks "did pending change since the last INVOCATION", which is
  not "has this been spoken". A task that fires and returns early records the pending
  it staged, so the next invocation sees fresh_pending False for a readback nobody
  has heard. Gating on freshness alone silently handed it to the model."""
  cfg = _verbatim_cfg(True)
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {"phone": "2124561234"}
  # _last_state already carries this pending -> fresh_pending is False.
  sm["_last_state"] = {"filled": {}, "pending": {"phone": "2124561234"},
                       "deferred": {}}
  action = drive(cfg, sm, "", turn=2)["action"]
  assert action["preempt"] is True
  assert action["message"] == READBACK_LINE


def test_a_cleared_readback_re_arms():
  """Rejecting a value and re-collecting the SAME slot must be spoken again."""
  cfg = _verbatim_cfg(True)
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  sm["_readback_spoken"] = ["phone"]
  drive(cfg, sm, "", turn=1)
  assert "_readback_spoken" not in sm


def test_readback_verbatim_is_a_valid_slot_key():
  cfg = _verbatim_cfg(True)
  assert not [e for e in errors_for(cfg) if "readback_verbatim" in e]


def test_readback_verbatim_must_be_a_bool():
  cfg = _verbatim_cfg(True)
  cfg["slots"][0]["readback_verbatim"] = "yes"
  assert [e for e in errors_for(cfg) if "readback_verbatim" in e]


# --------------------------------------------------------------------------- #
# 5. preempt_then_say
# --------------------------------------------------------------------------- #

def _then_say_cfg(flag):
  t = {
      "name": "send_sms", "tool": "send_sms", "inputs": ["number"],
      "outputs": {"ok": "sent"}, "success_check": "ok", "terminal": False,
      "requires": ["number"], "then_say": "I've sent that confirmation text.",
  }
  if flag:
    t["preempt_then_say"] = True
  return {
      "slots": [
          {"name": "number", "source": "user", "setter": "set_number",
           "ask": "What's your number?"},
          {"name": "sent", "source": "task:send_sms"},
          {"name": "anything_else", "source": "user",
           "setter": "set_anything_else", "ask": "Anything else?",
           "requires": ["sent"]},
      ],
      "tasks": [t], "gate_slot": None,
  }


def _fire_and_land(cfg):
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {"number": "2124561234"}, {}
  drive(cfg, sm, "", turn=1)
  sm.update(fb.run_intake("send_sms", {"ok": True}, sm)["sm"])
  return drive(cfg, sm, "", turn=2)["action"]


def test_preempt_then_say_speaks_only_the_scripted_line():
  """Right after a backend action is exactly where a relaying model invents outcomes
  it never performed ("I've sent that to you" with the tool never called)."""
  action = _fire_and_land(_then_say_cfg(True))
  assert action["preempt"] is True
  assert action["message"] == "I've sent that confirmation text."


def test_without_the_flag_then_say_is_folded_in_with_the_next_question():
  action = _fire_and_land(_then_say_cfg(False))
  assert action["message"] == "I've sent that confirmation text. Anything else?"


def test_preempt_then_say_is_a_valid_task_key():
  assert not [e for e in errors_for(_then_say_cfg(True))
              if "preempt_then_say" in e]


# --------------------------------------------------------------------------- #
# 6. on_failure.then + on_failure.on_exhaust.escalate
# --------------------------------------------------------------------------- #

def _otp_cfg(then=None, escalate=None):
  on_failure = {
      "max_retries": 2, "clear_slots": ["otp"],
      "retry_say": "That code didn't match. I've sent a new code to your phone.",
      "on_exhaust": {
          "say": "That code still isn't verifying. Let's try security questions.",
          "then": {"tool": "set_auth_method", "args": {"auth_method": "kba"}},
      },
  }
  if then is not None:
    on_failure["then"] = then
  if escalate is not None:
    on_failure["on_exhaust"]["escalate"] = escalate
  return {
      "slots": [
          {"name": "otp", "source": "user", "setter": "set_otp",
           "ask": "What's the code?"},
          {"name": "verified", "source": "task:validate_otp"},
      ],
      "tasks": [{
          "name": "validate_otp", "tool": "validate_otp", "inputs": ["otp"],
          "outputs": {"verified": "verified"}, "success_check": "verified",
          "terminal": False, "requires": ["otp"], "on_failure": on_failure,
      }],
      "gate_slot": None,
  }


def _fail_once(cfg, prior_retries):
  sm = fb.seed_sm(copy.deepcopy(cfg))
  sm["filled"], sm["pending"] = {"otp": "111111"}, {}
  sm["_retries"] = {"validate_otp": prior_retries}
  sm["task_results"] = {"validate_otp": {"verified": False}}
  sm["_task_just_completed"] = "validate_otp"
  out = drive(cfg, sm, "", turn=2)
  return out["action"], out["sm"]


def test_on_failure_then_fires_a_tool_on_the_RETRY_branch():
  """The retry line says a NEW code was sent. Without `then` none is: the caller reads
  back either the old code (rejected again) or nothing, three times, to exhaustion."""
  action, _ = _fail_once(_otp_cfg(then={"tool": "send_otp", "args": {}}), 0)
  assert action["message"].startswith("That code didn't match.")
  assert action["function_call"] == {"name": "send_otp", "args": {}}


def test_a_retry_without_then_fires_nothing():
  action, _ = _fail_once(_otp_cfg(), 0)
  assert action.get("function_call") is None


def test_then_args_interpolate_filled_slots():
  """Same `{slot}` resolution on_exhaust.then already has — one contract, not two.
  `otp` is NOT usable here: on_failure.clear_slots pops it before this branch runs."""
  cfg = _otp_cfg(then={"tool": "send_otp", "args": {"to": "{phone}"}})
  cfg["slots"].append(
      {"name": "phone", "source": "user", "setter": "set_phone", "ask": "Number?"})
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {"otp": "111111", "phone": "2124561234"}, {}
  sm["_retries"] = {"validate_otp": 0}
  sm["task_results"] = {"validate_otp": {"verified": False}}
  sm["_task_just_completed"] = "validate_otp"
  action = drive(cfg, sm, "", turn=2)["action"]
  assert action["function_call"] == {
      "name": "send_otp", "args": {"to": "2124561234"}}


def test_exhaust_escalate_false_fires_the_tool_but_lets_the_flow_continue():
  """The OTP -> KBA pivot. Escalating here strands the caller: they are told security
  questions are coming and the flow terminates instead, with the whole KBA branch
  (8 slots, 2 tasks) unreachable."""
  action, sm = _fail_once(_otp_cfg(escalate=False), 1)
  assert action["function_call"]["name"] == "set_auth_method"
  assert sm.get("status") != "escalated"


@pytest.mark.parametrize("escalate", [None, True])
def test_exhaust_still_escalates_by_default(escalate):
  _, sm = _fail_once(_otp_cfg(escalate=escalate), 1)
  assert sm["status"] == "escalated"


def test_then_and_escalate_are_valid_on_failure_keys():
  cfg = _otp_cfg(then={"tool": "send_otp", "args": {}}, escalate=False)
  assert not [e for e in errors_for(cfg)
              if "on_failure" in e and "unknown keys" in e]


def test_escalate_is_rejected_where_the_engine_never_reads_it():
  """Only the TASK on_failure exhaust sets sm["status"], so `escalate` anywhere else
  would be inert config that reads as though it did something."""
  cfg = _otp_cfg()
  cfg["slots"][0]["validation"] = {
      "max_retries": 2, "on_exhaust": {"say": "Sorry.", "escalate": False}}
  assert [e for e in errors_for(cfg)
          if "validation.on_exhaust.escalate is only supported" in e]


# --------------------------------------------------------------------------- #
# 7. escalatable / cancelable — read by the engine, missing from the whitelist
# --------------------------------------------------------------------------- #

_ESCALATABLE_CFG = {
    "slots": [{"name": "topic", "source": "user", "setter": "set_topic",
               "ask": "What can I help with?"}],
    "tasks": [],
    "gate_slot": None,
    "escalatable": False,
    "cancelable": False,
}


def test_escalatable_and_cancelable_are_valid_config_keys():
  assert not [e for e in errors_for(_ESCALATABLE_CFG)
              if "Unknown top-level config keys" in e]


def test_escalatable_false_suppresses_the_synthesized_escalate_tool():
  """The escalation flow itself sets this: with the control slot synthesized,
  transfer_to_human is advertised on turn one and the flow terminates before its own
  containment ask ever runs."""
  cfg = copy.deepcopy(_ESCALATABLE_CFG)
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  drive(cfg, sm, "", turn=1)
  assert sm["_escalate_tool"] == ""

  allowed = copy.deepcopy(_ESCALATABLE_CFG)
  allowed.pop("escalatable")
  sm2 = fb.seed_sm(allowed)
  sm2["filled"], sm2["pending"] = {}, {}
  drive(allowed, sm2, "", turn=1)
  assert sm2["_escalate_tool"] == "transfer_to_human"


def test_the_dsl_accepts_them_as_flow_policy():
  cfg = Flow("human", escalatable=False).to_config()
  assert cfg["escalatable"] is False


# --------------------------------------------------------------------------- #
# 8. _check_circular_requires — one cycle must report as one cycle
# --------------------------------------------------------------------------- #

def _slots_cfg(slots):
  return {"slots": slots, "tasks": [], "gate_slot": None}


def _cycle_errors(cfg):
  return [e for e in errors_for(cfg) if "Circular requires" in e]


def test_a_real_cycle_no_longer_cascades_onto_innocent_slots():
  """`has_cycle` returned True without discarding its own frame, so every name on the
  failing path stayed in `stack` forever and every LATER slot whose dependency closure
  touched one was reported too. One cycle, four errors."""
  cfg = _slots_cfg([
      {"name": "x", "source": "user", "setter": "set_x", "ask": "x?",
       "requires": ["y"]},
      {"name": "y", "source": "user", "setter": "set_y", "ask": "y?",
       "requires": ["x"]},
      # Innocent chain hanging off the cycle.
      {"name": "b", "source": "user", "setter": "set_b", "ask": "b?",
       "requires": ["x"]},
      {"name": "c", "source": "user", "setter": "set_c", "ask": "c?",
       "requires": ["b"]},
      {"name": "d", "source": "user", "setter": "set_d", "ask": "d?",
       "requires": ["c"]},
  ])
  assert _cycle_errors(cfg) == ["Circular requires involving 'x'"]


def test_a_self_referential_condition_is_the_never_ask_idiom_not_a_cycle():
  """A slot whose own condition names itself is inactive while unfilled, so the
  selector skips it and a sibling multi-field setter writes it. `requires`
  auto-satisfies an INACTIVE dependency and there is no topological sort anywhere in
  the engine, so nothing stalls — proven against the live flow. The documented
  alternative (`passive: true`) is NOT equivalent: it un-hides the slot's setter
  unconditionally, which for an auth-method slot means a caller can skip OTP."""
  cfg = _slots_cfg([
      {"name": "a", "source": "user", "setter": "set_a", "ask": "a?"},
      {"name": "selfy", "source": "user", "setter": "set_a",
       "setter_field": "selfy", "ask": "never asked",
       "condition": {"slot": "selfy", "eq": "yes"}},
      {"name": "b", "source": "user", "setter": "set_b", "ask": "b?",
       "requires": ["selfy"]},
      {"name": "c", "source": "user", "setter": "set_c", "ask": "c?",
       "requires": ["b"]},
      {"name": "d", "source": "user", "setter": "set_d", "ask": "d?",
       "requires": ["c"]},
  ])
  assert _cycle_errors(cfg) == []


def test_a_genuine_self_loop_in_requires_is_still_an_error():
  cfg = _slots_cfg([
      {"name": "loop", "source": "user", "setter": "set_loop", "ask": "?",
       "requires": ["loop"]},
  ])
  assert _cycle_errors(cfg) == ["Circular requires involving 'loop'"]


def test_a_two_slot_condition_cycle_is_still_an_error():
  """Only the SELF edge is dropped; a mutual condition cycle still stalls."""
  cfg = _slots_cfg([
      {"name": "p", "source": "user", "setter": "set_p", "ask": "p?",
       "condition": {"slot": "q", "eq": "yes"}},
      {"name": "q", "source": "user", "setter": "set_q", "ask": "q?",
       "condition": {"slot": "p", "eq": "yes"}},
  ])
  assert _cycle_errors(cfg) == ["Circular requires involving 'p'"]


# --------------------------------------------------------------------------- #
# 9. The announce-preempt pair — the question is spoken ONCE
# --------------------------------------------------------------------------- #
#
# The production app puts each PII question in a preempting `announce` and captures
# the answer in the NEXT slot, whose `ask` repeats the same question. The pairing is
# deliberate: folding the question into the capture slot's own `ask` with `preempt`
# would preempt the ANSWER turn too, so the setter never fires and the slot re-asks
# forever. The `ask` is there for the STANDALONE re-ask (a validation retry, where no
# announce fires).
#
# Both land on the announce turn and are concatenated into one directive message, so
# without suppression the caller hears the question twice. Live on the deployed app:
#   "[Auth_Agent] Last one — what's your 9-digit Social Security Number?
#                 What's your 9-digit Social Security Number?"
# Every string below was produced by executing the live fork side by side with this
# engine on the same input.

# Byte-exact from the deployed app's auth DAG.
SSN_Q = {"name": "ssn_q", "source": "announce", "preempt": True,
         "message": "Last one — what's your 9-digit Social Security Number?"}
SSN = {"name": "ssn", "source": "user", "setter": "set_pii", "setter_field": "ssn",
       "requires": ["ssn_q"], "requires_readback": True, "readback_verbatim": True,
       "readback_fmt": {"type": "digits", "text": "the Social Security Number"},
       "ask": "What's your 9-digit Social Security Number?"}
ZIP_Q = {"name": "zip_q", "source": "announce", "preempt": True,
         "message": "What's your 5-digit ZIP code?"}
ZIP = {"name": "zip_code", "source": "user", "setter": "set_pii",
       "setter_field": "zip_code", "requires": ["zip_q"],
       "ask": "What's your 5-digit ZIP code?"}


def _pair_cfg(*slots):
  return {"slots": [copy.deepcopy(s) for s in slots], "tasks": [], "gate_slot": None}


def _first_turn(*slots):
  cfg = _pair_cfg(*slots)
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  return drive(cfg, sm, "", turn=1)["action"]


def test_the_announce_swallows_an_ask_it_already_said_verbatim():
  """zip_q/zip_code: announce text == ask text. Was said twice."""
  action = _first_turn(ZIP_Q, ZIP)
  assert action["message"] == "What's your 5-digit ZIP code?"


def test_a_prefixed_announce_still_swallows_its_ask():
  """ssn_q/ssn: DIFFERENT strings — the announce merely CONTAINS the ask, prefixed
  with "Last one —". Equality would have missed this one; containment catches it."""
  action = _first_turn(SSN_Q, SSN)
  assert action["message"] == (
      "Last one — what's your 9-digit Social Security Number?")


def test_the_announce_still_preempts_and_the_capture_slot_is_still_awaited():
  """Suppressing the ask must not disturb the turn's disposition: the announce is
  still spoken verbatim (preempt) and the engine is still waiting on the value."""
  cfg = _pair_cfg(SSN_Q, SSN)
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  out = drive(cfg, sm, "", turn=1)
  assert out["action"]["preempt"] is True
  assert out["action"]["force_preempt"] is True
  assert out["sm"]["_awaiting"] == "ssn"


def test_a_complementary_ask_survives():
  """The KBA pairs read "<question> … Please say the number of your answer." — the
  trailing instruction is the only thing telling the caller HOW to reply, and the
  announce never said it. Containment, not a blanket drop."""
  q = {"name": "kba_q1_announce", "source": "announce", "preempt": True,
       "message": "Which of these streets have you lived on? 1 Elm. 2 Oak. 3 Pine."}
  a = {"name": "kba_answer_1", "source": "user", "setter": "set_kba_answer",
       "requires": ["kba_q1_announce"],
       "ask": "Please say the number of your answer."}
  action = _first_turn(q, a)
  assert action["message"] == (
      "Which of these streets have you lived on? 1 Elm. 2 Oak. 3 Pine."
      " Please say the number of your answer.")


def test_an_unrelated_announce_leaves_the_next_ask_alone():
  """The rule is additive: a flow whose announce is not the question keeps both."""
  q = {"name": "note", "source": "announce", "preempt": True,
       "message": "I see you're calling from a number on file."}
  action = _first_turn(q, dict(SSN, requires=["note"]))
  assert action["message"] == (
      "I see you're calling from a number on file."
      " What's your 9-digit Social Security Number?")


def test_the_standalone_re_ask_is_never_suppressed():
  """Suppression is scoped to the announce turn. On a later turn the announce has
  already filled, so the `ask` is the ONLY content — dropping it would produce an
  empty agent turn, the caller's answer would land mid-question, and the whole
  conversation would slip one turn (set_pii never fires)."""
  cfg = _pair_cfg(SSN_Q, SSN)
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {"ssn_q": True}, {}
  action = drive(cfg, sm, "", turn=2)["action"]
  assert action["message"] == "What's your 9-digit Social Security Number?"


def test_duplicate_detection_ignores_punctuation_and_case():
  """An announce written with an em-dash and an ask written without it are the same
  sentence to a listener."""
  norm = fb.load_engine()._norm_for_dup  # noqa: SLF001
  assert norm("Last one — what's your 9-digit SSN?") == (
      "last one what s your 9 digit ssn")
  assert norm("What's your ZIP?") in norm("Okay. what's your zip???")
  assert norm(None) == ""


# --------------------------------------------------------------------------- #
# Authoring surface
# --------------------------------------------------------------------------- #

def test_the_readback_builder_knows_digits():
  assert readback("digits", text="the ZIP code") == {
      "type": "digits", "text": "the ZIP code"}
  assert readback("date", text="starting") == {"type": "date", "text": "starting"}
  assert readback("prefix", text="you'd like me to", values=_ACTIONS) == {
      "type": "prefix", "text": "you'd like me to", "values": _ACTIONS}


# --------------------------------------------------------------------------- #
# 8. validate_against on a MULTI-FIELD setter
#
# The live app's action menu is one slot with `validate_against` fed by a setter
# that also writes a second field. That combination was silently unusable: the
# check read the response field at the TOP level, which the multi-setter contract
# never populates, so the comparison saw "" and rejected every value the caller
# gave. Found on a deployed call — the caller picked an action, the slot never
# staged, no readback opened, `confirm_pending` had nothing to commit, and every
# task gated on that slot was unreachable for the rest of the call.
# --------------------------------------------------------------------------- #

_ACTION_SLOTS = [
    {"name": "allowed_actions", "source": "task:load"},
    {
        "name": "freeze_action",
        "source": "user",
        "setter": "set_freeze_action",
        "setter_field": "value",
        "requires_readback": True,
        "validate_against": {
            "response_field": "value",
            "filled_slot": "allowed_actions",
            "error_code": "action_not_allowed",
        },
        "ask": "What would you like to do?",
    },
    {
        "name": "lift_default_requested",
        "source": "user",
        "setter": "set_freeze_action",
        "setter_field": "use_default_lift",
        "condition": {"slot": "lift_default_requested", "eq": "yes"},
    },
]


def _action_sm(allowed="place_freeze,lift_freeze"):
  cfg = {"slots": _ACTION_SLOTS, "tasks": []}
  sm = fb.seed_sm(copy.deepcopy(cfg))
  sm["filled"] = {"allowed_actions": allowed}
  sm.setdefault("pending", {})
  return cfg, sm


def _set_action(sm, response_data):
  fb.load_intake().slot_intake({
      "tool_name": "set_freeze_action", "response_data": response_data,
      "sm": sm, "current_agent": "", "channel": "",
  })
  return sm


# The shape `flows.authoring.setters.gen_multi_setter` emits: values only.
_GENERATED = {"stored": True, "values": {"value": "place_freeze"},
              "field_errors": {}}
# The shape a hand-written setter emits: values PLUS a top-level mirror.
_MIRRORED = {"stored": True, "value": "place_freeze",
             "values": {"value": "place_freeze"}}


@pytest.mark.parametrize("response_data", [_GENERATED, _MIRRORED])
def test_validate_against_reads_a_multi_setters_own_field(response_data):
  """Both setter shapes stage the value. The generated one used to be rejected."""
  _, sm = _action_sm()
  _set_action(sm, response_data)
  assert sm["pending"] == {"freeze_action": "place_freeze"}
  assert not sm.get("_slot_errors")


def test_a_value_outside_the_allowed_list_is_still_rejected():
  """The gate still bites — it just compares the value the setter actually sent."""
  _, sm = _action_sm(allowed="lift_freeze")
  _set_action(sm, _GENERATED)
  assert sm.get("pending") == {}
  assert sm["_slot_errors"] == [
      {"slot": "freeze_action", "code": "action_not_allowed"}]


def test_the_top_level_read_is_still_the_fallback():
  """A setter that reports the checked field ONLY at the top level keeps working:
  the source app's own tools do exactly that, and the fallback is what makes this
  a widening rather than a behaviour change."""
  _, sm = _action_sm()
  _set_action(sm, {"stored": True, "value": "place_freeze",
                   "values": {"value": "place_freeze"}, "field_errors": {}})
  assert sm["pending"] == {"freeze_action": "place_freeze"}


def test_a_staged_value_opens_the_readback_before_any_task_can_fire():
  """The ordering the defect broke: staged -> READBACK -> confirm. A rejected value
  never reached pending, so the engine skipped straight past the readback and the
  caller was answered by whatever the model invented."""
  cfg, sm = _action_sm()
  _set_action(sm, _GENERATED)
  action = drive(cfg, sm, "place a freeze")["action"]
  assert action["message"] == (
      "Just to confirm — freeze_action: place_freeze. Is that correct?")
  assert sm["filled"].get("freeze_action") is None  # pending, not committed


def test_a_non_dict_values_payload_cannot_crash_the_check():
  """`values` is whatever the tool returned; a scalar there must not except out of
  intake (the crash envelope would swallow the whole result silently)."""
  _, sm = _action_sm()
  _set_action(sm, {"stored": True, "value": "place_freeze", "values": "oops"})
  assert not sm.get("pending")


# --------------------------------------------------------------------------- #
# 9. The validator's setter/config agreement check reads a dict LITERAL
#
# `_check_setter_output_keys` compares each `setter_field` against the keys the
# setter's source writes into `values`. It only saw `values["k"] = v` subscripts,
# so a setter that builds its payload in one expression — the source app's own
# idiom — was reported as writing none of its fields, and the whole app failed to
# emit. A computed key now marks the name UNDETERMINED instead of half-read.
# --------------------------------------------------------------------------- #

def _values_keys(src):
  return fb.load_validator()._extract_values_dict_keys(src)  # noqa: SLF001


def test_a_dict_literal_bound_to_values_is_read():
  assert _values_keys(
      'def s():\n  values = {"value": k}\n  return {"values": values}\n'
  ) == {"value"}


def test_a_literal_and_a_later_subscript_are_unioned():
  assert _values_keys(
      'def s():\n'
      '  values = {"value": k}\n'
      '  values["use_default_lift"] = "yes"\n'
      '  return {"values": values}\n'
  ) == {"value", "use_default_lift"}


def test_subscripts_alone_still_work():
  assert _values_keys(
      'def s():\n  values = {}\n  values["ssn"] = v\n  return {"values": values}\n'
  ) == {"ssn"}


@pytest.mark.parametrize("body", [
    '  values = {}\n  values[field] = v\n',          # computed subscript key
    '  values = {field: v}\n',                        # computed literal key
])
def test_a_computed_key_makes_the_read_undetermined(body):
  """Not "writes nothing" — unknowable. A per-field loop (the KBA answer setter)
  writes every field through one computed subscript; reporting that as an empty
  set accused it of writing none of them."""
  assert _values_keys(f'def s():\n{body}  return {{"values": values}}\n') is None


def test_a_setter_with_no_values_name_is_undetermined():
  """Unchanged: nothing named `values` means the check has nothing to assert on."""
  assert _values_keys('def s():\n  return {"values": {"a": 1}}\n') is None


# --------------------------------------------------------------------------- #
# 10. A native sibling `transfer_to` never opens the destination's gate
#
# Two engine fixes that only work TOGETHER (either alone makes the call worse):
#
#   FIX 1 — a terminal task's `on_complete.transfer_to` tears its own flow down to a
#   zombie (_terminate -> _flow_clear wipes `filled`, gate slot included) and hands
#   control straight to the sibling agent. Every OTHER hop goes through the host's
#   `set_active_flow`, which refills the gate; a native transfer does not, so the
#   destination engine took the gate early-return on every turn: no announce, no
#   executor preempt, and a `requires:` naming that announce could never be satisfied.
#   Live trace: {"tag":"prereq_not_met","slot":"freeze_action","missing":"action_menu"}
#   on all 151 entries. The arrival now seeds the gate from the destination's own
#   config id.
#
#   FIX 2 — the gate/terminal render turns return BEFORE the DAG runs, so nothing
#   computes the executor-tool hide list that every in-flow turn gets. The model saw
#   load_session / check_eligibility / place_freeze and called them itself, then
#   narrated a result the engine never produced ("I have successfully placed a
#   security freeze... your confirmation number is..."). Fix 1 alone leaves that hole
#   open; fix 2 alone makes a stuck destination agent silent instead of wrong.
# --------------------------------------------------------------------------- #

_DEST_ID = "freeze_action"


def _dest_cfg(**over):
  """A destination flow shaped like the one a sibling transfers INTO: gated,
  reset_on_complete, an announce gated behind a task result, and executors."""
  cfg = {
      "bootstrap": {"tool": "set_active_flow", "slot": "active_flow",
                    "reset_on_complete": True, "welcome_slot": "welcome"},
      "gate_slot": "active_flow",
      "slots": [
          {"name": "welcome", "source": "announce", "preempt": True,
           "message": "Thanks for waiting."},
          {"name": "tracker_id", "source": "task:load_session"},
          {"name": "status_greeting", "source": "task:check_eligibility"},
          {"name": "action_menu", "source": "announce", "preempt": True,
           "requires": ["status_greeting"], "message": "{status_greeting}"},
          {"name": "freeze_action", "source": "user", "setter": "set_freeze_action",
           "requires": ["action_menu"], "ask": "What would you like to do?"},
      ],
      "tasks": [
          {"name": "load_session", "tool": "load_session", "inputs": [],
           "outputs": {"tracker_id": "tracker_id"},
           "condition": {"slot": "tracker_id", "filled": False}},
          {"name": "check_eligibility", "tool": "check_eligibility",
           "inputs": ["tracker_id"], "requires": ["tracker_id"],
           "outputs": {"status_greeting": "status_greeting"},
           "condition": {"slot": "status_greeting", "filled": False}},
          {"name": "place_freeze", "tool": "place_freeze", "inputs": ["freeze_action"],
           "requires": ["freeze_action"], "outputs": {"confirmation": "confirmation"}},
      ],
  }
  cfg.update(over)
  return cfg


def _arrived_from(flow, cfg=None, status="zombie"):
  """The sm as it reaches the destination agent: the SOURCE flow's `_terminate`
  already ran (scope cleared, status=zombie) and the transfer part was delivered,
  so `_zombie` carries only the source flow id + its shared values."""
  cfg = cfg or _dest_cfg()
  sm = fb.seed_sm(cfg)
  sm["_config_id"] = "auth"          # the config the ENGINE last ran (the source)
  sm["filled"], sm["pending"], sm["deferred"] = {}, {}, {}
  sm["status"] = status
  sm["_zombie"] = {"flow": flow, "shared_values": {"auth_passed": True}}
  sm["_shared_slots"] = ["auth_passed"]
  return sm


def _dest_drive(cfg, sm, text="", turn=1):
  """One turn of the DESTINATION agent (config_id is its own flow id)."""
  return fb.load_engine().slot_filling_engine({
      "raw_config": copy.deepcopy(cfg), "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False,
      "event_data": {}, "config_id": _DEST_ID, "n_user_turns": turn,
  })


def _task_tools(cfg):
  return {t["tool"] for t in cfg["tasks"] if t.get("tool")}


# ── FIX 1: seed the destination's gate on arrival ────────────────────────────

def test_a_sibling_transfer_arrival_opens_the_destination_flow():
  """Before: tag="gate" forever. After: the gate carries the destination's own flow
  id, the zombie is reaped, and the DAG drives (welcome announce + first executor)."""
  cfg = _dest_cfg()
  out = _dest_drive(cfg, _arrived_from("auth", cfg))
  sm = out["sm"]

  assert sm["filled"]["active_flow"] == _DEST_ID
  assert sm["status"] == "in_progress"
  assert out["action"].get("tag") != "gate"
  assert out["action"]["message"] == "Thanks for waiting."
  assert out["action"]["function_call"]["name"] == "load_session"
  assert "_zombie" not in sm


def test_the_arrival_carries_the_sources_shared_values_forward():
  """The reap keeps what the source published (auth_passed), so the destination's
  auth-conditioned tasks are live on turn one rather than re-asking."""
  cfg = _dest_cfg()
  out = _dest_drive(cfg, _arrived_from("auth", cfg))
  assert out["sm"]["filled"]["auth_passed"] is True


def test_the_seeded_gate_lets_the_announce_chain_reach_its_dependent_slot():
  """The live defect end to end: `action_menu` is announce-gated behind a task
  result, and `freeze_action.requires` names it. With the gate shut the announce
  never fires and the slot is permanently `prereq_not_met`."""
  cfg = _dest_cfg()
  sm = _arrived_from("auth", cfg)
  _dest_drive(cfg, sm)
  sm["filled"]["tracker_id"] = "T1"                      # load_session returned
  out = _dest_drive(cfg, sm, turn=2)
  assert out["action"]["function_call"]["name"] == "check_eligibility"
  sm["filled"]["status_greeting"] = "You're eligible. What next?"
  out = _dest_drive(cfg, sm, turn=3)

  assert out["sm"]["filled"]["action_menu"] is True
  # announce + the now-unblocked ask, spoken as one turn
  assert out["action"]["message"] == (
      "You're eligible. What next? What would you like to do?")


def test_a_flows_own_completion_is_not_re_seeded():
  """The guard. When THIS flow just completed, the zombie is its own — re-opening
  the gate there would drive a finished flow (400 SESSION_ALREADY_ENDED). Only a
  zombie belonging to a DIFFERENT flow means "we arrived from elsewhere"."""
  cfg = _dest_cfg()
  out = _dest_drive(cfg, _arrived_from(_DEST_ID, cfg))

  assert out["sm"]["filled"].get("active_flow") is None
  assert out["sm"]["status"] == "zombie"
  assert out["action"]["tag"] == "gate"


def test_a_destination_without_reset_on_complete_is_left_alone():
  """`reset_on_complete` is what makes a flow re-enterable; without it the author
  has said the flow runs once, so the arrival must not force it open."""
  cfg = _dest_cfg()
  cfg["bootstrap"] = dict(cfg["bootstrap"], reset_on_complete=False)
  out = _dest_drive(cfg, _arrived_from("auth", cfg))

  assert out["sm"]["filled"].get("active_flow") is None
  assert out["action"]["tag"] == "gate"


def test_an_ordinary_gate_turn_is_untouched():
  """No zombie = a normal pre-request gate turn: still the gate render, still
  waiting for the host's set_active_flow."""
  cfg = _dest_cfg()
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"], sm["status"] = {}, {}, "in_progress"
  out = _dest_drive(cfg, sm)

  assert out["sm"]["filled"].get("active_flow") is None
  assert out["action"]["tag"] == "gate"


# ── FIX 2: executor tools are engine-owned on gate/terminal turns too ────────

def _hide_on(phase, cfg=None):
  cfg = cfg or _dest_cfg()
  return set(fb.load_engine()._hiding_policy({}, cfg, phase))  # noqa: SLF001


def test_the_gate_turn_hides_every_executor_tool():
  cfg = _dest_cfg()
  assert _task_tools(cfg) <= _hide_on("gate", cfg)


def test_the_terminal_turn_hides_every_executor_tool():
  cfg = _dest_cfg()
  assert _task_tools(cfg) <= _hide_on("terminal", cfg)


def test_the_gate_render_action_carries_the_hides():
  """Not just the policy — the action the gate branch actually returns."""
  cfg = _dest_cfg()
  out = _dest_drive(cfg, _arrived_from(_DEST_ID, cfg))
  assert out["action"]["tag"] == "gate"
  assert _task_tools(cfg) <= set(out["action"]["hide_tools"])


def test_the_in_flow_policy_is_unchanged():
  """In-flow turns already hide executors — `_compute_hidden_tools` takes the same
  task tools as `executor_tools`. Adding them to the in-flow POLICY too would
  re-hide the tool the engine is firing this turn and break the pinned
  cancel/escalate disposition lists, so the fix is scoped to the two render turns."""
  assert not (_task_tools(_dest_cfg()) & _hide_on("in_flow"))


def test_a_component_task_without_a_tool_is_skipped():
  """A Component task has no `tool` key; the hide loop must not KeyError on it."""
  cfg = _dest_cfg()
  cfg["tasks"] = cfg["tasks"] + [{"name": "sub", "component": "child_flow"}]
  assert _task_tools(cfg) <= _hide_on("gate", cfg)


# --------------------------------------------------------------------------- #
# 11. An event prefill into a `requires_readback` slot is STAGED, not accepted
# --------------------------------------------------------------------------- #
# The eighth fork primitive, found while porting the Equifax auth flow. Its `phone`
# slot is `source: ["event", "user"]` + `event_key: "ani"` + `requires_readback`, and
# the app's own variable doc says the ANI "Prefills the auth `phone` slot as PENDING so
# it is read back for confirmation, never silently accepted". The fork's
# `_apply_event_prefill` splits the event values on `requires_readback` and stages that
# half with `skip_readback=False`; the blessed bundle called `fill_slots` once with the
# default and wrote every event value straight into `filled`. So the one identity field
# the caller never spoke was the one field never confirmed — and the validator's own
# `_check_requires_readback_source` note ("event fills … skipping the confirmation
# entirely") described the gap rather than a rule.


def _ani_cfg():
  return {
      "slots": [
          {"name": "ani", "source": "event", "event_key": "ani"},
          {"name": "phone", "source": ["event", "user"], "event_key": "ani",
           "setter": "set_phone", "ask": "What's your mobile number?",
           "requires_readback": True,
           "readback_fmt": {"type": "digits", "text": "the mobile phone number"}},
      ],
      "tasks": [], "gate_slot": None,
  }


def _event_drive(cfg, sm, event_data, text="", turn=1):
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False,
      "event_data": dict(event_data), "config_id": "t", "n_user_turns": turn,
  })


def test_a_readback_slot_prefilled_from_an_event_lands_in_pending():
  cfg = _ani_cfg()
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  out = _event_drive(cfg, sm, {"ani": "2124561234"})
  assert out["sm"]["pending"] == {"phone": "2124561234"}
  assert "phone" not in out["sm"]["filled"]
  assert out["action"]["message"] == (
      "Just to confirm — the mobile phone number 2 1 2 4 5 6 1 2 3 4. Is that correct?")


def test_an_event_slot_without_a_readback_is_still_filled_outright():
  """`ani` itself is evidence a prefill happened, not a value anyone confirms."""
  cfg = _ani_cfg()
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  out = _event_drive(cfg, sm, {"ani": "2124561234"})
  assert out["sm"]["filled"]["ani"] == "2124561234"


def test_a_rejected_event_prefill_is_not_silently_re_staged():
  """`event_data` is re-delivered on EVERY engine call, and `fill_slots` only skips
  what is already in FILLED — so without the `_event_prefill_readback_done` guard the
  caller's "no" is undone on the very next call and the rejection can never stick."""
  cfg = _ani_cfg()
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  sm = _event_drive(cfg, sm, {"ani": "2124561234"})["sm"]
  sm["pending"] = {}                      # reject_pending clears the staged value
  out = _event_drive(cfg, sm, {"ani": "2124561234"}, text="no", turn=2)
  assert out["sm"]["pending"] == {}
  assert "phone" not in out["sm"]["filled"]


def test_a_confirmed_event_prefill_is_not_re_staged_either():
  cfg = _ani_cfg()
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  sm = _event_drive(cfg, sm, {"ani": "2124561234"})["sm"]
  sm["filled"]["phone"] = sm["pending"].pop("phone")   # confirm_pending
  out = _event_drive(cfg, sm, {"ani": "2124561234"}, turn=2)
  assert out["sm"]["filled"]["phone"] == "2124561234"
  assert not out["sm"]["pending"]


# --------------------------------------------------------------------------- #
# 12. `skip_readback_if_matches` — never read the same number back twice
# --------------------------------------------------------------------------- #
# The NINTH fork primitive, found on the `confirmation_sms_number` slot of the
# production Equifax app. The slot carries `requires_readback` + `readback_verbatim` +
# `readback_fmt {digits}`, which is right for a number the caller has just DICTATED and
# nobody has checked. It is redundant for one they merely re-affirm: the offer leads
# with the mobile already verified during auth ("…text it to the mobile ending in
# 0199?"), the caller says "yes, that one", and the readback then asks them to confirm
# — for the third time — digits they have already confirmed out loud.
#
# The semantics below are the FORK's, read out of its `_auto_promote_and_route` and then
# confirmed by executing both engines side by side on the same state (see
# `test_fork_parity_on_the_same_state`):
#
#   * the comparison is NORMALISED, not exact — `_digits_only` on both sides, so
#     "212-555-0199" matches "2125550199";
#   * a staged value with fewer than 5 digits never matches, because short values
#     collide by coincidence ("yes" == "yes", a menu "1" == a menu "1") and skipping a
#     readback on a coincidence silently accepts a value nobody confirmed;
#   * a listed slot that is not FILLED cannot match, so an unconfirmed source never
#     suppresses a readback;
#   * more than one slot may be listed and it is ANY-match (the loop breaks on the
#     first hit), not all-match;
#   * the skip is not cosmetic — the slot never enters the pending-confirmation state
#     at all. It is dropped from `readback_set`, so the same turn promotes it straight
#     to `filled` and no readback question is asked.
#
# The sources are DECLARED, never inferred. The fork's own comment records that its
# first cut scanned for any other `requires_readback` slot already holding the value and
# fired on nothing (measured 12/12 still reading the number back): the confirming slot
# and the confirmed one live in different configs, and the value re-enters the second as
# a task output carrying no `requires_readback` of its own. An implicit scan cannot see
# across that boundary without trusting arbitrary task outputs — which is exactly what
# it must not do. Naming the source is the whole safety property.


def _sms_cfg(skip=("phone",), readback=True):
  slot = {
      "name": "confirmation_sms_number", "source": "user", "setter": "set_sms_number",
      "ask": "What number should I text the confirmation to?",
      "readback_verbatim": True,
      "readback_fmt": {"type": "digits", "text": "the mobile phone number"},
  }
  if readback:
    slot["requires_readback"] = True
  if skip is not None:
    slot["skip_readback_if_matches"] = list(skip)
  return {
      "config_id": "sms",
      # Both sources arrive as TASK OUTPUTS — the auth-verified mobile crossing a flow
      # boundary, which is the shape that defeats an implicit scan.
      "slots": [{"name": "phone", "source": "task:load_session"},
                {"name": "alt_phone", "source": "task:load_session"},
                slot],
      "tasks": [], "gate_slot": None,
  }


def _promote(cfg, filled, pending, engine=None):
  """One engine turn from an explicit staged state; returns the disposition."""
  eng = engine or fb.load_engine()
  sm = fb.seed_sm(copy.deepcopy(cfg))
  sm["filled"], sm["pending"] = dict(filled), dict(pending)
  out = eng.slot_filling_engine({
      "raw_config": copy.deepcopy(cfg), "sm": sm, "last_user_text": "",
      "scanned_user_text": "", "is_inactivity": False,
      "event_data": {}, "config_id": "sms", "n_user_turns": 1,
  })
  return {"filled": out["sm"]["filled"], "pending": out["sm"]["pending"],
          "message": out["action"].get("message")}


SMS_READBACK_LINE = (
    "Just to confirm — the mobile phone number 2 1 2 5 5 5 0 1 9 9."
    " Is that correct?")


def test_a_staged_value_matching_a_named_slot_skips_the_readback():
  """The accept case: same digits, differently formatted. Straight to filled, and
  the pending-confirmation state is never entered — no question is asked at all."""
  d = _promote(_sms_cfg(), {"phone": "212-555-0199"},
               {"confirmation_sms_number": "2125550199"})
  assert d["filled"]["confirmation_sms_number"] == "2125550199"
  assert d["pending"] == {}
  assert d["message"] == ""


def test_a_number_matching_nothing_named_is_still_read_back():
  """The REDIRECT case — a number nobody has checked — is what the readback is for."""
  d = _promote(_sms_cfg(), {"phone": "212-555-0199"},
               {"confirmation_sms_number": "3105550123"})
  assert "confirmation_sms_number" not in d["filled"]
  assert d["pending"] == {"confirmation_sms_number": "3105550123"}
  assert d["message"] == (
      "Just to confirm — the mobile phone number 3 1 0 5 5 5 0 1 2 3."
      " Is that correct?")


def test_an_unfilled_source_slot_cannot_suppress_the_readback():
  """`phone` never arrived (the session lookup failed), so there is nothing the caller
  has already confirmed. Suppressing on an absent source would accept an unconfirmed
  number in silence — the one outcome worse than asking twice."""
  d = _promote(_sms_cfg(), {}, {"confirmation_sms_number": "2125550199"})
  assert "confirmation_sms_number" not in d["filled"]
  assert d["pending"] == {"confirmation_sms_number": "2125550199"}
  assert d["message"] == SMS_READBACK_LINE


def test_a_value_under_five_digits_never_matches():
  """The coincidence guard. Two menu answers of "1" are equal after digit-stripping
  and mean nothing; a readback skipped on that accepts a value nobody confirmed."""
  d = _promote(_sms_cfg(), {"phone": "199"}, {"confirmation_sms_number": "199"})
  assert d["pending"] == {"confirmation_sms_number": "199"}


def test_any_one_of_several_named_slots_is_enough():
  """ANY-match, not all-match: the second listed slot matching is a skip even though
  the first does not."""
  d = _promote(_sms_cfg(skip=("phone", "alt_phone")),
               {"phone": "212-555-0000", "alt_phone": "(310) 555-0123"},
               {"confirmation_sms_number": "3105550123"})
  assert d["filled"]["confirmation_sms_number"] == "3105550123"
  assert d["pending"] == {}


def test_without_the_key_the_readback_is_unchanged():
  """Additive and conditional: absent the key, the same state reads back as always."""
  d = _promote(_sms_cfg(skip=None), {"phone": "212-555-0199"},
               {"confirmation_sms_number": "2125550199"})
  assert "confirmation_sms_number" not in d["filled"]
  assert d["pending"] == {"confirmation_sms_number": "2125550199"}
  assert d["message"] == SMS_READBACK_LINE


# --- the validator ----------------------------------------------------------


def _warnings_for(cfg):
  return fb.load_validator().DagConfigValidator(cfg).validate().warnings


def test_skip_readback_if_matches_is_a_valid_slot_key():
  assert not [e for e in errors_for(_sms_cfg())
              if "skip_readback_if_matches" in e]


def test_naming_a_slot_that_does_not_exist_is_an_error():
  """A misspelled source is never in `filled`, so it never matches, so the readback
  the author believed they had suppressed keeps firing — and nothing says why."""
  errs = errors_for(_sms_cfg(skip=("phoen",)))
  assert [e for e in errs if "unknown slot 'phoen'" in e]


def test_naming_itself_is_an_error():
  errs = errors_for(_sms_cfg(skip=("confirmation_sms_number",)))
  assert [e for e in errs if "lists ITSELF" in e]


@pytest.mark.parametrize("value", ["phone", [], ["phone", ""], [7], {}])
def test_the_value_must_be_a_non_empty_list_of_slot_names(value):
  cfg = _sms_cfg()
  cfg["slots"][2]["skip_readback_if_matches"] = value
  assert [e for e in errors_for(cfg) if "must be a non-empty list" in e]


def test_a_source_slot_needs_no_readback_of_its_own():
  """The normal cross-flow case: `phone` is a task output here, confirmed in the
  config BEFORE this one. Requiring `requires_readback` on it would reject exactly
  the shape the primitive exists to serve."""
  cfg = _sms_cfg()
  assert not [d for d in errors_for(cfg) + _warnings_for(cfg)
              if "skip_readback_if_matches" in d]


def test_the_key_without_requires_readback_on_itself_is_flagged():
  """Nothing to skip, so the key is inert — and inert in the worst way, because the
  author believes the second confirmation is gone."""
  warns = _warnings_for(_sms_cfg(readback=False))
  assert [w for w in warns
          if "skip_readback_if_matches but not 'requires_readback'" in w]


# --- the authoring surface --------------------------------------------------


def test_user_slot_threads_the_key_through():
  s = user_slot("confirmation_sms_number", "What number should I text?",
                readback=True, skip_readback_if_matches=["phone"])
  assert s["skip_readback_if_matches"] == ["phone"]
  # Emitted right after `requires_readback` — render_slot's round-trip comparison is
  # key-ORDER sensitive.
  keys = list(s)
  assert keys.index("skip_readback_if_matches") == keys.index("requires_readback") + 1


def test_user_slot_refuses_the_key_without_a_readback_to_skip():
  with pytest.raises(ValueError, match="readback=True"):
    user_slot("confirmation_sms_number", "What number should I text?",
              skip_readback_if_matches=["phone"])


def test_a_config_carrying_the_key_still_renders_as_a_builder():
  """Not `raw({...})`: the renderer has to know the key or the whole slot degrades."""
  f = Flow("sms_demo", root_agent="SMS_Agent")
  f.add(user_slot("confirmation_sms_number", "What number should I text?",
                  readback=True, skip_readback_if_matches=["phone"]))
  src = render_config_source(f.to_config(), config_id="sms_demo",
                             root_agent="SMS_Agent")
  assert 'skip_readback_if_matches=["phone"]' in src
  assert "raw(" not in src.split("flow.add(", 1)[1].split("\n)", 1)[0]
  # ROUND-TRIP: executing the rendered source rebuilds the identical config.
  ns: dict = {}
  exec(src, ns)  # noqa: S102 — the round-trip IS the contract
  assert ns["flow"].to_config() == f.to_config()


# --- fork parity ------------------------------------------------------------


_FORK_ENGINE = os.environ.get("FLOWS_FORK_ENGINE", "")


@pytest.mark.skipif(
    not _FORK_ENGINE,
    reason="set FLOWS_FORK_ENGINE=<path to the fork's slot_filling_engine"
           " python_code.py> to run the live side-by-side")
def test_fork_parity_on_the_same_state():
  """Execute BOTH engines on the same state and compare the disposition.

  The fork body is not vendored (it is a 300KB app-specific divergence), so this is
  env-gated; it was run against the live app's `slot_filling_engine`, pulled from
  `apps/0c80d70e-07ae-47d1-af65-0a7d796babdb/tools`, and the four cases below agree
  exactly. The pinned strings in this section came from that run.

  What is compared is `filled`/`pending` — the DISPOSITION this primitive controls.
  Not the readback sentence: the fork's `_build_readback` speaks "I heard X for your
  Y", where the blessed default is "Just to confirm — X.", an unrelated divergence in
  a different function that has nothing to do with this key.
  """
  import importlib.util

  spec = importlib.util.spec_from_file_location("_fork_engine", _FORK_ENGINE)
  fork = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(fork)

  cases = {
      "exact match, digit-normalised":
          (_sms_cfg(), {"phone": "212-555-0199"},
           {"confirmation_sms_number": "2125550199"}),
      "no match -> readback":
          (_sms_cfg(), {"phone": "212-555-0199"},
           {"confirmation_sms_number": "3105550123"}),
      "referenced slot unfilled":
          (_sms_cfg(), {}, {"confirmation_sms_number": "2125550199"}),
      "short value, under the coincidence floor":
          (_sms_cfg(), {"phone": "199"}, {"confirmation_sms_number": "199"}),
      "any-match across two named slots":
          (_sms_cfg(skip=("phone", "alt_phone")),
           {"phone": "212-555-0000", "alt_phone": "(310) 555-0123"},
           {"confirmation_sms_number": "3105550123"}),
  }
  for label, (cfg, filled, pending) in cases.items():
    mine = _promote(cfg, filled, pending)
    theirs = _promote(cfg, filled, pending, engine=fork)
    assert (mine["filled"], mine["pending"]) == (theirs["filled"],
                                                 theirs["pending"]), label


# --------------------------------------------------------------------------- #
# 13. `validation.errors.<code>` may be a LADDER — one rung per attempt
# --------------------------------------------------------------------------- #
# The TENTH fork primitive, and the one that hid the longest, because nothing
# DECLARES it: it is a value shape, not a key. `validation.errors` maps an error code
# to a line; the fork lets that line be a LIST, walked by attempt and clamped to the
# last entry, exactly the way `reprompts` already is.
#
# The two ladders are not interchangeable, which is why the fork grew this one.
# `reprompts` escalates by attempt but is indexed by attempt ALONE, so declaring it
# throws the error code away: "I only caught four digits" and "I didn't hear a number
# at all" collapse into one sentence. Those are the two things a caller most needs
# told apart, and telling them apart is the entire reason the `errors` map exists —
# `freeze_action_dag`'s date slots distinguish seven codes.
#
# It is also the failure mode that matters most here. An unported key degrades to an
# unknown-key validation error, which is loud. This degrades to `list.format(...)` ->
# AttributeError inside `before_model`, which the callback turns into `return None`:
# slot filling silently OFF for the whole flow, every turn — the same shape as the
# `readback_fmt: digits` crash that blocked this migration in the first place. The
# fork's own comment notes it was probed against the validator before it landed and
# produced no new error and no new warning, so nothing but execution finds it.


def _ladder_cfg(missing):
  return {
      "config_id": "t",
      "slots": [{
          "name": "phone", "source": "user", "setter": "set_phone",
          "ask": "What is your mobile number?",
          "validation": {"max_retries": 3, "errors": {"missing": missing}},
      }],
      "tasks": [],
  }


def _rung(cfg, attempt, filled=None):
  """The line spoken on the `attempt`-th consecutive failure of the same slot.

  `_retries` is seeded one BELOW the attempt because the engine increments it as it
  handles the error, and `max_retries` is 3 so the ladder is walked rather than
  exhausted (attempts past the end are clamped by re-driving, below).
  """
  out = drive(cfg, {
      "filled": dict(filled or {}), "pending": {},
      "_slot_errors": [{"slot": "phone", "code": "missing"}],
      "_retries": {"slot:phone": attempt - 1},
  })
  return (out.get("action") or {}).get("message") or ""


LADDER = [
    "I didn't catch a number. What's your 10-digit mobile?",
    "Still not getting it — read me the ten digits one at a time.",
]


@pytest.mark.parametrize("attempt,expected", [
    (1, LADDER[0]),
    # Clamped, not exhausted: a rung past the end of the list repeats the last one
    # rather than raising, exactly as `reprompts` does.
    (2, LADDER[1]),
])
def test_a_list_valued_error_code_walks_one_rung_per_attempt(attempt, expected):
  assert _rung(_ladder_cfg(LADDER), attempt) == expected


def test_a_longer_ladder_is_clamped_to_its_last_rung():
  """Three rungs, two lines: the second is spoken twice rather than indexing off the
  end. `max_retries` is what bounds the attempts; the ladder only supplies wording."""
  cfg = _ladder_cfg(LADDER)
  cfg["slots"][0]["validation"]["max_retries"] = 9
  assert [_rung(cfg, n) for n in (1, 2, 3, 8)] == [
      LADDER[0], LADDER[1], LADDER[1], LADDER[1]]


def test_a_string_valued_error_code_is_unchanged():
  """Every config written before this existed takes the old path untouched."""
  cfg = _ladder_cfg("Sorry, I didn't catch that.")
  cfg["slots"][0]["validation"]["max_retries"] = 9
  for attempt in (1, 2, 5):
    assert _rung(cfg, attempt) == "Sorry, I didn't catch that."


def test_a_list_valued_error_code_no_longer_crashes_the_turn():
  """The regression this closes: `list.format` is an AttributeError, and an exception
  out of the slot-filling call is a whole turn with no slot filling at all."""
  assert _rung(_ladder_cfg(LADDER), 1)  # would raise AttributeError before this


def test_an_empty_ladder_falls_back_rather_than_indexing_off_the_end():
  """`errors: {"missing": []}` is a config mistake; silence is the worst answer to it."""
  assert _rung(_ladder_cfg([]), 1) == "Could you try that again?"


def test_a_ladder_rung_still_interpolates_filled_slots():
  """The `.format(**filled)` pass applies to the chosen rung, not to the list."""
  cfg = _ladder_cfg(["Nothing yet.", "Still nothing for {name}."])
  cfg["slots"].insert(0, {"name": "name", "source": "task:load"})
  assert _rung(cfg, 2, filled={"name": "Dana"}) == "Still nothing for Dana."


def test_a_list_valued_error_code_is_not_a_validation_error():
  """It never was — which is exactly why it had to be found by executing the config.
  Pinned so the port's `sync_source` check is not silently made redundant."""
  assert not [e for e in errors_for(_ladder_cfg(LADDER)) if "missing" in e]


# --------------------------------------------------------------------------- #
# 14. SF109 — a slot exhaust must DISPOSE of the attempt, not just speak
# --------------------------------------------------------------------------- #
# Not a ported primitive: a rule the port made necessary. `validation.on_exhaust` is
# the bottom of a slot's retry ladder, and the engine gives it exactly three ways to
# be a bottom — `fill` (resolve the slot and carry on), `then` (fire a tool) and
# `response` (emit parts). `open_slot` and `component` are rejected on a slot exhaust,
# so those three are the whole set.
#
# With `say` and none of them the exhaust branch changes NO state: the slot stays
# unfilled, `retries` is already past `max_retries`, `status` is untouched and nothing
# is emitted. The next turn re-asks the same slot and speaks the same line. It is not a
# quiet exhaust, it is a ladder with no bottom — and it reads as deliberate, because
# the `say` is nearly always a goodbye.
#
# The sibling of SF020 (a hand-off payload with no `end_session`): both are "this
# terminal speaks and never terminates". Found on the production fork, which shipped
# FIVE in one revision, one of which said "I'll let you go for now" and then did not,
# on every later turn. The counterpart in the port is divergence family D29a-e.
#
# Deliberately NARROW. A `response` carrying only text or a payload does not dispose
# either, but adjudicating part-by-part is a judgement about transfer/payload semantics
# this rule does not make — it fires only on the unambiguous shape, which is also every
# real instance seen.


def _exhaust_cfg(on_exhaust):
  return {
      "config_id": "t",
      "slots": [{
          "name": "order_number", "source": "user", "setter": "set_order",
          "ask": "What is your order number?",
          "validation": {"max_retries": 2, "on_exhaust": on_exhaust},
      }],
      "tasks": [],
  }


def _sf109(cfg):
  return [e for e in errors_for(cfg) if "disposes of nothing" in e]


def test_a_say_only_slot_exhaust_is_rejected():
  assert _sf109(_exhaust_cfg({"say": "I'll let you go for now."}))


def test_an_empty_slot_exhaust_is_rejected():
  """`{}` disposes of nothing and does not even say so."""
  assert _sf109(_exhaust_cfg({}))


@pytest.mark.parametrize("disposition", [
    {"fill": "unknown"},
    {"then": "escalate"},
    {"response": [{"type": "end_session", "reason": "completed"}]},
])
def test_each_of_the_three_dispositions_satisfies_it(disposition):
  cfg = _exhaust_cfg(dict({"say": "Sorry, I could not get that."}, **disposition))
  assert not _sf109(cfg)


def test_the_rule_is_slot_only():
  """A TASK's `on_failure.on_exhaust` is untouched: it need not resolve a slot, so
  `say` alone can be a complete disposition there. Pinned because widening this rule
  to tasks would fail configs across the suite that are not defective."""
  cfg = _exhaust_cfg({"say": "x", "then": "escalate"})
  cfg["tasks"] = [{
      "name": "lookup", "tool": "do_lookup", "outputs": {"ok": "order_number"},
      "on_failure": {"max_retries": 1, "on_exhaust": {"say": "I could not reach it."}},
  }]
  assert not _sf109(cfg)


def test_the_five_real_instances_are_all_caught():
  """The production fork's actual shapes, by copy — each says goodbye and does not."""
  for say in (
      "I'm not able to get a workable date for the lift, so I haven't scheduled one.",
      "I'm still not getting a valid mobile number, so I won't send the text.",
      "I'll let you go for now. Thanks for calling Equifax.",
  ):
    assert _sf109(_exhaust_cfg({"say": say})), say


def test_the_model_accepts_a_ladder_as_well_as_the_engine():
  """The tenth primitive's OTHER half. `flows.config.models` is a mirror of what the
  engine accepts, and it typed `errors` values as `str` — so a config the engine runs
  happily was rejected by its own model. The whitelist-drift gate compares key SETS,
  so it cannot see a value-shape widening; nothing but this would have caught it."""
  from flows.config.models import Validation

  assert Validation(errors={"missing": ["first", "second"]}).errors["missing"] == [
      "first", "second"]
  assert Validation(errors={"missing": "one line"}).errors["missing"] == "one line"
