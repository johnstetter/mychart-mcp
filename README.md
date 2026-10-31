# mychart-mcp

A read-only [MCP](https://modelcontextprotocol.io) server over a **local copy** of your own
MyChart (Epic) health records. Ask an AI assistant plain-English questions like "When was I
first diagnosed with X?" or "How has my A1c changed over the last five years?" and get
answers with dates and sources.

**Local first.** The server never talks to MyChart or the network; it only reads a SQLite
database on your machine. Pair it with a **local model** (for example
[Hermes](https://github.com/NousResearch/hermes-agent) running an open model through Ollama)
and your records never leave your hardware. It also works with hosted assistants such as
Claude Desktop, but then every answer's source records are sent to that provider. See
[Privacy](#privacy-and-security).

```
iPhone Health app ──export.zip──▶ mychart-mcp import ──▶ local SQLite database
                                                              │ read-only
                                  AI client ◀──stdio── mychart-mcp serve
```

> **Status: early.** Apple Health exports are the only data source so far. Tested against a
> real export from one health system plus synthetic data. Other organizations' exports may
> have fields this doesn't handle yet; issues with `mychart-mcp stats` output (counts only,
> never record contents) are welcome.

> **Not affiliated with** Epic Systems (MyChart is Epic's trademark) or Apple. **Not medical
> advice:** an AI summary of your chart can be wrong or incomplete. Check anything important
> with your care team.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- An iPhone with your MyChart organization connected in the Health app (to produce the export)
- An MCP client that launches stdio servers: Hermes, Claude Desktop, Claude Code, …

macOS is the main target. Import and serve also work on Linux and Windows once you've copied
the export over.

## Install

```sh
git clone <this repo> && cd mychart-mcp
uv sync
git config core.hooksPath .githooks   # pre-commit guard against committing health data
```

## Getting data

1. iPhone: Health → Browse → Health Records → Add Account → sign in to your MyChart
   organization. When asked how long to allow access, pick the longest option.
2. Wait until the records appear in Health, then: profile picture → Export All Health Data.
3. Copy `export.zip` to your computer, into a private folder (e.g. `~/HealthData`, `chmod 700`).

### What the export includes

Conditions, labs and vitals, medications, procedures, diagnostic
reports, immunizations, and allergies, as FHIR resources. **Clinical note text is not
included:** Apple's export only has each note's type, date, and author, plus a link back to
the health system. Notes are searchable by those fields, not by their content. Getting note
text requires a direct FHIR connection (see below).

### Direct Epic sync (experimental)

`mychart-mcp sync` signs in to MyChart in your browser (SMART on FHIR, read-only scopes, PKCE)
and downloads your chart straight from your health system's Epic FHIR R4 endpoint, including
clinical note text. The access token is held in memory for that run only and never saved.
This is the only command that uses the network; `serve` never does.

You need a patient-app client ID from [fhir.epic.com](https://fhir.epic.com), registered with
`http://127.0.0.1:8765/callback` (sandbox) and/or `https://127.0.0.1:8765/callback`
(production, which Epic requires to be https) as redirect URIs.

```sh
# Try it against Epic's sandbox first, in a scratch database
MYCHART_MCP_DB=/tmp/sandbox.db uv run mychart-mcp sync --sandbox --client-id <non-production id>

# Your health system (find its R4 base URL in Epic's open.epic.com endpoint list)
brew install mkcert && mkcert -install && mkcert 127.0.0.1   # once, for the https redirect
uv run mychart-mcp sync --fhir-base <R4 base URL> --client-id <production id> \
    --redirect-uri https://127.0.0.1:8765/callback --cert 127.0.0.1.pem --key 127.0.0.1-key.pem
```

Flags can also come from `MYCHART_MCP_FHIR_BASE`, `MYCHART_MCP_CLIENT_ID`,
`MYCHART_MCP_REDIRECT_URI`, `MYCHART_MCP_CERT` and `MYCHART_MCP_KEY`. Re-running replaces
that health system's synced records.

Once direct sync covers everything your Apple Health import did (compare `mychart-mcp stats`),
drop the Apple copy so records aren't duplicated:

```sh
uv run mychart-mcp forget apple_health   # asks first; --source limits it to one health system
```

## Usage

```sh
uv run mychart-mcp import ~/HealthData/export.zip   # replaces any previous Apple Health import
uv run mychart-mcp stats                             # counts and date ranges per record type
rm ~/HealthData/export.zip                           # once the import looks right
```

The database lives at:

| OS | Default path |
|---|---|
| macOS | `~/Library/Application Support/mychart-mcp/health.db` |
| Linux | `$XDG_DATA_HOME/mychart-mcp/health.db` (default `~/.local/share/…`) |
| Windows | `%LOCALAPPDATA%\mychart-mcp\health.db` |

Set `MYCHART_MCP_DB` to use a different path.

## Connecting a client

Desktop apps launch MCP servers with a minimal `PATH` and an arbitrary working directory, so
point them at the absolute path of this project's executable. Generate the snippet:

```sh
uv run mychart-mcp client-config hermes   # Hermes
uv run mychart-mcp client-config claude   # Claude Desktop
```

| Client | Config file | Then |
|---|---|---|
| Hermes | `~/.hermes/config.yaml` (add under `mcp_servers:`) | Restart Hermes |
| Claude Desktop | `claude_desktop_config.json` (merge into `mcpServers`) | Quit and reopen Claude |
| Claude Code | `.mcp.json` in this repo (already set up) | Run `claude` in this directory |

Set `MYCHART_MCP_DB` before running `client-config` to bake a non-default path into the
snippet. After moving the repo, run `uv sync` and regenerate the config.

### What it's for

The server answers questions about your records; the assistant does the rest. For example,
ask it to pull your last three A1c results and the medications started since, then have the
agent draft a short, clear message to your care team from that. The server provides the
facts with dates and sources. Drafting is the model's job, and sending stays with you.

### Tools

| Tool | Purpose |
|---|---|
| `data_summary` | Record counts, date ranges, sources, last import |
| `search_records` | Full-text search over names, codes (ICD-10, SNOMED, LOINC, …) and descriptive text |
| `condition_timeline` | Date-ordered evidence for a condition across all record types |
| `list_records` | Records of one type, newest first |
| `lab_results` | Lab values oldest-first, for trends |
| `get_record` | Full detail of one record (and decoded note text, when a source provides it) |

## Privacy and security

**Where your records go depends on the model, not this server.** Tool results become part of
the conversation, and the conversation goes wherever the model runs.

- **Local model (recommended):** Hermes, or any MCP client, with a model served on your own
  machine (Ollama, LM Studio, …). Nothing leaves your hardware.
- **Hosted model (Claude, GPT, …):** the records each answer relies on are sent to that
  provider and stored with your chat history under their data policy. Check whether your
  plan allows chats to be used for training. HIPAA doesn't cover data you hold yourself or
  share with an AI provider.

The server itself:

- **Read-only, three ways:** no write tools; the database is opened with `mode=ro` and
  `query_only`; tools carry `readOnlyHint` annotations.
- **Local storage:** the database is created `0600` in a `0700` directory outside the repo.
  `.gitignore` and the pre-commit hook keep databases and exports out of git. Use full-disk
  encryption.
- **Untrusted text:** record text could contain instructions aimed at the model (prompt
  injection). Don't give the same chat tools that can send data out (email, messaging, web
  requests) while your records are in it:
  - **Claude Code:** `.claude/settings.json` denies web access in this project. Add your own
    outbound connectors (email, chat, …) to the deny list in `.claude/settings.local.json`.
    That file is git-ignored.
  - **Claude Desktop:** turn off other connectors in the chat's tools menu.
  - **Hermes:** check which toolsets and messaging platforms are enabled. Memories and session
    history are stored under `~/.hermes/`.
- **Revoking:** remove Apple Health and/or mychart-mcp under MyChart → Linked Apps, and
  delete the database.

See [TERMS.md](TERMS.md) for the terms of use and privacy statement shown to Epic.

## Project layout

```
src/mychart_mcp/
  cli.py        import / sync / stats / serve / client-config commands
  importers.py  Apple Health export.zip → SQLite
  epic.py       Epic SMART on FHIR sign-in and download (the only network code)
  fhir.py       field extraction from FHIR R4 and DSTU2 resources
  db.py         schema and read-write / read-only connections
  server.py     MCP tools
tests/          end-to-end tests on a synthetic export
.githooks/      pre-commit hook that blocks health-data files
```

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

CI runs both on Python 3.11 to 3.13. Tests use a synthetic export only; never put real health
data in `tests/`. The pre-commit hook rejects databases, zips, Apple Health exports, and
FHIR/C-CDA files outside `tests/`.

## Roadmap

- Direct Epic sync: refresh tokens in the OS keychain so re-syncing doesn't need a browser
  sign-in, and de-duplicating records that arrive both via Apple Health and direct sync
- MyChart "Download My Record" (C-CDA) importer
- Encrypted database (SQLCipher)
- Care-team messages: a prompt/template for turning chart facts into a clear message, and
  a local log of what was asked and answered, so follow-ups have the history

## License

MIT. See [LICENSE](LICENSE).
