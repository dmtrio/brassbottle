# annotated-watch

An **env-only** plugin: one hybrid secret slot and **no MCP server**. It exists
solely to deliver each agent's resolved key into its shim environment.

`ANNOTATED_WATCH_KEY` is the poll-scope key the `watch-vault` skill's background
monitor reads from the environment — there's nothing to wire.

```yaml
plugins: [annotated-watch]
agent_secrets:
  - {agent: claude, slot: ANNOTATED_WATCH_KEY, secret: OBSIDIAN_WATCH_KEY_me_claude}
```

This is proof that the general secret-slot mechanism is what "identities" always
were — a watch key is just a secret slot with no server.
