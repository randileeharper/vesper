"""Application factory utilities."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
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


class Application:
    """Owns the long-lived service and the adaptive-session worker lifecycle.

    The worker start/stop logic lives here in exactly one place (see #45).
    Long-lived transports (HTTP app, standalone MCP stdio) compose
    :meth:`worker_lifespan` into their own lifespan so they behave
    consistently. One-shot CLI commands never start the worker;
    :meth:`CiderAgentService.close` drains it as a teardown safety net.
    """

    def __init__(self, service: CiderAgentService) -> None:
        self._service = service

    @property
    def service(self) -> CiderAgentService:
        return self._service

    @asynccontextmanager
    async def worker_lifespan(self) -> AsyncIterator[None]:
        """Start the adaptive-session worker on entry, stop it on exit.

        This is the single owner of worker start/stop. Transports compose it
        into their lifespans instead of calling the service start/stop methods
        directly, so the worker is never started twice or stopped too early.
        """
        self._service.start_background_session_worker()
        try:
            yield
        finally:
            self._service.stop_background_session_worker()


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
