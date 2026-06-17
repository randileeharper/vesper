"""Low-level Cider RPC client."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .errors import CiderRpcError


class CiderRpcClient:
    """HTTP client wrapper around Cider's local RPC API."""

    def __init__(self, settings: Settings, session: httpx.Client | None = None) -> None:
        self._settings = settings
        self._session = session or httpx.Client(
            base_url=settings.cider_base_url,
            timeout=settings.request_timeout_seconds,
            verify=settings.verify_tls,
        )

    def close(self) -> None:
        self._session.close()

    def playback_get(self, path: str) -> Any:
        return self._request("GET", f"/api/v1/playback{path}")

    def playback_post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", f"/api/v1/playback{path}", json_body=body)

    def run_amapi_v3(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"path": path}
        if method != "GET":
            payload["method"] = method
        if body is not None:
            payload["body"] = body
        return self._request("POST", "/api/v1/amapi/run-v3", json_body=payload)

    def search_catalog(self, query: str, *, limit: int = 10, storefront: str = "us", offset: int = 0) -> Any:
        encoded_query = quote(query, safe="")
        path = f"/v1/catalog/{storefront}/search?term={encoded_query}&types=songs&limit={limit}"
        if offset:
            path = f"{path}&offset={offset}"
        return self.run_amapi_v3(path)

    def search_library(self, query: str, *, limit: int = 10, types: list[str] | None = None) -> Any:
        encoded_query = quote(query, safe="")
        search_types = types or ["library-songs", "library-albums", "library-artists", "library-playlists"]
        encoded_types = quote(",".join(search_types), safe=",")
        return self.run_amapi_v3(f"/v1/me/library/search?term={encoded_query}&types={encoded_types}&limit={limit}")

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        headers: dict[str, str] = {}
        if self._settings.cider_api_token:
            headers["apptoken"] = self._settings.cider_api_token
            headers["apitoken"] = self._settings.cider_api_token
        try:
            response = self._session.request(method, path, headers=headers, json=json_body)
        except httpx.HTTPError as exc:
            raise CiderRpcError(f"Could not reach Cider RPC at {self._settings.cider_base_url}: {exc}") from exc

        if response.status_code == 204:
            return None

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.is_error:
            detail = None
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or payload.get("error")
            raise CiderRpcError(
                f"Cider RPC returned HTTP {response.status_code} for {method} {path}.",
                status_code=response.status_code,
                detail=detail,
            )

        if payload is None:
            raise CiderRpcError(
                f"Cider RPC returned a non-JSON response for {method} {path}.",
                status_code=response.status_code,
            )
        return payload
