"""Domain services for cider_agent."""

from __future__ import annotations

from datetime import datetime
import logging
import uuid
from dataclasses import dataclass
import threading
import time
from typing import Any
from urllib.parse import quote
import re

from .config import Settings
from .errors import CiderAgentError, CiderRpcError, CiderValidationError, TextRequestExecutionError
from .resolver import ResolvedAction, Resolver, SessionPlan, build_resolver
from .rpc import CiderRpcClient
from .storage import PreferenceStore


LOGGER = logging.getLogger(__name__)


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


def _normalize_match_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = value.casefold()
    normalized = normalized.replace("p!nk", "pink")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _clean_id(value: Any) -> str:
    if value is None:
        return ""
    cleaned = str(value).strip()
    return "" if cleaned.lower() == "none" else cleaned


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

    SESSION_REFILL_INTERVAL_SECONDS = 5.0
    SESSION_ADVANCE_COOLDOWN_SECONDS = 5.0

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
        self._session_worker_thread: threading.Thread | None = None
        self._session_worker_stop = threading.Event()
        self._session_worker_lock = threading.Lock()
        self._session_runtime_lock = threading.Lock()
        self._session_runtime: dict[int, dict[str, Any]] = {}
        self._session_advance_lock = threading.Lock()

    def close(self) -> None:
        self.stop_background_session_worker()
        self._rpc.close()
        close = getattr(self._resolver, "close", None)
        if callable(close):
            close()

    def default_search_source(self) -> str:
        return self._settings.default_search_source

    def response_detail(self) -> str:
        return self._settings.response_detail

    def session_recent_tracks_limit(self) -> int:
        return self._settings.session_recent_tracks_limit

    def current_timestamp(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def start_background_session_worker(self) -> None:
        with self._session_worker_lock:
            if self._session_worker_thread is not None and self._session_worker_thread.is_alive():
                return
            self._session_worker_stop.clear()
            self._session_worker_thread = threading.Thread(
                target=self._session_worker_loop,
                name="cider-agent-session-worker",
                daemon=True,
            )
            self._session_worker_thread.start()

    def stop_background_session_worker(self) -> None:
        with self._session_worker_lock:
            self._session_worker_stop.set()
            thread = self._session_worker_thread
            self._session_worker_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def status(self) -> dict[str, Any]:
        payload = {
            "status": "ok",
            "source": "cider-agent",
            "config": self._settings.sanitized(),
            "playback": self.playback_snapshot(),
            "preferences_count": len(self._preferences.list_preferences()),
        }
        if self.response_detail() == "compact":
            queue = self.get_queue()
            session = self._preferences.get_active_session()
            return {
                "status": "ok",
                "source": "cider-agent",
                "playback": {
                    "is_playing": payload["playback"].get("is_playing"),
                    "track": self._compact_track(payload["playback"].get("track", {})),
                    "volume": payload["playback"].get("volume"),
                    "queue_length": queue.get("count", 0),
                },
                "queue": {
                    "count": queue.get("count", 0),
                    "tracks": [
                        self._compact_track(item["track"])
                        for item in queue.get("items", [])[:5]
                        if isinstance(item, dict) and isinstance(item.get("track"), dict)
                    ],
                },
                "session": {
                    "id": session.get("id"),
                    "is_active": session.get("is_active"),
                    "mode": session.get("mode"),
                }
                if isinstance(session, dict)
                else None,
                "preferences_count": payload["preferences_count"],
            }
        return payload

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
        session = self._preferences.get_active_session()
        if session is not None:
            self._set_session_runtime(session["id"], suspended=False)
            snapshot = self.playback_snapshot()
            if snapshot.get("is_playing"):
                return {"status": "ok", "result": self._rpc.playback_post("/play")}
            if _clean_id(snapshot.get("track", {}).get("track_id")):
                return {"status": "ok", "result": self._rpc.playback_post("/play")}
            return self._play_session_track(session, selection_strategy="adaptive-session-resume")
        return {"status": "ok", "result": self._rpc.playback_post("/play")}

    def pause(self) -> dict[str, Any]:
        session = self._preferences.get_active_session()
        if session is not None:
            self._set_session_runtime(session["id"], suspended=True)
        return {"status": "ok", "result": self._rpc.playback_post("/pause")}

    def playpause(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/playpause")}

    def stop(self) -> dict[str, Any]:
        stopped = self._preferences.stop_active_session()
        if stopped is not None:
            self._clear_session_runtime(stopped["id"])
        return {"status": "ok", "result": self._rpc.playback_post("/stop")}

    def next_track(self) -> dict[str, Any]:
        session = self._preferences.get_active_session()
        if session is not None:
            self._set_session_runtime(session["id"], suspended=False)
            return self._play_session_track(session, selection_strategy="adaptive-session-skip")
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
        normalized_volume = volume / 100.0
        return {
            "status": "ok",
            "requested_volume": volume,
            "normalized_volume": normalized_volume,
            "result": self._rpc.playback_post("/volume", {"volume": normalized_volume}),
        }

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

    def session_status(self, *, include_recent_tracks: bool = True, compact: bool | None = None) -> dict[str, Any]:
        session = self._preferences.get_active_session()
        if session is None:
            payload = {"status": "ok", "session": None}
            return self._compact_output(payload) if compact is not False and self.response_detail() == "compact" else payload
        payload = {"status": "ok", "session": session}
        if include_recent_tracks:
            payload["recent_tracks"] = self.recent_session_tracks(limit=self.session_recent_tracks_limit())
        if compact is False:
            return payload
        if compact is True or self.response_detail() == "compact":
            return self._compact_output(payload)
        return payload

    def recent_session_tracks(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        session = self._preferences.get_active_session()
        if session is None:
            return []
        return self._preferences.list_session_tracks(session["id"], limit=limit or self.session_recent_tracks_limit())

    def stop_session(self) -> dict[str, Any]:
        stopped = self._preferences.stop_active_session()
        if stopped is not None:
            self._clear_session_runtime(stopped["id"])
        return {"status": "ok", "stopped": stopped is not None, "session": stopped}

    def play_session(self, request: str) -> dict[str, Any]:
        if not request.strip():
            raise CiderValidationError("request cannot be empty.")
        session = self._preferences.start_session(request_text=request.strip())
        self._clear_all_session_runtime()
        self._set_session_runtime(session["id"], suspended=False)
        result = self._play_session_track(session, selection_strategy="adaptive-session-start")
        return {
            "status": "ok",
            "mode": "adaptive-session",
            "session": self._preferences.get_session(session["id"]),
            "result": result,
        }

    def steer_session(self, request: str) -> dict[str, Any]:
        if not request.strip():
            raise CiderValidationError("request cannot be empty.")
        session = self._preferences.get_active_session()
        if session is None:
            raise CiderValidationError("No active session is running.")
        session = self._preferences.add_session_steering(session["id"], request.strip())
        self._set_session_runtime(session["id"], suspended=False)
        result = self._play_session_track(session, selection_strategy="adaptive-session-steer")
        return {
            "status": "ok",
            "mode": "adaptive-session",
            "session": self._preferences.get_session(session["id"]),
            "result": result,
        }

    def refill_active_session(self) -> dict[str, Any]:
        session = self._preferences.get_active_session()
        if session is None:
            raise CiderValidationError("No active session is running.")
        self._set_session_runtime(session["id"], suspended=False)
        result = self._play_session_track(session, selection_strategy="adaptive-session-manual-advance")
        return {
            "status": "ok",
            "mode": "adaptive-session",
            "session": self._preferences.get_session(session["id"]),
            "result": result,
        }

    def play_candidate_match(
        self,
        *,
        candidate_tracks: list[dict[str, str]] | None = None,
        candidate_artists: list[str] | None = None,
        candidate_queries: list[str] | None = None,
        storefront: str = "us",
    ) -> dict[str, Any]:
        track_candidates = candidate_tracks or []
        artist_candidates = candidate_artists or []
        query_candidates = candidate_queries or []

        for candidate in track_candidates:
            title = str(candidate.get("title", "")).strip()
            artist = str(candidate.get("artist", "")).strip()
            if not title or not artist:
                continue
            search = self.search_catalog_tracks(f"{artist} {title}", limit=10, storefront=storefront)
            match = self._best_track_match(search["tracks"], title=title, artist=artist)
            if match is not None:
                playback = self._play_flattened_track(match, is_library_default=False)
                return {
                    "status": "ok",
                    "selection_strategy": "candidate_track_exactish_match",
                    "selected_track": match,
                    "playback": playback,
                }

        for artist in artist_candidates:
            artist_name = str(artist).strip()
            if not artist_name:
                continue
            search = self.search_catalog_tracks(artist_name, limit=10, storefront=storefront)
            match = self._best_artist_track_match(search["tracks"], artist=artist_name)
            if match is not None:
                playback = self._play_flattened_track(match, is_library_default=False)
                return {
                    "status": "ok",
                    "selection_strategy": "candidate_artist_track_match",
                    "selected_track": match,
                    "playback": playback,
                }

        for query in query_candidates:
            query_text = str(query).strip()
            if not query_text:
                continue
            result = self.play_search_result(query=query_text, source="default", index=0, storefront=storefront)
            return {
                "status": "ok",
                "selection_strategy": "candidate_query_fallback",
                "selected_query": query_text,
                "playback": result,
            }

        raise CiderValidationError("No playable candidate match could be resolved.")

    def resolve_text_request(self, text: str) -> ResolvedAction:
        return self._resolver.resolve(text, self)

    def handle_text_request(self, text: str) -> dict[str, Any]:
        resolved = self.resolve_text_request(text)
        response = {
            "status": "ok",
            "input": text,
            "resolver": resolved.resolver,
            "resolved_action": self._compact_resolved_action(resolved.action, resolved.parameters),
        }
        if resolved.reasoning:
            response["reasoning"] = resolved.reasoning
        if resolved.raw_content:
            response["resolver_raw_content"] = resolved.raw_content
        if resolved.raw is not None and self._settings.resolver_include_raw_output:
            response["resolver_raw_action"] = resolved.raw
        try:
            execution = self.run_action(resolved.action, resolved.parameters)
        except CiderAgentError as exc:
            response["status"] = "error"
            response["error"] = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }
            raise TextRequestExecutionError(str(exc), response) from exc
        response["execution"] = execution
        return response

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
            "set_volume": lambda: self.set_volume(self._coerce_volume_param(params)),
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
            "play_session": lambda: self.play_session(str(params["request"])),
            "steer_session": lambda: self.steer_session(str(params["request"])),
            "session_status": self.session_status,
            "stop_session": self.stop_session,
            "refill_session": self.refill_active_session,
            "search_catalog": lambda: self.search_catalog(
                str(params["query"]),
                limit=int(params.get("limit", 10)),
                storefront=str(params.get("storefront", "us")),
            ),
            "play_candidate_match": lambda: self.play_candidate_match(
                candidate_tracks=list(params.get("candidate_tracks", [])) or None,
                candidate_artists=list(params.get("candidate_artists", [])) or None,
                candidate_queries=list(params.get("candidate_queries", params.get("candidate_query", []))) or None,
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
        try:
            result = actions[action]()
        except (KeyError, TypeError, ValueError) as exc:
            raise CiderValidationError(f"Invalid parameters for action {action}: {exc}") from exc
        payload = {
            "action": action,
            "result": self._finalize_output(result),
        }
        if self.response_detail() == "debug":
            payload["request_id"] = str(uuid.uuid4())
        return payload

    def _session_worker_loop(self) -> None:
        while not self._session_worker_stop.wait(self.SESSION_REFILL_INTERVAL_SECONDS):
            try:
                session = self._preferences.get_active_session()
                if session is None:
                    continue
                playback = self.playback_snapshot()
                self._record_current_track_for_session(session, playback=playback)
                if self._should_advance_session(session, playback):
                    self._play_session_track(session, selection_strategy="adaptive-session-auto-advance")
            except Exception:
                LOGGER.exception("Adaptive session worker failed during refill loop.")

    def _play_session_track(self, session: dict[str, Any], *, selection_strategy: str) -> dict[str, Any]:
        with self._session_advance_lock:
            now = time.monotonic()
            self._set_session_runtime(
                session["id"],
                suspended=False,
                advance_in_progress=True,
                last_advance_at=now,
            )
            try:
                playback = self.playback_snapshot()
                self._record_current_track_for_session(session, playback=playback)
                plan = self._plan_session_tracks(session, count=3)
                tracks = self._collect_session_tracks(plan, limit=3)
                if not tracks:
                    raise CiderValidationError("No playable candidate match could be resolved.")
                lead_track = tracks[0]
                playback_result = self._play_flattened_track(lead_track, is_library_default=False)
                self._preferences.add_session_track(session["id"], lead_track)
                self._preferences.touch_session_refill(session["id"])
                self._set_session_runtime(
                    session["id"],
                    suspended=False,
                    advance_in_progress=False,
                    last_advance_at=time.monotonic(),
                    last_selected_track_id=_clean_id(lead_track.get("play_params", {}).get("id")),
                )
                return {
                    "status": "ok",
                    "selection_strategy": selection_strategy,
                    "playback": playback_result,
                    "enqueued_count": 0,
                    "tracks": [lead_track],
                    "plan": {
                        "candidate_tracks": plan.candidate_tracks,
                        "candidate_artists": plan.candidate_artists,
                        "candidate_queries": plan.candidate_queries,
                    },
                }
            except Exception:
                self._set_session_runtime(session["id"], advance_in_progress=False)
                raise

    def _plan_session_tracks(self, session: dict[str, Any], *, count: int) -> SessionPlan:
        planner = getattr(self._resolver, "plan_session", None)
        if not callable(planner):
            raise CiderValidationError("The configured resolver does not support adaptive play sessions.")
        request = self._session_effective_request(session)
        return planner(request, self, session, count)

    def _session_effective_request(self, session: dict[str, Any]) -> str:
        steering = session.get("steering_history", [])
        if not steering:
            return str(session.get("request_text", "")).strip()
        steering_text = " ".join(str(item).strip() for item in steering if str(item).strip())
        if not steering_text:
            return str(session.get("request_text", "")).strip()
        return f"{session.get('request_text', '').strip()} Current steering: {steering_text}".strip()

    def _collect_session_tracks(self, plan: SessionPlan, *, limit: int) -> list[dict[str, Any]]:
        excluded_ids = self._session_excluded_track_ids()
        chosen: list[dict[str, Any]] = []
        seen_ids = set(excluded_ids)

        for candidate in plan.candidate_tracks:
            if len(chosen) >= limit:
                break
            title = str(candidate.get("title", "")).strip()
            artist = str(candidate.get("artist", "")).strip()
            if not title or not artist:
                continue
            search = self.search_catalog_tracks(f"{artist} {title}", limit=10)
            match = self._best_track_match(search["tracks"], title=title, artist=artist)
            if match is None:
                continue
            match_id = str(match.get("id", "")).strip()
            if match_id and match_id in seen_ids:
                continue
            chosen.append(match)
            if match_id:
                seen_ids.add(match_id)

        for artist in plan.candidate_artists:
            if len(chosen) >= limit:
                break
            search = self.search_catalog_tracks(str(artist), limit=15)
            for match in self._best_artist_track_matches(search["tracks"], artist=str(artist), limit=limit - len(chosen)):
                match_id = str(match.get("id", "")).strip()
                if match_id and match_id in seen_ids:
                    continue
                chosen.append(match)
                if match_id:
                    seen_ids.add(match_id)
                if len(chosen) >= limit:
                    break

        for query in plan.candidate_queries:
            if len(chosen) >= limit:
                break
            results = self.search(str(query), limit=25)
            for track in results.get("tracks", []):
                match_id = str(track.get("id", "")).strip()
                if match_id and match_id in seen_ids:
                    continue
                chosen.append(track)
                if match_id:
                    seen_ids.add(match_id)
                if len(chosen) >= limit:
                    break

        return chosen

    def _session_excluded_track_ids(self) -> set[str]:
        excluded: set[str] = set()
        current = self.get_now_playing().get("track", {})
        current_id = _clean_id(current.get("play_params", {}).get("id"))
        if current_id:
            excluded.add(current_id)
        for track in self.recent_session_tracks(limit=self.session_recent_tracks_limit()):
            track_id = _clean_id(track.get("track_id"))
            if track_id:
                excluded.add(track_id)
        return excluded

    def _record_current_track_for_session(self, session: dict[str, Any], *, playback: dict[str, Any] | None = None) -> None:
        current = (playback or self.playback_snapshot()).get("track", {})
        current_id = _clean_id(current.get("track_id"))
        if not current_id:
            return
        recent = self._preferences.list_session_tracks(session["id"], limit=1)
        if recent and _clean_id(recent[0].get("track_id")) == current_id:
            return
        self._preferences.add_session_track(
            session["id"],
            {
                "id": current_id,
                "title": current.get("title"),
                "artist": current.get("artist"),
                "album": current.get("album"),
                "href": None,
            },
        )

    def _should_advance_session(self, session: dict[str, Any], playback: dict[str, Any]) -> bool:
        runtime = self._get_session_runtime(session["id"])
        if runtime.get("suspended"):
            return False
        if runtime.get("advance_in_progress"):
            return False
        if playback.get("is_playing"):
            return False
        now = time.monotonic()
        last_advance_at = runtime.get("last_advance_at", 0.0)
        if now - float(last_advance_at) < self.SESSION_ADVANCE_COOLDOWN_SECONDS:
            return False
        return True

    def _get_session_runtime(self, session_id: int) -> dict[str, Any]:
        with self._session_runtime_lock:
            return dict(self._session_runtime.get(session_id, {}))

    def _set_session_runtime(self, session_id: int, **updates: Any) -> None:
        with self._session_runtime_lock:
            runtime = dict(self._session_runtime.get(session_id, {}))
            runtime.update(updates)
            self._session_runtime[session_id] = runtime

    def _clear_session_runtime(self, session_id: int) -> None:
        with self._session_runtime_lock:
            self._session_runtime.pop(session_id, None)

    def _clear_all_session_runtime(self) -> None:
        with self._session_runtime_lock:
            self._session_runtime.clear()

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

    def _coerce_volume_param(self, params: dict[str, Any]) -> int:
        raw = None
        for key in ("volume", "value", "level", "percent"):
            if key in params:
                raw = params[key]
                break
        if raw is None:
            raise CiderValidationError("set_volume requires a volume parameter.")
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                raise CiderValidationError("volume cannot be empty.")
            numeric = float(raw)
            if "." in raw and 0.0 <= numeric <= 1.0:
                return round(numeric * 100)
            return round(numeric)
        if isinstance(raw, (int, float)):
            numeric = float(raw)
            if isinstance(raw, float) and 0.0 <= numeric <= 1.0:
                return round(numeric * 100)
            return round(numeric)
        raise CiderValidationError(f"volume must be numeric, got {type(raw).__name__}.")

    def _normalize_track_ref(self, ref: dict[str, str]) -> dict[str, str]:
        item_id = str(ref.get("id", "")).strip()
        item_type = str(ref.get("type", "songs")).strip() or "songs"
        if not item_id:
            raise CiderValidationError("Each track ref must include a non-empty id.")
        return {"id": item_id, "type": item_type}

    def _best_track_match(self, tracks: list[dict[str, Any]], *, title: str, artist: str) -> dict[str, Any] | None:
        title_norm = _normalize_match_text(title)
        artist_norm = _normalize_match_text(artist)
        for track in tracks:
            if _normalize_match_text(track.get("title")) == title_norm and _normalize_match_text(track.get("artist")) == artist_norm:
                return track
        for track in tracks:
            track_title = _normalize_match_text(track.get("title"))
            track_artist = _normalize_match_text(track.get("artist"))
            if title_norm in track_title and artist_norm == track_artist:
                return track
        return None

    def _best_artist_track_match(self, tracks: list[dict[str, Any]], *, artist: str) -> dict[str, Any] | None:
        matches = self._best_artist_track_matches(tracks, artist=artist, limit=1)
        return matches[0] if matches else None

    def _best_artist_track_matches(self, tracks: list[dict[str, Any]], *, artist: str, limit: int) -> list[dict[str, Any]]:
        artist_norm = _normalize_match_text(artist)
        exact_artist_tracks = [track for track in tracks if _normalize_match_text(track.get("artist")) == artist_norm]
        if not exact_artist_tracks:
            return []
        scored = sorted(exact_artist_tracks, key=self._artist_track_score, reverse=True)
        return scored[:limit]

    def _artist_track_score(self, track: dict[str, Any]) -> tuple[int, int]:
        album = _normalize_match_text(track.get("album"))
        title = _normalize_match_text(track.get("title"))
        album_score = 0
        if "greatest hits" in album:
            album_score += 5
        if "essential" in album:
            album_score += 4
        if "so far" in album:
            album_score += 3
        if "the truth about love" in album:
            album_score += 2
        title_penalty = 0
        if title == "pink" or title == "pink!":
            title_penalty -= 3
        return (album_score, title_penalty)

    def _play_flattened_track(self, track: dict[str, Any], *, is_library_default: bool) -> dict[str, Any]:
        play_params = track.get("play_params", {})
        item_id = str(play_params.get("id", "")).strip()
        kind = str(play_params.get("kind", "songs")).strip() or "songs"
        is_library = bool(play_params.get("is_library", is_library_default))
        if not item_id:
            raise CiderValidationError("Resolved track did not include a playable id.")
        return self.play_item(item_id, kind=kind, is_library=is_library)

    def _enqueue_flattened_track(self, track: dict[str, Any]) -> dict[str, Any]:
        play_params = track.get("play_params", {})
        item_id = str(play_params.get("id", "")).strip()
        kind = str(play_params.get("kind", "songs")).strip() or "songs"
        is_library = bool(play_params.get("is_library", False))
        if not item_id:
            raise CiderValidationError("Resolved track did not include a playable id.")
        return self.play_later({"id": item_id, "type": kind, "isLibrary": is_library})

    def _finalize_output(self, payload: Any) -> Any:
        if self.response_detail() == "debug":
            return payload
        return self._compact_output(payload)

    def _compact_resolved_action(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if self.response_detail() == "debug":
            return {
                "action": action,
                "parameters": parameters,
            }
        return {"action": action}

    def _compact_output(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._compact_output(item) for item in value]
        if not isinstance(value, dict):
            return value

        if self._looks_like_session_execution(value):
            return self._compact_session_execution(value)
        if self._looks_like_session_status(value):
            return self._compact_session_status(value)
        if self._looks_like_play_candidate_result(value):
            return self._compact_play_candidate_result(value)

        if self._looks_like_track(value):
            return self._compact_track(value)
        if self._looks_like_playlist(value):
            return self._compact_playlist(value)
        if self._looks_like_album(value):
            return self._compact_album(value)
        if self._looks_like_artist(value):
            return self._compact_artist(value)

        compact: dict[str, Any] = {}
        for key, item in value.items():
            if key == "raw":
                continue
            if key in {"tracks", "candidate_tracks"} and isinstance(item, list):
                compact[key] = [self._compact_track(track) if isinstance(track, dict) else track for track in item]
                continue
            if key in {"selected_track", "track"} and isinstance(item, dict):
                compact[key] = self._compact_track(item)
                continue
            if key == "items" and isinstance(item, list):
                compact[key] = [self._compact_queue_item(queue_item) for queue_item in item]
                continue
            if key == "playlists" and isinstance(item, list):
                compact[key] = [self._compact_playlist(playlist) if isinstance(playlist, dict) else playlist for playlist in item]
                continue
            if key == "playlist" and isinstance(item, dict):
                compact[key] = self._compact_playlist(item)
                continue
            if key == "albums" and isinstance(item, list):
                compact[key] = [self._compact_album(album) if isinstance(album, dict) else album for album in item]
                continue
            if key == "artists" and isinstance(item, list):
                compact[key] = [self._compact_artist(artist) if isinstance(artist, dict) else artist for artist in item]
                continue
            compact[key] = self._compact_output(item)
        return compact

    def _compact_track(self, track: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key in (
            "title",
            "artist",
            "album",
            "duration_millis",
        ):
            if key in track:
                compact[key] = track[key]
        return compact

    def _compact_playlist(self, playlist: dict[str, Any]) -> dict[str, Any]:
        return {
            key: playlist[key]
            for key in ("id", "type", "href", "name", "description", "can_edit", "is_public")
            if key in playlist
        }

    def _compact_album(self, album: dict[str, Any]) -> dict[str, Any]:
        return {key: album[key] for key in ("id", "type", "href", "name", "artist", "track_count") if key in album}

    def _compact_artist(self, artist: dict[str, Any]) -> dict[str, Any]:
        return {key: artist[key] for key in ("id", "type", "href", "name") if key in artist}

    def _compact_queue_item(self, item: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        if "index" in item:
            compact["index"] = item["index"]
        if isinstance(item.get("track"), dict):
            compact["track"] = self._compact_track(item["track"])
        return compact

    def _compact_session_execution(self, value: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {
            "status": value.get("status"),
            "mode": value.get("mode"),
        }
        session = value.get("session")
        if isinstance(session, dict):
            compact["session"] = {
                "id": session.get("id"),
                "is_active": session.get("is_active"),
            }
        result = value.get("result")
        if isinstance(result, dict):
            compact["result"] = self._compact_session_refill_result(result)
        return compact

    def _compact_session_refill_result(self, value: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {
            "status": value.get("status"),
            "selection_strategy": value.get("selection_strategy"),
            "enqueued_count": value.get("enqueued_count", 0),
        }
        playback = value.get("playback")
        if isinstance(playback, dict):
            compact["playback"] = self._compact_output(playback)
        tracks = value.get("tracks")
        if isinstance(tracks, list) and tracks:
            compact["primary_track"] = self._compact_track(tracks[0]) if isinstance(tracks[0], dict) else tracks[0]
        return compact

    def _compact_session_status(self, value: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {
            "status": value.get("status"),
            "session": None,
        }
        session = value.get("session")
        if isinstance(session, dict):
            compact["session"] = {
                "id": session.get("id"),
                "is_active": session.get("is_active"),
                "mode": session.get("mode"),
            }
        recent_tracks = value.get("recent_tracks")
        if isinstance(recent_tracks, list):
            compact["recent_tracks"] = [self._compact_recent_track(track) for track in recent_tracks[:5] if isinstance(track, dict)]
        return compact

    def _compact_recent_track(self, track: dict[str, Any]) -> dict[str, Any]:
        return {
            key: track.get(key)
            for key in ("track_id", "title", "artist", "album")
            if key in track
        }

    def _compact_play_candidate_result(self, value: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {
            "status": value.get("status"),
            "selection_strategy": value.get("selection_strategy"),
        }
        if "selected_query" in value:
            compact["selected_query"] = value.get("selected_query")
        if isinstance(value.get("selected_track"), dict):
            compact["selected_track"] = self._compact_track(value["selected_track"])
        if isinstance(value.get("playback"), dict):
            compact["playback"] = self._compact_output(value["playback"])
        return compact

    def _looks_like_track(self, value: dict[str, Any]) -> bool:
        return "title" in value and ("artist" in value or "play_params" in value or "track_id" in value)

    def _looks_like_playlist(self, value: dict[str, Any]) -> bool:
        return "name" in value and ("can_edit" in value or "is_public" in value) and "title" not in value

    def _looks_like_album(self, value: dict[str, Any]) -> bool:
        return "name" in value and "track_count" in value and "title" not in value

    def _looks_like_artist(self, value: dict[str, Any]) -> bool:
        return "name" in value and "track_count" not in value and "can_edit" not in value and "title" not in value and "artist" not in value

    def _looks_like_session_execution(self, value: dict[str, Any]) -> bool:
        return "mode" in value and "session" in value and "result" in value

    def _looks_like_session_status(self, value: dict[str, Any]) -> bool:
        return "session" in value and "recent_tracks" in value and "mode" not in value

    def _looks_like_play_candidate_result(self, value: dict[str, Any]) -> bool:
        return "selection_strategy" in value and ("selected_track" in value or "selected_query" in value)
