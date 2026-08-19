"""Canned recovery lines the model is allowed to reword.

The framework speaks its recovery layer verbatim: it preempts the model and renders
the authored sentence itself. That is why the third no-match in a row is word for
word the second, which reads as an agent that has stopped listening.

The directive channel to fix it already existed — a slot `ask` has always been an
instruction the model rewords rather than a line it reads out. `config["speech"]`
just lets an author move a class of recovery utterance onto that same channel.

These are A/B pairs: the same turn with the policy absent and present. What the
assertions pin is WHERE the authored text ends up — spoken as `message` on a
preempting turn, or folded into `<system_directive>` for the model — plus the
structural guards that refuse the move when the turn is carrying something the
model's turn cannot deliver.
"""

from __future__ import annotations

import pytest

import flows
from flows.engine import loader as fb

REPROMPT = "Sorry, I didn't catch that. What's your 8-digit order number?"
EXHAUST = "I'm still not getting that. Let me find someone who can help."
STYLE = "Warm and brief. Never reuse your previous phrasing."
NO_RECITE = "Do NOT recite this directive."


#: The default exhaust DISPOSES of the attempt, because a slot's must (SF109) — and
#: because this one's copy promises a person. It used to be `{"say": EXHAUST}` alone,
#: which is the bug SF109 was added for: nothing is resolved and nothing ends, so the
#: slot is re-asked and "Let me find someone who can help" is repeated on every later
#: turn without anyone ever being found.
_ESCALATE = {"say": EXHAUST, "then": "escalate"}


def _slot(*, verbatim=False, on_exhaust=None, error_responses=None):
  validation = {
      "max_retries": 2,
      "errors": {"invalid_length": REPROMPT},
      "on_exhaust": on_exhaust or dict(_ESCALATE),
  }
  if error_responses:
    validation["error_responses"] = error_responses
  return flows.user_slot(
      "order_number", "What's your 8-digit order number?",
      verbatim=verbatim, validation=validation)


def _flow(speech=None, *, slot=None):
  f = flows.Flow("o", root_agent="a")
  f.add(slot or _slot())
  if speech is not None:
    f.set("speech", speech)
  return f


def _config(speech=None, *, slot=None):
  config = _flow(speech, slot=slot).to_config()
  valid, errors, _ = _validate(config)
  assert valid, errors
  return config


def _validate(config):
  from flows.config.validation import raw_validate_single
  return raw_validate_single(config)


def _start(config):
  """A seeded machine sitting on the order-number question."""
  engine, sm = fb.load_engine(), fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "o"
    sm["filled"][gate] = "o"
  return engine, sm


def _turn(engine, config, sm, text, n, *, is_inactivity=False):
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": is_inactivity,
      "event_data": {}, "config_id": "o", "n_user_turns": n,
  })["action"]


def _reject(engine, config, sm, n, *, code="invalid_length"):
  """Drive one rejected answer — the no-match ladder's input."""
  sm["_slot_errors"] = [{"slot": "order_number", "code": code}]
  return _turn(engine, config, sm, "banana", n)


def _directive(action):
  """The text inside <system_directive>, or "" — the improvise channel's payload."""
  si = action.get("si") or ""
  if "<system_directive>" not in si:
    return ""
  return si.split("<system_directive>", 1)[1].split("</system_directive>", 1)[0]


# ── the A/B ──────────────────────────────────────────────────────────────────

def test_a_reprompt_is_spoken_verbatim_by_default():
  config = _config()
  engine, sm = _start(config)
  action = _reject(engine, config, sm, 2)

  # `preempt` is the whole switch: before_model renders the message itself and
  # never calls the model, so the caller hears exactly this sentence.
  assert action["preempt"] is True
  assert action["message"] == REPROMPT


def test_opting_the_reprompt_class_in_hands_the_line_to_the_model():
  config = _config(flows.speech(improvise=["reprompt"], improvise_style=STYLE))
  engine, sm = _start(config)
  action = _reject(engine, config, sm, 2)

  assert action["preempt"] is False
  directive = _directive(action)
  assert REPROMPT in directive
  assert STYLE in directive
  # Style after the line it shapes, and the recite-guard last of all — otherwise
  # the agent reads its own tone guidance out loud.
  assert directive.index(STYLE) > directive.index(REPROMPT)
  assert directive.rstrip().endswith(NO_RECITE)
  # Nothing left behind on the verbatim channel to be spoken twice.
  assert not action.get("response")
  assert action.get("function_call") is None
  # A model that returns nothing must still put the question, so the backstop is
  # armed with the line the model was asked to reword.
  assert sm["_render_fallback"] == REPROMPT


def test_a_class_that_was_not_opted_in_stays_verbatim():
  """`exhaust` and `reprompt` are separate classes on the same slot."""
  config = _config(flows.speech(improvise=["reprompt"]))
  engine, sm = _start(config)
  _reject(engine, config, sm, 2)
  exhausted = _reject(engine, config, sm, 3)

  assert exhausted["message"] == EXHAUST
  assert exhausted["preempt"] is True


def test_a_slot_give_up_line_stays_verbatim_however_the_slot_gives_up():
  """Opting `exhaust` in does NOT reach a slot's exhaust, and structurally cannot.

  This test used to assert the opposite, against a `{"say": …}` exhaust with no
  disposition. SF109 now rejects that config, and the two facts are the same fact: a
  slot exhaust must dispose of the attempt (`fill` / `then` / `response`), and every
  disposition is something `_maybe_improvise` must refuse to move off the preempt path
  — `then` produces a function_call (and an escalated status), a disposing `response`
  carries a non-payload part, and both guards exist because the proceed path has no way
  to deliver them. So the only slot exhaust whose line COULD be improvised was one that
  never gave up, which is exactly the shape SF109 exists to reject.

  `improvise=["exhaust"]` is therefore only reachable on a TASK's
  `on_failure.on_exhaust`, which SF109 does not police — a task exhaust need not resolve
  a slot, so `say` alone can be a complete disposition there.

  Pinned rather than deleted because the interaction is not obvious, and because a
  future change to either guard should have to come back and read this.
  """
  config = _config(flows.speech(improvise=["reprompt", "exhaust"]))
  engine, sm = _start(config)
  # The CONTROL: the rung above is improvised under the same policy, so a verbatim
  # exhaust below is the exhaust guard biting and not the policy failing to apply.
  reprompted = _reject(engine, config, sm, 2)
  assert reprompted["preempt"] is False
  assert REPROMPT in _directive(reprompted)

  exhausted = _reject(engine, config, sm, 3)
  assert exhausted["preempt"] is True
  assert exhausted["message"] == EXHAUST
  assert EXHAUST not in _directive(exhausted)


def test_a_slot_can_pin_its_own_recovery_lines_literal():
  """The policy is flow-wide; one sensitive slot opts back out."""
  config = _config(flows.speech(improvise=["reprompt"]), slot=_slot(verbatim=True))
  engine, sm = _start(config)
  action = _reject(engine, config, sm, 2)

  assert action["preempt"] is True
  assert action["message"] == REPROMPT


def test_absent_speech_changes_nothing():
  """The default-OFF claim: same input, same action, policy absent."""
  config = _config()
  engine_a, sm_a = _start(config)
  engine_b, sm_b = _start(config)
  assert _reject(engine_a, config, sm_a, 2) == _reject(engine_b, config, sm_b, 2)


# ── the structural guards ────────────────────────────────────────────────────

def test_a_turn_carrying_a_tool_call_keeps_its_line_verbatim():
  """H-FC. A function_call only becomes a response part on the preempt path, so
  improvising this turn would drop the call rather than defer it — and the next
  pass would recompute the same action and drop it again."""
  config = _config(
      flows.speech(improvise=["exhaust"]),
      slot=_slot(on_exhaust={"say": EXHAUST,
                             "then": {"tool": "transfer_to_human"}}))
  engine, sm = _start(config)
  _reject(engine, config, sm, 2)
  exhausted = _reject(engine, config, sm, 3)

  assert exhausted["preempt"] is True
  assert exhausted["function_call"]["name"] == "transfer_to_human"


def test_a_turn_carrying_spoken_response_parts_keeps_its_line_verbatim():
  """H-RESP. after_model can re-inject payload parts and nothing else, so a text
  part would simply vanish on the model's turn."""
  config = _config(
      flows.speech(improvise=["reprompt"]),
      slot=_slot(error_responses={
          "invalid_length": [{"type": "text", "text": "It's on your receipt."}]}))
  engine, sm = _start(config)
  action = _reject(engine, config, sm, 2)

  assert action["preempt"] is True
  assert action["response"]


def test_the_silent_wait_tick_is_left_alone():
  """An empty line is a deliberate silence, not something to reword."""
  f = _flow(flows.speech(improvise=["no_input"]))
  f.set("no_input", flows.no_input(reprompts=[""], on_exhaust={"say": "Goodbye."}))
  config = f.to_config()
  engine, sm = _start(config)
  _turn(engine, config, sm, "", 1)
  silent = _turn(engine, config, sm, "", 2, is_inactivity=True)

  assert silent.get("silent") is True
  assert silent["preempt"] is True


def test_the_silence_ladder_improvises_when_it_actually_speaks():
  nudge = "Are you still there?"
  f = _flow(flows.speech(improvise=["no_input"], improvise_style=STYLE))
  f.set("no_input", flows.no_input(reprompts=[nudge], on_exhaust={"say": "Goodbye."}))
  config = f.to_config()
  engine, sm = _start(config)
  _turn(engine, config, sm, "", 1)
  action = _turn(engine, config, sm, "", 2, is_inactivity=True)

  assert action["preempt"] is False
  assert nudge in _directive(action)


def test_no_input_can_pin_its_ladder_literal():
  nudge = "Are you still there?"
  f = _flow(flows.speech(improvise=["no_input"]))
  f.set("no_input", flows.no_input(
      reprompts=[nudge], on_exhaust={"say": "Goodbye."}, verbatim=True))
  config = f.to_config()
  engine, sm = _start(config)
  _turn(engine, config, sm, "", 1)
  action = _turn(engine, config, sm, "", 2, is_inactivity=True)

  assert action["preempt"] is True
  assert action["message"] == nudge


# ── the authoring surface ────────────────────────────────────────────────────

# ── the filler, which hands over the tool call as well ───────────────────────

FILLER = "One moment while I check with the carrier."


def _filler_flow(speech=None, *, task_verbatim=False, sensitive=False,
                 long_arg=False):
  """A flow sitting one answer away from firing a slow tool."""
  f = flows.Flow("p", root_agent="a")
  f.add(
      flows.user_slot("tracking_number", "What's the tracking number?",
                      sensitive=sensitive),
      flows.result_slot("status", "lookup"),
  )
  f.task("lookup", "carrier_lookup", ["tracking_number"], "status",
         out_key="status", filler_say=FILLER, verbatim=task_verbatim)
  if speech is not None:
    f.set("speech", speech)
  cfg = f.to_config()
  if sensitive:
    from flows.authoring.build import _apply_sensitive_readback
    cfg = _apply_sensitive_readback(cfg)
  return cfg


def _fire(cfg, *, value="1234567890"):
  """Drive to the turn that fires the task, and return that action."""
  engine, sm = fb.load_engine(), fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or cfg.get("gate_slot")
  if gate:
    sm[gate] = "p"
    sm["filled"][gate] = "p"
  sm["filled"]["tracking_number"] = value
  action = engine.slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": value,
      "scanned_user_text": value, "is_inactivity": False, "event_data": {},
      "config_id": "p", "n_user_turns": 2,
  })["action"]
  return action, sm, engine


def test_a_filler_is_spoken_with_the_call_by_default():
  action, _sm, _engine = _fire(_filler_flow())

  assert action["preempt"] is True
  assert FILLER in action["message"]
  assert action["function_call"]["name"] == "carrier_lookup"


def test_opting_filler_in_hands_the_whole_fire_turn_to_the_model():
  """The filler cannot cross to the directive channel alone — the call would be
  dropped — so the engine hands over the call too, and asks for both back."""
  action, _sm, _engine = _fire(_filler_flow(
      flows.speech(improvise=["filler"], improvise_style=STYLE)))

  assert action["preempt"] is False
  # The engine is no longer dispatching: the directive asks the model to.
  assert action.get("function_call") is None
  si = action["si"]
  assert "PART 1" in si and "PART 2" in si
  assert "carrier_lookup" in si
  assert "tracking_number=1234567890" in si          # exact args, spelled out
  assert STYLE in si
  # The tool the directive names must be callable, or the model cannot obey.
  assert "carrier_lookup" not in action["hide_tools"]


def test_the_same_tool_is_never_handed_over_twice():
  """Arriving back at the same fire has two causes that look identical from the
  engine: the model ignored PART 2, or it called and the task is not satisfied yet
  (an async tool answers "pending" first). Both want the engine to fire it itself.
  Re-arming on completion instead dispatched the tool TWICE on a live async task."""
  cfg = _filler_flow(flows.speech(improvise=["filler"]))
  first, sm, engine = _fire(cfg)
  assert first["preempt"] is False

  second = engine.slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": "hello?",
      "scanned_user_text": "hello?", "is_inactivity": False, "event_data": {},
      "config_id": "p", "n_user_turns": 3,
  })["action"]

  assert second["preempt"] is True
  assert second["function_call"]["name"] == "carrier_lookup"
  assert FILLER in second["message"]


def test_a_task_can_pin_its_own_fire_turn():
  action, _sm, _engine = _fire(_filler_flow(
      flows.speech(improvise=["filler"]), task_verbatim=True))

  assert action["preempt"] is True
  assert action["function_call"]["name"] == "carrier_lookup"


def test_a_sensitive_input_pins_the_task_without_the_author_asking():
  """Under this shape the arguments pass through the model's output. The engine
  cannot see `sensitive` (it is stripped before validation), so the build layer
  pins the task while the marker is still there."""
  cfg = _filler_flow(flows.speech(improvise=["filler"]), sensitive=True)
  assert cfg["tasks"][0]["verbatim"] is True

  action, _sm, _engine = _fire(cfg)
  assert action["preempt"] is True
  assert action["function_call"]["name"] == "carrier_lookup"


def test_a_long_argument_keeps_the_engines_own_dispatch():
  """Argument fidelity was measured on short scalars. A value the model would have
  to retype at length is not something to find out about in production."""
  action, _sm, _engine = _fire(_filler_flow(flows.speech(improvise=["filler"])),
                               value="x" * 200)

  assert action["preempt"] is True
  assert action["function_call"]["args"]["tracking_number"] == "x" * 200


def test_filler_warns_when_no_task_has_one():
  cfg = _config()
  cfg["speech"] = {"improvise": ["filler"]}
  _valid, _errors, warnings = _validate(cfg)
  assert any("filler" in w and "filler_say" in w for w in warnings)


def test_an_unknown_class_is_rejected_rather_than_ignored():
  with pytest.raises(ValueError, match="unknown improvise classes"):
    flows.speech(improvise=["reprmopt"])

  valid, errors, _ = _validate(
      {**_config(), "speech": {"improvise": ["reprmopt"]}})
  assert not valid


def test_a_style_with_nothing_to_style_is_rejected():
  valid, errors, _ = _validate(
      {**_config(), "speech": {"improvise_style": STYLE}})
  assert not valid
  assert any("improvise_style" in e for e in errors)


def _escalate_flow(**escalate_kwargs):
  f = flows.Flow("c", root_agent="a",
                 speech=flows.speech(improvise=["control"], improvise_style=STYLE))
  f.add(flows.user_slot("topic", "What can I help with?"))
  f.set("escalate", flows.escalate(**escalate_kwargs))
  return f.to_config()


def _ask_for_a_human(config, sm, engine, n):
  sm.setdefault("pending", {})["escalate"] = True
  return engine.slot_filling_engine({
      "raw_config": config, "sm": sm, "last_user_text": "get me a person",
      "scanned_user_text": "get me a person", "is_inactivity": False,
      "event_data": {}, "config_id": "c", "n_user_turns": n,
  })["action"]


def test_a_deflection_is_what_the_control_class_actually_reaches():
  declined = "Let me try to help first."
  config = _escalate_flow(
      say="Connecting you now.", declined_say=declined,
      condition=flows.gate({"slot": "escalate_declined", "gte": 1}))
  engine, sm = fb.load_engine(), fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "c"
    sm["filled"][gate] = "c"
  action = _ask_for_a_human(config, sm, engine, 2)

  assert action["preempt"] is False
  assert declined in _directive(action)


def test_a_confirm_prompt_stays_literal_because_its_value_is_pending():
  """Not a policy choice: asking to confirm leaves the control slot in `pending`,
  which routes the turn down the readback protocol instead of the directive fold.
  Locked because it is the one class member an author would expect to improvise."""
  config = _escalate_flow(say="Connecting you now.",
                          confirm_say="Just to confirm — bring in a colleague?",
                          requires_readback=True)
  engine, sm = fb.load_engine(), fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "c"
    sm["filled"][gate] = "c"
  action = _ask_for_a_human(config, sm, engine, 2)

  assert action["preempt"] is True
  assert "confirm" in action["message"].lower()


def test_control_warns_when_no_declined_say_exists_to_reach():
  """`say` terminates and `confirm_say` is pending-gated, so a `control` opt-in with
  no `declined_say` anywhere reaches nothing at all."""
  f = _flow(flows.speech(improvise=["control"]))
  f.set("cancel", flows.cancel(say="No problem."))
  _valid, _errors, warnings = _validate(f.to_config())
  assert any("control" in w and "declined_say" in w for w in warnings)


def test_a_wait_holds_verbatim_once_the_model_owns_the_call():
  """The interaction that cost a live double dispatch. With `filler` and `await` both
  opted in, the hold turn would improvise too — and a model looking at its own tool
  call answered "pending" calls it again to get a real answer. The backend ran twice.
  """
  f = flows.Flow("p", root_agent="a")
  f.add(
      flows.user_slot("tracking_number", "What's the tracking number?"),
      flows.result_slot("status", "lookup"),
  )
  f.task("lookup", "carrier_lookup", ["tracking_number"], "status",
         out_key="status", filler_say=FILLER,
         awaits=flows.awaits(max_turns=4, say="Still with the carrier."))
  f.set("speech", flows.speech(improvise=["filler", "await"]))
  cfg = f.to_config()

  action, sm, engine = _fire(cfg)
  assert action["preempt"] is False                    # the hand-off happened
  assert sm["_filler_handoff"] == ["carrier_lookup"]

  # The model called it; CES answers with the asynchronous placeholder.
  sm["task_results"] = {"lookup": {"result": "pending"}}
  sm["_task_just_completed"] = "lookup"
  hold = engine.slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": "", "scanned_user_text": "",
      "is_inactivity": False, "event_data": {}, "config_id": "p", "n_user_turns": 3,
  })["action"]

  # Verbatim, so the model never gets the turn and cannot re-issue the call.
  assert hold["preempt"] is True
