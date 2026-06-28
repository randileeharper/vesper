"""Text-request orchestration for the Cider agent.

Extracted from the :class:`vesper.service.CiderAgentService` god-class
(issue #42). This controller owns the resolve -> execute -> render pipeline
for natural-language text requests, plus action dispatch
(execute_action/run_action) and the action-definition listing.

The resolve/execute/render flow orchestrates with operation contexts,
historian emission, resolver-debug episodes, and timing instrumentation.
Cross-cutting capabilities are reached through the
:class:`TextRequestHost` Protocol so the controller never imports the
concrete service class. Action executors receive the host as their
``service`` argument, preserving the existing ``lambda service, params:
service.<method>()`` dispatch.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from .action_registry import (
    get_action_definition,
    list_action_definitions,
    list_public_action_definitions,
)
from .errors import CiderAgentError, CiderValidationError, TextRequestExecutionError
from .historian import replace_operation, reset_operation
from .output import compact_resolved_action, finalize_output, summarize_execution
from .resolver import ResolvedAction, Resolver
from .results import EngineActionResult, TextRequestResult
from .utils import _elapsed_ms


class TextRequestHost(Protocol):
    """Structural interface for the cross-cutting capabilities
    :class:`TextRequestController` borrows from its host."""

    _resolver: Resolver

    def operation(
        self,
        *,
        caller: str = "direct",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        session_id: str | None = None,
    ):
        ...

    def _emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        source: str,
        subject: str | None = None,
        session_id: int | str | None = None,
    ) -> str:
        ...

    def _caller(self) -> str:
        ...

    def _record_error(self, component: str, operation: str | None, exc: Exception) -> None:
        ...

    def response_detail(self) -> str:
        ...

    def include_timing_debug(self) -> bool:
        ...

    def _begin_resolver_debug_episode(self, reason: str) -> bool:
        ...

    def _end_resolver_debug_episode(self, started: bool) -> None:
        ...

    @property
    def _settings(self) -> Any:
        ...


class TextRequestController:
    """Resolve -> execute -> render pipeline and action dispatch."""

    def __init__(self, host: TextRequestHost) -> None:
        self._host = host

    def list_action_definitions(
        self, *, text_exposable_only: bool = False, public_only: bool = True
    ) -> list[dict[str, Any]]:
        definitions_source = (
            list_public_action_definitions()
            if public_only and not text_exposable_only
            else list_action_definitions(text_exposable_only=text_exposable_only)
        )
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "summary_label": definition.summary_label,
                "parameter_schema": definition.parameter_schema,
                "required_fields": list(definition.required_fields),
                "read_only": definition.read_only,
                "text_exposable": definition.text_exposable,
                "public_exposed": definition.public_exposed,
                "session_aware": definition.session_aware,
                "deferred_a2a_eligible": definition.deferred_a2a_eligible,
                "advanced_only": definition.advanced_only,
            }
            for definition in definitions_source
        ]

    def resolve_text_request(self, text: str) -> ResolvedAction:
        return self._host._resolver.resolve(text, self._host)

    def execute_text_request(self, text: str) -> TextRequestResult:
        with self._host.operation():
            request_event_id = self._host._emit(
                "music.request.received",
                {
                    "caller": self._host._caller(),
                    "request": text,
                    "resolved_action": None,
                },
                source="app://vesper/request",
            )
            operation_token = replace_operation(causation_id=request_event_id)
            try:
                return self._execute_text_request(text)
            finally:
                reset_operation(operation_token)

    def _execute_text_request(self, text: str) -> TextRequestResult:
        try:
            started_debug_episode = self._host._begin_resolver_debug_episode(f"text-request: {text.strip()}")
            started_at = time.perf_counter()
            resolve_started_at = time.perf_counter()
            resolved = self.resolve_text_request(text)
            resolve_ms = _elapsed_ms(resolve_started_at)
            resolved_action = compact_resolved_action(
                resolved.action, resolved.parameters, self._host.response_detail()
            )
            execute_started_at = time.perf_counter()
            try:
                execution = self.execute_action(resolved.action, resolved.parameters)
            except CiderAgentError as exc:
                self._host._record_error("service", resolved.action, exc)
                error = {"type": exc.__class__.__name__, "message": str(exc)}
                timings = None
                if self._host.include_timing_debug():
                    timings = {
                        "resolve_ms": resolve_ms,
                        "execute_ms": _elapsed_ms(execute_started_at),
                        "total_ms": _elapsed_ms(started_at),
                    }
                failure = TextRequestResult(
                    status="error",
                    input=text,
                    resolver=resolved.resolver,
                    resolved_action=resolved_action,
                    execution=EngineActionResult(action=resolved.action, result={}),
                    reasoning=resolved.reasoning,
                    resolver_raw_content=resolved.raw_content,
                    resolver_raw_action=resolved.raw
                    if self._host._settings.resolver_include_raw_output
                    else None,
                    timings=timings,
                    error=error,
                )
                raise TextRequestExecutionError(str(exc), failure.as_dict()) from exc
            summary = summarize_execution(execution.as_dict())
            timings = None
            if self._host.include_timing_debug():
                timings = {
                    "resolve_ms": resolve_ms,
                    "execute_ms": _elapsed_ms(execute_started_at),
                    "total_ms": _elapsed_ms(started_at),
                }
            return TextRequestResult(
                input=text,
                resolver=resolved.resolver,
                resolved_action=resolved_action,
                execution=execution,
                summary=summary,
                reasoning=resolved.reasoning,
                resolver_raw_content=resolved.raw_content,
                resolver_raw_action=resolved.raw
                if self._host._settings.resolver_include_raw_output
                else None,
                timings=timings,
            )
        except CiderAgentError as exc:
            if not isinstance(exc, TextRequestExecutionError):
                self._host._record_error("resolver", "resolve_text_request", exc)
            raise
        finally:
            self._host._end_resolver_debug_episode(locals().get("started_debug_episode", False))

    def handle_text_request(self, text: str) -> dict[str, Any]:
        return self.execute_text_request(text).as_dict()

    def execute_action(self, action: str, parameters: dict[str, Any] | None = None) -> EngineActionResult:
        params = parameters or {}
        definition = get_action_definition(action)
        if definition is None:
            raise CiderValidationError(f"Unsupported action: {action}")
        try:
            result = definition.executor(self._host, params)
        except (KeyError, TypeError, ValueError) as exc:
            self._host._record_error("service", action, exc)
            raise CiderValidationError(f"Invalid parameters for action {action}: {exc}") from exc
        return EngineActionResult(
            action=action,
            result=finalize_output(
                result, self._host.response_detail(), self._host.include_timing_debug()
            ),
        )

    def run_action(self, action: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.execute_action(action, parameters).as_dict()
