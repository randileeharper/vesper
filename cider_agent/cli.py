"""CLI entrypoint for cider_agent."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

import httpx

from .a2a import _message
from .app import get_service, get_settings
from .errors import CiderAgentError, TextRequestExecutionError
from .renderers import render_task_payload_for_cli


def _print_payload(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    result = payload.get("result", payload)
    print(json.dumps(result, indent=2, sort_keys=True))


def _post_local_a2a(*, method: str, params: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    endpoint = f"{settings.public_base_url}/a2a"
    request_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    try:
        with httpx.Client(timeout=settings.request_timeout_seconds, verify=settings.verify_tls) as client:
            response = client.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        raise ConnectionError(str(exc)) from exc
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        error = body["error"]
        message_text = str(error.get("message", "Unknown server error."))
        data = error.get("data")
        if isinstance(data, dict):
            raise TextRequestExecutionError(message_text, data)
        raise CiderAgentError(message_text)
    result = body.get("result")
    if not isinstance(result, dict):
        raise CiderAgentError("Local A2A server returned an invalid task payload.")
    return result


def _raise_for_failed_task(task: dict[str, Any]) -> None:
    status = task.get("status", {})
    message = "Local A2A server task failed."
    payload: dict[str, Any] | None = None
    if isinstance(status, dict):
        status_message = status.get("message")
        if isinstance(status_message, dict):
            payload = None
            parts = status_message.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and part.get("kind") == "data" and isinstance(part.get("data"), dict):
                        payload = dict(part["data"])
                        break
            parts = status_message.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and part.get("kind") == "text" and isinstance(part.get("text"), str):
                        text = part["text"].strip()
                        if text:
                            message = text
                            break
    if isinstance(payload, dict):
        raise TextRequestExecutionError(message, payload)
    raise CiderAgentError(message)


def _call_local_a2a(message: dict[str, Any]) -> dict[str, Any]:
    task = _post_local_a2a(method="message/send", params={"message": message, "defer": False})
    state = str(task.get("status", {}).get("state", "")).strip().lower()
    if state != "completed":
        _raise_for_failed_task(task)
    return task


def _task_to_cli_payload(task: dict[str, Any], *, original_text: str | None = None) -> dict[str, Any]:
    try:
        return render_task_payload_for_cli(task, original_text=original_text)
    except ValueError as exc:
        raise CiderAgentError("Local A2A server task did not include a data artifact.") from exc


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
        task = _call_local_a2a(
            _message(
                "user",
                [{"kind": "text", "text": args.text}],
            )
        )
        return _task_to_cli_payload(task, original_text=args.text)

    action_request = _build_action_request(args)
    if action_request is None:
        raise CiderAgentError(f"Unsupported CLI command for local server mode: {args.command}")
    action, parameters = action_request
    task = _call_local_a2a(
        _message(
            "user",
            [{"kind": "data", "data": {"action": action, "parameters": parameters}}],
        )
    )
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

    preferences = subparsers.add_parser("preferences")
    preferences_subparsers = preferences.add_subparsers(dest="preferences_command", required=True)
    preferences_subparsers.add_parser("list")
    pref_forget = preferences_subparsers.add_parser("forget")
    pref_forget.add_argument("preference_id", type=int)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

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
                create_a2a_app(),
                host=args.host or settings.http_host,
                port=args.port or settings.http_port,
                reload=False,
            )
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
