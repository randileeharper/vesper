"""SQLite-backed persistence for Vesper."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .errors import PreferenceStoreError


class PreferenceStore:
    """Store explicit music preferences and session state in SQLite."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        # Serializes session lifecycle mutations (start/stop) so that two
        # concurrent start_session calls can't interleave their
        # deactivate-all-then-insert and leave more than one active session.
        self._lifecycle_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            # Legacy table retained for existing installs; new code does not write to it.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    category TEXT,
                    value TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    note TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_preferences_unique
                ON preferences(kind, COALESCE(category, ''), value)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS music_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preference_type TEXT NOT NULL,
                    track_id TEXT,
                    title TEXT,
                    artist_id TEXT,
                    artist_name TEXT,
                    artist_key TEXT,
                    album TEXT,
                    item_kind TEXT,
                    is_library INTEGER,
                    session_request_text TEXT,
                    session_search_query TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_music_preferences_track_unique
                ON music_preferences(preference_type, track_id)
                WHERE track_id IS NOT NULL AND track_id != ''
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_music_preferences_artist_unique
                ON music_preferences(preference_type, artist_key)
                WHERE artist_key IS NOT NULL AND artist_key != ''
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_text TEXT NOT NULL,
                    steering_history_json TEXT NOT NULL DEFAULT '[]',
                    mode TEXT NOT NULL DEFAULT 'adaptive',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_refilled_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    track_id TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    href TEXT,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_tracks_session_recorded
                ON session_tracks(session_id, recorded_at DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_runtime (
                    session_id INTEGER PRIMARY KEY,
                    active_intent TEXT NOT NULL DEFAULT 'active',
                    last_advance_at TEXT,
                    last_selected_track_id TEXT,
                    last_known_playback_state TEXT,
                    pending_stop_track_id TEXT,
                    pending_stop_observed_at TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                )
                """
            )
            self._ensure_session_runtime_columns(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    track_id TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    href TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_events_session_created
                ON session_events(session_id, created_at DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_queue_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    source_kind TEXT,
                    source_term TEXT,
                    source_key TEXT,
                    track_id TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    href TEXT,
                    track_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_queue_items_session_state_position
                ON session_queue_items(session_id, state, position, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_queue_items_session_track
                ON session_queue_items(session_id, track_id)
                """
            )

    def _ensure_session_runtime_columns(self, connection: sqlite3.Connection) -> None:
        # ``ALTER TABLE ... ADD COLUMN`` has no ``IF NOT EXISTS`` in SQLite, so
        # backfill the pending-stop confirmation columns for databases created
        # before they existed. New databases get them from CREATE TABLE above.
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(session_runtime)")}
        if "pending_stop_track_id" not in existing:
            connection.execute("ALTER TABLE session_runtime ADD COLUMN pending_stop_track_id TEXT")
        if "pending_stop_observed_at" not in existing:
            connection.execute("ALTER TABLE session_runtime ADD COLUMN pending_stop_observed_at TEXT")

    def list_preferences(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
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
        return [self._decode_music_preference_row(row) for row in rows]

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
        return self._upsert_music_preference(
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
        return self._upsert_music_preference(
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
        self,
        *,
        artist_id: str | None = None,
        artist_name: str | None = None,
        session_request_text: str | None = None,
        session_search_query: str | None = None,
    ) -> dict[str, Any]:
        artist_key = self._normalize_artist_key(artist_id=artist_id, artist_name=artist_name)
        if artist_key is None:
            raise PreferenceStoreError("Could not save favored artist without an artist id or name.")
        return self._upsert_music_preference(
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

    def liked_tracks(self) -> list[dict[str, Any]]:
        return self._list_music_preferences_by_type("liked_track")

    def favored_artists(self) -> list[dict[str, Any]]:
        return self._list_music_preferences_by_type("favored_artist")

    def globally_rejected_tracks(self) -> list[dict[str, Any]]:
        return self._list_music_preferences_by_type("globally_rejected_track")

    def globally_rejected_track_ids(self) -> set[str]:
        return {
            str(item["track_id"]).strip()
            for item in self.globally_rejected_tracks()
            if str(item.get("track_id", "")).strip()
        }

    def _upsert_music_preference(
        self,
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
            with self._connect() as connection:
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
        return self.get_preference(preference_id)

    def delete_preference(self, preference_id: int) -> bool:
        try:
            with self._connect() as connection:
                cursor = connection.execute("DELETE FROM music_preferences WHERE id = ?", (preference_id,))
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise PreferenceStoreError(f"Could not delete preference: {exc}") from exc

    def get_preference(self, preference_id: int) -> dict[str, Any]:
        with self._connect() as connection:
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
        return self._decode_music_preference_row(row)

    def _list_music_preferences_by_type(self, preference_type: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
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
        return [self._decode_music_preference_row(row) for row in rows]

    def _decode_music_preference_row(self, row: sqlite3.Row) -> dict[str, Any]:
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

    def _normalize_artist_key(self, *, artist_id: str | None, artist_name: str | None) -> str | None:
        if artist_id is not None and str(artist_id).strip():
            return f"id:{str(artist_id).strip()}"
        if artist_name is not None and str(artist_name).strip():
            normalized = " ".join(str(artist_name).strip().casefold().split())
            return f"name:{normalized}"
        return None

    def get_active_session(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, request_text, steering_history_json, mode, is_active, created_at, updated_at, last_refilled_at
                FROM sessions
                WHERE is_active = 1
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._decode_session_row(row) if row is not None else None

    def start_session(self, *, request_text: str, mode: str = "adaptive") -> dict[str, Any]:
        with self._lifecycle_lock:
            try:
                with self._connect() as connection:
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
            session = self.get_session(session_id)
            if session is None:
                raise PreferenceStoreError(f"Session {session_id} was not found after creation.")
            return session

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, request_text, steering_history_json, mode, is_active, created_at, updated_at, last_refilled_at
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._decode_session_row(row) if row is not None else None

    def add_session_steering(self, session_id: int, steering_text: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session is None:
            raise PreferenceStoreError(f"Session {session_id} was not found.")
        steering_history = list(session.get("steering_history", []))
        steering_history.append(steering_text)
        try:
            with self._connect() as connection:
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
        updated = self.get_session(session_id)
        if updated is None:
            raise PreferenceStoreError(f"Session {session_id} was not found after update.")
        return updated

    def touch_session_refill(self, session_id: int) -> None:
        try:
            with self._connect() as connection:
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

    def stop_active_session(self) -> dict[str, Any] | None:
        with self._lifecycle_lock:
            session = self.get_active_session()
            if session is None:
                return None
            try:
                with self._connect() as connection:
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

    def add_session_track(self, session_id: int, track: dict[str, Any]) -> None:
        try:
            with self._connect() as connection:
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

    def replace_session_queue(
        self,
        session_id: int,
        items: list[dict[str, Any]],
        *,
        preserve_history: bool = False,
    ) -> None:
        try:
            with self._connect() as connection:
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
                position_start = self._next_session_queue_position(connection, session_id)
                for offset, item in enumerate(items):
                    track = dict(item.get("track") or {})
                    source = dict(item.get("source") or {})
                    track_id = self._queue_track_id(track)
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

    def append_session_queue(self, session_id: int, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        try:
            with self._connect() as connection:
                position_start = self._next_session_queue_position(connection, session_id)
                for offset, item in enumerate(items):
                    track = dict(item.get("track") or {})
                    source = dict(item.get("source") or {})
                    track_id = self._queue_track_id(track)
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
        self,
        session_id: int,
        *,
        limit: int = 50,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        where_state = "" if include_history else "AND state IN ('queued', 'playing')"
        with self._connect() as connection:
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
        return [self._decode_session_queue_row(row) for row in rows]

    def claim_next_session_queue_item(self, session_id: int) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
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
                connection.execute(
                    """
                    UPDATE session_queue_items
                    SET state = 'playing', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
        except sqlite3.Error as exc:
            raise PreferenceStoreError(f"Could not claim next session queue item: {exc}") from exc
        claimed = self.get_session_queue_item(int(row["id"]))
        return claimed

    def get_session_queue_item(self, queue_item_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
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
        return self._decode_session_queue_row(row) if row is not None else None

    def mark_session_queue_item(self, queue_item_id: int, state: str) -> None:
        if state not in {"queued", "playing", "played", "rejected", "filtered", "failed"}:
            raise PreferenceStoreError(f"Unsupported session queue state: {state}")
        try:
            with self._connect() as connection:
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

    def mark_session_queue_track(self, session_id: int, track_id: str, state: str) -> int:
        if state not in {"queued", "playing", "played", "rejected", "filtered", "failed"}:
            raise PreferenceStoreError(f"Unsupported session queue state: {state}")
        try:
            with self._connect() as connection:
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

    def reset_stale_session_queue_items(self, session_id: int) -> None:
        try:
            with self._connect() as connection:
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

    def _next_session_queue_position(self, connection: sqlite3.Connection, session_id: int) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM session_queue_items WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["next_position"]) if row is not None else 0

    def _queue_track_id(self, track: dict[str, Any]) -> str | None:
        track_id = str(track.get("id") or track.get("play_params", {}).get("id") or "").strip()
        return track_id or None

    def _decode_session_queue_row(self, row: sqlite3.Row) -> dict[str, Any]:
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

    def list_session_tracks(self, session_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
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

    def list_recent_tracks(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
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

    def get_session_runtime(self, session_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
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
        self,
        session_id: int,
        *,
        active_intent: str | None = None,
        last_advance_at: str | None = None,
        last_selected_track_id: str | None = None,
        last_known_playback_state: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_session_runtime(session_id) or {
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
            with self._connect() as connection:
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
        runtime = self.get_session_runtime(session_id)
        if runtime is None:
            raise PreferenceStoreError(f"Session runtime for session {session_id} was not found after update.")
        return runtime

    def update_session_pending_stop(
        self,
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
            with self._connect() as connection:
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

    def clear_session_runtime(self, session_id: int) -> None:
        try:
            with self._connect() as connection:
                connection.execute("DELETE FROM session_runtime WHERE session_id = ?", (session_id,))
        except sqlite3.Error as exc:
            raise PreferenceStoreError(f"Could not clear session runtime: {exc}") from exc

    def add_session_event(
        self,
        session_id: int,
        *,
        event_type: str,
        track: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        track = track or {}
        try:
            with self._connect() as connection:
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
        self,
        session_id: int,
        *,
        limit: int = 50,
        event_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
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

    def _decode_session_row(self, row: sqlite3.Row) -> dict[str, Any]:
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
