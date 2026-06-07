"""Domain services for cider_agent."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .config import Settings
from .errors import CiderRpcError, CiderValidationError
from .resolver import ResolvedAction, Resolver, build_resolver
from .rpc import CiderRpcClient
from .storage import PreferenceStore


def _flatten_track_item(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
    play_params = attributes.get("playParams", {}) if isinstance(attributes, dict) else {}
    artwork = attributes.get("artwork", {}) if isinstance(attributes, dict) else {}
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "href": item.get("href"),
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


def _flatten_playlist_item(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
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
        "raw": item,
    }


def _flatten_artist_item(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "name": attributes.get("name"),
        "href": item.get("href"),
        "raw": item,
    }


def _flatten_album_item(item: dict[str, Any]) -> dict[str, Any]:
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


def _encode_query(query: str) -> str:
    return quote(query, safe="")


@dataclass
class RecommendationResult:
    """Normalized recommendation result."""

    strategy: str
    reason: str
    source: str
    items: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "reason": self.reason,
            "source": self.source,
            "count": len(self.items),
            "items": self.items,
        }


class DeterministicRecommender:
    """Use explicit preferences and available library/catalog search to rank tracks."""

    def recommend(
        self,
        *,
        service: "CiderAgentService",
        query: str | None = None,
        limit: int = 5,
        prefer_library: bool | None = None,
    ) -> RecommendationResult:
        preferences = service.list_preferences()["preferences"]
        positive = [item for item in preferences if item["kind"] == "like"]
        negative_values = {item["value"].strip().lower() for item in preferences if item["kind"] == "dislike"}

        search_terms: list[str] = []
        if query:
            search_terms.append(query)
        for item in positive:
            value = str(item["value"]).strip()
            if value and value not in search_terms:
                search_terms.append(value)
        if not search_terms:
            raise CiderValidationError("No explicit preferences are stored yet.")

        if prefer_library is None:
            prefer_library = service.default_search_source() == "library"
        source = "library" if prefer_library else "catalog"
        chosen_items: list[dict[str, Any]] = []
        for term in search_terms:
            if prefer_library:
                results = service.search_library_tracks(term, limit=limit)
                candidates = results["tracks"]
            else:
                results = service.search_catalog_tracks(term, limit=limit)
                candidates = results["tracks"]
            filtered = [
                item
                for item in candidates
                if str(item.get("title", "")).strip().lower() not in negative_values
                and str(item.get("artist", "")).strip().lower() not in negative_values
            ]
            chosen_items.extend(filtered)
            if chosen_items:
                break

        if not chosen_items and prefer_library:
            return self.recommend(service=service, query=query, limit=limit, prefer_library=False)
        if not chosen_items:
            raise CiderValidationError("No recommendation candidates matched the stored preferences.")
        return RecommendationResult(
            strategy="deterministic-explicit-preferences",
            reason=f"Matched {len(search_terms)} stored or requested preference terms.",
            source=source,
            items=chosen_items[:limit],
        )


class CiderAgentService:
    """High-level operations for the Cider agent."""

    def __init__(
        self,
        settings: Settings,
        *,
        rpc_client: CiderRpcClient | None = None,
        preference_store: PreferenceStore | None = None,
        recommender: DeterministicRecommender | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._settings = settings
        self._rpc = rpc_client or CiderRpcClient(settings)
        self._preferences = preference_store or PreferenceStore(settings.database_path)
        self._recommender = recommender or DeterministicRecommender()
        self._resolver = resolver or build_resolver(settings)

    def close(self) -> None:
        self._rpc.close()
        close = getattr(self._resolver, "close", None)
        if callable(close):
            close()

    def default_search_source(self) -> str:
        return self._settings.default_search_source

    def status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "source": "cider-agent",
            "config": self._settings.sanitized(),
            "playback": self.playback_snapshot(),
            "preferences_count": len(self._preferences.list_preferences()),
        }

    def get_now_playing(self) -> dict[str, Any]:
        payload = self._rpc.playback_get("/now-playing")
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        return {
            "status": "ok",
            "source": "cider-rpc",
            "track": _flatten_track_item({"attributes": info}),
            "raw": payload,
        }

    def playback_snapshot(self) -> dict[str, Any]:
        is_playing_payload = self._rpc.playback_get("/is-playing")
        now_playing_payload = self._rpc.playback_get("/now-playing")
        volume_payload = self._rpc.playback_get("/volume")
        queue_payload = self._rpc.playback_get("/queue")
        repeat_payload = self._rpc.playback_get("/repeat-mode")
        shuffle_payload = self._rpc.playback_get("/shuffle-mode")
        autoplay_payload = self._rpc.playback_get("/autoplay")
        info = now_playing_payload.get("info", {}) if isinstance(now_playing_payload, dict) else {}
        return {
            "status": "ok",
            "source": "cider-rpc",
            "is_playing": self._extract_is_playing(is_playing_payload),
            "track": {
                "title": info.get("name"),
                "artist": info.get("artistName"),
                "album": info.get("albumName"),
                "track_id": info.get("playParams", {}).get("id"),
                "kind": info.get("playParams", {}).get("kind"),
                "is_library": info.get("playParams", {}).get("isLibrary"),
                "current_playback_time": info.get("currentPlaybackTime"),
                "remaining_time": info.get("remainingTime"),
                "duration_millis": info.get("durationInMillis"),
            },
            "volume": volume_payload.get("volume") if isinstance(volume_payload, dict) else None,
            "repeat_mode": repeat_payload.get("value") if isinstance(repeat_payload, dict) else None,
            "shuffle_mode": shuffle_payload.get("value") if isinstance(shuffle_payload, dict) else None,
            "autoplay": autoplay_payload.get("value") if isinstance(autoplay_payload, dict) else None,
            "queue_length": len(queue_payload) if isinstance(queue_payload, list) else 0,
        }

    def is_playing(self) -> dict[str, Any]:
        return {"status": "ok", "is_playing": self._extract_is_playing(self._rpc.playback_get("/is-playing"))}

    def play(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/play")}

    def pause(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/pause")}

    def playpause(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/playpause")}

    def stop(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/stop")}

    def next_track(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/next")}

    def previous_track(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/previous")}

    def seek(self, position_seconds: float) -> dict[str, Any]:
        if position_seconds < 0:
            raise CiderValidationError("position_seconds must be non-negative.")
        return {"status": "ok", "result": self._rpc.playback_post("/seek", {"position": position_seconds})}

    def get_volume(self) -> dict[str, Any]:
        return {"status": "ok", "volume": self._rpc.playback_get("/volume")}

    def set_volume(self, volume: int) -> dict[str, Any]:
        if volume < 0 or volume > 100:
            raise CiderValidationError("volume must be between 0 and 100.")
        return {"status": "ok", "result": self._rpc.playback_post("/volume", {"volume": volume})}

    def get_repeat_mode(self) -> dict[str, Any]:
        return {"status": "ok", "repeat_mode": self._rpc.playback_get("/repeat-mode")}

    def toggle_repeat(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/toggle-repeat")}

    def get_shuffle_mode(self) -> dict[str, Any]:
        return {"status": "ok", "shuffle_mode": self._rpc.playback_get("/shuffle-mode")}

    def toggle_shuffle(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/toggle-shuffle")}

    def get_autoplay(self) -> dict[str, Any]:
        return {"status": "ok", "autoplay": self._rpc.playback_get("/autoplay")}

    def toggle_autoplay(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/toggle-autoplay")}

    def get_queue(self) -> dict[str, Any]:
        payload = self._rpc.playback_get("/queue")
        items = payload if isinstance(payload, list) else []
        return {
            "status": "ok",
            "count": len(items),
            "items": [
                {
                    "index": index,
                    "track": _flatten_track_item(item),
                }
                for index, item in enumerate(items)
            ],
            "raw": payload,
        }

    def play_next(self, item: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/play-next", item)}

    def play_later(self, item: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/play-later", item)}

    def move_queue_item(self, from_index: int, to_index: int) -> dict[str, Any]:
        self._validate_index(from_index, "from_index")
        self._validate_index(to_index, "to_index")
        return {
            "status": "ok",
            "result": self._rpc.playback_post("/queue/move-to-position", {"fromIndex": from_index, "toIndex": to_index}),
        }

    def remove_queue_item(self, index: int) -> dict[str, Any]:
        self._validate_index(index, "index")
        return {"status": "ok", "result": self._rpc.playback_post("/queue/remove-by-index", {"index": index})}

    def clear_queue(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/queue/clear-queue")}

    def play_url(self, url: str) -> dict[str, Any]:
        if not url.strip():
            raise CiderValidationError("url cannot be empty.")
        return {"status": "ok", "result": self._rpc.playback_post("/play-url", {"url": url})}

    def play_item(self, item_id: str, *, kind: str = "songs", is_library: bool = False) -> dict[str, Any]:
        if not item_id.strip():
            raise CiderValidationError("item_id cannot be empty.")
        body = {"id": item_id, "type": kind, "isLibrary": is_library}
        return {"status": "ok", "result": self._rpc.playback_post("/play-item", body)}

    def play_item_href(self, href: str) -> dict[str, Any]:
        if not href.strip():
            raise CiderValidationError("href cannot be empty.")
        return {"status": "ok", "result": self._rpc.playback_post("/play-item-href", {"href": href})}

    def search_catalog(self, query: str, *, limit: int = 10, storefront: str = "us") -> dict[str, Any]:
        self._validate_search(query, limit)
        payload = self._rpc.search_catalog(query=query, limit=limit, storefront=storefront)
        items = payload.get("data", {}).get("results", {}).get("songs", {}).get("data", [])
        return {
            "status": "ok",
            "query": query,
            "storefront": storefront,
            "count": len(items) if isinstance(items, list) else 0,
            "tracks": [_flatten_track_item(item) for item in items] if isinstance(items, list) else [],
            "raw": payload,
        }

    def search_catalog_tracks(self, query: str, *, limit: int = 10, storefront: str = "us") -> dict[str, Any]:
        return self.search_catalog(query, limit=limit, storefront=storefront)

    def search(self, query: str, *, limit: int = 10, storefront: str = "us") -> dict[str, Any]:
        if self.default_search_source() == "library":
            result = self.search_library(query, limit=limit)
        else:
            result = self.search_catalog(query, limit=limit, storefront=storefront)
        result["search_source"] = self.default_search_source()
        return result

    def search_library(self, query: str, *, limit: int = 10, types: list[str] | None = None) -> dict[str, Any]:
        self._validate_search(query, limit)
        payload = self._rpc.search_library(query=query, limit=limit, types=types)
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
            "tracks": [_flatten_track_item(item) for item in tracks] if isinstance(tracks, list) else [],
            "playlists": [_flatten_playlist_item(item) for item in playlists] if isinstance(playlists, list) else [],
            "albums": [_flatten_album_item(item) for item in albums] if isinstance(albums, list) else [],
            "artists": [_flatten_artist_item(item) for item in artists] if isinstance(artists, list) else [],
            "raw": payload,
        }

    def search_library_tracks(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        self._validate_search(query, limit)
        payload = self._rpc.run_amapi_v3(f"/v1/me/library/search?term={_encode_query(query)}&types=library-songs&limit={limit}")
        items = payload.get("data", {}).get("results", {}).get("library-songs", {}).get("data", [])
        return {
            "status": "ok",
            "query": query,
            "count": len(items) if isinstance(items, list) else 0,
            "tracks": [_flatten_track_item(item) for item in items] if isinstance(items, list) else [],
            "raw": payload,
        }

    def list_library_playlists(self, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        self._validate_limit_offset(limit, offset)
        path = f"/v1/me/library/playlists?limit={limit}"
        if offset:
            path = f"{path}&offset={offset}"
        payload = self._rpc.run_amapi_v3(path)
        items = payload.get("data", {}).get("data", [])
        return {
            "status": "ok",
            "count": len(items) if isinstance(items, list) else 0,
            "next": payload.get("data", {}).get("next") if isinstance(payload, dict) else None,
            "playlists": [_flatten_playlist_item(item) for item in items] if isinstance(items, list) else [],
            "raw": payload,
        }

    def search_library_playlists(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        self._validate_search(query, limit)
        payload = self._rpc.run_amapi_v3(
            f"/v1/me/library/search?term={_encode_query(query)}&types=library-playlists&limit={limit}"
        )
        items = payload.get("data", {}).get("results", {}).get("library-playlists", {}).get("data", [])
        return {
            "status": "ok",
            "query": query,
            "count": len(items) if isinstance(items, list) else 0,
            "playlists": [_flatten_playlist_item(item) for item in items] if isinstance(items, list) else [],
            "raw": payload,
        }

    def get_library_playlist(self, playlist_id: str) -> dict[str, Any]:
        self._validate_playlist_id(playlist_id)
        payload = self._rpc.run_amapi_v3(f"/v1/me/library/playlists/{playlist_id}")
        items = payload.get("data", {}).get("data", [])
        playlist = items[0] if isinstance(items, list) and items else {}
        return {
            "status": "ok",
            "playlist": _flatten_playlist_item(playlist) if isinstance(playlist, dict) else None,
            "raw": payload,
        }

    def get_library_playlist_tracks(self, playlist_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        self._validate_playlist_id(playlist_id)
        self._validate_limit_offset(limit, offset)
        path = f"/v1/me/library/playlists/{playlist_id}/tracks?limit={limit}"
        if offset:
            path = f"{path}&offset={offset}"
        payload = self._rpc.run_amapi_v3(path)
        items = payload.get("data", {}).get("data", [])
        return {
            "status": "ok",
            "playlist_id": playlist_id,
            "count": len(items) if isinstance(items, list) else 0,
            "tracks": [_flatten_track_item(item) for item in items] if isinstance(items, list) else [],
            "raw": payload,
        }

    def create_playlist(
        self,
        *,
        name: str,
        description: str | None = None,
        track_refs: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise CiderValidationError("name cannot be empty.")
        raise CiderRpcError(
            "Current Cider RPC builds do not expose a working playlist creation endpoint through /api/v1/amapi/run-v3.",
            detail="Live probing showed run-v3 accepts path-only read requests and ignores mutation payloads.",
        )

    def add_playlist_tracks(self, playlist_id: str, *, track_refs: list[dict[str, str]]) -> dict[str, Any]:
        self._validate_playlist_id(playlist_id)
        if not track_refs:
            raise CiderValidationError("track_refs cannot be empty.")
        raise CiderRpcError(
            "Current Cider RPC builds do not expose a working playlist track mutation endpoint through /api/v1/amapi/run-v3.",
            detail="Live probing showed run-v3 accepts path-only read requests and ignores mutation payloads.",
        )

    def list_recently_played(self, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        self._validate_limit_offset(limit, offset)
        path = f"/v1/me/recent/played/tracks?limit={limit}"
        if offset:
            path = f"{path}&offset={offset}"
        payload = self._rpc.run_amapi_v3(path)
        items = payload.get("data", {}).get("data", [])
        return {
            "status": "ok",
            "count": len(items) if isinstance(items, list) else 0,
            "tracks": [_flatten_track_item(item) for item in items] if isinstance(items, list) else [],
            "raw": payload,
        }

    def play_search_result(
        self,
        *,
        query: str,
        source: str = "library",
        index: int = 0,
        storefront: str = "us",
    ) -> dict[str, Any]:
        self._validate_search(query, 25)
        self._validate_index(index, "index")
        if source == "library":
            results = self.search_library_tracks(query, limit=25)
            items = results["tracks"]
        elif source == "catalog":
            results = self.search_catalog_tracks(query, limit=25, storefront=storefront)
            items = results["tracks"]
        elif source == "default":
            resolved_source = self.default_search_source()
            return self.play_search_result(query=query, source=resolved_source, index=index, storefront=storefront)
        else:
            raise CiderValidationError("source must be 'library', 'catalog', or 'default'.")
        if index >= len(items):
            raise CiderValidationError("Search result index was out of range.")
        play_params = items[index].get("play_params", {})
        item_id = str(play_params.get("id", "")).strip()
        kind = str(play_params.get("kind", "songs")).strip() or "songs"
        is_library = bool(play_params.get("is_library", source == "library"))
        if not item_id:
            raise CiderValidationError("Selected search result did not include a playable id.")
        return self.play_item(item_id, kind=kind, is_library=is_library)

    def remember_preference(
        self,
        *,
        kind: str,
        value: str,
        category: str | None = None,
        weight: float = 1.0,
        note: str | None = None,
    ) -> dict[str, Any]:
        normalized_kind = kind.strip().lower()
        if normalized_kind not in {"like", "dislike"}:
            raise CiderValidationError("kind must be 'like' or 'dislike'.")
        if not value.strip():
            raise CiderValidationError("value cannot be empty.")
        preference = self._preferences.remember_preference(
            kind=normalized_kind,
            category=category.strip() if category else None,
            value=value.strip(),
            weight=weight,
            note=note.strip() if note else None,
        )
        return {"status": "ok", "preference": preference}

    def list_preferences(self) -> dict[str, Any]:
        preferences = self._preferences.list_preferences()
        return {"status": "ok", "count": len(preferences), "preferences": preferences}

    def forget_preference(self, preference_id: int) -> dict[str, Any]:
        removed = self._preferences.delete_preference(preference_id)
        if not removed:
            raise CiderValidationError(f"Preference {preference_id} was not found.")
        return {"status": "ok", "removed": True, "preference_id": preference_id}

    def recommend(self, *, query: str | None = None, limit: int = 5) -> dict[str, Any]:
        if limit <= 0 or limit > 50:
            raise CiderValidationError("limit must be between 1 and 50.")
        recommendation = self._recommender.recommend(service=self, query=query, limit=limit, prefer_library=None)
        return {"status": "ok", "recommendation": recommendation.as_dict()}

    def play_recommendation(self, *, query: str | None = None) -> dict[str, Any]:
        recommendation = self._recommender.recommend(service=self, query=query, limit=1, prefer_library=None)
        item = recommendation.items[0]
        play_params = item.get("play_params", {})
        item_id = str(play_params.get("id", "")).strip()
        kind = str(play_params.get("kind", "songs")).strip() or "songs"
        is_library = bool(play_params.get("is_library", recommendation.source == "library"))
        if not item_id:
            raise CiderValidationError("Recommended item did not include a playable id.")
        play_result = self.play_item(item_id, kind=kind, is_library=is_library)
        return {
            "status": "ok",
            "recommendation": recommendation.as_dict(),
            "playback": play_result,
        }

    def resolve_text_request(self, text: str) -> ResolvedAction:
        return self._resolver.resolve(text, self)

    def handle_text_request(self, text: str) -> dict[str, Any]:
        resolved = self.resolve_text_request(text)
        execution = self.run_action(resolved.action, resolved.parameters)
        return {
            "status": "ok",
            "input": text,
            "resolver": resolved.resolver,
            "resolved_action": {
                "action": resolved.action,
                "parameters": resolved.parameters,
            },
            "execution": execution,
        }

    def run_action(self, action: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        params = parameters or {}
        actions: dict[str, Any] = {
            "status": self.status,
            "get_now_playing": self.get_now_playing,
            "playback_snapshot": self.playback_snapshot,
            "is_playing": self.is_playing,
            "play": self.play,
            "pause": self.pause,
            "playpause": self.playpause,
            "stop": self.stop,
            "next_track": self.next_track,
            "previous_track": self.previous_track,
            "seek": lambda: self.seek(float(params["position_seconds"])),
            "get_volume": self.get_volume,
            "set_volume": lambda: self.set_volume(int(params["volume"])),
            "get_repeat_mode": self.get_repeat_mode,
            "toggle_repeat": self.toggle_repeat,
            "get_shuffle_mode": self.get_shuffle_mode,
            "toggle_shuffle": self.toggle_shuffle,
            "get_autoplay": self.get_autoplay,
            "toggle_autoplay": self.toggle_autoplay,
            "get_queue": self.get_queue,
            "play_next": lambda: self.play_next(dict(params["item"])),
            "play_later": lambda: self.play_later(dict(params["item"])),
            "move_queue_item": lambda: self.move_queue_item(int(params["from_index"]), int(params["to_index"])),
            "remove_queue_item": lambda: self.remove_queue_item(int(params["index"])),
            "clear_queue": self.clear_queue,
            "play_url": lambda: self.play_url(str(params["url"])),
            "play_item": lambda: self.play_item(
                str(params["item_id"]),
                kind=str(params.get("kind", "songs")),
                is_library=bool(params.get("is_library", False)),
            ),
            "play_item_href": lambda: self.play_item_href(str(params["href"])),
            "search_catalog": lambda: self.search_catalog(
                str(params["query"]),
                limit=int(params.get("limit", 10)),
                storefront=str(params.get("storefront", "us")),
            ),
            "search": lambda: self.search(
                str(params["query"]),
                limit=int(params.get("limit", 10)),
                storefront=str(params.get("storefront", "us")),
            ),
            "search_catalog_tracks": lambda: self.search_catalog_tracks(
                str(params["query"]),
                limit=int(params.get("limit", 10)),
                storefront=str(params.get("storefront", "us")),
            ),
            "search_library": lambda: self.search_library(
                str(params["query"]),
                limit=int(params.get("limit", 10)),
                types=list(params.get("types", [])) or None,
            ),
            "search_library_tracks": lambda: self.search_library_tracks(
                str(params["query"]),
                limit=int(params.get("limit", 10)),
            ),
            "list_library_playlists": lambda: self.list_library_playlists(
                limit=int(params.get("limit", 25)),
                offset=int(params.get("offset", 0)),
            ),
            "search_library_playlists": lambda: self.search_library_playlists(
                str(params["query"]),
                limit=int(params.get("limit", 10)),
            ),
            "get_library_playlist": lambda: self.get_library_playlist(str(params["playlist_id"])),
            "get_library_playlist_tracks": lambda: self.get_library_playlist_tracks(
                str(params["playlist_id"]),
                limit=int(params.get("limit", 100)),
                offset=int(params.get("offset", 0)),
            ),
            "create_playlist": lambda: self.create_playlist(
                name=str(params["name"]),
                description=str(params["description"]) if params.get("description") is not None else None,
                track_refs=list(params.get("track_refs", [])) or None,
            ),
            "add_playlist_tracks": lambda: self.add_playlist_tracks(
                str(params["playlist_id"]),
                track_refs=list(params["track_refs"]),
            ),
            "list_recently_played": lambda: self.list_recently_played(
                limit=int(params.get("limit", 25)),
                offset=int(params.get("offset", 0)),
            ),
            "play_search_result": lambda: self.play_search_result(
                query=str(params["query"]),
                source=str(params.get("source", "default")),
                index=int(params.get("index", 0)),
                storefront=str(params.get("storefront", "us")),
            ),
            "remember_preference": lambda: self.remember_preference(
                kind=str(params["kind"]),
                value=str(params["value"]),
                category=str(params["category"]) if params.get("category") is not None else None,
                weight=float(params.get("weight", 1.0)),
                note=str(params["note"]) if params.get("note") is not None else None,
            ),
            "list_preferences": self.list_preferences,
            "forget_preference": lambda: self.forget_preference(int(params["preference_id"])),
            "recommend": lambda: self.recommend(
                query=str(params["query"]) if params.get("query") is not None else None,
                limit=int(params.get("limit", 5)),
            ),
            "play_recommendation": lambda: self.play_recommendation(
                query=str(params["query"]) if params.get("query") is not None else None,
            ),
        }
        if action not in actions:
            raise CiderValidationError(f"Unsupported action: {action}")
        result = actions[action]()
        return {
            "action": action,
            "result": result,
            "request_id": str(uuid.uuid4()),
        }

    def _extract_is_playing(self, payload: Any) -> bool | None:
        if isinstance(payload, bool):
            return payload
        if isinstance(payload, dict):
            if "value" in payload:
                return bool(payload["value"])
            if "is_playing" in payload:
                return bool(payload["is_playing"])
        return None

    def _validate_search(self, query: str, limit: int) -> None:
        if not query.strip():
            raise CiderValidationError("query cannot be empty.")
        if limit <= 0 or limit > 100:
            raise CiderValidationError("limit must be between 1 and 100.")

    def _validate_index(self, value: int, name: str) -> None:
        if value < 0:
            raise CiderValidationError(f"{name} must be non-negative.")

    def _validate_limit_offset(self, limit: int, offset: int) -> None:
        if limit <= 0 or limit > 100:
            raise CiderValidationError("limit must be between 1 and 100.")
        if offset < 0:
            raise CiderValidationError("offset must be non-negative.")

    def _validate_playlist_id(self, playlist_id: str) -> None:
        if not playlist_id.strip():
            raise CiderValidationError("playlist_id cannot be empty.")

    def _normalize_track_ref(self, ref: dict[str, str]) -> dict[str, str]:
        item_id = str(ref.get("id", "")).strip()
        item_type = str(ref.get("type", "songs")).strip() or "songs"
        if not item_id:
            raise CiderValidationError("Each track ref must include a non-empty id.")
        return {"id": item_id, "type": item_type}
