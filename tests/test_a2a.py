from __future__ import annotations

import asyncio
import time
from typing import Any

from google.protobuf.json_format import MessageToDict
import httpx

from a2a.helpers import new_data_part, new_text_part
from a2a.types import CancelTaskRequest, GetTaskRequest, ListTasksRequest, Message, Role, SendMessageRequest
from a2a.utils.constants import PROTOCOL_VERSION_1_0, VERSION_HEADER

from cider_agent import a2a


def _headers() -> dict[str, str]:
    return {VERSION_HEADER: PROTOCOL_VERSION_1_0}


def _jsonrpc_envelope(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "test-request",
        "method": method,
        "params": params,
    }


def _text_message(text: str) -> Message:
    return Message(
        role=Role.ROLE_USER,
        message_id=f"msg-{text}",
        parts=[new_text_part(text, media_type="text/plain")],
    )


def _action_message(action: str, parameters: dict[str, Any] | None = None) -> Message:
    return Message(
        role=Role.ROLE_USER,
        message_id=f"msg-{action}",
        parts=[
            new_data_part(
                {"action": action, "parameters": parameters or {}},
                media_type="application/json",
            )
        ],
    )


def _send_message_payload(message: Message, *, return_immediately: bool = False) -> dict[str, Any]:
    return MessageToDict(
        SendMessageRequest(
            message=message,
            configuration={"return_immediately": return_immediately},
        ),
        preserving_proto_field_name=False,
    )


def _extract_data_parts(parts: list[dict[str, Any]]) -> dict[str, Any]:
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("data"), dict):
            return dict(part["data"])
    raise AssertionError("No data part found")


def _app(monkeypatch, service, settings):
    monkeypatch.setattr(a2a, "get_service", lambda: service)
    monkeypatch.setattr(a2a, "get_settings", lambda: settings)
    return a2a.create_a2a_app()


async def _request(app, method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def test_agent_card_is_published(monkeypatch, service, settings) -> None:
    response = asyncio.run(_request(_app(monkeypatch, service, settings), "GET", "/.well-known/agent-card.json"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert payload["supportedInterfaces"][0]["url"] == "http://127.0.0.1:8766/a2a"
    assert payload["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
    assert payload["skills"][0]["inputModes"] == ["text/plain"]


def test_agent_json_alias_is_removed(monkeypatch, service, settings) -> None:
    response = asyncio.run(_request(_app(monkeypatch, service, settings), "GET", "/.well-known/agent.json"))

    assert response.status_code == 404


def test_text_request_execution_through_jsonrpc(monkeypatch, service, settings) -> None:
    response = asyncio.run(
        _request(
            _app(monkeypatch, service, settings),
            "POST",
            "/a2a",
            headers=_headers(),
            json=_jsonrpc_envelope(
                "SendMessage",
                _send_message_payload(_text_message("what's playing?")),
            ),
        )
    )

    assert response.status_code == 200
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _extract_data_parts(task["artifacts"][0]["parts"])["status"] == "ok"
    assert task["metadata"]["action"] == "status"


def test_structured_public_action_execution_through_jsonrpc(monkeypatch, service, settings) -> None:
    response = asyncio.run(
        _request(
            _app(monkeypatch, service, settings),
            "POST",
            "/a2a",
            headers=_headers(),
            json=_jsonrpc_envelope(
                "SendMessage",
                _send_message_payload(_action_message("list_preferences")),
            ),
        )
    )

    assert response.status_code == 200
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["metadata"]["action"] == "list_preferences"
    assert _extract_data_parts(task["artifacts"][0]["parts"])["status"] == "ok"


def test_hidden_structured_actions_are_rejected(monkeypatch, service, settings) -> None:
    response = asyncio.run(
        _request(
            _app(monkeypatch, service, settings),
            "POST",
            "/a2a",
            headers=_headers(),
            json=_jsonrpc_envelope(
                "SendMessage",
                _send_message_payload(_action_message("status")),
            ),
        )
    )

    assert response.status_code == 200
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "not publicly exposed" in task["status"]["message"]["parts"][0]["text"]


def test_mutating_text_requests_return_tasks(monkeypatch, service, settings) -> None:
    response = asyncio.run(
        _request(
            _app(monkeypatch, service, settings),
            "POST",
            "/a2a",
            headers=_headers(),
            json=_jsonrpc_envelope(
                "SendMessage",
                _send_message_payload(
                    _text_message("play upbeat morning music"),
                    return_immediately=True,
                ),
            ),
        )
    )

    assert response.status_code == 200
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert task["id"]


def test_read_only_text_requests_complete_immediately(monkeypatch, service, settings) -> None:
    response = asyncio.run(
        _request(
            _app(monkeypatch, service, settings),
            "POST",
            "/a2a",
            headers=_headers(),
            json=_jsonrpc_envelope(
                "SendMessage",
                _send_message_payload(_text_message("what's playing?")),
            ),
        )
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_tasks_get_list_and_cancel(monkeypatch, service, settings) -> None:
    app = _app(monkeypatch, service, settings)

    async def _exercise() -> tuple[str, httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            send_response = await client.post(
                "/a2a",
                headers=_headers(),
                json=_jsonrpc_envelope(
                    "SendMessage",
                    _send_message_payload(
                        _action_message("play"),
                        return_immediately=True,
                    ),
                ),
            )
            task_id = send_response.json()["result"]["task"]["id"]
            get_response = await client.post(
                "/a2a",
                headers=_headers(),
                json=_jsonrpc_envelope(
                    "GetTask",
                    MessageToDict(GetTaskRequest(id=task_id), preserving_proto_field_name=False),
                ),
            )
            list_response = await client.post(
                "/a2a",
                headers=_headers(),
                json=_jsonrpc_envelope(
                    "ListTasks",
                    MessageToDict(ListTasksRequest(), preserving_proto_field_name=False),
                ),
            )
            cancel_response = await client.post(
                "/a2a",
                headers=_headers(),
                json=_jsonrpc_envelope(
                    "CancelTask",
                    MessageToDict(CancelTaskRequest(id=task_id), preserving_proto_field_name=False),
                ),
            )
        return task_id, get_response, list_response, cancel_response

    task_id, get_response, list_response, cancel_response = asyncio.run(_exercise())

    assert get_response.status_code == 200
    assert get_response.json()["result"]["id"] == task_id
    assert list_response.status_code == 200
    assert task_id in {task["id"] for task in list_response.json()["result"]["tasks"]}
    assert cancel_response.status_code == 200
    assert cancel_response.json()["result"]["status"]["state"] in {"TASK_STATE_CANCELED", "TASK_STATE_COMPLETED"}


def test_rest_message_send_matches_jsonrpc(monkeypatch, service, settings) -> None:
    payload = _send_message_payload(_action_message("list_preferences"))
    app = _app(monkeypatch, service, settings)

    async def _exercise() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            rpc_response = await client.post(
                "/a2a",
                headers=_headers(),
                json=_jsonrpc_envelope("SendMessage", payload),
            )
            rest_response = await client.post(
                "/message:send",
                headers=_headers(),
                json=payload,
            )
        return rpc_response, rest_response

    rpc_response, rest_response = asyncio.run(_exercise())

    assert rpc_response.status_code == 200
    assert rest_response.status_code == 200
    rpc_task = rpc_response.json()["result"]["task"]
    rest_task = rest_response.json()["task"]
    assert rpc_task["metadata"] == rest_task["metadata"]
    assert rpc_task["status"]["state"] == rest_task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _extract_data_parts(rpc_task["artifacts"][0]["parts"]) == _extract_data_parts(rest_task["artifacts"][0]["parts"])
