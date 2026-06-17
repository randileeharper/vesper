"""CLI entrypoint for Vesper."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .app import get_service, get_settings
from .errors import CiderAgentError, TextRequestExecutionError


def _print_payload(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    result = payload.get("result", payload)
    print(json.dumps(result, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vesper", description="Control Cider through Vesper.")
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
    serve.add_argument("--a2a", action="store_true", help="Enable the A2A HTTP transport.")
    serve.add_argument("--mcp", action="store_true", help="Enable the MCP Streamable HTTP transport at /mcp.")

    return parser
def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command == "serve":
            if not args.a2a and not args.mcp:
                parser.error("serve requires at least one transport flag: --a2a and/or --mcp.")

            import uvicorn

            from .a2a import create_http_app

            settings = get_settings()
            uvicorn.run(
                create_http_app(include_a2a=args.a2a, include_mcp=args.mcp),
                host=args.host or settings.http_host,
                port=args.port or settings.http_port,
                reload=False,
            )
            return
        if args.command == "mcp":
            from .mcp_server import create_mcp_server

            create_mcp_server().run("stdio")
            return

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
