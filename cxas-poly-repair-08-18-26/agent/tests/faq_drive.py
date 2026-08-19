"""Drive ad-hoc turns against a deployed app and print the full tool calls.

`cuj_diff.drive` keeps only the tool NAME, but the whole point of the FAQ work is what
query the model sent to search and what came back, so this prints the raw call dicts.

    python faq_drive.py --app <APP_ID> [--cuj <name>] "turn one" "turn two"
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

import flows  # noqa: E402

import cuj_diff  # noqa: E402


def main(argv):
    app_id = cuj_diff.CONVERTED
    cuj_name = None
    if "--app" in argv:
        i = argv.index("--app")
        app_id = argv[i + 1]
        del argv[i:i + 2]
    if "--cuj" in argv:
        i = argv.index("--cuj")
        cuj_name = argv[i + 1]
        del argv[i:i + 2]

    seed = {}
    if cuj_name:
        cujs = flows.load_cujs(start=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        seed = cujs[cuj_name]

    print(f"app: {app_id}   cuj: {cuj_name or '(none)'}\n" + "=" * 78)
    session = cuj_diff._session(app_id, seed)
    for utterance in argv:
        print(f"\n> {utterance}")
        turn = session.send(utterance)
        for call in (turn.tool_calls or []):
            print(f"  [tool] {json.dumps(call, default=str)[:1400]}")
        for resp in (turn.tool_responses or []):
            print(f"  [resp] {json.dumps(resp, default=str)[:2500]}")
        print(f"< {cuj_diff._say(turn.agent_text)}")
        if turn.session_ended:
            print("  [session ended]")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
