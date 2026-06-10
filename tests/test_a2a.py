from __future__ import annotations

import pytest

from cider_agent import a2a


def test_execute_message_returns_completed_task(monkeypatch, service) -> None:
    monkeypatch.setattr(a2a, "get_service", lambda: service)

    task = a2a._execute_message(
        {
            "kind": "message",
            "messageId": "m-1",
            "role": "user",
            "parts": [{"kind": "data", "data": {"action": "list_preferences", "parameters": {}}}],
        }
    )

    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["metadata"]["action"] == "list_preferences"
    assert "summary" in task["metadata"]


def test_execute_text_message_uses_resolver(monkeypatch, service) -> None:
    monkeypatch.setattr(a2a, "get_service", lambda: service)

    task = a2a._execute_message(
        {
            "kind": "message",
            "messageId": "m-2",
            "role": "user",
            "parts": [{"kind": "text", "text": "play some kep1er"}],
        }
    )

    assert task["metadata"]["action"] == "search"
    assert task["metadata"]["resolver"] == "stub"
    assert "summary" in task["metadata"]


def test_agent_card_is_published(monkeypatch) -> None:
    monkeypatch.setattr(
        a2a,
        "get_settings",
        lambda: type(
            "SettingsStub",
            (),
            {"public_base_url": "http://127.0.0.1:8766"},
        )(),
    )

    payload = a2a.build_agent_card()

    assert payload["protocolVersion"] == "1.0.0"
    assert payload["url"] == "http://127.0.0.1:8766/a2a"
    assert "plain-language requests" in payload["description"]
    assert payload["skills"][0]["inputModes"] == ["text/plain"]


def test_should_defer_message_for_mutating_text_request() -> None:
    message = {
        "kind": "message",
        "messageId": "m-3",
        "role": "user",
        "parts": [{"kind": "text", "text": "play upbeat morning music"}],
    }

    assert a2a._should_defer_message(message) is True


def test_should_not_defer_message_for_status_text_request() -> None:
    message = {
        "kind": "message",
        "messageId": "m-4",
        "role": "user",
        "parts": [{"kind": "text", "text": "what's playing?"}],
    }

    assert a2a._should_defer_message(message) is False


def test_should_defer_message_for_mutating_structured_action() -> None:
    message = {
        "kind": "message",
        "messageId": "m-5",
        "role": "user",
        "parts": [{"kind": "data", "data": {"action": "play_session", "parameters": {"request": "play upbeat music"}}}],
    }

    assert a2a._should_defer_message(message) is True


def test_execute_message_rejects_hidden_structured_action(monkeypatch, service) -> None:
    monkeypatch.setattr(a2a, "get_service", lambda: service)

    with pytest.raises(Exception, match="not publicly exposed"):
        a2a._execute_message(
            {
                "kind": "message",
                "messageId": "m-hidden",
                "role": "user",
                "parts": [{"kind": "data", "data": {"action": "status", "parameters": {}}}],
            }
        )


def test_resolve_defer_mode_allows_explicit_override() -> None:
    message = {
        "kind": "message",
        "messageId": "m-5b",
        "role": "user",
        "parts": [{"kind": "text", "text": "play upbeat morning music"}],
    }

    assert a2a._resolve_defer_mode(message, {"defer": False}) is False
    assert a2a._resolve_defer_mode(message, {"defer": True}) is True


def test_complete_submitted_task_updates_state() -> None:
    request = {
        "kind": "message",
        "messageId": "m-6",
        "role": "user",
        "parts": [{"kind": "text", "text": "play something upbeat"}],
    }
    task = a2a._submitted_task(task_id="task-1", context_id="ctx-1", request_message=request)
    completed = a2a._complete_submitted_task(
        task,
        {
            "kind": "task",
            "id": "task-1",
            "contextId": "ctx-1",
            "status": {"state": "completed", "timestamp": "2026-06-07T00:00:00Z"},
            "artifacts": [{"artifactId": "artifact-1", "name": "cider-agent-result", "parts": [{"kind": "data", "data": {"status": "ok"}}]}],
            "history": [request],
            "metadata": {"action": "play_session", "summary": "playing something upbeat"},
        },
    )

    assert completed["status"]["state"] == "completed"
    assert completed["metadata"]["action"] == "play_session"
