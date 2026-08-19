"""Offline proof that the Intent Clarification Gate classifies without an LLM.

Drives the EMITTED config with real caller utterances and reports what the engine
resolved by cue matching alone. Nothing here calls a model.

    python clarify_check.py [--app-dir ./built]

Note the harness detail this depends on: the engine only lets every `option_cues` slot
fill on the first in-flow turn, and that "first turn" test derives from
`scanned_user_text`, which `flows.engine.loader.run_engine` does NOT pass but the live
`before_model` always does. Driving through `run_engine` would therefore show the gate
silently never firing. This calls the engine directly with the scalar, matching live.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths()

from flows.engine import loader as fb  # noqa: E402

fb.set_framework_root(labs_paths.framework_root())

# The caller has an account and the sweep has already run clean, so the ONLY thing that
# can vary is the clarification gate.
SWEPT = dict(diagnostics_complete="true", reboot_answer_allowed="true",
             account_status="clear", outage_status="none", convoy_status="clear",
             network_status="healthy", gateway_status="healthy", wifi_status="healthy",
             cable_modem_mac="AA:BB", device_id="AA:BB",
             accountNumber="8344200010126021", active_flow="repair")

# (utterance, expected complaint_scope, expected app_name or None)
OPENINGS = [
    ("My Netflix doesn't work", "app_specific", "Netflix"),
    ("Netflix keeps buffering", "app_specific", "Netflix"),
    ("Facebook isn't loading", "app_specific", "Facebook"),
    ("YouTube won't play", "app_specific", "YouTube"),
    ("My Zoom keeps dropping", "app_specific", "Zoom"),
    ("Teams won't connect", "app_specific", "Teams"),
    ("My game keeps disconnecting", "app_specific", "your game"),
    ("My email won't load", "app_specific", "your email"),
    ("My printer is offline", "app_specific", "your printer"),
    ("I can't access my banking website", "app_specific", "your bank's website"),
    ("My internet is down", "broad", None),
    ("I have no internet", "broad", None),
    ("My Wi-Fi isn't working", "broad", None),
    ("Nothing will load", "broad", None),
    ("I can't connect to anything", "broad", None),
    ("All my devices are offline", "broad", None),
    ("Everything is down", "broad", None),
    ("The internet keeps dropping", "broad", None),
    # Neither, or both — must fall through to diagnostics without asking.
    ("it's being weird", None, None),
    ("netflix won't load and my internet is down", None, "Netflix"),
]

# (reply, expected clarify_reply)
REPLIES = [
    ("just Netflix", "ONLY_APP"),
    ("only Facebook", "ONLY_APP"),
    ("everything else works", "ONLY_APP"),
    ("other stuff loads fine", "ONLY_APP"),
    ("actually nothing works", "EVERYTHING_DOWN"),
    ("other sites are slow too", "EVERYTHING_DOWN"),
    ("yeah I can't get on anything", "EVERYTHING_DOWN"),
    ("I'm not sure", "UNSURE"),
    ("I don't know", "UNSURE"),
    ("I haven't checked", "UNSURE"),
    ("I only tried Netflix", "UNSURE"),   # collides with the ONLY_APP cue if unguarded
    # The two that failed LIVE under the router, plus their obvious neighbours.
    # `clarify_reply`'s other filling path — the `set_clarify_reply` classifier the MODEL
    # calls — is unavailable there, so a phrasing these cues miss is a STUCK turn, not a
    # graceful fallback: driven, the agent asked the same question over again. An adverb
    # between "not" and "sure" is the ordinary way people say it and `\bnot sure\b` could
    # not see it.
    ("I'm not really sure", "UNSURE"),
    ("couldn't tell you honestly", "UNSURE"),
    ("I'm not entirely sure", "UNSURE"),
    ("no clue", "UNSURE"),
    ("hard to say", "UNSURE"),
    # The other half of widening a cue set: it must not swallow a caller who IS sure.
    ("just Netflix, everything else is fine", "ONLY_APP"),
    ("no, everything is down", "EVERYTHING_DOWN"),
]


def load_config(app_dir):
  path = os.path.join(app_dir, "tools", "repair_dag", "python_function",
                      "python_code.py")
  ns = {}
  with open(path) as fh:
    exec(compile(fh.read(), path, "exec"), ns)  # noqa: S102 - our own emitted file
  return ns["repair_dag"]()


def drive(cfg, sm, text, scanned):
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": scanned, "is_inactivity": False,
      "event_data": {}, "config_id": "repair",
  })


def run(app_dir):
  cfg = load_config(app_dir)
  failures = 0

  print("OPENING UTTERANCE -> scope / app (no model)\n")
  for utt, want_scope, want_app in OPENINGS:
    sm = fb.seed_sm(cfg)
    sm["filled"] = dict(SWEPT)
    sm["pending"] = {}
    out = drive(cfg, sm, utt, utt)
    filled = out["sm"].get("filled", {})
    scope, app = filled.get("complaint_scope"), filled.get("app_name")
    asked = out["action"].get("message") or ""
    ok = (scope == want_scope) and (app == want_app)
    failures += (not ok)
    print(f"{'ok  ' if ok else 'FAIL'} {utt[:44]:46} scope={scope!s:14} app={app!s}")
    if want_scope == "app_specific" and "{app_name}" in asked:
      print("       LEAK: the question rendered a literal placeholder")
      failures += 1

  print("\nREPLY -> branch (no model)\n")
  for reply, want in REPLIES:
    sm = fb.seed_sm(cfg)
    sm["filled"] = dict(SWEPT, complaint_scope="app_specific", app_name="Netflix")
    sm["pending"] = {}
    drive(cfg, sm, "My Netflix doesn't work", "My Netflix doesn't work")
    out = drive(cfg, sm, reply, reply)
    got = out["sm"].get("filled", {}).get("clarify_reply")
    ok = got == want
    failures += (not ok)
    print(f"{'ok  ' if ok else 'FAIL'} {reply[:44]:46} -> {got!s:18} want={want}")

  total = len(OPENINGS) + len(REPLIES)
  print(f"\n{total - failures}/{total} classified correctly with no LLM")
  return 1 if failures else 0


if __name__ == "__main__":
  ap = argparse.ArgumentParser()
  ap.add_argument("--app-dir", default="./built")
  raise SystemExit(run(os.path.abspath(ap.parse_args().app_dir)))
