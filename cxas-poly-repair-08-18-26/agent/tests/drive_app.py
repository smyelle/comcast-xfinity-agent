#!/usr/bin/env python3
"""Live baseline harness for the CES Comcast/Xfinity internet-repair agent.

Drives the ORIGINAL hosted CES app and records what it really does, so the
rewrite can be diffed against observed behaviour rather than against goldens
that may be stale.

  app : d4ec582c-034b-4126-83e6-057bf1b77a35  (xa-repair-voice-deterministic)
  proj: ces-deployment-dev            location: us

Driver
------
`app.products.slot_studio.studio.chat_session.ChatSession`, hosted mode
selected via `...studio.state.apply_settings(mode="hosted", project=…,
location=…)`.

Seeding session variables (the mechanism)
-----------------------------------------
`ChatSession(initial_variable_state={...})` -> the dict is splatted into
`Sessions.run(variables=...)` on turn 0 ONLY, which the transport packs as an
`{"variables": {...}}` input.  That is how a golden's `userInput.variables`
(accountNumber / account_id / mock_config_string / pre-set *_status) reach
`callback_context.state`, where the tool_fake_config code and the
before_agent_callback read them.  Variables supplied on later turns are ignored
by ChatSession, so everything a scenario needs must be in
`initial_variable_state`.

Tool fakes
----------
`app.json` sets goldenEvaluationToolCallBehaviour: FAKE, i.e. the goldens were
recorded with every tool's `tool_fake_config` executing.  `Sessions.run` gates
that on `use_tool_fakes=True`, which `ChatSession` does not expose -- so the
harness wraps `session._sessions.run` to inject it.  Without this the app hits
the real Comcast backends and `mock_config_string` does nothing.

Usage
-----
    python3 drive.py                          # every scenario, 8 workers
    python3 drive.py --scenario MVP_TC_04_Outage -v
    python3 drive.py --filter MVP_ --workers 6
    python3 drive.py --utterance "slow internet" \
                     --vars '{"accountNumber":"1","mock_config_string":"..."}'
    python3 drive.py --no-trace               # skip the (slow) state read

Writes one transcript per scenario to /tmp/cw/baseline/<scenario>.json:
    {"scenario", "app", "session_id", "seeded_variables", "mock_config_string",
     "turns":[{"user","agent_text","tool_calls","tool_responses","transfer",
               "session_ended"}],
     "final_state": {...}, "slot_machine_filled": {...},
     "predicted": {...}, "error": null}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

import flows  # noqa: E402

APP_ID = os.environ.get("DRIVE_APP_ID", "d4ec582c-034b-4126-83e6-057bf1b77a35")
PROJECT = "ces-deployment-dev"
LOCATION = "us"
APP_NAME = f"projects/{PROJECT}/locations/{LOCATION}/apps/{APP_ID}"

ORACLE = "/tmp/cw/eval_oracle.json"
OUTDIR = os.environ.get("DRIVE_OUTDIR", os.path.join(HERE, "baseline"))

STATUS_VARS = ["account_status", "outage_status", "network_status",
               "gateway_status", "wifi_status", "convoy_status",
               "convoy_routing_action", "cable_modem_mac", "accountNumber",
               "account_id", "device_id", "diagnostics_triggered",
               "outage_detected", "outage_message", "customer_message",
               "convoy_customer_message", "impacted_services",
               "technician_type", "activityType", "activityCode", "jobType",
               "intent_clarified", "intent_clarification_pending",
               "verdict_delivered", "authStatus"]

# Conversation logging lags the turn by ~15-30s; poll rather than fixed-sleep.
TRACE_DELAYS = (12, 8, 10, 15, 20)

_import_lock = threading.Lock()
_settings_done = False


def _setup():
    global _settings_done
    with _import_lock:
        if not _settings_done:
            from app.products.slot_studio.studio import state
            state.apply_settings(mode="hosted", project=PROJECT,
                                 location=LOCATION)
            _settings_done = True
        from app.products.slot_studio.studio.chat_session import ChatSession
    return ChatSession


# ------------------------------------------------------------------ seeding

def clean_vars(seed: dict) -> dict:
    """Strip extractor-added `_fact_*` keys; keep everything the app reads."""
    out = {}
    for k, v in (seed or {}).items():
        if k.startswith("_") or v is None:
            continue
        out[k] = v
    # `_fact_accountNumber` in a scenario eval is the number the simulated user
    # would speak; promote it so the deterministic path is exercised.
    for fk in ("_fact_accountNumber", "_fact_account_number"):
        val = str((seed or {}).get(fk) or "")
        if val.isdigit() and not out.get("accountNumber"):
            out["accountNumber"] = val
            out.setdefault("account_id", val)
    return out


def utterances_for(sc: dict) -> list[str]:
    """The ordered user turns to send for a scenario."""
    if sc.get("user_utterances"):
        return list(sc["user_utterances"])
    task = sc.get("scenario_task")
    return [task] if task else ["my internet is not working"]


# ------------------------------------------------------------------- state

def state_from_trace(session) -> tuple[dict, dict, str | None]:
    """Merge every variable_update in the conversation trace into one dict."""
    err = None
    for delay in TRACE_DELAYS:
        time.sleep(delay)
        try:
            norm = session.get_normalized_trace()
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:160]}"
            continue
        if not isinstance(norm, dict):
            continue
        merged, sm = {}, {}
        for e in norm.get("entries") or []:
            if e.get("kind") in ("variable_update", "variable_default"):
                v = e.get("variables") or {}
                if isinstance(v, dict):
                    for k, val in v.items():
                        if k == "sm" and isinstance(val, dict):
                            sm = val
                        merged[k] = val
        if not sm:
            try:
                from app.products.slot_studio.studio import trace_slots
                sm = trace_slots.slot_machine_from_trace(norm) or {}
            except Exception:  # noqa: BLE001
                pass
        merged.pop("sm", None)
        return merged, sm, None
    return {}, {}, err or "trace unavailable"


def final_state(merged: dict, sm: dict) -> dict:
    """The diagnostic end state: sm.filled WINS over session variables.

    The before_agent_callback leaves `network_status`/`gateway_status` at
    "PENDING_BACKEND_RESULT" in the session variables while the RESOLVED values
    land in the slot machine's `filled` map -- which is what the repair DAG
    actually evaluates.  So `filled` is authoritative for the status variables.
    """
    out = {}
    for k in STATUS_VARS:
        if k in merged:
            out[k] = merged[k]
    for k, v in ((sm or {}).get("filled") or {}).items():
        if k.startswith("_"):
            continue
        if k in STATUS_VARS or k in ("outage_message", "customer_message",
                                     "convoy_customer_message", "device_id"):
            out[k] = v
    return out


# ------------------------------------------------------------------- driver

def run_scenario(sc: dict, use_fakes=True, want_trace=True,
                 verbose=False) -> dict:
    ChatSession = _setup()
    seeded = clean_vars(sc.get("seeded_variables") or {})
    utts = utterances_for(sc)
    rec = {
        "scenario": sc["id"],
        "kind": sc.get("kind"),
        "app": APP_NAME,
        "seeded_variables": seeded,
        "mock_config_string": seeded.get("mock_config_string"),
        "utterances": utts,
        "use_tool_fakes": use_fakes,
        "turns": [],
        "final_state": {},
        "slot_machine_filled": {},
        "predicted": sc.get("prediction"),
        "golden_agent_text": sc.get("expected_agent_text"),
        "error": None,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    session = None
    try:
        session = flows.open_session(seeded, APP_NAME, session_factory=ChatSession,
                                     use_tool_fakes=use_fakes)
        rec["session_id"] = session.session_id
        for u in utts:
            if session.is_ended:
                rec["turns"].append({"user": u, "agent_text": None,
                                     "skipped": "session already ended"})
                break
            t = session.send(u)
            rec["turns"].append({
                "user": u,
                "agent_text": t.agent_text,
                "tool_calls": t.tool_calls,
                "tool_responses": [
                    {"action": r.get("action"),
                     "response": _shrink(r.get("response"))}
                    for r in (t.tool_responses or [])],
                "transfer": _transfer(t.agent_transfer),
                "session_ended": t.session_ended,
            })
            if verbose:
                print(f"[{sc['id']}] > {u[:70]}\n[{sc['id']}] < "
                      f"{(t.agent_text or '')[:200]}", flush=True)
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["traceback"] = traceback.format_exc()[-2000:]

    if want_trace and session is not None and rec["turns"]:
        merged, sm, terr = state_from_trace(session)
        rec["slot_machine_filled"] = {
            k: v for k, v in ((sm or {}).get("filled") or {}).items()
            if not k.startswith("_")}
        rec["final_state"] = final_state(merged, sm)
        rec["session_variables"] = {k: v for k, v in merged.items()
                                    if not k.startswith("_")
                                    and not str(k).isupper()}
        if terr:
            rec["trace_error"] = terr
    rec["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return rec


def _shrink(x, n=1200):
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s if len(s) <= n else s[:n] + "…<truncated>"


def _transfer(t):
    if t is None:
        return None
    if hasattr(t, "display_name"):
        return t.display_name
    if isinstance(t, dict):
        return t.get("display_name") or t.get("target_agent")
    return str(t)


# ---------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oracle", default=ORACLE)
    ap.add_argument("--outdir", default=OUTDIR)
    ap.add_argument("--scenario", action="append",
                    help="scenario id (repeatable)")
    ap.add_argument("--filter", help="substring filter on scenario id")
    ap.add_argument("--kind", choices=["golden", "scenario"])
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-fakes", action="store_true",
                    help="hit the real backends instead of tool_fake_config")
    ap.add_argument("--no-trace", action="store_true",
                    help="skip the trace read (much faster, no final_state)")
    ap.add_argument("--utterance", action="append",
                    help="ad-hoc probe: user turn (repeatable)")
    ap.add_argument("--vars", help="ad-hoc probe: JSON seed variables")
    ap.add_argument("--name", default="adhoc", help="ad-hoc probe: output name")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    os.makedirs(a.outdir, exist_ok=True)

    if a.utterance:
        scenarios = [{"id": a.name, "kind": "adhoc",
                      "seeded_variables": json.loads(a.vars or "{}"),
                      "user_utterances": a.utterance}]
    else:
        oracle = json.load(open(a.oracle))
        scenarios = oracle["scenarios"]
        if a.scenario:
            want = set(a.scenario)
            scenarios = [s for s in scenarios if s["id"] in want]
        if a.filter:
            scenarios = [s for s in scenarios if a.filter in s["id"]]
        if a.kind:
            scenarios = [s for s in scenarios if s["kind"] == a.kind]

    if not scenarios:
        print("no scenarios selected", file=sys.stderr)
        return 2
    print(f"driving {len(scenarios)} scenario(s) against {APP_NAME} "
          f"with {a.workers} worker(s), fakes={not a.no_fakes}", flush=True)

    _setup()  # warm the cxas import once, serially, before fanning out
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(run_scenario, s, not a.no_fakes, not a.no_trace,
                          a.verbose): s for s in scenarios}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                rec = {"scenario": s["id"], "error": f"{type(e).__name__}: {e}",
                       "traceback": traceback.format_exc()[-2000:],
                       "turns": []}
            with open(os.path.join(a.outdir, s["id"] + ".json"), "w") as f:
                json.dump(rec, f, indent=2, default=str)
            done += 1
            first = (rec.get("turns") or [{}])[0].get("agent_text") or ""
            flag = "ERR " if rec.get("error") else "    "
            print(f"[{done}/{len(scenarios)}] {flag}{s['id']:46s} "
                  f"{(rec.get('error') or first)[:110]!r}", flush=True)
    print(f"\ndone in {time.time() - t0:.0f}s -> {a.outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
