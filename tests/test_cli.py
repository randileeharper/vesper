from __future__ import annotations

import json

import pytest

from vesper import cli
from vesper.errors import TextRequestExecutionError


def test_cli_play_calls_service_directly(monkeypatch, capsys) -> None:
    class FakeService:
        def play(self):
            return {"status": "ok", "result": {"path": "/play", "body": None}}

    monkeypatch.setattr(cli, "get_service", lambda: FakeService())
    monkeypatch.setattr("sys.argv", ["vesper", "play"])

    cli.main()

    captured = capsys.readouterr()
    assert json.loads(captured.out)["path"] == "/play"


def test_cli_ask_calls_service_directly(monkeypatch, capsys) -> None:
    class FakeService:
        def handle_text_request(self, text: str):
            assert text == "play some kep1er"
            return {"status": "ok", "input": text, "summary": "done"}

    monkeypatch.setattr(cli, "get_service", lambda: FakeService())
    monkeypatch.setattr("sys.argv", ["vesper", "ask", "play some kep1er"])

    cli.main()

    captured = capsys.readouterr()
    assert json.loads(captured.out)["input"] == "play some kep1er"


def test_cli_prints_text_request_errors(monkeypatch, capsys) -> None:
    class FakeService:
        def handle_text_request(self, text: str):
            raise TextRequestExecutionError("No active session.", {"status": "error", "message": "No active session."})

    monkeypatch.setattr(cli, "get_service", lambda: FakeService())
    monkeypatch.setattr("sys.argv", ["vesper", "ask", "stop session"])

    with pytest.raises(SystemExit, match="1"):
        cli.main()

    captured = capsys.readouterr()
    assert json.loads(captured.err)["message"] == "No active session."


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
