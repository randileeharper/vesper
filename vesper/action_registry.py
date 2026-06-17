"""Central action registry for Vesper."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable


ActionExecutor = Callable[[Any, dict[str, Any]], Any]


@dataclass(frozen=True)
class ActionDefinition:
    """Machine-readable action metadata."""

    name: str
    description: str
    summary_label: str
    executor: ActionExecutor
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    required_fields: tuple[str, ...] = ()
    read_only: bool = False
    text_exposable: bool = True
    public_exposed: bool = False
    session_aware: bool = False
    deferred_a2a_eligible: bool = True
    advanced_only: bool = False
    text_patterns: tuple[str, ...] = ()

    def matches_text(self, text: str) -> bool:
        return any(re.match(pattern, text, flags=re.IGNORECASE) for pattern in self.text_patterns)


def _definition(
    name: str,
    description: str,
    summary_label: str,
    executor: ActionExecutor,
    *,
    parameter_schema: dict[str, Any] | None = None,
    required_fields: tuple[str, ...] = (),
    read_only: bool = False,
    text_exposable: bool = True,
    public_exposed: bool = False,
    session_aware: bool = False,
    deferred_a2a_eligible: bool | None = None,
    advanced_only: bool = False,
    text_patterns: tuple[str, ...] = (),
) -> ActionDefinition:
    return ActionDefinition(
        name=name,
        description=description,
        summary_label=summary_label,
        executor=executor,
        parameter_schema=parameter_schema or {},
        required_fields=required_fields,
        read_only=read_only,
        text_exposable=text_exposable,
        public_exposed=public_exposed,
        session_aware=session_aware,
        deferred_a2a_eligible=read_only if deferred_a2a_eligible is None else deferred_a2a_eligible,
        advanced_only=advanced_only,
        text_patterns=text_patterns,
    )


ACTION_DEFINITIONS: tuple[ActionDefinition, ...] = (
    _definition(
        "status",
        "Show overall service and playback status.",
        "status",
        lambda service, params: service.status(),
        read_only=True,
        text_patterns=(r"^\s*status\s*$",),
    ),
    _definition(
        "get_now_playing",
        "Show the current track.",
        "now playing",
        lambda service, params: service.get_now_playing(),
        read_only=True,
        text_patterns=(r"^\s*what('?s| is)\s+playing\??\s*$", r"^\s*now\s+playing\??\s*$"),
    ),
    _definition("playback_snapshot", "Return a detailed playback snapshot.", "playback snapshot", lambda service, params: service.playback_snapshot(), read_only=True, advanced_only=True),
    _definition("is_playing", "Return whether playback is active.", "is playing", lambda service, params: service.is_playing(), read_only=True, advanced_only=True),
    _definition("play", "Resume playback.", "play", lambda service, params: service.play(), public_exposed=True, text_patterns=(r"^\s*play\s*$", r"^\s*resume\s*$")),
    _definition("pause", "Pause playback.", "pause", lambda service, params: service.pause(), public_exposed=True, text_patterns=(r"^\s*pause\s*$",)),
    _definition("playpause", "Toggle playback.", "playpause", lambda service, params: service.playpause(), advanced_only=True),
    _definition("stop", "Stop playback and end any active session.", "stop", lambda service, params: service.stop(), public_exposed=True),
    _definition("next_track", "Skip to the next track or session-selected track.", "next track", lambda service, params: service.next_track()),
    _definition("previous_track", "Go to the previous track.", "previous track", lambda service, params: service.previous_track()),
    _definition("get_volume", "Get the current playback volume.", "volume", lambda service, params: service.get_volume(), read_only=True, advanced_only=True),
    _definition(
        "set_volume",
        "Set the playback volume.",
        "set volume",
        lambda service, params: service.set_volume(service._coerce_volume_param(params)),
        parameter_schema={"volume": {"type": "number"}},
    ),
    _definition("get_repeat_mode", "Get the repeat mode.", "repeat mode", lambda service, params: service.get_repeat_mode(), read_only=True, advanced_only=True),
    _definition("toggle_repeat", "Toggle repeat mode.", "toggle repeat", lambda service, params: service.toggle_repeat(), advanced_only=True),
    _definition("get_shuffle_mode", "Get the shuffle mode.", "shuffle mode", lambda service, params: service.get_shuffle_mode(), read_only=True, advanced_only=True),
    _definition("toggle_shuffle", "Toggle shuffle mode.", "toggle shuffle", lambda service, params: service.toggle_shuffle(), advanced_only=True),
    _definition("get_autoplay", "Get autoplay status.", "autoplay", lambda service, params: service.get_autoplay(), read_only=True, advanced_only=True),
    _definition("toggle_autoplay", "Toggle autoplay.", "toggle autoplay", lambda service, params: service.toggle_autoplay(), advanced_only=True),
    _definition("get_queue", "Show the playback queue.", "queue", lambda service, params: service.get_queue(), read_only=True, text_patterns=(r"^\s*queue\s+status\??\s*$",)),
    _definition("play_next", "Queue an item to play next.", "play next", lambda service, params: service.play_next(dict(params["item"])), required_fields=("item",), advanced_only=True),
    _definition("play_later", "Queue an item to play later.", "play later", lambda service, params: service.play_later(dict(params["item"])), required_fields=("item",), advanced_only=True),
    _definition("move_queue_item", "Move a queue item.", "move queue item", lambda service, params: service.move_queue_item(int(params["from_index"]), int(params["to_index"])), required_fields=("from_index", "to_index"), advanced_only=True),
    _definition("remove_queue_item", "Remove a queue item.", "remove queue item", lambda service, params: service.remove_queue_item(int(params["index"])), required_fields=("index",), advanced_only=True),
    _definition("clear_queue", "Clear the playback queue.", "clear queue", lambda service, params: service.clear_queue(), advanced_only=True),
    _definition("play_url", "Play a URL directly.", "play url", lambda service, params: service.play_url(str(params["url"])), required_fields=("url",), advanced_only=True),
    _definition("play_item", "Play a specific item by id.", "play item", lambda service, params: service.play_item(str(params["item_id"]), kind=str(params.get("kind", "songs")), is_library=bool(params.get("is_library", False))), required_fields=("item_id",), advanced_only=True),
    _definition("play_item_href", "Play a specific item by href.", "play item href", lambda service, params: service.play_item_href(str(params["href"])), required_fields=("href",), advanced_only=True),
    _definition("play_session", "Start an adaptive session for a music request.", "play session", lambda service, params: service.play_session(str(params["request"])), required_fields=("request",), session_aware=True),
    _definition("steer_session", "Steer the active adaptive session.", "steer session", lambda service, params: service.steer_session(str(params["request"]), search_update=dict(params.get("search_update", {})) if isinstance(params.get("search_update"), dict) else None), required_fields=("request",), session_aware=True),
    _definition("like_current_track", "Save the current track, artist, and session cue as a positive music preference.", "like current track", lambda service, params: service.like_current_track(), session_aware=True),
    _definition("reject_current_track", "Reject only the current session track.", "reject current track", lambda service, params: service.reject_current_track(), session_aware=True),
    _definition("session_status", "Show adaptive session status.", "session status", lambda service, params: service.session_status(), read_only=True, session_aware=True, text_patterns=(r"^\s*session\s+status\??\s*$",)),
    _definition("stop_session", "Stop the active adaptive session.", "stop session", lambda service, params: service.stop_session(), session_aware=True),
    _definition("refill_session", "Manually advance the active adaptive session.", "refill session", lambda service, params: service.refill_active_session(), session_aware=True),
    _definition("search", "Search using the default source.", "search", lambda service, params: service.search(str(params["query"]), limit=int(params.get("limit", 10)), storefront=str(params.get("storefront", "us"))), required_fields=("query",), read_only=True),
    _definition("search_catalog", "Search the Apple Music catalog.", "search catalog", lambda service, params: service.search_catalog(str(params["query"]), limit=int(params.get("limit", 10)), storefront=str(params.get("storefront", "us"))), required_fields=("query",), read_only=True),
    _definition("search_catalog_tracks", "Search catalog tracks.", "search catalog tracks", lambda service, params: service.search_catalog_tracks(str(params["query"]), limit=int(params.get("limit", 10)), storefront=str(params.get("storefront", "us"))), required_fields=("query",), read_only=True),
    _definition("search_library", "Search the user library.", "search library", lambda service, params: service.search_library(str(params["query"]), limit=int(params.get("limit", 10)), types=list(params.get("types", [])) or None), required_fields=("query",), read_only=True),
    _definition("search_library_tracks", "Search library tracks.", "search library tracks", lambda service, params: service.search_library_tracks(str(params["query"]), limit=int(params.get("limit", 10))), required_fields=("query",), read_only=True),
    _definition("list_library_playlists", "List library playlists.", "list playlists", lambda service, params: service.list_library_playlists(limit=int(params.get("limit", 25)), offset=int(params.get("offset", 0))), read_only=True),
    _definition("search_library_playlists", "Search library playlists.", "search playlists", lambda service, params: service.search_library_playlists(str(params["query"]), limit=int(params.get("limit", 10))), required_fields=("query",), read_only=True),
    _definition("get_library_playlist", "Get a library playlist.", "get playlist", lambda service, params: service.get_library_playlist(str(params["playlist_id"])), required_fields=("playlist_id",), read_only=True),
    _definition("get_library_playlist_tracks", "Get library playlist tracks.", "playlist tracks", lambda service, params: service.get_library_playlist_tracks(str(params["playlist_id"]), limit=int(params.get("limit", 100)), offset=int(params.get("offset", 0))), required_fields=("playlist_id",), read_only=True),
    _definition("play_library_playlist", "Play a library playlist by name.", "play playlist", lambda service, params: service.play_library_playlist(str(params["playlist_name"])), required_fields=("playlist_name",)),
    _definition("list_recently_played", "List recently played tracks.", "recently played", lambda service, params: service.list_recently_played(limit=int(params.get("limit", 25)), offset=int(params.get("offset", 0))), read_only=True),
    _definition("play_search_result", "Play a specific search result.", "play search result", lambda service, params: service.play_search_result(query=str(params["query"]), source=str(params.get("source", "default")), index=int(params.get("index", 0)), storefront=str(params.get("storefront", "us"))), required_fields=("query",)),
    _definition("play_candidate_match", "Resolve candidate tracks, artists, or queries into playback.", "play candidate match", lambda service, params: service.play_candidate_match(candidate_tracks=list(params.get("candidate_tracks", [])) or None, candidate_artists=list(params.get("candidate_artists", [])) or None, candidate_queries=list(params.get("candidate_queries", params.get("candidate_query", []))) or None, storefront=str(params.get("storefront", "us")))),
    _definition("list_preferences", "List explicit music preferences.", "list preferences", lambda service, params: service.list_preferences(), read_only=True, public_exposed=True),
    _definition("forget_preference", "Delete a stored preference.", "forget preference", lambda service, params: service.forget_preference(int(params["preference_id"])), required_fields=("preference_id",), public_exposed=True),
)

ACTION_REGISTRY: dict[str, ActionDefinition] = {definition.name: definition for definition in ACTION_DEFINITIONS}

RESOLVER_ACTION_NAMES: tuple[str, ...] = (
    "status",
    "get_now_playing",
    "get_queue",
    "play",
    "pause",
    "stop",
    "next_track",
    "previous_track",
    "set_volume",
    "play_session",
    "steer_session",
    "like_current_track",
    "reject_current_track",
    "session_status",
    "stop_session",
    "search",
    "list_library_playlists",
    "play_library_playlist",
    "play_search_result",
    "play_candidate_match",
    "list_preferences",
    "forget_preference",
)


def get_action_definition(action: str) -> ActionDefinition | None:
    return ACTION_REGISTRY.get(action)


def require_action_definition(action: str) -> ActionDefinition:
    definition = get_action_definition(action)
    if definition is None:
        raise KeyError(action)
    return definition


def list_action_definitions(*, text_exposable_only: bool = False) -> list[ActionDefinition]:
    definitions = list(ACTION_DEFINITIONS)
    if text_exposable_only:
        definitions = [definition for definition in definitions if definition.text_exposable and not definition.advanced_only]
    return definitions


def list_public_action_definitions() -> list[ActionDefinition]:
    return [definition for definition in ACTION_DEFINITIONS if definition.public_exposed]


def is_public_action(action: str) -> bool:
    definition = get_action_definition(action)
    return bool(definition and definition.public_exposed)


def list_resolver_action_definitions() -> list[ActionDefinition]:
    return [ACTION_REGISTRY[name] for name in RESOLVER_ACTION_NAMES]


def match_text_action_definition(text: str) -> ActionDefinition | None:
    for definition in ACTION_DEFINITIONS:
        if definition.matches_text(text):
            return definition
    return None
