from __future__ import annotations

import httpx
import pytest

from tests.conftest import FakeResponse, FakeSession
from vesper.errors import CiderRpcError
from vesper.rpc import CiderRpcClient


def _flaky_session(fail: int, *, status: int = 503, exc: Exception | None = None) -> FakeSession:
    """Return a FakeSession whose first ``fail`` GET attempts fail transiently.

    ``exc`` (a connection error) takes precedence over a 5xx ``status``. The
    final attempt always returns a 200 with ``{"ok": True}``.
    """
    calls = {"count": 0}

    def responder(method: str, path: str, headers, body) -> FakeResponse:
        calls["count"] += 1
        if calls["count"] <= fail:
            if exc is not None:
                raise exc
            return FakeResponse(status, {"status": "error"})
        return FakeResponse(200, {"ok": True})

    return FakeSession(responder)


def test_get_retries_connection_error_then_succeeds(settings) -> None:
    # The first attempt raises a connection error; the retry succeeds.
    session = _flaky_session(1, exc=httpx.ConnectError("offline"))
    client = CiderRpcClient(settings, session=session, sleep=lambda _: None)

    result = client.playback_get("/is-playing")

    assert result == {"ok": True}
    assert len(session.requests) == 2  # one failure + one success


def test_get_retries_5xx_then_succeeds(settings) -> None:
    session = _flaky_session(2, status=503)
    client = CiderRpcClient(settings, session=session, sleep=lambda _: None)

    result = client.playback_get("/now-playing")

    assert result == {"ok": True}
    assert len(session.requests) == 3  # two failures + one success


def test_get_exhausts_retries_and_raises(settings) -> None:
    session = _flaky_session(3, status=503)  # more failures than retry_count=2
    client = CiderRpcClient(settings, session=session, sleep=lambda _: None)

    with pytest.raises(CiderRpcError) as exc_info:
        client.playback_get("/is-playing")

    # default cider_retry_count is 2 -> 3 total attempts
    assert len(session.requests) == 3
    assert exc_info.value.status_code == 503


def test_get_uses_exponential_backoff(settings) -> None:
    delays: list[float] = []
    session = _flaky_session(2, status=503)
    client = CiderRpcClient(settings, session=session, sleep=delays.append)

    client.playback_get("/is-playing")

    # 2 retries -> delays for attempts 0 and 1: 0.1 and 0.2
    assert delays == [0.1, 0.2]


def test_post_does_not_retry_connection_error(settings) -> None:
    calls = {"count": 0}

    def responder(method, path, headers, body) -> FakeResponse:
        calls["count"] += 1
        raise httpx.ConnectError("offline")

    session = FakeSession(responder)
    client = CiderRpcClient(settings, session=session, sleep=lambda _: None)

    with pytest.raises(CiderRpcError):
        client.playback_post("/play", {"track": "x"})

    assert calls["count"] == 1  # no retries for POST


def test_post_does_not_retry_5xx(settings) -> None:
    calls = {"count": 0}

    def responder(method, path, headers, body) -> FakeResponse:
        calls["count"] += 1
        return FakeResponse(503, {"status": "error"})

    session = FakeSession(responder)
    client = CiderRpcClient(settings, session=session, sleep=lambda _: None)

    with pytest.raises(CiderRpcError) as exc_info:
        client.playback_post("/play", {"track": "x"})

    assert calls["count"] == 1  # POST is never retried, even on 5xx
    assert exc_info.value.status_code == 503


@pytest.mark.parametrize("status_code", [400, 401, 403, 409, 422])
def test_get_does_not_retry_client_errors(settings, status_code: int) -> None:
    calls = {"count": 0}

    def responder(method, path, headers, body) -> FakeResponse:
        calls["count"] += 1
        return FakeResponse(status_code, {"detail": "bad request"})

    session = FakeSession(responder)
    client = CiderRpcClient(settings, session=session, sleep=lambda _: None)

    with pytest.raises(CiderRpcError) as exc_info:
        client.playback_get("/is-playing")

    assert calls["count"] == 1  # 4xx is definitive, never retried
    assert exc_info.value.status_code == status_code


def test_get_204_returns_none_without_retry(settings) -> None:
    calls = {"count": 0}

    def responder(method, path, headers, body) -> FakeResponse:
        calls["count"] += 1
        return FakeResponse(204)

    session = FakeSession(responder)
    client = CiderRpcClient(settings, session=session, sleep=lambda _: None)

    assert client.playback_get("/is-playing") is None
    assert calls["count"] == 1


def test_failure_callback_reports_once_after_retries_exhausted(settings) -> None:
    reports: list[dict] = []
    session = _flaky_session(3, status=503)  # exceeds retry_count=2
    client = CiderRpcClient(
        settings,
        session=session,
        failure_callback=reports.append,
        sleep=lambda _: None,
    )

    with pytest.raises(CiderRpcError):
        client.playback_get("/is-playing")

    # The callback fires exactly once (on the final failure), not per attempt.
    assert len(reports) == 1
    assert reports[0]["status_code"] == 503


def test_cider_retry_count_zero_disables_retries(settings) -> None:
    from vesper.config import Settings as _Settings

    values = {k: getattr(settings, k) for k in type(settings).model_fields if k != "config_path"}
    values["cider_retry_count"] = 0
    no_retry_settings = _Settings(**values)

    session = _flaky_session(1, status=503)
    client = CiderRpcClient(no_retry_settings, session=session, sleep=lambda _: None)

    with pytest.raises(CiderRpcError):
        client.playback_get("/is-playing")

    assert len(session.requests) == 1  # no retry when count is 0


def test_playback_get_uses_dual_token_headers(rpc_client) -> None:
    client, session = rpc_client

    client.playback_get("/is-playing")

    assert session.requests[0]["path"] == "/api/v1/playback/is-playing"
    assert session.requests[0]["headers"]["apptoken"] == "secret-token"
    assert session.requests[0]["headers"]["apitoken"] == "secret-token"


def test_run_amapi_v3_supports_method_and_body(rpc_client) -> None:
    client, session = rpc_client

    client.run_amapi_v3("/v1/me/library/playlists", method="POST", body={"attributes": {"name": "Mix"}})

    assert session.requests[0]["path"] == "/api/v1/amapi/run-v3"
    assert session.requests[0]["json"] == {
        "path": "/v1/me/library/playlists",
        "method": "POST",
        "body": {"attributes": {"name": "Mix"}},
    }


def test_search_catalog_passes_through_valid_storefront(rpc_client) -> None:
    client, session = rpc_client

    client.search_catalog("some query", storefront="jp")

    assert session.requests[0]["json"]["path"].startswith("/v1/catalog/jp/search")


def test_search_catalog_rejects_unsafe_storefront(rpc_client) -> None:
    client, session = rpc_client

    client.search_catalog("some query", storefront="../../admin")

    path = session.requests[0]["json"]["path"]
    assert path.startswith("/v1/catalog/us/search")
    assert "../" not in path
    assert "admin" not in path


def test_catalog_resource_search_sanitizes_storefront(rpc_client) -> None:
    from vesper.catalog import catalog_resource_search

    client, session = rpc_client

    catalog_resource_search(client, "query", resource_type="songs", storefront="us/../../v1/me/library")

    path = session.requests[0]["json"]["path"]
    assert path.startswith("/v1/catalog/us/search")
    assert "../" not in path
    assert "library" not in path


def test_catalog_relationship_tracks_sanitizes_storefront(rpc_client) -> None:
    from vesper.catalog import catalog_relationship_tracks

    client, session = rpc_client

    catalog_relationship_tracks(
        client, "/albums/album-1/tracks", result_limit=1, page_limit=1, storefront="us/../../v1/me/library"
    )

    path = session.requests[0]["json"]["path"]
    assert path.startswith("/v1/catalog/us/albums/album-1/tracks")
    assert "../" not in path
    assert "library" not in path


def test_load_genre_map_sanitizes_storefront(rpc_client) -> None:
    from vesper.catalog import load_genre_map

    client, session = rpc_client

    load_genre_map(client, {}, storefront="us/../../v1/me/library")

    path = session.requests[0]["json"]["path"]
    assert path == "/v1/catalog/us/genres"
    assert "../" not in path
    assert "library" not in path
