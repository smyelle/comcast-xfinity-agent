"""Progressive fan-out lowering: the codegen behind a narrate-as-they-land group.

`flows.parallel(...)` already dispatches its legs in one action, so three four-second
lookups cost the caller four seconds rather than twelve. What it cannot do synchronously
is REPORT: the runtime hands back the whole batch after the slowest leg, so three checks
of 8s/18s/30s buy half a minute of silence and then a wall of results. A human agent says
"the line test is back, that's your fault right there" while still waiting on the other
two.

This module is the compilation half of closing that gap. **The authoring surface does not
change at all** — no new kwargs, no new task keys. Each leg already carries its own
`then_say`; only what the group lowers to is different:

  * every leg is re-emitted as an ASYNCHRONOUS tool wrapping the author's own body and
    publishing its result to the leg's OWN state key, `<group>_<leg>`. Separate keys
    because concurrent writes to one shared structure lose N-1 of them outright, values
    included (ces-probes 37 and 38); separate keys have no conflict to lose.
  * a `<group>_peek` reports which of those keys are present. It has to be its own tool:
    a running tool body's view of state is frozen at the moment it started (61), so a
    watcher re-reading its own state could spin for a minute and see nothing. Each FRESH
    invocation gets a fresh snapshot (71).
  * a `<group>_watch` polls peek THROUGH THE INJECTED `tools` GLOBAL until a leg outside
    `seen` lands. That routing is load-bearing, not stylistic: sub-calls made that way
    never enter the transcript and cost no reasoning pass (70), and there are exactly ten
    passes per input with nothing that resets them (72, 73). A watcher polling sixteen
    times as ordinary tool calls would spend the whole budget before speaking a word.

The engine half lives in `slot_filling_engine` (`_progressive_groups` and friends) and
`before_model`. `progressive_groups` below and the engine's `_progressive_groups` MUST
agree — a group the engine watches but this module did not lower has no watcher to
dispatch, and a leg name resolving to no registered tool is SILENT AND FATAL: it survives
neither a daemon thread nor `join(timeout=10)`, nothing surfaces anywhere, and the turn
simply dies (69). `validate_dag_config._check_parallel_groups` carries the rule that
stops that reaching a deployment.
"""

from __future__ import annotations

from typing import Any, Optional

# The watch window, in seconds. The deadline is per CALL, not per turn: a single call is
# safe to ~29s and fails somewhere at or below 60s, while cumulative time in one turn is
# fine to at least 82s (ces-probes 80 and 82). So the watcher returns before the per-call
# deadline and is re-dispatched on the next pass; a gap between landings longer than one
# window costs one of the ten passes rather than breaking the group. Twenty is
# conservatively inside the evidence.
WATCH_WINDOW_SECONDS = 20

# Seconds between polls inside a window. Free — the sub-call costs no reasoning pass.
POLL_INTERVAL_SECONDS = 1.0

# peek reports its landed set pipe-DELIMITED ("|a|b|") and watch matches "|<leg>|". The
# watcher only sees peek's return rendered as text, so a bare substring test would let a
# leg named `test` match a sibling named `line_test`.
_MARK = "|"

# Stamped into every generated body. The scaffold reads it to give these tools an
# honest tool-json description instead of the setter default ("Record the value for
# <name>"), and it keys on the MARKER rather than the name because an author's own
# `check_watch` must not be described as an engine-owned watcher.
MARKER = "# flows:progressive-fanout"


def leg_tool_name(group: str, leg: str) -> str:
  """The generated ASYNCHRONOUS tool a lowered leg fires."""
  return f"{group}_{leg}_leg"


def state_key(group: str, leg: str) -> str:
  """The state key a lowered leg publishes its result to."""
  return f"{group}_{leg}"


def peek_tool_name(group: str) -> str:
  return f"{group}_peek"


def watch_tool_name(group: str) -> str:
  return f"{group}_watch"


def progressive_groups(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
  """`{group: [legs]}` for every fan-out group eligible for progressive lowering.

  A group qualifies when it has two or more legs and every leg fires a tool. Both are
  shapes `parallel()` and the validator already refuse, so in practice EVERY well-formed
  group is lowered — which is deliberate. An eligibility rule that quietly held some
  groups back would be the same class of defect as the ghost leg name: a program that
  looks right, builds clean, and behaves like the shape it was written to replace.

  In particular `awaits` does NOT disqualify a leg. `parallel(deadline=…)`,
  `waiting_say=` and `on_timeout=` all merge into `awaits` on the asynchronous legs, so
  excluding it would mean the most natural way to write a slow group — the one with a
  deadline and a holding line — was the one way to opt out of narrating it. An `awaits`
  leg lowers identically (the wrapper inlines the body; `asynchronous` is a property of
  the tool RESOURCE, not of the code), `awaits.say` is spoken on the first watch
  dispatch, and `max_turns`/`on_timeout` stay live as the cross-TURN backstop for a
  group that outlives the held floor.

  `parallel(progressive=False)` opts a group out entirely: its legs carry
  `parallel_batch` and keep the #541 batch shape -- synchronous, dispatched together,
  collected on the same pass, under their own tool names. That costs one reasoning pass
  instead of one per watch window, which is what makes a group affordable on a DAG near
  the ten-pass ceiling, and it keeps name-keyed config attached. The trade is real: a
  synchronous fan-out has one observation point, after the slowest leg (ces-probes 40).

  MIRRORS `slot_filling_engine._progressive_groups`. Change one, change both.
  """
  groups: dict[str, list[dict[str, Any]]] = {}
  for task in config.get("tasks") or []:
    group = task.get("parallel")
    if group:
      groups.setdefault(group, []).append(task)
  return {
      group: legs
      for group, legs in groups.items()
      if len(legs) >= 2 and all(leg.get("tool") for leg in legs)
      and not any(leg.get("parallel_batch") for leg in legs)
  }


def unlowered_groups(
    config: dict[str, Any], bodies: dict[str, str],
) -> dict[str, str]:
  """`{group: reason}` for every fan-out group this build will NOT narrate per leg.

  The point is that there is no silent path out of the feature. A group that keeps the
  old batch shape says so, by name, with the reason — the caller-visible difference
  (half a minute of silence, then everything at once) is invisible in the source and
  invisible offline, so nothing else would ever surface it.
  """
  eligible = progressive_groups(config)
  out: dict[str, str] = {}
  groups: dict[str, list[dict[str, Any]]] = {}
  for task in config.get("tasks") or []:
    group = task.get("parallel")
    if group:
      groups.setdefault(group, []).append(task)
  for group, legs in groups.items():
    if group not in eligible:
      bad = [leg.get("name", "<unnamed>") for leg in legs if not leg.get("tool")]
      out[group] = (
          f"leg(s) {sorted(bad)} call no tool"
          if bad else f"it has {len(legs)} leg(s), and a group needs two")
      continue
    missing = sorted({leg["tool"] for leg in legs if leg["tool"] not in bodies})
    if missing:
      out[group] = (
          f"tool(s) {missing} have no body to wrap — the leg wrapper inlines the"
          " tool's own source, so there is nothing to publish from")
  return out


def leg_tool_names(all_map: dict[str, dict[str, Any]]) -> set[str]:
  """Every generated leg tool across a config map — the set emitted ASYNCHRONOUS.

  Derived from the group shape rather than recorded on the tasks, so no new task key is
  introduced and validation and emission cannot disagree about which tools these are.
  """
  return {
      leg_tool_name(group, leg["name"])
      for cfg in all_map.values()
      for group, legs in progressive_groups(cfg).items()
      for leg in legs
  }


def leg_tool_sources(all_map: dict[str, dict[str, Any]]) -> dict[str, str]:
  """{generated leg wrapper: the author's tool it wraps}, across a config map.

  The wrapper is the resource CES dispatches, so anything the author declared ON the
  tool — a `timeout`, today — has to be carried across to it. Keyed the same way as
  `leg_tool_names`, from the group shape rather than a recorded task key.

  Args:
    all_map: Every config in the app, keyed by config id.

  Returns:
    Wrapper tool name mapped to the tool name the leg fires.
  """
  return {
      leg_tool_name(group, leg["name"]): leg["tool"]
      for cfg in all_map.values()
      for group, legs in progressive_groups(cfg).items()
      for leg in legs
      if leg.get("tool")
  }


def synthetic_tool_names(all_map: dict[str, dict[str, Any]]) -> set[str]:
  """Every generated peek/watch tool across a config map.

  Named by no task and no slot, so nothing scoping an agent's tools from its config can
  find them — they have to be added explicitly or the watcher the engine dispatches is
  not on the agent at all.
  """
  names: set[str] = set()
  for cfg in all_map.values():
    for group in progressive_groups(cfg):
      names.add(peek_tool_name(group))
      names.add(watch_tool_name(group))
  return names


def _leg_params(task: dict[str, Any]) -> list[str]:
  """The wrapper's parameter names — the CALLEE names the engine dispatches with.

  Mirrors the engine's `_task_input_args`: a dict `{my_slot: callee_param}` contributes
  its values, a bare list contributes its entries. Named parameters, never `**kwargs`:
  CES derives a tool's schema from the signature and silently DROPS a tool that takes
  only `**kwargs`, so a kwargs wrapper would be a leg that resolves to nothing — the
  silent-and-fatal case.
  """
  inputs = task.get("inputs") or []
  if isinstance(inputs, dict):
    return list(dict.fromkeys(inputs.values()))
  return list(dict.fromkeys(inputs))


def render_leg_tool(group: str, task: dict[str, Any], body: str) -> str:
  """The `python_code.py` for one lowered leg: the author's tool, plus publication.

  The author's rendered body is inlined VERBATIM rather than called across modules — a
  CES tool runs in an isolated sandbox and cannot import another one, and calling it
  through the `tools` dispatcher instead would answer `pending` for anything already
  asynchronous. Inlining keeps the shape identical to the one measured end to end: an
  asynchronous body that does the work and writes to state (ces-probes 68, 75).
  """
  tool = task["tool"]
  name = leg_tool_name(group, task["name"])
  key = state_key(group, task["name"])
  params = _leg_params(task)
  sig = ", ".join(f'{p}: str = ""' for p in params)
  arg_lines = "".join(f"    {p}: input {p}.\n" for p in params) or "    (none)\n"
  args_literal = (
      "{" + ", ".join(f'"{p}": {p}' for p in params) + "}" if params else "{}")
  return (
      f'"""Leg `{task["name"]}` of the `{group}` fan-out — runs `{tool}`, publishes'
      " the result.\n\n"
      "Emitted ASYNCHRONOUS, so CES answers the dispatch immediately and runs this body\n"
      "in the background while its siblings run too. The result is published to this\n"
      f"leg's OWN state key (`{key}`) rather than a shared structure, because concurrent\n"
      "writes to one structure lose all but the last (ces-probes 37/38). The framework\n"
      "reads it from there the moment the group's watcher wakes the next pass, which is\n"
      "what lets the finding be spoken while the other legs are still running.\n"
      '"""\n'
      f"{MARKER} leg\n\n"
      f"{body.rstrip()}\n\n\n"
      f"def {name}({sig}) -> dict:\n"
      f'  """Run the {task["name"]} check and publish its result for the fan-out.\n\n'
      "  Args:\n"
      f"{arg_lines}"
      "  Returns:\n"
      "    The wrapped tool's result dict.\n"
      '  """\n'
      "  import inspect as _inspect\n"
      "  import json as _json\n"
      f"  _args = {args_literal}\n"
      "  try:\n"
      f"    _sig = _inspect.signature({tool})\n"
      "    _only = list(_sig.parameters.values())\n"
      "    if len(_only) == 1 and hasattr(\n"
      '        getattr(_only[0], "annotation", None), "model_validate"):\n'
      "      # A pydantic-input tool takes ONE model, not the flat arguments CES sends.\n"
      f"      _out = {tool}(_only[0].annotation.model_validate(_args))\n"
      "    else:\n"
      f"      _out = {tool}(\n"
      "          **{k: v for k, v in _args.items() if k in _sig.parameters})\n"
      "  except Exception as _exc:\n"
      "    # A leg that throws must still PUBLISH: `<group>_done` means every leg\n"
      "    # reported, not that every leg succeeded, so swallowing the failure here\n"
      "    # would hang the group on one flaky backend — the opposite of why the legs\n"
      "    # were grouped. The framework routes it through the leg's on_failure ladder.\n"
      '    _out = {"success": False,\n'
      '            "error": "%s: %s" % (type(_exc).__name__, _exc)}\n'
      '  if hasattr(_out, "model_dump"):\n'
      "    _out = _out.model_dump()\n"
      "  if not isinstance(_out, dict):\n"
      '    _out = {"result": _out}\n'
      "  try:\n"
      f'    context.state["{key}"] = _json.dumps(_out, default=str)  # noqa: F821\n'
      "  except Exception:\n"
      "    # Publication is the only delivery inside the turn, but the completion\n"
      "    # envelope still arrives later — degrade to that rather than take the leg\n"
      "    # down with an exception nothing would surface.\n"
      "    pass\n"
      "  return _out\n"
  )


def render_peek_tool(group: str, legs: list[str]) -> str:
  """The `python_code.py` for `<group>_peek` — one FRESH snapshot per invocation.

  Its own tool rather than a loop inside the watcher because a running body's view of
  state is frozen at the moment it started (ces-probes 61): seeing a change requires
  dispatching a new invocation (71). That asymmetry is the load-bearing discovery of the
  whole design.
  """
  name = peek_tool_name(group)
  pairs = ", ".join(f'("{leg}", "{state_key(group, leg)}")' for leg in legs)
  return (
      f'"""Report which legs of the `{group}` fan-out have published a result."""\n'
      f"{MARKER} peek\n\n\n"
      f"def {name}(query: str = \"\") -> dict:\n"
      f'  """Read every leg\'s state key and report which are present.\n\n'
      "  Args:\n"
      "    query: Caller-supplied tag, unused except to vary the call.\n\n"
      "  Returns:\n"
      "    Dict naming the legs that have landed, pipe-delimited.\n"
      '  """\n'
      f"  _KEYS = ({pairs},)\n"
      "  landed = []\n"
      "  for _leg, _key in _KEYS:\n"
      "    try:\n"
      "      if context.state.get(_key):  # noqa: F821\n"
      "        landed.append(_leg)\n"
      "    except Exception:\n"
      "      pass\n"
      '  # Pipe-delimited so the watcher, which only sees this rendered as text, cannot\n'
      '  # match a leg named `test` against a sibling named `line_test`.\n'
      f'  marks = "{_MARK}" + "{_MARK}".join(landed) + "{_MARK}" if landed else ""\n'
      '  return {"success": True, "landed": marks, "n": len(landed)}\n'
  )


def render_watch_tool(group: str, legs: list[str]) -> str:
  """The `python_code.py` for `<group>_watch` — block until something NEW lands.

  Polls `peek` through the injected `tools` global. Sub-calls made that way never enter
  the transcript and cost no reasoning pass (ces-probes 70), so one watcher can poll for
  its whole window inside a single pass (74) — which is what leaves the ten passes per
  turn for NARRATION POINTS rather than for looking.

  Bounded well under the per-call deadline and re-dispatched by the engine, because the
  deadline is per call and not per turn (80, 82).
  """
  name = watch_tool_name(group)
  peek = peek_tool_name(group)
  legs_literal = "(" + ", ".join(f'"{leg}"' for leg in legs) + ",)"
  return (
      f'"""Wait, off the reasoning-pass budget, until a leg of `{group}` that has NOT\n'
      "been narrated lands.\n"
      '"""\n'
      f"{MARKER} watch\n\n\n"
      f"def {name}(seen: str = \"\") -> dict:\n"
      f'  """Poll until a leg outside `seen` lands, then return immediately.\n\n'
      "  Args:\n"
      "    seen: Comma-separated leg names already narrated.\n\n"
      "  Returns:\n"
      "    Dict naming the newly landed legs and whether the group is complete.\n"
      '  """\n'
      "  import time\n"
      f"  _LEGS = {legs_literal}\n"
      '  already = {s for s in (seen or "").split(",") if s}\n'
      "  t0 = time.time()\n"
      f"  while time.time() - t0 < {WATCH_WINDOW_SECONDS}:\n"
      "    try:\n"
      "      body = str(getattr(\n"
      f'          getattr(globals()["tools"], "{peek}")({{"query": "p"}}),\n'
      '          "text", "") or "")\n'
      "    except Exception as exc:\n"
      "      # The engine re-dispatches on the next pass; a broken poll must not hang\n"
      "      # the turn until the per-call deadline kills it outright.\n"
      '      return {"success": False, "error": type(exc).__name__,\n'
      '              "fresh": "", "all": "0"}\n'
      f'    landed = [n for n in _LEGS if ("{_MARK}%s{_MARK}" % n) in body]\n'
      "    fresh = [n for n in landed if n not in already]\n"
      "    if fresh:\n"
      '      return {"success": True, "fresh": ",".join(fresh),\n'
      '              "all": "1" if len(landed) == len(_LEGS) else "0",\n'
      '              "at": "%.0f" % (time.time() - t0)}\n'
      f"    time.sleep({POLL_INTERVAL_SECONDS})\n"
      '  return {"success": True, "fresh": "", "all": "0", "at": "timeout"}\n'
  )


def lower(
    all_map: dict[str, dict[str, Any]],
    bodies: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
  """Rewrite every eligible group onto the progressive path.

  Returns `(all_map, generated_bodies)`. Each lowered leg's `tool` is repointed at its
  generated wrapper; nothing else in the config moves, so `then_say`, `outputs`,
  `on_failure` and the group's all-done announce all keep working unchanged.

  A group whose legs have no body available is left alone rather than half-lowered: the
  wrapper inlines the author's source, so without it there is nothing to wrap, and a leg
  pointing at a tool that does not exist is the one failure mode with no symptom at all.

  Configs are rebuilt rather than mutated — the task dicts can be the author's own `Flow`
  objects, and a build must not leave the program it compiled rewritten behind it.
  """
  generated: dict[str, str] = {}
  out_map: dict[str, dict[str, Any]] = {}
  for cid, cfg in all_map.items():
    groups = progressive_groups(cfg)
    lowered: dict[str, str] = {}   # task name -> generated tool
    for group, legs in sorted(groups.items()):
      if any(leg["tool"] not in bodies for leg in legs):
        continue
      for leg in legs:
        generated[leg_tool_name(group, leg["name"])] = render_leg_tool(
            group, leg, bodies[leg["tool"]])
        lowered[leg["name"]] = leg_tool_name(group, leg["name"])
      names = [leg["name"] for leg in legs]
      generated[peek_tool_name(group)] = render_peek_tool(group, names)
      generated[watch_tool_name(group)] = render_watch_tool(group, names)
    if not lowered:
      out_map[cid] = cfg
      continue
    out_map[cid] = dict(
        cfg,
        tasks=[dict(t, tool=lowered[t["name"]]) if t.get("name") in lowered else t
               for t in (cfg.get("tasks") or [])],
    )
  return out_map, generated


def apply(
    all_map: dict[str, dict[str, Any]],
    bodies: dict[str, str],
    available: Optional[list[str]] = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
  """`lower`, folded back into the `(all_map, bodies, available)` an assembly carries.

  A no-op returning the same objects when the app declares no eligible group, which is
  what keeps every existing app's emitted tree byte-for-byte what it was.
  """
  out_map, generated = lower(all_map, bodies)
  if not generated:
    return all_map, bodies, list(available or [])
  return (
      out_map,
      {**bodies, **generated},
      sorted(set(available or []) | set(generated)),
  )
