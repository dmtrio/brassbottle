# proxyman

Bridges [Proxyman](https://proxyman.io)'s stdio MCP server over HTTP for
containers (traffic capture). **Remote** HTTP server on the Mac host, port
**8813**. Nothing is baked.

```yaml
plugins: [proxyman]
```

## Start the host service

```bash
./service.sh proxyman      # Proxyman.app must be running; leave it up (tmux/launchd)
```

First run self-generates `PROXYMAN_BRIDGE_KEY` into `secrets.env`; declare it in
`common_secrets` to make it the shared default. The bridge binds localhost only
and requires the key on inbound requests.
