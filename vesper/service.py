"""Domain services for Vesper."""

from __future__ import annotations

from datetime import datetime
from functools import wraps
import logging
import json
import random
import threading
import time
from typing import Any
from urllib.parse import quote
import re

from .config import Settings
from .historian import (
    HistorianSink,
    NullHistorianSink,
    build_event,
    current_operation,
    operation_context,
    replace_operation,
    reset_operation,
)
from .action_registry import get_action_definition, list_action_definitions, list_public_action_definitions
from .errors import CiderAgentError, CiderRpcError, CiderValidationError, TextRequestExecutionError
from .results import EngineActionResult, TextRequestResult
from .resolver import (
    ResolvedAction,
    Resolver,
    SessionQueryPlan,
    SessionSearchSource,
    SessionTrackSelection,
    build_resolver,
)
from .rpc import CiderRpcClient
from .storage import PreferenceStore
from .output import (
    compact_output,
    compact_resolved_action,
    compact_track,
    finalize_output,
    summarize_execution,
)
from .validation import (
    validate_index,
    validate_limit_offset,
    validate_playlist_id,
    validate_search,
)


LOGGER = logging.getLogger(__name__)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 2)


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


def _normalize_title_match_text(value: str | None) -> str:
    if value is None:
        return ""
    simplified = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", value)
    simplified = re.sub(
        r"\s+-\s+(version|ver|movie ver|movie version|instrumental|cover|edit|mix|remaster(?:ed)?)\b.*$",
        "",
        simplified,
        flags=re.IGNORECASE,
    )
    return _normalize_match_text(simplified)


def _clean_id(value: Any) -> str:
    if value is None:
        return ""
    cleaned = str(value).strip()
    return "" if cleaned.lower() == "none" else cleaned


def _historian_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.operation():
            return method(self, *args, **kwargs)

    return wrapped


class CiderAgentService:
    """High-level operations for the Cider agent."""

    SESSION_REFILL_INTERVAL_SECONDS = 5.0
    SESSION_ADVANCE_COOLDOWN_SECONDS = 5.0
    TRACK_SELECTION_POOL_SIZE = 3
    SESSION_SEARCH_RESULT_LIMIT = 100
    SESSION_SEARCH_PAGE_LIMIT = 50
    SESSION_SELECTION_WINDOW_SIZE = 6
    PREFERENCE_SEED_SEARCH_LIMIT = 6
    PREFERENCE_SEED_ARTIST_CAP = 4
    PREFERENCE_SEED_POOL_QUERY = "__preference_seeded__"
    PREFERENCE_SEED_SOURCE = SessionSearchSource(kind="preference", term=PREFERENCE_SEED_POOL_QUERY)
    SESSION_STOREFRONT = "us"

    def __init__(
        self,
        settings: Settings,
        *,
        rpc_client: CiderRpcClient | None = None,
        preference_store: PreferenceStore | None = None,
        resolver: Resolver | None = None,
        historian_sink: HistorianSink | None = None,
    ) -> None:
        self._settings = settings
        self._historian = historian_sink or NullHistorianSink()
        self._rpc = rpc_client or CiderRpcClient(settings, failure_callback=self._record_rpc_failure)
        set_failure_callback = getattr(self._rpc, "set_failure_callback", None)
        if callable(set_failure_callback):
            set_failure_callback(self._record_rpc_failure)
        self._preferences = preference_store or PreferenceStore(settings.database_path)
        self._resolver = resolver or build_resolver(settings)
        self._resolver_debug_log_lock = threading.Lock()
        self._resolver_debug_episode_depth = 0
        self._random = random.SystemRandom()
        self._genre_cache: dict[str, dict[str, str]] = {}
        # The adaptive-session engine owns the session runtime, the background
        # refill worker, and the search/planning/pool machinery. Imported here
        # (rather than at module top) so vesper.session can import the pure
        # helpers this module defines without a circular import.
        from .session import SessionEngine

        self._session = SessionEngine(
            self,
            rpc=self._rpc,
            preferences=self._preferences,
            resolver=self._resolver,
            settings=self._settings,
        )
        self.reconcile_session_runtime()

    @property
    def _session_runtime(self) -> dict[int, dict[str, Any]]:
        # Exposed for tests/debugging; the authoritative runtime lives on the engine.
        return self._session._session_runtime

    def close(self) -> None:
        # Stop the refill worker first and give it long enough to honor the
        # cooperative cancel at its next phase boundary (at most one in-flight
        # request, bounded by request_timeout_seconds) before tearing down the
        # RPC client, resolver, and historian. Otherwise close() can close the
        # very clients the worker thread is still using mid-advance. See #4.
        self.stop_background_session_worker(timeout=self._settings.request_timeout_seconds)
        self._rpc.close()
        close = getattr(self._resolver, "close", None)
        if callable(close):
            close()
        self._historian.close()

    @property
    def _session_worker_thread(self):
        return self._session._session_worker_thread

    def operation(
        self,
        *,
        caller: str = "direct",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        session_id: str | None = None,
    ):
        return operation_context(
            caller=caller,
            correlation_id=correlation_id,
            causation_id=causation_id,
            session_id=session_id,
        )

    def _emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        source: str,
        subject: str | None = None,
        session_id: int | str | None = None,
    ) -> str:
        event = build_event(
            event_type,
            self._sanitize_event_data(data),
            source=source,
            subject=subject,
            session_id=str(session_id) if session_id is not None else None,
        )
        try:
            self._historian.emit(event)
        except Exception as exc:
            LOGGER.warning(
                "Historian delivery failed for event_id=%s type=%s: %s",
                event["id"],
                event_type,
                exc,
            )
        return str(event["id"])

    def _sanitize_event_data(self, value: Any) -> Any:
        secrets = [
            secret
            for secret in (
                self._settings.cider_api_token,
                self._settings.resolver_api_key,
                self._settings.historian_token,
            )
            if secret
        ]
        if isinstance(value, str):
            sanitized = value
            for secret in secrets:
                sanitized = sanitized.replace(secret, "[REDACTED]")
            return sanitized
        if isinstance(value, dict):
            return {str(key): self._sanitize_event_data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize_event_data(item) for item in value]
        if isinstance(value, tuple):
            return [self._sanitize_event_data(item) for item in value]
        return value

    def _caller(self) -> str:
        context = current_operation()
        return context.caller if context is not None else "direct"

    def _record_rpc_failure(self, failure: dict[str, Any]) -> None:
        self._emit(
            "music.rpc.failed",
            {
                "caller": self._caller(),
                "operation": str(failure.get("operation", "")),
                "status_code": failure.get("status_code"),
                "error": str(failure.get("message", "")),
            },
            source="app://vesper/rpc",
        )

    def _record_error(self, component: str, operation: str | None, exc: Exception) -> None:
        self._emit(
            "core.operation.error",
            {
                "app_id": "vesper",
                "component": component,
                "error_type": exc.__class__.__name__,
                "message": str(exc),
                "operation": operation,
                "details": {"caller": self._caller()},
            },
            source=f"app://vesper/{component}",
        )

    def _track_payload(self, track: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(track, dict):
            return None
        play_params = track.get("play_params", {}) if isinstance(track.get("play_params"), dict) else {}
        track_id = (
            _clean_id(track.get("track_id"))
            or _clean_id(track.get("id"))
            or _clean_id(play_params.get("id"))
        )
        if not track_id and not any(track.get(key) for key in ("title", "artist", "album")):
            return None
        return {
            "id": track_id or None,
            "title": track.get("title"),
            "artist": track.get("artist"),
            "album": track.get("album"),
            "kind": track.get("kind") or track.get("type") or play_params.get("kind"),
            "is_library": track.get("is_library")
            if track.get("is_library") is not None
            else play_params.get("is_library"),
        }

    def _preference_target(self, preference: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(preference, dict):
            return None
        return {
            "track_id": preference.get("track_id"),
            "title": preference.get("title"),
            "artist_id": preference.get("artist_id"),
            "artist_name": preference.get("artist_name"),
            "album": preference.get("album"),
        }

    def default_search_source(self) -> str:
        return self._settings.default_search_source

    def response_detail(self) -> str:
        return self._settings.response_detail

    def include_timing_debug(self) -> bool:
        return self._settings.include_timing_debug

    def resolver_debug_log_path(self):
        return self._settings.resolver_debug_log_path

    def session_recent_tracks_limit(self) -> int:
        return self._settings.session_recent_tracks_limit

    def global_recent_tracks_limit(self) -> int:
        return self._settings.global_recent_tracks_limit

    def current_timestamp(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def list_action_definitions(self, *, text_exposable_only: bool = False, public_only: bool = True) -> list[dict[str, Any]]:
        definitions_source = (
            list_public_action_definitions()
            if public_only and not text_exposable_only
            else list_action_definitions(text_exposable_only=text_exposable_only)
        )
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "summary_label": definition.summary_label,
                "parameter_schema": definition.parameter_schema,
                "required_fields": list(definition.required_fields),
                "read_only": definition.read_only,
                "text_exposable": definition.text_exposable,
                "public_exposed": definition.public_exposed,
                "session_aware": definition.session_aware,
                "deferred_a2a_eligible": definition.deferred_a2a_eligible,
                "advanced_only": definition.advanced_only,
            }
            for definition in definitions_source
        ]

    def reconcile_session_runtime(self) -> None:
        self._session.reconcile_session_runtime()

    def start_background_session_worker(self) -> None:
        self._session.start_background_session_worker()

    def stop_background_session_worker(self, *, timeout: float = 1.0) -> None:
        self._session.stop_background_session_worker(timeout=timeout)

    def status(self) -> dict[str, Any]:
        payload = {
            "status": "ok",
            "source": "vesper",
            "config": self._settings.sanitized(),
            "playback": self.playback_snapshot(),
            "preferences_count": len(self._preferences.list_preferences()),
        }
        if self.response_detail() == "compact":
            queue = self.get_queue()
            session = self._preferences.get_active_session()
            return {
                "status": "ok",
                "source": "vesper",
                "playback": {
                    "is_playing": payload["playback"].get("is_playing"),
                    "track": compact_track(payload["playback"].get("track", {})),
                    "volume": payload["playback"].get("volume"),
                    "queue_length": queue.get("count", 0),
                },
                "queue": {
                    "count": queue.get("count", 0),
                    "tracks": [
                        compact_track(item["track"])
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

    @_historian_operation
    def play(self) -> dict[str, Any]:
        with self.operation():
            session = self._preferences.get_active_session()
            track = None
            if session is not None:
                self._set_session_runtime(session["id"], suspended=False)
                self._session._persist_session_runtime(session["id"], suspended=False, last_known_playback_state="playing")
                self._preferences.add_session_event(session["id"], event_type="session_resumed")
                snapshot = self.playback_snapshot()
                track = self._track_payload(snapshot.get("track"))
                if snapshot.get("is_playing") or _clean_id(snapshot.get("track", {}).get("track_id")):
                    result = {"status": "ok", "result": self._rpc.playback_post("/play")}
                else:
                    started_debug_episode = self._begin_resolver_debug_episode("adaptive-session-resume")
                    try:
                        return self._session._play_session_track(session, selection_strategy="adaptive-session-resume")
                    finally:
                        self._end_resolver_debug_episode(started_debug_episode)
            else:
                result = {"status": "ok", "result": self._rpc.playback_post("/play")}
            self._emit(
                "music.playback.started",
                {"caller": self._caller(), "action": "play", "track": track},
                source="app://vesper/playback",
            )
            return result

    @_historian_operation
    def pause(self) -> dict[str, Any]:
        with self.operation():
            session = self._preferences.get_active_session()
            if session is not None:
                self._set_session_runtime(session["id"], suspended=True)
                self._session._persist_session_runtime(session["id"], suspended=True, last_known_playback_state="paused")
                self._preferences.add_session_event(session["id"], event_type="session_suspended")
            result = {"status": "ok", "result": self._rpc.playback_post("/pause")}
            self._emit(
                "music.playback.paused",
                {"caller": self._caller()},
                source="app://vesper/playback",
                session_id=session["id"] if session else None,
            )
            return result

    def playpause(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/playpause")}

    @_historian_operation
    def stop(self) -> dict[str, Any]:
        with self.operation():
            stopped = self._preferences.stop_active_session()
            if stopped is not None:
                self._session._clear_session_runtime(stopped["id"])
                self._preferences.add_session_event(stopped["id"], event_type="session_stopped")
            result = {"status": "ok", "result": self._rpc.playback_post("/stop")}
            self._emit(
                "music.playback.stopped",
                {"caller": self._caller(), "reason": "stop"},
                source="app://vesper/playback",
                session_id=stopped["id"] if stopped else None,
            )
            if stopped is not None:
                self._emit(
                    "music.session.ended",
                    {
                        "caller": self._caller(),
                        "request": stopped["request_text"],
                        "reason": "playback-stopped",
                    },
                    source="app://vesper/session",
                    subject=str(stopped["id"]),
                    session_id=stopped["id"],
                )
            return result

    @_historian_operation
    def next_track(self) -> dict[str, Any]:
        with self.operation():
            session = self._preferences.get_active_session()
            if session is not None:
                self._session._set_session_runtime(session["id"], suspended=False)
                self._session._persist_session_runtime(session["id"], suspended=False, last_known_playback_state="playing")
                result = self._session._play_session_track_with_debug_episode(
                    session,
                    selection_strategy="adaptive-session-skip",
                    debug_reason="adaptive-session-skip",
                )
            else:
                result = {"status": "ok", "result": self._rpc.playback_post("/next")}
            self._emit(
                "music.track.skipped",
                {"caller": self._caller(), "direction": "next"},
                source="app://vesper/playback",
                session_id=session["id"] if session else None,
            )
            return result

    @_historian_operation
    def previous_track(self) -> dict[str, Any]:
        with self.operation():
            result = {"status": "ok", "result": self._rpc.playback_post("/previous")}
            self._emit(
                "music.track.skipped",
                {"caller": self._caller(), "direction": "previous"},
                source="app://vesper/playback",
            )
            return result

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
        validate_index(from_index, "from_index")
        validate_index(to_index, "to_index")
        return {
            "status": "ok",
            "result": self._rpc.playback_post("/queue/move-to-position", {"fromIndex": from_index, "toIndex": to_index}),
        }

    def remove_queue_item(self, index: int) -> dict[str, Any]:
        validate_index(index, "index")
        return {"status": "ok", "result": self._rpc.playback_post("/queue/remove-by-index", {"index": index})}

    def clear_queue(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/queue/clear-queue")}

    def play_url(self, url: str) -> dict[str, Any]:
        if not url.strip():
            raise CiderValidationError("url cannot be empty.")
        return {"status": "ok", "result": self._rpc.playback_post("/play-url", {"url": url})}

    @_historian_operation
    def play_item(
        self,
        item_id: str,
        *,
        kind: str = "songs",
        is_library: bool = False,
        track: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not item_id.strip():
            raise CiderValidationError("item_id cannot be empty.")
        body = {"id": item_id, "type": kind, "isLibrary": is_library}
        result = {"status": "ok", "result": self._rpc.playback_post("/play-item", body)}
        track_payload = self._track_payload(track) or {}
        track_payload.update({"id": item_id, "kind": kind, "is_library": is_library})
        track_payload.setdefault("title", None)
        track_payload.setdefault("artist", None)
        track_payload.setdefault("album", None)
        self._emit(
            "music.playback.started",
            {
                "caller": self._caller(),
                "action": "play_item",
                "track": track_payload,
            },
            source="app://vesper/playback",
            subject=item_id,
        )
        return result

    def play_item_href(self, href: str) -> dict[str, Any]:
        if not href.strip():
            raise CiderValidationError("href cannot be empty.")
        return {"status": "ok", "result": self._rpc.playback_post("/play-item-href", {"href": href})}

    def search_catalog(self, query: str, *, limit: int = 10, storefront: str = "us", offset: int = 0) -> dict[str, Any]:
        validate_limit_offset(limit, offset)
        if not query.strip():
            raise CiderValidationError("query cannot be empty.")
        payload = self._rpc.search_catalog(query=query, limit=limit, storefront=storefront, offset=offset)
        items = payload.get("data", {}).get("results", {}).get("songs", {}).get("data", [])
        return {
            "status": "ok",
            "query": query,
            "storefront": storefront,
            "offset": offset,
            "count": len(items) if isinstance(items, list) else 0,
            "tracks": [_flatten_track_item(item) for item in items] if isinstance(items, list) else [],
            "raw": payload,
        }

    def search_catalog_tracks(self, query: str, *, limit: int = 10, storefront: str = "us", offset: int = 0) -> dict[str, Any]:
        return self.search_catalog(query, limit=limit, storefront=storefront, offset=offset)

    def _catalog_resource_search(
        self,
        query: str,
        *,
        resource_type: str,
        limit: int = 5,
        storefront: str = SESSION_STOREFRONT,
    ) -> list[dict[str, Any]]:
        path = (
            f"/v1/catalog/{storefront}/search?term={_encode_query(query)}"
            f"&types={resource_type}&limit={limit}"
        )
        payload = self._rpc.run_amapi_v3(path)
        items = payload.get("data", {}).get("results", {}).get(resource_type, {}).get("data", [])
        return list(items) if isinstance(items, list) else []

    def _catalog_relationship_tracks(
        self,
        path: str,
        *,
        storefront: str = SESSION_STOREFRONT,
    ) -> list[dict[str, Any]]:
        tracks: list[dict[str, Any]] = []
        offset = 0
        while len(tracks) < self.SESSION_SEARCH_RESULT_LIMIT:
            limit = min(self.SESSION_SEARCH_PAGE_LIMIT, self.SESSION_SEARCH_RESULT_LIMIT - len(tracks))
            separator = "&" if "?" in path else "?"
            page_path = f"/v1/catalog/{storefront}{path}{separator}limit={limit}"
            if offset:
                page_path = f"{page_path}&offset={offset}"
            payload = self._rpc.run_amapi_v3(page_path)
            data = payload.get("data", {})
            items = data.get("data", [])
            if not items:
                items = data.get("results", {}).get("songs", [{}])[0].get("data", [])
            if not isinstance(items, list) or not items:
                break
            playable = [
                _flatten_track_item(item)
                for item in items
                if isinstance(item, dict)
                and item.get("type") == "songs"
                and isinstance(item.get("attributes", {}).get("playParams"), dict)
            ]
            tracks.extend(playable)
            if len(items) < limit:
                break
            offset += len(items)
        return tracks[: self.SESSION_SEARCH_RESULT_LIMIT]

    def _load_genre_map(self, storefront: str = SESSION_STOREFRONT) -> dict[str, str]:
        if storefront in self._genre_cache:
            return dict(self._genre_cache[storefront])
        try:
            payload = self._rpc.run_amapi_v3(f"/v1/catalog/{storefront}/genres")
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
        except Exception as exc:
            LOGGER.warning("Could not load Apple Music genres for %s: %s", storefront, exc)
            genre_map = {}
        self._genre_cache[storefront] = genre_map
        return dict(genre_map)

    def session_genre_names(self) -> list[str]:
        return list(self._load_genre_map(self.SESSION_STOREFRONT))

    def session_rejected_search_sources(self, session: dict[str, Any]) -> list[dict[str, str]]:
        return self._session.session_rejected_search_sources(session)

    def search(self, query: str, *, limit: int = 10, storefront: str = "us") -> dict[str, Any]:
        if self.default_search_source() == "library":
            result = self.search_library(query, limit=limit)
        else:
            result = self.search_catalog(query, limit=limit, storefront=storefront)
        result["search_source"] = self.default_search_source()
        return result

    def search_library(self, query: str, *, limit: int = 10, types: list[str] | None = None) -> dict[str, Any]:
        validate_search(query, limit)
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
        validate_search(query, limit)
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
        validate_limit_offset(limit, offset)
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
        validate_search(query, limit)
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
        validate_playlist_id(playlist_id)
        payload = self._rpc.run_amapi_v3(f"/v1/me/library/playlists/{playlist_id}")
        items = payload.get("data", {}).get("data", [])
        playlist = items[0] if isinstance(items, list) and items else {}
        return {
            "status": "ok",
            "playlist": _flatten_playlist_item(playlist) if isinstance(playlist, dict) else None,
            "raw": payload,
        }

    def get_library_playlist_tracks(self, playlist_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        validate_playlist_id(playlist_id)
        validate_limit_offset(limit, offset)
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

    def play_library_playlist(self, playlist_name: str) -> dict[str, Any]:
        if not playlist_name.strip():
            raise CiderValidationError("playlist_name cannot be empty.")
        exact_results = self.search_library_playlists(playlist_name, limit=10)
        playlists = list(exact_results.get("playlists", []))
        if not playlists:
            playlists = list(self.list_library_playlists(limit=50).get("playlists", []))
        match = self._best_playlist_match(playlists, playlist_name=playlist_name)
        if match is None:
            raise CiderValidationError(f"No library playlist matched: {playlist_name}")
        playlist_id = str(match.get("id", "")).strip()
        playlist_type = str(match.get("type", "library-playlists")).strip() or "library-playlists"
        if not playlist_id:
            raise CiderValidationError("Matched playlist did not include a playable id.")
        playback = self.play_item(playlist_id, kind=playlist_type, is_library=True)
        return {
            "status": "ok",
            "playlist": match,
            "playback": playback,
        }

    def list_recently_played(self, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        validate_limit_offset(limit, offset)
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
        validate_search(query, 25)
        validate_index(index, "index")
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
        return self._play_flattened_track(items[index], is_library_default=source == "library")

    def list_preferences(self) -> dict[str, Any]:
        preferences = self._preferences.list_preferences()
        liked_tracks = [item for item in preferences if item.get("preference_type") == "liked_track"]
        favored_artists = [item for item in preferences if item.get("preference_type") == "favored_artist"]
        rejected_tracks = [item for item in preferences if item.get("preference_type") == "globally_rejected_track"]
        return {
            "status": "ok",
            "count": len(preferences),
            "summary": {
                "liked_tracks": len(liked_tracks),
                "favored_artists": len(favored_artists),
                "globally_rejected_tracks": len(rejected_tracks),
            },
            "liked_tracks": liked_tracks,
            "favored_artists": favored_artists,
            "globally_rejected_tracks": rejected_tracks,
            "preferences": preferences,
        }

    @_historian_operation
    def forget_preference(self, preference_id: int) -> dict[str, Any]:
        preference = None
        try:
            preference = self._preferences.get_preference(preference_id)
        except Exception:
            pass
        removed = self._preferences.delete_preference(preference_id)
        if not removed:
            raise CiderValidationError(f"Preference {preference_id} was not found.")
        self._emit(
            "music.preference.forgotten",
            {
                "caller": self._caller(),
                "preference_id": preference_id,
                "preference_type": preference.get("preference_type") if preference else None,
                "target": self._preference_target(preference),
            },
            source="app://vesper/preferences",
            subject=str(preference_id),
        )
        return {"status": "ok", "removed": True, "preference_id": preference_id}

    @_historian_operation
    def like_current_track(self) -> dict[str, Any]:
        playback = self.playback_snapshot()
        current = playback.get("track", {})
        current_id = _clean_id(current.get("track_id"))
        if not current_id:
            raise CiderValidationError("No current track is available to like.")
        session = self._preferences.get_active_session()
        runtime = self._get_session_runtime(session["id"]) if session is not None else {}
        liked_track = self._preferences.record_liked_track(
            track_id=current_id,
            title=str(current.get("title", "")).strip() or None,
            artist_name=str(current.get("artist", "")).strip() or None,
            album=str(current.get("album", "")).strip() or None,
            item_kind=str(current.get("kind", "")).strip() or None,
            is_library=bool(current.get("is_library")) if current.get("is_library") is not None else None,
            session_request_text=str(session.get("request_text", "")).strip() or None if session is not None else None,
            session_search_query=self._session._current_preference_context_query(runtime),
        )
        favored_artist = None
        if str(current.get("artist", "")).strip():
            favored_artist = self._preferences.record_favored_artist(
                artist_name=str(current.get("artist", "")).strip(),
                session_request_text=liked_track.get("session_request_text"),
                session_search_query=liked_track.get("session_search_query"),
            )
        if session is not None:
            self._preferences.add_session_event(
                session["id"],
                event_type="track_liked",
                track={
                    "track_id": current_id,
                    "title": current.get("title"),
                    "artist": current.get("artist"),
                    "album": current.get("album"),
                    "href": None,
                },
                metadata={
                    "session_request_text": liked_track.get("session_request_text"),
                    "session_search_query": liked_track.get("session_search_query"),
                },
            )
        self._emit(
            "music.preference.recorded",
            {
                "caller": self._caller(),
                "preference_id": liked_track["id"],
                "preference_type": liked_track["preference_type"],
                "polarity": "like",
                "target": self._preference_target(liked_track),
                "reason": None,
            },
            source="app://vesper/preferences",
            subject=str(liked_track["id"]),
            session_id=session["id"] if session else None,
        )
        if favored_artist is not None:
            self._emit(
                "music.preference.recorded",
                {
                    "caller": self._caller(),
                    "preference_id": favored_artist["id"],
                    "preference_type": favored_artist["preference_type"],
                    "polarity": "prefer",
                    "target": self._preference_target(favored_artist),
                    "reason": "artist of liked track",
                },
                source="app://vesper/preferences",
                subject=str(favored_artist["id"]),
                session_id=session["id"] if session else None,
            )
        return {
            "status": "ok",
            "playback_continues": True,
            "liked_track": liked_track,
            "favored_artist": favored_artist,
        }

    def session_status(self, *, include_recent_tracks: bool = True, compact: bool | None = None) -> dict[str, Any]:
        return self._session.session_status(include_recent_tracks=include_recent_tracks, compact=compact)

    def recent_session_tracks(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self._session.recent_session_tracks(limit=limit)

    def recent_global_tracks(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self._session.recent_global_tracks(limit=limit)

    def session_planning_playback_snapshot(self, session: dict[str, Any]) -> dict[str, Any]:
        return self._session.session_planning_playback_snapshot(session)

    @_historian_operation
    def stop_session(self) -> dict[str, Any]:
        return self._session.stop_session()

    @_historian_operation
    def play_session(self, request: str) -> dict[str, Any]:
        return self._session.play_session(request)

    @_historian_operation
    def steer_session(self, request: str, *, search_update: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._session.steer_session(request, search_update=search_update)

    @_historian_operation
    def refill_active_session(self) -> dict[str, Any]:
        return self._session.refill_active_session()

    @_historian_operation
    def reject_current_track(self) -> dict[str, Any]:
        return self._session.reject_current_track()

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
            match = self._best_track_match(search["tracks"], title=title, artist=artist)
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

    def resolve_text_request(self, text: str) -> ResolvedAction:
        return self._resolver.resolve(text, self)

    def execute_text_request(self, text: str) -> TextRequestResult:
        with self.operation():
            request_event_id = self._emit(
                "music.request.received",
                {
                    "caller": self._caller(),
                    "request": text,
                    "resolved_action": None,
                },
                source="app://vesper/request",
            )
            operation_token = replace_operation(causation_id=request_event_id)
            try:
                return self._execute_text_request(text)
            finally:
                reset_operation(operation_token)

    def _execute_text_request(self, text: str) -> TextRequestResult:
        try:
            started_debug_episode = self._begin_resolver_debug_episode(f"text-request: {text.strip()}")
            started_at = time.perf_counter()
            resolve_started_at = time.perf_counter()
            resolved = self.resolve_text_request(text)
            resolve_ms = _elapsed_ms(resolve_started_at)
            resolved_action = compact_resolved_action(resolved.action, resolved.parameters, self.response_detail())
            execute_started_at = time.perf_counter()
            try:
                execution = self.execute_action(resolved.action, resolved.parameters)
            except CiderAgentError as exc:
                self._record_error("service", resolved.action, exc)
                error = {"type": exc.__class__.__name__, "message": str(exc)}
                timings = None
                if self.include_timing_debug():
                    timings = {
                        "resolve_ms": resolve_ms,
                        "execute_ms": _elapsed_ms(execute_started_at),
                        "total_ms": _elapsed_ms(started_at),
                    }
                failure = TextRequestResult(
                    status="error",
                    input=text,
                    resolver=resolved.resolver,
                    resolved_action=resolved_action,
                    execution=EngineActionResult(action=resolved.action, result={}),
                    reasoning=resolved.reasoning,
                    resolver_raw_content=resolved.raw_content,
                    resolver_raw_action=resolved.raw if self._settings.resolver_include_raw_output else None,
                    timings=timings,
                    error=error,
                )
                raise TextRequestExecutionError(str(exc), failure.as_dict()) from exc
            summary = summarize_execution(execution.as_dict())
            timings = None
            if self.include_timing_debug():
                timings = {
                    "resolve_ms": resolve_ms,
                    "execute_ms": _elapsed_ms(execute_started_at),
                    "total_ms": _elapsed_ms(started_at),
                }
            return TextRequestResult(
                input=text,
                resolver=resolved.resolver,
                resolved_action=resolved_action,
                execution=execution,
                summary=summary,
                reasoning=resolved.reasoning,
                resolver_raw_content=resolved.raw_content,
                resolver_raw_action=resolved.raw if self._settings.resolver_include_raw_output else None,
                timings=timings,
            )
        except CiderAgentError as exc:
            if not isinstance(exc, TextRequestExecutionError):
                self._record_error("resolver", "resolve_text_request", exc)
            raise
        finally:
            self._end_resolver_debug_episode(locals().get("started_debug_episode", False))

    def handle_text_request(self, text: str) -> dict[str, Any]:
        return self.execute_text_request(text).as_dict()

    def execute_action(self, action: str, parameters: dict[str, Any] | None = None) -> EngineActionResult:
        params = parameters or {}
        definition = get_action_definition(action)
        if definition is None:
            raise CiderValidationError(f"Unsupported action: {action}")
        try:
            result = definition.executor(self, params)
        except (KeyError, TypeError, ValueError) as exc:
            self._record_error("service", action, exc)
            raise CiderValidationError(f"Invalid parameters for action {action}: {exc}") from exc
        return EngineActionResult(action=action, result=finalize_output(result, self.response_detail(), self.include_timing_debug()))

    def run_action(self, action: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.execute_action(action, parameters).as_dict()

    def _play_session_track_with_debug_episode(
        self,
        session: dict[str, Any],
        *,
        selection_strategy: str,
        debug_reason: str,
    ) -> dict[str, Any]:
        return self._session._play_session_track_with_debug_episode(
            session,
            selection_strategy=selection_strategy,
            debug_reason=debug_reason,
        )

    def _play_session_track(self, session: dict[str, Any], *, selection_strategy: str) -> dict[str, Any]:
        return self._session._play_session_track(session, selection_strategy=selection_strategy)

    def _collect_session_tracks(
        self,
        session: dict[str, Any],
        plan: SessionQueryPlan,
        *,
        limit: int,
        timings: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], SessionSearchSource, SessionTrackSelection]:
        return self._session._collect_session_tracks(session, plan, limit=limit, timings=timings)

    def _normalize_session_search_update(self, value: dict[str, Any] | None) -> dict[str, Any]:
        return self._session._normalize_session_search_update(value)

    def _sources_payload(self, sources: list[SessionSearchSource]) -> list[dict[str, str]]:
        return [{"kind": source.kind, "term": source.term} for source in sources]

    def _next_session_search_sources(
        self,
        runtime: dict[str, Any],
        search_update: dict[str, Any],
    ) -> list[SessionSearchSource]:
        return self._session._next_session_search_sources(runtime, search_update)

    def _build_session_query_pool(self, session: dict[str, Any], source: SessionSearchSource) -> dict[str, Any]:
        return self._session._build_session_query_pool(session, source)

    def _fetch_session_source_results(
        self,
        session: dict[str, Any],
        source: SessionSearchSource,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self._session._fetch_session_source_results(session, source)

    def _ensure_session_query_pools(
        self,
        session: dict[str, Any],
        search_sources: list[SessionSearchSource],
    ) -> None:
        self._session._ensure_session_query_pools(session, search_sources)

    def _next_session_candidate_window(
        self,
        session: dict[str, Any],
        source: SessionSearchSource,
    ) -> list[dict[str, Any]]:
        return self._session._next_session_candidate_window(session, source)

    def _begin_resolver_debug_episode(self, reason: str) -> bool:
        path = self.resolver_debug_log_path()
        if path is None:
            return False
        with self._resolver_debug_log_lock:
            if self._resolver_debug_episode_depth == 0:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"timestamp: {self.current_timestamp()}\nreason: {reason}\n\n",
                    encoding="utf-8",
                )
            self._resolver_debug_episode_depth += 1
            return True

    def _end_resolver_debug_episode(self, started: bool) -> None:
        if not started:
            return
        with self._resolver_debug_log_lock:
            self._resolver_debug_episode_depth = max(0, self._resolver_debug_episode_depth - 1)

    def append_resolver_debug_log(
        self,
        *,
        stage: str,
        messages: list[dict[str, Any]],
        response_body: dict[str, Any],
        response_content: str,
    ) -> None:
        path = self.resolver_debug_log_path()
        if path is None:
            return
        entry = (
            f"=== {stage} ===\n"
            f"timestamp: {self.current_timestamp()}\n"
            "messages:\n"
            f"{json.dumps(messages, ensure_ascii=False, indent=2)}\n\n"
            "response_body:\n"
            f"{json.dumps(response_body, ensure_ascii=False, indent=2)}\n\n"
            "response_content:\n"
            f"{response_content}\n\n"
        )
        with self._resolver_debug_log_lock:
            if self._resolver_debug_episode_depth <= 0:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(entry)

    def append_session_debug_log(self, *, stage: str, payload: dict[str, Any]) -> None:
        path = self.resolver_debug_log_path()
        if path is None:
            return
        entry = (
            f"=== {stage} ===\n"
            f"timestamp: {self.current_timestamp()}\n"
            "payload:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        )
        with self._resolver_debug_log_lock:
            if self._resolver_debug_episode_depth <= 0:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(entry)

    def _should_advance_session(self, session: dict[str, Any], playback: dict[str, Any]) -> bool:
        return self._session._should_advance_session(session, playback)

    def _get_session_runtime(self, session_id: int) -> dict[str, Any]:
        return self._session._get_session_runtime(session_id)

    def _set_session_runtime(self, session_id: int, **updates: Any) -> None:
        self._session._set_session_runtime(session_id, **updates)

    def _extract_is_playing(self, payload: Any) -> bool | None:
        if isinstance(payload, bool):
            return payload
        if isinstance(payload, dict):
            if "value" in payload:
                return bool(payload["value"])
            if "is_playing" in payload:
                return bool(payload["is_playing"])
        return None

    def _best_track_match(self, tracks: list[dict[str, Any]], *, title: str, artist: str) -> dict[str, Any] | None:
        title_norm = _normalize_match_text(title)
        title_base_norm = _normalize_title_match_text(title)
        artist_norm = _normalize_match_text(artist)
        for track in tracks:
            if _normalize_match_text(track.get("title")) == title_norm and _normalize_match_text(track.get("artist")) == artist_norm:
                return track
        for track in tracks:
            track_title = _normalize_match_text(track.get("title"))
            track_artist = _normalize_match_text(track.get("artist"))
            if title_norm in track_title and artist_norm == track_artist:
                return track
        if title_base_norm:
            for track in tracks:
                track_title_base = _normalize_title_match_text(track.get("title"))
                track_artist = _normalize_match_text(track.get("artist"))
                if track_artist != artist_norm:
                    continue
                if track_title_base == title_base_norm:
                    return track
            for track in tracks:
                track_title_base = _normalize_title_match_text(track.get("title"))
                track_artist = _normalize_match_text(track.get("artist"))
                if track_artist != artist_norm:
                    continue
                if title_base_norm in track_title_base or track_title_base in title_base_norm:
                    return track
        return None

    def _best_artist_track_match(self, tracks: list[dict[str, Any]], *, artist: str) -> dict[str, Any] | None:
        matches = self._best_artist_track_matches(tracks, artist=artist, limit=1)
        return matches[0] if matches else None

    def _best_playlist_match(self, playlists: list[dict[str, Any]], *, playlist_name: str) -> dict[str, Any] | None:
        target = _normalize_match_text(playlist_name)
        if not target:
            return None
        for playlist in playlists:
            if _normalize_match_text(playlist.get("name")) == target:
                return playlist
        for playlist in playlists:
            name = _normalize_match_text(playlist.get("name"))
            if target in name or name in target:
                return playlist
        return None

    def _best_artist_track_matches(self, tracks: list[dict[str, Any]], *, artist: str, limit: int) -> list[dict[str, Any]]:
        artist_norm = _normalize_match_text(artist)
        exact_artist_tracks = [track for track in tracks if _normalize_match_text(track.get("artist")) == artist_norm]
        if not exact_artist_tracks:
            return []
        scored = sorted(exact_artist_tracks, key=self._artist_track_score, reverse=True)
        return self._top_pool_order(scored, take=limit)

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

    def _top_pool_order(
        self,
        tracks: list[dict[str, Any]],
        *,
        take: int,
        pool_size: int | None = None,
    ) -> list[dict[str, Any]]:
        if take <= 0 or not tracks:
            return []
        bounded_pool = max(1, min(pool_size or self.TRACK_SELECTION_POOL_SIZE, len(tracks)))
        top_pool = list(tracks[:bounded_pool])
        self._random.shuffle(top_pool)
        ordered = top_pool + list(tracks[bounded_pool:])
        return ordered[:take]

    def _play_search_result_from_pool(self, *, query: str, source: str, storefront: str) -> dict[str, Any]:
        resolved_source = source.strip().lower()
        if resolved_source == "default":
            resolved_source = self.default_search_source()
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
        return self.play_item(item_id, kind=kind, is_library=is_library, track=track)

    def _enqueue_flattened_track(self, track: dict[str, Any]) -> dict[str, Any]:
        play_params = track.get("play_params", {})
        item_id = str(play_params.get("id", "")).strip()
        kind = str(play_params.get("kind", "songs")).strip() or "songs"
        is_library = bool(play_params.get("is_library", False))
        if not item_id:
            raise CiderValidationError("Resolved track did not include a playable id.")
        return self.play_later({"id": item_id, "type": kind, "isLibrary": is_library})

