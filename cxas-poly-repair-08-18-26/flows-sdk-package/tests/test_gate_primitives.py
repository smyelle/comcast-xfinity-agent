"""The four gate primitives, driven against the real engine.

Each test is written as an A/B: the same utterance through the same flow with the
primitive off and on, so the assertion is about the DIFFERENCE the primitive makes rather
than about the demo's particular wording.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)


def drive(cfg, sm, text, turn=1):
  """One engine turn.

  `scanned_user_text` is passed explicitly: the live `before_model` always supplies it and
  it is what makes `is_routing` (the first-turn cue-fill window) real. `run_engine` omits
  it, so driving through that helper would silently lose first-turn cue fill.
  """
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False,
      "event_data": {}, "config_id": "t", "n_user_turns": turn,
  })


def base_cfg(**slot_over):
  """A one-question flow: an intent slot with overlapping cue sets."""
  slot = {
      "name": "reply", "source": "user", "kind": "intent", "setter": "set_reply",
      "ask": "Only that app, or others too?",
      "option_cues": {
          "UNSURE": [r"\bonly tried\b", r"\bnot sure\b"],
          "ONLY_APP": [r"\bonly\b", r"\bjust\b"],
      },
      "validation_rules": [{"kind": "enum", "detail": "UNSURE|ONLY_APP"}],
  }
  slot.update(slot_over)
  return {"slots": [slot], "tasks": [], "gate_slot": None}


def fresh(cfg):
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  return sm


# --------------------------------------------------------------------------- #
# 1. cue_priority
# --------------------------------------------------------------------------- #

AMBIGUOUS = "I only tried Streamly"   # matches BOTH UNSURE and ONLY_APP


def test_ambiguous_cues_fill_nothing_by_default():
  """Today's contract: two matching values => no fill, no tiebreak."""
  cfg = base_cfg()
  sm = fresh(cfg)
  drive(cfg, sm, AMBIGUOUS)
  assert "reply" not in sm["filled"]


def test_cue_priority_first_resolves_ambiguity_by_declaration_order():
  cfg = base_cfg(cue_priority="first")
  sm = fresh(cfg)
  drive(cfg, sm, AMBIGUOUS)
  assert sm["filled"]["reply"] == "UNSURE"   # declared first


def test_cue_priority_does_not_change_an_unambiguous_match():
  for mode in (None, "first"):
    cfg = base_cfg(**({"cue_priority": mode} if mode else {}))
    sm = fresh(cfg)
    drive(cfg, sm, "just that one")
    assert sm["filled"]["reply"] == "ONLY_APP", mode


# --------------------------------------------------------------------------- #
# 2. {slot|fallback}
# --------------------------------------------------------------------------- #

def _ask_text(cfg, sm):
  return (drive(cfg, sm, "hello")["action"].get("message") or "")


def test_bare_placeholder_still_renders_literal_when_unfilled():
  cfg = base_cfg(ask="Is it only {app_name} that's broken?")
  assert "{app_name}" in _ask_text(cfg, fresh(cfg))


def test_fallback_placeholder_renders_the_fallback_when_unfilled():
  cfg = base_cfg(ask="Is it only {app_name|that app} that's broken?")
  text = _ask_text(cfg, fresh(cfg))
  assert "that app" in text and "{" not in text


def test_fallback_placeholder_prefers_the_real_value():
  cfg = base_cfg(ask="Is it only {app_name|that app} that's broken?")
  sm = fresh(cfg)
  sm["filled"]["app_name"] = "Streamly"
  text = _ask_text(cfg, sm)
  assert "Streamly" in text and "that app" not in text


def test_fallback_is_not_treated_as_a_missing_field():
  """The terminal value-gate must not suppress a line whose only gap has a fallback."""
  from flows.engine import loader
  eng = loader.load_engine()
  assert eng._template_missing_field("done {conf}", {}) is True
  assert eng._template_missing_field("done {conf|shortly}", {}) is False


# --------------------------------------------------------------------------- #
# 3. validation.on_exhaust.fill
# --------------------------------------------------------------------------- #

def exhaust_cfg(**over):
  cfg = base_cfg(validation={
      "max_retries": 2,
      "on_exhaust": {"say": "No problem — I'll just check.", "fill": "UNSURE"},
  })
  cfg["slots"][0].update(over)
  return cfg


def test_unresolvable_answers_eventually_fill_and_continue():
  cfg = exhaust_cfg()
  sm = fresh(cfg)
  drive(cfg, sm, "hello", turn=1)                 # asks
  assert sm.get("_awaiting") == "reply"
  drive(cfg, sm, "mmm hard to say", turn=2)       # barren #1
  assert "reply" not in sm["filled"]
  action = drive(cfg, sm, "dunno really", turn=3)["action"]   # barren #2 -> exhaust
  assert sm["filled"]["reply"] == "UNSURE"
  assert "No problem" in (action.get("message") or "")


def test_without_fill_the_slot_never_resolves():
  """The behaviour being fixed: no ladder at all, so it re-asks forever."""
  cfg = base_cfg()          # no validation block, as intent_slot ships today
  sm = fresh(cfg)
  for turn in range(1, 5):
    drive(cfg, sm, "mmm hard to say", turn=turn)
  assert "reply" not in sm["filled"]


def test_a_productive_turn_does_not_count_against_the_question():
  """The multi-tool case: the caller answers a DIFFERENT question.

  A turn that fills some other slot is progress, so it must not consume an attempt —
  otherwise engaging usefully still defaults you into the fallback branch.
  """
  cfg = exhaust_cfg()
  cfg["slots"].append({"name": "other", "source": "user", "setter": "set_other",
                       "ask": "And your name?"})
  sm = fresh(cfg)
  drive(cfg, sm, "hello", turn=1)
  sm["filled"]["other"] = "Sam"                    # progress elsewhere
  drive(cfg, sm, "unrelated chatter", turn=2)
  assert "reply" not in sm["filled"]
  assert sm.get("_no_match", {}).get("reply") in (None, 0)


def test_silence_does_not_count_against_the_question():
  """Silence belongs to no_input; only text that resolved nothing counts here."""
  cfg = exhaust_cfg()
  sm = fresh(cfg)
  drive(cfg, sm, "hello", turn=1)
  for turn in (2, 3, 4):
    drive(cfg, sm, "", turn=turn)
  assert "reply" not in sm["filled"]


def test_repeated_engine_passes_in_one_turn_count_once():
  cfg = exhaust_cfg()
  sm = fresh(cfg)
  drive(cfg, sm, "hello", turn=1)
  for _ in range(3):
    drive(cfg, sm, "mmm", turn=2)      # same user turn, engine re-invoking
  assert "reply" not in sm["filled"]
  assert sm.get("_no_match", {}).get("reply") == 1


# --------------------------------------------------------------------------- #
# 4. intent_change.switch
# --------------------------------------------------------------------------- #

def switch_cfg(switch=None):
  ic = {"tool": "set_intent_changed", "hint": "wants something else"}
  if switch:
    ic["switch"] = switch
  return {
      "slots": [{"name": "q", "source": "user", "setter": "set_q", "ask": "Q?"}],
      "tasks": [], "gate_slot": "active_flow", "flow_types": ["support", "billing"],
      "bootstrap": {"tool": "set_active_flow", "slot": "active_flow"},
      "intent_change": ic,
  }


@pytest.mark.parametrize("switch,expected", [(None, ""), ("billing", "billing")])
def test_untargeted_switch_uses_the_authored_destination(switch, expected):
  cfg = switch_cfg(switch)
  sm = fresh(cfg)
  drive(cfg, sm, "hello", turn=1)
  sm["_classified"] = {"intent": "switch", "target": ""}   # caller named no target
  action = drive(cfg, sm, "actually something else", turn=2)["action"]
  fc = action.get("function_call") or {}
  assert fc.get("name") == "set_active_flow"
  assert fc.get("args", {}).get("flow", "") == expected


def test_a_named_target_still_wins_over_the_authored_default():
  cfg = switch_cfg("billing")
  sm = fresh(cfg)
  drive(cfg, sm, "hello", turn=1)
  sm["_classified"] = {"intent": "switch", "target": "support"}
  action = drive(cfg, sm, "no, support", turn=2)["action"]
  assert (action.get("function_call") or {}).get("args", {}).get("flow") == "support"


# --------------------------------------------------------------------------- #
# 5. escalate on the FIRST turn (the setter map must already be stashed)
# --------------------------------------------------------------------------- #

def escalate_cfg():
  """A flow with one question and an authored escalate disposition."""
  return {
      "slots": [{"name": "q", "source": "user", "setter": "set_q", "ask": "Q?"}],
      "tasks": [],
      "escalate": {"say": "Of course — connecting you now.",
                   "tool": "hand_off_to_human"},
  }


def test_escalate_tool_is_mappable_on_the_very_first_turn():
  """The control inject fires a setter; intake can only record it via _setter_slots.

  That map is stashed with the compiled config, which used to happen AFTER the inject's
  early return — so a caller who opened with "I want a person" produced a tool call
  nothing could map, and the DAG then spoke over the hand-off.
  """
  cfg = escalate_cfg()
  sm = {"filled": {}, "pending": {}, "task_results": {}, "status": "in_progress"}
  action = drive(cfg, sm, "I just want to talk to a real person", turn=1)["action"]
  assert (action.get("function_call") or {}).get("name") == "transfer_to_human"
  assert sm["_setter_slots"].get("transfer_to_human") == "escalate"


def test_escalate_disposition_applies_after_the_tool_returns():
  cfg = escalate_cfg()
  sm = {"filled": {}, "pending": {}, "task_results": {}, "status": "in_progress"}
  drive(cfg, sm, "I just want to talk to a real person", turn=1)
  # The real tool's return shape.
  sm.update(fb.run_intake(
      "transfer_to_human",
      {"stored": True, "value": True, "escalated": True, "reason": ""}, sm)["sm"])
  action = drive(cfg, sm, "", turn=1)["action"]
  assert "connecting you now" in (action.get("message") or "").lower()


# --------------------------------------------------------------------------- #
# 6. one setter, one enum
# --------------------------------------------------------------------------- #

def _enum_slot(name, options, setter=None):
  return {"name": name, "source": "user", "kind": "intent",
          "setter": setter or f"set_{name}", "ask": "?",
          "option_cues": {o: [o.lower()] for o in options},
          "validation_rules": [{"kind": "enum", "detail": "|".join(options)}]}


def test_shared_setter_with_conflicting_enums_fails_the_build():
  """Two slots on one setter with different options would silently reject valid
  answers for whichever slot lost the race, as `not_in_enum`, at runtime."""
  from flows.authoring import build
  configs = [{"slots": [_enum_slot("topic", ["a", "b"], setter="set_topic")],
              "tasks": []},
             {"slots": [_enum_slot("other", ["c", "d"], setter="set_topic")],
              "tasks": []}]
  with pytest.raises(ValueError, match="different enum options"):
    build.collect(configs, {})


def test_shared_setter_with_identical_enums_is_fine():
  """The same slot reused across flows is legitimate and must keep working."""
  from flows.authoring import build
  slot = _enum_slot("topic", ["a", "b"])
  bodies, _ = build.collect([{"slots": [slot], "tasks": []},
                             {"slots": [dict(slot)], "tasks": []}], {})
  assert "set_topic" in bodies


def test_then_say_renders_a_fallback_placeholder():
  """A `then_say` carrying `{slot|words}` must render, not raise.

  Raw `str.format` treats the whole `slot|words` as one field name and raises KeyError.
  Upstream that exception is swallowed, so the authored line silently disappears and the
  model improvises its own wording in place of the script — which is exactly what was
  observed live before this was fixed.
  """
  eng = fb.load_engine()
  task = {"name": "T", "tool": "do_it", "inputs": [], "outputs": {},
          "success_check": "success", "then_say": "Try {app_name|that app} again."}
  sm = {"filled": {}, "_task_just_completed": "T"}
  _result, task_msg, _resp = eng._handle_post_executor(
      sm, [task], {"T": {"success": True}}, {}, {}, {}, {}, "", 1, "collect", False,
      [], config={"slots": [], "tasks": [task]})
  assert task_msg == "Try that app again."


def test_then_say_prefers_the_real_value_over_the_fallback():
  eng = fb.load_engine()
  task = {"name": "T", "tool": "do_it", "inputs": [], "outputs": {},
          "success_check": "success", "then_say": "Try {app_name|that app} again."}
  sm = {"filled": {"app_name": "Streamly"}, "_task_just_completed": "T"}
  _result, task_msg, _resp = eng._handle_post_executor(
      sm, [task], {"T": {"success": True}}, {"app_name": "Streamly"}, {}, {}, {}, "",
      1, "collect", False, [], config={"slots": [], "tasks": [task]})
  assert task_msg == "Try Streamly again."


# --------------------------------------------------------------------------- #
# 7. the SI must only advertise tools the app actually declared
# --------------------------------------------------------------------------- #

def _si_for(cfg, **sm_over):
  sm = {"filled": {"q": "x"}, "pending": {}, "task_results": {},
        "status": "in_progress"}
  sm.update(sm_over)
  return drive(cfg, sm, "and something else", turn=2)["action"].get("si", "")


def test_si_does_not_name_an_intent_setter_the_app_never_declared():
  """Naming it unconditionally makes CES warn 'References to undeclared tools' every
  turn, and a model that obeys calls a tool that does not exist."""
  cfg = {"slots": [{"name": "q", "source": "user", "setter": "set_q", "ask": "Q?"}],
         "tasks": [], "gate_slot": "active_flow",
         "bootstrap": {"tool": "set_active_flow", "slot": "active_flow"}}
  assert "set_intent_changed" not in _si_for(cfg, filled={"active_flow": "f", "q": "x"})


def test_si_names_the_configured_intent_setter():
  cfg = {"slots": [{"name": "q", "source": "user", "setter": "set_q", "ask": "Q?"}],
         "tasks": [], "gate_slot": "active_flow",
         "bootstrap": {"tool": "set_active_flow", "slot": "active_flow"},
         "intent_change": {"tool": "note_intent_change"}}
  si = _si_for(cfg, filled={"active_flow": "f", "q": "x"})
  assert "note_intent_change" in si and "set_intent_changed" not in si


def test_intake_routes_the_configured_intent_setter():
  """The SI advertises `intent_change.tool`, so intake must recognise that name too.

  Otherwise an app naming its own setter has the model call a tool intake does not match:
  it falls through to the generic setter path, finds no slot mapped, and silently does
  nothing — the transition is simply lost.
  """
  sm = {"filled": {"active_flow": "f", "q": "x"}, "pending": {}, "task_results": {},
        "status": "in_progress", "_intent_changed_tool": "note_intent_change"}
  out = fb.run_intake("note_intent_change",
                      {"intent": "new_request", "target": "other"}, sm)["sm"]
  assert out.get("pending", {}).get("intent_changed") is True
  assert out.get("_classified", {}).get("intent") == "new_request"


def test_intake_still_routes_the_framework_default():
  sm = {"filled": {}, "pending": {}, "task_results": {}, "status": "in_progress"}
  out = fb.run_intake("set_intent_changed",
                      {"intent": "cancel", "target": ""}, sm)["sm"]
  assert out.get("pending", {}).get("intent_changed") is True
