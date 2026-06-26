# Transports

Vesper has three entrypoints over the same `CiderAgentService`: CLI, A2A, and MCP. Transport code should stay thin. Music behavior belongs in the service, session engine, resolver, storage, and output layers.

## CLI

The CLI entrypoint is `vesper.cli:main` and is installed as the `vesper` console script.

Common commands:

```bash
vesper play
vesper pause
vesper stop
vesper ask "play some music"
vesper ask "what's playing?"
vesper preferences list
vesper preferences forget 12
vesper session queue --json
vesper session queue --all --limit 100 --json
vesper session candidates --json
```

Use `--json` to print the full payload instead of the default result-focused view:

```bash
vesper --json ask "play some music"
```

CLI commands call the service directly in the current process. They do not require `vesper serve` to be running.

`vesper session queue` inspects Vesper's persisted adaptive-session queue, not Cider's native playback queue. By default it shows queued/playing rows; `--all` includes played, rejected, and filtered history.

`vesper session candidates` inspects the active session's in-memory candidate pools — the runtime pools Vesper builds from Apple Music search results before materializing them into the persisted queue. This is a read-only developer/debug view. It reports active search sources, per-pool cursor and candidate counts by state (`fresh`, `played`, `screened_out`, `rejected`), and a next-candidate window of fresh entries. Use `--window N` to control how many fresh entries are shown per pool. Candidate pools are process-local runtime state; after a process restart the pools are empty even if a session is persisted.

## HTTP Serving

`vesper serve` starts a FastAPI app. At least one transport flag is required:

```bash
vesper serve --a2a
vesper serve --mcp
vesper serve --a2a --mcp
```

Common endpoint available whenever an HTTP transport is enabled:

- `GET /healthz`

## A2A

A2A hosting lives in `vesper/a2a.py`.

When `--a2a` is enabled, Vesper exposes:

- `POST /a2a`
- `GET /.well-known/agent-card`
- `GET /.well-known/agent-card.json`
- `POST /message:send`
- `POST /message:stream`
- `GET /tasks`
- `GET /tasks/{id}`
- `POST /tasks/{id}:cancel`
- `GET /tasks/{id}:subscribe`
- `POST /tasks/{id}:subscribe`

The intended A2A request is a text message:

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "parts": [
        {
          "text": "play upbeat morning music",
          "mediaType": "text/plain"
        }
      ]
    }
  }
}
```

A2A also accepts structured `data` parts, but only for the public structured action surface:

- `play`
- `pause`
- `stop`
- `list_preferences`
- `forget_preference`

Example structured request:

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "parts": [
        {
          "data": {
            "action": "play",
            "parameters": {}
          },
          "mediaType": "application/json"
        }
      ]
    }
  }
}
```

Hidden/internal actions are rejected as structured A2A requests. Use text for richer behavior.

## MCP

MCP hosting lives in `vesper/mcp_server.py`.

Run stdio MCP:

```bash
vesper mcp
```

Example host config:

```json
{
  "mcpServers": {
    "vesper": {
      "command": "vesper",
      "args": ["mcp"]
    }
  }
}
```

Run MCP over Streamable HTTP:

```bash
vesper serve --mcp
```

The HTTP MCP endpoint is:

```text
http://127.0.0.1:8766/mcp
```

Exposed MCP tools:

- `play()` — resume playback
- `pause()` — pause playback
- `next()` — skip to the next track or session-selected track
- `previous()` — go to the previous track
- `ask(text)` — natural-language music request

Prefer `ask` for anything richer than direct playback control.

## Background Session Worker

Long-lived transports start the adaptive-session background worker so sessions can continue advancing. When MCP is mounted inside the shared FastAPI app, the parent app owns the worker; the embedded MCP lifespan intentionally does not stop it after each request.
