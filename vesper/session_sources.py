"""Query pools, preference seeding, and vibe/playlist search resolution for
:class:`vesper.session.SessionEngine`.

Extracted behavior-preservingly from ``SessionEngine`` (issue #34). Covers
normalizing search sources/queries, planning search sources, building and
maintaining query pools, bootstrapping preference-seeded sessions, fetching
session source results (artist/genre/vibe/legacy/preference), vibe rephrasing,
and playlist selection.

Combined into ``SessionEngine`` via cooperative inheritance; ``self`` is the
engine instance and reads ``self._host``, ``self._preferences``,
``self._session_runtime``/``self._session_runtime_lock``, and the
``self._debug_candidate_*`` counters exactly as before. Cross-mixin calls
(e.g. ``self._get_session_runtime``/``self._set_session_runtime``) resolve
against the combined class.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from .catalog import flatten_playlist_item as _flatten_playlist_item
from .matching import normalize_match_text as _normalize_match_text
from .resolver import SessionSearchSource, SessionTrackSelection
from .utils import _clean_id, _elapsed_ms, _encode_query

if TYPE_CHECKING:
    import random

    from .session import SessionHost
    from .storage import PreferenceStore


class SessionSourcesMixin:
    """Normalize sources/queries, plan search sources, build and maintain query
    pools, bootstrap preference-seeded sessions, and resolve session search
    source results (artist/genre/vibe/legacy/preference).

    Expects the combined ``SessionEngine`` to provide ``self._host``,
    ``self._preferences``, ``self._session_runtime``/``self._session_runtime_lock``,
    ``self._random``, and the ``self._debug_candidate_*`` counters.
    """

    if TYPE_CHECKING:
        _host: SessionHost
        _preferences: PreferenceStore
        _random: random.Random
        _debug_candidate_query_search_count: int
        _debug_candidate_query_search_ms: float

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
                kind: Any = item.kind
                term: Any = item.term
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

    def _reject_session_plan_sources(self, session_id: int, plan: Any) -> None:
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
                tracks: list[dict[str, Any]] = self._host._catalog_relationship_tracks(f"/artists/{artist_id}/view/top-songs")
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
                return self._fetch_vibe_session_source_results(session, source)
            if source.kind == "preference":
                return [], {}
            if source.kind == "legacy":
                tracks = []
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

    def _fetch_vibe_session_source_results(
        self,
        session: dict[str, Any],
        source: SessionSearchSource,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        attempted_terms: list[str] = []
        max_attempts = max(1, self._host.session_vibe_rephrase_attempts())
        for attempt in range(1, max_attempts + 1):
            term = source.term if attempt == 1 else self._rephrase_session_vibe(session, source, attempted_terms)
            if not term:
                break
            attempted_terms.append(term)
            attempt_source = SessionSearchSource(kind="vibe", term=term)
            if attempt > 1:
                self._debug_candidate_query_search_count += 1
            playlists = self._host._catalog_resource_search(term, resource_type="playlists", limit=5)
            candidates = [self._playlist_selection_candidate(item) for item in playlists]
            self._host.append_session_debug_log(
                stage="session_playlist_candidates",
                payload={
                    "session_id": session.get("id"),
                    "search_source": {"kind": attempt_source.kind, "term": attempt_source.term},
                    "original_search_source": {"kind": source.kind, "term": source.term},
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "candidates": candidates,
                },
            )
            if not candidates:
                self._host.append_session_debug_log(
                    stage="session_vibe_rephrase_attempt",
                    payload={
                        "session_id": session.get("id"),
                        "original_term": source.term,
                        "attempted_term": term,
                        "attempt": attempt,
                        "result": "empty_playlist_search",
                    },
                )
                continue
            chooser = getattr(self._host._resolver, "select_session_playlist", None)
            selection = (
                chooser(self._session_effective_request(session), self._host, session, attempt_source, candidates)
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
            if attempt_source.term != source.term:
                metadata["resolved_vibe_term"] = attempt_source.term
            self._host.append_session_debug_log(
                stage="session_playlist_selected",
                payload={
                    "session_id": session.get("id"),
                    "search_source": {"kind": attempt_source.kind, "term": attempt_source.term},
                    "original_search_source": {"kind": source.kind, "term": source.term},
                    "selected_index": selected_index,
                    "playlist": candidates[selected_index],
                },
            )
            return tracks, metadata
        return [], {}

    def _rephrase_session_vibe(
        self,
        session: dict[str, Any],
        source: SessionSearchSource,
        attempted_terms: list[str],
    ) -> str | None:
        rephraser = getattr(self._host._resolver, "rephrase_session_vibe", None)
        if callable(rephraser):
            candidate = str(
                rephraser(self._session_effective_request(session), self._host, session, source, list(attempted_terms))
                or ""
            ).strip()
            if candidate and candidate.casefold() not in {term.casefold() for term in attempted_terms}:
                return candidate
        return self._fallback_vibe_rephrase(source.term, attempted_terms)

    def _fallback_vibe_rephrase(self, term: str, attempted_terms: list[str]) -> str | None:
        seen = {item.casefold() for item in attempted_terms}
        words = [word for word in str(term).replace("-", " ").split() if word]
        candidates: list[str] = []
        if len(words) > 2:
            candidates.append(" ".join(words[:2]))
        if len(words) > 1:
            candidates.append(words[-1])
        candidates.extend(["chill", "upbeat", "focus", "workout", "party"])
        for candidate in candidates:
            normalized = candidate.strip()
            if normalized and normalized.casefold() not in seen:
                return normalized
        return None

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
