"""Focused tests for multi-source session planning and interleaved queue
materialization (issue #24).

These cover the five behaviors the issue calls out:

- multi-source plan parsing (resolver returns >1 typed source, preserved up to cap);
- initial multi-artist session start (two artist sources both looked up and interleaved);
- additive steering plus interleaving (new source rows woven into the remaining queue);
- refill/rebuild preserving multiple active sources (empty-queue replan keeps all sources);
- duplicate suppression across sources (same track ID in two sources appears once).

The interleave algorithm itself is covered as pure unit tests on the mixin so the
weaving semantics (even decks, uneven decks, empty decks, duplicate IDs) are pinned
independently of the resolver/HTTP machinery.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from vesper.config import Settings
from vesper.resolver import (
    OpenAICompatibleResolver,
    ResolvedAction,
    SessionQueryPlan,
    SessionSearchSource,
)
from vesper.session_queue import SessionQueueMixin
from vesper.service import _clean_id


def _track(track_id: str, title: str, artist: str) -> dict[str, Any]:
    return {
        "id": track_id,
        "title": title,
        "artist": artist,
        "album": "Album",
        "play_params": {"id": track_id, "kind": "songs", "is_library": False},
    }


def _artist_tracks(source: SessionSearchSource, ids: list[str]) -> list[dict[str, Any]]:
    """Tracks for an artist source, one per id, named after the source term."""
    return [
        {
            "id": tid,
            "attributes": {"name": f"{source.term} {i}", "artistName": source.term},
            "play_params": {"id": tid, "kind": "songs", "is_library": False},
        }
        for i, tid in enumerate(ids, start=1)
    ]


def _patch_fetch_by_source(service, monkeypatch, tracks_by_term: dict[str, list[dict[str, Any]]]):
    """Patch the host-level catalog fetch so each artist source term returns
    its canned track list, regardless of catalog search details."""

    def fetch(session, source):
        tracks = tracks_by_term.get(source.term, [])
        return list(tracks), {"resolved_artist_id": source.term, "resolved_name": source.term}

    monkeypatch.setattr(service, "_fetch_session_source_results", fetch)


# ---------------------------------------------------------------------------
# Pure unit tests for the interleave algorithm itself.
# ---------------------------------------------------------------------------


class _QueueMixinHarness(SessionQueueMixin):
    """Minimal harness exposing the interleave/group helpers without a host.

    ``_interleave_decks`` and ``_group_queue_items_by_source`` are the pure
    weaving logic; ``_group_queue_items_by_source`` relies on the source-key
    helpers from SessionSourcesMixin, which only touch their arguments.
    """

    def __init__(self) -> None:
        pass  # Bypass SessionEngine.__init__; the pure helpers need no state.


def _deck(kind: str, term: str, ids: list[str]) -> list[dict[str, Any]]:
    source = {"kind": kind, "term": term}
    return [
        {"source": source, "source_key": f"{kind}:{term.casefold()}", "track": {"id": tid, "title": tid}}
        for tid in ids
    ]


def test_interleave_round_robins_even_decks_preserving_per_deck_order() -> None:
    harness = _QueueMixinHarness()
    deck_a = _deck("artist", "Nirvana", ["n1", "n2", "n3"])
    deck_b = _deck("artist", "Nine Inch Nails", ["t1", "t2", "t3"])

    result = harness._interleave_decks([deck_a, deck_b])

    assert [item["track"]["id"] for item in result] == ["n1", "t1", "n2", "t2", "n3", "t3"]
    # Per-source ordering within each deck is preserved.
    assert [item["track"]["id"] for item in result if item["track"]["id"].startswith("n")] == ["n1", "n2", "n3"]
    assert [item["track"]["id"] for item in result if item["track"]["id"].startswith("t")] == ["t1", "t2", "t3"]


def test_interleave_handles_uneven_decks() -> None:
    harness = _QueueMixinHarness()
    deck_a = _deck("artist", "Nirvana", ["n1", "n2", "n3", "n4"])
    deck_b = _deck("artist", "Portishead", ["p1"])
    deck_c = _deck("artist", "Nine Inch Nails", ["t1", "t2"])

    result = harness._interleave_decks([deck_a, deck_b, deck_c])

    # Round-robin: take one from each deck per round; shorter decks drop out.
    assert [item["track"]["id"] for item in result] == [
        "n1", "p1", "t1",
        "n2", "t2",
        "n3",
        "n4",
    ]


def test_interleave_single_deck_returned_unchanged() -> None:
    harness = _QueueMixinHarness()
    deck = _deck("artist", "Nirvana", ["n1", "n2"])

    result = harness._interleave_decks([deck])

    assert [item["track"]["id"] for item in result] == ["n1", "n2"]


def test_interleave_empty_decks_ignored() -> None:
    harness = _QueueMixinHarness()
    deck_a = _deck("artist", "Nirvana", ["n1", "n2"])
    empty_deck: list[dict[str, Any]] = []
    deck_b = _deck("artist", "Portishead", ["p1"])

    result = harness._interleave_decks([deck_a, empty_deck, deck_b])

    # The empty deck contributes nothing; the two non-empty decks interleave.
    assert [item["track"]["id"] for item in result] == ["n1", "p1", "n2"]


def test_interleave_no_decks_returns_empty() -> None:
    harness = _QueueMixinHarness()

    assert harness._interleave_decks([]) == []
    assert harness._interleave_decks([[]]) == []


# ---------------------------------------------------------------------------
# Resolver plan parsing: multiple typed sources preserved up to the cap.
# ---------------------------------------------------------------------------


def _make_resolver_settings(settings) -> Settings:
    return Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend="openai_compatible",
        resolver_base_url="https://resolver.example/v1",
        resolver_model="gpt-test",
        resolver_api_key="secret",
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )


def test_multi_source_plan_preserves_multiple_typed_sources(settings, service) -> None:
    """A plan returning two distinct artist sources keeps both, in order."""
    resolver_settings = _make_resolver_settings(settings)

    class MultiArtistTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "search_sources": [
                                        {"kind": "artist", "term": "Nirvana"},
                                        {"kind": "artist", "term": "Nine Inch Nails"},
                                    ],
                                    "queue_policy": "source_order",
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=MultiArtistTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    plan = resolver.plan_session(
        "play a mix of nirvana and nine inch nails",
        service,
        {"request_text": "play a mix of nirvana and nine inch nails"},
        4,
    )

    assert [(s.kind, s.term) for s in plan.search_sources] == [
        ("artist", "Nirvana"),
        ("artist", "Nine Inch Nails"),
    ]
    assert plan.queue_policy == "source_order"


# ---------------------------------------------------------------------------
# Initial multi-artist session start: both sources looked up and interleaved.
# ---------------------------------------------------------------------------


class _TwoArtistSessionResolver:
    def resolve(self, text: str, service: Any) -> ResolvedAction:
        return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int):
        return SessionQueryPlan(
            search_sources=[
                SessionSearchSource(kind="artist", term="Nirvana"),
                SessionSearchSource(kind="artist", term="Nine Inch Nails"),
            ],
            resolver="stub",
        )


def test_initial_multi_artist_start_interleaves_both_sources(service, monkeypatch) -> None:
    """An initial request that plans two artist sources fans out into two
    real catalog lookups whose results are interleaved into one queue."""
    service._resolver = _TwoArtistSessionResolver()
    _patch_fetch_by_source(
        service,
        monkeypatch,
        {
            "Nirvana": _artist_tracks(SessionSearchSource(kind="artist", term="Nirvana"), ["n1", "n2"]),
            "Nine Inch Nails": _artist_tracks(SessionSearchSource(kind="artist", term="Nine Inch Nails"), ["t1", "t2"]),
        },
    )

    service.play_session("play a mix of nirvana and nine inch nails")

    session = service._preferences.get_active_session()
    queue = service._preferences.list_session_queue(session["id"], limit=100)

    # Both sources contributed rows to a single materialized queue.
    sources = {item.get("source", {}).get("term") for item in queue}
    assert sources == {"Nirvana", "Nine Inch Nails"}

    # Rows alternate across the two sources (round-robin interleave).
    terms = [item.get("source", {}).get("term") for item in queue]
    assert terms == ["Nirvana", "Nine Inch Nails", "Nirvana", "Nine Inch Nails"]


# ---------------------------------------------------------------------------
# Duplicate suppression across sources.
# ---------------------------------------------------------------------------


class _DuplicateSuppressingResolver:
    """Plans two artist sources that both resolve to the same track IDs so
    cross-source duplicate suppression can be verified end-to-end."""

    def resolve(self, text: str, service: Any) -> ResolvedAction:
        return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int):
        return SessionQueryPlan(
            search_sources=[
                SessionSearchSource(kind="artist", term="Nirvana"),
                SessionSearchSource(kind="artist", term="Foo Fighters"),
            ],
            resolver="stub",
        )


def test_duplicate_track_ids_across_sources_appear_once(service, monkeypatch) -> None:
    """Both artist sources resolve to overlapping top-song track IDs; the
    materialized queue must keep each ID only once, crediting the first source."""
    service._resolver = _DuplicateSuppressingResolver()
    shared = [
        {"id": "shared-1", "attributes": {"name": "Shared One"}, "play_params": {"id": "shared-1"}},
        {"id": "shared-2", "attributes": {"name": "Shared Two"}, "play_params": {"id": "shared-2"}},
    ]
    _patch_fetch_by_source(
        service,
        monkeypatch,
        {"Nirvana": shared, "Foo Fighters": shared},
    )

    service.play_session("play a mix of nirvana and foo fighters")

    queue = service._preferences.list_session_queue(
        service._preferences.get_active_session()["id"], limit=100
    )
    track_ids = [_clean_id(item.get("track", {}).get("id")) for item in queue]

    # Each shared ID appears exactly once across the combined queue.
    assert track_ids.count("shared-1") == 1
    assert track_ids.count("shared-2") == 1
    assert len(track_ids) == len(set(track_ids))


# ---------------------------------------------------------------------------
# Refill / rebuild preserving multiple active sources.
# ---------------------------------------------------------------------------


class _MultiSourcePlanResolver:
    """Resolver that plans two artist sources for every plan call."""

    def resolve(self, text: str, service: Any) -> ResolvedAction:
        return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int):
        return SessionQueryPlan(
            search_sources=[
                SessionSearchSource(kind="artist", term="Nirvana"),
                SessionSearchSource(kind="artist", term="Portishead"),
            ],
            resolver="stub",
        )


def test_refill_preserves_multiple_active_sources(service, monkeypatch) -> None:
    """After a multi-source start, an empty-queue rebuild must keep both active
    sources instead of collapsing to the first one (issue #24 rebuild path)."""
    service._resolver = _MultiSourcePlanResolver()
    _patch_fetch_by_source(
        service,
        monkeypatch,
        {
            "Nirvana": _artist_tracks(SessionSearchSource(kind="artist", term="Nirvana"), ["n1", "n2"]),
            "Portishead": _artist_tracks(SessionSearchSource(kind="artist", term="Portishead"), ["p1", "p2"]),
        },
    )

    service.play_session("play a mix of nirvana and portishead")

    session = service._preferences.get_active_session()
    runtime = service._get_session_runtime(session["id"])
    active_sources = service._session._normalize_search_sources(runtime.get("active_search_sources"))

    # Both sources survived into active runtime state after the multi-source start.
    assert len(active_sources) == 2
    assert [s.term for s in active_sources] == ["Nirvana", "Portishead"]

    # An empty-queue rebuild path (_plan_session_query with active sources present)
    # must preserve the full multi-source mix rather than slicing to count=1.
    plan = service._session._plan_session_query(session, count=1)
    rebuilt_sources = service._session._plan_search_sources(plan)
    assert [s.term for s in rebuilt_sources] == ["Nirvana", "Portishead"]


# ---------------------------------------------------------------------------
# Additive steering plus interleaving.
# ---------------------------------------------------------------------------


class _SingleArtistStartResolver:
    """Plans one artist source for the initial start; additive steering adds
    a second artist source via search_update.mode=add."""

    def resolve(self, text: str, service: Any) -> ResolvedAction:
        return ResolvedAction(action="play_session", parameters={"request": text}, resolver="stub")

    def plan_session(self, request: str, service: Any, session: dict[str, Any], count: int):
        return SessionQueryPlan(
            search_sources=[SessionSearchSource(kind="artist", term="Nirvana")],
            resolver="stub",
        )


def test_additive_steering_interleaves_new_source_into_remaining_queue(service, monkeypatch) -> None:
    """Starting with one artist and adding a second via steering must weave the
    new source's rows through the *remaining* queue, not append them after it."""
    service._resolver = _SingleArtistStartResolver()
    _patch_fetch_by_source(
        service,
        monkeypatch,
        {
            "Nirvana": _artist_tracks(SessionSearchSource(kind="artist", term="Nirvana"), ["n1", "n2", "n3"]),
            "Nine Inch Nails": _artist_tracks(SessionSearchSource(kind="artist", term="Nine Inch Nails"), ["t1", "t2", "t3"]),
        },
    )

    service.play_session("play some nirvana")

    session = service._preferences.get_active_session()
    queue_before = service._preferences.list_session_queue(session["id"], limit=100)
    # The start queue is all Nirvana, in order.
    assert [item.get("source", {}).get("term") for item in queue_before] == [
        "Nirvana", "Nirvana", "Nirvana"
    ]

    # Mark the first item as playing so it leaves the remaining queue; the
    # second and third Nirvana rows should stay and be interleaved with the
    # newly added Nine Inch Nails rows.
    first_item_id = queue_before[0]["id"]
    service._preferences.mark_session_queue_item(first_item_id, "playing")
    service._session._set_session_runtime(session["id"], current_queue_item_id=first_item_id)

    service.steer_session(
        "add some nine inch nails",
        search_update={"mode": "add", "sources": [{"kind": "artist", "term": "Nine Inch Nails"}]},
    )

    queue_after = service._preferences.list_session_queue(session["id"], limit=100)
    terms = [item.get("source", {}).get("term") for item in queue_after if item.get("state") == "queued"]

    # The new source contributed its rows, and they were woven through the
    # remaining Nirvana rows rather than appended after them.
    assert terms.count("Nine Inch Nails") == 3
    assert terms.count("Nirvana") == 2
    # Interleaving means a Nine Inch Nails row appears before the last Nirvana row.
    last_nir_index = max(i for i, t in enumerate(terms) if t == "Nirvana")
    first_nin_index = terms.index("Nine Inch Nails")
    assert first_nin_index < last_nir_index
