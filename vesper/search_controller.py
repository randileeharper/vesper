"""Catalog and library search for the Cider agent.

Extracted from the :class:`vesper.service.CiderAgentService` god-class
(issue #42). This controller owns catalog search, library search, playlist
lookups, and the play-from-search flow (including candidate-match resolution
and pool-based selection).

Cross-cutting capabilities (RPC client, settings, playback via play_item,
default search source) are reached through the :class:`SearchHost` Protocol
so the controller never imports the concrete service class.
"""

from __future__ import annotations

from typing import Any, Protocol

from .catalog import (
    catalog_relationship_tracks as _catalog_relationship_tracks_impl,
    catalog_resource_search as _catalog_resource_search_impl,
    get_library_playlist as _get_library_playlist_impl,
    get_library_playlist_tracks as _get_library_playlist_tracks_impl,
    list_library_playlists as _list_library_playlists_impl,
    list_recently_played as _list_recently_played_impl,
    load_genre_map as _load_genre_map_impl,
    search_catalog as _search_catalog_impl,
    search_library as _search_library_impl,
    search_library_playlists as _search_library_playlists_impl,
    search_library_tracks as _search_library_tracks_impl,
)
from .errors import CiderAgentError, CiderValidationError
from .matching import (
    best_artist_track_match,
    best_artist_track_matches,
    best_playlist_match,
    best_track_match,
    top_pool_order,
)
from .validation import (
    validate_index,
    validate_limit_offset,
    validate_playlist_id,
    validate_search,
)


class SearchHost(Protocol):
    """Structural interface for the cross-cutting capabilities
    :class:`SearchController` borrows from its host."""

    def play_item(
        self,
        item_id: str,
        *,
        kind: str = "songs",
        is_library: bool = False,
        track: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    def default_search_source(self) -> str:
        ...

    @property
    def _rpc(self) -> Any:
        ...

    @property
    def _random(self) -> Any:
        ...

    # Class-level constant used for pool-based selection.
    TRACK_SELECTION_POOL_SIZE: int
    SESSION_SEARCH_RESULT_LIMIT: int
    SESSION_SEARCH_PAGE_LIMIT: int
    SESSION_STOREFRONT: str


class SearchController:
    """Catalog search, library search, playlist lookups, and play-from-search."""

    def __init__(self, host: SearchHost, *, rpc) -> None:
        self._host = host
        self._rpc = rpc
        self._genre_cache: dict[str, dict[str, str]] = {}

    # --- catalog search ---

    def search_catalog(self, query: str, *, limit: int = 10, storefront: str = "us", offset: int = 0) -> dict[str, Any]:
        validate_limit_offset(limit, offset)
        if not query.strip():
            raise CiderValidationError("query cannot be empty.")
        return _search_catalog_impl(self._rpc, query, limit=limit, storefront=storefront, offset=offset)

    def search_catalog_tracks(self, query: str, *, limit: int = 10, storefront: str = "us", offset: int = 0) -> dict[str, Any]:
        return self.search_catalog(query, limit=limit, storefront=storefront, offset=offset)

    def _catalog_resource_search(
        self,
        query: str,
        *,
        resource_type: str,
        limit: int = 5,
        storefront: str = "us",
    ) -> list[dict[str, Any]]:
        return _catalog_resource_search_impl(self._rpc, query, resource_type=resource_type, limit=limit, storefront=storefront)

    def _catalog_relationship_tracks(
        self,
        path: str,
        *,
        result_limit: int,
        page_limit: int,
        storefront: str = "us",
    ) -> list[dict[str, Any]]:
        return _catalog_relationship_tracks_impl(
            self._rpc, path, result_limit=result_limit, page_limit=page_limit, storefront=storefront
        )

    def _load_genre_map(self, storefront: str = "us") -> dict[str, str]:
        return _load_genre_map_impl(self._rpc, self._genre_cache, storefront=storefront)

    def session_genre_names(self, *, storefront: str) -> list[str]:
        return list(self._load_genre_map(storefront))

    # --- library search ---

    def search_library(self, query: str, *, limit: int = 10, types: list[str] | None = None) -> dict[str, Any]:
        validate_search(query, limit)
        return _search_library_impl(self._rpc, query, limit=limit, types=types)

    def search_library_tracks(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        validate_search(query, limit)
        return _search_library_tracks_impl(self._rpc, query, limit=limit)

    def list_library_playlists(self, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        validate_limit_offset(limit, offset)
        return _list_library_playlists_impl(self._rpc, limit=limit, offset=offset)

    def search_library_playlists(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        validate_search(query, limit)
        return _search_library_playlists_impl(self._rpc, query, limit=limit)

    def get_library_playlist(self, playlist_id: str) -> dict[str, Any]:
        validate_playlist_id(playlist_id)
        return _get_library_playlist_impl(self._rpc, playlist_id)

    def get_library_playlist_tracks(self, playlist_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        validate_playlist_id(playlist_id)
        validate_limit_offset(limit, offset)
        return _get_library_playlist_tracks_impl(self._rpc, playlist_id, limit=limit, offset=offset)

    def list_recently_played(self, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        validate_limit_offset(limit, offset)
        return _list_recently_played_impl(self._rpc, limit=limit, offset=offset)

    # --- combined search + play ---

    def search(self, query: str, *, limit: int = 10, storefront: str = "us") -> dict[str, Any]:
        if self._host.default_search_source() == "library":
            result = self.search_library(query, limit=limit)
        else:
            result = self.search_catalog(query, limit=limit, storefront=storefront)
        result["search_source"] = self._host.default_search_source()
        return result

    def play_library_playlist(self, playlist_name: str) -> dict[str, Any]:
        if not playlist_name.strip():
            raise CiderValidationError("playlist_name cannot be empty.")
        exact_results = self.search_library_playlists(playlist_name, limit=10)
        playlists = list(exact_results.get("playlists", []))
        if not playlists:
            playlists = list(self.list_library_playlists(limit=50).get("playlists", []))
        match = best_playlist_match(playlists, playlist_name=playlist_name)
        if match is None:
            raise CiderValidationError(f"No library playlist matched: {playlist_name}")
        playlist_id = str(match.get("id", "")).strip()
        playlist_type = str(match.get("type", "library-playlists")).strip() or "library-playlists"
        if not playlist_id:
            raise CiderValidationError("Matched playlist did not include a playable id.")
        playback = self._host.play_item(playlist_id, kind=playlist_type, is_library=True)
        return {
            "status": "ok",
            "playlist": match,
            "playback": playback,
        }

    def play_search_result(
        self,
        *,
        query: str,
        source: str = "library",
        index: int = 0,
        storefront: str = "us",
    ) -> dict[str, Any]:
        validate_search(query, 25)
        validate_index(index, "index")
        if source == "library":
            results = self.search_library_tracks(query, limit=25)
            items = results["tracks"]
        elif source == "catalog":
            results = self.search_catalog_tracks(query, limit=25, storefront=storefront)
            items = results["tracks"]
        elif source == "default":
            resolved_source = self._host.default_search_source()
            return self.play_search_result(query=query, source=resolved_source, index=index, storefront=storefront)
        else:
            raise CiderValidationError("source must be 'library', 'catalog', or 'default'.")
        if index >= len(items):
            raise CiderValidationError("Search result index was out of range.")
        return self._play_flattened_track(items[index], is_library_default=source == "library")

    def play_candidate_match(
        self,
        *,
        candidate_tracks: list[dict[str, str]] | None = None,
        candidate_queries: list[str] | None = None,
        storefront: str = "us",
    ) -> dict[str, Any]:
        track_candidates = candidate_tracks or []
        query_candidates = candidate_queries or []

        for candidate in track_candidates:
            title = str(candidate.get("title", "")).strip()
            artist = str(candidate.get("artist", "")).strip()
            if not title or not artist:
                continue
            search = self.search_catalog_tracks(f"{artist} {title}", limit=10, storefront=storefront)
            match = best_track_match(search["tracks"], title=title, artist=artist)
            if match is not None:
                playback = self._play_flattened_track(match, is_library_default=False)
                return {
                    "status": "ok",
                    "selection_strategy": "candidate_track_exactish_match",
                    "selected_track": match,
                    "playback": playback,
                }

        for query in query_candidates:
            query_text = str(query).strip()
            if not query_text:
                continue
            try:
                result = self._play_search_result_from_pool(query=query_text, source="default", storefront=storefront)
            except CiderAgentError:
                continue
            return {
                "status": "ok",
                "selection_strategy": "candidate_query_fallback",
                "selected_query": query_text,
                "playback": result,
            }

        raise CiderValidationError("No playable candidate match could be resolved.")

    # --- matching helpers (delegate to vesper.matching) ---

    def _best_track_match(self, tracks: list[dict[str, Any]], *, title: str, artist: str) -> dict[str, Any] | None:
        return best_track_match(tracks, title=title, artist=artist)

    def _best_artist_track_match(self, tracks: list[dict[str, Any]], *, artist: str) -> dict[str, Any] | None:
        return best_artist_track_match(tracks, artist=artist, rng=self._host._random, pool_size=self._host.TRACK_SELECTION_POOL_SIZE)

    def _best_playlist_match(self, playlists: list[dict[str, Any]], *, playlist_name: str) -> dict[str, Any] | None:
        return best_playlist_match(playlists, playlist_name=playlist_name)

    def _best_artist_track_matches(self, tracks: list[dict[str, Any]], *, artist: str, limit: int) -> list[dict[str, Any]]:
        return best_artist_track_matches(tracks, artist=artist, limit=limit, rng=self._host._random, pool_size=self._host.TRACK_SELECTION_POOL_SIZE)

    def _top_pool_order(
        self,
        tracks: list[dict[str, Any]],
        *,
        take: int,
        pool_size: int | None = None,
    ) -> list[dict[str, Any]]:
        return top_pool_order(tracks, take=take, rng=self._host._random, pool_size=pool_size, default_pool_size=self._host.TRACK_SELECTION_POOL_SIZE)

    # --- internal play helpers ---

    def _play_search_result_from_pool(self, *, query: str, source: str, storefront: str) -> dict[str, Any]:
        resolved_source = source.strip().lower()
        if resolved_source == "default":
            resolved_source = self._host.default_search_source()
        if resolved_source not in {"catalog", "library"}:
            raise CiderValidationError("source must be one of: default, catalog, library.")
        if resolved_source == "catalog":
            results = self.search_catalog_tracks(query, limit=10, storefront=storefront)
        else:
            results = self.search_library_tracks(query, limit=10)
        tracks = results["tracks"]
        if not tracks:
            raise CiderValidationError(f"No tracks found for query: {query}")
        selected = self._top_pool_order(tracks, take=1)[0]
        return self._play_flattened_track(selected, is_library_default=resolved_source == "library")

    def _play_flattened_track(self, track: dict[str, Any], *, is_library_default: bool) -> dict[str, Any]:
        play_params = track.get("play_params", {})
        item_id = str(play_params.get("id", "")).strip()
        kind = str(play_params.get("kind", "songs")).strip() or "songs"
        is_library = bool(play_params.get("is_library", is_library_default))
        if not item_id:
            raise CiderValidationError("Resolved track did not include a playable id.")
        return self._host.play_item(item_id, kind=kind, is_library=is_library, track=track)
