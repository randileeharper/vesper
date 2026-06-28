"""Compact output rendering for :class:`vesper.service.CiderAgentService`.

Pure dict-in/dict-out transforms extracted out of the service class so that
response shaping lives in one cohesive module instead of inline on the
~3300-line god-class.

These helpers were previously ``_compact_*`` / ``_finalize_output`` /
``_summarize_execution`` / ``_looks_like_*`` methods on ``CiderAgentService``.
The only thing that kept them from being trivially pure was two settings
flags; they are now threaded in explicitly so every function here is free of
``self`` / hidden state:

* ``response_detail`` (``"compact"`` vs ``"debug"``) - gates whether
  :func:`finalize_output` / :func:`compact_resolved_action` compact at all.
* ``include_timing_debug`` - gates whether session-refill timings are kept,
  and is threaded through :func:`compact_output`'s recursion.
"""

from __future__ import annotations

from typing import Any, Callable


# --- leaf compacters ---------------------------------------------------------


def compact_track(track: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in (
        "title",
        "artist",
        "album",
        "duration_millis",
    ):
        if key in track:
            compact[key] = track[key]
    return compact


def compact_music_preference(preference: dict[str, Any]) -> dict[str, Any]:
    preference_type = preference.get("preference_type")
    if preference_type == "favored_artist":
        keys: tuple[str, ...] = ("artist_name",)
    else:
        keys = ("title", "artist_name")
    return {key: preference[key] for key in keys if preference.get(key) is not None}


def compact_playlist(playlist: dict[str, Any]) -> dict[str, Any]:
    return {
        key: playlist[key]
        for key in ("id", "type", "href", "name", "description", "can_edit", "is_public")
        if key in playlist
    }


def compact_album(album: dict[str, Any]) -> dict[str, Any]:
    return {key: album[key] for key in ("id", "type", "href", "name", "artist", "track_count") if key in album}


def compact_artist(artist: dict[str, Any]) -> dict[str, Any]:
    return {key: artist[key] for key in ("id", "type", "href", "name") if key in artist}


def compact_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    if "index" in item:
        compact["index"] = item["index"]
    if isinstance(item.get("track"), dict):
        compact["track"] = compact_track(item["track"])
    return compact


def compact_recent_track(track: dict[str, Any]) -> dict[str, Any]:
    return {
        key: track.get(key)
        for key in ("track_id", "title", "artist", "album")
        if key in track
    }


# --- composite compacters ----------------------------------------------------


def compact_session_refill_result(value: dict[str, Any], include_timing_debug: bool) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "status": value.get("status"),
        "selection_strategy": value.get("selection_strategy"),
        "enqueued_count": value.get("enqueued_count", 0),
    }
    playback = value.get("playback")
    if isinstance(playback, dict):
        compact["playback"] = compact_output(playback, include_timing_debug)
    tracks = value.get("tracks")
    if isinstance(tracks, list) and tracks:
        compact["primary_track"] = compact_track(tracks[0]) if isinstance(tracks[0], dict) else tracks[0]
    if include_timing_debug and isinstance(value.get("timings"), dict):
        compact["timings"] = value["timings"]
    return compact


def compact_session_execution(value: dict[str, Any], include_timing_debug: bool) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "status": value.get("status"),
        "mode": value.get("mode"),
    }
    session = value.get("session")
    if isinstance(session, dict):
        compact["session"] = {
            "id": session.get("id"),
            "is_active": session.get("is_active"),
        }
    result = value.get("result")
    if isinstance(result, dict):
        compact["result"] = compact_session_refill_result(result, include_timing_debug)
    return compact


def compact_session_status(value: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "status": value.get("status"),
        "session": None,
    }
    session = value.get("session")
    if isinstance(session, dict):
        compact["session"] = {
            "id": session.get("id"),
            "is_active": session.get("is_active"),
            "mode": session.get("mode"),
        }
    recent_tracks = value.get("recent_tracks")
    if isinstance(recent_tracks, list):
        compact["recent_tracks"] = [compact_recent_track(track) for track in recent_tracks[:5] if isinstance(track, dict)]
    return compact


def compact_play_candidate_result(value: dict[str, Any], include_timing_debug: bool) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "status": value.get("status"),
        "selection_strategy": value.get("selection_strategy"),
    }
    if "selected_query" in value:
        compact["selected_query"] = value.get("selected_query")
    if isinstance(value.get("selected_track"), dict):
        compact["selected_track"] = compact_track(value["selected_track"])
    if isinstance(value.get("playback"), dict):
        compact["playback"] = compact_output(value["playback"], include_timing_debug)
    return compact


# --- shape guards ------------------------------------------------------------


def looks_like_track(value: dict[str, Any]) -> bool:
    return "title" in value and ("artist" in value or "play_params" in value or "track_id" in value)


def looks_like_music_preference(value: dict[str, Any]) -> bool:
    return value.get("preference_type") in {
        "liked_track",
        "favored_artist",
        "globally_rejected_track",
    }


def looks_like_playlist(value: dict[str, Any]) -> bool:
    return "name" in value and ("can_edit" in value or "is_public" in value) and "title" not in value


def looks_like_album(value: dict[str, Any]) -> bool:
    return "name" in value and "track_count" in value and "title" not in value


def looks_like_artist(value: dict[str, Any]) -> bool:
    return "name" in value and "track_count" not in value and "can_edit" not in value and "title" not in value and "artist" not in value


def looks_like_session_execution(value: dict[str, Any]) -> bool:
    return "mode" in value and "session" in value and "result" in value


def looks_like_session_status(value: dict[str, Any]) -> bool:
    return "session" in value and "recent_tracks" in value and "mode" not in value


def looks_like_play_candidate_result(value: dict[str, Any]) -> bool:
    return "selection_strategy" in value and ("selected_track" in value or "selected_query" in value)


# Ordered predicate -> compacter table consulted by ``compact_output`` for dict
# inputs. ORDER IS LOAD-BEARING: the first matching predicate wins, which is how
# ambiguous shapes are disambiguated.
#
#   * Session-shaped envelopes (execution, status, play-candidate-result) are
#     tested first, before leaf shapes, so a top-level result that happens to
#     contain a ``title``/``artist`` track inside it isn't miscompacted as a
#     bare track.
#   * Among leaves, music preferences precede tracks because a liked-track
#     preference carries ``title`` + ``artist_name`` and would otherwise match
#     ``looks_like_track`` (which keys on ``title`` + ``artist``/``play_params``/
#     ``track_id``).
#   * Playlist/album/artist are ordered by specificity (the presence of
#     ``track_count``/``can_edit`` narrows them) and all exclude ``title`` so a
#     track is never misread as a catalog entity.
#
# Each entry is ``(predicate, compacter, pass_timing)``. ``pass_timing`` is True
# for compacters that thread ``include_timing_debug`` (session envelopes) and
# False for leaf compacters that only need the value.
#
# To add a new result shape, append an entry in the position that preserves
# these disambiguation rules rather than adding another ``if looks_like_*(value)``
# branch to ``compact_output``.
_SHAPE_DISPATCH: tuple[tuple[Any, Callable[..., dict[str, Any]], bool], ...] = (
    (looks_like_session_execution, compact_session_execution, True),
    (looks_like_session_status, compact_session_status, False),
    (looks_like_play_candidate_result, compact_play_candidate_result, True),
    (looks_like_music_preference, compact_music_preference, False),
    (looks_like_track, compact_track, False),
    (looks_like_playlist, compact_playlist, False),
    (looks_like_album, compact_album, False),
    (looks_like_artist, compact_artist, False),
)


def compact_output(value: Any, include_timing_debug: bool) -> Any:
    if isinstance(value, list):
        return [compact_output(item, include_timing_debug) for item in value]
    if not isinstance(value, dict):
        return value

    for predicate, compacter, pass_timing in _SHAPE_DISPATCH:
        if predicate(value):
            return compacter(value, include_timing_debug) if pass_timing else compacter(value)

    compact: dict[str, Any] = {}
    for key, item in value.items():
        if key == "raw":
            continue
        if key in {"tracks", "candidate_tracks"} and isinstance(item, list):
            compact[key] = [compact_track(track) if isinstance(track, dict) else track for track in item]
            continue
        if key in {"selected_track", "track"} and isinstance(item, dict):
            compact[key] = compact_track(item)
            continue
        if key == "items" and isinstance(item, list):
            compact[key] = [compact_queue_item(queue_item) for queue_item in item]
            continue
        if key == "playlists" and isinstance(item, list):
            compact[key] = [compact_playlist(playlist) if isinstance(playlist, dict) else playlist for playlist in item]
            continue
        if key == "playlist" and isinstance(item, dict):
            compact[key] = compact_playlist(item)
            continue
        if key == "albums" and isinstance(item, list):
            compact[key] = [compact_album(album) if isinstance(album, dict) else album for album in item]
            continue
        if key == "artists" and isinstance(item, list):
            compact[key] = [compact_artist(artist) if isinstance(artist, dict) else artist for artist in item]
            continue
        compact[key] = compact_output(item, include_timing_debug)
    return compact


SummaryHandler = Callable[[dict[str, Any], str], str | None]


def _summarize_status(result: dict[str, Any], action: str) -> str | None:
    playback = result.get("playback", {})
    if isinstance(playback, dict):
        track = playback.get("track", {})
        if isinstance(track, dict) and track.get("title") and track.get("artist"):
            state = "playing" if playback.get("is_playing") else "paused"
            return f"{state}: {track['title']} by {track['artist']}"
    return "status updated"


def _summarize_session_advance(result: dict[str, Any], action: str) -> str | None:
    payload = result.get("result", result)
    if isinstance(payload, dict):
        tracks = payload.get("tracks")
        if isinstance(tracks, list) and tracks and isinstance(tracks[0], dict):
            title = tracks[0].get("title")
            artist = tracks[0].get("artist")
            if title and artist:
                return f"playing {title} by {artist}"
        primary = payload.get("primary_track")
        if isinstance(primary, dict) and primary.get("title") and primary.get("artist"):
            return f"playing {primary['title']} by {primary['artist']}"
    if result.get("rejected_track_id"):
        return "rejected current track and skipped ahead"
    return "session advanced"


def _summarize_like_current_track(result: dict[str, Any], action: str) -> str | None:
    liked_track = result.get("liked_track", {})
    if isinstance(liked_track, dict) and liked_track.get("title") and liked_track.get("artist_name"):
        return f"saved {liked_track['title']} by {liked_track['artist_name']}"
    return "saved current track preference"


def _summarize_steer_session(result: dict[str, Any], action: str) -> str | None:
    payload = result.get("result", result)
    if isinstance(payload, dict):
        playback = payload.get("playback", {})
        if isinstance(playback, dict):
            track = playback.get("track", {})
            if isinstance(track, dict) and track.get("title") and track.get("artist"):
                return f"updated session steering; keeping {track['title']} by {track['artist']}"
    return "updated session steering"


def _summarize_transport(result: dict[str, Any], action: str) -> str | None:
    return action.replace("playpause", "toggled playback")


def _summarize_get_now_playing(result: dict[str, Any], action: str) -> str | None:
    track = result.get("track", {})
    if isinstance(track, dict) and track.get("title") and track.get("artist"):
        return f"now playing {track['title']} by {track['artist']}"
    return None


def _summarize_list_library_playlists(result: dict[str, Any], action: str) -> str | None:
    count = result.get("count")
    if count is not None:
        return f"found {count} playlists"
    return None


def _summarize_play_library_playlist(result: dict[str, Any], action: str) -> str | None:
    playlist = result.get("playlist", {})
    if isinstance(playlist, dict) and playlist.get("name"):
        return f"playing playlist {playlist['name']}"
    return None


def _summarize_search(result: dict[str, Any], action: str) -> str | None:
    count = result.get("count")
    query = result.get("query")
    if query is not None and count is not None:
        return f"found {count} results for {query}"
    return None


# Action name(s) -> summary builder. Each builder takes the result dict and the
# action name, and returns a summary string, or None to fall through to the
# default ``action or "completed"``. Adding a new action's summary is a one-line
# entry here instead of another elif branch in ``summarize_execution``.
_SUMMARY_HANDLERS: tuple[tuple[tuple[str, ...], SummaryHandler], ...] = (
    (("status",), _summarize_status),
    (("play_session", "refill_session", "reject_current_track", "next_track"), _summarize_session_advance),
    (("like_current_track",), _summarize_like_current_track),
    (("steer_session",), _summarize_steer_session),
    (("play", "pause", "playpause", "stop"), _summarize_transport),
    (("get_now_playing",), _summarize_get_now_playing),
    (("list_library_playlists",), _summarize_list_library_playlists),
    (("play_library_playlist",), _summarize_play_library_playlist),
    (("search", "search_catalog", "search_library", "search_catalog_tracks", "search_library_tracks"), _summarize_search),
)


def _summary_default(action: str) -> str:
    return action or "completed"


def summarize_execution(execution: dict[str, Any]) -> str:
    action = str(execution.get("action", "")).strip()
    result = execution.get("result", {})
    if not isinstance(result, dict):
        return _summary_default(action)
    for names, handler in _SUMMARY_HANDLERS:
        if action in names:
            summary = handler(result, action)
            if summary is not None:
                return summary
    return _summary_default(action)


# --- settings-gated entry points --------------------------------------------


def finalize_output(payload: Any, response_detail: str, include_timing_debug: bool) -> Any:
    if response_detail == "debug":
        return payload
    return compact_output(payload, include_timing_debug)


def compact_resolved_action(action: str, parameters: dict[str, Any], response_detail: str) -> dict[str, Any]:
    if response_detail == "debug":
        return {
            "action": action,
            "parameters": parameters,
        }
    return {"action": action}
