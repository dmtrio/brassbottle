# obsidian-annotated

The Annotated Obsidian MCP endpoint (`mcp-obsidian.dmetr.io`) — a remote HTTP
server on a real internet host (reached via the egress allowlist, so no
`host_port`, no host service), bridged to **local stdio** by `mcp-remote`. Its
required secret resolves per agent, and the server is wired only where the
manifest supplies an effective key.

```yaml
plugins: [obsidian-annotated]
agent_secrets:
  - {agent: claude,       slot: OBSIDIAN_ANNOTATED_KEY, secret: OBSIDIAN_KEY_me_claude}
  - {agent: cursor-agent, slot: OBSIDIAN_ANNOTATED_KEY, secret: OBSIDIAN_KEY_bot_cursor_agent}
```

## Per-agent wiring

`djinn up` delivers each bound agent's key into its own `<agent>.env` and wires
the server per agent. Because the spec is local (`command: mcp-remote`), every
bound agent gets the **same** entry — a stdio command — through the ordinary
local wiring path; `mcp-remote` substitutes `${OBSIDIAN_ANNOTATED_KEY}` into the
`Authorization` header from its own process env at connect time. So no config
file on disk carries the key, and no agent needs to understand remote-MCP
headers.

The per-agent key is what Annotated uses to attribute comment threads, and it is
preserved: each agent's shim env holds its own resolved key, so each connects as
itself.

This replaced an earlier remote (`url:` + `headers:`) shape, under which
cursor-agent and pi — which cannot expand `${VAR}` inside a remote header — had
the literal key baked into their config files. `mcp-remote` is a base tool
installed once by the Dockerfile; see `plugins/axiom/` for the same pattern.

## Access

`requires: [OBSIDIAN_ANNOTATED_KEY]` gates the server: an agent with no
effective key for the slot gets no entry at all. Binding a key is the only way
an agent reaches the vault, before and after the bridge change.
