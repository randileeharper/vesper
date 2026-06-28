"""Small, dependency-free helpers shared across the service and session layers.

These were previously defined as private helpers in :mod:`vesper.service` and
imported from there by the session modules, which created a circular import
with :class:`vesper.service.CiderAgentService` (issue #44). Moving them here
breaks that cycle: :mod:`vesper.session` and its mixins import from this module
instead of ``vesper.service``, and ``vesper.service`` itself imports them from
here as well.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote


def _elapsed_ms(start: float) -> float:
    """Elapsed time in milliseconds since *start* (a ``perf_counter`` value)."""
    return round((time.perf_counter() - start) * 1000.0, 2)


def _encode_query(query: str) -> str:
    """URL-encode a query term with no safe characters."""
    return quote(query, safe="")


def _clean_id(value: Any) -> str:
    """Normalize an id-like value to a stripped string.

    ``None`` (and the literal string ``"none"``) collapse to the empty string so
    callers can treat missing ids uniformly.
    """
    if value is None:
        return ""
    cleaned = str(value).strip()
    return "" if cleaned.lower() == "none" else cleaned


def _track_payload(track: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build a normalized track payload for historian events.

    Returns ``None`` when *track* is not a dict or carries no identifiable
    information (no id and no title/artist/album).
    """
    if not isinstance(track, dict):
        return None
    play_params = track.get("play_params", {}) if isinstance(track.get("play_params"), dict) else {}
    track_id = (
        _clean_id(track.get("track_id"))
        or _clean_id(track.get("id"))
        or _clean_id(play_params.get("id"))
    )
    if not track_id and not any(track.get(key) for key in ("title", "artist", "album")):
        return None
    return {
        "id": track_id or None,
        "title": track.get("title"),
        "artist": track.get("artist"),
        "album": track.get("album"),
        "kind": track.get("kind") or track.get("type") or play_params.get("kind"),
        "is_library": track.get("is_library")
        if track.get("is_library") is not None
        else play_params.get("is_library"),
    }


def _preference_target(preference: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract a historian-friendly target dict from a preference row."""
    if not isinstance(preference, dict):
        return None
    return {
        "track_id": preference.get("track_id"),
        "title": preference.get("title"),
        "artist_id": preference.get("artist_id"),
        "artist_name": preference.get("artist_name"),
        "album": preference.get("album"),
    }


def _extract_is_playing(payload: Any) -> bool | None:
    """Normalize various is-playing payload shapes to a bool (or ``None``)."""
    if isinstance(payload, bool):
        return payload
    if isinstance(payload, dict):
        if "value" in payload:
            return bool(payload["value"])
        if "is_playing" in payload:
            return bool(payload["is_playing"])
    return None
