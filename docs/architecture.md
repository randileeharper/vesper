# Architecture

Vesper is a transport-agnostic music-control service with thin adapters for CLI, A2A, and MCP. The public contract is text-first: users and agent hosts usually send natural-language music requests, and Vesper translates those into constrained service actions.

## Component Map

```text
vesper.cli              vesper.a2a                 vesper.mcp_server
   |                       |                              |
   +-----------------------+------------------------------+
                           |
                           v
                    vesper.app.get_service()
                           |
                           v
                 CiderAgentService (service.py)
                           |
       +-------------------+-------------------+------------------+
       |                   |                   |                  |
 CiderRpcClient      PreferenceStore       Resolver        SessionEngine
   (rpc.py)           (storage/)       (resolver.py)     (session.py)
       |                   |                   |                  |
     Cider              SQLite       fallback/OpenAI-      adaptive session
                                  compatible decisions    runtime + worker
```

## Service Layer

`CiderAgentService` in `vesper/service.py` is the domain facade. It owns the high-level operations that transports call, including playback commands, text request execution, search, preference updates, and session delegates.

Important responsibilities:

- creates or receives the Cider RPC client, preference store, resolver, and Historian sink
- exposes direct playback and query methods such as `play`, `pause`, `stop`, `get_now_playing`, and `search_catalog_tracks`
- resolves natural-language requests through `execute_text_request` / `handle_text_request`
- executes action-registry entries through `execute_action`
- emits optional Historian events under an operation context
- delegates adaptive-session behavior to `SessionEngine`

The service is intentionally transport-neutral. CLI, A2A, and MCP do not implement music behavior themselves; they validate/shape requests, call the service, and render results.

## Action Registry

`vesper/action_registry.py` is the central list of known actions. Each `ActionDefinition` records:

- action name
- description and summary label
- executor callback
- parameter schema and required fields
- whether the action is read-only
- whether it is public as a structured action
- whether it is resolver-visible

There are three related surfaces:

1. **Internal action registry** — all actions Vesper code can execute.
2. **Resolver action list** — the constrained set an LLM resolver may choose from.
3. **Public structured surface** — intentionally tiny; currently `play`, `pause`, `stop`, `list_preferences`, and `forget_preference`.

Natural-language text can reach richer behavior such as playlist lookup, adaptive sessions, and feedback. Public structured requests are kept small so external integrations do not depend on internal machinery.

## Resolver

`vesper/resolver.py` defines the resolver protocol and implementations.

- `FallbackResolver` handles obvious deterministic commands such as `play`, `pause`, `stop`, `next`, `previous`, and `status`.
- `OpenAICompatibleResolver` calls an OpenAI-compatible chat-completions endpoint when general text resolution is needed.

Resolver use is deliberately constrained. It should choose from known actions, create small query plans, filter already-materialized queue candidates, or select from real playlist candidates. It should not invent tracks as if it were a catalog.

Saved preferences are stored and applied by Vesper itself, not exposed as resolver prompt context. For vague sessions the resolver can choose an abstract `preference` source, then `SessionEngine` materializes that source locally from liked tracks and favored artists.

The resolver participates in these adaptive-session decisions:

1. `plan_session` — produce typed search sources for the next session step. The built-in OpenAI-compatible planner may return a single source or multiple sources (for example for "play a mix of nirvana and nine inch nails"), up to `MAX_SESSION_SEARCH_QUERIES`; mid-session steering can also add more sources later.
2. `select_session_playlist` / `rephrase_session_vibe` — handle playlist-oriented session searches and empty vibe searches.
3. `filter_session_queue` — filter remaining materialized queue rows after steering.

Resolver debug output can be enabled with `resolver_debug_log_path` plus the include flags documented in [configuration.md](configuration.md).

## Adaptive Sessions

`vesper/session.py` contains `SessionEngine`, extracted from the service so session state and worker behavior are isolated.

Adaptive sessions are used for vague or descriptive requests such as:

- `play some music`
- `play upbeat morning music`
- `more pop`
- `add some KATSEYE`

The basic flow is:

```text
text request
  -> resolver chooses play_session or steer_session
  -> SessionEngine starts/updates a persisted session
  -> query planning creates typed search sources
  -> Apple Music searches return real candidates
  -> SessionEngine materializes concrete queue rows in SQLite
  -> service plays the selected track through Cider
  -> worker advances the session when appropriate
```

Session runtime is split between SQLite and in-memory state:

- SQLite stores active sessions, steering history, selected tracks, session events, preferences, materialized session queue rows, and persisted runtime fields.
- In-memory runtime tracks process-local worker state, locks, cooldowns, active search sources, cached query-pool metadata used during queue materialization, and the current queue item being played.

The background worker is started by long-lived transports and reconciles stored session state on startup. CLI one-shot commands do not need a long-running server.

See [Adaptive Sessions, Search, and Preferences](adaptive-sessions.md) for the practical behavior details: preference effects, typed search kinds, steering, where session queue rows live, and how track advancement works.

## Persistence

The `vesper.storage` package implements `PreferenceStore`, a SQLite store for:

- legacy generic preferences
- music preferences such as liked tracks, favored artists, and rejected tracks
- adaptive sessions and steering history
- session tracks and events
- persisted session runtime

The default database path is `~/.local/share/vesper/vesper.db`, configurable with `database_path` or `VESPER_DATABASE_PATH`.

## Cider RPC

`vesper/rpc.py` is the boundary to Cider. Service methods should use this client rather than issuing ad-hoc HTTP calls. Validation for user-controlled arguments belongs in `vesper/validation.py` or near the service method before calling Cider.

## Output Rendering

Output shaping lives primarily in:

- `vesper/results.py` for domain result containers
- `vesper/output.py` for compact output and summaries
- `vesper/renderers.py` for A2A-specific rendering

`response_detail` defaults to `compact` so tool-facing responses stay small.

## Historian Events

`vesper/historian.py` provides optional private event delivery. `operation_context` carries correlation, causation, caller, and session IDs through nested service calls. If Historian delivery fails, Vesper logs/records the failure but does not convert a successful music action into a user-visible failure.
