"""MCP transport for Vesper."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

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


def create_mcp_server(*, streamable_http_path: str = "/mcp") -> FastMCP:
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
        log_level=settings.log_level,
        lifespan=_mcp_lifespan,
    )

    @server.tool(name="play", description="Resume playback.", structured_output=True)
    def play() -> dict[str, Any]:
        return get_service().play()

    @server.tool(name="pause", description="Pause playback.", structured_output=True)
    def pause() -> dict[str, Any]:
        return get_service().pause()

    @server.tool(name="next", description="Skip to the next track or session-selected track.", structured_output=True)
    def next_track() -> dict[str, Any]:
        return get_service().next_track()

    @server.tool(name="previous", description="Go to the previous track.", structured_output=True)
    def previous_track() -> dict[str, Any]:
        return get_service().previous_track()

    @server.tool(name="ask", description="Handle a natural-language music request.", structured_output=True)
    def ask(text: str) -> dict[str, Any]:
        if not text.strip():
            raise CiderValidationError("text cannot be empty.")
        return get_service().handle_text_request(text)

    return server
