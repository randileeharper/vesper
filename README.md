# Cider Agent

`cider_agent` is a dedicated music-control agent for the Cider Apple Music client. It exposes a text-first A2A interface for delegation from another agent, plus a local CLI for direct use.
It also exposes a small MCP tool surface for hosts that want direct playback control plus one natural-language entrypoint.

The project is built around a simple idea: keep the main conversational agent lean, and hand off music work to a narrow specialist.

## Design Philosophy

Many agent harnesses assume frontier models can absorb large tool surfaces, long instruction blocks, and lots of command schema without falling apart. Smaller local models usually do worse at that. They get slower, more error-prone, and more likely to lose the thread.

`cider_agent` is designed to reduce that cognitive load.

- The conversational agent stays the main user-facing entrypoint.
- It does not need to memorize a large music-control schema.
- It delegates plain-language requests to a small specialist agent.
- The specialist agent keeps prompts tight and moves stateful work into code.
- The resolver is only asked to make small grounded decisions.

In practice, that means:

- natural language is the main contract: `play some music`, `more pop`, `i don't like this`
- playback state, preference memory, repeat avoidance, playlist lookup, and session caches live in code rather than prompt text
- the resolver chooses an action, plans a short query, or selects from a very small window of real candidates instead of inventing tracks
- the service layer is transport-agnostic, so A2A, CLI, and future transports such as MCP can reuse the same domain logic

This project is less about adding an LLM to music control and more about constraining the LLM so a smaller local stack stays fast and reliable.

## What It Does

- text-first playback control for Cider
- adaptive sessions for vague or descriptive music requests
- playlist listing and play-by-name through natural-language requests
- music-specific preference memory in SQLite
- a tiny public structured control surface for direct integrations

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

See `config.example.json` for the full schema. Every setting also supports an environment-variable override. The most commonly used ones are:

- `CIDER_AGENT_PUBLIC_BASE_URL`
- `CIDER_AGENT_CIDER_BASE_URL`
- `CIDER_AGENT_CIDER_API_TOKEN`
- `CIDER_AGENT_RESOLVER_BACKEND`
- `CIDER_AGENT_RESOLVER_BASE_URL`
- `CIDER_AGENT_RESOLVER_MODEL`
- `CIDER_AGENT_RESOLVER_API_KEY`
- `CIDER_AGENT_DATABASE_PATH`

## Run

CLI examples:

```bash
cider-agent play
cider-agent pause
cider-agent stop
cider-agent preferences list
cider-agent preferences forget 12
cider-agent ask "play some kep1er"
cider-agent ask "play something upbeat for the morning"
cider-agent ask "play some music"
cider-agent ask "what playlists do I have?"
cider-agent ask "play playlist Mix"
cider-agent ask "i don't like this"
cider-agent ask "i like this track"
```

Start the A2A server:

```bash
cider-agent-serve
# or
cider-agent serve
```

Run the MCP server over stdio:

```bash
cider-agent mcp
```

Run A2A and also mount MCP over HTTP:

```bash
cider-agent serve --mcp
# or
cider-agent-serve --mcp
```

Published endpoints:

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
- `GET /healthz`
- `POST /mcp`

## A2A Usage

The intended integration path is plain-language text requests. Upstream conversational agents do not need to know the internal action schema. In the common case, they only need to know:

- `cider_agent` exists
- it accepts natural-language music requests
- it returns compact structured results

Recommended request shape:

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

Responses include a compact `summary` field for tool-friendly consumption plus the structured execution payload.
Read-only requests currently complete as completed tasks, and mutating requests can be returned as submitted tasks when `returnImmediately` is used.

## Structured Actions

Structured requests still exist for integrations that want a tiny direct-control surface, but they are intentionally small. Everything richer should go through plain-language text requests.

Exposed structured actions:

- `play`
- `pause`
- `stop`
- `list_preferences`
- `forget_preference`

Structured requests should send a `data` part:

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

Playlist listing, playlist play-by-name, adaptive sessions, queue-aware behavior, and current-track feedback are available through text requests and intentionally hidden from the public structured contract.

## MCP Usage

The MCP surface is intentionally smaller than the internal action registry. Exposed MCP tools:

- `play`
- `pause`
- `next`
- `previous`
- `ask`

`ask` is the rich entrypoint. It reuses the same text-first behavior as A2A and CLI requests, so richer behavior such as adaptive sessions, playlist requests, and feedback requests should go through `ask`.

Example MCP host config:

```json
{
  "mcpServers": {
    "cider-agent": {
      "command": "cider-agent",
      "args": ["mcp"]
    }
  }
}
```

When MCP is mounted over HTTP with `cider-agent serve --mcp`, the Streamable HTTP endpoint is:

```text
http://127.0.0.1:8766/mcp
```

## How It Works

- The core service owns playback, search, session state, and preference memory.
- The CLI and A2A server are thin adapters over that same service layer.
- Descriptive or vague `play` requests usually start an adaptive session instead of one-shot playback.
- Adaptive sessions search real Apple Music candidates first, then ask the resolver to choose from a small candidate window instead of making it invent songs.
- Very vague requests such as `play some music` can bootstrap from saved liked-track cues, favored artists, and directly liked tracks before normal adaptive selection takes over.

Track cache entries move through a small state machine:

- `fresh`
- `played`
- `screened_out`
- `rejected`

That keeps repeat avoidance and memory work in code rather than forcing the LLM to re-evaluate a giant history blob every turn.

## Notes

- The public structured API is intentionally tiny; text is the main interface.
- Playlist requests are text-first right now.
- Resolver prompts are intentionally compact for smaller local models.
- `response_detail` defaults to `compact` so tool-facing responses stay small.
- The default request timeout is `60` seconds to accommodate slower local models and Cider RPC calls.
