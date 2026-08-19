"""Offline proof that equipment is recognised, and that the search gate is shut.

Two things this pins down, both with no model anywhere in the loop:

  * the equipment cue sets capture the right devices — including SEVERAL from one
    utterance, which is the whole reason there is a slot per family rather than one
    catalogue slot (see clarify.EQUIPMENT);
  * `BuildDeviceQuery` fires ONLY behind the DAG gate. That is the property the whole
    re-hook exists for, and it is much cheaper to assert here than to drive live.

    python device_check.py [--app-dir ./built]

The `scanned_user_text` detail from clarify_check.py applies here too: cue slots only
fill on a turn the live `before_model` marks as scanned, so the engine is called directly
with that scalar rather than through `run_engine`.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths()

import clarify  # noqa: E402
from flows.engine import loader as fb  # noqa: E402

fb.set_framework_root(labs_paths.framework_root())

# Swept clean, so nothing but the device path can vary.
SWEPT = dict(diagnostics_complete="true", reboot_answer_allowed="true",
             account_status="clear", outage_status="none", convoy_status="clear",
             network_status="healthy", gateway_status="healthy", wifi_status="healthy",
             cable_modem_mac="AA:BB", device_id="AA:BB",
             accountNumber="8344200010126021", active_flow="repair")

# (utterance, {slot: value} expected to be filled)
EQUIPMENT = [
    ("my xFi pod keeps dropping off", {"dev_pod": "xFi pod",
                                       "device_symptom": "keeps dropping"}),
    ("the pods went offline again", {"dev_pod": "xFi pod",
                                     "device_symptom": "keeps going offline"}),
    ("my X1 box is stuck on a black screen", {"dev_tv_box": "X1 TV Box",
                                              "device_symptom": "black screen"}),
    ("the cable box won't turn on", {"dev_tv_box": "X1 TV Box",
                                     "device_symptom": "won't turn on"}),
    ("my remote stopped pairing", {"dev_remote": "remote",
                                   "device_symptom": "won't pair"}),
    # "X1" brands the line, not just the box. This must resolve to the REMOTE alone —
    # matching the box too made a one-device complaint read as two.
    ("my X1 remote stopped pairing with the box", {"dev_remote": "remote",
                                                   "device_symptom": "won't pair"}),
    # TV and Xfinity Home are repair's now, so their vocabulary has to resolve. The
    # first of these fell through to the internet ladder and was answered with a WiFi
    # walkthrough offer, because nothing here knew the word "television".
    ("the picture on my television is frozen", {"dev_tv_box": "X1 TV Box",
                                                "device_symptom": "frozen"}),
    ("the on-screen guide won't come up", {"dev_tv_box": "X1 TV Box",
                                           "device_symptom": "frozen"}),
    ("my doorbell camera stopped recording", {"dev_camera": "camera",
                                              "device_symptom": "not recording"}),
    ("the motion sensor keeps going off", {"dev_camera": "camera"}),
    ("the camera keeps going offline", {"dev_camera": "camera",
                                        "device_symptom": "keeps going offline"}),
    ("where do I return the old gateway", {"dev_gateway": "gateway",
                                           "device_need": "return"}),
    ("how do I self install the new modem", {"dev_gateway": "gateway",
                                             "device_need": "self install"}),
    ("the xfinity app shows a blinking light", {"dev_app": "Xfinity app",
                                                "device_symptom": "blinking light"}),
    # PLURAL — the point of one slot per family. "playing up" is the catch-all symptom.
    ("my pod and my remote are both playing up", {"dev_pod": "xFi pod",
                                                  "dev_remote": "remote",
                                                  "device_symptom": "not working"}),
    ("my X1 box and my camera are not working", {"dev_tv_box": "X1 TV Box",
                                                 "dev_camera": "camera",
                                                 "device_symptom": "not working"}),
    # Third-party apps are NOT equipment. Nothing may fill, or the DAG would look up
    # Comcast pages for somebody else's outage.
    ("my Netflix won't load", {}),
    ("Spotify keeps buffering", {}),
    # A symptom with no Xfinity device. `device_symptom` filling here is harmless and
    # expected — the gate needs DEVICE_NAMED, so a symptom on its own opens nothing.
    ("my printer is offline", {"device_symptom": "keeps going offline"}),
]

# (label, seed beyond SWEPT, utterances, expect BuildDeviceQuery to have fired)
GATE = [
    ("confirmed device-specific -> OPEN", {},
     ["my xFi pod keeps dropping off", "just the pod, everything else is fine"], True),
    ("network fault, device named -> SHUT",
     dict(network_status="impaired", technician_type="network_tech"),
     ["how do I reset my X1 box"], False),
    # Still SHUT, but no longer for the reason first written here. A follow-up device IS
    # captured now — the equipment slots are `kind="intent", multi_fill=True`, so they no
    # longer wait for the routing turn. What keeps this door shut is the CLARIFICATION
    # GATE, which is pre-diagnostic on purpose (see `clarify_reply` in app.py: a stray
    # product name late in the call once re-opened it mid-walkthrough). The gateway is
    # also deliberately absent from `SCOPE_CUES`, since diagnosing it is the ladder's job.
    # Opening a post-diagnosis device door is a product decision, not a framework limit.
    ("swap verdict, device named on turn 2 -> SHUT (pre-diagnostic gate)",
     dict(gateway_status="swap"),
     ["my internet is not working", "where do I return the old gateway"], False),
    ("device named but never confirmed -> SHUT", {},
     ["my xFi pod keeps dropping off"], False),
    ("no device named at all -> SHUT", {},
     ["my internet is not working", "just that, everything else is fine"], False),
]


# (utterance, expected device_subject, expected substring of the clarifying question)
# `device_subject` names ONE device and is empty when two were named, which is what makes
# a single sentence read correctly either way. A pod is not an app, so the app wording
# must never reach a device — that is the defect this pins.
SUBJECT = [
    ("my xFi pod keeps dropping off", "your xFi pod", "only your xFi pod"),
    ("my X1 box is stuck on a black screen", "your X1 TV Box", "only your X1 TV Box"),
    ("my X1 remote stopped pairing with the box", "your remote", "only your remote"),
    ("my remote stopped pairing", "your remote", "only your remote"),
    ("the picture on my television is frozen", "your X1 TV Box", "only your X1 TV Box"),
    ("my pod and my remote are both playing up", None, "only those"),
    ("my Netflix won't load", None, "only Netflix that's not working"),
]


def load_config(app_dir):
  path = os.path.join(app_dir, "tools", "repair_dag", "python_function",
                      "python_code.py")
  ns = {}
  with open(path) as fh:
    exec(compile(fh.read(), path, "exec"), ns)  # noqa: S102 - our own emitted file
  return ns["repair_dag"]()


def drive(cfg, sm, text):
  return fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text,
      "scanned_user_text": text, "is_inactivity": False,
      "event_data": {}, "config_id": "repair",
  })


def fired(action, tool):
  """Did this action DISPATCH `tool`?

  Read `function_call.name` and nothing else. Substring-matching the whole action is a
  false positive every time: the engine's per-turn `hide_tools` list names every tool it
  is NOT firing, so `build_device_query` appears in the action on exactly the turns it
  did not run.
  """
  call = (action or {}).get("function_call") or {}
  return call.get("name") == tool


def run(app_dir):
  cfg = load_config(app_dir)
  failures = 0

  print("Cue sets are disjoint within each family\n")
  for slot, cues in clarify.EQUIPMENT.items():
    if len(cues) != 1:
      print(f"FAIL {slot}: {len(cues)} values — a family must hold exactly one, or two "
            "matches leave the slot unfilled")
      failures += 1
  print(f"  {len(clarify.EQUIPMENT)} families, one value each")

  print("\nUTTERANCE -> equipment slots (no model)\n")
  for utt, want in EQUIPMENT:
    sm = fb.seed_sm(cfg)
    sm["filled"] = dict(SWEPT)
    sm["pending"] = {}
    out = drive(cfg, sm, utt)
    filled = out["sm"].get("filled", {})
    got = {k: v for k, v in filled.items()
           if k in clarify.EQUIPMENT or k in ("device_symptom", "device_need")}
    ok = got == want
    failures += (not ok)
    print(f"{'ok  ' if ok else 'FAIL'} {utt[:42]:44} {got if got else '(none)'}")
    if not ok:
      print(f"       want {want}")

  print("\nTHE CLARIFYING QUESTION — device wording, singular and plural (no model)\n")
  for utt, want_subject, want_text in SUBJECT:
    sm = fb.seed_sm(cfg)
    sm["filled"] = dict(SWEPT)
    sm["pending"] = {}
    out = drive(cfg, sm, utt)
    subject = out["sm"].get("filled", {}).get("device_subject")
    asked = out["action"].get("message") or ""
    ok = (subject == want_subject) and (want_text in asked)
    failures += (not ok)
    print(f"{'ok  ' if ok else 'FAIL'} {utt[:40]:42} {asked[:78]}")
    if not ok:
      print(f"       subject={subject!r} want={want_subject!r}; wanted {want_text!r}")
    if "app" in asked.lower() and want_subject is not None:
      print("       LEAK: a DEVICE was offered the app wording")
      failures += 1

  print("\nTHE GATE — does BuildDeviceQuery fire? (no model)\n")
  for label, seed, utterances, want in GATE:
    sm = fb.seed_sm(cfg)
    sm["filled"] = dict(SWEPT, **seed)
    sm["pending"] = {}
    did = False
    for utt in utterances:
      out = drive(cfg, sm, utt)
      did = did or fired(out.get("action", {}), "build_device_query")
    ok = did == want
    failures += (not ok)
    print(f"{'ok  ' if ok else 'FAIL'} {label:44} fired={did}  want={want}")

  total = len(EQUIPMENT) + len(SUBJECT) + len(GATE) + 1
  print(f"\n{total - failures}/{total} device checks correct with no LLM")
  return 1 if failures else 0


if __name__ == "__main__":
  ap = argparse.ArgumentParser()
  ap.add_argument("--app-dir", default="./built")
  raise SystemExit(run(os.path.abspath(ap.parse_args().app_dir)))
