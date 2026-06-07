from __future__ import annotations

from cider_agent import a2a


def test_execute_message_returns_completed_task(monkeypatch, service) -> None:
    monkeypatch.setattr(a2a, "get_service", lambda: service)

    task = a2a._execute_message(
        {
            "kind": "message",
            "messageId": "m-1",
            "role": "user",
            "parts": [{"kind": "data", "data": {"action": "status", "parameters": {}}}],
        }
    )

    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["metadata"]["action"] == "status"


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
