"""Characterization tests for the output-rendering helpers in ``vesper.output``.

These pin the exact behavior of the ``compact_*`` / ``finalize_output`` /
``summarize_execution`` / ``looks_like_*`` helpers. The expected values were
first captured against the original methods on ``CiderAgentService`` and are
unchanged after the extraction into ``vesper/output.py`` - only the call
target moved from ``self._<helper>`` to a module function - so a green run
here proves the extraction is behavior-preserving.
"""

from __future__ import annotations

from vesper.output import (
    compact_album,
    compact_artist,
    compact_music_preference,
    compact_output,
    compact_play_candidate_result,
    compact_playlist,
    compact_queue_item,
    compact_recent_track,
    compact_resolved_action,
    compact_session_execution,
    compact_session_refill_result,
    compact_session_status,
    compact_track,
    finalize_output,
    looks_like_album,
    looks_like_artist,
    looks_like_play_candidate_result,
    looks_like_playlist,
    looks_like_session_execution,
    looks_like_session_status,
    looks_like_track,
    summarize_execution,
)

TIMING_OFF = False
TIMING_ON = True


# --- leaf compacters ------------------------------------------------------


def test_compact_track_drops_unwanted_keys():
    track = {"title": "T", "artist": "A", "album": "Al", "duration_millis": 1000, "id": "x", "type": "songs"}
    assert compact_track(track) == {"title": "T", "artist": "A", "album": "Al", "duration_millis": 1000}


def test_compact_music_preference_favored_artist():
    pref = {"preference_type": "favored_artist", "artist_name": "Pink", "title": "ignored"}
    assert compact_music_preference(pref) == {"artist_name": "Pink"}


def test_compact_music_preference_liked_track():
    pref = {"preference_type": "liked_track", "title": "T", "artist_name": "A", "track_id": "x"}
    assert compact_music_preference(pref) == {"title": "T", "artist_name": "A"}


def test_compact_playlist():
    playlist = {"id": "p1", "type": "library-playlists", "name": "Mix", "foo": "bar"}
    assert compact_playlist(playlist) == {"id": "p1", "type": "library-playlists", "name": "Mix"}


def test_compact_album():
    album = {"id": "a1", "name": "Al", "artist": "Ar", "track_count": 5, "extra": 1}
    assert compact_album(album) == {"id": "a1", "name": "Al", "artist": "Ar", "track_count": 5}


def test_compact_artist():
    artist = {"id": "ar1", "name": "Ar", "href": "h"}
    assert compact_artist(artist) == {"id": "ar1", "name": "Ar", "href": "h"}


def test_compact_queue_item():
    item = {"index": 2, "track": {"title": "T", "artist": "A", "raw": {}}, "container": {"id": "c"}}
    assert compact_queue_item(item) == {"index": 2, "track": {"title": "T", "artist": "A"}}


def test_compact_recent_track():
    track = {"track_id": "x", "title": "T", "artist": "A", "album": "Al", "ts": 123}
    assert compact_recent_track(track) == {"track_id": "x", "title": "T", "artist": "A", "album": "Al"}


# --- compact_output dispatch + recursion ----------------------------------


def test_compact_output_dispatches_track():
    value = {"title": "T", "artist": "A", "album": "Al", "duration_millis": 1, "id": "x"}
    assert compact_output(value, TIMING_OFF) == {"title": "T", "artist": "A", "album": "Al", "duration_millis": 1}


def test_compact_output_dispatches_playlist_album_artist():
    assert compact_output({"name": "Mix", "can_edit": True, "id": "p1"}, TIMING_OFF) == {
        "name": "Mix",
        "can_edit": True,
        "id": "p1",
    }
    assert compact_output({"name": "Al", "track_count": 5, "id": "a1"}, TIMING_OFF) == {
        "name": "Al",
        "track_count": 5,
        "id": "a1",
    }
    assert compact_output({"name": "Ar", "id": "ar1"}, TIMING_OFF) == {"name": "Ar", "id": "ar1"}


def test_compact_output_dispatches_music_preference():
    value = {"preference_type": "liked_track", "title": "T", "artist_name": "A", "track_id": "x"}
    assert compact_output(value, TIMING_OFF) == {"title": "T", "artist_name": "A"}


def test_compact_output_dispatches_session_status():
    value = {
        "status": "ok",
        "session": {"id": "s1", "is_active": True, "mode": "radio"},
        "recent_tracks": [{"track_id": "x", "title": "T", "artist": "A", "album": "Al", "ts": 9}],
    }
    assert compact_output(value, TIMING_OFF) == {
        "status": "ok",
        "session": {"id": "s1", "is_active": True, "mode": "radio"},
        "recent_tracks": [{"track_id": "x", "title": "T", "artist": "A", "album": "Al"}],
    }


def test_compact_output_dispatches_session_execution():
    value = {
        "status": "ok",
        "mode": "radio",
        "session": {"id": "s1", "is_active": True},
        "result": {
            "status": "ok",
            "selection_strategy": "seeded",
            "enqueued_count": 2,
            "tracks": [{"title": "T", "artist": "A", "id": "x"}],
        },
    }
    assert compact_output(value, TIMING_OFF) == {
        "status": "ok",
        "mode": "radio",
        "session": {"id": "s1", "is_active": True},
        "result": {
            "status": "ok",
            "selection_strategy": "seeded",
            "enqueued_count": 2,
            "primary_track": {"title": "T", "artist": "A"},
        },
    }


def test_compact_output_dispatches_play_candidate_result():
    value = {
        "status": "ok",
        "selection_strategy": "seeded",
        "selected_query": "q",
        "selected_track": {"title": "T", "artist": "A", "id": "x"},
    }
    assert compact_output(value, TIMING_OFF) == {
        "status": "ok",
        "selection_strategy": "seeded",
        "selected_query": "q",
        "selected_track": {"title": "T", "artist": "A"},
    }


def test_compact_output_generic_dict_with_keyed_collections():
    value = {
        "tracks": [{"title": "T", "artist": "A", "id": "x"}],
        "candidate_tracks": [{"title": "C", "artist": "CA"}],
        "items": [{"index": 0, "track": {"title": "Q", "artist": "QA", "raw": {}}, "container": {}}],
        "playlists": [{"name": "P", "can_edit": True, "id": "p1"}, "skip-me"],
        "albums": [{"name": "Al", "track_count": 3, "id": "a1"}],
        "artists": [{"name": "Ar", "id": "ar1"}],
        "playlist": {"name": "PL", "can_edit": False, "id": "p2"},
        "selected_track": {"title": "ST", "artist": "SA", "id": "x"},
        "track": {"title": "TR", "artist": "TA"},
        "count": 3,
        "raw": {"secret": True},
    }
    assert compact_output(value, TIMING_OFF) == {
        "tracks": [{"title": "T", "artist": "A"}],
        "candidate_tracks": [{"title": "C", "artist": "CA"}],
        "items": [{"index": 0, "track": {"title": "Q", "artist": "QA"}}],
        "playlists": [{"name": "P", "can_edit": True, "id": "p1"}, "skip-me"],
        "albums": [{"name": "Al", "track_count": 3, "id": "a1"}],
        "artists": [{"name": "Ar", "id": "ar1"}],
        "playlist": {"name": "PL", "can_edit": False, "id": "p2"},
        "selected_track": {"title": "ST", "artist": "SA"},
        "track": {"title": "TR", "artist": "TA"},
        "count": 3,
    }


def test_compact_output_recurses_into_lists():
    value = [{"title": "T", "artist": "A", "id": "x"}, "plain", 7]
    assert compact_output(value, TIMING_OFF) == [{"title": "T", "artist": "A"}, "plain", 7]


def test_compact_output_passes_through_scalars():
    assert compact_output("text", TIMING_OFF) == "text"
    assert compact_output(7, TIMING_OFF) == 7
    assert compact_output(None, TIMING_OFF) is None


# --- timing-gated refill result -------------------------------------------


def test_compact_session_refill_result_omits_timings_when_disabled():
    value = {"status": "ok", "selection_strategy": "seeded", "enqueued_count": 2, "timings": {"resolve_ms": 5}}
    assert compact_session_refill_result(value, TIMING_OFF) == {
        "status": "ok",
        "selection_strategy": "seeded",
        "enqueued_count": 2,
    }


def test_compact_session_refill_result_includes_timings_when_enabled():
    value = {"status": "ok", "selection_strategy": "seeded", "enqueued_count": 2, "timings": {"resolve_ms": 5}}
    assert compact_session_refill_result(value, TIMING_ON) == {
        "status": "ok",
        "selection_strategy": "seeded",
        "enqueued_count": 2,
        "timings": {"resolve_ms": 5},
    }


def test_compact_output_propagates_timing_flag_through_session_execution():
    value = {
        "status": "ok",
        "mode": "radio",
        "session": {"id": "s1", "is_active": True},
        "result": {"status": "ok", "selection_strategy": "seeded", "enqueued_count": 1, "timings": {"resolve_ms": 5}},
    }
    assert compact_output(value, TIMING_ON)["result"]["timings"] == {"resolve_ms": 5}
    assert "timings" not in compact_output(value, TIMING_OFF)["result"]


# --- finalize_output / compact_resolved_action ----------------------------


def test_finalize_output_compacts_in_compact_mode():
    payload = {"title": "T", "artist": "A", "id": "x"}
    assert finalize_output(payload, "compact", TIMING_OFF) == {"title": "T", "artist": "A"}


def test_finalize_output_passes_through_in_debug_mode():
    payload = {"title": "T", "artist": "A", "id": "x"}
    assert finalize_output(payload, "debug", TIMING_OFF) == {"title": "T", "artist": "A", "id": "x"}


def test_compact_resolved_action_compact_mode():
    assert compact_resolved_action("play", {"query": "x"}, "compact") == {"action": "play"}


def test_compact_resolved_action_debug_mode():
    assert compact_resolved_action("play", {"query": "x"}, "debug") == {"action": "play", "parameters": {"query": "x"}}


# --- summarize_execution --------------------------------------------------


def test_summarize_execution_status_playing():
    execution = {"action": "status", "result": {"playback": {"is_playing": True, "track": {"title": "T", "artist": "A"}}}}
    assert summarize_execution(execution) == "playing: T by A"


def test_summarize_execution_play_session_tracks():
    execution = {"action": "play_session", "result": {"result": {"tracks": [{"title": "T", "artist": "A"}]}}}
    assert summarize_execution(execution) == "playing T by A"


def test_summarize_execution_like_current_track():
    execution = {"action": "like_current_track", "result": {"liked_track": {"title": "T", "artist_name": "A"}}}
    assert summarize_execution(execution) == "saved T by A"


def test_summarize_execution_play_and_playpause():
    assert summarize_execution({"action": "play", "result": {}}) == "play"
    assert summarize_execution({"action": "playpause", "result": {}}) == "toggled playback"


def test_summarize_execution_search_count():
    execution = {"action": "search", "result": {"count": 3, "query": "q"}}
    assert summarize_execution(execution) == "found 3 results for q"


def test_summarize_execution_unknown_falls_back_to_action():
    assert summarize_execution({"action": "unknown", "result": {}}) == "unknown"


def test_summarize_execution_empty_action():
    assert summarize_execution({"action": "", "result": {}}) == "completed"


# --- composite compacters (direct) ----------------------------------------


def test_compact_session_execution_threads_timing_flag():
    value = {
        "status": "ok",
        "mode": "radio",
        "session": {"id": "s1", "is_active": True},
        "result": {"status": "ok", "selection_strategy": "seeded", "enqueued_count": 1, "timings": {"resolve_ms": 5}},
    }
    assert compact_session_execution(value, TIMING_ON)["result"]["timings"] == {"resolve_ms": 5}
    assert "timings" not in compact_session_execution(value, TIMING_OFF)["result"]


def test_compact_session_status_compacts_session_and_recent_tracks():
    value = {
        "status": "ok",
        "session": {"id": "s1", "is_active": True, "mode": "radio", "extra": 1},
        "recent_tracks": [{"track_id": "x", "title": "T", "artist": "A", "album": "Al"}],
    }
    assert compact_session_status(value) == {
        "status": "ok",
        "session": {"id": "s1", "is_active": True, "mode": "radio"},
        "recent_tracks": [{"track_id": "x", "title": "T", "artist": "A", "album": "Al"}],
    }


def test_compact_play_candidate_result_compacts_selected_track():
    value = {
        "status": "ok",
        "selection_strategy": "seeded",
        "selected_track": {"title": "T", "artist": "A", "id": "x"},
        "playback": {"is_playing": True, "track": {"title": "T", "artist": "A", "id": "x"}},
    }
    assert compact_play_candidate_result(value, TIMING_OFF) == {
        "status": "ok",
        "selection_strategy": "seeded",
        "selected_track": {"title": "T", "artist": "A"},
        "playback": {"is_playing": True, "track": {"title": "T", "artist": "A"}},
    }


# --- looks_like_* guards --------------------------------------------------


def test_looks_like_guards():
    assert looks_like_track({"title": "T", "artist": "A"}) is True
    assert looks_like_track({"album": "Al"}) is False
    assert looks_like_playlist({"name": "P", "can_edit": True}) is True
    assert looks_like_playlist({"title": "T"}) is False
    assert looks_like_album({"name": "Al", "track_count": 3}) is True
    assert looks_like_artist({"name": "Ar"}) is True
    assert looks_like_session_execution({"mode": "radio", "session": {}, "result": {}}) is True
    assert looks_like_session_status({"session": {}, "recent_tracks": []}) is True
    assert looks_like_play_candidate_result({"selection_strategy": "x", "selected_track": {}}) is True


# --- dispatch ordering -----------------------------------------------------


def test_compact_output_compacts_music_preference_before_track():
    # A liked-track preference carries ``title`` + ``artist_name``, which would
    # also match ``looks_like_track`` if the preference predicate did not win.
    # Pin that preference compaction takes precedence.
    value = {"preference_type": "liked_track", "title": "T", "artist_name": "A", "track_id": "x"}
    assert compact_output(value, TIMING_OFF) == {"title": "T", "artist_name": "A"}


def test_compact_output_compacts_session_envelope_before_track():
    # A session-execution envelope contains a nested track-shaped ``result``
    # and itself is not a track; the envelope predicate must win so the whole
    # envelope is compacted rather than the top-level dict being miscompacted as
    # a bare track.
    value = {
        "status": "ok",
        "mode": "radio",
        "session": {"id": "s1", "is_active": True},
        "result": {"status": "ok", "selection_strategy": "seeded", "enqueued_count": 1, "tracks": [{"title": "T", "artist": "A"}]},
    }
    compacted = compact_output(value, TIMING_OFF)
    assert compacted["result"] == {"status": "ok", "selection_strategy": "seeded", "enqueued_count": 1, "primary_track": {"title": "T", "artist": "A"}}


def test_compact_output_generic_dict_falls_through_to_keywise_path():
    # A dict matching no shape guard falls through to the generic key-wise path,
    # which recursively compacts values but preserves scalar keys as-is.
    value = {"count": 3, "query": "q", "ok": True}
    assert compact_output(value, TIMING_OFF) == {"count": 3, "query": "q", "ok": True}


def test_summarize_execution_falls_through_when_handler_returns_none():
    # ``get_now_playing`` with no track data returns None from its handler and
    # must fall through to the default ``action`` rather than crash or coerce.
    assert summarize_execution({"action": "get_now_playing", "result": {}}) == "get_now_playing"
    assert summarize_execution({"action": "list_library_playlists", "result": {}}) == "list_library_playlists"


def test_summarize_execution_non_dict_result_falls_back_to_action():
    assert summarize_execution({"action": "play", "result": ["not", "a", "dict"]}) == "play"
    assert summarize_execution({"action": "", "result": "x"}) == "completed"
