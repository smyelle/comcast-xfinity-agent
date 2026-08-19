"""Where a turn's time actually goes: LLM, guardrail, callback and tool, per span.

Every other latency instrument here measures a TOTAL. `latency_check.py` times the whole
turn and says whether the engine opened it; the audio drivers time end-of-speech to first
sample. Neither can say which component spent the time, so "the guardrails are slow" and
"the router is slow" have been indistinguishable — and `guard_check.py`, the only thing
that reads guardrail telemetry at all, records `(name, triggered, reason)` and has no
clock.

It turns out the platform already reports this. A CES `Span` carries `duration` alongside
`attributes`, and the spans are typed: `LLM`, `Guardrail`, `Callback`, `Tool`. So one text
drive prices every component of every turn, with no deployed A/B and no audio harness.
`cxas_scrapi.utils.latency_parser.LatencyParser` already walks that tree into per-row
durations, so this is a driver around it rather than a new parser.

Two things this answers that nothing else could:

  * **What a guardrail costs.** Per rule, per turn, in ms.
  * **Whether a guardrail is evaluated once per TURN or once per reasoning PASS.** Count
    `Guardrail` spans against `LLM` spans on the same turn. A routing turn runs Pass A
    (classify) then Pass B (act), so it carries at least two `LLM` spans; if the guardrail
    count tracks the pass count rather than staying flat, each extra pass is paying the
    judges again.

`MessageToDict` is called with `preserving_proto_field_name=True` on purpose: the walk
recurses on `child_spans`, and the default camelCase conversion yields `childSpans`, so
it would visit only the root and every component would read as 0ms.

Two reasons this does not just call `LatencyParser._process_spans`, which is the obvious
reuse and was tried first. It sums INCLUSIVE durations, but a `Tool` span sits inside the
`Callback` span that invoked it, so callback+tool double-counts the same milliseconds and
the components add up to more than the turn. And it matches span names exactly against
`{LLM, Guardrail, Callback, Tool}`, which silently drops the `FakeTool: <name>` and
`Async Tool` spans this app actually emits. So the walk below attributes SELF time —
a span's duration minus its children's — which partitions the root exactly.

    python tests/latency_attribution.py --app <APP_NAME>
    python tests/latency_attribution.py --app <APP_NAME> --cuj all_clear --turns "..."

Tool fakes are ON by default, via `flows.open_session` — the same seeding `guard_check.py`
documents, and without which the drive never reaches a verdict. That makes `Tool` rows
meaningless and everything else cleaner, since a 20s live sweep otherwise swamps the
components being measured. Pass `--real-tools` to time tools instead.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

from google.protobuf.json_format import MessageToDict  # noqa: E402

_CUJS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "cujs.yaml")

# Enough to reach a verdict, then one free-text follow-up. The follow-up matters: an
# engine-preempted turn carries no diagnostic span at all (guard_check.py says why), so a
# drive made only of preempts measures nothing.
DEFAULT_TURNS = ["my internet is not working", "is everything really fine?"]


def _spans(resp) -> list[dict]:
  """The root span of every output on this response, as plain dicts."""
  roots = []
  for out in getattr(resp, "outputs", []) or []:
    diag = getattr(out, "diagnostic_info", None)
    root = getattr(diag, "root_span", None) if diag else None
    if root is None:
      continue
    roots.append(MessageToDict(root._pb if hasattr(root, "_pb") else root,
                               preserving_proto_field_name=True))
  return roots


def _ms(duration) -> float:
  """A protobuf Duration as rendered by MessageToDict: the string `'1.450s'`."""
  return float(str(duration).rstrip("s")) * 1000 if duration else 0.0


def _bucket(span_name: str) -> str:
  """Which component a span belongs to.

  Prefix matching, not equality: the platform emits `FakeTool: <name>` and `Async Tool`
  alongside plain `Tool`, and an exact match drops them.
  """
  if span_name.startswith(("Tool", "FakeTool", "Async Tool")):
    return "tool"
  return {"LLM": "llm", "Guardrail": "guardrail", "Callback": "callback"}.get(
      span_name, "other")


def _turn_rows(roots: list[dict], turn_idx: int) -> dict[str, list]:
  """Every span of one turn as `(component, label, self_ms, total_ms)` rows.

  Self time is the span's own duration less its children's, so summing every row
  reconstructs the turn rather than over-counting the nesting.
  """
  rows: dict[str, list] = {"tool": [], "callback": [], "guardrail": [], "llm": [],
                           "other": [], "root": []}

  def walk(span, depth):
    total = _ms(span.get("duration"))
    children = span.get("child_spans") or []
    self_ms = total - sum(_ms(c.get("duration")) for c in children)
    name = span.get("name") or "?"
    attrs = span.get("attributes") or {}
    if depth == 0:
      rows["root"].append({"label": name, "self_ms": self_ms, "total_ms": total})
    else:
      rows[_bucket(name)].append({
          "label": attrs.get("name") or attrs.get("stage") or name,
          "detail": attrs.get("description", ""),
          "self_ms": self_ms, "total_ms": total,
          "model": attrs.get("model", ""),
          "input_tokens": attrs.get("input token count", 0),
      })
    for child in children:
      walk(child, depth + 1)

  for root in roots:
    walk(root, 0)
  return rows


def drive(app_name: str, turns: list[str], cuj: str | None,
          real_tools: bool) -> list[dict]:
  """One seeded session; per turn, the caller text and the component rows.

  Wraps `session._sessions.run` rather than reading what `send()` returns, because the
  spans live on the raw protobuf that `send()` does not hand back. Same reason
  `guard_check.py` does it.
  """
  import flows  # noqa: PLC0415

  seed = flows.cuj_variables(cuj, _CUJS) if cuj else {}
  session = (flows.open_session(seed, app_name) if not real_tools
             else flows.open_session(seed, app_name, use_tool_fakes=False))
  captured: list = []
  inner = session._sessions.run

  def capture(*a, **kw):
    resp = inner(*a, **kw)
    captured.append(resp)
    return resp

  session._sessions.run = capture

  out = []
  for i, text in enumerate(turns, start=1):
    if session.is_ended:
      break
    rec = session.send(text)
    resp = captured[-1] if captured else None
    out.append({"caller": text,
                "agent": (rec.agent_text or "(none)")[:100],
                "rows": _turn_rows(_spans(resp), i) if resp is not None else None})
  return out


_COMPONENTS = ("llm", "guardrail", "callback", "tool", "other")


def _total(rows: list) -> float:
  return sum(r["self_ms"] for r in rows)


def _report(drives: list[dict]) -> None:
  for n, turn in enumerate(drives, start=1):
    print(f"\n--- turn {n}")
    print(f"  > {turn['caller']}")
    print(f"  < {turn['agent']}")
    rows = turn["rows"]
    if rows is None or not any(rows.values()):
      print("    (no diagnostic span on this turn — engine preempt, not observable)")
      continue

    root = _total(rows["root"]) + sum(_total(rows[c]) for c in _COMPONENTS)
    print(f"    turn {root:8.0f} ms total")
    for comp in _COMPONENTS:
      spans = rows[comp]
      if not spans:
        continue
      share = 100 * _total(spans) / root if root else 0
      print(f"    {comp:<9} {len(spans):>3} span(s) {_total(spans):8.0f} ms  {share:4.1f}%")
      for r in sorted(spans, key=lambda x: -x["self_ms"])[:6]:
        extra = f"  in={r['input_tokens']}" if r["model"] else ""
        print(f"                {r['self_ms']:8.0f} ms  {r['label']} {r['detail']}{extra}")

  observed = [t for t in drives if t["rows"] and any(t["rows"].values())]
  if not observed:
    print("\nNo turn carried a diagnostic span. Nothing was measured.")
    return

  print("\n=== per turn, across the turns that carried spans")
  for comp in _COMPONENTS:
    per_turn = [_total(t["rows"][comp]) for t in observed]
    counts = [len(t["rows"][comp]) for t in observed]
    print(f"  {comp:<10} median {statistics.median(per_turn):8.0f} ms"
          f"   range {min(per_turn):.0f}-{max(per_turn):.0f}"
          f"   spans/turn {min(counts)}-{max(counts)}")

  print("\n=== cost by span label (total self ms across the drive, top 15)")
  by_label: dict[str, list] = {}
  for t in observed:
    for comp in _COMPONENTS:
      for r in t["rows"][comp]:
        by_label.setdefault(f"{comp}:{r['label']} {r['detail']}".strip(),
                            []).append(r["self_ms"])
  for label, ds in sorted(by_label.items(), key=lambda kv: -sum(kv[1]))[:15]:
    print(f"  {sum(ds):8.0f} ms  x{len(ds):<3} {label}")

  # The per-turn / per-pass question. A guardrail count that stays flat while the pass
  # count varies means the judges are billed once per turn; counts that track each other
  # mean once per pass.
  passes = [len(t["rows"]["llm"]) for t in observed]
  guards = [len(t["rows"]["guardrail"]) for t in observed]
  print(f"\n=== passes per turn {passes}  vs  guardrail evals per turn {guards}")


def _drive_totals(drives: list[dict]) -> dict[str, float] | None:
  """One drive reduced to per-component totals over the whole session.

  `None` when no turn carried a span, which is the ~1-in-5 empty session rather than a
  fast arm. Counting it as zero would make an arm look better the more often it flaked.
  """
  observed = [t for t in drives if t["rows"] and any(t["rows"].values())]
  if not observed:
    return None
  totals = {c: sum(_total(t["rows"][c]) for t in observed) for c in _COMPONENTS}
  totals["turn"] = sum(totals.values()) + sum(_total(t["rows"]["root"]) for t in observed)
  # The confounder. How many times the engine re-entered is chosen by the MODEL — how many
  # tasks it dispatched — so it varies run to run independently of anything being measured,
  # and each re-entry is worth ~250ms. Comparing two arms without it compares dice rolls.
  totals["engine_calls"] = sum(
      1 for t in observed for r in t["rows"]["tool"]
      if r["label"] == "slot_filling_engine")
  return totals


def _ab(app_a: str, app_b: str, turns: list[str], cuj: str, real_tools: bool,
        repeat: int) -> None:
  """Alternate the two arms and compare them.

  Alternated rather than blocked, because a block measures backend drift as much as the
  change: whichever arm runs second inherits whatever the platform was doing by then.
  """
  runs: dict[str, list] = {app_a: [], app_b: []}
  per_call: dict[str, dict[str, list]] = {app_a: {}, app_b: {}}
  for i in range(repeat):
    for app in (app_a, app_b):
      drives = drive(app, turns, cuj, real_tools)
      totals = _drive_totals(drives)
      runs[app].append(totals)
      if totals is not None:
        for t in drives:
          for comp in _COMPONENTS:
            for r in (t["rows"] or {}).get(comp, []):
              per_call[app].setdefault(f"{comp}:{r['label']}", []).append(r["self_ms"])
      state = "empty session, discarded" if totals is None else f"{totals['turn']:.0f} ms"
      print(f"  run {i + 1} {'A' if app == app_a else 'B'}  {state}")

  print(f"\n=== A = {app_a}\n=== B = {app_b}")
  print(f"{'component':<12}{'A median':>12}{'B median':>12}{'delta':>12}   n")
  for comp in ("turn", *_COMPONENTS, "engine_calls"):
    a = [r[comp] for r in runs[app_a] if r]
    b = [r[comp] for r in runs[app_b] if r]
    if not a or not b:
      continue
    ma, mb = statistics.median(a), statistics.median(b)
    unit = "   " if comp == "engine_calls" else " ms"
    print(f"{comp:<12}{ma:>10.0f}{unit}{mb:>10.0f}{unit}{mb - ma:>+10.0f}{unit}"
          f"   {len(a)}/{len(b)}")
  for label, app in (("A", app_a), ("B", app_b)):
    got = [r["turn"] for r in runs[app] if r]
    if got:
      print(f"  {label} turn totals: {', '.join(f'{v:.0f}' for v in sorted(got))}")

  # Per-CALL medians, which is where a change to one tool actually shows. A session total
  # mixes the change with however many times the model happened to invoke the thing, so
  # the per-call number is both the smaller claim and the better-evidenced one.
  print(f"\n{'per call':<26}{'A median':>12}{'B median':>12}{'delta':>12}   calls")
  labels = sorted({lb for app in (app_a, app_b) for lb in per_call[app]},
                  key=lambda lb: -statistics.median(per_call[app_a].get(lb) or [0]))
  for lb in labels[:10]:
    a, b = per_call[app_a].get(lb), per_call[app_b].get(lb)
    if not a or not b:
      continue
    ma, mb = statistics.median(a), statistics.median(b)
    print(f"{lb[:25]:<26}{ma:>10.0f} ms{mb:>10.0f} ms{mb - ma:>+10.0f} ms"
          f"   {len(a)}/{len(b)}")


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--app", required=True)
  ap.add_argument("--vs", help="second app; alternate the two and compare")
  ap.add_argument("--repeat", type=int, default=1)
  ap.add_argument("--cuj", default="all_clear")
  ap.add_argument("--turns", nargs="*", default=DEFAULT_TURNS)
  ap.add_argument("--real-tools", action="store_true",
                  help="drive real backends, so Tool rows mean something")
  args = ap.parse_args()

  if args.vs:
    _ab(args.app, args.vs, list(args.turns), args.cuj, args.real_tools, args.repeat)
  else:
    _report(drive(args.app, list(args.turns), args.cuj, args.real_tools))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
