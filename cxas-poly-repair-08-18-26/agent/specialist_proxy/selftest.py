#!/usr/bin/env python3
"""Offline self-test for the specialist proxy: the two failures a reading finds.

Both are concurrency, both are invisible from a green deploy, and both were found by
review rather than by driving:

  * the job store's fall back to memory is meant to be SURVIVABLE. Read as
    `if self._fs: self._fs.put(...)` it was not: another thread degrading in the window
    between the check and the call turned it into `AttributeError: 'NoneType'` on an
    unrelated caller.
  * `future.result(timeout=)` does not abort anything. With the pool closed by
    `with ...:` -- which exits through `shutdown(wait=True)` -- a wedged specialist held
    the job's own thread past every timeout, so the job never reached a terminal status
    and the agent saw `remote_job_lost`.

No network and no credentials: the CES client, `google.auth` and FastAPI are stubbed
before `main` is imported, and the store is forced to memory. Runs in `make check`.

    python specialist_proxy/selftest.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types as pytypes

HERE = os.path.dirname(os.path.abspath(__file__))

# ── Stubs, installed before `main` is imported ───────────────────────────────
os.environ["JOB_STORE"] = "memory"


class _FakeApp:
  """Just enough FastAPI for the decorators at module scope."""

  def __init__(self, **_kw):
    self.routes = []
    self.openapi_schema = None

  def _register(self, _path):
    def deco(fn):
      self.routes.append(fn)
      return fn
    return deco

  def get(self, path, **_kw):
    return self._register(path)

  def post(self, path, **_kw):
    return self._register(path)


_fastapi = pytypes.ModuleType("fastapi")
_fastapi.FastAPI = _FakeApp
_openapi_utils = pytypes.ModuleType("fastapi.openapi.utils")
_openapi_utils.get_openapi = lambda **_kw: {}
_openapi_pkg = pytypes.ModuleType("fastapi.openapi")
_openapi_pkg.utils = _openapi_utils
sys.modules.setdefault("fastapi", _fastapi)
sys.modules.setdefault("fastapi.openapi", _openapi_pkg)
sys.modules.setdefault("fastapi.openapi.utils", _openapi_utils)

import google.auth  # noqa: E402

google.auth.default = lambda **_kw: (object(), "selftest")  # type: ignore[assignment]

from google.cloud import ces_v1beta  # noqa: E402

_run_session_calls: list[dict] = []


class _FakeSessionClient:
  def __init__(self, **_kw):
    pass

  def run_session(self, request=None, timeout=None, **_kw):  # noqa: ARG002
    _run_session_calls.append({"timeout": timeout})
    raise RuntimeError("selftest: no CES here")


ces_v1beta.SessionServiceClient = _FakeSessionClient  # type: ignore[assignment]

sys.path.insert(0, HERE)
import main  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
  print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"\n       {detail}" if detail else ""))
  if not ok:
    FAILURES.append(name)


# ── 1. The store survives a degrade racing a write ───────────────────────────
def test_store_survives_a_concurrent_degrade() -> None:
  """A `_degrade()` between the check and the call must not raise on this thread.

  Modelled exactly: a store whose backing `put` degrades from INSIDE the call, which is
  the real ordering (the exception handler is what nulls `_fs`). The pre-fix code
  re-read `self._fs` for the call and so blew up on an interleaving; this asserts the
  caller sees a degraded store and no exception, which is what "falls back" means.
  """
  store = main.ResilientStore()

  class _Flaky(main.JobStore):
    name = "flaky"

    def __init__(self):
      self.hits = 0

    def put(self, job_id, doc):
      self.hits += 1
      store._degrade("write", RuntimeError("permission denied"))  # noqa: SLF001
      raise RuntimeError("permission denied")

    def get(self, job_id):
      store._degrade("read", RuntimeError("permission denied"))  # noqa: SLF001
      raise RuntimeError("permission denied")

  store._fs = _Flaky()  # noqa: SLF001
  store.name = "firestore"
  try:
    store.put("j1", {"status": "running"})
    store.get("j1")
    raised = ""
  except AttributeError as exc:
    raised = f"{type(exc).__name__}: {exc}"
  check("a degrade racing a write does not raise on the caller's thread",
        raised == "", raised)
  check("...and the store really did fall back", store.degraded and store.name == "memory")
  check("...and the write still landed in memory",
        (store.get("j1") or {}).get("status") == "running")


def test_store_survives_two_threads_degrading_at_once() -> None:
  """The same thing under real contention, which is how it would reach production."""
  store = main.ResilientStore()
  errors: list[str] = []

  class _Flaky(main.JobStore):
    name = "flaky"

    def put(self, job_id, doc):
      time.sleep(0.001)
      raise RuntimeError("permission denied")

    def get(self, job_id):
      time.sleep(0.001)
      raise RuntimeError("permission denied")

  store._fs = _Flaky()  # noqa: SLF001

  def hammer(n: int) -> None:
    for i in range(40):
      try:
        store.put(f"j{n}-{i}", {"status": "running"})
        store.get(f"j{n}-{i}")
      except Exception as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")

  threads = [threading.Thread(target=hammer, args=(n,)) for n in range(6)]
  for t in threads:
    t.start()
  for t in threads:
    t.join()
  check("six threads degrading concurrently raise nothing", not errors,
        "; ".join(sorted(set(errors))[:2]))


# ── 2. A hung specialist does not hold the job's own thread ──────────────────
def test_a_hung_specialist_does_not_wedge_the_job() -> None:
  """`.result(timeout=)` abandons the result; the pool's exit used to JOIN the worker.

  One leg never returns. The job must still reach a terminal status inside its budget,
  because that is what the agent polls -- a job stuck `running` past its heartbeat is
  reported `remote_job_lost` and retried into a second wedged pair.
  """
  release = threading.Event()
  original = main._run_specialist  # noqa: SLF001
  original_floor = main.MIN_LEG_SECONDS

  def _fake(agent, text, variables, use_fakes, timeout):  # noqa: ARG001
    if agent == main.NETWORK_AGENT:
      release.wait(60)          # wedged: ignores its own deadline, as a hang would
      return {}
    return {"report": {}, "state": {}, "tool_results": [], "elapsed": 0.1}

  main._run_specialist = _fake  # noqa: SLF001
  main.MIN_LEG_SECONDS = 1.0    # the only thing shortened; the wedge is real
  try:
    body = main.StartIn(accountNumber="8069100230359946", deadline_seconds=1)
    t0 = time.time()
    main._work("selftest-hung", body)  # noqa: SLF001
    elapsed = time.time() - t0
  finally:
    release.set()
    main.MIN_LEG_SECONDS = original_floor
    main._run_specialist = original  # noqa: SLF001

  doc = main.STORE.get("selftest-hung") or {}
  # Under `with ThreadPoolExecutor(...)` this took the full 60s the leg hangs for: the
  # timeout below fired at 1s and the closing brace then joined the worker anyway.
  check("the job finishes while a leg is still hung",
        elapsed < 15, f"took {elapsed:.1f}s (budget 1s)")
  check("...and lands on a TERMINAL status the poll can read",
        doc.get("status") in ("done", "failed"), f"status={doc.get('status')!r}")


def test_the_rpc_itself_carries_the_deadline() -> None:
  """The client is the only thing that can actually end the call, so the budget has to
  reach it. Without this the worker sits in `run_session` for as long as CES takes."""
  _run_session_calls.clear()
  try:
    main._run_specialist(main.NETWORK_AGENT, "x", {}, False, 17.0)  # noqa: SLF001
  except Exception:  # noqa: BLE001, S110
    pass
  passed = [c["timeout"] for c in _run_session_calls]
  check("run_session is called with the leg's own timeout", passed == [17.0], str(passed))


def test_every_declared_output_is_always_sent() -> None:
  """Both branches must send every key the remote tool declares — always, and EMPTY
  rather than absent when there is nothing to say.

  A declared output is lifted BY NAME into the generated wrapper, so a key the result
  omits arrives as None, counts as `missing from response`, and the task never
  completes. `_derive` used to omit `technician_type` on a healthy line while `_fixture`
  emptied it, and two branches disagreeing about the key set is invisible until a caller
  whose line is perfectly healthy is told the checks could not be finished, and
  transferred. Measured cold on a real account before the fix: healthy/healthy derived
  correctly, and the journey still ended in `verdict_no_telemetry`.

  The expected set is READ from the agent's own `remote_tool(outputs={...})`. A contract
  asserted against a hand-written copy of itself proves nothing — that is how the local
  and remote paths drifted apart in the first place.

  Searched for by CONTENT across the copy modules rather than read from one hardcoded
  path. It used to name `app.py`, and when the sweep moved to
  `journeys/diagnostics_sweep.py` this failed loudly with an empty set — which is the
  right way round, but the next move should not need a code change here.
  """
  import glob
  import re

  root = os.path.join(HERE, "..")
  block = None
  for path in ([os.path.join(root, "app.py")]
               + sorted(glob.glob(os.path.join(root, "journeys", "*.py")))):
    with open(path) as fh:
      block = re.search(r"resolve_specialists_remote = flows\.remote_tool\((.*?)\n\)",
                        fh.read(), re.S)
    if block:
      break
  keys = set(re.findall(r'"(\w+)":\s*str', block.group(1))) if block else set()
  # That call carries `params` as well; the outputs are what a task can read back.
  declared = keys - {"accountNumber", "cable_modem_mac", "mock_config_string"}
  check("read the declared outputs off the remote_tool contract", len(declared) == 7,
        str(sorted(declared)))

  # A healthy line recommends nobody, which is the weakest case for "always send it".
  healthy = main._derive(  # noqa: SLF001
      {"report": {"network_status": "healthy"}, "state": {}, "tool_results": []},
      {"report": {"gateway_status": "healthy"}, "state": {}, "tool_results": []})
  absent = sorted(declared - set(healthy))
  check("_derive sends every declared output on a healthy line", not absent,
        f"absent: {absent or 'none'}")
  check("...and 'no technician' is an EMPTY value, not an absent key",
        healthy.get("technician_type") == "", repr(healthy.get("technician_type")))

  fixture = main._fixture("network_status=clear&gateway_status=clear")  # noqa: SLF001
  absent = sorted(declared - set(fixture))
  check("_fixture sends every declared output too", not absent,
        f"absent: {absent or 'none'}")
  check("...and the two branches agree on the key set",
        set(healthy) == set(fixture),
        f"derive-only={sorted(set(healthy) - set(fixture))} "
        f"fixture-only={sorted(set(fixture) - set(healthy))}")


if __name__ == "__main__":
  test_store_survives_a_concurrent_degrade()
  test_store_survives_two_threads_degrading_at_once()
  test_a_hung_specialist_does_not_wedge_the_job()
  test_the_rpc_itself_carries_the_deadline()
  test_every_declared_output_is_always_sent()
  print()
  if FAILURES:
    print(f"{len(FAILURES)} specialist-proxy self-test failure(s)")
    raise SystemExit(1)
  print("specialist proxy: all self-tests pass")
