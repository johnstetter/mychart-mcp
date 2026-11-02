# Getting started

**In short:** install mychart-mcp, register a free app with Epic (once), download your
records, and connect an AI assistant. Plan on about an hour, plus up to a day's wait for
Epic.

## What you need

- A Mac (the only tested system) with [uv](https://docs.astral.sh/uv/), which installs
  Python 3.11+ for you
- A MyChart account at a health system that uses Epic
- A free developer account at [fhir.epic.com](https://fhir.epic.com) to register your own
  app, and [mkcert](https://github.com/FiloSottile/mkcert) for the secure sign-in redirect
- An AI assistant that supports MCP: [Hermes](https://github.com/NousResearch/hermes-agent)
  with a local model, Claude Desktop, Claude Code, …

## 1. Install

```sh
git clone https://github.com/johnstetter/mychart-mcp.git && cd mychart-mcp
uv sync
git config core.hooksPath .githooks   # blocks committing health data if you work on the code
```

## 2. Register your app with Epic

Each person registers their own app on Epic's developer site; it's free. The
[Epic setup guide](epic-setup.md) covers every form field, testing against Epic's sandbox,
going to production, finding your health system's address, and the certificate for the
sign-in redirect.

## 3. Download your records

Try Epic's sandbox first, with a test patient and a scratch database:

```sh
MYCHART_MCP_DB=/tmp/sandbox.db uv run mychart-mcp sync --sandbox --client-id <non-production id>
```

Then your own health system:

```sh
brew install mkcert && mkcert -install && mkcert 127.0.0.1   # once, for the https redirect
uv run mychart-mcp sync --fhir-base <R4 base URL> --client-id <production id> \
    --redirect-uri https://127.0.0.1:8765/callback --cert 127.0.0.1.pem --key 127.0.0.1-key.pem
```

Your browser opens your health system's MyChart sign-in. Sign in, approve read-only access,
and the terminal shows progress, then a summary with counts only (never record contents).

To avoid repeating the flags, set `MYCHART_MCP_FHIR_BASE`, `MYCHART_MCP_CLIENT_ID`,
`MYCHART_MCP_REDIRECT_URI`, `MYCHART_MCP_CERT` and `MYCHART_MCP_KEY`; then it's just
`uv run mychart-mcp sync`.

Run it in your own terminal, not through an AI assistant's shell tool, so your records stay
out of that assistant's conversation.

Check what arrived:

```sh
uv run mychart-mcp stats   # counts and date ranges per record type and source
```

## 4. Connect an assistant

See [Connect an assistant](connect.md).

## Keeping it current

Run `uv run mychart-mcp sync` again whenever you want fresh records: after a visit, when new
results post, or on a monthly reminder. Each run asks you to sign in again in the browser.
Later runs download only what changed. The [Epic setup guide](epic-setup.md#7-keeping-it-current)
explains when it downloads everything instead.

## More than one health system

Run `sync` once per health system, each with its own `--fhir-base`; the same production
client ID works at every health system Epic delivered your app to. Each keeps its own
restart point. Records are labelled with a source name taken from the chart (the managing
organization), or with `--name`. Pick the name once: syncing under a new name makes a fresh
full copy, and the old one stays until `mychart-mcp forget fhir --source "<old name>"`.

## Apple Health instead

The Apple Health import was the first data source and is a quick way to try mychart-mcp
without an Epic app. It has no visit-note text, so switch to direct download once that's set
up.

1. iPhone: Health → Browse → Health Records → Add Account → sign in to your MyChart
   organization. When asked how long to allow access, pick the longest option.
2. Wait until the records appear in Health, then: profile picture → Export All Health Data.
3. Copy `export.zip` to your computer, into a private folder (e.g. `~/HealthData`, `chmod 700`).
4. Import it, check the counts, then delete the export:

```sh
uv run mychart-mcp import ~/HealthData/export.zip   # replaces any previous Apple Health import
uv run mychart-mcp stats
rm ~/HealthData/export.zip
```

The export has conditions, labs and vitals, medications, procedures, diagnostic reports,
immunizations, and allergies. Notes are searchable by type, date and author only.

Once direct download covers everything the Apple import did (compare `mychart-mcp stats`),
remove the Apple copy so records aren't duplicated:

```sh
uv run mychart-mcp forget apple_health   # asks first; --source limits it to one health system
```
