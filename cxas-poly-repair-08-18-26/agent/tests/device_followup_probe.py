"""Does a device named AFTER a verdict still reach the device-help door?

Journey 1 names the device in the opening breath and searches correctly. Journeys 2 and 3
name it on a later turn and never search — in journey 2 the model then reconstructed X1
pairing steps from memory, which `<device_help>` forbids in as many words. This prints the
slot state after every turn so the answer is the engine's, not an inference from the words.

    python tests/device_followup_probe.py [APP_ID]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

from app.products.slot_studio.studio.chat_session import ChatSession  # noqa: E402

APP_ID = sys.argv[1] if len(sys.argv) > 1 else "95ea967e-73c2-48d5-bc16-f1836076cf65"
APP = f"projects/ces-deployment-dev/locations/us/apps/{APP_ID}"

ALL_CLEAR = ("8069100230359946", "all_clear")

# The two shapes, differing only in WHEN the device is named.
CASES = [
    ("device named FIRST (known good)", ["my X1 remote stopped pairing with the box"]),
    ("device named AFTER a verdict", ["my internet is not working",
                                      "my X1 remote stopped pairing with the box"]),
]

WATCH = ("dev_pod", "dev_tv_box", "dev_remote", "dev_camera", "dev_app", "dev_gateway",
         "device_subject", "device_symptom", "device_query", "device_searched")


def main() -> None:
  account, mock = ALL_CLEAR
  for title, utterances in CASES:
    print(f"\n=== {title}")
    s = ChatSession(app_name=APP, initial_variable_state={
        "mock_config_string": mock, "accountNumber": account, "account_id": account})
    orig = s._sessions.run
    s._sessions.run = lambda **kw: orig(use_tool_fakes=True, **kw)
    for utt in utterances:
      turn = s.send(utt)
      called = [c.get("action") for c in (turn.tool_calls or [])]
      print(f"  > {utt}")
      print(f"    tools: {called}")
      print(f"    < {(turn.agent_text or '').strip()[:150]}")
      filled = (s.get_state() or {}).get("filled_slots") or {}
      seen = {k: v for k, v in filled.items() if k in WATCH}
      print(f"    device slots: {seen or '(none filled)'}")
    print(f"  session: {s.session_id}")


if __name__ == "__main__":
  main()
