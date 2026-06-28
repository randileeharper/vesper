# Development

This guide is for contributors and agents changing Vesper.

## Environment

Run `uv sync --extra dev` to create the `.venv` and install the project with dev dependencies:

```bash
uv sync --extra dev
```

Use `uv run` for all Python commands. `uv run` executes commands inside the environment managed by `uv sync`:

```bash
uv run pytest -q
uv run python -m compileall vesper tests
```

Run `uv sync --extra dev` first if the environment does not exist; `uv run` will then manage execution within it.

## Test Commands

Preferred full verification:

```bash
uv run pytest -q
```

Focused checks:

```bash
uv run pytest tests/test_service.py -q
uv run pytest tests/test_config.py -q
uv run pytest tests/test_a2a.py tests/test_mcp.py -q
```

Syntax/import check:

```bash
uv run python -m compileall vesper tests
```

## Project Layout

```text
vesper/
  action_registry.py   # central action metadata and public/resolver surfaces
  a2a.py               # A2A FastAPI transport and agent card
  app.py               # cached settings/service factories
  cli.py               # local command-line entrypoint
  config.py            # Settings dataclass and config/env loading
  errors.py            # domain exception types
  historian.py         # optional private event delivery + operation context
  mcp_server.py        # MCP stdio / Streamable HTTP server
  output.py            # compact output shaping and summaries
  renderers.py         # transport-specific result rendering
  resolver.py          # fallback and OpenAI-compatible text resolvers
  results.py           # domain result containers
  rpc.py               # Cider boundary
  service.py           # transport-neutral domain facade
  session.py           # adaptive-session engine and worker
  storage/             # SQLite preferences and session persistence
  validation.py        # argument validation/coercion helpers

tests/
  test_*.py            # pytest coverage by component/behavior
```

## Where Common Changes Belong

- **New user-visible action**: add an `ActionDefinition` in `action_registry.py`, implement the service method in `service.py` or delegate to a focused module, add validation, and add tests.
- **Action should be resolver-selectable**: add it to `RESOLVER_ACTION_NAMES` only if the resolver should be allowed to choose it from text.
- **Action should be public structured API**: set `public_exposed=True` only for stable, intentionally tiny external contracts.
- **New CLI command**: add argparse wiring in `cli.py`, then call the service. Keep behavior out of CLI code.
- **New A2A behavior**: prefer service/action changes first; update `a2a.py` only for request inspection, task semantics, or rendering needs.
- **New MCP tool**: add it in `mcp_server.py` only if it should be a direct tool. Otherwise prefer routing through `ask(text)`.
- **Resolver prompt or parsing changes**: update `resolver.py` and tests. Keep the resolver constrained to known actions, short query plans, or candidate selection.
- **Adaptive-session behavior**: change `session.py`. Use the `SessionHost` protocol for cross-cutting service capabilities instead of importing the concrete service class.
- **Persistence changes**: update `vesper.storage` initialization/migration behavior and add tests that cover existing database compatibility when possible.
- **Configuration changes**: update `Settings` in `config.py`, `config.example.json`, this documentation, and config tests.

## Design Rules

1. **Text is the rich interface.** Keep structured public APIs small and stable.
2. **Transports are adapters.** Do not duplicate music logic in CLI, A2A, or MCP.
3. **The resolver is constrained.** It chooses actions, queries, or candidates; it does not invent catalog data.
4. **State lives in code/storage.** Preferences, session runtime, repeat avoidance, and output compaction should not be prompt-only behavior.
5. **Failures should be explicit.** Validate inputs before Cider calls and return compact, useful errors.
6. **Historian is optional.** Event delivery failures should not break successful music actions.

## Local Notes

The repository may contain untracked scratch files under `tmp/`. Keep those out of commits unless they are intentionally promoted into source or docs.
