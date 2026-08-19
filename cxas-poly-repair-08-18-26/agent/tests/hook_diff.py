"""Diff what `hooks.py` DOES, between two versions of it.

Nothing else in this repo tests `hooks.py`. `ladder_check.py` hand-seeds the state "as the
before_agent hook would leave it" and never executes it, so its 88 scenarios are blind to
this file by construction — and can be made green by editing the seeder. The
byte-identical build oracle cannot help either: a hook edit changes the emitted bytes by
definition.

So: run BOTH versions of the callbacks over the same corpus of inputs and diff what they
write. For a deletion that is supposed to change nothing, the answer must be *exactly*
nothing.

    python tests/hook_diff.py                  # hook-level: what the callbacks write
    python tests/hook_diff.py --e2e            # end-to-end: hooks THEN the engine
    python tests/hook_diff.py --base <ref>     # ...vs another ref
    python tests/hook_diff.py --show           # print every write, not just diffs

Two modes, because the right question changes as work moves out of the hook. While a step
only deletes, hook-level identity is the proof. Once a step MOVES a write into the
declarative layer -- `publish=`, `event_slot(default=)` -- the hook legitimately stops
writing something, so hook-level will differ by design and the only meaningful question is
whether the engine still ends the turn in the same place. That is `--e2e`.

The corpus is built to reach every guard and branch in the file rather than to be
realistic: the interesting inputs are the ones that take a different early return.
"""

import argparse
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SDK = os.path.dirname(_HERE)
_REPO = os.path.dirname(_SDK)


# --------------------------------------------------------------------------- #
# Fakes for the CES ambient objects. Deliberately minimal: the hook only ever
# touches `.state`, `.events` and `llm_request.contents`.
# --------------------------------------------------------------------------- #

class _Part:
  def __init__(self, text):
    self.text = text


class _Content:
  def __init__(self, role, text):
    self.role = role
    self.parts = [_Part(text)]


class _Event:
  """An ADK event as `stated_problem()` reads it: an author and callable parts()."""

  def __init__(self, author, text):
    self.author = author
    self._parts = [_Part(text)]

  def parts(self):
    return self._parts


class _Ctx:
  def __init__(self, state, events=(), variables=None):
    self.state = state
    self.events = list(events)
    self.variables = dict(variables or {})


class _Req:
  def __init__(self, contents=()):
    self.contents = list(contents)


# --------------------------------------------------------------------------- #
# The corpus: one entry per branch the callbacks can take.
# --------------------------------------------------------------------------- #

def _sm(**filled):
  return {"filled": dict(filled), "pending": {}, "status": "in_progress",
          "task_results": {}}


CASES = [
    # --- before_agent -----------------------------------------------------
    ("cold open, silent", "before_agent",
     {"sm": _sm()}, [], None),
    ("cold open, caller speaks", "before_agent",
     {"sm": _sm()}, [_Event("user", "my internet is down")], None),
    ("account in state, first substantive turn", "before_agent",
     {"sm": _sm(), "accountNumber": "8069100230359946"},
     [_Event("user", "my internet is down")], None),
    ("account under the legacy key", "before_agent",
     {"sm": _sm(), "account_id": "8069100230359946"},
     [_Event("user", "my internet is down")], None),
    ("bare greeting only (opener suppression)", "before_agent",
     {"sm": _sm(), "accountNumber": "806910023035"}, [_Event("user", "hello")], None),
    ("already swept", "before_agent",
     {"sm": _sm(diagnostics_complete="true"), "accountNumber": "806910023035",
      "diagnostics_triggered": "true", "caller_heard": "true"},
     [_Event("user", "what now")], None),
    ("already swept, reboot offered", "before_agent",
     {"sm": _sm(diagnostics_complete="true"), "accountNumber": "806910023035",
      "diagnostics_triggered": "true", "caller_heard": "true",
      "reboot_offered": "true"}, [_Event("user", "yes please")], None),
    ("wifi offered -> answer allowed", "before_agent",
     {"sm": _sm(diagnostics_complete="true"), "accountNumber": "806910023035",
      "diagnostics_triggered": "true", "caller_heard": "true",
      "wifi_offered": "true"}, [_Event("user", "ok")], None),
    ("early scope answered, promotion path", "before_agent",
     {"sm": _sm(diagnostics_complete="true", wifi_scope_early="ONE_DEVICE"),
      "accountNumber": "806910023035", "diagnostics_triggered": "true",
      "caller_heard": "true", "wifi_scope_early": "ONE_DEVICE",
      "wifi_scope_asked": "true"}, [_Event("user", "just one device")], None),
    ("three tips given -> cap", "before_agent",
     {"sm": _sm(diagnostics_complete="true"), "accountNumber": "806910023035",
      "diagnostics_triggered": "true", "caller_heard": "true",
      "wifi_tip_rejoin": "true", "wifi_tip_closer": "true",
      "wifi_tip_toggle": "true"}, [_Event("user", "still not working")], None),
    ("per-turn mutexes set", "before_agent",
     {"sm": _sm(diagnostics_complete="true", cost_answered="true",
                cost_question="VISIT", already_tried="true", already_tried_ack="true"),
      "accountNumber": "806910023035", "diagnostics_triggered": "true",
      "caller_heard": "true", "cost_answered": "true", "cost_question": "VISIT",
      "already_tried": "true", "already_tried_ack": "true"},
     [_Event("user", "will this cost me")], None),
    ("inquiry answered -> full check allowed", "before_agent",
     {"sm": _sm(), "accountNumber": "806910023035", "caller_heard": "true",
      "inquiry_answered": "true"}, [_Event("user", "yes please")], None),
    ("inquiry closed", "before_agent",
     {"sm": _sm(), "accountNumber": "806910023035", "caller_heard": "true",
      "inquiry_closed": "true"}, [_Event("user", "no thanks")], None),
    ("fee answered once carries", "before_agent",
     {"sm": _sm(), "accountNumber": "806910023035", "caller_heard": "true",
      "fee_answered_once": "true"}, [_Event("user", "and the cost")], None),
    ("async sweep armed", "before_agent",
     {"sm": _sm(), "accountNumber": "806910023035", "caller_heard": "true",
      "async_sweep_armed": "1"}, [_Event("user", "my internet is down")], None),
    ("technician fee already present", "before_agent",
     {"sm": _sm(technician_fee="$0"), "accountNumber": "806910023035",
      "caller_heard": "true"}, [_Event("user", "my internet is down")], None),
    ("dispatch values already in state", "before_agent",
     {"sm": _sm(), "accountNumber": "806910023035", "caller_heard": "true",
      "activityType": "SWAP", "activityCode": "X1", "jobType": "FIELD"},
     [_Event("user", "my internet is down")], None),

    # --- before_model -----------------------------------------------------
    ("model turn, caller spoke", "before_model",
     {"sm": _sm(), "accountNumber": "806910023035"}, None,
     [_Content("user", "my internet is down")]),
    ("model turn, silence tick", "before_model",
     {"sm": _sm(), "accountNumber": "806910023035"}, None,
     [_Content("user", "")]),
    ("model turn, tip latch release", "before_model",
     {"sm": _sm(wifi_tip_given="true"), "accountNumber": "806910023035",
      "wifi_tip_given": "true", "diagnostics_triggered": "true"}, None,
     [_Content("user", "that did not help")]),
    ("model turn, scope correction", "before_model",
     {"sm": _sm(wifi_scope="ALL_DEVICES"), "accountNumber": "806910023035",
      "diagnostics_triggered": "true"}, None,
     [_Content("user", "actually just my laptop")]),
    ("model turn, ambiguous scope (must NOT correct)", "before_model",
     {"sm": _sm(wifi_scope="ALL_DEVICES"), "accountNumber": "806910023035",
      "diagnostics_triggered": "true"}, None,
     [_Content("user", "my laptop is fine, everything else is down")]),
    ("model turn, no account", "before_model",
     {"sm": _sm()}, None, [_Content("user", "my internet is down")]),
    ("model turn, already triggered", "before_model",
     {"sm": _sm(), "accountNumber": "806910023035",
      "diagnostics_triggered": "true"}, None, [_Content("user", "hello again")]),
    # A turn CES made rather than the caller: an ASYNCHRONOUS tool publishing its result.
    # The ask ladder's per-turn hold has to be released on one of these or the outstanding
    # question is put again in the same words.
    ("model turn, an async completion push", "before_model",
     {"sm": dict(_sm(), _ask_rung_turn={"clarify_reply_device": 2}),
      "accountNumber": "806910023035", "diagnostics_triggered": "true"}, None,
     [_Content("user", "<context>function [SweepLegs_leg_outage_leg] completed with "
                       'response {\n  "result": {}\n}</context>')]),
    ("model turn, an inactivity tick", "before_model",
     {"sm": dict(_sm(), _ask_rung_turn={"clarify_reply_device": 2}),
      "accountNumber": "806910023035", "diagnostics_triggered": "true"}, None,
     [_Content("user", "<context>no user activity detected for 5 seconds.</context>")]),
    # The control: the caller's own words must leave the hold standing, or the question
    # is reworded on the very turn it is first asked.
    ("model turn, the caller speaks (hold must stand)", "before_model",
     {"sm": dict(_sm(), _ask_rung_turn={"clarify_reply_device": 2}),
      "accountNumber": "806910023035", "diagnostics_triggered": "true"}, None,
     [_Content("user", "it is just the tv box")]),
]


# --------------------------------------------------------------------------- #

def _load(source, tag):
  """Import a `hooks.py` given as source text, under a unique module name."""
  path = os.path.join(tempfile.mkdtemp(prefix=f"hooks_{tag}_"), "hooks_mod.py")
  with open(path, "w") as fh:
    fh.write(source)
  spec = importlib.util.spec_from_file_location(f"hooks_{tag}", path)
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


def _run(mod, case):
  """Run one case and return everything the callback wrote, as plain JSON."""
  _name, which, state, events, contents = case
  state = copy.deepcopy(state)
  ctx = _Ctx(state, events or [])
  try:
    if which == "before_agent":
      mod.before_agent_callback(ctx)
    else:
      mod.before_model_callback(ctx, _Req(contents or []))
  except Exception as exc:                                    # noqa: BLE001
    return {"__raised__": f"{type(exc).__name__}: {exc}"}
  sm = state.get("sm") or {}
  # `_log` carries a repr of any swallowed exception, which is signal, not noise.
  return {
      "filled": sm.get("filled") or {},
      "log": [dict(e) for e in (sm.get("_log") or [])],
      "state": {k: v for k, v in state.items() if k != "sm"},
      # The ask ladder's bookkeeping. Not in `filled` and not in `state`, so without it
      # here a hook that stops releasing the per-turn hold -- and therefore asks the same
      # question twice -- reads as no change at all.
      "ask_rung": {k: sm.get(k) for k in ("_ask_rung", "_ask_rung_turn") if k in sm},
  }


def _engine_outcome(mod, case, config, loader):
  """Run the hook, then one engine turn, and return where the turn ended up."""
  _name, which, state, events, contents = case
  if which != "before_agent":
    return None
  state = copy.deepcopy(state)
  sm = loader.seed_sm(config)
  sm.setdefault("filled", {})
  sm["filled"].update((state.get("sm") or {}).get("filled") or {})
  sm.setdefault("pending", {})
  gate = sm.get("_gate_slot") or "active_flow"
  sm[gate] = "repair"
  sm["filled"][gate] = "repair"
  state["sm"] = sm
  ctx = _Ctx(state, events or [])
  try:
    mod.before_agent_callback(ctx)
  except Exception as exc:                                    # noqa: BLE001
    return {"__raised__": f"{type(exc).__name__}: {exc}"}
  text = ""
  for ev in (events or []):
    for part in ev.parts():
      text = part.text or text
  try:
    out = loader.run_engine(config, state["sm"], last_user_text=text,
                            config_id="repair")
  except Exception as exc:                                    # noqa: BLE001
    return {"__engine_raised__": f"{type(exc).__name__}: {exc}"}
  action = out.get("action", {})
  fired = (action.get("task") or {}).get("name") or action.get("task_name")
  if not fired and action.get("function_call"):
    tool = action["function_call"].get("name")
    fired = next((t["name"] for t in config["tasks"] if t.get("tool") == tool), None)
  return {
      "fired": fired,
      "message": action.get("message") or "",
      "hide_tools": sorted(action.get("hide_tools") or []),
      "filled": dict(state["sm"].get("filled") or {}),
  }


def main():
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--base", default="HEAD", help="git ref to compare against")
  ap.add_argument("--show", action="store_true", help="print every write")
  ap.add_argument("--e2e", action="store_true",
                  help="compare hooks THEN the engine, not just what the hooks write")
  ap.add_argument("--app-dir", default="./built")
  args = ap.parse_args()

  old_src = subprocess.run(["git", "show", f"{args.base}:flows-sdk/hooks.py"],
                           cwd=_REPO, capture_output=True, text=True).stdout
  if not old_src:
    raise SystemExit(f"could not read hooks.py at {args.base}")
  new_src = open(os.path.join(_SDK, "hooks.py")).read()

  if old_src == new_src:
    print(f"hooks.py is unchanged from {args.base} — nothing to diff")
    return 0

  old = _load(old_src, "old")
  new = _load(new_src, "new")

  config = engine_loader = None
  if args.e2e:
    sys.path.insert(0, _SDK)
    import labs_paths                                          # noqa: PLC0415
    labs_paths.add_sdk_paths()
    from flows.engine import loader as engine_loader           # noqa: PLC0415
    path = os.path.join(args.app_dir, "tools", "repair_dag", "python_function",
                        "python_code.py")
    ns: dict = {}
    with open(path) as fh:
      exec(compile(fh.read(), path, "exec"), ns)               # noqa: S102
    config = ns["repair_dag"]()
    engine_loader.set_framework_root(
        os.path.join(args.app_dir, "tools"))

  differing = []
  for case in CASES:
    name = case[0]
    if args.e2e:
      a, b = (_engine_outcome(old, case, config, engine_loader),
              _engine_outcome(new, case, config, engine_loader))
      if a is None:
        continue
    else:
      a, b = _run(old, case), _run(new, case)
    if args.show:
      print(f"\n--- {name}\n{json.dumps(b, indent=2, sort_keys=True, default=str)}")
    if a != b:
      differing.append((name, a, b))

  mode = "end-to-end (hooks + engine)" if args.e2e else "hook-level"
  n = sum(1 for c in CASES if not args.e2e or c[1] == "before_agent")
  print(f"\n{mode}: {n} cases, {len(differing)} differ")
  if not differing:
    print("IDENTICAL — both versions write exactly the same thing everywhere")
    return 0

  for name, a, b in differing:
    print(f"\n=== {name}")
    for section in ("fired", "message", "hide_tools", "filled", "state", "log",
                    "__raised__", "__engine_raised__"):
      x, y = a.get(section), b.get(section)
      if x == y:
        continue
      if isinstance(x, dict) and isinstance(y, dict):
        for k in sorted(set(x) | set(y)):
          if x.get(k) != y.get(k):
            print(f"  {section}[{k}]: {x.get(k)!r} -> {y.get(k)!r}")
      else:
        print(f"  {section}: {x!r} -> {y!r}")
  return 1


if __name__ == "__main__":
  raise SystemExit(main())
