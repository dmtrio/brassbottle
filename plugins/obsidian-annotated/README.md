# obsidian-annotated

The Annotated Obsidian MCP endpoint (`mcp-obsidian.dmetr.io`). **Remote** HTTP
server on a real internet host (reached via the egress allowlist, so no
`host_port`, no host service). Its required secret resolves per agent, and the
server is wired only where the manifest supplies an effective key.

```yaml
plugins: [obsidian-annotated]
agent_secrets:
  - {agent: claude,       slot: OBSIDIAN_ANNOTATED_KEY, secret: OBSIDIAN_KEY_me_claude}
  - {agent: cursor-agent, slot: OBSIDIAN_ANNOTATED_KEY, secret: OBSIDIAN_KEY_bot_cursor_agent}
```

## Per-agent wiring

`djinn up` delivers each bound agent's key into its own `<agent>.env` and wires the
server per agent:

- **claude** — `.mcp.json` keeps the `${VAR}` ref (the shim expands it).
- **cursor-agent / pi** — the literal key is baked into their config
  (they can't expand env refs in remote headers).
- **codex** — wired into its managed TOML block as `url` +
  `bearer_token_env_var` (codex's own native remote-MCP shape); codex reads
  the token from its own process env at connect time, so the file carries no
  literal secret and no `${VAR}` ref either.
