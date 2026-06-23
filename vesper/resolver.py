"""Text-to-action resolution for Vesper."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .action_registry import list_resolver_action_definitions
from .config import Settings
from .errors import ResolverError


@dataclass
class ResolvedAction:
    """Structured action chosen by a resolver."""

    action: str
    parameters: dict[str, Any]
    resolver: str
    raw: dict[str, Any] | None = None
    reasoning: str | None = None
    raw_content: str | None = None


@dataclass
class SessionSearchSource:
    """Typed catalog source for an adaptive play session."""

    kind: str
    term: str


@dataclass
class SessionQueryPlan:
    """Search sources for the next step of an adaptive play session."""

    search_sources: list[SessionSearchSource]
    resolver: str
    raw: dict[str, Any] | None = None
    reasoning: str | None = None
    raw_content: str | None = None

    @property
    def candidate_queries(self) -> list[str]:
        return [source.term for source in self.search_sources]

    @property
    def search_queries(self) -> list[str]:
        """Compatibility view for older callers and debug consumers."""
        return self.candidate_queries

    @property
    def candidate_tracks(self) -> list[dict[str, str]]:
        return []

    @property
    def candidate_artists(self) -> list[str]:
        return []


@dataclass
class SessionTrackSelection:
    """Resolved selection from real catalog search results."""

    selected_index: int
    resolver: str
    raw: dict[str, Any] | None = None
    reasoning: str | None = None
    raw_content: str | None = None


class Resolver(Protocol):
    """Resolve freeform user text into a structured action."""

    def resolve(self, text: str, service: Any) -> ResolvedAction:
        """Resolve text into an action."""

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int) -> SessionQueryPlan:
        """Generate search queries for the next adaptive-session track."""

    def select_session_track(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_query: str,
        candidates: list[dict[str, Any]],
    ) -> SessionTrackSelection:
        """Choose one track from real catalog candidates."""

    def select_session_playlist(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_source: SessionSearchSource,
        candidates: list[dict[str, Any]],
    ) -> SessionTrackSelection:
        """Choose one playlist from real catalog candidates."""

    def rephrase_session_vibe(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_source: SessionSearchSource,
        attempted_terms: list[str],
    ) -> str | None:
        """Return an alternate vibe playlist search term after empty results."""


class FallbackResolver:
    """Minimal deterministic fallback for obvious direct commands."""

    SIMPLE_ACTIONS = {
        "play": "play",
        "pause": "pause",
        "stop": "stop",
        "next": "next_track",
        "previous": "previous_track",
        "status": "status",
    }

    def resolve(self, text: str, service: Any) -> ResolvedAction:
        normalized = text.strip().lower()
        action = self.SIMPLE_ACTIONS.get(normalized)
        if action is None:
            raise ResolverError(
                "Text resolution is not configured for general language requests. "
                "Use a structured action or enable the openai_compatible resolver."
            )
        return ResolvedAction(action=action, parameters={}, resolver="fallback")

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int) -> SessionQueryPlan:
        query = request.strip()
        return SessionQueryPlan(
            search_sources=[SessionSearchSource(kind="vibe", term=query)] if query else [],
            resolver="fallback",
        )

    def select_session_track(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_query: str,
        candidates: list[dict[str, Any]],
    ) -> SessionTrackSelection:
        return SessionTrackSelection(selected_index=0, resolver="fallback")

    def select_session_playlist(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_source: SessionSearchSource,
        candidates: list[dict[str, Any]],
    ) -> SessionTrackSelection:
        return SessionTrackSelection(selected_index=0, resolver="fallback")

    def rephrase_session_vibe(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_source: SessionSearchSource,
        attempted_terms: list[str],
    ) -> str | None:
        return None


class OpenAICompatibleResolver:
    """Resolve text requests using an OpenAI-compatible chat completions endpoint."""

    MAX_COMPLETION_ATTEMPTS = 5
    MAX_SESSION_SEARCH_QUERIES = 1
    MAX_SESSION_SELECTION_CANDIDATES = 6
    ACTION_ALIASES = {
        "now_playing": "get_now_playing",
        "current_track": "get_now_playing",
        "what_is_playing": "get_now_playing",
        "nowplaying": "get_now_playing",
        "queue_status": "get_queue",
        "show_queue": "get_queue",
        "next": "next_track",
        "skip": "next_track",
        "previous": "previous_track",
        "prev": "previous_track",
        "back": "previous_track",
        "resume": "play",
        "resume_playback": "play",
        "volume": "set_volume",
        "play_search": "play_search_result",
        "play_result": "play_search_result",
        "play_candidate": "play_candidate_match",
        "candidate_match": "play_candidate_match",
        "show_preferences": "list_preferences",
        "get_preferences": "list_preferences",
        "list_playlists": "list_library_playlists",
        "show_playlists": "list_library_playlists",
        "play_playlist": "play_library_playlist",
        "delete_preference": "forget_preference",
        "remove_preference": "forget_preference",
        "like_track": "like_current_track",
        "like_current_song": "like_current_track",
        "skip_current_track": "reject_current_track",
        "reject_track": "reject_current_track",
        "update_session": "steer_session",
        "adjust_session": "steer_session",
        "start_session": "play_session",
    }

    def __init__(self, settings: Settings, session: httpx.Client | None = None) -> None:
        self._settings = settings
        self._session = session or httpx.Client(
            base_url=settings.resolver_base_url,
            timeout=settings.request_timeout_seconds,
            verify=settings.verify_tls,
        )
        self._fallback = FallbackResolver()

    def close(self) -> None:
        self._session.close()

    def resolve(self, text: str, service: Any) -> ResolvedAction:
        try:
            return self._fallback.resolve(text, service)
        except ResolverError:
            pass

        headers = {"Content-Type": "application/json"}
        if self._settings.resolver_api_key:
            headers["Authorization"] = f"Bearer {self._settings.resolver_api_key}"

        messages = self._build_messages(text, service)
        body, content, parsed = self._complete_parsed_json(messages, headers)
        logger = getattr(service, "append_resolver_debug_log", None)
        if callable(logger):
            logger(stage="resolve_text_request", messages=messages, response_body=body, response_content=content)
        action = self._normalize_action_name(parsed.get("action"))
        parameters = parsed.get("parameters", {})
        if not action:
            raise ResolverError("Resolver output did not include an action.")
        if not isinstance(parameters, dict):
            raise ResolverError("Resolver output parameters must be an object.")
        parameters = self._normalize_parameters(action, parameters, original_text=text)
        action, parameters = self._normalize_playback_intent(action, parameters)
        return ResolvedAction(
            action=action,
            parameters=parameters,
            resolver="openai_compatible",
            raw=parsed,
            reasoning=self._extract_reasoning(body),
            raw_content=self._extract_raw_content(content),
        )

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int) -> SessionQueryPlan:
        headers = {"Content-Type": "application/json"}
        if self._settings.resolver_api_key:
            headers["Authorization"] = f"Bearer {self._settings.resolver_api_key}"
        messages = self._build_session_messages(request, service, session, count)
        body, content, parsed = self._complete_parsed_json(messages, headers)
        logger = getattr(service, "append_resolver_debug_log", None)
        if callable(logger):
            logger(stage="plan_session_query", messages=messages, response_body=body, response_content=content)
        search_sources = self._normalize_search_sources(parsed.get("search_sources"))
        if not search_sources:
            search_sources = [
                SessionSearchSource(kind="vibe", term=query)
                for query in self._normalize_candidate_queries(parsed.get("search_queries"))
            ]
        if not search_sources:
            synthesized = self._fallback_query_from_text(request)
            if synthesized:
                search_sources = [SessionSearchSource(kind="vibe", term=synthesized)]
        search_sources = search_sources[: self.MAX_SESSION_SEARCH_QUERIES]
        return SessionQueryPlan(
            search_sources=search_sources,
            resolver="openai_compatible",
            raw=parsed,
            reasoning=self._extract_reasoning(body),
            raw_content=self._extract_raw_content(content),
        )

    def select_session_track(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_query: str,
        candidates: list[dict[str, Any]],
    ) -> SessionTrackSelection:
        headers = {"Content-Type": "application/json"}
        if self._settings.resolver_api_key:
            headers["Authorization"] = f"Bearer {self._settings.resolver_api_key}"
        messages = self._build_session_selection_messages(request, service, session, search_query, candidates)
        body, content, parsed = self._complete_parsed_json(messages, headers)
        logger = getattr(service, "append_resolver_debug_log", None)
        if callable(logger):
            logger(stage="select_session_track", messages=messages, response_body=body, response_content=content)
        selected_index = parsed.get("selected_index")
        if not isinstance(selected_index, int):
            selected_index = 0
        if selected_index < -1:
            selected_index = -1
        if selected_index >= len(candidates):
            selected_index = 0
        return SessionTrackSelection(
            selected_index=selected_index,
            resolver="openai_compatible",
            raw=parsed,
            reasoning=self._extract_reasoning(body),
            raw_content=self._extract_raw_content(content),
        )

    def select_session_playlist(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_source: SessionSearchSource,
        candidates: list[dict[str, Any]],
    ) -> SessionTrackSelection:
        headers = {"Content-Type": "application/json"}
        if self._settings.resolver_api_key:
            headers["Authorization"] = f"Bearer {self._settings.resolver_api_key}"
        messages = self._build_playlist_selection_messages(request, service, session, search_source, candidates)
        body, content, parsed = self._complete_parsed_json(messages, headers)
        logger = getattr(service, "append_resolver_debug_log", None)
        if callable(logger):
            logger(stage="select_session_playlist", messages=messages, response_body=body, response_content=content)
        selected_index = parsed.get("selected_index")
        if not isinstance(selected_index, int) or selected_index >= len(candidates):
            selected_index = 0
        if selected_index < -1:
            selected_index = -1
        return SessionTrackSelection(
            selected_index=selected_index,
            resolver="openai_compatible",
            raw=parsed,
            reasoning=self._extract_reasoning(body),
            raw_content=self._extract_raw_content(content),
        )

    def rephrase_session_vibe(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_source: SessionSearchSource,
        attempted_terms: list[str],
    ) -> str | None:
        headers = {"Content-Type": "application/json"}
        if self._settings.resolver_api_key:
            headers["Authorization"] = f"Bearer {self._settings.resolver_api_key}"
        messages = self._build_vibe_rephrase_messages(request, session, search_source, attempted_terms)
        body, content, parsed = self._complete_parsed_json(messages, headers)
        logger = getattr(service, "append_resolver_debug_log", None)
        if callable(logger):
            logger(stage="rephrase_session_vibe", messages=messages, response_body=body, response_content=content)
        term = parsed.get("term")
        if not isinstance(term, str) or not term.strip():
            return None
        normalized = term.strip()
        if normalized.casefold() in {item.casefold() for item in attempted_terms}:
            return None
        return normalized[:120]

    def _build_messages(self, text: str, service: Any) -> list[dict[str, str]]:
        playback = service.playback_snapshot()
        active_session = service.session_status(include_recent_tracks=False, compact=False).get("session")
        context = {
            "current_timestamp": service.current_timestamp(),
            "default_search_source": service.default_search_source(),
            "playback_summary": {
                "is_playing": playback.get("is_playing"),
                "track": playback.get("track"),
                "queue_length": playback.get("queue_length"),
            },
            "active_session": active_session,
            "preferences": service.list_preferences()["preferences"][:5],
            "allowed_actions": self._resolver_action_specs(),
        }
        system = (
            "You are the Vesper request resolver. "
            "Return exactly one JSON object with keys action and parameters. "
            "Use only an action from allowed_actions. "
            "Do not explain anything. "
            "Use the bare action name only, for example next_track not next_track(). "
            "Do not use markdown, code fences, comments, or prose. "
            "Use play_session for descriptive music requests, vibe requests, activity requests, or artist-only requests. "
            "Use list_library_playlists when the user asks what playlists are available or to list playlists. "
            "Use play_library_playlist when the user asks to play a specific playlist by name. "
            "Use like_current_track when the user says they like the current song or track. "
            "Use steer_session only when there is already an active session and the user wants to shape future picks. "
            "Use reject_current_track when the user dislikes only the current song and wants a new one now. "
            "When the user asks for vague playback like 'play some music', still use play_session. "
            "Use play_search_result or play_candidate_match only for specific song requests. "
            "For play_session and steer_session, always use parameters.request. "
            "For steer_session search_update, use {mode, sources}; each source has kind artist, genre, or vibe and term. "
            "Keep request text short and concrete. "
            "Do not invent songs, artists, or albums."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Context:\n{json.dumps(context, ensure_ascii=True)}\n\nRequest:\n{text}"},
        ]

    def _build_session_messages(self, request: str, service: Any, session: dict[str, Any], count: int) -> list[dict[str, str]]:
        context = {
            "current_timestamp": service.current_timestamp(),
            "session_request": session.get("request_text"),
            "session_steering": session.get("steering_history", [])[-5:],
            "playback_summary": self._compact_session_playback_summary(service.session_planning_playback_snapshot(session)),
            "preferences": service.list_preferences()["preferences"][:5],
            "supported_genres": service.session_genre_names(),
            "rejected_sources": service.session_rejected_search_sources(session),
            "count": count,
        }
        system = (
            "You are planning the next typed source for an adaptive music session in Vesper. "
            "Return only JSON with key search_sources, containing objects with kind and term. "
            f"The session needs {count} source right now; return exactly 1 source when possible. "
            "Allowed kinds are artist, genre, and vibe. "
            "Use artist for artist names, including artist-plus-mood requests; put only the artist name in term. "
            "Use genre only when term exactly matches one of supported_genres. "
            "Use vibe for genre-plus-mood/activity, unsupported subgenres, and descriptive requests. "
            "If supported_genres is empty, never use genre. "
            "Never repeat a source listed in rejected_sources; choose a materially different source. "
            "Honor the original session_request, steering changes, and the current timestamp. "
            "Treat session_steering as persistent cumulative session state, not a one-turn hint. "
            "Negative steering must continue to apply to future selections until explicitly overridden. "
            "Positive steering must continue to shape future selections until explicitly overridden. "
            "If the session request is extremely vague, the service may already seed playback from saved preferences; do not force a made-up micro-vibe in that case. "
            "If the request is generic, you may adapt to time of day, such as higher energy in the morning and calmer music late at night. "
            "If the user already asked for a specific genre, artist, era, or other concrete music descriptor, preserve that request broadly instead of narrowing it to a more specific sub-vibe, mood, or subset unless the user explicitly asked for that narrowing. "
            "For example, a request like 'play trip-hop' should stay broad and should not be rewritten into a narrower variant like 'atmospheric trip hop' unless the user asked for atmospheric music. "
            "Use more creative interpretation only when the request is open-ended, contextual, or activity-based, such as cleaning, studying, waking up, winding down, or hosting people. "
            "Do not guess final tracks from memory here; choose the best typed Apple Music retrieval source."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Context:\n{json.dumps(context, ensure_ascii=True)}\n\nPlan the next search query for this session."},
        ]

    def _build_playlist_selection_messages(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_source: SessionSearchSource,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        context = {
            "current_timestamp": service.current_timestamp(),
            "session_request": session.get("request_text"),
            "session_steering": session.get("steering_history", [])[-5:],
            "search_source": {"kind": search_source.kind, "term": search_source.term},
            "candidates": candidates[:5],
        }
        system = (
            "Choose the Apple Music playlist that best matches this adaptive session vibe. "
            "Return only JSON with shape {\"selected_index\": number}. "
            "Use -1 when none of the playlists fit."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Context:\n{json.dumps(context, ensure_ascii=True)}\n\nChoose a playlist."},
        ]

    def _build_vibe_rephrase_messages(
        self,
        request: str,
        session: dict[str, Any],
        search_source: SessionSearchSource,
        attempted_terms: list[str],
    ) -> list[dict[str, str]]:
        context = {
            "session_request": session.get("request_text"),
            "effective_request": request,
            "failed_vibe_term": search_source.term,
            "attempted_terms": attempted_terms,
        }
        system = (
            "Rewrite a failed Apple Music playlist search term for an adaptive music-session vibe. "
            "Return only JSON with shape {\"term\": string}. "
            "Use a short, broader phrase suitable for playlist search. "
            "Do not repeat any attempted_terms. "
            "Preserve the user's core mood, activity, genre, or era."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Context:\n{json.dumps(context, ensure_ascii=True)}\n\nReturn a new term."},
        ]

    def _normalize_search_sources(self, value: Any) -> list[SessionSearchSource]:
        if not isinstance(value, list):
            return []
        sources: list[SessionSearchSource] = []
        seen: set[tuple[str, str]] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            term = str(item.get("term", "")).strip()
            if kind not in {"artist", "genre", "vibe"} or not term:
                continue
            key = (kind, term.casefold())
            if key in seen:
                continue
            seen.add(key)
            sources.append(SessionSearchSource(kind=kind, term=term))
        return sources

    def _build_session_selection_messages(
        self,
        request: str,
        service: Any,
        session: dict[str, Any],
        search_query: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        context = {
            "current_timestamp": service.current_timestamp(),
            "session_request": session.get("request_text"),
            "session_steering": session.get("steering_history", [])[-5:],
            "playback_summary": self._compact_session_playback_summary(service.session_planning_playback_snapshot(session)),
            "search_query": search_query,
            "candidates": [
                self._compact_session_selection_candidate(candidate)
                for candidate in candidates[: self.MAX_SESSION_SELECTION_CANDIDATES]
            ],
        }
        system = (
            "You are choosing the next track for an adaptive music session in Vesper from real Apple Music results. "
            "Return only JSON with shape {\"selected_index\": number}. "
            "Choose the single best candidate for the session request and steering. "
            "Treat session_steering as persistent cumulative session state, not a one-turn hint. "
            "Negative steering must continue to apply until explicitly overridden. "
            "Positive steering must continue to apply until explicitly overridden. "
            "Prefer candidates that fit the session direction and avoid recent repeats. "
            "If none of the shown candidates are suitable, return {\"selected_index\": -1}."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Context:\n{json.dumps(context, ensure_ascii=True)}\n\nChoose the best candidate index for the next track."},
        ]

    def _compact_session_selection_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key in ("id", "title", "artist", "album"):
            value = candidate.get(key)
            if value is not None:
                compact[key] = value
        return compact

    def _compact_session_playback_summary(self, playback: dict[str, Any]) -> dict[str, Any]:
        compact = {
            "is_playing": playback.get("is_playing"),
            "queue_length": playback.get("queue_length"),
        }
        track = playback.get("track", {})
        if isinstance(track, dict):
            compact["track"] = {
                key: track.get(key)
                for key in ("title", "artist", "album")
                if key in track
            }
        return compact

    def _complete_json(self, messages: list[dict[str, str]], headers: dict[str, str]) -> tuple[dict[str, Any], str]:
        payload = {
            "model": self._settings.resolver_model,
            "messages": messages,
            **self._reasoning_request_options(),
        }
        try:
            response = self._session.post("/chat/completions", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ResolverError(f"Could not reach resolver endpoint at {self._settings.resolver_base_url}: {exc}") from exc

        if response.is_error:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise ResolverError(f"Resolver endpoint returned HTTP {response.status_code}: {detail}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ResolverError("Resolver endpoint returned non-JSON output.") from exc
        return body, self._extract_content(body)

    def _reasoning_request_options(self) -> dict[str, Any]:
        include_reasoning = self._settings.resolver_include_reasoning
        effort = "medium" if include_reasoning else "none"
        return {
            "think": include_reasoning,
            "reasoning_effort": effort,
            "reasoning": {"effort": effort},
        }

    def _complete_parsed_json(
        self, messages: list[dict[str, str]], headers: dict[str, str]
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        last_error: ResolverError | None = None
        for attempt in range(1, self.MAX_COMPLETION_ATTEMPTS + 1):
            try:
                body, content = self._complete_json(messages, headers)
                return body, content, self._parse_json_object(content)
            except ResolverError as exc:
                last_error = exc
                if attempt == self.MAX_COMPLETION_ATTEMPTS:
                    break
        if last_error is None:
            raise ResolverError("Resolver request failed without producing a completion result.")
        raise ResolverError(
            f"Resolver failed after {self.MAX_COMPLETION_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    def _extract_content(self, body: dict[str, Any]) -> str:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ResolverError("Resolver response did not include choices.")
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            return "\n".join(part for part in text_parts if part)
        raise ResolverError("Resolver response did not include text content.")

    def _extract_reasoning(self, body: dict[str, Any]) -> str | None:
        if not self._settings.resolver_include_reasoning:
            return None
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = choices[0].get("message", {})
        for key in ("reasoning", "reasoning_content", "thinking"):
            extracted = self._extract_text_block(message.get(key))
            if extracted:
                return extracted
        return None

    def _extract_text_block(self, value: Any) -> str | None:
        if isinstance(value, str):
            trimmed = value.strip()
            return trimmed or None
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    trimmed = item.strip()
                    if trimmed:
                        parts.append(trimmed)
                    continue
                if not isinstance(item, dict):
                    continue
                for key in ("text", "content", "reasoning", "thinking"):
                    nested = item.get(key)
                    if isinstance(nested, str):
                        trimmed = nested.strip()
                        if trimmed:
                            parts.append(trimmed)
                            break
            if parts:
                return "\n".join(parts)
        if isinstance(value, dict):
            for key in ("text", "content", "reasoning", "thinking"):
                nested = value.get(key)
                extracted = self._extract_text_block(nested)
                if extracted:
                    return extracted
        return None

    def _extract_raw_content(self, content: str) -> str | None:
        if not self._settings.resolver_include_raw_output:
            return None
        trimmed = content.strip()
        return trimmed or None

    def _parse_json_object(self, content: str) -> dict[str, Any]:
        content = content.strip()
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < start:
            raise ResolverError("Resolver output did not contain a JSON object.")
        try:
            parsed = json.loads(content[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ResolverError(f"Resolver output was not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ResolverError("Resolver output JSON must be an object.")
        return parsed

    def _normalize_parameters(self, action: str, parameters: dict[str, Any], *, original_text: str) -> dict[str, Any]:
        normalized = dict(parameters)
        if action == "forget_preference":
            if "preference_id" not in normalized:
                for alias in ("id", "preference", "preferenceId"):
                    if alias in normalized:
                        normalized["preference_id"] = normalized[alias]
                        break
        if action == "play_library_playlist":
            playlist_name = normalized.get("playlist_name")
            if not isinstance(playlist_name, str):
                for alias in ("name", "playlist", "query", "request"):
                    candidate = normalized.get(alias)
                    if isinstance(candidate, str):
                        playlist_name = candidate
                        normalized["playlist_name"] = candidate.strip()
                        break
            elif playlist_name.strip():
                normalized["playlist_name"] = playlist_name.strip()
            for alias in ("name", "playlist", "query"):
                normalized.pop(alias, None)
        if action in {"search", "search_catalog", "search_library", "search_catalog_tracks", "search_library_tracks", "play_search_result"}:
            query = normalized.get("query")
            if not isinstance(query, str):
                for alias in ("request", "text", "term", "search"):
                    candidate = normalized.get(alias)
                    if isinstance(candidate, str):
                        query = candidate
                        normalized["query"] = candidate
                        break
            if isinstance(query, str):
                normalized_query = self._normalize_query_text(query)
                if normalized_query:
                    normalized["query"] = normalized_query
        if action == "play_candidate_match":
            normalized["candidate_tracks"] = self._normalize_candidate_tracks(normalized.get("candidate_tracks"))
            normalized["candidate_artists"] = self._normalize_candidate_artists(normalized.get("candidate_artists"))
            fallback_queries = normalized.get("candidate_queries")
            if fallback_queries is None:
                fallback_queries = normalized.get("candidate_query")
            normalized["candidate_queries"] = self._normalize_candidate_queries(fallback_queries)
            if not normalized["candidate_queries"]:
                synthesized = self._fallback_query_from_text(original_text)
                if synthesized:
                    normalized["candidate_queries"] = [synthesized]
            normalized.pop("candidate_query", None)
        if action in {"play_session", "steer_session"}:
            request = normalized.get("request")
            if not isinstance(request, str):
                request = normalized.get("request_text")
            if isinstance(request, str):
                normalized["request"] = request.strip()
            elif original_text.strip():
                normalized["request"] = original_text.strip()
            if action == "steer_session":
                normalized["search_update"] = self._normalize_search_update(normalized.get("search_update"))
            normalized.pop("request_text", None)
        return normalized

    def _normalize_action_name(self, value: Any) -> str:
        action = str(value or "").strip()
        if not action:
            return ""
        action = re.sub(r"\s*\([^)]*\)\s*$", "", action).strip()
        normalized_key = action.casefold().replace("-", "_").replace(" ", "_")
        return self.ACTION_ALIASES.get(normalized_key, normalized_key)

    def _resolver_action_specs(self) -> list[str]:
        specs: list[str] = []
        for definition in list_resolver_action_definitions():
            if definition.name == "play_search_result":
                specs.append("play_search_result(query[, source, index])")
                continue
            if definition.name == "play_candidate_match":
                specs.append("play_candidate_match(candidate_tracks|candidate_queries)")
                continue
            if definition.name == "set_volume":
                specs.append("set_volume(volume)")
                continue
            if definition.name == "forget_preference":
                specs.append("forget_preference(preference_id)")
                continue
            if definition.name == "play_library_playlist":
                specs.append("play_library_playlist(playlist_name)")
                continue
            if definition.name == "steer_session":
                specs.append("steer_session(request[, search_update])")
                continue
            if definition.required_fields:
                specs.append(f"{definition.name}({', '.join(definition.required_fields)})")
                continue
            specs.append(f"{definition.name}()")
        return specs

    def _normalize_playback_intent(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if action != "play_candidate_match":
            return action, parameters

        candidate_tracks = parameters.get("candidate_tracks", [])
        candidate_artists = parameters.pop("candidate_artists", [])
        if not candidate_tracks and candidate_artists:
            return "play_session", {"request": candidate_artists[0]}
        return action, parameters

    def _normalize_query_text(self, query: str) -> str:
        cleaned = query.strip()
        substitutions = [
            r"^(popular|top|best|hit)\s+(songs|tracks)\s+by\s+",
            r"^(songs|tracks|music)\s+by\s+",
            r"^(play|find|search(?:\s+for)?)\s+",
            r"^(some|one of)\s+",
            r"\.\s*one of (his|her|their)\s+more\s+popular\s+songs\.?$",
            r"\.\s*one of (his|her|their)\s+popular\s+songs\.?$",
        ]
        for pattern in substitutions:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip(" .,:;!?\"'")
        return cleaned or query.strip()

    def _fallback_query_from_text(self, text: str) -> str:
        cleaned = self._normalize_query_text(text)
        lowered = cleaned.casefold()
        if lowered.startswith("music for "):
            subject = cleaned[10:].strip()
            return f"{subject} music".strip() if subject else cleaned
        if lowered.startswith("music to "):
            subject = cleaned[9:].strip()
            subject = re.sub(r"^make me\s+", "", subject, flags=re.IGNORECASE)
            return f"{subject} music".strip() if subject else cleaned
        return cleaned

    def _normalize_candidate_tracks(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        tracks: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            artist = str(item.get("artist", "")).strip()
            if title and artist:
                tracks.append({"title": title, "artist": artist})
        return tracks

    def _normalize_candidate_artists(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _normalize_candidate_queries(self, value: Any) -> list[str]:
        if isinstance(value, str):
            normalized = self._normalize_query_text(value)
            return [normalized] if normalized else []
        if not isinstance(value, list):
            return []
        return [self._normalize_query_text(str(item)) for item in value if str(item).strip()]

    def _normalize_search_update(self, value: Any) -> dict[str, Any]:
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
        return {
            "mode": mode,
            "sources": [{"kind": source.kind, "term": source.term} for source in sources],
        }


def build_resolver(settings: Settings) -> Resolver:
    """Create the configured resolver backend."""

    if settings.resolver_backend == "openai_compatible":
        return OpenAICompatibleResolver(settings)
    return FallbackResolver()
