"""The deploy itself: gates -> render -> materialize -> `cxas push` -> read the result.

This is the recipe that existed three times. Slot Studio ran it inside a FastAPI
route handler; Specter called that route function in-process to get at it;
slotfill_migration called it too, from a sync thread via ``asyncio.run``. Every one
of them therefore depended on a web framework to deploy an app, and any fix to the
recipe had to be applied in a place none of the other two could see.

:func:`push` is that recipe with the web server taken out. It never raises: every
failure (gate block, bad config, missing agent dir, nonzero exit, timeout, missing
cxas) comes back as ``PushOutcome(ok=False, …)`` with a typed ``error_kind``, because
its callers are a UI, an autonomous agent loop, and a batch job — none of which can
do anything useful with a traceback.

Two payload shapes, as before:

* **Config-only** (``config``/``config_id``, no ``app_files``) — render the canvas
  config and push the configured on-disk ``env.agent_dir``.
* **Whole-app** (``app_files`` present) — run the gates, fold the config(s) into the
  app's real ``*_dag`` tools, materialize the file SET to a scratch dir, push that,
  discard it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from ..config import config_io
from . import argv as argv_mod
from . import gates, render, runner, target, workdir
from .env import DeployEnv
from .errors import DagUnresolvedError, RenderFailedError
from .models import PrePushReport, PrePushRequest, PushOutcome, PushSpec

logger = logging.getLogger(__name__)


#: Runs the pre-push gates for a request. Injected so a product can wrap them (Slot
#: Studio supplies its own settings-bound `prepush.run`) without this module knowing
#: anything about that product.
GateRunner = Callable[[PrePushRequest], PrePushReport]


async def push(
    spec: PushSpec,
    env: Optional[DeployEnv] = None,
    *,
    gate_runner: Optional[GateRunner] = None,
    timeout_s: Optional[float] = None,
    error_classifier: Optional[Callable[[str, str, int], Optional[str]]] = None,
) -> PushOutcome:
    """Render ``spec`` and deploy it via ``cxas push``. Never raises."""
    env = env or DeployEnv()
    run_gates_fn: GateRunner = gate_runner or (lambda req: gates.run(req, env))
    timeout_s = runner.DEFAULT_PUSH_TIMEOUT_S if timeout_s is None else timeout_s
    classify = error_classifier or argv_mod.classify_push_error
    has_app_files = bool(spec.app_files)
    logger.info(
        "[push] ENTER has_app_files=%s n_files=%d strict=%s run_gates=%s",
        has_app_files, len(spec.app_files or []), spec.strict, spec.run_gates,
    )

    # 1. Pre-push gates: for a whole-app payload, run them over the effective
    #    tools tree BEFORE any subprocess. Any failing check blocks — the push
    #    never runs. The legacy config-only path keeps its original (un-gated)
    #    behavior so existing local pushes are unaffected.
    if has_app_files and spec.run_gates:
        _t = time.monotonic()
        logger.info("[push] gates: starting gates.run …")
        # Run the gates OFF the event loop: they validate (and under strict,
        # import/execute) the generated tool code, which can take seconds — or
        # hang on bad codegen. On the loop that freezes the WHOLE server (every
        # product, the SSE heartbeat, /healthz). In a thread it can't, and the
        # caller's per-tool timeout/cancel can actually take effect.
        gate_report = await asyncio.to_thread(
            run_gates_fn,
            PrePushRequest(
                app_files=spec.app_files,
                config=spec.config,
                config_id=spec.config_id,
                strict=spec.strict,
            ),
        )
        logger.info("[push] gates: gates.run done in %.0fms ok=%s",
                    (time.monotonic() - _t) * 1000.0, gate_report.ok)
        if not gate_report.ok:
            return PushOutcome(
                ok=False,
                error="Pre-push gates failed; fix the flagged checks and retry.",
                error_kind="gate_failed",
                gate_report=gate_report,
            )

    # 2. Config-only payloads still render the canvas config so what we push is
    #    what the author sees (a bad/empty config is a client error, ok=False).
    if not has_app_files:
        try:
            config = config_io.import_config(
                config_id=spec.config_id,
                raw_dict=spec.config.model_dump(by_alias=True, exclude_none=True)
                if spec.config is not None
                else None,
            )
            config_io.export_python(config, spec.config_id or "config")
        except config_io.ConfigImportError as exc:
            return PushOutcome(ok=False, error=f"Config could not be rendered: {exc}")

    # 3. Resolve the --app-dir: a whole-app payload materializes a scratch dir;
    #    otherwise push the configured on-disk agent dir. Track the scratch dir so
    #    the finally block always cleans it up.
    scratch: Optional[str] = None
    pushed_dag: Optional[str] = None
    try:
        if has_app_files and spec.configs:
            # Multi-DAG BUNDLE push: render the whole component workspace.
            # 1. Gate on cross-config validation (component ref/io/cycle/depth).
            #    Off the event loop for the same reason the single-flow path below is:
            #    it is CPU-bound work over every config in the workspace, and on the
            #    loop it freezes the whole server (heartbeat, healthz, every product).
            report = await asyncio.to_thread(render.cross_validate_bundle, spec.configs)
            if not report.valid:
                errs = [d.message for d in report.diagnostics if d.severity == "error"]
                detail = "; ".join(errs[:4]) or "see diagnostics"
                return PushOutcome(
                    ok=False,
                    error=f"Component validation failed: {detail}",
                    error_kind="component_invalid",
                )
            # 2. Every referenced setter/task tool must live in the app (V1 scope).
            missing = render.missing_tools(
                spec.app_files, render.referenced_setter_task_tools(spec.configs)
            )
            if missing:
                return PushOutcome(
                    ok=False,
                    error=(
                        "These tools the flow needs aren't in this app: "
                        f"{', '.join(missing)}. Author them here (or add the "
                        "component from the gallery) before pushing."
                    ),
                    error_kind="tool_unresolved",
                )
            # 3. Render each config into its *_dag (create+register children). Off the
            #    loop as well: this is the SAME synchronous codegen the single-flow path
            #    threads, run once per config in the bundle.
            try:
                effective_files, pushed_dag = await asyncio.to_thread(
                    render.render_bundle_into_app_files,
                    spec.app_files, spec.configs,
                    spec.config_id or spec.configs[0].config_id,
                )
            except RenderFailedError as exc:
                return PushOutcome(
                    ok=False,
                    error=f"Your flow couldn't be rendered: {exc}",
                    error_kind="render_failed",
                )
            scratch = await asyncio.to_thread(workdir.materialize, effective_files)
            app_dir = scratch
        elif has_app_files:
            # Fold the author's canvas edits into the app's real *_dag before
            # pushing. A wrong/missing dag or a failed render is a HARD error here
            # (never a silent stale ship) so the push can't "not take" quietly.
            try:
                _t = time.monotonic()
                logger.info("[push] render: folding canvas into the *_dag …")
                # Off the event loop: the render is synchronous codegen; on the loop
                # it would freeze the whole server (heartbeat, healthz, every product).
                effective_files, pushed_dag = await asyncio.to_thread(
                    render.render_canvas_into_app_files,
                    spec.app_files, spec.config, spec.config_id,
                )
                logger.info("[push] render: done in %.0fms (dag=%s)",
                            (time.monotonic() - _t) * 1000.0, pushed_dag)
            except DagUnresolvedError as exc:
                opts = ", ".join(exc.candidates) or "the app's dag tool"
                return PushOutcome(
                    ok=False,
                    error=(
                        "Couldn't tell which flow to update — select the flow and "
                        f"retry (expected one of: {opts})."
                    ),
                    error_kind="dag_unresolved",
                )
            except RenderFailedError as exc:
                return PushOutcome(
                    ok=False,
                    error=f"Your flow couldn't be rendered: {exc}",
                    error_kind="render_failed",
                )
            _t = time.monotonic()
            logger.info("[push] materialize: writing %d files to scratch …", len(effective_files))
            scratch = await asyncio.to_thread(workdir.materialize, effective_files)
            logger.info("[push] materialize: done in %.0fms (dir=%s)",
                        (time.monotonic() - _t) * 1000.0, scratch)
            app_dir = scratch
        else:
            app_dir = env.agent_dir
            if not app_dir:
                # No whole-app payload AND no on-disk agent dir. In hosted mode
                # this means the working copy wasn't loaded — re-open the app so
                # its files seed the session. In local mode it needs --agent-dir.
                hosted = env.mode == "hosted"
                return PushOutcome(
                    ok=False,
                    error=(
                        "Nothing staged to push — re-open (re-select) the app so its "
                        "files load into the session, then push again."
                        if hosted
                        else "No agent directory configured to push from. "
                        "Launch Slot Studio with --agent-dir pointing at the app."
                    ),
                )

        # 4. Create-vs-update: an existing deployment (deployed_app_id or a
        #    resolvable `to`) updates via --to; otherwise a create REQUIRES an
        #    explicit display name. Neither → refuse (no silent create).
        #
        #    The strict gate applies to whole-app payloads. The legacy config-only
        #    path keeps its original behavior: push the on-disk agent dir with `to`
        #    passed through verbatim (which may be None).
        try:
            to_target, display_name = target.decide(
                to=spec.to,
                deployed_app_id=spec.deployed_app_id,
                display_name=spec.display_name,
                env=env,
                strict=has_app_files,
            )
        except target.FirstPushNeedsDisplayName as exc:
            return PushOutcome(ok=False, error=str(exc))

        push_argv = argv_mod.build_push_argv(
            app_dir,
            to=to_target,
            display_name=display_name,
            project_id=env.project,
            location=env.location,
            overwrite=spec.overwrite,
        )

        # 5. Invoke the push subprocess (the single mocked seam).
        _t = time.monotonic()
        logger.info("[push] subprocess: launching `cxas push` (timeout=%.0fs) argv=%s",
                    timeout_s, " ".join(map(str, push_argv[:6])))
        try:
            rc, stdout, stderr = await runner.run_push(
                push_argv, timeout_s=timeout_s
            )
            logger.info("[push] subprocess: returned rc=%s in %.0fms",
                        rc, (time.monotonic() - _t) * 1000.0)
        except asyncio.TimeoutError:
            logger.warning("[push] subprocess: TIMED OUT after %.0fs",
                           timeout_s)
            return PushOutcome(
                ok=False,
                error="cxas push timed out. Retry, or check the deployment target.",
                error_kind="timeout",
            )

        # 6. Map a nonzero rc to a typed error_kind via the shared classifier.
        if rc != 0:
            # cxas prints its real error to STDOUT; stderr often carries only noise (e.g. the pydub/ffmpeg
            # RuntimeWarning), which would otherwise MASK the actual failure in the UI. Filter the noise
            # and prefer substantive output; always log the full streams so a failure is diagnosable.
            # Re-filtered here (the default runner already did) because a product may inject
            # its OWN runner, whose stderr never passed through that filter. One marker list,
            # in `runner` — a second copy here drifted the moment a warning class was added
            # to one and not the other.
            _clean = runner.strip_benign_stderr(stderr or "", drop_blank=True)
            detail = ((stdout or "").strip() or _clean.strip())
            logger.warning("[push] cxas push failed rc=%s\n--- STDOUT ---\n%s\n--- STDERR ---\n%s",
                           rc, (stdout or "")[-3000:], (stderr or "")[-1500:])
            error_kind = classify(stdout, stderr, rc)
            if error_kind == "cxas_missing" or rc == 127:
                detail = (
                    "`cxas` is not available in this environment's venv. "
                    "Install cxas-scrapi or activate the project venv."
                )
            return PushOutcome(
                ok=False,
                output=stdout,
                error=detail or "cxas push failed.",
                error_kind=error_kind,
            )

        rendered_slots = (
            [s.name for s in spec.config.slots]
            if spec.config is not None and pushed_dag
            else None
        )
        return PushOutcome(
            ok=True,
            app_name=argv_mod.parse_app_name(stdout, stderr),
            output=stdout,
            deployed_app_id=argv_mod.parse_deployed_app_id(stdout, stderr),
            dag=pushed_dag,
            rendered_slots=rendered_slots,
        )
    finally:
        # Whole-app pushes always discard their scratch dir.
        workdir.cleanup(scratch)
