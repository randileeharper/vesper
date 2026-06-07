"""Minimal A2A-compatible HTTP server for cider_agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .app import get_service, get_settings
from .errors import CiderAgentError, CiderValidationError, TextRequestExecutionError


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

READ_ONLY_ACTIONS = {
    "status",
    "get_now_playing",
    "playback_snapshot",
    "is_playing",
    "get_volume",
    "get_repeat_mode",
    "get_shuffle_mode",
    "get_autoplay",
    "get_queue",
    "session_status",
    "search",
    "search_catalog",
    "search_library",
    "search_catalog_tracks",
    "search_library_tracks",
    "list_library_playlists",
    "search_library_playlists",
    "get_library_playlist",
    "get_library_playlist_tracks",
    "list_preferences",
    "recommend",
    "list_recently_played",
}

READ_ONLY_TEXT_PATTERNS = [
    r"^\s*status\s*$",
    r"^\s*what('?s| is)\s+playing\??\s*$",
    r"^\s*now\s+playing\??\s*$",
    r"^\s*session\s+status\??\s*$",
    r"^\s*queue\s+status\??\s*$",
]


def build_agent_card() -> dict[str, Any]:
    settings = get_settings()
    return {
        "protocolVersion": "1.0.0",
        "name": "Cider Agent",
        "description": "A dedicated music control agent for Cider. The intended interface is plain-language requests over A2A text messages.",
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
                "id": "natural-language-music-control",
                "name": "Natural-Language Music Control",
                "description": "Send plain-language music requests like 'play upbeat morning music', 'more pop', or 'what's playing?'.",
                "tags": ["audio", "playback", "music"],
                "examples": ["Play upbeat morning music", "I don't like this", "Add some KATSEYE"],
                "inputModes": ["text/plain"],
                "outputModes": ["application/json", "text/plain"],
            },
            {
                "id": "advanced-structured-actions",
                "name": "Advanced Structured Actions",
                "description": "Structured action payloads are supported for advanced integrations, but most callers should use natural-language text requests instead.",
                "tags": ["advanced", "structured", "integration"],
                "examples": ["Status", "Search my library for k-pop", "Set volume to 35"],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json"],
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


def _is_read_only_action(action: str) -> bool:
    return action in READ_ONLY_ACTIONS


def _is_read_only_text_request(text: str) -> bool:
    return any(re.match(pattern, text, flags=re.IGNORECASE) for pattern in READ_ONLY_TEXT_PATTERNS)


def _should_defer_message(message: dict[str, Any]) -> bool:
    parts = message.get("parts", [])
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            action = str(part["data"].get("action", "")).strip()
            return not _is_read_only_action(action)
        if part.get("kind") == "text":
            return not _is_read_only_text_request(str(part.get("text", "")))
    return False


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
    summary = payload.get("summary") if isinstance(payload, dict) else None
    message_text = str(summary).strip() if isinstance(summary, str) and summary.strip() else f"Completed action '{action}'."
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": "completed",
            "timestamp": _iso_now(),
            "message": _message(
                "agent",
                [_text_part(message_text), _data_part(payload)],
                task_id=task_id,
            ),
        },
        "artifacts": [_artifact(payload)],
        "history": [
            request_message,
            _message("agent", [_text_part(message_text), _data_part(payload)], task_id=task_id),
        ],
        "metadata": {"action": action, "summary": message_text},
    }


def _submitted_task(*, task_id: str, context_id: str, request_message: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": "submitted",
            "timestamp": _iso_now(),
            "message": _message(
                "agent",
                [_text_part("Request accepted."), _data_part({"status": "submitted"})],
                task_id=task_id,
            ),
        },
        "artifacts": [],
        "history": [
            request_message,
            _message("agent", [_text_part("Request accepted."), _data_part({"status": "submitted"})], task_id=task_id),
        ],
        "metadata": {"summary": "Request accepted."},
    }


def _failed_task(
    *,
    task: dict[str, Any],
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task["status"] = {
        "state": "failed",
        "timestamp": _iso_now(),
        "message": _message(
            "agent",
            [_text_part(message), _data_part(payload or {"status": "error", "message": message})],
            task_id=task["id"],
        ),
    }
    if payload is not None:
        task["artifacts"] = [_artifact(payload)]
    task["history"].append(
        _message(
            "agent",
            [_text_part(message), _data_part(payload or {"status": "error", "message": message})],
            task_id=task["id"],
        )
    )
    task["metadata"]["summary"] = message
    return task


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
        if "reasoning" in resolved:
            task["metadata"]["reasoning"] = resolved["reasoning"]
        if "resolver_raw_content" in resolved:
            task["metadata"]["resolver_raw_content"] = resolved["resolver_raw_content"]
        if "resolver_raw_action" in resolved:
            task["metadata"]["resolver_raw_action"] = resolved["resolver_raw_action"]
    TASK_STORE.save(task)
    return task


def _complete_submitted_task(task: dict[str, Any], completed: dict[str, Any]) -> dict[str, Any]:
    task["status"] = completed["status"]
    task["artifacts"] = completed["artifacts"]
    task["history"] = completed["history"]
    task["metadata"].update(completed.get("metadata", {}))
    return task


async def _execute_message_background(message: dict[str, Any], *, task_id: str, context_id: str) -> None:
    try:
        completed = await asyncio.to_thread(
            _execute_message,
            message,
            task_id=task_id,
            context_id=context_id,
        )
    except TextRequestExecutionError as exc:
        task = TASK_STORE.get(task_id)
        if task is None:
            return
        TASK_STORE.save(_failed_task(task=task, message=str(exc), payload=exc.payload))
        return
    except CiderAgentError as exc:
        task = TASK_STORE.get(task_id)
        if task is None:
            return
        TASK_STORE.save(_failed_task(task=task, message=str(exc)))
        return
    task = TASK_STORE.get(task_id)
    if task is None:
        return
    TASK_STORE.save(_complete_submitted_task(task, completed))


@asynccontextmanager
async def _lifespan(_: FastAPI):
    service = get_service()
    service.start_background_session_worker()
    try:
        yield
    finally:
        service.stop_background_session_worker()


def create_a2a_app() -> FastAPI:
    app = FastAPI(title="Cider Agent", version="0.1.0", lifespan=_lifespan)

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
                resolved_task_id = str(params.get("taskId") or uuid.uuid4())
                resolved_context_id = str(message.get("contextId") or params.get("contextId") or uuid.uuid4())
                if _should_defer_message(message):
                    task = _submitted_task(
                        task_id=resolved_task_id,
                        context_id=resolved_context_id,
                        request_message=message,
                    )
                    TASK_STORE.save(task)
                    asyncio.create_task(
                        _execute_message_background(
                            message,
                            task_id=resolved_task_id,
                            context_id=resolved_context_id,
                        )
                    )
                else:
                    task = _execute_message(
                        message,
                        task_id=resolved_task_id,
                        context_id=resolved_context_id,
                    )
                return _jsonrpc_response(request_id, result=task)
            if method == "message/stream":
                message = params.get("message")
                if not isinstance(message, dict):
                    raise CiderValidationError("message/stream requires params.message.")
                resolved_task_id = str(params.get("taskId") or uuid.uuid4())
                resolved_context_id = str(message.get("contextId") or params.get("contextId") or uuid.uuid4())
                deferred = _should_defer_message(message)
                if deferred:
                    task = _submitted_task(
                        task_id=resolved_task_id,
                        context_id=resolved_context_id,
                        request_message=message,
                    )
                    TASK_STORE.save(task)
                    asyncio.create_task(
                        _execute_message_background(
                            message,
                            task_id=resolved_task_id,
                            context_id=resolved_context_id,
                        )
                    )
                else:
                    task = _execute_message(
                        message,
                        task_id=resolved_task_id,
                        context_id=resolved_context_id,
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
                    if deferred:
                        while True:
                            await asyncio.sleep(0.1)
                            current = TASK_STORE.get(task["id"])
                            if current is None:
                                continue
                            if current["status"]["state"] in {"completed", "failed", "cancelled", "rejected"}:
                                task.update(current)
                                break
                    else:
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
        except TextRequestExecutionError as exc:
            return _jsonrpc_response(request_id, error=_jsonrpc_error(-32000, str(exc), data=exc.payload))
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
