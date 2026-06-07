"""Error types used by cider_agent."""

from __future__ import annotations


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
