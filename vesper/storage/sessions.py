"""Session lifecycle persistence for Vesper's SQLite store."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..errors import PreferenceStoreError
from .schema import connect


# Serializes session lifecycle mutations (start/stop) per database so that two
# concurrent start_session calls can't interleave their deactivate-all-then-insert
# and leave more than one active session. Keyed by resolved database path so that
# independent stores pointing at the same file share one lock.
_lifecycle_locks: dict[Path, threading.Lock] = {}
_lifecycle_locks_guard = threading.Lock()


def _lifecycle_lock(database_path: Path) -> threading.Lock:
    key = Path(database_path)
    with _lifecycle_locks_guard:
        lock = _lifecycle_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _lifecycle_locks[key] = lock
        return lock


def _decode_session_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        steering_history = json.loads(row["steering_history_json"])
    except (TypeError, json.JSONDecodeError):
        steering_history = []
    return {
        "id": int(row["id"]),
        "request_text": row["request_text"],
        "steering_history": steering_history if isinstance(steering_history, list) else [],
        "mode": row["mode"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_refilled_at": row["last_refilled_at"],
    }


def get_active_session(database_path: Path) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT id, request_text, steering_history_json, mode, is_active, created_at, updated_at, last_refilled_at
            FROM sessions
            WHERE is_active = 1
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    return _decode_session_row(row) if row is not None else None


def start_session(database_path: Path, *, request_text: str, mode: str = "adaptive") -> dict[str, Any]:
    with _lifecycle_lock(database_path):
        try:
            with connect(database_path) as connection:
                connection.execute("UPDATE sessions SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE is_active = 1")
                cursor = connection.execute(
                    """
                    INSERT INTO sessions(request_text, steering_history_json, mode, is_active)
                    VALUES (?, '[]', ?, 1)
                    """,
                    (request_text, mode),
                )
                session_id = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise PreferenceStoreError(f"Could not start session: {exc}") from exc
        session = get_session(database_path, session_id)
        if session is None:
            raise PreferenceStoreError(f"Session {session_id} was not found after creation.")
        return session


def get_session(database_path: Path, session_id: int) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT id, request_text, steering_history_json, mode, is_active, created_at, updated_at, last_refilled_at
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
    return _decode_session_row(row) if row is not None else None


def add_session_steering(database_path: Path, session_id: int, steering_text: str) -> dict[str, Any]:
    session = get_session(database_path, session_id)
    if session is None:
        raise PreferenceStoreError(f"Session {session_id} was not found.")
    steering_history = list(session.get("steering_history", []))
    steering_history.append(steering_text)
    try:
        with connect(database_path) as connection:
            connection.execute(
                """
                UPDATE sessions
                SET steering_history_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (json.dumps(steering_history, ensure_ascii=True), session_id),
            )
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not update session steering: {exc}") from exc
    updated = get_session(database_path, session_id)
    if updated is None:
        raise PreferenceStoreError(f"Session {session_id} was not found after update.")
    return updated


def touch_session_refill(database_path: Path, session_id: int) -> None:
    try:
        with connect(database_path) as connection:
            connection.execute(
                """
                UPDATE sessions
                SET updated_at = CURRENT_TIMESTAMP, last_refilled_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (session_id,),
            )
    except sqlite3.Error as exc:
        raise PreferenceStoreError(f"Could not update session refill timestamp: {exc}") from exc


def stop_active_session(database_path: Path) -> dict[str, Any] | None:
    with _lifecycle_lock(database_path):
        session = get_active_session(database_path)
        if session is None:
            return None
        try:
            with connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE sessions
                    SET is_active = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (session["id"],),
                )
                connection.execute("DELETE FROM session_runtime WHERE session_id = ?", (session["id"],))
        except sqlite3.Error as exc:
            raise PreferenceStoreError(f"Could not stop active session: {exc}") from exc
        session["is_active"] = False
        return session
