from __future__ import annotations

import json

import pytest

from vesper.config import Settings
from vesper.errors import CiderConfigError


def test_settings_reads_config_file(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
                {
                    "http_port": 9900,
                    "cider_api_token": "from-config",
                    "default_search_source": "library",
                    "resolver_include_reasoning": True,
                    "resolver_include_raw_output": True,
                    "resolver_debug_log_path": str(tmp_path / "resolver.log"),
                    "include_timing_debug": True,
                    "response_detail": "debug",
                    "session_recent_tracks_limit": 12,
                    "session_vibe_rephrase_attempts": 4,
                    "global_recent_tracks_limit": 34,
                    "database_path": str(tmp_path / "db.sqlite3"),
                }
            ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VESPER_CONFIG_PATH", str(config_path))

    settings = Settings.from_env()

    assert settings.http_port == 9900
    assert settings.cider_api_token == "from-config"
    assert settings.default_search_source == "library"
    assert settings.resolver_include_reasoning is True
    assert settings.resolver_include_raw_output is True
    assert settings.resolver_debug_log_path == tmp_path / "resolver.log"
    assert settings.include_timing_debug is True
    assert settings.response_detail == "debug"
    assert settings.session_recent_tracks_limit == 12
    assert settings.session_vibe_rephrase_attempts == 4
    assert settings.global_recent_tracks_limit == 34
    assert settings.database_path == tmp_path / "db.sqlite3"


def test_historian_config_defaults_and_token_redaction(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VESPER_CONFIG_PATH", str(config_path))

    settings = Settings.from_env()

    assert settings.historian_enabled is False
    assert settings.historian_base_url == "http://127.0.0.1:8768"
    assert settings.historian_timeout_seconds == 5.0
    assert settings.historian_verify_tls is True
    assert settings.historian_retry_count == 2
    assert settings.sanitized()["has_historian_token"] is False
    assert "historian_token" not in settings.sanitized()


def test_historian_environment_overrides_and_normalizes_url(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VESPER_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("VESPER_HISTORIAN_ENABLED", "true")
    monkeypatch.setenv("VESPER_HISTORIAN_BASE_URL", "https://historian.test///")
    monkeypatch.setenv("VESPER_HISTORIAN_TOKEN", "hist_secret")
    monkeypatch.setenv("VESPER_HISTORIAN_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("VESPER_HISTORIAN_VERIFY_TLS", "false")
    monkeypatch.setenv("VESPER_HISTORIAN_RETRY_COUNT", "4")

    settings = Settings.from_env()

    assert settings.historian_enabled is True
    assert settings.historian_base_url == "https://historian.test"
    assert settings.historian_token == "hist_secret"
    assert settings.historian_timeout_seconds == 2.5
    assert settings.historian_verify_tls is False
    assert settings.historian_retry_count == 4


def test_enabled_historian_requires_token(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"historian_enabled": True}), encoding="utf-8")
    monkeypatch.setenv("VESPER_CONFIG_PATH", str(config_path))

    with pytest.raises(CiderConfigError, match="historian_token"):
        Settings.from_env()


def test_unknown_config_fields_are_rejected(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"typo_setting": True}), encoding="utf-8")
    monkeypatch.setenv("VESPER_CONFIG_PATH", str(config_path))

    with pytest.raises(CiderConfigError, match="Unknown config fields: typo_setting"):
        Settings.from_env()
