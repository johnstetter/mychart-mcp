# Privacy & security

**In short:** mychart-mcp keeps your records in a file on your computer and never sends them
to the developer or anyone else. Where they go when you ask a question depends on the AI
model you use: a local model keeps everything on your computer; a hosted one receives the
records each answer relies on.

## Where your records go

Tool results become part of the conversation, and the conversation goes wherever the model
runs.

- **Local model (recommended):** Hermes, or any MCP client, with a model served on your own
  machine (Ollama, LM Studio, …). Nothing leaves your hardware.
- **Hosted model (Claude, GPT, …):** the records each answer relies on are sent to that
  provider and stored with your chat history under their data policy. Check whether your
  plan allows chats to be used for training. HIPAA doesn't cover data you hold yourself or
  share with an AI provider.

## What mychart-mcp does to protect them

- **Read-only, three ways:** the server has no write tools; it opens the database with
  `mode=ro` and `query_only`; its tools carry `readOnlyHint` annotations. The MyChart
  permission it asks for is read-only too.
- **No network in the server:** only `sync` talks to your health system. The server that
  answers your assistant never connects to anything.
- **Sign-ins aren't stored:** the MyChart access token is held in memory for one sync and
  only ever sent to your health system's server.
- **Local storage:** the database is created readable only by your account (`0600`, in a
  `0700` directory) outside the project folder. `.gitignore` and the pre-commit hook keep
  databases and exports out of git. Use full-disk encryption (FileVault on a Mac).

## Untrusted text

Text in a record could contain instructions aimed at the model ("prompt injection"). Don't
give the same chat tools that can send data out (email, messaging, web requests) while your
records are in it. [Connect an assistant](connect.md) shows how for Hermes, Claude Code and
Claude Desktop.

## Deleting and revoking

- **Records:** `mychart-mcp forget fhir` (or `apple_health`) deletes one source's records;
  deleting the database file deletes everything. [Reference](reference.md#where-the-database-lives)
  lists where it lives.
- **Access:** remove mychart-mcp (and Apple Health, if you used it) under MyChart → Linked
  Apps. If you saved a sign-in (experimental), also run `mychart-mcp logout`.

The [terms of use and privacy statement](terms.md) given to Epic say the same in formal terms.
