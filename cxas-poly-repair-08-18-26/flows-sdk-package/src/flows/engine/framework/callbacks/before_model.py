# pylint: disable=invalid-name,undefined-variable,unused-argument,broad-exception-caught,line-too-long,g-doc-args,g-doc-return-or-yield,g-docstring-missing-newline,g-no-space-after-docstring-summary,g-short-docstring-punctuation
"""Before-model callback — DAG engine orchestration.

FRAMEWORK CODE — fully generic across all agents.
Config-driven: reads config_id from state (set by before_agent),
bootstrap/gate from {config_id}_dag (stashed in SM on first load).
"""
import json as json_lib
import logging
import re
import traceback
from typing import Optional


_SM_KEY = "sm"

_FRAMEWORK_SENTINEL = "<!-- slot-framework -->"

# Pass-A / <language_detection> reconciliation (flows-passA-language-switching-bug).
# The Pass-A classifier SI demands the model call ONLY classify_turn_intent, but a
# language-switching app appends a <language_detection> block to the SAME instruction
# that tells the model to call update_language BEFORE it speaks when the caller switches
# to another supported language. On a non-default-language turn those two contracts
# contradict, pulling the model off classify_turn_intent. When BOTH are present this note
# is appended to the classifier SI to reconcile them: a language switch stays allowed in
# Pass A (update_language is orthogonal to intent), but classify_turn_intent is STILL
# required. after_model then threads any update_language call through into Pass B.
_PASS_A_LANGUAGE_NOTE = (
    "\n<language_in_pass_a>\n"
    "The <language_detection> instruction above STILL applies this turn. If — and ONLY "
    "if — the caller has switched to another supported language per that block, you MAY "
    "call update_language for the switch. Even then you MUST STILL call "
    "classify_turn_intent (use `continue` for an ordinary answer). Do NOT speak or give a "
    "response yet — emit only these tool call(s).\n"
    "</language_in_pass_a>")

_EVENT_TAG_PATTERN = re.compile(r"<event>(.*?)</event>")

# The engine's proceed-turn fold of a canned line: `<system_directive>\n{msg}\n...`.
# Matched so a relay can DROP the fold of the very line it is re-issuing as a translate
# directive (see _strip_relayed_directive) — two instructions for one line let the model
# obey the English one under a language lock.
_DIRECTIVE_BLOCK = re.compile(r"\n?<system_directive>.*?</system_directive>", re.DOTALL)

_TRANSFER_MARKERS = ("transfer_to_agent", "<context>", "</context>")

# --- Relayed-copy translation under an active language lock --------------------
# Canned copy the engine SPEAKS VERBATIM (a preempt `message`) or DROPS (a terminal
# `message` the proceed-path SI fold omits) bypasses the model — the ONLY translator
# in the stack — so under a non-default caller language it leaks in the authored
# (English) wording even with the app's <language_lock> in force: the lock steers what
# the model COMPOSES, but relayed copy is never composed. When active_language is
# non-default we reroute that one line through the model as an explicit translate-and-
# speak directive and DEFER any session teardown (end_session) to after_model so it
# lands AFTER the translated close. Gated on active_language — byte-identical for every
# English / non-language turn. PHI/PCI stay literal: `verbatim`/readback copy never
# relays. See flows-passA-language-switching-bug.md and the fedex language_hooks.py
# KNOWN LIMITATION note this fix closes.
# "no_input" is a canned silence-ladder reprompt the engine SPEAKS as a verbatim preempt
# (the model never runs), so — exactly like its sibling "reprompt" (a validation re-ask) —
# it must be relayed for translation under a language lock, not left to a soft reminder the
# absent model turn can never honor (the tracking no_input Spanish leak: a `no user activity`
# tick re-asked "Please go ahead with your tracking or door tag number…" in English). An
# EMPTY no_input reprompt is a silent wait tick (no message) so _relayable_prose drops it.
_RELAY_CLASSES = frozenset({"exhaust", "control", "retry", "reprompt", "no_input"})
# Only these response descriptors can be rebuilt by after_model AFTER the model's
# translated close; a transfer / audio / interruptable:False descriptor carries hand-off
# or barge-in timing we must not reorder, so an action bearing one keeps verbatim delivery.
_DEFERRABLE_PART_TYPES = frozenset({"end_session", "payload"})
# active_language is a CES display name ("Spanish"); these tokens mean "the default /
# English base" — no relay needed. Mirrors fedex language_hooks.before_model_callback
# so the framework and the app agree on what counts as a real switch.
_DEFAULT_LANGUAGE_TOKENS = frozenset({"", "english", "en", "en-us"})

# CES delivers a user turn like "<context>no user activity detected for N seconds.
# </context>" once `audioProcessingConfig.inactivityTimeout` elapses. That is silence,
# not speech, so it must drive the flow-level no_input (silence) ladder rather than be
# handed to the model as input (see _extract). Match the FULL context-wrapped shape
# (not a bare "no user activity" substring) so ordinary speech that happens to mention
# the phrase is never mistaken for an inactivity signal.
_INACTIVITY_PATTERN = re.compile(
    r"<context>\s*no user activity\b.*?</context>",
    re.IGNORECASE | re.DOTALL)

# An ASYNCHRONOUS tool answers twice. The call itself returns a platform-substituted
# `{"result": "pending"}`; the outcome arrives one or more turns later as a synthetic
# USER turn, and after_tool is NOT fired again for it, so that turn is the ONLY delivery
# (measured — ces-probes/probes/24-async-execution). Two shapes, both observed live:
#     <context>function [tool] completed with response {json}</context>
#     <context>function [tool] failed with error {json}</context>
# The error form matters as much as the success form: unrecognized, a crashed backend
# looks identical to a slow one and the flow waits out its whole deadline instead of
# failing fast. DOTALL because CES pretty-prints the payload.
#
# The VERB is captured, not skipped. Reading it away assumed every payload says whether
# it succeeded, which a python tool's pydantic model does and an agent's reply does not —
# an agent answers `{"response": "<text>"}` with no `success` key, and so does a failure.
# At the one line that decides the verdict those two are indistinguishable, and the only
# thing that could tell them apart was this word, thrown away here. Intake now takes it as
# a FALLBACK, consulted only when the payload carries no explicit signal.
_ASYNC_DONE_PATTERN = re.compile(
    r"<context>\s*function\s*\[(?P<tool>[^\]]+)\]\s*"
    r"(?P<verb>completed with response|failed with error)\s*"
    r"(?P<body>.*?)\s*</context>",
    re.IGNORECASE | re.DOTALL)
# What intake is told, derived from the verb above.
_OUTCOME_OK = "completed"
_OUTCOME_FAILED = "failed"

# A DTMF keypress arrives as its OWN context wrapper, e.g.
# "<context>user pressed 1 on keypad.</context>" (measured live). It is deliberate user
# input, not speech, so _is_real_user_text rejects it like any marker — but the engine's
# dtmf_map fast-path (_apply_dtmf_input) needs the BARE token to fire, and CES never
# delivers one. Lift the digits out so a keypad selection resolves deterministically
# instead of depending on the model to read the wrapper from history. `keys` is a menu
# key ("1") or a whole number entry ("7231232"); # and * are valid DTMF too.
_KEYPAD_PATTERN = re.compile(
    r"<context>\s*user pressed\s+(?P<keys>[0-9#*]+)\s+on keypad\b.*?</context>",
    re.IGNORECASE | re.DOTALL)

# A BARGE-IN arrives as its own context wrapper, but ONLY when the app declares
# `audioProcessingConfig.bargeInConfig.bargeInAwareness: true` (measured live —
# ces-probes 161 for the shape, 162 for the gating):
#     <context>agent speaking was interrupted. user only heard '<prefix>' in the last
#      agent response.</context>
# The quoted string is a VERBATIM PREFIX of what the caller actually heard, and it is the
# only channel the platform offers: the agent's own history still records the full text it
# INTENDED to say, and `Event.interrupted` exists on the event type but is never set.
# Without the flag the speech is cut anyway and no wrapper is sent, so an unconfigured app
# is truncated silently — which is why `App.barge_in_awareness` now defaults on.
#
# `heard` is OPTIONAL by design. A wrapper whose body cannot be read still means "you were
# interrupted", which is enough to fire the on_interrupted policy; only the {unheard} split
# degrades. Straight and curly quotes are both accepted because the prefix is prose the
# platform composes, not a field we control.
#
# THE ENGINE UNWRAPS THIS ENVELOPE TOO — see `_BARGE_ENVELOPE` in
# tools/slot_filling_engine/.../python_code.py. Same split as DTMF and for the same
# reason: the offline simulator never loads this package. Change the wire shape and BOTH
# regexes need updating.
_BARGE_PATTERN = re.compile(
    r"<context>\s*agent speaking was interrupted\b"
    r"(?:[^<]*?user only heard\s*['\"‘’“”]"
    r"(?P<heard>.*?)['\"‘’“”])?"
    r"[^<]*?</context>",
    re.IGNORECASE | re.DOTALL)

_LEVEL_MAP = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
              "WARN": logging.WARNING, "ERROR": logging.ERROR}
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
_logger = logging.getLogger("slot_filling.before_model")


# sm["_log"] is serialized into session state every turn; cap it (ring buffer).
_LOG_CAP = 200


def _log(sm, tag, level="INFO", **data):
  """Emit a structured log entry (level DEBUG/INFO/WARN/ERROR) to sm["_log"]."""
  min_level = sm.get("_log_level", "INFO")
  if _LEVEL_ORDER.get(level, 1) < _LEVEL_ORDER.get(min_level, 1):
    return
  entry = {"src": "before_model", "tag": tag, "level": level,
           "data": {k: v for k, v in data.items() if v is not None}}
  _logger.log(_LEVEL_MAP.get(level, logging.INFO),
              json_lib.dumps(entry, default=str))
  _lst = sm.setdefault("_log", [])
  _lst.append(entry)
  if len(_lst) > _LOG_CAP:
    del _lst[:-_LOG_CAP]


def _parse_async_completions(txt):
  """Every `(tool_name, payload, outcome)` async-completion envelope in `txt`, in order.

  `outcome` is the envelope's own verdict — `completed` or `failed` — which intake uses
  only when the payload does not say for itself.

  Tolerant on purpose: the envelope is a platform convention, not a documented contract,
  so a body that will not parse degrades to `{"raw": ...}` rather than throwing — the
  same fallback the transcript viewer uses. The `result` unwrap matches after_tool, so
  both ingestion paths hand intake the identical shape.

  Plural because two waits can resolve into the same turn. One envelope per turn is the
  ordinary case (each completion arrives as its own synthetic user turn — measured in
  ces-probes/probes/24-async-execution), but with two tools outstanding nothing makes
  the platform space them out, and a completion that is merely NOT SEEN is worse than
  one that is late: the task sits until `max_turns` and reports a timeout for a backend
  that actually answered.
  """
  out = []
  for m in _ASYNC_DONE_PATTERN.finditer(txt or ""):
    tool = (m.group("tool") or "").strip()
    if not tool:
      continue
    body = (m.group("body") or "").strip()
    try:
      payload = json_lib.loads(body)
    except Exception:
      payload = {"raw": body}
    if isinstance(payload, dict) and "result" in payload:
      payload = payload["result"]
    verb = (m.group("verb") or "").strip().lower()
    outcome = _OUTCOME_FAILED if verb.startswith("failed") else _OUTCOME_OK
    out.append((tool, payload, outcome))
  return out


def _strip_async_envelopes(txt):
  """`txt` with every completion envelope removed, whitespace-normalized.

  An envelope is not speech, but it can be CO-PRESENT with speech: the caller answers
  the question the flow asked to fill the wait at the same moment the backend replies,
  and CES attaches both to one turn. Dropping the whole part would lose the caller's
  answer; keeping it whole would feed raw JSON to intent matching. Strip and keep the
  remainder — the same treatment the co-present inactivity context already gets.
  """
  return _ASYNC_DONE_PATTERN.sub(" ", txt or "").strip()


def _is_real_user_text(txt):
  """Filter out CES transfer markers and empty strings.

  txt may be None: function_call / function_response Parts carry a `text`
  attribute set to None (not absent), so callers' getattr(part, "text", "")
  returns None, not "". Coerce so .strip() never raises — this None case is what
  made an unguarded full-history scan crash on the post-setter re-invocation pass.
  """
  stripped = (txt or "").strip()
  if not stripped:
    return False
  for marker in _TRANSFER_MARKERS:
    if marker in stripped:
      return False
  return True


def _part_requires_closing(txt):
  """Does this one part represent something the CALLER did, so the platform is
  holding a turn open for it?

  Not "is this speech". The first draft asked that — `_is_real_user_text` and nothing
  else — and it was wrong in a way that reproduced the very freeze the fix exists to
  stop: `_is_real_user_text` rejects EVERY `<context>` wrapper, and two of those
  wrappers are the caller acting. A DTMF keypress is
  `<context>user pressed 1 on keypad.</context>` and a barge-in is
  `<context>agent speaking was interrupted. user only heard '…'</context>`; both are
  user-initiated, both leave a turn to close, and both were classified as "nobody
  spoke" — so a caller who pressed a key or talked over the agent during a silent hold
  hit a dead line exactly as before. This repo unwraps keypresses on the very next
  screenful (`_KEYPAD_PATTERN`, and the engine's own `_apply_dtmf_input`), so a keypad
  turn during a wait is a shipped path, not a hypothetical.

  So the question is inverted: everything closes EXCEPT the two envelopes the platform
  authors on its own initiative — the inactivity tick (`_INACTIVITY_PATTERN`) and an
  asynchronous completion delivery (`_ASYNC_DONE_PATTERN`). Those two must NOT close.
  A tick answered with no content at all is what lets the platform's inactivity clock
  re-arm and tick again (ces-probes 42 and 83, the second driven in audio); closing
  them would break the ladder and the free-polling design built on it.

  Stripped rather than matched, so speech co-present with an envelope in the SAME part
  still closes: CES attaches a marker to the turn the caller spoke in when the clock
  fires mid-word, or when a backend answers at the same moment the caller does.
  """
  stripped = (txt or "").strip()
  if not stripped:
    return False
  if _is_real_user_text(stripped):
    return True
  residue = _INACTIVITY_PATTERN.sub(" ", _strip_async_envelopes(stripped)).strip()
  return bool(residue)


def _turn_requires_closing(llm_request):
  """Is THIS turn one the platform is waiting on us to close?

  A physical question about the turn being answered, not a policy one: `answer_first`
  can decide the engine should IGNORE what the caller said on a delivery turn, and the
  platform still has a turn to close. So it reads the request rather than anything the
  engine derived from it.

  Only the last content, and only a user one: earlier turns are history.
  """
  contents = llm_request.contents or []
  last = contents[-1] if contents else None
  if last is None or getattr(last, "role", "") != "user":
    return False
  return any(_part_requires_closing(getattr(p, "text", ""))
             for p in (getattr(last, "parts", None) or []))


def _classify_turn(llm_request):
  """WHO took this turn — the caller, the platform, or nobody (we are still in one).

  The engine cannot work this out for itself. Four situations arrive there byte-identical
  on every input it has — empty `last_user_text`, unchanged `n_user_turns`, `is_inactivity`
  false: a post-setter re-invoke, an asynchronous completion push, the engine's own
  re-invoke inside that push, and a push whose ingest was skipped. Only this layer sees the
  request, so only this layer can tell them apart.

  Three values, and the third is the one that makes it non-trivial:

    "caller"        the caller did something — spoke, pressed a key, talked over us, or was
                    transferred in. There is a turn open and it is theirs.
    "manufactured"  the platform authored this turn on its own initiative: an inactivity
                    tick or an asynchronous completion delivery. Nobody is waiting to hear
                    a reply, so putting an outstanding question again just talks over
                    someone who is still thinking about it.
    "continuation"  no user-authored content at all — a function_response coming back. This
                    is another PASS inside a turn that already happened, so its kind is
                    whatever that turn's was; the engine carries the previous value forward.

  `_turn_requires_closing` is deliberately NOT reused whole, though it answers a very
  similar question and shares `_part_requires_closing` below. It returns False on a
  function_response content, which is right for its own job (there is no turn to close) and
  wrong here: a re-invoke would read as manufactured and we would go silent on a caller who
  had just spoken. Splitting "nothing to close" from "nobody took this turn" is the entire
  reason for the third value.
  """
  contents = llm_request.contents or []
  last = contents[-1] if contents else None
  if last is None or getattr(last, "role", "") != "user":
    return "continuation"
  texts = [getattr(p, "text", "") or "" for p in (getattr(last, "parts", None) or [])]
  if any(_part_requires_closing(t) for t in texts):
    return "caller"
  # Text, but none of it the caller's: the bare envelopes, which is exactly what a tick or
  # a completion delivery looks like. No text at all means no user content on the turn.
  return "manufactured" if any(t.strip() for t in texts) else "continuation"


def _si_read(llm_request):
  """Read system_instruction as `(text, part)`. `part` is the Part to write back
  into (Content/Part form) or None (plain-str / absent / other form); `text` is ""
  when there is no usable SI. The single place the Content/Part-vs-str access lives
  — `_si_write` reverses it."""
  try:
    si = llm_request.config.system_instruction
  except Exception:  # pylint: disable=broad-except
    return "", None
  if not si:
    return "", None
  if hasattr(si, "parts") and si.parts:
    return (si.parts[0].text or ""), si.parts[0]
  if isinstance(si, str):
    return si, None
  return "", None


def _si_write(llm_request, part, text):
  """Write `text` back into the system_instruction in the form `_si_read` returned
  it (Content/Part if `part`, else plain str)."""
  if part is not None:
    part.text = text
  else:
    llm_request.config.system_instruction = text


def _si_text(llm_request):
  """The current system_instruction text (or "")."""
  return _si_read(llm_request)[0]


def _has_tool(llm_request, name):
  """True iff `name` is a function tool declared on this request.

  Reads `llm_request.config.tools` — the assembled tool list on the request. It is
  polymorphic (`list[Tool | Callable | mcp.Tool | McpClientSession]`), and a function
  tool's name lives at the optional `Tool.function_declarations[].name`; callables /
  MCP / built-in tools carry no such declaration and simply never match. Best-effort:
  any absence / shape surprise (a non-iterable `tools`, a tool without
  `function_declarations`) degrades to False. It is only ever OR-combined with the
  SI-marker check, so a False here can never regress the Pass-A fix — the marker
  still fires."""
  try:
    cfg = getattr(llm_request, "config", None)
    for tool in getattr(cfg, "tools", None) or ():
      for fd in getattr(tool, "function_declarations", None) or ():
        if getattr(fd, "name", None) == name:
          return True
    return False
  except Exception:  # pylint: disable=broad-except
    return False


def _inject_phase_suffix(llm_request, phase_suffix):
  """Replace any previous framework suffix with the new one."""
  text, part = _si_read(llm_request)
  if part is None and not text:
    return  # no Part/str SI to inject into
  # rstrip the separator the PREVIOUS injection appended — otherwise each
  # re-injection keeps its "\n\n" and adds another, growing the system
  # instruction by two blank lines on every pass of the turn.
  base = (text[:text.find(_FRAMEWORK_SENTINEL)].rstrip("\n")
          if _FRAMEWORK_SENTINEL in text else text)
  _si_write(llm_request, part,
            f"{base}\n\n{_FRAMEWORK_SENTINEL}\n{phase_suffix}")


_TRANSFER_BOILERPLATE_MARKER = "You have a list of other agents to transfer to:"


def _strip_transfer_boilerplate(llm_request):
  """Remove the ADK-injected `transfer_to_agent` prose from the system prompt.

  ADK appends a multi-paragraph "You have a list of other agents to transfer
  to: …" block whenever an agent has child/parent agents. We route entirely
  deterministically (set_active_flow + framework-injected Part.from_agent_transfer
  for routing, completion, cancel, and resume) and `transfer_to_agent` is hidden,
  so this prose is dead weight on every pass AND tells the model to call a tool it
  can't see. Strip from the marker to the framework sentinel (if our suffix is
  already appended) or to end-of-text. Covers parent (flow agent) and no-parent
  (Host) variants of the block.
  """
  text, part = _si_read(llm_request)
  idx = text.find(_TRANSFER_BOILERPLATE_MARKER)
  if idx == -1:
    return
  end = text.find(_FRAMEWORK_SENTINEL, idx)
  if end == -1:
    end = len(text)
  cleaned = text[:idx].rstrip()
  tail = text[end:]
  if tail:
    cleaned = cleaned + "\n\n" + tail.lstrip("\n")
  _si_write(llm_request, part, cleaned)


def _record_si(sm, llm_request, tag, model_invoked, **meta):
  """Append a per-pass SI snapshot to sm['_si_trace'] when capture is enabled.

  Powers the `--with-si` inspector: shows exactly what the LLM saw on each
  before_model pass that reaches the model (model_invoked=True, full SI text),
  and what the framework did deterministically on short-circuit passes
  (model_invoked=False, e.g. action='inject:resume_flow' / 'preempt'). The
  buffer is reset once per turn by before_agent; capture is off by default.
  """
  if not sm.get("_capture_si"):
    return
  try:
    trace = sm.setdefault("_si_trace", [])
    if len(trace) >= 12:  # backstop against a runaway pass loop
      return
    entry = {
        "pass": len(trace),
        "tag": tag,
        "model_invoked": model_invoked,
        "config": sm.get("_config_id"),
    }
    if model_invoked:
      entry["si"] = _si_text(llm_request)
    for k, v in meta.items():
      if v is not None:
        entry[k] = v
    trace.append(entry)
  except Exception:  # pylint: disable=broad-except
    pass  # capture is debug-only; never let it break the agent


def _ok(callback_context, llm_request, sm, tag):
  """Persist sm, record the SI the LLM is about to see, and return OK."""
  _record_si(sm, llm_request, tag, True)
  callback_context.state[_SM_KEY] = sm
  return {"decision": "OK"}


def _apply_state_writes(callback_context, writes):
  """Apply an action's {set, pop} writes to CES state."""
  for k, v in (writes.get("set") or {}).items():
    callback_context.state[k] = v
  for k in (writes.get("pop") or []):
    callback_context.state.pop(k, None)


# A CES bracket audio tag: [whispers], [calm], [slow]. Deliberately narrow — a
# `{template}` and an SSML `<tag>` are different things handled elsewhere.
_AUDIO_TAG = re.compile(r"\[[a-zA-Z][a-zA-Z ]{1,20}\]")


def _safe_spoken(text, partial):
  """Strip bracket audio tags from a part that is marked ``partial``.

  Measured on gemini-composite-v1 (ces-probes 86): a partial part carrying a tag
  truncates at ~1.9s AND reads the markup aloud, so the caller hears "left bracket
  whispers right bracket" and then half a sentence. Untagged partial parts are fine
  on both models, and a tag in a NON-partial part is left alone — it is honoured on
  composite and merely read aloud on flash-live, which the linter flags (FLV002).
  """
  return _AUDIO_TAG.sub("", text).strip() if partial and text else text


def _tts_tail(text):
  """Pad canned text with a trailing space so the A2A/flash-live voice doesn't
  clip its final token — a brand name at sentence-end loses tail runway and gets
  cut (memory: audio-cutoff-cxas). No-op on empty/already-trailing-space text;
  invisible on text channels, punctuation untouched."""
  return text + " " if text and not text.endswith((" ", "\n", "\t")) else text


def _resolve_end_params(state, sm):
  """The flow-level terminal return, resolved from `on_end` config, as end_session `params`.

  `flows.end_params_handoff(...)` declares `on_end = {delivery:"end_params", envelope,
  from_state}`, which the engine surfaces into `sm["_on_end"]`. The return itself is a SESSION
  variable the app stages into `from_state` — and session variables live in
  `callback_context.state`, NOT the engine's sm — so it is resolved HERE and wrapped as
  `{envelope: <staged>}`. Returns None (a strict no-op) unless on_end is an `end_params` spec
  AND the staged value is a non-empty dict — the engine stays agnostic to the return's shape
  (xHeaders, endSession, or any channel-specific keys); the flow owns what it stages."""
  on_end = sm.get("_on_end")
  if not isinstance(on_end, dict) or on_end.get("delivery") != "end_params":
    return None
  envelope = on_end.get("envelope")
  from_state = on_end.get("from_state")
  if not envelope or not from_state:
    return None
  try:
    staged = state.get(from_state)
  except Exception:  # pylint: disable=broad-except
    # A failing state backend must not silently drop the return: log it so a native-channel
    # routing dropout is diagnosable, then fall through to a bare end (no return).
    _logger.warning(
        "on_end: reading staged return from state[%r] failed; sending a bare end with no "
        "native-channel return", from_state, exc_info=True)
    return None
  if isinstance(staged, dict) and staged:
    return {envelope: staged}
  return None


def _attach_call_params(function_call, params):
  """Attach `params` onto a function call's args across runtime arg shapes; True on success.

  The concrete type of `function_call.args` varies by runtime: the CES runtime exposes a
  mutable mapping (google-genai `FunctionCall.args` is a plain dict) where item assignment
  works; a protobuf `Struct` accepts the same `[]=` (converting a nested dict recursively) and
  also `.update()`; some protobuf map containers reject item assignment but honour `update()`.
  Try item assignment, then `update()`, and WARN only if neither takes — a native-channel
  return silently dropped onto a bare end is a routing failure the caller would never see."""
  args = getattr(function_call, "args", None)
  last_err = None
  if args is not None:
    try:
      args["params"] = params
      return True
    except Exception as err:  # pylint: disable=broad-except
      last_err = err
    try:
      args.update({"params": params})
      return True
    except Exception as err:  # pylint: disable=broad-except
      last_err = err
  _logger.warning(
      "on_end: could not attach params to the end_session tool call (args=%s); the "
      "native-channel return will NOT reach the wire (a bare end was sent)",
      type(args).__name__ if args is not None else None, exc_info=last_err)
  return False


def _end_session_part(rp, extra_params=None):
  """An `end_session` Part, carrying any vendor `params` onto the tool call's args.

  A native telephony channel (e.g. FIVE9) returns its outbound SIP headers ONLY from the
  `end_session` tool call's `params` — a session variable never reaches the wire. The return
  is attached to the SAME end_session part the engine already emits, so it and the teardown
  travel as one unit in the engine's own ordering — no separate hook has to race the close.
  `extra_params` is the resolved flow-level `on_end` return (see _resolve_end_params); an
  explicit `params` on the descriptor wins. `Part.from_end_session` takes no `params` argument,
  so it is folded onto the built function_call's args via `_attach_call_params` (protobuf-safe,
  and it warns rather than swallow if no runtime path delivers it)."""
  part = Part.from_end_session(reason=rp.get("reason", "completed"),
                               escalated=rp.get("escalated", False))
  params = rp.get("params") or extra_params
  if params:
    _attach_call_params(part.function_call, params)
  return part


def _terminal_emit_parts(action, sm, end_params):
  """Deterministic terminal parts for a leg whose flow declares a native-channel return.

  A terminal turn's end MUST NOT be left to the model: on the live model a `message=` terminal
  announce is rendered as speech and the model then FREEFORM-ends the call with a BARE
  end_session, so the return never reaches the wire (measured on the FIVE9 dev line). This
  builds the terminal turn deterministically instead — the spoken close plus the `end_session`
  carrying `end_params` — for before_model to PREEMPT with (the model never runs).

  Returns `(parts, stash_keys)` when this turn is terminal (an `end_session` descriptor is
  present, inline on `action['response']` or in the proceed-path stash), else `(None, None)`
  so the caller falls through to normal handling. `stash_keys` names the pending stashes the
  caller must clear so after_model does not ALSO deliver the end.

  Every sibling descriptor is preserved through the same `_build_response_part` the normal
  emit uses — audio chimes, payload cards, and non-bargeable disclaimers (`interruptable:
  False`) keep their own shape and metadata rather than being flattened to plain text. Only
  the `end_session` additionally carries the folded native-channel return. If the turn also
  carries a `transfer`, the call is continuing rather than ending, so we DON'T preempt (return
  `(None, None)`) and let the normal path sequence the transfer + teardown — this covers a
  custom/telephony redirect, not just the zombie-transfer the caller already guards."""
  descs = list(action.get("response") or [])
  stash_keys = []
  if not any((rp or {}).get("type") == "end_session" for rp in descs):
    pend, keys = _pending_teardown(sm)
    descs = descs + list(pend)
    stash_keys = keys
  if not any((rp or {}).get("type") == "end_session" for rp in descs):
    return None, None
  if any((rp or {}).get("type") == "transfer" for rp in descs):
    return None, None
  parts = []
  # The spoken close: the model-rendered `message` (folded into the SI on a proceed-path
  # goodbye). Mirrors the normal emit, which prepends action['message'] before the response.
  if action.get("message"):
    parts.append(Part.from_text(text=_tts_tail(action["message"])))
  for rp in descs:
    if (rp or {}).get("type") == "end_session":
      parts.append(_end_session_part(rp, end_params))
    else:
      part = _build_response_part(rp)
      if part is not None:
        parts.append(part)
  return parts, stash_keys


def _build_response_part(rp):
  """Map one engine response descriptor to a CES Part (None for an unknown type,
  which the caller skips).

  Conversation-design vocabulary (declarative, so builders don't hand-write
  callbacks):
    * ``audio`` — a chime / brand audio / pre-recorded prompt. Emitted as a JSON
      payload the CES audio player consumes: ``audioUri`` (gs:// or https),
      optional ``interruptable`` (barge-in), ``cancellable`` (stops on the next
      response — hold music), and ``transcript`` (gives the model the audio's
      content as context).
    * ``interruptable: false`` on a ``text`` part — a non-interruptable segment
      (e.g. a legal/recording disclaimer) delivered via ``from_customized_response``
      with barge-in disabled when the runtime supports it, else plain text.
  """
  rp_type = rp.get("type", "text")
  if rp_type == "text":
    text = _tts_tail(_safe_spoken(rp.get("text", ""), bool(rp.get("partial"))))
    if rp.get("interruptable") is False and hasattr(
        Part, "from_customized_response"):
      return Part.from_customized_response(content=text, disable_barge_in=True)
    return Part.from_text(text=text)
  if rp_type == "audio":
    # Playable audio MUST use Part.from_audio (mime_type "application/json+audio"),
    # which the CES audio pipeline recognizes as a PLAY directive. Part.from_json
    # yields a generic "application/json" custom payload that the client renders
    # but never plays. Fall back to the JSON payload only on runtimes without
    # from_audio (older cxas).
    if hasattr(Part, "from_audio"):
      return Part.from_audio(
          rp.get("audioUri", ""),
          cancellable=bool(rp.get("cancellable", False)),
          interruptable=bool(rp.get("interruptable", True)),
      )
    audio = {"audioUri": rp.get("audioUri", "")}
    for k in ("interruptable", "cancellable", "transcript"):
      if k in rp:
        audio[k] = rp[k]
    return Part.from_json(json_lib.dumps(audio))
  if rp_type == "payload":
    return Part.from_json(json_lib.dumps(rp["data"]))
  if rp_type == "end_session":
    return _end_session_part(rp)
  if rp_type == "transfer":
    return Part.from_agent_transfer(agent=rp["agent"])
  return None


def _function_responses(llm_request):
  """{tool name: payload} for every function_response on the newest content.

  A parallel group's legs all come back on ONE content (ces-probes 34/39), named by
  their calling tool, with the payload a plain dict — so the whole batch is readable
  here without touching shared state. Only the newest content is scanned: the parts
  persist in history afterwards, so a wider scan would re-ingest the same batch on
  every later turn.
  """
  contents = getattr(llm_request, "contents", None) or []
  if not contents:
    return {}
  out = {}
  for part in (getattr(contents[-1], "parts", None) or []):
    fr = getattr(part, "function_response", None)
    if not fr or not getattr(fr, "name", ""):
      continue
    resp = getattr(fr, "response", None)
    try:
      resp = dict(resp) if resp is not None else {}
    except (TypeError, ValueError):
      resp = {}
    out[fr.name] = resp.get("result", resp)
  return out


_ASYNC_PENDING = "pending"


def _is_pending(payload):
  """Is this CES's placeholder for an ASYNCHRONOUS tool that has not run yet?

  Mirrors the engine's `_is_async_pending` — the two cannot import each other, and the
  callback needs it BEFORE the engine runs so a placeholder is never handed to intake
  as though it were a result.
  """
  if isinstance(payload, str):
    return payload.strip().lower() == _ASYNC_PENDING
  if isinstance(payload, dict):
    inner = payload.get("result")
    return isinstance(inner, str) and inner.strip().lower() == _ASYNC_PENDING
  return False


def _fanout_publications(callback_context, sm):
  """`[(leg, tool, payload)]` for every progressive fan-out leg that has published.

  A lowered leg writes its result to its OWN state key (`<group>_<leg>`) — separate
  keys because concurrent writes to one shared structure lose N-1 of them. The
  watcher's job is only to make this pass HAPPEN at the moment something landed; what
  actually landed is read here, straight out of state, so a watcher that reports a leg
  it cannot carry the payload for still gets it narrated.

  Every outstanding leg is checked each pass rather than only the ones the watcher
  named, so two legs landing inside one window are both picked up.
  """
  fan = sm.get("_fanout") or {}
  group = fan.get("group")
  if not group:
    return []
  done = set(fan.get("done_legs") or [])
  out = []
  for leg in (fan.get("legs") or []):
    if leg in done:
      continue
    try:
      raw = callback_context.state.get(f"{group}_{leg}")
    except Exception:  # pylint: disable=broad-except
      raw = None
    if not raw:
      continue
    if isinstance(raw, str):
      try:
        payload = json_lib.loads(raw)
      except Exception:  # pylint: disable=broad-except
        payload = {"raw": raw}
    else:
      payload = raw
    if isinstance(payload, dict) and "result" in payload and len(payload) == 1:
      payload = payload["result"]
    if not isinstance(payload, dict):
      payload = {"result": payload}
    out.append((leg, (fan.get("tools") or {}).get(leg, ""), payload))
  return out


def _action_calls(action):
  """Every function_call this action dispatches, singular or fan-out.

  A parallel group emits `function_calls` AND keeps `function_call` set to its first
  leg, so reading the plural first is what makes the group N calls rather than one.
  Every caller goes through here so the parts assembly and the two hide-tool
  exceptions below cannot disagree about which tools are being dispatched.
  """
  plural = action.get("function_calls")
  if plural:
    return [fc for fc in plural if fc and fc.get("name")]
  fc = action.get("function_call")
  return [fc] if fc and fc.get("name") else []


def _preempt_parts(action):
  """Assemble the LlmResponse Parts for a preempt — message, then each response
  descriptor, then every function_call. Empty when nothing was produced."""
  parts = []
  if action.get("message"):
    parts.append(Part.from_text(text=_tts_tail(action["message"])))
  for rp in action.get("response") or []:
    # D1: a transfer part's `disclaimer` is spoken right before the hand-off
    # (the "call transfer disclaimer / VOC" the CFD requires).
    if rp.get("type") == "transfer" and rp.get("disclaimer"):
      parts.append(Part.from_text(text=_tts_tail(rp["disclaimer"])))
    part = _build_response_part(rp)
    if part is not None:
      parts.append(part)
  # A fan-out group appends one Part per leg. CES dispatches every function_call part
  # in a preempt and runs them concurrently (ces-probes 09 and 33).
  for fc in _action_calls(action):
    parts.append(
        Part.from_function_call(name=fc["name"], args=fc.get("args", {})))
  return parts


def _zombie_exit_parts(callback_context, sm):
  """On a terminal preempt, flush the zombie's exit_status to CES state and return
  its transfer Part (if any) to append to the response."""
  zombie = sm.get("_zombie", {})
  for k, v in zombie.get("exit_status", {}).items():
    callback_context.state[k] = v
  # Pop (not get): the transfer is delivered exactly once. A re-entry turn must
  # NOT see a stale transfer_to (the engine's zombie-transfer finalizer keys on
  # it to recover an UNdelivered transfer; leaving it set would double-dispatch).
  transfer_to = zombie.pop("transfer_to", None)
  return [Part.from_agent_transfer(agent=transfer_to)] if transfer_to else []


def _relay_language(callback_context):
  """The active non-default caller language (a display name), or None.

  `active_language` is CES state set by the update_language tool; it is absent/English
  on an ordinary call. Returns the trimmed display name only when a real switch is in
  force, so the relay path is a strict no-op for every English / non-language turn."""
  try:
    lang = callback_context.state.get("active_language")
  except Exception:  # pylint: disable=broad-except
    return None
  if not lang:
    return None
  name = str(lang).strip()
  if name.lower() in _DEFAULT_LANGUAGE_TOKENS:
    return None
  return name


def _relayable_prose(action):
  """True when `action['message']` is a line the model may RESTATE — a non-empty prose
  line, not `verbatim`/readback copy (PHI/PCI stays literal), and not riding a
  function_call (a dispatch/filler the model must not swallow). WHERE/WHEN to relay (a
  terminal give-up vs a proceed auto-turn) is decided by the caller; this is only
  "is the copy translatable at all"."""
  msg = (action.get("message") or "").strip()
  if not msg or action.get("verbatim"):
    return False
  if _action_calls(action):
    return False
  return True


def _is_relayable(action):
  """True when one canned prose line, with its teardown carried INLINE on
  `action['response']`, should be TRANSLATED by the model this turn (the preempt shape).

  The line must be restatable prose (see `_relayable_prose`) and carry only teardown
  parts after_model can re-inject AFTER the translated close. A terminal give-up (an
  `end_session` part) is relayable whatever its class, so a closing announce that carries
  no `speech_class` is still covered. Proceed-path lines the engine folded into the SI —
  whose teardown it routed to a pending stash, not `action['response']` — are covered
  separately in `_apply_directive` (see `_pending_teardown`)."""
  if not _relayable_prose(action):
    return False
  resp = action.get("response") or []
  if any((rp or {}).get("type") not in _DEFERRABLE_PART_TYPES for rp in resp):
    return False
  has_end = any((rp or {}).get("type") == "end_session" for rp in resp)
  return has_end or action.get("speech_class") in _RELAY_CLASSES


def _pending_teardown(sm):
  """Deferrable teardown the engine ROUTED to a proceed-turn stash instead of onto
  `action['response']`. On a proceed terminal turn (e.g. a goodbye announce, `end=True`)
  the engine folds the line into the SI and stashes its `end_session` in
  `_pending_payloads` / `_pending_announce_payloads` — so a relay that inspected only
  `action['response']` would miss it and leave the model to parrot the English.

  Returns `(parts, keys)`: the deferrable descriptors and the stash keys they came from,
  so the caller can clear those stashes once the parts are deferred to after the
  translated close. A stash is harvested ONLY when it is ENTIRELY deferrable, so a card
  or chip riding alongside teardown is never silently dropped."""
  parts, keys = [], []
  for key in ("_pending_payloads", "_pending_announce_payloads"):
    stash = sm.get(key)
    if not isinstance(stash, list) or not stash:
      continue
    if all((rp or {}).get("type") in _DEFERRABLE_PART_TYPES for rp in stash):
      parts.extend(stash)
      keys.append(key)
  return parts, keys


def _response_relay(action):
  """Prose carried as text PART(s) on `action['response']` when `action['message']` is
  empty — two shapes:

    * a TERMINAL announce, e.g. `no_number_disconnect`, authored as
      `response=[{type:text, text:...}, {type:end_session}]`; and
    * a NON-terminal PREEMPT announce, e.g. tracking's `received_yes`
      (`F.announce(..., ["Great, I'm glad it arrived."], preempt=True)`), which the engine
      renders as `response=[{type:text, ...}, {type:text, <the follow-up ask>}]` with no
      teardown at all — the model was never going to run for it, so it would be spoken
      verbatim in English under a language lock.

  `_is_relayable` misses both twice over: its prose probe (`_relayable_prose`) reads
  `action['message']`, which is empty here, and its response probe rejects any part not in
  `_DEFERRABLE_PART_TYPES` — and `text` is not deferrable. Returns `(prose, teardown)` when
  the response is one-or-more text parts plus ONLY deferrable teardown, AND the action is
  either terminal (an `end_session` present, teardown to defer past the translated close) or
  a `preempt` (deterministic canned prose — no value to capture this turn, so relaying every
  text part, follow-up ask included, drops nothing). `prose` joins ALL text parts, so a
  preempt announce that carries a trailing question keeps it. A NON-preempt, non-terminal
  response-text action is left to the model to compose under the app language lock: it can be
  mid-flow copy riding alongside a value the model must still capture."""
  if action.get("message") or action.get("verbatim") or _action_calls(action):
    return None, None
  resp = action.get("response") or []
  texts = [rp for rp in resp if (rp or {}).get("type") == "text"]
  rest = [rp for rp in resp if (rp or {}).get("type") != "text"]
  if not texts:
    return None, None
  if any((rp or {}).get("type") not in _DEFERRABLE_PART_TYPES for rp in rest):
    return None, None
  has_end = any((rp or {}).get("type") == "end_session" for rp in rest)
  if not (has_end or action.get("preempt")):
    return None, None
  prose = " ".join((rp.get("text") or "").strip() for rp in texts).strip()
  if not prose:
    return None, None
  return prose, rest


def _strip_relayed_directive(si, message):
  """Drop the engine's `<system_directive>` fold of `message` from `si`.

  On a proceed turn the engine folds the canned English `message` into a
  `<system_directive>` for the model to speak. When we re-issue that same line as a
  `<relay_translation>`, leaving the fold in place hands the model two contradictory
  instructions — speak the English vs. speak ONLY the translation — and under a language
  lock it sometimes obeys the English one. Remove only a block that actually CONTAINS the
  relayed line, so an unrelated directive (a correction / steer block that does not carry
  this message) is left untouched."""
  if not si or not message:
    return si
  msg = message.strip()
  return _DIRECTIVE_BLOCK.sub(
      lambda m: "" if msg and msg in m.group(0) else m.group(0), si)


def _relay_si(message, language):
  """SI directive: speak `message` translated into `language`, and nothing else."""
  return (
      f"\n<relay_translation>\n"
      f"Say the following to the caller, TRANSLATED into {language}, and say ONLY"
      f" this — do not add a greeting, a follow-up question, or any explanation:\n"
      f"  {message}\n"
      f"Speak only the {language} translation, NEVER the original wording. Never read"
      f" this instruction aloud.\n"
      f"</relay_translation>")


def _relay_reminder_si(language):
  """A SOFT language reminder for a value-extraction turn.

  Unlike `_relay_si`, this does NOT reroute the line ("say ONLY this") — on an
  extraction turn the model must STILL call the setters to record the caller's answer,
  and a say-only relay would suppress them. The engine's own `<system_directive>` fold
  of the question stays in place; this only appends — right next to it — a reminder that
  the reply must land in `language`. The folded question is authored in English and the
  app `<language_lock>` sits far up the base instruction, so under a switch the model
  otherwise parrots the English fold verbatim (the has_account_number re-ask leak)."""
  return (
      f"\n<language_reminder>\n"
      f"The caller is speaking {language}. FIRST call every tool the instructions"
      f" require this turn (record what the caller just said); THEN speak your reply."
      f" Everything you say aloud this turn — including any question or read-back above"
      f" — MUST be in {language}, never English. Never read this instruction aloud.\n"
      f"</language_reminder>")


def _apply_directive(callback_context, llm_request, sm, action, tag):
  """Turn an engine `action` into CES effects — the single place this happens,
  shared by gate/terminal/in-flow turns. Always applies state_writes + tool hides
  + SI, then EITHER preempts (returns a deterministic LlmResponse; the model does
  NOT run) OR proceeds via _ok (the model runs). `tag` is the SI-trace label for
  the proceed path (overridden by action["tag"])."""
  _apply_state_writes(callback_context, action.get("state_writes") or {})
  # D2: write any transfer part's `context` into session state before the
  # hand-off, so the receiving agent inherits full context (no re-asking).
  for rp in action.get("response") or []:
    if rp.get("type") == "transfer" and isinstance(rp.get("context"), dict):
      for _ck, _cv in rp["context"].items():
        callback_context.state[_ck] = _cv
  # `or []`, not a .get default: an explicit `"hide_tools": None` would otherwise be
  # iterated and raise TypeError, taking the turn down into the platform "having
  # trouble" render. Every sibling list read in this function already uses `or []`.
  for tool_name in (action.get("hide_tools") or []):
    llm_request.config.hide_tool(tool_name)
  # Engine-fired task executors are NEVER model-callable — only the engine dispatches them (with the
  # task's inputs). The engine's per-turn hide covers just the ACTIVE config's tasks, so in a single-agent
  # multi-flow app every other flow's tasks (and, in any app, every component's tasks) would leak to the
  # model; a stray empty model call satisfies the task with no outputs and strands the flow. Hide the
  # complete app-level set (emitted as `engine_task_tools`) on every turn — EXCEPT the tool the engine is
  # itself dispatching this turn (its fire action's function_call), which must stay callable or the
  # dispatch renders empty ("having trouble"). Absent → no-op.
  _raw_ett = callback_context.state.get("engine_task_tools")
  if _raw_ett:
    try:
      _ett = json_lib.loads(_raw_ett) if isinstance(_raw_ett, str) else _raw_ett
    except (ValueError, TypeError):
      _ett = []
    _firing = {fc["name"] for fc in _action_calls(action)}
    for _tool in (_ett or []):
      if _tool not in _firing:
        llm_request.config.hide_tool(_tool)
  # Router tool hygiene (single-agent router-over-flows). The one agent exposes EVERY flow's setters and
  # `{flow}_dag` config loaders; on a router turn the model then "does" the request by calling a flow tool
  # directly instead of the routing/bootstrap tool, so the active_flow gate is never set and the engine
  # never leaves the router — no flow DAG ever drives. When the active config IS the router (its id equals
  # the default/host config), hide the flow-specific set (emitted as `router_hide_tools`) so routing is the
  # only option; once a flow is active (config switched off the router) the block no-ops and the flow's own
  # tools are visible again. Absent `router_hide_tools` (multi-agent / journey / non-migrated) → no-op.
  _active_cfg = callback_context.state.get("_active_config_id")
  _default_cfg = callback_context.state.get("default_config_id")
  # Also hide the flow-specific set inside a SILENT flow — a no-model-input flow (a fan-out/verdict whose
  # spine fires + ladder announces with NO setter for the model to call). Without this, once the router
  # activates such a flow the OTHER flows' setters re-appear and the model "does" the request by calling a
  # sibling setter instead of letting the engine drive the silent spine → the verdict never renders. The
  # migration marks these as `silent_flow_configs`. Absent → no-op (byte-identical for every existing agent).
  _raw_silent = callback_context.state.get("silent_flow_configs")
  try:
    _silent = json_lib.loads(_raw_silent) if isinstance(_raw_silent, str) else (_raw_silent or [])
  except (ValueError, TypeError):
    _silent = []
  _hide_flow_tools = ((_active_cfg and _default_cfg and _active_cfg == _default_cfg)
                      or (_active_cfg and _active_cfg in _silent))
  if _hide_flow_tools:
    _raw_rht = callback_context.state.get("router_hide_tools")
    if _raw_rht:
      try:
        _rht = json_lib.loads(_raw_rht) if isinstance(_raw_rht, str) else _raw_rht
      except (ValueError, TypeError):
        _rht = []
      # EXCEPT the tool the engine is dispatching THIS turn: inside a silent flow the engine fires the
      # verdict's own spine tasks, which ARE in router_hide_tools. Hiding does not by itself block
      # dispatch — an isolated probe fired a tool it had just hidden, first try — so this is not load-
      # bearing for the fire. It keeps the dispatched tool declared on the request, mirroring the
      # engine_task_tools exception directly above rather than diverging from it.
      _firing2 = {fc["name"] for fc in _action_calls(action)}
      for _tool in (_rht or []):
        if _tool not in _firing2:
          llm_request.config.hide_tool(_tool)
  # Deterministic terminal emit (native-channel return). When the flow has staged the
  # reserved handoff var, the leg's end must be emitted by the FRAMEWORK, not left to the
  # model: on the live model a terminal `message=` announce is spoken and the model then
  # freeform-ends with a BARE end_session, dropping the staged return (measured on the
  # FIVE9 dev line). So on a terminal turn we PREEMPT with the spoken close + the
  # end_session carrying the folded return, and short-circuit — the deterministic emit an
  # app-level before_model hook used to do by hand. Gated on a real staged handoff, so it
  # is a strict no-op for every flow that does not stage one (byte-identical). Placed
  # BEFORE the relay so it also covers a terminal turn under a language lock (the close is
  # spoken verbatim, as the app hook did; translated-terminal is a separate enhancement).
  _end_params = _resolve_end_params(callback_context.state, sm)
  if _end_params and not sm.get("_zombie", {}).get("transfer_to"):
    _term_parts, _term_keys = _terminal_emit_parts(action, sm, _end_params)
    if _term_parts:
      for _k in _term_keys:
        sm.pop(_k, None)
      _log(sm, "on_end_terminal_emit", parts=len(_term_parts))
      callback_context.state[_SM_KEY] = sm
      return LlmResponse.from_parts(parts=_term_parts)

  # Relayed-copy translation gate. When a non-default caller language is active and this
  # action is one canned prose line, reroute it through the model for translation instead
  # of speaking/dropping it verbatim. Two shapes are covered (a: preempt, b: proceed);
  # see each below. Skip when a zombie exit is in flight (transfer/exit_status): that
  # terminal path flushes hand-off state on the verbatim rail and must not be reordered.
  # relay_lang is None on every English turn, so this whole block is a strict no-op there.
  relay_lang = _relay_language(callback_context)
  _zombie = sm.get("_zombie") or {}
  _relay_ok = (bool(relay_lang) and not _zombie.get("transfer_to")
               and not _zombie.get("exit_status"))
  # Clear any stale relay-fallback language marker up front: it is re-set below ONLY when a
  # relay fires this turn, so a later non-relay turn (whose _render_fallback the engine sets
  # to plain English) can never inherit a marker that would wrongly suppress it in after_model.
  sm.pop("_render_fallback_lang", None)
  # (a) Preempt shape: teardown rides INLINE on action['response'] (see _is_relayable).
  relay = _relay_ok and _is_relayable(action)
  _deferred_parts = ([rp for rp in (action.get("response") or [])
                      if (rp or {}).get("type") in _DEFERRABLE_PART_TYPES]
                     if relay else [])
  _deferred_keys = []
  # The prose to relay. Paths (a)/(b) source it from action['message']; path (d) overrides
  # it with the text carried on action['response'] (an announce whose message is empty).
  _relay_message = action.get("message")
  # (b) Proceed shape: the engine folded the line into the SI (so _is_relayable, which
  # inspects only action['response'], is False) and routed any teardown to a pending
  # stash. Relay ONLY when the model is not ALSO being asked to capture a value this turn
  # — i.e. the turn is TERMINAL (a pending end_session, e.g. a goodbye announce) or has
  # EMPTY contents (an auto-turn / no-op re-run with no caller utterance to extract).
  soft_lang = False
  if _relay_ok and not relay and _relayable_prose(action):
    _pend_parts, _pend_keys = _pending_teardown(sm)
    _terminal = any((rp or {}).get("type") == "end_session" for rp in _pend_parts)
    if _terminal or not llm_request.contents:
      relay = True
      _deferred_parts = _pend_parts
      _deferred_keys = _pend_keys
    else:
      # (c) Value-extraction turn (non-terminal, caller utterance present): the model must
      # BOTH call the setter (record the answer) AND reply. A "say ONLY this" relay would
      # suppress the setter and drop the answer, so we do NOT reroute the line. Instead
      # leave the engine's <system_directive> fold in place and append a SOFT language
      # reminder next to it, so the reply lands in the caller's language instead of the
      # model parroting the English fold (the has_account_number re-ask leak). No deferral,
      # no fallback: this is a reminder, not a relay.
      soft_lang = True

  # (d) Announce shape: the prose rides as text PART(s) on action['response'] alongside a
  # terminal end_session, with action['message'] EMPTY (e.g. no_number_disconnect). Neither
  # (a) nor (b) sees it — both source prose from action['message'] via _relayable_prose, and
  # _is_relayable's response probe rejects the non-deferrable text part. Relay the response
  # text and defer the teardown so the session ends only AFTER the translated close.
  if _relay_ok and not relay and not soft_lang:
    _resp_prose, _resp_teardown = _response_relay(action)
    if _resp_prose:
      relay = True
      _relay_message = _resp_prose
      _deferred_parts = _resp_teardown

  if action.get("si") or relay or soft_lang:
    si = action.get("si") or ""
    # Pass-A language reconciliation: only on the classifier pass, and only for a
    # language-switching agent — detected two ways, either sufficient (OR):
    #   * the base SI carries a <language_detection> block (read before the suffix is
    #     injected), the original marker; and
    #   * update_language is a declared tool on the request (_has_tool -> config.tools),
    #     a semantic check robust to the marker string not matching verbatim.
    # OR (not AND) on purpose: config.tools is a sandbox surface we have not relied on
    # before, so if it is absent the SI marker still fires — the check can only ADD
    # detection, never regress the fix. No-op for every non-language agent. See
    # _PASS_A_LANGUAGE_NOTE (flows-passA-language-switching-bug).
    if action.get("tag") == "pass_a_classify":
      _via_si = "<language_detection>" in _si_text(llm_request)
      _via_tool = _has_tool(llm_request, "update_language")
      if _via_si or _via_tool:
        si = si + _PASS_A_LANGUAGE_NOTE
        # Logged so a live run reveals whether CES actually populates config.tools:
        # via_tool=True on a language agent confirms the semantic surface works.
        _log(sm, "pass_a_language_reconciled", "DEBUG",
             via_si=_via_si, via_tool=_via_tool)
    # Fold the translate-and-speak directive into the SAME suffix — _inject_phase_suffix
    # REPLACES the framework suffix, so a second call would wipe the block above. Drop the
    # engine's own <system_directive> fold of this same line first, so the relay directive
    # is the single, unambiguous instruction for it (see _strip_relayed_directive).
    if relay:
      si = _strip_relayed_directive(si, _relay_message) + _relay_si(
          _relay_message, relay_lang)
    elif soft_lang:
      # Keep the engine's <system_directive> fold; only append the language reminder.
      si = si + _relay_reminder_si(relay_lang)
      _log(sm, "relay_language_reminder", lang=relay_lang,
           message=str(action.get("message"))[:80])
    _inject_phase_suffix(llm_request, si)
  tag = action.get("tag", tag)

  if relay:
    # Defer any teardown so the session ends only AFTER the model's translated close, and
    # clear the proceed-path stash it came from so after_model does not ALSO inject it
    # (once, ahead of the close) via its pending-payload path. `_deferred_parts` is the
    # preempt path's action['response'] teardown or, on the proceed path, the pending
    # stash `_pending_teardown` harvested; `_deferred_keys` names the stashes to clear.
    if _deferred_parts:
      sm["_deferred_terminal_parts"] = _deferred_parts
    for _k in _deferred_keys:
      sm.pop(_k, None)
    # Backstop: if the model returns nothing, after_model speaks this (authored) line
    # rather than leaving the caller in silence — then still tears the session down.
    # BUT this line is the canned ENGLISH copy the relay is translating away, so mark it
    # language-locked: after_model must NOT speak it verbatim (that is the very leak the
    # relay exists to prevent). Under a switch it suppresses the fallback instead.
    sm["_render_fallback"] = _relay_message
    sm["_render_fallback_lang"] = relay_lang
    _log(sm, "relay_translation", lang=relay_lang, deferred=len(_deferred_parts),
         speech_class=action.get("speech_class"),
         message=str(_relay_message)[:80])
    # Proceed: the model runs and composes the translated line; the verbatim message
    # Part is never built. sm is persisted by _ok.
    return _ok(callback_context, llm_request, sm, tag)

  # A preempt normally needs user contents to react to. But a preempt that DISPATCHES A TOOL
  # (``function_call``) is a SILENT engine-driven fire — e.g. a Shape-C verdict's diagnostic spine
  # runs on the auto-turn right after routing, when the user's utterance was already consumed by the
  # routing turn so ``llm_request.contents`` is empty. Gating that fire on non-empty contents drops it,
  # and CES re-invokes the engine every turn without ever executing the tool (reasoning-loop cap). Honor
  # a function_call preempt even with empty contents; contents-present behavior is unchanged (byte-safe).
  #
  # A terminal EXHAUST verdict (``speech_class == "exhaust"``) is the other engine-driven fire that
  # can land on an empty-contents auto-turn: a no-ladder task fails on the auto-turn right after
  # routing and the engine emits its disposition line with NO function_call. Dropping that message
  # lets the model run without ever ending the turn, so CES re-invokes, the still-eligible task fires
  # again, and the turn loops to the reasoning-loop cap. Honor the exhaust line on empty contents too;
  # it is terminal (the engine has given up) so speaking it ends the wedge instead of feeding it.
  parts = (_preempt_parts(action)
           if action.get("preempt") and (llm_request.contents
                                         or action.get("function_call")
                                         or action.get("speech_class") == "exhaust") else [])
  if not parts:
    # Silent wait tick: suppress the model with an empty LlmResponse (no audio) so
    # the silence countdown just waits instead of the model improvising.
    #
    # A TURN THE CALLER ACTED ON IS NEVER ANSWERED WITH ZERO CONTENT.
    #
    # `parts=[]` adds no model content at all (ces-probes 42), which is exactly what a
    # SILENCE turn wants: nothing reaches the voice pipeline and the platform's own
    # inactivity clock re-arms and ticks again (83, driven in audio). On a turn the
    # caller acted on — speech, a keypress, a barge-in — it is fatal, and silently so:
    # the turn is never closed, and the request stops advancing.
    #
    # Driven on a voice agent that asks a question during an asynchronous wait. The
    # caller said "uh" mid-wait; the engine held the wait silently (correctly — the
    # reassurance ladder is cover for dead air, and a turn the caller is spending
    # thinking is not that); this branch answered with no content. Every invocation
    # afterwards — inactivity ticks and real utterances alike — was handed the SAME
    # `last_user_text` ("uh"), the same `scanned_user_text` and a frozen
    # `n_user_turns`. The words the caller spoke next never entered
    # `llm_request.contents` at all, so the engine kept reading the turn as one the
    # caller had spoken on, kept holding, and kept producing nothing: the silence
    # sustains itself and the call is over. No ladder line, no `no_input` rung, no
    # await timeout, no hang-up — every one of those is downstream of a turn the
    # platform never closed. Read off the platform's own traces (the callback and the
    # engine ran on every one of those turns; there was no error and no model pass),
    # and reported independently by five testers as the call simply dying.
    #
    # One EMPTY content is the other shape ces-probes 42 measured, and it is what a
    # turn the caller acted on gets: silent to the caller, but a turn the platform can
    # close. ces-probes 159 reduced the choice to a two-turn A/B and measured all of it:
    # after `parts=[]` the next request carries ONE content where the control carries
    # three — the turn the caller took is gone from the conversation — and one empty
    # content reproduces the control exactly, on speech and on a keypress alike.
    #
    # NOT `turn_complete = True`. The attribute is real (57 reads `LlmResponse`'s fields
    # as `content, partial, turn_complete`) and 73 saw it end a run early, so it looks
    # like the clean way to say this. It is not: 73 set it on a response that carried
    # TEXT, and on an EMPTY response 159 measured it as byte-identical to `parts=[]` —
    # the assignment is accepted and the turn is lost anyway. It is the CONTENT the
    # platform records, not the flag.
    #
    # The engine's decision is untouched — a wait turn the caller spoke on still holds
    # the ladder silently and still does not spend a rung — and so is the delivery on
    # every turn the caller did not take, which is every silence tick of every wait.
    if action.get("silent") and llm_request.contents:
      _close = _turn_requires_closing(llm_request)
      _log(sm, "no_input_silent_tick", closes_turn=_close)
      sm.pop("_pending_payloads", None)
      sm.pop("_pending_question_payloads", None)
      _record_si(sm, llm_request, "silent", False, action="silent")
      callback_context.state[_SM_KEY] = sm
      return LlmResponse.from_parts(
          parts=[Part.from_text(text="")] if _close else [])
    # Latency filler on a turn the MODEL will author. There is no function_call to
    # ride here (that is the task path), so the line is spoken as a PARTIAL preempt:
    # it reaches the caller immediately and keeps the floor, and the model's own reply
    # lands in the same turn (ces-probes 57). The engine armed it and owns the
    # once-per-turn and pass-budget guards (_arm_model_filler).
    filler = action.get("filler_partial")
    if filler and llm_request.contents:
      resp = LlmResponse.from_parts(
          parts=[Part.from_text(text=_tts_tail(_safe_spoken(filler, True)))])
      try:
        resp.partial = True
        resp.turn_complete = False
      except Exception as exc:  # pylint: disable=broad-except
        # Degrade to NO filler rather than to a full preempt: a full preempt would
        # speak the line and hand the floor back (ces-probes 26), so the caller would
        # hear "one moment" and then have to prompt for the answer themselves.
        _log(sm, "filler_partial_not_settable", "WARN", err=type(exc).__name__)
        return _ok(callback_context, llm_request, sm, tag)
      _log(sm, "filler_partial_spoken", text=filler)
      _record_si(sm, llm_request, "filler", False, action="filler")
      callback_context.state[_SM_KEY] = sm
      return resp
    return _ok(callback_context, llm_request, sm, tag)

  # Preempt: deliver our own response, so the model does not run this turn.
  _log(sm, "preemption", has_message=bool(action.get("message")),
       has_response=bool(action.get("response")),
       has_function_call=bool(action.get("function_call")))
  # Model won't run -> after_model won't consume staged payloads; drop them so
  # they don't leak onto a later turn (see the _pending_payloads lifecycle).
  sm.pop("_pending_payloads", None)
  sm.pop("_pending_question_payloads", None)
  # A preempt that SPEAKS cancels the model turn. If a task earlier in this turn issued a
  # `then_directive`, the answer it promised is now never generated — silently, which is
  # what made this cost four rounds of live hunting to find. Behaviour is unchanged; the
  # loss is merely no longer invisible. The parallel path has logged its equivalent
  # (`parallel_actions_dropped`) all along; the sequential path never did.
  if sm.pop("_directive_open", None) and action.get("message"):
    _log(sm, "directive_cancelled_by_preempt", "WARN",
         message=str(action.get("message"))[:80])
  parts += _zombie_exit_parts(callback_context, sm)
  _record_si(sm, llm_request, "preempt", False, action="preempt")
  callback_context.state[_SM_KEY] = sm
  resp = LlmResponse.from_parts(parts=parts)
  # Partial / prefix (A4): if any response descriptor is marked ``partial``, speak
  # the deterministic prefix but keep processing so the model continues the turn
  # (static message + generative continuation). Best-effort — a runtime without a
  # settable ``partial`` degrades to a normal full preempt.
  #
  # An action-level ``partial`` is the progressive fan-out's narration: the parts are
  # the leg's line plus the next watch call, and ``partial`` is what SPEAKS the line
  # without ending the turn. A full preempt would hand the floor back and abandon the
  # legs still in flight, so the caller would hear the first finding and nothing else.
  if action.get("partial") or any(
      isinstance(rp, dict) and rp.get("partial")
      for rp in (action.get("response") or [])):
    try:
      resp.partial = True
    except Exception as exc:  # pylint: disable=broad-except
      # Not silent: the degrade is a FULL preempt, which ends the turn (ces-probes
      # 26). A fan-out that lands here speaks its first finding and abandons the legs
      # still in flight — the exact failure `partial` is here to prevent.
      _log(sm, "partial_not_settable", "WARN", err=type(exc).__name__)
  return resp


def _extract(callback_context, llm_request, sm):
  """STEP 1 — gather EVERY engine input as plain, JSON-serializable data: the
  CES-only reads the engine cannot do itself. That is now just (a) `llm_request`
  reads — `last_user_text` and the contents scan resolved to scalars (NO Part
  objects cross the boundary; they are not serializable) — and (b)
  `callback_context.state` reads. The engine FETCHES + validates its own config
  (tool→tool calls work), so the callback passes `config_id` + `agent_config_map`,
  not raw_config. Returns the turn_input dict handed straight to the engine.
  """
  pending_transfer = callback_context.state.pop("_pending_transfer", "")
  config_id = callback_context.state.get("_active_config_id")
  # Mid-turn router->flow switch (single-agent router-over-flows). before_agent resolves config at turn
  # START (= the router), but a deterministic route_cues match or the model's set_active_flow sets the
  # active_flow gate DURING the turn. Without re-resolving, the config wouldn't switch until the NEXT turn,
  # so the router turn renders a redundant "which operation?" clarification instead of the flow's first
  # question. Re-resolve here (each pass) so the pass right after the gate is set runs the FLOW's DAG and
  # asks its first question on the SAME turn — the flow-keyed analog of a transfer's fresh-turn boundary.
  # Guarded to the router->flow transition (flow_config_map declared, currently on the default/router
  # config, gate now a mapped flow, in progress); no-op otherwise -> byte-identical for non-router apps.
  _raw_fmap = callback_context.state.get("flow_config_map")
  if _raw_fmap and config_id and config_id == callback_context.state.get("default_config_id"):
    try:
      _fmap = json_lib.loads(_raw_fmap) if isinstance(_raw_fmap, str) else _raw_fmap
    except (ValueError, TypeError):
      _fmap = {}
    _gate = sm.get("_gate_slot") or "active_flow"
    _af = (sm.get("filled") or {}).get(_gate)
    _cfg = _fmap.get(_af) if _af else None
    # Re-enter / switch into a mapped flow after a prior flow terminated — the MID-turn
    # analog of before_agent's turn-start re-arm, and the path that actually matters for a
    # model-driven router: the model calls set_active_flow THIS turn (or a route_cues /
    # backstop fires) for the SAME flow or a DIFFERENT one while the prior flow is still
    # status="zombie". Reap the zombie IN PLACE (carry any shared_values, drop _zombie) and
    # re-arm to in_progress BEFORE the model/engine runs, so the destination DAG drives on
    # THIS turn. Leaving status="zombie" for the engine's own _reap_zombie_on_reentry does
    # NOT work here: the model runs on the router config, sees the zombie, and renders
    # "having trouble" before the engine reap can flip it (verified live). Fires for
    # same-flow re-entry too. Keyed on status=="zombie", NOT on _zombie["flow"] being
    # populated — the steering record_path terminator leaves an EMPTY flow name and that
    # zombie must re-arm too (verified live). A "complete"/"escalated" status (no zombie to
    # reap) is left to the router; never while PAUSED (_flow_state). sm is threaded into
    # the engine call below and persisted with the engine's out_sm, so the mutation sticks.
    if (_cfg and _cfg != config_id and _af
        and sm.get("status") == "zombie" and not sm.get("_flow_state")):
      _carried = dict((sm.get("_zombie") or {}).get("shared_values", {}))
      sm.pop("_zombie", None)
      sm["status"] = "in_progress"
      if _carried:
        sm.setdefault("filled", {}).update(_carried)
    if (_cfg and _cfg != config_id
        and sm.get("status", "in_progress") not in ("complete", "zombie", "escalated")):
      callback_context.state["_active_config_id"] = _cfg
      config_id = _cfg
  last_user_text = ""
  _real_text = ""
  _keypad = ""
  is_barge_in = False
  barge_heard = ""
  async_completions = []
  if llm_request.contents:
    _last = llm_request.contents[-1]
    if getattr(_last, "role", "") == "user":
      _texts = [p.text for p in getattr(_last, "parts", []) if getattr(p, "text", "")]
      # Harvest completion envelopes from EVERY part of this turn. Scanning all parts
      # (not just the first non-inactivity one) is what stops a second outstanding tool
      # losing its result to a co-arriving sibling — a completion that is merely NOT
      # SEEN is worse than a late one, since the task then waits out max_turns and
      # reports a timeout for a backend that answered. Only THIS turn's parts are
      # scanned: the envelope is last exactly when it lands and merely history
      # afterwards, which is what keeps ingestion idempotent.
      async_completions = [c for t in _texts for c in _parse_async_completions(t)]
      if async_completions:
        # The envelope itself is never speech, so it is always stripped. Whether the
        # REMAINDER counts as speech is a config decision (`awaits.answer_first`), and
        # the config lives in the engine — so hand it both and let it choose. The engine
        # blanks this by default, which is the behaviour every agent has today.
        _texts = [x for x in (_strip_async_envelopes(t) for t in _texts) if x]
      # Prefer a REAL utterance over ANY co-present context wrapper. CES attaches a
      # context part to the SAME turn the caller speaks in for two shapes: a "no user
      # activity" hold (the caller reading their number as the silence fires) and a
      # barge-in "agent speaking was interrupted" note (the caller answering over the
      # prompt). Skipping only the inactivity one grabs the interruption note as the
      # utterance and DROPS the speech, so the slot stays empty and the engine re-asks
      # (issue #511). `_is_real_user_text` already rejects every <context> marker, so
      # use it here; fall back to the raw first part only when a wrapper is all there is.
      _real_text = next((t for t in _texts if _is_real_user_text(t)), "")
      # A DTMF keypress is also a <context> wrapper (rejected above), but it IS
      # deliberate input. Lift its bare token so the engine's dtmf_map fast-path can
      # match it — including when it rides alongside a barge-in note. Speech wins; the
      # keypad token is the next-best real input, ahead of the raw context fallback.
      #
      # THE ENGINE UNWRAPS THE SAME ENVELOPE TOO — see `_DTMF_ENVELOPE` in
      # tools/slot_filling_engine/.../python_code.py (`_apply_dtmf_input`). That is
      # deliberate, not drift: the OFFLINE SIMULATOR (flows/sim/engine_sim.py ->
      # flows/engine/loader.py) calls the engine tool directly and never loads this
      # callbacks package, so offline the engine's own matcher is the ONLY thing that
      # makes dtmf_map fire. This layer stays the live one because only it sees every
      # part of the turn and can therefore order precedence; the engine's is a
      # single-string fallback. Change the wire shape of a keypad part and BOTH
      # regexes need updating.
      _keypad = "" if _real_text else next(
          (m.group("keys") for t in _texts
           for m in (_KEYPAD_PATTERN.search(t),) if m), "")
      last_user_text = _real_text or _keypad or (_texts[0] if _texts else "")
      # A BARGE-IN note rides on the SAME turn the caller spoke in, so it is read
      # independently of the precedence above rather than competing with it: the caller's
      # words stay the utterance, and this only records that they arrived over the top of
      # something. Scanned across every part for the same reason the keypad lift is —
      # CES attaches the wrapper as its own part alongside the speech.
      _barge = next((m for t in _texts
                     for m in (_BARGE_PATTERN.search(t),) if m), None)
      if _barge is not None:
        is_barge_in = True
        barge_heard = (_barge.group("heard") or "").strip()
  # A CES inactivity signal is silence, not user speech: blank it so the engine's
  # no_input (silence) ladder handles it, instead of the model treating it as input.
  # is_inactivity flags a GENUINE silence turn (a bare inactivity context, no real
  # speech) so the engine advances the ladder only here — not on a post-setter
  # re-invoke (empty last_user_text) and not when the caller actually spoke.
  is_inactivity = (not _real_text) and bool(
      _INACTIVITY_PATTERN.search(last_user_text or ""))
  if is_inactivity:
    last_user_text = ""
  # A BARGE-IN wrapper with nothing else on the turn is not speech either, and this line
  # is what keeps `barge_in_awareness` defaulting ON from changing anything for an agent
  # that does not use the feature. Without it the `_texts[0]` fallback above hands the raw
  # marker to the engine as the utterance, where it would reach intent classification,
  # option cues and steer-back — the same leak the inactivity blanking exists to stop.
  # NOT folded into is_inactivity: a barge is the caller ACTING, so the silence ladder must
  # not advance on it (ces-probes 161; `_part_requires_closing` above turns on the same
  # distinction).
  if is_barge_in and not _real_text and not _keypad:
    last_user_text = ""
  # The envelopes were already stripped above, for the same reason silence is blanked:
  # they are not speech. Left populated they would reach _intent_directive (JSON
  # containing "stop"/"transfer" reads as a control intent), steer-back (whose
  # `progressed` test ignores task_results, so every such turn would count as off-topic
  # and six would escalate the flow), option-cue matching and DTMF.
  event_data = callback_context.state.get("event_data", {})
  ia_event = callback_context.state.get("ia_event_name")
  if not ia_event and last_user_text:
    m = _EVENT_TAG_PATTERN.search(last_user_text)
    if m:
      ia_event = m.group(1)
  if ia_event:
    event_data["ia_event_name"] = ia_event

  # Contents scan -> scalars (scanned_user_text, n_user_turns). The engine has no
  # llm_request access, so this is the irreducible callback residue. None-safe
  # (function Parts carry text=None); the access pattern is otherwise unconstrained.
  scanned_user_text = _scan_real(llm_request.contents)
  n_user_turns = sum(
      1 for c in (llm_request.contents or [])
      if getattr(c, "role", "") == "user"
      and any(_is_real_user_text(getattr(p, "text", ""))
              for p in (getattr(c, "parts", None) or [])))

  return {
      "sm": sm,
      "pending_transfer": pending_transfer,
      "config_id": config_id,
      "agent_config_map": callback_context.state.get("agent_config_map", "{}"),
      "last_user_text": last_user_text,
      "is_inactivity": is_inactivity,
      "is_barge_in": is_barge_in,
      "barge_heard": barge_heard,
      "event_data": event_data,
      "transfer_slots": callback_context.state.get("_transfer_slots", {}),
      "gate_user_text": callback_context.state.get("_gate_user_text", ""),
      "scanned_user_text": scanned_user_text,
      "n_user_turns": n_user_turns,
      "turn_kind": _classify_turn(llm_request),
      "async_completions": async_completions,
      "async_completion_landed": bool(async_completions),
  }


def _scan_real(contents):
  """Latest real user text in llm_request.contents (skips markers / None-text)."""
  for c in reversed(contents or []):
    if getattr(c, "role", "") == "user":
      for p in (getattr(c, "parts", None) or []):
        if _is_real_user_text(getattr(p, "text", "")):
          return p.text
  return ""


def _hide_internal_tools(callback_context, llm_request):
  """Hide the framework/engine tools the user must never see (engine, intake,
  transfer, EVERY config's *_dag tool, validate/evaluate). Config-derived,
  never user-facing. Pulled out of _apply so the crash fallback can run it too —
  returning None on error must NOT expose the raw model with internal tools."""
  # `settle_guard` is dispatched BY the engine alongside a deferred call and is never
  # the model's to choose: offered, it is a plausible-looking no-op that a model with
  # nothing better to do will call (the unadvertised-tool lure).
  hide = ["slot_filling_engine", "slot_intake", "transfer_to_agent", "settle_guard"]
  _cid = callback_context.state.get("_active_config_id")
  if _cid:
    hide += [f"{_cid}_dag", "evaluate_conditions"]

  # EVERY flow's loader, not just the active one. A multi-flow app declares
  # `{cid}_dag` for all of its flows on the one agent, and hiding only the active
  # config's left every SIBLING flow's loader callable — a way into a flow the
  # router did not choose, on every turn after the routing turn.
  #
  # `router_hide_tools` already covers these, but ONLY on the router turn, which is
  # why the routing decision itself was never the symptom. Observed on a repair agent
  # driven over voice: correctly routed to the repair flow, the model then called
  # `reboot_dag` and announced it had restarted the caller's gateway (a healthy
  # account, no task fired), and on another run `technical_phone_dag` and deflected an
  # internet fault to the phone queue. 2 of 3 spoken runs; 0 of 3 in text, so a
  # text-only harness cannot see it.
  #
  # Loaders are pure config fetches the engine makes itself, through the app registry
  # (`getattr(tools, f"{cid}_dag")({})`) rather than the agent's tool list, so hiding
  # them from the model costs nothing: dispatch is unaffected.
  # EVERY config the app names, from every map that names one. No single source is
  # complete:
  #
  #   flow_config_map    the routable CHILDREN of a single-agent router
  #   default_config_id  the ROUTER's own config. Never in the map above, and the
  #                      entrance the observed leak went through: `steering_dag` -> a
  #                      sibling flow's DAG -> an unrequested gateway restart. Measured
  #                      on a real agent with the children already hidden, the router's
  #                      own was still reached on 5 of 6 runs.
  #   intent_config_map  present when an engine host routes on an intent
  #   agent_config_map   each agent's config in a multi-agent app. Usually redundant, as
  #                      a sibling agent's loader is not declared on this agent - but
  #                      `scoped_agent_tools(extra_config_ids=...)` can declare extras
  #                      that appear in no other map, and hiding a tool that is not
  #                      present costs nothing.
  #
  # The `_dag` suffix is appended UNCONDITIONALLY, and that is deliberate: the tool name
  # is always `{config_id}_dag`, INCLUDING when the config id itself ends in `_dag`. A
  # flow named `app_host_dag` emits the tool `app_host_dag_dag` (verified). Treating the
  # suffix as already-present would hide a name that does not exist and leave the real
  # loader visible - the opposite of the intent.
  def _config_ids_in(state_key):
    raw = callback_context.state.get(state_key)
    if not raw:
      return []
    try:
      parsed = json_lib.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
      return []
    return [v for v in parsed.values() if v] if isinstance(parsed, dict) else []

  _known = set()
  _default_cid = callback_context.state.get("default_config_id")
  if _default_cid:
    _known.add(_default_cid)
  for _key in ("flow_config_map", "intent_config_map", "agent_config_map"):
    _known.update(_config_ids_in(_key))
  hide += [f"{_cfg}_dag" for _cfg in sorted(_known)]

  for _t in hide:
    llm_request.config.hide_tool(_t)


def _apply(callback_context, llm_request, sm, directive):
  """STEP 3 — the single apply: framework hides + ADK boilerplate strip, then
  _apply_directive (config/engine hides, SI, state_writes, preempt-or-proceed).
  The static base framework hides (the config's *_dag tool + validate/evaluate)
  are applied here directly — config-derived, never user-facing — so no directive
  needs to carry them.
  """
  _hide_internal_tools(callback_context, llm_request)
  _strip_transfer_boilerplate(llm_request)
  return _apply_directive(callback_context, llm_request, sm, directive,
                          directive.get("tag", "engine"))


def before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
  """CES entry point — extract -> engine -> apply.

  _extract gathers every engine input as plain JSON data (incl. the contents
  scan, resolved to scalars — the one thing the engine can't do). ONE
  slot_filling_engine call owns the whole turn decision: transfer/no-config
  guards, intent inject, resume-offer, zombie-reap, render-capture, phase. _apply
  turns the directive into CES effects. (Instrumented: any engine/apply error is
  logged to sm._log as before_model_CRASH and the turn proceeds degraded, so a
  crash is diagnosable instead of silently becoming "having trouble".)
  """
  sm = callback_context.state.get(_SM_KEY, {})
  # Which model is running. `after_model` is handed only a response, and the two
  # supported models invert what a substituted response DOES (see `_supersede`), so the
  # id has to be stashed here or the decision cannot be made at all. The request is the
  # only place it appears; nothing else in the framework has ever read it.
  sm["_model"] = str(getattr(llm_request, "model", "") or "")[:60]
  turn_input = _extract(callback_context, llm_request, sm)
  # An ASYNCHRONOUS tool's completion arrives as user text, never as a tool response, so
  # after_tool never sees it and this is the only place it can be recorded. Route it
  # through the intake tool rather than reimplementing _intake_executor here: intake
  # already records task_results, maps declared outputs into filled, sets
  # _task_just_completed and resets the steer-back counter, and CES tools cannot import
  # each other, so a local copy would be a verbatim duplicate with no way to pin it.
  _ingested = 0
  for _tool, _payload, _outcome in turn_input.pop("async_completions", None) or []:
    # A progressive fan-out leg publishes to state AND, later, produces the ordinary
    # completion envelope. Ingesting it twice would re-run the leg's then_say, so the
    # caller hears the same finding a second time on a later turn. Skipped only for a
    # leg already recorded: one still outstanding keeps its envelope, which is the
    # cross-turn safety net for a group whose watcher never saw it land.
    if _tool in (sm.get("_fanout_ingested") or []):
      _log(sm, "fanout_completion_skipped", tool=_tool)
      continue
    try:
      sm = tools.slot_intake({"input_data": {
          "tool_name": _tool,
          "response_data": _payload,
          # The envelope's own verdict. Intake falls back to it when the payload
          # carries no explicit success signal — an agent's reply never does.
          "outcome": _outcome,
          "sm": sm,
          "current_agent": "",
          "channel": callback_context.state.get("channel", ""),
      }}).json()["result"]["sm"]
      turn_input["sm"] = sm
      # Each intake call OVERWRITES the scalar `_task_just_completed`, and the engine's
      # post-executor handler pops exactly one. With two completions in a turn the first
      # would keep its entry in `_awaiting_async` and sit there until `max_turns` — a
      # timeout for a backend that answered, which is the precise failure harvesting all
      # envelopes was meant to prevent. Record the batch so the engine can clear them all.
      _done = sm.get("_task_just_completed")
      if _done:
        _batch = sm.setdefault("_async_batch", [])
        if _done not in _batch:
          _batch.append(_done)
      _ingested += 1
      _log(sm, "async_completion_ingested", tool=_tool)
    except Exception as _e:  # pylint: disable=broad-except
      # Never let an unparseable completion take the turn down; the task's own
      # awaits.max_turns deadline is the backstop. Keep going: one bad envelope must
      # not cost a sibling wait its perfectly good result.
      _log(sm, "async_completion_FAILED", "WARN", tool=_tool, err=repr(_e))
  # WHAT THE ENGINE IS TOLD IS WHAT WAS RECORDED, not what was on the wire, and the
  # difference costs a caller their answer.
  #
  # `async_completion_landed` is the engine's cue to read the turn as a DELIVERY: it
  # blanks the caller's speech so the terminal the completion unblocked can fire
  # (`_apply_answer_first`). That trade only pays when something actually landed. Every
  # envelope above can be SKIPPED — a progressive fan-out leg republishes its envelope on
  # a later turn, long after the group ingested it from state — and then nothing is
  # unblocked, nothing needs the floor, and blanking is pure loss: the slot stays open,
  # the engine never sees the answer, and the flow re-asks a question the caller already
  # answered.
  #
  # Driven on an agent that asks a question DURING a progressive fan-out: the legs
  # republish their envelopes on the turn the caller answers on (or, when that turn was
  # silent, merged into the next user content), and the answer was discarded on 12 of 12
  # voice drives — `async_completion_text_dropped`, the asked slot never filled, and the
  # reply the caller heard was an improvisation rather than the flow's own line.
  #
  # An ingestion that FAILED counts as nothing landing too, for the same reason: the
  # task's `awaits.max_turns` is the backstop, and until then the caller's turn is still
  # an ordinary turn.
  turn_input["async_completion_landed"] = bool(_ingested)
  # The legs of a parallel group, for the same reason and by the same route. after_tool
  # stood aside for these (it is invoked once per leg, concurrently, and racing writers
  # on one state key keep only the last), so their results are still sitting in the
  # request as function_response parts and this is where they are recorded. Ingested in
  # the DECLARATION order the engine dispatched them in, never arrival order, which is
  # neither stable nor observable.
  _legs = sm.get("_parallel_firing") or []
  if _legs:
    _payloads = _function_responses(llm_request)
    _landed = []
    _pending_legs = []
    for _tool in _legs:
      if _tool not in _payloads:
        continue  # not back yet; it stays fire-eligible and is picked up later
      if _is_pending(_payloads[_tool]):
        # A progressive fan-out leg is ASYNCHRONOUS, so the dispatch is answered with
        # CES's placeholder and the real payload is published to state instead. The
        # placeholder is not a result: handing it to intake would record it as one and
        # route the leg into its on_failure ladder, where max_retries defaults to 0 —
        # the group would escalate the flow on its own first fire, with nothing failed.
        _pending_legs.append(_tool)
        continue
      try:
        sm = tools.slot_intake({"input_data": {
            "tool_name": _tool,
            "response_data": _payloads[_tool],
            "sm": sm,
            "current_agent": "",
            "channel": callback_context.state.get("channel", ""),
        }}).json()["result"]["sm"]
        turn_input["sm"] = sm
        _done = sm.get("_task_just_completed")
        if _done and _done not in _landed:
          _landed.append(_done)
      except Exception as _e:  # pylint: disable=broad-except
        # One unreadable leg must not cost its siblings their results — that is the
        # whole point of the legs being independent.
        _log(sm, "parallel_leg_FAILED", "WARN", tool=_tool, err=repr(_e))
    if _landed:
      # Every leg gets its disposition, not just the last. The scalar the engine pops
      # holds one name; this list is what lets it speak each leg's then_say.
      sm["_completed_batch"] = _landed
      sm.pop("_parallel_firing", None)
      _log(sm, "parallel_batch_ingested", tasks=_landed)
    if _pending_legs:
      # Tell the engine which legs are genuinely running so it marks them in flight
      # (otherwise the selector sees them un-fired and dispatches the whole group
      # again) and starts watching. Reported by LEG NAME, which is the vocabulary the
      # engine's task selector and `<group>_done` are written in.
      _fan_tools = (sm.get("_fanout") or {}).get("tools") or {}
      _by_tool = {v: k for k, v in _fan_tools.items()}
      sm["_fanout_pending"] = [_by_tool[t] for t in _pending_legs if t in _by_tool]
      sm.pop("_parallel_firing", None)
      _log(sm, "fanout_legs_dispatched", tools=_pending_legs)
      turn_input["sm"] = sm
  # A progressive fan-out leg's real result arrives in STATE, not as a tool response —
  # the dispatch was answered `pending` and the body ran on in the background. Every
  # outstanding leg's key is read on every pass of the group, so a leg is narrated as
  # soon as the pass the watcher woke happens, and two landing inside one window are
  # both picked up. Ingested by the same route as everything else so the leg gets its
  # ordinary disposition (then_say, output slots, the on_failure ladder).
  _published = _fanout_publications(callback_context, sm)
  if _published:
    _fresh = []
    for _leg, _tool, _payload in _published:
      if not _tool:
        continue
      try:
        sm = tools.slot_intake({"input_data": {
            "tool_name": _tool,
            "response_data": _payload,
            "sm": sm,
            "current_agent": "",
            "channel": callback_context.state.get("channel", ""),
        }}).json()["result"]["sm"]
        turn_input["sm"] = sm
        _done = sm.get("_task_just_completed")
        if _done and _done not in _fresh:
          _fresh.append(_done)
        _fan = sm.setdefault("_fanout", {})
        _fan.setdefault("done_legs", []).append(_leg)
        # Keyed by TOOL so the later completion envelope for the same leg is
        # recognized and dropped rather than speaking the finding twice.
        _ing = sm.setdefault("_fanout_ingested", [])
        if _tool not in _ing:
          _ing.append(_tool)
      except Exception as _e:  # pylint: disable=broad-except
        # One unreadable publication must not cost its siblings their results.
        _log(sm, "fanout_leg_FAILED", "WARN", leg=_leg, err=repr(_e))
    if _fresh:
      _batch = sm.get("_completed_batch") or []
      sm["_completed_batch"] = _batch + [n for n in _fresh if n not in _batch]
      _log(sm, "fanout_batch_ingested", tasks=_fresh)
      turn_input["sm"] = sm
  # Keep the volatile SI-trace diagnostic keys OUT of the engine tool-call.
  _dbg = {k: sm[k] for k in ("_si_trace", "_si_turn", "_capture_si") if k in sm}
  if _dbg:
    turn_input["sm"] = {k: v for k, v in sm.items() if k not in _dbg}
  try:
    result = tools.slot_filling_engine(
        {"input_data": turn_input}).json()["result"]
    out_sm = result["sm"]
    if _dbg:
      out_sm.update(_dbg)
    # Steering post-model net: stash the caller's utterance so the after_model callback
    # can match a route's backstop keywords / count disambiguation turns when the model
    # declines to route. One extra sm key; no behaviour change for any other app.
    out_sm["_steering_user_text"] = turn_input.get("last_user_text", "")
    return _apply(callback_context, llm_request, out_sm, result["action"])
  except Exception as _e:  # pylint: disable=broad-except
    _log(sm, "before_model_CRASH", err=repr(_e),
         tail=traceback.format_exc().strip().splitlines()[-1][:160])
    callback_context.state[_SM_KEY] = sm
    # Degrade safely: hide internal tools even on the crash path. A bare
    # `return None` here would hand the raw model the engine/intake/transfer
    # tools (they are normally hidden in _apply, which never ran), so the model
    # could call slot_filling_engine etc. directly. Hide them, then proceed.
    try:
      _hide_internal_tools(callback_context, llm_request)
      _strip_transfer_boilerplate(llm_request)
    except Exception:  # pylint: disable=broad-except
      pass
    return None
