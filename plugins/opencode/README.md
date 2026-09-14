# opencode

An **env-only** plugin that delivers `OPENCODE_API_KEY` to selected agent
shims. It does not install a harness, proxy API traffic, or add an MCP server.

One key serves both OpenCode products. Pi ships native providers for each:

| Provider id   | Product                                   | Billing                    |
|---------------|-------------------------------------------|----------------------------|
| `opencode-go` | OpenCode Go: curated open-weight models   | $10/month, per-model caps  |
| `opencode`    | OpenCode Zen: Go models plus Claude/GPT   | pay per request, credits   |

Bind the key to the `pi` agent:

```yaml
plugins: [opencode]
agent_secrets:
  - {agent: pi, slot: OPENCODE_API_KEY, secret: OPENCODE_KEY_pi}
```

Set `OPENCODE_KEY_pi` in the bottle's local `secrets.env`; do not put the
secret value in the manifest. The selected agent receives it as
`OPENCODE_API_KEY` when its shim starts.

```bash
pi --list-models opencode-go
pi -p --no-session --provider opencode-go --model kimi-k2.7-code \
  "Inspect the current worktree and implement the requested change."
```

## Notes

- Go limits are per model on 5-hour (20%), weekly (50%) and monthly windows,
  shared by every agent using the same key. A fan-out on one model can hit the
  5-hour cap; spread subagents across models.
- Only one member per OpenCode workspace can hold the Go subscription.
- Privacy is ordinary provider terms (no training, 0-day retention for most Go
  models, 30 days for a few). It is not an attested enclave; use the `tinfoil`
  plugin for work the inference operator must not be able to read.
- Docs: https://opencode.ai/docs/go/ and https://opencode.ai/docs/zen/
