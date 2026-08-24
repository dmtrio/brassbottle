# ngrok

A **CLI plugin, not an MCP server.** ngrok is just a command-line app plus a
secret, so this plugin uses the server-less shape (like
[`annotated-watch`](../annotated-watch/README.md)) with an install block added:

- **`install:`** bakes the ngrok v3 static binary into the image at build time.
- **`secrets:`** delivers `NGROK_AUTHTOKEN` into each agent's shim environment.
  ngrok reads that variable natively — no `ngrok config add-authtoken` step.
- **`egress:`** allowlists `connect.ngrok-agent.com`, the agent's outbound
  tunnel connection at runtime.

Because it carries an `install:` block, ngrok is **baked into the shared image**:
enabling it in a new manifest needs an **image rebuild**, not just `./djinn up`.

## Enable it

```yaml
plugins: [ngrok]
common_secrets: [NGROK_AUTHTOKEN]   # shared default: binds the slot to a secrets.env var
```

Add the token to `secrets.env`:

```
NGROK_AUTHTOKEN=2abc...your_token
```

Then rebuild the image and `./djinn up <container>`. Verify inside the container:

```bash
ngrok version          # binary is on PATH
ngrok http 3000        # authenticates from NGROK_AUTHTOKEN, tunnels a local port
```

## ⚠ Security posture — read before enabling

ngrok deliberately punches an **inbound** path from the public internet to a
local port. That is the opposite of this container's default design (an
egress-firewalled isolation box). The egress allowlist still governs the
outbound leg — the agent connects *out* to ngrok's edge, which proxies inbound
traffic back down the tunnel — but the net effect is that a local service
becomes publicly reachable for the tunnel's lifetime.

Enable ngrok only in containers where exposing a local port is an intentional
goal (e.g. sharing a dev server, receiving a webhook). It is not a general-use
plugin. The token in `secrets.env` is account-scoped; treat it like any other
credential.

## Per-agent tokens (optional)

`common_secrets: [NGROK_AUTHTOKEN]` shares one account token across every
enabled agent, which matches ngrok's one-token-per-account free tier. For
attribution with a paid plan that issues multiple tokens, override individual
agents via `agent_secrets` (each names its own `secrets.env` var); drop
`common_secrets` if you don't want a shared fallback for the rest:

```yaml
plugins: [ngrok]
agent_secrets:
  - {agent: claude, slot: NGROK_AUTHTOKEN, secret: NGROK_AUTHTOKEN_claude}
  - {agent: cursor-agent, slot: NGROK_AUTHTOKEN, secret: NGROK_AUTHTOKEN_cursor}
```

To disable ngrok for one agent while others keep it, use `disabled: true`:

```yaml
agent_secrets:
  - {agent: cursor-agent, slot: NGROK_AUTHTOKEN, disabled: true}
```
