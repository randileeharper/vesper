# Cider Agent

`cider_agent` is a standalone Python service that owns audio control for the Cider Apple Music client. It gives other agents a strict A2A-style endpoint for delegating music tasks, plus a local CLI for direct use.

V1 includes:

- playback control
- queue inspection and mutation
- Apple Music catalog and library search
- library playlist browse
- explicit preference memory in SQLite
- deterministic preference-based recommendations with a pluggable recommender seam
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
- `CIDER_AGENT_REQUEST_TIMEOUT_SECONDS`
- `CIDER_AGENT_VERIFY_TLS`
- `CIDER_AGENT_LOG_LEVEL`
- `CIDER_AGENT_DATABASE_PATH`

## Run

CLI:

```bash
cider-agent status
cider-agent now-playing
cider-agent search default "k-pop"
cider-agent search library "k-pop"
cider-agent search catalog "k-pop"
cider-agent ask "play some kep1er"
cider-agent preferences remember like "k-pop" --category genre
cider-agent recommend --play
```

A2A server:

```bash
cider-agent-serve
```

Published endpoints:

- `POST /a2a`
- `GET /.well-known/agent.json`
- `GET /.well-known/agent-card.json`
- `GET /healthz`

## A2A request shape

Use a JSON-RPC 2.0 request against `/a2a` with `method` set to `message/send` or `message/stream`.

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
            "action": "create_playlist",
            "parameters": {
              "query": "kep1er",
              "limit": 5
            }
          }
        }
      ]
    }
  }
}
```

Common actions:

- `status`
- `get_now_playing`
- `play`, `pause`, `playpause`, `stop`, `next_track`, `previous_track`
- `seek`, `set_volume`
- `get_queue`, `move_queue_item`, `remove_queue_item`, `clear_queue`
- `search`, `search_catalog`, `search_library`, `search_library_tracks`
- `list_library_playlists`, `get_library_playlist_tracks`
- `remember_preference`, `list_preferences`, `forget_preference`
- `recommend`, `play_recommendation`

## Notes

- The RPC client sends both `apptoken` and `apitoken` headers because shipped Cider builds vary.
- Generic `search` uses `default_search_source` from config, which defaults to `catalog`.
- Text requests go through the configured resolver backend. `fallback` only supports tiny direct commands like `play` and `pause`; `openai_compatible` sends chat-completions requests to a configurable OpenAI-style endpoint, including local endpoints such as Ollama when they expose the same API shape.
- The default request timeout is 60 seconds to accommodate slower local models and Cider RPC calls.
- Live verification against a current Cider build showed `/api/v1/amapi/run-v3` behaves as a read-only `path` passthrough. Playlist creation and add-track mutation are therefore not exposed in this version of `cider_agent`.
- The web UI is intentionally not part of v1, but the service layer is transport-agnostic so a future local UI can reuse the same operations.
