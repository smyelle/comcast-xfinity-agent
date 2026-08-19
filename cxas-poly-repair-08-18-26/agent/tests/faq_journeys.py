"""Drive the journeys the FAQ search tool opens up, and print them as full scripts.

These are the showcase transcripts: every one is a real drive against the deployed app,
so what it prints is what the agent actually said, tool calls and all.

    python faq_journeys.py [--app <APP_ID>] [name ...]
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

import flows  # noqa: E402

import cuj_diff  # noqa: E402

APP = "95ea967e-73c2-48d5-bc16-f1836076cf65"

# (title, cuj seed, utterances)
JOURNEYS = [
    ("Device fix instead of a shrug — xFi pod", "all_clear", [
        "my xFi pod keeps dropping off the network",
        "just the pod, everything else is fine"]),
    ("Device fix — X1 remote won't pair", "all_clear", [
        "my internet is not working",
        "my X1 remote stopped pairing with the box"]),
    ("Device fix — camera offline on a healthy network", "all_clear", [
        "my internet is not working",
        "the wifi is fine but my Xfinity camera keeps going offline"]),
    ("Hardware swap, then where to return the old one", "gateway_swap", [
        "my internet is not working",
        "where do I return the old gateway?"]),
    ("Self-install the replacement", "gateway_swap", [
        "my internet is not working",
        "how do I self install the new modem?"]),
    ("Reboot accepted, then a worry about the side effects", "gateway_reboot", [
        "my internet is not working",
        "yes go ahead",
        "will that reset my wifi password?"]),
    ("Technician dispatched — will it cost me?", "network_impaired", [
        "my internet is not working",
        "will I be charged for the technician visit?"]),
    ("Outage advisory, then how to stay informed", "area_outage", [
        "my internet is not working",
        "how can I get text updates about the outage?"]),
    ("Suspected phishing email", "all_clear", [
        "my internet is not working",
        "I got an email saying my Xfinity account was compromised, how do I know if "
        "it's a scam?"]),
    ("Moving house mid-call", "all_clear", [
        "my internet is not working",
        "actually I'm moving next month, how do I transfer my service?"]),
    ("Pausing service for the summer", "all_clear", [
        "my internet is not working",
        "can I pause my service while I'm away for the summer?"]),
    ("Upsell question the agent could never answer before", "all_clear", [
        "my internet is not working",
        "how do I upgrade to a faster plan?"]),
]


def run(title, cuj_name, utterances, cujs):
    seed = cujs[cuj_name]
    lines = [f"### {title}", f"    [seed: {cuj_name}]", ""]
    try:
        session = cuj_diff._session(APP, seed)
    except Exception as exc:  # noqa: BLE001
        return "\n".join(lines + [f"    ERROR {type(exc).__name__}: {exc}"])
    for utterance in utterances:
        lines.append(f"> {utterance}")
        try:
            turn = session.send(utterance)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  ERROR {type(exc).__name__}: {exc}")
            break
        for call in (turn.tool_calls or []):
            lines.append(f"  [tool] {call.get('action')} {call.get('args') or ''}")
        lines.append(f"< {cuj_diff._say(turn.agent_text)}")
        lines.append("")
        if turn.session_ended:
            lines.append("  [session ended]")
            break
    return "\n".join(lines)


def main(argv):
    global APP
    if "--app" in argv:
        i = argv.index("--app")
        APP = argv[i + 1]
        del argv[i:i + 2]
    cujs = flows.load_cujs(start=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    wanted = [j for j in JOURNEYS if not argv or j[0] in argv]

    cuj_diff._session(APP, cujs["all_clear"])  # warm the import serially
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, t, c, u, cujs) for t, c, u in wanted]
        for future in futures:
            print(future.result())
            print("-" * 90)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
