"""Stable engine result DTOs for Vesper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineActionResult:
    """Protocol-agnostic result for a single engine action."""

    action: str
    result: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "result": self.result,
        }


@dataclass
class TextRequestResult:
    """Protocol-agnostic result for a text request."""

    input: str
    resolver: str
    resolved_action: dict[str, Any]
    execution: EngineActionResult
    status: str = "ok"
    summary: str | None = None
    reasoning: str | None = None
    resolver_raw_content: str | None = None
    resolver_raw_action: dict[str, Any] | None = None
    timings: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "input": self.input,
            "resolver": self.resolver,
            "resolved_action": self.resolved_action,
            "execution": self.execution.as_dict(),
        }
        if self.summary is not None:
            payload["summary"] = self.summary
        if self.reasoning:
            payload["reasoning"] = self.reasoning
        if self.resolver_raw_content:
            payload["resolver_raw_content"] = self.resolver_raw_content
        if self.resolver_raw_action is not None:
            payload["resolver_raw_action"] = self.resolver_raw_action
        if self.timings is not None:
            payload["timings"] = self.timings
        if self.error is not None:
            payload["error"] = self.error
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload
