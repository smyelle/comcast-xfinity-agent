# pylint: disable=undefined-variable,unused-argument,line-too-long,broad-exception-caught

"""After-model callback for video_specialist_agent.

Suppresses the platform's generic retry fallback ("Hmm, I'm having trouble with
that. Do you want me to try again?") on the post-transfer LLM pass.

Why this exists
---------------
The XIT Recommended Action delivers the Convoy customer message AND calls
`transfer_potato_to_agent_v2` (skill="appointment") in the SAME turn, then — per
the NO POST-TRANSFER CONFIRMATION rule — is told to end the turn silently. CES
runs a second LLM pass after the tool returns; that intentionally text-less turn
gets replaced by the generic "Hmm, I'm having trouble..." fallback, which then
leaks to the customer even though steering already routed the potato transfer.

The orchestrator solves the equivalent leak for skill="human" transfers via
`_suppress_post_transfer_llm_text` (armed in its before_tool callback). The video
specialist has no before_tool/after_model callbacks of its own, so the appointment
path was unguarded. This callback is fully self-contained: it arms suppression the
moment it sees a `transfer_potato_to_agent_v2` call in the response, and blanks the
generic fallback on the immediately following pass (or if co-emitted in the same
response). It never touches legitimate video prose — the specialist never authors
that fallback phrase (failures route to a human instead).
"""

from typing import Optional
import json

# The platform's generic retry fallback substituted for a text-less post-tool turn.
_FALLBACK_MARKERS = ("having trouble with that", "do you want me to try again")

# Video-scoped state flag: armed when a potato transfer is emitted, so the very
# next (text-less) pass can be recognized and its injected fallback suppressed.
_SUPPRESS_FLAG = "_video_post_transfer_suppress"

# ---------------------------------------------------------------------------
# VIDEO XIT "View alert summary" CARD (deterministic)
# ---------------------------------------------------------------------------
# Mirrors the Internet XIT path: the recommendation's adkCustomerMessage stays the
# HEADLINE (the specialist delivers it verbatim per its instruction, unchanged), and
# we ADDITIVELY prepend the same approved collapsible card carrying the four-part
# explanation. The four fields come from {video_summary_fields}, populated by the
# check_convoy_recommendations tool from its per-recommendation registry — the model
# never authors this HTML, and never sees it.
#
# Rendered HERE (not by the model) because the XIT turn must emit text AND the
# transfer tool together; the card is attached to that same turn's text.
# Card HTML is byte-for-byte identical to the orchestrator's renderers.
_SUMMARY_SECTIONS = (
    ("happening", "What's happening"),
    ("why", "Why"),
    ("doing", "What we're doing"),
    ("todo", "What you need to do"),
)
# Suppress the HTML card ONLY on a recognized voice/IVR platform; web is the default.
_VOICE_PLATFORMS = {"ivr", "voice", "phone", "audio", "voip"}
# Guard so the card is attached at most ONCE per conversation.
_CARD_SHOWN_FLAG = "_video_summary_card_shown"


def _esc_summary_field(s) -> str:
  """Escape only the field TEXT (never the card's own tags)."""
  return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_summary_card(fields) -> str:
  ps = "".join(
      "<p style='margin:8px 0;'><strong>{}:</strong> {}</p>".format(
          label, _esc_summary_field(str(fields[key]).strip())
      )
      for key, label in _SUMMARY_SECTIONS
      if str(fields.get(key, "")).strip()
  )
  return (
      "<details style='border:1px solid #d0d0d0;border-radius:8px;padding:8px 12px;margin-top:10px;background:#ffffff;color:#202124;'>"
      "<summary style='cursor:pointer;font-weight:bold;color:#1a73e8;'>View alert summary</summary>"
      "<div style='margin-top:8px;line-height:1.5;color:#202124;'>{}</div></details>".format(ps)
  )


def _video_summary_fields(callback_context):
  """Customer copy for the matched VIDEO rec: {headline?, happening, why, doing, whatYouNeedToDo}."""
  try:
    raw = callback_context.state.get("video_summary_fields", "")
    if not raw:
      return {}
    fields = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(fields, dict):
      return {}
    allowed = set(dict(_SUMMARY_SECTIONS)) | {"headline"}
    return {k: str(v) for k, v in fields.items() if k in allowed and v}
  except Exception as e:  # noqa: BLE001
    print(f"[video after_model_callback] summary fields unavailable: {e}")
    return {}


def _attach_summary_card(callback_context, parts) -> bool:
  """Render the XIT card onto the FIRST visible text part of this turn.

  When product authored a HEADLINE for this recommendation, that headline REPLACES the
  model's delivered text (which is the raw adkCustomerMessage, per the specialist's XIT
  Recommended Action instruction). Without an authored headline the model's text is kept
  as-is and the card is simply prepended.

  Fully guarded: missing data, a voice surface, an already-shown card, or any exception
  leaves the turn's text completely untouched. Returns True only when text changed.
  """
  try:
    if str(callback_context.state.get(_CARD_SHOWN_FLAG, "")).lower() == "true":
      return False
    platform = str(callback_context.state.get("platform", "") or "").strip().lower()
    if platform in _VOICE_PLATFORMS:
      return False
    fields = _video_summary_fields(callback_context)
    headline = fields.pop("headline", "")
    if not fields:
      return False
    for part in parts:
      text = getattr(part, "text", None)
      if not isinstance(text, str) or not text.strip():
        continue
      if "<details" in text.lower():
        return False  # a card is somehow already present — never add a second
      body = headline or text.strip()
      part.text = (_render_summary_card(fields) + "\n\n" + body).strip()
      callback_context.state[_CARD_SHOWN_FLAG] = "true"
      print(
          "[video after_model_callback] Attached XIT troubleshooting summary card"
          f" (authored headline: {'yes' if headline else 'no'})."
      )
      return True
    return False
  except Exception as e:  # noqa: BLE001
    print(f"[video after_model_callback] summary card render skipped: {e}")
    return False




def _blank_fallback_parts(parts) -> bool:
  """Blank any part whose text is the generic fallback. Returns True if changed."""
  stripped = False
  for part in parts:
    text = getattr(part, "text", None)
    if isinstance(text, str) and text.strip() and any(
        mk in text.lower() for mk in _FALLBACK_MARKERS
    ):
      part.text = ""
      stripped = True
  return stripped


def after_model_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
  """Strip the generic retry fallback that follows a potato transfer.

  Returns the modified llm_response when the fallback is suppressed, else None."""
  try:
    content = getattr(llm_response, "content", None)
    if content is None:
      return None
    parts = getattr(content, "parts", None) or []
    if not parts:
      return None

    has_potato_transfer = any(
        getattr(getattr(p, "function_call", None), "name", None)
        == "transfer_potato_to_agent_v2"
        for p in parts
    )
    has_any_function_call = any(getattr(p, "function_call", None) for p in parts)

    # Case 1: the transfer pass itself. Arm suppression for the follow-up pass and
    # strip any fallback text co-emitted alongside the transfer call (keep the call).
    if has_potato_transfer:
      callback_context.state[_SUPPRESS_FLAG] = "true"
      changed = _blank_fallback_parts(parts)
      if changed:
        print("[video after_model_callback] Stripped fallback text co-emitted with"
              " potato transfer.")
      # ADDITIVE: attach the XIT explanation card to this turn's customer message.
      # Runs AFTER fallback-blanking so a blanked part is never used as the anchor.
      if _attach_summary_card(callback_context, parts):
        changed = True
      return llm_response if changed else None


    armed = str(callback_context.state.get(_SUPPRESS_FLAG, "")).lower() == "true"
    if not armed:
      return None

    # Case 2: the post-transfer, text-only pass whose empty turn CES replaced with
    # the generic fallback. Suppress it and disarm.
    if not has_any_function_call and _blank_fallback_parts(parts):
      callback_context.state[_SUPPRESS_FLAG] = "false"
      print("[video after_model_callback] Suppressed post-transfer generic fallback"
            " text.")
      return llm_response

    # Anything else substantive followed — disarm to avoid over-suppression.
    callback_context.state[_SUPPRESS_FLAG] = "false"
    return None
  except Exception as e:
    print(f"[video after_model_callback] Error during fallback suppression: {e}")
    return None

