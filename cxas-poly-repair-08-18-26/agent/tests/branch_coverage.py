#!/usr/bin/env python3
"""How much of the ladder's DECISION surface the journey walks actually discriminate.

The rung gate asks whether every rung fired somewhere. This asks a harder question: for
every leaf in every condition, did the suite ever produce a state where it is TRUE and a
state where it is FALSE? A leaf only ever seen one way is a branch no scenario decides --
the rung above it fires, the gate reports green, and the other side of that leaf has never
run.

Both defects the suite found in August were exactly that shape. `INQUIRY_NO_OUTAGE` gated
on `{"slot": "outage_status", "neq": "active"}`, which is TRUE on an unfilled slot; no
scenario had ever evaluated it against a filled one, so nothing noticed the agent
answering before it checked.

Denominator and numerator come from different places on purpose, because measuring either
alone flatters the result:

  * the DENOMINATOR is enumerated STATICALLY from the emitted config, so a leaf the engine
    never reaches at all is still counted against us. `any`/`all` short-circuit, and a
    purely runtime probe simply never sees those leaves -- it scored 128 of them here when
    the real surface is 150.
  * the NUMERATOR is what the engine ACTUALLY decided while the suite ran. Evaluating the
    static leaves against the pooled corpus of states instead scores 89%, but that is a
    weaker claim than it sounds: it says some scenario somewhere reached a state that
    would discriminate the leaf, not that any scenario ever made that decision. Only a
    real decision counts here.

One condition in this agent is authored as lambda source rather than as a dict. It
compiles to a callable with no inspectable leaves, so it is counted as OPAQUE and
reported, not silently skipped.
"""

from __future__ import annotations

import json

from flows.engine import loader

#: Coverage may not fall below this, and may not sit more than `_SLACK` above it without
#: the floor being raised -- a ratchet nobody tightens is a number that stops meaning
#: anything, which is the same reason the copy allowlist is asserted exactly.
#:
#: Set to what is MEASURED, not to a target. The first guess at this number was 77, made
#: before the instrument existed; the instrument said 59, and a batch of scenarios aimed
#: at the named gaps took it to 65. A floor above the real figure is a red build that
#: teaches people to raise floors, which is the opposite of a ratchet.
#:
#: Getting much past this means fighting SHORT-CIRCUIT evaluation rather than writing
#: useful scenarios: the leaves still undecided are ones whose enclosing `all` is settled
#: by an earlier leg in exactly the states that would decide them -- `gateway_status ==
#: error` is never reached because `HandleNoTelemetry` matches the same state first and
#: latches. Those are worth leaving red-flagged and unclaimed rather than contorting a
#: journey to touch them.
RATCHET = 68
_SLACK = 3


def _leaves(cond, out):
  """Every decidable leaf in a condition tree, through all / any / not."""
  if isinstance(cond, dict):
    for key in ("all", "any"):
      if key in cond:
        for sub in cond[key] or []:
          _leaves(sub, out)
        return
    if "not" in cond:
      _leaves(cond["not"], out)
      return
    if "slot" in cond:
      out.append(cond)


def surface(configs: dict) -> tuple[dict, int]:
  """`{flow: [leaf, ...]}` plus the count of conditions with no inspectable leaves."""
  found, opaque = {}, 0
  for flow, config in configs.items():
    leaves: list = []
    for kind in ("slots", "tasks"):
      for node in config.get(kind, []) or []:
        cond = node.get("condition")
        if cond is None:
          continue
        if not isinstance(cond, dict):
          opaque += 1          # lambda source: compiled to a callable, no leaves to read
          continue
        _leaves(cond, leaves)
    # De-duplicated: the same leaf authored on six rungs is ONE decision, and counting it
    # six times would let a single well-covered shared gate inflate the score.
    uniq = {json.dumps(x, sort_keys=True): x for x in leaves}
    found[flow] = list(uniq.values())
  return found, opaque


class Recorder:
  """Watches what the engine decides, for the duration of a suite run.

  Hooks `_eval_condition`, which both `_is_slot_active` and `_is_task_active` route
  through, so one hook sees every dict-form decision the engine makes. Restores the
  original on exit -- the engine module is process-global and a leaked hook would follow
  every later test in the same process.

  `framework_root` is not optional in practice. The loader caches a module per root, and
  the suite drives the APP's own copy under `built/tools`, not the packaged bundle --
  hook the wrong one and the recorder sees nothing at all while reporting a clean 0%.
  """

  def __init__(self, framework_root: str | None = None):
    self.decided: dict[str, set] = {}
    self._root = framework_root
    self._engine = None
    self._orig = None

  def __enter__(self):
    self._engine = loader.load_engine(framework_root=self._root)
    self._orig = self._engine._eval_condition

    def spy(spec, filled, *args, **kwargs):
      out = self._orig(spec, filled, *args, **kwargs)
      if isinstance(spec, dict) and "slot" in spec:
        self.decided.setdefault(json.dumps(spec, sort_keys=True), set()).add(bool(out))
      return out

    self._engine._eval_condition = spy
    return self

  def __exit__(self, *exc):
    self._engine._eval_condition = self._orig
    return False


def measure(configs: dict, decided: dict) -> dict:
  """Score the statically enumerated surface against what the engine actually decided."""
  leaves, opaque = surface(configs)
  both, one_way, never = 0, [], []
  for flow, flow_leaves in leaves.items():
    for leaf in flow_leaves:
      key = json.dumps(leaf, sort_keys=True)
      results = decided.get(key) or set()
      label = f"{flow}:{key}"
      if len(results) == 2:
        both += 1
      elif results:
        one_way.append(f"{label}  [only ever {list(results)[0]}]")
      else:
        never.append(label)
  total = sum(len(v) for v in leaves.values())
  return {"total": total, "both": both, "one_way": sorted(one_way),
          "never": sorted(never), "opaque": opaque,
          "pct": round(both / total * 100) if total else 100}


def gate(configs: dict, decided: dict, verbose: bool = False) -> list[str]:
  """Ratchet. Returns failures; empty means the floor still holds."""
  m = measure(configs, decided)
  failures = []
  if m["pct"] < RATCHET:
    failures.append(
        f"branch coverage {m['pct']}% is below the {RATCHET}% floor "
        f"({m['both']}/{m['total']} leaves decided both ways). A rung can fire with half "
        f"its condition never exercised, which is how both August defects shipped.")
  if m["pct"] >= RATCHET + _SLACK:
    failures.append(
        f"branch coverage is {m['pct']}%, {m['pct'] - RATCHET} above the {RATCHET}% "
        f"floor -- raise RATCHET in tests/branch_coverage.py to {m['pct']}.")
  if verbose:
    for label in m["one_way"]:
      print(f"       decided one way only : {label}")
    for label in m["never"]:
      print(f"       never decided        : {label}")
  return failures


def summary(configs: dict, decided: dict) -> str:
  m = measure(configs, decided)
  tail = f", {m['opaque']} opaque condition(s)" if m["opaque"] else ""
  return (f"branch coverage: {m['both']}/{m['total']} condition leaves decided both ways "
          f"({m['pct']}%, floor {RATCHET}%){tail}")
