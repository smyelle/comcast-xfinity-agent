import re
import traceback
from typing import Optional


# Regex to strip XML/HTML-like tags the LLM sometimes hallucinates (e.g., <state_update>...</state_update>)
_XML_TAG_PATTERN = re.compile(r"<[^>]+>.*?</[^>]+>|<[^>]+/>|<[^>]+>", re.DOTALL)

# ---------------------------------------------------------------------------
# APPROVED <details> CARDS ALLOW-LIST (deterministic exception to tag stripping)
# ---------------------------------------------------------------------------
# The agent's output-purity rule forbids ALL HTML — EXCEPT a small, curated set of
# collapsible <details> cards (the post-diagnostics "View troubleshooting summary" and the
# "View speed test results" card). Those are legitimately emitted on web/app surfaces.
# _XML_TAG_PATTERN would otherwise shred them (stripping the <summary> label so the browser
# renders a bare "Details" toggle with the raw text inside), so BEFORE sanitizing we PROTECT
# each approved card behind an opaque placeholder and RESTORE it verbatim afterward.
#
# TO ADD A NEW APPROVED CARD IN THE FUTURE: append its exact <summary> label (lower-case) to
# _APPROVED_SUMMARY_LABELS below. Nothing else needs to change — any <details> whose <summary>
# text is NOT on this list is still treated as hallucinated HTML and stripped.
_APPROVED_SUMMARY_LABELS = (
    "view troubleshooting summary",
    "view alert summary",
    "view speed test results",
)

# Matches a single, non-nested <details>...</details> block (our cards never nest another
# <details>, so a non-greedy body correctly stops at the first closing tag).
_DETAILS_BLOCK_PATTERN = re.compile(r"<details\b[^>]*>.*?</details>", re.DOTALL | re.IGNORECASE)
# Extracts the visible label from the block's <summary>...</summary> for allow-list matching.
_SUMMARY_LABEL_PATTERN = re.compile(r"<summary\b[^>]*>(.*?)</summary>", re.DOTALL | re.IGNORECASE)


def _is_approved_details(block: str) -> bool:
  """True only when the block is a well-formed <details> whose <summary> label is on the
  approved allow-list. Anything else (no <summary>, or an unknown label) is NOT approved and
  will be stripped by the normal tag sanitizer."""
  m = _SUMMARY_LABEL_PATTERN.search(block)
  if not m:
    return False
  label = re.sub(r"<[^>]+>", "", m.group(1)).strip().lower()
  return label in _APPROVED_SUMMARY_LABELS


def _protect_approved_details(text: str):
  """Replaces each APPROVED <details> card with an opaque placeholder so the downstream tag
  sanitizers leave it untouched. Returns (protected_text, {placeholder: original_block}).
  Non-approved <details> blocks are left in place so the sanitizer still strips them."""
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
# approved collapsible <details> card (deterministic — the model never writes HTML).
# ---------------------------------------------------------------------------
# This MIRRORS after_model_callback: the model emits ONLY four plain-text fields in a
# [[TS]]...[[/TS]] block; we render the exact literal-HTML card here. after_model normally
# renders it on the raw chunk first (so by the time this runs the text already holds a valid
# rendered card and this is a no-op). This is the SAFETY NET for the final-surfaced-text path:
# if a raw [[TS]] block ever reaches here un-rendered, we still produce the approved card
# (and never leak raw [[TS]] markers to the customer). Keep this byte-for-byte identical to
# the after_model version so both paths produce the same card.
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


# ---------------------------------------------------------------------------
# ENTITY-ENCODED APPROVED CARD REPAIR
# ---------------------------------------------------------------------------
# The instruction stores the approved <details> template HTML-ENTITY-ENCODED (&lt;details&gt;...)
# because the instruction file is itself pseudo-XML and a literal <details> would break its
# structure. The model frequently reproduces those entities LITERALLY in its output, e.g.:
#   <details style='...'&gt;&lt;summary ...&gt;View troubleshooting summary&lt;/summary&gt;...&lt;/details&gt;
# The browser then renders a broken bare "Details" toggle (no real <summary>), and BOTH our
# allow-list protector and _XML_TAG_PATTERN miss it (they need a literal '>' / '</details>').
# We repair it deterministically: locate the card region (start '<details' or '&lt;details',
# end '</details>' or '&lt;/details&gt;'), and if it carries an APPROVED summary label, HTML-
# unescape ONLY that region into valid literal HTML. The trailing customer message is untouched.
_DETAILS_START_PATTERN = re.compile(r"(?:<|&lt;)details\b", re.IGNORECASE)
_DETAILS_END_PATTERN = re.compile(r"(?:</|&lt;/)details(?:>|&gt;)", re.IGNORECASE)


def _unescape_html_entities(s: str) -> str:
  """Minimal, deterministic HTML-entity unescape for the tags our cards use. '&amp;' is done
  LAST so an already-literal '&' in body text is never double-decoded."""
  return (
      s.replace("&lt;", "<")
       .replace("&gt;", ">")
       .replace("&quot;", '"')
       .replace("&#39;", "'")
       .replace("&apos;", "'")
       .replace("&amp;", "&")
  )


def _repair_entity_encoded_details(text: str):
  """If the reply contains an APPROVED card emitted with entity-encoded tags (or a mix), decode
  that card region into valid literal HTML so it renders and so the allow-list protector can see
  it. Returns (repaired_text, changed?). Leaves everything else (including the message) intact."""
  if not text or "details" not in text.lower():
    return text, False
  changed = False
  result = text
  search_from = 0
  while True:
    m_start = _DETAILS_START_PATTERN.search(result, search_from)
    if not m_start:
      break
    m_end = _DETAILS_END_PATTERN.search(result, m_start.end())
    if not m_end:
      break
    start, end = m_start.start(), m_end.end()
    region = result[start:end]
    region_low = region.lower()
    # Only repair a region that is one of our blessed cards.
    if any(lbl in region_low for lbl in _APPROVED_SUMMARY_LABELS):
      fixed = _unescape_html_entities(region)
      if fixed != region:
        changed = True
      result = result[:start] + fixed + result[end:]
      search_from = start + len(fixed)
    else:
      search_from = end
  return result, changed


# Matches a LEADING tag-like start token that the closed-tag pattern above misses,
# i.e. an UNCLOSED/truncated tag at the very start of the reply with no closing '>'
# (e.g. "<state_update" or "<state_update wifi_tips_given" cut off before '>').
# Only the "<" + tag-name token is matched (word/':'/'.'/'-' chars, NO spaces), so at
# most the stray fragment is removed and any following words are preserved. A genuine
# customer message never begins with "<" + a letter, so this can never eat real prose.
_LEADING_UNCLOSED_TAG_TOKEN = re.compile(r'^\s*<\s*/?[A-Za-z][\w:.\-]*')


def _strip_leading_unclosed_tag(text: str) -> str:
  """Removes a leading unclosed/truncated tag start (e.g. "<state_update") that the
  closed-tag stripper leaves behind when the model cuts the tag off before its '>'.
  Runs only AFTER closed <...> tags are removed, so it fires solely on a genuine
  dangling '<tagname' at the start of the reply."""
  if not text:
    return text
  stripped = _LEADING_UNCLOSED_TAG_TOKEN.sub("", text, count=1)
  return stripped.lstrip() if stripped != text else text


# Pattern to detect garbage text fragments (JSON artifacts, stray braces/quotes)
# Only matches short strings (1-6 chars) that are purely punctuation/whitespace.
# This avoids false positives on real content while catching "}", '"}', '"}"}', '"}}' etc.
_GARBAGE_TEXT_PATTERN = re.compile(r'^[\s"\'{}()\[\],:]{1,6}$')

# Pattern to strip a trailing JSON-artifact tail glued onto an otherwise-valid sentence
# (e.g. "...connectivity issues.}", '...help."}'). The LLM occasionally leaks the closing
# brace/quote of the tool-call JSON into the spoken text when it emits a message and a
# tool call in the same turn. Requires at least one brace/bracket in the tail so we never
# strip legitimate sentence-ending punctuation (. ? !) or a lone closing quote.
_TRAILING_ARTIFACT_PATTERN = re.compile(r'[\s"\'{}\[\],:]*[{}\[\]][\s"\'{}\[\],:]*$')

# Pattern to detect LLM hallucinated internal state/metadata (e.g., "security_check: true", "network_status: impaired")
# Only matches when:
#   - Key contains an underscore (state vars always do, natural language almost never does), OR
#   - Value is a known boolean/state token (true/false/on/off/null/none/pending/done/skipped)
# This avoids false positives on natural language like "Ok: thanks" or "Sure: ok"
_STATE_VAR_KEY_PATTERN = re.compile(r'^\s*\w*_\w+\s*:\s*\S+\s*$', re.IGNORECASE)
_STATE_VAR_VALUE_PATTERN = re.compile(
    r'^\s*\w+\s*:\s*(true|false|on|off|null|none|pending|done|skipped|error|success|clear|impaired|healthy)\s*$',
    re.IGNORECASE
)

# Pattern to strip INLINE leaked flow-control state fragments that the LLM occasionally glues
# onto (before/inside/after) an otherwise-valid message — with or without separating whitespace,
# e.g. "wifi_offer_pending:falsewifi_scoping_pending:truewifi_flow_active:trueIs this happening...".
# The anchored patterns above only catch a part whose ENTIRE text is one key:value, so they miss
# these concatenated leaks. We target KNOWN flow-control variable names explicitly (so we never
# touch natural language) and a fixed set of value tokens (matched as a prefix, since the value is
# often glued directly to the next word like "trueIs"). Applying this removes the fragments and
# leaves the real customer message intact.
_LEAKED_STATE_FRAGMENT_PATTERN = re.compile(
    r'(?:'
    r'wifi_troubleshooting_agent_enabled|wifi_troubleshooting_agent_active|wifi_blaster_plan_id|wifi_blaster_result|wifi_blaster_target_device|wifi_flow_active|wifi_offer_pending|wifi_scoping_pending|wifi_tips_given|wifi_pod_help_pending|wifi_status|'
    r'device_help_active|device_help_pending|device_help_steps_given|device_help_target|'
    r'app_issue_reported|app_issue_target|'
    r'speed_test_execution_enabled|speed_test_async_result_pending|speed_test_restart_offer_pending|'
    r'diagnostics_triggered|intent_clarified|intent_clarification_pending|'
    r'outage_inquiry_answered|outage_inquiry_pending|outage_troubleshoot_offer_pending|'
    r'awaiting_outage_consent|escalate_to_human_flag|direct_response_mode|'
    r'outage_status|network_status|gateway_status|account_status|convoy_status|technician_type'
    r')'
    r'\s*[:=]\s*'
    # Value is OPTIONAL: the model sometimes leaks a bare "wifi_flow_active:" whose value was
    # cut off (e.g. by a newline) — e.g. "...wifi_pod_help_pending: falsewifi_flow_active:\n".
    # A known state-var name followed by ':'/'=' is never natural language, so stripping the
    # name+separator even without a trailing value token is safe and removes the leftover.
    r'(?:\{[^}]*\}|true|false|on|off|null|none|pending|done|skipped|error|success|clear|impaired|'
    r'healthy|active|degradation|offline|reboot|swap|predictive_swap|predictive_offline|'
    r'no_telemetry|unsupported_device|resolved|handoff|suspended|disconnected|'
    r'devices_found|no_devices|restart_issued|restart_failed_offline|refresh_started|refresh_throttled|'
    r'network_tech|install_repair_tech|\d+)?',
    re.IGNORECASE,
)

# Known flow-control state-variable names, used to detect a LEADING state-dump the model
# occasionally prefixes to its reply (with or without a <state_update> wrapper), e.g.
# "device_help_active: truedevice_help_target: Vizio sound bardevice_help_steps_given: 3...".
_KNOWN_STATE_NAMES = (
    r'async_speed_test_result|async_speed_test_execution_id|speed_test_execution_enabled|speed_test_async_result_pending|xbo_id|wifi_troubleshooting_agent_enabled|wifi_troubleshooting_agent_active|wifi_blaster_plan_id|wifi_blaster_result|wifi_blaster_target_device|wifi_flow_active|wifi_offer_pending|wifi_scoping_pending|wifi_tips_given|wifi_pod_help_pending|wifi_status|'
    r'device_help_active|device_help_pending|device_help_steps_given|device_help_target|'
    r'app_issue_reported|app_issue_target|'
    r'speed_test_execution_enabled|speed_test_async_result_pending|speed_test_restart_offer_pending|'
    r'diagnostics_triggered|intent_clarified|intent_clarification_pending|'
    r'outage_inquiry_answered|outage_inquiry_pending|outage_troubleshoot_offer_pending|'
    r'awaiting_outage_consent|escalate_to_human_flag|direct_response_mode|'
    r'video_flow_active|video_issue_category|video_status|video_device_select_pending|video_disambiguation_pending|video_restart_offer_pending|video_refresh_offer_pending|video_tips_given|video_notified|'
    r'outage_status|network_status|gateway_status|account_status|convoy_status|technician_type'
)
_KNOWN_STATE_VALUE_TOKEN = (
    r'\{[^}]*\}|true|false|on|off|null|none|pending|done|skipped|error|success|clear|impaired|'
    r'healthy|active|degradation|offline|reboot|swap|predictive_swap|predictive_offline|'
    r'no_telemetry|unsupported_device|resolved|handoff|suspended|disconnected|'
    r'devices_found|no_devices|restart_issued|restart_failed_offline|refresh_started|refresh_throttled|'
    r'network_tech|install_repair_tech|\d+'
)
# A single leading fragment: EITHER a token-valued var, OR a *_target var whose free-text
# value runs up to (but not into) the NEXT known state-var name. The free-text branch only
# fires when another state var follows, so it can never swallow the real customer message.
# The token value is OPTIONAL so a bare leading "wifi_flow_active:" (value cut off) is peeled too.
_LEADING_STATE_FRAGMENT = re.compile(
    r'^\s*(?:'
    r'async_speed_test_execution_id\s*[:=]\s*[A-Za-z0-9_.-]+'
    r'|'
    r'wifi_blaster_plan_id\s*[:=]\s*[A-Za-z0-9_.-]+'
    r'|'
    rf'(?:{_KNOWN_STATE_NAMES})\s*[:=]\s*(?:{_KNOWN_STATE_VALUE_TOKEN})?'
    r'|'
    rf'(?:device_help_target|app_issue_target)\s*[:=]\s*.+?(?=(?:{_KNOWN_STATE_NAMES})\s*[:=])'
    r')',
    re.IGNORECASE | re.DOTALL,
)

# A short stray junk prefix (e.g. a single duplicated char) glued directly onto the start of a
# leaked state dump: the model streams a partial token then restarts, producing "wwifi_tips_given:
# 1...". The leading '\w{1,4}' only matches when it is IMMEDIATELY followed by a KNOWN state-var
# name + ':'/'=', so it can never consume the first word of a genuine customer message.
_LEADING_JUNK_BEFORE_STATE = re.compile(
    rf'^\s*\w{{1,4}}(?=(?:{_KNOWN_STATE_NAMES})\s*[:=])',
    re.IGNORECASE,
)


def _strip_leading_state_dump(text: str) -> str:
  """Removes a run of state-var fragments prefixed to the START of the reply. Real customer
  messages never begin with a state-var name, so repeatedly peeling leading fragments is safe
  and leaves the genuine message (e.g. 'Based on Vizio's support guidance: ...') intact."""
  if not text:
    return text
  remainder = text
  while True:
    m = _LEADING_STATE_FRAGMENT.match(remainder)
    if not m:
      break
    remainder = remainder[m.end():]
  return remainder if remainder != text else text


def _strip_leaked_state(text: str) -> str:
  """Removes inline leaked flow-control state fragments, then tidies leftover separators."""
  if not text:
    return text
  # First remove any LEADING state-dump run (e.g. the model prefixing its reply with
  # "device_help_active: truedevice_help_target: Vizio sound bardevice_help_steps_given: 3
  # device_help_pending: true"). This catches FREE-TEXT-valued vars like *_target that the
  # token-only pattern below cannot, since the target value ("Vizio sound bar") is not a
  # known token. Real customer messages never START with a state-var name, so this is safe.
  cleaned = _strip_leading_state_dump(text)
  # A stray junk prefix (e.g. the duplicated "w" in "wwifi_tips_given:") glued directly onto
  # the state dump defeats the leading-dump peeler above; remove it, then peel the dump again.
  dejunked = _LEADING_JUNK_BEFORE_STATE.sub("", cleaned)
  if dejunked != cleaned:
    cleaned = _strip_leading_state_dump(dejunked)
  # Then remove any remaining token-valued fragments glued elsewhere in the message.
  cleaned = _LEAKED_STATE_FRAGMENT_PATTERN.sub(" ", cleaned)
  # LOSSLESS GUARD: if no leaked state fragment was present, return the ORIGINAL text
  # UNCHANGED. Do NOT collapse whitespace/newlines on a clean message — doing so mutates
  # multi-line replies (e.g. a line-broken numbered list), falsely flags them as sanitized,
  # and makes the platform emit a reformatted SECOND, duplicate message.
  if cleaned == text:
    return text
  # A fragment was removed: tidy leftover separators WITHOUT destroying line structure.
  # Collapse only runs of HORIZONTAL whitespace (spaces/tabs) — never newlines — so a bullet
  # list or multi-line tip keeps its formatting after the leak prefix is peeled (previously we
  # collapsed all whitespace, which flattened "intro:\n\n* bullet\n* bullet" into one line).
  cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
  # Trim trailing spaces left on each line, and collapse 3+ blank lines to at most one.
  cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)
  cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
  cleaned = re.sub(r'^[\s"\'{}\[\],:;=|-]+', '', cleaned)
  return cleaned.strip()


# Pattern that flags a *sentence* as leaked INTERNAL REASONING / meta-commentary — the model
# narrating its own tool calls or its turn-taking policy instead of speaking to the customer,
# e.g. "I have already called the transfer tool and received the response." / "I should not take
# any further actions or send any more messages." The existing sanitizers only catch name:value
# state, XML, and JSON artifacts, so this natural-language self-narration slips through to the
# customer. Each alternative is a phrase a genuine customer-facing repair message never contains,
# so matching it lets us drop just the offending sentence(s) while keeping the real reply.
_INTERNAL_REASONING_PATTERN = re.compile(
    # ── self-narration about (having) called / invoked / used a tool ──
    r'\b(?:i|we)\b[^.?!]*\b(?:call(?:ed|ing)?|invok(?:e|ed|ing)|trigger(?:ed|ing)?|us(?:e|ed|ing)?)\b[^.?!]*\btools?\b'
    r'|\btools?\b[^.?!]*\b(?:call(?:ed)?|respon(?:se|ded)|result|return(?:ed)?)\b'
    r'|\b(?:call(?:ed|ing)?|invok(?:e|ed|ing)|trigger(?:ed|ing)?)\b[^.?!]*\btools?\b'
    # ── self-narration about receiving a (tool) response/result ──
    r'|\b(?:received|got|have\s+received)\b[^.?!]*\b(?:the\s+)?(?:tool\s+)?(?:response|result|reply)\b'
    # ── turn-taking / action policy self-talk ──
    r'|\b(?:i|we)\s+should\s+not\b'
    r'|\b(?:i|we)\s+(?:must|shall|will|wo\s*n\'?t|should)\s+(?:not\s+)?(?:take|send|make|do|reply|respond|continue|proceed)\b'
    r'|\b(?:no|any)\s+(?:more|further)\s+(?:actions?|messages?|steps?|repl(?:y|ies)|responses?)\b'
    r'|\bfurther\s+(?:actions?|steps?)\b',
    re.IGNORECASE,
)

# Splits text into sentences after ., !, or ? (consuming any following whitespace, including
# newlines). Zero-width friendly so a leak glued directly after punctuation still splits.
_SENTENCE_SPLIT_PATTERN = re.compile(r'(?<=[.!?])\s*')


def _strip_internal_reasoning(text: str) -> str:
  """Drops sentences that are internal reasoning / meta-commentary about the agent's own
  tool calls or turn-taking, keeping the genuine customer-facing sentences intact."""
  if not text:
    return text
  sentences = [s for s in _SENTENCE_SPLIT_PATTERN.split(text) if s and s.strip()]
  # If splitting produced nothing meaningful, evaluate the whole string as one unit.
  if not sentences:
    sentences = [text]
  kept = [s for s in sentences if not _INTERNAL_REASONING_PATTERN.search(s)]
  # LOSSLESS GUARD: if NO sentence was actual internal-reasoning, return the
  # ORIGINAL text UNCHANGED. The sentence split/rejoin is lossy around a period
  # immediately followed by a closing quote (e.g. 'Forget This Network."' becomes
  # 'Forget This Network. "'): rewriting a perfectly clean message falsely flags it
  # as sanitized, so after_agent_callback returns a new Content and the platform
  # emits it as a SECOND, duplicate message. Only rebuild when we truly drop a leak.
  if len(kept) == len(sentences):
    return text
  cleaned = ' '.join(part.strip() for part in kept if part.strip())
  cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
  return cleaned


# Ordered-list markers to normalize away when comparing two messages for duplication:
# line-start bullets/numbers ("1. ", "2) ", "- ", "* ", "• ") and inline "1. "/"2) " runs.
_LIST_MARKER_LINE_START = re.compile(r'(?m)^\s*(?:\d{1,2}[.)]|[-*\u2022])\s+')
_LIST_MARKER_INLINE = re.compile(r'(?<!\d)\b\d{1,2}[.)]\s+')
_NON_WORD = re.compile(r'[^\w\s]')
_MULTI_WS = re.compile(r'\s+')


def _norm_for_dedupe(text: str) -> str:
  """Aggressively normalizes a message so DIFFERENT FORMATTINGS of the SAME answer
  compare equal for de-duplication. Collapses list numbering ('1. Go...' vs a plain
  line-broken 'Go...'), punctuation/symbols ('>', quotes, colons), whitespace/newlines,
  and case. Used ONLY to detect duplicate customer messages within one turn."""
  if not text:
    return ""
  t = text.lower()
  t = _LIST_MARKER_LINE_START.sub(' ', t)   # "1. " / "- " at the start of a line
  t = _LIST_MARKER_INLINE.sub(' ', t)       # inline "1. " / "2) "
  t = _NON_WORD.sub(' ', t)                  # drop punctuation/symbols uniformly
  t = _MULTI_WS.sub(' ', t).strip()
  return t


# Appointment->Repair invoke payload markers seen in steering hand-back turns.
# When these appear in the latest user text and the model mirrors that text back,
# we replace it with a deterministic customer-facing line so the agent never
# behaves like a customer.
_A2A_REINVOKE_HINTS = (
    "internet troubleshooting required",
    "troubleshooting is required before scheduling an appointment",
    "user wants to schedule a technician",
    "predictive analysis identified a network issue",
    "key_events:",
)


def _get_last_user_text(callback_context) -> str:
  """Returns the concatenated text of the customer's most recent message."""
  try:
    parts = callback_context.get_last_user_input() or []
    texts = []
    for p in parts:
      t = None
      try:
        t = p.text_or_transcript()
      except Exception:
        t = getattr(p, "text", None)
      if t:
        texts.append(str(t))
    return " ".join(texts).strip()
  except Exception:
    return ""


def _looks_like_a2a_reinvoke_payload(text: str) -> bool:
  """True when input resembles an appointment->repair invoke payload."""
  t = (text or "").lower().strip()
  if not t:
    return False
  return any(h in t for h in _A2A_REINVOKE_HINTS)


def _looks_like_customer_echo(agent_text: str, user_text: str) -> bool:
  """True when the agent output is effectively the same as user input.

  Includes the common doubled echo shape:
  "X ...?X ...?" (same sentence concatenated twice).
  """
  a = _norm_for_dedupe(agent_text or "")
  u = _norm_for_dedupe(user_text or "")
  if not a or not u:
    return False
  if a == u:
    return True
  # Guard against over-triggering on very short utterances like "yes".
  if len(u) < 16:
    return False
  return a == f"{u} {u}" or a == f"{u}{u}"


def _collapse_doubled_message(text: str) -> str:
  """Collapses a SINGLE text part that contains the same message twice back-to-back.

  The model occasionally streams its whole reply, then repeats it verbatim in the SAME part,
  e.g. "Since you have pods... Did that help? Since you have pods... Did that help?". The
  cross-part deduper only compares SEPARATE parts, so this intra-part doubling slips through.
  We look for a split near the midpoint where the two halves are equal under dedupe
  normalization (which ignores whitespace/punctuation/case), and return the ORIGINAL first
  half unchanged (preserving its formatting). Only fires on a genuine near-exact doubling, so
  a normal, non-repeating customer message is returned untouched."""
  if not text:
    return text
  core = text.strip()
  length = len(core)
  if length < 8:
    return text
  mid = length // 2
  # Probe split points around the midpoint. Because normalization ignores punctuation and
  # whitespace, several adjacent split points can compare equal (e.g. the boundary landing just
  # before vs. just after a trailing '?'). Collect every matching split, then prefer the one whose
  # first half ends on a clean sentence boundary (., !, ?, closing quote/paren), tie-breaking on
  # the LONGEST such half so we keep the full first copy (never dropping its final punctuation).
  best_i = None
  best_clean = False
  for i in range(mid - 2, mid + 3):
    if i <= 0 or i >= length:
      continue
    left = core[:i]
    left_norm = _norm_for_dedupe(left)
    if not left_norm or left_norm != _norm_for_dedupe(core[i:]):
      continue
    left_rstripped = left.rstrip()
    ends_clean = bool(left_rstripped) and left_rstripped[-1] in '.!?"\')'
    if best_i is None or (ends_clean and not best_clean) or (ends_clean == best_clean and i > best_i):
      best_i = i
      best_clean = ends_clean
  if best_i is not None:
    return core[:best_i].strip()
  return text


def _dedupe_text_parts(final_response):
  """Returns (deduped_texts, dropped_any). Keeps the FIRST occurrence of each distinct
  (normalized) message and drops later duplicates/near-duplicates. Generic guard: this
  agent is one-thing-per-turn, so a single turn must produce ONE customer message; the
  model / grounding tool occasionally emits the same answer twice in different formats
  (numbered vs. line-broken), which the platform would otherwise render as two bubbles."""
  texts = []
  seen = set()
  dropped = False
  for item in final_response:
    txt = _get_text(item)
    if txt is None or not isinstance(txt, str) or not txt.strip():
      continue
    norm = _norm_for_dedupe(txt)
    if norm and norm in seen:
      dropped = True
      print(f"[after_agent_callback] Dropped duplicate message part: '{txt.strip()}'")
      continue
    seen.add(norm)
    texts.append(txt.strip())
  return texts, dropped



def _get_text(item):
  """Safely extracts text from a Part object or dict."""
  if isinstance(item, dict):
    return item.get("text")
  return getattr(item, "text", None)


def _set_text(item, value):
  """Safely sets text on a Part object or dict."""
  if isinstance(item, dict):
    item["text"] = value
  elif hasattr(item, "text"):
    item.text = value


def _get_contextual_replacement(callback_context) -> str:
  """Returns the correct customer-facing message based on diagnostic state.
  Used when the LLM hallucinates non-customer text after a transfer was already initiated."""
  network_status = callback_context.state.get("network_status", "")
  tech_type = callback_context.state.get("technician_type", "")

  if network_status == "impaired":
    if tech_type == "network_tech":
      return "It looks like there's a problem with the network signal going to your home. Let me connect you with someone to get this sorted out for you."
    return "We found an issue with the connection to your home. A technician will take a closer look, and depending on the type of issue found, a service charge may apply."

  gateway_status = callback_context.state.get("gateway_status", "")
  if gateway_status == "swap":
    return "Your gateway has a hardware fault and needs replacement. You can get a new one shipped or swap at a store here: https://customer.xfinity.com/devices/equipment-update/new"

  outage_status = callback_context.state.get("outage_status", "")
  if outage_status == "active":
    return "There's a service outage in your area and crews are working on it. You can enroll in outage SMS updates here: https://www.xfinity.com/support/enroll-sms"

  return "I just ran a few checks but wasn't able to get all the info I need. Let me get you to someone who can help."


def _response_has_function_call(final_response, name: str) -> bool:
  """True if the agent's final output contains a function call with this name
  (e.g. 'end_session'). Used to pick the right message when the turn surfaced no
  visible text."""
  try:
    for item in final_response or []:
      fc = getattr(item, "function_call", None)
      if fc and getattr(fc, "name", None) == name:
        return True
  except Exception:  # pylint: disable=broad-exception-caught
    pass
  return False


def after_agent_callback(callback_context: CallbackContext) -> Optional[Content]:
  """
  Executes after the agent finishes, but before the result is returned to the user.

  Sanitizes the agent's output to remove any hallucinated XML/HTML tags
  or JSON artifact fragments that occasionally leak into responses.
  """
  final_response = callback_context.get_last_agent_output()
  print(f"[after_agent_callback] Raw response: {final_response}")

  # Storing the response in state for tracking
  try:
    callback_context.state["repair_orchestration_agent_final_response"] = final_response
  except Exception as e:
    traceback.print_exc()
    print(f"[after_agent_callback] Error saving final response in state: {e}")

  # Sanitize: strip XML/HTML tags, garbage text, and state-variable hallucinations from output
  try:
    if final_response and isinstance(final_response, list):
      sanitized = False
      for item in final_response:
        original_text = _get_text(item)
        if original_text is None or not isinstance(original_text, str):
          continue
        # SAFETY NET (mirrors after_model_callback): if a RAW [[TS]]...[[/TS]] plain-text
        # summary block reached the final surfaced text un-rendered, render it into the
        # approved literal-HTML card here so raw [[TS]] markers never leak to the customer.
        # Normally after_model already rendered it, so this is a no-op (no "[[" present).
        ts_expanded_text, ts_expanded = _expand_summary_block(original_text)
        if ts_expanded:
          print(f"[after_agent_callback] Rendered plain-text [[TS]] summary block into approved card. Before: '{original_text}' After: '{ts_expanded_text}'")
          original_text = ts_expanded_text
          # Persist immediately + flag sanitized so the rendered card is emitted even if no
          # later sanitizer changes the text (otherwise the raw [[TS]] item would be returned).
          _set_text(item, ts_expanded_text)
          sanitized = True
        # Repair an APPROVED card the model emitted with entity-encoded tags (&lt;/&gt;) so it
        # becomes valid literal HTML that renders (and that the protector below can recognize).
        repaired_text, entities_fixed = _repair_entity_encoded_details(original_text)
        if entities_fixed:
          print(f"[after_agent_callback] Repaired entity-encoded approved <details> card. Before: '{original_text}' After: '{repaired_text}'")
        # Protect the APPROVED <details> cards (troubleshooting summary / speed test results)
        # behind opaque placeholders so the tag sanitizers below cannot shred them. Any
        # non-approved <details> is left in place and stripped as usual. Restored at the end.
        working_text, protected_details = _protect_approved_details(repaired_text)
        # Strip XML/HTML tags (operate on working_text so protected approved <details>
        # cards survive; they are restored after sanitization).
        cleaned_text = _XML_TAG_PATTERN.sub("", working_text).strip()
        # Strip a leading UNCLOSED/truncated tag start (e.g. "<state_update" with no
        # closing '>') that the closed-tag pattern above cannot match.
        deleadtag = _strip_leading_unclosed_tag(cleaned_text)
        if deleadtag != cleaned_text:
          print(f"[after_agent_callback] Stripped leading unclosed tag. Before: '{cleaned_text}' After: '{deleadtag}'")
          cleaned_text = deleadtag
        # Strip inline leaked flow-control state fragments (e.g. "wifi_scoping_pending:true...")
        # glued onto an otherwise-valid message. Done early so downstream checks see clean text.
        destateleaked = _strip_leaked_state(cleaned_text)
        if destateleaked != cleaned_text:
          print(f"[after_agent_callback] Stripped leaked state fragment(s). Before: '{cleaned_text}' After: '{destateleaked}'")
          cleaned_text = destateleaked
        # Strip natural-language internal reasoning / meta-commentary sentences (e.g. "I have
        # already called the transfer tool and received the response. I should not take any
        # further actions or send any more messages.") while keeping the real customer reply.
        dereasoned = _strip_internal_reasoning(cleaned_text)
        if dereasoned != cleaned_text:
          print(f"[after_agent_callback] Stripped internal reasoning/meta-commentary. Before: '{cleaned_text}' After: '{dereasoned}'")
          cleaned_text = dereasoned
        # Strip trailing JSON-artifact tails (e.g. a stray "}" leaked from tool-call JSON)
        artifact_stripped = _TRAILING_ARTIFACT_PATTERN.sub("", cleaned_text).strip()
        if artifact_stripped != cleaned_text:
          print(f"[after_agent_callback] Stripped trailing JSON artifact. Before: '{cleaned_text}' After: '{artifact_stripped}'")
          cleaned_text = artifact_stripped
        # Collapse an intra-part doubled message (the same reply repeated back-to-back in ONE
        # part). The cross-part deduper below only compares separate parts, so this catches the
        # "message ... message" duplication the model glues into a single string.
        decollapsed = _collapse_doubled_message(cleaned_text)
        if decollapsed != cleaned_text:
          print(f"[after_agent_callback] Collapsed doubled message within a single part. Before: '{cleaned_text}' After: '{decollapsed}'")
          cleaned_text = decollapsed
        # Restore the protected approved <details> card(s) verbatim now that all tag/text
        # sanitizers have run. After this, cleaned_text == original_text for a clean approved
        # card, so the LOSSLESS comparison below leaves it untouched (no duplicate emission).
        if protected_details:
          cleaned_text = _restore_approved_details(cleaned_text, protected_details)
        # Check if remaining text is just garbage (JSON artifacts like "}", '"}"}', etc.)
        if cleaned_text and _GARBAGE_TEXT_PATTERN.match(cleaned_text):
          _set_text(item, "")
          sanitized = True
          print(f"[after_agent_callback] Removed garbage text fragment: '{original_text}'")
        # Check if text is an LLM-hallucinated state variable (e.g., "security_check: true")
        elif cleaned_text and (_STATE_VAR_KEY_PATTERN.match(cleaned_text) or _STATE_VAR_VALUE_PATTERN.match(cleaned_text)):
          replacement = _get_contextual_replacement(callback_context)
          _set_text(item, replacement)
          sanitized = True
          print(f"[after_agent_callback] Replaced hallucinated state var '{original_text}' with contextual message: '{replacement}'")
        elif cleaned_text != original_text.strip():
          _set_text(item, cleaned_text)
          sanitized = True
          print(f"[after_agent_callback] Stripped XML tags from response. Cleaned: '{cleaned_text}'")
        elif cleaned_text != original_text:
          # WHITESPACE-ONLY difference (the sanitizers .strip() the text, so a model reply
          # that merely starts/ends with a space would otherwise be flagged as "sanitized").
          # Returning Content here APPENDS a second bubble instead of replacing the already-
          # surfaced model text, so the customer sees the identical answer twice. Normalize
          # the part in place, but do NOT set sanitized.
          _set_text(item, cleaned_text)

      # Build the final visible text parts, ALWAYS dropping duplicate/near-duplicate
      # messages (runs even when nothing else was sanitized). Normalization ignores list
      # numbering, punctuation, whitespace and case so different formattings of the same
      # answer (e.g. a numbered '1. 2. 3.' variant and a line-broken variant) collapse to
      # one bubble. function_call/function_response parts have no text and are ignored.
      deduped_texts, duplicate_dropped = _dedupe_text_parts(final_response)
      visible_texts = [t for t in deduped_texts if t and t.strip()]

      # Echo guard: on agent-to-agent invoke payload turns (e.g. Appointment asks
      # Repair to run Internet troubleshooting), the LLM can mirror the payload/
      # customer phrase verbatim and "speak as the customer". Replace that with a
      # deterministic assistant line so we never surface mirrored customer text.
      if visible_texts:
        last_user_text = _get_last_user_text(callback_context)
        if _looks_like_a2a_reinvoke_payload(last_user_text):
          for t in visible_texts:
            if _looks_like_customer_echo(t, last_user_text):
              replacement = (
                  "Got it — I can help with your internet connection. Let me run"
                  " a quick check now."
              )
              print(
                  "[after_agent_callback] Replaced mirrored customer-text echo on"
                  " A2A reinvoke payload turn."
                  f" user={last_user_text!r} agent={t!r}"
              )
              return Content(parts=[Part(text=replacement)])

      # SAFETY NET (runs REGARDLESS of sanitize/dedupe): an agent turn that
      # surfaces NO visible text to the customer is always a bug — the customer
      # sees an empty bubble and the platform/steering then replaces it with a
      # generic "Hmm, looks like something went wrong there" fallback. The common
      # cause is the model ending a turn on a tool call ONLY — most often
      # end_session with its closing line stuffed into the 'reason' arg and
      # nothing spoken (also a bare transfer where the sub-agent stayed silent).
      # Inject a customer-facing message so the customer always sees text.
      if not visible_texts:
        if _response_has_function_call(final_response, "end_session"):
          replacement = "I'm glad I could help today. Take care, and reach out anytime you need us!"
        else:
          replacement = _get_contextual_replacement(callback_context)
        print(
            "[after_agent_callback] Agent output had no visible text; injected"
            f" message: '{replacement}'"
        )
        return Content(parts=[Part(text=replacement)])

      if sanitized or duplicate_dropped:
        text_parts = [Part(text=t) for t in visible_texts]

        print(f"[after_agent_callback] Returning Content with {len(text_parts)} text part(s) (sanitized={sanitized}, duplicate_dropped={duplicate_dropped}).")
        return Content(parts=text_parts)
  except Exception as e:
    traceback.print_exc()
    print(f"[after_agent_callback] Error during sanitization: {e}")

  return None
