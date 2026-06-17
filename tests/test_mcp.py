from __future__ import annotations

from pathlib import Path

import anyio
import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from cider_agent import a2a, mcp_server
from cider_agent.a2a import create_a2a_app
from cider_agent.config import Settings
from cider_agent.service import CiderAgentService
from cider_agent.storage import PreferenceStore
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


def test_a2a_app_optionally_exposes_mcp(monkeypatch, tmp_path: Path) -> None:
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(a2a, "get_settings", lambda: settings)
    monkeypatch.setattr(a2a, "get_service", lambda: service)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    async def _exercise() -> None:
        without_mcp = create_a2a_app(include_mcp=False)
        with_mcp = create_a2a_app(include_mcp=True)

        async with without_mcp.router.lifespan_context(without_mcp):
            transport_without = httpx.ASGITransport(app=without_mcp)
            async with httpx.AsyncClient(
                transport=transport_without,
                base_url="http://127.0.0.1:8766",
                follow_redirects=True,
            ) as client:
                missing = await client.post("/mcp", json={})
                assert missing.status_code == 404

        async with with_mcp.router.lifespan_context(with_mcp):
            transport_with = httpx.ASGITransport(app=with_mcp)
            async with httpx.AsyncClient(transport=transport_with, base_url="http://127.0.0.1:8766") as client:
                health = await client.get("/healthz")
                mcp_response = await client.post("/mcp", json={})
                assert health.status_code == 200
                assert mcp_response.status_code in {200, 400, 406}

    anyio.run(_exercise)


def test_streamable_http_client_can_call_mounted_mcp(monkeypatch, tmp_path: Path) -> None:
    settings, service = _make_service(tmp_path)
    monkeypatch.setattr(a2a, "get_settings", lambda: settings)
    monkeypatch.setattr(a2a, "get_service", lambda: service)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    app = create_a2a_app(include_mcp=True)

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
