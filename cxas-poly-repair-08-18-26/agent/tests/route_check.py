#!/usr/bin/env python3
"""Route-classification check: does the steering router pick the right golden category?

Drives a DEPLOYED build with the opening utterance of each golden eval
(tests/routing_corpus.json, derived from the GECX golden steering agent's own eval
suite) and reads back which flow the model routed to, from the set_active_flow tool
call. Every flow key is a GECX golden intent category, so a correct route is also the
label a deferred call hands downstream (detected_intent) — see
source_tools.ROUTE_CATALOGUE. This is the routing counterpart to ladder_check.py
(which covers the diagnostics verdict ladder offline).

Routing is a MODEL decision, so there is no offline path: this needs a deployed app
and ADC credentials.

    APP_ID=<app id> python tests/route_check.py
    python tests/route_check.py --app <resource-or-id> --workers 6

Accuracy is split by tag so the score stays honest:
  * clean            — in-scope; the router should route to the golden category exactly
  * kb_gap           — golden answers these from a knowledge base this app does not have
  * global_behavior  — unintelligible / unrelated: absorbed into diagnostics by design
  * bilingual        — Spanish: this app is English-first
Only `clean` is a hard expectation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

from cxas_scrapi.core.sessions import Modality, Sessions  # noqa: E402

CORPUS = os.path.join(HERE, "routing_corpus.json")
PROJECT = "ces-deployment-dev"
LOCATION = "us"


def resource(app: str) -> str:
    """Accept a bare app id or a full resource name."""
    if app.startswith("projects/"):
        return app
    return f"projects/{PROJECT}/locations/{LOCATION}/apps/{app}"


def route_of(resp):
    """The L1 flow the router chose (first set_active_flow) and the L2 head-intent leaf.

    L1 is the first `set_active_flow` tool-call arg. L2 comes from the multi-level
    steering recorder (`steering_record_path` for an internal node,
    `steering_record_intent` for a plain deferred leaf): its tool RESPONSE carries
    `detected_intent` (the deepest leaf) and `detected_path` (the slash-joined
    ancestry). The leaf is what a deferred call hands downstream — so this is the
    level-2 counterpart to the L1 route, read straight off the emitted primitive.
    """
    route, tools, texts, head_intent, detected_path = None, [], [], None, None
    for output in resp.outputs:
        di = getattr(output, "diagnostic_info", None)
        if not (di and hasattr(di, "messages")):
            continue
        for message in di.messages:
            role = getattr(message, "role", "")
            for chunk in getattr(message, "chunks", []):
                kind = chunk._pb.WhichOneof("data") if hasattr(chunk, "_pb") else None
                if kind == "tool_call":
                    tc = chunk.tool_call
                    name = tc.display_name or tc.tool
                    tools.append(name)
                    if name == "set_active_flow" and route is None:
                        args = Sessions._expand_pb_struct(tc.args) or {}
                        route = args.get("flow")
                elif kind == "tool_response":
                    tr = chunk.tool_response
                    rname = tr.display_name or tr.tool
                    # The generated recorder tool is namespaced `<router>_record_path` /
                    # `<router>_record_intent`; match by suffix so the router config id
                    # (here "steering") does not have to be hard-coded.
                    if rname.endswith("_record_path") or rname.endswith("_record_intent"):
                        r = Sessions._expand_pb_struct(tr.response) or {}
                        # Tool responses come back wrapped as {"result": {...}}; unwrap it.
                        if isinstance(r, dict) and isinstance(r.get("result"), dict):
                            r = r["result"]
                        if isinstance(r, dict):
                            if r.get("detected_intent"):
                                head_intent = r["detected_intent"]
                            if r.get("detected_path"):
                                detected_path = r["detected_path"]
                elif kind in ("text", "transcript") and role != "user":
                    text = getattr(chunk, kind)
                    if text and text.strip():
                        texts.append(text.strip())
    return route, tools, " ".join(texts)[:160], head_intent, detected_path


def drive(scenario: dict, app: str, tag: str, modality) -> dict:
    """Send one opening utterance and record the route (with light retry).

    In AUDIO mode the sim TTS's the utterance and CES transcribes it (STT) before the
    router sees it, so this also proves routing survives the speech path.
    """
    session_id = f"{scenario['id']}-{tag}"
    for attempt in range(3):
        try:
            resp = Sessions(app_name=app).run(
                session_id,
                text=scenario["user_utterances"][0],
                modality=modality,
                variables=dict(scenario["seeded_variables"]),
                use_tool_fakes=True,
            )
            route, tools, text, head_intent, detected_path = route_of(resp)
            return {**scenario, "route": route, "tools": tools, "agent_text": text,
                    "head_intent": head_intent, "detected_path": detected_path}
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return {**scenario, "route": None, "error": f"{type(e).__name__}: {e}"}


HANDLED = {"repair", "reboot", "human"}


def ok(r: dict) -> bool:
    return r.get("route") == r["expected_flow"] or r.get("route") in (
        r.get("acceptable_flows") or []
    )


def disposition_ok(r: dict):
    """Did the chosen route drive the matching downstream action?

    A defer category must actually fire `verdict_defer` (which records detected_intent
    = the route and hands the leg back for downstream routing); `human` must escalate;
    a handled route (diagnostics/reboot) must NOT defer. Returns None when no route
    fired (nothing to check). This is what validates the golden-category label really
    reaches the downstream handoff, not just that the model named a flow.
    """
    route, tools = r.get("route"), r.get("tools") or []
    if not route:
        return None
    if route == "human":
        return ("verdict_human_request" in tools or "transfer_to_human" in tools
                or bool(r.get("agent_text")))
    deferred = ("verdict_defer" in tools
                or any(t.endswith("_record_path") or t.endswith("_record_intent")
                       for t in tools))
    if route in HANDLED:  # repair / reboot: handled here, must not defer
        return not deferred
    return deferred  # a golden-category defer must hand off (records the detected path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--app", default=os.environ.get("APP_ID", ""),
        help="deployed app id or full resource name (or set APP_ID)")
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument(
        "--tag", default=str(os.getpid()),
        help="session-id suffix; a fresh value per run avoids resuming old sessions")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--modality", choices=["text", "audio"], default="text",
                    help="audio drives the utterance through TTS->STT before the router")
    a = ap.parse_args(argv)
    if not a.app:
        ap.error("--app (or APP_ID) is required — routing needs a deployed app")
    app = resource(a.app)
    modality = Modality.AUDIO if a.modality == "audio" else Modality.TEXT

    scenarios = json.load(open(a.corpus))["scenarios"]
    print(f"driving {len(scenarios)} routing scenarios ({a.modality}) against "
          f"{app.split('/')[-1]}\n", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(drive, s, app, a.tag, modality) for s in scenarios]
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: r["id"])

    print("%-38s %-24s %-24s %-6s %s" % ("scenario", "expected", "got", "route", "disp"))
    print("-" * 100)
    for r in results:
        verdict = "ERR" if r.get("error") else ("PASS" if ok(r) else "MISS")
        got = r.get("error") or (r.get("route") or "(none)")
        d = disposition_ok(r)
        disp = "-" if d is None else ("ok" if d else "BAD")
        print("%-38s %-24s %-24s %-6s %s"
              % (r["id"][:38], r["expected_flow"], str(got)[:24], verdict, disp))

    by_tag: dict[str, list] = {}
    for r in results:
        by_tag.setdefault(r["tag"], []).append(r)
    print("\n== accuracy by tag ==")
    for tag in ("clean", "disambiguation", "kb_gap", "global_behavior", "bilingual",
                "proxy"):
        rs = by_tag.get(tag, [])
        if rs:
            print(f"  {tag:16s}: {sum(1 for r in rs if ok(r))}/{len(rs)}")
    clean = by_tag.get("clean", [])
    print(f"\nCLEAN routing accuracy: {sum(1 for r in clean if ok(r))}/{len(clean)}")
    print(f"overall (all tags):     {sum(1 for r in results if ok(r))}/{len(results)}")
    disp = [disposition_ok(r) for r in results]
    print(f"disposition (route drove handle/defer/escalate): "
          f"{sum(1 for d in disp if d)}/{sum(1 for d in disp if d is not None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
