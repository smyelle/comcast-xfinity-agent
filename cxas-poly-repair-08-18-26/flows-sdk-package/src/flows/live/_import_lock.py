"""Shared lock that serializes the FIRST import of cxas_scrapi submodules.

A host may warm cxas on several daemon threads at once. Importing
`cxas_scrapi.core.sessions`, `core.traces`, `core.apps` and friends concurrently
can catch a module partially initialized ("cannot import name 'Sessions' …
circular import"), because those submodules import each other.

Routing every cxas import through this RLock makes the first import of each
module run to completion before another thread enters, so nothing ever sees a
half-initialized module. RLock (not Lock) so a wrapped import that transitively
triggers another wrapped import on the same thread can't self-deadlock.
"""

from __future__ import annotations

import threading

LOCK = threading.RLock()
