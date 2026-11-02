# Reference

**In short:** every command, setting and assistant tool on one page. Run commands from the
project folder as `uv run mychart-mcp <command>`; `--help` on any command lists its options.

## Commands

| Command | What it does |
|---|---|
| `sync` | Download from an Epic FHIR server. The first run is full; later ones fetch changes |
| `sync --full` | Download everything again, dropping records deleted upstream (automatic every 30 days) |
| `import <export.zip>` | Import an Apple Health export, replacing the previous one |
| `stats` | Counts and date ranges per record type, broken down by origin and source |
| `forget apple_health\|fhir [--source NAME]` | Delete the records from one origin, or one health system; asks first (`--yes` skips) |
| `client-config claude\|claude-code\|hermes` | Set up an AI assistant ([Connect an assistant](connect.md)) |
| `serve` | The MCP server itself; your assistant starts it |
| `logout --fhir-base URL` | Delete a sign-in saved by `sync --remember` (experimental) |
| `schedule-config` | Print a macOS LaunchAgent for daily unattended syncs (experimental) |

Saved sign-ins and scheduled syncs are in the code but [don't work against Epic
yet](epic-setup.md#saved-sign-ins-experimental-not-working-yet).

## `sync` options

| Option | Environment variable | Default |
|---|---|---|
| `--fhir-base` | `MYCHART_MCP_FHIR_BASE` | none: your health system's Epic R4 base URL |
| `--sandbox` | | off: use Epic's public sandbox (needs `MYCHART_MCP_DB`) |
| `--client-id` | `MYCHART_MCP_CLIENT_ID` | none: from your fhir.epic.com app |
| `--redirect-uri` | `MYCHART_MCP_REDIRECT_URI` | `http://127.0.0.1:8765/callback` |
| `--cert`, `--key` | `MYCHART_MCP_CERT`, `MYCHART_MCP_KEY` | none: mkcert files for an https redirect |
| `--name` | | the chart's managing organization |
| `--full` | | off: download everything, not just changes |
| `--remember`, `--unattended` | | off: saved sign-ins (experimental) |

## Where the database lives

| OS | Default path |
|---|---|
| macOS | `~/Library/Application Support/mychart-mcp/health.db` |
| Linux | `$XDG_DATA_HOME/mychart-mcp/health.db` (default `~/.local/share/…`) |
| Windows | `%LOCALAPPDATA%\mychart-mcp\health.db` |

Set `MYCHART_MCP_DB` to use a different path.

## Assistant tools

What the MCP server offers your assistant. All are read-only.

| Tool | Purpose |
|---|---|
| `data_summary` | Record counts, date ranges, sources, and when each export or health system was last imported |
| `search_records` | Full-text search over names, codes (ICD-10, SNOMED, LOINC, …) and descriptive text |
| `condition_timeline` | Date-ordered evidence for a condition across all record types |
| `list_records` | Records of one type, newest first |
| `lab_results` | Lab values oldest-first, for trends |
| `get_record` | Full detail of one record (and decoded note text, when a source provides it) |
