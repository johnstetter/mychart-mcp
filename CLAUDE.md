# CLAUDE.md

Read-only MCP server over a local SQLite copy of the user's MyChart records. See README.md.

## Privacy rules (non-negotiable)

- Never read, print, copy or commit real health data: the database
  (`~/Library/Application Support/mychart-mcp/health.db`), `export.zip`, `export.xml`, or
  `clinical-records/`. When debugging an import, ask for `mychart-mcp stats` output or
  field-level shapes, not record contents.
- Test fixtures must be synthetic and live under `tests/`. Use generic conditions; never the user's own diagnoses.
- Personal Claude Code deny rules go in `.claude/settings.local.json` (git-ignored), not `settings.json`.
- Keep the server read-only: no write tools, database opened with `connect_ro()`, every tool
  annotated `READ_ONLY`. The server must not make network calls.
- Don't loosen the deny list in `.claude/settings.json` or suggest `curl`/network commands
  while health data is in the conversation.

## Commands

```sh
uv run pytest            # tests
uv run ruff check .      # lint
uv run ruff format .     # format
```

## Layout

- `src/mychart_mcp/fhir.py`: field extraction from FHIR R4/DSTU2 resources
- `src/mychart_mcp/importers.py`: Apple Health export.zip → SQLite
- `src/mychart_mcp/db.py`: schema; `connect_rw` (importer) / `connect_ro` (server)
- `src/mychart_mcp/db.py` `_default_db()`: per-OS database path (macOS, XDG, Windows)
- `src/mychart_mcp/cli.py`: `client-config` prints Claude Desktop / Hermes setup (absolute paths)
- `src/mychart_mcp/server.py`: MCP tools (mcp SDK 2.x, `MCPServer`; raise `ToolError` for
  messages the model should see)
