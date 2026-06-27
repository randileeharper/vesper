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
import random
import threading
import time
from typing import Any, Protocol

from .errors import CiderAgentError, CiderValidationError
from .output import compact_output
from .resolver import Resolver, SessionQueryPlan, SessionSearchSource
from .session_queue import SessionQueueMixin
from .session_runtime import SessionRuntimeMixin
from .session_sources import SessionSourcesMixin
from .utils import _clean_id, _elapsed_ms

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

    def session_vibe_rephrase_attempts(self) -> int:
        ...

    def global_recent_tracks_limit(self) -> int:
        ...

    def _fetch_session_source_results(
        self,
        session: dict[str, Any],
        source: SessionSearchSource,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ...

    # Class-level constants used by the session worker and search planning.
    SESSION_REFILL_INTERVAL_SECONDS: float
    SESSION_SEARCH_RESULT_LIMIT: int
    SESSION_SEARCH_PAGE_LIMIT: int
    SESSION_STOREFRONT: str
    SESSION_ADVANCE_COOLDOWN_SECONDS: float
    PREFERENCE_SEED_SEARCH_LIMIT: int
    PREFERENCE_SEED_ARTIST_CAP: int
    PREFERENCE_SEED_POOL_QUERY: str
    PREFERENCE_SEED_SOURCE: SessionSearchSource

    # The configured resolver, used for adaptive session planning.
    _resolver: Resolver


class SessionEngine(SessionRuntimeMixin, SessionSourcesMixin, SessionQueueMixin):
    """Owns adaptive-session runtime state, the refill worker, and the
    search/planning/pool machinery. Constructed by and delegating cross-cutting
    work to a :class:`SessionHost`.

    Behavior is split across cohesive mixins for testability (issue #34):

    - :class:`SessionRuntimeMixin` — runtime get/set/clear/persist, auto-advance
      decisions, two-snapshot stop confirmation, and selection events.
    - :class:`SessionSourcesMixin` — query pools, preference seeding, and
      vibe/playlist search resolution.
    - :class:`SessionQueueMixin` — search planning and queue materialization.

    The worker loop and playback orchestration remain on this class. Mixins are
    combined via cooperative inheritance, so ``self`` is the engine and the
    mixins read ``self._host``/``self._preferences``/``self._session_runtime``
    exactly as before extraction.
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
        self._session_queue_batch_size = 20
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
            self._restore_current_queue_item_runtime(session["id"], playback=playback)
            self._record_current_track_for_session(session, playback=playback, event_type="track_started")
        else:
            self._preferences.reset_stale_session_queue_items(session["id"])

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
            payload: dict[str, Any] = {"status": "ok", "session": None}
            return compact_output(payload, self._host.include_timing_debug()) if compact is not False and self._host.response_detail() == "compact" else payload
        payload = {"status": "ok", "session": session}
        if include_recent_tracks:
            payload["recent_tracks"] = self.recent_session_tracks(limit=self._host.session_recent_tracks_limit())
        if compact is False:
            return payload
        if compact is True or self._host.response_detail() == "compact":
            return compact_output(payload, self._host.include_timing_debug())
        return payload

    def session_queue(self, *, limit: int = 50, include_history: bool = False) -> dict[str, Any]:
        session = self._preferences.get_active_session()
        if session is None:
            return {"status": "ok", "session": None, "count": 0, "items": []}
        items = self._preferences.list_session_queue(
            session["id"],
            limit=limit,
            include_history=include_history,
        )
        return {
            "status": "ok",
            "session": session,
            "count": len(items),
            "items": items,
        }

    def session_candidates(self, *, window: int = 10) -> dict[str, Any]:
        """Inspect the active session's in-memory candidate pools.

        This is a read-only developer/debug view of the adaptive-session
        candidate pools.  It is distinct from Cider's native playback queue
        and from the persisted session queue shown by ``session_queue``.

        Candidate pools are process-local runtime state.  After a process
        restart the pools are empty even if a session is persisted.
        """
        session = self._preferences.get_active_session()
        if session is None:
            return {"status": "ok", "session": None, "pools": []}
        session_id = int(session["id"])
        runtime = self._get_session_runtime(session_id)
        active_sources = self._normalize_search_sources(runtime.get("active_search_sources"))
        pools = self._normalize_session_query_pools(runtime.get("query_pools"))
        pool_payloads: list[dict[str, Any]] = []
        for source_key, pool in pools.items():
            entries = pool.get("entries", [])
            state_counts: dict[str, int] = {"fresh": 0, "played": 0, "screened_out": 0, "rejected": 0}
            fresh_entries: list[dict[str, Any]] = []
            for index, entry in enumerate(entries):
                state = entry.get("state", "fresh")
                if state in state_counts:
                    state_counts[state] += 1
                else:
                    state_counts[state] = 1
                if state == "fresh" and len(fresh_entries) < window:
                    track = entry.get("track") or {}
                    fresh_entries.append({
                        "index": index,
                        "title": track.get("title"),
                        "artist": track.get("artist"),
                        "album": track.get("album"),
                        "id": _clean_id(track.get("id")) or _clean_id(track.get("play_params", {}).get("id")),
                        "source": pool.get("source"),
                    })
            cursor = pool.get("cursor", 0)
            if not isinstance(cursor, int):
                cursor = 0
            pool_payloads.append({
                "source_key": source_key,
                "source": pool.get("source"),
                "search_query": pool.get("search_query"),
                "cursor": cursor,
                "total_entries": len(entries),
                "state_counts": state_counts,
                "next_window": fresh_entries,
            })
        return {
            "status": "ok",
            "session": {
                "id": session["id"],
                "request_text": session.get("request_text"),
                "mode": session.get("mode"),
            },
            "active_search_sources": self._host._sources_payload(active_sources),
            "pools": pool_payloads,
        }

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
            # Broad catch is intentional: session startup touches RPC, resolver,
            # and preference-store calls, and any failure during startup must
            # run the abort/cleanup below before re-raising, so we never leave a
            # half-started session wedged. Domain errors (CiderAgentError and
            # subclasses) are expected operational failures and are recorded
            # without a noisy traceback; unexpected errors get a full
            # ``LOGGER.exception`` so they remain actionable.
            if not isinstance(exc, CiderAgentError):
                LOGGER.exception("Unexpected error starting adaptive session.")
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
            self._materialize_session_queue(session, next_sources, queue_policy="source_order", preserve_history=True)
        elif resolved_search_update["mode"] == "add":
            current_keys = {self._session_source_key(source) for source in current_sources}
            added_sources = [source for source in next_sources if self._session_source_key(source) not in current_keys]
            if added_sources:
                self._ensure_session_query_pools(session, added_sources)
                self._append_session_sources_to_queue(session, added_sources, queue_policy="source_order")
        else:
            self._filter_remaining_session_queue(session)
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
                started_debug_episode = False
                try:
                    self._check_worker_cancelled()
                    session = self._preferences.get_active_session()
                    if session is None:
                        continue
                    playback = self._host.playback_snapshot()
                    if playback.get("is_playing") is False:
                        started_debug_episode = self._host._begin_resolver_debug_episode(
                            "adaptive-session-auto-advance-check"
                        )
                    self._record_current_track_for_session(session, playback=playback)
                    if self._should_advance_session(session, playback):
                        self._host.append_session_debug_log(
                            stage="session_auto_advance_triggered",
                            payload={"session_id": session["id"]},
                        )
                        if started_debug_episode:
                            self._play_session_track(session, selection_strategy="adaptive-session-auto-advance")
                        else:
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
                    # Broad catch is intentional per the issue's direction #3:
                    # the background refill loop must stay alive across
                    # unexpected failures rather than tearing down the worker.
                    # The full traceback is logged at EXCEPTION level (via
                    # ``LOGGER.exception``) and the error is recorded, so the
                    # failure stays actionable without disguising its type.
                    self._host._record_error("worker", "adaptive-session-refill", exc)
                    LOGGER.exception("Adaptive session worker failed during refill loop.")
                finally:
                    self._host._end_resolver_debug_episode(started_debug_episode)
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
                pending_stop_track_id=None,
                pending_stop_observed_at=None,
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
                self._ensure_materialized_session_queue(session, plan)
                if self._host.include_timing_debug():
                    self._copy_queue_materialization_timings(timings)
                self._check_worker_cancelled()
                tracks, search_source, selection, queue_item = self._claim_session_queue_track(session)
                if not tracks:
                    self._ensure_materialized_session_queue(session, plan)
                    if self._host.include_timing_debug():
                        self._copy_queue_materialization_timings(timings)
                    self._check_worker_cancelled()
                    tracks, search_source, selection, queue_item = self._claim_session_queue_track(session)
                if not tracks and getattr(plan, "resolver", "") != "preference-seeded":
                    self._check_worker_cancelled()
                    self._reject_session_plan_sources(session["id"], plan)
                    replan_started_at = time.perf_counter()
                    plan = self._plan_session_query(session, count=1, force_replan=True)
                    timings["replan_session_ms"] = _elapsed_ms(replan_started_at)
                    collect_retry_started_at = time.perf_counter()
                    self._ensure_materialized_session_queue(session, plan)
                    if self._host.include_timing_debug():
                        self._copy_queue_materialization_timings(timings)
                    self._check_worker_cancelled()
                    tracks, search_source, selection, queue_item = self._claim_session_queue_track(session)
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
                    current_queue_item_id=queue_item.get("id") if isinstance(queue_item, dict) else None,
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
                # Broad catch is intentional: this is a cleanup-then-reraise
                # guard, not a recovery point. Any failure (domain or
                # unexpected) leaving ``_play_session_track`` must clear
                # ``advance_in_progress`` so the session runtime is never
                # wedged, then re-raise so the original error propagates
                # unchanged. See issue #47.
                self._set_session_runtime(session["id"], advance_in_progress=False, planning_playback_snapshot=None)
                raise

    def _plan_session_query(self, session: dict[str, Any], *, count: int, force_replan: bool = False) -> SessionQueryPlan:
        runtime = self._get_session_runtime(session["id"])
        active_sources = self._normalize_search_sources(runtime.get("active_search_sources"))
        if active_sources and not force_replan:
            # Preserve the full multi-source mix on refill / empty-queue rebuild
            # instead of collapsing to the first active source. ``count`` only
            # constrains fresh resolver plans, not already-planned runtime state.
            return SessionQueryPlan(search_sources=active_sources, resolver="session-runtime")
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
            queue_policy=self._normalize_queue_policy(getattr(plan, "queue_policy", "source_order")),
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

