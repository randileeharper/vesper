# Adaptive Sessions, Search, and Preferences

This page answers the practical behavior questions that come up when using or changing Vesper's text-first music flow.

## Session vs. One-Track Playback

Vesper has two different playback modes:

| Mode | What starts it | What happens after the first track? |
| --- | --- | --- |
| One-track/direct playback | Direct commands such as `play`, `pause`, `stop`, `play_search_result`, or a specific candidate match | Vesper performs that action once. Cider may continue its own native queue/autoplay behavior, but Vesper is not actively choosing future tracks. |
| Adaptive session | Text requests resolved to `play_session`, such as `play some music`, `play upbeat morning music`, or artist/vibe/activity requests | Vesper creates an active session, clears Cider's queue, chooses a first real track, records session state, and lets the background worker choose later tracks. |

An adaptive session is therefore not just “play one result.” It is persistent state plus a selection loop.

Starting a new session replaces any existing active session. `stop` stops playback and ends the active session; `stop_session` ends only the session state.

## What Preferences Do

Preferences are explicit music memory stored in SQLite by `PreferenceStore`.

Current preference types:

- `liked_track` — a track the user explicitly liked.
- `favored_artist` — the artist of a liked track, recorded so future vague sessions can seed from that artist.
- `globally_rejected_track` — a track the user explicitly rejected; future session queue materialization filters these out.

### Does “I like this” save a preference?

Yes, when the text resolver is configured and resolves the request to `like_current_track`.

The resolver prompt explicitly says to use `like_current_track` when the user says they like the current song or track. So messages like these are intended to save a preference for the currently playing track:

- `i like this`
- `i like this track`
- `this song is good`

When `like_current_track` runs, Vesper:

1. reads the current Cider playback snapshot;
2. requires a current track ID;
3. upserts a `liked_track` preference;
4. upserts a `favored_artist` preference when the current track has an artist;
5. if a session is active, records a `track_liked` session event;
6. leaves playback running.

If the fallback resolver is the only resolver, broad phrases like `i like this` are not understood; the fallback resolver only handles simple direct commands. In that case, call the structured/internal action through code or enable the OpenAI-compatible resolver.

### How preferences affect future sessions

Preferences are used in two main ways:

1. **Vague-session seeding.** Requests like `play some music` can bootstrap from saved preference cues before asking the resolver to invent a new search direction. Vesper uses liked tracks' previous session queries, favored artists, and liked tracks as seeds.
2. **Avoidance.** Globally rejected tracks are excluded from future session queue rows.

Preferences do not currently act like a global recommender profile for every possible search. They are most important for vague session starts and repeat/rejection avoidance.

Saved preference rows stay local to Vesper's service/storage layer. The OpenAI-compatible resolver may be told that an abstract `preference` source is available for extremely vague sessions, but Vesper does not send the actual saved preference list in `resolve_text_request` or `plan_session_query` prompt context.

View preferences with:

```bash
vesper preferences list
```

Delete one with:

```bash
vesper preferences forget <preference_id>
```

## Search Source Types

Adaptive sessions use typed search sources. A source is `{kind, term}`.

Supported resolver-planned kinds:

| Kind | When the resolver should use it | What Vesper does with it |
| --- | --- | --- |
| `artist` | Artist names, including artist-plus-mood requests. The term should be only the artist name. | Searches Apple Music catalog artists, chooses the exact normalized match or first result, then fetches that artist's top songs. |
| `genre` | Only when the term exactly matches an Apple Music supported genre name loaded from `/genres`. | Fetches Apple Music chart songs for that genre ID. |
| `vibe` | Descriptive requests, activities, moods, unsupported subgenres, genre-plus-mood phrases, and broad contextual requests. | Searches Apple Music catalog playlists for the term, asks the resolver to choose the best playlist, then fetches tracks from that playlist. |

Internal/transitional kinds:

| Kind | Purpose |
| --- | --- |
| `preference` | Synthetic source used for preference-seeded vague sessions. Vesper materializes it locally from liked tracks, favored artists, and preference cues. |
| `legacy` | Compatibility source for older resolver output that only returned query strings. It behaves like catalog track search. |

The planner can also return `queue_policy`:

| Policy | Meaning |
| --- | --- |
| `source_order` | Keep the materialized queue in the order produced by the source lookup. This is the default. |
| `shuffle` | Shuffle the concrete queue rows after materialization. |

There is no built-in interleave policy today. If multiple sources are materialized together, Vesper either keeps their produced order or shuffles the combined concrete rows.

## How the LLM Chooses Search Types

For adaptive-session planning, the OpenAI-compatible resolver receives:

- the original session request;
- recent session steering;
- compact playback state;
- supported Apple Music genre names;
- rejected search sources;
- the current timestamp.

Its planning instruction is constrained:

- use `artist` for artist names;
- use `genre` only for exact supported genre names;
- use `vibe` for moods, activities, unsupported subgenres, descriptive requests, and genre-plus-mood requests;
- use `preference` only as an abstract source for extremely vague requests;
- preserve concrete user descriptors instead of unnecessarily narrowing them;
- use creative interpretation mainly for open-ended/contextual/activity requests;
- do not invent final tracks.

The session data model can store multiple active typed sources, but the built-in OpenAI-compatible planner currently asks for one source at a time for a new session start or replan. Additional sources can still be added later through steering with `search_update.mode = add`.

The resolver returns only the source. Vesper performs the real Apple Music lookup. If the source is `preference`, Vesper locally builds the pool from saved likes and favored artists without exposing those saved preference rows to the resolver.

The planner does not choose the next track during normal session advances. It chooses a source, and Vesper materializes that source into concrete persisted queue rows. Later advances claim the next eligible row from SQLite.

## Current Multi-Source Behavior

The current implementation supports **additive multi-source steering**, but it does **not** yet support a true multi-source session start in the built-in planner.

### Follow-up additive requests

Example:

1. `play some nirvana`
2. `add some nine inch nails`

If the follow-up resolves to `steer_session` with `search_update.mode = add`, Vesper keeps the existing session, looks up the new typed source, and appends concrete queue rows for that new source to the persisted session queue.

What this means in practice:

- Nine Inch Nails results are added to the same Vesper session queue as the Nirvana results.
- The new rows are appended after the existing queued rows; they are not interleaved into the remaining queue.
- The current track is not interrupted. The change is usually audible on later tracks.

### Initial multi-artist or multi-source starts

Example:

- `play a mix of nirvana and nine inch nails`

This is **not currently a true multi-source session start** in the built-in resolver flow. The session engine can materialize multiple sources into one queue, but the built-in session-start planner currently plans at most one starting source. So this request does not reliably trigger two independent artist searches whose results are combined into one queue before playback starts.

### Rebuild and refill limitation

When a session already has multiple active sources because of additive steering, the existing materialized queue can contain rows from all of them. However, future empty-queue rebuilds and replans currently reuse the first active source from runtime rather than rebuilding a balanced multi-source mix. In other words, additive multi-source sessions are real for the current materialized queue, but that behavior is not yet preserved as a first-class session-start/rebuild strategy.

## Is the User's Search Used Verbatim?

It depends on the path.

### Direct search actions

Direct search methods such as `search_catalog_tracks(query)` and `play_search_result(query=...)` use the provided query string for Apple Music search after light validation (whitespace trimming only). These are the closest thing to verbatim search.

The resolver is responsible for query shaping. Resolver-returned search queries, candidate queries, and session sources are used verbatim — Vesper does not strip prefixes like `play`, `find`, `search for`, or `songs by` from resolver output. If the resolver wants a clean search term, it should return one.

### Adaptive sessions

Adaptive sessions are not guaranteed verbatim. The resolver plans a typed source from the user's request. For concrete requests it is instructed to preserve the broad request; for open-ended requests it may take creative license.

Examples:

- `play trip-hop` should stay broad, not become a narrower invented vibe like `atmospheric trip hop` unless the user asked for that.
- `music for cleaning the house` may become a more search-friendly vibe/source.
- `play Beyoncé` should become an `artist` source with term `Beyoncé`.

There is not currently a user-facing “verbatim adaptive session” switch. If you need exact search behavior, use direct search/play-search functionality rather than starting an adaptive session.

## Where Session Queue Items Live

When a session plans a source, Vesper now builds a **materialized session queue**:

```text
search source -> Apple Music lookup -> filtered candidate tracks -> SQLite session queue
```

A session queue item contains:

- the source `{kind, term}`;
- the flattened Apple Music track payload needed for playback;
- its concrete queue position;
- state such as `queued`, `playing`, `played`, `rejected`, `filtered`, or `failed`.

The queue is Vesper's own adaptive-session queue. It is persisted in SQLite and is separate from Cider's native playback queue.

Queue rows move through these states:

| State | Meaning |
| --- | --- |
| `queued` | Eligible for a future session advance. |
| `playing` | Claimed by Vesper and handed to Cider for playback. |
| `played` | Previously playing and superseded by a later session advance. |
| `rejected` | Rejected by the user or marked unavailable for the session. |
| `filtered` | Removed from the future queue by steering/filtering while retained as history. |
| `failed` | Reserved for rows that could not be played. |

SQLite stores durable session data such as:

- sessions and steering history;
- materialized session queue rows;
- selected session tracks;
- session events;
- minimal persisted runtime fields like active/suspended intent, last advance time, last selected track ID, and last known playback state;
- preferences.

Because the future queue is persisted, restarting the service can inspect and continue the same planned queue instead of reconstructing future choices from process-local state.

On startup, Vesper reconciles persisted queue state with current Cider playback. If Cider is still playing the row marked `playing`, the service restores that queue item into runtime state. If playback is stopped, stale `playing` rows are reset so they can be claimed again.

## Can You View the Search Results or Queue?

There are three different things people might call “the queue”:

1. **Cider's native queue** — visible through Vesper's `get_queue` action / `what is the queue?` if the resolver maps it there. This is Cider's playback queue.
2. **Session recent tracks** — selected session tracks persisted in SQLite and shown by `session_status`.
3. **Vesper's session queue** — the concrete future adaptive-session items persisted by Vesper.

Use the developer CLI path to inspect Vesper's session queue:

```sh
vesper session queue --json
vesper session queue --all --limit 100 --json
```

This means: `get_queue` is not the same as “show me every future Vesper session item.” Vesper usually plays selected tracks directly rather than enqueueing the whole session queue into Cider.

## What Happens After a Session Starts?

Starting a session does this:

1. stop/replace any previous active session;
2. create a new active session row in SQLite;
3. clear Cider's queue;
4. plan or seed a search source;
5. build and persist a concrete session queue;
6. claim the first queued item;
7. play the claimed track directly through Cider;
8. record the selected track in SQLite;
9. keep the remaining queue active for later advances.

The session does **not** enqueue every candidate and play them in order.

Instead, each advance uses the materialized queue:

```text
persisted session queue
  -> mark the previous playing item played
  -> claim the next queued item
  -> play it directly
  -> record selection and playback state
```

Normal advance does not call the resolver. If the queue is exhausted, Vesper may rebuild from the active planned source; richer automatic queue-extension policies can be layered on separately.

The auto-advance worker is intentionally conservative. It advances only when playback state is explicitly stopped, cooldown has elapsed, and the current playback snapshot does not show an unfinished track.

## Resolver Calls During a Session

The materialized queue changes when the resolver is consulted:

| Hook | When it runs | What it decides |
| --- | --- | --- |
| `plan_session` | Session start, forced replans, or empty queue rebuilds. | Typed search sources and optional `queue_policy`. |
| `select_session_playlist` | While building `vibe` queue rows from playlist search results. | Which real Apple Music playlist to use for the source. |
| `rephrase_session_vibe` | When a `vibe` source returns no usable playlist/track results. | A fallback search phrase. |
| `filter_session_queue` | Steering with preserved sources, in batches of remaining queue rows. | Which existing queued rows remain eligible and whether to apply a queue policy. |

Normal queue advancement does **not** call `select_session_track`. The old per-advance candidate-window selection path has been replaced by claiming materialized rows. This keeps the future session queue inspectable and stable across process restarts.

## Mid-Session Steering

Steering means changing the future direction of an active session, for example:

- `prefer female vocalists`
- `more pop`
- `less sleepy`
- `keep it upbeat`
- `no more ballads`

The resolver should choose `steer_session` only when there is already an active session and the user wants to shape future picks.

When steering runs, Vesper:

1. appends the steering text to the session's persisted steering history;
2. normalizes an optional `search_update` from the resolver;
3. updates active search sources according to the mode;
4. filters or rebuilds the remaining materialized queue;
5. records a `session_steered` event;
6. usually defers audible change until the next track.

`search_update.mode` can be:

| Mode | Meaning |
| --- | --- |
| `preserve` | Keep current active search sources. The steering text still affects future resolver choices because it is included in selection/planning context. |
| `add` | Add new typed sources alongside existing sources. |
| `replace` | Replace active sources and rebuild the materialized queue for the new direction. |

For a request like `prefer female vocalists`, the resolver may preserve the current source and filter the remaining queue, or it may add/replace sources if it can express the steering as an `artist`, `genre`, or `vibe` source. The exact choice depends on resolver output.

For a request like `add some nine inch nails`, `add` mode appends queue rows for the new source after the currently queued rows. It does not currently weave the two sources together like shuffled decks of cards.

Steering is cumulative. Resolver prompts tell the model to treat steering as persistent session state, not a one-turn hint, until explicitly overridden.

## Track Rejection vs. Steering

`i don't like this` / `reject current track` is different from steering.

When `reject_current_track` runs, Vesper:

- records the current track as `globally_rejected_track`;
- if a session is active, marks the current session track rejected and records a session event;
- immediately advances the session to a replacement track.

Steering changes future preferences; rejection says this specific current track should be avoided and replaced now.
