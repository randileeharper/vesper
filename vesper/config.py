"""Configuration loading for Vesper."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

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


def _config_or_env(config: dict[str, Any], env_name: str, key: str, default: Any = None) -> Any:
    if env_name in os.environ:
        return os.environ[env_name]
    return config.get(key, default)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise CiderConfigError(f"Could not interpret boolean value: {value!r}")


@dataclass(frozen=True)
class Settings:
    """Runtime settings for Vesper."""

    http_host: str = "127.0.0.1"
    http_port: int = 8766
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
    session_recent_tracks_limit: int = 10
    session_vibe_rephrase_attempts: int = 3
    global_recent_tracks_limit: int = 10
    request_timeout_seconds: float = 60.0
    verify_tls: bool = True
    log_level: str = "INFO"
    historian_enabled: bool = False
    historian_base_url: str = "http://127.0.0.1:8768"
    historian_token: str | None = None
    historian_timeout_seconds: float = 5.0
    historian_verify_tls: bool = True
    historian_retry_count: int = 2
    database_path: Path = Path("~/.local/share/vesper/vesper.db").expanduser()
    config_path: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        config, config_path = _load_json_config()
        configurable = {item.name for item in fields(cls) if item.init}
        unknown = sorted(set(config) - configurable)
        if unknown:
            raise CiderConfigError(f"Unknown config fields: {', '.join(unknown)}")

        http_host = str(_config_or_env(config, "VESPER_HTTP_HOST", "http_host", "127.0.0.1")).strip()
        http_port = int(_config_or_env(config, "VESPER_HTTP_PORT", "http_port", 8766))
        public_base_url = str(
            _config_or_env(config, "VESPER_PUBLIC_BASE_URL", "public_base_url", f"http://{http_host}:{http_port}")
        ).strip().rstrip("/")
        cider_base_url = str(
            _config_or_env(config, "VESPER_CIDER_BASE_URL", "cider_base_url", "http://localhost:10767")
        ).strip().rstrip("/")
        cider_api_token_raw = _config_or_env(config, "VESPER_CIDER_API_TOKEN", "cider_api_token")
        default_search_source = str(
            _config_or_env(config, "VESPER_DEFAULT_SEARCH_SOURCE", "default_search_source", "catalog")
        ).strip().lower()
        resolver_backend = str(
            _config_or_env(config, "VESPER_RESOLVER_BACKEND", "resolver_backend", "fallback")
        ).strip().lower()
        resolver_base_url = str(
            _config_or_env(config, "VESPER_RESOLVER_BASE_URL", "resolver_base_url", "https://api.openai.com/v1")
        ).strip().rstrip("/")
        resolver_model_raw = _config_or_env(config, "VESPER_RESOLVER_MODEL", "resolver_model")
        resolver_api_key_raw = _config_or_env(config, "VESPER_RESOLVER_API_KEY", "resolver_api_key")
        resolver_include_reasoning = _as_bool(
            _config_or_env(
                config,
                "VESPER_RESOLVER_INCLUDE_REASONING",
                "resolver_include_reasoning",
                False,
            )
        )
        resolver_include_raw_output = _as_bool(
            _config_or_env(
                config,
                "VESPER_RESOLVER_INCLUDE_RAW_OUTPUT",
                "resolver_include_raw_output",
                False,
            )
        )
        resolver_debug_log_path_raw = _config_or_env(
            config,
            "VESPER_RESOLVER_DEBUG_LOG_PATH",
            "resolver_debug_log_path",
        )
        include_timing_debug = _as_bool(
            _config_or_env(
                config,
                "VESPER_INCLUDE_TIMING_DEBUG",
                "include_timing_debug",
                False,
            )
        )
        response_detail = str(
            _config_or_env(
                config,
                "VESPER_RESPONSE_DETAIL",
                "response_detail",
                "compact",
            )
        ).strip().lower()
        session_recent_tracks_limit = int(
            _config_or_env(
                config,
                "VESPER_SESSION_RECENT_TRACKS_LIMIT",
                "session_recent_tracks_limit",
                10,
            )
        )
        session_vibe_rephrase_attempts = int(
            _config_or_env(
                config,
                "VESPER_SESSION_VIBE_REPHRASE_ATTEMPTS",
                "session_vibe_rephrase_attempts",
                3,
            )
        )
        global_recent_tracks_limit = int(
            _config_or_env(
                config,
                "VESPER_GLOBAL_RECENT_TRACKS_LIMIT",
                "global_recent_tracks_limit",
                10,
            )
        )
        request_timeout_seconds = float(
            _config_or_env(config, "VESPER_REQUEST_TIMEOUT_SECONDS", "request_timeout_seconds", 60.0)
        )
        verify_tls = _as_bool(_config_or_env(config, "VESPER_VERIFY_TLS", "verify_tls", True))
        log_level = str(_config_or_env(config, "VESPER_LOG_LEVEL", "log_level", "INFO")).strip().upper()
        historian_enabled = _as_bool(
            _config_or_env(config, "VESPER_HISTORIAN_ENABLED", "historian_enabled", False)
        )
        historian_base_url = str(
            _config_or_env(
                config,
                "VESPER_HISTORIAN_BASE_URL",
                "historian_base_url",
                "http://127.0.0.1:8768",
            )
        ).strip().rstrip("/")
        historian_token_raw = _config_or_env(config, "VESPER_HISTORIAN_TOKEN", "historian_token")
        historian_timeout_seconds = float(
            _config_or_env(
                config,
                "VESPER_HISTORIAN_TIMEOUT_SECONDS",
                "historian_timeout_seconds",
                5.0,
            )
        )
        historian_verify_tls = _as_bool(
            _config_or_env(config, "VESPER_HISTORIAN_VERIFY_TLS", "historian_verify_tls", True)
        )
        historian_retry_count = int(
            _config_or_env(config, "VESPER_HISTORIAN_RETRY_COUNT", "historian_retry_count", 2)
        )
        database_path = Path(
            str(
                _config_or_env(
                    config,
                    "VESPER_DATABASE_PATH",
                    "database_path",
                    "~/.local/share/vesper/vesper.db",
                )
            )
        ).expanduser()

        cider_api_token = str(cider_api_token_raw).strip() if cider_api_token_raw is not None else None
        resolver_model = str(resolver_model_raw).strip() if resolver_model_raw is not None else None
        resolver_api_key = str(resolver_api_key_raw).strip() if resolver_api_key_raw is not None else None
        historian_token = str(historian_token_raw).strip() if historian_token_raw is not None else None
        resolver_debug_log_path = (
            Path(str(resolver_debug_log_path_raw)).expanduser()
            if resolver_debug_log_path_raw not in {None, ""}
            else None
        )

        if not http_host:
            raise CiderConfigError("http_host cannot be empty.")
        if http_port <= 0 or http_port > 65535:
            raise CiderConfigError("http_port must be between 1 and 65535.")
        if not public_base_url:
            raise CiderConfigError("public_base_url cannot be empty.")
        if not cider_base_url:
            raise CiderConfigError("cider_base_url cannot be empty.")
        if default_search_source not in {"catalog", "library"}:
            raise CiderConfigError("default_search_source must be either 'catalog' or 'library'.")
        if resolver_backend not in {"fallback", "openai_compatible"}:
            raise CiderConfigError("resolver_backend must be either 'fallback' or 'openai_compatible'.")
        if not resolver_base_url:
            raise CiderConfigError("resolver_base_url cannot be empty.")
        if resolver_backend == "openai_compatible" and not resolver_model:
            raise CiderConfigError("resolver_model is required when resolver_backend is openai_compatible.")
        if response_detail not in {"compact", "debug"}:
            raise CiderConfigError("response_detail must be either 'compact' or 'debug'.")
        if session_recent_tracks_limit <= 0 or session_recent_tracks_limit > 200:
            raise CiderConfigError("session_recent_tracks_limit must be between 1 and 200.")
        if session_vibe_rephrase_attempts < 1 or session_vibe_rephrase_attempts > 10:
            raise CiderConfigError("session_vibe_rephrase_attempts must be between 1 and 10.")
        if global_recent_tracks_limit <= 0 or global_recent_tracks_limit > 500:
            raise CiderConfigError("global_recent_tracks_limit must be between 1 and 500.")
        if request_timeout_seconds <= 0:
            raise CiderConfigError("request_timeout_seconds must be positive.")
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise CiderConfigError("log_level must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG.")
        if not historian_base_url:
            raise CiderConfigError("historian_base_url cannot be empty.")
        if historian_enabled and not historian_token:
            raise CiderConfigError("historian_token is required when historian_enabled is true.")
        if historian_timeout_seconds <= 0:
            raise CiderConfigError("historian_timeout_seconds must be positive.")
        if historian_retry_count < 0:
            raise CiderConfigError("historian_retry_count must be non-negative.")

        return cls(
            http_host=http_host,
            http_port=http_port,
            public_base_url=public_base_url,
            cider_base_url=cider_base_url,
            cider_api_token=cider_api_token,
            default_search_source=default_search_source,
            resolver_backend=resolver_backend,
            resolver_base_url=resolver_base_url,
            resolver_model=resolver_model,
            resolver_api_key=resolver_api_key,
            resolver_include_reasoning=resolver_include_reasoning,
            resolver_include_raw_output=resolver_include_raw_output,
            resolver_debug_log_path=resolver_debug_log_path,
            include_timing_debug=include_timing_debug,
            response_detail=response_detail,
            session_recent_tracks_limit=session_recent_tracks_limit,
            session_vibe_rephrase_attempts=session_vibe_rephrase_attempts,
            global_recent_tracks_limit=global_recent_tracks_limit,
            request_timeout_seconds=request_timeout_seconds,
            verify_tls=verify_tls,
            log_level=log_level,
            historian_enabled=historian_enabled,
            historian_base_url=historian_base_url,
            historian_token=historian_token,
            historian_timeout_seconds=historian_timeout_seconds,
            historian_verify_tls=historian_verify_tls,
            historian_retry_count=historian_retry_count,
            database_path=database_path,
            config_path=config_path,
        )

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
            "log_level": self.log_level,
            "historian_enabled": self.historian_enabled,
            "historian_base_url": self.historian_base_url,
            "has_historian_token": bool(self.historian_token),
            "historian_timeout_seconds": self.historian_timeout_seconds,
            "historian_verify_tls": self.historian_verify_tls,
            "historian_retry_count": self.historian_retry_count,
            "database_path": str(self.database_path),
            "config_path": str(self.config_path) if self.config_path else None,
        }
