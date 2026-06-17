"""CLI entrypoint for cider_agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

import httpx

from .app import get_service, get_settings
from .errors import CiderAgentError, TextRequestExecutionError
from .renderers import render_task_payload_for_cli


def _load_a2a_sdk():
    try:
        from a2a.client import ClientConfig
        from a2a.client.client_factory import ClientFactory
        from a2a.client.errors import AgentCardResolutionError
        from a2a.helpers import new_data_part, new_text_part
        from a2a.types import Message, Role, SendMessageRequest, Task, TaskState
    except ModuleNotFoundError as exc:
        raise ConnectionError("A2A SDK is not installed in this interpreter.") from exc

    return {
        "AgentCardResolutionError": AgentCardResolutionError,
        "ClientConfig": ClientConfig,
        "ClientFactory": ClientFactory,
        "Message": Message,
        "Role": Role,
        "SendMessageRequest": SendMessageRequest,
        "Task": Task,
        "TaskState": TaskState,
        "new_data_part": new_data_part,
        "new_text_part": new_text_part,
    }


def _print_payload(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    result = payload.get("result", payload)
    print(json.dumps(result, indent=2, sort_keys=True))


def _build_user_message(*, text: str | None = None, action: str | None = None, parameters: dict[str, Any] | None = None) -> Any:
    sdk = _load_a2a_sdk()
    parts = []
    if text is not None:
        parts.append(sdk["new_text_part"](text, media_type="text/plain"))
    if action is not None:
        parts.append(
            sdk["new_data_part"](
                {"action": action, "parameters": parameters or {}},
                media_type="application/json",
            )
        )
    return sdk["Message"](
        role=sdk["Role"].ROLE_USER,
        message_id=str(uuid.uuid4()),
        parts=parts,
    )


async def _send_local_a2a_request(message: Any) -> Any:
    sdk = _load_a2a_sdk()
    settings = get_settings()
    httpx_client = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        verify=settings.verify_tls,
    )
    client = None
    try:
        client = await sdk["ClientFactory"](
            sdk["ClientConfig"](streaming=False, polling=False, httpx_client=httpx_client)
        ).create_from_url(settings.public_base_url)
    except (httpx.HTTPError, sdk["AgentCardResolutionError"], ConnectionError) as exc:
        await httpx_client.aclose()
        raise ConnectionError(str(exc)) from exc
    try:
        async for response in client.send_message(sdk["SendMessageRequest"](message=message)):
            if response.HasField("task"):
                return response.task
            if response.HasField("message"):
                return response.message
        raise CiderAgentError("Local A2A server returned an empty response.")
    except httpx.HTTPError as exc:
        raise ConnectionError(str(exc)) from exc
    finally:
        if client is not None:
            await client.close()
        else:
            await httpx_client.aclose()


def _raise_for_failed_task(task: Any) -> None:
    sdk = _load_a2a_sdk()
    state = task.status.state
    status_message = task.status.message if task.status.HasField("message") else None
    message = "Local A2A server task failed."
    payload = {"status": "error", "message": message}
    if status_message is not None:
        try:
            rendered = render_task_payload_for_cli(status_message)
            if isinstance(rendered, dict):
                payload = rendered
        except ValueError:
            payload = {"status": "error", "message": message}
        for part in status_message.parts:
            if part.HasField("text") and part.text.strip():
                message = part.text.strip()
                break
    if state in {
        sdk["TaskState"].TASK_STATE_FAILED,
        sdk["TaskState"].TASK_STATE_REJECTED,
        sdk["TaskState"].TASK_STATE_CANCELED,
    }:
        raise TextRequestExecutionError(message, payload)
    raise CiderAgentError(message)


def _call_local_a2a(message: Any) -> Any:
    sdk = _load_a2a_sdk()
    result = asyncio.run(_send_local_a2a_request(message))
    if isinstance(result, sdk["Task"]):
        terminal = {
            sdk["TaskState"].TASK_STATE_COMPLETED,
            sdk["TaskState"].TASK_STATE_FAILED,
            sdk["TaskState"].TASK_STATE_REJECTED,
            sdk["TaskState"].TASK_STATE_CANCELED,
        }
        if result.status.state not in terminal:
            raise CiderAgentError("Local A2A server returned a non-terminal task.")
        if result.status.state != sdk["TaskState"].TASK_STATE_COMPLETED:
            _raise_for_failed_task(result)
    return result


def _task_to_cli_payload(task: Any, *, original_text: str | None = None) -> dict[str, Any]:
    try:
        return render_task_payload_for_cli(task, original_text=original_text)
    except ValueError as exc:
        raise CiderAgentError("Local A2A server response did not include a data payload.") from exc


def _build_action_request(args: argparse.Namespace) -> tuple[str, dict[str, Any]] | None:
    if args.command == "play":
        return "play", {}
    if args.command == "pause":
        return "pause", {}
    if args.command == "stop":
        return "stop", {}
    if args.command == "preferences":
        if args.preferences_command == "list":
            return "list_preferences", {}
        return "forget_preference", {"preference_id": args.preference_id}
    return None


def _run_via_local_server(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "ask":
        task = _call_local_a2a(_build_user_message(text=args.text))
        return _task_to_cli_payload(task, original_text=args.text)

    action_request = _build_action_request(args)
    if action_request is None:
        raise CiderAgentError(f"Unsupported CLI command for local server mode: {args.command}")
    action, parameters = action_request
    task = _call_local_a2a(_build_user_message(action=action, parameters=parameters))
    return _task_to_cli_payload(task)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cider-agent", description="Control Cider through cider_agent.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("play")
    subparsers.add_parser("pause")
    subparsers.add_parser("stop")

    ask = subparsers.add_parser("ask")
    ask.add_argument("text")

    subparsers.add_parser("mcp")

    preferences = subparsers.add_parser("preferences")
    preferences_subparsers = preferences.add_subparsers(dest="preferences_command", required=True)
    preferences_subparsers.add_parser("list")
    pref_forget = preferences_subparsers.add_parser("forget")
    pref_forget.add_argument("preference_id", type=int)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--mcp", action="store_true", help="Also mount the MCP Streamable HTTP transport at /mcp.")

    return parser
def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command == "serve":
            import uvicorn

            from .a2a import create_a2a_app

            settings = get_settings()
            uvicorn.run(
                create_a2a_app(include_mcp=args.mcp),
                host=args.host or settings.http_host,
                port=args.port or settings.http_port,
                reload=False,
            )
            return
        if args.command == "mcp":
            from .mcp_server import create_mcp_server

            create_mcp_server().run("stdio")
            return

        try:
            payload = _run_via_local_server(args)
        except ConnectionError:
            service = get_service()
            if args.command == "play":
                payload = service.play()
            elif args.command == "pause":
                payload = service.pause()
            elif args.command == "stop":
                payload = service.stop()
            elif args.command == "ask":
                payload = service.handle_text_request(args.text)
            elif args.command == "preferences":
                if args.preferences_command == "list":
                    payload = service.list_preferences()
                else:
                    payload = service.forget_preference(args.preference_id)
            else:  # pragma: no cover - argparse enforces commands
                raise RuntimeError(f"Unhandled command: {args.command}")
    except TextRequestExecutionError as exc:
        print(json.dumps(exc.payload, indent=2, sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from None
    except CiderAgentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    _print_payload(payload, args.json)


if __name__ == "__main__":
    main()
