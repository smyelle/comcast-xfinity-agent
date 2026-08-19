"""Record real calls, step by step, for the lessons to teach from.

The architecture lessons are built around one conversation, decomposed. Written by hand,
that decomposition is prose about code, and it drifts the way every other hand-written
account in this repo has. So the walks are RECORDED: the real engine over the emitted
config, with no model and no network, and what comes out is what actually fired.

Each step records what happened, which tasks fired, which lines were heard, and which
slots became filled AT THAT STEP. The last of those is the teaching artifact. A reader
watching `verdict_delivered` appear at the moment the ladder stops walking has understood
the latch, and no paragraph does that as well.

The step vocabulary is `harness`'s own -- `say`, `gate`, `legs`, `specialists`, `walk`,
`fill` -- the same one `journey_scenarios.py` is written in and `journey_check.py` grades.
Driving the engine correctly is not obvious (the specialists are a two-phase remote job,
and answering an ineligible leg stalls the sweep), so this borrows the vocabulary rather
than re-deriving it and getting it subtly wrong.
"""

from __future__ import annotations

import harness
import journey_check
import scripts
from harness import fill, gate, legs, say, specialists, walk

DOWN = "hi my internet is down"

# Two calls, and no more. A third is a third thing to hold in mind.
#
# `suspended` is the cold open: the ladder answers on the turn the account arrives, before
# any diagnostic has run, which makes "one thing speaks, and it was CHOSEN" visible on the
# smallest complete example there is.
#
# `reboot` is the spine. It is the only path where a rung ASKS rather than concludes, so it
# is the one case where the choice of latch is visible: the offer latches `reboot_offered`
# and deliberately leaves the ladder open, and a second rung speaks later in the same call.
# Every other path closes on its first verdict and cannot demonstrate that.
WALKS = {
    "suspended": {
        "title": "An account on hold",
        "why": "The shortest complete call. The ladder answers before a single check runs.",
        "teaches": "One task speaks per turn, and it was chosen rather than composed.",
        "steps": [
            ("The caller says what is wrong", say(DOWN)),
            ("The engine walks, and asks for the account", walk()),
            ("The account comes back on hold", gate(account="suspended")),
            ("The ladder answers", walk()),
        ],
    },
    "reboot": {
        "title": "A gateway that needs restarting",
        "why": (
            "The only path where a rung asks instead of concluding, so the ladder stays "
            "open and a second rung speaks later in the same call."
        ),
        "teaches": "A latch decides whether the ladder closes or stays open.",
        "steps": [
            ("The caller says what is wrong", say(DOWN)),
            ("The engine walks, and asks for the account", walk()),
            ("The account checks out", gate()),
            ("The two local checks report", legs()),
            ("The specialists report a gateway fault", specialists(gw="reboot")),
            ("The ladder offers a restart", walk()),
            ("The caller accepts, one turn later", fill("set_confirm_reboot", confirm_reboot="yes")),
            ("The restart is sent", walk()),
        ],
    },
}


def _known(call: harness.Call) -> dict:
    """Slots carrying a value right now."""
    return {k: v for k, v in (call.filled or {}).items() if v not in (None, "")}


def record(app_dir: str, spec: dict) -> list[dict]:
    """Drive one call and return a step-by-step trace."""
    call = harness.Call(harness.load_config(app_dir))
    trace: list[dict] = []
    before = _known(call)

    for caption, step in spec["steps"]:
        lines, rungs = journey_check._run_step(step, call)
        after = _known(call)
        payload = step.payload or {}
        trace.append(
            {
                "caption": caption,
                "kind": step.kind,
                "you": payload.get("text") if step.kind == "say" else None,
                "rungs": list(rungs or []),
                # Collapsed because a `then_say` built from two constants arrives with the
                # join between them intact.
                "heard": [" ".join(t.split()) for t in (lines or []) if t and t.strip()],
                # The point of the artifact: what became known at THIS step.
                "filled": sorted(set(after) - set(before)),
                "known": len(after),
            }
        )
        before = after
    return trace


def all_walks(app_dir: str) -> dict:
    """Every recorded call, keyed by the name the lessons refer to it by."""
    out = {}
    for name, spec in WALKS.items():
        entry = {k: spec[k] for k in ("title", "why", "teaches")}
        try:
            entry["steps"] = record(app_dir, spec)
        except Exception as exc:
            # A walk that cannot be driven is reported, never invented. A lesson built on
            # a fabricated trace is worse than a lesson that is missing.
            entry["steps"] = []
            entry["error"] = f"{type(exc).__name__}: {exc}"
        out[name] = entry
    return out


if __name__ == "__main__":
    import json
    import sys

    _ = scripts  # fail here rather than at render if the copy module moves
    print(json.dumps(all_walks(sys.argv[1] if len(sys.argv) > 1 else "built"), indent=2))
