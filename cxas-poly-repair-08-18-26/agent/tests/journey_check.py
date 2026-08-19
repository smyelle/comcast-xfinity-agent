#!/usr/bin/env python3
"""Offline proof that every journey this agent has still walks end to end.

Runs each scenario in `journey_scenarios.py` as a whole call -- greeting to terminal
outcome -- against the EMITTED config, with the real callbacks, the real engine and the
real verdict bodies, and no model or network anywhere. See `harness.py` for why that is
possible and what it does not cover.

Two coverage gates run over the same pass, so the suite grades its own completeness:
every rung must be reached by some scenario, and every approved line must be spoken by
one. Both fail on a SUPERSET as well as a subset -- an allowlist that quietly grows is how
a constant goes dead without anyone noticing.

    python tests/journey_check.py [--app-dir ./built] [--only A1,B5] [--verbose]

Build first: these read `built/`, and scoring a stale build is a mistake this repo has
made before.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import branch_coverage  # noqa: E402
import harness  # noqa: E402
import journey_scenarios as js  # noqa: E402


class Failure(Exception):
  pass


def _norm(text: str) -> str:
  """Compare copy the way a caller hears it, not the way it is typed.

  The emitter renders `{placeholder}` fields and joins parts with single spaces, so a
  constant's own newlines and doubled spaces are not what reaches the caller. Nothing
  else is normalised: case and punctuation are approved copy and must match.
  """
  return " ".join(text.split())


def _check(step: harness.Step, call: harness.Call, lines: list[str],
           rungs: list[str] | None) -> None:
  a = step.asserts
  spoken = [_norm(ln) for ln in lines]

  if "rungs" in a:
    if rungs is None:
      raise Failure("rungs= is only meaningful on a walk() step")
    if rungs != list(a["rungs"]):
      raise Failure(f"rungs fired {rungs} , wanted {list(a['rungs'])}")

  if a.get("silent"):
    if spoken:
      raise Failure(f"expected silence, heard {spoken}")

  if "text" in a:
    want = _norm(a["text"])
    if " ".join(spoken) != want:
      raise Failure(f"said {' '.join(spoken)!r}\n       wanted {want!r}")

  if "joined" in a:
    want = _norm(a["joined"])
    if " ".join(spoken) != want:
      raise Failure(f"halves rejoin to {' '.join(spoken)!r}\n       approved {want!r}")

  # `says` is a VERBATIM contiguous run, not a whole line: a rung's copy and the question
  # that follows it are frequently delivered as one utterance, and pinning the whole
  # utterance would make every scenario re-state its neighbours' copy. Use `text=` or
  # `joined=` where the whole of what the caller hears is the point.
  heard = " ".join(spoken)
  for line in a.get("says", ()):
    if _norm(line) not in heard:
      raise Failure(f"did not say {_norm(line)!r}\n       heard {heard!r}")

  for line in a.get("never", ()):
    if _norm(line) in heard:
      raise Failure(f"said {_norm(line)!r}, which it must not on this step")

  if "fc" in a:
    got = (call.result.get("function_call") or {}).get("name")
    if got != a["fc"]:
      raise Failure(f"function_call {got!r}, wanted {a['fc']!r}")

  if "next" in a and call.result.get("next_action") != a["next"]:
    raise Failure(f"next_action {call.result.get('next_action')!r}, "
                  f"wanted {a['next']!r}")

  if "status" in a and call.status != a["status"]:
    raise Failure(f"status {call.status!r}, wanted {a['status']!r}")

  if "escalated" in a:
    end = call.ended()
    if end is None:
      raise Failure(f"the call did not end; wanted escalated={a['escalated']}")
    if bool(end.get("escalated")) is not bool(a["escalated"]):
      raise Failure(f"ended escalated={end.get('escalated')}, wanted {a['escalated']}")

  for slot, want in (a.get("filled") or {}).items():
    got = call.filled.get(slot)
    if want is None:
      if got is not None:
        raise Failure(f"slot {slot} is {got!r}, wanted UNFILLED")
    elif str(got) != str(want):
      raise Failure(f"slot {slot} is {got!r}, wanted {want!r}")


def _expect_dispatched(call: harness.Call, task: str) -> None:
  """A scenario may only answer a task the engine actually asked for.

  Without this a mis-ordered scenario feeds a payload into the void: the slots fill,
  nothing fires, and the walk reports an empty cascade several steps later with no clue
  which step was wrong.
  """
  want = call.task_to_tool.get(task)
  got = call.pending_tool()
  if got != want:
    raise Failure(f"cannot answer {task}: the engine is asking for {got!r}, "
                  f"not {want!r}")


def _run_step(step: harness.Step, call: harness.Call) -> tuple[list[str], list | None]:
  """Perform one step. Returns `(lines heard, rungs fired or None)`."""
  p = step.payload
  if step.kind == "say":
    call.turn(p["text"])
  elif step.kind == "say_settling":
    call.turn_settling(p["text"], network_status=p["net"], gateway_status=p["gw"],
                       wifi_status=p["wifi"], technician_type=p["tech"],
                       activityType="TROUBLE_CALL", activityCode="", jobType="",
                       resolve_specialists_remote__job="JOB1")
  elif step.kind == "quiet":
    call.silence()
  elif step.kind == "delivered":
    call.delivered(p["tool"])
  elif step.kind == "fill":
    call.setter(p["tool"], **p["args"])
  elif step.kind == "task":
    _expect_dispatched(call, p["task"])
    call.task_returns(p["task"], success=p["success"], **p["outputs"])
    return call._lines(call.result), [p["task"]]
  elif step.kind == "legs":
    # Answer only the legs the engine actually dispatched, in whatever order it asks.
    # `leg_convoy` needs a MAC, so on a gateway-less account only one leg runs -- and
    # answering the other anyway does not just get ignored, it stops the parallel group
    # ever completing, so `SweepLegs_done` never fires and the whole sweep stalls
    # silently. That is the failure this loop exists to make impossible to write.
    payloads = {
        "leg_outage": dict(outage_detected=p["outage"] == "active",
                           outage_status=p["outage"], outage_message="OUTAGE_MSG",
                           customer_message="CUSTOMER_MSG"),
        "leg_convoy": dict(routing_action=p["action"], convoy_status="clear"),
    }
    answered = []
    for _ in range(len(payloads)):
      task = call.task_for_tool(call.pending_tool() or "")
      if task not in payloads:
        break
      call.task_returns(task, **payloads[task])
      answered.append(task)
    if not answered:
      raise Failure(f"no sweep leg was dispatched; engine wants "
                    f"{call.pending_tool()!r}")
    return call._lines(call.result), answered
  elif step.kind == "remote":
    call.remote_returns(**p)
  elif step.kind == "specialists":
    _expect_dispatched(call, "Specialists")
    call.task_returns("Specialists", resolve_specialists_remote__job="JOB1")
    call.remote_returns(resolve_specialists_remote__job="JOB1", network_status=p["net"],
                        gateway_status=p["gw"], wifi_status=p["wifi"],
                        technician_type=p["tech"], activityType="TROUBLE_CALL",
                        activityCode="", jobType="")
    return call._lines(call.result), ["Specialists"]
  elif step.kind == "walk":
    rungs, lines = call.drive()
    return lines, rungs
  else:  # pragma: no cover
    raise Failure(f"unknown step kind {step.kind!r}")
  return call._lines(call.result), None


def run_scenario(scenario: js.Scenario, configs: dict, app_dir: str
                 ) -> tuple[list[str], list[str], list[str]]:
  """Walk one call. Returns `(rungs fired, lines heard, failures)`."""
  call = harness.new_call(configs[scenario.flow], app_dir=app_dir, flow=scenario.flow,
                          account=scenario.account)
  fired: list[str] = []
  failures: list[str] = []
  for i, step in enumerate(scenario.steps):
    try:
      lines, rungs = _run_step(step, call)
      if rungs:
        fired.extend(rungs)
      _check(step, call, lines, rungs)
    except Failure as exc:
      failures.append(f"step {i} {step!r}: {exc}")
      break
    except Exception as exc:  # noqa: BLE001 - a raising engine is a scenario failure
      failures.append(f"step {i} {step!r}: {type(exc).__name__}: {exc}")
      break
  return fired, call.transcript, failures


# --- Gate A: every ending is reached -----------------------------------------


def gate_terminals(configs: dict, reached: set[str]) -> list[str]:
  """Every rung in every flow must be walked by some scenario.

  The point of this gate is the rung nobody thought to cover: a new one lands, no
  scenario reaches it, and the suite still reports all green. Reachability is asserted
  in both directions, so a scenario naming a rung that no longer exists fails too.
  """
  declared = set()
  for flow, config in configs.items():
    prefix = "" if flow == "repair" else f"{flow}:"
    declared |= {prefix + t["name"] for t in config.get("tasks", [])}
  failures = []
  for missing in sorted(declared - reached - js.INERT_RUNGS):
    failures.append(f"rung never reached by any scenario: {missing}")
  for gone in sorted(js.INERT_RUNGS - declared):
    failures.append(f"inert list names a rung that no longer exists: {gone}")
  for extra in sorted(reached & js.INERT_RUNGS):
    failures.append(f"rung is on the inert list but a scenario reached it: {extra}")
  return failures


# --- Gate B: every approved line is spoken -----------------------------------


def _constants() -> dict[str, str]:
  """Every approved customer-facing line, by name.

  Read out of `vars(scripts)` rather than by parsing the file, because the journey split
  left most of these as re-exported bindings with no literal in `scripts.py` at all. The
  facade is eager precisely so this works (`scripts.py:798-802`).
  """
  import clarify
  import scripts
  from journeys import device_help

  out = {}
  for name, value in vars(scripts).items():
    if name.startswith(("SAY_", "ASK_", "FILLER_")) and isinstance(value, (str, list)):
      out[name] = value
  for name in ("ASK_CLARIFY", "ASK_CLARIFY_AGAIN", "ASK_CLARIFY_DEVICE",
               "ASK_CLARIFY_DEVICE_AGAIN", "SAY_CLARIFY_STILL_HERE", "SAY_ONLY_APP",
               "SAY_EVERYTHING_DOWN", "SAY_UNSURE"):
    if hasattr(clarify, name):
      out[f"clarify.{name}"] = getattr(clarify, name)
  out["device_help._NO_STEPS_SAY"] = device_help._NO_STEPS_SAY
  return out


def _appears(value, corpus: str) -> bool:
  """Is this constant's copy in the transcript?

  A list constant (a filler pool, a reassurance ladder) counts if ANY member was heard --
  the engine picks one per turn by design. A constant carrying a `{placeholder}` is
  matched on its longest literal run, since the rendered value is scenario data rather
  than approved copy.
  """
  if isinstance(value, list):
    return any(_appears(v, corpus) for v in value)
  text = _norm(value)
  if "{" in text:
    runs = []
    for chunk in text.replace("}", "{").split("{"):
      runs.append(chunk)
    text = max(runs, key=len).strip()
    if len(text) < 20:
      return True  # nothing literal enough left to assert on
  return text in corpus


def gate_dead_copy() -> list[str]:
  """Copy declared dead must still be referenced by nothing but its own definition.

  Checked against the SOURCE, not the transcript, because the transcript cannot tell
  deadness from a substring: `SAY_OFFER_WHILE_CHECKING` is contained in
  `SAY_SCOPE_NOTED`, which a scenario does speak. Grepping the agent is the only way to
  make the claim the list is actually making -- and it fails in both directions, so a
  constant that comes back into use loses its entry.
  """
  root = os.path.dirname(HERE)
  sources = []
  for base, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs
               if d not in {"tests", "__pycache__", "substrate", "specialist_proxy"}
               and not d.startswith("built")]
    sources += [os.path.join(base, f) for f in files if f.endswith(".py")]
  failures = []
  for name, why in js.DEAD_COPY.items():
    uses = 0
    for path in sources:
      with open(path) as fh:
        for line in fh:
          stripped = line.strip()
          if name in stripped and not stripped.startswith("#"):
            # The definition itself, and the `__all__` entry that re-exports it, are not
            # uses -- everything else is.
            uses += 0 if (stripped.startswith(f"{name} =")
                          or stripped.strip(",'\" ") == name) else 1
    if uses:
      failures.append(f"{name} is on the dead list but {uses} place(s) use it -- "
                      f"remove the entry (was: {why})")
  return failures


def gate_copy(transcripts: list[str]) -> list[str]:
  """Every approved line must have been spoken, and the allowlist must be EXACT."""
  corpus = _norm(" ".join(transcripts))
  constants = {n: v for n, v in _constants().items() if n not in js.DEAD_COPY}
  unheard = {n for n, v in constants.items() if not _appears(v, corpus)}
  failures = []
  for name in sorted(unheard - set(js.UNREACHABLE_COPY)):
    failures.append(f"approved line never spoken by any scenario: {name}")
  for name in sorted(set(js.UNREACHABLE_COPY) - unheard):
    failures.append(f"allowlisted as unreachable, but a scenario spoke it: {name}")
  for name in sorted(set(js.UNREACHABLE_COPY) - set(constants)):
    failures.append(f"allowlist names a constant that no longer exists: {name}")
  return failures


def run(app_dir: str, only: set[str] | None, verbose: bool) -> int:
  configs = {flow: harness.load_config(app_dir, flow)
             for flow in ("repair", "reboot", "human")}
  scenarios = [s for s in js.SCENARIOS if not only or s.sid in only]
  print("config: " + ", ".join(
      f"{f} {len(c['slots'])} slots / {len(c['tasks'])} tasks"
      for f, c in configs.items()) + f"\n{len(scenarios)} scenarios\n")

  reached: set[str] = set()
  transcripts: list[str] = []
  failed = 0
  # Watches what the engine DECIDES for the whole run, so the branch gate below scores
  # real decisions rather than states that merely could have discriminated a leaf.
  recorder = branch_coverage.Recorder(harness.framework_root(app_dir))
  recorder.__enter__()
  for scenario in scenarios:
    fired, transcript, failures = run_scenario(scenario, configs, app_dir)
    prefix = "" if scenario.flow == "repair" else f"{scenario.flow}:"
    reached |= {prefix + r for r in fired}
    reached |= set(scenario.endings)
    transcripts.extend(transcript)
    status = "ok  " if not failures else "FAIL"
    failed += bool(failures)
    print(f"{status} {scenario.sid:5} {scenario.title:52} "
          f"{len(scenario.steps)} steps")
    for failure in failures:
      print(f"       {failure}")
    if verbose:
      for line in transcript:
        print(f"       | {line}")

  recorder.__exit__(None, None, None)

  print()
  gates = 0
  if only:
    print("coverage gates skipped (--only runs a subset)")
  else:
    for failure in (gate_terminals(configs, reached) + gate_copy(transcripts)
                    + gate_dead_copy()
                    + branch_coverage.gate(configs, recorder.decided, verbose)):
      print(f"FAIL coverage: {failure}")
      gates += 1
    print(branch_coverage.summary(configs, recorder.decided))
    if not gates:
      print(f"coverage: every rung reached, every approved line spoken, "
            f"{len(js.DEAD_COPY)} dead line(s) still dead")

  print(f"\n{len(scenarios) - failed}/{len(scenarios)} journeys walked"
        + (f", {gates} coverage failure(s)" if gates else ""))
  return 1 if (failed or gates) else 0


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("--app-dir", default="built")
  parser.add_argument("--only", default="", help="comma-separated scenario ids")
  parser.add_argument("--verbose", action="store_true", help="print each transcript")
  args = parser.parse_args()
  raise SystemExit(run(args.app_dir, set(filter(None, args.only.split(","))),
                       args.verbose))
