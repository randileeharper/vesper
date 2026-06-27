from __future__ import annotations

from pathlib import Path

import anyio
import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.memory import create_connected_server_and_client_session

from vesper import a2a, mcp_server
from vesper.a2a import create_http_app
from vesper.config import Settings
from vesper.service import CiderAgentService
from vesper.storage import PreferenceStore
from tests.conftest import StubResolver, StubRpcClient


def _make_service(tmp_path: Path) -> tuple[Settings, CiderAgentService]:
    settings = Settings(
        http_host="127.0.0.1",
        http_port=8766,
        public_base_url="http://127.0.0.1:8766",
        cider_base_url="http://localhost:10767",
        cider_api_token="secret-token",
        default_search_source="catalog",
        resolver_backend="fallback",
        resolver_base_url="https://api.openai.com/v1",
        resolver_model=None,
        resolver_api_key=None,
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
        request_timeout_seconds=10.0,
        verify_tls=True,
        log_level="INFO",
        database_path=tmp_path / "mcp-test.db",
        config_path=None,
    )
    service = CiderAgentService(
        settings,
        rpc_client=StubRpcClient(),
        preference_store=PreferenceStore(settings.database_path),
        resolver=StubResolver(),
    )
    return settings, service


def test_mcp_lists_only_transport_and_ask_tools(monkeypatch, tmp_path: Path) -> None:
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    async def _exercise() -> None:
        async with create_connected_server_and_client_session(mcp_server.create_mcp_server()) as session:
            tools = await session.list_tools()
            resources = await session.list_resources()
            assert [tool.name for tool in tools.tools] == ["play", "pause", "next", "previous", "ask"]
            assert resources.resources == []

    anyio.run(_exercise)


def test_mcp_transport_tools_delegate_to_service(monkeypatch, tmp_path: Path) -> None:
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    async def _exercise() -> None:
        async with create_connected_server_and_client_session(mcp_server.create_mcp_server()) as session:
            play = await session.call_tool("play", {})
            pause = await session.call_tool("pause", {})
            next_track = await session.call_tool("next", {})
            previous = await session.call_tool("previous", {})

            assert play.structuredContent == {"status": "ok", "result": {"path": "/play", "body": None}}
            assert pause.structuredContent == {"status": "ok", "result": {"path": "/pause", "body": None}}
            assert next_track.structuredContent["result"]["path"] == "/next"
            assert previous.structuredContent["result"]["path"] == "/previous"

    anyio.run(_exercise)


def test_mcp_ask_returns_text_request_payload(monkeypatch, tmp_path: Path) -> None:
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    async def _exercise() -> None:
        async with create_connected_server_and_client_session(mcp_server.create_mcp_server()) as session:
            result = await session.call_tool("ask", {"text": "play some kep1er"})
            assert result.isError is False
            assert result.structuredContent["status"] == "ok"
            assert result.structuredContent["input"] == "play some kep1er"
            assert result.structuredContent["execution"]["action"] == "search"

    anyio.run(_exercise)


def test_mcp_ask_rejects_empty_text(monkeypatch, tmp_path: Path) -> None:
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    async def _exercise() -> None:
        async with create_connected_server_and_client_session(mcp_server.create_mcp_server()) as session:
            result = await session.call_tool("ask", {"text": ""})
            assert result.isError is True
            assert "text cannot be empty" in result.content[0].text

    anyio.run(_exercise)


def test_http_app_enables_requested_transports(monkeypatch, tmp_path: Path) -> None:
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(a2a, "get_settings", lambda: settings)
    monkeypatch.setattr(a2a, "get_service", lambda: service)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    async def _exercise() -> None:
        a2a_only = create_http_app(include_a2a=True)
        mcp_only = create_http_app(include_mcp=True)
        both = create_http_app(include_a2a=True, include_mcp=True)

        async with a2a_only.router.lifespan_context(a2a_only):
            transport_without = httpx.ASGITransport(app=a2a_only)
            async with httpx.AsyncClient(
                transport=transport_without,
                base_url="http://127.0.0.1:8766",
                follow_redirects=True,
            ) as client:
                missing = await client.post("/mcp", json={})
                agent_card = await client.get("/.well-known/agent-card")
                assert missing.status_code == 404
                assert agent_card.status_code == 200

        async with mcp_only.router.lifespan_context(mcp_only):
            transport_with = httpx.ASGITransport(app=mcp_only)
            async with httpx.AsyncClient(transport=transport_with, base_url="http://127.0.0.1:8766") as client:
                health = await client.get("/healthz")
                mcp_response = await client.post("/mcp", json={})
                agent_card = await client.get("/.well-known/agent-card")
                assert health.status_code == 200
                assert mcp_response.status_code in {200, 400, 406}
                assert agent_card.status_code == 404

        async with both.router.lifespan_context(both):
            transport_both = httpx.ASGITransport(app=both)
            async with httpx.AsyncClient(transport=transport_both, base_url="http://127.0.0.1:8766") as client:
                agent_card = await client.get("/.well-known/agent-card")
                mcp_response = await client.post("/mcp", json={})
                assert agent_card.status_code == 200
                assert mcp_response.status_code in {200, 400, 406}

    anyio.run(_exercise)


def test_streamable_http_client_can_call_mounted_mcp(monkeypatch, tmp_path: Path) -> None:
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(a2a, "get_settings", lambda: settings)
    monkeypatch.setattr(a2a, "get_service", lambda: service)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    app = create_http_app(include_mcp=True)

    def client_factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8766",
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=True,
        )

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            async with streamablehttp_client("http://127.0.0.1:8766/mcp", httpx_client_factory=client_factory) as streams:
                read_stream, write_stream, _ = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("play", {})
                    assert [tool.name for tool in tools.tools] == ["play", "pause", "next", "previous", "ask"]
                    assert result.structuredContent["result"]["path"] == "/play"

    anyio.run(_exercise)


def test_mounted_mcp_requests_do_not_stop_parent_session_worker(monkeypatch, tmp_path: Path) -> None:
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(a2a, "get_settings", lambda: settings)
    monkeypatch.setattr(a2a, "get_service", lambda: service)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    app = create_http_app(include_mcp=True)

    def client_factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8766",
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=True,
        )

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            worker = service._session_worker_thread
            assert worker is not None
            assert worker.is_alive()

            async with streamablehttp_client("http://127.0.0.1:8766/mcp", httpx_client_factory=client_factory) as streams:
                read_stream, write_stream, _ = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    await session.list_tools()

            assert service._session_worker_thread is worker
            assert worker.is_alive()

    anyio.run(_exercise)


@pytest.mark.parametrize("include_a2a, include_mcp", [(True, False), (False, True), (True, True)])
def test_worker_does_not_run_after_http_lifespan_shutdown(monkeypatch, tmp_path: Path, include_a2a: bool, include_mcp: bool) -> None:
    """The worker must be stopped once the HTTP app lifespan exits (#45).

    All three HTTP transport combinations delegate worker start/stop to the
    single Application.worker_lifespan, so the worker must not be alive after
    the lifespan context closes.
    """
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(a2a, "get_settings", lambda: settings)
    monkeypatch.setattr(a2a, "get_service", lambda: service)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    app = create_http_app(include_a2a=include_a2a, include_mcp=include_mcp)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            assert service._session_worker_thread is not None
            assert service._session_worker_thread.is_alive()
        # After the lifespan exits, the worker must have been stopped.
        assert service._session_worker_thread is None

    anyio.run(_exercise)


def test_worker_does_not_run_after_standalone_mcp_lifespan_shutdown(monkeypatch, tmp_path: Path) -> None:
    """Standalone MCP (e.g. stdio) must stop the worker on lifespan exit (#45)."""
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    server = mcp_server.create_mcp_server()

    async def _exercise() -> None:
        async with create_connected_server_and_client_session(server) as session:
            await session.list_tools()
            assert service._session_worker_thread is not None
            assert service._session_worker_thread.is_alive()
        # After the standalone MCP session closes, the worker must have stopped.
        assert service._session_worker_thread is None

    anyio.run(_exercise)
