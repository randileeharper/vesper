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


class Resolver(Protocol):
    """Resolve freeform user text into a structured action."""

    def resolve(self, text: str, service: Any) -> ResolvedAction:
        """Resolve text into an action."""


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

        prompt = self._build_messages(text, service)
        payload = {
            "model": self._settings.resolver_model,
            "messages": prompt,
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

        content = self._extract_content(body)
        parsed = self._parse_json_object(content)
        action = str(parsed.get("action", "")).strip()
        parameters = parsed.get("parameters", {})
        if not action:
            raise ResolverError("Resolver output did not include an action.")
        if not isinstance(parameters, dict):
            raise ResolverError("Resolver output parameters must be an object.")
        parameters = self._normalize_parameters(action, parameters)
        return ResolvedAction(action=action, parameters=parameters, resolver="openai_compatible", raw=parsed)

    def _build_messages(self, text: str, service: Any) -> list[dict[str, str]]:
        playback = service.playback_snapshot()
        context = {
            "default_search_source": service.default_search_source(),
            "playback_summary": {
                "is_playing": playback.get("is_playing"),
                "track": playback.get("track"),
                "queue_length": playback.get("queue_length"),
            },
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
                "If the user asks to play a song, artist, or search phrase, prefer play_search_result with query text and index 0.",
                "For search or play_search_result, query should be the minimal artist/song text, not a descriptive phrase.",
                "Bad query example: 'popular songs by Pink'. Good query example: 'Pink'.",
                "If the user asks what is playing, use get_now_playing.",
                "If the user asks to resume, use play. If the user asks to pause, use pause.",
            ],
        }
        system = (
            "You are a music control resolver for cider_agent. "
            "Convert a user request into one structured action for the supported action set. "
            "Return only JSON with shape {\"action\": string, \"parameters\": object}. "
            "Do not explain your reasoning. "
            "Prefer direct execution actions over informational searches when the user clearly asked to play or pause something."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Context:\n{json.dumps(context, ensure_ascii=True)}\n\nRequest:\n{text}"},
        ]

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

    def _normalize_parameters(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(parameters)
        if action in {"search", "search_catalog", "search_library", "search_catalog_tracks", "search_library_tracks", "play_search_result"}:
            query = normalized.get("query")
            if isinstance(query, str):
                normalized_query = self._normalize_query_text(query)
                if normalized_query:
                    normalized["query"] = normalized_query
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


def build_resolver(settings: Settings) -> Resolver:
    """Create the configured resolver backend."""

    if settings.resolver_backend == "openai_compatible":
        return OpenAICompatibleResolver(settings)
    return FallbackResolver()
