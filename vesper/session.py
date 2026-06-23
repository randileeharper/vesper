"""Adaptive-session engine for :class:`vesper.service.CiderAgentService`.

These were the ``_session_*`` / background-worker / planning / pool / runtime
methods on the ~3300-line ``CiderAgentService`` god-class, extracted
behavior-preservingly into a cohesive :class:`SessionEngine`. The engine owns
the in-memory session runtime state, the background refill worker thread, and
the search/planning/pool machinery; it does not touch catalog search, playback,
historian emission, resolver-debug logging, or settings directly.

Cross-cutting needs are reached through the :class:`SessionHost` Protocol so
``vesper.session`` never imports the concrete service class. The service
constructs a ``SessionEngine`` and keeps the public session methods as one-line
facade delegates, preserving every existing call site and test.
"""

from __future__ import annotations

import logging
import json
import random
import re
import threading
import time
from datetime import datetime
from typing import Any, Protocol

from .errors import CiderValidationError
from .output import compact_output
from .resolver import SessionQueryPlan, SessionSearchSource, SessionTrackSelection
from .service import (
    _clean_id,
    _elapsed_ms,
    _flatten_playlist_item,
    _normalize_match_text,
    _encode_query,
)

LOGGER = logging.getLogger(__name__)


class SessionWorkerCancelled(Exception):
    """Raised inside the session worker to cooperatively abort an in-flight
    advance once :meth:`SessionEngine.stop_background_session_worker` has been
    requested. The worker loop catches it and exits quietly (instead of logging
    a refill failure), and ``_play_session_task``'s exception handler clears
    ``advance_in_progress`` before re-raising, so cancellation never wedges the
    session runtime. See issue #4."""

    __slots__ = ()


class SessionHost(Protocol):
    """Structural interface for the cross-cutting capabilities SessionEngine
    borrows from its host (the :class:`vesper.service.CiderAgentService`
    facade).

    This is a :class:`typing.Protocol`: the concrete service satisfies it
    structurally without inheriting from it, so ``vesper.session`` does not
    import ``vesper.service`` for the service class itself. Method bodies are
    elided (``...``); defaults are decorative since runtime calls dispatch to
    the host's real implementation.
    """

    def playback_snapshot(self) -> dict[str, Any]:
        ...

    def operation(
        self,
        *,
        caller: str = "direct",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        session_id: str | None = None,
    ):
        ...

    def _emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        source: str,
        subject: str | None = None,
        session_id: int | str | None = None,
    ) -> str:
        ...

    def _record_error(self, component: str, operation: str | None, exc: Exception) -> None:
        ...

    def _caller(self) -> str:
        ...

    def current_timestamp(self) -> str:
        ...

    def include_timing_debug(self) -> bool:
        ...

    def _track_payload(self, track: dict[str, Any] | None) -> dict[str, Any] | None:
        ...

    def _preference_target(self, preference: dict[str, Any] | None) -> dict[str, Any] | None:
        ...

    def _sources_payload(self, sources: list[SessionSearchSource]) -> list[dict[str, str]]:
        ...

    def _catalog_resource_search(
        self,
        query: str,
        *,
        resource_type: str,
        limit: int = 5,
        storefront: str = "us",
    ) -> list[dict[str, Any]]:
        ...

    def _catalog_relationship_tracks(
        self,
        path: str,
        *,
        storefront: str = "us",
    ) -> list[dict[str, Any]]:
        ...

    def search_catalog_tracks(self, query: str, *, limit: int = 10, storefront: str = "us", offset: int = 0) -> dict[str, Any]:
        ...

    def _load_genre_map(self, storefront: str = "us") -> dict[str, str]:
        ...

    def _play_flattened_track(self, track: dict[str, Any], *, is_library_default: bool) -> dict[str, Any]:
        ...

    def append_session_debug_log(self, *, stage: str, payload: dict[str, Any]) -> None:
        ...

    def append_resolver_debug_log(
        self,
        *,
        stage: str,
        messages: list[dict[str, Any]],
        response_body: dict[str, Any],
        response_content: str,
    ) -> None:
        ...

    def _begin_resolver_debug_episode(self, reason: str) -> bool:
        ...

    def _end_resolver_debug_episode(self, started: bool) -> None:
        ...

    def response_detail(self) -> str:
        ...

    def session_recent_tracks_limit(self) -> int:
        ...

    def global_recent_tracks_limit(self) -> int:
        ...

    def _fetch_session_source_results(
        self,
        session: dict[str, Any],
        source: SessionSearchSource,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ...


class SessionEngine:
    """Owns adaptive-session runtime state, the refill worker, and the
    search/planning/pool machinery. Constructed by and delegating cross-cutting
    work to a :class:`SessionHost`.
    """

    def __init__(self, host: SessionHost, *, rpc, preferences, resolver, settings) -> None:
        self._host = host
        self._rpc = rpc
        self._preferences = preferences
        self._resolver = resolver
        self._settings = settings
        self._session_worker_thread: threading.Thread | None = None
        self._session_worker_stop = threading.Event()
        self._worker_thread_ident: int | None = None
        self._session_worker_lock = threading.Lock()
        self._session_runtime_lock = threading.Lock()
        self._session_runtime: dict[int, dict[str, Any]] = {}
        self._session_advance_lock = threading.Lock()
        self._random = random.SystemRandom()
        self._debug_candidate_track_search_count = 0
        self._debug_candidate_track_search_ms = 0.0
        self._debug_candidate_artist_search_count = 0
        self._debug_candidate_artist_search_ms = 0.0
        self._debug_candidate_query_search_count = 0
        self._debug_candidate_query_search_ms = 0.0
        self._debug_selection_candidate_count = 0


    def reconcile_session_runtime(self) -> None:
        session = self._preferences.get_active_session()
        self._clear_all_session_runtime()
        if session is None:
            return
        stored_runtime = self._preferences.get_session_runtime(session["id"]) or {}
        playback = self._host.playback_snapshot()
        # Playback naturally becomes stopped between adaptive-session tracks.
        # Starting another service process during that window (for example, a
        # one-shot CLI status request) must not turn the shared session into an
        # explicitly suspended one. Only persisted user intent may suspend it.
        suspended = stored_runtime.get("active_intent") == "suspended"
        self._set_session_runtime(
            session["id"],
            suspended=suspended,
            last_advance_at=stored_runtime.get("last_advance_at"),
            last_selected_track_id=stored_runtime.get("last_selected_track_id"),
            last_known_playback_state=stored_runtime.get("last_known_playback_state"),
        )
        self._persist_session_runtime(
            session["id"],
            suspended=suspended,
            last_selected_track_id=stored_runtime.get("last_selected_track_id"),
            last_known_playback_state="playing" if playback.get("is_playing") else "stopped",
            preserve_last_advance=True,
        )
        if playback.get("is_playing"):
            self._record_current_track_for_session(session, playback=playback, event_type="track_started")

    def start_background_session_worker(self) -> None:
        with self._session_worker_lock:
            if self._session_worker_thread is not None and self._session_worker_thread.is_alive():
                return
            self._session_worker_stop.clear()
            self._session_worker_thread = threading.Thread(
                target=self._session_worker_loop,
                name="vesper-session-worker",
                daemon=True,
            )
            self._session_worker_thread.start()
        with self._host.operation(caller="startup"):
            self._host._emit(
                "music.worker.started",
                {"caller": self._host._caller(), "worker": "adaptive-session"},
                source="app://vesper/worker",
                subject="adaptive-session",
            )

    def stop_background_session_worker(self, *, timeout: float = 1.0) -> None:
        with self._session_worker_lock:
            self._session_worker_stop.set()
            thread = self._session_worker_thread
            self._session_worker_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if thread is not None:
            with self._host.operation(caller="startup"):
                self._host._emit(
                    "music.worker.stopped",
                    {"caller": self._host._caller(), "worker": "adaptive-session"},
                    source="app://vesper/worker",
                    subject="adaptive-session",
                )

    def session_rejected_search_sources(self, session: dict[str, Any]) -> list[dict[str, str]]:
        runtime = self._get_session_runtime(int(session.get("id", 0)))
        return [
            {"kind": source.kind, "term": source.term}
            for source in self._normalize_search_sources(runtime.get("rejected_search_sources"))
        ]

    def session_status(self, *, include_recent_tracks: bool = True, compact: bool | None = None) -> dict[str, Any]:
        session = self._preferences.get_active_session()
        if session is None:
            payload = {"status": "ok", "session": None}
            return compact_output(payload, self._host.include_timing_debug()) if compact is not False and self._host.response_detail() == "compact" else payload
        payload = {"status": "ok", "session": session}
        if include_recent_tracks:
            payload["recent_tracks"] = self.recent_session_tracks(limit=self._host.session_recent_tracks_limit())
        if compact is False:
            return payload
        if compact is True or self._host.response_detail() == "compact":
            return compact_output(payload, self._host.include_timing_debug())
        return payload

    def recent_session_tracks(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        session = self._preferences.get_active_session()
        if session is None:
            return []
        return self._preferences.list_session_tracks(session["id"], limit=limit or self._host.session_recent_tracks_limit())

    def recent_global_tracks(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self._preferences.list_recent_tracks(limit=limit or self._host.global_recent_tracks_limit())

    def session_planning_playback_snapshot(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = session.get("id")
        if session_id is not None:
            runtime = self._get_session_runtime(int(session_id))
            cached = runtime.get("planning_playback_snapshot")
            if isinstance(cached, dict):
                return dict(cached)
        return self._host.playback_snapshot()

    def stop_session(self) -> dict[str, Any]:
        stopped = self._preferences.stop_active_session()
        if stopped is not None:
            self._clear_session_runtime(stopped["id"])
            self._preferences.add_session_event(stopped["id"], event_type="session_stopped")
            self._host._emit(
                "music.session.ended",
                {
                    "caller": self._host._caller(),
                    "request": stopped["request_text"],
                    "reason": "explicit-stop",
                },
                source="app://vesper/session",
                subject=str(stopped["id"]),
                session_id=stopped["id"],
            )
        return {"status": "ok", "stopped": stopped is not None, "session": stopped}

    def play_session(self, request: str) -> dict[str, Any]:
        if not request.strip():
            raise CiderValidationError("request cannot be empty.")
        previous = self._preferences.get_active_session()
        session = self._preferences.start_session(request_text=request.strip())
        if previous is not None:
            self._host._emit(
                "music.session.ended",
                {
                    "caller": self._host._caller(),
                    "request": previous["request_text"],
                    "reason": "replaced",
                },
                source="app://vesper/session",
                subject=str(previous["id"]),
                session_id=previous["id"],
            )
        self._clear_all_session_runtime()
        self._set_session_runtime(session["id"], suspended=False, active_search_sources=[], query_pools={})
        self._persist_session_runtime(session["id"], suspended=False, last_known_playback_state="starting")
        self._preferences.add_session_event(session["id"], event_type="session_started", metadata={"request": request.strip()})
        self._host._emit(
            "music.session.started",
            {
                "caller": self._host._caller(),
                "request": request.strip(),
                "mode": session["mode"],
            },
            source="app://vesper/session",
            subject=str(session["id"]),
            session_id=session["id"],
        )
        try:
            self._rpc.playback_post("/queue/clear-queue")
            result = self._play_session_track_with_debug_episode(
                session,
                selection_strategy="adaptive-session-start",
                debug_reason=f"adaptive-session-start: {request.strip()}",
            )
        except Exception as exc:
            self._abort_failed_session_start(session["id"])
            self._host._emit(
                "music.session.ended",
                {
                    "caller": self._host._caller(),
                    "request": request.strip(),
                    "reason": "startup-failed",
                },
                source="app://vesper/session",
                subject=str(session["id"]),
                session_id=session["id"],
            )
            self._host._record_error("session", "play_session", exc)
            raise
        return {
            "status": "ok",
            "mode": "adaptive-session",
            "session": self._preferences.get_session(session["id"]),
            "result": result,
        }

    def steer_session(self, request: str, *, search_update: dict[str, Any] | None = None) -> dict[str, Any]:
        if not request.strip():
            raise CiderValidationError("request cannot be empty.")
        session = self._preferences.get_active_session()
        if session is None:
            raise CiderValidationError("No active session is running.")
        session = self._preferences.add_session_steering(session["id"], request.strip())
        runtime = self._get_session_runtime(session["id"])
        resolved_search_update = self._normalize_session_search_update(search_update)
        next_sources = self._next_session_search_sources(runtime, resolved_search_update)
        current_sources = self._normalize_search_sources(runtime.get("active_search_sources"))
        self._set_session_runtime(session["id"], suspended=False, active_search_sources=self._host._sources_payload(next_sources))
        if resolved_search_update["mode"] == "replace" and next_sources != current_sources:
            self._replace_session_query_pools(session, next_sources)
        elif resolved_search_update["mode"] == "add":
            current_keys = {self._session_source_key(source) for source in current_sources}
            added_sources = [source for source in next_sources if self._session_source_key(source) not in current_keys]
            if added_sources:
                self._ensure_session_query_pools(session, added_sources)
        self._persist_session_runtime(session["id"], suspended=False, last_known_playback_state="playing")
        self._preferences.add_session_event(
            session["id"],
            event_type="session_steered",
            metadata={"request": request.strip(), "search_update": resolved_search_update},
        )
        self._host._emit(
            "music.session.steered",
            {
                "caller": self._host._caller(),
                "request": session["request_text"],
                "steering": request.strip(),
                "search_update": resolved_search_update,
            },
            source="app://vesper/session",
            subject=str(session["id"]),
            session_id=session["id"],
        )
        playback = self._host.playback_snapshot()
        result = {
            "status": "ok",
            "selection_strategy": "adaptive-session-steer",
            "playback": playback,
            "tracks": [],
            "deferred_until_next_track": True,
            "search_update": resolved_search_update,
        }
        return {
            "status": "ok",
            "mode": "adaptive-session",
            "session": self._preferences.get_session(session["id"]),
            "result": result,
        }

    def refill_active_session(self) -> dict[str, Any]:
        session = self._preferences.get_active_session()
        if session is None:
            raise CiderValidationError("No active session is running.")
        self._set_session_runtime(session["id"], suspended=False)
        self._persist_session_runtime(session["id"], suspended=False, last_known_playback_state="playing")
        self._preferences.add_session_event(session["id"], event_type="session_manual_advance")
        result = self._play_session_track_with_debug_episode(
            session,
            selection_strategy="adaptive-session-manual-advance",
            debug_reason="adaptive-session-manual-advance",
        )
        return {
            "status": "ok",
            "mode": "adaptive-session",
            "session": self._preferences.get_session(session["id"]),
            "result": result,
        }

    def reject_current_track(self) -> dict[str, Any]:
        playback = self._host.playback_snapshot()
        current = playback.get("track", {})
        current_id = _clean_id(current.get("track_id"))
        if not current_id:
            raise CiderValidationError("No current track is available to reject.")
        rejected = self._preferences.record_global_rejected_track(
            track_id=current_id,
            title=str(current.get("title", "")).strip() or None,
            artist_name=str(current.get("artist", "")).strip() or None,
            album=str(current.get("album", "")).strip() or None,
            item_kind=str(current.get("kind", "")).strip() or None,
            is_library=bool(current.get("is_library")) if current.get("is_library") is not None else None,
        )
        self._host._emit(
            "music.preference.recorded",
            {
                "caller": self._host._caller(),
                "preference_id": rejected["id"],
                "preference_type": rejected["preference_type"],
                "polarity": "avoid",
                "target": self._host._preference_target(rejected),
                "reason": "current track rejected",
            },
            source="app://vesper/preferences",
            subject=str(rejected["id"]),
        )
        session = self._preferences.get_active_session()
        if session is None:
            native = {"status": "ok", "result": self._rpc.playback_post("/next")}
            return {
                "status": "ok",
                "mode": "playback",
                "rejected_track_id": current_id,
                "result": native,
            }
        if current_id:
            self._mark_session_track_rejected(session["id"], current_id)
            self._preferences.add_session_event(
                session["id"],
                event_type="track_rejected",
                track={
                    "track_id": current_id,
                    "title": current.get("title"),
                    "artist": current.get("artist"),
                    "album": current.get("album"),
                    "href": None,
                },
            )
        self._set_session_runtime(session["id"], suspended=False)
        self._persist_session_runtime(session["id"], suspended=False, last_known_playback_state="playing")
        result = self._play_session_track_with_debug_episode(
            session,
            selection_strategy="adaptive-session-reject-current",
            debug_reason="adaptive-session-reject-current",
        )
        return {
            "status": "ok",
            "mode": "adaptive-session",
            "session": self._preferences.get_session(session["id"]),
            "result": result,
        }

    def _check_worker_cancelled(self) -> None:
        """Raise :class:`SessionWorkerCancelled` if the background worker has
        been asked to stop. Called at phase boundaries inside an advance so a
        shutdown request is honored promptly (before the next HTTP call)
        instead of running the full plan/collect/play chain — which lets
        ``close()`` tear down the RPC/resolver/historian without the worker
        still using them.

        Confined to the worker thread (``_worker_thread_ident``): direct,
        user-initiated advances (``play_session``, ``steer_session``, ...) run
        on other threads and must never be cancelled, since the stop flag stays
        set after a stop until the worker is restarted. See issue #4."""
        if self._session_worker_stop.is_set() and threading.get_ident() == self._worker_thread_ident:
            raise SessionWorkerCancelled()

    def _session_worker_loop(self) -> None:
        self._worker_thread_ident = threading.get_ident()
        while not self._session_worker_stop.wait(self._host.SESSION_REFILL_INTERVAL_SECONDS):
            with self._host.operation(caller="worker"):
                try:
                    self._check_worker_cancelled()
                    session = self._preferences.get_active_session()
                    if session is None:
                        continue
                    playback = self._host.playback_snapshot()
                    self._record_current_track_for_session(session, playback=playback)
                    if self._should_advance_session(session, playback):
                        self._play_session_track_with_debug_episode(
                            session,
                            selection_strategy="adaptive-session-auto-advance",
                            debug_reason="adaptive-session-auto-advance",
                        )
                except SessionWorkerCancelled:
                    # Cooperative shutdown: exit the loop without logging a
                    # failure. Any in-flight phase already cleared its state.
                    break
                except CiderValidationError as exc:
                    self._host._record_error("worker", "adaptive-session-auto-advance", exc)
                    LOGGER.warning("Adaptive session worker could not advance session: %s", exc)
                except Exception as exc:
                    self._host._record_error("worker", "adaptive-session-refill", exc)
                    LOGGER.exception("Adaptive session worker failed during refill loop.")
        self._worker_thread_ident = None

    def _play_session_track_with_debug_episode(
        self,
        session: dict[str, Any],
        *,
        selection_strategy: str,
        debug_reason: str,
    ) -> dict[str, Any]:
        started_debug_episode = self._host._begin_resolver_debug_episode(debug_reason)
        try:
            return self._play_session_track(session, selection_strategy=selection_strategy)
        finally:
            self._host._end_resolver_debug_episode(started_debug_episode)

    def _play_session_track(self, session: dict[str, Any], *, selection_strategy: str) -> dict[str, Any]:
        with self._session_advance_lock:
            total_started_at = time.perf_counter()
            timings: dict[str, Any] = {}
            self._set_session_runtime(
                session["id"],
                suspended=False,
                advance_in_progress=True,
                last_advance_at=self._host.current_timestamp(),
            )
            self._persist_session_runtime(session["id"], suspended=False, last_advance_at=self._host.current_timestamp())
            try:
                self._check_worker_cancelled()
                playback_started_at = time.perf_counter()
                playback = self._host.playback_snapshot()
                timings["playback_snapshot_ms"] = _elapsed_ms(playback_started_at)
                self._set_session_runtime(session["id"], planning_playback_snapshot=playback)
                record_current_started_at = time.perf_counter()
                self._record_current_track_for_session(session, playback=playback)
                timings["record_current_track_ms"] = _elapsed_ms(record_current_started_at)
                self._check_worker_cancelled()
                planning_started_at = time.perf_counter()
                plan = self._plan_session_query(session, count=1)
                timings["plan_session_ms"] = _elapsed_ms(planning_started_at)
                self._check_worker_cancelled()
                collect_started_at = time.perf_counter()
                tracks, search_source, selection = self._collect_session_tracks(session, plan, limit=1, timings=timings)
                if not tracks and getattr(plan, "resolver", "") != "preference-seeded":
                    self._check_worker_cancelled()
                    self._reject_session_plan_sources(session["id"], plan)
                    replan_started_at = time.perf_counter()
                    plan = self._plan_session_query(session, count=1, force_replan=True)
                    timings["replan_session_ms"] = _elapsed_ms(replan_started_at)
                    collect_retry_started_at = time.perf_counter()
                    tracks, search_source, selection = self._collect_session_tracks(session, plan, limit=1, timings=timings)
                    timings["collect_retry_ms"] = _elapsed_ms(collect_retry_started_at)
                timings["collect_tracks_ms"] = _elapsed_ms(collect_started_at)
                if not tracks:
                    raise CiderValidationError("No playable candidate match could be resolved.")
                lead_track = tracks[0]
                public_search_kind = "vibe" if search_source.kind == "legacy" else search_source.kind
                self._check_worker_cancelled()
                play_started_at = time.perf_counter()
                playback_result = self._host._play_flattened_track(lead_track, is_library_default=False)
                timings["play_track_ms"] = _elapsed_ms(play_started_at)
                record_selected_started_at = time.perf_counter()
                self._preferences.add_session_track(session["id"], lead_track)
                self._record_session_selection_event(session["id"], lead_track, selection_strategy=selection_strategy)
                self._host._emit(
                    "music.session.track_selected",
                    {
                        "caller": self._host._caller(),
                        "request": session["request_text"],
                        "selection_strategy": selection_strategy,
                        "search_query": search_source.term,
                        "search_kind": public_search_kind,
                        "track": self._host._track_payload(lead_track),
                    },
                    source="app://vesper/session",
                    subject=_clean_id(lead_track.get("id")) or str(session["id"]),
                    session_id=session["id"],
                )
                timings["record_selected_track_ms"] = _elapsed_ms(record_selected_started_at)
                touch_started_at = time.perf_counter()
                self._preferences.touch_session_refill(session["id"])
                timings["touch_session_ms"] = _elapsed_ms(touch_started_at)
                self._set_session_runtime(
                    session["id"],
                    suspended=False,
                    advance_in_progress=False,
                    last_advance_at=self._host.current_timestamp(),
                    last_selected_track_id=_clean_id(lead_track.get("play_params", {}).get("id")),
                    last_known_playback_state="playing",
                    planning_playback_snapshot=None,
                )
                self._persist_session_runtime(
                    session["id"],
                    suspended=False,
                    last_advance_at=self._host.current_timestamp(),
                    last_selected_track_id=_clean_id(lead_track.get("play_params", {}).get("id")),
                    last_known_playback_state="playing",
                )
                result = {
                    "status": "ok",
                    "selection_strategy": selection_strategy,
                    "playback": playback_result,
                    "enqueued_count": 0,
                    "tracks": [lead_track],
                    "plan": {
                        "search_sources": self._host._sources_payload(plan.search_sources),
                    },
                    "selection": {
                        "search_query": search_source.term,
                        "search_kind": public_search_kind,
                        "selected_index": selection.selected_index,
                    },
                }
                if self._host.include_timing_debug():
                    timings["total_ms"] = _elapsed_ms(total_started_at)
                    result["timings"] = timings
                return result
            except Exception:
                self._set_session_runtime(session["id"], advance_in_progress=False, planning_playback_snapshot=None)
                raise

    def _plan_session_query(self, session: dict[str, Any], *, count: int, force_replan: bool = False) -> SessionQueryPlan:
        runtime = self._get_session_runtime(session["id"])
        active_sources = self._normalize_search_sources(runtime.get("active_search_sources"))
        if active_sources and not force_replan:
            self._ensure_session_query_pools(session, active_sources)
            return SessionQueryPlan(search_sources=active_sources[:count] or active_sources, resolver="session-runtime")
        if self._is_vague_play_request(session.get("request_text")):
            seeded_sources = self._bootstrap_preference_seeded_session(session)
            if seeded_sources:
                self._set_session_runtime(session["id"], active_search_sources=self._host._sources_payload(seeded_sources))
                return SessionQueryPlan(search_sources=seeded_sources[:count] or seeded_sources, resolver="preference-seeded")
        planner = getattr(self._host._resolver, "plan_session", None)
        if not callable(planner):
            raise CiderValidationError("The configured resolver does not support adaptive play sessions.")
        request = self._session_effective_request(session)
        plan = planner(request, self._host, session, count)
        resolved_sources = self._plan_search_sources(plan)
        if force_replan:
            rejected_keys = {
                self._session_source_key(source)
                for source in self._normalize_search_sources(runtime.get("rejected_search_sources"))
            }
            resolved_sources = [
                source for source in resolved_sources if self._session_source_key(source) not in rejected_keys
            ]
        normalized_plan = SessionQueryPlan(
            search_sources=resolved_sources,
            resolver=getattr(plan, "resolver", "unknown"),
            raw=getattr(plan, "raw", None),
            reasoning=getattr(plan, "reasoning", None),
            raw_content=getattr(plan, "raw_content", None),
        )
        if resolved_sources:
            self._set_session_runtime(
                session["id"],
                active_search_sources=self._host._sources_payload(resolved_sources),
                query_pools={},
            )
        return normalized_plan

    def _session_effective_request(self, session: dict[str, Any]) -> str:
        steering = session.get("steering_history", [])
        if not steering:
            return str(session.get("request_text", "")).strip()
        steering_text = " ".join(str(item).strip() for item in steering if str(item).strip())
        if not steering_text:
            return str(session.get("request_text", "")).strip()
        return f"{session.get('request_text', '').strip()} Current steering: {steering_text}".strip()

    def _collect_session_tracks(
        self,
        session: dict[str, Any],
        plan: SessionQueryPlan,
        *,
        limit: int,
        timings: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], SessionSearchSource, SessionTrackSelection]:
        chosen, search_source, selection = self._collect_session_tracks_from_pools(session, plan, limit=limit)

        if timings is not None and self._host.include_timing_debug():
            timings["candidate_track_search_count"] = getattr(self, "_debug_candidate_track_search_count", 0)
            timings["candidate_track_search_ms"] = round(getattr(self, "_debug_candidate_track_search_ms", 0.0), 2)
            timings["candidate_artist_search_count"] = getattr(self, "_debug_candidate_artist_search_count", 0)
            timings["candidate_artist_search_ms"] = round(getattr(self, "_debug_candidate_artist_search_ms", 0.0), 2)
            timings["candidate_query_search_count"] = getattr(self, "_debug_candidate_query_search_count", 0)
            timings["candidate_query_search_ms"] = round(getattr(self, "_debug_candidate_query_search_ms", 0.0), 2)
            timings["selected_track_count"] = len(chosen)
            timings["selection_candidate_count"] = getattr(self, "_debug_selection_candidate_count", 0)
            timings["query_pool_count"] = len(
                self._normalize_session_query_pools(self._get_session_runtime(session["id"]).get("query_pools"))
            )

        return chosen, search_source, selection

    def _collect_session_tracks_from_pools(
        self,
        session: dict[str, Any],
        plan: SessionQueryPlan,
        *,
        limit: int,
    ) -> tuple[list[dict[str, Any]], SessionSearchSource, SessionTrackSelection]:
        self._debug_candidate_track_search_count = 0
        self._debug_candidate_track_search_ms = 0.0
        self._debug_candidate_artist_search_count = 0
        self._debug_candidate_artist_search_ms = 0.0
        self._debug_selection_candidate_count = 0
        self._debug_candidate_query_search_count = 0
        self._debug_candidate_query_search_ms = 0.0
        empty_selection = SessionTrackSelection(selected_index=0, resolver="fallback")

        search_sources = self._plan_search_sources(plan)
        last_source = SessionSearchSource(kind="vibe", term="")
        for source in search_sources:
            last_source = source
            source_key = self._session_source_key(source)
            while True:
                self._check_worker_cancelled()
                window = self._next_session_candidate_window(session, source)
                if not window:
                    break
                candidates = [entry["track"] for entry in window]
                self._debug_selection_candidate_count = len(candidates)
                selection = self._select_session_track(session, plan, source.term, candidates)
                if selection.selected_index < 0:
                    self._mark_session_selection_window_screened_out(session["id"], source_key, window)
                    continue
                chosen_index = min(selection.selected_index, len(candidates) - 1)
                chosen_entry = window[chosen_index]
                self._mark_session_track_played(session["id"], source_key, window, chosen_entry["index"])
                return [chosen_entry["track"]][:limit], source, selection

        return [], last_source, empty_selection

    def _normalize_session_search_update(self, value: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"mode": "preserve", "sources": []}
        mode = str(value.get("mode", "preserve")).strip().lower()
        if mode not in {"preserve", "add", "replace"}:
            mode = "preserve"
        sources = self._normalize_search_sources(value.get("sources"))
        if mode in {"add", "replace"} and not sources:
            mode = "preserve"
        if mode == "preserve":
            sources = []
        return {"mode": mode, "sources": self._host._sources_payload(sources)}

    def _normalize_search_sources(self, value: Any) -> list[SessionSearchSource]:
        if isinstance(value, SessionSearchSource):
            value = [value]
        if not isinstance(value, list):
            return []
        sources: list[SessionSearchSource] = []
        seen: set[str] = set()
        for item in value:
            if isinstance(item, SessionSearchSource):
                kind, term = item.kind, item.term
            elif isinstance(item, dict):
                kind, term = item.get("kind"), item.get("term")
            else:
                continue
            kind = str(kind or "").strip().lower()
            term = str(term or "").strip()
            if kind not in {"artist", "genre", "vibe", "preference", "legacy"} or not term:
                continue
            source = SessionSearchSource(kind=kind, term=term)
            key = self._session_source_key(source)
            if key in seen:
                continue
            seen.add(key)
            sources.append(source)
        return sources

    def _plan_search_sources(self, plan: Any) -> list[SessionSearchSource]:
        sources = self._normalize_search_sources(getattr(plan, "search_sources", []))
        if sources:
            return sources
        # Transitional compatibility for third-party resolvers.
        return [
            SessionSearchSource(kind="legacy", term=query)
            for query in self._normalize_search_queries(getattr(plan, "search_queries", []))
        ]

    def _session_source_key(self, source: SessionSearchSource) -> str:
        if isinstance(source, str):
            source = SessionSearchSource(kind="legacy", term=source)
        return json.dumps(
            {"kind": source.kind, "term": source.term.casefold()},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _normalize_search_queries(self, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        queries: list[str] = []
        seen: set[str] = set()
        for item in value:
            query = str(item).strip()
            if not query:
                continue
            lowered = query.casefold()
            if lowered in seen:
                continue
            seen.add(lowered)
            queries.append(query)
        return queries

    def _next_session_search_sources(
        self,
        runtime: dict[str, Any],
        search_update: dict[str, Any],
    ) -> list[SessionSearchSource]:
        current_sources = self._normalize_search_sources(runtime.get("active_search_sources"))
        mode = search_update.get("mode", "preserve")
        new_sources = self._normalize_search_sources(search_update.get("sources"))
        if mode == "replace":
            return new_sources
        if mode == "add":
            merged = list(current_sources)
            seen = {self._session_source_key(source) for source in merged}
            for source in new_sources:
                key = self._session_source_key(source)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(source)
            return merged
        return current_sources

    def _reject_session_plan_sources(self, session_id: int, plan: SessionQueryPlan) -> None:
        runtime = self._get_session_runtime(session_id)
        rejected = self._normalize_search_sources(runtime.get("rejected_search_sources"))
        keys = {self._session_source_key(source) for source in rejected}
        for source in self._plan_search_sources(plan):
            if self._session_source_key(source) not in keys:
                rejected.append(source)
        self._set_session_runtime(session_id, rejected_search_sources=self._host._sources_payload(rejected))

    def _filter_session_search_candidates(
        self,
        tracks: list[dict[str, Any]],
        excluded_ids: set[str],
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for track in tracks:
            match_id = str(track.get("id", "")).strip()
            if match_id and (match_id in excluded_ids or match_id in seen_ids):
                continue
            filtered.append(track)
            if match_id:
                seen_ids.add(match_id)
        return filtered

    def _select_session_track(
        self,
        session: dict[str, Any],
        plan: SessionQueryPlan,
        search_query: str,
        candidates: list[dict[str, Any]],
    ) -> SessionTrackSelection:
        chooser = getattr(self._host._resolver, "select_session_track", None)
        if not callable(chooser):
            return SessionTrackSelection(selected_index=0, resolver="fallback")
        request = self._session_effective_request(session)
        return chooser(request, self._host, session, search_query, candidates)

    def _normalize_session_query_pools(self, value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for raw_key, raw_pool in value.items():
            key = str(raw_key).strip()
            if not key or not isinstance(raw_pool, dict):
                continue
            source_values = raw_pool.get("source")
            sources = self._normalize_search_sources([source_values] if isinstance(source_values, dict) else [])
            if not sources:
                legacy_query = str(raw_pool.get("search_query") or key).strip()
                sources = [SessionSearchSource(kind="vibe", term=legacy_query)] if legacy_query else []
            if not sources:
                continue
            source = sources[0]
            raw_entries = raw_pool.get("entries", [])
            entries: list[dict[str, Any]] = []
            if isinstance(raw_entries, list):
                for raw_entry in raw_entries:
                    if not isinstance(raw_entry, dict) or not isinstance(raw_entry.get("track"), dict):
                        continue
                    state = str(raw_entry.get("state", "fresh")).strip().lower()
                    if state not in {"fresh", "played", "screened_out", "rejected"}:
                        state = "fresh"
                    entries.append({"track": dict(raw_entry["track"]), "state": state})
            cursor = raw_pool.get("cursor", 0)
            if not isinstance(cursor, int):
                cursor = 0
            normalized[key] = {
                "source": {"kind": source.kind, "term": source.term},
                "search_query": source.term,
                "cursor": cursor % len(entries) if entries else 0,
                "entries": entries,
            }
            for metadata_key in ("resolved_artist_id", "resolved_genre_id", "resolved_playlist_id", "resolved_name"):
                if raw_pool.get(metadata_key) is not None:
                    normalized[key][metadata_key] = raw_pool[metadata_key]
        return normalized

    def _session_query_pool_build_excluded_ids(self) -> set[str]:
        excluded = set(self._preferences.globally_rejected_track_ids())
        for track in self.recent_global_tracks(limit=self._host.global_recent_tracks_limit()):
            track_id = _clean_id(track.get("track_id"))
            if track_id:
                excluded.add(track_id)
        return excluded

    def _build_session_query_pool(self, session: dict[str, Any], source: SessionSearchSource) -> dict[str, Any]:
        if isinstance(source, str):
            source = SessionSearchSource(kind="legacy", term=source)
        raw_tracks, metadata = self._host._fetch_session_source_results(session, source)
        global_rejected_ids = self._preferences.globally_rejected_track_ids()
        recent_track_ids = {
            _clean_id(track.get("track_id"))
            for track in self.recent_global_tracks(limit=self._host.global_recent_tracks_limit())
            if _clean_id(track.get("track_id"))
        }
        cached_tracks = self._filter_session_search_candidates(raw_tracks, recent_track_ids | global_rejected_ids)
        # Global recent history should influence pool creation, but it should not
        # dead-end a brand-new pool when real results exist.
        if not cached_tracks and raw_tracks and recent_track_ids:
            cached_tracks = self._filter_session_search_candidates(raw_tracks, global_rejected_ids)
        self._host.append_session_debug_log(
            stage="session_query_pool_built",
            payload={
                "session_id": session.get("id"),
                "search_source": {"kind": source.kind, "term": source.term},
                "resolved_resource": metadata,
                "cursor": 0,
                "raw_track_count": len(raw_tracks),
                "pool_track_count": len(cached_tracks),
                "sample_tracks": [
                    {
                        "id": track.get("id"),
                        "title": track.get("title"),
                        "artist": track.get("artist"),
                        "album": track.get("album"),
                    }
                    for track in cached_tracks[:12]
                ],
            },
        )
        return {
            "source": {"kind": source.kind, "term": source.term},
            "search_query": source.term,
            "cursor": 0,
            "entries": [{"track": track, "state": "fresh"} for track in cached_tracks],
            **metadata,
        }

    def _bootstrap_preference_seeded_session(self, session: dict[str, Any]) -> list[SessionSearchSource]:
        cues = self._preference_seed_cues()
        artists = self._preferences.favored_artists()
        liked_tracks = self._preferences.liked_tracks()
        if not cues and not artists and not liked_tracks:
            return []

        merged_tracks: list[dict[str, Any]] = []
        seen_track_ids: set[str] = set()
        artist_counts: dict[str, int] = {}
        rejected_ids = self._session_query_pool_build_excluded_ids()

        for seed in cues:
            results = self._fetch_preference_seed_results(seed["query"])
            if not results:
                self._add_preference_seed_fallback_track(
                    merged_tracks,
                    seen_track_ids=seen_track_ids,
                    artist_counts=artist_counts,
                    preference=seed["fallback"],
                    rejected_ids=rejected_ids,
                    seed_query=seed["query"],
                )
                continue
            added = self._add_preference_seed_track_batch(
                merged_tracks,
                seen_track_ids=seen_track_ids,
                artist_counts=artist_counts,
                tracks=results,
                rejected_ids=rejected_ids,
                limit=3,
                seed_query=seed["query"],
            )
            if added == 0:
                self._add_preference_seed_fallback_track(
                    merged_tracks,
                    seen_track_ids=seen_track_ids,
                    artist_counts=artist_counts,
                    preference=seed["fallback"],
                    rejected_ids=rejected_ids,
                    seed_query=seed["query"],
                )

        for artist in artists:
            query = str(artist.get("artist_name", "")).strip()
            if not query:
                continue
            results = self._fetch_preference_seed_results(query)
            self._add_preference_seed_track_batch(
                merged_tracks,
                seen_track_ids=seen_track_ids,
                artist_counts=artist_counts,
                tracks=results,
                rejected_ids=rejected_ids,
                limit=2,
                seed_query=query,
            )

        for liked_track in liked_tracks:
            fallback_track = self._stored_preference_to_track(liked_track)
            self._add_preference_seed_track(
                merged_tracks,
                seen_track_ids=seen_track_ids,
                artist_counts=artist_counts,
                track=fallback_track,
                rejected_ids=rejected_ids,
                limit=1,
                seed_query=str(liked_track.get("session_search_query") or liked_track.get("title") or "").strip(),
            )

        if not merged_tracks:
            return []
        self._random.shuffle(merged_tracks)
        self._set_session_runtime(
            session["id"],
            query_pools={
                self._session_source_key(self._host.PREFERENCE_SEED_SOURCE): {
                    "source": {
                        "kind": self._host.PREFERENCE_SEED_SOURCE.kind,
                        "term": self._host.PREFERENCE_SEED_SOURCE.term,
                    },
                    "search_query": self._host.PREFERENCE_SEED_POOL_QUERY,
                    "cursor": 0,
                    "entries": [{"track": track, "state": "fresh"} for track in merged_tracks],
                }
            },
            active_search_sources=self._host._sources_payload([self._host.PREFERENCE_SEED_SOURCE]),
        )
        return [self._host.PREFERENCE_SEED_SOURCE]

    def _preference_seed_cues(self) -> list[dict[str, Any]]:
        cues: list[dict[str, Any]] = []
        seen: set[str] = set()
        for liked_track in self._preferences.liked_tracks():
            for candidate in (liked_track.get("session_search_query"), liked_track.get("session_request_text")):
                query = str(candidate or "").strip()
                if not query:
                    continue
                key = query.casefold()
                if key in seen:
                    continue
                seen.add(key)
                cues.append({"query": query, "fallback": liked_track})
                break
        return cues

    def _fetch_preference_seed_results(self, query: str) -> list[dict[str, Any]]:
        query_text = str(query).strip()
        if not query_text:
            return []
        search_started_at = time.perf_counter()
        results = self._host.search_catalog_tracks(query_text, limit=self._host.PREFERENCE_SEED_SEARCH_LIMIT)
        self._debug_candidate_query_search_ms += _elapsed_ms(search_started_at)
        self._debug_candidate_query_search_count += 1
        return list(results.get("tracks", []))

    def _stored_preference_to_track(self, preference: dict[str, Any]) -> dict[str, Any]:
        track_id = _clean_id(preference.get("track_id"))
        return {
            "id": track_id,
            "title": preference.get("title"),
            "artist": preference.get("artist_name"),
            "album": preference.get("album"),
            "play_params": {
                "id": track_id,
                "kind": preference.get("item_kind") or "songs",
                "is_library": bool(preference.get("is_library")),
            },
        }

    def _add_preference_seed_track_batch(
        self,
        merged_tracks: list[dict[str, Any]],
        *,
        seen_track_ids: set[str],
        artist_counts: dict[str, int],
        tracks: list[dict[str, Any]],
        rejected_ids: set[str],
        limit: int,
        seed_query: str,
    ) -> int:
        added = 0
        for track in tracks:
            if self._add_preference_seed_track(
                merged_tracks,
                seen_track_ids=seen_track_ids,
                artist_counts=artist_counts,
                track=track,
                rejected_ids=rejected_ids,
                limit=limit,
                seed_query=seed_query,
                added=added,
            ):
                added += 1
            if added >= limit:
                return added
        return added

    def _add_preference_seed_fallback_track(
        self,
        merged_tracks: list[dict[str, Any]],
        *,
        seen_track_ids: set[str],
        artist_counts: dict[str, int],
        preference: dict[str, Any],
        rejected_ids: set[str],
        seed_query: str,
    ) -> None:
        global_rejected_ids = self._preferences.globally_rejected_track_ids()
        self._add_preference_seed_track(
            merged_tracks,
            seen_track_ids=seen_track_ids,
            artist_counts=artist_counts,
            track=self._stored_preference_to_track(preference),
            rejected_ids=global_rejected_ids,
            limit=1,
            seed_query=seed_query,
        )

    def _add_preference_seed_track(
        self,
        merged_tracks: list[dict[str, Any]],
        *,
        seen_track_ids: set[str],
        artist_counts: dict[str, int],
        track: dict[str, Any],
        rejected_ids: set[str],
        limit: int,
        seed_query: str,
        added: int = 0,
    ) -> bool:
        if added >= limit:
            return False
        track_id = _clean_id(track.get("id")) or _clean_id(track.get("play_params", {}).get("id"))
        if not track_id or track_id in rejected_ids or track_id in seen_track_ids:
            return False
        artist_key = _normalize_match_text(track.get("artist"))
        if artist_key and artist_counts.get(artist_key, 0) >= self._host.PREFERENCE_SEED_ARTIST_CAP:
            return False
        normalized_track = dict(track)
        normalized_track["_seed_query"] = seed_query
        merged_tracks.append(normalized_track)
        seen_track_ids.add(track_id)
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        return True

    def _is_vague_play_request(self, value: Any) -> bool:
        text = str(value or "").strip().casefold()
        if not text:
            return False
        compact = re.sub(r"[^a-z0-9]+", " ", text)
        compact = " ".join(compact.split())
        vague_patterns = {
            "play music",
            "play some music",
            "play some songs",
            "play something",
            "play something good",
            "play anything",
            "some music",
            "music please",
        }
        return compact in vague_patterns

    def _current_preference_context_query(self, runtime: dict[str, Any]) -> str | None:
        for key in ("current_seed_query", "current_pool_query"):
            value = str(runtime.get(key, "")).strip()
            if value and value != self._host.PREFERENCE_SEED_POOL_QUERY:
                return value
        return None

    def _fetch_session_source_results(
        self,
        session: dict[str, Any],
        source: SessionSearchSource,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        search_started_at = time.perf_counter()
        self._debug_candidate_query_search_count += 1
        try:
            if source.kind == "artist":
                artists = self._host._catalog_resource_search(source.term, resource_type="artists", limit=5)
                if not artists:
                    return [], {}
                normalized_term = _normalize_match_text(source.term)
                artist = next(
                    (
                        item
                        for item in artists
                        if _normalize_match_text(item.get("attributes", {}).get("name")) == normalized_term
                    ),
                    artists[0],
                )
                artist_id = _clean_id(artist.get("id"))
                if not artist_id:
                    return [], {}
                tracks = self._host._catalog_relationship_tracks(f"/artists/{artist_id}/view/top-songs")
                return tracks, {
                    "resolved_artist_id": artist_id,
                    "resolved_name": artist.get("attributes", {}).get("name"),
                }
            if source.kind == "genre":
                genre_map = self._host._load_genre_map(self._host.SESSION_STOREFRONT)
                genre_id = genre_map.get(source.term)
                if not genre_id:
                    return [], {}
                tracks = self._host._catalog_relationship_tracks(f"/charts?types=songs&genre={_encode_query(genre_id)}")
                return tracks, {"resolved_genre_id": genre_id, "resolved_name": source.term}
            if source.kind == "vibe":
                playlists = self._host._catalog_resource_search(source.term, resource_type="playlists", limit=5)
                candidates = [self._playlist_selection_candidate(item) for item in playlists]
                self._host.append_session_debug_log(
                    stage="session_playlist_candidates",
                    payload={
                        "session_id": session.get("id"),
                        "search_source": {"kind": source.kind, "term": source.term},
                        "candidates": candidates,
                    },
                )
                if not candidates:
                    return [], {}
                chooser = getattr(self._host._resolver, "select_session_playlist", None)
                selection = (
                    chooser(self._session_effective_request(session), self._host, session, source, candidates)
                    if callable(chooser)
                    else SessionTrackSelection(selected_index=0, resolver="fallback")
                )
                if selection.selected_index < 0:
                    return [], {}
                selected_index = min(selection.selected_index, len(playlists) - 1)
                playlist = playlists[selected_index]
                playlist_id = _clean_id(playlist.get("id"))
                if not playlist_id:
                    return [], {}
                tracks = self._host._catalog_relationship_tracks(f"/playlists/{playlist_id}/tracks")
                metadata = {
                    "resolved_playlist_id": playlist_id,
                    "resolved_name": playlist.get("attributes", {}).get("name"),
                }
                self._host.append_session_debug_log(
                    stage="session_playlist_selected",
                    payload={
                        "session_id": session.get("id"),
                        "search_source": {"kind": source.kind, "term": source.term},
                        "selected_index": selected_index,
                        "playlist": candidates[selected_index],
                    },
                )
                return tracks, metadata
            if source.kind == "preference":
                return [], {}
            if source.kind == "legacy":
                tracks: list[dict[str, Any]] = []
                offset = 0
                while len(tracks) < self._host.SESSION_SEARCH_RESULT_LIMIT:
                    limit = min(self._host.SESSION_SEARCH_PAGE_LIMIT, self._host.SESSION_SEARCH_RESULT_LIMIT - len(tracks))
                    results = self._host.search_catalog_tracks(source.term, limit=limit, offset=offset)
                    page_tracks = list(results.get("tracks", []))
                    if not page_tracks:
                        break
                    tracks.extend(page_tracks)
                    if len(page_tracks) < limit:
                        break
                    offset += len(page_tracks)
                return tracks[: self._host.SESSION_SEARCH_RESULT_LIMIT], {"resolved_name": source.term}
            return [], {}
        finally:
            self._debug_candidate_query_search_ms += _elapsed_ms(search_started_at)

    def _playlist_selection_candidate(self, item: dict[str, Any]) -> dict[str, Any]:
        playlist = _flatten_playlist_item(item)
        description = str(playlist.get("description") or "").strip()
        return {
            "name": playlist.get("name"),
            "curator": playlist.get("curator"),
            "playlist_type": playlist.get("playlist_type") or playlist.get("type"),
            "description": description[:280],
        }

    def _replace_session_query_pools(
        self,
        session: dict[str, Any],
        search_sources: list[SessionSearchSource],
    ) -> None:
        pools: dict[str, dict[str, Any]] = {}
        for source in self._normalize_search_sources(search_sources):
            pools[self._session_source_key(source)] = self._build_session_query_pool(session, source)
        self._set_session_runtime(
            session["id"],
            query_pools=pools,
            active_search_sources=self._host._sources_payload(self._normalize_search_sources(search_sources)),
        )

    def _ensure_session_query_pools(
        self,
        session: dict[str, Any],
        search_sources: list[SessionSearchSource],
    ) -> None:
        runtime = self._get_session_runtime(session["id"])
        pools = self._normalize_session_query_pools(runtime.get("query_pools"))
        updated = False
        for source in self._normalize_search_sources(search_sources):
            key = self._session_source_key(source)
            if key in pools:
                continue
            pools[key] = self._build_session_query_pool(session, source)
            updated = True
        if updated:
            self._set_session_runtime(session["id"], query_pools=pools)
            self._host.append_session_debug_log(
                stage="session_query_pools_initialized",
                payload={
                    "session_id": session.get("id"),
                    "active_search_sources": self._host._sources_payload(
                        self._normalize_search_sources(runtime.get("active_search_sources"))
                    ),
                    "pool_sources": [pool.get("source") for pool in pools.values()],
                    "pool_count": len(pools),
                },
            )

    def _current_session_track_id(self, session: dict[str, Any]) -> str:
        playback = self.session_planning_playback_snapshot(session)
        current = playback.get("track", {})
        return _clean_id(current.get("track_id"))

    def _next_session_candidate_window(
        self,
        session: dict[str, Any],
        source: SessionSearchSource,
    ) -> list[dict[str, Any]]:
        # Session selection is intentionally cursor-based and sequential:
        # build one ordered pool per search query, offer the next window of
        # fresh tracks starting at the pool cursor, and rely on entry state
        # (`played`, `screened_out`, `rejected`) to prevent replaying the same
        # candidates until the pool has been exhausted and explicitly reset.
        if isinstance(source, str):
            source = SessionSearchSource(kind="legacy", term=source)
        runtime = self._get_session_runtime(session["id"])
        pools = self._normalize_session_query_pools(runtime.get("query_pools"))
        source_key = self._session_source_key(source)
        if source_key not in pools and source.term in pools:
            source_key = source.term
        else:
            self._ensure_session_query_pools(session, [source])
            runtime = self._get_session_runtime(session["id"])
            pools = self._normalize_session_query_pools(runtime.get("query_pools"))
        pool = pools.get(source_key)
        if not pool:
            return []
        current_track_id = self._current_session_track_id(session)
        for _ in range(3):
            window = self._gather_session_query_pool_window(pool, current_track_id=current_track_id)
            self._host.append_session_debug_log(
                stage="session_candidate_window",
                payload={
                    "session_id": session.get("id"),
                    "search_source": {"kind": source.kind, "term": source.term},
                    "cursor": pool.get("cursor", 0),
                    "pool_track_count": len(pool.get("entries", [])),
                    "window_track_count": len(window),
                    "window_tracks": [
                        {
                            "index": entry["index"],
                            "id": entry["track"].get("id"),
                            "title": entry["track"].get("title"),
                            "artist": entry["track"].get("artist"),
                            "album": entry["track"].get("album"),
                        }
                        for entry in window
                    ],
                },
            )
            if window:
                return window
            # Once a pool has no fresh candidates left, screened-out tracks are
            # reconsidered before played tracks. Rejected tracks are never reset
            # here; they stay unavailable until the pool is rebuilt.
            if self._reset_session_query_pool_state(pool, from_state="screened_out"):
                pools[source_key] = pool
                self._set_session_runtime(session["id"], query_pools=pools)
                continue
            if self._reset_session_query_pool_state(pool, from_state="played"):
                pools[source_key] = pool
                self._set_session_runtime(session["id"], query_pools=pools)
                continue
            return []
        return []

    def _gather_session_query_pool_window(
        self,
        pool: dict[str, Any],
        *,
        current_track_id: str,
    ) -> list[dict[str, Any]]:
        # This is deliberately simple: starting at the cursor, walk forward
        # through the ordered pool and collect the first N fresh entries.
        # There is no diversification, reranking, or random sampling here.
        entries = list(pool.get("entries", []))
        if not entries:
            return []
        cursor = int(pool.get("cursor", 0)) % len(entries)
        window: list[dict[str, Any]] = []
        for offset in range(len(entries)):
            index = (cursor + offset) % len(entries)
            entry = entries[index]
            if entry.get("state") != "fresh":
                continue
            track = entry.get("track", {})
            track_id = _clean_id(track.get("id")) or _clean_id(track.get("play_params", {}).get("id"))
            if current_track_id and track_id == current_track_id:
                continue
            window.append({"index": index, "track": track})
            if len(window) >= self._host.SESSION_SELECTION_WINDOW_SIZE:
                break
        return window

    def _reset_session_query_pool_state(self, pool: dict[str, Any], *, from_state: str) -> bool:
        changed = False
        for entry in pool.get("entries", []):
            if entry.get("state") == from_state:
                entry["state"] = "fresh"
                changed = True
        return changed

    def _update_session_query_pool_after_window(
        self,
        session_id: int,
        search_query: str,
        window: list[dict[str, Any]],
        *,
        selected_entry_index: int | None,
        mark_state: str | None,
    ) -> None:
        runtime = self._get_session_runtime(session_id)
        pools = self._normalize_session_query_pools(runtime.get("query_pools"))
        pool = pools.get(search_query)
        if not pool or not pool.get("entries"):
            return
        entries = pool["entries"]
        # Window state is persistent across advances:
        # - if the resolver rejects the whole window, every shown entry becomes
        #   `screened_out`
        # - if one entry is chosen, only that entry becomes `played`
        # In both cases the cursor advances to just after the last shown entry,
        # so the next call offers the next sequential slice of the pool.
        if mark_state is not None:
            for entry in window:
                entries[entry["index"]]["state"] = mark_state
        if selected_entry_index is not None and 0 <= selected_entry_index < len(entries):
            entries[selected_entry_index]["state"] = "played"
            selected_track = entries[selected_entry_index]["track"]
            source_term = str(pool.get("source", {}).get("term") or pool.get("search_query") or search_query)
            self._set_session_runtime(
                session_id,
                current_pool_query=search_query,
                current_seed_query=str(selected_track.get("_seed_query", "")).strip() or source_term,
                current_track_id=_clean_id(selected_track.get("id")) or _clean_id(selected_track.get("play_params", {}).get("id")),
            )
        last_index = window[-1]["index"]
        pool["cursor"] = (last_index + 1) % len(entries)
        pools[search_query] = pool
        self._set_session_runtime(session_id, query_pools=pools)
        self._host.append_session_debug_log(
            stage="session_candidate_window_updated",
            payload={
                "session_id": session_id,
                "search_query": search_query,
                "applied_state": mark_state,
                "selected_entry_index": selected_entry_index,
                "new_cursor": pool["cursor"],
                "window_tracks": [
                    {
                        "index": entry["index"],
                        "id": entry["track"].get("id"),
                        "title": entry["track"].get("title"),
                        "artist": entry["track"].get("artist"),
                    }
                    for entry in window
                ],
            },
        )

    def _mark_session_selection_window_screened_out(
        self,
        session_id: int,
        search_query: str,
        window: list[dict[str, Any]],
    ) -> None:
        self._update_session_query_pool_after_window(
            session_id,
            search_query,
            window,
            selected_entry_index=None,
            mark_state="screened_out",
        )

    def _mark_session_track_played(
        self,
        session_id: int,
        search_query: str,
        window: list[dict[str, Any]],
        selected_entry_index: int,
    ) -> None:
        self._update_session_query_pool_after_window(
            session_id,
            search_query,
            window,
            selected_entry_index=selected_entry_index,
            mark_state=None,
        )

    def _mark_session_track_rejected(self, session_id: int, track_id: str) -> None:
        runtime = self._get_session_runtime(session_id)
        pools = self._normalize_session_query_pools(runtime.get("query_pools"))
        preferred_query = str(runtime.get("current_pool_query", "")).strip()
        ordered_queries = [preferred_query] if preferred_query else []
        ordered_queries.extend(query for query in pools if query != preferred_query)
        for query in ordered_queries:
            pool = pools.get(query)
            if not pool:
                continue
            for entry in pool.get("entries", []):
                entry_track = entry.get("track", {})
                entry_track_id = _clean_id(entry_track.get("id")) or _clean_id(entry_track.get("play_params", {}).get("id"))
                if entry_track_id != track_id:
                    continue
                entry["state"] = "rejected"
                pools[query] = pool
                self._set_session_runtime(session_id, query_pools=pools, current_track_id=track_id)
                return

    def _record_current_track_for_session(
        self,
        session: dict[str, Any],
        *,
        playback: dict[str, Any] | None = None,
        event_type: str = "track_started",
    ) -> None:
        current = (playback or self._host.playback_snapshot()).get("track", {})
        current_id = _clean_id(current.get("track_id"))
        if not current_id:
            return
        recent = self._preferences.list_session_tracks(session["id"], limit=1)
        if recent and _clean_id(recent[0].get("track_id")) == current_id:
            return
        self._preferences.add_session_track(
            session["id"],
            {
                "id": current_id,
                "title": current.get("title"),
                "artist": current.get("artist"),
                "album": current.get("album"),
                "href": None,
            },
        )
        self._preferences.add_session_event(
            session["id"],
            event_type=event_type,
            track={
                "track_id": current_id,
                "title": current.get("title"),
                "artist": current.get("artist"),
                "album": current.get("album"),
                "href": None,
            },
        )

    def _should_advance_session(self, session: dict[str, Any], playback: dict[str, Any]) -> bool:
        # This coordinates long-lived processes through persisted runtime, but
        # intentionally does not provide an atomic cross-process worker lease.
        runtime = self._effective_session_runtime(session["id"])
        if runtime.get("suspended"):
            return False
        if runtime.get("advance_in_progress"):
            return False
        if playback.get("is_playing"):
            return False
        elapsed = self._seconds_since_runtime_timestamp(runtime.get("last_advance_at"))
        if elapsed is not None and elapsed < self._host.SESSION_ADVANCE_COOLDOWN_SECONDS:
            return False
        return True

    def _effective_session_runtime(self, session_id: int) -> dict[str, Any]:
        runtime = self._get_session_runtime(session_id)
        # Session control can be split across long-lived processes. Persisted
        # control state must override stale in-memory values from this process.
        stored = self._preferences.get_session_runtime(session_id) or {}
        if stored:
            runtime["suspended"] = stored.get("active_intent") == "suspended"
        if stored.get("last_advance_at") is not None:
            runtime["last_advance_at"] = stored.get("last_advance_at")
        if "last_selected_track_id" not in runtime and stored.get("last_selected_track_id") is not None:
            runtime["last_selected_track_id"] = stored.get("last_selected_track_id")
        if "last_known_playback_state" not in runtime and stored.get("last_known_playback_state") is not None:
            runtime["last_known_playback_state"] = stored.get("last_known_playback_state")
        return runtime

    def _get_session_runtime(self, session_id: int) -> dict[str, Any]:
        with self._session_runtime_lock:
            return dict(self._session_runtime.get(session_id, {}))

    def _set_session_runtime(self, session_id: int, **updates: Any) -> None:
        with self._session_runtime_lock:
            runtime = dict(self._session_runtime.get(session_id, {}))
            runtime.update(updates)
            self._session_runtime[session_id] = runtime
        if updates:
            interesting = {
                key: value
                for key, value in updates.items()
                if key
                in {
                    "active_search_sources",
                    "rejected_search_sources",
                    "query_pools",
                    "current_pool_query",
                    "current_track_id",
                    "suspended",
                }
            }
            if "query_pools" in interesting and isinstance(interesting["query_pools"], dict):
                interesting["query_pools"] = {
                    str(source_key): {
                        "source": pool.get("source"),
                        "cursor": pool.get("cursor", 0),
                        "entry_count": len(pool.get("entries", [])) if isinstance(pool, dict) else 0,
                    }
                    for source_key, pool in interesting["query_pools"].items()
                    if isinstance(pool, dict)
                }
            if interesting:
                self._host.append_session_debug_log(
                    stage="session_runtime_updated",
                    payload={"session_id": session_id, "updates": interesting},
                )

    def _clear_session_runtime(self, session_id: int) -> None:
        with self._session_runtime_lock:
            self._session_runtime.pop(session_id, None)
        self._preferences.clear_session_runtime(session_id)

    def _clear_all_session_runtime(self) -> None:
        with self._session_runtime_lock:
            self._session_runtime.clear()
        self._host.append_session_debug_log(stage="session_runtime_cleared", payload={"scope": "all"})

    def _abort_failed_session_start(self, session_id: int) -> None:
        active = self._preferences.get_active_session()
        if active is not None and int(active["id"]) == int(session_id):
            self._preferences.stop_active_session()
        self._clear_session_runtime(session_id)

    def _persist_session_runtime(
        self,
        session_id: int,
        *,
        suspended: bool | None = None,
        last_advance_at: str | None = None,
        last_selected_track_id: str | None = None,
        last_known_playback_state: str | None = None,
        preserve_last_advance: bool = False,
    ) -> None:
        runtime = self._preferences.get_session_runtime(session_id)
        resolved_last_advance_at = runtime.get("last_advance_at") if runtime and preserve_last_advance else last_advance_at
        self._preferences.upsert_session_runtime(
            session_id,
            active_intent="suspended" if suspended else "active",
            last_advance_at=resolved_last_advance_at,
            last_selected_track_id=last_selected_track_id,
            last_known_playback_state=last_known_playback_state,
        )

    def _seconds_since_runtime_timestamp(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return time.monotonic() - float(value)
        if isinstance(value, str):
            try:
                then = datetime.fromisoformat(value)
            except ValueError:
                return None
            return (datetime.now().astimezone() - then.astimezone()).total_seconds()
        return None

    def _record_session_selection_event(self, session_id: int, track: dict[str, Any], *, selection_strategy: str) -> None:
        event_type = "track_selected"
        if selection_strategy == "adaptive-session-auto-advance":
            event_type = "track_auto_advanced"
        elif selection_strategy == "adaptive-session-skip":
            event_type = "track_manual_skip"
        elif selection_strategy == "adaptive-session-steer":
            event_type = "track_manually_steered"
        self._preferences.add_session_event(
            session_id,
            event_type=event_type,
            track={
                "track_id": _clean_id(track.get("play_params", {}).get("id")) or _clean_id(track.get("id")),
                "title": track.get("title"),
                "artist": track.get("artist"),
                "album": track.get("album"),
                "href": track.get("href"),
            },
            metadata={"selection_strategy": selection_strategy},
        )

