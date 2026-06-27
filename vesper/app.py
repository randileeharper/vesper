"""Application factory utilities."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from .config import Settings
from .historian import build_historian_sink
from .service import CiderAgentService


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings.from_env()
    settings.ensure_storage_parent()
    return settings


@lru_cache(maxsize=1)
def get_service() -> CiderAgentService:
    settings = get_settings()
    return CiderAgentService(settings, historian_sink=build_historian_sink(settings))


@contextmanager
def service_context() -> Iterator[CiderAgentService]:
    """Yield the cached service, then tear it down on exit.

    The server transports share one long-lived service across requests, so they
    should keep using :func:`get_service` directly. The CLI runs one-shot
    commands and must release the RPC client, SQLite connections, and resolver
    when the command finishes -- otherwise they leak until process exit (issue
    #46). The ``lru_cache`` is cleared after teardown so a re-entrant call (e.g.
    a second CLI command in the same process) gets a fresh instance.
    """
    service = get_service()
    try:
        yield service
    finally:
        try:
            service.close()
        finally:
            get_service.cache_clear()
