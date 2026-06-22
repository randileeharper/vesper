"""HTTP transport hosting for Vesper."""

from __future__ import annotations

import argparse
import asyncio
import uuid
from contextlib import AsyncExitStack
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct

from a2a.helpers import new_data_part, new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from starlette.routing import Route
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Role,
    TaskState,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, PROTOCOL_VERSION_1_0, TransportProtocol
from a2a.utils.errors import InternalError, InvalidParamsError

from .action_registry import get_action_definition, is_public_action, match_text_action_definition
from .app import get_service, get_settings
from .errors import CiderAgentError, CiderValidationError, TextRequestExecutionError
from .mcp_server import create_mcp_server
from .renderers import render_action_result_for_a2a, render_text_result_for_a2a


def _struct_from_dict(payload: dict[str, Any]) -> Struct:
    return ParseDict(payload, Struct())


def _data_payload_from_message(message: Message) -> dict[str, Any] | None:
    for part in message.parts:
        if part.HasField("data"):
            payload = MessageToDict(part.data)
            if isinstance(payload, dict):
                return payload
    return None


def _text_from_message(message: Message) -> str | None:
    for part in message.parts:
        if part.HasField("text"):
            text = part.text.strip()
            if text:
                return text
    return None


def _metadata_for_action(action: str, metadata: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(metadata)
    enriched.setdefault("action", action)
    return enriched


def _agent_message(
    *,
    text: str,
    payload: dict[str, Any],
    context_id: str,
    task_id: str,
    metadata: dict[str, Any],
) -> Message:
    return Message(
        role=Role.ROLE_AGENT,
        message_id=str(uuid.uuid4()),
        context_id=context_id,
        task_id=task_id,
        parts=[
            new_text_part(text, media_type="text/plain"),
            new_data_part(payload, media_type="application/json"),
        ],
        metadata=_struct_from_dict(metadata),
    )


@dataclass(frozen=True)
class RequestInspection:
    kind: str
    read_only: bool
    action: str | None = None
    parameters: dict[str, Any] | None = None
    text: str | None = None
    public_action: bool = True


@dataclass(frozen=True)
class ExecutionResult:
    action: str
    payload: dict[str, Any]
    metadata: dict[str, Any]
    summary: str


def build_agent_card() -> AgentCard:
    settings = get_settings()
    return AgentCard(
        name="vesper",
        description="A dedicated music control agent for Cider. The intended interface is plain-language requests over A2A text messages.",
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=f"{settings.public_base_url}/a2a",
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            ),
            AgentInterface(
                url=settings.public_base_url,
                protocol_binding=TransportProtocol.HTTP_JSON.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            ),
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extended_agent_card=False,
        ),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(
                id="natural-language-music-control",
                name="Natural-Language Music Control",
                description="Send plain-language music requests like 'play upbeat morning music', 'more pop', or 'what's playing?'.",
                tags=["audio", "playback", "music"],
                examples=["Play upbeat morning music", "I don't like this", "Play some music"],
                input_modes=["text/plain"],
                output_modes=["application/json", "text/plain"],
            ),
            AgentSkill(
                id="advanced-structured-actions",
                name="Advanced Structured Actions",
                description="Structured action payloads are intentionally limited to the small public playback and preference surface; use natural-language text requests for everything else.",
                tags=["advanced", "structured", "integration"],
                examples=["Play", "Pause", "Stop", "List preferences"],
                input_modes=["application/json", "text/plain"],
                output_modes=["application/json"],
            ),
        ],
    )


def _inspect_message(message: Message) -> RequestInspection:
    payload = _data_payload_from_message(message)
    if payload is not None:
        action = str(payload.get("action", "")).strip()
        if not action:
            raise CiderValidationError("Data part must include a non-empty action.")
        parameters = payload.get("parameters", {})
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise CiderValidationError("parameters must be an object.")
        definition = get_action_definition(action)
        return RequestInspection(
            kind="action",
            action=action,
            parameters=parameters,
            read_only=bool(definition and definition.read_only),
            public_action=is_public_action(action),
        )

    text = _text_from_message(message)
    if text is not None:
        definition = match_text_action_definition(text)
        return RequestInspection(
            kind="text",
            text=text,
            read_only=bool(definition and definition.read_only),
        )

    raise CiderValidationError("Message did not include a supported text or data part.")


def _execute_inspection(
    inspection: RequestInspection,
    *,
    correlation_id: str | None = None,
) -> ExecutionResult:
    service = get_service()
    with service.operation(caller="a2a", correlation_id=correlation_id):
        if inspection.kind == "text":
            resolved = service.execute_text_request(inspection.text or "")
            payload, metadata = render_text_result_for_a2a(resolved)
            action = resolved.execution.action
        else:
            if not inspection.public_action:
                raise CiderValidationError(
                    f"Structured action '{inspection.action}' is not publicly exposed. Use a plain-language text request instead."
                )
            action = inspection.action or ""
            payload, metadata = render_action_result_for_a2a(service.execute_action(action, inspection.parameters or {}))

    metadata = _metadata_for_action(action, metadata)
    summary = str(metadata.get("summary", "")).strip() or str(payload.get("summary", "")).strip() or f"Completed action '{action}'."
    metadata["summary"] = summary
    return ExecutionResult(action=action, payload=payload, metadata=metadata, summary=summary)


class CiderAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        if message is None:
            raise InvalidParamsError("SendMessageRequest.message is required.")

        task_id = context.task_id
        context_id = context.context_id
        if not task_id or not context_id:
            raise InternalError("Request context did not include task identifiers.")

        try:
            inspection = _inspect_message(message)
        except CiderValidationError as exc:
            raise InvalidParamsError(str(exc)) from exc

        if inspection.kind == "action" and not inspection.public_action:
            await self._reject_hidden_action(
                event_queue=event_queue,
                message=message,
                task_id=task_id,
                context_id=context_id,
                action=inspection.action or "",
            )
            return

        await self._run_task(
            inspection,
            event_queue=event_queue,
            message=message,
            task_id=task_id,
            context_id=context_id,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        if not task_id or not context_id:
            raise InternalError("Cancellation request did not include task identifiers.")

        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
        message = _agent_message(
            text="Request canceled.",
            payload={"status": "error", "message": "Request canceled."},
            context_id=context_id,
            task_id=task_id,
            metadata={"summary": "Request canceled."},
        )
        await updater.update_status(
            TaskState.TASK_STATE_CANCELED,
            message=message,
            metadata={"summary": "Request canceled."},
        )

    async def _run_task(
        self,
        inspection: RequestInspection,
        *,
        event_queue: EventQueue,
        message: Message,
        task_id: str,
        context_id: str,
    ) -> None:
        submitted = new_task(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_SUBMITTED,
            history=[message],
        )
        submitted.status.message.CopyFrom(
            _agent_message(
                text="Request accepted.",
                payload={"status": "submitted"},
                context_id=context_id,
                task_id=task_id,
                metadata={"summary": "Request accepted."},
            )
        )
        submitted.metadata.CopyFrom(_struct_from_dict({"summary": "Request accepted."}))
        await event_queue.enqueue_event(submitted)

        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
        await updater.start_work(
            message=_agent_message(
                text="Working on request.",
                payload={"status": "working"},
                context_id=context_id,
                task_id=task_id,
                metadata={"summary": "Working on request."},
            )
        )

        try:
            result = await asyncio.to_thread(_execute_inspection, inspection, correlation_id=task_id)
        except TextRequestExecutionError as exc:
            payload = dict(exc.payload)
            agent_message = _agent_message(
                text=str(exc),
                payload=payload,
                context_id=context_id,
                task_id=task_id,
                metadata={"summary": str(exc)},
            )
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                message=agent_message,
                metadata={"summary": str(exc)},
            )
            return
        except CiderAgentError as exc:
            agent_message = _agent_message(
                text=str(exc),
                payload={"status": "error", "message": str(exc)},
                context_id=context_id,
                task_id=task_id,
                metadata={"summary": str(exc)},
            )
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                message=agent_message,
                metadata={"summary": str(exc)},
            )
            return

        await updater.add_artifact(
            parts=[new_data_part(result.payload, media_type="application/json")],
            name="vesper-result",
            metadata=result.metadata,
        )
        await updater.update_status(
            TaskState.TASK_STATE_COMPLETED,
            message=_agent_message(
                text=result.summary,
                payload=result.payload,
                context_id=context_id,
                task_id=task_id,
                metadata=result.metadata,
            ),
            metadata=result.metadata,
        )

    async def _reject_hidden_action(
        self,
        *,
        event_queue: EventQueue,
        message: Message,
        task_id: str,
        context_id: str,
        action: str,
    ) -> None:
        rejection = f"Structured action '{action}' is not publicly exposed. Use a plain-language text request instead."
        task = new_task(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_SUBMITTED,
            history=[message],
        )
        task.metadata.CopyFrom(_struct_from_dict({"summary": rejection, "action": action}))
        await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
        await updater.update_status(
            TaskState.TASK_STATE_REJECTED,
            message=_agent_message(
                text=rejection,
                payload={"status": "error", "message": rejection},
                context_id=context_id,
                task_id=task_id,
                metadata={"summary": rejection, "action": action},
            ),
            metadata={"summary": rejection, "action": action},
        )


def _create_lifespan(mcp_session_manager=None):
    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        service = get_service()
        service.start_background_session_worker()
        try:
            async with AsyncExitStack() as stack:
                if mcp_session_manager is not None:
                    await stack.enter_async_context(mcp_session_manager.run())
                yield
        finally:
            service.stop_background_session_worker()

    return _lifespan


def create_http_app(*, include_a2a: bool = False, include_mcp: bool = False) -> FastAPI:
    if not include_a2a and not include_mcp:
        raise ValueError("At least one HTTP transport must be enabled.")

    mcp_server = None
    mcp_endpoint = None
    if include_mcp:
        mcp_server = create_mcp_server(streamable_http_path="/", manage_session_worker=False)
        mcp_app = mcp_server.streamable_http_app()
        mcp_endpoint = mcp_app.routes[0].endpoint

    app = FastAPI(title="Vesper", version="0.1.0", lifespan=_create_lifespan(mcp_server.session_manager if mcp_server else None))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if include_a2a:
        @app.get("/.well-known/agent-card", response_model=None)
        async def agent_card_alias() -> dict[str, Any]:
            return MessageToDict(build_agent_card(), preserving_proto_field_name=False)

        handler = DefaultRequestHandlerV2(
            agent_executor=CiderAgentExecutor(),
            task_store=InMemoryTaskStore(),
            agent_card=build_agent_card(),
        )
        add_a2a_routes_to_fastapi(
            app,
            agent_card_routes=create_agent_card_routes(build_agent_card(), card_url=AGENT_CARD_WELL_KNOWN_PATH),
            jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a"),
            rest_routes=create_rest_routes(handler),
        )

    if mcp_endpoint is not None:
        app.router.routes.append(Route("/mcp", endpoint=mcp_endpoint))
        app.router.routes.append(Route("/mcp/", endpoint=mcp_endpoint))
    return app


def create_a2a_app(*, include_mcp: bool = False) -> FastAPI:
    return create_http_app(include_a2a=True, include_mcp=include_mcp)


def run_server(*, include_a2a: bool = False, include_mcp: bool = False) -> None:
    settings = get_settings()
    uvicorn.run(
        create_http_app(include_a2a=include_a2a, include_mcp=include_mcp),
        host=settings.http_host,
        port=settings.http_port,
        reload=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Vesper HTTP transports.")
    parser.add_argument("--a2a", action="store_true", help="Enable the A2A HTTP transport.")
    parser.add_argument("--mcp", action="store_true", help="Also mount the MCP Streamable HTTP transport at /mcp.")
    args = parser.parse_args()
    if not args.a2a and not args.mcp:
        parser.error("At least one transport flag is required: --a2a and/or --mcp.")
    run_server(include_a2a=args.a2a, include_mcp=args.mcp)


if __name__ == "__main__":
    main()
