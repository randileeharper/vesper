"""SQLite-backed persistence for Vesper.

The single ``PreferenceStore`` class is preserved as a thin facade that
delegates to cohesive per-concern modules in this package. Callers continue
to construct ``PreferenceStore(database_path)`` exactly as before; each
public method forwards to the module that owns that concern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import preferences, schema, session_data, session_queue, sessions


class PreferenceStore:
    """Store explicit music preferences and session state in SQLite.

    A thin facade over the :mod:`vesper.storage` submodules. Each public
    method delegates to the module owning that concern while preserving the
    original API and per-operation connection behavior.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        schema.initialize(self._database_path)

    # --- Preferences -----------------------------------------------------

    def list_preferences(self) -> list[dict[str, Any]]:
        return preferences.list_preferences(self._database_path)

    def record_global_rejected_track(
        self,
        *,
        track_id: str,
        title: str | None = None,
        artist_name: str | None = None,
        album: str | None = None,
        item_kind: str | None = None,
        is_library: bool | None = None,
    ) -> dict[str, Any]:
        return preferences.record_global_rejected_track(
            self._database_path,
            track_id=track_id,
            title=title,
            artist_name=artist_name,
            album=album,
            item_kind=item_kind,
            is_library=is_library,
        )

    def record_liked_track(
        self,
        *,
        track_id: str,
        title: str | None = None,
        artist_id: str | None = None,
        artist_name: str | None = None,
        album: str | None = None,
        item_kind: str | None = None,
        is_library: bool | None = None,
        session_request_text: str | None = None,
        session_search_query: str | None = None,
    ) -> dict[str, Any]:
        return preferences.record_liked_track(
            self._database_path,
            track_id=track_id,
            title=title,
            artist_id=artist_id,
            artist_name=artist_name,
            album=album,
            item_kind=item_kind,
            is_library=is_library,
            session_request_text=session_request_text,
            session_search_query=session_search_query,
        )

    def record_favored_artist(
        self,
        *,
        artist_id: str | None = None,
        artist_name: str | None = None,
        session_request_text: str | None = None,
        session_search_query: str | None = None,
    ) -> dict[str, Any]:
        return preferences.record_favored_artist(
            self._database_path,
            artist_id=artist_id,
            artist_name=artist_name,
            session_request_text=session_request_text,
            session_search_query=session_search_query,
        )

    def liked_tracks(self) -> list[dict[str, Any]]:
        return preferences.liked_tracks(self._database_path)

    def favored_artists(self) -> list[dict[str, Any]]:
        return preferences.favored_artists(self._database_path)

    def globally_rejected_tracks(self) -> list[dict[str, Any]]:
        return preferences.globally_rejected_tracks(self._database_path)

    def globally_rejected_track_ids(self) -> set[str]:
        return preferences.globally_rejected_track_ids(self._database_path)

    def delete_preference(self, preference_id: int) -> bool:
        return preferences.delete_preference(self._database_path, preference_id)

    def get_preference(self, preference_id: int) -> dict[str, Any]:
        return preferences.get_preference(self._database_path, preference_id)

    # --- Session lifecycle -----------------------------------------------

    def get_active_session(self) -> dict[str, Any] | None:
        return sessions.get_active_session(self._database_path)

    def start_session(self, *, request_text: str, mode: str = "adaptive") -> dict[str, Any]:
        return sessions.start_session(self._database_path, request_text=request_text, mode=mode)

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        return sessions.get_session(self._database_path, session_id)

    def add_session_steering(self, session_id: int, steering_text: str) -> dict[str, Any]:
        return sessions.add_session_steering(self._database_path, session_id, steering_text)

    def touch_session_refill(self, session_id: int) -> None:
        return sessions.touch_session_refill(self._database_path, session_id)

    def stop_active_session(self) -> dict[str, Any] | None:
        return sessions.stop_active_session(self._database_path)

    # --- Session queue ---------------------------------------------------

    def replace_session_queue(
        self,
        session_id: int,
        items: list[dict[str, Any]],
        *,
        preserve_history: bool = False,
    ) -> None:
        return session_queue.replace_session_queue(
            self._database_path,
            session_id,
            items,
            preserve_history=preserve_history,
        )

    def append_session_queue(self, session_id: int, items: list[dict[str, Any]]) -> None:
        return session_queue.append_session_queue(self._database_path, session_id, items)

    def list_session_queue(
        self,
        session_id: int,
        *,
        limit: int = 50,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        return session_queue.list_session_queue(
            self._database_path,
            session_id,
            limit=limit,
            include_history=include_history,
        )

    def claim_next_session_queue_item(self, session_id: int) -> dict[str, Any] | None:
        return session_queue.claim_next_session_queue_item(self._database_path, session_id)

    def get_session_queue_item(self, queue_item_id: int) -> dict[str, Any] | None:
        return session_queue.get_session_queue_item(self._database_path, queue_item_id)

    def mark_session_queue_item(self, queue_item_id: int, state: str) -> None:
        return session_queue.mark_session_queue_item(self._database_path, queue_item_id, state)

    def mark_session_queue_track(self, session_id: int, track_id: str, state: str) -> int:
        return session_queue.mark_session_queue_track(self._database_path, session_id, track_id, state)

    def reset_stale_session_queue_items(self, session_id: int) -> None:
        return session_queue.reset_stale_session_queue_items(self._database_path, session_id)

    # --- Session tracks, events & runtime --------------------------------

    def add_session_track(self, session_id: int, track: dict[str, Any]) -> None:
        return session_data.add_session_track(self._database_path, session_id, track)

    def list_session_tracks(self, session_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        return session_data.list_session_tracks(self._database_path, session_id, limit=limit)

    def list_recent_tracks(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return session_data.list_recent_tracks(self._database_path, limit=limit)

    def get_session_runtime(self, session_id: int) -> dict[str, Any] | None:
        return session_data.get_session_runtime(self._database_path, session_id)

    def upsert_session_runtime(
        self,
        session_id: int,
        *,
        active_intent: str | None = None,
        last_advance_at: str | None = None,
        last_selected_track_id: str | None = None,
        last_known_playback_state: str | None = None,
    ) -> dict[str, Any]:
        return session_data.upsert_session_runtime(
            self._database_path,
            session_id,
            active_intent=active_intent,
            last_advance_at=last_advance_at,
            last_selected_track_id=last_selected_track_id,
            last_known_playback_state=last_known_playback_state,
        )

    def update_session_pending_stop(
        self,
        session_id: int,
        *,
        track_id: str | None,
        observed_at: str | None,
    ) -> None:
        return session_data.update_session_pending_stop(
            self._database_path,
            session_id,
            track_id=track_id,
            observed_at=observed_at,
        )

    def clear_session_runtime(self, session_id: int) -> None:
        return session_data.clear_session_runtime(self._database_path, session_id)

    def add_session_event(
        self,
        session_id: int,
        *,
        event_type: str,
        track: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return session_data.add_session_event(
            self._database_path,
            session_id,
            event_type=event_type,
            track=track,
            metadata=metadata,
        )

    def list_session_events(
        self,
        session_id: int,
        *,
        limit: int = 50,
        event_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return session_data.list_session_events(
            self._database_path,
            session_id,
            limit=limit,
            event_types=event_types,
        )


__all__ = ["PreferenceStore"]
