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
    def __init__(self) -> None:
        self.is_playing = True
        self.volume = 0.5
        self.current_track = self._track(
            "track-1",
            "Track",
            "Artist",
            "Album",
            is_library=True,
        )
        self.queue_items = [self._track("queued-track", "Queued", "Queued Artist", "Queued Album")]
        self.posts: list[dict[str, Any]] = []

    def _track(
        self,
        track_id: str,
        title: str,
        artist: str = "Artist",
        album: str = "Album",
        *,
        is_library: bool = False,
    ) -> dict[str, Any]:
        return {
            "id": track_id,
            "type": "songs",
            "attributes": {
                "name": title,
                "artistName": artist,
                "albumName": album,
                "playParams": {"id": track_id, "kind": "songs", "isLibrary": is_library},
                "durationInMillis": 180000,
            },
        }

    def close(self) -> None:
        return None

    def playback_get(self, path: str):
        if path == "/now-playing":
            return {"info": self.current_track["attributes"] if self.current_track is not None else {}}
        if path == "/queue":
            return self.queue_items
        if path == "/is-playing":
            return {"status": "ok", "is_playing": self.is_playing}
        if path == "/volume":
            return {"volume": self.volume}
        if path == "/repeat-mode":
            return {"value": 0}
        if path == "/shuffle-mode":
            return {"value": 0}
        if path == "/autoplay":
            return {"value": False}
        return {"value": True}

    def playback_post(self, path: str, body=None):
        self.posts.append({"path": path, "body": body})
        if path == "/pause":
            self.is_playing = False
        elif path == "/play":
            self.is_playing = True
        elif path == "/stop":
            self.is_playing = False
        elif path == "/next":
            self.is_playing = False
            self.current_track = None
        elif path == "/volume" and isinstance(body, dict):
            self.volume = body.get("volume", self.volume)
        elif path == "/play-item" and isinstance(body, dict):
            item_id = str(body.get("id", "")).strip()
            self.current_track = self._catalog_track_for_id(item_id)
            self.is_playing = True
        elif path == "/queue/clear-queue":
            self.queue_items = []
        return {"path": path, "body": body}

    def _catalog_track_for_id(self, item_id: str) -> dict[str, Any]:
        catalog_map = {
            "catalog-track-favorite": self._track("catalog-track-favorite", "Liked Song", "Favorite Artist"),
            "catalog-track-2": self._track("catalog-track-2", "Another Song", "Favorite Artist"),
            "catalog-track-3": self._track("catalog-track-3", "Third Song", "Favorite Artist"),
            "catalog-track-1": self._track("catalog-track-1", "k-pop", "Catalog Artist"),
        }
        return catalog_map.get(item_id, self._track(item_id or "unknown-track", item_id or "Unknown"))

    def search_catalog(self, query: str, *, limit: int, storefront: str):
        if query == "Favorite Artist Liked Song":
            return {
                "data": {
                    "results": {
                        "songs": {
                            "data": [
                                {
                                    "id": "catalog-track-favorite",
                                    "type": "songs",
                                    "attributes": {
                                        "name": "Liked Song",
                                        "artistName": "Favorite Artist",
                                        "playParams": {"id": "catalog-track-favorite", "kind": "songs", "isLibrary": False},
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        if query == "Favorite Artist Another Song":
            return {
                "data": {
                    "results": {
                        "songs": {
                            "data": [
                                {
                                    "id": "catalog-track-2",
                                    "type": "songs",
                                    "attributes": {
                                        "name": "Another Song",
                                        "artistName": "Favorite Artist",
                                        "albumName": "Album",
                                        "playParams": {"id": "catalog-track-2", "kind": "songs", "isLibrary": False},
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        if query == "Favorite Artist Third Song":
            return {
                "data": {
                    "results": {
                        "songs": {
                            "data": [
                                {
                                    "id": "catalog-track-3",
                                    "type": "songs",
                                    "attributes": {
                                        "name": "Third Song",
                                        "artistName": "Favorite Artist",
                                        "albumName": "Album",
                                        "playParams": {"id": "catalog-track-3", "kind": "songs", "isLibrary": False},
                                    },
                                }
                            ]
                        }
                    }
                }
            }
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
    def __init__(self) -> None:
        self.session_plan_calls = 0

    def resolve(self, text: str, service: Any) -> ResolvedAction:
        normalized = text.strip().lower()
        if "kep1er" in normalized:
            return ResolvedAction(action="search", parameters={"query": "kep1er", "limit": 3, "storefront": "us"}, resolver="stub")
        if "pink" in normalized:
            return ResolvedAction(
                action="play_candidate_match",
                parameters={
                    "candidate_tracks": [{"title": "Just Give Me a Reason", "artist": "P!nk"}],
                    "candidate_artists": ["P!nk"],
                    "candidate_queries": ["Pink"],
                },
                resolver="stub",
            )
        if "upbeat" in normalized or "morning" in normalized:
            return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")
        if "more pop" in normalized:
            return ResolvedAction(action="steer_session", parameters={"request": text}, resolver="stub")
        return ResolvedAction(action="status", parameters={}, resolver="stub")

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int):
        self.session_plan_calls += 1
        if self.session_plan_calls == 1:
            candidate_tracks = [{"title": "Liked Song", "artist": "Favorite Artist"}]
        elif self.session_plan_calls == 2:
            candidate_tracks = [{"title": "Another Song", "artist": "Favorite Artist"}]
        else:
            candidate_tracks = [{"title": "Third Song", "artist": "Favorite Artist"}]
        return type(
            "Plan",
            (),
            {
                "candidate_tracks": candidate_tracks,
                "candidate_artists": ["Favorite Artist"],
                "candidate_queries": [request],
                "resolver": "stub",
                "raw": None,
                "reasoning": None,
                "raw_content": None,
            },
        )()


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
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
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
