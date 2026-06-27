from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from vesper import cli
from vesper.errors import TextRequestExecutionError


def _patch_service(monkeypatch, service: Any) -> None:
    """Make ``cli.service_context`` yield *service* and call its ``close()``."""

    @contextmanager
    def _ctx():
        try:
            yield service
        finally:
            service.close()

    monkeypatch.setattr(cli, "service_context", _ctx)


def test_cli_play_calls_service_directly(monkeypatch, capsys) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.closed = False

        def play(self):
            return {"status": "ok", "result": {"path": "/play", "body": None}}

        def close(self) -> None:
            self.closed = True

    service = FakeService()
    _patch_service(monkeypatch, service)
    monkeypatch.setattr("sys.argv", ["vesper", "play"])

    cli.main()

    captured = capsys.readouterr()
    assert json.loads(captured.out)["path"] == "/play"
    assert service.closed is True


def test_cli_ask_calls_service_directly(monkeypatch, capsys) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.closed = False

        def handle_text_request(self, text: str):
            assert text == "play some kep1er"
            return {"status": "ok", "input": text, "summary": "done"}

        def close(self) -> None:
            self.closed = True

    service = FakeService()
    _patch_service(monkeypatch, service)
    monkeypatch.setattr("sys.argv", ["vesper", "ask", "play some kep1er"])

    cli.main()

    captured = capsys.readouterr()
    assert json.loads(captured.out)["input"] == "play some kep1er"
    assert service.closed is True


def test_cli_session_queue_calls_service(monkeypatch, capsys) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.closed = False

        def session_queue(self, *, limit: int, include_history: bool):
            return {
                "status": "ok",
                "limit": limit,
                "include_history": include_history,
                "items": [{"title": "Queued"}],
            }

        def close(self) -> None:
            self.closed = True

    service = FakeService()
    _patch_service(monkeypatch, service)
    monkeypatch.setattr("sys.argv", ["vesper", "--json", "session", "queue", "--limit", "7", "--all"])

    cli.main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["limit"] == 7
    assert payload["include_history"] is True
    assert payload["items"][0]["title"] == "Queued"
    assert service.closed is True


def test_cli_session_candidates_calls_service(monkeypatch, capsys) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.closed = False

        def session_candidates(self, *, window: int):
            return {
                "status": "ok",
                "window": window,
                "pools": [{"source": {"kind": "artist", "term": "Pink"}, "cursor": 0}],
            }

        def close(self) -> None:
            self.closed = True

    service = FakeService()
    _patch_service(monkeypatch, service)
    monkeypatch.setattr("sys.argv", ["vesper", "--json", "session", "candidates", "--window", "5"])

    cli.main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["window"] == 5
    assert payload["pools"][0]["source"]["term"] == "Pink"
    assert service.closed is True


def test_cli_prints_text_request_errors(monkeypatch, capsys) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.closed = False

        def handle_text_request(self, text: str):
            raise TextRequestExecutionError("No active session.", {"status": "error", "message": "No active session."})

        def close(self) -> None:
            self.closed = True

    service = FakeService()
    _patch_service(monkeypatch, service)
    monkeypatch.setattr("sys.argv", ["vesper", "ask", "stop session"])

    with pytest.raises(SystemExit, match="1"):
        cli.main()

    captured = capsys.readouterr()
    assert json.loads(captured.err)["message"] == "No active session."
    # close() must still run even when the command raises.
    assert service.closed is True


def test_serve_requires_transport_flag(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["vesper", "serve"])

    with pytest.raises(SystemExit, match="2"):
        cli.main()


def test_serve_accepts_a2a_transport(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr("sys.argv", ["vesper", "serve", "--a2a"])
    monkeypatch.setattr(cli, "get_settings", lambda: type("Settings", (), {"http_host": "127.0.0.1", "http_port": 8766})())

    def fake_create_http_app(*, include_a2a: bool, include_mcp: bool):
        captured["flags"] = (include_a2a, include_mcp)
        return object()

    def fake_run(app, *, host, port, reload):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["reload"] = reload

    monkeypatch.setattr("vesper.a2a.create_http_app", fake_create_http_app)
    monkeypatch.setattr("uvicorn.run", fake_run)

    cli.main()

    assert captured["flags"] == (True, False)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8766


def test_serve_accepts_both_transports(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr("sys.argv", ["vesper", "serve", "--a2a", "--mcp"])
    monkeypatch.setattr(cli, "get_settings", lambda: type("Settings", (), {"http_host": "127.0.0.1", "http_port": 8766})())

    def fake_create_http_app(*, include_a2a: bool, include_mcp: bool):
        captured["flags"] = (include_a2a, include_mcp)
        return object()

    def fake_run(app, *, host, port, reload):
        captured["app"] = app

    monkeypatch.setattr("vesper.a2a.create_http_app", fake_create_http_app)
    monkeypatch.setattr("uvicorn.run", fake_run)

    cli.main()

    assert captured["flags"] == (True, True)
