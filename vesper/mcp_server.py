"""MCP transport for Vesper."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from .app import get_service, get_settings
from .errors import CiderValidationError


@asynccontextmanager
async def _mcp_lifespan(_: FastMCP):
    service = get_service()
    service.start_background_session_worker()
    try:
        yield
    finally:
        service.stop_background_session_worker()


@asynccontextmanager
async def _embedded_mcp_lifespan(_: FastMCP):
    # The parent HTTP app owns the shared session worker. FastMCP invokes its
    # lifespan for stateless request sessions, so stopping the worker here
    # would disable adaptive-session auto-advance after every MCP request.
    yield


def create_mcp_server(*, streamable_http_path: str = "/mcp", manage_session_worker: bool = True) -> FastMCP:
    settings = get_settings()
    server = FastMCP(
        "vesper",
        instructions=(
            "A compact music-control MCP server for the Cider Apple Music client. "
            "Use ask for rich natural-language requests; use the transport tools for direct playback control."
        ),
        host=settings.http_host,
        port=settings.http_port,
        streamable_http_path=streamable_http_path,
        json_response=True,
        stateless_http=True,
        log_level=cast(Any, settings.log_level),
        lifespan=_mcp_lifespan if manage_session_worker else _embedded_mcp_lifespan,
    )

    @server.tool(name="play", description="Resume playback.", structured_output=True)
    def play() -> dict[str, Any]:
        service = get_service()
        with service.operation(caller="mcp"):
            return service.play()

    @server.tool(name="pause", description="Pause playback.", structured_output=True)
    def pause() -> dict[str, Any]:
        service = get_service()
        with service.operation(caller="mcp"):
            return service.pause()

    @server.tool(name="next", description="Skip to the next track or session-selected track.", structured_output=True)
    def next_track() -> dict[str, Any]:
        service = get_service()
        with service.operation(caller="mcp"):
            return service.next_track()

    @server.tool(name="previous", description="Go to the previous track.", structured_output=True)
    def previous_track() -> dict[str, Any]:
        service = get_service()
        with service.operation(caller="mcp"):
            return service.previous_track()

    @server.tool(name="ask", description="Handle a natural-language music request.", structured_output=True)
    def ask(text: str) -> dict[str, Any]:
        if not text.strip():
            raise CiderValidationError("text cannot be empty.")
        service = get_service()
        with service.operation(caller="mcp"):
            return service.handle_text_request(text)

    return server
