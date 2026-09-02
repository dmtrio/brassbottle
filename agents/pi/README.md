# pi

Earendil's [pi](https://github.com/earendil-works/pi-coding-agent) coding agent
(`@earendil-works/pi-coding-agent`, installed globally via npm).

## MCP wiring

pi ships **no built-in MCP client**, so this agent is wired in two halves:

1. **`up.sh` wiring** (`src/wire_plugins.py`, descriptor below) renders every
   enabled plugin's servers into `~/.pi/agent/mcp.json` — the same
   `mcpServers` JSON shape Claude uses. By itself that file is inert.
2. **`pi-mcp-adapter`** (`./mcp-adapter/`, baked at build into
   `~/.pi/agent/extensions/pi-mcp-adapter/`) is the consumer: at
   `session_start` it reads the config, connects to each server, and
   registers every tool as a callable pi tool. `/mcp` reports per-server
   status.

`${VAR}` refs never become real keys on disk: remote servers render as
`mcp-remote` shims that expand refs from pi's process env, and the adapter
expands refs in native `{type: http}` entries (and in `command`/`args`/`env`)
from the same env. A ref with no matching env var fails that one server at
connect time with a precise message instead of sending an empty credential —
per-server failures never block pi startup.

## Config shape

```json
{
  "mcpServers": {
    "serena":   { "command": "bash", "args": ["-c", "..."] },
    "gateway":  { "type": "http", "url": "http://host.docker.internal:8811/mcp",
                  "headers": { "Authorization": "Bearer ${MCP_GATEWAY_TOKEN}" } },
    "obsidian": { "command": "mcp-remote", "args": ["https://…/mcp", "--header", "…"] }
  }
}
```

stdio and native-http entries are both bridged; an entry with `command` is
stdio, one with `url` (and no `command`) is HTTP (streamable first, SSE
fallback).

## Rebuilding

The adapter is baked at image build (agent `install:` block runs `npm ci`
from the committed `package-lock.json` in `/opt/agents/pi/mcp-adapter`, then
copies the directory into the extensions dir). Any change under `agents/pi/`
requires an image rebuild to take effect in containers.
