from __future__ import annotations

import json

from vesper.config import Settings


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
    assert settings.global_recent_tracks_limit == 34
    assert settings.database_path == tmp_path / "db.sqlite3"
