# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""PolySynth callback function optimized with real-time concurrent notification dispatches and dynamic MAC-Less fallbacks."""

# pylint: disable=undefined-variable

import concurrent.futures
import datetime
import json
import re
import time
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# POST-DIAGNOSTICS "View troubleshooting summary" CARD (deterministic)
# ---------------------------------------------------------------------------
# The diagnostic-ERROR verdict below is produced HERE in this callback (it returns
# Content directly to guarantee the transfer + message, since the LLM can't reliably
# emit text + a tool call in one turn). That bypasses the orchestrator LLM, so the
# LLM's plain-text [[TS]] summary never runs on this path. To keep the "View
# troubleshooting summary" card present for network/gateway(RDK)/outage FAILURES,
# we render the SAME approved card here, from grounded fields composed off the
# statuses already collected. Card HTML is byte-for-byte identical to the
# after_model / after_agent renderers.
_SUMMARY_SECTIONS = (
    ("happening", "What's happening"),
    ("why", "Why"),
    ("doing", "What we're doing"),
    ("todo", "What you need to do"),
)
# A recognized voice/IVR platform suppresses the HTML card; everything else
# (AIQSDK / xFiMobileXAIOS / xFiMobileXAAndroid, or missing/unrecognized = web
# default) shows it — matching the instruction's web/app surface gate.
_VOICE_PLATFORMS = {"ivr", "voice", "phone", "audio", "voip"}

# Card <summary> labels. The XIT/Convoy (predictive alert) card is labelled
# "View alert summary" so it is visually distinguishable from the diagnostics-driven
# "View troubleshooting summary" card produced for Connect/RDK/outage verdicts.
# Both labels MUST stay on the after_model / after_agent approved-card allow-lists.
_CARD_LABEL_TROUBLESHOOTING = "View troubleshooting summary"
_CARD_LABEL_ALERTS = "View alert summary"


def _esc_summary_field(s) -> str:
  """Escape only the field TEXT (never the card's own tags)."""
  return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_summary_card(fields: Dict[str, str], label: str = _CARD_LABEL_TROUBLESHOOTING) -> str:
  """Build the approved collapsible card from the four fields (identical HTML to
  the after_model / after_agent renderers)."""
  ps = "".join(
      "<p style='margin:8px 0;'><strong>{}:</strong> {}</p>".format(
          label_text, _esc_summary_field(fields[key].strip())
      )
      for key, label_text in _SUMMARY_SECTIONS
      if str(fields.get(key, "")).strip()
  )
  return (
      "<details style='border:1px solid #d0d0d0;border-radius:8px;padding:8px 12px;margin-top:10px;background:#ffffff;color:#202124;'>"
      "<summary style='cursor:pointer;font-weight:bold;color:#1a73e8;'>{}</summary>".format(label)
      + "<div style='margin-top:8px;line-height:1.5;color:#202124;'>{}</div></details>".format(ps)
  )


def _summary_surface_ok(callback_context) -> bool:
  """Web/app only, mirroring the instruction: suppress the card ONLY on a
  recognized voice/IVR platform; show it otherwise (web is the default)."""
  platform = str(callback_context.state.get("platform", "") or "").strip().lower()
  return platform not in _VOICE_PLATFORMS


def _prepend_summary_card(
    callback_context,
    fields: Dict[str, str],
    message: str,
    label: str = _CARD_LABEL_TROUBLESHOOTING,
) -> str:
  """Return `message` with the rendered card placed FIRST (card, blank line, then
  the message). Fully guarded: ANY failure returns the original message unchanged
  so the verdict + transfer are never broken by summary rendering."""
  try:
    if not _summary_surface_ok(callback_context):
      return message
    if not any(str(fields.get(k, "")).strip() for k, _ in _SUMMARY_SECTIONS):
      return message
    card = _render_summary_card(fields, label)
    msg = (message or "").strip()
    return (card + ("\n\n" + msg if msg else "")).strip()
  except Exception as e:  # noqa: BLE001
    print(f"[before_agent_callback] summary card render skipped: {e}")
    return message


def _xit_summary_fields(callback_context, state_key: str = "convoy_summary_fields") -> Dict[str, str]:
  """Read the XIT recommendation's customer copy out of state.

  Returns {headline?, happening, why, doing, whatYouNeedToDo}. Populated by the
  check_convoy_recommendations tool (its per-recommendation registry is the single
  source of truth). 'headline' is present only when product authored copy for that
  recommendation; callers pop it and fall back to adkCustomerMessage when it is absent.
  Returns {} when absent/malformed, in which case the caller renders NO card and the
  message is unchanged.
  """
  try:
    raw = callback_context.state.get(state_key, "")
    if not raw:
      return {}
    fields = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(fields, dict):
      return {}
    allowed = set(dict(_SUMMARY_SECTIONS)) | {"headline"}
    return {k: str(v) for k, v in fields.items() if k in allowed and v}
  except Exception as e:  # noqa: BLE001
    print(f"[before_agent_callback] XIT summary fields unavailable: {e}")
    return {}



def _join_readable(items) -> str:
  """Join a list into readable English: 'a', 'a and b', 'a, b, and c'."""
  items = [i for i in items if i]
  if not items:
    return ""
  if len(items) == 1:
    return items[0]
  if len(items) == 2:
    return "{} and {}".format(items[0], items[1])
  return "{}, and {}".format(", ".join(items[:-1]), items[-1])


def _build_error_summary_fields(callback_context) -> Dict[str, str]:
  """Compose a GROUNDED summary for a diagnostic-error turn from the statuses
  already collected. States only what is actually known — which checks came back
  clear and which ran into an error — and never invents a cause."""
  st = callback_context.state

  def _v(k):
    return str(st.get(k, "") or "").strip().lower()

  account, outage = _v("account_status"), _v("outage_status")
  network, gateway = _v("network_status"), _v("gateway_status")

  clear = []
  if account in ("clear", "active", "healthy", "good"):
    clear.append("your account is in good standing")
  if outage in ("none", "healthy", "clear", "skipped"):
    clear.append("there's no outage in your area")
  if network == "healthy":
    clear.append("the signal on the line into your home tested normal")
  if gateway == "healthy":
    clear.append("your gateway checked out healthy")

  errored = []
  if network == "error":
    errored.append("the network line-signal check")
  if gateway == "error":
    errored.append("the gateway (modem) check")
  if outage == "error":
    errored.append("the area outage check")

  verb = "ran into errors" if len(errored) > 1 else "ran into an error"
  tail = " and couldn't finish, so we couldn't complete the full picture."
  if clear and errored:
    why = _join_readable(clear).capitalize() + ". However, " + _join_readable(errored) + " " + verb + tail
  elif errored:
    why = _join_readable(errored).capitalize() + " " + verb + tail
  elif clear:
    why = _join_readable(clear).capitalize() + ", but one of our checks couldn't finish, so we couldn't complete the full picture."
  else:
    why = "One of our checks ran into an error and couldn't finish, so we couldn't complete the full picture."

  return {
      "happening": "We ran a full check on your connection but couldn't finish all the diagnostics.",
      "why": why,
      "doing": "Getting you connected to a specialist who can take a closer look.",
      "todo": "Stay with us while we connect you to a live agent.",
  }


def _extract_technician_type(data: Any) -> str:
  """Format-tolerant extraction of the recommended technician type.

  The network specialist LLM emits inconsistent JSON shapes: sometimes the
  instructed lowercase form ('recommendation'/'technician_type'), sometimes a
  capitalized form ('Recommendation'/'Technician Type' alongside
  'Analysis'/'Severity'). Match keys case-insensitively and ignore
  spaces/underscores so either shape resolves correctly.
  """
  if not isinstance(data, dict):
    return ""
  rec: Dict[str, Any] = {}
  for k, v in data.items():
    if isinstance(k, str) and k.strip().lower() == "recommendation" and isinstance(v, dict):
      rec = v
      break
  for k, v in rec.items():
    if isinstance(k, str) and k.strip().lower().replace(" ", "").replace("_", "") == "techniciantype":
      return str(v or "").strip()
  return ""


def _find_balanced_json(text: str) -> str:
  """Return the first balanced ``{...}`` JSON object substring, or ''.

  Tolerates trailing prose (e.g. a "## Tool Calls Summary" block) the network
  specialist appends after the JSON payload by matching braces while respecting
  string literals.
  """
  if not isinstance(text, str):
    return ""
  start = text.find("{")
  if start == -1:
    return ""
  depth = 0
  in_str = False
  esc = False
  for i in range(start, len(text)):
    c = text[i]
    if in_str:
      if esc:
        esc = False
      elif c == "\\":
        esc = True
      elif c == '"':
        in_str = False
    else:
      if c == '"':
        in_str = True
      elif c == "{":
        depth += 1
      elif c == "}":
        depth -= 1
        if depth == 0:
          return text[start:i + 1]
  return ""


def _build_appointment_transfer_data(callback_context) -> str:
  """Builds the appointment transfer_data payload from session state.

  All fields (including problemCode/intents) are always included, mirroring how
  activityType/activityCode/jobType are handled — empty/absent values simply
  flow through as empty strings.
  """
  inner = {
      "source": "repair_gecx_agent",
      "activityType": callback_context.state.get("activityType", "TROUBLE_CALL"),
      "activityCode": callback_context.state.get("activityCode", ""),
      "jobType": callback_context.state.get("jobType", ""),
      "problemCode": callback_context.state.get("problemCode", ""),
      "intents": callback_context.state.get("intents", ""),
  }
  return json.dumps({"transfer_data": inner})


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
        texts.append(t)
    return " ".join(texts).strip()
  except Exception as e:
    print(f"[before_agent_callback] Could not read last user input: {e}")
    return ""


def _is_outage_inquiry(text: str) -> bool:
  """True when the customer is ASKING whether there is an outage / whether
  service is out in their area, as opposed to reporting their own broken
  device. Keyword/phrase based so it is deterministic and audio-safe."""
  t = (text or "").lower().strip()
  if not t:
    return False
  # Strong signal: the word "outage" is almost always an outage-status question.
  if "outage" in t:
    return True
  # "Is <provider/service/network> down/out (in my area)?" style questions.
  down_phrases = (
      "is xfinity down",
      "is comcast down",
      "is service down",
      "is the service down",
      "is service out",
      "is the service out",
      "is the network down",
      "service down in my area",
      "service out in my area",
      "down in my area",
      "out in my area",
      "known issue",
      "known issues",
      "any issues in my area",
  )
  return any(p in t for p in down_phrases)


# Third-party TV devices and streaming apps that must NEVER be classified as
# Xfinity Video issues. These connect over the customer's internet — any failure
# is an internet/device-helper problem, not an X1/STB/cable-box issue.
# Checked AFTER the XRE prefix (XRE codes are Xfinity-only; no third-party
# device can show one) and BEFORE every other TV-noun/symptom check.
_THIRD_PARTY_TV_EXCLUSIONS = (
    "apple tv", "appletv",
    "roku", "fire tv", "firetv", "firestick",
    "google tv", "android tv", "chromecast",
    "smart tv",
    "samsung tv", "lg tv", "vizio",
    "netflix", "hulu", "disney+", "disney plus",
    "amazon prime", "peacock", "youtube tv",
    "sling", "fubo", "philo",
)

# ---------------------------------------------------------------------------
# STREAMING SERVICE DISAMBIGUATION
# ---------------------------------------------------------------------------
# Xfinity actively bundles Apple TV+, Netflix, Peacock, Max, and others.
# "Netflix is not working" can mean:
#   (a) Internet connectivity issue  → run the Internet diagnostic battery.
#   (b) Xfinity subscription/app issue → human agent.
# We ask ONE disambiguation question before routing, exactly like the
# outage-inquiry consent gate.

# Streaming services Xfinity bundles/supports (subscription or app).
_XFINITY_STREAMING_SERVICES = (
    "apple tv", "appletv", "apple tv+", "apple tv plus",
    "netflix",
    "peacock",
    "disney+", "disney plus", "disneyplus",
    "hbo max", "hbo", "max",
    "paramount+", "paramount plus",
    "amazon prime", "prime video",
    "hulu",
    "espn+", "espn plus",
    "youtube tv",
)

# Pretty-print mapping used in the disambiguation question.
_STREAMING_DISPLAY_NAME = {
    "apple tv": "Apple TV", "appletv": "Apple TV",
    "apple tv+": "Apple TV+", "apple tv plus": "Apple TV+",
    "netflix": "Netflix",
    "peacock": "Peacock",
    "disney+": "Disney+", "disney plus": "Disney+", "disneyplus": "Disney+",
    "hbo max": "Max", "hbo": "Max", "max": "Max",
    "paramount+": "Paramount+", "paramount plus": "Paramount+",
    "amazon prime": "Amazon Prime Video", "prime video": "Amazon Prime Video",
    "hulu": "Hulu",
    "espn+": "ESPN+", "espn plus": "ESPN+",
    "youtube tv": "YouTube TV",
}

# Complaint verbs that, paired with a streaming-service name, signal the query
# is ambiguous (internet vs subscription). Kept narrow to avoid false positives.
_STREAMING_COMPLAINT_CUES = (
    "not working", "won't work", "isn't working", "doesn't work",
    "not loading", "won't load", "isn't loading",
    "not playing", "won't play",
    "can't access", "can't connect", "can't open",
    "not opening", "won't open",
    "keeps buffering", "keeps freezing", "keeps crashing",
    "having issues", "having trouble", "having problems",
    "stopped working", "not available", "not responding",
)


def _is_streaming_service_ambiguous(text: str) -> bool:
  """True when the message mentions an Xfinity-supported streaming service with a
  complaint cue AND the intent (internet connectivity vs subscription/app) is unclear.

  Explicitly NOT ambiguous (returns False) when:
  - The text already contains clear internet/connectivity words → let the Internet
    battery run directly.
  - No complaint cue is present (pure mention, no problem indicated).
  """
  t = (text or "").lower().strip()
  if not t:
    return False
  if not any(s in t for s in _XFINITY_STREAMING_SERVICES):
    return False
  if not any(c in t for c in _STREAMING_COMPLAINT_CUES):
    return False
  # Already clearly internet → no disambiguation needed, fall through to the battery.
  clear_internet = ("internet", "wifi", "wi-fi", "connection", "network", "speed",
                    "all devices", "everything", "other apps", "whole house",
                    "no internet", "can't get online")
  if any(c in t for c in clear_internet):
    return False
  return True


def _has_tv_steering_intent(text: str) -> bool:
  """True when the inbound message carries a steering TV/video intent header.

  Example prefix from steering:
  '### Automated system message intent identified: tv.equipment.issue ...'
  """
  t = (text or "").lower()
  return (
      "automated system message intent identified: tv." in t
      or "automated system message intent identified: video." in t
      or "intent identified: tv." in t
      or "intent identified: video." in t
      or "tv.equipment.issue" in t
      or "video.service.issue" in t
  )


def _get_streaming_service_display_name(text: str) -> str:
  """Returns the display name of the first matched streaming service."""
  t = (text or "").lower()
  # Match longest keys first so "apple tv+" beats "apple tv".
  for key in sorted(_STREAMING_DISPLAY_NAME, key=len, reverse=True):
    if key in t:
      return _STREAMING_DISPLAY_NAME[key]
  return "streaming service"


def _classify_streaming_response(text: str) -> str:
  """Classifies a streaming disambiguation follow-up.

  Returns:
    "internet"      - connectivity/internet issue → run the battery.
    "subscription"  - subscription/app/account issue → human agent.
    "unclear"       - can't tell → default to internet (safer: run diagnostics).
  """
  t = (text or "").lower().strip()

  # Negated internet phrases ("not my internet", "not the internet", "not internet")
  # should NOT count as internet signals — strip them first.
  t_for_internet = re.sub(r"not\s+(my\s+)?internet", "", t)
  t_for_internet = re.sub(r"(it'?s\s+)?not\s+(the\s+)?internet", "", t_for_internet)

  internet_signals = (
      "internet", "wifi", "wi-fi", "connection", "network", "speed",
      "connectivity", "connect", "all devices", "everything is down",
      "everything stopped", "slow",
      "all apps", "whole house", "other apps also",
      "can't get online", "no internet", "my internet",
  )
  subscription_signals = (
      "subscription", "billing", "payment", "subscribe", "cancel",
      "log in", "login", "password", "sign in", "account",
      "not included", "add channel", "missing", "plan",
      "just that app", "only that app", "just the app", "the app itself",
      "streaming", "playback", "the service", "service itself",
      "just netflix", "just peacock", "just apple", "just hulu",
      "only netflix", "only peacock", "only apple", "only hulu",
  )
  i_score = sum(1 for s in internet_signals if s in t_for_internet)
  s_score = sum(1 for s in subscription_signals if s in t)
  if s_score > i_score:
    return "subscription"
  if i_score > 0:
    return "internet"
  # Short affirmatives to "is it internet?" → internet.
  if any(t == a or t.startswith(a + " ") for a in ("yes", "yeah", "yep", "correct", "right", "exactly", "internet", "connection")):
    return "internet"
  # Default: run diagnostics (safe — never wrongly block troubleshooting).
  return "unclear"


def _is_explicit_internet_topic(text: str) -> bool:
  """True when the customer is clearly asking for Internet/Wi-Fi troubleshooting.

  Mirrors the orchestrator's 0v exception semantics: when a session is currently
  sticky Video (`video_flow_active=true`) but the latest message is a clearly NEW
  Internet topic, callback should NOT keep the turn video-scoped.
  """
  t = (text or "").lower().strip()
  if not t:
    return False
  cues = (
      "my internet", "internet is", "internet's", "internet keeps",
      "wifi", "wi-fi", "network",
      "can't get online", "cannot get online", "no internet",
      "connect to wifi", "connect to wi-fi", "won't connect to wifi",
      "wont connect to wifi", "wifi keeps dropping", "internet is slow",
      "connection is slow", "my connection",
  )
  return any(c in t for c in cues)


def _is_video_routed_turn(state: Dict[str, Any], last_user_text: str) -> bool:
  """Deterministic callback-level route classifier for this turn.

  True means: this turn should stay on the Video route in callback scope (run
  shared account/outage checks, skip Internet battery, defer to 0v/video agent).
  """
  video_enabled = str(state.get("video_flow_enabled", "")).lower() == "true"
  if not video_enabled:
    return False
  video_signal = _is_video_issue(last_user_text)
  video_active = str(state.get("video_flow_active", "")).lower() == "true"

  # Critical cross-domain exception: when a sticky Video session receives a clear
  # NEW Internet topic, release callback-level video scoping so the full Internet
  # diagnostic battery can run on this turn.
  if video_active and _is_explicit_internet_topic(last_user_text) and not video_signal:
    return False

  return video_signal or video_active


def _should_skip_internet_convoy_dispatch(state: Dict[str, Any]) -> bool:
  """True when callback-level Internet convoy dispatch must be suppressed."""
  return str(state.get("flow_route_hint", "")).lower() == "video"


def _should_prefetch_video_xit(state: Dict[str, Any]) -> bool:
  """True when a Video diagnostics turn should check Convoy for missing video XIT."""
  video_cached = bool(str(state.get("video_xit_recommendation", "")).strip())
  return not video_cached


def _prefetch_video_xit_if_needed(callback_context, account_number: str):
  """Best-effort Convoy prefetch for Video turns; keeps Internet dispatch inert."""
  if not _should_prefetch_video_xit(callback_context.state):
    return
  try:
    print(
        "[before_agent_callback] Video turn: prefetching convoy recommendations"
        " once for video_xit_recommendation cache."
    )
    tools.check_convoy_recommendations({"account_number": account_number})
  except Exception as e:
    print(f"[before_agent_callback] Video prefetch failed (non-fatal): {e}")


def _is_video_issue(text: str) -> bool:
  """True when the latest message is a clear VIDEO/TV/X1 problem the Video
  specialist should own -- mirrors the instruction's "0v VIDEO ROUTING"
  indicators so the internet diagnostic battery (account/outage/gateway + the
  "modem offline" notifications) is SKIPPED for a TV turn. Deterministic +
  audio-safe, and PRECISION-biased: it only matches a clear TV signal, so a pure
  internet/Wi-Fi complaint never matches (worst case we miss a TV cue and fall
  back to today's behavior). A clear TV symptom wins even when the customer also
  mentions internet (e.g. "not sure if it's my TV or internet, the TV stopped
  playing") -- the Video agent hands back to the Internet flow if it turns out to
  be connectivity.

  Third-party TV devices (Apple TV, Roku, Fire TV, smart TVs) and streaming apps
  (Netflix, Hulu, etc.) are EXCLUDED -- those are internet/device-helper issues
  even when the word "TV" appears in the brand name (e.g. "apple tv won't connect"
  contains the substring "tv won" but is NOT an Xfinity video/STB issue).
  """
  t = (text or "").lower().strip()
  if not t:
    return False
  # Strong deterministic signal: steering already identified TV/video intent.
  # Respect this even when customer wording uses variants our phrase list may
  # not cover (e.g. "setup box is pixelating").
  if _has_tv_steering_intent(t):
    return True
  # XRE error code prefix -> Xfinity video error, always TV (no third-party
  # device can display an Xfinity XRE code, so the exclusion list below never
  # needs to override this).
  if "xre" in t:
    return True
  # Third-party TV brands / streaming apps -> Internet/device-helper, NOT Video.
  # This runs BEFORE the TV-noun check to catch names like "apple tv" that contain
  # fragments ("tv won") which would otherwise produce a false positive.
  if any(ex in t for ex in _THIRD_PARTY_TV_EXCLUSIONS):
    return False
  # Clear TV / cable-box / set-top / X1 nouns.
  tv_nouns = (
      "my tv", "the tv", "our tv", "on tv", "tv is", "tv isn", "tv won",
      "tv stopped", "tv screen", "tv keeps", "tv has", "tv turns",
      "my television", "the television", "cable box", "set-top", "set top",
      "settop", "setup box", "set up box", "set-up box", "x1 box",
      "cable is out", "no cable",
  )
  if any(k in t for k in tv_nouns):
    return True
  # TV-specific symptoms (deliberately narrow to avoid matching internet talk).
  symptoms = (
      "no picture", "black screen", "blank screen", "frozen screen",
      "screen is frozen", "picture is frozen", "picture keeps breaking",
      "pixelated", "pixelating", "pixelation", "missing channels", "channel not working",
      "channels aren", "channels are out", "channel is out",
      "voice remote", "tv remote", "remote won", "remote isn",
      "remote not working", "pair my remote", "pair the remote",
  )
  return any(k in t for k in symptoms)


def _is_speed_test_request(text: str) -> bool:
  """True when the customer is explicitly asking to RUN/SEE a speed test.

  Keyword/phrase based so it is deterministic and audio-safe. A plain "my
  internet is slow" is a CONNECTIVITY COMPLAINT (not a speed-test request) and
  must NOT match here."""
  t = (text or "").lower().strip()
  if not t:
    return False
  # Strong signal: the words "speed test" / "speedtest" are almost always a
  # request to run one.
  if "speed test" in t or "speedtest" in t:
    return True
  phrases = (
      "test my speed",
      "test my internet speed",
      "check my speed",
      "check my internet speed",
      "run a speed",
      "how fast is my internet",
      "how fast is my connection",
      "what speed am i getting",
      "what speeds am i getting",
      "see my speed",
      "see my speeds",
      "measure my speed",
  )
  return any(p in t for p in phrases)


def _is_selfserve_howto(text: str) -> bool:
  """True when the customer is asking a SELF-SERVE how-to / settings question.

  These are INFORMATION requests the customer performs themselves on the web
  (xfinity.com) or in the Xfinity app -- change/reset/find the Wi-Fi name or
  password, set up a guest network, or set up/change parental controls -- NOT a
  broken-connection complaint. Deterministic + audio-safe so the diagnostic
  battery is skipped and the instruction's "0a SELF-SERVE HOW-TO" pre-check can
  route to Wi-Fi Settings Help. Mirrors the 0a SELF-SERVE HOW-TO INDICATORS in
  the instruction. Conservative: requires BOTH a settings noun AND a self-serve
  action/intent, and never matches a connectivity complaint.
  """
  t = (text or "").lower().strip()
  if not t:
    return False
  # A connectivity COMPLAINT is never a self-serve how-to (run diagnostics).
  complaint_markers = (
      "not working",
      "isn't working",
      "isnt working",
      "won't connect",
      "wont connect",
      "not connecting",
      "keeps dropping",
      "keep dropping",
      "dropping",
      "no internet",
      "no wifi",
      "no wi-fi",
      "is down",
      "is slow",
      "so slow",
      "too slow",
      "very slow",
      "stopped working",
  )
  if any(m in t for m in complaint_markers):
    return False
  # Self-serve settings nouns.
  settings_nouns = (
      "wifi name",
      "wi-fi name",
      "network name",
      "ssid",
      "wifi password",
      "wi-fi password",
      "wifi passphrase",
      "wi-fi passphrase",
      "network password",
      "wireless password",
      "passphrase",
      "guest network",
      "parental control",
      "parental controls",
      "parental pin",
  )
  if not any(n in t for n in settings_nouns):
    return False
  # Self-serve action/intent (either "how do I..." or "do it for me").
  action_markers = (
      "how do i",
      "how can i",
      "how to",
      "how would i",
      "where do i",
      "where can i",
      "where is my",
      "what is my",
      "what's my",
      "change",
      "reset",
      "update",
      "set up",
      "setup",
      "set-up",
      "create",
      "find",
      "see",
      "view",
      "rename",
  )
  return any(a in t for a in action_markers)


def _classify_outage_consent(text: str) -> str:
  """Classifies a customer's reply to the 'want me to run a check?' offer.

  Returns one of: "agent", "decline", "accept", "unclear".
  """
  t = (text or "").lower().strip()
  if not t:
    return "unclear"
  tokens = set(re.findall(r"[a-z']+", t))
  if (
      tokens & {"agent", "human", "representative", "rep", "supervisor", "person"}
      or "live agent" in t
      or "real person" in t
  ):
    return "agent"
  if (
      tokens & {"no", "nope", "nah", "stop"}
      or "no thanks" in t
      or "no thank you" in t
      or "that's all" in t
      or "thats all" in t
      or "i'm good" in t
      or "im good" in t
      or "never mind" in t
      or "nevermind" in t
      or "all set" in t
  ):
    return "decline"
  if (
      tokens & {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please"}
      or "go ahead" in t
      or "do it" in t
      or "sounds good" in t
      or "run" in t
      or "check" in t
      or "go for it" in t
  ):
    return "accept"
  return "unclear"


# Vague/unspecified internet-complaint cues. Paired with an internet/connection
# noun (or a "get online" phrase) they signal the customer HASN'T established the
# scope (nothing connecting vs. slow vs. certain rooms vs. one device/app), so we
# should ask ONE clarifying question BEFORE running the full diagnostic battery.
_VAGUE_INTERNET_CUES = (
    "acting up", "acting weird", "acting strange", "acting funny",
    "being weird", "being strange", "being funny",
    "is broken", "broken", "messed up", "messing up", "keeps messing up",
    "spotty", "flaky", "wonky", "glitchy", "janky",
    "not working right", "isn't working right", "isnt working right",
    "not working properly", "isn't working properly",
    "not great", "isn't great", "isnt great", "not good", "not the best",
    "seems off", "seems weird", "something's wrong", "somethings wrong",
    "something wrong", "something is wrong", "not right",
    "having issues", "having trouble", "having problems", "having some trouble",
    "some issues", "some trouble", "some problems",
    "trouble", "issue", "issues", "problem", "problems",
    "not cooperating", "won't cooperate", "wont cooperate",
    "unreliable", "not reliable", "in and out", "off and on",
    "hit or miss", "hit and miss", "keeps acting", "barely works",
    "hardly works", "not behaving", "misbehaving", "playing up",
    "having a hard time", "struggling", "weird",
    # tentative "can't get online" phrasings (uncertain scope, unlike a flat
    # "no internet" which is a clear total-loss and runs diagnostics directly).
    "don't think i can get online", "dont think i can get online",
    "can't seem to get online", "cant seem to get online",
    "trouble getting online", "having trouble getting on",
    "can't really get online", "cant really get online",
)

# Internet/connection nouns that anchor a vague cue to the internet domain.
_VAGUE_INTERNET_NOUNS = (
    "internet", "wifi", "wi-fi", "wi fi", "connection", "connectivity",
    "network", "service", "online",
)

# Clear TOTAL-LOSS phrases: the customer has already established "nothing is
# connecting", so no clarification is needed — run diagnostics directly. These
# take precedence over a vague cue in the same message.
_CLEAR_TOTAL_LOSS = (
    "no internet", "no wifi", "no wi-fi", "no connection", "no service",
    "nothing loads", "nothing will load", "nothing works", "nothing is working",
    "won't load anything", "wont load anything",
    "can't connect to anything", "cant connect to anything",
    "everything is down", "everything's down", "all my devices are offline",
    "service is out", "internet is out", "totally down", "completely down",
    "completely out", "totally out", "internet is down", "wifi is down",
    "wi-fi is down", "completely dead", "totally dead",
)


def _is_vague_internet_opener(text: str) -> bool:
  """True when the customer reports a VAGUE internet problem that should be
  disambiguated (via the R40 clarification question) BEFORE running the full
  diagnostic battery.

  Precision-biased so we never gate a message that already establishes scope:
  - Requires an internet/connection noun (or a "get online" phrase) AND a vague
    cue (e.g. "broken", "spotty", "acting up", "not working right").
  - Returns False for a clear TOTAL-LOSS ("no internet", "nothing loads",
    "everything is down") — those run diagnostics directly.
  - Returns False when a specific third-party streaming service is named (those
    are owned by the streaming-disambiguation gate).
  Other gates (self-serve how-to, streaming, video, speed test, outage inquiry)
  run BEFORE this one, so a match here is a genuine vague connectivity opener.
  """
  t = (text or "").lower().strip()
  if not t:
    return False
  # A clearly-established total loss needs no clarification — run diagnostics.
  if any(p in t for p in _CLEAR_TOTAL_LOSS):
    return False
  # A named streaming service is owned by the streaming-disambiguation gate.
  if any(s in t for s in _XFINITY_STREAMING_SERVICES):
    return False
  # A named third-party device/app is app-specific — per R10 that runs the full
  # diagnostics first (and asks the scope question only at the all-clear stage),
  # so it must NOT be pulled into the up-front vague gate here.
  _named_devices = (
      "laptop", "phone", "iphone", "ipad", "tablet", "computer", "desktop",
      "xbox", "playstation", "ps5", "ps4", "nintendo", "printer", "roku",
      "firestick", "fire stick", "chromecast", "smart tv", "alexa", "echo",
  )
  if any(d in t for d in _named_devices):
    return False
  has_noun = any(n in t for n in _VAGUE_INTERNET_NOUNS)
  has_cue = any(c in t for c in _VAGUE_INTERNET_CUES)
  # "get online" phrasings carry their own internet anchoring.
  online_phrase = "get online" in t or "getting online" in t or "get on the internet" in t
  return has_cue and (has_noun or online_phrase)


def _classify_vague_response(text: str) -> str:
  """Classifies the customer's answer to the vague-opener clarification
  question ("nothing connecting / slow / certain rooms / one device or app?").

  Returns one of:
    "agent"       - wants a human instead of answering -> transfer.
    "one_device"  - only one device/app is affected -> set app-scope flags, then
                    run diagnostics (existing Priority 10 app-scope flow).
    "proceed"     - nothing-connecting / slow / room-specific / unclear -> run
                    the full diagnostic battery normally.
  """
  t = (text or "").lower().strip()
  if not t:
    return "proceed"
  tokens = set(re.findall(r"[a-z']+", t))
  if (
      tokens & {"agent", "human", "representative", "rep", "supervisor", "person", "someone"}
      or "live agent" in t
      or "real person" in t
      or "talk to someone" in t
  ):
    return "agent"
  one_device_cues = (
      "one device", "single device", "just one", "only one", "one of my",
      "just my", "only my", "only on my", "just on my", "only on one",
      "one app", "just one app", "only that", "just that", "single app",
      "one thing", "just one thing", "on one device",
  )
  if any(c in t for c in one_device_cues):
    return "one_device"
  # Named single device/app without an "only/just" qualifier still implies scope.
  device_nouns = (
      "laptop", "phone", "iphone", "ipad", "tablet", "computer", "desktop",
      "xbox", "playstation", "ps5", "ps4", "nintendo", "switch", "printer",
      "roku", "firestick", "fire stick", "chromecast", "smart tv",
  )
  if any(s in t for s in _XFINITY_STREAMING_SERVICES) or any(d in t for d in device_nouns):
    # Only treat as single-scope when the customer frames it as limited.
    if any(w in t for w in ("only", "just", "single", "one")):
      return "one_device"
  return "proceed"


def _extract_vague_device_target(text: str) -> str:
  """Best-effort extraction of the specific device/app the customer named when
  they say only one thing is affected. Falls back to a generic label."""
  t = (text or "").lower()
  for key in sorted(_STREAMING_DISPLAY_NAME, key=len, reverse=True):
    if key in t:
      return _STREAMING_DISPLAY_NAME[key]
  named = {
      "laptop": "laptop", "phone": "phone", "iphone": "iPhone", "ipad": "iPad",
      "tablet": "tablet", "computer": "computer", "desktop": "desktop",
      "xbox": "Xbox", "playstation": "PlayStation", "ps5": "PlayStation",
      "ps4": "PlayStation", "nintendo": "Nintendo Switch", "switch": "Nintendo Switch",
      "printer": "printer", "roku": "Roku", "firestick": "Fire TV",
      "fire stick": "Fire TV", "chromecast": "Chromecast", "smart tv": "smart TV",
      "tv": "TV",
  }
  for key in sorted(named, key=len, reverse=True):
    if key in t:
      return named[key]
  return "one device"


def _safe_parse_to_dict(response) -> Dict[str, Any]:
  """Safely parses various response types (ExternalResponse, str, dict) into a dict."""
  if response is None:
    return {}
  if hasattr(response, "json") and callable(response.json):
    try:
      parsed = response.json()
      if isinstance(parsed, str):
        parsed = json.loads(parsed)
      if isinstance(parsed, dict):
        return parsed
    except Exception:
      pass
  if isinstance(response, dict):
    return response
  if isinstance(response, str):
    try:
      parsed = json.loads(response)
      if isinstance(parsed, dict):
        return parsed
    except json.JSONDecodeError:
      return {"text": response}
  return {}


def _unwrap_result(res_dict) -> Dict[str, Any]:
  """Safely extracts and parses the nested 'result' key from a response dictionary."""
  if not isinstance(res_dict, dict):
    return {}
  res = res_dict.get("result", res_dict)
  if isinstance(res, str):
    res = _safe_parse_to_dict(res)
  if isinstance(res, dict) and "result" in res:
    res = res["result"]
    if isinstance(res, str):
      res = _safe_parse_to_dict(res)
  return res if isinstance(res, dict) else {}


def _parse_tool_dict(resp) -> Dict[str, Any]:
  """Parse a callback tool return (dict/str/ExternalResponse) into a status dict.

  The speed-test / reboot wrappers return a top-level dict with a "status" key.
  The framework sometimes wraps that in a {"result": ...} envelope, so unwrap once
  when the top level lacks "status"."""
  d = _safe_parse_to_dict(resp)
  if isinstance(d, dict) and "status" not in d and "result" in d:
    d = _unwrap_result(d)
  return d if isinstance(d, dict) else {}


def _build_followup_response(callback_context) -> str:
  """Builds a contextual follow-up response based on diagnostic state variables.
  Mirrors the Follow-Up Gate QUESTION logic from instructions."""
  outage_status = callback_context.state.get("outage_status", "")
  network_status = callback_context.state.get("network_status", "")
  gateway_status = callback_context.state.get("gateway_status", "")
  convoy_status = callback_context.state.get("convoy_status", "")

  if outage_status == "active":
    return "There's a service outage in your area and crews are working on it. You can enroll in outage SMS updates here: https://www.xfinity.com/support/enroll-sms"
  if network_status == "impaired":
    tech_type = callback_context.state.get("technician_type", "")
    if tech_type == "network_tech":
      return "We found a problem with the network signal going to your home. I'm connecting you with someone to get this sorted out for you."
    return "We found an issue with the connection to your home. A technician is being arranged to take a closer look."
  if gateway_status == "swap" or convoy_status == "predictive_swap":
    return "Your gateway has a hardware fault and needs replacement. You can get a new one shipped or swap at a store here: https://customer.xfinity.com/devices/equipment-update/new"
  if gateway_status == "reboot":
    return "Your gateway had a software issue and a reboot was recommended. If you're still having trouble, let me know."
  if network_status == "error" or gateway_status == "error":
    return "I wasn't able to get all the diagnostic info I need. I'm connecting you with someone who can help — they'll be with you shortly."
  # Default fallback
  return "I've already started connecting you with a specialist who can help. They'll be with you shortly."


# ---------------------------------------------------------------------------
# DETERMINISTIC SPEED-TEST FLOW (post-escalation / passthrough state)
# ---------------------------------------------------------------------------
# Once diagnostics have escalated to a human (escalate_to_human_flag == "on" or a
# terminal-error transfer), the orchestrator LLM is in passthrough/echo mode and
# cannot reliably run the LLM-driven "Speed Test" subtask. So when the customer
# asks for a speed test in that state, we run the SAME flow deterministically here
# (offer -> confirm -> run -> results card -> restart offer -> reboot), mirroring
# the instruction's Speed Test subtask, using the same tools and card template.
def _speed_test_headline(overall_result: str) -> str:
  r = str(overall_result or "").upper()
  if r == "FULL_PASS":
    return "You're all set!"
  if r == "RESTART":
    return "Your speeds could be better"
  return "Your speed test results"


def _render_speed_test_card(callback_context, result: Dict[str, Any]) -> str:
  """Render the approved 'View speed test results' <details> card (web/app only).
  Fully guarded: returns '' on voice/IVR surfaces or on any failure."""
  try:
    if not _summary_surface_ok(callback_context):
      return ""
    rows = [
        "<p style='margin:6px 0;font-weight:bold;'>{}</p>".format(
            _speed_test_headline(result.get("overall_result", ""))
        )
    ]
    dl = result.get("download_mbps")
    if dl is not None:
      pct, plan = result.get("download_percent_of_plan"), result.get("plan_download_mbps")
      if pct is not None and plan is not None:
        rows.append(
            "<p style='margin:6px 0;'><strong>Download:</strong> {} Mbps ({}% of your {} Mbps plan)</p>".format(dl, pct, plan)
        )
      else:
        rows.append("<p style='margin:6px 0;'><strong>Download:</strong> {} Mbps</p>".format(dl))
    ul = result.get("upload_mbps")
    if ul is not None:
      pct, plan = result.get("upload_percent_of_plan"), result.get("plan_upload_mbps")
      if pct is not None and plan is not None:
        rows.append(
            "<p style='margin:6px 0;'><strong>Upload:</strong> {} Mbps ({}% of your {} Mbps plan)</p>".format(ul, pct, plan)
        )
      else:
        rows.append("<p style='margin:6px 0;'><strong>Upload:</strong> {} Mbps</p>".format(ul))
    lat = result.get("latency_ms")
    if lat is not None:
      rows.append("<p style='margin:6px 0;'><strong>Latency:</strong> {} ms</p>".format(lat))
    return (
        "<details style='border:1px solid #d0d0d0;border-radius:8px;padding:8px 12px;margin-top:10px;'>"
        "<summary style='cursor:pointer;font-weight:bold;color:#1a73e8;'>View speed test results</summary>"
        "<div style='margin-top:8px;line-height:1.6;color:#202124;'>{}</div></details>".format("".join(rows))
    )
  except Exception as e:  # noqa: BLE001
    print(f"[before_agent_callback] speed test card render skipped: {e}")
    return ""


def _speed_test_blocked_reason(callback_context) -> str:
  """Return a customer-facing message when a speed test should NOT be run given the
  current diagnostic state, or '' to proceed.

  A speed test runs ON the gateway over the internet path, so it can't succeed (or
  would mislead) during an active area outage or when the gateway itself is
  down/faulty. In those states we skip the test and give the accurate reason.
  Tool-error/impaired states are NOT blocked here — the speed test is an independent
  measurement that may still return useful data.
  """
  st = callback_context.state

  def _v(k):
    return str(st.get(k, "") or "").strip().lower()

  if _v("outage_status") in ("active", "degradation"):
    return (
        "There's an outage in your area right now, so a speed test wouldn't give an"
        " accurate reading. I'm connecting you with someone who can help."
    )
  gateway_down = _v("gateway_status") in (
      "offline", "no_telemetry", "swap", "unsupported_device"
  )
  convoy_down = _v("convoy_status") in ("predictive_swap", "predictive_offline")
  if gateway_down or convoy_down:
    return (
        "Your gateway isn't responding normally right now, so a speed test wouldn't"
        " give an accurate reading. I'm connecting you with someone who can help."
    )
  return ""


_SPEED_TEST_UNSUPPORTED_NOW_MSG = (
    "Speed tests aren't supported here right now. I can still help troubleshoot"
    " your connection."
)


def _speed_test_execution_enabled(callback_context) -> bool:
  return (
      str(callback_context.state.get("speed_test_execution_enabled", "") or "")
      .strip()
      .lower()
      == "true"
  )


def _disabled_speed_test_response(callback_context) -> Content:
  callback_context.state["speed_test_confirm_pending"] = "false"
  callback_context.state["speed_test_async_result_pending"] = "false"
  callback_context.state["speed_test_restart_offer_pending"] = "false"
  print("[before_agent_callback] Speed-test execution disabled by feature flag.")
  return Content(parts=[Part(text=_SPEED_TEST_UNSUPPORTED_NOW_MSG)])


# ---------------------------------------------------------------------------
# CALLBACK TIME-BUDGET GUARD for in-callback tool calls
# ---------------------------------------------------------------------------
# The BeforeAgent callback runs in a sandbox with a HARD ~29s execution limit.
# The deterministic speed-test flow runs its tools INSIDE this callback (because
# the orchestrator LLM is in passthrough/echo mode after an escalation and can't
# be trusted to run them). The speed-test start/result calls should be quick, but
# every in-callback backend call is still bounded so a slow auth-proxy response
# degrades gracefully instead of letting the sandbox kill the whole callback.
_SPEED_TOOL_BUDGET_S = 24.0  # < the ~29s sandbox limit, leaves response headroom


def _run_tool_with_budget(fn, args, budget_s: float, label: str):
  """Run a (potentially slow) tool call with a hard time budget.

  Returns (completed, result). If the call exceeds `budget_s` we return
  (False, {}) and abandon the worker thread rather than let the sandbox kill the
  whole callback. The worker is a daemon so it never blocks process teardown.
  """
  try:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn, args)
    try:
      result = future.result(timeout=budget_s)
      executor.shutdown(wait=False)
      return True, result
    except concurrent.futures.TimeoutError:
      # Don't wait on the still-running worker; abandon it so we return in time.
      executor.shutdown(wait=False)
      print(
          f"[before_agent_callback] {label} exceeded {budget_s:.0f}s callback"
          " budget; degrading gracefully instead of timing out the sandbox."
      )
      return False, {}
  except Exception as e:  # noqa: BLE001
    print(f"[before_agent_callback] {label} failed under time-budget guard: {e}")
    return False, {}


def _handle_speed_test_request(callback_context) -> Optional[Content]:
  """Deterministic 'Offer Speed Test' step: resolve eligibility and offer to run."""
  print("[before_agent_callback] Deterministic speed-test request (intercepted/passthrough state).")
  if not _speed_test_execution_enabled(callback_context):
    return _disabled_speed_test_response(callback_context)
  # CAPABILITY GATE: a speed test runs ON the gateway over the internet path, so it
  # can't succeed (or would mislead) when the internet path is down (active outage)
  # or the gateway itself is down/faulty. Don't offer it in those states — give the
  # accurate reason and keep the human handoff, instead of wasting ~30s on a call
  # that will fail. (Tool-error/impaired states are still allowed: the speed test is
  # an independent measurement that may still give useful data.)
  blocked_msg = _speed_test_blocked_reason(callback_context)
  if blocked_msg:
    print("[before_agent_callback] Speed test not offered — blocked by diagnostic state.")
    return Content(parts=[Part(text=blocked_msg)])
  try:
    completed, resp = _run_tool_with_budget(
        tools.prepare_speed_test, {}, _SPEED_TOOL_BUDGET_S, "prepare_speed_test",
    )
    if not completed:
      resp = {}
  except Exception as e:
    print(f"[before_agent_callback] prepare_speed_test failed: {e}")
    resp = {}
  parsed = _parse_tool_dict(resp)
  if parsed.get("status") == "available":
    callback_context.state["speed_test_confirm_pending"] = "true"
    return Content(parts=[Part(text=(
        "Sure — a quick speed test may take a minute or so. Want me to go ahead and start it?"
    ))])
  # Unavailable / unknown -> do NOT offer or run.
  return Content(parts=[Part(text=(
      "A speed test isn't available on your account right now. I've already flagged"
      " the issue and I'm connecting you with someone who can help."
  ))])


def _build_speed_test_started_reply(callback_context, result: Dict[str, Any]) -> Optional[Content]:
  status = str(result.get("status", "")).lower()
  if status != "success":
    if str(result.get("reason", "")).lower() == "no_xbo":
      msg = (
          "A speed test isn't available on your account right now. I can connect you"
          " with someone who can help instead."
      )
    else:
      msg = (
          "I couldn't start the speed test right now. I can try again, or connect"
          " you with someone who can help."
      )
    return Content(parts=[Part(text=msg)])

  callback_context.state["speed_test_async_result_pending"] = "true"
  return Content(parts=[Part(text=(
      "The speed test has started. It may take a minute or so to complete — reply"
      " here in about a minute and I'll check the result."
  ))])


def _build_speed_test_result_reply(callback_context, result: Dict[str, Any]) -> Optional[Content]:
  """Deterministic 'Present Speed Test Results' step: card + summary + recommendation."""
  status = str(result.get("status", "")).lower()
  if status != "success":
    if str(result.get("reason", "")).lower() == "no_xbo":
      msg = (
          "A speed test isn't available on your account right now. I can connect you"
          " with someone who can help instead."
      )
    else:
      msg = (
          "I couldn't complete the speed test right now. I can try again, or connect"
          " you with someone who can help."
      )
    return Content(parts=[Part(text=msg)])

  card = _render_speed_test_card(callback_context, result)
  summary = str(result.get("summary", "")).strip()
  if str(result.get("recommendation", "")).lower() == "restart_gateway":
    callback_context.state["speed_test_restart_offer_pending"] = "true"
    rec_line = "Restarting your gateway should help bring your speeds back up. Want me to restart it now?"
  else:
    rec_line = "Everything's running the way it should. Anything else I can help with?"
  body = " ".join(p for p in (summary, rec_line) if p).strip()
  text = (card + ("\n\n" + body if body else "")).strip() if card else body
  return Content(parts=[Part(text=text)])


def _handle_speed_test_confirm(callback_context, user_text: str) -> Optional[Content]:
  """Deterministic 'Process Speed Test Confirmation' step (yes -> run)."""
  if not _speed_test_execution_enabled(callback_context):
    return _disabled_speed_test_response(callback_context)
  decision = _classify_outage_consent(user_text)  # accept/decline/agent/unclear
  print(f"[before_agent_callback] Deterministic speed-test confirm. decision={decision}")
  if decision == "decline":
    callback_context.state["speed_test_confirm_pending"] = "false"
    return Content(parts=[Part(text=(
        "No problem — if you change your mind, just let me know. Anything else I can help with?"
    ))])
  if decision == "agent":
    callback_context.state["speed_test_confirm_pending"] = "false"
    try:
      tools.transfer_potato_to_agent_v2({
          "task": "Connect the user to the live agent",
          "skill": "human",
          "key_events": "Customer requested a speed test, then asked for a person before running it.",
      })
    except Exception as e:
      print(f"[before_agent_callback] speed-test confirm human transfer failed: {e}")
    return Content(parts=[Part(text="Of course — let me connect you with someone who can help.")])
  if decision == "accept":
    callback_context.state["speed_test_confirm_pending"] = "false"
    # A REAL synchronous gateway speed test takes ~30s, but this callback runs in a
    # sandbox with a hard ~29s limit. Bound the call to a safe budget: if it can't
    # finish in time, degrade gracefully (honest message + keep the human handoff)
    # instead of letting the sandbox KeyboardInterrupt-kill the callback and emit
    # the generic "having trouble" fallback.
    completed, resp = _run_tool_with_budget(
        tools.perform_speed_test, {"device_id": ""},
        _SPEED_TOOL_BUDGET_S, "perform_speed_test",
    )
    if not completed:
      return Content(parts=[Part(text=(
          "I couldn't start the speed test right now. I can try again, or connect"
          " you with someone who can help."
      ))])
    return _build_speed_test_started_reply(callback_context, _parse_tool_dict(resp))
  # unclear -> keep pending, re-offer.
  return Content(parts=[Part(text=(
      "Just say the word and I'll start a quick speed test — it may take a minute or so. Want me to go ahead?"
  ))])


def _handle_speed_test_async_result_pending(callback_context) -> Optional[Content]:
  """Poll an async speed test started on a prior turn, then summarize once complete."""
  if not _speed_test_execution_enabled(callback_context):
    return _disabled_speed_test_response(callback_context)
  print("[before_agent_callback] Deterministic async speed-test result polling.")
  completed, resp = _run_tool_with_budget(
      tools.get_async_speed_test_result, {"execution_id": ""},
      _SPEED_TOOL_BUDGET_S, "get_async_speed_test_result",
  )
  if not completed:
    callback_context.state["speed_test_async_result_pending"] = "true"
    return Content(parts=[Part(text=(
        "The speed test is still taking a little longer than expected. Reply again"
        " in a bit and I'll check it for you."
    ))])
  result = _parse_tool_dict(resp)
  status = str(result.get("status", "")).lower()
  if status != "success":
    callback_context.state["speed_test_async_result_pending"] = "false"
    return Content(parts=[Part(text=(
        "I couldn't retrieve the speed test result right now. I can try again, or"
        " connect you with someone who can help."
    ))])
  if not bool(result.get("result_available")):
    callback_context.state["speed_test_async_result_pending"] = "true"
    return Content(parts=[Part(text=(
        "The speed test is still running. Reply again in a bit and I'll check the"
        " result for you."
    ))])
  callback_context.state["speed_test_async_result_pending"] = "false"
  return _build_speed_test_result_reply(callback_context, result)


def _handle_speed_test_restart_offer(callback_context, user_text: str) -> Optional[Content]:
  """Deterministic 'Process Speed Test Restart Offer' step (yes -> reboot)."""
  if not _speed_test_execution_enabled(callback_context):
    return _disabled_speed_test_response(callback_context)
  decision = _classify_outage_consent(user_text)
  print(f"[before_agent_callback] Deterministic speed-test restart offer. decision={decision}")
  if decision == "accept":
    callback_context.state["speed_test_restart_offer_pending"] = "false"
    # Same callback ~29s budget applies here: bound the reboot trigger so a slow
    # backend degrades into the existing "couldn't start restart" fallback instead
    # of a sandbox timeout.
    completed, resp = _run_tool_with_budget(
        tools.reboot, {"restart": False, "override_timeline_check": True},
        _SPEED_TOOL_BUDGET_S, "reboot",
    )
    if not completed:
      resp = {}
    r = _parse_tool_dict(resp)
    status = str(r.get("status", "")).lower()
    if status == "timeline_blocked":
      return Content(parts=[Part(text=(
          "It looks like your gateway was restarted recently within the last hour, so"
          " another restart won't help just yet. If your speeds are still off in a bit,"
          " let me know."
      ))])
    if status == "success":
      return Content(parts=[Part(text=(
          "Your gateway is restarting now — it takes about ten minutes. If you still see"
          " slow speeds after that, just let me know."
      ))])
    return Content(parts=[Part(text=(
        "I wasn't able to start the restart just now. Let me connect you with someone who can help."
    ))])
  if decision == "agent":
    callback_context.state["speed_test_restart_offer_pending"] = "false"
    try:
      tools.transfer_potato_to_agent_v2({
          "task": "Connect the user to the live agent",
          "skill": "human",
          "key_events": "Ran a gateway speed test; a restart was recommended. Customer requested a person.",
      })
    except Exception as e:
      print(f"[before_agent_callback] speed-test restart human transfer failed: {e}")
    return Content(parts=[Part(text="Of course — let me connect you with someone who can dig in further.")])
  if decision == "decline":
    callback_context.state["speed_test_restart_offer_pending"] = "false"
    return Content(parts=[Part(text=(
        "No problem. If your speeds keep bothering you, a restart is always here to try."
        " Anything else I can help with?"
    ))])
  # unclear
  return Content(parts=[Part(text=(
      "Would you like me to restart your gateway now? It takes about ten minutes and often helps with speed."
  ))])


def before_agent_callback(
    callback_context: CallbackContext,
) -> Optional[Content]:
  """Initializes session variables, executes concurrent pre-loading, and dispatches progress notifications in real-time."""
  # Robustly resolve and set the mandatory session trace identifier
  trace_id = str(
      callback_context.state.get("xa_session_id")
      or callback_context.state.get("xflow-traceid")
      or callback_context.state.get("convoy_session_id")
      or "E-sandbox-placeholder-session-id"
  )
  callback_context.state["xa_session_id"] = trace_id
  callback_context.state["xflow-traceid"] = trace_id

  # Set current datetime for scheduling context
  now = datetime.datetime.now()
  callback_context.state["current_datetime"] = now.strftime(
      "%A, %B %d, %Y, %I:%M %p"
  )
  callback_context.state["RDK_CURRENT_DATE_FORMATTED"] = now.strftime(
      "%m-%d-%Y"
  )

  # Per-turn guard used by after_model: once a human transfer is fired in this
  # turn, suppress any second-pass post-tool confirmation text so the customer
  # sees only one handoff line. Reset at turn start to avoid cross-turn bleed.
  callback_context.state["_suppress_post_transfer_llm_text"] = "false"

  # ---- Cross-domain Internet-topic reset (DeviceHelp→Internet / Video→Internet) ----
  # When the customer switches to an explicit Internet topic (e.g. "my internet is slow")
  # after a device-help session ({device_help_active}="true") or a video session
  # ({video_flow_active}="true"), stale cross-domain flags would otherwise cause the LLM
  # to skip a fresh diagnostic battery and jump straight to device tips (e.g. Samsung tips)
  # rather than running the standard internet priority rules.  Detect the switch here —
  # before the diagnostics_triggered guard — and clear the stale state so that guard is
  # skipped and the full internet battery runs cleanly on this turn.
  if (
      callback_context.state.get("diagnostics_triggered") == "true"
      and _is_explicit_internet_topic(_get_last_user_text(callback_context))
      and (
          str(callback_context.state.get("device_help_active", "")).lower() == "true"
          or str(callback_context.state.get("video_flow_active", "")).lower() == "true"
      )
  ):
    _xd_device = str(callback_context.state.get("device_help_active", "")).lower() == "true"
    _xd_video = str(callback_context.state.get("video_flow_active", "")).lower() == "true"
    print(
        "[before_agent_callback] Cross-domain Internet-topic reset:"
        f" device_help_active={_xd_device}, video_flow_active={_xd_video}."
        " Clearing stale cross-domain state and re-running internet diagnostic battery."
    )
    # Stale device-help flow
    callback_context.state["device_help_active"] = "false"
    callback_context.state["device_help_pending"] = "false"
    callback_context.state["device_help_steps_given"] = "0"
    callback_context.state["device_help_target"] = ""
    # Stale app-scope clarification
    callback_context.state["app_issue_reported"] = "false"
    callback_context.state["app_issue_target"] = ""
    callback_context.state["intent_clarification_pending"] = "false"
    callback_context.state["intent_clarified"] = "false"
    # Stale video conversation ownership
    callback_context.state["video_flow_active"] = "false"
    callback_context.state["video_issue_category"] = ""
    # Stale Video XIT / Convoy state — clear so the LLM doesn't act on a Video
    # XIT recommendation (e.g. truck_roll) on this fresh Internet turn, and so
    # the fresh Internet Convoy call drives the verdict cleanly.
    callback_context.state["video_xit_recommendation"] = ""
    callback_context.state["video_convoy_status"] = ""
    callback_context.state["video_appointment_transfer_data"] = ""
    callback_context.state["video_customer_message"] = ""
    callback_context.state["video_summary_fields"] = ""
    callback_context.state["convoy_status"] = ""
    callback_context.state["convoy_recommendations"] = ""
    # Reset diagnostics_triggered so the battery below re-runs; leave account_notified
    # intact to suppress a redundant "Account looks good" re-ping in the same session.
    callback_context.state["diagnostics_triggered"] = "false"
    callback_context.state["outage_notified"] = "false"
    callback_context.state["network_notified"] = "false"
    callback_context.state["gateway_notified"] = "false"
    # Fall through — diagnostics_triggered is now "false" so the guard below is skipped
    # and the full internet diagnostic battery runs on this turn.

  # Avoid executing the pre-loading routine multiple times
  if callback_context.state.get("diagnostics_triggered") == "true":
    # Handle convoy predictive_swap dispatch
    convoy_status_val = callback_context.state.get("convoy_status", "")
    if convoy_status_val == "predictive_swap":
      swap_msg = "Your gateway has a hardware fault — it's failing intermittently and a restart won't fix it. The fix is a replacement. You can get a new one shipped or swap at a store here: [Equipment Update](https://customer.xfinity.com/devices/equipment-update/new)."
      print(f"[before_agent_callback] Convoy predictive_swap dispatch handled in callback.")
      return Content(parts=[Part(text=swap_msg)])

    # If a transfer was already initiated (steering layer flags), intercept follow-up
    # queries to prevent the LLM from echoing in passthrough mode.
    # Respond contextually based on diagnostic state (mirrors Follow-Up Gate QUESTION logic).
    #
    # We also intercept when diagnostics ended in a terminal error state. In that case the
    # session has been escalated to a human and the LLM is unreliable on follow-up turns —
    # it tends to echo the user's input or return an empty response (see trace). A
    # deterministic contextual reply guarantees the customer always gets a response.
    escalate_flag = callback_context.state.get("escalate_to_human_flag", "off")
    fu_network_status = callback_context.state.get("network_status", "")
    fu_gateway_status = callback_context.state.get("gateway_status", "")
    fu_outage_status = callback_context.state.get("outage_status", "")
    terminal_error = (
        fu_network_status == "error"
        or fu_gateway_status == "error"
        or fu_outage_status == "error"
    )
    intercepted = (
        escalate_flag == "on"
        or callback_context.state.get("direct_response_mode") == "true"
        or terminal_error
    )
    # SPEED-TEST DETERMINISTIC HANDLER: In the intercepted (post-escalation)
    # state the orchestrator LLM is in passthrough and echoes the user, so the
    # speed-test flow CANNOT be deferred to it. Run the whole flow deterministically
    # here instead (offer -> confirm -> run -> results -> restart -> reboot),
    # mirroring the instruction's "Speed Test" subtask. Only engages while
    # intercepted; the healthy/non-escalated follow-up path is unchanged and still
    # handled by the LLM.
    if intercepted:
      user_text = _get_last_user_text(callback_context)
      if str(callback_context.state.get("speed_test_restart_offer_pending", "")).lower() == "true":
        return _handle_speed_test_restart_offer(callback_context, user_text)
      if str(callback_context.state.get("speed_test_async_result_pending", "")).lower() == "true":
        return _handle_speed_test_async_result_pending(callback_context)
      if str(callback_context.state.get("speed_test_confirm_pending", "")).lower() == "true":
        return _handle_speed_test_confirm(callback_context, user_text)
      if _is_speed_test_request(user_text):
        return _handle_speed_test_request(callback_context)
    if intercepted:
      print(
          "[before_agent_callback] Follow-up after transfer/terminal-error state."
          " Building contextual follow-up response from diagnostic state."
      )
      follow_up_msg = _build_followup_response(callback_context)
      # Re-emit the human transfer signal on follow-up turns for the escalation/
      # terminal-error path. Turn 1 fires transfer_potato (human) and sets the
      # escalate flag, but this branch previously returned text only — dropping the
      # transfer signal whenever steering re-invokes this agent on a follow-up turn.
      if escalate_flag == "on" or terminal_error:
        try:
          tools.transfer_potato_to_agent_v2({
              "task": "Connect the user to the live agent",
              "skill": "human",
              "key_events": (
                  "Diagnostics ended in an error/escalation state; re-emitting"
                  " live-agent transfer on follow-up turn."
              ),
          })
        except Exception as e:
          print(
              "[before_agent_callback] follow-up human transfer re-emit failed:"
              f" {e}"
          )
      return Content(parts=[Part(text=follow_up_msg)])

    return None

  # ---- Outage-inquiry: troubleshooting-consent follow-up turn ----
  # Set on a prior turn when the customer asked about an outage, none was found,
  # and we offered to run a connection check. Handle their yes/no here BEFORE the
  # full diagnostics path so we don't run all the checks unless they opt in.
  if str(callback_context.state.get("awaiting_outage_consent", "false")).lower() == "true":
    user_text = _get_last_user_text(callback_context)
    decision = _classify_outage_consent(user_text)
    print(
        "[before_agent_callback] Outage-inquiry consent follow-up."
        f" user_text={user_text!r} decision={decision}"
    )
    callback_context.state["awaiting_outage_consent"] = "false"

    if decision == "decline":
      callback_context.state["diagnostics_triggered"] = "true"
      callback_context.state["outage_inquiry_answered"] = "true"
      return Content(parts=[Part(
          text="No problem. Is there anything else I can help you with?"
      )])

    if decision == "agent":
      callback_context.state["diagnostics_triggered"] = "true"
      callback_context.state["outage_inquiry_answered"] = "true"
      try:
        tools.transfer_potato_to_agent_v2({
            "task": "Connect the user to the live agent",
            "skill": "human",
            "key_events": (
                "Customer asked about an area outage (none found), then requested"
                " a live agent."
            ),
        })
      except Exception as e:
        print(
            "[before_agent_callback] transfer_potato_to_agent_v2 failed (outage"
            f" consent agent): {e}"
        )
      return Content(parts=[Part(
          text="Sure — let me connect you with someone who can help."
      )])

    # "accept" or "unclear" -> proceed to the full diagnostics path below.
    print(
        "[before_agent_callback] Outage-inquiry consent granted (or unclear)."
        " Proceeding to full diagnostics."
    )
    # Fall through to the normal first-turn diagnostics flow.

  # ---- Streaming-service disambiguation follow-up ----
  # Set on the prior turn when a streaming-service complaint was ambiguous
  # (internet vs subscription). Handle the customer's yes/no BEFORE the full
  # diagnostics path so we only run the battery when it's needed.
  if str(callback_context.state.get("awaiting_streaming_disambig", "")).lower() == "true":
    user_text = _get_last_user_text(callback_context)
    decision = _classify_streaming_response(user_text)
    print(
        "[before_agent_callback] Streaming disambiguation follow-up."
        f" user_text={user_text!r} decision={decision}"
    )
    callback_context.state["awaiting_streaming_disambig"] = "false"

    if decision == "subscription":
      # Subscription/app issue → live agent (no diagnostic battery needed).
      callback_context.state["diagnostics_triggered"] = "true"
      callback_context.state["escalate_to_human_flag"] = "on"
      try:
        tools.transfer_potato_to_agent_v2({
            "task": "Connect the user to the live agent",
            "skill": "human",
            "key_events": (
                "Customer's streaming service issue is subscription/app-related"
                " (not an internet connectivity problem). Connecting to a live agent."
            ),
        })
      except Exception as e:
        print(
            "[before_agent_callback] transfer_potato_to_agent_v2 failed"
            f" (streaming subscription): {e}"
        )
      return Content(parts=[Part(
          text="Got it — let me connect you with someone who can help sort that out."
      )])

    # "internet" or "unclear" → fall through to the full internet battery.
    print(
        "[before_agent_callback] Streaming issue is internet-related or unclear;"
        " proceeding to full diagnostics."
    )

  # ---- Vague internet-opener clarification follow-up ----
  # Set on the prior turn when the customer's opener was a VAGUE internet
  # complaint (e.g. "my internet is broken", "it's been spotty") and we asked the
  # scope question ("nothing connecting / slow / certain rooms / one device or
  # app?"). Handle their answer here BEFORE the diagnostics path: a human request
  # transfers; a single-device answer sets the app-scope flags the existing
  # Priority 10 flow uses; everything else falls through to run the full battery.
  if str(callback_context.state.get("vague_clarification_pending", "")).lower() == "true":
    user_text = _get_last_user_text(callback_context)
    decision = _classify_vague_response(user_text)
    print(
        "[before_agent_callback] Vague-opener clarification follow-up."
        f" user_text={user_text!r} decision={decision}"
    )
    callback_context.state["vague_clarification_pending"] = "false"
    callback_context.state["intent_clarified"] = "true"

    if decision == "agent":
      callback_context.state["diagnostics_triggered"] = "true"
      callback_context.state["escalate_to_human_flag"] = "on"
      try:
        tools.transfer_potato_to_agent_v2({
            "task": "Connect the user to the live agent",
            "skill": "human",
            "key_events": (
                "Customer reported a vague internet issue. Asked the scope"
                " clarification question; customer requested a live agent"
                " instead of answering."
            ),
        })
      except Exception as e:
        print(
            "[before_agent_callback] transfer_potato_to_agent_v2 failed"
            f" (vague clarification agent): {e}"
        )
      return Content(parts=[Part(
          text="Of course — let me connect you with someone who can help."
      )])

    if decision == "one_device":
      # Only one device/app affected — record scope so the all-clear (Priority 10)
      # app-scope flow narrows correctly AFTER the full battery confirms healthy.
      callback_context.state["app_issue_reported"] = "true"
      callback_context.state["app_issue_target"] = _extract_vague_device_target(user_text)

    # "one_device" or "proceed" → fall through to the full internet battery so the
    # verdict is delivered this turn (diagnostics run synchronously below).
    print(
        "[before_agent_callback] Vague-opener answered; proceeding to full"
        f" diagnostics. app_issue_reported={callback_context.state.get('app_issue_reported')!r}"
    )

  # Telephony-provided billing account number
  account_number = (
      callback_context.state.get("accountNumber")
      or callback_context.state.get("account_id")
      or ""
  )
  if not account_number:
    print("[before_agent_callback] No account number found in state.")
    return None

  # ---- Self-serve how-to gate: skip the diagnostic battery ----
  # A self-serve how-to / settings question (reset/change/find the Wi-Fi name or
  # password, guest network, parental controls) is an INFORMATION request the
  # customer performs themselves -- NOT a broken-connection complaint. Running
  # the full diagnostic battery (outage/network/gateway + notifications) here is
  # unnecessary and confusing. Defer to the LLM so the instruction's "0a
  # SELF-SERVE HOW-TO" pre-check routes to Wi-Fi Settings Help. Leave
  # diagnostics_triggered UNSET so a later genuine connectivity complaint still
  # runs the checks. (Parallels the outage-inquiry deferral.)
  _last_user_text = _get_last_user_text(callback_context)
  if _is_selfserve_howto(_last_user_text):
    print(
        "[before_agent_callback] Self-serve how-to detected; skipping"
        " diagnostics and deferring to the instruction's 0a gate."
        f" msg={_last_user_text!r}"
    )
    return None

  # ---- Streaming-service disambiguation gate ----
  # When a customer mentions an Xfinity-supported streaming service (Apple TV,
  # Netflix, Peacock, etc.) with a complaint cue but WITHOUT clear internet words,
  # the intent is ambiguous: it could be a connectivity issue (→ Internet battery)
  # or a subscription/app issue (→ human agent). Ask ONE question before routing.
  # Mirrors the outage-inquiry consent pattern — leave diagnostics_triggered UNSET
  # so a later turn can run the full battery if the customer says "internet".
  if (
      _is_streaming_service_ambiguous(_last_user_text)
      and str(callback_context.state.get("awaiting_streaming_disambig", "")).lower() != "true"
  ):
    service_name = _get_streaming_service_display_name(_last_user_text)
    print(
        "[before_agent_callback] Streaming service query detected; asking"
        f" disambiguation question. service={service_name!r}"
        f" msg={_last_user_text!r}"
    )
    callback_context.state["awaiting_streaming_disambig"] = "true"
    return Content(parts=[Part(
        text=(
            f"I can help with that! Just to make sure I point you the right way —"
            f" are you having trouble connecting to the internet in general, or is it"
            f" specifically your {service_name} subscription or app that's not working?"
        )
    )])

  # ---- Video gate: run the SHARED checks, skip the INTERNET diagnostic battery ----
  # When Video is enabled and the latest message is a clear Video/TV/X1 problem, the
  # turn belongs to the Video specialist (instruction "0v VIDEO ROUTING"), which runs
  # AFTER this callback. We STILL run the shared context checks that the Video flow
  # needs and that customers expect on any turn -- account standing (posts the account
  # notification) and the outage check (posts the outage notification) -- but we SKIP
  # the Internet-specific diagnostic battery (Convoy Internet routing/dispatch, network
  # specialist, gateway RDK triage, and the "modem offline" notification), which does
  # not belong on a TV turn. We leave diagnostics_triggered UNSET so a later genuine
  # connectivity complaint still runs the full Internet sweep. A clear TV symptom wins
  # even if the customer also mentions internet -- the Video agent hands back to the
  # Internet flow if it turns out to be connectivity. Gated on video_flow_enabled so
  # with the flag off this is inert (today's behavior is unchanged).
  #
  # ALSO covers mid-video follow-up turns (video_flow_active == "true"): even when
  # the customer's follow-up text doesn't re-trigger _is_video_issue (e.g. a bare
  # "only Apple TV" or "only on that one"), the conversation is still owned by the
  # Video specialist. Without this, the full internet battery would re-run, Convoy
  # would fire the modem-offline rec, and before_agent_callback would dispatch the
  # "modem offline" technician message on what is still a Video-owned turn.
  _video_scoped_turn = _is_video_routed_turn(callback_context.state, _last_user_text)
  # Debug/routing indicator for Insights and callback-only guardrails.
  callback_context.state["flow_route_hint"] = "video" if _video_scoped_turn else "internet"
  if _video_scoped_turn:
    print(
        "[before_agent_callback] Video/TV issue detected (new issue or mid-video"
        " session); running account + outage checks (with notifications), then"
        " SKIPPING the internet diagnostics and deferring to the instruction's"
        f" 0v VIDEO ROUTING. msg={_last_user_text!r}"
        f" video_flow_active={callback_context.state.get('video_flow_active')!r}"
    )

  # ---- Vague internet-opener gate: ask ONE scope question before diagnostics ----
  # A vague opener ("my internet is broken", "it's been spotty", "I don't think I
  # can get online") does NOT establish scope (nothing connecting vs. slow vs.
  # certain rooms vs. one device/app). Running the full battery blindly is exactly
  # what product asked us to stop. Ask ONE clarifying question first, mirroring the
  # streaming/outage-consent pattern, and leave diagnostics_triggered UNSET so the
  # customer's next answer (handled at the top of this callback) runs the battery.
  # Runs AFTER the self-serve / streaming / video gates so those more-specific
  # intents win, and only on a fresh (non-video) turn.
  if (
      not _video_scoped_turn
      and str(callback_context.state.get("vague_clarification_pending", "")).lower() != "true"
      and _is_vague_internet_opener(_last_user_text)
  ):
    print(
        "[before_agent_callback] Vague internet opener detected; asking scope"
        f" clarification before diagnostics. msg={_last_user_text!r}"
    )
    callback_context.state["vague_clarification_pending"] = "true"
    return Content(parts=[Part(
        text=(
            "I can help with that. Before I run any checks — is nothing"
            " connecting at all, is everything just slow, does the Wi-Fi drop in"
            " certain rooms, or is it only one device or app?"
        )
    )])

  print(
      "[before_agent_callback] Starting real-time pre-loading for account:"
      f" {account_number}"
  )

  # Initialize core checklist tracking flags if not already present
  tracking_flags = [
      "account_notified",
      "outage_notified",
      "network_notified",
      "gateway_notified"
  ]
  for flag in tracking_flags:
    if flag not in callback_context.state:
      callback_context.state[flag] = "false"

  # Set default pending state variables
  callback_context.state["account_status"] = "PENDING_BACKEND_RESULT"
  callback_context.state["outage_status"] = "PENDING_BACKEND_RESULT"
  callback_context.state["network_status"] = "PENDING_BACKEND_RESULT"
  callback_context.state["gateway_status"] = "PENDING_BACKEND_RESULT"

  # 1. Fetch live customer context (MAC address) synchronously first
  cable_modem_mac = ""
  try:
    response = tools.fetch_customer_context({"account_number": account_number})
    res_dict = _safe_parse_to_dict(response)
    if res_dict.get("status") == "error":
      error_details = res_dict.get("error") or "Context Hub Error"
      print(f"[before_agent_callback] fetch_customer_context returned error: {error_details}")
      callback_context.state["gateway_status"] = "error"
      callback_context.state["outage_status"] = "error"
      callback_context.state["network_status"] = "error"
      callback_context.state["diagnostics_triggered"] = "true"
      try:
        tools.transfer_potato_to_agent_v2({
            "task": "Connect the user to the live agent",
            "skill": "human",
            "key_events": f"I just ran a few checks but wasn't able to get all the info I need. Let me get you to someone who can help.",
            "data": f"Account Number: {account_number}",
        })
      except Exception as transfer_err:
        print(f"[before_agent_callback] transfer_potato_to_agent_v2 failed: {transfer_err}")
      escalation_text = "I just ran a few checks but wasn't able to get all the info I need. Let me get you to someone who can help."
      return Content(parts=[Part(text=escalation_text)])

    # Empty / unavailable Context Hub payload (HTTP 200 with no usable
    # account/device context, OR a non-2xx upstream status). Either way this is
    # NOT a disconnected account, so DO NOT post the "Account is disconnected"
    # account-failure notification and DO NOT set account_status to "disconnected".
    # Mirror the soft error path: hand off to a human with a gentle "can't access
    # your account right now" message. The customer message NEVER references
    # Context Hub, HTTP status, or any exception -- that detail stays internal.
    if (
        res_dict.get("status") == "empty_context"
        or res_dict.get("customer_context_status") == "empty"
    ):
      print(
          "[before_agent_callback] Context Hub context unavailable (empty 200 or"
          " non-2xx). Treating as context-unavailable, NOT disconnected."
      )
      callback_context.state["customer_context_status"] = "empty"
      # Clear the pre-fetch PENDING placeholder to a neutral value so insights
      # never shows a stale PENDING_BACKEND_RESULT -- and it is explicitly NOT
      # "disconnected" (empty/unavailable context is not a disconnected account).
      callback_context.state["account_status"] = ""
      callback_context.state["gateway_status"] = "error"
      callback_context.state["outage_status"] = "error"
      callback_context.state["network_status"] = "error"
      callback_context.state["diagnostics_triggered"] = "true"
      empty_ctx_text = (
          "I'm unable to get information about your account right now. Let me"
          " connect you with someone who can help."
      )
      # Internal-only handoff context for the human agent (never shown to the
      # customer): include the upstream reason if the tool provided one.
      _ctx_reason = res_dict.get("error") or "empty/unavailable account context"
      try:
        tools.transfer_potato_to_agent_v2({
            "task": "Connect the user to the live agent",
            "skill": "human",
            "key_events": (
                "Unable to retrieve account context"
                f" ({_ctx_reason}). Diagnostics could not run."
            ),
            "data": f"Account Number: {account_number}",
        })
      except Exception as transfer_err:
        print(f"[before_agent_callback] transfer_potato_to_agent_v2 failed: {transfer_err}")
      return Content(parts=[Part(text=empty_ctx_text)])

    res = _unwrap_result(res_dict)

    # Double-gated extraction: try tool parameter first, then raw equipment context!
    cable_modem_mac = res.get("cable_modem_mac", "")
    if not cable_modem_mac or cable_modem_mac == "NOT_FOUND":
      equipment = res.get("deviceContext", {}).get("equipment", [])
      if not equipment and "result" in res_dict:
        equipment = (
            res_dict["result"].get("deviceContext", {}).get("equipment", [])
        )
      for device in equipment:
        if (
            device.get("deviceStatus") == "ACTIVE"
            and device.get("itemTypeCode") != "STB"
        ):
          mac = device.get("macaddress")
          if mac:
            cable_modem_mac = mac
            break

    print(
        f"[before_agent_callback] Context Hub returned MAC: '{cable_modem_mac}'"
    )
  except Exception as e:
    print(f"[before_agent_callback] fetch_customer_context failed: {e}")
    callback_context.state["gateway_status"] = "error"
    callback_context.state["outage_status"] = "error"
    callback_context.state["network_status"] = "error"
    callback_context.state["diagnostics_triggered"] = "true"
    try:
      tools.transfer_potato_to_agent_v2({
          "task": "Connect the user to the live agent",
          "skill": "human",
          "key_events": f"I just ran a few checks but wasn't able to get all the info I need. Let me get you to someone who can help.",
          "data": f"Account Number: {account_number}",
      })
    except Exception as transfer_err:
      print(f"[before_agent_callback] transfer_potato_to_agent_v2 failed: {transfer_err}")
    escalation_text = "I just ran a few checks but wasn't able to get all the info I need. Let me get you to someone who can help."
    return Content(parts=[Part(text=escalation_text)])

  # Safety Fallback
  if not cable_modem_mac or cable_modem_mac == "NOT_FOUND":
    session_mac = callback_context.state.get("cable_modem_mac", "")
    if session_mac:
      cable_modem_mac = session_mac
      print(
          "[before_agent_callback] Falling back to session MAC:"
          f" {cable_modem_mac}"
      )

  # Explicitly propagate core context variables
  callback_context.state["cable_modem_mac"] = cable_modem_mac or ""
  callback_context.state["accountNumber"] = account_number
  callback_context.state["account_id"] = account_number

  # Retrieve account status resolved dynamically from fetch_customer_context tool
  account_status = res.get("account_status", "A")

  # Instantly resolve & notify Account Standing checkbox
  if account_status in ["D", "S", "C"]:
    # Retrieve human-readable status name for logging/verbal responses
    status_names = {
        "S": "Suspended",
        "D": "Disconnected",
        "C": "Pending Activation",
    }
    reason = status_names.get(account_status, "Restricted Standing")
    #Need to take a look for other statuses apart from 3
    mapped_status = (
        "suspended"
        if account_status == "S"
        else ("disconnected" if account_status == "D" else "pending activation")
    )

    callback_context.state["account_status"] = mapped_status
    if callback_context.state["account_notified"] == "false":
      try:
        tools.post_user_notification({
            "tool": "account",
            "status": "failure",
            "text": "Account is " + str(mapped_status),
            "calling_agent": "repair_orchestration_agent",
            "calling_tool": "before_agent_callback",
        })
        print(
            "[before_agent_callback] posted notification with Account issue found."
        )
        callback_context.state["account_notified"] = "true"
      except Exception as e:
        print(
            "[before_agent_callback] Failed to post account failure"
            f" notification: {e}"
        )

    print(
        f"[before_agent_callback] Restricted standing '{reason}'. Synchronously"
        " executing Billing live transfer."
    )
    try:
      tools.transfer_potato_to_agent_v2({
          "task": "Connect the user to the live agent",
          "skill": "human",
          "key_events": (
              f"I found a few issues with your account. Let me connect you with someone who can help resolve this."
          ),
          "data": f"Account Number: {account_number}",
      })
    except Exception as e:
      print(f"[before_agent_callback] transfer_potato_to_agent_v2 failed: {e}")

    escalation_text = (
        f"I found a few issues with your account. Let me connect you with someone who can help resolve this."
    )
    return Content(parts=[Part(text=escalation_text)])

  else:
    callback_context.state["account_status"] = "clear"
    if callback_context.state["account_notified"] == "false":
      try:
        tools.post_user_notification({
            "tool": "account",
            "status": "success",
            "text": "Account looks good",
            "calling_agent": "repair_orchestration_agent",
            "calling_tool": "before_agent_callback",
        })
        callback_context.state["account_notified"] = "true"
      except Exception as e:
        print(
            "[before_agent_callback] Failed to post account success"
            f" notification: {e}"
        )

  # Bifurcate tasks based on MAC availability (MAC-Less Resilient Flow)
  has_mac = bool(cable_modem_mac and cable_modem_mac != "NOT_FOUND")

  # Define background tasks dict
  tasks = {}

  with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
    # 1. Run Outage Check first
    print("[before_agent_callback] Triggering Outage check...")
    tasks[
        executor.submit(tools.check_outage, {"account_number": account_number})
    ] = "outage"

    start_wait_time = time.time()
    try:
      for future in concurrent.futures.as_completed(tasks.keys(), timeout=20.0):
        task_name = tasks[future]
        elapsed = time.time() - start_wait_time
        print(
            f"[before_agent_callback] Task '{task_name}' finished after"
            f" {elapsed:.2f}s"
        )
        try:
          result = future.result()
          if task_name == "outage":
            res_dict = _safe_parse_to_dict(result)
            if res_dict.get("status") == "error":
              print(f"[before_agent_callback] Outage task returned error: {res_dict.get('error')}")
              callback_context.state["outage_status"] = "error"
              # Clear stale outage detail fields so a prior turn's outage
              # message does not linger while outage_status is "error".
              callback_context.state["outage_detected"] = "false"
              callback_context.state["outage_message"] = ""
              callback_context.state["customer_message"] = ""
              callback_context.state["impacted_services"] = ""
              outage_detected = False
            else:
              res = _unwrap_result(res_dict)
              outage_detected = res.get("outage_detected", False)
              outage_status = "active" if outage_detected else "none"

              # Explicitly commit to memory state to prevent concurrent lost updates
              callback_context.state["outage_status"] = outage_status
              # Propagate the outage detail fields from the tool return into
              # persistent session state. The check_outage tool writes these to
              # its own context.state, which does not always persist to the
              # session the LLM reads; committing them here from the return
              # value guarantees {outage_message}/{customer_message} are set.
              _svcs = res.get("impacted_services", "")
              if isinstance(_svcs, list):
                _svcs = ",".join(_svcs)
              callback_context.state["outage_detected"] = "true" if outage_detected else "false"
              callback_context.state["outage_message"] = res.get("outage_message", "") or ""
              callback_context.state["customer_message"] = res.get("customer_message", "") or ""
              callback_context.state["impacted_services"] = _svcs or ""
              if callback_context.state["outage_notified"] == "false":
                if outage_status in ("active", "degradation"):
                  tools.post_user_notification({
                      "tool": "outage",
                      "status": "failure",
                      "text": "Service outage found in your neighborhood",
                      "calling_agent": "repair_orchestration_agent",
                      "calling_tool": "before_agent_callback",
                  })
                else:
                  tools.post_user_notification({
                      "tool": "outage",
                      "status": "success",
                      "text": "No service outages in your area",
                      "calling_agent": "repair_orchestration_agent",
                      "calling_tool": "before_agent_callback",
                  })
                callback_context.state["outage_notified"] = "true"
        except Exception as task_err:
          print(
              "[before_agent_callback] Error parsing completed task"
              f" '{task_name}': {task_err}"
          )
          if task_name == "outage":
            callback_context.state["outage_status"] = "error"
            # Clear stale outage detail fields on parse/exception failure.
            callback_context.state["outage_detected"] = "false"
            callback_context.state["outage_message"] = ""
            callback_context.state["customer_message"] = ""
            callback_context.state["impacted_services"] = ""
            outage_detected = False
    except concurrent.futures.TimeoutError:
      print("[before_agent_callback] Timeout waiting for outage task.")
      # Outage check did not complete: mark error and clear stale detail fields.
      callback_context.state["outage_status"] = "error"
      callback_context.state["outage_detected"] = "false"
      callback_context.state["outage_message"] = ""
      callback_context.state["customer_message"] = ""
      callback_context.state["impacted_services"] = ""
      outage_detected = False

    # ---- Outage-status inquiry with NO outage: answer conversationally and
    # offer troubleshooting instead of running the full diagnostic battery. ----
    if not outage_detected:
      _last_user_text = _get_last_user_text(callback_context)
      if _is_outage_inquiry(_last_user_text):
        print(
            "[before_agent_callback] Outage-status inquiry detected with no"
            " outage. Offering a connection check instead of running full"
            f" diagnostics. msg={_last_user_text!r}"
        )
        callback_context.state["outage_status"] = "none"
        callback_context.state["network_status"] = "skipped"
        callback_context.state["gateway_status"] = "skipped"
        callback_context.state["convoy_status"] = "skipped"
        # Wait for the customer's yes/no before running diagnostics. Leave
        # diagnostics_triggered unset so a "yes" re-enters this callback and
        # runs the full checks.
        callback_context.state["awaiting_outage_consent"] = "true"
        return Content(parts=[Part(
            text=(
                "Good news — I'm not seeing any outages in your area right now."
                " Would you like me to run a quick check on your connection to"
                " see what might be going on?"
            )
        )])

    if outage_detected:
      print(
          "[before_agent_callback] Outage detected. Stopping further"
          " troubleshooting."
      )
      callback_context.state["convoy_status"] = "skipped"
      callback_context.state["network_status"] = "skipped"
      callback_context.state["gateway_status"] = "skipped"
      callback_context.state["diagnostics_triggered"] = "true"
      return None

    # ---- Video-scoped turn: account + outage are done (and notified). SKIP the
    # Internet diagnostic battery (Convoy Internet routing/dispatch, network
    # specialist, gateway RDK triage, and the "modem offline" notification) and
    # defer to the instruction's 0v VIDEO ROUTING -> video_specialist_agent, which
    # runs its OWN scoped Video checks (including the video XIT recommendation via a
    # cache-first check_convoy_recommendations call). Leave diagnostics_triggered
    # UNSET so a later genuine Internet complaint still runs the full sweep. ----
    if _video_scoped_turn:
      # Deterministically prefetch once so first-turn Video XIT state is ready even
      # if the model delays the instruction-level cache-first call.
      _prefetch_video_xit_if_needed(callback_context, account_number)
      callback_context.state["network_status"] = "skipped"
      callback_context.state["gateway_status"] = "skipped"
      callback_context.state["convoy_status"] = "skipped"
      print(
          "[before_agent_callback] Video turn: account + outage checked/notified;"
          " skipping internet diagnostics, deferring to 0v VIDEO ROUTING."
      )
      return None

    # ---- Video→Internet cross-domain cleanup (fresh-session path) ----
    # Fires when the customer switches from a Video session to an Internet topic in
    # the SAME run of the battery (diagnostics_triggered was "false" so the reset
    # block above did not apply).  The Video specialist may have left
    # video_xit_recommendation / video_convoy_status ("truck_roll") in state.
    # If not cleared, the Convoy tool's direct context.state write during the battery
    # below would produce a "truck_roll" convoy_status that immediately dispatches
    # to the appointment agent — bypassing the troubleshooting summary flow.
    # Clearing the Video XIT variables here ensures the Internet battery's fresh
    # Convoy result is the ONLY signal that drives routing, and the LLM receives a
    # clean state to evaluate the standard internet priority rules.
    if (
        not _video_scoped_turn
        and _is_explicit_internet_topic(_last_user_text)
        and (
            str(callback_context.state.get("video_xit_recommendation", "")).strip()
            or str(callback_context.state.get("video_convoy_status", "")).strip()
        )
    ):
      print(
          "[before_agent_callback] Video→Internet cross-domain cleanup:"
          " clearing stale Video XIT / convoy state before Internet battery."
          f" video_convoy_status={callback_context.state.get('video_convoy_status')!r}"
      )
      callback_context.state["video_xit_recommendation"] = ""
      callback_context.state["video_convoy_status"] = ""
      callback_context.state["video_appointment_transfer_data"] = ""
      callback_context.state["video_customer_message"] = ""
      callback_context.state["video_summary_fields"] = ""
      callback_context.state["convoy_status"] = ""
      callback_context.state["convoy_recommendations"] = ""
      callback_context.state["video_flow_active"] = "false"
      callback_context.state["video_issue_category"] = ""

    # 2. No Outage -> Run Convoy
    print(
        "[before_agent_callback] No outage detected. Triggering Convoy"
        " recommendations..."
    )
    tasks = {}  # reset tasks dict
    tasks[
        executor.submit(
            tools.check_convoy_recommendations,
            {"account_number": account_number},
        )
    ] = "convoy"

    if has_mac:
      print(
          "[before_agent_callback] Triggering full concurrent diagnostics for"
          f" MAC: {cable_modem_mac}"
      )
      print(
          "[before_agent_callback] Long running operations to be handled by"
          " subagents..."
      )
    else:
      print(
          "[before_agent_callback] MAC missing. Instantly resolving and"
          " notifying offline hardware components."
      )
      # Resolve and notify MAC-dependent statuses immediately on Turn 1
      callback_context.state["network_status"] = "healthy"
      callback_context.state["gateway_status"] = "offline"

      if callback_context.state["gateway_notified"] == "false":
        try:
          tools.post_user_notification({
              "tool": "device",
              "status": "failure",
                "text": "Modem is offline or disconnected, please check connection",
              "calling_agent": "repair_orchestration_agent",
              "calling_tool": "before_agent_callback",
          })
          callback_context.state["gateway_notified"] = "true"
        except Exception as e:
          print(
              "[before_agent_callback] Failed to post instant gateway offline"
              f" notification: {e}"
          )

    # Monitor parallel futures in real-time as they complete (up to 20.0 seconds)
    start_wait_time = time.time()
    try:
      for future in concurrent.futures.as_completed(tasks.keys(), timeout=20.0):
        task_name = tasks[future]
        elapsed = time.time() - start_wait_time
        print(
            f"[before_agent_callback] Task '{task_name}' finished after"
            f" {elapsed:.2f}s"
        )

        try:
          result = future.result()

          # Parse Convoy Task
          if task_name == "convoy":
            # The wrapper already returns a clean dict, but we pass it through safety parsers just in case
            res_dict = _safe_parse_to_dict(result)
            res = _unwrap_result(res_dict)

            # If the tool framework stripped the dict, fallback to the raw result
            if "routing_action" not in res and "routing_action" in res_dict:
              res = res_dict

            status = res.get("status", "error")
            routing_action = res.get("routing_action", "none")

            if status == "success":
              # The tool writes {convoy_summary_fields} to its OWN context.state, whose
              # delta is NOT visible to this callback's state during the same turn — so
              # the XIT dispatch below used to read '' and render no card. Mirror it off
              # the tool's RETURN value, exactly as we do for description/ticket fields.
              if "summary_fields" in res:
                _sf = res.get("summary_fields") or {}
                callback_context.state["convoy_summary_fields"] = (
                    json.dumps(_sf) if isinstance(_sf, dict) and _sf else ""
                )
              # Ensure main-thread state is updated based on Convoy's predictive routing
              if routing_action == "swap":
                callback_context.state["gateway_status"] = "swap"
                callback_context.state["convoy_status"] = "predictive_swap"
                callback_context.state["network_status"] = "skipped"
              elif routing_action == "predictive_swap":
                callback_context.state["gateway_status"] = "predictive_swap"
                callback_context.state["convoy_status"] = "predictive_swap"
                callback_context.state["network_status"] = "skipped"
              elif routing_action == "technician":
                callback_context.state["network_status"] = "impaired"
                callback_context.state["convoy_status"] = (
                    "predictive_impairment"
                )
                callback_context.state["gateway_status"] = "skipped"
                callback_context.state["network_status"] = "skipped"
                # Extract transfer values from first repair recommendation
                repair_recs = res.get("repair_recommendations", [])
                if repair_recs and isinstance(repair_recs, list):
                  first_rec = repair_recs[0]
                  callback_context.state["activityType"] = first_rec.get("activity_type", "TROUBLE_CALL")
                  callback_context.state["activityCode"] = first_rec.get("activity_code", "")
                  callback_context.state["jobType"] = first_rec.get("job_type", "")
                  # Propagated the same way as activityCode/jobType — empty when absent.
                  callback_context.state["problemCode"] = first_rec.get("problem_code", "")
                  callback_context.state["intents"] = first_rec.get("intents", "")
                  # Also extract customer message
                  desc = first_rec.get("description", "")
                  if desc:
                    callback_context.state["convoy_customer_message"] = desc
              elif routing_action == "device_offline":
                callback_context.state["convoy_status"] = "predictive_offline"
                callback_context.state["gateway_status"] = "skipped"
                callback_context.state["network_status"] = "skipped"
              else:
                callback_context.state["convoy_status"] = "clear"

              # Log Convoy Predictive Recommendations directly to stdout (not visual UI cards)
              if (
                  callback_context.state.get("convoy_notified", "false")
                  == "false"
              ):
                if routing_action in ("swap", "predictive_swap"):
                  print(
                      "[before_agent_callback] [CONVOY PREDICTION] swap ->"
                      " Predictive AI recommends immediate gateway replacement"
                  )
                elif routing_action == "technician":
                  print(
                      "[before_agent_callback] [CONVOY PREDICTION] technician"
                      " -> Predictive AI detected network impairment requiring"
                      " a technician"
                  )
                elif routing_action == "device_offline":
                  print(
                      "[before_agent_callback] [CONVOY PREDICTION]"
                      " device_offline -> Predictive AI detected persistent"
                      " offline telemetry"
                  )
                else:
                  print(
                      "[before_agent_callback] [CONVOY PREDICTION] none ->"
                      " Predictive AI analysis found no imminent"
                      " hardware/network failures"
                  )
                callback_context.state["convoy_notified"] = "true"
            else:
              print(
                  "[before_agent_callback] Convoy task returned an error:"
                  f" {res.get('error', 'Unknown Error')}"
              )
              callback_context.state["convoy_status"] = "error"

          # Parse Network Task
          elif task_name == "network":
            res_dict = _safe_parse_to_dict(result)
            res = _unwrap_result(res_dict)
            inner_data = {}
            a2a_text = ""
            msg = res.get("message", {})
            if isinstance(msg, dict):
              parts = msg.get("parts", [])
              if (
                  parts
                  and isinstance(parts, list)
                  and isinstance(parts[0], dict)
              ):
                a2a_text = parts[0].get("text", "")
            if not a2a_text:
              result_obj = res.get("result", {})
              if isinstance(result_obj, dict):
                msg = result_obj.get("message", {})
                if isinstance(msg, dict):
                  parts = msg.get("parts", [])
                  if (
                      parts
                      and isinstance(parts, list)
                      and isinstance(parts[0], dict)
                  ):
                    a2a_text = parts[0].get("text", "")
            if a2a_text:
              cleaned_text = a2a_text.strip()
              if cleaned_text.startswith("```json"):
                cleaned_text = cleaned_text[7:]
              if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text[:-3]
              cleaned_text = cleaned_text.strip()
              try:
                inner_data = json.loads(cleaned_text)
              except json.JSONDecodeError:
                # The specialist sometimes appends trailing prose (e.g. a
                # "## Tool Calls Summary" block) after the JSON. Fall back to the
                # first balanced {...} object so the recommendation still parses.
                candidate = _find_balanced_json(cleaned_text) or _find_balanced_json(a2a_text)
                try:
                  inner_data = json.loads(candidate) if candidate else {}
                except json.JSONDecodeError:
                  inner_data = {}

            network_status = "healthy"
            tech_type = _extract_technician_type(inner_data)
            if tech_type.lower() in (
                "network tech",
                "install and repair tech",
            ):
              network_status = "impaired"

            # Deterministically set transfer values based on technician type.
            # Network Tech is a LIVE-HUMAN handoff and needs no appointment ticket
            # data — record only the technician_type signal. Everything else fills
            # the appointment dispatch codes.
            matched_activity_type = ""
            matched_activity_code = ""
            matched_job_type = ""
            matched_problem_code = ""
            matched_intents = ""
            is_network_tech = tech_type.lower() == "network tech"

            if is_network_tech:
              callback_context.state["technician_type"] = "network_tech"
            else:
              if tech_type.lower() == "install and repair tech":
                matched_activity_type = "TROUBLE_CALL"
                matched_activity_code = "H3"
                matched_job_type = "AO"
                matched_problem_code = "88653"
                matched_intents = "INTERNET_REPAIR"
                callback_context.state["technician_type"] = "install_repair_tech"

              # Apply defaults if not matched (same pattern as check_convoy_recommendations)
              if not matched_activity_code:
                matched_activity_code = "H2"
              if not matched_job_type:
                matched_job_type = "Test"
              if not matched_activity_type:
                matched_activity_type = "TROUBLE_CALL"

              callback_context.state["activityCode"] = matched_activity_code
              callback_context.state["jobType"] = matched_job_type
              callback_context.state["activityType"] = matched_activity_type
              # Optional appointment-routing attributes — install_repair maps to
              # 88653/INTERNET_REPAIR. Only write when this network recommendation
              # produced them; otherwise preserve any values already set upstream
              # (e.g. from a convoy/cara recommendation) instead of clobbering.
              if matched_problem_code:
                callback_context.state["problemCode"] = matched_problem_code
              if matched_intents:
                callback_context.state["intents"] = matched_intents
            print(
                "[before_agent_callback] Propagated network transfer values:"
                f" activityCode='{matched_activity_code}',"
                f" jobType='{matched_job_type}',"
                f" activityType='{matched_activity_type}'"
            )

            callback_context.state["network_status"] = network_status
            if callback_context.state["network_notified"] == "false":
              if network_status == "impaired":
                tools.post_user_notification({
                    "tool": "network",
                    "status": "failure",
                    "text": "Network issue may be affecting your service",
                    "calling_agent": "repair_orchestration_agent",
                    "calling_tool": "before_agent_callback",
                })
              else:
                tools.post_user_notification({
                    "tool": "network",
                    "status": "success",
                    "text": "Network is stable",
                    "calling_agent": "repair_orchestration_agent",
                    "calling_tool": "before_agent_callback",
                })
              callback_context.state["network_notified"] = "true"

          # Parse Device Triage Task
          elif task_name == "triage":
            res_dict = _safe_parse_to_dict(result)
            res = _unwrap_result(res_dict)
            content_items = res.get("content", [])
            content_text = "".join([
                item.get("text", "")
                for item in content_items
                if isinstance(item, dict)
            ]).lower()

            gateway_status = "healthy"
            if "reboot" in content_text or "restart" in content_text:
              gateway_status = "reboot"
            elif "swap" in content_text or "replace" in content_text:
              gateway_status = "swap"
            elif "offline" in content_text or "no telemetry" in content_text:
              gateway_status = "offline"

            callback_context.state["gateway_status"] = gateway_status
            if callback_context.state["gateway_notified"] == "false":
              if gateway_status == "reboot":
                tools.post_user_notification({
                    "tool": "device",
                    "status": "failure",
                    "text": "Modem is offline or disconnected",
                    "calling_agent": "repair_orchestration_agent",
                    "calling_tool": "before_agent_callback",
                })
              elif gateway_status == "swap":
                tools.post_user_notification({
                    "tool": "device",
                    "status": "failure",
                    "text": "Modem has a hardware issue, replacement needed",
                    "calling_agent": "repair_orchestration_agent",
                    "calling_tool": "before_agent_callback",
                })
              elif gateway_status == "offline":
                tools.post_user_notification({
                    "tool": "device",
                    "status": "failure",
                    "text": "Modem is offline or disconnected",
                    "calling_agent": "repair_orchestration_agent",
                    "calling_tool": "before_agent_callback",
                })
              else:
                tools.post_user_notification({
                    "tool": "device",
                    "status": "success",
                    "text": "Modem is connected",
                    "calling_agent": "repair_orchestration_agent",
                    "calling_tool": "before_agent_callback",
                })
              callback_context.state["gateway_notified"] = "true"

        except Exception as task_err:
          print(
              "[before_agent_callback] Error parsing completed task"
              f" '{task_name}': {task_err}"
          )
          if task_name == "outage":
            callback_context.state["outage_status"] = "error"
          elif task_name == "convoy":
            callback_context.state["convoy_status"] = "error"
          elif task_name == "network":
            callback_context.state["network_status"] = "error"
          elif task_name == "triage":
            callback_context.state["gateway_status"] = "error"

    except concurrent.futures.TimeoutError:
      print(
          "[before_agent_callback] Timeout waiting for concurrent tasks. Some"
          " diagnostics remain pending."
      )

  # Mark pre-loading as triggered successfully
  callback_context.state["diagnostics_triggered"] = "true"
  print("[before_agent_callback] Real-time concurrent pre-loading completed.")

  # Handle Convoy predictive_swap dispatch directly in callback
  convoy_status_val = callback_context.state.get("convoy_status", "")
  if convoy_status_val == "predictive_swap":
    swap_msg = "Your gateway has a hardware fault — it's failing intermittently and a restart won't fix it. The fix is a replacement. You can get a new one shipped or swap at a store here: [Equipment Update](https://customer.xfinity.com/devices/equipment-update/new)."
    # Authored headline wins when present; otherwise keep the existing swap message
    # (which carries the Equipment Update link). Card is ADDITIVE on top.
    _swap_fields = _xit_summary_fields(callback_context)
    swap_msg = _swap_fields.pop("headline", "") or swap_msg
    swap_msg = _prepend_summary_card(callback_context, _swap_fields, swap_msg, _CARD_LABEL_ALERTS)
    print(f"[before_agent_callback] Convoy predictive_swap dispatch handled in callback.")
    return Content(parts=[Part(text=swap_msg)])

  # Handle Convoy technician dispatch directly in callback
  # (LLM cannot reliably output text + tool calls in the same turn)
  if convoy_status_val in ("predictive_impairment", "truck_roll"):
    # Safety guard: when this turn is classified as Video, NEVER let the
    # callback-level Internet convoy dispatch preempt 0v routing.
    if _should_skip_internet_convoy_dispatch(callback_context.state):
      print(
          "[before_agent_callback] Skipping callback Internet convoy dispatch on"
          " video-routed turn; deferring to 0v/video_specialist_agent."
      )
      return None
    convoy_msg = callback_context.state.get("convoy_customer_message", "")
    fallback_msg = "We found an issue with the connection to your home. A technician will take a closer look, and depending on the type of issue found, a service charge may apply."
    # The product-authored HEADLINE replaces the raw adkCustomerMessage as the visible
    # message when this recommendation has authored copy. Recs WITHOUT authored copy keep
    # today's behaviour exactly: adkCustomerMessage verbatim (then the generic fallback).
    _xit_fields = _xit_summary_fields(callback_context)
    _headline = _xit_fields.pop("headline", "")
    display_msg = _headline or convoy_msg or fallback_msg
    # ADDITIVE: the four-part explanation goes INSIDE the collapsible card, above the
    # headline. No card copy / voice surface -> the message is returned unchanged.
    # XIT/Convoy alerts use the "View alert summary" label so they are distinguishable
    # from the Connect/RDK diagnostics "View troubleshooting summary" card.
    display_msg = _prepend_summary_card(callback_context, _xit_fields, display_msg, _CARD_LABEL_ALERTS)


    # Execute transfer directly from callback
    activity_type = callback_context.state.get("activityType", "TROUBLE_CALL")
    activity_code = callback_context.state.get("activityCode", "")
    job_type = callback_context.state.get("jobType", "")
    transfer_data = _build_appointment_transfer_data(callback_context)
    try:
      tools.transfer_potato_to_agent_v2({
          "task": "Convoy predictive analysis identified a network issue requiring technician dispatch.",
          "skill": "appointment",
          "key_events": "Ran outage check (no outage), convoy predictive analysis identified technician dispatch recommendation.",
          "data": transfer_data,
      })
    except Exception as e:
      print(f"[before_agent_callback] transfer_potato_to_agent_v2 failed: {e}")

    # Return Content as the agent's spoken text response to the customer
    print(f"[before_agent_callback] Convoy technician dispatch handled in callback. Message: '{display_msg}'")
    return Content(parts=[Part(text=display_msg)])

  # Handle Network impairment dispatch directly in callback
  # (same pattern as convoy — LLM cannot reliably output text + tool calls in the same turn)
  network_status_val = callback_context.state.get("network_status", "")
  if network_status_val == "impaired":
    # A "Network Tech" recommendation is a LIVE-HUMAN handoff (skill=human) and
    # carries NO appointment ticket data. Everything else stays on the
    # appointment flow. technician_type is the single routing signal (set
    # deterministically upstream by the network specialist callback).
    tech_type_val = callback_context.state.get("technician_type", "")
    is_network_tech = tech_type_val == "network_tech"
    # "network_tech" → no service charge language (network-side issue, not customer's fault)
    # "install_repair_tech" or default → include service charge language
    if is_network_tech:
      network_msg = "It looks like there's a problem with the network signal going to your home. Let me connect you with someone to get this sorted out for you."
    else:
      network_msg = "We found an issue with the connection to your home. A technician will take a closer look, and depending on the type of issue found, a service charge may apply."

    if is_network_tech:
      # Network-side signal fault: hand off to a live human agent (NOT the
      # appointment/truck-roll flow). Like any other human transfer it needs no
      # ticket data. Escalate so follow-up turns are intercepted.
      callback_context.state["technician_type"] = "network_tech"
      callback_context.state["escalate_to_human_flag"] = "on"
      try:
        tools.transfer_potato_to_agent_v2({
            "task": "Connect the user to the live agent",
            "skill": "human",
            "key_events": "Ran outage check (no outage) and network diagnostics; found a signal issue with the connection to the customer's home requiring a network technician. Connecting the customer with a live agent.",
        })
      except Exception as e:
        print(f"[before_agent_callback] transfer_potato_to_agent_v2 (human) failed for network: {e}")
    else:
      # Install & Repair (or default) -> appointment/truck-roll dispatch.
      transfer_data = _build_appointment_transfer_data(callback_context)
      try:
        tools.transfer_potato_to_agent_v2({
            "task": "Network specialist analysis identified a line signal issue requiring technician dispatch.",
            "skill": "appointment",
            "key_events": "Ran outage check (no outage), network specialist analysis identified impaired line signals requiring technician dispatch.",
            "data": transfer_data,
        })
      except Exception as e:
        print(f"[before_agent_callback] transfer_potato_to_agent_v2 failed for network: {e}")

    # Return Content as the agent's spoken text response to the customer
    print(f"[before_agent_callback] Network impairment dispatch handled in callback. Message: '{network_msg}'")
    return Content(parts=[Part(text=network_msg)])

  # Handle diagnostic error states deterministically in callback
  # (Priority 9 equivalent — LLM sometimes fails to output text + tool in same turn, causing silent responses)
  outage_status_val = callback_context.state.get("outage_status", "")
  gateway_status_val = callback_context.state.get("gateway_status", "")
  # Only trigger if no higher-priority actionable state exists (reboot, swap, etc.)
  has_actionable_gateway = gateway_status_val in ("reboot", "swap", "no_telemetry", "unsupported_device")
  has_actionable_convoy = convoy_status_val in ("predictive_swap", "predictive_offline")

  if not has_actionable_gateway and not has_actionable_convoy:
    if network_status_val == "error" or gateway_status_val == "error" or outage_status_val == "error":
      error_msg = "I just ran a few checks but wasn't able to get all the info I need. Let me get you to someone who can help."
      # Mark the session as escalated so subsequent follow-up turns are intercepted
      # deterministically (the LLM is unreliable once escalated).
      callback_context.state["escalate_to_human_flag"] = "on"
      try:
        tools.transfer_potato_to_agent_v2({
            "task": "Diagnostic tools returned incomplete/error results. Transfer the consumer to a live agent.",
            "skill": "human",
            "key_events": "Ran outage check (no outage). Advanced diagnostics partially failed — some tools returned errors or no data. Unable to determine root cause automatically.",
        })
      except Exception as e:
        print(f"[before_agent_callback] transfer_potato_to_agent_v2 failed for error state: {e}")

      # Prepend the approved "View troubleshooting summary" card (grounded in the
      # statuses already collected) so network/gateway(RDK)/outage FAILURES still
      # show the summary. The transfer + message above are unchanged; card render
      # is fully guarded and never blocks the verdict.
      error_msg = _prepend_summary_card(
          callback_context, _build_error_summary_fields(callback_context), error_msg
      )
      print(f"[before_agent_callback] Diagnostic error dispatch handled in callback.")
      return Content(parts=[Part(text=error_msg)])

  return None
