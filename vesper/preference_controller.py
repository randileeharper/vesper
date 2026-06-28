"""Preference management for the Cider agent.

Extracted from the :class:`vesper.service.CiderAgentService` god-class
(issue #42). This controller owns the positive/negative preference lifecycle:
listing, recording likes, and forgetting. It reaches cross-cutting
capabilities (historian emission, caller context, playback snapshot, session
runtime) through the :class:`PreferenceHost` Protocol so it never imports the
concrete service class.
"""

from __future__ import annotations

from typing import Any, Protocol

from .errors import CiderValidationError, PreferenceStoreError
from .storage import PreferenceStore
from .utils import _clean_id, _preference_target


class PreferenceHost(Protocol):
    """Structural interface for the cross-cutting capabilities
    :class:`PreferenceController` borrows from its host."""

    def playback_snapshot(self) -> dict[str, Any]:
        ...

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

    def _get_session_runtime(self, session_id: int) -> dict[str, Any]:
        ...

    def _current_preference_context_query(self, runtime: dict[str, Any]) -> str | None:
        ...

    def _get_active_session(self) -> dict[str, Any] | None:
        """Return the active session dict, or None."""
        ...


class PreferenceController:
    """Manages like/reject/list preferences and their historian events."""

    def __init__(self, host: PreferenceHost, *, preferences: PreferenceStore) -> None:
        self._host = host
        self._preferences = preferences

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

    def forget_preference(self, preference_id: int) -> dict[str, Any]:
        preference = None
        try:
            preference = self._preferences.get_preference(preference_id)
        except PreferenceStoreError:
            # ``get_preference`` raises this when the row is missing. We treat
            # a missing preference as already forgotten (idempotent delete)
            # and fall through to the delete below, which surfaces a clean
            # CiderValidationError if the row truly is gone. Unexpected errors
            # are not caught so they propagate.
            pass
        removed = self._preferences.delete_preference(preference_id)
        if not removed:
            raise CiderValidationError(f"Preference {preference_id} was not found.")
        self._host._emit(
            "music.preference.forgotten",
            {
                "caller": self._host._caller(),
                "preference_id": preference_id,
                "preference_type": preference.get("preference_type") if preference else None,
                "target": _preference_target(preference),
            },
            source="app://vesper/preferences",
            subject=str(preference_id),
        )
        return {"status": "ok", "removed": True, "preference_id": preference_id}

    def like_current_track(self) -> dict[str, Any]:
        playback = self._host.playback_snapshot()
        current = playback.get("track", {})
        current_id = _clean_id(current.get("track_id"))
        if not current_id:
            raise CiderValidationError("No current track is available to like.")
        session = self._host._get_active_session()
        runtime = self._host._get_session_runtime(session["id"]) if session is not None else {}
        liked_track = self._preferences.record_liked_track(
            track_id=current_id,
            title=str(current.get("title", "")).strip() or None,
            artist_name=str(current.get("artist", "")).strip() or None,
            album=str(current.get("album", "")).strip() or None,
            item_kind=str(current.get("kind", "")).strip() or None,
            is_library=bool(current.get("is_library")) if current.get("is_library") is not None else None,
            session_request_text=str(session.get("request_text", "")).strip() or None if session is not None else None,
            session_search_query=self._host._current_preference_context_query(runtime),
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
        self._host._emit(
            "music.preference.recorded",
            {
                "caller": self._host._caller(),
                "preference_id": liked_track["id"],
                "preference_type": liked_track["preference_type"],
                "polarity": "like",
                "target": _preference_target(liked_track),
                "reason": None,
            },
            source="app://vesper/preferences",
            subject=str(liked_track["id"]),
            session_id=session["id"] if session else None,
        )
        if favored_artist is not None:
            self._host._emit(
                "music.preference.recorded",
                {
                    "caller": self._host._caller(),
                    "preference_id": favored_artist["id"],
                    "preference_type": favored_artist["preference_type"],
                    "polarity": "prefer",
                    "target": _preference_target(favored_artist),
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
