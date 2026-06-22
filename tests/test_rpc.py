from __future__ import annotations


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
