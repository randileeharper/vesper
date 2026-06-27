"""Music preference persistence for Vesper's SQLite store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..errors import PreferenceStoreError
from .schema import connect


def list_preferences(database_path: Path) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                preference_type,
                track_id,
                title,
                artist_id,
                artist_name,
                artist_key,
                album,
                item_kind,
                is_library,
                session_request_text,
                session_search_query,
                created_at,
                updated_at
            FROM music_preferences
            ORDER BY preference_type ASC, updated_at DESC, id DESC
            """
        ).fetchall()
    return [_decode_music_preference_row(row) for row in rows]


def record_global_rejected_track(
    database_path: Path,
    *,
    track_id: str,
    title: str | None = None,
    artist_name: str | None = None,
    album: str | None = None,
    item_kind: str | None = None,
    is_library: bool | None = None,
) -> dict[str, Any]:
    return _upsert_music_preference(
        database_path,
        preference_type="globally_rejected_track",
        match_field="track_id",
        match_value=track_id,
        payload={
            "track_id": track_id,
            "title": title,
            "artist_name": artist_name,
            "album": album,
            "item_kind": item_kind,
            "is_library": int(is_library) if is_library is not None else None,
        },
    )


def record_liked_track(
    database_path: Path,
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
    return _upsert_music_preference(
        database_path,
        preference_type="liked_track",
        match_field="track_id",
        match_value=track_id,
        payload={
            "track_id": track_id,
            "title": title,
            "artist_id": artist_id,
            "artist_name": artist_name,
            "album": album,
            "item_kind": item_kind,
            "is_library": int(is_library) if is_library is not None else None,
            "session_request_text": session_request_text,
            "session_search_query": session_search_query,
        },
    )


def record_favored_artist(
    database_path: Path,
    *,
    artist_id: str | None = None,
    artist_name: str | None = None,
    session_request_text: str | None = None,
    session_search_query: str | None = None,
) -> dict[str, Any]:
    artist_key = _normalize_artist_key(artist_id=artist_id, artist_name=artist_name)
    if artist_key is None:
        raise PreferenceStoreError("Could not save favored artist without an artist id or name.")
    return _upsert_music_preference(
        database_path,
        preference_type="favored_artist",
        match_field="artist_key",
        match_value=artist_key,
        payload={
            "artist_id": artist_id,
            "artist_name": artist_name,
            "artist_key": artist_key,
            "session_request_text": session_request_text,
            "session_search_query": session_search_query,
        },
    )


def liked_tracks(database_path: Path) -> list[dict[str, Any]]:
    return _list_music_preferences_by_type(database_path, "liked_track")


def favored_artists(database_path: Path) -> list[dict[str, Any]]:
    return _list_music_preferences_by_type(database_path, "favored_artist")


def globally_rejected_tracks(database_path: Path) -> list[dict[str, Any]]:
    return _list_music_preferences_by_type(database_path, "globally_rejected_track")


def globally_rejected_track_ids(database_path: Path) -> set[str]:
    return {
        str(item["track_id"]).strip()
        for item in globally_rejected_tracks(database_path)
        if str(item.get("track_id", "")).strip()
    }


def delete_preference(database_path: Path, preference_id: int) -> bool:
    try:
        with connect(database_path) as connection:
            cursor = connection.execute("DELETE FROM music_preferences WHERE id = ?", (preference_id,))
            return cursor.rowcount > 0
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not delete preference: {exc}") from exc


def get_preference(database_path: Path, preference_id: int) -> dict[str, Any]:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                preference_type,
                track_id,
                title,
                artist_id,
                artist_name,
                artist_key,
                album,
                item_kind,
                is_library,
                session_request_text,
                session_search_query,
                created_at,
                updated_at
            FROM music_preferences
            WHERE id = ?
            """,
            (preference_id,),
        ).fetchone()
    if row is None:
        raise PreferenceStoreError(f"Preference {preference_id} was not found.")
    return _decode_music_preference_row(row)


def _upsert_music_preference(
    database_path: Path,
    *,
    preference_type: str,
    match_field: str,
    match_value: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not str(match_value).strip():
        raise PreferenceStoreError(f"Could not save {preference_type}: missing match value.")
    if match_field not in {"track_id", "artist_key"}:
        raise PreferenceStoreError(f"Could not save {preference_type}: unsupported match field {match_field}.")
    columns = (
        "preference_type",
        "track_id",
        "title",
        "artist_id",
        "artist_name",
        "artist_key",
        "album",
        "item_kind",
        "is_library",
        "session_request_text",
        "session_search_query",
    )
    try:
        with connect(database_path) as connection:
            existing = connection.execute(
                f"""
                SELECT id FROM music_preferences
                WHERE preference_type = ? AND COALESCE({match_field}, '') = COALESCE(?, '')
                """,
                (preference_type, match_value),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    f"""
                    INSERT INTO music_preferences({", ".join(columns)})
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(payload.get(column) if column != "preference_type" else preference_type for column in columns),
                )
                preference_id = int(cursor.lastrowid)
            else:
                preference_id = int(existing["id"])
                assignments = ", ".join(f"{column} = ?" for column in columns[1:])
                connection.execute(
                    f"""
                    UPDATE music_preferences
                    SET {assignments}, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    tuple(payload.get(column) for column in columns[1:]) + (preference_id,),
                )
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not save {preference_type}: {exc}") from exc
    return get_preference(database_path, preference_id)


def _list_music_preferences_by_type(database_path: Path, preference_type: str) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                preference_type,
                track_id,
                title,
                artist_id,
                artist_name,
                artist_key,
                album,
                item_kind,
                is_library,
                session_request_text,
                session_search_query,
                created_at,
                updated_at
            FROM music_preferences
            WHERE preference_type = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (preference_type,),
        ).fetchall()
    return [_decode_music_preference_row(row) for row in rows]


def _decode_music_preference_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "preference_type": row["preference_type"],
        "track_id": row["track_id"],
        "title": row["title"],
        "artist_id": row["artist_id"],
        "artist_name": row["artist_name"],
        "artist_key": row["artist_key"],
        "album": row["album"],
        "item_kind": row["item_kind"],
        "is_library": None if row["is_library"] is None else bool(row["is_library"]),
        "session_request_text": row["session_request_text"],
        "session_search_query": row["session_search_query"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _normalize_artist_key(*, artist_id: str | None, artist_name: str | None) -> str | None:
    if artist_id is not None and str(artist_id).strip():
        return f"id:{str(artist_id).strip()}"
    if artist_name is not None and str(artist_name).strip():
        normalized = " ".join(str(artist_name).strip().casefold().split())
        return f"name:{normalized}"
    return None
