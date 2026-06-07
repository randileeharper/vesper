"""CLI entrypoint for cider_agent."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .app import get_service
from .errors import CiderAgentError


def _print_payload(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    result = payload.get("result", payload)
    print(json.dumps(result, indent=2, sort_keys=True))


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
        service = get_service()

        if args.command == "serve":
            import uvicorn

            from .a2a import create_a2a_app
            from .app import get_settings

            settings = get_settings()
            uvicorn.run(
                create_a2a_app(),
                host=args.host or settings.http_host,
                port=args.port or settings.http_port,
                reload=False,
            )
            return

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
    except CiderAgentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    _print_payload(payload, args.json)


if __name__ == "__main__":
    main()
