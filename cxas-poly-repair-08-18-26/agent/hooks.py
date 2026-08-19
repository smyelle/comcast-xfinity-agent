"""Author lifecycle hooks for the Comcast repair agent.

These functions are rendered VERBATIM into the deployed agent's callback files and run
inside CES with the ambient globals the platform injects (`tools`, `context`, ...). Only
the function's own source is carried, so every helper lives inside the function that uses
it and anything beyond the emitted `from typing import Any, Optional` is imported
in-function. A module-level reference raises NameError in the deployed agent.
"""


def before_agent_callback(callback_context) -> None:
  """Resolve the full diagnostic picture before the engine evaluates the ladder."""

  SM_KEY = "sm"
  # Every status the ladder gates on. All must be present (even as "skipped") before a
  # rung is evaluated — an absent status would let a lower-priority rung win by default.
  STATUS_SLOTS = ("account_status", "outage_status", "convoy_status",
                  "network_status", "gateway_status", "wifi_status")

  state = callback_context.state
  sm = state.get(SM_KEY) or {}
  filled = sm.setdefault("filled", {})
  sm.setdefault("pending", {})
  sm.setdefault("status", "in_progress")
  sm.setdefault("task_results", {})

  def stated_problem():
    """The caller's own words, if they have said why they are calling. Else ""."""
    markers = ("transfer_to_agent", "<context>", "</context>", "<event>", "</event>")
    # A bare opener is the caller opening their mouth, not telling us anything, so it is
    # matched as a WHOLE utterance: "hi, my internet is down" states a problem and sweeps.
    openers = {
        "hello", "hi", "hey", "yo", "hiya", "morning", "afternoon", "evening",
        "good morning", "good afternoon", "good evening", "hello there", "hi there",
        "um", "uh", "erm", "hmm", "ah", "oh", "okay", "ok", "yes", "yeah", "yep",
        "hello", "anyone there", "are you there", "can you hear me", "testing",
    }
    try:
      events = list(callback_context.events or [])
    except Exception:
      # "" rather than a sentinel: a sentinel is TRUTHY and would skip the opening-turn
      # guard below. Whether this raises depends on the voice transport having attached
      # yet; turn 1+ recovers the words from `_turn_user_text`.
      return ""
    for event in events:
      if getattr(event, "author", "") not in ("user", "", None):
        continue
      for part in (event.parts() or []):
        text = (getattr(part, "text", None) or "").strip()
        if not text or any(m in text for m in markers):
          continue
        if text.startswith("<") and text.endswith(">"):
          continue  # any wholly-bracketed marker, including ones not yet named above
        bare = "".join(c for c in text.lower() if c.isalnum() or c == " ").strip()
        if bare in openers:
          continue
        return text
    return ""

  _WIFI_TIP_LATCHES = ("wifi_tip_rejoin", "wifi_tip_closer",
                       "wifi_tip_toggle", "wifi_tip_restart",
                       # The whole-house pair, counted alongside the device-specific ones
                       # so the cap is a cap on TIPS rather than on tips of one shape.
                       "wifi_tip_placement", "wifi_tip_nearby")
  _WIFI_TIP_LIMIT = 3

  # Nested like every helper here: a module-level function is not rendered into the
  # deployed agent and raises NameError inside CES, which the surrounding except swallows.
  # Two gates, for the reason the reboot answer gate exists — a question asked and answered
  # inside one turn is the model answering for the caller. The cap lives here because the
  # engine has no counter.
  def wifi_gates():
    """Derive the walkthrough's two answer gates and its tip cap."""
    # The latch is read from `filled` as well as session state: a rung's `latch` fills the
    # SLOT, and only `also_state` writes session state, which the all-clear rung has none
    # of. Safe against the hazard the two-latch gate exists for, because this callback runs
    # BEFORE the engine: on the turn a latch fires the slot is not filled yet, so the gate
    # still cannot open in the same turn the question is asked.
    for latch, allowed in (("wifi_offered", "wifi_answer_allowed"),
                           ("wifi_offered_early", "wifi_answer_allowed"),
                           ("wifi_scope_asked", "wifi_scope_allowed")):
      if (str(state.get(latch) or "") == "true"
          or str(filled.get(latch) or "") == "true"):
        filled[allowed] = "true"
        state[allowed] = "true"

    # Promote an answer captured during the sweep into the slot every tip is gated on, so
    # the walkthrough's conditions stay in one vocabulary.
    #
    # Never overwrites, so a correction cannot be heard here: this runs BEFORE the engine
    # reads the turn, and the early slot still holds the answer being corrected. "Later
    # wins" is enforced in `before_model`, the one hook with this turn's utterance in hand.
    #
    # All three together, atomically. `wifi_scope`'s own condition requires
    # `wifi_scope_asked`, and a value in a slot whose gate is shut is not merely ignored —
    # the engine DEACTIVATES the slot and drops it.
    _early = str(state.get("wifi_scope_early") or filled.get("wifi_scope_early") or "")
    if _early and not filled.get("wifi_scope"):
      for _k, _v in (("wifi_scope", _early), ("wifi_scope_asked", "true"),
                     ("wifi_scope_allowed", "true")):
        filled[_k] = _v
        state[_k] = _v

    # `wifi_tip_given` is deliberately NOT cleared here. It must be released only on a real
    # caller turn, and `_turn_n` is written by the ENGINE, so at before_agent time a caller
    # who has just spoken is indistinguishable from an inactivity tick. `before_model` does
    # it instead, off this turn's utterance.

    # Per-turn: `cost_answered` stops the fee being read twice in ONE turn and must not stop
    # it being answered again on a LATER turn. `cost_question` is cleared with it so the
    # cues have to re-match the caller actually asking.
    for _per_turn in ("cost_answered", "cost_question",
                      "already_tried", "already_tried_ack"):
      filled.pop(_per_turn, None)
      state.pop(_per_turn, None)

    given = 0
    for key in _WIFI_TIP_LATCHES:
      if str(state.get(key) or "") == "true":
        filled[key] = "true"
        given += 1
    # Derived from the same count as the cap, and here for the same reason: "any of these
    # six" IS expressible as a condition, but only as a six-leg `any` that short circuits
    # on the first leg, so five of them are never decided and the branch gate rightly
    # stops believing the rung is covered. One slot, one leaf, one place to read.
    #
    # It means "on an EARLIER turn", which is what its reader needs: this runs before the
    # engine, so a tip given on THIS turn has not written its latch yet.
    if given:
      filled["wifi_tip_spent"] = "true"
      state["wifi_tip_spent"] = "true"
    if given >= _WIFI_TIP_LIMIT:
      filled["wifi_tips_exhausted"] = "true"
      state["wifi_tips_exhausted"] = "true"

  try:
    # Counting invocations, not events: before_agent runs before the voice transport
    # attaches the caller's utterance, so events are still empty on the turn they speak.
    # The opening turn is the only one on which the caller cannot have spoken. Events stay
    # the fast path, so a text caller who opens with the problem is swept on that turn.
    opening_turn = not state.get("caller_heard")
    state["caller_heard"] = "true"

    # The lead-in interpolated into the (verbatim) account ask, and the whole of the greeting
    # difference. Greeting ONLY on a direct call's agent-first opening turn: the caller has
    # heard nothing yet. An upstream hand-off seeds `skip_greeting` (the caller was welcomed
    # one agent ago), and every later turn -- including reboot's own account ask mid-call --
    # is not the opening, so both take the bare lead-in. Set ABOVE every early return so the
    # ask always renders; a bare `{welcome_lead}` would raise, and an empty one loops. Kept
    # byte-identical to scripts.WELCOME_LEAD / WELCOME_LEAD_HANDOFF by greeting_check, since
    # a module-level reference does not survive the emission into the deployed callback.
    if opening_turn and not str(state.get("skip_greeting") or ""):
      _lead = "Welcome to Xfinity. To get started, "
    else:
      _lead = "To get started, "
    filled["welcome_lead"] = _lead
    state["welcome_lead"] = _lead

    # Seeded ABOVE the opening-turn return below. Upstream hands the number over on the
    # very first turn, and a known value missing from `filled` leaves the engine free to
    # ask the caller for a number they have already given.
    account_number = (filled.get("accountNumber")
                      or state.get("accountNumber")
                      or state.get("account_id")
                      or "")
    # Any non-empty account number sweeps, matching the source — upstream hands over
    # short, masked and redacted values, and the source still diagnoses them. Format
    # enforcement belongs to the slot's setter, on the path where the CALLER speaks it.
    if account_number:
      filled["accountNumber"] = account_number
      state["accountNumber"] = account_number

    # A caller who has not spoken gets NO sweep; this early return is what protects the
    # opening turn. It costs the router nothing: any caller with something to route HAS
    # spoken, so `stated_problem()` holds and the sweep runs on that turn — including the
    # cold "reboot my modem" that needs `device_id`.
    if not stated_problem() and opening_turn:
      state[SM_KEY] = sm
      return None
    # Past this point the caller HAS spoken. `BridgeToSweep` reads this so its "give me
    # a moment while I check your connection" can only be said when a sweep is actually
    # about to run.
    filled["caller_spoke"] = "true"

    # The outage inquiry's gates are derived rather than declarative, for the same reason
    # the reboot's answer gate is. The sweep is NOT skipped for an inquiry: this callback
    # runs BEFORE the engine cue-matches the turn's utterance, so `call_intent` is never
    # filled on the turn the caller asks. The sweep runs for everyone and the DAG decides
    # what is SPOKEN; the only cost is diagnostics that may go unused.
    if str(state.get("inquiry_answered") or "") == "true":
      filled["inquiry_answered"] = "true"
      # The consent answer counts only once the offer has actually been spoken on an
      # EARLIER turn. Offered and answered inside one turn is the model answering for
      # the caller — the same gate `reboot_answer_allowed` exists for.
      filled["full_check_allowed"] = "true"
    if str(state.get("inquiry_closed") or "") == "true":
      filled["inquiry_closed"] = "true"

    # Written by the fee rungs via `also_state`, which reaches session state and NOT
    # `filled` — and conditions are evaluated against `filled`. Without this carry the
    # re-answer rung cannot see that the schedule has already been spoken.
    if str(state.get("fee_answered_once") or "") == "true":
      filled["fee_answered_once"] = "true"


    # Opened on EVERY turn, and BEFORE the branch below: a turn that falls through to the
    # main body must still open the gates, and that is the turn the say-your-account path
    # lands on — `before_agent` runs before `set_account_number`, so the account turn
    # returns early without setting `diagnostics_triggered`.
    wifi_gates()

    if (str(state.get("diagnostics_triggered") or "") == "true"
        and filled.get("diagnostics_complete")):
      # Already swept AND the result is still in the CURRENT slot scope. The check on
      # `filled` is load-bearing, not belt-and-braces: `diagnostics_triggered` lives in
      # session state, which outlives the slot-machine wipe the engine performs when a
      # flow terminates — but the swept statuses live in `filled`, which does NOT.
      # Requiring `diagnostics_complete` in `filled` means a wiped scope falls THROUGH to
      # re-run the sweep below (the account is still in session state), so the verdict
      # lands on the turn the caller switches into repair.
      #
      # The statuses need no restoring here: they are declared `shared` (see app.py),
      # so the framework carries them across the `reset_on_complete` re-arm that empties
      # `filled`. What is left is the one thing sharing cannot express, because it is
      # DERIVED rather than remembered.
      #
      # An answer may be honoured only once the offer has ACTUALLY been made on an
      # earlier turn — `reboot_offered` is latched into session state by the rung that
      # speaks it, so seeing it here means the caller has heard the question and had a
      # turn to reply. Keying off the sweep alone is too loose: when the clarification
      # gate runs first, the sweep lands a turn BEFORE the offer.
      if str(state.get("reboot_offered") or "") == "true":
        filled["reboot_answer_allowed"] = "true"
        state["reboot_answer_allowed"] = "true"
      state[SM_KEY] = sm
      return None

    if not account_number:
      state[SM_KEY] = sm
      return None

    # The sweep below is UNCONDITIONAL by necessity, not by oversight. This callback runs
    # at slot `_00`, before the router resolves, so on turn 0 — the only turn a deferred
    # call gets — `active_flow` is still empty and there is nothing to gate on. A caller
    # headed for billing is swept anyway; the ~30 unqualified slots this seeds do not reach
    # a defer flow's caller, and the cost is a wasted sweep on every deferred call.


    # When the async path is armed, the SweepAsync task owns the sweep and this hook must
    # not race it. Nothing sets `async_sweep_armed` by default, so this is inert on every
    # ordinary call. It guards the CALL, not the whole callback: everything above still
    # runs, because a rung that reads an unresolvable placeholder makes the engine raise
    # while rendering.
    if str(state.get("async_sweep_armed") or ""):
      state[SM_KEY] = sm
      return None

    # An upstream-seeded dispatch value lives in session state, and the slot's own
    # `default` cannot see it -- a default fills an ABSENT slot, it does not promote.
    # Without this a seeded `SWAP` would be overwritten by the default on the first turn.
    for key in ("activityType", "activityCode", "jobType"):
      if state.get(key):
        filled[key] = state[key]

    # Marks the sweep DISPATCHED. Every later turn takes the "already swept" branch above,
    # and that branch is what flips `reboot_answer_allowed` to true once the offer has
    # actually been spoken.
    state["diagnostics_triggered"] = "true"

    # And the sweep itself is NOT here. It is the `Sweep` TASK, so the engine dispatches
    # it and speaks `filler_say` as it goes -- from here it holds the turn in silence,
    # because the engine has not run yet and no rung can speak.
    #
    # Nothing is defaulted here first, deliberately: seeding "skipped" or "NOT_FOUND"
    # before the sweep answers would let a lower-priority rung deliver a verdict early,
    # which is the defect `diagnostics_complete` exists to prevent.

  except Exception as exc:  # degrade to the P9 "tools errored" rung, never break the turn
    for key in STATUS_SLOTS:
      if not filled.get(key):
        filled[key] = "error"
        state[key] = "error"
    filled["diagnostics_complete"] = "true"
    state["diagnostics_complete"] = "true"
    state["diagnostics_triggered"] = "true"
    sm.setdefault("_log", []).append({
        "src": "author_before_agent", "tag": "diagnostics_failed",
        "level": "ERROR", "data": {"err": repr(exc)},
    })

  state[SM_KEY] = sm
  return None


# `before_agent` runs before the voice transport attaches the caller's utterance, so on the
# opening AUDIO turn its `stated_problem()` sees nothing, the opening-turn guard skips the
# sweep and the verdict lands a turn late. This runs a few milliseconds later, once
# `llm_request` carries the turn's utterance, and before the model routes to `repair`, so
# the status slots are in place when the engine evaluates the ladder.
def before_model_callback(callback_context, llm_request) -> None:
  """Complete the diagnostic sweep on the turn the caller speaks, over VOICE."""

  SM_KEY = "sm"

  state = callback_context.state

  # Mirrors `stated_problem()` but reads `llm_request.contents`, which before_agent has not
  # been given yet. Its own copy, because only this function's source is rendered. "" means
  # silence or a greeting: do NOT sweep.
  def real_user_text():
    """The caller's own words for THIS turn, from the request, or "" when none."""
    markers = ("transfer_to_agent", "<context>", "</context>", "<event>", "</event>")
    openers = {
        "hello", "hi", "hey", "yo", "hiya", "morning", "afternoon", "evening",
        "good morning", "good afternoon", "good evening", "hello there", "hi there",
        "um", "uh", "erm", "hmm", "ah", "oh", "okay", "ok", "yes", "yeah", "yep",
        "anyone there", "are you there", "can you hear me", "testing",
    }
    try:
      contents = list(getattr(llm_request, "contents", None) or [])
    except Exception:
      return ""
    if not contents:
      return ""
    last = contents[-1]
    if getattr(last, "role", "") != "user":
      return ""
    for part in (getattr(last, "parts", None) or []):
      text = (getattr(part, "text", None) or "").strip()
      if not text or any(m in text for m in markers):
        continue
      if text.startswith("<") and text.endswith(">"):
        continue
      bare = "".join(c for c in text.lower() if c.isalnum() or c == " ").strip()
      if bare in openers:
        continue
      return text
    return ""

  # Nested for the reason every helper here is: only this function's own source is
  # rendered into the deployed callback, so a module-level one raises NameError inside CES.
  def manufactured_turn():
    """Did the PLATFORM make this turn, rather than the caller taking it?

    True when the newest user content is nothing but markers: an ASYNCHRONOUS tool
    publishing its result (`<context>function [...] completed ...</context>`), an
    inactivity tick, a session event. A re-invoke inside the caller's own turn is NOT
    one of these -- its newest user content is still the caller's words, several
    function responses back.
    """
    try:
      contents = list(getattr(llm_request, "contents", None) or [])
    except Exception:
      return False
    for content in reversed(contents):
      if getattr(content, "role", "") != "user":
        continue
      texts = [(getattr(part, "text", None) or "").strip()
               for part in (getattr(content, "parts", None) or [])]
      texts = [t for t in texts if t]
      # A barge-in note rides as its OWN part alongside the speech, so one marker does
      # not make the turn machinery. Every part has to be one.
      return bool(texts) and all(t.startswith("<") and t.endswith(">") for t in texts)
    return False

  # An ask LADDER (`intent_slot(ask=[...])`) plays one wording per turn and holds it for
  # the whole of that turn, and it measures a turn by `_turn_n` -- which counts CALLER
  # turns. So on a turn the platform manufactured the counter never moves, the hold never
  # releases, and the ladder speaks rung one again, word for word. That is the repeat:
  # the sweep's own completion push lands the moment the clarifying question has been
  # asked, and asks it again.
  #
  # Releasing the hold is all that is needed; the ladder then advances exactly as it does
  # between caller turns. `_commit_ask_rung` still only spends a rung whose wording
  # actually reached the caller, so a manufactured turn that says nothing costs nothing.
  # Inert for every slot whose `ask` is a plain string.
  if manufactured_turn():
    _sm_ask = state.get(SM_KEY) or {}
    if _sm_ask.pop("_ask_rung_turn", None) is not None:
      state[SM_KEY] = _sm_ask

  # Release the walkthrough's one-tip-per-turn latch, but ONLY when the caller has really
  # spoken. Every tip ends in a question the caller has to go and DO something to answer,
  # which takes minutes, so a tick that frees the next tip talks through the rest of the
  # list and the exhaustion rung then hands off and ENDS THE CALL. Here rather than in
  # `before_agent` because only `real_user_text()` can tell a tick from a caller, and above
  # the early return because the walkthrough runs long after `diagnostics_triggered` is set.
  _sm_tips = state.get(SM_KEY) or {}
  if real_user_text():
    _sm_tips.setdefault("filled", {}).pop("wifi_tip_given", None)
    state.pop("wifi_tip_given", None)
    state[SM_KEY] = _sm_tips

  # A SCOPE CORRECTION, and this is the only place in the call that can hear one:
  # `wifi_scope_early` is latched (a filled slot is not collected again) and `wifi_scope`
  # was filled by the promotion in `before_agent`, which reads a value written BEFORE the
  # engine saw the correction — so without this the stale answer wins.
  #
  # THREE GUARDS, and each one is what keeps this from being a slot that flips at random:
  #   * it only ever OVERWRITES. An empty `wifi_scope` is left alone, so the ordinary
  #     capture path -- the engine's own cue match, and the promotion -- is untouched and
  #     this cannot race it.
  #   * the new value must DIFFER from the one held. Repeating yourself is not a
  #     correction.
  #   * the turn must match cues for exactly ONE value. "My laptop is fine, everything
  #     else is down" hits both lists, and a turn that could be read either way is not
  #     evidence enough to overturn an answer the caller already gave.
  #
  # The cue map is INLINED because it has to be: module-level references do not survive the
  # emission, so `app.WIFI_SCOPE_CUES` cannot be read from here. `tests/scope_check.py`
  # asserts the two are byte-identical, so the copy cannot drift without an oracle going red.
  _scope_said = real_user_text()
  if _scope_said:
    import re as re_lib

    _SCOPE_CUES = {
        "ALL_DEVICES": [r"\beverything\b(?!\s+else)", "all of them", "all devices",
                        "every device",
                        "the whole house", "nothing works", "none of them",
                        "all of it", "everywhere", "the whole place",
                        "the entire house", "all my devices", "everything in the house",
                        "nothing is working", "no devices"],
        "ONE_DEVICE": ["one device", "just one", "just my", "only my", "only one",
                       "a single", "my laptop", "my phone", "my tv",
                       "just the one", "only the", "this one", "my tablet",
                       "my computer", "my ipad", "one of them"],
    }
    _low = _scope_said.lower()
    _hits = []
    for _value, _cues in _SCOPE_CUES.items():
      for _cue in _cues:
        try:
          _found = bool(re_lib.search(_cue, _low))
        except Exception:
          _found = _cue in _low
        if _found:
          _hits.append(_value)
          break
    if len(_hits) == 1:
      _sm_scope = state.get(SM_KEY) or {}
      _scope_filled = _sm_scope.setdefault("filled", {})
      _held = str(_scope_filled.get("wifi_scope") or "")
      if _held and _held != _hits[0]:
        # All three together, for the reason the promotion sets all three: a value in a
        # slot whose gate is shut is not merely ignored, the engine DEACTIVATES the slot
        # and drops it.
        for _k, _v in (("wifi_scope", _hits[0]), ("wifi_scope_asked", "true"),
                       ("wifi_scope_allowed", "true")):
          _scope_filled[_k] = _v
          state[_k] = _v
        state[SM_KEY] = _sm_scope

  # Already swept: the text fast-path did it in before_agent, or an earlier turn did.
  # BELOW the latch release above, which has to run on every turn.
  if str(state.get("diagnostics_triggered") or "") == "true":
    return None

  # A silent/greeting turn gets no sweep, exactly as the opening-turn guard intends.
  if not real_user_text():
    return None

  sm = state.get(SM_KEY) or {}
  filled = sm.setdefault("filled", {})
  sm.setdefault("pending", {})
  sm.setdefault("status", "in_progress")
  sm.setdefault("task_results", {})

  account_number = (filled.get("accountNumber")
                    or state.get("accountNumber")
                    or state.get("account_id")
                    or "")
  # No account yet: the no-account ask path owns this turn, not the sweep.
  if not account_number:
    return None

  filled["caller_spoke"] = "true"
  for _key in ("activityType", "activityCode", "jobType"):
    if state.get(_key):
      filled[_key] = state[_key]
  state["diagnostics_triggered"] = "true"
  state[SM_KEY] = sm
  return None
