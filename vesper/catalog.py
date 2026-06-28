"""Apple Music catalog and library search helpers.

Extracted from :class:`CiderAgentService` so the service facade stays focused
on playback, preferences, and text-request orchestration. These functions take
an RPC client (and other small dependencies) rather than the service itself,
keeping ``vesper.catalog`` free of the service import.

The flatten helpers are shared across catalog and playback code, so they live
here and are re-exported by ``vesper.service`` for backward compatibility.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import CiderRpcError
from .rpc import CiderRpcClient, _sanitize_storefront

LOGGER = logging.getLogger(__name__)

SESSION_STOREFRONT = "us"


def track_attributes(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    attributes = item.get("attributes")
    if isinstance(attributes, dict):
        return attributes
    if "attributes" in item:
        return {}
    return item


def now_playing_info(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    info = payload.get("info", {})
    if isinstance(info, list):
        for item in info:
            attributes = track_attributes(item)
            if attributes:
                return attributes
        return {}
    return track_attributes(info)


def flatten_track_item(item: Any) -> dict[str, Any]:
    raw_item = item if isinstance(item, dict) else {}
    attributes = track_attributes(item)
    play_params = attributes.get("playParams", {})
    artwork = attributes.get("artwork", {})
    return {
        "id": raw_item.get("id"),
        "type": raw_item.get("type"),
        "href": raw_item.get("href"),
        "title": attributes.get("name"),
        "artist": attributes.get("artistName"),
        "album": attributes.get("albumName"),
        "duration_millis": attributes.get("durationInMillis"),
        "isrc": attributes.get("isrc"),
        "artwork_url": artwork.get("url"),
        "play_params": {
            "id": play_params.get("id"),
            "kind": play_params.get("kind"),
            "is_library": play_params.get("isLibrary"),
        },
        "raw": item,
    }


def flatten_playlist_item(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
    curator = attributes.get("curatorName")
    if not curator and isinstance(item.get("relationships"), dict):
        curator_data = item["relationships"].get("curator", {}).get("data", [])
        if isinstance(curator_data, list) and curator_data:
            curator = curator_data[0].get("attributes", {}).get("name")
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "href": item.get("href"),
        "name": attributes.get("name"),
        "description": attributes.get("description", {}).get("standard")
        if isinstance(attributes.get("description"), dict)
        else attributes.get("description"),
        "can_edit": attributes.get("canEdit"),
        "is_public": attributes.get("isPublic"),
        "curator": curator,
        "playlist_type": attributes.get("playlistType"),
        "raw": item,
    }


def flatten_artist_item(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "name": attributes.get("name"),
        "href": item.get("href"),
        "raw": item,
    }


def flatten_album_item(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "name": attributes.get("name"),
        "artist": attributes.get("artistName"),
        "track_count": attributes.get("trackCount"),
        "href": item.get("href"),
        "raw": item,
    }


def search_catalog(
    rpc: CiderRpcClient,
    query: str,
    *,
    limit: int = 10,
    storefront: str = "us",
    offset: int = 0,
) -> dict[str, Any]:
    payload = rpc.search_catalog(query=query, limit=limit, storefront=storefront, offset=offset)
    items = payload.get("data", {}).get("results", {}).get("songs", {}).get("data", [])
    return {
        "status": "ok",
        "query": query,
        "storefront": storefront,
        "offset": offset,
        "count": len(items) if isinstance(items, list) else 0,
        "tracks": [flatten_track_item(item) for item in items] if isinstance(items, list) else [],
        "raw": payload,
    }


def catalog_resource_search(
    rpc: CiderRpcClient,
    query: str,
    *,
    resource_type: str,
    limit: int = 5,
    storefront: str = SESSION_STOREFRONT,
) -> list[dict[str, Any]]:
    from urllib.parse import quote

    storefront = _sanitize_storefront(storefront)
    path = (
        f"/v1/catalog/{storefront}/search?term={quote(query, safe='')}"
        f"&types={resource_type}&limit={limit}"
    )
    payload = rpc.run_amapi_v3(path)
    items = payload.get("data", {}).get("results", {}).get(resource_type, {}).get("data", [])
    return list(items) if isinstance(items, list) else []


def catalog_relationship_tracks(
    rpc: CiderRpcClient,
    path: str,
    *,
    result_limit: int,
    page_limit: int,
    storefront: str = SESSION_STOREFRONT,
) -> list[dict[str, Any]]:
    storefront = _sanitize_storefront(storefront)
    tracks: list[dict[str, Any]] = []
    offset = 0
    while len(tracks) < result_limit:
        limit = min(page_limit, result_limit - len(tracks))
        separator = "&" if "?" in path else "?"
        page_path = f"/v1/catalog/{storefront}{path}{separator}limit={limit}"
        if offset:
            page_path = f"{page_path}&offset={offset}"
        payload = rpc.run_amapi_v3(page_path)
        data = payload.get("data", {})
        items = data.get("data", [])
        if not items:
            items = data.get("results", {}).get("songs", [{}])[0].get("data", [])
        if not isinstance(items, list) or not items:
            break
        playable = [
            flatten_track_item(item)
            for item in items
            if isinstance(item, dict)
            and item.get("type") == "songs"
            and isinstance(item.get("attributes", {}).get("playParams"), dict)
        ]
        tracks.extend(playable)
        if len(items) < limit:
            break
        offset += len(items)
    return tracks[:result_limit]


def load_genre_map(
    rpc: CiderRpcClient,
    genre_cache: dict[str, dict[str, str]],
    storefront: str = SESSION_STOREFRONT,
) -> dict[str, str]:
    storefront = _sanitize_storefront(storefront)
    if storefront in genre_cache:
        return dict(genre_cache[storefront])
    try:
        payload = rpc.run_amapi_v3(f"/v1/catalog/{storefront}/genres")
        items = payload.get("data", {}).get("data", [])
        genre_map = {
            str(item.get("attributes", {}).get("name", "")).strip(): str(item.get("id", "")).strip()
            for item in items
            if isinstance(item, dict)
            and str(item.get("attributes", {}).get("name", "")).strip()
            and str(item.get("attributes", {}).get("name", "")).strip() != "Music"
            and str(item.get("id", "")).strip()
        }
        if len(genre_map) > 50:
            genre_map = {}
    except CiderRpcError as exc:
        # Genre data is non-essential enrichment used only when resolving
        # genre-typed session sources. A catalog/RPC failure degrades
        # gracefully to an empty map (genre sources simply resolve to
        # nothing) rather than blocking session planning. Unexpected errors
        # are not caught here so they remain visible and actionable.
        LOGGER.warning("Could not load Apple Music genres for %s: %s", storefront, exc)
        genre_map = {}
    genre_cache[storefront] = genre_map
    return dict(genre_map)


def search_library(
    rpc: CiderRpcClient,
    query: str,
    *,
    limit: int = 10,
    types: list[str] | None = None,
) -> dict[str, Any]:
    payload = rpc.search_library(query=query, limit=limit, types=types)
    results = payload.get("data", {}).get("results", {}) if isinstance(payload, dict) else {}
    tracks = results.get("library-songs", {}).get("data", [])
    playlists = results.get("library-playlists", {}).get("data", [])
    albums = results.get("library-albums", {}).get("data", [])
    artists = results.get("library-artists", {}).get("data", [])
    return {
        "status": "ok",
        "query": query,
        "types": types or ["library-songs", "library-albums", "library-artists", "library-playlists"],
        "counts": {
            "tracks": len(tracks) if isinstance(tracks, list) else 0,
            "playlists": len(playlists) if isinstance(playlists, list) else 0,
            "albums": len(albums) if isinstance(albums, list) else 0,
            "artists": len(artists) if isinstance(artists, list) else 0,
        },
        "tracks": [flatten_track_item(item) for item in tracks] if isinstance(tracks, list) else [],
        "playlists": [flatten_playlist_item(item) for item in playlists] if isinstance(playlists, list) else [],
        "albums": [flatten_album_item(item) for item in albums] if isinstance(albums, list) else [],
        "artists": [flatten_artist_item(item) for item in artists] if isinstance(artists, list) else [],
        "raw": payload,
    }


def search_library_tracks(rpc: CiderRpcClient, query: str, *, limit: int = 10) -> dict[str, Any]:
    from urllib.parse import quote

    payload = rpc.run_amapi_v3(f"/v1/me/library/search?term={quote(query, safe='')}&types=library-songs&limit={limit}")
    items = payload.get("data", {}).get("results", {}).get("library-songs", {}).get("data", [])
    return {
        "status": "ok",
        "query": query,
        "count": len(items) if isinstance(items, list) else 0,
        "tracks": [flatten_track_item(item) for item in items] if isinstance(items, list) else [],
        "raw": payload,
    }


def list_library_playlists(rpc: CiderRpcClient, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
    path = f"/v1/me/library/playlists?limit={limit}"
    if offset:
        path = f"{path}&offset={offset}"
    payload = rpc.run_amapi_v3(path)
    items = payload.get("data", {}).get("data", [])
    return {
        "status": "ok",
        "count": len(items) if isinstance(items, list) else 0,
        "next": payload.get("data", {}).get("next") if isinstance(payload, dict) else None,
        "playlists": [flatten_playlist_item(item) for item in items] if isinstance(items, list) else [],
        "raw": payload,
    }


def search_library_playlists(rpc: CiderRpcClient, query: str, *, limit: int = 10) -> dict[str, Any]:
    from urllib.parse import quote

    payload = rpc.run_amapi_v3(
        f"/v1/me/library/search?term={quote(query, safe='')}&types=library-playlists&limit={limit}"
    )
    items = payload.get("data", {}).get("results", {}).get("library-playlists", {}).get("data", [])
    return {
        "status": "ok",
        "query": query,
        "count": len(items) if isinstance(items, list) else 0,
        "playlists": [flatten_playlist_item(item) for item in items] if isinstance(items, list) else [],
        "raw": payload,
    }


def get_library_playlist(rpc: CiderRpcClient, playlist_id: str) -> dict[str, Any]:
    payload = rpc.run_amapi_v3(f"/v1/me/library/playlists/{playlist_id}")
    items = payload.get("data", {}).get("data", [])
    playlist = items[0] if isinstance(items, list) and items else {}
    return {
        "status": "ok",
        "playlist": flatten_playlist_item(playlist) if isinstance(playlist, dict) else None,
        "raw": payload,
    }


def get_library_playlist_tracks(rpc: CiderRpcClient, playlist_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    path = f"/v1/me/library/playlists/{playlist_id}/tracks?limit={limit}"
    if offset:
        path = f"{path}&offset={offset}"
    payload = rpc.run_amapi_v3(path)
    items = payload.get("data", {}).get("data", [])
    return {
        "status": "ok",
        "playlist_id": playlist_id,
        "count": len(items) if isinstance(items, list) else 0,
        "tracks": [flatten_track_item(item) for item in items] if isinstance(items, list) else [],
        "raw": payload,
    }


def list_recently_played(rpc: CiderRpcClient, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
    path = f"/v1/me/recent/played/tracks?limit={limit}"
    if offset:
        path = f"{path}&offset={offset}"
    payload = rpc.run_amapi_v3(path)
    items = payload.get("data", {}).get("data", [])
    return {
        "status": "ok",
        "count": len(items) if isinstance(items, list) else 0,
        "tracks": [flatten_track_item(item) for item in items] if isinstance(items, list) else [],
        "raw": payload,
    }
