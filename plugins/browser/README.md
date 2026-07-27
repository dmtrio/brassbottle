# browser

A watchable desktop browser the agent drives (for research containers).
**Remote** HTTP server on the Mac host. Nothing is baked.

```yaml
plugins: [browser]
# optional — give this container its own bridge port (default 8814):
# plugin_ports: {browser: 8815}
```

## Start the host service

```bash
./service.sh browser <container>          # default: Brave if installed, else Chrome
./service.sh browser <container> chrome   # extra args are forwarded to the launcher
```

Each container gets its own browser instance, bridge port, profile directory,
API key, and file-exchange directory. The launcher reads `plugin_ports.browser`
from `containers/<container>.yml` (falling back to `host_port` in
`plugin.yml`, default **8814**).

Which `secrets.env` variable holds this container's API key is whatever the
manifest binds `RESEARCH_BROWSER_KEY` to in `common_secrets` — the launcher
reads that binding, so the bridge and the container always agree:

```yaml
common_secrets: {RESEARCH_BROWSER_KEY: RESEARCH_BROWSER_KEY_job_hunt}
```

With no binding it falls back to `RESEARCH_BROWSER_KEY_<container>` (`-`
becomes `_`). Either way the value self-generates into `secrets.env` on first
run.

## Per-container paths

| What | Host path | In container |
|---|---|---|
| Browser profile | `$BASE_PATH/browser-profiles/<container>` | — |
| MCP write scope / file exchange | `$BASE_PATH/browser-tmp/<container>` | `/artifacts/browser` |

Screenshots and uploads must use host paths under the TMPDIR above; see
`AGENTS.md` for agent-facing guidance.

## Notes

- Dedicated instance with its **own** profile dir — none of your cookies,
  sessions, or extensions. Windows appear on your desktop so you can watch and
  interrupt the agent.
- CDP debug port binds localhost only; the bridge requires `X-API-Key`.
- When two containers both enable `browser`, give each a distinct
  `plugin_ports.browser` value (host ports are exclusive, same idea as
  `ssh.port` / `remote.mosh_ports`).
