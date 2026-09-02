# openrouter

An **env-only** plugin that delivers `OPENROUTER_API_KEY` to selected agent
shims. It does not install a harness, proxy API traffic, or add an MCP server.

For Pi, bind the key to the `pi` agent and run its native OpenRouter provider:

```yaml
plugins: [openrouter]
agent_secrets:
  - {agent: pi, slot: OPENROUTER_API_KEY, secret: OPENROUTER_KEY_pi}
```

Set `OPENROUTER_KEY_pi` in the bottle's local `secrets.env`; do not put the
secret value in the manifest. The selected agent receives it as
`OPENROUTER_API_KEY` when its shim starts.

```bash
pi -p --no-session --provider openrouter --model qwen/qwen3.8-27b \
  "Inspect the current worktree and implement the requested change."
```
