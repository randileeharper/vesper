"""Input-validation helpers for :class:`vesper.service.CiderAgentService`.

Pure validators and coercers extracted out of the service class. None of them
touch instance state: they validate their arguments and raise
:class:`vesper.errors.CiderValidationError` on bad input.
"""

from __future__ import annotations

from typing import Any

from .errors import CiderValidationError


def validate_search(query: str, limit: int) -> None:
    if not query.strip():
        raise CiderValidationError("query cannot be empty.")
    if limit <= 0 or limit > 100:
        raise CiderValidationError("limit must be between 1 and 100.")


def validate_index(value: int, name: str) -> None:
    if value < 0:
        raise CiderValidationError(f"{name} must be non-negative.")


def validate_limit_offset(limit: int, offset: int) -> None:
    if limit <= 0 or limit > 100:
        raise CiderValidationError("limit must be between 1 and 100.")
    if offset < 0:
        raise CiderValidationError("offset must be non-negative.")


def validate_playlist_id(playlist_id: str) -> None:
    if not playlist_id.strip():
        raise CiderValidationError("playlist_id cannot be empty.")


def coerce_volume_param(params: dict[str, Any]) -> int:
    raw = None
    for key in ("volume", "value", "level", "percent"):
        if key in params:
            raw = params[key]
            break
    if raw is None:
        raise CiderValidationError("set_volume requires a volume parameter.")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raise CiderValidationError("volume cannot be empty.")
        numeric = float(raw)
        if "." in raw and 0.0 <= numeric <= 1.0:
            return round(numeric * 100)
        return round(numeric)
    if isinstance(raw, (int, float)):
        numeric = float(raw)
        if isinstance(raw, float) and 0.0 <= numeric <= 1.0:
            return round(numeric * 100)
        return round(numeric)
    raise CiderValidationError(f"volume must be numeric, got {type(raw).__name__}.")
