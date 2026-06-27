from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator
import pytest

from tests.conftest import StubResolver, StubRpcClient
from vesper import a2a, cli
from vesper.config import Settings
from vesper.historian import (
    FakeHistorianSink,
    HistorianDeliveryError,
    HttpHistorianSink,
    NullHistorianSink,
    build_event,
)
from vesper.service import CiderAgentService
from vesper.storage import PreferenceStore


def _enabled_settings(settings: Settings, **overrides: Any) -> Settings:
    values = {
        key: getattr(settings, key)
        for key in settings.__dataclass_fields__
        if key != "config_path"
    }
    values.update(
        {
            "historian_enabled": True,
            "historian_token": "hist_test_token",
            "historian_base_url": "https://historian.test",
            "historian_timeout_seconds": 3.0,
            "historian_verify_tls": False,
            "historian_retry_count": 2,
            "config_path": None,
        }
    )
    values.update(overrides)
    return Settings(**values)


def _service(settings: Settings, sink: Any, tmp_path: Path) -> CiderAgentService:
    configured = _enabled_settings(settings, database_path=tmp_path / "historian-events.db")
    return CiderAgentService(
        configured,
        rpc_client=StubRpcClient(),
        preference_store=PreferenceStore(configured.database_path),
        resolver=StubResolver(),
        historian_sink=sink,
    )


def test_null_sink_never_performs_network_io() -> None:
    sink = NullHistorianSink()
    sink.emit({"id": "one"})
    sink.emit_batch([{"id": "two"}])
    sink.close()


def test_http_sink_uses_bearer_url_and_payload(settings: Settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.Client(
        base_url="https://historian.test",
        transport=httpx.MockTransport(handler),
        timeout=3.0,
        verify=False,
    )
    sink = HttpHistorianSink(_enabled_settings(settings), client=client)
    event = build_event("music.playback.paused", {"caller": "direct"}, source="app://vesper/playback")

    sink.emit(event)
    sink.emit_batch([event])

    assert [request.url.path for request in requests] == ["/v1/events", "/v1/events:batch"]
    assert all(request.headers["Authorization"] == "Bearer hist_test_token" for request in requests)
    assert json.loads(requests[0].content) == event
    assert json.loads(requests[1].content) == {"events": [event]}


def test_http_sink_retries_connection_and_5xx_with_stable_event_id(settings: Settings) -> None:
    attempts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(json.loads(request.content))
        if len(attempts) == 1:
            raise httpx.ConnectError("offline", request=request)
        if len(attempts) == 2:
            return httpx.Response(503, json={"status": "error"})
        return httpx.Response(200, json={"status": "ok"})

    sink = HttpHistorianSink(
        _enabled_settings(settings),
        client=httpx.Client(base_url="https://historian.test", transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    event = build_event("music.playback.paused", {"caller": "direct"}, source="app://vesper/playback")

    sink.emit(event)

    assert len(attempts) == 3
    assert {attempt["id"] for attempt in attempts} == {event["id"]}


@pytest.mark.parametrize("status_code", [400, 401, 403, 409, 422])
def test_http_sink_does_not_retry_client_failures(settings: Settings, status_code: int) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, request=request, json={"status": "error"})

    sink = HttpHistorianSink(
        _enabled_settings(settings),
        client=httpx.Client(base_url="https://historian.test", transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )

    with pytest.raises(httpx.HTTPStatusError):
        sink.emit(build_event("music.playback.paused", {"caller": "direct"}, source="app://vesper/playback"))

    assert attempts == 1


def test_service_events_share_correlation_and_request_causation(settings: Settings, tmp_path: Path) -> None:
    sink = FakeHistorianSink()
    service = _service(settings, sink, tmp_path)

    with service.operation(caller="a2a", correlation_id="task-123"):
        service.handle_text_request("play upbeat music")

    request_event = next(event for event in sink.events if event["type"] == "music.request.received")
    resulting = [event for event in sink.events if event["type"] != "music.request.received"]
    assert resulting
    assert all(event["correlationid"] == "task-123" for event in sink.events)
    assert all(event["causationid"] == request_event["id"] for event in resulting)
    assert request_event["data"]["caller"] == "a2a"


def test_service_captures_full_domain_event_set_and_manifest_matches(settings: Settings, tmp_path: Path) -> None:
    sink = FakeHistorianSink()
    service = _service(settings, sink, tmp_path)

    service.start_background_session_worker()
    service.stop_background_session_worker()
    service.play_session("play upbeat music")
    service.steer_session("more pop")
    liked = service.like_current_track()
    service.forget_preference(liked["liked_track"]["id"])
    service.reject_current_track()
    service.pause()
    service.play()
    service.next_track()
    service.previous_track()
    service.stop()

    event_types = {event["type"] for event in sink.events}
    assert {
        "music.playback.started",
        "music.playback.paused",
        "music.playback.stopped",
        "music.track.skipped",
        "music.session.started",
        "music.session.steered",
        "music.session.track_selected",
        "music.session.ended",
        "music.preference.recorded",
        "music.preference.forgotten",
        "music.worker.started",
        "music.worker.stopped",
    } <= event_types

    manifest = json.loads((Path(__file__).parents[1] / "historian.manifest.json").read_text(encoding="utf-8"))
    schemas = {(item["event_type"], item["version"]): item["json_schema"] for item in manifest["schemas"]}
    for event in sink.events:
        if event["type"].startswith("core."):
            continue
        Draft202012Validator(schemas[(event["type"], event["schemaversion"])]).validate(event["data"])
        encoded = json.dumps(event)
        assert "secret-token" not in encoded
        assert "hist_test_token" not in encoded

    started = next(event for event in sink.events if event["type"] == "music.session.started")
    selected = next(
        event
        for event in sink.events
        if event["type"] == "music.session.track_selected"
        and event.get("sessionid") == started.get("sessionid")
    )
    assert started["correlationid"] == selected["correlationid"]


def test_flattened_track_playback_event_includes_known_metadata(settings: Settings, tmp_path: Path) -> None:
    sink = FakeHistorianSink()
    service = _service(settings, sink, tmp_path)

    service.play_search_result(query="k-pop", source="catalog")

    event = next(event for event in sink.events if event["type"] == "music.playback.started")
    assert event["data"]["track"]["title"] == "k-pop"
    assert event["data"]["track"]["artist"] == "Catalog Artist"


def test_direct_id_only_play_item_remains_compatible(settings: Settings, tmp_path: Path) -> None:
    sink = FakeHistorianSink()
    service = _service(settings, sink, tmp_path)

    service.play_item("catalog-track-1")

    event = next(event for event in sink.events if event["type"] == "music.playback.started")
    assert event["data"]["track"] == {
        "id": "catalog-track-1",
        "title": None,
        "artist": None,
        "album": None,
        "kind": "songs",
        "is_library": False,
    }


def test_adaptive_session_playback_event_includes_selected_track_metadata(
    settings: Settings,
    tmp_path: Path,
) -> None:
    sink = FakeHistorianSink()
    service = _service(settings, sink, tmp_path)

    service.play_session("play upbeat music")

    event = next(event for event in sink.events if event["type"] == "music.playback.started")
    assert event["data"]["track"]["title"] == "Liked Song"
    assert event["data"]["track"]["artist"] == "Favorite Artist"


def test_cli_and_a2a_emit_once_at_service_boundary(
    settings: Settings,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    cli_sink = FakeHistorianSink()
    cli_service = _service(settings, cli_sink, tmp_path / "cli")

    @contextmanager
    def _cli_service_context():
        yield cli_service

    monkeypatch.setattr(cli, "service_context", _cli_service_context)
    monkeypatch.setattr("sys.argv", ["vesper", "pause"])
    cli.main()
    capsys.readouterr()

    a2a_sink = FakeHistorianSink()
    a2a_service = _service(settings, a2a_sink, tmp_path / "a2a")
    monkeypatch.setattr(a2a, "get_service", lambda: a2a_service)
    inspection = a2a.RequestInspection(
        kind="action",
        action="pause",
        parameters={},
        public_action=True,
    )
    a2a._execute_inspection(inspection, correlation_id="a2a-task")

    assert [event["type"] for event in cli_sink.events].count("music.playback.paused") == 1
    assert [event["type"] for event in a2a_sink.events].count("music.playback.paused") == 1
    assert cli_sink.events[0]["data"]["caller"] == "cli"
    assert a2a_sink.events[0]["data"]["caller"] == "a2a"
    assert a2a_sink.events[0]["correlationid"] == "a2a-task"


def test_historian_unavailability_does_not_change_success(settings: Settings, tmp_path: Path, caplog) -> None:
    class FailingSink:
        def emit(self, event):
            raise HistorianDeliveryError("offline")

        def emit_batch(self, events):
            raise HistorianDeliveryError("offline")

        def close(self):
            return None

    service = _service(settings, FailingSink(), tmp_path)

    result = service.pause()

    assert result["status"] == "ok"
    assert "Historian delivery failed" in caplog.text


def test_rpc_failures_emit_sanitized_event(settings: Settings, tmp_path: Path) -> None:
    sink = FakeHistorianSink()
    configured = _enabled_settings(settings, database_path=tmp_path / "rpc-failure.db")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request, json={"detail": "Cider unavailable"})

    from vesper.rpc import CiderRpcClient

    rpc = CiderRpcClient(
        configured,
        session=httpx.Client(base_url=configured.cider_base_url, transport=httpx.MockTransport(handler)),
    )
    service = CiderAgentService(
        configured,
        rpc_client=rpc,
        preference_store=PreferenceStore(configured.database_path),
        resolver=StubResolver(),
        historian_sink=sink,
    )

    with pytest.raises(Exception):
        service.pause()

    event = next(event for event in sink.events if event["type"] == "music.rpc.failed")
    assert event["data"]["operation"] == "POST /api/v1/playback/pause"
    assert event["data"]["status_code"] == 500
    assert "secret-token" not in json.dumps(event)
