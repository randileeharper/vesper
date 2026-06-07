from __future__ import annotations

import pytest

from cider_agent.config import Settings
from cider_agent.errors import CiderRpcError, CiderValidationError, TextRequestExecutionError
from cider_agent.resolver import ResolvedAction
from cider_agent.service import CiderAgentService
from cider_agent.storage import PreferenceStore


def test_create_playlist_is_explicitly_unsupported(service) -> None:
    with pytest.raises(CiderRpcError):
        service.create_playlist(name="Late Night Mix")


def test_add_playlist_tracks_requires_refs(service) -> None:
    with pytest.raises(CiderValidationError):
        service.add_playlist_tracks("playlist-1", track_refs=[])


def test_add_playlist_tracks_is_explicitly_unsupported(service) -> None:
    with pytest.raises(CiderRpcError):
        service.add_playlist_tracks("playlist-1", track_refs=[{"id": "track-1", "type": "songs"}])


def test_preferences_round_trip(service) -> None:
    remembered = service.remember_preference(kind="like", value="k-pop", category="genre")
    listed = service.list_preferences()

    assert remembered["preference"]["value"] == "k-pop"
    assert listed["count"] == 1


def test_recommendation_uses_preferences(service) -> None:
    service.remember_preference(kind="like", value="k-pop", category="genre")

    result = service.recommend()

    assert result["status"] == "ok"
    assert result["recommendation"]["items"][0]["play_params"]["id"] == "catalog-track-1"
    assert result["recommendation"]["source"] == "catalog"


def test_run_action_dispatches_search(service) -> None:
    result = service.run_action("search_library_tracks", {"query": "k-pop", "limit": 3})

    assert result["action"] == "search_library_tracks"
    assert result["result"]["count"] == 1


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


def test_handle_text_request_can_start_adaptive_session(service) -> None:
    result = service.handle_text_request("it's morning - play something upbeat and with energy")

    assert result["resolved_action"]["action"] == "play_session"
    assert result["execution"]["action"] == "play_session"
    assert result["execution"]["result"]["mode"] == "adaptive-session"
    assert "request_id" not in result["execution"]
    assert "plan" not in result["execution"]["result"]["result"]
    assert result["execution"]["result"]["result"]["enqueued_count"] == 0
    assert "primary_track" in result["execution"]["result"]["result"]


def test_steer_session_updates_active_session(service) -> None:
    service.play_session("play upbeat music")

    result = service.steer_session("more pop")

    assert result["session"]["steering_history"][-1] == "more pop"
    assert result["result"]["selection_strategy"] == "adaptive-session-steer"


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


def test_next_track_advances_active_session_without_native_queue(service) -> None:
    service.play_session("play upbeat music")

    result = service.next_track()

    assert result["status"] == "ok"
    assert result["selection_strategy"] == "adaptive-session-skip"
    assert result["tracks"][0]["title"] == "Another Song"
    assert service._rpc.posts[-1]["path"] == "/play-item"


def test_session_worker_advances_when_playback_stops(service) -> None:
    service.play_session("play upbeat music")
    session = service._preferences.get_active_session()
    assert session is not None

    service._rpc.is_playing = False
    service._rpc.current_track = None
    service._set_session_runtime(session["id"], last_advance_at=0.0)

    assert service._should_advance_session(session, service.playback_snapshot()) is True

    result = service._play_session_track(session, selection_strategy="adaptive-session-auto-advance")

    assert result["selection_strategy"] == "adaptive-session-auto-advance"
    assert result["tracks"][0]["title"] == "Another Song"


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
        candidate_artists=["Favorite Artist"],
        candidate_queries=["k-pop"],
    )

    assert result["selection_strategy"] == "candidate_track_exactish_match"
    assert result["selected_track"]["play_params"]["id"] == "catalog-track-favorite"


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
