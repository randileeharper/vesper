"""Session queue persistence for Vesper's SQLite store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..errors import PreferenceStoreError
from .schema import connect


# Upper bound on retry attempts when a queued row is taken by another caller
# between the SELECT and the conditional UPDATE in claim_next_session_queue_item.
# Each iteration re-reads the next available queued row, so this only retries
# under genuine contention; the common uncontended path returns on attempt 1.
_CLAIM_MAX_RETRIES = 8


def replace_session_queue(
    database_path: Path,
    session_id: int,
    items: list[dict[str, Any]],
    *,
    preserve_history: bool = False,
) -> None:
    try:
        with connect(database_path) as connection:
            if preserve_history:
                connection.execute(
                    """
                    UPDATE session_queue_items
                    SET state = 'filtered', updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ? AND state = 'queued'
                    """,
                    (session_id,),
                )
            else:
                connection.execute("DELETE FROM session_queue_items WHERE session_id = ?", (session_id,))
            position_start = _next_session_queue_position(connection, session_id)
            for offset, item in enumerate(items):
                track = dict(item.get("track") or {})
                source = dict(item.get("source") or {})
                track_id = _queue_track_id(track)
                connection.execute(
                    """
                    INSERT INTO session_queue_items(
                        session_id,
                        position,
                        source_kind,
                        source_term,
                        source_key,
                        track_id,
                        title,
                        artist,
                        album,
                        href,
                        track_json,
                        state
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
                    """,
                    (
                        session_id,
                        position_start + offset,
                        source.get("kind"),
                        source.get("term"),
                        item.get("source_key"),
                        track_id,
                        track.get("title"),
                        track.get("artist"),
                        track.get("album"),
                        track.get("href"),
                        json.dumps(track, ensure_ascii=True),
                    ),
                )
            connection.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not replace session queue: {exc}") from exc


def append_session_queue(database_path: Path, session_id: int, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    try:
        with connect(database_path) as connection:
            position_start = _next_session_queue_position(connection, session_id)
            for offset, item in enumerate(items):
                track = dict(item.get("track") or {})
                source = dict(item.get("source") or {})
                track_id = _queue_track_id(track)
                connection.execute(
                    """
                    INSERT INTO session_queue_items(
                        session_id,
                        position,
                        source_kind,
                        source_term,
                        source_key,
                        track_id,
                        title,
                        artist,
                        album,
                        href,
                        track_json,
                        state
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
                    """,
                    (
                        session_id,
                        position_start + offset,
                        source.get("kind"),
                        source.get("term"),
                        item.get("source_key"),
                        track_id,
                        track.get("title"),
                        track.get("artist"),
                        track.get("album"),
                        track.get("href"),
                        json.dumps(track, ensure_ascii=True),
                    ),
                )
            connection.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not append session queue: {exc}") from exc


def list_session_queue(
    database_path: Path,
    session_id: int,
    *,
    limit: int = 50,
    include_history: bool = False,
) -> list[dict[str, Any]]:
    where_state = "" if include_history else "AND state IN ('queued', 'playing')"
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT
                id,
                session_id,
                position,
                source_kind,
                source_term,
                source_key,
                track_id,
                title,
                artist,
                album,
                href,
                track_json,
                state,
                created_at,
                updated_at
            FROM session_queue_items
            WHERE session_id = ?
            {where_state}
            ORDER BY position ASC, id ASC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [_decode_session_queue_row(row) for row in rows]


def claim_next_session_queue_item(database_path: Path, session_id: int) -> dict[str, Any] | None:
    try:
        with connect(database_path) as connection:
            # Claim atomically: UPDATE only if the row is still 'queued', then
            # inspect rowcount. This closes a SELECT-then-UPDATE race where two
            # concurrent callers (e.g. the background refill worker and a manual
            # refill_active_session call) could both read the same 'queued' row
            # before either updates it, claiming the same item twice. If the
            # row is no longer queued (another caller won), retry the lookup a
            # bounded number of times before concluding nothing is available.
            for _ in range(_CLAIM_MAX_RETRIES):
                row = connection.execute(
                    """
                    SELECT
                        id,
                        session_id,
                        position,
                        source_kind,
                        source_term,
                        source_key,
                        track_id,
                        title,
                        artist,
                        album,
                        href,
                        track_json,
                        state,
                        created_at,
                        updated_at
                    FROM session_queue_items
                    WHERE session_id = ? AND state = 'queued'
                    ORDER BY position ASC, id ASC
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    return None
                cursor = connection.execute(
                    """
                    UPDATE session_queue_items
                    SET state = 'playing', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND state = 'queued'
                    """,
                    (int(row["id"]),),
                )
                if cursor.rowcount == 1:
                    claimed = _decode_session_queue_row(row)
                    claimed["state"] = "playing"
                    return claimed
                # Another caller claimed this row between our SELECT and
                # UPDATE; loop to try the next queued row.
            return None
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not claim next session queue item: {exc}") from exc


def get_session_queue_item(database_path: Path, queue_item_id: int) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                session_id,
                position,
                source_kind,
                source_term,
                source_key,
                track_id,
                title,
                artist,
                album,
                href,
                track_json,
                state,
                created_at,
                updated_at
            FROM session_queue_items
            WHERE id = ?
            """,
            (queue_item_id,),
        ).fetchone()
    return _decode_session_queue_row(row) if row is not None else None


def mark_session_queue_item(database_path: Path, queue_item_id: int, state: str) -> None:
    if state not in {"queued", "playing", "played", "rejected", "filtered", "failed"}:
        raise PreferenceStoreError(f"Unsupported session queue state: {state}")
    try:
        with connect(database_path) as connection:
            connection.execute(
                """
                UPDATE session_queue_items
                SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (state, queue_item_id),
            )
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not mark session queue item: {exc}") from exc


def mark_session_queue_track(database_path: Path, session_id: int, track_id: str, state: str) -> int:
    if state not in {"queued", "playing", "played", "rejected", "filtered", "failed"}:
        raise PreferenceStoreError(f"Unsupported session queue state: {state}")
    try:
        with connect(database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE session_queue_items
                SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ? AND track_id = ? AND state IN ('queued', 'playing')
                """,
                (state, session_id, track_id),
            )
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not mark session queue track: {exc}") from exc
    return int(cursor.rowcount)


def reset_stale_session_queue_items(database_path: Path, session_id: int) -> None:
    try:
        with connect(database_path) as connection:
            connection.execute(
                """
                UPDATE session_queue_items
                SET state = 'queued', updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ? AND state = 'playing'
                """,
                (session_id,),
            )
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not reset stale session queue items: {exc}") from exc


def _next_session_queue_position(connection: sqlite3.Connection, session_id: int) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM session_queue_items WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["next_position"]) if row is not None else 0


def _queue_track_id(track: dict[str, Any]) -> str | None:
    track_id = str(track.get("id") or track.get("play_params", {}).get("id") or "").strip()
    return track_id or None


def _decode_session_queue_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        track = json.loads(row["track_json"])
    except (TypeError, json.JSONDecodeError):
        track = {}
    if not isinstance(track, dict):
        track = {}
    return {
        "id": int(row["id"]),
        "session_id": int(row["session_id"]),
        "position": int(row["position"]),
        "source": {
            "kind": row["source_kind"],
            "term": row["source_term"],
        },
        "source_key": row["source_key"],
        "track_id": row["track_id"],
        "title": row["title"],
        "artist": row["artist"],
        "album": row["album"],
        "href": row["href"],
        "track": track,
        "state": row["state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
