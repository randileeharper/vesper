"""CLI entrypoint for cider_agent."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

import httpx

from .a2a import _message
from .app import get_service, get_settings
from .errors import CiderAgentError, TextRequestExecutionError


TERMINAL_TASK_STATES = {"completed", "failed", "cancelled", "rejected"}


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


def _extract_data_part(parts: Any) -> dict[str, Any] | None:
    if not isinstance(parts, list):
        return None
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "data" and isinstance(part.get("data"), dict):
            return dict(part["data"])
    return None


def _extract_task_payload(task: dict[str, Any]) -> dict[str, Any]:
    artifacts = task.get("artifacts", [])
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            payload = _extract_data_part(artifact.get("parts", []))
            if payload is not None:
                return payload
    raise CiderAgentError("Local A2A server task did not include a data artifact.")


def _raise_for_failed_task(task: dict[str, Any]) -> None:
    status = task.get("status", {})
    message = "Local A2A server task failed."
    payload: dict[str, Any] | None = None
    if isinstance(status, dict):
        status_message = status.get("message")
        if isinstance(status_message, dict):
            payload = _extract_data_part(status_message.get("parts", []))
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


def _wait_for_task_completion(task: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    task_id = str(task.get("id", "")).strip()
    if not task_id:
        raise CiderAgentError("Local A2A server returned a submitted task without an id.")
    deadline = time.monotonic() + float(settings.request_timeout_seconds)
    while time.monotonic() < deadline:
        current = _post_local_a2a(method="tasks/get", params={"id": task_id})
        state = str(current.get("status", {}).get("state", "")).strip().lower()
        if state == "completed":
            return current
        if state in TERMINAL_TASK_STATES:
            _raise_for_failed_task(current)
        time.sleep(0.1)
    raise CiderAgentError(f"Local A2A server task {task_id} did not complete within {settings.request_timeout_seconds:g}s.")


def _call_local_a2a(message: dict[str, Any]) -> dict[str, Any]:
    task = _post_local_a2a(method="message/send", params={"message": message})
    state = str(task.get("status", {}).get("state", "")).strip().lower()
    if state == "submitted":
        return _wait_for_task_completion(task)
    if state in TERMINAL_TASK_STATES and state != "completed":
        _raise_for_failed_task(task)
    return task


def _task_to_cli_payload(task: dict[str, Any], *, original_text: str | None = None) -> dict[str, Any]:
    payload = _extract_task_payload(task)
    metadata = task.get("metadata", {}) if isinstance(task.get("metadata"), dict) else {}
    action = metadata.get("action")
    if original_text is not None:
        response: dict[str, Any] = {
            "status": "ok",
            "input": original_text,
            "resolver": metadata.get("resolver"),
            "resolved_action": metadata.get("resolved_action", {"action": action} if action else {}),
            "execution": {
                "action": action,
                "result": payload,
            },
        }
        if "reasoning" in metadata:
            response["reasoning"] = metadata["reasoning"]
        if "resolver_raw_content" in metadata:
            response["resolver_raw_content"] = metadata["resolver_raw_content"]
        if "resolver_raw_action" in metadata:
            response["resolver_raw_action"] = metadata["resolver_raw_action"]
        return response
    return payload


def _build_action_request(args: argparse.Namespace) -> tuple[str, dict[str, Any]] | None:
    if args.command == "status":
        return "status", {}
    if args.command == "now-playing":
        return "get_now_playing", {}
    if args.command == "play":
        return "play", {}
    if args.command == "pause":
        return "pause", {}
    if args.command == "playpause":
        return "playpause", {}
    if args.command == "stop":
        return "stop", {}
    if args.command == "next":
        return "next_track", {}
    if args.command == "previous":
        return "previous_track", {}
    if args.command == "seek":
        return "seek", {"position_seconds": args.position_seconds}
    if args.command == "volume":
        if args.volume_command == "get":
            return "get_volume", {}
        return "set_volume", {"volume": args.volume}
    if args.command == "queue":
        if args.queue_command == "show":
            return "get_queue", {}
        if args.queue_command == "clear":
            return "clear_queue", {}
        if args.queue_command == "remove":
            return "remove_queue_item", {"index": args.index}
        return "move_queue_item", {"from_index": args.from_index, "to_index": args.to_index}
    if args.command == "search":
        if args.search_command == "default":
            return "search", {"query": args.query, "limit": args.limit, "storefront": args.storefront}
        if args.search_command == "library":
            return "search_library", {"query": args.query, "limit": args.limit}
        return "search_catalog", {"query": args.query, "limit": args.limit, "storefront": args.storefront}
    if args.command == "session":
        if args.session_command == "status":
            return "session_status", {}
        if args.session_command == "stop":
            return "stop_session", {}
        return "refill_session", {}
    if args.command == "playlist":
        if args.playlist_command == "list":
            return "list_library_playlists", {}
        if args.playlist_command == "tracks":
            return "get_library_playlist_tracks", {"playlist_id": args.playlist_id}
        if args.playlist_command == "create":
            return "create_playlist", {
                "name": args.name,
                "description": args.description,
                "track_refs": _parse_track_refs(args.track_ref) if args.track_ref else None,
            }
        return "add_playlist_tracks", {
            "playlist_id": args.playlist_id,
            "track_refs": _parse_track_refs(args.track_ref),
        }
    if args.command == "preferences":
        if args.preferences_command == "list":
            return "list_preferences", {}
        if args.preferences_command == "remember":
            return "remember_preference", {
                "kind": args.kind,
                "value": args.value,
                "category": args.category,
                "weight": args.weight,
                "note": args.note,
            }
        return "forget_preference", {"preference_id": args.preference_id}
    if args.command == "recommend":
        if args.play:
            return "play_recommendation", {"query": args.query}
        return "recommend", {"query": args.query, "limit": args.limit}
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

    subparsers.add_parser("status")
    subparsers.add_parser("now-playing")
    subparsers.add_parser("play")
    subparsers.add_parser("pause")
    subparsers.add_parser("playpause")
    subparsers.add_parser("stop")
    subparsers.add_parser("next")
    subparsers.add_parser("previous")

    seek = subparsers.add_parser("seek")
    seek.add_argument("position_seconds", type=float)

    volume = subparsers.add_parser("volume")
    volume_subparsers = volume.add_subparsers(dest="volume_command", required=True)
    volume_subparsers.add_parser("get")
    volume_set = volume_subparsers.add_parser("set")
    volume_set.add_argument("volume", type=int)

    queue = subparsers.add_parser("queue")
    queue_subparsers = queue.add_subparsers(dest="queue_command", required=True)
    queue_subparsers.add_parser("show")
    queue_subparsers.add_parser("clear")
    queue_remove = queue_subparsers.add_parser("remove")
    queue_remove.add_argument("index", type=int)
    queue_move = queue_subparsers.add_parser("move")
    queue_move.add_argument("from_index", type=int)
    queue_move.add_argument("to_index", type=int)

    search = subparsers.add_parser("search")
    search_subparsers = search.add_subparsers(dest="search_command", required=True)
    search_default = search_subparsers.add_parser("default")
    search_default.add_argument("query")
    search_default.add_argument("--limit", type=int, default=10)
    search_default.add_argument("--storefront", default="us")
    search_library = search_subparsers.add_parser("library")
    search_library.add_argument("query")
    search_library.add_argument("--limit", type=int, default=10)
    search_catalog = search_subparsers.add_parser("catalog")
    search_catalog.add_argument("query")
    search_catalog.add_argument("--limit", type=int, default=10)
    search_catalog.add_argument("--storefront", default="us")

    ask = subparsers.add_parser("ask")
    ask.add_argument("text")

    session = subparsers.add_parser("session")
    session_subparsers = session.add_subparsers(dest="session_command", required=True)
    session_subparsers.add_parser("status")
    session_subparsers.add_parser("stop")
    session_subparsers.add_parser("refill")

    playlist = subparsers.add_parser("playlist")
    playlist_subparsers = playlist.add_subparsers(dest="playlist_command", required=True)
    playlist_subparsers.add_parser("list")
    playlist_show = playlist_subparsers.add_parser("tracks")
    playlist_show.add_argument("playlist_id")
    playlist_create = playlist_subparsers.add_parser("create")
    playlist_create.add_argument("name")
    playlist_create.add_argument("--description")
    playlist_create.add_argument(
        "--track-ref",
        action="append",
        default=[],
        help="Track ref in id:type form. Repeatable.",
    )
    playlist_add = playlist_subparsers.add_parser("add-tracks")
    playlist_add.add_argument("playlist_id")
    playlist_add.add_argument("--track-ref", action="append", required=True, help="Track ref in id:type form.")

    preferences = subparsers.add_parser("preferences")
    preferences_subparsers = preferences.add_subparsers(dest="preferences_command", required=True)
    preferences_subparsers.add_parser("list")
    pref_remember = preferences_subparsers.add_parser("remember")
    pref_remember.add_argument("kind", choices=["like", "dislike"])
    pref_remember.add_argument("value")
    pref_remember.add_argument("--category")
    pref_remember.add_argument("--weight", type=float, default=1.0)
    pref_remember.add_argument("--note")
    pref_forget = preferences_subparsers.add_parser("forget")
    pref_forget.add_argument("preference_id", type=int)

    recommend = subparsers.add_parser("recommend")
    recommend.add_argument("--query")
    recommend.add_argument("--limit", type=int, default=5)
    recommend.add_argument("--play", action="store_true")

    serve = subparsers.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    return parser


def _parse_track_refs(values: list[str]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for value in values:
        track_id, _, track_type = value.partition(":")
        refs.append({"id": track_id, "type": track_type or "songs"})
    return refs


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
            if args.command == "status":
                payload = service.status()
            elif args.command == "now-playing":
                payload = service.get_now_playing()
            elif args.command == "play":
                payload = service.play()
            elif args.command == "pause":
                payload = service.pause()
            elif args.command == "playpause":
                payload = service.playpause()
            elif args.command == "stop":
                payload = service.stop()
            elif args.command == "next":
                payload = service.next_track()
            elif args.command == "previous":
                payload = service.previous_track()
            elif args.command == "seek":
                payload = service.seek(args.position_seconds)
            elif args.command == "volume":
                if args.volume_command == "get":
                    payload = service.get_volume()
                else:
                    payload = service.set_volume(args.volume)
            elif args.command == "queue":
                if args.queue_command == "show":
                    payload = service.get_queue()
                elif args.queue_command == "clear":
                    payload = service.clear_queue()
                elif args.queue_command == "remove":
                    payload = service.remove_queue_item(args.index)
                else:
                    payload = service.move_queue_item(args.from_index, args.to_index)
            elif args.command == "search":
                if args.search_command == "default":
                    payload = service.search(args.query, limit=args.limit, storefront=args.storefront)
                elif args.search_command == "library":
                    payload = service.search_library(args.query, limit=args.limit)
                else:
                    payload = service.search_catalog(args.query, limit=args.limit, storefront=args.storefront)
            elif args.command == "ask":
                payload = service.handle_text_request(args.text)
            elif args.command == "session":
                if args.session_command == "status":
                    payload = service.session_status()
                elif args.session_command == "stop":
                    payload = service.stop_session()
                else:
                    payload = service.refill_active_session()
            elif args.command == "playlist":
                if args.playlist_command == "list":
                    payload = service.list_library_playlists()
                elif args.playlist_command == "tracks":
                    payload = service.get_library_playlist_tracks(args.playlist_id)
                elif args.playlist_command == "create":
                    payload = service.create_playlist(
                        name=args.name,
                        description=args.description,
                        track_refs=_parse_track_refs(args.track_ref) if args.track_ref else None,
                    )
                else:
                    payload = service.add_playlist_tracks(
                        args.playlist_id,
                        track_refs=_parse_track_refs(args.track_ref),
                    )
            elif args.command == "preferences":
                if args.preferences_command == "list":
                    payload = service.list_preferences()
                elif args.preferences_command == "remember":
                    payload = service.remember_preference(
                        kind=args.kind,
                        value=args.value,
                        category=args.category,
                        weight=args.weight,
                        note=args.note,
                    )
                else:
                    payload = service.forget_preference(args.preference_id)
            elif args.command == "recommend":
                if args.play:
                    payload = service.play_recommendation(query=args.query)
                else:
                    payload = service.recommend(query=args.query, limit=args.limit)
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
