# How it works

**In short:** download your records once, keep them in a file on your computer, and let an
AI assistant read that file. Three separate pieces, and only the first one ever uses the
internet.

```
 1. Download                       2. Store                      3. Ask
 MyChart ──Epic's patient API──▶  mychart-mcp sync  ──▶  database file  ──read-only──▶  AI assistant
 (your health system)              (on your computer)     (on your computer)              (you choose)
```

## 1. Download: `mychart-mcp sync`

You sign in to MyChart in your normal web browser, on your health system's own sign-in page.
mychart-mcp never sees your password. MyChart asks whether to let the app read your records;
you approve, and the download starts.

- **What's downloaded:** conditions and diagnoses, lab results and vital signs, visit notes
  (including their full text), medications, allergies, immunizations, procedures, reports and
  visits.
- **First time:** the whole chart. A large chart with thousands of notes takes several
  minutes.
- **After that:** only what changed since last time, which is much quicker. Once a month it
  downloads everything again to catch records your health system removed.
- **When:** whenever you run it, for example after a visit or once a month. It doesn't run
  on its own.

## 2. Store: one file on your computer

Records go into a single database file in your user folder that only your account can read.
Nothing is uploaded anywhere. Delete the file and they're gone.

## 3. Ask: `mychart-mcp serve`

Your AI assistant starts this part. It answers the assistant's lookups, like "find lab
results for A1c" or "show this note", from the database file. It opens the file read-only and
never connects to the internet.

What happens to the answers depends on the assistant: a model running on your computer keeps
everything local; a hosted one receives the records it uses. See
[Privacy & security](privacy.md).

## Without an Epic app: Apple Health

If your health system is connected to the Health app on your iPhone, you can export
everything from Health and import that file instead (`mychart-mcp import`). It's a quick way
to try mychart-mcp, but Apple's export doesn't include the text of visit notes. Direct
download is the recommended way. See [Getting started](getting-started.md#apple-health-instead).

## Technical details

- **Standards:** the download is a SMART on FHIR standalone patient launch against your
  health system's Epic FHIR R4 API: a public client using PKCE, read-only `patient/*.read`
  scopes, and a one-shot redirect listener on `127.0.0.1`. The access token stays in memory
  for that run only.
- **Incremental syncs:** each sync records a restart point per FHIR server. Later runs read
  the server's CapabilityStatement and search labs, reports, notes and encounters with
  `_lastUpdated`; small types are refetched in full. A full sync runs every 30 days or with
  `--full`. [Keeping it current](epic-setup.md#7-keeping-it-current) has the details.
- **Storage:** SQLite with an FTS5 full-text index over names, codes (ICD-10, SNOMED, LOINC,
  …) and note text. The file is created `0600` in a `0700` directory.
- **Server:** an [MCP](https://modelcontextprotocol.io) server on stdio. It opens the
  database with `mode=ro` and `query_only`, has no write tools, and never imports the
  network code. Its tools are listed in the [Reference](reference.md#assistant-tools).
