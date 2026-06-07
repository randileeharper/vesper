"""Minimal A2A-compatible HTTP server for cider_agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .app import get_service, get_settings
from .errors import CiderAgentError, CiderValidationError


def _iso_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _text_part(text: str) -> dict[str, Any]:
    return {"kind": "text", "text": text}


def _data_part(data: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "data", "data": data}


def _message(role: str, parts: list[dict[str, Any]], *, task_id: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": role,
        "parts": parts,
    }
    if task_id:
        message["taskId"] = task_id
    return message


def _artifact(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifactId": str(uuid.uuid4()),
        "name": "cider-agent-result",
        "parts": [_data_part(payload)],
    }


@dataclass
class TaskStore:
    """In-memory task snapshots used by the A2A transport."""

    tasks: dict[str, dict[str, Any]]

    def __init__(self) -> None:
        self.tasks = {}

    def save(self, task: dict[str, Any]) -> dict[str, Any]:
        self.tasks[task["id"]] = task
        return task

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id)

    def list(self) -> list[dict[str, Any]]:
        return list(self.tasks.values())


TASK_STORE = TaskStore()


def build_agent_card() -> dict[str, Any]:
    settings = get_settings()
    return {
        "protocolVersion": "1.0.0",
        "name": "Cider Agent",
        "description": "A dedicated audio control agent for the Cider Apple Music client.",
        "url": f"{settings.public_base_url}/a2a",
        "preferredTransport": "JSONRPC",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": "playback-control",
                "name": "Playback Control",
                "description": "Play, pause, seek, skip, and inspect current playback state in Cider.",
                "tags": ["audio", "playback", "music"],
                "examples": ["Pause playback", "Skip to the next track", "Set volume to 35"],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["application/json", "text/plain"],
            },
            {
                "id": "queue-management",
                "name": "Queue Management",
                "description": "Inspect and modify the Cider queue.",
                "tags": ["queue", "playlist", "music"],
                "examples": ["Show queue", "Remove queue item 3", "Move queue item 5 to 1"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "library-and-playlists",
                "name": "Library and Playlists",
                "description": "Search the Apple Music catalog and library, browse playlists, and create playlists.",
                "tags": ["search", "library", "playlists", "apple-music"],
                "examples": ["Search my library for k-pop", "Create playlist Late Night Mix", "Add tracks to playlist"],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json"],
            },
            {
                "id": "preference-memory",
                "name": "Preference Memory",
                "description": "Store and retrieve explicit listening preferences for later recommendations.",
                "tags": ["preferences", "recommendations", "memory"],
                "examples": ["Remember that I like k-pop", "List my music preferences", "Play something I like"],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json", "text/plain"],
            },
        ],
    }


def _jsonrpc_response(request_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return JSONResponse(payload)


def _jsonrpc_error(code: int, message: str, data: Any = None) -> dict[str, Any]:
    payload = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return payload


def _extract_action(message: dict[str, Any], service: Any) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    parts = message.get("parts", [])
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            payload = dict(part["data"])
            action = str(payload.get("action", "")).strip()
            if not action:
                raise CiderValidationError("Data part must include a non-empty action.")
            parameters = payload.get("parameters", {})
            if parameters is None:
                parameters = {}
            if not isinstance(parameters, dict):
                raise CiderValidationError("parameters must be an object.")
            return action, parameters, None
        if part.get("kind") == "text":
            resolved = service.handle_text_request(str(part.get("text", "")))
            execution = resolved["execution"]
            return execution["action"], execution["result"], resolved
    raise CiderValidationError("Message did not include a supported text or data part.")


def _task_from_result(
    *,
    task_id: str,
    context_id: str,
    request_message: dict[str, Any],
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": "completed",
            "timestamp": _iso_now(),
            "message": _message(
                "agent",
                [_text_part(f"Completed action '{action}'."), _data_part(payload)],
                task_id=task_id,
            ),
        },
        "artifacts": [_artifact(payload)],
        "history": [
            request_message,
            _message("agent", [_text_part(f"Completed action '{action}'."), _data_part(payload)], task_id=task_id),
        ],
        "metadata": {"action": action},
    }


def _execute_message(message: dict[str, Any], *, task_id: str | None = None, context_id: str | None = None) -> dict[str, Any]:
    service = get_service()
    action, parameters, resolved = _extract_action(message, service)
    payload = parameters if resolved is not None else service.run_action(action, parameters)
    resolved_task_id = task_id or str(uuid.uuid4())
    resolved_context_id = context_id or str(uuid.uuid4())
    task = _task_from_result(
        task_id=resolved_task_id,
        context_id=resolved_context_id,
        request_message=message,
        action=action,
        payload=payload,
    )
    if resolved is not None:
        task["metadata"]["resolver"] = resolved["resolver"]
        task["metadata"]["resolved_action"] = resolved["resolved_action"]
    TASK_STORE.save(task)
    return task


def create_a2a_app() -> FastAPI:
    app = FastAPI(title="Cider Agent", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/.well-known/agent.json")
    async def agent_card_json() -> dict[str, Any]:
        return build_agent_card()

    @app.get("/.well-known/agent-card.json")
    async def agent_card_alias() -> dict[str, Any]:
        return build_agent_card()

    @app.post("/a2a")
    async def a2a_rpc(request: Request):
        body = await request.json()
        request_id = body.get("id")
        method = body.get("method")
        params = body.get("params", {}) or {}
        try:
            if body.get("jsonrpc") != "2.0":
                return _jsonrpc_response(request_id, error=_jsonrpc_error(-32600, "jsonrpc must be '2.0'."))
            if method == "message/send":
                message = params.get("message")
                if not isinstance(message, dict):
                    raise CiderValidationError("message/send requires params.message.")
                task = _execute_message(
                    message,
                    task_id=params.get("taskId"),
                    context_id=message.get("contextId") or params.get("contextId"),
                )
                return _jsonrpc_response(request_id, result=task)
            if method == "message/stream":
                message = params.get("message")
                if not isinstance(message, dict):
                    raise CiderValidationError("message/stream requires params.message.")
                task = _execute_message(
                    message,
                    task_id=params.get("taskId"),
                    context_id=message.get("contextId") or params.get("contextId"),
                )

                async def event_stream():
                    submitted = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "kind": "status-update",
                            "taskId": task["id"],
                            "contextId": task["contextId"],
                            "status": {
                                "state": "submitted",
                                "timestamp": _iso_now(),
                            },
                            "final": False,
                        },
                    }
                    yield f"data: {json.dumps(submitted)}\n\n"
                    await asyncio.sleep(0)
                    completed = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "kind": "status-update",
                            "taskId": task["id"],
                            "contextId": task["contextId"],
                            "status": task["status"],
                            "artifact": task["artifacts"][0],
                            "final": True,
                        },
                    }
                    yield f"data: {json.dumps(completed)}\n\n"

                return StreamingResponse(event_stream(), media_type="text/event-stream")
            if method == "tasks/get":
                task_id = str(params.get("id", "")).strip()
                task = TASK_STORE.get(task_id)
                if task is None:
                    return _jsonrpc_response(request_id, error=_jsonrpc_error(-32004, "Task not found."))
                return _jsonrpc_response(request_id, result=task)
            if method == "tasks/cancel":
                task_id = str(params.get("id", "")).strip()
                task = TASK_STORE.get(task_id)
                if task is None:
                    return _jsonrpc_response(request_id, error=_jsonrpc_error(-32004, "Task not found."))
                if task["status"]["state"] in {"completed", "failed", "cancelled", "rejected"}:
                    return _jsonrpc_response(request_id, result=task)
                task["status"] = {"state": "cancelled", "timestamp": _iso_now()}
                TASK_STORE.save(task)
                return _jsonrpc_response(request_id, result=task)
            if method == "tasks/list":
                tasks = TASK_STORE.list()
                return _jsonrpc_response(
                    request_id,
                    result={
                        "tasks": tasks,
                        "nextPageToken": "",
                        "pageSize": len(tasks),
                        "totalSize": len(tasks),
                    },
                )
            return _jsonrpc_response(request_id, error=_jsonrpc_error(-32601, f"Unknown method: {method}"))
        except CiderAgentError as exc:
            return _jsonrpc_response(request_id, error=_jsonrpc_error(-32000, str(exc)))
        except Exception as exc:  # pragma: no cover - defensive fallback
            return _jsonrpc_response(request_id, error=_jsonrpc_error(-32603, f"Internal error: {exc}"))

    return app


def run_server() -> None:
    settings = get_settings()
    uvicorn.run(create_a2a_app(), host=settings.http_host, port=settings.http_port, reload=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the cider_agent A2A server.")
    parser.parse_args()
    run_server()


if __name__ == "__main__":
    main()
