"""Domain services for Vesper."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import wraps
import random
from typing import Any

from .config import Settings
from .historian import HistorianSink
from .results import EngineActionResult, TextRequestResult
from .resolver import (
    ResolvedAction,
    Resolver,
    SessionSearchSource,
    build_resolver,
)
from .rpc import CiderRpcClient
from .storage import PreferenceStore, close_connections, close_lifecycle_locks
# Shared helpers live in :mod:`vesper.utils` to avoid a circular import with the
# session layer (issue #44). ``_clean_id`` is re-exported here for back-compat
# with tests that import it from ``vesper.service``.
from .utils import _clean_id, _extract_is_playing, _preference_target, _track_payload
# :class:`vesper.session.SessionEngine` can be imported at module top now that
# the session modules no longer import from ``vesper.service`` (issue #44).
from .session import SessionEngine
from .preference_controller import PreferenceController
from .search_controller import SearchController
from .playback_controller import PlaybackController
from .text_request_controller import TextRequestController
from .resolver_debug import ResolverDebugLogger
from .events import EventEmitter


def _historian_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.operation():
            return method(self, *args, **kwargs)

    return wrapped


class CiderAgentService:
    """High-level operations for the Cider agent."""

    SESSION_REFILL_INTERVAL_SECONDS = 5.0
    SESSION_ADVANCE_COOLDOWN_SECONDS = 5.0
    TRACK_SELECTION_POOL_SIZE = 3
    SESSION_SEARCH_RESULT_LIMIT = 100
    SESSION_SEARCH_PAGE_LIMIT = 50
    SESSION_SELECTION_WINDOW_SIZE = 6
    PREFERENCE_SEED_SEARCH_LIMIT = 6
    PREFERENCE_SEED_ARTIST_CAP = 4
    PREFERENCE_SEED_POOL_QUERY = "__preference_seeded__"
    PREFERENCE_SEED_SOURCE = SessionSearchSource(kind="preference", term=PREFERENCE_SEED_POOL_QUERY)
    SESSION_STOREFRONT = "us"

    def __init__(
        self,
        settings: Settings,
        *,
        rpc_client: CiderRpcClient | None = None,
        preference_store: PreferenceStore | None = None,
        resolver: Resolver | None = None,
        historian_sink: HistorianSink | None = None,
    ) -> None:
        self._settings = settings
        self._events = EventEmitter(self, historian_sink)
        self._historian = self._events.sink
        self._rpc = rpc_client or CiderRpcClient(settings, failure_callback=self._record_rpc_failure)
        set_failure_callback = getattr(self._rpc, "set_failure_callback", None)
        if callable(set_failure_callback):
            set_failure_callback(self._record_rpc_failure)
        self._preferences = preference_store or PreferenceStore(settings.database_path)
        self._resolver = resolver or build_resolver(settings)
        self._resolver_debug = ResolverDebugLogger(self)
        self._random = random.SystemRandom()
        # The adaptive-session engine owns the session runtime, the background
        # refill worker, and the search/planning/pool machinery.
        self._session = SessionEngine(
            self,
            rpc=self._rpc,
            preferences=self._preferences,
            resolver=self._resolver,
            settings=self._settings,
        )
        self._preferences_ctrl = PreferenceController(self, preferences=self._preferences)
        self._search_ctrl = SearchController(self, rpc=self._rpc)
        self._playback_ctrl = PlaybackController(self, rpc=self._rpc, preferences=self._preferences, settings=self._settings)
        self._text_request_ctrl = TextRequestController(self)
        self._genre_cache = self._search_ctrl._genre_cache

    @property
    def _session_runtime(self) -> dict[int, dict[str, Any]]:
        # Exposed for tests/debugging; the authoritative runtime lives on the engine.
        return self._session._session_runtime

    def close(self) -> None:
        # Stop the refill worker first and give it long enough to honor the
        # cooperative cancel at its next phase boundary (at most one in-flight
        # request, bounded by request_timeout_seconds) before tearing down the
        # RPC client, resolver, and historian. Otherwise close() can close the
        # very clients the worker thread is still using mid-advance. See #4.
        self.stop_background_session_worker(timeout=self._settings.request_timeout_seconds)
        # Release the reused playback snapshot thread pool now that the
        # background worker (which fans out playback reads through it) has
        # stopped. See #67.
        self._playback_ctrl.close()
        self._rpc.close()
        close = getattr(self._resolver, "close", None)
        if callable(close):
            close()
        self._historian.close()
        # Release cached SQLite connections now that the background worker (the
        # only other thread that could touch the DB) has stopped. See #50.
        close_connections(self._settings.database_path)
        # Drop the per-database lifecycle lock too; with the background
        # session worker stopped there is no remaining holder. See #62.
        close_lifecycle_locks(self._settings.database_path)

    @property
    def _session_worker_thread(self):
        return self._session._session_worker_thread

    def operation(
        self,
        *,
        caller: str = "direct",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        session_id: str | None = None,
    ):
        return self._events.operation(
            caller=caller,
            correlation_id=correlation_id,
            causation_id=causation_id,
            session_id=session_id,
        )

    def _emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        source: str,
        subject: str | None = None,
        session_id: int | str | None = None,
    ) -> str:
        return self._events.emit(
            event_type, data, source=source, subject=subject, session_id=session_id
        )

    def _sanitize_event_data(self, value: Any) -> Any:
        return self._events._sanitize_event_data(value)

    def _caller(self) -> str:
        return self._events.caller()

    def _record_rpc_failure(self, failure: dict[str, Any]) -> None:
        self._events.record_rpc_failure(failure)

    def _record_error(self, component: str, operation: str | None, exc: Exception) -> None:
        self._events.record_error(component, operation, exc)

    def _track_payload(self, track: dict[str, Any] | None) -> dict[str, Any] | None:
        return _track_payload(track)

    def _preference_target(self, preference: dict[str, Any] | None) -> dict[str, Any] | None:
        return _preference_target(preference)

    def _get_active_session(self) -> dict[str, Any] | None:
        return self._preferences.get_active_session()

    def _current_preference_context_query(self, runtime: dict[str, Any]) -> str | None:
        return self._session._current_preference_context_query(runtime)

    def default_search_source(self) -> str:
        return self._settings.default_search_source

    def response_detail(self) -> str:
        return self._settings.response_detail

    def include_timing_debug(self) -> bool:
        return self._settings.include_timing_debug

    def resolver_debug_log_path(self):
        return self._settings.resolver_debug_log_path

    def session_recent_tracks_limit(self) -> int:
        return self._settings.session_recent_tracks_limit

    def session_vibe_rephrase_attempts(self) -> int:
        return self._settings.session_vibe_rephrase_attempts

    def global_recent_tracks_limit(self) -> int:
        return self._settings.global_recent_tracks_limit

    def current_timestamp(self) -> str:
        # Wall-clock UTC ISO-8601. Persisted session-runtime timestamps
        # (last_advance_at, pending_stop_observed_at) must be comparable across
        # processes and after restarts, so they may never be time.monotonic()
        # values; a single UTC source of truth keeps every consumer consistent.
        return datetime.now(UTC).isoformat(timespec="seconds")

    def list_action_definitions(self, *, text_exposable_only: bool = False, public_only: bool = True) -> list[dict[str, Any]]:
        return self._text_request_ctrl.list_action_definitions(
            text_exposable_only=text_exposable_only, public_only=public_only
        )

    def reconcile_session_runtime(self) -> None:
        self._session.reconcile_session_runtime()

    def start_background_session_worker(self) -> None:
        self._session.start_background_session_worker()

    def stop_background_session_worker(self, *, timeout: float = 1.0) -> None:
        self._session.stop_background_session_worker(timeout=timeout)

    def status(self) -> dict[str, Any]:
        return self._playback_ctrl.status()

    def get_now_playing(self) -> dict[str, Any]:
        return self._playback_ctrl.get_now_playing()

    def playback_snapshot(self) -> dict[str, Any]:
        return self._playback_ctrl.playback_snapshot()

    def is_playing(self) -> dict[str, Any]:
        return self._playback_ctrl.is_playing()

    @_historian_operation
    def play(self) -> dict[str, Any]:
        with self.operation():
            session = self._preferences.get_active_session()
            track = None
            if session is not None:
                self._session._set_session_runtime(session["id"], suspended=False)
                self._session._persist_session_runtime(session["id"], suspended=False, last_known_playback_state="playing")
                self._preferences.add_session_event(session["id"], event_type="session_resumed")
                snapshot = self.playback_snapshot()
                track = self._track_payload(snapshot.get("track"))
                if snapshot.get("is_playing") or _clean_id(snapshot.get("track", {}).get("track_id")):
                    result = {"status": "ok", "result": self._rpc.playback_post("/play")}
                else:
                    started_debug_episode = self._begin_resolver_debug_episode("adaptive-session-resume")
                    try:
                        return self._session._play_session_track(session, selection_strategy="adaptive-session-resume")
                    finally:
                        self._end_resolver_debug_episode(started_debug_episode)
            else:
                result = {"status": "ok", "result": self._rpc.playback_post("/play")}
            self._emit(
                "music.playback.started",
                {"caller": self._caller(), "action": "play", "track": track},
                source="app://vesper/playback",
            )
            return result

    @_historian_operation
    def pause(self) -> dict[str, Any]:
        with self.operation():
            session = self._preferences.get_active_session()
            if session is not None:
                self._session._set_session_runtime(session["id"], suspended=True)
                self._session._persist_session_runtime(session["id"], suspended=True, last_known_playback_state="paused")
                self._preferences.add_session_event(session["id"], event_type="session_suspended")
            result = {"status": "ok", "result": self._rpc.playback_post("/pause")}
            self._emit(
                "music.playback.paused",
                {"caller": self._caller()},
                source="app://vesper/playback",
                session_id=session["id"] if session else None,
            )
            return result

    def playpause(self) -> dict[str, Any]:
        return self._playback_ctrl.playpause()

    @_historian_operation
    def stop(self) -> dict[str, Any]:
        with self.operation():
            stopped = self._preferences.stop_active_session()
            if stopped is not None:
                self._session._clear_session_runtime(stopped["id"])
                self._preferences.add_session_event(stopped["id"], event_type="session_stopped")
            result = {"status": "ok", "result": self._rpc.playback_post("/stop")}
            self._emit(
                "music.playback.stopped",
                {"caller": self._caller(), "reason": "stop"},
                source="app://vesper/playback",
                session_id=stopped["id"] if stopped else None,
            )
            if stopped is not None:
                self._emit(
                    "music.session.ended",
                    {
                        "caller": self._caller(),
                        "request": stopped["request_text"],
                        "reason": "playback-stopped",
                    },
                    source="app://vesper/session",
                    subject=str(stopped["id"]),
                    session_id=stopped["id"],
                )
            return result

    @_historian_operation
    def next_track(self) -> dict[str, Any]:
        with self.operation():
            session = self._preferences.get_active_session()
            if session is not None:
                self._session._set_session_runtime(session["id"], suspended=False)
                self._session._persist_session_runtime(session["id"], suspended=False, last_known_playback_state="playing")
                result = self._session._play_session_track_with_debug_episode(
                    session,
                    selection_strategy="adaptive-session-skip",
                    debug_reason="adaptive-session-skip",
                )
            else:
                result = {"status": "ok", "result": self._rpc.playback_post("/next")}
            self._emit(
                "music.track.skipped",
                {"caller": self._caller(), "direction": "next"},
                source="app://vesper/playback",
                session_id=session["id"] if session else None,
            )
            return result

    @_historian_operation
    def previous_track(self) -> dict[str, Any]:
        with self.operation():
            result = {"status": "ok", "result": self._rpc.playback_post("/previous")}
            self._emit(
                "music.track.skipped",
                {"caller": self._caller(), "direction": "previous"},
                source="app://vesper/playback",
            )
            return result

    def get_volume(self) -> dict[str, Any]:
        return self._playback_ctrl.get_volume()

    def set_volume(self, volume: int) -> dict[str, Any]:
        return self._playback_ctrl.set_volume(volume)

    def get_repeat_mode(self) -> dict[str, Any]:
        return self._playback_ctrl.get_repeat_mode()

    def toggle_repeat(self) -> dict[str, Any]:
        return self._playback_ctrl.toggle_repeat()

    def get_shuffle_mode(self) -> dict[str, Any]:
        return self._playback_ctrl.get_shuffle_mode()

    def toggle_shuffle(self) -> dict[str, Any]:
        return self._playback_ctrl.toggle_shuffle()

    def get_autoplay(self) -> dict[str, Any]:
        return self._playback_ctrl.get_autoplay()

    def toggle_autoplay(self) -> dict[str, Any]:
        return self._playback_ctrl.toggle_autoplay()

    def get_queue(self) -> dict[str, Any]:
        return self._playback_ctrl.get_queue()

    def _queue_result(self, payload: Any) -> dict[str, Any]:
        return self._playback_ctrl._queue_result(payload)

    def play_next(self, item: dict[str, Any]) -> dict[str, Any]:
        return self._playback_ctrl.play_next(item)

    def play_later(self, item: dict[str, Any]) -> dict[str, Any]:
        return self._playback_ctrl.play_later(item)

    def move_queue_item(self, from_index: int, to_index: int) -> dict[str, Any]:
        return self._playback_ctrl.move_queue_item(from_index, to_index)

    def remove_queue_item(self, index: int) -> dict[str, Any]:
        return self._playback_ctrl.remove_queue_item(index)

    def clear_queue(self) -> dict[str, Any]:
        return self._playback_ctrl.clear_queue()

    def play_url(self, url: str) -> dict[str, Any]:
        return self._playback_ctrl.play_url(url)

    @_historian_operation
    def play_item(
        self,
        item_id: str,
        *,
        kind: str = "songs",
        is_library: bool = False,
        track: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._playback_ctrl.play_item(item_id, kind=kind, is_library=is_library, track=track)

    def play_item_href(self, href: str) -> dict[str, Any]:
        return self._playback_ctrl.play_item_href(href)

    def search_catalog(self, query: str, *, limit: int = 10, storefront: str = "us", offset: int = 0) -> dict[str, Any]:
        return self._search_ctrl.search_catalog(query, limit=limit, storefront=storefront, offset=offset)

    def search_catalog_tracks(self, query: str, *, limit: int = 10, storefront: str = "us", offset: int = 0) -> dict[str, Any]:
        return self._search_ctrl.search_catalog_tracks(query, limit=limit, storefront=storefront, offset=offset)

    def _catalog_resource_search(
        self,
        query: str,
        *,
        resource_type: str,
        limit: int = 5,
        storefront: str = SESSION_STOREFRONT,
    ) -> list[dict[str, Any]]:
        return self._search_ctrl._catalog_resource_search(query, resource_type=resource_type, limit=limit, storefront=storefront)

    def _catalog_relationship_tracks(
        self,
        path: str,
        *,
        storefront: str = SESSION_STOREFRONT,
    ) -> list[dict[str, Any]]:
        return self._search_ctrl._catalog_relationship_tracks(
            path,
            result_limit=self.SESSION_SEARCH_RESULT_LIMIT,
            page_limit=self.SESSION_SEARCH_PAGE_LIMIT,
            storefront=storefront,
        )

    def _load_genre_map(self, storefront: str = SESSION_STOREFRONT) -> dict[str, str]:
        return self._search_ctrl._load_genre_map(storefront=storefront)

    def session_genre_names(self) -> list[str]:
        return self._search_ctrl.session_genre_names(storefront=self.SESSION_STOREFRONT)

    def session_rejected_search_sources(self, session: dict[str, Any]) -> list[dict[str, str]]:
        return self._session.session_rejected_search_sources(session)

    def search(self, query: str, *, limit: int = 10, storefront: str = "us") -> dict[str, Any]:
        return self._search_ctrl.search(query, limit=limit, storefront=storefront)

    def search_library(self, query: str, *, limit: int = 10, types: list[str] | None = None) -> dict[str, Any]:
        return self._search_ctrl.search_library(query, limit=limit, types=types)

    def search_library_tracks(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        return self._search_ctrl.search_library_tracks(query, limit=limit)

    def list_library_playlists(self, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        return self._search_ctrl.list_library_playlists(limit=limit, offset=offset)

    def search_library_playlists(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        return self._search_ctrl.search_library_playlists(query, limit=limit)

    def get_library_playlist(self, playlist_id: str) -> dict[str, Any]:
        return self._search_ctrl.get_library_playlist(playlist_id)

    def get_library_playlist_tracks(self, playlist_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return self._search_ctrl.get_library_playlist_tracks(playlist_id, limit=limit, offset=offset)

    def play_library_playlist(self, playlist_name: str) -> dict[str, Any]:
        return self._search_ctrl.play_library_playlist(playlist_name)

    def list_recently_played(self, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        return self._search_ctrl.list_recently_played(limit=limit, offset=offset)

    def play_search_result(
        self,
        *,
        query: str,
        source: str = "library",
        index: int = 0,
        storefront: str = "us",
    ) -> dict[str, Any]:
        return self._search_ctrl.play_search_result(query=query, source=source, index=index, storefront=storefront)

    def list_preferences(self) -> dict[str, Any]:
        return self._preferences_ctrl.list_preferences()

    @_historian_operation
    def forget_preference(self, preference_id: int) -> dict[str, Any]:
        return self._preferences_ctrl.forget_preference(preference_id)

    @_historian_operation
    def like_current_track(self) -> dict[str, Any]:
        return self._preferences_ctrl.like_current_track()

    def session_status(self, *, include_recent_tracks: bool = True, compact: bool | None = None) -> dict[str, Any]:
        return self._session.session_status(include_recent_tracks=include_recent_tracks, compact=compact)

    def session_queue(self, *, limit: int = 50, include_history: bool = False) -> dict[str, Any]:
        return self._session.session_queue(limit=limit, include_history=include_history)

    def session_candidates(self, *, window: int = 10) -> dict[str, Any]:
        return self._session.session_candidates(window=window)

    def recent_session_tracks(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self._session.recent_session_tracks(limit=limit)

    def recent_global_tracks(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self._session.recent_global_tracks(limit=limit)

    def session_planning_playback_snapshot(self, session: dict[str, Any]) -> dict[str, Any]:
        return self._session.session_planning_playback_snapshot(session)

    @_historian_operation
    def stop_session(self) -> dict[str, Any]:
        return self._session.stop_session()

    @_historian_operation
    def play_session(self, request: str) -> dict[str, Any]:
        return self._session.play_session(request)

    @_historian_operation
    def steer_session(self, request: str, *, search_update: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._session.steer_session(request, search_update=search_update)

    @_historian_operation
    def refill_active_session(self) -> dict[str, Any]:
        return self._session.refill_active_session()

    @_historian_operation
    def reject_current_track(self) -> dict[str, Any]:
        return self._session.reject_current_track()

    def play_candidate_match(
        self,
        *,
        candidate_tracks: list[dict[str, str]] | None = None,
        candidate_queries: list[str] | None = None,
        storefront: str = "us",
    ) -> dict[str, Any]:
        return self._search_ctrl.play_candidate_match(
            candidate_tracks=candidate_tracks,
            candidate_queries=candidate_queries,
            storefront=storefront,
        )

    def resolve_text_request(self, text: str) -> ResolvedAction:
        return self._text_request_ctrl.resolve_text_request(text)

    def execute_text_request(self, text: str) -> TextRequestResult:
        return self._text_request_ctrl.execute_text_request(text)

    def _execute_text_request(self, text: str) -> TextRequestResult:
        return self._text_request_ctrl._execute_text_request(text)

    def handle_text_request(self, text: str) -> dict[str, Any]:
        return self._text_request_ctrl.handle_text_request(text)

    def execute_action(self, action: str, parameters: dict[str, Any] | None = None) -> EngineActionResult:
        return self._text_request_ctrl.execute_action(action, parameters)

    def run_action(self, action: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._text_request_ctrl.run_action(action, parameters)

    def _sources_payload(self, sources: list[SessionSearchSource]) -> list[dict[str, str]]:
        return [{"kind": source.kind, "term": source.term} for source in sources]

    def _fetch_session_source_results(
        self,
        session: dict[str, Any],
        source: SessionSearchSource,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self._session._fetch_session_source_results(session, source)

    def _begin_resolver_debug_episode(self, reason: str) -> bool:
        return self._resolver_debug.begin_episode(reason)

    def _end_resolver_debug_episode(self, started: bool) -> None:
        self._resolver_debug.end_episode(started)

    def append_resolver_debug_log(
        self,
        *,
        stage: str,
        messages: list[dict[str, Any]],
        response_body: dict[str, Any],
        response_content: str,
    ) -> None:
        self._resolver_debug.append_resolver_log(
            stage=stage,
            messages=messages,
            response_body=response_body,
            response_content=response_content,
        )

    def append_session_debug_log(self, *, stage: str, payload: dict[str, Any]) -> None:
        self._resolver_debug.append_session_log(stage=stage, payload=payload)

    def _get_session_runtime(self, session_id: int) -> dict[str, Any]:
        return self._session._get_session_runtime(session_id)

    def _extract_is_playing(self, payload: Any) -> bool | None:
        return _extract_is_playing(payload)

    def _best_playlist_match(self, playlists: list[dict[str, Any]], *, playlist_name: str) -> dict[str, Any] | None:
        return self._search_ctrl._best_playlist_match(playlists, playlist_name=playlist_name)

    def _play_flattened_track(self, track: dict[str, Any], *, is_library_default: bool) -> dict[str, Any]:
        return self._search_ctrl._play_flattened_track(track, is_library_default=is_library_default)
