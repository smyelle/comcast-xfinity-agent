"""Regression: relayed canned copy is TRANSLATED under an active language lock.

Closes the terminal-line language leak documented in the fedex language_hooks.py KNOWN
LIMITATION and flows-passA-language-switching-bug.md: canned copy the engine SPEAKS
VERBATIM (a preempt `message`) or DROPS (a terminal `message` the proceed-path SI fold
omits) bypasses the model — the only translator in the stack — so under a non-default
caller language it leaks in the authored (English) wording even with the app's
<language_lock> in force.

THE FIX (runtime model-relay, entirely in the two callbacks):

  before_model, when `active_language` is a real non-default switch, reroutes ONE canned
  prose line through the model as a <relay_translation> directive (PROCEED, not preempt)
  and stashes any teardown (end_session/payload) in `_deferred_terminal_parts`;
  after_model re-injects that teardown AFTER the model's translated close so the session
  ends only once the caller has heard the goodbye.

Gated on active_language — every English / non-language turn is byte-identical, and
`verbatim`/readback copy (PHI/PCI) is NEVER relayed. Everything here runs against the
REAL before_model and after_model callbacks loaded from source.
"""

from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


# --------------------------------------------------------------------------- #
# CES-shaped stubs (the pattern from tests/test_exhaust_reasoning_loop.py).

class _Part:
  def __init__(self, kind, **d):
    self.kind = kind
    self.text = d.get("text")
    self.function_call = d.get("function_call")
    self.__dict__.update({k: v for k, v in d.items() if k != "function_call"})

  @classmethod
  def from_text(cls, text=""):
    return cls("text", text=text)

  @classmethod
  def from_function_call(cls, name="", args=None):
    fc = type("FC", (), {"name": name, "args": args or {}})
    return cls("call", function_call=fc, name=name, args=args or {})

  @classmethod
  def from_agent_transfer(cls, agent=""):
    return cls("transfer", text=agent)

  @classmethod
  def from_json(cls, payload=""):
    return cls("json", payload=payload)

  @classmethod
  def from_end_session(cls, reason="", escalated=False):
    return cls("end_session", reason=reason, escalated=escalated)


class _Resp:
  def __init__(self, parts):
    self.content = type("C", (), {"parts": parts})
    self.parts = parts
    self.partial = None

  @classmethod
  def from_parts(cls, parts):
    return cls(parts)


class _Ctx:
  def __init__(self, state=None):
    self.state = dict(state or {})


class _Config:
  def __init__(self, system_instruction="base"):
    self.system_instruction = system_instruction
    self.hidden = []
    self.tools = None

  def hide_tool(self, name):
    self.hidden.append(name)


class _Request:
  def __init__(self, system_instruction="base", contents=None):
    self.config = _Config(system_instruction)
    self.contents = contents if contents is not None else []
    self.model = "gemini-3.1-flash-live"


def _load_abs(path, name):
  spec = importlib.util.spec_from_file_location(name, path)
  mod = importlib.util.module_from_spec(spec)
  for g in ("CallbackContext", "LlmRequest", "Content", "Tool", "ces_internal"):
    setattr(mod, g, type(g, (), {}))
  mod.Part = _Part
  mod.LlmResponse = _Resp
  mod.tools = type("tools", (), {})
  spec.loader.exec_module(mod)
  return mod


_BM = _load_abs(
    os.path.join(_ROOT, "src/flows/engine/framework/callbacks/before_model.py"),
    "_bm_relay")
_AM = _load_abs(
    os.path.join(_ROOT, "src/flows/engine/framework/callbacks/after_model.py"),
    "_am_relay")


_UC = [type("C", (), {"role": "user", "parts": [_Part.from_text("hola")]})]
_END = {"type": "end_session", "reason": "completed"}


def _apply(action, *, active_language=None, sm=None, contents=None):
  """Run before_model._apply_directive; return (result, request, sm_after)."""
  state = {"sm": dict(sm or {})}
  if active_language is not None:
    state["active_language"] = active_language
  ctx = _Ctx(state)
  req = _Request()
  req.contents = contents if contents is not None else []
  res = _BM._apply_directive(ctx, req, dict(sm or {}), action, "t")
  return res, req, ctx.state["sm"]


def _is_preempt(res):
  """A preempt returns an LlmResponse (parts); a proceed returns {'decision': 'OK'}."""
  return not (isinstance(res, dict) and res.get("decision") == "OK")


# --------------------------------------------------------------------------- #
# before_model: the relay gate.

def test_terminal_exhaust_relays_under_language_lock():
  """A terminal exhaust line under Spanish PROCEEDS with a translate directive and
  defers its end_session — instead of speaking the English line verbatim."""
  action = {"preempt": True, "message": "An error occurred. Goodbye.",
            "speech_class": "exhaust", "response": [_END]}
  res, req, sm = _apply(action, active_language="Spanish")

  assert not _is_preempt(res)                       # proceed: the model composes it
  si = req.config.system_instruction
  assert "<relay_translation>" in si
  assert "An error occurred. Goodbye." in si        # the line to translate
  assert "Spanish" in si                            # into the active language
  assert sm.get("_deferred_terminal_parts") == [_END]   # teardown deferred
  assert sm.get("_render_fallback") == "An error occurred. Goodbye."


def test_english_turn_is_byte_identical():
  """No active_language -> the exhaust is spoken VERBATIM (my empty-contents fix),
  no relay directive, no deferred teardown. English calls are unchanged."""
  action = {"preempt": True, "message": "An error occurred. Goodbye.",
            "speech_class": "exhaust", "response": [_END]}
  res, req, sm = _apply(action)                     # active_language absent

  assert _is_preempt(res)                           # verbatim preempt
  assert "An error occurred. Goodbye." in " ".join(
      (p.text or "") for p in res.parts if getattr(p, "text", None))
  assert "<relay_translation>" not in req.config.system_instruction
  assert "_deferred_terminal_parts" not in sm


def test_default_language_tokens_do_not_relay():
  """active_language set to an English token is still the default base -> no relay."""
  for tok in ("English", "en-US", "en", ""):
    action = {"preempt": True, "message": "All set. Goodbye.",
              "speech_class": "exhaust", "response": [_END]}
    res, req, _sm = _apply(action, active_language=tok)
    assert _is_preempt(res), tok
    assert "<relay_translation>" not in req.config.system_instruction, tok


def test_verbatim_readback_stays_literal():
  """PHI/PCI: a `verbatim` line (a readback) is NEVER relayed, even under Spanish."""
  action = {"preempt": True, "message": "Your tracking number is 1 2 3 4 5.",
            "verbatim": True, "speech_class": "reprompt"}
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC)
  assert _is_preempt(res)                           # spoken literally
  assert "<relay_translation>" not in req.config.system_instruction
  assert "_deferred_terminal_parts" not in sm


def test_cancel_proceed_path_gets_translate_directive():
  """The cancel goodbye is already preempt:False but the engine drops its message on a
  terminal turn; under Spanish the relay directive re-supplies it for translation."""
  action = {"preempt": False, "speech_class": "control",
            "message": "No problem. Let me know if there's anything else."}
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC)

  assert not _is_preempt(res)
  si = req.config.system_instruction
  assert "<relay_translation>" in si
  assert "No problem. Let me know if there's anything else." in si
  assert "_deferred_terminal_parts" not in sm       # no teardown to defer here


def test_function_call_fire_is_not_relayed():
  """A preempt that DISPATCHES a tool is an engine fire, not prose — never relay it
  (handing the call to the model would drop the dispatch)."""
  action = {"preempt": True, "message": "one moment",
            "speech_class": "filler",
            "function_call": {"name": "fedex_dag", "args": {}}}
  res, req, _sm = _apply(action, active_language="Spanish")
  assert _is_preempt(res)
  assert "<relay_translation>" not in req.config.system_instruction


def test_zombie_transfer_keeps_verbatim_path():
  """A terminal line whose sm carries a zombie hand-off must NOT relay: that path
  flushes transfer/exit_status on the verbatim rail and must not be reordered."""
  action = {"preempt": True, "message": "Connecting you now.",
            "speech_class": "control", "response": [_END]}
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC,
                        sm={"_zombie": {"transfer_to": "human_agent"}})
  assert _is_preempt(res)
  assert "<relay_translation>" not in req.config.system_instruction
  assert "_deferred_terminal_parts" not in sm


def test_non_relay_class_without_end_session_unchanged():
  """A non-terminal line whose class is not a relay class and carries no end_session
  is left alone even under Spanish (the model already composes such turns)."""
  action = {"preempt": True, "message": "Got it.", "speech_class": "await"}
  res, req, _sm = _apply(action, active_language="Spanish", contents=_UC)
  assert _is_preempt(res)
  assert "<relay_translation>" not in req.config.system_instruction


def test_no_input_reprompt_relays_under_language_lock():
  """The live tracking no_input leak: a silence-ladder reprompt is a canned PREEMPT
  (message spoken verbatim, model never runs) with speech_class 'no_input' and NO
  end_session. Under Spanish it must RELAY (translate) — not fall to a soft reminder the
  absent model turn can never honor. The inactivity marker rides as contents, so this
  also proves the preempt is not misread as a value-extraction proceed turn."""
  action = {"preempt": True, "speech_class": "no_input",
            "message": "Sorry, I didn't catch that. Please go ahead with your tracking "
                       "or door tag number whenever you're ready."}
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC)

  assert not _is_preempt(res)                        # proceed: the model translates it
  si = req.config.system_instruction
  assert "<relay_translation>" in si
  assert "door tag number" in si                     # the line to translate
  assert "Spanish" in si
  # The English backstop is marked language-locked so after_model suppresses it on an
  # empty completion instead of speaking the very copy the relay is translating away.
  assert sm.get("_render_fallback_lang") == "Spanish"


def test_no_input_reprompt_english_is_byte_identical():
  """No active_language -> the no_input reprompt is spoken VERBATIM as a preempt, with no
  relay directive and no language-locked backstop. English calls are unchanged."""
  action = {"preempt": True, "speech_class": "no_input",
            "message": "Sorry, I didn't catch that. Please go ahead whenever you're ready."}
  res, req, sm = _apply(action, contents=_UC)        # active_language absent

  assert _is_preempt(res)                            # verbatim preempt
  assert "<relay_translation>" not in req.config.system_instruction
  assert "_render_fallback_lang" not in sm


def test_empty_no_input_reprompt_is_not_relayed():
  """An EMPTY no_input reprompt is a silent wait tick (no message) — _relayable_prose
  drops it, so even under Spanish it is never rerouted through the model."""
  action = {"preempt": True, "speech_class": "no_input", "message": "", "silent": True}
  res, req, _sm = _apply(action, active_language="Spanish", contents=_UC)
  assert "<relay_translation>" not in req.config.system_instruction


# --------------------------------------------------------------------------- #
# before_model: the PROCEED-path gate (the live fedex leaks). On a proceed turn the
# engine folds the line into a <system_directive> in `si` and routes any teardown to a
# pending stash — NOT onto action['response'] — so _is_relayable misses it. These cover
# a terminal goodbye (leak 2) and an empty-contents auto-turn, and guard the one turn we
# must NOT relay: a value-extraction turn (relaying would drop the caller's answer).

_DIRECTIVE = ("\n<system_directive>\nThanks for choosing FedEx, and have a great day.\n"
              "Respond to the caller in your own words. Do NOT recite this directive.\n"
              "</system_directive>")

# A value-extraction turn's SI: a slot_filling_protocol directing a setter, plus the
# engine's <system_directive> fold of the awaited question (mirrors the real Pass-B SI).
_EXTRACT_SI = ("<slot_filling_protocol>\nCALL TOOLS FIRST: call set_has_account_number.\n"
               "</slot_filling_protocol>\n<system_directive>\nDo you have a FedEx account"
               " number?\nRespond in your own words. Do NOT recite this directive.\n"
               "</system_directive>")


def test_proceed_terminal_goodbye_relays_and_defers_pending_teardown():
  """The live tracking goodbye: preempt:False, message folded into the SI, end_session
  routed to _pending_announce_payloads. Relay must fire on the TERMINAL signal (regardless
  of contents), defer the end_session, and clear the stash so after_model does not inject
  it ahead of the translated close."""
  action = {"preempt": False, "si": _DIRECTIVE,
            "message": "Thanks for choosing FedEx, and have a great day."}
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC,
                        sm={"_pending_announce_payloads": [_END]})

  assert not _is_preempt(res)                        # proceed: the model translates it
  si = req.config.system_instruction
  assert "<relay_translation>" in si
  assert "Spanish" in si
  # The competing English fold is stripped; the line survives only inside the directive.
  assert "<system_directive>" not in si
  assert sm.get("_deferred_terminal_parts") == [_END]        # teardown deferred
  assert "_pending_announce_payloads" not in sm              # stash cleared
  assert sm.get("_render_fallback") == "Thanks for choosing FedEx, and have a great day."


def test_proceed_empty_contents_autoturn_relays():
  """An empty-contents auto-turn (no caller utterance to extract) with a folded proceed
  message is relayed — nothing to capture, so "say ONLY this" is safe."""
  action = {"preempt": False, "message": "Do you have a FedEx account number?"}
  res, req, sm = _apply(action, active_language="Spanish", contents=[])  # empty contents

  assert not _is_preempt(res)
  assert "<relay_translation>" in req.config.system_instruction
  assert "_deferred_terminal_parts" not in sm                # no teardown here


def test_proceed_extraction_turn_gets_soft_reminder_not_relay():
  """The live has_account_number re-ask: preempt:False, message folded, NON-empty
  contents (the caller just answered), NO pending end_session. This is a value-extraction
  turn — a "say ONLY this" relay would suppress the setter and DROP the caller's answer —
  so instead of rerouting the line the gate appends a SOFT <language_reminder> next to the
  engine's fold: the reply lands in the caller's language WITHOUT losing the setter."""
  action = {"preempt": False, "si": _EXTRACT_SI,
            "message": "Do you have a FedEx account number?"}
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC)  # caller present

  assert not _is_preempt(res)                        # still proceeds normally
  si = req.config.system_instruction
  assert "<relay_translation>" not in si             # NOT a say-only relay
  assert "<language_reminder>" in si                 # soft reminder appended
  assert "Spanish" in si
  assert "<slot_filling_protocol>" in si             # the setter directive survives
  assert "_deferred_terminal_parts" not in sm        # no deferral / relay semantics
  assert "_render_fallback" not in sm


def test_proceed_extraction_turn_english_is_byte_identical():
  """The soft reminder is gated on active_language too: with no switch in force the
  extraction turn is untouched — no <language_reminder>, SI passes through unchanged."""
  action = {"preempt": False, "si": _EXTRACT_SI,
            "message": "Do you have a FedEx account number?"}
  res, req, sm = _apply(action, contents=_UC)         # active_language absent
  assert "<language_reminder>" not in req.config.system_instruction
  assert "_render_fallback" not in sm


def test_proceed_relay_is_english_byte_identical():
  """The proceed gate is gated on active_language too: an English terminal goodbye is
  untouched — no relay directive, teardown left on its pending stash for the normal path."""
  action = {"preempt": False, "si": _DIRECTIVE,
            "message": "Thanks for choosing FedEx, and have a great day."}
  res, req, sm = _apply(action, contents=_UC,        # active_language absent
                        sm={"_pending_announce_payloads": [_END]})
  assert "<relay_translation>" not in req.config.system_instruction
  assert "_deferred_terminal_parts" not in sm
  assert sm.get("_pending_announce_payloads") == [_END]      # stash untouched


# --------------------------------------------------------------------------- #
# before_model: the RESPONSE-TEXT announce gate (path d, the live no_number_disconnect
# leak). A terminal announce authored as response=[{type:text,...},{type:end_session}]
# carries its prose as a text PART, not in action['message'] — so _is_relayable misses it
# (its prose probe reads message, empty here; its response probe rejects the non-deferrable
# text part) and the English text renders verbatim under a language lock.

_RESP_TEXT = {"type": "text",
              "text": ("No problem. Without a tracking or door tag number, I am not able "
                       "to look up the shipment myself. Thanks for choosing FedEx.")}


def test_response_text_terminal_announce_relays_and_defers_end_session():
  """no_number_disconnect: a preempt announce whose prose is a text PART on
  action['response'] alongside an inline end_session, action['message'] empty. Under
  Spanish it must PROCEED with a translate directive sourced from the RESPONSE text, and
  defer the end_session to land after the translated close."""
  action = {"preempt": True, "response": [_RESP_TEXT, _END]}
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC)

  assert not _is_preempt(res)                          # proceed: the model translates it
  si = req.config.system_instruction
  assert "<relay_translation>" in si
  assert _RESP_TEXT["text"] in si                      # the response text, not a message
  assert "Spanish" in si
  assert sm.get("_deferred_terminal_parts") == [_END]  # end_session deferred
  assert sm.get("_render_fallback") == _RESP_TEXT["text"]


def test_response_text_announce_english_is_byte_identical():
  """No active_language -> the announce renders its text PART verbatim (preempt), no relay
  directive, no deferred teardown. English calls are unchanged."""
  action = {"preempt": True, "response": [_RESP_TEXT, _END]}
  res, req, sm = _apply(action, contents=_UC)          # active_language absent

  assert _is_preempt(res)
  assert _RESP_TEXT["text"] in " ".join(
      (p.text or "") for p in res.parts if getattr(p, "text", None))
  assert "<relay_translation>" not in req.config.system_instruction
  assert "_deferred_terminal_parts" not in sm


def test_response_text_nonterminal_preempt_announce_relays_all_parts():
  """A NON-terminal PREEMPT announce (tracking's received_yes) — prose as text PART(s) on
  action['response'], action['message'] empty, NO end_session — is deterministic canned
  copy the model was never going to run for, so under Spanish it must PROCEED with a
  translate directive rather than speak the English text verbatim. Every text part is
  relayed (the pleasantry AND the trailing wrap-up ask), and there is no teardown to defer.
  """
  ask = {"type": "text",
         "text": "If that is all you need, press 8 now, or say track another shipment."}
  action = {"preempt": True, "response": [_RESP_TEXT, ask]}  # no end_session
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC)

  assert not _is_preempt(res)                          # proceed: the model translates it
  si = req.config.system_instruction
  assert "<relay_translation>" in si
  assert _RESP_TEXT["text"] in si and ask["text"] in si  # BOTH parts kept (ask not dropped)
  assert "Spanish" in si
  assert "_deferred_terminal_parts" not in sm          # non-terminal: nothing to defer
  assert sm.get("_render_fallback_lang") == "Spanish"  # fallback marked language-locked


def test_response_text_nonterminal_preempt_english_is_byte_identical():
  """No active_language -> the non-terminal preempt announce renders its text PARTs verbatim
  (preempt), no relay directive, no language marker. English calls are unchanged."""
  ask = {"type": "text", "text": "press 8 now, or say track another shipment."}
  action = {"preempt": True, "response": [_RESP_TEXT, ask]}
  res, req, sm = _apply(action, contents=_UC)          # active_language absent
  assert _is_preempt(res)
  spoken = " ".join((p.text or "") for p in res.parts if getattr(p, "text", None))
  assert _RESP_TEXT["text"] in spoken and ask["text"] in spoken
  assert "<relay_translation>" not in req.config.system_instruction
  assert "_render_fallback_lang" not in sm


def test_response_text_nonterminal_nonpreempt_announce_not_relayed():
  """A NON-preempt, non-terminal response-text announce is still left alone even under
  Spanish: the model runs for it and it may carry a value the model must capture, so it is
  composed under the app language lock, not rerouted. Boundary guard for path (d)."""
  action = {"preempt": False, "response": [_RESP_TEXT]}  # no end_session, not a preempt
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC)
  assert "<relay_translation>" not in req.config.system_instruction
  assert "_deferred_terminal_parts" not in sm
  assert "_render_fallback_lang" not in sm


def test_message_shape_still_owned_by_is_relayable():
  """When action['message'] IS present (the classic preempt shape), path (d) does NOT
  hijack it — _response_relay bails on a non-empty message, so _is_relayable stays the
  single owner of the message + inline-teardown shape and relays the message text."""
  action = {"preempt": True, "message": "All done. Goodbye.",
            "speech_class": "control", "response": [_END]}
  res, req, sm = _apply(action, active_language="Spanish", contents=_UC)
  assert not _is_preempt(res)                           # relayed via (a)
  assert "All done. Goodbye." in req.config.system_instruction
  assert sm.get("_render_fallback") == "All done. Goodbye."


# --------------------------------------------------------------------------- #
# after_model: deferred teardown re-injection.

def test_teardown_lands_after_translated_close():
  """The model spoke the translated goodbye; after_model appends the deferred
  end_session AFTER it, so the session ends only once the caller has heard it."""
  sm = {"_deferred_terminal_parts": [_END],
        "_render_fallback": "An error occurred. Goodbye."}
  ctx = _Ctx({"sm": sm})
  resp = _Resp([_Part.from_text("Ocurrió un error. Adiós.")])
  out = _AM.after_model_callback(ctx, resp)

  kinds = [p.kind for p in out.parts]
  assert kinds == ["text", "end_session"]           # translated close, THEN teardown
  assert out.parts[0].text.startswith("Ocurrió un error")
  assert "_deferred_terminal_parts" not in ctx.state["sm"]
  assert "_render_fallback" not in ctx.state["sm"]


def test_teardown_falls_back_to_authored_line_on_empty_completion():
  """If the model returns no text (and NO language switch is active), speak the authored line
  (better than silence) and STILL tear down — the deferred end_session is not lost."""
  sm = {"_deferred_terminal_parts": [_END],
        "_render_fallback": "An error occurred. Goodbye."}
  ctx = _Ctx({"sm": sm})
  out = _AM.after_model_callback(ctx, _Resp([]))     # empty completion, no language lock

  kinds = [p.kind for p in out.parts]
  assert kinds == ["text", "end_session"]
  assert "An error occurred. Goodbye." in out.parts[0].text


def test_teardown_empty_completion_suppresses_english_under_language_lock():
  """The e5493160 leak: relay fired on the terminal goodbye but the model returned empty
  (a flaky completion right after a setter turn). WITHOUT this, after_model spoke the English
  `_render_fallback` verbatim under the language lock. With `_render_fallback_lang` set, the
  English line is SUPPRESSED and the close lands on the teardown alone (a clean silent
  hang-up) — no English reaches the caller."""
  sm = {"_deferred_terminal_parts": [_END],
        "_render_fallback": "Thanks for choosing FedEx, and have a great day.",
        "_render_fallback_lang": "Spanish"}
  ctx = _Ctx({"sm": sm})
  out = _AM.after_model_callback(ctx, _Resp([]))     # empty completion under Spanish

  kinds = [p.kind for p in out.parts]
  assert kinds == ["end_session"]                    # teardown only, NO English text
  assert not any(getattr(p, "text", None) for p in out.parts)
  assert "_render_fallback" not in ctx.state["sm"]
  assert "_render_fallback_lang" not in ctx.state["sm"]


def test_render_empty_backstop_suppresses_english_under_language_lock():
  """The non-terminal counterpart (received_yes relay): no deferred teardown, but a relay
  armed `_render_fallback` (English) and marked it language-locked. On an empty completion
  the last-resort backstop must NOT speak the English line — it returns silence instead."""
  sm = {"_render_fallback": "Great, I'm glad it arrived.",
        "_render_fallback_lang": "Spanish"}
  ctx = _Ctx({"sm": sm})
  out = _AM.after_model_callback(ctx, _Resp([]))     # empty completion under Spanish

  assert out is not None
  assert out.parts == []                             # silence, not the English backstop
  assert "_render_fallback" not in ctx.state["sm"]
  assert "_render_fallback_lang" not in ctx.state["sm"]


def test_render_empty_backstop_speaks_fallback_without_language_lock():
  """No language marker -> the ordinary empty-render backstop is unchanged: it speaks the
  armed `_render_fallback` rather than surfacing the platform 'having trouble' render."""
  sm = {"_render_fallback": "What is your tracking number?"}
  ctx = _Ctx({"sm": sm})
  out = _AM.after_model_callback(ctx, _Resp([]))     # empty completion, no language lock

  spoken = " ".join((p.text or "") for p in out.parts if getattr(p, "text", None))
  assert "What is your tracking number?" in spoken


def test_after_model_no_deferred_is_noop():
  """No stashed teardown -> after_model does not fabricate one (byte-safe)."""
  ctx = _Ctx({"sm": {}})
  out = _AM.after_model_callback(ctx, _Resp([_Part.from_text("hello")]))
  # No deferred parts and nothing else pending -> returns None (unchanged response).
  assert out is None
