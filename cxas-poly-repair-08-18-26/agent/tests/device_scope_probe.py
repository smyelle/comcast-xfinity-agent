"""Is there a SUPPORTED way into device help after the ladder has already run?

The clarification gate is pre-diagnostic on purpose (app.py's `clarify_reply` comment
records the live regression that made it so), so a device named after the all-clear cannot
re-open it. But the walkthrough asks its own scope question — "is everything having
trouble, or just one device?" — and a caller naming Xfinity equipment there is the same
signal arriving by a sanctioned route. This checks whether that route reaches the search.

    python tests/device_scope_probe.py [APP_ID]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

from app.products.slot_studio.studio.chat_session import ChatSession  # noqa: E402

APP_ID = sys.argv[1] if len(sys.argv) > 1 else "95ea967e-73c2-48d5-bc16-f1836076cf65"
APP = f"projects/ces-deployment-dev/locations/us/apps/{APP_ID}"
ACCOUNT, MOCK = "8069100230359946", "all_clear"

CASES = [
    ("accept the walkthrough, then name the device", [
        "my internet is not working", "yes please", "just my Xfinity camera"]),
    ("name the device in the scope answer", [
        "my internet is not working", "yes", "only my xFi pod, everything else is fine"]),
]


def main() -> None:
  for title, utterances in CASES:
    print(f"\n=== {title}")
    s = ChatSession(app_name=APP, initial_variable_state={
        "mock_config_string": MOCK, "accountNumber": ACCOUNT, "account_id": ACCOUNT})
    orig = s._sessions.run
    s._sessions.run = lambda **kw: orig(use_tool_fakes=True, **kw)
    searched = False
    for utt in utterances:
      turn = s.send(utt)
      called = [c.get("action") for c in (turn.tool_calls or [])]
      searched = searched or any("faq_search" in (c or "") for c in called)
      print(f"  > {utt}")
      print(f"    tools: {called}")
      print(f"    < {(turn.agent_text or '').strip()[:200]}")
    print(f"  SEARCHED: {searched}   session: {s.session_id}")


if __name__ == "__main__":
  main()
