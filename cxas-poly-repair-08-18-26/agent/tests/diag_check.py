#!/usr/bin/env python3
"""Live diagnostics eval: drive every verdict rung against a DEPLOYED app, in text or audio.

This is the live, modality-switchable counterpart to the offline ladder_check.py /
clarify_check.py. For each cujs.yaml preset it seeds the mock_config_string, drives the
conversation, and asserts the rung the ladder fired (the `verdict_*` tool) — plus the
reboot accept/decline handshake, the single-verdict latch, both clarification branches,
and the no-account ask. In AUDIO mode the caller's turns go through TTS->STT before the
sweep + ladder, so it proves the diagnostics survive the speech path, not just routing.

    APP_ID=<id> python tests/diag_check.py                 # text
    APP_ID=<id> python tests/diag_check.py --modality audio

Needs a deployed app + ADC. Assertions: {"tool": name|[names]} = one of those tool calls
must fire this turn; a string / [strings] = all must appear in the agent's reply; None =
the reply must NOT re-speak the previous verdict (the latch).
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

import flows  # noqa: E402
from cxas_scrapi.core.sessions import Modality, Sessions  # noqa: E402

PROJECT = "ces-deployment-dev"
LOCATION = "us"
CUJS = flows.load_cujs(start=HERE)

# (name, cuj preset, [(utterance, expect)]) — expect per the module docstring.
SCENARIOS = [
    ("all clear", "all_clear",
     [("my internet is not working", {"tool": "verdict_all_clear"})]),
    ("area outage (no live agent)", "area_outage",
     [("my internet is not working", {"tool": "verdict_area_outage"})]),
    ("gateway hardware swap", "gateway_swap",
     [("my internet is not working", {"tool": "verdict_hardware_swap"})]),
    ("network impairment -> technician", "network_impaired",
     [("my internet is not working",
       {"tool": ["verdict_network_tech", "verdict_network_generic"]})]),
    ("account suspended -> billing block", "account_suspended",
     [("my internet is not working", {"tool": "verdict_account_block"})]),
    ("convoy predicted impairment", "convoy_technician",
     [("my internet is not working",
       {"tool": ["verdict_convoy_impairment", "verdict_convoy_swap"]})]),
    ("gateway reboot offered", "gateway_reboot",
     [("my internet is not working", {"tool": "verdict_offer_reboot"})]),
    ("reboot offered -> ACCEPTED", "convoy_predictive_reboot",
     [("my internet is not working", {"tool": "verdict_offer_reboot"}),
      ("yes please", {"tool": "verdict_execute_reboot"})]),
    ("reboot offered -> DECLINED", "convoy_predictive_reboot",
     [("my internet is not working", {"tool": "verdict_offer_reboot"}),
      ("no thanks", {"tool": "verdict_reboot_declined"})]),
    ("no account -> asks for it", "no_account",
     [("my internet is not working", "account number")]),
    ("verdict spoken ONCE (latch)", "all_clear",
     [("my internet is not working", {"tool": "verdict_all_clear"}),
      ("ok thanks", None)]),
    ("clarify: only that app -> advise", "all_clear",
     [("my Netflix won't load", "is it only"),
      ("just Netflix, everything else is fine", {"tool": "verdict_app_specific"})]),
    ("clarify: everything down -> verdict", "all_clear",
     [("my Netflix won't load", "is it only"),
      ("nothing works, other sites are down too", {"tool": "verdict_all_clear"})]),
]


def resource(app: str) -> str:
    if app.startswith("projects/"):
        return app
    return f"projects/{PROJECT}/locations/{LOCATION}/apps/{app}"


def turn_result(resp):
    """(agent_text, {tool names fired}) for one response."""
    tools, texts = set(), []
    for output in resp.outputs:
        di = getattr(output, "diagnostic_info", None)
        if not (di and hasattr(di, "messages")):
            continue
        for message in di.messages:
            role = getattr(message, "role", "")
            for chunk in getattr(message, "chunks", []):
                kind = chunk._pb.WhichOneof("data") if hasattr(chunk, "_pb") else None
                if kind == "tool_call":
                    tools.add(chunk.tool_call.display_name or chunk.tool_call.tool)
                elif kind in ("text", "transcript") and role != "user":
                    t = getattr(chunk, kind)
                    if t and t.strip():
                        texts.append(t.strip())
    return " ".join(texts), tools


def check(expect, text, tools, prior_text):
    if isinstance(expect, dict):
        want = expect["tool"]
        want = [want] if isinstance(want, str) else want
        return any(w in tools for w in want)
    if expect is None:  # latch: must not re-speak the prior verdict
        return not (prior_text and prior_text[:60].lower() in text.lower())
    wanted = [expect] if isinstance(expect, str) else expect
    return all(w.lower() in text.lower() for w in wanted)


def drive(scenario, app, tag, modality):
    name, cuj, steps = scenario
    seed = dict(CUJS[cuj].variables)
    session_id = f"diag-{cuj}-{tag}-{abs(hash(name)) % 10000}"
    sess = Sessions(app_name=app)
    rows, ok, prior = [], True, ""
    for i, (utt, expect) in enumerate(steps):
        try:
            resp = sess.run(session_id, text=utt, modality=modality,
                            variables=seed if i == 0 else None, use_tool_fakes=True)
        except Exception as e:  # noqa: BLE001
            rows.append((utt, f"ERR {type(e).__name__}", False))
            ok = False
            break
        text, tools = turn_result(resp)
        good = check(expect, text, tools, prior)
        ok = ok and good
        verdicts = ",".join(sorted(t for t in tools if t.startswith("verdict_"))) or "-"
        rows.append((utt, verdicts, good))
        prior = text
    return {"name": name, "cuj": cuj, "ok": ok, "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default=os.environ.get("APP_ID", ""))
    ap.add_argument("--tag", default=str(os.getpid()))
    # Concurrency is a text convenience. In AUDIO every turn is a real TTS->STT round trip,
    # and driving several at once measurably degrades transcription (contention on the
    # speech path): 4 workers swung a passing suite to 4/13, the same build serial held
    # 11-13/13. So audio defaults to serial; text keeps the fast fan-out.
    ap.add_argument("--workers", type=int, default=None)
    # A single audio scenario can fail on transient STT noise (a dropped word on a short
    # "yes please"/"ok thanks" answer) even though the agent handles it correctly on a
    # clean transcript. Re-drive a FAILED scenario up to this many times, in a fresh
    # session; it counts as passing if any attempt passes. A scenario that fails EVERY
    # attempt is a real defect, not noise. Text is deterministic, so it defaults to none.
    ap.add_argument("--retries", type=int, default=None)
    ap.add_argument("--modality", choices=["text", "audio"], default="text")
    a = ap.parse_args(argv)
    if not a.app:
        ap.error("--app (or APP_ID) is required — a live diagnostics eval needs a deploy")
    app = resource(a.app)
    is_audio = a.modality == "audio"
    modality = Modality.AUDIO if is_audio else Modality.TEXT
    workers = a.workers if a.workers is not None else (1 if is_audio else 4)
    retries = a.retries if a.retries is not None else (2 if is_audio else 0)

    print(f"driving {len(SCENARIOS)} diagnostics scenarios ({a.modality}, workers={workers}, "
          f"retries={retries}) against {app.split('/')[-1]}\n", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(drive, s, app, a.tag, modality) for s in SCENARIOS]
        for fut in as_completed(futs):
            results.append(fut.result())
    order = {s[0]: i for i, s in enumerate(SCENARIOS)}
    results.sort(key=lambda r: order[r["name"]])

    # Re-drive transient failures serially. Each retry is a fresh session; the scenario
    # passes if any attempt does. `attempts` records how many it took, so a scenario that
    # only ever passes on the last retry stays visible as flaky rather than silently green.
    by_name = {r["name"]: r for r in results}
    for r in results:
        r["attempts"] = 1
        for attempt in range(retries):
            if r["ok"]:
                break
            fresh = drive(SCENARIOS[order[r["name"]]], app, f"{a.tag}-r{attempt + 1}", modality)
            fresh["attempts"] = r["attempts"] + 1
            by_name[r["name"]] = fresh
            r = fresh
    results = [by_name[s[0]] for s in SCENARIOS]

    for r in results:
        tag = "PASS" if r["ok"] else "FAIL"
        extra = f"  [passed on attempt {r['attempts']}]" if r["ok"] and r["attempts"] > 1 else (
            f"  [failed {r['attempts']} attempts]" if not r["ok"] and r["attempts"] > 1 else "")
        print(f"  {tag}  {r['name']:42s} ({r['cuj']}){extra}")
        for utt, verdicts, good in r["rows"]:
            print(f"        {'ok ' if good else 'BAD'} {utt[:44]:46s} -> {verdicts}")
    passed = sum(1 for r in results if r["ok"])
    flaky = sum(1 for r in results if r["ok"] and r["attempts"] > 1)
    note = f" ({flaky} passed only after retry — transient STT noise)" if flaky else ""
    print(f"\n{passed}/{len(results)} diagnostics scenarios correct ({a.modality}){note}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
