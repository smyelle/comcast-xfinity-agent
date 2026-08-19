# pylint: disable=invalid-name,undefined-variable,unused-argument,broad-exception-caught,line-too-long
"""After-model callback — payload injection for non-preempted turns.

FRAMEWORK CODE — byte-identical across all agents (no per-agent differences;
_SM_KEY == "sm" everywhere). CES cannot share a module across callbacks, so the
three copies are kept in sync by hand; test_callback_parity.py enforces it.
"""

import json as json_lib
import logging
import re
from typing import Optional


_SM_KEY = "sm"


def _tts_tail(text):
  """Pad canned text with a trailing space so the A2A/flash-live voice doesn't
  clip its final token — a brand name at sentence-end loses tail runway and gets
  cut (memory: audio-cutoff-cxas). No-op on empty/already-trailing-space text;
  invisible on text channels, punctuation untouched. Mirrors before_model."""
  return text + " " if text and not text.endswith((" ", "\n", "\t")) else text


_LEVEL_MAP = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
              "WARN": logging.WARNING, "ERROR": logging.ERROR}
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
_logger = logging.getLogger("slot_filling.after_model")


# sm["_log"] is serialized into session state every turn; cap it (ring buffer).
_LOG_CAP = 200


def _log(sm, tag, level="INFO", **data):
  """Emit structured log entry; append to sm["_log"].

  Args:
    sm: Session state machine dict (callback_context.state).
    tag: Short label identifying the log event.
    level: Severity — DEBUG, INFO, WARN, or ERROR.
    **data: Arbitrary key-value payload for the log entry.
  """
  min_level = sm.get("_log_level", "INFO")
  if _LEVEL_ORDER.get(level, 1) < _LEVEL_ORDER.get(min_level, 1):
    return
  entry = {"src": "after_model", "tag": tag, "level": level,
           "data": {k: v for k, v in data.items() if v is not None}}
  _logger.log(_LEVEL_MAP.get(level, logging.INFO),
              json_lib.dumps(entry, default=str))
  _lst = sm.setdefault("_log", [])
  _lst.append(entry)
  if len(_lst) > _LOG_CAP:
    del _lst[:-_LOG_CAP]


def _extract_response_parts(response_parts):
  """Extract the NON-spoken parts of a stashed response: payloads plus the
  terminal dispositions (end_session / transfer).

  Text parts are still skipped — the model already produced this turn's spoken
  render, so re-emitting an announce's verbatim `texts` would double-speak (see
  _supersede). But end_session and transfer are dispositions, not speech: a
  terminal announce authored `end=True, preempt=False` (or `transfer_to=`) that
  cascades on a NON-preempting turn stashes its parts here via _route_payloads,
  and dropping them left the call open / un-transferred (#719). Mirror the
  preempt path's converter in before_model (`_build_response_part`)."""
  parts = []
  for rp in response_parts:
    rp_type = rp.get("type")
    if rp_type == "payload":
      parts.append(Part.from_json(json_lib.dumps(rp["data"])))
    elif rp_type == "end_session":
      parts.append(Part.from_end_session(
          reason=rp.get("reason", "completed"),
          escalated=rp.get("escalated", False)))
    elif rp_type == "transfer":
      parts.append(Part.from_agent_transfer(agent=rp["agent"]))
  return parts


def _build_terminal_part(rp):
  """Rebuild one deferred teardown descriptor as a Part (None for anything else).

  before_model stashes these (see _DEFERRABLE_PART_TYPES) when it reroutes a terminal
  line through the model for translation; we re-inject them here so the session tears
  down AFTER the model's translated close rather than before it."""
  rp_type = (rp or {}).get("type")
  if rp_type == "end_session":
    return Part.from_end_session(
        reason=rp.get("reason", "completed"),
        escalated=rp.get("escalated", False))
  if rp_type == "payload":
    return Part.from_json(json_lib.dumps(rp.get("data", {})))
  return None


def _supersede(sm, callback_context, parts, path):
  """Return a response that stands in for text the model has ALREADY authored.

  Every call site below was written believing this SUPPRESSES that text. It does on
  `gemini-composite-v1`, where nothing has reached the caller yet. It does not on
  `gemini-3.1-flash-live`, where the words were streamed before this callback ran and
  cannot be retracted — the caller hears the model's line and then ours. Measured on
  both models and in both shapes, text and `function_call`, in ces-probes `91`: the
  `function_call` shape is no better, which was the hopeful reading.

  We still emit. The deterministic next step is the more useful of the two things the
  caller ends up hearing, and withholding it would leave them with only the line we
  were trying to replace. What changes is that the framework stops claiming to
  suppress: the log makes the double-speak countable, so the paths worth moving into
  `before_model` (the only place left that can stop the model speaking) can be chosen
  from data rather than guessed at.

  Unknown model -> treated as composite, the non-destructive read: it only means we
  skip a warning we cannot substantiate.
  """
  if "live" in (sm.get("_model") or "").lower():
    _log(sm, "supersede_not_retractable", "WARN", path=path)
  callback_context.state[_SM_KEY] = sm
  return LlmResponse.from_parts(parts=parts)


def _emit_render_fallback(sm, callback_context, path=None):
  """Speak the engine's pending `_render_fallback` (next question / readback / all-done)
  deterministically, instead of letting CES surface its "Hmm, I'm having trouble" render.

  Shared by the empty-completion backstop and the #590 redundant-setter guard: pops the
  fallback, rolls back the #513 speculative steer-back increment (this turn is being
  covered by a deterministic re-ask, so it must not count as off-topic), and re-surfaces
  the pending question's chips.

  `path` names the caller when the model ALREADY SPOKE on this turn, which routes the
  return through `_supersede` — there the fallback stands beside the model's line rather
  than replacing it (ces-probes 91). Callers on an empty or function-call turn leave it
  None: there is no streamed text to stand beside, so nothing to report.
  """
  fallback = sm.pop("_render_fallback", None)
  sm.pop("_render_fallback_lang", None)  # consumed with the fallback (relay language marker)
  if sm.pop("_steer_back_speculative", None):
    sm["_steer_back_turns"] = max(0, sm.get("_steer_back_turns", 0) - 1)
  parts = [Part.from_text(text=fallback)]
  q = sm.pop("_pending_question_payloads", None)
  if q:
    parts.extend(_extract_response_parts(q.get("parts", [])))
  if path:
    return _supersede(sm, callback_context, parts, path)
  callback_context.state[_SM_KEY] = sm
  return LlmResponse.from_parts(parts=parts)


_FRAMEWORK_TOOLS = frozenset({
    "cancel_flow", "transfer_to_human", "set_slot_change", "classify_turn_intent",
    "try_again", "new_flow_instance", "resume_flow", "confirm_pending", "reject_pending",
})


def _known_actionable_tools(sm):
  """Every tool the model can legitimately call to MAKE PROGRESS this turn: per-slot setters
  (single + multi-field), executor task tools, the app's correction/cancel/escalate tools,
  and the framework control tools. A function-call name outside this set is not a real tool
  — a malformed / unparseable completion (Gemini MALFORMED_FUNCTION_CALL-class), which CES
  would otherwise surface as its "Hmm, I'm having trouble" parser-error render.
  """
  tools = set(sm.get("_setter_slots", {}) or {})
  tools |= set(sm.get("_multi_setter_slots", {}) or {})
  tools |= set(sm.get("_executor_tasks", {}) or {})
  tools |= _FRAMEWORK_TOOLS
  for key in ("_correction_tool", "_cancel_tool", "_escalate_tool", "_intent_changed_tool"):
    val = sm.get(key)
    if val:
      tools.add(val)
  return tools


def _completion_makes_no_progress(sm, fc_names):
  """True iff NONE of the function-call names will advance the flow this turn — so, with the
  next question already armed in `_render_fallback`, after_model should speak that instead of
  letting CES render its platform fallback. #590. Two no-progress shapes:

  * a known setter whose target slot(s) are ALREADY filled (a redundant re-call), and
  * a name that is not a known tool at all (a malformed / parser-error completion).

  Any legit progress-making call returns False and proceeds untouched: a REAL first setter
  (after_model runs before intake, so the target is not yet filled), a genuine multi-tool
  turn (a setter for a still-unfilled slot), a correction, an executor/business tool, or a
  framework control tool.
  """
  if not fc_names:
    return False
  filled = sm.get("filled", {})
  setter_slots = sm.get("_setter_slots", {})
  multi = sm.get("_multi_setter_slots", {})
  known = _known_actionable_tools(sm)
  for name in fc_names:
    if name in setter_slots:
      if setter_slots[name] not in filled:
        return False  # real setter for an unfilled slot -> progress
    elif name in multi:
      targets = list((multi[name] or {}).values())
      if not targets or any(t not in filled for t in targets):
        return False  # multi-setter for a still-unfilled field -> progress
    elif name in known:
      return False  # a recognized actionable tool (executor / control / correction)
    # else: an UNKNOWN name (malformed) -> no progress; keep scanning.
  return True


def _substitute(template, ctx):
  """Safe format substitution — missing keys left as-is (and logged).

  An unresolved {token} silently shipped a broken payload to the user (a template
  typo or a missing ctx key looked exactly like intended text). We still leave the
  token in place so the turn proceeds, but WARN so the typo is diagnosable instead
  of invisible.
  """
  try:
    return template.format(**ctx)
  except (KeyError, IndexError):
    import re as _re  # pylint: disable=g-import-not-at-top
    unresolved = []
    def _repl(m):
      # `{{` / `}}` are escapes, exactly as on the .format() happy path — matched
      # FIRST so one missing key cannot change how the rest of the template
      # renders (and so a literal `{{word}}` is not reported as an unresolved key).
      if m.group(0) == "{{":
        return "{"
      if m.group(0) == "}}":
        return "}"
      key = m.group(1)
      if key in ctx:
        return str(ctx[key])
      unresolved.append(key)
      return m.group(0)
    out = _re.sub(r"\{\{|\}\}|\{(\w+)\}", _repl, template)
    if unresolved:
      _logger.warning("unresolved _substitute token(s) %s in template %r",
                      sorted(set(unresolved)), template[:120])
    return out


def _substitute_data(value, ctx):
  """Substitute tokens INSIDE a parsed payload structure.

  Substituting into the JSON *text* instead meant a slot value carrying a `"`
  produced invalid JSON, and the `json.loads` that followed raised uncaught —
  crashing the turn into the platform "having trouble" render. Walking the parsed
  structure keeps any value safe, and leaves non-string leaves (ints, bools,
  None) untouched.
  """
  if isinstance(value, str):
    return _substitute(value, ctx)
  if isinstance(value, dict):
    return {(_substitute(k, ctx) if isinstance(k, str) else k):
            _substitute_data(v, ctx) for k, v in value.items()}
  if isinstance(value, list):
    return [_substitute_data(v, ctx) for v in value]
  return value


def _missing_value(template, ctx):
  """True when `template` interpolates a value the producer did not supply (absent,
  None or ""). Mirrors the engine's own terminal value-gate
  (`_template_missing_field`), which this path had no equivalent of."""
  if not template or "{" not in template:
    return False
  import re as _re  # pylint: disable=g-import-not-at-top
  # Root field only, so a conversion / format spec ({amount:.2f}) is gated too.
  for m in _re.finditer(r"\{\{|\}\}|\{([^{}]*)\}", template):
    field = m.group(1)
    if field is None:            # an escaped brace — not an interpolation
      continue
    root = field.split("!")[0].split(":")[0].split(".")[0].split("[")[0].strip()
    if not root or root.isdigit():   # positional {} / {0} — nothing to check
      continue
    if ctx.get(root) in (None, ""):
      return True
  return False


def _resolve_terminal_response(then_response, ctx):
  """Resolve tokenized fields in then_response payloads."""
  if not then_response:
    return []
  resolved = []
  for rp in then_response:
    rp_type = rp.get("type", "text")
    if rp_type == "payload":
      resolved.append({
          "type": "payload",
          "data": _substitute_data(rp.get("data", {}), ctx),
      })
    elif rp_type == "end_session":
      resolved.append(rp)
    elif rp_type == "text":
      # Carry the descriptor's OTHER keys through — notably `interruptable: false`,
      # which the renderer below reads to disable barge-in. Dropping them made that
      # branch unreachable, so an author who asked for a non-interruptable line
      # (a legal / recording disclaimer) silently did not get one on a voice call.
      resolved.append({
          **rp,
          "type": "text",
          "text": _substitute(rp.get("text", ""), ctx),
      })
    else:
      resolved.append(rp)
  return resolved


def _try_terminal_fallback(sm, callback_context):
  """Handle terminal task when before_model didn't process it.

  When CES doesn't invoke before_model after a terminal task's tool
  completes, the engine never gets to deliver the terminal response.
  This fallback detects that case and builds the response parts.

  Args:
    sm: Slot machine state dict.
    callback_context: CES callback context object.

  Returns:
    List of Parts to use, or None if no fallback needed.
  """
  task_just = sm.get("_task_just_completed")
  if not task_just:
    return None

  executor_tasks = sm.get("_executor_tasks", {})
  task_info = None
  for info in executor_tasks.values():
    if info["task_name"] == task_just and info.get("terminal"):
      task_info = info
      break
  if not task_info:
    return None

  task_results = sm.get("task_results", {})
  result = task_results.get(task_just, {})
  success_key = task_info.get("success_check", "success")
  if not result.get(success_key):
    return None

  filled = sm.get("filled", {})
  ctx = {**filled, **result}

  parts = []
  then_say = task_info.get("then_say", "")
  # Value-gate the closing line, as the engine's own terminal path does. A then_say
  # whose interpolated value the task never produced ("All done, your confirmation
  # number is {conf}.") would falsely tell the caller the job completed AND speak the
  # raw "{conf}". Say nothing instead; a then_response text line, if any, still renders.
  if then_say and _missing_value(then_say, ctx):
    _log(sm, "then_say_value_gated", "WARN", task=task_just)
    then_say = ""
  if then_say:
    msg = _substitute(then_say, ctx)
    parts.append(Part.from_text(text=_tts_tail(msg)))

  then_response = task_info.get("then_response")
  if then_response:
    resolved = _resolve_terminal_response(then_response, ctx)
    for rp in resolved:
      rp_type = rp.get("type", "text")
      if rp_type == "text":
        if not parts:
          if rp.get("interruptable") is False and hasattr(
              Part, "from_customized_response"):
            parts.append(Part.from_customized_response(
                content=_tts_tail(rp.get("text", "")), disable_barge_in=True))
          else:
            parts.append(Part.from_text(text=_tts_tail(rp.get("text", ""))))
      elif rp_type == "audio":
        # Playable audio uses Part.from_audio ("application/json+audio"); a plain
        # JSON payload renders but never plays. Mirror before_model.
        if hasattr(Part, "from_audio"):
          parts.append(Part.from_audio(
              rp.get("audioUri", ""),
              cancellable=bool(rp.get("cancellable", False)),
              interruptable=bool(rp.get("interruptable", True)),
          ))
        else:
          audio = {"audioUri": rp.get("audioUri", "")}
          for k in ("interruptable", "cancellable", "transcript"):
            if k in rp:
              audio[k] = rp[k]
          parts.append(Part.from_json(json_lib.dumps(audio)))
      elif rp_type == "payload":
        parts.append(Part.from_json(json_lib.dumps(rp["data"])))
      elif rp_type == "end_session":
        parts.append(Part.from_end_session(
            reason=rp.get("reason", "completed"),
            escalated=rp.get("escalated", False),
        ))

  if parts:
    sm["status"] = "zombie"
    sm.pop("_task_just_completed", None)
    callback_context.state[_SM_KEY] = sm
    _log(sm, "terminal_fallback", task=task_just)

  return parts or None


def _steering_route_or_escalate(sm, callback_context):
  """Steering's POST-model routing net, for a Route-based `router_flow`.

  Runs only when the model produced no routing call on a router turn. Two behaviours,
  both driven by state vars a steering router emits (absent ⇒ this returns None and the
  turn is unchanged — a strict no-op for every other app):

  * `steering_backstop` — `{route: [keywords]}`. The model declined to route; if the
    caller's utterance matches a route's keywords, route there deterministically (a net
    UNDER the model, not a pre-model preempt).
  * `steering_disambiguate` — `{max_turns, on_exhaust}`. Count the turns the model spends
    clarifying instead of routing; once `max_turns` is reached, route to the `on_exhaust`
    route (a hand-off), bounding the clarifying loop. Below the budget, return None so the
    model's clarifying question is spoken.
  """
  state = callback_context.state
  backstop_raw = state.get("steering_backstop")
  disambig_raw = state.get("steering_disambiguate")
  if not backstop_raw and not disambig_raw:
    return None
  active = state.get("_active_config_id")
  default = state.get("default_config_id")
  if not (active and default):
    return None
  # A flow is ACTIVE (routed via cue / default / model / backstop) — any disambiguation
  # sequence is over, so clear the abstain counter and let the flow's turn proceed. This
  # also covers the cue-hit / default-preempt route, where the function-call branch's reset
  # never ran (that turn had no model call).
  if active != default:
    if sm.pop("_steering_abstain", None) is not None:
      state[_SM_KEY] = sm
    return None

  # --- router turn (active == default): the model produced no routing call ---
  boot_tool = (sm.get("_bootstrap") or {}).get("tool") or "set_active_flow"
  user_text = str(sm.get("_steering_user_text") or "").strip().lower()

  def _loads(raw):
    # Tolerate a malformed value: only a dict is usable ({} otherwise), so a list / int /
    # bare string never reaches `.items()` / `.get()`.
    try:
      val = json_lib.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
      return {}
    return val if isinstance(val, dict) else {}

  def _kw_hit(kw, text):
    # Whole-word / phrase match (word boundaries) so "pay" does not match "paycheck".
    kw = str(kw).lower().strip()
    return bool(kw) and re.search(r"\b" + re.escape(kw) + r"\b", text) is not None

  # 1) Post-model keyword backstop.
  backstop = _loads(backstop_raw)
  if user_text and backstop:
    for route, kws in backstop.items():
      if any(_kw_hit(k, user_text) for k in (kws or [])):
        sm.pop("_steering_abstain", None)
        state[_SM_KEY] = sm
        _log(sm, "steering_backstop_route", route=route)
        return LlmResponse.from_parts(
            parts=[Part.from_function_call(name=boot_tool, args={"flow": route})])

  # 2) Disambiguation budget (only count turns the caller actually spoke on).
  disambig = _loads(disambig_raw)
  if disambig and user_text:
    n = int(sm.get("_steering_abstain") or 0) + 1
    try:
      max_turns = int(disambig.get("max_turns") or 0)
    except (TypeError, ValueError):
      max_turns = 0
    on_exhaust = disambig.get("on_exhaust") or ""
    if max_turns > 0 and n >= max_turns and on_exhaust:
      sm.pop("_steering_abstain", None)
      state[_SM_KEY] = sm
      _log(sm, "steering_disambiguate_exhaust", route=on_exhaust, turns=n)
      return LlmResponse.from_parts(
          parts=[Part.from_function_call(name=boot_tool, args={"flow": on_exhaust})])
    sm["_steering_abstain"] = n
    state[_SM_KEY] = sm
    _log(sm, "steering_disambiguate_tick", turns=n)
  return None  # let the model's clarifying question stand


def after_model_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
  """Inject stashed payloads into the LLM response if present."""
  sm = callback_context.state.get(_SM_KEY, {})

  # Intent-first Pass A: before_model hid every tool but classify_turn_intent and
  # rewrote the SI to a classifier. If the model classified, let it through (CES
  # runs the setter; before_model re-invokes into Pass B).
  #
  # If the model instead emitted text (it tried to engage with / act on the current
  # request rather than calling the classifier), THAT IS `continue` — it is not
  # requesting a transition. The model reliably classifies REAL transitions
  # (switch/cancel/escalate) on the first pass; it only "refuses" to classify on
  # data-answer turns, where it wants to record the value and `continue` is already
  # the right intent. So we do NOT retry (retrying just burns model calls fighting
  # that instinct) — we accept `continue` and hop once into Pass B via try_again,
  # suppressing the model's premature text. None-safe over parts (function Parts
  # carry text=None).
  if sm.get("_classify_mode"):
    _c = getattr(llm_response, "content", None)
    _cp = getattr(_c, "parts", None) or []
    _fcs = [fc for fc in (getattr(p, "function_call", None) for p in _cp) if fc]
    _classified = any(
        getattr(fc, "name", "") == "classify_turn_intent" for fc in _fcs)
    if _classified:
      # classify present — possibly ALONGSIDE update_language, which Pass A now
      # permits (a language switch is orthogonal to intent). Let every call through;
      # CES runs the switch AND the classifier, and before_model hops into Pass B.
      callback_context.state[_SM_KEY] = sm
      return None
    # A language switch is the ONE non-classify action the injected <language_detection>
    # block legitimately demands in Pass A, and it is orthogonal to intent. If the model
    # honored that block and called update_language INSTEAD of classifying, THREAD THE
    # SWITCH THROUGH — pass the call on so CES actually flips active_language, and default
    # the intent to `continue` so the SAME turn proceeds into Pass B — rather than
    # superseding it with try_again, which would silently DROP the caller's requested
    # language switch (flows-passA-language-switching-bug: a non-default-language turn
    # could burn the turn and never switch). Byte-safe for every non-language agent: they
    # never emit update_language, so this branch is never taken.
    _lang = next((fc for fc in _fcs
                  if getattr(fc, "name", "") == "update_language"), None)
    if _lang is not None:
      sm["_pending_intent"] = "continue"
      _log(sm, "classify_update_language_threaded", "DEBUG")
      callback_context.state[_SM_KEY] = sm
      return LlmResponse.from_parts(parts=[Part.from_function_call(
          name="update_language",
          args=dict(getattr(_lang, "args", None) or {}))])
    sm["_pending_intent"] = "continue"
    _log(sm, "classify_defaulted_continue", "DEBUG")
    return _supersede(
        sm, callback_context,
        [Part.from_function_call(name="try_again", args={})],
        "classify_defaulted_continue")

  # Be robust to an empty model response: when the model returns no content
  # (content is None / no parts), this callback must NOT crash — a crash here is
  # caught upstream and turned into a hard "Hmm, I'm having trouble" fallback with
  # the whole turn's state rolled back. Treat it as "nothing to inject".
  _content = getattr(llm_response, "content", None)
  _parts = getattr(_content, "parts", None) or []
  _fc_names = [getattr(getattr(p, "function_call", None), "name", "")
               for p in _parts if getattr(p, "function_call", None)]

  # Deferred terminal teardown (relayed-copy translation). before_model rerouted a
  # canned terminal line through the model for translation under an active language lock
  # and stashed its end_session/payload parts here, to land AFTER the model's translated
  # close instead of before it. Append them to whatever the model produced and tear down.
  # If the model said nothing, speak the (authored) fallback so the caller hears the close
  # rather than silence, then still tear down. Byte-safe when unset (no language turn).
  _deferred = sm.pop("_deferred_terminal_parts", None)
  if _deferred:
    tail = [p for p in (_build_terminal_part(rp) for rp in _deferred) if p is not None]
    head = list(_parts)
    if not any((getattr(p, "text", None) or "").strip() for p in head):
      fb = sm.get("_render_fallback")
      # Under an active language switch the fallback is the canned ENGLISH copy the relay was
      # translating; speaking it verbatim is the exact leak the relay exists to prevent. Close
      # on the teardown alone (a clean silent hang-up for a terminal give-up) rather than say
      # the English line the caller must not hear.
      if fb and sm.get("_render_fallback_lang"):
        _log(sm, "relay_teardown_empty_suppressed", "WARN",
             lang=sm.get("_render_fallback_lang"))
      elif fb:
        _log(sm, "relay_teardown_empty_fallback", "WARN")
        head = [Part.from_text(text=_tts_tail(fb))]
    sm.pop("_render_fallback", None)
    sm.pop("_render_fallback_lang", None)
    _log(sm, "relay_teardown_injected", n=len(tail))
    callback_context.state[_SM_KEY] = sm
    return LlmResponse.from_parts(parts=head + tail)

  if _fc_names:
    # A routing (or any) tool call ends a steering disambiguation sequence — reset the
    # abstain counter so the next request starts fresh. No-op for every non-steering turn.
    if sm.pop("_steering_abstain", None) is not None:
      callback_context.state[_SM_KEY] = sm
    # #590: on the continuation pass after a setter, the model sometimes re-calls an
    # already-satisfied setter with NO text. The engine has advanced and armed the next
    # question in `_render_fallback`, but a function_call turn skips the empty-render
    # backstop below, so CES surfaces "Hmm, I'm having trouble" instead of the question.
    # If every call is a no-progress re-call of an already-filled setter and a fallback is
    # pending, speak the fallback. Real first setters (target not yet filled — after_model
    # runs before intake), genuine multi-tool turns (a call targeting an unfilled slot),
    # and non-setter tools (e.g. set_slot_change) all fall through to the normal return.
    if sm.get("_render_fallback") and _completion_makes_no_progress(sm, _fc_names):
      _log(sm, "render_fallback_no_progress_fc", "WARN", calls=_fc_names)
      return _emit_render_fallback(sm, callback_context)
    # _reask_question_payloads is a single-shot re-ask chip stash, valid only
    # for THIS turn's text render. The model fired a tool instead (e.g. supplied
    # the corrected value), so the re-ask did not happen — drop it so it does not
    # leak onto a later turn (the eventual readback). (_pending_* are
    # intentionally preserved across a tool turn and are NOT dropped here.)
    if sm.pop("_reask_question_payloads", None) is not None:
      callback_context.state[_SM_KEY] = sm
    return None

  # Steering post-model net (backstop keywords + disambiguation budget). Reached only when
  # the model produced NO function call — i.e. it declined to route. Fires only for a
  # Route-based steering router turn; a strict no-op for every other app (the state vars it
  # reads are emitted only by such a router).
  _steer = _steering_route_or_escalate(sm, callback_context)
  if _steer is not None:
    return _steer

  # #590 (platform-error parroting). The dominant live failure mode is NOT an empty or
  # malformed completion (the ADK LlmResponse handed to after_model exposes only content /
  # partial / turn_complete — there is no finish_reason to key off): on the continuation
  # pass after a setter fires, the model sometimes emits the platform no-match/error line
  # verbatim AS ITS OWN TEXT ("Hmm, I'm having trouble with that. Do you want me to try
  # again?" / "I'm still having trouble hearing you.") instead of the next question the
  # engine already armed in `_render_fallback`. That phrase is never a real agent turn — it
  # is the failure text itself, and (perversely) is reinforced when an app instruction names
  # it to forbid it. When a fallback is armed and the completion is that parroted error line,
  # speak the deterministic next step. Matched on the stable platform stem, so a genuine
  # answer / next question (which never contains it) is unaffected.
  _txt_lower = " ".join(
      (getattr(p, "text", None) or "") for p in _parts).lower()
  if (sm.get("_render_fallback") and "having trouble" in _txt_lower
      and ("try again" in _txt_lower or "hearing you" in _txt_lower)):
    _log(sm, "render_fallback_error_parrot", "WARN")
    return _emit_render_fallback(sm, callback_context, "error_parrot")

  # Last-resort empty-render guard. On any PROCEED turn the engine hands a
  # deterministic message to the LLM and discards it (before_model delivers the
  # engine `message` only on the preempt path); the engine stashes that text as
  # `_render_fallback`. If the model then returns no text and no tool call — a
  # flaky empty completion, most common right after a multi-tool turn — emit the
  # deterministic fallback (the readback prompt, the next question, or the
  # all-done message) plus its chips, instead of letting CES surface the platform
  # "Hmm, I'm having trouble". Naturalness is unaffected: a normal text render
  # never reaches here.
  _has_text = any((getattr(p, "text", None) or "").strip() for p in _parts)
  if not _has_text and sm.get("_render_fallback"):
    # Under an active language switch, a `_render_fallback` set by the relay path is the canned
    # ENGLISH copy the relay was translating; speaking it on an empty completion re-introduces
    # the leak. Return silence instead (the caller re-prompts / the next turn re-asks in the
    # caller's language) rather than emit English under the language lock. Only the relay path
    # marks the fallback (see _render_fallback_lang), so an ordinary proceed turn is unaffected.
    if sm.get("_render_fallback_lang"):
      _log(sm, "render_empty_relay_suppressed", "WARN",
           lang=sm.get("_render_fallback_lang"))
      sm.pop("_render_fallback", None)
      sm.pop("_render_fallback_lang", None)
      callback_context.state[_SM_KEY] = sm
      return LlmResponse.from_parts(parts=[])
    _log(sm, "render_empty_backstop", "WARN")
    return _emit_render_fallback(sm, callback_context)

  # Hard steer-back deterministic re-ask. The engine armed `_steer_reask` and let
  # the model run so a value in the user's message could still be captured. We
  # reach here only when the model fired NO setter this turn (a capture clears the
  # arm in the engine), so the user's turn produced no progress — deterministically
  # emit the firm re-ask instead of the model's free-text. This preserves the point
  # of hard steer-back (a guaranteed firm redirect) while keeping the collection
  # window recoverable (a cooperative answer is always captured first).
  _reask = sm.get("_steer_reask")
  if _reask:
    sm.pop("_steer_reask", None)
    parts = [Part.from_text(text=_reask)]
    # Re-surface the re-asked slot's chips (the engine stashed them) so a
    # deterministic re-ask is not chip-less.
    reask_parts = sm.pop("_reask_question_payloads", None)
    if reask_parts:
      parts.extend(_extract_response_parts(reask_parts))
    return _supersede(sm, callback_context, parts, "steer_reask")

  terminal_parts = _try_terminal_fallback(sm, callback_context)
  if terminal_parts:
    zombie = sm.get("_zombie", {})
    for k, v in zombie.get("exit_status", {}).items():
      callback_context.state[k] = v
    # Pop (not get): deliver the transfer once so a re-entry turn can't see a
    # stale transfer_to (the engine's zombie-transfer finalizer keys on it to
    # recover an UNdelivered transfer; leaving it set would double-dispatch).
    transfer_to = zombie.pop("transfer_to", None)
    if transfer_to:
      terminal_parts.append(Part.from_agent_transfer(agent=transfer_to))
    return _supersede(sm, callback_context, terminal_parts, "terminal_fallback")

  if (not sm.get("_pending_announce_payloads")
      and not sm.get("_pending_payloads")
      and not sm.get("_pending_question_payloads")
      and not sm.get("_reask_question_payloads")):
    return None

  # The welcome/announce card survives across a function_call (setter) turn (see
  # _route_payloads) and is delivered here on the next text render, then cleared.
  announce_card = sm.pop("_pending_announce_payloads", None)
  announce = sm.pop("_pending_payloads", None)
  question = sm.pop("_pending_question_payloads", None)
  # A re-ask (reject) whose question text the MODEL produced this turn. The
  # re-asked slot may still be `pending` (rejection snapshot not yet cleared), so
  # inject UNCONDITIONALLY rather than via the open-slot guard used for
  # _pending_question_payloads.
  reask = sm.pop("_reask_question_payloads", None)

  extra_parts = []

  if announce_card:
    extra_parts.extend(_extract_response_parts(announce_card))
  if announce:
    extra_parts.extend(_extract_response_parts(announce))

  if question:
    slot_name = question.get("slot")
    filled = sm.get("filled", {})
    pending = sm.get("pending", {})
    if slot_name and slot_name not in filled and slot_name not in pending:
      extra_parts.extend(_extract_response_parts(question.get("parts", [])))

  if reask:
    extra_parts.extend(_extract_response_parts(reask))

  if not extra_parts:
    return None

  _log(sm, "payloads_injected", "DEBUG",
       n_announce=len(_extract_response_parts(announce)) if announce else 0,
       n_question=len(_extract_response_parts(question.get("parts", []))) if question else 0)
  callback_context.state[_SM_KEY] = sm
  combined = list(_parts) + extra_parts
  return LlmResponse.from_parts(parts=combined)
