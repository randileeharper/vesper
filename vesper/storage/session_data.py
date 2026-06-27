"""Session tracks, events, and runtime persistence for Vesper's SQLite store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..errors import PreferenceStoreError
from .schema import connect


def add_session_track(database_path: Path, session_id: int, track: dict[str, Any]) -> None:
    try:
        with connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO session_tracks(session_id, track_id, title, artist, album, href)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    track.get("id"),
                    track.get("title"),
                    track.get("artist"),
                    track.get("album"),
                    track.get("href"),
                ),
            )
            connection.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not record session track: {exc}") from exc


def list_session_tracks(database_path: Path, session_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT track_id, title, artist, album, href, recorded_at
            FROM session_tracks
            WHERE session_id = ?
            ORDER BY recorded_at DESC, id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    tracks: list[dict[str, Any]] = []
    for row in rows:
        track_id = str(row["track_id"]).strip() if row["track_id"] is not None else ""
        if not track_id or track_id.lower() == "none":
            continue
        tracks.append(
            {
                "track_id": track_id,
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "href": row["href"],
                "recorded_at": row["recorded_at"],
            }
        )
    return tracks


def list_recent_tracks(database_path: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT track_id, title, artist, album, href, recorded_at, session_id
            FROM session_tracks
            ORDER BY recorded_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    tracks: list[dict[str, Any]] = []
    for row in rows:
        track_id = str(row["track_id"]).strip() if row["track_id"] is not None else ""
        if not track_id or track_id.lower() == "none":
            continue
        tracks.append(
            {
                "session_id": int(row["session_id"]),
                "track_id": track_id,
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "href": row["href"],
                "recorded_at": row["recorded_at"],
            }
        )
    return tracks


def get_session_runtime(database_path: Path, session_id: int) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT session_id, active_intent, last_advance_at, last_selected_track_id, last_known_playback_state, pending_stop_track_id, pending_stop_observed_at, updated_at
            FROM session_runtime
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "session_id": int(row["session_id"]),
        "active_intent": row["active_intent"],
        "last_advance_at": row["last_advance_at"],
        "last_selected_track_id": row["last_selected_track_id"],
        "last_known_playback_state": row["last_known_playback_state"],
        "pending_stop_track_id": row["pending_stop_track_id"],
        "pending_stop_observed_at": row["pending_stop_observed_at"],
        "updated_at": row["updated_at"],
    }


def upsert_session_runtime(
    database_path: Path,
    session_id: int,
    *,
    active_intent: str | None = None,
    last_advance_at: str | None = None,
    last_selected_track_id: str | None = None,
    last_known_playback_state: str | None = None,
) -> dict[str, Any]:
    current = get_session_runtime(database_path, session_id) or {
        "active_intent": "active",
        "last_advance_at": None,
        "last_selected_track_id": None,
        "last_known_playback_state": None,
    }
    resolved_active_intent = current["active_intent"] if active_intent is None else active_intent
    resolved_last_advance_at = current["last_advance_at"] if last_advance_at is None else last_advance_at
    resolved_last_selected_track_id = (
        current["last_selected_track_id"] if last_selected_track_id is None else last_selected_track_id
    )
    resolved_last_known_playback_state = (
        current["last_known_playback_state"] if last_known_playback_state is None else last_known_playback_state
    )
    try:
        with connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO session_runtime(
                    session_id,
                    active_intent,
                    last_advance_at,
                    last_selected_track_id,
                    last_known_playback_state,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    active_intent = excluded.active_intent,
                    last_advance_at = excluded.last_advance_at,
                    last_selected_track_id = excluded.last_selected_track_id,
                    last_known_playback_state = excluded.last_known_playback_state,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    session_id,
                    resolved_active_intent,
                    resolved_last_advance_at,
                    resolved_last_selected_track_id,
                    resolved_last_known_playback_state,
                ),
            )
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not update session runtime: {exc}") from exc
    runtime = get_session_runtime(database_path, session_id)
    if runtime is None:
        raise PreferenceStoreError(f"Session runtime for session {session_id} was not found after update.")
    return runtime


def update_session_pending_stop(
    database_path: Path,
    session_id: int,
    *,
    track_id: str | None,
    observed_at: str | None,
) -> None:
    """Persist (or clear, when both are ``None``) the two-snapshot stop
    confirmation so it survives a process restart. A focused UPDATE: it
    touches only these columns and is a no-op when no runtime row exists,
    which is fine because a stop is only ever evaluated for an active,
    already-persisted session."""
    try:
        with connect(database_path) as connection:
            connection.execute(
                """
                UPDATE session_runtime
                SET pending_stop_track_id = ?, pending_stop_observed_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                (track_id, observed_at, session_id),
            )
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not update session pending stop: {exc}") from exc


def clear_session_runtime(database_path: Path, session_id: int) -> None:
    try:
        with connect(database_path) as connection:
            connection.execute("DELETE FROM session_runtime WHERE session_id = ?", (session_id,))
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not clear session runtime: {exc}") from exc


def add_session_event(
    database_path: Path,
    session_id: int,
    *,
    event_type: str,
    track: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    track = track or {}
    try:
        with connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO session_events(
                    session_id,
                    event_type,
                    track_id,
                    title,
                    artist,
                    album,
                    href,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    event_type,
                    track.get("track_id") or track.get("id"),
                    track.get("title"),
                    track.get("artist"),
                    track.get("album"),
                    track.get("href"),
                    json.dumps(metadata or {}, ensure_ascii=True),
                ),
            )
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not record session event: {exc}") from exc


def list_session_events(
    database_path: Path,
    session_id: int,
    *,
    limit: int = 50,
    event_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            rows = connection.execute(
                f"""
                SELECT id, session_id, event_type, track_id, title, artist, album, href, metadata_json, created_at
                FROM session_events
                WHERE session_id = ? AND event_type IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (session_id, *event_types, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, session_id, event_type, track_id, title, artist, album, href, metadata_json, created_at
                FROM session_events
                WHERE session_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        events.append(
            {
                "id": int(row["id"]),
                "session_id": int(row["session_id"]),
                "event_type": row["event_type"],
                "track_id": row["track_id"],
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "href": row["href"],
                "metadata": metadata if isinstance(metadata, dict) else {},
                "created_at": row["created_at"],
            }
        )
    return events
