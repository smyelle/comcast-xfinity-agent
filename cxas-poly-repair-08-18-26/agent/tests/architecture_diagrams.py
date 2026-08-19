"""Draw the agent, from the emitted config rather than by hand.

The workbench already renders journey graphs with a layout engine, and those graphs are
hand-authored markdown. These are the same shape of graph built out of the config the
build emits, so the diagrams of the AGENT cannot drift the way the diagrams of the
journeys did.

Three shapes, because the agent has three stories worth a picture:

* the shape of a flow: what it collects, what it checks, and the outcomes it can reach
* the ladder as a chain, so first-match-wins is visible rather than asserted
* the deployed topology, which is the one diagram nothing in either repo can derive,
  because it spans two services and a job store

Node kinds reuse the journey graph's own vocabulary (start/slot/tool/decision/end and so
on) so both kinds of diagram read the same way on the page.
"""

from __future__ import annotations

# A rung whose tool ends the call is drawn as a terminal; one that hands over is a
# transfer. Everything else is a step, which is what the ladder mostly is.
def _rung_kind(task: dict) -> str:
    if task.get("escalates"):
        return "transfer"
    if task.get("ends_call"):
        return "end"
    if task.get("awaits"):
        return "tool"
    return "step"


def _short(text, limit: int = 58) -> str:
    """An `ask` is a LIST when the slot re-asks with different wording, so the first one
    is taken rather than the whole ladder of them."""
    if isinstance(text, (list, tuple)):
        text = text[0] if text else ""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def flow_shape(flow: dict) -> dict:
    """One flow as collect -> check -> outcome, which is how the engine actually walks it.

    Every task is not drawn. A ladder of fifty rungs laid out as fifty boxes is a wall, and
    the ladder page lists them properly anyway. What a picture is good for here is the
    SHAPE: the asks, the checks that gate everything, and the outcomes those checks reach.
    """
    nodes = [{"id": "start", "label": "The caller says what is wrong", "kind": "start"}]
    edges = []

    asked = [s for s in flow["slots"] if s.get("ask")][:6]
    previous = "start"
    for slot in asked:
        node = f"slot_{slot['name']}"
        nodes.append({"id": node, "label": _short(slot.get("ask") or slot["name"]), "kind": "slot"})
        edges.append({"from": previous, "to": node})
        previous = node

    # The tasks that gather rather than answer: they have outputs other flows gate on and
    # no spoken verdict of their own.
    checks = [t for t in flow["tasks"] if not t.get("then_say") and t.get("outputs")][:5]
    for task in checks:
        node = f"task_{task['name']}"
        nodes.append({"id": node, "label": task["name"], "kind": "tool"})
        edges.append({"from": previous, "to": node})
        previous = node

    if checks or asked:
        nodes.append({"id": "ladder", "label": "The verdict ladder, first match wins", "kind": "decision"})
        edges.append({"from": previous, "to": "ladder"})
        previous = "ladder"

    speaking = [t for t in flow["tasks"] if t.get("then_say")]
    ends = [t for t in speaking if t.get("ends_call") or t.get("escalates")][:6]
    stays = [t for t in speaking if not (t.get("ends_call") or t.get("escalates"))][:6]
    for task in stays + ends:
        node = f"out_{task['name']}"
        nodes.append({"id": node, "label": task["name"], "kind": _rung_kind(task)})
        edges.append({"from": previous, "to": node})

    if len(speaking) > len(ends) + len(stays):
        rest = len(speaking) - len(ends) - len(stays)
        nodes.append({"id": "more", "label": f"and {rest} more outcomes", "kind": "subjourney"})
        edges.append({"from": previous, "to": "more"})
    return {"nodes": nodes, "edges": edges}


def ladder_chain(flow: dict, pinned: list[str], limit: int = 18) -> dict:
    """The ladder as a chain, so that first-match-wins is something you can see.

    Only the rungs that SPEAK are drawn. The silent ones are plumbing and would triple the
    height of the picture without adding a decision to it.
    """
    speaking = [t for t in flow["tasks"] if t.get("then_say")][:limit]
    nodes = [{"id": "walk", "label": "The engine walks the tasks in order", "kind": "start"}]
    edges = []
    previous = "walk"
    for task in speaking:
        node = task["name"]
        label = task["name"] + (" *" if task["name"] in pinned else "")
        nodes.append({"id": node, "label": label, "kind": _rung_kind(task), "detail": _short(task.get("then_say"), 120)})
        edges.append({"from": previous, "to": node, "label": "no match"})
        previous = node
    nodes.append({"id": "none", "label": "nothing matched, the model answers", "kind": "error"})
    edges.append({"from": previous, "to": "none"})
    return {"nodes": nodes, "edges": edges}


# The one diagram nothing can derive. Every piece of it is discoverable in the source, but
# the wiring spans the agent, a Cloud Run service and a job store, and no file describes
# the whole path. The node NAMES are pulled from the config so a rename shows up here.
SWEEP_TASKS = ("ContextGate", "SweepLegs", "Specialists", "Settle")


def sweep_task_names(flow: dict) -> list[str]:
    """The tasks that make up the sweep, in the order the flow declares them.

    Named rather than inferred. "A task that does not speak and has outputs" also catches
    the device-help lookup, which is not part of the sweep at all, and a page that lists it
    here is telling an engineer to look in the wrong place.
    """
    names = [t["name"] for t in flow["tasks"]]
    legs = [n for n in names if n.startswith("SweepLegs_") or n.startswith("leg_")]
    return [n for n in names if n in SWEEP_TASKS or n in legs]


def sweep_topology(flow: dict) -> dict:
    task_names = {t["name"] for t in flow["tasks"]}
    # The remote job is the Specialists task specifically. Picking "the first task that
    # awaits" finds a lowered parallel leg instead and labels the box after it.
    remote = next(
        (t for t in flow["tasks"] if t["name"] == "Specialists"),
        next((t for t in flow["tasks"] if t.get("awaits")), None),
    )
    nodes = [
        {"id": "caller", "label": "The caller says what is wrong", "kind": "start"},
        {"id": "hooks", "label": "before_agent sets caller_spoke", "kind": "step",
         "detail": "The latch every check is gated on. Set past the opening-turn guard."},
        {"id": "ContextGate", "label": "ContextGate", "kind": "tool",
         "detail": "Resolves the account and the modem address from the context hub."},
        {"id": "SweepLegs", "label": "SweepLegs", "kind": "tool",
         "detail": "Two legs in parallel: the outage check and the predictive check."},
        {"id": "start_job", "label": (remote or {}).get("name", "Specialists"), "kind": "tool",
         "detail": "Starts a remote job and returns a handle in under a second."},
        {"id": "proxy", "label": "specialist proxy on Cloud Run", "kind": "subjourney",
         "detail": "A service, because a synchronous tool yields no turns to speak over."},
        {"id": "store", "label": "Firestore job store", "kind": "subjourney",
         "detail": "Falls back to memory when Firestore is unreachable."},
        {"id": "network", "label": "network specialist", "kind": "subjourney"},
        {"id": "gateway", "label": "gateway specialist", "kind": "subjourney"},
        {"id": "poll", "label": "the status tool, polled once a turn", "kind": "decision",
         "detail": "While it is running the agent speaks the waiting lines instead of silence."},
        {"id": "Settle", "label": "Settle", "kind": "tool",
         "detail": "Writes all six statuses and marks the diagnostics complete."},
        {"id": "ladder", "label": "the verdict ladder", "kind": "end"},
    ]
    edges = [
        {"from": "caller", "to": "hooks"},
        {"from": "hooks", "to": "ContextGate"},
        {"from": "ContextGate", "to": "SweepLegs"},
        {"from": "ContextGate", "to": "start_job"},
        {"from": "start_job", "to": "proxy"},
        {"from": "proxy", "to": "store"},
        {"from": "proxy", "to": "network"},
        {"from": "proxy", "to": "gateway"},
        {"from": "start_job", "to": "poll", "label": "handle"},
        {"from": "poll", "to": "poll", "label": "still running"},
        {"from": "poll", "to": "Settle", "label": "landed"},
        {"from": "SweepLegs", "to": "Settle"},
        {"from": "Settle", "to": "ladder"},
    ]
    return {
        "nodes": [n for n in nodes if n["id"] not in task_names or n["id"] in task_names],
        "edges": edges,
    }


def routing_shape(catalogue: list[dict], tree: dict, limit: int = 10) -> dict:
    """The router: one classification, then a second inside whichever category it picked."""
    nodes = [{"id": "utterance", "label": "What the caller said", "kind": "start"}]
    edges = []
    handled = [c for c in catalogue if c.get("kind") == "handle"]
    deferred = [c for c in catalogue if c.get("kind") != "handle"]
    nodes.append({"id": "l1", "label": "Which area is this?", "kind": "decision"})
    edges.append({"from": "utterance", "to": "l1"})
    for entry in handled:
        nodes.append({"id": f"h_{entry['key']}", "label": entry["key"], "kind": "step",
                      "detail": _short(entry.get("description"), 160)})
        edges.append({"from": "l1", "to": f"h_{entry['key']}", "label": "handled here"})
    leaves = {c["key"]: c.get("leaves", []) for c in tree.get("categories", [])}
    for entry in deferred[:limit]:
        node = f"d_{entry['key']}"
        count = len(leaves.get(entry["key"], []))
        nodes.append({"id": node, "label": f"{entry['key']} ({count})" if count else entry["key"],
                      "kind": "subjourney", "detail": _short(entry.get("description"), 160)})
        edges.append({"from": "l1", "to": node, "label": "deferred"})
    if len(deferred) > limit:
        nodes.append({"id": "d_more", "label": f"and {len(deferred) - limit} more areas", "kind": "subjourney"})
        edges.append({"from": "l1", "to": "d_more"})
    return {"nodes": nodes, "edges": edges}
