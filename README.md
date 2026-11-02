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
[Privacy & security](docs/privacy.md).

```
MyChart (your health system) ──Epic FHIR API──▶ mychart-mcp sync ───┐
iPhone Health app (optional) ──export.zip────▶ mychart-mcp import ──┤
                                                                    ▼
                                                     local SQLite database
                                                                    │ read-only
                                         AI client ◀──stdio── mychart-mcp serve
```

> **Status: early.** Direct sync from Epic's FHIR API is the main way to get records. It's
> tested against Epic's sandbox, one real health system, and synthetic data. Other
> health systems may return fields this doesn't handle yet; issues with `mychart-mcp stats`
> output (counts only, never record contents) are welcome.

> **Not affiliated with** Epic Systems (MyChart is Epic's trademark) or Apple. **Not medical
> advice:** an AI summary of your chart can be wrong or incomplete. Check anything important
> with your care team.

## Documentation

**Website: <https://johnstetter.github.io/mychart-mcp/>** (built from [`docs/`](docs/)):

- [How it works](docs/how-it-works.md): download, store, ask, in plain words and in detail
- [Getting started](docs/getting-started.md): requirements, install, first sync, several
  health systems, Apple Health instead
- [Epic setup guide](docs/epic-setup.md): registering your app, sandbox, production,
  certificates, keeping records current, troubleshooting
- [Connect an assistant](docs/connect.md): Hermes, Claude Desktop, Claude Code
- [Privacy & security](docs/privacy.md) and [Reference](docs/reference.md): commands, options,
  database paths, assistant tools
- [FAQ](docs/faq.md), and the [terms of use and privacy statement](TERMS.md) given to Epic

## Quick start

```sh
git clone https://github.com/johnstetter/mychart-mcp.git && cd mychart-mcp
uv sync
git config core.hooksPath .githooks   # pre-commit guard against committing health data

# After registering an app at fhir.epic.com (see the Epic setup guide):
uv run mychart-mcp sync --fhir-base <R4 base URL> --client-id <production id> \
    --redirect-uri https://127.0.0.1:8765/callback --cert 127.0.0.1.pem --key 127.0.0.1-key.pem
uv run mychart-mcp stats
uv run mychart-mcp client-config hermes   # or claude, or claude-code --dir ~/HealthChat
```

`sync` is the only command that uses the network; the server your assistant talks to only
reads the local database. With a local model nothing leaves your machine; with a hosted
assistant the records behind each answer go to that provider
([details](docs/privacy.md)).

## Project layout

```
src/mychart_mcp/
  cli.py        the mychart-mcp commands
  epic.py       Epic SMART on FHIR sign-in and download (the only network code)
  keychain.py   saved sign-ins in the macOS Keychain (experimental)
  importers.py  Apple Health export.zip → SQLite, and record upserts for sync
  fhir.py       field extraction from FHIR R4 and DSTU2 resources
  db.py         schema and read-write / read-only connections
  server.py     MCP tools
docs/           the documentation site's pages (mkdocs.yml)
tests/          synthetic data only: a fake FHIR server, saved sign-ins, an Apple Health export
.githooks/      pre-commit hook that blocks health-data files
```

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run --group docs mkdocs serve      # preview the docs site at http://127.0.0.1:8000
```

CI runs the tests and ruff on Python 3.11 to 3.13, and `mkdocs build --strict`. Tests use synthetic data only; never put real health
data in `tests/`. The pre-commit hook rejects databases, zips, Apple Health exports, and
FHIR/C-CDA files outside `tests/`.

## Roadmap

- Direct Epic sync: saved sign-ins that Epic accepts, and de-duplicating records that arrive
  both via Apple Health and direct sync
- MyChart "Download My Record" (C-CDA) importer
- Encrypted database (SQLCipher)
- Care-team messages: a prompt/template for turning chart facts into a clear message, and
  a local log of what was asked and answered, so follow-ups have the history

## License

MIT. See [LICENSE](LICENSE).
