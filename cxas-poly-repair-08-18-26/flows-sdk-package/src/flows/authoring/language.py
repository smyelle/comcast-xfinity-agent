"""Language authoring: locale display names + the `<language_detection>` block.

`flows` lets an author declare supported languages on `flows.App` and enable an
in-conversation language switch. CES supports this via the app-level
`languageSettings` (which locales ASR/TTS accept) PLUS, for a mid-call switch, an
`update_language` tool and a `<language_detection>` instruction block. The live
model does NOT reliably auto-detect a switch, so the switch is gated explicitly and
kept sticky by conservative guardrails (CES design guide; b/484305525, b/506098142).

This module owns the two build-time pieces: `display_name`/`display_names` map a
BCP-47 code to a human language name (what the `update_language` tool and the model
speak in), and `language_detection_block` renders the instruction block customized
to the configured languages. The `update_language` tool body itself is generated in
`flows.authoring.setters.gen_update_language`.
"""

from __future__ import annotations

import inspect
import textwrap
from typing import Any, Optional

# BCP-47 (or bare language subtag) -> human display name. Falls back to the code
# itself for anything not listed, so an author can use any locale CES supports.
LOCALE_NAMES: dict[str, str] = {
    "en": "English", "en-US": "English", "en-GB": "English", "en-CA": "English",
    "es": "Spanish", "es-US": "Spanish", "es-ES": "Spanish", "es-MX": "Spanish",
    "fr": "French", "fr-FR": "French", "fr-CA": "French",
    "de": "German", "de-DE": "German",
    "it": "Italian", "it-IT": "Italian",
    "pt": "Portuguese", "pt-BR": "Portuguese", "pt-PT": "Portuguese",
    "nl": "Dutch", "ja": "Japanese", "ja-JP": "Japanese",
    "ko": "Korean", "ko-KR": "Korean",
    "zh": "Chinese", "zh-CN": "Chinese", "cmn-CN": "Chinese",
    "hi": "Hindi", "hi-IN": "Hindi",
}


def display_name(code: str) -> str:
  """Human language name for a locale code (falls back to the code itself)."""
  return LOCALE_NAMES.get(code) or LOCALE_NAMES.get(code.split("-")[0]) or code


def display_names(codes: list[str]) -> list[str]:
  """De-duplicated display names for `codes`, preserving order (English, Spanish…)."""
  out: list[str] = []
  for c in codes:
    n = display_name(c)
    if n not in out:
      out.append(n)
  return out


def language_detection_block(
    languages: list[str], default: Optional[str] = None, mode: str = "explicit"
) -> str:
  """The `<language_detection>` instruction block for the configured languages.

  `mode="explicit"` (production default) switches ONLY on an explicit caller request
  ("Spanish please" / "en español"); `mode="auto"` also switches on a complete,
  grammatically unambiguous sentence in another language (opt-in, less reliable on
  the live model). Appended to the END of an agent's instruction at emit.
  """
  names = display_names(languages)
  default_name = display_name(default) if default else (names[0] if names else "English")
  lang_list = ", ".join(names)
  if mode == "auto":
    threshold = (
        "You may switch if the caller EXPLICITLY requests another supported "
        "language OR speaks a complete, grammatically unambiguous sentence in it."
    )
  else:
    threshold = (
        "You may ONLY switch when the caller EXPLICITLY requests another supported "
        'language (e.g. "Spanish please", "en français", "can you speak French?"). '
        "Do NOT switch just because a word or phrase is in another language."
    )
  return (
      "<language_detection>\n"
      f"  <goal>The supported languages are: {lang_list}. Determine the language of "
      "each caller utterance and switch the active language only when the caller "
      "wants it, then STAY in that language.</goal>\n"
      f"  <current_language>The active language starts as {default_name} and is "
      "tracked in {active_language}. Keep responding in the active language until a "
      "switch is confirmed.</current_language>\n"
      "  <evaluation_rules>\n"
      "    - Re-evaluate the language on EVERY new caller utterance.\n"
      "    - Contextual inertia: heavily weight the ongoing conversation language; "
      "callers rarely switch for a single word.\n"
      f"    - Switching threshold: {threshold}\n"
      "    - Length guardrail: do NOT switch for utterances shorter than 3 words "
      "unless it is an explicit request.\n"
      "    - Cognate guardrail: words spelled/sounding alike in both languages "
      '(e.g. "no", "nein" vs "nine") MUST NEVER trigger a switch on their own.\n'
      "    - Isolated words: politeness markers from another language (e.g. "
      '"gracias", "merci" in an English sentence) must NOT trigger a switch.\n'
      "    - Noisy audio: if the input is unclear or noisy, keep the active "
      "language.\n"
      "  </evaluation_rules>\n"
      "  <execution_steps>\n"
      "    <step>\n"
      "      <trigger>The caller's language meets the switching threshold above and "
      "differs from the active language.</trigger>\n"
      "      <action>Call update_language with the target language BEFORE you speak, "
      "then give your entire response in the new language.</action>\n"
      "    </step>\n"
      "    <step>\n"
      "      <trigger>The threshold is not met.</trigger>\n"
      "      <action>Do NOT call update_language. Continue in the active "
      "language.</action>\n"
      "    </step>\n"
      "  </execution_steps>\n"
      "</language_detection>\n"
  )


# ---------------------------------------------------------------------------
# "select" mode: a turn-1 language menu that HARD-LOCKS the chosen language.
#   - language_select(): the first user slot (bilingual menu + DTMF).
#   - language_lock_block(): the standing instruction lock.
#   - the nudge hooks (before_model sync + escalation, after_model drift check).
# ---------------------------------------------------------------------------


def language_select(
    prompt: str,
    *,
    second_code: str = "es-US",
    name: str = "language_choice",
    reprompt: Optional[str] = None,
) -> dict[str, Any]:
  """The first USER slot for the turn-1 language menu (DTMF fills the first user slot,
  never the gate). `prompt` is the bilingual greeting/menu; pressing 9 / "marque nueve"
  deterministically selects `second_code`. A generic setter records the spoken choice;
  `active_language` is derived from the filled value by the before_model lock hook (so
  the DTMF path, which bypasses the setter, is covered too)."""
  first = reprompt or (
      "Sorry, I didn't catch that. " + prompt
  )
  return {
      "name": name,
      "source": "user",
      "setter": f"set_{name}",
      "hint": "preferred language",
      "ask": prompt,
      "dtmf_map": {"9": second_code},
      "validation": {
          "max_retries": 2,
          "reprompts": [first, prompt],
      },
  }


def language_menu_block(
    languages: list[str], default: Optional[str] = None, second: Optional[str] = None
) -> str:
  """Turn-1 menu guidance for `select` mode: record a choice only when the caller
  clearly picks one, and RE-OFFER the options once if the opener is ambiguous (so a
  caller who jumps into their request isn't silently defaulted). Instruction-only."""
  names = display_names(languages)
  default_name = display_name(default) if default else (names[0] if names else "English")
  second_name = display_name(second) if second else (names[1] if len(names) > 1 else names[0])
  return (
      "<language_menu>\n"
      f"  Open the call by offering the language options ({', '.join(names)}).\n"
      "  Record the choice with set_language_choice ONLY when the caller clearly picks "
      f"one: naming a language, saying \"{second_name}\" or \"{default_name}\", or "
      f"pressing/saying 9 for {second_name}.\n"
      "  If the caller's FIRST message does not clearly choose a language (they jump "
      "straight into their request, or say something ambiguous), briefly RE-OFFER the "
      f"language options once. If they still do not choose, continue in {default_name}.\n"
      "</language_menu>\n"
  )


def language_lock_block(languages: list[str], default: Optional[str] = None) -> str:
  """The `<language_lock>` instruction block for `select` mode: once a language is
  chosen it is fixed for the rest of the call (no switching, even if the caller uses
  another language). Token-light and guardrail-safe (pure instruction)."""
  names = display_names(languages)
  return (
      "<language_lock>\n"
      f"  The caller selects a language at the very start of the call (one of: "
      f"{', '.join(names)}).\n"
      "  Once selected, you MUST respond ONLY in that selected language for the ENTIRE "
      "remainder of the conversation, regardless of the language the caller uses. Do NOT "
      "switch languages for any reason after the initial choice.\n"
      "  If the caller ASKS to change or switch languages at any point, do NOT switch, "
      "and do NOT end, cancel, or transfer the conversation. Briefly acknowledge IN THE "
      "SELECTED LANGUAGE that you will continue in it, then keep helping with their "
      "request. A language-change request is never a reason to end the call.\n"
      "  The system reminds you of the exact selected language each turn; always follow "
      "that reminder.\n"
      "</language_lock>\n"
  )


# --- the language-detection heuristic (real + testable; also emitted into the hook) ---

_LANG_MARKERS: dict[str, dict[str, Any]] = {
    "es": {
        "words": {
            "el", "la", "los", "las", "un", "una", "de", "que", "y", "es", "por",
            "para", "con", "su", "usted", "gracias", "hola", "sí", "número", "numero",
            "pedido", "ayudar", "puedo", "está", "estoy", "claro", "perfecto", "cuál",
        },
        "chars": set("ñ¿¡áéíóúü"),
    },
    "fr": {
        "words": {
            "le", "la", "les", "un", "une", "de", "des", "que", "et", "est", "pour",
            "avec", "votre", "vous", "merci", "bonjour", "oui", "numéro", "numero",
            "commande", "aider", "je", "puis", "bien", "quel", "quelle", "s'il",
        },
        "chars": set("àâçéèêëîïôûùü"),
    },
    "en": {
        "words": {
            "the", "a", "an", "and", "is", "are", "your", "you", "order", "please",
            "hello", "hi", "thanks", "thank", "to", "for", "can", "help", "number",
            "okay", "sure", "what", "yes", "no", "i'll", "i", "we", "will",
        },
        "chars": set(),
    },
}


def _lang_family(lang: str) -> str:
  """Normalize a locale code OR a language display name to a base language subtag,
  for ANY language (not just es/fr/en): "es-US"/"es"/"Spanish" -> "es",
  "de-DE"/"German" -> "de", "fr-CA"/"French" -> "fr". "" when empty. A bare
  unrecognized word returns its own first token, which simply won't match the
  configured family map (falls back to the default) rather than mis-resolving."""
  low = (lang or "").strip().lower()
  if not low:
    return ""
  for code, name in LOCALE_NAMES.items():
    if low == name.lower():
      return code.split("-")[0].lower()
  return low.split("-")[0]


def _response_language_ok(text: str, lang: str) -> bool:
  """Cheap in-sandbox check: is `text` plausibly in `lang`? Fail-OPEN (returns True)
  whenever unsure, so a false positive never triggers a needless nudge. Only returns
  False on a CLEAR miss: an obviously-English reply when the locked language is es/fr."""
  fam = _lang_family(lang)
  if fam == "en" or fam not in _LANG_MARKERS:
    return True  # base language, or a language we have no heuristic for: don't nudge
  low = (text or "").lower()
  words = [w.strip(".,!?;:¿¡\"'()-") for w in low.split()]
  words = [w for w in words if w]
  if len(words) < 3:
    return True  # too short to judge
  tgt = _LANG_MARKERS[fam]
  if any(c in tgt["chars"] for c in low):
    return True  # target-language diacritics present
  tgt_hits = sum(1 for w in words if w in tgt["words"])
  en_hits = sum(1 for w in words if w in _LANG_MARKERS["en"]["words"])
  if en_hits >= 2 and tgt_hits == 0:
    return False  # clearly English while locked to es/fr
  return True


# --- emitted callback sources (self-contained; CES injects the callback globals) ---

_HOOK_HEADER = (
    "# pylint: disable=invalid-name,undefined-variable,unused-argument,"
    "broad-exception-caught,line-too-long\n"
    "from typing import Any, Optional\n"
)


def _family_display_map(languages: list[str], default: Optional[str]) -> dict[str, str]:
  """family key -> display name for the configured languages (+ a default)."""
  out: dict[str, str] = {}
  for code in languages:
    fam = _lang_family(code)
    if fam and fam not in out:
      out[fam] = display_name(code)
  default_name = display_name(default) if default else (
      display_names(languages)[0] if languages else "English")
  out.setdefault("", default_name)
  return out


def gen_language_before_model(languages: list[str], default: Optional[str] = None,
                             choice_slot: str = "language_choice") -> str:
  """before_model hook (emitted in `select` mode). Enforces the lock by MUTATING the
  system instruction each turn (the mechanism the framework itself uses — reliable,
  unlike a variable write): once the caller has chosen, append a concrete
  "respond ONLY in <language>" directive naming the chosen language literally (read from
  the choice slot in `sm`, which covers BOTH the DTMF path — which bypasses the setter —
  and the spoken path). Also mirrors the choice into `active_language` (best effort) and
  escalates the directive while a drift nudge is armed."""
  fam_display = _family_display_map(languages, default)
  default_name = fam_display.get("", "English")
  helpers = textwrap.dedent(inspect.getsource(_lang_family))
  return (
      _HOOK_HEADER
      + '"""Language lock (select mode): inject a concrete language directive + escalate."""\n\n'
      + f"_FAMILY_DISPLAY = {fam_display!r}\n"
      + f"_DEFAULT_DISPLAY = {default_name!r}\n"
      + f"_CHOICE_SLOT = {choice_slot!r}\n"
      + f"LOCALE_NAMES = {LOCALE_NAMES!r}\n\n\n"  # _lang_family (inlined below) reads this
      + helpers + "\n\n"
      "def _append_si(llm_request, note):\n"
      "  try:\n"
      "    si = llm_request.config.system_instruction\n"
      "    if hasattr(si, \"parts\") and si.parts:\n"
      "      si.parts[0].text = (si.parts[0].text or \"\") + note\n"
      "    elif isinstance(si, str):\n"
      "      llm_request.config.system_instruction = si + note\n"
      "  except Exception:\n"
      "    pass\n\n\n"
      "def before_model_callback(callback_context, llm_request):\n"
      "  sm = callback_context.state.get(\"sm\", {})\n"
      "  # Read the chosen language from pending+filled+deferred (staged the same turn).\n"
      "  merged = {}\n"
      "  for _k in (\"deferred\", \"pending\", \"filled\"):\n"
      "    _v = sm.get(_k)\n"
      "    if isinstance(_v, dict):\n"
      "      merged.update(_v)\n"
      "  choice = merged.get(_CHOICE_SLOT)\n"
      "  if not choice:\n"
      "    return None  # no choice yet: the menu turn runs untouched\n"
      "  lang = _FAMILY_DISPLAY.get(_lang_family(str(choice)), _DEFAULT_DISPLAY)\n"
      "  # Mirror into the variable too (best effort; the SI injection is the real lock).\n"
      "  try:\n"
      "    callback_context.state[\"active_language\"] = lang\n"
      "  except Exception:\n"
      "    pass\n"
      "  # The concrete, literal-language lock directive (not a {var}) appended each turn.\n"
      "  _append_si(llm_request, (\n"
      "      \"\\n\\n<language_lock>\\nThe caller selected \" + lang + \". You MUST respond \"\n"
      "      \"ONLY in \" + lang + \" for the entire rest of the conversation, regardless of \"\n"
      "      \"the language the caller uses. Do NOT switch languages. If the caller asks to \"\n"
      "      \"switch languages, decline briefly in \" + lang + \" and keep helping; NEVER \"\n"
      "      \"end, cancel, or transfer the call because of a language request.\\n\"\n"
      "      \"</language_lock>\"))\n"
      "  if sm.get(\"_lang_nudge\", 0) > 0:\n"
      "    _append_si(llm_request, (\n"
      "        \"\\n\\n<language_correction>\\nYour previous reply was NOT in \" + lang\n"
      "        + \". Rewrite your ENTIRE response in \" + lang + \" now.\\n</language_correction>\"))\n"
      "  return None\n"
  )


def gen_language_after_model() -> str:
  """after_model hook (emitted in `select` mode): if the model's reply drifts out of the
  locked language, arm a nudge and force a regeneration via `try_again` (capped, fail
  open). Token cost is ~0 on a compliant turn."""
  markers = "_LANG_MARKERS = " + repr(_LANG_MARKERS) + "\n"
  locale = "LOCALE_NAMES = " + repr(LOCALE_NAMES) + "\n\n\n"  # _lang_family reads this
  fam = textwrap.dedent(inspect.getsource(_lang_family))
  ok = textwrap.dedent(inspect.getsource(_response_language_ok))
  return (
      _HOOK_HEADER
      + '"""Language lock (select mode): nudge the model back into active_language."""\n\n'
      + markers
      + locale
      + fam + "\n\n"
      + ok + "\n\n"
      "def after_model_callback(callback_context, llm_response):\n"
      "  sm = callback_context.state.get(\"sm\", {})\n"
      "  lang = callback_context.state.get(\"active_language\", \"\")\n"
      "  _fam = _lang_family(lang)\n"
      "  if _fam == \"en\" or _fam not in _LANG_MARKERS:\n"
      "    return None  # base language, or no heuristic for it: skip the drift nudge\n"
      "  content = getattr(llm_response, \"content\", None)\n"
      "  parts = getattr(content, \"parts\", None) or []\n"
      "  if any(getattr(p, \"function_call\", None) for p in parts):\n"
      "    return None  # tool turn: no free text to check\n"
      "  text = \" \".join((getattr(p, \"text\", None) or \"\") for p in parts).strip()\n"
      "  if not text:\n"
      "    return None\n"
      "  if _response_language_ok(text, lang):\n"
      "    if sm.get(\"_lang_nudge\"):\n"
      "      sm[\"_lang_nudge\"] = 0\n"
      "      callback_context.state[\"sm\"] = sm\n"
      "    return None\n"
      "  if sm.get(\"_lang_nudge\", 0) >= 2:\n"
      "    sm[\"_lang_nudge\"] = 0  # fail open after the cap\n"
      "    callback_context.state[\"sm\"] = sm\n"
      "    return None\n"
      "  sm[\"_lang_nudge\"] = sm.get(\"_lang_nudge\", 0) + 1\n"
      # The regeneration stands BESIDE the drifted reply, it does not replace it: on\n
      # gemini-3.1-flash-live the text has already been streamed to the caller by the\n
      # time this hook runs and cannot be retracted, in this shape or any other\n
      # (ces-probes 91). So the caller hears the wrong-language line and then the\n
      # corrected one. Logged, not silently assumed, since only before_model can\n
      # actually prevent it.\n
      "  if \"live\" in str(sm.get(\"_model\") or \"\").lower():\n"
      "    sm.setdefault(\"_log\", []).append({\"src\": \"after_model\",\n"
      "        \"tag\": \"supersede_not_retractable\", \"level\": \"WARNING\",\n"
      "        \"data\": {\"path\": \"language_nudge\"}})\n"
      "  callback_context.state[\"sm\"] = sm\n"
      "  return LlmResponse.from_parts(parts=[Part.from_function_call(name=\"try_again\", args={})])\n"
  )
