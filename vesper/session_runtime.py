"""Runtime state, auto-advance decisions, and selection-event recording for
:class:`vesper.session.SessionEngine`.

Extracted behavior-preservingly from ``SessionEngine`` (issue #34). This is the
foundational runtime layer reused by the queue and sources mixins and by the
worker/playback loop that remains on ``SessionEngine``.

The mixin is combined into ``SessionEngine`` via cooperative inheritance, so
``self`` is the engine instance: it reads ``self._host``, ``self._preferences``,
``self._session_runtime``, and ``self._session_runtime_lock`` exactly as the
methods did before extraction. Cross-mixin calls (e.g.
``self._normalize_session_query_pools``) resolve against the combined class.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .utils import _clean_id

if TYPE_CHECKING:
    import threading

    from .session import SessionHost
    from .storage import PreferenceStore

# Sentinel stored as ``pending_stop_track_id`` when Cider reports playback
# stopped with no current track at all. Real track ids are numeric, so this
# never collides; it lets the two-snapshot stop confirmation apply to the
# no-track case exactly as it does to a known-but-ambiguous track.
_PENDING_STOP_NO_TRACK_SENTINEL = "<missing>"


class SessionRuntimeMixin:
    """Runtime get/set/clear/persist, should-advance decisions, two-snapshot
    stop confirmation, pending-stop confirmation, auto-advance logging, and
    selection events.

    Expects the combined ``SessionEngine`` to provide ``self._host``,
    ``self._preferences``, ``self._session_runtime`` (``dict[int, dict]``),
    and ``self._session_runtime_lock``.
    """

    if TYPE_CHECKING:
        _host: SessionHost
        _preferences: PreferenceStore
        _session_runtime: dict[int, dict[str, Any]]
        _session_runtime_lock: threading.Lock

    def _mark_session_track_rejected(self, session_id: int, track_id: str) -> None:
        self._preferences.mark_session_queue_track(session_id, track_id, "rejected")
        runtime = self._get_session_runtime(session_id)
        self._set_session_runtime(session_id, current_queue_item_id=None)
        pools = self._normalize_session_query_pools(runtime.get("query_pools"))
        preferred_query = str(runtime.get("current_pool_query", "")).strip()
        ordered_queries = [preferred_query] if preferred_query else []
        ordered_queries.extend(query for query in pools if query != preferred_query)
        for query in ordered_queries:
            pool = pools.get(query)
            if not pool:
                continue
            for entry in pool.get("entries", []):
                entry_track = entry.get("track", {})
                entry_track_id = _clean_id(entry_track.get("id")) or _clean_id(entry_track.get("play_params", {}).get("id"))
                if entry_track_id != track_id:
                    continue
                entry["state"] = "rejected"
                pools[query] = pool
                self._set_session_runtime(session_id, query_pools=pools, current_track_id=track_id)
                return

    def _record_current_track_for_session(
        self,
        session: dict[str, Any],
        *,
        playback: dict[str, Any] | None = None,
        event_type: str = "track_started",
    ) -> None:
        current = (playback or self._host.playback_snapshot()).get("track", {})
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
        self._preferences.add_session_event(
            session["id"],
            event_type=event_type,
            track={
                "track_id": current_id,
                "title": current.get("title"),
                "artist": current.get("artist"),
                "album": current.get("album"),
                "href": None,
            },
        )

    def _should_advance_session(self, session: dict[str, Any], playback: dict[str, Any]) -> bool:
        # This coordinates long-lived processes through persisted runtime, but
        # intentionally does not provide an atomic cross-process worker lease.
        runtime = self._effective_session_runtime(session["id"])
        track = playback.get("track", {})
        if not isinstance(track, dict):
            track = {}
        track_state = self._playback_current_track_state(playback)
        elapsed = self._seconds_since_runtime_timestamp(runtime.get("last_advance_at"))
        debug_payload = {
            "session_id": session["id"],
            "playback": {
                "is_playing": playback.get("is_playing"),
                "track": {
                    "track_id": _clean_id(track.get("track_id")) or None,
                    "title": track.get("title"),
                    "artist": track.get("artist"),
                    "album": track.get("album"),
                    "current_playback_time": track.get("current_playback_time"),
                    "remaining_time": track.get("remaining_time"),
                    "duration_millis": track.get("duration_millis"),
                },
            },
            "runtime": {
                "suspended": runtime.get("suspended"),
                "advance_in_progress": runtime.get("advance_in_progress"),
                "last_advance_at": runtime.get("last_advance_at"),
                "last_known_playback_state": runtime.get("last_known_playback_state"),
                "pending_stop_track_id": runtime.get("pending_stop_track_id"),
                "pending_stop_observed_at": runtime.get("pending_stop_observed_at"),
            },
            "track_state": track_state,
            "seconds_since_last_advance": elapsed,
            "cooldown_seconds": self._host.SESSION_ADVANCE_COOLDOWN_SECONDS,
        }
        if runtime.get("suspended"):
            self._log_session_auto_advance_decision(debug_payload, advance=False, blocked_by="session_suspended")
            return False
        if runtime.get("advance_in_progress"):
            self._log_session_auto_advance_decision(debug_payload, advance=False, blocked_by="advance_in_progress")
            return False
        is_playing = playback.get("is_playing")
        if is_playing is True:
            self._clear_pending_stop_confirmation(session["id"])
            debug_payload["runtime_after_clear"] = self._session_runtime_confirmation_state(session["id"])
            self._log_session_auto_advance_decision(debug_payload, advance=False, blocked_by="playback_active")
            return False
        if is_playing is not False:
            self._clear_pending_stop_confirmation(session["id"])
            debug_payload["runtime_after_clear"] = self._session_runtime_confirmation_state(session["id"])
            self._log_session_auto_advance_decision(debug_payload, advance=False, blocked_by="playback_state_unknown")
            return False
        if track_state == "unfinished":
            self._clear_pending_stop_confirmation(session["id"])
            debug_payload["runtime_after_clear"] = self._session_runtime_confirmation_state(session["id"])
            self._log_session_auto_advance_decision(debug_payload, advance=False, blocked_by="unfinished_current_track")
            return False
        if track_state in ("ambiguous", "missing") and not self._confirm_pending_stop_snapshot(session["id"], runtime, playback):
            debug_payload["runtime_after_confirmation"] = self._session_runtime_confirmation_state(session["id"])
            self._log_session_auto_advance_decision(debug_payload, advance=False, blocked_by="awaiting_second_stopped_snapshot")
            return False
        if track_state not in ("ambiguous", "missing"):
            self._clear_pending_stop_confirmation(session["id"])
            debug_payload["runtime_after_clear"] = self._session_runtime_confirmation_state(session["id"])
        if elapsed is not None and elapsed < self._host.SESSION_ADVANCE_COOLDOWN_SECONDS:
            self._log_session_auto_advance_decision(debug_payload, advance=False, blocked_by="advance_cooldown")
            return False
        debug_payload["runtime_after_confirmation"] = self._session_runtime_confirmation_state(session["id"])
        self._log_session_auto_advance_decision(debug_payload, advance=True, blocked_by=None)
        return True

    def _playback_current_track_state(self, playback: dict[str, Any]) -> str:
        track = playback.get("track", {})
        if not isinstance(track, dict):
            return "missing"
        if not _clean_id(track.get("track_id")):
            return "missing"
        remaining = self._numeric_value(track.get("remaining_time"))
        if remaining is not None and remaining > 1.0:
            return "unfinished"
        current = self._numeric_value(track.get("current_playback_time"))
        duration = self._numeric_value(track.get("duration_millis"))
        if current is not None and duration is not None and duration > 0:
            duration_seconds = duration / 1000.0 if duration > 1000 else duration
            current_seconds = current / 1000.0 if current > duration_seconds + 5 and current <= duration + 5 else current
            return "unfinished" if current_seconds < duration_seconds - 1.0 else "finished"
        if remaining is not None:
            return "ambiguous"
        # Cider may briefly report is-playing false while still exposing the
        # current track during startup/buffering. Require two consecutive
        # stopped snapshots before treating that as an advance signal.
        return "ambiguous"

    def _confirm_pending_stop_snapshot(
        self,
        session_id: int,
        runtime: dict[str, Any],
        playback: dict[str, Any],
    ) -> bool:
        track = playback.get("track", {})
        track_id = _clean_id(track.get("track_id")) if isinstance(track, dict) else ""
        if not track_id:
            # Cider can briefly report a stopped snapshot with an empty
            # now-playing payload while a track is still playing. Treat the
            # absence of a track id like any other ambiguous stop and key the
            # confirmation on a sentinel so two consecutive stopped snapshots are
            # still required before advancing.
            track_id = _PENDING_STOP_NO_TRACK_SENTINEL
        pending_track_id = _clean_id(runtime.get("pending_stop_track_id"))
        pending_observed_at = runtime.get("pending_stop_observed_at")
        if pending_track_id == track_id and pending_observed_at is not None:
            return True
        observed_at = self._host.current_timestamp()
        self._set_session_runtime(
            session_id,
            pending_stop_track_id=track_id,
            pending_stop_observed_at=observed_at,
        )
        self._preferences.update_session_pending_stop(session_id, track_id=track_id, observed_at=observed_at)
        return False

    def _clear_pending_stop_confirmation(self, session_id: int) -> None:
        runtime = self._effective_session_runtime(session_id)
        if runtime.get("pending_stop_track_id") is None and runtime.get("pending_stop_observed_at") is None:
            return
        self._set_session_runtime(session_id, pending_stop_track_id=None, pending_stop_observed_at=None)
        self._preferences.update_session_pending_stop(session_id, track_id=None, observed_at=None)

    def _session_runtime_confirmation_state(self, session_id: int) -> dict[str, Any]:
        runtime = self._get_session_runtime(session_id)
        return {
            "pending_stop_track_id": runtime.get("pending_stop_track_id"),
            "pending_stop_observed_at": runtime.get("pending_stop_observed_at"),
        }

    def _log_session_auto_advance_decision(
        self,
        payload: dict[str, Any],
        *,
        advance: bool,
        blocked_by: str | None,
    ) -> None:
        decision_payload = dict(payload)
        decision_payload["decision"] = {
            "advance": advance,
            "blocked_by": blocked_by,
        }
        self._host.append_session_debug_log(
            stage="session_auto_advance_evaluated",
            payload=decision_payload,
        )

    def _numeric_value(self, value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                return float(stripped)
            except ValueError:
                return None
        return None

    def _effective_session_runtime(self, session_id: int) -> dict[str, Any]:
        runtime = self._get_session_runtime(session_id)
        # Session control can be split across long-lived processes. Persisted
        # control state must override stale in-memory values from this process.
        stored = self._preferences.get_session_runtime(session_id) or {}
        if stored:
            runtime["suspended"] = stored.get("active_intent") == "suspended"
        if stored.get("last_advance_at") is not None:
            runtime["last_advance_at"] = stored.get("last_advance_at")
        if "last_selected_track_id" not in runtime and stored.get("last_selected_track_id") is not None:
            runtime["last_selected_track_id"] = stored.get("last_selected_track_id")
        if "last_known_playback_state" not in runtime and stored.get("last_known_playback_state") is not None:
            runtime["last_known_playback_state"] = stored.get("last_known_playback_state")
        # The two-snapshot stop confirmation is cross-process state: a stop
        # observed by one process must count toward confirmation in another.
        # Persisted values are authoritative, including None once cleared.
        if stored:
            runtime["pending_stop_track_id"] = stored.get("pending_stop_track_id")
            runtime["pending_stop_observed_at"] = stored.get("pending_stop_observed_at")
        return runtime

    def _get_session_runtime(self, session_id: int) -> dict[str, Any]:
        with self._session_runtime_lock:
            return dict(self._session_runtime.get(session_id, {}))

    def _set_session_runtime(self, session_id: int, **updates: Any) -> None:
        with self._session_runtime_lock:
            runtime = dict(self._session_runtime.get(session_id, {}))
            runtime.update(updates)
            self._session_runtime[session_id] = runtime
        if updates:
            interesting = {
                key: value
                for key, value in updates.items()
                if key
                in {
                    "active_search_sources",
                    "rejected_search_sources",
                    "query_pools",
                    "current_pool_query",
                    "current_track_id",
                    "suspended",
                }
            }
            if "query_pools" in interesting and isinstance(interesting["query_pools"], dict):
                interesting["query_pools"] = {
                    str(source_key): {
                        "source": pool.get("source"),
                        "cursor": pool.get("cursor", 0),
                        "entry_count": len(pool.get("entries", [])) if isinstance(pool, dict) else 0,
                    }
                    for source_key, pool in interesting["query_pools"].items()
                    if isinstance(pool, dict)
                }
            if interesting:
                self._host.append_session_debug_log(
                    stage="session_runtime_updated",
                    payload={"session_id": session_id, "updates": interesting},
                )

    def _clear_session_runtime(self, session_id: int) -> None:
        with self._session_runtime_lock:
            self._session_runtime.pop(session_id, None)
        self._preferences.clear_session_runtime(session_id)

    def _clear_all_session_runtime(self) -> None:
        with self._session_runtime_lock:
            self._session_runtime.clear()
        self._host.append_session_debug_log(stage="session_runtime_cleared", payload={"scope": "all"})

    def _abort_failed_session_start(self, session_id: int) -> None:
        active = self._preferences.get_active_session()
        if active is not None and int(active["id"]) == int(session_id):
            self._preferences.stop_active_session()
        self._clear_session_runtime(session_id)

    def _persist_session_runtime(
        self,
        session_id: int,
        *,
        suspended: bool | None = None,
        last_advance_at: str | None = None,
        last_selected_track_id: str | None = None,
        last_known_playback_state: str | None = None,
        preserve_last_advance: bool = False,
    ) -> None:
        runtime = self._preferences.get_session_runtime(session_id)
        resolved_last_advance_at = runtime.get("last_advance_at") if runtime and preserve_last_advance else last_advance_at
        self._preferences.upsert_session_runtime(
            session_id,
            active_intent="suspended" if suspended else "active",
            last_advance_at=resolved_last_advance_at,
            last_selected_track_id=last_selected_track_id,
            last_known_playback_state=last_known_playback_state,
        )

    def _seconds_since_runtime_timestamp(self, value: Any) -> float | None:
        # Persisted session-runtime timestamps are wall-clock UTC ISO-8601
        # strings produced by ``SessionHost.current_timestamp``. They must be
        # comparable across processes and after restarts, so numeric
        # ``time.monotonic()`` values are intentionally rejected: monotonic
        # time is process-local and meaningless once persisted or read back by
        # another process. Non-string values (including floats) yield ``None``
        # rather than being treated as monotonic.
        if not isinstance(value, str):
            return None
        try:
            then = datetime.fromisoformat(value)
        except ValueError:
            return None
        if then.tzinfo is None:
            then = then.replace(tzinfo=UTC)
        return (datetime.now(UTC) - then).total_seconds()

    def _record_session_selection_event(self, session_id: int, track: dict[str, Any], *, selection_strategy: str) -> None:
        event_type = "track_selected"
        if selection_strategy == "adaptive-session-auto-advance":
            event_type = "track_auto_advanced"
        elif selection_strategy == "adaptive-session-skip":
            event_type = "track_manual_skip"
        elif selection_strategy == "adaptive-session-steer":
            event_type = "track_manually_steered"
        self._preferences.add_session_event(
            session_id,
            event_type=event_type,
            track={
                "track_id": _clean_id(track.get("play_params", {}).get("id")) or _clean_id(track.get("id")),
                "title": track.get("title"),
                "artist": track.get("artist"),
                "album": track.get("album"),
                "href": track.get("href"),
            },
            metadata={"selection_strategy": selection_strategy},
        )
