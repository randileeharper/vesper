"""Historian event emission and operation context for the Cider agent.

Extracted from the :class:`vesper.service.CiderAgentService` god-class
(issue #42). This class owns historian event construction, secret sanitization,
RPC-failure and error recording, and the operation-context manager.

The historian sink, settings (for secrets), and operation-context functions
are reached through the :class:`EventHost` Protocol so the emitter never
imports the concrete service class.
"""

from __future__ import annotations

from typing import Any, Protocol

from .historian import (
    HistorianDeliveryError,
    HistorianSink,
    NullHistorianSink,
    build_event,
    current_operation,
    operation_context,
)


class EventHost(Protocol):
    """Structural interface for the cross-cutting capabilities
    :class:`EventEmitter` borrows from its host."""

    @property
    def _settings(self) -> Any:
        ...

    @property
    def _historian(self) -> HistorianSink:
        ...

    def _track_payload(self, track: dict[str, Any] | None) -> dict[str, Any] | None:
        ...

    def _preference_target(self, preference: dict[str, Any] | None) -> dict[str, Any] | None:
        ...


class EventEmitter:
    """Historian event emission, secret sanitization, and error recording."""

    def __init__(self, host: EventHost, historian: HistorianSink | None = None) -> None:
        self._host = host
        self._historian = historian or NullHistorianSink()

    @property
    def sink(self) -> HistorianSink:
        return self._historian

    def operation(
        self,
        *,
        caller: str = "direct",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        session_id: str | None = None,
    ):
        return operation_context(
            caller=caller,
            correlation_id=correlation_id,
            causation_id=causation_id,
            session_id=session_id,
        )

    def emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        source: str,
        subject: str | None = None,
        session_id: int | str | None = None,
    ) -> str:
        event = build_event(
            event_type,
            self._sanitize_event_data(data),
            source=source,
            subject=subject,
            session_id=str(session_id) if session_id is not None else None,
        )
        try:
            self._historian.emit(event)
        except HistorianDeliveryError as exc:
            # Historian delivery is best-effort: a failed delivery must never
            # fail the surrounding operation. Only the documented delivery
            # failure is swallowed (with a warning); unexpected sink failures
            # propagate so they remain visible and actionable.
            import logging

            logging.getLogger(__name__).warning(
                "Historian delivery failed for event_id=%s type=%s: %s",
                event["id"],
                event_type,
                exc,
            )
        return str(event["id"])

    def _sanitize_event_data(self, value: Any) -> Any:
        secrets = [
            secret
            for secret in (
                self._host._settings.cider_api_token,
                self._host._settings.resolver_api_key,
                self._host._settings.historian_token,
            )
            if secret
        ]
        if isinstance(value, str):
            sanitized = value
            for secret in secrets:
                sanitized = sanitized.replace(secret, "[REDACTED]")
            return sanitized
        if isinstance(value, dict):
            return {str(key): self._sanitize_event_data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize_event_data(item) for item in value]
        if isinstance(value, tuple):
            return [self._sanitize_event_data(item) for item in value]
        return value

    def caller(self) -> str:
        context = current_operation()
        return context.caller if context is not None else "direct"

    def record_rpc_failure(self, failure: dict[str, Any]) -> None:
        self.emit(
            "music.rpc.failed",
            {
                "caller": self.caller(),
                "operation": str(failure.get("operation", "")),
                "status_code": failure.get("status_code"),
                "error": str(failure.get("message", "")),
            },
            source="app://vesper/rpc",
        )

    def record_error(self, component: str, operation: str | None, exc: Exception) -> None:
        self.emit(
            "core.operation.error",
            {
                "app_id": "vesper",
                "component": component,
                "error_type": exc.__class__.__name__,
                "message": str(exc),
                "operation": operation,
                "details": {"caller": self.caller()},
            },
            source=f"app://vesper/{component}",
        )
