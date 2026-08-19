"""Describe the agent that ships, as data, so a page about it cannot drift.

Two documents in this repo already try to explain the architecture in prose, and both
have drifted without anything noticing: `AGENTS.md` still says "one agent, one flow" when
there are four rooted flows, and quotes a rung count from before the ladder doubled;
`flows-sdk/README.md` describes a flat intent router that no longer exists and carries a
file table whose line counts are all wrong. Prose about code, maintained by hand, with no
gate, goes stale. That is the same failure the journey documents had.

So the facts are dumped from the artifact instead. Everything here is read from the
EMITTED app dir - the same bytes `cxas push` deploys - plus the source tables that
`make oracles` already holds to it. The reasons stay hand-written, because they are not
derivable from anything, but the ids those reasons name are checked against this file.

    uv run python tests/architecture_dump.py --app-dir built --out ../docs/architecture.json

Run it after `make agent`. Scoring a stale build is a mistake this repo has made before,
so the dump records which build it read and refuses one that was not built from this tree.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import architecture_diagrams
import architecture_facts
import architecture_walks
import branch_coverage
import order_check
from harness import load_config

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_config
import source_tools
from journeys.common import status as status_vocab

# The flows a reader cares about. The steering router emits a further thirteen leaf flows,
# one per deferred intent category, which are generated rather than authored and say
# nothing an engineer needs; they are counted, not dumped.
AUTHORED_FLOWS = ("repair", "reboot", "human", "steering")

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE_DIR = ROOT / "journeys"

# A dump smaller than this means it stopped reading the agent rather than that the agent
# shrank. The same floor idea as the coverage gate, for the same reason.
MIN_TASKS = 40


def flow_names(app_dir: str) -> list[str]:
    """Every flow the build emitted, by the `<name>_dag` tool it wrote."""
    tools = pathlib.Path(app_dir) / "tools"
    return sorted(p.name[: -len("_dag")] for p in tools.glob("*_dag"))


def condition_leaves(condition) -> list[dict]:
    """Every decidable leaf in a condition tree, flattened.

    `branch_coverage` already knows how to walk `all` / `any` / `not`, and it is gated,
    so the walk is borrowed rather than written a second time and left to disagree.
    """
    out: list[dict] = []
    branch_coverage._leaves(condition, out)
    return out


def dump_task(task: dict) -> dict:
    """One engine task, as a page would show it."""
    return {
        "name": task.get("name"),
        "tool": task.get("tool"),
        "inputs": task.get("inputs", []),
        "outputs": list((task.get("outputs") or {}).keys()),
        "requires": task.get("requires", []),
        "condition": task.get("condition"),
        "condition_leaves": condition_leaves(task.get("condition")),
        "then_say": task.get("then_say"),
        "filler_say": task.get("filler_say"),
        # `then_response` carries the end_session part, which is the only thing that ends
        # a call, so whether a rung is an ending is worth stating rather than implying.
        "ends_call": any(
            part.get("type") == "end_session" for part in (task.get("then_response") or [])
        ),
        "escalates": any(
            part.get("escalated") for part in (task.get("then_response") or [])
        ),
        "terminal": task.get("terminal", False),
        "awaits": bool(task.get("awaits")),
        "on_failure": task.get("on_failure"),
    }


def dump_slot(slot: dict) -> dict:
    """One slot, as a page would show it."""
    return {
        "name": slot.get("name"),
        "kind": slot.get("kind"),
        "setter": slot.get("setter"),
        "ask": slot.get("ask"),
        "option_cues": slot.get("option_cues"),
        "condition": slot.get("condition"),
        "requires": slot.get("requires"),
        "shared": slot.get("shared", False),
        "default": slot.get("default"),
        "publish": slot.get("publish"),
    }


def dump_flow(app_dir: str, name: str) -> dict:
    config = load_config(app_dir, name)
    return {
        "name": name,
        "slots": [dump_slot(s) for s in config.get("slots", [])],
        # Declaration order IS the priority ladder, first match wins, so the order of this
        # list is load-bearing information rather than an implementation detail.
        "tasks": [dump_task(t) for t in config.get("tasks", [])],
        "policies": {
            key: config.get(key)
            for key in ("bootstrap", "escalate", "cancel", "no_input", "steer_back")
            if config.get(key) is not None
        },
        "shared_slots": config.get("shared_slots"),
        "variable_maps": config.get("variable_maps"),
        "remote_tools": config.get("remote_tools"),
    }


def dump_ladder() -> dict:
    """The annotated ladder, and the contests that decide what a caller hears.

    `order_check` holds all three of these tables to the emitted config on every run of
    `make oracles`, including failing when a pair STOPS being a contest, so they are the
    one description of the priority order that cannot quietly go wrong.
    """
    return {
        "order": order_check.ORDER,
        "last": {flow: {"task": task, "why": why} for flow, (task, why) in order_check.LAST.items()},
        # Each overlap is a state on which both rungs are active, so the order is what
        # decides. The reason is already written next to it, in English.
        "overlaps": [
            {"winner": winner, "loser": loser, "state": state, "why": why}
            for winner, loser, state, why in order_check.OVERLAPS
        ],
        "exclusive": [
            {"first": first, "second": second, "state": state, "why": why}
            for first, second, state, why in order_check.EXCLUSIVE
        ],
    }


def dump_tools() -> dict:
    """The tool surface, split by who is allowed to call what."""
    return {
        "carried": list(source_tools.CARRIED_TOOLS),
        # Callable by the engine but hidden from the model, each for an observed failure.
        "engine_only": list(source_tools.ENGINE_ONLY_TOOLS),
        "rung_executors": list(source_tools.RUNG_TOOLS),
        "specialist_agents": list(source_tools.SPECIALIST_AGENTS),
    }


def dump_routing() -> dict:
    """What the steering router can route to, when it is switched on."""
    catalogue = getattr(source_tools, "ROUTE_CATALOGUE", [])
    return {
        "catalogue": [
            {"key": entry[0], "kind": entry[1], "description": entry[2]}
            if isinstance(entry, (list, tuple)) and len(entry) >= 3
            else {"key": str(entry)}
            for entry in catalogue
        ],
    }


_RUNG_CALL = re.compile(
    r"(?:say_rung|advice_rung|reboot_rung|offer_rung|rung|flows\.task|flows\.announce)"
    r'\(\s*\n?\s*"([A-Za-z_][A-Za-z0-9_]*)"'
)


def dump_modules() -> list[dict]:
    """One entry per journey module: where it is, how big, and what it declares.

    This is the bridge between a rung name on a page and the file someone has to open,
    which is the first thing an engineer wants and the one thing the emitted config cannot
    tell them, because the emitted ladder is flat and says nothing about who declared what.
    """
    out = []
    for path in sorted(MODULE_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        source = path.read_text()
        out.append(
            {
                "name": path.stem,
                "path": str(path.relative_to(ROOT.parent)),
                "lines": source.count("\n") + 1,
                "summary": architecture_facts._docline(source),
                "rungs": list(dict.fromkeys(_RUNG_CALL.findall(source))),
                "spoken_lines": sorted(set(re.findall(r"^(SAY_[A-Z0-9_]+|ASK_[A-Z0-9_]+) = ", source, re.M))),
            }
        )
    return out


def build(app_dir: str) -> dict:
    emitted = flow_names(app_dir)
    flows = [dump_flow(app_dir, name) for name in AUTHORED_FLOWS if name in emitted]
    task_total = sum(len(f["tasks"]) for f in flows)
    if task_total < MIN_TASKS:
        raise SystemExit(
            f"architecture_dump: found only {task_total} engine tasks across "
            f"{len(flows)} flows, expected at least {MIN_TASKS}. This dump is no longer "
            f"reading the agent."
        )
    manifest = build_config.read_manifest(app_dir)
    if dataclasses.is_dataclass(manifest):
        manifest = dataclasses.asdict(manifest)
    targets = architecture_facts.makefile_targets()
    gated = architecture_facts.gated_checks(targets)
    tests = architecture_facts.test_inventory(gated)
    tree = architecture_facts.routing_tree()
    routing = dump_routing()
    by_name = {f["name"]: f for f in flows}
    repair = by_name.get("repair")

    diagrams = {
        name: architecture_diagrams.flow_shape(flow)
        for name, flow in by_name.items()
        if flow["tasks"]
    }
    if repair:
        diagrams["ladder_chain"] = architecture_diagrams.ladder_chain(
            repair, order_check.ORDER.get("repair", [])
        )
        diagrams["sweep"] = architecture_diagrams.sweep_topology(repair)
        sweep_names = architecture_diagrams.sweep_task_names(repair)
    diagrams["routing"] = architecture_diagrams.routing_shape(routing["catalogue"], tree)

    return {
        # How the app under description was built. A page that does not say this invites
        # the reader to assume the demo build and the deployable are the same agent.
        "build": manifest,
        "flows": flows,
        "generated_flows": sorted(set(emitted) - set(AUTHORED_FLOWS)),
        "modules": dump_modules(),
        "ladder": dump_ladder(),
        "tools": dump_tools(),
        "routing": {**routing, "tree": tree},
        "guardrails": architecture_facts.guardrails(),
        "hooks": architecture_facts.hook_summary(),
        "code_map": architecture_facts.code_map(),
        "commands": targets,
        "tests": tests,
        "diagrams": diagrams,
        # Recorded, not written: the lessons decompose these rather than describing them.
        "walks": architecture_walks.all_walks(app_dir),
        "sweep_tasks": sweep_names if repair else [],
        "status_vocabulary": list(status_vocab.SHARED_STATUS),
        "totals": {
            "flows_emitted": len(emitted),
            "flows_authored": len(flows),
            "tasks": task_total,
            "slots": sum(len(f["slots"]) for f in flows),
            "modules": len(dump_modules()),
            "checks": sum(1 for t in tests if t["kind"] == "check"),
            "checks_gated": sum(1 for t in tests if t["kind"] == "check" and t["gated"]),
            "drivers": sum(1 for t in tests if t["kind"] == "driver"),
            "source_lines": sum(area["lines"] for area in architecture_facts.code_map()),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", default="built", help="The emitted app dir to read.")
    parser.add_argument("--out", default="../docs/architecture.json", help="Where to write.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed dump differs from what this build produces.",
    )
    args = parser.parse_args()

    data = build(args.app_dir)
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    out = pathlib.Path(args.out)

    if args.check:
        if not out.exists():
            print(f"architecture: {out} does not exist; run without --check", file=sys.stderr)
            return 1
        if out.read_text() != text:
            print(
                f"architecture: {out} is stale. The agent changed and the dump did not. "
                f"Re-run `make architecture`.",
                file=sys.stderr,
            )
            return 1
        print(f"architecture: {out} matches the build")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    totals = data["totals"]
    print(
        f"architecture: {totals['flows_authored']} authored flows "
        f"({totals['flows_emitted']} emitted), {totals['tasks']} tasks, "
        f"{totals['slots']} slots -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
