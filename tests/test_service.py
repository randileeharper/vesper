from __future__ import annotations

import pytest

from cider_agent.errors import CiderRpcError, CiderValidationError


def test_create_playlist_is_explicitly_unsupported(service) -> None:
    with pytest.raises(CiderRpcError):
        service.create_playlist(name="Late Night Mix")


def test_add_playlist_tracks_requires_refs(service) -> None:
    with pytest.raises(CiderValidationError):
        service.add_playlist_tracks("playlist-1", track_refs=[])


def test_add_playlist_tracks_is_explicitly_unsupported(service) -> None:
    with pytest.raises(CiderRpcError):
        service.add_playlist_tracks("playlist-1", track_refs=[{"id": "track-1", "type": "songs"}])


def test_preferences_round_trip(service) -> None:
    remembered = service.remember_preference(kind="like", value="k-pop", category="genre")
    listed = service.list_preferences()

    assert remembered["preference"]["value"] == "k-pop"
    assert listed["count"] == 1


def test_recommendation_uses_preferences(service) -> None:
    service.remember_preference(kind="like", value="k-pop", category="genre")

    result = service.recommend()

    assert result["status"] == "ok"
    assert result["recommendation"]["items"][0]["play_params"]["id"] == "catalog-track-1"
    assert result["recommendation"]["source"] == "catalog"


def test_run_action_dispatches_search(service) -> None:
    result = service.run_action("search_library_tracks", {"query": "k-pop", "limit": 3})

    assert result["action"] == "search_library_tracks"
    assert result["result"]["count"] == 1


def test_default_search_uses_catalog_by_default(service) -> None:
    result = service.search("kep1er", limit=3)

    assert result["search_source"] == "catalog"
    assert result["tracks"][0]["artist"] == "Catalog Artist"


def test_handle_text_request_uses_resolver(service) -> None:
    result = service.handle_text_request("play some kep1er")

    assert result["resolver"] == "stub"
    assert result["resolved_action"]["action"] == "search"
    assert result["execution"]["action"] == "search"


def test_status_handles_is_playing_payload_shape(service) -> None:
    result = service.status()

    assert result["playback"]["is_playing"] is True
