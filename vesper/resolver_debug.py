"""Resolver debug-episode logging for the Cider agent.

Extracted from the :class:`vesper.service.CiderAgentService` god-class
(issue #42). This class owns the resolver-debug-log file writer: it tracks
episode depth (nested begin/end), creates the log file, and appends
stage-tagged entries for resolver and session debugging.

The log path and timestamp are reached through the :class:`DebugLogHost`
Protocol so the logger never imports the concrete service class.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Protocol


class DebugLogHost(Protocol):
    """Structural interface for the cross-cutting capabilities
    :class:`ResolverDebugLogger` borrows from its host."""

    def resolver_debug_log_path(self) -> Any:
        ...

    def current_timestamp(self) -> str:
        ...


class ResolverDebugLogger:
    """Writes resolver/session debug log entries to a file, scoped by
    nested begin/end episodes."""

    def __init__(self, host: DebugLogHost) -> None:
        self._host = host
        self._lock = threading.Lock()
        self._episode_depth = 0

    def begin_episode(self, reason: str) -> bool:
        path = self._host.resolver_debug_log_path()
        if path is None:
            return False
        with self._lock:
            if self._episode_depth == 0:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"timestamp: {self._host.current_timestamp()}\nreason: {reason}\n\n",
                    encoding="utf-8",
                )
            self._episode_depth += 1
            return True

    def end_episode(self, started: bool) -> None:
        if not started:
            return
        with self._lock:
            self._episode_depth = max(0, self._episode_depth - 1)

    def append_resolver_log(
        self,
        *,
        stage: str,
        messages: list[dict[str, Any]],
        response_body: dict[str, Any],
        response_content: str,
    ) -> None:
        path = self._host.resolver_debug_log_path()
        if path is None:
            return
        entry = (
            f"=== {stage} ===\n"
            f"timestamp: {self._host.current_timestamp()}\n"
            "messages:\n"
            f"{json.dumps(messages, ensure_ascii=False, indent=2)}\n\n"
            "response_body:\n"
            f"{json.dumps(response_body, ensure_ascii=False, indent=2)}\n\n"
            "response_content:\n"
            f"{response_content}\n\n"
        )
        with self._lock:
            if self._episode_depth <= 0:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(entry)

    def append_session_log(self, *, stage: str, payload: dict[str, Any]) -> None:
        path = self._host.resolver_debug_log_path()
        if path is None:
            return
        entry = (
            f"=== {stage} ===\n"
            f"timestamp: {self._host.current_timestamp()}\n"
            "payload:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        )
        with self._lock:
            if self._episode_depth <= 0:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(entry)
