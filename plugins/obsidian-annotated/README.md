# obsidian-annotated

The Annotated Obsidian MCP endpoint (`mcp-obsidian.dmetr.io`) — a remote HTTP
server on a real internet host, reached via the egress allowlist, so no
`host_port` and no host service. Its required secret resolves per agent, and the
server is wired only where the manifest supplies an effective key.

```yaml
plugins: [obsidian-annotated]
agent_secrets:
  - {agent: claude,       slot: OBSIDIAN_ANNOTATED_KEY, secret: OBSIDIAN_KEY_me_claude}
  - {agent: cursor-agent, slot: OBSIDIAN_ANNOTATED_KEY, secret: OBSIDIAN_KEY_bot_cursor_agent}
```

## How each agent reaches it

`djinn up` delivers each bound agent's key into its own `<agent>.env`, then
wires the server per agent — the plugin declares the endpoint, the wiring layer
picks the transport:

- **claude, codex, kimi** — a native remote entry holding the `${SLOT}` ref (or,
  for codex/kimi, the env var's *name* in their own `bearer_token_env_var` /
  `bearerTokenEnvVar` field). No proxy.
- **cursor-agent, pi, antigravity-cli** — cannot expand a `${VAR}` inside a
  remote header, so they get an `mcp-remote` stdio shim with the same ref;
  `mcp-remote` substitutes it from that agent's own process env at connect time.

Either way the key is in **no config file and on no command line**, and an agent
moves off the shim by itself if it gains native support. This replaced a design
under which the second group had the raw key written into their config files.

The per-agent key is what Annotated uses to attribute comment threads, and it is
preserved on both paths: each agent's shim env holds its own resolved key, so
each connects as itself.

## Access

`requires: [OBSIDIAN_ANNOTATED_KEY]` gates the server: an agent with no
effective key for the slot gets no entry at all. Binding a key is the only way
an agent reaches the vault, before and after the bridge change.
