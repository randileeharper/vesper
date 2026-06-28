"""Playback control for the Cider agent.

Extracted from the :class:`vesper.service.CiderAgentService` god-class
(issue #42). This controller owns the pure RPC-backed playback operations:
volume, repeat, shuffle, autoplay, queue management, play-item/url/href,
playpause, now-playing, and the playback snapshot. It also owns ``status``
which composes a snapshot with preference/session summary.

The session-aware playback methods (play, pause, stop, next_track,
previous_track) remain on the facade because they orchestrate across
playback, session runtime, preferences, and historian emission.

Cross-cutting capabilities (historian emission, caller context, track payload
normalization, response detail) are reached through the
:class:`PlaybackHost` Protocol so the controller never imports the concrete
service class.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol

from .catalog import (
    flatten_track_item as _flatten_track_item,
    now_playing_info as _now_playing_info,
)
from .errors import CiderValidationError
from .output import compact_track
from .storage import PreferenceStore
from .utils import _extract_is_playing, _track_payload
from .validation import validate_index


class PlaybackHost(Protocol):
    """Structural interface for the cross-cutting capabilities
    :class:`PlaybackController` borrows from its host."""

    def _emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        source: str,
        subject: str | None = None,
        session_id: int | str | None = None,
    ) -> str:
        ...

    def _caller(self) -> str:
        ...

    def response_detail(self) -> str:
        ...

    @property
    def _settings(self) -> Any:
        ...


class PlaybackController:
    """Pure RPC playback operations: volume, queue, transport, snapshot."""

    # Each playback_snapshot fans out 7 concurrent RPC reads. The instance
    # pool is sized to allow two overlapping snapshots (e.g. a request handler
    # and the background session worker advancing in parallel) without
    # contending on a single snapshot's worth of workers, while bounding the
    # thread count. Reusing one pool avoids creating/destroying 7 threads on
    # every call to this hot path. See #67.
    _POOL_WORKERS = 14

    # Per-future timeout when draining in-flight snapshots in close(). Bounded
    # so a hung RPC can't block close() indefinitely (issue #89).
    _CLOSE_DRAIN_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        host: PlaybackHost,
        *,
        rpc,
        preferences: PreferenceStore,
        settings,
    ) -> None:
        self._host = host
        self._rpc = rpc
        self._preferences = preferences
        self._settings = settings
        self._executor = ThreadPoolExecutor(
            max_workers=self._POOL_WORKERS, thread_name_prefix="playback-snapshot"
        )
        # In-flight snapshot futures, guarded by _pending_lock so close() can
        # drain a consistent set while playback_snapshot() submits new work.
        # See #89.
        self._pending_futures: set[Future[Any]] = set()
        self._pending_lock = threading.Lock()

    def close(self) -> None:
        """Release the shared snapshot thread pool. Idempotent.

        Signals the pool to stop accepting work, then waits a bounded time for
        any in-flight playback_snapshot() fan-out to finish so worker threads
        are not orphaned and can't raise at interpreter shutdown (issue #89).
        """
        # Stop accepting new submissions. Do NOT use cancel_futures=True here:
        # playback_snapshot() is actively awaiting future.result() on the same
        # futures, and cancelling them would raise CancelledError inside the
        # snapshot call. The drain loop below handles waiting.
        self._executor.shutdown(wait=False)
        with self._pending_lock:
            pending = list(self._pending_futures)
        # Drain in-flight tasks with a short bounded timeout per future. If a
        # snapshot RPC hangs, we don't block close() indefinitely — the worker
        # is a daemon thread and won't block interpreter exit, but we give
        # well-behaved tasks a chance to complete.
        for future in pending:
            future.result(timeout=self._CLOSE_DRAIN_TIMEOUT_SECONDS)

    def status(self) -> dict[str, Any]:
        playback = self.playback_snapshot()
        payload = {
            "status": "ok",
            "source": "vesper",
            "config": self._settings.sanitized(),
            "playback": playback,
            "preferences_count": len(self._preferences.list_preferences()),
        }
        if self._host.response_detail() == "compact":
            queue = playback.get("queue", {})
            session = self._preferences.get_active_session()
            return {
                "status": "ok",
                "source": "vesper",
                "playback": {
                    "is_playing": playback.get("is_playing"),
                    "track": compact_track(playback.get("track", {})),
                    "volume": playback.get("volume"),
                    "queue_length": queue.get("count", 0) if isinstance(queue, dict) else 0,
                },
                "queue": {
                    "count": queue.get("count", 0) if isinstance(queue, dict) else 0,
                    "tracks": [
                        compact_track(item["track"])
                        for item in (queue.get("items", []) if isinstance(queue, dict) else [])[:5]
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
        info = _now_playing_info(payload)
        return {
            "status": "ok",
            "source": "cider-rpc",
            "track": _flatten_track_item({"attributes": info}),
            "raw": payload,
        }

    def playback_snapshot(self) -> dict[str, Any]:
        snapshot_paths = {
            "is_playing": "/is-playing",
            "now_playing": "/now-playing",
            "volume": "/volume",
            "queue": "/queue",
            "repeat": "/repeat-mode",
            "shuffle": "/shuffle-mode",
            "autoplay": "/autoplay",
        }
        # Fan out the 7 playback reads on the reused instance pool rather than
        # creating a ThreadPoolExecutor per call. Each future's result() is
        # awaited below, so all submissions complete before we return. See #67.
        # Futures are registered in _pending_futures so close() can drain them
        # if it runs while this fan-out is in flight (issue #89).
        futures = {
            name: self._executor.submit(self._rpc.playback_get, path)
            for name, path in snapshot_paths.items()
        }
        with self._pending_lock:
            self._pending_futures.update(futures.values())
        try:
            payloads = {name: future.result() for name, future in futures.items()}
        finally:
            with self._pending_lock:
                self._pending_futures.difference_update(futures.values())
        is_playing_payload = payloads["is_playing"]
        now_playing_payload = payloads["now_playing"]
        volume_payload = payloads["volume"]
        queue_payload = payloads["queue"]
        repeat_payload = payloads["repeat"]
        shuffle_payload = payloads["shuffle"]
        autoplay_payload = payloads["autoplay"]
        info = _now_playing_info(now_playing_payload)
        queue = self._queue_result(queue_payload)
        return {
            "status": "ok",
            "source": "cider-rpc",
            "is_playing": _extract_is_playing(is_playing_payload),
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
            "queue_length": queue["count"],
            "queue": queue,
        }

    def is_playing(self) -> dict[str, Any]:
        return {"status": "ok", "is_playing": _extract_is_playing(self._rpc.playback_get("/is-playing"))}

    def playpause(self) -> dict[str, Any]:
        return {"status": "ok", "result": self._rpc.playback_post("/playpause")}

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
        return self._queue_result(self._rpc.playback_get("/queue"))

    def _queue_result(self, payload: Any) -> dict[str, Any]:
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
        track_payload = _track_payload(track) or {}
        track_payload.update({"id": item_id, "kind": kind, "is_library": is_library})
        track_payload.setdefault("title", None)
        track_payload.setdefault("artist", None)
        track_payload.setdefault("album", None)
        self._host._emit(
            "music.playback.started",
            {
                "caller": self._host._caller(),
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
