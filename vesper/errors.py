"""Error types used by Vesper."""

from __future__ import annotations

from typing import Any


class CiderAgentError(Exception):
    """Base error for the project."""


class CiderConfigError(CiderAgentError):
    """Raised when configuration is invalid."""


class CiderRpcError(CiderAgentError):
    """Raised when the Cider RPC endpoint fails."""

    def __init__(self, message: str, status_code: int | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class CiderValidationError(CiderAgentError):
    """Raised when an input is invalid."""


class PreferenceStoreError(CiderAgentError):
    """Raised when local persistence fails."""


class ResolverError(CiderAgentError):
    """Raised when the text-to-action resolver fails."""


class TextRequestExecutionError(CiderAgentError):
    """Raised when a resolved text request fails during execution."""

    def __init__(self, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = payload
