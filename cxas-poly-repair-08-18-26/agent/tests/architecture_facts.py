"""The parts of the architecture that are read from SOURCE rather than from the build.

`architecture_dump` reads the emitted app, which is the authority on what the agent does.
It cannot say where any of it lives, how to run it, or what holds it up, and those are the
first three questions anyone asks. Those answers are here, harvested rather than written:
file sizes and docstrings off the tree, targets and their reasoning out of the Makefile,
the test inventory off `tests/`, and the routing taxonomy out of its own JSON.

Harvested, not restated. Every string is either a measurement or the first line of a
docstring somebody already wrote next to the thing it describes, so the page cannot say
something the code does not.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = ROOT.parent

# Anything above this in a directory is worth listing individually; the tail is summarised.
BIG_FILE = 60


def _docline(source: str) -> str:
    """The first sentence of a module docstring, which is where this repo puts the point.

    Dashes are folded to a hyphen here rather than at each place that renders one. These
    are Python docstrings, written with no thought for the voice pipeline, and the
    workbench forbids en and em dashes throughout; folding once means no consumer has to
    remember to.
    """
    try:
        doc = ast.get_docstring(ast.parse(source)) or ""
    except SyntaxError:
        return ""
    first = doc.strip().split("\n\n")[0].replace("\n", " ").strip()
    return " ".join(first.split()).replace("\u2014", "-").replace("\u2013", "-")


def _entry(path: pathlib.Path) -> dict:
    source = path.read_text(errors="replace")
    return {
        "path": str(path.relative_to(REPO)),
        "name": path.name,
        "lines": source.count("\n") + 1,
        "summary": _docline(source),
    }


def code_map() -> list[dict]:
    """Where the agent lives, by area, largest first inside each."""
    areas = [
        (
            "The agent",
            "Authored here. `app.py` assembles the flows; everything else it imports.",
            sorted(ROOT.glob("*.py")),
        ),
        (
            "Journeys",
            "One module per behaviour the agent has. This is where a rung is added.",
            sorted(ROOT.glob("journeys/*.py")),
        ),
        (
            "Shared journey parts",
            "The factories and vocabulary every journey is built from.",
            sorted(ROOT.glob("journeys/common/*.py")),
        ),
        (
            "The specialist proxy",
            "A Cloud Run service, because the diagnostics cannot run inside a tool call.",
            sorted(ROOT.glob("specialist_proxy/*.py")),
        ),
        (
            "Checks",
            "Offline oracles. They read the EMITTED app, so they grade what deploys.",
            sorted(ROOT.glob("tests/*.py")),
        ),
    ]
    out = []
    for title, why, paths in areas:
        files = [_entry(p) for p in paths if p.name != "__init__.py"]
        files.sort(key=lambda f: -f["lines"])
        out.append(
            {
                "title": title,
                "why": why,
                "files": files,
                "lines": sum(f["lines"] for f in files),
            }
        )
    return out


def makefile_targets() -> list[dict]:
    """Every target, its one-line help, its recipe, and the comment block above it.

    The comment blocks in this Makefile are paragraphs of reasoning rather than notes, and
    they are the best short explanation of the build in the repo. They are lifted whole.
    """
    text = (REPO / "Makefile").read_text()
    out = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        match = re.match(r"^([a-z][a-z0-9-]*):\s*([a-z0-9- ]*?)\s*(?:##\s*(.*))?$", line)
        if not match:
            continue
        name, deps, help_text = match.group(1), match.group(2).split(), match.group(3)
        if not help_text:
            continue
        # The comment block immediately above, if there is one.
        why: list[str] = []
        j = i - 1
        while j >= 0 and lines[j].startswith("#"):
            why.insert(0, lines[j].lstrip("#").strip())
            j -= 1
        recipe = []
        for k in range(i + 1, len(lines)):
            if lines[k].startswith("\t"):
                recipe.append(lines[k].strip())
            elif lines[k].strip() == "" and recipe:
                continue
            elif lines[k].startswith("#") or lines[k].strip() == "":
                continue
            else:
                break
        out.append(
            {
                "name": name,
                "help": help_text,
                "deps": deps,
                "why": " ".join(why).strip(),
                "recipe": recipe,
            }
        )
    return out


def gated_checks(targets: list[dict]) -> list[str]:
    """Which check scripts `make check` actually runs, resolved through its dependencies."""
    by_name = {t["name"]: t for t in targets}
    seen: list[str] = []

    def walk(name: str) -> None:
        target = by_name.get(name)
        if not target:
            return
        for dep in target["deps"]:
            walk(dep)
        for line in target["recipe"]:
            hit = re.search(r"([\w/]+\.py)", line)
            if hit and hit.group(1) not in seen:
                seen.append(hit.group(1))

    walk("check")
    return seen


# `tests/` holds three different kinds of thing, and calling them all checks would
# misreport most of the directory. A driver is meant to be run by hand at a live app; an
# analysis script produces a number someone reads once. Only a CHECK asserts, and only a
# check is worth reporting as ungated.
_DRIVER = re.compile(r"^(drive|try|demo_(drive|voice)|cuj_|faq_drive|hook_drives|.*_probe)")
_ANALYSIS = re.compile(r"^(build_|derive_|gen_|split_|score_|measure_|.*_analysis|fixture_audit|crash_dump)")


def _kind(name: str) -> str:
    stem = name[:-3]
    if stem.endswith("_check") or stem.startswith("check_") or stem in {"branch_coverage", "hook_diff"}:
        return "check"
    if stem.startswith("architecture_"):
        return "support"
    if _DRIVER.match(stem):
        return "driver"
    if _ANALYSIS.match(stem):
        return "analysis"
    return "support"


def test_inventory(gated: list[str]) -> list[dict]:
    """Everything in `tests/`, classified, with whether anything runs it.

    Whether a CHECK is gated is the fact worth having: this repo has already had one drift
    into uselessness precisely because nothing ran it. Whether a driver is gated is not a
    question, so the classification comes first and the gating is only reported for checks.
    """
    gated_names = {pathlib.Path(g).name for g in gated}
    out = []
    for path in sorted(ROOT.glob("tests/*.py")):
        if path.name in {"__init__.py", "harness.py"}:
            continue
        entry = _entry(path)
        entry["kind"] = _kind(path.name)
        entry["gated"] = path.name in gated_names
        out.append(entry)
    order = {"check": 0, "driver": 1, "analysis": 2, "support": 3}
    out.sort(key=lambda e: (order[e["kind"]], not e["gated"], e["name"]))
    return out


def routing_tree() -> dict:
    """The intent taxonomy the steering router routes over."""
    data = json.loads((ROOT / "head_intents.json").read_text())
    categories = data.get("categories", {})
    return {
        "source": data.get("source", ""),
        "categories": [
            {
                "key": key,
                "description": (body or {}).get("description", "") if isinstance(body, dict) else "",
                "leaves": sorted((body or {}).get("leaves", {}).keys())
                if isinstance(body, dict) and isinstance(body.get("leaves"), dict)
                else sorted(body)
                if isinstance(body, list)
                else [],
            }
            for key, body in sorted(categories.items())
        ],
    }


def guardrails() -> list[dict]:
    """The guardrails, by kind, read off the calls that declare them."""
    source = (ROOT / "guardrails.py").read_text()
    out = []
    for kind, name in re.findall(r"flows\.(safety|blocklist|policy|prompt_guard)\(\s*\n?\s*\"([^\"]*)\"", source):
        out.append({"kind": kind, "name": name})
    return out


def hook_summary() -> list[dict]:
    """The two callbacks, and the state each one is responsible for.

    Parsed rather than described: the names of the slots each callback writes are read off
    its own source, so a callback that stops writing one drops off this list.
    """
    source = (ROOT / "hooks.py").read_text()
    out = []
    for match in re.finditer(r"^def (\w*callback\w*|\w*_callback)\(", source, re.M):
        name = match.group(1)
        body = source[match.end() :]
        end = re.search(r"\n(?=def )", body)
        body = body[: end.start()] if end else body
        writes = sorted(set(re.findall(r'filled\[[\'"](\w+)[\'"]\]\s*=', body)))
        state = sorted(set(re.findall(r'state\[[\'"](\w+)[\'"]\]\s*=', body)))
        out.append(
            {
                "name": name,
                "lines": body.count("\n"),
                "summary": " ".join((re.search(r'"""(.*?)"""', body, re.S).group(1) if '"""' in body[:400] else "").split())[:400],
                "fills": writes,
                "writes_state": state,
            }
        )
    return out
