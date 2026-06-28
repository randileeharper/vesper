from __future__ import annotations

import sqlite3
import threading

from vesper.storage import PreferenceStore
from vesper.storage import sessions as sessions_module


def test_start_and_stop_session_toggles_active(settings) -> None:
    store = PreferenceStore(settings.database_path)

    started = store.start_session(request_text="play some music")
    assert started["is_active"] is True
    assert store.get_active_session() is not None

    stopped = store.stop_active_session()
    assert stopped is not None
    assert stopped["is_active"] is False
    assert store.get_active_session() is None


def test_concurrent_start_session_leaves_exactly_one_active(settings) -> None:
    # Without lifecycle serialization, two concurrent start_session calls can
    # interleave deactivate-all-then-insert on separate connections and leave
    # more than one row with is_active = 1. The lifecycle lock must prevent that.
    store = PreferenceStore(settings.database_path)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(10):
                store.start_session(request_text="play some music")
        except BaseException as exc:  # pragma: no cover - records any failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    with sqlite3.connect(settings.database_path) as connection:
        active = connection.execute("SELECT COUNT(*) FROM sessions WHERE is_active = 1").fetchone()[0]
    assert active == 1


def test_session_queue_round_trip_and_claim(settings) -> None:
    store = PreferenceStore(settings.database_path)
    session = store.start_session(request_text="play upbeat music")

    store.replace_session_queue(
        session["id"],
        [
            {
                "source": {"kind": "legacy", "term": "wide"},
                "source_key": "wide",
                "track": {"id": "track-1", "title": "One", "artist": "Artist"},
            },
            {
                "source": {"kind": "legacy", "term": "wide"},
                "source_key": "wide",
                "track": {"id": "track-2", "title": "Two", "artist": "Artist"},
            },
        ],
    )

    first = store.claim_next_session_queue_item(session["id"])
    queued = store.list_session_queue(session["id"], include_history=True)

    assert first is not None
    assert first["title"] == "One"
    assert [item["state"] for item in queued] == ["playing", "queued"]

    store.mark_session_queue_item(first["id"], "played")
    store.mark_session_queue_track(session["id"], "track-2", "rejected")

    queued = store.list_session_queue(session["id"], include_history=True)
    assert [item["state"] for item in queued] == ["played", "rejected"]


def test_reset_stale_session_queue_items(settings) -> None:
    store = PreferenceStore(settings.database_path)
    session = store.start_session(request_text="play upbeat music")
    store.replace_session_queue(
        session["id"],
        [
            {
                "source": {"kind": "legacy", "term": "wide"},
                "source_key": "wide",
                "track": {"id": "track-1", "title": "One"},
            }
        ],
    )

    claimed = store.claim_next_session_queue_item(session["id"])
    assert claimed is not None

    store.reset_stale_session_queue_items(session["id"])

    assert store.list_session_queue(session["id"])[0]["state"] == "queued"


def test_close_lifecycle_locks_drops_entry_for_path(settings, tmp_path) -> None:
    # start_session caches a lifecycle lock keyed by the database path; without
    # cleanup the dict grows by one entry per test database (issue #62).
    store = PreferenceStore(settings.database_path)
    store.start_session(request_text="play some music")
    key = settings.database_path
    assert key in sessions_module._lifecycle_locks

    sessions_module.close_lifecycle_locks(key)
    assert key not in sessions_module._lifecycle_locks

    # Scoping by a different path leaves the target lock untouched.
    store.start_session(request_text="play some music")
    other = tmp_path / "other.db"
    sessions_module.close_lifecycle_locks(other)
    assert key in sessions_module._lifecycle_locks

    # Full teardown (used by the autouse conftest fixture) clears everything.
    sessions_module.close_lifecycle_locks()
    assert sessions_module._lifecycle_locks == {}
