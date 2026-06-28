from __future__ import annotations

import threading

import pytest

from vesper.action_registry import get_action_definition
from vesper.config import Settings
from vesper.errors import CiderValidationError, TextRequestExecutionError
from vesper.resolver import ResolvedAction
from vesper.service import CiderAgentService
from vesper.session import SessionWorkerCancelled
from vesper.storage import PreferenceStore


def test_preferences_round_trip(service) -> None:
    service.play_session("play upbeat music")
    remembered = service.like_current_track()
    listed = service.list_preferences()

    assert remembered["liked_track"]["title"] == "Liked Song"
    assert remembered["favored_artist"]["artist_name"] == "Favorite Artist"
    assert listed["count"] == 2
    assert listed["summary"] == {
        "liked_tracks": 1,
        "favored_artists": 1,
        "globally_rejected_tracks": 0,
    }
    assert len(listed["liked_tracks"]) == 1
    assert len(listed["favored_artists"]) == 1
    assert listed["globally_rejected_tracks"] == []


def test_like_current_track_saves_session_context_without_interrupting_playback(service) -> None:
    service.play_session("play upbeat music")

    result = service.like_current_track()

    assert result["status"] == "ok"
    assert result["playback_continues"] is True
    assert result["liked_track"]["track_id"] == "catalog-track-favorite"
    assert result["liked_track"]["session_request_text"] == "play upbeat music"
    assert result["liked_track"]["session_search_query"] == "Favorite Artist Liked Song"


def test_run_action_dispatches_search(service) -> None:
    result = service.run_action("search_library_tracks", {"query": "k-pop", "limit": 3})

    assert result["action"] == "search_library_tracks"
    assert result["result"]["count"] == 1


def test_can_list_library_playlists(service) -> None:
    result = service.list_library_playlists()

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["playlists"][0]["name"] == "Mix"


def test_can_play_library_playlist_by_name(service) -> None:
    result = service.play_library_playlist("Mix")

    assert result["status"] == "ok"
    assert result["playlist"]["name"] == "Mix"
    assert service._rpc.posts[-1]["path"] == "/play-item"
    assert service._rpc.posts[-1]["body"] == {"id": "playlist-1", "type": "library-playlists", "isLibrary": True}


def test_action_registry_defines_exported_actions(service) -> None:
    definitions = {item["name"]: item for item in service.list_action_definitions()}

    assert set(definitions) == {"play", "pause", "stop", "list_preferences", "forget_preference"}
    assert definitions["list_preferences"]["read_only"] is True
    assert definitions["play"]["public_exposed"] is True
    assert get_action_definition("search_library_tracks") is not None


def test_set_volume_normalizes_percent_for_cider(service) -> None:
    result = service.set_volume(50)

    assert result["requested_volume"] == 50
    assert result["normalized_volume"] == 0.5
    assert result["result"]["body"] == {"volume": 0.5}


def test_run_action_set_volume_accepts_normalized_float_value(service) -> None:
    result = service.run_action("set_volume", {"value": 1.0})

    assert result["action"] == "set_volume"
    assert result["result"]["requested_volume"] == 100
    assert result["result"]["normalized_volume"] == 1.0


def test_run_action_reports_invalid_parameters_cleanly(service) -> None:
    with pytest.raises(CiderValidationError, match="set_volume requires a volume parameter"):
        service.run_action("set_volume", {})


def test_default_search_uses_catalog_by_default(service) -> None:
    result = service.search("kep1er", limit=3)

    assert result["search_source"] == "catalog"
    assert result["tracks"][0]["artist"] == "Catalog Artist"


def test_handle_text_request_uses_resolver(service) -> None:
    result = service.handle_text_request("play some kep1er")

    assert result["resolver"] == "stub"
    assert result["resolved_action"]["action"] == "search"
    assert result["execution"]["action"] == "search"
    assert isinstance(result["summary"], str)


def test_handle_text_request_compacts_liked_track_as_title_and_artist(service) -> None:
    result = service.handle_text_request("i like this song")

    assert result["execution"]["result"]["liked_track"] == {
        "title": "Track",
        "artist_name": "Artist",
    }
    assert result["execution"]["result"]["favored_artist"] == {
        "artist_name": "Artist",
    }
    assert result["summary"] == "saved Track by Artist"


def test_handle_text_request_includes_timings_when_enabled(settings, service, tmp_path) -> None:
    timed_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=settings.resolver_include_raw_output,
        include_timing_debug=True,
        response_detail=settings.response_detail,
        session_recent_tracks_limit=settings.session_recent_tracks_limit,
        global_recent_tracks_limit=settings.global_recent_tracks_limit,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "timed.db",
        config_path=settings.config_path,
    )
    timed_service = CiderAgentService(
        timed_settings,
        rpc_client=service._rpc,
        preference_store=PreferenceStore(timed_settings.database_path),
        resolver=service._resolver,
    )

    result = timed_service.handle_text_request("play some kep1er")

    assert "timings" in result
    assert "resolve_ms" in result["timings"]
    assert "execute_ms" in result["timings"]
    assert "total_ms" in result["timings"]
    assert "execution" not in result["timings"]


def test_handle_text_request_writes_resolver_debug_log(settings, service, tmp_path) -> None:
    debug_log_path = tmp_path / "resolver-debug.log"
    debug_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=settings.resolver_include_raw_output,
        resolver_debug_log_path=debug_log_path,
        response_detail=settings.response_detail,
        session_recent_tracks_limit=settings.session_recent_tracks_limit,
        global_recent_tracks_limit=settings.global_recent_tracks_limit,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "resolver-debug.db",
        config_path=settings.config_path,
    )

    class LoggingResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            service.append_resolver_debug_log(
                stage="resolve_text_request",
                messages=[{"role": "user", "content": text}],
                response_body={"ok": True},
                response_content='{"action":"status","parameters":{}}',
            )
            return ResolvedAction(action="status", parameters={}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            raise AssertionError("plan_session should not be called in this test")

    debug_service = CiderAgentService(
        debug_settings,
        rpc_client=service._rpc,
        preference_store=PreferenceStore(debug_settings.database_path),
        resolver=LoggingResolver(),
    )

    debug_service.handle_text_request("status")

    log_text = debug_log_path.read_text(encoding="utf-8")
    assert "reason: text-request: status" in log_text
    assert "=== resolve_text_request ===" in log_text
    assert '"content": "status"' in log_text
    assert '{"action":"status","parameters":{}}' in log_text


def test_handle_text_request_can_start_adaptive_session(service) -> None:
    result = service.handle_text_request("it's morning - play something upbeat and with energy")

    assert result["resolved_action"]["action"] == "play_session"
    assert result["execution"]["action"] == "play_session"
    assert result["execution"]["result"]["mode"] == "adaptive-session"
    assert "request_id" not in result["execution"]
    assert "plan" not in result["execution"]["result"]["result"]
    assert result["execution"]["result"]["result"]["enqueued_count"] == 0
    assert "primary_track" in result["execution"]["result"]["result"]
    assert result["summary"].startswith("playing ")
    assert service._rpc.posts[-2]["path"] == "/queue/clear-queue"
    assert service._rpc.posts[-1]["path"] == "/play-item"
    assert service._rpc.queue_items == []


def test_adaptive_session_timing_debug_includes_selection_breakdown(settings, service, tmp_path) -> None:
    timed_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=settings.resolver_include_raw_output,
        include_timing_debug=True,
        response_detail=settings.response_detail,
        session_recent_tracks_limit=settings.session_recent_tracks_limit,
        global_recent_tracks_limit=settings.global_recent_tracks_limit,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "timed-session.db",
        config_path=settings.config_path,
    )
    timed_service = CiderAgentService(
        timed_settings,
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(timed_settings.database_path),
        resolver=service._resolver.__class__(),
    )

    result = timed_service.handle_text_request("it's morning - play something upbeat and with energy")

    execution_timings = result["execution"]["result"]["result"]["timings"]
    assert "playback_snapshot_ms" in execution_timings
    assert "plan_session_ms" in execution_timings
    assert "collect_tracks_ms" in execution_timings
    assert "candidate_track_search_count" in execution_timings


def test_steer_session_updates_active_session(service) -> None:
    service.play_session("play upbeat music")

    result = service.steer_session("more pop")

    assert result["session"]["steering_history"][-1] == "more pop"
    assert result["result"]["selection_strategy"] == "adaptive-session-steer"
    assert result["result"]["deferred_until_next_track"] is True
    assert result["result"]["tracks"] == []


def test_session_status_includes_recent_tracks(service) -> None:
    service.play_session("play upbeat music")

    status = service.session_status()

    assert status["session"] is not None
    assert isinstance(status["recent_tracks"], list)
    assert "request_text" not in status["session"]


def test_status_is_trimmed_and_includes_queue_tracks(service) -> None:
    status = service.status()

    assert status["status"] == "ok"
    assert "config" not in status
    assert "queue" in status
    assert status["queue"]["count"] == 1
    assert status["queue"]["tracks"][0]["title"] == "Queued"
    assert "id" not in status["queue"]["tracks"][0]
    assert service._rpc.playback_get_calls.count("/queue") == 1


def test_next_track_advances_active_session_without_native_queue(service) -> None:
    service.play_session("play upbeat music")

    result = service.next_track()

    assert result["status"] == "ok"
    assert result["selection_strategy"] == "adaptive-session-skip"
    assert result["tracks"][0]["title"] == "Liked Song"
    assert service._rpc.posts[-1]["path"] == "/play-item"


def test_session_worker_advances_when_playback_stops(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    service._rpc.is_playing = False
    service._rpc.current_track = None
    service._session._set_session_runtime(session["id"], last_advance_at=0.0)
    service._preferences.upsert_session_runtime(session["id"], last_advance_at="1970-01-01T00:00:00+00:00")

    playback = service.playback_snapshot()

    # An empty now-playing snapshot is ambiguous: Cider can briefly report no
    # current track while one is still playing. Require two consecutive stopped
    # snapshots before treating playback as stopped.
    assert service._session._should_advance_session(session, playback) is False
    assert service._session._should_advance_session(session, playback) is True

    result = service._session._play_session_track(session, selection_strategy="adaptive-session-auto-advance")

    assert result["selection_strategy"] == "adaptive-session-auto-advance"
    assert result["tracks"][0]["title"] == "Liked Song"


def test_session_worker_does_not_advance_when_now_playing_has_remaining_time(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    service._rpc.is_playing = False
    service._rpc.current_track["attributes"]["currentPlaybackTime"] = 15
    service._rpc.current_track["attributes"]["remainingTime"] = 165
    service._session._set_session_runtime(session["id"], last_advance_at=0.0)
    service._preferences.upsert_session_runtime(session["id"], last_advance_at="1970-01-01T00:00:00+00:00")

    assert service._session._should_advance_session(session, service.playback_snapshot()) is False


def test_session_worker_does_not_advance_when_remaining_time_conflicts_with_progress(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    service._rpc.is_playing = False
    service._rpc.current_track["attributes"]["currentPlaybackTime"] = 15
    service._rpc.current_track["attributes"]["remainingTime"] = 0
    service._session._set_session_runtime(session["id"], last_advance_at=0.0)
    service._preferences.upsert_session_runtime(session["id"], last_advance_at="1970-01-01T00:00:00+00:00")

    assert service._session._should_advance_session(session, service.playback_snapshot()) is False


def test_session_worker_does_not_advance_when_playing_state_is_unknown(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    playback = service.playback_snapshot()
    playback["is_playing"] = None
    playback["track"] = None
    service._session._set_session_runtime(session["id"], last_advance_at=0.0)
    service._preferences.upsert_session_runtime(session["id"], last_advance_at="1970-01-01T00:00:00+00:00")

    assert service._session._should_advance_session(session, playback) is False


def test_session_worker_can_advance_when_now_playing_track_has_ended(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    service._rpc.is_playing = False
    service._rpc.current_track["attributes"]["currentPlaybackTime"] = 180
    service._rpc.current_track["attributes"]["remainingTime"] = 0
    service._session._set_session_runtime(session["id"], last_advance_at=0.0)
    service._preferences.upsert_session_runtime(session["id"], last_advance_at="1970-01-01T00:00:00+00:00")

    assert service._session._should_advance_session(session, service.playback_snapshot()) is True


def test_session_worker_requires_two_ambiguous_stopped_snapshots_before_advancing(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    service._rpc.is_playing = False
    service._rpc.current_track["attributes"]["remainingTime"] = 0
    service._rpc.current_track["attributes"].pop("currentPlaybackTime", None)
    service._session._set_session_runtime(session["id"], last_advance_at=0.0)
    service._preferences.upsert_session_runtime(session["id"], last_advance_at="1970-01-01T00:00:00+00:00")

    playback = service.playback_snapshot()

    assert service._session._should_advance_session(session, playback) is False
    assert service._get_session_runtime(session["id"])["pending_stop_track_id"] == "catalog-track-favorite"
    assert service._session._should_advance_session(session, playback) is True


def test_session_worker_requires_two_missing_track_stopped_snapshots_before_advancing(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    service._rpc.is_playing = False
    service._rpc.current_track = None
    service._session._set_session_runtime(session["id"], last_advance_at=0.0)
    service._preferences.upsert_session_runtime(session["id"], last_advance_at="1970-01-01T00:00:00+00:00")

    playback = service.playback_snapshot()

    assert service._session._should_advance_session(session, playback) is False
    assert service._get_session_runtime(session["id"])["pending_stop_track_id"] == "<missing>"
    assert service._session._should_advance_session(session, playback) is True


def test_session_runtime_timestamps_are_utc_wall_clock_not_monotonic(service) -> None:
    # Issue #48: persisted session-runtime timestamps must be wall-clock UTC
    # ISO-8601, never time.monotonic() floats, so they stay valid across
    # processes and after restarts.
    from datetime import UTC, datetime

    ts = service.current_timestamp()
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert ts.endswith("+00:00")

    # Numeric / monotonic values are rejected outright (no monotonic branch).
    engine = service._session
    assert engine._seconds_since_runtime_timestamp(0.0) is None
    assert engine._seconds_since_runtime_timestamp(None) is None

    # A persisted UTC string from the recent past yields a non-negative,
    # timezone-correct elapsed delta rather than a monotonic difference.
    recent = (datetime.now(UTC).isoformat(timespec="seconds"))
    elapsed = engine._seconds_since_runtime_timestamp(recent)
    assert elapsed is not None and elapsed >= 0.0

    # A far-past UTC timestamp reports a large elapsed span (cooldown bypassed).
    old = engine._seconds_since_runtime_timestamp("1970-01-01T00:00:00+00:00")
    assert old is not None and old > service.SESSION_ADVANCE_COOLDOWN_SECONDS


def test_pending_stop_confirmation_persists_across_process_restart(settings, service, tmp_path) -> None:
    database_path = tmp_path / "cross-process-pending-stop.db"
    rpc = service._rpc.__class__()
    first = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=database_path,
            config_path=settings.config_path,
        ),
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=service._resolver.__class__(),
    )
    first.play_session("play upbeat music")
    session = first._preferences.get_active_session()
    assert session is not None

    rpc.is_playing = False
    rpc.current_track = None
    first._session._set_session_runtime(session["id"], last_advance_at=0.0)
    first._preferences.upsert_session_runtime(session["id"], last_advance_at="1970-01-01T00:00:00+00:00")

    # The first process observes one stopped snapshot: it arms and PERSISTS the
    # pending confirmation, so it must not advance yet.
    assert first._session._should_advance_session(session, first.playback_snapshot()) is False
    assert first._preferences.get_session_runtime(session["id"])["pending_stop_track_id"] == "<missing>"

    # A fresh process on the same database must observe the armed confirmation
    # and confirm on its very first stopped snapshot instead of re-arming.
    second = CiderAgentService(
        first._settings,
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=first._resolver,
    )
    restarted_session = second._preferences.get_active_session()
    assert restarted_session is not None
    assert second._session._should_advance_session(restarted_session, second.playback_snapshot()) is True


def test_preference_store_backfills_pending_stop_columns(tmp_path) -> None:
    import sqlite3

    database_path = tmp_path / "legacy-runtime.db"
    # Simulate a database written by a pre-migration version: the runtime table
    # exists but lacks the pending_stop confirmation columns.
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE session_runtime (
                session_id INTEGER PRIMARY KEY,
                active_intent TEXT NOT NULL DEFAULT 'active',
                last_advance_at TEXT,
                last_selected_track_id TEXT,
                last_known_playback_state TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("INSERT INTO session_runtime (session_id) VALUES (1)")

    # Instantiating the store runs the schema migration.
    store = PreferenceStore(database_path)
    runtime = store.get_session_runtime(1)
    assert runtime is not None
    assert runtime["pending_stop_track_id"] is None
    assert runtime["pending_stop_observed_at"] is None

    store.update_session_pending_stop(1, track_id="<missing>", observed_at="2026-01-01T00:00:00+00:00")
    assert store.get_session_runtime(1)["pending_stop_track_id"] == "<missing>"


def test_active_session_reconcile_tolerates_empty_now_playing_info_list(settings, service) -> None:
    service._preferences.start_session(request_text="play upbeat music")

    class EmptyNowPlayingRpc(service._rpc.__class__):
        def playback_get(self, path: str):
            if path == "/now-playing":
                self.playback_get_calls.append(path)
                return {"info": []}
            return super().playback_get(path)

    rpc = EmptyNowPlayingRpc()

    restarted = CiderAgentService(
        settings,
        rpc_client=rpc,
        preference_store=PreferenceStore(settings.database_path),
        resolver=service._resolver,
    )

    assert restarted.playback_snapshot()["track"]["title"] is None
    assert restarted.get_now_playing()["track"]["title"] is None
    assert restarted.play()["status"] == "ok"
    assert rpc.posts[-1]["path"] == "/play"


def test_session_worker_respects_persisted_cooldown_across_processes(settings, service, tmp_path) -> None:
    database_path = tmp_path / "cross-process-cooldown.db"
    rpc = service._rpc.__class__()
    first = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=database_path,
            config_path=settings.config_path,
        ),
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=service._resolver.__class__(),
    )
    first.play_session("play upbeat music")
    second = CiderAgentService(
        first._settings,
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=first._resolver,
    )
    session = second._preferences.get_active_session()
    assert session is not None

    second._session._set_session_runtime(session["id"], last_advance_at=0.0)
    first.next_track()
    rpc.is_playing = False
    rpc.current_track = None

    assert second._get_session_runtime(session["id"])["last_advance_at"] == 0.0
    assert second._session._should_advance_session(session, second.playback_snapshot()) is False


def test_session_worker_respects_persisted_suspension_across_long_lived_processes(settings, service, tmp_path) -> None:
    database_path = tmp_path / "cross-process-suspension.db"
    rpc = service._rpc.__class__()
    first = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=database_path,
            config_path=settings.config_path,
        ),
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=service._resolver.__class__(),
    )
    first.play_session("play upbeat music")
    second = CiderAgentService(
        first._settings,
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=first._resolver,
    )
    session = second._preferences.get_active_session()
    assert session is not None

    first.pause()
    second._session._set_session_runtime(session["id"], suspended=False, last_advance_at=0.0)
    rpc.is_playing = False
    rpc.current_track = None

    assert second._get_session_runtime(session["id"])["suspended"] is False
    assert second._session._should_advance_session(session, second.playback_snapshot()) is False


def test_reject_current_track_advances_active_session_without_changing_vibe(service) -> None:
    service.play_session("play upbeat music")

    result = service.reject_current_track()

    assert result["status"] == "ok"
    assert result["result"]["selection_strategy"] == "adaptive-session-reject-current"
    assert result["result"]["tracks"][0]["title"] == "Another Song"
    rejected = service._preferences.globally_rejected_tracks()
    assert rejected[0]["track_id"] == "catalog-track-favorite"


def test_reject_current_track_without_session_creates_global_reject_and_skips(service) -> None:
    result = service.reject_current_track()

    assert result["status"] == "ok"
    assert result["mode"] == "playback"
    assert result["rejected_track_id"] == "track-1"
    assert service._rpc.posts[-1]["path"] == "/next"
    listed = service.list_preferences()
    assert listed["summary"]["globally_rejected_tracks"] == 1
    assert listed["globally_rejected_tracks"][0]["track_id"] == "track-1"


def test_vague_session_bootstraps_from_saved_preferences(service) -> None:
    service.play_session("play upbeat music")
    service.like_current_track()
    service.stop_session()

    result = service.play_session("play some music")

    assert result["result"]["plan"]["search_sources"] == [
        {"kind": "preference", "term": "__preference_seeded__"}
    ]
    runtime = service._get_session_runtime(result["session"]["id"])
    pool = next(iter(runtime["query_pools"].values()))
    assert pool["entries"]
    assert any(entry["track"].get("_seed_query") == "Favorite Artist Liked Song" for entry in pool["entries"])


def test_preference_seeded_session_falls_back_to_direct_liked_track_when_search_returns_nothing(settings, service, tmp_path) -> None:
    class EmptySearchRpc(service._rpc.__class__):
        def search_catalog(self, query: str, *, limit: int, storefront: str, offset: int = 0):
            return {"data": {"results": {"songs": {"data": []}}}}

    seed_service = CiderAgentService(
        settings,
        rpc_client=EmptySearchRpc(),
        preference_store=PreferenceStore(tmp_path / "preference-fallback.db"),
        resolver=service._resolver,
    )
    seed_service._preferences.record_liked_track(
        track_id="catalog-track-favorite",
        title="Liked Song",
        artist_name="Favorite Artist",
        item_kind="songs",
        is_library=False,
        session_request_text="play upbeat music",
        session_search_query="missing cue",
    )
    seed_service._preferences.record_favored_artist(artist_name="Favorite Artist", session_search_query="missing cue")

    result = seed_service.play_session("play some music")

    assert result["result"]["tracks"][0]["title"] == "Liked Song"


def test_session_candidates_returns_none_when_no_active_session(service) -> None:
    result = service.session_candidates()

    assert result["status"] == "ok"
    assert result["session"] is None
    assert result["pools"] == []


def test_session_candidates_reports_active_session_pools(service) -> None:
    service.play_session("play something upbeat")

    result = service.session_candidates()

    assert result["status"] == "ok"
    assert result["session"]["request_text"] == "play something upbeat"
    assert isinstance(result["active_search_sources"], list)
    assert len(result["active_search_sources"]) >= 1
    pools = result["pools"]
    assert len(pools) >= 1
    pool = pools[0]
    assert "source" in pool
    assert "cursor" in pool
    assert "total_entries" in pool
    assert "state_counts" in pool
    assert "next_window" in pool
    state_counts = pool["state_counts"]
    assert set(state_counts.keys()) >= {"fresh", "played", "screened_out", "rejected"}
    # After session start, one track has been claimed/played, the rest are fresh.
    assert state_counts["fresh"] >= 1 or pool["total_entries"] >= 1


def test_session_candidates_respects_window_limit(service) -> None:
    service.play_session("play something upbeat")

    result = service.session_candidates(window=2)

    pool = result["pools"][0]
    assert len(pool["next_window"]) <= 2


def test_session_candidates_empty_pools_after_runtime_cleared(service) -> None:
    service.play_session("play something upbeat")
    service._session._clear_all_session_runtime()

    result = service.session_candidates()

    # Session is still persisted, but runtime pools are gone.
    assert result["session"] is not None
    assert result["pools"] == []


def test_global_rejects_are_excluded_from_new_session_caches(service) -> None:
    service._preferences.record_global_rejected_track(
        track_id="catalog-track-favorite",
        title="Liked Song",
        artist_name="Favorite Artist",
    )

    result = service.play_session("play upbeat music")

    assert result["result"]["tracks"][0]["title"] == "Another Song"


def test_failed_session_start_does_not_leave_active_session(settings, service, tmp_path) -> None:
    class NoMatchResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            return type(
                "Plan",
                (),
                {
                    "search_queries": [],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    failing_service = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=tmp_path / "failed-session-start.db",
            config_path=settings.config_path,
        ),
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(tmp_path / "failed-session-start.db"),
        resolver=NoMatchResolver(),
    )

    with pytest.raises(CiderValidationError, match="No playable candidate match could be resolved."):
        failing_service.play_session("piano music with radwimps vibes")

    assert failing_service._preferences.get_active_session() is None


def test_new_session_avoids_recent_global_starter_track(settings, service, tmp_path) -> None:
    class RepeatingPlanResolver:
        def __init__(self) -> None:
            self.plan_calls = 0

        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            self.plan_calls += 1
            return type(
                "Plan",
                (),
                {
                    "search_queries": [
                        "Favorite Artist Liked Song" if self.plan_calls == 1 else "Favorite Artist Another Song"
                    ],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    repeat_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=settings.resolver_include_raw_output,
        response_detail=settings.response_detail,
        session_recent_tracks_limit=5,
        global_recent_tracks_limit=10,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "global-recent.db",
        config_path=settings.config_path,
    )
    repeat_service = CiderAgentService(
        repeat_settings,
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(repeat_settings.database_path),
        resolver=RepeatingPlanResolver(),
    )

    first = repeat_service.play_session("play upbeat music")
    assert first["result"]["tracks"][0]["title"] == "Liked Song"

    repeat_service.stop_session()

    second = repeat_service.play_session("play upbeat music again")
    assert second["result"]["tracks"][0]["title"] == "Another Song"


def test_new_query_pools_apply_global_recent_history_only_at_build_time(settings, service, tmp_path) -> None:
    class SameTrackResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            return type(
                "Plan",
                (),
                {
                    "search_queries": ["Favorite Artist Liked Song"],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    pool_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=settings.resolver_include_raw_output,
        include_timing_debug=True,
        response_detail=settings.response_detail,
        session_recent_tracks_limit=5,
        global_recent_tracks_limit=10,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "global-recent-pool.db",
        config_path=settings.config_path,
    )
    pool_service = CiderAgentService(
        pool_settings,
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(pool_settings.database_path),
        resolver=SameTrackResolver(),
    )

    first = pool_service.play_session("play upbeat music")
    assert first["result"]["tracks"][0]["title"] == "Liked Song"
    pool_service.stop_session()
    pool_service._rpc.current_track = None

    session = {"id": 999, "request_text": "play upbeat music", "steering_history": []}
    built_pool = pool_service._session._build_session_query_pool(session, "Favorite Artist Liked Song")
    assert [entry["track"]["title"] for entry in built_pool["entries"]] == ["Liked Song"]


def test_new_query_pool_relaxes_global_recent_filter_when_it_would_be_empty(settings, service, tmp_path) -> None:
    class SameTrackResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            return type(
                "Plan",
                (),
                {
                    "search_queries": ["Favorite Artist Liked Song"],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    pool_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=settings.resolver_include_raw_output,
        response_detail=settings.response_detail,
        session_recent_tracks_limit=5,
        global_recent_tracks_limit=10,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "global-recent-relax.db",
        config_path=settings.config_path,
    )
    pool_service = CiderAgentService(
        pool_settings,
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(pool_settings.database_path),
        resolver=SameTrackResolver(),
    )

    first = pool_service.play_session("play upbeat music")
    assert first["result"]["tracks"][0]["title"] == "Liked Song"
    pool_service.stop_session()
    pool_service._rpc.current_track = None

    session = {"id": 999, "request_text": "play upbeat music", "steering_history": []}
    built_pool = pool_service._session._build_session_query_pool(session, "Favorite Artist Liked Song")

    assert [entry["track"]["title"] for entry in built_pool["entries"]] == ["Liked Song"]


def test_materialized_session_queue_reuses_large_result_pool_without_requerying(service) -> None:
    class QueueResolver:
        def __init__(self) -> None:
            self.selection_calls = 0

        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            return type(
                "Plan",
                (),
                {
                    "search_queries": ["Favorite Artist Wide Pool"],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    service._resolver = QueueResolver()
    session = service._preferences.start_session(request_text="play upbeat music")

    first = service._session._play_session_track(session, selection_strategy="adaptive-session-manual-advance")
    second = service._session._play_session_track(session, selection_strategy="adaptive-session-manual-advance")
    queue = service.session_queue(limit=20, include_history=True)

    assert first["tracks"][0]["title"] == "Wide Song 1"
    assert second["tracks"][0]["title"] == "Wide Song 2"
    assert len(service._rpc.search_catalog_calls) == 1
    assert service._resolver.selection_calls == 0
    assert [item["title"] for item in queue["items"][:3]] == ["Wide Song 1", "Wide Song 2", "Wide Song 3"]
    assert [item["state"] for item in queue["items"][:3]] == ["played", "playing", "queued"]


def test_session_query_pool_fetches_100_results_as_two_paginated_calls(settings, tmp_path) -> None:
    class PaginatedRpcClient:
        def __init__(self) -> None:
            self.search_calls: list[dict[str, int]] = []

        def close(self) -> None:
            return None

        def playback_get(self, path: str):
            if path == "/now-playing":
                return {"info": {}}
            if path == "/queue":
                return []
            if path == "/is-playing":
                return {"status": "ok", "is_playing": False}
            if path == "/volume":
                return {"volume": 0.5}
            if path == "/repeat-mode":
                return {"value": 0}
            if path == "/shuffle-mode":
                return {"value": 0}
            if path == "/autoplay":
                return {"value": False}
            return {"value": True}

        def playback_post(self, path: str, body=None):
            return {"path": path, "body": body}

        def search_catalog(self, query: str, *, limit: int, storefront: str, offset: int = 0):
            self.search_calls.append({"limit": limit, "offset": offset})
            songs = [
                {
                    "id": f"page-track-{offset + index + 1}",
                    "type": "songs",
                    "attributes": {
                        "name": f"Page Song {offset + index + 1}",
                        "artistName": "Paged Artist",
                        "albumName": "Paged Album",
                        "playParams": {
                            "id": f"page-track-{offset + index + 1}",
                            "kind": "songs",
                            "isLibrary": False,
                        },
                    },
                }
                for index in range(limit)
            ]
            return {"data": {"results": {"songs": {"data": songs}}}}

        def search_library(self, query: str, *, limit: int, types: list[str] | None = None):
            return {"data": {"results": {}}}

        def run_amapi_v3(self, path: str, *, method: str = "GET", body: dict[str, object] | None = None):
            return {"data": {"data": []}}

    class FixedResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            return type(
                "Plan",
                (),
                {
                    "search_queries": ["trip hop essentials"],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    rpc = PaginatedRpcClient()
    paged_service = CiderAgentService(
        settings,
        rpc_client=rpc,
        preference_store=PreferenceStore(tmp_path / "paged-limit.db"),
        resolver=FixedResolver(),
    )

    result = paged_service.play_session("play trip-hop")

    assert result["result"]["tracks"][0]["title"] == "Page Song 1"
    assert rpc.search_calls == [{"limit": 50, "offset": 0}, {"limit": 50, "offset": 50}]


def test_reject_current_track_marks_cached_entry_rejected_across_passes(service) -> None:
    class FixedPoolResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            return type(
                "Plan",
                (),
                {
                    "search_queries": ["Favorite Artist Wide Pool"],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    service._resolver = FixedPoolResolver()
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    rejected_track_id = service.playback_snapshot()["track"]["track_id"]
    service.reject_current_track()

    runtime = service._get_session_runtime(session["id"])
    pool = next(iter(runtime["query_pools"].values()))
    rejected_entry = next(entry for entry in pool["entries"] if entry["track"]["id"] == rejected_track_id)

    assert rejected_entry["state"] == "rejected"


def test_resolver_debug_log_resets_between_track_selection_episodes(settings, service, tmp_path) -> None:
    debug_log_path = tmp_path / "episode-debug.log"
    debug_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=settings.resolver_include_raw_output,
        resolver_debug_log_path=debug_log_path,
        response_detail=settings.response_detail,
        session_recent_tracks_limit=settings.session_recent_tracks_limit,
        global_recent_tracks_limit=settings.global_recent_tracks_limit,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "episode-debug.db",
        config_path=settings.config_path,
    )

    class EpisodeLoggingResolver:
        def __init__(self) -> None:
            self.plan_calls = 0

        def resolve(self, text: str, service) -> ResolvedAction:
            raise AssertionError("resolve should not be called in this test")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            self.plan_calls += 1
            label = "Favorite Artist Liked Song" if self.plan_calls == 1 else "Favorite Artist Another Song"
            marker = f"query-{self.plan_calls}"
            service.append_resolver_debug_log(
                stage="plan_session_query",
                messages=[{"role": "user", "content": marker}],
                response_body={"search_queries": [label], "marker": marker},
                response_content=label,
            )
            return type(
                "Plan",
                (),
                {
                    "search_queries": [label],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    debug_service = CiderAgentService(
        debug_settings,
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(debug_settings.database_path),
        resolver=EpisodeLoggingResolver(),
    )

    debug_service.play_session("play upbeat music")
    first_log = debug_log_path.read_text(encoding="utf-8")
    assert "query-1" in first_log

    debug_service.next_track()
    second_log = debug_log_path.read_text(encoding="utf-8")
    assert "reason: adaptive-session-skip" in second_log
    assert "query-1" not in second_log
    assert debug_service._resolver.plan_calls == 1


def test_auto_advance_writes_fresh_resolver_debug_episode(settings, service, tmp_path) -> None:
    debug_log_path = tmp_path / "auto-advance-debug.log"
    debug_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=settings.resolver_include_raw_output,
        resolver_debug_log_path=debug_log_path,
        response_detail=settings.response_detail,
        session_recent_tracks_limit=settings.session_recent_tracks_limit,
        global_recent_tracks_limit=settings.global_recent_tracks_limit,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "auto-advance-debug.db",
        config_path=settings.config_path,
    )

    class EpisodeLoggingResolver:
        def __init__(self) -> None:
            self.plan_calls = 0

        def resolve(self, text: str, service) -> ResolvedAction:
            raise AssertionError("resolve should not be called in this test")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            self.plan_calls += 1
            marker = f"auto-query-{self.plan_calls}"
            query = "Favorite Artist Liked Song" if self.plan_calls == 1 else "Favorite Artist Another Song"
            service.append_resolver_debug_log(
                stage="plan_session_query",
                messages=[{"role": "user", "content": marker}],
                response_body={"search_queries": [query], "marker": marker},
                response_content=query,
            )
            return type(
                "Plan",
                (),
                {
                    "search_queries": [query],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    debug_service = CiderAgentService(
        debug_settings,
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(debug_settings.database_path),
        resolver=EpisodeLoggingResolver(),
    )

    debug_service.play_session("play upbeat music")
    first_log = debug_log_path.read_text(encoding="utf-8")
    assert "reason: adaptive-session-start: play upbeat music" in first_log
    assert "auto-query-1" in first_log

    active = debug_service._preferences.get_active_session()
    assert active is not None
    debug_service._session._play_session_track_with_debug_episode(
        active,
        selection_strategy="adaptive-session-auto-advance",
        debug_reason="adaptive-session-auto-advance",
    )

    second_log = debug_log_path.read_text(encoding="utf-8")
    assert "reason: adaptive-session-auto-advance" in second_log
    assert "auto-query-1" not in second_log
    assert debug_service._resolver.plan_calls == 1


def test_auto_advance_debug_log_captures_decision_payload(settings, service, tmp_path) -> None:
    debug_log_path = tmp_path / "auto-advance-check.log"
    debug_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=settings.resolver_include_raw_output,
        resolver_debug_log_path=debug_log_path,
        response_detail=settings.response_detail,
        session_recent_tracks_limit=settings.session_recent_tracks_limit,
        global_recent_tracks_limit=settings.global_recent_tracks_limit,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "auto-advance-check.db",
        config_path=settings.config_path,
    )
    debug_service = CiderAgentService(
        debug_settings,
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(debug_settings.database_path),
        resolver=service._resolver.__class__(),
    )

    debug_service.play_session("play upbeat music")
    session = debug_service._preferences.get_active_session()
    assert session is not None

    debug_service._rpc.is_playing = False
    debug_service._rpc.current_track["attributes"]["remainingTime"] = 0
    debug_service._rpc.current_track["attributes"].pop("currentPlaybackTime", None)
    debug_service._session._set_session_runtime(session["id"], last_advance_at=0.0)
    debug_service._preferences.upsert_session_runtime(
        session["id"], last_advance_at="1970-01-01T00:00:00+00:00"
    )

    started = debug_service._begin_resolver_debug_episode("adaptive-session-auto-advance-check")
    try:
        assert debug_service._session._should_advance_session(session, debug_service.playback_snapshot()) is False
    finally:
        debug_service._end_resolver_debug_episode(started)

    log_text = debug_log_path.read_text(encoding="utf-8")
    assert "reason: adaptive-session-auto-advance-check" in log_text
    assert "=== session_auto_advance_evaluated ===" in log_text
    assert '"blocked_by": "awaiting_second_stopped_snapshot"' in log_text
    assert '"track_state": "ambiguous"' in log_text
    assert '"track_id": "catalog-track-favorite"' in log_text


def test_preserve_steering_keeps_session_query_pools(service) -> None:
    class CacheFillingResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            return type(
                "Plan",
                (),
                {
                    "search_queries": ["Favorite Artist Wide Pool"],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    service._resolver = CacheFillingResolver()
    started = service.play_session("play upbeat music")
    session = started["session"]
    runtime = service._get_session_runtime(session["id"])
    original_key = next(iter(runtime["query_pools"]))
    original_pool = runtime["query_pools"][original_key]

    service.steer_session("prefer female vocalists")

    runtime = service._get_session_runtime(session["id"])
    assert runtime["query_pools"][original_key] == original_pool


def test_replace_steering_rebuilds_session_query_pools(service) -> None:
    class CacheFillingResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            return type(
                "Plan",
                (),
                {
                    "search_queries": ["Favorite Artist Wide Pool"],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    service._resolver = CacheFillingResolver()
    started = service.play_session("play upbeat music")
    session = started["session"]
    runtime = service._get_session_runtime(session["id"])
    assert next(iter(runtime["query_pools"].values()))["search_query"] == "Favorite Artist Wide Pool"

    service.steer_session(
        "switch to dream pop",
        search_update={"mode": "replace", "sources": [{"kind": "vibe", "term": "dream pop"}]},
    )

    runtime = service._get_session_runtime(session["id"])
    assert runtime["active_search_sources"] == [{"kind": "vibe", "term": "dream pop"}]
    assert next(iter(runtime["query_pools"].values()))["source"] == {"kind": "vibe", "term": "dream pop"}


def test_additive_steering_appends_session_search_query(service) -> None:
    class CacheFillingResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            return type(
                "Plan",
                (),
                {
                    "search_queries": ["trip hop"],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    service._resolver = CacheFillingResolver()
    started = service.play_session("play trip hop")
    session = started["session"]

    service.steer_session(
        "add some radwimps",
        search_update={"mode": "add", "sources": [{"kind": "artist", "term": "RADWIMPS"}]},
    )

    runtime = service._get_session_runtime(session["id"])
    assert runtime["active_search_sources"] == [
        {"kind": "legacy", "term": "trip hop"},
        {"kind": "artist", "term": "RADWIMPS"},
    ]
    assert [pool["source"] for pool in runtime["query_pools"].values()] == [
        {"kind": "legacy", "term": "trip hop"},
        {"kind": "artist", "term": "RADWIMPS"},
    ]


def test_top_pool_order_randomizes_within_small_high_confidence_bucket(service) -> None:
    class ReverseRandom:
        def shuffle(self, items) -> None:
            items.reverse()

    service._random = ReverseRandom()
    ordered = service._search_ctrl._top_pool_order(
        [
            {"title": "One"},
            {"title": "Two"},
            {"title": "Three"},
            {"title": "Four"},
        ],
        take=3,
    )

    assert [track["title"] for track in ordered] == ["Three", "Two", "One"]


def test_session_planning_reuses_cached_playback_snapshot(settings, tmp_path) -> None:
    class SnapshotCountingRpcClient:
        def __init__(self) -> None:
            self.playback_get_calls: list[str] = []

        def close(self) -> None:
            return None

        def playback_get(self, path: str):
            self.playback_get_calls.append(path)
            if path == "/now-playing":
                return {
                    "info": {
                        "name": "Track",
                        "artistName": "Artist",
                        "albumName": "Album",
                        "playParams": {"id": "track-1", "kind": "songs", "isLibrary": False},
                        "durationInMillis": 180000,
                    }
                }
            if path == "/queue":
                return []
            if path == "/is-playing":
                return {"status": "ok", "is_playing": True}
            if path == "/volume":
                return {"volume": 0.5}
            if path == "/repeat-mode":
                return {"value": 0}
            if path == "/shuffle-mode":
                return {"value": 0}
            if path == "/autoplay":
                return {"value": False}
            return {"value": True}

        def playback_post(self, path: str, body=None):
            return {"path": path, "body": body}

        def search_catalog(self, query: str, *, limit: int, storefront: str, offset: int = 0):
            return {
                "data": {
                    "results": {
                        "songs": {
                            "data": [
                                {
                                    "id": "catalog-track-1",
                                    "type": "songs",
                                    "attributes": {
                                        "name": "Liked Song",
                                        "artistName": "Favorite Artist",
                                        "albumName": "Album",
                                        "playParams": {"id": "catalog-track-1", "kind": "songs", "isLibrary": False},
                                    },
                                }
                            ]
                        }
                    }
                }
            }

    class SnapshotAwareResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

        def plan_session(self, request: str, service: CiderAgentService, session: dict[str, object], count: int):
            playback = service.session_planning_playback_snapshot(session)
            assert playback["track"]["title"] == "Track"
            return type(
                "Plan",
                (),
                {
                    "search_queries": ["Favorite Artist Liked Song"],
                    "resolver": "stub",
                    "raw": None,
                    "reasoning": None,
                    "raw_content": None,
                },
            )()

    rpc = SnapshotCountingRpcClient()
    snapshot_service = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=tmp_path / "snapshot-cache.db",
            config_path=settings.config_path,
        ),
        rpc_client=rpc,
        preference_store=PreferenceStore(tmp_path / "snapshot-cache.db"),
        resolver=SnapshotAwareResolver(),
    )

    snapshot_service.play_session("play upbeat music")

    assert rpc.playback_get_calls.count("/is-playing") == 1
    assert rpc.playback_get_calls.count("/now-playing") == 1
    assert rpc.playback_get_calls.count("/queue") == 1


def test_playback_snapshot_reuses_instance_thread_pool(service) -> None:
    # playback_snapshot must fan out its 7 reads on a single reused
    # instance-level pool, not create a ThreadPoolExecutor per call. See #67.
    executor_before = service._playback_ctrl._executor

    service._rpc.playback_get_calls.clear()
    service.playback_snapshot()

    # All seven snapshot paths are fetched exactly once per call.
    expected_paths = {
        "/is-playing",
        "/now-playing",
        "/volume",
        "/queue",
        "/repeat-mode",
        "/shuffle-mode",
        "/autoplay",
    }
    assert set(service._rpc.playback_get_calls) == expected_paths

    # A second call reuses the same pool instance.
    service.playback_snapshot()
    assert service._playback_ctrl._executor is executor_before


def test_playback_close_drains_in_flight_snapshot(service) -> None:
    # close() must wait for in-flight playback_snapshot() fan-out tasks to
    # finish before returning, instead of orphaning worker threads with
    # shutdown(wait=False) (issue #89).
    import threading as _threading
    from concurrent.futures import ThreadPoolExecutor

    ctrl = service._playback_ctrl
    rpc = service._rpc

    # Gate that blocks ALL RPC calls until released. We wait for all 7
    # snapshot reads to start before signalling, then block every call on the
    # gate. This ensures all 7 futures are submitted and in-flight when close()
    # runs, regardless of pool scheduling order.
    gate = _threading.Event()
    call_count = _threading.Lock()
    call_started = _threading.Event()

    original_get = rpc.playback_get

    def gated_playback_get(path: str):
        with call_count:
            rpc.playback_get_calls.append(path)
            if len(rpc.playback_get_calls) >= 7 and not call_started.is_set():
                call_started.set()
        gate.wait(timeout=10)
        return original_get(path)

    rpc.playback_get_calls.clear()
    rpc.playback_get = gated_playback_get

    # Start a snapshot in a background thread. It will block on the gate.
    snapshot_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-snapshot")
    snapshot_future = snapshot_executor.submit(ctrl.playback_snapshot)

    assert call_started.wait(timeout=5), "gated RPC call should have started"

    # Now call close() while the snapshot fan-out is in flight. It should
    # not return until the in-flight tasks complete (after we release the
    # gate). We verify ordering: close() blocks, then we release the gate,
    # then close() returns.
    close_done = _threading.Event()

    def call_close():
        ctrl.close()
        close_done.set()

    close_thread = _threading.Thread(target=call_close, name="test-close")
    close_thread.start()

    # close() should be blocked waiting for the in-flight future.
    assert not close_done.wait(timeout=0.3), "close() should not return while tasks are in flight"

    # Release the gate so the in-flight snapshot can complete.
    gate.set()

    # close() should now return promptly (within the drain timeout).
    assert close_done.wait(timeout=6), "close() should return after in-flight tasks drain"
    close_thread.join(timeout=5)

    # The background snapshot should also have completed successfully.
    snapshot_result = snapshot_future.result(timeout=5)
    assert snapshot_result["status"] == "ok"
    snapshot_executor.shutdown(wait=True)

    # Restore the stub so the fixture teardown doesn't hit the gated version.
    rpc.playback_get = original_get


def test_handle_text_request_includes_raw_output_when_enabled(settings, service, tmp_path) -> None:
    class RawStubResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(
                action="status",
                parameters={},
                resolver="stub",
                raw={"action": "status", "parameters": {}},
                raw_content='{"action":"status","parameters":{}}',
            )

    debug_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=True,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "debug.db",
        config_path=settings.config_path,
    )

    debug_service = CiderAgentService(
        debug_settings,
        rpc_client=service._rpc,
        preference_store=PreferenceStore(debug_settings.database_path),
        resolver=RawStubResolver(),
    )

    result = debug_service.handle_text_request("status")

    assert result["resolver_raw_content"] == '{"action":"status","parameters":{}}'
    assert result["resolver_raw_action"] == {"action": "status", "parameters": {}}


def test_handle_text_request_preserves_debug_output_on_execution_error(settings, service, tmp_path) -> None:
    class FailingStubResolver:
        def resolve(self, text: str, service) -> ResolvedAction:
            return ResolvedAction(
                action="play_candidate_match",
                parameters={"candidate_tracks": [{"title": "Nope", "artist": "Nobody"}]},
                resolver="stub",
                raw={"action": "play_candidate_match", "parameters": {"candidate_tracks": [{"title": "Nope", "artist": "Nobody"}]}},
                raw_content='{"action":"play_candidate_match","parameters":{"candidate_tracks":[{"title":"Nope","artist":"Nobody"}]}}',
            )

    debug_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend=settings.resolver_backend,
        resolver_base_url=settings.resolver_base_url,
        resolver_model=settings.resolver_model,
        resolver_api_key=settings.resolver_api_key,
        resolver_include_reasoning=settings.resolver_include_reasoning,
        resolver_include_raw_output=True,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=tmp_path / "debug-failure.db",
        config_path=settings.config_path,
    )

    debug_service = CiderAgentService(
        debug_settings,
        rpc_client=service._rpc,
        preference_store=PreferenceStore(debug_settings.database_path),
        resolver=FailingStubResolver(),
    )

    with pytest.raises(TextRequestExecutionError) as exc_info:
        debug_service.handle_text_request("play sleepy piano music")

    payload = exc_info.value.payload
    assert payload["status"] == "error"
    assert payload["resolver_raw_content"] == '{"action":"play_candidate_match","parameters":{"candidate_tracks":[{"title":"Nope","artist":"Nobody"}]}}'
    assert payload["resolver_raw_action"]["action"] == "play_candidate_match"
    assert payload["error"]["type"] == "CiderValidationError"


def test_play_candidate_match_prefers_track_candidate(service) -> None:
    result = service.play_candidate_match(
        candidate_tracks=[{"title": "Liked Song", "artist": "Favorite Artist"}],
        candidate_queries=["k-pop"],
    )

    assert result["selection_strategy"] == "candidate_track_exactish_match"
    assert result["selected_track"]["play_params"]["id"] == "catalog-track-favorite"


def test_play_candidate_match_does_not_treat_artist_as_one_track(service) -> None:
    with pytest.raises(CiderValidationError, match="No playable candidate match"):
        service.run_action("play_candidate_match", {"candidate_artists": ["RADWIMPS"]})


def test_play_candidate_match_falls_through_failed_query(settings) -> None:
    class _QueryFallbackRpc:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def close(self) -> None:
            return None

        def search_catalog(self, query: str, *, limit: int, storefront: str, offset: int = 0):
            self.queries.append(query)
            if query == "hit":
                return {
                    "data": {
                        "results": {
                            "songs": {
                                "data": [
                                    {
                                        "id": "hit-track",
                                        "type": "songs",
                                        "attributes": {
                                            "name": "Hit Song",
                                            "artistName": "Hit Artist",
                                            "playParams": {"id": "hit-track", "kind": "songs", "isLibrary": False},
                                        },
                                    }
                                ]
                            }
                        }
                    }
                }
            return {"data": {"results": {"songs": {"data": []}}}}

        def playback_post(self, path: str, body=None):
            return {"path": path, "body": body}

    rpc = _QueryFallbackRpc()
    fallback_service = CiderAgentService(settings, rpc_client=rpc)

    result = fallback_service.play_candidate_match(candidate_queries=["miss", "hit"])

    assert result["selection_strategy"] == "candidate_query_fallback"
    assert result["selected_query"] == "hit"
    assert rpc.queries == ["miss", "hit"]


def test_best_track_match_accepts_title_variants(service) -> None:
    tracks = [
        {
            "title": "Sparkle",
            "artist": "RADWIMPS",
        },
        {
            "title": "Dream Lantern",
            "artist": "RADWIMPS",
        },
    ]

    match = service._search_ctrl._best_track_match(tracks, title="Sparkle (Piano Version)", artist="RADWIMPS")

    assert match is not None
    assert match["title"] == "Sparkle"


def test_run_action_compacts_track_payloads_by_default(service) -> None:
    result = service.run_action(
        "play_candidate_match",
        {
            "candidate_tracks": [{"title": "Liked Song", "artist": "Favorite Artist"}],
        },
    )

    selected_track = result["result"]["selected_track"]
    assert "raw" not in selected_track
    assert selected_track["title"] == "Liked Song"
    assert selected_track["artist"] == "Favorite Artist"
    assert "id" not in selected_track
    assert "href" not in selected_track
    assert "play_params" not in selected_track
    assert "type" not in selected_track
    assert "request_id" not in result


def test_run_action_play_candidate_match_accepts_singular_query_alias(service) -> None:
    result = service.run_action(
        "play_candidate_match",
        {
            "candidate_query": ["k-pop"],
        },
    )

    assert result["action"] == "play_candidate_match"
    assert result["result"]["selection_strategy"] == "candidate_query_fallback"
    assert result["result"]["selected_query"] == "k-pop"


def test_status_handles_is_playing_payload_shape(service) -> None:
    result = service.status()

    assert result["playback"]["is_playing"] is True


def test_paused_session_runtime_survives_restart(settings, service, tmp_path) -> None:
    database_path = tmp_path / "paused-restart.db"
    first = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=database_path,
            config_path=settings.config_path,
        ),
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(database_path),
        resolver=service._resolver.__class__(),
    )
    first.play_session("play upbeat music")
    first.pause()

    restarted = CiderAgentService(
        first._settings,
        rpc_client=first._rpc,
        preference_store=PreferenceStore(database_path),
        resolver=first._resolver,
    )
    restarted.reconcile_session_runtime()

    session = restarted._preferences.get_active_session()
    assert session is not None
    assert restarted._get_session_runtime(session["id"])["suspended"] is True
    assert restarted._session._should_advance_session(session, restarted.playback_snapshot()) is False


def test_reconcile_preserves_current_playing_queue_item(settings, service, tmp_path) -> None:
    database_path = tmp_path / "playing-queue-restart.db"
    rpc = service._rpc.__class__()
    first = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=database_path,
            config_path=settings.config_path,
        ),
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=service._resolver.__class__(),
    )
    first.play_session("play upbeat music")
    session = first._preferences.get_active_session()
    assert session is not None
    assert first.session_queue(include_history=True)["items"][0]["state"] == "playing"
    post_count = len(rpc.posts)

    restarted = CiderAgentService(
        first._settings,
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=first._resolver,
    )
    restarted.reconcile_session_runtime()

    queue = restarted.session_queue(include_history=True)
    assert queue["items"][0]["state"] == "playing"
    assert restarted._get_session_runtime(session["id"])["current_queue_item_id"] == queue["items"][0]["id"]
    assert len(rpc.posts) == post_count


def test_active_stopped_session_remains_eligible_after_restart(settings, service, tmp_path) -> None:
    database_path = tmp_path / "active-restart.db"
    rpc = service._rpc.__class__()
    first = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=database_path,
            config_path=settings.config_path,
        ),
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=service._resolver.__class__(),
    )
    first.play_session("play upbeat music")
    rpc.is_playing = False
    rpc.current_track = None
    session = first._preferences.get_active_session()
    assert session is not None
    first._preferences.upsert_session_runtime(session["id"], last_advance_at="1970-01-01T00:00:00+00:00")

    restarted = CiderAgentService(
        first._settings,
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=first._resolver,
    )
    restarted.reconcile_session_runtime()

    session = restarted._preferences.get_active_session()
    assert session is not None
    assert restarted._get_session_runtime(session["id"])["suspended"] is False
    assert restarted._preferences.get_session_runtime(session["id"])["active_intent"] == "active"
    # No current track is ambiguous: confirm across two stopped snapshots.
    assert restarted._session._should_advance_session(session, restarted.playback_snapshot()) is False
    assert restarted._session._should_advance_session(session, restarted.playback_snapshot()) is True


def test_explicit_play_advances_stopped_active_session_after_restart(settings, service, tmp_path) -> None:
    database_path = tmp_path / "resume-after-restart.db"
    rpc = service._rpc.__class__()
    first = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=database_path,
            config_path=settings.config_path,
        ),
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=service._resolver.__class__(),
    )
    first.play_session("play upbeat music")
    rpc.is_playing = False
    rpc.current_track = None

    restarted = CiderAgentService(
        first._settings,
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=first._resolver,
    )
    restarted.reconcile_session_runtime()

    session = restarted._preferences.get_active_session()
    assert session is not None
    assert restarted._get_session_runtime(session["id"])["suspended"] is False

    result = restarted.play()

    assert result["status"] == "ok"
    assert restarted._get_session_runtime(session["id"])["suspended"] is False
    assert restarted.playback_snapshot()["is_playing"] is True
    assert restarted._session._should_advance_session(session, restarted.playback_snapshot()) is False


def test_reconcile_without_active_session_has_no_runtime(settings, service, tmp_path) -> None:
    database_path = tmp_path / "no-active-session.db"
    restarted = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=database_path,
            config_path=settings.config_path,
        ),
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(database_path),
        resolver=service._resolver.__class__(),
    )

    restarted.reconcile_session_runtime()
    assert restarted._preferences.get_active_session() is None
    assert restarted._session_runtime == {}


def test_construction_has_no_storage_or_rpc_side_effects(settings, service, tmp_path) -> None:
    # CiderAgentService.__init__ must be cheap and deterministic: it should
    # not hit SQLite for active-session lookup or call Cider RPC via
    # playback_snapshot() during construction. Previously __init__ called
    # reconcile_session_runtime() unconditionally (issue #86).
    from vesper.storage import PreferenceStore

    database_path = tmp_path / "side-effects.db"
    rpc = service._rpc.__class__()
    # Pre-seed an active session so reconcile would have side effects if it
    # were still called in __init__.
    store = PreferenceStore(database_path)
    store.start_session(request_text="play upbeat music")

    rpc.playback_get_calls.clear()

    svc = CiderAgentService(
        Settings(
            http_host=settings.http_host,
            http_port=settings.http_port,
            public_base_url=settings.public_base_url,
            cider_base_url=settings.cider_base_url,
            cider_api_token=settings.cider_api_token,
            default_search_source=settings.default_search_source,
            resolver_backend=settings.resolver_backend,
            resolver_base_url=settings.resolver_base_url,
            resolver_model=settings.resolver_model,
            resolver_api_key=settings.resolver_api_key,
            resolver_include_reasoning=settings.resolver_include_reasoning,
            resolver_include_raw_output=settings.resolver_include_raw_output,
            response_detail=settings.response_detail,
            session_recent_tracks_limit=settings.session_recent_tracks_limit,
            global_recent_tracks_limit=settings.global_recent_tracks_limit,
            request_timeout_seconds=settings.request_timeout_seconds,
            verify_tls=settings.verify_tls,
            log_level=settings.log_level,
            database_path=database_path,
            config_path=settings.config_path,
        ),
        rpc_client=rpc,
        preference_store=PreferenceStore(database_path),
        resolver=service._resolver.__class__(),
    )
    try:
        # No playback RPC calls should have been made during construction.
        assert rpc.playback_get_calls == []
        # Session runtime should be empty until reconcile is called explicitly.
        session = svc._preferences.get_active_session()
        assert session is not None
        assert svc._session_runtime == {}
        # Reconcile restores the runtime.
        svc.reconcile_session_runtime()
        assert session["id"] in svc._session_runtime
    finally:
        svc._playback_ctrl.close()


def test_session_events_distinguish_rejection_steering_skip_and_auto_advance(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    service.steer_session("more pop")
    service.reject_current_track()
    service.next_track()

    second_service = service.__class__(
        service._settings,
        rpc_client=service._rpc.__class__(),
        preference_store=PreferenceStore(service._settings.database_path.parent / "event-auto-advance.db"),
        resolver=service._resolver.__class__(),
    )
    second_service.play_session("play upbeat music")
    second_session = second_service._preferences.get_active_session()
    assert second_session is not None
    second_service._rpc.is_playing = False
    second_service._rpc.current_track = None
    second_service._session._set_session_runtime(second_session["id"], last_advance_at=0.0)
    second_service._session._play_session_track(second_session, selection_strategy="adaptive-session-auto-advance")

    event_types = [event["event_type"] for event in service._preferences.list_session_events(session["id"], limit=20)]
    event_types.extend(
        event["event_type"] for event in second_service._preferences.list_session_events(second_session["id"], limit=20)
    )

    assert "session_steered" in event_types
    assert "track_rejected" in event_types
    assert "track_manual_skip" in event_types
    assert "track_auto_advanced" in event_types


def test_check_worker_cancelled_signals_shutdown(service) -> None:
    # No-op while running normally.
    assert service._session._check_worker_cancelled() is None
    service._session._session_worker_stop.set()
    # Direct (non-worker-thread) calls are never cancelled, even once stopped.
    assert service._session._check_worker_cancelled() is None
    # On the worker thread, the stop flag is honored.
    service._session._worker_thread_ident = threading.get_ident()
    with pytest.raises(SessionWorkerCancelled):
        service._session._check_worker_cancelled()


def test_play_session_track_aborts_on_cancel_and_clears_advance_flag(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    # Simulate running on the worker thread, then request shutdown before the
    # advance starts. The entry cancel check must abort before any HTTP work,
    # and advance_in_progress must be cleared so the cancellation does not wedge
    # the session runtime.
    service._session._worker_thread_ident = threading.get_ident()
    service._session._session_worker_stop.set()
    with pytest.raises(SessionWorkerCancelled):
        service._session._play_session_track(session, selection_strategy="adaptive-session-auto-advance")

    assert service._get_session_runtime(session["id"])["advance_in_progress"] is False


def test_close_drains_worker_before_closing_clients(service, monkeypatch) -> None:
    order: list[str] = []
    seen_timeout: dict[str, float] = {}

    original_stop = service.stop_background_session_worker

    def spy_stop(*, timeout: float = 1.0) -> None:
        seen_timeout["timeout"] = timeout
        order.append("stop_worker")
        original_stop(timeout=timeout)

    monkeypatch.setattr(service, "stop_background_session_worker", spy_stop)
    monkeypatch.setattr(service._playback_ctrl, "close", lambda: order.append("playback_close"))
    monkeypatch.setattr(service._rpc, "close", lambda: order.append("rpc_close"))
    monkeypatch.setattr(service._resolver, "close", lambda: order.append("resolver_close"), raising=False)
    monkeypatch.setattr(service._historian, "close", lambda: order.append("historian_close"))

    service.close()

    # The worker must be stopped (and joined) before any client is torn down,
    # and close() must give the worker a join window that covers one in-flight
    # request (request_timeout_seconds). See #4. The reused playback snapshot
    # thread pool is shut down after the worker (its only remaining user) but
    # before the RPC client. See #67.
    assert order == ["stop_worker", "playback_close", "rpc_close", "resolver_close", "historian_close"]
    assert seen_timeout["timeout"] == service._settings.request_timeout_seconds


def test_worker_exits_at_phase_boundary_on_cancel(service) -> None:
    # Advance promptly instead of waiting out the full refill interval.
    service.SESSION_REFILL_INTERVAL_SECONDS = 0.01

    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    # Make the worker want to advance: playback stopped + stale last advance.
    service._rpc.is_playing = False
    service._rpc.current_track = None
    service._session._set_session_runtime(session["id"], last_advance_at=0.0)
    service._preferences.upsert_session_runtime(
        session["id"], last_advance_at="1970-01-01T00:00:00+00:00"
    )

    # Park the worker while it rebuilds the materialized queue so the stop
    # signal arrives while an advance is genuinely in flight.
    gate = threading.Event()
    entered_search = threading.Event()
    original_search = service._rpc.search_catalog

    def blocking_search(query, *, limit, storefront, offset=0):
        entered_search.set()
        gate.wait(timeout=5.0)
        return original_search(query, limit=limit, storefront=storefront, offset=offset)

    service._rpc.search_catalog = blocking_search

    service.start_background_session_worker()
    worker_thread = service._session_worker_thread
    assert worker_thread is not None

    # Wait until the worker is actually blocked mid-materialization.
    assert entered_search.wait(timeout=5.0)

    # Request cooperative shutdown, then release the in-flight materialization.
    service._session._session_worker_stop.set()
    gate.set()

    # The worker must honor the stop at the next phase boundary (before play)
    # instead of continuing through _play_flattened_track and the persist steps.
    worker_thread.join(timeout=5.0)
    assert not worker_thread.is_alive()
    # Cancellation must clear advance_in_progress rather than wedging the session.
    assert service._get_session_runtime(session["id"])["advance_in_progress"] is False
