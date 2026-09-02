# pi

Earendil's [pi](https://github.com/badlogic/pi-mono) coding agent
(`@earendil-works/pi-coding-agent`, installed globally via npm).

## MCP wiring

pi ships **no built-in MCP client**, so this agent is wired in two halves:

1. **`up.sh` wiring** (`src/wire_plugins.py`, descriptor below) renders every
   enabled plugin's servers into `~/.pi/agent/mcp.json` — the same
   `mcpServers` JSON shape Claude uses.
2. **[pi-mcp-adapter](https://pi.dev/packages/pi-mcp-adapter)** (upstream
   package, installed pinned at build via `pi install
   npm:pi-mcp-adapter@<version>`) is the consumer: it reads that file
   natively (Pi-global override, precedence 4 in its merge chain), connects
   servers lazily, and exposes their tools through a single proxy `mcp`
   tool. `/mcp` in pi reports per-server status.

Bumping the adapter = editing the pinned version in `agent.yml` + rebuild.

### Secrets

`${VAR}` refs never become real keys on disk. The adapter interpolates
`${VAR}` / `$env:VAR` in `env`, `cwd`, `url`, `headers`, and `bearerToken`
from pi's process env — and the agent shim sources `~/.agent-keys/pi.env`
with `set -a`, so agent-scoped secrets resolve at connect time. Remote
servers rendered as `mcp-remote` shims keep working unchanged: the shim
expands its own refs from the inherited env. A ref with no value fails that
one server (lazy: only when used) instead of sending an empty credential.

### Config interplay with the workspace

The adapter merges project-level `.mcp.json` too (later entries win), and
session cwds under `/workspace/repos` carry the symlinked canonical config.
Same-named servers resolve to the project file (same content djinn
rendered); servers from it whose secret slots pi doesn't hold surface as
per-server failures via `/mcp` — noisy servers are the exception, and lazy
connect means they cost nothing until called. `hostConfigDiscovery` stays
`off` (the default): no automatic adoption of Claude/Cursor configs.
