"""Configuration loading for Vesper."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import (
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import CiderConfigError


CONFIG_ENV_VAR = "VESPER_CONFIG_PATH"


def _env_path(name: str) -> Path | None:
    value = os.getenv(name)
    if not value:
        return None
    return Path(value).expanduser()


def _xdg_config_home() -> Path:
    return _env_path("XDG_CONFIG_HOME") or (Path.home() / ".config")


def _default_config_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = _env_path(CONFIG_ENV_VAR)
    if explicit is not None:
        paths.append(explicit)
    paths.append(Path.cwd() / "config.json")
    paths.append(_xdg_config_home() / "vesper" / "config.json")
    return paths


def _load_json_config() -> tuple[dict[str, Any], Path | None]:
    """Load the first available JSON config file.

    Returns the parsed payload and the path it was read from, or ``({}, None)``
    when no config file is found. Mirrors the legacy lookup order: an explicit
    ``VESPER_CONFIG_PATH``, then ``./config.json``, then the XDG default.
    """
    for path in _default_config_paths():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CiderConfigError(f"Could not parse config file {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CiderConfigError(f"Config file {path} must contain a JSON object.")
        return payload, path
    return {}, None


class Settings(BaseSettings):
    """Runtime settings for Vesper.

    Each field is declared with its type, default, and validation constraints.
    The :meth:`from_env` classmethod assembles values from a JSON config file
    (lowest priority) and environment variables (highest priority), preserving
    backward-compatible config keys and ``VESPER_*`` env var names. Pydantic
    then coerces and validates the merged values declaratively.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        frozen=True,
        populate_by_name=True,
    )

    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8766, ge=1, le=65535)
    public_base_url: str = "http://127.0.0.1:8766"
    cider_base_url: str = "http://localhost:10767"
    cider_api_token: str | None = None
    default_search_source: str = "catalog"
    resolver_backend: str = "fallback"
    resolver_base_url: str = "https://api.openai.com/v1"
    resolver_model: str | None = None
    resolver_api_key: str | None = None
    resolver_include_reasoning: bool = False
    resolver_include_raw_output: bool = False
    resolver_debug_log_path: Path | None = None
    include_timing_debug: bool = False
    response_detail: str = "compact"
    session_recent_tracks_limit: int = Field(default=10, ge=1, le=200)
    session_vibe_rephrase_attempts: int = Field(default=3, ge=1, le=10)
    global_recent_tracks_limit: int = Field(default=10, ge=1, le=500)
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    verify_tls: bool = True
    cider_retry_count: int = Field(default=2, ge=0)
    log_level: str = "INFO"
    historian_enabled: bool = False
    historian_base_url: str = "http://127.0.0.1:8768"
    historian_token: str | None = None
    historian_timeout_seconds: float = Field(default=5.0, gt=0)
    historian_verify_tls: bool = True
    historian_retry_count: int = Field(default=2, ge=0)
    database_path: Path = Field(default=Path("~/.local/share/vesper/vesper.db").expanduser())
    config_path: Path | None = None
    preferred_language: list[str] | None = None

    @field_validator("preferred_language", mode="before")
    @classmethod
    def _normalize_preferred_language(cls, v: Any) -> Any:
        # Accept None, a comma-separated string, or a list. Empty entries are
        # dropped so a stray "" never becomes a phantom preferred language.
        if v is None or v == "":
            return None
        if isinstance(v, str):
            items = [item.strip() for item in v.split(",")]
        else:
            items = [str(item).strip() for item in v]
        items = [item for item in items if item]
        return items or None

    @field_validator("default_search_source", "response_detail", "log_level", "resolver_backend")
    @classmethod
    def _normalize_lower(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        normalized = v.strip().upper()
        if normalized not in allowed:
            raise ValueError("log_level must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG.")
        return normalized

    @field_validator("default_search_source")
    @classmethod
    def _validate_default_search_source(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"catalog", "library"}:
            raise ValueError("default_search_source must be either 'catalog' or 'library'.")
        return normalized

    @field_validator("response_detail")
    @classmethod
    def _validate_response_detail(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"compact", "debug"}:
            raise ValueError("response_detail must be either 'compact' or 'debug'.")
        return normalized

    @field_validator("resolver_backend")
    @classmethod
    def _validate_resolver_backend(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"fallback", "openai_compatible"}:
            raise ValueError("resolver_backend must be either 'fallback' or 'openai_compatible'.")
        return normalized

    @field_validator(
        "public_base_url",
        "cider_base_url",
        "resolver_base_url",
        "historian_base_url",
    )
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @field_validator("http_host")
    @classmethod
    def _validate_http_host(cls, v: str) -> str:
        normalized = v.strip()
        if not normalized:
            raise ValueError("http_host cannot be empty.")
        return normalized

    @field_validator("cider_api_token", "resolver_model", "resolver_api_key", "historian_token")
    @classmethod
    def _strip_optional_token(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else None

    @field_validator("database_path", mode="before")
    @classmethod
    def _expand_database_path(cls, v: Any) -> Any:
        if v is None or v == "":
            return Path("~/.local/share/vesper/vesper.db").expanduser()
        if isinstance(v, Path):
            return v.expanduser()
        return Path(str(v)).expanduser()

    @field_validator("resolver_debug_log_path", mode="before")
    @classmethod
    def _expand_debug_log_path(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, Path):
            return v.expanduser()
        return Path(str(v)).expanduser()

    @model_validator(mode="after")
    def _validate_cross_field(self) -> "Settings":
        if self.resolver_backend == "openai_compatible" and not self.resolver_model:
            raise ValueError("resolver_model is required when resolver_backend is openai_compatible.")
        if self.historian_enabled and not self.historian_token:
            raise ValueError("historian_token is required when historian_enabled is true.")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from a JSON config file and environment variables.

        Environment variables take precedence over config file values, and
        both take precedence over field defaults. Only explicitly set env
        vars override the config file; unset env vars fall back to config.
        """
        config, config_path = _load_json_config()
        configurable = set(cls.model_fields)
        unknown = sorted(set(config) - configurable)
        if unknown:
            raise CiderConfigError(f"Unknown config fields: {', '.join(unknown)}")

        merged: dict[str, Any] = dict(config)
        for field_name in cls.model_fields:
            env_name = f"VESPER_{field_name}".upper()
            if env_name in os.environ:
                merged[field_name] = os.environ[env_name]
        if config_path is not None:
            merged["config_path"] = config_path

        try:
            return cls(**merged)
        except ValueError as exc:
            raise CiderConfigError(str(exc)) from exc

    def ensure_storage_parent(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def sanitized(self) -> dict[str, Any]:
        return {
            "http_host": self.http_host,
            "http_port": self.http_port,
            "public_base_url": self.public_base_url,
            "cider_base_url": self.cider_base_url,
            "has_cider_api_token": bool(self.cider_api_token),
            "default_search_source": self.default_search_source,
            "resolver_backend": self.resolver_backend,
            "resolver_base_url": self.resolver_base_url,
            "resolver_model": self.resolver_model,
            "has_resolver_api_key": bool(self.resolver_api_key),
            "resolver_include_reasoning": self.resolver_include_reasoning,
            "resolver_include_raw_output": self.resolver_include_raw_output,
            "resolver_debug_log_path": str(self.resolver_debug_log_path) if self.resolver_debug_log_path else None,
            "include_timing_debug": self.include_timing_debug,
            "response_detail": self.response_detail,
            "session_recent_tracks_limit": self.session_recent_tracks_limit,
            "session_vibe_rephrase_attempts": self.session_vibe_rephrase_attempts,
            "global_recent_tracks_limit": self.global_recent_tracks_limit,
            "request_timeout_seconds": self.request_timeout_seconds,
            "verify_tls": self.verify_tls,
            "cider_retry_count": self.cider_retry_count,
            "log_level": self.log_level,
            "historian_enabled": self.historian_enabled,
            "historian_base_url": self.historian_base_url,
            "has_historian_token": bool(self.historian_token),
            "historian_timeout_seconds": self.historian_timeout_seconds,
            "historian_verify_tls": self.historian_verify_tls,
            "historian_retry_count": self.historian_retry_count,
            "database_path": str(self.database_path),
            "config_path": str(self.config_path) if self.config_path else None,
            "preferred_language": self.preferred_language,
        }
