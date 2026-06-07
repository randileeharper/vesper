from __future__ import annotations

import pytest

from cider_agent import cli
from cider_agent.errors import TextRequestExecutionError


def test_call_local_a2a_waits_for_submitted_task(monkeypatch, settings) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    responses = [
        {
            "kind": "task",
            "id": "task-1",
            "status": {"state": "submitted"},
        },
        {
            "kind": "task",
            "id": "task-1",
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "artifactId": "artifact-1",
                    "parts": [{"kind": "data", "data": {"status": "ok"}}],
                }
            ],
        },
    ]

    def fake_post_local_a2a(*, method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return responses.pop(0)

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_post_local_a2a", fake_post_local_a2a)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    task = cli._call_local_a2a(
        {
            "kind": "message",
            "messageId": "m-1",
            "role": "user",
            "parts": [{"kind": "data", "data": {"action": "pause", "parameters": {}}}],
        }
    )

    assert task["status"]["state"] == "completed"
    assert calls == [
        ("message/send", {"message": {"kind": "message", "messageId": "m-1", "role": "user", "parts": [{"kind": "data", "data": {"action": "pause", "parameters": {}}}]}}),
        ("tasks/get", {"id": "task-1"}),
    ]


def test_call_local_a2a_raises_for_failed_submitted_task(monkeypatch, settings) -> None:
    responses = [
        {
            "kind": "task",
            "id": "task-2",
            "status": {"state": "submitted"},
        },
        {
            "kind": "task",
            "id": "task-2",
            "status": {
                "state": "failed",
                "message": {
                    "kind": "message",
                    "parts": [
                        {"kind": "text", "text": "No active session is running."},
                        {"kind": "data", "data": {"status": "error", "message": "No active session is running."}},
                    ],
                },
            },
            "artifacts": [],
        },
    ]

    def fake_post_local_a2a(*, method: str, params: dict[str, object]) -> dict[str, object]:
        return responses.pop(0)

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_post_local_a2a", fake_post_local_a2a)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    with pytest.raises(TextRequestExecutionError, match="No active session is running."):
        cli._call_local_a2a(
            {
                "kind": "message",
                "messageId": "m-2",
                "role": "user",
                "parts": [{"kind": "data", "data": {"action": "stop_session", "parameters": {}}}],
            }
        )
