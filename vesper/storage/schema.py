"""Schema initialization for Vesper's SQLite store."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Dict, Tuple


# SQLite connections are opened with ``check_same_thread=False`` so that
# :func:`close_connections` can release a connection from a different thread
# (e.g. the main thread closing a background worker's connection after the
# worker has stopped). Concurrency safety comes from the per-operation ``with``
# blocks that serialize transactions on a given connection, plus SQLite's own
# WAL locking. A thread-local cache keyed by the resolved database path gives
# every thread one long-lived connection per database file. This replaces the
# previous "open a new connection per operation" behavior (issue #50), which
# churned through connections, leaked them (the ``with`` only managed the
# transaction, never closing the handle), and contended on SQLite's file-level
# locks between the background session worker and request handlers.
_connections: Dict[Tuple[int, str], sqlite3.Connection] = {}
_connections_guard = threading.Lock()


def _cache_key(database_path: Path) -> Tuple[int, str]:
    return (threading.get_ident(), str(Path(database_path).resolve()))


def connect(database_path: Path) -> sqlite3.Connection:
    """Return a cached, thread-local SQLite connection with row access by name.

    Each ``(thread, database file)`` pair gets one persistent connection. The
    connection is configured for WAL mode (``journal_mode=wal``) and
    ``synchronous=NORMAL`` for better read/write concurrency. Callers that wrap
    this in a ``with`` statement drive per-operation commit/rollback exactly as
    before; the connection itself simply survives across operations.
    """
    key = _cache_key(database_path)
    # Fast path: the common case once a thread has warmed its connection.
    connection = _connections.get(key)
    if connection is not None:
        return connection

    with _connections_guard:
        # Re-check under the lock in case another thread created one for a key
        # that happens to hash identically (it cannot be the same thread here,
        # since the key embeds the thread id, but the dict write must be guarded).
        connection = _connections.get(key)
        if connection is not None:
            return connection

        connection = sqlite3.connect(database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # WAL allows concurrent readers alongside a single writer; NORMAL is
        # safe under WAL and avoids an fsync per commit, which is the right
        # tradeoff for a local application store.
        connection.execute("PRAGMA journal_mode=wal")
        connection.execute("PRAGMA synchronous=normal")
        _connections[key] = connection
        return connection


def close_connections(database_path: Path | None = None) -> None:
    """Close cached connections, optionally scoped to one database file.

    With *database_path*, closes only connections to that file (across all
    threads). Without it, closes every cached connection. Safe to call from any
    thread. Used by :meth:`vesper.service.CiderAgentService.close` and by test
    fixtures to release per-test databases.
    """
    with _connections_guard:
        if database_path is None:
            keys = list(_connections.keys())
        else:
            resolved = str(Path(database_path).resolve())
            keys = [key for key in _connections if key[1] == resolved]
        for key in keys:
            connection = _connections.pop(key, None)
            if connection is not None:
                connection.close()


def initialize(database_path: Path) -> None:
    """Create all tables and indexes if they do not already exist."""
    with connect(database_path) as connection:
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
                -- Wall-clock UTC ISO-8601 (e.g. 2026-06-27T12:00:00+00:00).
                -- Never store time.monotonic() here: monotonic time is
                -- process-local and invalid across restarts / other processes.
                last_advance_at TEXT,
                last_selected_track_id TEXT,
                last_known_playback_state TEXT,
                pending_stop_track_id TEXT,
                -- Same UTC ISO-8601 wall-clock format as last_advance_at.
                pending_stop_observed_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
            """
        )
        ensure_session_runtime_columns(connection)
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


def ensure_session_runtime_columns(connection: sqlite3.Connection) -> None:
    """Backfill pending-stop confirmation columns for databases created before
    they existed. New databases get them from CREATE TABLE above."""
    # ``ALTER TABLE ... ADD COLUMN`` has no ``IF NOT EXISTS`` in SQLite, so
    # backfill the pending-stop confirmation columns for databases created
    # before they existed. New databases get them from CREATE TABLE above.
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(session_runtime)")}
    if "pending_stop_track_id" not in existing:
        connection.execute("ALTER TABLE session_runtime ADD COLUMN pending_stop_track_id TEXT")
    if "pending_stop_observed_at" not in existing:
        connection.execute("ALTER TABLE session_runtime ADD COLUMN pending_stop_observed_at TEXT")
