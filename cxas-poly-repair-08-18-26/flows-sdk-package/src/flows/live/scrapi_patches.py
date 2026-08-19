"""Behaviour the live driver needs from ``cxas-scrapi`` that upstream does not provide.

Patching the imported classes in place (rather than subclassing at the client
factory) is unavoidable: SCRAPI constructs the affected objects deep inside its
own call paths (``core/sessions.py``, ``evals/simulation_evals.py``), so a
subclass handed out at construction time would never be the class it actually
instantiates.

:func:`apply` is idempotent and cheap after the first call. Call it at the lazy
``cxas_scrapi`` import boundary, never at module import, so that importing
``flows`` does not pull in the heavy ``google.cloud.ces_v1beta`` stack.

Idempotence across COPIES of this module matters too: a host may carry its own
copy applied to the same class in the same process, and wrapping ``__init__``
twice would double-count text. The patch therefore stamps a marker attribute on
the function it installs and skips if it is already present. Any other copy of
this patch must set and honour the same marker.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

#: Stamped on the installed ``__init__`` so a second copy of this module (e.g. a
#: host's own vendored copy) detects the patch and does not wrap it again.
_MARKER = "_per_output_agent_text_patch"

_applied = False
_apply_lock = threading.Lock()


def apply() -> None:
    """Install the patches onto the imported SCRAPI classes, once.

    Double-checked locking: callers may invoke this on every client construction
    from several worker threads at once. Without the lock two threads can both
    read ``_applied`` as False and wrap ``__init__`` twice. The flag is set only
    after patching, so a thread taking the fast path always sees a fully
    installed patch.
    """
    global _applied
    if _applied:
        return
    with _apply_lock:
        if _applied:
            return
        try:
            _patch_per_output_agent_text()
        except Exception:
            # Never let a refinement take down client construction: callers reach
            # this on the way to building a CES client, and a client that cannot
            # be built is far worse than one whose agent text is scoped the way
            # upstream scopes it. Marked applied either way, because the failures
            # here are structural (module missing, upstream reshaped) and retrying
            # on every construction would only spam this log.
            logger.warning(
                "SCRAPI patches could not be installed; agent text in multi-output "
                "responses will follow upstream's response-wide scoping.",
                exc_info=True,
            )
        _applied = True


def _patch_per_output_agent_text() -> None:
    """Scope the "reply already seen at top level" guard per output, except for
    text the response has already spoken.

    A reply can arrive both as ``output.text`` and again in that output's
    ``diagnostic_info`` trace. Upstream suppresses the duplicate with a flag
    (``_parse``'s ``top_level_agent_text_found``) that is scoped to the WHOLE
    response and never reset, so once any output carries top-level text, every
    later output that carries text ONLY in diagnostic_info is dropped outright:

        outputs=[ SessionOutput(text="First part."),
                  SessionOutput(diagnostic_info=<agent chunk "Second part."/>) ]
        upstream -> "First part."            (the second output is lost)
        wanted   -> "First part. Second part."

    Rather than re-implement the chunk walking (upstream handles protobuf, dict
    and attribute-style shapes, and that is precisely the code most likely to
    drift under us), we re-run upstream's own parser on one output at a time.
    Each single-output pass gets a fresh flag, which IS the per-output scoping we
    want, and the text extraction stays 100% upstream's.

    ``detailed_trace`` is rebuilt the same way, because the flag suppresses the
    trace line alongside the text and simulation evals match expectations against
    the trace. Concatenating the per-output traces is byte-identical to upstream's
    single pass for everything the flag does not touch (tool calls, payloads,
    citations), since all of it is derived per output.

    A fresh flag per output is too generous at the END of a turn, though,
    because the diagnostic mirror is scoped to the TURN and not to the output
    that carries it. A turn that ends the session arrives as

        outputs=[ SessionOutput(text="Handing you over."),
                  SessionOutput(end_session=..., diagnostic_info=<same text>) ]

    and the second output's fresh pass has no top-level text of its own to
    suppress against, so the reply is counted twice. Streaming is the same shape
    with a diagnostics-only turn-end output in place of ``end_session``. So
    diagnostic-only text is dropped when the response already spoke it at top
    level. Top-level text is never dropped, which is what keeps a line the agent
    genuinely says twice appearing twice.

    The comparison is a whitespace-normalised SUBSTRING rather than equality: a
    multi-part turn arrives at top level already joined into one ``output.text``
    while the mirror carries the parts separately, so equality would miss every
    multi-part turn. ``detailed_trace`` is deliberately left alone — both copies
    belong in the human-readable trace.
    """
    from cxas_scrapi.core.response_parser import ParsedSessionResponse

    original_init = ParsedSessionResponse.__init__
    if getattr(original_init, _MARKER, False):
        return  # another copy of this patch already wrapped it

    def _norm(text: str) -> str:
        """Whitespace-insensitive comparison key."""
        return " ".join((text or "").split())

    def _top_text(output) -> str:
        top = getattr(output, "text", None)
        return top if isinstance(top, str) else ""

    def __init__(self, response, tools_map=None):  # noqa: N807
        original_init(self, response, tools_map)
        # One output means upstream's response-wide flag is already per-output.
        if len(self.outputs) < 2:
            return
        spoken_at_top = _norm(
            " ".join(_top_text(o) for o in self.outputs if o is not None)
        )
        texts: list[str] = []
        trace: list[str] = []
        for output in self.outputs:
            if output is None:
                continue
            scratch = ParsedSessionResponse.__new__(ParsedSessionResponse)
            original_init(scratch, [output], tools_map)  # not the patched one
            if _top_text(output):
                texts.extend(scratch.agent_texts)
            else:
                texts.extend(
                    t
                    for t in scratch.agent_texts
                    if not (
                        spoken_at_top and _norm(t) and _norm(t) in spoken_at_top
                    )
                )
            trace.extend(scratch.detailed_trace)
        self.agent_texts = texts
        self.detailed_trace = trace
        # Mirrors how upstream derives it at the end of _parse.
        self.consolidated_agent_text = " ".join(texts).strip()

    setattr(__init__, _MARKER, True)
    ParsedSessionResponse.__init__ = __init__
