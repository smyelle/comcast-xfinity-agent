"""The single chokepoint for constructing CES clients in the live driver.

One place to do the two things every construction needs: install the runtime
patches (:mod:`flows.live.scrapi_patches`, safe to call repeatedly) now that
``cxas_scrapi`` is imported, and bind an explicit CES host when one is given.

A host embedding the driver can pass its own module with the same ``make_*``
signatures as ``client_factory`` (see :class:`flows.live.session.ChatSession`) —
that is how a service applies a per-request endpoint override, which a CLI does
not need.

Construction is lazy (imports live inside each factory) because ``cxas_scrapi``
pulls in the heavy ``google.cloud.ces_v1beta`` stack plus ADC, which must not
load when ``flows`` is merely imported for offline authoring.
"""

from __future__ import annotations

import functools
import os
from typing import Any

from . import scrapi_patches

__all__ = ["make_sessions", "make_traces", "default_endpoint"]

#: Read when no explicit ``api_endpoint`` is passed. Unset means "no override",
#: and the upstream client falls back to its own default host.
ENV_ENDPOINT = "CES_API_ENDPOINT"


def default_endpoint() -> str | None:
    """The endpoint override from the environment, or None for upstream's default."""
    return os.environ.get(ENV_ENDPOINT) or None


@functools.lru_cache(maxsize=None)
def _bind_endpoint(cls: type, endpoint: str | None) -> type:
    """Return ``cls`` pinned to ``endpoint``, or ``cls`` itself when unset.

    Upstream resolves the CES host inside the static ``_get_client_options``, and
    ``get_grpc_transport`` then reads ``client_options["api_endpoint"]``, so
    overriding that one staticmethod redirects the whole gRPC/REST transport
    without touching any other upstream internals.

    An empty result from upstream means the resource name was unparseable; pass
    that through rather than manufacturing options for it.

    Subclasses are cached so repeated calls against the same host reuse one class
    instead of minting a new one per call.
    """
    if not endpoint:
        return cls

    class _EndpointBound(cls):  # type: ignore[valid-type,misc]
        @staticmethod
        def _get_client_options(resource_name: str) -> dict[str, str]:
            opts = cls._get_client_options(resource_name)
            return {**opts, "api_endpoint": endpoint} if opts else opts

    # Keep tracebacks and repr readable; this is an implementation detail.
    _EndpointBound.__name__ = cls.__name__
    _EndpointBound.__qualname__ = cls.__qualname__
    _EndpointBound.__module__ = cls.__module__
    return _EndpointBound


def _client(cls: type, api_endpoint: str | None) -> type:
    scrapi_patches.apply()
    return _bind_endpoint(cls, api_endpoint or default_endpoint())


def _scrapi(module: str, attr: str):
    """Import one ``cxas_scrapi`` attribute, naming the extra when it is absent.

    This is where a missing runtime dependency actually surfaces: every import in
    this module is deferred to construction time, so the offline authoring path
    never pays for it and never sees this error.
    """
    import importlib

    try:
        return getattr(importlib.import_module(module), attr)
    except ImportError as e:
        raise ImportError(
            f"driving a live app needs cxas-scrapi ({e}). Install the extra with"
            ' `pip install "flows[deploy]"`.'
        ) from e


def make_sessions(app_name: str, *, api_endpoint: str | None = None, **kwargs: Any):
    """Construct a ``cxas_scrapi.core.sessions.Sessions`` client.

    Covers the unary ``run()`` path. ``BidiSessionHandler`` builds its websocket
    URI from the module default and is not reached by the endpoint override.
    """
    Sessions = _scrapi("cxas_scrapi.core.sessions", "Sessions")

    return _client(Sessions, api_endpoint)(app_name, **kwargs)


def make_traces(app_name: str, *, api_endpoint: str | None = None, **kwargs: Any):
    """Construct a ``cxas_scrapi.core.traces.Traces`` client."""
    Traces = _scrapi("cxas_scrapi.core.traces", "Traces")

    return _client(Traces, api_endpoint)(app_name, **kwargs)
