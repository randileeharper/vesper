"""Application-local Historian event delivery."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
import time
from typing import Any, Iterator, Protocol
from uuid import uuid4

import httpx

from .config import Settings


class HistorianDeliveryError(Exception):
    """Raised when an event could not be delivered to Historian."""


class HistorianSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...
    def emit_batch(self, events: list[dict[str, Any]]) -> None: ...
    def close(self) -> None: ...


class NullHistorianSink:
    def emit(self, event: dict[str, Any]) -> None:
        return None

    def emit_batch(self, events: list[dict[str, Any]]) -> None:
        return None

    def close(self) -> None:
        return None


class FakeHistorianSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def emit_batch(self, events: list[dict[str, Any]]) -> None:
        self.events.extend(events)

    def close(self) -> None:
        return None


class HttpHistorianSink:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        if not settings.historian_token:
            raise ValueError("Historian token is required.")
        self._token = settings.historian_token
        self._retry_count = settings.historian_retry_count
        self._sleep = sleep
        self._client = client or httpx.Client(
            base_url=settings.historian_base_url,
            timeout=settings.historian_timeout_seconds,
            verify=settings.historian_verify_tls,
        )

    def emit(self, event: dict[str, Any]) -> None:
        self._request("/v1/events", event)

    def emit_batch(self, events: list[dict[str, Any]]) -> None:
        self._request("/v1/events:batch", {"events": events})

    def close(self) -> None:
        self._client.close()

    def _request(self, path: str, payload: dict[str, Any]) -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        last_error: Exception | None = None
        for attempt in range(self._retry_count + 1):
            try:
                response = self._client.post(path, headers=headers, json=payload)
                if response.status_code >= 500:
                    last_error = HistorianDeliveryError(
                        f"Historian returned HTTP {response.status_code} for POST {path}."
                    )
                    if attempt < self._retry_count:
                        self._sleep(0.1 * (2**attempt))
                        continue
                    raise last_error
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise HistorianDeliveryError("Historian returned a non-object response.")
                return
            except httpx.RequestError as exc:
                last_error = exc
                if attempt < self._retry_count:
                    self._sleep(0.1 * (2**attempt))
                    continue
                break
            except (httpx.HTTPStatusError, ValueError, HistorianDeliveryError):
                raise
        raise HistorianDeliveryError(f"Historian request failed: {last_error}") from last_error


def build_historian_sink(settings: Settings) -> HistorianSink:
    if not settings.historian_enabled:
        return NullHistorianSink()
    return HttpHistorianSink(settings)


@dataclass(frozen=True)
class OperationContext:
    correlation_id: str
    caller: str
    causation_id: str | None = None
    session_id: str | None = None


_OPERATION: ContextVar[OperationContext | None] = ContextVar("vesper_historian_operation", default=None)


def current_operation() -> OperationContext | None:
    return _OPERATION.get()


@contextmanager
def operation_context(
    *,
    caller: str = "direct",
    correlation_id: str | None = None,
    causation_id: str | None = None,
    session_id: str | None = None,
) -> Iterator[OperationContext]:
    existing = current_operation()
    if existing is not None:
        yield existing
        return
    context = OperationContext(
        correlation_id=correlation_id or str(uuid4()),
        caller=caller,
        causation_id=causation_id,
        session_id=session_id,
    )
    token = _OPERATION.set(context)
    try:
        yield context
    finally:
        _OPERATION.reset(token)


def replace_operation(**updates: Any) -> Any:
    current = current_operation()
    if current is None:
        return None
    values: dict[str, Any] = {
        "correlation_id": current.correlation_id,
        "caller": current.caller,
        "causation_id": current.causation_id,
        "session_id": current.session_id,
    }
    values.update(updates)
    return _OPERATION.set(OperationContext(**values))


def reset_operation(token: Any) -> None:
    if token is not None:
        _OPERATION.reset(token)


def build_event(
    event_type: str,
    data: dict[str, Any],
    *,
    source: str,
    subject: str | None = None,
    event_id: str | None = None,
    occurred_at: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    context = current_operation()
    event: dict[str, Any] = {
        "specversion": "1.0",
        "id": event_id or str(uuid4()),
        "source": source,
        "type": event_type,
        "time": occurred_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "schemaversion": 1,
        "visibility": "private",
        "data": data,
    }
    resolved_correlation = correlation_id or (context.correlation_id if context else None)
    resolved_causation = causation_id or (context.causation_id if context else None)
    resolved_session = session_id or (context.session_id if context else None)
    if subject is not None:
        event["subject"] = subject
    if resolved_correlation is not None:
        event["correlationid"] = resolved_correlation
    if resolved_causation is not None:
        event["causationid"] = resolved_causation
    if resolved_session is not None:
        event["sessionid"] = str(resolved_session)
    return event
