from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cider_agent.config import Settings
from cider_agent.rpc import CiderRpcClient
from cider_agent.resolver import ResolvedAction
from cider_agent.service import CiderAgentService
from cider_agent.storage import PreferenceStore


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responder: Callable[[str, str, dict[str, str], Any], FakeResponse]) -> None:
        self._responder = responder
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, path: str, headers: dict[str, str], json: Any = None) -> FakeResponse:
        self.requests.append({"method": method, "path": path, "headers": headers, "json": json})
        return self._responder(method, path, headers, json)

    def close(self) -> None:
        return None


class StubRpcClient:
    def close(self) -> None:
        return None

    def playback_get(self, path: str):
        if path == "/now-playing":
            return {
                "info": {
                    "name": "Track",
                    "artistName": "Artist",
                    "albumName": "Album",
                    "playParams": {"id": "track-1", "kind": "songs", "isLibrary": True},
                }
            }
        if path == "/queue":
            return [{"id": "queued-track", "attributes": {"name": "Queued"}}]
        if path == "/is-playing":
            return {"status": "ok", "is_playing": True}
        if path == "/volume":
            return {"volume": 0.5}
        if path == "/repeat-mode":
            return {"value": 0}
        if path == "/shuffle-mode":
            return {"value": 0}
        if path == "/autoplay":
            return {"value": False}
        return {"value": True}

    def playback_post(self, path: str, body=None):
        return {"path": path, "body": body}

    def search_catalog(self, query: str, *, limit: int, storefront: str):
        return {
            "data": {
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "catalog-track-1",
                                "type": "songs",
                                "attributes": {
                                    "name": query,
                                    "artistName": "Catalog Artist",
                                    "playParams": {"id": "catalog-track-1", "kind": "songs", "isLibrary": False},
                                },
                            }
                        ]
                    }
                }
            }
        }

    def search_library(self, query: str, *, limit: int, types: list[str] | None = None):
        return {
            "data": {
                "results": {
                    "library-songs": {
                        "data": [
                            {
                                "id": "library-track-1",
                                "type": "library-songs",
                                "attributes": {
                                    "name": query,
                                    "artistName": "Library Artist",
                                    "playParams": {"id": "library-track-1", "kind": "songs", "isLibrary": True},
                                },
                            }
                        ]
                    },
                    "library-playlists": {"data": [{"id": "playlist-1", "type": "library-playlists", "attributes": {"name": "Mix"}}]},
                    "library-albums": {"data": [{"id": "album-1", "type": "library-albums", "attributes": {"name": "Album"}}]},
                    "library-artists": {"data": [{"id": "artist-1", "type": "library-artists", "attributes": {"name": "Artist"}}]},
                }
            }
        }

    def run_amapi_v3(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None):
        if path.startswith("/v1/me/library/search?") and "library-songs" in path:
            return {
                "data": {
                    "results": {
                        "library-songs": {
                            "data": [
                                {
                                    "id": "library-track-1",
                                    "type": "library-songs",
                                    "attributes": {
                                        "name": "Liked Song",
                                        "artistName": "Favorite Artist",
                                        "playParams": {"id": "library-track-1", "kind": "songs", "isLibrary": True},
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        if path.startswith("/v1/me/library/playlists?"):
            return {
                "data": {
                    "data": [
                        {"id": "playlist-1", "type": "library-playlists", "attributes": {"name": "Mix"}},
                    ]
                }
            }
        if "/tracks?limit=" in path:
            return {
                "data": {
                    "data": [
                        {
                            "id": "track-1",
                            "type": "library-songs",
                            "attributes": {
                                "name": "Playlist Track",
                                "artistName": "Artist",
                                "playParams": {"id": "track-1", "kind": "songs", "isLibrary": True},
                            },
                        }
                    ]
                }
            }
        if path.startswith("/v1/me/recent/played/tracks"):
            return {"data": {"data": [{"id": "recent-1", "type": "library-songs", "attributes": {"name": "Recent"}}]}}
        return {"data": {"data": [{"id": "playlist-1", "type": "library-playlists", "attributes": {"name": "Mix"}}]}}


class StubResolver:
    def resolve(self, text: str, service: Any) -> ResolvedAction:
        normalized = text.strip().lower()
        if "kep1er" in normalized:
            return ResolvedAction(
                action="search",
                parameters={"query": "kep1er", "limit": 3, "storefront": "us"},
                resolver="stub",
            )
        return ResolvedAction(action="status", parameters={}, resolver="stub")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        http_host="127.0.0.1",
        http_port=8766,
        public_base_url="http://127.0.0.1:8766",
        cider_base_url="http://localhost:10767",
        cider_api_token="secret-token",
        default_search_source="catalog",
        resolver_backend="fallback",
        resolver_base_url="https://api.openai.com/v1",
        resolver_model=None,
        resolver_api_key=None,
        request_timeout_seconds=10.0,
        verify_tls=True,
        log_level="INFO",
        database_path=tmp_path / "cider-agent.db",
        config_path=None,
    )


@pytest.fixture
def service(settings: Settings) -> CiderAgentService:
    return CiderAgentService(
        settings,
        rpc_client=StubRpcClient(),
        preference_store=PreferenceStore(settings.database_path),
        resolver=StubResolver(),
    )


@pytest.fixture
def rpc_client(settings: Settings):
    session = FakeSession(lambda method, path, headers, body: FakeResponse(200, {"ok": True}))
    return CiderRpcClient(settings, session=session), session
