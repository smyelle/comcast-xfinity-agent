#!/usr/bin/env python3
"""Head-to-head MULTI-TURN differ: converted app vs the original, turn by turn.

Why this exists
---------------
Every one of the 58 eval scenarios sends exactly ONE utterance, so the 50/58
fidelity number only ever compared the FIRST agent turn. Everything a repair
call actually does after that — the reboot handshake, the clarification branch,
a mid-flow request for a human, a corrected account number, follow-ups after a
verdict — was unmeasured against the original.

This drives BOTH apps through the same conversation and diffs each turn, so a
divergence is attributable to a specific exchange rather than to a score.

    python cuj_diff.py                       # every journey
    python cuj_diff.py --journey reboot_accept --verbose
    python cuj_diff.py --repeat 3            # re-run to separate real
                                             # divergence from model noise

The original is model-driven and genuinely nondeterministic (it also hits the
10-step reasoning cap under load), so a single differing run proves nothing.
`--repeat` reports how many runs diverged; treat 1-of-3 as noise and 3-of-3 as
a real difference.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

import flows  # noqa: E402

PROJECT, LOCATION = "ces-deployment-dev", "us"
ORIGINAL = os.environ.get("ORIG_APP_ID", "d4ec582c-034b-4126-83e6-057bf1b77a35")
CONVERTED = os.environ.get("MINE_APP_ID", "5fc33f37-19c2-4dee-a0c0-7e88c911f627")


def app_name(app_id):
    return f"projects/{PROJECT}/locations/{LOCATION}/apps/{app_id}"


# ------------------------------------------------------------------ scenarios

_CUJS = flows.load_cujs(start=os.path.dirname(os.path.abspath(__file__)))

CLEAR = _CUJS["all_clear"]
REBOOT = _CUJS["convoy_predictive_reboot"]
OUTAGE = _CUJS["area_outage"]
SUSPENDED = _CUJS["account_suspended"]
NETWORK = _CUJS["network_impaired"]
NO_ACCT = _CUJS["no_account"]

# name -> (seed variables, [user utterances])
JOURNEYS = {
    # --- the reboot handshake, both branches, plus what follows the answer
    "reboot_accept": (REBOOT, [
        "my internet is not working", "yes please", "how long will that take?"]),
    "reboot_decline": (REBOOT, [
        "my internet is not working", "no thanks", "what else can I do?"]),
    "reboot_unclear_then_yes": (REBOOT, [
        "my internet is not working", "hmm I'm not sure", "ok yes go ahead"]),

    # --- a delivered verdict must not be re-spoken on every follow-up
    "clear_then_followups": (CLEAR, [
        "my internet is not working", "ok thanks", "are you still there?"]),
    "clear_then_still_broken": (CLEAR, [
        "my internet is not working", "it's still not working"]),

    # --- the intent clarification gate, all three replies
    "clarify_only_app": (CLEAR, [
        "my Netflix won't load", "just Netflix, everything else is fine"]),
    "clarify_everything_down": (CLEAR, [
        "my Netflix won't load", "nothing works, other sites are down too"]),
    "clarify_unsure": (CLEAR, [
        "my Netflix won't load", "I'm not really sure"]),

    # --- asking for a human, at three different points in the call
    "agent_request_opening": (CLEAR, ["I want to speak to a real person"]),
    "agent_request_after_verdict": (CLEAR, [
        "my internet is not working", "can I talk to someone please"]),
    "agent_request_during_outage": (OUTAGE, [
        "my internet is not working", "let me talk to a human"]),

    # --- account number collection: the paths the single-turn corpus can't reach
    "acct_hold_then_number": (NO_ACCT, [
        "my internet is not working. hold on, I need a moment to find my account number",
        "ok it's 8069100230359946"]),
    "acct_invalid_then_valid": (NO_ACCT, [
        "my internet is not working, and my account number is.",
        "8069100230359946"]),
    "acct_correction": (NO_ACCT, [
        "my internet is down, account 1234", "sorry, I meant 8069100230359946"]),

    # --- precedence and hand-offs beyond turn 1
    "suspended_then_question": (SUSPENDED, [
        "my internet is not working", "why is it suspended?"]),
    "network_then_when": (NETWORK, [
        "my internet is not working", "when will the technician come?"]),
    "outage_then_eta": (OUTAGE, [
        "my internet is not working", "when will it be fixed?"]),

    # --- off-topic mid-flow
    "offtopic_then_back": (CLEAR, [
        "my internet is not working", "what's the weather like?",
        "anyway is my internet fixed"]),
}

_lock = threading.Lock()
_ready = False


def _session(app_id, seed):
    global _ready
    with _lock:
        if not _ready:
            from app.products.slot_studio.studio import state
            state.apply_settings(mode="hosted", project=PROJECT, location=LOCATION)
            _ready = True
        from app.products.slot_studio.studio.chat_session import ChatSession
    # The factory is passed explicitly: apply_settings above is not thread-safe and
    # these journeys run concurrently, so it stays behind the lock.
    return flows.open_session(seed, app_name(app_id), session_factory=ChatSession)


def _normalize_raw(s):
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)   # markdown link -> its label
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _filler_openers():
    """Every phrase a latency filler can prepend, read from scripts.py itself.

    Read rather than listed here so a new pool cannot silently start reporting as
    a divergence — the same reason the routing block is generated from the
    catalogue. Longest first, so "okay one moment" is stripped before "okay".
    """
    import scripts
    out = []
    for name in dir(scripts):
        if not name.startswith("FILLER"):
            continue
        v = getattr(scripts, name)
        out.extend([v] if isinstance(v, str) else [x for x in v if isinstance(x, str)])
    return sorted({_normalize_raw(x) for x in out if x}, key=len, reverse=True)


_FILLERS = None


def normalize(s):
    """Case/punctuation/whitespace-tolerant compare key.

    Two agents phrasing the same sentence with different capitalisation, a
    unicode dash, or a markdown link must not read as a divergence.

    A leading FILLER is stripped for the same reason. The pools pick at RANDOM per
    turn, so without this the differ reports pure wording as behavioural change:
    measured with the SAME app on both sides, 9 of 18 journeys diverged at least
    once and 5 diverged on all three runs — every one of them a filler swap
    ("Okay, let's do that." against "Sure thing."). That is what made this tool's
    own rule, "diverged on EVERY run = real, not model noise", read backwards.
    """
    global _FILLERS
    if _FILLERS is None:
        _FILLERS = _filler_openers()
    n = _normalize_raw(s)
    for f in _FILLERS:
        if f and n.startswith(f + " "):
            return n[len(f) + 1:]
    return n


def _say(text):
    """Collapse the known transcript-mirror duplicate (see README)."""
    t = (text or "").strip()
    half = len(t) // 2
    if half and t[:half].strip() == t[half:].strip():
        t = t[:half].strip()
    return t


def drive(app_id, seed, utterances):
    """Return a list of {user, agent, tools, ended, error} — never raises."""
    out = []
    try:
        s = _session(app_id, seed)
    except Exception as e:  # noqa: BLE001
        return [{"user": utterances[0], "agent": "", "tools": [],
                 "error": f"{type(e).__name__}: {e}"[:140]}]
    for u in utterances:
        if s.is_ended:
            out.append({"user": u, "agent": "", "tools": [], "ended_before": True})
            continue
        try:
            t = s.send(u)
        except Exception as e:  # noqa: BLE001
            out.append({"user": u, "agent": "", "tools": [],
                        "error": f"{type(e).__name__}: {e}"[:140]})
            break
        out.append({"user": u, "agent": _say(t.agent_text),
                    "tools": [c.get("action") for c in (t.tool_calls or [])],
                    "ended": bool(t.session_ended)})
    return out


def compare(name, seed, utterances):
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_o = ex.submit(drive, ORIGINAL, seed, utterances)
        f_m = ex.submit(drive, CONVERTED, seed, utterances)
        orig, mine = f_o.result(), f_m.result()
    turns = []
    for i in range(max(len(orig), len(mine))):
        o = orig[i] if i < len(orig) else {}
        m = mine[i] if i < len(mine) else {}
        no, nm = normalize(o.get("agent") or ""), normalize(m.get("agent") or "")
        turns.append({
            "i": i, "user": o.get("user") or m.get("user"),
            "orig": o.get("agent") or "", "mine": m.get("agent") or "",
            "orig_err": o.get("error"), "mine_err": m.get("error"),
            "same": bool(no) and no == nm,
            "orig_blank": not no, "mine_blank": not nm,
        })
    return {"name": name, "turns": turns,
            "diverged": [t for t in turns if not t["same"]]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--journey", action="append")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    names = a.journey or list(JOURNEYS)
    _session(CONVERTED, {})  # warm the import serially

    tally = {n: 0 for n in names}
    errors, last = {}, {}
    for r in range(a.repeat):
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(compare, n, *JOURNEYS[n]): n for n in names}
            for fut, n in futs.items():
                try:
                    rep = fut.result()
                except Exception as e:  # noqa: BLE001
                    # A harness error is NOT agreement. Counting it as a
                    # divergence stops a broken run reporting "identical" — which
                    # it did, cheerfully, when an import failed and every journey
                    # came back green without a single turn being compared.
                    print(f"  {n}: HARNESS ERROR {type(e).__name__}: {e}")
                    tally[n] += 1
                    errors[n] = f"{type(e).__name__}: {e}"
                    continue
                if rep["diverged"]:
                    tally[n] += 1
                last[n] = rep
        print(f"-- run {r + 1}/{a.repeat} done", flush=True)

    print("\n" + "=" * 100)
    for n in names:
        rep = last.get(n)
        if not rep:
            print(f"\n### {n}   NOT COMPARED — {errors.get(n, 'no result')}")
            continue
        d = tally[n]
        verdict = ("IDENTICAL" if d == 0 else
                   f"DIVERGED {d}/{a.repeat}")
        print(f"\n### {n}   {verdict}")
        for t in rep["turns"]:
            mark = "  " if t["same"] else "!!"
            if t["same"] and not a.verbose:
                print(f" {mark} [{t['i']}] > {(t['user'] or '')[:60]}   (identical)")
                continue
            print(f" {mark} [{t['i']}] > {(t['user'] or '')[:70]}")
            print(f"      ORIG: {(t['orig_err'] or t['orig'])[:230]!r}")
            print(f"      MINE: {(t['mine_err'] or t['mine'])[:230]!r}")
    n_div = sum(1 for n in names if tally[n])
    print("\n" + "=" * 100)
    print(f"{len(names)} journeys | identical every run: {len(names) - n_div} "
          f"| diverged at least once: {n_div}")
    always = [n for n in names if tally[n] == a.repeat and a.repeat > 1]
    if always:
        print(f"diverged on EVERY run (real, not model noise): {always}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
