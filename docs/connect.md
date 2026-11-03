# Connect an assistant

**In short:** one command prints the settings your AI assistant needs. For the most privacy,
use an assistant with a model that runs on your own computer, and keep tools that can send
data out (web, email, messaging) away from your records.

## Generate the settings

Desktop apps start MCP servers with a minimal `PATH` and an arbitrary working directory, so
the settings point at this project's program by its full path:

```sh
uv run mychart-mcp client-config hermes                         # Hermes
uv run mychart-mcp client-config claude                         # Claude Desktop
uv run mychart-mcp client-config claude-code --dir ~/HealthChat # Claude Code (creates the folder)
```

| Assistant | Where the settings go | Then |
|---|---|---|
| Hermes | a dedicated profile's `config.yaml`, under `mcp_servers:` (below) | `hermes -p health` |
| Claude Desktop | `claude_desktop_config.json`, merged into `mcpServers` | Quit and reopen Claude |
| Claude Code | `.mcp.json` + `.claude/settings.json` in the folder you pass | Run `claude` in that folder |

Set `MYCHART_MCP_DB` before running `client-config` to use a non-default database path. After
moving the project folder, run `uv sync` and generate the settings again.

## Hermes: use a dedicated profile

Give your records their own [Hermes profile](https://hermes-agent.nousresearch.com/docs/reference/cli-commands)
instead of adding the server to your everyday one. A profile is a separate Hermes instance
with its own config, tools, memories and session history, so health details don't end up in
the memory your other chats draw on, and the tools that can send data out stay off.

```sh
hermes profile create health        # no --clone: start without your usual memories and skills
hermes -p health tools              # turn off web, browser, terminal and messaging toolsets
uv run mychart-mcp client-config hermes   # paste under mcp_servers: in the profile's config.yaml
hermes -p health                    # ask about your records here
```

- The profile's files live under `~/.hermes/profiles/health/` (`hermes -p health dump`
  shows the path).
- Don't make it the default with `hermes profile use`, and don't start a messaging gateway
  for it; ask from the terminal on this machine.
- Check `hermes -p health tools --summary` after Hermes updates, which can add toolsets.
- Pair it with a local model (Ollama, LM Studio, …) so nothing leaves the machine.

## Claude Code: a separate folder

`client-config claude-code --dir ~/HealthChat` creates a folder whose sessions get the
mychart-mcp tools but no web or shell access, so text in a record can't get the model to send
data anywhere. Run `claude` there to ask about your records. If you've added other connectors
that can send data out (email, Slack, …), deny them too in that folder's
`.claude/settings.local.json`.

## Open WebUI: through a local bridge

The server only speaks stdio, which Open WebUI can't launch itself. [mcpo](https://github.com/open-webui/mcpo)
wraps a stdio MCP server as an OpenAPI tool server that Open WebUI can call.

```sh
uv run mychart-mcp client-config claude    # note the "command" and "args" it prints
uvx mcpo --host 127.0.0.1 --port 8000 -- <command> <args>
```

Then in Open WebUI, add `http://127.0.0.1:8000/` under Admin Settings → Tools (use
`http://host.docker.internal:8000/` if Open WebUI runs in Docker).

Keep the records away from anything that can send data out:

- Use a model served on your own computer (Ollama, …). Don't connect Open WebUI to a hosted
  API while this tool is enabled: the records would travel in the conversation.
- Make a dedicated model (or user) with only this tool enabled. Turn off web search, code
  execution and every other tool server for it.
- Keep `mcpo` on `127.0.0.1`. It has no sign-in by default, so anything that can reach the
  port can read your records.

## Claude Desktop

Turn off other connectors in the chat's tools menu while asking about your records.

## What to ask

The server finds facts in your records; the assistant does the rest. For example, ask it to
pull your last three A1c results and the medications started since, then draft a short,
clear message to your care team from that. The server provides the facts with dates and
sources. Drafting is the model's job, and sending stays with you.
