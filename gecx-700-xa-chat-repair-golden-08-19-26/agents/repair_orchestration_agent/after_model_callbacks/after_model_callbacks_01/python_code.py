# pylint: disable=undefined-variable,unused-argument,line-too-long,broad-exception-caught

"""After-model callback for repair_orchestration_agent.

Deterministically sanitizes the model's RAW output text the moment the model
returns it — BEFORE it becomes the streamed/recorded response chunk. This closes
the leak where downstream steering reads the FIRST text chunk (the raw model
output) instead of the after_agent-sanitized final chunk: e.g. a hallucinated
"<state_update>wifi_tips_given: 2</state_update>Next, let's ..." prefix reaching
the customer in production.

Unlike after_agent_callback (which only cleans the FINAL surfaced text and thus
cannot reach the already-emitted raw chunk), this hook rewrites the LlmResponse
in place, so the single recorded chunk is clean. It is intentionally narrow:
it removes hallucinated tags and leaked flow-control state fragments only, and
never touches legitimate natural-language prose.
"""

import re
from typing import Optional

# Any closed / self-closing / single XML-or-HTML-like tag the LLM hallucinates
# (e.g. <state_update>...</state_update>, <thinking>, <plan>). This agent emits
# no legitimate markup, so stripping all such tags is safe.
_CLOSED_TAG = re.compile(r"<[^>]+>.*?</[^>]+>|<[^>]+/>|<[^>]+>", re.DOTALL)

# A leading UNCLOSED/truncated tag start the closed-tag pattern misses
# (e.g. a "<state_update" cut off before its '>'). Only the "<tagname" token is
# removed; a genuine customer message never starts with "<" + a letter.
_LEADING_TAG_TOKEN = re.compile(r"^\s*<\s*/?[A-Za-z][\w:.\-]*")

# Known flow-control / diagnostic state-variable names. Only these are stripped,
# so natural language is never touched.
_STATE_NAMES = (
    r"async_speed_test_result|async_speed_test_execution_id|speed_test_execution_enabled|speed_test_async_result_pending|xbo_id|wifi_troubleshooting_agent_enabled|wifi_troubleshooting_agent_active|wifi_blaster_plan_id|wifi_blaster_result|wifi_blaster_target_device|wifi_flow_active|wifi_offer_pending|wifi_scoping_pending|wifi_tips_given|wifi_pod_help_pending|wifi_status|"
    r"device_help_active|device_help_pending|device_help_steps_given|device_help_target|"
    r"app_issue_reported|app_issue_target|diagnostics_triggered|intent_clarified|intent_clarification_pending|"
    r"outage_inquiry_answered|outage_inquiry_pending|outage_troubleshoot_offer_pending|awaiting_outage_consent|"
    r"escalate_to_human_flag|direct_response_mode|outage_status|network_status|gateway_status|account_status|"
    r"convoy_status|technician_type"
)
_VALUE_TOKEN = (
    r"\{[^}]*\}|true|false|on|off|null|none|pending|done|skipped|error|success|clear|impaired|healthy|active|degradation|"
    r"offline|reboot|swap|predictive_swap|predictive_offline|no_telemetry|unsupported_device|resolved|handoff|"
    r"suspended|disconnected|network_tech|install_repair_tech|\d+"
)
# A LEADING run of "name: value" state assignments (the exact dump shape the model
# prefixes, e.g. "wifi_offer_pending: falsewifi_scoping_pending: true").
_LEADING_STATE = re.compile(
    rf"^\s*(?:(?:{_STATE_NAMES})\s*[:=]\s*(?:{_VALUE_TOKEN})?\s*)+",
    re.IGNORECASE,
)
# Any remaining inline "name: value" state fragment glued into the text.
_INLINE_STATE = re.compile(
    rf"(?:{_STATE_NAMES})\s*[:=]\s*(?:{_VALUE_TOKEN})?",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# APPROVED <details> CARDS ALLOW-LIST (kept in sync with after_agent_callback)
# ---------------------------------------------------------------------------
# The agent legitimately emits a small set of collapsible <details> cards on
# web/app surfaces (the post-diagnostics "View troubleshooting summary" and the
# "View speed test results" card). This after_model hook runs BEFORE the
# after_agent allow-list, so the approved cards must be protected here too — the
# _CLOSED_TAG stripper above would otherwise shred them. Any <details> whose
# <summary> label is NOT on this list is treated as hallucinated HTML and stripped.
# (Entity-encoded cards — &lt;details&gt; — carry no literal '<', so _CLOSED_TAG
# never touches them here; the after_agent callback repairs/protects those.)
_APPROVED_SUMMARY_LABELS = (
    "view troubleshooting summary",
    "view alert summary",
    "view speed test results",
)
_DETAILS_BLOCK_PATTERN = re.compile(r"<details\b[^>]*>.*?</details>", re.DOTALL | re.IGNORECASE)
_SUMMARY_LABEL_PATTERN = re.compile(r"<summary\b[^>]*>(.*?)</summary>", re.DOTALL | re.IGNORECASE)


def _is_approved_details(block: str) -> bool:
  """True only when the block is a well-formed <details> whose <summary> label is
  on the approved allow-list."""
  m = _SUMMARY_LABEL_PATTERN.search(block)
  if not m:
    return False
  label = re.sub(r"<[^>]+>", "", m.group(1)).strip().lower()
  return label in _APPROVED_SUMMARY_LABELS


def _protect_approved_details(text: str):
  """Replace each APPROVED <details> card with an opaque placeholder so the tag
  stripper leaves it untouched. Returns (protected_text, {placeholder: block})."""
  if not text or "<details" not in text.lower():
    return text, {}
  blocks: dict = {}

  def _sub(match):
    block = match.group(0)
    if _is_approved_details(block):
      token = "\x00APPROVED_DETAILS_{}\x00".format(len(blocks))
      blocks[token] = block
      return token
    return block  # unknown/malformed card -> leave for the sanitizer to strip

  return _DETAILS_BLOCK_PATTERN.sub(_sub, text), blocks


def _restore_approved_details(text: str, blocks: dict) -> str:
  """Puts the protected approved <details> cards back exactly as they were."""
  for token, block in blocks.items():
    text = text.replace(token, block)
  return text


# ---------------------------------------------------------------------------
# TROUBLESHOOTING SUMMARY: render the model's plain-text [[TS]] block into the
# approved collapsible <details> card (deterministic — the model never writes HTML)
# ---------------------------------------------------------------------------
# The model emits ONLY four plain-text fields inside a [[TS]]...[[/TS]] block; we
# render the exact literal-HTML card here. This makes the card structurally
# reliable: always valid tags, always exactly the four product-approved sections,
# never entity-encoded, never duplicated, never freelanced.
_TS_BLOCK = re.compile(r"\[\[\s*TS\s*]](.*?)\[\[\s*/\s*TS\s*]]", re.DOTALL | re.IGNORECASE)
_TS_KEYS = ("happening", "why", "doing", "todo")
_TS_LABELS = {
    "happening": "What's happening",
    "why": "Why",
    "doing": "What we're doing",
    "todo": "What you need to do",
}
# Safety net: a stray/orphan [[TS]] or [[/TS]] marker with no matching pair.
_TS_STRAY = re.compile(r"\[\[\s*/?\s*TS\s*]]", re.IGNORECASE)


def _esc(s: str) -> str:
  """Escape only the FIELD TEXT (never the card's own tags)."""
  return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_summary_card(fields: dict) -> str:
  """Build the exact literal-HTML collapsible card from the four fields."""
  ps = "".join(
      "<p style='margin:8px 0;'><strong>{}:</strong> {}</p>".format(
          _TS_LABELS[k], _esc(fields[k].strip())
      )
      for k in _TS_KEYS
      if fields.get(k, "").strip()
  )
  return (
      "<details style='border:1px solid #d0d0d0;border-radius:8px;padding:8px 12px;margin-top:10px;background:#ffffff;color:#202124;'>"
      "<summary style='cursor:pointer;font-weight:bold;color:#1a73e8;'>View troubleshooting summary</summary>"
      "<div style='margin-top:8px;line-height:1.5;color:#202124;'>{}</div></details>".format(ps)
  )


def _expand_summary_block(text):
  """Convert a [[TS]]...[[/TS]] block into the approved literal-HTML card, placed
  FIRST, followed by the rest of the message. Returns (new_text, changed)."""
  if not text or "[[" not in text:
    return text, False
  m = _TS_BLOCK.search(text)
  if not m:
    # No well-formed block; drop any stray orphan markers so they never render.
    stripped = _TS_STRAY.sub("", text)
    return (stripped, stripped != text)
  body = m.group(1)
  fields = {}
  for k in _TS_KEYS:
    fm = re.search(
        r"(?is)\b" + k + r"\s*:\s*(.*?)(?=\n\s*(?:" + "|".join(_TS_KEYS) + r")\s*:|\[\[\s*/\s*TS\s*]]|\Z)",
        body,
    )
    if fm:
      fields[k] = fm.group(1).strip()
  if not any(fields.get(k, "").strip() for k in _TS_KEYS):
    # Empty/garbled block — just remove it rather than render an empty card.
    return (text[: m.start()].strip() + "\n\n" + text[m.end():].strip()).strip(), True
  card = _render_summary_card(fields)
  before = text[: m.start()].strip()
  after = _TS_STRAY.sub("", text[m.end():]).strip()
  out = card + (("\n\n" + after) if after else "")
  out = ((before + "\n\n" + out) if before else out).strip()
  return out, True


def _sanitize(text: str) -> str:
  """Removes hallucinated tags and leaked state fragments while PRESERVING the
  approved <details> cards; returns the original string UNCHANGED when nothing
  leaked (lossless guard)."""
  if not text or not isinstance(text, str):
    return text
  original = text
  # FIRST: render the model's plain-text [[TS]] summary block into the approved
  # literal-HTML card (so the tag stripping/allow-list below treats it as a real
  # approved card). Deterministic — the model never writes the HTML itself.
  text, _ts_changed = _expand_summary_block(text)
  # Protect approved <details> cards behind opaque placeholders so the tag
  # stripper below cannot shred them.
  working, protected = _protect_approved_details(text)
  cleaned = _CLOSED_TAG.sub("", working)
  # Peel a leading unclosed tag start left behind by a truncated tag.
  deleadtag = _LEADING_TAG_TOKEN.sub("", cleaned, count=1)
  if deleadtag != cleaned:
    cleaned = deleadtag.lstrip()
  # Peel a leading run of "name: value" state assignments.
  cleaned = _LEADING_STATE.sub("", cleaned)
  # Remove any remaining inline state fragments.
  cleaned = _INLINE_STATE.sub(" ", cleaned)
  # Tidy leftover separators ONLY when something was stripped from the protected
  # working text. Collapse ONLY spaces/tabs — NEVER newlines — so multi-line
  # formatting (pod intro + blank line + bullet list) is preserved. NOTE: '<'/'>'
  # are intentionally NOT in the leading-trim class, so a restored approved
  # <details> card that leads the reply is never damaged.
  if cleaned != working:
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"^[ \t\"'{}\[\],:;=|-]+", "", cleaned).strip()
  # Restore the protected approved cards verbatim.
  cleaned = _restore_approved_details(cleaned, protected)
  return original if cleaned == original else cleaned


# ---------------------------------------------------------------------------
# STRAY VIDEO-TOOL CALL GUARD (deterministic redirect to the Video specialist)
# ---------------------------------------------------------------------------
# The orchestrator owns NO Video/TV tools (restart_ip_stb, whole_home_refresh,
# get_video_devices, lookup_xre_error all belong to `video_specialist_agent`).
# When a Video conversation is mid-flow and the customer sends a bare answer
# (e.g. picking a TV: "Living Room"), the turn can land on the orchestrator; its
# LLM then tries to take the next Video action and emits a function call it does
# not own — a real Video tool (restart_ip_stb) OR a hallucinated one
# (get_video_device_telemetry). The platform rejects it as
# `unexpected_function_call` and surfaces the generic "Hmm, I'm having trouble
# with that. Do you want me to try again?" fallback.
#
# This guard catches that deterministically: if the orchestrator emits any
# function call it does NOT own while Video is enabled, and the call looks like a
# Video action (or the session is mid-Video), rewrite the response into a
# transfer_to_agent(video_specialist_agent) so the specialist owns the turn.
# Independent of LLM adherence and of the upstream steering decision.
_ORCH_TOOLS = frozenset({
    "check_convoy_recommendations", "check_outage", "end_session",
    "fetch_customer_context", "gateway_specialist_agent_as_a_tool",
    "network_specialist_agent_as_a_tool", "outage_specialist_agent_as_a_tool",
    "perform_speed_test", "prepare_speed_test", "post_user_notification",
    "reboot", "search_connectivity_help", "search_wifi_settings_help",
    "transfer_potato_to_agent_v2", "transfer_to_agent",
})
# Tokens that mark a stray/hallucinated call as belonging to the Video/TV domain.
_VIDEO_TOOL_HINTS = ("stb", "video", "whole_home", "ip_stb", "xre", "refresh", "telemetry")
# The platform's generic retry fallback shown when the model emits an
# unexpected/unknown function call. If the pipeline substitutes this text BEFORE
# after_model sees it (stripping the raw function_call), we still redirect on it
# when the session is clearly a Video/TV conversation (Case B below).
_FALLBACK_MARKERS = ("having trouble with that", "do you want me to try again")


def _is_video_context(state) -> bool:
  """Reliable, NON-LLM signals that the current conversation is Video/TV:
  the sticky Video flow flag (set deterministically by the Video agent's
  before-agent callback) or the NLU intent (e.g. 'tv.equipment.issue')."""
  if str(state.get("video_flow_active", "")).lower() == "true":
    return True
  nlu = str(state.get("nlu_intent", "")).lower()
  return nlu.startswith("tv") or "video" in nlu


def _redirect_stray_video_call(callback_context, llm_response):
  """If the orchestrator emitted a Video/unknown function call it does not own
  (Case A), OR the platform already replaced such a call with the generic retry
  fallback (Case B) while a Video conversation is in play, rewrite the response
  into a transfer to `video_specialist_agent`. Returns the modified
  llm_response, or None."""
  try:
    if str(callback_context.state.get("video_flow_enabled", "")).lower() != "true":
      return None
    content = getattr(llm_response, "content", None)
    parts = getattr(content, "parts", None) or []

    stray = None
    fallback_text = False
    for part in parts:
      fc = getattr(part, "function_call", None)
      name = getattr(fc, "name", None) if fc else None
      if name and name not in _ORCH_TOOLS:
        stray = name
        break
      t = getattr(part, "text", None)
      if t and any(mk in t.lower() for mk in _FALLBACK_MARKERS):
        fallback_text = True

    if not stray and not fallback_text:
      return None

    # Diagnostic (helps confirm in dev whether after_model sees the RAW call
    # (Case A) or the already-substituted fallback text (Case B)).
    print(
        "[after_model_callback] video-guard inspecting:"
        f" stray_call={stray!r} fallback_text={fallback_text}"
        f" video_flow_active={callback_context.state.get('video_flow_active')!r}"
        f" nlu_intent={callback_context.state.get('nlu_intent')!r}"
    )

    if stray:
      # Case A: raw stray call still present. Redirect when it looks like a Video
      # action OR the session is a Video conversation.
      if not (
          _is_video_context(callback_context.state)
          or any(h in stray.lower() for h in _VIDEO_TOOL_HINTS)
      ):
        return None
    else:
      # Case B: the function_call was already stripped and replaced by the retry
      # fallback text. Only redirect when the session is clearly Video/TV, to
      # avoid hijacking an unrelated (e.g. Internet) fallback.
      if not _is_video_context(callback_context.state):
        return None

    # Keep the Video conversation sticky for the specialist's turn.
    callback_context.state["video_flow_active"] = "true"
    content.parts = [Part.from_agent_transfer("video_specialist_agent")]
    print(
        "[after_model_callback] Redirected"
        f" (stray_call={stray!r}, fallback_text={fallback_text})"
        " -> transfer_to_agent(video_specialist_agent)."
    )
    return llm_response
  except Exception as e:
    print(f"[after_model_callback] stray-video-call redirect failed: {e}")
    return None


def _is_human_transfer_call(part) -> bool:
  """True when the part is transfer_potato_to_agent_v2(skill=human)."""
  fc = getattr(part, "function_call", None)
  if not fc or getattr(fc, "name", None) != "transfer_potato_to_agent_v2":
    return False
  args = getattr(fc, "args", None) or {}
  if not isinstance(args, dict):
    return False
  return str(args.get("skill", "")).strip().lower() == "human"


def after_model_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
  """Rewrites the raw model text in place so the recorded/streamed chunk is clean.

  Returns the modified llm_response when a leak was stripped, else None (leaving
  the response untouched)."""
  try:
    # FIRST: deterministically catch a stray Video/unknown function call the
    # orchestrator does not own and redirect it to the Video specialist (prevents
    # the "unexpected_function_call" -> "Hmm, I'm having trouble" fallback).
    redirected = _redirect_stray_video_call(callback_context, llm_response)
    if redirected is not None:
      return redirected
    content = getattr(llm_response, "content", None)
    if content is None:
      return None
    parts = getattr(content, "parts", None) or []

    # Human-transfer de-dupe guard: once transfer_potato_to_agent_v2(skill=human)
    # is fired, the model often emits a second, redundant handoff line after the
    # tool response in the same turn. Drop that post-tool text pass entirely.
    suppress_post_transfer = (
        str(callback_context.state.get("_suppress_post_transfer_llm_text", "")).lower() == "true"
    )
    has_any_function_call = any(getattr(p, "function_call", None) for p in parts)
    has_human_transfer_call = any(_is_human_transfer_call(p) for p in parts)
    if suppress_post_transfer and not has_any_function_call and not has_human_transfer_call:
      suppressed = False
      for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
          part.text = ""
          suppressed = True
      if suppressed:
        callback_context.state["_suppress_post_transfer_llm_text"] = "false"
        print("[after_model_callback] Suppressed duplicate post-transfer handoff text.")
        return llm_response

    changed = False
    for part in parts:
      text = getattr(part, "text", None)
      if not text or not isinstance(text, str):
        continue
      cleaned = _sanitize(text)
      if cleaned != text:
        try:
          part.text = cleaned
        except Exception:
          continue
        changed = True
        print(
            "[after_model_callback] Sanitized raw model text. "
            f"Before: {text!r} After: {cleaned!r}"
        )
    return llm_response if changed else None
  except Exception as e:
    print(f"[after_model_callback] Error during sanitization: {e}")
    return None
