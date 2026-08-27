# proxyman

Bridges [Proxyman](https://proxyman.io)'s stdio MCP server over HTTP for
containers (traffic capture). Host HTTP bridge on port **8813**; containers
reach it via a **local** `mcp-remote` stdio proxy (a base tool, installed once
by the `Dockerfile` — this plugin has no `install:` block).

```yaml
plugins: [proxyman]
```

## Start the host service

```bash
./djinn service proxyman      # Proxyman.app must be running; leave it up (tmux/launchd)
```

First run self-generates `PROXYMAN_BRIDGE_KEY` into `secrets.env`; declare it in
`common_secrets` to make it the shared default. The bridge binds localhost only
and requires the key on inbound requests.
