# Cider Agent

`cider_agent` is a standalone Python service that owns audio control for the Cider Apple Music client. It gives other agents a text-first A2A endpoint for delegating music tasks, plus a local CLI for direct use.

V1 includes:

- playback control
- queue inspection and mutation
- Apple Music catalog and library search
- library playlist browse
- music-specific preference memory in SQLite
- optional OpenAI-compatible text resolver for natural-language requests

## Requirements

- Python 3.12+
- Cider running locally
- Cider external application access enabled
- a Cider API token if your Cider build requires one

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp config.example.json config.json
```

## Configuration

Config resolution order:

1. `CIDER_AGENT_CONFIG_PATH`
2. `./config.json`
3. `~/.config/cider-agent/config.json`

Environment variable overrides are available for every field:

- `CIDER_AGENT_HTTP_HOST`
- `CIDER_AGENT_HTTP_PORT`
- `CIDER_AGENT_PUBLIC_BASE_URL`
- `CIDER_AGENT_CIDER_BASE_URL`
- `CIDER_AGENT_CIDER_API_TOKEN`
- `CIDER_AGENT_DEFAULT_SEARCH_SOURCE`
- `CIDER_AGENT_RESOLVER_BACKEND`
- `CIDER_AGENT_RESOLVER_BASE_URL`
- `CIDER_AGENT_RESOLVER_MODEL`
- `CIDER_AGENT_RESOLVER_API_KEY`
- `CIDER_AGENT_RESOLVER_INCLUDE_REASONING`
- `CIDER_AGENT_RESOLVER_INCLUDE_RAW_OUTPUT`
- `CIDER_AGENT_RESOLVER_DEBUG_LOG_PATH`
- `CIDER_AGENT_INCLUDE_TIMING_DEBUG`
- `CIDER_AGENT_RESPONSE_DETAIL`
- `CIDER_AGENT_SESSION_RECENT_TRACKS_LIMIT`
- `CIDER_AGENT_GLOBAL_RECENT_TRACKS_LIMIT`
- `CIDER_AGENT_REQUEST_TIMEOUT_SECONDS`
- `CIDER_AGENT_VERIFY_TLS`
- `CIDER_AGENT_LOG_LEVEL`
- `CIDER_AGENT_DATABASE_PATH`

## Run

CLI:

```bash
cider-agent play
cider-agent pause
cider-agent stop
cider-agent preferences list
cider-agent preferences forget 12
cider-agent ask "play some kep1er"
cider-agent ask "play something upbeat for the morning"
cider-agent ask "play some music"
cider-agent ask "i don't like this"
cider-agent ask "i like this track"
cider-agent serve
```

A2A server:

```bash
cider-agent-serve
# or
cider-agent serve
```

Published endpoints:

- `POST /a2a`
- `GET /.well-known/agent.json`
- `GET /.well-known/agent-card.json`
- `GET /healthz`

## A2A integration

The intended integration path is plain-language text requests over A2A. Upstream conversational agents do not need to know the internal action schema. In the common case, they only need to know:

- `cider_agent` exists
- it accepts natural-language music requests
- it returns compact structured results

Recommended request shape:

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/send",
  "params": {
    "message": {
      "kind": "message",
      "messageId": "msg-1",
      "role": "user",
      "parts": [
        {
          "kind": "text",
          "text": "play upbeat morning music"
        }
      ]
    }
  }
}
```

Typical text requests:

- `play upbeat morning music`
- `play some music`
- `what playlists do I have?`
- `play playlist Mix`
- `add some KATSEYE`
- `more pop`
- `i don't like this`
- `i like this track`
- `what's playing?`

Responses include a compact `summary` field for tool-friendly consumption, plus the structured execution payload.

## Structured actions

Structured requests still exist for integrations that want a tiny direct control surface, but they are not required for ordinary use. Everything outside the small exposed action set should go through plain-language text requests.

Structured requests should send a `data` part:

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/send",
  "params": {
    "message": {
      "kind": "message",
      "messageId": "msg-1",
      "role": "user",
      "parts": [
        {
          "kind": "data",
          "data": {
            "action": "play",
            "parameters": {}
          }
        }
      ]
    }
  }
}
```

Exposed structured actions:

- `play`
- `pause`
- `stop`
- `list_preferences`
- `forget_preference`

Hidden internal actions:

- adaptive session, search, queue, resolver, and current-track feedback actions still exist internally
- playlist listing and play-by-name are available through plain-language text requests
- they are intentionally not part of the public structured contract
- callers should use `cider-agent ask ...` or A2A text messages for those behaviors

## Architecture

`cider_agent` is built around a transport-agnostic service layer. The CLI and A2A server are thin adapters over the same core engine, so future transports such as MCP or a local web UI can reuse the same playback, search, and session logic.

Text requests follow a grounded two-step flow:

1. the resolver decides whether the request is a direct control action, a session/search request, or a steering update
2. for adaptive sessions, `cider_agent` searches Apple Music first and then asks the resolver to choose from real catalog candidates instead of trusting guessed track names

Adaptive sessions are cache-driven:

- each active query pool caches up to 100 real catalog tracks
- the resolver only sees the next eligible window of 6 tracks at a time
- cache entry state drives repeat avoidance and retry behavior
- steering can preserve existing query pools, add new ones, or replace them entirely
- extremely vague requests such as `play some music` can bootstrap from saved liked-track cues, favored artists, and directly liked tracks before normal adaptive selection takes over

Track cache entries move through a small state machine:

- `fresh`: not yet used in the current cache pass
- `played`: already selected and played in the current cache lifecycle
- `screened_out`: shown to the resolver in a 6-track window that it rejected as unsuitable
- `rejected`: explicitly rejected by the user and kept unavailable until the query pool changes

This keeps most of the memory and repeat-avoidance work in the service instead of making the LLM re-evaluate large recent-history blobs on every turn.

## Operational notes

- Generic `search` uses `default_search_source` from config, which defaults to `catalog`.
- Text requests go through the configured resolver backend. `fallback` only supports tiny direct commands like `play` and `pause`; `openai_compatible` sends chat-completions requests to a configurable OpenAI-style endpoint, including local endpoints such as Ollama when they expose the same API shape.
- Generic or descriptive `play` requests usually start an adaptive session. Specific track requests still resolve to one-shot playback.
- Playlist requests are text-first right now: asking to list playlists or play a playlist by name goes through the local resolver path rather than the tiny public structured API.
- `response_detail` defaults to `compact`, which trims tool-facing execution results down to summaries instead of returning full raw Apple Music and Cider payloads.
- The default request timeout is 60 seconds to accommodate slower local models and Cider RPC calls.

## Development notes

- The RPC client sends both `apptoken` and `apitoken` headers because shipped Cider builds vary.
- Current Cider RPC builds reject larger per-request catalog search limits, so 100-track adaptive pools are fetched as two paginated searches of 50.
- When a pool runs out of `fresh` candidates, the service first resets `screened_out`, then later resets `played` after full-pass exhaustion. If all entries become unusable, the service can replan or rebuild pools before finally giving up with `No playable candidate match could be resolved.`
- Mid-session steering accepts an optional `search_update` object with `mode` of `preserve`, `add`, or `replace`.
- `preserve` keeps the current query pools and only changes future selection behavior.
- `add` keeps the current pools and adds newly planned search pools alongside them.
- `replace` discards the current pools and rebuilds from the replacement query set.
- `like_current_track` is the lightweight "save this one" action. It stores the current track, its artist, and the active session cue/query context without changing playback.
- `reject_current_track` is the "never this exact track again" action. It marks the current cached entry as rejected for the active session, persists a global hard reject, and immediately advances.
- `resolver_include_reasoning` is a debug-only option. When enabled, `cider_agent` includes model-provided reasoning text if the resolver backend returns it.
- `resolver_include_raw_output` is another debug-only option. When enabled, `cider_agent` includes the resolver's exact raw `message.content` as `resolver_raw_content`, plus the parsed JSON object as `resolver_raw_action`.
- `resolver_debug_log_path` keeps a plain-text resolver trace file for the current resolver episode, wiping it each time a new episode starts and appending every resolver prompt/response involved in that episode.
- `include_timing_debug` attaches timing breakdowns to text requests and adaptive session execution so you can see whether latency is coming from resolver calls, Cider RPC state snapshots, catalog lookups, or playback actions.
- `session_recent_tracks_limit` only affects status-style responses and summaries. It does not drive adaptive track selection.
- `global_recent_tracks_limit` controls how many recently played tracks across all sessions are used when building brand-new, additive, or replacement adaptive query pools. It does not act as a hard exclusion against an already-built in-session cache. The default is `10`.
- Resolver session prompts are intentionally compact now. Session planning no longer includes recent session tracks, and session selection no longer includes recent/global history blobs or bulky metadata like artwork URLs, ISRCs, or raw play params.
- Live verification against a current Cider build showed `/api/v1/amapi/run-v3` behaves as a read-only `path` passthrough.
