"""Two conversational primitives, driven against the real engine.

Each is written as an A/B — the same utterance through the same flow with the
primitive off and on — so the assertion is about the DIFFERENCE it makes rather
than about the fixture's particular wording.

Both close holes that could previously only be papered over with prompting:

* `no_input.hold_ack` — a caller who SAYS "hold on, let me find it" was heard
  (the engine already set hold state) but got the same question put to them
  again, which is the one reply that request rules out.
* `escalate.condition` — a disposition could be worded but never declined, so a
  flow that must refuse a hand-off in some state (an area outage, where no
  troubleshooting helps and the queue would fill with callers nobody can help)
  had no way to say so.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from flows.engine import loader as fb  # noqa: E402

FRAMEWORK_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src/flows/engine/framework/tools")
fb.set_framework_root(FRAMEWORK_ROOT)

HOLD_PHRASES = ["hold on", "give me a second", "let me find"]


def drive(cfg, sm, text, turn=1, inactivity=False):
  """One engine turn, returning the ACTION (`sm` is mutated in place)."""
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": inactivity,
      "event_data": {}, "config_id": "t", "n_user_turns": turn,
  })["action"]


def acct_cfg(no_input=None, escalate=None, extra_slots=()):
  """A one-question flow that collects an account number."""
  cfg = {
      "slots": [{
          "name": "account_number", "source": "user", "setter": "set_account",
          "ask": "What's your account number?",
      }] + list(extra_slots),
      "tasks": [],
      "gate_slot": None,
  }
  if no_input is not None:
    cfg["no_input"] = no_input
  if escalate is not None:
    cfg["escalate"] = escalate
  return cfg


def ni(hold_ack=None):
  policy = {
      "reprompts": ["Sorry, I didn't catch that. What's the account number?"],
      "hold_phrases": list(HOLD_PHRASES),
      "hold_reprompts": ["", "", "Take your time."],
      "on_exhaust": {"say": "Let me get someone to help."},
  }
  if hold_ack is not None:
    policy["hold_ack"] = hold_ack
  return policy


def fresh(cfg):
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  return sm


ACK = "No problem, take your time. I'll be here when you're ready."
HOLD_UTTERANCE = "my internet is down. hold on, I need to find my account number"


# --------------------------------------------------------------------------- #
# 1. no_input.hold_ack


def test_without_hold_ack_a_spoken_hold_still_gets_the_question():
  """The behaviour being fixed: the caller asks for time and is asked anyway."""
  cfg = acct_cfg(no_input=ni())
  out = drive(cfg, fresh(cfg), HOLD_UTTERANCE)
  assert "account number" in (out.get("message") or "").lower()


def test_hold_ack_replaces_the_question_on_the_turn_the_caller_asks():
  cfg = acct_cfg(no_input=ni(hold_ack=ACK))
  out = drive(cfg, fresh(cfg), HOLD_UTTERANCE)
  assert out.get("message") == ACK
  # Spoken verbatim rather than handed to the model to paraphrase — an
  # improvised "sure, and what IS the number?" would defeat the point.
  assert out.get("preempt") is True


def test_hold_ack_does_not_consume_the_question():
  """The slot stays awaited, so the silence ladder and a later answer both work."""
  cfg = acct_cfg(no_input=ni(hold_ack=ACK))
  sm = fresh(cfg)
  drive(cfg, sm, HOLD_UTTERANCE)
  assert sm.get("_awaiting") == "account_number"
  assert "account_number" not in (sm.get("filled") or {})
  # Hold mode is live, so the following silence uses the quiet ladder.
  assert sm.get("_hold_on") is True


def test_hold_ack_fires_once_not_on_every_later_turn():
  """`_hold_on` persists through the silence that follows; the ACK must not."""
  cfg = acct_cfg(no_input=ni(hold_ack=ACK))
  sm = fresh(cfg)
  assert drive(cfg, sm, HOLD_UTTERANCE).get("message") == ACK
  # A silent turn while still holding -> the hold_reprompts ladder, not the ack.
  out2 = drive(cfg, sm, "", turn=2, inactivity=True)
  assert out2.get("message") != ACK


def test_a_hold_phrase_that_also_supplies_the_value_still_fills():
  """"hold on -- actually it's 12345" must progress, not stall on the ack."""
  cfg = acct_cfg(no_input=ni(hold_ack=ACK))
  sm = fresh(cfg)
  sm["filled"] = {"account_number": "12345"}
  out = drive(cfg, sm, "hold on, actually it's 12345")
  assert out.get("message") != ACK


def test_hold_ack_is_inert_without_a_matching_phrase():
  cfg = acct_cfg(no_input=ni(hold_ack=ACK))
  out = drive(cfg, fresh(cfg), "my internet is down")
  assert out.get("message") != ACK


# --------------------------------------------------------------------------- #
# 2. escalate.condition / declined_say

OUTAGE_SLOT = {"name": "outage_status", "source": "event",
               "event_key": "outage_status"}
ESC_SAY = "Let me get you to a live agent."
REFUSAL = ("During an outage we can't connect you with a live agent, as "
           "troubleshooting wouldn't bring your service back.")
NO_OUTAGE = {"not": {"slot": "outage_status", "eq": "active"}}


def esc_cfg(**over):
  block = {"say": ESC_SAY}
  block.update(over)
  return acct_cfg(escalate=block, extra_slots=[OUTAGE_SLOT])


def request_escalate(cfg, outage=None):
  sm = fresh(cfg)
  if outage is not None:
    sm["filled"] = {"outage_status": outage}
  sm["pending"] = {"escalate": True}
  return sm, drive(cfg, sm, "I want to speak to a person")


def escalated(sm):
  """Did the disposition actually TERMINATE the flow?

  `_terminate` hands the exit to a zombie carrier and marks the flow "zombie";
  that carrier is the termination signal. (Its `outcome` defaults to the block
  NAME — "escalate" — unless the block sets one, so it is the wrong thing to
  assert on here.)
  """
  return sm.get("status") == "zombie" and bool(sm.get("_zombie"))


def test_unconditional_escalate_still_terminates():
  """Baseline: no condition means the disposition is always available."""
  cfg = esc_cfg()
  sm, out = request_escalate(cfg, outage="active")
  assert out.get("message") == ESC_SAY
  assert escalated(sm)


def test_escalate_is_declined_when_its_condition_is_false():
  cfg = esc_cfg(condition=NO_OUTAGE, declined_say=REFUSAL)
  sm, out = request_escalate(cfg, outage="active")
  assert out.get("message") == REFUSAL
  assert not escalated(sm)


def test_a_declined_escalate_can_be_asked_for_again():
  """Dropped, not deferred — the state it was refused for may change."""
  cfg = esc_cfg(condition=NO_OUTAGE, declined_say=REFUSAL)
  sm, _ = request_escalate(cfg, outage="active")
  assert "escalate" not in (sm.get("pending") or {})
  assert "escalate" not in (sm.get("filled") or {})


def test_escalate_runs_when_its_condition_holds():
  cfg = esc_cfg(condition=NO_OUTAGE, declined_say=REFUSAL)
  sm, out = request_escalate(cfg, outage="none")
  assert out.get("message") == ESC_SAY
  assert escalated(sm)


def test_declining_without_a_line_is_silent_not_spoken_over():
  """No declined_say -> the block simply never fires; the flow carries on."""
  cfg = esc_cfg(condition=NO_OUTAGE)
  sm, out = request_escalate(cfg, outage="active")
  assert not escalated(sm)
  assert (out.get("message") or "") != ESC_SAY


def test_a_malformed_condition_fails_open():
  """A broken condition must not silently swallow a request for a human."""
  cfg = esc_cfg(condition={"slot": "outage_status", "bogus_op": 1},
                declined_say=REFUSAL)
  sm, out = request_escalate(cfg, outage="active")
  assert out.get("message") == ESC_SAY
  assert escalated(sm)


# --------------------------------------------------------------------------- #
# 3. What the validator catches. Each of these is dead config that reads as
#    though it does something, which is the failure mode worth erroring on.


def check(cfg):
  """Validate a config, returning (errors, warnings) as message strings."""
  res = fb.load_validator().DagConfigValidator(cfg).validate()
  return ([str(e) for e in (res.errors or [])],
          [str(w) for w in (res.warnings or [])])


def test_hold_ack_without_hold_phrases_is_an_error():
  """Nothing could ever match it, so the holds would go on being ignored."""
  policy = ni(hold_ack=ACK)
  policy.pop("hold_phrases")
  errors, _ = check(acct_cfg(no_input=policy))
  assert any("hold_ack" in e and "hold_phrases" in e for e in errors), errors


def test_hold_ack_with_phrases_is_clean():
  errors, _ = check(acct_cfg(no_input=ni(hold_ack=ACK)))
  assert not any("hold_ack" in e for e in errors), errors


def test_escalate_condition_on_an_undeclared_slot_is_an_error():
  """A typo would otherwise leave the block silently always-available."""
  cfg = esc_cfg(condition={"slot": "no_such_slot", "eq": "x"})
  errors, _ = check(cfg)
  assert any("no_such_slot" in e for e in errors), errors


def test_declined_say_without_a_condition_is_an_error():
  """Nothing can decline, so the line can never be reached."""
  errors, _ = check(esc_cfg(declined_say=REFUSAL))
  assert any("declined_say" in e for e in errors), errors


def test_a_control_block_tool_key_warns_that_it_is_runtime_inert():
  """The graph draws an edge to it; the engine never calls it."""
  _, warnings = check(esc_cfg(tool="my_transfer_tool"))
  assert any("my_transfer_tool" in w and "ignored at runtime" in w
             for w in warnings), warnings


def test_a_valid_gated_escalate_is_clean():
  """The whole point: the supported shape must not trip any of the new rules."""
  cfg = esc_cfg(condition=NO_OUTAGE, declined_say=REFUSAL)
  errors, _ = check(cfg)
  assert errors == [], errors


# --------------------------------------------------------------------------- #
# 4. Regressions from wiring hold_ack to a real agent. Both are cases of state
#    arriving on the opening turn that is NOT the caller answering the question,
#    and both silently suppressed the ack in production before being scoped out.


def test_seeded_event_slots_do_not_count_as_the_caller_answering():
  """A before_agent hook resolving state up front must not suppress the ack.

  Real agents routinely seed a dozen event slots on the opening turn (an account
  lookup, a diagnostic sweep). Treating that as "they answered" meant the ack
  never fired on any of them.
  """
  cfg = acct_cfg(no_input=ni(hold_ack=ACK),
                 extra_slots=[{"name": "account_status", "source": "event",
                               "event_key": "account_status"}])
  sm = fresh(cfg)
  sm["filled"] = {"account_status": "clear"}   # as a hook would leave it
  assert drive(cfg, sm, HOLD_UTTERANCE).get("message") == ACK


def test_a_passive_cue_fill_does_not_count_as_the_caller_answering():
  """An option_cue classifying the SAME utterance is not an answer to the ask.

  The hold utterance itself ("my internet is down. hold on...") cue-fills a
  passive intent slot, which read as progress and cancelled the acknowledgement.
  """
  cfg = acct_cfg(no_input=ni(hold_ack=ACK), extra_slots=[{
      "name": "complaint_scope", "source": "user", "kind": "intent",
      "passive": True, "setter": "set_complaint_scope",
      "option_cues": {"BROAD": [r"\binternet\b"]},
  }])
  sm = fresh(cfg)
  out = drive(cfg, sm, HOLD_UTTERANCE)
  assert sm["filled"].get("complaint_scope") == "BROAD"   # the cue did fire
  assert out.get("message") == ACK                        # and the ack still won


def test_answering_the_asked_slot_still_suppresses_the_ack():
  """The guard must still work for the slot actually under collection."""
  cfg = acct_cfg(no_input=ni(hold_ack=ACK))
  sm = fresh(cfg)
  sm["filled"] = {"account_number": "12345"}
  assert drive(cfg, sm, "hold on, it's 12345").get("message") != ACK


def test_the_ack_leaves_no_state_behind_for_a_later_transition():
  """The hold-mode reset must not be suppressed on some LATER, unrelated turn.

  The signal that "an ack was just spoken" is within-turn: the ack branch sets it
  and the slot-transition reset reads it on the same pass. Held in `sm` it survived
  every turn where the awaited slot did not change — i.e. every hold that arrives
  AFTER the question was put — and then swallowed the reset on a later transition,
  starting the NEXT slot in hold mode.
  """
  cfg = acct_cfg(no_input=ni(hold_ack=ACK), extra_slots=[{
      "name": "zip_code", "source": "user", "setter": "set_zip",
      "ask": "And your postcode?"}])
  sm = fresh(cfg)
  drive(cfg, sm, "hi")                                    # asks for the number
  assert drive(cfg, sm, "hold on, let me find it", turn=2).get("message") == ACK
  assert not any(k for k in sm if "hold_ack" in k), sorted(sm)
  # And the behaviour that key was only ever a proxy for: answer, and the next slot must
  # not start in hold mode.
  drive(cfg, sm, "", turn=3)
  sm["filled"] = dict(sm.get("filled") or {}, account_number="12345")
  drive(cfg, sm, "12345", turn=4)
  assert not sm.get("_hold_on")


def test_a_declined_escalate_never_arms_the_pre_terminal_task_chain():
  """Interaction with `escalate.tasks` (the hand-off summary chain).

  The chain exists to brief the human who receives the call. If the hand-off is
  refused there is no such human, so running it would burn turns building a
  summary for nobody — and, because arming it ends the pass, would swallow the
  refusal itself. The condition is checked before the chain can arm.
  """
  cfg = esc_cfg(condition=NO_OUTAGE, declined_say=REFUSAL,
                tasks=["summarise_for_human"])
  sm, out = request_escalate(cfg, outage="active")
  assert out.get("message") == REFUSAL
  assert "_escalate_path" not in sm
  assert not escalated(sm)
