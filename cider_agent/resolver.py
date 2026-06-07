"""Text-to-action resolution for cider_agent."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

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
class SessionPlan:
    """Candidate tracks and fallback queries for an adaptive play session."""

    candidate_tracks: list[dict[str, str]]
    candidate_artists: list[str]
    candidate_queries: list[str]
    resolver: str
    raw: dict[str, Any] | None = None
    reasoning: str | None = None
    raw_content: str | None = None


class Resolver(Protocol):
    """Resolve freeform user text into a structured action."""

    def resolve(self, text: str, service: Any) -> ResolvedAction:
        """Resolve text into an action."""

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int) -> SessionPlan:
        """Generate the next candidate tracks for an adaptive session."""


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

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int) -> SessionPlan:
        query = request.strip()
        return SessionPlan(
            candidate_tracks=[],
            candidate_artists=[],
            candidate_queries=[query] if query else [],
            resolver="fallback",
        )


class OpenAICompatibleResolver:
    """Resolve text requests using an OpenAI-compatible chat completions endpoint."""

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

        body, content = self._complete_json(self._build_messages(text, service), headers)
        parsed = self._parse_json_object(content)
        action = str(parsed.get("action", "")).strip()
        parameters = parsed.get("parameters", {})
        if not action:
            raise ResolverError("Resolver output did not include an action.")
        if not isinstance(parameters, dict):
            raise ResolverError("Resolver output parameters must be an object.")
        parameters = self._normalize_parameters(action, parameters, original_text=text)
        return ResolvedAction(
            action=action,
            parameters=parameters,
            resolver="openai_compatible",
            raw=parsed,
            reasoning=self._extract_reasoning(body),
            raw_content=self._extract_raw_content(content),
        )

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int) -> SessionPlan:
        headers = {"Content-Type": "application/json"}
        if self._settings.resolver_api_key:
            headers["Authorization"] = f"Bearer {self._settings.resolver_api_key}"
        body, content = self._complete_json(self._build_session_messages(request, service, session, count), headers)
        parsed = self._parse_json_object(content)
        candidate_tracks = self._normalize_candidate_tracks(parsed.get("candidate_tracks"))
        candidate_artists = self._normalize_candidate_artists(parsed.get("candidate_artists"))
        candidate_queries = self._normalize_candidate_queries(parsed.get("candidate_queries"))
        artist_seed = self._extract_artist_seed(request)
        if artist_seed and artist_seed not in candidate_artists:
            candidate_artists = [artist_seed, *candidate_artists]
        if artist_seed and not self._request_mentions_specific_track(request):
            candidate_tracks = []
        if artist_seed and not candidate_queries:
            candidate_queries = [artist_seed]
        if not candidate_queries:
            synthesized = self._fallback_query_from_text(request)
            if synthesized:
                candidate_queries = [synthesized]
        return SessionPlan(
            candidate_tracks=candidate_tracks,
            candidate_artists=candidate_artists,
            candidate_queries=candidate_queries,
            resolver="openai_compatible",
            raw=parsed,
            reasoning=self._extract_reasoning(body),
            raw_content=self._extract_raw_content(content),
        )

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
            "supported_actions": [
                "status",
                "get_now_playing",
                "play",
                "pause",
                "playpause",
                "stop",
                "next_track",
                "previous_track",
                "seek",
                "get_volume",
                "set_volume",
                "get_queue",
                "remove_queue_item",
                "clear_queue",
                "play_url",
                "search",
                "search_catalog",
                "search_library",
                "search_catalog_tracks",
                "search_library_tracks",
                "play_candidate_match",
                "play_session",
                "steer_session",
                "reject_current_track",
                "session_status",
                "stop_session",
                "play_search_result",
                "remember_preference",
                "list_preferences",
                "forget_preference",
                "recommend",
                "play_recommendation",
            ],
            "notes": [
                "Return JSON only with keys action and parameters.",
                "Use source='default' when selecting search results unless the request explicitly mentions library or catalog.",
                "Playlist creation and add-track mutation are unsupported and must not be selected.",
                "If the user names a specific song and artist, you may use play_search_result or play_candidate_match.",
                "If the user asks for a vibe, era, popularity, activity, time-of-day, or descriptive request, prefer play_session.",
                "If the user asks for something by an artist without naming a specific track, prefer play_session.",
                "If there is an active session and the user asks for a change like 'more pop' or 'more of this artist', prefer steer_session.",
                "If there is an active session and the new request implies a major change of activity, mood, or context, starting a new play_session is acceptable.",
                "If there is an active session and the user says things like 'I don't like this', 'skip this', 'not this one', or otherwise rejects only the current track, prefer reject_current_track instead of changing the whole session vibe.",
                "For play_candidate_match, provide candidate_tracks as [{'title': ..., 'artist': ...}] when possible.",
                "For play_candidate_match, provide candidate_artists only as fallback support.",
                "For play_candidate_match, always include candidate_queries for descriptive requests as last-resort fallback search phrases.",
                "Do not invent fake artists or track titles. Prefer real, attributable music. If uncertain, rely more on candidate_queries.",
                "If the user names an artist but not a specific song, do not guess a current track from memory. Prefer artist-driven live catalog selection via play_session or candidate_artists.",
                "For play_session and steer_session, use the parameter name 'request'. Do not use 'request_text' or any alternate field names.",
                "Bad fallback query example: 'popular songs by Pink'. Better candidate artist: 'P!nk'. Better candidate tracks might be her known singles.",
                "If the user asks what is playing, use get_now_playing.",
                "If the user asks to resume, use play. If the user asks to pause, use pause.",
            ],
        }
        system = (
            "You are a music control resolver for cider_agent. "
            "Convert a user request into one structured action for the supported action set. "
            "Return only JSON with shape {\"action\": string, \"parameters\": object}. "
            "Do not explain your reasoning. "
            "Prefer direct execution actions over informational searches when the user clearly asked to play or pause something. "
            "Treat generic or descriptive play requests as adaptive long-form listening sessions. "
            "When a user gives negative feedback about only the currently playing song, reject just that track rather than changing the whole session. "
            "You may infer a helpful music request from surrounding life context when the user is clearly asking for music help, such as cleaning, studying, waking up, or winding down. "
            "For descriptive playback requests, propose concrete track and artist candidates rather than a literal English search phrase. "
            "Never invent obviously fake artist or song names; if you are unsure, include candidate_queries fallback phrases."
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
            "recent_tracks": service.recent_session_tracks(limit=service.session_recent_tracks_limit()),
            "playback_summary": service.playback_snapshot(),
            "preferences": service.list_preferences()["preferences"][:5],
            "count": count,
        }
        system = (
            "You are planning the next tracks for an adaptive music session in cider_agent. "
            "Return only JSON with keys candidate_tracks, candidate_artists, and candidate_queries. "
            "candidate_tracks must be a list of objects shaped like {\"title\": string, \"artist\": string}. "
            "Use real, attributable music. Do not invent fake artist or track names. "
            "Avoid repeating tracks from recent_tracks unless truly necessary. "
            "Honor the original session_request, steering changes, and the current timestamp. "
            "If the request is generic, you may adapt to time of day, such as higher energy in the morning and calmer music late at night. "
            "If the request names an artist but not a specific song, prefer candidate_artists and let the service retrieve live catalog tracks rather than guessing exact songs from memory. "
            "Always include at least one candidate_queries fallback phrase."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Context:\n{json.dumps(context, ensure_ascii=True)}\n\nPlan the next tracks for this session."},
        ]

    def _complete_json(self, messages: list[dict[str, str]], headers: dict[str, str]) -> tuple[dict[str, Any], str]:
        payload = {
            "model": self._settings.resolver_model,
            "messages": messages,
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
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning.strip()
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
        if action in {"search", "search_catalog", "search_library", "search_catalog_tracks", "search_library_tracks", "play_search_result"}:
            query = normalized.get("query")
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
            normalized.pop("request_text", None)
        return normalized

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

    def _extract_artist_seed(self, text: str) -> str | None:
        patterns = [
            r"^\s*play\s+(?:some|something\s+by|music\s+by)\s+(.+?)\s*$",
            r"^\s*more\s+of\s+(.+?)\s*$",
            r"^\s*add\s+(?:some|something\s+by|music\s+by)\s+(.+?)\s*$",
        ]
        for pattern in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = match.group(1).strip(" .,:;!?\"'")
            candidate = re.sub(r"\s+(please|for me)$", "", candidate, flags=re.IGNORECASE).strip()
            if candidate and not self._request_mentions_specific_track(text):
                return candidate
        return None

    def _request_mentions_specific_track(self, text: str) -> bool:
        lowered = text.casefold()
        markers = [
            "song ",
            "track ",
            "called ",
            "named ",
            " titled ",
            " title ",
        ]
        return any(marker in lowered for marker in markers)

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


def build_resolver(settings: Settings) -> Resolver:
    """Create the configured resolver backend."""

    if settings.resolver_backend == "openai_compatible":
        return OpenAICompatibleResolver(settings)
    return FallbackResolver()
