#!/usr/bin/env python3
"""Offline proof that a demo build's account numbers really do pick their journeys.

`build.py --demo` exists because a tool fake is a SESSION setting: nobody opening the CES
console sets one, so a console conversation on an ordinary build reaches the live Comcast
backends. The demo build's answer is to bake `cujs.yaml`'s account -> scenario bindings
into the app, so a caller who simply TYPES an account number lands on that CUJ's journey
with nothing seeded.

Nothing tested it. Driven cold, the demo build recognised every account and then reached
the wrong verdict -- an all-clear account was told there was a problem with its account
standing -- and no oracle here could see it, because every other driver seeds the CUJ it
is about to assert on. That is the gap this file closes, and it closes it in the only
place a seeded driver cannot: the two calls it compares differ ONLY in what the session
carries when the caller starts talking.

Two invariants, over the same emitted app:

  A. The baked map is complete and correct. Every CUJ with an account and a scenario must
     appear in `DEMO_ACCOUNTS`, bound to the merged scenario `source_tools` derives. This
     is what catches a CUJ silently dropped from the map -- or a build that never baked
     one at all.

  B. Demo mode agrees with seeding. For each account, the journey walked COLD (nothing
     seeded, no session fakes -- a console caller) must walk the same rungs, in the same
     order, as the same journey walked SEEDED (that CUJ's `mock_config_string` on the
     session and the fakes firing -- what `--cuj` and the live drivers get, and what the
     existing oracles already grade as correct). Making those two agree is the entire
     promise of a demo build.

     The expectation is DERIVED from the seeded walk rather than written down. A table of
     expected verdicts rots the first time a journey changes, and drift nobody noticed is
     exactly the failure mode here. The WHOLE cascade is compared, not just the verdict: a
     demo build that arrives at the right answer having skipped the checks has still lost
     the journey.

The backends are answered by the demo build's OWN code, so this grades the app rather
than a restatement of it: the account gate and both sweep legs are the emitted bodies,
run for real, and the specialists -- the one leg that is an HTTP service and cannot run
offline -- are answered by the proxy's own `_fixture`, which is what a demo build gets
from it. Their inputs are whatever the engine hands them, so the account gate is the only
thing that can make the two walks diverge, which is the point.

    python tests/demo_check.py [--app-dir ./built_demo] [--verbose]

Build first, with the same flags the console gets:

    python build.py --out ./built_demo --demo
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import io
import os
import sys
import typing

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths()

import flows  # noqa: E402
import harness  # noqa: E402
import source_tools  # noqa: E402

#: The caller's opening line. Broad by cue, so the clarification gate stays shut and the
#: account number is the only thing steering the call.
DOWN = "hi my internet is down"

#: The remote specialists. Not in `harness.BACKEND_TOOLS` because the harness answers it
#: in two phases; it is a backend stop here all the same.
SPECIALISTS = "resolve_specialists_remote"

#: A demo build only claims to be honest about the repair journey's backends.
BACKENDS = (harness.BACKEND_TOOLS | {SPECIALISTS}) - {"xfinity_faq_search"}

#: `demo_delay` is a latency knob (`DEMO_SWEEP_SECONDS`), not a journey binding, so gate A
#: asserts it is PRESENT rather than pinning the number a particular build was made with.
LATENCY_KEY = "demo_delay"


def _parse(query: str) -> dict:
  pairs = (p.partition("=") for p in str(query or "").split("&") if p)
  return {k.strip(): v.strip() for k, sep, v in pairs if sep}


@functools.cache
def _proxy_fixture():
  """The specialist proxy's canned answer, `_fixture`, imported without its server.

  The proxy is a FastAPI app that builds a CES client at module scope, so importing it
  needs both stubbed -- the same two `specialist_proxy/selftest.py` installs, and for the
  same reason: this must run with no network and no credentials. `_fixture` itself is a
  pure function of `mock_config_string`.

  Borrowed rather than restated. It is the demo build's real specialist answer, and a
  second copy of the mapping here would eventually disagree with it and grade the wrong
  thing green.
  """
  import logging  # noqa: PLC0415
  import types as pytypes  # noqa: PLC0415

  class _App:
    def __init__(self, **_kw):
      pass

    def get(self, _path, **_kw):
      return lambda fn: fn

    def post(self, _path, **_kw):
      return lambda fn: fn

  fastapi = pytypes.ModuleType("fastapi")
  fastapi.FastAPI = _App
  openapi_utils = pytypes.ModuleType("fastapi.openapi.utils")
  openapi_utils.get_openapi = lambda **_kw: {}
  openapi = pytypes.ModuleType("fastapi.openapi")
  openapi.utils = openapi_utils
  sys.modules.setdefault("fastapi", fastapi)
  sys.modules.setdefault("fastapi.openapi", openapi)
  sys.modules.setdefault("fastapi.openapi.utils", openapi_utils)

  import google.auth  # noqa: PLC0415

  # Left alone if something real is already configured; without this the default lookup
  # reaches for the metadata server on a machine that has no ADC.
  google.auth.default = lambda **_kw: (object(), "demo_check")  # type: ignore[assignment]

  os.environ.setdefault("JOB_STORE", "memory")
  sys.path.insert(0, os.path.join(os.path.dirname(HERE), "specialist_proxy"))
  # A service configures logging for itself at import, and announces its job store while
  # doing it. Left in place the handler it attaches to the ROOT logger makes every engine
  # line the walk produces arrive twice.
  root = logging.getLogger()
  before = (list(root.handlers), root.level)
  logging.disable(logging.CRITICAL)
  try:
    import main as proxy  # noqa: PLC0415
  finally:
    logging.disable(logging.NOTSET)
    root.handlers, root.level = before
  return proxy._fixture  # noqa: SLF001


# --- The demo build's own backends, run offline ------------------------------


class _Ctx:
  """The CES-injected `context` global, over one call's session state."""

  def __init__(self, state: dict):
    self.state = state
    self.variables: dict = {}


class _Tools:
  """The CES-injected `tools` global: a tool calling one of its siblings.

  `fakes` is `use_tool_fakes`, and it is the whole distinction this file is about. With it
  set the callee answers from its recorded `toolFakeConfig`, which is what a seeded driver
  gets; without it the callee runs its real body, which is what the CES console gets and
  what the demo build has to make work by itself.
  """

  def __init__(self, app_dir: str, ctx: _Ctx, fakes: bool):
    self._app_dir = app_dir
    self._ctx = ctx
    self._fakes = fakes

  def __getattr__(self, name: str):
    def call(args: dict | None = None, **kwargs):
      merged = dict(args or {})
      merged.update(kwargs)
      return self.run(name, merged)
    return call

  def _namespace(self) -> dict:
    # The globals CES injects, plus the typing names a lifted fixture annotates with: a
    # fixture is a `code_block`, so its imports live in the host rather than in the file.
    return {"context": self._ctx, "tools": self,
            "Any": typing.Any, "Optional": typing.Optional,
            "Tool": object, "CallbackContext": object}

  def _exec(self, path: str) -> dict:
    namespace = self._namespace()
    with open(path) as fh:
      exec(compile(fh.read(), path, "exec"), namespace)  # noqa: S102 - our own app
    return namespace

  def run(self, name: str, args: dict):
    """Call one of the app's tools the way the runtime would.

    The source app's tool bodies print an audit line per call, and a fixture prints the
    mode it chose. Useful in a Cloud Logging trace, and here they would bury the report,
    so they are captured and dropped.
    """
    fake = os.path.join(self._app_dir, "tools", name, "tool_fake_config", "code_block",
                        "python_code.py")
    body = os.path.join(self._app_dir, "tools", name, "python_function",
                        "python_code.py")
    with contextlib.redirect_stdout(io.StringIO()):
      if self._fakes and os.path.exists(fake):
        return self._exec(fake)["fake_tool_call"](None, dict(args), self._ctx)
      return self._exec(body)[name](**args)


def _answer_backend(call: harness.Call, tools: _Tools) -> str | None:
  """Answer whatever backend the engine is asking for. Returns the rung, or None."""
  tool = call.pending_tool()
  if tool not in BACKENDS:
    return None
  task = call.task_for_tool(tool)
  args = dict((call.result.get("function_call") or {}).get("args") or {})
  if tool == SPECIALISTS:
    # The specialists are a remote job against a service, so there is no body to run. The
    # proxy resolves a demo build's answer from the `mock_config_string` it is handed, and
    # that argument is a declared output of the account gate -- so a gate that fails to
    # publish the scenario shows up here as healthy specialists, which is precisely the
    # drift worth catching.
    call.task_returns(task, resolve_specialists_remote__job="JOB1")
    call.remote_returns(resolve_specialists_remote__job="JOB1",
                        **_proxy_fixture()(args.get("mock_config_string") or ""))
    return task
  returned = tools.run(tool, args)
  call.task_returns(task, **(returned if isinstance(returned, dict) else {}))
  return task


def walk_call(app_dir: str, config: dict, account: str, seeded: str | None
              ) -> tuple[list[str], list[str]]:
  """One whole call against the demo build. Returns `(rungs fired, lines heard)`.

  `seeded` is the caller's session: a `mock_config_string` (and the tool fakes that go
  with it) for a driver, `None` for someone who has just clicked the app link.

  A cold walk whose gate finds no account binding falls through to the real context hub,
  which is not reachable from here -- so it fails deterministically rather than returning
  what the hub would have said. That is the right verdict for this check either way: a
  demo build reaching for a live backend is the defect, not the answer it gets.
  """
  call = harness.new_call(config, app_dir=app_dir, flow="repair", account=account)
  if seeded:
    call.state["mock_config_string"] = seeded
  tools = _Tools(app_dir, _Ctx(call.state), fakes=bool(seeded))

  rungs: list[str] = []
  call.turn(DOWN)
  # Each pass runs the engine's cascade out to the next backend stop and answers it. The
  # bound is a stall guard: a demo build whose gate errors ends the call inside two.
  for _ in range(8):
    fired, _ = call.drive()
    rungs.extend(fired)
    answered = _answer_backend(call, tools)
    if answered is None:
      break
    rungs.append(answered)
  return rungs, call.transcript


# --- Gate A: the baked map ---------------------------------------------------


def _baked_accounts(app_dir: str) -> dict | None:
  """`DEMO_ACCOUNTS` as the emitted account gate carries it, or None if it has none."""
  path = os.path.join(app_dir, "tools", source_tools.CONTEXT_GATE, "python_function",
                      "python_code.py")
  if not os.path.exists(path):
    raise SystemExit(f"demo_check: {app_dir} has no {source_tools.CONTEXT_GATE}; "
                     f"build it first")
  namespace: dict = {}
  with open(path) as fh:
    exec(compile(fh.read(), path, "exec"), namespace)  # noqa: S102 - our own emitted file
  return namespace.get("DEMO_ACCOUNTS")


def gate_baked_map(app_dir: str, wanted: dict) -> list[str]:
  """Every CUJ binding must be in the emitted map, and nothing else may be."""
  baked = _baked_accounts(app_dir)
  if baked is None:
    return [f"the emitted {source_tools.CONTEXT_GATE} carries no DEMO_ACCOUNTS at all, "
            f"so no account picks a journey and every cold caller gets whatever the live "
            f"backends say ({len(wanted)} binding(s) dropped)"]
  failures = []
  for account, scenario in sorted(wanted.items()):
    if account not in baked:
      failures.append(f"account {account} is bound in cujs.yaml but missing from the "
                      f"baked map, so a caller giving it reaches an unknown account")
      continue
    want, got = _parse(scenario), _parse(baked[account])
    if not got.get(LATENCY_KEY):
      failures.append(f"account {account} is baked without {LATENCY_KEY}, so the "
                      f"specialist job lands inside the turn that started it and the "
                      f"wait a demo exists to show is not there")
    want.pop(LATENCY_KEY, None)
    got.pop(LATENCY_KEY, None)
    if want != got:
      failures.append(f"account {account} is baked as {got}, wanted {want}")
  for extra in sorted(set(baked) - set(wanted)):
    failures.append(f"the baked map binds {extra}, which no CUJ names")
  return failures


# --- Gate B: cold agrees with seeded -----------------------------------------


def _verdicts(config: dict, rungs: list[str]) -> list[str]:
  """The rungs that ANSWER the caller, read off the config rather than listed here.

  A rung answers if a `verdict_*` tool speaks for it; everything else -- the gate, the two
  sweep legs, the settle -- is plumbing. Only used to report the journeys readably and to
  tell a real baseline from a call that fell over before it said anything; the comparison
  itself is over the WHOLE cascade, because a demo build that reaches the right verdict by
  a different route has still lost the journey.
  """
  tools = {t["name"]: str(t.get("tool") or "") for t in config.get("tasks", [])}
  return [r for r in rungs if tools.get(r, "").startswith("verdict_")]


def gate_cold_matches_seeded(app_dir: str, config: dict, bindings: dict, verbose: bool
                             ) -> list[str]:
  failures = []
  for account, scenario in sorted(bindings.items()):
    seeded_rungs, seeded_lines = walk_call(app_dir, config, account, scenario)
    cold_rungs, cold_lines = walk_call(app_dir, config, account, None)
    # A seeded walk that answers nothing is not a baseline, it is a broken measurement,
    # and comparing two silences would report agreement.
    if not _verdicts(config, seeded_rungs):
      failures.append(f"{account}: the seeded walk reached no verdict at all, so there "
                      f"is nothing to hold the cold walk to")
      status = "FAIL"
    elif seeded_rungs != cold_rungs:
      failures.append(f"{account}: cold walked {cold_rungs}, seeded walked {seeded_rungs}")
      status = "FAIL"
    else:
      status = "ok  "
    print(f"{status} {account}  seeded {_verdicts(config, seeded_rungs)}  "
          f"cold {_verdicts(config, cold_rungs)}")
    if verbose or status == "FAIL":
      for line in cold_lines:
        print(f"       cold   | {line}")
      for line in seeded_lines:
        print(f"       seeded | {line}")
  return failures


def run(app_dir: str, verbose: bool) -> int:
  bindings = source_tools._demo_account_scenarios()  # noqa: SLF001
  config = harness.load_config(app_dir, "repair")
  print(f"app dir: {app_dir}\n{len(bindings)} account binding(s) from cujs.yaml "
        f"({len(flows.load_cujs(start=os.path.dirname(HERE)).names())} CUJs)\n")

  failures = gate_baked_map(app_dir, bindings)
  for failure in failures:
    print(f"FAIL map: {failure}")
  print()
  behaviour = gate_cold_matches_seeded(app_dir, config, bindings, verbose)
  print()
  for failure in behaviour:
    print(f"FAIL cold: {failure}")

  total = len(failures) + len(behaviour)
  print(f"\n{len(bindings) - len(behaviour)}/{len(bindings)} accounts reach the same "
        f"journey cold as seeded" + (f", {len(failures)} map failure(s)"
                                     if failures else ""))
  return 1 if total else 0


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("--app-dir", default="built_demo")
  parser.add_argument("--verbose", action="store_true",
                      help="print both transcripts for every account")
  args = parser.parse_args()
  raise SystemExit(run(args.app_dir, args.verbose))
