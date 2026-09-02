# pi-mcp-adapter

MCP client bridge for pi — the consumer half of pi's MCP wiring. `up.sh`
renders every enabled plugin's MCP servers into `~/.pi/agent/mcp.json`;
pi itself has no MCP client, so this extension (baked at image build into
`~/.pi/agent/extensions/pi-mcp-adapter/`) is what makes the file live.

At `session_start` it connects to each configured server, lists its tools,
and registers them as callable pi tools (`mcp_<server>_<tool>`); `/mcp`
reports per-server status. stdio servers spawn as child processes; remote
servers connect over streamable HTTP with an SSE fallback.

`${VAR}` refs in `url`/`headers`/`command`/`args`/`env` are expanded from
pi's process env (the agent shim sources `~/.agent-keys/pi.env` with
`set -a`). A ref with no value fails that one server at connect time with a
precise message; per-server failures never block pi startup, and resolved
secret values are redacted from everything shown to the LLM or user.

Dependencies are pinned in `package-lock.json`; the agent's `install:` block
runs `npm ci --omit=dev` from it at build and copies the directory into the
extensions dir — rebuild the image to ship changes.
