from __future__ import annotations

from typing import Any

from vesper.resolver import SessionQueryPlan, SessionSearchSource, SessionTrackSelection


def _track(track_id: str, title: str, artist: str) -> dict[str, Any]:
    return {
        "id": track_id,
        "title": title,
        "artist": artist,
        "album": "Album",
        "play_params": {"id": track_id, "kind": "songs", "is_library": False},
    }


def test_artist_source_uses_exact_artist_and_top_songs(service, monkeypatch) -> None:
    searches: list[tuple[str, str, int]] = []
    paths: list[str] = []

    def search(term: str, *, resource_type: str, limit: int, storefront: str = "us"):
        searches.append((term, resource_type, limit))
        return [
            {"id": "wrong", "attributes": {"name": "RADWIMPS Tribute"}},
            {"id": "right", "attributes": {"name": "RADWIMPS"}},
        ]

    def tracks(path: str, *, storefront: str = "us"):
        paths.append(path)
        return [_track("song-1", "Sparkle", "RADWIMPS")]

    monkeypatch.setattr(service, "_catalog_resource_search", search)
    monkeypatch.setattr(service, "_catalog_relationship_tracks", tracks)

    source = SessionSearchSource(kind="artist", term="RADWIMPS")
    pool = service._build_session_query_pool({"id": 1}, source)

    assert searches == [("RADWIMPS", "artists", 5)]
    assert paths == ["/artists/right/view/top-songs"]
    assert pool["resolved_artist_id"] == "right"
    assert pool["entries"][0]["track"]["title"] == "Sparkle"


def test_genre_source_resolves_exact_cached_genre_and_chart(service, monkeypatch) -> None:
    paths: list[str] = []
    monkeypatch.setattr(service, "_load_genre_map", lambda storefront="us": {"Pop": "14"})
    monkeypatch.setattr(
        service,
        "_catalog_relationship_tracks",
        lambda path, storefront="us": paths.append(path) or [_track("pop-1", "Pop Song", "Artist")],
    )

    pool = service._build_session_query_pool({"id": 1}, SessionSearchSource(kind="genre", term="Pop"))

    assert paths == ["/charts?types=songs&genre=14"]
    assert pool["resolved_genre_id"] == "14"


def test_vibe_source_selects_playlist_once_and_keeps_playlist_order(service, monkeypatch) -> None:
    playlist_selections: list[list[dict[str, Any]]] = []

    class Resolver:
        def select_session_playlist(self, request, service, session, source, candidates):
            playlist_selections.append(candidates)
            return SessionTrackSelection(selected_index=1, resolver="stub")

        def select_session_track(self, request, service, session, search_query, candidates):
            return SessionTrackSelection(selected_index=0, resolver="stub")

    service._resolver = Resolver()
    monkeypatch.setattr(
        service,
        "_catalog_resource_search",
        lambda term, resource_type, limit, storefront="us": [
            {
                "id": "playlist-1",
                "type": "playlists",
                "attributes": {"name": "Wrong", "curatorName": "Apple Music", "playlistType": "editorial"},
            },
            {
                "id": "playlist-2",
                "type": "playlists",
                "attributes": {
                    "name": "Upbeat Pop",
                    "curatorName": "Apple Music Pop",
                    "playlistType": "editorial",
                    "description": {"standard": "Bright pop for moving."},
                },
            },
        ],
    )
    monkeypatch.setattr(
        service,
        "_catalog_relationship_tracks",
        lambda path, storefront="us": [
            _track("song-1", "First", "Artist"),
            _track("song-2", "Second", "Artist"),
        ],
    )

    source = SessionSearchSource(kind="vibe", term="upbeat pop")
    service._ensure_session_query_pools({"id": 1, "request_text": "upbeat pop"}, [source])
    service._ensure_session_query_pools({"id": 1, "request_text": "upbeat pop"}, [source])
    pool = next(iter(service._get_session_runtime(1)["query_pools"].values()))

    assert len(playlist_selections) == 1
    assert playlist_selections[0][1] == {
        "name": "Upbeat Pop",
        "curator": "Apple Music Pop",
        "playlist_type": "editorial",
        "description": "Bright pop for moving.",
    }
    assert pool["resolved_playlist_id"] == "playlist-2"
    assert [entry["track"]["title"] for entry in pool["entries"]] == ["First", "Second"]


def test_vibe_source_rephrases_empty_playlist_search_before_failing(service, monkeypatch) -> None:
    searches: list[str] = []
    rephrase_calls: list[list[str]] = []

    class Resolver:
        def rephrase_session_vibe(self, request, service, session, source, attempted_terms):
            rephrase_calls.append(attempted_terms)
            return "focus" if attempted_terms == ["oddly specific productivity fog"] else None

        def select_session_playlist(self, request, service, session, source, candidates):
            return SessionTrackSelection(selected_index=0, resolver="stub")

    def search(term: str, *, resource_type: str, limit: int, storefront: str = "us"):
        searches.append(term)
        if term == "focus":
            return [{"id": "playlist-focus", "type": "playlists", "attributes": {"name": "Focus"}}]
        return []

    service._resolver = Resolver()
    monkeypatch.setattr(service, "_catalog_resource_search", search)
    monkeypatch.setattr(
        service,
        "_catalog_relationship_tracks",
        lambda path, storefront="us": [_track("song-1", "Deep Work", "Artist")],
    )

    pool = service._build_session_query_pool(
        {"id": 1, "request_text": "play oddly specific productivity fog"},
        SessionSearchSource(kind="vibe", term="oddly specific productivity fog"),
    )

    assert searches == ["oddly specific productivity fog", "focus"]
    assert rephrase_calls == [["oddly specific productivity fog"]]
    assert pool["resolved_playlist_id"] == "playlist-focus"
    assert pool["resolved_vibe_term"] == "focus"
    assert pool["entries"][0]["track"]["title"] == "Deep Work"


def test_vibe_source_uses_fallback_rephrase_when_resolver_has_no_alternate(service, monkeypatch) -> None:
    searches: list[str] = []

    def search(term: str, *, resource_type: str, limit: int, storefront: str = "us"):
        searches.append(term)
        if term == "fog":
            return [{"id": "playlist-fog", "type": "playlists", "attributes": {"name": "Fog"}}]
        return []

    monkeypatch.setattr(service, "_catalog_resource_search", search)
    monkeypatch.setattr(
        service,
        "_catalog_relationship_tracks",
        lambda path, storefront="us": [_track("song-1", "Haze", "Artist")],
    )

    pool = service._build_session_query_pool(
        {"id": 1, "request_text": "play oddly specific productivity fog"},
        SessionSearchSource(kind="vibe", term="oddly specific productivity fog"),
    )

    assert searches == ["oddly specific productivity fog", "oddly specific", "fog"]
    assert pool["resolved_playlist_id"] == "playlist-fog"
    assert pool["resolved_vibe_term"] == "fog"


def test_typed_steering_adds_heterogeneous_sources(service) -> None:
    runtime = {
        "active_search_sources": [{"kind": "genre", "term": "Pop"}],
    }
    update = service._normalize_session_search_update(
        {"mode": "add", "sources": [{"kind": "artist", "term": "RADWIMPS"}]}
    )

    sources = service._next_session_search_sources(runtime, update)

    assert service._sources_payload(sources) == [
        {"kind": "genre", "term": "Pop"},
        {"kind": "artist", "term": "RADWIMPS"},
    ]


def test_genre_context_excludes_music_and_guards_large_lists(service) -> None:
    service._rpc.run_amapi_v3 = lambda path: {
        "data": {
            "data": [
                {"id": "0", "attributes": {"name": "Music"}},
                {"id": "14", "attributes": {"name": "Pop"}},
            ]
        }
    }
    assert service.session_genre_names() == ["Pop"]

    service._genre_cache.clear()
    service._rpc.run_amapi_v3 = lambda path: {
        "data": {
            "data": [
                {"id": str(index), "attributes": {"name": f"Genre {index}"}}
                for index in range(51)
            ]
        }
    }
    assert service.session_genre_names() == []


def test_empty_source_replans_once_with_rejected_source_excluded(service, monkeypatch) -> None:
    class Resolver:
        def __init__(self) -> None:
            self.plans = 0
            self.rejected_contexts: list[list[dict[str, str]]] = []

        def plan_session(self, request, service, session, count):
            self.plans += 1
            self.rejected_contexts.append(service.session_rejected_search_sources(session))
            source = (
                SessionSearchSource(kind="vibe", term="bad vibe")
                if self.plans == 1
                else SessionSearchSource(kind="artist", term="RADWIMPS")
            )
            return SessionQueryPlan(search_sources=[source], resolver="stub")

        def select_session_track(self, request, service, session, search_query, candidates):
            return SessionTrackSelection(selected_index=0, resolver="stub")

    resolver = Resolver()
    service._resolver = resolver
    monkeypatch.setattr(
        service,
        "_fetch_session_source_results",
        lambda session, source: (
            ([], {})
            if source.term == "bad vibe"
            else ([_track("song-1", "Sparkle", "RADWIMPS")], {"resolved_artist_id": "artist-1"})
        ),
    )
    session = service._preferences.start_session(request_text="play something energetic")

    result = service._play_session_track(session, selection_strategy="test")

    assert result["tracks"][0]["title"] == "Sparkle"
    assert resolver.plans == 2
    assert resolver.rejected_contexts == [
        [],
        [{"kind": "vibe", "term": "bad vibe"}],
    ]
