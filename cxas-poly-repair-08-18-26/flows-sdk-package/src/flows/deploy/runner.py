"""The ONE place a `cxas` subprocess is launched — and the one place tests mock.

There used to be two default runners. Slot Studio's shelled out with
``asyncio.create_subprocess_exec``; Specter installed its own over the top of the
module global before every push because that one deadlocked. Which runner you got
depended on whether a Specter build had happened yet in the same process — the exact
divergence this package exists to end. There is one default now, and it is the fixed
one.

Two things in here are bug fixes, not preferences:

1. **Fork from a WORKER THREAD, not the event loop.** ``asyncio.create_subprocess_exec``
   forks on the event-loop thread, and this process is full of grpc/google clients
   (GRPC_ENABLE_FORK_SUPPORT is on) whose ``atfork`` handlers can deadlock the forking
   thread. That froze the whole control plane the instant a deploy started: loop
   blocked, SSE heartbeat stopped, every product's UI showing "Reconnecting…".
   A blocking ``Popen``/``communicate`` inside ``asyncio.to_thread`` keeps the loop free,
   and its timeout is what actually kills a stuck deploy — the thread itself is
   un-cancellable, so the coroutine reaps the process GROUP on cancellation instead.

2. **Resolve the interpreter, don't trust ``sys.executable``.** On macOS dev the server
   is often launched as ``frameworkpython .venv/bin/uvicorn``, so ``sys.executable`` is
   a framework Python whose site-packages lack ``cxas_scrapi`` — the push then failed
   with a cryptic ModuleNotFoundError reported to the author as "missing cxas". We probe
   the likely venv interpreters and use the first that can actually import it.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Tuple

# A runner takes the argv (NOT including the cxas program) and returns
# (return_code, stdout, stderr). The default runner prepends the cxas program.
PushRunner = Callable[[List[str]], Awaitable[Tuple[int, str, str]]]

# Default timeout for a push subprocess (seconds). A real push uploads + deploys,
# so this is generous relative to a single chat-step turn.
DEFAULT_PUSH_TIMEOUT_S = 300.0

#: Hard ceiling for the subprocess itself. The thread that runs it is un-cancellable,
#: so the wait's own timeout is what actually kills a stuck deploy.
_PUSH_SUBPROCESS_TIMEOUT_S = 300

#: Cached (python_path, error) once resolved — probing is a subprocess so do it once.
_CXAS_PY: Optional[Tuple[Optional[str], str]] = None

#: Substrings marking a benign, non-causal stderr line (import-time warnings from
#: transitive deps). Dropped from captured stderr so a deploy failure's surfaced error
#: reflects the REAL cause and not noise — a model reading "Couldn't find ffmpeg" in a
#: failed deploy will confidently misdiagnose it as a missing system dependency.
_BENIGN_STDERR_MARKERS = (
    "ffmpeg", "avconv", "pydub", "RuntimeWarning", "DeprecationWarning",
    "UserWarning", "warn(", "warnings.warn",
)


def strip_benign_stderr(stderr: str, *, drop_blank: bool = False) -> str:
    """Drop benign warning lines from captured stderr.

    ``drop_blank`` also drops whitespace-only lines. That variant exists for the one
    caller that filters stderr it did NOT capture itself — a product may inject its own
    runner, whose stderr never passed through here — and wants a "is there anything
    substantive left?" answer rather than a cleaned stream. It used to be a second,
    narrower marker list living in ``service.py``, which is exactly the divergence
    (a warning class filtered in one place and not the other) this package exists to end.
    """
    if not stderr:
        return stderr
    kept = [ln for ln in stderr.splitlines()
            if not any(m in ln for m in _BENIGN_STDERR_MARKERS)
            and not (drop_blank and not ln.strip())]
    return "\n".join(kept).strip()


def resolve_cxas_python() -> Tuple[Optional[str], str]:
    """Find an interpreter that can ``import cxas_scrapi`` (the deploy CLI lives there).

    Probes VIRTUAL_ENV / UV_PROJECT_ENVIRONMENT (dev.sh sets the latter to
    ``service/.venv``), then sys.executable / sys.prefix, and uses the first that can
    import it. Returns ``(None, message)`` with an actionable hint if the deploy
    toolchain isn't installed anywhere reachable."""
    global _CXAS_PY
    if _CXAS_PY is not None:
        return _CXAS_PY
    # NB: only a SUCCESS is cached (see the end of this function). Caching the failure
    # would let one transient probe hiccup wedge every deploy until the server restarts.
    candidates: List[str] = []
    for env in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        base = os.environ.get(env, "").strip()
        if base:
            candidates.append(os.path.join(base, "bin", "python"))
    candidates += [sys.executable, os.path.join(sys.prefix, "bin", "python")]
    seen: set = set()
    for py in candidates:
        if not py or py in seen or not os.path.exists(py):
            continue
        seen.add(py)
        try:
            r = subprocess.run([py, "-c", "import cxas_scrapi"], capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            continue
        if r.returncode == 0:
            _CXAS_PY = (py, "")
            return _CXAS_PY
    return (
        None,
        "cxas deploy toolchain unavailable: 'cxas_scrapi' is not importable by any "
        "candidate interpreter (VIRTUAL_ENV / UV_PROJECT_ENVIRONMENT / sys.executable). "
        "Install the 'service' dependency group into the server venv — e.g. "
        "`uv sync --group service`.",
    )


def cxas_argv_prefix() -> Tuple[Optional[List[str]], str]:
    """``(prefix, error)`` — how to invoke cxas, or why we can't.

    Preference order merges what the two old runners each did:

    1. The probed interpreter's ``-m cxas_scrapi.cli.main`` (Specter's fix). This is
       deliberately tried BEFORE the console script: ``Path(sys.executable).resolve()``
       follows symlinks from the venv python to the macOS framework python and picks up
       a broken framework ``cxas`` (missing google-cloud-storage).
    2. The ``cxas`` console-script next to ``sys.executable`` (Slot Studio's runner).
       Reached only when nothing importable was found, so a working console script is
       still better than refusing outright.
    """
    cxas_py, err = resolve_cxas_python()
    if cxas_py:
        return [cxas_py, "-m", "cxas_scrapi.cli.main"], ""
    cxas_bin = Path(sys.executable).resolve().parent / "cxas"
    if cxas_bin.is_file():
        return [str(cxas_bin)], ""
    return None, err


def kill_process_group(proc: "subprocess.Popen") -> None:
    """SIGKILL the subprocess AND everything it spawned.

    ``start_new_session=True`` puts the child in its own process group, so this signals
    the whole tree without ever touching our own. Killing just the lead process (which
    is all ``Popen.kill`` — and therefore ``subprocess.run``'s timeout path — does)
    orphans its children: `cxas push` shells out to terraform, which would keep running
    against the deployment target long after we reported a timeout.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Already reaped, or no killpg (non-POSIX). Fall back to the lead process.
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 — best-effort teardown, never the caller's problem
            pass


async def default_push_runner(argv: List[str]) -> Tuple[int, str, str]:
    """Execute ``cxas <argv>`` as a subprocess; return (rc, stdout, stderr).

    ``argv`` is everything after the cxas program (i.e. starts with ``push``). A
    missing cxas toolchain surfaces as rc=127 so the caller reports it cleanly.
    See the module docstring for why this forks from a worker thread.
    """
    prefix, err = cxas_argv_prefix()
    if prefix is None:
        return 127, "", err
    cmd = prefix + argv

    # Silence benign import-time warnings in the deploy subprocess (cxas_scrapi
    # transitively imports pydub, which warns "Couldn't find ffmpeg…"). Warnings are
    # never the error channel here (returncode + real messages are).
    env = {**os.environ, "PYTHONWARNINGS": "ignore"}

    # Published so the coroutine can reap the tree if the CALLER's timeout fires first.
    # The worker thread is un-cancellable, so without this the subprocess would keep
    # deploying (and terraform keep applying) after `run_push` gave up on it.
    live: List["subprocess.Popen"] = []
    abandoned = threading.Event()

    def _run() -> Tuple[int, str, str]:
        # Popen rather than subprocess.run: run() only ever kills the LEAD process on
        # timeout. Everything else about the call is deliberately unchanged — same
        # blocking wait, same worker thread (see the module docstring: forking off the
        # event loop is the grpc-atfork deadlock fix), same new session.
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                # New session so a kill doesn't also signal our own process group, and
                # so the whole tree can be reaped by group.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return 127, "", f"cxas interpreter not found: {exc}"
        live.append(proc)
        if abandoned.is_set():
            # The caller gave up between the fork and here; don't leave it running.
            kill_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=_PUSH_SUBPROCESS_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            stdout, stderr = proc.communicate()   # drain the pipes of the dead tree
            return (
                124,
                stdout or "",
                strip_benign_stderr(stderr or "")
                + f"\ncxas push timed out after {_PUSH_SUBPROCESS_TIMEOUT_S}s",
            )
        return (
            proc.returncode if proc.returncode is not None else 1,
            stdout or "",
            strip_benign_stderr(stderr or ""),
        )

    try:
        return await asyncio.to_thread(_run)
    except asyncio.CancelledError:
        # `run_push`'s wait_for timed out, or the request was dropped. The thread runs
        # on regardless, so kill the tree here — otherwise a deploy we already reported
        # as timed out carries on writing to the target.
        abandoned.set()
        for proc in live:
            kill_process_group(proc)
        raise


# The single injectable subprocess seam every test mocks. Module-global so every
# product (and tests) share one runner; `set_runner` swaps it for a fake.
_runner: PushRunner = default_push_runner


def set_runner(runner: PushRunner) -> None:
    """Test seam: replace the push subprocess runner (tests inject a fake)."""
    global _runner
    _runner = runner


def get_runner() -> PushRunner:
    """The active push runner (default shells out; tests inject a fake)."""
    return _runner


async def run_push(
    argv: List[str], *, timeout_s: float = DEFAULT_PUSH_TIMEOUT_S
) -> Tuple[int, str, str]:
    """Run one push invocation through the active runner with a timeout.

    The ONE place a subprocess is launched, so a test that mocks the runner is
    guaranteed never to touch the real CLI. Raises :class:`asyncio.TimeoutError`
    on timeout (mapped to an error result by :mod:`flows.deploy.service`).
    """
    return await asyncio.wait_for(_runner(argv), timeout=timeout_s)
